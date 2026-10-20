#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
#     "aiohttp==3.14.5",
# ]
# ///
"""Compact GitHub issue planning helper for Codex skills."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import urllib.parse
from dataclasses import replace
from typing import Any, NoReturn, Optional

import github_api as github_api_core
import github_read
import github_plan_graph
import github_plan_body
from github_plan_body import (
    PLAN_ORIGINAL_START as PLAN_ORIGINAL_START,
    PLAN_ORIGINAL_END as PLAN_ORIGINAL_END,
    PLAN_MANAGED_START,
    PLAN_MANAGED_END,
    PLAN_MANAGED_PROVENANCE_MARKER,
    PLAN_OWNERSHIP_MARKERS,
    PLAN_RESERVED_MARKERS,
    PLAN_MANAGED_NOTICE as PLAN_MANAGED_NOTICE,
)
import github_repository_scope as repository_scope
import github_close_attention
import github_comment as github_comment_core
import github_issue as github_issue_core
import github_milestone as github_milestone_core
import github_identity
import github_client
import github_direction_next
import github_next_queue
import github_next_reads
import github_plan_claim
import github_plan_closure
import github_plan_wait
import github_plan_trigger
import github_plan_release
import github_plan_landing
import github_plan_status
import github_agent
from github_direction_next import (
    normalize_labels,
    compact_list_issue,
    section_map,
    relationship_refs,
    next_milestone_context,
    next_plan_status,
    next_static_exclusion,
    evaluate_next_plan,
    rank_next_candidates,
)


SKILL_DIR = pathlib.Path(__file__).resolve().parents[1]
BOT_GH = SKILL_DIR.parent / "github/scripts/gh-with-env-token"
PEOPLE_RESOLVER = SKILL_DIR.parent / "people/scripts/resolve_person.py"
API_VERSION_ARGS = ["-H", "X-GitHub-Api-Version: 2022-11-28"]
EXPECTED_ACTOR = github_identity.automation_login()
CURRENT_OPERATION = "github.plan.unknown"
CURRENT_TRANSPORT = "helper"
CURRENT_BUCKET = "unknown"
CURRENT_IS_WRITE = False
CURRENT_RETRY_FIELDS: dict[str, Any] = {}
CURRENT_RETRY_SUMMARY: Optional[github_api_core.RetrySummary] = None
NEXT_READS: github_next_reads.NextReads | None = None
RETRY_SUMMARY_LOCK = threading.Lock()
PLAN_SECTION_NAMES = frozenset(
    {
        "Objective",
        "Finish Line",
        "Current Status",
        "Scope",
        "Acceptance Criteria",
        "Relationships",
        "Validation",
        "Decisions",
        "Open Questions",
    }
)

PLAN_COMMAND_CONTEXT: dict[str, tuple[str, str, bool]] = {
    "index": ("rest_api", "rest_core", False),
    "search": ("rest_api", "search", False),
    "show": ("rest_api", "rest_core", False),
    "claim": ("composite", "rest_core", True),
    "release-claim": ("composite", "rest_core", True),
    "release": ("composite", "rest_core", True),
    "wait": ("composite", "rest_core", True),
    "create": ("composite", "mixed", True),
    "update-section": ("rest_api", "rest_core", True),
    "link": ("rest_api", "rest_core", True),
    "unlink": ("rest_api", "rest_core", True),
    "deps": ("rest_api", "rest_core", False),
    "next": ("composite", "mixed", False),
    "close": ("composite", "mixed", True),
    "reconcile-landing": ("composite", "mixed", True),
    "reconcile-status": ("composite", "rest_core", True),
    "project-add": ("gh_cli_graphql", "graphql", True),
    "project-set": ("gh_cli_graphql", "graphql", True),
    "project-list": ("gh_cli_graphql", "graphql", False),
    "ensure-labels": ("rest_api", "rest_core", True),
    "milestone-list": ("rest_api", "rest_core", False),
    "milestone-show": ("rest_api", "rest_core", False),
    "milestone-create": ("rest_api", "rest_core", True),
    "milestone-update": ("rest_api", "rest_core", True),
    "milestone-close": ("rest_api", "rest_core", True),
}

DEFAULT_CONFIG: dict[str, Any] = {
    "labels": {
        **github_plan_status.DEFAULT_STATUS_LABELS,
        "audit": "audit",
        "own_project": "own-project",
    },
    "label_defs": {
        **github_agent.LABEL_DEFS,
        "plan": {"color": "5319e7", "description": "Durable planning issue"},
        "plan:active": {"color": "0e8a16", "description": "See https://github.com/cbusillo/codex-skills/blob/main/skills/github-plan/SKILL.md#status-labels"},
        "plan:blocked": {"color": "d93f0b", "description": "See https://github.com/cbusillo/codex-skills/blob/main/skills/github-plan/SKILL.md#status-labels"},
        "plan:waiting": {"color": "fbca04", "description": "See https://github.com/cbusillo/codex-skills/blob/main/skills/github-plan/SKILL.md#status-labels"},
        "plan:stale": {"color": "bfbfbf", "description": "See https://github.com/cbusillo/codex-skills/blob/main/skills/github-plan/SKILL.md#status-labels"},
        "plan:done": {"color": "006b75", "description": "See https://github.com/cbusillo/codex-skills/blob/main/skills/github-plan/SKILL.md#status-labels"},
        "audit": {"color": "d4c5f9", "description": "Question or completed work for the weekly direction audit"},
        "own-project": {"color": "c2e0c6", "description": "Own-project work in a mixed repository; counted as own by the weekly direction audit"},
    },
    "default_sections": [
        "Finish Line",
        "Current Status",
        "Relationships",
        "Acceptance Criteria",
        "Open Questions",
    ],
    "projects": {"enabled": True, "owner": None, "default_project": None},
    "workflow": {"default_manager": None, "repo_managers": {}},
    "project_fields": {
        "focus": "Focus",
        "manager": "Manager",
        "finish_line": "Finish Line",
    },
}

class PlanError(Exception):
    def __init__(
        self,
        message: str,
        *,
        failure: Optional[github_api_core.FailureDetail] = None,
        api_result: Optional[dict[str, Any]] = None,
        payload: Optional[dict[str, Any]] = None,
    ):
        super().__init__(github_api_core.redact_string(message))
        self.failure = failure
        self.api_result = github_api_core.redact_body(api_result) if api_result is not None else None
        self.payload = github_api_core.redact_body(payload or {})


class ClassifiedPlanError(PlanError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_at: Optional[int] = None,
        failure: Optional[github_api_core.FailureDetail] = None,
        api_result: Optional[dict[str, Any]] = None,
        payload: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message, failure=failure, api_result=api_result, payload=payload)
        self.code = code
        self.retry_at = retry_at


def plan_cancelled(exc: PlanError) -> bool:
    """A Ctrl-C that a retry wait turned into a result, not an exception."""
    return exc.failure is not None and exc.failure.cause == "cancelled"


def raise_if_cancelled(exc: PlanError) -> None:
    if plan_cancelled(exc):
        raise exc


def record_retry_fields(result: github_api_core.ApiResult) -> None:
    record_retry_summary(result.retry_summary)


def record_retry_summary(summary: Optional[github_api_core.RetrySummary]) -> None:
    global CURRENT_RETRY_FIELDS, CURRENT_RETRY_SUMMARY
    if summary is None:
        return
    with RETRY_SUMMARY_LOCK:
        CURRENT_RETRY_SUMMARY = github_api_core.aggregate_retry_summaries(
            [item for item in (CURRENT_RETRY_SUMMARY, summary) if item is not None]
        )
        CURRENT_RETRY_FIELDS = CURRENT_RETRY_SUMMARY.as_dict() if CURRENT_RETRY_SUMMARY else {}


def record_retry_metadata(result: dict[str, Any]) -> None:
    """Include the shared milestone backend's own transport receipt."""
    keys = ("attempts", "elapsed_wait", "retry_eligible", "last_actor", "last_bucket",
            "outcome_certainty", "reconciliation", "recommended_next_action", "effective_deadline")
    if all(key in result for key in keys):
        record_retry_summary(github_api_core.RetrySummary(**{key: result[key] for key in keys},
            exhausted_reason=result.get("retry_exhausted_reason")))


def die(
    message: str,
    *,
    detail: str | None = None,
    code: int = 1,
    error_code: str | None = None,
    retry_at: int | None = None,
    failure: github_api_core.FailureDetail | None = None,
    api_result: dict[str, Any] | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> NoReturn:
    if failure is None:
        failure = github_api_core.FailureDetail(
            cause=error_code or "plan_error",
            message=message,
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="not_started" if CURRENT_IS_WRITE else None,
        )
    payload: dict[str, Any] = dict(extra_payload or {})
    if detail:
        payload["detail"] = detail.strip()
    if api_result is not None:
        payload["api_result"] = api_result
    expected_actor = (
        api_result["expected_actor"]
        if api_result is not None and "expected_actor" in api_result
        else EXPECTED_ACTOR
    )
    envelope = github_api_core.terminal_failure(
        failure,
        operation=CURRENT_OPERATION,
        payload=payload,
        expected_actor=expected_actor,
        transport=CURRENT_TRANSPORT,
        bucket=CURRENT_BUCKET,
        exit_code=code,
        error=message,
        error_code=error_code,
    )
    if api_result is not None:
        for key in (
            "actor",
            "status",
            "request_id",
            "quota",
            "rate_limit",
            "retry_at",
            "retry_after",
            "graphql_operation",
            "completed_steps",
            "failed_step",
            *github_api_core.RETRY_TERMINAL_KEYS,
        ):
            if api_result.get(key) is not None:
                envelope[key] = api_result[key]
    if retry_at is not None:
        envelope["retry_at"] = retry_at
    if CURRENT_RETRY_FIELDS:
        envelope.update(CURRENT_RETRY_FIELDS)
    github_api_core.emit_terminal(envelope, stderr_message=f"error: {message}")
    raise SystemExit(code)


def emit(payload: Any) -> None:
    if isinstance(payload, dict) and CURRENT_RETRY_FIELDS:
        payload = {**payload, **CURRENT_RETRY_FIELDS}
    actor = payload.get("actor") if isinstance(payload, dict) else None
    expected_actor = payload.get("expected_actor", EXPECTED_ACTOR) if isinstance(payload, dict) else EXPECTED_ACTOR
    completed_steps = payload.get("completed_steps") if isinstance(payload, dict) else None
    github_api_core.emit_terminal(
        github_api_core.terminal_success(
            payload,
            operation=CURRENT_OPERATION,
            actor=actor,
            expected_actor=expected_actor,
            transport=CURRENT_TRANSPORT,
            bucket=CURRENT_BUCKET,
            completed_steps=completed_steps if isinstance(completed_steps, list) else None,
        )
    )


def get_runtime_home() -> pathlib.Path:
    if os.environ.get("CODE_HOME"):
        return pathlib.Path(os.environ["CODE_HOME"]).expanduser()
    if os.environ.get("CODEX_HOME"):
        return pathlib.Path(os.environ["CODEX_HOME"]).expanduser()
    code_home = pathlib.Path("~/.code").expanduser()
    if (code_home / "skills").is_dir():
        return code_home
    return pathlib.Path("~/.codex").expanduser()


def workspace_config_path() -> pathlib.Path:
    return get_runtime_home() / "github-planning.json"


def comment_route() -> tuple[str, str, Optional[str]]:
    """Resolve the same explicit actor route used by plan writes."""
    skip_bot = os.environ.get("GH_PLAN_SKIP_BOT") == "1"
    if BOT_GH.exists() and not skip_bot:
        return "automation-gh", str(BOT_GH), EXPECTED_ACTOR
    if skip_bot:
        return "active-gh-user", "gh", None

    failure = github_api_core.FailureDetail(
        cause="invalid_credentials",
        message="Automation GitHub helper is unavailable and active auth was not explicitly authorized",
        retryable=False,
        fallback_eligible=True,
        disposition="requires_authorization",
        write_outcome="not_started",
        failed_step="auth_selection",
    )
    result = github_api_core.ApiResult(
        ok=False,
        status=0,
        body=None,
        operation=CURRENT_OPERATION,
        expected_actor=EXPECTED_ACTOR,
        host=github_api_core.DEFAULT_HOST,
        transport="rest_api",
        bucket="rest_core",
        failure=failure,
        failed_step="auth_selection",
    )
    raise PlanError(failure.message, failure=failure, api_result=result.as_dict())


def merge_completed_steps(*groups: list[str] | None) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for step in group or []:
            if step not in merged:
                merged.append(step)
    return merged


def plan_error_from_issue(
    exc: github_issue_core.IssueError,
    *,
    completed_steps: list[str] | None = None,
) -> PlanError:
    combined_steps = merge_completed_steps(completed_steps, exc.failure.completed_steps)
    exc.failure.completed_steps = combined_steps
    api_result = dict(exc.api_result or {})
    if api_result:
        api_result["completed_steps"] = combined_steps
        api_result["failed_step"] = api_result.get("failed_step") or exc.failure.failed_step
        nested_failure = api_result.get("failure")
        if isinstance(nested_failure, dict):
            api_result["failure"] = {**nested_failure, "completed_steps": combined_steps}
    return PlanError(
        str(exc),
        failure=exc.failure,
        api_result=api_result or None,
        payload=exc.payload,
    )


def plan_error_from_comment(
    exc: github_comment_core.CommentError,
    *,
    completed_steps: list[str] | None = None,
) -> PlanError:
    combined_steps = merge_completed_steps(completed_steps, exc.failure.completed_steps)
    exc.failure.completed_steps = combined_steps
    api_result = dict(exc.api_result or {})
    if api_result:
        api_result["completed_steps"] = combined_steps
        api_result["failed_step"] = api_result.get("failed_step") or exc.failure.failed_step
        nested_failure = api_result.get("failure")
        if isinstance(nested_failure, dict):
            api_result["failure"] = {**nested_failure, "completed_steps": combined_steps}
    return PlanError(
        str(exc),
        failure=exc.failure,
        api_result=api_result or None,
        payload=exc.payload,
    )


def plan_close_reason(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized not in {"completed", "not_planned"}:
        raise PlanError("Close reason must be completed or not planned")
    return normalized


def run_raw(
    args: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    prefer_active: bool = False,
    operation: str | None = None,
    is_write: bool | None = None,
    bucket: str | None = None,
    graphql_operation: github_api_core.GraphQLOperation | None = None,
    completed_steps: list[str] | None = None,
    failed_step: str = "gh_invocation",
    repository: str | None = None,
) -> tuple[str, str, str]:
    """Run gh through the configured actor without implicit identity fallback."""
    tried: list[tuple[str, subprocess.CompletedProcess[str]]] = []
    skip_bot = os.environ.get("GH_PLAN_SKIP_BOT") == "1"
    bot_enabled = BOT_GH.exists() and not skip_bot
    active_first = prefer_active and os.environ.get("GH_PLAN_ALLOW_ACTIVE_FIRST") == "1"
    commands: list[tuple[str, list[str]]] = []
    if active_first:
        commands.append(("active-gh-user", ["gh", *args]))
    elif bot_enabled:
        commands.append(("automation-gh", [str(BOT_GH), *args]))
    elif skip_bot:
        commands.append(("active-gh-user", ["gh", *args]))
    else:
        inferred = github_api_core.infer_gh_command_context(args, input_text=input_text)
        resolved_is_write = inferred.is_write if is_write is None else is_write
        failure = github_api_core.FailureDetail(
            cause="invalid_credentials",
            message="Automation GitHub helper is unavailable and active auth was not explicitly authorized",
            retryable=False,
            fallback_eligible=True,
            disposition="requires_authorization",
            write_outcome="not_started" if resolved_is_write else None,
            completed_steps=list(completed_steps or []),
            failed_step="auth_selection",
        )
        auth_result = github_api_core.ApiResult(
            ok=False,
            status=0,
            body=None,
            operation=operation or CURRENT_OPERATION,
            expected_actor=EXPECTED_ACTOR,
            host=github_api_core.DEFAULT_HOST,
            transport=inferred.transport,
            bucket=bucket or inferred.bucket,
            graphql_operation=graphql_operation or inferred.graphql_operation,
            completed_steps=list(completed_steps or []),
            failure=failure,
            failed_step="auth_selection",
        )
        raise PlanError(failure.message, failure=failure, api_result=auth_result.as_dict())

    route_actor, command = commands[-1]
    inferred = github_api_core.infer_gh_command_context(args, input_text=input_text)
    resolved_is_write = inferred.is_write if is_write is None else is_write
    resolved_bucket = bucket or inferred.bucket
    resolved_graphql_operation = graphql_operation or inferred.graphql_operation
    resolved_operation = operation or CURRENT_OPERATION
    request_repository = repository if repository is not None else github_api_core.github_http_cache.repository_from_command(args)
    request_env = {**os.environ, "GH_REPO": repository} if repository is not None else None
    initial_retry_actor = route_actor if route_actor == "active-gh-user" else EXPECTED_ACTOR
    initial_expected_actor = None if route_actor == "active-gh-user" else EXPECTED_ACTOR
    retry_rule, _ = github_api_core.operation_retry_rule(resolved_operation)
    if route_actor != "active-gh-user":
        try:
            initial_retry_actor, initial_expected_actor, identity_prefix = github_api_core.request_identity(
                operation=resolved_operation, is_write=resolved_is_write or inferred.is_write,
                gh_cmd=command[0], gh_prefix_args=[], actor=initial_retry_actor,
                expected_actor=initial_expected_actor,
                repository=request_repository,
                preserve_identity=bool(prefer_active and retry_rule and
                                       retry_rule.actor_policy == "automation_required_with_explicit_project_override"),
            )
        except github_identity.GitHubAppError as error:
            result = github_api_core._identity_failure(
                error, operation=resolved_operation, is_write=resolved_is_write,
                actor=initial_retry_actor, expected_actor=initial_expected_actor, host=None, bucket=resolved_bucket,
            )
            raise PlanError(str(error), failure=result.failure, api_result=result.as_dict()) from error
        command = [command[0], *identity_prefix, *command[1:]]
    probe_allowed = bool(
        retry_rule and retry_rule.retry_eligibility in {"safe", "conditional"}
    )

    if not check:
        unchecked_timeout = github_api_core.remaining_retry_timeout_seconds()
        if unchecked_timeout <= 0:
            return route_actor, "", "GitHub command skipped because the retry deadline expired"
        try:
            unchecked = subprocess.run(
                command,
                input=input_text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=request_env,
                timeout=unchecked_timeout,
            )
        except subprocess.TimeoutExpired:
            return route_actor, "", "GitHub command exceeded the effective retry deadline"
        return route_actor, unchecked.stdout, unchecked.stderr

    last_proc: subprocess.CompletedProcess[str] | None = None
    last_display_actor = route_actor

    def attempt(timeout_seconds: Optional[float] = None) -> github_api_core.ApiResult:
        nonlocal last_proc, last_display_actor
        attempt_started = time.monotonic()
        try:
            proc = subprocess.run(
                command,
                input=input_text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=request_env,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            partial_stderr = github_api_core.subprocess_output_text(exc.stderr).strip()
            timeout_message = "GitHub command exceeded the effective retry deadline"
            proc = subprocess.CompletedProcess(
                args=command,
                returncode=124,
                stdout="",
                stderr="\n".join(part for part in (partial_stderr, timeout_message) if part),
            )
            last_proc = proc
            tried.append((route_actor, proc))
            result = github_api_core.subprocess_timeout_result(
                operation=resolved_operation,
                is_write=resolved_is_write,
                actor=initial_retry_actor,
                expected_actor=initial_expected_actor,
                host=github_api_core.DEFAULT_HOST,
                transport=inferred.transport,
                bucket=resolved_bucket,
                graphql_operation=resolved_graphql_operation,
                completed_steps=completed_steps,
                failed_step=failed_step,
                stderr=exc.stderr,
            )
            last_display_actor = result.actor or route_actor
            return result
        last_proc = proc
        tried.append((route_actor, proc))
        reported_actor = github_api_core.actor_from_gh_stderr(proc.stderr)
        authorized_fallback = github_api_core.active_fallback_was_authorized(proc.stderr)
        actual_actor = reported_actor or initial_retry_actor
        if "--include" in args:
            status, headers, _ = github_api_core.parse_gh_include_output(proc.stdout)
            endpoint = next((item for item in args if re.match(
                r"^/?(?:repos/|repositories/|orgs/|users/|user(?:/|$)|app/|installation/|search/|graphql$|rate_limit$)", item
            )), "")
            method_flag = "-X" if "-X" in args else "--method" if "--method" in args else None
            method = args[args.index(method_flag) + 1] if method_flag else "GET"
            github_api_core.github_request_usage.record_response(
                method=method, path=endpoint, status=status, headers=headers,
                operation=resolved_operation, actor=actual_actor, bucket=resolved_bucket, host=github_api_core.DEFAULT_HOST,
            )
        expected_context_actor = None if authorized_fallback else initial_expected_actor
        last_display_actor = reported_actor or route_actor
        if proc.returncode == 0:
            return github_api_core.ApiResult(
                ok=True,
                status=0,
                body=None,
                operation=resolved_operation,
                actor=actual_actor,
                expected_actor=expected_context_actor,
                host=github_api_core.DEFAULT_HOST,
                transport=inferred.transport,
                bucket=resolved_bucket,
                graphql_operation=resolved_graphql_operation,
                completed_steps=list(completed_steps or []),
            )
        result = github_api_core.legacy_process_result(
            proc.returncode,
            proc.stdout,
            proc.stderr,
            operation=resolved_operation,
            is_write=resolved_is_write,
            actor=actual_actor,
            expected_actor=expected_context_actor,
            transport=inferred.transport,
            bucket=resolved_bucket,
            graphql_operation=resolved_graphql_operation,
            completed_steps=completed_steps,
            failed_step=failed_step,
        )
        if (
            probe_allowed
            and result.failure
            and result.failure.cause in github_api_core.PRIMARY_RATE_LIMIT_CAUSES
            and not result.failure.rate_limit
        ):
            probe_timeout = timeout_seconds
            if probe_timeout is not None:
                probe_timeout -= time.monotonic() - attempt_started
                if probe_timeout <= 0:
                    return result
            probe = github_api_core.rate_limit_probe(
                gh_cmd=command[0],
                actor=actual_actor,
                expected_actor=expected_context_actor,
                timeout_seconds=probe_timeout,
            )
            result = github_api_core.legacy_process_result(
                proc.returncode,
                proc.stdout,
                proc.stderr,
                operation=resolved_operation,
                is_write=resolved_is_write,
                actor=actual_actor,
                expected_actor=expected_context_actor,
                transport=inferred.transport,
                bucket=resolved_bucket,
                graphql_operation=resolved_graphql_operation,
                completed_steps=completed_steps,
                failed_step=failed_step,
                rate_limit_result=probe,
            )
        return result

    retried = github_api_core.run_with_retry(
        lambda: attempt(),
        operation=resolved_operation,
        is_write=resolved_is_write,
        actor=initial_retry_actor,
        expected_actor=initial_expected_actor,
        bucket=resolved_bucket,
        attempt_with_timeout=lambda timeout: attempt(timeout),
        repository=request_repository,
        retry_runtime=NEXT_READS.retry_runtime if NEXT_READS is not None and not resolved_is_write else None,
    )
    record_retry_fields(retried)
    if last_proc is None:
        raise PlanError("gh command did not execute", failure=retried.failure, api_result=retried.as_dict())
    if retried.ok:
        return last_display_actor, last_proc.stdout, last_proc.stderr
    detail = "\n".join(
        f"[{name}] exit={tried_proc.returncode}\n{tried_proc.stderr.strip()}"
        for name, tried_proc in tried
        if tried_proc.stderr.strip()
    )
    message = retried.failure.message if retried.failure else detail or last_proc.stdout or "gh command failed"
    raise PlanError(
        f"gh command failed: {message}",
        failure=retried.failure,
        api_result=retried.as_dict(),
    )


PROJECT_CACHE: dict[tuple[Any, ...], Any] = {}
GRAPHQL_PREFLIGHT_MINIMUM = 25
NEXT_PROJECT_ITEM_LIMIT = 1000
NEXT_RELATIONSHIP_LIMIT = 100
NEXT_PLAN_INVENTORY_LIMIT = 1000
NEXT_DEFAULT_SCAN_LIMIT = 500
NEXT_DELIVERY_REFERENCE_LIMIT = 8


def classify_project_error(message: str) -> str:
    lowered = message.lower()
    if "rate limit" in lowered or "graphql" in lowered or "secondary rate" in lowered:
        return "rate_limited"
    if (
        "resource not accessible" in lowered
        or "forbidden" in lowered
        or "permission denied" in lowered
        or "must have admin rights" in lowered
        or ("403" in lowered and "project" in lowered)
    ):
        return "project_auth_denied"
    if "not in project" in lowered:
        return "not_in_project"
    if "field not found" in lowered or "unknown option" in lowered or "no such field" in lowered:
        return "field_or_option_missing"
    if "could not resolve" in lowered or "not found" in lowered or "lookup" in lowered:
        return "lookup_stale"
    return "project_update_failed"


def project_error(
    message: str,
    *,
    retry_at: int | None = None,
    source: PlanError | None = None,
) -> ClassifiedPlanError:
    return ClassifiedPlanError(
        "cancelled" if source is not None and plan_cancelled(source) else classify_project_error(message),
        message,
        retry_at=retry_at,
        failure=source.failure if source else None,
        api_result=source.api_result if source else None,
    )


def project_failure_payload(
    exc: PlanError,
    *,
    owner: str | None = None,
    project: str | None = None,
    operation: str | None = None,
    non_blocking: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": str(exc), "error_code": classify_project_error(str(exc))}
    if isinstance(exc, ClassifiedPlanError):
        payload["error_code"] = exc.code
        if exc.retry_at is not None:
            payload["retry_at"] = exc.retry_at
    if exc.api_result is not None:
        payload["api_result"] = exc.api_result
    if owner or project:
        payload["target"] = {"owner": owner, "project": project}
    if operation:
        payload["operation"] = operation
    if non_blocking:
        payload["warning"] = True
        payload["blocking"] = False
        payload["message"] = "Issue operation completed; Project reconciliation needs follow-up."
        if payload.get("error_code") == "lookup_stale":
            payload["recommended_action"] = (
                "Check planning.projects owner/default_project config, disable Projects, "
                "create the Project, or verify auth can see it."
            )
            payload["recommended_actions"] = [
                "Check planning.projects owner/default_project config.",
                "Create or rename the configured Project if it is intentionally missing.",
                "Verify the acting GitHub identity can see the Project.",
                "Disable Project sync if this repo or workspace should not use Projects.",
            ]
        elif payload.get("error_code") == "project_auth_denied":
            payload["recommended_action"] = (
                "Grant the automation identity access to the Project, use Project-capable "
                "auth for this operation, disable Project sync, or fix stale Project config."
            )
            payload["recommended_actions"] = [
                "Grant the automation identity access to the Project.",
                "Use Project-capable auth for this operation.",
                "Disable Project sync if this repo or workspace should not use Projects.",
                "Fix planning.projects owner/default_project config if it is stale.",
            ]
    return payload


def ensure_graphql_budget(
    *,
    prefer_active: bool = False,
    recoverable: bool = False,
    minimum: int = GRAPHQL_PREFLIGHT_MINIMUM,
) -> None:
    def probe() -> github_api_core.ApiResult:
        try:
            actor, data = gh_json(
                ["api", *API_VERSION_ARGS, "rate_limit"],
                prefer_active=prefer_active,
            )
        except PlanError as exc:
            if isinstance(exc, ClassifiedPlanError):
                raise
            if recoverable:
                raise project_error(f"rate_limit preflight failed: {exc}", source=exc) from exc
            raise
        resources = data.get("resources") if isinstance(data, dict) else None
        graphql = resources.get("graphql") if isinstance(resources, dict) else None
        if not isinstance(graphql, dict):
            return github_api_core.ApiResult(
                ok=True,
                status=200,
                body=data,
                operation=CURRENT_OPERATION,
                actor=actor,
                host=github_api_core.DEFAULT_HOST,
                transport="rest_api",
                bucket="graphql",
            )
        remaining = graphql.get("remaining")
        reset = graphql.get("reset")
        rate_limit = github_api_core.RateLimitInfo(
            limit=graphql.get("limit") if isinstance(graphql.get("limit"), int) else None,
            remaining=remaining if isinstance(remaining, int) else None,
            reset=reset if isinstance(reset, int) else None,
            resource="graphql",
        )
        if not isinstance(remaining, int) or remaining >= minimum:
            return github_api_core.ApiResult(
                ok=True,
                status=200,
                body=data,
                operation=CURRENT_OPERATION,
                actor=actor,
                host=github_api_core.DEFAULT_HOST,
                transport="rest_api",
                bucket="graphql",
                rate_limit=rate_limit,
            )
        failure = github_api_core.FailureDetail(
            cause="graphql_primary_rate_limited",
            message=(
                f"GraphQL quota too low for Project operation: "
                f"remaining={remaining}, minimum={minimum}"
            ),
            retryable=True,
            fallback_eligible=False,
            disposition="retry",
            rate_limit=rate_limit.as_dict(),
        )
        return github_api_core.ApiResult(
            ok=False,
            status=200,
            body=data,
            operation=CURRENT_OPERATION,
            actor=actor,
            host=github_api_core.DEFAULT_HOST,
            transport="rest_api",
            bucket="graphql",
            rate_limit=rate_limit,
            failure=failure,
            failed_step="graphql_preflight",
        )

    attempts_before = CURRENT_RETRY_SUMMARY.attempts if CURRENT_RETRY_SUMMARY is not None else 0
    result = github_api_core.run_with_retry(
        probe,
        operation=CURRENT_OPERATION,
        is_write=False,
        bucket="graphql",
        retry_runtime=NEXT_READS.retry_runtime if NEXT_READS is not None else None,
    )
    attempts_after_nested = CURRENT_RETRY_SUMMARY.attempts if CURRENT_RETRY_SUMMARY is not None else 0
    nested_attempts = max(0, attempts_after_nested - attempts_before)
    summary = result.retry_summary
    if summary is not None:
        record_retry_summary(
            replace(summary, attempts=max(0, summary.attempts - nested_attempts))
        )
    if result.ok:
        return
    payload = result.as_dict()
    retry_at = payload.get("retry_at")
    raise ClassifiedPlanError(
        "cancelled" if result.failure and result.failure.cause == "cancelled" else "rate_limited",
        result.failure.message if result.failure else "GraphQL quota preflight failed",
        retry_at=int(retry_at) if isinstance(retry_at, (int, float)) else None,
        failure=result.failure,
        api_result=payload,
    )


def gh_json(
    args: list[str],
    *,
    input_text: str | None = None,
    prefer_active: bool = False,
    operation: str | None = None,
    is_write: bool | None = None,
    bucket: str | None = None,
    graphql_operation: github_api_core.GraphQLOperation | None = None,
) -> tuple[str, Any]:
    actor, stdout, _ = run_raw(
        args,
        input_text=input_text,
        prefer_active=prefer_active,
        operation=operation,
        is_write=is_write,
        bucket=bucket,
        graphql_operation=graphql_operation,
    )
    if not stdout.strip():
        return actor, None
    try:
        return actor, json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise PlanError(f"Expected JSON from gh, got: {stdout[:300]}") from exc


def api_json(method: str, path: str, payload: Any = None, **kwargs: Any) -> tuple[str, Any]:
    if NEXT_READS is not None and method.upper() == "GET" and payload is None:
        try:
            return NEXT_READS.observe(path, lambda: api_json_uncached(method, path, payload, **kwargs))
        except github_next_reads.ReadDeadline as exc:
            raise PlanError(str(exc)) from exc
    return api_json_uncached(method, path, payload, **kwargs)


def api_json_uncached(
    method: str,
    path: str,
    payload: Any = None,
    *,
    operation: str | None = None,
    is_write: bool | None = None,
    bucket: str | None = None,
    graphql_operation: github_api_core.GraphQLOperation | None = None,
    completed_steps: list[str] | None = None,
    failed_step: str | None = None,
    repository: str | None = None,
) -> tuple[str, Any]:
    resolved_graphql_operation = graphql_operation
    if github_api_core.is_graphql_path(path) and resolved_graphql_operation is None:
        resolved_graphql_operation = github_api_core.infer_graphql_operation_type(payload)
    resolved_is_write = github_api_core.infer_is_write(
        method,
        path,
        payload,
        explicit_is_write=is_write,
        graphql_operation=resolved_graphql_operation,
    )
    resolved_bucket = bucket or ("graphql" if github_api_core.is_graphql_path(path) else "rest_core")
    # Planning observations must contact GitHub even immediately after another
    # process read them. Reuse a body only after its validator receives a 304.
    # Mutation preflights/readbacks and explicitly selected active-auth routes
    # retain their existing uncached transport.
    if (
        method.upper() == "GET" and payload is None and resolved_bucket == "rest_core"
        and not resolved_is_write and not CURRENT_IS_WRITE
        and CURRENT_OPERATION in {"github.plan.show", "github.plan.index", "github.plan.next"}
        and BOT_GH.exists() and EXPECTED_ACTOR
        and os.environ.get("GH_PLAN_SKIP_BOT") != "1"
        and os.environ.get("GH_PLAN_ALLOW_ACTIVE_FIRST") != "1"
        and not github_identity.active_auth_fallback_allowed()
        and not github_identity.own_user_opted_in()
    ):
        read_deadline = time.time() + github_api_core.remaining_retry_timeout_seconds()
        if NEXT_READS is not None and NEXT_READS.deadline is not None:
            read_deadline = min(NEXT_READS.deadline, time.time() + github_api_core.default_retry_policy().max_wait_seconds)
        reader = github_read.GitHubReader(
            gh_cmd=str(BOT_GH), expected_actor=EXPECTED_ACTOR, actor=EXPECTED_ACTOR,
            operation=operation or CURRENT_OPERATION, strict_actor=True,
            cache_enabled=True, cache_revalidate=True,
            deadline_at=read_deadline,
            retry_runtime=NEXT_READS.retry_runtime if NEXT_READS is not None else None,
            # Transport selects the App from the request path, so a wait or
            # blocker in another owner's repository is checked against that
            # owner's App rather than the command repository's.
            repository=repository or github_api_core.github_http_cache.repository_from_path(path),
        )
        reader.completed_steps = list(completed_steps or [])
        try:
            result = reader.request("GET", path, step=failed_step or "gh_invocation")
        except github_read.GitHubReadError as exc:
            record_retry_fields(exc.result)
            raise PlanError(str(exc), failure=exc.result.failure, api_result=exc.result.as_dict()) from exc
        record_retry_fields(result)
        return "automation-gh", result.body
    args = [
        "api",
        *API_VERSION_ARGS,
        "-X",
        method.upper(),
        path.lstrip("/"),
        "--include",
    ]
    input_text = None
    if payload is not None or method.upper() in ("POST", "PUT", "PATCH"):
        args.extend(["--input", "-"])
        input_text = json.dumps(payload if payload is not None else {})
    actor, stdout, _ = run_raw(
        args,
        input_text=input_text,
        operation=operation or CURRENT_OPERATION,
        is_write=resolved_is_write,
        bucket=resolved_bucket,
        graphql_operation=resolved_graphql_operation,
        completed_steps=completed_steps,
        failed_step=failed_step or "gh_invocation",
        repository=repository,
    )
    status, headers, body = github_api_core.parse_gh_include_output(stdout)
    if status == 0:
        status = 200
    rate_limit = github_api_core.RateLimitInfo.from_headers(headers)
    if not 200 <= status < 300 or (status == 200 and github_api_core.is_graphql_rate_limit_body(body)):
        failure = github_api_core.classify_error(status, headers, body, is_write=resolved_is_write)
        failure.completed_steps = completed_steps or []
        failure.failed_step = failed_step or f"http_{status}"
        result = github_api_core.ApiResult(
            ok=False,
            status=status,
            body=body,
            headers=headers,
            request_id=headers.get("x-github-request-id"),
            rate_limit=rate_limit if rate_limit.is_populated() else None,
            failure=failure,
            operation=operation or CURRENT_OPERATION,
            actor=actor,
            expected_actor=EXPECTED_ACTOR,
            host=github_api_core.DEFAULT_HOST,
            transport="graphql_api" if resolved_bucket == "graphql" else "rest_api",
            bucket=resolved_bucket,
            graphql_operation=resolved_graphql_operation,
            completed_steps=completed_steps or [],
            failed_step=failure.failed_step,
        )
        raise PlanError(failure.message, failure=failure, api_result=result.as_dict())
    return actor, body


def positive_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid limit: {value}") from exc
    if limit <= 0:
        raise argparse.ArgumentTypeError(f"invalid limit: {value}")
    return limit


def path_with_query(path: str, params: dict[str, Any]) -> str:
    return f"{path}?{urllib.parse.urlencode(params)}"


def collect_paged_rest_items(
    path: str,
    *,
    query: dict[str, Any],
    bucket: str,
    step_prefix: str,
    limit: int | None = None,
    collection_key: str | None = None,
    issue_only: bool = False,
    completed_steps: list[str] | None = None,
    page_metadata: list[dict[str, Any]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    actor = ""
    items: list[dict[str, Any]] = []
    steps = completed_steps if completed_steps is not None else []
    page = 1
    while limit is None or len(items) < limit:
        page_path = path_with_query(path, {**query, "per_page": 100, "page": page})
        page_actor, payload = api_json(
            "GET",
            page_path,
            bucket=bucket,
            completed_steps=steps,
            failed_step=f"{step_prefix}_page_{page}",
        )
        if not actor:
            actor = page_actor
        if collection_key:
            page_items = payload.get(collection_key) if isinstance(payload, dict) else None
        else:
            page_items = payload
        if not isinstance(page_items, list):
            raise PlanError(f"GitHub {step_prefix} response did not contain a list")
        if page_metadata is not None and isinstance(payload, dict):
            page_metadata.append({key: value for key, value in payload.items() if key != collection_key})
        for item in page_items:
            if not isinstance(item, dict):
                raise PlanError(f"GitHub {step_prefix} response contained a non-object item")
            if issue_only and item.get("pull_request") is not None:
                continue
            items.append(item)
            if limit is not None and len(items) >= limit:
                break
        steps.append(f"{step_prefix}_page_{page}")
        if len(page_items) < 100:
            break
        page += 1
    return actor, items


def compact_dedupe_issue(issue: dict[str, Any]) -> dict[str, Any]:
    state = issue.get("state")
    return {
        "number": issue.get("number"),
        "title": issue.get("title"),
        "state": state.upper() if isinstance(state, str) else state,
        "url": issue.get("html_url") or issue.get("url"),
    }


def find_existing_plan_issues(
    repo: str,
    title: str,
    *,
    completed_steps: list[str],
) -> tuple[str, list[dict[str, Any]]]:
    escaped_title = title.replace("\\", "\\\\").replace('"', '\\"')
    actor, items = collect_paged_rest_items(
        "/search/issues",
        query={"q": repository_scope.search_query(f'"{escaped_title}" in:title repo:{repo} is:issue')},
        bucket="search",
        step_prefix="dedupe_plan_issues",
        limit=100,
        collection_key="items",
        issue_only=True,
        completed_steps=completed_steps,
    )
    exact = [compact_dedupe_issue(item) for item in items if item.get("title") == title]
    return actor, exact


def plan_error_status(error: PlanError) -> int | None:
    if not isinstance(error.api_result, dict):
        return None
    status = error.api_result.get("status")
    return status if isinstance(status, int) else None


def git_root(start: pathlib.Path | None = None) -> pathlib.Path | None:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start or pathlib.Path.cwd(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if proc.returncode == 0:
        return pathlib.Path(proc.stdout.strip())
    return None


def repo_from_git(start: pathlib.Path | None = None) -> str | None:
    proc = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=start or pathlib.Path.cwd(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+)/([^/.]+)(?:\.git)?$", url)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return None


def default_repo(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    repo = repo_from_git()
    if repo:
        return repo
    actor, stdout, stderr = run_raw(["repo", "view", "--json", "nameWithOwner"], check=False)
    if not stdout.strip():
        raise PlanError("Could not resolve a GitHub repo; pass --repo OWNER/REPO")
    try:
        data = json.loads(stdout)
        return data["nameWithOwner"]
    except Exception as exc:  # pragma: no cover - defensive CLI UX
        raise PlanError("Could not resolve a GitHub repo; pass --repo OWNER/REPO") from exc


def deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def repo_config_path(
    repo: str | None, *, checkout: str | pathlib.Path | None = None,
    discovery: dict[str, Any] | None = None,
) -> pathlib.Path | None:
    candidates: list[pathlib.Path] = [pathlib.Path(checkout).expanduser()] if checkout is not None else []
    root = git_root()
    if root:
        candidates.append(root)
    if repo:
        repo_name = repo.split("/", 1)[1]
        candidates.append(pathlib.Path.home() / "Developer" / repo_name)
    verified_checkout: pathlib.Path | None = None
    for candidate in candidates:
        if not candidate.exists():
            continue
        if repo and (repo_from_git(candidate) or "").casefold() != repo.casefold():
            continue
        verified_checkout = verified_checkout or candidate
        path = candidate / ".github/github.json"
        if path.exists():
            if discovery is not None:
                discovery.update({"repo": repo, "status": "discovered", "path": str(path)})
            return path
    if discovery is not None:
        discovery.update({
            "repo": repo,
            "status": "verified_defaults" if verified_checkout else "unavailable",
            "path": None,
        })
        if verified_checkout is None:
            discovery["next_action"] = "rerun_with_planning_checkout"
    return None


def load_config(
    repo: str | None = None, *, checkout: str | pathlib.Path | None = None,
    discovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    workspace_config = workspace_config_path()
    if workspace_config.exists():
        config = deep_merge(config, json.loads(workspace_config.read_text()))
    if discovery is not None:
        repo_config = repo_config_path(repo, checkout=checkout, discovery=discovery)
        discovery["workspace_config"] = str(workspace_config) if workspace_config.exists() else None
    else:
        repo_config = repo_config_path(repo, checkout=checkout) if checkout is not None else repo_config_path(repo)
    if repo_config and repo_config.exists():
        data = json.loads(repo_config.read_text())
        if isinstance(data.get("planning"), dict):
            config = deep_merge(config, data["planning"])
    return config


def labels(config: dict[str, Any], *keys: str) -> list[str]:
    return [config["labels"][key] for key in keys]


def resolve_manager_value(value: Any) -> str | None:
    if not value:
        return None
    raw_value = str(value).strip()
    if not raw_value:
        return None
    if raw_value.casefold().startswith("person:"):
        return resolve_person_for_project(raw_value)
    return raw_value


def resolve_required_manager_value(value: Any) -> str | None:
    raw_value = str(value).strip() if value else ""
    return resolve_manager_value(raw_value)


def resolve_person_for_project(value: str) -> str | None:
    if not PEOPLE_RESOLVER.exists():
        return None
    uv = shutil.which("uv")
    if not uv:
        return None
    try:
        command = [uv, "run", str(PEOPLE_RESOLVER), value, "--strict"]
        proc = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    match = payload.get("match") if isinstance(payload, dict) else None
    if not isinstance(match, dict):
        return None
    preferred = match.get("preferred_reference")
    if isinstance(preferred, str) and preferred.strip():
        return preferred.strip()
    display_name = match.get("display_name")
    if isinstance(display_name, str) and display_name.strip():
        return display_name.strip()
    return None


def issue_ref(ref: str, repo: str) -> tuple[str, int]:
    ref = ref.strip()
    url = re.search(r"github\.com/([^/]+/[^/]+)/issues/(\d+)", ref)
    if url:
        return url.group(1), int(url.group(2))
    full = re.fullmatch(r"([^/\s]+/[^#\s]+)#(\d+)", ref)
    if full:
        return full.group(1), int(full.group(2))
    if ref.startswith("#"):
        return repo, int(ref[1:])
    if ref.isdigit():
        return repo, int(ref)
    raise PlanError(f"Unsupported issue reference: {ref}")


def get_issue(ref: str, repo: str) -> tuple[str, dict[str, Any]]:
    issue_repo, number = issue_ref(ref, repo)
    actor, data = api_json("GET", f"/repos/{issue_repo}/issues/{number}", failed_step="get_issue")
    data["repo"] = issue_repo
    return actor, data


def rest_edit_issue(
    repo: str,
    number: int,
    *,
    body: str | None = None,
    title: str | None = None,
    label_names: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {}
    if body is not None:
        payload["body"] = body
    if title is not None:
        payload["title"] = title
    if label_names is not None:
        payload["labels"] = label_names
    if not payload:
        raise PlanError("No issue fields to update")
    actor, data = api_json(
        "PATCH",
        f"/repos/{repo}/issues/{number}",
        payload,
        failed_step="edit_issue",
    )
    if not isinstance(data, dict):
        raise PlanError("gh api issue edit returned no issue")
    data["repo"] = repo
    return actor, data


def get_issue_compact(ref: str, repo: str) -> tuple[str, dict[str, Any]]:
    actor, issue = get_issue(ref, repo)
    return actor, compact_issue(issue)


def compact_issue(issue: dict[str, Any]) -> dict[str, Any]:
    milestone = issue.get("milestone") or {}
    deps = issue.get("issue_dependencies_summary") or {}
    sub = issue.get("sub_issues_summary") or {}
    repo = issue.get("repo") or (issue.get("repository") or {}).get("full_name")
    return {
        "repo": repo,
        "number": issue.get("number"),
        "title": issue.get("title"),
        "state": issue.get("state"),
        "state_reason": issue.get("state_reason"),
        "updated_at": issue.get("updated_at") or issue.get("updatedAt"),
        "url": issue.get("html_url") or issue.get("url"),
        "labels": normalize_labels(issue.get("labels")),
        "milestone": milestone.get("title") if isinstance(milestone, dict) else None,
        "dependencies": {
            "blocked_by": deps.get("blocked_by"),
            "blocking": deps.get("blocking"),
        },
        "sub_issues": {
            "total": sub.get("total"),
            "completed": sub.get("completed"),
            "percent_completed": sub.get("percent_completed"),
        },
    }


def issue_repository_name(issue: dict[str, Any]) -> str | None:
    repo = issue.get("repo")
    if isinstance(repo, str) and re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
        return repo
    repository = issue.get("repository")
    if isinstance(repository, str) and re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
        return repository
    if isinstance(repository, dict):
        full_name = repository.get("full_name")
        if isinstance(full_name, str) and re.fullmatch(r"[^/\s]+/[^/\s]+", full_name):
            return full_name
    for key in ("repository_url", "html_url", "url"):
        value = issue.get(key)
        if not isinstance(value, str):
            continue
        parts = [part for part in urllib.parse.urlparse(value).path.split("/") if part]
        if len(parts) >= 3 and parts[0] == "repos":
            candidate = f"{parts[1]}/{parts[2]}"
            if re.fullmatch(r"[^/\s]+/[^/\s]+", candidate):
                return candidate
        if len(parts) >= 4 and parts[-2] == "issues":
            candidate = f"{parts[-4]}/{parts[-3]}"
            if re.fullmatch(r"[^/\s]+/[^/\s]+", candidate):
                return candidate
    return None


def compact_relationship_issue(issue: dict[str, Any], relationship: str) -> dict[str, Any]:
    repo = issue_repository_name(issue)
    number = issue.get("number")
    state = issue.get("state")
    if repo is None:
        raise PlanError(f"GitHub {relationship} response omitted the related issue repository")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise PlanError(f"GitHub {relationship} response omitted a valid related issue number")
    if not isinstance(state, str) or state.casefold() not in {"open", "closed"}:
        raise PlanError(f"GitHub {relationship} response returned an ambiguous related issue state")
    title = issue.get("title")
    return {
        "repo": repo,
        "number": number,
        "title": title if isinstance(title, str) else None,
        "state": state.casefold(),
        "url": issue.get("html_url") or f"https://github.com/{repo}/issues/{number}",
    }


def read_handoff_history_child(event: dict[str, Any], source_repo: str) -> dict[str, Any]:
    """Resolve a REST timeline's omitted child through that exact event node."""
    child = event.get("sub_issue")
    if isinstance(child, dict):
        return child
    node_id = event.get("node_id")
    if child is not None or not isinstance(node_id, str) or not node_id.strip():
        raise ClassifiedPlanError("claim_child_history_invalid", "Native child history omitted its issue record")
    _, response = api_json(
        "POST", "/graphql", is_write=False, bucket="graphql", failed_step="claim_handoff_child_history_node",
        repository=source_repo,
        payload={"query": "query($id:ID!){node(id:$id){id __typename ... on SubIssueAddedEvent{subIssue{id number state title url repository{nameWithOwner}}}}}",
                 "variables": {"id": node_id}})
    data = response.get("data") if isinstance(response, dict) else None
    node = data.get("node") if isinstance(data, dict) else None
    if (not isinstance(node, dict) or response.get("errors") or node.get("id") != node_id
            or node.get("__typename") != "SubIssueAddedEvent"):
        raise ClassifiedPlanError("claim_child_history_invalid", "Native child history event could not be resolved")
    child = node.get("subIssue")
    repository = child.get("repository") if isinstance(child, dict) else None
    repo = repository.get("nameWithOwner") if isinstance(repository, dict) else None
    if (not isinstance(child, dict) or not isinstance(child.get("id"), str) or not child["id"].strip()
            or not isinstance(repo, str) or not re.fullmatch(r"[^/\s]+/[^/\s]+", repo)):
        raise ClassifiedPlanError("claim_child_history_invalid", "Native child history omitted its issue record")
    related = {"node_id": child["id"], "number": child.get("number"), "state": child.get("state"),
               "title": child.get("title"), "html_url": child.get("url"), "repository": {"full_name": repo}}
    compact_relationship_issue(related, "handoff history child")
    if related["html_url"] != f"https://github.com/{repo}/issues/{related['number']}":
        raise ClassifiedPlanError("claim_child_history_invalid", "Native child history returned an inconsistent issue identity")
    return related


def read_issue_relationships(
    issue_repo: str,
    number: int,
    *,
    include_blocking: bool,
) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    relationships: dict[str, list[dict[str, Any]]] = {}
    actor = ""
    endpoints = [
        ("blocked_by", f"/repos/{issue_repo}/issues/{number}/dependencies/blocked_by"),
    ]
    if include_blocking:
        endpoints.append(("blocking", f"/repos/{issue_repo}/issues/{number}/dependencies/blocking"))
    endpoints.append(("sub_issues", f"/repos/{issue_repo}/issues/{number}/sub_issues"))
    for relationship, endpoint in endpoints:
        page_actor, items = collect_paged_rest_items(
            endpoint,
            query={},
            bucket="rest_core",
            step_prefix=f"read_{relationship}",
        )
        actor = page_actor or actor
        relationships[relationship] = [
            compact_relationship_issue(item, relationship)
            for item in items
        ]
    return actor, relationships


def relationship_preflight_error(
    exc: PlanError,
    *,
    issue_repo: str,
    number: int,
) -> PlanError:
    if exc.failure is not None:
        failure = replace(
            exc.failure,
            write_outcome="not_started",
            completed_steps=[],
            failed_step="relationship_preflight",
        )
        result = "unavailable"
    else:
        failure = github_api_core.FailureDetail(
            cause="invalid_response",
            message=str(exc),
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="not_started",
            failed_step="relationship_preflight",
        )
        result = "ambiguous"
    api_result = dict(exc.api_result or {})
    if api_result:
        api_result["completed_steps"] = []
        api_result["failed_step"] = "relationship_preflight"
        api_result["failure"] = failure.as_dict()
    return PlanError(
        f"Cannot safely close {issue_repo}#{number}: relationship preflight {result}: {exc}",
        failure=failure,
        api_result=api_result or None,
        payload={
            **exc.payload,
            "relationship_preflight": {
                "result": result,
                "issue": {"repo": issue_repo, "number": number},
                "writes_started": False,
            },
        },
    )


def close_relationship_preflight(
    issue_repo: str,
    number: int,
    close_reason: str,
) -> tuple[str, dict[str, Any]]:
    try:
        actor, relationships = read_issue_relationships(
            issue_repo,
            number,
            include_blocking=False,
        )
    except PlanError as exc:
        raise relationship_preflight_error(exc, issue_repo=issue_repo, number=number) from exc
    blocked_by = relationships["blocked_by"]
    sub_issues = relationships["sub_issues"]
    open_blocked_by = [item for item in blocked_by if item["state"] == "open"]
    open_sub_issues = [item for item in sub_issues if item["state"] == "open"]
    preflight = {
        "result": "passed" if close_reason == "completed" else "not_planned_allowed",
        "policy": "require_closed" if close_reason == "completed" else "retain_and_report",
        "blocking_policy": "issues_blocked_by_this_plan_do_not_prevent_closure",
        "blocked_by": blocked_by,
        "sub_issues": sub_issues,
        "open_blocked_by": open_blocked_by,
        "open_sub_issues": open_sub_issues,
        "writes_started": False,
    }
    if close_reason == "completed" and (open_blocked_by or open_sub_issues):
        details: list[str] = []
        if open_blocked_by:
            details.append(f"open blockers: {', '.join(relationship_refs(open_blocked_by, issue_repo))}")
        if open_sub_issues:
            details.append(f"open sub-issues: {', '.join(relationship_refs(open_sub_issues, issue_repo))}")
        message = f"Cannot close {issue_repo}#{number} as completed; {'; '.join(details)}"
        failure = github_api_core.FailureDetail(
            cause="open_relationships",
            message=message,
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="not_started",
            failed_step="relationship_preflight",
        )
        preflight["result"] = "rejected"
        raise PlanError(
            message,
            failure=failure,
            payload={"relationship_preflight": preflight},
        )
    return actor, preflight


def section_machine_markers(text: str) -> dict[str, list[str]]:
    """Live helper records by kind, recognized as each kind's reader does; examples are not records."""
    found: dict[str, list[str]] = {}
    claims = github_plan_claim.live_lines(text)
    if claims:
        found["github-plan:claim"] = claims
    for _, line in github_plan_wait.live_lines(text, prefix="<!-- "):
        match = re.match(r"<!-- (github-plan:[\w-]+|github-skill-operation):?", line)
        if match and match.group(1) != "github-plan:claim" and line.strip() not in PLAN_RESERVED_MARKERS:
            found.setdefault(match.group(1), []).append(line)
    return found


def with_carried_markers(old_text: str, new_text: str) -> str:
    """Keep the section's wait, claim and other helper records unless the caller supplies that kind."""
    supplied = section_machine_markers(new_text)
    carried = [line for kind, lines in section_machine_markers(old_text).items()
               if kind not in supplied for line in lines]
    return "\n\n".join(part for part in (new_text.strip(), "\n".join(carried)) if part)


def replace_section(body: str, section: str, new_text: str) -> str:
    body = body or ""
    if any(marker in new_text for marker in PLAN_RESERVED_MARKERS):
        raise PlanError("Plan section content contains reserved GitHub plan ownership markers")
    pattern = re.compile(rf"(?ms)^##\s+{re.escape(section)}\s*\n.*?(?=^##\s+|\Z)")
    replacement = f"## {section}\n\n{new_text.strip()}\n\n"
    if pattern.search(body):
        return pattern.sub(
            lambda match: f"## {section}\n\n{with_carried_markers(match.group(0), new_text)}\n\n",
            body).rstrip() + "\n"
    if body and not body.endswith("\n"):
        body += "\n"
    return f"{body}\n{replacement}".lstrip()


def issue_author_login(issue: dict[str, Any]) -> str | None:
    author = issue.get("user") or issue.get("author")
    if not isinstance(author, dict):
        return None
    login = author.get("login")
    return login.strip() if isinstance(login, str) and login.strip() else None


def has_managed_provenance(body: str) -> bool:
    count = body.count(PLAN_MANAGED_PROVENANCE_MARKER)
    if count == 0:
        return False
    if count != 1:
        raise PlanError("Issue body contains duplicate GitHub plan provenance markers")
    if re.search(rf"(?m)^{re.escape(PLAN_MANAGED_PROVENANCE_MARKER)}[ \t]*\r?$", body) is None:
        raise PlanError("GitHub plan provenance marker must occupy its own line")
    return True


def issue_body_is_fully_managed(issue: dict[str, Any]) -> bool:
    body = issue.get("body") or ""
    has_managed_provenance(body)
    if contributor_plan_body(issue) is not None:
        return False
    author_login = issue_author_login(issue)
    managed_authors = {
        login.casefold()
        for login in (
            *github_identity.configured_bot_logins(),
            *([EXPECTED_ACTOR] if EXPECTED_ACTOR else []),
        )
    }
    return bool(
        author_login
        and author_login.casefold() in managed_authors
    )


def marked_block(body: str, start_marker: str, end_marker: str) -> tuple[int, int, str] | None:
    try:
        return github_plan_body.marked_block(body, start_marker, end_marker)
    except github_plan_body.PlanBodyError as exc:
        raise PlanError(str(exc)) from exc


def contributor_plan_body(issue: dict[str, Any]) -> str | None:
    try:
        return github_plan_body.contributor_plan_body(issue)
    except github_plan_body.PlanBodyError as exc:
        raise PlanError(str(exc)) from exc


def ensure_unmarked_contributor_body_is_unambiguous(issue: dict[str, Any]) -> None:
    body = issue.get("body") or ""
    recognized_sections = PLAN_SECTION_NAMES.intersection(section_map(body))
    if recognized_sections:
        headings = ", ".join(sorted(recognized_sections))
        raise PlanError(
            "Contributor issue already contains unmarked planning sections "
            f"({headings}); automation cannot safely distinguish the original request from prior managed content. "
            "Preserve the request in a linked maintainer-owned plan or migrate it explicitly."
        )


def wrap_contributor_plan(issue: dict[str, Any], managed_body: str) -> str:
    try:
        return github_plan_body.wrap_contributor_plan(issue, managed_body)
    except github_plan_body.PlanBodyError as exc:
        raise PlanError(str(exc)) from exc


def replace_issue_plan_section(issue: dict[str, Any], section: str, new_text: str) -> str:
    body = issue.get("body") or ""
    if any(marker in new_text for marker in PLAN_RESERVED_MARKERS):
        raise PlanError("Plan section content contains reserved GitHub plan ownership markers")
    if issue_body_is_fully_managed(issue):
        return replace_section(body, section, new_text)
    if section.casefold() == "original request":
        raise PlanError("Contributor-authored original request content is immutable")
    managed_body = contributor_plan_body(issue)
    if managed_body is None:
        ensure_unmarked_contributor_body_is_unambiguous(issue)
    updated_managed = replace_section(managed_body or "", section, new_text)
    if managed_body is None:
        return wrap_contributor_plan(issue, updated_managed)
    managed = marked_block(body, PLAN_MANAGED_START, PLAN_MANAGED_END)
    if managed is None:
        raise PlanError("Contributor issue managed plan block disappeared during update")
    start, end, _ = managed
    return body[:start] + updated_managed.rstrip() + body[end:]


def issue_plan_sections(issue: dict[str, Any]) -> dict[str, str]:
    if issue_body_is_fully_managed(issue):
        return section_map(issue.get("body") or "")
    managed_body = contributor_plan_body(issue)
    if managed_body is None:
        ensure_unmarked_contributor_body_is_unambiguous(issue)
        return {}
    return section_map(managed_body)


def read_plan_sections(issue: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    body = issue.get("body") or ""
    author_login = issue_author_login(issue)
    author_association = issue.get("author_association")
    if issue_body_is_fully_managed(issue):
        sections = section_map(body)
        ownership = "automation_managed"
        sections_source = "issue_body"
        section_updates_allowed = True
        unmarked_planning_headings: list[str] = []
    else:
        managed_body = contributor_plan_body(issue)
        if managed_body is not None:
            sections = section_map(managed_body)
            ownership = "contributor_envelope"
            sections_source = "managed_block"
            section_updates_allowed = True
            unmarked_planning_headings = []
        else:
            sections = section_map(body)
            ownership = "contributor_unmanaged"
            sections_source = "unmanaged_body"
            unmarked_planning_headings = sorted(PLAN_SECTION_NAMES.intersection(sections))
            original_request = body if body else str(issue.get("title") or "").strip()
            has_reserved_markers = any(marker in original_request for marker in PLAN_RESERVED_MARKERS)
            section_updates_allowed = not unmarked_planning_headings and not has_reserved_markers
    provenance = {
        "ownership": ownership,
        "author": author_login,
        "author_association": author_association,
        "sections_source": sections_source,
        "section_updates_allowed": section_updates_allowed,
        "unmarked_planning_headings": unmarked_planning_headings,
    }
    return sections, provenance


def template_body(title: str) -> str:
    return f"""{PLAN_MANAGED_PROVENANCE_MARKER}

## Objective

{title}

## Finish Line

This work is done when the desired outcome is observable and the next re-entry
point is captured.

## Current Status

State: Active
Next action: Decide the first concrete implementation step.
Last verified: Not yet verified.

## Scope

- In:
- Out:

## Acceptance Criteria

- [ ] Outcome is defined.
- [ ] Validation is captured.

## Relationships

- None yet.

## Validation

- Not defined yet.

## Decisions

- None yet.

## Open Questions

- None yet.
"""


def ensure_managed_provenance(body: str) -> str:
    if any(marker in body for marker in PLAN_OWNERSHIP_MARKERS):
        raise PlanError("Fully managed plan body contains contributor ownership markers")
    if has_managed_provenance(body):
        return body
    return f"{PLAN_MANAGED_PROVENANCE_MARKER}\n\n{body.lstrip()}"


def read_body(args: argparse.Namespace, fallback: str = "") -> str:
    if getattr(args, "body", None) is not None:
        return args.body
    body_file = getattr(args, "body_file", None)
    if body_file:
        if body_file == "-":
            return sys.stdin.read()
        return pathlib.Path(body_file).read_text()
    return fallback


def write_temp_body(body: str) -> pathlib.Path:
    with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
        tmp.write(body)
        return pathlib.Path(tmp.name)


def relationship_line(rel: str, target: dict[str, Any]) -> str:
    target_repo = target["repo"]
    target_number = int(target["number"])
    return f"- {rel}: {target_repo}#{target_number} - {target.get('html_url') or target.get('url')}"


def add_relationship_note(issue: dict[str, Any], rel: str, target: dict[str, Any]) -> str:
    line = relationship_line(rel, target)
    sections = issue_plan_sections(issue)
    current = sections.get("Relationships", "")
    current_lines = [item.strip() for item in current.splitlines()]
    if line in current_lines:
        return issue.get("body") or ""
    if not current or current.strip() in {"- None yet.", "None yet.", "None."}:
        new_text = line
    else:
        new_text = current.rstrip() + "\n" + line
    return replace_issue_plan_section(issue, "Relationships", new_text)


def remove_relationship_note(issue: dict[str, Any], rel: str, target: dict[str, Any]) -> str:
    line = relationship_line(rel, target)
    sections = issue_plan_sections(issue)
    current = sections.get("Relationships", "")
    current_lines = [item.strip() for item in current.splitlines()]
    if line not in current_lines:
        return issue.get("body") or ""
    kept = [item for item in current.splitlines() if item.strip() != line]
    new_text = "\n".join(kept).strip() or "- None yet."
    return replace_issue_plan_section(issue, "Relationships", new_text)


def ensure_labels(
    repo: str,
    wanted: list[str],
    config: dict[str, Any],
    *,
    create_unknown: bool = True,
    completed_steps: list[str] | None = None,
) -> tuple[str, list[str]]:
    steps = completed_steps if completed_steps is not None else []
    actor, existing = collect_paged_rest_items(
        f"/repos/{repo}/labels",
        query={},
        bucket="rest_core",
        step_prefix="list_labels",
        completed_steps=steps,
    )
    existing_names = {
        item["name"].casefold()
        for item in existing
        if isinstance(item.get("name"), str)
    }
    created: list[str] = []
    defs = config.get("label_defs", {})
    missing_without_defs: list[str] = []
    for name in wanted:
        normalized_name = name.casefold()
        if normalized_name in existing_names:
            continue
        info = defs.get(name)
        if info is None and not create_unknown:
            missing_without_defs.append(name)
            continue
        if info is None:
            info = {"color": "ededed", "description": "Planning label"}
        try:
            api_json(
                "POST",
                f"/repos/{repo}/labels",
                {
                    "name": name,
                    "color": info.get("color", "ededed"),
                    "description": info.get("description", "Planning label"),
                },
                completed_steps=steps,
                failed_step="create_label",
            )
        except PlanError as create_error:
            if plan_error_status(create_error) != 422:
                raise
            try:
                _, reconciled = api_json(
                    "GET",
                    f"/repos/{repo}/labels/{urllib.parse.quote(name, safe='')}",
                    completed_steps=steps,
                    failed_step="reconcile_label_create",
                )
            except PlanError:
                raise create_error
            reconciled_name = reconciled.get("name") if isinstance(reconciled, dict) else None
            if not isinstance(reconciled_name, str) or reconciled_name.casefold() != normalized_name:
                raise create_error
            existing_names.add(normalized_name)
            steps.append("reconcile_label_create")
            continue
        created.append(name)
        existing_names.add(normalized_name)
        steps.append("create_label")
    if missing_without_defs:
        missing = ", ".join(sorted(missing_without_defs))
        raise PlanError(
            f"Refusing to create undocumented label(s): {missing}. "
            "Create them intentionally first, or add planning.label_defs metadata."
        )
    return actor, created


def cmd_index(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    label = args.label or config["labels"]["plan"]
    actor, data = collect_paged_rest_items(
        f"/repos/{repo}/issues",
        query={
            "labels": label,
            "state": args.state,
            "sort": "updated",
            "direction": "desc",
        },
        bucket="rest_core",
        step_prefix="list_plan_issues",
        limit=args.limit,
        issue_only=True,
    )
    items = [compact_list_issue(repo, item) for item in data]
    try:
        _, metadata = api_json("GET", f"/repos/{repo}", bucket="rest_core", failed_step="inventory_repository")
        context = github_read.repository_disposition(metadata)
    except PlanError as exc:
        next_source_error(exc)
        context = github_read.repository_disposition(None)
    if not repository_scope.includes_repository(context, include_archived=getattr(args, "include_archived", False)):
        items = []
    emit({"ok": True, "actor": actor, "repo": repo, "repository": context,
          "count": len(items), "plans": [{**item, "repository": context} for item in items]})


def search_has_scope(query: str) -> bool:
    unquoted_query = re.sub(r'"(?:\\.|[^"\\])*"', '""', query)
    return bool(re.search(r"(?:^|[\s(])(?:repo|org|user):", unquoted_query, re.IGNORECASE))


def cmd_search(args: argparse.Namespace) -> None:
    query = args.query.strip()
    has_scope = search_has_scope(query)
    repo = args.repo or (repo_from_git() if has_scope else default_repo())
    query_parts = [query]
    if args.repo or not has_scope:
        query_parts.append(f"repo:{repo}")
    query_parts.append("is:issue")
    if args.state != "all":
        query_parts.append(f"is:{args.state}")
    page_metadata: list[dict[str, Any]] = []
    actor, data = collect_paged_rest_items(
        "/search/issues",
        query={"q": repository_scope.search_query(" ".join(part for part in query_parts if part),
                                                  include_archived=getattr(args, "include_archived", False))},
        bucket="search",
        step_prefix="search_plan_issues",
        limit=args.limit,
        collection_key="items",
        issue_only=True,
        page_metadata=page_metadata,
    )
    items = []
    for item in data:
        item_repo = issue_repository_name(item)
        if item_repo is None:
            raise PlanError("GitHub search response omitted the issue repository")
        items.append(compact_list_issue(item_repo, item))
    totals: list[int] = []
    for page in page_metadata:
        value = page.get("total_count")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            totals.append(value)
    partial_flags = [page.get("incomplete_results") for page in page_metadata]
    total_count = max(totals) if totals and len(totals) == len(page_metadata) else None
    incomplete_results = (
        True if any(value is True for value in partial_flags)
        else False if partial_flags and all(value is False for value in partial_flags)
        else None
    )
    emit({"ok": True, "actor": actor, "repo": repo, "count": len(items), "issues": items,
          "total_count": total_count, "incomplete_results": incomplete_results, "limit": args.limit})


BATCH_READ_CONTEXT: dict[str, Any] = {}

def read_planning_graph(repo: str, **kwargs: Any) -> list[dict[str, Any]] | None:
    BATCH_READ_CONTEXT.clear()
    BATCH_READ_CONTEXT.update({"used": False, "fallback_reason": "explicit_identity_route"})
    if not EXPECTED_ACTOR or not BOT_GH.exists():
        return None
    if os.environ.get("GH_PLAN_SKIP_BOT") == "1" or os.environ.get("GH_PLAN_ALLOW_ACTIVE_FIRST") == "1" or github_identity.active_auth_fallback_allowed() or github_identity.own_user_opted_in():
        return None
    reader = github_read.GitHubReader(gh_cmd=str(BOT_GH), expected_actor=EXPECTED_ACTOR, actor=EXPECTED_ACTOR, operation=CURRENT_OPERATION, strict_actor=True,
        cache_enabled=True, cache_revalidate=True,
        deadline_at=time.time() + github_api_core.remaining_retry_timeout_seconds())
    try:
        rows = github_plan_graph.planning_graph(reader, repo, **kwargs)
        for row in rows:
            row["_graph_actor"] = reader.actor
        BATCH_READ_CONTEXT.update({"used": True, "fallback_reason": None})
        return rows
    except (github_read.GitHubReadError, github_read.GitHubReadShapeError, KeyError, TypeError, ValueError, AttributeError) as error:
        if isinstance(error, github_read.GitHubReadError):
            # Ctrl-C in the graph's retry wait ends the command; it is not a REST fallback.
            raise_if_cancelled(PlanError(str(error), failure=error.result.failure, api_result=error.result.as_dict()))
        BATCH_READ_CONTEXT["fallback_reason"] = github_api_core.redact_string(str(error))
        # Discard the whole optional graph before recovering through REST.
        return None
    finally:
        BATCH_READ_CONTEXT["diagnostics"] = reader.diagnostics()
        BATCH_READ_CONTEXT["cost"] = sum((result.body.get("data") or {}).get("rateLimit", {}).get("cost", 0)
            for result in reader.results if isinstance(result.body, dict) and isinstance((result.body.get("data") or {}).get("rateLimit"), dict))
        for result in reader.results:
            record_retry_fields(result)


def cmd_show(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    issue_repo, number = issue_ref(args.issue, repo)
    config = load_config(issue_repo)
    graph = None
    if getattr(args, "batch_read", False):
        issue_repo, number = issue_ref(args.issue, repo)
        graph = read_planning_graph(issue_repo, number=number)
    actor, issue = (graph[0]["_graph_actor"], graph[0]) if graph else get_issue(args.issue, repo)
    body = issue.get("body") or ""
    result = compact_issue(issue)
    if args.full:
        result["body"] = body
        try:
            _, provenance = read_plan_sections(issue)
        except PlanError:
            provenance = {
                "ownership": "unknown",
                "author": issue_author_login(issue),
                "author_association": issue.get("author_association"),
                "sections_source": "malformed_ownership_markers",
                "section_updates_allowed": False,
                "unmarked_planning_headings": [],
            }
    else:
        names = args.sections or config.get("default_sections") or []
        sections, provenance = read_plan_sections(issue)
        result["sections"] = {name: sections.get(name, "") for name in names}
    result["provenance"] = provenance
    if graph:
        comments = issue["_graph_comments"]
    else:
        _, comments = collect_paged_rest_items(
            f"/repos/{issue['repo']}/issues/{issue['number']}/comments",
            query={},
            bucket="rest_core",
            step_prefix="list_issue_comments",
        )
    result["comments"] = [
        {
            "id": comment.get("id"),
            "author": (comment.get("user") or {}).get("login"),
            "created_at": comment.get("created_at"),
            "updated_at": comment.get("updated_at"),
            "url": comment.get("html_url"),
            "body": comment.get("body") or "",
        }
        for comment in comments
    ]
    try:
        wait = current_wait(issue, comments, issue["repo"])
    except (PlanError, ValueError) as exc:
        wait = {"version": 1, "state": "review", "evidence": str(exc), "waits": []}
    try:
        relationships = issue.get("_graph_relationships")
        if relationships is None:
            _, relationships = read_issue_relationships(issue["repo"], issue["number"], include_blocking=False)
        else:
            relationships = {key: [compact_relationship_issue(row, key) for row in rows]
                             for key, rows in relationships.items()}
        derived = github_plan_status.derive(issue, relationships, wait, label_names=config["labels"])
        derived["relationships_complete"] = True
    except (PlanError, ValueError, TypeError, KeyError) as exc:
        derived = {"status": "review", "blocked_by": [], "waits": wait["waits"],
                   "structured_wait": wait, "labels": [], "relationships_complete": False,
                   "error": {"message": str(exc), **getattr(exc, "payload", {})}}
    result["derived"] = derived
    result["recorded_labels"] = result["labels"]
    if derived["relationships_complete"]:
        result["labels"] = github_plan_status.project_labels(result["labels"], config["labels"], derived)
    if not args.full and "Current Status" in result["sections"]:
        result["recorded_current_status"] = result["sections"]["Current Status"]
        result["sections"]["Current Status"] = github_plan_status.render_status(result["sections"]["Current Status"], derived)
    emit({"ok": True, "actor": actor, "issue": result,
          **({"batch_read": BATCH_READ_CONTEXT} if getattr(args, "batch_read", False) else {})})


def wait_authors(repo: str) -> set[str]:
    """Use the same primary and historical automation identities as managed plans."""
    return github_plan_wait.authors(repo, expected_actor=EXPECTED_ACTOR)


def current_wait(issue: dict[str, Any], comments: list[dict[str, Any]], repo: str) -> dict[str, Any]:
    return github_plan_wait.current(issue, comments, wait_authors(repo))


def _claim_release_evidence(evidence_repo, _number, evidence_id):
    try:
        _, evidence = api_json("GET", f"/repos/{evidence_repo}/issues/comments/{evidence_id}",
                               bucket="rest_core", failed_step="claim_release_evidence")
        return evidence
    except PlanError as error:
        if plan_error_status(error) not in {403, 404}:
            raise
        return None


def claim_snapshot(ref: str, repo: str, *, allow_closed: bool = False) -> tuple[dict[str, Any], str, list[dict[str, Any]], bool]:
    _, issue = get_issue(ref, repo)
    if issue.get("state") not in ({"open", "closed"} if allow_closed else {"open"}):
        raise PlanError("Claim requires an open issue or pull request")
    sections, provenance = read_plan_sections(issue)
    _, comments = collect_paged_rest_items(
        f"/repos/{issue['repo']}/issues/{issue['number']}/comments",
        query={}, bucket="rest_core", step_prefix="claim_comments",
    )
    comments = github_plan_release.verified_comments(comments, _claim_release_evidence)
    can_update = "pull_request" not in issue and provenance["section_updates_allowed"] and provenance["ownership"] != "contributor_unmanaged"
    status = sections.get("Current Status", "")
    if "pull_request" in issue:
        comment_status = next((section_map(c.get("body") or "").get("Current Status", "")
                               for c in reversed(comments) if "Current Status" in section_map(c.get("body") or "")), "")
        status = "\n\n".join(part for part in (status, comment_status) if part)
    issue["claim_wait"] = current_wait(issue, comments, issue["repo"])
    return issue, status, comments, can_update


def cmd_release_claim(args: argparse.Namespace) -> None:
    """Release a closed session through the same maintained comment transport."""
    repo = default_repo(args.repo)
    args = claim_reference(args)
    actor, gh_cmd, expected_actor = comment_route()
    if not expected_actor or not args.confirm_session_ended:
        raise PlanError("Release requires configured automation auth and --confirm-session-ended after verifying closure")
    if not args.session or any(c in args.session for c in "\n\r"):
        raise PlanError("Releaser must name its native session ID")
    if pathlib.Path(gh_cmd).name == "gh-with-env-token" and github_identity.github_app_prefix(repo) == "GITHUB_CLIENT_APP":
        try:
            config = github_identity.github_app_config(repository=repo)
            assert config is not None
            _, selected_actor = github_identity.github_app_auth(config, repository=repo, require_installation=True)
        except github_identity.GitHubAppError as error:
            raise PlanError(str(error)) from error
        override = github_identity.configured_value("GH_WITH_ENV_TOKEN_EXPECTED_LOGIN")
        if override and override.casefold() != selected_actor.casefold():
            raise PlanError(f"Release would run as '{selected_actor}', expected '{override}'")
        expected_actor = selected_actor
    legacy_options = {"legacy": getattr(args, "legacy", False),
                      "legacy_branch": getattr(args, "legacy_branch", None),
                      "legacy_reason": getattr(args, "legacy_reason", None)}
    evidence_match = github_plan_release.COMMENT_URL.fullmatch(args.evidence_comment)
    if not evidence_match:
        raise PlanError("Evidence must be an exact GitHub issue or PR timeline-comment URL")
    evidence_repo, evidence_number, evidence_id = evidence_match.groups()
    _, evidence = api_json("GET", f"/repos/{evidence_repo}/issues/comments/{evidence_id}", bucket="rest_core", failed_step="release_evidence")
    if evidence.get("issue_url") != f"https://api.github.com/repos/{evidence_repo}/issues/{evidence_number}":
        raise PlanError("Session-ended evidence is not on the cited issue")
    completed = []
    result = {}
    release_state = "not_started"
    def recovery_action():
        if release_state == "confirmed":
            return ("The source release is confirmed. Preserve its unchanged attestation. Inspect the failed related "
                    "record and transport reconciliation before retrying; after resolving any unknown outcome, "
                    "rerun the same command with unchanged arguments to reuse the source and deduplicate related releases.")
        if release_state == "not_started":
            return ("This invocation started no release write; earlier records may exist. Inspect the issue history and "
                    "preserve any confirmed release's unchanged attestation. Resolve the current preflight refusal "
                    "and inspect a fresh --dry-run before releasing.")
        if release_state == "pending_unconfirmed":
            return ("The initial pending POST is unconfirmed and release_comment may be empty. Inspect the transport "
                    "operation marker and reconciliation evidence to locate any posted pending record before recovery; "
                    "never replay an unknown write. A pending record grants no release authority. Resolve the refusal, "
                    "freshly verify closure and post a new --evidence-comment attestation before another --dry-run.")
        return ("Read the exact release_comment, cited evidence and transport reconciliation before recovery; "
                "never replay an unknown write. If confirmed, rerun with unchanged arguments to reuse it. "
                "A pending record does not release ownership: resolve the refusal, freshly verify closure and post a new "
                "--evidence-comment attestation before another --dry-run and release. Preserve other claims, retained artifacts and waits.")
    # Fresh-read before writing and again after posting. Native peer coverage
    # stays explicit; the caller attests closure for hosts without peer APIs.
    def preflight():
        source_issue, _status, source_comments, _can_update = claim_snapshot(args.issue, repo, allow_closed=True)
        source = next((c for c in source_comments if c.get("id") == args.claim_comment), None)
        if source is None:
            raise PlanError("Source claim is missing from the canonical issue")
        if legacy_options["legacy"] and "pull_request" in source_issue:
            raise PlanError("Legacy release supports bare issue claims only; PR claims use structured release")
        source_inventory = github_plan_claim.local_inventory(source_issue["repo"], source_issue["number"])
        _, fresh_evidence = api_json("GET", f"/repos/{evidence_repo}/issues/comments/{evidence_id}", bucket="rest_core", failed_step="release_evidence_readback")
        if fresh_evidence != evidence:
            raise PlanError("Session-ended evidence changed during release")
        related_comments = []
        for related_id in args.related_claim_comment:
            related_comment = next((c for c in source_comments if c.get("id") == related_id), None)
            if related_comment is None or related_id == args.claim_comment:
                raise PlanError("Related ownership comment is missing or repeats the structured source")
            related_comments.append(related_comment)
        if expected_actor is None:
            raise PlanError("Release requires the configured source automation identity")
        facts = github_plan_release.parse_release(source, evidence, actor=expected_actor,
            role=args.role, releaser_session=args.session, **legacy_options)
        source_branch = facts.record["branch"]
        github_plan_release.termination.verify_attestation(
            getattr(args, "termination_record", None), evidence.get("body") or "",
            facts.record["session"])
        # Check source identity, closure and native peers before a retained-PR
        # refusal can be classified as independent issue work.
        checked_paths = github_plan_release.validate_source_release(facts, source_comments, source_inventory,
            retained_prs=args.retained_pr, related=related_comments)
        issue_refusal = None
        branch_query = ({"state": "all", "head": f"{source_issue['repo'].split('/')[0]}:{source_branch}"}
                        if legacy_options["legacy"] else {"state": "open"})
        _, open_pulls = collect_paged_rest_items(f"/repos/{source_issue['repo']}/pulls", query=branch_query,
            bucket="rest_core", step_prefix="release_source_branch_prs")
        if legacy_options["legacy"] and not any(
                (p.get("head") or {}).get("ref") == source_branch
                and (p.get("user") or {}).get("login") == expected_actor
                and github_plan_claim.pr_issue_reference(p, source_issue["number"], repo=source_issue["repo"],
                    attestation=True, record_kind="pr" if "pull_request" in source_issue else "issue")
                for p in open_pulls):
            raise PlanError("Legacy source branch requires an automation-authored PR linking this canonical record")
        branch_prs = [f"https://github.com/{source_issue['repo']}/pull/{p['number']}" for p in open_pulls
            if (p.get("head") or {}).get("ref") == source_branch]
        subject_pr = "pull_request" in source_issue
        subject_url = f"https://github.com/{source_issue['repo']}/pull/{source_issue['number']}"
        if subject_pr or legacy_options["legacy"]:
            # The claim's branch identifies source-session activity. A separate
            # Dependabot work-record branch may advance after that session ends.
            source_ref = None
            try:
                _, source_ref = api_json("GET",
                    f"/repos/{source_issue['repo']}/git/ref/heads/{urllib.parse.quote(source_branch, safe='')}",
                    bucket="rest_core", failed_step="release_claim_branch")
            except PlanError as branch_error:
                if (plan_error_status(branch_error) != 404
                        or source_branch in source_inventory["remote_branches"]):
                    raise
            if source_ref is not None:
                _, source_commit = api_json("GET",
                    f"/repos/{source_issue['repo']}/commits/{source_ref['object']['sha']}",
                    bucket="rest_core", failed_step="release_claim_branch_commit")
                if github_plan_release.stamp(source_commit["commit"]["committer"]["date"]) >= facts.cutoff:
                    raise PlanError("Claimed source branch has commit activity after the cited session-ended evidence")
        checked_prs = list(dict.fromkeys([*args.retained_pr, *branch_prs, *([subject_url] if subject_pr else [])]))
        retained_branches = set()
        for url in checked_prs:
            match = re.fullmatch(r"https://github\.com/([^/\s]+/[^/\s]+)/pull/([1-9]\d*)", url)
            if not match or match.group(1).casefold() != source_issue["repo"].casefold():
                raise PlanError("Retained PR must be an exact same-repository PR URL")
            _, pull = api_json("GET", f"/repos/{source_issue['repo']}/pulls/{match.group(2)}", bucket="rest_core", failed_step="release_retained_pr")
            retained_branches.add(pull["head"]["ref"])
            _, head_commit = api_json("GET", f"/repos/{source_issue['repo']}/commits/{pull['head']['sha']}", bucket="rest_core", failed_step="release_retained_head")
            try:
                github_plan_release.check_retained_pr(pull, head_commit, repo=source_issue["repo"],
                    number=source_issue["number"], actor=expected_actor, ended=facts.cutoff.isoformat(),
                    subject_pr=source_issue["number"] if subject_pr else None, source_branch=source_branch,
                    allow_closed_unmerged=legacy_options["legacy"])
            except github_plan_release.IssueReleaseRefusal as retained_error:
                issue_refusal = retained_error
        github_plan_release.validate_retained_release(facts, source_inventory,
            retained_branches=retained_branches, checked_paths=checked_paths)
        release_body = github_plan_release.render_release(facts,
            evidence_url=args.evidence_comment, retained_prs=args.retained_pr,
            related=related_comments, checked_prs=checked_prs)
        if issue_refusal is not None:
            raise issue_refusal
        return source_issue, source_comments, source_inventory, release_body, related_comments
    try:
        issue, comments, inventory, body, related = preflight()
        completed.append("release_preflight")
        receipt = github_plan_release.receipt_for({"body": body})
        if receipt is None:
            raise PlanError("Prepared release has no valid receipt")
        existing = next((c for c in reversed(comments)
                         if (prior := github_plan_release.receipt_for(c)) is not None
                         and all(prior.get(k) == v for k, v in receipt.items() if k not in {"role", "releaser_session", "checked_prs"})
                         and (c.get("user") or {}).get("login") == expected_actor
                         and github_plan_claim.released_claim_id(c.get("body") or "") == args.claim_comment), None)
        def comment_result(comment):
            return {"comment": {"id": comment["id"], "url": comment.get("html_url")}}
        if existing is None:
            for comment in comments:
                if ((comment.get("user") or {}).get("login") != expected_actor
                        or (comment.get("body") or "").splitlines()[:1] != [f"Pending release of claim {args.claim_comment}"]):
                    continue
                prior = github_plan_release.pending_receipt_for(comment)
                if prior is None or (prior["evidence_url"] == receipt["evidence_url"]
                                     and prior["evidence_updated_at"] == receipt["evidence_updated_at"]):
                    result = comment_result(comment)
                    release_state = "pending"
                    raise PlanError("Prior pending attempt requires freshly verified closure and a refreshed --evidence-comment attestation")
        if args.dry_run:
            emit({"ok": True, "dry_run": True, "release_body": body, "related_ids": args.related_claim_comment,
                  "release_state": "confirmed" if existing is not None else "not_started",
                  "release_comment": comment_result(existing)["comment"] if existing is not None else {},
                  "session_coverage": inventory.get("session_coverage"), "completed_steps": completed})
            return
        if existing is not None:
            result = comment_result(existing)
            completed.append("reuse_confirmed_release")
        else:
            pending_body = github_plan_release.pending_body(body)
            release_state = "pending_unconfirmed"
            result = github_comment_core.comment("issue", issue["number"], pending_body, repo=issue["repo"],
                gh_cmd=gh_cmd, operation=CURRENT_OPERATION, expected_actor=expected_actor, completed_steps=completed, failed_step="post_release", dedupe_body=True)
            release_state = "pending"
            completed.append("post_release")
            issue, comments, inventory, fresh_body, related = preflight()
            release_id = result["comment"]["id"]
            pending = next((c for c in comments if c.get("id") == release_id), None)
            if pending is None or not (pending.get("body") or "").startswith(pending_body) or fresh_body != body:
                raise PlanError("Release readback changed; preserve records and inspect before any retry")
            completed.append("release_readback")
            # Publish authority only after full post-write preflight, through
            # the maintained guarded, single-attempt exact-comment PATCH.
            release_state = "confirmation_unconfirmed"
            result = github_comment_core.comment("issue", issue["number"], body, repo=issue["repo"],
                edit_comment=release_id, expected_body=pending["body"], expected_updated_at=pending["updated_at"],
                gh_cmd=gh_cmd, operation=CURRENT_OPERATION, expected_actor=expected_actor,
                completed_steps=completed, failed_step="confirm_release")
            completed.append("confirm_release")
        release_state = "confirmed"
        related_results = []
        for comment in related:
            related_results.append(github_comment_core.comment("issue", issue["number"],
                f"Released claim {comment['id']}\n\nOwnership follow-up of claim {args.claim_comment}; closed-session release {result['comment']['url']}.",
                repo=issue["repo"], gh_cmd=gh_cmd, operation=CURRENT_OPERATION, expected_actor=expected_actor,
                completed_steps=completed, failed_step="release_related_ownership", dedupe_body=True))
        emit({"ok": True, "release_comment": result["comment"], "related_releases": related_results,
              "release_state": release_state,
              "source_claim": args.claim_comment, "session_coverage": inventory.get("session_coverage"),
              "completed_steps": completed, "exclusive_lock": False})
    except PlanError as exc:
        exc.payload.update(completed_steps=completed, release_comment=result.get("comment", {}),
                           release_state=release_state, release_recovery_action=recovery_action())
        raise
    except github_comment_core.CommentError as exc:
        raise PlanError(str(exc), failure=exc.failure, api_result=exc.api_result,
            payload={**exc.payload, "completed_steps": completed, "release_comment": result.get("comment", {}),
                     "release_state": release_state, "release_recovery_action": recovery_action()}) from exc
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        raise PlanError(str(exc), payload={"completed_steps": completed, "release_comment": result.get("comment", {}),
            "release_state": release_state, "release_recovery_action": recovery_action(),
            "release_refusal_scope": github_plan_release.ISSUE_REFUSAL_SCOPE if isinstance(exc, github_plan_release.IssueReleaseRefusal) else "session_or_unknown"}) from exc


def cmd_release(args: argparse.Namespace) -> None:
    """Release this session's own claim with the exact directive its consumers read."""
    repo = default_repo(args.repo)
    args = claim_reference(args)
    actor, gh_cmd, expected_actor = comment_route()
    if not expected_actor:
        raise PlanError("Release requires configured automation auth, the claim's own identity")
    agent = github_agent.running_agent(getattr(args, "agent", None))
    native = github_agent.native_sessions(agent)
    if native and args.session not in native.values():
        found = ", ".join(f"{name}={value}" for name, value in native.items())
        raise ClassifiedPlanError("session_not_native",
            f"--session {args.session} is not this session's native ID ({found}); release only your own claim.",
            payload={"native_sessions": native, "running_agent": agent})
    handoff = read_body(argparse.Namespace(body_file=args.handoff_file)).strip() if args.handoff_file else ""
    body = f"Released claim {args.claim_comment}" + (f"\n\n{handoff}" if handoff else "")
    # The handoff is narrative only: it can neither restate nor forge release authority.
    if (github_plan_claim.released_claim_id(body) != args.claim_comment
            or any(marker in body for marker in (github_plan_release.RELEASE_MARKER, github_plan_release.PENDING_MARKER))
            or github_plan_claim.records(body)):
        raise PlanError("Handoff must be narrative; it cannot carry a release receipt or ownership record")
    completed = []

    def snapshot():
        record, _status, comments, _can_update = claim_snapshot(args.issue, repo, allow_closed=True)
        source = next((c for c in comments if c.get("id") == args.claim_comment), None)
        records = github_plan_claim.records((source or {}).get("body") or "")
        if (source is None or (source.get("user") or {}).get("login") != expected_actor
                or len(records) != 1 or records[0]["session"] != args.session):
            raise PlanError("The claim comment is not this session's own structured claim on this record; "
                            "an ended session's claim uses release-claim")
        confirmed = next((c for c in comments if c.get("id") != source["id"]
                          and github_plan_release.exact_source_release(source["id"], c, comments)), None)
        return record, confirmed

    issue, existing = snapshot()
    completed.append("release_preflight")
    if existing is not None:
        emit({"ok": True, "release_state": "already_released", "source_claim": args.claim_comment,
              "release_comment": {"id": existing["id"], "url": existing.get("html_url")}, "completed_steps": completed})
        return
    if args.dry_run:
        emit({"ok": True, "dry_run": True, "release_body": body, "release_state": "not_started",
              "source_claim": args.claim_comment, "completed_steps": completed})
        return
    result = github_comment_core.comment("pr" if "pull_request" in issue else "issue", issue["number"], body,
        repo=issue["repo"], gh_cmd=gh_cmd, operation=CURRENT_OPERATION, expected_actor=expected_actor,
        completed_steps=completed, failed_step="post_release", dedupe_body=True)
    completed.append("post_release")
    _issue, existing = snapshot()
    if existing is None or existing["id"] != result["comment"]["id"]:
        raise PlanError("Release readback is not an exact release of this claim; inspect the record before any retry",
                        payload={"release_comment": result["comment"], "completed_steps": completed})
    completed.append("release_readback")
    emit({"ok": True, "actor": actor, "release_state": "confirmed", "source_claim": args.claim_comment,
          "release_comment": {"id": existing["id"], "url": existing.get("html_url")}, "completed_steps": completed})


def claim_reference(args: argparse.Namespace) -> argparse.Namespace:
    """Accept a canonical PR URL on claim/release without broadening issue commands."""
    match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9]\d*)", args.issue.strip())
    return argparse.Namespace(**{**vars(args), "issue": f"{match[1]}#{match[2]}"}) if match else args


def cmd_claim(args: argparse.Namespace) -> None:
    """One explicit claim attempt; released work uses --resume-from ORIGINAL."""
    claim_once(claim_reference(args))


def claim_once(args: argparse.Namespace) -> None:
    with github_plan_closure.admission():
        _claim_once(args)


def _claim_once(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    issue_repo, number = issue_ref(args.issue, repo)
    claim = {key: getattr(args, key) for key in ("worker", "session", "branch")}
    for key, value in claim.items():
        if not value or re.search(r"\s|[<>]", value):
            raise PlanError(f"Claim {key} must be a nonempty single-line token")
    if not args.next_action.strip() or any(c in args.next_action for c in "\n\r"):
        raise PlanError("Claim next action must be a nonempty single line")
    if args.wait_resolved is not None and (not args.wait_resolved.strip() or any(c in args.wait_resolved for c in "\n\r")):
        raise PlanError("Wait resolution must record existing evidence on one line")
    override_input = getattr(args, "agent_override", None)
    if override_input is not None and (not override_input.strip() or any(c in override_input for c in "\n\r")):
        raise PlanError("Agent override must record the Director decision on one nonempty line")
    if subprocess.run(["git", "check-ref-format", "--branch", args.branch],
                      capture_output=True).returncode:
        raise PlanError("Claim branch is not a valid Git branch")
    admission = getattr(args, "admission", None)
    admission_links = list(dict.fromkeys(getattr(args, "admission_link", None) or []))
    admission_unlinked = getattr(args, "admission_unlinked", None)
    if (admission_links or admission_unlinked is not None) and not admission:
        raise PlanError("--admission-link and --admission-unlinked require --admission")
    admission_targets = {link: github_plan_claim.admission_link_target(link) for link in admission_links}
    if None in admission_targets.values():
        raise PlanError("--admission-link must be https://github.com/OWNER/REPO/issues/N or "
                        "https://github.com/OWNER/REPO/pull/N, optionally followed by #issuecomment-ID "
                        "(N and ID are positive integers)")
    if admission_unlinked is not None and (admission != "milestone" or not admission_unlinked.strip()
                                           or any(c in admission_unlinked for c in "\n\r")):
        raise PlanError("--admission-unlinked records one nonempty line for milestone admission")
    # Spellings of one issue, PR or comment cite one occurrence; keep the first as written.
    first_spellings: dict[tuple[str, int, int | None], str] = {}
    for link, target in admission_targets.items():
        first_spellings.setdefault(target, link)
    admission_links = list(first_spellings.values())
    if admission == "repeat-stop" and len(admission_links) < 2:
        raise PlanError("Repeat-stop admission needs both occurrences: pass --admission-link once for each")
    target_repo = issue_repo
    handoff_id = None
    retained_mode = False
    selection = None
    claim["claimed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    completed: list[str] = []
    claim_comment: dict[str, Any] = {}
    inventory: dict[str, Any] = {}
    previous_status = ""
    record_kind = "issue"
    retained_waits: dict[int, str] | None = None
    previous_wait_labels: set[str] = set()
    previous_wait = ""
    planned_wait = ""
    ended_owners: list[dict[str, Any]] = []
    resume_source_number: int | None = None
    retained_ended_sources: set[str] = set()
    planning_checkout = getattr(args, "planning_checkout", None)
    config = load_config(issue_repo, checkout=planning_checkout) if planning_checkout else load_config(issue_repo)
    phase_records = {}
    phase_peers = []
    phase_owners = set()
    phase_family_error = None
    phase = {}

    def read_record(record_number, record_repo, *, allow_closed=False):
        key = (record_repo.casefold(), int(record_number))
        if key not in phase_records:
            if phase.get("observed"):
                raise PlanError("Claim phase requires an unprepared ownership record")
            phase_records[key] = claim_snapshot(str(record_number), record_repo, allow_closed=allow_closed)
        snapshot = phase_records[key]
        if snapshot[0].get("state") not in ({"open", "closed"} if allow_closed else {"open"}):
            raise PlanError("Claim requires an open issue or pull request")
        return snapshot

    def retain_released_sources(sessions, observed) -> None:
        # Unknown/non-Codex IDs keep their existing authored-release route.
        # Every positively proved native source must instead reach admission;
        # admit_sources refuses if its required lease has been lost.
        retained_ended_sources.update(session for session in sessions if session != claim["session"]
            and observed.get("session_states", {}).get(session, {}).get("state") == "ended")

    def observe_claim_sources():
        import github_plan_sessions
        owners = [(snapshot[0].get("repo", owner_repo), owner_number, snapshot[2])
                  for (owner_repo, owner_number), snapshot in phase_records.items()
                  if (owner_repo, owner_number) in phase_owners]
        contexts = tuple(context for owner_repo, owner_number, discussion in owners
                         if (owner_repo.casefold(), owner_number) not in phase["unqualified_targets"]
                         for context in github_plan_release._released_source_contexts(discussion))
        sessions, proof_sessions = set(), set()
        for _, _, discussion in owners:
            requested, required = github_plan_claim.observation_inputs(discussion)
            sessions.update(requested)
            proof_sessions.update(required)
        def readback(context):
            owner = next((item for item in owners if any(c.get("id") == context.source_id for c in item[2])), None)
            if owner is None:
                raise PlanError("Released source has no canonical ownership record")
            owner_repo, owner_number, _ = owner
            current_record = owner_repo.casefold() == issue_repo.casefold() and owner_number == number
            _, _, fresh, _ = claim_snapshot(str(owner_number), owner_repo, allow_closed=not current_record)
            if context not in github_plan_release._released_source_contexts(fresh):
                raise ClassifiedPlanError("claim_conflict", "Released source authority changed during native verification",
                    payload={"competing_evidence": [{"source": "released_source_changed", "id": context.source_id,
                                                      "session": context.session}]})
        with github_plan_sessions._source_readback(readback, contexts):
            observation = github_plan_sessions.observe(sessions, proof_sessions)
        for scope in ("inventory", "planning_inventory"):
            if scope in phase:
                phase[scope] = github_plan_claim.observed_inventory(phase[scope], observation)
        phase["observed"] = True

    def ownership_snapshot(current_status: str, current_comments: list[dict[str, Any]]):
        nonlocal inventory, ended_owners
        inventory = phase["inventory"]
        ended_owners = github_plan_claim.ended_ownership(current_comments, inventory)
        retain_released_sources((entry["session"] for entry in ended_owners), inventory)
        branches = set(inventory["local_branches"]) | set(inventory["remote_branches"]) | {t["branch"] for t in inventory["worktrees"]}
        if any(entry["branch"] == claim["branch"] and entry["branch"] in branches and entry["claim_id"] != args.resume_from for entry in ended_owners):
            raise PlanError("Ended ownership requires a new task branch; preserve the retained source branch and worktree")
        conflicts, owned = github_plan_claim.discussion_evidence(
            current_status, current_comments, claim, resume_from=args.resume_from,
            ended_claims={entry["claim_id"] for entry in ended_owners})
        source_sessions = {record["session"] for c in current_comments
                           for record in github_plan_claim.records(c.get("body") or "")}
        conflicts.extend({"source": peer.get("source", "native_session"), "session": peer["sessionId"],
                          "certainty": "visible_in_native_inventory",
                          **({"error": peer["error"]} if "error" in peer else {})}
                         for peer in inventory["sessions"]
                         if peer.get("sessionId") in source_sessions and peer.get("sessionId") != claim["session"])
        if inventory.get("session_coverage", {}).get("codex_claims", {}).get("observed_source_incomplete"):
            conflicts.extend({"source": "native_session_inventory", "branch": entry["branch"], "certainty": "incomplete"}
                             for entry in ended_owners if entry["branch"] in branches)
        return conflicts, owned

    def check_wait(waiting_issue: dict[str, Any], waiting_status: str, *, target: bool = False) -> bool:
        wait = waiting_issue["claim_wait"]
        status_state = next_plan_status(waiting_issue, phase["target_config"] if target else config)
        has_wait = bool(wait["state"] != "clear" or status_state in {"stale", "done"})
        if has_wait and not args.wait_resolved:
            raise ClassifiedPlanError("claim_wait_unresolved",
                "Review the structured wait with gh-plan.py wait. For a missing legacy record, read the full "
                "issue/discussion once and record its waits or verified absence; --wait-resolved records existing resolution evidence.",
                payload={"structured_wait": wait, "previous_current_status": waiting_status,
                         "review_required": wait["state"] == "review"})
        return has_wait

    def check_issue_holds(checked_issue: dict[str, Any]) -> None:
        expected = previous_wait
        observed_holds = github_plan_wait.marker(checked_issue["claim_wait"])
        wait_labels = set(normalize_labels(checked_issue.get("labels"))) & {config["labels"][key] for key in ("waiting", "blocked")}
        if observed_holds not in {expected, planned_wait}:
            raise PlanError("Structured wait changed during claim readback; verify the new record before recovery")
        if retained_mode and (observed_holds != expected or wait_labels != previous_wait_labels):
            raise PlanError("Issue holds changed during retained-handoff readback; verify the new evidence before recovery")

    def resume_source(
        current_comments: list[dict[str, Any]], current_status: str,
    ) -> tuple[int, list[dict[str, Any]], str]:
        """Resolve proof on this record or its native parent, never a prose link."""
        nonlocal resume_source_number
        if resume_source_number is None and any(c.get("id") == args.resume_from for c in current_comments):
            resume_source_number = number
        if resume_source_number == number or record_kind != "issue":
            return number, current_comments, current_status
        parent = read_next_parent(issue_repo, number)
        if (parent is None or parent["repo"].casefold() != issue_repo.casefold()
                or parent["number"] == number):
            raise ClassifiedPlanError("claim_native_parent_required", "Cross-record resume requires a native parent in the claimed issue's repository")
        if resume_source_number is not None and parent["number"] != resume_source_number:
            raise ClassifiedPlanError("claim_native_parent_changed", "Native resume parent changed during claim readback")
        source, source_status, source_comments, _ = read_record(
            str(parent["number"]), issue_repo, allow_closed=True)
        if "pull_request" in source:
            raise ClassifiedPlanError("claim_native_parent_invalid", "Native resume parent must be an issue")
        parent_number: int = parent["number"]
        resume_source_number = parent_number
        return parent_number, source_comments, source_status

    def check_peer_ownership(
        peer_repo: str, peer_number: int, peer_status: str, peer_comments: list[dict[str, Any]],
        source_inventory: dict[str, Any],
    ) -> None:
        peer_records = github_plan_claim.records(peer_status) + [
            record for c in peer_comments for record in github_plan_claim.records(c.get("body") or "")]
        peer_inventory: dict[str, Any] = source_inventory
        # The requester can occupy a released source worktree. Exclude only its
        # native identity, including observations added by observe_sessions;
        # foreign sessions and unreleased discussion ownership still compete.
        peer_inventory = {**peer_inventory,
                          "sessions": [peer for peer in peer_inventory["sessions"]
                                       if peer.get("sessionId") != claim["session"]],
                          "session_states": {session: state for session, state in peer_inventory.get("session_states", {}).items()
                                             if session != claim["session"]}}
        ended = github_plan_claim.ended_ownership(peer_comments, peer_inventory)
        peer_conflicts, peer_owned = github_plan_claim.discussion_evidence(
            peer_status, peer_comments, claim, ended_claims={entry["claim_id"] for entry in ended})
        competing = list(peer_conflicts)
        competing.extend({"source": "native_child_claim", "record": record} for record in peer_owned)
        sessions = {record["session"] for record in peer_records}
        branches = {record["branch"] for record in peer_records}
        paths = [pathlib.Path(tree["path"]).resolve() for tree in peer_inventory["worktrees"]
                 if tree["branch"] in branches]
        competing.extend({"source": "native_child_session", "session": peer["sessionId"],
                          **({"error": peer["error"]} if "error" in peer else {})}
                         for peer in peer_inventory["sessions"]
                         if peer.get("sessionId") in sessions or any(
                             pathlib.Path(peer.get("cwd") or "/").resolve().is_relative_to(path) for path in paths))
        if peer_inventory.get("session_coverage", {}).get("codex_claims", {}).get("observed_source_incomplete"):
            present = (set(peer_inventory["local_branches"]) | set(peer_inventory["remote_branches"])
                       | {tree["branch"] for tree in peer_inventory["worktrees"]})
            competing.extend({"source": "native_session_inventory", "branch": branch, "certainty": "incomplete"}
                             for branch in sorted(branches & present))
        if competing:
            refuse(competing, owner_comments=peer_comments, owner_repo=peer_repo, owner_number=peer_number)
        retain_released_sources((entry["session"] for entry in ended), peer_inventory)

    def admit_observed_sources() -> None:
        try:
            github_plan_closure.admit_sources(retained_ended_sources)
        except (OSError, ValueError) as error:
            raise ClassifiedPlanError("claim_closure_unavailable",
                "Source ownership could not be committed; preserve it and retry this claim after the owning operation ends",
                payload={"closure_error": str(error)}) from error

    def check_handoff_successors(
        source_number: int, *,
        source_claim_id: int | None = None, source_handoff_url: str | None = None,
        source_comments: list[dict[str, Any]] | None = None,
        source_repo: str | None = None, source_issue: dict[str, Any] | None = None,
        source_status: str | None = None,
        traversal: dict[tuple[str, int], set[str]] | None = None,
    ) -> None:
        """Released intermediates still connect the complete native handoff family."""
        source_repo = issue_repo if source_repo is None else source_repo
        if traversal is None:
            traversal = {}
            if source_claim_id is None:
                source_claim_id = args.resume_from
            if source_handoff_url is None and handoff_id is not None:
                source_handoff_url = github_plan_claim.handoff_url(source_repo, source_number, handoff_id)
        directions = traversal.setdefault((source_repo.casefold(), source_number), set())
        if "children" in directions:
            return
        directions.add("children")
        if source_issue is None:
            source_issue, source_status, source_comments, _ = read_record(
                str(source_number), source_repo, allow_closed=True)
        check_inherited_sources(source_comments or [], source_status or "",
                                node_repo=source_repo, node_number=source_number, node_issue=source_issue,
                                traversal=traversal)
        _, children = collect_paged_rest_items(
            f"/repos/{source_repo}/issues/{source_number}/sub_issues", query={},
            bucket="rest_core", step_prefix="claim_handoff_children")
        _, history = collect_paged_rest_items(
            f"/repos/{source_repo}/issues/{source_number}/timeline", query={},
            bucket="rest_core", step_prefix="claim_handoff_child_history")
        for event in history:
            if event.get("event") != "sub_issue_added":
                continue
            children.append(read_handoff_history_child(event, source_repo))
        source_ids = ({str(source_claim_id)} if source_claim_id is not None else set()) | {
            str(comment["id"]) for comment in source_comments or []
            if type(comment.get("id")) is int and comment["id"] > 0
            and github_plan_claim.records(comment.get("body") or "")}
        parent_handoff = re.compile(
            rf"https://github\.com/{re.escape(source_repo)}/issues/{source_number}#issuecomment-[1-9]\d*", re.IGNORECASE)

        def uses_source(record: dict[str, Any]) -> bool:
            source_id, handoff_url = record.get("resume_from"), record.get("retained_handoff")
            if isinstance(source_id, str) and source_id in source_ids:
                return True
            if not isinstance(handoff_url, str):
                return False
            if handoff_url == source_handoff_url:
                return True
            return parent_handoff.fullmatch(handoff_url) is not None

        seen_children = set()
        for child in children:
            related = compact_relationship_issue(child, "handoff child")
            peer_repo, peer_number = related["repo"], related["number"]
            child_key = (peer_repo.casefold(), peer_number)
            if child_key in seen_children:
                continue
            seen_children.add(child_key)
            peer_issue, peer_status, peer_comments, _ = read_record(str(peer_number), peer_repo, allow_closed=True)
            peer_records = github_plan_claim.records(peer_status) + [
                record for c in peer_comments for record in github_plan_claim.records(c.get("body") or "")]
            claimed_node = child_key == (issue_repo.casefold(), number)
            # Taking this source will connect the claimed node after posting.
            # Inspect its existing descendants now, before any claim write.
            if not claimed_node and not any(uses_source(record) for record in peer_records):
                continue
            phase_owners.add(child_key)
            # Same-record resumes need not repeat the original handoff metadata,
            # and can move to a fresh branch. Once this issue holds the source,
            # its full ownership must end before a sibling can take the source.
            if not claimed_node:
                phase_peers.append((peer_repo, peer_number, peer_status, peer_comments))
            # The claimed record has independent ownership checks, but may itself
            # have successors that must remain visible through this ancestor.
            check_handoff_successors(peer_number, source_repo=peer_repo,
                                     source_issue=peer_issue, source_status=peer_status,
                                     source_comments=peer_comments, traversal=traversal)

    def check_inherited_sources(
        current_comments: list[dict[str, Any]], current_status: str, *,
        node_repo: str | None = None, node_number: int | None = None,
        node_issue: dict[str, Any] | None = None,
        traversal: dict[tuple[str, int], set[str]] | None = None,
    ) -> None:
        """Later work on a child cannot race a holder of its original source."""
        node_repo = issue_repo if node_repo is None else node_repo
        node_number = number if node_number is None else node_number
        node_issue = read_record(node_number, node_repo, allow_closed=True)[0] if node_issue is None else node_issue
        traversal = {} if traversal is None else traversal
        directions = traversal.setdefault((node_repo.casefold(), node_number), set())
        if "ancestors" in directions:
            return
        directions.add("ancestors")
        checked = set()
        for record in github_plan_claim.records(current_status) + [
                record for c in current_comments for record in github_plan_claim.records(c.get("body") or "")]:
            handoff_url = record.get("retained_handoff")
            if not handoff_url:
                continue
            match = (re.fullmatch(
                r"https://github\.com/([^/\s]+/[^/\s]+)/issues/([1-9]\d*)#issuecomment-([1-9]\d*)", handoff_url)
                if isinstance(handoff_url, str) else None)
            if not match:
                raise ClassifiedPlanError("claim_resume_lineage_invalid", "Retained source lineage is malformed; preserve its ownership evidence")
            parent_repo, parent_number = match.group(1), int(match.group(2))
            if parent_repo.casefold() == node_repo.casefold() and parent_number == node_number:
                continue  # Same-record metadata is not inherited-parent proof.
            if parent_repo.casefold() != node_repo.casefold() and (args.resume_from or handoff_id):
                current_id = node_issue.get("id")
                if type(current_id) is int and current_id > 0:
                    _, candidate = api_json("GET", f"/repos/{parent_repo}/issues/{parent_number}",
                                            bucket="rest_core", failed_step="claim_lineage_identity")
                    if type(candidate.get("id")) is int and candidate["id"] == current_id:
                        continue  # Renamed/transferred self-reference, proved by immutable issue ID.
            source_id = record.get("resume_from")
            if not isinstance(source_id, str) or not source_id.isdecimal() or int(source_id) <= 0:
                raise ClassifiedPlanError("claim_resume_lineage_invalid", "Retained source lineage is malformed; preserve its ownership evidence")
            if parent_repo.casefold() != issue_repo.casefold():
                if not args.resume_from and not handoff_id:
                    # Foreign historical handoffs cannot supply native lineage
                    # for independent fresh work. Explicit resume still refuses.
                    continue
                raise ClassifiedPlanError("claim_resume_lineage_invalid", "Retained source lineage must stay in the claimed issue's repository")
            key = (parent_number, source_id, handoff_url)
            if key in checked:
                continue
            checked.add(key)
            parent_issue, parent_status, parent_comments, _ = read_record(str(parent_number), parent_repo, allow_closed=True)
            phase_owners.add((parent_repo.casefold(), parent_number))
            if (parent_repo.casefold(), parent_number) != (issue_repo.casefold(), number):
                phase_peers.append((parent_repo, parent_number, parent_status, parent_comments))
            check_handoff_successors(parent_number, source_claim_id=int(source_id),
                                     source_handoff_url=handoff_url, source_comments=parent_comments,
                                     source_repo=parent_repo, source_issue=parent_issue, source_status=parent_status,
                                     traversal=traversal)

    def handoff_record(pull):
        if phase.get("observed"):
            raise PlanError("Claim phase requires an unprepared retained PR")
        key = (target_repo.casefold(), pull["number"])
        cached = phase_records.get(key)
        if cached is not None:
            target, _, target_comments, _ = cached
        else:
            _, target = get_issue(str(pull["number"]), target_repo)
        if target.get("state") != pull.get("state") or "pull_request" not in target:
            raise PlanError("Refresh handoff PR changed or closed during ownership preflight")
        sections, _ = read_plan_sections(target)
        if cached is None:
            _, target_comments = collect_paged_rest_items(
                f"/repos/{target_repo}/issues/{pull['number']}/comments", query={},
                bucket="rest_core", step_prefix="refresh_comments",
            )
            target_comments = github_plan_release.verified_comments(target_comments, _claim_release_evidence)
            phase_records[key] = (target, sections.get("Current Status", ""), target_comments, False)
        phase_owners.add(key)
        return target, sections, target_comments

    def prepare_handoff(source_info, open_pulls):
        nonlocal selection, handoff_id, retained_mode
        if not args.resume_from:
            return None
        source_number, source_comments, source_status = source_info
        try:
            source = github_plan_claim.released_source(source_comments, args.resume_from)
            # Canonical native releases are freshly validated again by the
            # observer's owner callback. Equivalent replacement receipts may
            # attest a later end; ordinary worker releases keep their exact ID.
            release_identity = source["release"]["id"]
            if ("_release_evidence" in source["release"] and any(
                    context.source_id == source["comment"]["id"]
                    for context in github_plan_release._released_source_contexts(source_comments))):
                release_identity = "canonical-native-release"
            projection = (issue_repo.casefold(), source_number, source["comment"]["id"],
                          source["author"], source["record"], release_identity)
            if selection is not None and projection != selection["source"]:
                raise github_plan_claim.ClaimProofError("claim_resume_source_changed",
                    "Selected released source changed during claim readback")
            phase["source"] = source
        except ValueError as error:
            phase["source_error"] = error
            return None
        if record_kind == "pr":
            if selection is None:
                selection = {"source": projection, "mode": "plain", "handoff": None, "next_pr": None}
            return None
        candidates = github_plan_claim.handoff_candidates(source_comments, source)
        target_pulls = list(open_pulls)
        named = set().union(*(github_plan_claim.handoff_pr_numbers(
            candidate.get("body") or "", issue_repo=issue_repo, target_repo=target_repo) for candidate in candidates))
        # Fetch only named PRs absent from the open inventory, rather than the
        # repository's complete closed history. A non-PR issue reference is 404.
        for missing in sorted(named - {p["number"] for p in target_pulls}):
            try:
                _, retained_pull = api_json("GET", f"/repos/{target_repo}/pulls/{missing}",
                                            bucket="rest_core", failed_step="refresh_retained_pr")
            except PlanError as retained_error:
                if (retained_error.api_result or {}).get("status") != 404:
                    raise
                continue
            target_pulls.append(retained_pull)
        prepared = {"source": source_info, "pulls": target_pulls,
                    "permitted": set(), "error": None, "targets": {}}
        _, current_status, current_comments, _ = phase_records[(issue_repo.casefold(), number)]
        candidate_owners = {}
        def usable(candidate: dict[str, Any]) -> bool:
            # Selection uses the existing artifact rules on raw checkout facts.
            # Fresh native/peer authority remains with the phase observer and
            # admission checks; this predicate grants no ownership.
            raw_inventory = phase["inventory"]
            candidate_claim, own = candidate_owners[candidate["url"]]
            scopes = {(issue_repo.casefold(), number): (issue_repo, current_comments, current_status),
                      (issue_repo.casefold(), source_number): (issue_repo, source_comments, source_status)}
            scopes.update({(target_repo.casefold(), pull["number"]): (target_repo, [], "")
                           for pull in candidate["qualified"] if pull.get("state") == "open"})
            for (_, checked_number), (checked_repo, owner_comments, owner_status) in scopes.items():
                ended = github_plan_claim.ended_ownership(owner_comments, raw_inventory)
                conflicts = github_plan_claim.artifact_evidence(
                    raw_inventory, target_pulls, checked_number, candidate_claim, own_record=bool(own),
                    retained=source["record"]["branch"], retained_branches=candidate["permitted"],
                    retained_repo=target_repo, repo=checked_repo, inventory_repo=target_repo,
                    ended_branches=github_plan_claim.ended_artifact_branches(ended, owner_comments, target_pulls),
                    recorded_branches=github_plan_claim.recorded_claim_branches(owner_status, owner_comments))
                if any(conflict["source"] in {"local_branches", "remote_branches", "worktree", "open_pr",
                                               "closed_pr_artifacts"} for conflict in conflicts):
                    return False
            return True
        try:
            owned_ids = []
            if selection is None or selection["handoff"] is not None:
                for candidate in candidates:
                    url = github_plan_claim.handoff_url(issue_repo, source_number, candidate["id"])
                    candidate_claim = {**claim, "resume_from": str(args.resume_from), "retained_handoff": url}
                    _, own = github_plan_claim.discussion_evidence("", current_comments, candidate_claim)
                    candidate_owners[url] = (candidate_claim, own)
                    if any(record.get("_legacy") != "yes" for record in own):
                        owned_ids.append(candidate["id"])
            # A retry keeps its published lineage even if sibling cleanup makes
            # an older handoff usable. Ownership and admission still run below.
            selected_id = selection["handoff"] if selection else owned_ids[0] if len(owned_ids) == 1 else None
            handoff = (github_plan_claim.retained_handoff(source, candidates, target_pulls,
                issue_repo=issue_repo, issue_number=source_number, target_repo=target_repo,
                selected_id=selected_id, usable=usable)
                if selection is None or selection["handoff"] is not None else None)
            record = source["record"]
            source_open = any(pull.get("state") == "open" and (pull.get("head") or {}).get("ref") == record["branch"]
                              for pull in open_pulls)
            if selection is None:
                mode = ("retained" if handoff is not None and (source_number != number
                        or issue_repo.casefold() != target_repo.casefold() or claim["branch"] != record["branch"]
                        or any(pull.get("state") == "open" for pull in handoff["qualified"])) else "plain")
                next_pr = None
                if mode == "retained" and claim["branch"] in handoff["permitted"]:
                    matching = [pull for pull in handoff["qualified"] if pull.get("state") == "open"
                                and pull["head"]["ref"] == claim["branch"]]
                    bindings = github_agent.native_sessions(agent)
                    if (record["worker"] == claim["worker"] and record["session"] == claim["session"]
                            and bindings and set(bindings.values()) == {claim["session"]}
                            and claim["branch"] != record["branch"] and len(matching) == 1):
                        mode, next_pr = "next", matching[0]["number"]
                selection = {"source": projection, "mode": mode,
                             "handoff": handoff["comment"]["id"] if handoff is not None else None,
                             "next_pr": next_pr}
            retained_mode = selection["mode"] != "plain"
            handoff_id = selection["handoff"]
            if not retained_mode:
                if source_number != number or issue_repo.casefold() != target_repo.casefold() or source_open:
                    raise github_plan_claim.ClaimProofError("claim_handoff_pr_required",
                        "Released work requires a qualified source-authored retained-PR handoff")
                return None
            if handoff is None:
                raise github_plan_claim.ClaimProofError("claim_handoff_pr_required", "Released retained work has no qualified handoff")
            claim["resume_from"] = str(args.resume_from)
            claim["retained_handoff"] = handoff["url"]
            prepared.update(permitted=handoff["permitted"],
                            open_conflicts=handoff["open_conflicts"])
            if selection["mode"] == "next":
                matching = [pull for pull in handoff["qualified"] if pull.get("state") == "open"
                            and pull["head"]["ref"] == claim["branch"]]
                bindings = github_agent.native_sessions(agent)
                if (len(matching) != 1 or matching[0]["number"] != selection["next_pr"]
                        or not bindings or set(bindings.values()) != {claim["session"]}):
                    raise github_plan_claim.ClaimProofError("claim_next_pr_changed",
                        "Selected next branch requires its exact qualified open PR and native owner")
        except ValueError as error:
            # Keep the existing current-ownership decision ahead of local
            # handoff qualification. Unqualified PRs supply no source context.
            prepared["error"] = error
            return prepared
        qualified_ids = {pull["number"] for pull in handoff["qualified"]}
        phase["retained_pr_numbers"] = {pull["number"] for pull in handoff["qualified"]
                                        if pull.get("state") == "open"}
        for pull in target_pulls:
            if pull["number"] not in qualified_ids:
                if pull["number"] not in handoff["named"] or pull.get("state") != "closed":
                    continue
                # Closed historical research still has local ownership and
                # native vetoes, but cannot lend canonical closure authority.
                phase["unqualified_targets"].add((target_repo.casefold(), pull["number"]))
            target, sections, target_comments = handoff_record(pull)
            if "claim_wait" not in target:
                target["claim_wait"] = current_wait(target, target_comments, target_repo)
            prepared["targets"][pull["number"]] = (target, sections, target_comments)
        return prepared

    def prepare_phase(snapshot):
        nonlocal phase_family_error
        phase_records.clear()
        phase_peers.clear()
        phase_owners.clear()
        phase.clear()
        phase["unqualified_targets"] = set()
        phase["source_error"] = None
        phase_family_error = None
        phase_records[(issue_repo.casefold(), number)] = snapshot
        current_issue, current_status, current_comments, _ = snapshot
        source_info = resume_source(current_comments, current_status) if args.resume_from else (number, current_comments, current_status)
        phase_owners.update(((issue_repo.casefold(), number), (issue_repo.casefold(), source_info[0])))
        _, phase["open_pulls"] = collect_paged_rest_items(
            f"/repos/{target_repo}/pulls", query={"state": "open"},
            bucket="rest_core", step_prefix="claim_pr_readback",
        )
        phase["inventory"] = github_plan_claim.local_inventory(target_repo, number)
        prepared = prepare_handoff(source_info, phase["open_pulls"])
        if record_kind == "issue":
            traversal = {}
            try:
                check_inherited_sources(current_comments, current_status,
                                        node_issue=current_issue, traversal=traversal)
                if handoff_id or args.resume_from:
                    source_number, source_comments, source_status = source_info
                    source_issue = read_record(source_number, issue_repo, allow_closed=True)[0]
                    check_handoff_successors(source_number, source_comments=source_comments,
                                             source_status=source_status, source_issue=source_issue, traversal=traversal)
            except ClassifiedPlanError as error:
                if error.code not in {"claim_resume_lineage_invalid", "claim_child_history_invalid"}:
                    raise
                # Stop at the invalid edge. Already collected peer ownership
                # still has its original precedence over that local predicate.
                phase_family_error = error
        if retained_mode:
            phase["target_config"] = load_config(target_repo)
            if issue_repo.casefold() != target_repo.casefold():
                handoff_checkout = getattr(args, "planning_checkout", None)
                config_path = repo_config_path(issue_repo) if not handoff_checkout else None
                planning_path = pathlib.Path(handoff_checkout) if handoff_checkout else config_path.parent.parent if config_path else None
                if planning_path is None:
                    raise PlanError("Cross-repository refresh requires a verified planning checkout; pass --planning-checkout PATH")
                phase["planning_inventory"] = github_plan_claim.local_inventory(issue_repo, number, cwd=planning_path)
                _, phase["planning_pulls"] = collect_paged_rest_items(
                    f"/repos/{issue_repo}/pulls", query={"state": "open"},
                    bucket="rest_core", step_prefix="refresh_planning_prs",
                )
        phase["blockers"] = []
        if record_kind != "pr":
            _, phase["blockers"] = collect_paged_rest_items(
                f"/repos/{issue_repo}/issues/{number}/dependencies/blocked_by",
                query={}, bucket="rest_core", step_prefix="claim_blockers",
            )
        phase["canonical_issue"] = None
        subject = next((pull for pull in phase["open_pulls"] if pull["number"] == number), None)
        if record_kind == "pr" and subject is not None:
            text = (subject.get("title") or "") + "\n" + (subject.get("body") or "")
            links = re.findall(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/([1-9]\d*)|(?<![\w/])([\w.-]+/[\w.-]+)#([1-9]\d*)|(?<![\w/])#([1-9]\d*)", text)
            for url_repo, url_number, qualified_repo, qualified_number, bare_number in links:
                linked_repo = url_repo or qualified_repo or issue_repo
                linked_number = int(url_number or qualified_number or bare_number)
                if linked_repo.casefold() == issue_repo.casefold() and linked_number == number:
                    continue
                if not github_plan_claim.pr_issue_reference(subject, linked_number, repo=linked_repo,
                                                            local_references=linked_repo.casefold() == issue_repo.casefold()):
                    continue
                _, linked = get_issue(str(linked_number), linked_repo)
                if "pull_request" not in linked:
                    phase["canonical_issue"] = f"https://github.com/{linked_repo}/issues/{linked_number}"
                    break
        return source_info, prepared

    def check_phase_peers(source_inventory):
        checked = set()
        for peer_repo, peer_number, peer_status, peer_comments in phase_peers:
            key = (peer_repo.casefold(), peer_number)
            if key not in checked:
                checked.add(key)
                check_peer_ownership(peer_repo, peer_number, peer_status, peer_comments, source_inventory)
        if phase_family_error is not None:
            raise phase_family_error

    def handoff_preflight(current_comments: list[dict[str, Any]], current_status: str, prepared) -> set[str]:
        nonlocal retained_waits
        if prepared is None:
            check_phase_peers(inventory)
            return set()
        if prepared["error"] is not None:
            raise prepared["error"]
        source_number, source_comments, source_status = prepared["source"]
        target_pulls, permitted = prepared["pulls"], prepared["permitted"]
        for ended in ended_owners:
            ended["pull_requests"] = [f"https://github.com/{target_repo}/pull/{p['number']}" for p in target_pulls
                                      if (p.get("head") or {}).get("ref") == ended["branch"]]
        if claim["branch"] in permitted and selection["mode"] != "next":
            raise PlanError("Retained handoff requires a new task branch, separate from every retained PR/source branch")
        if prepared["open_conflicts"]:
            refuse(prepared["open_conflicts"], owner_comments=source_comments, owner_number=source_number)
        observed_waits = {}
        released_target_sessions = set()
        session_owners = {}
        def retain_session_owner(record, owner_comments, owner_repo, owner_number):
            session_owners.setdefault(record["session"], {})[(owner_repo, owner_number)] = owner_comments
        for comment in source_comments:
            for record in github_plan_claim.records(comment.get("body") or ""):
                retain_session_owner(record, source_comments, issue_repo, source_number)
        for pull in target_pulls:
            if pull["number"] not in prepared["targets"]:
                continue
            target, sections, target_comments = prepared["targets"][pull["number"]]
            for comment in target_comments:
                for record in github_plan_claim.records(comment.get("body") or ""):
                    retain_session_owner(record, target_comments, target_repo, pull["number"])
            # PR status may name the canonical issue being claimed. Keep its
            # holder prose conservative rather than applying issue exclusions.
            competing, _ = github_plan_claim.discussion_evidence(sections.get("Current Status", ""), target_comments, claim)
            if competing:
                refuse(competing, owner_comments=target_comments, owner_repo=target_repo, owner_number=pull["number"])
            for _, record, _ in github_plan_claim.exact_released_sources(target_comments):
                released_target_sessions.add(record["session"])
                retain_session_owner(record, target_comments, target_repo, pull["number"])
            target_status = sections.get("Current Status", "")
            target_wait = target["claim_wait"]
            if check_wait(target, target_status, target=True):
                wait_labels = sorted(set(normalize_labels(target.get("labels"))) &
                                     {config["labels"][key] for key in ("waiting", "blocked", "stale", "done")})
                observed_waits[pull["number"]] = "Labels: " + ", ".join(wait_labels) + "\n" + github_plan_wait.marker(target_wait)
        if retained_waits is not None and observed_waits != retained_waits:
            raise PlanError("Retained PR waits changed during claim readback; verify the new evidence before recovery")
        retained_waits = observed_waits
        target_inventory = inventory
        source_ended = github_plan_claim.ended_ownership(source_comments, target_inventory)
        if source_number != number:
            # The parent's wait remains there; only its ownership is inherited.
            # Check its full discussion/status, not just the selected release.
            competing, _ = github_plan_claim.discussion_evidence(
                source_status, source_comments, claim, resume_from=args.resume_from,
                ended_claims={entry["claim_id"] for entry in source_ended})
            if competing:
                refuse(competing, owner_comments=source_comments, owner_number=source_number)
        released_target_sessions.update(record["session"] for _, record, _ in
                                         github_plan_claim.exact_released_sources(source_comments))
        source_session = github_plan_claim.records(next(c for c in source_comments if c.get("id") == args.resume_from)["body"])[0]["session"]
        if target_inventory.get("session_coverage", {}).get("codex_claims", {}).get("observed_source_incomplete"):
            present = set(target_inventory["local_branches"]) | set(target_inventory["remote_branches"]) | {t["branch"] for t in target_inventory["worktrees"]}
            uncertain = [{"source": "native_session_inventory", "branch": branch, "session": source_session, "certainty": "incomplete"}
                         for branch in sorted(permitted & present)]
            if uncertain:
                refuse(uncertain, owner_comments=source_comments, owner_number=source_number)
        live_sources = [{"source": peer.get("source", "source_session"), "session": peer["sessionId"],
                         "certainty": "visible_in_native_inventory",
                         **({"error": peer["error"]} if "error" in peer else {})}
                        for peer in target_inventory["sessions"]
                        if peer.get("sessionId") in session_owners.keys() | {source_session}
                        and peer.get("sessionId") != claim["session"]]
        if live_sources:
            owners = {}
            for source in live_sources:
                for owner, owner_comments in session_owners[source["session"]].items():
                    owners.setdefault(owner, (owner_comments, []))[1].append(source)
            refuse(live_sources, coordination=[holder
                for (owner_repo, owner_number), (owner_comments, sources) in owners.items()
                for holder in github_plan_claim.coordination(sources, owner_comments, owner_repo, owner_number)])
        retain_released_sources(released_target_sessions, target_inventory)
        check_phase_peers(target_inventory)
        _, own_comments = github_plan_claim.discussion_evidence("", current_comments, claim)
        for checked_number, checked_repo in dict.fromkeys(((number, issue_repo), (source_number, issue_repo))):
            checked_comments, checked_status, checked_ended = [], "", []
            if checked_repo.casefold() == issue_repo.casefold():
                if checked_number == source_number:
                    checked_comments, checked_status, checked_ended = source_comments, source_status, source_ended
                elif checked_number == number:
                    checked_comments, checked_status, checked_ended = current_comments, current_status, ended_owners
            competing = github_plan_claim.artifact_evidence(
                target_inventory, target_pulls, checked_number, claim, own_record=bool(own_comments),
                retained_branches=permitted, retained_repo=target_repo, repo=checked_repo,
                ended_branches=github_plan_claim.ended_artifact_branches(checked_ended, checked_comments, target_pulls),
                inventory_repo=target_repo,
                recorded_branches=github_plan_claim.recorded_claim_branches(checked_status, checked_comments),
            )
            if competing:
                refuse(competing, owner_comments=checked_comments, owner_repo=checked_repo, owner_number=checked_number)
        if issue_repo.casefold() != target_repo.casefold():
            planning_inventory = phase["planning_inventory"]
            check_phase_peers(planning_inventory)
            planning_pulls = phase["planning_pulls"]
            competing = github_plan_claim.artifact_evidence(
                planning_inventory, planning_pulls, number, claim, own_record=bool(own_comments), repo=issue_repo,
                recorded_branches=github_plan_claim.recorded_claim_branches(source_status, source_comments),
            )
            if competing:
                refuse(competing)
        return permitted

    agent = github_agent.running_agent(getattr(args, "agent", None))
    override = (getattr(args, "agent_override", None) or "").strip()
    # release-claim can only release a session that exists; an unreadable ID stays permitted.
    native = github_agent.native_sessions(agent)
    if native and claim["session"] not in native.values():
        found = ", ".join(f"{name}={value}" for name, value in native.items())
        raise ClassifiedPlanError("session_not_native",
            f"--session {claim['session']} is not this session's native ID ({found}). Claim with that native ID; "
            "release-claim can never release a claim made under an invented label.",
            payload={"native_sessions": native, "running_agent": agent})

    def check_agent(assigned_issue: dict[str, Any]) -> None:
        mismatch = github_agent.exclusion(assigned_issue, agent)
        if mismatch and not override:
            names = ", ".join(mismatch["agent_labels"])
            raise ClassifiedPlanError("agent_assignment_mismatch",
                f"Issue carries {names}; running agent is {agent or 'unknown'}. Stop and use the assigned family, or --agent FAMILY to identify this harness. Only an explicit Director override permits --agent-override REASON.",
                payload={"agent_labels": mismatch["agent_labels"], "running_agent": agent})

    def claim_recovery() -> dict[str, Any]:
        recovery = {}
        comment_id = (claim_comment.get("comment") or {}).get("id")
        if "post_claim" in completed and comment_id:
            recovery = {"release_own_claim": {"comment_id": comment_id,
                                             "body": f"Released claim {comment_id}"},
                        "then": "Post this exact release through the same bot, preserve competing ownership, and recheck before any retry"}
        return recovery

    def refuse(competing_claims: list[dict[str, Any]], *, owner_comments: list[dict[str, Any]] | None = None,
               owner_repo: str | None = None, owner_number: int | None = None,
               coordination: list[dict[str, Any]] | None = None) -> None:
        raise ClassifiedPlanError(
            "claim_conflict", "Another worker or ambiguous ownership evidence holds this issue; preserve it for owner review",
            payload={"competing_evidence": competing_claims, "claim_recovery": claim_recovery(),
                     "coordination": coordination if coordination is not None else github_plan_claim.coordination(
                         competing_claims, comments if owner_comments is None else owner_comments,
                         owner_repo or issue_repo, number if owner_number is None else owner_number)},
        )

    def admission_line() -> str:
        if not admission:
            return ""
        record = github_plan_claim.admission_text(admission, admission_links, unlinked=admission_unlinked)
        return record + "\n\n"

    def refuse_or_route(competing_claims: list[dict[str, Any]], route_comments: list[dict[str, Any]]) -> None:
        """Refuse; before any write, name a released handoff's successor route for cmd_claim to verify."""
        route = None
        if not args.resume_from and not completed:
            route = github_plan_claim.released_successor_route(competing_claims, route_comments, issue_repo=issue_repo)
        raise ClassifiedPlanError(
            "claim_conflict", "Another worker or ambiguous ownership evidence holds this issue; preserve it for owner review",
            payload={"competing_evidence": competing_claims, "claim_recovery": claim_recovery(),
                     "coordination": github_plan_claim.coordination(competing_claims, route_comments, issue_repo, number),
                     **({"successor_route": {"resume_from": route}} if route else {})},
        )

    def check_native_blockers() -> None:
        if any(blocker.get("state") != "closed" for blocker in phase["blockers"]):
            raise ClassifiedPlanError("claim_native_blockers_unresolved", "Claim requires resolved native blockers; preserve blocked planning state")

    def check_pr_artifacts(status_to_check: str, current_comments: list[dict[str, Any]]) -> None:
        fresh_inventory = inventory
        fresh_pulls = phase["open_pulls"]
        if phase["canonical_issue"] is not None:
            raise ClassifiedPlanError("claim_issue_record_required",
                "PR has a canonical implementation issue; claim that issue instead",
                payload={"canonical_issue": phase["canonical_issue"]})
        _, own = github_plan_claim.discussion_evidence("", current_comments, claim)
        ended_branches = github_plan_claim.ended_artifact_branches(ended_owners, current_comments, fresh_pulls)
        status_sessions = {entry["session"] for entry in ended_owners}
        recorded_branches = github_plan_claim.recorded_claim_branches(status_to_check, current_comments)
        scopes = dict.fromkeys(((number, issue_repo), *((n, target_repo)
                               for n in sorted(phase.get("retained_pr_numbers", ())))))
        for checked_number, checked_repo in scopes:
            canonical = checked_repo.casefold() == issue_repo.casefold() and checked_number == number
            conflicts = github_plan_claim.artifact_evidence(
                fresh_inventory, fresh_pulls, checked_number, claim, own_record=bool(own), repo=checked_repo,
                subject_pr=number if record_kind == "pr" else None, retained=retained,
                retained_branches=retained_branches, retained_repo=target_repo if retained_mode else None,
                inventory_repo=target_repo,
                ended_branches=ended_branches if canonical else set(),
                status_sessions=status_sessions if canonical else set(),
                recorded_branches=recorded_branches if canonical else set(),
            )
            if conflicts:
                if canonical:
                    refuse(conflicts)
                refuse(conflicts, owner_comments=[], owner_repo=checked_repo, owner_number=checked_number)

    try:
        issue, status, comments, can_update = claim_snapshot(args.issue, repo)
        record_kind = "pr" if "pull_request" in issue else "issue"
        if record_kind == "issue" and args.resume_from:
            target_repo = repo
        previous_status = status
        previous_wait = github_plan_wait.marker(issue["claim_wait"])
        previous_wait_labels = set(normalize_labels(issue.get("labels"))) & {config["labels"][key] for key in ("waiting", "blocked")}
        check_agent(issue)
        source_info, prepared_handoff = prepare_phase((issue, status, comments, can_update))
        observe_claim_sources()
        _, resume_comments, _ = source_info
        conflicts, owned = ownership_snapshot(status, comments)
        if conflicts:
            refuse_or_route(conflicts, comments)
        check_wait(issue, status)
        planned_wait = previous_wait if retained_mode else github_plan_wait.marker(
            github_plan_wait.reviewed(evidence=args.wait_resolved or issue["claim_wait"]["evidence"]))
        if owned:
            claim = {key: owned[0][key] for key in claim}
        if phase["source_error"] is not None:
            raise phase["source_error"]
        retained = phase["source"]["record"]["branch"] if args.resume_from else None
        check_native_blockers()
        retained_branches = handoff_preflight(comments, status, prepared_handoff)
        pulls = phase["open_pulls"]
        conflicts = github_plan_claim.artifact_evidence(inventory, pulls, number, claim,
                                                       subject_pr=number if record_kind == "pr" else None,
                                                       own_record=bool(owned), retained=retained, repo=issue_repo,
                                                       retained_branches=retained_branches,
                                                       retained_repo=target_repo if retained_mode else None,
                                                       inventory_repo=target_repo,
                                                       ended_branches=github_plan_claim.ended_artifact_branches(ended_owners, comments, pulls),
                                                       recorded_branches=github_plan_claim.recorded_claim_branches(status, comments),
                                                       status_sessions={record["session"] for _, record, _ in github_plan_claim.exact_released_sources(comments)})
        if conflicts:
            refuse_or_route(conflicts, comments)
        check_pr_artifacts(status, comments)
        _, comment_owners = github_plan_claim.discussion_evidence("", comments, claim)
        comment_recorded = any(record.get("_legacy") != "yes" for record in comment_owners)
        admission_record = "" if comment_recorded else admission_line()
        completed.append("ownership_preflight")
        actor, gh_cmd, expected_actor = comment_route()
        wait_receipt = ("\n\nRetained PR waits verified for this step:\n" + "\n".join(
            f"PR #{pr}:\n" + "\n".join("> " + line for line in wait.splitlines())
            for pr, wait in sorted(retained_waits.items())) if retained_waits else "")
        text = (
            f"Claimed by {claim['worker']}\n\nSession: {claim['session']}\nBranch: {claim['branch']}\n"
            f"Claimed at: {claim['claimed_at']}\nNext action: {args.next_action}\n\n"
            + (f"Agent override (Director-authorized): {override}\n\n" if override else "")
            + admission_record
            + github_plan_claim.marker(claim)
            + "\n" + planned_wait
            + (f"\n\nRetained-work handoff: released claim {args.resume_from}; handoff comment {handoff_id}. This successor claim preserves retained PRs and their product holds." if retained_mode else "")
            + ("\n\nRetained PR waits verified for this step: " + ", ".join(
                f"https://github.com/{target_repo}/pull/{pr}" for pr in sorted(retained_waits)
            ) if retained_waits else "")
            + (f"\n\nWait resolution: {args.wait_resolved}" if args.wait_resolved else "")
            + f"\n\n[Work record](https://github.com/{issue_repo}/"
              f"{'pull' if record_kind == 'pr' else 'issues'}/{number})"
        )
        # Freeze the source before any external claim write. If publication
        # fails, the closed source stays frozen for supported claim recovery.
        admit_observed_sources()
        if not comment_recorded:
            claim_comment = github_comment_core.comment(
                record_kind, number, text, repo=issue_repo, gh_cmd=gh_cmd,
                expected_actor=expected_actor, operation=CURRENT_OPERATION,
                completed_steps=completed, failed_step="post_claim", dedupe_body=True,
            )
            completed.append("post_claim")
        # Check the discussion again before touching status or labels.
        issue, status, comments, can_update = claim_snapshot(args.issue, repo)
        check_agent(issue)
        _, prepared_handoff = prepare_phase((issue, status, comments, can_update))
        observe_claim_sources()
        conflicts, observed = ownership_snapshot(status, comments)
        if conflicts:
            refuse(conflicts)
        if phase["source_error"] is not None:
            raise phase["source_error"]
        check_pr_artifacts(status, comments)
        check_wait(issue, status)
        check_issue_holds(issue)
        check_native_blockers()
        handoff_preflight(comments, status, prepared_handoff)
        if not any(github_plan_claim.same_owner(record, claim) for record in observed):
            raise PlanError("Claim was not visible on readback; do not create a worktree")
        admit_observed_sources()
        completed.append("claim_readback")
        status_text = (
            f"State: Active; owned by {claim['worker']}.\nSession: {claim['session']}\n"
            f"Branch: {claim['branch']}\nNext action: {args.next_action}\n"
            + f"Last verified: {claim['claimed_at']}\n\n"
            + (f"Wait resolution: {args.wait_resolved}\n\n" if retained_mode and args.wait_resolved else "")
            + github_plan_claim.marker(claim)
            + "\n" + planned_wait
            + wait_receipt
        )
        if can_update:
            if retained_mode:
                _, pre_write_issue = get_issue(args.issue, repo)
                if pre_write_issue.get("body") != issue.get("body"):
                    raise PlanError("Issue body changed before retained-handoff status write; preserve the new hold and verify before recovery")
            # Creation may place its record after later sections. The authored
            # claim record replaces it; examples remain unchanged.
            record_body = issue.get("body") or ""
            managed_block = marked_block(record_body, PLAN_MANAGED_START, PLAN_MANAGED_END)
            if managed_block is not None:
                start, end, managed_text = managed_block
                record_body = record_body[:start] + github_plan_wait.without_records(managed_text) + record_body[end:]
            else:
                record_body = github_plan_wait.without_records(record_body)
            status_issue = {**issue, "body": record_body}
            body = replace_issue_plan_section(status_issue, "Current Status", status_text)
            rest_edit_issue(issue_repo, number, body=body)
            completed.append("update_current_status")
        label_map = config["labels"]
        if record_kind == "issue":
            # Status publication is a real mutation boundary. Re-read mutable
            # assignment and holds before labels can remove them, without
            # discovering or admitting another native source.
            label_issue, label_status, _, _ = claim_snapshot(args.issue, repo)
            check_agent(label_issue)
            check_issue_holds(label_issue)
            previous_labels = set(normalize_labels(issue.get("labels")))
            fresh_labels = set(normalize_labels(label_issue.get("labels")))
            if (fresh_labels - previous_labels) & {label_map[key] for key in ("waiting", "blocked", "stale", "done")}:
                raise PlanError("Issue acquired a new hold before claim label update")
            if can_update and not any(github_plan_claim.same_owner(record, claim)
                                      for record in github_plan_claim.records(label_status)):
                raise PlanError("Current Status claim changed before label update")
            issue = label_issue
            label_result = github_issue_core.edit_issue(
                number, repo=issue_repo, add_labels=[label_map["active"]],
                remove_labels=[label_map[key] for key in (("stale", "done") if retained_mode
                                                         else ("waiting", "stale", "done", "blocked"))
                               if label_map[key] in normalize_labels(issue.get("labels"))],
                gh_cmd=gh_cmd, expected_actor=expected_actor, operation=CURRENT_OPERATION,
            )
            completed.append("update_labels")
            actor = label_result.get("actor") or actor
        final, final_status, final_comments, _ = claim_snapshot(args.issue, repo)
        comments = final_comments
        check_agent(final)
        _, prepared_handoff = prepare_phase((final, final_status, final_comments, can_update))
        observe_claim_sources()
        conflicts, observed = ownership_snapshot(final_status, final_comments)
        if conflicts:
            refuse(conflicts)
        if phase["source_error"] is not None:
            raise phase["source_error"]
        check_pr_artifacts(final_status, final_comments)
        check_wait(final, final_status)
        check_issue_holds(final)
        check_native_blockers()
        handoff_preflight(final_comments, final_status, prepared_handoff)
        if not observed or (record_kind == "issue" and label_map["active"] not in normalize_labels(final.get("labels"))):
            raise PlanError("Claim metadata was not visible on final readback")
        if can_update and not any(github_plan_claim.same_owner(record, claim)
                                  for record in github_plan_claim.records(final_status)):
            raise PlanError("Current Status claim changed during readback")
        admit_observed_sources()
        completed.append("metadata_readback")
    except github_comment_core.CommentError as exc:
        error = plan_error_from_comment(exc, completed_steps=completed)
        error.payload.update({"claim": claim, "claim_comment": claim_comment, "previous_current_status": previous_status,
                              "claim_recovery": claim_recovery()})
        raise error from exc
    except github_issue_core.IssueError as exc:
        error = plan_error_from_issue(exc, completed_steps=completed)
        error.payload.update({"claim": claim, "claim_comment": claim_comment, "previous_current_status": previous_status,
                              "claim_recovery": claim_recovery()})
        raise error from exc
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        payload = {"completed_steps": completed, "claim": claim,
                   "previous_current_status": previous_status, "claim_recovery": claim_recovery(),
                   "claim_comment": claim_comment, "session_coverage": inventory.get("session_coverage", {})}
        if isinstance(exc, github_plan_claim.ClaimProofError):
            raise ClassifiedPlanError(exc.code, str(exc), payload={**exc.payload, **payload}) from exc
        raise PlanError(str(exc), payload=payload) from exc
    except PlanError as exc:
        exc.payload.update({"completed_steps": completed, "claim": claim, "claim_comment": claim_comment,
                            "previous_current_status": previous_status,
                            "claim_recovery": claim_recovery(),
                            "session_coverage": inventory.get("session_coverage", {})})
        if exc.failure is not None:
            exc.failure.completed_steps = merge_completed_steps(completed, exc.failure.completed_steps)
        elif completed:
            exc.failure = github_api_core.FailureDetail(
                cause=getattr(exc, "code", "claim_incomplete"), message=str(exc),
                retryable=False, fallback_eligible=False, disposition="stop",
                write_outcome="unknown", completed_steps=completed,
            )
        raise
    claim_id = (claim_comment.get("comment") or {}).get("id")
    # Name the supported release here so a finishing worker never hand-writes the directive.
    release_route = ({"release_command": shlex.join([
        "uv", "run", str(pathlib.Path(__file__).resolve()), "--repo", issue_repo, "release", str(number),
        "--claim-comment", str(claim_id), "--session", claim["session"], "--handoff-file", "<handoff.md>"])}
        if claim_id else {})
    emit({"ok": True, "actor": actor, "record_kind": record_kind, "claim": claim, "claim_comment": claim_comment, **release_route,
          "issue": compact_issue(final),
          "current_status_location": "issue_body" if can_update else "claim_comment",
          "previous_current_status": previous_status,
          "retained_artifacts": [entry for entry in ended_owners if entry.get("worktrees") or entry.get("pull_requests")
                                 or entry["branch"] in inventory["local_branches"] or entry["branch"] in inventory["remote_branches"]],
          "session_coverage": inventory["session_coverage"], "exclusive_lock": False,
          "completed_steps": completed})


def automatic_planning_writes_allowed() -> bool:
    """An exact personal command approval never authorizes discovered targets."""
    return not (os.environ.get("GH_PLAN_SKIP_BOT") == "1"
                or os.environ.get("GH_PLAN_ALLOW_ACTIVE_FIRST") == "1"
                or github_identity.active_auth_fallback_allowed()
                or github_identity.own_user_opted_in())


def refresh_planning_targets(targets: list[tuple[str, int]], *, apply: bool = True,
                             include_dependents: bool = False, required: bool = True,
                             config_optional_unless_planning: bool = False,
                             planning_checkout: str | None = None) -> dict[str, Any]:
    """A confirmed fact write and its label projection have separate receipts."""
    tasks = [{"repo": repo, "number": number, "role": "related", "required": required,
              **({"config_optional_unless_planning": True} if config_optional_unless_planning else {})}
             for repo, number in dict.fromkeys(targets)]
    if include_dependents:
        for repo, number in targets:
            for target_repo, target_number in github_plan_landing.neighborhood(
                    argparse.Namespace(**globals()), repo, [(repo, number)]):
                if (target_repo, target_number) in targets:
                    continue
                entry = {"repo": target_repo, "number": target_number, "role": "related", "automatic": True}
                if target_repo.split("/")[0].casefold() != repo.split("/")[0].casefold():
                    entry["status"] = "review_cross_repository"
                if not any(item["repo"] == target_repo and item["number"] == target_number for item in tasks):
                    tasks.append(entry)
    if planning_checkout:
        root_repositories = {repo.casefold() for repo, _ in targets}
        for entry in tasks:
            if entry["repo"].casefold() in root_repositories:
                entry["planning_checkout"] = planning_checkout
    return github_plan_landing.run_reconciliation_tasks(argparse.Namespace(**globals()), tasks, apply=apply)


def status_recovery(repo: str, number: int) -> dict[str, Any]:
    return {"issue": {"repo": repo, "number": number},
            "next_action": f"gh-plan.py --repo {repo} reconcile-status {number} --apply",
            "scope": "Retry only the projection after resolving its reported review or failure; preserve the committed fact"}


def cmd_reconcile_status(args: argparse.Namespace) -> None:
    repo, number = issue_ref(args.issue, default_repo(args.repo))
    recovery = status_recovery(repo, number)
    planning_checkout = getattr(args, "planning_checkout", None)
    if planning_checkout:
        checkout_path = pathlib.Path(planning_checkout).expanduser()
        if not checkout_path.is_dir() or (repo_from_git(checkout_path) or "").casefold() != repo.casefold():
            raise PlanError("--planning-checkout must name a checkout of the resolved target repository")
    if args.include_dependents:
        recovery["next_action"] = f"gh-plan.py --repo {repo} reconcile-status {number} --include-dependents --apply"
    if planning_checkout:
        recovery["next_action"] += " " + shlex.quote("--planning-checkout=" + planning_checkout)
    try:
        result = refresh_planning_targets([(repo, number)], apply=args.apply,
            include_dependents=args.include_dependents, required=True,
            **({"planning_checkout": planning_checkout} if planning_checkout else {}))
    except PlanError as error:
        error.payload["recovery"] = recovery
        raise
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise PlanError(str(error), payload={"recovery": recovery}) from error
    result["recovery"] = recovery
    if not result["ok"]:
        raise PlanError("Planning label projection is incomplete", payload=result)
    emit(result)


def cmd_wait(args: argparse.Namespace) -> None:
    try:
        wait_once(args)
    except github_comment_core.CommentError as exc:
        raise plan_error_from_comment(exc) from exc
    except (ValueError, OSError) as exc:
        raise PlanError(str(exc)) from exc


def wait_once(args: argparse.Namespace) -> None:
    """Review a legacy record once, or explicitly maintain a structured wait."""
    repo = default_repo(args.repo)
    if args.all:
        if args.issue or args.clear or args.waiting_for or args.ends_when or args.evidence:
            raise PlanError("--all is a read-only inventory; choose a single issue to record a review")
        _, issues = collect_paged_rest_items(f"/repos/{repo}/issues", query={"state": "open"},
                                            bucket="rest_core", step_prefix="wait_inventory")
        review = []
        checked = 0
        for issue in issues:
            if "pull_request" in issue:
                continue
            _, comments = collect_paged_rest_items(f"/repos/{repo}/issues/{issue['number']}/comments",
                                                  query={}, bucket="rest_core", step_prefix="wait_inventory_comments")
            checked += 1
            try:
                wait = current_wait(issue, comments, repo)
                reason = "No reviewed structured record" if wait["state"] == "review" else None
            except (ValueError, PlanError) as exc:
                reason = str(exc)
            if reason:
                review.append({"repo": repo, "number": issue["number"], "url": issue.get("html_url"),
                               "reason": reason, "next_action": f"gh-plan.py --repo {repo} wait {issue['number']}"})
        emit({"ok": True, "repo": repo, "checked": checked, "review_required": review,
              "writes": [], "next_action": "Review each listed record once before its next claim; no prose-only wait or owner was cleared"})
        return
    if not args.issue:
        raise PlanError("Choose an issue or --all for the read-only legacy review inventory")
    args = claim_reference(args)
    issue, status, comments, _ = claim_snapshot(args.issue, repo, allow_closed=True)
    current = issue["claim_wait"]
    if not args.clear and not args.waiting_for:
        if args.ends_when or args.evidence:
            raise PlanError("Choose --clear or --waiting-for with --ends-when and --evidence")
        emit({"ok": True, "review_required": current["state"] == "review", "structured_wait": current,
              "issue": compact_issue(issue), "review_evidence": {
                  "body": issue.get("body") or "", "comments": comments},
              "next_action": "Read the full evidence once; record each actual person/event hold with "
                             "--waiting-for and --ends-when, or --clear after verifying absence. "
                             "The review also covers any prose-only current owner; preserve it as a wait until released."})
        return
    try:
        wait = github_plan_wait.reviewed(evidence=args.evidence or "", waiting_for=args.waiting_for,
                                        ends_when=args.ends_when)
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    # A dedicated authored comment works for PRs and contributor issues without
    # replacing anyone's prose. Later status edits do not erase the record.
    actor, gh_cmd, expected_actor = comment_route()
    kind = "pr" if "pull_request" in issue else "issue"
    latest_issue, _, latest_comments, _ = claim_snapshot(args.issue, repo, allow_closed=True)
    if latest_issue["claim_wait"] != current or latest_issue.get("body") != issue.get("body") or latest_comments != comments:
        raise PlanError("Wait evidence changed before the review write; read the new record and discussion")
    result = github_comment_core.comment(kind, issue["number"],
        "Structured wait review\n\n" + github_plan_wait.marker(wait), repo=issue["repo"],
        gh_cmd=gh_cmd, expected_actor=expected_actor, operation=CURRENT_OPERATION, dedupe_body=True)
    try:
        observed, _, _, _ = claim_snapshot(args.issue, repo, allow_closed=True)
        if observed["claim_wait"] != wait:
            raise PlanError("Structured wait changed or was not visible on readback; preserve the newer record")
    except (PlanError, ValueError, OSError) as exc:
        failure = github_api_core.FailureDetail(cause="wait_readback_failed", message=str(exc),
            retryable=False, fallback_eligible=False, disposition="stop",
            completed_steps=["post_wait"], failed_step="wait_record_readback")
        raise PlanError(str(exc), failure=failure, payload={"structured_wait": wait, "comment": result,
                        "completed_steps": ["post_wait"],
                        "recovery": "Read the published comment and latest wait before any retry"}) from exc
    projection = refresh_planning_targets([(issue["repo"], issue["number"])]) if kind == "issue" else None
    published = {"actor": actor, "structured_wait": wait, "comment": result,
                 "issue": compact_issue(observed), "projection": projection, "completed_steps": ["post_wait", "wait_record_readback"]}
    if projection is not None and projection["coverage"] != "complete":
        published["recovery"] = status_recovery(issue["repo"], issue["number"])
    if projection is not None and not projection["ok"]:
        raise PlanError("Wait record is published; planning label projection is incomplete", payload=published)
    emit({"ok": True, **published})


def cmd_create(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    title = (args.title_flag or args.title or "").strip()
    if not title:
        raise PlanError("Issue title is required (pass as positional argument or --title)")
    wait = None
    if getattr(args, "wait_clear", False) or getattr(args, "waiting_for", None):
        try:
            wait = github_plan_wait.reviewed(evidence=args.wait_evidence or "",
                                             waiting_for=args.waiting_for, ends_when=args.ends_when)
        except ValueError as exc:
            raise PlanError(str(exc)) from exc
    elif getattr(args, "ends_when", None) or getattr(args, "wait_evidence", None):
        raise PlanError("Choose --wait-clear or --waiting-for with --ends-when and --wait-evidence")
    completed_steps: list[str] = []
    actor, exact = find_existing_plan_issues(repo, title, completed_steps=completed_steps)
    if exact and not args.force:
        emit({
            "ok": True,
            "actor": actor,
            "deduped": True,
            "repo": repo,
            "existing": exact[0],
            "completed_steps": completed_steps,
        })
        return

    base_labels = labels(config, "plan")
    if args.plan_status != "none":
        base_labels.extend(labels(config, args.plan_status))
    try:
        extra_labels = github_agent.creation_labels(args.label or [], getattr(args, "agent", None))
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    wanted_labels = list(dict.fromkeys(base_labels + extra_labels))
    _, created_base_labels = ensure_labels(
        repo,
        base_labels,
        config,
        completed_steps=completed_steps,
    )
    created_extra_labels: list[str] = []
    if extra_labels:
        _, created_extra_labels = ensure_labels(
            repo,
            extra_labels,
            config,
            create_unknown=False,
            completed_steps=completed_steps,
        )
    created_labels = created_base_labels + created_extra_labels

    body = read_body(args, template_body(title))
    if args.finish_line:
        body = replace_section(body, "Finish Line", args.finish_line)
    body = ensure_managed_provenance(body)
    if wait is not None:
        body = github_plan_wait.without_records(body) + "\n\n" + github_plan_wait.marker(wait)
    project_config = config.get("projects") or {}
    project = args.project
    if project is None and project_config.get("enabled", True):
        project = project_config.get("default_project")
    route_actor, gh_cmd, expected_actor = comment_route()
    try:
        issue = github_issue_core.create_issue(
            title,
            body,
            repo=repo,
            labels=wanted_labels,
            milestone=args.milestone,
            gh_cmd=gh_cmd,
            expected_actor=expected_actor,
            operation=CURRENT_OPERATION,
        )
    except github_issue_core.IssueError as exc:
        raise plan_error_from_issue(exc, completed_steps=completed_steps) from exc
    actor = issue.get("actor") or route_actor
    issue_steps = issue.get("completed_steps")
    if isinstance(issue_steps, list):
        completed_steps.extend(str(step) for step in issue_steps)
    else:
        completed_steps.append("reconcile_create" if issue.get("reconciled") else "create_issue")
    issue_url = issue.get("url")
    if not isinstance(issue_url, str):
        raise PlanError("Shared issue create returned no issue URL")
    project_fields_set: dict[str, Any] = {}
    if project:
        project_steps: list[str] = []
        try:
            owner = project_config.get("owner") or repo.split("/", 1)[0]
            _, number, _ = resolve_project(owner, project, recoverable=True)
            project_steps.append("resolve_project")
            ensure_graphql_budget(prefer_active=True, recoverable=True)
            _, added_stdout, _ = run_raw(["project", "item-add", str(number), "--owner", owner, "--url", issue_url, "--format", "json"], prefer_active=True)
            project_steps.append("add_project_item")
            added_item = json.loads(added_stdout) if added_stdout.strip() else {}
            added_item_id = added_item.get("id") if isinstance(added_item, dict) else None
            if args.focus or args.manager:
                project_fields_set = set_project_fields(
                    owner=owner,
                    project_ref=project,
                    issue_url=issue_url,
                    config=config,
                    focus=args.focus,
                    manager=resolve_required_manager_value(args.manager),
                    item_id=added_item_id,
                    recoverable=True,
                )
        except PlanError as exc:
            completed_steps.extend(project_steps)
            owner = project_config.get("owner") or repo.split("/", 1)[0]
            project_fields_set = project_failure_payload(
                exc,
                owner=owner,
                project=project,
                operation="create_project_sync",
                non_blocking=True,
            )
        else:
            completed_steps.append("sync_project")
    result: dict[str, Any] = {
        "ok": True,
        "actor": actor,
        "expected_actor": issue.get("expected_actor", expected_actor),
        "created_labels": created_labels,
        "project_fields": project_fields_set,
        "issue": compact_issue(issue),
        "completed_steps": completed_steps,
    }
    for key in ("operation_marker", "reconciled", "reconciliation"):
        if key in issue:
            result[key] = issue[key]
    result["projection"] = refresh_planning_targets([(repo, int(issue["number"]))])
    if result["projection"]["coverage"] != "complete":
        result["recovery"] = status_recovery(repo, int(issue["number"]))
    if not result["projection"]["ok"]:
        raise PlanError("Issue is created; planning label projection is incomplete; do not repeat creation", payload=result)
    emit(result)


def cmd_update_section(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    issue_repo, number = issue_ref(args.issue, repo)
    _, issue = get_issue(args.issue, repo)
    if "pull_request" in issue:
        message = (
            f"{issue_repo}#{number} is a pull request; update-section only updates planning issues. "
            "Select the intended issue reference, or use gh-plan.py --repo OWNER/REPO update-section "
            "with a bare issue number. "
            f"For an intended PR body edit, use gh-pr.py --repo {issue_repo} edit {number} --body-file FILE."
        )
        failure = github_api_core.FailureDetail(
            cause="validation_error",
            message=message,
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="not_started",
            failed_step="validate_object_kind",
        )
        raise PlanError(message, failure=failure)
    preparation_step = "read_body"
    try:
        if args.body is None and not args.body_file:
            raise ValueError(
                "Section text requires --body TEXT or --body-file FILE; use --body-file - for stdin. "
                "To intentionally clear the section, pass --body ''."
            )
        new_text = read_body(args)
        preparation_step = "section_replacement"
        updated = replace_issue_plan_section(issue, args.section, new_text)
    except (OSError, ValueError, re.error) as exc:
        message = f"Cannot prepare plan section update: {exc}"
        failure = github_api_core.FailureDetail(
            cause="validation_error",
            message=message,
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="not_started",
            failed_step=preparation_step,
        )
        raise PlanError(message, failure=failure) from exc
    actor, refreshed = rest_edit_issue(issue_repo, number, body=updated)
    emit({"ok": True, "actor": actor, "updated_section": args.section, "issue": compact_issue(refreshed)})


def cmd_link(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    actor_a, source = get_issue(args.issue, repo)
    _, target = get_issue(args.target, repo)
    source_repo = source["repo"]
    target_repo = target["repo"]
    source_number = int(source["number"])
    target_number = int(target["number"])
    rel = args.relationship

    if rel == "blocked-by":
        actor, _ = api_json(
            "POST",
            f"/repos/{source_repo}/issues/{source_number}/dependencies/blocked_by",
            {"issue_id": target["id"]},
            failed_step="link_blocked_by",
        )
    elif rel == "blocks":
        actor, _ = api_json(
            "POST",
            f"/repos/{target_repo}/issues/{target_number}/dependencies/blocked_by",
            {"issue_id": source["id"]},
            failed_step="link_blocks",
        )
    elif rel == "subissue":
        actor, _ = api_json(
            "POST",
            f"/repos/{source_repo}/issues/{source_number}/sub_issues",
            {"sub_issue_id": target["id"]},
            failed_step="link_subissue",
        )
    elif rel == "related":
        updated = add_relationship_note(source, "related", target)
        actor, source = rest_edit_issue(source_repo, source_number, body=updated)
    else:
        raise PlanError(f"Unsupported relationship: {rel}")

    affected = target if rel == "blocks" else source
    projection = (refresh_planning_targets([(affected["repo"], int(affected["number"]))])
                  if rel in {"blocked-by", "blocks", "subissue"} else None)
    recovery = (status_recovery(affected["repo"], int(affected["number"]))
                if projection is not None and projection["coverage"] != "complete" else None)
    if projection is not None and not projection["ok"]:
        raise PlanError("Native relationship is linked; label projection is incomplete; do not repeat the edge write",
                        payload={"relationship": rel, "source": compact_issue(source), "target": compact_issue(target),
                                 "projection": projection, "recovery": recovery, "completed_steps": ["link_relationship"]})
    emit({
        "ok": True,
        "actor": actor,
        "projection": projection,
        "recovery": recovery,
        "relationship": rel,
        "source": compact_issue(source),
        "target": compact_issue(target),
    })


def cmd_unlink(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    _, source = get_issue(args.issue, repo)
    _, target = get_issue(args.target, repo)
    source_repo = source["repo"]
    target_repo = target["repo"]
    source_number = int(source["number"])
    target_number = int(target["number"])
    rel = args.relationship
    if rel == "blocked-by":
        actor, _ = api_json(
            "DELETE",
            f"/repos/{source_repo}/issues/{source_number}/dependencies/blocked_by/{target['id']}",
            failed_step="unlink_blocked_by",
        )
    elif rel == "blocks":
        actor, _ = api_json(
            "DELETE",
            f"/repos/{target_repo}/issues/{target_number}/dependencies/blocked_by/{source['id']}",
            failed_step="unlink_blocks",
        )
    elif rel == "subissue":
        actor, _ = api_json(
            "DELETE",
            f"/repos/{source_repo}/issues/{source_number}/sub_issue",
            {"sub_issue_id": target["id"]},
            failed_step="unlink_subissue",
        )
    elif rel == "related":
        updated = remove_relationship_note(source, "related", target)
        actor, _ = rest_edit_issue(source_repo, source_number, body=updated)
    else:
        raise PlanError(f"Unlink supports blocked-by, blocks, subissue, and related; got {rel}")
    affected = target if rel == "blocks" else source
    projection = (refresh_planning_targets([(affected["repo"], int(affected["number"]))])
                  if rel in {"blocked-by", "blocks", "subissue"} else None)
    recovery = (status_recovery(affected["repo"], int(affected["number"]))
                if projection is not None and projection["coverage"] != "complete" else None)
    if projection is not None and not projection["ok"]:
        raise PlanError("Native relationship is unlinked; label projection is incomplete; do not repeat the edge write",
                        payload={"relationship_removed": rel, "source": compact_issue(source), "target": compact_issue(target),
                                 "projection": projection, "recovery": recovery, "completed_steps": ["unlink_relationship"]})
    emit({"ok": True, "actor": actor, "relationship_removed": rel, "projection": projection, "recovery": recovery})


def cmd_deps(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    _, issue = get_issue(args.issue, repo)
    issue_repo = issue["repo"]
    number = int(issue["number"])
    actor, relationships = read_issue_relationships(
        issue_repo,
        number,
        include_blocking=True,
    )
    emit({"ok": True, "actor": actor, "issue": compact_issue(issue), **relationships})


def project_item_field_value(item: dict[str, Any], field_name: str) -> str | None:
    wanted = re.sub(r"[^a-z0-9]+", "", field_name.casefold())
    for key, value in item.items():
        normalized_key = re.sub(r"[^a-z0-9]+", "", key.casefold())
        if normalized_key == wanted and isinstance(value, str) and value.strip():
            return value.strip()
    return None


def next_focus_context(
    repo: str,
    config: dict[str, Any],
) -> tuple[str | None, dict[str, str], dict[str, Any]]:
    project_config = config.get("projects") or {}
    owner = project_config.get("owner")
    project_ref = project_config.get("default_project")
    if not project_config.get("enabled") or not owner or not project_ref:
        return None, {}, {"available": False, "reason": "project_not_configured"}
    try:
        actor, project_number, project = project_meta(
            str(owner),
            str(project_ref),
            recoverable=True,
        )
        items = project_items(
            str(owner),
            project_number,
            query=f"repo:{repo}",
            limit=NEXT_PROJECT_ITEM_LIMIT + 1,
            recoverable=True,
        )
    except PlanError as exc:
        raise_if_cancelled(exc)
        failure = project_failure_payload(
            exc,
            owner=str(owner),
            project=str(project_ref),
            operation="next_focus_context",
        )
        failure.update({
            "available": False,
            "reason": "project_focus_unavailable",
        })
        failure["error"] = github_api_core.redact_string(str(exc))
        return None, {}, failure
    truncated = len(items) > NEXT_PROJECT_ITEM_LIMIT
    items = items[:NEXT_PROJECT_ITEM_LIMIT]
    focus_field = (config.get("project_fields") or {}).get("focus", "Focus")
    focus_by_url: dict[str, str] = {}
    for item in items:
        content = item.get("content") or {}
        url = content.get("url") if isinstance(content, dict) else None
        focus = project_item_field_value(item, str(focus_field))
        if isinstance(url, str) and focus:
            focus_by_url[url] = focus
    return actor, focus_by_url, {
        "available": True,
        "owner": owner,
        "project": project.get("title") or project_ref,
        "project_number": project_number,
        "matched_items": len(focus_by_url),
        "item_limit": NEXT_PROJECT_ITEM_LIMIT,
        "truncated": truncated,
    }


def read_next_issue_relationships(
    issue_repo: str,
    number: int,
    issue: dict[str, Any] | None = None,
    *,
    limit: int = NEXT_RELATIONSHIP_LIMIT,
) -> tuple[str, dict[str, list[dict[str, Any]]], list[str]]:
    if issue is not None and "_graph_relationships" in issue:
        graph = issue["_graph_relationships"]
        return issue.get("_graph_actor", ""), {key: [compact_relationship_issue(row, key) for row in rows[:limit]]
            for key, rows in graph.items()}, [key for key, rows in graph.items() if len(rows) > limit]
    relationships: dict[str, list[dict[str, Any]]] = {}
    truncated: list[str] = []
    actor = ""
    endpoints = [
        ("blocked_by", f"/repos/{issue_repo}/issues/{number}/dependencies/blocked_by"),
        ("blocking", f"/repos/{issue_repo}/issues/{number}/dependencies/blocking"),
        ("sub_issues", f"/repos/{issue_repo}/issues/{number}/sub_issues"),
    ]
    for relationship, endpoint in endpoints:
        summary_key, total_key = {
            "blocked_by": ("issue_dependencies_summary", "total_blocked_by"),
            "blocking": ("issue_dependencies_summary", "total_blocking"),
            "sub_issues": ("sub_issues_summary", "total"),
        }[relationship]
        summary = (issue or {}).get(summary_key)
        total = summary.get(total_key) if isinstance(summary, dict) else None
        # Only native total zero proves there is no history to read. Open
        # counts, missing fields and malformed values cannot prove emptiness.
        if type(total) is int and total == 0:
            relationships[relationship] = []
            continue
        page_actor, items = collect_paged_rest_items(
            endpoint,
            query={},
            bucket="rest_core",
            step_prefix=f"next_{relationship}",
            limit=limit + 1,
        )
        actor = page_actor or actor
        if len(items) > limit:
            truncated.append(relationship)
            items = items[:limit]
        relationships[relationship] = [
            compact_relationship_issue(item, relationship)
            for item in items
        ]
    return actor, relationships, truncated


def read_next_inbound_blockers(repo: str, *, scan_limit: int) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Separate bounded visibility scan; never consumes ranked plan slots."""
    actor, inventory = collect_paged_rest_items(
        f"/repos/{repo}/issues", query={"state": "open", "sort": "created", "direction": "asc"},
        bucket="rest_core", step_prefix="next_inbound_inventory",
        limit=NEXT_PLAN_INVENTORY_LIMIT + 1, issue_only=True,
    )
    inventory_truncated = len(inventory) > NEXT_PLAN_INVENTORY_LIMIT
    gates = [issue for issue in inventory[:NEXT_PLAN_INVENTORY_LIMIT]
             if (issue.get("issue_dependencies_summary") or {}).get("blocking") != 0]
    report: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for issue in gates[:scan_limit]:
        try:
            relation_actor, blocking = collect_paged_rest_items(
                f"/repos/{repo}/issues/{issue['number']}/dependencies/blocking",
                query={}, bucket="rest_core", step_prefix="next_inbound_blocking",
                limit=NEXT_RELATIONSHIP_LIMIT + 1,
            )
            actor = relation_actor or actor
            if len(blocking) > NEXT_RELATIONSHIP_LIMIT:
                errors.append({"number": issue["number"], "error": "blocking_relationships_truncated"})
            targets = [compact_relationship_issue(item, "blocking") for item in blocking[:NEXT_RELATIONSHIP_LIMIT]]
        except PlanError as exc:
            errors.append({"number": issue["number"], "error": next_source_error(exc)})
            continue
        source_repo = issue_repository_name(issue) or repo
        downstream = [item for item in targets if item["state"] == "open" and item["repo"].casefold() != source_repo.casefold()]
        if downstream:
            report.append({**compact_list_issue(repo, issue), "blocking": downstream,
                           "selection": "visibility_only_review_recorded_waits_and_ownership_before_starting"})
    return actor, report, {
        "complete": not (inventory_truncated or len(gates) > scan_limit or errors),
        "inventory_count": min(len(inventory), NEXT_PLAN_INVENTORY_LIMIT),
        "inventory_truncated": inventory_truncated,
        "gate_count": len(gates), "evaluated": min(len(gates), scan_limit),
        "scan_limit": scan_limit, "errors": errors,
        "scope": "repository_open_cross_repository_dependencies",
        "unevaluated": [{"number": issue["number"]} for issue in gates[scan_limit:]],
    }


def read_next_train_enrollment(repo: str) -> dict[str, Any]:
    """Reuse Launchplane's bounded, public-safe policy projection."""
    helper = SKILL_DIR.parent / "launchplane/scripts/launchplane-write-action.py"
    try:
        timeout = min(30, github_api_core.remaining_retry_timeout_seconds())
        if timeout <= 0:
            return {"source": "launchplane", "status": "unknown", "reason": "read_deadline"}
        result = subprocess.run(
            [str(SKILL_DIR / "scripts/github-python"), str(helper), "merge-train-policy-read", "--repo", repo],
            capture_output=True, text=True, timeout=timeout,
        )
        payload = json.loads(result.stdout)
        summary = payload.get("result", {})
        if (result.returncode == 0 and payload.get("status") == "available"
                and summary.get("source") == "launchplane"
                and summary.get("status") in {"enrolled", "not_enrolled"}):
            return summary
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        pass
    # Never expose subprocess diagnostics: they can contain private config.
    return {"source": "launchplane", "status": "unknown"}


def next_dependabot_work(
    sources: list[dict[str, Any]], *, scan_limit: int, limit: int,
    now: datetime | None = None, inventory_complete: bool = True,
) -> dict[str, Any]:
    """List old PR facts separately from ranked issues and ownership judgments."""
    observed_at = now or datetime.now(timezone.utc)
    candidates: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {
        "complete": inventory_complete, "repositories": [],
        "pull_request_limit_per_repository": scan_limit,
        "observed_at": observed_at.isoformat(),
    }
    seen: set[str] = set()
    enrollment_unavailable = False
    if NEXT_READS is not None:
        def prefetch_pulls(source: dict[str, Any]) -> None:
            name = source.get("repo")
            if (not isinstance(name, str) or source.get("hold") or source.get("exclusion") in
                    {"other_owner", "archived_or_disabled", "invalid_repository_inventory_entry", "empty_without_open_issues"}):
                return
            try:
                collect_paged_rest_items(f"/repos/{name}/pulls",
                    query={"state": "open", "sort": "created", "direction": "asc"},
                    bucket="rest_core", step_prefix="next_dependabot_pulls", limit=scan_limit + 1)
            except PlanError as exc:
                next_source_error(exc)  # The projection records degraded sources.
        NEXT_READS.map(prefetch_pulls, sources)
    for source in sources:
        repo = source.get("repo")
        if not isinstance(repo, str) or repo.casefold() in seen:
            continue
        seen.add(repo.casefold())
        if source.get("exclusion") in {"other_owner", "archived_or_disabled", "invalid_repository_inventory_entry", "empty_without_open_issues"}:
            continue
        context: dict[str, Any] = {"repo": repo}
        coverage["repositories"].append(context)
        if source.get("hold"):
            context.update(exclusion="repository_held", hold=source["hold"])
            continue
        try:
            _, pulls = collect_paged_rest_items(
                f"/repos/{repo}/pulls",
                query={"state": "open", "sort": "created", "direction": "asc"},
                bucket="rest_core", step_prefix="next_dependabot_pulls", limit=scan_limit + 1,
            )
        except PlanError as exc:
            context.update(complete=False, error=next_source_error(exc))
            coverage["complete"] = False
            continue
        context.update(complete=len(pulls) <= scan_limit,
                       truncated=len(pulls) > scan_limit, pull_request_count=min(len(pulls), scan_limit))
        coverage["complete"] &= context["complete"]
        observations: list[dict[str, Any]] = []
        for pull in pulls[:scan_limit]:
            if pull.get("state") != "open" or (pull.get("user") or {}).get("login") != "dependabot[bot]":
                continue
            try:
                created = datetime.fromisoformat(pull["created_at"].replace("Z", "+00:00"))
                age = (observed_at - created).total_seconds()
            except (KeyError, ValueError, TypeError, AttributeError):
                context["complete"] = coverage["complete"] = False
                context["invalid_created_at"] = True
                continue
            if age <= 24 * 60 * 60:
                continue
            observations.append({
                "repo": repo, "number": pull["number"], "url": pull["html_url"],
                "title": pull["title"], "record_type": "pull_request",
                "created_at": pull["created_at"], "age_hours": round(age / 3600, 1),
                "age": f"{age / 3600:.1f} hours", "head_sha": (pull.get("head") or {}).get("sha"),
                "base_branch": (pull.get("base") or {}).get("ref"),
                "reasons": ["dependabot_open_more_than_one_day"],
                "next_action": "Review the PR discussion, changelog, compatibility and current ownership before taking work.",
                "ownership": "not_checked",
            })
        if not observations:
            context["enrollment"] = {"source": "launchplane", "status": "not_read", "reason": "no_old_dependabot_prs"}
            continue
        enrollment: dict[str, Any] = ({"source": "launchplane", "status": "unknown",
                                      "reason": "earlier_enrollment_read_unavailable"}
                                     if enrollment_unavailable else read_next_train_enrollment(repo))
        status = enrollment["status"]
        context["enrollment"] = enrollment
        if status == "not_enrolled":
            continue
        if status == "unknown":
            enrollment_unavailable = True
            context["complete"] = coverage["complete"] = False
        branches = {target["baseBranch"] for target in enrollment.get("targets", [])}
        for observation in observations:
            if status == "enrolled" and observation["base_branch"] not in branches:
                continue
            observation["enrollment"] = status
            (candidates if status == "enrolled" else unverified).append(observation)
    for items in (candidates, unverified):
        items.sort(key=lambda entry: (datetime.fromisoformat(entry["created_at"].replace("Z", "+00:00")), entry["repo"].casefold(), entry["number"]))
    coverage["result_truncated"] = len(candidates) > limit or len(unverified) > limit
    return {
        "dependabot_candidates": candidates[:limit], "dependabot_candidate_count": len(candidates),
        "dependabot_unverified_candidates": unverified[:limit],
        "dependabot_unverified_candidate_count": len(unverified), "dependabot_context": coverage,
    }


def cmd_next(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    if github_direction_next.is_direction_repository(repo):
        if getattr(args, "batch_read", False):
            raise PlanError("--batch-read is currently repository-local; global next retains REST")
        cmd_direction_next(args, repo)
        return
    if any(getattr(args, name, None) is not None for name in ("repo_limit", "repository_issue_limit")):
        raise PlanError("Global discovery options require an OWNER/direction repository")
    config = load_config(repo)
    actor: str | None = None
    milestone_scope: dict[str, Any] | None = None
    issue_query: dict[str, Any] = {
        "labels": config["labels"]["plan"],
        "state": "open",
        "sort": "created",
        "direction": "asc",
    }
    direction_milestones: list[str] | None = None
    direction_order_source = "explicit_scope" if args.milestone else "milestone_created_at"
    direction_note: str | None = None
    direction_error: str | None = None
    if not args.milestone:
        try:
            direction_text = load_direction(repo)
        except PlanError as exc:
            raise_if_cancelled(exc)
            direction_note = "direction_unavailable"
            direction_error = github_api_core.redact_string(str(exc))
        else:
            if direction_text is not None:
                parsed_titles = direction_milestone_titles(direction_text)
                if parsed_titles:
                    direction_milestones = parsed_titles
                    direction_order_source = f"{repo}:{DIRECTION_FILE}"
                else:
                    direction_note = "direction_milestones_unparsed"
    if args.milestone:
        gh_cmd, expected_actor = milestone_route()
        milestone_result = github_milestone_core.show_milestone(
            repo,
            args.milestone,
            operation=CURRENT_OPERATION,
            actor=None,
            expected_actor=expected_actor,
            gh_cmd=gh_cmd,
        )
        actor = milestone_result.get("actor")
        milestone_scope = milestone_result["milestone"]
        issue_query["milestone"] = milestone_scope["number"]

    graph = read_planning_graph(repo, labels=[config["labels"]["plan"]],
        milestone=issue_query.get("milestone"), limit=NEXT_PLAN_INVENTORY_LIMIT + 1) if getattr(args, "batch_read", False) else None
    if graph is not None:
        page_actor, issues = BATCH_READ_CONTEXT["diagnostics"]["actor"], graph
    else:
        page_actor, issues = collect_paged_rest_items(
            f"/repos/{repo}/issues",
            query=issue_query,
            bucket="rest_core",
            step_prefix="next_plan_issues",
            limit=NEXT_PLAN_INVENTORY_LIMIT + 1,
            issue_only=True,
        )
    actor = page_actor or actor
    inventory_truncated = len(issues) > NEXT_PLAN_INVENTORY_LIMIT
    issues = issues[:NEXT_PLAN_INVENTORY_LIMIT]
    inventory_count = len(issues)
    inventory_issues = list(issues)
    agent = github_agent.running_agent(getattr(args, "agent", None))
    routed_issues: list[dict[str, Any]] = []
    routing_excluded: list[dict[str, Any]] = []
    for issue in issues:
        issue["repo"] = repo
        mismatch = github_agent.exclusion(compact_list_issue(repo, issue), agent)
        if mismatch:
            routing_excluded.append(mismatch)
        else:
            routed_issues.append(issue)
    issues = routed_issues
    rank_next_candidates(issues, direction_milestones=direction_milestones)
    scan_truncated = len(issues) > args.scan_limit
    issues = issues[: args.scan_limit]

    project_actor, focus_by_url, focus_context = next_focus_context(repo, config)
    actor = actor or project_actor
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = routing_excluded
    dependency_degraded_count = 0
    structured_waits: dict[tuple[str, int], dict[str, Any]] = {}
    for issue in issues:
        issue_url = issue.get("html_url") or issue.get("url")
        focus = focus_by_url.get(issue_url) if isinstance(issue_url, str) else None
        static_exclusion = next_static_exclusion(issue, config=config, focus=focus)
        if static_exclusion is not None:
            excluded.append(static_exclusion)
            continue
        relationship_error = None
        relationships = None
        wait = {"version": 1, "state": "review", "evidence": "", "waits": []}
        truncated_relationships: list[str] = []
        try:
            relation_actor, relationships, truncated_relationships = read_next_issue_relationships(
                repo,
                int(issue["number"]),
                issue,
            )
            actor = relation_actor or actor
        except PlanError as exc:
            relationship_error = next_source_error(exc)
        wait_error = None
        comment_limit = getattr(args, "comment_limit", None) or 100
        try:
            if "_graph_comments" in issue:
                comments = issue["_graph_comments"]
            else:
                _, comments = collect_paged_rest_items(
                    f"/repos/{repo}/issues/{issue['number']}/comments", query={}, limit=comment_limit + 1,
                    bucket="rest_core", step_prefix="next_issue_comments")
            if len(comments) > comment_limit:
                wait_error = "Discussion exceeded the comment read bound"
            else:
                wait = current_wait(issue, comments, repo)
        except (ValueError, PlanError) as exc:
            wait_error = next_source_error(exc) if isinstance(exc, PlanError) else str(exc)
        if wait_error is not None:
            wait = {"version": 1, "state": "review", "evidence": wait_error, "waits": []}
        if truncated_relationships:
            relationship_error = (
                f"dependency relationship reads exceeded the per-relationship limit "
                f"of {NEXT_RELATIONSHIP_LIMIT}"
            )
            relationships = None
        structured_waits[(repo.casefold(), int(issue["number"]))] = wait
        disposition, evaluated = evaluate_next_plan(
            issue,
            config=config,
            focus=focus,
            relationships=relationships,
            relationship_error=relationship_error,
            structured_wait=wait,
        )
        if wait_error is not None:
            evaluated.update(wait_error=wait_error, wait_evidence_complete=False)
        if relationship_error is not None:
            dependency_degraded_count += 1
            if truncated_relationships:
                evaluated["truncated_relationships"] = truncated_relationships
        (candidates if disposition == "candidate" else excluded).append(evaluated)

    wait_context = next_wait_context(issues, scan_limit=args.scan_limit,
                                     inventory_complete=not inventory_truncated and not scan_truncated,
                                     structured_waits=structured_waits)
    exclude_landed_candidates(candidates, excluded, wait_context)
    rank_next_candidates(candidates, direction_milestones=direction_milestones)
    if direction_milestones is not None:
        listed = set(direction_milestones)
        for candidate in candidates:
            milestone = candidate.get("milestone")
            if (
                isinstance(milestone, dict)
                and milestone.get("state") == "open"
                and milestone.get("title") not in listed
            ):
                candidate.setdefault("notes", []).append("milestone_unlisted_from_direction")
    try:
        inbound_actor, blocking_work_elsewhere, inbound_context = read_next_inbound_blockers(repo, scan_limit=args.scan_limit)
        actor = inbound_actor or actor
    except PlanError as exc:
        blocking_work_elsewhere = []
        inbound_context = {"complete": False, "error": next_source_error(exc)}
    notes = [
        "native_blocked_by_relationships_are_authoritative",
        "milestones_are_the_execution_order",
        "closed_plans_are_excluded_by_the_open_issue_source_query",
    ]
    if direction_note:
        notes.append(direction_note)
    if inventory_truncated:
        notes.append("plan_inventory_truncated")
    if scan_truncated:
        notes.append("scan_limit_truncated_prioritized_plans")
        notes.append(f"Checked {len(issues)} of {len(routed_issues)} inventoried plans eligible for this agent. Evaluation stopped early; raise --scan-limit to check more.")
    if not focus_context.get("available"):
        notes.append("project_focus_unavailable")
    if focus_context.get("truncated"):
        notes.append("project_focus_truncated")
    if dependency_degraded_count:
        notes.append("dependency_reads_degraded")
    dependabot = next_dependabot_work([{"repo": repo}], scan_limit=args.scan_limit, limit=args.limit)
    selection_context = next_selection_context(args)
    queue_context = annotate_next_queue(candidates, args)
    queue_result = project_local_queue(candidates, excluded, selection_context)
    emit({
        "ok": True,
        "actor": actor,
        "repo": repo,
        "scope": {
            "kind": "milestone" if milestone_scope else "repository",
            "milestone": milestone_scope,
        },
        "milestone_order": {
            "source": direction_order_source,
            "titles": direction_milestones,
            "fallback_reason": direction_note,
            "error": direction_error,
        },
        "focus_context": focus_context,
        **({"batch_read": BATCH_READ_CONTEXT} if getattr(args, "batch_read", False) else {}),
        "dependency_context": {
            "complete": dependency_degraded_count == 0,
            "degraded_count": dependency_degraded_count,
            "relationship_limit": NEXT_RELATIONSHIP_LIMIT,
        },
        "blocking_work_elsewhere": blocking_work_elsewhere,
        "blocking_work_elsewhere_count": len(blocking_work_elsewhere),
        "blocking_work_elsewhere_context": inbound_context,
        "evaluated": len(issues),
        "truncated": inventory_truncated or scan_truncated,
        "inventory_count": inventory_count,
        "inventory_limit": NEXT_PLAN_INVENTORY_LIMIT,
        **queue_result,
        "candidates": queue_result["candidates"][:args.limit],
        "available_candidates": queue_result["available_candidates"][:args.limit],
        "queue_context": queue_context,
        "selection_coverage": github_next_queue.coverage(inventory_issues, [*issues, *routing_excluded], queue_result, active_label=config["labels"]["active"]),
        "stale_wait_report": {key: value for key, value in wait_context.items() if key != "references"},
        "running_agent": agent,
        "notes": notes,
        **dependabot,
    })


def next_selection_context(args: argparse.Namespace) -> dict[str, Any]:
    path = getattr(args, "selection_context", None)
    try:
        return github_direction_next.validate_selection_context(json.loads(pathlib.Path(path).read_text()) if path else {})
    except (OSError, ValueError) as exc:
        raise PlanError(f"Invalid selection context: {exc}") from exc


def next_source_delivery(item: dict[str, Any], trusted: set[str], *, repositories: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the current status before verifying its bounded source-PR links."""
    result: dict[str, Any] = {"complete": True, "merged_prs": [], "completion_proven": False,
                              "scope": "current_status_explicit_pr_references", "all_linked_prs_searched": False}
    discussion = item.get("discussion") or {}
    body_status = re.sub(r"<!--.*?-->", "", section_map(discussion.get("body") or "").get("Current Status", ""), flags=re.S).strip()
    comment_statuses = [re.sub(r"<!--.*?-->", "", section_map(comment.get("body") or "").get("Current Status", ""), flags=re.S).strip()
                        for comment in discussion.get("comments", [])
                        if str(comment.get("author") or "").casefold() in trusted]
    # Activity time cannot choose between disagreeing authored status records,
    # even when no source PR was named or its implementation link is absent.
    comment_statuses = [status for status in comment_statuses if status]
    if body_status and comment_statuses and comment_statuses[-1] != body_status:
        try:
            owner, name = item["repo"].split("/")
            def read_edit() -> tuple[str, Any]:
                return api_json("POST", "/graphql", bucket="graphql", failed_step="next_delivery_body_edit",
                    payload={"query": "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issue(number:$number){createdAt lastEditedAt}}}",
                             "variables": {"owner": owner, "name": name, "number": item["number"]}}, is_write=False,
                    repository=item["repo"])
            try:
                _, edited = (NEXT_READS.observe(f"graphql:delivery_body_edit:{item['repo']}#{item['number']}", read_edit)
                             if NEXT_READS is not None else read_edit())
            except github_next_reads.ReadDeadline as exc:
                raise PlanError(str(exc)) from exc
            data = edited.get("data") if isinstance(edited, dict) else None
            repository = data.get("repository") if isinstance(data, dict) else None
            issue = repository.get("issue") if isinstance(repository, dict) else None
            if not isinstance(issue, dict) or "lastEditedAt" not in issue or not issue.get("createdAt"):
                raise PlanError("Source action body-edit evidence unavailable")
            result["body_recorded_at"] = issue["lastEditedAt"] or issue["createdAt"]
            try:
                recorded = datetime.fromisoformat(result["body_recorded_at"].replace("Z", "+00:00"))
                if recorded.tzinfo is None:
                    raise ValueError("Body edit timestamp lacks timezone")
            except (AttributeError, TypeError, ValueError) as exc:
                raise PlanError("Source action body-edit time unavailable") from exc
        except PlanError as exc:
            result["complete"] = False
            result["body_edit_error"] = next_source_error(exc)
            return result
    refs = github_next_queue.delivery_references(item, trusted, body_recorded_at=result.get("body_recorded_at"))
    if len(refs) > NEXT_DELIVERY_REFERENCE_LIMIT:
        result.update(complete=False, omitted_reference_count=len(refs) - NEXT_DELIVERY_REFERENCE_LIMIT)
    repositories = repositories if repositories is not None else {}
    for repo, number in refs[:NEXT_DELIVERY_REFERENCE_LIMIT]:
        if NEXT_READS is not None and NEXT_READS.expired:
            result.update(complete=False, unread_reference_count=len(refs[:NEXT_DELIVERY_REFERENCE_LIMIT]) - refs[:NEXT_DELIVERY_REFERENCE_LIMIT].index((repo, number)))
            break
        try:
            _, pull = api_json("GET", f"/repos/{repo}/pulls/{number}", bucket="rest_core", failed_step="next_delivery_pull")
            if not isinstance(pull, dict) or "merged_at" not in pull:
                raise PlanError("Source PR merge evidence unavailable")
            if not pull["merged_at"]:
                continue
            try:
                merged = datetime.fromisoformat(pull["merged_at"].replace("Z", "+00:00"))
                if merged.tzinfo is None:
                    raise ValueError("Merge timestamp lacks timezone")
            except (TypeError, AttributeError, ValueError) as exc:
                raise PlanError("Source PR merge timestamp unavailable") from exc
            if repo.casefold() not in repositories:
                _, repositories[repo.casefold()] = api_json("GET", f"/repos/{repo}", bucket="rest_core", failed_step="next_delivery_destination")
            repository = repositories[repo.casefold()]
            default = repository.get("default_branch") if isinstance(repository, dict) else None
            if not default or not (pull.get("base") or {}).get("ref"):
                raise PlanError("Source PR landing destination unavailable")
            if ((pull.get("base") or {}).get("ref") == default
                    and github_plan_claim.pr_issue_reference(pull, item["number"], repo=item["repo"],
                                                            local_references=repo.casefold() == item["repo"].casefold())):
                result["merged_prs"].append({"url": f"https://github.com/{repo}/pull/{number}",
                                             "merged_at": pull["merged_at"], "base": default})
        except PlanError as exc:
            result["complete"] = False
            result.setdefault("unavailable", []).append({"repo": repo, "number": number, "reason": next_source_error(exc)})
    return result

def annotate_next_queue(items: list[dict[str, Any]], args: argparse.Namespace, *, parallel: bool = True) -> dict[str, Any]:
    """Read ownership once per repository; project every evaluated current step."""
    if parallel and NEXT_READS is not None and NEXT_READS.workers > 1:
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            groups.setdefault(item["repo"], []).append(item)
        results = NEXT_READS.map(lambda group: annotate_next_queue(group, args, parallel=False), list(groups.values()))
        return {"source": "issue_discussion_and_current_claim_pr_branch_reads", "claim_required": True,
                "all_active_sessions_searched": False,
                "repositories": {repo: value for result in results for repo, value in result["repositories"].items()}}
    evidence: dict[str, dict[str, Any]] = {}
    delivery_repositories: dict[str, Any] = {}
    comment_limit = getattr(args, "comment_limit", None) or 100
    for item in items:
        if item.get("queue"):
            continue
        repo = item["repo"]
        trusted = {login.casefold() for login in wait_authors(repo)}
        if not item.get("discussion"):
            try:
                _, raw = get_issue(str(item["number"]), repo)
                _, comments = collect_paged_rest_items(f"/repos/{repo}/issues/{item['number']}/comments",
                    query={}, bucket="rest_core", step_prefix="next_queue_comments", limit=comment_limit + 1)
                item["discussion"] = github_direction_next.discussion_snapshot(raw, comments[:comment_limit], complete=len(comments) <= comment_limit)
            except PlanError as exc:
                item["queue"] = {"state": "review", "reason": "incomplete_discussion", "detail": next_source_error(exc)}
                continue
        if repo not in evidence:
            observed: dict[str, Any] = {"complete": False, "coverage": {"sessions": "partial; claim rechecks current sessions"}}
            evidence[repo] = observed
            try:
                bound = NEXT_PLAN_INVENTORY_LIMIT
                _, pulls = collect_paged_rest_items(f"/repos/{repo}/pulls", query={"state": "open", "sort": "created", "direction": "asc"},
                    bucket="rest_core", step_prefix="next_queue_pulls", limit=bound + 1)
                _, branches = collect_paged_rest_items(f"/repos/{repo}/branches", query={},
                    bucket="rest_core", step_prefix="next_queue_branches", limit=bound + 1)
                observed.update(pulls=pulls[:bound], complete=len(pulls) <= bound and len(branches) <= bound,
                    inventory={"remote_branches": [branch["name"] for branch in branches[:bound]],
                               "local_branches": [], "worktrees": [], "sessions": []})
                observed["coverage"].update(pull_count=min(len(pulls), bound), branch_count=min(len(branches), bound),
                    truncated=not observed["complete"], local_artifacts="not_read")
                # Local artifacts are supplementary evidence. Do not claim a
                # complete peer inventory even when the native registry works.
                target = pathlib.Path.home() / "Developer" / repo.split("/")[1]
                if target.is_dir() and not (NEXT_READS and NEXT_READS.expired):
                    try:
                        local = github_plan_claim.local_inventory(repo, 0, cwd=target,
                            **({"deadline_at": NEXT_READS.deadline} if NEXT_READS else {}))
                        observed["inventory"] = local
                        observed["coverage"].update(local_artifacts="read", sessions=local.get("session_coverage", {}))
                    except (ValueError, OSError, subprocess.SubprocessError) as exc:
                        observed["coverage"]["local_error"] = str(exc)
            except PlanError as exc:
                observed["coverage"]["error"] = next_source_error(exc)
        preliminary = github_next_queue.classify(item, evidence[repo], trusted=trusted)
        if preliminary.get("state") not in {"underway", "waiting"} and (item.get("discussion") or {}).get("complete") is True:
            item["source_delivery"] = next_source_delivery(item, trusted, repositories=delivery_repositories)
        item["queue"] = github_next_queue.classify(item, evidence[repo], trusted=trusted)
    return {"source": "issue_discussion_and_current_claim_pr_branch_reads", "claim_required": True,
            "all_active_sessions_searched": False,
            "repositories": {repo: {"complete": value["complete"], **value["coverage"]} for repo, value in evidence.items()}}


def project_local_queue(candidates: list[dict[str, Any]], excluded: list[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any]:
    groups: dict[str, Any] = {"candidates": [], "available_candidates": [], "underway": [], "verification_only": [], "scope_review": [], "excluded": list(excluded)}
    reviews = {key.casefold(): review for key, review in context.get("issues", {}).items()}
    for item in candidates:
        hold = github_direction_next.repository_hold(context, item["repo"])
        state = (item.get("queue") or {}).get("state", "review")
        review = reviews.get(f"{item['repo']}#{item['number']}".casefold(), {})
        if hold:
            groups["excluded"].append({**item, "exclusion": "repository_held", "review": hold})
        elif state in {"underway", "verification", "scope_review"} and not (state == "scope_review" and github_direction_next.reviewed_remaining_scope(item, review)):
            groups[{"verification": "verification_only"}.get(state, state)].append(item)
        elif state == "waiting":
            groups["excluded"].append({**item, "exclusion": "recorded_wait"})
        else:
            review = reviews.get(f"{item['repo']}#{item['number']}".casefold(), {})
            discussion = item.get("discussion") or {}
            complete = discussion.get("complete")
            if isinstance(complete, bool) and complete and review.get("discussion_digest") == discussion.get("digest"):
                if review.get("state") == "underway":
                    groups["underway"].append({**item, "availability": "underway", "review": review})
                    continue
                if review.get("state") in {"waiting", "ineligible"}:
                    groups["excluded"].append({**item, "exclusion": "reviewed_wait" if review["state"] == "waiting" else "outside_owner_direction", "review": review})
                    continue
            row = {**item, "availability": "available" if state == "available" else "needs_review"}
            if (isinstance(complete, bool) and complete and review.get("discussion_digest") == discussion.get("digest")
                    and review.get("state") == "available" and review.get("ownership_complete") is True
                    and (item.get("queue") or {}).get("observed_ownership_complete") is True
                    and ((item.get("queue") or {}).get("reason") in {"current_scope_needs_review", "current_action_not_recorded"}
                         or github_direction_next.reviewed_remaining_scope(item, review))):
                row.update(availability="available", review=review)
            groups["candidates"].append(row)
            if row["availability"] == "available":
                groups["available_candidates"].append(row)
    for field, count in (("candidates", "candidate_count"), ("available_candidates", "available_candidate_count"),
                         ("underway", "underway_count"), ("verification_only", "verification_only_count"), ("scope_review", "scope_review_count")):
        groups[count] = len(groups[field])
    groups["review_required_count"] = len(groups["candidates"]) - len(groups["available_candidates"])
    return groups


def next_source_error(exc: PlanError) -> str:
    # An App-installation refusal is unread coverage, just like a denied or
    # missing record. Never switch actor to complete an optional selection read.
    # Credential rejection, quota and provider failures retain their stop policy.
    if isinstance(exc, ClassifiedPlanError):
        raise exc
    metadata = exc.api_result or {}
    effective_deadline = metadata.get("effective_deadline")
    budget_refusal = (NEXT_READS is not None and exc.failure and exc.failure.cause == "deadline_exceeded" and (
        NEXT_READS.expired or (metadata.get("retry_exhausted_reason") == "deadline_exceeded"
            and type(effective_deadline) in {int, float} and NEXT_READS.deadline is not None
            and effective_deadline == NEXT_READS.deadline)))
    if NEXT_READS is not None and (isinstance(exc.__cause__, github_next_reads.ReadDeadline) or budget_refusal):
        NEXT_READS.budget_stop(exc.failure.cause if exc.failure else "read_budget_exhausted",
                              error=exc.__cause__ or exc, retry_at=metadata.get("retry_at"))
        return "global next read deadline prevents this evidence read"
    if exc.failure is not None and exc.failure.cause not in {"permission_denied", "not_found", "identity_refused"}:
        raise exc
    return github_api_core.redact_string(str(exc))


def read_next_parent(repo: str, number: int) -> dict[str, Any] | None:
    try:
        _, parent = api_json("GET", f"/repos/{repo}/issues/{number}/parent", bucket="rest_core", failed_step="next_parent_issue")
    except PlanError as exc:
        if (exc.api_result or {}).get("status") == 404:
            return None
        raise
    ref = compact_relationship_issue(parent, "parent")
    return {**parent, "repo": ref["repo"]}


def discover_direction_work(
    repo: str, args: argparse.Namespace, *, selection_context: dict[str, Any],
    capacity_evidence: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Inventory the current actor's owner repositories, including non-plan issues.

    No name/label heuristic can decide product vs tooling or override an owner
    hold. Return merged direction and explicit source coverage for that review.
    """
    repo_limit = getattr(args, "repo_limit", None) or 100
    issue_limit = getattr(args, "repository_issue_limit", None) or NEXT_DEFAULT_SCAN_LIMIT
    app_configured = github_identity.github_app_config() is not None
    path = "/installation/repositories" if app_configured else "/user/repos"
    coverage: dict[str, Any] = {
        "source": path, "owner": repo.split("/")[0], "repository_limit": repo_limit,
        "issue_limit_per_repository": issue_limit, "repositories": [],
        "complete": True, "capacity_complete": True, "inventory_truncated": False,
        "scope": "repositories accessible to the configured actor; owner repositories only",
        "outside_scope": ["repositories not accessible to this actor", "closed issues", "pull requests", "active session inventory"],
    }
    try:
        _, repositories = collect_paged_rest_items(
            path, query={} if app_configured else {"affiliation": "owner,collaborator,organization_member"},
            collection_key="repositories" if app_configured else None,
            bucket="rest_core", step_prefix="next_repository_inventory", limit=repo_limit + 1,
        )
    except PlanError as exc:
        coverage.update(complete=False, error=next_source_error(exc), inventory_count=None)
        return [], coverage
    coverage["inventory_count"] = min(len(repositories), repo_limit)
    coverage["inventory_truncated"] = len(repositories) > repo_limit
    coverage["complete"] = not coverage["inventory_truncated"]
    queues: list[list[dict[str, Any]]] = []
    def read_repository(repository: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], bool, bool]:
        name = repository.get("full_name")
        source: dict[str, Any] = {"repo": name}
        source_complete = source_capacity = True
        if not isinstance(name, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", name):
            source.update(exclusion="invalid_repository_inventory_entry")
            source_complete = False
            return source, [], source_complete, source_capacity
        reason = None
        if name.split("/")[0].casefold() != coverage["owner"].casefold():
            reason = "other_owner"
        elif not repository_scope.includes_repository(repository, include_archived=getattr(args, "include_archived", False)) or repository.get("disabled"):
            reason = "archived_or_disabled"
        elif repository.get("has_issues") is False:
            reason = "issues_disabled"
        elif repository.get("size") == 0 and repository.get("open_issues_count") == 0:
            reason = "empty_without_open_issues"
        hold = github_direction_next.repository_hold(selection_context, name)
        if hold:
            source["hold"] = hold
        if hold and reason is None:
            reason = "repository_held"
        if reason:
            source["exclusion"] = reason
            if reason != "repository_held" or not capacity_evidence:
                if reason == "repository_held":
                    source_capacity = False
                return source, [], source_complete, source_capacity
        # A hold forbids selection, not the read-only inventory needed to prove
        # portfolio-wide milestone waits. rank_portfolio_work still excludes it.
        try:
            source["direction"] = load_direction(name)
            _, issues = collect_paged_rest_items(
                f"/repos/{name}/issues", query={"state": "open", "sort": "updated", "direction": "desc"},
                bucket="rest_core", step_prefix="next_repository_issues", limit=issue_limit + 1, issue_only=True,
            )
            source.update(issue_count=min(len(issues), issue_limit), truncated=len(issues) > issue_limit)
            source_capacity &= not source["truncated"]
            if not hold:
                source_complete &= not source["truncated"]
            listed: list[dict[str, Any]] = [{**issue, "repo": name} for issue in issues[:issue_limit]]
            if source["truncated"]:
                # A quiet incident can lie beyond even the per-repository list.
                # Keep a separate bounded label inventory, independent of scan_limit.
                try:
                    _, incidents = collect_paged_rest_items(
                        f"/repos/{name}/issues",
                        query={"state": "open", "labels": github_direction_next.LIVE_BREAKAGE_LABEL},
                        bucket="rest_core", step_prefix="next_live_breakage_issues",
                        limit=NEXT_PLAN_INVENTORY_LIMIT + 1, issue_only=True,
                    )
                    source["live_breakage_inventory"] = {
                        "count": min(len(incidents), NEXT_PLAN_INVENTORY_LIMIT),
                        "limit": NEXT_PLAN_INVENTORY_LIMIT,
                        "complete": len(incidents) <= NEXT_PLAN_INVENTORY_LIMIT,
                    }
                    listed_by_number = {issue["number"]: issue for issue in listed}
                    listed_by_number.update({issue["number"]: {**issue, "repo": name} for issue in incidents[:NEXT_PLAN_INVENTORY_LIMIT]})
                    listed = list(listed_by_number.values())
                except PlanError as exc:
                    source["live_breakage_inventory"] = {"complete": False, "error": next_source_error(exc)}
                    source_capacity = False
                    if not hold:
                        source_complete = False
            prioritized: list[dict[str, Any]] = [{**compact_list_issue(name, issue), "milestone": next_milestone_context(issue)} for issue in listed]
            rank_next_candidates(prioritized, direction_milestones=direction_milestone_titles(source["direction"]) if source["direction"] else None)
            ranks: dict[int, int] = {int(item["number"]): int(item["rank"]) for item in prioritized}
            listed.sort(key=lambda issue: ranks[issue["number"]])
            # Round-robin sources so a large tooling backlog cannot consume the
            # whole evaluation budget before smaller own projects are inspected.
            return source, listed, source_complete, source_capacity
        except PlanError as exc:
            source.update(error=next_source_error(exc), exclusion="repository_held" if hold else "source_unavailable")
            source_capacity = False
            if not hold:
                source_complete = False
        return source, [], source_complete, source_capacity
    ordered_repositories = sorted(repositories[:repo_limit], key=lambda item: str(item.get("full_name", "")).casefold())
    results = NEXT_READS.map(read_repository, ordered_repositories) if NEXT_READS else [read_repository(row) for row in ordered_repositories]
    for source, listed, complete, capacity_complete in results:
        coverage["repositories"].append(source)
        coverage["complete"] &= complete
        coverage["capacity_complete"] &= capacity_complete
        if listed:
            queues.append(listed)
    coverage["capacity_complete"] &= coverage["complete"]
    ordered = [queue[index] for index in range(max((len(queue) for queue in queues), default=0)) for queue in queues if index < len(queue)]
    coverage["issue_inventory_count"] = len(ordered)
    return ordered, coverage


def repository_direction_milestones(source: dict[str, Any]) -> list[str] | None:
    """Keep absent/empty waypoints distinct from an unread or unparsed source."""
    if source.get("error") or "direction" not in source:
        return None
    text = source.get("direction")
    if text is None:
        return []
    titles = direction_milestone_titles(text)
    if not re.search(r"(?m)^##\s+Milestones\s*$", text):
        return None
    if not titles and section_map(text).get("Milestones", "").strip():
        return None
    return titles


def exclude_landed_candidates(candidates: list[dict[str, Any]], excluded: list[dict[str, Any]],
                              report: dict[str, Any]) -> None:
    checked = {(row["repo"].casefold(), row["number"]) for row in report["checked_issues"]}
    unread = {(row["repo"].casefold(), row["number"]) for row in report["unavailable"]}
    active = {(row["repo"].casefold(), row["number"]): row for row in report["items"]
              if "completion_proven" in row}
    for entry in list(candidates):
        key = (entry["repo"].casefold(), entry["number"])
        entry.pop("post_merge_evidence_complete", None)
        candidate_labels = set(normalize_labels(entry.get("labels")))
        scope_labels = report.get("scope_labels", {}).get(entry["repo"].casefold(), ["plan:active", "plan:waiting", "plan:blocked"])
        if key in unread or (key not in checked and candidate_labels.intersection(scope_labels)):
            entry["post_merge_evidence_complete"] = False
        evidence = active.get(key)
        if evidence:
            entry["post_merge_evidence"] = evidence
        if evidence and evidence.get("selection_exclusion"):
            candidates.remove(entry)
            excluded.append({**entry, "exclusion": "landed_status_needs_reconciliation",
                             "post_merge_evidence": evidence})


def next_wait_context(
    issues: list[dict[str, Any]], *, scan_limit: int, inventory_complete: bool,
    reference_repositories: set[str] | None = None,
    structured_waits: dict[tuple[str, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reuse the direction audit's stale report within the already-read scope."""
    root = str(pathlib.Path(__file__).resolve().parents[3])
    if root not in sys.path:
        sys.path.insert(0, root)
    from skills.direction.scripts import direction_audit

    cache: dict[str, Any] = {}
    read_paths: set[str] = set()
    fetch_lock = threading.Lock()
    unavailable: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    failures: dict[str, direction_audit.AuditError] = {}
    budget = max(1, scan_limit) * 5

    def fetch(argv: list[str]) -> Any:
        path = "/" + argv[1].lstrip("/")
        if path not in cache:
            with fetch_lock:
                if path not in read_paths and len(read_paths) >= budget:
                    raise direction_audit.AuditError("wait read limit reached")
                read_paths.add(path)
            try:
                _, cache[path] = api_json("GET", path, bucket="rest_core", failed_step="next_wait_evidence")
            except PlanError as exc:
                # Unavailable records, including App-installation refusals,
                # degrade coverage through the same policy as other next reads.
                detail = next_source_error(exc)
                missing = exc.failure is not None and exc.failure.cause == "not_found"
                cache[path] = (direction_audit.ReferenceNotFound if missing else direction_audit.AuditError)(detail)
        if isinstance(cache[path], direction_audit.AuditError):
            raise cache[path]
        return cache[path]

    known_repos = {item["repo"] for item in issues} | (reference_repositories or set())
    configs = {repo: load_config(repo) for repo in {item["repo"] for item in issues}}
    unique = {(item["repo"].casefold(), item["number"]): item for item in issues
              if set(normalize_labels(item.get("labels"))).intersection(
                  configs[item["repo"]]["labels"][key] for key in ("active", "waiting", "blocked"))
              or (structured_waits or {}).get((item["repo"].casefold(), item["number"]), {}).get("state") == "waiting"}
    groups: dict[str, list[dict[str, Any]]] = {}
    selected = list(unique.values())[:scan_limit]
    for item in selected:
        groups.setdefault(item["repo"], []).append(item)
    reports: list[dict[str, Any]] = []
    complete = inventory_complete and len(unique) <= scan_limit
    references: dict[str, dict[str, Any]] = {}
    checked = 0
    scope_labels = {}
    repositories = {}
    frozen_issues = []
    checked_issue_refs = []
    def read_report(group: tuple[str, list[dict[str, Any]]]) -> tuple[str, dict[str, Any]]:
        repo, items = group
        return repo, direction_audit.stale_wait_report(items, repo, fetch=fetch, inventory_complete=complete,
                                                      active_label=configs[repo]["labels"]["active"])
    if (NEXT_READS and NEXT_READS.workers > 1
            and all(len(items) <= direction_audit.MAX_PAGES * 100 for items in groups.values())):
        # One large repository must not serialize the whole wait-evidence pass.
        # Keep the existing report/classifier and its shared bounded fetch cache.
        group_reports: dict[str, dict[str, Any]] = {}
        individual = [(repo, [item]) for repo, items in groups.items() for item in items]
        for repo, report in NEXT_READS.map(read_report, individual):
            if repo not in group_reports:
                group_reports[repo] = report
                continue
            combined = group_reports[repo]
            combined["complete"] &= report["complete"]
            combined["checked"] += report["checked"]
            for field in ("items", "checked_issues", "frozen_issues"):
                combined[field].extend(report[field])
            for field in ("unavailable", "unresolved_references"):
                for error in report[field]:
                    if error not in combined[field]:
                        combined[field].append(error)
    else:
        group_reports = dict(read_report(group) for group in groups.items())
    reference_paths = list(dict.fromkeys(
        f"repos/{reference['repo']}/issues/{reference['number']}"
        for repo, items in groups.items() if group_reports[repo]["repository"]["archived"] is not True
        for item in items if str(item.get("state", "")).casefold() == "open"
        for row in github_direction_next.waiting_records(
            {**compact_list_issue(repo, item), "plan_status": next_plan_status(item, configs[repo]),
             **({"structured_wait": structured_waits[(repo.casefold(), item["number"])]}
                if (repo.casefold(), item["number"]) in (structured_waits or {}) else {})},
            section_map(item.get("body") or "").get("Current Status", ""), reference_repositories=known_repos)
        for reference in row["references"]))
    def read_reference(path: str) -> None:
        try:
            fetch(["api", path])
        except direction_audit.AuditError:
            pass  # The normal projection below retains each unread reference.
    if NEXT_READS:
        NEXT_READS.map(read_reference, reference_paths)
    for repo, items in groups.items():
        config = configs[repo]
        report = group_reports[repo]
        reports.extend({**row, "repo": repo} for row in report["items"])
        for row in report["unavailable"]:
            if row["source"] == "repository":
                unavailable.extend({**row, "repo": repo, "number": item["number"]} for item in items)
            else:
                unavailable.append({**row, "repo": repo})
        unresolved.extend({**row, "repo": repo} for row in report["unresolved_references"])
        checked += report["checked"]
        checked_issue_refs.extend({"repo": repo, "number": number} for number in report["checked_issues"])
        scope_labels[repo.casefold()] = [config["labels"][key] for key in ("active", "waiting", "blocked")]
        complete &= report["complete"]
        repositories[repo] = report["repository"]
        frozen_issues.extend({**row, "repo": repo} for row in report["frozen_issues"])
        if report["repository"]["archived"] is True:
            continue
        for item in items:
            if str(item.get("state", "")).casefold() != "open":
                continue
            status = section_map(item.get("body") or "").get("Current Status", "")
            own = {**compact_list_issue(repo, item), "plan_status": next_plan_status(item, config)}
            wait = (structured_waits or {}).get((repo.casefold(), item["number"]))
            if wait is not None:
                own["structured_wait"] = wait
            for row in github_direction_next.waiting_records(own, status, reference_repositories=known_repos):
                for reference in row["references"]:
                    url = reference["url"]
                    stored = {key: value for key, value in reference.items() if key != "bare"}
                    if url not in references:
                        try:
                            target = fetch(["api", f"repos/{reference['repo']}/issues/{reference['number']}"])
                            state = target.get("state") if isinstance(target, dict) else None
                            if state not in {"open", "closed"}:
                                raise direction_audit.AuditError("unknown wait reference state")
                            references[url] = {**stored, "state": state, "closed_at": target.get("closed_at"),
                                               "state_reason": target.get("state_reason")}
                        except direction_audit.AuditError as exc:
                            references[url] = {**stored, "state": "unknown"}
                            failures[url] = exc
                    if url not in failures:
                        continue
                    # The read is shared; whether it leaves this issue unread depends on how it was written.
                    if direction_audit.bare_reference_missing(failures[url], not reference.get("bare")):
                        # Absent here, so read; the wait stays pending on its unknown state.
                        gap = {"repo": repo, "number": item["number"], "source": "wait_reference",
                               "reason": "unresolved_reference", "reference": f"#{reference['number']}"}
                        if gap not in unresolved:
                            unresolved.append(gap)
                        continue
                    gap = {"repo": repo, "number": item["number"], "source": "wait_reference", "url": url, "reason": "unavailable"}
                    if gap not in unavailable:
                        unavailable.append(gap)
                    complete = False
    return {"read_only": True, "complete": complete, "checked": checked, "items": reports,
            "repositories": repositories, "frozen_issues": frozen_issues,
            "unavailable": unavailable, "unresolved_references": unresolved, "references": references,
            "inventory_complete": inventory_complete, "scope": "evaluated_global_next_issues",
            "read_limit": budget, "read_count": len(cache),
            "checked_issues": checked_issue_refs,
            "scope_labels": scope_labels}


def cmd_direction_next(args: argparse.Namespace, repo: str) -> None:
    global NEXT_READS
    try:
        with github_next_reads.NextReads(workers=getattr(args, "read_workers", 1),
                                         seconds=getattr(args, "time_budget", 0)) as reads:
            NEXT_READS = reads
            direction_next(args, repo)
    finally:
        NEXT_READS = None


def direction_next(args: argparse.Namespace, repo: str) -> None:
    assert NEXT_READS is not None
    NEXT_READS.start_phase("milestone_graph")
    selection_context = next_selection_context(args)
    direction_text = load_direction(repo)
    titles = direction_milestone_titles(direction_text or "")
    if direction_text is None or not re.search(r"(?m)^##\s+Milestones\s*$", direction_text):
        raise PlanError(f"Global next needs the ordered milestones in {repo}/{DIRECTION_FILE}")
    if not titles and section_map(direction_text).get("Milestones", "").strip():
        raise PlanError(f"Could not parse the nonempty Milestones section in {repo}/{DIRECTION_FILE}")
    gh_cmd, expected_actor = milestone_route()
    milestone_result = github_milestone_core.list_milestones(
        repo, state="all", limit=NEXT_PLAN_INVENTORY_LIMIT + 1,
        operation=CURRENT_OPERATION, actor=None, expected_actor=expected_actor, gh_cmd=gh_cmd,
    )
    record_retry_metadata(milestone_result)
    milestone_inventory = milestone_result["milestones"]
    milestones_truncated = len(milestone_inventory) > NEXT_PLAN_INVENTORY_LIMIT
    completed_titles = [
        milestone["title"] for milestone in milestone_inventory[:NEXT_PLAN_INVENTORY_LIMIT]
        if milestone.get("state") == "closed"
    ]
    scope = None
    wait_titles = list(titles)
    if args.milestone:
        result = github_milestone_core.show_milestone(
            repo, args.milestone, operation=CURRENT_OPERATION,
            actor=None, expected_actor=expected_actor, gh_cmd=gh_cmd,
        )
        record_retry_metadata(result)
        scope = result["milestone"]
        if scope["title"] not in titles:
            raise PlanError("The selected milestone is not listed in the owner's direction")
        titles = [scope["title"]]
    config = load_config(repo)
    query = {"labels": config["labels"]["plan"], "state": "open", "sort": "created", "direction": "asc"}
    if scope:
        query["milestone"] = scope["number"]
    actor, issues = collect_paged_rest_items(
        f"/repos/{repo}/issues", query=query, bucket="rest_core",
        step_prefix="next_plan_issues", limit=NEXT_PLAN_INVENTORY_LIMIT + 1, issue_only=True,
    )
    inventory_truncated = len(issues) > NEXT_PLAN_INVENTORY_LIMIT
    issues = issues[:NEXT_PLAN_INVENTORY_LIMIT]
    seeds = {(repo.casefold(), item["number"]): {**item, "repo": repo} for item in issues}
    client_records: dict[str, dict[str, Any]] = {}
    client_bot_logins = github_identity.configured_bot_logins()
    contexts: dict[str, tuple[dict[str, Any], dict[str, str]]] = {}
    focus_contexts: dict[str, dict[str, Any]] = {}
    nodes: dict[tuple[str, int], dict[str, Any]] = {}
    parents: dict[tuple[str, int], dict[str, Any] | None] = {}
    context_lock = threading.Lock()
    context_locks: dict[str, Any] = {}
    graph_read_ahead: set[tuple[str, int]] = set()

    def load_node(issue_repo: str, number: int) -> dict[str, Any]:
        nonlocal actor
        key = (issue_repo.casefold(), number)
        if key in nodes:
            return nodes[key]
        raw_issue = seeds.get(key)
        try:
            if raw_issue is None:
                issue_actor, raw_issue = get_issue(str(number), issue_repo)
                actor = issue_actor or actor
            seeds[key] = {**raw_issue, "repo": issue_repo}
            context_key = issue_repo.casefold()
            with context_lock:
                repo_lock = context_locks.setdefault(context_key, threading.Lock())
            with repo_lock:
                if context_key not in contexts:
                    target_config = load_config(issue_repo)
                    focus_actor, focus_values, focus_context = next_focus_context(issue_repo, target_config)
                    actor = focus_actor or actor
                    contexts[context_key] = (target_config, focus_values)
                    focus_contexts[issue_repo] = focus_context
            target_config, focus_values = contexts[context_key]
            focus = focus_values.get(raw_issue.get("html_url") or raw_issue.get("url"))
            base = {
                **compact_list_issue(issue_repo, raw_issue),
                "plan_status": next_plan_status(raw_issue, target_config),
                "focus": focus,
                "milestone": next_milestone_context(raw_issue),
            }
            if raw_issue.get("pull_request") is not None:
                return {"item": {**base, "exclusion": "pull_request"}}
            comment_limit = getattr(args, "comment_limit", None) or 100
            if type(raw_issue.get("comments")) is int and raw_issue["comments"] == 0:
                comments = []
            else:
                _, comments = collect_paged_rest_items(
                    f"/repos/{issue_repo}/issues/{number}/comments", query={}, bucket="rest_core",
                    step_prefix="next_issue_comments", limit=comment_limit + 1,
                )
            complete = len(comments) <= comment_limit
            try:
                wait = current_wait(raw_issue, comments[:comment_limit], issue_repo) if complete else None
            except (ValueError, KeyError, TypeError):
                wait = None
            except PlanError as exc:
                if exc.failure is not None:
                    raise
                wait = None
            if wait is None:
                wait = {"version": 1, "state": "review", "evidence": "", "waits": []}
            static = next_static_exclusion(raw_issue, config=target_config, focus=focus)
            if static:
                evaluated_node = {"item": static}
            else:
                relation_actor, relationships, truncated = read_next_issue_relationships(issue_repo, number, raw_issue)
                actor = relation_actor or actor
                evaluated_node = github_direction_next.evaluate_direction_node(
                    raw_issue, config=target_config, focus=focus, relationships=relationships,
                    truncated_relationships=truncated, structured_wait=wait,
                )
            evaluated_node["item"]["discussion"] = github_direction_next.discussion_snapshot(
                raw_issue, comments[:comment_limit], complete=complete,
            )
            return evaluated_node
        except PlanError as exc:
            # Inaccessible cross-owner nodes are unknown, not unblocked. Quota,
            # auth and provider failures retain the shared helper's stop policy.
            return {"item": {
                "repo": issue_repo, "number": number,
                "url": f"https://github.com/{issue_repo}/issues/{number}",
                "exclusion": "unknown_dependencies", "detail": next_source_error(exc),
            }}

    def parent_observation(key: tuple[str, int]) -> dict[str, Any] | PlanError | None:
        try:
            return read_next_parent(*key)
        except PlanError as exc:
            next_source_error(exc)
            return exc

    def prefetch_parent(key: tuple[str, int], future: Any) -> None:
        node = future.result()
        item = node["item"]
        reason = item.get("exclusion")
        wait = github_direction_next.milestone_wait_evidence(item, node.get("status_text") or "", titles)
        listed_wait = reason in {"waiting", "wait_review_required"} and ((item.get("milestone") or {}).get("title") in titles or not wait["valid"])
        if ((not reason or capacity_evidence or listed_wait)
                and reason not in {"completed", "pull_request", "unknown_dependencies"}):
            parent_observation(key)

    def prefetch_nodes(refs: list[dict[str, Any]], *, ancestry: bool = False) -> None:
        for ref in refs[:NEXT_READS.workers]:
            key = (ref["repo"].casefold(), ref["number"])
            issue_repo, number = ref["repo"], ref["number"]
            future = NEXT_READS.submit(("node", *key), lambda issue_repo=issue_repo, number=number: load_node(issue_repo, number))
            if ancestry:
                NEXT_READS.submit(("prefetch_parent", *key), lambda key=key, future=future: prefetch_parent(key, future))

    def read_node(issue_repo: str, number: int) -> dict[str, Any]:
        key = (issue_repo.casefold(), number)
        if key not in nodes:
            if NEXT_READS.workers > 1:
                nodes[key] = NEXT_READS.submit(("node", *key), lambda: load_node(issue_repo, number)).result()
            else:
                nodes[key] = load_node(issue_repo, number)
        return nodes[key]

    def prefetch_graph(refs: list[dict[str, Any]]) -> None:
        for ref in refs:
            key = (ref["repo"].casefold(), ref["number"])
            if key in nodes or key in graph_read_ahead:
                continue
            if len(graph_read_ahead.difference(nodes)) >= NEXT_READS.workers:
                break
            graph_read_ahead.add(key)
            prefetch_nodes([ref])

    unused_errors: set[BaseException] = set()
    def unused_failure(exc: BaseException) -> None:
        if (isinstance(exc, PlanError) and not isinstance(exc, ClassifiedPlanError) and exc.failure
                and exc.failure.cause in {"network_provider_failure", "provider_failure"}):
            if exc in unused_errors:
                return
            unused_errors.add(exc)
            cause = exc.failure.cause
            NEXT_READS.read_ahead_failures[cause] = NEXT_READS.read_ahead_failures.get(cause, 0) + 1
            return
        raise exc

    def with_ancestry(leaf_item: dict[str, Any]) -> dict[str, Any]:
        ancestors: list[dict[str, Any]] = []
        key = (leaf_item["repo"].casefold(), leaf_item["number"])
        visited = {key}
        complete = False
        detail = "parent ancestry exceeded the limit of 10 or contains a cycle"
        # GitHub ancestry is separate from the downward graph. Bound it even
        # when a provider returns a cycle or an unexpectedly deep chain.
        for _ in range(10):
            try:
                if key not in parents:
                    parent = (NEXT_READS.submit(("parent", *key), lambda key=key: parent_observation(key)).result()
                              if NEXT_READS.workers > 1 else parent_observation(key))
                    if isinstance(parent, PlanError):
                        raise parent
                    parents[key] = parent
                parent = parents[key]
                if parent is None:
                    complete = True
                    break
                key = (parent["repo"].casefold(), parent["number"])
                if key in visited:
                    break
                visited.add(key)
                seeds[key] = parent
                classified = read_node(parent["repo"], parent["number"])["item"]
                ancestors.append(classified)
                if classified.get("exclusion") == "unknown_dependencies":
                    break
            except PlanError as exc:
                detail = next_source_error(exc)
                break
        annotated_item = github_direction_next.include_parent_context(
            leaf_item, ancestors, complete=complete, tracking_roots=ranked["tracking_roots"],
        )
        if not complete:
            annotated_item["detail"] = detail
        return annotated_item

    roots = [{
        **compact_list_issue(repo, item), "milestone": next_milestone_context(item),
    } for item in issues]
    ranked = github_direction_next.rank_direction_work(
        roots, milestone_titles=titles, read_node=read_node,
        scan_limit=args.scan_limit, completed_milestone_titles=completed_titles,
        agent=github_agent.running_agent(getattr(args, "agent", None)),
        wait_milestone_titles=wait_titles,
        prefetch_nodes=prefetch_graph if NEXT_READS.workers > 1 else None,
    )
    NEXT_READS.read_ahead_unevaluated = len(graph_read_ahead.difference(nodes))
    # Settle the bounded graph look-ahead before taking inventory snapshots.
    for key in graph_read_ahead:
        try:
            NEXT_READS.tasks[("node", *key)].result()
        except BaseException as exc:
            unused_failure(exc)
    ranked["dependency_context"]["relationship_limit"] = NEXT_RELATIONSHIP_LIMIT
    graph_evaluation_note = ("Milestone graph evaluation stopped early. Raise --scan-limit to check more linked issues."
                             if ranked["truncated"] else None)
    if inventory_truncated or milestones_truncated:
        ranked["truncated"] = True
        ranked["dependency_context"]["complete"] = False
    ranked["dependency_context"]["milestone_inventory_truncated"] = milestones_truncated
    graph_coverage = {"complete": ranked["dependency_context"]["complete"], "evaluated": ranked["evaluated"],
                      "truncated": ranked["truncated"], "evaluation_note": graph_evaluation_note}
    # Establish useful native-frontier evidence before optional portfolio work.
    NEXT_READS.start_phase("milestone_ownership")
    graph_queue_context = annotate_next_queue(ranked["candidates"], args)
    NEXT_READS.start_phase("milestone_wait_evidence")
    next_wait_context([seeds[key] for key in nodes if key in seeds], scan_limit=args.scan_limit * 2,
        inventory_complete=graph_coverage["complete"],
        structured_waits={key: node["item"]["structured_wait"] for key, node in nodes.items() if "structured_wait" in node["item"]})
    NEXT_READS.start_phase("repository_inventory")
    discoveries: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []
    unevaluated_milestones: list[dict[str, Any]] = []
    discovery: dict[str, Any] = {"complete": False, "exclusion": "explicit_milestone_scope"}
    preflight = github_direction_next.tooling_capacity_context(
        ranked, [], milestone_titles=titles, context=selection_context,
        repository_waypoints={}, coverage_complete=graph_coverage["complete"],
    )
    capacity_evidence = bool(preflight["admitted"] or (preflight["reason"] in {"no_milestone_waits", "milestone_names_no_person"} and any(
        review.get("state") == "underway" or (review.get("state") == "waiting" and review.get("waiting_on") in {"person", "event"})
        for review in selection_context.get("issues", {}).values()
    )))
    if scope is None:
        inventory, discovery = discover_direction_work(repo, args, selection_context=selection_context, capacity_evidence=capacity_evidence)
        seen = {(item["repo"].casefold(), item["number"]) for item in [*ranked["candidates"], *ranked["excluded"]] if item.get("exclusion") != "outside_direction_tracks"}
        inventory = [item for item in inventory if (item["repo"].casefold(), item["number"]) not in seen]
        waypoints = {source["repo"]: repository_direction_milestones(source)
                     for source in discovery.get("repositories", []) if isinstance(source.get("repo"), str)}
        # Already-inventoried Client requests are milestone evidence even when
        # the scan budget leaves them unevaluated. Resolve each eligible repo once.
        for item in inventory:
            name = item["repo"]
            native = any(entry["repo"].casefold() == name.casefold() and entry.get("via")
                         for entry in [*ranked["candidates"], *ranked["excluded"]])
            if not native and not set(waypoints.get(name) or []).intersection(titles):
                continue
            author = (item.get("user") or {}).get("login")
            if (author and (item.get("user") or {}).get("type") != "Bot"
                    and author.casefold() not in {login.casefold() for login in client_bot_logins}
                    and name.casefold() not in client_records):
                client_records[name.casefold()] = github_client.recorded_client(name)
        inventory = [github_direction_next.with_client_milestone(
            item, ranked, milestone_titles=titles, repository_clients=client_records,
            repository_waypoints=waypoints, director_owner=repo.split("/")[0], bot_logins=client_bot_logins,
        ) for item in inventory]
        scanned = github_direction_next.discovery_scan(
            inventory, args.scan_limit, selection_context,
            agent=github_agent.running_agent(getattr(args, "agent", None)),
        )
        scanned_keys = {(item["repo"].casefold(), item["number"]) for item in scanned}
        skipped_counts: dict[str, int] = {}
        capacity_unevaluated = len(inventory) - len(scanned)
        for item in inventory:
            if (item["repo"].casefold(), item["number"]) not in scanned_keys:
                # Inventory already identifies title-matched milestone work;
                # an ordinary scan bound must not erase that known frontier.
                if (item.get("milestone") or {}).get("title") in titles or item.get("client_request"):
                    unevaluated_milestones.append(compact_list_issue(item["repo"], item))
                if not github_direction_next.repository_hold(selection_context, item["repo"]):
                    skipped_counts[item["repo"]] = skipped_counts.get(item["repo"], 0) + 1
        discovery.update(
            evaluated=len(scanned), scan_limit=args.scan_limit,
            live_breakage_evaluated=sum(github_direction_next.is_live_breakage(item) and not github_direction_next.repository_hold(selection_context, item["repo"]) for item in scanned),
            unevaluated_count=sum(skipped_counts.values()),
            capacity_unevaluated_count=capacity_unevaluated,
            unevaluated_milestone_issues=unevaluated_milestones,
            held_scan_limit=args.scan_limit,
            evaluation_note=(f"Checked {len(scanned)} of {len(inventory)} discovered open issues outside the evaluated milestone graph. "
                             "Evaluation stopped early; raise --scan-limit to check more."
                             if capacity_unevaluated else None),
            unevaluated_repositories=[{"repo": name, "issue_count": count} for name, count in sorted(skipped_counts.items())],
        )
        discovery["parent_limit_per_issue"] = 10
        discovery["complete"] &= not discovery["unevaluated_count"]
        discovery["capacity_complete"] = discovery.get("capacity_complete", True) and not capacity_unevaluated and discovery["complete"]
        NEXT_READS.start_phase("discovery_evaluation")
        # All current inventory seeds exist before concurrent node observations.
        seeds.update({(raw["repo"].casefold(), raw["number"]): raw for raw in scanned})
        evaluated_discoveries = []
        for offset, raw in enumerate(scanned):
            # Leave time to qualify the rows already read. Spending the entire
            # budget on more discussions would leave ownership unread for all
            # discoveries, even when independent source work is available.
            final_evidence_reserve = NEXT_READS.final_evidence_reserve
            if NEXT_READS.expired or NEXT_READS.remaining_seconds < final_evidence_reserve:
                break
            if NEXT_READS.workers > 1:
                prefetch_nodes(scanned[offset:offset + NEXT_READS.workers], ancestry=True)
            ranked["excluded"] = [item for item in ranked["excluded"] if not (item.get("exclusion") == "outside_direction_tracks" and item["repo"].casefold() == raw["repo"].casefold() and item["number"] == raw["number"])]
            seeds[(raw["repo"].casefold(), raw["number"])] = raw
            node = read_node(raw["repo"], raw["number"])
            evaluated_discoveries.append(raw)
            item: dict[str, Any] = {**node["item"], "source": "repository_discovery"}
            original_exclusion = item.get("exclusion")
            ordinary_discussion_complete = bool((item.get("discussion") or {}).get("complete"))
            held = github_direction_next.repository_hold(selection_context, item["repo"])
            status_text = node.get("status_text") or ""
            wait_evidence = github_direction_next.milestone_wait_evidence(item, status_text, titles)
            listed_wait = original_exclusion in {"waiting", "wait_review_required"} and ((item.get("milestone") or {}).get("title") in titles or not wait_evidence["valid"])
            if (not original_exclusion or capacity_evidence or listed_wait) and item.get("exclusion") not in {"completed", "pull_request", "unknown_dependencies"}:
                exclusion = item.get("exclusion")
                item = with_ancestry(item)
                preserve_ancestry = exclusion in {"waiting", "wait_review_required"} and (
                    item.get("exclusion") == "parent_waiting"
                    or (item.get("exclusion") == "unknown_ancestry" and not wait_evidence["valid"])
                )
                if exclusion and not preserve_ancestry:
                    item["exclusion"] = exclusion
                    if exclusion != "parent_waiting":
                        item.pop("waiting_on_parent", None)
            if not (item.get("discussion") or {}).get("complete"):
                discovery["capacity_complete"] = False
                if not held and (not original_exclusion or not ordinary_discussion_complete):
                    discovery["complete"] = False
            if held:
                discoveries.append(item)
                continue
            if item.get("exclusion"):
                ranked["excluded"].append(item)
                reports = node.get("waiting") or []
                ranked["waiting"].extend(reports)
                if item["exclusion"] in {"waiting", "wait_review_required", "parent_waiting"} and not reports:
                    ranked["waiting"].append({
                        "repo": item["repo"], "number": item["number"], "url": item["url"],
                        **({"source": "legacy_wait_review"} if item["exclusion"] == "wait_review_required" else {}),
                        "milestone": item.get("milestone"),
                        "waiting_for": item.get("waiting_on_parent") or node.get("status_text") or "Waiting party or condition not recorded",
                        "reported_by": item["url"], "reported_at": item.get("updated_at"),
                    })
                if item["exclusion"] in {"unknown_dependencies", "unknown_ancestry"}:
                    discovery["capacity_complete"] = False
                    if not held and (original_exclusion not in {"waiting", "wait_review_required"} or not ordinary_discussion_complete
                                     or (item.get("milestone") or {}).get("title") in titles):
                        discovery["complete"] = False
            else:
                discoveries.append(item)
        if len(evaluated_discoveries) < len(scanned):
            omitted = scanned[len(evaluated_discoveries):]
            for raw in omitted:
                if not github_direction_next.repository_hold(selection_context, raw["repo"]):
                    skipped_counts[raw["repo"]] = skipped_counts.get(raw["repo"], 0) + 1
                if (raw.get("milestone") or {}).get("title") in titles or raw.get("client_request"):
                    unevaluated_milestones.append(compact_list_issue(raw["repo"], raw))
            discovery.update(complete=False, capacity_complete=False, time_budget_limited=True, evaluated=len(evaluated_discoveries),
                unevaluated_count=sum(skipped_counts.values()),
                capacity_unevaluated_count=capacity_unevaluated + len(omitted),
                unevaluated_repositories=[{"repo": name, "issue_count": count} for name, count in sorted(skipped_counts.items())],
                evaluation_note="Global next reserved its remaining time for wait/ownership evidence; raise --time-budget to inspect remaining discoveries.")
    # Marked incidents can be decomposed outside both the ordinary scan and
    # the milestone graph. Their native leaves retain the marker's provenance.
    incident_roots = [compact_list_issue(raw["repo"], raw) for raw in [*list(seeds.values()), *inventory]
                      if github_direction_next.is_live_breakage(raw)] if scope is None else []
    def read_incident_node(issue_repo: str, number: int) -> dict[str, Any]:
        incident_node = read_node(issue_repo, number)
        incident_item = incident_node["item"]
        exclusion = incident_item.get("exclusion")
        if (exclusion in {None, "blocked_by_open_dependency", "delegated_to_open_sub_issues"} or capacity_evidence) and exclusion not in {"completed", "pull_request", "unknown_dependencies"}:
            incident_item = with_ancestry(incident_item)
            if exclusion and exclusion not in {"blocked_by_open_dependency", "delegated_to_open_sub_issues"}:
                incident_item["exclusion"] = exclusion
        return {**incident_node, "item": incident_item}
    incidents = github_direction_next.incident_work_paths(
        incident_roots, read_node=read_incident_node, scan_limit=args.scan_limit,
    )
    incident_keys = {(item["repo"].casefold(), item["number"]) for item in incidents["items"]}
    ranked["excluded"] = [item for item in ranked["excluded"] if not (
        item.get("exclusion") == "outside_direction_tracks"
        and (item["repo"].casefold(), item["number"]) in incident_keys)]
    existing = {(item["repo"].casefold(), item["number"]): item
                for item in [*ranked["candidates"], *ranked["excluded"], *discoveries]}
    for item in incidents["items"]:
        if not (item.get("discussion") or {}).get("complete"):
            discovery["complete"] = False
            discovery["capacity_complete"] = False
        if item.get("exclusion") in {"unknown_dependencies", "unknown_ancestry"}:
            discovery["capacity_complete"] = False
        incident_key = (item["repo"].casefold(), item["number"])
        if incident_key in existing:
            existing[incident_key]["incident_via"] = item["incident_via"]
        elif item.get("exclusion"):
            ranked["excluded"].append(item)
            ranked["waiting"].extend(nodes.get(incident_key, {}).get("waiting", []))
        else:
            discoveries.append({**item, "source": "incident_native_graph"})
    discovery["incident_context"] = incidents["context"]
    if not incidents["context"]["complete"]:
        discovery["complete"] = False
    incomplete_milestone_sources = [
        source["repo"] for source in discovery.get("repositories", [])
        if set(direction_milestone_titles(source.get("direction") or "")).intersection(titles)
        and (source.get("truncated") or source.get("error"))
    ]
    discovery["incomplete_milestone_repositories"] = incomplete_milestone_sources
    candidate_coverage_complete = bool(graph_coverage["complete"] and (scope is not None or discovery.get("complete")))
    priority_keys = [(entry["repo"].casefold(), entry["number"]) for entry in [*ranked["candidates"], *discoveries]]
    evidence_keys = dict.fromkeys([*priority_keys, *nodes])
    NEXT_READS.start_phase("ownership")
    queue_context = annotate_next_queue(list({(entry["repo"].casefold(), entry["number"]): entry
        for entry in [*ranked["candidates"], *discoveries]}.values()), args)
    for name, observed in graph_queue_context.get("repositories", {}).items():
        queue_context.setdefault("repositories", {}).setdefault(name, observed)
    NEXT_READS.start_phase("wait_evidence")
    wait_context = next_wait_context(
        [seeds[key] for key in evidence_keys if key in seeds],
        scan_limit=args.scan_limit * 2,
        inventory_complete=candidate_coverage_complete,
        reference_repositories={item["repo"] for item in discovery.get("repositories", [])},
        structured_waits={key: node["item"]["structured_wait"] for key, node in nodes.items() if "structured_wait" in node["item"]},
    )
    stale = {(row["repo"].casefold(), row["number"]): row for row in wait_context["items"]}
    checked_waits = {(row["repo"].casefold(), row["number"]) for row in wait_context["checked_issues"]}
    unread_waits = {(row["repo"].casefold(), row["number"]) for row in wait_context["unavailable"]}
    reported_entries = [*ranked["candidates"], *ranked["excluded"], *discoveries]
    reported_parents = [parent for entry in reported_entries for parent in (entry.get("discussion") or {}).get("parents", [])]
    for entry in [*reported_entries, *reported_parents]:
        wait_key = (entry["repo"].casefold(), entry["number"])
        entry["wait_evidence_complete"] = wait_key in checked_waits and wait_key not in unread_waits
        evidence = stale.get(wait_key)
        if evidence:
            key = "post_merge_evidence" if "completion_proven" in evidence else "stale_wait_evidence"
            entry[key] = evidence
    exclude_landed_candidates(ranked["candidates"], ranked["excluded"], wait_context)
    exclude_landed_candidates(discoveries, ranked["excluded"], wait_context)
    queue_items = list({(entry["repo"].casefold(), entry["number"]): entry
                        for entry in [*ranked["candidates"], *discoveries]}.values())
    NEXT_READS.start_phase("ranking")
    from skills.direction.scripts import direction_audit
    try:
        own_projects = direction_audit.own_project_patterns(direction_text)
        own_project_context = {"complete": True, "source": f"{repo}:{DIRECTION_FILE}:Order", "patterns": own_projects}
    except direction_audit.AuditError as exc:
        own_projects = []
        own_project_context = {"complete": False, "error": str(exc)}
    for entry in queue_items:
        if github_next_queue.own_project(entry["repo"], own_projects, repo.split("/")[0]):
            entry["direction_category"] = "own_project"
    portfolio = github_direction_next.rank_portfolio_work(
        ranked, discoveries, milestone_titles=titles, selection_context=selection_context,
        coverage_complete=scope is None and graph_coverage["complete"] and not unevaluated_milestones and not incomplete_milestone_sources,
        repository_clients=client_records, director_owner=repo.split("/")[0], bot_logins=client_bot_logins,
        reference_repositories={entry["repo"] for entry in list(seeds.values())} | {entry["repo"] for entry in discovery.get("repositories", [])},
        repository_milestones={source["repo"]: direction_milestone_titles(source["direction"]) if source.get("direction") else None for source in discovery.get("repositories", []) if isinstance(source.get("repo"), str)},
        repository_waypoints={source["repo"]: repository_direction_milestones(source) for source in discovery.get("repositories", []) if isinstance(source.get("repo"), str)},
    )
    graph_waits = list(ranked["waiting"])
    track_next_actions = ranked.pop("track_next_actions", {})
    ranked.update(portfolio)
    ranked["queue_context"] = queue_context
    ranked["own_project_context"] = own_project_context
    ranked["selection_coverage"] = github_next_queue.coverage(
        [*list(seeds.values()), *inventory], [seeds[key] for key in nodes if key in seeds], portfolio)
    graph_active = {(entry["repo"].casefold(), entry["number"]) for entry in [*ranked["candidates"], *ranked["excluded"], *ranked["underway"], *ranked["verification_only"], *ranked["scope_review"]]
                    if entry.get("via") and config["labels"]["active"] in normalize_labels(entry.get("labels"))}
    ranked["selection_coverage"].update(active_in_evaluated_graph_count=len(graph_active),
        active_outside_evaluated_graph_count=ranked["selection_coverage"]["active_inventory_count"] - len(graph_active))
    for entry in [*ranked["candidates"], *ranked["excluded"], *ranked["underway"]]:
        entry.pop("wait_evidence_complete", None)
    ranked["stale_wait_report"] = {key: value for key, value in wait_context.items() if key != "references"}
    for row in [*ranked["waiting"], *ranked["recorded_waits"], *ranked["stale_waits"]]:
        row["references"] = [wait_context["references"].get(ref["url"], {**ref, "state": "unknown"}) for ref in row["references"]]
        row["closed_references"] = [ref for ref in row["references"] if ref["state"] == "closed"]
        evidence = stale.get((row["repo"].casefold(), row["number"]))
        row["stale_wait_evidence"] = evidence if evidence and "completion_proven" not in evidence else None
        row["review_required"] = True
    ranked["client_context"] = {key: {field: value for field, value in record.items() if field != "login"} for key, record in client_records.items()}
    github_agent.filter_selection(ranked, github_agent.running_agent(getattr(args, "agent", None)))
    ranked["milestone_summary"] = github_direction_next.milestone_summary(
        titles, completed=ranked.get("completed_milestones", []), candidates=ranked["candidates"],
        graph_waits=[*graph_waits, *ranked["waiting"], *ranked["recorded_waits"]], track_next_actions=track_next_actions,
        underway=ranked["underway"], verification_only=ranked["verification_only"], scope_review=ranked["scope_review"],
    )
    ranked["candidates"] = ranked["candidates"][:args.limit]
    ranked["available_candidates"] = ranked["available_candidates"][:args.limit]
    ranked["graph_context"] = graph_coverage
    ranked["discovery_context"] = discovery
    milestone_ranking_complete = bool(graph_coverage["complete"] and not unevaluated_milestones and not incomplete_milestone_sources
                                      and not NEXT_READS.expired and not discovery.get("time_budget_limited"))
    candidate_coverage_complete &= not NEXT_READS.expired
    if not incidents["context"]["complete"]:
        coverage_warning = ("Incident-path coverage is partial: a native path was truncated, cyclic or unavailable. "
                            "Unseen incident work may outrank milestone and other candidates.")
    elif candidate_coverage_complete:
        coverage_warning = None
    elif milestone_ranking_complete:
        coverage_warning = ("Milestone ranking is complete; portfolio discovery is partial, so unseen work outside "
                            "the listed milestones may outrank the non-milestone candidates, never milestone work. "
                            "milestone_summary names each milestone's work or waits.")
    else:
        coverage_warning = ("Milestone ranking is partial: the milestone graph or a milestone repository was not "
                            "fully read, so unseen work may outrank these candidates.")
    ranked["candidate_coverage"] = {
        "scope": "milestone" if scope is not None else "portfolio",
        "complete": candidate_coverage_complete,
        "milestone_ranking_complete": milestone_ranking_complete,
        "warning": coverage_warning,
        "unevaluated_discovery_count": discovery.get("unevaluated_count", 0),
        "unevaluated_repositories": discovery.get("unevaluated_repositories", []),
    }
    ranked["truncated"] |= bool(discovery.get("inventory_truncated") or discovery.get("unevaluated_count") or any(source.get("truncated") and not source.get("hold") for source in discovery.get("repositories", [])))
    sections = section_map(direction_text or "")
    NEXT_READS.start_phase("dependabot")
    dependabot = next_dependabot_work(
        discovery.get("repositories", []), scan_limit=args.scan_limit, limit=args.limit,
        inventory_complete=not discovery.get("inventory_truncated", False) and "error" not in discovery,
    ) if scope is None else {"dependabot_context": {"complete": False, "exclusion": "explicit_milestone_scope"}}
    # Account for every speculative read before emitting the terminal receipt.
    NEXT_READS.drain(unused_failure=unused_failure)
    emit({
        "ok": True, "actor": actor, "repo": repo,
        "scope": {"kind": "direction", "owner": repo.split("/")[0], "milestone": scope},
        "milestone_order": {"source": f"{repo}:{DIRECTION_FILE}", "titles": titles, "fallback_reason": None, "error": None},
        "direction_context": {name: sections[name] for name in ("Order", "Capacity") if name in sections},
        "focus_context": {"repositories": focus_contexts},
        "inventory_count": len(issues), "inventory_limit": NEXT_PLAN_INVENTORY_LIMIT,
        "performance": NEXT_READS.receipt(),
        **ranked,
        **dependabot,
        "notes": [
            "native_blocked_by_relationships_are_authoritative",
            "tracking_wait_labels_do_not_hide_linked_work",
            "waiting_reports_are_current_status_evidence_not_dependency_edges",
            "live_breakage_and_repeated_tooling_stops_require_direction_context",
            "weekly_own_project_share_is_audit_context_not_a_per_call_quota",
            "candidates_are_possible_work_until_direction_discussion_and_ownership_review",
            "graph_coverage_is_not_portfolio_or_active_session_coverage",
        ],
    })


def resolve_project(owner: str, title_or_number: str, *, recoverable: bool = False) -> tuple[str, int, dict[str, Any] | None]:
    if title_or_number.isdigit():
        return "unknown", int(title_or_number), None
    cache_key = ("project", owner, title_or_number)
    if cache_key in PROJECT_CACHE:
        return PROJECT_CACHE[cache_key]
    ensure_graphql_budget(prefer_active=True, recoverable=recoverable)
    actor, data = gh_json(
        ["project", "list", "--owner", owner, "--format", "json", "--limit", "100"],
        prefer_active=True,
    )
    projects = data.get("projects", data) if isinstance(data, dict) else data
    for item in projects or []:
        if item.get("title") == title_or_number:
            result = (actor, int(item.get("number")), item)
            PROJECT_CACHE[cache_key] = result
            PROJECT_CACHE[("project", owner, str(result[1]))] = result
            return result
    raise project_error(f"Project not found for {owner}: {title_or_number}")


def project_meta(owner: str, title_or_number: str, *, recoverable: bool = False) -> tuple[str, int, dict[str, Any]]:
    actor, number, project_data = resolve_project(owner, title_or_number, recoverable=recoverable)
    if project_data:
        return actor, number, project_data
    cache_key = ("project", owner, str(number))
    if cache_key in PROJECT_CACHE and PROJECT_CACHE[cache_key][2]:
        return PROJECT_CACHE[cache_key]
    ensure_graphql_budget(prefer_active=True, recoverable=recoverable)
    actor, data = gh_json(["project", "view", str(number), "--owner", owner, "--format", "json"], prefer_active=True)
    PROJECT_CACHE[cache_key] = (actor, number, data)
    return actor, number, data


def project_fields(owner: str, project_number: int, *, recoverable: bool = False) -> dict[str, dict[str, Any]]:
    cache_key = ("fields", owner, project_number)
    if cache_key in PROJECT_CACHE:
        return PROJECT_CACHE[cache_key]
    ensure_graphql_budget(prefer_active=True, recoverable=recoverable)
    _, data = gh_json(["project", "field-list", str(project_number), "--owner", owner, "--format", "json"], prefer_active=True)
    fields = {item["name"]: item for item in data.get("fields", [])}
    PROJECT_CACHE[cache_key] = fields
    return fields


def project_items(
    owner: str,
    project_number: int,
    *,
    query: str | None = None,
    limit: int = 1000,
    recoverable: bool = False,
) -> list[dict[str, Any]]:
    cache_key = ("items", owner, project_number, query, limit)
    if cache_key in PROJECT_CACHE:
        return PROJECT_CACHE[cache_key]
    ensure_graphql_budget(prefer_active=True, recoverable=recoverable)
    args = [
        "project",
        "item-list",
        str(project_number),
        "--owner",
        owner,
        "--format",
        "json",
        "--limit",
        str(limit),
    ]
    if query:
        args.extend(["--query", query])
    _, data = gh_json(args, prefer_active=True)
    items = data.get("items", [])
    PROJECT_CACHE[cache_key] = items
    return items


def find_project_item(
    owner: str,
    project_number: int,
    issue_url: str,
    *,
    item_id: str | None = None,
    recoverable: bool = False,
    attempts: int = 3,
) -> dict[str, Any]:
    if item_id:
        return {"id": item_id}
    issue_number = issue_url.rstrip("/").rsplit("/", 1)[-1]
    for attempt in range(attempts):
        try:
            items = project_items(
                owner,
                project_number,
                query=f"#{issue_number}",
                limit=50,
                recoverable=recoverable,
            )
        except PlanError:
            items = project_items(owner, project_number, recoverable=recoverable)
        for item in items:
            content = item.get("content") or {}
            if content.get("url") == issue_url:
                return item
        if items:
            for item in project_items(owner, project_number, recoverable=recoverable):
                content = item.get("content") or {}
                if content.get("url") == issue_url:
                    return item
        if attempt + 1 < attempts:
            for key in list(PROJECT_CACHE):
                if len(key) >= 3 and key[:3] == ("items", owner, project_number):
                    PROJECT_CACHE.pop(key, None)
            time.sleep(1)
    raise project_error(f"Issue is not in project {owner}/{project_number}: {issue_url}")


def set_project_field(
    *,
    project: dict[str, Any],
    item: dict[str, Any],
    field: dict[str, Any],
    value: str,
) -> str:
    args = [
        "project",
        "item-edit",
        "--id",
        item["id"],
        "--project-id",
        project["id"],
        "--field-id",
        field["id"],
        "--format",
        "json",
    ]
    if field.get("type") == "ProjectV2SingleSelectField":
        option = next((opt for opt in field.get("options", []) if opt.get("name") == value), None)
        if not option:
            raise project_error(f"Unknown option for {field['name']}: {value}")
        args.extend(["--single-select-option-id", option["id"]])
    else:
        args.extend(["--text", value])
    try:
        actor, _, _ = run_raw(args, prefer_active=True)
        return actor
    except PlanError as exc:
        if isinstance(exc, ClassifiedPlanError):
            raise
        raise project_error(str(exc)) from exc


def clear_project_field(
    *,
    project: dict[str, Any],
    item: dict[str, Any],
    field: dict[str, Any],
) -> str:
    try:
        actor, _, _ = run_raw([
            "project",
            "item-edit",
            "--id",
            item["id"],
            "--project-id",
            project["id"],
            "--field-id",
            field["id"],
            "--clear",
            "--format",
            "json",
        ], prefer_active=True)
        return actor
    except PlanError as exc:
        if isinstance(exc, ClassifiedPlanError):
            raise
        raise project_error(str(exc)) from exc


def set_project_fields(
    *,
    owner: str,
    project_ref: str,
    issue_url: str,
    config: dict[str, Any],
    focus: str | None = None,
    manager: str | None = None,
    finish_line: str | None = None,
    item_id: str | None = None,
    recoverable: bool = False,
) -> dict[str, Any]:
    if not any((focus, manager, finish_line)):
        return {"updated": {}}
    actor, project_number, project = project_meta(owner, project_ref, recoverable=recoverable)
    fields = project_fields(owner, project_number, recoverable=recoverable)
    item = find_project_item(owner, project_number, issue_url, item_id=item_id, recoverable=recoverable)
    field_names = config.get("project_fields") or {}
    updates = {
        field_names.get("focus", "Focus"): focus,
        field_names.get("manager", "Manager"): manager,
        field_names.get("finish_line", "Finish Line"): finish_line,
    }
    updated: dict[str, str] = {}
    for field_name, value in updates.items():
        if not value:
            continue
        field = fields.get(field_name)
        if not field:
            raise project_error(f"Project field not found: {field_name}")
        actor = set_project_field(project=project, item=item, field=field, value=value)
        updated[field_name] = value
    return {"actor": actor, "project": project.get("title"), "updated": updated}


def cmd_project_add(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    _, issue = get_issue(args.issue, repo)
    issue_repo = issue["repo"]
    owner = args.owner or (config.get("projects") or {}).get("owner") or issue_repo.split("/", 1)[0]
    project = args.project or (config.get("projects") or {}).get("default_project")
    if not project:
        raise PlanError("Pass --project or set planning.projects.default_project")
    _, number, project_data = resolve_project(owner, project, recoverable=True)
    ensure_graphql_budget(prefer_active=True, recoverable=True)
    actor, data = gh_json([
        "project", "item-add", str(number), "--owner", owner, "--url", issue["html_url"], "--format", "json"
    ], prefer_active=True)
    emit({"ok": True, "actor": actor, "owner": owner, "project": project_data or {"number": number}, "item": data})


def cmd_project_set(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    _, issue = get_issue(args.issue, repo)
    if not any((args.focus, args.manager, args.finish_line)):
        emit({"ok": True, "updated": {}})
        return
    project_config = config.get("projects") or {}
    owner = args.owner or project_config.get("owner") or issue["repo"].split("/", 1)[0]
    project = args.project or project_config.get("default_project")
    if not project:
        raise PlanError("Pass --project or set planning.projects.default_project")
    result = set_project_fields(
        owner=owner,
        project_ref=project,
        issue_url=issue["html_url"],
        config=config,
        focus=args.focus,
        manager=resolve_required_manager_value(args.manager),
        finish_line=args.finish_line,
        item_id=args.item_id,
        recoverable=True,
    )
    emit({"ok": True, **result})


def normalized_close_reason(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip().casefold().replace("-", "_").replace(" ", "_")


def issue_matches_close_reason(issue: dict[str, Any], close_reason: str) -> bool:
    state = issue.get("state")
    return (
        isinstance(state, str)
        and state.casefold() == "closed"
        and normalized_close_reason(issue.get("state_reason")) == close_reason
    )


def close_error_with_recovery(
    exc: PlanError,
    *,
    issue_repo: str,
    number: int,
    close_reason: str,
    relationship_preflight: dict[str, Any],
    project_result: dict[str, Any],
    issue_closed: bool | None,
    metadata_state: str,
    planning_config: dict[str, Any],
) -> PlanError:
    if not project_result:
        project_state = "not_configured"
    elif project_result.get("warning"):
        project_state = "warning"
    else:
        project_state = "synchronized_before_close"
    authority_denied = exc.failure and exc.failure.cause in {
        "permission_denied", "not_installed", "repository_access_denied"}
    return PlanError(
        str(exc),
        failure=exc.failure,
        api_result=exc.api_result,
        payload={
            **exc.payload,
            "relationship_preflight": relationship_preflight,
            "project": project_result or None,
            "planning_config": planning_config,
            "recovery": {
                "commit_point": "issue_closed_with_requested_reason",
                "issue": {"repo": issue_repo, "number": number},
                "close_reason": close_reason,
                "issue_closed": issue_closed,
                "completion_metadata": metadata_state,
                "project_state": project_state,
                "retry_safe": not bool(authority_denied),
                "next_action": "existing_authorized_target_session" if authority_denied else "rerun_same_close_command",
                "projection_command": f"gh-plan.py --repo {issue_repo} reconcile-status {number} --include-dependents --apply",
            },
        },
    )


def close_attention_preflight(
    issue_repo: str, number: int, issue: dict[str, Any], decision_url: str | None, gh_cmd: str,
) -> dict[str, Any]:
    try:
        return github_close_attention.check(issue_repo, number, issue=issue, decision_url=decision_url,
                                            gh_cmd=gh_cmd)
    except github_close_attention.CloseAttentionError as exc:
        raise PlanError(
            str(exc),
            failure=github_api_core.FailureDetail(
                cause=exc.cause,
                message="closure needs every outside comment answered, or the Director's decision",
                retryable=False,
                fallback_eligible=False,
                disposition="stop",
                write_outcome="not_started",
                failed_step="close_attention_preflight",
            ),
            payload={"close_attention": exc.payload},
        ) from exc


def cmd_close(args: argparse.Namespace) -> None:
    trigger = None
    if getattr(args, "reopen_trigger", None) is not None:
        try:
            trigger = json.loads(args.reopen_trigger)
            github_plan_trigger.validate(trigger)
        except ValueError as exc:
            raise PlanError(f"Invalid --reopen-trigger: {exc}") from exc
    if getattr(args, "clear_reopen_trigger", None) is not None:
        trigger = {"version": 1, "state": "clear", "evidence": args.clear_reopen_trigger}
        try:
            github_plan_trigger.validate(trigger)
        except ValueError as exc:
            raise PlanError(str(exc)) from exc
    repo = default_repo(args.repo)
    issue_repo, number = issue_ref(args.issue, repo)
    planning_checkout = getattr(args, "planning_checkout", None)
    if planning_checkout:
        checkout_path = pathlib.Path(planning_checkout).expanduser()
        if not checkout_path.is_dir() or (repo_from_git(checkout_path) or "").casefold() != issue_repo.casefold():
            raise PlanError("--planning-checkout must name a checkout of the resolved target repository")
    planning_config: dict[str, Any] = {}
    config = load_config(issue_repo, checkout=planning_checkout, discovery=planning_config)
    close_reason = plan_close_reason(args.reason or "completed")
    issue_actor, issue = get_issue(args.issue, repo)
    if trigger is not None and args.reason is None and issue.get("state") == "closed":
        close_reason = plan_close_reason(issue.get("state_reason") or "completed")
    if getattr(args, "only_closed", False) and not issue_matches_close_reason(issue, close_reason):
        raise PlanError("Landing reconciliation only repairs already-closed issues")
    _, relationship_preflight = close_relationship_preflight(issue_repo, number, close_reason)
    owner_decision: dict[str, Any] | None = None
    if close_reason == "not_planned" and not issue_matches_close_reason(issue, close_reason):
        owner_decision = check_owner_decides_not_planned(issue_repo, number, issue)
    close_comment = read_body(args) if args.body is not None or args.body_file else ""
    plan_labels = config.get("labels") or {}
    route_actor, gh_cmd, expected_actor = comment_route()
    close_attention = close_attention_preflight(issue_repo, number, issue, getattr(args, "unanswered_decision", None),
                                                gh_cmd)
    actor = issue_actor
    completed_steps: list[str] = []
    comment_result: dict[str, Any] = {}
    if trigger is not None:
        # Persist and read back the trigger before closing: failed publication
        # must never leave a closed issue whose reopen event was lost.
        if github_plan_wait.live_lines(close_comment, prefix=github_plan_trigger.PREFIX):
            raise PlanError("Use only --reopen-trigger to supply the close comment's trigger")
        trigger_comment = (close_comment + "\n\nReopen trigger: " + github_plan_trigger.describe(trigger) +
                           "\n\n" + github_plan_trigger.marker(trigger)).strip()
        _, prior_comments = collect_paged_rest_items(
            f"/repos/{issue_repo}/issues/{number}/comments", query={}, bucket="rest_core",
            step_prefix="reopen_trigger_preflight")
        try:
            prior_trigger = github_plan_trigger.current(issue, prior_comments, wait_authors(issue_repo))
        except ValueError as exc:
            raise PlanError(f"Existing reopen trigger needs review before closure: {exc}") from exc
        try:
            comment_result = github_comment_core.comment(
                "issue", number, trigger_comment, repo=issue_repo, gh_cmd=gh_cmd,
                expected_actor=expected_actor, operation=CURRENT_OPERATION,
                completed_steps=completed_steps, failed_step="post_reopen_trigger",
                dedupe_body=prior_trigger == trigger)
        except github_comment_core.CommentError as exc:
            raise plan_error_from_comment(exc, completed_steps=completed_steps) from exc
        completed_steps.append("post_reopen_trigger")
        try:
            _, latest_issue = get_issue(args.issue, repo)
            _, latest_comments = collect_paged_rest_items(
                f"/repos/{issue_repo}/issues/{number}/comments", query={}, bucket="rest_core",
                step_prefix="reopen_trigger_readback")
            if github_plan_trigger.current(latest_issue, latest_comments, wait_authors(issue_repo)) != trigger:
                raise PlanError("Published reopen trigger differs from the current authoritative record")
        except (PlanError, ValueError) as exc:
            raise PlanError(f"Reopen trigger readback failed; issue not closed: {exc}",
                            payload={"comment": comment_result, "completed_steps": completed_steps,
                                     "recovery": "Read the current trigger before retrying close"}) from exc
        completed_steps.append("reopen_trigger_readback")
        # This label is only a discovery view; the JSON record owns the event.
        # Index before closure so an interrupted close never hides a trigger.
        index_label = github_plan_trigger.INDEX_LABEL
        indexed = trigger.get("state") != "clear" and trigger.get("kind") != "recurrence"
        if indexed:
            ensure_labels(issue_repo, [index_label], {"label_defs": {index_label: {
                "color": "c5def5", "description": "Helper-managed view of structured reopen-trigger records"}}},
                completed_steps=completed_steps)
        if indexed or index_label.casefold() in {name.casefold() for name in normalize_labels(latest_issue.get("labels"))}:
            try:
                github_issue_core.edit_issue(number, repo=issue_repo,
                    add_labels=[index_label] if indexed else None,
                    remove_labels=None if indexed else [index_label],
                    gh_cmd=gh_cmd, expected_actor=expected_actor, operation=CURRENT_OPERATION)
            except github_issue_core.IssueError as exc:
                raise plan_error_from_issue(exc, completed_steps=completed_steps) from exc
        completed_steps.append("index_reopen_trigger")
        close_comment = ""

    project_result: dict[str, Any] = {}
    project_config = config.get("projects") or {}
    project = args.project or project_config.get("default_project")
    owner = args.owner or project_config.get("owner") or issue_repo.split("/", 1)[0]
    if project:
        project_steps: list[str] = []
        try:
            _, project_number, project_data = project_meta(owner, project, recoverable=True)
            fields = project_fields(owner, project_number, recoverable=True)
            item = find_project_item(owner, project_number, issue["html_url"], recoverable=True)
            updated: dict[str, str | None] = {}
            status_field = fields.get("Status")
            if status_field:
                set_project_field(
                    project=project_data,
                    item=item,
                    field=status_field,
                    value="Done",
                )
                updated["Status"] = "Done"
                project_steps.append("set_project_status")
            project_result = {"project": project_data.get("title"), "updated": updated}
        except PlanError as exc:
            completed_steps.extend(project_steps)
            project_result = project_failure_payload(
                exc,
                owner=owner,
                project=project,
                operation="close_project_sync",
                non_blocking=True,
            )
            project_result["message"] = (
                "Project synchronization needs follow-up; inspect issue and recovery state separately."
            )
        else:
            completed_steps.append("sync_project")

    close_result: dict[str, Any] = {}
    if issue_matches_close_reason(issue, close_reason):
        close_result = {"actor": actor, "expected_actor": expected_actor}
        completed_steps.append("close_issue_already_complete")
    else:
        try:
            close_result = github_issue_core.set_issue_state(
                number,
                state="closed",
                state_reason=close_reason,
                repo=issue_repo,
                gh_cmd=gh_cmd,
                expected_actor=expected_actor,
                operation=CURRENT_OPERATION,
            )
        except github_issue_core.IssueError as exc:
            close_error = plan_error_from_issue(exc, completed_steps=completed_steps)
            close_failure = close_error.failure
            observed_closed: bool | None = (
                str(issue.get("state") or "").casefold() == "closed"
            )
            if close_failure and close_failure.write_outcome == "unknown":
                try:
                    observed_actor, observed_issue = get_issue(f"{issue_repo}#{number}", issue_repo)
                except PlanError as read_exc:
                    observed_closed = None
                    close_error = PlanError(
                        str(close_error),
                        failure=close_error.failure,
                        api_result=close_error.api_result,
                        payload={
                            **close_error.payload,
                            "close_reconciliation": {
                                "strategy": "read_issue_and_compare_state",
                                "result": "unavailable",
                                "failure": read_exc.api_result or {
                                    "cause": read_exc.failure.cause if read_exc.failure else "read_failed"
                                },
                            },
                        },
                    )
                else:
                    observed_state = observed_issue.get("state")
                    observed_closed = (
                        isinstance(observed_state, str)
                        and observed_state.casefold() == "closed"
                    )
                    if issue_matches_close_reason(observed_issue, close_reason):
                        issue = observed_issue
                        actor = (
                            (close_error.api_result or {}).get("actor")
                            or observed_actor
                            or actor
                        )
                        close_result = {
                            "actor": actor,
                            "expected_actor": expected_actor,
                            "reconciled": True,
                            "reconciliation": {
                                "strategy": "read_issue_and_compare_state",
                                "result": "matched",
                            },
                        }
                        completed_steps = merge_completed_steps(
                            close_failure.completed_steps,
                            ["reconcile_close_issue"],
                        )
                    else:
                        close_error = PlanError(
                            str(close_error),
                            failure=close_error.failure,
                            api_result=close_error.api_result,
                            payload={
                                **close_error.payload,
                                "close_reconciliation": {
                                    "strategy": "read_issue_and_compare_state",
                                    "result": "no_match",
                                    "observed": compact_issue(observed_issue),
                                },
                            },
                        )
            if not close_result:
                raise close_error_with_recovery(
                    close_error,
                    issue_repo=issue_repo,
                    number=number,
                    close_reason=close_reason,
                    relationship_preflight=relationship_preflight,
                    project_result=project_result,
                    issue_closed=observed_closed,
                    metadata_state="not_started",
                    planning_config=planning_config,
                ) from exc
        else:
            actor = close_result.get("actor") or actor
            completed_steps.append("close_issue")

    source_error: PlanError | None = None
    denied_repositories: dict[str, Any] = {}
    stopped_reason: dict[str, Any] | None = None
    source_projection: dict[str, Any] = {"repo": issue_repo, "number": number}

    def record_source_error(error: PlanError, metadata_state: str, *, writes_started: bool) -> None:
        nonlocal source_error, stopped_reason
        wrapped = close_error_with_recovery(error, issue_repo=issue_repo, number=number, close_reason=close_reason,
            relationship_preflight=relationship_preflight, project_result=project_result, issue_closed=True,
            metadata_state=metadata_state, planning_config=planning_config)
        if planning_checkout:
            wrapped.payload["recovery"]["projection_command"] += " " + shlex.quote("--planning-checkout=" + planning_checkout)
        if close_comment:
            resume = ["gh-plan.py", "--repo", issue_repo, "close", str(number),
                      "--reason", close_reason, "--comment=" + close_comment]
            for flag, value in (("--project", args.project), ("--owner", args.owner),
                                ("--planning-checkout", planning_checkout)):
                if value:
                    resume.append(flag + "=" + str(value))
            comment_outcome = "not_attempted"
            if metadata_state == "comment_incomplete":
                comment_outcome = (error.failure.write_outcome if error.failure else None) or "unknown"
            wrapped.payload["recovery"]["pending_comment"] = {
                "resume_argv": resume,
                "write_outcome": comment_outcome,
                "next_action": "An existing authorized owning session reads current state and discussion before resuming these arguments; "
                               "the confirmed close and exact-body comment are read-reconciled and deduplicated first. "
                               "Observe an unknown comment outcome before resuming. Metadata-only projection does not post this note. "
                               "Do not replay a denied write in this session or switch identity."}
        if source_error is None:
            source_error = wrapped
        source_projection["writes_started"] = writes_started
        denied, stop = github_plan_landing.reconciliation_error(source_projection, wrapped)
        reason = {"repo": issue_repo, "number": number, "error": source_projection["error"]}
        if denied:
            denied_repositories[issue_repo.casefold()] = reason
        if stop:
            stopped_reason = reason

    label_source = close_result if isinstance(close_result.get("labels"), list) else issue
    try:
        _, wait_comments = collect_paged_rest_items(
            f"/repos/{issue_repo}/issues/{number}/comments", query={}, bucket="rest_core", step_prefix="close_wait_comments")
        close_wait = current_wait(issue, wait_comments, issue_repo)
    except (PlanError, ValueError) as error:
        try:
            close_wait = github_plan_landing.unread_wait(error)
        except PlanError as denied_error:
            record_source_error(denied_error, "wait_read_incomplete", writes_started=False)
            close_wait = {"version": 1, "state": "review", "evidence": str(denied_error), "waits": []}
    try:
        closed_projection = github_plan_status.derive(
            {**issue, "state": "closed", "labels": label_source.get("labels", [])},
            {"blocked_by": relationship_preflight.get("blocked_by", [])}, close_wait, label_names=plan_labels)
        label_diff = github_plan_status.label_changes(normalize_labels(label_source.get("labels")), plan_labels, closed_projection)
    except (PlanError, ValueError, KeyError, TypeError) as error:
        error = error if isinstance(error, PlanError) else PlanError(str(error))
        raise close_error_with_recovery(error, issue_repo=issue_repo, number=number, close_reason=close_reason,
            relationship_preflight=relationship_preflight, project_result=project_result, issue_closed=True,
            metadata_state="wait_read_incomplete", planning_config=planning_config) from error
    add_labels, remove_labels = label_diff["add"] or None, label_diff["remove"] or None
    if (add_labels or remove_labels) and not denied_repositories and stopped_reason is None:
        try:
            label_result = github_issue_core.edit_issue(
                number,
                repo=issue_repo,
                add_labels=add_labels,
                remove_labels=remove_labels,
                gh_cmd=gh_cmd,
                expected_actor=expected_actor,
                operation=CURRENT_OPERATION,
            )
        except github_issue_core.IssueError as exc:
            record_source_error(plan_error_from_issue(exc, completed_steps=completed_steps),
                                "labels_incomplete", writes_started=True)
        else:
            actor = label_result.get("actor") or route_actor
            completed_steps.append("update_labels")
    elif not add_labels and not remove_labels:
        completed_steps.append("labels_already_complete")
    source_projection.update(derived=closed_projection, changes=label_diff)
    if source_error is None:
        source_projection.update(github_plan_status.projection_outcome(
            closed_projection, "reconciled" if add_labels or remove_labels else "unchanged"))
    if closed_projection["wait_review_required"] and source_error is None:
        source_projection["next_action"] = f"gh-plan.py --repo {issue_repo} wait {number}"

    if close_comment and source_error is None:
        try:
            comment_result = github_comment_core.comment(
                "issue",
                number,
                close_comment,
                repo=issue_repo,
                gh_cmd=gh_cmd,
                expected_actor=expected_actor,
                operation=CURRENT_OPERATION,
                completed_steps=completed_steps,
                failed_step="post_close_comment",
                dedupe_body=True,
            )
            actor = comment_result.get("actor") or route_actor
            completed_steps = merge_completed_steps(
                completed_steps,
                comment_result.get("completed_steps"),
            )
        except github_comment_core.CommentError as exc:
            record_source_error(plan_error_from_comment(exc, completed_steps=completed_steps),
                                "comment_incomplete", writes_started=True)

    public_comment = {key: value for key, value in comment_result.items() if key != "completed_steps"}
    output_expected_actor = (
        comment_result["expected_actor"]
        if "expected_actor" in comment_result
        else close_result.get("expected_actor", expected_actor)
    )

    closed_result: dict[str, Any] = {
        "repo": issue_repo,
        "number": number,
        "reason": close_reason,
        "url": issue["html_url"],
    }
    if close_result.get("reconciled"):
        closed_result["reconciled"] = True
        closed_result["reconciliation"] = close_result.get("reconciliation")
    if "close_issue_already_complete" in completed_steps:
        closed_result["already_complete"] = True
    if owner_decision is not None:
        closed_result["owner_decision"] = owner_decision

    try:
        dependents = (github_plan_landing.reconcile_dependents(
                          argparse.Namespace(**globals()), issue_repo, number, apply=True,
                          denied_repositories=denied_repositories, stopped_reason=stopped_reason,
                          **({"planning_checkout": planning_checkout} if planning_checkout else {}))
                      if getattr(args, "reconcile_dependents", True) else {"ok": True, "issues": []})
    except (PlanError, ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        dependents = github_plan_landing.discovery_failure(argparse.Namespace(**globals()),
            issue_repo, number, exc, planning_checkout=planning_checkout)
    if source_error is not None:
        source_error.payload.update(closed=closed_result, source_projection=source_projection,
                                    dependents=dependents, completed_steps=completed_steps,
                                    comment=public_comment or None)
        raise source_error
    if not dependents["ok"]:
        raise PlanError("Issue is closed; dependent planning reconciliation is incomplete", payload={
            "closed": closed_result, "source_projection": source_projection,
            "dependents": dependents, "completed_steps": completed_steps,
            "recovery": {"next_action": f"gh-plan.py --repo {issue_repo} reconcile-status {number} --include-dependents --apply" + (" " + shlex.quote("--planning-checkout=" + planning_checkout) if planning_checkout else ""),
                         "scope": "Repair only metadata after resolving the reported problem; preserve the confirmed close"}})
    emit({
        "ok": True,
        "actor": actor,
        "expected_actor": output_expected_actor,
        "closed": closed_result,
        "dependents": dependents,
        "source_projection": source_projection,
        "coverage": "partial" if closed_projection["wait_review_required"] or dependents.get("coverage") == "partial" else "complete",
        "review_required": closed_projection["wait_review_required"] or dependents.get("review_required", False),
        "comment": public_comment or None,
        "project": project_result,
        "relationship_preflight": relationship_preflight,
        "close_attention": close_attention,
        "planning_config": planning_config,
        "completed_steps": completed_steps,
    })


def cmd_reconcile_landing(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    reader = github_read.GitHubReader(operation="github.plan.reconcile_landing")
    try:
        result = github_plan_landing.reconcile(sys.modules[__name__], reader, repo, args.pr, apply=args.apply)
    except (ValueError, TypeError, subprocess.TimeoutExpired, OSError) as exc:
        raise PlanError(str(exc)) from exc
    if not result["ok"]:
        failure = github_api_core.FailureDetail(
            cause="planning_reconciliation_incomplete", message="Planning reconciliation needs owning-agent review",
            retryable=False, fallback_eligible=False, disposition="stop",
            write_outcome="unknown" if any(item.get("writes_started") for item in result["issues"]) else "not_started",
        )
        raise PlanError(failure.message, payload=result, failure=failure)
    emit(result)


def cmd_project_list(args: argparse.Namespace) -> None:
    owner = args.owner
    ensure_graphql_budget(prefer_active=True, recoverable=True)
    cmd = ["project", "list", "--owner", owner, "--format", "json", "--limit", str(args.limit)]
    if args.closed:
        cmd.append("--closed")
    actor, data = gh_json(
        cmd,
        prefer_active=True,
    )
    emit({"ok": True, "actor": actor, "owner": owner, "projects": data})


def cmd_ensure_labels(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    wanted = list(dict.fromkeys([*config["labels"].values(), *github_agent.LABEL_DEFS]))
    actor, created = ensure_labels(repo, wanted, config)
    emit({"ok": True, "actor": actor, "repo": repo, "ensured": wanted, "created": created})


def milestone_description(args: argparse.Namespace) -> str | None:
    if args.description is not None and args.description_file:
        raise PlanError("--description and --description-file are mutually exclusive")
    if args.description is not None:
        return args.description
    if args.description_file:
        if args.description_file == "-":
            return sys.stdin.read()
        return pathlib.Path(args.description_file).read_text()
    return None


def milestone_route() -> tuple[str, str | None]:
    _, gh_cmd, expected_actor = comment_route()
    return gh_cmd, expected_actor


def cmd_milestone_list(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    gh_cmd, expected_actor = milestone_route()
    emit(
        github_milestone_core.list_milestones(
            repo,
            state=args.state,
            limit=args.limit,
            operation=CURRENT_OPERATION,
            actor=None,
            expected_actor=expected_actor,
            gh_cmd=gh_cmd,
        )
    )


def cmd_milestone_show(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    gh_cmd, expected_actor = milestone_route()
    emit(
        github_milestone_core.show_milestone(
            repo,
            args.milestone,
            operation=CURRENT_OPERATION,
            actor=None,
            expected_actor=expected_actor,
            gh_cmd=gh_cmd,
        )
    )


DIRECTION_FILE = "DIRECTION.md"
DIRECTION_MILESTONE_LINE = re.compile(r"^\s*[-*]\s+`([^`]+)`")
DIRECTION_HEADING = re.compile(r"^##\s+(.+?)\s*$")


def direction_milestone_titles(text: str) -> list[str]:
    """Exact milestone titles listed under `## Milestones` in a DIRECTION.md body."""
    titles: list[str] = []
    current: str | None = None
    for line in text.splitlines():
        heading = DIRECTION_HEADING.match(line)
        if heading:
            current = heading.group(1)
            continue
        if current == "Milestones":
            item = DIRECTION_MILESTONE_LINE.match(line)
            if item:
                titles.append(item.group(1).strip())
    return titles


def load_direction(repo: str) -> str | None:
    """The target repository's merged DIRECTION.md, or None when it has none.

    The merged default-branch copy is the owner-approved one. A local checkout
    is never consulted: it may belong to another repository, or hold an edit
    nobody approved. Any failure other than a clean 404 refuses the write.
    """
    try:
        _, body = api_json(
            "GET",
            f"/repos/{repo}/contents/{DIRECTION_FILE}",
            operation="github.plan.direction_read",
            is_write=False,
            bucket="rest_core",
            failed_step="read_direction",
        )
    except PlanError as exc:
        status = (exc.api_result or {}).get("status")
        if status == 404:
            return None
        raise PlanError(
            f"could not read {DIRECTION_FILE} from {repo}: {github_api_core.redact_string(str(exc))}",
            failure=exc.failure,
            api_result=exc.api_result,
        ) from exc
    if isinstance(body, dict) and isinstance(body.get("content"), str):
        import base64

        return base64.b64decode(body["content"]).decode()
    raise PlanError(
        f"could not read {DIRECTION_FILE} from {repo}: response did not contain file content",
        failure=github_api_core.FailureDetail(
            cause="read_failure",
            message="direction file response did not contain content",
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            failed_step="read_direction",
        ),
    )


def check_direction_lists_milestone(title: str, direction_text: str | None, *, repo: str) -> dict[str, Any]:
    """Refuse a milestone the repository's merged direction file does not name.

    No file means the repository has not adopted one, and the write proceeds as
    before. Once the file exists it is the only source of milestone titles, so
    an agent cannot add, rename to, or reopen a waypoint the owner never approved.
    """
    if direction_text is None:
        return {"direction": "not_adopted"}
    listed = direction_milestone_titles(direction_text)
    if title in listed:
        return {"direction": "listed", "direction_source": f"{repo}:{DIRECTION_FILE}"}
    raise PlanError(
        f"milestone title is not listed under '## Milestones' in {repo}:{DIRECTION_FILE}: {title!r}. "
        "List the title in DIRECTION.md first: a direction session drafts that pull request; "
        "executing agents escalate under the direction skill.",
        failure=github_api_core.FailureDetail(
            cause="validation_error",
            message="milestone not listed in DIRECTION.md",
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="not_started",
            failed_step="check_direction",
        ),
    )


def cmd_milestone_create(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    direction = check_direction_lists_milestone(args.title, load_direction(repo), repo=repo)
    gh_cmd, expected_actor = milestone_route()
    emit(
        direction
        | github_milestone_core.create_milestone(
            repo,
            args.title,
            description=milestone_description(args),
            due_on=args.due_on,
            state=args.state,
            operation=CURRENT_OPERATION,
            actor=None,
            expected_actor=expected_actor,
            gh_cmd=gh_cmd,
        )
    )


def cmd_milestone_update(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    gh_cmd, expected_actor = milestone_route()
    direction: dict[str, Any] = {}
    if args.title is not None or args.state == "open":
        # A rename is a create by another name, and reopening a retired milestone
        # is a create by yet another; the effective title must be listed.
        effective = args.title
        if effective is None:
            shown = github_milestone_core.show_milestone(
                repo,
                args.milestone,
                operation=CURRENT_OPERATION,
                actor=None,
                expected_actor=expected_actor,
                gh_cmd=gh_cmd,
            )
            effective = str(shown["milestone"]["title"])
        direction = check_direction_lists_milestone(effective, load_direction(repo), repo=repo)
    description = milestone_description(args)
    updates: dict[str, Any] = {}
    if description is not None:
        updates["description"] = description
    if args.title is not None:
        updates["title"] = args.title
    if args.due_on is not None:
        updates["due_on"] = args.due_on
    if args.clear_due_on:
        updates["clear_due_on"] = True
    if args.state is not None:
        updates["state"] = args.state
    emit(
        direction
        | github_milestone_core.update_milestone(
            repo,
            args.milestone,
            **updates,
            operation=CURRENT_OPERATION,
            actor=None,
            expected_actor=expected_actor,
            gh_cmd=gh_cmd,
        )
    )


ISSUE_LAST_EDITED_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) { lastEditedAt }
  }
}
"""


def owner_decision_refusal(message: str, *, cause: str = "validation_error") -> PlanError:
    return PlanError(
        message,
        failure=github_api_core.FailureDetail(
            cause=cause,
            message="not-planned close of direction milestone work needs the owner's decision",
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="not_started",
            failed_step="check_owner_decision",
        ),
    )


OWNER_DECISION_COMMENT_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on IssueComment { databaseId body createdAt lastEditedAt }
  }
}
"""


def unedited_owner_decision(comment: dict[str, Any], *, repository: str) -> bool:
    """Require explicit edit-history evidence matching the recorded comment."""
    node_id = comment.get("node_id")
    if not isinstance(node_id, str) or not node_id:
        return False
    _, data = api_json(
        "POST", "/graphql",
        {"query": OWNER_DECISION_COMMENT_QUERY, "variables": {"id": node_id}},
        is_write=False, failed_step="read_owner_decision_edit_history", repository=repository,
    )
    node = (data.get("data") or {}).get("node")
    if data.get("errors") or not isinstance(node, dict) or "lastEditedAt" not in node:
        raise PlanError("Director decision edit history is unavailable")
    return (
        node["lastEditedAt"] is None
        and node.get("databaseId") == comment.get("id")
        and node.get("body") == comment.get("body")
        and node.get("createdAt") == comment.get("created_at")
    )


def owner_reaction_decision(
    issue_repo: str, number: int, comment: dict[str, Any], status_updated_at: str,
) -> dict[str, Any] | None:
    # An exact first line binds the reaction to this issue and this action.
    body = comment.get("body")
    if not isinstance(body, str) or body.splitlines()[:1] not in (
        [f"Director decision: Close #{number} as not planned."],
        [f"Owner decision: Close #{number} as not planned."],
    ):
        return None
    comment_id = comment.get("id")
    if not isinstance(comment_id, int) or isinstance(comment_id, bool):
        return None
    if not unedited_owner_decision(comment, repository=issue_repo):
        return None
    _, reactions = collect_paged_rest_items(
        f"/repos/{issue_repo}/issues/comments/{comment_id}/reactions",
        query={}, bucket="rest_core", step_prefix="owner_decision_reactions",
    )
    for reaction in reactions:
        login = (reaction.get("user") or {}).get("login")
        reacted_at = reaction.get("created_at")
        created_at = comment.get("created_at")
        try:
            times = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in
                     (reacted_at, created_at, status_updated_at)]
            if any(value.tzinfo is None for value in times):
                continue
        except (ValueError, TypeError, AttributeError):
            continue
        if (
            reaction.get("content") == "+1"
            and isinstance(login, str) and login.casefold() == issue_repo.split("/", 1)[0].casefold()
            and isinstance(reacted_at, str) and isinstance(created_at, str)
            and times[0] > times[2] and times[0] >= times[1]
            and unedited_owner_decision(comment, repository=issue_repo)
        ):
            return {
                "approval": "owner_reaction", "comment_url": comment.get("html_url"),
                "reaction_id": reaction.get("id"), "reacted_at": reacted_at,
            }
    return None


def check_owner_decides_not_planned(issue_repo: str, number: int, issue: dict[str, Any]) -> dict[str, Any]:
    """Refuse to drop milestone work the owner admitted unless the owner said so.

    Applies only to an issue in a milestone listed in the merged DIRECTION.md.
    The decision is an owner comment or an owner thumbs-up on an unedited
    recorded decision, after the body holding Current Status was last edited.
    Automation cannot supply the owner identity on either path.
    """
    milestone = issue.get("milestone")
    title = milestone.get("title") if isinstance(milestone, dict) else None
    if not isinstance(title, str):
        return {"required": False, "reason": "no_milestone"}
    direction_text = load_direction(issue_repo)
    if direction_text is None or title not in direction_milestone_titles(direction_text):
        return {"required": False, "reason": "milestone_not_listed_in_direction"}

    repo_owner, repo_name = issue_repo.split("/", 1)
    try:
        _, data = api_json(
            "POST",
            "/graphql",
            {
                "query": ISSUE_LAST_EDITED_QUERY,
                "variables": {"owner": repo_owner, "name": repo_name, "number": number},
            },
            is_write=False,
            failed_step="read_issue_last_edited",
            repository=issue_repo,
        )
        last_edited = data["data"]["repository"]["issue"]["lastEditedAt"]
    except (PlanError, KeyError, TypeError) as exc:
        raise owner_decision_refusal(
            f"Cannot close {issue_repo}#{number} as not planned: could not read when its "
            f"Current Status was last updated ({github_api_core.redact_string(str(exc))}).",
            cause="read_failure",
        ) from exc
    status_updated_at = last_edited or issue.get("created_at")
    if not isinstance(status_updated_at, str):
        raise owner_decision_refusal(
            f"Cannot close {issue_repo}#{number} as not planned: the issue has no creation or edit time.",
            cause="read_failure",
        )

    _, comments = collect_paged_rest_items(
        f"/repos/{issue_repo}/issues/{number}/comments",
        query={}, bucket="rest_core",
        step_prefix="owner_decision_comments",
    )
    for comment in comments:
        author = (comment.get("user") or {}).get("login")
        created_at = comment.get("created_at")
        if (
            isinstance(author, str)
            and author.casefold() == repo_owner.casefold()
            and isinstance(created_at, str)
            and created_at > status_updated_at
        ):
            return {
                "required": True,
                "milestone": title,
                "status_updated_at": status_updated_at,
                "comment_url": comment.get("html_url"),
            }
    try:
        for comment in comments:
            decision = owner_reaction_decision(issue_repo, number, comment, status_updated_at)
            if decision is not None:
                return {"required": True, "milestone": title, "status_updated_at": status_updated_at} | decision
    except (PlanError, KeyError, TypeError, AttributeError) as exc:
        raise owner_decision_refusal(
            f"Cannot close {issue_repo}#{number} as not planned: could not verify the owner's "
            "reaction and the decision's edit history.", cause="read_failure",
        ) from exc
    raise owner_decision_refusal(
        f"Cannot close {issue_repo}#{number} as not planned: it is in milestone {title!r} listed in "
        f"{issue_repo}:{DIRECTION_FILE}, and no comment by the repository owner {repo_owner!r} after the "
        f"last Current Status update ({status_updated_at}) records the decision. Ask the Director to comment "
        "on the issue, or react with thumbs-up to an unedited comment whose first line is "
        f"\"Director decision: Close #{number} as not planned.\", then rerun; do not close it another way."
    )


def cmd_milestone_close(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    gh_cmd, expected_actor = milestone_route()
    emit(
        github_milestone_core.close_milestone(
            repo,
            args.milestone,
            operation=CURRENT_OPERATION,
            actor=None,
            expected_actor=expected_actor,
            gh_cmd=gh_cmd,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = github_api_core.TerminalArgumentParser(description="Compact GitHub issue planning helper")
    parser.add_argument("--repo", help="Default OWNER/REPO for issue refs")
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=github_api_core.TerminalArgumentParser,
    )

    p = sub.add_parser("index", help="Compact plan issue index, no bodies")
    repository_scope.add_include_archived_argument(p)
    p.add_argument("--state", default="open", choices=["open", "closed", "all"])
    p.add_argument("--limit", type=positive_limit, default=20)
    p.add_argument("--label")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("search", help="Compact issue search, no bodies")
    repository_scope.add_include_archived_argument(p)
    p.add_argument("query")
    p.add_argument("--state", default="all", choices=["open", "closed", "all"])
    p.add_argument("--limit", type=positive_limit, default=20)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("show", help="Show issue sections and all comments")
    p.add_argument("--batch-read", action="store_true", help="Explicit cold/changed GraphQL slice; REST remains the unchanged-read route")
    p.add_argument("issue")
    p.add_argument("--full", action="store_true", help="Include the entire body instead of selected sections")
    p.add_argument("--sections", nargs="+")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("release-claim", help="Release an abandoned automation claim with verified closed-session evidence")
    p.add_argument("issue")
    p.add_argument("--claim-comment", type=int, required=True)
    p.add_argument("--evidence-comment", required=True, help="Canonical closed-session attestation issue-comment URL")
    p.add_argument("--role", choices=["supervisor", "direction"], required=True)
    p.add_argument("--session", required=True, help="Releasing session's actual native ID")
    p.add_argument("--confirm-session-ended", action="store_true", help="Attest verified closure, including unavailable peer inventory")
    p.add_argument("--termination-record", type=pathlib.Path, help="Private source-host crash record; freshly verifies process, transcripts and tmux for a Terminated session attestation")
    p.add_argument("--legacy", action="store_true", help="Release a bare legacy claim with no native session identity")
    p.add_argument("--legacy-branch", help="Verified original source branch; requires --legacy")
    p.add_argument("--legacy-reason", help="Why no native session ID exists; must match the legacy attestation")
    p.add_argument("--related-claim-comment", action="append", type=int, default=[])
    p.add_argument("--retained-pr", action="append", default=[])
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_release_claim)

    p = sub.add_parser("release", help="Release this session's own claim with the exact directive and a handoff")
    p.add_argument("issue")
    p.add_argument("--claim-comment", type=int, required=True)
    p.add_argument("--session", required=True, help="This session's native ID, as claimed")
    p.add_argument("--handoff-file", help="Handoff narrative posted below the directive; - reads stdin")
    p.add_argument("--agent", choices=github_agent.FAMILIES, help="Running family; defaults to harness detection")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("claim", help="Recheck, record, and read back issue or PR ownership before work")
    p.add_argument("issue")
    p.add_argument("--agent", choices=github_agent.FAMILIES, help="Running family; defaults to harness detection")
    p.add_argument("--agent-override", metavar="REASON", help="Record an explicit Director override for this session")
    p.add_argument("--worker", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--branch", required=True, help="Exact branch for this claim; create or continue its verified worktree only after claim succeeds")
    p.add_argument("--next-action", required=True)
    p.add_argument("--resume-from", type=int, help="Original released structured claim comment ID; resolve its retained handoff automatically")
    p.add_argument("--planning-checkout", help="Canonical planning repository checkout for cross-repository ownership inventory")
    p.add_argument("--wait-resolved", help="Existing resolution evidence for a recorded wait/hold; grants no new authority")
    p.add_argument("--admission", choices=github_plan_claim.ADMISSIONS,
                   help="Which overall Order rule admits this work; recorded with evidence, without an admission graph check")
    p.add_argument("--admission-link", action="append", default=[], metavar="URL",
                   help="Issue or PR URL, optionally with #issuecomment-ID; preserves the cited comment; repeat-stop needs both occurrences")
    p.add_argument("--admission-unlinked", metavar="REASON",
                   help="Optional context for milestone admission; recorded without a graph check")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("wait", help="Report legacy evidence or record a reviewed structured wait; never parses prose")
    p.add_argument("issue", nargs="?")
    p.add_argument("--all", action="store_true", help="Read every open issue/discussion and list records needing one review")
    choice = p.add_mutually_exclusive_group()
    choice.add_argument("--clear", action="store_true", help="Record verified absence/resolution after full review")
    choice.add_argument("--waiting-for", action="append", help="Person, decision or external event; repeat for every wait")
    p.add_argument("--ends-when", action="append", help="Observable ending condition; repeat in the same order as --waiting-for")
    p.add_argument("--evidence", help="Existing decision or full-record review evidence; grants no authority")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("create", help="Create a durable plan issue")
    p.add_argument("title", nargs="?", help="Issue title (positional)")
    p.add_argument("--title", dest="title_flag", help="Issue title (flag)")
    p.add_argument("--body")
    p.add_argument("--body-file")
    p.add_argument("--agent", choices=github_agent.FAMILIES, help="Assign this issue to an agent family")
    p.add_argument("--label", action="append")
    p.add_argument("--milestone")
    p.add_argument("--project")
    p.add_argument("--force", action="store_true")
    p.add_argument("--plan-status", choices=["active", "blocked", "waiting", "stale", "done", "none"], default="active")
    p.add_argument("--focus", choices=["Now", "Next", "Waiting", "Later"], help="Explicit Project field edit; not part of routine planning")
    p.add_argument("--manager", help="Explicit Project field edit; configured managers are not copied")
    p.add_argument("--finish-line", help="Set the issue body's Finish Line only")
    choice = p.add_mutually_exclusive_group()
    choice.add_argument("--wait-clear", action="store_true", help="Record reviewed absence of person/event/owner holds at creation")
    choice.add_argument("--waiting-for", action="append", help="Record every person/event hold; repeat per wait")
    p.add_argument("--ends-when", action="append", help="Ending condition for each wait, in order")
    p.add_argument("--wait-evidence", help="Existing decision or full-record review evidence")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("update-section", help="Patch one markdown section")
    p.add_argument("issue")
    p.add_argument("section")
    p.add_argument("--body", help="Section text; an empty string intentionally clears the section")
    p.add_argument("--body-file", help="Read section text from a file, or '-' for stdin; empty input clears the section")
    p.set_defaults(func=cmd_update_section)

    p = sub.add_parser("link", help="Create native issue relationships")
    p.add_argument("issue")
    p.add_argument("relationship", choices=["blocked-by", "blocks", "subissue", "related"])
    p.add_argument("target")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("unlink", help="Remove native issue relationships")
    p.add_argument("issue")
    p.add_argument("relationship", choices=["blocked-by", "blocks", "subissue", "related"])
    p.add_argument("target")
    p.set_defaults(func=cmd_unlink)

    p = sub.add_parser("deps", help="Show dependencies and sub-issues")
    p.add_argument("issue")
    p.set_defaults(func=cmd_deps)

    p = sub.add_parser("next", help="Rank the next actionable durable plans")
    p.add_argument("--batch-read", action="store_true", help="Explicit repository graph batch; REST remains the unchanged-read route")
    repository_scope.add_include_archived_argument(p)
    p.add_argument("--agent", choices=github_agent.FAMILIES, help="Running family; defaults to harness detection")
    p.add_argument("--milestone", help="Limit selection to one milestone by number or title")
    p.add_argument("--limit", type=positive_limit, default=5,
                   help="How many ranked results to return (default %(default)s); does not widen evaluation")
    p.add_argument("--scan-limit", type=positive_limit, default=NEXT_DEFAULT_SCAN_LIMIT,
                   help="How many issues to evaluate per scan allowance (default %(default)s); global next has separate graph and discovery allowances, each with an extra allowance for other-agent issues; discovery also reserves a held-repository allowance and evaluates marked incidents separately")
    p.add_argument("--repo-limit", type=positive_limit, help="Global next: bound accessible repository inventory (default 100)")
    p.add_argument("--repository-issue-limit", type=positive_limit, help=f"Global next: open issues per repository (default {NEXT_DEFAULT_SCAN_LIMIT})")
    p.add_argument("--comment-limit", type=positive_limit, help="Comments per evaluated issue (default 100)")
    p.add_argument("--read-workers", type=positive_limit, choices=range(1, 17), default=8, help="Global next: concurrent read workers (default %(default)s)")
    p.add_argument("--time-budget", type=positive_limit, default=180, help="Global next: total read budget in seconds (default %(default)s); raise for wider evidence")
    p.add_argument("--selection-context", help="Current owner holds and issue review evidence JSON; never a durable plan")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("close", help="Close a completed or not-planned plan safely")
    p.add_argument("issue")
    trigger_options = p.add_mutually_exclusive_group()
    trigger_options.add_argument("--reopen-trigger", help="Version-1 structured reopen-trigger JSON; published before closure")
    trigger_options.add_argument("--clear-reopen-trigger", metavar="EVIDENCE", help="Retire a handled reopen trigger and its discovery label")
    p.add_argument("--planning-checkout", help="Target repository checkout for planning metadata outside automatic discovery")
    p.add_argument("--reason", help="Closure reason (default completed; trigger-only updates preserve an already-closed reason)")
    p.add_argument("--unanswered-decision", metavar="COMMENT_URL",
                   help="The Director's comment on this issue, or a Director decision record they approved, to close over unanswered outside comments")
    p.add_argument("--comment", dest="body")
    p.add_argument("--comment-file", dest="body_file")
    p.add_argument("--owner")
    p.add_argument("--project")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("reconcile-landing", help="Reconcile landed source metadata and native parent/dependent status; report unproved finish lines")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--apply", action="store_true", help="Repair already-closed automation-owned plans; default is read-only")
    p.set_defaults(func=cmd_reconcile_landing)

    p = sub.add_parser("reconcile-status", help="Project an issue's status labels without repeating a fact write")
    p.add_argument("issue")
    p.add_argument("--planning-checkout", help="Verified target repository checkout used for root and same-repository configuration")
    p.add_argument("--include-dependents", action="store_true", help="Also refresh direct native parents and blocking dependents")
    p.add_argument("--apply", action="store_true", help="Apply the guarded label projection; default is read-only")
    p.set_defaults(func=cmd_reconcile_status)

    p = sub.add_parser("project-add", help="Add issue to a personal/org Project")
    p.add_argument("issue")
    p.add_argument("--owner")
    p.add_argument("--project")
    p.set_defaults(func=cmd_project_add)

    p = sub.add_parser("project-set", help="Set explicitly requested Project fields (no default writes)")
    p.add_argument("issue")
    p.add_argument("--owner")
    p.add_argument("--project")
    p.add_argument("--focus", choices=["Now", "Next", "Waiting", "Later"])
    p.add_argument("--manager")
    p.add_argument("--finish-line")
    p.add_argument("--item-id", help="Project item id returned by project-add; avoids rediscovery")
    p.set_defaults(func=cmd_project_set)

    p = sub.add_parser("project-list", help="List Projects for an owner")
    p.add_argument("--owner", required=True)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--closed", action="store_true")
    p.set_defaults(func=cmd_project_list)

    p = sub.add_parser("ensure-labels", help="Create fixed planning labels when missing")
    p.set_defaults(func=cmd_ensure_labels)

    p = sub.add_parser("milestone-list", help="List repository milestones")
    p.add_argument("--state", default="open", choices=["open", "closed", "all"])
    p.add_argument("--limit", type=positive_limit, default=100)
    p.set_defaults(func=cmd_milestone_list)

    p = sub.add_parser("milestone-show", help="Show one milestone by number or exact title")
    p.add_argument("milestone")
    p.set_defaults(func=cmd_milestone_show)

    p = sub.add_parser("milestone-create", help="Create an exact-title-idempotent milestone")
    p.add_argument("title")
    p.add_argument("--description")
    p.add_argument("--description-file")
    p.add_argument("--due-on")
    p.add_argument("--state", default="open", choices=["open", "closed"])
    p.set_defaults(func=cmd_milestone_create)

    p = sub.add_parser("milestone-update", help="Update or reopen a milestone")
    p.add_argument("milestone")
    p.add_argument("--title")
    p.add_argument("--description")
    p.add_argument("--description-file")
    due_on_group = p.add_mutually_exclusive_group()
    due_on_group.add_argument("--due-on")
    due_on_group.add_argument("--clear-due-on", action="store_true")
    p.add_argument("--state", choices=["open"])
    p.set_defaults(func=cmd_milestone_update)

    p = sub.add_parser("milestone-close", help="Guarded-close a milestone with no open items")
    p.add_argument("milestone")
    p.set_defaults(func=cmd_milestone_close)

    return parser


def command_repository(args: argparse.Namespace) -> str | None:
    """Bind single-repository commands without narrowing owner/global discovery."""
    if getattr(args, "issue", None):
        # A qualified issue overrides --repo, which is only its default.
        return issue_ref(claim_reference(args).issue, args.repo or "")[0] or default_repo()
    if args.repo:
        return args.repo
    if args.command == "project-list":
        return None
    if args.command == "search" and search_has_scope(args.query):
        return None
    repo = default_repo()
    if args.command == "next" and github_direction_next.is_direction_repository(repo):
        return None
    return repo


def main() -> None:
    global CURRENT_OPERATION, CURRENT_TRANSPORT, CURRENT_BUCKET, CURRENT_IS_WRITE
    global CURRENT_RETRY_FIELDS, CURRENT_RETRY_SUMMARY
    CURRENT_RETRY_FIELDS = {}
    CURRENT_RETRY_SUMMARY = None
    parser = build_parser()
    try:
        args = parser.parse_args()
    except github_api_core.ArgumentParsingError as exc:
        command = github_api_core.requested_subcommand(sys.argv[1:], set(PLAN_COMMAND_CONTEXT))
        CURRENT_OPERATION = f"github.plan.{command.replace('-', '_')}"
        CURRENT_TRANSPORT, CURRENT_BUCKET, CURRENT_IS_WRITE = PLAN_COMMAND_CONTEXT.get(
            command,
            ("helper", "unknown", False),
        )
        failure = github_api_core.FailureDetail(
            cause="validation_error",
            message=str(exc),
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="not_started" if CURRENT_IS_WRITE else None,
            failed_step="argument_parsing",
        )
        return die(
            str(exc),
            code=2,
            error_code="validation_error",
            failure=failure,
        )
    CURRENT_OPERATION = f"github.plan.{args.command.replace('-', '_')}"
    CURRENT_TRANSPORT, CURRENT_BUCKET, CURRENT_IS_WRITE = PLAN_COMMAND_CONTEXT.get(
        args.command,
        ("helper", "unknown", False),
    )
    if args.command == "wait" and not args.clear and not args.waiting_for:
        CURRENT_IS_WRITE = False
    previous_repo = os.environ.get("GH_REPO")
    try:
        target_repo = command_repository(args)
        if target_repo and not args.repo and getattr(args, "issue", None) and not repo_from_git():
            # Without a GitHub checkout a qualified issue is the only repository
            # the command names, so it is also the default for its other references.
            args.repo = target_repo
        # Bind readers, probes and transport before any cache is prepared.
        if target_repo:
            os.environ["GH_REPO"] = target_repo
        args.func(args)
    except ClassifiedPlanError as exc:
        die(
            str(exc),
            code=130 if plan_cancelled(exc) else 1,
            error_code="cancelled" if plan_cancelled(exc) else exc.code,
            retry_at=exc.retry_at,
            failure=exc.failure,
            api_result=exc.api_result,
            extra_payload=exc.payload,
        )
    except PlanError as exc:
        error_code = exc.failure.cause if exc.failure else classify_project_error(str(exc))
        die(
            str(exc),
            code=130 if plan_cancelled(exc) else 1,
            error_code=error_code,
            failure=exc.failure,
            api_result=exc.api_result,
            extra_payload=exc.payload,
        )
    except github_milestone_core.MilestoneError as exc:
        die(
            str(exc),
            code=130 if exc.failure.cause == "cancelled" else 1,
            error_code=exc.failure.cause,
            failure=exc.failure,
            api_result=exc.api_result,
            extra_payload=exc.payload,
        )
    except KeyboardInterrupt:
        failure = github_api_core.FailureDetail(
            cause="cancelled",
            message="Interrupted",
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="unknown" if CURRENT_IS_WRITE else None,
        )
        die("Interrupted", code=130, error_code="cancelled", failure=failure)
    finally:
        if previous_repo is None:
            os.environ.pop("GH_REPO", None)
        else:
            os.environ["GH_REPO"] = previous_repo


if __name__ == "__main__":
    main()
