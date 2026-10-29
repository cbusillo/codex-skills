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


def resolve(text: str, explicit: str | None, previous: str = "") -> str:
    supplied = read(text)
    if explicit is not None:
        validate(explicit)
        if supplied is not None and supplied != explicit:
            raise ValueError("--next-actor conflicts with the supplied Next actor line")
    actor = explicit or supplied or read(previous)
    if actor is None:
        raise ValueError("Supply --next-actor or a Next actor line in Current Status or the release handoff")
    return actor
