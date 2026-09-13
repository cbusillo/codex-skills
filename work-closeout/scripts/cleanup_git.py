# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read-only Git evidence; remote advertisements are live, never fetch-cache claims."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from cleanup_probe import ProbeError, command, signature, tag


UNSAFE_ENV = {"GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
              "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
              "GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"}
OPERATIONS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "BISECT_LOG",
              "BISECT_START", "rebase-merge", "rebase-apply", "sequencer", "index.lock",
              "HEAD.lock", "config.lock", "packed-refs.lock", "shallow.lock")


def git(repo: str, *args: str, check: bool = True) -> tuple[int, bytes]:
    if UNSAFE_ENV.intersection(os.environ):
        raise ProbeError("git_environment_override")
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    result = command(["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                      "-c", "core.hooksPath=/dev/null", "-C", repo, *args], env=env)
    if check and result[0]:
        raise ProbeError("git_command_failed")
    return result


def git_text(repo: str, *args: str) -> str:
    return os.fsdecode(git(repo, *args)[1]).strip()


def registrations(repo: str) -> list[dict]:
    raw = git(repo, "worktree", "list", "--porcelain", "-z")[1]
    worktrees: list[dict] = []
    current: dict = {}
    for field in raw.split(b"\0"):
        if not field:
            if current:
                worktrees.append(current)
                current = {}
            continue
        key, _, value = field.partition(b" ")
        if key == b"worktree":
            current["path"] = os.fsdecode(value)
        elif key in (b"HEAD", b"branch"):
            current[os.fsdecode(key).lower()] = os.fsdecode(value)
        elif key in (b"locked", b"prunable", b"bare", b"detached"):
            current[os.fsdecode(key)] = True  # Do not print arbitrary lock/prune reason text.
    if current:
        worktrees.append(current)
    if not worktrees or len(worktrees) > 64:
        raise ProbeError("worktree_count_invalid")
    return worktrees


def operation_state(directory: str) -> list[dict]:
    result = []
    for name in OPERATIONS:
        path = Path(directory) / name
        try:
            result.append({"name": name, "stat": signature(path.lstat())})
        except FileNotFoundError:
            pass
    refs = Path(directory) / "refs"
    if refs.is_symlink():
        result.append({"name": "refs", "stat": signature(refs.lstat())})
    elif refs.is_dir():
        examined = 0

        def fail_unreadable(error):
            raise error

        for parent, dirs, files in os.walk(refs, followlinks=False, onerror=fail_unreadable):
            examined += len(dirs) + len(files)
            if examined > 10000:
                raise ProbeError("git_metadata_limit")
            for name in dirs + files:
                path = Path(parent) / name
                if name.endswith(".lock") or path.is_symlink():
                    result.append({"name": str(path.relative_to(directory)), "stat": signature(path.lstat())})
    return sorted(result, key=lambda item: item["name"])


def snapshot(repo: str, key: bytes, remote_probe: bool = True) -> dict:
    root = git_text(repo, "rev-parse", "--show-toplevel")
    common = git_text(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    start_registrations = registrations(root)
    raw_refs = git(root, "for-each-ref", "--format=%(refname)%00%(objectname)",
                   "refs/heads", "refs/tags", "refs/stash")[1]
    refs = {}
    for line in raw_refs.splitlines():
        name, oid = line.split(b"\0", 1)
        refs[os.fsdecode(name)] = os.fsdecode(oid)
    if len(refs) > 512:
        raise ProbeError("ref_limit")
    remotes = []
    default_branch = None
    live_tips: dict[str, str] = {}
    names = git_text(root, "remote").splitlines()
    if len(names) > 16:
        raise ProbeError("remote_limit")
    for name in names:
        remote: dict[str, Any] = {"name": name, "_url_tag": tag(key, git(root, "remote", "get-url", "--all", name)[1]),
                  "coverage": "excluded" if not remote_probe else "unavailable"}
        if remote_probe:
            try:
                code, output = git(root, "ls-remote", "--symref", "--", name,
                                   "HEAD", "refs/heads/*", "refs/tags/*", check=False)
                if code == 0:
                    advertised = {}
                    for line in output.splitlines():
                        value, _, ref = line.partition(b"\t")
                        if value.startswith(b"ref: ") and ref == b"HEAD":
                            if name == "origin" or ("origin" not in names and len(names) == 1):
                                default_branch = os.fsdecode(value[5:]).removeprefix("refs/heads/")
                        elif re.fullmatch(rb"[0-9a-f]{40,64}", value):
                            advertised[os.fsdecode(ref)] = os.fsdecode(value)
                    remote.update(coverage="completed", refs=advertised)
                    live_tips.update({f"{name}:{ref}": oid for ref, oid in advertised.items()})
            except ProbeError as exc:
                remote["error"] = str(exc)
        remotes.append(remote)
    worktrees = []
    for registration in start_registrations:
        item = dict(registration)
        path = item["path"]
        try:
            directory = git_text(path, "rev-parse", "--absolute-git-dir")
            item.update(identity=signature(os.stat(path)), git_dir=directory,
                        head=git_text(path, "rev-parse", "HEAD"),
                        operations=operation_state(directory),
                        tracked=[os.fsdecode(p) for p in git(path, "ls-files", "-z")[1].split(b"\0") if p],
                        ignored=[os.fsdecode(p) for p in git(path, "ls-files", "--others", "--ignored",
                                                          "--exclude-standard", "-z")[1].split(b"\0") if p])
            dirty = git(path, "status", "--porcelain=v1", "-z", "--untracked-files=no")[1]
            item.update(dirty_tracked=bool(dirty), _status_tag=tag(key, dirty), coverage="completed")
        except (OSError, ProbeError):
            item["coverage"] = "unavailable"
        worktrees.append(item)
    coverage = {}
    for name, oid in refs.items():
        covered = next((ref for ref, tip in live_tips.items() if tip == oid), None)
        method = "exact_advertisement" if covered else "unknown"
        if not covered and name.startswith("refs/heads/"):
            for ref, tip in live_tips.items():
                if ":refs/heads/" not in ref:
                    continue
                code, _ = git(root, "merge-base", "--is-ancestor", oid, tip, check=False)
                if code == 0:
                    covered, method = ref, "ancestor_of_live_tip"
                    break
        coverage[name] = {"state": "covered" if covered else "unknown", "method": method, "remote_ref": covered}
    stash_code, stash_data = git(root, "reflog", "show", "--format=%H", "refs/stash", check=False)
    if stash_code and "refs/stash" in refs:
        raise ProbeError("stash_evidence_unavailable")
    if start_registrations != registrations(root) or raw_refs != git(root, "for-each-ref",
            "--format=%(refname)%00%(objectname)", "refs/heads", "refs/tags", "refs/stash")[1]:
        raise ProbeError("git_changed")
    return {"root": root, "common_dir": common, "common_identity": signature(os.stat(common)),
            "default_branch": default_branch, "default_branch_evidence": "live" if default_branch else "unknown",
            "refs": refs, "ref_coverage": coverage, "remotes": remotes, "worktrees": worktrees,
            "operations": operation_state(common),
            "stashes": os.fsdecode(stash_data).splitlines() if stash_code == 0 else [],
            "coverage": "completed" if names and default_branch
            and all(item["coverage"] == "completed" for item in remotes + worktrees) else "unavailable"}
