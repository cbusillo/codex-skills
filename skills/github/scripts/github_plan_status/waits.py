"""Pure structured-wait format and trusted-record selection."""
from __future__ import annotations

import hashlib
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


def agent_step(actor: str) -> bool:
    return re.match(r"(?i)^(?:the\s+|next\s+)?(?:agents?|supervisor)(?:\b|:)", actor.strip()) is not None


def reviewed(*, evidence: str, waiting_for: str | list[str] | None = None,
             ends_when: str | list[str] | None = None, release_product: str | None = None,
             release_commit: str | None = None, release_next_action: str | None = None, not_before: str | None = None,
             event: dict[str, Any] | None = None) -> dict[str, Any]:
    if not evidence.strip():
        raise ValueError("Wait review requires existing evidence")
    actors = [waiting_for] if isinstance(waiting_for, str) else waiting_for or []
    endings = [ends_when] if isinstance(ends_when, str) else ends_when or []
    if len(actors) != len(endings):
        raise ValueError("Each --waiting-for requires a corresponding --ends-when")
    if any(agent_step(actor) for actor in actors):
        raise ValueError("Agent and Supervisor steps belong in Next actor and Next action, not --waiting-for")
    record = {"version": 1, "state": "waiting" if waiting_for else "clear", "evidence": evidence,
              "waits": [{"waiting_for": actor, "ends_when": end} for actor, end in zip(actors, endings)]}
    if not_before is not None or event is not None:
        from . import events
        if not actors or (not_before is not None and event is not None):
            raise ValueError("Supply a wait and either --not-before or --event")
        event = {"version": 1, "kind": "date", "at": not_before, "evidence": evidence} if not_before is not None else event
        events.validate(event)
        if event.get("state") == "clear" or event.get("kind") == "recurrence":
            raise ValueError("A wait needs a machine-checkable event")
        record["waits"][0]["event"] = event
    if any(value is not None for value in (release_product, release_commit, release_next_action)):
        if not actors:
            raise ValueError("A release pointer requires a wait")
        record["waits"][0]["release"] = {"product": " ".join(release_product.split()).casefold() if release_product else release_product, "source_commit": release_commit.strip().lower() if release_commit else release_commit,
                                         "next_action": " ".join(release_next_action.split()) if release_next_action else release_next_action}
    validate(record)
    return record


def validate(record: Any) -> None:
    if (not isinstance(record, dict) or set(record) not in ({"version", "state", "evidence", "waits"},
                                                   {"version", "state", "evidence", "waits", "replaces"})
            or type(record["version"]) is not int or record["version"] != 1
            or not isinstance(record["state"], str) or record["state"] not in {"clear", "waiting", "review"}
            or not isinstance(record["evidence"], str) or not isinstance(record["waits"], list)):
        raise ValueError("Malformed structured wait record; review with gh-plan.py wait")
    if "replaces" in record and (not isinstance(record["replaces"], str) or re.fullmatch(r"[0-9a-f]{64}", record["replaces"]) is None):
        raise ValueError("A conditional wait resolution needs its previous record digest")
    if record["state"] != "review" and not record["evidence"].strip():
        raise ValueError("Structured wait record lacks review evidence")
    if bool(record["waits"]) != (record["state"] == "waiting"):
        raise ValueError("Structured wait state and entries disagree")
    for wait in record["waits"]:
        if (not isinstance(wait, dict) or (set(wait) - {"event"}) not in ({"waiting_for", "ends_when"}, {"waiting_for", "ends_when", "release"})
                or any(not isinstance(value, str) or not value.strip() for value in (wait["waiting_for"], wait["ends_when"]))):
            raise ValueError("A structured wait needs its actor/event and ending condition")

        if "event" in wait:
            from . import events
            events.validate(wait["event"])
            if wait["event"].get("state") == "clear" or wait["event"].get("kind") == "recurrence" or "release" in wait:
                raise ValueError("A wait has one machine-checkable ending event")
        if "release" in wait:
            release = wait["release"]
            if (not isinstance(release, dict) or set(release) != {"product", "source_commit", "next_action"}
                    or any(not isinstance(value, str) or not value.strip() for value in release.values())):
                raise ValueError("A release wait needs a product, source commit and next agent action")
            if re.fullmatch(r"[0-9a-f]{40}", release["source_commit"]) is None:
                raise ValueError("A release wait needs its full source commit")


def snapshot(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "replaces"}


def record_digest(record: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(snapshot(record), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def release_event_hint(product: str, source_commit: str = "") -> str:
    return f"Release event: {product} includes {source_commit}".strip()


def resolve_release(record: dict[str, Any], *, product: str, contains_commit: Callable[[str], bool],
                    evidence: str) -> tuple[dict[str, Any], list[str]]:
    validate(record)
    matched = [entry for entry in record["waits"] if "release" in entry
               and entry["release"]["product"].casefold() == product.strip().casefold()
               and contains_commit(entry["release"]["source_commit"])]
    if not matched:
        return record, []
    if not evidence.strip():
        raise ValueError("Release resolution requires completion evidence")
    remaining = [entry for entry in record["waits"] if entry not in matched]
    resolved = {**record, "state": "waiting" if remaining else "clear", "waits": remaining,
                "evidence": record["evidence"] + "\n" + evidence, "replaces": record_digest(record)}
    validate(resolved)
    return resolved, [entry["release"]["next_action"] for entry in matched]


def resolve_events(record: dict[str, Any], *, now: Any, one: Any, listing: Any) -> dict[str, Any]:
    from . import events

    matched = [entry for entry in record["waits"] if "event" in entry
               and events.fired(entry["event"], now=now, one=one, listing=listing)]
    if not matched:
        return record
    remaining = [entry for entry in record["waits"] if entry not in matched]
    return {**record, "state": "waiting" if remaining else "clear", "waits": remaining,
            "evidence": record["evidence"] + "\nObserved completed events: " + "; ".join(events.describe(entry["event"]) for entry in matched),
            "replaces": record_digest(record)}


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


def _select_record(issue: dict[str, Any], comments: list[dict[str, Any]], trusted: set[str] | Callable[[dict[str, Any]], bool], *,
                   prefix: str, validator: Callable[[Any], None]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Last authorized record wins; arbitrary discussion cannot change it."""
    selected = None
    selected_source = None
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
            record = found[0]
            keep_age = False
            if prefix == PREFIX and "replaces" in record:
                if selected is None or record["replaces"] != record_digest(selected):
                    continue
                keep_age = bool(record["waits"]) and all(entry in selected["waits"] for entry in record["waits"])
            selected = snapshot(record) if prefix == PREFIX else record
            if not keep_age:
                selected_source = source
    return selected, selected_source


def current_record(issue: dict[str, Any], comments: list[dict[str, Any]],
                   trusted: set[str] | Callable[[dict[str, Any]], bool], *,
                   prefix: str, validator: Callable[[Any], None]) -> dict[str, Any] | None:
    return _select_record(issue, comments, trusted, prefix=prefix, validator=validator)[0]


def _wait_selection(issue: dict[str, Any], comments: list[dict[str, Any]],
                    trusted: set[str] | Callable[[dict[str, Any]], bool]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    managed = contributor_plan_body(issue)
    source = {**issue, "body": managed} if managed is not None else issue
    record, origin = _select_record(source, comments, trusted, prefix=PREFIX, validator=validate)
    return record, issue if origin is source else origin


def current_source(issue: dict[str, Any], comments: list[dict[str, Any]],
                   trusted: set[str] | Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
    return _wait_selection(issue, comments, trusted)[1]


def resolve(issue: dict[str, Any], comments: list[dict[str, Any]],
            trusted: set[str] | Callable[[dict[str, Any]], bool]) -> tuple[dict[str, Any], bool]:
    """Return the current wait and whether a trusted record set it.

    With no trusted record the wait is an implied review. A trusted record can
    also ask for review, so readers use the flag to tell the two apart.
    """
    record, _ = _wait_selection(issue, comments, trusted)
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
