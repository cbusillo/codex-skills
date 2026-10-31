#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5", "PyYAML==6.0.3"]
# ///
import argparse
import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

os.environ["CODEX_SKILLS_ENV_FILE"] = "/definitely/missing/codex-skills-test.env"

import goal_board
import routine_check
import session_closeout
from check_operation import GateFailed, Operation
from test_goal_board import TRUSTED, child, comment

from skills.github.scripts.github_api import ApiResult


def patch_attr(target: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    return patch.object(target, name, *args, **kwargs)


class RoutineCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.dict(os.environ, {"CODE_HOME": temporary.name}))
        self.enterContext(patch.object(routine_check.oq, "default_decision_authors", return_value=["agent[bot]"]))
        self.enterContext(patch_attr(routine_check.radar.github_identity, "automation_login", return_value="agent[bot]"))
        self.enterContext(patch_attr(routine_check.radar.github_identity, "configured_bot_logins", return_value=["agent[bot]"]))

    def test_digest_omits_answered_history_and_keeps_new_unanswered_comments(self) -> None:
        cases = (
            ("companion-module-apple-tv", 50, "closed", "issue_body", "mikecreates86", "Thanks for this module.",
             "shiny-code-app[bot]", "Thanks Mike! Tested and merged."),
            ("odoo-tenant-cm", 296, "open", "issue_body", "cellmechanic-website-agent[bot]", "Update bot permissions.",
             "cbusillo", "@cellmechanic done. Please test and close this issue."),
            ("codex-skills", 3047, "closed", "issue_body", "cellmechanic", "The audit reports false findings.",
             "shiny-code-app[bot]", "Completed through PR #3054."),
            *(('odoo-tenant-cm-website', number, "closed", "issue_comment", "cellmechanic-website-agent[bot]",
               "Status correction from Justin.", "shiny-code-app[bot]", "Closing as delivered.") for number in (33, 75, 76)),
        )
        for repo, number, state, kind, author, body, reply_author, reply_body in cases:
            with self.subTest(repo=repo, number=number):
                external = routine_check.radar.Comment(
                    repo=f"cbusillo/{repo}", number=number, kind=kind, comment_id=1, author=author,
                    author_type="", created_at="2026-10-20T15:49:00Z", updated_at="2026-10-20T15:49:00Z",
                    body=body, url=f"https://github.com/cbusillo/{repo}/issues/{number}#issuecomment-1",
                )
                reply = replace(external, kind="issue_comment", comment_id=2, author=reply_author, body=reply_body,
                                created_at="2026-10-24T15:00:00Z", updated_at="2026-10-24T15:00:00Z", url="reply")
                followup = replace(external, kind="issue_comment", comment_id=3, body="A new question?",
                                   created_at="2026-10-25T15:00:00Z", updated_at="2026-10-25T15:00:00Z", url="followup")
                rows = routine_check.radar.comment_states_for_thread(
                    {"state": state}, [external, reply, followup], {external.key, followup.key},
                    {"cbusillo"}, {"shiny-code-app[bot]"}, {external.key: [], followup.key: []},
                    automation_logins={"cellmechanic-website-agent[bot]": "Client"},
                )
                output = routine_check.digest({"comments": {"attention": rows}})
                self.assertNotIn(external.url, output)
                self.assertIn(followup.url, output)

    def test_opening_post_stops_repeating_after_any_reply_even_after_a_body_edit(self) -> None:
        opening = routine_check.radar.Comment(
            repo="owner/repo", number=7, kind=routine_check.radar.OPENING_POST, comment_id=1, author="client",
            author_type="User", created_at="2026-10-20T15:00:00Z", updated_at="2026-10-25T15:00:00Z",
            body="Please implement this.", url="opening",
        )
        reply = replace(opening, kind="issue_comment", comment_id=2, author="another-person", body="More context.",
                        created_at="2026-10-21T15:00:00Z", updated_at="2026-10-21T15:00:00Z", url="reply")
        for comments in ([opening], [opening, reply]):
            rows = routine_check.radar.comment_states_for_thread(
                {"state": "open"}, comments, {opening.key}, {"owner"}, {"agent[bot]"}, {opening.key: []},
            )
            output = routine_check.digest({"comments": {"attention": rows}})
            self.assertEqual(opening.url in output, len(comments) == 1)

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
                "questions": [{"url": "question", "delivery": "acknowledged", "registration_url": "registration"}],
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

    def test_reader_failure_does_not_hide_other_results_and_repeats_errors(self) -> None:
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

        for error in (routine_check.radar.rollup.RollupError("radar failed"), SystemExit("config failed")):
            readers["comments"].side_effect = error
            output = routine_check.digest(routine_check.collect(readers))
            self.assertIn(str(error), output)
            self.assertIn("watch offline", output)

    def test_digest_lists_registered_queue_and_summarizes_unregistered_questions(self) -> None:
        registered = {"url": "registered-question", "registration_url": "registration", "delivery": "unacknowledged"}
        discovered = [{"url": f"discovered-{number}"} for number in range(50)]
        output = routine_check.digest({
            "board": {"tracks": [{"open_questions": [{"url": "historical-board-question"}]}]},
            "questions": {"queue": "owner/direction#98", "questions": [registered, *discovered],
                          "unnumbered_sources": ["discovered-49"], "untrusted_sources": ["discovered-48"]},
        })
        self.assertIn("registered-question", output)
        self.assertNotIn("historical-board-question", output)
        summary = next(line for line in output.splitlines() if discovered[0]["url"] in line)
        self.assertIn(str(len(discovered)), summary)
        self.assertNotIn("discovered-47", output)
        self.assertIn("discovered-49", output)
        self.assertIn("discovered-48", output)

    def test_missing_and_partial_watch_logs_are_coverage_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "watch.jsonl"
            with self.assertRaises(OSError):
                routine_check.watch_report(log)
            report = {"notices": [{"url": "client", "kind": "foreign"}], "errors": [], "coverage_gaps": []}
            log.write_text(json.dumps(report) + '\n{"error":"watch restart refused"}\n{')
            for _ in range(2):
                watch = routine_check.watch_report(log)
                output = routine_check.digest({"watch": watch})
                self.assertEqual(watch["history"][0]["url"], "client")
                self.assertIn("watch restart refused", output)
                self.assertIn("incomplete", output)

    def test_staffing_clears_only_after_its_targeted_reply(self) -> None:
        request = comment("STAFF NOW: take the next source slice")
        unrelated = comment(f"Staffed: {request['url']}0", ident=2)
        node = child(277, comments=[request, unrelated])
        self.assertTrue(goal_board.child_row(node, TRUSTED)["staffing_requests"])
        node["comments"]["nodes"].append(comment(f"Staffed the requested slices: {request['url']}", ident=3))
        node["comments"]["totalCount"] += 1
        self.assertFalse(goal_board.child_row(node, TRUSTED)["staffing_requests"])

    def test_watch_notifications_do_not_accumulate_bot_noise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "watch.jsonl"
            now = datetime(2026, 10, 30, tzinfo=UTC)
            old = now - timedelta(
                hours=routine_check.radar.rollup.parse_duration_hours(routine_check.radar.DEFAULT_WINDOW), seconds=1
            )
            rows = [
                {"event": "started", "pid": 10},
                {
                    "notices": [
                        {"url": "old-bot", "kind": "foreign"},
                        {"url": "old-event", "kind": "launchplane", "updated_at": old.isoformat()},
                        {"url": "current-event", "kind": "launchplane", "updated_at": now.isoformat()},
                    ],
                    "errors": [],
                    "coverage_gaps": [],
                },
                {"event": "completed", "exit_code": 0},
                {"event": "started", "pid": 11},
                {"notices": [], "errors": [], "coverage_gaps": []},
            ]
            log.write_text("\n".join(json.dumps(row) for row in rows))
            output = routine_check.digest({"watch": routine_check.watch_report(log, now=now)})
            self.assertIn("current-event", output)
            self.assertNotIn("old-bot", output)
            self.assertNotIn("old-event", output)
            self.assertNotIn("ERROR", output)
            with log.open("a") as stream:
                stream.write('\n{"event":"completed","exit_code":2}\n{"event":"reconciliation_required","exit_code":2}')
            self.assertIn("watcher stopped", routine_check.digest({"watch": routine_check.watch_report(log, now=now)}))
            with log.open("a") as stream:
                stream.write('\n{"notices":[],"errors":[],"coverage_gaps":[]}')
            self.assertNotIn("ERROR", routine_check.digest({"watch": routine_check.watch_report(log, now=now)}))

    def test_reader_wiring_retains_client_attention_and_other_questions_on_source_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger, log = root / "ledger.json", root / "watch.jsonl"
            ledger.write_text("[]")
            log.write_text(json.dumps({"notices": [], "errors": [], "coverage_gaps": []}) + "\n")
            args = argparse.Namespace(
                owner="owner",
                queue="owner/direction#98",
                source=[],
                repo=["owner/repo"],
                thread=[],
                ledger=ledger,
                capacity_state=None,
                foreign_log=log,
                radar_config=root / "absent.yaml",
            )
            good_url = "https://github.com/owner/repo/issues/8#issuecomment-80"
            question = {
                "id": 80,
                "body": "Director question: Q80. Continue?",
                "html_url": good_url,
                "user": {"login": "owner"},
                "created_at": "2026-10-30T20:00:00Z",
            }

            def paged(endpoint: str, **_: object) -> list[dict]:
                if "/issues/7/" in endpoint:
                    raise routine_check.github_read.GitHubReadError(
                        "source unread", result=ApiResult(ok=False, status=404, body=None), diagnostics={}
                    )
                return [question] if "/issues/8/" in endpoint else []

            with (
                patch_attr(routine_check.github_read, "GitHubReader", return_value=SimpleNamespace(paged_json=paged)),
                patch.object(goal_board, "goal_board", return_value={"tracks": [], "coverage": {"complete": True}}),
                patch_attr(
                    routine_check.radar,
                    "collect_payload",
                    return_value={"attention": [{"url": "client"}], "ok": True, "coverage": {"errors": []}},
                ),
            ):
                watch = {
                    "history": [
                        {"kind": "director_question", "url": "https://github.com/owner/repo/issues/7#issuecomment-70"},
                        {"kind": "director_question", "url": good_url},
                    ]
                }
                output = routine_check.digest(routine_check.collect(routine_check.build_readers(args, watch)))
            self.assertIn("client", output)
            self.assertIn(good_url, output)
            self.assertIn("source unread", output)

    def test_selected_reader_404_preserves_staffing_rows_and_unknown_authority_is_explicit(self) -> None:
        reader_module = routine_check.github_read
        request = comment("STAFF NOW: take the next source slice")
        program = child(277, comments=[request])
        args = argparse.Namespace(
            owner="owner",
            ledger=Path("unused"),
            capacity_state=None,
            repo=[],
            thread=[],
            source=[],
            queue="owner/direction#98",
            radar_config=Path("unused"),
        )

        def board(reader: Any, _owner: str, *, trusted: set[str]) -> dict:
            self.assertIsNone(goal_board.claim_evidence(reader, "owner/repo", 4))
            return {
                "tracks": [{"url": "track", "children": [goal_board.child_row(program, trusted)]}],
                "coverage": {"complete": True},
            }

        with (
            patch_attr(reader_module.github_api_core, "request_identity", return_value=("automation-gh", "agent[bot]", [])),
            patch_attr(reader_module.GitHubReader, "_transport_request", return_value=ApiResult(ok=False, status=404, body=None)),
            patch.object(goal_board, "goal_board", side_effect=board),
            patch.object(routine_check.oq, "default_decision_authors", return_value=["Shiny-Code-App[bot]"]),
        ):
            report = routine_check.build_readers(args, {})["board"]()
            self.assertIn(request["url"], routine_check.digest({"board": report}))
            with patch.object(routine_check.oq, "default_decision_authors", return_value=[]):
                report = routine_check.build_readers(args, {})["board"]()
            self.assertFalse(report["coverage"]["complete"])
            self.assertIn("unconfigured", routine_check.digest({"board": report}))

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
                reader.paged_json.return_value = [handoff, {**handoff, "id": 9}]
                unrecorded = {key: value for key, value in entry.items() if key != "handoff_url"}
                ledger.write_text(json.dumps([{**unrecorded, "note": "updated metadata"}]))
                session_closeout.verify_handoff(unrecorded, args, operation, retired=False)
                self.assertEqual(json.loads(ledger.read_text())[0]["note"], "updated metadata")
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

    def test_explicit_handoff_supports_another_work_issue_and_partial_legacy_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = {"repository": "owner/Program", "session_id": "native"}
            ledger = root / "ledger.json"
            ledger.write_text(json.dumps([entry]))
            url = "https://github.com/owner/child/issues/8#issuecomment-9"
            handoff = {
                "body": "Session: native",
                "user": {"login": "agent[bot]"},
                "issue_url": "https://api.github.com/repos/owner/child/issues/8",
            }
            args = argparse.Namespace(ledger=ledger, session_id="native", handoff_url=url)
            reader = SimpleNamespace(get_json=Mock(return_value=handoff), paged_json=Mock())
            with (
                patch.object(session_closeout, "GitHubReader", return_value=reader),
                patch.object(session_closeout.github_identity, "automation_login", return_value="agent[bot]"),
            ):
                session_closeout.verify_handoff(entry, args, Operation(root / "evidence", ["native"]), retired=False)
            reader.paged_json.assert_not_called()
            self.assertEqual(entry["handoff_url"], url)


if __name__ == "__main__":
    unittest.main()
