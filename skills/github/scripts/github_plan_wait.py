# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Structured claim waits; prose is evidence for review, never authority."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

try:
    import github_identity
    import github_plan_status
except ModuleNotFoundError:
    from skills.github.scripts import github_identity, github_plan_status


PREFIX = "<!-- github-plan:wait "


def authors(repo: str, *, expected_actor: str | None = None) -> set[str]:
    return set(github_identity.configured_bot_logins()) | {
        name for name in (github_identity.automation_login(), expected_actor, repo.split("/")[0]) if name}


def marker(record: dict[str, Any]) -> str:
    return PREFIX + json.dumps(record, sort_keys=True) + " -->"


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
    github_plan_status.validate_wait(record)


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


def current_record(issue: dict[str, Any], comments: list[dict[str, Any]], trusted: set[str], *,
                   prefix: str, validator: Callable[[Any], None]) -> dict[str, Any] | None:
    """Last authorized record wins; arbitrary discussion cannot change it."""
    selected = None
    sources = [issue, *comments]
    for source in sources:
        author = (source.get("user") or {}).get("login", "")
        if author.casefold() not in {login.casefold() for login in trusted}:
            continue
        found = records(source.get("body") or "", prefix=prefix, validator=validator)
        if len(found) > 1:
            raise ValueError("Multiple structured records in one source; preserve them for review")
        if found:
            selected = found[0]
    return selected


def current(issue: dict[str, Any], comments: list[dict[str, Any]], trusted: set[str]) -> dict[str, Any]:
    return current_record(issue, comments, trusted, prefix=PREFIX, validator=validate) or {
        "version": 1, "state": "review", "evidence": "", "waits": []}


def without_records(text: str) -> str:
    indexes = {index for index, _ in live_lines(text)}
    return "\n".join(line for index, line in enumerate(text.splitlines()) if index not in indexes)
