#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Session start prints the shared loop only for adopted direction repositories."""

from __future__ import annotations

import datetime as dt
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOOK = Path(__file__).resolve().parent / "direction_check_hook.py"
sys.path.insert(0, str(HOOK.parent))

import direction_check_hook as hook  # noqa: E402

NOW = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.timezone.utc)
MARKER = Path("/x/direction-last-check.json")


def ago(**kwargs: float) -> dt.datetime:
    return NOW - dt.timedelta(**kwargs)


def marker(turn: dt.datetime | None = None, **audits: dt.datetime) -> dict[str, object]:
    return {"turn": turn, "audits": {repo.replace("__", "/"): stamp for repo, stamp in audits.items()}}


class ReminderTests(unittest.TestCase):
    def test_current_checks_print_nothing(self) -> None:
        m = marker(ago(hours=3), owner__repo=ago(days=2))
        self.assertEqual(hook.reminder(m, NOW, "owner/repo", MARKER), "")

    def test_a_fresh_audit_anywhere_counts_as_a_turn(self) -> None:
        m = marker(ago(days=3), owner__other=ago(hours=2))
        self.assertEqual(hook.reminder(m, NOW, None, MARKER), "")

    def test_an_audit_in_one_repo_does_not_silence_another(self) -> None:
        m = marker(ago(hours=1), owner__a=ago(hours=1))
        text = hook.reminder(m, NOW, "owner/b", MARKER)
        self.assertIn("owner/b has a DIRECTION.md but no recorded weekly audit", text)
        self.assertNotIn("direction turn", text)

    def test_stale_turn_and_stale_audit_are_named_separately(self) -> None:
        m = marker(ago(days=2), owner__repo=ago(days=9))
        text = hook.reminder(m, NOW, "owner/repo", MARKER)
        self.assertIn("last direction turn was 2 days ago", text)
        self.assertIn("last weekly audit of owner/repo was 9 days ago", text)
        self.assertIn("Do not run the marking helpers yourself", text)
        self.assertIn(str(MARKER), text)

    def test_unadopted_repo_gets_only_the_turn_reminder(self) -> None:
        self.assertEqual(hook.reminder(marker(ago(hours=2)), NOW, None, MARKER), "")
        text = hook.reminder(marker(ago(days=2)), NOW, None, MARKER)
        self.assertIn("direction turn", text)
        self.assertNotIn("last weekly audit of", text)
        self.assertNotIn("has a DIRECTION.md", text)

    def test_missing_marker_asks_for_a_turn(self) -> None:
        text = hook.reminder({"turn": None, "audits": {}}, NOW, None, MARKER)
        self.assertIn("no direction turn has been recorded", text)

    def test_marker_path_is_shared_across_hosts(self) -> None:
        # Codex sets CODEX_HOME for its hooks; Claude Code sets neither. Both must read one file.
        self.assertEqual(hook.marker_path({"CODEX_HOME": "/h/.codex", "HOME": "/h"}), Path("/h/.code/direction-last-check.json"))
        self.assertEqual(hook.marker_path({"CODE_HOME": "/x", "HOME": "/h"}), Path("/h/.code/direction-last-check.json"))
        self.assertEqual(hook.marker_path({"HOME": "/h"}), Path("/h/.code/direction-last-check.json"))
        self.assertEqual(hook.marker_path({"DIRECTION_MARKER": "/shared/m.json", "CODEX_HOME": "/y", "HOME": "/h"}), Path("/shared/m.json"))

    def test_malformed_and_naive_stamps_do_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            self.assertEqual(hook.read_marker(path), {"turn": None, "audits": {}})
            path.write_text("not json")
            self.assertEqual(hook.read_marker(path), {"turn": None, "audits": {}})
            path.write_text(json.dumps({"turn": "yesterday", "audits": {"o/r": "2026-09-20T10:00:00", "bad": 5}}))
            read = hook.read_marker(path)
            self.assertIsNone(read["turn"])
            audits = read["audits"]
            assert isinstance(audits, dict)
            self.assertEqual(list(audits), ["o/r"])
            text = hook.reminder(read, NOW, "o/r", path)
            self.assertNotIn("last weekly audit of o/r", text, "a naive two-day-old audit stamp is read as UTC, not a crash")
            self.assertIn("last direction turn was 2 days ago", text, "the naive audit stamp stands in for the unparseable turn stamp")

    def test_adopted_repo_needs_a_direction_file_and_an_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            nested = root / "a"
            nested.mkdir()
            self.assertIsNone(hook.direction_root(nested))
            (root / "DIRECTION.md").write_text("# Direction\n")
            self.assertEqual(hook.direction_root(nested), root.resolve())
            self.assertIsNone(hook.adopted_repo(root.resolve()), "no origin, no repo key")
            subprocess.run(["git", "-C", str(root), "remote", "add", "origin", "git@github.com:owner/repo.git"], check=True)
            self.assertEqual(hook.adopted_repo(root.resolve()), "owner/repo")

    def test_loop_prints_from_reference_only_in_direction_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            nested = root / "nested"
            nested.mkdir()
            marker_path = root / hook.MARKER_NAME
            marker_path.write_text(json.dumps({"turn": dt.datetime.now(dt.timezone.utc).isoformat(), "audits": {}}))
            env = {"DIRECTION_MARKER": str(marker_path), "PATH": "/usr/bin:/bin"}
            def run(cwd: Path) -> subprocess.CompletedProcess[str]:
                return subprocess.run([sys.executable, str(HOOK)], cwd=cwd, env=env, capture_output=True, text=True, check=True)

            self.assertEqual(run(nested).stdout, "")
            (root / "DIRECTION.md").write_text("# Direction\n")
            expected = hook.LOOP_PATH.read_text().strip()
            self.assertEqual(run(nested).stdout.strip(), expected)
            self.assertEqual(run(root).stdout.strip(), expected)

    def test_missing_loop_reference_keeps_overdue_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "DIRECTION.md").write_text("# Direction\n")
            output = io.StringIO()
            with mock.patch.object(hook, "LOOP_PATH", root / "missing.md"), mock.patch.object(hook, "marker_path", return_value=root / "missing-marker.json"), mock.patch("pathlib.Path.cwd", return_value=root), contextlib.redirect_stdout(output):
                self.assertEqual(hook.main(), 0)
            self.assertIn("Direction check overdue", output.getvalue())

    def test_hook_process_never_blocks_and_ignores_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"DIRECTION_MARKER": str(Path(tmp) / hook.MARKER_NAME), "PATH": "/usr/bin:/bin"}
            proc = subprocess.run([sys.executable, str(HOOK)], input='{"hook_event_name":"SessionStart"}', capture_output=True, text=True, env=env, cwd=tmp)
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Direction check overdue", proc.stdout)
            (Path(tmp) / hook.MARKER_NAME).write_text(json.dumps({"turn": dt.datetime.now(dt.timezone.utc).isoformat(), "audits": {}}))
            proc = subprocess.run([sys.executable, str(HOOK)], stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, cwd=tmp)
            self.assertEqual((proc.returncode, proc.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
