#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import github_api
import github_plan_wait
import github_work_graph as graph

FIXTURE = Path(__file__).parent / "fixtures/planning_graph.json"


def connection(nodes: list[dict[str, Any]], *, total: int | None = None) -> dict[str, Any]:
    count = len(nodes) if total is None else total
    return {
        "nodes": nodes,
        "totalCount": count,
        "pageInfo": {"hasNextPage": count > len(nodes), "endCursor": "next" if count > len(nodes) else None},
    }


def node(number: int) -> dict[str, Any]:
    raw = copy.deepcopy(json.loads(FIXTURE.read_text())["data"]["repository"]["issues"]["nodes"][0])
    raw.update(
        number=number,
        title=f"Work {number}",
        state="OPEN",
        stateReason=None,
        body="## Current Status\nNext action: Implement the requested work.\n"
        + github_plan_wait.marker(github_plan_wait.reviewed(evidence="Fixture reviewed without holds")),
        url=f"https://github.com/owner/repo/issues/{number}",
        author={"login": "owner"},
        milestone=None,
    )
    for name in graph.github_plan_graph.CONNECTIONS:
        raw[name] = connection([])
    raw["labels"] = connection([{"name": "plan"}, {"name": "plan:active"}])
    return raw


def replay(nodes: list[dict[str, Any]]) -> Mock:
    reader = Mock(actor="automation-gh")
    reader.results = []
    lookup = {row["number"]: row for row in nodes}

    def query(text: str, _variables: dict[str, Any], **_kwargs: Any) -> github_api.ApiResult:
        numbers = [int(value) for value in re.findall(r"issue\(number:(\d+)\)", text)]
        return github_api.ApiResult(
            ok=True,
            status=200,
            body={
                "data": {
                    f"r{index}": {
                        "issue": {
                            key: copy.deepcopy(value)
                            for key, value in lookup[number].items()
                            if key in set(re.findall(r"\b[A-Za-z_][A-Za-z_0-9]*\b", text))
                        }
                    }
                    for index, number in enumerate(numbers)
                }
            },
        )

    reader.graphql_json.side_effect = query
    return reader


def unavailable(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("An observed record must not be read again")


def cached_issue(reads: graph.github_next_reads.NextReads, number: int = 1, repo: str = "owner/repo") -> dict[str, Any]:
    return reads.observe(graph.issue_path(repo, number), unavailable)[1]


def evaluate(row: dict[str, Any], *, limit: int = 100, read_comments: Any = unavailable) -> dict[str, Any]:
    return graph.read_direction_node(
        row,
        config={"labels": graph.github_plan_graph.github_plan_status.DEFAULT_STATUS_LABELS},
        focus=None,
        comment_limit=limit,
        read_comments=read_comments,
        read_relationships=lambda: graph.read_relationships(row["repo"], row["number"], row, unavailable),
        resolve_wait=lambda comments: github_plan_wait.current(row, comments, {"owner"}),
    )["item"]


class WorkGraphTests(unittest.TestCase):
    def test_batches_publish_native_issue_discussion_and_complete_relationship_pages(self) -> None:
        import math

        nodes = [node(number) for number in range(1, 13)]
        reader = replay(nodes)
        refs = [{"repo": "owner/repo", "number": row["number"]} for row in nodes]
        with graph.github_next_reads.NextReads(workers=4, seconds=0) as reads:
            for offset in range(0, len(refs), graph.TRACKS_PER_QUERY):
                graph.prefetch_issues(lambda _repo: reader, refs[offset : offset + graph.TRACKS_PER_QUERY], reads)
            for ref in refs:
                item = evaluate(cached_issue(reads, ref["number"]))
                self.assertNotIn("exclusion", item)
                self.assertTrue(item["discussion"]["complete"])
            self.assertEqual(reader.graphql_json.call_count, math.ceil(len(refs) / graph.TRACKS_PER_QUERY))
            row = cached_issue(reads)
            row["title"] = "Caller annotation"
            self.assertNotEqual(cached_issue(reads)["title"], row["title"])
            self.assertEqual(reads.observe("/repos/owner/repo/issues/1/comments?per_page=100&page=1", unavailable)[1], [])
            reads.drain()

    def test_partial_labels_relationships_and_wait_history_never_activate_work(self) -> None:
        from itertools import product

        for field, incomplete in product(("labels", "blockedBy", "subIssues", "comments"), ("count", "cursor")):
            with self.subTest(field=field, incomplete=incomplete):
                raw = node(1)
                if field in {"blockedBy", "subIssues"}:
                    raw[field] = connection(
                        [
                            {
                                "number": 2,
                                "title": "Related",
                                "state": "OPEN",
                                "url": "https://github.com/owner/repo/issues/2",
                                "repository": {"nameWithOwner": "owner/repo"},
                            }
                        ]
                    )
                if field == "comments":
                    raw[field] = connection(
                        [
                            {
                                "databaseId": 1,
                                "body": "Progress",
                                "createdAt": raw["createdAt"],
                                "updatedAt": raw["updatedAt"],
                                "url": raw["url"] + "#issuecomment-1",
                                "author": {"login": "owner"},
                            }
                        ]
                    )
                raw[field]["totalCount"] += incomplete == "count"
                raw[field]["pageInfo"] = {"hasNextPage": True, "endCursor": "unread"}
                reader = replay([raw])
                with graph.github_next_reads.NextReads(workers=2, seconds=0) as reads:
                    graph.prefetch_issues(lambda _repo, reader=reader: reader, [{"repo": "owner/repo", "number": 1}], reads)
                    row = cached_issue(reads)
                    item = evaluate(row, limit=1)
                    self.assertEqual(item["exclusion"], "wait_review_required" if field == "comments" else "unknown_dependencies")
                    if field == "comments":
                        self.assertFalse(item["discussion"]["complete"])
                        self.assertIsNone(reads.observation("/repos/owner/repo/issues/1/comments?per_page=100&page=1"))
                    reads.drain()

    def test_failed_batches_are_reused_and_exposed_to_drain_without_replaying(self) -> None:
        reader = Mock(actor="automation-gh")
        reader.graphql_json.return_value = github_api.ApiResult(ok=False, status=403, body=None)
        with graph.github_next_reads.NextReads(workers=2, seconds=0) as reads:
            graph.prefetch_issues(lambda _repo: reader, [{"repo": "owner/repo", "number": 1}], reads)
            for _ in range(2):
                with self.assertRaises(graph.github_read.GitHubReadError):
                    cached_issue(reads)
            graph.prefetch_issues(lambda _repo: reader, [{"repo": "owner/repo", "number": 1}], reads)
            failures = []
            reads.drain(unused_failure=failures.append)
            self.assertEqual(len(failures), 1)
            self.assertEqual(reader.graphql_json.call_count, 1)

    def test_an_unread_alias_preserves_readable_work_in_the_same_batch(self) -> None:
        reader = replay([node(1)])
        query = reader.graphql_json.side_effect

        def partial(*args: Any, **kwargs: Any) -> github_api.ApiResult:
            result = query(*args, **kwargs)
            result.body["data"]["r1"] = None
            result.body["errors"] = [{"message": "Unavailable", "path": ["r1"]}]
            return result

        reader.graphql_json.side_effect = partial
        with graph.github_next_reads.NextReads(workers=2, seconds=0) as reads:
            graph.prefetch_issues(
                lambda _repo: reader, [{"repo": "owner/repo", "number": 1}, {"repo": "owner/private", "number": 1}], reads
            )
            self.assertNotIn("exclusion", evaluate(cached_issue(reads)))
            with self.assertRaises(graph.github_read.GitHubReadShapeError):
                cached_issue(reads, repo="owner/private")
            reads.drain()
            self.assertEqual(reader.graphql_json.call_count, 1)

    def test_larger_allowance_uses_an_explicit_sentinel_and_keeps_incomplete_history_unknown(self) -> None:
        raw = node(1)
        comments = [
            {
                "databaseId": number,
                "body": "Progress",
                "createdAt": raw["createdAt"],
                "updatedAt": raw["updatedAt"],
                "url": raw["url"] + f"#issuecomment-{number}",
                "author": None,
            }
            for number in range(1, 103)
        ]
        raw["comments"] = connection(comments[:100], total=len(comments))
        row = graph.read_issues(replay([raw]), [{"repo": "owner/repo", "number": 1}]).rows[0]
        restored = [
            {
                "id": value["databaseId"],
                "body": value["body"],
                "created_at": value["createdAt"],
                "updated_at": value["updatedAt"],
                "html_url": value["url"],
                "user": None,
            }
            for value in comments
        ]
        allowance = 150
        read_comments = Mock(return_value=(restored, True))
        item = evaluate(row, limit=allowance, read_comments=read_comments)
        self.assertNotIn("exclusion", item)
        self.assertTrue(item["discussion"]["complete"])
        self.assertEqual(len(item["discussion"]["comments"]), len(comments))
        read_comments.assert_called_once_with(allowance + 1)
        self.assertEqual(item["discussion"]["comments"][0]["author"], graph.github_plan_graph.rest_author(None)["login"])
        held = evaluate(row, limit=allowance, read_comments=Mock(return_value=(restored[:1], False)))
        self.assertEqual(held["exclusion"], "wait_review_required")
        self.assertFalse(held["discussion"]["complete"])

    def test_selected_closure_and_edit_metadata_survives_the_graph_read(self) -> None:
        raw = node(1)
        raw["closedAt"] = "2026-10-20T12:00:00Z"
        raw["lastEditedAt"] = "2026-10-19T12:00:00Z"
        row = graph.read_issues(replay([raw]), [{"repo": "owner/repo", "number": 1}]).rows[0]
        self.assertEqual(row["closed_at"], raw["closedAt"])
        self.assertEqual(row["last_edited_at"], raw["lastEditedAt"])

    def test_malformed_alias_uses_the_shared_unread_response_error(self) -> None:
        raw = node(1)
        del raw["authorAssociation"]
        reader = replay([raw])
        with graph.github_next_reads.NextReads(workers=2, seconds=0) as reads:
            graph.prefetch_issues(lambda _repo: reader, [{"repo": "owner/repo", "number": 1}], reads)
            with self.assertRaises(graph.github_read.GitHubReadShapeError):
                cached_issue(reads)
            reads.drain()

    def test_concurrent_consumers_share_the_reserved_rest_future(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        entered, release = Event(), Event()
        reader = replay([node(1)])
        query = reader.graphql_json.side_effect

        def blocked(*args: Any, **kwargs: Any) -> github_api.ApiResult:
            entered.set()
            if not release.wait(3):
                raise AssertionError("Batch consumer did not release the transport")
            return query(*args, **kwargs)

        reader.graphql_json.side_effect = blocked
        with graph.github_next_reads.NextReads(workers=2, seconds=0) as reads, ThreadPoolExecutor(max_workers=1) as consumers:
            graph.prefetch_issues(lambda _repo: reader, [{"repo": "owner/repo", "number": 1}], reads)
            self.assertTrue(entered.wait(3))
            try:
                graph.prefetch_issues(lambda _repo: reader, [{"repo": "OWNER/REPO", "number": 1}], reads)
                consumer = consumers.submit(reads.observe, "/repos/OWNER/REPO/issues/1", unavailable)
            finally:
                release.set()
            self.assertEqual(consumer.result(timeout=3)[1]["number"], 1)
            reads.drain()
            self.assertEqual(reader.graphql_json.call_count, 1)

    def test_cancelled_or_unsubmitted_batches_settle_every_reserved_issue(self) -> None:
        from concurrent.futures import CancelledError
        from threading import Event
        from unittest.mock import patch

        reader = replay([node(1)])
        with graph.github_next_reads.NextReads(workers=1, seconds=0) as reads:
            with patch.object(reads, "submit", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    graph.prefetch_issues(lambda _repo: reader, [{"repo": "owner/repo", "number": 1}], reads)
            self.assertTrue(reads.observation(graph.issue_path("owner/repo", 1)).done())
            with self.assertRaises(KeyboardInterrupt):
                cached_issue(reads)
        entered, release = Event(), Event()
        with graph.github_next_reads.NextReads(workers=1, seconds=0) as reads:

            def block() -> None:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("Blocked task was not released")

            reads.submit("blocked", block)
            self.assertTrue(entered.wait(3))
            try:
                graph.prefetch_issues(lambda _repo: reader, [{"repo": "owner/repo", "number": 1}], reads)
                job = reads.tasks[("work_graph_batch", graph.issue_path("owner/repo", 1))]
                self.assertTrue(job.cancel())
                with self.assertRaises(CancelledError):
                    cached_issue(reads)
            finally:
                release.set()
            reads.drain()
            reader.graphql_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
