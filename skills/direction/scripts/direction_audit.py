#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read-only audit of a repository's DIRECTION.md against GitHub state.

Reports, as JSON, where milestones, escalations, and issue text have drifted
from the direction file. It never writes to GitHub or to the checkout.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys
from typing import Any, Callable

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
GITHUB_SCRIPTS = REPO_ROOT / "skills" / "github" / "scripts"
if str(GITHUB_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(GITHUB_SCRIPTS))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skills.github.scripts import github_identity, github_rulesets

REQUIRED_HEADINGS = ("Purpose", "Stop Boundaries", "Journey", "Retired", "Milestones")
ESCALATION_LABEL = "direction"
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


def direction_quotes(body: str) -> list[str]:
    """Read quoted evidence from Markdown blockquotes in an issue body."""
    quotes: list[str] = []
    fenced = False
    for line in body.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if line.startswith(("    ", "\t")):
            continue
        if not fenced and (match := re.match(r"^\s{0,3}>\s*([^>].*?)\s*$", line)):
            quotes.append(match.group(1).strip())
    return quotes


def plain_direction_text(value: str) -> str:
    """Compare a copied rendered phrase with its Markdown source."""
    value = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", value)
    return " ".join(re.sub(r"[`*_]", "", value).split()).lower()


def has_direction_quote(body: str, milestone_line: str) -> bool:
    evidence_line = re.sub(r"^\s*[-*]\s+`[^`]+`\s*", "", milestone_line)
    source = plain_direction_text(evidence_line)
    for quote in direction_quotes(body):
        phrase = plain_direction_text(quote)
        if len(phrase) < 12:
            continue
        position = source.find(phrase)
        if position < 0:
            continue
        before = source[position - 1] if position else " "
        after = source[position + len(phrase):position + len(phrase) + 1] or " "
        if not before.isalnum() and not after.isalnum():
            return True
    return False


def milestone_admission_actor(events: list[dict[str, Any]]) -> str | None:
    """The last milestone assignment actor, including across milestone renames."""
    admissions = [event for event in events if event.get("event") == "milestoned"]
    if not admissions:
        return None
    latest = max(enumerate(admissions), key=lambda pair: (str(pair[1].get("created_at") or ""), pair[0]))[1]
    return str(((latest.get("actor") or {}).get("login")) or "").lower()


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
    direction_pulls: list[dict[str, Any]] | None = None,
    truncated: list[str] | None = None,
    rulesets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    if truncated:
        findings.append({"kind": "coverage_incomplete", "detail": "a listing hit the page cap; drift beyond it is unreported", "listings": sorted(truncated)})
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
        if rulesets is not None:
            for name in github_rulesets.missing_standard_rulesets(rulesets):
                findings.append({
                    "kind": "ruleset_missing",
                    "name": name,
                    "detail": "an active repository branch ruleset with this standard name was not found",
                })

    # The owner's automation can span identities, such as a bot user and a later App.
    bots = {login.lower() for login in (automation, *bot_logins) if login} - {owner.lower()}
    trusted = {owner.lower()} | bots
    if direction_text is not None and listed and not bots:
        findings.append({
            "kind": "coverage_incomplete",
            "detail": "automation identity is indistinguishable from the owner; milestone admission audit cannot classify actors",
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
        milestone_title = str(((issue.get("milestone") or {}).get("title")) or "")
        author = str(((issue.get("user") or {}).get("login")) or "").lower()
        admission_actor = issue.get("_milestone_admitted_by")
        non_owner_admitted = admission_actor is not None and admission_actor != owner.lower()
        bot_authored_without_known_admission = author in bots and admission_actor is None
        if (not issue.get("_admission_unknown") and milestone_title in milestone_lines
                and (non_owner_admitted or bot_authored_without_known_admission)):
            quotes = direction_quotes(str(issue.get("body") or ""))
            if not quotes:
                findings.append({"kind": "milestone_issue_quote_missing", "number": number, "milestone": milestone_title})
            elif not has_direction_quote(str(issue.get("body") or ""), milestone_lines[milestone_title]):
                findings.append({"kind": "milestone_issue_quote_mismatch", "number": number, "milestone": milestone_title})
        if ESCALATION_LABEL in labels and issue.get("state", "open") == "open":
            opened = _parse_time(issue.get("created_at"))
            age_days = (now - opened).days if opened else None
            findings.append({"kind": "escalation_open", "number": number, "title": issue.get("title"), "age_days": age_days})
        text = f"{issue.get('title') or ''}\n{issue.get('body') or ''}"
        phrases = gate_phrases(text)
        if phrases and issue.get("state", "open") == "open":
            findings.append({"kind": "gate_phrase", "number": number, "title": issue.get("title"), "phrases": phrases})

    order = {
        "coverage_incomplete": -1,
        "direction_missing": 0,
        "direction_shape": 1,
        "ruleset_missing": 2,
        "escalation_open": 3,
        "milestone_unlisted": 4,
        "milestone_closed_listed": 5,
        "milestone_pending": 6,
        "milestone_creator": 7,
        "milestone_issue_quote_missing": 8,
        "milestone_issue_quote_mismatch": 8,
        "gate_phrase": 9,
    }
    findings.sort(key=lambda item: (order.get(item["kind"], 99), str(item.get("number") or item.get("milestone") or item.get("title") or "")))
    return {
        "ok": not findings,
        "listed_milestones": listed,
        "open_milestones": sorted(open_titles),
        "findings": findings,
        "counts": _counts(findings),
    }


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


MAX_PAGES = 20


def fetch_paginated(path: str, *, fetch: Callable[[list[str]], Any], max_pages: int = MAX_PAGES) -> tuple[list[dict[str, Any]], bool]:
    """All pages of a listing, and whether the page cap cut it short."""
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        joiner = "&" if "?" in path else "?"
        body = fetch(["api", f"{path}{joiner}per_page=100&page={page}", "--method", "GET"])
        if not isinstance(body, list):
            raise AuditError(f"unexpected response shape for {path}")
        items.extend(body)
        if len(body) < 100:
            return items, False
        if page >= max_pages:
            return items, True
        page += 1


def enrich_admission_actors(
    issues: list[dict[str, Any]], milestone_lines: dict[str, str], repo: str,
    *, fetch: Callable[[list[str]], Any], max_issues: int = MAX_ADMISSION_ISSUES,
) -> bool:
    """Add last admission actors within a bounded event-read budget."""
    incomplete = False
    examined = 0
    recent_first = sorted(issues, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    for issue in recent_first:
        title = ((issue.get("milestone") or {}).get("title"))
        if "pull_request" in issue or title not in milestone_lines:
            continue
        if has_direction_quote(str(issue.get("body") or ""), milestone_lines[title]):
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
        issue["_milestone_admitted_by"] = milestone_admission_actor(events)
    return incomplete


def merged_direction(repo: str, *, fetch: Callable[[list[str]], Any]) -> str | None:
    """DIRECTION.md from the default branch, or None when the repository has none."""
    import base64

    try:
        body = fetch(["api", f"repos/{repo}/contents/DIRECTION.md", "--method", "GET"])
    except AuditError as exc:
        if "404" in str(exc) or "Not Found" in str(exc):
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


def record_audit(repo: str) -> str | None:
    """Stamp this repository's audit in the local marker the session-start hook reads.

    The stamp is written here, not by hand, so an audit stamp means an audit ran.
    GitHub is untouched; the marker is local state under the catalog home.
    """
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("direction_mark", pathlib.Path(__file__).with_name("direction_mark.py"))
        if spec is None or spec.loader is None:
            return None
        mark = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mark)
        path = mark.marker_path()
        mark.mark_audit(path, repo, dt.datetime.now(dt.timezone.utc))
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


def fetch_audit_issues(
    repo: str, milestones: list[dict[str, Any]], milestone_lines: dict[str, str],
    since: dt.datetime, *, fetch: Callable[[list[str]], Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Open issues plus bounded closed milestone issues since the last audit."""
    issues, open_cut = fetch_paginated(f"repos/{repo}/issues?state=open", fetch=fetch)
    truncated = ["issues"] if open_cut else []
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
    issues = list({issue.get("number"): issue for issue in issues}.values())
    if enrich_admission_actors(issues, milestone_lines, repo, fetch=fetch):
        truncated.append("milestone_issue_events")
    return issues, truncated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="OWNER/REPO; defaults to the origin remote of the current checkout")
    parser.add_argument("--owner", help="login treated as the owner; defaults to the repo owner")
    parser.add_argument("--automation", help="automation login allowed to create milestones; defaults to the wrapper's account")
    parser.add_argument("--gh", default=str(WRAPPER), help="gh-compatible command used for reads")
    args = parser.parse_args(argv)

    repo = args.repo or default_repo(git_root(pathlib.Path.cwd()))
    if not repo:
        print(json.dumps({"ok": False, "error": "could not resolve a repository; pass --repo OWNER/REPO"}))
        return 2

    fetch = lambda a: gh_json(a, gh=args.gh)  # noqa: E731
    try:
        # The merged default-branch file is the owner-approved one; a checkout may hold an unapproved edit.
        direction_text = merged_direction(repo, fetch=fetch)
        automation = args.automation
        if automation is None:
            me = fetch(["api", "user", "--method", "GET"])
            login = me.get("login") if isinstance(me, dict) else None
            automation = login if isinstance(login, str) else None
        truncated: list[str] = []
        milestones, cut = fetch_paginated(f"repos/{repo}/milestones?state=all", fetch=fetch)
        truncated += ["milestones"] if cut else []
        milestone_lines = parse_direction(direction_text)["milestone_lines"] if direction_text else {}
        previous = previous_audit_stamp(repo)
        now = dt.datetime.now(dt.timezone.utc)
        week_ago = now - dt.timedelta(days=7)
        since = min(previous, week_ago) if previous and previous <= now else week_ago
        issues, issue_truncation = fetch_audit_issues(repo, milestones, milestone_lines, since, fetch=fetch)
        truncated.extend(issue_truncation)
        pulls, cut = fetch_paginated(f"repos/{repo}/pulls?state=open", fetch=fetch)
        truncated += ["pulls"] if cut else []
        rulesets, cut = fetch_paginated(
            f"repos/{repo}/rulesets?includes_parents=false&targets=branch",
            fetch=fetch,
        )
        truncated += ["rulesets"] if cut else []
        direction_pulls = direction_pull_requests(repo, pulls, fetch=fetch)
    except AuditError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1

    result = audit(
        direction_text=direction_text,
        milestones=milestones,
        issues=issues,
        owner=args.owner or repo.split("/")[0],
        automation=automation,
        now=dt.datetime.now(dt.timezone.utc),
        direction_pulls=direction_pulls,
        truncated=truncated,
        rulesets=rulesets,
        bot_logins=github_identity.configured_bot_logins(),
    )
    result.update({"repo": repo, "direction_source": f"{repo}:DIRECTION.md@default-branch", "read_only": True})
    result["marked"] = record_audit(repo)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
