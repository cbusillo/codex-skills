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
import datetime as dt
import json
import re
from collections.abc import Callable
from typing import Any

import github_agent
import github_plan_claim
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
    return bool(issue.get("incident_via")) or LIVE_BREAKAGE_LABEL in {name.casefold() for name in normalize_labels(issue.get("labels"))}


def discovery_scan(inventory: list[dict[str, Any]], scan_limit: int, selection_context: dict[str, Any] | None = None, *, agent: str | None = None) -> list[dict[str, Any]]:
    """Keep bounded evidence allowances separate from selectable discovery."""
    counts = {"ordinary": 0, "held": 0, "other_family": 0}
    incidents = []
    scanned = []
    for item in inventory:
        if is_live_breakage(item):
            incidents.append(item)
            continue
        mismatch = github_agent.exclusion(item, agent)
        bucket = ("held" if repository_hold(selection_context or {}, item["repo"]) else
                  "other_family" if mismatch and mismatch["exclusion"] == "assigned_elsewhere" else
                  "ordinary")
        if counts[bucket] < scan_limit:
            counts[bucket] += 1
            scanned.append(item)
    return incidents + scanned


def compact_list_issue(repo: str, issue: dict[str, Any]) -> dict[str, Any]:
    milestone = issue.get("milestone") or {}
    state = issue.get("state")
    return {
        "repo": repo,
        "number": issue.get("number"),
        "title": issue.get("title"),
        "author": (issue.get("user") or {}).get("login") or issue.get("author"),
        "author_is_bot": (issue.get("user") or {}).get("type") == "Bot",
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


def undated_wait_reason(reason: str) -> str:
    """Keep explicit wait-start metadata separate from the pending condition."""
    return re.sub(r";\s*since\s+\d{4}-\d{2}-\d{2}(?:T\S+)?$", "", reason.strip().rstrip(" ."), flags=re.I)


def unnamed_wait_reason(reason: str) -> bool:
    plain = re.sub(r"[`*]", "", undated_wait_reason(reason)).casefold().rstrip(" .")
    return not plain or no_current_wait(plain) or plain in {"tbd", "unknown", "not recorded", "testing", "people"}


def milestone_order_wait(reason: str, milestone_titles: list[str]) -> bool:
    """Match only milestone ordering, preserving any additional person/event hold."""
    plain = re.sub(r"[`*]", "", undated_wait_reason(reason)).casefold().replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    completion = r"(?:\s+(?:(?:(?:to\s+)?(?:be\s+)?)?(?:finish(?:ed|es)?|complete(?:d|s)?|land(?:ed|s)?|end(?:ed|s)?|ship(?:ped|s)?|close(?:d|s)?|wraps?\s+up)|(?:is|are)\s+(?:done|complete)))?"
    generic = r"(?:the\s+)?(?:another|other|earlier|previous|next)\s+milestones?" + completion
    if re.fullmatch(generic, plain):
        return True
    if not milestone_titles:
        return False
    title = "(?:" + "|".join(re.escape(value.casefold()) for value in milestone_titles) + ")"
    milestone = r"(?:milestones?\s+)?(?:the\s+)?['\"]?" + title + r"['\"]?(?:\s+milestone)?" + completion
    prefix = r"(?:(?:starts?|starting)\s+)?(?:(?:after|until|following|once)\s+)?(?:[\w.-]+/direction\s+)?"
    return re.fullmatch(prefix + milestone + r"(?:\s+and\s+" + milestone + ")*", plain) is not None


def milestone_wait_evidence(item: dict[str, Any], status_text: str, milestone_titles: list[str]) -> dict[str, Any]:
    """Validate the recorded wait, leaving person/event interpretation to review."""
    status_text = re.sub(r"<!--.*?-->", "", status_text, flags=re.DOTALL)
    status_text = re.sub(r"\*\*(Waiting for|Parked until|Blocked by|Waiting since)(:?)\*\*(:?)", r"\1\2\3", status_text, flags=re.IGNORECASE)
    rows = waiting_records(item, status_text)
    pending = [row for row in rows if not row["no_current_wait"]]
    valid_pending = [row for row in pending
                     if not unnamed_wait_reason(row["waiting_for"])
                     and not milestone_order_wait(row["waiting_for"], milestone_titles)]
    other_holds = [match[1].strip() for match in re.finditer(r"(?im)^\s*(?:[-*]\s+)?Blocked by:\s*([^\n]+)", status_text)
                   if not github_plan_claim.no_wait_reason(match[1], field="Blocked by")]
    if rows:
        other_holds.extend(match[1].strip() for match in re.finditer(
            r"(?im)^\s*(?:[-*]\s+)?State:\s*(?:waiting on|waiting for|awaiting|parked until)\s+([^\n]+)", status_text))
    for hold in other_holds:
        suffix = re.search(r"\bwaiting for\s+(.+)", hold, re.I)
        hold = suffix[1] if suffix else hold
        if not unnamed_wait_reason(hold) and not milestone_order_wait(hold, milestone_titles):
            valid_pending.append({"waiting_for": hold})
    reason = (valid_pending or pending or rows or [{"waiting_for": ""}])[0]["waiting_for"]
    if not reason or no_current_wait(reason):
        fallback = re.search(r"(?im)^[ \t]*(?:[-*][ \t]+)?Blocked by:[^\n]*?\bwaiting for[ \t]+([^\n]*)", status_text)
        if fallback:
            reason = fallback.group(1).strip()
    requested = bool((reason and not no_current_wait(reason)) or item.get("exclusion") == "waiting"
                     or item.get("plan_status") == "waiting"
                     or re.search(r"(?im)^\s*(?:[-*]\s+)?State:\s*(?:waiting|parked)\b", status_text))
    invalid = None
    if unnamed_wait_reason(reason):
        invalid = "wait_names_no_person_or_event"
    elif milestone_order_wait(reason, milestone_titles):
        invalid = "wait_names_another_milestone"
    # updated_at is evidence of when the record was observed, not a fabricated
    # start date. Only an explicit since field establishes how long it waited.
    since = re.search(r"(?im)^[ \t]*(?:[-*][ \t]+)?Waiting since:[ \t]*([^\n]+)", status_text)
    if since is None:
        since = re.search(r"(?:^|;\s*)since\s+(\d{4}-\d{2}-\d{2}(?:T\S+)?)", reason, re.IGNORECASE)
    start = since.group(1).strip().rstrip(".,;") if since else None
    if start:
        dated_note = re.match(r"^(\d{4}-\d{2}-\d{2})(?:\s+.*)?$", start)
        if dated_note:
            start = dated_note.group(1)
    if start:
        try:
            dt.datetime.fromisoformat(start)
        except ValueError:
            start = None
    return {"requested": requested, "valid": invalid is None, "reason": invalid,
            "waiting_for": reason, "since": start,
            "recorded_at": item.get("updated_at")}


def check_milestone_wait(item: dict[str, Any], status_text: str, milestone_titles: list[str]) -> dict[str, Any]:
    if item.get("plan_status") in {"blocked", "stale", "done"} or item.get("exclusion") in {"completed", "stale_needs_review", "unknown_dependencies", "unknown_ancestry", "label_blocked_without_native_edge", "tracking", "tracking_without_open_work", "pull_request"}:
        return item
    evidence = milestone_wait_evidence(item, status_text, milestone_titles)
    if not evidence["requested"] or evidence["valid"]:
        return item
    finding = {"kind": "milestone_wait_invalid", "repo": item["repo"], "number": item["number"],
               "url": item["url"], **evidence}
    result = {**item, "wait_finding": finding}
    if item.get("blocked_by"):
        return {**result, "exclusion": "blocked_by_open_dependency"}
    if result.get("exclusion") == "waiting":
        result.pop("exclusion")
        if result.get("open_sub_issues"):
            result["exclusion"] = "delegated_to_open_sub_issues"
    return result


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
    if status == "waiting":
        return "excluded", {**base, "exclusion": "waiting", "evidence": []}
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
        request = candidate.get("client_request")
        milestone = {"title": request["milestone"], "state": "open"} if request else candidate.get("milestone")
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
        if parent.get("exclusion") in {"waiting", "label_blocked_without_native_edge"}:
            return {**result, "exclusion": "parent_waiting", "waiting_on_parent": parent["url"], "_own_exclusion": item.get("exclusion")}
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
    inherited = set()
    for parent in (item.get("discussion") or {}).get("parents", []):
        parent_milestone = parent.get("milestone") or {}
        parent_titles = next((value for key, value in repository_milestones.items()
                              if key.casefold() == parent["repo"].casefold()), None)
        if (parent_milestone.get("state") == "open" and parent_milestone.get("title") in milestone_titles
                and parent_milestone.get("title") in (parent_titles or [])):
            inherited.add(parent_milestone["title"])
    if inherited:
        own_milestone = item.get("milestone") or {}
        own_titles = next((value for key, value in repository_milestones.items()
                           if key.casefold() == item["repo"].casefold()), None)
        if (own_milestone.get("state") == "open" and own_milestone.get("title") in milestone_titles
                and own_milestone.get("title") in (own_titles or [])):
            inherited.add(own_milestone["title"])
        return {"state": "matched", "titles": [title for title in milestone_titles if title in inherited],
                "source": "native_milestone_ancestry"}
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
    """Require current caller evidence for staffed work or real person/event waits."""
    result: dict[str, Any] = {"admitted": False, "reason": "milestone_waits_not_proven"}
    if not coverage_complete or not graph.get("dependency_context", {}).get("complete"):
        refusal = {**result, "reason": "incomplete_milestone_coverage"}
        missing = graph.get("dependency_context", {}).get("missing_tracking_milestones", [])
        if missing:
            refusal["detail"] = ("Spare-capacity tooling is not admitted: no Track issue was found in the read inventory for these listed milestones: "
                                 + ", ".join(f'"{title}"' for title in missing)
                                 + ". Check the Track issues and their milestone links; their work or waits cannot yet be checked.")
        return refusal
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
    staffed: dict[tuple[str, int], dict[str, Any]] = {}
    for entry in frontier:
        if entry.get("exclusion") not in {None, "waiting", "parent_waiting"}:
            return {**result, "reason": "milestone_issue_excluded", "issue": f"{entry['repo']}#{entry['number']}", "exclusion": entry["exclusion"]}
        if entry.get("wait_evidence_complete") is False:
            return {**result, "issue": f"{entry['repo']}#{entry['number']}", "required": "current_wait_evidence"}
        review = reviews.get(f"{entry['repo']}#{entry['number']}".casefold(), {})
        discussion = entry.get("discussion") or {}
        own_status = section_map(discussion.get("body", "")).get("Current Status", "")
        own_waits = waiting_records(entry, own_status)
        if (review.get("state") == "underway" and entry.get("exclusion") is None
                and entry.get("plan_status") == "active"
                and not repository_hold(context, entry["repo"])
                and not any(not row["no_current_wait"] for row in own_waits)
                and not entry.get("stale_wait_evidence")):
            if (not discussion.get("complete") or review.get("discussion_digest") != discussion.get("digest")
                    or review.get("ownership_complete") is not True):
                return {**result, "issue": f"{entry['repo']}#{entry['number']}", "required": "current_complete_staffing_review"}
            staffed[(entry["repo"].casefold(), entry["number"])] = {
                "repo": entry["repo"], "number": entry["number"], "url": entry["url"],
                "reason": review["reason"], "evidence": review["evidence"],
            }
            continue
        own_status = section_map((entry.get("discussion") or {}).get("body", "")).get("Current Status", "")
        own_waits = waiting_records(entry, own_status)
        if (entry.get("plan_status") != "waiting" or not own_waits
                or any(row.get("unowned", row["non_external"]) for row in own_waits) or entry.get("stale_wait_evidence")):
            return {**result, "issue": f"{entry['repo']}#{entry['number']}", "required": "current_own_issue_wait"}
        review = reviews.get(f"{entry['repo']}#{entry['number']}".casefold(), {})
        discussion = entry.get("discussion") or {}
        if (
            not discussion.get("complete")
            or review.get("discussion_digest") != discussion.get("digest")
            or review.get("state") != "waiting"
            or review.get("waiting_on") not in {"person", "event"}
            or review.get("ownership_complete") is not True
        ):
            return {**result, "issue": f"{entry['repo']}#{entry['number']}", "required": "current_complete_person_wait_review"}
        source, status = entry, own_status
        evidence = milestone_wait_evidence(source, status, milestone_titles)
        if not evidence["valid"]:
            return {**result, "reason": evidence["reason"], "issue": f"{entry['repo']}#{entry['number']}"}
    # Staffed-work and external-wait reviews enable capacity-only ancestry reads.
    # Report that prerequisite before context those reads have not gathered.
    if unknown:
        entry = unknown[0]
        return {**result, "reason": "unknown_milestone_context", "issue": f"{entry['repo']}#{entry['number']}"}
    if not frontier:
        return {**result, "reason": "no_milestone_waits"}
    # Propagate every Track's title over the native edges, including shared
    # prerequisites whose ranking path retains only the earliest milestone.
    waits: dict[str, list[dict[str, Any]]] = {title: [] for title in milestone_titles
                                          if title not in graph.get("completed_milestones", [])}
    staffing: dict[str, list[dict[str, Any]]] = {title: [] for title in waits}
    frontier_keys = {(entry["repo"].casefold(), entry["number"]) for entry in frontier}
    for title, records in waits.items():
        pending_keys = [key for key, entry in by_key.items()
                        if title in overall_milestone_context(entry, graph, milestone_titles, repository_waypoints).get("titles", [])]
        visited: set[tuple[str, int]] = set()
        while pending_keys:
            key = pending_keys.pop()
            if key in visited:
                continue
            visited.add(key)
            entry = by_key[key]
            if key in staffed:
                if staffed[key] not in staffing[title]:
                    staffing[title].append(staffed[key])
            elif key in frontier_keys:
                source = entry
                status = section_map((entry.get("discussion") or {}).get("body", "")).get("Current Status", "")
                evidence = milestone_wait_evidence(source, status, milestone_titles)
                record = {"repo": source["repo"], "number": source["number"], "url": source["url"],
                                     "waiting_for": evidence["waiting_for"], "since": evidence["since"],
                                     "recorded_at": evidence["recorded_at"]}
                if record not in records:
                    records.append(record)
            pending_keys.extend((dep["repo"].casefold(), dep["number"])
                                for dep in [*entry.get("blocked_by", []), *entry.get("open_sub_issues", [])]
                                if (dep["repo"].casefold(), dep["number"]) in by_key)
        if not records and not staffing[title]:
            return {**result, "reason": "milestone_names_no_person", "milestone": title}
    people_only = not staffed and all(reviews[f"{entry['repo']}#{entry['number']}".casefold()].get("waiting_on") == "person" for entry in frontier)
    return {"admitted": True, "reason": "all_milestones_waiting_on_people" if people_only else "all_milestones_staffed_or_waiting",
            "milestone_wait_count": len(frontier) - len(staffed), "milestone_staffed_count": len(staffed),
            "milestone_waits": [{"milestone": title, "waits": records} for title, records in waits.items()],
            "milestone_staffing": [{"milestone": title, "underway": records} for title, records in staffing.items()]}


def with_client_milestone(
    entry: dict[str, Any], graph: dict[str, Any], *, milestone_titles: list[str],
    repository_clients: dict[str, dict[str, Any]] | None,
    repository_waypoints: dict[str, list[str] | None] | None,
    director_owner: str | None, bot_logins: tuple[str, ...] = (),
) -> dict[str, Any]:
    if entry.get("via") or director_owner is None or entry["repo"].split("/")[0].casefold() != director_owner.casefold():
        return entry
    record = next((value for key, value in (repository_clients or {}).items()
                   if key.casefold() == entry["repo"].casefold()), None)
    waypoints = next((value for key, value in (repository_waypoints or {}).items()
                      if key.casefold() == entry["repo"].casefold()), None)
    if not github_client.is_client_issue(entry, record, bot_logins=bot_logins):
        return entry
    native_titles = {(item.get("milestone") or {}).get("title")
                     for item in [*graph.get("candidates", []), *graph.get("excluded", [])]
                     if item["repo"].casefold() == entry["repo"].casefold() and item.get("via")
                     and (item.get("milestone") or {}).get("state") == "open"}
    if not native_titles and not graph.get("dependency_context", {}).get("complete"):
        return entry
    eligible = [title for title in milestone_titles if title not in graph.get("completed_milestones", [])
                and title in (native_titles if native_titles else waypoints or [])]
    assigned = (entry.get("milestone") or {}).get("title")
    if not eligible or assigned in graph.get("completed_milestones", []):
        return entry
    if assigned:
        if (entry.get("milestone") or {}).get("state") != "open":
            return entry
        if assigned in eligible:
            title = assigned
        elif assigned not in milestone_titles and assigned in (waypoints or []) and native_titles:
            title = eligible[0]
        else:
            return entry
    else:
        title = eligible[0]
    return {**entry, "client_request": {"source": record["source"], "milestone": title, "ranking_only": True,
                                      "basis": "native_track" if native_titles else "exact_listed_title_match"}}


def rank_portfolio_work(
    graph: dict[str, Any], discoveries: list[dict[str, Any]], *,
    milestone_titles: list[str], selection_context: dict[str, Any] | None = None,
    repository_milestones: dict[str, list[str] | None] | None = None,
    repository_waypoints: dict[str, list[str] | None] | None = None,
    coverage_complete: bool = False,
    repository_clients: dict[str, dict[str, Any]] | None = None,
    director_owner: str | None = None,
    bot_logins: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Share final evidence handling across adapters without inferring permission.

    Unreviewed issues remain possible work, never independently available work.
    Callers interpret direction and full discussions and supply current ownership
    evidence; neither labels nor an incomplete session list establish availability.
    coverage_complete attests the milestone graph, independently of portfolio
    discovery bounds. Inspected discoveries still contribute milestone evidence.
    """
    def client_milestone(entry: dict[str, Any]) -> dict[str, Any]:
        return with_client_milestone(entry, graph, milestone_titles=milestone_titles,
                                     repository_clients=repository_clients, repository_waypoints=repository_waypoints,
                                     director_owner=director_owner, bot_logins=bot_logins)

    graph = {**graph, "candidates": [client_milestone(item) for item in graph.get("candidates", [])],
             "excluded": [client_milestone(item) for item in graph.get("excluded", [])]}
    discoveries = [client_milestone(item) for item in discoveries]
    findings = list(graph.get("findings", []))

    def checked_wait(item: dict[str, Any]) -> dict[str, Any]:
        if overall_milestone_context(item, graph, milestone_titles, repository_waypoints or {})["state"] != "matched":
            return {key: value for key, value in item.items() if key != "_own_exclusion"}
        if item.get("exclusion") == "parent_waiting":
            valid_parent = None
            tracking = {(root["repo"].casefold(), root["number"]) for root in graph.get("tracking_roots", [])}
            for parent in (item.get("discussion") or {}).get("parents", []):
                if (parent["repo"].casefold(), parent["number"]) in tracking:
                    continue
                status = section_map((parent.get("discussion") or {}).get("body", "")).get("Current Status", "")
                checked = check_milestone_wait(parent, status, milestone_titles)
                if checked.get("wait_finding") and checked["wait_finding"] not in findings:
                    findings.append(checked["wait_finding"])
                if checked.get("exclusion") in {"waiting", "label_blocked_without_native_edge"}:
                    valid_parent = parent
                    break
            item = dict(item)
            if valid_parent is not None:
                item["waiting_on_parent"] = valid_parent["url"]
            else:
                item.pop("exclusion", None)
                item.pop("waiting_on_parent", None)
                if item.get("_own_exclusion"):
                    item["exclusion"] = item["_own_exclusion"]
            item.pop("_own_exclusion", None)
        item = {key: value for key, value in item.items() if key != "_own_exclusion"}
        status = section_map((item.get("discussion") or {}).get("body", "")).get("Current Status", "")
        result = check_milestone_wait(item, status, milestone_titles)
        if result.get("wait_finding") and result["wait_finding"] not in findings:
            findings.append(result["wait_finding"])
        return result

    checked_excluded = [checked_wait(item) for item in graph.get("excluded", [])]
    graph = {**graph, "candidates": [checked_wait(item) for item in graph.get("candidates", [])]
             + [item for item in checked_excluded if not item.get("exclusion")],
             "excluded": [item for item in checked_excluded if item.get("exclusion")]}
    discoveries = [checked_wait(item) for item in discoveries]
    context = validate_selection_context(selection_context or {})
    capacity = tooling_capacity_context(
        graph, discoveries, milestone_titles=milestone_titles, context=context,
        repository_waypoints=repository_waypoints or {}, coverage_complete=coverage_complete,
    )
    reviews = {key.casefold(): value for key, value in context.get("issues", {}).items()}
    candidates: list[dict[str, Any]] = []
    excluded = list(graph.get("excluded", []))
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
        elif review["state"] == "waiting" and not item.get("wait_finding"):
            excluded.append({**item, "exclusion": "reviewed_wait", "review": review})
            continue
        elif review["state"] == "ineligible":
            excluded.append({**item, "exclusion": "outside_owner_direction", "review": review})
            continue
        elif review["state"] == "waiting":
            item["review_required"] = "invalid_milestone_wait_needs_current_ownership_review"
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
                        rule = "repeated_stops" if stop_count >= 2 else capacity["reason"]
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
        0 if is_live_breakage(candidate) else (4 if candidate.get("tooling_admission_rule") in {"all_milestones_waiting_on_people", "all_milestones_staffed_or_waiting"} else priority.get(candidate.get("category"), 1 if candidate.get("via") or candidate.get("client_request") else 5)),
        -candidate.get("recorded_stop_count", 0) if capacity["admitted"] and candidate.get("category") == "repeated_stop_tooling" and not is_live_breakage(candidate) else (candidate["rank"] if candidate.get("via") or candidate.get("client_request") else candidate.get("repository_rank", candidate["rank"])),
        str(candidate.get("created_at") or ""), candidate["repo"].casefold(), candidate["number"],
    ))
    for rank, item in enumerate(candidates, 1):
        item["rank"] = rank
    recorded_waits: list[dict[str, Any]] = []
    unowned: list[dict[str, Any]] = []
    current_waits: list[dict[str, Any]] = []
    stale_waits: list[dict[str, Any]] = []
    seen_waits: set[tuple[str, int, str]] = set()
    # Reconstruct from each issue's own status, never a parent's text or a
    # reference used as the identity of a child. Parent exclusions stay intact.
    entries = [*graph.get("candidates", []), *excluded, *discoveries, *underway]
    parents = [parent for entry in entries for parent in (entry.get("discussion") or {}).get("parents", [])]
    invalid_waits = {finding["url"]: finding for finding in findings}
    for entry in [*entries, *parents]:
        if entry.get("url") in invalid_waits:
            entry = {**entry, "wait_finding": invalid_waits[entry["url"]]}
        if entry.get("exclusion") in {"completed", "stale_needs_review", "outside_direction_tracks"}:
            continue
        body = (entry.get("discussion") or {}).get("body", "")
        status = section_map(body).get("Current Status", "")
        rows = waiting_records(entry, status)
        if entry.get("exclusion") == "reviewed_wait":
            recorded_waits.append({"repo": entry["repo"], "number": entry["number"], "url": entry["url"],
                                   "waiting_for": entry["review"]["reason"], "reported_by": entry["url"],
                                   "reported_at": entry.get("updated_at"), "last_verified": None,
                                   "references": [], "review": entry["review"], "source": "caller_review"})
        if not rows and entry.get("plan_status") == "waiting":
            rows = [{"repo": entry["repo"], "number": entry["number"], "url": entry["url"],
                     "waiting_for": "Waiting party or condition not recorded", "reported_by": entry["url"],
                     "reported_at": entry.get("updated_at"), "last_verified": None,
                     "references": [], "non_external": True}]
        for row in rows:
            key = (row["repo"].casefold(), row["number"], row["waiting_for"])
            if key in seen_waits:
                continue
            seen_waits.add(key)
            if row.get("unowned", row["non_external"]):
                if entry.get("plan_status") == "waiting" or entry.get("exclusion") == "waiting":
                    unowned.append({**row, "review_required": True})
                elif not row["non_external"]:
                    recorded_waits.append({**row, "review_required": True})
            elif entry.get("stale_wait_evidence"):
                stale_waits.append({**row, "stale_wait_evidence": entry["stale_wait_evidence"], "review_required": True})
            elif entry.get("wait_finding"):
                unowned.append({**row, "wait_finding": entry["wait_finding"], "review_required": True})
            elif entry.get("plan_status") == "waiting":
                current_waits.append(row)
            else:
                recorded_waits.append({**row, "review_required": True})
    waiting = current_waits
    available = [item for item in candidates if item["availability"] == "available"]
    return {
        "candidates": candidates, "candidate_count": len(candidates),
        "available_candidates": available, "available_candidate_count": len(available),
        "review_required_count": len(candidates) - len(available),
        "underway": underway, "waiting": waiting, "unowned": unowned,
        "recorded_waits": recorded_waits, "stale_waits": stale_waits, "excluded": excluded,
        "tooling_capacity_context": capacity,
        "findings": findings,
        "repository_holds": context.get("repository_holds", {}),
        "ownership_context": {"source": "caller_evidence", "all_active_sessions_searched": False},
    }


def word_pattern(*words: str) -> str:
    return r"\b(?:" + "|".join(words) + r")\b"


# Words that make a clause a precondition, not a later step or a note. A lead
# clause may say "None before <step>"; a bare "None" note also keeps approvals.
PRECONDITION_WORDS = ("but", "except", "unless", "until", "if", "once", "after", "however", "pending",
                      "still", "then", "must", r"wait\w*", r"await\w*", "blocked", "parked",
                      r"requir\w*", r"need\w*", r"approv\w*", r"prerequisite\w*",
                      "outstanding", r"sign\w*[\s-]+off")
LEAD_HOLD_WORDS = word_pattern(*PRECONDITION_WORDS)
LATER_HOLD_WORDS = word_pattern(*PRECONDITION_WORDS, "before", "first")
HOLD_WORDS = word_pattern(*PRECONDITION_WORDS, "before", "first", r"authoriz\w*", "decision", r"accept\w*")


def lead_clause(reason: str) -> tuple[str, str]:
    """Split a status line's first clause from the context recorded after it."""
    text = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", reason).strip()
    match = re.match(r"(.+?)(?:;|\.(?=\s|$)|\n)\s*(.*)", text, re.S)
    return (match[1].strip(), match[2].strip()) if match else (text.rstrip(" ."), "")


def lead_names(pattern: str, reason: str) -> bool:
    """The first clause states who acts next; later clauses describe later steps.

    A precondition anywhere, such as "Note: only after Justin approves" or
    "Director approval is required first", keeps the wait.
    """
    lead, rest = lead_clause(reason)
    return (re.fullmatch(pattern, lead, re.I) is not None
            and not re.search(LEAD_HOLD_WORDS, lead, re.I)
            and not re.search(LATER_HOLD_WORDS, rest, re.I))


def no_current_wait(reason: str) -> bool:
    """Explicit absence may carry a completion note, never a pending clause."""
    if github_plan_claim.no_wait_reason(reason, field="Waiting for"):
        return True
    if re.fullmatch(r"nothing[;.]\s*(?:this is |only )?agent work", reason.strip().rstrip(" ."), re.I):
        return True
    # "None for the bounded step; later live actions keep their boundaries."
    # scopes the absence to the next action.
    if lead_names(r"(?:none|nothing|n/a)\s+(?:for|before)\s+.+", reason):
        return True
    # A bare "None; next actor is an agent." keeps any hold its note records.
    lead, rest = lead_clause(reason)
    if re.fullmatch(r"none|nothing|n/a", lead, re.I) and rest:
        return not re.search(HOLD_WORDS, rest, re.I)
    lines = reason.splitlines()
    if len(lines) > 1 and github_plan_claim.no_wait_reason(lines[0], field="Waiting for"):
        return not re.search(HOLD_WORDS, "\n".join(lines[1:]), re.I)
    return False


def agent_next_wait(reason: str) -> bool:
    """An agent or the Supervisor acts next, e.g. "Supervisor delivery closeout;
    the Client's acceptance belongs to #2682." That step is agent work."""
    return lead_names(r"(?:the |an? )?(?:next )?(?:agent|supervisor)(?:'s|’s)?(?:\s+[\w/-]+){0,4}", reason)


def non_external_wait(reason: str) -> bool:
    """Recognize agent work and explicit absence; never clear an actual hold."""
    if github_plan_claim.no_wait_reason(reason, field="Waiting for") or agent_next_wait(reason):
        return True
    return bool(re.fullmatch(
        r"(?:the |an |a )?(?:next )?agent(?: selection| assignment)?|"
        r"(?:the )?supervisor(?: routing| to route(?: the (?:PR|train))?)?|"
        r"(?:provider |spare )?capacity|engineering selection|future work|"
        r"future engineering selection|"
        r"nothing[;.]\s*(?:this is |only )?agent work",
        reason.strip().rstrip(" ."), re.I,
    ))


def waiting_records(issue: dict[str, Any], status_text: str) -> list[dict[str, Any]]:
    """A wait belongs to its reporting issue; references are supporting context."""
    records: list[dict[str, Any]] = []
    status_text = re.sub(r"<!--.*?-->", "", status_text, flags=re.S)
    status_text = re.sub(r"\*\*(Waiting for|Parked until|Blocked by|Waiting since)(:?)\*\*(:?)", r"\1\2\3", status_text, flags=re.I)
    verified = re.search(r"(?im)^\s*(?:[-*]\s+)?Last verified:\s*(.+)$", status_text)
    fields = re.split(
        r"(?im)(?=^[ \t]*(?:[-*]\s+)?(?:State|Next action|Blocked by|Waiting for|"
        r"Parked until|Waiting since|Last verified|Validation|Evidence|Retention|Recovery|Worker|Session|Branch|Notes):)|\n[ \t]*\n",
        status_text,
    )
    for entry in fields:
        match = re.match(r"\s*(?:[-*]\s+)?(?:Waiting for|Parked until):\s*(.+)", entry, re.I | re.S)
        if not match:
            continue
        reason = re.sub(r"<!--.*?-->", "", match[1], flags=re.S).strip()
        if not reason:
            continue
        references: dict[tuple[str, int], dict[str, Any]] = {}
        plain = re.sub(r"\[([^]]+)]\(([^)]+)\)", r"\2", reason)
        for ref in re.finditer(
            r"https://github\.com/([^/\s)]+/[^/\s)]+)/(issues|pull)/(\d+)"
            r"|(?<![\w/])([\w.-]+/[\w.-]+)?#(\d+)\b", plain,
        ):
            repo = ref.group(1) or ref.group(4) or issue["repo"]
            number = int(ref.group(3) or ref.group(5))
            kind = ref.group(2) or "issues"
            references[(repo, number)] = {
                "repo": repo, "number": number,
                "url": f"https://github.com/{repo}/{kind}/{number}",
            }
        records.append({
            "repo": issue["repo"], "number": issue["number"], "url": issue["url"],
            "state": issue.get("state"), "plan_status": issue.get("plan_status"),
            "waiting_for": reason, "reported_by": issue["url"],
            "reported_at": issue.get("updated_at"),
            "last_verified": verified[1].strip() if verified else None,
            "references": list(references.values()),
            "no_current_wait": no_current_wait(reason),
            "agent_next": no_current_wait(reason) or agent_next_wait(reason),
            "non_external": no_current_wait(reason) or non_external_wait(reason),
            "unowned": no_current_wait(reason) or non_external_wait(reason) or bool(re.fullmatch(
                r"separately authorized (?:production |live )?(?:activation|work)|live acceptance|approval|authorization", reason.strip().rstrip(" ."), re.I,
            )),
        })
    return records


def next_action_references(status_text: str) -> list[dict[str, Any]]:
    """Issues a Track's Next action names with their repository.

    A Track names work across repositories, so a bare #N is ambiguous and skipped.
    """
    match = re.search(r"(?im)^\s*(?:[-*]\s+)?Next action:\s*(.+)$", status_text)
    if not match:
        return []
    plain = re.sub(r"\[([^]]+)]\(([^)]+)\)", r"\2", match[1])
    references: dict[tuple[str, int], dict[str, Any]] = {}
    for ref in re.finditer(
        r"https://github\.com/([^/\s)]+/[^/\s)]+)/issues/(\d+)"
        r"|(?<![\w/])([\w.-]+/[\w.-]+)?#(\d+)\b", plain,
    ):
        repo = ref.group(1) or ref.group(3)
        if not repo:
            continue
        number = int(ref.group(2) or ref.group(4))
        references[(repo.casefold(), number)] = {
            "repo": repo, "number": number, "url": f"https://github.com/{repo}/issues/{number}",
        }
    return list(references.values())


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
    # An agent's own next step, such as Supervisor closeout, is work to offer.
    rows = waiting_records(item, status_text)
    reports = [row for row in rows if not row["agent_next"]]
    agent_steps = [row["waiting_for"] for row in rows if row["agent_next"] and not row["no_current_wait"]]
    if agent_steps:
        item["agent_next_step"] = agent_steps
    summary = next_relationship_summary(relationships or {})
    if summary["open_sub_issues"]:
        item["open_sub_issues"] = summary["open_sub_issues"]
    if (
        next_plan_status(issue, config) == "waiting"
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


def incident_work_paths(
    roots: list[dict[str, Any]], *, read_node: Callable[[str, int], dict[str, Any]], scan_limit: int,
) -> dict[str, Any]:
    """Follow native incident prerequisites/children with a separate bounded allowance.

    Priority is provenance only. Node exclusions and caller ownership review still
    decide availability; whole-parent waits stop traversal. Each unique descendant
    is read once, and the first current marked root supplies its native path.
    """
    root_keys = {(root["repo"].casefold(), root["number"]) for root in roots}
    pending: list[tuple[dict[str, Any], list[dict[str, Any]]]] = [(root, []) for root in reversed(roots)]
    seen: set[tuple[str, int]] = set()
    items = []
    evaluated = 0
    cycles = []
    truncated = False
    degraded = False
    while pending:
        ref, path = pending.pop()
        key = (ref["repo"].casefold(), ref["number"])
        step = {"repo": ref["repo"], "number": ref["number"], "url": ref["url"]}
        if ref.get("relationship"):
            step["relationship"] = ref["relationship"]
        via = [*path, step]
        if any((entry["repo"].casefold(), entry["number"]) == key for entry in path):
            cycles.append(via)
            continue
        if key in seen:
            continue
        if key not in root_keys:
            if evaluated >= scan_limit:
                truncated = True
                continue
            evaluated += 1
        seen.add(key)
        node = read_node(ref["repo"], ref["number"])
        item: dict[str, Any] = {**node["item"], "incident_via": via}
        items.append(item)
        reason = item.get("exclusion")
        if reason in {"unknown_dependencies", "unknown_ancestry"}:
            degraded = True
        if reason not in {None, "blocked_by_open_dependency", "delegated_to_open_sub_issues"}:
            continue
        edges = [(edge, "blocked_by") for edge in node.get("blockers", [])]
        edges += [(edge, "sub_issue") for edge in node.get("children", [])]
        for edge, relationship in reversed(edges):
            pending.append(({**edge, "relationship": relationship}, via))
    return {"items": items, "context": {"complete": not (truncated or cycles or degraded),
            "descendants_evaluated": evaluated, "descendant_scan_limit": scan_limit,
            "truncated": truncated, "cycles": cycles, "degraded": degraded}}


def rank_direction_work(
    roots: list[dict[str, Any]],
    *,
    milestone_titles: list[str],
    read_node: Callable[[str, int], dict[str, Any]],
    scan_limit: int,
    completed_milestone_titles: list[str] | None = None,
    agent: str | None = None,
    wait_milestone_titles: list[str] | None = None,
) -> dict[str, Any]:
    """Walk ordered Track issues through native blockers and sub-issues.

    read_node returns item (the ordinary next evaluation), blockers, children,
    and waiting (explicit Current Status reports). Each unique issue is read
    once. A shared leaf inherits the earliest overall milestone that reaches it.
    Tracking issues are containers even when their summary label says waiting;
    an ordinary waiting issue remains excluded. Candidates retain their original
    issue milestone and the native path that gives them their overall priority.
    A known other-family assignment has its own scan_limit allowance, preserving
    portfolio evidence without spending the ordinary allowance prematurely.
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
    ordinary_evaluated = 0
    other_family_evaluated = 0
    track_next_actions: dict[str, dict[str, Any]] = {}
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
        if ordinary_evaluated >= scan_limit:
            truncated = True
            break
        seen.add(key)
        node = read_node(ref["repo"], ref["number"])
        mismatch = github_agent.exclusion(node["item"], agent)
        if mismatch and mismatch["exclusion"] == "assigned_elsewhere" and other_family_evaluated < scan_limit:
            other_family_evaluated += 1
        else:
            ordinary_evaluated += 1
        item = {
            **node["item"],
            "blocked_by": node.get("blockers") or [],
            "open_sub_issues": node.get("children") or [],
            "issue_milestone": node["item"].get("milestone"),
            "milestone": milestone,
            "via": via,
        }
        status_text = node.get("status_text")
        if status_text is None:
            status_text = section_map((item.get("discussion") or {}).get("body", "")).get("Current Status", "")
        if not tracking:
            item = check_milestone_wait(item, status_text, wait_milestone_titles or milestone_titles)
        reason = item.get("exclusion")
        blockers = node.get("blockers") or []
        children = node.get("children") or []
        if reason in {"completed", "stale_needs_review", "unknown_dependencies", "pull_request"}:
            excluded.append(item)
            degraded += reason == "unknown_dependencies"
            continue
        reports = [] if item.get("wait_finding") else node.get("waiting") or []
        waiting.extend({**report, "milestone": milestone, "via": via} for report in reports)
        if reason == "waiting" and not reports and not tracking:
            waiting.append({
                **step,
                "milestone": milestone,
                "waiting_for": node.get("status_text") or "Waiting party or condition not recorded",
                "reported_by": item["url"],
                "reported_at": item.get("updated_at"),
            })
        if tracking:
            track_next_actions[milestone["title"]] = {
                "track": item["url"],
                "references": next_action_references(status_text or ""),
            }
        if reason in {"waiting", "label_blocked_without_native_edge"} and not tracking:
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
        elif tracking and not (
            item.get("plan_status") == "active" and not reports
            and re.search(r"(?im)^\s*State:\s*active\b", status_text or "")
            and re.search(r"(?im)^\s*Next action:.*\b(?:agent|supervisor)\b", status_text or "")
        ):
            excluded.append({**item, "exclusion": "tracking_without_open_work"})
        elif tracking:
            # An active Track with no linked work issue and no recorded wait
            # is itself the next step when that step names an agent, such as scoping
            # its first issue.
            item.pop("exclusion", None)
            item["reasons"] = [
                f"direction_milestone_{order[milestone['title']] + 1}",
                "tracking_issue_next_action_without_work_issue",
                *(item.get("reasons") or []),
            ]
            candidates.append(item)
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
        "findings": [item["wait_finding"] for item in [*candidates, *excluded] if item.get("wait_finding")],
        "completed_milestones": [title for title in milestone_titles if title in completed],
        "tracking_roots": [{"repo": root["repo"], "number": root["number"]} for root in tracks],
        "tracking_milestones": [{"repo": root["repo"], "number": root["number"], "milestone": root["milestone"]} for root in tracks],
        "evaluated": len(seen),
        "truncated": truncated,
        "track_next_actions": {
            title: {**entry, "references": [ref for ref in entry["references"]
                                            if (ref["repo"].casefold(), ref["number"]) not in seen]}
            for title, entry in track_next_actions.items()
        },
        "dependency_context": {
            "complete": not (degraded or truncated or missing),
            "degraded_count": degraded,
            "missing_tracking_milestones": missing,
        },
    }


def milestone_summary(
    milestone_titles: list[str], *, completed: list[str], candidates: list[dict[str, Any]],
    graph_waits: list[dict[str, Any]], track_next_actions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Say, for each open listed milestone, what work it offers or who it waits on.

    A run without milestone candidates must still name each milestone's waits,
    so unrelated work never reads as the top item by default.
    """
    summary: list[dict[str, Any]] = []
    for title in milestone_titles:
        if title in completed:
            continue
        work = [{"repo": item["repo"], "number": item["number"], "url": item["url"],
                 "availability": item.get("availability")}
                for item in candidates
                if (item.get("via") and (item.get("milestone") or {}).get("title") == title)
                or title in (item.get("overall_milestone_context") or {}).get("titles", [])]
        waits: list[dict[str, Any]] = []
        for row in graph_waits:
            record = {"repo": row["repo"], "number": row["number"], "url": row["url"],
                      "waiting_for": row.get("waiting_for")}
            if (row.get("milestone") or {}).get("title") == title and record not in waits:
                waits.append(record)
        track = track_next_actions.get(title) or {}
        summary.append({
            "milestone": title,
            "track": track.get("track"),
            "state": "work_listed" if work else "waiting" if waits else "no_linked_work",
            "work": work,
            "waits": waits,
            # Named by the Track's Next action but not reached over native edges.
            "next_action_outside_graph": track.get("references", []),
        })
    return summary
