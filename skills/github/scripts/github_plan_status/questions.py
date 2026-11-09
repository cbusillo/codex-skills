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
    r"^(?:Director|Owner) (?:decision(?: on)?|answer to)"
    r"(?: \[?(?P<number>Q[1-9][0-9]*)]?(?:\(https://[^)\s]*\))?| \((?P<parenthetical_number>Q[1-9][0-9]*)\))?"
    r"(?: \([^\n)]*\))?(?:\s*[:,]|\*\*| (?=https://))"
)


def recorded_decision(text: str) -> re.Match[str] | None:
    return DECISION_HEADING.match(text.lstrip().lstrip("*"))


def decision_parts(body: str, decision: re.Match[str]) -> tuple[str | None, str]:
    """A heading's leading Q ID, if any, and the text it records."""
    content = body.lstrip().lstrip("*")[decision.end():].replace("*", "").strip(" ,:\t\r\n")
    leading = QUESTION.match(content)
    content = re.sub(r"^Q[1-9][0-9]*\s*[:=,.—-]?\s*", "", content)
    return leading[0] if leading else None, re.sub(r"https://\S+", "", content).strip()


def deferred(content: str) -> bool:
    paragraph = re.split(r"\r?\n(?:[ \t]*\r?\n|[ \t]*(?:Question:|<!--))", content, 1)[0]
    return re.fullmatch(r"(?i)[\"'“”]*(?:later|not now|not yet|waiting|pending)[\"'“”\s.;!—-]*", paragraph) is not None


def substantive_decision(body: str) -> bool:
    decision = recorded_decision(body)
    return decision is not None and not deferred(decision_parts(body, decision)[1])


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
            decision = recorded_decision(answer.get("body", ""))
            recorded = decision is not None
            heading_number = (decision["number"] or decision["parenthetical_number"]) if decision else None
            if heading_number and heading_number != number:
                continue
            authorized = author == owner or (
                author in decision_authors and recorded
            )
            if recorded_decisions_only and not recorded:
                continue
            leading_number, content = decision_parts(answer.get("body", ""), decision) if decision else (None, "")
            decision_number = heading_number or leading_number
            if recorded and deferred(content):
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
    r"^(?:Supervisor answer to (?P<answered>Q[1-9][0-9]*)\b(?P<from_record> from the record\b[^:\n]*)?"
    r"(?:[ \t]*\([^)\n]*\))?[ \t]*[.:][ \t]*"
    r"(?i:(?:yes|no)(?=[,.:!?]|$)|this is already decided\b)"
    r"|(?P<notice>Supervisor:[ \t]*)?(?P<withdrawn>Q[1-9][0-9]*)(?(notice) is withdrawn| withdrawn by the Supervisor)\b)"
)


def settlement_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def supervisor_settled_questions(comments: list[dict[str, Any]], trusted: set[str], *,
                                asker_only: bool = False) -> set[str]:
    """With asker_only, only what the asking identity may close: its withdrawal or its question's cited Director decision."""
    def author(source: dict[str, Any]) -> str:
        return str((source.get("user") or {}).get("login") or source.get("author") or "").casefold()

    decisions = {comment.get("html_url") or comment.get("url") for comment in comments
                 if author(comment) in trusted and substantive_decision(str(comment.get("body") or ""))}
    settlements: dict[str, list[dict[str, Any]]] = {}
    for comment in comments:
        if author(comment) not in trusted:
            continue
        body = str(comment.get("body") or "")
        heading = body.strip().partition("\n")[0].replace("*", "")
        match = SUPERVISOR_SETTLEMENT.match(heading)
        when = settlement_time(comment.get("created_at"))
        if not match or not when:
            continue
        cited_threads = {(link[1], link[2]) for link in COMMENT_URL.finditer(body) if link[0] in decisions}
        if not asker_only or match["withdrawn"] or match["from_record"]:
            settlements.setdefault(match["answered"] or match["withdrawn"], []).append(
                {"when": when, "author": author(comment), "needs_citation": bool(match["from_record"]), "cited_threads": cited_threads})
    sources = {comment.get("html_url") or comment["url"]: comment for comment in comments}
    numbers = Counter(heading_id(str(comment.get("body") or "").strip()) for comment in sources.values())
    settled = set()
    for comment in sources.values():
        question = heading_id(str(comment.get("body") or "").strip())
        asked = settlement_time(comment.get("created_at"))
        if not question or not asked or (asker_only and numbers[question] != 1):
            continue
        own = COMMENT_URL.fullmatch(comment.get("html_url") or comment["url"])
        if any(entry["when"] > asked and (not asker_only or entry["author"] == author(comment))
               and (not entry["needs_citation"] or (own is not None and (own[1], own[2]) in entry["cited_threads"]))
               for entry in settlements.get(question, [])):
            settled.add(comment.get("html_url") or comment["url"])
    return settled
