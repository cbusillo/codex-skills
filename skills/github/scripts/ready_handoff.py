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
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

READY = "Ready for the merge train"
WAITING = "Engineering ready; waiting for"
CLIENT_WAIT = f"{WAITING} the Client's acceptance"
HEAD = re.compile(
    r"(?im)^Head(?: SHA)?:[ \t]*(?P<marker>\*\*|`)?"
    r"(?P<sha>[0-9a-f]{40})(?(marker)(?P=marker))(?:[.\s]|$)"
)
# Support both standalone helpers and the Supervisor's package-style imports.
if __package__:
    from .github_plan_status import client_test_notes as notes_contract
else:
    from github_plan_status import client_test_notes as notes_contract

ENGINEERING_RELEASE_TESTS = notes_contract.ENGINEERING_RELEASE_TESTS
NOTHING_TO_TEST = notes_contract.NOTHING_TO_TEST
ClientTestNotes = notes_contract.ClientTestNotes
INLINE_TEST_NOTES = notes_contract.INLINE_TEST_NOTES
_markdown_lines = notes_contract._markdown_lines
_strip_note_decoration = notes_contract._strip_note_decoration
client_test_notes = notes_contract.client_test_notes
body_client_test_notes = notes_contract.body_client_test_notes
owner_test_notes = notes_contract.owner_test_notes
test_notes_sections = notes_contract.test_notes_sections
MISSING_CLIENT_TEST_NOTES_MESSAGE = (
    'Client test notes are missing; use a "## Client test notes" section '
    'with test instructions or "Nothing for the Client to test: <reason>".'
)
UNCONFIRMED_CLIENT_ACCEPTANCE_MESSAGE = (
    "Client acceptance is not confirmed; use --waiting-for-client, or for "
    'engineering-only changes use "Nothing for the Client to test: <reason>" '
    'under "## Client test notes"'
)
CLIENT_CHANGE = "Client change"
CLIENT_CHANGE_LINE = re.compile(rf"^{CLIENT_CHANGE}:[ \t]*\S")
NO_CLIENT_CHANGE_MESSAGE = (
    f'Client test notes name no Client-visible change; add "{CLIENT_CHANGE}: '
    '<what the Client will see or do differently>", or if nothing changes for '
    f'the Client use "{NOTHING_TO_TEST}: <reason>" and plain ready'
)
BOTH_CLASSES_MESSAGE = (
    f'Client test notes say both "{NOTHING_TO_TEST}" and "{CLIENT_CHANGE}:"; '
    "keep the one that matches the diff"
)
BATCH_MEMBER_HEADING = re.compile(r"^### #[0-9]+")
RELEASE_TESTS_MESSAGE = (
    f'"{ENGINEERING_RELEASE_TESTS}" asks the Client to check engineering work '
    f'at release; use "{NOTHING_TO_TEST}: <reason>", or name the change with '
    f'"{CLIENT_CHANGE}: <...>" and use --waiting-for-client'
)
REVIEW_CONFIG_PATH = ".github/github.json"
# Leads sessions and the Supervisor already write; see model-review Reporting.
REVIEW_LEAD = re.compile(r"^[\s#>*_]+")
# Explicit attestation emitted by gh-pr.py comment --reviewed-head. The
# disposition below it is free text; progress prose is never completion proof.
REVIEW_RESULT = "Review result: weighed"
REVIEW_RECEIPT = re.compile(
    r"\A" + re.escape(REVIEW_RESULT) + r"\nHead: (?P<sha>[0-9a-f]{40})\n\n(?=\S)",
)
REVIEW_REQUEST = re.compile(
    r"(?i)^review (?:prompt|request)\b|^review of record (?:still )?(?:needed|requested)\b"
)
REVIEW_REQUEST_CONTEXT = re.compile(
    r"(?i)\breview of record\b|\banother model\b|\bmodel[- ]review\b"
    r"|\bprovider\s*:\s*`?(?:openai|anthropic|google)\b"
)
# The Supervisor's posted review of record (pending_reviews --apply).
SUPERVISOR_REPORT_HEADING = "Review of record, run by the Supervisor"
SUPERVISOR_RECEIPT = re.compile(r"<!-- supervisor-review (.*?) -->")
SUPERVISOR_RUNS_REVIEW = re.compile(
    r"(?i)^(?:.*?\bso\s+)?supervisor runs the review[.:]?\s*$"
)


def handoff_line(line):
    """Normalize Ready and renewed waits with the same presentation rules."""
    line = line.strip().removesuffix(".")
    if line.startswith("**") and line.endswith("**"):
        line = line[2:-2].removesuffix(".")
    return line


def handoff_body(head_sha: str, *, waiting_for_client: bool = False) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ValueError("handoff requires a full 40-character head SHA")
    first = CLIENT_WAIT if waiting_for_client else READY
    return f"{first}\n\nHead: {head_sha}"


def acceptance_reader():
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from skills.launchplane.scripts import launchplane_acceptance

    return launchplane_acceptance


def client_release(repository):
    return acceptance_reader().read_client_release(repository)


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


def client_test_notes_error(pr) -> str:
    has_review_label = any(
        label["name"].casefold() == "owner-review" for label in pr.get("labels") or []
    )
    body = pr.get("body") or ""
    sections = test_notes_sections(body)
    if sections:
        intent = body_client_test_notes(body, review_override=has_review_label)
        if intent.error:
            if intent.error == "Client test notes are missing.":
                return MISSING_CLIENT_TEST_NOTES_MESSAGE
            return intent.error
        return ""
    if has_inline_client_test_notes(body):
        return MISSING_CLIENT_TEST_NOTES_MESSAGE
    if _is_dependabot_pr(pr) and not has_review_label:
        return ""
    return MISSING_CLIENT_TEST_NOTES_MESSAGE


def _notes_units(body):
    for section in test_notes_sections(body):
        unit = []
        for line, heading, _fence in _markdown_lines(re.sub(r"<!--[\s\S]*?-->", "", section)):
            if heading and BATCH_MEMBER_HEADING.match(line):
                yield from _nonempty("\n".join(unit))
                unit = []
                continue
            unit.append(line)
        yield from _nonempty("\n".join(unit))


def _nonempty(unit):
    return [unit.strip()] if unit.strip() else []


def _unit_lines(unit):
    return [line for line, heading, fence in _markdown_lines(unit) if not fence and not heading]


def _statements(unit):
    return [_strip_note_decoration(line.strip()) for line in _unit_lines(unit) if line.strip() and not _is_example(line)]


def _is_example(line) -> bool:
    return line.startswith(("    ", "\t")) or line.lstrip().startswith(">")


def _names_client_change(unit) -> bool:
    return any(CLIENT_CHANGE_LINE.match(line) for line in _statements(unit))


def _says_nothing_changes(unit) -> bool:
    return any(
        line.casefold().startswith(marker.casefold())
        for line in _statements(unit)
        for marker in notes_contract.NO_TEST_MARKERS
    )


def _unit_class_error(unit) -> str:
    if ENGINEERING_RELEASE_TESTS in _statements(unit):
        return RELEASE_TESTS_MESSAGE
    names_change = _names_client_change(unit)
    if names_change and _says_nothing_changes(unit):
        return BOTH_CLASSES_MESSAGE
    if client_test_notes(unit).review_required and not names_change:
        return NO_CLIENT_CHANGE_MESSAGE
    return ""


def _handoff_class_error(pr, *, waiting_for_client=False) -> str:
    units = tuple(_notes_units(pr.get("body") or ""))
    error = next((error for error in map(_unit_class_error, units) if error), "")
    if error:
        return error
    if waiting_for_client and not any(map(_names_client_change, units)):
        return NO_CLIENT_CHANGE_MESSAGE
    return ""


class ClientHandoff(NamedTuple):
    error: str = ""
    acceptance_required: bool = False
    unknown_reason: str = ""

    def refusal(self, *, accepted) -> str:
        message = self.error or (
            UNCONFIRMED_CLIENT_ACCEPTANCE_MESSAGE if self.acceptance_required and not accepted else ""
        )
        if message and self.unknown_reason:
            return f"{message.rstrip('.')}. {self.unknown_reason}."
        return message


def explicit_client_hold(pr, check_runs) -> bool:
    if any(label["name"].casefold() == "owner-review" for label in pr.get("labels") or []):
        return True
    engineering = bool(test_notes_sections(pr.get("body") or "")) and _client_path(
        pr, held=False, waiting_for_client=False,
    ) == ClientHandoff()
    return any(
        check["name"] == "launchplane/owner-review"
        and not (check.get("status") == "completed" and check.get("conclusion") == "neutral")
        and not (check.get("status") != "completed" and engineering)
        for check in check_runs
    )


def _client_path(pr, *, held, waiting_for_client) -> ClientHandoff:
    error = client_test_notes_error(pr) or _handoff_class_error(pr, waiting_for_client=waiting_for_client)
    body = pr.get("body") or ""
    if held or not test_notes_sections(body):
        return ClientHandoff(error, held)
    intent = body_client_test_notes(body)
    return ClientHandoff(error, bool(intent.error or intent.review_required))


def client_handoff(pr, check_runs, *, repository, waiting_for_client=False) -> ClientHandoff:
    held = explicit_client_hold(pr, check_runs)
    strict = _client_path(pr, held=held, waiting_for_client=waiting_for_client)
    if held or waiting_for_client or strict == ClientHandoff():
        return strict
    release, states = client_release(repository), acceptance_reader()
    if release.state == states.NO_CLIENT:
        return ClientHandoff()
    if release.state == states.DIRECTOR_STANDING:
        return ClientHandoff(client_test_notes_error(pr))
    if release.state == states.CLIENT:
        return strict
    return ClientHandoff(
        strict.error,
        strict.acceptance_required,
        f"Launchplane could not confirm whether {repository} has a Client ({release.reason}), "
        "so this PR stays on the Client path",
    )


def pending_client_reviews(check_runs: list[dict[str, object]], handoff: ClientHandoff, *, waiting_for_client: bool = False) -> int:
    if not waiting_for_client and handoff != ClientHandoff():
        return 0
    return sum(
        check.get("name") == "launchplane/owner-review" and check.get("status") != "completed"
        for check in check_runs
    )


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return parsed.timestamp()


def latest_ready(pr, comments, trusted, *, include_client_wait: bool = False) -> bool:
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
    first = handoff_line(body.splitlines()[0])
    return (first == READY or include_client_wait and first == CLIENT_WAIT) and [
        match.group("sha") for match in HEAD.finditer(body)
    ] == [pr["head"]["sha"]]


def review_result_body(body, head):
    """Record the author's weighed disposition for exactly one reviewed head."""
    if not re.fullmatch(r"[0-9a-f]{40}", head or ""):
        raise ValueError("Review result requires a full 40-character reviewed head SHA")
    if not body or not body.strip():
        raise ValueError("Review result requires a disposition body")
    return f"{REVIEW_RESULT}\nHead: {head}\n\n{body.strip()}"


def review_receipt_head(body):
    match = REVIEW_RECEIPT.match(body or "")
    return match.group("sha") if match else None


def review_prose_lines(body):
    """Review directives in examples, quotes and indented code are not actions."""
    fence = None
    for line in re.sub(r"<!--.*?-->", "", body or "", flags=re.S).splitlines():
        if line.lstrip().startswith(">"):
            continue
        if fence:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", line):
                fence = None
            continue
        opening = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if opening:
            fence = opening[1]
            continue
        if not line.startswith(("    ", "\t")):
            yield line.strip().replace("**", "")


def supervisor_review_request(body):
    return any(SUPERVISOR_RUNS_REVIEW.fullmatch(line) for line in review_prose_lines(body))


def raw_receipt(body):
    # gh-pr appends a transport marker. Only the raw report's final marker
    # is a receipt; source snippets inside the review remain ordinary data.
    # Imported here, in either import style, so importers need no path setup.
    try:
        from github_plan_claim import without_operation_marker
    except ModuleNotFoundError:
        from skills.github.scripts.github_plan_claim import without_operation_marker
    lines = without_operation_marker(body).strip().splitlines()
    match = SUPERVISOR_RECEIPT.fullmatch(lines[-1]) if lines else None
    if match:
        try:
            value = json.loads(match[1])
        except json.JSONDecodeError:
            return None
        if (isinstance(value, dict) and type(value.get("request")) is int
                and isinstance(value.get("head"), str) and re.fullmatch(r"[0-9a-f]{40}", value["head"])):
            return value
    return None


def reported_review(body):
    """A Supervisor review of record leads with its heading; a receipt alone is not one."""
    first = body.strip().splitlines()[0].replace("**", "") if body.strip() else ""
    return (re.match(r"(?i)^Review of record[:,].*\brun by the Supervisor\b", first) is not None
            and not re.search(r"(?i)\b(?:pending|awaiting|waiting|not run)\b", first))


def review_record(body):
    """Read explicit completion evidence; prose leads only detect requests."""
    if review_receipt_head(body):
        return "outcome"
    lines = (body or "").strip().splitlines()
    first = REVIEW_LEAD.sub("", lines[0]) if lines else ""
    explicit_request = REVIEW_REQUEST.search(first) and REVIEW_REQUEST_CONTEXT.search(first)
    supervisor_request = supervisor_review_request(body)
    if explicit_request or supervisor_request:
        return "request"
    return None


def review_required_patterns(config):
    """Read qualityGate.modelReview.requiredPaths from the base branch's config."""
    if not isinstance(config, dict):
        raise ValueError("Review configuration must be an object")
    gate = config.get("qualityGate", {})
    if not isinstance(gate, dict):
        raise ValueError("qualityGate must be an object")
    review = gate.get("modelReview", {})
    if not isinstance(review, dict):
        raise ValueError("qualityGate.modelReview must be an object")
    value = review.get("requiredPaths", [])
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
        if kind == "outcome" and head and review_receipt_head(comment.get("body")) != head:
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
            f"post the disposition with gh-pr.py comment --reviewed-head {head}; "
            f"accepted identities: {', '.join(sorted(trusted))}"
        )
    if required_files and not outcome:
        shown = ", ".join(required_files[:3]) + (" ..." if len(required_files) > 3 else "")
        return (
            f"This PR changes review-required paths ({shown}) and has no review-of-record "
            f"weighed outcome for {head}; post the disposition with gh-pr.py comment "
            f"--reviewed-head {head}; accepted identities: {', '.join(sorted(trusted))}"
        )
    return ""


def review_evidence_gap(repo, number, *, base, head, comments, trusted, read_json, read_pages, missing, file_inventory=None):
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
        inventory = file_inventory if file_inventory is not None else read_pages(f"/repos/{repo}/pulls/{number}/files")
        expected = read_json(f"/repos/{repo}/pulls/{number}")["changed_files"]
        if type(expected) is not int or expected < 0 or len(inventory) != expected:
            raise ValueError("Review path coverage is incomplete: changed-file count does not match inventory")
        files = review_required_files(inventory, patterns)
    return review_of_record_gap(comments, trusted, required_files=files, head=head)
