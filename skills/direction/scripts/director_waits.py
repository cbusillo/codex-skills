#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Read every visible open Director wait, independent of labels and questions."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / "github/scripts"))
sys.path.insert(0, str(SKILLS / "github-work-rollup/scripts"))
import github_direction_next
import github_identity
import github_plan_wait
import github_repository_scope as repository_scope
import github_unanswered_comments as attention
import question_id
from github_plan_status import questions
from github_read import (
    GitHubReader,
    GitHubReadError,
    GitHubReadShapeError,
    automation_only_gh_prefix_args,
    path_with_query,
)


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


class WaitResolution(NamedTuple):
    record: dict[str, Any] | None
    review: str | None
    source: dict[str, Any] | None = None


def resolve_wait(repo: str, issue: dict[str, Any], comments: list[dict[str, Any]] | None,
                 trusted: set[str] | None = None) -> WaitResolution:
    if comments is None:
        return WaitResolution(None, "discussion_unread")
    try:
        trusted = github_plan_wait.authors(repo) if trusted is None else trusted
        wait, recorded = github_plan_wait.resolve(issue, comments, trusted)
        if wait["state"] == "review":
            return WaitResolution(None, "structured_wait_review" if recorded else "no_structured_record")
        return WaitResolution(wait, None, github_plan_wait.current_source(issue, comments, trusted))
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return WaitResolution(None, f"structured_wait_unreadable: {error}")


def wait_rows(repo, issues, terms, owner, now, discussions=None, trusted=None, errors=None) -> list[dict[str, Any]]:
    """``discussions`` maps issue numbers to completely read comments."""
    rows = []
    foreign = repo.split("/")[0].casefold() != owner.casefold()
    if foreign:
        terms = [term for term in terms if term.casefold() not in {"director", "owner"} or term.casefold() == owner.casefold()]
    for issue in issues:
        if "pull_request" in issue or issue.get("state") != "open":
            continue
        # Reuse the human-attention classifier; do not filter by status labels
        # or whether an Owner/Director question has already been posted.
        sections = github_direction_next.section_map(issue.get("body") or "")
        status = next((v for k, v in sections.items() if k.casefold() == "current status"), issue.get("body") or "")
        item = {"repo": repo, "number": issue["number"], "url": issue.get("html_url"),
                "updated_at": issue.get("updated_at")}
        evidence = github_direction_next.milestone_wait_evidence(item, status, [])
        resolution = resolve_wait(repo, issue, (discussions or {}).get(issue["number"]), trusted)
        wait_record, review = resolution.record, resolution.review
        item.update({"structured_wait": wait_record} if wait_record else {"wait_review_required": True})
        source = resolution.source or {}
        verified = source.get("created_at")
        last_verified = parse_date(verified)
        if review and (review == "structured_wait_review" or review.startswith("structured_wait_unreadable")):
            if review != "structured_wait_review" and errors is not None:
                errors.append({"scope": "structured wait", "repo": repo, "number": issue["number"], "error": review})
            rows.append({
                "repo": repo, "number": issue["number"], "title": issue.get("title"), "untrusted": foreign,
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
        if foreign:
            item["untrusted"] = True
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
        since = parse_date(evidence["since"])
        inline_since = re.search(r";\s*since\s+(.+)$", wait[0], re.I)
        if inline_since:
            since = parse_date(inline_since[1])
        elif sum(not record["no_current_wait"] for record in records) > 1:
            since = None  # A status-wide start cannot date one of several waits.
        rows.append({
            "repo": repo, "number": issue["number"], "title": issue.get("title"), "untrusted": foreign,
            "url": issue.get("html_url"), "waiting_for": wait[0], "wait_kind": wait[1],
            "waiting_since": since.isoformat() if since else None,
            "wait_age_days": max(0, (now - since).total_seconds() / 86400) if since else None,
            "wait_age_unknown": since is None,
            "recorded_at": evidence["recorded_at"],
            "last_verified": last_verified.isoformat() if last_verified else None,
            "last_verified_raw": verified,
            "verification_age_days": max(0, (now - last_verified).total_seconds() / 86400) if last_verified else None,
            "possibly_stale": last_verified is None or bool(since and last_verified < since),
            "staleness_basis": "verification_missing" if last_verified is None else
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


def normalized_comments(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**comment, "author": (comment.get("user") or {}).get("login") or comment.get("author") or "",
             "url": comment.get("html_url") or comment.get("url")} for comment in comments]


def linked_questions(reader: GitHubReader, wait: dict[str, Any], owner: str,
                       discussions: dict[tuple[str, int], Any], errors: list[dict[str, Any]],
                       related_comments: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    if wait.get("state") != "waiting":
        return []
    found = []
    for match in questions.COMMENT_URL.finditer(wait.get("evidence", "")):
        repo, number = match[1], int(match[2])
        comments = read_discussion(reader, repo, number, discussions, errors)
        if comments is None:
            found.append({"url": match[0], "status": "unavailable", "answers": []})
            continue
        normalized = normalized_comments(comments)
        trusted = github_plan_wait.trusted_authors(owner, configured_bots=github_identity.configured_bot_logins(),
                                                  automation=github_identity.automation_login())
        sources = [comment for comment in normalized if comment["url"] == match[0]]
        if len(sources) != 1:
            errors.append({"scope": "question evidence", "url": match[0], "error": "source comment is missing or ambiguous"})
            found.append({"url": match[0], "status": "unavailable", "answers": []})
            continue
        if sources[0]["author"].casefold() not in trusted:
            continue
        source_urls = {comment["url"] for comment in normalized}
        related = [comment for comment in normalized_comments(related_comments or []) if comment["url"] not in source_urls]
        matched = [question for question in questions.questions(normalized, owner, trusted, related, recorded_decisions_only=True)
                   if question["url"] == match[0]]
        queue = question_id.configured_queue()
        if matched and not matched[0]["answers"] and queue:
            queue_repo, queue_number = queue.split("#")
            queue_comments = read_discussion(reader, queue_repo, int(queue_number), discussions, errors)
            matched = [question for question in questions.questions(normalized, owner, trusted,
                       [*related, *normalized_comments(queue_comments or [])], recorded_decisions_only=True)
                       if question["url"] == match[0]]
        found.extend(matched)
    return found


def collect_query(reader, owner, terms, now, issue_limit=1000, phrase="Waiting for", *, include_archived=False,
                  where="body", discussions: dict[tuple[str, int], Any] | None = None,
                  recipient: str | None = None, recipient_term: str | None = None) -> dict[str, Any]:
    search = (f'-user:{owner} is:issue is:open {recipient}' if recipient
              else f'user:{owner} is:issue is:open in:{where} "{phrase}"')
    query = repository_scope.search_query(search, include_archived=include_archived)
    discussions = {} if discussions is None else discussions
    result: dict[str, Any] = {"waits": [], "needs_status": [], "complete": False, "errors": [],
              "repositories_with_matches": [], "query": query, "total_count": None,
              "scope": "Visible open issues in owned repositories and recipient-named issues elsewhere, by wait fields in the body "
                       "or structured wait records in comments, resolved from live discussions; recorded answers go to needs_status, "
                       "preserving their holds. Search indexing may lag discovery"}
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
                    if not match:
                        raise GitHubReadShapeError("invalid search repository identity")
                    repo = match[1]
                    key = (repo, entry["number"])
                    if key in seen:
                        result["errors"].append({"scope": "search", "error": "duplicate issue during pagination"})
                        continue
                    seen.add(key)
                    comments = read_discussion(reader, repo, entry["number"], discussions, result["errors"])
                    if recipient_term and comments is not None:
                        texts = "\n".join(str(source.get("body") or "") for source in [entry, *comments])
                        if not github_plan_wait.names_recipient(texts, [recipient_term], {owner.casefold()}):
                            continue
                    waits = wait_rows(repo, [entry], terms, owner, now, {entry["number"]: comments}, errors=result["errors"])
                    resolution = resolve_wait(repo, entry, comments)
                    wait_record = resolution.record
                    decisions = []
                    question_states = []
                    if waits and wait_record is not None:
                        question_states = linked_questions(reader, wait_record, owner, discussions, result["errors"], comments)
                        decisions = [question for question in question_states if question["answers"]]
                    if question_states and all(question["answers"] for question in question_states):
                        result["needs_status"].extend({**row, "next_actor": "agent", "recorded_decisions": decisions,
                                                     "structured_wait": resolution.record,
                                                     "next_action": "Read the recorded answers; leave the foreign record to its owner." if row["untrusted"] else
                                                     "Read the recorded answers and reconcile within existing authority; preserve every other hold."}
                                                    for row in waits)
                    else:
                        rows.extend({**row, **({"recorded_decisions": decisions} if decisions else {})} for row in waits)
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


def collect(reader, owner, terms, now, issue_limit=1000, *, include_archived=False, include_owned: bool = True) -> dict[str, Any]:
    # Scoped searches cover both supported status fields and structured wait
    # records written only in comments, without relying on provider-specific
    # boolean query parsing or visiting each repository. Each candidate's
    # discussion is read once and resolved with the shared wait resolver.
    discussions: dict[tuple[str, int], Any] = {}
    reports = [collect_query(reader, owner, terms, now, issue_limit, phrase, include_archived=include_archived,
                             where=where, discussions=discussions)
               for phrase, where in (("Waiting for", "body"), ("Parked until", "body"), ("waiting_for", "comments"))] if include_owned else []
    reports.extend(collect_query(reader, owner, terms, now, issue_limit, include_archived=include_archived,
                                 discussions=discussions, recipient=query, recipient_term=term)
                   for term, query in github_plan_wait.recipient_queries(owner, terms))
    rows = {(row["repo"], row["number"]): row for report in reports for row in report["waits"]}
    needs_status = {(row["repo"], row["number"]): row for report in reports for row in report["needs_status"]}
    return {"waits": sorted(rows.values(), key=lambda row: (row["repo"], row["number"])),
            "needs_status": sorted(needs_status.values(), key=lambda row: (row["repo"], row["number"])),
            "complete": all(report["complete"] for report in reports),
            "errors": [error for report in reports for error in report["errors"]],
            "searches": [{key: report[key] for key in ("query", "total_count", "complete")} for report in reports],
            "repositories_with_matches": sorted({repo for report in reports for repo in report["repositories_with_matches"]}),
            "scope": reports[0]["scope"]}


def collect_for_owner(reader: GitHubReader, owner: str, now: datetime, issue_limit: int = 1000, *,
                      director_names: list[str] | None = None, include_archived: bool = False,
                      include_owned: bool = True) -> dict[str, Any]:
    extra_names = director_names or []
    people = attention.people_identities({owner.casefold()}, None)
    terms = [owner, "Director", "owner", *people["director_names"], *extra_names]
    result = collect(reader, owner, terms, now, issue_limit, include_archived=include_archived, include_owned=include_owned)
    if (people["status"] == "error" or people["director_ambiguous"]
            or (not people["director_names"] and not extra_names)):
        result["complete"] = False
        result["errors"].append({"scope": "Director aliases", "error": "people identity unavailable or ambiguous; supply known --director-name aliases when no index exists"})
    result["people_status"] = people["status"]
    result["director_names"] = [*people["director_names"], *extra_names]
    return result


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
    reader = GitHubReader(operation="github.direction.director_waits", strict_actor=True,
                          gh_prefix_args=automation_only_gh_prefix_args(),
                          include_archived=args.include_archived,
                          deadline_at=time.time() + args.deadline_seconds)
    result = collect_for_owner(reader, args.owner, datetime.now(timezone.utc), args.issue_limit,
                               director_names=args.director_name, include_archived=args.include_archived)
    print(json.dumps(result))
    return int(not result["complete"])


if __name__ == "__main__":
    raise SystemExit(main())
