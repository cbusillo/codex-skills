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
import os
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLOCKED_NOTICE_AFTER = dt.timedelta(hours=1)
MAX_BLOCKING_PATHS = 20


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError(f"git {args[0]} failed")
    return result.stdout.rstrip("\n")


def checkout_state(root: Path) -> dict:
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root.resolve():
        raise ValueError("catalog path is not a checkout root")
    head = git(root, "rev-parse", "HEAD")
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        return {"state": "blocked", "reason": f"branch is {branch}, expected main", "head": head}
    changes = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if changes:
        entries = iter(changes.split("\0"))
        paths = []
        for entry in entries:
            if not entry:
                continue
            paths.append(entry[3:])
            if "R" in entry[:2] or "C" in entry[:2]:
                paths.append(next(entries))
        return {"state": "blocked", "reason": "checkout has local changes", "head": head,
                "blocking_paths": paths[:MAX_BLOCKING_PATHS],
                "blocking_paths_omitted": max(0, len(paths) - MAX_BLOCKING_PATHS)}
    behind, ahead = map(int, git(root, "rev-list", "--left-right", "--count", "origin/main...HEAD").split())
    if ahead:
        return {"state": "blocked", "reason": "checkout has local commits or has diverged", "head": head}
    if behind:
        return {"state": "stale", "reason": f"{behind} commits behind origin/main", "head": head}
    return {"state": "current", "reason": "", "head": head}


def blocked_since(record: dict, now: dt.datetime) -> dt.datetime | None:
    """Older receipts establish only the last observed block, not its true onset."""
    if record.get("checkout_state", record.get("state")) != "blocked":
        return None
    try:
        stamp = dt.datetime.fromisoformat(record.get("blocked_since") or record["checked_at"])
        return stamp if stamp.tzinfo is not None and stamp <= now else None
    except (ValueError, TypeError, KeyError):
        return None


def blocking_paths_text(state: dict) -> str:
    paths = ", ".join(json.dumps(path, ensure_ascii=False) for path in state.get("blocking_paths", []))
    omitted = state.get("blocking_paths_omitted", 0)
    return paths + (f" (and {omitted} more paths)" if omitted else "")


def blocked_notice(root: Path, state: dict, since: dt.datetime | None, now: dt.datetime) -> str:
    if state.get("state") != "blocked" or since is None or now - since < BLOCKED_NOTICE_AFTER:
        return ""
    hours = int((now - since).total_seconds() // 3600)
    paths = blocking_paths_text(state)
    return (f"WARNING: CATALOG UPDATES BLOCKED; first observed {hours} hours ago\n"
            f"Checkout: {root}\n"
            f"Blocker: {state['reason']}" + (f"\nBlocking paths: {paths}" if paths else "") +
            "\nInspect and preserve the local work, resolve the blocker, then run scripts/catalog_runtime.py --update.")


def update_lock_path(root: Path) -> Path:
    common = Path(git(root, "rev-parse", "--git-common-dir"))
    return (common if common.is_absolute() else root / common) / "catalog-update.lock"


def write_receipt(root: Path, record: dict) -> None:
    receipt = root / ".local" / "catalog-update.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    staged = receipt.with_suffix(".tmp")
    staged.write_text(json.dumps(record) + "\n")
    staged.replace(receipt)


def observe_checkout(root: Path, state: dict, now: dt.datetime) -> dict:
    """Record startup observations without pretending to have checked origin."""
    receipt = root / ".local" / "catalog-update.json"
    with update_lock_path(root).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Atomic updater receipts remain readable; startup never waits or writes.
            return json.loads(receipt.read_text()) if receipt.exists() else {}
        record = json.loads(receipt.read_text()) if receipt.exists() else {}
        if not isinstance(record, dict):
            raise ValueError("invalid update receipt")
        previous = record.copy()
        since = blocked_since(record, now)
        record["checkout_state"] = state["state"]
        if state["state"] == "blocked":
            record["blocked_since"] = (since or now).isoformat()
        else:
            record.pop("blocked_since", None)
        if record != previous:
            write_receipt(root, record)
        return record


def preview_instructions(root: Path, arguments: list[str]) -> dict:
    result = subprocess.run(
        [sys.executable, str(root / "scripts/install-catalog.py"), "--refresh-instructions", "--skip-codex-hooks", *arguments],
        capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL, check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if result.returncode:
        raise ValueError("instruction preview unavailable")
    return json.loads(result.stdout)


def instruction_warning(root: Path, installation: dict) -> str:
    if not all(installation.get(key) for key in ("home", "codex", "claude")):
        return ""
    home = Path(installation["home"])
    codex = Path(installation["codex"])
    claude = Path(installation["claude"])
    if os.environ.get("CLAUDECODE") == "1":
        claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")).expanduser()
    elif os.environ.get("CODEX_HOME"):
        codex = Path(os.environ["CODEX_HOME"]).expanduser()
    arguments = ["--home-dir", str(home), "--codex-dir", str(codex), "--claude-dir", str(claude)]
    changed_home = codex.resolve() != Path(installation["codex"]).resolve() or claude.resolve() != Path(installation["claude"]).resolve()
    recovery = shlex.join(["uv", "run", str(root / "scripts/catalog_runtime.py"), "--update"])
    if changed_home:
        recovery = shlex.join(["uv", "run", str(root / "scripts/install-catalog.py"), "--refresh-instructions", "--write", *arguments])
    destinations = [str(claude / "CLAUDE.md"), str(codex / "AGENTS.md")]
    try:
        preview = preview_instructions(root, arguments)
        outputs = {Path(entry["path"]).resolve(): entry["state"] for entry in preview["outputs"]}
        stale = [path for path in destinations if outputs.get(Path(path).resolve()) != "current"]
        if not stale:
            return ""
        detail = "missing or stale: " + ", ".join(stale)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        detail = "unconfirmed: " + ", ".join(destinations)
    return f"DIRECTION delivery {detail}; run {recovery}; restart the session after refresh."


def status_line(root: Path, *, observe: bool = False) -> str:
    try:
        installation_path = root / ".local" / "catalog-install.json"
        if git(root, "rev-parse", "--git-dir") != git(root, "rev-parse", "--git-common-dir") and not installation_path.is_file():
            return ""
        installation = json.loads(installation_path.read_text()) if installation_path.is_file() else {}
        if not isinstance(installation, dict):
            raise ValueError("invalid installation receipt")
        scheduled = installation.get("scheduled_updater", False) and bool(installation.get("updater_plist")) and Path(installation["updater_plist"]).is_file()
        state = checkout_state(root)
        receipt = root / ".local" / "catalog-update.json"
        now = dt.datetime.now(dt.timezone.utc)
        try:
            if observe:
                try:
                    observe_checkout(root, state, now)
                except OSError:
                    pass  # Readable status remains useful when observation storage is unavailable.
            recorded = json.loads(receipt.read_text()) if receipt.exists() else {}
            if not isinstance(recorded, dict):
                raise ValueError("invalid update receipt")
        except (OSError, ValueError):
            if state["state"] == "current":
                raise
            recorded = {}
        warning = blocked_notice(root, state, blocked_since(recorded, now), now)
        if warning:
            return warning
        if state.get("blocking_paths"):
            state["reason"] += ": " + blocking_paths_text(state)
        if state["state"] == "current":
            stamp = installation.get("scheduled_at")
            if receipt.exists():
                stamp = recorded.get("checked_at", stamp)
                if recorded.get("state") == "error":
                    step = recorded.get("failure_step", "catalog update")
                    recovery = "preview scripts/install-catalog.py --refresh-instructions to diagnose, then run scripts/catalog_runtime.py --update" if step == "instruction refresh" else "run scripts/catalog_runtime.py --update"
                    detail = recorded.get("reason")
                    state = {"state": "blocked", "reason": f"last {step} failed{f': {detail}' if detail else ''}; {recovery}"}

            checked_at = dt.datetime.fromisoformat(stamp) if stamp else None
            if checked_at is not None and checked_at.tzinfo is None:
                raise ValueError("update timestamp has no timezone")
            if scheduled and checked_at and dt.datetime.now(dt.timezone.utc) - checked_at > dt.timedelta(hours=12) and state["state"] == "current":
                state = {"state": "stale", "reason": "scheduled update has not checked origin in over 12 hours"}
            if state["state"] == "current" and receipt.exists() and recorded.get("alert_refresh"):
                state = {"state": "notice", "reason": "catalog checkout is current but alert retirement was skipped; preview scripts/install-catalog.py --refresh-instructions --show-diff, reconcile the reported hook source, then run scripts/catalog_runtime.py --update"}
            if state["state"] in ("current", "notice") and receipt.exists() and recorded.get("instruction_refresh"):
                state = {"state": "notice", "reason": "catalog checkout is current but instruction refresh was skipped; preview scripts/install-catalog.py --refresh-instructions --show-diff, reconcile the reported instruction source, then run scripts/catalog_runtime.py --update"}
                if recorded.get("alert_refresh"):
                    state["reason"] += "; alert retirement was also skipped; reconcile the reported hook source as well"
            if state["state"] == "current":
                warning = instruction_warning(root, installation)
                if warning:
                    state = {"state": "stale", "reason": warning}
        if state["state"] == "current":
            return ""
        return f"Catalog {state['state']}: {state['reason']} ({root})."
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return f"Catalog blocked: could not read checkout/update state ({root})."


def update(root: Path) -> dict:
    """Serialize catalog updates; never switch, reset, stash, clean, or merge divergence."""
    with update_lock_path(root).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "blocked", "reason": "another catalog update is running"}
        failure_step = "catalog checkout check"
        observation = None
        try:
            state = checkout_state(root)
            observation = state["state"]
            if state["state"] != "blocked":
                failure_step = "origin fetch"
                git(root, "fetch", "--quiet", "origin", "main")
                state = checkout_state(root)
                if state["state"] == "stale":
                    # Repeat the clean/branch/ancestor gate immediately before mutation.
                    state = checkout_state(root)
                    if state["state"] == "stale":
                        failure_step = "catalog fast-forward"
                        git(root, "merge", "--ff-only", "--no-edit", "origin/main")
                        state = checkout_state(root)
                if state["state"] == "current" and (root / ".local" / "catalog-install.json").is_file():
                    failure_step = "instruction refresh"
                    # Refresh installed global instructions through the same installer;
                    # Existing catalog hook bindings receive alert updates; removed
                    # bindings and unrelated hooks remain as the user configured them.
                    installation = json.loads((root / ".local" / "catalog-install.json").read_text())
                    if not isinstance(installation, dict):
                        raise ValueError("invalid installation receipt")
                    result = subprocess.run([sys.executable, str(root / "scripts" / "install-catalog.py"), "--write", "--refresh-instructions",
                                             "--home-dir", installation["home"], "--codex-dir", installation["codex"], "--claude-dir", installation["claude"]],
                                            capture_output=True, text=True, timeout=60)
                    if result.returncode:
                        details = result.stderr.strip().splitlines()
                        raise ValueError(details[-1] if details else "catalog is current but instruction refresh failed; preview scripts/install-catalog.py --refresh-instructions to diagnose")
                    refreshed = json.loads(result.stdout)
                    skipped = [entry for entry in refreshed.get("outputs", []) if entry.get("state") == "skipped"]
                    if any(Path(entry["path"]).name in ("CLAUDE.md", "AGENTS.md") for entry in skipped):
                        state["instruction_refresh"] = "skipped; preview scripts/install-catalog.py --refresh-instructions --show-diff to diagnose"
                    if any(Path(entry["path"]).name not in ("CLAUDE.md", "AGENTS.md") for entry in skipped):
                        state["alert_refresh"] = "skipped; preview scripts/install-catalog.py --refresh-instructions to diagnose"
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
            state = {"state": "error", "reason": str(error), "failure_step": failure_step}
        now = dt.datetime.now(dt.timezone.utc)
        state["checked_at"] = now.isoformat()
        state["checkout_state"] = state["state"]
        receipt = root / ".local" / "catalog-update.json"
        try:
            previous = json.loads(receipt.read_text())
        except (OSError, ValueError):
            previous = {}
        since = blocked_since(previous, now) if isinstance(previous, dict) else None
        if state["state"] == "error" and observation is None and since:
            # A failed observation proves no recovery; keep the last known block.
            state["checkout_state"] = "blocked"
            state["blocked_since"] = since.isoformat()
        if state["state"] == "blocked":
            since = since or now
            state["blocked_since"] = since.isoformat()
            warning = blocked_notice(root, state, since, now)
            if warning:
                state["warning"] = warning
        write_receipt(root, state)
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
            if state.get("warning"):
                print(state["warning"], file=sys.stderr)
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
