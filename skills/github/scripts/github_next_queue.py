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
from typing import Any

import github_plan_claim
import github_plan_wait
import github_direction_next


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


def delivery_references(item: dict[str, Any], trusted: set[str], *, body_recorded_at: str | None = None) -> list[tuple[str, int]]:
    """Read only the selected current status; historical slices are not blockers."""
    status = current_status(item.get("discussion") or {}, trusted, body_recorded_at=body_recorded_at)
    refs = [(match[1], int(match[2])) for match in re.finditer(
        r"https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9]\d*)(?!\d)", status)]
    shorthand = re.sub(r"\[[^]\n]*]\(https://github\.com/[\w.-]+/[\w.-]+/pull/[1-9]\d*[^)]*\)", "", status)
    refs.extend((item["repo"], int(match[1])) for match in re.finditer(
        r"(?i)\bPR\s+#([1-9]\d*)\b", shorthand))
    return list(dict.fromkeys(refs))


def remaining_work(status: str) -> dict[str, Any]:
    """Classify only the recorded current step, never the original title or goal.

    Ambiguous mixed source/verification remainders stay review work. A merged
    prerequisite alone never proves delivery or completion of the parent.
    """
    state = status_field(status, "State")
    action = status_field(status, "Next action")
    result = {"next_action": action, "recorded_state": state}
    if re.match(r"(?:[\w.-]+/[\w.-]+)?#[1-9]\d*\b|https://github[.]com/[\w.-]+/[\w.-]+/issues/[1-9]\d*\b", action):
        return {**result, "state": "scope_review", "reason": "current_action_belongs_to_linked_record"}
    source_action = re.split(r"\b(?:do not|don't|never)\b", action, maxsplit=1, flags=re.I)[0]
    # A next independent source slice remains work after an earlier slice lands.
    def next_source_slice(text: str) -> bool:
        return bool(re.search(
            r"\bselects?\s+(?:the\s+)?next\s+(?:(?:independent|worthwhile)\s+)?(?:duplicate-code\s+cluster|(?:test-audit|source|implementation|refactor)\s+slice)\b",
            text, re.I))
    source_action = re.sub(r"\b(?:signed|matching|companion|release) (?:\w+ ){0,2}build\b", "artifact", source_action, flags=re.I)
    # A prerequisite's delivery says nothing about this issue's source. Keep
    # completed action clauses out of the current-work verb scan as well.
    def clauses(text: str) -> list[str]:
        return re.split(r"[;\n]|[.]\s+", text)
    delivery_state = ";".join(re.split(r"\bprerequisites?\b", clause, maxsplit=1, flags=re.I)[0]
                              for clause in clauses(state))
    delivered = bool(re.search(r"\b(?:landed|merged|delivered|source landing confirmed|source engineering is complete|completed .*slices)\b", delivery_state, re.I))
    if re.search(r"\b(?:not|never|yet to be|needs to be|must be|will be|to be|awaiting|pending)\s+(?:\w+\s+){0,2}(?:merged|landed|delivered)\b", state, re.I):
        delivered = False
    # Work explicitly routed to a separate canonical issue is no fresh source
    # assignment on this record. It remains open, with its current action.
    if delivered and re.search(r"\b(?:canonical|separately assigned)\b", action, re.I):
        return {**result, "state": "scope_review", "reason": "delivered_slice_has_separate_remaining_scope"}
    source_verbs = r"\b(?:implement|integrate|repair|fix|build|remove|migrate|add|develop|refactor|patch|diagnose|investigate|resolve|correct|update|extend|wire|replace|port|tune|restore|complete)(?:s|es|ing)?\b"
    qualification_words = r"\b(?:qualif\w*|verif\w*|observ\w*|confirm\w*|read(?:s|ing|back)?|proof|evidence|acceptance|test\w*|compares?)\b"
    def current_clause(clause: str) -> str:
        if re.match(r"\s*(?:no|not|never)\b", clause, re.I):
            return ""
        completed = re.search(r"\b(?:is|are|was|were|has been|have been)\s+(?:already\s+)?(?:complete[dn]?|merged|landed|delivered)\b", clause, re.I)
        if completed is None:
            return clause
        # A source action before a completion predicate can describe its goal
        # or a prerequisite condition, rather than finished work.
        prefix = clause[:completed.start()]
        if source_step(prefix) and any(prefix[match.end():].strip() for match in re.finditer(source_verbs, prefix, re.I)):
            return clause
        remainder = clause[completed.end():]
        following = re.match(r"\s*(?:,\s*(?:(?:and|but|so)\s+)?|(?:and|but)\s+)(.+)", remainder, re.I)
        return following[1] if following else ""
    def source_step(text: str) -> bool:
        for match in re.finditer(source_verbs, text, re.I):
            if re.fullmatch(r"(?:diagnose|investigate)(?:s|es|ing)?", match[0], re.I) and re.search(
                    r"\b(?:retained|historical)\b.*\b(?:evidence|logs?|trace\w*)\b", re.split(r";|[.]\s+", text[match.end():], maxsplit=1)[0], re.I):
                continue
            if re.fullmatch(r"complete(?:s|ing)?", match[0], re.I):
                completion = re.split(r"[.;]", text[match.end():], maxsplit=1)[0]
                if (re.search(qualification_words, completion, re.I) or not re.search(
                        r"\b(?:source|implementation|migration|contracts|storage|refactor)\b", completion, re.I)):
                    continue
            prefix = re.sub(r"\b(?:[\w-]+/)*(?:qualification|verification|observation|test)(?:/[\w-]+)*\s+(?:agent|worker|lead)\b",
                            "agent", text[:match.start()], flags=re.I)
            if re.search(qualification_words, prefix, re.I) and not re.search(r"(?:\b(?:and|or)\s+(?:\w+ly\s+)?|[.;]\s*)$", prefix, re.I):
                continue
            if match.start() and text[match.start() - 1] in "/-":
                continue
            before = re.findall(r"\w+", text[:match.start()].casefold())
            if before and before[-1] in {"the", "a", "an", "candidate", "contract", "source", "record", "bounded", "missing", "prerequisite", "their", "its", "old", "new", "existing", "current", "installed", "version", "genuine", "successful", "failed", "delivered", "deployed"}:
                continue
            if re.match(r"(?:correct|fix|repair)\s+only\s+(?:a\s+)?reproduced\b", text[match.start():], re.I):
                continue
            if re.match(r"adds?\s+(?:a\s+|new\s+)*(?:occurrence|receipt|evidence|note)\b", text[match.start():], re.I):
                continue
            return True
        return False
    source_action = "; ".join(filter(None, (current_clause(clause) for clause in clauses(source_action))))
    engineering = source_step(source_action) or next_source_slice(source_action)
    historical_investigation = re.search(r"\b(?:search\w*|investigat\w*|diagnos\w*)\b.*\b(?:retained|historical)\b.*\b(?:evidence|logs?|trace\w*)\b", source_action, re.I)
    if delivered and historical_investigation and not engineering:
        return {**result, "state": "scope_review", "reason": "delivered_source_has_followthrough"}
    qualification = re.search(qualification_words, source_action, re.I)
    # A conditional future source step after qualification is still a
    # qualification frontier; do not suggest its implementation prematurely.
    conditional = re.search(r"\b(?:once|after|then)\b", source_action, re.I)
    first_action = source_action[:conditional.start()] if conditional else source_action
    current_engineering = source_step(first_action) or next_source_slice(first_action)
    qualification_frontier = re.search(qualification_words, first_action, re.I) or re.search(r"\b(?:signed .*build|releases?|deploy\w*|publication|image|rollout|install\w*)\b", first_action, re.I)
    if qualification and qualification_frontier and not current_engineering and (delivered or re.search(r"\bqualification\b", state, re.I)):
        waiting_on = "deploy_or_release" if re.search(r"\b(?:deploy\w*|releases?|publication|signed .*build)\b", first_action, re.I) else "qualification"
        return {**result, "state": "verification", "reason": "recorded_remaining_step_is_qualification", "waiting_on": waiting_on}
    if (delivered and not engineering and action
            and not re.search(r"\b(?:source|implementation|code|repair|fix|bug|migration)\b", first_action, re.I)):
        return {**result, "state": "scope_review", "reason": "delivered_source_has_followthrough"}
    if delivered and not action:
        return {**result, "state": "scope_review", "reason": "delivered_scope_lacks_current_action"}
    if not action:
        return {**result, "state": "review", "reason": "current_action_not_recorded"}
    if engineering and re.match(r"(?:the\s+)?Supervisor\b", action, re.I):
        return {**result, "state": "scope_review", "reason": "current_action_assigned_to_supervisor"}
    return {**result, "state": "engineering" if engineering else "review",
            "reason": "current_source_action" if engineering else "current_scope_needs_review"}


def ownership_facts(item: dict[str, Any], ownership: dict[str, Any], *, trusted: set[str],
                    resume_from: int | None = None, resume_source: dict[str, Any] | None = None,
                    expired_sessions: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    discussion = item.get("discussion") or {}
    status = current_status(discussion, trusted, body_recorded_at=(item.get("source_delivery") or {}).get("body_recorded_at"))
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
    branches = github_plan_claim.recorded_claim_branches(status, comments)
    artifacts = github_plan_claim.artifact_evidence(inventory, ownership.get("pulls") or [], item["number"],
        {"worker": "", "session": "", "branch": ""}, own_record=False, repo=item["repo"], recorded_branches=branches,
        retained_branches=retained["permitted"] if retained else None, retained_repo=item["repo"] if retained else None,
        ended_branches=github_plan_claim.ended_artifact_branches(ended, comments, ownership.get("pulls") or []))
    return {**row, "conflicts": conflicts, "artifacts": artifacts, "wait": wait}


def classify(item: dict[str, Any], ownership: dict[str, Any], *, trusted: set[str]) -> dict[str, Any]:
    discussion = item.get("discussion") or {}
    delivery = item.get("source_delivery") or {}
    status = current_status(discussion, trusted, body_recorded_at=delivery.get("body_recorded_at"))
    work = remaining_work(status)
    row = {**work, "claim_required": True, "ownership_complete": False,
           "ownership_source": "current_reads", "ownership_coverage": ownership.get("coverage", {}),
           "observed_ownership_complete": ownership.get("complete") is True}
    if delivery:
        row["source_delivery"] = delivery
    if delivery.get("merged_prs") and work["state"] == "review":
        work = {**work, "state": "scope_review", "reason": "merged_source_needs_finish_line_review"}
        row.update(work)
    if delivery.get("merged_prs") and work["state"] == "engineering":
        recorded_times = []
        # A body edit can change an evidence link or marker while leaving a
        # stale action intact. Only a newly posted matching status establishes
        # an automatic post-merge remainder; a caller can review body-only work.
        values = [comment.get("created_at") for comment in discussion.get("comments", [])
                  if str(comment.get("author") or "").casefold() in trusted
                  and re.sub(r"<!--.*?-->", "", github_direction_next.section_map(comment.get("body") or "").get("Current Status", ""), flags=re.S).strip() == status]
        for value in values:
            try:
                recorded = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if recorded.tzinfo is not None:
                    recorded_times.append(recorded)
            except (AttributeError, TypeError, ValueError):
                continue
        try:
            merged = [datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00")) for pr in delivery["merged_prs"]]
            predates = not recorded_times or any(time.tzinfo is None or max(recorded_times) <= time for time in merged)
        except (KeyError, TypeError, ValueError):
            predates = True  # An activity timestamp cannot prove a fresh body action.
        distinct_remainder = re.search(r"\b(?:remaining|next|new|follow-up|regression|subsequent)\b", work.get("next_action") or "", re.I)
        if predates or not distinct_remainder:
            work = {**work, "state": "scope_review", "reason": "source_action_predates_merged_pr" if predates and recorded_times else "merged_source_needs_finish_line_review"}
            row.update(work)
    facts = ownership_facts(item, ownership, trusted=trusted)
    if facts.get("error"):
        return {**row, "state": "review", "reason": facts["error"], **({"detail": facts["detail"]} if "detail" in facts else {})}
    conflicts, artifacts, wait = facts["conflicts"], facts["artifacts"], facts["wait"]
    if conflicts or artifacts:
        return {**row, "state": "underway", "reason": "existing_claim_or_artifact",
                "ownership_evidence": [*conflicts, *artifacts], "certainty": "current_or_stale"}
    if wait["state"] == "waiting":
        return {**row, "state": "waiting", "reason": "recorded_wait", "wait": wait}
    if work["state"] in {"verification", "scope_review"}:
        return {**row, "wait_review_required": wait["state"] == "review"}
    if wait["state"] == "review":
        return {**row, "state": "review", "reason": "wait_review_required", "wait_review_required": True}
    if delivery and delivery.get("complete") is not True:
        return {**row, "state": "review", "reason": "source_delivery_reads_incomplete"}
    if work["state"] == "review":
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
