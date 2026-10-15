#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Create a linked task worktree, or route to an explicitly configured host helper."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys


class Refused(ValueError):
    """The request cannot safely create a new task worktree."""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def configuration() -> tuple[Path, Path | None]:
    explicit = os.environ.get("DEV_WORKTREE_CONFIG")
    config = Path(explicit).expanduser() if explicit else Path.home() / ".config/codex-skills/worktrees.json"
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
    ancestor = root
    while not ancestor.exists():
        ancestor = ancestor.parent
    probe = subprocess.run(["git", "-C", str(ancestor), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    if probe.returncode == 0 and probe.stdout.strip() == "true":
        raise Refused("The worktree root must be outside repositories.")
    parent = root / main.name
    if parent.is_symlink():
        raise Refused("The repository worktree parent must not be a symlink.")
    destination = parent / task
    if os.path.lexists(destination):
        raise Refused(f"Destination already exists: {destination}")
    commit = git(repo, "rev-parse", "--verify", "--end-of-options", f"{start}^{{commit}}")
    branch = f"work/{task}"
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
        print("Usage: dev-worktree REPOSITORY TASK-SLUG START-POINT\nConfig: ~/.config/codex-skills/worktrees.json (override: DEV_WORKTREE_CONFIG)\nSee skills/github/references/worktrees.md for host integration and cleanup.")
        return 0
    try:
        root, helper = configuration()
        if helper:
            if helper.resolve() in {Path(__file__).resolve(), Path(__file__).with_name("dev-worktree").resolve()}:
                raise Refused("hostHelper must not refer to the catalog helper itself.")
            # The host owns volume identity, locks and retirement; do not mkdir or fall back.
            return subprocess.run([str(helper), *args], check=False).returncode
        if len(args) != 3 or args[0] == "retire":
            raise Refused("Usage: dev-worktree REPOSITORY TASK-SLUG START-POINT. For cleanup follow work-closeout; this portable helper only creates worktrees.")
        print(create(*args, root))
        return 0
    except (Refused, OSError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        print(f"dev-worktree: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
