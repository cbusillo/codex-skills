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
from github_read import GitHubReader, GitHubReadError, GitHubReadShapeError, automation_only_gh_prefix_args, path_with_query
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
    month = re.match(r"(?:[A-Za-z]+ \d{1,2}, \d{4}|\d{1,2} [A-Za-z]+ \d{4})", value.strip())
    if month:
        for form in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(month[0], form).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def wait_rows(repo, issues, terms, owner, now):
    rows = []
    for issue in issues:
        if "pull_request" in issue or issue.get("state") != "open":
            continue
        # Reuse the human-attention classifier; do not filter by status labels
        # or whether an Owner/Director question has already been posted.
        sections = github_direction_next.section_map(issue.get("body") or "")
        status = next((v for k, v in sections.items() if k.casefold() == "current status"), issue.get("body") or "")
        item = {"repo": repo, "number": issue["number"], "url": issue.get("html_url"),
                "updated_at": issue.get("updated_at")}
        records = github_direction_next.waiting_records(item, status)
        candidates = []
        for record in records:
            reason = record["waiting_for"]
            scoped_terms = [term for term in terms if term.casefold() != "owner"] if re.search(
                r"\b(?:product|upstream|code) owner\b", reason, re.I) else terms
            kind = None if record["non_external"] else attention.current_director_wait(reason, scoped_terms, {owner.casefold()})
            if kind:
                candidates.append((reason, kind))
        if not candidates:
            continue
        wait = next((candidate for candidate in candidates if candidate[1] == "decision"), candidates[0])
        evidence = github_direction_next.milestone_wait_evidence(item, status, [])
        since = parse_date(evidence["since"])
        if sum(not record["no_current_wait"] for record in records) > 1:
            since = None  # A status-wide start cannot date one of several waits.
        verified = next((record.get("last_verified") for record in records if record.get("last_verified")), None)
        last_verified = parse_date(verified)
        rows.append({
            "repo": repo, "number": issue["number"], "title": issue.get("title"),
            "url": issue.get("html_url"), "waiting_for": wait[0], "wait_kind": wait[1],
            "waiting_since": since.isoformat() if since else None,
            "wait_age_days": max(0, (now - since).total_seconds() / 86400) if since else None,
            "wait_age_unknown": since is None,
            "recorded_at": evidence["recorded_at"],
            "last_verified": last_verified.isoformat() if last_verified else None,
            "last_verified_raw": verified,
            "verification_age_days": max(0, (now - last_verified).total_seconds() / 86400) if last_verified else None,
            "possibly_stale": last_verified is None or bool(since and last_verified < since),
            "staleness_basis": "verification_unknown" if last_verified is None else
                "verification_predates_wait" if since and last_verified < since else None,
            "verification_unknown": last_verified is None,
        })
    return rows


def collect_query(reader, owner, terms, now, issue_limit=1000, phrase="Waiting for"):
    query = f'user:{owner} is:issue is:open in:body "{phrase}"'
    result = {"waits": [], "complete": False, "errors": [],
              "repositories_with_matches": [], "query": query, "total_count": None,
              "scope": "Open issue bodies in the Director's repositories visible to GitHub Search; search indexing may lag live records"}
    rows, seen, totals = [], set(), []
    # GitHub Search exposes at most 1,000 results for a query. Read response
    # metadata on every page so timeouts/truncation cannot look complete.
    bound = min(issue_limit, 1000)
    read_count = 0
    for page in range(1, (bound + 99) // 100 + 1):
        try:
            response = reader.request("GET", path_with_query("/search/issues", {
                "q": query, "per_page": min(bound, 100), "page": page,
                "sort": "created", "order": "asc"}), step=f"director_wait_search_page_{page}", bucket="search")
            payload = response.body
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise GitHubReadShapeError("search response omitted items")
            total = payload.get("total_count")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise GitHubReadShapeError("search response omitted total_count")
            totals.append(total)
            result["total_count"] = max(totals)
            if payload.get("incomplete_results") is not False:
                result["errors"].append({"scope": "search", "error": "search coverage incomplete or unknown"})
            items = payload["items"]
            for entry in items[:bound - read_count]:
                read_count += 1
                try:
                    if not isinstance(entry, dict):
                        raise GitHubReadShapeError("invalid search issue")
                    repo_url = entry.get("repository_url", "")
                    match = re.search(r"/repos/([\w.-]+/[\w.-]+)$", repo_url)
                    if not match or match[1].split("/")[0].casefold() != owner.casefold():
                        raise GitHubReadShapeError("invalid search repository identity")
                    repo = match[1]
                    key = (repo, entry["number"])
                    if key in seen:
                        result["errors"].append({"scope": "search", "error": "duplicate issue during pagination"})
                        continue
                    seen.add(key)
                    rows.extend(wait_rows(repo, [entry], terms, owner, now))
                except (GitHubReadShapeError, ValueError, TypeError, KeyError) as error:
                    result["errors"].append({"scope": "search item", "error": str(error)})
            if read_count >= total or len(items) < min(bound, 100) or read_count >= bound:
                break
        except (GitHubReadError, GitHubReadShapeError, ValueError, TypeError) as error:
            result["errors"].append({"scope": "search", "error": str(error)})
            break
    if result["total_count"] is not None and read_count < result["total_count"]:
        result["errors"].append({"scope": "search", "error": "search result limit or pagination gap", "returned": read_count})
    if len(set(totals)) > 1:
        result["errors"].append({"scope": "search", "error": "search total changed during pagination"})
    result["waits"] = sorted(rows, key=lambda row: (row["repo"], row["number"]))
    result["repositories_with_matches"] = sorted({repo for repo, _ in seen})
    result["complete"] = bool(totals) and not result["errors"]
    return result


def collect(reader, owner, terms, now, issue_limit=1000):
    # Two scoped searches cover both supported status fields without relying
    # on provider-specific boolean query parsing or visiting each repository.
    reports = [collect_query(reader, owner, terms, now, issue_limit, phrase)
               for phrase in ("Waiting for", "Parked until")]
    rows = {(row["repo"], row["number"]): row for report in reports for row in report["waits"]}
    return {"waits": sorted(rows.values(), key=lambda row: (row["repo"], row["number"])),
            "complete": all(report["complete"] for report in reports),
            "errors": [error for report in reports for error in report["errors"]],
            "searches": [{key: report[key] for key in ("query", "total_count", "complete")} for report in reports],
            "repositories_with_matches": sorted({repo for report in reports for repo in report["repositories_with_matches"]}),
            "scope": reports[0]["scope"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--director-name", action="append", default=[])
    parser.add_argument("--issue-limit", type=int, default=1000)
    parser.add_argument("--deadline-seconds", type=float, default=600)
    args = parser.parse_args()
    if (not re.fullmatch(r"[\w.-]+", args.owner) or args.issue_limit <= 0
            or not math.isfinite(args.deadline_seconds) or args.deadline_seconds <= 0):
        parser.error("owner and positive finite bounds are required")
    people = attention.people_identities({args.owner.casefold()}, None)
    terms = [args.owner, "Director", "owner", *people["director_names"], *args.director_name]
    reader = GitHubReader(operation="github.direction.director_waits", strict_actor=True,
                          gh_prefix_args=automation_only_gh_prefix_args(),
                          deadline_at=time.time() + args.deadline_seconds)
    result = collect(reader, args.owner, terms, datetime.now(timezone.utc), args.issue_limit)
    if (people["status"] == "error" or people["director_ambiguous"]
            or (not people["director_names"] and not args.director_name)):
        result["complete"] = False
        result["errors"].append({"scope": "Director aliases", "error": "people identity unavailable or ambiguous; supply known --director-name aliases when no index exists"})
    result["people_status"] = people["status"]
    result["director_names"] = [*people["director_names"], *args.director_name]
    print(json.dumps(result))
    return int(not result["complete"])


if __name__ == "__main__":
    raise SystemExit(main())
