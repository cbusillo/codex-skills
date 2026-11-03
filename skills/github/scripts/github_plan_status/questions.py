from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

QUESTION = re.compile(r"\bQ([1-9][0-9]*)\b")
QUESTION_HEADING = re.compile(r"^\s*\**(?:Owner|Director) question:[ \t*]*")
COMMENT_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/([1-9][0-9]*)#issuecomment-([1-9][0-9]*)")


def heading_id(body: str) -> str | None:
    heading = QUESTION_HEADING.match(body)
    match = QUESTION.match(body, heading.end()) if heading else None
    if not match:
        return None
    if int(match[1]) > 4:
        return match[0]
    suffix = body[match.end():].lstrip(" *\t")
    return match[0] if not suffix or suffix[0] in ".—–" else None

QUESTION_PREFIXES = ("Director question:", "Owner question:")
DECISION_HEADING = re.compile(
    r"^(?:Director|Owner) decision(?: (?P<number>Q[1-9][0-9]*)\s*:"
    r"| \((?P<parenthetical_number>Q[1-9][0-9]*)\)\s*:|[:,]| \([^\n)]*\)[:,]|\*\*| (?=https://))"
)


def questions(comments: list[dict[str, Any]], owner: str, decision_authors: Iterable[str],
              related_comments: Iterable[dict[str, Any]] = (), *, recorded_decisions_only: bool = False) -> list[dict[str, Any]]:
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
            decision = DECISION_HEADING.match(text)
            recorded = decision is not None
            heading_number = (decision["number"] or decision["parenthetical_number"]) if decision else None
            if heading_number and heading_number != number:
                continue
            authorized = author == owner or (
                author in decision_authors and recorded
            )
            if recorded_decisions_only and not recorded:
                continue
            content = re.sub(r"^(?:Director|Owner) decision(?:\s*\([^)]*\))?[, :*]*", "", text)
            content = content.replace("*", "").strip()
            number_match = QUESTION.match(content)
            decision_number = heading_number or (number_match[0] if number_match else None)
            content = re.sub(r"^Q[1-9][0-9]*\s*[:=,.—-]?\s*", "", content)
            content = re.sub(r"https://\S+", "", content).strip()
            if recorded and re.fullmatch(r"(?i)(?:later|not now|not yet|waiting|pending)[\s.;!—-]*", content):
                continue
            numbered = (
                recorded and number is not None and numbers[number] == 1
                and decision_number == number and bool(content.strip(".,:;= -"))
                and (heading_number is not None or {match[0] for match in QUESTION.finditer(text)} == {number})
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

