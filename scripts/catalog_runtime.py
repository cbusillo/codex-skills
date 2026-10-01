#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read catalog state and optionally fetch and fast-forward a clean main checkout."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError(f"git {args[0]} failed")
    return result.stdout.strip()


def checkout_state(root: Path) -> dict[str, str]:
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root.resolve():
        raise ValueError("catalog path is not a checkout root")
    head = git(root, "rev-parse", "HEAD")
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        return {"state": "blocked", "reason": f"branch is {branch}, expected main", "head": head}
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        return {"state": "blocked", "reason": "checkout has local changes", "head": head}
    behind, ahead = map(int, git(root, "rev-list", "--left-right", "--count", "origin/main...HEAD").split())
    if ahead:
        return {"state": "blocked", "reason": "checkout has local commits or has diverged", "head": head}
    if behind:
        return {"state": "stale", "reason": f"{behind} commits behind origin/main", "head": head}
    return {"state": "current", "reason": "", "head": head}


def status_line(root: Path) -> str:
    """No network or writes at session start; task worktrees are not runtime installs."""
    try:
        if git(root, "rev-parse", "--git-dir") != git(root, "rev-parse", "--git-common-dir"):
            return ""
        state = checkout_state(root)
        if state["state"] == "current":
            receipt = root / ".local" / "catalog-update.json"
            if receipt.exists():
                recorded = json.loads(receipt.read_text())
                stamp = dt.datetime.fromisoformat(recorded["checked_at"])
                if recorded.get("state") == "error":
                    state = {"state": "blocked", "reason": "last scheduled update failed; run scripts/catalog_runtime.py --update"}
                elif dt.datetime.now(dt.timezone.utc) - stamp > dt.timedelta(hours=12):
                    state = {"state": "stale", "reason": "scheduled update has not checked origin in over 12 hours"}
        if state["state"] == "current":
            return ""
        return f"Catalog {state['state']}: {state['reason']} ({root})."
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return f"Catalog blocked: could not read checkout/update state ({root})."


def update(root: Path) -> dict[str, str]:
    """Serialize catalog updates; never switch, reset, stash, clean, or merge divergence."""
    lock_path = Path(git(root, "rev-parse", "--git-common-dir"))
    if not lock_path.is_absolute():
        lock_path = root / lock_path
    with (lock_path / "catalog-update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "blocked", "reason": "another catalog update is running"}
        try:
            state = checkout_state(root)
            if state["state"] != "blocked":
                git(root, "fetch", "--quiet", "origin", "main")
                state = checkout_state(root)
                if state["state"] == "stale":
                    # Repeat the clean/branch/ancestor gate immediately before mutation.
                    state = checkout_state(root)
                    if state["state"] == "stale":
                        git(root, "merge", "--ff-only", "--no-edit", "origin/main")
                        state = checkout_state(root)
                if state["state"] == "current" and (root / ".local" / "catalog-global-source.md").is_file():
                    # Refresh installed global instructions through the same installer;
                    # stable hook entries retain their trust and unrelated settings.
                    installation = json.loads((root / ".local" / "catalog-install.json").read_text())
                    result = subprocess.run([sys.executable, str(root / "scripts" / "install-catalog.py"), "--write",
                                             "--home-dir", installation["home"], "--codex-dir", installation["codex"], "--claude-dir", installation["claude"]],
                                            capture_output=True, text=True, timeout=60)
                    if result.returncode:
                        raise ValueError("catalog pulled but install refresh failed; rerun scripts/install-catalog.py --write")
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            state = {"state": "error", "reason": str(error)}
        state["checked_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        receipt = root / ".local" / "catalog-update.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        staged = receipt.with_suffix(".tmp")
        staged.write_text(json.dumps(state) + "\n")
        staged.replace(receipt)
        return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT)
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    try:
        if args.update:
            state = update(args.catalog.resolve())
            print(json.dumps(state))
            return 0 if state["state"] == "current" else 1
        line = status_line(args.catalog.resolve())
        if line:
            print(line)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Catalog update failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
