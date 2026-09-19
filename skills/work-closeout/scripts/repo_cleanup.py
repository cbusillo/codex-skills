#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read-only cleanup inventory, freshness checks, and post-action verification.

No subcommand removes, moves, resets, cleans, unlocks, or changes repository data.
Saved manifests are private, short-lived evidence, never deletion authorization.
"""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import secrets
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cleanup_git
import cleanup_probe
from cleanup_probe import PRIVATE_PARTS, ProbeError, scan_root, signature


SCHEMA = 1
LIMITS = {"max_entries": 20000, "max_bytes": 256 * 1024 * 1024, "max_depth": 64,
          "root_timeout": 10.0, "total_timeout": 60.0, "exclude": []}
POLICY = {"may_delete": False, "authorization": "not_granted",
          "live_use_absence_proves_inactive": False,
          "space_reclaimed_bytes": None, "snapshot_is_atomic": False}


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def absolute(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def public(value):
    """Never emit keyed content fingerprints, URL fingerprints, or manifest keys."""
    if isinstance(value, dict):
        return {key: public(item) for key, item in value.items() if not key.startswith("_")}
    if isinstance(value, list):
        return [public(item) for item in value]
    return value


def bindings(preserved: list[str]) -> list[dict]:
    code_home = os.environ.get("CODE_HOME") or os.environ.get("CODEX_HOME") or "~/.code"
    result = [{"kind": "active_cwd", "path": os.path.realpath(os.getcwd())},
              {"kind": "skills_runtime", "path": os.path.realpath(absolute(code_home) + "/skills")}]
    result.extend({"kind": "explicit_preserve", "path": os.path.realpath(absolute(path))} for path in preserved)
    # Other installed apps, IDEs, agents and shared caches may have bindings we cannot observe.
    return result


def git_core(snapshot: dict, removed: set[str] | None = None, moved: dict[str, str] | None = None) -> dict:
    removed, moved = removed or set(), moved or {}
    worktrees = []
    for original in snapshot.get("worktrees", []):
        if original["path"] in removed:
            continue
        item = {key: value for key, value in original.items()
                if key not in ("tracked", "ignored", "identity")}
        item["path"] = moved.get(item["path"], item["path"])
        worktrees.append(item)
    return {**{key: snapshot.get(key) for key in ("root", "common_dir", "default_branch", "refs",
                                                "remotes", "operations", "stashes", "coverage")},
            "common_identity": {key: snapshot.get("common_identity", {}).get(key) for key in ("device", "inode")},
            "worktrees": sorted(worktrees, key=lambda entry: entry["path"])}


def root_git_paths(root: str, repository: dict) -> tuple[set[str], set[str]]:
    tracked, ignored = set(), set()
    for worktree in repository.get("worktrees", []):
        base: str = worktree["path"]
        for name, destination in (("tracked", tracked), ("ignored", ignored)):
            names: list[str] = worktree.get(name, [])
            for relative in names:
                full = os.path.join(base, relative)
                if within(full, root):
                    destination.add(os.path.relpath(full, root))
    return tracked, ignored


def assess(root: dict, repository: dict, runtime_bindings: list[dict]) -> None:
    reasons = []
    resolved = root.get("resolved", root["requested"])
    for binding in runtime_bindings:
        if within(binding["path"], resolved) or (binding["kind"] != "active_cwd" and within(resolved, binding["path"])):
            reasons.append(binding["kind"])
    if any(part.lower() in PRIVATE_PARTS for part in Path(resolved).parts):
        reasons.append("protected_root")
    worktree = next((item for item in repository.get("worktrees", []) if item["path"] == resolved), None)
    if worktree:
        branch = worktree.get("branch", "").removeprefix("refs/heads/")
        if resolved == repository.get("root"):
            reasons.append("source_checkout")
        if branch == repository.get("default_branch") or branch in ("main", "master", "production", "stable", "release") or branch.startswith(("release/", "production/", "source/")):
            reasons.append("protected_branch")
        for name in ("locked", "prunable", "detached", "dirty_tracked"):
            if worktree.get(name):
                reasons.append(name)
        if worktree.get("operations") or repository.get("operations"):
            reasons.append("git_operation")
        if repository.get("ref_coverage", {}).get(worktree.get("branch"), {}).get("state") != "covered":
            reasons.append("remote_coverage_unknown")
    entries = root.get("entries", {})
    if any(item["category"] == "protected_private" for item in entries.values()):
        reasons.append("protected_contents")
    if any(item["category"] == "tracked_source" for item in entries.values()) and not worktree:
        reasons.append("tracked_source")
    if any(item["kind"] in ("symlink", "special") for item in entries.values()):
        reasons.append("filesystem_boundary")
    if any(item["stat"]["links"] > 1 and item["kind"] == "file" for item in entries.values()):
        reasons.append("shared_file_identity")
    if root.get("root_alias"):
        reasons.append("root_alias")
    if root.get("live_use", {}).get("state") == "observed":
        reasons.append("observed_live_use")
    if root["coverage"] != "completed":
        reasons.append("incomplete_coverage")
    if repository.get("coverage") != "completed":
        reasons.append("git_coverage_unknown")
    root["holds"] = sorted(set(reasons))
    unknown_files = any(item["kind"] == "file" and item["category"] == "unclassified"
                        and path != ".git" for path, item in entries.items())
    if root["coverage"] not in ("completed", "excluded"):
        root["disposition"] = "Could not check"
    elif reasons:
        root["disposition"] = "Keep"
    elif unknown_files:
        root["disposition"] = "Needs review"
    else:
        root["disposition"] = "Possible cleanup"
    root["unresolved"] = ["ownership_and_authorization", "unobserved_app_runtime_or_ide_use"]
    if unknown_files:
        root["unresolved"].append("unclassified_local_files")


def inventory(repo: str, roots: list[str], *, options: dict | None = None, key: bytes | None = None,
              content: bool = False, preserved: list[str] | None = None, offline: bool = False,
              default_roots: bool = True) -> dict:
    options = {**LIMITS, **(options or {})}
    key = key or secrets.token_bytes(32)
    start = time.monotonic()
    deadline = start + options["total_timeout"]
    result: dict[str, Any] = {"schema_version": SCHEMA, "operation": "inventory", "started_at": timestamp(),
              "created_epoch": time.time(), "policy": dict(POLICY), "options": options,
              "_key": key.hex(), "roots": [], "preserve_roots": preserved or [], "offline": offline}
    interrupted = False

    def probe_call(operation, arguments, timeout) -> dict[str, Any]:
        nonlocal interrupted
        if interrupted:
            return {"error": "interrupted"}
        try:
            return cleanup_probe.bounded(operation, arguments, min(timeout, deadline - time.monotonic()))
        except KeyboardInterrupt:
            interrupted = True
            return {"error": "interrupted"}
        except ProbeError as exc:
            return {"error": str(exc)}
        except OSError:
            return {"error": "probe_unavailable"}

    runtime = probe_call(bindings, (preserved or [],), 2)
    result["bindings"] = runtime.get("result", [])
    git_result = probe_call(cleanup_git.snapshot, (absolute(repo), key, not offline), 20)
    repository: dict[str, Any] = git_result.get("result", {"coverage": "unavailable", "error": git_result.get("error")})
    result["repository"] = repository
    requested = list(dict.fromkeys(absolute(root) for root in roots))
    if not requested and default_roots:
        requested = [item["path"] for item in repository.get("worktrees", [])] or [absolute(repo)]
    if len(requested) > 64:
        raise ProbeError("root_limit")
    seen: dict[tuple[int, int], dict] = {}
    for requested_root in requested:
        root: dict[str, Any] = {"requested": requested_root, "started_at": timestamp()}
        try:
            tracked, ignored = root_git_paths(requested_root, repository)
            probe = probe_call(
                scan_root, (requested_root, key if content else None, tracked, ignored, options),
                options["root_timeout"])
        except KeyboardInterrupt:
            interrupted = True
            probe = {"error": "interrupted"}
        if "result" in probe:
            root.update(probe["result"])
            identity = (root["identity"]["device"], root["identity"]["inode"])
            if identity in seen:
                root["alias_of"] = seen[identity]["requested"]
                root["unique_file_bytes"] = 0
            else:
                seen[identity] = root
            root["coverage"] = "excluded" if root["exclusions"] else "completed"
            live = probe_call(cleanup_probe.use_probe, (root["resolved"],), 3)
            root["live_use"] = live.get("result", {"state": "unknown", "reason": live.get("error")})
        else:
            reason = probe.get("error", "unavailable")
            root.update(coverage="permission-denied" if reason == "permission_denied" else
                        "timed-out" if reason in ("timed_out", "interrupted", "probe_interrupted") else "unavailable",
                        errors=[{"code": reason, "errno": probe.get("errno")}], live_use={"state": "unknown"})
        root["finished_at"] = timestamp()
        assess(root, repository, result["bindings"])
        if "error" in runtime:
            root["holds"].append("bindings_unknown")
            root["disposition"] = "Keep" if root["coverage"] == "completed" else "Could not check"
        result["roots"].append(root)
    # Changes in HEAD, refs, registrations, locks or repository state invalidate the entire observation.
    final_git = probe_call(cleanup_git.snapshot, (absolute(repo), key, not offline), 20)
    stable_git = "result" in final_git and git_core(repository) == git_core(final_git["result"])
    result["git_stable"] = stable_git
    if not stable_git:
        for root in result["roots"]:
            root["holds"].append("git_changed_or_unavailable")
            root["disposition"] = "Keep" if root["coverage"] == "completed" else "Could not check"
    result.update(finished_at=timestamp(), duration_seconds=round(time.monotonic() - start, 3),
                  complete=stable_git and repository.get("coverage") == "completed"
                  and all(root["coverage"] == "completed" and (not content or root.get("content_verified"))
                          for root in result["roots"]),
                  repo_argument=absolute(repo))
    return result


def save_manifest(path: str, report: dict, purpose: str, ttl: int) -> None:
    destination = Path(absolute(path))
    parent = destination.parent
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid() or str(parent.resolve()) != str(parent):
        raise ProbeError("manifest_parent_must_be_private_directory")
    if any(within(str(destination), root.get("resolved", root["requested"])) for root in report["roots"]):
        raise ProbeError("manifest_inside_scan_scope")
    report.update(_manifest_purpose=purpose, expires_epoch=time.time() + ttl)
    serialized = json.dumps(report, sort_keys=True)
    if len(serialized.encode()) > 32_000_000:
        raise ProbeError("manifest_too_large_reduce_scope")
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def load_manifest(path: str) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid() or info.st_size > 32_000_000:
            raise ProbeError("manifest_permissions_or_size_invalid")
        try:
            raw = stream.read(32_000_001)
            if len(raw.encode()) > 32_000_000 or signature(os.fstat(stream.fileno())) != signature(info):
                raise ProbeError("manifest_changed_or_too_large")
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("roots"), list):
                raise ValueError
            if any(not isinstance(root, dict) for root in data["roots"]):
                raise ValueError
            if data["schema_version"] != SCHEMA or len(bytes.fromhex(data["_key"])) != 32:
                raise ValueError
            if data["expires_epoch"] <= time.time():
                raise ProbeError("manifest_expired")
            if not data["roots"] or any(not root.get("content_verified") for root in data["roots"]):
                raise ProbeError("manifest_content_evidence_incomplete")
        except (ValueError, KeyError, TypeError) as exc:
            raise ProbeError("manifest_invalid") from exc
    return data


def comparable(root: dict, *, moved: bool = False) -> dict:
    if not moved:
        return {key: root.get(key) for key in ("requested", "resolved", "root_alias", "identity", "entries", "exclusions", "coverage")}
    return {path: {"kind": item["kind"], "mode": item["stat"]["mode"],
                   "size": item["stat"]["size"] if item["kind"] != "directory" else None,
                   "links": item["stat"]["links"] if item["kind"] != "directory" else None,
                   "uid": item["stat"]["uid"], "gid": item["stat"]["gid"],
                   "_content_tag": item.get("_content_tag")}
            for path, item in root["entries"].items()}


def absent(path: str) -> bool:
    try:
        os.lstat(path)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return True
        raise
    return False


def check_manifest(before: dict, *, removed: list[str] | None = None,
                   moved: list[list[str]] | None = None, max_age: int = 300) -> dict:
    removed_set = {absolute(path) for path in removed or []}
    move_map = {absolute(source): absolute(destination) for source, destination in moved or []}
    baseline_roots = {root["requested"]: root for root in before["roots"]}
    selected = removed_set | set(move_map)
    errors = []
    if (not selected <= set(baseline_roots) or removed_set & set(move_map)
            or len(set(move_map.values())) != len(move_map) or len(moved or []) != len(move_map)):
        raise ProbeError("action_targets_must_be_unique_manifest_roots")
    if not before["complete"] or before["options"]["exclude"]:
        raise ProbeError("baseline_coverage_incomplete")
    if any(not root.get("content_verified") for root in before["roots"]):
        raise ProbeError("baseline_content_evidence_incomplete")
    if not selected and time.time() - before["created_epoch"] > max_age:
        raise ProbeError("baseline_too_old_reinventory")
    targets = []
    for path, root in baseline_roots.items():
        if path in selected and (root.get("root_alias") or root.get("alias_of")):
            errors.append("action_on_alias_ambiguous")
        if path in selected:
            gone = cleanup_probe.bounded(absent, (path,), 2)
            if gone.get("result") is not True:
                errors.append("source_removal_unverified")
        if path in removed_set:
            if root["holds"] or "unclassified_local_files" in root["unresolved"]:
                errors.append("protected_or_ambiguous_root_removed")
        else:
            targets.append(move_map.get(path, path))
        if path in move_map and any(part.lower() in (".trash", ".trashes", "trash") for part in Path(move_map[path]).parts):
            errors.append("trash_is_not_durable_recovery")
        if path in move_map and any(hold in root["holds"] for hold in
                ("skills_runtime", "active_cwd", "explicit_preserve", "observed_live_use", "locked", "source_checkout", "protected_branch", "filesystem_boundary")):
            errors.append("protected_binding_or_boundary_moved")
    current = inventory(before["repo_argument"], targets, options=before["options"],
                        key=bytes.fromhex(before["_key"]), content=True, preserved=before["preserve_roots"],
                        offline=before["offline"], default_roots=False)
    if not current["complete"]:
        errors.append("current_coverage_incomplete")
    if git_core(before["repository"], removed_set, move_map) != git_core(current["repository"]):
        errors.append("git_postcondition_changed")
    current_roots = {root["requested"]: root for root in current["roots"]}
    for path, root in baseline_roots.items():
        if path in removed_set:
            continue
        after = current_roots.get(move_map.get(path, path))
        if not after or after.get("coverage") != "completed":
            errors.append("retained_or_moved_root_unverified")
        elif comparable(root, moved=path in move_map) != comparable(after, moved=path in move_map):
            errors.append("recovery_content_changed" if path in move_map else "retained_content_or_identity_changed")
        if after and path in move_map:
            if after.get("root_alias"):
                errors.append("recovery_destination_alias")
            if any(root["identity"][key] != after.get("identity", {}).get(key) for key in ("mode", "uid", "gid")):
                errors.append("recovery_root_permissions_changed")
    return {"schema_version": SCHEMA, "operation": "verify" if selected else "revalidate", "policy": dict(POLICY),
            "checked_at": timestamp(), "ok": not errors, "errors": sorted(set(errors)),
            "roots": public(current["roots"]), "removed": sorted(removed_set), "moved": move_map,
            "evidence": "postconditions_match" if selected and not errors else "snapshot_unchanged" if not errors else "unverified"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    scan = sub.add_parser("inventory", help="Read one repository and requested roots; default roots are registered worktrees.")
    scan.add_argument("--repo", required=True)
    scan.add_argument("--root", action="append", default=[])
    scan.add_argument("--preserve-root", action="append", default=[])
    scan.add_argument("--exclude", action="append", default=[], help="Literal relative path; excludes are reported and cannot be revalidated.")
    scan.add_argument("--offline", action="store_true", help="Explicitly excludes live remote evidence; cannot yield complete verification evidence.")
    scan.add_argument("--manifest", help="New private evidence file outside scanned roots; enables content fingerprinting.")
    scan.add_argument("--purpose", help="Why the private manifest is retained.")
    scan.add_argument("--expires-in", type=int, default=3600)
    for option in ("max_entries", "max_bytes", "max_depth", "root_timeout", "total_timeout"):
        scan.add_argument("--" + option.replace("_", "-"), type=type(LIMITS[option]), default=LIMITS[option])
    for name in ("revalidate", "verify"):
        check = sub.add_parser(name)
        check.add_argument("--before", required=True)
        if name == "verify":
            check.add_argument("--removed", action="append", default=[])
            check.add_argument("--moved", nargs=2, action="append", default=[], metavar=("SOURCE", "DESTINATION"))
        else:
            check.add_argument("--max-age", type=int, default=300)
    for command_parser in (scan, *list(sub.choices.values())[1:]):
        command_parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        if args.operation == "inventory":
            if args.manifest and not args.purpose:
                parser.error("--manifest requires --purpose")
            if not 1 <= args.expires_in <= 86400 or any(not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0 for name in LIMITS if name != "exclude"):
                parser.error("limits must be positive; manifest expiry must be 1..86400 seconds")
            if any(Path(path).is_absolute() or ".." in Path(path).parts or path in ("", ".") for path in args.exclude):
                parser.error("exclusions must be literal relative descendant paths")
            report = inventory(args.repo, args.root, options={name: getattr(args, name) for name in LIMITS},
                               content=bool(args.manifest), preserved=args.preserve_root, offline=args.offline)
            if args.manifest:
                save_manifest(args.manifest, report, args.purpose, args.expires_in)
            success = report["complete"]
        else:
            if args.operation == "verify" and not (args.removed or args.moved):
                parser.error("verify requires at least one exact --removed or --moved root")
            report = check_manifest(load_manifest(args.before), removed=getattr(args, "removed", []),
                                    moved=getattr(args, "moved", []), max_age=getattr(args, "max_age", 300))
            success = report["ok"]
    except (OSError, ProbeError) as exc:
        report = {"schema_version": SCHEMA, "operation": args.operation, "policy": dict(POLICY),
                  "ok": False, "error": str(exc) if isinstance(exc, ProbeError) else "filesystem_unavailable"}
        success = False
    except (KeyError, TypeError, ValueError, AttributeError):
        report = {"schema_version": SCHEMA, "operation": args.operation, "policy": dict(POLICY),
                  "ok": False, "error": "invalid_input_structure"}
        success = False
    if args.json:
        print(json.dumps(public(report), indent=2, sort_keys=True))
    else:
        print(f"{args.operation}: {'evidence complete' if success else 'incomplete or changed'}; deletion is not authorized.")
        for root in report.get("roots", []):
            print(f"{root['disposition']}: {json.dumps(root['requested'])} ({root['coverage']})")
            print(f"  Holds: {', '.join(root['holds']) or 'none observed'}; live use: {root['live_use']['state']}.")
        if report.get("error") or report.get("errors"):
            print(json.dumps(report.get("error") or report.get("errors")))
    return 0 if success else 2


if __name__ == "__main__":
    sys.exit(main())
