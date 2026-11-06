from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
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
            paragraph = re.split(r"\r?\n(?:[ \t]*\r?\n|[ \t]*(?:Question:|<!--))", content, 1)[0]
            if recorded and re.fullmatch(r"(?i)(?:later|not now|not yet|waiting|pending)[\s.;!—-]*", paragraph):
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



SUPERVISOR_SETTLEMENT = re.compile(
    r"^(?:Supervisor answer to (Q[1-9][0-9]*)\b"
    r"(?:[ \t]*\([^)\n]*\))?[ \t]*[.:][ \t]*"
    r"(?i:(?:yes|no)(?=[,.:!?]|$)|this is already decided\b)"
    r"|(Q[1-9][0-9]*) withdrawn by the Supervisor\b)"
)


def settlement_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def supervisor_settled_questions(comments: list[dict[str, Any]], trusted: set[str], *,
                                withdrawals_only: bool = False) -> set[str]:
    def author(source: dict[str, Any]) -> str:
        return str((source.get("user") or {}).get("login") or source.get("author") or "").casefold()

    settlements: dict[str, list[dict[str, Any]]] = {}
    for comment in comments:
        if author(comment) not in trusted:
            continue
        heading = str(comment.get("body") or "").strip().partition("\n")[0].replace("*", "")
        match = SUPERVISOR_SETTLEMENT.match(heading)
        when = settlement_time(comment.get("created_at"))
        if match and when and (not withdrawals_only or match[2]):
            settlements.setdefault(match[1] or match[2], []).append({"when": when, "author": author(comment)})
    sources = {comment.get("html_url") or comment["url"]: comment for comment in comments}
    numbers = Counter(heading_id(str(comment.get("body") or "").strip()) for comment in sources.values())
    settled = set()
    for comment in sources.values():
        question = heading_id(str(comment.get("body") or "").strip())
        asked = settlement_time(comment.get("created_at"))
        if question and asked and (not withdrawals_only or numbers[question] == 1) and any(entry["when"] > asked and
                                     (not withdrawals_only or entry["author"] == author(comment))
                                     for entry in settlements.get(question, [])):
            settled.add(comment.get("html_url") or comment["url"])
    return settled
