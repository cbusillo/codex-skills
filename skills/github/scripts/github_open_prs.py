#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Cross-repository open-PR discovery; never supplies final write authority."""
from __future__ import annotations

import json
import re
from typing import Any

import github_api
import github_pr_batch as batch
from github_read import GitHubReadError, GitHubReadShapeError

FIELDS = """number title body isDraft createdAt state headRefOid headRefName baseRefName
 headRepository { nameWithOwner } author { __typename login } updatedAt url"""


class InventoryUnavailable(ValueError):
    """This repository is skipped until the next pass; no eligibility is inferred."""


def scan(reader, repositories):
    """One ordinary-pass query; isolate unavailable or changing repositories."""
    repos = list(dict.fromkeys(repositories))
    if any(not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) for repo in repos):
        raise ValueError("open-PR scan requires OWNER/REPO names")
    if len(repos) > 100:
        grouped = {}
        for offset in range(0, len(repos), 100):
            grouped.update(scan(reader, repos[offset:offset + 100]))
        return grouped
    result: dict[str, list[dict[str, Any]] | Exception] = {repo: [] for repo in repos}
    cursors = {repo: None for repo in repos}
    totals, seen = {}, {repo: set() for repo in repos}
    for _ in range(100):
        if not cursors:
            return result
        aliases = {}
        selections = []
        for index, (repo, cursor) in enumerate(cursors.items()):
            owner, name = repo.split("/")
            alias = f"r{index}"
            aliases[alias] = repo
            selections.append(
                f'{alias}:repository(owner:{json.dumps(owner)},name:{json.dumps(name)}) {{ nameWithOwner '
                f'pullRequests(first:20,after:{json.dumps(cursor)},states:OPEN,orderBy:{{field:CREATED_AT,direction:DESC}}) '
                '{ totalCount pageInfo { hasNextPage endCursor } nodes { '
                + FIELDS + ' ' + batch.connection("labels", "name").replace("first:100", "first:20") + ' } } }'
            )
        response = reader.graphql_json(
            'query { ' + ' '.join(selections) + ' rateLimit { cost remaining resetAt } }', {},
            step="open_prs", retry_policy=github_api.RetryPolicy(max_wait_seconds=30, max_attempts=1),
        )
        if not response.ok:
            error = GitHubReadError("open-PR scan unavailable", result=response, diagnostics=reader.diagnostics())
            result.update(dict.fromkeys(cursors, error))
            return result
        body = response.body if isinstance(response.body, dict) else {}
        payload = body.get("data")
        data: dict[str, Any] = payload if isinstance(payload, dict) else {}
        errors = body.get("errors") or []
        invalid = set()
        for error in errors:
            path = error.get("path") if isinstance(error, dict) else None
            if not isinstance(path, list) or not path or path[0] not in aliases:
                invalid.update(aliases)
            else:
                invalid.add(path[0])
        rate = data.get("rateLimit")
        cost = rate.get("cost") if isinstance(rate, dict) else None
        previous = reader.batch_diagnostic.get("cost", 0)
        reader.batch_diagnostic["cost"] = previous + cost if type(previous) is int and type(cost) is int else None
        pending = {}
        for alias, repo in aliases.items():
            pulls = result[repo]
            if isinstance(pulls, Exception):
                continue
            try:
                repository = data.get(alias)
                if alias in invalid or not isinstance(repository, dict) or str(repository.get("nameWithOwner", "")).casefold() != repo.casefold():
                    raise InventoryUnavailable("open-PR repository unavailable or mismatched")
                nodes, cursor, total = batch.page(repository.get("pullRequests"))
                if total != totals.setdefault(repo, total):
                    raise InventoryUnavailable("open-PR inventory changed during pagination")
                for node in nodes:
                    number = node.get("number")
                    if type(number) is not int or number <= 0 or any(pr["number"] == number for pr in pulls) or node.get("state") != "OPEN":
                        raise InventoryUnavailable("invalid or duplicate open PR")
                    labels = batch.complete(reader, repo, node, "labels")
                    pulls.append({
                        "number": number, "title": node["title"], "body": node["body"],
                        "state": "open", "draft": node["isDraft"], "created_at": node["createdAt"],
                        "user": batch.rest_author(node["author"]), "labels": labels,
                        "html_url": node["url"], "head": {"sha": node["headRefOid"], "ref": node["headRefName"],
                            "repo": {"full_name": (node["headRepository"] or {}).get("nameWithOwner")}},
                        "base": {"ref": node["baseRefName"]},
                    })
                if cursor:
                    if cursor in seen[repo]:
                        raise InventoryUnavailable("open-PR cursor repeated")
                    seen[repo].add(cursor)
                    pending[repo] = cursor
                elif len(pulls) != total:
                    raise InventoryUnavailable("incomplete open-PR inventory")
            except GitHubReadError as error:
                result[repo] = error
            except (InventoryUnavailable, GitHubReadShapeError, KeyError, TypeError, ValueError):
                result[repo] = InventoryUnavailable("open-PR inventory unavailable; retry next pass")
        cursors = pending
    result.update({repo: InventoryUnavailable("open-PR pagination exceeded bound") for repo in cursors})
    return result
