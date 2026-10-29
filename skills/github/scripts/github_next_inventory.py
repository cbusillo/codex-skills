# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import json
from typing import Any

import github_plan_graph as graph
import github_read

PAGE_SIZE = 20
COMMON_FIELDS = """number title body state createdAt updatedAt lastEditedAt url
 author { __typename login } authorAssociation repository { nameWithOwner }
 milestone { number title state createdAt dueOn url }
 assignees(first:100) { totalCount nodes { login } pageInfo { hasNextPage endCursor } }"""
PULL_FIELDS = """isDraft headRefOid headRefName baseRefName
 headRepository { nameWithOwner } baseRepository { nameWithOwner }"""


def connection(name: str, fields: str, cursor: str | None = None, *, size: int = 100) -> str:
    return (
        f"{name}(first:{size}, after:{json.dumps(cursor)}) {{ totalCount "
        f"nodes {{ {fields} }} pageInfo {{ hasNextPage endCursor }} }}"
    )


def complete_connections(
    reader: github_read.GitHubReader,
    node: dict[str, Any],
    fields: dict[str, str],
    *,
    kind: str,
) -> None:
    owner, name = node["repository"]["nameWithOwner"].split("/", 1)
    for field, selection in fields.items():
        rows, cursor, total = graph.connection_page(node.get(field))
        seen: set[str] = set()
        while cursor is not None:
            if cursor in seen:
                raise github_read.GitHubReadShapeError("Inventory connection cursor repeated")
            seen.add(cursor)
            query = (
                f"query {{ repository(owner:{json.dumps(owner)},name:{json.dumps(name)}) {{ "
                f"{kind}(number:{node['number']}) {{ updatedAt {connection(field, selection, cursor)} }} }} }}"
            )
            current = graph.query_data(reader, query, step="next_inventory_connection")["repository"][kind]
            more, cursor, count = graph.connection_page(current.get(field))
            if count != total or current["updatedAt"] != node["updatedAt"]:
                raise github_read.GitHubReadShapeError("Inventory record changed during pagination")
            rows.extend(more)
        if len(rows) != total:
            raise github_read.GitHubReadShapeError("Inventory connection incomplete")
        node[field] = rows


def rest_record(node: dict[str, Any], *, kind: str) -> dict[str, Any]:
    if kind == "issue":
        row = graph.rest_issue(node)
        row.update(
            _graph_truncated=[],
            _graph_comments_complete=True,
            _graph_labels_complete=True,
            parent=graph.rest_related_issue(node["parent"]) if node.get("parent") else None,
        )
    else:
        row = {
            "repo": node["repository"]["nameWithOwner"],
            "number": node["number"],
            "title": node["title"],
            "body": node["body"],
            "state": node["state"].lower(),
            "html_url": node["url"],
            "created_at": node["createdAt"],
            "updated_at": node["updatedAt"],
            "user": graph.rest_author(node["author"]),
            "labels": node["labels"],
            "milestone": graph.rest_milestone(node.get("milestone")),
            "pull_request": {"url": node["url"]},
            "draft": node["isDraft"],
            "head": {
                "sha": node["headRefOid"],
                "ref": node["headRefName"],
                "repo": {"full_name": (node.get("headRepository") or {}).get("nameWithOwner")},
            },
            "base": {
                "ref": node["baseRefName"],
                "repo": {"full_name": node["baseRepository"]["nameWithOwner"]},
            },
            "_graph_comments": [graph.rest_comment(row) for row in node["comments"]],
            "_graph_comments_complete": True,
        }
    return {
        **row,
        "last_edited_at": node.get("lastEditedAt"),
        "assignees": node["assignees"],
        "comments": len(node["comments"]),
    }


def read_repository(reader: github_read.GitHubReader, repo: str) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    cursors: dict[str, str | None] = {"issues": None, "pullRequests": None}
    totals: dict[str, int] = {}
    seen_cursors: dict[str, set[str]] = {field: set() for field in cursors}
    rows: dict[str, list[dict[str, Any]]] = {field: [] for field in cursors}
    seen_records: set[int] = set()
    while cursors:
        selections = []
        for field, cursor in cursors.items():
            kind = "issue" if field == "issues" else "pullRequest"
            connections = {**graph.CONNECTIONS, "assignees": "login"}
            if kind == "pullRequest":
                connections = {key: value for key, value in connections.items() if key in {"labels", "comments", "assignees"}}
            fields = (
                graph.ISSUE_FIELDS + " lastEditedAt parent { " + graph.RELATED_FIELDS + " }"
                if kind == "issue"
                else COMMON_FIELDS + " " + PULL_FIELDS
            )
            if kind == "issue":
                fields += " assignees(first:100) { totalCount nodes { login } pageInfo { hasNextPage endCursor } }"
            fields += " " + " ".join(connection(key, value) for key, value in connections.items() if key != "assignees")
            selections.append(
                f"{field}(states:[OPEN],first:{PAGE_SIZE},after:{json.dumps(cursor)},"
                f"orderBy:{{field:CREATED_AT,direction:ASC}}) {{ totalCount nodes {{ {fields} }} "
                "pageInfo { hasNextPage endCursor } }"
            )
        query = f"query {{ repository(owner:{json.dumps(owner)},name:{json.dumps(name)}) {{ {' '.join(selections)} }} }}"
        repository = graph.query_data(reader, query, step="next_repository_inventory")["repository"]
        for field in list(cursors):
            nodes, cursor, total = graph.connection_page(repository.get(field))
            if field in totals and totals[field] != total:
                raise github_read.GitHubReadShapeError("Inventory changed during pagination")
            totals[field] = total
            for node in nodes:
                kind = "issue" if field == "issues" else "pullRequest"
                if (
                    node["repository"]["nameWithOwner"].casefold() != repo.casefold()
                    or node["number"] in seen_records
                    or node["state"] != "OPEN"
                ):
                    raise github_read.GitHubReadShapeError("Inventory record identity changed")
                fields = {**graph.CONNECTIONS, "assignees": "login"}
                if kind == "pullRequest":
                    fields = {key: value for key, value in fields.items() if key in {"labels", "comments", "assignees"}}
                complete_connections(reader, node, fields, kind=kind)
                rows[field].append(rest_record(node, kind=kind))
                seen_records.add(node["number"])
            if cursor is None:
                if len(rows[field]) != total:
                    raise github_read.GitHubReadShapeError("Inventory incomplete")
                del cursors[field]
            else:
                if cursor in seen_cursors[field]:
                    raise github_read.GitHubReadShapeError("Inventory cursor repeated")
                seen_cursors[field].add(cursor)
                cursors[field] = cursor
    return [*rows["issues"], *rows["pullRequests"]]
