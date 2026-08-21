#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Shared REST implementation for GitHub milestone lifecycle operations."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any, Optional

import github_api as github_api_core
import github_identity


EXPECTED_ACTOR = github_identity.automation_login()
DEFAULT_GH = github_api_core.DEFAULT_GH
PER_PAGE = 100
MAX_PAGES = 100
MAX_ITEMS = PER_PAGE * MAX_PAGES
_MISSING = object()


class MilestoneError(Exception):
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


def _success_metadata(
    actor: Optional[str],
    expected_actor: Optional[str],
    retry_summaries: list[github_api_core.RetrySummary],
) -> dict[str, Any]:
    summary = github_api_core.aggregate_retry_summaries(retry_summaries)
    payload: dict[str, Any] = {
        "actor": summary.last_actor if summary and summary.last_actor else actor,
        "expected_actor": expected_actor,
    }
    if summary is not None:
        payload.update(summary.as_dict())
    return payload


def _local_error(
    message: str,
    *,
    operation: str,
    cause: str,
    actor: Optional[str],
    expected_actor: Optional[str],
    failed_step: str,
    completed_steps: Optional[list[str]] = None,
    write_outcome: Optional[str] = "not_started",
    payload: Optional[dict[str, Any]] = None,
    retry_summaries: Optional[list[github_api_core.RetrySummary]] = None,
) -> MilestoneError:
    steps = list(completed_steps or [])
    failure = github_api_core.FailureDetail(
        cause=cause,
        message=message,
        retryable=False,
        fallback_eligible=False,
        disposition="stop",
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
        retry_summary=github_api_core.aggregate_retry_summaries(retry_summaries or []),
    )
    return MilestoneError(
        message,
        failure=failure,
        api_result=_public_api_result(result),
        payload=payload,
    )


def _validate_repo(repo: str) -> str:
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo or ""):
        raise ValueError(f"invalid repository: {repo}")
    return repo


def _require_repo(
    repo: str,
    *,
    operation: str,
    actor: Optional[str],
    expected_actor: Optional[str],
) -> str:
    try:
        return _validate_repo(repo)
    except ValueError as exc:
        raise _local_error(
            str(exc),
            operation=operation,
            cause="validation_error",
            actor=actor,
            expected_actor=expected_actor,
            failed_step="validate_repo",
        ) from exc


def _command_state(
    repo: str,
    *,
    operation: str,
    actor: Optional[str],
    expected_actor: Optional[str],
    gh_cmd: str,
    verify_actor: bool,
) -> tuple[str, Optional[str], Optional[str], list[str], list[github_api_core.RetrySummary]]:
    effective_expected_actor = (
        None if github_identity.active_auth_fallback_allowed() else expected_actor
    )
    repo = _require_repo(
        repo,
        operation=operation,
        actor=actor,
        expected_actor=effective_expected_actor,
    )
    steps: list[str] = []
    retry_summaries: list[github_api_core.RetrySummary] = []
    if verify_actor:
        actor = _authenticated_actor(
            gh_cmd=gh_cmd,
            operation=operation,
            expected_actor=effective_expected_actor,
            completed_steps=steps,
            retry_summaries=retry_summaries,
        )
    return repo, actor, effective_expected_actor, steps, retry_summaries


def normalize_due_on(value: Any) -> Optional[str]:
    """Normalize GitHub milestone due dates to UTC RFC3339 seconds or null."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, dt.date):
        parsed = dt.datetime.combine(value, dt.time())
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            parsed = dt.datetime.strptime(text, "%Y-%m-%d")
        else:
            try:
                parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"invalid due_on: {value}") from exc
    else:
        raise ValueError(f"invalid due_on: {value}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_milestone(milestone: Any) -> dict[str, Any]:
    if not isinstance(milestone, dict):
        raise ValueError("GitHub milestone response was not an object")
    state = milestone.get("state")
    if isinstance(state, str):
        state = state.lower()
    normalized = {
        "number": milestone.get("number"),
        "title": milestone.get("title"),
        "state": state,
        "description": milestone.get("description") or "",
        "due_on": normalize_due_on(milestone.get("due_on")),
        "open_issues": milestone.get("open_issues", 0),
        "closed_issues": milestone.get("closed_issues", 0),
        "created_at": milestone.get("created_at"),
        "updated_at": milestone.get("updated_at"),
        "closed_at": milestone.get("closed_at"),
        "url": milestone.get("html_url") or milestone.get("url"),
    }
    return normalized


def _has_next_page(headers: dict[str, str]) -> bool:
    return any('rel="next"' in part for part in headers.get("link", "").split(","))


def request_fingerprint(repo: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"repo": repo.casefold(), **payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _call(
    method: str,
    path: str,
    body: Any,
    *,
    operation: str,
    actor: Optional[str],
    expected_actor: Optional[str],
    gh_cmd: str,
    completed_steps: list[str],
    failed_step: str,
    is_write: bool,
    reconcile: Any = None,
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
        is_write=is_write,
        bucket="rest_core",
        completed_steps=completed_steps,
        failed_step=failed_step,
        reconcile=reconcile,
        retry_policy=retry_context.retry_policy if retry_context is not None else None,
        retry_runtime=retry_context.retry_runtime if retry_context is not None else None,
        deadline_at=retry_context.deadline_at if retry_context is not None else None,
    )
    if retry_summaries is not None and result.retry_summary is not None:
        retry_summaries.append(result.retry_summary)
        result.retry_summary = github_api_core.aggregate_retry_summaries(retry_summaries)
    if not result.ok:
        failure = result.failure or github_api_core.FailureDetail(
            cause="unknown_error",
            message=f"GitHub milestone request failed: {method} {path}",
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="unknown" if is_write else None,
        )
        failure.completed_steps = list(completed_steps)
        failure.failed_step = failed_step
        raise MilestoneError(
            failure.message,
            failure=failure,
            api_result=_public_api_result(result),
        )
    return result


def _authenticated_actor(
    *,
    gh_cmd: str,
    operation: str,
    expected_actor: Optional[str],
    completed_steps: list[str],
    retry_summaries: list[github_api_core.RetrySummary],
) -> str:
    result = _call(
        "GET",
        "/user",
        None,
        operation=operation,
        actor=None,
        expected_actor=None,
        gh_cmd=gh_cmd,
        completed_steps=completed_steps,
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
            actor=result.actor,
            expected_actor=expected_actor,
            failed_step="resolve_actor",
            completed_steps=completed_steps,
            retry_summaries=retry_summaries,
        )
    if expected_actor and login.casefold() != expected_actor.casefold():
        raise _local_error(
            f"Authenticated actor '{login}' does not match expected actor '{expected_actor}'",
            operation=operation,
            cause="actor_mismatch",
            actor=login,
            expected_actor=expected_actor,
            failed_step="resolve_actor",
            completed_steps=completed_steps,
            retry_summaries=retry_summaries,
        )
    completed_steps.append("resolve_actor")
    return login


def _list_raw(
    repo: str,
    state: str,
    *,
    operation: str,
    actor: Optional[str],
    expected_actor: Optional[str],
    gh_cmd: str,
    completed_steps: list[str],
    limit: Optional[int] = None,
    retry_context: Optional[github_api_core.ReconciliationContext] = None,
    retry_summaries: Optional[list[github_api_core.RetrySummary]] = None,
) -> list[dict[str, Any]]:
    if state not in {"open", "closed", "all"}:
        raise _local_error(
            f"invalid milestone state: {state}",
            operation=operation,
            cause="validation_error",
            actor=actor,
            expected_actor=expected_actor,
            failed_step="validate_state",
        )
    if limit is not None and (limit <= 0 or limit > MAX_ITEMS):
        raise _local_error(
            f"milestone limit must be between 1 and {MAX_ITEMS}",
            operation=operation,
            cause="validation_error",
            actor=actor,
            expected_actor=expected_actor,
            failed_step="validate_limit",
        )
    items: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        result = _call(
            "GET",
            f"/repos/{repo}/milestones?state={state}&per_page={PER_PAGE}&page={page}",
            None,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            gh_cmd=gh_cmd,
            completed_steps=completed_steps,
            failed_step=f"list_page_{page}",
            is_write=False,
            retry_context=retry_context,
            retry_summaries=retry_summaries,
        )
        if not isinstance(result.body, list):
            raise _local_error(
                "GitHub milestone list response was not a list",
                operation=operation,
                cause="invalid_response",
                actor=actor,
                expected_actor=expected_actor,
                failed_step=f"list_page_{page}",
                completed_steps=completed_steps,
            )
        for item in result.body:
            if not isinstance(item, dict):
                raise _local_error(
                    "GitHub milestone list contained a non-object item",
                    operation=operation,
                    cause="invalid_response",
                    actor=actor,
                    expected_actor=expected_actor,
                    failed_step=f"list_page_{page}",
                    completed_steps=completed_steps,
                )
            items.append(item)
            if limit is not None and len(items) >= limit:
                completed_steps.append(f"list_page_{page}")
                return items[:limit]
        completed_steps.append(f"list_page_{page}")
        if not _has_next_page(result.headers):
            return items
    raise _local_error(
        "GitHub milestone pagination exceeded the page limit",
        operation=operation,
        cause="pagination_limit",
        actor=actor,
        expected_actor=expected_actor,
        failed_step="list_pagination",
        completed_steps=completed_steps,
    )


def list_milestones(
    repo: str,
    *,
    state: str = "open",
    limit: Optional[int] = None,
    operation: str = "github.plan.milestone_list",
    actor: Optional[str] = EXPECTED_ACTOR,
    expected_actor: Optional[str] = EXPECTED_ACTOR,
    gh_cmd: str = DEFAULT_GH,
) -> dict[str, Any]:
    repo, actor, expected_actor, steps, retry_summaries = _command_state(
        repo,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        gh_cmd=gh_cmd,
        verify_actor=False,
    )
    raw = _list_raw(
        repo,
        state,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        gh_cmd=gh_cmd,
        completed_steps=steps,
        limit=limit,
        retry_summaries=retry_summaries,
    )
    return {
        "ok": True,
        "repo": repo,
        "state": state,
        "milestones": [normalize_milestone(item) for item in raw],
        "completed_steps": steps,
        **_success_metadata(actor, expected_actor, retry_summaries),
    }


def _resolve_number(
    repo: str,
    reference: str,
    *,
    operation: str,
    actor: Optional[str],
    expected_actor: Optional[str],
    gh_cmd: str,
    completed_steps: list[str],
    retry_context: Optional[github_api_core.ReconciliationContext] = None,
    retry_summaries: Optional[list[github_api_core.RetrySummary]] = None,
) -> int:
    raw = _list_raw(
        repo,
        "all",
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        gh_cmd=gh_cmd,
        completed_steps=completed_steps,
        retry_context=retry_context,
        retry_summaries=retry_summaries,
    )
    matches = matching_milestones(raw, reference)
    if len(matches) == 1 and isinstance(matches[0].get("number"), int):
        return int(matches[0]["number"])
    if len(matches) > 1:
        raise _local_error(
            f"milestone title is ambiguous: {reference}",
            operation=operation,
            cause="validation_error",
            actor=actor,
            expected_actor=expected_actor,
            failed_step="resolve_milestone",
            completed_steps=completed_steps,
        )
    if reference.isdigit() and int(reference) > 0:
        return int(reference)
    raise _local_error(
        f"milestone not found: {reference}",
        operation=operation,
        cause="not_found",
        actor=actor,
        expected_actor=expected_actor,
        failed_step="resolve_milestone",
        completed_steps=completed_steps,
    )


def matching_milestones(
    milestones: list[dict[str, Any]],
    reference: str,
) -> list[dict[str, Any]]:
    exact = [item for item in milestones if item.get("title") == reference]
    if exact:
        return exact
    return [
        item
        for item in milestones
        if isinstance(item.get("title"), str)
        and item["title"].casefold() == reference.casefold()
    ]


def _show_number(
    repo: str,
    number: int,
    *,
    operation: str,
    actor: Optional[str],
    expected_actor: Optional[str],
    gh_cmd: str,
    completed_steps: list[str],
    retry_context: Optional[github_api_core.ReconciliationContext] = None,
    retry_summaries: Optional[list[github_api_core.RetrySummary]] = None,
) -> dict[str, Any]:
    result = _call(
        "GET",
        f"/repos/{repo}/milestones/{number}",
        None,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        gh_cmd=gh_cmd,
        completed_steps=completed_steps,
        failed_step="show_milestone",
        is_write=False,
        retry_context=retry_context,
        retry_summaries=retry_summaries,
    )
    try:
        milestone = normalize_milestone(result.body)
        completed_steps.append("show_milestone")
        return milestone
    except ValueError as exc:
        raise _local_error(
            str(exc),
            operation=operation,
            cause="invalid_response",
            actor=actor,
            expected_actor=expected_actor,
            failed_step="show_milestone",
            completed_steps=completed_steps,
        ) from exc


def _resolve_and_show(
    repo: str,
    reference: str,
    *,
    operation: str,
    actor: Optional[str],
    expected_actor: Optional[str],
    gh_cmd: str,
    completed_steps: list[str],
    retry_summaries: list[github_api_core.RetrySummary],
) -> tuple[int, dict[str, Any]]:
    number = _resolve_number(
        repo,
        reference,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        gh_cmd=gh_cmd,
        completed_steps=completed_steps,
        retry_summaries=retry_summaries,
    )
    milestone = _show_number(
        repo,
        number,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        gh_cmd=gh_cmd,
        completed_steps=completed_steps,
        retry_summaries=retry_summaries,
    )
    return number, milestone


def show_milestone(
    repo: str,
    reference: str,
    *,
    operation: str = "github.plan.milestone_show",
    actor: Optional[str] = EXPECTED_ACTOR,
    expected_actor: Optional[str] = EXPECTED_ACTOR,
    gh_cmd: str = DEFAULT_GH,
) -> dict[str, Any]:
    repo, actor, expected_actor, steps, retry_summaries = _command_state(
        repo,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        gh_cmd=gh_cmd,
        verify_actor=False,
    )
    _, milestone = _resolve_and_show(
        repo,
        reference,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        gh_cmd=gh_cmd,
        completed_steps=steps,
        retry_summaries=retry_summaries,
    )
    return {
        "ok": True,
        "repo": repo,
        "milestone": milestone,
        "completed_steps": steps,
        **_success_metadata(actor, expected_actor, retry_summaries),
    }


def _same_requested_value(field: str, actual: Any, requested: Any) -> bool:
    if field != "due_on" or actual is None or requested is None:
        return actual == requested
    return normalize_due_on(actual)[:10] == normalize_due_on(requested)[:10]


def _requested_fields(
    *,
    title: Optional[str] = None,
    description: Any = _MISSING,
    due_on: Any = _MISSING,
    state: Any = _MISSING,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if title is not None:
        if not title.strip():
            raise ValueError("milestone title must not be empty")
        fields["title"] = title
    if description is not _MISSING:
        fields["description"] = description
    if due_on is not _MISSING:
        fields["due_on"] = normalize_due_on(due_on)
    if state is not _MISSING:
        if not isinstance(state, str) or state not in {"open", "closed"}:
            raise ValueError("invalid milestone state")
        fields["state"] = state
    return fields


def _matches_requested(milestone: dict[str, Any], requested: dict[str, Any]) -> bool:
    return all(
        _same_requested_value(key, milestone.get(key), value)
        for key, value in requested.items()
    )


def _verified_write_response(
    result: github_api_core.ApiResult,
    requested: dict[str, Any],
    *,
    operation: str,
    actor: Optional[str],
    expected_actor: Optional[str],
    completed_steps: list[str],
    failed_step: str,
    retry_summaries: list[github_api_core.RetrySummary],
) -> dict[str, Any]:
    try:
        milestone = normalize_milestone(result.body)
    except ValueError as exc:
        raise _local_error(
            str(exc),
            operation=operation,
            cause="invalid_response",
            actor=actor,
            expected_actor=expected_actor,
            failed_step=failed_step,
            completed_steps=completed_steps,
            write_outcome="unknown",
            retry_summaries=retry_summaries,
        ) from exc
    if not _matches_requested(milestone, requested):
        raise _local_error(
            "GitHub milestone write response did not match requested fields",
            operation=operation,
            cause="invalid_response",
            actor=actor,
            expected_actor=expected_actor,
            failed_step=failed_step,
            completed_steps=completed_steps,
            write_outcome="unknown",
            payload={"milestone": milestone, "requested": requested},
            retry_summaries=retry_summaries,
        )
    return milestone


def _reconcile_by_title(
    repo: str,
    title: str,
    existing_numbers: set[int],
    requested: dict[str, Any],
    fingerprint: str,
    *,
    operation: str,
    actor: Optional[str],
    expected_actor: Optional[str],
    gh_cmd: str,
    retry_summaries: list[github_api_core.RetrySummary],
) -> Any:
    def reconcile(_result: github_api_core.ApiResult, _context: github_api_core.ReconciliationContext) -> github_api_core.ReconciliationDecision:
        steps: list[str] = []
        try:
            candidates = [
                normalize_milestone(item)
                for item in _list_raw(
                    repo,
                    "all",
                    operation=operation,
                    actor=actor,
                    expected_actor=expected_actor,
                    gh_cmd=gh_cmd,
                    completed_steps=steps,
                    retry_context=_context,
                    retry_summaries=retry_summaries,
                )
                if item.get("title") == title and item.get("number") not in existing_numbers
            ]
        except (MilestoneError, ValueError) as exc:
            return github_api_core.ReconciliationDecision(
                "failed",
                details={"error": f"{type(exc).__name__}: reconciliation read failed"},
            )
        if len(candidates) == 1 and _matches_requested(candidates[0], requested):
            return github_api_core.ReconciliationDecision(
                "matched",
                body=candidates[0],
                details={
                    "matched_number": candidates[0].get("number"),
                    "request_fingerprint": fingerprint,
                },
            )
        if candidates:
            return github_api_core.ReconciliationDecision(
                "ambiguous",
                details={
                    "matching_numbers": [item.get("number") for item in candidates],
                    "reason": "exact-title candidate does not match requested fields",
                },
            )
        return github_api_core.ReconciliationDecision("no_match")

    return reconcile


def create_milestone(
    repo: str,
    title: str,
    *,
    description: Optional[str] = None,
    due_on: Any = None,
    state: str = "open",
    operation: str = "github.plan.milestone_create",
    actor: Optional[str] = EXPECTED_ACTOR,
    expected_actor: Optional[str] = EXPECTED_ACTOR,
    gh_cmd: str = DEFAULT_GH,
) -> dict[str, Any]:
    repo = _require_repo(repo, operation=operation, actor=actor, expected_actor=expected_actor)
    try:
        requested = _requested_fields(
            title=title,
            description=description if description is not None else "",
            due_on=due_on,
            state=state,
        )
    except ValueError as exc:
        raise _local_error(
            str(exc),
            operation=operation,
            cause="validation_error",
            actor=actor,
            expected_actor=expected_actor,
            failed_step="validate_request",
        ) from exc
    body = {
        "title": requested["title"],
        "description": requested["description"],
        "state": requested["state"],
    }
    if requested["due_on"] is not None:
        body["due_on"] = requested["due_on"]
    fingerprint = request_fingerprint(repo, body)
    repo, actor, expected_actor, steps, retry_summaries = _command_state(
        repo,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        gh_cmd=gh_cmd,
        verify_actor=True,
    )
    all_milestones = _list_raw(
        repo,
        "all",
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        gh_cmd=gh_cmd,
        completed_steps=steps,
        retry_summaries=retry_summaries,
    )
    matching = matching_milestones(all_milestones, title)
    exact = [item for item in matching if item.get("title") == title]
    if matching and not exact:
        raise _local_error(
            f"milestone title conflicts case-insensitively with existing milestone: {title}",
            operation=operation,
            cause="conflict",
            actor=actor,
            expected_actor=expected_actor,
            failed_step="dedupe_milestone",
            completed_steps=steps,
            payload={
                "existing": [normalize_milestone(item) for item in matching],
                "requested": requested,
                "request_fingerprint": fingerprint,
            },
            retry_summaries=retry_summaries,
        )
    existing = [normalize_milestone(item) for item in exact]
    if len(existing) > 1:
        raise _local_error(
            f"exact milestone title has conflicting duplicates: {title}",
            operation=operation,
            cause="conflict",
            actor=actor,
            expected_actor=expected_actor,
            failed_step="dedupe_milestone",
            completed_steps=steps,
            payload={
                "existing": existing,
                "requested": requested,
                "request_fingerprint": fingerprint,
            },
        )
    if existing:
        current = existing[0]
        if _matches_requested(current, requested):
            return {
                "ok": True,
                "repo": repo,
                "milestone": current,
                "created": False,
                "no_op": True,
                "completed_steps": steps,
                "request_fingerprint": fingerprint,
                **_success_metadata(actor, expected_actor, retry_summaries),
            }
        raise _local_error(
            f"milestone already exists with conflicting fields: {title}",
            operation=operation,
            cause="conflict",
            actor=actor,
            expected_actor=expected_actor,
            failed_step="dedupe_milestone",
            completed_steps=steps,
            payload={
                "existing": current,
                "requested": requested,
                "request_fingerprint": fingerprint,
            },
        )

    existing_numbers = {
        int(item["number"])
        for item in all_milestones
        if isinstance(item.get("number"), int)
    }
    result = _call(
        "POST",
        f"/repos/{repo}/milestones",
        body,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        completed_steps=steps,
        failed_step="create_milestone",
        is_write=True,
        reconcile=_reconcile_by_title(
            repo,
            title,
            existing_numbers,
            requested,
            fingerprint,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            gh_cmd=gh_cmd,
            retry_summaries=retry_summaries,
        ),
        gh_cmd=gh_cmd,
        retry_summaries=retry_summaries,
    )
    milestone = _verified_write_response(
        result,
        requested,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        completed_steps=steps,
        failed_step="parse_create_response",
        retry_summaries=retry_summaries,
    )
    return {
        "ok": True,
        "repo": repo,
        "milestone": milestone,
        "created": True,
        "no_op": False,
        "completed_steps": steps,
        "request_fingerprint": fingerprint,
        **_success_metadata(actor, expected_actor, retry_summaries),
    }


def _reconcile_update(
    repo: str,
    number: int,
    requested: dict[str, Any],
    *,
    operation: str,
    actor: Optional[str],
    expected_actor: Optional[str],
    gh_cmd: str,
    retry_summaries: list[github_api_core.RetrySummary],
) -> Any:
    def reconcile(_result: github_api_core.ApiResult, _context: github_api_core.ReconciliationContext) -> github_api_core.ReconciliationDecision:
        try:
            steps: list[str] = []
            milestone = _show_number(
                repo,
                number,
                operation=operation,
                actor=actor,
                expected_actor=expected_actor,
                gh_cmd=gh_cmd,
                completed_steps=steps,
                retry_context=_context,
                retry_summaries=retry_summaries,
            )
        except MilestoneError:
            return github_api_core.ReconciliationDecision("failed", details={"error": "reconciliation read failed"})
        if _matches_requested(milestone, requested):
            return github_api_core.ReconciliationDecision("matched", body=milestone)
        return github_api_core.ReconciliationDecision("no_match")

    return reconcile


def update_milestone(
    repo: str,
    reference: str,
    *,
    title: Optional[str] = None,
    description: Any = _MISSING,
    due_on: Any = _MISSING,
    clear_due_on: bool = False,
    state: Any = _MISSING,
    operation: str = "github.plan.milestone_update",
    actor: Optional[str] = EXPECTED_ACTOR,
    expected_actor: Optional[str] = EXPECTED_ACTOR,
    gh_cmd: str = DEFAULT_GH,
) -> dict[str, Any]:
    repo = _require_repo(repo, operation=operation, actor=actor, expected_actor=expected_actor)
    if clear_due_on and due_on is not _MISSING:
        raise _local_error(
            "--due-on and --clear-due-on are mutually exclusive",
            operation=operation,
            cause="validation_error",
            actor=actor,
            expected_actor=expected_actor,
            failed_step="validate_request",
        )
    if state == "closed":
        raise _local_error(
            "milestone-update cannot close a milestone; use milestone-close",
            operation=operation,
            cause="validation_error",
            actor=actor,
            expected_actor=expected_actor,
            failed_step="validate_state",
        )
    if clear_due_on:
        due_on = None
    try:
        requested = _requested_fields(
            title=title,
            description=description,
            due_on=due_on,
            state=state,
        )
    except ValueError as exc:
        raise _local_error(
            str(exc),
            operation=operation,
            cause="validation_error",
            actor=actor,
            expected_actor=expected_actor,
            failed_step="validate_request",
        ) from exc
    if not requested:
        raise _local_error(
            "milestone-update requires at least one field",
            operation=operation,
            cause="validation_error",
            actor=actor,
            expected_actor=expected_actor,
            failed_step="validate_request",
        )
    repo, actor, expected_actor, steps, retry_summaries = _command_state(
        repo,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        gh_cmd=gh_cmd,
        verify_actor=True,
    )
    number, current = _resolve_and_show(
        repo,
        reference,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        gh_cmd=gh_cmd,
        completed_steps=steps,
        retry_summaries=retry_summaries,
    )
    if _matches_requested(current, requested):
        return {
            "ok": True,
            "repo": repo,
            "milestone": current,
            "updated": False,
            "no_op": True,
            "completed_steps": steps,
            **_success_metadata(actor, expected_actor, retry_summaries),
        }
    result = _call(
        "PATCH",
        f"/repos/{repo}/milestones/{number}",
        requested,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        completed_steps=steps,
        failed_step="update_milestone",
        is_write=True,
        reconcile=_reconcile_update(
            repo,
            number,
            requested,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            gh_cmd=gh_cmd,
            retry_summaries=retry_summaries,
        ),
        gh_cmd=gh_cmd,
        retry_summaries=retry_summaries,
    )
    milestone = _verified_write_response(
        result,
        requested,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        completed_steps=steps,
        failed_step="parse_update_response",
        retry_summaries=retry_summaries,
    )
    return {
        "ok": True,
        "repo": repo,
        "milestone": milestone,
        "updated": True,
        "no_op": False,
        "completed_steps": steps,
        **_success_metadata(actor, expected_actor, retry_summaries),
    }


def _open_assigned_items(
    repo: str,
    number: int,
    *,
    operation: str,
    actor: Optional[str],
    expected_actor: Optional[str],
    gh_cmd: str,
    completed_steps: list[str],
    retry_context: Optional[github_api_core.ReconciliationContext] = None,
    retry_summaries: Optional[list[github_api_core.RetrySummary]] = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        result = _call(
            "GET",
            f"/repos/{repo}/issues?milestone={number}&state=open&per_page={PER_PAGE}&page={page}",
            None,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            gh_cmd=gh_cmd,
            completed_steps=completed_steps,
            failed_step=f"check_open_items_page_{page}",
            is_write=False,
            retry_context=retry_context,
            retry_summaries=retry_summaries,
        )
        if not isinstance(result.body, list):
            raise _local_error(
                "GitHub milestone open-item response was not a list",
                operation=operation,
                cause="invalid_response",
                actor=actor,
                expected_actor=expected_actor,
                failed_step=f"check_open_items_page_{page}",
                completed_steps=completed_steps,
            )
        for item in result.body:
            if not isinstance(item, dict):
                raise _local_error(
                    "GitHub milestone open-item response contained a non-object item",
                    operation=operation,
                    cause="invalid_response",
                    actor=actor,
                    expected_actor=expected_actor,
                    failed_step=f"check_open_items_page_{page}",
                    completed_steps=completed_steps,
                )
            items.append(item)
        completed_steps.append(f"check_open_items_page_{page}")
        if not _has_next_page(result.headers):
            return items
    raise _local_error(
        "GitHub milestone open-item pagination exceeded the page limit",
        operation=operation,
        cause="pagination_limit",
        actor=actor,
        expected_actor=expected_actor,
        failed_step="check_open_items_pagination",
        completed_steps=completed_steps,
    )


def _reconcile_close(
    repo: str,
    number: int,
    *,
    operation: str,
    actor: Optional[str],
    expected_actor: Optional[str],
    gh_cmd: str,
    retry_summaries: list[github_api_core.RetrySummary],
) -> Any:
    def reconcile(_result: github_api_core.ApiResult, _context: github_api_core.ReconciliationContext) -> github_api_core.ReconciliationDecision:
        try:
            steps: list[str] = []
            milestone = _show_number(
                repo,
                number,
                operation=operation,
                actor=actor,
                expected_actor=expected_actor,
                gh_cmd=gh_cmd,
                completed_steps=steps,
                retry_context=_context,
                retry_summaries=retry_summaries,
            )
        except MilestoneError:
            return github_api_core.ReconciliationDecision("failed", details={"error": "reconciliation read failed"})
        if milestone.get("state") == "closed":
            return github_api_core.ReconciliationDecision("matched", body=milestone)
        return github_api_core.ReconciliationDecision("no_match")

    return reconcile


def close_milestone(
    repo: str,
    reference: str,
    *,
    operation: str = "github.plan.milestone_close",
    actor: Optional[str] = EXPECTED_ACTOR,
    expected_actor: Optional[str] = EXPECTED_ACTOR,
    gh_cmd: str = DEFAULT_GH,
) -> dict[str, Any]:
    repo, actor, expected_actor, steps, retry_summaries = _command_state(
        repo,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        gh_cmd=gh_cmd,
        verify_actor=True,
    )
    number, current = _resolve_and_show(
        repo,
        reference,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        gh_cmd=gh_cmd,
        completed_steps=steps,
        retry_summaries=retry_summaries,
    )
    if current.get("state") == "closed":
        return {
            "ok": True,
            "repo": repo,
            "milestone": current,
            "closed": False,
            "no_op": True,
            "blocking_items": [],
            "completed_steps": steps,
            **_success_metadata(actor, expected_actor, retry_summaries),
        }
    blockers = _open_assigned_items(
        repo,
        number,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        gh_cmd=gh_cmd,
        completed_steps=steps,
        retry_summaries=retry_summaries,
    )
    normalized_blockers = [
        {
            "number": item.get("number"),
            "title": item.get("title"),
            "state": item.get("state"),
            "type": "pull_request" if item.get("pull_request") is not None else "issue",
            "url": item.get("html_url") or item.get("url"),
        }
        for item in blockers
    ]
    if normalized_blockers:
        raise _local_error(
            "milestone-close refused because open issues or pull requests remain assigned",
            operation=operation,
            cause="conflict",
            actor=actor,
            expected_actor=expected_actor,
            failed_step="guard_open_items",
            completed_steps=steps,
            payload={"blocking_items": normalized_blockers, "milestone": current},
        )
    result = _call(
        "PATCH",
        f"/repos/{repo}/milestones/{number}",
        {"state": "closed"},
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        completed_steps=steps,
        failed_step="close_milestone",
        is_write=True,
        reconcile=_reconcile_close(
            repo,
            number,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            gh_cmd=gh_cmd,
            retry_summaries=retry_summaries,
        ),
        gh_cmd=gh_cmd,
        retry_summaries=retry_summaries,
    )
    milestone = _verified_write_response(
        result,
        {"state": "closed"},
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        completed_steps=steps,
        failed_step="parse_close_response",
        retry_summaries=retry_summaries,
    )
    return {
        "ok": True,
        "repo": repo,
        "milestone": milestone,
        "closed": True,
        "no_op": False,
        "blocking_items": [],
        "completed_steps": steps,
        **_success_metadata(actor, expected_actor, retry_summaries),
    }
