#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from collections.abc import Callable
from typing import Any

import github_change_feed as feed
import github_read

SINCE = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
UNTIL = SINCE + timedelta(hours=1)


def node(repo: str, number: int, kind: str = "Issue") -> dict[str, Any]:
    return {"__typename": kind, "number": number, "updatedAt": "2026-11-09T12:30:00Z",
            "repository": {"nameWithOwner": repo}}


class Reader:
    def __init__(self, answer: Callable[[str], tuple[int, list[dict[str, Any]]]]) -> None:
        self.answer, self.queries = answer, list[str]()

    def graphql_json(self, _query: str, variables: dict[str, Any], **_: Any) -> SimpleNamespace:
        self.queries.append(variables["q"])
        total, nodes = self.answer(variables["q"])
        start = int(variables["after"] or 0)
        page = nodes[start:start + 100]
        more = start + 100 < len(nodes)
        return SimpleNamespace(ok=True, body={"data": {"search": {
            "issueCount": total, "nodes": page,
            "pageInfo": {"hasNextPage": more, "endCursor": str(start + 100) if more else None}}}})

    def diagnostics(self) -> dict:
        return {}


def test_lists_every_changed_thread_across_pages_as_pointers() -> None:
    nodes = [node("director/a", number) for number in range(1, 151)] + [node("director/b", 7, "PullRequest")]
    result = feed.changed(Reader(lambda _q: (len(nodes), nodes)), ["director"], SINCE, until=UNTIL)
    assert result.complete
    assert len(result.threads) == 151
    assert result.repositories() == {"director/a", "director/b"}
    assert feed.Thread("director/b", 7, "pull_request", "2026-11-09T12:30:00Z") in result.threads


def test_window_starts_before_the_last_look_and_names_every_owner() -> None:
    reader = Reader(lambda _q: (0, []))
    feed.changed(reader, ["director", "client"], SINCE, until=UNTIL)
    assert reader.queries == ["user:director user:client archived:false "
                              "updated:2026-11-09T11:50:00Z..2026-11-09T13:00:00Z sort:updated-asc"]


def test_a_window_over_the_search_cap_is_split_until_each_half_fits() -> None:
    def answer(query: str) -> tuple[int, list[dict]]:
        if "11:50:00Z..2026-11-09T13:00:00Z" in query:
            return feed.SEARCH_RESULT_CAP + 1, []
        return (1, [node("director/a", 1)]) if "..2026-11-09T12:25:00Z" in query else (1, [node("director/a", 2)])

    result = feed.changed(Reader(answer), ["director"], SINCE, until=UNTIL)
    assert result.complete
    assert {thread.number for thread in result.threads} == {1, 2}


def test_unlisted_or_unreadable_threads_make_the_feed_incomplete() -> None:
    short = feed.changed(Reader(lambda _q: (3, [node("director/a", 1)])), ["director"], SINCE, until=UNTIL)
    assert not short.complete and "1 of 3" in (short.reason or "")

    def fail(_query: str) -> tuple[int, list[dict]]:
        raise github_read.GitHubReadShapeError("search unavailable")

    failed = feed.changed(Reader(fail), ["director"], SINCE, until=UNTIL)
    assert not failed.complete and "search unavailable" in (failed.reason or "")


def test_a_burst_too_dense_to_split_is_incomplete() -> None:
    dense = feed.changed(Reader(lambda _q: (feed.SEARCH_RESULT_CAP + 1, [])), ["director"], SINCE, until=UNTIL)
    assert not dense.complete and "more than" in (dense.reason or "")


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
