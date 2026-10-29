# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import github_plan_claim
import github_plan_wait
import ready_handoff
from github_plan_status.waits import current_source

DISCUSSION_EXCERPT_LIMIT = 1000


def read_acceptance(repo: str, number: int, head: str) -> bool:
    helper = Path(__file__).resolve().parents[2] / "launchplane/scripts/launchplane-owner-review.py"
    result = subprocess.run(
        [sys.executable, str(helper), "--repo", repo, "--pr", str(number)],
        capture_output=True, text=True, timeout=20,
    )
    payload = json.loads(result.stdout)
    if result.returncode or not isinstance(payload, dict) or payload.get("ok") is not True:
        raise ValueError("client_acceptance_unavailable")
    decision = payload.get("decision")
    return bool(isinstance(decision, dict) and decision.get("decision") == "accepted" and decision.get("head_sha") == head)


def project(repo: str, pull: dict[str, Any], comments: list[dict[str, Any]], *, trusted: set[str], complete: bool,
            acceptance_reader: Callable[[str, int, str], bool] | None = None) -> dict[str, Any]:
    head = (pull.get("head") or {}).get("sha")
    row = {
        "repo": repo,
        "number": pull["number"],
        "url": pull["html_url"],
        "title": pull["title"],
        "record_type": "pull_request",
        "head_sha": head,
        "created_at": pull.get("created_at"),
        "ownership": "fresh_claim_required",
        "review_required": True,
        "next_action": "Reconcile PR discussion, source custody, Client authority and current-head readiness before claiming follow-through.",
        "discussion_complete": complete,
        "discussion": [
            {
                "id": comment.get("id"),
                "body": (comment.get("body") or "")[:DISCUSSION_EXCERPT_LIMIT],
                "body_truncated": len(comment.get("body") or "") > DISCUSSION_EXCERPT_LIMIT,
                "author": (comment.get("user") or {}).get("login"),
                "url": comment.get("html_url")
                or f"https://github.com/{repo}/pull/{pull['number']}#issuecomment-{comment.get('id')}",
                "created_at": comment.get("created_at"),
            }
            for comment in comments
        ],
    }
    if github_plan_claim.is_train_batch(pull):
        return {**row, "state": "underway", "reason": "train_batch"}
    if pull.get("draft"):
        return {**row, "state": "waiting", "reason": "draft_park"}
    if not complete or not head:
        return {**row, "state": "unknown", "reason": "incomplete_pr_evidence"}
    trusted_comments = [comment for comment in comments if str((comment.get("user") or {}).get("login") or "").casefold() in trusted]
    try:
        conflicts, _ = github_plan_claim.discussion_evidence(
            "", trusted_comments, {"worker": "", "session": "", "branch": "", "claimed_at": ""}
        )
        if conflicts:
            return {**row, "state": "underway", "reason": "existing_claim", "ownership_evidence": conflicts}
        wait = github_plan_wait.current(pull, comments, trusted)
        ready = ready_handoff.latest_ready(pull, comments, trusted)
        source = current_source(pull, comments, trusted)
        handoffs = (
            comments[comments.index(source) + 1 :]
            if wait["state"] == "clear" and source is not None and source in comments
            else comments
        )
        client_wait = ready_handoff.latest_ready(pull, handoffs, trusted, include_client_wait=True) and not ready
    except (KeyError, ValueError, TypeError) as exc:
        return {**row, "state": "unknown", "reason": "unreadable_pr_evidence", "detail": str(exc)}
    row["wait"] = wait
    if wait["state"] == "waiting":
        return {**row, "state": "waiting", "reason": "recorded_wait"}
    if ready:
        return {**row, "state": "underway", "reason": "current_head_ready"}
    if client_wait:
        try:
            accepted = (acceptance_reader or read_acceptance)(repo, pull["number"], head)
        except (ValueError, OSError, subprocess.SubprocessError):
            return {**row, "state": "unknown", "reason": "client_acceptance_unavailable"}
        if accepted and (wait["state"] == "clear" or source is None):
            return {**row, "state": "review", "reason": "pr_followthrough_needs_fresh_readiness", "next_actor": "agent",
                    "next_action": "Post checked Ready for the accepted current head after fresh readiness and custody checks."}
        return {**row, "state": "waiting", "reason": "current_head_client_wait"}
    if wait["state"] != "clear":
        return {**row, "state": "scope_review", "reason": "wait_review_required"}
    return {**row, "state": "review", "reason": "pr_followthrough_needs_fresh_readiness"}
