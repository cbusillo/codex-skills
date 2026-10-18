#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Shared merge-train handoff format and current-head selection."""

import base64
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
from urllib.parse import quote

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
REVIEW_CONFIG_PATH = ".github/github.json"
# Leads sessions and the Supervisor already write; see model-review Reporting.
REVIEW_LEAD = re.compile(r"^[\s#>*_]+")
REVIEW_OUTCOME = re.compile(
    r"(?i)^(?:[\w-]+ )?review of record(?: \([^)]*\))?:"
    r"|^review weighed\b|^review by another model\b"
    r"|^(?:openai|anthropic|google|gemini|claude)\b.*?\bcompleted the review of record\b"
)
# An outcome needs an affirmative result. Examine only the text preceding
# that result for unfinished review status; the findings describe the code.
REVIEW_RESULT = re.compile(
    r"(?i)\b(?:completed|finished|reviewed|found|identified)\b"
    r"|\brun by (?:the )?supervisor\b"
    r"|\b(?:no findings|none(?: to act on)?|nothing to act on|no changes needed)\b(?!\s+yet)"
    r"|\bfindings?\s*:(?=\s|$)|\bfindings? (?:acted on|fixed|addressed|declined)\b"
    r"|\bacted on\b|\bis correct\b"
)
REVIEW_UNFINISHED = re.compile(
    r"(?i)\b(?:not|never|neither|cannot|unable|pending|blocked|requested|needed|waiting"
    r"|outstanding|incomplete|unavailable|queued|running|errored|failed|retrying|rerunning|declined|refused"
    r"|will|would|until)\b(?![-\w])|\btimed out\b"
)
# A provider refusal or a continuation explicitly saying this review is still
# running overrides result words from partial file coverage or launch probes.
REVIEW_PROGRESS = re.compile(
    r"(?i)\b(?:declined|refused) to review\b"
    r"|(?:[.;,]\s*|\band (?:is )?)(?:still running|waiting for (?:the )?result)\b"
    r"|[.;,]\s*(?:rerunning|retrying)(?=\s+(?:at|review)\b|[.!]|$)"
)
REVIEW_REQUEST = re.compile(
    r"(?i)^review (?:prompt|request)\b|^review of record (?:still )?(?:needed|requested)\b"
)
REVIEW_REQUEST_CONTEXT = re.compile(
    r"(?i)\breview of record\b|\banother model\b|\bmodel[- ]review\b"
    r"|\bprovider\s*:\s*`?(?:openai|anthropic|google)\b"
)
SUPERVISOR_RUNS_REVIEW = re.compile(
    r"(?i)^(?:.*?\bso\s+)?supervisor runs the review[.:]?\s*$"
)
REVIEW_HEAD = re.compile(
    r"(?i)(?:\bhead(?: SHA)?\s*:?\s*[`*]*|\bat\s+`)(?P<sha>[0-9a-f]{7,40})\b"
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


def review_record(body):
    """Classify a review-of-record comment by the leads already in use."""
    lines = (body or "").strip().splitlines()
    first = REVIEW_LEAD.sub("", lines[0]) if lines else ""
    result_text = first
    result = REVIEW_RESULT.search(result_text)
    # Some existing records put the actual result in a following section.
    if not result and REVIEW_OUTCOME.search(first):
        result_text = "\n".join(lines)
        result = REVIEW_RESULT.search(result_text)
        prefix = result_text[:result.start()] if result else ""
    else:
        prefix = first[:result.start()] if result else ""
    if (
        REVIEW_OUTCOME.search(first) and result
        and not REVIEW_UNFINISHED.search(prefix)
        and not REVIEW_PROGRESS.search(result_text)
    ):
        return "outcome"
    explicit_request = REVIEW_REQUEST.search(first) and REVIEW_REQUEST_CONTEXT.search(first)
    supervisor_request = any(
        SUPERVISOR_RUNS_REVIEW.fullmatch(line.strip().replace("**", ""))
        for line in lines if not line.lstrip().startswith((">", "`"))
    )
    if explicit_request or supervisor_request:
        return "request"
    return None


def review_required_patterns(config):
    """Read qualityGate.modelReview.requiredPaths from the base branch's config."""
    value = ((config.get("qualityGate") or {}).get("modelReview") or {}).get("requiredPaths", [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError("qualityGate.modelReview.requiredPaths must list path patterns")
    return tuple(value)


def review_required_files(files, patterns):
    names = {
        name
        for item in files
        for name in (item.get("filename"), item.get("previous_filename"))
        if name
    }
    return sorted(name for name in names if any(fnmatchcase(name, p) for p in patterns))


def review_of_record_gap(comments, trusted, *, required_files=(), head=None):
    """Return why a required review of record is unrecorded, or an empty string.

    A posted request with no later outcome is pending. A change to a
    review-required path also needs an outcome naming the current head.
    """
    latest = {}
    for comment in comments:
        if (comment.get("user") or {}).get("login", "").casefold() not in trusted:
            continue
        kind = review_record(comment.get("body"))
        if kind == "outcome" and head:
            body = comment.get("body") or ""
            declared = [match.group("sha").lower() for match in HEAD.finditer(body)]
            reviewed_heads = declared or [
                match.group("sha").lower()
                for match in REVIEW_HEAD.finditer(body.splitlines()[0])
            ]
            if not reviewed_heads or not all(head.lower().startswith(sha) for sha in reviewed_heads):
                continue
        if kind:
            key = (timestamp(comment["created_at"]), comment["id"])
            if kind not in latest or latest[kind][0] < key:
                latest[kind] = (key, comment)
    request, outcome = latest.get("request"), latest.get("outcome")
    if request and (not outcome or outcome[0] < request[0]):
        where = request[1].get("html_url") or f"comment {request[1]['id']}"
        return (
            f"A review of record was requested ({where}) with no later recorded outcome; "
            'post the weighed result (for example "Review weighed: ..." or '
            f'"Review of record: <provider> ...") naming Head: {head}; accepted identities: {", ".join(sorted(trusted))}'
        )
    if required_files and not outcome:
        shown = ", ".join(required_files[:3]) + (" ..." if len(required_files) > 3 else "")
        return (
            f"This PR changes review-required paths ({shown}) and has no review-of-record "
            f"outcome naming Head: {head}; accepted identities: {', '.join(sorted(trusted))}. Run the review under model-review and "
            'post the weighed result (for example "Review weighed: ...") before ready'
        )
    return ""


def review_evidence_gap(repo, number, *, base, head, comments, trusted, read_json, read_pages, missing):
    """Read the base branch's review paths and the changed files they govern.

    The base branch's config decides, so a PR cannot drop its own requirement.
    ``missing`` names the caller's not-found error; any other read failure raises.
    """
    try:
        payload = read_json(f"/repos/{repo}/contents/{REVIEW_CONFIG_PATH}?ref={quote(base, safe='')}")
    except Exception as error:
        if not missing(error):
            raise
        payload = None
    patterns = ()
    if payload is not None:
        try:
            config = json.loads(base64.b64decode(payload["content"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"{REVIEW_CONFIG_PATH} on {base} is unreadable") from error
        patterns = review_required_patterns(config)
    files = []
    if patterns:
        inventory = read_pages(f"/repos/{repo}/pulls/{number}/files")
        expected = read_json(f"/repos/{repo}/pulls/{number}")["changed_files"]
        if type(expected) is not int or expected < 0 or len(inventory) != expected:
            raise ValueError("Review path coverage is incomplete: changed-file count does not match inventory")
        files = review_required_files(inventory, patterns)
    return review_of_record_gap(comments, trusted, required_files=files, head=head)
