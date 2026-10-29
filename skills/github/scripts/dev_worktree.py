#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Create a linked task worktree, or route to an explicitly configured host helper."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
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


def configuration() -> tuple[Path, Path | None]:
    explicit = os.environ.get("DEV_WORKTREE_CONFIG")
    config = Path(explicit).expanduser() if explicit else Path.home() / DEFAULT_CONFIG_RELATIVE
    settings = json.loads(config.read_text()) if explicit or os.path.lexists(config) else {}
    if not isinstance(settings, dict) or set(settings) - {"root", "hostHelper"}:
        raise Refused("Expected a JSON object with root and optional hostHelper.")
    root = settings.get("root", str(Path.home() / "Developer/worktrees"))
    helper = settings.get("hostHelper")
    if not isinstance(root, str):
        raise Refused("root must be an absolute path.")
    for value in (root, helper):
        if value is not None and (not isinstance(value, str) or not value or not Path(value).expanduser().is_absolute()):
            raise Refused("root and hostHelper must be absolute paths (a leading ~ is allowed).")
    if "hostHelper" in settings and (helper is None or "root" not in settings):
        raise Refused("A hostHelper requires an explicit root and helper path.")
    return Path(root).expanduser(), Path(helper).expanduser() if helper else None


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
        print(f"Usage: dev-worktree REPOSITORY TASK-SLUG START-POINT\nConfig: ~/{DEFAULT_CONFIG_RELATIVE} (override: DEV_WORKTREE_CONFIG)\nA configured hostHelper also receives: retire [--dry-run] PATH\nSee skills/github/references/worktrees.md for host integration and cleanup.")
        return 0
    try:
        root, helper = configuration()
        if helper:
            if delegation_active(args):
                raise Refused("Host helper delegated back to the catalog; configure the actual host executable.")
            if helper.resolve() in {Path(__file__).resolve(), Path(__file__).with_name("dev-worktree").resolve()}:
                raise Refused("hostHelper must not refer to the catalog helper itself.")
        if len(args) == 3 and args[0] != "retire":
            refresh_primary(Path(args[0]))
            retire_finished(Path(args[0]))
        if helper:
            # The host owns volume identity, locks and retirement; do not mkdir or fall back.
            # Preserve the host's signal traps, including relocking during interrupted retirement.
            os.environ["DEV_WORKTREE_DELEGATED"] = f"{os.getpid()}:{json.dumps(args)}"
            os.execv(str(helper), [str(helper), *args])
        if len(args) != 3 or args[0] == "retire":
            raise Refused("Usage: dev-worktree REPOSITORY TASK-SLUG START-POINT. For cleanup follow work-closeout; this portable helper only creates worktrees.")
        print(create(*args, root))
        return 0
    except (Refused, OSError, UnicodeError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        print(f"dev-worktree: {detail}", file=sys.stderr)
        return 1


def checkout_rules() -> ModuleType:
    spec = importlib.util.spec_from_file_location("checkout_rules", Path(__file__).with_name("reconcile-runtime-checkout.py"))
    if spec is None or spec.loader is None:
        raise Refused("Checkout rules unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require_clean_default(rules: ModuleType, primary: Path, branch: str) -> None:
    blockers = rules.runtime_blockers(primary, branch)
    operations = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "rebase-merge",
                  "rebase-apply", "sequencer", "BISECT_LOG", "BISECT_START")
    if blockers or any(Path(rules.git_text(primary, "rev-parse", "--path-format=absolute", "--git-path", name)).exists()
                       for name in operations):
        raise Refused("Primary checkout is dirty, on another branch, or has an active Git operation")


def refresh_primary(checkout: Path) -> None:
    rules = checkout_rules()
    try:
        common = rules.git_common_dir(checkout)
        if common.name != ".git":
            raise Refused("Unsupported primary checkout layout")
        primary = common.parent
        for binding, _, authoritative in rules.runtime_skills_paths():
            status, reason, bound_root = rules.inspect_binding(binding, common)
            if status == "failed" and authoritative:
                raise Refused(reason)
            if bound_root == primary:
                helper = primary / rules.HELPER_IN_CATALOG
                expected_repo = rules.repository_from_remote_url(rules.git_text(checkout, "config", "--get", "remote.origin.url"))
                result = subprocess.run([sys.executable, str(helper), "--merged-worktree", str(primary), "--repo", expected_repo],
                                        capture_output=True, text=True, env=git_environment())
                print(result.stdout.strip() or result.stderr.strip(), file=sys.stderr)
                if result.returncode:
                    raise Refused("Runtime reconciler did not refresh the primary checkout")
                return
        with rules.reconciliation_lock(common):
            branch = rules.resolve_default_branch(checkout)
            require_clean_default(rules, primary, branch)
            head = rules.git_text(primary, "rev-parse", "HEAD")
            upstream = rules.git_text(primary, "for-each-ref", "--format=%(upstream:remotename)%00%(upstream:remoteref)%00%(upstream)",
                                      f"refs/heads/{branch}").split("\0")
            if len(upstream) != 3 or not all(upstream) or upstream[0] == "." or upstream[1] != f"refs/heads/{branch}":
                raise Refused("Primary default branch needs an unambiguous configured remote upstream")
            remote, remote_ref, tracking_ref = upstream
            origin_url = rules.git_text(checkout, "config", "--get", "remote.origin.url")
            if rules.git_has_url_rewrite(primary) or rules.git_text(primary, "config", "--get", f"remote.{remote}.url") != origin_url:
                raise Refused("Primary upstream does not match the source repository")
            rules.run_git(primary, "fetch", "--no-tags", "--no-recurse-submodules", remote, f"{remote_ref}:{tracking_ref}")
            tip = rules.git_text(primary, "rev-parse", f"{tracking_ref}^{{commit}}")
            if not rules.git_is_ancestor(primary, head, tip):
                raise Refused("Primary checkout is ahead or diverged")
            require_clean_default(rules, primary, branch)
            if rules.git_text(primary, "rev-parse", "HEAD") != head or not rules.git_is_ancestor(primary, head, tip):
                raise Refused("Primary checkout changed before fast-forward")
            if head != tip:
                rules.run_git(primary, "merge", "--ff-only", "--no-autostash", "--no-overwrite-ignore", tip)
            require_clean_default(rules, primary, branch)
            if rules.git_text(primary, "rev-parse", "HEAD") != tip:
                raise Refused("Primary checkout did not reach the pinned upstream commit")
            print(f"dev-worktree: primary default checkout current: {primary} ({tip})", file=sys.stderr)
    except (Refused, OSError, ValueError, rules.GitCommandError, rules.ReconciliationBusy) as exc:
        print(f"dev-worktree: Local default checkout remains stale; fast-forward it before default-branch work or audits. {exc}",
              file=sys.stderr)


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


if __name__ == "__main__":
    sys.exit(main())
