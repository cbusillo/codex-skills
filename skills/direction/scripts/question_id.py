#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import uuid
from pathlib import Path
from typing import Any

QUESTION = re.compile(r"\bQ([1-9][0-9]*)\b")
ITEM = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*")
RESERVATION = re.compile(r"<!-- director-question:allocation (.+) -->")
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


def configured_queue() -> str | None:
    sys.path.append(str(Path(__file__).resolve().parents[2] / "supervisor/scripts"))
    import account_choice

    for path in account_choice.config_paths(os.environ, Path.home()):
        if not path.exists():
            continue
        try:
            section = tomllib.loads(path.read_text(encoding="utf-8")).get("questions")
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError(f"cannot read private supervisor config {path}") from error
        if section is None:
            continue
        queue = section.get("queue") if isinstance(section, dict) else None
        if not isinstance(queue, str) or not ITEM.fullmatch(queue):
            raise ValueError(f'[questions] in {path} needs queue = "OWNER/REPO#NUMBER"')
        return queue
    return None


def read_discussion(item: str) -> dict[str, Any]:
    if not ITEM.fullmatch(item):
        raise ValueError("Numbering discussion must be OWNER/REPO#NUMBER")
    helper = Path(__file__).resolve().parents[2] / "github/scripts/gh-plan.py"
    repo, number = item.split("#")
    run = subprocess.run(["uv", "run", str(helper), "--repo", repo, "show", number, "--full"],
                         capture_output=True, text=True, check=True, timeout=120)
    result = json.loads(run.stdout)
    if (result.get("ok") is not True or result.get("disposition") != "complete"
            or result.get("outcome_certainty") not in {"confirmed", "not_applicable"}):
        raise ValueError(f"Incomplete numbering discussion read: {item}")
    return result


def heading_numbers(body: str) -> list[int]:
    numbers = []
    fence = None
    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence is None and (question := heading_id(line)):
            numbers.append(int(question[1:]))
    return numbers


def question_maximum(result: dict[str, Any], director: str) -> int:
    issue = result["issue"]
    trusted = {director.casefold(), str(result.get("expected_actor") or "").casefold()}
    records = [{"body": issue["body"], "author": issue.get("provenance", {}).get("author")}, *issue["comments"]]
    return max((number for record in records
                if str(record.get("author") or "").casefold() in trusted - {""}
                for number in heading_numbers(record["body"])), default=0)


def github_seed(items: list[str]) -> tuple[int, list[str]]:
    if not items or any(not ITEM.fullmatch(item) for item in items):
        raise ValueError("--seed-from must be OWNER/REPO#NUMBER")
    director = items[0].split("/", 1)[0].casefold()
    if any(item.split("/", 1)[0].casefold() != director for item in items):
        raise ValueError("Seed discussions must belong to the same Director")
    highest, sources = 0, []
    for item in items:
        result = read_discussion(item)
        highest = max(highest, question_maximum(result, director))
        sources.append(result["issue"]["url"])
    if not highest:
        raise ValueError("Seed discussions contain no Director question headings; supply the current numbering records")
    return highest, sources


def post_reservation(queue: str, body: str) -> None:
    sys.path.append(str(Path(__file__).resolve().parents[2] / "github/scripts"))
    import github_comment

    repo, number = queue.split("#")
    try:
        result = github_comment.comment("issue", int(number), body, repo=repo, dedupe_body=True)
    except github_comment.CommentError as error:
        raise ValueError(str(error)) from error
    if (result.get("comment_action") not in {"created", "existing"}
            or result.get("outcome_certainty") not in {None, "confirmed", "reconciled_applied", "not_applicable"}):
        raise ValueError("Question allocation write is unconfirmed; reconcile its queue record before retrying")


def reservations(result: dict[str, Any], director: str) -> dict[str, int]:
    trusted = {director.casefold(), str(result.get("expected_actor") or "").casefold()} - {""}
    assignments: dict[str, int] = {}
    highest = 0
    for comment in sorted(result["issue"]["comments"], key=lambda entry: entry["id"]):
        if str(comment.get("author") or "").casefold() not in trusted:
            continue
        match = RESERVATION.match(comment["body"])
        if match is None:
            if "<!-- director-question:allocation" in comment["body"]:
                raise ValueError("Malformed allocation record; preserve the queue before recovery")
            continue
        record = json.loads(match[1])
        if (not isinstance(record, dict) or record.get("version") != 1 or not isinstance(record.get("key"), str) or not record["key"]
                or type(record.get("minimum")) is not int or record["minimum"] < 0):
            raise ValueError("Invalid allocation record; preserve the queue before recovery")
        if record["key"] not in assignments:
            highest = max(highest, record["minimum"]) + 1
            assignments[record["key"]] = highest
    return assignments


def reserve_question(minimum: int, key: str, sources: list[str]) -> int:
    queue = configured_queue()
    if queue is None:
        raise ValueError('Configure the existing [questions] queue = "OWNER/REPO#NUMBER" in skill-data/supervisor.toml on each machine')
    director = queue.split("/", 1)[0]
    result = read_discussion(queue)
    assigned = reservations(result, director)
    if key in assigned:
        return assigned[key]
    for source in sources:
        match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/issues/([1-9][0-9]*)", source)
        if match is None or match[1].split("/", 1)[0].casefold() != director.casefold():
            raise ValueError("Counter seed sources must belong to the configured queue's Director")
        minimum = max(minimum, question_maximum(read_discussion(f"{match[1]}#{match[2]}"), director))
    minimum = max(minimum, question_maximum(result, director))
    body = "<!-- director-question:allocation " + json.dumps({"version": 1, "key": key, "minimum": minimum}, sort_keys=True) + " -->"
    post_reservation(queue, body)
    assigned = reservations(read_discussion(queue), director)
    if key not in assigned:
        raise ValueError("Question allocation absent from queue readback; preserve the pending request before recovery")
    return assigned[key]


def save_state(path: Path, state: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(state, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def update(path: Path, *, seed: tuple[int, list[str]] | None = None, allocate: bool = True,
           question_url: str | None = None) -> int:
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
        if "pending_request" in state and (not isinstance(state["pending_request"], str) or not state["pending_request"]):
            raise ValueError("Invalid pending question allocation; preserve the counter before recovery")
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
        number = state["last_id"]
        if allocate:
            key = question_url or state.setdefault("pending_request", uuid.uuid4().hex)
            save_state(path, state)
            number = reserve_question(state["last_id"], key, state["seed_sources"])
            state["last_id"] = max(state["last_id"], number)
            if question_url is None:
                state.pop("pending_request", None)
        if question_url is not None:
            state["questions"][question_url] = number
        save_state(path, state)
        return number


def main() -> int:
    parser = argparse.ArgumentParser(description="Allocate Director question IDs through the shared GitHub question queue")
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
