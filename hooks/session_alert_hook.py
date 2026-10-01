#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Append advisory Stop/Interrupt notices; never control the turn or read transcripts."""
from __future__ import annotations

import datetime as dt
import json
import os
import stat
import sys
from pathlib import Path
from typing import Mapping


def events_path(env: Mapping[str, str]) -> Path:
    # Host-specific CODEX_HOME/CLAUDE_CONFIG_DIR would split the shared stream.
    shared = Path(env.get("CODE_HOME") or Path.home() / ".code").expanduser()
    if not shared.is_absolute():
        raise ValueError("CODE_HOME must be absolute")
    return shared / "session-events.jsonl"


def notice(payload: object, env: Mapping[str, str]) -> dict | None:
    if env.get("SESSION_ALERTS_DISABLED") == "1":
        return None
    if not isinstance(payload, dict):
        return None
    event = payload.get("hook_event_name")
    session = payload.get("session_id")
    harness = env.get("CODEX_SKILLS_HARNESS", "claude")
    if event not in ("Stop", "Interrupt") or harness not in ("codex", "claude"):
        return None
    if event == "Interrupt" and harness != "codex":
        return None
    if not isinstance(session, str) or not session or len(session) > 256:
        return None
    turn = payload.get("turn_id")
    active = payload.get("stop_hook_active")
    return {
        "schema_version": 1,
        "harness": harness,
        "session_id": session,
        "turn_id": turn if isinstance(turn, str) and len(turn) <= 256 else None,
        "time": dt.datetime.now(dt.timezone.utc).isoformat(),
        "event": event,
        "advisory": True,
        "clean": None,  # Neither hook proves final completion or success.
        "stop_hook_active": active if event == "Stop" and isinstance(active, bool) else None,
    }


def append_notice(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (json.dumps(record, separators=(",", ":")) + "\n").encode()
    # O_APPEND plus one write keeps concurrent local hook records intact. Do not
    # follow a redirected file, or wait on a FIFO when the destination is unsafe.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("events destination is not a regular file")
        if os.write(fd, data) != len(data):
            raise OSError("short events write")
    finally:
        os.close(fd)


def main() -> int:
    try:
        record = notice(json.load(sys.stdin), os.environ)
        if record is not None:
            append_notice(events_path(os.environ), record)
    except (OSError, ValueError):
        # An unavailable alert stream must not block or restart the session.
        print("Session alert unavailable; supervisor must check session state.", file=sys.stderr)
    # Empty JSON is accepted by both hosts and has no decision fields.
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
