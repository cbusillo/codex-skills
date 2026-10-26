#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import argparse
import base64
import hashlib
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
COUNTER_BRANCH = "state/director-questions"
COUNTER_FILE = "counter.json"
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


def counter_request(method: str, path: str, body: dict[str, Any] | None = None,
                    allowed: tuple[int, ...] = ()) -> Any:
    sys.path.append(str(Path(__file__).resolve().parents[2] / "github/scripts"))
    import github_api
    import github_identity

    result = github_api.call_gh_with_retry(method, path, body,
        operation="github.direction.counter_read" if method == "GET" else "github.direction.counter_write",
        expected_actor=github_identity.automation_login(), is_write=method != "GET", conditional_cache=False)
    if not result.ok and result.status not in allowed:
        reason = result.failure.cause if result.failure else "unknown"
        raise ValueError(f"GitHub question counter {reason} (HTTP {result.status}); preserve the pending request and reconcile before retrying")
    return result


def counter_endpoint(repo: str) -> str:
    return f"/repos/{repo}/contents/{COUNTER_FILE}"


def initialize_counter(repo: str, minimum: int) -> None:
    reference = counter_request("GET", f"/repos/{repo}/git/ref/heads/{COUNTER_BRANCH}", allowed=(404,))
    if reference.ok:
        raise ValueError("Counter branch exists without its state file; preserve it and restore the last verified state")
    state = {"version": 1, "director": repo.split("/", 1)[0].casefold(), "last_id": minimum, "questions": {}}
    tree = counter_request("POST", f"/repos/{repo}/git/trees", {"tree": [
        {"path": COUNTER_FILE, "mode": "100644", "type": "blob", "content": json.dumps(state, sort_keys=True)}]})
    commit = counter_request("POST", f"/repos/{repo}/git/commits",
                             {"message": "Initialize Director question counter", "tree": tree.body["sha"], "parents": []})
    created = counter_request("POST", f"/repos/{repo}/git/refs",
                              {"ref": f"refs/heads/{COUNTER_BRANCH}", "sha": commit.body["sha"]}, allowed=(422,))
    if not created.ok:
        counter_request("GET", counter_endpoint(repo) + f"?ref={COUNTER_BRANCH}")


def read_counter(repo: str, minimum: int) -> tuple[str, dict[str, Any]]:
    result = counter_request("GET", counter_endpoint(repo) + f"?ref={COUNTER_BRANCH}", allowed=(404,))
    if not result.ok:
        initialize_counter(repo, minimum)
        result = counter_request("GET", counter_endpoint(repo) + f"?ref={COUNTER_BRANCH}")
    state = json.loads(base64.b64decode(result.body["content"], validate=False))
    if (not isinstance(state, dict) or state.get("version") != 1
            or state.get("director") != repo.split("/", 1)[0].casefold()
            or type(state.get("last_id")) is not int or state["last_id"] < 0
            or not isinstance(state.get("questions"), dict)
            or any(not isinstance(key, str) or type(number) is not int or not 0 < number <= state["last_id"]
                   for key, number in state["questions"].items())):
        raise ValueError("Invalid shared question counter; preserve it and restore its last verified state")
    return result.body["sha"], state


def write_counter(repo: str, sha: str, state: dict[str, Any]) -> bool:
    content = base64.b64encode((json.dumps(state, sort_keys=True) + "\n").encode()).decode()
    result = counter_request("PUT", counter_endpoint(repo), {"message": "Advance Director question counter",
                             "branch": COUNTER_BRANCH, "sha": sha, "content": content}, allowed=(409,))
    return result.ok


def reserve_question(minimum: int, key: str | None, repo: str | None) -> int:
    if repo is None:
        return minimum + int(key is not None)
    request_key = hashlib.sha256(key.encode()).hexdigest() if key is not None else None
    try:
        for _ in range(8):
            sha, state = read_counter(repo, minimum)
            if request_key in state["questions"]:
                return state["questions"][request_key]
            state["last_id"] = max(state["last_id"], minimum)
            if request_key is not None:
                state["last_id"] += 1
                state["questions"][request_key] = state["last_id"]
            if write_counter(repo, sha, state):
                return state["last_id"]
    except (KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        raise ValueError("Shared question allocation could not be confirmed; preserve its request and retry after reconciliation") from error
    raise ValueError("Shared counter changed concurrently; preserve the pending request and retry allocation")


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
        queue = configured_queue()
        repository = queue.split("#", 1)[0] if queue else None
        if state.get("counter_repository") and state["counter_repository"] != repository:
            raise ValueError("Restore the recorded counter repository in [questions].queue before allocating")
        if repository:
            state["counter_repository"] = repository
        if allocate:
            key = question_url or state.setdefault("pending_request", uuid.uuid4().hex)
            save_state(path, state)
            number = reserve_question(state["last_id"], key, repository)
            state["last_id"] = max(state["last_id"], number)
            if question_url is None:
                state.pop("pending_request", None)
        elif repository:
            number = reserve_question(state["last_id"], None, repository)
            state["last_id"] = max(state["last_id"], number)
        if question_url is not None:
            state["questions"][question_url] = number
        save_state(path, state)
        return number


def main() -> int:
    parser = argparse.ArgumentParser(description="Allocate Director question IDs from a shared counter or a local single-machine counter")
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
