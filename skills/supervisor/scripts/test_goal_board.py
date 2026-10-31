#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Replay synthetic GraphQL pages and prove the board never shows an unread status."""

from contextlib import redirect_stdout
import io
import json
import re
import unittest
from typing import Any
from unittest.mock import Mock, patch

import goal_board
from skills.github.scripts.github_api import ApiResult
from skills.github.scripts import github_plan_body, github_plan_graph, github_plan_wait, github_plan_status, github_plan_claim

BOT = {"__typename": "Bot", "login": "shiny-code-app"}
TRUSTED = {"owner", "shiny-code-app[bot]"}
HEAD = "a" * 40
OLD = "b" * 40


def comment(body, author=BOT, at="2026-10-16T10:00:00Z", ident=1) -> dict[str, Any]:
    return {"url": f"https://example.test/c{ident}", "createdAt": at, "updatedAt": at,
            "databaseId": ident, "body": body, "author": author}


def pr(number, state="OPEN", comments=()) -> dict[str, Any]:
    return {"number": number, "url": f"https://github.com/owner/app/pull/{number}", "state": state,
            "isDraft": False, "mergeable": "MERGEABLE", "headRefOid": HEAD,
            "comments": {"totalCount": len(comments), "nodes": list(comments)}}


def status(action, state="Working"):
    return f"## Current Status\n\nState: {state}\nNext action: {action}\nBlocked by: None\n"


def child(number, prs=(), comments=(), total_prs=None, state="OPEN", label="plan:active", body=None) -> dict[str, Any]:
    return {"number": number, "title": f"Child {number}", "url": f"https://github.com/owner/app/issues/{number}",
            "state": state, "repository": {"nameWithOwner": "owner/app"},
            "body": (status(f"Child {number} step.") if body is None else body) + "\n" +
                    github_plan_wait.marker(github_plan_wait.reviewed(evidence="No person hold remains")),
            "author": BOT, "blockedBy": {"totalCount": 0, "nodes": []},
            "createdAt": "2026-10-01T00:00:00Z", "lastEditedAt": "2026-10-15T00:00:00Z",
            "labels": {"totalCount": 2, "nodes": [{"name": "plan"}, {"name": label}]},
            "subIssuesSummary": {"total": 0, "completed": 0},
            "comments": {"totalCount": len(comments), "nodes": list(comments)},
            "closedByPullRequestsReferences": {"totalCount": len(prs) if total_prs is None else total_prs,
                                               "nodes": list(prs)}}


def track(number, children=(), comments=(), total_children=None) -> dict[str, Any]:
    return {"number": number, "title": f"Track: {number}", "url": f"https://github.com/owner/direction/issues/{number}",
            "state": "OPEN", "repository": {"nameWithOwner": "owner/direction"},
            "milestone": {"title": f"Milestone {number}"},
            "body": status(f"Track {number} step."), "createdAt": "2026-10-01T00:00:00Z", "lastEditedAt": None,
            "author": BOT, "blockedBy": {"totalCount": 0, "nodes": []},
            "labels": {"totalCount": 2, "nodes": [{"name": "plan"}, {"name": "plan:waiting"}]},
            "comments": {"totalCount": len(comments) + 1, "nodes": [comment(github_plan_wait.marker(
                github_plan_wait.reviewed(evidence="Track has no person hold")), at="2026-10-01T00:00:00Z"), *comments]},
            "subIssues": {"totalCount": len(children) if total_children is None else total_children,
                          "nodes": list(children)}}


def listing(nodes, total=None, next_cursor=None) -> dict[str, Any]:
    return {"data": {"repository": {"issues": {"totalCount": len(nodes) if total is None else total,
        "nodes": nodes, "pageInfo": {"hasNextPage": next_cursor is not None, "endCursor": next_cursor}}}}}


def details(**tracks) -> dict[str, Any]:
    return {"data": {"repository": tracks}}


class GoalBoardTests(unittest.TestCase):
    def test_track_proof_stays_visible_without_using_a_recorded_summary(self) -> None:
        node = track(3, children=[child(10)])
        held = github_plan_wait.reviewed(evidence="Track acceptance remains", waiting_for="Client acceptance",
                                        ends_when="Client accepts the result")
        question = comment("Director question: Q2: Choose the result?", ident=53)
        node["comments"]["nodes"].extend([comment(github_plan_wait.marker(held), ident=52), question,
                                         comment("Claimed by historical-worker", ident=55)])
        node["comments"]["totalCount"] = len(node["comments"]["nodes"])
        blocker = {"number": 2, "state": "OPEN", "url": "https://github.com/owner/direction/issues/2",
                   "repository": {"nameWithOwner": "owner/direction"}}
        node["blockedBy"] = {"totalCount": 1, "nodes": [blocker]}
        proof = "The site serves visitors after backup and tested rollback."
        node["body"] = f"## Objective\n{proof}\n\n{status('Wait forever.', 'Waiting')}"
        first = goal_board.track_row(node, TRUSTED, TRUSTED)
        node["body"] = f"## Objective\n{proof}\n"
        second = goal_board.track_row(node, TRUSTED, TRUSTED)
        self.assertEqual(first, second)
        self.assertEqual(second["closure_review"]["track_proof"], proof)
        self.assertEqual(second["waits"], held["waits"])
        self.assertEqual(second["blocked_by"][0]["number"], blocker["number"])
        self.assertEqual(second["open_questions"][0]["url"], question["url"])
        self.assertEqual([row["number"] for row in second["groups"]["available"]], [10])
        self.assertFalse(second["unread"])
        board = {"coverage": {"discovered": 1, "read": 1, "complete": True}, "checked_at": "now",
                 "tracks": [{**second, "read": True}], "errors": []}
        self.assertIn(second["hold_status"], goal_board.markdown(board))


    def test_child_records_group_claims_waits_and_questions_without_track_summary(self) -> None:
        claim = {"worker": "worker", "session": "native-session", "branch": "work/task",
                 "claimed_at": "2026-10-16T10:00:00Z"}
        claimed = comment(github_plan_claim.marker(claim), ident=50)
        question = comment("Director question: Q1: Choose the result?", ident=51)
        held = github_plan_wait.reviewed(evidence="Device result needed", waiting_for="Owner device test",
                                        ends_when="Owner posts the test result")
        held_child = child(11)
        held_child["body"] = github_plan_wait.marker(held)
        rows = [child(10, comments=[claimed]), held_child,
                child(12), child(13, comments=[question])]
        node = track(1, children=rows)
        node["body"] = "## Objective\nPeople use the result.\n"
        board = self.replay(listing([{"number": 1, "title": node["title"]}]), details(t1=node))
        row = board["tracks"][0]
        self.assertEqual({key: [item["number"] for item in values] for key, values in row["groups"].items()},
                         {"running": [10], "waiting": [11, 13], "available": [12], "closed": [], "unknown": [], "held": []})
        self.assertEqual(row["children"][0]["claims"], [claim])
        self.assertEqual(row["children"][3]["open_questions"][0]["url"], question["url"])
        self.assertNotIn("next_action", row)
        self.assertTrue(board["coverage"]["complete"], board)
        text = goal_board.markdown({**board, "checked_at": "now"})
        self.assertIn(held["waits"][0]["ends_when"], text)
        self.assertIn(question["url"], text)

    def test_recursive_children_keep_prs_and_released_claims_become_available(self) -> None:
        claim = {"worker": "worker", "session": "native-session", "branch": "work/task",
                 "claimed_at": "2026-10-16T10:00:00Z"}
        source = comment(github_plan_claim.marker(claim), ident=50)
        release = comment("Released claim 50", at="2026-10-16T11:00:00Z", ident=51)
        parent = child(10, prs=[pr(90)])
        parent["subIssuesSummary"] = {"total": 1, "completed": 0}
        expanded = {**parent, "subIssues": {"totalCount": 1, "nodes": [child(11, comments=[source, release])]}}
        self.reader.graphql_json.side_effect = [ApiResult(ok=True, status=200, body=body) for body in (
            listing([{"number": 1, "title": "Track: One"}]), details(t1=track(1, children=[parent])), details(t10=expanded))]
        board = goal_board.goal_board(self.reader, "owner", trusted=TRUSTED, wait_trusted=TRUSTED)
        row = board["tracks"][0]
        self.assertTrue(board["coverage"]["complete"], board)
        self.assertEqual([item["number"] for item in row["groups"]["waiting"]], [10])
        self.assertEqual([item["number"] for item in row["groups"]["available"]], [11])
        self.assertFalse(row["children"][1]["claims"])
        self.assertEqual(row["children"][0]["open_prs"][0]["url"], parent["closedByPullRequestsReferences"]["nodes"][0]["url"])

    def test_grandchildren_cannot_mask_missing_direct_children(self) -> None:
        parent = child(10)
        parent["subIssuesSummary"]["total"] = 1
        expanded = {**parent, "subIssues": {"totalCount": 1, "nodes": [child(11)]}}
        self.reader.graphql_json.side_effect = [ApiResult(ok=True, status=200, body=body) for body in (
            listing([{"number": 1, "title": "Track: One"}]),
            details(t1=track(1, children=[parent], total_children=2)), details(t10=expanded))]
        board = goal_board.goal_board(self.reader, "owner", trusted=TRUSTED, wait_trusted=TRUSTED)
        self.assertFalse(board["coverage"]["complete"], board)
        self.assertEqual(board["tracks"][0]["status"], goal_board.UNKNOWN)
        self.assertEqual([row["number"] for row in board["tracks"][0]["children"]], [10, 11])

    def test_unread_track_does_not_change_a_readable_tracks_state(self) -> None:
        parent = child(10)
        parent["subIssuesSummary"]["total"] = 1
        self.reader.graphql_json.side_effect = [ApiResult(ok=True, status=200, body=body) for body in (
            listing([{"number": 1, "title": "Track: One"}, {"number": 2, "title": "Track: Two"}]),
            details(t1=track(1, children=[parent])), details(t2=track(2, children=[child(12)])))] + [
            ApiResult(ok=False, status=502, body=None)]
        board = goal_board.goal_board(self.reader, "owner", trusted=TRUSTED, wait_trusted=TRUSTED)
        self.assertFalse(board["coverage"]["complete"])
        self.assertEqual(board["tracks"][0]["status"], goal_board.UNKNOWN)
        self.assertNotEqual(board["tracks"][1]["status"], goal_board.UNKNOWN)
        self.assertEqual([row["number"] for row in board["tracks"][1]["groups"]["available"]], [12])

    def test_invalid_queue_configuration_preserves_tracks_with_incomplete_coverage(self) -> None:
        self.reader.graphql_json.side_effect = [ApiResult(ok=True, status=200, body=body) for body in (
            listing([{"number": 1, "title": "Track: One"}]), details(t1=track(1, children=[child(12)])))]
        with patch.object(goal_board.question_id, "configured_queue", side_effect=ValueError("invalid queue")):
            board = goal_board.goal_board(self.reader, "owner", trusted=TRUSTED, wait_trusted=TRUSTED)
        self.assertFalse(board["coverage"]["complete"])
        self.assertEqual(board["coverage"]["read"], 1)
        self.assertTrue(board["errors"])

    def test_changed_supervisor_release_evidence_preserves_claim_ownership(self) -> None:
        from skills.github.scripts import test_gh_plan_release as release_fixtures
        fixture = release_fixtures.ReleaseHarness()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.f.doCleanups)
        fixture.args.dry_run = True
        fixture.run_release()
        release_body = fixture.f.emitted.call_args.args[0]["release_body"]
        released = fixture.comment(10, release_body, "2026-10-03T00:00:00Z")
        discussion = [*fixture.f.comments, released]
        comments = [comment(source["body"], author={"__typename": "User", "login": source["user"]["login"]},
                            at=source["created_at"], ident=source["id"]) for source in discussion]
        row = child(10, comments=comments)
        row["url"] = "https://github.com/owner/repo/issues/42"
        row["repository"] = {"nameWithOwner": "owner/repo"}
        for valid, closed in ((True, False), (False, False), (False, True)):
            with self.subTest(valid=valid, closed=closed):
                row["state"] = "CLOSED" if closed else "OPEN"
                self.reader.get_json.reset_mock()
                evidence = fixture.evidence if valid else {**fixture.evidence, "updated_at": "2026-10-03T00:00:00Z"}
                self.reader.get_json.return_value = evidence
                board = self.replay(listing([{"number": 1, "title": "Track: One"}]), details(t1=track(1, children=[row])))
                self.assertTrue(board["coverage"]["complete"], board)
                observed = board["tracks"][0]["children"][0]
                self.assertEqual(bool(observed["claims"]), not valid and not closed)
                self.assertEqual(observed["group"], "closed" if closed else "available" if valid else "running")
                if closed:
                    self.reader.get_json.assert_not_called()

    def test_recursive_failure_preserves_siblings_and_reports_unknown(self) -> None:
        parent = child(10)
        parent["subIssuesSummary"]["total"] = 1
        self.reader.graphql_json.side_effect = [ApiResult(ok=True, status=200, body=body) for body in (
            listing([{"number": 1, "title": "Track: One"}]), details(t1=track(1, children=[parent, child(12)])))] + [
            ApiResult(ok=False, status=502, body=None)]
        board = goal_board.goal_board(self.reader, "owner", trusted=TRUSTED, wait_trusted=TRUSTED)
        self.assertFalse(board["coverage"]["complete"])
        self.assertEqual([item["number"] for item in board["tracks"][0]["groups"]["unknown"]], [10])
        self.assertEqual([item["number"] for item in board["tracks"][0]["groups"]["available"]], [12])

    def test_recorded_queue_answers_resolve_child_questions(self) -> None:
        question = comment("Director question: Q1: Choose the result?", ident=51)
        answer = comment(f"Director decision: Q1: Yes. {question['url']}", ident=52)
        root_question = comment("Director question: Q2: Choose the result?", ident=53)
        root_answer = comment(f"Director decision: Q2: Yes. {root_question['url']}", ident=54)
        queue = {"number": 98, "updatedAt": "2026-10-16T12:00:00Z", "repository": {"nameWithOwner": "owner/direction"},
                 "comments": {"nodes": [answer, root_answer], "totalCount": 2, "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        self.reader.graphql_json.side_effect = [ApiResult(ok=True, status=200, body=body) for body in (
            listing([{"number": 1, "title": "Track: One"}]), details(t1=track(1, comments=[root_question], children=[child(10, comments=[question])])),
            {"data": {"repository": {"issue": queue}}})]
        with patch.object(goal_board.question_id, "configured_queue", return_value="owner/direction#98"):
            board = goal_board.goal_board(self.reader, "owner", trusted=TRUSTED, wait_trusted=TRUSTED)
        self.assertTrue(board["coverage"]["complete"], board)
        self.assertFalse(board["tracks"][0]["open_questions"])
        self.assertFalse(board["tracks"][0]["children"][0]["open_questions"])
        self.assertEqual(board["tracks"][0]["children"][0]["group"], "available")

    def test_mixed_case_trusted_people_and_bots_keep_decisions_and_handoffs(self):
        for typename, name in (("User", "MixedCaseOwner"), ("Bot", "MixedCaseApp")):
            with self.subTest(typename=typename):
                author = {"__typename": typename, "login": name}
                trusted = {(name + ("[bot]" if typename == "Bot" else "")).casefold()}
                decision = comment("Director decision (Q1): approve", author=author)
                ready = comment(f"Ready for the merge train\n\nHead: {HEAD}", author=author)
                self.assertEqual(goal_board.latest_decision({"nodes": [decision], "totalCount": 1}, trusted, []),
                                 decision["url"])
                self.assertEqual(goal_board.handoff(pr(1, comments=[ready]), trusted, []), "ready")

    def setUp(self) -> None:
        self.enterContext(patch.object(goal_board.question_id, "configured_queue", return_value=None))
        self.enterContext(patch.object(goal_board.github_plan_claim, "expired_lease_sessions", return_value=set()))

    def __init__(self, *args, **kwargs):
        # unittest builds one instance per test method, so each test gets its own reader.
        super().__init__(*args, **kwargs)
        self.reader = Mock()
        self.reader.diagnostics.return_value = {}

    def replay(self, *bodies):
        responses = iter(bodies)
        detail = None

        def query(text: str, *_args: Any, **_kwargs: Any) -> ApiResult:
            nonlocal detail
            if "WorkGraphTrackList" in text:
                body = next(responses)
            else:
                if detail is None:
                    detail = next(responses)
                body = detail
                if isinstance(body, dict):
                    aliases = re.findall(r"t[0-9]+(?=:)", text)
                    selected = {alias: body["data"]["repository"].get(alias) for alias in aliases}
                    errors = [error for error in body.get("errors", [])
                              if not error.get("path") or any(alias in error["path"] for alias in aliases)]
                    body = {"data": {"repository": selected}, "errors": errors}
            return body if isinstance(body, ApiResult) else ApiResult(ok=True, status=200, body=body)

        self.reader.graphql_json.side_effect = query
        return goal_board.goal_board(self.reader, "owner", trusted=TRUSTED, wait_trusted=TRUSTED)

    def test_closed_legacy_child_does_not_require_an_unused_wait_projection(self):
        row = child(10, state="CLOSED")
        row["body"] = github_plan_wait.without_records(row["body"])
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]), details(t1=track(1, children=[row])))
        observed = board["tracks"][0]["children"][0]
        self.assertTrue(board["coverage"]["complete"], board)
        self.assertFalse(observed["unread"])
        self.assertEqual(observed["status"], github_plan_status.DEFAULT_STATUS_LABELS["done"])

    def test_closed_child_preserves_readable_person_wait_and_native_blocker(self):
        wait = github_plan_wait.reviewed(evidence="Closure did not end the recorded hold",
            waiting_for="Owner acceptance", ends_when="Owner accepts the observed result")
        row = child(10, state="CLOSED", body=github_plan_wait.marker(wait))
        row["body"] = github_plan_wait.marker(wait)
        blocker = {"number": 3, "state": "OPEN", "url": "https://github.com/owner/app/issues/3",
                   "repository": {"nameWithOwner": "owner/app"}}
        row["blockedBy"] = {"totalCount": 1, "nodes": [blocker]}
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]), details(t1=track(1, children=[row])))
        observed = board["tracks"][0]["children"][0]
        self.assertEqual(observed["waits"], wait["waits"])
        self.assertEqual(observed["structured_wait"], wait)
        self.assertEqual(observed["blocked_by"][0]["number"], blocker["number"])
        self.assertEqual(observed["status"], github_plan_status.DEFAULT_STATUS_LABELS["done"])

    def test_native_blockers_and_structured_wait_override_stale_label_and_prose(self):
        clear = github_plan_wait.reviewed(evidence="No person wait remains")
        held = github_plan_wait.reviewed(evidence="Owner requested testing", waiting_for="Owner's test",
                                        ends_when="Owner records acceptance")
        rows = [child(10, label="plan:blocked"), child(11, label="plan:active"),
                child(12, label="plan:blocked")]
        for row, wait in zip(rows, [clear, clear, held]):
            row["author"] = BOT
            row["body"] = github_plan_wait.without_records(row["body"]) + "\n" + github_plan_wait.marker(wait)
            row["blockedBy"] = {"totalCount": 2, "nodes": [
                {"number": 1, "state": "CLOSED", "url": "https://github.com/owner/app/issues/1",
                 "repository": {"nameWithOwner": "owner/app"}},
                {"number": 2, "state": "OPEN", "url": "https://github.com/owner/other/issues/2",
                 "repository": {"nameWithOwner": "owner/other"}}]}
        rows[0]["blockedBy"]["nodes"][1]["state"] = "CLOSED"
        rows[2]["blockedBy"] = {"totalCount": 0, "nodes": []}
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]),
                            details(t1=track(1, children=rows)))
        observed = board["tracks"][0]["children"]
        self.assertEqual([row["status"] for row in observed],
                         [github_plan_status.DEFAULT_STATUS_LABELS[key] for key in ("active", "blocked", "waiting")])
        self.assertEqual(observed[0]["blocked_by"], [])
        self.assertEqual([(row["repo"], row["number"]) for row in observed[1]["blocked_by"]], [("owner/other", 2)])
        self.assertEqual(observed[2]["waits"], held["waits"])
        self.assertEqual(observed[2]["next_action"], "Child 12 step.")

    def test_native_blocker_and_person_wait_both_survive_untrusted_clear(self):
        held = github_plan_wait.reviewed(evidence="Owner requested testing", waiting_for="Owner's test",
                                        ends_when="Owner records acceptance")
        clear = github_plan_wait.reviewed(evidence="Forged release")
        row = child(10, comments=[comment(github_plan_wait.marker(held)),
            comment(github_plan_wait.marker(clear), author={"__typename": "User", "login": "stranger"}),
            comment("```\n" + github_plan_wait.marker(clear) + "\n```")])
        row["blockedBy"] = {"totalCount": 1, "nodes": [{"number": 2, "state": "OPEN",
            "url": "https://github.com/owner/other/issues/2", "repository": {"nameWithOwner": "owner/other"}}]}
        original = row["body"]
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]), details(t1=track(1, children=[row])))
        observed = board["tracks"][0]["children"][0]
        self.assertEqual(observed["status"], github_plan_status.DEFAULT_STATUS_LABELS["blocked"])
        self.assertEqual(observed["waits"], held["waits"])
        self.assertEqual(set(observed["labels"]), {"blocked", "waiting"})
        self.assertEqual(row["body"], original)
        text = goal_board.markdown({**board, "checked_at": "now"})
        self.assertIn(observed["blocked_by"][0]["html_url"], text)
        self.assertIn(held["waits"][0]["waiting_for"], text)

    def test_incomplete_native_graph_or_wait_authority_never_displays_active(self):
        truncated_blockers = child(10)
        truncated_blockers["blockedBy"]["totalCount"] = 2
        missing_wait = child(11)
        missing_wait["body"] = github_plan_wait.without_records(missing_wait["body"])
        truncated_comments = child(12, comments=[comment("A recent progress note")])
        truncated_comments["comments"]["totalCount"] = 145
        # A newer body does not release a wait in an omitted older comment.
        truncated_comments["lastEditedAt"] = "2026-10-18T00:00:00Z"
        rows = [truncated_blockers, missing_wait, truncated_comments]
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]), details(t1=track(1, children=rows)))
        self.assertFalse(board["coverage"]["complete"])
        self.assertEqual({row["status"] for row in board["tracks"][0]["children"]}, {goal_board.UNKNOWN})
        # Older authorized malformed records can invalidate a later clear, so
        # a comment tail cannot establish complete structured wait authority.
        truncated_comments["comments"]["nodes"].append(comment(github_plan_wait.marker(
            github_plan_wait.reviewed(evidence="Full discussion reviewed; hold resolved"))))
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]),
                            details(t1=track(1, children=[truncated_comments])))
        self.assertEqual(board["tracks"][0]["children"][0]["status"], goal_board.UNKNOWN)

    def test_truncated_labels_cannot_hide_a_scope_review_hold(self):
        names = github_plan_status.DEFAULT_STATUS_LABELS
        row = child(10, label=names["active"])
        visible = [{"name": names["plan"]}, {"name": names["active"]},
                   *({"name": f"area:{index}"} for index in range(goal_board.LABEL_LIMIT - 2))]
        # A stale scope hold may be just beyond the bounded label connection,
        # even when an old active label is visible in that connection.
        row["labels"] = {"totalCount": len(visible) + 1, "nodes": visible}
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]),
                            details(t1=track(1, children=[row])))
        self.assertFalse(board["coverage"]["complete"])
        self.assertEqual(board["tracks"][0]["children"][0]["status"], goal_board.UNKNOWN)
        complete_labels = [*visible, {"name": names["stale"]}]
        complete = {**row, "labels": {"totalCount": len(complete_labels), "nodes": complete_labels}}
        observed = goal_board.derived_status(complete, TRUSTED, [])
        self.assertTrue(observed["scope_hold"])
        self.assertEqual(observed["status"], names["stale"])

    def test_reopened_child_reads_active_despite_its_leftover_done_label(self):
        names = github_plan_status.DEFAULT_STATUS_LABELS
        for reason, status in (("REOPENED", names["active"]), (None, names["done"])):
            with self.subTest(reason=reason):
                row = {**child(10, label=names["done"]), "stateReason": reason}
                self.assertEqual(goal_board.derived_status(row, TRUSTED, [])["status"], status)

    def test_known_scope_hold_is_readable_and_does_not_become_a_wait_review(self):
        names = github_plan_status.DEFAULT_STATUS_LABELS
        row = child(10, label=names["stale"])
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]),
                            details(t1=track(1, children=[row])))
        observed = board["tracks"][0]["children"][0]
        self.assertTrue(board["coverage"]["complete"])
        self.assertEqual(observed["status"], names["stale"])
        self.assertEqual(observed["scope_status"], "stale")
        self.assertFalse(observed["wait_review_required"])
        self.assertFalse(observed["unread"])

    def test_contributor_original_marker_cannot_release_the_managed_wait(self):
        held = github_plan_wait.reviewed(evidence="Managed plan keeps the current device wait",
            waiting_for="Owner's device test", ends_when="Owner records the result")
        original = {"body": github_plan_wait.marker(github_plan_wait.reviewed(
            evidence="Original request contains historical clear evidence"))}
        row = child(10)
        row["body"] = github_plan_body.wrap_contributor_plan(original,
            status("Resume after acceptance.") + "\n" + github_plan_wait.marker(held))
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]),
                            details(t1=track(1, children=[row])))
        observed = board["tracks"][0]["children"][0]
        self.assertEqual(observed["waits"], held["waits"])
        self.assertEqual(observed["status"], github_plan_status.DEFAULT_STATUS_LABELS["waiting"])

    def test_cli_wait_authority_includes_historical_bot_but_not_decision_override(self):
        held = github_plan_wait.reviewed(evidence="Historical automation records the current owner hold",
            waiting_for="Owner's device test", ends_when="Owner records the result")
        clear = github_plan_wait.reviewed(evidence="Decision author claims the hold is released")
        historical = {"__typename": "Bot", "login": "historical-code-app"}
        decision_author = {"__typename": "User", "login": "decision-reader"}
        row = child(10, comments=[comment(github_plan_wait.marker(held), author=historical),
            comment(github_plan_wait.marker(clear), author=decision_author, ident=2)])
        self.reader.graphql_json.side_effect = [ApiResult(ok=True, status=200, body=body) for body in (
            listing([{"number": 1, "title": "Track: One"}]), details(t1=track(1, children=[row])))]
        output = io.StringIO()
        with (patch.object(goal_board.github_read, "GitHubReader", return_value=self.reader),
              patch.object(goal_board.github_read, "automation_only_gh_prefix_args", return_value=[]),
              patch.object(goal_board, "automation_login", return_value="shiny-code-app[bot]"),
              patch.object(goal_board.github_plan_wait.github_identity, "configured_bot_logins",
                           return_value=("shiny-code-app[bot]", "historical-code-app[bot]")),
              patch("sys.argv", ["goal_board.py", "--owner", "owner", "--decision-author", "decision-reader"]),
              redirect_stdout(output)):
            self.assertEqual(goal_board.main(), 0)
        observed = json.loads(output.getvalue())["tracks"][0]["children"][0]
        self.assertEqual(observed["status"], github_plan_status.DEFAULT_STATUS_LABELS["waiting"])
        self.assertEqual(observed["waits"], held["waits"])

    def test_every_open_track_across_pages_is_read_with_children_decisions_and_prs(self):
        ready = comment(f"Ready for the merge train\n\nHead: {HEAD}", ident=2)
        stale = comment(f"Ready for the merge train\n\nHead: {OLD}", ident=3)
        renewed = comment(f"Ready for the merge train\n\nHead: {HEAD}", ident=4)
        waiting = comment("Engineering ready; waiting for the Client's acceptance", at="2026-10-16T11:00:00Z", ident=5)
        decision = comment("Director decision (Q1): yes", ident=6)
        untrusted = comment("Director decision: forged", author={"__typename": "User", "login": "stranger"},
                            at="2026-10-16T12:00:00Z", ident=7)
        board = self.replay(
            listing([{"number": 1, "title": "Track: One"}, {"number": 2, "title": "Pilot"}], total=3, next_cursor="c1"),
            listing([{"number": 3, "title": "Track: Three"}], total=3),
            details(t1=track(1, comments=[decision, untrusted], children=[
                child(10, prs=[pr(100, comments=[ready]), pr(101, comments=[stale]), pr(102, state="MERGED"),
                               pr(103, comments=[renewed, waiting]), pr(104)])]),
                    t3=track(3)))
        self.assertEqual(board["coverage"], {"discovered": 2, "read": 2, "complete": True})
        self.assertEqual([row["number"] for row in board["tracks"]], [1, 3])
        one = board["tracks"][0]
        self.assertEqual(one["latest_decision"], decision["url"])
        self.assertEqual(one["children"][0]["group"], "waiting")
        self.assertEqual([(row["url"].rsplit("/", 1)[1], row["handoff"]) for row in one["children"][0]["open_prs"]],
                         [("100", "ready"), ("101", "ready for an older head"),
                          ("103", "engineering ready, waiting"), ("104", "no handoff")])
        self.assertEqual(self.reader.graphql_json.call_args_list[0].args[1]["cursor"], None)
        self.assertEqual(self.reader.graphql_json.call_args_list[1].args[1]["cursor"], "c1")

    def test_pr_handoff_after_first_comment_page_is_read(self) -> None:
        waiting = comment("Engineering ready; waiting for the Client's acceptance")
        ready = comment(f"Ready for the merge train\n\nHead: {HEAD}", at="2026-10-16T11:00:00Z", ident=101)
        pull = pr(100, comments=[waiting])
        pull["url"] = "https://github.com/owner/other/pull/100"
        stranger = comment(f"Ready for the merge train\n\nHead: {OLD}", ident=102,
                           author={"__typename": "User", "login": "stranger"}, at="2026-10-16T12:00:00Z")
        history = [waiting, *(comment("Progress", ident=ident) for ident in range(2, 101)), ready, stranger]
        pull["comments"]["totalCount"] = len(history)
        self.reader.paged_json.return_value = [github_plan_graph.rest_comment(item) for item in history]

        board = self.replay(listing([{"number": 1, "title": "Track: One"}]),
                            details(t1=track(1, children=[child(10, prs=[pull])])))

        observed = board["tracks"][0]["children"][0]["open_prs"][0]
        self.assertEqual(observed["handoff"], "ready")
        self.assertEqual(self.reader.paged_json.call_args.args, ("/repos/owner/other/issues/100/comments",))

    def test_incomplete_or_failed_pr_history_stays_unknown(self) -> None:
        ready = comment(f"Ready for the merge train\n\nHead: {HEAD}")
        for failure in (None, ValueError("missing page")):
            with self.subTest(failure=failure):
                pull = pr(100, comments=[ready])
                pull["comments"]["totalCount"] = 101
                self.reader.paged_json.side_effect = failure
                self.reader.paged_json.return_value = [github_plan_graph.rest_comment(ready)]
                board = self.replay(listing([{"number": 1, "title": "Track: One"}]),
                                    details(t1=track(1, children=[child(10, prs=[pull])])))
                self.assertFalse(board["coverage"]["complete"])
                self.assertEqual(board["tracks"][0]["children"][0]["open_prs"][0]["handoff"], goal_board.UNKNOWN)

    def test_pr_history_failure_preserves_other_prs_and_descendants(self) -> None:
        ready = comment(f"Ready for the merge train\n\nHead: {HEAD}")
        unread_pr = pr(90, comments=[ready])
        unread_pr["comments"]["totalCount"] = 101
        parent = child(10, prs=[unread_pr, pr(91, comments=[ready])])
        parent["subIssuesSummary"] = {"total": 1, "completed": 0}
        expanded = {**parent, "subIssues": {"totalCount": 1, "nodes": [child(11, prs=[pr(92, comments=[ready])])]}}
        self.reader.paged_json.return_value = []
        self.reader.graphql_json.side_effect = [ApiResult(ok=True, status=200, body=body) for body in (
            listing([{"number": 1, "title": "Track: One"}]), details(t1=track(1, children=[parent])), details(t10=expanded))]
        board = goal_board.goal_board(self.reader, "owner", trusted=TRUSTED, wait_trusted=TRUSTED)
        rows = board["tracks"][0]["children"]
        self.assertFalse(board["coverage"]["complete"])
        self.assertEqual([pull["handoff"] for pull in rows[0]["open_prs"]], [goal_board.UNKNOWN, "ready"])
        self.assertEqual(rows[1]["open_prs"][0]["handoff"], "ready")
        self.assertFalse(rows[1]["unread"])

    def test_failed_or_partial_track_read_says_unknown_and_never_complete(self):
        board = self.replay(listing([{"number": 1, "title": "Track: One"}, {"number": 2, "title": "Track: Two"}]),
                            ApiResult(ok=False, status=502, body=None))
        self.assertEqual(board["coverage"], {"discovered": 2, "read": 0, "complete": False})
        self.assertEqual({row["status"] for row in board["tracks"]}, {goal_board.UNKNOWN})
        self.assertTrue(board["errors"])
        partial = self.replay(listing([{"number": 1, "title": "Track: One"}, {"number": 2, "title": "Track: Two"}]),
                              {"data": {"repository": {"t1": track(1), "t2": None}}, "errors": [{"message": "x"}]})
        self.assertEqual([row["read"] for row in partial["tracks"]], [True, False])
        self.assertFalse(partial["coverage"]["complete"])
        for truncated in (track(1, children=[child(10)], total_children=60),
                          track(1, children=[child(10, prs=[pr(100)], total_prs=9)])):
            board = self.replay(listing([{"number": 1, "title": "Track: One"}]), details(t1=truncated))
            self.assertEqual(board["coverage"]["read"], 1)
            self.assertFalse(board["coverage"]["complete"])
            self.assertIn(goal_board.UNKNOWN, goal_board.markdown({**board, "checked_at": "now"}))

    def test_truncated_comments_or_labels_say_unknown_instead_of_a_status(self):
        long_track = track(1, comments=[comment("Progress note")])
        long_track["comments"]["totalCount"] = 145
        many_labels = child(11)
        many_labels["labels"] = {"totalCount": 60, "nodes": [{"name": "area"}]}
        long_pr = pr(100, comments=[comment("CI is green")])
        long_pr["comments"]["totalCount"] = 80
        self.reader.paged_json.return_value = []
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]),
                            details(t1=track(1, children=[many_labels, child(12, prs=[long_pr])])))
        self.assertFalse(board["coverage"]["complete"])
        rows = board["tracks"][0]["children"]
        self.assertEqual(rows[0]["status"], goal_board.UNKNOWN)
        self.assertEqual(rows[1]["open_prs"][0]["handoff"], goal_board.UNKNOWN)
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]), details(t1=long_track))
        self.assertFalse(board["coverage"]["complete"])
        self.assertIn(f"older decisions {goal_board.UNKNOWN}", goal_board.markdown({**board, "checked_at": "now"}))
        # The latest decision/handoff is visible, but omitted wait history stays unknown.
        decided = track(1, comments=[comment("Director decision: yes")])
        decided["comments"]["totalCount"] = 145
        decided["lastEditedAt"] = "2026-10-16T12:00:00Z"
        ready = pr(100, comments=[comment(f"Ready for the merge train\n\nHead: {HEAD}", ident=80)])
        ready["comments"]["totalCount"] = 80
        history = [*(comment("Progress", ident=ident) for ident in range(1, 80)), ready["comments"]["nodes"][0]]
        self.reader.paged_json.return_value = [github_plan_graph.rest_comment(item) for item in history]
        decided["subIssues"] = {"totalCount": 1, "nodes": [child(12, prs=[ready])]}
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]), details(t1=decided))
        self.assertFalse(board["coverage"]["complete"])
        self.assertEqual(board["tracks"][0]["children"][0]["open_prs"][0]["handoff"], "ready")
        self.assertIsNotNone(board["tracks"][0]["latest_decision"])

    def test_unread_field_keeps_sibling_fields_children_and_tracks_readable(self) -> None:
        unread_handoff = pr(101)
        unread_handoff["comments"] = None
        row = child(10, prs=[pr(100), unread_handoff, None])
        row["closedByPullRequestsReferences"]["nodes"][0]["mergeable"] = None
        broken_comments = child(11)
        broken_comments["comments"] = None
        unread_repository = child(13)
        unread_repository["repository"] = None
        board = self.replay(listing([{"number": 1, "title": "Track: One"}, {"number": 2, "title": "Track: Two"}]),
            details(t1=track(1, children=[None, row, broken_comments, child(12), unread_repository]), t2=track(2)))
        self.assertEqual(board["coverage"], {"discovered": 2, "read": 2, "complete": False})
        children = board["tracks"][0]["children"]
        self.assertEqual(children[0]["open_prs"][0]["mergeable"], goal_board.UNKNOWN)
        self.assertEqual(children[0]["open_prs"][0]["handoff"], "no handoff")
        self.assertEqual(children[0]["open_prs"][1]["mergeable"], "mergeable")
        self.assertEqual(children[0]["open_prs"][1]["handoff"], goal_board.UNKNOWN)
        self.assertEqual(children[3]["repository"], goal_board.UNKNOWN)
        self.assertEqual(children[3]["next_action"], "Child 13 step.")
        self.assertEqual(children[0]["next_action"], "Child 10 step.")
        self.assertEqual(children[1]["next_action"], goal_board.UNKNOWN)
        self.assertEqual(children[2]["next_action"], "Child 12 step.")
        text = goal_board.markdown({**board, "checked_at": "now"})
        for observed in [board["tracks"][0], *children]:
            for reason in observed["unread"]:
                self.assertIn(reason, text)
        self.assertFalse(board["tracks"][1]["unread"])

    def test_graphql_errors_name_the_field_and_preserve_readable_tracks(self) -> None:
        broken = track(1)
        broken["subIssues"] = None
        board = self.replay(listing([{"number": 1, "title": "Track: One"}, {"number": 2, "title": "Track: Two"}]),
            {**details(t1=broken, t2=track(2)), "errors": [
                {"path": ["repository", "t1", "subIssues"], "message": "Field unavailable"}]})
        self.assertEqual(board["coverage"], {"discovered": 2, "read": 2, "complete": False})
        self.assertFalse(board["tracks"][1]["unread"])
        text = goal_board.markdown({**board, "checked_at": "now"})
        self.assertIn("repository.t1.subIssues", text)
        self.assertIn("Field unavailable", text)
        self.assertIn(board["tracks"][0]["unread"][0], text)

    def test_http_200_detail_failure_cannot_hide_every_track(self) -> None:
        failure = {"message": "Detail query unavailable"}
        for errors in ([failure], [None], {"message": "Malformed error collection"}):
            with self.subTest(errors=errors):
                self.reader.graphql_json.side_effect = [ApiResult(ok=True, status=200, body=body) for body in (
                    listing([{"number": 1, "title": "Track: One"}, {"number": 2, "title": "Track: Two"}]),
                    {"data": {"repository": None}, "errors": errors},
                    details(t2=track(2)))]
                board = goal_board.goal_board(self.reader, "owner", trusted=TRUSTED, wait_trusted=TRUSTED)
                self.assertEqual(board["coverage"], {"discovered": 2, "read": 1, "complete": False})
                self.assertFalse(board["tracks"][0]["read"])
                self.assertTrue(board["tracks"][1]["read"])
                text = goal_board.markdown({**board, "checked_at": "now"})
                for reason in board["errors"]:
                    self.assertIn(reason, text)

    def test_discovery_failures_refuse_rather_than_return_a_short_board(self):
        for pages in ([ApiResult(ok=False, status=403, body=None)],
                      [listing([{"number": 1, "title": "Track: One"}], total=2)],
                      [listing([{"number": 1, "title": "Track: One"}], total=2, next_cursor="c1"),
                       listing([{"number": 2, "title": "Track: Two"}], total=3)],
                      [listing([{"number": 1, "title": "Track: One"}], total=3, next_cursor="c1"),
                       listing([{"number": 2, "title": "Track: Two"}], total=3, next_cursor="c1")]):
            with self.subTest(pages=pages), self.assertRaises((goal_board.github_read.GitHubReadError, ValueError)):
                self.replay(*pages)

    def test_cli_prints_unknown_board_and_exits_nonzero_when_discovery_fails(self):
        self.reader.graphql_json.side_effect = [ApiResult(ok=False, status=403, body=None)]
        output = io.StringIO()
        with (patch.object(goal_board.github_read, "GitHubReader", return_value=self.reader),
              patch.object(goal_board.github_read, "automation_only_gh_prefix_args", return_value=[]),
              patch.object(goal_board, "automation_login", return_value="shiny-code-app[bot]"),
              patch("sys.argv", ["goal_board.py", "--owner", "owner", "--format", "links"]),
              redirect_stdout(output)):
            self.assertEqual(goal_board.main(), 1)
        self.assertIn(goal_board.UNKNOWN, output.getvalue())
        self.assertIn("Tracks discovered: unknown", output.getvalue())

    def test_links_format_omits_titles(self):
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]), details(t1=track(1, children=[child(10)])))
        text = goal_board.markdown({**board, "checked_at": "now"}, links_only=True)
        self.assertNotIn("Child 10", text)
        self.assertNotIn("Track: 1", text)
        self.assertIn("https://github.com/owner/app/issues/10", text)
        self.assertIn("Child 10", goal_board.markdown({**board, "checked_at": "now"}))

    def test_closed_child_shows_closed_and_notes_a_stale_open_label(self):
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]), details(t1=track(1, children=[
            child(10, state="CLOSED"), child(11, state="CLOSED", label="plan:done"), child(12)])))
        text = goal_board.markdown({**board, "checked_at": "now"}, links_only=True)
        self.assertIn("https://github.com/owner/app/issues/10: closed (label plan:active is stale)", text)
        self.assertIn("https://github.com/owner/app/issues/11: closed", text)
        self.assertNotIn("issues/11: closed (", text)
        self.assertIn("https://github.com/owner/app/issues/12: plan:active", text)

    def test_each_row_carries_its_live_next_action(self):
        newer = comment(status("Comment step."), at="2026-10-16T00:00:00Z", ident=8)
        forged = comment(status("Forged step."), author={"__typename": "User", "login": "stranger"},
                         at="2026-10-17T00:00:00Z", ident=9)
        wordy = child(13, body=status("x" * 300 + "\nsecond line"))
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]), details(t1=track(1, children=[
            child(10), child(11, comments=[newer, forged]), child(12, body="No status here."), wordy,
            child(14, body="## Current Status\n\nState: Done\n")])))
        one = board["tracks"][0]
        self.assertEqual([row["next_action"] for row in one["children"][:3]],
                         ["Child 10 step.", "Comment step.", "no Current Status"])
        self.assertEqual(len(one["children"][3]["next_action"]), goal_board.NEXT_ACTION_CHARS)
        self.assertEqual(one["children"][4]["next_action"], "no Next action in Current Status")
        self.assertTrue(board["coverage"]["complete"])
        text = goal_board.markdown({**board, "checked_at": "now"})
        self.assertIn(one["children"][0]["url"], text)
        self.assertIn(one["children"][0]["status"], text)
        self.assertIn(one["children"][0]["next_action"], text)
        self.assertNotIn("second line", text)
        self.assertNotIn("Child 10 step.", goal_board.markdown({**board, "checked_at": "now"}, links_only=True))

    def test_long_thread_without_a_status_in_the_window_says_next_action_unknown(self):
        window = [comment("Progress note", at="2026-10-16T00:00:00Z")]
        older_body = child(10, comments=window)
        older_body["comments"]["totalCount"] = 145
        older_body["lastEditedAt"] = "2026-10-10T00:00:00Z"
        # GitHub stamps whole seconds, so an unread status comment may share the body's edit time.
        tied_body = child(12, comments=window)
        tied_body["comments"]["totalCount"] = 145
        tied_body["lastEditedAt"] = window[0]["createdAt"]
        no_body = child(11, comments=window, body="No status here.")
        no_body["comments"]["totalCount"] = 145
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]),
                            details(t1=track(1, children=[older_body, no_body, tied_body])))
        self.assertFalse(board["coverage"]["complete"])
        self.assertEqual({row["next_action"] for row in board["tracks"][0]["children"]}, {goal_board.UNKNOWN})
        self.assertIn(f"next action: {goal_board.UNKNOWN}", goal_board.markdown({**board, "checked_at": "now"}, links_only=True))
        # A body edited after the oldest comment read cannot be overridden by an unread older comment.
        newer_body = child(10, comments=window)
        newer_body["comments"]["totalCount"] = 145
        newer_body["lastEditedAt"] = "2026-10-16T12:00:00Z"
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]), details(t1=track(1, children=[newer_body])))
        row = board["tracks"][0]["children"][0]
        self.assertEqual(row["next_action"], "Child 10 step.")
        self.assertEqual(row["status"], goal_board.UNKNOWN)
        self.assertFalse(board["coverage"]["complete"])

        root = track(1, comments=window)
        root["comments"]["totalCount"] = 145
        root_row = goal_board.track_row(root, TRUSTED, TRUSTED)
        self.assertEqual(len(root_row["unread"]), len(set(root_row["unread"])))


if __name__ == "__main__":
    unittest.main()
