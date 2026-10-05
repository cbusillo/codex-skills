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
from pathlib import Path

ITEM = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)")
QUESTION_PREFIXES = ("Director question:", "Owner question:")
DECISION_PREFIXES = ("Director decision", "Owner decision")


def questions(comments, owner, decision_authors):
    results = []
    for i, question in enumerate(comments):
        body = question.get("body", "").strip()
        if not body.lstrip("*").startswith(QUESTION_PREFIXES):
            continue
        answers = []
        for answer in comments[i + 1 :]:
            author = answer.get("author")
            text = answer.get("body", "").lstrip().lstrip("*")
            authorized = author == owner or (
                author in decision_authors and text.startswith(DECISION_PREFIXES)
            )
            # Later unrelated replies never resolve a question. Require its id or URL.
            linked = question["url"] in text or re.search(
                rf"(?<!\d){question['id']}(?!\d)", text
            )
            if authorized and linked and not text.startswith(QUESTION_PREFIXES):
                answers.append(answer["url"])
        results.append(
            {
                "url": question["url"],
                "question": body,
                "status": "answered" if answers else "needs_review",
                "answers": answers,
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
    if not result.get("ok") or result.get("outcome_certainty") != "confirmed":
        raise ValueError("issue discussion read is incomplete")
    return result["issue"]["comments"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("items", nargs="+")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--decision-author", action="append", default=[])
    args = parser.parse_args()
    failed = False
    for item in args.items:
        try:
            print(
                json.dumps(
                    {
                        "item": item,
                        "questions": questions(
                            fetch(item), args.owner, args.decision_author
                        ),
                    }
                )
            )
        except (ValueError, KeyError, subprocess.SubprocessError) as error:
            failed = True
            print(json.dumps({"item": item, "error": str(error)}))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
