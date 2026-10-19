#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
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
from typing import Any

SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / "github/scripts"))
sys.path.insert(0, str(SKILLS / "github-work-rollup/scripts"))
from github_read import GitHubReader, GitHubReadError, GitHubReadShapeError, automation_only_gh_prefix_args, path_with_query
import github_direction_next
import github_plan_wait
import github_repository_scope as repository_scope
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
    month = re.search(r"[A-Za-z]+\.? \d{1,2},? \d{4}|\d{1,2} [A-Za-z]+\.? \d{4}", value.strip())
    if month:
        normalized = month[0].replace(".", "").replace(",", "")
        for form in ("%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(normalized, form).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def resolve_wait(repo, issue, comments, trusted=None):
    """Return the shared resolver's current wait and why it needs review, if it does.

    ``comments`` is None when the discussion was not completely read. Without a
    trusted readable record the recorded prose stays visible, marked for review.
    A trusted record that asks for review is its own reason, never prose.
    """
    if comments is None:
        return None, "discussion_unread"
    try:
        wait, recorded = github_plan_wait.resolve(
            issue, comments, github_plan_wait.authors(repo) if trusted is None else trusted)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return None, f"structured_wait_unreadable: {error}"
    if wait["state"] == "review":
        return None, "structured_wait_review" if recorded else "no_structured_record"
    return wait, None


def wait_rows(repo, issues, terms, owner, now, discussions=None, trusted=None, errors=None) -> list[dict[str, Any]]:
    """``discussions`` maps issue numbers to completely read comments."""
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
        prose = github_direction_next.waiting_records(item, status)
        wait_record, review = resolve_wait(repo, issue, (discussions or {}).get(issue["number"]), trusted)
        item.update({"structured_wait": wait_record} if wait_record else {"wait_review_required": True})
        # The status line's verification still dates the issue's recorded state.
        verified = next((record.get("last_verified") for record in prose if record.get("last_verified")), None)
        last_verified = parse_date(verified)
        missing_date_basis = ("verification_commit_only" if verified and re.fullmatch(r"[0-9a-fA-F]{7,40}", verified.strip())
                              else "verification_unparseable" if verified else "verification_missing")
        if review and (review == "structured_wait_review" or review.startswith("structured_wait_unreadable")):
            # A record that cannot be read may hide or release a hold, and a trusted
            # record may ask for review. Like the goal board, keep the issue as an
            # unknown wait for review and never resolve it from prose. Only an
            # unreadable record leaves coverage partial.
            if review != "structured_wait_review" and errors is not None:
                errors.append({"scope": "structured wait", "repo": repo, "number": issue["number"], "error": review})
            rows.append({
                "repo": repo, "number": issue["number"], "title": issue.get("title"),
                "url": issue.get("html_url"), "waiting_for": "unknown: the structured wait record needs review",
                "wait_kind": "unresolved", "waiting_since": None, "wait_age_days": None, "wait_age_unknown": True,
                "recorded_at": None, "last_verified": last_verified.isoformat() if last_verified else None,
                "last_verified_raw": verified,
                "verification_age_days": max(0, (now - last_verified).total_seconds() / 86400) if last_verified else None,
                "possibly_stale": True, "staleness_basis": review.split(":")[0],
                "verification_unknown": last_verified is None, "wait_source": "unresolved_structured_wait",
                "wait_review_required": True, "wait_review_reason": review,
            })
            continue
        records = github_direction_next.waiting_records(item, status)
        candidates = []
        for record in records:
            reason = record["waiting_for"]
            classified_reason = re.sub(r"\b(?:product|upstream|code) owner\b", "third party", reason, flags=re.I)
            kind = None if record["non_external"] else attention.current_director_wait(classified_reason, terms, {owner.casefold()})
            if kind:
                candidates.append((reason, kind))
        if not candidates:
            continue
        wait = next((candidate for candidate in candidates if candidate[1] == "decision"), candidates[0])
        evidence = github_direction_next.milestone_wait_evidence(item, status, [])
        since = parse_date(evidence["since"])
        inline_since = re.search(r";\s*since\s+(.+)$", wait[0], re.I)
        if inline_since:
            since = parse_date(inline_since[1])
        elif sum(not record["no_current_wait"] for record in records) > 1:
            since = None  # A status-wide start cannot date one of several waits.
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
            "staleness_basis": missing_date_basis if last_verified is None else
                "verification_predates_wait" if since and last_verified < since else None,
            "verification_unknown": last_verified is None,
            "wait_source": "structured_wait" if wait_record else "legacy_wait_review",
            "wait_review_required": wait_record is None,
            "wait_review_reason": review,
        })
    return rows


def read_discussion(reader, repo, number, discussions, errors):
    """Read one issue's complete discussion once per report; None when unread."""
    key = (repo.casefold(), number)
    if key not in discussions:
        try:
            comments = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="director_wait_comments")
            if not isinstance(comments, list) or not all(isinstance(comment, dict) for comment in comments):
                raise GitHubReadShapeError("issue comments response was not a list of comments")
        except (GitHubReadError, GitHubReadShapeError, ValueError, TypeError) as error:
            comments = None
            errors.append({"scope": "discussion", "repo": repo, "number": number, "error": str(error)})
        discussions[key] = comments
    return discussions[key]


def collect_query(reader, owner, terms, now, issue_limit=1000, phrase="Waiting for", *, include_archived=False,
                  where="body", discussions=None) -> dict[str, Any]:
    query = repository_scope.search_query(f'user:{owner} is:issue is:open in:{where} "{phrase}"', include_archived=include_archived)
    discussions = {} if discussions is None else discussions
    result = {"waits": [], "complete": False, "errors": [],
              "repositories_with_matches": [], "query": query, "total_count": None,
              "scope": "Open issues in the Director's repositories visible to GitHub Search, by wait fields in the body "
                       "or structured wait records in comments, resolved from each complete discussion; search indexing may lag live records"}
    rows, seen, totals = [], set(), []
    # GitHub Search exposes at most 1,000 results for a query. Read response
    # metadata on every page so timeouts/truncation cannot look complete.
    bound = min(issue_limit, 1000)
    read_count = 0
    for page in range(1, (bound + 99) // 100 + 1):
        try:
            response = reader.request("GET", path_with_query("/search/issues", {
                "q": query, "per_page": min(bound, 100), "page": page,
                "sort": "created", "order": "desc"}), step=f"director_wait_search_page_{page}", bucket="search")
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
                    comments = read_discussion(reader, repo, entry["number"], discussions, result["errors"])
                    rows.extend(wait_rows(repo, [entry], terms, owner, now, {entry["number"]: comments},
                                          errors=result["errors"]))
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


def collect(reader, owner, terms, now, issue_limit=1000, *, include_archived=False) -> dict[str, Any]:
    # Scoped searches cover both supported status fields and structured wait
    # records written only in comments, without relying on provider-specific
    # boolean query parsing or visiting each repository. Each candidate's
    # discussion is read once and resolved with the shared wait resolver.
    discussions: dict[tuple[str, int], Any] = {}
    reports = [collect_query(reader, owner, terms, now, issue_limit, phrase, include_archived=include_archived,
                             where=where, discussions=discussions)
               for phrase, where in (("Waiting for", "body"), ("Parked until", "body"), ("waiting_for", "comments"))]
    rows = {(row["repo"], row["number"]): row for report in reports for row in report["waits"]}
    return {"waits": sorted(rows.values(), key=lambda row: (row["repo"], row["number"])),
            "complete": all(report["complete"] for report in reports),
            "errors": [error for report in reports for error in report["errors"]],
            "searches": [{key: report[key] for key in ("query", "total_count", "complete")} for report in reports],
            "repositories_with_matches": sorted({repo for report in reports for repo in report["repositories_with_matches"]}),
            "scope": reports[0]["scope"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    repository_scope.add_include_archived_argument(parser)
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
    result = collect(reader, args.owner, terms, datetime.now(timezone.utc), args.issue_limit, include_archived=args.include_archived)
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
