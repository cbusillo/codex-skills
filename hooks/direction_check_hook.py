#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Session-start reminder that a direction turn or audit is overdue.

The `direction` skill records the end of every daily turn and weekly audit in
a small local marker. At session start this hook reads the marker and, when a
turn is older than a day or an audit older than a week, prints one line asking
the owner to run the direction skill in Claude Code. It prints nothing when
the checks are current, never blocks, and exits 0 whatever it finds.

Contract: hook input JSON on stdin is read and ignored, so the same script
serves Claude Code's SessionStart hook and a Codex session-start hook.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

MARKER_NAME = "direction-last-check.json"
TURN_STALE = dt.timedelta(hours=24)
AUDIT_STALE = dt.timedelta(days=7)


def marker_path(env: Mapping[str, str] | None = None) -> Path:
    """Where the marker lives, mirroring how the catalog finds its home."""
    source: Mapping[str, str] = os.environ if env is None else env
    for name in ("CODE_HOME", "CODEX_HOME"):
        value = source.get(name)
        if value:
            return Path(value) / MARKER_NAME
    return Path(source.get("HOME", "~")).expanduser() / ".code" / MARKER_NAME


def read_marker(path: Path) -> dict[str, dt.datetime]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    stamps: dict[str, dt.datetime] = {}
    for kind in ("turn", "audit"):
        value = raw.get(kind) if isinstance(raw, dict) else None
        if isinstance(value, str):
            try:
                stamps[kind] = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
    return stamps


def reminder(stamps: dict[str, dt.datetime], now: dt.datetime) -> str:
    """One line when something is overdue, empty when the checks are current."""
    overdue: list[str] = []
    turn = stamps.get("turn")
    audit = stamps.get("audit")
    latest = max((stamp for stamp in (turn, audit) if stamp), default=None)
    if latest is None:
        overdue.append("no direction turn has been recorded on this machine")
    elif now - latest > TURN_STALE:
        overdue.append(f"the last direction turn was {(now - latest).days} days ago")
    if audit is None:
        overdue.append("no weekly direction audit has been recorded")
    elif now - audit > AUDIT_STALE:
        overdue.append(f"the last weekly audit was {(now - audit).days} days ago")
    if not overdue:
        return ""
    return (
        "Direction check overdue: " + "; ".join(overdue) + ". "
        "Open Claude Code and run the `direction` skill (a daily turn, or the weekly audit when that is what is overdue). "
        "Say this to the owner once at the start of the session, then continue with the task."
    )


def main() -> int:
    try:
        sys.stdin.read()
    except OSError:
        pass
    text = reminder(read_marker(marker_path()), dt.datetime.now(dt.timezone.utc))
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
