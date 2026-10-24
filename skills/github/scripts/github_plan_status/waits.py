"""Pure structured-wait format and trusted-record selection."""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from typing import Any

from .body import contributor_plan_body

PREFIX = "<!-- github-plan:wait "


def trusted_authors(owner: str, *, configured_bots: Iterable[str] = (),
                    automation: str | None = None, expected_actor: str | None = None) -> set[str]:
    """Compose existing author authority; adapters discover identities, never this package."""
    return {name.casefold() for name in (*configured_bots, owner, automation, expected_actor) if name is not None and name}


def author_is_trusted(source: dict[str, Any], owner: str, *,
                      author_logins: Iterable[str] = (), author_ids: Iterable[int] = ()) -> bool:
    from . import normalize_author
    user = normalize_author(source)
    return (str(user.get("login", "")).casefold() in trusted_authors(owner, configured_bots=author_logins)
            or (type(user.get("id")) is int and user["id"] in author_ids))


def marker(record: dict[str, Any]) -> str:
    return PREFIX + json.dumps(record, sort_keys=True).replace(">", "\\u003e") + " -->"


def reviewed(*, evidence: str, waiting_for: str | list[str] | None = None,
             ends_when: str | list[str] | None = None) -> dict[str, Any]:
    if not evidence.strip():
        raise ValueError("Wait review requires existing evidence")
    actors = [waiting_for] if isinstance(waiting_for, str) else waiting_for or []
    endings = [ends_when] if isinstance(ends_when, str) else ends_when or []
    if len(actors) != len(endings):
        raise ValueError("Each --waiting-for requires a corresponding --ends-when")
    record = {"version": 1, "state": "waiting" if waiting_for else "clear", "evidence": evidence,
              "waits": [{"waiting_for": actor, "ends_when": end} for actor, end in zip(actors, endings)]}
    validate(record)
    return record


def validate(record: Any) -> None:
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


def live_lines(text: str, *, prefix: str = PREFIX) -> list[tuple[int, str]]:
    result = []
    fence = None
    html_end = None
    for index, line in enumerate(text.splitlines()):
        if html_end:
            if html_end in line.casefold():
                html_end = None
            continue
        html = re.match(r"^ {0,3}<(pre|code|blockquote|details|script|style|textarea)[\s>]", line, re.I)
        if html:
            end = f"</{html.group(1).casefold()}>"
            if end not in line.casefold():
                html_end = end
            continue
        opener = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if opener:
            token = opener.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is not None or not line.startswith(prefix):
            continue
        result.append((index, line))
    return result


def records(text: str, *, prefix: str = PREFIX, validator: Callable[[Any], None] = validate) -> list[dict[str, Any]]:
    result = []
    for _, line in live_lines(text, prefix=prefix):
        match = re.fullmatch(re.escape(prefix) + r"(.*?) -->", line)
        if not match:
            raise ValueError(f"Malformed structured marker: {prefix}")
        record = json.loads(match.group(1))
        validator(record)
        result.append(record)
    return result


def current_record(issue: dict[str, Any], comments: list[dict[str, Any]], trusted: set[str] | Callable[[dict[str, Any]], bool], *,
                   prefix: str, validator: Callable[[Any], None]) -> dict[str, Any] | None:
    """Last authorized record wins; arbitrary discussion cannot change it."""
    selected = None
    sources = [issue, *comments]
    for source in sources:
        from . import normalize_author
        author = normalize_author(source)["login"]
        if not (trusted(source) if callable(trusted) else author.casefold() in {login.casefold() for login in trusted}):
            continue
        found = records(source.get("body") or "", prefix=prefix, validator=validator)
        if len(found) > 1:
            raise ValueError("Multiple structured records in one source; preserve them for review")
        if found:
            selected = found[0]
    return selected


def resolve(issue: dict[str, Any], comments: list[dict[str, Any]],
            trusted: set[str] | Callable[[dict[str, Any]], bool]) -> tuple[dict[str, Any], bool]:
    """Return the current wait and whether a trusted record set it.

    With no trusted record the wait is an implied review. A trusted record can
    also ask for review, so readers use the flag to tell the two apart.
    """
    managed = contributor_plan_body(issue)
    source = {**issue, "body": managed} if managed is not None else issue
    record = current_record(source, comments, trusted, prefix=PREFIX, validator=validate)
    if record is None:
        return {"version": 1, "state": "review", "evidence": "", "waits": []}, False
    return record, True


def current(issue: dict[str, Any], comments: list[dict[str, Any]], trusted: set[str] | Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    return resolve(issue, comments, trusted)[0]


def without_records(text: str) -> str:
    indexes = {index for index, _ in live_lines(text)}
    return "\n".join(line for index, line in enumerate(text.splitlines()) if index not in indexes)


def require_clear(record: dict[str, Any]) -> None:
    validate(record)
    if record["state"] != "clear":
        raise ValueError("Preserve an unresolved or unreviewed structured wait")
