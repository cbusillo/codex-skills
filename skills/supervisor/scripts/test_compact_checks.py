#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import argparse
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import catalog_landing
import check_changes
import check_operation
import goal_board
import oq
import session_closeout


class ChangesTests(unittest.TestCase):
    def test_incomplete_discovery_cannot_remove_records_and_recovers_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            rows = [{"url": "record", "status": "active"}]
            self.assertEqual(check_changes.changes(rows, state, ["source"], key="url", complete=True), rows)
            self.assertEqual(check_changes.changes(rows, state, ["source"], key="url", complete=True), [])
            self.assertEqual(check_changes.changes([], state, ["source"], key="url", complete=False), [])
            self.assertTrue(check_changes.changes([], state, ["source"], key="url", complete=True)[0]["removed"])
            self.assertNotIn("active", state.read_text())

    def test_board_and_questions_emit_changes_without_reprinting_private_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            board = {
                "repository": "owner/direction",
                "checked_at": "first",
                "errors": [],
                "coverage": {"complete": True},
                "tracks": [
                    {
                        "url": "track",
                        "status": "active",
                        "read": True,
                        "next_action": "private action",
                        "children": [{"url": "child", "status": "waiting", "unread": []}],
                    }
                ],
            }
            state = Path(directory) / "board.json"
            first = goal_board.board_changes(board, state, [])
            self.assertEqual(len(first["records"]), 2)
            self.assertNotIn("private action", json.dumps(first))
            board["checked_at"] = "later"
            self.assertEqual(goal_board.board_changes(board, state, [])["records"], [])
            board["tracks"][0]["children"][0]["status"] = "active"
            self.assertEqual(goal_board.board_changes(board, state, [])["records"][0]["url"], "child")
            questions = {"complete": True, "questions": [{"url": "question", "question": "private question", "age_seconds": 1}]}
            state = Path(directory) / "questions.json"
            self.assertNotIn("private question", json.dumps(oq.question_changes(questions, state, [])))
            questions["questions"][0]["age_seconds"] = 500
            self.assertEqual(oq.question_changes(questions, state, [])["questions"], [])
            questions["questions"][0]["question"] = "edited question"
            self.assertTrue(oq.question_changes(questions, state, [])["questions"])
            self.assertNotIn("edited question", state.read_text())


class OperationTests(unittest.TestCase):
    def test_uncertain_mutation_is_not_replayed_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory)
            operation = check_operation.Operation(evidence, ["session"])
            with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("native", 1)) as run:
                with self.assertRaises(subprocess.TimeoutExpired):
                    operation.run("quit", ["native"], once=True)
                operation = check_operation.Operation(evidence, ["session"])
                with self.assertRaises(check_operation.GateFailed):
                    operation.run("quit", ["native"], once=True)
                self.assertEqual(run.call_count, 1)

    def test_failed_finish_keeps_terminal_and_returns_first_gate(self) -> None:
        entry = {"session_id": "native-session", "backend": "tmux", "tmux_session_id": "terminal", "transcript": "fixture", "pid": 8}
        args = argparse.Namespace(
            ledger=Path("ledger"), session_id=entry["session_id"], supervisor_session="supervisor", owner="owner", exit_seconds=1
        )
        operation = Mock(state={"steps": {"quit": {"complete": True}}})
        operation.run.side_effect = [
            {"claims_clear": True, "coverage": {"complete": True}},
            {"ok": False, "needs_reconciliation": True},
        ]
        with (
            patch.object(session_closeout, "load_ledger", return_value=[entry]),
            patch.object(session_closeout, "ready"),
            patch.object(session_closeout.release, "handoff", return_value={"body": "handoff"}),
            patch.object(session_closeout.release.release, "related_sessions_match", return_value=True),
            patch.object(session_closeout.release.github_identity, "automation_login", return_value="agent[bot]"),
            patch.object(session_closeout.session_liveness, "process_absent", return_value=True),
        ):
            with self.assertRaises(check_operation.GateFailed) as failure:
                session_closeout.closeout(args, operation)
        self.assertEqual(failure.exception.gate, "finish")
        self.assertEqual(operation.run.call_count, 2)

    def test_handoff_observation_requires_a_new_completed_turn(self) -> None:
        original = {"state": "turn_ended", "size": 5, "last_text": "landing", "safe_verdict": False}
        final = {**original, "size": 10, "safe_verdict": True}
        with (
            patch.object(catalog_landing, "session_status", side_effect=[original, final]),
            patch.object(catalog_landing.time, "sleep"),
        ):
            self.assertEqual(catalog_landing.observe({}, "landing", after_size=5, final=True), final)


if __name__ == "__main__":
    unittest.main()
