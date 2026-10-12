#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Count open issues and PRs across an owner's visible, non-archived repositories."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
import time

CATALOG = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(CATALOG))
sys.path.insert(0, str(CATALOG / "skills/github/scripts"))
from skills.github.scripts.github_read import GitHubReader, GitHubReadError, automation_only_gh_prefix_args

QUERY = """query SupervisorFleetCounts($owner: String!, $cursor: String, $pageSize: Int!) {
  repositoryOwner(login: $owner) {
    repositories(first: $pageSize, after: $cursor, ownerAffiliations: [OWNER],
                 isArchived: false, orderBy: {field: NAME, direction: ASC}) {
      totalCount
      nodes {
        nameWithOwner
        isArchived
        issues(states: [OPEN]) { totalCount }
        pullRequests(states: [OPEN]) { totalCount }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""


def count(value):
    if type(value) is not int or value < 0:
        raise ValueError("count must be a nonnegative integer")
    return value


def fleet_counts(reader, owner, *, page_size=100, max_pages=100):
    """One query per repository page; never return totals from an incomplete read."""
    rows, seen_cursors = {}, set()
    cursor = None
    expected_total = None
    for page in range(max_pages):
        result = reader.graphql_json(QUERY, {"owner": owner, "cursor": cursor,
            "pageSize": page_size}, step=f"fleet_counts_page_{page + 1}")
        if not result.ok:
            raise GitHubReadError("fleet count read failed", result=result,
                                  diagnostics=reader.diagnostics())
        body = result.body
        if not isinstance(body, dict) or body.get("errors"):
            raise ValueError("GraphQL count read returned errors or invalid data")
        connection = body["data"]["repositoryOwner"]["repositories"]
        total = count(connection["totalCount"])
        if expected_total is not None and total != expected_total:
            raise ValueError("repository inventory changed during pagination; take a new snapshot")
        expected_total = total
        nodes = connection["nodes"]
        if not isinstance(nodes, list):
            raise ValueError("repository nodes must be a list")
        for node in nodes:
            name = node["nameWithOwner"]
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name)
                    or name.split("/")[0].casefold() != owner.casefold()
                    or node["isArchived"] is not False or name.casefold() in rows):
                raise ValueError("unexpected or duplicate repository in count read")
            rows[name.casefold()] = {"repository": name,
                "open_issues": count(node["issues"]["totalCount"]),
                "open_prs": count(node["pullRequests"]["totalCount"])}
        info = connection["pageInfo"]
        if type(info["hasNextPage"]) is not bool:
            raise ValueError("pagination flag must be a boolean")
        if not info["hasNextPage"]:
            if len(rows) != expected_total:
                raise ValueError("repository inventory incomplete")
            repos = sorted(rows.values(), key=lambda row: row["repository"].casefold())
            return {"repositories": repos, "totals": {"repositories": len(repos),
                "open_issues": sum(row["open_issues"] for row in repos),
                "open_prs": sum(row["open_prs"] for row in repos)}, "pages": page + 1}
        cursor = info["endCursor"]
        if not nodes or not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise ValueError("pagination cursor did not advance")
        seen_cursors.add(cursor)
    raise ValueError("repository pagination exceeded page bound")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--deadline-seconds", type=float, default=120)
    parser.add_argument("--service-snapshot", action="store_true",
        help="keep the read-only service sampling after a failed snapshot; failure remains ok: false")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.owner):
        parser.error("owner must be a GitHub account login")
    if (not 1 <= args.page_size <= 100 or args.max_pages < 1
            or not math.isfinite(args.deadline_seconds) or args.deadline_seconds <= 0):
        parser.error("page size must be 1..100, with positive page bound and deadline")
    reader = GitHubReader(operation="github.supervisor.fleet_counts", strict_actor=True,
        gh_prefix_args=automation_only_gh_prefix_args(), deadline_at=time.time() + args.deadline_seconds)
    sampled_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        data = fleet_counts(reader, args.owner, page_size=args.page_size, max_pages=args.max_pages)
    except (GitHubReadError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"ok": False, "owner": args.owner, "sampled_at": sampled_at,
            "error": str(error), "diagnostics": reader.diagnostics()}))
        return 0 if args.service_snapshot else 1
    print(json.dumps({"ok": True, "owner": args.owner, "sampled_at": sampled_at,
        "coverage": "repositories visible to the selected bulk reader identity (or main automation fallback); excludes archived",
        **data, "diagnostics": reader.diagnostics()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
