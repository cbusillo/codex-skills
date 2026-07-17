#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Shared REST implementation for GitHub issue and pull-request comments."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any, Callable, Optional

import github_api as github_api_core


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
DEFAULT_GH = os.environ.get("GH_COMMENT_GH") or str(SCRIPT_DIR / "gh-with-env-token")
EXPECTED_ACTOR = os.environ.get("GH_WITH_ENV_TOKEN_EXPECTED_LOGIN") or "shiny-code-bot"
PER_PAGE = 100
MAX_PAGES = 1000
RECONCILIATION_CLOCK_SKEW_SECONDS = 5


class CommentError(Exception):
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


def effective_expected_actor(expected_actor: Optional[str]) -> Optional[str]:
    if str(os.environ.get("GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK") or "").lower() in {
        "1",
        "true",
        "yes",
    }:
        return None
    return expected_actor


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
) -> CommentError:
    failure = github_api_core.FailureDetail(
        cause=cause,
        message=message,
        retryable=retryable,
        fallback_eligible=False,
        disposition=disposition,
        write_outcome=write_outcome,
        completed_steps=list(completed_steps or []),
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
        completed_steps=list(completed_steps or []),
        failed_step=failed_step,
        failure=failure,
    )
    return CommentError(
        message,
        failure=failure,
        api_result=_public_api_result(result),
        payload=payload,
    )


def _enrich_error_with_retry_summaries(
    error: CommentError,
    retry_summaries: list[github_api_core.RetrySummary],
) -> None:
    summary = github_api_core.aggregate_retry_summaries(retry_summaries)
    if summary is None:
        return
    retry_fields = summary.as_dict()
    error.payload = github_api_core.redact_body({**retry_fields, **error.payload})
    if error.api_result is not None:
        error.api_result = github_api_core.redact_body({**retry_fields, **error.api_result})


def _raise_api_failure(
    result: github_api_core.ApiResult,
    *,
    completed_steps: list[str],
    failed_step: str,
    write_not_started: bool = False,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    if result.retry_summary is not None:
        retry_payload = result.retry_summary.as_dict()
        if retry_payload.get("reconciliation") is None and payload and "reconciliation" in payload:
            retry_payload.pop("reconciliation")
        payload = {**(payload or {}), **retry_payload}
    failure = result.failure or github_api_core.FailureDetail(
        cause="unknown_error",
        message="GitHub comment request failed",
        retryable=False,
        fallback_eligible=False,
        disposition="stop",
        write_outcome="not_started" if write_not_started else "unknown",
    )
    if write_not_started and failure.write_outcome is None:
        failure.write_outcome = "not_started"
    failure.completed_steps = list(completed_steps)
    failure.failed_step = failed_step
    result.completed_steps = list(completed_steps)
    result.failed_step = failed_step
    raise CommentError(
        failure.message,
        failure=failure,
        api_result=_public_api_result(result),
        payload=payload,
    )


def _normalized_repo(value: str) -> str:
    candidate = value.strip().strip("/")
    parts = candidate.split("/")
    if len(parts) == 3 and "." in parts[0]:
        parts = parts[1:]
    if len(parts) != 2 or not all(parts):
        raise ValueError("Repository must use OWNER/REPO or HOST/OWNER/REPO form")
    return f"{parts[0]}/{parts[1].removesuffix('.git')}"


def _repo_from_remote(remote: str) -> Optional[str]:
    match = re.search(r"(?:[:/])([^/:]+)/([^/]+?)(?:\.git)?$", remote.strip())
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def resolve_repo(
    explicit: Optional[str],
    *,
    gh_cmd: str = DEFAULT_GH,
    operation: str = "github.comment.unknown",
) -> str:
    for candidate in (explicit, os.environ.get("GH_REPO")):
        if candidate:
            try:
                return _normalized_repo(candidate)
            except ValueError as exc:
                raise _local_error(
                    str(exc),
                    operation=operation,
                    cause="validation_error",
                    failed_step="resolve_repository",
                ) from exc

    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        remote = None
    if remote is not None and remote.returncode == 0:
        resolved = _repo_from_remote(remote.stdout)
        if resolved:
            return resolved

    try:
        proc = subprocess.run(
            [gh_cmd, "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise _local_error(
            f"Could not launch GitHub repository resolver ({type(exc).__name__})",
            operation=operation,
            cause="subprocess_launch_failure",
            failed_step="resolve_repository",
            payload={"repo": explicit},
        ) from exc
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            return _normalized_repo(proc.stdout.strip())
        except ValueError:
            pass
    result = github_api_core.legacy_process_result(
        proc.returncode or 1,
        proc.stdout,
        proc.stderr,
        operation=operation,
        is_write=False,
        expected_actor=EXPECTED_ACTOR,
        transport="gh_cli_wrapper",
        bucket="mixed",
        failed_step="resolve_repository",
        command_started=proc.returncode != 127,
    )
    _raise_api_failure(
        result,
        completed_steps=[],
        failed_step="resolve_repository",
        write_not_started=True,
        payload={"repo": explicit},
    )
    raise AssertionError("unreachable")


def _call_api(
    method: str,
    path: str,
    payload: Any,
    *,
    gh_cmd: str,
    operation: str,
    actor: Optional[str],
    expected_actor: Optional[str],
    completed_steps: list[str],
    failed_step: str,
    is_write: bool,
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
        payload,
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
            write_not_started=not is_write,
            payload=failure_payload,
        )
    return result


def authenticated_actor(
    *,
    gh_cmd: str,
    operation: str,
    expected_actor: Optional[str],
    retry_summaries: Optional[list[github_api_core.RetrySummary]] = None,
) -> str:
    result = _call_api(
        "GET",
        "/user",
        None,
        gh_cmd=gh_cmd,
        operation=operation,
        actor=None,
        expected_actor=None,
        completed_steps=[],
        failed_step="resolve_actor",
        is_write=False,
        retry_summaries=retry_summaries,
    )
    login = result.body.get("login") if isinstance(result.body, dict) else None
    if not isinstance(login, str) or not login:
        raise _local_error(
            "GitHub actor lookup returned no login",
            operation=operation,
            cause="invalid_response",
            expected_actor=expected_actor,
            failed_step="resolve_actor",
        )
    if expected_actor and login.casefold() != expected_actor.casefold():
        failure = github_api_core.classify_error(
            0,
            {},
            None,
            is_write=True,
            expected_actor=expected_actor,
            actual_actor=login,
        )
        result = github_api_core.ApiResult(
            ok=False,
            status=0,
            body=None,
            operation=operation,
            actor=login,
            expected_actor=expected_actor,
            host=github_api_core.DEFAULT_HOST,
            transport="rest_api",
            bucket="rest_core",
            failure=failure,
            failed_step="resolve_actor",
        )
        _raise_api_failure(
            result,
            completed_steps=[],
            failed_step="resolve_actor",
            write_not_started=True,
            payload={"actor": login},
        )
    return login


def _has_next_page(headers: dict[str, str]) -> bool:
    return any('rel="next"' in part for part in headers.get("link", "").split(","))


def list_comments(
    repo: str,
    number: int,
    *,
    gh_cmd: str,
    operation: str,
    actor: str,
    expected_actor: Optional[str],
    completed_steps: list[str],
    retry_context: Optional[github_api_core.ReconciliationContext] = None,
    retry_summaries: Optional[list[github_api_core.RetrySummary]] = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    comments: list[dict[str, Any]] = []
    steps = list(completed_steps)
    for page in range(1, MAX_PAGES + 1):
        step = f"list_comments_page_{page}"
        result = _call_api(
            "GET",
            f"/repos/{repo}/issues/{number}/comments?per_page={PER_PAGE}&page={page}",
            None,
            gh_cmd=gh_cmd,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=steps,
            failed_step=step,
            is_write=False,
            retry_context=retry_context,
            retry_summaries=retry_summaries,
        )
        if not isinstance(result.body, list):
            raise _local_error(
                "GitHub comments response was not a list",
                operation=operation,
                cause="invalid_response",
                actor=actor,
                expected_actor=expected_actor,
                completed_steps=steps,
                failed_step=step,
            )
        comments.extend(item for item in result.body if isinstance(item, dict))
        steps.append(step)
        if not _has_next_page(result.headers):
            return comments, steps
    raise _local_error(
        f"GitHub comment pagination exceeded {MAX_PAGES} pages",
        operation=operation,
        cause="pagination_limit_exceeded",
        actor=actor,
        expected_actor=expected_actor,
        retryable=False,
        completed_steps=steps,
        failed_step="list_comments",
    )


def _latest_actor_comment(comments: list[dict[str, Any]], actor: str) -> Optional[dict[str, Any]]:
    authored = [
        comment
        for comment in comments
        if str((comment.get("user") or {}).get("login") or "").casefold() == actor.casefold()
    ]
    if not authored:
        return None
    return max(
        authored,
        key=lambda comment: (
            str(comment.get("created_at") or ""),
            int(comment.get("id") or 0),
        ),
    )


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


def _comment_fingerprint(repo: str, number: int, actor: str, body: str) -> str:
    canonical = json.dumps(
        {"repo": repo, "number": number, "actor": actor.casefold(), "body": body},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def reconcile_created_comment(
    repo: str,
    number: int,
    actor: str,
    started_at: str,
    *,
    gh_cmd: str,
    operation: str,
    expected_actor: Optional[str],
    completed_steps: list[str],
    fingerprint: str,
    operation_id: str,
    existing_comment_ids: set[int],
    retry_context: github_api_core.ReconciliationContext,
    retry_summaries: list[github_api_core.RetrySummary],
) -> github_api_core.ReconciliationDecision:
    try:
        comments, _ = list_comments(
            repo,
            number,
            gh_cmd=gh_cmd,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=completed_steps,
            retry_context=retry_context,
            retry_summaries=retry_summaries,
        )
    except CommentError as exc:
        return github_api_core.ReconciliationDecision(
            "failed",
            details={
                "request_fingerprint": fingerprint,
                "failure": exc.api_result or {"cause": exc.failure.cause},
            },
        )
    threshold = _parse_timestamp(started_at)
    if threshold is None:
        return github_api_core.ReconciliationDecision(
            "failed",
            details={
                "request_fingerprint": fingerprint,
                "failure": {"cause": "invalid_reconciliation_timestamp"},
            },
        )
    threshold -= dt.timedelta(seconds=RECONCILIATION_CLOCK_SKEW_SECONDS)
    matches: list[dict[str, Any]] = []
    for item in comments:
        item_actor = (item.get("user") or {}).get("login") if isinstance(item.get("user"), dict) else None
        created_at = _parse_timestamp(item.get("created_at"))
        comment_id = item.get("id")
        if not isinstance(comment_id, int) or comment_id in existing_comment_ids:
            continue
        if not isinstance(item_actor, str) or item_actor.casefold() != actor.casefold():
            continue
        if not github_api_core.body_has_operation_marker(item.get("body"), operation_id):
            continue
        if created_at is None or created_at < threshold:
            continue
        matches.append(item)
    details = {
        "request_fingerprint": fingerprint,
        "operation_id": operation_id,
        "started_at": started_at,
        "clock_skew_seconds": RECONCILIATION_CLOCK_SKEW_SECONDS,
        "preexisting_comment_ids": sorted(existing_comment_ids),
        "matching_comment_ids": [item.get("id") for item in matches],
    }
    if len(matches) == 1:
        return github_api_core.ReconciliationDecision("matched", body=matches[0], details=details)
    if len(matches) > 1:
        return github_api_core.ReconciliationDecision("ambiguous", details=details)
    return github_api_core.ReconciliationDecision("no_match", details=details)


def _comment_payload(
    comment: Any,
    *,
    operation: str,
    kind: str,
    repo: str,
    number: int,
    actor: str,
    expected_actor: Optional[str],
    comment_action: str,
    completed_steps: list[str],
    operation_marker: Optional[dict[str, Any]] = None,
    retry_summary: Optional[github_api_core.RetrySummary] = None,
) -> dict[str, Any]:
    if not isinstance(comment, dict) or not isinstance(comment.get("html_url"), str):
        raise _local_error(
            "GitHub comment write returned no comment URL",
            operation=operation,
            cause="invalid_response",
            actor=actor,
            expected_actor=expected_actor,
            write_outcome="unknown",
            completed_steps=completed_steps,
            failed_step="parse_comment_response",
            payload={
                "kind": kind,
                "repo": repo,
                "number": number,
                "comment_action": comment_action,
            },
        )
    author = (comment.get("user") or {}).get("login") if isinstance(comment.get("user"), dict) else None
    if isinstance(author, str) and expected_actor and author.casefold() != expected_actor.casefold():
        raise _local_error(
            f"GitHub comment write returned actor '{author}', expected '{expected_actor}'",
            operation=operation,
            cause="actor_mismatch",
            actor=author,
            expected_actor=expected_actor,
            write_outcome="unknown",
            completed_steps=completed_steps,
            failed_step="parse_comment_response",
            payload={
                "kind": kind,
                "repo": repo,
                "number": number,
                "comment_action": comment_action,
                "url": comment.get("html_url"),
                "reconciliation": {"strategy": "read_comment_by_url"},
            },
        )
    normalized = {
        "id": comment.get("id"),
        "url": comment.get("html_url"),
        "author": author,
        "created_at": comment.get("created_at"),
        "updated_at": comment.get("updated_at"),
    }
    payload = {
        "kind": kind,
        "repo": repo,
        "number": number,
        "actor": author if isinstance(author, str) and author else actor,
        "expected_actor": expected_actor,
        "comment_action": comment_action,
        "url": comment["html_url"],
        "comment": normalized,
        "completed_steps": completed_steps,
    }
    if operation_marker is not None:
        payload["operation_marker"] = operation_marker
    if retry_summary is not None:
        payload.update(retry_summary.as_dict())
    return payload


def _comment_impl(
    kind: str,
    number: int,
    body: str,
    *,
    repo: Optional[str] = None,
    edit_last: bool = False,
    create_if_none: bool = False,
    gh_cmd: str = DEFAULT_GH,
    expected_actor: Optional[str] = EXPECTED_ACTOR,
    operation: Optional[str] = None,
    completed_steps: Optional[list[str]] = None,
    failed_step: Optional[str] = None,
    retry_summaries: Optional[list[github_api_core.RetrySummary]] = None,
) -> dict[str, Any]:
    operation = operation or f"github.comment.{kind}"
    expected_actor = effective_expected_actor(expected_actor)
    if kind not in ("issue", "pr"):
        raise _local_error(
            "Comment kind must be 'issue' or 'pr'",
            operation=operation,
            cause="validation_error",
            expected_actor=expected_actor,
            failed_step="input_validation",
        )
    if number <= 0:
        raise _local_error(
            "Comment target number must be positive",
            operation=operation,
            cause="validation_error",
            expected_actor=expected_actor,
            failed_step="input_validation",
        )
    if not body:
        raise _local_error(
            "Comment body is empty",
            operation=operation,
            cause="validation_error",
            expected_actor=expected_actor,
            failed_step="input_validation",
        )
    if create_if_none and not edit_last:
        raise _local_error(
            "--create-if-none requires --edit-last",
            operation=operation,
            cause="validation_error",
            expected_actor=expected_actor,
            failed_step="input_validation",
        )

    resolved_repo = resolve_repo(repo, gh_cmd=gh_cmd, operation=operation)
    collected_retry_summaries = retry_summaries if retry_summaries is not None else []
    actor = authenticated_actor(
        gh_cmd=gh_cmd,
        operation=operation,
        expected_actor=expected_actor,
        retry_summaries=collected_retry_summaries,
    )
    steps = list(completed_steps or [])
    steps.append("resolve_actor")
    existing_comments: Optional[list[dict[str, Any]]] = None

    if edit_last:
        comments, steps = list_comments(
            resolved_repo,
            number,
            gh_cmd=gh_cmd,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=steps,
            retry_summaries=collected_retry_summaries,
        )
        existing_comments = comments
        selected = _latest_actor_comment(comments, actor)
        if selected is not None:
            selected_id = selected.get("id")
            if not isinstance(selected_id, int):
                raise _local_error(
                    "Selected GitHub comment has no numeric id",
                    operation=operation,
                    cause="invalid_response",
                    actor=actor,
                    expected_actor=expected_actor,
                    completed_steps=steps,
                    failed_step="select_comment",
                )
            update_step = failed_step or "update_comment"
            result = _call_api(
                "PATCH",
                f"/repos/{resolved_repo}/issues/comments/{selected_id}",
                {"body": body},
                gh_cmd=gh_cmd,
                operation=operation,
                actor=actor,
                expected_actor=expected_actor,
                completed_steps=steps,
                failed_step=update_step,
                is_write=True,
                retry_summaries=collected_retry_summaries,
                failure_payload={
                    "kind": kind,
                    "repo": resolved_repo,
                    "number": number,
                    "comment_action": "update",
                    "selected_comment_id": selected_id,
                    "reconciliation": {
                        "strategy": "selected_comment_lookup",
                        "creation_skipped": True,
                    },
                },
            )
            steps.append(update_step)
            return _comment_payload(
                result.body,
                operation=operation,
                kind=kind,
                repo=resolved_repo,
                number=number,
                actor=actor,
                expected_actor=expected_actor,
                comment_action="updated",
                completed_steps=steps,
                retry_summary=result.retry_summary,
            )
        if not create_if_none:
            raise _local_error(
                f"No existing comment by '{actor}' was found",
                operation=operation,
                cause="comment_not_found",
                actor=actor,
                expected_actor=expected_actor,
                completed_steps=steps,
                failed_step="select_comment",
                payload={
                    "kind": kind,
                    "repo": resolved_repo,
                    "number": number,
                    "comment_action": "not_found",
                },
            )

    if existing_comments is None:
        existing_comments, _ = list_comments(
            resolved_repo,
            number,
            gh_cmd=gh_cmd,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            completed_steps=steps,
            retry_summaries=collected_retry_summaries,
        )
    existing_comment_ids = {
        comment_id
        for item in existing_comments
        if isinstance((comment_id := item.get("id")), int)
    }
    create_step = failed_step or "create_comment"
    started_at = _format_timestamp(_utc_now())
    fingerprint = _comment_fingerprint(resolved_repo, number, actor, body)
    operation_id = github_api_core.new_operation_id()
    provider_body = github_api_core.body_with_operation_marker(body, operation_id)
    marker = {
        "kind": "request_fingerprint",
        "value": fingerprint,
        "started_at": started_at,
        "operation_id": operation_id,
    }
    reconciliation = {
        "strategy": "list_recent_actor_comments_and_match_operation_id",
        "required_before_retry": True,
        "request_fingerprint": fingerprint,
        "operation_id": operation_id,
        "started_at": started_at,
        "actor": actor,
    }
    result = _call_api(
        "POST",
        f"/repos/{resolved_repo}/issues/{number}/comments",
        {"body": provider_body},
        gh_cmd=gh_cmd,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        completed_steps=steps,
        failed_step=create_step,
        is_write=True,
        failure_payload={
            "kind": kind,
            "repo": resolved_repo,
            "number": number,
            "comment_action": "create",
            "operation_marker": marker,
            "reconciliation": reconciliation,
        },
        reconcile=lambda failed_result, retry_context: reconcile_created_comment(
            resolved_repo,
            number,
            failed_result.actor or actor,
            started_at,
            gh_cmd=gh_cmd,
            operation=operation,
            expected_actor=expected_actor,
            completed_steps=steps,
            fingerprint=fingerprint,
            operation_id=operation_id,
            existing_comment_ids=existing_comment_ids,
            retry_context=retry_context,
            retry_summaries=collected_retry_summaries,
        ),
        retry_summaries=collected_retry_summaries,
    )
    reconciled = bool(
        result.retry_summary
        and result.retry_summary.reconciliation
        and result.retry_summary.reconciliation.get("result") == "matched"
    )
    steps.append("reconcile_create_comment" if reconciled else create_step)
    return _comment_payload(
        result.body,
        operation=operation,
        kind=kind,
        repo=resolved_repo,
        number=number,
        actor=actor,
        expected_actor=expected_actor,
        comment_action="created",
        completed_steps=steps,
        operation_marker=marker,
        retry_summary=result.retry_summary,
    )


def comment(
    kind: str,
    number: int,
    body: str,
    *,
    repo: Optional[str] = None,
    edit_last: bool = False,
    create_if_none: bool = False,
    gh_cmd: str = DEFAULT_GH,
    expected_actor: Optional[str] = EXPECTED_ACTOR,
    operation: Optional[str] = None,
    completed_steps: Optional[list[str]] = None,
    failed_step: Optional[str] = None,
    retry_summaries: Optional[list[github_api_core.RetrySummary]] = None,
) -> dict[str, Any]:
    collected_retry_summaries = retry_summaries if retry_summaries is not None else []
    try:
        return _comment_impl(
            kind,
            number,
            body,
            repo=repo,
            edit_last=edit_last,
            create_if_none=create_if_none,
            gh_cmd=gh_cmd,
            expected_actor=expected_actor,
            operation=operation,
            completed_steps=completed_steps,
            failed_step=failed_step,
            retry_summaries=collected_retry_summaries,
        )
    except CommentError as error:
        _enrich_error_with_retry_summaries(error, collected_retry_summaries)
        raise


def read_body(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        return pathlib.Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise _local_error(
            f"Could not read comment body file: {github_api_core.redact_path(path)}",
            operation="github.comment.unknown",
            cause="input_error",
            failed_step="read_body",
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = github_api_core.TerminalArgumentParser(
        description="Create or update GitHub issue and pull-request timeline comments through REST.",
    )
    parser.add_argument("kind", choices=("issue", "pr"))
    parser.add_argument("number", type=int)
    parser.add_argument("-R", "--repo")
    parser.add_argument("--body-file", default="-", help="Read Markdown from a file, or '-' for stdin.")
    parser.add_argument("--edit-last", action="store_true")
    parser.add_argument("--create-if-none", action="store_true")
    return parser


def _terminal_failure(
    exc: CommentError,
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


def main() -> int:
    operation = "github.comment.unknown"
    try:
        args = build_parser().parse_args()
    except github_api_core.ArgumentParsingError as exc:
        kind = github_api_core.requested_subcommand(sys.argv[1:], {"issue", "pr"})
        operation = f"github.comment.{kind}"
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

    operation = f"github.comment.{args.kind}"
    expected_actor = effective_expected_actor(EXPECTED_ACTOR)
    try:
        body = read_body(args.body_file)
        payload = comment(
            args.kind,
            args.number,
            body,
            repo=args.repo,
            edit_last=args.edit_last,
            create_if_none=args.create_if_none,
            operation=operation,
            expected_actor=expected_actor,
        )
    except CommentError as exc:
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
