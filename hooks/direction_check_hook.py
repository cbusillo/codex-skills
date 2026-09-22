#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Session-start reminder that a direction turn or audit is overdue.

The `direction` skill records the end of every daily turn in a small local
marker, and the audit script records each weekly audit there per repository.
At session start this hook reads the marker and prints one line when the last
turn is older than a day, or when the repository the session opened in has a
`DIRECTION.md` and its last audit is older than a week. It prints nothing when
the checks are current, never reads stdin, never blocks, and exits 0 whatever
it finds, so the same script serves Claude Code's SessionStart hook and a
Codex session-start hook.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

MARKER_NAME = "direction-last-check.json"
TURN_STALE = dt.timedelta(hours=24)
AUDIT_STALE = dt.timedelta(days=7)


def marker_path(env: Mapping[str, str] | None = None) -> Path:
    """One marker for every host: DIRECTION_MARKER when set, else ~/.code/direction-last-check.json.

    Host home variables are deliberately not consulted. Codex sets CODEX_HOME for
    its hooks and Claude Code does not, so a lookup by those would give each host
    its own file and a check done in one would never clear the other's reminder.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    explicit = source.get("DIRECTION_MARKER")
    if explicit:
        return Path(explicit).expanduser()
    return Path(source.get("HOME", "~")).expanduser() / ".code" / MARKER_NAME


def parse_stamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def read_marker(path: Path) -> dict[str, object]:
    """The marker as {"turn": datetime | None, "audits": {repo: datetime}}."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        return {"turn": None, "audits": {}}
    audits_raw = raw.get("audits")
    audits: dict[str, dt.datetime] = {}
    if isinstance(audits_raw, dict):
        for repo, value in audits_raw.items():
            stamp = parse_stamp(value)
            if isinstance(repo, str) and stamp:
                audits[repo] = stamp
    return {"turn": parse_stamp(raw.get("turn")), "audits": audits}


def adopted_repo(cwd: Path) -> str | None:
    """OWNER/REPO for the checkout at cwd when it has a root DIRECTION.md, else None."""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, text=True, capture_output=True, timeout=5)
        if top.returncode != 0 or not top.stdout.strip():
            return None
        root = Path(top.stdout.strip())
        if not (root / "DIRECTION.md").is_file():
            return None
        remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root, text=True, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"github\.com[:/]([^/]+)/([^/.]+)(?:\.git)?$", remote.stdout.strip()) if remote.returncode == 0 else None
    return f"{match.group(1)}/{match.group(2)}" if match else None


def reminder(marker: dict[str, object], now: dt.datetime, repo: str | None, path: Path) -> str:
    """One line when something is overdue, empty when the checks are current."""
    overdue: list[str] = []
    turn = marker.get("turn")
    audits = marker.get("audits")
    audits = audits if isinstance(audits, dict) else {}
    latest = max([stamp for stamp in [turn, *audits.values()] if isinstance(stamp, dt.datetime)], default=None)
    if latest is None:
        overdue.append("no direction turn has been recorded on this machine")
    elif now - latest > TURN_STALE:
        overdue.append(f"the last direction turn was {(now - latest).days} days ago")
    if repo:
        audit = audits.get(repo)
        if not isinstance(audit, dt.datetime):
            overdue.append(f"{repo} has a DIRECTION.md but no recorded weekly audit")
        elif now - audit > AUDIT_STALE:
            overdue.append(f"the last weekly audit of {repo} was {(now - audit).days} days ago")
    if not overdue:
        return ""
    return (
        "Direction check overdue: " + "; ".join(overdue) + ". "
        "Tell the owner once at the start of the session to open Claude Code and run the `direction` skill "
        "(a daily turn, or the weekly audit of this repository when that is what is overdue), then continue with the task. "
        f"Do not run the marking helpers yourself; the marker is {path}."
    )


def main() -> int:
    try:
        path = marker_path()
        text = reminder(read_marker(path), dt.datetime.now(dt.timezone.utc), adopted_repo(Path.cwd()), path)
        if text:
            print(text)
    except Exception:  # noqa: BLE001 - a reminder must never break a session start
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
