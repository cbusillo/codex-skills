# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Planning status projected from complete native relationships and reviewed waits."""
from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from . import waits as wait_records
from .waits import validate as validate_wait

DEFAULT_STATUS_LABELS: Mapping[str, str] = MappingProxyType({})


def derive(issue: dict[str, Any], relationships: dict[str, Any], wait: dict[str, Any], *,
           label_names: Mapping[str, str] | None = None) -> dict[str, Any]:
    del label_names
    validate_wait(wait)
    wait = wait_records.project_dates(wait)
    state = str(issue.get("state", "")).casefold()
    if state not in {"open", "closed"} or any(
            str(item.get("state", "")).casefold() not in {"open", "closed"}
            for item in relationships["blocked_by"]):
        raise ValueError("Native issue state is incomplete; planning status needs review")
    blockers = [item for item in relationships["blocked_by"] if item["state"].casefold() == "open"]
    pending_waits = wait["waits"]
    wait_review = wait["state"] == "review"
    status = ("done" if state == "closed" else "review" if wait_review else
              "blocked" if blockers else "waiting" if pending_waits else "active")
    return {"status": status, "blocked_by": blockers, "waits": pending_waits,
            "structured_wait": wait, "wait_review_required": wait_review}


def render_status(text: str, derived: dict[str, Any]) -> str:
    """A read projection; the original issue and human prose remain unchanged."""
    lines = []
    dropping = False
    live = {index for index, _ in wait_records.live_lines(text, prefix="")}
    field = r"(?:[-*][ \t]+)?(?:\*\*)?[A-Za-z][A-Za-z /_-]*?(?:\*\*)?:(?:\*\*)?"
    for index, line in enumerate(text.splitlines(keepends=True)):
        # Fences, quoted HTML and markers are authored context, not status.
        boundary = (index not in live or not line.strip()
                    or re.match(r"^[ \t]*(?:" + field + r"|#{1,6}[ \t]|<!--)", line))
        if index in live and re.match(
                r"^ {0,3}(?:[-*][ \t]+)?(?:\*\*)?(?:State|Blocked by|Waiting for|Parked until|Waiting since)"
                r"(?:\*\*)?:(?:\*\*)?", line, re.I):
            dropping = True
            continue
        if dropping and not boundary:
            continue
        dropping = False
        lines.append(line)
    prose = "".join(lines)
    blockers = ", ".join(f"{item['repo']}#{item['number']}" for item in derived["blocked_by"])
    if not blockers:
        blockers = "Unknown; native relationships need review." if derived.get("relationships_complete") is False else "None."
    waiting = "; ".join(item["waiting_for"] for item in derived["waits"]) or (
        "Review the structured wait record." if derived.get("wait_review_required", derived["status"] == "review") else "None.")
    return f"State: {derived['status']}.\nBlocked by: {blockers}\nWaiting for: {waiting}\n" + prose


def label_changes(recorded: list[str], names: Mapping[str, str], derived: dict[str, Any]) -> dict[str, list[str]]:
    del names, derived
    return {"add": [], "remove": [], "result": list(recorded)}


def is_planning_issue(issue: dict[str, Any], names: Mapping[str, str] | None = None) -> bool:
    return "pull_request" not in issue


def same_wait_projection(issue: dict[str, Any], first: dict[str, Any], second: dict[str, Any]) -> bool:
    return first == second or (issue.get("state") == "closed" and first["state"] == second["state"] == "review")


def projection_outcome(derived: dict[str, Any], label_status: str) -> dict[str, Any]:
    return {"status": "review_wait_record" if derived["wait_review_required"] else label_status,
            "verified": not derived["wait_review_required"], "projection_status": label_status}


def normalize_author(source: dict[str, Any]) -> dict[str, Any]:
    """REST and GraphQL expose the same author with different bot/ID shapes."""
    author = source.get("user") or source.get("author") or {}
    login = str(author.get("login") or "")
    bot = author.get("type") == "Bot" or author.get("__typename") == "Bot"
    result: dict[str, Any] = {"login": login + "[bot]" if bot and login and not login.endswith("[bot]") else login,
                              "type": "Bot" if bot else "User"}
    identity = author.get("databaseId", author.get("id"))
    if type(identity) is int:
        result["id"] = identity
    return result


def receipt_summary(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    verified = {"reconciled", "unchanged", "would_reconcile", "not_planning_issue"}
    return {"ok": all(item["ok"] if "ok" in item else (
                item.get("informational") or item.get("verified") or item["status"] in verified) for item in outcomes),
            "coverage": "complete" if all(item.get("verified") or item["status"] in verified for item in outcomes) else "partial",
            "review_required": any(item["status"].startswith(("review_", "not_attempted", "reconciliation_")) for item in outcomes)}


def classify_outcome(status: str, *, verified: bool = False, required: bool = False,
                     projection_status: str | None = None, completion_required: bool = False) -> dict[str, Any]:
    verified = verified or status in {"reconciled", "unchanged", "would_reconcile", "not_planning_issue"}
    informational = not required and status.startswith(("review_", "not_attempted"))
    projection_complete = projection_status in {"reconciled", "unchanged", "would_reconcile"}
    return {"status": status, "verified": verified, "informational": informational,
            "ok": informational or verified or (projection_complete and not completion_required)}


def failure_disposition(cause: str | None, write_outcome: str | None, *,
                        writes_started: bool, validation_error: bool = False) -> dict[str, Any]:
    """Adapters supply classified transport evidence; this function grants nothing."""
    stop = (write_outcome == "unknown"
            or cause in {"identity_refused", "unconfigured_identity", "invalid_credentials", "actor_mismatch"}
            or (cause is None and writes_started and not validation_error))
    denied = not stop and cause in {"permission_denied", "not_installed", "repository_access_denied"}
    return {"status": "review_posting_authority" if denied else "reconciliation_failed",
            "repository_denied": denied, "stop_writes": stop}
