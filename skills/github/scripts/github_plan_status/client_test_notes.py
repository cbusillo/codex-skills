# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Client notes classification shared by handoff and merge-train consumers.

This controls acceptance before merge only. Every release still needs the
Client's acceptance through its existing gated path.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass

NOTHING_TO_TEST = "Nothing for the Client to test"
LEGACY_NOTHING_TO_TEST = "Nothing for the owner to test"
NO_TEST_MARKERS = (NOTHING_TO_TEST, LEGACY_NOTHING_TO_TEST)
TEST_NOTE_TITLES = {"client test notes", "owner test notes"}
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
INLINE_TEST_NOTES = re.compile(
    r"(?i)^(?:client|owner) test(?: notes|ing)(?:\*\*|[_`])*\s*(?::(?:\*\*|[_`])*|\s*$)"
)
ENGINEERING_RELEASE_TESTS = "Engineering: merge to testing; Client tests at release."
OPEN_CLIENT_QUESTIONS = "Open questions for the Client"
CLIENT_PLACEHOLDER_PREFIX = "[[NEEDS CLIENT:"
# "Nothing for Justin to test." means the marker but would otherwise read as steps.
# Only the declaration's shape, so "Nothing happens when you try to test checkout."
# stays real steps.
NEAR_MISS_NOTHING_TO_TEST = re.compile(
    r"(?i)^nothing(?: (?:new|else|more|further|left))?(?: for [^\n.,:;!?]{1,40}?)? to test\b"
)


@dataclass(frozen=True)
class ClientTestNotes:
    review_required: bool
    error: str = ""
    explicit_override: bool = False


def _markdown_lines(body: str) -> Iterator[tuple[str, re.Match[str] | None, str]]:
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
    return "\n".join(
        section for section in test_notes_sections(body) if section
    ).strip()


def client_test_notes(notes: str, *, review_override: bool = False) -> ClientTestNotes:
    """Validate a Client test notes section with Launchplane's merge-train rule."""
    notes = re.sub(r"<!--[\s\S]*?-->", "", notes).strip()
    if open_client_questions(notes):
        return ClientTestNotes(
            True,
            "Answer the open Client questions before accepting this change.",
            review_override,
        )
    notes = "\n".join(
        line
        for line, heading, _fence in _markdown_lines(notes)
        if not heading
        or heading[2].strip().casefold() != OPEN_CLIENT_QUESTIONS.casefold()
    ).strip()
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
    first, _, steps = value.partition("\n")
    if first == ENGINEERING_RELEASE_TESTS:
        remaining = client_test_notes(steps, review_override=True)
        if remaining.error:
            return ClientTestNotes(
                True,
                "The engineering marker requires real release test steps.",
                review_override,
            )
        return ClientTestNotes(review_override, explicit_override=review_override)
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
    if NEAR_MISS_NOTHING_TO_TEST.match(undecorated):
        return ClientTestNotes(
            True,
            f"Write {NOTHING_TO_TEST}: <reason> exactly; other wording reads as Client test steps.",
            review_override,
        )
    return ClientTestNotes(True, explicit_override=review_override)


def open_client_questions(notes: str) -> str:
    questions: list[str] = []
    collecting = False
    level = 0
    for line, heading, _fence in _markdown_lines(re.sub(r"<!--[\s\S]*?-->", "", notes)):
        if heading:
            if heading[2].strip().casefold() == OPEN_CLIENT_QUESTIONS.casefold():
                collecting = True
                level = len(heading[1])
                continue
            if collecting and len(heading[1]) <= level:
                collecting = False
        if collecting:
            questions.append(line)
    return "\n".join(questions).strip()


def body_client_test_notes(
    body: str, *, review_override: bool = False
) -> ClientTestNotes:
    """Every notes section and batch member keeps its own acceptance requirement."""
    members = tuple(
        client_test_notes(section, review_override=review_override)
        for section in test_notes_sections(body)
        if section
    ) or (client_test_notes("", review_override=review_override),)
    return ClientTestNotes(
        any(member.review_required for member in members),
        next((member.error for member in members if member.error), ""),
        review_override,
    )
