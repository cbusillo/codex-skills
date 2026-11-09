from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import github_read

SEARCH_INDEX_LAG = timedelta(minutes=10)
SEARCH_RESULT_CAP = 1000
SMALLEST_SPLIT_WINDOW = timedelta(minutes=1)
QUERY = """query($q: String!, $after: String) {
  search(type: ISSUE, query: $q, first: 100, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      __typename
      ... on Issue { number updatedAt repository { nameWithOwner } }
      ... on PullRequest { number updatedAt repository { nameWithOwner } }
    }
  }
}"""


@dataclass(frozen=True)
class Thread:
    repository: str
    number: int
    kind: str
    updated_at: str


@dataclass(frozen=True)
class Feed:
    threads: tuple[Thread, ...]
    complete: bool
    reason: str | None = None

    def repositories(self) -> set[str]:
        return {thread.repository.casefold() for thread in self.threads}


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def changed(reader: github_read.GitHubReader, owners: list[str], since: datetime, *,
            until: datetime | None = None) -> Feed:
    if not owners:
        return Feed((), False, "no owners to search")
    start, end = since - SEARCH_INDEX_LAG, until or datetime.now(UTC)
    found: dict[tuple[str, int], Thread] = {}
    windows = [(start, end)]
    while windows:
        low, high = windows.pop()
        query = " ".join([*(f"user:{owner}" for owner in owners), "archived:false",
                          f"updated:{stamp(low)}..{stamp(high)}", "sort:updated-asc"])
        try:
            total, threads = search(reader, query)
        except (github_read.GitHubReadError, github_read.GitHubReadShapeError, KeyError, TypeError, ValueError) as error:
            return Feed(tuple(found.values()), False, f"change search failed: {error}")
        if total > SEARCH_RESULT_CAP:
            if high - low <= SMALLEST_SPLIT_WINDOW:
                return Feed(tuple(found.values()), False, f"more than {SEARCH_RESULT_CAP} threads changed within {stamp(low)}..{stamp(high)}")
            middle = low + (high - low) / 2
            windows += [(low, middle), (middle, high)]
            continue
        if len(threads) != total:
            return Feed(tuple(found.values()), False, f"change search listed {len(threads)} of {total} threads")
        for thread in threads:
            found[thread.repository.casefold(), thread.number] = thread
    return Feed(tuple(found.values()), True)


def search(reader: github_read.GitHubReader, query: str) -> tuple[int, list[Thread]]:
    threads: list[Thread] = []
    after: str | None = None
    while True:
        result = reader.graphql_json(QUERY, {"q": query, "after": after}, step="change_feed_search")
        if not result.ok:
            raise github_read.GitHubReadError("change search unavailable", result=result, diagnostics=reader.diagnostics())
        page: dict[str, Any] = github_read.graphql_data(result)["search"]
        total = page["issueCount"]
        if total > SEARCH_RESULT_CAP:
            return total, []
        for node in page["nodes"]:
            kind = {"Issue": "issue", "PullRequest": "pull_request"}[node["__typename"]]
            number = node["number"]
            if type(number) is not int or number <= 0:
                raise ValueError("change search returned a thread without a number")
            threads.append(Thread(node["repository"]["nameWithOwner"], number, kind, node["updatedAt"]))
        info = page["pageInfo"]
        if not info["hasNextPage"]:
            return total, threads
        if info["endCursor"] in (None, after):
            raise ValueError("change search cursor did not advance")
        after = info["endCursor"]
