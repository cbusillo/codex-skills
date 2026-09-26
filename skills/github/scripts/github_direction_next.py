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
        reason = re.split(r"(?<=[.!?])\s+", match.group(1).strip(), maxsplit=1)[0]
        references: dict[tuple[str, int], str] = {}
        for ref in re.finditer(
            r"https://github\.com/([^/\s)]+/[^/\s)]+)/(issues|pull)/(\d+)"
            r"|(?<![\w/])(?:([\w.-]+/[\w.-]+))?#(\d+)\b",
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


def rank_direction_work(
    roots: list[dict[str, Any]],
    *,
    milestone_titles: list[str],
    read_node: Callable[[str, int], dict[str, Any]],
    scan_limit: int,
    rank_candidates: Callable[..., None],
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
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    tracks: list[dict[str, Any]] = []
    for root in roots:
        milestone = root.get("milestone") or {}
        title = milestone.get("title")
        if title in order and milestone.get("state") == "open" and str(root.get("title", "")).startswith("Track:"):
            tracks.append(root)
        else:
            excluded.append({**root, "exclusion": "outside_direction_tracks"})
    tracks.sort(key=lambda root: (order[root["milestone"]["title"]], root["number"]))
    present = {root["milestone"]["title"] for root in tracks}
    missing = [title for title in milestone_titles if title not in present]
    seen: set[tuple[str, int]] = set()
    degraded = 0
    truncated = False
    # A stack keeps even a deep dependency chain within the explicit scan bound.
    pending = [(root, root["milestone"], [], True) for root in reversed(tracks)]
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
        if reason == "waiting" and not reports and (not tracking or not (blockers or children)):
            waiting.append({
                **step,
                "milestone": milestone,
                "waiting_for": node.get("status_text") or "Waiting party or condition not recorded",
                "reported_by": item["url"],
                "reported_at": item.get("updated_at"),
            })
        if reason in {"waiting", "later_focus", "label_blocked_without_native_edge"} and not tracking:
            excluded.append(item)
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
    candidates.sort(key=lambda item: (item["repo"].casefold(), item["number"]))
    rank_candidates(candidates, direction_milestones=milestone_titles)
    return {
        "candidates": candidates,
        "candidate_count": len(candidates),
        "excluded": excluded,
        "waiting": waiting,
        "evaluated": len(seen),
        "truncated": truncated,
        "dependency_context": {
            "complete": not (degraded or truncated or missing),
            "degraded_count": degraded,
            "missing_tracking_milestones": missing,
        },
    }
