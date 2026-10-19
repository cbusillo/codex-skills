# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Planning status projected from complete native relationships and reviewed waits."""
from __future__ import annotations

import re
from typing import Any


def validate_wait(record: Any) -> None:
    if (not isinstance(record, dict) or set(record) != {"version", "state", "evidence", "waits"}
            or type(record["version"]) is not int or record["version"] != 1
            or not isinstance(record["state"], str) or record["state"] not in {"clear", "waiting", "review"}
            or not isinstance(record["evidence"], str) or not isinstance(record["waits"], list)):
        raise ValueError("Malformed structured wait record; review with gh-plan.py wait")
    if record["state"] != "review" and not record["evidence"].strip():
        raise ValueError("Structured wait record lacks review evidence")
    if bool(record["waits"]) != (record["state"] == "waiting"):
        raise ValueError("Structured wait state and entries disagree")
    for wait in record["waits"]:
        if (not isinstance(wait, dict) or set(wait) != {"waiting_for", "ends_when"}
                or any(not isinstance(value, str) or not value.strip() for value in wait.values())):
            raise ValueError("A structured wait needs its actor/event and ending condition")


def derive(issue: dict[str, Any], relationships: dict[str, Any], wait: dict[str, Any]) -> dict[str, Any]:
    validate_wait(wait)
    state = str(issue.get("state", "")).casefold()
    if state not in {"open", "closed"} or any(
            str(item.get("state", "")).casefold() not in {"open", "closed"}
            for item in relationships["blocked_by"]):
        raise ValueError("Native issue state is incomplete; planning status needs review")
    blockers = [item for item in relationships["blocked_by"] if item["state"].casefold() == "open"]
    waits = wait["waits"]
    if state == "closed":
        status, labels = "done", ["done"]
    elif wait["state"] == "review":
        status, labels = "review", ["blocked"] if blockers else []
    else:
        status = "blocked" if blockers else "waiting" if waits else "active"
        labels = [status]
        if blockers and waits:
            labels.append("waiting")
    return {"status": status, "blocked_by": blockers, "waits": waits, "labels": labels,
            "structured_wait": wait}


def render_status(text: str, derived: dict[str, Any]) -> str:
    """A read projection; the original issue and human prose remain unchanged."""
    prose = re.sub(r"(?im)^[ \t]*(?:[-*][ \t]+)?(?:\*\*)?(?:State|Blocked by|Waiting for)"
                   r"(?:\*\*)?:(?:\*\*)?[^\n]*(?:\n|$)", "", text)
    blockers = ", ".join(f"{item['repo']}#{item['number']}" for item in derived["blocked_by"]) or "None."
    waits = "; ".join(item["waiting_for"] for item in derived["waits"]) or (
        "Review the structured wait record." if derived["status"] == "review" else "None.")
    return f"State: {derived['status']}.\nBlocked by: {blockers}\nWaiting for: {waits}\n" + prose


def project_labels(recorded: list[str], names: dict[str, str], derived: dict[str, Any]) -> list[str]:
    statuses = {names[key].casefold() for key in ("active", "blocked", "waiting", "stale", "done") if key in names}
    return sorted({name for name in recorded if name.casefold() not in statuses}
                  | {names[key] for key in derived["labels"] if key in names})
