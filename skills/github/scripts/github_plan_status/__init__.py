# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Planning status projected from complete native relationships and reviewed waits."""
from __future__ import annotations

import re
from typing import Any

from . import waits as wait_records
from .waits import validate as validate_wait

DEFAULT_STATUS_LABELS = {key: f"plan:{key}" for key in
                         ("active", "blocked", "waiting", "stale", "done")}
DEFAULT_STATUS_LABELS["plan"] = "plan"


def derive(issue: dict[str, Any], relationships: dict[str, Any], wait: dict[str, Any], *,
           label_names: dict[str, str] = DEFAULT_STATUS_LABELS) -> dict[str, Any]:
    validate_wait(wait)
    state = str(issue.get("state", "")).casefold()
    if state not in {"open", "closed"} or any(
            str(item.get("state", "")).casefold() not in {"open", "closed"}
            for item in relationships["blocked_by"]):
        raise ValueError("Native issue state is incomplete; planning status needs review")
    blockers = [item for item in relationships["blocked_by"] if item["state"].casefold() == "open"]
    waits = wait["waits"]
    names = {str(item.get("name", "") if isinstance(item, dict) else item).casefold()
             for item in issue.get("labels", [])}
    held = [key for key in ("stale", "done") if label_names.get(key, "").casefold() in names]
    unassigned = (label_names.get("plan", "").casefold() in names
                  and not any(label_names.get(key, "").casefold() in names
                              for key in ("active", "blocked", "waiting", "stale", "done")))
    scope_hold = state == "open" and bool(held or unassigned)
    wait_review = wait["state"] == "review"
    scope_status = (held[0] if held else "unstatused") if scope_hold else None
    if state == "closed":
        status, labels = "done", ["done", *(["waiting"] if waits else [])]
    elif wait_review:
        status, labels = "review", ["blocked"] if blockers else []
    elif scope_hold:
        status, labels = "hold", held + (["blocked"] if blockers else []) + (["waiting"] if waits else [])
    else:
        status = "blocked" if blockers else "waiting" if waits else "active"
        labels = [status]
        if blockers and waits:
            labels.append("waiting")
    return {"status": status, "blocked_by": blockers, "waits": waits, "labels": labels,
            "structured_wait": wait, "scope_hold": scope_hold, "scope_status": scope_status,
            "wait_review_required": wait_review, "labels_writable": not (scope_hold or wait_review)}


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
    waits = "; ".join(item["waiting_for"] for item in derived["waits"]) or (
        "Review the structured wait record." if derived.get("wait_review_required", derived["status"] == "review") else "None.")
    return f"State: {derived['status']}.\nBlocked by: {blockers}\nWaiting for: {waits}\n" + prose


def label_changes(recorded: list[str], names: dict[str, str], derived: dict[str, Any]) -> dict[str, list[str]]:
    """One status-label diff for catalog and native train consumers."""
    if not derived.get("labels_writable", False):
        return {"add": [], "remove": [], "result": list(recorded)}
    statuses = {names[key].casefold() for key in ("active", "blocked", "waiting", "stale", "done") if key in names}
    wanted = {names[key] for key in derived["labels"] if key in names}
    remove = {name for name in recorded if name.casefold() in statuses
              and name.casefold() not in {value.casefold() for value in wanted}}
    add = {name for name in wanted if name.casefold() not in {value.casefold() for value in recorded}}
    return {"add": sorted(add), "remove": sorted(remove),
            "result": sorted((set(recorded) - remove) | add)}


def project_labels(recorded: list[str], names: dict[str, str], derived: dict[str, Any]) -> list[str]:
    return label_changes(recorded, names, derived)["result"]


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


def is_planning_issue(issue: dict[str, Any], names: dict[str, str]) -> bool:
    observed = {str(item.get("name", "") if isinstance(item, dict) else item).casefold()
                for item in issue.get("labels", [])}
    return "pull_request" not in issue and bool(observed & {
        names[key].casefold() for key in DEFAULT_STATUS_LABELS if key in names})


def receipt_summary(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    verified = {"reconciled", "unchanged", "would_reconcile", "not_planning_issue"}
    return {"ok": all(item.get("informational") or item["status"] in verified for item in outcomes),
            "coverage": "complete" if all(item.get("verified") or item["status"] in verified for item in outcomes) else "partial",
            "review_required": any(item["status"].startswith(("review_", "not_attempted", "reconciliation_")) for item in outcomes)}
