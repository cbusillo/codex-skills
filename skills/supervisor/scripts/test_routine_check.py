#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5", "PyYAML==6.0.3", "tree-sitter==0.26.0", "tree-sitter-bash==0.25.1", "tree-sitter-javascript==0.25.0"]
# ///
import argparse
from functools import partial
from contextlib import redirect_stdout
from io import StringIO
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

os.environ["CODEX_SKILLS_ENV_FILE"] = "/definitely/missing/codex-skills-test.env"

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "work-closeout/scripts"))

import routine_check
import question_delivery
import session_closeout
claim_transcripts: Any = importlib.import_module("claim_transcripts")
from check_operation import GateFailed, Operation

from skills.github.scripts.github_api import ApiResult
from skills.github.scripts import github_agent
from skills.github.scripts.test_gh_plan_next import global_fixture, global_issue, next_args, relationships, track
from skills.github.scripts.test_github_next_queue import classify, item


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
        self.enterContext(patch.object(session_closeout.closure, "source_records", return_value={}))

    def test_seats_recompute_incomplete_reports_while_sharing_successful_queries(self) -> None:
        root = Path(os.environ["CODE_HOME"])
        watch = root / "watch.jsonl"
        watch.write_text(json.dumps({"notices": [], "errors": [], "coverage_gaps": []}))
        argv = ["routine_check.py", "--owner", "owner", "--queue", "owner/direction#1", "--ledger", str(root / "ledger.json"),
                "--foreign-log", str(watch)]
        reader = routine_check.github_read.GitHubReader(gh_cmd="fake-gh", operation="github.supervisor.routine_check",
                                                       actor="agent[bot]", expected_actor="agent[bot]")

        def next_report() -> dict:
            result = reader.graphql_json("query { repository(owner:\"owner\",name:\"repo\") { name } }", {}, step="source")
            return {"candidate_coverage": {"complete": result.ok and result.body["data"]["repository"] is not None}}

        response = ApiResult(ok=True, status=200, body={"data": {"repository": None}}, actor="agent[bot]", expected_actor="agent[bot]")
        with (patch_attr(routine_check.github_read.github_api_core, "call_gh_with_retry", return_value=response) as remote,
              patch.object(routine_check.github_access, "identity_context", return_value=({"CODEX_SKILLS_ENV_FILE": str(root / "env")}, True)),
              patch.object(routine_check, "build_readers", return_value={"next": next_report, "sessions": lambda: {"sessions": []}}),
              patch.object(routine_check, "primary_reads", return_value={"coverage": {"complete": True}})):
            errors = []
            for producer in ("first", "second", "third"):
                with (patch("sys.argv", argv), patch.dict(os.environ, {"CODEX_SESSION_ID": producer, "CLAUDE_CODE_SESSION_ID": ""}),
                      redirect_stdout(StringIO()) as output):
                    self.assertEqual(routine_check.main(), 1)
                errors.append([line for line in output.getvalue().splitlines() if line.startswith("ERROR next")])
            self.assertTrue(errors[0])
            self.assertEqual(errors, [errors[0]] * len(errors))
            self.assertEqual(remote.call_count, 1)
            self.assertRegex(output.getvalue(), r"REUSED .*age=\d+\.\ds")
            with (patch("sys.argv", argv), patch.dict(os.environ, {"CODEX_SESSION_ID": "first", "CLAUDE_CODE_SESSION_ID": ""}),
                  redirect_stdout(StringIO())):
                routine_check.main()
            self.assertEqual(remote.call_count, 2)

    def test_digest_keeps_every_track_decision_wait_and_unread_source(self) -> None:
        roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second"),
                 track("someone/direction", 3, "First")]
        held = global_issue("someone/product", 4, waiting_for="Client acceptance")
        decision_url = roots[0]["html_url"] + "#issuecomment-10"
        forged_url = roots[0]["html_url"] + "#issuecomment-11"
        comments = {("someone/direction", 1): [
            {"id": 10, "user": {"login": "someone"}, "body": "\n\t**Director decision Q50: Keep the hold.", "html_url": decision_url},
            {"id": 11, "user": {"login": "outsider"}, "body": "Director decision Q50: Drop it.", "html_url": forged_url},
        ]}
        for unread in (False, True):
            with self.subTest(unread=unread), global_fixture(
                roots, [held], {("someone/direction", 1): relationships(sub_issues=[held])},
                comments=comments, clear_waits=roots,
            ) as (module, result, _reads):
                original = module.read_next_issue_relationships

                def read(repo: str, number: int, raw: dict | None = None) -> Any:
                    if unread and number == 2:
                        raise module.PlanError("Track source unavailable")
                    return original(repo, number, raw)

                with patch_attr(module, "read_next_issue_relationships", side_effect=read):
                    module.cmd_next(next_args())
                for full in (False, True):
                    output = routine_check.digest({"next": result}, full=full)
                    for root in roots:
                        self.assertIn(root["html_url"], output)
                    self.assertIn(decision_url, output)
                    self.assertNotIn(forged_url, output)
                    self.assertIn(held["html_url"], output)
                    self.assertIn("Client acceptance", output)
                    if unread:
                        self.assertRegex(output, roots[1]["html_url"] + r"[^\n]*UNKNOWN")

    def test_failed_next_keeps_discovered_tracks_when_all_bodies_are_unread(self) -> None:
        roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
        for root in roots:
            root.pop("body")
        with global_fixture(roots, [], {}) as (module, _result, _reads):
            with self.assertRaises(module.PlanError) as error:
                module.cmd_next(next_args())
            output = routine_check.digest({"next": {"ok": False, **error.exception.payload}})
        for root in roots:
            self.assertRegex(output, root["html_url"] + r"[^\n]*UNKNOWN")
        self.assertIn("ERROR next", output)

    def test_graphql_bot_sources_keep_acknowledgement_until_edited(self) -> None:
        stamp = "2026-11-01T00:00:00Z"
        def message(ident: int, body: str) -> dict[str, Any]:
            return {"__typename": "IssueComment", "id": f"node-{ident}", "databaseId": ident,
                    "body": body, "createdAt": stamp, "updatedAt": stamp,
                    "url": f"https://github.com/owner/repo/issues/8#issuecomment-{ident}",
                    "author": {"__typename": "Bot", "login": "agent"}}
        messages = [message(80, "Director question: Q80. Proceed?"), message(81, "Director question: Q81. Publish?"),
                    message(82, "Director decision Q80: Yes. https://github.com/owner/repo/issues/8#issuecomment-80")]
        target = messages[1]["url"]
        queue = [message(90, question_delivery.marker_body("registered", target)),
                 message(91, question_delivery.marker_body(
                     "delivered", target, "https://github.com/owner/direction/issues/98#issuecomment-92", source_updated_at=stamp))]
        for row in queue:
            row["url"] = f"https://github.com/owner/direction/issues/98#issuecomment-{row['databaseId']}"
        def response(_query: str, variables: dict[str, Any], **_kwargs: Any) -> ApiResult:
            number = variables["number"]
            comments = messages if number == 8 else queue
            node = {**message(number, "Work"), "__typename": "Issue", "number": number,
                    "title": "Work", "state": "OPEN", "closedAt": None,
                    "comments": {"nodes": comments, "totalCount": len(comments),
                                 "pageInfo": {"hasNextPage": False, "endCursor": None}}}
            return ApiResult(ok=True, status=200, body={"data": {"repository": {"issueOrPullRequest": node}}})
        reader = SimpleNamespace(deadline_at=None, graphql_json=Mock(side_effect=response))
        args = argparse.Namespace(owner="owner", queue="owner/direction#98", source=["owner/repo#8"],
                                  repo=[], thread=[], radar_config=Path("/absent/radar.yaml"))
        with patch_attr(routine_check.github_read, "GitHubReader", return_value=reader):
            report = routine_check.build_readers(args, {})["questions"]()
            messages[1]["updatedAt"] = "2026-11-01T01:00:00Z"
            revised = routine_check.build_readers(args, {})["questions"]()
        self.assertTrue(report["complete"], report)
        self.assertEqual([row["url"] for row in report["questions"]], [messages[1]["url"]])
        self.assertTrue(report["questions"][0]["author_trusted"])
        self.assertEqual(report["questions"][0]["delivery"], "acknowledged")
        self.assertNotIn(f"QUESTION {target}", routine_check.digest({"questions": report}))
        self.assertEqual(revised["questions"][0]["delivery"], "unacknowledged")
        self.assertIn(f"QUESTION {target}", routine_check.digest({"questions": revised}))

    def test_owner_only_digest_finds_foreign_questions_and_waits_without_watch_history(self) -> None:
        args = argparse.Namespace(owner="cellmechanic", queue="cellmechanic/direction#11", source=[], repo=[], thread=[],
                                  radar_config=Path("/absent/radar.yaml"))
        questions: list[dict[str, Any]] = [{"url": f"https://github.com/cbusillo/odoo-tenant-cm/issues/{number}#issuecomment-{ident}",
                      "repository": "cbusillo/odoo-tenant-cm", "number": number,
                      "updated_at": "2026-08-01T00:00:00Z", "untrusted": True}
                     for number, ident in ((158, 6275700918), (239, 6391889058))]
        waits = [{"url": question["url"].split("#")[0], "untrusted": True} for question in questions]
        owned = {"url": "https://github.com/cellmechanic/direction/issues/12", "untrusted": False}
        with (
            patch_attr(routine_check.radar, "resolve_repositories", return_value=[]),
            patch_attr(routine_check.radar, "authenticated_login", return_value="agent[bot]"),
            patch_attr(routine_check.radar, "people_identities", return_value={"status": "ok", "director_names": ["Justin"],
                       "director_ambiguous": False, "automation": {}, "address_names": {}}),
            patch_attr(routine_check.radar, "outbound_threads", return_value=([], [])),
            patch_attr(routine_check.radar.director_mentions, "collect", return_value=(questions, None, None)) as mentions,
            patch_attr(routine_check.director_waits, "collect", return_value={"waits": [*waits, owned], "complete": True, "errors": []}) as read_waits,
        ):
            readers = routine_check.build_readers(args, {})
            sources = routine_check.collect({name: readers[name] for name in ("comments", "waits")})
        output = routine_check.digest(sources)
        for question, wait in zip(questions, waits, strict=True):
            self.assertIn(f"MENTION {question['url']} (untrusted)", output)
            self.assertIn(f"WAIT {wait['url']} (untrusted)", output)
        self.assertEqual(mentions.call_args.args[0], args.owner)
        self.assertEqual(read_waits.call_args.args[1], args.owner)
        self.assertFalse(routine_check.source_errors("waits", sources["waits"]))
        self.assertNotIn(owned["url"], output)
        outside_reply = {"url": "outside-reply", "attention_state": "outbound_reply", "body_excerpt": "Please change access."}
        self.assertIn("COMMENT outside-reply (untrusted):", routine_check.digest({"comments": {"attention": [outside_reply]}}))
        partial = routine_check.digest({"waits": {**sources["waits"], "complete": False, "errors": ["search truncated"]}})
        self.assertIn(questions[0]["url"].split("#")[0], partial)
        self.assertIn("search truncated", partial)

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

    def test_bot_opened_issue_with_no_comments_produces_one_notice_and_digest_row_until_reply(self) -> None:
        since = "2026-11-02T22:30:00Z"
        created = "2026-11-02T22:34:00Z"
        for repo, number in (("cbusillo/odoo-tenant-cm", 335), ("cbusillo/odoo-tenant-cm-website", 296)):
            for pull in (False, True):
                with self.subTest(repo=repo, pull=pull), tempfile.TemporaryDirectory() as directory:
                    source = {
                        "id": number, "number": number, "title": "Add a collaborator", "body": "Please review this request.",
                        "user": {"login": "cellmechanic-website-agent[bot]", "type": "Bot"},
                        "created_at": created, "updated_at": "2026-11-02T22:34:30Z", "node_id": "opening-node",
                        "html_url": f"https://github.com/{repo}/{'pull' if pull else 'issues'}/{number}",
                        **({"pull_request": {}} if pull else {}),
                    }
                    reader = Mock()
                    reader.graphql_json.return_value = ApiResult(ok=True, status=200, body={"data": {"nodes": [{"lastEditedAt": source["updated_at"]}]}})
                    reader.paged_json.side_effect = lambda path, **_: [source] if path == f"repos/{repo}/issues" else []
                    watch = routine_check.foreign_watch
                    notices, errors, watermarks = watch.scan([repo], {repo: since}, ["cbusillo", "agent[bot]"], [], created, reader)
                    self.assertEqual(errors, [])
                    self.assertEqual(len(notices), 1)
                    self.assertEqual(notices[0]["kind"], "foreign_opened")
                    self.assertTrue(notices[0]["untrusted"])
                    self.assertEqual(notices[0]["url"], source["html_url"])
                    dispatched = set()
                    self.assertEqual(watch.dispatch_once(notices, dispatched), notices)
                    state = Path(directory) / "state.json"
                    watch.save_state(state, watermarks, dispatched=dispatched)
                    self.assertEqual(watch.dispatch_once(notices, watch.load_dispatched(state)), [])
                    self.assertEqual(notices[0]["updated_at"], source["created_at"])
                    old_notices, errors, _ = watch.scan([repo], {repo: source["updated_at"]}, ["cbusillo", "agent[bot]"], [], source["updated_at"], reader)
                    self.assertEqual((old_notices, errors), ([], []))
                    args = argparse.Namespace(owner="cbusillo", repo=[], thread=[], radar_config=Path("/absent/radar.yaml"))
                    report = {"attention": [], "coverage": {"errors": []}, "ok": True}
                    with patch_attr(routine_check.radar, "resolve_settings", return_value={}), patch_attr(routine_check.radar.rollup, "load_config", return_value={}), patch_attr(routine_check.radar, "collect_payload", return_value=report) as collect:
                        routine_check.build_readers(args, {"history": notices})["comments"]()
                    [hint] = collect.call_args.kwargs["comment_hints"]
                    self.assertEqual(hint.url, source["html_url"])
                    self.assertEqual(hint.kind, routine_check.radar.OPENING_POST)
                    opening = routine_check.radar.normalize_opening_post(repo, source)
                    self.assertIsNotNone(opening)
                    assert opening is not None
                    self.assertTrue(routine_check.radar.is_external_comment(opening, {"cbusillo"}, {"agent[bot]"}))
                    reply = replace(opening, kind="issue_comment", comment_id=number + 1, author="another-person",
                                    created_at="2026-11-02T22:35:00Z", url="reply")
                    for comments in ([opening], [opening, reply]):
                        rows = routine_check.radar.comment_states_for_thread(
                            source, comments, {opening.key}, {"cbusillo"}, {"agent[bot]"}, {opening.key: []},
                        )
                        output = routine_check.digest({"comments": {"attention": rows}})
                        self.assertEqual(f"COMMENT {opening.url} (untrusted):" in output, len(comments) == 1)

    def test_held_accept_and_agent_next_action_survive_unchanged_checks(self) -> None:
        program = item(number=277, actor="agent:integrator", action="Finish the two source slices.")
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
            "next": {"candidates": [{**program, "queue": classify(program)}], "candidate_coverage": {"complete": True}},
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
            self.assertIn(program["url"], output)
            self.assertIn("worker", output)

    def test_default_omits_acknowledged_questions_and_full_restores_them(self) -> None:
        sources = {
            "questions": {"queue": "owner/direction#98", "questions": [
                {"url": "seen", "registration_url": "registration", "delivery": "acknowledged"},
                {"url": "unseen", "registration_url": "registration", "delivery": "unacknowledged"},
            ]},
            "next": {"candidates": [{"url": "unclaimed", "state": "OPEN", "queue": {
                "state": "available", "next_actor": "agent", "next_action": "Implement."}}]},
            "comments": {"attention": [{"url": "client", "body_excerpt": "Please fix."}]},
        }
        compact = routine_check.digest(sources)
        self.assertNotIn("QUESTION seen:", compact)
        self.assertIn("OMITTED 1", compact)
        complete = routine_check.digest(sources, full=True)
        self.assertIn("QUESTION seen:", complete)
        for prefix in ("QUESTION unseen:", "NEXT unclaimed:", "COMMENT client", "LOAD "):
            self.assertIn(prefix, compact)
            self.assertIn(prefix, complete)

    def test_reader_failure_does_not_hide_other_results_and_repeats_errors(self) -> None:
        readers = {
            "next": Mock(side_effect=ValueError("unread program")),
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

    def test_readers_overlap_but_comments_reuse_finished_question_reads(self) -> None:
        next_started = threading.Event()
        order = []

        def staffing() -> dict:
            next_started.set()
            return {"overlapped": True}

        def questions() -> dict:
            order.append("questions")
            return {"overlapped": next_started.wait(5)}

        def comments() -> dict:
            order.append("comments")
            return {}

        sources = routine_check.collect({"questions": questions, "comments": comments, "next": staffing})

        self.assertTrue(sources["questions"]["overlapped"])
        self.assertEqual(order, ["questions", "comments"])

    def test_staffing_lists_all_five_requests_outside_tracks_and_both_agent_families(self) -> None:
        requests = [("odoo-devkit", 336), ("odoo-tenant-opw", 419), ("shiny-infra-ops", 811),
                    ("codex-skills", 3283), ("odoo-tenant-cm", 315)]
        rows: list[dict[str, Any]] = [{"url": f"https://github.com/owner/{repo}/issues/{number}", "state": "OPEN",
                 "queue": {"state": "available", "next_actor": "agent:supervisor", "next_action": "Staff this slice."}}
                for repo, number in requests]
        rows[-1]["labels"] = ["agent:claude"]
        rows[0]["labels"] = ["agent:codex"]
        for family in ("codex", None):
            report = {"candidates": rows, "candidate_coverage": {"complete": True}}
            github_agent.filter_selection(report, family)
            output = routine_check.digest({"next": report})
            self.assertEqual(len([line for line in output.splitlines() if line.startswith("SUPERVISOR_ACTION ")]), len(rows))
            for row in rows:
                self.assertIn(row["url"], output)

    def test_staffing_preserves_waits_claims_and_named_recipient_reconciliation(self) -> None:
        def row(source_url: str, state: str, actor: str = "agent") -> dict:
            return {"url": source_url, "state": "OPEN", "queue": {"state": state, "next_actor": actor,
                    "next_action": "Reconcile custody.", "reason": "named_recipient_requires_reconciliation"}}

        report: dict[str, Any] = {"candidates": [row("available", "available"), row("waiting", "waiting"),
                                 row("running", "underway"), row("person", "available", "person:owner")],
                  "needs_status": [row("named-lead", "needs_status", "agent:lead-token"),
                                   row("missing-actor", "needs_status", ""), row("another-missing-actor", "needs_status", "")],
                  "candidate_coverage": {"complete": True}}
        conflicting = row("conflicting-families", "available")
        conflicting["labels"] = ["agent:codex", "agent:claude"]
        report["candidates"].append(conflicting)
        github_agent.filter_selection(report, "codex")
        output = routine_check.digest({"next": report})
        self.assertIn("NEXT available", output)
        self.assertIn("REVIEW named-lead", output)
        self.assertIn("REVIEW conflicting-families", output)
        self.assertIn("NEEDS_STATUS 2:", output)
        self.assertNotIn("REVIEW missing-actor", output)
        for url in ("waiting", "running", "person"):
            self.assertNotIn(f"NEXT {url}", output)

    def test_staffing_coverage_names_omitted_repositories_and_unread_issues(self) -> None:
        report = {"candidate_coverage": {"complete": False, "unevaluated_repositories": [
                      {"repo": "owner/odoo-devkit", "issue_count": 5}]},
                  "discovery_context": {"complete": False, "repositories": [
                      {"repo": "owner/odoo-tenant-cm", "error": "deadline exhausted"}]},
                  "graph_context": {"complete": False, "errors": ["owner/repo#9 unread"]},
                  "queue_context": {"repositories": {"owner/claims": {"complete": False, "error": "claims unread"}}},
                  "focus_context": {"repositories": {"owner/board": {"truncated": True}}},
                  "performance": {"deadline_exhausted": True}}
        for _ in range(2):
            output = routine_check.digest({"next": report})
            for source in ("owner/odoo-devkit", "owner/odoo-tenant-cm", "owner/repo#9", "deadline exhausted", "owner/claims", "owner/board"):
                self.assertIn(source, output)
            self.assertTrue(routine_check.source_errors("next", report))

    def test_digest_lists_registered_queue_and_summarizes_unregistered_questions(self) -> None:
        registered = {"url": "registered-question", "registration_url": "registration", "delivery": "unacknowledged"}
        discovered = [{"url": f"discovered-{number}"} for number in range(50)]
        output = routine_check.digest({
            "questions": {"queue": "owner/direction#98", "questions": [registered, *discovered],
                          "unnumbered_sources": ["discovered-49"], "untrusted_sources": ["discovered-48"]},
        })
        self.assertIn("registered-question", output)
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

    def test_service_failures_repeat_until_a_completed_success_after_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "catalog.jsonl"
            foreign = Path(directory) / "foreign.jsonl"
            foreign.write_text(json.dumps({"notices": [], "errors": [], "coverage_gaps": []}))
            rows = [
                {"event": "started", "pid": 10},
                {"ok": False, "gate": "smoke_response"},
                {"event": "completed", "exit_code": 1},
                {"event": "reconciliation_required", "exit_code": 1},
            ]
            log.write_text("\n".join(json.dumps(row) for row in rows))
            for _ in range(2):
                report = routine_check.watch_report(log, service=True)
                self.assertTrue(report["errors"])
                self.assertIn(rows[1]["gate"], routine_check.digest({"catalog": report}))
            with (patch("sys.argv", ["routine_check.py", "--owner", "owner", "--queue", "owner/direction#1",
                                     "--ledger", str(Path(directory) / "ledger.json"), "--foreign-log", str(foreign),
                                     "--service-log", str(log)]),
                  patch.object(routine_check, "build_readers", return_value={}), redirect_stdout(StringIO()) as stdout):
                self.assertEqual(routine_check.main(), 1)
            self.assertIn(rows[1]["gate"], stdout.getvalue())
            with log.open("a") as stream:
                stream.write('\n{"event":"started","pid":11}\n{"event":"completed","exit_code":0}')
            self.assertTrue(routine_check.watch_report(log, service=True)["errors"])
            with log.open("a") as stream:
                stream.write('\n{"ok":true,"landings":[]}\n{"event":"completed","exit_code":0}')
            self.assertFalse(routine_check.watch_report(log, service=True)["errors"])

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
            self.assertTrue(routine_check.watch_report(log, now=now)["errors"])
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
                "updated_at": "2026-10-30T20:30:00Z",
            }

            def paged(endpoint: str, **_: object) -> list[dict]:
                if "/issues/7/" in endpoint:
                    raise routine_check.github_read.GitHubReadError(
                        "source unread", result=ApiResult(ok=False, status=404, body=None), diagnostics={}
                    )
                return [question] if "/issues/8/" in endpoint else []

            with (
                patch_attr(routine_check.github_read, "GitHubReader", return_value=SimpleNamespace(paged_json=paged)),
                patch_attr(routine_check.github_read, "read_discussion",
                           side_effect=lambda _reader, repo, number: {"comments": paged(f"/repos/{repo}/issues/{number}/comments")}),
                patch.object(routine_check, "staffing", return_value={"candidates": [], "candidate_coverage": {"complete": True}}),
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
                sources = routine_check.collect(routine_check.build_readers(args, watch))
                output = routine_check.digest(sources)
            self.assertIn("client", output)
            self.assertIn(good_url, output)
            self.assertIn("source unread", output)

    def test_staffing_reuses_global_next_and_preserves_partial_failure_evidence(self) -> None:
        report = {"ok": False, "candidate_coverage": {"complete": False},
                  "discovery_context": {"complete": False, "unevaluated_repositories": [{"repo": "owner/repo", "issue_count": 5}]}}
        completed = subprocess.CompletedProcess([], 1, json.dumps(report), "progress only")
        with patch.object(subprocess, "run", return_value=completed) as run:
            result = routine_check.staffing("owner")
        self.assertEqual(result, report)
        self.assertGreater(run.call_args.kwargs["timeout"], 0)
        self.assertIn("owner/repo", routine_check.digest({"next": result}))
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "failed")):
            output = routine_check.digest(routine_check.collect({"next": lambda: routine_check.staffing("owner")}))
        self.assertIn("ERROR next", output)
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("global next", 1)):
            output = routine_check.digest(routine_check.collect({"next": lambda: routine_check.staffing("owner")}))
        self.assertIn("ERROR next", output)

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
                "body": "Explicit handoff from native session native (worker).\nEngineering ready.",
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

    def test_verified_claim_release_disambiguates_session_progress_comments(self) -> None:
        root = Path(os.environ["CODE_HOME"])
        entry = {"repository": "owner/repo", "issue": 7, "session_id": "native"}
        ledger = root / "ledger.json"
        url = "https://github.com/owner/repo/issues/7#issuecomment-8"
        handoff = {"id": 8, "html_url": url, "body": "Released claim 2\nSession: native",
                   "issue_url": "https://api.github.com/repos/owner/repo/issues/7", "user": {"login": "agent[bot]"}}
        checked = {"ok": True, "claims_clear": True, "coverage": {"complete": True}, "session_ids": ["native"],
                   "blocked": [], "unresolved_invocations": [],
                   "released_claims": [{"repository": "owner/repo", "record": 7, "source_session": "native", "release_url": url}]}
        for variant in ("verified", "pr_anchor", "repository_case", "no_session", "failed", "incomplete", "other_session",
                        "ambiguous", "wrong_actor", "conflicting_session"):
            with self.subTest(variant=variant):
                ledger.write_text(json.dumps([entry]))
                current = json.loads(json.dumps(checked))
                release_comment = dict(handoff)
                progress = {**handoff, "id": 9, "html_url": url.replace("-8", "-9"), "body": "Session: native\nReview started"}
                args = argparse.Namespace(ledger=ledger, session_id="native", handoff_url=None)
                if variant == "pr_anchor":
                    release_comment["html_url"] = url.replace("/issues/", "/pull/")
                elif variant == "repository_case":
                    current["released_claims"][0].update(repository="OWNER/REPO", release_url=url.replace("owner/repo", "OWNER/REPO"))
                elif variant == "no_session":
                    release_comment["body"] = "Released claim 2"
                elif variant == "incomplete":
                    current["coverage"]["complete"] = False
                elif variant == "other_session":
                    current["released_claims"][0]["source_session"] = "another"
                elif variant == "ambiguous":
                    progress["body"] = "Released claim 3\nSession: native"
                    current["released_claims"].append({**current["released_claims"][0], "release_url": progress["html_url"]})
                elif variant == "wrong_actor":
                    release_comment["user"] = {"login": "foreign"}
                elif variant == "conflicting_session":
                    release_comment["body"] = "Released claim 2\nSession: another"
                operation = Operation(root / variant, ["native"])
                (operation.evidence / "claims.json").write_text(json.dumps({
                    "exit_code": int(variant == "failed"), "stdout": json.dumps(current)}))
                fallback = variant in {"no_session", "wrong_actor", "conflicting_session"}
                expected = progress if fallback else release_comment
                reader = SimpleNamespace(paged_json=Mock(return_value=[release_comment, progress]), get_json=Mock(return_value=expected))
                with (patch_attr(session_closeout, "GitHubReader", return_value=reader),
                      patch.object(session_closeout.github_identity, "automation_login", return_value="agent[bot]")):
                    if variant in {"failed", "incomplete", "other_session", "ambiguous"}:
                        with self.assertRaises(GateFailed):
                            session_closeout.verify_handoff(dict(entry), args, operation, retired=False)
                    else:
                        session_closeout.verify_handoff(dict(entry), args, operation, retired=False)
                        self.assertEqual(operation.state["handoff"]["url"], expected["html_url"])
                        self.assertEqual(reader.get_json.call_args.args[0], f"/repos/owner/repo/issues/comments/{expected['id']}")

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

    def test_real_no_claim_stops_select_only_a_source_bound_original_comment(self) -> None:
        incidents = json.loads((Path(__file__).parent / "fixtures/closeout-native-posts.json").read_text())
        for incident in (row for row in incidents if not row.get("ended_at")):
            for variant in (None, "case", "elsewhere", "ambiguous", "copied", "wrong_actor", "edited", "conflicting_session", "failed_check", "claims_remain", "later_elsewhere"):
                with self.subTest(worker=incident["token"], variant=variant), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    entry = {key: incident[key] for key in ("repository", "issue", "session_id")}
                    ledger = root / "ledger.json"
                    ledger.write_text(json.dumps([entry]))
                    comments = json.loads(json.dumps(incident["comments"]))
                    number = entry["issue"] + (variant == "elsewhere")
                    if variant == "elsewhere":
                        comments[0]["issue_url"] = f"https://api.github.com/repos/{entry['repository']}/issues/{number}"
                        comments[0]["html_url"] = comments[0]["html_url"].replace(f"/issues/{entry['issue']}#", f"/issues/{number}#")
                    post = incident["posts"][0]
                    evidence: list[dict[str, Any]] = [{"source_session": entry["session_id"], "expected_actor": post["expected_actor"],
                                 "repository": entry["repository"], "number": number,
                                 "comment": {**post["comment"], "url": comments[0]["html_url"]}}]
                    actor = comments[0]["user"]["login"]
                    if variant == "case":
                        evidence[0]["repository"] = entry["repository"].upper()
                    elif variant == "wrong_actor":
                        evidence[0]["expected_actor"] = "another[bot]"
                    elif variant == "edited":
                        comments[0]["updated_at"] = (datetime.fromisoformat(comments[0]["created_at"]) + timedelta(minutes=1)).isoformat()
                    elif variant == "conflicting_session":
                        comments[0]["body"] += "\nSession: another\n"
                    elif variant == "ambiguous":
                        comments.append({**comments[0], "id": 99, "html_url": comments[0]["html_url"].rsplit("-", 1)[0] + "-99"})
                        evidence.append({**evidence[0], "comment": {**evidence[0]["comment"], "id": 99, "url": comments[1]["html_url"]}})
                    if variant == "later_elsewhere":
                        evidence.append({**evidence[0], "number": number + 1})
                    records = []
                    for index, row in enumerate(evidence):
                        created = {**post, "repo": row["repository"], "number": row["number"],
                                   "expected_actor": row["expected_actor"], "comment": row["comment"]}
                        identity = f"post-{index}"
                        command = f"gh-comment issue {row['number']} --repo {row['repository']} --body-file /evidence/stop.md"
                        if variant == "copied":
                            command = "cat /evidence/predecessor.json"
                        records.extend([
                            {"type": "assistant", "sessionId": entry["session_id"], "message": {"content": [{
                                "type": "tool_use", "id": identity, "name": "Bash", "input": {"command": command}}]}},
                            {"type": "user", "sessionId": entry["session_id"], "message": {"content": [{
                                "type": "tool_result", "tool_use_id": identity, "content": json.dumps(created)}]}}])
                    projected = claim_transcripts.handoff_comments({"session_id": entry["session_id"], "_transcript_records": records})
                    checked = {**incident["claims"], "handoff_comments": projected}
                    if variant == "claims_remain":
                        checked["released_claims"] = [{"claim_comment": 1}]
                    operation = Operation(root / "evidence", [entry["session_id"]])
                    (operation.evidence / "claims.json").write_text(json.dumps({
                        "exit_code": 1 if variant == "failed_check" else 0, "stdout": json.dumps(checked), "stderr": ""}))
                    args = argparse.Namespace(ledger=ledger, session_id=entry["session_id"],
                                              handoff_url=comments[0]["html_url"] if variant == "elsewhere" else None)
                    reader = SimpleNamespace(paged_json=Mock(return_value=comments), get_json=Mock(return_value=comments[0]))
                    with (patch.object(session_closeout, "GitHubReader", return_value=reader),
                          patch.object(session_closeout.github_identity, "automation_login", return_value=actor),
                          patch.object(subprocess, "run") as mutation):
                        if variant not in {None, "case", "elsewhere", "claims_remain", "later_elsewhere"}:
                            with self.assertRaises(GateFailed):
                                session_closeout.verify_handoff(entry, args, operation, retired=False)
                            self.assertEqual(json.loads(ledger.read_text()), [entry])
                        else:
                            session_closeout.verify_handoff(entry, args, operation, retired=False)
                            self.assertEqual(entry["handoff_url"], comments[0]["html_url"])
                            self.assertEqual(operation.state["steps"], {})
                        mutation.assert_not_called()

    def test_handoff_association_follows_the_canonical_launch_generation(self) -> None:
        root = Path(os.environ["CODE_HOME"])
        session = "fictional-native"
        urls = {name: f"https://github.com/owner/repo/issues/7#issuecomment-{number}"
                for name, number in (("old", 8), ("intermediate", 9), ("new", 10))}
        original = {"session_id": session, "harness": "codex", "supervisor_owned": True,
                    "pid": 12, "launch_pid": 12, "tty": "ttys010", "launch_generation": "original",
                    "launch_started_at": "2026-11-07T22:00:00Z", "codex_home": str(root),
                    "transcript": str(root / "native.jsonl"), "handoff_url": urls["old"]}
        generation = {**original, "pid": 23, "launch_pid": 23, "tty": "ttys020", "launch_generation": "resumed",
                      "launch_started_at": "2026-11-08T01:35:10Z"}
        source = {**original, "ended_at": "2026-11-07T22:32:16Z", "process_exit_verified": True,
                  "resumed_at": "2026-11-08T01:35:55Z", "resumed_generation": generation}
        variants: list[str] = ["explicit", "intermediate", "manual_closed", "discovery", "journal_retry",
                    "aborted_prelaunch", "aborted_conflict", "same_generation", "missing_source", "wrong_generation",
                    "unknown_source", "unknown_comment", "old_selected", "no_new_handoff", "journal_conflict", "ledger_changed",
                    "canonical_conflict", "canonical_recovery"]
        for variant in variants:
            with self.subTest(variant=variant):
                saved = json.loads(json.dumps(source))
                current = {**generation, "repository": "owner/repo", "issue": 7}
                comments = {name: {"id": number, "html_url": urls[name], "body": f"Session: {session}",
                                   "user": {"login": "agent[bot]"}, "issue_url": "https://api.github.com/repos/owner/repo/issues/7",
                                   "created_at": stamp, "updated_at": stamp}
                            for name, number, stamp in (("old", 8, "2026-11-07T22:31:00Z"),
                                                       ("intermediate", 9, "2026-11-08T00:31:00Z"),
                                                       ("new", 10, "2026-11-08T01:59:00Z"))}
                if variant in {"intermediate", "unknown_comment"}:
                    current["handoff_url"] = urls["intermediate"]
                elif variant == "manual_closed":
                    saved = {**current, "ended_at": "2026-11-08T02:00:00Z", "process_exit_verified": True}
                elif variant == "discovery":
                    current.pop("handoff_url")
                elif variant.startswith("aborted"):
                    current = {**original, "repository": "owner/repo", "issue": 7}
                    saved.pop("resumed_generation")
                elif variant == "same_generation":
                    current["handoff_url"] = urls["intermediate"]
                    comments["intermediate"].update(created_at=comments["new"]["created_at"], updated_at=comments["new"]["created_at"])
                elif variant.startswith("canonical"):
                    saved = {**current, "handoff_url": urls["intermediate"],
                             "ended_at": "2026-11-08T02:00:00Z", "process_exit_verified": True}
                    comments["intermediate"].update(created_at=comments["new"]["created_at"], updated_at=comments["new"]["created_at"])
                elif variant == "wrong_generation":
                    saved["resumed_generation"]["launch_started_at"] = "2026-11-08T01:35:11Z"
                selected = None if variant in {"discovery", "no_new_handoff", "journal_retry", "aborted_prelaunch", "canonical_recovery"} else urls["new"]
                if variant == "old_selected":
                    selected = urls["old"]
                ledger = root / "ledger.json"
                rows = [{**current, "launch_generation": "changed"}] if variant == "ledger_changed" else [current]
                ledger.write_text(json.dumps(rows))
                operation = Operation(root / variant, [session])
                if variant in {"journal_retry", "journal_conflict"}:
                    operation.state["handoff"] = {"url": urls["old" if variant == "journal_conflict" else "new"]}
                args = argparse.Namespace(ledger=ledger, session_id=session, handoff_url=selected)
                by_id = {str(row["id"]): row for row in comments.values()}
                if variant in {"explicit", "intermediate", "discovery", "journal_retry"}:
                    by_id.pop("8")
                if variant == "unknown_comment":
                    by_id.pop("9")
                def read_comment(mapping: dict[str, dict], path: str, **_kwargs: Any) -> dict:
                    if path.rsplit("/", 1)[-1] not in mapping:
                        raise session_closeout.GitHubReadError("prior handoff deleted", result=ApiResult(False, status=404, body=None), diagnostics={})
                    return mapping[path.rsplit("/", 1)[-1]]

                reader = SimpleNamespace(get_json=Mock(side_effect=partial(read_comment, by_id)),
                                         paged_json=Mock(return_value=[comments["old"]] if variant == "no_new_handoff" else list(comments.values())))
                error = {"same_generation": "current handoff differs", "aborted_conflict": "current handoff differs",
                         "missing_source": "current handoff differs", "wrong_generation": "source authority",
                         "unknown_source": "source unavailable", "unknown_comment": "prior handoff deleted",
                         "old_selected": "created in", "no_new_handoff": "found 0 candidates",
                         "journal_conflict": "journal already records", "ledger_changed": "ledger generation changed",
                         "canonical_conflict": "current handoff differs"}.get(variant)
                with (patch_attr(session_closeout, "GitHubReader", return_value=reader),
                      patch.object(session_closeout.github_identity, "automation_login", return_value="agent[bot]"),
                      patch.object(session_closeout.closure, "source_records", return_value={} if variant == "missing_source" else {session: saved},
                                   side_effect=ValueError("source unavailable") if variant == "unknown_source" else None)):
                    if error:
                        with self.assertRaisesRegex(GateFailed, error):
                            session_closeout.verify_handoff(current, args, operation, retired=False)
                        self.assertEqual(json.loads(ledger.read_text()), rows)
                        if variant != "ledger_changed":
                            self.assertNotIn("revision", operation.state.get("handoff", {}))
                    else:
                        session_closeout.verify_handoff(current, args, operation, retired=False)
                        expected = urls["old" if variant == "aborted_prelaunch" else "intermediate" if variant == "canonical_recovery" else "new"]
                        self.assertEqual(json.loads(ledger.read_text())[0]["handoff_url"], expected)
                        self.assertEqual(operation.state["handoff"]["url"], expected)

    def test_handoff_refusal_identifies_examined_comment_and_missing_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            url = "https://github.com/owner/repo/pull/7#issuecomment-8"
            entry = {"repository": "owner/repo", "issue": 7, "session_id": "native"}
            args = argparse.Namespace(session_id="native", handoff_url=url)
            reasons = []
            for body in ("Finished", "Finished\n<!-- github-skill-operation:def456 -->"):
                handoff = {"id": 8, "html_url": url, "body": body, "user": {"login": "agent[bot]"},
                           "issue_url": "https://api.github.com/repos/owner/repo/issues/7"}
                with (self.subTest(body=body), patch.object(session_closeout, "GitHubReader", return_value=SimpleNamespace(
                        get_json=Mock(return_value=handoff))),
                      patch.object(session_closeout.github_identity, "automation_login", return_value="agent[bot]"),
                      self.assertRaises(GateFailed) as refusal):
                    session_closeout.verify_handoff(entry, args, Operation(root, ["native"]), retired=True)
                self.assertIn(url, str(refusal.exception))
                self.assertIn(entry["session_id"], str(refusal.exception))
                reasons.append(str(refusal.exception))
            self.assertNotEqual(*reasons)


def queue_row(number: int, actor: str, action: str, *, recorded: str | None = None, state: str = "available", record_type: str | None = None) -> dict[str, Any]:
    return {"repo": "owner/repo", "number": number, "state": "OPEN", "url": f"https://github.com/owner/repo/issues/{number}",
            **({"record_type": record_type} if record_type else {}),
            "queue": {"state": state, "next_actor": actor, "next_action": action, "status_recorded_at": recorded, "reason": "agent_work"}}


def client_wait(number: int, title: str = "Change") -> dict[str, Any]:
    return {"repo": "owner/repo", "number": number, "record_type": "pull_request", "title": title, "reason": "current_head_client_wait",
            "url": f"https://github.com/owner/repo/pull/{number}", "queue": {"reason": "current_head_client_wait"}}


class PrimaryReadTests(unittest.TestCase):
    now = datetime(2026, 11, 8, 19, 55, tzinfo=UTC)

    def setUp(self) -> None:
        self.enterContext(patch.object(routine_check, "clock", return_value=self.now))
        for name in ("CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            self.enterContext(patch.dict(os.environ, {name: ""}))

    @staticmethod
    def run_digest(selection: dict[str, Any], sessions: list[dict[str, Any]] | None = None, **readers: Any) -> list[str]:
        supplied = {"owner_review": Mock(return_value=None), "client_release": Mock(return_value=SimpleNamespace(state="client", reason="")),
                    "production": Mock(return_value={"reason": "unmapped"}), "anchor_response": Mock(return_value="anchor_response=UNKNOWN"),
                    "checks": Mock(side_effect=AssertionError("no PR lacks a handoff")), **readers}
        primary = routine_check.primary_reads(selection, sessions or [], **supplied)
        return routine_check.digest({"next": selection, "primary": primary, "sessions": {"sessions": sessions or []}}).splitlines()

    def test_supervisor_request_is_its_own_aged_row_that_repeats_until_the_record_changes(self) -> None:
        request = queue_row(746, "agent:supervisor", "Restore the stopped runner\nwith details", recorded="2026-11-08T15:42:00+00:00")
        other = queue_row(1, "agent", "Build the page", recorded="2026-11-08T19:50:00+00:00")
        selection = {"candidates": [other, request], "needs_status": [request]}
        for _ in range(2):
            lines = self.run_digest(selection)
            action = [line for line in lines if line.startswith("SUPERVISOR_ACTION")]
            self.assertEqual(len(action), 1)
            self.assertIn("age=4h13m: Restore the stopped runner", action[0])
            self.assertNotIn("with details", action[0])
            self.assertFalse([line for line in lines if line.startswith("NEXT") and "/746" in line])
            self.assertLess(lines.index(action[0]), next(index for index, line in enumerate(lines) if line.startswith("NEXT")))
            self.assertTrue([line for line in lines if line.startswith("ERROR supervisor_action") and "/746" in line])
        settled = {**request, "queue": {**request["queue"], "next_actor": "agent:lead"}}
        lines = self.run_digest({"candidates": [settled]})
        self.assertFalse([line for line in lines if "SUPERVISOR_ACTION" in line or line.startswith("ERROR")])

    def test_supervisor_action_inside_one_interval_is_not_an_error_and_unknown_age_is(self) -> None:
        fresh = queue_row(2, "agent:Supervisor", "Route it", recorded="2026-11-08T19:40:00+00:00")
        self.assertFalse([line for line in self.run_digest({"candidates": [fresh]}) if line.startswith("ERROR")])
        undated = queue_row(3, "agent:supervisor", "Route it")
        lines = self.run_digest({"candidates": [undated]})
        self.assertTrue([line for line in lines if line.startswith("SUPERVISOR_ACTION") and "age=UNKNOWN" in line])
        self.assertTrue([line for line in lines if line.startswith("ERROR supervisor_action")])

    def test_refused_production_read_is_routed_with_its_read_and_repeats_until_settled(self) -> None:
        read = "health payload of owner/app production over the private operations path"
        row = queue_row(190, "agent:supervisor", f"Production read refused: {read}", recorded="2026-11-08T15:42:00+00:00")
        lines = self.run_digest({"candidates": [row]})
        refused = [line for line in lines if line.startswith("PRODUCTION_READ_REFUSED")]
        self.assertEqual(len(refused), 1)
        self.assertIn(read, refused[0])
        self.assertIn(routine_check.REFUSED_READ_ROUTE, refused[0])
        self.assertFalse([line for line in lines if line.startswith(("SUPERVISOR_ACTION", "NEXT"))])
        self.assertTrue([line for line in lines if line.startswith("ERROR supervisor_action") and "/190" in line])

    def test_a_refused_read_handed_to_a_named_lead_keeps_its_read_through_the_queue(self) -> None:
        lead = "01a1b839-608e-7a73-b683-bb6c7da113a7"
        read = "timed export profile on owner/app production"
        issue = item(number=5, actor=f"agent:{lead}", action=f"Production read refused: {read}")
        queued = {**issue, "queue": classify(issue)}
        self.assertEqual(queued["queue"]["reason"], "named_recipient_requires_reconciliation")
        lines = self.run_digest({"needs_status": [queued]})
        self.assertTrue([line for line in lines if line.startswith("PRODUCTION_READ_REFUSED") and read in line and f"next_actor=agent:{lead}" in line])
        self.assertFalse([line for line in lines if line.startswith("ERROR")])

    def test_other_denials_and_settled_records_are_not_routed_as_refused_reads(self) -> None:
        cases = {
            "other denial": {"candidates": [queue_row(6, "agent:supervisor", "Helper refused the deploy [Production Reads]; name the next route")]},
            "person owns it": {"candidates": [queue_row(7, "person:owner", "Production read refused: export profile")]},
            "recorded wait": {"candidates": [queue_row(8, "agent:supervisor", "Production read refused: export profile", state="waiting")]},
            "claim still held": {"candidates": [queue_row(9, "agent:supervisor", "Production read refused: export profile", state="underway")]},
        }
        for name, selection in cases.items():
            with self.subTest(name):
                self.assertFalse([line for line in self.run_digest(selection) if line.startswith("PRODUCTION_READ_REFUSED")])
        lines = self.run_digest(cases["other denial"])
        self.assertTrue([line for line in lines if line.startswith("SUPERVISOR_ACTION") and "/6" in line])

    def test_either_harness_native_id_names_the_supervisor(self) -> None:
        for variable in ("CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: "Native-1"}):
                row = queue_row(4, "agent:native-1", "Route it", recorded="2026-11-08T19:40:00+00:00")
                self.assertTrue([line for line in self.run_digest({"candidates": [row]}) if line.startswith("SUPERVISOR_ACTION")])

    def test_seat_rows_reach_only_their_roles_until_two_checks_have_passed(self) -> None:
        fallback_age: timedelta = routine_check.ROUTINE_INTERVAL * 2
        for role in routine_check.SUPERVISOR_ROLES:
            row = queue_row(4031, f"agent:supervisor-{role}", "Take this row", recorded=self.now.isoformat())
            for held in ((), (role,), ("milestone", "tooling"), ("cleanup",)):
                with self.subTest(role=role, held=held):
                    output = routine_check.digest({"next": {"candidates": [row]}}, roles=held)
                    self.assertEqual(row["url"] in output, not held or role in held)
                    if not held or role in held:
                        self.assertIn("SUPERVISOR_ACTION " + row["url"], output)
            for age in (fallback_age, fallback_age + timedelta(seconds=1)):
                row["queue"]["status_recorded_at"] = (self.now - age).isoformat()
                for held in routine_check.SUPERVISOR_ROLES:
                    with self.subTest(role=role, held=held, age=age):
                        output = routine_check.digest({"next": {"needs_status": [row]}}, roles=(held,))
                        self.assertEqual(row["url"] in output, held == role or age > fallback_age)
            row["queue"]["status_recorded_at"] = self.now.isoformat()
            output = routine_check.digest({"next": {"candidates": [row]}}, roles=(role,))
            self.assertNotIn("FALLBACK", output)

    def test_unrouted_and_undated_seat_rows_are_visible_without_inventing_age(self) -> None:
        for actor in ("agent:supervisor", "agent:supervisor-tooling"):
            row = queue_row(4031, actor, "Take this row")
            for role in routine_check.SUPERVISOR_ROLES:
                with self.subTest(actor=actor, role=role):
                    output = routine_check.digest({"next": {"candidates": [row]}}, roles=(role,))
                    self.assertIn("SUPERVISOR_ACTION " + row["url"], output)
                    self.assertIn("UNROUTED" if actor == "agent:supervisor" else "age=UNKNOWN", output)
                    if actor != "agent:supervisor" and role != "tooling":
                        self.assertIn("FALLBACK", output)

    def test_role_prompts_cover_each_seat_and_ignore_unrelated_tables(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "references/routine-check.md").read_text()
        with patch.object(Path, "read_text", return_value=text + "\n| Other | Table |\n"):
            for role in routine_check.SUPERVISOR_ROLES:
                self.assertEqual(len(routine_check.role_prompt((role,))), 1)
        with patch.object(Path, "read_text", side_effect=AssertionError("The old command must not read the role table")):
            routine_check.digest({})

    def test_both_harness_bindings_load_one_rule_source(self) -> None:
        catalog = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location("reconcile_binding", catalog / "skills/github/scripts/reconcile-runtime-checkout.py")
        assert spec is not None and spec.loader is not None
        reconcile = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reconcile)
        with tempfile.TemporaryDirectory() as home:
            codex, claude = Path(home, "code"), Path(home, "claude")
            codex.mkdir()
            (claude / "skills").mkdir(parents=True)
            (codex / "skills").symlink_to(catalog / "skills")
            (claude / "skills/shared").symlink_to(catalog)
            with patch.dict(os.environ, {"CODE_HOME": str(codex), "CLAUDE_CONFIG_DIR": str(claude), "CODEX_HOME": ""}):
                bindings = {label: path for path, label, _ in reconcile.runtime_skills_paths()}
            rule = "supervisor/references/routine-check.md"
            self.assertTrue((bindings["CODE_HOME"] / rule).samefile(bindings["CLAUDE_CONFIG_DIR/skills/shared"] / "skills" / rule))

    def test_client_wait_prints_the_current_head_check_and_flags_a_wrong_wait(self) -> None:
        cases = {
            "preview failed": ("Preview failed: nothing for the Client to review yet", "client", True),
            "healthy review": ("Preview ready for the Client's review", "client", False),
            "director standing": ("Preview ready for the Client's review", "director_standing", True),
        }
        for name, (title, release, wrong) in cases.items():
            with self.subTest(name):
                lines = self.run_digest({"pr_followthrough": [client_wait(376)]},
                                        owner_review=Mock(return_value={"status": "completed", "conclusion": "neutral", "title": title}),
                                        client_release=Mock(return_value=SimpleNamespace(state=release, reason="")))
                row = next(line for line in lines if line.startswith("CLIENT_WAIT"))
                self.assertIn(f'owner-review=completed/neutral "{title}"', row)
                self.assertEqual("WRONG_WAIT" in row, wrong)

    def test_failed_owner_review_read_stays_unknown_and_reports_coverage(self) -> None:
        lines = self.run_digest({"pr_followthrough": [client_wait(5)]}, owner_review=Mock(side_effect=RuntimeError("denied")),
                                client_release=Mock(return_value=SimpleNamespace(state="unknown", reason="no token")))
        row = next(line for line in lines if line.startswith("CLIENT_WAIT"))
        self.assertIn("owner-review=UNKNOWN", row)
        self.assertIn("client_release=UNKNOWN (no token)", row)
        self.assertNotIn("WRONG_WAIT", row)
        self.assertTrue([line for line in lines if line.startswith("ERROR primary")])

    def test_owner_review_reads_the_pull_requests_current_head(self) -> None:
        github = SimpleNamespace(get_json=Mock(side_effect=[{"head": {"sha": "new"}}, {"check_runs": [{"status": "completed", "conclusion": "failure", "output": {"title": "Preview failed"}}]}]))
        args = argparse.Namespace()
        with patch_attr(routine_check.github_read, "GitHubReader", return_value=github), patch_attr(routine_check.github_access, "reader_options", return_value={}):
            read = routine_check.primary_readers(args)["owner_review"]("owner/repo", 7)
        self.assertEqual(read["title"], "Preview failed")
        self.assertIn("commits/new/check-runs", github.get_json.call_args_list[1].args[0])

    def test_release_request_is_checked_against_production(self) -> None:
        request = queue_row(403, "agent:supervisor", "Supervisor requests the gated release", recorded="2026-11-08T19:50:00+00:00")
        contained = {"deployed": "e1521e55" + "0" * 32, "newest": "e1521e55" + "0" * 32, "contained": True, "behind": 0}
        row = next(line for line in self.run_digest({"candidates": [request]}, production=Mock(return_value=contained)) if line.startswith("SUPERVISOR_ACTION"))
        self.assertIn("production=e1521e55", row)
        self.assertIn("NOTHING_TO_RELEASE", row)
        pending = {**contained, "newest": "abcdef12" + "0" * 32, "contained": False, "behind": 3}
        row = next(line for line in self.run_digest({"candidates": [request]}, production=Mock(return_value=pending)) if line.startswith("SUPERVISOR_ACTION"))
        self.assertNotIn("NOTHING_TO_RELEASE", row)
        self.assertIn("not_released=3", row)

    def test_failed_production_read_is_unknown_never_a_release_need_or_healthy(self) -> None:
        request = queue_row(9, "agent", "Run the promotion", recorded="2026-11-08T19:50:00+00:00")
        lines = self.run_digest({"candidates": [request]}, production=Mock(side_effect=RuntimeError("down")))
        row = next(line for line in lines if line.startswith("NEXT"))
        self.assertIn("production=UNKNOWN", row)
        self.assertNotIn("NOTHING_TO_RELEASE", row)
        self.assertTrue([line for line in lines if line.startswith("ERROR primary")])

    def test_production_contains_newest_landed_only_when_github_says_so(self) -> None:
        deployed = "a" * 40
        for status, contained in (("identical", True), ("ahead", True), ("behind", False), ("diverged", False)):
            with self.subTest(status=status):
                github = SimpleNamespace(get_json=Mock(side_effect=[{"default_branch": "main"}, {"sha": "b" * 40}, {"status": status, "behind_by": 2}]))
                acceptance = SimpleNamespace(read_production_commit=Mock(return_value=SimpleNamespace(commit=deployed, reason="")))
                with patch_attr(routine_check.github_read, "GitHubReader", return_value=github), patch_attr(routine_check.github_access, "reader_options", return_value={}), \
                        patch.object(routine_check.importlib, "import_module", return_value=acceptance):
                    self.assertEqual(routine_check.primary_readers(argparse.Namespace())["production"]("owner/repo")["contained"], contained)
                self.assertIn(f"compare/{'b' * 40}...{deployed}", github.get_json.call_args_list[2].args[0])

    def test_claim_release_wording_is_not_a_release_request(self) -> None:
        row = queue_row(10, "agent", "Finish and release the claim", recorded="2026-11-08T19:50:00+00:00")
        production = Mock(return_value={"reason": "unmapped"})
        self.assertFalse([line for line in self.run_digest({"candidates": [row]}, production=production) if "production=" in line])
        production.assert_not_called()

    def test_setting_change_request_is_flagged_for_its_reason_and_acting_service(self) -> None:
        row = queue_row(915, "agent", "Set onboot=1 on the guest so it restarts", recorded="2026-11-08T19:50:00+00:00")
        line = next(line for line in self.run_digest({"candidates": [row]}) if line.startswith("NEXT"))
        self.assertIn("SETTING_UNVERIFIED", line)
        flag = queue_row(11, "agent", "Run the helper with --limit=5 and report", recorded="2026-11-08T19:50:00+00:00")
        self.assertNotIn("SETTING_UNVERIFIED", next(line for line in self.run_digest({"candidates": [flag]}) if line.startswith("NEXT")))

    def test_anchor_status_edit_counts_as_a_response_alongside_comments(self) -> None:
        comment = {"created_at": "2026-11-08T10:00:00Z"}
        edited = routine_check.anchor_note({"body": "## Current Status\nDone", "_graphql": {"lastEditedAt": "2026-11-08T12:00:00Z"}, "comments": [comment]})
        self.assertIn("via=current_status", edited)
        older = routine_check.anchor_note({"body": "## Current Status\nDone", "_graphql": {"lastEditedAt": "2026-11-08T09:00:00Z"}, "comments": [comment]})
        self.assertIn("via=comment", older)
        unrelated = routine_check.anchor_note({"body": "No status here", "_graphql": {"lastEditedAt": "2026-11-08T12:00:00Z"}, "comments": []})
        self.assertEqual(unrelated, "anchor_response=UNKNOWN")
        session = {"session_id": "lead", "action": "inspect_idle_session", "issue": "owner/repo#1"}
        worker = next(line for line in self.run_digest({}, [session], anchor_response=Mock(return_value=edited)) if line.startswith("WORKER"))
        self.assertIn("via=current_status", worker)

    def test_ledger_issue_number_becomes_a_readable_anchor(self) -> None:
        anchor = routine_check.compact_check.session_anchor({"issue": 3946, "repository": "owner/repo"})
        self.assertEqual(anchor, "owner/repo#3946")
        self.assertIsNone(routine_check.compact_check.session_anchor({"issue": 3946}))
        github = SimpleNamespace(deadline_at=None)
        with patch_attr(routine_check.github_read, "GitHubReader", return_value=github), patch_attr(routine_check.github_access, "reader_options", return_value={}), \
                patch_attr(routine_check.github_read, "read_discussion", return_value={"comments": [], "body": ""}) as read:
            routine_check.primary_readers(argparse.Namespace())["anchor_response"](anchor)
        self.assertEqual(read.call_args.args[1:], ("owner/repo", 3946))

    def test_digest_timestamp_comes_from_the_clock_and_names_its_zone(self) -> None:
        header = routine_check.digest({}).splitlines()[0]
        self.assertIn(routine_check.clock_label(self.now), header)
        self.assertRegex(routine_check.clock_label(self.now), r"\d{1,2}:\d{2} [AP]M \S+$")

    def test_wait_rows_show_the_ask_author_and_reply(self) -> None:
        asked = {"url": "https://github.com/owner/repo/issues/120", "waiting_for": "Justin confirms triggers",
                 "ask": {"ask_url": "https://github.com/owner/repo/issues/120#issuecomment-5", "author": "helper-bot", "replied_since": False}}
        missing = {"url": "https://github.com/owner/repo/issues/7", "waiting_for": "Justin builds the guest",
                   "ask": {"ask_url": None, "author": None, "replied_since": None}}
        unknown = {"url": "https://github.com/owner/repo/issues/8", "waiting_for": "Justin reviews",
                   "ask": {"ask_url": "https://github.com/owner/repo/issues/8#issuecomment-6", "author": None, "replied_since": None}}
        event = {"url": "https://github.com/owner/repo/issues/9", "waiting_for": "A free worker slot"}
        lines = self.run_digest({"milestone_summary": [{"milestone": "M", "state": "waiting", "waits": [asked, missing, unknown, event]}]})
        waits = [line for line in lines if line.startswith("WAIT")]
        self.assertIn("by helper-bot replied=no", waits[0])
        self.assertIn("ask=MISSING", waits[1])
        self.assertIn("replied=UNKNOWN", waits[2])
        self.assertNotIn("ask=", waits[3])

    def test_stale_items_repeat_with_their_age_and_evidence(self) -> None:
        stale = {"url": "https://github.com/owner/repo/issues/1", "evidence": [{"kind": "overdue_recheck", "recorded_at": "2026-11-04T00:00:00Z", "matched": "Waiting for proof"}]}
        selection = {"stale_wait_report": {"items": [stale]}}
        for _ in range(2):
            row = next(line for line in self.run_digest(selection) if line.startswith("STALE"))
            self.assertIn("overdue_recheck age=4d19h evidence=Waiting for proof", row)

    def test_every_green_pr_without_a_handoff_repeats_with_its_age_until_handed_off(self) -> None:
        next_pr: Any = importlib.import_module("github_next_pr")
        handoffs: Any = importlib.import_module("ready_handoff")
        head = "e" * 40

        def row(number: int, *bodies: str, **changes: Any) -> dict[str, Any]:
            pull = {"number": number, "html_url": f"https://github.com/owner/repo/pull/{number}", "title": f"Change {number}",
                    "created_at": "2026-11-08T12:00:00Z", "head": {"sha": head, "ref": f"work/{number}"}, **changes}
            comments = [{"id": index, "body": body, "user": {"login": "bot"}, "created_at": "2026-11-08T13:00:00Z"}
                        for index, body in enumerate(bodies, 1)]
            return next_pr.project("owner/repo", pull, comments, trusted={"bot"}, complete=True)

        def checks(state: str) -> dict[str, Any]:
            runs: list[dict[str, Any]] = [] if state == "none" else [{"name": "test", "status": "completed", "completedAt": "2026-11-08T14:55:00Z",
                                                "conclusion": "failure" if state == "failing" else "success"}]
            if state == "client":
                runs.append({"name": routine_check.OWNER_REVIEW_CHECK, "status": "queued", "completedAt": None})
            return {"headSha": "f" * 40 if state == "moved" else head, "checkRuns": runs, "statuses": [], "summary": {
                "countsComplete": state != "partial", "countsAreLowerBounds": state == "partial",
                "unavailableComponents": ["checkRuns"] if state == "partial" else [], "failingCount": int(state == "failing"),
                "pendingCount": int(state == "client"), "unfinishedWorkflowRunCount": 0, "checkRunCount": len(runs), "statusCount": 0}}

        states = {906: "green", 907: "failing", 910: "client", 911: "none", 912: "partial", 915: "moved"}
        rows = [row(906), row(907), row(908, "Engineering ready; waiting for the Director's landing decision"),
                row(909, handoffs.handoff_body(head)), row(910), row(911), row(912), row(913, draft=True), row(914, head={}), row(915)]
        reader = Mock(side_effect=lambda _repo, number: checks(states[number]))
        for _ in range(2):
            lines = self.run_digest({"pr_followthrough": rows}, checks=reader)
            found = {line.split()[1].rsplit("/", 1)[1]: line for line in lines if line.startswith("NO_HANDOFF")}
            self.assertEqual(set(found), {"906", "910", "911"})
            self.assertIn("age=5h00m: Change 906", found["906"])
            self.assertIn("age=7h55m", found["911"])
            self.assertIn("draft", found["906"])
            errors = [line for line in lines if line.startswith("ERROR primary")]
            self.assertTrue([line for line in errors if "#912" in line], errors)
            self.assertTrue([line for line in errors if "/pull/914" in line], errors)
            self.assertTrue([line for line in errors if "#915" in line and "head moved" in line], errors)
        self.assertEqual({call.args[1] for call in reader.call_args_list}, set(states))


if __name__ == "__main__":
    unittest.main()
