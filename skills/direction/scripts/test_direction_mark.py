#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""The marker writer records turns and audits the session-start hook can read."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).with_name("direction_mark.py")
HOOK = Path(__file__).resolve().parents[3] / "hooks" / "direction_check_hook.py"
NOW = dt.datetime(2026, 9, 22, 12, 0, 30, 123456, tzinfo=dt.timezone.utc)


def load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_turn_updates_turn_only_and_audit_updates_both() -> None:
    mark = load(SCRIPT, "direction_mark_under_test")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "nested" / "direction-last-check.json"
        first = mark.mark(path, "turn", NOW)
        assert first == {"turn": "2026-09-22T12:00:30Z"}, first
        later = mark.mark(path, "audit", NOW + dt.timedelta(days=1))
        assert later == {"turn": "2026-09-23T12:00:30Z", "audit": "2026-09-23T12:00:30Z"}, later
        again = mark.mark(path, "turn", NOW + dt.timedelta(days=2))
        assert again["audit"] == "2026-09-23T12:00:30Z", "a turn must not touch the audit stamp"
        assert json.loads(path.read_text()) == again


def test_hook_reads_what_the_marker_writes() -> None:
    mark = load(SCRIPT, "direction_mark_under_test2")
    hook = load(HOOK, "direction_check_hook_under_test")
    assert hook.MARKER_NAME == mark.MARKER_NAME
    env = {"CODE_HOME": "/x"}
    assert hook.marker_path(env) == mark.marker_path(env)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / mark.MARKER_NAME
        mark.mark(path, "audit", NOW)
        stamps = hook.read_marker(path)
        assert hook.reminder(stamps, NOW + dt.timedelta(hours=1)) == ""
        assert "last weekly audit was 8 days ago" in hook.reminder(stamps, NOW + dt.timedelta(days=8))


def test_malformed_marker_is_replaced_not_crashed() -> None:
    mark = load(SCRIPT, "direction_mark_under_test3")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "m.json"
        path.write_text("[1, 2]")
        assert mark.mark(path, "turn", NOW) == {"turn": "2026-09-22T12:00:30Z"}


def main() -> int:
    for name, test in list(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
            print(f"ok {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
