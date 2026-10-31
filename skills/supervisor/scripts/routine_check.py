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
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import compact_check
import foreign_watch
import goal_board
import oq
from question_delivery import run_queue, source_item

if TYPE_CHECKING:
    from skills.github.scripts import github_read
else:
    github_read = importlib.import_module("github_read")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github-work-rollup/scripts"))
radar = importlib.import_module("github_unanswered_comments")


def collect(readers: dict[str, Callable[[], dict]]) -> dict[str, dict]:
    results = {}
    for name, read in readers.items():
        try:
            results[name] = read()
        except (Exception, SystemExit) as error:  # noqa: BLE001
            results[name] = {"error": str(error), "coverage": {"complete": False}}
    return results


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
    return errors


def pointer(row: dict) -> str:
    return str(row.get("comment_url") or row.get("url") or row.get("session_id") or json.dumps(row, sort_keys=True))


def digest(sources: dict[str, dict]) -> str:
    lines = ["Routine check (source candidates; routing receipts are delivery evidence only)"]
    for name, report in sources.items():
        lines.extend(source_errors(name, report))
    sessions = sources.get("sessions", {}).get("sessions", [])
    for row in sessions:
        if row.get("action") or row.get("error"):
            lines.append(f"WORKER {pointer(row)}: {row.get('error') or row['action']}")
    board = sources.get("board", {})
    for track in board.get("tracks", []):
        for row in (track, *track.get("children", [])):
            if row.get("unread") or row.get("read") is False:
                lines.append(f"ERROR board {pointer(row)}: {row.get('unread') or 'unread record'}")
            for request in row.get("staffing_requests", []):
                lines.append(f"STAFF {pointer(request)}: {' '.join(request['body'].split())[:180]}")
    questions = sources.get("questions", {})
    lines.append(f"QUEUE {questions.get('queue', 'UNKNOWN')}")
    queued = [question for question in questions.get("questions", []) if question.get("registration_url")]
    unregistered = [question for question in questions.get("questions", []) if not question.get("registration_url")]
    for question in queued:
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
        lines.append(f"COMMENT {pointer(row)}: {' '.join((row.get('body_excerpt') or row.get('body') or '').split())[:180]}")
    for row in comments.get("unasked_director_waits", []):
        lines.append(f"WAIT {pointer(row)}")
    for row in comments.get("director_mentions", []):
        lines.append(f"MENTION {pointer(row)} (untrusted)")
    for row in sources.get("watch", {}).get("notices", []):
        lines.append(f"WATCH {pointer(row)} (untrusted notification)")
    lines.append(
        f"LOAD sessions={len(sessions)} tracks={len(board.get('tracks', []))} host={','.join(f'{value:.1f}' for value in os.getloadavg())}"
    )
    return "\n".join(dict.fromkeys(lines))


def build_readers(args: argparse.Namespace, watch: dict) -> dict[str, Callable[[], dict]]:
    def github_reader() -> github_read.GitHubReader:
        return github_read.GitHubReader(
            operation="github.supervisor.routine_check",
            strict_actor=True,
            gh_prefix_args=github_read.automation_only_gh_prefix_args(),
            deadline_at=time.time() + 120,
        )

    radar_args = argparse.Namespace(
        config=radar.DEFAULT_CONFIG,
        repo=args.repo,
        repo_owner=[] if args.repo else [args.owner],
        thread=args.thread,
        outbound=False,
        self_login=[],
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

    def fetch(item: str) -> list[dict]:
        match = oq.ITEM.fullmatch(item)
        if not match:
            raise ValueError("question source must be OWNER/REPO#NUMBER")
        try:
            source_comments = github_reader().paged_json(
                f"/repos/{match[1]}/issues/{match[2]}/comments", step_prefix="routine_questions"
            )
        except (github_read.GitHubReadError, github_read.GitHubReadShapeError) as error:
            raise ValueError(str(error)) from error
        return [
            {
                "id": row["id"],
                "url": row["html_url"],
                "body": row["body"],
                "author": row["user"]["login"],
                "created_at": row["created_at"],
            }
            for row in source_comments
        ]

    def board() -> dict:
        authors = {author.casefold() for author in oq.default_decision_authors()}
        result = goal_board.goal_board(github_reader(), args.owner, trusted={args.owner.casefold(), *authors})
        if not authors:
            result["coverage"]["complete"] = False
            result["errors"] = [*result.get("errors", []), "Staffing automation authors are unconfigured; coverage is incomplete"]
        return result

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
            if notice.get("kind") != "foreign":
                continue
            try:
                hints.append(comment_hint(notice))
            except (ValueError, KeyError, TypeError, SystemExit) as error:
                errors.append({"repo": notice.get("repository", "-"), "lane": "watch_hint", "error": str(error)})
        settings = radar.resolve_settings(radar_args, radar.rollup.load_config(args.radar_config))
        result = radar.collect_payload(settings, comment_hints=tuple(hints))
        result["coverage"]["errors"].extend(errors)
        result["ok"] = result["ok"] and not errors
        if getattr(args, "foreign_state", None):
            foreign_watch.route_report(args, result, github_reader(), [*settings["self_logins"], *github_read.github_identity.configured_bot_logins()])
            result["coverage"]["errors"].extend(result.get("routing_errors", []))
            result["ok"] = result["ok"] and not result.get("routing_errors")
        return result

    return {
        "board": board,
        "sessions": lambda: compact_check.report(args.ledger, args.capacity_state),
        "questions": questions,
        "comments": comments,
    }


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

    def read_watch() -> dict:
        config = radar.rollup.load_config(args.radar_config)
        return watch_report(args.foreign_log, window=str(config.get("comment_window") or radar.DEFAULT_WINDOW))

    service_readers = {f"service {path}": lambda path=path: watch_report(path, service=True) for path in args.service_log}
    sources = collect({"watch": read_watch, **service_readers})
    sources.update(collect(build_readers(args, sources["watch"])))
    print(digest(sources))
    return int(
        any(source_errors(name, report) for name, report in sources.items())
        or any(
            row.get("routing", {}).get("status") not in {None, "sent", "pending"} or row.get("routing", {}).get("audit_error")
            for row in sources.get("comments", {}).get("attention", [])
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
