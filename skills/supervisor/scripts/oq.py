#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""List Director questions, including legacy headings, with conservative answer matching."""

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))
from skills.github.scripts import github_identity
from skills.github.scripts.github_plan_status import questions as question_records
from skills.supervisor.scripts.check_changes import changes, fingerprint

QUESTION_PREFIXES = question_records.QUESTION_PREFIXES
DECISION_HEADING = question_records.DECISION_HEADING
questions = question_records.questions

ITEM = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)")
def fetch(item):
    match = ITEM.fullmatch(item)
    if not match:
        raise ValueError("item must be OWNER/REPO#NUMBER")
    from skills.github.scripts.github_read import (
        GitHubReader,
        GitHubReadError,
        GitHubReadShapeError,
        automation_only_gh_prefix_args,
    )

    try:
        comments = GitHubReader(operation="github.question.read", strict_actor=True,
                                gh_prefix_args=automation_only_gh_prefix_args()).paged_json(
            f"/repos/{match[1]}/issues/{match[2]}/comments", step_prefix="question_comments"
        )
    except (GitHubReadError, GitHubReadShapeError) as error:
        raise ValueError(f"issue discussion read is incomplete: {error}") from error
    if not isinstance(comments, list) or not all(isinstance(c, dict) for c in comments):
        raise ValueError("issue discussion comments are malformed")
    return [{**comment, "author": (comment.get("user") or {}).get("login") or "", "url": comment["html_url"]} for comment in comments]


def default_decision_authors() -> list[str]:
    authors: set[str] = set(github_identity.configured_bot_logins())
    author = github_identity.automation_login()
    if author is not None:
        authors.add(author)
    return sorted(authors)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in {"create", "update", "show-question"}:
        from question_record import main as record_main
        return record_main(sys.argv[1:], fetch)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("items", nargs="*")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--decision-author", action="append", default=default_decision_authors())
    parser.add_argument("--queue", help="Existing Supervisor question discussion, OWNER/REPO#NUMBER")
    parser.add_argument("--register", action="store_true", help="Register exact positional question URLs in that queue, with readback")
    parser.add_argument("--ack", metavar="QUESTION_URL", help="Acknowledge actual presentation to the Director")
    parser.add_argument("--delivery-url", help="Durable receipt for the presentation, not just queue registration")
    parser.add_argument("--changes-state", type=Path, help="print changed question pointers; save only fingerprints")
    parser.add_argument("--include-answers", action="store_true", help="Include accepted answer text and source links in item reads")
    args = parser.parse_args()
    args.decision_author = sorted({author.casefold() for author in args.decision_author})
    if args.changes_state and (args.register or args.ack):
        parser.error("changes mode is read-only; use a separate registration or presentation acknowledgement")
    if args.include_answers and (args.queue or args.changes_state):
        parser.error("include-answers requires ordinary item reads")
    if args.queue:
        from question_delivery import run_queue
        if (args.register and args.ack) or bool(args.ack) != bool(args.delivery_url):
            parser.error("use either --register or --ack QUESTION_URL --delivery-url RECEIPT_URL")
        try:
            report = run_queue(args, fetch)
            if args.changes_state:
                report = question_changes(report, args.changes_state, [args.owner, *sorted(args.decision_author), args.queue])
            print(json.dumps(report))
            return 0 if report["complete"] else 1
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            print(json.dumps({"queue": args.queue, "complete": False, "error": str(error)}))
            return 1
    if not args.items or args.register or args.ack or args.delivery_url:
        parser.error("supply source items, or --queue for queue operations")
    failed = False
    reports = []
    for item in args.items:
        try:
            comments = fetch(item)
            found = questions(comments, args.owner, args.decision_author, recorded_decisions_only=args.include_answers)
            if args.include_answers:
                numbers = Counter(question_records.heading_id(question["question"]) for question in found)
                for question in found:
                    number = question_records.heading_id(question["question"])
                    question["number"] = number if number and numbers[number] == 1 else None
                    question["decision_comments"] = [comment for comment in comments if comment["url"] in question["answers"]]
            report = {
                        "item": item,
                        "questions": found,
                    }
            reports.append(report)
            if not args.changes_state:
                print(json.dumps(report))
        except (ValueError, KeyError, subprocess.SubprocessError) as error:
            failed = True
            reports.append({"item": item, "error": str(error)})
            if not args.changes_state:
                print(json.dumps(reports[-1]))
    if args.changes_state:
        report = {"complete": not failed, "questions": [row for report in reports for row in report.get("questions", [])],
                  "source_errors": [report for report in reports if "error" in report]}
        print(json.dumps(question_changes(report, args.changes_state, [args.owner, *sorted(args.decision_author), *sorted(args.items)])))
    return int(failed)


def question_changes(report: dict, state: Path, scope: list[str]) -> dict:
    rows = []
    for question in report["questions"]:
        source = {key: value for key, value in question.items() if key != "age_seconds"}
        rows.append({**{key: value for key, value in source.items() if key != "question"}, "revision": fingerprint(source)})
    return {**{key: value for key, value in report.items() if key != "questions"},
            "questions": changes(rows, state, scope, key="url", complete=report["complete"])}


if __name__ == "__main__":
    raise SystemExit(main())
