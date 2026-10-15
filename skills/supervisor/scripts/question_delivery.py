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
import sys

import oq

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))
import github_comment

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "direction/scripts"))
from question_id import QUESTION, counter_path

COMMENT_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/([1-9][0-9]*)#issuecomment-([1-9][0-9]*)")
MARKER = re.compile(r"<!-- director-question:delivery (\{[^\n]*\}) -->")


def source_item(url):
    match = COMMENT_URL.fullmatch(url)
    if not match:
        raise ValueError("question must be an exact GitHub issue-comment URL")
    return f"{match[1]}#{match[2]}"


def question_id(body):
    """Only the heading's own ID, never another question mentioned in its context."""
    heading = body.lstrip().lstrip("*")
    for prefix in oq.QUESTION_PREFIXES:
        if heading.startswith(prefix):
            match = QUESTION.match(heading[len(prefix):].lstrip(" *"))
            return match[0] if match else None
    return None


def records(comments, trusted):
    """Only standalone first-line records from configured trusted authors count."""
    result = []
    for comment in comments:
        if comment.get("author", "").casefold() not in trusted:
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
    if not isinstance(url, str) or not re.fullmatch(
        r"https://(?:github\.com/[A-Za-z0-9_./#-]+|(?:discord\.com|discordapp\.com)/channels/[0-9]+/[0-9]+/[0-9]+)", url
    ):
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
    if result.get("ok") is not True or result.get("outcome_certainty") != "confirmed":
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


def inventory(source_comments, queue_comments, owner, decision_authors, now=None):
    """Read-only view; a delivery receipt never removes an unanswered question."""
    now = now or datetime.now(timezone.utc)
    trusted = {author.casefold() for author in [owner, *decision_authors]}
    entries = records(queue_comments, trusted)
    registrations, acknowledgements = {}, {}
    for record in entries:
        if record["event"] == "registered":
            registrations.setdefault(record["question_url"], record["record_url"])
        elif record["question_url"] in registrations:
            acknowledgements[record["question_url"]] = record
    def without_receipts(comments):
        result = []
        for comment in comments:
            lines = (comment.get("body") or "").strip().splitlines()
            if not lines or not MARKER.fullmatch(lines[0]):
                result.append(comment)
        return result

    source_comments = sorted(without_receipts(source_comments), key=lambda c: c.get("created_at") or "")
    source_comments = [c for c in source_comments if c.get("author", "").casefold() in trusted]
    answers = without_receipts(queue_comments)
    rows = []
    # Source URL is immutable identity. A repeat presentation keeps its Q ID;
    # registration never allocates a second number from the shared counter.
    found = oq.questions(source_comments, owner, decision_authors, answers)
    for question in found:
        if question["status"] == "answered":
            continue
        url = question["url"]
        source_item(url)
        created = question.get("created_at")
        age = None
        if created:
            parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("question timestamp needs a timezone")
            age = max(0, (now - parsed).total_seconds())
        receipt = acknowledgements.get(url)
        rows.append({**question, "question_id": question_id(question["question"]),
                     "registration_url": registrations.get(url),
                     "delivery": "acknowledged" if receipt else "unacknowledged",
                     "delivery_url": receipt["delivery_url"] if receipt else None,
                     "resurface": receipt is None, "age_seconds": age})
    known = {row["url"] for row in found}
    if set(registrations) - known:
        raise ValueError("registered question missing from source reads; queue coverage incomplete")
    rows.sort(key=lambda row: (row.get("created_at") or "", row["url"]))
    for position, row in enumerate(rows, 1):
        row["position"] = position
    return {"complete": True, "queue_length": len(rows), "questions": rows,
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
    items = list(dict.fromkeys([*args.items, *(source_item(r["question_url"]) for r in existing)]))
    if any(item.split("/")[0].casefold() != args.owner.casefold() for item in items):
        raise ValueError("source discussions must belong to the configured Director")
    comments_by_url = {}
    for item in items:
        for comment in fetch(item):
            comments_by_url[comment["url"]] = comment
    comments = list(comments_by_url.values())
    report = inventory(comments, queue_comments, args.owner, args.decision_author)
    writes = []
    if args.register:
        for question in report["questions"]:
            if not question["registration_url"]:
                writes.append(marker_body("registered", question["url"]))
    if args.ack:
        validate_receipt(args.delivery_url)
        question = next((q for q in report["questions"] if q["url"] == args.ack), None)
        if not question or not question["registration_url"]:
            raise ValueError("acknowledgement requires a registered unresolved question")
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
                   and c.get("author", "").casefold() in trusted for c in queue_comments):
            raise ValueError("question delivery record readback failed")
    return {"queue": args.queue, "scope": items,
            **inventory(comments, queue_comments, args.owner, args.decision_author)}
