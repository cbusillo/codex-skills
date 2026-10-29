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
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import catalog_landing
import check_changes
import check_operation
import goal_board
import iterm_tab
import oq
import session_closeout
import session_liveness


def patch_attr(target: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    return patch.object(target, name, *args, **kwargs)


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
            board: dict[str, Any] = {
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
            questions: dict[str, Any] = {
                "complete": True,
                "questions": [{"url": "question", "question": "private question", "age_seconds": 1}],
            }
            state = Path(directory) / "questions.json"
            self.assertNotIn("private question", json.dumps(oq.question_changes(questions, state, [])))
            questions["questions"][0]["age_seconds"] = 500
            self.assertEqual(oq.question_changes(questions, state, [])["questions"], [])
            questions["questions"][0]["question"] = "edited question"
            self.assertTrue(oq.question_changes(questions, state, [])["questions"])
            self.assertNotIn("edited question", state.read_text())


class OperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def test_uncertain_mutation_is_not_replayed_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory)
            operation = check_operation.Operation(evidence, ["session"])
            with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("native", 1)) as run:
                with self.assertRaises(check_operation.GateFailed):
                    operation.run("quit", ["native"], once=True)
                operation = check_operation.Operation(evidence, ["session"])
                with self.assertRaises(check_operation.GateFailed):
                    operation.run("quit", ["native"], once=True)
                self.assertEqual(run.call_count, 1)

    def test_failed_finish_keeps_terminal_and_returns_first_gate(self) -> None:
        entry: dict[str, Any] = {
            "session_id": "native-session",
            "backend": "tmux",
            "tmux_session_id": "terminal",
            "transcript": "fixture",
            "pid": 8,
        }
        args = argparse.Namespace(
            ledger=Path("ledger"), session_id=entry["session_id"], supervisor_session="supervisor", owner="owner", exit_seconds=1
        )
        operation = check_operation.Operation(self.root, [entry["session_id"]])
        operation.state["steps"]["quit"] = {"complete": True}
        run = Mock(
            side_effect=[
                {"claims_clear": True, "coverage": {"complete": True}},
                {"ok": False, "needs_reconciliation": True},
            ]
        )
        with (
            patch.object(operation, "run", run),
            patch_attr(session_closeout, "load_ledger", return_value=[entry]),
            patch.object(session_closeout, "ready"),
            patch.object(session_closeout.release, "handoff", return_value={"body": "Session: " + entry["session_id"]}),
            patch.object(session_closeout.github_identity, "automation_login", return_value="agent[bot]"),
            patch.object(session_closeout.session_liveness, "process_absent", return_value=True),
        ):
            with self.assertRaises(check_operation.GateFailed) as failure:
                session_closeout.closeout(args, operation)
            entry["pid"] += 1
            with self.assertRaises(check_operation.GateFailed) as changed:
                session_closeout.closeout(args, operation)
            self.assertEqual(changed.exception.gate, "identity")
        self.assertEqual(failure.exception.gate, "finish")
        self.assertFalse(any(call.args[0] == "close" for call in run.call_args_list))

    def test_handoff_observation_requires_a_new_completed_turn(self) -> None:
        original = {"state": "turn_ended", "size": 5, "last_text": "landing", "safe_verdict": False}
        final = {**original, "size": 10, "safe_verdict": True, "last_text": "Posted the handoff. Safe to exit: yes"}
        with (
            patch_attr(catalog_landing, "session_status", side_effect=[original, final]),
            patch.object(catalog_landing.time, "sleep"),
        ):
            self.assertEqual(catalog_landing.observe({}, "landing", after_size=5, final=True), final)

    def test_definite_refusal_can_retry_but_uncertain_send_cannot(self) -> None:
        operation = check_operation.Operation(self.root, ["target"])
        refused = subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "mutation_started": False}), "")
        success = subprocess.CompletedProcess([], 0, json.dumps({"close_requested": True}), "")
        with patch.object(subprocess, "run", side_effect=[refused, success]):
            with self.assertRaises(check_operation.GateFailed):
                operation.run("close", ["native"], once=True)
            operation = check_operation.Operation(self.root, ["target"])
            self.assertTrue(operation.run("close", ["native"], once=True)["close_requested"])

    def test_unpresented_questions_and_positive_handoff_identity(self) -> None:
        report: dict[str, Any] = {"complete": True, "questions": [{"url": "q", "question": "question", "resurface": True}]}
        for _ in range(2):
            self.assertTrue(oq.question_changes(report, self.root / "questions.json", ["source"])["questions"])
        self.assertFalse(session_closeout.explicit_session("Catalog landing verified", "native"))
        self.assertTrue(session_closeout.explicit_session("Session: native", "native"))
        self.assertFalse(session_closeout.explicit_session("Session: another", "native"))

    def test_managed_proxy_and_dim_suggestions_use_shared_native_rules(self) -> None:
        proxy = str(Path(__file__).with_name("codex-session.py"))
        self.assertTrue(session_liveness.source_harness([proxy, "native"], "codex"))
        self.assertTrue(iterm_tab.empty_agent_input({"input_line": {"style": "dim", "text": "a different suggestion"}}))
        self.assertFalse(iterm_tab.empty_agent_input({"input_line": {"style": "mixed", "text": "draft"}}))

    def test_smoke_brief_satisfies_launcher_rules_and_read_only_ownership(self) -> None:
        operation = check_operation.Operation(self.root, ["landing"])
        config = {
            "repo": "owner/catalog",
            "issue": 1,
            "landing_sha": "a" * 40,
            "ledger": str(self.root / "ledger.json"),
            "command_files": {"codex": str(self.root / "command.txt")},
            "window_id": "container",
        }
        (self.root / "command.txt").write_text("exec codex --remote unix://")
        with patch.object(operation, "rows", side_effect=check_operation.GateFailed("launch", "fixture")):
            with self.assertRaises(check_operation.GateFailed):
                catalog_landing.verify_smoke(config, operation, "codex", "openai", self.root)
        brief = (self.root / "codex-brief.txt").read_text()
        iterm_tab.validate_brief_rules(brief)
        payload = {"ok": True, "issue": {"launch_check": {"allowed": True}}}
        with patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload))) as run:
            iterm_tab.check_launch_issue(brief)
        self.assertIn("--read-only", run.call_args.args[0])

    def test_claude_registration_is_idempotent_and_preserves_other_terminal_identity(self) -> None:
        native = {"session_id": "native-claude", "home": str(self.root)}
        transcript = self.root / "projects" / "fixture" / (native["session_id"] + ".jsonl")
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps(
                {
                    "sessionId": native["session_id"],
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "acknowledged"}], "stop_reason": "end_turn"},
                }
            )
            + "\n"
        )
        launch = {"session_id": "owned-terminal", "pid": 8, "tty": "/dev/ttys001", "backend": "tmux"}
        ledger = self.root / "ledger.json"
        with patch.object(session_liveness, "inventory", return_value=[(8, "ttys001", "claude")]):
            first = iterm_tab.register_claude_launch(launch, native, ledger)
            second = iterm_tab.register_claude_launch(launch, native, ledger)
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(len(json.loads(ledger.read_text())), 1)
        with self.assertRaises(ValueError):
            iterm_tab.register_claude_launch({**launch, "session_id": "another-terminal"}, native, ledger)


if __name__ == "__main__":
    unittest.main()
