#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Shared REST implementation for GitHub issue mutations."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys
import urllib.parse
from typing import Any, Callable, Optional

import github_api as github_api_core
import github_comment


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
DEFAULT_GH = os.environ.get("GH_ISSUE_GH") or str(SCRIPT_DIR / "gh-with-env-token")
EXPECTED_ACTOR = os.environ.get("GH_WITH_ENV_TOKEN_EXPECTED_LOGIN") or "shiny-code-bot"
PER_PAGE = 100
MAX_PAGES = 1000
RECONCILIATION_CLOCK_SKEW_SECONDS = 5


class IssueError(Exception):
    def __init__(
        self,
        message: str,
        *,
        failure: github_api_core.FailureDetail,
        api_result: Optional[dict[str, Any]] = None,
        payload: Optional[dict[str, Any]] = None,
    ):
        super().__init__(github_api_core.redact_string(message))
        self.failure = failure
        self.api_result = github_api_core.redact_body(api_result) if api_result is not None else None
        self.payload = github_api_core.redact_body(payload or {})


def _public_api_result(result: github_api_core.ApiResult) -> dict[str, Any]:
    payload = result.as_dict()
    payload.pop("body", None)
    return payload


def _local_error(
    message: str,
    *,
    operation: str,
    cause: str,
    actor: Optional[str] = None,
    expected_actor: Optional[str] = EXPECTED_ACTOR,
    retryable: bool = False,
    disposition: str = "stop",
    write_outcome: Optional[str] = "not_started",
    completed_steps: Optional[list[str]] = None,
    failed_step: str,
    payload: Optional[dict[str, Any]] = None,
) -> IssueError:
    steps = list(completed_steps or [])
    failure = github_api_core.FailureDetail(
        cause=cause,
        message=message,
        retryable=retryable,
        fallback_eligible=False,
        disposition=disposition,
        write_outcome=write_outcome,
        completed_steps=steps,
        failed_step=failed_step,
    )
    result = github_api_core.ApiResult(
        ok=False,
        status=0,
        body=None,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        host=github_api_core.DEFAULT_HOST,
        transport="rest_api",
        bucket="rest_core",
        completed_steps=steps,
        failed_step=failed_step,
        failure=failure,
    )
    return IssueError(
        message,
        failure=failure,
        api_result=_public_api_result(result),
        payload=payload,
    )


def _enrich_error_with_retry_summaries(
    error: IssueError,
    retry_summaries: list[github_api_core.RetrySummary],
) -> None:
    summary = github_api_core.aggregate_retry_summaries(retry_summaries)
    if summary is None:
        return
    retry_fields = summary.as_dict()
    error.payload = github_api_core.redact_body({**retry_fields, **error.payload})
    if error.api_result is not None:
        error.api_result = github_api_core.redact_body({**retry_fields, **error.api_result})


def _from_comment_error(exc: github_comment.CommentError) -> IssueError:
    return IssueError(
        str(exc),
        failure=exc.failure,
        api_result=exc.api_result,
        payload=exc.payload,
    )


def _raise_api_failure(
    result: github_api_core.ApiResult,
    *,
    completed_steps: list[str],
    failed_step: str,
    write_outcome_if_missing: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    if result.retry_summary is not None:
        retry_payload = result.retry_summary.as_dict()
        if retry_payload.get("reconciliation") is None and payload and "reconciliation" in payload:
            retry_payload.pop("reconciliation")
        payload = {**(payload or {}), **retry_payload}
    failure = result.failure or github_api_core.FailureDetail(
        cause="unknown_error",
        message="GitHub issue request failed",
        retryable=False,
        fallback_eligible=False,
        disposition="stop",
        write_outcome=write_outcome_if_missing or "unknown",
    )
    if failure.write_outcome is None and write_outcome_if_missing is not None:
        failure.write_outcome = write_outcome_if_missing
    failure.completed_steps = list(completed_steps)
    failure.failed_step = failed_step
    result.completed_steps = list(completed_steps)
    result.failed_step = failed_step
    raise IssueError(
        failure.message,
        failure=failure,
        api_result=_public_api_result(result),
        payload=payload,
    )


def _call_api(
    method: str,
    path: str,
    body: Any,
    *,
    gh_cmd: str,
    operation: str,
    actor: Optional[str],
    expected_actor: Optional[str],
    completed_steps: list[str],
    failed_step: str,
    is_write: bool,
    write_outcome_if_missing: Optional[str] = None,
    failure_payload: Optional[dict[str, Any]] = None,
    reconcile: Optional[
        Callable[
            [github_api_core.ApiResult, github_api_core.ReconciliationContext],
            github_api_core.ReconciliationDecision,
        ]
    ] = None,
    retry_context: Optional[github_api_core.ReconciliationContext] = None,
    retry_summaries: Optional[list[github_api_core.RetrySummary]] = None,
) -> github_api_core.ApiResult:
    result = github_api_core.call_gh_with_retry(
        method,
        path,
        body,
        gh_cmd=gh_cmd,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        bucket="rest_core",
        completed_steps=completed_steps,
        failed_step=failed_step,
        is_write=is_write,
        reconcile=reconcile,
        retry_policy=retry_context.retry_policy if retry_context is not None else None,
        retry_runtime=retry_context.retry_runtime if retry_context is not None else None,
        deadline_at=retry_context.deadline_at if retry_context is not None else None,
    )
    if retry_summaries is not None and result.retry_summary is not None:
        retry_summaries.append(result.retry_summary)
        result.retry_summary = github_api_core.aggregate_retry_summaries(retry_summaries)
    result.transport = "rest_api"
    if not result.ok:
        _raise_api_failure(
            result,
            completed_steps=completed_steps,
            failed_step=failed_step,
            write_outcome_if_missing=write_outcome_if_missing,
            payload=failure_payload,
        )
    return result


def _resolve_repo(explicit: Optional[str], *, gh_cmd: str, operation: str) -> str:
    try:
        return github_comment.resolve_repo(explicit, gh_cmd=gh_cmd, operation=operation)
    except github_comment.CommentError as exc:
        raise _from_comment_error(exc) from exc


def _authenticated_actor(
    *,
    gh_cmd: str,
    operation: str,
    expected_actor: Optional[str],
    retry_summaries: Optional[list[github_api_core.RetrySummary]] = None,
) -> str:
    try:
        return github_comment.authenticated_actor(
            gh_cmd=gh_cmd,
            operation=operation,
            expected_actor=expected_actor,
            retry_summaries=retry_summaries,
        )
    except github_comment.CommentError as exc:
        raise _from_comment_error(exc) from exc


def _split_values(values: Optional[list[str]]) -> list[str]:
    normalized: list[str] = []
    for value in values or []:
        for item in value.split(","):
            item = item.strip()
            if item and item not in normalized:
                normalized.append(item)
    return normalized


def _normalize_assignees(values: Optional[list[str]], actor: str) -> list[str]:
    normalized: list[str] = []
    for value in _split_values(values):
        if value.casefold() == "@me":
            value = actor
        elif value.casefold() == "@copilot":
            value = "copilot-swe-agent[bot]"
        if value not in normalized:
            normalized.append(value)
    return normalized


def _has_next_page(headers: dict[str, str]) -> bool:
    return any('rel="next"' in part for part in headers.get("link", "").split(","))


def resolve_milestone_number(
    repo: str,
    value: str,
    *,
    gh_cmd: str,
    operation: str,
    actor: str,
    expected_actor: Optional[str],
    completed_steps: list[str],
    retry_summaries: Optional[list[github_api_core.RetrySummary]] = None,
) -> int:
    milestones: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        result = _call_api(
            "GET",
            f"/repos/{repo}/milestones?state=all&per_page={PER_PAGE}&page={page}",
            None,
            gh_cmd=gh_cmd,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=completed_steps,
            failed_step="resolve_milestone",
            is_write=False,
            write_outcome_if_missing="not_started",
            failure_payload={"repo": repo, "milestone": value},
            retry_summaries=retry_summaries,
        )
        if not isinstance(result.body, list):
            raise _local_error(
                "GitHub milestone lookup returned a non-list response",
                operation=operation,
                cause="invalid_response",
                actor=actor,
                expected_actor=expected_actor,
                completed_steps=completed_steps,
                failed_step="resolve_milestone",
                payload={"repo": repo, "milestone": value},
            )
        milestones.extend(item for item in result.body if isinstance(item, dict))
        if not _has_next_page(result.headers):
            break
    else:
        raise _local_error(
            "GitHub milestone lookup exceeded the page limit",
            operation=operation,
            cause="pagination_limit",
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=completed_steps,
            failed_step="resolve_milestone",
            payload={"repo": repo, "milestone": value},
        )

    exact = [item for item in milestones if item.get("title") == value]
    folded = [
        item
        for item in milestones
        if isinstance(item.get("title"), str) and item["title"].casefold() == value.casefold()
    ]
    matches = exact or folded
    if len(matches) == 1 and isinstance(matches[0].get("number"), int):
        return int(matches[0]["number"])
    if len(matches) > 1:
        raise _local_error(
            f"Milestone title is ambiguous: {value}",
            operation=operation,
            cause="validation_error",
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=completed_steps,
            failed_step="resolve_milestone",
            payload={"repo": repo, "milestone": value},
        )
    if value.isdigit():
        return int(value)
    raise _local_error(
        f"Milestone not found: {value}",
        operation=operation,
        cause="not_found",
        actor=actor,
        expected_actor=expected_actor,
        completed_steps=completed_steps,
        failed_step="resolve_milestone",
        payload={"repo": repo, "milestone": value},
    )


def _request_fingerprint(
    *,
    repo: str,
    title: str,
    body: str,
    labels: list[str],
    assignees: list[str],
    milestone: Optional[int],
) -> str:
    request = {
        "repo": repo.casefold(),
        "title": title,
        "body": body,
        "labels": sorted(labels, key=str.casefold),
        "assignees": sorted(assignees, key=str.casefold),
        "milestone": milestone,
    }
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _format_timestamp(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        return None


def _create_reconciliation(
    repo: str,
    creator: Optional[str],
    fingerprint: str,
    started_at: str,
    operation_id: str,
) -> dict[str, Any]:
    creator_query = f"&creator={urllib.parse.quote(creator, safe='')}" if creator else ""
    return {
        "strategy": "list_recent_issues_and_match_operation_id",
        "required_before_retry": True,
        "request_fingerprint": fingerprint,
        "operation_id": operation_id,
        "started_at": started_at,
        "clock_skew_seconds": RECONCILIATION_CLOCK_SKEW_SECONDS,
        "endpoint": (
            f"/repos/{repo}/issues?state=all{creator_query}"
            "&sort=created&direction=desc&per_page=100"
        ),
    }


def _matching_created_issues(
    repo: str,
    actor: str,
    creator: Optional[str],
    operation_id: Optional[str],
    started_at: str,
    *,
    gh_cmd: str,
    operation: str,
    expected_actor: Optional[str],
    completed_steps: list[str],
    existing_issue_ids: set[int],
    failed_step: str,
    write_outcome_if_missing: str,
    retry_context: Optional[github_api_core.ReconciliationContext] = None,
    retry_summaries: Optional[list[github_api_core.RetrySummary]] = None,
) -> list[dict[str, Any]]:
    threshold = _parse_timestamp(started_at)
    if threshold is None:
        raise _local_error(
            "Issue create reconciliation timestamp is invalid",
            operation=operation,
            cause="invalid_reconciliation_timestamp",
            actor=actor,
            expected_actor=expected_actor,
            write_outcome=write_outcome_if_missing,
            completed_steps=completed_steps,
            failed_step=failed_step,
        )
    threshold -= dt.timedelta(seconds=RECONCILIATION_CLOCK_SKEW_SECONDS)
    creator_query = f"&creator={urllib.parse.quote(creator, safe='')}" if creator else ""
    matches: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        result = _call_api(
            "GET",
            (
                f"/repos/{repo}/issues?state=all{creator_query}"
                f"&sort=created&direction=desc&per_page={PER_PAGE}&page={page}"
            ),
            None,
            gh_cmd=gh_cmd,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=completed_steps,
            failed_step=failed_step,
            is_write=False,
            write_outcome_if_missing=write_outcome_if_missing,
            retry_context=retry_context,
            retry_summaries=retry_summaries,
        )
        if not isinstance(result.body, list):
            raise _local_error(
                "GitHub issue candidate lookup returned a non-list response",
                operation=operation,
                cause="invalid_response",
                actor=actor,
                expected_actor=expected_actor,
                write_outcome=write_outcome_if_missing,
                completed_steps=completed_steps,
                failed_step=failed_step,
            )
        stop = False
        for item in result.body:
            if not isinstance(item, dict) or "pull_request" in item:
                continue
            created_at = _parse_timestamp(item.get("created_at"))
            if created_at is None:
                continue
            if created_at < threshold:
                stop = True
                continue
            issue_id = item.get("id")
            if not isinstance(issue_id, int) or issue_id in existing_issue_ids:
                continue
            if operation_id is None or github_api_core.body_has_operation_marker(
                item.get("body"), operation_id
            ):
                matches.append(item)
        if stop or not _has_next_page(result.headers):
            break
    return matches


def reconcile_created_issue(
    repo: str,
    actor: str,
    creator: Optional[str],
    fingerprint: str,
    operation_id: str,
    started_at: str,
    *,
    gh_cmd: str,
    operation: str,
    expected_actor: Optional[str],
    completed_steps: list[str],
    existing_issue_ids: set[int],
    retry_context: github_api_core.ReconciliationContext,
    retry_summaries: list[github_api_core.RetrySummary],
) -> Optional[dict[str, Any]]:
    matches = _matching_created_issues(
        repo,
        actor,
        creator,
        operation_id,
        started_at,
        gh_cmd=gh_cmd,
        operation=operation,
        expected_actor=expected_actor,
        completed_steps=completed_steps,
        existing_issue_ids=existing_issue_ids,
        failed_step="reconcile_create",
        write_outcome_if_missing="unknown",
        retry_context=retry_context,
        retry_summaries=retry_summaries,
    )
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise _local_error(
            "Issue create reconciliation found multiple matching issues",
            operation=operation,
            cause="ambiguous_reconciliation",
            actor=actor,
            expected_actor=expected_actor,
            write_outcome="unknown",
            completed_steps=completed_steps,
            failed_step="reconcile_create",
            payload={"repo": repo, "matching_issue_numbers": [item.get("number") for item in matches]},
        )
    return None


def _issue_payload(
    issue: Any,
    *,
    operation: str,
    repo: str,
    actor: str,
    expected_actor: Optional[str],
    completed_steps: list[str],
    operation_marker: Optional[dict[str, Any]] = None,
    retry_summary: Optional[github_api_core.RetrySummary] = None,
    verify_creator: bool = False,
) -> dict[str, Any]:
    if not isinstance(issue, dict) or not isinstance(issue.get("html_url"), str):
        raise _local_error(
            "GitHub issue write returned no issue URL",
            operation=operation,
            cause="invalid_response",
            actor=actor,
            expected_actor=expected_actor,
            write_outcome="unknown",
            completed_steps=completed_steps,
            failed_step="parse_issue_response",
            payload={"repo": repo, "reconciliation": {"strategy": "read_issue_by_url"}},
        )
    response_actor = None
    user = issue.get("user")
    if isinstance(user, dict) and isinstance(user.get("login"), str):
        response_actor = user["login"]
    if verify_creator and response_actor and expected_actor and response_actor.casefold() != expected_actor.casefold():
        raise _local_error(
            f"GitHub issue create returned actor '{response_actor}', expected '{expected_actor}'",
            operation=operation,
            cause="actor_mismatch",
            actor=response_actor,
            expected_actor=expected_actor,
            write_outcome="unknown",
            completed_steps=completed_steps,
            failed_step="parse_issue_response",
            payload={
                "repo": repo,
                "url": issue.get("html_url"),
                "reconciliation": {"strategy": "read_issue_by_url"},
            },
        )
    labels = [
        item["name"] if isinstance(item, dict) else item
        for item in issue.get("labels") or []
        if isinstance(item, str) or (isinstance(item, dict) and isinstance(item.get("name"), str))
    ]
    assignees = [
        item["login"]
        for item in issue.get("assignees") or []
        if isinstance(item, dict) and isinstance(item.get("login"), str)
    ]
    milestone = issue.get("milestone")
    normalized_milestone = None
    if isinstance(milestone, dict):
        normalized_milestone = {
            "number": milestone.get("number"),
            "title": milestone.get("title"),
        }
    payload: dict[str, Any] = {
        "repo": repo,
        "number": issue.get("number"),
        "title": issue.get("title"),
        "state": issue.get("state"),
        "state_reason": issue.get("state_reason"),
        "updated_at": issue.get("updated_at"),
        "actor": response_actor if verify_creator and response_actor else actor,
        "expected_actor": expected_actor,
        "url": issue["html_url"],
        "labels": labels,
        "assignees": assignees,
        "milestone": normalized_milestone,
        "completed_steps": completed_steps,
    }
    if operation_marker is not None:
        payload["operation_marker"] = operation_marker
    if retry_summary is not None:
        payload.update(retry_summary.as_dict())
    return payload


def create_issue(
    title: str,
    body: str,
    *,
    repo: Optional[str] = None,
    labels: Optional[list[str]] = None,
    assignees: Optional[list[str]] = None,
    milestone: Optional[str] = None,
    gh_cmd: str = DEFAULT_GH,
    expected_actor: Optional[str] = EXPECTED_ACTOR,
    operation: str = "github.issue.create",
) -> dict[str, Any]:
    expected_actor = github_comment.effective_expected_actor(expected_actor)
    if not title:
        raise _local_error(
            "Issue title is empty",
            operation=operation,
            cause="validation_error",
            expected_actor=expected_actor,
            failed_step="input_validation",
        )
    if not body:
        raise _local_error(
            "Issue body is empty",
            operation=operation,
            cause="validation_error",
            expected_actor=expected_actor,
            failed_step="input_validation",
        )
    resolved_repo = _resolve_repo(repo, gh_cmd=gh_cmd, operation=operation)
    retry_summaries: list[github_api_core.RetrySummary] = []
    actor = _authenticated_actor(
        gh_cmd=gh_cmd,
        operation=operation,
        expected_actor=expected_actor,
        retry_summaries=retry_summaries,
    )
    steps = ["resolve_actor"]
    normalized_labels = _split_values(labels)
    normalized_assignees = _normalize_assignees(assignees, actor)
    milestone_number = None
    if milestone is not None:
        milestone_number = resolve_milestone_number(
            resolved_repo,
            milestone,
            gh_cmd=gh_cmd,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=steps,
            retry_summaries=retry_summaries,
        )
        steps.append("resolve_milestone")
    request: dict[str, Any] = {"title": title, "body": body}
    if normalized_labels:
        request["labels"] = normalized_labels
    if normalized_assignees:
        request["assignees"] = normalized_assignees
    if milestone_number is not None:
        request["milestone"] = milestone_number
    fingerprint = _request_fingerprint(
        repo=resolved_repo,
        title=title,
        body=body,
        labels=normalized_labels,
        assignees=normalized_assignees,
        milestone=milestone_number,
    )
    operation_id = github_api_core.new_operation_id()
    request["body"] = github_api_core.body_with_operation_marker(body, operation_id)
    started_at = _format_timestamp(_utc_now())
    preexisting_matches = _matching_created_issues(
        resolved_repo,
        actor,
        None,
        None,
        started_at,
        gh_cmd=gh_cmd,
        operation=operation,
        expected_actor=expected_actor,
        completed_steps=steps,
        existing_issue_ids=set(),
        failed_step="snapshot_create_candidates",
        write_outcome_if_missing="not_started",
        retry_summaries=retry_summaries,
    )
    preexisting_issue_ids = {
        issue_id
        for item in preexisting_matches
        if isinstance((issue_id := item.get("id")), int)
    }
    marker = {
        "kind": "request_fingerprint",
        "value": fingerprint,
        "started_at": started_at,
        "operation_id": operation_id,
    }
    stable_creator = actor if expected_actor is not None else None
    reconciliation = _create_reconciliation(
        resolved_repo,
        stable_creator,
        fingerprint,
        started_at,
        operation_id,
    )
    reconciliation["preexisting_issue_ids"] = sorted(preexisting_issue_ids)

    def reconcile_failure(
        failed_result: github_api_core.ApiResult,
        retry_context: github_api_core.ReconciliationContext,
    ) -> github_api_core.ReconciliationDecision:
        reconciliation_actor = failed_result.actor or (actor if expected_actor is not None else None)
        if reconciliation_actor is None:
            return github_api_core.ReconciliationDecision(
                "failed",
                details={
                    **reconciliation,
                    "failure": {"cause": "actor_unknown", "failed_step": "reconcile_create"},
                },
            )
        retry_reconciliation = _create_reconciliation(
            resolved_repo,
            reconciliation_actor,
            fingerprint,
            started_at,
            operation_id,
        )
        retry_reconciliation["preexisting_issue_ids"] = sorted(preexisting_issue_ids)
        try:
            matched = reconcile_created_issue(
                resolved_repo,
                reconciliation_actor,
                reconciliation_actor,
                fingerprint,
                operation_id,
                started_at,
                gh_cmd=gh_cmd,
                operation=operation,
                expected_actor=expected_actor,
                completed_steps=steps,
                existing_issue_ids=preexisting_issue_ids,
                retry_context=retry_context,
                retry_summaries=retry_summaries,
            )
        except IssueError as reconciliation_error:
            return github_api_core.ReconciliationDecision(
                "failed",
                details={
                    **retry_reconciliation,
                    "failure": reconciliation_error.api_result or {
                        "cause": reconciliation_error.failure.cause,
                        "failed_step": reconciliation_error.failure.failed_step,
                    },
                },
            )
        if matched is None:
            return github_api_core.ReconciliationDecision("no_match", details=retry_reconciliation)
        return github_api_core.ReconciliationDecision("matched", body=matched, details=retry_reconciliation)

    result = _call_api(
        "POST",
        f"/repos/{resolved_repo}/issues",
        request,
        gh_cmd=gh_cmd,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        completed_steps=steps,
        failed_step="create_issue",
        is_write=True,
        failure_payload={
            "repo": resolved_repo,
            "operation_marker": marker,
            "reconciliation": reconciliation,
        },
        reconcile=reconcile_failure,
        retry_summaries=retry_summaries,
    )
    reconciled = bool(
        result.retry_summary
        and result.retry_summary.reconciliation
        and result.retry_summary.reconciliation.get("result") == "matched"
    )
    steps.append("reconcile_create" if reconciled else "create_issue")
    payload = _issue_payload(
        result.body,
        operation=operation,
        repo=resolved_repo,
        actor=result.actor or actor,
        expected_actor=expected_actor,
        completed_steps=steps,
        operation_marker=marker,
        retry_summary=result.retry_summary,
        verify_creator=True,
    )
    if reconciled:
        payload["reconciled"] = True
    return payload


def _edit_reconciliation(repo: str, number: int, requested: dict[str, Any]) -> dict[str, Any]:
    return {
        "strategy": "read_issue_and_compare_requested_fields",
        "endpoint": f"/repos/{repo}/issues/{number}",
        "requested": requested,
    }


def _edit_issue_impl(
    number: int,
    *,
    body: Optional[str] = None,
    title: Optional[str] = None,
    repo: Optional[str] = None,
    add_labels: Optional[list[str]] = None,
    remove_labels: Optional[list[str]] = None,
    add_assignees: Optional[list[str]] = None,
    remove_assignees: Optional[list[str]] = None,
    milestone: Optional[str] = None,
    remove_milestone: bool = False,
    gh_cmd: str = DEFAULT_GH,
    expected_actor: Optional[str] = EXPECTED_ACTOR,
    operation: str = "github.issue.edit",
    retry_summaries: list[github_api_core.RetrySummary],
) -> dict[str, Any]:
    expected_actor = github_comment.effective_expected_actor(expected_actor)
    if number <= 0:
        raise _local_error(
            "Issue number must be positive",
            operation=operation,
            cause="validation_error",
            expected_actor=expected_actor,
            failed_step="input_validation",
        )
    resolved_repo = _resolve_repo(repo, gh_cmd=gh_cmd, operation=operation)
    actor = _authenticated_actor(
        gh_cmd=gh_cmd,
        operation=operation,
        expected_actor=expected_actor,
        retry_summaries=retry_summaries,
    )
    steps = ["resolve_actor"]
    normalized_add_labels = _split_values(add_labels)
    normalized_remove_labels = _split_values(remove_labels)
    normalized_add_assignees = _normalize_assignees(add_assignees, actor)
    normalized_remove_assignees = _normalize_assignees(remove_assignees, actor)
    if set(normalized_add_labels) & set(normalized_remove_labels):
        raise _local_error(
            "The same label cannot be added and removed in one edit",
            operation=operation,
            cause="validation_error",
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=steps,
            failed_step="input_validation",
        )
    if set(normalized_add_assignees) & set(normalized_remove_assignees):
        raise _local_error(
            "The same assignee cannot be added and removed in one edit",
            operation=operation,
            cause="validation_error",
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=steps,
            failed_step="input_validation",
        )
    milestone_number: Optional[int] = None
    if milestone is not None:
        milestone_number = resolve_milestone_number(
            resolved_repo,
            milestone,
            gh_cmd=gh_cmd,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=steps,
            retry_summaries=retry_summaries,
        )
        steps.append("resolve_milestone")
    core: dict[str, Any] = {}
    if body is not None:
        core["body"] = body
    if title is not None:
        core["title"] = title
    if milestone is not None:
        core["milestone"] = milestone_number
    elif remove_milestone:
        core["milestone"] = None
    requested = {
        "fields": sorted(core),
        "add_labels": normalized_add_labels,
        "remove_labels": normalized_remove_labels,
        "add_assignees": normalized_add_assignees,
        "remove_assignees": normalized_remove_assignees,
    }
    if not core and not any(
        (
            normalized_add_labels,
            normalized_remove_labels,
            normalized_add_assignees,
            normalized_remove_assignees,
        )
    ):
        raise _local_error(
            "No issue fields or membership changes were provided",
            operation=operation,
            cause="validation_error",
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=steps,
            failed_step="input_validation",
        )
    reconciliation = _edit_reconciliation(resolved_repo, number, requested)
    failure_payload = {"repo": resolved_repo, "number": number, "reconciliation": reconciliation}
    if core:
        _call_api(
            "PATCH",
            f"/repos/{resolved_repo}/issues/{number}",
            core,
            gh_cmd=gh_cmd,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=steps,
            failed_step="edit_issue_fields",
            is_write=True,
            failure_payload=failure_payload,
            retry_summaries=retry_summaries,
        )
        steps.append("edit_issue_fields")
    if normalized_add_labels:
        _call_api(
            "POST",
            f"/repos/{resolved_repo}/issues/{number}/labels",
            {"labels": normalized_add_labels},
            gh_cmd=gh_cmd,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=steps,
            failed_step="add_labels",
            is_write=True,
            failure_payload=failure_payload,
            retry_summaries=retry_summaries,
        )
        steps.append("add_labels")
    for label in normalized_remove_labels:
        encoded = urllib.parse.quote(label, safe="")
        _call_api(
            "DELETE",
            f"/repos/{resolved_repo}/issues/{number}/labels/{encoded}",
            None,
            gh_cmd=gh_cmd,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=steps,
            failed_step="remove_label",
            is_write=True,
            failure_payload=failure_payload,
            retry_summaries=retry_summaries,
        )
        steps.append("remove_label")
    if normalized_add_assignees:
        _call_api(
            "POST",
            f"/repos/{resolved_repo}/issues/{number}/assignees",
            {"assignees": normalized_add_assignees},
            gh_cmd=gh_cmd,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=steps,
            failed_step="add_assignees",
            is_write=True,
            failure_payload=failure_payload,
            retry_summaries=retry_summaries,
        )
        steps.append("add_assignees")
    if normalized_remove_assignees:
        _call_api(
            "DELETE",
            f"/repos/{resolved_repo}/issues/{number}/assignees",
            {"assignees": normalized_remove_assignees},
            gh_cmd=gh_cmd,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=steps,
            failed_step="remove_assignees",
            is_write=True,
            failure_payload=failure_payload,
            retry_summaries=retry_summaries,
        )
        steps.append("remove_assignees")
    result = _call_api(
        "GET",
        f"/repos/{resolved_repo}/issues/{number}",
        None,
        gh_cmd=gh_cmd,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        completed_steps=steps,
        failed_step="read_after_write",
        is_write=False,
        write_outcome_if_missing="unknown" if steps[1:] else "not_started",
        failure_payload=failure_payload,
        retry_summaries=retry_summaries,
    )
    steps.append("read_after_write")
    return _issue_payload(
        result.body,
        operation=operation,
        repo=resolved_repo,
        actor=actor if expected_actor is not None else None,
        expected_actor=expected_actor,
        completed_steps=steps,
        retry_summary=github_api_core.aggregate_retry_summaries(retry_summaries),
    )


def edit_issue(
    number: int,
    *,
    body: Optional[str] = None,
    title: Optional[str] = None,
    repo: Optional[str] = None,
    add_labels: Optional[list[str]] = None,
    remove_labels: Optional[list[str]] = None,
    add_assignees: Optional[list[str]] = None,
    remove_assignees: Optional[list[str]] = None,
    milestone: Optional[str] = None,
    remove_milestone: bool = False,
    gh_cmd: str = DEFAULT_GH,
    expected_actor: Optional[str] = EXPECTED_ACTOR,
    operation: str = "github.issue.edit",
) -> dict[str, Any]:
    retry_summaries: list[github_api_core.RetrySummary] = []
    try:
        return _edit_issue_impl(
            number,
            body=body,
            title=title,
            repo=repo,
            add_labels=add_labels,
            remove_labels=remove_labels,
            add_assignees=add_assignees,
            remove_assignees=remove_assignees,
            milestone=milestone,
            remove_milestone=remove_milestone,
            gh_cmd=gh_cmd,
            expected_actor=expected_actor,
            operation=operation,
            retry_summaries=retry_summaries,
        )
    except IssueError as error:
        _enrich_error_with_retry_summaries(error, retry_summaries)
        raise


def _issue_reference(value: str, default_repo: str) -> tuple[str, int]:
    candidate = value.strip()
    if candidate.isdigit():
        return default_repo, int(candidate)
    if candidate.startswith("#") and candidate[1:].isdigit():
        return default_repo, int(candidate[1:])
    if "#" in candidate:
        repo, _, number = candidate.rpartition("#")
        if number.isdigit() and "/" in repo:
            parts = repo.strip().strip("/").split("/")
            if len(parts) == 3 and "." in parts[0]:
                parts = parts[1:]
            if len(parts) == 2 and all(parts):
                return f"{parts[0]}/{parts[1].removesuffix('.git')}", int(number)
    parsed = urllib.parse.urlparse(candidate)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme and parsed.netloc and len(parts) >= 4 and parts[-2] == "issues" and parts[-1].isdigit():
        return f"{parts[-4]}/{parts[-3]}", int(parts[-1])
    raise ValueError(f"Unsupported issue reference: {value}")


def _state_reconciliation(repo: str, number: int, state: str, state_reason: str) -> dict[str, Any]:
    return {
        "strategy": "read_issue_and_compare_state",
        "endpoint": f"/repos/{repo}/issues/{number}",
        "expected_state": state,
        "expected_state_reason": state_reason,
    }


def _set_issue_state_impl(
    number: int,
    *,
    state: str,
    state_reason: str,
    repo: Optional[str] = None,
    comment_body: Optional[str] = None,
    duplicate_of: Optional[str] = None,
    gh_cmd: str = DEFAULT_GH,
    expected_actor: Optional[str] = EXPECTED_ACTOR,
    operation: Optional[str] = None,
    retry_summaries: list[github_api_core.RetrySummary],
) -> dict[str, Any]:
    operation = operation or f"github.issue.{'close' if state == 'closed' else 'reopen'}"
    expected_actor = github_comment.effective_expected_actor(expected_actor)
    if number <= 0:
        raise _local_error(
            "Issue number must be positive",
            operation=operation,
            cause="validation_error",
            expected_actor=expected_actor,
            failed_step="input_validation",
        )
    resolved_repo = _resolve_repo(repo, gh_cmd=gh_cmd, operation=operation)
    mutation_step = "close_issue" if state == "closed" else "reopen_issue"
    comment_step = "post_close_comment" if state == "closed" else "post_reopen_comment"
    steps: list[str] = []
    actor = _authenticated_actor(
        gh_cmd=gh_cmd,
        operation=operation,
        expected_actor=expected_actor,
        retry_summaries=retry_summaries,
    )
    request: dict[str, Any] = {"state": state, "state_reason": state_reason}
    if duplicate_of is not None:
        try:
            duplicate_repo, duplicate_number = _issue_reference(duplicate_of, resolved_repo)
        except ValueError as exc:
            raise _local_error(
                str(exc),
                operation=operation,
                cause="validation_error",
                actor=actor,
                expected_actor=expected_actor,
                completed_steps=steps,
                failed_step="resolve_duplicate_issue",
            ) from exc
        result = _call_api(
            "GET",
            f"/repos/{duplicate_repo}/issues/{duplicate_number}",
            None,
            gh_cmd=gh_cmd,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=steps,
            failed_step="resolve_duplicate_issue",
            is_write=False,
            write_outcome_if_missing="not_started",
            retry_summaries=retry_summaries,
        )
        duplicate_id = result.body.get("id") if isinstance(result.body, dict) else None
        if not isinstance(duplicate_id, int):
            raise _local_error(
                "Duplicate issue lookup returned no database id",
                operation=operation,
                cause="invalid_response",
                actor=actor,
                expected_actor=expected_actor,
                write_outcome="not_started",
                completed_steps=steps,
                failed_step="resolve_duplicate_issue",
            )
        request["duplicate_issue_id"] = duplicate_id
        steps.append("resolve_duplicate_issue")
    if comment_body:
        try:
            comment_payload = github_comment.comment(
                "issue",
                number,
                comment_body,
                repo=resolved_repo,
                gh_cmd=gh_cmd,
                expected_actor=expected_actor,
                operation="github.comment.issue",
                completed_steps=steps,
                failed_step=comment_step,
                retry_summaries=retry_summaries,
            )
        except github_comment.CommentError as exc:
            raise _from_comment_error(exc) from exc
        actor = comment_payload.get("actor") or actor
        steps.append(comment_step)
    reconciliation = _state_reconciliation(resolved_repo, number, state, state_reason)
    result = _call_api(
        "PATCH",
        f"/repos/{resolved_repo}/issues/{number}",
        request,
        gh_cmd=gh_cmd,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        completed_steps=steps,
        failed_step=mutation_step,
        is_write=True,
        failure_payload={
            "repo": resolved_repo,
            "number": number,
            "reconciliation": reconciliation,
        },
        retry_summaries=retry_summaries,
    )
    steps.append(mutation_step)
    return _issue_payload(
        result.body,
        operation=operation,
        repo=resolved_repo,
        actor=actor if expected_actor is not None or comment_body else None,
        expected_actor=expected_actor,
        completed_steps=steps,
        retry_summary=github_api_core.aggregate_retry_summaries(retry_summaries),
    )


def set_issue_state(
    number: int,
    *,
    state: str,
    state_reason: str,
    repo: Optional[str] = None,
    comment_body: Optional[str] = None,
    duplicate_of: Optional[str] = None,
    gh_cmd: str = DEFAULT_GH,
    expected_actor: Optional[str] = EXPECTED_ACTOR,
    operation: Optional[str] = None,
) -> dict[str, Any]:
    retry_summaries: list[github_api_core.RetrySummary] = []
    try:
        return _set_issue_state_impl(
            number,
            state=state,
            state_reason=state_reason,
            repo=repo,
            comment_body=comment_body,
            duplicate_of=duplicate_of,
            gh_cmd=gh_cmd,
            expected_actor=expected_actor,
            operation=operation,
            retry_summaries=retry_summaries,
        )
    except IssueError as error:
        _enrich_error_with_retry_summaries(error, retry_summaries)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = github_api_core.TerminalArgumentParser(
        description="Create, edit, close, and reopen GitHub issues through REST.",
    )
    sub = parser.add_subparsers(dest="command", required=True, parser_class=github_api_core.TerminalArgumentParser)

    create = sub.add_parser("create")
    create.add_argument("title")
    create.add_argument("-R", "--repo")
    create.add_argument("-l", "--label", action="append", default=[])
    create.add_argument("-a", "--assignee", action="append", default=[])
    create.add_argument("-m", "--milestone")

    edit = sub.add_parser("edit")
    edit.add_argument("number")
    edit.add_argument("-R", "--repo")
    edit.add_argument("-t", "--title")
    edit.add_argument("--add-label", action="append", default=[])
    edit.add_argument("--remove-label", action="append", default=[])
    edit.add_argument("--add-assignee", action="append", default=[])
    edit.add_argument("--remove-assignee", action="append", default=[])
    milestone_group = edit.add_mutually_exclusive_group()
    milestone_group.add_argument("-m", "--milestone")
    milestone_group.add_argument("--remove-milestone", action="store_true")

    close = sub.add_parser("close")
    close.add_argument("number")
    close.add_argument("-R", "--repo")
    close.add_argument("-c", "--comment")
    close_mode = close.add_mutually_exclusive_group()
    close_mode.add_argument("-r", "--reason", default="completed")
    close_mode.add_argument("--duplicate-of")

    reopen = sub.add_parser("reopen")
    reopen.add_argument("number")
    reopen.add_argument("-R", "--repo")
    reopen.add_argument("-c", "--comment")
    return parser


def _terminal_failure(
    exc: IssueError,
    operation: str,
    *,
    expected_actor: Optional[str],
    exit_code: int = 1,
) -> dict[str, Any]:
    api_result = exc.api_result or {}
    envelope = github_api_core.terminal_failure(
        exc.failure,
        operation=operation,
        payload=exc.payload,
        actor=api_result.get("actor") or exc.payload.get("actor"),
        expected_actor=api_result.get("expected_actor") if "expected_actor" in api_result else expected_actor,
        host=api_result.get("host") or github_api_core.DEFAULT_HOST,
        transport=api_result.get("transport") or "rest_api",
        bucket=api_result.get("bucket") or "rest_core",
        status=int(api_result.get("status") or 0),
        exit_code=exit_code,
        request_id=api_result.get("request_id"),
        completed_steps=api_result.get("completed_steps") or exc.failure.completed_steps,
        failed_step=api_result.get("failed_step") or exc.failure.failed_step,
        error=str(exc),
        error_code=exc.failure.cause,
    )
    for key in ("quota", "rate_limit", "retry_at", "retry_after", "write_outcome", "disposition"):
        if api_result.get(key) is not None:
            envelope[key] = api_result[key]
    return envelope


def _read_stdin(*, optional: bool) -> str:
    if optional and sys.stdin.isatty():
        return ""
    return sys.stdin.read()


def _close_reason(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "completed": "completed",
        "not_planned": "not_planned",
        "duplicate": "duplicate",
    }
    if normalized not in mapping:
        raise ValueError("Close reason must be completed, not planned, or duplicate")
    return mapping[normalized]


def _resolve_cli_target(value: str, repo: Optional[str], *, operation: str) -> tuple[str, int]:
    target_repo, target_number = _issue_reference(value, "")
    if target_repo:
        return target_repo, target_number
    return _resolve_repo(repo, gh_cmd=DEFAULT_GH, operation=operation), target_number


def main() -> int:
    operation = "github.issue.unknown"
    try:
        args = build_parser().parse_args()
    except github_api_core.ArgumentParsingError as exc:
        command = github_api_core.requested_subcommand(
            sys.argv[1:],
            {"create", "edit", "close", "reopen"},
        )
        operation = f"github.issue.{command}"
        failure = github_api_core.FailureDetail(
            cause="validation_error",
            message=str(exc),
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="not_started",
            failed_step="argument_parsing",
        )
        return github_api_core.emit_terminal(
            github_api_core.terminal_failure(
                failure,
                operation=operation,
                expected_actor=EXPECTED_ACTOR,
                transport="rest_api",
                bucket="rest_core",
                exit_code=2,
                failed_step="argument_parsing",
            ),
            stderr_message=f"error: {exc}",
        )

    operation = f"github.issue.{args.command}"
    expected_actor = github_comment.effective_expected_actor(EXPECTED_ACTOR)
    try:
        if args.command == "create":
            payload = create_issue(
                args.title,
                _read_stdin(optional=False),
                repo=args.repo,
                labels=args.label,
                assignees=args.assignee,
                milestone=args.milestone,
                expected_actor=expected_actor,
            )
        elif args.command == "edit":
            target_repo, target_number = _resolve_cli_target(args.number, args.repo, operation=operation)
            stdin_body = _read_stdin(optional=True)
            payload = edit_issue(
                target_number,
                body=stdin_body if stdin_body else None,
                title=args.title,
                repo=target_repo,
                add_labels=args.add_label,
                remove_labels=args.remove_label,
                add_assignees=args.add_assignee,
                remove_assignees=args.remove_assignee,
                milestone=args.milestone,
                remove_milestone=args.remove_milestone,
                expected_actor=expected_actor,
            )
        elif args.command == "close":
            target_repo, target_number = _resolve_cli_target(args.number, args.repo, operation=operation)
            stdin_comment = _read_stdin(optional=True)
            comment_body = stdin_comment if stdin_comment else args.comment
            reason = _close_reason("duplicate" if args.duplicate_of else args.reason)
            payload = set_issue_state(
                target_number,
                state="closed",
                state_reason=reason,
                repo=target_repo,
                comment_body=comment_body,
                duplicate_of=args.duplicate_of,
                expected_actor=expected_actor,
                operation=operation,
            )
        else:
            target_repo, target_number = _resolve_cli_target(args.number, args.repo, operation=operation)
            stdin_comment = _read_stdin(optional=True)
            comment_body = stdin_comment if stdin_comment else args.comment
            payload = set_issue_state(
                target_number,
                state="open",
                state_reason="reopened",
                repo=target_repo,
                comment_body=comment_body,
                expected_actor=expected_actor,
                operation=operation,
            )
    except ValueError as exc:
        issue_error = _local_error(
            str(exc),
            operation=operation,
            cause="validation_error",
            expected_actor=expected_actor,
            failed_step="input_validation",
        )
        return github_api_core.emit_terminal(
            _terminal_failure(issue_error, operation, expected_actor=expected_actor, exit_code=2),
            stderr_message=f"error: {issue_error}",
        )
    except IssueError as exc:
        return github_api_core.emit_terminal(
            _terminal_failure(exc, operation, expected_actor=expected_actor),
            stderr_message=f"error: {exc}",
        )

    completed_steps = payload.pop("completed_steps", [])
    envelope = github_api_core.terminal_success(
        payload,
        operation=operation,
        actor=payload.get("actor"),
        expected_actor=payload.get("expected_actor"),
        transport="rest_api",
        bucket="rest_core",
        completed_steps=completed_steps,
    )
    envelope["body"] = payload.get("url")
    return github_api_core.emit_terminal(envelope)


if __name__ == "__main__":
    raise SystemExit(main())
