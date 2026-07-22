#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Dispatch and babysit one GitHub Actions workflow run without lossy rediscovery."""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import re
import shutil
import sys
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import github_api as github_api_core


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
AUTOMATION_GH = os.environ.get("GITHUB_WORKFLOW_BABYSIT_AUTOMATION_GH") or str(
    SCRIPT_DIR / "gh-with-env-token"
)
ACTIVE_GH = os.environ.get("GITHUB_WORKFLOW_BABYSIT_ACTIVE_GH") or "gh"
EXPECTED_AUTOMATION_LOGIN = os.environ.get("GH_WITH_ENV_TOKEN_EXPECTED_LOGIN") or "shiny-code-bot"
DISPATCH_API_VERSION = "2026-03-10"
DEFAULT_TIMEOUT_SECONDS = 1800.0
MAX_TIMEOUT_SECONDS = 7200.0
DEFAULT_POLL_INTERVAL_SECONDS = 15.0
MAX_INPUT_FILE_BYTES = 64 * 1024
MAX_WORKFLOW_INPUTS = 25
MAX_APPROVAL_COMMENT_LENGTH = 500
NONTERMINAL_STATUSES = {"in_progress", "pending", "queued", "requested", "waiting"}
TERMINAL_FAILURE_STATUSES = {
    "action_required",
    "cancelled",
    "failure",
    "neutral",
    "skipped",
    "stale",
    "timed_out",
}


class WorkflowBabysitError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(github_api_core.redact_string(message))
        self.code = code


@dataclass(frozen=True)
class RunReference:
    run_id: int
    run_url: str


@dataclass(frozen=True)
class RunSnapshot:
    run_id: int
    run_url: str
    status: str
    conclusion: str | None
    event: str | None
    actor: str | None
    triggering_actor: str | None
    head_branch: str | None
    head_sha: str | None
    run_attempt: int


@dataclass(frozen=True)
class PendingEnvironment:
    environment_id: int
    name: str
    current_user_can_approve: bool
    wait_timer_minutes: int
    wait_timer_started_at: str | None
    reviewers: tuple[dict[str, str], ...]

    @property
    def requires_review(self) -> bool:
        return self.current_user_can_approve or bool(self.reviewers)


@dataclass(frozen=True)
class JobSnapshot:
    name: str
    status: str
    conclusion: str | None
    runner_name: str | None


class WorkflowClient(Protocol):
    repo: str
    automation_login: str | None
    reviewer_login: str | None

    def resolve_actors(self) -> tuple[str, str]: ...

    def dispatch(
        self,
        *,
        workflow: str,
        ref: str,
        inputs: Mapping[str, str | int | float | bool],
    ) -> RunReference: ...

    def get_run(self, run_id: int) -> RunSnapshot: ...

    def get_pending_environments(self, run_id: int) -> tuple[PendingEnvironment, ...]: ...

    def get_approved_environment_ids(self, run_id: int) -> frozenset[int]: ...

    def get_jobs(self, run_id: int) -> tuple[JobSnapshot, ...]: ...

    def approve_environments(
        self,
        run_id: int,
        environment_ids: Sequence[int],
        comment: str,
    ) -> None: ...

    def diagnostics(self) -> dict[str, Any]: ...


class GitHubWorkflowClient:
    def __init__(
        self,
        repo: str,
        *,
        automation_gh: str = AUTOMATION_GH,
        active_gh: str = ACTIVE_GH,
        expected_automation_login: str = EXPECTED_AUTOMATION_LOGIN,
        deadline_at: float | None = None,
    ) -> None:
        self.repo = normalize_repo(repo)
        self.automation_gh = automation_gh
        self.active_gh = active_gh
        self.expected_automation_login = expected_automation_login
        self.deadline_at = deadline_at
        self.automation_login: str | None = None
        self.reviewer_login: str | None = None
        self.last_run_reference: RunReference | None = None
        self._request_count = 0
        self._operation_counts: Counter[str] = Counter()
        self._retry_attempts = 0
        self._last_receipt: dict[str, Any] | None = None
        self._env_command = shutil.which("env") or "env"

    def resolve_actors(self) -> tuple[str, str]:
        automation_login = self._resolve_automation_login()
        reviewer_login = self._resolve_reviewer_login()
        return automation_login, reviewer_login

    def dispatch(
        self,
        *,
        workflow: str,
        ref: str,
        inputs: Mapping[str, str | int | float | bool],
    ) -> RunReference:
        automation_login = self._resolve_automation_login()
        workflow_segment = urllib.parse.quote(normalize_required(workflow, "workflow"), safe="")
        normalized_inputs = validate_inputs(inputs)
        result = self._call(
            "POST",
            f"/repos/{repo_path(self.repo)}/actions/workflows/{workflow_segment}/dispatches",
            body={"ref": normalize_required(ref, "ref"), "inputs": normalized_inputs},
            role="automation",
            operation="github.workflow.dispatch",
            api_version=DISPATCH_API_VERSION,
            actor=automation_login,
            expected_actor=automation_login,
        )
        body = require_mapping(result.body, "workflow dispatch response")
        run_id = positive_int(
            body.get("workflow_run_id"),
            "workflow dispatch response workflow_run_id",
        )
        api_run_url = required_string(body.get("run_url"), "workflow dispatch response run_url")
        run_url = required_string(body.get("html_url"), "workflow dispatch response html_url")
        validate_dispatch_run_url(api_run_url, self.repo, run_id, api_url=True)
        validate_dispatch_run_url(run_url, self.repo, run_id, api_url=False)
        reference = RunReference(run_id=run_id, run_url=run_url)
        self.last_run_reference = reference
        return reference

    def get_run(self, run_id: int) -> RunSnapshot:
        result = self._call(
            "GET",
            f"/repos/{repo_path(self.repo)}/actions/runs/{positive_int(run_id, 'run id')}",
            role="automation",
            operation="github.workflow.run.read",
            actor=self._resolve_automation_login(),
            expected_actor=self.automation_login,
        )
        return parse_run_snapshot(result.body, self.repo, expected_run_id=run_id)

    def get_pending_environments(self, run_id: int) -> tuple[PendingEnvironment, ...]:
        reviewer_login = self._verify_reviewer_login()
        result = self._call(
            "GET",
            f"/repos/{repo_path(self.repo)}/actions/runs/{positive_int(run_id, 'run id')}/pending_deployments",
            role="reviewer",
            operation="github.workflow.pending_deployments.read",
            actor=reviewer_login,
            expected_actor=reviewer_login,
        )
        return parse_pending_environments(result.body)

    def get_approved_environment_ids(self, run_id: int) -> frozenset[int]:
        reviewer_login = self._verify_reviewer_login()
        result = self._call(
            "GET",
            f"/repos/{repo_path(self.repo)}/actions/runs/{positive_int(run_id, 'run id')}/approvals",
            role="reviewer",
            operation="github.workflow.approvals.read",
            actor=reviewer_login,
            expected_actor=reviewer_login,
        )
        return parse_approved_environment_ids(result.body, reviewer_login)

    def get_jobs(self, run_id: int) -> tuple[JobSnapshot, ...]:
        result = self._call(
            "GET",
            (
                f"/repos/{repo_path(self.repo)}/actions/runs/{positive_int(run_id, 'run id')}"
                "/jobs?filter=latest&per_page=100"
            ),
            role="automation",
            operation="github.workflow.jobs.read",
            actor=self._resolve_automation_login(),
            expected_actor=self.automation_login,
        )
        return parse_jobs(result.body)

    def approve_environments(
        self,
        run_id: int,
        environment_ids: Sequence[int],
        comment: str,
    ) -> None:
        reviewer_login = self._verify_reviewer_login()
        normalized_ids = sorted({positive_int(value, "environment id") for value in environment_ids})
        if not normalized_ids:
            raise WorkflowBabysitError(
                "invalid_approval_request",
                "protected-environment approval requires at least one environment id",
            )
        self._call(
            "POST",
            f"/repos/{repo_path(self.repo)}/actions/runs/{positive_int(run_id, 'run id')}/pending_deployments",
            body={
                "environment_ids": normalized_ids,
                "state": "approved",
                "comment": normalize_approval_comment(comment),
            },
            role="reviewer",
            operation="github.workflow.pending_deployments.approve",
            actor=reviewer_login,
            expected_actor=reviewer_login,
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "transport": "rest_api",
            "request_count": self._request_count,
            "operations": dict(sorted(self._operation_counts.items())),
            "retry_attempts": self._retry_attempts,
            "last_request": self._last_receipt,
        }

    def _resolve_automation_login(self) -> str:
        if self.automation_login is not None:
            return self.automation_login
        result = self._call(
            "GET",
            "/user",
            role="automation",
            operation="github.workflow.actor.read",
            actor=self.expected_automation_login,
            expected_actor=self.expected_automation_login,
        )
        login = response_login(result.body, "automation GitHub identity")
        if login.casefold() != self.expected_automation_login.casefold():
            raise WorkflowBabysitError(
                "automation_actor_mismatch",
                (
                    f"configured automation actor '{login}' does not match expected actor "
                    f"'{self.expected_automation_login}'"
                ),
            )
        self.automation_login = login
        return login

    def _resolve_reviewer_login(self) -> str:
        if self.reviewer_login is not None:
            return self.reviewer_login
        result = self._call(
            "GET",
            "/user",
            role="reviewer",
            operation="github.workflow.actor.read",
        )
        self.reviewer_login = response_login(result.body, "active reviewer GitHub identity")
        return self.reviewer_login

    def _verify_reviewer_login(self) -> str:
        expected_login = self._resolve_reviewer_login()
        result = self._call(
            "GET",
            "/user",
            role="reviewer",
            operation="github.workflow.actor.read",
            actor=expected_login,
            expected_actor=expected_login,
        )
        actual_login = response_login(result.body, "active reviewer GitHub identity")
        if actual_login.casefold() != expected_login.casefold():
            raise WorkflowBabysitError(
                "reviewer_actor_changed",
                (
                    f"active reviewer identity changed from '{expected_login}' to '{actual_login}' "
                    "during workflow babysitting"
                ),
            )
        return expected_login

    def _call(
        self,
        method: str,
        path: str,
        *,
        role: str,
        operation: str,
        body: Any = None,
        api_version: str = github_api_core.DEFAULT_API_VERSION,
        actor: str | None = None,
        expected_actor: str | None = None,
    ) -> github_api_core.ApiResult:
        if role == "automation":
            gh_cmd = self.automation_gh
            gh_prefix_args = ["--require-automation-auth"]
        elif role == "reviewer":
            gh_cmd = self._env_command
            gh_prefix_args = [
                "-u",
                "GH_TOKEN",
                "-u",
                "GITHUB_TOKEN",
                "-u",
                "CODEX_GITHUB_TOKEN",
                self.active_gh,
            ]
        else:
            raise WorkflowBabysitError("invalid_auth_role", f"unsupported GitHub auth role: {role}")

        result = github_api_core.call_gh_with_retry(
            method,
            path,
            body,
            gh_cmd=gh_cmd,
            gh_prefix_args=gh_prefix_args,
            api_version=api_version,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            bucket="rest_core",
            deadline_at=self.deadline_at,
        )
        self._record_result(result, operation)
        if result.ok:
            return result
        failure = result.failure
        message = failure.message if failure else f"GitHub request failed with status {result.status}"
        raise WorkflowBabysitError(
            failure.cause if failure else "github_request_failed",
            message,
        )

    def _record_result(self, result: github_api_core.ApiResult, operation: str) -> None:
        self._request_count += 1
        self._operation_counts[operation] += 1
        if result.retry_summary is not None:
            self._retry_attempts += max(0, result.retry_summary.attempts - 1)
        self._last_receipt = {
            "operation": operation,
            "status": result.status,
            "request_id": result.request_id,
            "bucket": result.bucket,
            "ok": result.ok,
            "actor": result.actor,
            "error_code": result.failure.cause if result.failure else None,
            "write_outcome": result.failure.write_outcome if result.failure else None,
            "recommended_next_action": (
                result.retry_summary.recommended_next_action
                if result.retry_summary is not None
                else None
            ),
        }


class WorkflowBabysitter:
    def __init__(
        self,
        client: WorkflowClient,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.clock = clock
        self.sleep = sleep
        self.progress = progress or (lambda _message: None)

    def dispatch_and_watch(
        self,
        *,
        workflow: str,
        ref: str,
        inputs: Mapping[str, str | int | float | bool],
        authorized_environments: frozenset[str],
        approval_comment: str,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> dict[str, Any]:
        started_at = self.clock()
        deadline = started_at + timeout_seconds
        automation_login, reviewer_login = self.client.resolve_actors()
        if automation_login.casefold() == reviewer_login.casefold():
            return blocked_result(
                outcome="self_review_identity_conflict",
                message=(
                    "the configured automation dispatch actor and active reviewer are the same; "
                    "refusing to dispatch a protected workflow that cannot be independently reviewed"
                ),
                actors=actors_payload(automation_login, reviewer_login),
                authorized_environments=authorized_environments,
                elapsed_seconds=self.clock() - started_at,
            )
        reference = self.client.dispatch(workflow=workflow, ref=ref, inputs=inputs)
        self.progress(f"dispatched workflow run {reference.run_id}: {reference.run_url}")
        return self.watch(
            run_id=reference.run_id,
            run_url=reference.run_url,
            authorized_environments=authorized_environments,
            approval_comment=approval_comment,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            actors=(automation_login, reviewer_login),
            started_at=started_at,
            deadline=deadline,
        )

    def watch(
        self,
        *,
        run_id: int,
        run_url: str | None,
        authorized_environments: frozenset[str],
        approval_comment: str,
        timeout_seconds: float,
        poll_interval_seconds: float,
        actors: tuple[str, str] | None = None,
        started_at: float | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        automation_login, reviewer_login = actors or self.client.resolve_actors()
        started_at = self.clock() if started_at is None else started_at
        deadline = started_at + timeout_seconds if deadline is None else deadline
        if (
            authorized_environments
            and automation_login.casefold() == reviewer_login.casefold()
        ):
            return blocked_result(
                outcome="self_review_identity_conflict",
                message=(
                    "the configured automation actor and active reviewer are the same; "
                    "refusing protected-environment approval without independent review"
                ),
                actors=actors_payload(automation_login, reviewer_login),
                authorized_environments=authorized_environments,
                elapsed_seconds=self.clock() - started_at,
            )
        approvals: list[dict[str, Any]] = []
        submitted_environment_ids: set[int] = set()
        polls = 0
        last_diagnosis: dict[str, Any] | None = None
        previous_progress_key: str | None = None

        while True:
            polls += 1
            run = self.client.get_run(run_id)
            run_url = run.run_url or run_url
            if run.status == "completed" or run.status in TERMINAL_FAILURE_STATUSES:
                conclusion = run.conclusion or run.status
                success = conclusion == "success"
                return {
                    "ok": success,
                    "exit_code": 0 if success else 1,
                    "outcome": "completed_success" if success else "completed_failure",
                    "message": f"workflow run completed with conclusion '{conclusion}'",
                    "run": run_payload(run, fallback_url=run_url),
                    "actors": actors_payload(automation_login, reviewer_login),
                    "authorized_environments": sorted(authorized_environments),
                    "approvals": approvals,
                    "polls": polls,
                    "elapsed_seconds": rounded_elapsed(self.clock() - started_at),
                    "last_diagnosis": last_diagnosis,
                }
            if run.status not in NONTERMINAL_STATUSES:
                return blocked_result(
                    outcome="unsupported_run_status",
                    message=f"GitHub returned unsupported workflow run status '{run.status}'",
                    run=run_payload(run, fallback_url=run_url),
                    actors=actors_payload(automation_login, reviewer_login),
                    authorized_environments=authorized_environments,
                    approvals=approvals,
                    polls=polls,
                    elapsed_seconds=self.clock() - started_at,
                )

            approval_action: tuple[tuple[int, ...], tuple[str, ...]] | None = None
            if run.status == "waiting":
                pending = self.client.get_pending_environments(run_id)
                if pending:
                    if any(
                        item.requires_review
                        and item.environment_id not in submitted_environment_ids
                        for item in pending
                    ):
                        submitted_environment_ids.update(
                            self.client.get_approved_environment_ids(run_id)
                        )
                    decision = protected_environment_decision(
                        run=run,
                        pending=pending,
                        automation_login=automation_login,
                        reviewer_login=reviewer_login,
                        authorized_environments=authorized_environments,
                        submitted_environment_ids=submitted_environment_ids,
                    )
                    last_diagnosis = decision["diagnosis"]
                    if decision.get("blocked"):
                        return blocked_result(
                            outcome=str(decision["outcome"]),
                            message=str(decision["message"]),
                            run=run_payload(run, fallback_url=run_url),
                            actors=actors_payload(automation_login, reviewer_login),
                            authorized_environments=authorized_environments,
                            approvals=approvals,
                            polls=polls,
                            elapsed_seconds=self.clock() - started_at,
                            last_diagnosis=last_diagnosis,
                        )
                    approval_ids = tuple(decision.get("approval_ids") or ())
                    approval_names = tuple(decision.get("approval_names") or ())
                    if approval_ids:
                        approval_action = (approval_ids, approval_names)
                else:
                    jobs = self.client.get_jobs(run_id)
                    last_diagnosis = queue_diagnosis(run.status, jobs, no_pending_deployment=True)
            elif run.status in {"pending", "queued", "requested"}:
                jobs = self.client.get_jobs(run_id)
                last_diagnosis = queue_diagnosis(run.status, jobs, no_pending_deployment=False)
            else:
                last_diagnosis = {"category": "running", "run_status": run.status}

            progress_key = json.dumps(last_diagnosis, sort_keys=True)
            if progress_key != previous_progress_key:
                self.progress(render_diagnosis(last_diagnosis))
                previous_progress_key = progress_key

            if approval_action is not None:
                approval_ids, approval_names = approval_action
                self.client.approve_environments(run_id, approval_ids, approval_comment)
                approval_record = {
                    "environment_ids": list(approval_ids),
                    "environments": list(approval_names),
                    "reviewer": reviewer_login,
                }
                approvals.append(approval_record)
                submitted_environment_ids.update(approval_ids)
                last_diagnosis = {
                    "category": "protected_environment_approval_submitted",
                    **approval_record,
                }
                self.progress(
                    "approved protected environment review for " + ", ".join(approval_names)
                )
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return timeout_result(
                        run=run,
                        run_url=run_url,
                        actors=actors_payload(automation_login, reviewer_login),
                        authorized_environments=authorized_environments,
                        approvals=approvals,
                        polls=polls,
                        elapsed_seconds=self.clock() - started_at,
                        last_diagnosis=last_diagnosis,
                    )
                self.sleep(min(poll_interval_seconds, remaining))
                continue

            remaining = deadline - self.clock()
            if remaining <= 0:
                return timeout_result(
                    run=run,
                    run_url=run_url,
                    actors=actors_payload(automation_login, reviewer_login),
                    authorized_environments=authorized_environments,
                    approvals=approvals,
                    polls=polls,
                    elapsed_seconds=self.clock() - started_at,
                    last_diagnosis=last_diagnosis,
                )
            self.sleep(min(poll_interval_seconds, remaining))


def protected_environment_decision(
    *,
    run: RunSnapshot,
    pending: Sequence[PendingEnvironment],
    automation_login: str,
    reviewer_login: str,
    authorized_environments: frozenset[str],
    submitted_environment_ids: set[int],
) -> dict[str, Any]:
    summaries = [pending_environment_payload(item) for item in pending]
    review_pending = [
        item
        for item in pending
        if item.requires_review and item.environment_id not in submitted_environment_ids
    ]
    submitted_pending = [
        item for item in pending if item.environment_id in submitted_environment_ids
    ]
    diagnosis = {
        "category": "protected_environment_wait",
        "run_status": run.status,
        "environments": summaries,
        "submitted_environment_ids": [
            item.environment_id for item in submitted_pending
        ],
    }
    if review_pending and run.event != "workflow_dispatch":
        return {
            "blocked": True,
            "outcome": "protected_workflow_event_mismatch",
            "message": (
                "protected-environment approval is allowed only for workflow_dispatch runs; "
                f"GitHub reported event '{run.event or 'unknown'}'"
            ),
            "diagnosis": diagnosis,
        }
    if review_pending and run.run_attempt != 1:
        return {
            "blocked": True,
            "outcome": "protected_workflow_rerun_unsupported",
            "message": (
                "protected-environment approval is fail-closed for rerun attempts; "
                "redispatch the workflow to obtain a new exact run id"
            ),
            "diagnosis": diagnosis,
        }
    denied = [item for item in review_pending if not item.current_user_can_approve]
    if denied:
        triggering_actor = (run.triggering_actor or "").casefold()
        if triggering_actor and triggering_actor == reviewer_login.casefold():
            names = ", ".join(item.name for item in denied)
            return {
                "blocked": True,
                "outcome": "self_review_denied",
                "message": (
                    f"active reviewer '{reviewer_login}' triggered this run and cannot self-approve "
                    f"protected environment review for {names}; redispatch with the configured automation actor"
                ),
                "diagnosis": diagnosis,
            }
        names = ", ".join(item.name for item in denied)
        return {
            "blocked": True,
            "outcome": "reviewer_not_eligible",
            "message": (
                f"active reviewer '{reviewer_login}' is not eligible to approve protected environment "
                f"review for {names}"
            ),
            "diagnosis": diagnosis,
        }
    triggering_actor = run.triggering_actor
    if review_pending and (
        not triggering_actor
        or triggering_actor.casefold() != automation_login.casefold()
    ):
        return {
            "blocked": True,
            "outcome": "protected_workflow_dispatch_actor_mismatch",
            "message": (
                "protected-environment approval requires a run dispatched by the configured "
                f"automation actor '{automation_login}', but GitHub reported "
                f"'{triggering_actor or 'unknown'}'"
            ),
            "diagnosis": diagnosis,
        }

    unauthorized = [item for item in review_pending if item.name not in authorized_environments]
    if unauthorized:
        names = ", ".join(item.name for item in unauthorized)
        return {
            "blocked": True,
            "outcome": "protected_environment_approval_required",
            "message": (
                f"protected environment review is pending for {names}; rerun with an exact "
                "--approve-environment value only after operator authorization"
            ),
            "diagnosis": diagnosis,
        }

    approvable = [item for item in review_pending if item.current_user_can_approve]
    if approvable:
        return {
            "blocked": False,
            "diagnosis": diagnosis,
            "approval_ids": tuple(item.environment_id for item in approvable),
            "approval_names": tuple(item.name for item in approvable),
        }
    return {"blocked": False, "diagnosis": diagnosis}


def queue_diagnosis(
    run_status: str,
    jobs: Sequence[JobSnapshot],
    *,
    no_pending_deployment: bool,
) -> dict[str, Any]:
    queued_jobs = [job for job in jobs if job.status == "queued"]
    if queued_jobs:
        return {
            "category": "runner_queued",
            "run_status": run_status,
            "jobs": [job_payload(job) for job in queued_jobs],
            "pending_deployments": "none" if no_pending_deployment else "not_queried",
        }
    if no_pending_deployment:
        return {
            "category": "waiting_without_pending_deployment",
            "run_status": run_status,
            "jobs": [job_payload(job) for job in jobs],
            "pending_deployments": "none",
            "possible_causes": ["concurrency", "workflow_queue", "deployment_protection_rule"],
        }
    return {
        "category": "workflow_or_concurrency_queue",
        "run_status": run_status,
        "jobs": [job_payload(job) for job in jobs],
    }


def parse_run_snapshot(body: Any, repo: str, *, expected_run_id: int) -> RunSnapshot:
    payload = require_mapping(body, "workflow run response")
    run_id = positive_int(payload.get("id"), "workflow run response id")
    if run_id != expected_run_id:
        raise WorkflowBabysitError(
            "invalid_run_response",
            f"GitHub returned run id {run_id}, expected {expected_run_id}",
        )
    validate_run_repository(payload, repo, "workflow run response")
    return RunSnapshot(
        run_id=run_id,
        run_url=required_string(payload.get("html_url"), "workflow run response html_url"),
        status=required_string(payload.get("status"), "workflow run response status"),
        conclusion=optional_string(payload.get("conclusion")),
        event=optional_string(payload.get("event")),
        actor=nested_login(payload.get("actor")),
        triggering_actor=nested_login(payload.get("triggering_actor")),
        head_branch=optional_string(payload.get("head_branch")),
        head_sha=optional_string(payload.get("head_sha")),
        run_attempt=positive_int(payload.get("run_attempt"), "workflow run attempt"),
    )


def parse_pending_environments(body: Any) -> tuple[PendingEnvironment, ...]:
    if not isinstance(body, list):
        raise WorkflowBabysitError(
            "invalid_pending_deployments_response",
            "GitHub pending deployments response must be a list",
        )
    pending: list[PendingEnvironment] = []
    for index, raw_item in enumerate(body):
        item = require_mapping(raw_item, f"pending deployment {index}")
        environment = require_mapping(item.get("environment"), f"pending deployment {index} environment")
        can_approve = item.get("current_user_can_approve")
        if not isinstance(can_approve, bool):
            raise WorkflowBabysitError(
                "invalid_pending_deployments_response",
                f"pending deployment {index} is missing current_user_can_approve",
            )
        wait_timer = item.get("wait_timer", 0)
        if isinstance(wait_timer, bool) or not isinstance(wait_timer, int) or wait_timer < 0:
            raise WorkflowBabysitError(
                "invalid_pending_deployments_response",
                f"pending deployment {index} has an invalid wait_timer",
            )
        raw_reviewers = item.get("reviewers") or []
        if not isinstance(raw_reviewers, list):
            raise WorkflowBabysitError(
                "invalid_pending_deployments_response",
                f"pending deployment {index} reviewers must be a list",
            )
        pending.append(
            PendingEnvironment(
                environment_id=positive_int(environment.get("id"), "environment id"),
                name=required_string(environment.get("name"), "environment name"),
                current_user_can_approve=can_approve,
                wait_timer_minutes=wait_timer,
                wait_timer_started_at=optional_string(item.get("wait_timer_started_at")),
                reviewers=tuple(parse_reviewer(value) for value in raw_reviewers),
            )
        )
    return tuple(pending)


def parse_jobs(body: Any) -> tuple[JobSnapshot, ...]:
    payload = require_mapping(body, "workflow jobs response")
    raw_jobs = payload.get("jobs")
    if not isinstance(raw_jobs, list):
        raise WorkflowBabysitError(
            "invalid_jobs_response",
            "GitHub workflow jobs response is missing jobs",
        )
    jobs: list[JobSnapshot] = []
    for index, raw_job in enumerate(raw_jobs):
        job = require_mapping(raw_job, f"workflow job {index}")
        jobs.append(
            JobSnapshot(
                name=required_string(job.get("name"), f"workflow job {index} name"),
                status=required_string(job.get("status"), f"workflow job {index} status"),
                conclusion=optional_string(job.get("conclusion")),
                runner_name=optional_string(job.get("runner_name")),
            )
        )
    return tuple(jobs)


def parse_approved_environment_ids(body: Any, reviewer_login: str) -> frozenset[int]:
    if not isinstance(body, list):
        raise WorkflowBabysitError(
            "invalid_approvals_response",
            "GitHub workflow approvals response must be a list",
        )
    environment_ids: set[int] = set()
    for index, raw_review in enumerate(body):
        review = require_mapping(raw_review, f"workflow approval {index}")
        if review.get("state") != "approved":
            continue
        user_login = nested_login(review.get("user"))
        if not user_login or user_login.casefold() != reviewer_login.casefold():
            continue
        environments = review.get("environments") or []
        if not isinstance(environments, list):
            raise WorkflowBabysitError(
                "invalid_approvals_response",
                f"workflow approval {index} environments must be a list",
            )
        for raw_environment in environments:
            environment = require_mapping(raw_environment, "workflow approval environment")
            environment_ids.add(positive_int(environment.get("id"), "environment id"))
    return frozenset(environment_ids)


def parse_reviewer(value: Any) -> dict[str, str]:
    payload = require_mapping(value, "pending deployment reviewer")
    reviewer = require_mapping(payload.get("reviewer"), "pending deployment reviewer identity")
    reviewer_type = optional_string(payload.get("type")) or "unknown"
    identity = (
        optional_string(reviewer.get("login"))
        or optional_string(reviewer.get("slug"))
        or optional_string(reviewer.get("name"))
        or "unknown"
    )
    return {"type": reviewer_type, "identity": identity}


def pending_environment_payload(value: PendingEnvironment) -> dict[str, Any]:
    return {
        "id": value.environment_id,
        "name": value.name,
        "current_user_can_approve": value.current_user_can_approve,
        "wait_timer_minutes": value.wait_timer_minutes,
        "wait_timer_started_at": value.wait_timer_started_at,
        "reviewers": list(value.reviewers),
    }


def job_payload(value: JobSnapshot) -> dict[str, Any]:
    return {
        "name": value.name,
        "status": value.status,
        "conclusion": value.conclusion,
        "runner_assigned": bool(value.runner_name),
    }


def run_payload(value: RunSnapshot, *, fallback_url: str | None = None) -> dict[str, Any]:
    return {
        "id": value.run_id,
        "url": value.run_url or fallback_url,
        "status": value.status,
        "conclusion": value.conclusion,
        "event": value.event,
        "actor": value.actor,
        "triggering_actor": value.triggering_actor,
        "head_branch": value.head_branch,
        "head_sha": value.head_sha,
        "run_attempt": value.run_attempt,
    }


def actors_payload(automation_login: str, reviewer_login: str) -> dict[str, str]:
    return {"dispatch": automation_login, "review": reviewer_login}


def blocked_result(
    *,
    outcome: str,
    message: str,
    actors: dict[str, str],
    authorized_environments: frozenset[str],
    run: dict[str, Any] | None = None,
    approvals: Sequence[dict[str, Any]] = (),
    polls: int = 0,
    elapsed_seconds: float = 0.0,
    last_diagnosis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "exit_code": 2,
        "outcome": outcome,
        "message": message,
        "run": run,
        "actors": actors,
        "authorized_environments": sorted(authorized_environments),
        "approvals": list(approvals),
        "polls": polls,
        "elapsed_seconds": rounded_elapsed(elapsed_seconds),
        "last_diagnosis": last_diagnosis,
    }


def timeout_result(
    *,
    run: RunSnapshot,
    run_url: str | None,
    actors: dict[str, str],
    authorized_environments: frozenset[str],
    approvals: Sequence[dict[str, Any]],
    polls: int,
    elapsed_seconds: float,
    last_diagnosis: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "exit_code": 124,
        "outcome": "bounded_timeout",
        "message": "workflow run did not reach a terminal state before the bounded timeout",
        "run": run_payload(run, fallback_url=run_url),
        "actors": actors,
        "authorized_environments": sorted(authorized_environments),
        "approvals": list(approvals),
        "polls": polls,
        "elapsed_seconds": rounded_elapsed(elapsed_seconds),
        "last_diagnosis": last_diagnosis,
    }


def render_diagnosis(diagnosis: Mapping[str, Any]) -> str:
    category = str(diagnosis.get("category") or "unknown")
    if category == "protected_environment_wait":
        names = [
            str(item.get("name"))
            for item in diagnosis.get("environments") or []
            if isinstance(item, Mapping) and item.get("name")
        ]
        return "protected environment wait: " + ", ".join(names)
    if category == "runner_queued":
        names = [
            str(item.get("name"))
            for item in diagnosis.get("jobs") or []
            if isinstance(item, Mapping) and item.get("name")
        ]
        return "runner queue wait: " + ", ".join(names)
    return category.replace("_", " ")


def normalize_repo(value: str) -> str:
    normalized = normalize_required(value, "repo")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", normalized):
        raise WorkflowBabysitError("invalid_repo", "repo must use OWNER/REPO form")
    return normalized


def repo_path(repo: str) -> str:
    owner, name = normalize_repo(repo).split("/", 1)
    return f"{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"


def normalize_required(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise WorkflowBabysitError("invalid_argument", f"{field} is required")
    return normalized


def normalize_approval_comment(value: str) -> str:
    normalized = normalize_required(value, "approval comment")
    if len(normalized) > MAX_APPROVAL_COMMENT_LENGTH:
        raise WorkflowBabysitError(
            "invalid_argument",
            f"approval comment must be at most {MAX_APPROVAL_COMMENT_LENGTH} characters",
        )
    return normalized


def positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise WorkflowBabysitError("invalid_response", f"{field} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise WorkflowBabysitError("invalid_response", f"{field} must be a positive integer") from exc
    if normalized <= 0:
        raise WorkflowBabysitError("invalid_response", f"{field} must be a positive integer")
    return normalized


def required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowBabysitError("invalid_response", f"{field} must be a non-empty string")
    return value.strip()


def optional_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowBabysitError("invalid_response", f"{field} must be an object")
    return value


def nested_login(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    return optional_string(value.get("login"))


def response_login(body: Any, field: str) -> str:
    payload = require_mapping(body, field)
    return required_string(payload.get("login"), f"{field} login")


def validate_run_repository(payload: Mapping[str, Any], repo: str, field: str) -> None:
    repository = require_mapping(payload.get("repository"), f"{field} repository")
    full_name = required_string(repository.get("full_name"), f"{field} repository full_name")
    if full_name.casefold() != repo.casefold():
        raise WorkflowBabysitError(
            "repository_mismatch",
            f"{field} belongs to '{full_name}', expected '{repo}'",
        )


def validate_dispatch_run_url(value: str, repo: str, run_id: int, *, api_url: bool) -> None:
    parsed = urllib.parse.urlsplit(value)
    expected_path = (
        f"/repos/{repo}/actions/runs/{run_id}"
        if api_url
        else f"/{repo}/actions/runs/{run_id}"
    )
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path.casefold() != expected_path.casefold()
    ):
        raise WorkflowBabysitError(
            "invalid_dispatch_response",
            "GitHub workflow dispatch response returned a run URL for an unexpected repository or run",
        )


def validate_inputs(
    inputs: Mapping[str, str | int | float | bool],
) -> dict[str, str | int | float | bool]:
    if len(inputs) > MAX_WORKFLOW_INPUTS:
        raise WorkflowBabysitError(
            "invalid_argument",
            f"workflow dispatch accepts at most {MAX_WORKFLOW_INPUTS} inputs",
        )
    normalized: dict[str, str | int | float | bool] = {}
    for key, value in inputs.items():
        normalized_key = normalize_required(key, "workflow input name")
        if normalized_key in normalized:
            raise WorkflowBabysitError(
                "invalid_argument",
                f"duplicate workflow input: {normalized_key}",
            )
        if not isinstance(value, bool | int | float | str):
            raise WorkflowBabysitError(
                "invalid_argument",
                f"workflow input '{normalized_key}' must be a string, number, or boolean",
            )
        normalized[normalized_key] = value
    return normalized


def parse_fields(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, field_value = value.partition("=")
        key = key.strip()
        if not separator or not key:
            raise WorkflowBabysitError("invalid_argument", "--field values must use KEY=VALUE form")
        if key in result:
            raise WorkflowBabysitError("invalid_argument", f"duplicate workflow input: {key}")
        result[key] = field_value
    validate_inputs(result)
    return result


def load_json_inputs(path: pathlib.Path) -> dict[str, str | int | float | bool]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise WorkflowBabysitError("input_file_error", f"unable to read workflow input file: {exc}") from exc
    if size > MAX_INPUT_FILE_BYTES:
        raise WorkflowBabysitError(
            "input_file_error",
            f"workflow input file exceeds {MAX_INPUT_FILE_BYTES} bytes",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowBabysitError("input_file_error", f"invalid workflow input JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowBabysitError("input_file_error", "workflow input JSON must be an object")
    normalized: dict[str, str | int | float | bool] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise WorkflowBabysitError("input_file_error", "workflow input names must be non-empty strings")
        if isinstance(item, bool | int | float | str):
            normalized[key] = item
            continue
        raise WorkflowBabysitError(
            "input_file_error",
            f"workflow input '{key}' must be a string, number, or boolean",
        )
    return validate_inputs(normalized)


def rounded_elapsed(value: float) -> float:
    return round(max(0.0, value), 3)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = github_api_core.TerminalArgumentParser(
        description=(
            "Dispatch or watch one GitHub Actions run, diagnose protected-environment waits, "
            "and stop on a bounded timeout."
        )
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=github_api_core.TerminalArgumentParser,
    )

    dispatch_parser = subparsers.add_parser(
        "dispatch",
        help="Dispatch with automation auth and babysit the exact returned run id.",
    )
    add_common_arguments(dispatch_parser)
    dispatch_parser.add_argument("--workflow", required=True, help="Workflow file, name, or id.")
    dispatch_parser.add_argument("--ref", required=True, help="Exact branch or tag containing the workflow.")
    dispatch_parser.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Workflow input. Repeat for multiple inputs; values are never printed.",
    )
    dispatch_parser.add_argument(
        "--json-input-file",
        type=pathlib.Path,
        help="JSON object of workflow inputs; mutually exclusive with --field.",
    )

    watch_parser = subparsers.add_parser(
        "watch",
        help="Babysit an exact existing workflow run id without rediscovery.",
    )
    add_common_arguments(watch_parser)
    watch_parser.add_argument("--run-id", required=True, type=int, help="Exact GitHub Actions run id.")
    return parser.parse_args(argv)


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True, help="Repository in OWNER/REPO form.")
    parser.add_argument(
        "--approve-environment",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Exact protected environment authorized for approval by the active human GitHub account. "
            "Repeat for multiple environments."
        ),
    )
    parser.add_argument(
        "--approval-comment",
        default="Approved by bounded GitHub workflow babysitting.",
        help="Bounded comment recorded with a protected-environment approval.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Overall timeout, greater than zero and at most {int(MAX_TIMEOUT_SECONDS)} seconds.",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="Polling interval, greater than zero and no greater than the overall timeout.",
    )


def validate_runtime_arguments(args: argparse.Namespace) -> tuple[frozenset[str], str]:
    normalize_repo(args.repo)
    if (
        not math.isfinite(args.timeout_seconds)
        or args.timeout_seconds <= 0
        or args.timeout_seconds > MAX_TIMEOUT_SECONDS
    ):
        raise WorkflowBabysitError(
            "invalid_argument",
            f"--timeout-seconds must be greater than zero and at most {int(MAX_TIMEOUT_SECONDS)}",
        )
    if (
        not math.isfinite(args.poll_interval_seconds)
        or args.poll_interval_seconds <= 0
        or args.poll_interval_seconds > args.timeout_seconds
    ):
        raise WorkflowBabysitError(
            "invalid_argument",
            "--poll-interval-seconds must be greater than zero and no greater than the timeout",
        )
    authorized = frozenset(
        normalize_required(value, "approve environment") for value in args.approve_environment
    )
    return authorized, normalize_approval_comment(args.approval_comment)


def emit_terminal(payload: Mapping[str, Any]) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True))
    return int(payload.get("exit_code") or 0)


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    try:
        args = parse_args(raw_args)
    except github_api_core.ArgumentParsingError as exc:
        return emit_terminal(
            {
                "schema_version": 1,
                "ok": False,
                "exit_code": 2,
                "operation": "github.workflow.babysit",
                "outcome": "invalid_arguments",
                "error": github_api_core.redact_string(str(exc)),
                "command": github_api_core.requested_subcommand(
                    raw_args,
                    {"dispatch", "watch"},
                ),
            }
        )
    client: GitHubWorkflowClient | None = None
    operation = "github.workflow.babysit"
    input_keys: list[str] = []
    try:
        authorized_environments, approval_comment = validate_runtime_arguments(args)
        absolute_deadline = time.time() + args.timeout_seconds
        client = GitHubWorkflowClient(args.repo, deadline_at=absolute_deadline)
        babysitter = WorkflowBabysitter(
            client,
            progress=lambda message: print(message, file=sys.stderr),
        )
        if args.command == "dispatch":
            operation = "github.workflow.dispatch_and_babysit"
            if args.field and args.json_input_file:
                raise WorkflowBabysitError(
                    "invalid_argument",
                    "--field and --json-input-file are mutually exclusive",
                )
            inputs: dict[str, str | int | float | bool]
            if args.json_input_file:
                inputs = load_json_inputs(args.json_input_file)
            else:
                inputs = parse_fields(args.field)
            input_keys = sorted(inputs)
            result = babysitter.dispatch_and_watch(
                workflow=args.workflow,
                ref=args.ref,
                inputs=inputs,
                authorized_environments=authorized_environments,
                approval_comment=approval_comment,
                timeout_seconds=args.timeout_seconds,
                poll_interval_seconds=args.poll_interval_seconds,
            )
            result.update(
                {
                    "schema_version": 1,
                    "operation": operation,
                    "repo": client.repo,
                    "workflow": args.workflow,
                    "ref": args.ref,
                    "input_keys": input_keys,
                    "diagnostics": client.diagnostics(),
                }
            )
            return emit_terminal(result)

        result = babysitter.watch(
            run_id=args.run_id,
            run_url=None,
            authorized_environments=authorized_environments,
            approval_comment=approval_comment,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
        result.update(
            {
                "schema_version": 1,
                "operation": operation,
                "repo": client.repo,
                "diagnostics": client.diagnostics(),
            }
        )
        return emit_terminal(result)
    except WorkflowBabysitError as exc:
        run_reference = client.last_run_reference if client is not None else None
        timed_out = exc.code in {"bounded_timeout", "deadline_exceeded"}
        payload = {
            "schema_version": 1,
            "ok": False,
            "exit_code": 124 if timed_out else 1,
            "operation": operation,
            "outcome": "bounded_timeout" if timed_out else exc.code,
            "error": str(exc),
            "repo": client.repo if client is not None else getattr(args, "repo", None),
            "run": (
                {"id": run_reference.run_id, "url": run_reference.run_url}
                if run_reference is not None
                else None
            ),
            "actors": (
                {
                    "dispatch": client.automation_login,
                    "review": client.reviewer_login,
                }
                if client is not None
                else None
            ),
            "input_keys": input_keys,
            "diagnostics": client.diagnostics() if client is not None else None,
        }
        return emit_terminal(payload)


if __name__ == "__main__":
    raise SystemExit(main())
