#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Create a linked task worktree, or route to an explicitly configured host helper."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

DEFAULT_CONFIG_RELATIVE = Path(".config/codex-skills/worktrees.json")


def git_environment() -> dict[str, str]:
    # These are local operations on an explicit repository. Do not inherit a
    # hook caller's discovery, object storage, or command-scoped configuration.
    # Normal repository/user/system config files still apply.
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def delegation_active(args: list[str]) -> bool:
    marker = os.environ.get("DEV_WORKTREE_DELEGATED")
    if not marker:
        return False
    pid, separator, request = marker.partition(":")
    # Identical inherited callbacks remain loops even after a shim detaches.
    if not separator or request == json.dumps(args):
        return True
    # A different request can start after the exec'd host has exited.
    # Keep the old conservative guard where POSIX signal-zero probing is absent.
    if os.name != "posix" or not pid.isdecimal() or int(pid) <= 0:
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


class Refused(ValueError):
    """The request cannot safely create a new task worktree."""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        env=git_environment(),
    )
    return result.stdout.strip()


def settings() -> dict[str, str]:
    explicit = os.environ.get("DEV_WORKTREE_CONFIG")
    config = Path(explicit).expanduser() if explicit else Path.home() / DEFAULT_CONFIG_RELATIVE
    settings = json.loads(config.read_text()) if explicit or os.path.lexists(config) else {}
    keys = {"root", "hostHelper", "evidenceRoot", "retainedRoot", "evidenceMount"}
    if not isinstance(settings, dict) or set(settings) - keys:
        raise Refused(f"Expected a JSON object with keys from {sorted(keys)}.")
    for value in settings.values():
        if not isinstance(value, str) or not value or not Path(value).expanduser().is_absolute():
            raise Refused("Configured paths must be absolute (a leading ~ is allowed).")
    if "hostHelper" in settings and "root" not in settings:
        raise Refused("A hostHelper requires an explicit root and helper path.")
    return settings


def configuration() -> tuple[Path, Path | None]:
    values = settings()
    return (Path(values.get("root", "~/Developer/worktrees")).expanduser(),
            Path(values["hostHelper"]).expanduser() if "hostHelper" in values else None)


def artifact_roots() -> tuple[Path, Path]:
    values = settings()
    roots = [Path(values.get(key, default)).expanduser().absolute() for key, default in (
        ("evidenceRoot", "~/Developer/task-evidence"), ("retainedRoot", "~/Developer/retained-artifacts"),
        ("root", "~/Developer/worktrees"))]
    if roots[0].name != "task-evidence":
        raise Refused("evidenceRoot must be named task-evidence.")
    for root in roots:
        if any(parent.is_symlink() or (parent / ".git").exists() or parent.name == ".git"
               for parent in (root, *root.parents)):
            raise Refused(f"Artifact/worktree root is redirected or inside Git: {root}")
        if Path.home().resolve().is_relative_to(root.resolve()):
            raise Refused(f"Root must not contain the home directory: {root}")
    for index, root in enumerate(roots):
        if any(root.resolve().is_relative_to(other.resolve()) or other.resolve().is_relative_to(root.resolve())
               for other in roots[index + 1:]):
            raise Refused("Evidence, retained and worktree roots must not overlap.")
    mount = values.get("evidenceMount")
    if mount and (not os.path.ismount(Path(mount).expanduser()) or not roots[0].is_relative_to(Path(mount).expanduser())):
        raise Refused("evidenceMount must be mounted and contain evidenceRoot.")
    return roots[0], roots[1]


def host_environment(args: list[str]) -> dict[str, str]:
    evidence, _ = artifact_roots()
    return {**os.environ, "DEV_WORKTREE_EVIDENCE_ROOT": str(evidence),
            "DEV_WORKTREE_DELEGATED": f"{os.getpid()}:{json.dumps(args)}"}


def sweep_evidence(repo: Path, task: str) -> None:
    import task_evidence_gc

    try:
        evidence, retained = artifact_roots()
        task_evidence_gc.sweep(apply=True, budget_seconds=5)
        name = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent.name
        print(f"Task evidence: {evidence / name / task}; retained root: {retained}. "
              "See skills/github/references/worktrees.md for retention.", file=sys.stderr)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"dev-worktree: evidence sweep unavailable; creation continues: {exc}", file=sys.stderr)


def create(repo_arg: str, task: str, start: str, root: Path) -> Path:
    repo = Path(repo_arg).resolve(strict=True)
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    if common.name != ".git" or git(repo, "rev-parse", "--is-bare-repository") != "false":
        raise Refused("Use a normal main repository or linked worktree; bare/submodule/separate-git-dir layouts are unsupported.")
    main = common.parent
    for component in (main.name, task):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", component):
            raise Refused(f"Invalid repository/task slug: {component}")
    # Resolve aliases (including macOS /var) before checking repository containment.
    root = root.resolve()
    if root.is_relative_to(main) or root.is_relative_to(repo):
        raise Refused("The worktree root must be outside the source repository.")
    parent = root / main.name
    if parent.is_symlink():
        raise Refused("The repository worktree parent must not be a symlink.")
    # A root outside repositories can still contain an existing same-named checkout.
    ancestor = parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    probe = subprocess.run(
        ["git", "-C", str(ancestor), "rev-parse", "--git-dir"],
        capture_output=True, text=True, env={**git_environment(), "LC_ALL": "C"},
    )
    if probe.returncode == 0:
        raise Refused("The worktree destination must be outside repositories and Git metadata.")
    if "not a git repository" not in probe.stderr:
        raise Refused("Cannot verify that the destination is outside repositories; inspect the Git probe failure.")
    destination = parent / task
    if os.path.lexists(destination):
        raise Refused(f"Destination already exists: {destination}")
    commit = git(repo, "rev-parse", "--verify", "--end-of-options", f"{start}^{{commit}}")
    branch = f"work/{task}"
    git(repo, "check-ref-format", "--branch", branch)
    refs = git(repo, "for-each-ref", "--format=%(refname)", f"refs/heads/{branch}").splitlines()
    if f"refs/heads/{branch}" in refs:
        raise Refused(f"Branch already exists: {branch}; choose a new task slug.")
    parent.mkdir(parents=True, exist_ok=True)
    # Git also refuses stale registered destinations and branch races. Never force or prune.
    git(repo, "worktree", "add", "-b", branch, str(destination), commit)
    return destination


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args in (["--help"], ["-h"]):
        print(f"Usage: dev-worktree REPOSITORY TASK-SLUG START-POINT\n       dev-worktree retire [--dry-run] [--offline-only] PATH\nConfig: ~/{DEFAULT_CONFIG_RELATIVE} (override: DEV_WORKTREE_CONFIG)\nSee skills/github/references/worktrees.md for host integration and cleanup.")
        return 0
    try:
        root, helper = configuration()
        if helper:
            if delegation_active(args):
                raise Refused("Host helper delegated back to the catalog; configure the actual host executable.")
            if helper.resolve() in {Path(__file__).resolve(), Path(__file__).with_name("dev-worktree").resolve()}:
                raise Refused("hostHelper must not refer to the catalog helper itself.")
        if args and args[0] == "retire":
            parser = argparse.ArgumentParser(prog="dev-worktree retire")
            parser.add_argument("--dry-run", action="store_true")
            parser.add_argument("--offline-only", action="store_true")
            parser.add_argument("path", type=Path)
            retirement = parser.parse_args(args[1:])
            return retire_task(retirement.path, dry_run=retirement.dry_run, offline_only=retirement.offline_only)
        if len(args) == 3 and args[0] != "retire":
            refresh_primary(Path(args[0]))
            retire_finished(Path(args[0]))
            sweep_evidence(Path(args[0]), args[1])
        if helper:
            # The host owns volume identity, locks and retirement; do not mkdir or fall back.
            # Preserve the host's signal traps, including relocking during interrupted retirement.
            os.environ.update(host_environment(args))
            os.execv(str(helper), [str(helper), *args])
        if len(args) != 3 or args[0] == "retire":
            raise Refused("Usage: dev-worktree REPOSITORY TASK-SLUG START-POINT; see --help for retirement.")
        print(create(*args, root))
        return 0
    except (Refused, OSError, UnicodeError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        print(f"dev-worktree: {detail}", file=sys.stderr)
        return 1


def checkout_rules(source: Path | None = None) -> ModuleType:
    spec = importlib.util.spec_from_file_location("checkout_rules", source or Path(__file__).with_name("reconcile-runtime-checkout.py"))
    if spec is None or spec.loader is None:
        raise Refused("Checkout rules unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def refresh_primary(checkout: Path) -> None:
    try:
        receipt = checkout_rules().reconcile_primary_checkout(checkout)
        print(f"dev-worktree: primary checkout refresh: {json.dumps(receipt)}", file=sys.stderr)
    except Exception as exc:
        print(f"dev-worktree: primary checkout refresh unavailable; inspect checkout: {type(exc).__name__}: {exc}", file=sys.stderr)


def retire_finished(checkout: Path) -> None:
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("landed_worktrees.py")),
                                 "--checkout", str(checkout), "--all", "--apply", "--budget-seconds", "30"],
                                capture_output=True, text=True, env=git_environment())
    except OSError as exc:
        print(f"dev-worktree: retirement unavailable; existing trees kept: {exc}", file=sys.stderr)
        return
    print(result.stdout.strip() or result.stderr.strip() or "dev-worktree: retirement evidence unavailable; trees kept",
          file=sys.stderr)


def retire_task(target: Path, *, dry_run: bool, offline_only: bool = False) -> int:
    import landed_worktrees

    if not target.is_dir():
        raise Refused(f"Retirement target is unavailable: {target}")
    common = Path(git(target, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    decision = landed_worktrees.target_decision(target, common.parent)
    if decision["decision"] != "candidate":
        print(json.dumps({"ok": False, "worktrees": [decision]}))
        return 2
    _, helper = configuration()
    if helper is None:
        outcome = landed_worktrees.portable_retire(target, dry_run=dry_run)
        if outcome.stdout:
            print(outcome.stdout, end="")
        if outcome.stderr:
            print(outcome.stderr, end="", file=sys.stderr)
        return outcome.returncode
    args = ["retire", *(["--dry-run"] if dry_run else []), *(["--offline-only"] if offline_only else []), str(target)]
    os.environ.update(host_environment(args))
    os.execv(str(helper), [str(helper), *args])
    return 0


if __name__ == "__main__":
    sys.exit(main())
