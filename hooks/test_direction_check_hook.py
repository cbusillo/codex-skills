#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""The session-start reminder nags only while a direction check is overdue."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "direction_check_hook.py"
sys.path.insert(0, str(HOOK.parent))

import direction_check_hook as hook  # noqa: E402

NOW = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.timezone.utc)


def stamp(delta: dt.timedelta) -> dt.datetime:
    return NOW - delta


class ReminderTests(unittest.TestCase):
    def test_current_checks_print_nothing(self) -> None:
        stamps = {"turn": stamp(dt.timedelta(hours=3)), "audit": stamp(dt.timedelta(days=2))}
        self.assertEqual(hook.reminder(stamps, NOW), "")

    def test_a_fresh_audit_counts_as_a_turn(self) -> None:
        stamps = {"turn": stamp(dt.timedelta(days=3)), "audit": stamp(dt.timedelta(hours=2))}
        self.assertEqual(hook.reminder(stamps, NOW), "")

    def test_stale_turn_and_stale_audit_are_named_separately(self) -> None:
        stamps = {"turn": stamp(dt.timedelta(days=2)), "audit": stamp(dt.timedelta(days=9))}
        text = hook.reminder(stamps, NOW)
        self.assertIn("last direction turn was 2 days ago", text)
        self.assertIn("last weekly audit was 9 days ago", text)
        self.assertIn("run the `direction` skill", text)

    def test_missing_marker_asks_for_both(self) -> None:
        text = hook.reminder({}, NOW)
        self.assertIn("no direction turn has been recorded", text)
        self.assertIn("no weekly direction audit has been recorded", text)

    def test_marker_path_follows_the_catalog_home_order(self) -> None:
        self.assertEqual(hook.marker_path({"CODE_HOME": "/x", "CODEX_HOME": "/y", "HOME": "/h"}), Path("/x/direction-last-check.json"))
        self.assertEqual(hook.marker_path({"CODEX_HOME": "/y", "HOME": "/h"}), Path("/y/direction-last-check.json"))
        self.assertEqual(hook.marker_path({"HOME": "/h"}), Path("/h/.code/direction-last-check.json"))

    def test_unreadable_or_malformed_marker_is_treated_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            self.assertEqual(hook.read_marker(path), {})
            path.write_text("not json")
            self.assertEqual(hook.read_marker(path), {})
            path.write_text(json.dumps({"turn": "yesterday", "audit": "2026-09-20T10:00:00Z"}))
            self.assertEqual(list(hook.read_marker(path)), ["audit"])

    def test_hook_process_never_blocks_and_reads_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CODE_HOME": tmp, "PATH": "/usr/bin:/bin"}
            proc = subprocess.run([sys.executable, str(HOOK)], input='{"hook_event_name":"SessionStart"}', capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Direction check overdue", proc.stdout)
            (Path(tmp) / hook.MARKER_NAME).write_text(json.dumps({"turn": dt.datetime.now(dt.timezone.utc).isoformat(), "audit": dt.datetime.now(dt.timezone.utc).isoformat()}))
            proc = subprocess.run([sys.executable, str(HOOK)], input="{}", capture_output=True, text=True, env=env)
            self.assertEqual((proc.returncode, proc.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
