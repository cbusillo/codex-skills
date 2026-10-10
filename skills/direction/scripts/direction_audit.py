#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Read-only audit of a repository's DIRECTION.md against GitHub state.

Reports, as JSON, where milestones, escalations, and issue text have drifted
from the direction file. It never writes to GitHub or to the checkout.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import pathlib
import re
import runpy
import shutil
import subprocess
import sys
from typing import Any, Callable

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
GITHUB_SCRIPTS = REPO_ROOT / "skills" / "github" / "scripts"
if str(GITHUB_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(GITHUB_SCRIPTS))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skills.github.scripts import github_identity, github_rulesets, github_direction_next, github_plan_claim, github_read
from skills.direction.scripts import direction_mark

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
WAIT_REFERENCE = re.compile(
    r"https://github\.com/([\w.-]+/[\w.-]+)/(issues|pull)/(\d+)"
    r"|(?<![\w/])([\w.-]+/[\w.-]+)?#(\d+)\b"
)

STATUS_FIELD = r"(?:State|Next action|Blocked by|Waiting for|Parked until|Waiting since|Last verified|Validation|Evidence|Retention|Recovery|Worker|Session|Branch)"

# The overall direction owns classification; repositories can contain mixed work.
RANKS = ("milestone", "tooling", "own")
OWN_SHARE_FLOOR = 0.20
# GitHub's revert button titles the pull request `Revert "..."` and writes
# `Reverts OWNER/REPO#N`; `git revert` writes `This reverts commit <sha>`.
REVERT_TITLE = re.compile(r"^\s*revert\b", re.I)
REVERT_BODY = re.compile(r"^\s*Reverts [\w.-]+/[\w.-]+#\d+|\bThis reverts commit [0-9a-f]{7,40}\b", re.I | re.M)


class AuditError(Exception):
    pass


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
    if truncated:
        findings.append({"kind": "coverage_incomplete", "detail": "a bounded read was truncated or unavailable; drift beyond verified coverage is unreported", "listings": sorted(truncated)})
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
        if rulesets_unavailable:
            findings.append({
                "kind": "ruleset_unavailable",
                "detail": "GitHub's plan for this private repository does not offer rulesets; "
                          "nothing enforces owner review of DIRECTION.md",
            })
        elif rulesets is not None:
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
        if state == "open" and "plan:waiting" in labels and issue.get("_blocking_work_elsewhere"):
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
                and not str(issue.get("title", "")).startswith("Track:")):
            compact = github_direction_next.compact_list_issue(str(issue.get("repo") or owner), issue)
            compact["plan_status"] = issue.get("_plan_status")
            if compact["plan_status"] == "waiting":
                compact["exclusion"] = "waiting"
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
    if capacity is not None:
        if capacity["own_share_floor"] == "below":
            findings.append({"kind": "own_share_below_floor", "own_share": capacity["own_share"], "floor": OWN_SHARE_FLOOR})

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
        "own_share_below_floor": 10,
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


def gh_json(args: list[str], *, gh: str) -> Any:
    proc = subprocess.run([gh, *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise AuditError(f"{gh} {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise AuditError(f"non-JSON output from {gh} {' '.join(args[:3])}") from exc


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
    match = re.search(r"github\.com[:/]([^/]+)/([^/.]+)(?:\.git)?$", proc.stdout.strip()) if proc.returncode == 0 else None
    return f"{match.group(1)}/{match.group(2)}" if match else None


def git_root(start: pathlib.Path) -> pathlib.Path:
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return pathlib.Path(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else start


def record_audit(repo: str, started_at: dt.datetime, direction_text: str | None) -> str | None:
    """Stamp this repository's audit in the local marker the session-start hook reads.

    The stamp is written here, not by hand, so an audit stamp means an audit ran.
    GitHub is untouched; the marker is local state under the catalog home.
    """
    if direction_text is None:
        return None
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("direction_mark", pathlib.Path(__file__).with_name("direction_mark.py"))
        if spec is None or spec.loader is None:
            return None
        mark = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mark)
        path = mark.marker_path()
        # Retain closures that happen while listings are being fetched for the
        # next audit instead of skipping ahead to the completion time.
        mark.mark_audit(path, repo, started_at)
        return str(path)
    except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError):
        return None


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
    """Preview confirmed missing direction files; preserve unreadable repositories."""
    original = path.read_bytes()
    current = json.loads(original)
    if not isinstance(current, dict) or not isinstance(current.get("audits"), dict):
        raise AuditError("marker must contain an audits object; no changes made")
    removed: list[str] = []
    retained: list[str] = []
    unknown: dict[str, str] = {}
    for repo in current["audits"]:
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
                    continue
                raise
            if not isinstance(visible, dict) or str(visible.get("full_name", "")).casefold() != repo.casefold():
                raise AuditError("repository visibility could not be confirmed")
            try:
                body = fetch(["api", f"repos/{repo}/contents/DIRECTION.md", "--method", "GET"])
            except AuditError as exc:
                if not re.search(r"\bHTTP 404\b", str(exc)):
                    raise
                removed.append(repo)
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
                del current["audits"][repo]
            direction_mark.save(path, current)
    return {"ok": not unknown, "applied": apply and bool(removed), "removed": removed,
            "retained": retained, "unknown": unknown, "backup": backup}


def enrich_waiting_inbound_blockers(
    issues: list[dict[str, Any]], repo: str, *, fetch: Callable[[list[str]], Any],
    max_issues: int = MAX_INBOUND_ISSUES,
) -> bool:
    """Read native inbound dependencies for waiting issues, including unmilestoned ones."""
    incomplete = False
    examined = 0
    for issue in issues:
        labels = {str(label.get("name", "")).lower() for label in issue.get("labels") or []}
        if ("pull_request" in issue or issue.get("state", "open") != "open"
                or "plan:waiting" not in labels
                or (issue.get("issue_dependencies_summary") or {}).get("blocking") == 0):
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


def parked_issue(issue: dict[str, Any]) -> bool:
    labels = {label.casefold() for label in github_direction_next.normalize_labels(issue.get("labels"))}
    return ("pull_request" not in issue and issue.get("state", "open") == "open"
            and bool(labels & {"plan:waiting", "plan:blocked"}))


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
                         read: Callable[[str], Any]) -> dict[str, Any] | None:
    """A merge makes an old status suspect, never proves the finish line."""
    # Normalize field emphasis without rewriting reference URLs or body text.
    status = re.sub(rf"(?m)^(\s*(?:[-*]\s+)?)(?:\*\*|__)({STATUS_FIELD})(?::(?:\*\*|__)|(?:\*\*|__):)", r"\1\2:", status)
    next_action = re.search(
        rf"(?ims)^\s*(?:[-*]\s+)?Next action:\s*(.*?)(?=^\s*(?:[-*]\s+)?{STATUS_FIELD}:|\Z)", status,
    )
    def references(reference_text: str) -> dict[tuple[str, int], str | None]:
        found: dict[tuple[str, int], str | None] = {}
        for ref in WAIT_REFERENCE.finditer(reference_text):
            key = ((ref.group(1) or ref.group(4) or repo).casefold(), int(ref.group(3) or ref.group(5)))
            if found.get(key) != "pull":
                found[key] = ref.group(2)
        return found

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

    agent_wait = agent_delivery_wait(status, repo, issue["number"])
    if agent_wait and ";" in agent_wait:
        for (target_repo, number) in references(agent_wait):
            target = read(f"repos/{target_repo}/issues/{number}")
            if not isinstance(target, dict) or target.get("state") not in {"open", "closed"}:
                raise AuditError("unreadable delegated acceptance issue")
            if "pull_request" in target:
                return None
    # Delivery targets own the stack guard and implementation association;
    # historical PRs elsewhere in status cannot cancel or authorize selection.
    refs = references(next_action[1]) if next_action else {}
    if not refs:
        refs = references(status)
    pulls: dict[tuple[str, int], dict[str, Any]] = {}
    for (target_repo, number), kind in refs.items():
        if kind != "pull":
            target = read(f"repos/{target_repo}/issues/{number}")
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
                target = read(f"repos/{target_repo}/issues/{number}")
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
    hold_pending = False
    if agent_wait and next_action:
        hold_pending |= bool(re.search(r"\b(?:after|until|once|when|accept\w*|approv\w*|decision)\b", next_action[1], re.I))
    for match in re.finditer(
        rf"(?ims)^\s*(?:[-*]\s+)?(Waiting for|Blocked by|Parked until):\s*(.*?)(?=^\s*(?:[-*]\s+)?{STATUS_FIELD}:|\Z)", status,
    ):
        reason = match[2].strip()
        hold_pending |= not (github_plan_claim.no_wait_reason(reason, field="Waiting for")
                             or github_direction_next.non_external_wait(reason)
                             or (match[1].casefold() == "waiting for" and reason == agent_wait))
    for match in re.finditer(
        rf"(?ims)^\s*(?:[-*]\s+)?(?:State|Validation):\s*(.*?)(?=^\s*(?:[-*]\s+)?{STATUS_FIELD}:|\Z)", status,
    ):
        text = re.sub(r"\bno (?:failed|pending)(?:[ /]+(?:failed|pending))* (?:checks|workflows)\b", "", match[1], flags=re.I)
        hold_pending |= bool(re.search(r"\b(?:pending|awaiting|remaining|remainder|split|not yet|still required)\b", text, re.I))
    if agent_wait:
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


def stale_wait_report(
    issues: list[dict[str, Any]], repo: str, *, fetch: Callable[[list[str]], Any],
    inventory_complete: bool = True, max_issues: int = MAX_PAGES * 100,
    active_label: str = "plan:active",
) -> dict[str, Any]:
    """Report verifiably obsolete waits, never release or select their work.

    All recorded holds and native blockers must be satisfied or agent-only.
    Unknown prose, history, and elapsed time cannot prove acceptance.
    """
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    cache: dict[str, Any] = {}

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
                "checked": 0, "checked_issues": [], "items": [], "unavailable": [],
                "inventory_complete": inventory_complete}
    for issue in issues:
        labels = {label.casefold() for label in github_direction_next.normalize_labels(issue.get("labels"))}
        active = (active_label.casefold() in labels and "pull_request" not in issue
                  and issue.get("state", "open") == "open")
        if not parked_issue(issue) and not active:
            continue
        number = issue["number"]
        if checked >= max_issues:
            errors.append({"number": number, "source": "issue", "reason": "issue_limit"})
            continue
        checked += 1
        checked_issues.append(number)
        sections = github_direction_next.section_map(str(issue.get("body") or ""))
        status = next((text for title, text in sections.items() if title.casefold() == "current status"), "")
        normalized_status = re.sub(r"\*\*(Waiting for)(:?)\*\*(:?)", r"\1\2\3", status, flags=re.I)
        if (active and not parked_issue(issue)) or agent_delivery_wait(normalized_status, repo, number):
            try:
                row = active_merged_status(issue, repo, status, read=read)
                if row and parked_issue(issue) and row["recorded_hold_pending"]:
                    row = None
                if row:
                    rows.append(row)
            except AuditError:
                errors.append({"number": number, "source": "active_linked_pr", "reason": "unavailable"})
                continue
            if (active and not parked_issue(issue)) or row:
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
            if mechanical:
                for ref in WAIT_REFERENCE.finditer(plain):
                    target_repo = ref.group(1) or ref.group(4) or repo
                    target_number = int(ref.group(3) or ref.group(5))
                    refs[(target_repo, target_number)] = ref.group(2) or "issues"
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
                except AuditError:
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
            "checked": checked, "checked_issues": checked_issues, "items": rows, "unavailable": errors,
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
    if enrich_waiting_inbound_blockers(issues, repo, fetch=fetch):
        truncated.append("waiting_inbound_blockers")
    if enrich_milestone_waits(issues, repo, list(milestone_lines), fetch=fetch,
                              completed_titles=[item["title"] for item in milestones if item.get("state") == "closed"]):
        truncated.append("milestone_waits")
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

    def config(target_repo: str) -> dict[str, Any]:
        if target_repo not in configs:
            configs[target_repo] = planning_config(target_repo)
        return configs[target_repo]

    for item in issues:
        item["_plan_status"] = github_direction_next.next_plan_status(item, config(repo))

    def read_node(target_repo: str, number: int) -> dict[str, Any]:
        nonlocal incomplete
        try:
            raw = seeds.get((target_repo.casefold(), number))
            if raw is None:
                raw = fetch(["api", f"repos/{target_repo}/issues/{number}", "--method", "GET"])
            relations: dict[str, list[dict[str, Any]]] = {}
            for field, endpoint in (("blocked_by", "dependencies/blocked_by"), ("sub_issues", "sub_issues")):
                values, cut = fetch_paginated(f"repos/{target_repo}/issues/{number}/{endpoint}", fetch=fetch, max_pages=2)
                if cut:
                    raise AuditError("milestone wait relationships truncated")
                refs = []
                for value in values:
                    url = str(value.get("html_url") or "")
                    match = re.search(r"github\.com/([^/]+/[^/]+)/issues/(\d+)", url)
                    if not match or value.get("state") not in {"open", "closed"}:
                        raise AuditError("milestone wait relationship identity unknown")
                    refs.append({"repo": match.group(1), "number": int(match.group(2)),
                                 "url": url, "state": value["state"]})
                relations[field] = refs
            return github_direction_next.evaluate_direction_node({**raw, "repo": target_repo}, config=config(target_repo),
                                                                  focus=None, relationships=relations)
        except AuditError:
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


def own_project_patterns(text: str) -> list[str]:
    """Read repository names/patterns from the own-project item in merged Order."""
    order = github_direction_next.section_map(text).get("Order", "")
    match = re.search(r"(?ms)^\d+\.\s+(Own projects\b.*?)(?=^\d+\.\s|\Z)", order)
    if not match or ":" not in match[1]:
        raise AuditError("Order own-project list is missing")
    listing = match[1].split(":", 1)[1].split("This list", 1)[0]
    names = re.findall(r"`([\w.*-]+)`", listing)
    # Parentheses carry explicit identifiers and explanatory prose. Only single
    # repository identifiers in the surrounding comma/and-separated list count.
    plain = re.sub(r"\([^)]*\)", "", listing, flags=re.S)
    plain = re.sub(r"`[^`]*`", "", plain)
    plain = re.sub(r"\band its\b[^,]*", "", plain)
    for part in re.split(r",|\band\b", plain):
        name = part.strip().rstrip(".")
        if re.fullmatch(r"[\w.-]+", name):
            names.append(name)
    if not names:
        raise AuditError("Order own-project list contains no repository identifiers")
    return sorted({name.casefold() for name in names})


def tracked_capacity_issues(
    direction_repo: str, text: str, since: dt.datetime, *, fetch: Callable[[list[str]], Any], max_nodes: int = 500,
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
    seen: set[tuple[str, int]] = set()
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        if len(seen) >= max_nodes:
            errors.append("capacity_graph_limit")
            break
        seen.add(key)
        repo, number = key
        for endpoint in ("dependencies/blocked_by", "sub_issues"):
            try:
                edges, cut = fetch_paginated(f"repos/{repo}/issues/{number}/{endpoint}", fetch=fetch)
                if cut:
                    errors.append(f"capacity_graph:{repo}#{number}:{endpoint}")
                for edge in edges:
                    match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)", str(edge.get("html_url") or ""))
                    if not match or "pull_request" in edge:
                        raise AuditError("capacity graph relationship identity unknown")
                    pending.append((match[1].casefold(), int(match[2])))
            except AuditError:
                errors.append(f"capacity_graph:{repo}#{number}:{endpoint}")
    return seen, sorted(set(errors))


def pull_issue_refs(repo: str, pull: dict[str, Any]) -> set[tuple[str, int]]:
    """Explicit closing/Refs clauses, rather than incidental issue mentions."""
    refs = set(pull.get("_native_issue_refs") or [])
    text = f"{pull.get('title') or ''}\n{pull.get('body') or ''}"
    text = re.sub(r"\[[^\]]*\]\((https://github\.com/[\w.-]+/[\w.-]+/issues/\d+)\)", r"\1", text)
    for clause in re.finditer(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs)\b[ \t]*:?[ \t]*", text, re.I):
        if re.search(r"\b(?:not|never|doesn't|don't)\s*$", text[max(0, clause.start() - 20):clause.start()], re.I):
            continue
        # Only an immediate reference list is a link; list rows may be on the
        # following lines. Prose after the references ends the list.
        tail = re.sub(r"^\s*(?:[-*]\s+)?", "", text[clause.end():])
        while match := re.match(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)|([\w.-]+/[\w.-]+)?#(\d+)\b", tail):
            refs.add(((match[1] or match[3] or repo).casefold(), int(match[2] or match[4])))
            tail = tail[match.end():]
            separator = re.match(r"(?:[ \t]*(?:,|and)[ \t]*|[ \t]*\n\s*(?:[-*]\s+)?)", tail)
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


def _within(value: Any, since: dt.datetime, until: dt.datetime) -> bool:
    moment = _parse_time(value)
    return moment is not None and since <= moment <= until


def capacity_summary(
    *, own_projects: list[str], tracked_issues: set[tuple[str, int]], direction_source: str,
    pulls: dict[str, list[dict[str, Any]]], milestones: dict[str, list[dict[str, Any]]],
    events: dict[str, list[dict[str, Any]]], since: dt.datetime, until: dt.datetime,
    incomplete: bool = False, own_label: str = "own-project", own_issues: frozenset[tuple[str, int]] | set[tuple[str, int]] = frozenset(),
) -> dict[str, Any]:
    """The overall direction's weekly numbers from per-repository listings.

    A mixed repository's own-project work counts as own when the pull request,
    or an issue it links, carries `own_label`, or it links an issue in an
    own-project repository.
    """
    merged_by_rank = dict.fromkeys(RANKS, 0)
    by_repository: dict[str, dict[str, Any]] = {}
    reverts: list[dict[str, Any]] = []
    for repo in sorted(pulls):
        # A pull request updated mid-read can shift into the next page twice.
        unique = {pull.get("number"): pull for pull in pulls[repo]}
        merged = [pull for pull in unique.values() if _within(pull.get("merged_at"), since, until)]
        if not merged:
            continue
        counts = dict.fromkeys(RANKS, 0)
        for pull in merged:
            refs = pull_issue_refs(repo, pull)
            if refs & tracked_issues:
                rank = "milestone"
            elif (own_repository(repo, own_projects) or own_label.casefold() in _labels(pull)
                  or refs & set(own_issues) or any(own_repository(ref, own_projects) for ref, _ in refs)):
                rank = "own"
            else:
                rank = "tooling"
            merged_by_rank[rank] += 1
            counts[rank] += 1
            if REVERT_TITLE.search(str(pull.get("title") or "")) or REVERT_BODY.search(str(pull.get("body") or "")):
                reverts.append({"repo": repo, "number": pull.get("number"), "title": pull.get("title"), "merged_at": pull.get("merged_at")})
        by_repository[repo] = {"merged": len(merged), "merged_by_rank": counts}
    total = sum(merged_by_rank.values())
    own = merged_by_rank["own"]
    own_share = round(own / total, 3) if total else None
    if incomplete:
        # A repository that could not be read could move the share either way.
        floor = "unknown"
    elif not total:
        floor = "no_merges"
    elif own / total >= OWN_SHARE_FLOOR:
        floor = "met"
    else:
        floor = "below"
    closed = [
        {"repo": repo, "number": item.get("number"), "title": item.get("title"), "closed_at": item.get("closed_at")}
        for repo in sorted(milestones) for item in milestones[repo]
        if item.get("state") == "closed" and _within(item.get("closed_at"), since, until)
    ]
    reopened: dict[tuple[str, Any], dict[str, Any]] = {}
    for repo in sorted(events):
        for event in events[repo]:
            subject = event.get("issue") or {}
            if (event.get("event") != "reopened" or "pull_request" in subject
                    or not _within(event.get("created_at"), since, until)):
                continue
            key = (repo, subject.get("number"))
            if key not in reopened or str(event.get("created_at")) > str(reopened[key]["reopened_at"]):
                reopened[key] = {"repo": repo, "number": subject.get("number"), "title": subject.get("title"), "reopened_at": event.get("created_at")}
    return {
        "since": since.isoformat().replace("+00:00", "Z"),
        "until": until.isoformat().replace("+00:00", "Z"),
        "classification_source": direction_source,
        "own_projects": own_projects,
        "own_project_label": own_label,
        "merged_total": total,
        "merged_by_rank": merged_by_rank,
        "own_share": own_share,
        "own_share_floor": floor,
        "by_repository": by_repository,
        "milestones_closed": closed,
        "issues_reopened": sorted(reopened.values(), key=lambda item: (item["repo"], str(item["number"]))),
        "revert_pulls": reverts,
        "provider_capacity_unused": "manual",
    }


def own_project_label(repo: str) -> str:
    """The label that marks own-project work, from the planning helper that creates it."""
    return str(planning_config(repo)["labels"]["own_project"])


def fetch_capacity(
    direction_repo: str, since: dt.datetime, until: dt.datetime, *, fetch: Callable[[list[str]], Any],
) -> tuple[dict[str, Any], list[str]]:
    """Read merged direction, its Track graph and per-repository listings."""
    owner = direction_repo.split("/")[0].casefold()
    truncated: list[str] = []
    text = merged_direction(direction_repo, fetch=fetch)
    try:
        own_projects = own_project_patterns(text or "")
    except AuditError:
        own_projects = []
        truncated.append("capacity_own_projects")
    tracked_issues, graph_errors = tracked_capacity_issues(direction_repo, text or "", since, fetch=fetch)
    truncated.extend(graph_errors)
    try:
        listed, cut = fetch_paginated("installation/repositories", fetch=fetch, key="repositories")
    except AuditError as installation_error:
        # The Director's own login has no installation; it lists what it owns.
        try:
            listed, cut = fetch_paginated("user/repos?affiliation=owner", fetch=fetch)
        except AuditError as exc:
            raise AuditError(f"could not list repositories: {installation_error}; {exc}") from exc
    if cut:
        truncated.append("capacity_repositories")
    repos = {
        str(item.get("full_name")).casefold(): item for item in listed
        if str(((item.get("owner") or {}).get("login")) or "").casefold() == owner
    }
    def older(field: str) -> Callable[[dict[str, Any]], bool]:
        return lambda item: (moment := _parse_time(item.get(field))) is not None and moment < since

    pulls: dict[str, list[dict[str, Any]]] = {}
    milestones: dict[str, list[dict[str, Any]]] = {}
    events: dict[str, list[dict[str, Any]]] = {}
    own_label = own_project_label(direction_repo)
    own_issues: set[tuple[str, int]] = set()
    linked_labels: dict[tuple[str, int], set[str] | None] = {}

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
                if linked_labels[ref] is None:
                    truncated.append(f"capacity_issue_labels:{ref[0]}#{ref[1]}")
            if own_label.casefold() in (linked_labels[ref] or set()):
                own_issues.add(ref)
                return

    for meta in repos.values():
        name = str(meta.get("full_name"))
        pushed = _parse_time(meta.get("pushed_at"))
        quiet = pushed is not None and pushed < since
        updated = _parse_time(meta.get("updated_at"))
        if meta.get("archived") and updated is not None and updated < since:
            # Archiving updates the repository, so it was read-only all window.
            continue
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
                    if not _within(pull.get("merged_at"), since, until):
                        continue
                    if pull_issue_refs(name, pull) & tracked_issues:
                        continue
                    try:
                        pull["_native_issue_refs"] = native_pull_issue_refs(name, pull["number"], fetch=fetch)
                    except AuditError:
                        truncated.append(f"capacity_pull_links:{name}#{pull.get('number')}")
                    if not pull_issue_refs(name, pull) & tracked_issues:
                        mark_own_links(name, pull)
            milestones[name], cut = fetch_paginated(f"repos/{name}/milestones?state=closed", fetch=fetch, max_pages=2)
            truncated += [f"capacity_milestones:{name}"] if cut else []
            events[name], cut = reopened_issue_events(name, since, fetch=fetch)
            truncated += [f"capacity_events:{name}"] if cut else []
        except AuditError:
            truncated.append(f"capacity_reads:{name}")
    source = f"{direction_repo}:DIRECTION.md@default-branch"
    summary = capacity_summary(
        own_projects=own_projects, tracked_issues=tracked_issues, direction_source=source, pulls=pulls,
        milestones=milestones, events=events, since=since, until=until,
        incomplete=bool(truncated), own_label=own_label, own_issues=own_issues,
    )
    return summary, truncated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="OWNER/REPO; defaults to the origin remote of the current checkout")
    parser.add_argument("--owner", help="login treated as the owner; defaults to the repo owner")
    parser.add_argument("--automation", help="automation login allowed to create milestones; defaults to the wrapper's account")
    parser.add_argument("--gh", default=str(WRAPPER), help="gh-compatible command used for reads")
    parser.add_argument("--stale-waits-only", action="store_true", help="read parked waits without running or stamping a direction audit")
    parser.add_argument("--prune-unadopted", action="store_true", help="preview removal of unadopted repositories from the local marker")
    parser.add_argument("--apply-prune", action="store_true", help="apply the pruning preview with a recoverable backup")
    parser.add_argument("--remove-missing-repo", action="append", default=[], metavar="OWNER/REPO",
                        help="owner-approved marker removal when this exact repository returns HTTP 404; repeat per repository")
    args = parser.parse_args(argv)

    if args.stale_waits_only and (args.prune_unadopted or args.apply_prune or args.remove_missing_repo):
        parser.error("--stale-waits-only cannot prune audit markers")
    if args.apply_prune and not args.prune_unadopted:
        parser.error("--apply-prune requires --prune-unadopted")
    if args.remove_missing_repo and not args.prune_unadopted:
        parser.error("--remove-missing-repo requires --prune-unadopted")
    if any(not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) for repo in args.remove_missing_repo):
        parser.error("--remove-missing-repo requires an exact OWNER/REPO name")
    if args.prune_unadopted:
        import direction_mark

        try:
            result = prune_unadopted(
                direction_mark.marker_path(), fetch=lambda a: gh_json(a, gh=args.gh),
                apply=args.apply_prune, remove_missing_repos=tuple(args.remove_missing_repo),
            )
        except (AuditError, OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 3

    repo = args.repo or default_repo(git_root(pathlib.Path.cwd()))
    if not repo:
        print(json.dumps({"ok": False, "error": "could not resolve a repository; pass --repo OWNER/REPO"}))
        return 2

    owner_reader = pathlib.Path(shutil.which(args.gh) or args.gh).resolve() == pathlib.Path(shutil.which("gh") or "gh").resolve()
    fetch = lambda a: gh_json(a, gh=args.gh)  # noqa: E731
    if args.stale_waits_only:
        try:
            issues, cut = fetch_paginated(f"repos/{repo}/issues?state=open", fetch=fetch)
            report = stale_wait_report(issues, repo, fetch=fetch, inventory_complete=not cut)
        except AuditError:
            report = {"read_only": True, "complete": False, "inventory_complete": False,
                      "repository": github_read.repository_disposition(None), "frozen_issues": [],
                      "checked": 0, "items": [], "unavailable": [{"source": "issues", "reason": "unavailable"}]}
        print(json.dumps({"repo": repo, "read_only": True, "stale_wait_report": report}, indent=2, sort_keys=True))
        return 0
    try:
        # The merged default-branch file is the owner-approved one; a checkout may hold an unapproved edit.
        direction_text = merged_direction(repo, fetch=fetch)
        automation = args.automation
        if automation is None:
            me = fetch(["api", "user", "--method", "GET"])
            login = me.get("login") if isinstance(me, dict) else None
            automation = login if isinstance(login, str) else None
        truncated: list[str] = []
        if owner_reader and args.automation is None and (not automation or automation.casefold() != (args.owner or repo.split("/")[0]).casefold()):
            truncated.append("owner_reader_identity")
        milestones, cut = fetch_paginated(f"repos/{repo}/milestones?state=all", fetch=fetch)
        truncated += ["milestones"] if cut else []
        milestone_lines = parse_direction(direction_text)["milestone_lines"] if direction_text else {}
        previous = previous_audit_stamp(repo)
        now = dt.datetime.now(dt.timezone.utc)
        week_ago = now - dt.timedelta(days=7)
        audit_since = previous if previous and previous.tzinfo and previous <= now else week_ago
        since = min(audit_since, week_ago)
        issues, issue_truncation = fetch_audit_issues(
            repo, milestones, milestone_lines, since, fetch=fetch, audit_since=audit_since,
        )
        truncated.extend(issue_truncation)
        pulls, cut = fetch_paginated(f"repos/{repo}/pulls?state=open", fetch=fetch)
        truncated += ["pulls"] if cut else []
        rulesets, cut = fetch_rulesets(repo, fetch=fetch)
        truncated += ["rulesets"] if cut else []
        direction_pulls = direction_pull_requests(repo, pulls, fetch=fetch)
        capacity = None
        if repo.split("/")[1].casefold() == "direction":
            capacity, cut = fetch_capacity(repo, audit_since, now, fetch=fetch)
            truncated.extend(cut)
    except AuditError as exc:
        error: dict[str, Any] = {"ok": False, "error": str(exc)}
        if args.gh == str(WRAPPER):
            error["owner_reader_hint"] = "For an owner-only audit, pass --gh gh for read-only GitHub reads; no global fallback setting is needed."
        print(json.dumps(error))
        return 1

    configured_bots = github_identity.configured_bot_logins()
    result = audit(
        direction_text=direction_text,
        milestones=milestones,
        issues=issues,
        owner=args.owner or repo.split("/")[0],
        automation=automation,
        now=now,
        audit_since=audit_since,
        direction_pulls=direction_pulls,
        truncated=truncated,
        rulesets=rulesets,
        rulesets_unavailable=rulesets is None,
        bot_logins=configured_bots,
        expected_automation=github_identity.automation_login(),
        owner_identity_explicit=owner_reader or args.automation is not None,
        capacity=capacity,
        repo=repo,
    )
    result["stale_wait_report"] = stale_wait_report(
        issues, repo, fetch=fetch, inventory_complete="issues" not in truncated,
    )
    result.update({"repo": repo, "direction_source": f"{repo}:DIRECTION.md@default-branch", "read_only": True})
    result["audit_since"] = audit_since.isoformat().replace("+00:00", "Z")
    # Preserve unseen labeled closures and milestone additions without letting
    # unrelated listing caps keep stale reminders recurring indefinitely.
    # Incomplete capacity reads also keep the window open for a rerun.
    window_reads = {"recent_closed_audit_issues", "recent_closed_milestone_issues", "milestone_issue_events"}
    keep_window = bool(window_reads & set(truncated)) or any(item.startswith("capacity_") for item in truncated)
    result["marked"] = None if keep_window else record_audit(repo, now, direction_text)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
