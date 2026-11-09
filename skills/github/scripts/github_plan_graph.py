#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Explicit, complete planning GraphQL reads using the shared reader transport."""

from __future__ import annotations

import json
import copy
import re
from typing import Any, Callable, NamedTuple

import github_read
import github_pr_batch


class NativeIssueGraph(NamedTuple):
    nodes: set[tuple[str, int]]
    complete: bool


def native_issue_key(issue: dict[str, Any]) -> tuple[str, int]:
    match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)", str(issue.get("html_url") or ""))
    if not match or "pull_request" in issue:
        raise ValueError("native graph relationship identity unknown")
    return match[1].casefold(), int(match[2])


def walk_native_issues(
    roots: list[tuple[str, int]], *, read_edges: Callable[[str, int], list[tuple[str, int]]],
    max_nodes: int = 500, target: tuple[str, int] | None = None,
) -> NativeIssueGraph:
    pending = list(roots)
    seen: set[tuple[str, int]] = set()
    while pending:
        repo, number = pending.pop()
        key = repo.casefold(), number
        if key in seen:
            continue
        if len(seen) >= max_nodes:
            return NativeIssueGraph(seen, False)
        seen.add(key)
        if key == target:
            return NativeIssueGraph(seen, False)
        pending.extend(read_edges(repo, number))
    return NativeIssueGraph(seen, True)


ISSUE_FIELDS = """databaseId number title body state stateReason createdAt updatedAt closedAt url
 author { __typename login } authorAssociation repository { nameWithOwner }
 milestone { number title state createdAt dueOn url }
 issueDependenciesSummary { blockedBy blocking totalBlockedBy totalBlocking }
 subIssuesSummary { total completed percentCompleted }"""
RELATED_FIELDS = "number title state url repository { nameWithOwner }"
CLOSURE_FIELDS = RELATED_FIELDS + " body stateReason closedAt updatedAt subIssuesSummary { total completed percentCompleted }"
CONNECTIONS = {
    "labels": "name",
    "blockedBy": CLOSURE_FIELDS,
    "blocking": CLOSURE_FIELDS,
    "subIssues": CLOSURE_FIELDS,
    "comments": "id databaseId body createdAt updatedAt url author { __typename login }",
}

PULL_FIELDS = """databaseId number title body state isDraft createdAt updatedAt closedAt url merged mergedAt
 headRefOid headRefName baseRefName headRepository { nameWithOwner } baseRepository { nameWithOwner }
 author { __typename login } authorAssociation milestone { number title state createdAt dueOn url } repository { nameWithOwner }"""


def rest_record(node: dict[str, Any], *, kind: str) -> dict[str, Any]:
    if kind == "issue":
        return rest_issue(node)
    row = {
        **rest_issue(node),
        **github_pr_batch.rest_pull_request(node, node["labels"]),
        "pull_request": {"url": f"https://api.github.com/repos/{node['repository']['nameWithOwner']}/pulls/{node['number']}"},
        "_graph_relationships": {"blocked_by": [], "blocking": [], "sub_issues": []},
    }
    row.pop("id", None)
    return row


def repository_records(
    reader: github_read.GitHubReader,
    repo: str,
    *,
    kind: str = "issue",
    state: str = "open",
    labels: list[str] | None = None,
    milestone: int | None = None,
    sort: str = "created",
    direction: str = "desc",
    limit: int | None = None,
    metadata: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    field = "issues" if kind == "issue" else "pullRequests"
    states = {"open": "[OPEN]", "closed": "[CLOSED,MERGED]" if kind == "pullRequest" else "[CLOSED]", "all": "null"}[state]
    order = {"created": "CREATED_AT", "updated": "UPDATED_AT"}[sort]
    fields = (ISSUE_FIELDS if kind == "issue" else PULL_FIELDS) + " " + connection_query("labels")

    def selection(cursor: str | None) -> str:
        filters = f",filterBy:{{milestoneNumber:{json.dumps(str(milestone))}}}" if milestone is not None and kind == "issue" else ""
        return (
            f"isArchived {field}(first:100,after:{json.dumps(cursor)},states:{states},labels:{json.dumps(labels[:1] if labels else labels)}"
            f"{filters},orderBy:{{field:{order},direction:{direction.upper()}}}) {{ totalCount nodes {{ {fields} }} "
            "pageInfo { hasNextPage endCursor } }"
        )

    def normalize(_repo: str, node: dict[str, Any]) -> dict[str, Any]:
        return rest_record(complete_issue(reader, copy.deepcopy(node), comments=False, fields=("labels",), kind=kind), kind=kind)

    result = github_read.read_repository_connections(
        reader,
        [repo],
        field=field,
        selection=selection,
        normalize=normalize,
        step="planning_inventory",
        limit=None if labels and len(labels) > 1 or kind == "pullRequest" and milestone is not None else limit,
        metadata=metadata,
        retry_policy=github_read.github_api_core.optional_batch_retry_policy(),
    )[repo]
    if isinstance(result, Exception):
        raise result
    return [
        row
        for row in result
        if (not labels or {label.casefold() for label in labels} <= {label["name"].casefold() for label in row["labels"]})
        and (milestone is None or (row.get("milestone") or {}).get("number") == milestone)
    ][:limit]


def issue_connection(
    reader: github_read.GitHubReader, repo: str, number: int, field: str, *, limit: int | None = None
) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    selection = f"parent {{ {RELATED_FIELDS} }}" if field == "parent" else connection_query(field)
    payload = query_data(
        reader,
        f"query {{ repository(owner:{json.dumps(owner)},name:{json.dumps(name)}) {{ "
        f"issueOrPullRequest(number:{number}) {{ __typename ... on Issue {{ number updatedAt repository {{ nameWithOwner }} {selection} }} "
        + (
            f"... on PullRequest {{ number updatedAt repository {{ nameWithOwner }} {selection if field in {'comments', 'labels'} else ''} }}"
        )
        + " } } rateLimit { cost remaining resetAt } }",
        step="planning_connection",
        root_path=("repository", "issueOrPullRequest"),
    )
    node = payload["repository"]["issueOrPullRequest"]
    if not isinstance(node, dict) or node.get("number") != number:
        raise github_read.GitHubReadShapeError("Planning connection unavailable or mismatched")
    kind = "issue" if node["__typename"] == "Issue" else "pullRequest"
    if kind == "pullRequest" and field not in {"comments", "labels"}:
        return []
    if field == "parent":
        return [rest_related_issue(node["parent"])] if node["parent"] is not None else []
    nodes = complete_issue(reader, node, comments=True, fields=(field,), kind=kind, limit=limit)[field]
    if field == "comments":
        return [rest_comment(row) for row in nodes]
    if field == "labels":
        return nodes
    related = [rest_related_issue(row) for row in nodes]
    if any(row["state"] == "closed" and not closed_relationship_metadata_complete(row) for row in related):
        raise github_read.GitHubReadShapeError("Closed relationship metadata unavailable")
    return related


def repository_names(reader: github_read.GitHubReader, repo: str, field: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    selection_field = "refs" if field == "branches" else "labels"
    arguments = (
        'refPrefix:"refs/heads/",orderBy:{field:ALPHABETICAL,direction:ASC},'
        if field == "branches"
        else "orderBy:{field:NAME,direction:ASC},"
    )
    result = github_read.read_repository_connections(
        reader,
        [repo],
        field=selection_field,
        selection=lambda cursor: (
            f"{selection_field}({arguments}first:100,after:{json.dumps(cursor)}) {{ totalCount nodes {{ name }} pageInfo {{ hasNextPage endCursor }} }}"
        ),
        normalize=lambda _repo, node: node,
        identity=lambda node: node.get("name"),
        step="planning_names",
        limit=limit,
    )[repo]
    if isinstance(result, Exception):
        raise result
    return result


def search_records(
    reader: github_read.GitHubReader,
    query: str,
    *,
    limit: int,
    allow_partial: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cursor = None
    consumed = 0
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    identities: set[tuple[str, int]] = set()
    expected = None
    totals: set[int] = set()
    errors: list[str] = []
    fields = (
        "__typename ... on Issue { "
        + ISSUE_FIELDS
        + " "
        + connection_query("labels")
        + " } ... on PullRequest { "
        + PULL_FIELDS
        + " "
        + connection_query("labels")
        + " }"
    )
    while consumed < limit:
        try:
            document = (
                f"query {{ search(query:{json.dumps(query)},type:ISSUE,first:{min(100, limit - consumed)},after:{json.dumps(cursor)}) "
                f"{{ totalCount:issueCount nodes {{ {fields} }} pageInfo {{ hasNextPage endCursor }} }} rateLimit {{ cost remaining resetAt }} }}"
            )
            payload = query_data(reader, document, step="planning_search", root_path=("search",))
            nodes, cursor, total = connection_page(payload.get("search"))
            totals.add(total)
            expected = max(total, expected or 0)
            for node in nodes[:limit - consumed]:
                consumed += 1
                try:
                    kind = {"Issue": "issue", "PullRequest": "pullRequest"}[node["__typename"]]
                    repo, number = node["repository"]["nameWithOwner"], node["number"]
                    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or type(number) is not int or number <= 0:
                        raise github_read.GitHubReadShapeError("Search record identity unavailable")
                    key = repo.casefold(), number
                    if key in identities:
                        raise github_read.GitHubReadShapeError("Duplicate search record", retryable=True)
                    row = rest_record(complete_issue(reader, node, comments=False, fields=("labels",), kind=kind), kind=kind)
                    identities.add(key)
                    rows.append(row)
                except (github_read.GitHubReadError, github_read.GitHubReadShapeError, KeyError, TypeError, ValueError) as error:
                    if not allow_partial:
                        raise
                    errors.append(str(error))
            if cursor is None or consumed >= limit:
                break
            if cursor in seen:
                raise github_read.GitHubReadShapeError("Search cursor repeated")
            seen.add(cursor)
        except (github_read.GitHubReadError, github_read.GitHubReadShapeError, KeyError, TypeError, ValueError) as error:
            if not allow_partial:
                raise
            errors.append(str(error))
            break
    if len(totals) > 1:
        errors.append("Search total changed during pagination")
    return rows, {
        "total_count": expected,
        "incomplete_results": bool(errors) or cursor is None and len(rows) != expected,
        **({"errors": errors} if allow_partial else {}),
    }



connection_page = github_read.graphql_connection_page


def query_data(
    reader: github_read.GitHubReader,
    query: str,
    *,
    step: str,
    operation: str | None = None,
    root_path: tuple[str, ...] = ("repository",),
) -> dict[str, Any]:
    # Do not spend the reset window waiting on this optional enrichment. REST
    # recovery uses the same reader identity and inherited operation deadline.
    result = reader.graphql_json(
        query,
        {},
        step=step,
        retry_policy=github_read.github_api_core.optional_batch_retry_policy(),
        **({"operation": operation} if operation is not None else {}),
    )
    if not result.ok:
        raise github_read.GitHubReadError(
            "planning GraphQL read unavailable",
            result=result,
            diagnostics=reader.diagnostics(),
        )
    return github_read.graphql_data(result, root_path=root_path)


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
    reader: github_read.GitHubReader,
    node: dict[str, Any],
    *,
    comments: bool,
    fields: tuple[str, ...] | None = None,
    kind: str = "issue",
    limit: int | None = None,
) -> dict[str, Any]:
    names = fields if fields is not None else tuple(name for name in CONNECTIONS if comments or name != "comments")
    repo = node["repository"]["nameWithOwner"]
    number = node["number"]
    owner, name = repo.split("/", 1)
    for field in names:
        nodes, cursor, total = connection_page(node.get(field))
        seen = set()
        pages = 1
        target = min(total, limit) if limit is not None else total
        while cursor is not None and len(nodes) < target:
            if cursor in seen or pages >= 100:
                raise github_read.GitHubReadShapeError(
                    "GraphQL pagination exceeded bound or repeated cursor"
                )
            seen.add(cursor)
            query = (
                f"query {{ repository(owner:{json.dumps(owner)}, name:{json.dumps(name)}) "
                f"{{ {kind}(number:{number}) {{ updatedAt {connection_query(field, cursor)} }} }} rateLimit {{ cost remaining resetAt }} }}"
            )
            current = query_data(reader, query, step=f"plan_{number}_{field}_{pages + 1}", root_path=("repository", kind))["repository"][kind]
            if current["updatedAt"] != node["updatedAt"]:
                raise github_read.GitHubReadShapeError(
                    "issue changed during GraphQL pagination", retryable=True,
                )
            more, cursor, page_total = connection_page(current.get(field))
            if page_total != total:
                raise github_read.GitHubReadShapeError(
                    "GraphQL connection changed during pagination", retryable=True,
                )
            nodes.extend(more)
            pages += 1
        if len(nodes) < target or cursor is None and len(nodes) != total:
            raise github_read.GitHubReadShapeError("GraphQL connection incomplete")
        key = "databaseId" if field == "comments" else "name" if field == "labels" else "url"
        identities = [item.get(key) for item in nodes]
        if any(value is None for value in identities) or len(set(identities)) != len(nodes):
            raise github_read.GitHubReadShapeError("GraphQL connection identity unavailable or duplicated")
        node[field] = nodes[:target]
    return node


rest_author = github_read.graphql_author


def rest_related_issue(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "repo": node["repository"]["nameWithOwner"],
        "number": node["number"],
        "title": node.get("title"),
        "state": node["state"].lower(),
        "html_url": node["url"],
        **({"body": node["body"]} if "body" in node else {}),
        **({"state_reason": node["stateReason"].lower() if node["stateReason"] else None} if "stateReason" in node else {}),
        **({"closed_at": node["closedAt"]} if "closedAt" in node else {}),
        **({"updated_at": node["updatedAt"]} if "updatedAt" in node else {}),
        **(
            {
                "sub_issues_summary": {
                    "total": node["subIssuesSummary"]["total"],
                    "completed": node["subIssuesSummary"]["completed"],
                    "percent_completed": node["subIssuesSummary"]["percentCompleted"],
                }
            }
            if "subIssuesSummary" in node
            else {}
        ),
    }


def closed_relationship_metadata_complete(row: dict[str, Any]) -> bool:
    summary = row.get("sub_issues_summary")
    return (
        isinstance(row.get("body"), str)
        and "state_reason" in row
        and "closed_at" in row
        and isinstance(summary, dict)
        and type(summary.get("total")) is int
        and type(summary.get("completed")) is int
        and 0 <= summary["completed"] <= summary["total"]
    )


rest_comment = github_read.graphql_comment


def rest_milestone(milestone: dict[str, Any] | None) -> dict[str, Any] | None:
    if milestone is None:
        return None
    return {
        **milestone,
        "state": milestone["state"].lower(),
        "created_at": milestone["createdAt"],
        "due_on": milestone["dueOn"],
        "html_url": milestone["url"],
    }


def rest_issue(node: dict[str, Any]) -> dict[str, Any]:
    deps, sub = node.get("issueDependenciesSummary"), node.get("subIssuesSummary")
    return {
        **({"id": node["databaseId"]} if node.get("databaseId") is not None else {}),
        "repo": node["repository"]["nameWithOwner"],
        "number": node["number"],
        "title": node["title"],
        "body": node["body"],
        "state": "closed" if node["state"] == "MERGED" else node["state"].lower(),
        "state_reason": node["stateReason"].lower() if node.get("stateReason") else None,
        "created_at": node["createdAt"],
        "updated_at": node["updatedAt"],
        "closed_at": node.get("closedAt"),
        **(
            {"comments": len(node["comments"]) if isinstance(node["comments"], list) else node["comments"]["totalCount"]}
            if "comments" in node
            else {}
        ),
        "html_url": node["url"],
        "user": rest_author(node["author"]),
        "author_association": node["authorAssociation"],
        "labels": node["labels"],
        "milestone": rest_milestone(node["milestone"]),
        **({"parent": rest_related_issue(node["parent"]) if node["parent"] is not None else None} if "parent" in node else {}),
        **(
            {
                "issue_dependencies_summary": {
                    "blocked_by": deps["blockedBy"],
                    "blocking": deps["blocking"],
                    "total_blocked_by": deps["totalBlockedBy"],
                    "total_blocking": deps["totalBlocking"],
                }
            }
            if deps is not None
            else {}
        ),
        **(
            {
                "sub_issues_summary": {
                    "total": sub["total"],
                    "completed": sub["completed"],
                    "percent_completed": sub["percentCompleted"],
                }
            }
            if sub is not None
            else {}
        ),
        **(
            {
                "_graph_relationships": {
                    key: [rest_related_issue(n) for n in node[field]]
                    for key, field in [
                        ("blocked_by", "blockedBy"),
                        ("blocking", "blocking"),
                        ("sub_issues", "subIssues"),
                    ]
                }
            }
            if all(isinstance(node.get(field), list) for field in ("blockedBy", "blocking", "subIssues"))
            else {}
        ),
        **(
            {"_graph_comments": [rest_comment(n) for n in node.get("comments", [])]}
            if isinstance(node.get("comments"), list)
            else {}
        ),
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
    metadata: dict[str, dict[str, Any]] | None = None,
    connection_fields: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    if not 1 <= page_size <= 100 or limit < 1:
        raise ValueError("invalid planning graph bounds")
    owner, name = repo.split("/", 1)
    connections = (
        connection_fields
        if connection_fields is not None
        else tuple(field for field in CONNECTIONS if number is not None or field != "comments")
    )
    comment_count = "" if "comments" in connections else "comments(first:1) { totalCount }"
    fields = (
        ISSUE_FIELDS
        + " " + comment_count + " "
        + " ".join(
            connection_query(field)
            for field in connections
        )
    )
    cursor = None
    seen = set()
    rows = []
    expected_total = None
    seen_numbers = set()
    for page in range(100):
        selector = (
            f"issue:issueOrPullRequest(number:{number})"
            if number is not None
            else (
                f"issues(first:{page_size}, after:{json.dumps(cursor)}, states:[OPEN], "
                f"labels:{json.dumps(labels[:1] if labels else labels)}, "
                + (
                    f"filterBy:{{milestoneNumber:{json.dumps(str(milestone))}}}, "
                    if milestone is not None
                    else ""
                )
                + "orderBy:{field:CREATED_AT, direction:ASC})"
            )
        )
        selection = (
            "__typename ... on Issue { "
            + fields
            + " } ... on PullRequest { "
            + PULL_FIELDS
            + " "
            + comment_count
            + " "
            + " ".join(connection_query(field) for field in connections if field in {"labels", "comments"})
            + " }"
            if number is not None
            else f"totalCount nodes {{ {fields} }} pageInfo {{ hasNextPage endCursor }}"
        )
        data = query_data(
            reader,
            f"query {{ repository(owner:{json.dumps(owner)}, name:{json.dumps(name)}) "
            f"{{ nameWithOwner isArchived defaultBranchRef {{ name }} {selector} {{ {selection} }} }} rateLimit {{ cost remaining resetAt }} }}",
            step=f"plan_graph_page_{page + 1}",
            root_path=("repository", "issue") if number is not None else ("repository", "issues"),
        )
        repository = data.get("repository")
        if not isinstance(repository, dict):
            raise github_read.GitHubReadShapeError("GraphQL repository unavailable")
        if metadata is not None and isinstance(repository.get("isArchived"), bool):
            metadata[repo] = {
                "full_name": repository["nameWithOwner"],
                "archived": repository["isArchived"],
                "default_branch": (repository.get("defaultBranchRef") or {}).get("name"),
            }
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
            kind = "pullRequest" if node.get("__typename") == "PullRequest" else "issue"
            selected = tuple(field for field in connections if kind == "issue" or field in {"labels", "comments"})
            row = rest_record(
                complete_issue(
                    reader,
                    copy.deepcopy(node),
                    comments=number is not None,
                    kind=kind,
                    fields=selected,
                ),
                kind=kind,
            )
            if "comments" in selected:
                row["_graph_comments_complete"] = True
            if (milestone is None or (row["milestone"] or {}).get("number") == milestone) and (not labels or {label.casefold() for label in labels} <= {label["name"].casefold() for label in row["labels"]}):
                rows.append(row)
            if len(rows) >= limit:
                return rows
        if cursor is None:
            return rows
        if cursor in seen:
            raise github_read.GitHubReadShapeError("issue inventory cursor repeated")
        seen.add(cursor)
    raise github_read.GitHubReadShapeError("issue inventory exceeded page bound")
