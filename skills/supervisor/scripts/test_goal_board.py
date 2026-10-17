#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Replay synthetic GraphQL pages and prove the board never shows an unread status."""

from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import Mock, patch

import goal_board
from skills.github.scripts.github_api import ApiResult

BOT = {"__typename": "Bot", "login": "shiny-code-app"}
TRUSTED = {"owner", "shiny-code-app[bot]"}
HEAD = "a" * 40
OLD = "b" * 40


def comment(body, author=BOT, at="2026-10-16T10:00:00Z", ident=1):
    return {"url": f"https://example.test/c{ident}", "createdAt": at, "updatedAt": at,
            "databaseId": ident, "body": body, "author": author}


def pr(number, state="OPEN", comments=()):
    return {"number": number, "url": f"https://github.com/owner/app/pull/{number}", "state": state,
            "isDraft": False, "mergeable": "MERGEABLE", "headRefOid": HEAD,
            "comments": {"totalCount": len(comments), "nodes": list(comments)}}


def child(number, prs=(), comments=(), total_prs=None):
    return {"number": number, "title": f"Child {number}", "url": f"https://github.com/owner/app/issues/{number}",
            "state": "OPEN", "repository": {"nameWithOwner": "owner/app"},
            "labels": {"totalCount": 2, "nodes": [{"name": "plan"}, {"name": "plan:active"}]},
            "subIssuesSummary": {"total": 0, "completed": 0},
            "comments": {"totalCount": len(comments), "nodes": list(comments)},
            "closedByPullRequestsReferences": {"totalCount": len(prs) if total_prs is None else total_prs,
                                               "nodes": list(prs)}}


def track(number, children=(), comments=(), total_children=None):
    return {"number": number, "title": f"Track: {number}", "url": f"https://github.com/owner/direction/issues/{number}",
            "state": "OPEN", "milestone": {"title": f"Milestone {number}"},
            "labels": {"totalCount": 2, "nodes": [{"name": "plan"}, {"name": "plan:waiting"}]},
            "comments": {"totalCount": len(comments), "nodes": list(comments)},
            "subIssues": {"totalCount": len(children) if total_children is None else total_children,
                          "nodes": list(children)}}


def listing(nodes, total=None, next_cursor=None):
    return {"data": {"repository": {"issues": {"totalCount": len(nodes) if total is None else total,
        "nodes": nodes, "pageInfo": {"hasNextPage": next_cursor is not None, "endCursor": next_cursor}}}}}


def details(**tracks):
    return {"data": {"repository": tracks}}


class GoalBoardTests(unittest.TestCase):
    def setUp(self):
        self.reader = Mock()
        self.reader.diagnostics.return_value = {}

    def replay(self, *bodies):
        self.reader.graphql_json.side_effect = [
            body if isinstance(body, ApiResult) else ApiResult(ok=True, status=200, body=body) for body in bodies]
        return goal_board.goal_board(self.reader, "owner", trusted=TRUSTED)

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
        self.assertEqual(one["status"], "plan:waiting")
        self.assertEqual([(row["url"].rsplit("/", 1)[1], row["handoff"]) for row in one["children"][0]["open_prs"]],
                         [("100", "ready"), ("101", "ready for an older head"),
                          ("103", "engineering ready, waiting"), ("104", "no handoff")])
        self.assertEqual(self.reader.graphql_json.call_args_list[0].args[1]["cursor"], None)
        self.assertEqual(self.reader.graphql_json.call_args_list[1].args[1]["cursor"], "c1")

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
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]),
                            details(t1=track(1, children=[many_labels, child(12, prs=[long_pr])])))
        self.assertFalse(board["coverage"]["complete"])
        rows = board["tracks"][0]["children"]
        self.assertEqual(rows[0]["status"], goal_board.UNKNOWN)
        self.assertEqual(rows[1]["open_prs"][0]["handoff"], goal_board.UNKNOWN)
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]), details(t1=long_track))
        self.assertFalse(board["coverage"]["complete"])
        self.assertIn(f"older decisions {goal_board.UNKNOWN}", goal_board.markdown({**board, "checked_at": "now"}))
        # A decision or handoff inside the window is the latest one, so a long thread stays complete.
        decided = track(1, comments=[comment("Director decision: yes")])
        decided["comments"]["totalCount"] = 145
        ready = pr(100, comments=[comment(f"Ready for the merge train\n\nHead: {HEAD}")])
        ready["comments"]["totalCount"] = 80
        decided["subIssues"] = {"totalCount": 1, "nodes": [child(12, prs=[ready])]}
        board = self.replay(listing([{"number": 1, "title": "Track: One"}]), details(t1=decided))
        self.assertTrue(board["coverage"]["complete"])
        self.assertEqual(board["tracks"][0]["children"][0]["open_prs"][0]["handoff"], "ready")

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


if __name__ == "__main__":
    unittest.main()
