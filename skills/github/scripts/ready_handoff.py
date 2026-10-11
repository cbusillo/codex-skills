#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Shared merge-train handoff format and current-head selection."""

import re
from datetime import datetime

READY = "Ready for the merge train"
WAITING = "Engineering ready; waiting for"
HEAD = re.compile(
    r"(?im)^Head(?: SHA)?:[ \t]*(?P<marker>\*\*|`)?"
    r"(?P<sha>[0-9a-f]{40})(?(marker)(?P=marker))(?:[.\s]|$)"
)


def ready_line(line):
    """Accept presentation variants without broadening the readiness statement."""
    line = line.strip()
    if line.endswith("."):
        line = line[:-1]
    if line.startswith("**") and line.endswith("**"):
        line = line[2:-2]
    return line.removesuffix(".") == READY


def handoff_body(head_sha: str, *, waiting_for_client: bool = False) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ValueError("handoff requires a full 40-character head SHA")
    first = f"{WAITING} the Client's acceptance" if waiting_for_client else READY
    return f"{first}\n\nHead: {head_sha}"


def requires_client_acceptance(pr, check_runs):
    """Keep the producer and watcher on the same Client-review path."""
    if any(label["name"].casefold() == "owner-review" for label in pr["labels"]):
        return True
    if any(check["name"] == "launchplane/owner-review" for check in check_runs):
        return True
    body = pr.get("body") or ""
    return bool(
        re.search(r"(?im)^## (?:Client|Owner) test notes\s*$", body)
        and not re.search(r"(?im)^Nothing for the Client to test[:;.](?:[ \t]+[^\n]*)?[ \t]*$", body)
    )


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return parsed.timestamp()


def latest_ready(pr, comments, trusted):
    """A renewed wait invalidates Ready; a push requires a new explicit head."""
    handoffs = []
    for comment in comments:
        if (comment.get("user") or {}).get("login", "").casefold() not in trusted:
            continue
        body = comment.get("body") or ""
        first = body.splitlines()[0] if body.splitlines() else ""
        if ready_line(first) or first.startswith(WAITING):
            handoffs.append(comment)
    if not handoffs:
        return False
    latest = max(
        handoffs,
        key=lambda item: (
            timestamp(item.get("updated_at") or item["created_at"]),
            item["id"],
        ),
    )
    body = latest["body"]
    return ready_line(body.splitlines()[0]) and [
        match.group("sha") for match in HEAD.finditer(body)
    ] == [pr["head"]["sha"]]
