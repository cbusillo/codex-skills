from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable
from typing import Any

from . import waits

PREFIX = "<!-- github-plan:reopen-trigger "
INDEX_LABEL = "plan:reopen-trigger"
FIELDS = {
    "date": {"at"},
    "issue_closed": {"url"},
    "pr_merged": {"url"},
    "release_published": {"repo", "minimum_tag"},
    "advisory_fixed": {"ghsa_id"},
    "recurrence": {"symptom"},
}
TARGET = re.compile(r"https://github.com/([\w.-]+/[\w.-]+)/(issues|pull)/([1-9][0-9]*)")


def date(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return parsed.replace(tzinfo=dt.UTC)
    if parsed.tzinfo is None:
        raise ValueError("Trigger timestamp requires a timezone (or YYYY-MM-DD)")
    return parsed


def version(tag: str) -> tuple[int, ...]:
    if not re.fullmatch(r"v?\d+(?:\.\d+)*", tag):
        raise ValueError("minimum_tag must be a stable numeric tag, e.g. v1.2.3")
    parts = [int(v) for v in tag.removeprefix("v").split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def validate(record: Any) -> None:
    if isinstance(record, dict) and record.get("state") == "clear":
        if (
            set(record) != {"version", "state", "evidence"}
            or type(record["version"]) is not int
            or record["version"] != 1
            or not isinstance(record["evidence"], str)
            or not record["evidence"].strip()
        ):
            raise ValueError("Malformed cleared reopen trigger")
        return
    if not isinstance(record, dict) or not isinstance(record.get("kind"), str) or record["kind"] not in FIELDS:
        raise ValueError("Unknown reopen-trigger kind")
    kind = record["kind"]
    if (
        set(record) != {"version", "kind", "evidence"} | FIELDS[kind]
        or type(record["version"]) is not int
        or record["version"] != 1
        or any(not isinstance(record[k], str) or not record[k].strip() for k in {"evidence"} | FIELDS[kind])
    ):
        raise ValueError("Malformed structured reopen trigger")
    if kind == "date":
        date(record["at"])
    elif kind in {"issue_closed", "pr_merged"}:
        target = TARGET.fullmatch(record["url"])
        if not target or target[2] != ("issues" if kind == "issue_closed" else "pull"):
            raise ValueError("Trigger requires an exact GitHub issue or PR URL of the matching kind")
    elif kind == "release_published":
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", record["repo"]):
            raise ValueError("Release trigger requires OWNER/REPO")
        version(record["minimum_tag"])
    elif kind == "advisory_fixed" and not re.fullmatch(r"GHSA(?:-[23456789cfghjmpqrvwx]{4}){3}", record["ghsa_id"], re.IGNORECASE):
        raise ValueError("Trigger requires a GHSA identifier")


def describe(record: dict) -> str:
    if record.get("state") == "clear":
        return "cleared: " + record["evidence"]
    return record["kind"] + ": " + "; ".join(str(record[key]) for key in sorted(FIELDS[record["kind"]]))


def marker(record: dict) -> str:
    return PREFIX + json.dumps(record, sort_keys=True).replace(">", "\\u003e") + " -->"


def current(issue: dict, comments: list[dict], trusted: set[str]) -> dict | None:
    return waits.current_record(issue, comments, trusted, prefix=PREFIX, validator=validate)


def fired(record: dict, *, now: dt.datetime, one: Callable, listing: Callable) -> bool:
    validate(record)
    if record.get("state") == "clear":
        return False
    kind = record["kind"]
    if kind == "recurrence":
        return False
    if kind == "date":
        return date(record["at"]) <= now
    if kind in {"issue_closed", "pr_merged"}:
        target = TARGET.fullmatch(record["url"])
        assert target is not None
        path = "issues" if kind == "issue_closed" else "pulls"
        item = one(f"repos/{target[1]}/{path}/{target[3]}")
        if item.get("state") not in {"open", "closed"}:
            raise ValueError("Trigger target state unavailable")
        if kind == "issue_closed":
            if "pull_request" in item:
                raise ValueError("Issue trigger target is a pull request")
            return item["state"] == "closed"
        if "merged_at" not in item:
            raise ValueError("PR merge state unavailable")
        return item["state"] == "closed" and bool(item["merged_at"])
    if kind == "release_published":
        minimum = version(record["minimum_tag"])

        def matches(release: dict[str, Any]) -> bool:
            if (
                not isinstance(release, dict)
                or not isinstance(release.get("draft"), bool)
                or not isinstance(release.get("prerelease"), bool)
                or "published_at" not in release
                or not isinstance(release.get("tag_name"), str)
            ):
                raise ValueError("Release publication information unavailable")
            if release["draft"] or release["prerelease"] or not release["published_at"]:
                return False
            try:
                return version(release["tag_name"]) >= minimum
            except ValueError:
                return False

        return any(matches(release) for release in listing(f"repos/{record['repo']}/releases", stop=matches))
    advisory = one(f"advisories/{record['ghsa_id']}")
    vulnerabilities = advisory.get("vulnerabilities")
    if (
        not isinstance(vulnerabilities, list)
        or "withdrawn_at" not in advisory
        or any(not isinstance(v, dict) or "first_patched_version" not in v for v in vulnerabilities)
    ):
        raise ValueError("Advisory patch information unavailable")
    return (
        advisory["withdrawn_at"] is None
        and bool(vulnerabilities)
        and all(isinstance(v["first_patched_version"], str) and bool(v["first_patched_version"].strip()) for v in vulnerabilities)
    )
