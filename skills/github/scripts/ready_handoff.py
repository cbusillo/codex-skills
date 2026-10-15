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
    fence = None
    for line in body.splitlines():
        ticks = FENCE.match(line)
        if ticks:
            marker, info = ticks.group(1), ticks.group(2)
            if fence is None:
                if "`" not in marker or "`" not in info:
                    fence = (marker[0], len(marker))
                yield line, True
                continue
            if marker[0] == fence[0] and len(marker) >= fence[1] and not info.strip():
                fence = None
            yield line, True
            continue
        yield line, fence is not None


def _strip_note_decoration(value: str) -> str:
    return re.sub(r"^(?:[\s>*_`+\-]|\d+[.)]\s)+", "", value)


def _starts_with_no_test_marker(value: str) -> bool:
    folded = value.casefold()
    for marker in NO_TEST_MARKERS:
        prefix = marker.casefold()
        if folded == prefix or (
            folded.startswith(prefix) and not folded[len(prefix)].isalnum()
        ):
            return True
    return False


def _valid_no_test_note(value: str) -> bool:
    if value.startswith(NOTHING_TO_TEST):
        suffix = value[len(NOTHING_TO_TEST) :]
        return suffix.startswith(":") and bool(suffix[1:].strip())
    if value.casefold().startswith(LEGACY_NOTHING_TO_TEST.casefold()):
        suffix = value[len(LEGACY_NOTHING_TO_TEST) :]
        return suffix[:1] in {":", "."} and bool(suffix[1:].strip())
    return False


def _is_placeholder_note(value: str) -> bool:
    normalized = " ".join(value.split()).casefold().rstrip(" .!?:;")
    return normalized in {"none", "n/a", "todo", "tbd"} or any(
        normalized.startswith(f"{marker.casefold()} -") for marker in NO_TEST_MARKERS
    )


def _single_client_test_notes(
    notes: str, *, review_override: bool = False
) -> ClientTestNotes:
    cleaned = re.sub(r"<!--[\s\S]*?-->", "", notes).strip()
    if not cleaned or _is_placeholder_note(cleaned):
        return ClientTestNotes(False, error="Client test notes are missing.")
    if _starts_with_no_test_marker(cleaned):
        if not _valid_no_test_note(cleaned):
            return ClientTestNotes(
                False,
                error=f"Use {NOTHING_TO_TEST}: <reason> with a nonempty reason.",
            )
        if review_override:
            return ClientTestNotes(
                False, error="The Client review label requires real Client test notes."
            )
        return ClientTestNotes(False)
    if _starts_with_no_test_marker(_strip_note_decoration(cleaned)):
        return ClientTestNotes(
            False,
            error=f"Write {NOTHING_TO_TEST}: <reason> as a plain notes line.",
        )
    return ClientTestNotes(True, explicit_override=review_override)


def client_test_notes(notes: str, *, review_override: bool = False) -> ClientTestNotes:
    """Validate a Client test notes section with Launchplane's merge-train rule."""
    cleaned = re.sub(r"<!--[\s\S]*?-->", "", notes).strip()
    parts = re.split(r"(?m)^### #([0-9]+)[^\n]*\n", cleaned)
    if len(parts) == 1:
        return _single_client_test_notes(cleaned, review_override=review_override)
    if parts[0].strip():
        return ClientTestNotes(
            False,
            error="Batch Client test notes must be inside ### #<pr> sections.",
        )
    required = False
    for index in range(1, len(parts), 2):
        member = _single_client_test_notes(parts[index + 1])
        if member.error:
            return ClientTestNotes(False, error=f"PR #{parts[index]}: {member.error}")
        required = required or member.review_required
    if review_override and not required:
        return ClientTestNotes(
            False, error="The Client review label requires real Client test notes."
        )
    return ClientTestNotes(
        required or review_override, explicit_override=review_override
    )


def test_notes_sections(body: str) -> tuple[str, ...]:
    sections: list[str] = []
    lines: list[str] = []
    level = 0
    for line, in_fence in _markdown_lines(body):
        heading = None if in_fence else HEADING.match(line)
        if heading:
            current_level = len(heading.group(1))
            title = heading.group(2).strip().casefold()
            if level and current_level <= level:
                sections.append("\n".join(lines).strip())
                lines = []
                level = 0
            if not level and title in TEST_NOTE_TITLES:
                level = current_level
                continue
        if level:
            lines.append(line)
    if level:
        sections.append("\n".join(lines).strip())
    return tuple(sections)


def has_inline_client_test_notes(body: str) -> bool:
    cleaned = re.sub(r"<!--[\s\S]*?-->", "", body)
    for line, in_fence in _markdown_lines(cleaned):
        if in_fence or HEADING.match(line):
            continue
        candidate = _strip_note_decoration(line.strip())
        if INLINE_TEST_NOTES.match(candidate):
            return True
    return False


def client_test_notes_error(pr, *, repository="") -> str:
    """Return an actionable error when the merge train would reject the PR's notes."""
    has_review_label = any(
        label["name"].casefold() == "owner-review" for label in pr.get("labels") or []
    )
    body = pr.get("body") or ""
    sections = test_notes_sections(body)
    if sections:
        for section in sections:
            intent = client_test_notes(section, review_override=has_review_label)
            if intent.error:
                if intent.error == "Client test notes are missing.":
                    return MISSING_CLIENT_TEST_NOTES_MESSAGE
                return intent.error
        return ""
    if has_inline_client_test_notes(body):
        return MISSING_CLIENT_TEST_NOTES_MESSAGE
    user = pr.get("user")
    login = (
        str(user.get("login") or "").strip().casefold()
        if isinstance(user, dict)
        else ""
    )
    if login == "dependabot[bot]" and not has_review_label:
        return ""
    if repository and (
        client_release_repository(repository) or director_standing_acceptance(repository)
    ):
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
        return has_inline_client_test_notes(body)
    for section in sections:
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
