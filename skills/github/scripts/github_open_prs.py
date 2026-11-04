#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Cross-repository open-PR discovery; never supplies final write authority."""

from __future__ import annotations

import json
from typing import Any

import github_api
import github_pr_batch as batch
from github_read import (
    GitHubReader,
    RepositoryInventoryUnavailable,
    read_repository_connections,
)
from github_read import GitHubReadError as GitHubReadError

FIELDS = """number title body isDraft createdAt state headRefOid headRefName baseRefName
 headRepository { nameWithOwner } author { __typename login } updatedAt url"""


InventoryUnavailable = RepositoryInventoryUnavailable


def scan(reader: GitHubReader, repositories: list[str]) -> dict[str, list[dict[str, Any]] | Exception]:
    def selection(cursor: str | None) -> str:
        return (
            f"pullRequests(first:20,after:{json.dumps(cursor)},states:OPEN,orderBy:{{field:CREATED_AT,direction:DESC}}) "
            "{ totalCount pageInfo { hasNextPage endCursor } nodes { "
            + FIELDS
            + " "
            + batch.connection("labels", "name").replace("first:100", "first:20")
            + " } }"
        )

    def normalize(repo: str, node: dict[str, Any]) -> dict[str, Any]:
        if node.get("state") != "OPEN":
            raise InventoryUnavailable("invalid open PR")
        labels = batch.complete(reader, repo, node, "labels")
        return {
            "number": node["number"],
            "title": node["title"],
            "body": node["body"],
            "state": "open",
            "draft": node["isDraft"],
            "created_at": node["createdAt"],
            "user": batch.rest_author(node["author"]),
            "labels": labels,
            "html_url": node["url"],
            "head": {
                "sha": node["headRefOid"],
                "ref": node["headRefName"],
                "repo": {"full_name": (node["headRepository"] or {}).get("nameWithOwner")},
            },
            "base": {"ref": node["baseRefName"]},
        }

    return read_repository_connections(
        reader,
        repositories,
        field="pullRequests",
        selection=selection,
        normalize=normalize,
        step="open_prs",
        retry_policy=github_api.RetryPolicy(max_wait_seconds=30, max_attempts=1),
    )
