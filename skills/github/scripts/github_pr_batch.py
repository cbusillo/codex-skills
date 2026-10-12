#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""One-pass PR observer enrichment; no persistent authorization or readiness cache."""

from __future__ import annotations

import json
from typing import Any

import github_api
import github_read

FIELDS = """databaseId number title body state isDraft createdAt merged mergedAt mergeCommit { oid }
 mergeable mergeStateStatus reviewDecision headRefOid headRefName baseRefName
 headRepository { nameWithOwner } baseRepository { nameWithOwner } author { __typename login }
 changedFiles url updatedAt"""
CONTEXTS = """__typename
 ... on CheckRun { databaseId name status conclusion detailsUrl startedAt completedAt
 checkSuite { databaseId app { databaseId slug name } } }
 ... on StatusContext { id context state description targetUrl createdAt }"""
CONNECTIONS = {
    "labels": "name",
    "reviews": "databaseId body state submittedAt url author { __typename login } authorAssociation commit { oid }",
    "comments": "databaseId body createdAt updatedAt url author { __typename login } authorAssociation",
}


def connection(field: str, selection: str, cursor: str | None = None) -> str:
    return f"{field}(first:100, after:{json.dumps(cursor)}) {{ totalCount nodes {{ {selection} }} pageInfo {{ hasNextPage endCursor }} }}"


def query(reader, document):
    result = reader.graphql_json(
        document,
        {},
        step="pr_batch",
        retry_policy=github_api.RetryPolicy(max_wait_seconds=30, max_attempts=1),
    )
    if not result.ok:
        raise github_read.GitHubReadError(
            "PR batch unavailable", result=result, diagnostics=reader.diagnostics()
        )
    rate = (
        (result.body.get("data") or {}).get("rateLimit")
        if isinstance(result.body, dict)
        else None
    )
    cost = rate.get("cost") if isinstance(rate, dict) else None
    previous = reader.batch_diagnostic.get("cost", 0)
    reader.batch_diagnostic["cost"] = (
        previous + cost if type(previous) is int and type(cost) is int else None
    )
    if (
        not isinstance(result.body, dict)
        or result.body.get("errors")
        or not isinstance(result.body.get("data"), dict)
    ):
        raise github_read.GitHubReadShapeError("partial GraphQL PR batch")
    return result.body["data"]


def page(value):
    if not isinstance(value, dict):
        raise github_read.GitHubReadShapeError("missing PR connection")
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
        raise github_read.GitHubReadShapeError("invalid PR connection")
    cursor = info.get("endCursor") if info["hasNextPage"] else None
    if info["hasNextPage"] and (not nodes or not isinstance(cursor, str) or not cursor):
        raise github_read.GitHubReadShapeError("PR cursor did not advance")
    return nodes, cursor, total


def commit_contexts(node):
    commits = node["commits"]["nodes"]
    if (
        not isinstance(commits, list)
        or len(commits) != 1
        or commits[0]["commit"]["oid"] != node["headRefOid"]
    ):
        raise github_read.GitHubReadShapeError(
            "PR batch latest commit does not match head"
        )
    rollup = commits[0]["commit"]["statusCheckRollup"]
    # A null rollup has no positive completeness proof; REST recovers.
    if not isinstance(rollup, dict):
        raise github_read.GitHubReadShapeError("PR check rollup unavailable")
    return rollup["contexts"]


def complete(reader, repo, node, field):
    value = commit_contexts(node) if field == "contexts" else node[field]
    nodes, cursor, total = page(value)
    nodes = list(nodes)
    seen = set()
    owner, name = repo.split("/", 1)
    for _ in range(100):
        if cursor is None:
            if len(nodes) != total:
                raise github_read.GitHubReadShapeError("incomplete PR connection")
            if field == "contexts":
                identities = [
                    (
                        n["__typename"],
                        n["databaseId"] if n["__typename"] == "CheckRun" else n["id"],
                    )
                    for n in nodes
                ]
            else:
                identities = [
                    n["name"] if field == "labels" else n["databaseId"] for n in nodes
                ]
            if len(set(identities)) != len(nodes):
                raise github_read.GitHubReadShapeError("duplicate PR connection nodes")
            return nodes
        if cursor in seen:
            raise github_read.GitHubReadShapeError("PR cursor repeated")
        seen.add(cursor)
        select = connection(
            field, CONTEXTS if field == "contexts" else CONNECTIONS[field], cursor
        )
        if field == "contexts":
            select = f"commits(last:1) {{ nodes {{ commit {{ oid statusCheckRollup {{ {select} }} }} }} }}"
        more = query(
            reader,
            f"query {{ repository(owner:{json.dumps(owner)}, name:{json.dumps(name)}) "
            f"{{ pullRequest(number:{node['number']}) {{ headRefOid updatedAt {select} }} }} rateLimit {{ cost remaining resetAt }} }}",
        )["repository"]["pullRequest"]
        if (
            more["headRefOid"] != node["headRefOid"]
            or more["updatedAt"] != node["updatedAt"]
        ):
            raise github_read.GitHubReadShapeError("PR changed during pagination")
        extra, cursor, page_total = page(
            commit_contexts(more) if field == "contexts" else more[field]
        )
        if page_total != total:
            raise github_read.GitHubReadShapeError(
                "PR connection changed during pagination"
            )
        nodes.extend(extra)
    raise github_read.GitHubReadShapeError("PR pagination exceeded bound")


def rest_author(author):
    if author is None:
        return {"login": "ghost", "type": "User"}
    bot = author.get("__typename") == "Bot"
    login = author["login"]
    return {
        "login": login + "[bot]" if bot and not login.endswith("[bot]") else login,
        "type": "Bot" if bot else "User",
    }


def batch_pull_requests(
    reader: github_read.GitHubReader,
    repo: str,
    numbers: list[int],
    *,
    include_discussion: bool = True,
) -> dict[int, dict[str, Any]]:
    """Fetch complete bounded documents and install only one-pass REST-shaped reads."""
    if (
        not numbers
        or len(numbers) > 20
        or len(set(numbers)) != len(numbers)
        or any(type(n) is not int or n < 1 for n in numbers)
    ):
        raise ValueError("PR batch requires 1..20 distinct positive PR numbers")
    reader.batch_diagnostic = {"used": False, "cost": 0}
    owner, name = repo.split("/", 1)
    fields = CONNECTIONS if include_discussion else {"labels": CONNECTIONS["labels"]}
    selections = " ".join(connection(field, text) for field, text in fields.items())
    selections += (
        " commits(last:1) { nodes { commit { oid statusCheckRollup { "
        + connection("contexts", CONTEXTS)
        + " } } } }"
    )
    aliases = " ".join(
        f"p{n}:pullRequest(number:{n}) {{ {FIELDS} {selections} }}" for n in numbers
    )
    data = query(
        reader,
        f"query {{ repository(owner:{json.dumps(owner)}, name:{json.dumps(name)}) {{ {aliases} }} rateLimit {{ cost remaining resetAt }} }}",
    )
    repository = data.get("repository")
    if not isinstance(repository, dict):
        raise github_read.GitHubReadShapeError("PR repository unavailable")
    responses = {}
    pulls = {}
    for number in numbers:
        node = repository.get(f"p{number}")
        if not isinstance(node, dict) or node.get("number") != number:
            raise github_read.GitHubReadShapeError("PR unavailable or mismatched")
        connections = {
            field: complete(reader, repo, node, field)
            for field in [*fields, "contexts"]
        }
        sha = node["headRefOid"]
        if not isinstance(sha, str) or len(sha) != 40:
            raise github_read.GitHubReadShapeError("PR head unavailable")
        pull = {
            "id": node["databaseId"],
            "created_at": node["createdAt"],
            "number": number,
            "title": node["title"],
            "body": node["body"],
            "state": "closed" if node["state"] in {"CLOSED", "MERGED"} else "open",
            "draft": node["isDraft"],
            "merged": node["merged"],
            "merged_at": node["mergedAt"],
            "merge_commit_sha": (node["mergeCommit"] or {}).get("oid"),
            "mergeable": {"MERGEABLE": True, "CONFLICTING": False, "UNKNOWN": None}[
                node["mergeable"]
            ],
            "mergeable_state": node["mergeStateStatus"].lower(),
            "reviewDecision": node["reviewDecision"],
            "labels": connections["labels"],
            "html_url": node["url"],
            "user": rest_author(node["author"]),
            "head": {
                "sha": sha,
                "ref": node["headRefName"],
                "repo": {
                    "full_name": (node["headRepository"] or {}).get("nameWithOwner")
                },
            },
            "base": {
                "ref": node["baseRefName"],
                "repo": {"full_name": node["baseRepository"]["nameWithOwner"]},
            },
            "changed_files": node["changedFiles"],
            "updated_at": node["updatedAt"],
        }
        pulls[number] = pull
        responses[f"/repos/{repo}/pulls/{number}"] = pull
        if include_discussion:
            responses[f"/repos/{repo}/issues/{number}/comments"] = [
                {
                    "id": n["databaseId"],
                    "body": n["body"],
                    "created_at": n["createdAt"],
                    "updated_at": n["updatedAt"],
                    "html_url": n["url"],
                    "user": rest_author(n["author"]),
                    "author_association": n["authorAssociation"],
                }
                for n in connections["comments"]
            ]
            responses[f"/repos/{repo}/pulls/{number}/reviews"] = [
                {
                    "id": n["databaseId"],
                    "body": n["body"],
                    "state": n["state"],
                    "submitted_at": n["submittedAt"],
                    "html_url": n["url"],
                    "user": rest_author(n["author"]),
                    "author_association": n["authorAssociation"],
                    "commit_id": (n["commit"] or {}).get("oid"),
                }
                for n in connections["reviews"]
            ]
        checks = []
        statuses = []
        for context in connections["contexts"]:
            if context["__typename"] == "CheckRun":
                suite = context["checkSuite"]
                app = suite["app"] or {}
                checks.append(
                    {
                        "id": context["databaseId"],
                        "name": context["name"],
                        "status": context["status"].lower(),
                        "conclusion": context["conclusion"].lower()
                        if context["conclusion"]
                        else None,
                        "details_url": context["detailsUrl"],
                        "started_at": context["startedAt"],
                        "completed_at": context["completedAt"],
                        "head_sha": sha,
                        "check_suite": {"id": suite["databaseId"]},
                        "app": {
                            "id": app.get("databaseId"),
                            "slug": app.get("slug"),
                            "name": app.get("name"),
                        },
                    }
                )
            elif context["__typename"] == "StatusContext":
                statuses.append(
                    {
                        "id": context["id"],
                        "context": context["context"],
                        "state": "pending"
                        if context["state"] == "EXPECTED"
                        else context["state"].lower(),
                        "description": context["description"],
                        "target_url": context["targetUrl"],
                        "created_at": context["createdAt"],
                        "updated_at": context["createdAt"],
                    }
                )
            else:
                raise github_read.GitHubReadShapeError("unknown check context")
        responses[f"/repos/{repo}/commits/{sha}/check-runs"] = {"check_runs": checks}
        responses[f"/repos/{repo}/commits/{sha}/statuses"] = statuses
    # Publish atomically only after EVERY requested PR/connection is complete.
    # Snapshots never survive the pass or cross readers/actors. A consumed path
    # recontacts REST (notably the auto-enqueue final head/labels recheck).
    reader.batch_identity = reader.batch_context()
    reader.batch_responses = responses
    reader.batch_diagnostic["used"] = True
    return pulls


def try_batch(reader, repo, numbers, *, include_discussion=True):
    reader.batch_responses = {}
    reader.batch_diagnostic = {"used": False}
    try:
        pulls = batch_pull_requests(
            reader, repo, numbers, include_discussion=include_discussion
        )
        reader.batch_diagnostic["used"] = True
        return pulls
    except (
        github_read.GitHubReadError,
        github_read.GitHubReadShapeError,
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
    ) as error:
        reader.batch_responses = {}
        reader.batch_diagnostic["fallback_reason"] = github_api.redact_string(
            str(error)
        )
        return None
