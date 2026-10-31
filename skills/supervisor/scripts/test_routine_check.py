#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5", "PyYAML==6.0.3"]
# ///
import argparse
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import goal_board
import routine_check
import session_closeout
from check_operation import GateFailed, Operation
from test_goal_board import TRUSTED, child, comment


class RoutineCheckTests(unittest.TestCase):
    def test_held_accept_and_staff_now_survive_unchanged_checks(self) -> None:
        staffing = comment("STAFF NOW, two independent source slices: OPW406 and Devkit313", ident=6532738729)
        program = goal_board.child_row(child(277, comments=[staffing]), TRUSTED)
        url = "https://github.com/cbusillo/odoo-tenant-cm/pull/290#issuecomment-6531928916"
        client_comment = routine_check.radar.normalize_issue_comment(
            "cbusillo/odoo-tenant-cm",
            {
                "id": 6531928916,
                "html_url": url,
                "issue_url": "https://api.github.com/repos/cbusillo/odoo-tenant-cm/issues/290",
                "user": {"login": "cellmechanic-website-agent[bot]", "type": "Bot"},
                "body": "Justin is holding Accept until the Inventory and Helpdesk menus show for him.",
                "created_at": "2026-10-30T20:00:00Z",
                "updated_at": "2026-10-30T20:00:00Z",
            },
        )
        assert client_comment is not None
        attention = routine_check.radar.comment_states_for_thread(
            {"number": 290, "html_url": url.split("#")[0], "state": "open", "pull_request": {}},
            [client_comment],
            {client_comment.key},
            {"cbusillo"},
            {"shiny-code-app[bot]"},
            {client_comment.key: []},
            automation_logins={"cellmechanic-website-agent[bot]": "Client"},
        )
        client = attention[0]
        self.assertNotEqual(client["attention_state"], "handled")
        sources = {
            "board": {"tracks": [{"url": "track", "children": [program]}], "coverage": {"complete": True}},
            "sessions": {
                "sessions": [{"session_id": "worker", "action": "verify_finished_session"}],
                "coverage": {"complete": True},
            },
            "questions": {
                "queue": "owner/direction#98",
                "questions": [{"url": "question", "delivery": "acknowledged"}],
                "complete": True,
            },
            "comments": {"attention": attention, "coverage": {"errors": []}, "ok": True},
            "watch": {"notices": [], "errors": [], "coverage_gaps": []},
        }
        for _ in range(2):
            output = routine_check.digest(sources)
            self.assertIn(client["comment_url"], output)
            self.assertIn(staffing["url"], output)
            self.assertIn("worker", output)
            self.assertIn("question", output)

    def test_reader_failure_does_not_hide_other_results_or_repeat_errors(self) -> None:
        readers = {
            "board": Mock(side_effect=ValueError("unread program")),
            "watch": Mock(return_value={"errors": ["watch offline"], "coverage_gaps": ["unwatched repo"]}),
            "comments": Mock(
                return_value={
                    "ok": False,
                    "attention": [{"url": "client"}],
                    "coverage": {"errors": [{"lane": "people", "error": "unread identity"}]},
                }
            ),
        }
        for _ in range(2):
            output = routine_check.digest(routine_check.collect(readers))
            for expected in ("unread program", "watch offline", "unwatched repo", "unread identity", "client"):
                self.assertIn(expected, output)

    def test_missing_and_partial_watch_logs_are_coverage_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "watch.jsonl"
            for content in (None, "", '{"errors":[]}\n{'):
                if content is not None:
                    log.write_text(content)
                with self.assertRaises((ValueError, OSError)):
                    routine_check.watch_report(log)

    def test_discovered_handoff_is_unique_exact_authored_and_journaled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = {"repository": "owner/repo", "issue": 7, "session_id": "native"}
            ledger = root / "ledger.json"
            ledger.write_text(json.dumps([entry]))
            args = argparse.Namespace(ledger=ledger, session_id="native")
            operation = Operation(root / "evidence", ["native"])
            url = "https://github.com/owner/repo/issues/7#issuecomment-8"
            handoff = {
                "id": 8,
                "html_url": url,
                "body": "Session: native\nEngineering ready.",
                "issue_url": "https://api.github.com/repos/owner/repo/issues/7",
                "user": {"login": "agent[bot]"},
            }
            claim = {**handoff, "id": 2, "body": "Claimed by worker\nSession: native\nBranch: work/task"}
            reader = SimpleNamespace(paged_json=Mock(return_value=[claim, handoff]), get_json=Mock(return_value=handoff))
            with (
                patch.object(session_closeout, "GitHubReader", return_value=reader),
                patch.object(session_closeout.github_identity, "automation_login", return_value="agent[bot]"),
            ):
                session_closeout.verify_handoff(entry, args, operation, retired=False)
                self.assertEqual(json.loads(ledger.read_text())[0]["handoff_url"], url)
                self.assertEqual(json.loads(operation.path.read_text())["handoff"]["url"], url)
                self.assertEqual(entry["handoff_url"], url)
                for candidates in (
                    [claim],
                    [handoff, {**handoff, "id": 9, "html_url": url.replace("-8", "-9")}],
                    [{**handoff, "user": {"login": "foreign"}}],
                    [{**handoff, "body": "Session: other-native"}],
                ):
                    reader.paged_json.return_value = candidates
                    with self.assertRaises(GateFailed):
                        session_closeout.verify_handoff(
                            {k: v for k, v in entry.items() if k != "handoff_url"},
                            args,
                            Operation(root / "other", ["native"]),
                            retired=False,
                        )


if __name__ == "__main__":
    unittest.main()
