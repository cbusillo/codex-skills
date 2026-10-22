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
lease, the pause doubles from --poll-seconds up to --max-wait-seconds. After a
newly reported action Launchplane says to mutate once and call again (planning,
building or landing), the next call follows after --progress-seconds instead.
Every pause still respects the shared low-budget floor and the deadline.

The stop event's `cadence` counts controller requests, read-only observations
and GitHub reads and splits elapsed time into controller requests, sleep, reads,
recovery, terminal read-back and the unattributed remainder. `passes` has one
entry per controller pass (and a `recovery` entry when recovery ran); a pass
that paused names why.

When the landed repository is the skills catalog this command lives in, the
landed stop event carries `runtime_reconciliation`: the receipt from running
`reconcile-runtime-checkout.py` with the landing SHA, so the installed catalog
follows every train landing. Its result never changes the landed outcome.

Output is JSONL in `gh_pr_watch.py`'s shape: {"event": ..., "payload": ...}.
"""

from __future__ import annotations

import argparse
import calendar
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
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
PLAN_HELPER = SCRIPT_DIR.parent.parent / "github" / "scripts" / "gh-plan.py"
sys.path.insert(0, str(GH_WITH_ENV_TOKEN.parent))
import github_identity
import github_read

EXIT_CODES = {"landed": 0, "failed": 1, "needs_owner": 2, "error": 3}
LEASE_HELD_CODE = "merge_train_controller_lease_held"
FAILING_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
TERMINAL_READ_FAILURES = {"invalid_credentials", "permission_denied", "actor_mismatch", "unconfigured_identity"}
# Launchplane's policy says to mutate once and call again after these actions:
# the next controller pass has known work, so it need not wait a full poll.
CALL_AGAIN_ACTIONS = frozenset({"admit_collapsed_root", "plan_candidate", "build_candidate", "plan_landing", "land_batch"})


def _uncertain(response: object) -> bool:
    """Only a projected action or explicit service rejection settles an exchange."""
    if not isinstance(response, dict):
        return True
    summary = response.get("summary") or {}
    result = response.get("result")
    if not isinstance(summary, dict) or summary.get("projection_error_code"):
        return True
    if response.get("status") in {"accepted", "ok"}:
        return not (isinstance(result, dict) and isinstance(result.get("controller_action"), str)
                    and bool(result["controller_action"]))
    code = summary.get("error_code")
    return not (
        response.get("status") in {"denied", "unauthorized", "stale", "invalid_request", "invalid", "unavailable"}
        and isinstance(code, str) and bool(code)
        and isinstance(summary.get("http_status"), int)
        and (400 <= summary["http_status"] < 500 or code in {
            "github_request_failed", "github_token_not_configured", "database_storage_required"})
        and code not in {"client_timeout", "invalid_response", "provider_unavailable", "unknown_write"}
        and not summary.get("projection_error_code")
    )


def _accepted_unreadable(response: object) -> bool:
    """Launchplane completed the pass, but this helper could not project its reply.

    The saved request exists so an exchange with an unknown outcome is replayed
    instead of repeated. An accepted reply settles that question; only its
    meaning is unknown, which a replay of the same stored reply cannot change.
    """
    if not isinstance(response, dict):
        return False
    summary = response.get("summary")
    return (
        response.get("status") == "invalid"
        and isinstance(summary, dict)
        and summary.get("launchplane_status") == "accepted"
        and isinstance(summary.get("projection_error_code"), str)
        and bool(summary["projection_error_code"])
    )


@dataclass
class DriveIO:
    """Everything the loop reads or changes, so tests can replace it."""

    controller: Callable[[str, str, str], dict[str, Any] | None]
    pull_request: Callable[[str, int], dict[str, Any] | None]
    update_branch: Callable[[str, int], bool]
    candidate_checks: Callable[[str, str], list[dict[str, Any]] | None]
    # Bounded GitHub landing inventory, independent of mutable enqueue labels.
    merged_since: Callable[[str, float], dict[str, Any]] = lambda _repository, _since: {
        "numbers": [], "complete": False, "reason": "unavailable"}
    quota_wait: Callable[[], float] = lambda: 0.0
    # Reconciles the installed catalog after a landing; None when the repository is not this catalog.
    reconcile: Callable[[str, str], dict[str, Any] | None] | None = None
    reconcile_plans: Callable[[str, list[int]], dict[str, Any]] | None = None
    finish_reads: Callable[[], None] = lambda: None
    recover: Callable[[], dict[str, Any] | None] | None = None
    # Post-landing observation must never use the mutating controller.
    observe_controller: Callable[[str, str], dict[str, Any] | None] | None = None
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep


@dataclass
class DriveSettings:
    repository: str
    number: int
    base_branch: str = "main"
    deadline: float = 0.0
    poll_seconds: float = 60.0
    progress_seconds: float = 15.0
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
    dispositions: dict[int, str] = field(default_factory=dict)
    carried_children: dict[int, dict[str, Any]] = field(default_factory=dict)
    helper_failures: int = 0
    branch_updates: int = 0
    ineligible_streak: int = 0
    empty_candidate_failures: int = 0
    last_action: str = ""
    stack_plan: dict[str, Any] = field(default_factory=dict)
    stack_finish: str = ""
    lease_held: dict[str, Any] | None = None
    candidate_active: bool = False
    unchanged_passes: int = 0
    quota_waited: bool = False
    own_refusal_code: str = ""
    own_refusal_streak: int = 0
    controller_evidence: list[dict[str, Any]] = field(default_factory=list)
    recovery: dict[str, Any] | None = None
    companion_inventory: dict[str, Any] = field(default_factory=lambda: {"complete": False, "reason": "not_attempted"})
    target_merged_at: float | None = None
    cadence: Cadence | None = None


@dataclass
class Cadence:
    """Where a drive's time went.

    Categories never overlap: time inside the terminal read-back counts only as
    read-back, and time no wrapper measured stays unattributed instead of being
    called sleep. Request counts include read-back requests. A GitHub read is
    one helper call: a paged check-run read counts once. A recovery replay is
    one measured span, since it may read, observe and replay or make no request.
    """

    clock: Callable[[], float]
    started: float
    controller_requests: int = 0
    controller_seconds: float = 0.0
    observation_requests: int = 0
    github_reads: int = 0
    github_read_seconds: float = 0.0
    sleep_seconds: float = 0.0
    recovery_seconds: float = 0.0
    read_back_started: float | None = None
    read_back_seconds: float = 0.0
    passes: list[dict[str, Any]] = field(default_factory=list)

    def _measure(self, call: Callable[..., Any], *args: Any) -> tuple[Any, float]:
        before = self.clock()
        result = call(*args)
        return result, 0.0 if self.read_back_started is not None else self.clock() - before

    def wrap(self, io: DriveIO) -> DriveIO:
        def controller(*args: Any) -> dict[str, Any] | None:
            self.controller_requests += 1
            response, spent = self._measure(io.controller, *args)
            self.controller_seconds += spent
            return response

        def github_read(call: Callable[..., Any]) -> Callable[..., Any]:
            def read(*args: Any) -> Any:
                self.github_reads += 1
                payload, spent = self._measure(call, *args)
                self.github_read_seconds += spent
                return payload
            return read

        def observe_controller(*args: Any) -> dict[str, Any] | None:
            self.observation_requests += 1
            return io.observe_controller(*args) if io.observe_controller is not None else None

        def sleep(seconds: float) -> None:
            _, spent = self._measure(io.sleep, seconds)
            self.sleep_seconds += spent

        original_recover = io.recover

        def recover() -> dict[str, Any] | None:
            assert original_recover is not None
            response, spent = self._measure(original_recover)
            self.recovery_seconds += spent
            if response is not None:
                self.passes.append({"pass": 0, "controller_action": "recovery", "progress": False,
                                    "at_seconds": round(self.clock() - self.started, 3),
                                    "recovery_seconds": round(spent, 3)})
            return response

        return dataclasses.replace(
            io, controller=controller, sleep=sleep, pull_request=github_read(io.pull_request),
            candidate_checks=github_read(io.candidate_checks), merged_since=github_read(io.merged_since),
            quota_wait=github_read(io.quota_wait),
            observe_controller=observe_controller if io.observe_controller is not None else None,
            recover=recover if original_recover is not None else None,
        )

    def begin_read_back(self) -> None:
        if self.read_back_started is None:
            self.read_back_started = self.clock()

    def summary(self) -> dict[str, Any]:
        now = self.clock()
        read_back = self.read_back_seconds + (now - self.read_back_started if self.read_back_started is not None else 0.0)
        elapsed = now - self.started
        measured = (self.controller_seconds + self.github_read_seconds + self.sleep_seconds
                    + self.recovery_seconds + read_back)
        return {
            "elapsed_seconds": round(elapsed, 3),
            "controller_requests": self.controller_requests,
            "controller_seconds": round(self.controller_seconds, 3),
            "observation_requests": self.observation_requests,
            "github_reads": self.github_reads,
            "github_read_seconds": round(self.github_read_seconds, 3),
            "sleep_seconds": round(self.sleep_seconds, 3),
            "recovery_seconds": round(self.recovery_seconds, 3),
            "read_back_seconds": round(read_back, 3),
            "unattributed_seconds": round(max(0.0, elapsed - measured), 3),
            "passes": self.passes,
        }


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


def _pause(settings: DriveSettings, io: DriveIO, state: DriveState, *, minimum: float = 0.0, reason: str = "backoff") -> None:
    if reason == "progress":
        interval = min(settings.progress_seconds, settings.poll_seconds)
    else:
        interval = min(settings.poll_seconds * 2 ** min(state.unchanged_passes, 10),
                       max(settings.poll_seconds, settings.max_wait_seconds))
    delay = github_read.poll_delay(interval, minimum, repository=settings.repository)
    seconds = min(delay, max(0.0, settings.deadline - io.now()))
    cadence = state.cadence
    if cadence is not None and cadence.passes and cadence.read_back_started is None:
        cadence.passes[-1]["pause"] = {"reason": reason, "seconds": round(seconds, 3)}
    io.sleep(seconds)


def _record_pass(state: DriveState, io: DriveIO, pass_number: int, action: str, controller_before: float,
                 *, progress: bool = False) -> None:
    cadence = state.cadence
    if cadence is None:
        return
    cadence.passes.append({
        "pass": pass_number, "controller_action": action, "progress": progress,
        "at_seconds": round(io.now() - cadence.started, 3),
        "controller_seconds": round(cadence.controller_seconds - controller_before, 3),
    })


def drive(settings: DriveSettings, io: DriveIO, emit: Callable[[str, dict[str, Any]], None]) -> str:
    cadence = Cadence(clock=io.now, started=io.now())
    state = DriveState(batch={settings.number}, cadence=cadence)
    io = cadence.wrap(io)
    emit = _with_runtime_reconciliation(settings, io, emit)
    try:
        recovered = io.recover() if io.recover is not None else None
        if recovered is not None:
            if _uncertain(recovered):
                state.recovery = recovered.get("recovery") or {"reason": "controller recovery unavailable"}
                landing = _record_landings(settings, io, state, emit)
                return _stop(settings, state, emit, "landed" if landing == "landed" else "error")
            _remember_batch(recovered["result"], state)
            stack = recovered["result"].get("stack_collapse_plan") or {}
            members = {stack.get("root_pull_request_number"), *(child.get("pull_request_number")
                       for child in stack.get("child_dispositions") or ())}
            if stack.get("collapse_id") and settings.number in members:
                state.stack_plan = stack
            _emit_snapshot(state, emit, _snapshot(settings, state, "controller_recovered", recovered))
        return _drive(settings, io, state, emit)
    except (github_read.GitHubReadError, github_read.GitHubReadShapeError) as error:
        if isinstance(error, github_read.GitHubReadError):
            detail = {"read_failure_cause": error.result.failure.cause if error.result.failure else "read_failed",
                      "request_id": error.result.request_id}
        else:
            detail = {"read_failure_cause": "invalid_response"}
        if settings.number in state.landed:
            state.companion_inventory = {**state.companion_inventory, "complete": False, "reason": "read_unavailable"}
            return _stop(settings, state, emit, "landed", companion_evidence="unavailable", **detail)
        return _stop(settings, state, emit, "error", reason="GitHub read unavailable", **detail)


def _with_runtime_reconciliation(
    settings: DriveSettings, io: DriveIO, emit: Callable[[str, dict[str, Any]], None]
) -> Callable[[str, dict[str, Any]], None]:
    def wrapped(event: str, payload: dict[str, Any]) -> None:
        if event == "stop" and payload.get("outcome") == "landed" and payload.get("landing_sha"):
            receipt = io.reconcile(settings.repository, payload["landing_sha"]) if io.reconcile is not None else None
            if receipt is not None:
                payload["runtime_reconciliation"] = receipt
            landed = [item["number"] for item in payload.get("prs", []) if item.get("outcome") == "landed"]
            planning = io.reconcile_plans(settings.repository, landed) if io.reconcile_plans is not None else None
            if planning is not None:
                payload["planning_reconciliation"] = planning
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
        key = f"train-drive-{settings.repository.replace('/', '-')}-{settings.number}-{pass_number}-{uuid.uuid4().hex}"
        controller_before = state.cadence.controller_seconds if state.cadence is not None else 0.0
        response = io.controller(settings.repository, settings.base_branch, key)
        if _uncertain(response):
            unreadable = _accepted_unreadable(response)
            action = "controller_reply_unreadable" if unreadable else "controller_outcome_unknown"
            _record_pass(state, io, pass_number, action, controller_before)
            state.candidate_active = False
            state.recovery = {"reason": "controller mutation outcome is uncertain; reconcile before retry",
                              "request": {"repository": settings.repository, "base_branch": settings.base_branch,
                                          "mutate": True},
                              "idempotency_key_sha256": hashlib.sha256(key.encode()).hexdigest(),
                              "recommendation": "Read the PR and controller state, then use --recover-controller to replay the saved request; do not start a fresh pass."}
            if isinstance(response, dict):
                state.recovery.update(response.get("recovery") or {})
            _emit_snapshot(state, emit, _snapshot(settings, state, action, None))
            # A PR may have landed after the reply was lost, but an open PR
            # cannot establish the controller's candidate/admission outcome.
            landing = _record_landings(settings, io, state, emit)
            if landing == "landed":
                _finish_landing(settings, io, state, emit, started)
                return _stop(settings, state, emit, "landed")
            return _stop(settings, state, emit, "error")
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
            _record_pass(state, io, pass_number,
                         "controller_lease_held" if code == LEASE_HELD_CODE else "controller_refused", controller_before)
            if state.own_refusal_streak >= settings.max_own_refusals:
                _emit_snapshot(state, emit, snapshot)
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
                    _emit_snapshot(state, emit, snapshot)
                    _pause(settings, io, state, minimum=wait, reason="quota_reset")
                    continue
            if snapshot["error_code"] == LEASE_HELD_CODE:
                # Another driver is running the controller; keep polling until its lease is released or expires.
                snapshot["controller_action"] = "controller_lease_held"
                state.lease_held = snapshot
                # The lease holder is already advancing the train; back off like an unchanged pass.
                state.unchanged_passes = min(state.unchanged_passes + 1, 10)
                _emit_snapshot(state, emit, snapshot)
                _pause(settings, io, state, reason="lease_held")
                continue
            state.helper_failures += 1
            state.lease_held = None
            _emit_snapshot(state, emit, snapshot)
            if state.helper_failures >= settings.max_helper_failures:
                return _stop(settings, state, emit, "error", reason="merge-train controller kept refusing",
                             **{key: snapshot[key] for key in ("status", "error_code", "http_status", "trace_id")})
            _pause(settings, io, state, reason="refusal")
            continue
        state.helper_failures = 0
        state.own_refusal_streak = 0
        state.quota_waited = False
        state.lease_held = None
        result = response.get("result") or {}
        action = str(result.get("controller_action") or "")
        state.unchanged_passes = min(state.unchanged_passes + 1, 10) if action == state.last_action else 0
        _record_pass(state, io, pass_number, action, controller_before, progress=state.unchanged_passes == 0)
        candidate = result.get("candidate") or {}
        state.candidate_active = (
            action == "observe_candidate" and bool(candidate.get("candidate_sha"))
            and settings.number in (candidate.get("pull_request_numbers") or [])
        )
        _remember_batch(result, state)
        stack = result.get("stack_collapse_plan") or {}
        members = {stack.get("root_pull_request_number"), *(child.get("pull_request_number")
                   for child in stack.get("child_dispositions") or ())}
        if stack.get("collapse_id") and settings.number in members:
            state.stack_plan = stack
        _emit_snapshot(state, emit, _snapshot(settings, state, action, response), deduplicate=True)
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
        # A repeated call-again action is not known progress; it backs off.
        known_work = action in CALL_AGAIN_ACTIONS and state.unchanged_passes == 0
        _pause(settings, io, state, reason="progress" if known_work else "backoff")


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
        checks = (io.candidate_checks(settings.repository, candidate_sha) if candidate_sha else None) or []
        failing = [run for run in checks if run.get("conclusion") in FAILING_CONCLUSIONS]
        if failing:
            return "failed", {"reason": "candidate_failed", "candidate_sha": candidate_sha, "failing_checks": failing}
        if _any_check_running(checks):
            return None
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


def _any_check_running(checks: list[dict[str, Any]]) -> bool:
    return any(run.get("status") != "completed" for run in checks)


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
    for number in state.batch:
        if number not in state.landed and state.dispositions.get(number) not in {"closed", "superseded"}:
            state.dispositions[number] = "unknown"
    for number in sorted(state.batch):
        if number in state.landed:
            continue
        pull_request = io.pull_request(settings.repository, number)
        if pull_request is None:
            continue
        if pull_request.get("merged"):
            state.landed[number] = str(pull_request.get("merge_commit_sha") or "")
            if number == settings.number:
                state.target_merged_at = _github_timestamp(pull_request.get("merged_at"))
            emit("pr_landed", {"repository": settings.repository, "number": number, "merge_commit_sha": state.landed[number]})
        elif pull_request.get("state") == "closed" and pull_request.get("merged") is False:
            carried = state.carried_children.get(number)
            state.dispositions[number] = (
                "superseded" if carried and (pull_request.get("head") or {}).get("sha") == carried["expected_head_sha"]
                else "closed"
            )
            if number == settings.number:
                target_state = "closed"
        else:
            state.dispositions[number] = "open" if pull_request.get("state") == "open" else "unknown"
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
    if state.cadence is not None:
        state.cadence.begin_read_back()
    io.finish_reads()
    # The service's own controller completes child disposition. A generic
    # mutating pass can select unrelated work even after a matching dry run.
    # Observe only: there is no stack-bound mutation in this helper contract.
    if state.stack_plan and state.recovery is None:
        state.stack_finish = "pass_limit"
        for finish_pass in range(settings.max_stack_finish_passes):
            _record_landings(settings, io, state, emit)
            children = state.stack_plan.get("child_dispositions") or []
            if all(child.get("status") in {"preserved", "blocked", "stale"}
                   or child.get("pull_request_number") in state.landed
                   or state.dispositions.get(child.get("pull_request_number")) in {"closed", "superseded"}
                   for child in children):
                state.stack_finish = "complete"
                break
            if io.now() >= settings.deadline:
                state.stack_finish = "deadline"
                break
            if io.observe_controller is None:
                state.stack_finish = "observation_unavailable"
                break
            response = io.observe_controller(settings.repository, settings.base_branch)
            result = (response or {}).get("result") or {}
            stack = result.get("stack_collapse_plan") or {}
            if (response or {}).get("status") not in {"accepted", "ok"}:
                state.stack_finish = "observation_unavailable"
                break
            if (stack.get("collapse_id"), stack.get("root_pull_request_number")) != (
                state.stack_plan.get("collapse_id"), state.stack_plan.get("root_pull_request_number")
            ):
                state.stack_finish = "selection_changed"
                break
            state.stack_plan = stack
            # A matching stack is evidence; the dry run's whole queue is not
            # part of this post-landing scope.
            _remember_batch({"stack_collapse_plan": stack}, state)
            action = str(result.get("controller_action") or "")
            _emit_snapshot(state, emit, _snapshot(settings, state, action or "helper_unavailable", response))
            if stack.get("status") == "ready_for_train":
                state.stack_finish = "complete"
                break
            if finish_pass + 1 < settings.max_stack_finish_passes:
                _pause(settings, io, state)
    # Service membership is authoritative. Recover omitted batch members only
    # when GitHub independently ties them to the target's exact landing commit.
    state.companion_inventory = {"complete": False, "reason": "read_unavailable"}
    since = min(started, state.target_merged_at) if state.target_merged_at is not None else started
    inventory = io.merged_since(settings.repository, since)
    state.companion_inventory = {key: value for key, value in inventory.items() if key != "numbers"}
    landing_sha = state.landed.get(settings.number)
    if not landing_sha or state.target_merged_at is None:
        state.companion_inventory.update(complete=False, reason="landing_identity_unavailable")
    for number in inventory["numbers"]:
        if number in state.batch or not landing_sha:
            continue
        companion = io.pull_request(settings.repository, number)
        if companion is None:
            state.companion_inventory.update(complete=False, reason="read_unavailable")
            continue
        generated = str((companion or {}).get("head", {}).get("ref") or "").startswith(
            ("launchplane/train/", "launchplane/construct/"))
        if companion and not generated and companion.get("merged") and companion.get("merge_commit_sha") == landing_sha:
            state.batch.add(number)
    _record_landings(settings, io, state, emit)


def _remember_batch(result: dict[str, Any], state: DriveState) -> None:
    for key in ("candidate", "landing_plan"):
        for number in (result.get(key) or {}).get("pull_request_numbers") or ():
            if isinstance(number, int) and not isinstance(number, bool) and number > 0:
                state.batch.add(number)
    dry_run = result.get("dry_run_result") or {}
    for number in dry_run.get("queue_order") or ():
        if isinstance(number, int):
            state.batch.add(number)
    stack = result.get("stack_collapse_plan") or {}
    for child in stack.get("child_dispositions") or ():
        number = child.get("pull_request_number")
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            state.batch.add(number)
            if (child.get("status") == "closed" and child.get("expected_head_sha")
                and stack.get("collapse_id") and stack.get("root_pull_request_number")
                and stack["root_pull_request_number"] != number):
                state.carried_children[number] = {
                    "collapse_id": stack.get("collapse_id"),
                    "root_pull_request_number": stack.get("root_pull_request_number"),
                    "expected_head_sha": child.get("expected_head_sha"),
                }


def _github_timestamp(value: object) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(str(value or ""), "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def _queue_entry(result: dict[str, Any], number: int) -> dict[str, Any] | None:
    for entry in (result.get("dry_run_result") or {}).get("queue") or ():
        if isinstance(entry, dict) and entry.get("number") == number:
            return entry
    return None


def _snapshot(settings: DriveSettings, state: DriveState, action: str, response: dict[str, Any] | None) -> dict[str, Any]:
    response = response or {}
    summary = response.get("summary") or {}
    result = response.get("result") or {}
    diagnosis = result.get("last_controller_result")
    warnings = response.get("warnings") or [{}]
    first_warning = warnings[0] if isinstance(warnings[0], dict) else {}
    return {
        "pr": {"repo": settings.repository, "number": settings.number},
        "controller_action": action,
        "reason_code": (diagnosis.get("reason_code") if isinstance(diagnosis, dict) else result.get("reason_code")),
        "last_controller_result": diagnosis if isinstance(diagnosis, dict) else None,
        "status": response.get("status"),
        # Rejections carry error_code in the summary; local helper failures carry it as a warning code.
        "error_code": summary.get("error_code") or first_warning.get("code"),
        "http_status": summary.get("http_status"),
        "exit_code": response.get("exit_code"),
        "failure": response.get("failure"),
        "trace_id": summary.get("trace_id"),
        # live_io consumes the write-action helper's validated public projection,
        # never the provider response. Keep that projection's single schema owner.
        "records": response.get("records") or {},
        "result": result,
        "batch": sorted(state.batch),
        "landed": sorted(state.landed),
        "actions": ["wait_for_train"],
    }


def _emit_snapshot(
    state: DriveState, emit: Callable[[str, dict[str, Any]], None], snapshot: dict[str, Any],
    *, deduplicate: bool = False,
) -> None:
    # A new request trace alone is not progress. Evidence changes within the
    # same phase still need a snapshot, without changing polling backoff.
    if deduplicate and state.controller_evidence:
        previous = state.controller_evidence[-1]
        if {key: value for key, value in snapshot.items() if key != "trace_id"} == {
            key: value for key, value in previous.items() if key != "trace_id"
        }:
            return
    state.controller_evidence.append(snapshot)
    emit("snapshot", snapshot)


def _stop(settings: DriveSettings, state: DriveState, emit: Callable[[str, dict[str, Any]], None], outcome: str, **detail: Any) -> str:
    prs = [
        {"number": number, "outcome": "landed" if number in state.landed else state.dispositions.get(number, "unknown"),
         "merge_commit_sha": state.landed.get(number, ""),
         **({"carried_by": state.carried_children[number]} if state.dispositions.get(number) == "superseded" else {})}
        for number in sorted(state.batch)
    ]
    emit(
        "stop",
        {
            "pr": {"repo": settings.repository, "number": settings.number},
            "outcome": outcome,
            "landing_sha": state.landed.get(settings.number, ""),
            "prs": prs,
            "controller_evidence": state.controller_evidence,
            **({"companion_inventory": state.companion_inventory} if state.companion_inventory else {}),
            "actions": [f"stop_{outcome}"],
            **({"controller_recovery": state.recovery} if state.recovery is not None else {}),
            **({"stack_finish": state.stack_finish} if state.stack_finish else {}),
            **({"cadence": state.cadence.summary()} if state.cadence is not None else {}),
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


def live_io(helper_timeout: float, *, deadline_at: float | None = None, repository_context: str | None = None,
            recover_controller: bool = False, tracked_pr: int | None = None,
            base_branch_context: str = "main") -> DriveIO:
    reader = github_read.GitHubReader(
        gh_cmd=str(GH_WITH_ENV_TOKEN), expected_actor=github_identity.automation_login(),
        operation="github.train.drive", cache_enabled=True, cache_coalesce_seconds=0.0, deadline_at=deadline_at,
    )
    def controller(repository: str, base_branch: str, key: str) -> dict[str, Any] | None:
        remaining = max(0.0, deadline_at - time.time()) if deadline_at is not None else helper_timeout + 60
        if remaining <= 0:
            return {"status": "no_response", "failure": "deadline_reached"}
        root = Path.home() / ".cache" / "codex-skills" / "train-drivers"
        name = hashlib.sha256(json.dumps([repository.casefold(), base_branch]).encode()).hexdigest()
        journal = root / f"{name}.request.json"
        recovering = journal.exists()
        try:
            if recovering:
                saved = json.loads(journal.read_text())
                if (saved["repository"], saved["base_branch"], saved["mutate"]) != (repository, base_branch, True):
                    raise ValueError("request scope mismatch")
                key = saved["idempotency_key"]
                if not isinstance(key, str) or not key:
                    raise ValueError("missing request key")
            else:
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w") as handle:
                    json.dump({"schema_version": 1, "repository": repository, "base_branch": base_branch, "mutate": True,
                               "idempotency_key": key, "pull_request_number": tracked_pr}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
        except (OSError, ValueError, KeyError, TypeError):
            return {"status": "outcome_unknown", "recovery": {"reason": "saved controller request unavailable; no mutation made"}}
        recovery = {"request": {"repository": repository, "base_branch": base_branch, "mutate": True},
                    "idempotency_key_sha256": hashlib.sha256(key.encode()).hexdigest(),
                    "request_file": str(journal),
                    "reason": "controller mutation outcome requires reconciliation",
                    "recommendation": "Rerun the same driver command with --recover-controller; retain the saved request until an accepted replay."}
        command = [
            "uv", "run", str(WRITE_ACTION), "--timeout", str(min(helper_timeout, max(0.01, remaining - 2))),
            "merge-train-controller-run-once", "--repo", repository, "--base-branch", base_branch,
            "--mutate", "--idempotency-key", key,
        ]
        if recovering:
            if not recover_controller:
                return {"status": "outcome_unknown", "recovery": recovery}
            number = saved.get("pull_request_number")
            if not isinstance(number, int) or pull_request(repository, number) is None:
                return {"status": "outcome_unknown", "recovery": recovery}
            # Read-only controller evidence precedes exact-key replay. It never
            # proves the prior request completed, so it cannot release its key.
            observed = _run_json(command[:-3], min(helper_timeout + 60, remaining))
            if _uncertain(observed) or observed.get("status") not in {"accepted", "ok"}:
                return {"status": "outcome_unknown", "recovery": recovery}
            remaining = max(0.0, deadline_at - time.time()) if deadline_at is not None else helper_timeout + 60
            if remaining <= 0:
                return {"status": "outcome_unknown", "recovery": recovery}
            command[4] = str(min(helper_timeout, max(0.01, remaining - 2)))
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=min(helper_timeout + 60, remaining), check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"status": "no_response", "failure": type(error).__name__, "recovery": recovery}
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            if _accepted_unreadable(payload):
                try:
                    journal.unlink()
                except OSError:
                    return {"status": "outcome_unknown", "recovery": recovery}
                summary = payload["summary"]
                released: dict[str, Any] = {
                    "reason": "Launchplane accepted the controller pass, but this helper could not read its reply",
                    "request": recovery["request"],
                    "idempotency_key_sha256": recovery["idempotency_key_sha256"],
                    "saved_request": "released",
                    "recommendation": "The pass happened; read the PR and controller state for its effect. "
                                      "Compare the pinned reply schema with Launchplane's before relying on it; the next driver run starts a fresh pass.",
                }
                for field_name in ("error_code", "trace_id", "projection_error_code"):
                    value = summary.get(field_name)
                    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
                        released[field_name] = value
                payload["recovery"] = released
                if recovering:
                    payload["status"] = "outcome_unknown"
            elif not _uncertain(payload) and (not recovering or payload.get("status") in {"accepted", "ok"}):
                try:
                    journal.unlink()
                except OSError:
                    return {"status": "outcome_unknown", "recovery": recovery}
            else:
                summary = payload.get("summary")
                if isinstance(summary, dict):
                    for field_name in ("error_code", "trace_id", "projection_error_code"):
                        value = summary.get(field_name)
                        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
                            recovery[field_name] = value
                payload["recovery"] = recovery
                if recovering:
                    payload["status"] = "outcome_unknown"
            return payload
        # Stderr can echo local configuration, so only the exit code leaves this process.
        return {"status": "no_response", "exit_code": completed.returncode, "recovery": recovery}

    def observe_controller(repository: str, base_branch: str) -> dict[str, Any] | None:
        remaining = max(0.0, deadline_at - time.time()) if deadline_at is not None else helper_timeout + 60
        if remaining <= 0:
            return {"status": "no_response", "failure": "deadline_reached"}
        return _run_json([
            "uv", "run", str(WRITE_ACTION), "--timeout", str(min(helper_timeout, max(0.01, remaining - 2))),
            "merge-train-controller-run-once", "--repo", repository, "--base-branch", base_branch,
        ], min(helper_timeout + 60, remaining))

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

    def candidate_checks(repository: str, sha: str) -> list[dict[str, Any]] | None:
        try:
            payload = reader.paged_json(f"/repos/{repository}/commits/{sha}/check-runs",
                                        step_prefix="candidate_checks", collection_key="check_runs")
        except github_read.GitHubReadError as error:
            if error.result.failure and error.result.failure.cause in TERMINAL_READ_FAILURES:
                raise
            return None
        return [{"name": run.get("name", ""), "status": run.get("status"), "conclusion": run.get("conclusion"),
                 "url": run.get("details_url", "")} for run in payload]

    def merged_since(repository: str, since: float) -> dict[str, Any]:
        since = int(since)  # GitHub timestamps have one-second precision.
        numbers = []
        per_page, max_pages, observed = 100, 5, 0
        for page in range(1, max_pages + 1):
            try:
                payload = reader.get_json(
                    f"/repos/{repository}/pulls?state=closed&sort=updated&direction=desc&per_page={per_page}&page={page}",
                    step=f"merged_since_page_{page}",
                )
            except github_read.GitHubReadError as error:
                if error.result.failure and error.result.failure.cause in TERMINAL_READ_FAILURES:
                    raise
                return {"numbers": numbers, "complete": False, "reason": "read_unavailable"}
            if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
                raise github_read.GitHubReadShapeError("GitHub merged_since page is not a pull request list")
            for item in payload:
                observed += 1
                updated = _github_timestamp(item.get("updated_at"))
                if updated is not None and updated < since:
                    return {"numbers": numbers, "complete": True, "observed_count": observed}
                merged = _github_timestamp(item.get("merged_at"))
                if merged is not None and merged >= since:
                    numbers.append(int(item["number"]))
            if len(payload) < per_page:
                return {"numbers": numbers, "complete": True, "observed_count": observed}
        return {"numbers": numbers, "complete": False, "reason": "inventory_limit",
                "observed_count": observed, "limit": per_page * max_pages}

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

    def reconcile_plans(repository: str, numbers: list[int]) -> dict[str, Any]:
        receipts = []
        for number in numbers:
            receipt = _run_json(["uv", "run", str(PLAN_HELPER), "--repo", repository,
                                 "reconcile-landing", "--pr", str(number), "--apply"], 300)
            receipts.append({"pr": number, "receipt": receipt if isinstance(receipt, dict) else
                             {"ok": False, "reason": "planning_reconciliation_unavailable"}})
        return {"ok": all(item["receipt"].get("ok") is True for item in receipts), "prs": receipts}

    def recover() -> dict[str, Any] | None:
        if repository_context:
            # Recovery runs even when the tracked PR has already landed.
            name = hashlib.sha256(json.dumps([repository_context.casefold(), base_branch_context]).encode()).hexdigest()
            journal = Path.home() / ".cache" / "codex-skills" / "train-drivers" / f"{name}.request.json"
            if journal.exists():
                return controller(repository_context, base_branch_context, "")
        return None

    return DriveIO(
        controller=controller,
        observe_controller=observe_controller,
        pull_request=pull_request,
        update_branch=update_branch,
        candidate_checks=candidate_checks,
        merged_since=merged_since,
        quota_wait=quota_wait,
        reconcile=reconcile,
        reconcile_plans=reconcile_plans,
        finish_reads=finish_reads,
        recover=recover,
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="Repository in OWNER/REPO form.")
    parser.add_argument("--pr", type=int, required=True, help="The labeled pull request to drive.")
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--deadline-minutes", type=float, default=240.0)
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    parser.add_argument("--progress-seconds", type=float, default=15.0,
                        help="Pause after a newly reported plan, build or landing action (capped by --poll-seconds).")
    parser.add_argument("--max-wait-seconds", type=float, default=900.0,
                        help="Longest pause between controller calls while nothing changes.")
    parser.add_argument("--helper-timeout", type=float, default=180.0)
    parser.add_argument("--recover-controller", action="store_true",
                        help="Read controller state and replay a saved uncertain request with its original key.")
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
        progress_seconds=args.progress_seconds,
        max_wait_seconds=args.max_wait_seconds,
        allow_branch_update=args.allow_branch_update,
    )
    try:
        with local_driver(settings) as holder:
            if holder is not None:
                outcome = _stop(settings, DriveState(batch={settings.number}), emit, "needs_owner",
                                reason="another local driver is running for this train", running_driver=holder, prs=[],
                                recommendation="Use the enqueue label from active Launchplane policy and leave the PR to the running driver. "
                                               "If it exits before this PR lands, rerun this driver. "
                                               "No Director decision is needed for local contention.")
            else:
                outcome = drive(settings, live_io(args.helper_timeout, deadline_at=settings.deadline,
                                                 repository_context=settings.repository,
                                                 recover_controller=args.recover_controller, tracked_pr=args.pr,
                                                 base_branch_context=settings.base_branch), emit)
    except DriverLockError:
        outcome = _stop(settings, DriveState(batch={settings.number}), emit, "error",
                        reason="local train-driver lock unavailable; no controller call made")
    return EXIT_CODES[outcome]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
