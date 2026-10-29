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
from check_changes import changes, fingerprint

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from skills.direction.scripts.question_id import QUESTION, heading_id

ITEM = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)")
QUESTION_PREFIXES = ("Director question:", "Owner question:")
DECISION_PREFIXES = (
    "Director decision:",
    "Director decision,",
    "Director decision (",
    "Director decision**",
    "Owner decision",
)


def questions(comments, owner, decision_authors, related_comments=(), *, recorded_decisions_only=False):
    owner = owner.casefold()
    decision_authors = {author.casefold() for author in decision_authors}
    numbers = Counter(heading_id(comment.get("body", "")) for comment in comments)
    results = []
    for i, question in enumerate(comments):
        body = question.get("body", "").strip()
        if not body.lstrip("*").startswith(QUESTION_PREFIXES):
            continue
        number = heading_id(body)
        answers = []
        for answer in [*comments[i + 1 :], *related_comments]:
            author = (answer.get("author") or "").casefold()
            text = answer.get("body", "").lstrip().lstrip("*")
            recorded = text.startswith(DECISION_PREFIXES)
            authorized = author == owner or (
                author in decision_authors and recorded
            )
            # The queue needs a recorded decision, not merely a linked owner
            # reply. Keep the legacy classifier's candidate behavior for callers.
            if recorded_decisions_only and not recorded:
                continue
            content = re.sub(r"^(?:Director|Owner) decision(?:\s*\([^)]*\))?[, :*]*", "", text)
            content = content.replace("*", "").strip()
            decision_number = QUESTION.match(content)
            content = re.sub(r"^Q[1-9][0-9]*\s*[:=,.—-]?\s*", "", content)
            content = re.sub(r"https://\S+", "", content).strip()
            if recorded_decisions_only and re.fullmatch(r"(?i)(?:later|not now)[\s.;!—-]*", content):
                continue
            numbered = (
                recorded and number is not None and numbers[number] == 1 and decision_number is not None
                and decision_number[0] == number and bool(content.strip(".,:;= -"))
                and {match[0] for match in QUESTION.finditer(text)} == {number}
            )
            linked = question["url"] in text or re.search(
                rf"(?<!\d){question['id']}(?!\d)", text
            )
            if authorized and (linked or numbered) and not text.startswith(QUESTION_PREFIXES):
                answers.append(answer["url"])
        results.append(
            {
                "url": question["url"],
                "question": body,
                "status": "answered" if answers else "needs_review",
                "answers": answers,
                "created_at": question.get("created_at"),
            }
        )
    return results


def fetch(item):
    match = ITEM.fullmatch(item)
    if not match:
        raise ValueError("item must be OWNER/REPO#NUMBER")
    helper = Path(__file__).resolve().parents[2] / "github/scripts/gh-plan.py"
    run = subprocess.run(
        ["uv", "run", str(helper), "--repo", match[1], "show", match[2], "--full"],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    result = json.loads(run.stdout)
    if (result.get("ok") is not True
            or result.get("outcome_certainty") not in {"confirmed", "not_applicable"}
            or result.get("disposition") != "complete"
            or result.get("exit_code") != 0):
        raise ValueError("issue discussion read is incomplete")
    comments = result["issue"]["comments"]
    if not isinstance(comments, list) or not all(isinstance(c, dict) for c in comments):
        raise ValueError("issue discussion comments are malformed")
    return comments


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("items", nargs="*")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--decision-author", action="append", default=[])
    parser.add_argument("--queue", help="Existing Supervisor question discussion, OWNER/REPO#NUMBER")
    parser.add_argument("--register", action="store_true", help="Register exact positional question URLs in that queue, with readback")
    parser.add_argument("--ack", metavar="QUESTION_URL", help="Acknowledge actual presentation to the Director")
    parser.add_argument("--delivery-url", help="Durable receipt for the presentation, not just queue registration")
    parser.add_argument("--changes-state", type=Path, help="print changed question pointers; save only fingerprints")
    args = parser.parse_args()
    if args.changes_state and (args.register or args.ack):
        parser.error("changes mode is read-only; use a separate registration or presentation acknowledgement")
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
            report = {
                        "item": item,
                        "questions": questions(
                            fetch(item), args.owner, args.decision_author
                        ),
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
