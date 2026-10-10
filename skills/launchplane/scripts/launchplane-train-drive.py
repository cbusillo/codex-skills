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
- failed:      this PR or an unattributed train action was blocked, or a candidate failed; a block
               only because the batch candidate's checks are still running waits
- needs_owner: the PR was closed, stays ineligible, or needs a branch update
               this command may not make; another local driver is running,
               this PR repeatedly gets the same controller refusal, or another PR blocks the train
- error:       no response kept coming back, the controller kept refusing, or
               the wall-clock deadline passed, or local locking is unavailable

A controller lease held by another driver is a wait, not a failure; if it is
still held at the deadline the outcome is needs_owner.

Every controller call makes Launchplane re-read the whole train from GitHub
(roughly 15 requests per open PR in the repository, on the shared App quota),
and Launchplane already runs its own passes on a timer and on check
completions. So while the controller repeats the same action or holds its
lease, the pause doubles from --poll-seconds up to --max-wait-seconds.

When the landed repository is the skills catalog this command lives in, the
landed stop event carries `runtime_reconciliation`: the receipt from running
`reconcile-runtime-checkout.py` with the landing SHA, so the installed catalog
follows every train landing. Its result never changes the landed outcome.

Output is JSONL in `gh_pr_watch.py`'s shape: {"event": ..., "payload": ...}.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:
    fcntl = None

SCRIPT_DIR = Path(__file__).resolve().parent
WRITE_ACTION = SCRIPT_DIR / "launchplane-write-action.py"
GH_WITH_ENV_TOKEN = SCRIPT_DIR.parent.parent / "github" / "scripts" / "gh-with-env-token"
RECONCILER = SCRIPT_DIR.parent.parent / "github" / "scripts" / "reconcile-runtime-checkout.py"
sys.path.insert(0, str(GH_WITH_ENV_TOKEN.parent))
import github_identity
import github_read

EXIT_CODES = {"landed": 0, "failed": 1, "needs_owner": 2, "error": 3}
LEASE_HELD_CODE = "merge_train_controller_lease_held"
FAILING_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
TERMINAL_READ_FAILURES = {"invalid_credentials", "permission_denied", "actor_mismatch", "unconfigured_identity"}


@dataclass
class DriveIO:
    """Everything the loop reads or changes, so tests can replace it."""

    controller: Callable[[str, str, str], dict[str, Any] | None]
    pull_request: Callable[[str, int], dict[str, Any] | None]
    update_branch: Callable[[str, int], bool]
    failing_checks: Callable[[str, str], list[dict[str, str]] | None]
    # Numbers of ready-to-merge PRs merged at or after an epoch time.
    merged_since: Callable[[str, float], list[int]] = lambda _repository, _since: []
    quota_wait: Callable[[], float] = lambda: 0.0
    # Reconciles the installed catalog after a landing; None when the repository is not this catalog.
    reconcile: Callable[[str, str], dict[str, Any] | None] = lambda _repository, _sha: None
    finish_reads: Callable[[], None] = lambda: None
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep


@dataclass
class DriveSettings:
    repository: str
    number: int
    base_branch: str = "main"
    deadline: float = 0.0
    poll_seconds: float = 60.0
    max_wait_seconds: float = 900.0
    allow_branch_update: bool = False
    max_branch_updates: int = 3
    max_helper_failures: int = 5
    max_own_refusals: int = 3
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
    candidate_active: bool = False
    unchanged_passes: int = 0
    quota_waited: bool = False
    own_refusal_code: str = ""
    own_refusal_streak: int = 0


class DriverLockError(Exception):
    """Local lock setup failed before the controller was called."""


@contextmanager
def local_driver(settings: DriveSettings) -> Iterator[dict[str, Any] | None]:
    """Hold a host-local train lock across the entire CLI run, including read-back.

    Never unlink the file: waiters must keep referring to the same inode.
    The OS releases ownership on exit, even when the driver is killed.
    """
    if fcntl is None:
        raise DriverLockError("local train-driver locking requires POSIX flock")
    try:
        root = Path.home() / ".cache" / "codex-skills" / "train-drivers"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256(json.dumps([settings.repository.casefold(), settings.base_branch]).encode()).hexdigest()
        fd = os.open(root / f"{key}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise DriverLockError from error
    with os.fdopen(fd, "r+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                holder = json.load(handle)
            except (ValueError, OSError):
                holder = {}
            yield holder if isinstance(holder, dict) else {}
            return
        except OSError as error:
            raise DriverLockError from error
        try:
            handle.seek(0)
            handle.truncate()
            json.dump({"pid": os.getpid(), "pr": settings.number,
                       "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, handle)
            handle.flush()
        except OSError as error:
            raise DriverLockError from error
        yield None


def _pause(settings: DriveSettings, io: DriveIO, state: DriveState, *, minimum: float = 0.0) -> None:
    delay = github_read.poll_delay(
        min(settings.poll_seconds * 2 ** min(state.unchanged_passes, 10), max(settings.poll_seconds, settings.max_wait_seconds)),
        minimum,
        repository=settings.repository,
    )
    io.sleep(min(delay, max(0.0, settings.deadline - io.now())))


def drive(settings: DriveSettings, io: DriveIO, emit: Callable[[str, dict[str, Any]], None]) -> str:
    state = DriveState(batch={settings.number})
    emit = _with_runtime_reconciliation(settings, io, emit)
    try:
        return _drive(settings, io, state, emit)
    except (github_read.GitHubReadError, github_read.GitHubReadShapeError) as error:
        if isinstance(error, github_read.GitHubReadError):
            detail = {"read_failure_cause": error.result.failure.cause if error.result.failure else "read_failed",
                      "request_id": error.result.request_id}
        else:
            detail = {"read_failure_cause": "invalid_response"}
        if settings.number in state.landed:
            return _stop(settings, state, emit, "landed", companion_evidence="unavailable", **detail)
        return _stop(settings, state, emit, "error", reason="GitHub read unavailable", **detail)


def _with_runtime_reconciliation(
    settings: DriveSettings, io: DriveIO, emit: Callable[[str, dict[str, Any]], None]
) -> Callable[[str, dict[str, Any]], None]:
    def wrapped(event: str, payload: dict[str, Any]) -> None:
        if event == "stop" and payload.get("outcome") == "landed" and payload.get("landing_sha"):
            receipt = io.reconcile(settings.repository, payload["landing_sha"])
            if receipt is not None:
                payload["runtime_reconciliation"] = receipt
        emit(event, payload)

    return wrapped


def _drive(settings: DriveSettings, io: DriveIO, state: DriveState, emit: Callable[[str, dict[str, Any]], None]) -> str:
    started = io.now()
    pass_number = 0
    while True:
        if io.now() >= settings.deadline:
            io.finish_reads()
        # The controller observes an admitted candidate and reports its landing.
        # Do not separately poll every PR in that batch while checks run.
        outcome = None if state.candidate_active and io.now() < settings.deadline else _record_landings(settings, io, state, emit)
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
            state.own_refusal_streak = 0
            state.candidate_active = False
            state.helper_failures += 1
            state.lease_held = None
            emit("snapshot", _snapshot(settings, state, "helper_unavailable", response))
            if state.helper_failures >= settings.max_helper_failures:
                return _stop(settings, state, emit, "error", reason="merge-train helper kept failing")
            _pause(settings, io, state)
            continue
        if response.get("status") not in {"accepted", "ok"}:
            state.candidate_active = False
            snapshot = _snapshot(settings, state, "controller_refused", response)
            code = str(snapshot["error_code"] or "")
            own_refusal = code != LEASE_HELD_CODE and bool(code) and (
                (response.get("summary") or {}).get("pull_request_number") == settings.number
            )
            state.own_refusal_streak = (
                state.own_refusal_streak + 1 if own_refusal and code == state.own_refusal_code
                else 1 if own_refusal else 0
            )
            state.own_refusal_code = code if own_refusal else ""
            if state.own_refusal_streak >= settings.max_own_refusals:
                emit("snapshot", snapshot)
                landing = _record_landings(settings, io, state, emit)
                if landing == "landed":
                    _finish_landing(settings, io, state, emit, started)
                    return _stop(settings, state, emit, "landed")
                if landing == "needs_owner":
                    return _stop(settings, state, emit, "needs_owner", reason="pull request is closed")
                return _stop(settings, state, emit, "needs_owner", reason="repeated controller refusal for this pull request",
                             refusal_count=state.own_refusal_streak,
                             **{key: snapshot[key] for key in ("error_code", "http_status", "trace_id")})
            if snapshot["error_code"] == "github_request_failed" and not state.quota_waited:
                wait = io.quota_wait()
                if wait > 0:
                    state.quota_waited = True
                    snapshot["controller_action"] = "github_primary_limit_wait"
                    snapshot["retry_after_seconds"] = wait
                    emit("snapshot", snapshot)
                    _pause(settings, io, state, minimum=wait)
                    continue
            if snapshot["error_code"] == LEASE_HELD_CODE:
                # Another driver is running the controller; keep polling until its lease is released or expires.
                snapshot["controller_action"] = "controller_lease_held"
                state.lease_held = snapshot
                # The lease holder is already advancing the train; back off like an unchanged pass.
                state.unchanged_passes = min(state.unchanged_passes + 1, 10)
                emit("snapshot", snapshot)
                _pause(settings, io, state)
                continue
            state.helper_failures += 1
            state.lease_held = None
            emit("snapshot", snapshot)
            if state.helper_failures >= settings.max_helper_failures:
                return _stop(settings, state, emit, "error", reason="merge-train controller kept refusing",
                             **{key: snapshot[key] for key in ("status", "error_code", "http_status", "trace_id")})
            _pause(settings, io, state)
            continue
        state.helper_failures = 0
        state.own_refusal_streak = 0
        state.quota_waited = False
        state.lease_held = None
        result = response.get("result") or {}
        action = str(result.get("controller_action") or "")
        state.unchanged_passes = min(state.unchanged_passes + 1, 10) if action == state.last_action else 0
        candidate = result.get("candidate") or {}
        state.candidate_active = (
            action == "observe_candidate" and bool(candidate.get("candidate_sha"))
            and settings.number in (candidate.get("pull_request_numbers") or [])
        )
        _remember_batch(result, state)
        if action in {"execute_stack_collapse", "admit_collapsed_root"}:
            state.stack_seen = True
        if action != state.last_action:
            emit("snapshot", _snapshot(settings, state, action, response))
        state.last_action = action

        applied_block = result.get("block_result") or {}
        if action == "block" and applied_block.get("status") == "blocked":
            emit("pr_blocked", {"pull_request_number": applied_block.get("pull_request_number"),
                                "train_should_continue": applied_block.get("train_should_continue"),
                                "detail": (result.get("dry_run_result") or {}).get("next_action_detail") or applied_block.get("detail")})
        verdict = _judge(settings, io, state, result, action)
        if verdict is not None:
            outcome, detail = verdict
            # A block can race a landing that just happened; re-read before failing.
            if _record_landings(settings, io, state, emit) == "landed":
                _finish_landing(settings, io, state, emit, started)
                return _stop(settings, state, emit, "landed")
            return _stop(settings, state, emit, outcome, **detail)
        if action in {"batch_landed", "land_batch", "complete_landing"}:
            if _record_landings(settings, io, state, emit) == "landed":
                _finish_landing(settings, io, state, emit, started)
                return _stop(settings, state, emit, "landed")
        _pause(settings, io, state)


# Blocks Launchplane raises while a candidate's checks are still running; the deadline bounds the wait.
WAITING_BLOCK_CODES = frozenset({"batch_pull_request_checks_not_ready"})


def _judge(
    settings: DriveSettings, io: DriveIO, state: DriveState, result: dict[str, Any], action: str
) -> tuple[str, dict[str, Any]] | None:
    blocking_reason = result.get("blocking_reason")
    if action == "block" and isinstance(blocking_reason, dict) and blocking_reason.get("code") in WAITING_BLOCK_CODES:
        return None
    if action in {"block", "stack_unsupported"}:
        selected = ((result.get("dry_run_result") or {}).get("selected_pr") or {}).get("number")
        if action == "block" and isinstance(selected, int) and selected != settings.number:
            applied = result.get("block_result") or {}
            if (
                action == "block"
                and applied.get("status") == "blocked"
                and applied.get("pull_request_number") == selected
                and applied.get("train_should_continue") is True
            ):
                return None
            return "needs_owner", {
                "reason": "another pull request is blocking the train",
                "blocking_pull_request_number": selected,
                "tracked_pull_request_number": settings.number,
                "controller_action": action,
                "blocking_reason": blocking_reason,
                "block_detail": (result.get("dry_run_result") or {}).get("next_action_detail") or applied.get("detail"),
            }
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
    io.finish_reads()
    # A stack root lands before its children are resolved; let the controller
    # finish that batch, but never start driving unrelated work.
    if state.stack_seen:
        for finish_pass in range(settings.max_stack_finish_passes):
            if io.now() >= settings.deadline:
                break
            key = f"train-drive-{settings.repository.replace('/', '-')}-{settings.number}-finish-{finish_pass}-{int(io.now())}"
            response = io.controller(settings.repository, settings.base_branch, key)
            action = str(((response or {}).get("result") or {}).get("controller_action") or "")
            emit("snapshot", _snapshot(settings, state, action or "helper_unavailable", response))
            if action in {"batch_landed", "idle"}:
                break
            _pause(settings, io, state)
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


def live_io(helper_timeout: float, *, deadline_at: float | None = None, repository_context: str | None = None) -> DriveIO:
    reader = github_read.GitHubReader(
        gh_cmd=str(GH_WITH_ENV_TOKEN), expected_actor=github_identity.automation_login(),
        operation="github.train.drive", cache_enabled=True, cache_coalesce_seconds=0.0, deadline_at=deadline_at,
    )
    def controller(repository: str, base_branch: str, key: str) -> dict[str, Any] | None:
        remaining = max(0.0, deadline_at - time.time()) if deadline_at is not None else helper_timeout + 60
        if remaining <= 0:
            return {"status": "no_response", "failure": "deadline_reached"}
        command = [
            "uv", "run", str(WRITE_ACTION), "--timeout", str(min(helper_timeout, max(0.01, remaining - 2))),
            "merge-train-controller-run-once", "--repo", repository, "--base-branch", base_branch,
            "--mutate", "--idempotency-key", key,
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=min(helper_timeout + 60, remaining), check=False)
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
        try:
            payload = reader.get_json(f"/repos/{repository}/pulls/{number}", step="pull_request")
        except github_read.GitHubReadError as error:
            # Keep authentication and permission refusals terminal. A primary
            # throttle has already waited within the inherited deadline.
            if error.result.failure and error.result.failure.cause in TERMINAL_READ_FAILURES:
                raise
            return None
        if not isinstance(payload, dict):
            return None
        return {**payload, "head_repository": ((payload.get("head") or {}).get("repo") or {}).get("full_name")}

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
        try:
            payload = reader.paged_json(f"/repos/{repository}/commits/{sha}/check-runs",
                                        step_prefix="candidate_checks", collection_key="check_runs")
        except github_read.GitHubReadError as error:
            if error.result.failure and error.result.failure.cause in TERMINAL_READ_FAILURES:
                raise
            return None
        return [{"name": run.get("name", ""), "conclusion": run["conclusion"], "url": run.get("details_url", "")}
                for run in payload if run.get("conclusion") in FAILING_CONCLUSIONS]

    def merged_since(repository: str, since: float) -> list[int]:
        try:
            payload = reader.get_json(
                f"/repos/{repository}/pulls?state=closed&sort=updated&direction=desc&per_page=50", step="merged_since"
            )
        except github_read.GitHubReadError as error:
            if error.result.failure and error.result.failure.cause in TERMINAL_READ_FAILURES:
                raise
            return []
        if not isinstance(payload, list):
            return []
        numbers = []
        for item in payload:
            merged_at = str(item.get("merged_at") or "")
            try:
                merged = calendar.timegm(time.strptime(merged_at, "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                continue
            if merged >= since and "ready-to-merge" in [label.get("name") for label in item.get("labels") or []]:
                numbers.append(int(item["number"]))
        return numbers

    def quota_wait() -> float:
        # Launchplane currently projects upstream failures as github_request_failed.
        # /rate_limit is quota-free: verify exhaustion rather than treating every
        # 502 as a throttle or weakening the ordinary refusal budget.
        if not repository_context:
            return 0.0
        probe = github_read.GitHubReader(
            gh_cmd="env", gh_prefix_args=[f"GH_REPO={repository_context}", str(GH_WITH_ENV_TOKEN)],
            expected_actor=github_identity.automation_login(),
            operation="github.api.rate_limit", deadline_at=min(deadline_at or time.time() + 60, time.time() + 60),
        )
        try:
            payload = probe.get_json("/rate_limit", step="controller_quota")
        except github_read.GitHubReadError:
            return 0.0
        core = (payload.get("resources") or {}).get("core") if isinstance(payload, dict) else None
        if isinstance(core, dict) and core.get("remaining") == 0:
            reset = core.get("reset")
            if isinstance(reset, (int, float)):
                return max(0.0, reset + 3.0 - time.time())
        return 0.0

    def reconcile(repository: str, landing_sha: str) -> dict[str, Any] | None:
        origin = subprocess.run(
            ["git", "-C", str(RECONCILER.parent), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, check=False,
        ).stdout.strip().removesuffix(".git")
        if not origin.casefold().replace(":", "/").endswith("/" + repository.casefold()):
            return None
        receipt = _run_json(["uv", "run", str(RECONCILER), "--repo", repository, "--landing-sha", landing_sha], 300)
        if isinstance(receipt, dict):
            return receipt
        return {"status": "failed", "reason_code": "reconciler_unavailable", "landing_sha": landing_sha}

    def finish_reads() -> None:
        # Only final read-back gets a fixed, bounded grace window. Mutations
        # and reset waits retain the original drive deadline.
        if deadline_at is not None:
            reader.deadline_at = deadline_at + 15.0

    return DriveIO(
        controller=controller,
        pull_request=pull_request,
        update_branch=update_branch,
        failing_checks=failing_checks,
        merged_since=merged_since,
        quota_wait=quota_wait,
        reconcile=reconcile,
        finish_reads=finish_reads,
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="Repository in OWNER/REPO form.")
    parser.add_argument("--pr", type=int, required=True, help="The labeled pull request to drive.")
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--deadline-minutes", type=float, default=240.0)
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    parser.add_argument("--max-wait-seconds", type=float, default=900.0,
                        help="Longest pause between controller calls while nothing changes.")
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
        max_wait_seconds=args.max_wait_seconds,
        allow_branch_update=args.allow_branch_update,
    )
    try:
        with local_driver(settings) as holder:
            if holder is not None:
                outcome = _stop(settings, DriveState(batch={settings.number}), emit, "needs_owner",
                                reason="another local driver is running for this train", running_driver=holder, prs=[],
                                recommendation="Add ready-to-merge and leave the PR to the running driver. "
                                               "If it exits before this PR lands, rerun this driver. "
                                               "No Director decision is needed for local contention.")
            else:
                outcome = drive(settings, live_io(args.helper_timeout, deadline_at=settings.deadline,
                                                 repository_context=settings.repository), emit)
    except DriverLockError:
        outcome = _stop(settings, DriveState(batch={settings.number}), emit, "error",
                        reason="local train-driver lock unavailable; no controller call made")
    return EXIT_CODES[outcome]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
