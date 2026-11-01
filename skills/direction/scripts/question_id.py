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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from skills.github.scripts.github_plan_status import questions as question_records

QUESTION = question_records.QUESTION
QUESTION_HEADING = question_records.QUESTION_HEADING
heading_id = question_records.heading_id

ITEM = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*")
COUNTER_BRANCH = "state/director-questions"
COUNTER_FILE = "counter.json"
RESERVED_RANGE = re.compile(r"^\s*(?:Reserved|Reserving)\s+Q([1-9][0-9]*)\s*(?:[-–—]|through|to)\s*Q([1-9][0-9]*)\b", re.IGNORECASE)
NUMBER_HELP = ("Keep an existing question's source unchanged and re-present its original Q ID and link. "
               "For a new, never-posted question without a usable receipt, use question_id.py next; "
               "obtain exact-text approval again if the previously approved text contained the old number.")


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
        if fence is None:
            if question := heading_id(line):
                numbers.append(int(question[1:]))
            elif reserved := RESERVED_RANGE.match(line):
                numbers.append(max(map(int, reserved.groups())))
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
    queue = configured_queue()
    director = (queue or items[0]).split("/", 1)[0].casefold()
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


@dataclass
class CounterSnapshot:
    file_sha: str
    state: dict[str, Any]
    commit_sha: str


@dataclass
class Allocation:
    number: int
    commit_sha: str | None = None


def validate_counter(state: Any) -> None:
    if (not isinstance(state, dict) or type(state.get("last_id")) is not int or state["last_id"] < 0
            or not isinstance(state.get("questions", {}), dict)
            or any(not isinstance(key, str) or type(number) is not int or not 0 < number <= state["last_id"]
                   for key, number in state.get("questions", {}).items())):
        raise ValueError("Invalid question counter; preserve it and recover its last verified state")
    reservations = state.get("reservations", {})
    if (not isinstance(reservations, dict) or any(
            not isinstance(key, str) or type(number) is not int or not 0 < number <= state["last_id"]
            for key, number in reservations.items())):
        raise ValueError("Invalid question reservations; preserve the counter before recovery")


def require_number(state: dict[str, Any], number: int, source: str | None = None) -> None:
    reservations = state.get("reservations", {})
    owners = {key for key, value in state.get("questions", {}).items() if value == number and key not in reservations}
    if owners == {source} or (not owners and number in reservations.values()):
        return
    raise ValueError(f"Q{number} has no reservation or belongs to another source. {NUMBER_HELP}")


def reservation_key(state: dict[str, Any], number: int | None) -> str | None:
    return next((key for key, value in state.get("reservations", {}).items() if value == number), None)


def require_receipt(state: dict[str, Any], number: int, receipt: str | None) -> None:
    key = hashlib.sha256(receipt.encode()).hexdigest() if receipt else None
    if key is None or state.get("reservations", {}).get(key) != number:
        raise ValueError(f"This machine has no allocation receipt for that unbound number. {NUMBER_HELP}")


def verify_number(path: Path, number: int, source: str | None = None) -> None:
    with path.with_name(path.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text())
        validate_counter(state)
        repository = state.get("counter_repository")
        queue = configured_queue()
        if repository and (not queue or repository != queue.split("#", 1)[0]
                           or state.get("counter_seed_pending") or not state.get("counter_commit")):
            raise ValueError("Restore and reconcile the recorded shared counter before checking a question number")
        if repository:
            receipt = reservation_key(state, number)
            state = read_counter(repository, state["last_id"]).state
            source = hashlib.sha256(source.encode()).hexdigest() if source else None
            if source is None or state["questions"].get(source) != number:
                require_receipt(state, number, receipt)
        require_number(state, number, source)


def decoded_counter(repo: str, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("encoding") == "none":
        payload = counter_request("GET", f"/repos/{repo}/git/blobs/{payload['sha']}").body
    state = json.loads(base64.b64decode(payload["content"]))
    validate_counter(state)
    if state.get("director") != repo.split("/", 1)[0].casefold() or state.get("version") != 1:
        raise ValueError("Counter state does not belong to this Director; preserve it before recovery")
    return state


def initialize_counter(repo: str, minimum: int, assignments: dict[str, int] | None = None) -> None:
    state = {"version": 1, "director": repo.split("/", 1)[0].casefold(), "last_id": minimum, "questions": assignments or {}}
    tree = counter_request("POST", f"/repos/{repo}/git/trees", {"tree": [
        {"path": COUNTER_FILE, "mode": "100644", "type": "blob", "content": json.dumps(state, sort_keys=True)}]})
    commit = counter_request("POST", f"/repos/{repo}/git/commits",
                             {"message": "Initialize Director question counter", "tree": tree.body["sha"], "parents": []})
    counter_request("POST", f"/repos/{repo}/git/refs",
                    {"ref": f"refs/heads/{COUNTER_BRANCH}", "sha": commit.body["sha"]}, allowed=(422,))


def read_counter(repo: str, minimum: int, *, create: bool = False, restore_from: str | None = None,
                 assignments: dict[str, int] | None = None) -> CounterSnapshot:
    reference_path = f"/repos/{repo}/git/ref/heads/{COUNTER_BRANCH}"
    reference = counter_request("GET", reference_path, allowed=(404,))
    if not reference.ok:
        if restore_from is not None:
            candidate = counter_request("GET", counter_endpoint(repo) + f"?ref={restore_from}")
            decoded_counter(repo, candidate.body)
            counter_request("POST", f"/repos/{repo}/git/refs",
                            {"ref": f"refs/heads/{COUNTER_BRANCH}", "sha": restore_from})
        elif create:
            initialize_counter(repo, minimum, assignments)
        else:
            raise ValueError(f"Shared counter missing or not visible. If shared state ever existed, restore {COUNTER_BRANCH} "
                             "with --restore-counter-from using a bound machine's verified counter_commit; do not bootstrap again. "
                             "Only a Director who has never had shared state may bootstrap with all prior counters and --confirm-prior-counters-complete.")
        reference = counter_request("GET", reference_path)
    elif restore_from is not None:
        raise ValueError("Shared counter still exists; restoration never replaces an existing branch")
    commit = reference.body["object"]["sha"]
    result = counter_request("GET", counter_endpoint(repo) + f"?ref={commit}")
    state = decoded_counter(repo, result.body)
    return CounterSnapshot(result.body["sha"], state, commit)


def write_counter(repo: str, sha: str, state: dict[str, Any]) -> str | None:
    content = base64.b64encode((json.dumps(state, sort_keys=True) + "\n").encode()).decode()
    result = counter_request("PUT", counter_endpoint(repo), {"message": "Advance Director question counter",
                             "branch": COUNTER_BRANCH, "sha": sha, "content": content}, allowed=(409,))
    if result.ok:
        return result.body["commit"]["sha"]
    message = str((result.body or {}).get("message", "")).casefold()
    if not ("does not match" in message or ("sha" in message and any(word in message for word in ("mismatch", "expected")))
            or re.search(r"is at [0-9a-f]{40} but expected [0-9a-f]{40}", message)):
        raise ValueError("Shared counter update rejected; inspect repository rules before retrying")
    return None


def reserve_question(minimum: int, key: str | None, repo: str | None, *, create: bool = False,
                     assignments: dict[str, int] | None = None, restore_from: str | None = None,
                     number: int | None = None, reservation: bool = False, receipt: str | None = None,
                     reservations: dict[str, int] | None = None) -> Allocation:
    if repo is None:
        return Allocation(minimum + int(key is not None))
    request_key = hashlib.sha256(key.encode()).hexdigest() if key is not None else None
    try:
        for _ in range(8):
            snapshot = read_counter(repo, minimum, create=create, restore_from=restore_from, assignments=assignments)
            create, restore_from = False, None
            state = snapshot.state
            for prior, imported_number in (assignments or {}).items():
                if prior in state["questions"] and state["questions"][prior] != imported_number:
                    raise ValueError("Prior counters disagree on a question ID; preserve both assignments for reconciliation")
                state["questions"][prior] = imported_number
            for prior, imported_number in (reservations or {}).items():
                if prior in state["questions"] and state["questions"][prior] != imported_number:
                    raise ValueError("Prior counters disagree on a reservation; preserve them for reconciliation")
                owners = {k for k, value in state["questions"].items()
                          if value == imported_number and k not in state.get("reservations", {})}
                if owners - {prior}:
                    continue
                state["questions"][prior] = imported_number
                state.setdefault("reservations", {})[prior] = imported_number
            if request_key in state["questions"]:
                if number is not None and state["questions"][request_key] != number:
                    raise ValueError("Source already owns a different question number")
                if reservation and request_key not in state.get("reservations", {}):
                    state.setdefault("reservations", {})[request_key] = state["questions"][request_key]
                    if commit := write_counter(repo, snapshot.file_sha, state):
                        return Allocation(state["questions"][request_key], commit)
                    continue
                return Allocation(state["questions"][request_key], snapshot.commit_sha)
            state["last_id"] = max(state["last_id"], minimum)
            if request_key is not None:
                if number is None:
                    state["last_id"] += 1
                    number_to_assign = state["last_id"]
                else:
                    require_number(state, number, request_key)
                    require_receipt(state, number, receipt)
                    number_to_assign = number
                    for prior in [k for k, value in state.get("reservations", {}).items() if value == number]:
                        state["reservations"].pop(prior)
                        state["questions"].pop(prior, None)
                state["questions"][request_key] = number_to_assign
                if reservation:
                    state.setdefault("reservations", {})[request_key] = number_to_assign
            if commit := write_counter(repo, snapshot.file_sha, state):
                return Allocation(state["questions"][request_key] if request_key is not None else state["last_id"], commit)
    except (KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        raise ValueError("Shared question allocation could not be confirmed; preserve its request and retry after reconciliation") from error
    raise ValueError("Shared counter changed concurrently; preserve the pending request and retry allocation")


def prior_assignments(states: list[dict[str, Any]], field: str = "questions") -> dict[str, int]:
    assignments: dict[str, int] = {}
    for state in states:
        validate_counter(state)
        for url, number in state.get(field, {}).items():
            key = hashlib.sha256(url.encode()).hexdigest()
            if ((key in assignments and assignments[key] != number)
                    or (key not in assignments and number in assignments.values())):
                raise ValueError("Prior counters disagree on a question ID; preserve both assignments for reconciliation")
            assignments[key] = number
    return assignments


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
           question_url: str | None = None, include_counters: list[dict[str, Any]] | None = None,
           confirm_prior_counters_complete: bool = False, restore_from: str | None = None,
           question_number: int | None = None) -> int:
    if question_number is not None and (not allocate or not question_url or type(question_number) is not int or question_number <= 0):
        raise ValueError("Binding requires a positive reserved number and its confirmed question URL")
    if restore_from is not None and not re.fullmatch(r"[0-9a-fA-F]{40}", restore_from):
        raise ValueError("Restore requires a verified full counter commit SHA")
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
        validate_counter(state)
        if not isinstance(state.get("seed_sources"), list):
            raise ValueError("Invalid local seed sources; preserve the counter before recovery")
        snapshots = [state, *(include_counters or [])]
        imported = prior_assignments(snapshots)
        state["last_id"] = max(snapshot["last_id"] for snapshot in snapshots)
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
                if question_number is not None and assignments[question_url] != question_number:
                    raise ValueError("Source already owns a different question number")
                return assignments[question_url]
        number = state["last_id"]
        queue = configured_queue()
        configured_repository = queue.split("#", 1)[0] if queue else None
        repository = state.get("counter_repository") if allocate else configured_repository
        if state.get("counter_repository") and state["counter_repository"] != configured_repository:
            raise ValueError("Restore the recorded counter repository in [questions].queue before allocating")
        if allocate and repository and (not state.get("counter_commit") or state.get("counter_seed_pending")):
            raise ValueError("Shared seed is pending; reconcile it before allocating a question")
        if restore_from is not None and (allocate or not repository or not confirm_prior_counters_complete):
            raise ValueError("Counter restoration requires seed, a configured queue and --confirm-prior-counters-complete after reconciling pending writes")
        if allocate:
            key = question_url or state.setdefault("pending_request", uuid.uuid4().hex)
            if question_number is not None and not repository:
                require_number(state, question_number, question_url)
            save_state(path, state)
            allocation = reserve_question(state["last_id"], key, repository, number=question_number,
                                          reservation=question_url is None, receipt=reservation_key(state, question_number))
            number = question_number if question_number is not None and not repository else allocation.number
            state["last_id"] = max(state["last_id"], number)
            if allocation.commit_sha:
                state["counter_commit"] = allocation.commit_sha
            if question_url is None:
                state.setdefault("reservations", {})[key] = number
                state.pop("pending_request", None)
            else:
                state["reservations"] = {k: value for k, value in state.get("reservations", {}).items() if value != number}
        elif repository:
            known_history = any(snapshot.get("counter_commit") for snapshot in snapshots)
            for snapshot in snapshots:
                state.setdefault("questions", {}).update(snapshot.get("questions", {}))
            state["counter_repository"] = repository
            state["counter_seed_pending"] = True
            save_state(path, state)
            allocation = reserve_question(state["last_id"], None, repository,
                create=confirm_prior_counters_complete and not known_history, assignments=imported, restore_from=restore_from,
                reservations=prior_assignments(snapshots, "reservations"))
            number = allocation.number
            state.update(last_id=max(state["last_id"], number), counter_repository=repository, counter_commit=allocation.commit_sha)
            state.pop("counter_seed_pending", None)
        if question_url is not None:
            state["questions"][question_url] = number
        save_state(path, state)
        return number


def main() -> int:
    parser = argparse.ArgumentParser(description="Allocate Director question IDs from a shared counter or a local single-machine counter")
    parser.add_argument("action", choices=("next", "seed"))
    parser.add_argument("--seed-from", action="append", default=[], metavar="OWNER/REPO#NUMBER",
                        help="Current question/decision discussions; read all of them on initial setup")
    parser.add_argument("--include-counter", action="append", type=Path, default=[], metavar="PATH",
                        help="Existing counter snapshots from every other prior machine; reads IDs only")
    parser.add_argument("--confirm-prior-counters-complete", action="store_true",
                        help="All prior allocators are stopped and these snapshots cover every retained counter")
    parser.add_argument("--restore-counter-from", metavar="COMMIT", help="Restore only a missing shared branch from verified counter history")
    args = parser.parse_args()
    try:
        if args.action == "seed" and not (args.seed_from or args.include_counter or args.restore_counter_from or counter_path().exists() or configured_queue()):
            raise ValueError("seed requires current numbering discussions or retained counter snapshots")
        if args.action != "seed" and (args.include_counter or args.confirm_prior_counters_complete or args.restore_counter_from):
            raise ValueError("Counter migration and restoration options apply only to seed")
        seed = github_seed(args.seed_from) if args.seed_from else (0, []) if args.action == "seed" else None
        snapshots = [json.loads(path.read_text()) for path in args.include_counter]
        number = update(counter_path(), seed=seed, allocate=args.action == "next", include_counters=snapshots,
                        confirm_prior_counters_complete=args.confirm_prior_counters_complete, restore_from=args.restore_counter_from)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Q{number}" if args.action == "next" else f"Seeded through Q{number}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
