from __future__ import annotations

import re

from .waits import live_lines


def validate(actor: str) -> str:
    if not re.fullmatch(r"agent(?::[^\s<>]+)?|person:[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?|event:[^\r\n<>]+", actor):
        raise ValueError("Next actor requires agent, agent:<session or role>, person:<login> or event:<observable>")
    if (
        actor != actor.strip()
        or any(ord(char) < 32 for char in actor)
        or actor == "event:"
        or actor.removeprefix("event:").isspace()
    ):
        raise ValueError("Next actor must be a nonempty single line")
    return actor


def fields(text: str) -> list[tuple[int, str]]:
    result = []
    for index, line in live_lines(text, prefix=""):
        match = re.match(r"^ {0,3}(?:[-*][ \t]+)?(?:\*\*)?Next actor(?:\*\*)?:(?:\*\*)?[ \t]*(.*)$", line, re.I)
        if match:
            result.append((index, match[1].strip()))
    return result


def read(text: str) -> str | None:
    found = fields(text)
    if len(found) > 1:
        raise ValueError("Current Status must have only one Next actor line")
    return validate(found[0][1]) if found else None


def write(text: str, actor: str) -> str:
    validate(actor)
    found = fields(text)
    if len(found) > 1:
        raise ValueError("Current Status must have only one Next actor line")
    lines = text.splitlines(keepends=True)
    if not found:
        return f"Next actor: {actor}\n" + text
    index = found[0][0]
    previous = ""
    try:
        validate(found[0][1])
    except ValueError:
        previous = f"\nPrevious actor note: {found[0][1]}"
    lines[index] = f"Next actor: {actor}{previous}" + ("\n" if lines[index].endswith("\n") else "")
    return "".join(lines)


def resolve(text: str, explicit: str | None) -> str:
    if explicit is not None:
        validate(explicit)
    try:
        supplied = read(text)
    except ValueError:
        if explicit is None or len(fields(text)) != 1:
            raise
        return explicit
    if explicit is not None:
        if supplied is not None and supplied != explicit:
            raise ValueError("--next-actor conflicts with the supplied Next actor line")
    actor = explicit or supplied
    if actor is None:
        raise ValueError("Supply --next-actor or a Next actor line in Current Status or the release handoff")
    return actor


def record(issue: dict, comments: list[dict], trusted: set[str]) -> dict:
    from . import discussion, events, waits

    normalized = [{**comment, "author": (comment.get("user") or {}).get("login") or comment.get("author") or ""}
                  for comment in comments]
    status = discussion.current_status({**issue, "comments": normalized}, trusted,
                                       body_recorded_at=issue.get("body_recorded_at"))
    return {"status": status, "actor": read(status), "action": discussion.status_field(status, "Next action"),
            "wait": waits.current(issue, comments, trusted), "reopen": events.current(issue, comments, trusted)}


def direction(actor: str | None, wait: dict) -> bool:
    routed = re.compile(r"(?i)\b(?:direction(?:[ :_-]+session)?|weekly[ :_-]+(?:direction[ :_-]+)?audit)\b")
    return bool(actor and routed.search(actor)) or any(routed.search(entry["waiting_for"]) for entry in wait["waits"])
