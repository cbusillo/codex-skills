#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Explicit, complete planning GraphQL reads using the shared reader transport."""

from __future__ import annotations

import json
import copy
from typing import Any

import github_read
import github_plan_status

ISSUE_FIELDS = """number title body state stateReason createdAt updatedAt url
 author { __typename login } authorAssociation repository { nameWithOwner }
 milestone { number title state createdAt dueOn url }
 issueDependenciesSummary { blockedBy blocking totalBlockedBy totalBlocking }
 subIssuesSummary { total completed percentCompleted }"""
RELATED_FIELDS = "number title state url repository { nameWithOwner }"
CONNECTIONS = {
    "labels": "name",
    "blockedBy": RELATED_FIELDS,
    "blocking": RELATED_FIELDS,
    "subIssues": RELATED_FIELDS,
    "comments": "databaseId body createdAt updatedAt url author { __typename login }",
}


def connection_page(value: Any) -> tuple[list[dict[str, Any]], str | None, int]:
    if not isinstance(value, dict):
        raise github_read.GitHubReadShapeError("missing GraphQL connection")
    nodes, info, total = (
        value.get("nodes"),
        value.get("pageInfo"),
        value.get("totalCount"),
    )
    if (
        not isinstance(nodes, list)
        or any(not isinstance(n, dict) for n in nodes)
        or type(total) is not int
        or total < 0
        or not isinstance(info, dict)
        or type(info.get("hasNextPage")) is not bool
    ):
        raise github_read.GitHubReadShapeError("invalid GraphQL connection")
    cursor = info.get("endCursor") if info["hasNextPage"] else None
    if info["hasNextPage"] and (not nodes or not isinstance(cursor, str) or not cursor):
        raise github_read.GitHubReadShapeError("GraphQL cursor did not advance")
    return nodes, cursor, total


def query_data(
    reader: github_read.GitHubReader, query: str, *, step: str
) -> dict[str, Any]:
    # Do not spend the reset window waiting on this optional enrichment. REST
    # recovery uses the same reader identity and inherited operation deadline.
    result = reader.graphql_json(
        query,
        {},
        step=step,
        retry_policy=github_read.github_api_core.RetryPolicy(
            max_wait_seconds=30, max_attempts=1
        ),
    )
    if not result.ok:
        raise github_read.GitHubReadError(
            "planning GraphQL read unavailable",
            result=result,
            diagnostics=reader.diagnostics(),
        )
    body = result.body
    if (
        not isinstance(body, dict)
        or body.get("errors")
        or not isinstance(body.get("data"), dict)
    ):
        raise github_read.GitHubReadShapeError(
            "GraphQL returned partial or missing data"
        )
    return body["data"]


def connection_query(name: str, cursor: str | None = None) -> str:
    ordering = (
        ", orderBy:{field:CREATED_AT, direction:ASC}"
        if name in {"blockedBy", "blocking"}
        else ""
    )
    return (
        f"{name}(first:100, after:{json.dumps(cursor)}{ordering}) {{ totalCount "
        f"nodes {{ {CONNECTIONS[name]} }} pageInfo {{ hasNextPage endCursor }} }}"
    )


def complete_issue(
    reader: github_read.GitHubReader, node: dict[str, Any], *, comments: bool
) -> dict[str, Any]:
    names = [name for name in CONNECTIONS if comments or name != "comments"]
    repo = node["repository"]["nameWithOwner"]
    number = node["number"]
    owner, name = repo.split("/", 1)
    for field in names:
        nodes, cursor, total = connection_page(node.get(field))
        seen = set()
        pages = 1
        while cursor is not None:
            if cursor in seen or pages >= 100:
                raise github_read.GitHubReadShapeError(
                    "GraphQL pagination exceeded bound or repeated cursor"
                )
            seen.add(cursor)
            query = (
                f"query {{ repository(owner:{json.dumps(owner)}, name:{json.dumps(name)}) "
                f"{{ issue(number:{number}) {{ updatedAt {connection_query(field, cursor)} }} }} rateLimit {{ cost remaining resetAt }} }}"
            )
            current = query_data(
                reader, query, step=f"plan_{number}_{field}_{pages + 1}"
            )["repository"]["issue"]
            if current["updatedAt"] != node["updatedAt"]:
                raise github_read.GitHubReadShapeError(
                    "issue changed during GraphQL pagination"
                )
            more, cursor, page_total = connection_page(current.get(field))
            if page_total != total:
                raise github_read.GitHubReadShapeError(
                    "GraphQL connection changed during pagination"
                )
            nodes.extend(more)
            pages += 1
        if len(nodes) != total:
            raise github_read.GitHubReadShapeError("GraphQL connection incomplete")
        node[field] = nodes
    return node


def rest_author(author: dict[str, Any] | None) -> dict[str, str]:
    if author is None:
        return {"login": "ghost", "type": "User"}
    return github_plan_status.normalize_author({"author": author})


def rest_related_issue(node: dict[str, Any]) -> dict[str, Any]:
    return {"repo": node["repository"]["nameWithOwner"], "number": node["number"],
        "title": node.get("title"), "state": node["state"].lower(), "html_url": node["url"]}


def rest_comment(node: dict[str, Any]) -> dict[str, Any]:
    return {"id": node["databaseId"], "body": node["body"], "created_at": node["createdAt"],
        "updated_at": node["updatedAt"], "html_url": node["url"], "user": rest_author(node["author"])}


def rest_issue(node: dict[str, Any]) -> dict[str, Any]:
    # Keep the planner's single representation and selection implementation.
    deps, sub = node["issueDependenciesSummary"], node["subIssuesSummary"]
    milestone = node["milestone"]
    if milestone:
        milestone = {
            **milestone,
            "state": milestone["state"].lower(),
            "created_at": milestone["createdAt"],
            "due_on": milestone["dueOn"],
            "html_url": milestone["url"],
        }
    return {
        "repo": node["repository"]["nameWithOwner"],
        "number": node["number"],
        "title": node["title"],
        "body": node["body"],
        "state": node["state"].lower(),
        "state_reason": node["stateReason"].lower() if node["stateReason"] else None,
        "created_at": node["createdAt"],
        "updated_at": node["updatedAt"],
        "html_url": node["url"],
        "user": rest_author(node["author"]),
        "author_association": node["authorAssociation"],
        "labels": node["labels"],
        "milestone": milestone,
        "issue_dependencies_summary": {
            "blocked_by": deps["blockedBy"],
            "blocking": deps["blocking"],
            "total_blocked_by": deps["totalBlockedBy"],
            "total_blocking": deps["totalBlocking"],
        },
        "sub_issues_summary": {
            "total": sub["total"],
            "completed": sub["completed"],
            "percent_completed": sub["percentCompleted"],
        },
        "_graph_relationships": {
            key: [rest_related_issue(n) for n in node[field]]
            for key, field in [
                ("blocked_by", "blockedBy"),
                ("blocking", "blocking"),
                ("sub_issues", "subIssues"),
            ]
        },
        **({"_graph_comments": [
            rest_comment(n)
            for n in node.get("comments", [])
        ]} if "comments" in node else {}),
    }


def planning_graph(
    reader: github_read.GitHubReader,
    repo: str,
    *,
    number: int | None = None,
    labels: list[str] | None = None,
    milestone: int | None = None,
    limit: int = 1001,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    if not 1 <= page_size <= 100 or limit < 1:
        raise ValueError("invalid planning graph bounds")
    owner, name = repo.split("/", 1)
    fields = (
        ISSUE_FIELDS
        + " "
        + " ".join(
            connection_query(field)
            for field in CONNECTIONS
            if number is not None or field != "comments"
        )
    )
    cursor = None
    seen = set()
    rows = []
    expected_total = None
    seen_numbers = set()
    for page in range(100):
        selector = (
            f"issue(number:{number})"
            if number is not None
            else (
                f"issues(first:{page_size}, after:{json.dumps(cursor)}, states:[OPEN], "
                f"labels:{json.dumps(labels)}, "
                + (
                    f"filterBy:{{milestoneNumber:{json.dumps(str(milestone))}}}, "
                    if milestone is not None
                    else ""
                )
                + f"orderBy:{{field:CREATED_AT, direction:ASC}})"
            )
        )
        selection = (
            fields
            if number is not None
            else f"totalCount nodes {{ {fields} }} pageInfo {{ hasNextPage endCursor }}"
        )
        data = query_data(
            reader,
            f"query {{ repository(owner:{json.dumps(owner)}, name:{json.dumps(name)}) "
            f"{{ {selector} {{ {selection} }} }} rateLimit {{ cost remaining resetAt }} }}",
            step=f"plan_graph_page_{page + 1}",
        )
        repository = data.get("repository")
        if not isinstance(repository, dict):
            raise github_read.GitHubReadShapeError("GraphQL repository unavailable")
        if number is not None:
            nodes, cursor = [repository["issue"]], None
        else:
            nodes, cursor, total = connection_page(repository.get("issues"))
            if expected_total is not None and expected_total != total:
                raise github_read.GitHubReadShapeError(
                    "issue inventory changed during pagination"
                )
            expected_total = total
        for node in nodes:
            if not isinstance(node, dict):
                raise github_read.GitHubReadShapeError("GraphQL issue unavailable")
            if node.get("number") in seen_numbers:
                raise github_read.GitHubReadShapeError(
                    "duplicate issue during pagination"
                )
            seen_numbers.add(node.get("number"))
            row = rest_issue(
                complete_issue(reader, copy.deepcopy(node), comments=number is not None)
            )
            if milestone is None or (row["milestone"] or {}).get("number") == milestone:
                rows.append(row)
            if len(rows) >= limit:
                return rows
        if cursor is None:
            return rows
        if cursor in seen:
            raise github_read.GitHubReadShapeError("issue inventory cursor repeated")
        seen.add(cursor)
    raise github_read.GitHubReadShapeError("issue inventory exceeded page bound")
