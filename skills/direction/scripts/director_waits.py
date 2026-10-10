#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Read every visible open Director wait, independent of labels and questions."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
import time

SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / "github/scripts"))
sys.path.insert(0, str(SKILLS / "github-work-rollup/scripts"))
from github_read import GitHubReader, GitHubReadError, GitHubReadShapeError
import github_identity
import github_direction_next
import github_unanswered_comments as attention


def parse_date(value):
    if not isinstance(value, str):
        return None
    iso = re.match(r"\d{4}-\d{2}-\d{2}(?:T[0-9:.+-]+Z?)?", value.strip())
    if iso:
        try:
            parsed = datetime.fromisoformat(iso[0].replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        except ValueError:
            return None
    month = re.match(r"[A-Za-z]+ \d{1,2}, \d{4}", value.strip())
    if month:
        try:
            return datetime.strptime(month[0], "%B %d, %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def wait_rows(repo, issues, terms, owner, now):
    rows = []
    for issue in issues:
        if "pull_request" in issue or issue.get("state") != "open":
            continue
        # Reuse the human-attention classifier; do not filter by status labels
        # or whether an Owner/Director question has already been posted.
        wait = attention.director_wait_reason(repo, issue, terms, {owner.casefold()})
        if wait is None:
            continue
        sections = github_direction_next.section_map(issue.get("body") or "")
        status = next((v for k, v in sections.items() if k.casefold() == "current status"), "")
        item = {"repo": repo, "number": issue["number"], "updated_at": issue.get("updated_at")}
        evidence = github_direction_next.milestone_wait_evidence(item, status, [])
        since = parse_date(evidence["since"])
        verified = re.search(r"(?im)^\s*(?:[-*]\s+)?Last verified:\s*(.+)$", status)
        last_verified = parse_date(verified[1]) if verified else None
        rows.append({
            "repo": repo, "number": issue["number"], "title": issue.get("title"),
            "url": issue.get("html_url"), "waiting_for": wait[0], "wait_kind": wait[1],
            "waiting_since": since.isoformat() if since else None,
            "wait_age_days": max(0, (now - since).total_seconds() / 86400) if since else None,
            "wait_age_unknown": since is None,
            "last_verified": last_verified.isoformat() if last_verified else None,
            "possibly_stale": bool(since and last_verified and since < last_verified),
        })
    return rows


def collect(reader, owner, terms, now, repo_limit=1000, issue_limit=10000):
    rows, errors = [], []
    app = github_identity.github_app_config() is not None
    try:
        repos = reader.paged_json("/installation/repositories" if app else "/user/repos",
            collection_key="repositories" if app else None, step_prefix="director_wait_repositories",
            params={} if app else {"affiliation": "owner,collaborator,organization_member"}, limit=repo_limit + 1)
    except (GitHubReadError, GitHubReadShapeError) as error:
        return {"waits": [], "complete": False, "errors": [{"scope": "repositories", "error": str(error)}]}
    if len(repos) > repo_limit:
        errors.append({"scope": "repositories", "error": "repository limit reached"})
    scanned = []
    for entry in repos[:repo_limit]:
        repo = entry.get("full_name")
        if not isinstance(repo, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            errors.append({"scope": "repositories", "error": "invalid repository identity"})
            continue
        if repo.split("/")[0].casefold() != owner.casefold():
            continue
        try:
            issues = reader.paged_json(f"/repos/{repo}/issues", step_prefix="director_wait_issues",
                                      params={"state": "open"}, limit=issue_limit + 1)
            if len(issues) > issue_limit:
                errors.append({"scope": repo, "error": "issue limit reached"})
            rows.extend(wait_rows(repo, issues[:issue_limit], terms, owner, now))
            scanned.append(repo)
        except (GitHubReadError, GitHubReadShapeError, ValueError, TypeError, KeyError) as error:
            errors.append({"scope": repo, "error": str(error)})
    return {"waits": sorted(rows, key=lambda row: (row["repo"], row["number"])),
            "complete": not errors, "errors": errors, "repositories_scanned": scanned,
            "scope": "Director repositories visible to the configured automation identity; closed issues and PRs excluded"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--director-name", action="append", default=[])
    parser.add_argument("--repo-limit", type=int, default=1000)
    parser.add_argument("--issue-limit", type=int, default=10000)
    parser.add_argument("--deadline-seconds", type=float, default=600)
    args = parser.parse_args()
    if (not re.fullmatch(r"[\w.-]+", args.owner) or min(args.repo_limit, args.issue_limit) <= 0
            or not math.isfinite(args.deadline_seconds) or args.deadline_seconds <= 0):
        parser.error("owner and positive finite bounds are required")
    people = attention.people_identities({args.owner.casefold()}, None)
    terms = [args.owner, "Director", "owner", *people["director_names"], *args.director_name]
    reader = GitHubReader(operation="github.direction.director_waits", strict_actor=True,
                          deadline_at=time.time() + args.deadline_seconds)
    result = collect(reader, args.owner, terms, datetime.now(timezone.utc), args.repo_limit, args.issue_limit)
    if people["status"] == "error" or people["director_ambiguous"]:
        result["complete"] = False
        result["errors"].append({"scope": "Director aliases", "error": "people identity unavailable or ambiguous"})
    result["people_status"] = people["status"]
    print(json.dumps(result))
    return int(not result["complete"])


if __name__ == "__main__":
    raise SystemExit(main())
