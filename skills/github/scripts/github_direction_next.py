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

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import github_plan_graph
import github_plan_claim
import github_plan_wait
import github_plan_status
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


def discovery_scan(inventory: list[dict[str, Any]], scan_limit: int | None) -> list[dict[str, Any]]:
    incidents = [item for item in inventory if is_live_breakage(item)]
    ordinary = [item for item in inventory if not is_live_breakage(item)]
    return [*incidents, *ordinary[:scan_limit]]


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


def milestone_closure_review(body: str, status_text: str | None = None) -> dict[str, Any]:
    sections = section_map(body)
    status = sections.get("Current Status", "") if status_text is None else status_text
    return {
        "track_proof": sections.get("Finish Line") or sections.get("Objective") or None,
        "recorded_status": status or None,
    }


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
    structured = item.get("structured_wait")
    if structured is not None:
        rows = waiting_records(item, status_text)
        return {"requested": bool(rows) or item.get("plan_status") == "waiting",
                "valid": True, "reason": None,
                "waiting_for": "; ".join(row["waiting_for"] for row in rows),
                "since": None, "recorded_at": item.get("updated_at")}
    status_text = re.sub(r"<!--.*?-->", "", status_text, flags=re.DOTALL)
    status_text = re.sub(r"\*\*(Waiting for|Parked until|Blocked by|Waiting since)(:?)\*\*(:?)", r"\1\2\3", status_text, flags=re.IGNORECASE)
    rows = waiting_records(item, status_text)
    pending = [row for row in rows if not row["no_current_wait"]]
    valid_pending = [row for row in pending
                     if not unnamed_wait_reason(row["waiting_for"])
                     and not milestone_order_wait(row["waiting_for"], milestone_titles)]
    other_holds = [match[1].strip() for match in re.finditer(r"(?im)^\s*(?:[-*]\s+)?Blocked by:[ \t]*([^\n]+)", status_text)
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
    status_text = re.sub(r"\*\*(Waiting for|Parked until)(:?)\*\*(:?)", r"\1\2\3", status_text, flags=re.I)
    if (item.get("exclusion") == "wait_review_required" and not re.search(
            r"(?im)^\s*(?:[-*]\s+)?(?:Waiting for|Parked until):", status_text)):
        return item
    evidence = milestone_wait_evidence(item, status_text, milestone_titles)
    if not evidence["requested"] or evidence["valid"]:
        return item
    finding = {"kind": "milestone_wait_invalid", "repo": item["repo"], "number": item["number"],
               "url": item["url"], **evidence}
    result = {**item, "wait_finding": finding}
    if item.get("blocked_by"):
        return {**result, "exclusion": "blocked_by_open_dependency"}
    if result.get("exclusion") in {"waiting", "wait_review_required"}:
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
        if status == "done" and github_plan_status.reopened(issue):
            continue
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
    base: dict[str, Any] = {
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
    structured_wait: dict[str, Any] | None = None,
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
        if structured_wait is not None and relationships is not None and relationship_error is None:
            derived = github_plan_status.derive(issue, relationships, structured_wait, label_names=config["labels"])
            static_exclusion.update(plan_status=derived["status"], derived=derived,
                                    blocked_by=derived["blocked_by"])
        return "excluded", static_exclusion
    if relationship_error is not None or relationships is None:
        return "excluded", {
            **base,
            "exclusion": "unknown_dependencies",
            "evidence": [],
            "detail": relationship_error or "dependency reads unavailable",
        }

    summary = next_relationship_summary(relationships)
    if structured_wait is not None:
        derived = github_plan_status.derive(issue, relationships, structured_wait, label_names=config["labels"])
        status = derived["status"]
        base.update(plan_status=status, derived=derived, blocked_by=derived["blocked_by"])
        base["recorded_labels"] = base["labels"]
        base["labels"] = github_plan_status.project_labels(base["labels"], config["labels"], derived)
    open_blockers = summary["open_blockers"]
    if open_blockers:
        return "excluded", {
            **base,
            "exclusion": "blocked_by_open_dependency",
            "evidence": relationship_refs(open_blockers, str(issue.get("repo") or "")),
            "blocked_by": open_blockers,
        }
    if status == "blocked" and structured_wait is None:
        return "excluded", {
            **base,
            "exclusion": "label_blocked_without_native_edge",
            "evidence": [],
            "inconsistency": True,
        }
    if status == "waiting":
        return "excluded", {**base, "exclusion": "waiting", "evidence": []}
    if status == "hold":
        return "excluded", {**base, "exclusion": "scope_review_required", "evidence": [],
                            "scope_status": base["derived"]["scope_status"]}
    if status == "review":
        return "excluded", {**base, "exclusion": "wait_review_required", "evidence": [], "wait_review_required": True}
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
        "author": github_plan_graph.rest_author(issue.get("user"))["login"],
        "updated_at": issue.get("updated_at"),
        **({"body_recorded_at": issue.get("last_edited_at") or issue.get("created_at")} if "last_edited_at" in issue else {}),
        "comments": [{
            "id": comment.get("id"), "author": github_plan_graph.rest_author(comment.get("user"))["login"],
            "created_at": comment.get("created_at"),
            "updated_at": comment.get("updated_at"), "url": comment.get("html_url"),
            "body": comment.get("body") or "",
        } for comment in comments],
        "complete": complete,
    }
    return snapshot


def whole_plan_wait(parent: dict[str, Any]) -> bool:
    """Preserve an explicit hold on all preparation across prerequisite links."""
    record = parent.get("structured_wait")
    if parent.get("exclusion") in {"waiting", "wait_review_required"} and (record is None or parent.get("wait_review_required")):
        return True  # Legacy scope is unreviewed; do not release its links.
    record = record or {}
    conditions = " ".join(value for wait in record.get("waits", []) for value in wait.values())
    return bool(re.search(r"\b(?:all|any|whole|entire)\s+(?:(?:source|further|engineering)\s+)?(?:work|preparation|implementation|plan)\b", conditions, re.I))


def parent_holds_child(parent: dict[str, Any], child: dict[str, Any]) -> bool:
    """A stale label is an issue finding; a real parent wait still has scope."""
    if parent.get("exclusion") not in {"waiting", "wait_review_required"}:
        return False
    if whole_plan_wait(parent):
        return True
    evidence = (parent.get("structured_wait") or {}).get("evidence", "")
    # Existing wait evidence can explicitly split out an independently
    # selectable child without clearing the parent's person/event wait.
    for clause in re.split(r"[;\n]|[.]\s+", evidence):
        if re.search(r"\b(?:if|once|after|when|until|only|unless|not|never)\b", clause, re.I):
            continue
        if not re.search(r"\b(?:(?:owns?|is|remains?)\s+independently\s+selectable|can\s+proceed\s+independently)\b", clause, re.I):
            continue
        refs = issue_references(clause, repo=parent["repo"], known_repositories={parent["repo"], child["repo"]})
        if any(ref["repo"].casefold() == child["repo"].casefold() and ref["number"] == child["number"] for ref in refs):
            return False
        if parent["repo"].casefold() == child["repo"].casefold() and re.search(rf"\bNative\s+{child['number']}\b", clause, re.I):
            return False
    return True


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
    result = {**item, "discussion": discussion}
    if not complete:
        return {**result, "exclusion": "unknown_ancestry"}
    descendant = item
    for parent in parents:
        if (parent["repo"].casefold(), parent["number"]) in tracking:
            continue
        if parent_holds_child(parent, descendant):
            return {**result, "exclusion": "parent_waiting", "waiting_on_parent": parent["url"], "_own_exclusion": item.get("exclusion")}
        descendant = parent
    return result


def overall_milestone_context(
    item: dict[str, Any], graph: dict[str, Any], milestone_titles: list[str],
    repository_milestones: dict[str, list[str] | None],
) -> dict[str, Any]:
    """Resolve established waypoint links used for eligibility and ranking."""
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


def issue_key(item: dict[str, Any]) -> tuple[str, int]:
    return item["repo"].casefold(), item["number"]


@dataclass
class MilestoneScope:
    contexts: dict[tuple[str, int], dict[str, Any]]
    edges: dict[tuple[str, int], set[tuple[str, int]]]
    missing: bool


def resolve_milestone_scope(
    graph: dict[str, Any], entries: list[dict[str, Any]], *,
    milestone_titles: list[str], repository_waypoints: dict[str, list[str] | None],
) -> MilestoneScope:
    indexed: dict[tuple[str, int], dict[str, Any]] = {}
    for item in entries:
        indexed.setdefault(issue_key(item), item)
    edges = {ident: {issue_key(dependency) for dependency in [*item.get("blocked_by", []), *item.get("open_sub_issues", [])]}
             for ident, item in indexed.items()}
    contexts = {ident: overall_milestone_context(item, graph, milestone_titles, repository_waypoints)
                for ident, item in indexed.items()}
    pending = [ident for ident, context in contexts.items() if context["state"] == "matched"]
    missing = False
    while pending:
        ident = pending.pop()
        for dependency in edges[ident]:
            if dependency not in indexed:
                missing = True
                continue
            context = contexts[dependency]
            titles = set(context["titles"]) | set(contexts[ident]["titles"])
            if titles == set(context["titles"]):
                continue
            contexts[dependency] = {"state": "matched", "titles": [title for title in milestone_titles if title in titles],
                                    "source": "native_milestone_scope"}
            pending.append(dependency)
    return MilestoneScope(contexts, edges, missing)


def no_available_milestone_item(
    graph: dict[str, Any], entries: list[dict[str, Any]], available: list[dict[str, Any]], *,
    scope: MilestoneScope, milestone_titles: list[str], repository_waypoints: dict[str, list[str] | None],
    coverage_complete: bool,
) -> bool | None:
    indexed = {issue_key(item): item for item in reversed(entries)}
    matched = {ident for ident, context in scope.contexts.items() if context["state"] == "matched"}
    if any(issue_key(item) in matched for item in available):
        return False
    if scope.missing or not coverage_complete or not graph.get("dependency_context", {}).get("complete"):
        return None
    remaining = set(matched)
    while remaining:
        leaves = {ident for ident in remaining if not scope.edges[ident].intersection(remaining)}
        if not leaves:
            return None
        remaining.difference_update(leaves)
    for item in indexed.values():
        if item.get("exclusion") in {"completed", "outside_direction_tracks"}:
            continue
        context = scope.contexts[issue_key(item)]
        if context["state"] == "unknown":
            ancestry = overall_milestone_context({**item, "blocking": []}, graph, milestone_titles, repository_waypoints)
            if ancestry["state"] != "none_found":
                return None
    for ident in matched:
        item = indexed[ident]
        discussion = item.get("discussion") or {}
        if not discussion.get("complete") or discussion.get("ancestry_complete") is False:
            return None
        exclusion = item.get("exclusion")
        if exclusion in {"tracking", "tracking_without_open_work"}:
            continue
        if (item.get("wait_review_required") or item.get("stale_wait_evidence")
                or item.get("wait_evidence_complete") is False):
            return None
        queue = item.get("queue") or {}
        if queue.get("state") == "needs_status" or queue.get("state") == "review":
            return None
        if queue.get("state") == "underway":
            continue
        if queue.get("state") == "waiting" and queue.get("reason") == "recorded_next_actor":
            continue
        if exclusion in {"blocked_by_open_dependency", "delegated_to_open_sub_issues"}:
            continue
        if exclusion == "waiting" or queue.get("state") == "waiting":
            status = section_map(discussion.get("body", "")).get("Current Status", "")
            if (item.get("wait_evidence_complete") is True and not item.get("wait_finding")
                    and milestone_wait_evidence(item, status, milestone_titles)["valid"]):
                continue
            return None
        if exclusion == "parent_waiting":
            parent = next((parent for parent in discussion.get("parents", [])
                           if parent.get("url") == item.get("waiting_on_parent")), {})
            if (parent.get("wait_evidence_complete") is True and not parent.get("stale_wait_evidence")
                    and not parent.get("wait_review_required") and (parent.get("discussion") or {}).get("complete")):
                continue
        return None
    return True


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


def actor_wait_record(item: dict[str, Any]) -> dict[str, Any]:
    return {"repo": item["repo"], "number": item["number"], "url": item["url"],
            "state": item.get("state"), "plan_status": item.get("plan_status"),
            "waiting_for": item["queue"]["next_actor"], "reported_by": item["url"],
            "reported_at": item.get("updated_at"), "last_verified": None, "references": [],
            "no_current_wait": False, "agent_next": False, "non_external": False, "unowned": False,
            "source": "next_actor", "milestone": item.get("milestone"),
            "overall_milestone_context": item.get("overall_milestone_context"),
            "wait_review_required": item["queue"].get("wait_review_required", False)}


def rank_portfolio_work(
    graph: dict[str, Any], discoveries: list[dict[str, Any]], *,
    milestone_titles: list[str],
    repository_milestones: dict[str, list[str] | None] | None = None,
    repository_waypoints: dict[str, list[str] | None] | None = None,
    coverage_complete: bool = False,
    repository_clients: dict[str, dict[str, Any]] | None = None,
    director_owner: str | None = None,
    bot_logins: tuple[str, ...] = (),
    reference_repositories: set[str] | None = None,
) -> dict[str, Any]:
    def client_milestone(entry: dict[str, Any]) -> dict[str, Any]:
        return with_client_milestone(entry, graph, milestone_titles=milestone_titles,
                                     repository_clients=repository_clients, repository_waypoints=repository_waypoints,
                                     director_owner=director_owner, bot_logins=bot_logins)

    graph = {**graph, "candidates": [client_milestone(item) for item in graph.get("candidates", [])],
             "excluded": [client_milestone(item) for item in graph.get("excluded", [])]}
    discoveries = [client_milestone(item) for item in discoveries]
    findings = list(graph.get("findings", []))
    scope = resolve_milestone_scope(graph, [*graph.get("candidates", []), *discoveries, *graph.get("excluded", [])],
                                   milestone_titles=milestone_titles, repository_waypoints=repository_waypoints or {})

    def milestone_context(item: dict[str, Any]) -> dict[str, Any]:
        scoped_context = scope.contexts.get(issue_key(item))
        return scoped_context if scoped_context is not None else overall_milestone_context(item, graph, milestone_titles, repository_waypoints or {})

    def checked_wait(item: dict[str, Any]) -> dict[str, Any]:
        if milestone_context(item)["state"] != "matched":
            return {key: value for key, value in item.items() if key != "_own_exclusion"}
        if item.get("exclusion") == "parent_waiting":
            valid_parent = None
            tracking = {(root["repo"].casefold(), root["number"]) for root in graph.get("tracking_roots", [])}
            descendant = item
            for parent in (item.get("discussion") or {}).get("parents", []):
                if (parent["repo"].casefold(), parent["number"]) in tracking:
                    continue
                status = section_map((parent.get("discussion") or {}).get("body", "")).get("Current Status", "")
                checked = check_milestone_wait(parent, status, milestone_titles)
                if checked.get("wait_finding") and checked["wait_finding"] not in findings:
                    findings.append(checked["wait_finding"])
                if parent_holds_child(checked, descendant):
                    valid_parent = parent
                    break
                descendant = parent
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
    candidates: list[dict[str, Any]] = []
    excluded = list(graph.get("excluded", []))
    underway: list[dict[str, Any]] = []
    needs_status: list[dict[str, Any]] = []
    actor_waits: list[dict[str, Any]] = []
    seen = {f"{entry['repo']}#{entry['number']}".casefold() for entry in excluded if entry.get("exclusion") != "outside_direction_tracks"}
    work = [(entry, False) for entry in graph["candidates"]] + [(entry, True) for entry in discoveries]
    for raw, discovered in work:
        key = f"{raw['repo']}#{raw['number']}".casefold()
        if key in seen:
            continue
        seen.add(key)
        item: dict[str, Any] = {**raw, "availability": "needs_review"}
        item["overall_milestone_context"] = milestone_context(item)
        if item.get("exclusion"):
            excluded.append(item)
            continue
        discussion = item.get("discussion") or {}
        queue = item.get("queue") or {}
        if queue.get("wait_review_required"):
            item["wait_review_required"] = True
        if queue.get("state") == "underway":
            underway.append({**item, "availability": "underway"})
            continue
        if queue.get("state") == "needs_status":
            needs_status.append(item)
            continue
        if queue.get("state") == "waiting":
            if queue.get("reason") == "recorded_next_actor":
                actor_waits.append(actor_wait_record(item))
                continue
            excluded.append({**item, "exclusion": "recorded_wait"})
            continue
        if discovered and discussion.get("ancestry_complete") is not True:
            item["review_required"] = "complete_parent_context"
        elif not discussion.get("complete"):
            item["review_required"] = "complete_issue_discussion"
        else:
            category = ("milestone" if item["overall_milestone_context"]["state"] == "matched" else
                        "live_incident" if is_live_breakage(item) else item.get("direction_category"))
            if queue.get("state") == "available" and category in {"milestone", "live_incident", "own_project"}:
                item.update(availability="available", category=category, claim_required=True)
            else:
                item["review_required"] = "direction_discussion_and_current_ownership"
        candidates.append(item)
    for repo, titles in (repository_milestones or {}).items():
        group = [item for item in candidates if not item.get("via") and item["repo"].casefold() == repo.casefold()]
        rank_next_candidates(group, direction_milestones=titles)
        for item in group:
            item["repository_rank"] = item["rank"]
    rank_next_candidates(candidates, direction_milestones=milestone_titles)
    priority = {"live_incident": 0, "milestone": 1, "own_project": 3}
    candidates.sort(key=lambda candidate: (
        0 if is_live_breakage(candidate) else priority.get(candidate.get("category") or "", 1 if candidate.get("via") or candidate.get("client_request") else 5),
        (min((milestone_titles.index(title) for title in candidate["overall_milestone_context"]["titles"] if title in milestone_titles), default=len(milestone_titles)) if candidate.get("category") == "milestone" and not is_live_breakage(candidate) else candidate.get("repository_rank", candidate["rank"])),
        -len(candidate.get("blocking") or []) if candidate.get("category") == "milestone" else 0,
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
    entries = [*graph.get("candidates", []), *excluded, *discoveries, *underway, *needs_status]
    parents = [parent for entry in entries for parent in (entry.get("discussion") or {}).get("parents", [])]
    invalid_waits = {finding["url"]: finding for finding in findings}
    for entry in [*entries, *parents]:
        if entry.get("url") in invalid_waits:
            entry = {**entry, "wait_finding": invalid_waits[entry["url"]]}
        if entry.get("exclusion") in {"completed", "stale_needs_review", "outside_direction_tracks"} or (entry.get("queue") or {}).get("reason") == "recorded_next_actor":
            continue
        context = milestone_context(entry)
        body = (entry.get("discussion") or {}).get("body", "")
        status = section_map(body).get("Current Status", "")
        rows = waiting_records(entry, status, reference_repositories=reference_repositories)
        if not rows and (entry.get("plan_status") == "waiting" or entry.get("exclusion") == "wait_review_required"):
            rows = [{"repo": entry["repo"], "number": entry["number"], "url": entry["url"],
                     "waiting_for": "Waiting party or condition not recorded", "reported_by": entry["url"],
                     "reported_at": entry.get("updated_at"), "last_verified": None,
                     "references": [], "non_external": True,
                     **({"source": "legacy_wait_review"} if entry.get("wait_review_required") or (entry.get("queue") or {}).get("wait_review_required") else {})}]
        for row in rows:
            row = {**row, "milestone": entry.get("milestone"), "overall_milestone_context": context}
            key = (row["repo"].casefold(), row["number"], row["waiting_for"])
            if key in seen_waits:
                continue
            seen_waits.add(key)
            if row.get("unowned", row["non_external"]):
                if entry.get("plan_status") == "waiting" or entry.get("exclusion") in {"waiting", "wait_review_required"}:
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
    waiting = [*current_waits, *actor_waits]
    available = [item for item in candidates if item["availability"] == "available"]
    return {
        "candidates": candidates, "candidate_count": len(candidates),
        "available_candidates": available, "available_candidate_count": len(available),
        "review_required_count": len(candidates) - len(available),
        "underway": underway, "waiting": waiting, "unowned": unowned,
        "underway_count": len(underway), "verification_only": [], "verification_only_count": 0,
        "scope_review": [], "scope_review_count": 0,
        "needs_status": needs_status, "needs_status_count": len(needs_status),
        "recorded_waits": recorded_waits, "stale_waits": stale_waits, "excluded": excluded,
        "tooling_capacity_context": {},
        "all_listed_milestones_have_no_available_item": no_available_milestone_item(
            graph, entries, available, scope=scope, milestone_titles=milestone_titles,
            repository_waypoints=repository_waypoints or {}, coverage_complete=coverage_complete,
        ),
        "findings": findings,
        "repository_holds": {},
        "ownership_context": {"source": "observed_queue", "all_active_sessions_searched": False, "claim_required": True},
    }


word_pattern = github_plan_claim.word_pattern
PRECONDITION_WORDS = github_plan_claim.PRECONDITION_WORDS
LEAD_HOLD_WORDS = github_plan_claim.LEAD_HOLD_WORDS
LATER_HOLD_WORDS = github_plan_claim.LATER_HOLD_WORDS
HOLD_WORDS = github_plan_claim.HOLD_WORDS


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


def waiting_records(issue: dict[str, Any], status_text: str, *, reference_repositories: set[str] | None = None) -> list[dict[str, Any]]:
    """A wait belongs to its reporting issue; references are supporting context."""
    structured = issue.get("structured_wait")
    if structured is not None:
        if structured["state"] != "waiting":
            return []
        rows = []
        for wait in structured["waits"]:
            rows.append({
                "repo": issue["repo"], "number": issue["number"], "url": issue["url"],
                "state": issue.get("state"), "plan_status": issue.get("plan_status"),
                "waiting_for": wait["waiting_for"], "ends_when": wait["ends_when"],
                "reported_by": issue["url"], "reported_at": issue.get("updated_at"),
                "last_verified": None,
                "references": issue_references(wait["waiting_for"] + "\n" + wait["ends_when"], repo=issue["repo"], known_repositories=reference_repositories),
                "no_current_wait": False, "agent_next": False,
                "non_external": False, "unowned": False, "source": "structured_wait",
            })
        return rows
    records: list[dict[str, Any]] = []
    status_text = re.sub(r"<!--.*?-->", "", status_text, flags=re.S)
    status_text = re.sub(r"\*\*(Waiting for|Parked until|Blocked by|Waiting since|Last verified)(:?)\*\*(:?)", r"\1\2\3", status_text, flags=re.I)
    verified = re.search(r"(?im)^\s*(?:[-*]\s+)?Last verified:[ \t]*(.+)$", status_text)
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
        references = issue_references(reason, repo=issue["repo"], known_repositories=reference_repositories)
        records.append({
            "repo": issue["repo"], "number": issue["number"], "url": issue["url"],
            "state": issue.get("state"), "plan_status": issue.get("plan_status"),
            **({"source": "legacy_wait_review", "review_required": True} if issue.get("wait_review_required") else {}),
            "waiting_for": reason, "reported_by": issue["url"],
            "reported_at": issue.get("updated_at"),
            "last_verified": verified[1].strip() if verified else None,
            "references": references,
            "no_current_wait": no_current_wait(reason),
            "agent_next": no_current_wait(reason) or agent_next_wait(reason),
            "non_external": no_current_wait(reason) or non_external_wait(reason),
            "unowned": no_current_wait(reason) or non_external_wait(reason) or bool(re.fullmatch(
                r"separately authorized (?:production |live )?(?:activation|work)|live acceptance|approval|authorization", reason.strip().rstrip(" ."), re.I,
            )),
        })
    if not records and issue.get("wait_review_required"):
        state = re.search(r"(?im)^\s*(?:[-*]\s+)?State:\s*((?:waiting (?:on|for)|parked until)\s+\S[^\n]*)", status_text)
        if state:
            records.append({
                "repo": issue["repo"], "number": issue["number"], "url": issue["url"],
                "state": issue.get("state"), "plan_status": issue.get("plan_status"),
                "waiting_for": state[1], "reported_by": issue["url"],
                "reported_at": issue.get("updated_at"), "last_verified": None,
                "references": issue_references(state[1], repo=issue["repo"], known_repositories=reference_repositories),
                "no_current_wait": False, "agent_next": False, "non_external": False,
                "unowned": False, "source": "legacy_wait_review", "review_required": True,
            })
    return records


def next_action_references(status_text: str) -> list[dict[str, Any]]:
    """Issues a Track's Next action names with their repository.

    A Track names work across repositories, so a bare #N is ambiguous and skipped.
    """
    match = re.search(r"(?im)^\s*(?:[-*]\s+)?Next action:[ \t]*(.+)$", status_text)
    if not match:
        return []
    return issue_references(match[1], repo=None)


def issue_references(text: str, *, repo: str | None, known_repositories: set[str] | None = None) -> list[dict[str, Any]]:
    """Resolve repository shorthand only within its reporting owner's context."""
    plain = re.sub(r"\[([^]]+)]\(([^)]+)\)", r"\2", text)
    references: dict[tuple[str, int], dict[str, Any]] = {}
    previous_end = -1
    for ref in re.finditer(
        r"https://github\.com/([^/\s)]+/[^/\s)]+)/(issues|pull)/(\d+)"
        r"|(?<![\w/#])([\w.-]+/[\w.-]+)#(\d+)\b"
        r"|(?<![\w#])([\w.-]+)#(\d+)\b"
        r"|(?<![\w/])#(\d+)\b", plain,
    ):
        target = ref.group(1) or ref.group(4)
        if not target:
            # A slash separates adjacent recognized issue references; an
            # arbitrary URL/file path cannot establish repository shorthand.
            if ref.start() and plain[ref.start() - 1] == "/" and ref.start() != previous_end + 1:
                continue

            if repo is None:
                continue
            target = repo.split("/")[0] + "/" + ref.group(6) if ref.group(6) else repo
            if ref.group(6) and target.casefold() not in {name.casefold() for name in (known_repositories or {repo})}:
                continue
        number = int(ref.group(3) or ref.group(5) or ref.group(7) or ref.group(8))
        previous_end = ref.end()
        key = (target.casefold(), number)
        # A bare #N only assumes this repository; any named form of it makes it explicit.
        bare = bool(ref.group(8)) and references.get(key, {"bare": True}).get("bare", False)
        references[key] = {
            "repo": target, "number": number,
            "url": f"https://github.com/{target}/{ref.group(2) or 'issues'}/{number}",
            **({"bare": True} if bare else {}),
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
    structured_wait: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify raw issue evidence identically for CLI and service consumers.

    Supply structured_wait from the claim wait resolver after a complete,
    authorized discussion read. Omission retains legacy review evidence only.
    """
    if truncated_relationships:
        relationship_error = "Relationship evidence exceeded the reader's collection limit"
    _, item = evaluate_next_plan(
        issue, config=config, focus=focus, relationships=relationships,
        relationship_error=relationship_error,
        structured_wait=structured_wait,
    )
    if truncated_relationships:
        item["truncated_relationships"] = truncated_relationships
    if item.get("exclusion") in {"completed", "stale_needs_review", "scope_review_required", "unknown_dependencies"}:
        return {"item": item}
    if issue.get("pull_request") is not None:
        return {"item": {**item, "exclusion": "pull_request"}}
    status_text = section_map(issue.get("body") or "").get("Current Status", "")
    status_text = re.sub(r"<!--.*?-->", "", status_text, flags=re.S).strip()
    if structured_wait is not None:
        github_plan_wait.validate(structured_wait)
        item["wait_review_required"] = structured_wait["state"] == "review"
        if structured_wait["state"] != "review":
            item["structured_wait"] = structured_wait
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
        (structured_wait is None and next_plan_status(issue, config) == "waiting")
        or ((structured_wait is None or structured_wait["state"] == "review") and re.search(r"(?im)^\s*State:\s*(?:waiting|parked)\b", status_text))
        or (structured_wait is not None and structured_wait["state"] == "waiting")
        or (reports and not (summary["open_blockers"] or summary["open_sub_issues"]))
    ):
        item["exclusion"] = "waiting"
    if item.get("wait_review_required") and item.get("exclusion") == "waiting" and item.get("plan_status") != "waiting":
        item["exclusion"] = "wait_review_required"
    return {
        "item": item,
        "blockers": summary["open_blockers"],
        "children": summary["open_sub_issues"],
        "waiting": reports,
        "status_text": status_text,
    }


def incident_work_paths(
    roots: list[dict[str, Any]], *, read_node: Callable[[str, int], dict[str, Any]], scan_limit: int | None,
    prefetch_nodes: Callable[[list[dict[str, Any]]], None] | None = None,
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
        if prefetch_nodes is not None:
            allowance = max(0, scan_limit - evaluated) + len(root_keys.difference(seen)) if scan_limit is not None else None
            prefetch_nodes([ref for ref, _path in reversed(pending)
                            if (ref["repo"].casefold(), ref["number"]) not in seen][:allowance])
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
            if scan_limit is not None and evaluated >= scan_limit:
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
    scan_limit: int | None,
    completed_milestone_titles: list[str] | None = None,
    agent: str | None = None,
    wait_milestone_titles: list[str] | None = None,
    prefetch_nodes: Callable[[list[dict[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    """Walk ordered Track issues through native blockers and sub-issues.

    read_node returns item (the ordinary next evaluation), blockers, children,
    and waiting (explicit Current Status reports). Each unique issue is read
    once. A shared leaf inherits the earliest overall milestone that reaches it.
    Tracking issues are containers even when their summary label says waiting;
    an ordinary waiting issue remains excluded while its native links are read.
    Parent waits follow sub-issues; independent blockers and explicitly split
    children remain selectable. Candidates retain their original
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
    routes: set[tuple[str, int, bool]] = set()
    node_cache: dict[tuple[str, int], dict[str, Any]] = {}
    unavailable_relationships: list[dict[str, Any]] = []
    waits_recorded: set[tuple[str, int]] = set()
    degraded = 0
    truncated = False
    track_next_actions: dict[str, dict[str, Any]] = {}
    from github_next_queue import current_status
    closure_reviews: list[dict[str, Any]] = []
    for index, root in enumerate(tracks):
        if prefetch_nodes is not None:
            prefetch_nodes(tracks[index:])
        key = root["repo"].casefold(), root["number"]
        node_cache[key] = read_node(root["repo"], root["number"])
        discussion = node_cache[key]["item"].get("discussion") or {}
        status = current_status(discussion, github_plan_wait.authors(root["repo"]),
            body_recorded_at=discussion.get("body_recorded_at")) if discussion.get("complete") else ""
        closure_reviews.append({"url": root["url"], "milestone": root["milestone"]["title"],
            **milestone_closure_review(discussion.get("body", ""), status),
            "discussion_complete": bool(discussion.get("complete"))})
    # A stack keeps even a deep dependency chain within the explicit scan bound.
    pending: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], bool, list[dict[str, Any]]]] = [
        (root, root["milestone"], [], True, []) for root in reversed(tracks)
    ]
    while pending:
        scheduled = sorted(enumerate(pending), key=lambda row: (
            order[row[1][1]["title"]], bool(row[1][4]), -row[0]))
        if prefetch_nodes is not None:
            # Read ahead only within the remaining ordinary allowance. Logical
            # DFS order, assignment allowances and coverage stay unchanged.
            refs = [entry[0] for _index, entry in scheduled
                    if (entry[0]["repo"].casefold(), entry[0]["number"]) not in seen]
            prefetch_nodes(refs[:max(0, scan_limit - len(seen))] if scan_limit is not None else refs)
        # Evaluate the visible unheld frontier before spending the remaining
        # allowance on held subtrees. Those are still read, with the same bound.
        next_index = scheduled[0][0]
        ref, milestone, path, tracking, held_parents = pending.pop(next_index)
        key = (ref["repo"].casefold(), ref["number"])
        step = {"repo": ref["repo"], "number": ref["number"], "url": ref["url"]}
        if "relationship" in ref:
            step["relationship"] = ref["relationship"]
        via = [*path, step]
        if any((entry["repo"].casefold(), entry["number"]) == key for entry in path):
            excluded.append({**step, "milestone": milestone, "via": via, "exclusion": "dependency_cycle"})
            degraded += 1
            continue
        held = bool(held_parents)
        route_key = (*key, held)
        if route_key in routes or (held and (*key, False) in routes):
            continue
        if key not in seen and scan_limit is not None and len(seen) >= scan_limit:
            truncated = True
            break
        routes.add(route_key)
        if key not in seen:
            seen.add(key)
            if key not in node_cache:
                node_cache[key] = read_node(ref["repo"], ref["number"])
        elif not held:
            # A later independent route supersedes the earlier held route,
            # without a second issue read or a second scan allowance.
            excluded[:] = [row for row in excluded if (row["repo"].casefold(), row["number"]) != key]
        node = node_cache[key]
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
            if reason == "unknown_dependencies":
                unavailable_relationships.append({**step, "detail": item.get("detail"),
                                                  "truncated_relationships": item.get("truncated_relationships", [])})
            continue
        reports = [] if item.get("wait_finding") else node.get("waiting") or []
        if key not in waits_recorded:
            waiting.extend({**report, "milestone": milestone, "via": via} for report in reports)
        if key not in waits_recorded and reason in {"waiting", "wait_review_required"} and not reports and not tracking:
            waiting.append({
                **step,
                "milestone": milestone,
                **({"source": "legacy_wait_review"} if reason == "wait_review_required" else {}),
                "waiting_for": node.get("status_text") or "Waiting party or condition not recorded",
                "reported_by": item["url"],
                "reported_at": item.get("updated_at"),
            })
        waits_recorded.add(key)
        if tracking:
            track_next_actions[milestone["title"]] = {
                "track": item["url"],
                "references": next_action_references(status_text or ""),
            }
        if reason == "label_blocked_without_native_edge":
            item["label_finding"] = {"kind": "stale_blocked_label", **step}
        own_item = item
        if held_parents:
            item = include_parent_context(item, held_parents, complete=True, tracking_roots=tracks)
        if blockers or children:
            excluded.append({**item, "exclusion": "tracking" if tracking else item.get("exclusion") or "delegated_to_open_sub_issues"})
            # Read blockers first, then independent child work. Rank all actionable
            # leaves with the same milestone/dependency/age ordering as local next.
            edges = [(edge, "blocked_by") for edge in blockers]
            edges += [(edge, "sub_issue") for edge in children]
            child_keys = {(edge["repo"].casefold(), edge["number"]) for edge in children}
            for edge, relationship in reversed(edges):
                edge_is_child = (edge["repo"].casefold(), edge["number"]) in child_keys
                if edge_is_child:
                    inherits = bool(held_parents) or (not tracking and parent_holds_child(own_item, edge))
                else:
                    inherits = (not tracking and whole_plan_wait(own_item)) or any(whole_plan_wait(parent) for parent in held_parents)
                # Keep intermediate ancestors as well as wait owners: a named
                # exemption belongs to that parent's immediate native child.
                parents = [own_item, *held_parents] if inherits else []
                pending.append(({**edge, "relationship": relationship}, milestone, via, False, parents))
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
        elif item.get("exclusion"):
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
        "milestone_closure_reviews": closure_reviews,
        "excluded": excluded,
        "waiting": waiting,
        "findings": [item[field] for item in [*candidates, *excluded]
                     for field in ("wait_finding", "label_finding") if item.get(field)],
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
            "unavailable_relationships": unavailable_relationships,
            "missing_tracking_milestones": missing,
        },
    }


def milestone_summary(
    milestone_titles: list[str], *, completed: list[str], candidates: list[dict[str, Any]],
    graph_waits: list[dict[str, Any]], track_next_actions: dict[str, dict[str, Any]],
    underway: list[dict[str, Any]] | None = None, needs_status: list[dict[str, Any]] | None = None,
    graph_complete: bool = True,
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
        def linked(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [{"repo": row["repo"], "number": row["number"], "url": row["url"],
                     **({"wait_review_required": True} if row.get("wait_review_required") else {})}
                    for row in rows if (row.get("via") and (row.get("milestone") or {}).get("title") == title)
                    or title in (row.get("overall_milestone_context") or {}).get("titles", [])]
        owned = linked(underway or [])
        missing = linked(needs_status or [])
        waits: list[dict[str, Any]] = []
        reviews: list[dict[str, Any]] = []
        for row in graph_waits:
            record = {"repo": row["repo"], "number": row["number"], "url": row["url"],
                      "waiting_for": row.get("waiting_for")}
            target = reviews if (row.get("source") == "legacy_wait_review" or row.get("wait_review_required")) and row.get("plan_status") != "waiting" else waits
            if ((row.get("milestone") or {}).get("title") == title or title in (row.get("overall_milestone_context") or {}).get("titles", [])) and record not in target:
                target.append(record)
        for row in [*owned, *missing]:
            if row.get("wait_review_required") and not any((entry["repo"], entry["number"]) == (row["repo"], row["number"]) for entry in reviews):
                reviews.append({**row, "waiting_for": "Legacy wait review required"})
        track = track_next_actions.get(title) or {}
        summary.append({
            "milestone": title,
            "track": track.get("track"),
            "state": "work_listed" if work else "needs_status" if missing else "wait_review_required" if reviews else "underway" if owned else "waiting" if waits else "no_linked_work" if graph_complete else "unknown",
            "work": work, "underway": owned, "verification_only": [], "scope_review": [], "needs_status": missing, "wait_reviews": reviews,
            "waits": waits,
            # Named by the Track's Next action but not reached over native edges.
            "next_action_outside_graph": track.get("references", []),
        })
    return summary
