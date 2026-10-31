#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Acknowledged question delivery using references in an existing GitHub discussion."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import oq

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from skills.github.scripts import github_comment
from skills.direction.scripts.question_id import counter_path, heading_id

COMMENT_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/([1-9][0-9]*)#issuecomment-([1-9][0-9]*)")
MARKER = re.compile(r"<!-- director-question:delivery (\{[^\n]*}) -->")


def source_item(url):
    match = COMMENT_URL.fullmatch(url)
    if not match:
        raise ValueError("question must be an exact GitHub issue-comment URL")
    return f"{match[1]}#{match[2]}"


def question_id(body):
    """Only the heading's own ID, never another question mentioned in its context."""
    return heading_id(body)


def records(comments, trusted):
    """Only standalone first-line records from configured trusted authors count."""
    result = []
    for comment in comments:
        if (comment.get("author") or "").casefold() not in trusted:
            continue
        first = (comment.get("body") or "").strip().splitlines()
        match = MARKER.fullmatch(first[0]) if first else None
        if not match:
            continue
        record = json.loads(match[1])
        if (not isinstance(record, dict) or type(record.get("version")) is not int
                or record["version"] != 1 or record.get("event") not in {"registered", "delivered"}):
            raise ValueError("invalid question delivery record")
        source_item(record["question_url"])
        if record["event"] == "delivered":
            validate_receipt(record["delivery_url"])
        result.append({**record, "record_url": comment["url"]})
    return result


def validate_receipt(url):
    # Public-safe references only; private destinations and arbitrary text never
    # enter an issue comment. A receipt records presentation, never approval.
    if not isinstance(url, str) or not (COMMENT_URL.fullmatch(url) or re.fullmatch(
        r"https://(?:discord\.com|discordapp\.com)/channels/[0-9]+/[0-9]+/[0-9]+", url)):
        raise ValueError("delivery receipt must be a GitHub or Discord message URL")


def marker_body(event, question_url, delivery_url=None):
    record = {"version": 1, "event": event, "question_url": question_url}
    if delivery_url is not None:
        record["delivery_url"] = delivery_url
    line = "<!-- director-question:delivery " + json.dumps(record, sort_keys=True) + " -->"
    return f"{line}\n\nQuestion {event}: [original question]({question_url})." + (
        f" [Presentation receipt]({delivery_url}). This acknowledges delivery, not a decision.\n" if delivery_url else "\n"
    )


def post(queue, body):
    match = oq.ITEM.fullmatch(queue)
    if not match:
        raise ValueError("queue must be OWNER/REPO#NUMBER")
    # The maintained helper owns automation identity, body deduplication and
    # uncertain-write reconciliation. Never retry its unknown outcome here.
    try:
        result = github_comment.comment("issue", int(match[2]), body, repo=match[1], dedupe_body=True)
    except github_comment.CommentError as error:
        raise ValueError(str(error)) from error
    # Library payloads have no CLI `ok` envelope. A created/reused comment plus
    # the subsequent full readback proves delivery, including reconciled writes.
    if (result.get("comment_action") not in {"created", "existing"}
            or not isinstance(result.get("url"), str)
            or source_item(result["url"]).casefold() != queue.casefold()
            or result.get("outcome_certainty") not in {None, "confirmed", "reconciled_applied", "not_applicable"}):
        raise ValueError("question delivery write is unconfirmed; read queue before retrying")
    return result


@contextmanager
def queue_lock():
    # Same shared Code home as Q-number allocation, never a provider home.
    path = counter_path().with_name("director-question-delivery.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def settled(url, queue_comments, owner, decision_authors):
    """An explicit linked decision can dispose of even a deleted source."""
    reference = {"id": int(COMMENT_URL.fullmatch(url)[3]), "url": url, "body": "Owner question: unavailable source"}
    answers = [c for c in queue_comments if not (c.get("body") or "").lstrip().startswith("<!-- director-question:delivery ")]
    return oq.questions([reference], owner, decision_authors, answers, recorded_decisions_only=True)[0]["status"] == "answered"


def is_receipt(comment) -> bool:
    lines = (comment.get("body") or "").strip().splitlines()
    return bool(lines) and MARKER.fullmatch(lines[0]) is not None


def inventory(source_comments, queue_comments, owner, decision_authors, now=None, *, question_urls=None) -> dict[str, Any]:
    """Read-only view; a delivery receipt never removes an unanswered question."""
    now = now or datetime.now(timezone.utc)
    trusted = {author.casefold() for author in [owner, *decision_authors]}
    entries = records(queue_comments, trusted)
    untrusted_records = []
    for comment in queue_comments:
        if is_receipt(comment) and (comment.get("author") or "").casefold() not in trusted:
            untrusted_records.append({"url": comment["url"], "author": comment.get("author")})
    registrations, acknowledgements = {}, {}
    for record in entries:
        if record["event"] == "registered":
            registrations.setdefault(record["question_url"], record["record_url"])
        elif record["question_url"] in registrations:
            acknowledgements[record["question_url"]] = record
    source_comments = sorted((c for c in source_comments if not is_receipt(c)), key=lambda c: c.get("created_at") or "")
    answers = [c for c in queue_comments if not is_receipt(c)]
    rows = []
    # Source URL is immutable identity. A repeat presentation keeps its Q ID;
    # registration never allocates a second number from the shared counter.
    found = oq.questions(source_comments, owner, decision_authors, answers, recorded_decisions_only=True)
    candidates = {q["url"]: q["answers"] for q in oq.questions(source_comments, owner, decision_authors, answers)}
    source_authors = {c["url"]: (c.get("author") or "") for c in source_comments}
    for question in found:
        if question["status"] == "answered":
            continue
        url = question["url"]
        if question_urls is not None and url not in question_urls:
            continue
        number = question_id(question["question"])
        source_item(url)
        created = question.get("created_at")
        age = None
        if created:
            parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("question timestamp needs a timezone")
            age = max(0, (now - parsed).total_seconds())
        receipt = acknowledgements.get(url)
        rows.append({**question, "question_id": number,
                     "question": question["question"] if number else None,
                     "status": question["status"] if number else "needs_number",
                     "needs_repair": number is None, "deliverable": number is not None,
                     "candidate_answers": candidates[url],
                     "source_author": source_authors[url],
                     "author_trusted": source_authors[url].casefold() in trusted,
                     "registration_url": registrations.get(url),
                     "delivery": "needs_number" if not number else "acknowledged" if receipt else "unacknowledged",
                     "delivery_url": receipt["delivery_url"] if receipt else None,
                     "resurface": receipt is None, "age_seconds": age})
    known = {row["url"] for row in found}
    missing = sorted(url for url in set(registrations) - known
                     if (question_urls is None or url in question_urls)
                     and not settled(url, queue_comments, owner, decision_authors))
    for url in missing:
        rows.append({"url": url, "status": "missing_source", "question": None,
                     "created_at": None, "question_id": None, "age_seconds": None,
                     "registration_url": registrations[url], "resurface": True,
                     "delivery": "unknown", "delivery_url": None})
    rows.sort(key=lambda row: (row.get("created_at") or "", row["url"]))
    for position, row in enumerate(rows, 1):
        row["position"] = position
    untrusted = [q["url"] for q in rows if q.get("author_trusted") is False]
    unnumbered = [q["url"] for q in rows if q.get("needs_repair")]
    return {"complete": not (missing or untrusted or unnumbered), "queue_length": len(rows), "questions": rows,
            "unnumbered_sources": unnumbered,
            "missing_sources": missing, "untrusted_sources": untrusted,
            "untrusted_records": untrusted_records,
            "coverage": "Configured trusted queue authors; untrusted queue records are warnings only",
            "unacknowledged_count": sum(row["resurface"] for row in rows)}


def run_queue(args, fetch):
    if not oq.ITEM.fullmatch(args.queue):
        raise ValueError("queue must be OWNER/REPO#NUMBER")
    if args.queue.split("/")[0].casefold() != args.owner.casefold():
        raise ValueError("queue must belong to the configured Director")
    if args.register or args.ack:
        with queue_lock():
            return reconcile(args, fetch)
    return reconcile(args, fetch)


def reconcile(args, fetch):
    trusted = {author.casefold() for author in [args.owner, *args.decision_author]}
    queue_comments = fetch(args.queue)
    existing = records(queue_comments, trusted)
    targets = {value for value in args.items if COMMENT_URL.fullmatch(value)}
    if args.register and not targets:
        raise ValueError("registration requires exact original question URLs, not whole discussions")
    registered = set() if targets else {r["question_url"] for r in existing}
    active = {url for url in registered if not settled(url, queue_comments, args.owner, args.decision_author)}
    items = list(dict.fromkeys([*(source_item(value) if value in targets else value for value in args.items),
                               *(source_item(url) for url in sorted(active))]))
    selected = registered | targets
    explicit_discussions = set(args.items) - targets
    comments_by_url: dict[str, dict[str, Any]] = {}
    errors = []
    for item in items:
        try:
            source_comments = fetch(item)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            errors.append({"item": item, "error": str(error)})
            continue
        for comment in source_comments:
            comments_by_url[comment["url"]] = comment
            if item in explicit_discussions:
                selected.add(comment["url"])
    comments = list(comments_by_url.values())
    recognized = {q["url"] for q in oq.questions(comments, args.owner, args.decision_author)}
    for target in sorted(targets - recognized):
        if not settled(target, queue_comments, args.owner, args.decision_author):
            errors.append({"question_url": target, "error": "explicit source question missing or edited"})
    report = inventory(comments, queue_comments, args.owner, args.decision_author, question_urls=selected)
    writes = []
    if args.register:
        for target in targets:
            source = comments_by_url.get(target)
            if source is None or not oq.questions([source], args.owner, args.decision_author):
                raise ValueError(f"registration source is not an explicit question: {target}")
            if not question_id(source.get("body") or ""):
                raise ValueError(f"Question has no heading Q ID; number the original comment before delivery: {target}")
            if (source.get("author") or "").casefold() not in trusted:
                raise ValueError(f"question author needs review: {target}")
        for question in report["questions"]:
            if question["url"] in targets and not question["registration_url"]:
                writes.append(marker_body("registered", question["url"]))
    if args.ack:
        validate_receipt(args.delivery_url)
        question: dict[str, Any] | None = next((q for q in report["questions"] if q["url"] == args.ack), None)
        if question is None or not question["registration_url"]:
            raise ValueError("acknowledgement requires a registered unresolved question")
        if not question["question_id"]:
            raise ValueError(f"Question has no heading Q ID; number the original comment before delivery: {args.ack}")
        if args.delivery_url in {args.ack, question["registration_url"]}:
            raise ValueError("source/registration is not a presentation receipt")
        if not question["delivery_url"]:
            writes.append(marker_body("delivered", args.ack, args.delivery_url))
    for body in writes:
        post(args.queue, body)
        # Read back each record before the next write. A missing record remains
        # unacknowledged rather than being optimistically hidden.
        queue_comments = fetch(args.queue)
        if not any(c.get("body", "").strip().startswith(body.strip())
                   and (c.get("author") or "").casefold() in trusted for c in queue_comments):
            raise ValueError("question delivery record readback failed")
    report = inventory(comments, queue_comments, args.owner, args.decision_author, question_urls=selected)
    return {"queue": args.queue, "scope": items, "settled_questions": sorted(registered - active), **report,
            "source_errors": errors, "complete": report["complete"] and not errors}
