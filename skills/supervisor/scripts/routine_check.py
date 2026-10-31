#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
import argparse
import importlib
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import compact_check
import goal_board
import oq
from question_delivery import run_queue

from skills.github.scripts.github_read import (
    GitHubReader,
    GitHubReadError,
    GitHubReadShapeError,
    automation_only_gh_prefix_args,
)

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


def watch_report(path: Path) -> dict:
    notices: dict[str, dict] = {}
    latest: dict = {"errors": ["foreign watch log has no complete reports"], "coverage_gaps": []}
    incomplete = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            incomplete.append(f"watch log line {number} is incomplete or malformed; prior notices retained")
            continue
        if isinstance(row, dict) and row.get("error"):
            latest = {"errors": [row["error"]], "coverage_gaps": []}
        elif isinstance(row, dict) and all(isinstance(row.get(key), list) for key in ("notices", "errors", "coverage_gaps")):
            latest = row
            for notice in row["notices"]:
                notices[pointer(notice)] = notice
        else:
            incomplete.append(f"watch log line {number} has an unknown shape")
    return {**latest, "errors": [*latest["errors"], *incomplete], "notices": list(notices.values())}


def source_errors(name: str, report: dict) -> list[str]:
    errors = [f"ERROR {name}: {report['error']}"] if report.get("error") else []
    coverage = report.get("coverage", {})
    for key in ("errors", "source_errors", "coverage_gaps", "untrusted_records"):
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
    lines = ["Routine check (read-only candidates)"]
    for name, report in sources.items():
        lines.extend(source_errors(name, report))
    sessions = sources.get("sessions", {}).get("sessions", [])
    for row in sessions:
        if row.get("action") or row.get("error"):
            lines.append(f"WORKER {pointer(row)}: {row.get('error') or row['action']}")
    board = sources.get("board", {})
    question_urls = set()
    for track in board.get("tracks", []):
        for row in (track, *track.get("children", [])):
            if row.get("unread") or row.get("read") is False:
                lines.append(f"ERROR board {pointer(row)}: {row.get('unread') or 'unread record'}")
            for request in row.get("staffing_requests", []):
                lines.append(f"STAFF {pointer(request)}: {' '.join(request['body'].split())[:180]}")
            for question in row.get("open_questions", []):
                lines.append(f"QUESTION {pointer(question)}")
                question_urls.add(pointer(question))
    questions = sources.get("questions", {})
    lines.append(f"QUEUE {questions.get('queue', 'UNKNOWN')}")
    for question in questions.get("questions", []):
        lines.append(f"QUESTION {pointer(question)}: {question.get('delivery', question.get('status', 'unresolved'))}")
        question_urls.add(pointer(question))
    question_urls.update(questions.get("settled_questions", []))
    comments = sources.get("comments", {})
    for row in comments.get("attention", []):
        lines.append(f"COMMENT {pointer(row)}: {' '.join((row.get('body_excerpt') or row.get('body') or '').split())[:180]}")
    for row in comments.get("unasked_director_waits", []):
        lines.append(f"WAIT {pointer(row)}")
    for row in comments.get("director_mentions", []):
        lines.append(f"MENTION {pointer(row)} (untrusted)")
    radar_urls = {pointer(row) for row in [*comments.get("attention", []), *comments.get("handled", [])]}
    for row in sources.get("watch", {}).get("notices", []):
        if pointer(row) not in radar_urls | question_urls:
            lines.append(f"WATCH {pointer(row)} (untrusted)")
    lines.append(
        f"LOAD sessions={len(sessions)} tracks={len(board.get('tracks', []))} host={','.join(f'{value:.1f}' for value in os.getloadavg())}"
    )
    return "\n".join(dict.fromkeys(lines))


def build_readers(args: argparse.Namespace) -> dict[str, Callable[[], dict]]:
    trusted = set(oq.default_decision_authors()) | {args.owner.casefold()}

    def github_reader() -> GitHubReader:
        return GitHubReader(
            operation="github.supervisor.routine_check",
            strict_actor=True,
            gh_prefix_args=automation_only_gh_prefix_args(),
            deadline_at=time.time() + 120,
        )

    queue_args = argparse.Namespace(
        owner=args.owner,
        queue=args.queue,
        items=args.source,
        decision_author=sorted(trusted),
        register=False,
        ack=None,
        delivery_url=None,
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
        include_handled=True,
    )

    def fetch(item: str) -> list[dict]:
        match = oq.ITEM.fullmatch(item)
        if not match:
            raise ValueError("question source must be OWNER/REPO#NUMBER")
        try:
            comments = github_reader().paged_json(f"/repos/{match[1]}/issues/{match[2]}/comments", step_prefix="routine_questions")
        except (GitHubReadError, GitHubReadShapeError) as error:
            raise ValueError(str(error)) from error
        return [
            {
                "id": row["id"],
                "url": row["html_url"],
                "body": row["body"],
                "author": row["user"]["login"],
                "created_at": row["created_at"],
            }
            for row in comments
        ]

    return {
        "board": lambda: goal_board.goal_board(github_reader(), args.owner, trusted=trusted),
        "sessions": lambda: compact_check.report(args.ledger, args.capacity_state),
        "questions": lambda: run_queue(queue_args, fetch),
        "watch": lambda: watch_report(args.foreign_log),
        "comments": lambda: radar.collect_payload(radar.resolve_settings(radar_args, radar.rollup.load_config(args.radar_config))),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print one digest from the existing Supervisor readers; never launch, close or deliver."
    )
    parser.add_argument("--owner", required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--foreign-log", type=Path, required=True)
    parser.add_argument("--capacity-state", type=Path)
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--repo", action="append", default=[])
    parser.add_argument("--thread", action="append", default=[])
    parser.add_argument("--radar-config", type=Path, default=radar.DEFAULT_CONFIG)
    args = parser.parse_args()
    sources = collect(build_readers(args))
    print(digest(sources))
    return int(any(source_errors(name, report) for name, report in sources.items()))


if __name__ == "__main__":
    raise SystemExit(main())
