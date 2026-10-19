#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Pure parsing and preservation of the GitHub planning ownership envelope."""

from __future__ import annotations

import re
from typing import Any


class PlanBodyError(ValueError):
    """An ambiguous or malformed planning ownership envelope."""


PLAN_ORIGINAL_START = "<!-- github-plan:original-request:start -->"
PLAN_ORIGINAL_END = "<!-- github-plan:original-request:end -->"
PLAN_MANAGED_START = "<!-- github-plan:managed:start -->"
PLAN_MANAGED_END = "<!-- github-plan:managed:end -->"
PLAN_MANAGED_PROVENANCE_MARKER = "<!-- github-plan:managed-provenance -->"
PLAN_OWNERSHIP_MARKERS = (
    PLAN_ORIGINAL_START,
    PLAN_ORIGINAL_END,
    PLAN_MANAGED_START,
    PLAN_MANAGED_END,
)
PLAN_RESERVED_MARKERS = PLAN_OWNERSHIP_MARKERS + (PLAN_MANAGED_PROVENANCE_MARKER,)
PLAN_MANAGED_NOTICE = "_The following implementation plan is maintained by project automation._"


def marked_block(body: str, start_marker: str, end_marker: str) -> tuple[int, int, str] | None:
    start_count = body.count(start_marker)
    end_count = body.count(end_marker)
    if start_count == 0 and end_count == 0:
        return None
    if start_count != 1 or end_count != 1:
        raise PlanBodyError("Issue body contains duplicate or incomplete GitHub plan ownership markers")
    start_match = re.search(rf"(?m)^{re.escape(start_marker)}[ \t]*(?:\r?\n|$)", body)
    end_match = re.search(rf"(?m)^{re.escape(end_marker)}[ \t]*(?:\r?\n|$)", body)
    if start_match is None or end_match is None:
        raise PlanBodyError("GitHub plan ownership markers must occupy their own lines")
    content_start = start_match.end()
    content_end = end_match.start()
    if content_end < content_start:
        raise PlanBodyError("Issue body contains out-of-order GitHub plan ownership markers")
    if body[content_end - 2:content_end] == "\r\n":
        content_end -= 2
    elif body[content_end - 1:content_end] == "\n":
        content_end -= 1
    content_end = max(content_start, content_end)
    return content_start, content_end, body[content_start:content_end]


def contributor_plan_body(issue: dict[str, Any]) -> str | None:
    body = issue.get("body") or ""
    if not any(marker in body for marker in PLAN_OWNERSHIP_MARKERS):
        return None
    managed = marked_block(body, PLAN_MANAGED_START, PLAN_MANAGED_END)
    original = marked_block(body, PLAN_ORIGINAL_START, PLAN_ORIGINAL_END)
    if managed is None or original is None:
        raise PlanBodyError("Contributor issue has incomplete GitHub plan ownership markers")
    newline = r"\r?\n"
    canonical = re.fullmatch(
        rf"## Original request{newline}{newline}{re.escape(PLAN_ORIGINAL_START)}[ \t]*{newline}"
        rf"(?P<original>.*?){newline}{re.escape(PLAN_ORIGINAL_END)}[ \t]*{newline}{newline}---{newline}{newline}"
        rf"{re.escape(PLAN_MANAGED_NOTICE)}{newline}{newline}{re.escape(PLAN_MANAGED_START)}[ \t]*{newline}"
        rf"(?P<managed>.*?){newline}{re.escape(PLAN_MANAGED_END)}(?:[ \t]*{newline})*[ \t]*",
        body,
        flags=re.DOTALL,
    )
    if canonical is None:
        raise PlanBodyError("Contributor issue GitHub plan ownership markers are not in the canonical envelope")
    return managed[2]


def wrap_contributor_plan(issue: dict[str, Any], managed_body: str) -> str:
    original_body = issue.get("body") or ""
    original_request = original_body if original_body else str(issue.get("title") or "").strip()
    if any(marker in original_request for marker in PLAN_RESERVED_MARKERS):
        raise PlanBodyError("Contributor request already contains reserved GitHub plan ownership markers")
    return (
        "## Original request\n\n"
        f"{PLAN_ORIGINAL_START}\n"
        f"{original_request}\n"
        f"{PLAN_ORIGINAL_END}\n\n"
        "---\n\n"
        f"{PLAN_MANAGED_NOTICE}\n\n"
        f"{PLAN_MANAGED_START}\n"
        f"{managed_body.rstrip()}\n"
        f"{PLAN_MANAGED_END}\n"
    )
