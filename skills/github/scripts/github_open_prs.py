#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Cross-repository open-PR discovery; never supplies final write authority."""
from __future__ import annotations

import json
import re

import github_pr_batch as batch
from github_read import GitHubReadShapeError

FIELDS = """number title body isDraft createdAt state headRefOid headRefName baseRefName
 headRepository { nameWithOwner } author { __typename login } updatedAt url"""


def scan(reader, repositories):
    """One query for the ordinary pass; paginate only overflowing connections.

    Partial, changing or malformed inventories fail closed before publication.
    Keep the reader's identity, retry budget and deadline for every page.
    """
    repos = list(dict.fromkeys(repositories))
    if any(not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) for repo in repos):
        raise ValueError("open-PR scan requires OWNER/REPO names")
    if len(repos) > 100:
        raise ValueError("open-PR scan supports at most 100 repositories")
    result = {repo: [] for repo in repos}
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
        data = batch.query(reader, 'query { ' + ' '.join(selections) + ' rateLimit { cost remaining resetAt } }')
        pending = {}
        for alias, repo in aliases.items():
            repository = data.get(alias)
            if not isinstance(repository, dict) or str(repository.get("nameWithOwner", "")).casefold() != repo.casefold():
                raise GitHubReadShapeError("open-PR repository unavailable or mismatched")
            nodes, cursor, total = batch.page(repository.get("pullRequests"))
            if total != totals.setdefault(repo, total):
                raise GitHubReadShapeError("open-PR inventory changed during pagination")
            for node in nodes:
                number = node.get("number")
                if type(number) is not int or number <= 0 or any(pr["number"] == number for pr in result[repo]) or node.get("state") != "OPEN":
                    raise GitHubReadShapeError("invalid or duplicate open PR")
                labels = batch.complete(reader, repo, node, "labels")
                result[repo].append({
                    "number": number, "title": node["title"], "body": node["body"],
                    "state": "open", "draft": node["isDraft"], "created_at": node["createdAt"],
                    "user": batch.rest_author(node["author"]), "labels": labels,
                    "html_url": node["url"], "head": {"sha": node["headRefOid"], "ref": node["headRefName"],
                        "repo": {"full_name": (node["headRepository"] or {}).get("nameWithOwner")}},
                    "base": {"ref": node["baseRefName"]},
                })
            if cursor:
                if cursor in seen[repo]:
                    raise GitHubReadShapeError("open-PR cursor repeated")
                seen[repo].add(cursor)
                pending[repo] = cursor
            elif len(result[repo]) != total:
                raise GitHubReadShapeError("incomplete open-PR inventory")
        cursors = pending
    raise GitHubReadShapeError("open-PR pagination exceeded bound")
