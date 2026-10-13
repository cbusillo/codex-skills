#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Shared merge-train handoff format and current-head selection."""

import re
import sys
from datetime import datetime
from pathlib import Path

READY = "Ready for the merge train"
WAITING = "Engineering ready; waiting for"
HEAD = re.compile(
    r"(?im)^Head(?: SHA)?:[ \t]*(?P<marker>\*\*|`)?"
    r"(?P<sha>[0-9a-f]{40})(?(marker)(?P=marker))(?:[.\s]|$)"
)


def handoff_line(line):
    """Normalize Ready and renewed waits with the same presentation rules."""
    line = line.strip().removesuffix(".")
    if line.startswith("**") and line.endswith("**"):
        line = line[2:-2].removesuffix(".")
    return line


def handoff_body(head_sha: str, *, waiting_for_client: bool = False) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ValueError("handoff requires a full 40-character head SHA")
    first = f"{WAITING} the Client's acceptance" if waiting_for_client else READY
    return f"{first}\n\nHead: {head_sha}"


def director_standing_acceptance(repository):
    """Use Launchplane's repository mapping and scoped profile, never repo config."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from skills.launchplane.scripts.launchplane_acceptance import read_director_standing

    return read_director_standing(repository)


def requires_client_acceptance(pr, check_runs, *, repository=""):
    """Keep the producer and watcher on the same Client-review path."""
    if any(label["name"].casefold() == "owner-review" for label in pr["labels"]):
        return True
    # Launchplane completes engineering-only advisory checks as neutral.
    # Pending, accepted and rejected reviews still follow the acceptance path.
    if any(
        check["name"] == "launchplane/owner-review"
        and not (check.get("status") == "completed" and check.get("conclusion") == "neutral")
        for check in check_runs
    ):
        return True
    body = pr.get("body") or ""
    for heading in re.finditer(r"(?im)^## (?:Client|Owner) test notes[ \t]*\r?$", body):
        notes = re.split(r"(?m)^## ", body[heading.end():], maxsplit=1)[0].strip()
        if not re.match(r"(?i)Nothing for the (?:Client|owner) to test\b", notes):
            return not (repository and director_standing_acceptance(repository))
    return False


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
        first = handoff_line(body.splitlines()[0]) if body.splitlines() else ""
        if first == READY or first.startswith(WAITING):
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
    return handoff_line(body.splitlines()[0]) == READY and [
        match.group("sha") for match in HEAD.finditer(body)
    ] == [pr["head"]["sha"]]
