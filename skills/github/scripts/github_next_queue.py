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
from collections import Counter
from collections.abc import Callable
from typing import Any

import github_plan_claim
import github_plan_wait
import github_direction_next
import github_next_pr
from github_plan_status.discussion import current_status, status_field
from github_plan_status import next_actor, waits
from github_plan_status.references import linked_issues


def compact_output(value: Any) -> Any:
    if isinstance(value, list):
        return [compact_output(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == "discussion" and isinstance(item, dict):
            result[key] = {
                **compact_output({name: detail for name, detail in item.items() if name not in {"body", "comments"}}),
                "comment_count": len(item.get("comments", [])),
            }
        else:
            result[key] = compact_output(item)
    return result


def ownership_facts(item: dict[str, Any], ownership: dict[str, Any], *, trusted: set[str],
                    resume_from: int | None = None, resume_source: dict[str, Any] | None = None,
                    expired_sessions: set[str] | frozenset[str] | None = None,
                    worker: str = "", session: str = "") -> dict[str, Any]:
    discussion = item.get("discussion") or {}
    status = current_status(discussion, trusted, body_recorded_at=discussion.get("body_recorded_at"), preserve_markers=True)
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
        if expired_sessions is None:
            expired_sessions = github_plan_claim.expired_lease_sessions(comments)
        ended = github_plan_claim.ended_ownership(comments, inventory, expired_sessions)
        conflicts, _ = github_plan_claim.discussion_evidence(status, comments,
            {"worker": "", "session": "", "branch": "", "claimed_at": ""},
            ended_claims={source["claim_id"] for source in ended})
        recipients = []
        if recipient := github_plan_claim.recipient_evidence(
                status, comments, repo=item["repo"], number=item["number"], worker=worker, session=session,
                ended_claims={source["claim_id"] for source in ended}):
            recipients.append(recipient)
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
                ended_sources = github_plan_claim.ended_ownership(source_comments, inventory,
                    expired_sessions | github_plan_claim.expired_lease_sessions(source_comments))
                source_status = current_status(github_direction_next.discussion_snapshot(source, source_comments, complete=True), trusted, preserve_markers=True)
                source_conflicts, _ = github_plan_claim.discussion_evidence(source_status, source_comments,
                    {"worker": "", "session": "", "branch": "", "claimed_at": ""},
                    ended_claims={source["claim_id"] for source in ended_sources})
                conflicts.extend(source_conflicts)
                source_recipient = github_plan_claim.recipient_evidence(
                        source_status, source_comments, repo=source.get("repo") or item["repo"],
                        number=source["number"], worker=worker, session=session,
                        ended_claims={entry["claim_id"] for entry in ended_sources})
                if source_recipient:
                    recipients.append(source_recipient)
        wait = github_plan_wait.current(
            {"body": discussion.get("body") or "", "user": {"login": discussion.get("author") or ""}}, comments, trusted)
        original_wait = wait
        wait = waits.project_dates(wait)
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
    return {**row, "conflicts": conflicts, "artifacts": artifacts, "wait": wait, "ended_ownership": ended, "recipients": recipients,
            "event_completed": wait != original_wait and wait["state"] == "clear"}


def released_actor(actor: str, discussion: dict[str, Any], trusted: set[str]) -> bool:
    from github_plan_release import effective_comments

    from github_plan_status.discussion import released_actor as shared_released_actor

    comments = effective_comments([{**comment, "user": {"login": comment.get("author") or ""}}
                                   for comment in discussion.get("comments", [])])
    return shared_released_actor(actor, effective_comments=comments, trusted=trusted)


def is_scoping_track(item: dict[str, Any]) -> bool:
    return github_direction_next.TRACK_SCOPING in (item.get("reasons") or [])


def classify(item: dict[str, Any], ownership: dict[str, Any], *, trusted: set[str], status_known: bool = True,
             acceptance_reader: Callable[[str, int, str], bool] | None = None) -> dict[str, Any]:
    discussion = item.get("discussion") or {}
    scoping = is_scoping_track(item)
    status = current_status(discussion, trusted, body_recorded_at=discussion.get("body_recorded_at")) if status_known and not scoping else ""
    row = {"next_action": "Assess this Track's proof; close it if met, or scope the remaining work." if scoping else status_field(status, "Next action"),
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
    if facts["event_completed"] and actor and actor.startswith("event:"):
        actor = row["next_actor"] = "agent"
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
    if facts["recipients"]:
        return {**row, "state": "needs_status", "reason": "named_recipient_requires_reconciliation",
                "ownership_evidence": facts["recipients"], "recorded_next_action": row["next_action"],
                "next_action": facts["recipients"][0]["next_action"]}
    if projected.get("reason") == "client_acceptance_unavailable":
        return {**row, "state": "review", "reason": projected["reason"]}
    if not status_known:
        return {**row, "state": "review", "reason": "incomplete_discussion"}
    if ((actor or "").startswith("agent") or not next_actor.fields(status)) and projected.get("next_actor") == "agent":
        actor = row["next_actor"] = actor or "agent"
        row["next_action"] = row["next_action"] or projected["next_action"]
        row["wait_review_required"] = False
        wait = github_plan_wait.reviewed(evidence="Saved Client acceptance ends the current-head Client handoff wait.")
    if actor is None:
        return {**row, "state": "needs_status", "reason": "next_actor_missing_or_invalid"}
    if actor.startswith("agent:"):
        if (released_actor(actor, discussion, trusted) or any(
                actor.removeprefix("agent:").casefold() in {record["worker"].casefold(), record["session"].casefold()}
                for record in facts["ended_ownership"])):
            return {**row, "state": "needs_status", "reason": "next_actor_session_released"}
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
