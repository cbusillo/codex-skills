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
from collections import Counter
from typing import Any

import github_plan_claim
import github_plan_wait
import github_direction_next


def current_status(discussion: dict[str, Any], trusted: set[str]) -> str:
    texts = [discussion.get("body") or ""]
    texts.extend(comment.get("body") or "" for comment in discussion.get("comments", [])
                 if str(comment.get("author") or "").casefold() in trusted)
    status = ""
    for text in texts:
        found = github_direction_next.section_map(text).get("Current Status")
        if found is not None:
            status = re.sub(r"<!--.*?-->", "", found, flags=re.S).strip()
    return status


def status_field(status: str, field: str) -> str:
    match = re.search(rf"(?mi)^\s*(?:[-*]\s+)?(?:[*]{{2}})?{re.escape(field)}(?:[*]{{2}})?:(?:[*]{{2}})?\s*(.*)$", status)
    return match[1].strip() if match else ""


def remaining_work(status: str) -> dict[str, Any]:
    """Classify only the recorded current step, never the original title or goal.

    Ambiguous mixed source/verification remainders stay review work. A merged
    prerequisite alone never proves delivery or completion of the parent.
    """
    state = status_field(status, "State")
    action = status_field(status, "Next action")
    result = {"next_action": action, "recorded_state": state}
    source_action = re.split(r"\b(?:do not|don't|never)\b", action, maxsplit=1, flags=re.I)[0]
    source_action = re.sub(r"\b(?:signed|matching|companion|release) (?:\w+ ){0,2}build\b", "artifact", source_action, flags=re.I)
    delivered = bool(re.search(r"\b(?:landed|merged|delivered|source landing confirmed|source engineering is complete|completed .*slices)\b", state, re.I))
    if re.search(r"\b(?:not|never|yet to be|needs to be|must be|will be|to be|awaiting|pending)\s+(?:\w+\s+){0,2}(?:merged|landed|delivered)\b", state, re.I):
        delivered = False
    # Work explicitly routed to a separate canonical issue is no fresh source
    # assignment on this record. It remains open, with its current action.
    if delivered and re.search(r"\b(?:canonical|separately assigned)\b", action, re.I):
        return {**result, "state": "scope_review", "reason": "delivered_slice_has_separate_remaining_scope"}
    source_verbs = r"\b(?:implement|repair|fix|build|remove|migrate|add|develop|refactor|patch|diagnose|investigate|resolve|correct|update|extend|wire|replace|port|tune|restore)(?:s|es|ing)?\b"
    def source_step(text: str) -> bool:
        for match in re.finditer(source_verbs, text, re.I):
            if match.start() and text[match.start() - 1] in "/-":
                continue
            before = re.findall(r"\w+", text[:match.start()].casefold())
            if before and before[-1] in {"the", "a", "an", "candidate", "contract", "source", "record", "bounded", "missing", "prerequisite", "their", "its", "old", "new", "existing", "current", "installed", "version", "genuine", "successful", "failed"}:
                continue
            if re.match(r"(?:correct|fix|repair)\s+only\s+(?:a\s+)?reproduced\b", text[match.start():], re.I):
                continue
            if re.match(r"adds?\s+(?:a\s+|new\s+)*(?:occurrence|receipt|evidence|note)\b", text[match.start():], re.I):
                continue
            return True
        return False
    engineering = source_step(source_action)
    qualification_words = r"\b(?:qualif\w*|verif\w*|observ\w*|confirm\w*|read\w*|proof|evidence|acceptance|test\w*|compares?)\b"
    qualification = re.search(qualification_words, action, re.I)
    # A conditional future source step after qualification is still a
    # qualification frontier; do not suggest its implementation prematurely.
    conditional = re.search(r"\b(?:once|after|then)\b", source_action, re.I)
    first_action = source_action[:conditional.start()] if conditional else source_action
    current_engineering = source_step(first_action)
    qualification_frontier = re.search(qualification_words, first_action, re.I) or re.search(r"\b(?:signed .*build|releases?|deploy\w*|publication|image|rollout|install\w*)\b", first_action, re.I)
    if qualification and qualification_frontier and not current_engineering and (delivered or re.search(r"\bqualification\b", state, re.I)):
        waiting_on = "deploy_or_release" if re.search(r"\b(?:deploy\w*|releases?|publication|signed .*build)\b", action, re.I) else "qualification"
        return {**result, "state": "verification", "reason": "recorded_remaining_step_is_qualification", "waiting_on": waiting_on}
    if delivered and not action:
        return {**result, "state": "scope_review", "reason": "delivered_scope_lacks_current_action"}
    if not action:
        return {**result, "state": "review", "reason": "current_action_not_recorded"}
    return {**result, "state": "engineering" if engineering else "review",
            "reason": "current_source_action" if engineering else "current_scope_needs_review"}


def classify(item: dict[str, Any], ownership: dict[str, Any], *, trusted: set[str]) -> dict[str, Any]:
    discussion = item.get("discussion") or {}
    status = current_status(discussion, trusted)
    work = remaining_work(status)
    comments = [{**comment, "user": {"login": comment.get("author") or ""}} for comment in discussion.get("comments", [])]
    row = {**work, "claim_required": True, "ownership_complete": False,
           "ownership_source": "current_reads", "ownership_coverage": ownership.get("coverage", {})}
    complete = discussion.get("complete")
    if not isinstance(complete, bool) or not complete:
        return {**row, "state": "review", "reason": "incomplete_discussion"}
    if not discussion.get("author") or any(not comment.get("author") for comment in discussion.get("comments", [])):
        return {**row, "state": "review", "reason": "discussion_authorship_unknown"}
    # Use the claim helper's exact authored-release semantics instead of a
    # second claim parser. No synthetic owner can renew a real claim.
    try:
        conflicts, _ = github_plan_claim.discussion_evidence(status, comments,
            {"worker": "", "session": "", "branch": "", "claimed_at": ""})
        wait = github_plan_wait.current(
            {"body": discussion.get("body") or "", "user": {"login": discussion.get("author") or ""}}, comments, trusted)
    except (ValueError, KeyError, TypeError) as exc:
        return {**row, "state": "review", "reason": "unreadable_ownership_or_wait", "detail": str(exc)}
    branches = github_plan_claim.recorded_claim_branches(status, comments)
    inventory = ownership.get("inventory") or {"local_branches": [], "remote_branches": [], "worktrees": [], "sessions": []}
    artifacts = github_plan_claim.artifact_evidence(inventory, ownership.get("pulls") or [], item["number"],
        {"worker": "", "session": "", "branch": ""}, own_record=False, repo=item["repo"], recorded_branches=branches)
    if conflicts or artifacts:
        return {**row, "state": "underway", "reason": "existing_claim_or_artifact",
                "ownership_evidence": [*conflicts, *artifacts], "certainty": "current_or_stale"}
    if wait["state"] == "waiting":
        return {**row, "state": "waiting", "reason": "recorded_wait", "wait": wait}
    pending = [record for record in github_direction_next.waiting_records(item, status)
               if not record["agent_next"] and not record["no_current_wait"]]
    if pending:
        return {**row, "state": "waiting", "reason": "current_status_wait_needs_review", "waits": pending}
    if work["state"] in {"verification", "scope_review", "review"}:
        return row
    if not ownership.get("complete"):
        return {**row, "state": "review", "reason": "ownership_reads_incomplete"}
    return {**row, "state": "available", "reason": "source_work_without_observed_conflict",
            "wait_review_required": wait["state"] == "review"}


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
    states = {name: len(groups.get(name, [])) for name in ("candidates", "available_candidates", "underway", "verification_only", "scope_review", "excluded")}
    return {"inventoried_issue_count": len(unique), "active_inventory_count": len(active),
            "evaluated_issue_count": len(checked), "active_evaluated_count": len(active & checked),
            "active_unevaluated_count": len(active - checked), "excluded_by_reason": dict(sorted(reasons.items())), "queue_counts": states}
