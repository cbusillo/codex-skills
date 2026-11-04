#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import copy
import unittest
from typing import Any
from unittest.mock import Mock

import github_read
from github_api import ApiResult, FailureDetail
from github_read import GitHubReadError, GitHubReadShapeError


def comment(ident: int, kind: str = "IssueComment", **fields: Any) -> dict[str, Any]:
    return {
        "__typename": kind,
        "id": f"node-{ident}",
        "databaseId": ident,
        "body": f"message {ident}",
        "createdAt": "2026-11-01T00:00:00Z",
        "updatedAt": "2026-11-01T01:00:00Z",
        "lastEditedAt": "2026-11-01T01:00:00Z",
        "editor": {"login": "person"},
        "url": f"https://github.com/example/repo/issues/7#issuecomment-{ident}",
        "author": {"__typename": "User", "login": "person"},
        "reactions": connection(
            [{"id": "reaction", "content": "EYES", "user": {"login": "owner"}, "createdAt": "2026-11-01T02:00:00Z"}]
        ),
        **fields,
    }


def connection(values: list[dict[str, Any]], cursor: str | None = None, total: int | None = None) -> dict[str, Any]:
    return {
        "nodes": values,
        "totalCount": len(values) if total is None else total,
        "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
    }


def thread(kind: str = "Issue", **fields: Any) -> dict[str, Any]:
    return comment(
        7, kind, **{"number": 7, "title": "Work", "state": "OPEN", "closedAt": None, "comments": connection([comment(8)]), **fields}
    )


def response(data: dict[str, Any], **fields: Any) -> ApiResult:
    return ApiResult(ok=True, status=200, body={"data": data, **fields})


class DiscussionTests(unittest.TestCase):
    def test_worker_reviews_include_every_inline_location_and_submission(self) -> None:
        inline = comment(10, "PullRequestReviewComment", authorAssociation="CONTRIBUTOR", path="app.py", line=None, originalLine=12)
        review = comment(
            9,
            "PullRequestReview",
            authorAssociation="CONTRIBUTOR",
            state="CHANGES_REQUESTED",
            submittedAt="2026-11-01T02:00:00Z",
            comments=connection([inline], "inline", 2),
        )
        reader = Mock(deadline_at=None)
        another = dict(
            review, id="review-20", databaseId=20, comments=connection([dict(inline, id="node-11", databaseId=11, line=14)])
        )
        reader.graphql_json.side_effect = [
            response(
                {
                    "repository": {
                        "pullRequest": {"id": "pull", "__typename": "PullRequest", "reviews": connection([review, another])}
                    }
                }
            ),
            response({"node": {"comments": connection([dict(inline, id="node-13", databaseId=13, line=15)], total=2)}}),
        ]
        result = github_read.read_pull_request_reviews(reader, "example/repo", 7)
        self.assertEqual(result["reviews"][0]["submitted_at"], review["submittedAt"])
        self.assertEqual(
            [(row["id"], row["line"], row["original_line"]) for row in result["review_comments"]],
            [(10, None, 12), (11, 14, 12), (13, 15, 12)],
        )
        self.assertEqual(result["review_comments"][0]["author_association"], inline["authorAssociation"])
        self.assertEqual(reader.graphql_json.call_count, 2)
        reader.graphql_json.side_effect = [response({"repository": {"pullRequest": None}}, errors=[{"message": "partial"}])]
        with self.assertRaises(GitHubReadShapeError):
            github_read.read_pull_request_reviews(reader, "example/repo", 7)

    def test_thread_comments_authors_edits_and_reactions_share_one_query(self) -> None:
        reader = Mock(deadline_at=None)
        reader.graphql_json.return_value = response({"repository": {"issueOrPullRequest": thread()}})
        result = github_read.read_discussion(reader, "example/repo", 7)
        reader.graphql_json.assert_called_once()
        [message] = result["comments"]
        self.assertEqual(message["id"], 8)
        self.assertEqual(message["user"]["login"], "person")
        self.assertEqual(message["updated_at"], "2026-11-01T01:00:00Z")
        self.assertEqual(message["_graphql"]["reactions"]["nodes"][0]["content"], "EYES")
        self.assertEqual(result["state"], "open")

    def test_pages_include_every_comment_review_and_inline_reply(self) -> None:
        review = comment(
            9,
            "PullRequestReview",
            submittedAt="2026-11-01T00:00:00Z",
            comments=connection([comment(10, "PullRequestReviewComment", replyTo={"databaseId": 3})], "inline", 2),
        )
        another = comment(12, "PullRequestReview", comments=connection([]))
        node = thread(
            "PullRequest", databaseId=None, state="MERGED", reviews=connection([review], "reviews", 2), comments=connection([comment(8)], "next", 2)
        )
        reader = Mock(deadline_at=None)
        reader.graphql_json.side_effect = [
            response({"repository": {"issueOrPullRequest": node}}),
            response({"node": {"comments": connection([comment(11)], total=2)}}),
            response({"node": {"reviews": connection([another], total=2)}}),
            response({"node": {"comments": connection([comment(13, "PullRequestReviewComment")], total=2)}}),
        ]
        result = github_read.read_discussion(reader, "example/repo", 7, issue_id=17)
        self.assertEqual(result["id"], 17)
        self.assertEqual([row["id"] for row in result["comments"]], [8, 11])
        self.assertEqual([row["id"] for row in result["reviews"]], [9, 12])
        self.assertEqual(result["review_comments"][0]["in_reply_to_id"], 3)
        self.assertEqual([row["id"] for row in result["review_comments"]], [10, 13])
        self.assertEqual(result["state"], "closed")
        self.assertEqual(reader.graphql_json.call_count, 4)

    def test_partial_denied_and_stalled_reads_preserve_failure(self) -> None:
        denied = ApiResult(
            ok=False,
            status=403,
            body=None,
            failure=FailureDetail(
                cause="forbidden", message="read refused", retryable=False, fallback_eligible=False, disposition="stop"
            ),
        )
        cases = [
            [response({"repository": {"issueOrPullRequest": thread(databaseId=None)}})],
            [response({"repository": {"issueOrPullRequest": thread(comments=connection([comment(8, databaseId=None)]))}})],
            [response({"repository": {"issueOrPullRequest": thread("PullRequest", reviews=connection([]))}})],
            [denied],
            [response({"repository": {"issueOrPullRequest": thread()}}, errors=[{"message": "partial"}])],
            [
                response({"repository": {"issueOrPullRequest": thread(comments=connection([comment(8)], "same", 3))}}),
                response({"node": {"comments": connection([comment(8)], "same", 3)}}),
            ],
            [response({"repository": {"issueOrPullRequest": thread(comments=connection([comment(8)], total=2))}})],
        ]
        for replies in cases:
            with self.subTest(replies=replies):
                reader = Mock(deadline_at=None)
                reader.get_json.return_value = {"id": None}
                reader.graphql_json.side_effect = copy.deepcopy(replies)
                with self.assertRaises((GitHubReadError, GitHubReadShapeError)):
                    github_read.read_discussion(reader, "example/repo", 7)


if __name__ == "__main__":
    unittest.main()
