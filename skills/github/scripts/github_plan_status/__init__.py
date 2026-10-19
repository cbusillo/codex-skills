# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Planning status projected from complete native relationships and reviewed waits."""
from __future__ import annotations

import re
from typing import Any

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
    if state == "closed":
        status, labels = "done", ["done", *(["waiting"] if waits else [])]
    elif scope_hold:
        status, labels = "review", held + (["blocked"] if blockers else []) + (["waiting"] if waits else [])
    elif wait["state"] == "review":
        status, labels = "review", ["blocked"] if blockers else []
    else:
        status = "blocked" if blockers else "waiting" if waits else "active"
        labels = [status]
        if blockers and waits:
            labels.append("waiting")
    return {"status": status, "blocked_by": blockers, "waits": waits, "labels": labels,
            "structured_wait": wait, "scope_hold": scope_hold}


def render_status(text: str, derived: dict[str, Any]) -> str:
    """A read projection; the original issue and human prose remain unchanged."""
    lines = []
    dropping = False
    for line in text.splitlines(keepends=True):
        if re.match(r"^[ \t]*(?:[-*][ \t]+)?(?:\*\*)?(?:State|Blocked by|Waiting for)"
                    r"(?:\*\*)?:(?:\*\*)?", line, re.I):
            dropping = True
            continue
        if dropping and line.strip() and line[:1].isspace():
            continue
        dropping = False
        lines.append(line)
    prose = "".join(lines)
    blockers = ", ".join(f"{item['repo']}#{item['number']}" for item in derived["blocked_by"])
    if not blockers:
        blockers = "Unknown; native relationships need review." if derived.get("relationships_complete") is False else "None."
    waits = "; ".join(item["waiting_for"] for item in derived["waits"]) or (
        "Review the structured wait record." if derived["status"] == "review" else "None.")
    return f"State: {derived['status']}.\nBlocked by: {blockers}\nWaiting for: {waits}\n" + prose


def label_changes(recorded: list[str], names: dict[str, str], derived: dict[str, Any]) -> dict[str, list[str]]:
    """One status-label diff for catalog and native train consumers."""
    statuses = {names[key].casefold() for key in ("active", "blocked", "waiting", "stale", "done") if key in names}
    wanted = {names[key] for key in derived["labels"] if key in names}
    remove = {name for name in recorded if name.casefold() in statuses
              and name.casefold() not in {value.casefold() for value in wanted}}
    add = {name for name in wanted if name.casefold() not in {value.casefold() for value in recorded}}
    return {"add": sorted(add), "remove": sorted(remove),
            "result": sorted((set(recorded) - remove) | add)}


def project_labels(recorded: list[str], names: dict[str, str], derived: dict[str, Any]) -> list[str]:
    return label_changes(recorded, names, derived)["result"]
