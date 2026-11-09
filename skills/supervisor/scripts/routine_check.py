#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
import argparse
import importlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import compact_check
import foreign_watch
import github_access
import oq
from question_delivery import run_queue, source_item

if TYPE_CHECKING:
    from skills.github.scripts import github_read
else:
    github_read = importlib.import_module("github_read")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github-work-rollup/scripts"))
radar = importlib.import_module("github_unanswered_comments")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "direction/scripts"))
director_waits = importlib.import_module("director_waits")


READS_AFTER = {"comments": "questions"}
ROUTINE_INTERVAL = timedelta(minutes=30)
OWNER_REVIEW_CHECK = "launchplane/owner-review"
CLIENT_WAIT = re.compile(r"waiting (?:for|on) (?:the |this )?client|client(?:'s)? acceptance", re.I)
NOTHING_REVIEWABLE = re.compile(r"preview failed|nothing\b[^.]*\b(?:reviewable|to review)", re.I)
RELEASE_REQUEST = re.compile(r"\b(?:releas(?:e|es|ing)|promot(?:e|es|ing|ion))\b(?!\s+(?:(?:this|the|its|our|my)\s+)?claims?\b)", re.I)
SETTING_CHANGE = re.compile(r"\b(?:set|sets|setting|change|changes|flip|flips|toggle|toggles)\b[^.;\n]*?(?<![-\w])[a-z][a-z0-9_]*=[a-z0-9]+\b", re.I)
SHA = re.compile(r"[0-9a-f]{40}")
REFUSED_READ = re.compile(r"production read refused:\s*(\S.*)", re.I)
REFUSED_READ_ROUTE = ("confirm the overall direction permits this read, then start it with iterm_tab.py --brief-issue on a session that may run it, "
                      "within claim, cap and account rules; the refused session never retries it")


def clock() -> datetime:
    return datetime.now().astimezone()


def clock_label(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %-I:%M %p %Z").strip()


def age_text(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    return f"{minutes // 1440}d{minutes % 1440 // 60}h" if minutes >= 1440 else f"{minutes // 60}h{minutes % 60:02d}m"


def recorded_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def supervisor_actors() -> set[str]:
    native = (os.environ.get(name) for name in ("CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID"))
    return {"agent:supervisor", *(f"agent:{value}".casefold() for value in native if value)}


def read_source(read: Callable[[], dict], prerequisite: Future | None) -> dict:
    if prerequisite is not None:
        prerequisite.result()
    try:
        return read()
    except (Exception, SystemExit) as error:  # noqa: BLE001
        return {"error": str(error), "coverage": {"complete": False}}


def collect(readers: dict[str, Callable[[], dict]]) -> dict[str, dict]:
    futures: dict[str, Future] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(readers))) as pool:
        for name, read in readers.items():
            futures[name] = pool.submit(read_source, read, futures.get(READS_AFTER.get(name, "")))
    return {name: future.result() for name, future in futures.items()}


def watch_report(path: Path, *, service: bool = False, window: str = radar.DEFAULT_WINDOW, now: datetime | None = None) -> dict:
    notices: dict[str, dict] = {}
    latest: dict = {"errors": ["log has no complete reports"], "coverage_gaps": []}
    incomplete = []
    runner_error = None
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            incomplete.append(f"watch log line {number} is incomplete or malformed; prior notices retained")
            continue
        if isinstance(row, dict) and row.get("event") in {"started", "completed", "stopped", "reconciliation_required"}:
            if row["event"] in {"stopped", "reconciliation_required"} or row["event"] == "completed" and row.get("exit_code") != 0:
                runner_error = f"service stopped: {row['event']} (exit {row.get('exit_code')})"
        elif service and isinstance(row, dict) and type(row.get("ok")) is bool:
            latest = {"errors": [] if row["ok"] else [f"verification failed at {row.get('gate', 'unknown gate')}"],
                      "coverage_gaps": []}
            if row["ok"]:
                runner_error = None
        elif isinstance(row, dict) and row.get("error"):
            latest = {"errors": [row["error"]], "coverage_gaps": []}
        elif isinstance(row, dict) and all(isinstance(row.get(key), list) for key in ("notices", "errors", "coverage_gaps")):
            latest = row
            runner_error = None
            for notice in row["notices"]:
                if isinstance(notice, dict):
                    notices[pointer(notice)] = notice
                else:
                    incomplete.append(f"watch log line {number} has an invalid notice")
        else:
            incomplete.append(f"watch log line {number} has an unknown shape")
    since = (now or datetime.now(UTC)) - timedelta(hours=radar.rollup.parse_duration_hours(window))
    notifications = []
    for notice in notices.values():
        if notice.get("kind") not in {"launchplane", "director_mention"}:
            continue
        try:
            updated = datetime.fromisoformat(notice["updated_at"].replace("Z", "+00:00"))
            if updated.tzinfo is None:
                raise ValueError("notification timestamp has no timezone")
            if updated >= since:
                notifications.append(notice)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            incomplete.append(f"watch notification {pointer(notice)}: {error}")
    return {
        **latest,
        "errors": [*latest["errors"], *incomplete, *([runner_error] if runner_error else [])],
        "history": list(notices.values()),
        "notices": notifications,
    }


def source_errors(name: str, report: dict) -> list[str]:
    errors = [f"ERROR {name}: {report['error']}"] if report.get("error") else []
    coverage = report.get("coverage", {})
    for key in ("errors", "source_errors", "coverage_gaps", "untrusted_records", "unnumbered_sources", "untrusted_sources"):
        for error in report.get(key, []):
            errors.append(f"ERROR {name} {key}: {json.dumps(error, sort_keys=True)}")
    if isinstance(coverage, dict):
        errors.extend(f"ERROR {name}: {json.dumps(error, sort_keys=True)}" for error in coverage.get("errors", []))
        if coverage.get("complete") is False:
            errors.append(f"ERROR {name}: coverage incomplete")
    if report.get("ok") is False or report.get("complete") is False:
        errors.append(f"ERROR {name}: reader incomplete")
    if name == "next":
        for key in ("candidate_coverage", "graph_context", "discovery_context", "queue_context", "stale_wait_report",
                    "live_breakage_context", "focus_context", "performance"):
            errors.extend(coverage_errors(report.get(key, {}), f"next {key}"))
    return errors


def coverage_detail(value: Any) -> Any:
    if isinstance(value, list):
        return [coverage_detail(item) for item in value]
    if isinstance(value, dict):
        return {key: coverage_detail(item) for key, item in value.items()
                if key not in {"direction", "discussion", "body", "comments", "items", "checked_issues", "references"}}
    return value


def coverage_errors(value: Any, scope: str) -> list[str]:
    if isinstance(value, dict):
        if (value.get("complete") is False or value.get("errors") or value.get("error")
                or value.get("truncated") is True or value.get("deadline_exhausted") is True):
            return [f"ERROR {scope}: {json.dumps(coverage_detail(value), sort_keys=True)}"]
        return [error for key, item in value.items() for error in coverage_errors(item, f"{scope}.{key}")]
    if isinstance(value, list):
        return [error for index, item in enumerate(value) for error in coverage_errors(item, f"{scope}[{index}]")]
    return []


def staffing(owner: str) -> dict:
    helper = Path(__file__).resolve().parents[2] / "github/scripts/gh-plan.py"
    result = subprocess.run(["uv", "run", str(helper), "--repo", f"{owner}/direction", "next",
                             "--limit", str(sys.maxsize), "--time-budget", "120"], capture_output=True, text=True, timeout=180)
    report = json.loads(result.stdout)
    if not isinstance(report, dict):
        raise ValueError(f"Global next returned no portfolio report for {owner}")
    if result.returncode:
        report["ok"] = False
    return report


def pointer(row: dict) -> str:
    return str(row.get("comment_url") or row.get("url") or row.get("session_id") or json.dumps(row, sort_keys=True))


def refused_read(row: dict) -> str:
    queue = row.get("queue") or {}
    # A request naming its lead keeps the worker's words here once the queue rewrites next_action.
    named = queue.get("reason") == "named_recipient_requires_reconciliation"
    match = REFUSED_READ.match(queue.get("recorded_next_action" if named else "next_action") or "")
    return " ".join(match[1].split()) if match else ""


def queue_rows(selection: dict) -> list[tuple[str, dict]]:
    rows: dict[str, tuple[str, dict]] = {}
    actors = supervisor_actors()
    for field in ("candidates", "excluded", "needs_status"):
        for row in selection.get(field, []):
            queue = row.get("queue", {})
            if str(row.get("state", "")).casefold() != "open":
                continue
            if field == "excluded" and row.get("exclusion") not in {"assigned_elsewhere", "agent_assignment_unresolved"}:
                continue
            actor = queue.get("next_actor") or ""
            if refused_read(row) and actor.startswith("agent") and queue.get("state") in {"available", "review", "needs_status"}:
                rows[pointer(row)] = ("PRODUCTION_READ_REFUSED", row)
            elif actor.casefold() in actors and queue.get("state") in {"available", "review", "needs_status"}:
                rows[pointer(row)] = ("SUPERVISOR_ACTION", row)
            elif row.get("record_type") == "pull_request":
                continue
            elif row.get("exclusion") == "agent_assignment_unresolved" and len(row.get("agent_labels", [])) > 1:
                rows.setdefault(pointer(row), ("CONFLICT", row))
            elif queue.get("state") == "available" and actor.startswith("agent"):
                rows.setdefault(pointer(row), ("NEXT", row))
            elif queue.get("state") in {"review", "needs_status"}:
                rows.setdefault(pointer(row), ("REVIEW" if actor.startswith("agent") else "NEEDS_STATUS", row))
    return list(rows.values())


def client_wait_rows(selection: dict) -> list[dict]:
    return [row for row in selection.get("pr_followthrough", [])
            if row.get("reason") == "current_head_client_wait" or (row.get("queue") or {}).get("reason") == "current_head_client_wait"]


def milestone_waits(selection: dict) -> list[dict]:
    return [wait for milestone in selection.get("milestone_summary", [])
            for wait in [*milestone.get("waits", []), *milestone.get("wait_reviews", [])]]


def request_text(row: dict) -> str:
    queue = row.get("queue") or {}
    waits = (row.get("wait") or queue.get("wait") or {}).get("waits", [])
    return " ".join(str(part) for part in (queue.get("next_action"), row.get("waiting_for"), *(wait.get("waiting_for") for wait in waits)) if part)


def primary_reads(selection: dict, sessions: list[dict], *, owner_review: Callable[[str, int], dict | None],
                  client_release: Callable[[str], Any], production: Callable[[str], dict],
                  anchor_response: Callable[[str], str]) -> dict:
    errors: list[dict] = []
    client_waits = {pointer(row) for row in client_wait_rows(selection)}
    rows = [*(row for _, row in queue_rows(selection)), *client_wait_rows(selection), *milestone_waits(selection)]

    def attempt(label: str, read: Callable[[], Any]) -> Any:
        try:
            return read()
        except (Exception, SystemExit) as error:  # noqa: BLE001
            errors.append({"read": label, "error": str(error)[:200]})
            return None

    pull_rows = {(row["repo"], row["number"]) for row in rows if row.get("record_type") == "pull_request" and row.get("repo")
                 and (pointer(row) in client_waits or CLIENT_WAIT.search(request_text(row)))}
    client_repos = {repo for repo, _ in pull_rows}
    release_repos = {row["repo"] for row in rows if row.get("repo") and RELEASE_REQUEST.search(request_text(row))}
    with ThreadPoolExecutor(max_workers=8) as pool:
        reviews = dict(zip(pull_rows, pool.map(lambda key: attempt(f"owner-review {key[0]}#{key[1]}", lambda: (owner_review(*key),)), pull_rows), strict=True))
        releases = dict(zip(client_repos, pool.map(lambda repo: attempt(f"client release {repo}", lambda: client_release(repo)), client_repos), strict=True))
        productions = dict(zip(release_repos, pool.map(lambda repo: attempt(f"production {repo}", lambda: production(repo)), release_repos), strict=True))

    def owner_review_note(row: dict) -> str:
        if row.get("record_type") != "pull_request":
            return "owner-review=UNKNOWN (not a pull request)"
        read = reviews.get((row["repo"], row["number"]))
        check = read[0] if read else None
        release = releases.get(row["repo"])
        state = getattr(release, "state", "unknown")
        if read is None:
            note = "owner-review=UNKNOWN (read failed)"
        elif check is None:
            note = "owner-review=UNKNOWN (no check on the current head)"
        else:
            note = f"owner-review={read[0]['status']}/{read[0].get('conclusion') or '-'} \"{read[0].get('title') or 'UNKNOWN'}\""
        if state == "unknown":
            note += f" client_release=UNKNOWN ({getattr(release, 'reason', '') or 'read failed'})"
        if NOTHING_REVIEWABLE.search(str((check or {}).get("title") or "")) or state == "director_standing":
            note += " WRONG_WAIT"
        return note + (" (Director standing)" if state == "director_standing" else "")

    def production_note(repo: str) -> str:
        read = productions.get(repo)
        if not isinstance(read, dict):
            return "production=UNKNOWN (read failed)"
        if read.get("reason"):
            return f"production=UNKNOWN ({read['reason']})"
        note = f"production={read['deployed'][:8]} newest_landed={read['newest'][:8]}"
        return note + (" NOTHING_TO_RELEASE" if read["contained"] else f" not_released={read.get('behind', 'UNKNOWN')}")

    annotations = {}
    for row in rows:
        text = request_text(row)
        notes = []
        if pointer(row) in client_waits or CLIENT_WAIT.search(text):
            notes.append(owner_review_note(row))
        if RELEASE_REQUEST.search(text):
            notes.append(production_note(row.get("repo", "")) if row.get("repo") else "production=UNKNOWN (no repository)")
        if SETTING_CHANGE.search(text):
            notes.append("SETTING_UNVERIFIED (read the registry line's reason and name the service that acts on it)")
        if notes:
            annotations[pointer(row)] = " | ".join(notes)
    anchors = {row["session_id"]: attempt(f"anchor {row['issue']}", lambda issue=row["issue"]: anchor_response(issue)) or "anchor_response=UNKNOWN"
               for row in sessions if row.get("action") == "inspect_idle_session" and row.get("issue")}
    return {"annotations": annotations, "anchors": anchors, "errors": errors, "coverage": {"complete": not errors}}


def ask_note(row: dict) -> str:
    ask = row.get("ask") or {}
    if not ask:
        return ""
    if ask["ask_url"] is None:
        return " | ask=MISSING"
    replied = {True: "yes", False: "no"}.get(ask["replied_since"], "UNKNOWN")
    return f" | ask={ask['ask_url']} by {ask['author'] or 'UNKNOWN'} replied={replied}"


def stale_row(row: dict, now: datetime) -> str:
    evidence = row.get("evidence") or [{}]
    first = evidence[0]
    recorded = recorded_time(first.get("recorded_at") or first.get("merged_at") or first.get("closed_at"))
    detail = first.get("url") or first.get("matched") or first.get("recorded") or "see record"
    kinds = ",".join(dict.fromkeys(str(item.get("kind")) for item in evidence))
    return f"STALE {pointer(row)}: {kinds} age={age_text(now - recorded) if recorded else 'UNKNOWN'} evidence={' '.join(str(detail).split())[:180]}"


def digest(sources: dict[str, dict], *, full: bool = False) -> str:
    now = clock()
    lines = [f"Routine check at {clock_label(now)} (source candidates; routing receipts are delivery evidence only)"]
    for name, report in sources.items():
        lines.extend(source_errors(name, report))
    primary = sources.get("primary", {})
    annotations: dict[str, str] = primary.get("annotations") or {}
    anchors: dict[str, str] = primary.get("anchors") or {}

    def noted(text: str, source: dict) -> str:
        return f"{text} | {annotations[pointer(source)]}" if pointer(source) in annotations else text

    sessions = sources.get("sessions", {}).get("sessions", [])
    for session in sessions:
        if session.get("action") or session.get("error"):
            suffix = f"; {anchors[pointer(session)]}" if pointer(session) in anchors else ""
            lines.append(f"WORKER {pointer(session)}: {session.get('error') or session['action']}{suffix}")
    selection = sources.get("next", {})
    for milestone in selection.get("milestone_summary", []):
        lines.append(f"MILESTONE {milestone['milestone']}: {milestone['state']}")
        for track in milestone.get("tracks", []):
            lines.append(f"TRACK {pointer(track)}: {'read' if track['read'] else 'UNKNOWN, not refreshed'}")
            lines.extend(f"DECISION {url}" for url in track.get("decisions", []))
        for wait in [*milestone.get("waits", []), *milestone.get("wait_reviews", [])]:
            lines.append(noted(f"WAIT {pointer(wait)}: {wait.get('waiting_for')}{ask_note(wait)}", wait))
    needs_status = []
    rows = queue_rows(selection)
    actions = sorted((pair for pair in rows if pair[0] in {"SUPERVISOR_ACTION", "PRODUCTION_READ_REFUSED"}),
                     key=lambda pair: recorded_time(pair[1]["queue"].get("status_recorded_at")) or datetime.min.replace(tzinfo=UTC))
    supervisors = supervisor_actors()
    for kind, row in actions:
        recorded = recorded_time(row["queue"].get("status_recorded_at"))
        age = now - recorded if recorded else None
        request = (row["queue"].get("next_action") or row["queue"].get("reason") or "").splitlines()[0:1]
        detail = ''.join(request)[:180]
        if kind == "PRODUCTION_READ_REFUSED":
            detail = f"read=\"{refused_read(row)[:400]}\" next_actor={row['queue']['next_actor']} route={REFUSED_READ_ROUTE}"
        lines.append(noted(f"{kind} {pointer(row)} age={age_text(age) if age else 'UNKNOWN'}: {detail}", row))
        if (age is None or age > ROUTINE_INTERVAL) and (kind == "SUPERVISOR_ACTION" or row["queue"]["next_actor"].casefold() in supervisors):
            lines.append(f"ERROR supervisor_action {pointer(row)}: open {age_text(age) if age else 'for an UNKNOWN time'} with no disposition; "
                         "set another actor, record a wait or claim it on the issue")
    for row in client_wait_rows(selection):
        lines.append(noted(f"CLIENT_WAIT {pointer(row)}: {row.get('title') or row.get('next_action') or ''}", row))
    for kind, row in rows:
        queue = row.get("queue", {})
        if kind == "CONFLICT":
            lines.append(f"REVIEW {pointer(row)}: conflicting agent assignments")
        elif kind == "NEXT":
            lines.append(noted(f"NEXT {pointer(row)}: {queue['next_action']} (verify ownership)", row))
        elif kind == "REVIEW":
            lines.append(noted(f"REVIEW {pointer(row)}: {queue.get('reason')}; {queue.get('next_action', '')}", row))
        elif kind == "NEEDS_STATUS":
            needs_status.append(row)
    lines.extend(stale_row(row, now) for row in selection.get("stale_wait_report", {}).get("items", []))
    if needs_status:
        lines.append(f"NEEDS_STATUS {len(needs_status)}: {pointer(needs_status[0])} (review global next intake)")
    questions = sources.get("questions", {})
    lines.append(f"QUEUE {questions.get('queue', 'UNKNOWN')}")
    queued = [question for question in questions.get("questions", []) if question.get("registration_url")]
    unregistered = [question for question in questions.get("questions", []) if not question.get("registration_url")]
    omitted = 0
    for question in queued:
        if not full and question.get("delivery") == "acknowledged":
            omitted += 1
            continue
        lines.append(f"QUESTION {pointer(question)}: {question.get('delivery', question.get('status', 'unresolved'))}")
    if unregistered:
        lines.append(f"UNREGISTERED {len(unregistered)}: {pointer(unregistered[0])}")
    comments = sources.get("comments", {})
    for row in comments.get("attention", []):
        if row.get("later_reply_url") or (row.get("comment_kind") == radar.OPENING_POST and row.get("thread_has_replies")):
            continue
        routing = row.get("routing", {})
        if routing.get("status") not in {None, "sent", "pending"} or routing.get("audit_error"):
            lines.append(f"ERROR routing {pointer(row)}: {routing.get('audit_error') or routing.get('cause') or routing['status']}")
        untrusted = " (untrusted)" if row.get("attention_state") == "outbound_reply" or row.get("comment_kind") == radar.OPENING_POST else ""
        lines.append(f"COMMENT {pointer(row)}{untrusted}: {' '.join((row.get('body_excerpt') or row.get('body') or '').split())[:180]}")
    for row in sources.get("waits", {}).get("needs_status", []):
        note = "recorded answer; leave the foreign record to its owner (untrusted)" if row.get("untrusted") else "recorded answer needs wait reconciliation"
        lines.append(f"REVIEW {pointer(row)}: {note}")
    foreign_waits = [row for row in sources.get("waits", {}).get("waits", []) if row.get("untrusted")]
    for row in [*comments.get("unasked_director_waits", []), *foreign_waits]:
        lines.append(f"WAIT {pointer(row)}{' (untrusted)' if row.get('untrusted') else ''}")
    for row in comments.get("director_mentions", []):
        lines.append(f"MENTION {pointer(row)} (untrusted)")
    for row in sources.get("watch", {}).get("notices", []):
        lines.append(f"WATCH {pointer(row)} (untrusted notification)")
    lines.append(
        f"LOAD sessions={len(sessions)} candidates={selection.get('candidate_count', 0)} host={','.join(f'{value:.1f}' for value in os.getloadavg())}"
    )
    if omitted:
        lines.append(f"OMITTED {omitted} acknowledged questions; --full shows all")
    return "\n".join(dict.fromkeys(lines))


def build_readers(args: argparse.Namespace, watch: dict) -> dict[str, Callable[[], dict]]:
    discussions: dict[tuple[str, int], dict[str, Any]] = {}

    def github_reader(deadline_seconds: float = 120) -> github_read.GitHubReader:
        return github_read.GitHubReader(
            operation="github.supervisor.routine_check",
            **github_access.reader_options(getattr(args, "github_cli_login", None)),
            deadline_at=time.time() + deadline_seconds,
        )

    radar_args = argparse.Namespace(
        config=radar.DEFAULT_CONFIG,
        repo=args.repo,
        repo_owner=[] if args.repo else [args.owner],
        thread=args.thread,
        outbound=True,
        self_login=list(filter(None, [args.owner, getattr(args, "github_cli_login", None)])),
        director_login=args.owner,
        bot_login=[],
        people_index=None,
        window=None,
        since=None,
        until=None,
        limit_repos=1000,
        include_archived=False,
        no_cutoff=True,
        include_handled=False,
    )

    def waits() -> dict:
        return director_waits.collect_for_owner(github_reader(deadline_seconds=600), args.owner, datetime.now(UTC), include_owned=False)

    def fetch(item: str) -> list[dict]:
        match = oq.ITEM.fullmatch(item)
        if not match:
            raise ValueError("question source must be OWNER/REPO#NUMBER")
        try:
            discussion = github_read.read_discussion(github_reader(), match[1], int(match[2]))
            discussions[(match[1].casefold(), int(match[2]))] = discussion
            source_comments = discussion["comments"]
        except (github_read.GitHubReadError, github_read.GitHubReadShapeError) as error:
            raise ValueError(str(error)) from error
        return [
            {
                "id": row["id"],
                "url": row["html_url"],
                "body": row["body"],
                "author": row["user"]["login"],
                "created_at": row["created_at"],
                "updated_at": row.get("updated_at") or row["created_at"],
            }
            for row in source_comments
        ]

    def questions() -> dict:
        items = list(args.source)
        errors = []
        for notice in watch.get("history", []):
            if notice.get("kind") != "director_question":
                continue
            try:
                items.append(source_item(notice["url"]))
            except (ValueError, KeyError, TypeError) as error:
                errors.append({"item": pointer(notice), "error": str(error)})
        queue_args = argparse.Namespace(
            owner=args.owner,
            queue=args.queue,
            items=list(dict.fromkeys(items)),
            decision_author=oq.default_decision_authors(),
            register=False,
            ack=None,
            delivery_url=None,
        )
        result = run_queue(queue_args, fetch)
        result["source_errors"].extend(errors)
        result["complete"] = result["complete"] and not errors
        return result

    def comments() -> dict:
        hints = []
        errors = []
        for notice in watch.get("history", []):
            if notice.get("kind") not in {"foreign", "foreign_opened"}:
                continue
            try:
                hints.append(comment_hint(notice))
            except (ValueError, KeyError, TypeError, SystemExit) as error:
                errors.append({"repo": notice.get("repository", "-"), "lane": "watch_hint", "error": str(error)})
        settings = radar.resolve_settings(radar_args, radar.rollup.load_config(args.radar_config))
        settings["discussion_sources"] = discussions
        settings["scan_state"] = scan_state_path(args)
        result = radar.collect_payload(settings, comment_hints=tuple(hints))
        result["coverage"]["errors"].extend(errors)
        result["ok"] = result["ok"] and not errors
        if getattr(args, "foreign_state", None):
            foreign_watch.route_report(args, result, github_reader(), [*settings["self_logins"], *github_read.github_identity.configured_bot_logins()])
            result["coverage"]["errors"].extend(result.get("routing_errors", []))
            result["ok"] = result["ok"] and not result.get("routing_errors")
        return result

    return {
        "next": lambda: staffing(args.owner),
        "sessions": lambda: compact_check.report(args.ledger, args.capacity_state),
        "questions": questions,
        "waits": waits,
        "comments": comments,
    }


def primary_readers(args: argparse.Namespace) -> dict[str, Callable]:
    def reader() -> github_read.GitHubReader:
        return github_read.GitHubReader(operation="github.supervisor.routine_check",
                                        **github_access.reader_options(getattr(args, "github_cli_login", None)),
                                        deadline_at=time.time() + 60)

    def acceptance() -> Any:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "launchplane/scripts"))
        return importlib.import_module("launchplane_acceptance")

    def owner_review(repo: str, number: int) -> dict | None:
        github = reader()
        head = github.get_json(f"repos/{repo}/pulls/{number}", step="primary_pull")["head"]["sha"]
        runs = github.get_json(f"repos/{repo}/commits/{head}/check-runs?check_name={OWNER_REVIEW_CHECK}", step="primary_owner_review")["check_runs"]
        if not runs:
            return None
        return {"status": runs[0].get("status"), "conclusion": runs[0].get("conclusion"), "title": (runs[0].get("output") or {}).get("title")}

    def production(repo: str) -> dict:
        deployed = acceptance().read_production_commit(repo)
        if deployed.reason:
            return {"reason": deployed.reason}
        if not SHA.fullmatch(deployed.commit):
            return {"reason": "production's commit is not a full commit id"}
        github = reader()
        branch = github.get_json(f"repos/{repo}", step="primary_repository")["default_branch"]
        newest = github.get_json(f"repos/{repo}/commits/{branch}", step="primary_landed")["sha"]
        comparison = github.get_json(f"repos/{repo}/compare/{newest}...{deployed.commit}", step="primary_compare")
        return {"deployed": deployed.commit, "newest": newest, "contained": comparison["status"] in {"identical", "ahead"},
                "behind": comparison.get("behind_by")}

    def anchor_response(issue: str) -> str:
        match = re.search(r"([\w.-]+/[\w.-]+)(?:#|/issues/|/pull/)([1-9][0-9]*)", issue)
        if not match:
            raise ValueError("anchor is not OWNER/REPO#NUMBER")
        discussion = github_read.read_discussion(reader(), match[1], int(match[2]))
        return anchor_note(discussion)

    return {"owner_review": owner_review, "client_release": lambda repo: acceptance().read_client_release(repo),
            "production": production, "anchor_response": anchor_response}


def anchor_note(discussion: dict) -> str:
    edited = recorded_time((discussion.get("_graphql") or {}).get("lastEditedAt"))
    status = edited if edited and "## Current Status" in (discussion.get("body") or "") else None
    latest = max(filter(None, (recorded_time(comment.get("created_at")) for comment in discussion.get("comments", []))), default=None)
    if status and (latest is None or status >= latest):
        return f"anchor_response={clock_label(status)} via=current_status"
    if latest:
        return f"anchor_response={clock_label(latest)} via=comment"
    return "anchor_response=UNKNOWN"


def scan_state_path(args: argparse.Namespace) -> Path | None:
    ledger = getattr(args, "ledger", None)
    return ledger.with_name(f"{ledger.stem}-radar-scan.json") if isinstance(ledger, Path) else None


def comment_hint(notice: dict) -> Any:
    if not isinstance(notice.get("author"), str) or not notice["author"].strip():
        raise ValueError(f"watch hint author is unknown: {pointer(notice)}")
    repo, number = radar.parse_thread_reference(notice["url"])
    match = re.search(r"#issuecomment-([1-9][0-9]*)$", notice["url"])
    return radar.Comment(
        repo=repo,
        number=number,
        kind="issue_comment" if match else radar.OPENING_POST,
        comment_id=int(match[1]) if match else number,
        author=notice["author"],
        author_type="",
        created_at=notice["updated_at"],
        updated_at=notice["updated_at"],
        body=notice["excerpt"],
        url=notice["url"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print the existing Supervisor digest; optional routing delivers message pointers, never launches or closes."
    )
    parser.add_argument("--github-cli-login", help="retain the plain install login selected by supervisor.py start")
    parser.add_argument("--full", action="store_true", help="include acknowledged questions")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--foreign-log", type=Path, required=True)
    parser.add_argument("--service-log", type=Path, action="append", default=[])
    parser.add_argument("--capacity-state", type=Path)
    parser.add_argument("--foreign-state", type=Path, help="enable message routing with the existing watch state")
    parser.add_argument("--notify-config", type=Path)
    parser.add_argument("--miss-issue", help="existing internal Supervisor/repair issue, OWNER/REPO#NUMBER")
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--repo", action="append", default=[])
    parser.add_argument("--thread", action="append", default=[])
    parser.add_argument("--radar-config", type=Path, default=radar.DEFAULT_CONFIG)
    args = parser.parse_args()
    if any((args.foreign_state, args.notify_config, args.miss_issue)) and not all(
        (args.foreign_state, args.notify_config, args.miss_issue)
    ):
        parser.error("routing needs --foreign-state, --notify-config and --miss-issue together")
    if args.miss_issue and not re.fullmatch(rf"{re.escape(args.owner)}/[A-Za-z0-9_.-]+#[1-9][0-9]*", args.miss_issue, re.I):
        parser.error("--miss-issue must belong to the configured Director")

    if args.github_cli_login:
        args.github_cli_login = github_access.select_login(args.github_cli_login)

    def read_watch() -> dict:
        config = radar.rollup.load_config(args.radar_config)
        return watch_report(args.foreign_log, window=str(config.get("comment_window") or radar.DEFAULT_WINDOW))

    service_readers = {f"service {path}": lambda path=path: watch_report(path, service=True) for path in args.service_log}
    sources = collect({"watch": read_watch, **service_readers})
    sources.update(collect(build_readers(args, sources["watch"])))
    sources["primary"] = read_source(lambda: primary_reads(sources["next"], sources["sessions"].get("sessions", []), **primary_readers(args)), None)
    output = digest(sources, full=args.full)
    print(output)
    return int(any(line.startswith("ERROR ") for line in output.splitlines()))


if __name__ == "__main__":
    raise SystemExit(main())
