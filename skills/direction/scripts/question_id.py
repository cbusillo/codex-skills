#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Allocate Director question IDs from one locked counter shared by both harnesses."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

QUESTION = re.compile(r"\bQ([1-9][0-9]*)\b")
ITEM = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*")
QUESTION_HEADING = re.compile(r"^\s*\**(?:Owner|Director) question:[ \t*]*")


def heading_id(body: str) -> str | None:
    heading = QUESTION_HEADING.match(body)
    match = QUESTION.match(body, heading.end()) if heading else None
    if not match:
        return None
    # Preserve legacy bare question IDs. Q1 through Q4 also name calendar
    # quarters, so those labels need a separator before question text.
    if int(match[1]) > 4:
        return match[0]
    suffix = body[match.end():].lstrip(" *\t")
    return match[0] if not suffix or suffix[0] in ".—–" else None


def number_heading(body: str, question: str) -> str:
    heading = QUESTION_HEADING.match(body)
    if not heading or heading_id(body):
        return body
    return body[:heading.end()] + question + ". " + body[heading.end():]


def counter_path() -> Path:
    # Provider-specific homes must never split the shared counter.
    return Path(os.environ.get("CODE_HOME") or Path.home() / ".code").expanduser() / "director-question-counter.json"


def github_seed(items: list[str]) -> tuple[int, list[str]]:
    """Read the current numbering discussions, including all their comments."""
    helper = Path(__file__).resolve().parents[2] / "github/scripts/gh-plan.py"
    highest = 0
    sources = []
    for item in items:
        if not ITEM.fullmatch(item):
            raise ValueError("--seed-from must be OWNER/REPO#NUMBER")
        # Name the repository so the read never depends on the working directory.
        repo, number = item.split("#")
        run = subprocess.run(["uv", "run", str(helper), "--repo", repo, "show", number, "--full"],
                             capture_output=True, text=True, check=True, timeout=120)
        result = json.loads(run.stdout)
        if (result.get("ok") is not True or result.get("disposition") != "complete"
                or result.get("outcome_certainty") not in {"confirmed", "not_applicable"}):
            raise ValueError(f"Incomplete numbering discussion read: {item}")
        issue = result["issue"]
        bodies = [issue["body"], *(comment["body"] for comment in issue["comments"])]
        highest = max([highest, *(int(match.group(1)) for body in bodies for match in QUESTION.finditer(body))])
        sources.append(issue["url"])
    if not highest:
        raise ValueError("Seed discussions contain no question IDs; supply the current numbering records")
    return highest, sources


def update(path: Path, *, seed: tuple[int, list[str]] | None = None, allocate: bool = True,
           question_url: str | None = None) -> int:
    """Lock a stable sidecar across read, increment, and atomic replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state: dict[str, Any]
        try:
            state = json.loads(path.read_text())
        except FileNotFoundError:
            if seed is None:
                raise ValueError("Counter is not seeded; use --seed-from for every current numbering discussion") from None
            state = {"last_id": 0, "seed_sources": []}
        if (not isinstance(state, dict) or type(state.get("last_id")) is not int
                or state["last_id"] < 0 or not isinstance(state.get("seed_sources"), list)):
            raise ValueError("Invalid question counter; preserve it and recover its last verified value before allocating")
        if seed is not None:
            state["last_id"] = max(state["last_id"], seed[0])
            state["seed_sources"] = list(dict.fromkeys([*state["seed_sources"], *seed[1]]))
        if question_url is not None:
            if not allocate or not question_url:
                raise ValueError("A confirmed question URL requires allocation")
            assignments = state.setdefault("questions", {})
            if (not isinstance(assignments, dict) or any(
                    not isinstance(url, str) or type(number) is not int or not 0 < number <= state["last_id"]
                    for url, number in assignments.items())):
                raise ValueError("Invalid question assignments; preserve the counter before recovery")
            if question_url in assignments:
                return assignments[question_url]
        if allocate:
            state["last_id"] += 1
        if question_url is not None:
            state["questions"][question_url] = state["last_id"]
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".pending-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(json.dumps(state, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return state["last_id"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("next", "seed"))
    parser.add_argument("--seed-from", action="append", default=[], metavar="OWNER/REPO#NUMBER",
                        help="Current question/decision discussions; read all of them on initial setup")
    args = parser.parse_args()
    try:
        if args.action == "seed" and not args.seed_from:
            raise ValueError("seed requires --seed-from numbering discussions")
        seed = github_seed(args.seed_from) if args.seed_from else None
        number = update(counter_path(), seed=seed, allocate=args.action == "next")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Q{number}" if args.action == "next" else f"Seeded through Q{number}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
