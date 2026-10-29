# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Project current work and observed ownership into a read-only start queue.

An available row means no known conflict in the reported reads; claim remains
mandatory and independently checks holds, artifacts and current sessions.
"""
from __future__ import annotations

import fnmatch
import re
from datetime import datetime
from collections import Counter
from collections.abc import Callable
from typing import Any

import github_plan_claim
import github_plan_wait
import github_direction_next
import github_next_pr
from github_plan_status import next_actor
from github_plan_status.references import linked_issues


def current_status(discussion: dict[str, Any], trusted: set[str], *, body_recorded_at: str | None = None) -> str:
    def timestamp(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
            return parsed if parsed is not None and parsed.tzinfo is not None else None
        except ValueError:
            return None
    status = github_direction_next.section_map(discussion.get("body") or "").get("Current Status")
    has_body_status = status is not None
    body_time = timestamp(body_recorded_at or discussion.get("updated_at"))
    for comment in discussion.get("comments", []):
        if str(comment.get("author") or "").casefold() not in trusted:
            continue
        found = github_direction_next.section_map(comment.get("body") or "").get("Current Status")
        comment_time = timestamp(comment.get("created_at"))
        # Older status comments are historical once the issue's current body
        # has newer evidence. Unmanaged records without status use comments.
        if found is not None and (not has_body_status or (comment_time is not None and (body_time is None or comment_time >= body_time))):
            status = found
            body_time = comment_time
            has_body_status = True
    return re.sub(r"<!--.*?-->", "", status or "", flags=re.S).strip()


def status_field(status: str, field: str) -> str:
    match = re.search(rf"(?mi)^\s*(?:[-*]\s+)?(?:[*]{{2}})?{re.escape(field)}(?:[*]{{2}})?:(?:[*]{{2}})?[ \t]*(.*)$", status)
    return match[1].strip() if match else ""


def ownership_facts(item: dict[str, Any], ownership: dict[str, Any], *, trusted: set[str],
                    resume_from: int | None = None, resume_source: dict[str, Any] | None = None,
                    expired_sessions: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    discussion = item.get("discussion") or {}
    status = current_status(discussion, trusted, body_recorded_at=discussion.get("body_recorded_at"))
    comments = [{**comment, "user": {"login": comment.get("author") or ""}} for comment in discussion.get("comments", [])]
    row = {"complete": ownership.get("complete") is True, "coverage": ownership.get("coverage", {})}
    complete = discussion.get("complete")
    if not isinstance(complete, bool) or not complete:
        return {**row, "error": "incomplete_discussion"}
    if not discussion.get("author") or any(not comment.get("author") for comment in discussion.get("comments", [])):
        return {**row, "error": "discussion_authorship_unknown"}
    inventory = ownership.get("inventory") or {"local_branches": [], "remote_branches": [], "worktrees": [], "sessions": []}
    retained = None
    try:
        ended = github_plan_claim.ended_ownership(comments, inventory, expired_sessions)
        conflicts, _ = github_plan_claim.discussion_evidence(status, comments,
            {"worker": "", "session": "", "branch": "", "claimed_at": ""},
            ended_claims={source["claim_id"] for source in ended})
        if item.get("record_type") == "pull_request":
            pull = next((pull for pull in ownership.get("pulls", []) if pull["number"] == item["number"]), None)
            if not isinstance(pull, dict):
                return {**row, "error": "pr_ownership_inventory_incomplete"}
            source_keys = {((source.get("repo") or item["repo"]).casefold(), source["number"])
                           for source in ownership.get("records", []) if not source.get("pull_request")}
            missing_sources = [(repo, number) for repo, number in linked_issues(item["repo"], pull)
                               if (repo.casefold(), number) not in source_keys]
            if missing_sources:
                return {**row, "error": "source_custody_unread", "detail": ", ".join(f"{repo}#{number}" for repo, number in missing_sources)}
            for source in ownership.get("records", []):
                if source.get("pull_request") or not github_plan_claim.pr_issue_reference(pull, source["number"],
                        repo=source.get("repo") or item["repo"], local_references=(source.get("repo") or item["repo"]).casefold() == item["repo"].casefold()):
                    continue
                if source.get("_graph_comments_complete") is not True:
                    return {**row, "error": "source_claim_discussion_incomplete"}
                source_comments = source["_graph_comments"]
                ended_sources = github_plan_claim.ended_ownership(source_comments, inventory, expired_sessions)
                source_status = current_status(github_direction_next.discussion_snapshot(source, source_comments, complete=True), trusted)
                source_conflicts, _ = github_plan_claim.discussion_evidence(source_status, source_comments,
                    {"worker": "", "session": "", "branch": "", "claimed_at": ""},
                    ended_claims={source["claim_id"] for source in ended_sources})
                conflicts.extend(source_conflicts)
        wait = github_plan_wait.current(
            {"body": discussion.get("body") or "", "user": {"login": discussion.get("author") or ""}}, comments, trusted)
        if resume_from:
            source_item = resume_source or item
            source_comments = ([{**comment, "user": {"login": comment.get("author") or ""}}
                                for comment in source_item["discussion"]["comments"]] if resume_source else comments)
            source = github_plan_claim.released_source(source_comments, resume_from)
            retained = github_plan_claim.retained_handoff(
                source, github_plan_claim.handoff_candidates(source_comments, source), ownership.get("pulls") or [],
                issue_repo=source_item["repo"], issue_number=source_item["number"], target_repo=item["repo"])
            if retained and retained["open_conflicts"]:
                return {**row, "error": "retained_handoff_unavailable"}
    except (ValueError, KeyError, TypeError) as exc:
        return {**row, "error": "unreadable_ownership_or_wait", "detail": str(exc)}
    if item.get("record_type") == "pull_request":
        subject = next((pull for pull in ownership.get("pulls", []) if pull["number"] == item["number"]), {})
        if not (subject.get("head") or {}).get("ref") or any(
            (((subject.get(side) or {}).get("repo") or {}).get("full_name") or "").casefold() != item["repo"].casefold()
            for side in ("head", "base")):
            return {**row, "error": "pr_canonical_issue_required"}
    branches = github_plan_claim.recorded_claim_branches(status, comments)
    try:
        artifacts = github_plan_claim.artifact_evidence(inventory, ownership.get("pulls") or [], item["number"],
            {"worker": "", "session": "", "branch": ""}, own_record=False, repo=item["repo"], recorded_branches=branches,
            retained_branches=retained["permitted"] if retained else None, retained_repo=item["repo"] if retained else None,
            subject_pr=item["number"] if item.get("record_type") == "pull_request" else None,
            ended_branches=github_plan_claim.ended_artifact_branches(ended, comments, ownership.get("pulls") or []))
    except (ValueError, KeyError, TypeError) as exc:
        return {**row, "error": "unreadable_ownership_or_wait", "detail": str(exc)}
    return {**row, "conflicts": conflicts, "artifacts": artifacts, "wait": wait}


def released_actor(actor: str, discussion: dict[str, Any], trusted: set[str]) -> bool:
    from github_plan_release import effective_comments

    comments = effective_comments([{**comment, "user": {"login": comment.get("author") or ""}}
                                   for comment in discussion.get("comments", [])])
    relevant = [index for index, comment in enumerate(comments)
                if str((comment.get("user") or {}).get("login") or "").casefold() in trusted
                and any(record["session"] == actor.removeprefix("agent:")
                        for record in github_plan_claim.records(comment.get("body") or ""))]
    return bool(relevant and relevant[-1] in {index for index, _, _ in github_plan_claim.exact_released_sources(comments)})


def is_scoping_track(item: dict[str, Any]) -> bool:
    return github_direction_next.TRACK_SCOPING in (item.get("reasons") or [])


def classify(item: dict[str, Any], ownership: dict[str, Any], *, trusted: set[str], status_known: bool = True,
             acceptance_reader: Callable[[str, int, str], bool] | None = None) -> dict[str, Any]:
    discussion = item.get("discussion") or {}
    scoping = is_scoping_track(item)
    status = current_status(discussion, trusted, body_recorded_at=discussion.get("body_recorded_at")) if status_known and not scoping else ""
    row = {"next_action": "Scope the first work issue for this Track." if scoping else status_field(status, "Next action"),
           "recorded_state": status_field(status, "State"),
           "claim_required": True, "ownership_complete": False,
           "ownership_source": "current_reads", "ownership_coverage": ownership.get("coverage", {}),
           "observed_ownership_complete": ownership.get("complete") is True}
    try:
        actor = "agent" if scoping else next_actor.read(status)
    except ValueError:
        actor = None
    row["next_actor"] = actor
    pull = next((pull for pull in ownership.get("pulls", []) if pull["number"] == item["number"]), None) if item.get("record_type") == "pull_request" else None
    projected: dict[str, Any] = {}
    if isinstance(pull, dict):
        comments = [{**comment, "user": {"login": comment.get("author") or ""}} for comment in discussion.get("comments", [])]
        projected = github_next_pr.project(item["repo"], pull, comments, trusted=trusted, complete=discussion.get("complete") is True,
                                            acceptance_reader=acceptance_reader)
        if projected["reason"] in {"train_batch", "draft_park"} or (projected["state"] == "unknown" and projected["reason"] != "client_acceptance_unavailable"):
            return {**row, "state": "review" if projected["state"] == "unknown" else projected["state"], "reason": projected["reason"]}
    facts = ownership_facts(item, ownership, trusted=trusted)
    if facts.get("error"):
        return {**row, "state": "review", "observed_ownership_complete": False, "reason": facts["error"], **({"detail": facts["detail"]} if "detail" in facts else {})}
    conflicts, artifacts, wait = facts["conflicts"], facts["artifacts"], facts["wait"]
    row["wait_review_required"] = wait["state"] == "review"
    if wait["state"] == "waiting":
        return {**row, "state": "waiting", "reason": "recorded_wait", "wait": wait}
    if actor and actor.startswith(("person:", "event:")):
        return {**row, "state": "waiting", "reason": "recorded_next_actor"}
    if projected.get("reason") in {"current_head_ready", "current_head_client_wait"}:
        return {**row, "state": projected["state"], "reason": projected["reason"]}
    if item.get("record_type") == "pull_request":
        row["ownership_evidence"] = artifacts
        artifacts = [artifact for artifact in artifacts if artifact["source"] not in {"local_branches", "worktree"}]
    if conflicts or artifacts:
        return {**row, "state": "underway", "reason": "existing_claim_or_artifact",
                "ownership_evidence": [*conflicts, *artifacts], "certainty": "current_or_stale"}
    if projected.get("reason") == "client_acceptance_unavailable":
        return {**row, "state": "review", "reason": projected["reason"]}
    if not status_known:
        return {**row, "state": "review", "reason": "incomplete_discussion"}
    if (actor == "agent" or not next_actor.fields(status)) and projected.get("next_actor") == "agent":
        actor = row["next_actor"] = "agent"
        row["next_action"] = row["next_action"] or projected["next_action"]
        row["wait_review_required"] = False
        wait = github_plan_wait.reviewed(evidence="Saved Client acceptance ends the current-head Client handoff wait.")
    if actor is None:
        return {**row, "state": "needs_status", "reason": "next_actor_missing_or_invalid"}
    if actor.startswith("agent:"):
        if released_actor(actor, discussion, trusted):
            return {**row, "state": "needs_status", "reason": "next_actor_session_released"}
        return {**row, "state": "underway", "reason": "recorded_next_actor"}
    if wait["state"] == "review":
        return {**row, "state": "review", "reason": "wait_review_required", "wait_review_required": True}
    if not ownership.get("complete"):
        return {**row, "state": "review", "reason": "ownership_reads_incomplete"}
    return {**row, "state": "available", "reason": "agent_work_without_observed_conflict",
            "wait_review_required": False}


def own_project(repo: str, patterns: list[str], owner: str) -> bool:
    """Use the direction audit's parsed declaration, never a copied repo list."""
    if repo.split("/")[0].casefold() != owner.casefold():
        return False
    return any(fnmatch.fnmatchcase(repo.split("/")[1].casefold(), pattern) for pattern in patterns)


def coverage(inventory: list[dict[str, Any]], evaluated: list[dict[str, Any]], groups: dict[str, Any], *, active_label: str = "plan:active") -> dict[str, Any]:
    def key(item: dict[str, Any]) -> tuple[str, int]:
        return item["repo"].casefold(), item["number"]
    unique = {key(item): item for item in inventory}
    checked = {key(item) for item in evaluated}
    active = {ident for ident, item in unique.items() if active_label in [label if isinstance(label, str) else label.get("name") for label in item.get("labels", [])]}
    reasons = Counter(item.get("exclusion") or (item.get("queue") or {}).get("reason") or "review_required"
                      for item in groups.get("excluded", []))
    states = {name: len(groups.get(name, [])) for name in ("candidates", "available_candidates", "underway", "verification_only", "scope_review", "needs_status", "waiting", "excluded")}
    return {"inventoried_issue_count": len(unique), "active_inventory_count": len(active),
            "evaluated_issue_count": len(checked), "active_evaluated_count": len(active & checked),
            "active_unevaluated_count": len(active - checked), "excluded_by_reason": dict(sorted(reasons.items())), "queue_counts": states}
