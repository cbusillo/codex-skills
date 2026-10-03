#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Drive one labeled PR through its Launchplane merge train to a clear outcome.

Each pass calls `launchplane-write-action.py merge-train-controller-run-once
--mutate` once, then pauses. The command exits only on an outcome:

- landed:      the PR merged (by this train run or another one); every other
               PR seen in the batch is reported too
- failed:      the controller blocked, or a train candidate failed its checks; a block
               only because the batch candidate's checks are still running waits
- needs_owner: the PR was closed, stays ineligible, or needs a branch update
               this command may not make
- error:       no response kept coming back, the controller kept refusing, or
               the wall-clock deadline passed

A controller lease held by another driver is a wait, not a failure; if it is
still held at the deadline the outcome is needs_owner.

Output is JSONL in `gh_pr_watch.py`'s shape: {"event": ..., "payload": ...}.
"""

from __future__ import annotations

import argparse
import calendar
import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
WRITE_ACTION = SCRIPT_DIR / "launchplane-write-action.py"
GH_WITH_ENV_TOKEN = SCRIPT_DIR.parent.parent / "github" / "scripts" / "gh-with-env-token"

EXIT_CODES = {"landed": 0, "failed": 1, "needs_owner": 2, "error": 3}
LEASE_HELD_CODE = "merge_train_controller_lease_held"
FAILING_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}


@dataclass
class DriveIO:
    """Everything the loop reads or changes, so tests can replace it."""

    controller: Callable[[str, str, str], dict[str, Any] | None]
    pull_request: Callable[[str, int], dict[str, Any] | None]
    update_branch: Callable[[str, int], bool]
    failing_checks: Callable[[str, str], list[dict[str, str]] | None]
    # Numbers of ready-to-merge PRs merged at or after an epoch time.
    merged_since: Callable[[str, float], list[int]] = lambda _repository, _since: []
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep


@dataclass
class DriveSettings:
    repository: str
    number: int
    base_branch: str = "main"
    deadline: float = 0.0
    poll_seconds: float = 60.0
    allow_branch_update: bool = False
    max_branch_updates: int = 3
    max_helper_failures: int = 5
    ineligible_passes: int = 2
    empty_candidate_failures: int = 5
    max_stack_finish_passes: int = 5


@dataclass
class DriveState:
    batch: set[int] = field(default_factory=set)
    landed: dict[int, str] = field(default_factory=dict)
    helper_failures: int = 0
    branch_updates: int = 0
    ineligible_streak: int = 0
    empty_candidate_failures: int = 0
    last_action: str = ""
    stack_seen: bool = False
    lease_held: dict[str, Any] | None = None


def drive(settings: DriveSettings, io: DriveIO, emit: Callable[[str, dict[str, Any]], None]) -> str:
    state = DriveState(batch={settings.number})
    started = io.now()
    pass_number = 0
    while True:
        outcome = _record_landings(settings, io, state, emit)
        if outcome == "landed":
            _finish_landing(settings, io, state, emit, started)
            return _stop(settings, state, emit, "landed")
        if outcome is not None:
            return _stop(settings, state, emit, outcome)
        if io.now() >= settings.deadline:
            if state.lease_held is not None:
                return _stop(settings, state, emit, "needs_owner", reason="another driver held the merge-train controller lease",
                             trace_id=state.lease_held.get("trace_id"))
            return _stop(settings, state, emit, "error", reason="deadline reached", last_action=state.last_action)

        pass_number += 1
        key = f"train-drive-{settings.repository.replace('/', '-')}-{settings.number}-{pass_number}-{int(io.now())}"
        response = io.controller(settings.repository, settings.base_branch, key)
        if response is None or response.get("status") == "no_response":
            state.helper_failures += 1
            state.lease_held = None
            emit("snapshot", _snapshot(settings, state, "helper_unavailable", response))
            if state.helper_failures >= settings.max_helper_failures:
                return _stop(settings, state, emit, "error", reason="merge-train helper kept failing")
            io.sleep(settings.poll_seconds)
            continue
        if response.get("status") not in {"accepted", "ok"}:
            snapshot = _snapshot(settings, state, "controller_refused", response)
            if snapshot["error_code"] == LEASE_HELD_CODE:
                # Another driver is running the controller; keep polling until its lease is released or expires.
                snapshot["controller_action"] = "controller_lease_held"
                state.lease_held = snapshot
                emit("snapshot", snapshot)
                io.sleep(settings.poll_seconds)
                continue
            state.helper_failures += 1
            state.lease_held = None
            emit("snapshot", snapshot)
            if state.helper_failures >= settings.max_helper_failures:
                return _stop(settings, state, emit, "error", reason="merge-train controller kept refusing",
                             **{key: snapshot[key] for key in ("status", "error_code", "http_status", "trace_id")})
            io.sleep(settings.poll_seconds)
            continue
        state.helper_failures = 0
        state.lease_held = None
        result = response.get("result") or {}
        action = str(result.get("controller_action") or "")
        _remember_batch(result, state)
        if action in {"execute_stack_collapse", "admit_collapsed_root"}:
            state.stack_seen = True
        if action != state.last_action:
            emit("snapshot", _snapshot(settings, state, action, response))
        state.last_action = action

        verdict = _judge(settings, io, state, result, action)
        if verdict is not None:
            outcome, detail = verdict
            # A block can race a landing that just happened; re-read before failing.
            if _record_landings(settings, io, state, emit) == "landed":
                _finish_landing(settings, io, state, emit, started)
                return _stop(settings, state, emit, "landed")
            return _stop(settings, state, emit, outcome, **detail)
        io.sleep(settings.poll_seconds)


# Blocks Launchplane raises while a candidate's checks are still running; the deadline bounds the wait.
WAITING_BLOCK_CODES = frozenset({"batch_pull_request_checks_not_ready"})


def _judge(
    settings: DriveSettings, io: DriveIO, state: DriveState, result: dict[str, Any], action: str
) -> tuple[str, dict[str, Any]] | None:
    blocking_reason = result.get("blocking_reason")
    if action == "block" and isinstance(blocking_reason, dict) and blocking_reason.get("code") in WAITING_BLOCK_CODES:
        return None
    if action in {"block", "stack_unsupported"}:
        return "failed", {"reason": action, "blocking_reason": blocking_reason}
    if action == "candidate_failed":
        candidate = result.get("candidate") or {}
        candidate_sha = str(candidate.get("candidate_sha") or result.get("candidate_sha") or "")
        failing = io.failing_checks(settings.repository, candidate_sha) if candidate_sha else None
        if failing:
            return "failed", {"reason": "candidate_failed", "candidate_sha": candidate_sha, "failing_checks": failing}
        # A candidate made stale by a queue change is discarded and rebuilt.
        state.empty_candidate_failures += 1
        if state.empty_candidate_failures >= settings.empty_candidate_failures:
            return "failed", {"reason": "candidate_failed without a failing check", "candidate_sha": candidate_sha}
        return None
    intended_action = (result.get("dry_run_result") or {}).get("intended_next_action")
    if action == "update_branch" or intended_action == "update_branch":
        if (result.get("branch_update_result") or {}).get("status") == "updated":
            # The controller already refreshed a queued branch, possibly another PR's; the deadline bounds repeats.
            return None
        return _update_branch(settings, io, state, result)
    entry = _queue_entry(result, settings.number)
    reasons = list(entry.get("ineligible_reasons") or []) if entry else []
    if reasons:
        state.ineligible_streak += 1
        if state.ineligible_streak >= settings.ineligible_passes:
            return "needs_owner", {"reason": "pull request is not eligible", "ineligible_reasons": reasons}
    else:
        state.ineligible_streak = 0
    return None


def _update_branch(
    settings: DriveSettings, io: DriveIO, state: DriveState, result: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    # The controller reported a behind-base PR without refreshing it.
    selected = ((result.get("dry_run_result") or {}).get("selected_pr") or {}).get("number")
    if selected is not None and selected != settings.number:
        return "needs_owner", {"reason": f"pull request #{selected} is ahead in the queue and behind its base"}
    if not settings.allow_branch_update:
        return "needs_owner", {"reason": "branch is behind its base; rerun with --allow-branch-update to refresh it"}
    pull_request = io.pull_request(settings.repository, settings.number) or {}
    head_repository = str(pull_request.get("head_repository") or "").casefold()
    if head_repository != settings.repository.casefold():
        return "needs_owner", {"reason": "branch is behind its base and lives in another repository"}
    if state.branch_updates >= settings.max_branch_updates:
        return "needs_owner", {"reason": "branch kept falling behind its base"}
    if not io.update_branch(settings.repository, settings.number):
        return "needs_owner", {"reason": "branch update was refused"}
    state.branch_updates += 1
    return None


def _record_landings(
    settings: DriveSettings, io: DriveIO, state: DriveState, emit: Callable[[str, dict[str, Any]], None]
) -> str | None:
    target_state = None
    for number in sorted(state.batch):
        if number in state.landed:
            continue
        pull_request = io.pull_request(settings.repository, number)
        if pull_request is None:
            continue
        if pull_request.get("merged"):
            state.landed[number] = str(pull_request.get("merge_commit_sha") or "")
            emit("pr_landed", {"repository": settings.repository, "number": number, "merge_commit_sha": state.landed[number]})
        elif number == settings.number and pull_request.get("state") == "closed":
            target_state = "closed"
    if settings.number in state.landed:
        return "landed"
    if target_state == "closed":
        return "needs_owner"
    return None


def _finish_landing(
    settings: DriveSettings,
    io: DriveIO,
    state: DriveState,
    emit: Callable[[str, dict[str, Any]], None],
    started: float,
) -> None:
    # A stack root lands before its children are resolved; let the controller
    # finish that batch, but never start driving unrelated work.
    if state.stack_seen:
        for finish_pass in range(settings.max_stack_finish_passes):
            key = f"train-drive-{settings.repository.replace('/', '-')}-{settings.number}-finish-{finish_pass}-{int(io.now())}"
            response = io.controller(settings.repository, settings.base_branch, key)
            action = str(((response or {}).get("result") or {}).get("controller_action") or "")
            emit("snapshot", _snapshot(settings, state, action or "helper_unavailable", response))
            if action in {"batch_landed", "idle"}:
                break
            io.sleep(settings.poll_seconds)
    # Batch companions may be missing from projected evidence; report every
    # ready-to-merge PR that merged while this driver ran.
    for number in io.merged_since(settings.repository, started):
        state.batch.add(number)
    _record_landings(settings, io, state, emit)


def _remember_batch(result: dict[str, Any], state: DriveState) -> None:
    dry_run = result.get("dry_run_result") or {}
    for number in dry_run.get("queue_order") or ():
        if isinstance(number, int):
            state.batch.add(number)


def _queue_entry(result: dict[str, Any], number: int) -> dict[str, Any] | None:
    for entry in (result.get("dry_run_result") or {}).get("queue") or ():
        if isinstance(entry, dict) and entry.get("number") == number:
            return entry
    return None


def _snapshot(settings: DriveSettings, state: DriveState, action: str, response: dict[str, Any] | None) -> dict[str, Any]:
    response = response or {}
    summary = response.get("summary") or {}
    warnings = response.get("warnings") or [{}]
    first_warning = warnings[0] if isinstance(warnings[0], dict) else {}
    return {
        "pr": {"repo": settings.repository, "number": settings.number},
        "controller_action": action,
        "status": response.get("status"),
        # Rejections carry error_code in the summary; local helper failures carry it as a warning code.
        "error_code": summary.get("error_code") or first_warning.get("code"),
        "http_status": summary.get("http_status"),
        "exit_code": response.get("exit_code"),
        "failure": response.get("failure"),
        "trace_id": summary.get("trace_id"),
        "batch": sorted(state.batch),
        "landed": sorted(state.landed),
        "actions": ["wait_for_train"],
    }


def _stop(settings: DriveSettings, state: DriveState, emit: Callable[[str, dict[str, Any]], None], outcome: str, **detail: Any) -> str:
    prs = [
        {"number": number, "outcome": "landed" if number in state.landed else "open", "merge_commit_sha": state.landed.get(number, "")}
        for number in sorted(state.batch)
    ]
    emit(
        "stop",
        {
            "pr": {"repo": settings.repository, "number": settings.number},
            "outcome": outcome,
            "landing_sha": state.landed.get(settings.number, ""),
            "prs": prs,
            "actions": [f"stop_{outcome}"],
            **{key: value for key, value in detail.items() if value is not None},
        },
    )
    return outcome


def _run_json(command: list[str], timeout: float) -> Any | None:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None


def live_io(helper_timeout: float) -> DriveIO:
    def controller(repository: str, base_branch: str, key: str) -> dict[str, Any] | None:
        command = [
            "uv", "run", str(WRITE_ACTION), "--timeout", str(helper_timeout),
            "merge-train-controller-run-once", "--repo", repository, "--base-branch", base_branch,
            "--mutate", "--idempotency-key", key,
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=helper_timeout + 60, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"status": "no_response", "failure": type(error).__name__}
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return payload
        # Stderr can echo local configuration, so only the exit code leaves this process.
        return {"status": "no_response", "exit_code": completed.returncode}

    def pull_request(repository: str, number: int) -> dict[str, Any] | None:
        payload = _run_json(
            [
                str(GH_WITH_ENV_TOKEN), "api", f"repos/{repository}/pulls/{number}", "--jq",
                "{state, merged, merge_commit_sha, head_repository: .head.repo.full_name}",
            ],
            timeout=60,
        )
        return payload if isinstance(payload, dict) else None

    def update_branch(repository: str, number: int) -> bool:
        try:
            completed = subprocess.run(
                [str(GH_WITH_ENV_TOKEN), "pr", "update-branch", str(number), "-R", repository],
                capture_output=True, text=True, timeout=120, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    def failing_checks(repository: str, sha: str) -> list[dict[str, str]] | None:
        payload = _run_json(
            [
                str(GH_WITH_ENV_TOKEN), "api", f"repos/{repository}/commits/{sha}/check-runs?per_page=100",
                "--jq", "[.check_runs[] | {name, conclusion, url: .details_url}]",
            ],
            timeout=60,
        )
        if not isinstance(payload, list):
            return None
        return [run for run in payload if isinstance(run, dict) and run.get("conclusion") in FAILING_CONCLUSIONS]

    def merged_since(repository: str, since: float) -> list[int]:
        payload = _run_json(
            [
                str(GH_WITH_ENV_TOKEN), "api",
                f"repos/{repository}/pulls?state=closed&sort=updated&direction=desc&per_page=50",
                "--jq",
                "[.[] | select(.merged_at != null) | {number, merged_at, labels: [.labels[].name]}]",
            ],
            timeout=60,
        )
        if not isinstance(payload, list):
            return []
        numbers = []
        for item in payload:
            merged_at = str(item.get("merged_at") or "")
            try:
                merged = calendar.timegm(time.strptime(merged_at, "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                continue
            if merged >= since and "ready-to-merge" in (item.get("labels") or []):
                numbers.append(int(item["number"]))
        return numbers

    return DriveIO(
        controller=controller,
        pull_request=pull_request,
        update_branch=update_branch,
        failing_checks=failing_checks,
        merged_since=merged_since,
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="Repository in OWNER/REPO form.")
    parser.add_argument("--pr", type=int, required=True, help="The labeled pull request to drive.")
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--deadline-minutes", type=float, default=240.0)
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    parser.add_argument("--helper-timeout", type=float, default=180.0)
    parser.add_argument(
        "--allow-branch-update",
        action="store_true",
        help="Refresh a behind-base branch in the train repository (only your own PR branches).",
    )
    args = parser.parse_args(argv)

    def emit(event: str, payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps({"event": event, "payload": payload}, sort_keys=True) + "\n")
        sys.stdout.flush()

    settings = DriveSettings(
        repository=args.repo,
        number=args.pr,
        base_branch=args.base_branch,
        deadline=time.time() + args.deadline_minutes * 60,
        poll_seconds=args.poll_seconds,
        allow_branch_update=args.allow_branch_update,
    )
    outcome = drive(settings, live_io(args.helper_timeout), emit)
    return EXIT_CODES[outcome]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
