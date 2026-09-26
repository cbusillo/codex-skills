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

import re
from collections.abc import Callable
from typing import Any

import github_milestone as github_milestone_core


def normalize_labels(items: list[Any] | None) -> list[str]:
    names: list[str] = []
    for item in items or []:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def issue_labels(issue: dict[str, Any]) -> list[str]:
    raw_labels = issue.get("labels")
    if not isinstance(raw_labels, list):
        return []
    names: list[str] = []
    for item in raw_labels:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def compact_list_issue(repo: str, issue: dict[str, Any]) -> dict[str, Any]:
    milestone = issue.get("milestone") or {}
    state = issue.get("state")
    return {
        "repo": repo,
        "number": issue.get("number"),
        "title": issue.get("title"),
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
    issue_label_names = {name.casefold() for name in issue_labels(issue)}
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
        "evaluated": len(seen),
        "truncated": truncated,
        "dependency_context": {
            "complete": not (degraded or truncated or missing),
            "degraded_count": degraded,
            "missing_tracking_milestones": missing,
        },
    }
