#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
from __future__ import annotations

import argparse
import base64
import datetime as dt
import fnmatch
import importlib
import json
import math
import os
import pathlib
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from typing import Any, Callable, NamedTuple
from io import BytesIO, UnsupportedOperation
from urllib.parse import quote

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
GITHUB_SCRIPTS = REPO_ROOT / "skills" / "github" / "scripts"
if str(GITHUB_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(GITHUB_SCRIPTS))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skills.github.scripts import director_question_gate, github_identity, github_rulesets, github_direction_next, github_plan_claim, github_plan_wait, github_read, github_work_graph, github_next_reads
from skills.github.scripts import github_repository_scope as repository_scope
from skills.github.scripts.github_plan_status import questions as github_plan_questions
from skills.github.scripts.github_plan_status.discussion import status_field
from skills.direction.scripts import direction_mark, direction_reporting
from skills.github.scripts import github_api, github_plan_closure, github_plan_release
from skills.supervisor.scripts import session_outcomes

REQUIRED_HEADINGS = ("Purpose", "Stop Boundaries", "Journey", "Retired", "Milestones")
ESCALATION_LABEL = "direction"
AUDIT_LABEL = "audit"
MILESTONE_LINE = re.compile(r"^\s*[-*]\s+`([^`]+)`")
HEADING = re.compile(r"^##\s+(.+?)\s*$")

# Text that turns reviewer output into a gate. Each pattern needs a realistic
# sentence that produces it; see the tests for the sentences that motivated them.
GATE_PHRASES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("both reviewers approve", re.compile(r"\bboth\s+(?:reviewers|models)\s+(?:must\s+)?approv", re.I)),
    ("all findings resolved", re.compile(r"\b(?:(?:all|every|blocking|planning|final|review)\s+)*findings?\s+(?:(?:are|is|be|were|remain)\s+)?(?:resolved|addressed)\b", re.I)),
    ("named reviewer sign-off", re.compile(r"\b(?:opus|gemini|fable|sol|astra)\b[^.\n]{0,60}\b(?:sign-?off|approv(?:e|al|es|ed))", re.I)),
)

WRAPPER = pathlib.Path(__file__).resolve().parents[2] / "github" / "scripts" / "gh-with-env-token"
MAX_ADMISSION_ISSUES = 50
MAX_INBOUND_ISSUES = 50
COMMIT_FILES_PER_RESPONSE = 300
GROWTH_FLOOR_LINES = 2000
GROWTH_SHARE = 0.02
LARGEST_CONTRIBUTORS = 5
WAIT_REFERENCE = re.compile(
    r"https://github\.com/([\w.-]+/[\w.-]+)/(issues|pull)/(\d+)"
    r"|(?<![\w/])([\w.-]+/[\w.-]+)?#(\d+)\b"
)

STATUS_FIELD = r"(?:State|Next action|Blocked by|Waiting for|Parked until|Waiting since|Last verified|Validation|Evidence|Retention|Recovery|Worker|Session|Branch)"
STALE_STATUS_DAYS = 7
DRIFT_LINKED_LIMIT = 5
CITED_QUESTION = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+#issuecomment-\d+|\bQ[1-9]\d*\b")
IMPOSSIBLE_CONDITION = re.compile(r"\b(?:was|were) not (?:kept|retained|preserved|saved)\b|\bno longer exists?\b|\bcan(?:not|'t) (?:occur|happen|be (?:produced|recovered))\b|\bcould never\b", re.I)
DRIFT_COMMON_TERMS = {"that", "this", "with", "from", "when", "then", "will", "have", "been", "agent", "lead", "issue", "event", "only", "after", "wait"}

# The overall direction owns classification; repositories can contain mixed work.
RANKS = ("milestone", "tooling", "own")
ADMISSION_RANKS = {
    "live-breakage": "milestone", "client-request": "milestone", "milestone": "milestone",
    "own-project": "own", "repeat-stop": "tooling", "spare-capacity": "tooling",
}
# GitHub's revert button titles the pull request `Revert "..."` and writes
# `Reverts OWNER/REPO#N`; `git revert` writes `This reverts commit <sha>`.
REVERT_TITLE = re.compile(r"^\s*revert\b", re.I)
REVERT_BODY = re.compile(r"^\s*Reverts [\w.-]+/[\w.-]+#\d+|\bThis reverts commit [0-9a-f]{7,40}\b", re.I | re.M)


class DriftReport(NamedTuple):
    evidence: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    asks: list[dict[str, Any]]


class AuditError(Exception):
    pass


class ReferenceNotFound(AuditError):
    """GitHub answered 404: the record is absent, so it was read, not unread."""


def parse_direction(text: str) -> dict[str, Any]:
    """Return headings and owner-approved milestone lines."""
    headings: list[str] = []
    milestones: list[str] = []
    milestone_lines: dict[str, str] = {}
    current: str | None = None
    current_milestone: str | None = None
    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            name = heading.group(1)
            current = name
            current_milestone = None
            headings.append(name)
            continue
        if current == "Milestones":
            item = MILESTONE_LINE.match(line)
            if item:
                current_milestone = item.group(1).strip()
                milestones.append(current_milestone)
                milestone_lines[current_milestone] = line.strip()
            elif current_milestone and line[:1].isspace() and line.strip():
                milestone_lines[current_milestone] += " " + line.strip()
    missing = [name for name in REQUIRED_HEADINGS if name not in headings]
    return {"headings": headings, "missing_headings": missing, "milestones": milestones, "milestone_lines": milestone_lines}


def milestone_addition(events: list[dict[str, Any]]) -> dict[str, str] | None:
    """Who made the last milestone assignment and when, including across milestone renames."""
    additions = [event for event in events if event.get("event") == "milestoned"]
    if not additions:
        return None
    latest = max(enumerate(additions), key=lambda pair: (str(pair[1].get("created_at") or ""), pair[0]))[1]
    return {"by": str(((latest.get("actor") or {}).get("login")) or "").lower(), "at": str(latest.get("created_at") or "")}


def gate_phrases(text: str) -> list[str]:
    return [name for name, pattern in GATE_PHRASES if pattern.search(text or "")]


def audit(
    *,
    direction_text: str | None,
    milestones: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    owner: str,
    automation: str | None,
    now: dt.datetime,
    bot_logins: tuple[str, ...] = (),
    expected_automation: str | None = None,
    owner_identity_explicit: bool = True,
    direction_pulls: list[dict[str, Any]] | None = None,
    truncated: list[str] | None = None,
    rulesets: list[dict[str, Any]] | None = None,
    rulesets_unavailable: bool = False,
    audit_since: dt.datetime | None = None,
    capacity: dict[str, Any] | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    wait_findings: dict[tuple[str, int], dict[str, Any]] = {}
    limits: list[dict[str, Any]] = []
    milestone_additions: list[dict[str, Any]] = []
    audit_since = audit_since or now - dt.timedelta(days=7)
    unread_waits = [f"milestone_waits:{item['repo']}#{item['number']}" for item in issues if item.get("_milestone_wait_unread")]
    if truncated or unread_waits:
        findings.append({"kind": "coverage_incomplete", "detail": "a bounded read was truncated or unavailable; drift beyond verified coverage is unreported", "listings": sorted((truncated or []) + unread_waits)})
    if direction_text is None:
        findings.append({"kind": "direction_missing", "detail": "DIRECTION.md not found at the repository root"})
        listed: list[str] = []
        milestone_lines: dict[str, str] = {}
    else:
        parsed = parse_direction(direction_text)
        listed = parsed["milestones"]
        milestone_lines = parsed["milestone_lines"]
        if parsed["missing_headings"]:
            findings.append({"kind": "direction_shape", "detail": "missing headings", "headings": parsed["missing_headings"]})
        requires_rulesets = not github_rulesets.is_upstream_contribution_fork(repo or "")
        if requires_rulesets and rulesets_unavailable:
            findings.append({
                "kind": "ruleset_unavailable",
                "detail": "GitHub's plan for this private repository does not offer rulesets; "
                          "nothing enforces owner review of DIRECTION.md",
            })
        elif requires_rulesets and rulesets is not None:
            for name in github_rulesets.missing_standard_rulesets(rulesets):
                findings.append({
                    "kind": "ruleset_missing",
                    "name": name,
                    "detail": "an active repository branch ruleset with this standard name was not found",
                })

    # The owner's automation can span identities, such as a bot user and a later App.
    bots = {login.lower() for login in (automation, *bot_logins) if login} - {owner.lower()}
    trusted = {owner.lower()} | bots
    if (direction_text is not None and listed and automation
            and automation.casefold() == owner.casefold()
            and (not owner_identity_explicit or (expected_automation
                 and expected_automation.casefold() != owner.casefold()))):
        findings.append({
            "kind": "coverage_incomplete",
            "detail": "the audit reader returned the owner without an explicit owner reader, "
                      "or instead of the configured automation login",
            "listings": ["milestone_admission_identity"],
        })
    elif direction_text is not None and listed and automation and automation.casefold() == owner.casefold():
        limits.append({
            "kind": "owner_acts_as_automation",
            "detail": "Owner and automation use the same login; milestone additions by that login "
                      "cannot be told apart and are treated as owner decisions, so they are not listed.",
        })
    elif direction_text is not None and listed and not bots:
        findings.append({
            "kind": "coverage_incomplete",
            "detail": "automation identity is indistinguishable from the owner; milestone additions cannot be attributed",
            "listings": ["milestone_admission_identity"],
        })
    open_titles: set[str] = set()
    closed_titles: dict[str, Any] = {}
    for milestone in milestones:
        title = str(milestone.get("title") or "")
        state = milestone.get("state")
        creator = str(((milestone.get("creator") or {}).get("login")) or "")
        if state == "open":
            open_titles.add(title)
            if direction_text is not None and title not in listed:
                findings.append({"kind": "milestone_unlisted", "title": title, "number": milestone.get("number")})
        else:
            closed_titles[title] = milestone.get("number")
        if state == "open" and creator and creator.lower() not in trusted:
            findings.append({"kind": "milestone_creator", "title": title, "number": milestone.get("number"), "creator": creator})
        if state == "open":
            phrases = gate_phrases(str(milestone.get("description") or ""))
            if phrases:
                findings.append({"kind": "gate_phrase", "milestone": milestone.get("number"), "title": title, "phrases": phrases})
    for title in listed:
        if title in open_titles:
            continue
        if title in closed_titles:
            # Shipped and closed, but the file still lists it: remove the line, never recreate it.
            findings.append({"kind": "milestone_closed_listed", "title": title, "number": closed_titles[title]})
        else:
            findings.append({"kind": "milestone_pending", "title": title})

    for pull in direction_pulls or []:
        opened = _parse_time(pull.get("created_at"))
        findings.append({
            "kind": "escalation_open",
            "number": pull.get("number"),
            "title": pull.get("title"),
            "pull_request": True,
            "age_days": (now - opened).days if opened else None,
        })

    for issue in issues:
        if "pull_request" in issue:
            continue
        labels = {str(label.get("name", "")).lower() for label in issue.get("labels") or []}
        number = issue.get("number")
        state = issue.get("state", "open")
        if parked_issue(issue, str(issue.get("repo") or repo or "")) and issue.get("_blocking_work_elsewhere"):
            findings.append({
                "kind": "waiting_blocks_other_repository", "number": number,
                "title": issue.get("title"), "blocking": issue["_blocking_work_elsewhere"],
            })
        if AUDIT_LABEL in labels:
            if state == "open":
                findings.append({"kind": "audit_question", "number": number, "title": issue.get("title")})
            elif state == "closed":
                closed = _parse_time(issue.get("closed_at"))
                # GitHub's `since` filters updates, not closures. An old issue
                # edited recently must not be judged again at every audit.
                if closed and audit_since <= closed <= now:
                    findings.append({"kind": "audit_judge", "number": number, "title": issue.get("title"), "closed_at": issue.get("closed_at")})
        milestone_title = str(((issue.get("milestone") or {}).get("title")) or "")
        for finding in issue.get("_milestone_wait_findings", []):
            wait_findings[(finding["repo"].casefold(), finding["number"])] = finding
        if (state == "open" and milestone_title in listed
                and not str(issue.get("title", "")).startswith("Track:")
                and not issue.get("_milestone_wait_unread")):
            compact = github_direction_next.compact_list_issue(str(issue.get("repo") or owner), issue)
            compact["plan_status"] = issue.get("_plan_status")
            if compact["plan_status"] == "waiting":
                compact["exclusion"] = "waiting"
            if issue.get("structured_wait") is not None:
                if issue["structured_wait"]["state"] == "review":
                    compact["exclusion"] = "wait_review_required"
                else:
                    compact["structured_wait"] = issue["structured_wait"]
            status = github_direction_next.section_map(issue.get("body") or "").get("Current Status", "")
            checked = github_direction_next.check_milestone_wait(compact, status, listed)
            if checked.get("wait_finding"):
                finding = checked["wait_finding"]
                wait_findings[(finding["repo"].casefold(), finding["number"])] = finding
        added = issue.get("_milestone_added")
        added_at = _parse_time((added or {}).get("at"))
        if (added and milestone_title in milestone_lines and added["by"] != owner.lower()
                and added_at and audit_since <= added_at <= now):
            # Information for the direction session to read for fit, not a finding.
            milestone_additions.append({
                "repo": repo, "number": number, "title": issue.get("title"),
                "milestone": milestone_title, "added_by": added["by"] or None, "added_at": added["at"],
            })
        if ESCALATION_LABEL in labels and issue.get("state", "open") == "open":
            opened = _parse_time(issue.get("created_at"))
            age_days = (now - opened).days if opened else None
            findings.append({"kind": "escalation_open", "number": number, "title": issue.get("title"), "age_days": age_days})
        text = f"{issue.get('title') or ''}\n{issue.get('body') or ''}"
        phrases = gate_phrases(text)
        if phrases and issue.get("state", "open") == "open":
            findings.append({"kind": "gate_phrase", "number": number, "title": issue.get("title"), "phrases": phrases})

    findings.extend(wait_findings.values())

    order = {
        "coverage_incomplete": -1,
        "direction_missing": 0,
        "direction_shape": 1,
        "ruleset_missing": 2,
        "ruleset_unavailable": 2,
        "escalation_open": 3,
        "audit_question": 3,
        "audit_judge": 3,
        "milestone_unlisted": 4,
        "milestone_closed_listed": 5,
        "milestone_pending": 6,
        "milestone_creator": 7,
        "gate_phrase": 9,
        "waiting_blocks_other_repository": 8,
        "milestone_wait_invalid": 8,
    }
    findings.sort(key=lambda item: (order.get(item["kind"], 99), str(item.get("number") or item.get("milestone") or item.get("title") or "")))
    result = {
        "ok": not findings,
        "listed_milestones": listed,
        "open_milestones": sorted(open_titles),
        "findings": findings,
        "limits": limits,
        "counts": _counts(findings),
        "milestone_additions": sorted(milestone_additions, key=lambda item: (item["added_at"], str(item["number"]))),
    }
    if capacity is not None:
        result["capacity"] = capacity
    return result


def _counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in findings:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    return counts


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


NOT_FOUND_RE = re.compile(r"\bHTTP 404\b")


def gh_json(args: list[str], *, gh: str) -> Any:
    proc = subprocess.run([gh, *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        error = ReferenceNotFound if NOT_FOUND_RE.search(proc.stderr) else AuditError
        raise error(f"{gh} {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise AuditError(f"non-JSON output from {gh} {' '.join(args[:3])}") from exc


# A bare #N names the issue's own repository by default; prose often means another one.
UNRESOLVED = object()


def bare_reference_missing(exc: AuditError, explicit: bool) -> bool:
    """Only an unqualified reference may be left unresolved; named repositories stay errors."""
    return not explicit and isinstance(exc, ReferenceNotFound)


# GitHub Free refuses rulesets on private repositories with this 403; other 403s stay errors.
RULESET_PLAN_LIMIT_RE = re.compile(r"\bHTTP 403\b.*Upgrade to GitHub (?:Pro|Team)|Upgrade to GitHub (?:Pro|Team).*\bHTTP 403\b", re.S)


def fetch_rulesets(repo: str, *, fetch: Callable[[list[str]], Any]) -> tuple[list[dict[str, Any]] | None, bool]:
    """Branch rulesets and whether the page cap cut them short; None when the plan lacks rulesets."""
    try:
        return fetch_paginated(f"repos/{repo}/rulesets?includes_parents=false&targets=branch", fetch=fetch)
    except AuditError as exc:
        if RULESET_PLAN_LIMIT_RE.search(str(exc)):
            return None, False
        raise


MAX_PAGES = 20


def fetch_paginated(
    path: str, *, fetch: Callable[[list[str]], Any], max_pages: int = MAX_PAGES,
    key: str | None = None, stop: Callable[[dict[str, Any]], bool] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """All pages of a listing, and whether the page cap cut it short.

    `key` names the list inside an object response. `stop` ends the read after
    a page holding an item it matches, for listings sorted newest first.
    """
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        joiner = "&" if "?" in path else "?"
        body = fetch(["api", f"{path}{joiner}per_page=100&page={page}", "--method", "GET"])
        if key is not None and isinstance(body, dict):
            body = body.get(key)
        if not isinstance(body, list):
            raise AuditError(f"unexpected response shape for {path}")
        items.extend(body)
        if len(body) < 100 or (stop and any(stop(item) for item in body)):
            return items, False
        if page >= max_pages:
            return items, True
        page += 1


def enrich_admission_actors(
    issues: list[dict[str, Any]], milestone_lines: dict[str, str], repo: str,
    *, fetch: Callable[[list[str]], Any], since: dt.datetime, max_issues: int = MAX_ADMISSION_ISSUES,
) -> bool:
    """Add who last assigned each listed milestone, within a bounded event-read budget."""
    incomplete = False
    examined = 0
    recent_first = sorted(issues, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    for issue in recent_first:
        title = ((issue.get("milestone") or {}).get("title"))
        if "pull_request" in issue or title not in milestone_lines:
            continue
        # Assigning a milestone updates the issue, so an older issue was not added since.
        updated = _parse_time(issue.get("updated_at") or issue.get("created_at"))
        if updated is not None and updated < since:
            continue
        if examined >= max_issues:
            issue["_admission_unknown"] = True
            incomplete = True
            continue
        examined += 1
        events, cut = fetch_paginated(f"repos/{repo}/issues/{issue.get('number')}/events", fetch=fetch, max_pages=2)
        if cut:
            issue["_admission_unknown"] = True
            incomplete = True
            continue
        issue["_milestone_added"] = milestone_addition(events)
    return incomplete


def merged_direction(repo: str, *, fetch: Callable[[list[str]], Any]) -> str | None:
    """DIRECTION.md from the default branch, or None when the repository has none."""
    return merged_file(repo, "DIRECTION.md", fetch=fetch)


def merged_file(repo: str, path: str, *, fetch: Callable[[list[str]], Any]) -> str | None:
    """A file from the default branch, or None when it does not exist there."""
    import base64

    try:
        body = fetch(["api", f"repos/{repo}/contents/{path}", "--method", "GET"])
    except AuditError as exc:
        if re.search(r"\bHTTP 404\b", str(exc)):
            return None
        raise
    if isinstance(body, dict) and isinstance(body.get("content"), str):
        return base64.b64decode(body["content"]).decode()
    return None


def direction_pull_requests(repo: str, pulls: list[dict[str, Any]], *, fetch: Callable[[list[str]], Any]) -> list[dict[str, Any]]:
    """Open pull requests that carry the escalation label or change DIRECTION.md."""
    matched: list[dict[str, Any]] = []
    for pull in pulls:
        labels = {str(label.get("name", "")).lower() for label in pull.get("labels") or []}
        if ESCALATION_LABEL in labels:
            matched.append(pull)
            continue
        files = fetch(["api", f"repos/{repo}/pulls/{pull.get('number')}/files?per_page=100", "--method", "GET"])
        if isinstance(files, list) and any(str(item.get("filename")) == "DIRECTION.md" for item in files):
            matched.append(pull)
    return matched


def default_repo(root: pathlib.Path) -> str | None:
    proc = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode != 0:
        return None
    try:
        return github_read.repo_from_remote_url(proc.stdout.strip())
    except ValueError:
        return None


def git_root(start: pathlib.Path) -> pathlib.Path:
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return pathlib.Path(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else start


def record_audit(repo: str, started_at: dt.datetime, direction_text: str | None, held: str | None = None,
                 since: dt.datetime | None = None) -> str | None:
    """Stamp this repository's audit in the local marker the session-start hook reads.

    The stamp is written here, not by hand, so an audit stamp means an audit ran.
    A held audit records that it ran and why, without moving its window start.
    GitHub is untouched; the marker is local state under the catalog home.
    """
    if direction_text is None:
        return None
    path: pathlib.Path | str = os.environ.get("DIRECTION_MARKER") or "default direction marker"
    try:
        path = direction_mark.marker_path()
        # Retain closures that happen while listings are being fetched for the
        # next audit instead of skipping ahead to the completion time.
        direction_mark.mark_audit(path, repo, started_at, held, since)
        return str(path)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise AuditError(f"Could not record direction audit marker at {path}: {exc}") from exc


def drop_archived(repo: str) -> str | None:
    """Remove a repository this audit confirmed archived from the local marker."""
    path: pathlib.Path | str = os.environ.get("DIRECTION_MARKER") or "default direction marker"
    try:
        path = direction_mark.marker_path()
        direction_mark.drop_audit(path, repo)
        return str(path)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise AuditError(f"Could not remove archived direction audit marker at {path}: {exc}") from exc


def previous_audit_stamp(repo: str) -> dt.datetime | None:
    """Read the previous audit marker without changing it."""
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("direction_mark", pathlib.Path(__file__).with_name("direction_mark.py"))
        if spec is None or spec.loader is None:
            return None
        mark = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mark)
        audits = mark.load(mark.marker_path()).get("audits", {})
        return _parse_time(audits.get(repo)) if isinstance(audits, dict) else None
    except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError):
        return None


def prune_unadopted(
    path: pathlib.Path, *, fetch: Callable[[list[str]], Any], apply: bool = False,
    remove_missing_repos: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Preview archived or unadopted entries; preserve unreadable repositories."""
    original = path.read_bytes()
    current = json.loads(original)
    if not isinstance(current, dict) or not isinstance(current.get("audits"), dict):
        raise AuditError("marker must contain an audits object; no changes made")
    removed: list[str] = []
    removed_reasons: dict[str, str] = {}
    retained: list[str] = []
    unknown: dict[str, str] = {}
    runs = current.get("audit_runs") if isinstance(current.get("audit_runs"), dict) else {}
    # A first audit that held its window leaves an entry only in audit_runs.
    for repo in dict.fromkeys([*current["audits"], *runs]):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            unknown[repo] = "invalid repository name"
            continue
        try:
            # Contents 404 alone can also mean an inaccessible private repo.
            try:
                visible = fetch(["api", f"repos/{repo}", "--method", "GET"])
            except AuditError as exc:
                # A 404 does not prove deletion, even with the owner's reader.
                # Only an exact, independently owner-approved name may override it.
                if repo in remove_missing_repos and re.search(r"\bHTTP 404\b", str(exc)):
                    removed.append(repo)
                    removed_reasons[repo] = "owner_approved_missing_repository"
                    continue
                raise
            if not isinstance(visible, dict) or str(visible.get("full_name", "")).casefold() != repo.casefold():
                raise AuditError("repository visibility could not be confirmed")
            if not isinstance(visible.get("archived"), bool):
                raise AuditError("repository archive state could not be confirmed")
            if visible["archived"]:
                removed.append(repo)
                removed_reasons[repo] = "archived"
                continue
            try:
                body = fetch(["api", f"repos/{repo}/contents/DIRECTION.md", "--method", "GET"])
            except AuditError as exc:
                if not re.search(r"\bHTTP 404\b", str(exc)):
                    raise
                removed.append(repo)
                removed_reasons[repo] = "missing_direction_file"
            else:
                if not isinstance(body, dict) or body.get("type") != "file" or not isinstance(body.get("content"), str):
                    raise AuditError("direction response is not a readable file")
                retained.append(repo)
        except AuditError as exc:
            unknown[repo] = str(exc)
    backup = None
    if apply and removed:
        import tempfile

        with direction_mark.marker_lock(path) as path:
            if path.read_bytes() != original:
                raise AuditError("marker changed during preview; rerun before applying")
            descriptor, backup_name = tempfile.mkstemp(prefix=path.name + ".backup-", dir=path.parent)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
            backup = backup_name
            for repo in removed:
                current["audits"].pop(repo, None)
                runs.pop(repo, None)
            direction_mark.save(path, current)
    return {"ok": not unknown, "applied": apply and bool(removed), "removed": removed,
            "removed_reasons": removed_reasons,
            "retained": retained, "unknown": unknown, "backup": backup}


def enrich_waiting_inbound_blockers(
    issues: list[dict[str, Any]], repo: str, *, fetch: Callable[[list[str]], Any],
    max_issues: int = MAX_INBOUND_ISSUES,
) -> bool:
    """Read native inbound dependencies for waiting issues, including unmilestoned ones."""
    incomplete = False
    examined = 0
    for issue in issues:
        if ("pull_request" in issue or issue.get("state", "open") != "open"
                or not parked_issue(issue, repo)
                or (type((issue.get("issue_dependencies_summary") or {}).get("blocking")) is int
                    and issue["issue_dependencies_summary"]["blocking"] == 0)):
            continue
        if examined >= max_issues:
            incomplete = True
            continue
        examined += 1
        try:
            targets, cut = fetch_paginated(
                f"repos/{repo}/issues/{issue['number']}/dependencies/blocking", fetch=fetch, max_pages=2,
            )
            incomplete = incomplete or cut
            blocking = []
            source_url = str(issue.get("html_url") or issue.get("url") or "")
            source = re.search(r"github\.com/(?:repos/)?([^/]+/[^/]+)/issues/[0-9]+", source_url)
            source_repo = source.group(1) if source else repo
            for target in targets:
                if target.get("state") == "closed" or "pull_request" in target:
                    continue
                url = str(target.get("html_url") or target.get("url") or "")
                match = re.search(r"github\.com/(?:repos/)?([^/]+/[^/]+)/issues/([0-9]+)", url)
                if not match or target.get("state") != "open":
                    raise AuditError("ambiguous blocking issue repository or state")
                target_repo = match.group(1)
                if target_repo.casefold() != source_repo.casefold():
                    blocking.append({"repo": target_repo, "number": int(match.group(2)),
                                     "url": f"https://github.com/{target_repo}/issues/{match.group(2)}"})
            issue["_blocking_work_elsewhere"] = blocking
        except AuditError:
            incomplete = True
    return incomplete


def current_audit_wait(issue: dict[str, Any], repo: str, *, fetch: Callable[[list[str]], Any]) -> dict[str, Any] | None:
    supplied: dict[str, Any] | None = issue.get("structured_wait")
    if "structured_wait" in issue:
        if supplied is None:
            return None
        github_plan_wait.validate(supplied)
        return github_plan_wait.project_dates(supplied)

    def comments(_limit: int | None) -> tuple[list[dict[str, Any]], bool]:
        rows, cut = fetch_paginated(f"repos/{repo}/issues/{issue['number']}/comments", fetch=fetch, max_pages=2)
        return rows, not cut

    discussion, complete = github_work_graph.read_discussion(issue, comments, limit=200)
    if not complete:
        raise AuditError("Issue wait discussion is incomplete")
    record, recorded = github_plan_wait.resolve(issue, discussion, github_plan_wait.authors(repo))
    return github_plan_wait.project_dates(record) if recorded else None


def parked_issue(issue: dict[str, Any], repo: str) -> bool:
    if "pull_request" in issue or issue.get("state", "open") != "open":
        return False
    wait = issue.get("structured_wait")
    if isinstance(wait, dict) and wait["state"] != "review":
        return wait["state"] != "clear"
    status = github_direction_next.section_map(issue.get("body") or "").get("Current Status", "")
    return github_direction_next.milestone_wait_evidence(
        github_direction_next.compact_list_issue(repo, issue), status, [])["requested"]


def closed_wait_prerequisite(target: dict[str, Any]) -> bool:
    """A closure is review evidence, not proof that a split's remainder ran."""
    if target.get("state") != "closed" or target.get("state_reason") not in {None, "completed"}:
        return False
    summary = target.get("sub_issues_summary") or {}
    if isinstance(summary.get("total"), int) and isinstance(summary.get("completed"), int):
        if summary["completed"] < summary["total"]:
            return False
    return not re.search(r"(?im)^\s*(?:[-*]\s+)?State:\s*Split\b", str(target.get("body") or ""))


def agent_delivery_wait(status: str, repo: str, number: int) -> str | None:
    """Recognize an agent's delivery step, not a continued person/event wait."""
    match = re.search(
        rf"(?ims)^\s*(?:[-*]\s+)?Waiting for:\s*(.*?)(?=^\s*(?:[-*]\s+)?{STATUS_FIELD}:|\Z)", status,
    )
    if not match:
        return None
    reason = match[1].strip()
    # A delegated acceptance belongs to its named issue, not this closeout.
    plain = WAIT_REFERENCE.sub("REF", reason)
    plain = re.sub(r"\[([^]]+)]\(REF\)", "REF", plain)
    agent = r"(?:the |a |an )?(?:next )?(?:Supervisor|agent)"
    step = r"(?:train |delivery )?(?:routing|landing|close[ -]?out)(?:/landing| and confirmed landing)?"
    delegated = r"; [\w '-]+ acceptance belongs to REF\. No new Director decision is open"
    if re.fullmatch(rf"{agent} {step}(?:{delegated})?", plain.rstrip(" ."), re.I):
        if ";" in plain and any(
            ref.group(2) == "pull" or (
                (ref.group(1) or ref.group(4) or repo).casefold() == repo.casefold()
                and int(ref.group(3) or ref.group(5)) == number
            ) for ref in WAIT_REFERENCE.finditer(reason)
        ):
            return None
        return reason
    return None


def active_merged_status(issue: dict[str, Any], repo: str, status: str, *,
                         read: Callable[[str], Any],
                         unresolved: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """A merge makes an old status suspect, never proves the finish line."""
    # Normalize field emphasis without rewriting reference URLs or body text.
    status = re.sub(rf"(?m)^(\s*(?:[-*]\s+)?)(?:\*\*|__)({STATUS_FIELD})(?::(?:\*\*|__)|(?:\*\*|__):)", r"\1\2:", status)
    next_action = re.search(
        rf"(?ims)^\s*(?:[-*]\s+)?Next action:\s*(.*?)(?=^\s*(?:[-*]\s+)?{STATUS_FIELD}:|\Z)", status,
    )
    explicit: set[tuple[str, int]] = set()

    def references(reference_text: str) -> dict[tuple[str, int], str | None]:
        found: dict[tuple[str, int], str | None] = {}
        for ref in WAIT_REFERENCE.finditer(reference_text):
            key = ((ref.group(1) or ref.group(4) or repo).casefold(), int(ref.group(3) or ref.group(5)))
            if ref.group(1) or ref.group(4):
                explicit.add(key)
            if found.get(key) != "pull":
                found[key] = ref.group(2)
        return found

    def read_reference(ref_repo: str, ref_number: int) -> Any:
        """Read a referenced issue; a bare #N that is not here is recorded, not fatal."""
        try:
            return read(f"repos/{ref_repo}/issues/{ref_number}")
        except AuditError as exc:
            if not bare_reference_missing(exc, (ref_repo, ref_number) in explicit):
                raise
            if unresolved is not None and not any(row["reference"] == f"#{ref_number}" for row in unresolved):
                unresolved.append({"number": issue["number"], "source": "active_linked_pr",
                                   "reason": "unresolved_reference", "reference": f"#{ref_number}"})
            return UNRESOLVED

    def issue_associations(source_pull: dict[str, Any], source_repo: str) -> set[tuple[str, int]]:
        return {
            ((ref.group(1) or ref.group(4) or source_repo).casefold(), int(ref.group(3) or ref.group(5)))
            for line in str(source_pull.get("body") or "").splitlines()
            if re.match(r"\s*(?:[-*]\s+)?(?:Refs|Fixes|Closes|Resolves|Implements)\b", line, re.I)
            for ref in WAIT_REFERENCE.finditer(line)
            if ref.group(2) != "pull"
        }

    def implements_issue(source_pull: dict[str, Any], source_repo: str) -> bool:
        return (repo.casefold(), issue["number"]) in issue_associations(source_pull, source_repo)

    def delivers_issue(source_pull: dict[str, Any], source_repo: str) -> bool:
        """A keyword line, or a prose mention in a PR opened after this issue existed.

        A PR opened before the issue is usually its origin: its body may name
        the follow-up it spawned, which is not delivery.
        """
        if implements_issue(source_pull, source_repo):
            return True
        mentioned = any(
            ref.group(2) != "pull"
            and ((ref.group(1) or ref.group(4) or source_repo).casefold(), int(ref.group(3) or ref.group(5)))
            == (repo.casefold(), issue["number"])
            for ref in WAIT_REFERENCE.finditer(str(source_pull.get("body") or ""))
        )
        opened, created = _parse_time(source_pull.get("created_at")), _parse_time(issue.get("created_at"))
        return mentioned and not (opened and created and opened.tzinfo and created.tzinfo and opened < created)

    agent_wait = agent_delivery_wait(status, repo, issue["number"])
    # A delegate we cannot find leaves the delegated acceptance unproven, so it stays a hold.
    delegate_unresolved = False
    if agent_wait and ";" in agent_wait:
        for (target_repo, number) in references(agent_wait):
            target = read_reference(target_repo, number)
            if target is UNRESOLVED:
                delegate_unresolved = True
                continue
            if not isinstance(target, dict) or target.get("state") not in {"open", "closed"}:
                raise AuditError("unreadable delegated acceptance issue")
            if "pull_request" in target:
                return None
    # Delivery targets own the stack guard and implementation association;
    # historical PRs elsewhere in status cannot cancel or authorize selection.
    refs = references(next_action[1]) if next_action else {}
    # A PR named only elsewhere in status (often the PR a follow-up came from)
    # counts only when it delivers this issue.
    status_only = not refs
    if status_only:
        refs = references(status)
    pulls: dict[tuple[str, int], dict[str, Any]] = {}
    for (target_repo, number), kind in refs.items():
        if kind != "pull":
            target = read_reference(target_repo, number)
            if target is UNRESOLVED:
                continue
            if not isinstance(target, dict):
                raise AuditError("unreadable linked issue")
            if "pull_request" not in target:
                continue
        pulls[(target_repo, number)] = read(f"repos/{target_repo}/pulls/{number}")
    agent_next = bool(next_action and re.match(
        r"(?:the )?(?:Supervisor|(?:next )?agent)\s+(?:route|land|merge|close|reconcile|verify|retire)\w*\b",
        next_action[1].strip(), re.I,
    ))
    if not pulls and agent_wait and agent_next:
        # Closeout handoffs may name their source PR only in the discussion.
        # Read bounded complete history and require the PR's own association;
        # historical prerequisite PRs cannot establish this issue's delivery.
        comments, cut = fetch_paginated(
            f"repos/{repo}/issues/{issue['number']}/comments", fetch=lambda args: read(args[1]), max_pages=2,
        )
        if cut:
            raise AuditError("closeout comment history truncated")
        text = str(issue.get("body") or "") + "\n" + "\n".join(str(comment.get("body") or "") for comment in comments)
        for (target_repo, number), kind in references(text).items():
            if kind != "pull":
                if target_repo.casefold() == repo.casefold() and number == issue["number"]:
                    continue
                target = read_reference(target_repo, number)
                if target is UNRESOLVED:
                    continue
                if not isinstance(target, dict):
                    raise AuditError("unreadable handoff issue")
                if "pull_request" not in target:
                    continue
            pull = read(f"repos/{target_repo}/pulls/{number}")
            if not isinstance(pull, dict):
                raise AuditError("unreadable handoff pull")
            if implements_issue(pull, target_repo):
                pulls[(target_repo, number)] = pull
            elif not pull.get("merged_at") and not issue_associations(pull, target_repo):
                raise AuditError("unproven handoff stack association")
    if not pulls:
        return None
    recorded = _parse_time(issue.get("updated_at"))
    timestamp_proven = recorded is not None and recorded.tzinfo is not None
    evidence = []
    implements = True
    for (target_repo, number), pull in pulls.items():
        if not isinstance(pull, dict) or "merged_at" not in pull:
            raise AuditError("unreadable linked pull")
        if status_only and not delivers_issue(pull, target_repo):
            continue
        merged = _parse_time(pull.get("merged_at"))
        if merged is None:
            return None  # A partially landed stack still needs engineering.
        if merged.tzinfo is None:
            raise AuditError("unreadable merge timestamp")
        predates = bool(recorded is not None and recorded.tzinfo is not None and recorded < merged)
        timestamp_proven &= predates
        repository = read(f"repos/{target_repo}")
        default = repository.get("default_branch") if isinstance(repository, dict) else None
        if not default or not (pull.get("base") or {}).get("ref"):
            raise AuditError("unreadable landing destination")
        if (pull.get("base") or {}).get("ref") != default:
            return None
        implements &= implements_issue(pull, target_repo)
        evidence.append({"kind": "merged_active_pr", "url": f"https://github.com/{target_repo}/pull/{number}",
                         "merged_at": pull["merged_at"], "status_predates_merge": issue.get("updated_at") if predates else None})
    if not evidence:
        return None
    # Recognize only complete delivery/bookkeeping clauses. Anything outside
    # this grammar is remaining work, including wrapped and inflected actions.
    action = " ".join(WAIT_REFERENCE.sub("REF", next_action[1]).split()) if next_action else ""
    action = re.sub(r"\[([^]]+)]\(REF\)", "REF", action).rstrip(" .")
    raw_clauses = re.split(r",|;|\band\b|\bthen\b", action, flags=re.I)
    clauses = [part.strip() for part in raw_clauses if part.strip()]
    first = r"(?:the )?Supervisor (?:routes|lands|merges) (?:PR )?REF(?: through (?:Launchplane(?:'s)? )?(?:the )?(?:merge train|train))?"
    bookkeeping = (r"(?:verifies|confirms) (?:the )?(?:final )?landing(?: SHA)?|"
                   r"closes (?:this|the) issue|reconciles (?:the )?(?:runtime(?: checkout)?|closure)")
    delivery = bool(clauses and raw_clauses[-1].strip() and re.fullmatch(first, clauses[0], re.I)
                    and set(references(next_action[1])).issubset(pulls)
                    and all(re.fullmatch(bookkeeping, clause, re.I) for clause in clauses[1:]))
    reviewed_wait: dict[str, Any] | None = issue.get("structured_wait")
    hold_pending = delegate_unresolved or (reviewed_wait is not None and reviewed_wait["state"] != "clear")
    if agent_wait and next_action:
        hold_pending |= bool(re.search(r"\b(?:after|until|once|when|accept\w*|approv\w*|decision)\b", next_action[1], re.I))
    for match in re.finditer(
        rf"(?ims)^\s*(?:[-*]\s+)?(Waiting for|Blocked by|Parked until):\s*(.*?)(?=^\s*(?:[-*]\s+)?{STATUS_FIELD}:|\Z)", status,
    ):
        if reviewed_wait is not None and reviewed_wait["state"] == "clear":
            continue
        reason = match[2].strip()
        hold_pending |= not (github_plan_claim.no_wait_reason(reason, field="Waiting for")
                             or github_direction_next.non_external_wait(reason)
                             or (match[1].casefold() == "waiting for" and reason == agent_wait))
    for match in re.finditer(
        rf"(?ims)^\s*(?:[-*]\s+)?(?:State|Validation):\s*(.*?)(?=^\s*(?:[-*]\s+)?{STATUS_FIELD}:|\Z)", status,
    ):
        text = re.sub(r"\bno (?:failed|pending)(?:[ /]+(?:failed|pending))* (?:checks|workflows)\b", "", match[1], flags=re.I)
        hold_pending |= bool(re.search(r"\b(?:pending|awaiting|remaining|remainder|split|not yet|still required)\b", text, re.I))
    if agent_wait or (reviewed_wait is not None and reviewed_wait["state"] == "clear"):
        total = (issue.get("issue_dependencies_summary") or {}).get("total_blocked_by")
        if type(total) is not int or total != 0:
            blockers, cut = fetch_paginated(
                f"repos/{repo}/issues/{issue['number']}/dependencies/blocked_by",
                fetch=lambda args: read(args[1]), max_pages=2,
            )
            if cut:
                raise AuditError("closeout blockers truncated")
            hold_pending |= any(not closed_wait_prerequisite(blocker) for blocker in blockers)
    delivery_only = delivery and implements and not hold_pending
    obsolete_agent_wait = bool(agent_wait and implements)
    # Routing a PR already merged into its default branch is obsolete however
    # recently the issue was edited. This proves the delivery step, not the
    # finish line; unknown or remaining work still prevents selection exclusion.
    if not timestamp_proven and not delivery_only and not obsolete_agent_wait:
        return None
    return {"number": issue["number"], "title": issue.get("title"),
            "url": f"https://github.com/{repo}/issues/{issue['number']}", "evidence": evidence,
            "review_required": True, "completion_proven": False,
            "selection_exclusion": delivery_only,
            "recorded_hold_pending": hold_pending,
            "evidence_complete": True}


def record_drift(
    issue: dict[str, Any], repo: str, status: str, wait: dict[str, Any] | None, *,
    read: Callable[[str], Any], now: dt.datetime,
) -> DriftReport:
    number = issue["number"]
    evidence: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    asks: list[dict[str, Any]] = []
    waits = wait["waits"] if wait and wait["state"] == "waiting" else []

    def discussion(thread_repo: str, target: int) -> list[dict[str, Any]] | None:
        try:
            rows, partial = fetch_paginated(f"repos/{thread_repo}/issues/{target}/comments", fetch=lambda args: read(args[1]), max_pages=2)
        except AuditError:
            rows, partial = [], True
        if partial:
            errors.append({"number": number, "source": "drift_discussion", "reason": "unavailable", "url": f"https://github.com/{thread_repo}/issues/{target}"})
            return None
        return [{**row, "body": row.get("body") or "", "author": (row.get("user") or {}).get("login") or "", "url": row.get("html_url") or ""} for row in rows]

    for entry in waits:
        if "recheck" in entry and dt.datetime.fromisoformat(entry["recheck"]).date() < now.date():
            evidence.append({"kind": "overdue_recheck", "recheck": entry["recheck"], "recorded_at": f"{entry['recheck']}T00:00:00Z", "matched": entry["waiting_for"]})
    claimed = " ".join(f"{entry['waiting_for']} {entry['ends_when']}" for entry in waits)
    named = re.findall(r"(?im)^\s*(?:[-*]\s+)?(?:Next actor:\s*person:|Waiting for:).*$", status)
    cited = set(CITED_QUESTION.findall(f"{claimed} {' '.join(named)}"))
    trusted = {name.casefold() for name in github_plan_wait.authors(repo)} | {repo.split("/")[0].casefold()}
    own_comments = discussion(repo, number) if waits or cited else []
    if cited and own_comments is not None:
        linked = {(ref[1] or ref[4] or repo, int(ref[3] or ref[5])) for ref in WAIT_REFERENCE.finditer(f"{claimed} {' '.join(named)}")}
        linked.discard((repo, number))
        threads = [own_comments, *(discussion(target, target_number) or [] for target, target_number in sorted(linked)[:DRIFT_LINKED_LIMIT])]
        everything = [row for thread in threads for row in thread]
        matched: set[str] = set()
        for thread in threads:
            related = [row for row in everything if row not in thread]
            for question in github_plan_questions.questions(thread, repo.split("/")[0], trusted, related, recorded_decisions_only=True):
                if question["status"] == "answered" and question["url"] not in matched and (
                        question["url"] in cited or github_plan_questions.heading_id(question["question"]) in cited):
                    matched.add(question["url"])
                    answer = next((row for row in everything if row["url"] == question["answers"][0]), {})
                    evidence.append({"kind": "recorded_decision", "url": question["answers"][0], "question": question["url"], "recorded_at": answer.get("created_at")})
    for entry in waits:
        terms = set(re.findall(r"[a-z]{4,}", f"{entry['waiting_for']} {entry['ends_when']}".casefold())) - DRIFT_COMMON_TERMS
        for row in own_comments or []:
            if row["author"].casefold() not in trusted or (row["url"] and row["url"] in str((wait or {}).get("evidence"))):
                continue
            sentences = [part for part in re.split(r"(?<=[.!?])\s+|\n", row["body"]) if IMPOSSIBLE_CONDITION.search(part)]
            match = next((part for part in sentences if len(terms & set(re.findall(r"[a-z]{4,}", part.casefold()))) >= 2), None)
            if match:
                evidence.append({"kind": "impossible_ends_when", "url": row["url"], "matched": match.strip()[:240], "recorded_at": row.get("created_at"), "ends_when": entry["ends_when"]})
                break
        named = {(ref["repo"], ref["number"]) for ref in github_direction_next.issue_references(
            f"{github_plan_wait.first_step(entry['waiting_for'])}\n{entry['ends_when']}", repo=repo)}
        for target_repo, target_number in sorted(named - {(repo, number)}):
            try:
                target = read(f"repos/{target_repo}/issues/{target_number}")
            except AuditError:
                errors.append({"number": number, "source": "drift_reference", "reason": "unavailable", "url": f"https://github.com/{target_repo}/issues/{target_number}"})
                continue
            if isinstance(target, dict) and target.get("state") == "closed":
                kind = "impossible_ends_when" if target.get("state_reason") in {"not_planned", "duplicate"} else "closed_dependency"
                evidence.append({"kind": kind, "url": target.get("html_url") or f"https://github.com/{target_repo}/issues/{target_number}",
                                 "recorded_at": target.get("closed_at"), "ends_when": entry["ends_when"], "state_reason": target.get("state_reason")})
        ask = director_question_gate.ask_status(repo.split("/")[0], str((wait or {}).get("evidence") or ""), entry, trusted=trusted, read_thread=discussion)
        if ask is not None:
            asks.append({"number": number, **ask})
            if ask["ask_url"] is None:
                evidence.append({"kind": "ask_link_missing", "matched": entry["waiting_for"]})
            elif ask["found"] is False:
                evidence.append({"kind": "ask_comment_missing", "url": ask["ask_url"], "matched": entry["waiting_for"]})
    status_time = next(filter(None, (_parse_time(value) for value in (
        status_last_verified(status), issue.get("last_edited_at"), (issue.get("discussion") or {}).get("body_recorded_at")))), None)
    refs = {(ref[1] or ref[4] or repo, int(ref[3] or ref[5])) for ref in WAIT_REFERENCE.finditer(status)}
    if status_time is not None and status_time.tzinfo is None:
        status_time = status_time.replace(tzinfo=dt.timezone.utc)
    known_children = child_count(issue)
    aged = status_time is not None and now - status_time > dt.timedelta(days=STALE_STATUS_DAYS)
    if refs and known_children != 0 and (aged or (status_time is None and known_children)):
        try:
            children, cut = fetch_paginated(f"repos/{repo}/issues/{number}/sub_issues", fetch=lambda args: read(args[1]), max_pages=2)
        except AuditError:
            children, cut = [], True
        if cut:
            errors.append({"number": number, "source": "drift_children", "reason": "unavailable"})
            children = []
        named_children = [child for child in children if (repo, child.get("number")) in refs]
        if named_children and all(child.get("state") == "closed" for child in named_children):
            if status_time is None:
                errors.append({"number": number, "source": "drift_status_age", "reason": "status_age_unknown"})
            else:
                evidence.append({"kind": "closed_children_status", "recorded_at": status_time.isoformat(), "children": sorted(child["number"] for child in named_children),
                                 "matched": f"{len(named_children)} named children closed"})
    for item in evidence:
        recorded = _parse_time(item.get("recorded_at"))
        item["age_days"] = max(0, (now - (recorded if recorded.tzinfo else recorded.replace(tzinfo=dt.timezone.utc))).days) if recorded else None
    return DriftReport(evidence, errors, asks)


def status_last_verified(status: str) -> str | None:
    field = status_field(status, "Last verified") if status else ""
    match = re.search(r"\d{4}-\d{2}-\d{2}(?:T[0-9:.+-]+Z?)?", field)
    if match:
        return match[0]
    named = re.search(r"([A-Z][a-z]+)\.? (\d{1,2}),? (\d{4})", field)
    if not named:
        return None
    for form in ("%B %d %Y", "%b %d %Y"):
        try:
            return dt.datetime.strptime(f"{named[1]} {named[2]} {named[3]}", form).date().isoformat()
        except ValueError:
            continue
    return None


def child_count(issue: dict[str, Any]) -> int | None:
    summary = issue.get("sub_issues_summary")
    if isinstance(summary, dict) and isinstance(summary.get("total"), int):
        return summary["total"]
    counts = issue.get("sub_issues")
    if isinstance(counts, dict) and all(isinstance(counts.get(key), int) for key in ("open", "closed")):
        return counts["open"] + counts["closed"]
    return None


def stale_wait_report(
    issues: list[dict[str, Any]], repo: str, *, fetch: Callable[[list[str]], Any],
    inventory_complete: bool = True, max_issues: int = MAX_PAGES * 100, now: dt.datetime | None = None,
    read_waits: bool = False,
) -> dict[str, Any]:
    """Report verifiably obsolete waits and drifted records, never release or select their work.

    All recorded holds and native blockers must be satisfied or agent-only.
    Unknown prose, history, and elapsed time cannot prove acceptance.
    """
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    asks: list[dict[str, Any]] = []
    cache: dict[str, Any] = {}
    now = now or dt.datetime.now(dt.timezone.utc)

    def read(path: str) -> Any:
        if path not in cache:
            try:
                cache[path] = fetch(["api", path, "--method", "GET"])
            except AuditError as exc:
                cache[path] = exc
        if isinstance(cache[path], AuditError):
            raise cache[path]
        return cache[path]

    checked = 0
    checked_issues = []
    try:
        repository = github_read.repository_disposition(read(f"repos/{repo}"))
    except AuditError:
        repository = github_read.repository_disposition(None)
    if repository["archived"] is None:
        errors.append({"source": "repository", "reason": "unavailable"})
    if repository["archived"] is True:
        frozen = [{"number": issue["number"], "title": issue.get("title"),
                   "url": f"https://github.com/{repo}/issues/{issue['number']}"}
                  for issue in issues if "pull_request" not in issue and issue.get("state", "open") == "open"]
        return {"read_only": True, "complete": inventory_complete,
                "repository": repository, "frozen_issues": frozen,
                "checked": 0, "checked_issues": [], "items": [], "asks": [], "unavailable": [], "unresolved_references": [],
                "inventory_complete": inventory_complete}
    for issue in issues:
        active = "pull_request" not in issue and issue.get("state", "open") == "open"
        number = issue["number"]
        sections = github_direction_next.section_map(str(issue.get("body") or ""))
        status = next((text for title, text in sections.items() if title.casefold() == "current status"), "")
        if str(issue.get("state", "open")).casefold() == "open" and ("structured_wait" in issue or read_waits):
            try:
                recorded = "structured_wait" in issue or issue.get("comments") or "github-plan:wait" in str(issue.get("body") or "")
                current = current_audit_wait(issue, repo, fetch=lambda args: read(args[1])) if recorded else None
                drift, drift_errors, drift_asks = record_drift(issue, repo, status, current, read=read, now=now)
                errors.extend(error for error in drift_errors if error not in errors)
                asks.extend(drift_asks)
                if drift:
                    rows.append({"number": number, "title": issue.get("title"),
                                 "url": issue.get("html_url") or f"https://github.com/{repo}/issues/{number}",
                                 "evidence": drift, "review_required": True, "keeps_wait": True})
            except (AuditError, ValueError) as error:
                failure = {"number": number, "source": "structured_wait", "reason": str(error)}
                if failure not in errors:
                    errors.append(failure)
        if not parked_issue(issue, repo) and not active:
            continue
        normalized_status = re.sub(r"\*\*(Waiting for)(:?)\*\*(:?)", r"\1\2\3", status, flags=re.I)
        if checked >= max_issues:
            errors.append({"number": number, "source": "issue", "reason": "issue_limit"})
            continue
        checked += 1
        checked_issues.append(number)
        if (active and not parked_issue(issue, repo) and not WAIT_REFERENCE.search(status)
                and not agent_delivery_wait(normalized_status, repo, number)):
            continue
        try:
            wait = current_audit_wait(issue, repo, fetch=lambda args: read(args[1]))
        except (AuditError, ValueError) as error:
            errors.append({"number": number, "source": "structured_wait", "reason": str(error)})
            continue
        issue = {**issue, "structured_wait": wait}
        if wait is not None and wait["state"] == "waiting":
            continue
        if (active and not parked_issue(issue, repo)) or agent_delivery_wait(normalized_status, repo, number):
            try:
                issue_unresolved: list[dict[str, Any]] = []
                row = active_merged_status(issue, repo, status, read=read, unresolved=issue_unresolved)
                unresolved.extend(issue_unresolved)
                if row and parked_issue(issue, repo) and row["recorded_hold_pending"]:
                    row = None
                if row:
                    rows.append(row)
            except AuditError:
                errors.append({"number": number, "source": "active_linked_pr", "reason": "unavailable"})
                continue
            if (active and not parked_issue(issue, repo)) or row:
                continue
        evidence: list[dict[str, Any]] = []
        unread = False
        fields: list[tuple[str, str]] = []
        status_field = STATUS_FIELD
        entries = re.split(rf"(?im)(?=^\s*(?:[-*]\s+)?{status_field}:)", status)
        unknown_context = False
        for entry in entries:
            match = re.match(r"\s*(?:[-*]\s+)?(Waiting for|Blocked by|Parked until):\s*(.+)", entry, re.I | re.S)
            if match:
                fields.append((match[1], match[2].strip()))
            elif entry.strip():
                known = re.match(rf"\s*(?:[-*]\s+)?{status_field}:\s*(.*)", entry, re.I | re.S)
                if not known:
                    unknown_context = True
                elif re.match(r"\s*(?:[-*]\s+)?State:", entry, re.I):
                    unknown_context |= not bool(re.fullmatch(r"(?:active|waiting|blocked|parked|unstarted|not started)[. ]*", known[1].strip(), re.I))
                else:
                    unknown_context |= bool(re.search(
                        r"\b(?:after|until|once|when|awaiting|pending|must|has not|hasn't|not yet|approv\w*|accept\w*|decision)\b",
                        known[1], re.I,
                    ))
        pending = unknown_context or not fields or bool(re.search(r"(?im)^\s*(?:[-*]\s+)?(?:Waiting on:|State:.*(?:waiting for|awaiting|waiting on|parked until))", status))
        try:
            total = (issue.get("issue_dependencies_summary") or {}).get("total_blocked_by")
            blockers, cut = ([], False) if type(total) is int and total == 0 else fetch_paginated(
                f"repos/{repo}/issues/{number}/dependencies/blocked_by", fetch=lambda args: read(args[1]), max_pages=2,
            )
            if cut or any(blocker.get("state") not in {"open", "closed"} for blocker in blockers):
                unread = True
                errors.append({"number": number, "source": "native_blockers", "reason": "page_limit" if cut else "unknown_state"})
            pending = pending or any(not closed_wait_prerequisite(blocker) for blocker in blockers)
            for blocker in blockers:
                if closed_wait_prerequisite(blocker) and "pull_request" not in blocker:
                    evidence.append({"kind": "closed_native_blocker", "url": blocker.get("html_url"),
                                     "number": blocker.get("number"), "closed_at": blocker.get("closed_at"),
                                     "state_reason": blocker.get("state_reason")})
        except AuditError:
            unread = True
            errors.append({"number": number, "source": "native_blockers", "reason": "unavailable"})

        for field, reason in fields:
            reason = github_direction_next.undated_wait_reason(reason)
            # Reuse the claim helper's explicit no-wait semantics. Match the
            # whole agent phrase so a continued approval clause stays a hold.
            if github_plan_claim.no_wait_reason(reason, field="Waiting for"):
                if field.casefold() != "blocked by":
                    evidence.append({"kind": "no_external_wait", "field": field, "recorded": reason})
                continue
            if github_direction_next.non_external_wait(reason):
                evidence.append({"kind": "no_external_wait", "field": field, "recorded": reason})
                continue

            # Recognize complete reference-completion phrases, rather than
            # guessing whether arbitrary trailing prose still names a hold.
            plain = re.sub(r"\[([^]]+)]\(([^)]+)\)", r"\2", reason)
            normalized = WAIT_REFERENCE.sub("REF", plain).strip().rstrip(" .")
            references_phrase = r"REF(?:\s*(?:and|,|&|\+)\s*(?:(?:PR|issue)\s+)?REF)*"
            mechanical = bool(re.fullmatch(
                rf"(?:(?:the |a |an )?(?:source |train |open |native )?"
                rf"(?:PR|pull request|issue|blocker)\s+)?{references_phrase}"
                rf"(?:\s+to\s+(?:be\s+)?(?:merge|merged|land|close|closed|complete|completed))?|"
                rf"(?:merge|merging|landing|closure|completion) of\s+{references_phrase}",
                normalized, re.I,
            ))
            refs: dict[tuple[str, int], str] = {}
            explicit_refs: set[tuple[str, int]] = set()
            if mechanical:
                for ref in WAIT_REFERENCE.finditer(plain):
                    target_repo = ref.group(1) or ref.group(4) or repo
                    target_number = int(ref.group(3) or ref.group(5))
                    refs[(target_repo, target_number)] = ref.group(2) or "issues"
                    if ref.group(1) or ref.group(4):
                        explicit_refs.add((target_repo, target_number))
            if not refs:
                pending = True
                continue
            completed: list[dict[str, Any]] = []
            for (target_repo, target_number), kind in refs.items():
                try:
                    target = read(f"repos/{target_repo}/issues/{target_number}")
                    if not isinstance(target, dict) or target.get("state") not in {"open", "closed"}:
                        raise AuditError("unreadable wait target")
                    if "pull_request" in target or kind == "pull":
                        pull = read(f"repos/{target_repo}/pulls/{target_number}")
                        if not isinstance(pull, dict) or "merged_at" not in pull:
                            raise AuditError("unreadable wait pull")
                        landed = True
                        if pull.get("merged_at") and re.search(r"\b(?:land|landing)\b", normalized, re.I):
                            repository = read(f"repos/{target_repo}")
                            default_branch = repository.get("default_branch") if isinstance(repository, dict) else None
                            base_branch = (pull.get("base") or {}).get("ref")
                            if not default_branch or not base_branch:
                                raise AuditError("unreadable landing destination")
                            landed = base_branch == default_branch
                        if pull.get("merged_at") and landed:
                            completed.append({"kind": "merged_wait_pr", "field": field, "recorded": reason,
                                              "url": f"https://github.com/{target_repo}/pull/{target_number}",
                                              "merged_at": pull["merged_at"]})
                    elif closed_wait_prerequisite(target):
                        completed.append({"kind": "completed_wait_issue", "field": field, "recorded": reason,
                                          "url": f"https://github.com/{target_repo}/issues/{target_number}",
                                          "closed_at": target.get("closed_at"), "state_reason": target.get("state_reason")})
                except AuditError as exc:
                    if bare_reference_missing(exc, (target_repo, target_number) in explicit_refs):
                        # The hold names something we cannot find, so it stays a hold.
                        unresolved.append({"number": number, "source": "wait_reference",
                                           "reason": "unresolved_reference", "reference": f"#{target_number}"})
                        continue
                    unread = True
                    errors.append({"number": number, "source": "wait_reference",
                                   "url": f"https://github.com/{target_repo}/{kind}/{target_number}",
                                   "reason": "unavailable"})
            if len(completed) == len(refs):
                evidence.extend(completed)
            else:
                pending = True
        if evidence and not pending and not unread:
            rows.append({"number": number, "title": issue.get("title"),
                         "url": f"https://github.com/{repo}/issues/{number}", "evidence": evidence,
                         "review_required": True})
    return {"read_only": True, "complete": inventory_complete and not errors,
            "repository": repository, "frozen_issues": [],
            "checked": checked, "checked_issues": checked_issues, "items": rows, "asks": asks, "unavailable": errors,
            "unresolved_references": unresolved,
            "inventory_complete": inventory_complete}


def fetch_audit_issues(
    repo: str, milestones: list[dict[str, Any]], milestone_lines: dict[str, str],
    since: dt.datetime, *, fetch: Callable[[list[str]], Any],
    audit_since: dt.datetime | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Open issues plus recently closed milestone and audit-labeled issues."""
    issues, open_cut = fetch_paginated(f"repos/{repo}/issues?state=open", fetch=fetch)
    truncated = ["issues"] if open_cut else []
    if open_cut:
        # The general issue scan may be capped while the audit queue is small.
        # Still surface its questions, including older ones beyond that cap.
        open_audit, audit_cut = fetch_paginated(f"repos/{repo}/issues?state=open&labels={AUDIT_LABEL}", fetch=fetch)
        issues.extend(open_audit)
        if audit_cut:
            truncated.append("open_audit_issues")
    since_text = since.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    closed_cut = False
    for milestone in milestones:
        if str(milestone.get("title") or "") not in milestone_lines:
            continue
        closed, cut = fetch_paginated(
            f"repos/{repo}/issues?milestone={milestone.get('number')}&state=closed&since={since_text}",
            fetch=fetch, max_pages=2,
        )
        issues.extend(closed)
        closed_cut = closed_cut or cut
    if closed_cut:
        truncated.append("recent_closed_milestone_issues")
    audit_since_text = (audit_since or since).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    closed_audit, cut = fetch_paginated(
        f"repos/{repo}/issues?state=closed&labels={AUDIT_LABEL}&since={audit_since_text}", fetch=fetch,
    )
    issues.extend(closed_audit)
    if cut:
        truncated.append("recent_closed_audit_issues")
    issues = list({issue.get("number"): issue for issue in issues}.values())
    if enrich_admission_actors(issues, milestone_lines, repo, fetch=fetch, since=audit_since or since):
        truncated.append("milestone_issue_events")
    if enrich_milestone_waits(issues, repo, list(milestone_lines), fetch=fetch,
                              completed_titles=[item["title"] for item in milestones if item.get("state") == "closed"]):
        truncated.append("milestone_waits")
    if enrich_waiting_inbound_blockers(issues, repo, fetch=fetch):
        truncated.append("waiting_inbound_blockers")
    return issues, truncated


def planning_config(repo: str) -> dict[str, Any]:
    """Read labels through their owning helper, including its local overrides."""
    return runpy.run_path(str(GITHUB_SCRIPTS / "gh-plan.py"))["load_config"](repo)


def enrich_milestone_waits(
    issues: list[dict[str, Any]], repo: str, titles: list[str], *, fetch: Callable[[list[str]], Any],
    completed_titles: list[str] | None = None,
) -> bool:
    """Use the selection graph for overall Tracks; local audits check assigned work."""
    seeds = {(repo.casefold(), item["number"]): item for item in issues}
    for item in issues:
        item["repo"] = repo
    incomplete = False
    configs: dict[str, dict[str, Any]] = {}
    wait_helpers = runpy.run_path(str(GITHUB_SCRIPTS / "gh-plan.py"))

    def config(target_repo: str) -> dict[str, Any]:
        if target_repo not in configs:
            configs[target_repo] = planning_config(target_repo)
        return configs[target_repo]

    for item in issues:
        if "pull_request" in item or item.get("state", "open") != "open":
            continue
        blocking = (item.get("issue_dependencies_summary") or {}).get("blocking")
        if (not github_direction_next.is_direction_repository(repo)
                and (item.get("milestone") or {}).get("title") not in titles
                and type(blocking) is int and blocking == 0):
            continue
        try:
            item["structured_wait"] = current_audit_wait(item, repo, fetch=fetch)
        except (AuditError, ValueError) as error:
            item["structured_wait"] = {"version": 1, "state": "review", "evidence": str(error), "waits": []}
            item["_milestone_wait_unread"] = True
            incomplete = True
        item["_plan_status"] = github_direction_next.next_plan_status(item, config(repo))

    def read_node(target_repo: str, number: int) -> dict[str, Any]:
        nonlocal incomplete
        try:
            raw = seeds.get((target_repo.casefold(), number))
            if raw is None:
                raw = fetch(["api", f"repos/{target_repo}/issues/{number}", "--method", "GET"])

            def read_pages(path: str, _bound: int) -> tuple[list[dict[str, Any]], bool]:
                nonlocal incomplete
                rows, cut = fetch_paginated(path.lstrip("/"), fetch=fetch, max_pages=2)
                incomplete |= cut
                return rows, cut

            def read_comments(_bound: int) -> tuple[list[dict[str, Any]], bool]:
                rows, cut = read_pages(f"repos/{target_repo}/issues/{number}/comments", 200)
                return rows, not cut

            def resolve_wait(comments: list[dict[str, Any]]) -> dict[str, Any] | None:
                try:
                    return wait_helpers["current_wait"](raw, comments, target_repo)
                except wait_helpers["PlanError"]:
                    return None

            return github_work_graph.read_direction_node({**raw, "repo": target_repo}, config=config(target_repo),
                focus=None, comment_limit=200, read_comments=read_comments,
                read_relationships=lambda: github_work_graph.read_relationships(target_repo, number, raw, read_pages,
                    limit=200, fields=("blocked_by", "sub_issues")), resolve_wait=resolve_wait)
        except (AuditError, ValueError):
            incomplete = True
            return {"item": {"repo": target_repo, "number": number,
                             "url": f"https://github.com/{target_repo}/issues/{number}",
                             "exclusion": "unknown_dependencies"}}

    if github_direction_next.is_direction_repository(repo):
        roots = [{**github_direction_next.compact_list_issue(repo, item), "milestone": item.get("milestone")}
                 for item in issues if item.get("state", "open") == "open" and "pull_request" not in item]
        graph = github_direction_next.rank_direction_work(roots, milestone_titles=titles, read_node=read_node,
                                                          scan_limit=MAX_ADMISSION_ISSUES, completed_milestone_titles=completed_titles)
        target = next((item for item in issues if "pull_request" not in item), None)
        if target is not None:
            target["_milestone_wait_findings"] = graph["findings"]
        incomplete |= not graph["dependency_context"]["complete"]
    return incomplete


def own_project_order(text: str) -> str:
    """The Director's own-project declaration, without interpreting its share."""
    order = github_direction_next.section_map(text).get("Order", "")
    match = re.search(r"(?ms)^\d+\.\s+(Own projects\b.*?)(?=^\d+\.\s|\Z)", order)
    return match[1].strip() if match else ""


def own_project_patterns(text: str, *, own_label: str = "own-project") -> list[str]:
    """Read repository patterns, a label-only declaration, or explicit none."""
    declaration = own_project_order(text)
    if not declaration:
        raise AuditError("Order own-project list is missing")
    label_declaration = bool(re.search(rf"(?i)(?<![\w.*-]){re.escape(own_label)}(?![\w.*-])", declaration) and
                             re.search(r"(?i)\blabel(?:s|led|ed)?\b", declaration))
    no_projects = bool(re.match(r"(?i)^Own projects\s*(?::|are)\s*none\b", declaration))
    if ":" not in declaration:
        if label_declaration or no_projects:
            return []
        raise AuditError("Order own-project list is missing")
    listing = declaration.split(":", 1)[1].split("This list", 1)[0]
    if no_projects:
        return []
    if label_declaration:
        token = rf"(?<![\w.*-])`?{re.escape(own_label)}`?(?![\w.*-])"
        marker = (re.search(rf"(?i){token}\s+label\b|\blabel\s+{token}", listing)
                  or re.search(token, listing, re.I))
        if marker is None:
            if re.search(token, declaration.split(":", 1)[0], re.I):
                return []
            # Guidance after "This list" does not erase the declared list.
            return own_project_identifiers(listing)
        # Repository identifiers before the label belong to an explicit list;
        # those after it are examples of where labeled work can live.
        prefix = listing[:marker.start()]
        clauses = list(re.finditer(r";|\.\s", prefix))
        clause_start = clauses[-1].end() if clauses else 0
        label_clause = re.split(r";|\.\s", listing[clause_start:], maxsplit=1)[0]
        if re.search(r"(?i)\bwith\b|\blabel\s+only\b", label_clause):
            # A label restriction scopes repository examples in this clause.
            return own_project_identifiers(prefix[:clause_start])
        if re.search(r"`[\w.*-]+`|,|;|\band\b", prefix):
            return own_project_identifiers(prefix)
        return []
    names = own_project_identifiers(listing)
    if not names:
        raise AuditError("Order own-project list contains no repository identifiers")
    return names


def own_project_identifiers(listing: str) -> list[str]:
    """Repository identifiers in a list, excluding explanatory plain prose."""
    names = re.findall(r"`([\w.*-]+)`", listing)
    # Parentheses carry explicit identifiers and explanatory prose. Only single
    # repository identifiers in the surrounding comma/and-separated list count.
    plain = re.sub(r"\([^)]*\)", "", listing, flags=re.S)
    plain = re.sub(r"`[^`]*`", "", plain)
    plain = re.sub(r"\band its\b[^,]*", "", plain)
    for part in re.split(r",|;|\band\b", plain):
        name = part.strip().rstrip(".")
        if re.fullmatch(r"[\w.-]+", name):
            names.append(name)
    return sorted({name.casefold() for name in names})


def tracked_capacity_issues(
    direction_repo: str, text: str, since: dt.datetime, *, fetch: Callable[[list[str]], Any], max_nodes: int = 500,
    reads: github_next_reads.NextReads | None = None,
) -> tuple[set[tuple[str, int]], list[str]]:
    """Walk Track native blockers/sub-issues, including completed weekly work.

    These are the edges global next walks, without its availability filter:
    closed/waiting nodes still attribute the PRs that delivered their work.
    """
    titles = set(parse_direction(text)["milestones"])
    errors: list[str] = []
    pending: list[tuple[str, int]] = []
    try:
        milestones, cut = fetch_paginated(f"repos/{direction_repo}/milestones?state=all", fetch=fetch)
        if cut:
            errors.append("capacity_track_milestones")
        for milestone in milestones:
            closed_at = _parse_time(milestone.get("closed_at"))
            recent = milestone.get("state") == "closed" and closed_at is not None and closed_at >= since
            if milestone.get("title") not in titles and not recent:
                continue
            roots, cut = fetch_paginated(
                f"repos/{direction_repo}/issues?state=all&milestone={milestone['number']}", fetch=fetch,
            )
            if cut:
                errors.append(f"capacity_tracks:{milestone['number']}")
            pending.extend((direction_repo.casefold(), row["number"]) for row in roots
                           if "pull_request" not in row and str(row.get("title", "")).startswith("Track:"))
    except AuditError:
        errors.append("capacity_tracks")
    graph = read_audit_graph([{"repo": repo, "number": number} for repo, number in pending], fetch=fetch, max_nodes=max_nodes, reads=reads)
    errors.extend(f"capacity_graph:{source}" for source in graph.unavailable)
    if graph.truncated:
        errors.append("capacity_graph_limit")
    return set(graph.nodes), sorted(set(errors))


def read_audit_graph(
    roots: list[dict[str, Any]], *, fetch: Callable[[list[str]], Any], max_nodes: int,
    reads: github_next_reads.NextReads | None = None,
) -> github_work_graph.NativeGraphRead:
    def read(path: str, bound: int) -> tuple[list[dict[str, Any]], bool]:
        try:
            return fetch_paginated(path.lstrip("/"), fetch=fetch, max_pages=math.ceil(bound / 100))
        except AuditError as exc:
            raise ValueError(str(exc)) from exc

    return github_work_graph.read_native_graph(roots, read, max_nodes=max_nodes, relationship_limit=MAX_PAGES * 100, reads=reads)


def milestone_outcomes(
    direction_repo: str, text: str, since: dt.datetime, until: dt.datetime, *,
    fetch: Callable[[list[str]], Any], max_nodes: int = 500,
    reads: github_next_reads.NextReads | None = None,
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    try:
        milestones, cut = fetch_paginated(f"repos/{direction_repo}/milestones?state=all", fetch=fetch)
    except AuditError:
        return [{"milestone": None, "complete": False, "unavailable": ["milestones"]}]
    by_title = {item.get("title"): item for item in milestones}
    for title in parse_direction(text)["milestones"]:
        row: dict[str, Any] = {"milestone": title, "tracks": [], "issues_closed": [], "issues_open": 0,
                               "issues_waiting": 0, "unavailable": ["milestones"] if cut else []}
        outcomes.append(row)
        milestone = by_title.get(title)
        if milestone is None:
            row["unavailable"].append("milestone_missing")
            continue
        nodes: dict[tuple[str, int], dict[str, Any]] = {}
        try:
            roots, root_cut = fetch_paginated(
                f"repos/{direction_repo}/issues?state=all&milestone={milestone['number']}", fetch=fetch,
            )
            if root_cut:
                row["unavailable"].append("tracks")
            for root in roots:
                if "pull_request" not in root and str(root.get("title", "")).startswith("Track:"):
                    key = (direction_repo, int(root["number"]))
                    nodes[key] = root
                    row["tracks"].append(f"https://github.com/{direction_repo}/issues/{root['number']}")
                    row.setdefault("closure_reviews", []).append({"url": row["tracks"][-1],
                        **github_direction_next.milestone_closure_review(root.get("body") or "")})
        except AuditError:
            row["unavailable"].append("tracks")
        graph = read_audit_graph([{**node, "repo": key[0], "number": key[1]} for key, node in nodes.items()],
                                fetch=fetch, max_nodes=max_nodes, reads=reads)
        row["unavailable"].extend(f"graph:{source}" for source in graph.unavailable)
        if graph.truncated:
            row["unavailable"].append("graph_limit")
        def read_outcome(item: tuple[tuple[str, int], dict[str, Any]]) -> dict[str, Any]:
            (repo, number), node = item
            outcome: dict[str, Any] = {"issues_closed": [], "issues_open": 0, "issues_waiting": 0, "unavailable": []}
            if "updated_at" not in node or (node.get("state") == "closed" and "closed_at" not in node):
                try:
                    node = fetch(["api", f"repos/{repo}/issues/{number}", "--method", "GET"])
                    if (not isinstance(node, dict) or "updated_at" not in node
                            or (node.get("state") == "closed" and "closed_at" not in node)):
                        raise AuditError("milestone graph node metadata unavailable")
                except AuditError:
                    outcome["unavailable"].append(f"node:{repo}#{number}")
                    return outcome
            url = node.get("html_url") or f"https://github.com/{repo}/issues/{number}"
            if node.get("state") == "closed" and _within(node.get("closed_at"), since, until):
                outcome["issues_closed"].append({"url": url, "title": node.get("title"), "closed_at": node.get("closed_at")})
            if node.get("state") == "open":
                outcome["issues_open"] += 1
                try:
                    wait = current_audit_wait(node, repo, fetch=fetch)
                    outcome["issues_waiting"] += int((wait or {}).get("state") == "waiting")
                except (AuditError, ValueError):
                    outcome["unavailable"].append(f"wait:{repo}#{number}")
            return outcome

        items = sorted(graph.nodes.items())
        outcomes_read = reads.map(read_outcome, items) if reads else list(map(read_outcome, items))
        for node_outcome in outcomes_read:
            row["issues_closed"].extend(node_outcome["issues_closed"])
            row["unavailable"].extend(node_outcome["unavailable"])
            row["issues_open"] += node_outcome["issues_open"]
            row["issues_waiting"] += node_outcome["issues_waiting"]
        if any(source.startswith("wait:") for source in row["unavailable"]):
            row["issues_waiting"] = None
        row["complete"] = not row["unavailable"]
    return outcomes


def pull_issue_refs(repo: str, pull: dict[str, Any]) -> set[tuple[str, int]]:
    """Explicit closing/Refs clauses, rather than incidental issue mentions."""
    refs = set(pull.get("_native_issue_refs") or [])
    text = f"{pull.get('title') or ''}\n{pull.get('body') or ''}"
    text = re.sub(r"\[[^]]*]\((https://github\.com/[\w.-]+/[\w.-]+/issues/\d+)\)", r"\1", text)
    for clause in re.finditer(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs)\b[ \t]*:?[ \t]*", text, re.I):
        if re.search(r"\b(?:not|never|doesn't|don't)\s*$", text[max(0, clause.start() - 20):clause.start()], re.I):
            continue
        # Only an immediate reference list is a link; list rows may be on the
        # following lines. Prose after the references ends the list.
        tail = re.sub(r"^\s*(?:[-*]\s+)?", "", text[clause.end():])
        while match := re.match(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)|([\w.-]+/[\w.-]+)?#(\d+)\b", tail):
            refs.add(((match[1] or match[3] or repo).casefold(), int(match[2] or match[4])))
            tail = tail[match.end():]
            separator = re.match(r"[ \t]*(?:,|and)[ \t]*|[ \t]*\n\s*(?:[-*]\s+)?", tail)
            if separator is None:
                break
            tail = tail[separator.end():]
    return refs


def native_pull_issue_refs(repo: str, number: int, *, fetch: Callable[[list[str]], Any]) -> set[tuple[str, int]]:
    """Read GitHub's native PR issue links; REST pull listings omit them."""
    owner, name = repo.split("/")
    cursor = None
    refs: set[tuple[str, int]] = set()
    for _ in range(MAX_PAGES):
        query = """query($owner:String!,$name:String!,$number:Int!,$cursor:String){
          repository(owner:$owner,name:$name){pullRequest(number:$number){
            closingIssuesReferences(first:100,after:$cursor){
              nodes{number repository{nameWithOwner}} pageInfo{hasNextPage endCursor}
            }
          }}
        }"""
        args = ["api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}",
                "-f", f"name={name}", "-F", f"number={number}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        response = fetch(args)
        try:
            links = response["data"]["repository"]["pullRequest"]["closingIssuesReferences"]
            if response.get("errors"):
                raise AuditError("native PR issue links unavailable")
            for node in links["nodes"]:
                refs.add((node["repository"]["nameWithOwner"].casefold(), int(node["number"])))
            info = links["pageInfo"]
            if not info["hasNextPage"]:
                return refs
            if not info["endCursor"] or info["endCursor"] == cursor:
                raise AuditError("native PR issue links cursor missing")
            cursor = info["endCursor"]
        except (KeyError, TypeError, ValueError) as exc:
            raise AuditError("native PR issue links response unavailable") from exc
    raise AuditError("native PR issue links truncated")


def latest_issue_admission(repo: str, number: int, *, fetch: Callable[[list[str]], Any]) -> str | None:
    path = f"repos/{repo}/issues/{number}"
    issue = fetch(["api", path, "--method", "GET"])
    if not isinstance(issue, dict):
        raise AuditError("claim issue unavailable")
    if "pull_request" in issue:
        return None
    count = issue.get("comments")
    if type(count) is not int or count < 0:
        raise AuditError("claim comment count unavailable")
    trusted = github_plan_wait.authors(repo)
    last_page = math.ceil(count / 100)
    for page in range(last_page, max(0, last_page - MAX_PAGES), -1):
        comments = fetch(["api", f"{path}/comments?per_page=100&page={page}", "--method", "GET"])
        if not isinstance(comments, list):
            raise AuditError("claim comments unavailable")
        for comment in reversed(comments):
            if not github_plan_wait.author_is_trusted(comment, repo.split("/")[0], author_logins=trusted):
                continue
            body = github_plan_claim.unquoted_text(comment.get("body") or "")
            if not re.match(r"^Claimed by:?[ \t]+\S+[ \t]*(?:\n|$)", body.strip(), re.I):
                continue
            admission = re.search(r"^Admission:[ \t]*([a-z-]+)\b", body, re.M)
            return admission[1] if admission and admission[1] in ADMISSION_RANKS else None
    if last_page > MAX_PAGES:
        raise AuditError("claim comments truncated")
    return None


def reopened_issue_events(
    repo: str, since: dt.datetime, *, fetch: Callable[[list[str]], Any], max_pages: int = MAX_PAGES,
) -> tuple[list[dict[str, Any]], bool]:
    """Each issue's latest reopens since the window opened, and whether the page cap cut it short.

    Reopening updates an issue, so issues updated in the window hold every
    reopen. The repository event feed is mostly label and close traffic and
    runs past any page cap in a busy repository. Several reopens per issue are
    read so one after the window closes, while the audit runs, cannot hide one
    inside it.
    """
    owner, name = repo.split("/")
    since_text = since.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = """query($owner:String!,$name:String!,$since:DateTime!,$cursor:String){
      repository(owner:$owner,name:$name){
        issues(first:100,after:$cursor,filterBy:{since:$since},orderBy:{field:UPDATED_AT,direction:DESC}){
          nodes{number title timelineItems(itemTypes:[REOPENED_EVENT],since:$since,last:5){
            nodes{... on ReopenedEvent{createdAt}}
          }}
          pageInfo{hasNextPage endCursor}
        }
      }
    }"""
    events: list[dict[str, Any]] = []
    cursor = None
    for _ in range(max_pages):
        args = ["api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}",
                "-f", f"name={name}", "-f", f"since={since_text}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        response = fetch(args)
        try:
            if response.get("errors"):
                raise AuditError("reopened issues unavailable")
            issues = response["data"]["repository"]["issues"]
            for node in issues["nodes"]:
                for reopen in node["timelineItems"]["nodes"]:
                    events.append({"event": "reopened", "created_at": reopen["createdAt"],
                                   "issue": {"number": node["number"], "title": node["title"]}})
            info = issues["pageInfo"]
        except (AttributeError, KeyError, TypeError) as exc:
            raise AuditError("reopened issues response unavailable") from exc
        if not info["hasNextPage"]:
            return events, False
        if not info["endCursor"] or info["endCursor"] == cursor:
            raise AuditError("reopened issues cursor missing")
        cursor = info["endCursor"]
    return events, True


def own_repository(repo: str, own_projects: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(repo.split("/")[1].casefold(), pattern) for pattern in own_projects)


def _labels(item: dict[str, Any]) -> set[str]:
    return {str(label.get("name", "")).casefold() for label in item.get("labels") or [] if isinstance(label, dict)}


def _within(value: Any, since: dt.datetime, until: dt.datetime, *, end_exclusive: bool = False) -> bool:
    moment = _parse_time(value)
    return (moment is not None and since <= moment
            and (moment < until if end_exclusive else moment <= until))


def capacity_rank(
    repo: str, pull: dict[str, Any], *, own_projects: list[str], tracked_issues: set[tuple[str, int]],
    own_label: str, own_issues: set[tuple[str, int]] | frozenset[tuple[str, int]],
) -> str:
    refs = pull_issue_refs(repo, pull)
    if refs & tracked_issues:
        return "milestone"
    if (own_repository(repo, own_projects) or own_label.casefold() in _labels(pull)
            or refs & own_issues or any(own_repository(ref, own_projects) for ref, _ in refs)):
        return "own"
    return "tooling"


def change_statements(body: str, field: str) -> list[str]:
    statements = []
    for _, line in github_plan_wait.live_lines(body.replace("**", ""), prefix=""):
        match = re.search(rf"^[ \t]*(?:[-*][ \t]+)?{re.escape(field)}:[ \t]*(\S.*?)\s*$", line, re.I)
        if match and match[1].casefold().rstrip(".") not in {"none", "nothing", "n/a"}:
            statements.append(match[1])
    return statements


def capacity_summary(
    *, own_projects: list[str], tracked_issues: set[tuple[str, int]], direction_source: str,
    pulls: dict[str, list[dict[str, Any]]], milestones: dict[str, list[dict[str, Any]]],
    events: dict[str, list[dict[str, Any]]], since: dt.datetime, until: dt.datetime,
    incomplete: bool = False, own_label: str = "own-project", own_issues: frozenset[tuple[str, int]] | set[tuple[str, int]] = frozenset(),
    direction_text: str = "",
    reporting_rules: list[dict[str, Any]] | None = None, reporting_error: str | None = None,
    reporting_incomplete: bool = False,
) -> dict[str, Any]:
    """The overall direction's weekly numbers from per-repository listings.

    A mixed repository's own-project work counts as own when the pull request,
    or an issue it links, carries `own_label`, or it links an issue in an
    own-project repository.
    """
    merged_by_rank = dict.fromkeys(RANKS, 0)
    classification_counts = dict.fromkeys(("admission", "link_walk", "disagreement_fallback"), 0)
    by_repository: dict[str, dict[str, Any]] = {}
    reverts: list[dict[str, Any]] = []
    reporting_rows: list[tuple[str, dict[str, Any], bool, bool]] = []
    for repo in sorted(pulls):
        # A pull request updated mid-read can shift into the next page twice.
        unique = {pull.get("number"): pull for pull in pulls[repo]}
        merged = [pull for pull in unique.values() if not github_plan_claim.is_train_batch(pull)
                  and _within(pull.get("merged_at"), since, until, end_exclusive=True)]
        if not merged:
            continue
        counts = dict.fromkeys(RANKS, 0)
        tooling_pulls = []
        for pull in merged:
            refs = pull_issue_refs(repo, pull)
            admissions = pull.get("_admission_ranks") or set()
            if len(admissions) == 1:
                rank = next(iter(admissions))
                classification_counts["admission"] += 1
            else:
                rank = capacity_rank(repo, pull, own_projects=own_projects, tracked_issues=tracked_issues,
                                     own_label=own_label, own_issues=own_issues)
                classification_counts["link_walk"] += 1
                classification_counts["disagreement_fallback"] += int(len(admissions) > 1)
            reporting_rows.append((repo, pull, rank == "milestone",
                                   own_repository(repo, own_projects) or own_label.casefold() in _labels(pull)
                                   or own_label.casefold() in (pull.get("_report_labels") or set())
                                   or bool(refs & set(own_issues))
                                   or any(own_repository(ref, own_projects) for ref, _ in refs)))
            merged_by_rank[rank] += 1
            counts[rank] += 1
            if rank == "tooling":
                tooling_pulls.append({
                    "number": pull["number"], "title": pull.get("title"),
                    "url": f"https://github.com/{repo}/pull/{pull['number']}",
                    **{field: change_statements(pull.get("body") or "", field)
                       for field in ("removed", "added")},
                })
            if REVERT_TITLE.search(str(pull.get("title") or "")) or REVERT_BODY.search(str(pull.get("body") or "")):
                reverts.append({"repo": repo, "number": pull.get("number"), "title": pull.get("title"), "merged_at": pull.get("merged_at")})
        by_repository[repo] = {"merged": len(merged), "merged_by_rank": counts}
        if tooling_pulls:
            by_repository[repo]["tooling_pulls"] = tooling_pulls
    total = sum(merged_by_rank.values())
    own = merged_by_rank["own"]
    own_share = round(own / total, 3) if total else None
    # Partial counts remain visible, but cannot establish a settled share.
    share_status = "unknown" if incomplete else "complete" if total else "no_merges"
    closed = [
        {"repo": repo, "number": item.get("number"), "title": item.get("title"), "closed_at": item.get("closed_at")}
        for repo in sorted(milestones) for item in milestones[repo]
        if item.get("state") == "closed" and _within(item.get("closed_at"), since, until, end_exclusive=True)
    ]
    reopened: dict[tuple[str, Any], dict[str, Any]] = {}
    for repo in sorted(events):
        for event in events[repo]:
            subject = event.get("issue") or {}
            if (event.get("event") != "reopened" or "pull_request" in subject
                    or not _within(event.get("created_at"), since, until, end_exclusive=True)):
                continue
            key = (repo, subject.get("number"))
            if key not in reopened or str(event.get("created_at")) > str(reopened[key]["reopened_at"]):
                reopened[key] = {"repo": repo, "number": subject.get("number"), "title": subject.get("title"), "reopened_at": event.get("created_at")}
    result = {
        "since": since.isoformat().replace("+00:00", "Z"),
        "until": until.isoformat().replace("+00:00", "Z"),
        "classification_source": direction_source,
        "own_projects": own_projects,
        "own_project_label": own_label,
        "merged_total": total,
        "merged_by_rank": merged_by_rank,
        "classification_counts": classification_counts,
        "own_share": own_share,
        "own_share_basis": "merged_pull_requests",
        "own_share_status": share_status,
        "own_share_direction": {
            "order": own_project_order(direction_text),
            "capacity": github_direction_next.section_map(direction_text).get("Capacity", "").strip(),
        },
        "by_repository": by_repository,
        "milestones_closed": closed,
        "issues_reopened": sorted(reopened.values(), key=lambda item: (item["repo"], str(item["number"]))),
        "revert_pulls": reverts,
        "provider_capacity_unused": "manual",
    }
    if reporting_rules is not None or reporting_error:
        result["reporting"] = direction_reporting.report(
            reporting_rules or [], reporting_rows,
            capacity=result["own_share_direction"]["capacity"], source=direction_source,
            incomplete=incomplete or reporting_incomplete, error=reporting_error,
        )
    return result


def own_project_label(repo: str) -> str:
    """The label that marks own-project work, from the planning helper that creates it."""
    return str(planning_config(repo)["labels"]["own_project"])


def line_bucket(filename: str) -> str:
    path = pathlib.PurePosixPath(filename)
    return "tests" if (
        any(part.casefold() in {"test", "tests", "__tests__", "test-fixtures", "fixtures"}
            or part.endswith("Tests") for part in path.parts[:-1])
        or path.name.startswith("test_") or path.stem.endswith(("_test", "Test", "Tests"))
        or ".test." in path.name or ".spec." in path.name
    ) else "other"


def text_lines(content: bytes) -> int:
    return 0 if b"\0" in content else content.count(b"\n") + int(bool(content) and not content.endswith(b"\n"))


def commit_line_changes(repo: str, commit: dict[str, Any], *, fetch: Callable[[list[str]], Any]) -> dict[str, int]:
    files = commit.get("files")
    if not isinstance(files, list):
        raise AuditError("commit files unavailable")
    cut = False
    if len(files) >= COMMIT_FILES_PER_RESPONSE:
        files, cut = fetch_paginated(f"repos/{repo}/commits/{commit['sha']}", fetch=fetch, key="files", max_pages=31)
    stats = commit.get("stats") or {}
    if (cut or any(type(row.get(key)) is not int or row[key] < 0 for row in files for key in ("additions", "deletions"))
            or any(sum(row[key] for row in files) != stats.get(key) for key in ("additions", "deletions"))):
        raise AuditError("commit file line counts incomplete")
    changes = {"tests": 0, "other": 0}
    for row in files:
        if row.get("status") == "copied":
            raise AuditError("copied file line-count semantics unverified")
        bucket = line_bucket(row["filename"])
        net = row["additions"] - row["deletions"]
        previous = row.get("previous_filename")
        if row.get("status") == "renamed" and isinstance(previous, str) and line_bucket(previous) != bucket:
            parent = commit["parents"][0]["sha"]
            blob = fetch(["api", f"repos/{repo}/contents/{quote(previous)}?ref={parent}", "--method", "GET"])
            if not isinstance(blob, dict) or blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
                raise AuditError("renamed file contents unavailable")
            content = base64.b64decode(blob["content"])
            lines = text_lines(content)
            changes[line_bucket(previous)] -= lines
            changes[bucket] += lines + net
        else:
            changes[bucket] += net
    return changes


def default_branch_net_lines(
    repo: str, branch: str, since: dt.datetime, until: dt.datetime, *, fetch: Callable[[list[str]], Any],
    max_commits: int = 2000,
) -> dict[str, Any]:
    if not branch:
        raise AuditError("default branch unavailable")
    result: dict[str, Any] = {"status": "complete", "branch": branch, "base": None, "head": None,
                              "tests": 0, "other": 0, "total": 0, "basis": "default_branch_first_parent"}
    ref = quote(branch, safe="")
    seen: set[str] = set()
    contributors: list[dict[str, Any]] = []
    for _ in range(max_commits):
        commit = fetch(["api", f"repos/{repo}/commits/{ref}", "--method", "GET"])
        if not isinstance(commit, dict) or not isinstance(commit.get("sha"), str):
            raise AuditError("default branch commit unavailable")
        sha = commit["sha"]
        moment = _parse_time(((commit.get("commit") or {}).get("committer") or {}).get("date"))
        parents = commit.get("parents")
        if moment is None or not isinstance(parents, list) or sha in seen:
            raise AuditError("default branch history incomplete")
        seen.add(sha)
        if moment < since:
            result["base"] = sha
            break
        if moment < until:
            result["head"] = result["head"] or sha
            changes = commit_line_changes(repo, commit, fetch=fetch)
            for bucket, net in changes.items():
                result[bucket] += net
            subject = str((commit.get("commit") or {}).get("message") or "").partition("\n")[0]
            contributors.append({"sha": sha, "subject": subject, "total": sum(changes.values())})
        if not parents:
            break
        ref = parents[0]["sha"]
    else:
        raise AuditError("default branch history limit reached")
    result["total"] = result["tests"] + result["other"]
    growing = sorted((row for row in contributors if row["total"] > 0), key=lambda row: -row["total"])
    result["largest"] = growing[:LARGEST_CONTRIBUTORS]
    return result


def tarball_text_lines(repo: str, sha: str, *, gh: str) -> int:
    try:
        proc = subprocess.run([gh, "api", f"repos/{repo}/tarball/{sha}"], capture_output=True,
                              timeout=github_api.remaining_retry_timeout_seconds())
    except subprocess.TimeoutExpired as exc:
        raise AuditError("tarball read exceeded the read deadline") from exc
    if proc.returncode != 0:
        raise AuditError(f"tarball read failed: {proc.stderr.decode(errors='replace').strip()[:400]}")
    lines = 0
    try:
        with tarfile.open(fileobj=BytesIO(proc.stdout), mode="r:gz") as archive:
            for member in archive:
                handle = archive.extractfile(member) if member.isfile() else None
                if handle is not None:
                    lines += text_lines(handle.read())
    except (tarfile.TarError, OSError) as exc:
        raise AuditError(f"tarball unreadable: {exc}") from exc
    return lines


def growth_trigger(total: int, lines_at_head: int) -> dict[str, Any]:
    lines_before = lines_at_head - total
    threshold = max(GROWTH_FLOOR_LINES, math.floor(GROWTH_SHARE * lines_before))
    return {"lines_before": lines_before, "threshold": threshold, "exceeded": total > threshold}


def owner_repositories(owner: str, *, fetch: Callable[[list[str]], Any], include_archived: bool = False) -> tuple[dict[str, dict[str, Any]], bool]:
    """The owner's repositories the reader can see, and whether the listing was cut short."""
    try:
        listed, cut = fetch_paginated("installation/repositories", fetch=fetch, key="repositories")
    except AuditError as installation_error:
        # The Director's own login has no installation; it lists what it owns.
        try:
            listed, cut = fetch_paginated("user/repos?affiliation=owner", fetch=fetch)
        except AuditError as exc:
            raise AuditError(f"could not list repositories: {installation_error}; {exc}") from exc
    return {
        str(item.get("full_name")).casefold(): item for item in listed
        if str(((item.get("owner") or {}).get("login")) or "").casefold() == owner.casefold()
        and repository_scope.includes_repository(item, include_archived=include_archived)
    }, cut


def repository_listing_gaps(cut: bool, *, fetch: Callable[[list[str]], Any]) -> list[dict[str, str]]:
    """Why an account's repository listing may be incomplete: a page cap or a selected-repositories App."""
    gaps: list[dict[str, str]] = []
    if cut:
        gaps.append({"source": "repositories", "reason": "page_limit"})
    try:
        installation = fetch(["api", "installation/repositories?per_page=1", "--method", "GET"])
        if isinstance(installation, dict) and installation.get("repository_selection") == "selected":
            # An App installed on chosen repositories cannot see the rest of the account.
            gaps.append({"source": "repositories", "reason": "installation_selected_repositories"})
    except AuditError:
        pass  # The Director's own login lists every repository it owns.
    return gaps


def direction_adoption(repo: str, *, fetch: Callable[[list[str]], Any]) -> tuple[str, str | None]:
    """"adopted", "absent" or "unknown" for a repository's default-branch DIRECTION.md, with the reason.

    Only GitHub's 404 confirms absence; any other failure or an unexpected answer stays unknown.
    """
    try:
        body = fetch(["api", f"repos/{repo}/contents/DIRECTION.md", "--method", "GET"])
    except AuditError as exc:
        if NOT_FOUND_RE.search(str(exc)):
            return "absent", None
        return "unknown", str(exc)[:300]
    if isinstance(body, dict) and body.get("type", "file") == "file" and isinstance(body.get("content"), str):
        return "adopted", None
    return "unknown", "the DIRECTION.md response is not a readable file"


def emit_weekly_receipt(receipt_data: dict[str, Any]) -> None:
    print(json.dumps(receipt_data, indent=2, sort_keys=True), flush=True)
    try:
        descriptor = sys.stdout.fileno()
    except UnsupportedOperation:
        return
    if stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.fsync(descriptor)


def weekly_audit(
    owner: str, *, gh: str, automation: str | None = None, include_archived: bool = False,
    emit: Callable[[dict[str, Any]], None],
) -> tuple[int, dict[str, Any]]:
    with github_next_reads.NextReads(workers=4, seconds=0, label="weekly audit") as reads:
        return collect_weekly_audit(owner, gh=gh, automation=automation, include_archived=include_archived,
                                    emit=emit, reads=reads)


def weekly_repositories(
    owner: str, *, fetch: Callable[[list[str]], Any], reads: github_next_reads.NextReads,
) -> tuple[list[str], dict[str, Any]]:
    direction_repo = f"{owner}/direction"
    discovery: dict[str, Any] = {"complete": False, "gaps": [], "unknown": {}}
    adopted: list[str] = []
    try:
        listed, cut = owner_repositories(owner, fetch=fetch)
        discovery["gaps"] = repository_listing_gaps(cut, fetch=fetch)
        names = [str(listed[key].get("full_name")) for key in sorted(listed) if key != direction_repo.casefold()]
        states = reads.map(lambda target_repo: direction_adoption(target_repo, fetch=fetch), names)
        for name, (state, reason) in zip(names, states, strict=True):
            if state == "adopted":
                adopted.append(name)
            elif state == "unknown":
                discovery["unknown"][name] = reason
    except AuditError as exc:
        discovery["gaps"].append({"source": "repositories", "reason": "unavailable", "detail": str(exc)[:300]})
    discovery["complete"] = not discovery["gaps"] and not discovery["unknown"]
    repos = [direction_repo, *adopted]
    return repos, discovery


def collect_weekly_audit(
    owner: str, *, gh: str, automation: str | None, include_archived: bool,
    emit: Callable[[dict[str, Any]], None], reads: github_next_reads.NextReads,
) -> tuple[int, dict[str, Any]]:
    started = dt.datetime.now(dt.timezone.utc)
    observed = github_work_graph.cached_reads(lambda args: gh_json(args, gh=gh), reads=reads)

    def fetch(args: list[str]) -> Any:
        try:
            return observed(args)
        except github_next_reads.ReadDeadline as deadline_error:
            raise AuditError(str(deadline_error)) from deadline_error

    reads.start_phase("discovery")
    direction_repo = f"{owner}/direction"
    repos, discovery = weekly_repositories(owner, fetch=fetch, reads=reads)
    audits: dict[str, dict[str, Any]] = {}
    unread: dict[str, str] = {name: f"adoption unknown: {reason}" for name, reason in discovery["unknown"].items()}
    held: dict[str, str] = {}
    updates: list[dict[str, Any]] = []
    for repo in repos:
        _code, result = audit_one(repo, gh=gh, owner=owner, automation=automation,
                                  include_archived=include_archived, fetch=fetch, defer_marker=True, reads=reads)
        update = result.pop("_marker_update", None)
        recordable = isinstance(update, dict) and update.get("action") == "record"
        if update is not None:
            updates.append(update)
        audits[repo] = result
        if "error" in result:
            unread[repo] = f"reads failed: {result['error']}"
        elif not recordable:
            # Archived, unknown archive state or a missing DIRECTION.md: no run was recorded.
            unread[repo] = str(result.get("marked_skipped") or "direction_missing")
        elif result.get("marked_skipped") == "window_open":
            # The run was recorded with its window held open, so it counts as read.
            # The window keeps its start, and the next audit rereads what failed or was capped.
            held[repo] = ", ".join(held_reads([item for finding in result.get("findings", [])
                                               if finding.get("kind") == "coverage_incomplete"
                                               for item in finding.get("listings", [])])) or "window_open"
    inspection = importlib.import_module("skills.jetbrains-inspection.scripts.weekly_inspection")
    inspections = {repo: inspection.report(repo) for repo in repos}
    capacity = audits.get(direction_repo, {}).get("capacity")
    if isinstance(capacity, dict):
        capacity["project_inspections"] = inspections
    receipt: dict[str, Any] = {
        "owner": owner, "weekly": True, "read_only": True, "started": direction_mark.utc_stamp(started),
        "repos": repos, "discovery": discovery, "unread": unread, "held": held, "audits": audits,
        "ok": (all(result.get("ok") for result in audits.values()) and discovery["complete"]
               and not unread and not held),
        "weekly_recorded": None, "read_metrics": reads.receipt(),
    }
    complete = discovery["complete"] and not unread
    if not complete:
        receipt["weekly_skipped"] = "not every adopted repository was read" if unread else "repository listing incomplete"
    try:
        path = direction_mark.marker_path()
        shown = str(path)
        receipt["weekly_recorded"] = {"marker": shown, "ran": receipt["started"], "record": direction_repo} if complete else None
        emit(receipt)
    except (OSError, RuntimeError, TypeError, ValueError, UnicodeError) as exc:
        receipt["weekly_recorded"] = None
        receipt["marker_error"] = f"Could not prepare or emit the weekly audit receipt: {exc}"
        print(f"ERROR: {receipt['marker_error']}", file=sys.stderr)
        return 1, receipt
    try:
        direction_mark.mark_weekly_audits(path, owner, started, updates, complete)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        receipt["ok"] = False
        receipt["weekly_recorded"] = None
        receipt["marker_error"] = f"Could not record the weekly audit at {shown}: {exc}"
        print(f"ERROR: {receipt['marker_error']}. The weekly audit was not recorded.", file=sys.stderr)
        return 1, receipt
    if not complete:
        return 1, receipt
    return (0 if receipt["ok"] else 3), receipt


def active_unadopted_report(
    direction_repo: str, since: dt.datetime, *, fetch: Callable[[list[str]], Any], include_archived: bool = False,
    reads: github_next_reads.NextReads | None = None,
) -> dict[str, Any]:
    """Stale waits and gate wording for active repositories without a DIRECTION.md.

    Most milestone work lives in repositories that never adopted their own file.
    This reads them the way `--stale-waits-only` and the gate-phrase check do,
    without stamping, adopting, or reporting them as missing a file.
    """
    owner = direction_repo.split("/")[0]
    rows: list[dict[str, Any]] = []
    repos, cut = owner_repositories(owner, fetch=fetch, include_archived=include_archived)
    unavailable = repository_listing_gaps(cut, fetch=fetch)
    def read_repository(key: str) -> dict[str, Any] | None:
        meta = repos[key]
        name = str(meta.get("full_name"))
        pushed = _parse_time(meta.get("pushed_at"))
        # This report is about active adoption; historical scope must not
        # turn a frozen repository into active work.
        if not repository_scope.includes_repository(meta) or (pushed is not None and pushed < since) or key == direction_repo.casefold():
            return None
        try:
            if merged_direction(name, fetch=fetch) is not None:
                return None
            issues, issues_cut = fetch_paginated(f"repos/{name}/issues?state=open", fetch=fetch)
            milestones, milestones_cut = fetch_paginated(f"repos/{name}/milestones?state=open", fetch=fetch, max_pages=2)
        except AuditError:
            return {"unavailable": {"repo": name, "source": "issues", "reason": "unavailable"}}
        gates = []
        for item in issues:
            if "pull_request" not in item:
                found = gate_phrases(f"{item.get('title') or ''}\n{item.get('body') or ''}")
                gates += [{"number": item.get("number"), "title": item.get("title"), "phrases": found}] if found else []
        for item in milestones:
            found = gate_phrases(str(item.get("description") or ""))
            gates += [{"milestone": item.get("number"), "title": item.get("title"), "phrases": found}] if found else []
        return {
            "repo": name,
            "gate_phrases": gates,
            "gate_phrases_complete": not (issues_cut or milestones_cut),
            "stale_wait_report": stale_wait_report(issues, name, fetch=fetch, inventory_complete=not issues_cut),
        }

    results = reads.map(read_repository, sorted(repos)) if reads else list(map(read_repository, sorted(repos)))
    for result in results:
        if result is None:
            continue
        if "unavailable" in result:
            unavailable.append(result["unavailable"])
        else:
            rows.append(result)
    return {
        "since": since.isoformat().replace("+00:00", "Z"),
        "complete": not unavailable and all(
            row["gate_phrases_complete"] and row["stale_wait_report"]["complete"] for row in rows
        ),
        "repositories": rows,
        "unavailable": unavailable,
    }


def fetch_capacity(
    direction_repo: str, since: dt.datetime, until: dt.datetime, *, fetch: Callable[[list[str]], Any], include_archived: bool = False,
    reads: github_next_reads.NextReads | None = None, tree_lines: Callable[[str, str], int] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Read merged direction, its Track graph and per-repository listings."""
    fetch = github_work_graph.cached_reads(fetch)
    owner = direction_repo.split("/")[0].casefold()
    coverage_errors: list[str] = []
    text = merged_direction(direction_repo, fetch=fetch)
    all_reporting_errors: list[str] = []
    reporting_error = None
    try:
        reporting_rules = direction_reporting.reporting_map(text or "")
    except ValueError as exc:
        reporting_rules = []
        reporting_error = str(exc)
        all_reporting_errors.append("capacity_reporting_map")
    own_label = own_project_label(direction_repo)
    try:
        own_projects = own_project_patterns(text or "", own_label=own_label)
    except AuditError:
        own_projects = []
        coverage_errors.append("capacity_own_projects")
    tracked_issues, graph_errors = tracked_capacity_issues(direction_repo, text or "", since, fetch=fetch, reads=reads)
    coverage_errors.extend(graph_errors)
    repos, repositories_cut = owner_repositories(owner, fetch=fetch, include_archived=include_archived)
    if repositories_cut:
        coverage_errors.append("capacity_repositories")
    def older(field: str) -> Callable[[dict[str, Any]], bool]:
        return lambda item: (moment := _parse_time(item.get(field))) is not None and moment < since

    all_pulls: dict[str, list[dict[str, Any]]] = {}
    all_milestones: dict[str, list[dict[str, Any]]] = {}
    all_events: dict[str, list[dict[str, Any]]] = {}
    all_own_issues: set[tuple[str, int]] = set()

    def read_repository(meta: dict[str, Any]) -> dict[str, Any]:
        pulls: dict[str, list[dict[str, Any]]] = {}
        milestones: dict[str, list[dict[str, Any]]] = {}
        events: dict[str, list[dict[str, Any]]] = {}
        own_issues: set[tuple[str, int]] = set()
        linked_labels: dict[tuple[str, int], set[str] | None] = {}
        admissions: dict[tuple[str, int], str | None] = {}
        truncated: list[str] = []
        reporting_errors: list[str] = []

        def mark_reporting(repo_name: str, merged: dict[str, Any]) -> None:
            """Additional evidence is read only when the Director opted in."""
            try:
                files, files_cut = fetch_paginated(f"repos/{repo_name}/pulls/{merged['number']}/files", fetch=fetch)
                if files_cut or not files or any(not isinstance(row, dict) or not isinstance(row.get("filename"), str) for row in files):
                    raise AuditError("changed files incomplete")
                merged["_report_files"] = files
            except AuditError:
                reporting_errors.append(f"capacity_reporting_files:{repo_name}#{merged['number']}")
            labels = set(_labels(merged))
            labels_complete = isinstance(merged.get("labels"), list)
            for ref in sorted(pull_issue_refs(repo_name, merged)):
                if ref not in linked_labels:
                    try:
                        item = fetch(["api", f"repos/{ref[0]}/issues/{ref[1]}", "--method", "GET"])
                        linked_labels[ref] = _labels(item) if isinstance(item, dict) and isinstance(item.get("labels"), list) else None
                    except AuditError:
                        linked_labels[ref] = None
                labels_for_ref = linked_labels[ref]
                if labels_for_ref is None:
                    labels_complete = False
                    reporting_errors.append(f"capacity_reporting_labels:{ref[0]}#{ref[1]}")
                else:
                    labels.update(labels_for_ref)
            merged["_report_labels"] = labels if labels_complete else None

        def mark_own_links(repo_name: str, merged: dict[str, Any]) -> None:
            """Read labels of linked issues outside own-project repositories, once each."""
            refs = pull_issue_refs(repo_name, merged)
            if (own_repository(repo_name, own_projects) or own_label.casefold() in _labels(merged)
                    or any(own_repository(ref[0], own_projects) for ref in refs)):
                return
            for ref in sorted(refs):
                if ref not in linked_labels:
                    try:
                        issue = fetch(["api", f"repos/{ref[0]}/issues/{ref[1]}", "--method", "GET"])
                        linked_labels[ref] = _labels(issue) if isinstance(issue, dict) else None
                    except AuditError:
                        linked_labels[ref] = None
                labels_for_ref = linked_labels[ref]
                if labels_for_ref is None:
                    truncated.append(f"capacity_issue_labels:{ref[0]}#{ref[1]}")
                if own_label.casefold() in (linked_labels[ref] or set()):
                    own_issues.add(ref)
                    return

        name = str(meta.get("full_name"))
        pushed = _parse_time(meta.get("pushed_at"))
        quiet = pushed is not None and pushed < since
        try:
            # A merge pushes to the base branch, so a repository with no push
            # since the window opened has no merged pull request in it.
            if not quiet:
                pulls[name], cut = fetch_paginated(
                    f"repos/{name}/pulls?state=closed&sort=updated&direction=desc",
                    fetch=fetch, stop=older("updated_at"),
                )
                truncated += [f"capacity_pulls:{name}"] if cut else []
                for pull in {row.get("number"): row for row in pulls[name]}.values():
                    if (github_plan_claim.is_train_batch(pull)
                            or not _within(pull.get("merged_at"), since, until, end_exclusive=True)):
                        continue
                    pull["_report_links_complete"] = True
                    try:
                        pull["_native_issue_refs"] = native_pull_issue_refs(name, pull["number"], fetch=fetch)
                    except AuditError:
                        truncated.append(f"capacity_pull_links:{name}#{pull.get('number')}")
                        pull["_report_links_complete"] = False
                    if not pull_issue_refs(name, pull) & tracked_issues:
                        mark_own_links(name, pull)
                    pull["_admission_ranks"] = set()
                    for ref in sorted(pull_issue_refs(name, pull)):
                        try:
                            if ref not in admissions:
                                admissions[ref] = latest_issue_admission(*ref, fetch=fetch)
                            admission = admissions[ref]
                            if admission is not None:
                                pull["_admission_ranks"].add(ADMISSION_RANKS[admission])
                        except AuditError:
                            truncated.append(f"capacity_claim:{ref[0]}#{ref[1]}")
                    if reporting_rules:
                        mark_reporting(name, pull)
            milestones[name], cut = fetch_paginated(f"repos/{name}/milestones?state=closed", fetch=fetch, max_pages=2)
            truncated += [f"capacity_milestones:{name}"] if cut else []
            events[name], cut = reopened_issue_events(name, since, fetch=fetch)
            truncated += [f"capacity_events:{name}"] if cut else []
        except AuditError:
            truncated.append(f"capacity_reads:{name}")
        return {"pulls": pulls, "milestones": milestones, "events": events, "own_issues": own_issues,
                "truncated": truncated, "reporting_errors": reporting_errors}

    results = reads.map(read_repository, list(repos.values())) if reads else list(map(read_repository, repos.values()))
    for result in results:
        all_pulls.update(result["pulls"])
        all_milestones.update(result["milestones"])
        all_events.update(result["events"])
        all_own_issues.update(result["own_issues"])
        coverage_errors.extend(result["truncated"])
        all_reporting_errors.extend(result["reporting_errors"])
    source = f"{direction_repo}:DIRECTION.md@default-branch"
    summary = capacity_summary(
        own_projects=own_projects, tracked_issues=tracked_issues, direction_source=source, pulls=all_pulls,
        milestones=all_milestones, events=all_events, since=since, until=until,
        incomplete=bool(coverage_errors), own_label=own_label, own_issues=all_own_issues,
        direction_text=text or "",
        reporting_rules=reporting_rules, reporting_error=reporting_error,
        reporting_incomplete=bool(all_reporting_errors),
    )
    def read_net_lines(target_repo: str) -> dict[str, Any]:
        try:
            counts = default_branch_net_lines(
                target_repo, str(repos[target_repo.casefold()].get("default_branch") or ""), since, until, fetch=fetch,
            )
        except (AuditError, KeyError, TypeError, ValueError) as line_error:
            return {"status": "unknown", "reason": str(line_error), "tests": None, "other": None, "total": None}
        if counts["total"] > GROWTH_FLOOR_LINES:
            counts["growth"] = read_growth(target_repo, counts)
        return counts

    def read_growth(target_repo: str, counts: dict[str, Any]) -> dict[str, Any]:
        if tree_lines is None:
            return {"status": "unknown", "reason": "repository line count unavailable"}
        try:
            return {"status": "complete", **growth_trigger(counts["total"], tree_lines(target_repo, counts["head"]))}
        except AuditError as size_error:
            return {"status": "unknown", "reason": str(size_error)}

    if reads:
        reads.start_phase("capacity_net_lines")
    names = list(summary["by_repository"])
    line_results = reads.map(read_net_lines, names) if reads else list(map(read_net_lines, names))
    for line_repo, line_counts in zip(names, line_results, strict=True):
        summary["by_repository"][line_repo]["net_lines"] = line_counts
    summary["session_outcomes"] = session_outcomes.weekly(
        github_plan_closure.closed_log(), owner=owner, since=since, until=until,
        comment_url=github_plan_release.COMMENT_URL,
    )
    return summary, coverage_errors + all_reporting_errors


WINDOW_READS = {"recent_closed_audit_issues", "recent_closed_milestone_issues", "milestone_issue_events"}


def held_reads(truncated: list[str]) -> list[str]:
    """The incomplete reads that keep an audit's evidence window open for a rerun."""
    return sorted(item for item in truncated if item in WINDOW_READS or item.startswith("capacity_"))


def audit_one(
    repo: str, *, gh: str, owner: str | None = None, automation: str | None = None, include_archived: bool = False,
    fetch: Callable[[list[str]], Any] | None = None, defer_marker: bool = False,
    reads: github_next_reads.NextReads | None = None,
) -> tuple[int, dict[str, Any]]:
    """Audit one repository and stamp its own window; never the weekly audit.

    Returns the exit code `--repo` uses and the receipt it prints.
    """
    if reads:
        reads.start_phase("repository_setup")
    automation_arg = automation
    owner_reader = pathlib.Path(shutil.which(str(gh)) or gh).resolve() == pathlib.Path(shutil.which("gh") or "gh").resolve()
    fetch = fetch or github_work_graph.cached_reads(lambda args: gh_json(args, gh=gh))
    try:
        # The merged default-branch file is the owner-approved one; a checkout may hold an unapproved edit.
        direction_text = merged_direction(repo, fetch=fetch)
        automation = automation_arg
        if automation is None:
            me = fetch(["api", "user", "--method", "GET"])
            login = me.get("login") if isinstance(me, dict) else None
            automation = login if isinstance(login, str) else None
        truncated: list[str] = []
        if owner_reader and automation_arg is None and (not automation or automation.casefold() != (owner or repo.split("/")[0]).casefold()):
            truncated.append("owner_reader_identity")
        milestones, cut = fetch_paginated(f"repos/{repo}/milestones?state=all", fetch=fetch)
        truncated += ["milestones"] if cut else []
        milestone_lines = parse_direction(direction_text)["milestone_lines"] if direction_text else {}
        previous = previous_audit_stamp(repo)
        now = dt.datetime.now(dt.timezone.utc)
        week_ago = now - dt.timedelta(days=7)
        audit_since = previous if previous and previous.tzinfo and previous <= now else week_ago
        since = min(audit_since, week_ago)
        if reads:
            reads.start_phase("repository_issues")
        issues, issue_truncation = fetch_audit_issues(
            repo, milestones, milestone_lines, since, fetch=fetch, audit_since=audit_since,
        )
        truncated.extend(issue_truncation)
        if reads:
            reads.start_phase("repository_policy")
        pulls, cut = fetch_paginated(f"repos/{repo}/pulls?state=open", fetch=fetch)
        truncated += ["pulls"] if cut else []
        rulesets, cut = (([], False) if github_rulesets.is_upstream_contribution_fork(repo)
                         else fetch_rulesets(repo, fetch=fetch))
        truncated += ["rulesets"] if cut else []
        direction_pulls = direction_pull_requests(repo, pulls, fetch=fetch)
        capacity = None
        unadopted = None
        if repo.split("/")[1].casefold() == "direction":
            # The next window starts at the persisted marker, whose formatter
            # owns timestamp precision. Use that same instant as this end.
            capacity_until = dt.datetime.fromisoformat(direction_mark.utc_stamp(now))
            if reads:
                reads.start_phase("capacity")
            capacity, cut = fetch_capacity(repo, audit_since, capacity_until, fetch=fetch,
                                           include_archived=include_archived, reads=reads,
                                           tree_lines=lambda target, sha: tarball_text_lines(target, sha, gh=gh))
            truncated.extend(cut)
            if reads:
                reads.start_phase("milestone_outcomes")
            capacity["milestone_outcomes"] = milestone_outcomes(repo, direction_text or "", audit_since, now, fetch=fetch, reads=reads)
            if reads:
                reads.start_phase("active_unadopted")
            unadopted = active_unadopted_report(repo, audit_since, fetch=fetch,
                                               include_archived=include_archived, reads=reads)
    except AuditError as exc:
        error: dict[str, Any] = {"ok": False, "error": str(exc)}
        if gh == str(WRAPPER):
            error["owner_reader_hint"] = "For an owner-only audit, pass --gh gh for read-only GitHub reads; no global fallback setting is needed."
        return 1, error

    if reads:
        reads.start_phase("findings")
    configured_bots = github_identity.configured_bot_logins()
    result = audit(
        direction_text=direction_text,
        milestones=milestones,
        issues=issues,
        owner=owner or repo.split("/")[0],
        automation=automation,
        now=now,
        audit_since=audit_since,
        direction_pulls=direction_pulls,
        truncated=truncated,
        rulesets=rulesets,
        rulesets_unavailable=rulesets is None,
        bot_logins=configured_bots,
        expected_automation=github_identity.automation_login(),
        owner_identity_explicit=owner_reader or automation_arg is not None,
        capacity=capacity,
        repo=repo,
    )
    if reads:
        reads.start_phase("stale_waits")
    result["stale_wait_report"] = stale_wait_report(
        issues, repo, fetch=fetch, inventory_complete="issues" not in truncated,
    )
    if unadopted is not None:
        result["active_unadopted"] = unadopted
    result.update({"repo": repo, "direction_source": f"{repo}:DIRECTION.md@default-branch", "read_only": True})
    result["audit_since"] = audit_since.isoformat().replace("+00:00", "Z")
    # Preserve unseen labeled closures and milestone additions without letting
    # unrelated listing caps keep stale reminders recurring indefinitely.
    # Incomplete capacity reads also keep the window open for a rerun.
    held = held_reads(truncated)
    archived = result["stale_wait_report"]["repository"]["archived"]
    result["marked_skipped"] = ("archived" if archived is True else "window_open" if held
                                else "archive_state_unknown" if archived is not False else None)
    result["marked"] = result["run_recorded"] = result["dropped"] = None
    if defer_marker:
        stamp = direction_mark.utc_stamp(now)
        if archived is True:
            result["_marker_update"] = {"repo": repo, "action": "drop"}
        elif direction_text is not None and archived is False:
            result["_marker_update"] = {"repo": repo, "action": "record", "stamp": stamp,
                                        "held": ", ".join(held) if held else None,
                                        "since": direction_mark.utc_stamp(audit_since) if held else None}
        return (0 if result["ok"] else 3), result
    try:
        result["marked"] = None if result["marked_skipped"] else record_audit(repo, now, direction_text)
        # A held window still records that this audit ran, so it is not read as missed.
        result["run_recorded"] = (record_audit(repo, now, direction_text, ", ".join(held), audit_since)
                                  if held and archived is False else result["marked"])
        result["dropped"] = drop_archived(repo) if archived is True else None
    except AuditError as exc:
        result["ok"] = False
        result["marker_error"] = str(exc)
        print(f"ERROR: {exc}. The reminder still has the previous audit state.", file=sys.stderr)
    return (1 if "marker_error" in result else 0 if result["ok"] else 3), result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit merged direction against GitHub state and record local audit windows.")
    repository_scope.add_include_archived_argument(parser)
    parser.add_argument("--repo", help="OWNER/REPO; defaults to the origin remote of the current checkout")
    parser.add_argument("--owner", help="login treated as the owner; defaults to the repo owner; with --weekly, the account audited")
    parser.add_argument("--weekly", action="store_true",
                        help="audit OWNER/direction and every adopted repository of --owner, and record the weekly audit once all were read")
    parser.add_argument("--automation", help="automation login allowed to create milestones; defaults to the wrapper's account")
    parser.add_argument("--gh", default=str(WRAPPER), help="gh-compatible command used for reads")
    parser.add_argument("--stale-waits-only", action="store_true", help="read parked waits without running or stamping a direction audit")
    parser.add_argument("--stranded-work-only", action="store_true", help="read stranded work and structured reopen triggers without stamping an audit")
    parser.add_argument("--stranded-repo", action="append", default=[], metavar="OWNER/REPO", help="additional repository for the stranded sweep; repeat per repository")
    parser.add_argument("--stranded-owner", help="discover this owner's visible repositories; closed triggers include archived repositories")
    parser.add_argument("--director-name", action="append", default=[], help="known Director alias for structured wait receiver matching")
    parser.add_argument("--question-queue", help="existing Supervisor discussion as OWNER/REPO#NUMBER for the shared delivery view")
    parser.add_argument("--age-hours", type=float, default=2, help="minimum recorded-event age for stranded candidates")
    parser.add_argument("--history-days", type=int, default=30, help="closed-promise history window; open work and indexed reopen triggers have no age cutoff")
    parser.add_argument("--max-requests", type=int, default=800, help="stranded sweep's global request budget")
    parser.add_argument("--deadline-seconds", type=float, default=600, help="stranded sweep's total read deadline")
    parser.add_argument("--apply-next", action="store_true", help="Apply reversible next-record actions in the Director's repositories")
    parser.add_argument("--stranded-format", choices=("json", "markdown"), default="json")
    parser.add_argument("--worktree-root", type=pathlib.Path,
                        help="flag finished task worktrees under this root; defaults to the host's configured hostHelper root")
    parser.add_argument("--prune-unadopted", action="store_true", help="preview removal of archived or unadopted repositories from the local marker")
    parser.add_argument("--apply-prune", action="store_true", help="apply the pruning preview with a recoverable backup")
    parser.add_argument("--remove-missing-repo", action="append", default=[], metavar="OWNER/REPO",
                        help="owner-approved marker removal when this exact repository returns HTTP 404; repeat per repository")
    args = parser.parse_args(argv)

    if args.weekly:
        if args.repo or args.stale_waits_only or args.stranded_work_only or args.prune_unadopted or args.apply_prune or args.remove_missing_repo:
            parser.error("--weekly audits every adopted repository of --owner and cannot run another audit mode")
        if not args.owner or not re.fullmatch(r"[\w.-]+", args.owner):
            parser.error("--weekly requires --owner OWNER")
    if (args.stranded_owner or args.stranded_repo or args.question_queue or args.worktree_root) and not args.stranded_work_only:
        parser.error("stranded inventory scope requires --stranded-work-only")
    if args.apply_next and not args.stranded_work_only:
        parser.error("--apply-next requires --stranded-work-only")
    if args.stranded_work_only:
        if args.stale_waits_only or args.prune_unadopted or args.apply_prune or args.remove_missing_repo:
            parser.error("--stranded-work-only cannot run another audit mode")
        if (not math.isfinite(args.age_hours) or args.age_hours < 0 or args.history_days < 1
                or args.max_requests < 1 or not math.isfinite(args.deadline_seconds) or args.deadline_seconds <= 0):
            parser.error("stranded sweep requires nonnegative finite age and positive bounds")
        repos = [args.repo] if args.repo else []
        repos.extend(args.stranded_repo)
        if ((not repos and not args.stranded_owner)
                or any(not re.fullmatch(r"[\w.-]+/[\w.-]+", value) for value in repos)
                or (args.stranded_owner and not re.fullmatch(r"[\w.-]+", args.stranded_owner))):
            parser.error("stranded sweep requires explicit --repo / --stranded-repo OWNER/REPO")
        if args.question_queue and not re.fullmatch(r"[\w.-]+/[\w.-]+#[1-9][0-9]*", args.question_queue):
            parser.error("--question-queue requires OWNER/REPO#NUMBER")
        import direction_stranded
        import dev_worktree

        worktree_root = args.worktree_root
        if worktree_root is None:
            try:
                root, helper = dev_worktree.configuration()
                worktree_root = root if helper else None
            except (dev_worktree.Refused, OSError, ValueError):
                worktree_root = None
        deadline_at = time.time() + args.deadline_seconds
        reader = github_read.GitHubReader(
            gh_cmd=args.gh, operation="github.direction.stranded_work", strict_actor=True,
            cache_enabled=True, deadline_at=deadline_at,
        )
        result = direction_stranded.collect(
            repos, reader=reader, now=dt.datetime.now(dt.timezone.utc), age_hours=args.age_hours,
            history_days=args.history_days, max_requests=args.max_requests,
            owner=args.stranded_owner, director_names=tuple(args.director_name),
            question_queue=args.question_queue,
            policy_reader=lambda repo: direction_stranded.train_policy(repo, deadline_at=deadline_at),
            director=args.owner, worktree_root=worktree_root, apply_next=args.apply_next,
        )
        print(direction_stranded.markdown(result) if args.stranded_format == "markdown" else json.dumps(result, indent=2))
        return 0 if result["complete"] else 3
    if args.stale_waits_only and (args.prune_unadopted or args.apply_prune or args.remove_missing_repo):
        parser.error("--stale-waits-only cannot prune audit markers")
    if args.apply_prune and not args.prune_unadopted:
        parser.error("--apply-prune requires --prune-unadopted")
    if args.remove_missing_repo and not args.prune_unadopted:
        parser.error("--remove-missing-repo requires --prune-unadopted")
    if any(not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) for repo in args.remove_missing_repo):
        parser.error("--remove-missing-repo requires an exact OWNER/REPO name")
    if args.prune_unadopted:
        import direction_mark as prune_marker

        try:
            result = prune_unadopted(
                prune_marker.marker_path(), fetch=lambda a: gh_json(a, gh=args.gh),
                apply=args.apply_prune, remove_missing_repos=tuple(args.remove_missing_repo),
            )
        except (AuditError, OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 3

    if args.weekly:
        code, result = weekly_audit(args.owner, gh=args.gh, automation=args.automation,
                                    include_archived=args.include_archived, emit=emit_weekly_receipt)
        return code
    repo = args.repo or default_repo(git_root(pathlib.Path.cwd()))
    if not repo:
        print(json.dumps({"ok": False, "error": "could not resolve a repository; pass --repo OWNER/REPO"}))
        return 2

    if args.stale_waits_only:
        fetch = lambda a: gh_json(a, gh=args.gh)  # noqa: E731
        try:
            issues, cut = fetch_paginated(f"repos/{repo}/issues?state=open", fetch=fetch)
            report = stale_wait_report(issues, repo, fetch=fetch, inventory_complete=not cut, read_waits=True)
        except AuditError:
            report = {"read_only": True, "complete": False, "inventory_complete": False,
                      "repository": github_read.repository_disposition(None), "frozen_issues": [],
                      "checked": 0, "items": [], "unavailable": [{"source": "issues", "reason": "unavailable"}]}
        print(json.dumps({"repo": repo, "read_only": True, "stale_wait_report": report}, indent=2, sort_keys=True))
        return 0
    code, result = audit_one(repo, gh=args.gh, owner=args.owner, automation=args.automation,
                             include_archived=args.include_archived)
    print(json.dumps(result, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
