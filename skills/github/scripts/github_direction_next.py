#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read-only direction graph ranking, reusable by CLI and service callers.

The caller supplies a bounded node reader; this module performs no GitHub calls,
writes, owner-specific routing, or authorization decisions.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

import github_client
import github_milestone as github_milestone_core


LIVE_BREAKAGE_LABEL = "live-breakage"


def normalize_labels(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    names: list[str] = []
    for item in items:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def is_live_breakage(issue: dict[str, Any]) -> bool:
    return LIVE_BREAKAGE_LABEL in {name.casefold() for name in normalize_labels(issue.get("labels"))}


def discovery_scan(inventory: list[dict[str, Any]], scan_limit: int, selection_context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Owner-marked incidents do not consume the ordinary discovery allowance."""
    incidents = [item for item in inventory if is_live_breakage(item)]
    ordinary = [item for item in inventory if not is_live_breakage(item) and not repository_hold(selection_context or {}, item["repo"])]
    held = [item for item in inventory if not is_live_breakage(item) and repository_hold(selection_context or {}, item["repo"])]
    return incidents + ordinary[:scan_limit] + held[:scan_limit]


def compact_list_issue(repo: str, issue: dict[str, Any]) -> dict[str, Any]:
    milestone = issue.get("milestone") or {}
    state = issue.get("state")
    return {
        "repo": repo,
        "number": issue.get("number"),
        "title": issue.get("title"),
        "author": (issue.get("user") or {}).get("login") or issue.get("author"),
        "state": state.upper() if isinstance(state, str) else state,
        "created_at": issue.get("created_at") or issue.get("createdAt"),
        "updated_at": issue.get("updated_at") or issue.get("updatedAt"),
        "url": issue.get("html_url") or issue.get("url"),
        "labels": normalize_labels(issue.get("labels")),
        "milestone": milestone.get("title") if isinstance(milestone, dict) else None,
    }


def section_map(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", body or ""))
    for idx, match in enumerate(matches):
        name = match.group(1).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        sections[name] = body[start:end].strip()
    return sections


def relationship_refs(items: list[dict[str, Any]], issue_repo: str) -> list[str]:
    return [
        f"#{item['number']}" if item["repo"] == issue_repo else f"{item['repo']}#{item['number']}"
        for item in items
    ]


def next_milestone_context(issue: dict[str, Any]) -> dict[str, Any] | None:
    milestone = issue.get("milestone")
    if not isinstance(milestone, dict):
        return None
    normalized = github_milestone_core.normalize_milestone(milestone)
    return {
        "number": normalized.get("number"),
        "title": normalized.get("title"),
        "state": normalized.get("state"),
        "created_at": normalized.get("created_at"),
        "due_on": normalized.get("due_on"),
        "url": normalized.get("url"),
    }


def next_plan_status(issue: dict[str, Any], config: dict[str, Any]) -> str | None:
    issue_label_names = {name.casefold() for name in normalize_labels(issue.get("labels"))}
    plan_labels = config.get("labels") or {}
    for status in ("done", "stale", "blocked", "waiting", "active"):
        label = plan_labels.get(status)
        if isinstance(label, str) and label.casefold() in issue_label_names:
            return status
    return None


def next_relationship_summary(
    relationships: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    blocked_by = relationships.get("blocked_by") or []
    blocking = relationships.get("blocking") or []
    sub_issues = relationships.get("sub_issues") or []
    open_blockers = [item for item in blocked_by if item.get("state") == "open"]
    open_blocking = [item for item in blocking if item.get("state") == "open"]
    open_sub_issues = [item for item in sub_issues if item.get("state") == "open"]
    return {
        "blocked_by": blocked_by,
        "blocking": blocking,
        "sub_issues": sub_issues,
        "open_blockers": open_blockers,
        "open_blocking": open_blocking,
        "open_sub_issues": open_sub_issues,
    }


def next_static_exclusion(
    issue: dict[str, Any],
    *,
    config: dict[str, Any],
    focus: str | None,
) -> dict[str, Any] | None:
    state = str(issue.get("state") or "").casefold()
    status = next_plan_status(issue, config)
    base = {
        **compact_list_issue(str(issue.get("repo") or ""), issue),
        "plan_status": status,
        "focus": focus,
        "milestone": next_milestone_context(issue),
    }
    if state != "open" or status == "done":
        return {**base, "exclusion": "completed", "evidence": []}
    if status == "stale":
        return {**base, "exclusion": "stale_needs_review", "evidence": []}
    return None


def evaluate_next_plan(
    issue: dict[str, Any],
    *,
    config: dict[str, Any],
    focus: str | None,
    relationships: dict[str, list[dict[str, Any]]] | None,
    relationship_error: str | None = None,
) -> tuple[str, dict[str, Any]]:
    status = next_plan_status(issue, config)
    milestone = next_milestone_context(issue)
    base = {
        **compact_list_issue(str(issue.get("repo") or ""), issue),
        "plan_status": status,
        "focus": focus,
        "milestone": milestone,
    }
    static_exclusion = next_static_exclusion(issue, config=config, focus=focus)
    if static_exclusion is not None:
        return "excluded", static_exclusion
    if relationship_error is not None or relationships is None:
        return "excluded", {
            **base,
            "exclusion": "unknown_dependencies",
            "evidence": [],
            "detail": relationship_error or "dependency reads unavailable",
        }

    summary = next_relationship_summary(relationships)
    open_blockers = summary["open_blockers"]
    if open_blockers:
        return "excluded", {
            **base,
            "exclusion": "blocked_by_open_dependency",
            "evidence": relationship_refs(open_blockers, str(issue.get("repo") or "")),
            "blocked_by": open_blockers,
        }
    if status == "blocked":
        return "excluded", {
            **base,
            "exclusion": "label_blocked_without_native_edge",
            "evidence": [],
            "inconsistency": True,
        }
    normalized_focus = focus.casefold() if isinstance(focus, str) else None
    if status == "waiting" or normalized_focus == "waiting":
        return "excluded", {**base, "exclusion": "waiting", "evidence": []}
    if normalized_focus == "later":
        return "excluded", {**base, "exclusion": "later_focus", "evidence": []}
    open_sub_issues = summary["open_sub_issues"]
    if open_sub_issues:
        return "excluded", {
            **base,
            "exclusion": "delegated_to_open_sub_issues",
            "evidence": relationship_refs(open_sub_issues, str(issue.get("repo") or "")),
            "open_sub_issues": open_sub_issues,
        }

    notes: list[str] = []
    if milestone and milestone.get("state") == "closed":
        notes.append("milestone_closed_but_plan_open")
    reasons = ["no_open_blockers"]
    if focus:
        reasons.append(f"focus_{focus.casefold()}")
    if summary["open_blocking"]:
        reasons.append(f"unblocks_{len(summary['open_blocking'])}_open_plan(s)")
    return "candidate", {
        **base,
        "blocked_by": [],
        "blocking": summary["open_blocking"],
        "sub_issues": {
            "open": len(open_sub_issues),
            "closed": len(summary["sub_issues"]) - len(open_sub_issues),
        },
        "reasons": reasons,
        "notes": notes,
    }


def rank_next_candidates(
    candidates: list[dict[str, Any]],
    *,
    direction_milestones: list[str] | None = None,
) -> None:
    listed_order = {
        title: index
        for index, title in enumerate(direction_milestones or [])
    }

    def milestone_rank(candidate: dict[str, Any]) -> tuple[int, str, int]:
        milestone = candidate.get("milestone")
        if not isinstance(milestone, dict) or milestone.get("state") != "open":
            return len(listed_order) + 1, "9999-12-31T00:00:00Z", 0
        if direction_milestones is not None:
            title = milestone.get("title")
            return (
                listed_order.get(title, len(listed_order) + 1) if isinstance(title, str) else len(listed_order) + 1,
                "",
                0,
            )
        created_at = milestone.get("created_at")
        number = milestone.get("number")
        return (
            0,
            created_at if isinstance(created_at, str) else "9999-12-31T00:00:00Z",
            number if isinstance(number, int) else 0,
        )

    candidates.sort(
        key=lambda candidate: (
            milestone_rank(candidate),
            -len(candidate.get("blocking") or []),
            str(candidate.get("created_at") or "9999-12-31T00:00:00Z"),
            int(candidate.get("number") or 0),
        )
    )
    for rank, item in enumerate(candidates, start=1):
        item["rank"] = rank


def is_direction_repository(repo: str) -> bool:
    parts = repo.split("/")
    return len(parts) == 2 and bool(parts[0]) and parts[1].casefold() == "direction"


def discussion_snapshot(issue: dict[str, Any], comments: list[dict[str, Any]], *, complete: bool) -> dict[str, Any]:
    """Carry the discussion, not a guess about arbitrary human prose, to selection."""
    snapshot = {
        "body": issue.get("body") or "",
        "author": (issue.get("user") or {}).get("login") or issue.get("author"),
        "updated_at": issue.get("updated_at"),
        "comments": [{
            "id": comment.get("id"), "author": (comment.get("user") or {}).get("login"),
            "updated_at": comment.get("updated_at"), "url": comment.get("html_url"),
            "body": comment.get("body") or "",
        } for comment in comments],
        "complete": complete,
    }
    return {**snapshot, "digest": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()}


def validate_selection_context(context: Any) -> dict[str, Any]:
    """A caller's current evidence snapshot is not a new planning database."""
    if not isinstance(context, dict) or set(context) - {"repository_holds", "issues"}:
        raise ValueError("selection context needs only repository_holds and issues objects")
    for field in ("repository_holds", "issues"):
        entries = context.get(field, {})
        if not isinstance(entries, dict):
            raise ValueError(f"selection context {field} must be an object")
        for key, entry in entries.items():
            pattern = r"[\w.-]+/[\w.-]+" + (r"#[1-9]\d*" if field == "issues" else "")
            if not re.fullmatch(pattern, key) or not isinstance(entry, dict):
                raise ValueError(f"invalid {field} entry: {key}")
            if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
                raise ValueError(f"{key} needs a reason")
            evidence = entry.get("evidence")
            if not isinstance(evidence, list) or not evidence or any(not isinstance(value, str) or not value.strip() for value in evidence):
                raise ValueError(f"{key} needs evidence sources")
            if field == "issues" and entry.get("state") not in {"available", "underway", "waiting", "ineligible"}:
                raise ValueError(f"{key} has an invalid review state")
    return context


def repository_hold(context: dict[str, Any], repo: str) -> dict[str, Any] | None:
    return next((value for key, value in context.get("repository_holds", {}).items() if key.casefold() == repo.casefold()), None)


def include_parent_context(
    item: dict[str, Any], parents: list[dict[str, Any]], *, complete: bool,
    tracking_roots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Do not let discovery bypass a whole-plan wait hidden above the child."""
    tracking = {(root["repo"].casefold(), root["number"]) for root in tracking_roots}
    discussion: dict[str, Any] = {**item.get("discussion", {}), "parents": parents, "ancestry_complete": complete}
    discussion["complete"] = bool(discussion.get("complete") and complete and all(
        (parent.get("discussion") or {}).get("complete") for parent in parents
    ))
    discussion.pop("digest", None)
    discussion["digest"] = hashlib.sha256(json.dumps(discussion, sort_keys=True).encode()).hexdigest()
    result = {**item, "discussion": discussion}
    if not complete:
        return {**result, "exclusion": "unknown_ancestry"}
    for parent in parents:
        if (parent["repo"].casefold(), parent["number"]) in tracking:
            continue
        if parent.get("exclusion") in {"waiting", "later_focus", "label_blocked_without_native_edge"}:
            return {**result, "exclusion": "parent_waiting", "waiting_on_parent": parent["url"]}
    return result


def overall_milestone_context(
    item: dict[str, Any], graph: dict[str, Any], milestone_titles: list[str],
    repository_milestones: dict[str, list[str] | None],
) -> dict[str, Any]:
    """Explain established waypoint links without changing eligibility or rank."""
    if item.get("client_request"):
        return {"state": "matched", "titles": [item["client_request"]["milestone"]], "source": "recorded_product_client", "basis": "client_request"}
    if item.get("via") and (item.get("milestone") or {}).get("title") in milestone_titles:
        return {"state": "matched", "titles": [item["milestone"]["title"]], "source": "native_track_path"}
    parent_keys = {(p["repo"].casefold(), p["number"]) for p in (item.get("discussion") or {}).get("parents", [])}
    blocking_keys = {(p["repo"].casefold(), p["number"]) for p in item.get("blocking", []) if p.get("state") == "open"}
    tracking_keys = {(p["repo"].casefold(), p["number"]) for p in graph.get("tracking_milestones", [])}
    linked_entries = [
        entry
        for entry in [*graph.get("candidates", []), *graph.get("excluded", []), *graph.get("tracking_milestones", [])]
        if (entry.get("milestone") or {}).get("title") in milestone_titles
        and (entry.get("via") or (entry["repo"].casefold(), entry["number"]) in tracking_keys)
        and (entry["repo"].casefold(), entry["number"]) in parent_keys | blocking_keys
    ]
    if linked_entries:
        linked = {entry["milestone"]["title"] for entry in linked_entries}
        blocked_match = any((entry["repo"].casefold(), entry["number"]) in blocking_keys for entry in linked_entries)
        return {"state": "matched", "titles": [title for title in milestone_titles if title in linked], "source": "native_track_links" if blocked_match else "native_track_ancestry"}
    repo = item["repo"]
    repository_titles = next((value for key, value in repository_milestones.items() if key.casefold() == repo.casefold()), None)
    local_title = (item.get("milestone") or {}).get("title")
    if (item.get("milestone") or {}).get("state") == "open" and local_title in milestone_titles and local_title in (repository_titles or []):
        return {"state": "matched", "titles": [local_title], "source": f"{repo}:DIRECTION.md", "basis": "exact_listed_title_match"}
    known_repo = any(key.casefold() == repo.casefold() for key in repository_milestones)
    ancestry_complete = (item.get("discussion") or {}).get("ancestry_complete")
    complete = known_repo and repository_titles is not None and ancestry_complete and not item.get("blocking") and graph.get("dependency_context", {}).get("complete", False)
    return {"state": "none_found" if complete else "unknown", "titles": [], "source": "checked_native_ancestry_and_repository_direction" if complete else "incomplete_context"}


def tooling_capacity_context(
    graph: dict[str, Any], discoveries: list[dict[str, Any]], *,
    milestone_titles: list[str], context: dict[str, Any],
    repository_waypoints: dict[str, list[str] | None], coverage_complete: bool,
) -> dict[str, Any]:
    """Only current caller evidence can distinguish a person from an event wait."""
    result: dict[str, Any] = {"admitted": False, "reason": "milestone_waits_not_proven"}
    if not coverage_complete or not graph.get("dependency_context", {}).get("complete"):
        return {**result, "reason": "incomplete_milestone_coverage"}
    reviews = {key.casefold(): value for key, value in context.get("issues", {}).items()}
    entries = [*graph.get("candidates", []), *graph.get("excluded", []), *discoveries]
    by_key = {(entry["repo"].casefold(), entry["number"]): entry for entry in entries}
    entry_keys = set(by_key)
    matched = {key for key, entry in by_key.items() if overall_milestone_context(entry, graph, milestone_titles, repository_waypoints)["state"] == "matched"}
    pending = list(matched)
    while pending:
        key = pending.pop()
        entry = by_key[key]
        if entry.get("exclusion") not in {"blocked_by_open_dependency", "delegated_to_open_sub_issues"}:
            continue
        for dependency in [*entry.get("blocked_by", []), *entry.get("open_sub_issues", [])]:
            dependency_key = (dependency["repo"].casefold(), dependency["number"])
            if dependency_key not in entry_keys:
                return {**result, "reason": "incomplete_milestone_dependencies", "issue": f"{entry['repo']}#{entry['number']}"}
            if dependency_key not in matched:
                matched.add(dependency_key)
                pending.append(dependency_key)
    unresolved = {key for key in matched if by_key[key].get("exclusion") in {"blocked_by_open_dependency", "delegated_to_open_sub_issues"} and (by_key[key].get("blocked_by") or by_key[key].get("open_sub_issues"))}
    settled = matched - unresolved
    while unresolved:
        ready = {key for key in unresolved if all(
            (dependency["repo"].casefold(), dependency["number"]) in settled
            for dependency in [*by_key[key].get("blocked_by", []), *by_key[key].get("open_sub_issues", [])]
        )}
        if not ready:
            return {**result, "reason": "milestone_dependency_cycle"}
        settled.update(ready)
        unresolved.difference_update(ready)
    frontier: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("exclusion") in {"completed", "outside_direction_tracks", "tracking", "tracking_without_open_work"}:
            continue
        milestone = overall_milestone_context(entry, graph, milestone_titles, repository_waypoints)
        if (entry["repo"].casefold(), entry["number"]) in matched:
            milestone = {"state": "matched"}
        if milestone["state"] == "unknown":
            # A complete graph and ancestry can rule out native Track links.
            # Unrelated blocking issues do not become milestone work. A local
            # title shared with overall direction still needs parsed waypoints.
            if (entry.get("discussion") or {}).get("ancestry_complete") is True and (entry.get("milestone") or {}).get("title") not in milestone_titles:
                continue
            unknown.append(entry)
            continue
        if milestone["state"] != "matched":
            continue
        # Shared prerequisites may inherit only the first native path. Use the
        # recorded edges, not path membership, to recognize inspected containers.
        dependencies = [*entry.get("blocked_by", []), *entry.get("open_sub_issues", [])]
        if entry.get("exclusion") in {"blocked_by_open_dependency", "delegated_to_open_sub_issues"} and dependencies and all(
            (dependency["repo"].casefold(), dependency["number"]) in entry_keys for dependency in dependencies
        ):
            continue
        frontier.append(entry)
    for entry in frontier:
        if entry.get("exclusion") not in {None, "waiting", "parent_waiting"}:
            return {**result, "reason": "milestone_issue_excluded", "issue": f"{entry['repo']}#{entry['number']}", "exclusion": entry["exclusion"]}
        review = reviews.get(f"{entry['repo']}#{entry['number']}".casefold(), {})
        discussion = entry.get("discussion") or {}
        if (
            not discussion.get("complete")
            or review.get("discussion_digest") != discussion.get("digest")
            or review.get("state") != "waiting"
            or review.get("waiting_on") != "person"
            or review.get("ownership_complete") is not True
        ):
            return {**result, "issue": f"{entry['repo']}#{entry['number']}", "required": "current_complete_person_wait_review"}
    # Person-wait reviews enable the adapter's capacity-only ancestry reads.
    # Report that prerequisite before context those reads have not gathered.
    if unknown:
        entry = unknown[0]
        return {**result, "reason": "unknown_milestone_context", "issue": f"{entry['repo']}#{entry['number']}"}
    if not frontier:
        return {**result, "reason": "no_milestone_waits"}
    return {"admitted": True, "reason": "all_milestones_waiting_on_people", "milestone_wait_count": len(frontier)}


def rank_portfolio_work(
    graph: dict[str, Any], discoveries: list[dict[str, Any]], *,
    milestone_titles: list[str], selection_context: dict[str, Any] | None = None,
    repository_milestones: dict[str, list[str] | None] | None = None,
    repository_waypoints: dict[str, list[str] | None] | None = None,
    coverage_complete: bool = False,
    repository_clients: dict[str, dict[str, Any]] | None = None,
    director_owner: str | None = None,
) -> dict[str, Any]:
    """Share final evidence handling across adapters without inferring permission.

    Unreviewed issues remain possible work, never independently available work.
    Callers interpret direction and full discussions and supply current ownership
    evidence; neither labels nor an incomplete session list establish availability.
    coverage_complete attests the milestone graph, independently of portfolio
    discovery bounds. Inspected discoveries still contribute milestone evidence.
    """
    def client_milestone(entry: dict[str, Any]) -> dict[str, Any]:
        if director_owner is None or entry["repo"].split("/")[0].casefold() != director_owner.casefold():
            return entry
        record = next((value for key, value in (repository_clients or {}).items()
                       if key.casefold() == entry["repo"].casefold()), None)
        waypoints = next((value for key, value in (repository_waypoints or {}).items()
                          if key.casefold() == entry["repo"].casefold()), None)
        if not github_client.is_client_issue(entry, record):
            return entry
        native_titles = {(item.get("milestone") or {}).get("title")
                         for item in [*graph.get("candidates", []), *graph.get("excluded", [])]
                         if item["repo"].casefold() == entry["repo"].casefold() and item.get("via")
                         and (item.get("milestone") or {}).get("state") == "open"}
        eligible = [title for title in milestone_titles if title not in graph.get("completed_milestones", [])
                    and (title in (waypoints or []) or title in native_titles)]
        assigned = (entry.get("milestone") or {}).get("title")
        # Explicit membership outside this product's current direction is not
        # silently reassigned. Direction changes still require escalation.
        title = assigned if assigned in eligible else (
            eligible[0] if eligible and (not assigned or assigned in (waypoints or []) and native_titles) else None
        )
        if title is None:
            return entry
        if assigned and (entry.get("milestone") or {}).get("state") != "open":
            return entry
        return {**entry, "milestone": entry.get("milestone") if assigned == title else {"title": title, "state": "open"},
                "client_request": {"source": record["source"], "milestone": title, "ranking_only": True}}

    graph = {**graph, "candidates": [client_milestone(item) for item in graph.get("candidates", [])],
             "excluded": [client_milestone(item) for item in graph.get("excluded", [])]}
    discoveries = [client_milestone(item) for item in discoveries]
    context = validate_selection_context(selection_context or {})
    capacity = tooling_capacity_context(
        graph, discoveries, milestone_titles=milestone_titles, context=context,
        repository_waypoints=repository_waypoints or {}, coverage_complete=coverage_complete,
    )
    reviews = {key.casefold(): value for key, value in context.get("issues", {}).items()}
    candidates: list[dict[str, Any]] = []
    excluded = list(graph.get("excluded", []))
    waiting = list(graph.get("waiting", []))
    underway: list[dict[str, Any]] = []
    seen = {f"{entry['repo']}#{entry['number']}".casefold() for entry in excluded if entry.get("exclusion") != "outside_direction_tracks"}
    work = [(entry, False) for entry in graph["candidates"]] + [(entry, True) for entry in discoveries]
    for raw, discovered in work:
        key = f"{raw['repo']}#{raw['number']}".casefold()
        if key in seen:
            continue
        seen.add(key)
        item: dict[str, Any] = {**raw, "availability": "needs_review"}
        item["overall_milestone_context"] = overall_milestone_context(
            item, graph, milestone_titles,
            repository_waypoints or {},
        )
        hold = repository_hold(context, item["repo"])
        if hold:
            excluded.append({**item, "exclusion": "repository_held", "review": hold})
            continue
        if item.get("exclusion"):
            excluded.append(item)
            if item["exclusion"] in {"waiting", "parent_waiting"}:
                status_text = section_map((item.get("discussion") or {}).get("body", "")).get("Current Status", "")
                waiting.extend(waiting_records(item, status_text) or [{
                    "repo": item["repo"], "number": item["number"], "url": item["url"],
                    "waiting_for": item.get("waiting_on_parent") or status_text or "Waiting party or condition not recorded",
                    "reported_by": item["url"], "reported_at": item.get("updated_at"),
                }])
            continue
        discussion = item.get("discussion") or {}
        review = reviews.get(key)
        if discovered and discussion.get("ancestry_complete") is not True:
            item["review_required"] = "complete_parent_context"
        elif not discussion.get("complete"):
            item["review_required"] = "complete_issue_discussion"
        elif review is None or review.get("discussion_digest") != discussion.get("digest"):
            item["review_required"] = "direction_discussion_and_current_ownership"
        elif review["state"] == "underway":
            underway.append({**item, "availability": "underway", "review": review})
            continue
        elif review["state"] == "waiting":
            waiting.append({**item, "waiting_for": review["reason"], "review": review})
            excluded.append({**item, "exclusion": "reviewed_wait", "review": review})
            continue
        elif review["state"] == "ineligible":
            excluded.append({**item, "exclusion": "outside_owner_direction", "review": review})
            continue
        elif review.get("ownership_complete") is not True:
            item["review_required"] = "current_ownership_evidence_incomplete"
        else:
            category = review.get("category")
            if (item.get("via") or item.get("client_request")) and category != "live_incident":
                category = "milestone"
            occurrences = review.get("stop_occurrences") or []
            if category not in {"live_incident", "milestone", "repeated_stop_tooling", "own_project"} or (category == "milestone" and not (item.get("via") or item.get("client_request"))):
                item["review_required"] = "direction_eligibility"
            else:
                stop_count = len({url for url in occurrences if isinstance(url, str) and url.startswith("https://")}) if isinstance(occurrences, list) else 0
                if category == "repeated_stop_tooling" and stop_count < 2 and not capacity["admitted"]:
                    item["review_required"] = "two_linked_stop_occurrences"
                else:
                    item.update(availability="available", category=category, review=review)
                    if category == "repeated_stop_tooling":
                        rule = "repeated_stops" if stop_count >= 2 else "all_milestones_waiting_on_people"
                        item.update(tooling_admission_rule=rule, recorded_stop_count=stop_count)
                        item["reasons"] = [*item.get("reasons", []), rule]
        candidates.append(item)
    for repo, titles in (repository_milestones or {}).items():
        group = [item for item in candidates if not item.get("via") and item["repo"].casefold() == repo.casefold()]
        rank_next_candidates(group, direction_milestones=titles)
        for item in group:
            item["repository_rank"] = item["rank"]
    rank_next_candidates(candidates, direction_milestones=milestone_titles)
    priority = {"live_incident": 0, "milestone": 1, "repeated_stop_tooling": 2, "own_project": 3}
    candidates.sort(key=lambda candidate: (
        0 if is_live_breakage(candidate) else (4 if candidate.get("tooling_admission_rule") == "all_milestones_waiting_on_people" else priority.get(candidate.get("category"), 1 if candidate.get("via") or candidate.get("client_request") else 5)),
        -candidate.get("recorded_stop_count", 0) if capacity["admitted"] and candidate.get("category") == "repeated_stop_tooling" and not is_live_breakage(candidate) else (candidate["rank"] if candidate.get("via") or candidate.get("client_request") else candidate.get("repository_rank", candidate["rank"])),
        str(candidate.get("created_at") or ""), candidate["repo"].casefold(), candidate["number"],
    ))
    for rank, item in enumerate(candidates, 1):
        item["rank"] = rank
    available = [item for item in candidates if item["availability"] == "available"]
    return {
        "candidates": candidates, "candidate_count": len(candidates),
        "available_candidates": available, "available_candidate_count": len(available),
        "review_required_count": len(candidates) - len(available),
        "underway": underway, "waiting": waiting, "excluded": excluded,
        "tooling_capacity_context": capacity,
        "repository_holds": context.get("repository_holds", {}),
        "ownership_context": {"source": "caller_evidence", "all_active_sessions_searched": False},
    }


def waiting_records(issue: dict[str, Any], status_text: str) -> list[dict[str, Any]]:
    """Keep explicitly reported waits separate from a parent's independent work.

    Only Current Status is consulted. References identify the subject of a wait,
    not another dependency edge; prose never grants permission to execute it.
    """
    records: list[dict[str, Any]] = []
    for line in status_text.splitlines():
        match = re.match(r"\s*(?:[-*]\s+)?(?:Waiting for|Parked until):\s*(.+)", line, re.I)
        if not match:
            continue
        reason = match.group(1).strip()
        if reason.casefold().rstrip(" .") in {"none", "n/a", "nothing", "-"}:
            continue
        references: dict[tuple[str, int], str] = {}
        for ref in re.finditer(
            r"https://github\.com/([^/\s)]+/[^/\s)]+)/(issues|pull)/(\d+)"
            r"|(?<![\w/])([\w.-]+/[\w.-]+)?#(\d+)\b",
            reason,
        ):
            repo = ref.group(1) or ref.group(4) or issue["repo"]
            number = int(ref.group(3) or ref.group(5))
            kind = ref.group(2) or "issues"
            references[(repo, number)] = f"https://github.com/{repo}/{kind}/{number}"
        if not references:
            references[(issue["repo"], issue["number"])] = issue["url"]
        for (repo, number), url in references.items():
            records.append({
                "repo": repo,
                "number": number,
                "url": url,
                "waiting_for": reason,
                "reported_by": issue["url"],
                "reported_at": issue.get("updated_at"),
            })
    return records


def evaluate_direction_node(
    issue: dict[str, Any],
    *,
    config: dict[str, Any],
    focus: str | None,
    relationships: dict[str, list[dict[str, Any]]] | None,
    relationship_error: str | None = None,
    truncated_relationships: list[str] | None = None,
) -> dict[str, Any]:
    """Classify raw issue evidence identically for CLI and service consumers."""
    if truncated_relationships:
        relationship_error = "Relationship evidence exceeded the reader's collection limit"
    _, item = evaluate_next_plan(
        issue, config=config, focus=focus, relationships=relationships,
        relationship_error=relationship_error,
    )
    if truncated_relationships:
        item["truncated_relationships"] = truncated_relationships
    if item.get("exclusion") in {"completed", "stale_needs_review", "unknown_dependencies"}:
        return {"item": item}
    if issue.get("pull_request") is not None:
        return {"item": {**item, "exclusion": "pull_request"}}
    status_text = section_map(issue.get("body") or "").get("Current Status", "")
    status_text = re.sub(r"<!--.*?-->", "", status_text, flags=re.S).strip()
    reports = waiting_records(item, status_text)
    summary = next_relationship_summary(relationships or {})
    if (
        next_plan_status(issue, config) == "waiting"
        or (focus or "").casefold() == "waiting"
        or re.search(r"(?im)^\s*State:\s*(?:waiting|parked)\b", status_text)
        or (reports and not (summary["open_blockers"] or summary["open_sub_issues"]))
    ):
        item["exclusion"] = "waiting"
    return {
        "item": item,
        "blockers": summary["open_blockers"],
        "children": summary["open_sub_issues"],
        "waiting": reports,
        "status_text": status_text,
    }


def rank_direction_work(
    roots: list[dict[str, Any]],
    *,
    milestone_titles: list[str],
    read_node: Callable[[str, int], dict[str, Any]],
    scan_limit: int,
    completed_milestone_titles: list[str] | None = None,
) -> dict[str, Any]:
    """Walk ordered Track issues through native blockers and sub-issues.

    read_node returns item (the ordinary next evaluation), blockers, children,
    and waiting (explicit Current Status reports). Each unique issue is read
    once. A shared leaf inherits the earliest overall milestone that reaches it.
    Tracking issues are containers even when their summary label says waiting;
    an ordinary waiting issue remains excluded. Candidates retain their original
    issue milestone and the native path that gives them their overall priority.
    """
    order = {title: index for index, title in enumerate(milestone_titles)}
    completed = set(completed_milestone_titles or [])
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    tracks: list[dict[str, Any]] = []
    for root in roots:
        milestone = root.get("milestone") or {}
        title = milestone.get("title")
        if title in order and title not in completed and milestone.get("state") == "open" and str(root.get("title", "")).startswith("Track:"):
            tracks.append(root)
        else:
            excluded.append({**root, "exclusion": "outside_direction_tracks"})
    tracks.sort(key=lambda candidate: (order[candidate["milestone"]["title"]], candidate["number"]))
    present = {root["milestone"]["title"] for root in tracks}
    missing = [title for title in milestone_titles if title not in present and title not in completed]
    seen: set[tuple[str, int]] = set()
    degraded = 0
    truncated = False
    # A stack keeps even a deep dependency chain within the explicit scan bound.
    pending: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], bool]] = [
        (root, root["milestone"], [], True) for root in reversed(tracks)
    ]
    while pending:
        ref, milestone, path, tracking = pending.pop()
        key = (ref["repo"].casefold(), ref["number"])
        step = {"repo": ref["repo"], "number": ref["number"], "url": ref["url"]}
        if "relationship" in ref:
            step["relationship"] = ref["relationship"]
        via = [*path, step]
        if any((entry["repo"].casefold(), entry["number"]) == key for entry in path):
            excluded.append({**step, "milestone": milestone, "via": via, "exclusion": "dependency_cycle"})
            degraded += 1
            continue
        if key in seen:
            continue
        if len(seen) >= scan_limit:
            truncated = True
            break
        seen.add(key)
        node = read_node(ref["repo"], ref["number"])
        item = {
            **node["item"],
            "blocked_by": node.get("blockers") or [],
            "open_sub_issues": node.get("children") or [],
            "issue_milestone": node["item"].get("milestone"),
            "milestone": milestone,
            "via": via,
        }
        reason = item.get("exclusion")
        blockers = node.get("blockers") or []
        children = node.get("children") or []
        if reason in {"completed", "stale_needs_review", "unknown_dependencies", "pull_request"}:
            excluded.append(item)
            degraded += reason == "unknown_dependencies"
            continue
        reports = node.get("waiting") or []
        waiting.extend({**report, "milestone": milestone, "via": via} for report in reports)
        if reason == "waiting" and not reports and not tracking:
            waiting.append({
                **step,
                "milestone": milestone,
                "waiting_for": node.get("status_text") or "Waiting party or condition not recorded",
                "reported_by": item["url"],
                "reported_at": item.get("updated_at"),
            })
        if reason in {"waiting", "later_focus", "label_blocked_without_native_edge"} and not tracking:
            excluded.append(item)
            degraded += reason == "label_blocked_without_native_edge"
            continue
        if blockers or children:
            excluded.append({**item, "exclusion": "tracking" if tracking else reason or "delegated_to_open_sub_issues"})
            # Read blockers first, then independent child work. Rank all actionable
            # leaves with the same milestone/dependency/age ordering as local next.
            edges = [(edge, "blocked_by") for edge in blockers]
            edges += [(edge, "sub_issue") for edge in children]
            for edge, relationship in reversed(edges):
                pending.append(({**edge, "relationship": relationship}, milestone, via, False))
        elif tracking:
            excluded.append({**item, "exclusion": "tracking_without_open_work"})
        elif reason:
            excluded.append(item)
        else:
            item["reasons"] = [
                f"direction_milestone_{order[milestone['title']] + 1}",
                "native_path_from_tracking_issue",
                *(item.get("reasons") or []),
            ]
            candidates.append(item)
    candidates.sort(key=lambda candidate: (candidate["repo"].casefold(), candidate["number"]))
    rank_next_candidates(candidates, direction_milestones=milestone_titles)
    return {
        "candidates": candidates,
        "candidate_count": len(candidates),
        "excluded": excluded,
        "waiting": waiting,
        "completed_milestones": [title for title in milestone_titles if title in completed],
        "tracking_roots": [{"repo": root["repo"], "number": root["number"]} for root in tracks],
        "tracking_milestones": [{"repo": root["repo"], "number": root["number"], "milestone": root["milestone"]} for root in tracks],
        "evaluated": len(seen),
        "truncated": truncated,
        "dependency_context": {
            "complete": not (degraded or truncated or missing),
            "degraded_count": degraded,
            "missing_tracking_milestones": missing,
        },
    }
