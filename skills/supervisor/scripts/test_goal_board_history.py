#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import copy
import unittest
from unittest.mock import Mock

import goal_board
from test_goal_board import TRUSTED, comment, details, listing, status, track
from skills.github.scripts.github_api import ApiResult


class TrackHistoryTests(unittest.TestCase):
    def test_track_reads_multiple_comment_pages_and_preserves_unknown_on_failure(self) -> None:
        comments = [comment("Progress", ident=index) for index in range(1, 103)]
        decision = comment("Director decision: Continue the milestone.", ident=103)
        full = track(52, comments=[*comments, decision, comment(status("Continue the milestone."), ident=104)])
        full.update(repository={"nameWithOwner": "owner/direction"}, updatedAt="2026-10-29T10:00:00Z")
        blocker = {"number": 99, "state": "OPEN", "url": "https://github.com/owner/app/issues/99",
                   "repository": {"nameWithOwner": "owner/app"}}
        full["blockedBy"] = {"totalCount": 1, "nodes": [blocker]}
        expected = goal_board.track_row(full, TRUSTED, TRUSTED)
        first = copy.deepcopy(full)
        first["comments"].update(nodes=full["comments"]["nodes"][:100],
                                 pageInfo={"hasNextPage": True, "endCursor": "page2"})
        window = copy.deepcopy(full)
        window["comments"]["nodes"] = full["comments"]["nodes"][-100:]
        tail = {"updatedAt": full["updatedAt"], "comments": {
            "totalCount": full["comments"]["totalCount"], "nodes": full["comments"]["nodes"][100:],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        for complete, result in (
            (True, ApiResult(ok=True, status=200, body={"data": {"repository": {"issue": tail}}})),
            (False, ApiResult(ok=False, status=502, body=None)),
            (False, ApiResult(ok=True, status=200, body={"data": {"repository": {"issue": tail}},
                                               "errors": [{"message": "Unavailable"}]})),
            (False, ApiResult(ok=True, status=200, body={"data": {"repository": {"issue": {
                **tail, "updatedAt": "2026-10-29T11:00:00Z"}}}})),
        ):
            with self.subTest(response=result):
                reader = Mock()
                reader.graphql_json.side_effect = [
                    ApiResult(ok=True, status=200, body=listing([{"number": 52, "title": full["title"]}])),
                    ApiResult(ok=True, status=200, body=details(t52=copy.deepcopy(window))),
                    ApiResult(ok=True, status=200, body={"data": {"repository": {"issue": copy.deepcopy(first)}}}), result]
                board = goal_board.goal_board(reader, "owner", trusted=TRUSTED, wait_trusted=TRUSTED)
                row = board["tracks"][0]
                if complete:
                    self.assertEqual(row["status"], expected["status"])
                    self.assertEqual(row["latest_decision"], decision["url"])
                    self.assertTrue(board["coverage"]["complete"], board)
                else:
                    self.assertEqual(row["status"], goal_board.UNKNOWN)
                    self.assertFalse(board["coverage"]["complete"])
                    self.assertTrue(board["errors"])
                self.assertEqual(row["latest_decision"], expected["latest_decision"])
                self.assertEqual(row["closure_review"], expected["closure_review"])


if __name__ == "__main__":
    unittest.main()
