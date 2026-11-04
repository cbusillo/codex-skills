#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import ModuleType

DEFAULT_CONFIG_RELATIVE = Path(".config/codex-skills/worktrees.json")


def git_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def delegation_active(args: list[str]) -> bool:
    marker = os.environ.get("DEV_WORKTREE_DELEGATED")
    if not marker:
        return False
    pid, separator, request = marker.partition(":")
    if not separator or request == json.dumps(args):
        return True
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
    for root in roots[:2]:
        if any(parent.is_symlink() for parent in (root, *root.parents)) or root != root.resolve():
            raise Refused(f"Artifact root must be canonical and not redirected: {root}")
        verify_outside_repository(root)
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
    environment = {**os.environ, "DEV_WORKTREE_DELEGATED": f"{os.getpid()}:{json.dumps(args)}"}
    environment.pop("DEV_WORKTREE_EVIDENCE_ROOT", None)
    try:
        environment["DEV_WORKTREE_EVIDENCE_ROOT"] = str(artifact_roots()[0])
    except (OSError, ValueError) as exc:
        print(f"dev-worktree: diagnostic root unavailable: {exc}", file=sys.stderr)
    return environment


def sweep_evidence(repo: Path, task: str) -> None:
    try:
        evidence, retained = artifact_roots()
        name = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent.name
        print(f"Task evidence: {evidence / name / task}; retained root: {retained}. "
              "See skills/github/references/worktrees.md for retention.", file=sys.stderr)
        sweep(apply=True, budget_seconds=5)
    except (ImportError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"dev-worktree: evidence sweep unavailable; creation continues: {exc}", file=sys.stderr)


RETENTION_SECONDS = 30 * 86400


def raise_walk_error(error: OSError) -> None:
    raise error


def expired(name: str, parent_fd: int, cutoff: float) -> bool:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if info.st_mtime >= cutoff:
        return False
    if stat.S_ISDIR(info.st_mode):
        for _, directories, files, directory_fd in os.fwalk(name, dir_fd=parent_fd, onerror=raise_walk_error):
            if any(os.stat(entry, dir_fd=directory_fd, follow_symlinks=False).st_mtime >= cutoff for entry in directories + files):
                return False
    return True


def remove(name: str, parent_fd: int, cutoff: float) -> bool:
    quarantine = f".expiring-{uuid.uuid4().hex}"
    os.rename(name, quarantine, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    try:
        if not expired(quarantine, parent_fd, cutoff):
            return False
        info = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            for _, _, _, directory_fd in os.fwalk(quarantine, dir_fd=parent_fd):
                os.fchmod(directory_fd, os.fstat(directory_fd).st_mode | stat.S_IWUSR)
            shutil.rmtree(quarantine, dir_fd=parent_fd)
        else:
            os.unlink(quarantine, dir_fd=parent_fd)
        return True
    finally:
        if quarantine in os.listdir(parent_fd):
            try:
                if stat.S_ISDIR(os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False).st_mode):
                    os.mkdir(name, dir_fd=parent_fd)
                    os.rename(quarantine, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                else:
                    os.link(quarantine, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
                    os.unlink(quarantine, dir_fd=parent_fd)
            except OSError as exc:
                print(f"kept refreshed evidence at {quarantine}; original name {name}: {exc}", file=sys.stderr)


def collect(parent_fd: int, path: Path, cutoff: float, deadline: float, apply: bool, *, repositories: bool = False) -> None:
    names = os.listdir(parent_fd)
    random.shuffle(names)
    for name in names:
        if time.monotonic() >= deadline:
            print("evidence sweep budget exhausted; remaining units kept", file=sys.stderr)
            return
        try:
            if repositories and stat.S_ISDIR(os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode):
                repo_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                try:
                    collect(repo_fd, path / name, cutoff, deadline, apply)
                finally:
                    os.close(repo_fd)
            elif expired(name, parent_fd, cutoff) and (not apply or remove(name, parent_fd, cutoff)):
                print(f"{'removed' if apply else 'would remove'} {path / name}", file=sys.stderr)
        except OSError as exc:
            print(f"kept, could not measure/remove {path / name}: {exc}", file=sys.stderr)


def open_directory(path: Path) -> int:
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError:
        os.close(descriptor)
        raise


def sweep(*, apply: bool = False, budget_seconds: float = float("inf")) -> int:
    import fcntl

    root, _ = artifact_roots()
    if apply and not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError("symlink-resistant deletion is unavailable; evidence kept")
    if not root.exists():
        return 0
    deadline = time.monotonic() + budget_seconds
    cutoff = time.time() - RETENTION_SECONDS
    root_fd = open_directory(root)
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        collect(root_fd, root, cutoff, deadline, apply, repositories=True)
    except BlockingIOError:
        print("evidence sweep already running; kept evidence", file=sys.stderr)
    finally:
        os.close(root_fd)
    return 0


def verify_outside_repository(path: Path) -> None:
    ancestor = path
    while not ancestor.exists():
        ancestor = ancestor.parent
    probe = subprocess.run(
        ["git", "-C", str(ancestor), "rev-parse", "--git-dir"],
        capture_output=True, text=True, env={**git_environment(), "LC_ALL": "C"},
    )
    if probe.returncode == 0:
        raise Refused("Configured paths must be outside repositories and Git metadata.")
    if "not a git repository" not in probe.stderr:
        raise Refused("Cannot verify that the destination is outside repositories; inspect the Git probe failure.")


def create(repo_arg: str, task: str, start: str, root: Path) -> Path:
    repo = Path(repo_arg).resolve(strict=True)
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    if common.name != ".git" or git(repo, "rev-parse", "--is-bare-repository") != "false":
        raise Refused("Use a normal main repository or linked worktree; bare/submodule/separate-git-dir layouts are unsupported.")
    main = common.parent
    for component in (main.name, task):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", component):
            raise Refused(f"Invalid repository/task slug: {component}")
    root = root.resolve()
    if root.is_relative_to(main) or root.is_relative_to(repo):
        raise Refused("The worktree root must be outside the source repository.")
    parent = root / main.name
    if parent.is_symlink():
        raise Refused("The repository worktree parent must not be a symlink.")
    verify_outside_repository(parent)
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
    git(repo, "worktree", "add", "-b", branch, str(destination), commit)
    return destination


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args in (["--help"], ["-h"]):
        print(f"Usage: dev-worktree REPOSITORY TASK-SLUG START-POINT\n       dev-worktree retire [--dry-run] [--offline-only] PATH\n       dev-worktree sweep-evidence [--apply]\nConfig: ~/{DEFAULT_CONFIG_RELATIVE} (override: DEV_WORKTREE_CONFIG)\nSee skills/github/references/worktrees.md for host integration and cleanup.")
        return 0
    try:
        if args[:1] == ["sweep-evidence"]:
            parser = argparse.ArgumentParser(prog="dev-worktree sweep-evidence")
            parser.add_argument("--apply", action="store_true")
            return sweep(apply=parser.parse_args(args[1:]).apply)
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
            os.execve(str(helper), [str(helper), *args], host_environment(args))
        if len(args) != 3 or args[0] == "retire":
            raise Refused("Usage: dev-worktree REPOSITORY TASK-SLUG START-POINT; see --help for retirement.")
        print(create(*args, root))
        return 0
    except (ImportError, OSError, ValueError, subprocess.CalledProcessError) as exc:
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
    os.execve(str(helper), [str(helper), *args], host_environment(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
