#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Shared merge-train handoff format and current-head selection."""

import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

READY = "Ready for the merge train"
WAITING = "Engineering ready; waiting for"
HEAD = re.compile(
    r"(?im)^Head(?: SHA)?:[ \t]*(?P<marker>\*\*|`)?"
    r"(?P<sha>[0-9a-f]{40})(?(marker)(?P=marker))(?:[.\s]|$)"
)
NOTHING_TO_TEST = "Nothing for the Client to test"
LEGACY_NOTHING_TO_TEST = "Nothing for the owner to test"
NO_TEST_MARKERS = (NOTHING_TO_TEST, LEGACY_NOTHING_TO_TEST)
TEST_NOTE_TITLES = {"client test notes", "owner test notes"}
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
INLINE_TEST_NOTES = re.compile(
    r"(?i)^(?:client|owner) test(?: notes|ing)(?:\*\*|[_`])*\s*(?::(?:\*\*|[_`])*|\s*$)"
)
MISSING_CLIENT_TEST_NOTES_MESSAGE = (
    'Client test notes are missing; use a "## Client test notes" section '
    'with test instructions or "Nothing for the Client to test: <reason>".'
)
UNCONFIRMED_CLIENT_ACCEPTANCE_MESSAGE = (
    "Client acceptance is not confirmed; use --waiting-for-client, or for "
    'engineering-only changes use "Nothing for the Client to test: <reason>" '
    'under "## Client test notes"'
)


@dataclass(frozen=True)
class ClientTestNotes:
    review_required: bool
    error: str = ""
    explicit_override: bool = False


def handoff_line(line):
    """Normalize Ready and renewed waits with the same presentation rules."""
    line = line.strip().removesuffix(".")
    if line.startswith("**") and line.endswith("**"):
        line = line[2:-2].removesuffix(".")
    return line


def handoff_body(head_sha: str, *, waiting_for_client: bool = False) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ValueError("handoff requires a full 40-character head SHA")
    first = f"{WAITING} the Client's acceptance" if waiting_for_client else READY
    return f"{first}\n\nHead: {head_sha}"


def director_standing_acceptance(repository):
    """Use Launchplane's repository mapping and scoped profile, never repo config."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from skills.launchplane.scripts.launchplane_acceptance import read_director_standing

    return read_director_standing(repository)


def client_release_repository(repository):
    """Check whether Launchplane maps the repository to a Client release."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from skills.launchplane.scripts.launchplane_acceptance import (
        read_client_release_repository,
    )

    return read_client_release_repository(repository)


def _markdown_lines(body: str):
    fence = ""
    for line in body.splitlines():
        fence_match = FENCE.match(line)
        if fence_match:
            marker, rest = fence_match[1], fence_match[2]
            if not fence and not (marker[0] == "`" and "`" in rest):
                fence = marker
            elif (
                fence
                and marker[0] == fence[0]
                and len(marker) >= len(fence)
                and not rest.strip()
            ):
                fence = ""
        heading = HEADING.match(line) if not fence else None
        yield line, heading, fence


def _strip_note_decoration(value: str) -> str:
    return re.sub(r"^(?:[\s>*_`+\-]|\d+[.)]\s)+", "", value)


def test_notes_sections(body: str) -> tuple[str, ...]:
    sections: list[str] = []
    lines: list[str] = []
    collecting = False
    level = 0
    for line, heading, _fence in _markdown_lines(body):
        if heading:
            if heading[2].strip().casefold() in TEST_NOTE_TITLES:
                if collecting:
                    sections.append("\n".join(lines).strip())
                    lines = []
                collecting = True
                level = len(heading[1])
                continue
            if collecting and len(heading[1]) <= level:
                sections.append("\n".join(lines).strip())
                lines = []
                collecting = False
        if collecting:
            lines.append(line)
    if collecting:
        sections.append("\n".join(lines).strip())
    return tuple(sections)


def owner_test_notes(body: str) -> str:
    """Collect Client test notes sections, ignoring headings inside fenced examples."""
    return "\n".join(section for section in test_notes_sections(body) if section).strip()


def client_test_notes(notes: str, *, review_override: bool = False) -> ClientTestNotes:
    """Validate a Client test notes section with Launchplane's merge-train rule."""
    notes = re.sub(r"<!--[\s\S]*?-->", "", notes).strip()
    sections = re.split(r"(?m)^### #([0-9]+)[^\n]*\n", notes)
    if len(sections) > 1:
        member_notes = ([sections[0]] if sections[0].strip() else []) + sections[2::2]
        members = tuple(
            client_test_notes(section, review_override=review_override)
            for section in member_notes
        )
        return ClientTestNotes(
            any(member.review_required for member in members),
            next((member.error for member in members if member.error), ""),
            review_override,
        )
    value = notes
    placeholder = value.casefold().rstrip(" .!?:;")
    if not value or any(
        placeholder == marker or placeholder.startswith(marker + " -")
        for marker in ("none", "n/a", "todo", "tbd")
    ):
        return ClientTestNotes(True, "Client test notes are missing.", review_override)
    marker = next(
        (
            candidate
            for candidate in NO_TEST_MARKERS
            if value.casefold().startswith(candidate.casefold())
        ),
        "",
    )
    if marker:
        suffix = value[len(marker) :]
        separators = {":", "."} if marker == LEGACY_NOTHING_TO_TEST else {":"}
        if (
            (marker == NOTHING_TO_TEST and not value.startswith(marker))
            or not suffix
            or suffix[0] not in separators
            or not suffix[1:].strip()
        ):
            return ClientTestNotes(
                True,
                f"Use {NOTHING_TO_TEST}: <reason> with a nonempty reason.",
                review_override,
            )
        if review_override:
            return ClientTestNotes(
                True, "The Client review label requires real Client test notes.", True
            )
        return ClientTestNotes(False)
    undecorated = _strip_note_decoration(value)
    if undecorated.casefold().startswith(
        (NOTHING_TO_TEST.casefold(), LEGACY_NOTHING_TO_TEST.casefold())
    ):
        return ClientTestNotes(
            True,
            f"Write {NOTHING_TO_TEST}: <reason> as a plain notes line.",
            review_override,
        )
    return ClientTestNotes(True, explicit_override=review_override)


def has_inline_client_test_notes(body: str) -> bool:
    cleaned = re.sub(r"<!--[\s\S]*?-->", "", body)
    for line, heading, fence in _markdown_lines(cleaned):
        if fence or heading:
            continue
        candidate = _strip_note_decoration(line.strip())
        if INLINE_TEST_NOTES.match(candidate):
            return True
    return False


def _is_dependabot_pr(pr) -> bool:
    user = pr.get("user")
    if not isinstance(user, dict):
        return False
    if str(user.get("login") or "").strip().casefold() != "dependabot[bot]":
        return False
    user_type = str(user.get("type") or "").strip().casefold()
    if user_type and user_type != "bot":
        return False
    head_repo = str(pr.get("headRepository") or "").strip().casefold()
    base_repo = str(pr.get("baseRepository") or "").strip().casefold()
    if head_repo and base_repo and head_repo != base_repo:
        return False
    return True


def client_test_notes_error(pr, *, repository="") -> str:
    """Return an actionable error when the merge train would reject the PR's notes."""
    has_review_label = any(
        label["name"].casefold() == "owner-review" for label in pr.get("labels") or []
    )
    body = pr.get("body") or ""
    sections = test_notes_sections(body)
    if sections:
        intent = client_test_notes(
            owner_test_notes(body), review_override=has_review_label
        )
        if intent.error:
            if intent.error == "Client test notes are missing.":
                return MISSING_CLIENT_TEST_NOTES_MESSAGE
            return intent.error
        for section in (item for item in sections if item):
            member = client_test_notes(section, review_override=has_review_label)
            if member.error:
                if member.error == "Client test notes are missing.":
                    return MISSING_CLIENT_TEST_NOTES_MESSAGE
                return member.error
        return ""
    if has_inline_client_test_notes(body):
        return MISSING_CLIENT_TEST_NOTES_MESSAGE
    if _is_dependabot_pr(pr) and not has_review_label:
        return ""
    if repository and client_release_repository(repository):
        return MISSING_CLIENT_TEST_NOTES_MESSAGE
    return ""


def requires_client_acceptance(pr, check_runs, *, repository=""):
    """Keep the producer and watcher on the same Client-review path."""
    if any(label["name"].casefold() == "owner-review" for label in pr["labels"]):
        return True
    # Launchplane completes engineering-only advisory checks as neutral.
    # Pending, accepted and rejected reviews still follow the acceptance path.
    if any(
        check["name"] == "launchplane/owner-review"
        and not (check.get("status") == "completed" and check.get("conclusion") == "neutral")
        for check in check_runs
    ):
        return True
    body = pr.get("body") or ""
    sections = test_notes_sections(body)
    if not sections:
        return False
    non_empty = [section for section in sections if section]
    if not non_empty:
        return True
    for section in non_empty:
        intent = client_test_notes(section)
        if intent.error:
            return True
        if intent.review_required:
            return not (repository and director_standing_acceptance(repository))
    return False


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return parsed.timestamp()


def latest_ready(pr, comments, trusted):
    """A renewed wait invalidates Ready; a push requires a new explicit head."""
    handoffs = []
    for comment in comments:
        if (comment.get("user") or {}).get("login", "").casefold() not in trusted:
            continue
        body = comment.get("body") or ""
        first = handoff_line(body.splitlines()[0]) if body.splitlines() else ""
        if first == READY or first.startswith(WAITING):
            handoffs.append(comment)
    if not handoffs:
        return False
    latest = max(
        handoffs,
        key=lambda item: (
            timestamp(item.get("updated_at") or item["created_at"]),
            item["id"],
        ),
    )
    body = latest["body"]
    return handoff_line(body.splitlines()[0]) == READY and [
        match.group("sha") for match in HEAD.finditer(body)
    ] == [pr["head"]["sha"]]
