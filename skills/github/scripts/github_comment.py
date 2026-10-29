#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Shared REST implementation for GitHub issue and pull-request comments."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import html
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.parse
from collections import Counter
from typing import Any, Callable, Optional

import github_api as github_api_core
import github_identity


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parents[2]))
from skills.direction.scripts import question_id

DEFAULT_GH = os.environ.get("GH_COMMENT_GH") or str(SCRIPT_DIR / "gh-with-env-token")
EXPECTED_ACTOR = github_identity.automation_login()
PER_PAGE = 100
MAX_PAGES = 1000
RECONCILIATION_CLOCK_SKEW_SECONDS = 5
OPERATION_MARKER_LINE_RE = re.compile(
    rf"(?m)^[ \t]*{re.escape(github_api_core.OPERATION_MARKER_PREFIX)}[0-9a-f]+ -->[ \t]*(?:\r?\n)?"
)
MENTION_RE = re.compile(r"(?<!\w)@(?P<login>[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}(?:/[A-Za-z0-9_-]+)?)")
QUIET_MENTION_OPEN, QUIET_MENTION_CLOSE = "<code>", "</code>"
ATTACHMENT_OWNER = "cbusillo"
AGENT_POSTED_LINE = "Posted by an agent."
ATTACHMENT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp4", ".mov", ".webm"}


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
        self.api_result: Optional[dict[str, Any]] = github_api_core.redact_body(api_result) if api_result is not None else None
        self.payload: dict[str, Any] = github_api_core.redact_body(payload or {})


def _is_quiet_mention(body: str, match: re.Match[str]) -> bool:
    opening = match.start() - len(QUIET_MENTION_OPEN)
    return (body[opening:match.start()] == QUIET_MENTION_OPEN
            and body.startswith(QUIET_MENTION_CLOSE, match.end())
            and (opening == 0 or body[opening - 1] != "\\"))


def live_mentions(body: str, *, notify: frozenset[str] = frozenset()) -> list[str]:
    notified = {login.casefold() for login in notify}
    written = list(MENTION_RE.finditer(body))
    rendered = Counter(match["login"] for match in MENTION_RE.finditer(html.unescape(body)))
    entity_made = rendered - Counter(match["login"] for match in written)
    live = [match["login"] for match in written if not _is_quiet_mention(body, match)] + list(entity_made)
    return [f"@{login}" for login in live if login.casefold() not in notified]


def validate_question_mentions(body: str, repo: str) -> None:
    if question_id.QUESTION_HEADING.match(body) and (
        mentions := live_mentions(body, notify=frozenset({repo.split("/")[0]}))
    ):
        raise ValueError(
            f"A question must not notify the people it names ({', '.join(mentions)}); "
            "write each as <code>@login</code> or drop the @, then post that text"
        )


def validate_planning_references(body: object, repo: str) -> None:
    if isinstance(body, dict):
        body = list(body.values())
    if isinstance(body, list):
        for value in body:
            validate_planning_references(value, repo)
        return
    if not isinstance(body, str):
        return
    ambiguous = list(dict.fromkeys(re.findall(r"(?<![\w/#.-])[\w.-]+#\d+(?!\d)|(?<![\w/#])#\d+(?!\d)|(?<![\w/#.-])PR[1-9]\d*(?![\w-])", body, re.IGNORECASE)))
    if ambiguous:
        raise ValueError(
            f"Ambiguous issue references: {', '.join(ambiguous)}. "
            f"Write OWNER/REPO#N or a full GitHub issue/PR URL; local references use {repo}#N. "
            "An existing local issue with the same number does not prove the intended repository."
        )


def effective_expected_actor(expected_actor: Optional[str]) -> Optional[str]:
    if github_identity.active_auth_fallback_allowed():
        return None
    return expected_actor


def response_expected_actor(
    result: github_api_core.ApiResult, expected_actor: Optional[str], repository: Optional[str] = None
) -> Optional[str]:
    primary_actor = github_identity.automation_login()
    if (repository and github_identity.github_app_prefix(repository) == "GITHUB_CLIENT_APP"
            and result.expected_actor and expected_actor and primary_actor
            and expected_actor.casefold() == primary_actor.casefold()):
        return result.expected_actor
    # The wrapper may authorize an own-user route only on the repository write.
    # Preserve that resolved login instead of rechecking the original bot login.
    if expected_actor and result.actor and result.expected_actor is None:
        return result.actor
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
    gh_prefix_args: Optional[list[str]] = None,
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
    retry_policy: Optional[github_api_core.RetryPolicy] = None,
    retry_summaries: Optional[list[github_api_core.RetrySummary]] = None,
) -> github_api_core.ApiResult:
    result = github_api_core.call_gh_with_retry(
        method,
        path,
        payload,
        gh_cmd=gh_cmd,
        gh_prefix_args=gh_prefix_args,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        bucket="rest_core",
        completed_steps=completed_steps,
        failed_step=failed_step,
        is_write=is_write,
        reconcile=reconcile,
        retry_policy=retry_context.retry_policy if retry_context is not None else retry_policy,
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
    write_repository: Optional[str] = None,
    retry_summaries: Optional[list[github_api_core.RetrySummary]] = None,
) -> str:
    result = _call_api(
        "GET",
        "/user",
        None,
        gh_cmd=gh_cmd,
        gh_prefix_args=["--write-actor-for", write_repository] if write_repository else None,
        operation=operation,
        actor=None,
        expected_actor=None,
        completed_steps=[],
        failed_step="resolve_actor",
        is_write=False,
        retry_summaries=retry_summaries,
    )
    actor_repository = write_repository or github_identity.repository_context()
    if actor_repository:
        expected_actor = response_expected_actor(result, expected_actor, actor_repository)
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
    since: Optional[str] = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    comments: list[dict[str, Any]] = []
    steps = list(completed_steps)
    for page in range(1, MAX_PAGES + 1):
        step = f"list_comments_page_{page}"
        result = _call_api(
            "GET",
            f"/repos/{repo}/issues/{number}/comments?per_page={PER_PAGE}&page={page}"
            + ("&" + urllib.parse.urlencode({"since": since}) if since else ""),
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


def _canonical_comment_body(body: Any) -> str:
    if not isinstance(body, str):
        return ""
    return OPERATION_MARKER_LINE_RE.sub("", body).rstrip()


canonical_comment_body = _canonical_comment_body


def _matching_actor_comment(
    comments: list[dict[str, Any]],
    actor: str,
    body: str,
) -> Optional[dict[str, Any]]:
    matches = []
    for candidate in comments:
        if str((candidate.get("user") or {}).get("login") or "").casefold() != actor.casefold():
            continue
        prior_question = question_id.heading_id(candidate.get("body") or "")
        expected = question_id.number_heading(body, prior_question) if prior_question else body
        if _canonical_comment_body(candidate.get("body")) == _canonical_comment_body(expected):
            matches.append(candidate)
    if not matches:
        return None
    return max(
        matches,
        key=lambda comment: (
            str(comment.get("created_at") or ""),
            int(comment.get("id") or 0),
        ),
    )


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _format_timestamp(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        return None


def comment_fingerprint(repo: str, number: int, actor: str, body: str) -> str:
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
    threshold = parse_timestamp(started_at)
    if threshold is None:
        return github_api_core.ReconciliationDecision(
            "failed",
            details={
                "request_fingerprint": fingerprint,
                "actor": actor,
                "failure": {"cause": "invalid_reconciliation_timestamp"},
            },
        )
    threshold -= dt.timedelta(seconds=RECONCILIATION_CLOCK_SKEW_SECONDS)
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
            # GitHub's since filter is exclusive; keep the inclusive creation
            # threshold below in range even at whole-second precision.
            since=_format_timestamp(threshold - dt.timedelta(seconds=1)),
        )
    except CommentError as exc:
        return github_api_core.ReconciliationDecision(
            "failed",
            details={
                "request_fingerprint": fingerprint,
                "actor": actor,
                "failure": exc.api_result or {"cause": exc.failure.cause},
            },
        )
    matches: list[dict[str, Any]] = []
    for item in comments:
        item_actor = (item.get("user") or {}).get("login") if isinstance(item.get("user"), dict) else None
        created_at = parse_timestamp(item.get("created_at"))
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
        "actor": actor,
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
        "actor": author if comment_action != "updated" and isinstance(author, str) and author else actor,
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
    edit_comment: Optional[int] = None,
    expected_body: Optional[str] = None,
    expected_updated_at: Optional[str] = None,
    create_if_none: bool = False,
    gh_cmd: str = DEFAULT_GH,
    expected_actor: Optional[str] = EXPECTED_ACTOR,
    operation: Optional[str] = None,
    completed_steps: Optional[list[str]] = None,
    failed_step: Optional[str] = None,
    dedupe_body: bool = False,
    before_create: Callable[[dict[str, Any]], None] | None = None,
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
    if dedupe_body and edit_last:
        raise _local_error(
            "Body deduplication cannot be combined with edit-last",
            operation=operation,
            cause="validation_error",
            expected_actor=expected_actor,
            failed_step="input_validation",
        )

    if (
        (edit_comment is not None and (isinstance(edit_comment, bool) or not isinstance(edit_comment, int) or edit_comment <= 0))
        or (edit_comment is not None and (edit_last or create_if_none or dedupe_body))
        or (edit_comment is None and (expected_body is not None or expected_updated_at is not None))
    ):
        raise _local_error(
            "--edit-comment requires a positive ID and cannot be combined with edit-last, create-if-none or deduplication; expected values require --edit-comment",
            operation=operation,
            cause="validation_error",
            expected_actor=expected_actor,
            failed_step="input_validation",
        )

    resolved_repo = resolve_repo(repo, gh_cmd=gh_cmd, operation=operation)
    if (question_id.QUESTION_HEADING.match(body) and not question_id.heading_id(body)
            and github_identity.acts_as_own_user(resolved_repo)):
        raise _local_error(
            "Personal-identity question writes need the existing Q ID in the heading before exact-text approval; "
            "allocate with question_id.py next before requesting approval, then post that numbered text",
            operation=operation, cause="validation_error", expected_actor=expected_actor,
            failed_step="input_validation",
        )
    collected_retry_summaries = retry_summaries if retry_summaries is not None else []
    actor = authenticated_actor(
        gh_cmd=gh_cmd,
        operation=operation,
        expected_actor=expected_actor,
        write_repository=resolved_repo,
        retry_summaries=collected_retry_summaries,
    )
    if expected_actor:
        expected_actor = actor
    steps = list(completed_steps or [])
    steps.append("resolve_actor")
    existing_comments: Optional[list[dict[str, Any]]] = None

    if edit_comment is not None:
        path = f"/repos/{resolved_repo}/issues/comments/{edit_comment}"
        target = {"kind": kind, "repo": resolved_repo, "number": number, "selected_comment_id": edit_comment}
        result = _call_api(
            "GET", path, None,
            gh_cmd=gh_cmd, operation=operation, actor=actor, expected_actor=expected_actor,
            completed_steps=steps, failed_step="read_exact_comment", is_write=False,
            failure_payload=target, retry_summaries=collected_retry_summaries,
        )
        selected = result.body
        steps.append("read_exact_comment")
        api_root = (
            "https://api.github.com"
            if github_api_core.DEFAULT_HOST.casefold() == "github.com"
            else f"https://{github_api_core.DEFAULT_HOST}/api/v3"
        )
        issue_url = f"{api_root}/repos/{resolved_repo}/issues/{number}"
        if (
            not isinstance(selected, dict)
            or selected.get("id") != edit_comment
            or str(selected.get("issue_url") or "").casefold() != issue_url.casefold()
        ):
            raise _local_error(
                "Exact comment does not belong to the requested repository and thread",
                operation=operation, cause="comment_target_mismatch", actor=actor,
                expected_actor=expected_actor, completed_steps=steps,
                failed_step="validate_exact_comment", payload=target,
            )
        author = (selected.get("user") or {}).get("login") if isinstance(selected.get("user"), dict) else None
        if not isinstance(author, str) or author.casefold() != actor.casefold():
            raise _local_error(
                "Exact comment is not authored by the authenticated actor",
                operation=operation, cause="actor_mismatch", actor=actor,
                expected_actor=expected_actor, completed_steps=steps,
                failed_step="validate_exact_comment", payload=target,
            )
        if (
            (expected_body is not None and selected.get("body") != expected_body)
            or (expected_updated_at is not None and selected.get("updated_at") != expected_updated_at)
        ):
            raise _local_error(
                "Exact comment changed: expected body or updated_at does not match; read the target before editing",
                operation=operation, cause="comment_conflict", actor=actor,
                expected_actor=expected_actor, completed_steps=steps,
                failed_step="validate_exact_comment", payload=target,
            )
        steps.append("validate_exact_comment")
        prior_question = question_id.heading_id(selected.get("body") or "")
        if prior_question:
            incoming_question = question_id.heading_id(body)
            if incoming_question and incoming_question != prior_question:
                raise _local_error(
                    "Question ID differs from the saved question; keep its original ID when editing",
                    operation=operation, cause="validation_error", actor=actor,
                    expected_actor=expected_actor, completed_steps=steps,
                    failed_step="validate_question_identity", payload=target,
                )
            body = question_id.number_heading(body, prior_question)
        verify_question_number(body, selected.get("html_url"))
        update_step = failed_step or "update_comment"
        result = _call_api(
            "PATCH", path, {"body": body},
            gh_cmd=gh_cmd, operation=operation, actor=actor, expected_actor=expected_actor,
            completed_steps=steps, failed_step=update_step, is_write=True,
            failure_payload={**target, "reconciliation": {"strategy": "selected_comment_lookup", "creation_skipped": True}},
            # A repeated PATCH could overwrite a change made after the preflight read.
            retry_policy=dataclasses.replace(github_api_core.default_retry_policy(), max_attempts=1),
            retry_summaries=collected_retry_summaries,
        )
        steps.append(update_step)
        payload = _comment_payload(
            result.body, operation=operation, kind=kind, repo=resolved_repo,
            number=number, actor=result.actor or actor, expected_actor=expected_actor,
            comment_action="updated", completed_steps=steps, retry_summary=result.retry_summary,
        )
        payload["selected_comment_id"] = edit_comment
        return payload

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
            prior_question = question_id.heading_id(selected.get("body") or "")
            if prior_question:
                incoming_question = question_id.heading_id(body)
                if incoming_question and incoming_question != prior_question:
                    raise _local_error(
                        "Question ID differs from the saved question; keep its original ID when editing",
                        operation=operation, cause="validation_error", actor=actor,
                        expected_actor=expected_actor, completed_steps=steps,
                        failed_step="validate_question_identity",
                    )
                body = question_id.number_heading(body, prior_question)
            verify_question_number(body, selected.get("html_url"))
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
                actor=result.actor or actor,
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

    started = _utc_now()
    started_at = _format_timestamp(started)
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
            # Reconciliation excludes older creations. Ordinary appends need
            # only this same window for pre-existing IDs; deduplication still
            # searches the complete thread. GitHub filters by updated_at, so
            # old comments edited recently remain included conservatively.
            since=None if dedupe_body else _format_timestamp(
                started - dt.timedelta(seconds=RECONCILIATION_CLOCK_SKEW_SECONDS + 1)
            ),
        )
    if dedupe_body:
        selected = _matching_actor_comment(existing_comments, actor, body)
        if selected is not None:
            verify_question_number(selected.get("body") or "", selected.get("html_url"))
            steps.append("reuse_existing_comment")
            payload = _comment_payload(
                selected,
                operation=operation,
                kind=kind,
                repo=resolved_repo,
                number=number,
                actor=actor,
                expected_actor=expected_actor,
                comment_action="existing",
                completed_steps=steps,
                retry_summary=github_api_core.aggregate_retry_summaries(collected_retry_summaries),
            )
            payload["deduplicated"] = True
            return payload
    existing_comment_ids = {
        comment_id
        for item in existing_comments
        if isinstance((comment_id := item.get("id")), int)
    }
    create_step = failed_step or "create_comment"
    verify_question_number(body)
    fingerprint = comment_fingerprint(resolved_repo, number, actor, body)
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
    if before_create is not None:
        try:
            before_create({"repo": resolved_repo, "number": number, "actor": actor, "operation_marker": dict(marker)})
        except (ValueError, OSError) as error:
            raise _local_error(str(error), operation=operation, cause="comment_preparation_failed", actor=actor,
                               expected_actor=expected_actor, failed_step="prepare_comment_create") from error
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
            expected_actor=response_expected_actor(failed_result, expected_actor),
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
        actor=result.actor or actor,
        expected_actor=response_expected_actor(result, expected_actor),
        comment_action="created",
        completed_steps=steps,
        operation_marker=marker,
        retry_summary=result.retry_summary,
    )


def validate_attachment_request(repo: str, body: str, attachments: list[str]) -> None:
    if (not github_identity.REPOSITORY_PATTERN.fullmatch(repo)
            or repo.split("/")[0].casefold() != ATTACHMENT_OWNER):
        raise ValueError("Own-user attachments are limited to Chris-owned repositories")
    if body.splitlines()[:1] != [AGENT_POSTED_LINE]:
        raise ValueError(f"Attachment comments must start with: {AGENT_POSTED_LINE}")
    requested_body = body[len(AGENT_POSTED_LINE):].lstrip()
    validate_question_mentions(requested_body, repo)
    if question_id.QUESTION_HEADING.match(requested_body) and not question_id.heading_id(requested_body):
        raise ValueError("Personal-identity questions need an existing Q ID; allocate with question_id.py next first")
    verify_question_number(requested_body)
    if not attachments:
        raise ValueError("Own-user attachment comments require an image or video")
    for attachment in attachments:
        path = pathlib.Path(attachment.split("#", 1)[0])
        if path.suffix.casefold() not in ATTACHMENT_EXTENSIONS:
            raise ValueError("Only images and videos can be attached; PDFs stay manual")
        if not path.is_file() or not path.stat().st_size:
            raise ValueError("Attachments must be nonempty regular files")
        with path.open("rb") as stream:
            if b"%PDF-" in stream.read(1024):
                raise ValueError("PDFs stay manual, including PDFs renamed as images")


def validate_own_attachment_command(arguments: list[str], body: str) -> str:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("kind", choices=("issue", "pr"))
    parser.add_argument("action", choices=("comment",))
    parser.add_argument("number", type=int)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--body-file", required=True, choices=("-",))
    parser.add_argument("--attach", action="append", required=True)
    args = parser.parse_args(arguments)
    if args.number <= 0:
        raise ValueError("Attachment comments require a positive issue or PR number")
    validate_attachment_request(args.repo, body, args.attach)
    if str(github_identity.configured_value("GH_WITH_ENV_TOKEN_OWN_USER_LOGIN") or "").casefold() != ATTACHMENT_OWNER:
        raise ValueError("Own-user attachments require Chris's declared personal login")
    result = _call_api(
        "GET", f"/repos/{args.repo}", None, gh_cmd=DEFAULT_GH,
        operation="github.comment.attachment_preflight", actor=None, expected_actor=EXPECTED_ACTOR,
        completed_steps=[], failed_step="verify_attachment_owner", is_write=False,
    )
    if (not isinstance(result.body, dict)
            or str((result.body.get("owner") or {}).get("login", "")).casefold() != ATTACHMENT_OWNER
            or str(result.body.get("full_name", "")).casefold() != args.repo.casefold()):
        raise ValueError("Attachment target must still be owned by Chris at its current repository name")
    return args.repo


def bind_attachment_source(body: str, url: str) -> None:
    question = question_id.heading_id(body.removeprefix(AGENT_POSTED_LINE).lstrip())
    if question is None:
        return
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/[1-9][0-9]*#issuecomment-[1-9][0-9]*", url):
        raise ValueError("Attachment creation returned no exact source URL; reconcile the saved comment")
    question_id.update(question_id.counter_path(), question_url=url, question_number=int(question[1:]))


def attachment_write_outcome(stderr: str, nonce: str) -> str:
    for line in stderr.splitlines():
        try:
            receipt = json.loads(line)
        except json.JSONDecodeError:
            continue
        for outcome in ("not_started", "applied"):
            if receipt == {"schema_version": 1, "nonce": nonce, "write_outcome": outcome}:
                return outcome
    return "unknown"


def attached_comment(
    kind: str, number: int, body: str, attachments: list[str], *, repo: str | None,
    gh_cmd: str, operation: str, retry_summaries: list[github_api_core.RetrySummary],
) -> dict[str, Any]:
    resolved_repo = resolve_repo(repo, gh_cmd=gh_cmd, operation=operation)
    if body.splitlines()[:1] != [AGENT_POSTED_LINE]:
        body = f"{AGENT_POSTED_LINE}\n\n{body}"
    try:
        validate_attachment_request(resolved_repo, body, attachments)
    except (ValueError, OSError) as error:
        raise _local_error(
            str(error), operation=operation, cause="validation_error", failed_step="attachment_preflight",
        ) from error
    if not github_identity.own_user_opted_in():
        raise _local_error(
            "Attachments require GH_WITH_ENV_TOKEN_OWN_USER=1 on this command",
            operation=operation, cause="validation_error", failed_step="attachment_preflight",
        )
    try:
        help_result = subprocess.run(
            [gh_cmd, "help", kind, "comment"], capture_output=True, text=True,
        )
    except OSError as error:
        raise _local_error(
            str(error), operation=operation, cause="attachment_cli_unavailable", failed_step="attachment_preflight",
        ) from error
    if help_result.returncode:
        raise _local_error(
            f"Attachment CLI preflight failed: {github_api_core.redact_string(help_result.stderr.strip())}",
            operation=operation, cause="attachment_cli_unavailable", failed_step="attachment_preflight",
        )
    if "--attach" not in help_result.stdout:
        raise _local_error(
            "Attachments require GitHub CLI with the official --attach flag (2.103.2 or newer)",
            operation=operation, cause="attachment_cli_unavailable", failed_step="attachment_preflight",
        )
    actor = ATTACHMENT_OWNER
    command = [gh_cmd, kind, "comment", str(number), "--repo", resolved_repo, "--body-file", "-"]
    for attachment in attachments:
        command.extend(["--attach", attachment])
    receipt_nonce = os.urandom(16).hex()
    try:
        result = subprocess.run(
            command, input=body, capture_output=True, text=True,
            env={**os.environ, "GH_WITH_ENV_TOKEN_RECEIPT_NONCE": receipt_nonce},
        )
    except OSError as error:
        raise _local_error(
            str(error), operation=operation, cause="attachment_comment_failed", failed_step="attach_comment",
            actor=actor, expected_actor=actor, write_outcome="unknown",
        ) from error
    url = result.stdout.strip()
    match = re.fullmatch(
        rf"https://github\.com/{re.escape(resolved_repo)}/(?:issues|pull)/{number}#issuecomment-(\d+)", url, re.IGNORECASE,
    )
    if result.returncode and match and attachment_write_outcome(result.stderr, receipt_nonce) == "applied":
        raise _local_error(
            "Attachment saved, but source-number binding failed; read the saved question before repairing it",
            operation=operation, cause="question_numbering_failed", actor=actor, expected_actor=actor,
            completed_steps=["attach_comment"], failed_step="bind_question_source", write_outcome="applied",
            payload={"url": url},
        )
    if result.returncode or not match:
        raise _local_error(
            "GitHub CLI attachment comment failed using the opted-in personal account; no replay or identity fallback. "
            "Only images and videos are supported; PDFs still go by hand. "
            f"Read the thread before retrying. {github_api_core.redact_string(result.stderr.strip())}",
            operation=operation, cause="attachment_comment_failed", actor=actor, expected_actor=actor,
            failed_step="attach_comment", write_outcome=attachment_write_outcome(result.stderr, receipt_nonce),
            payload={"url": url if match else None},
        )
    try:
        readback = _call_api(
            "GET", f"/repos/{resolved_repo}/issues/comments/{match[1]}", None,
            gh_cmd=gh_cmd, operation=operation, actor=None, expected_actor=EXPECTED_ACTOR,
            completed_steps=["attach_comment"], failed_step="attachment_readback", is_write=False,
            retry_summaries=retry_summaries,
        )
    except CommentError as error:
        raise _local_error(
            str(error), operation=operation, cause=error.failure.cause,
            actor=actor, expected_actor=actor, completed_steps=["attach_comment"],
            failed_step="attachment_readback", write_outcome="applied",
            payload={"url": url, "failed_request": error.api_result},
        ) from error
    saved = readback.body
    saved_user = saved.get("user") if isinstance(saved, dict) else None
    saved_author = str(saved_user.get("login") or "") if isinstance(saved_user, dict) else ""
    if (not isinstance(saved, dict) or saved.get("id") != int(match[1])
            or saved.get("html_url") != url or saved_author.casefold() != actor
            or str(saved.get("body", "")).splitlines()[:1] != [AGENT_POSTED_LINE]
            or "https://github.com/user-attachments/assets/" not in str(saved.get("body", ""))):
        raise _local_error(
            "Attachment readback did not confirm the comment, author, agent line and uploaded asset; read the thread before retrying",
            operation=operation, cause="invalid_response", actor=actor, expected_actor=actor,
            failed_step="attachment_readback", write_outcome="applied", payload={"url": url},
        )
    return _comment_payload(
        readback.body, operation=operation, kind=kind, repo=resolved_repo, number=number,
        actor=actor, expected_actor=actor, comment_action="created",
        completed_steps=["attach_comment", "attachment_readback"],
    )


def comment(
    kind: str,
    number: int,
    body: str,
    *,
    repo: Optional[str] = None,
    edit_last: bool = False,
    edit_comment: Optional[int] = None,
    expected_body: Optional[str] = None,
    expected_updated_at: Optional[str] = None,
    create_if_none: bool = False,
    gh_cmd: str = DEFAULT_GH,
    expected_actor: Optional[str] = EXPECTED_ACTOR,
    operation: Optional[str] = None,
    completed_steps: Optional[list[str]] = None,
    failed_step: Optional[str] = None,
    dedupe_body: bool = False,
    before_create: Callable[[dict[str, Any]], None] | None = None,
    attachments: list[str] | None = None,
    retry_summaries: Optional[list[github_api_core.RetrySummary]] = None,
) -> dict[str, Any]:
    collected_retry_summaries = retry_summaries if retry_summaries is not None else []
    try:
        if question_id.QUESTION_HEADING.match(body):
            operation = operation or f"github.comment.{kind}"
            repo = resolve_repo(repo, gh_cmd=gh_cmd, operation=operation)
            try:
                validate_question_mentions(body, repo)
            except ValueError as error:
                raise _local_error(
                    str(error), operation=operation, cause="validation_error",
                    expected_actor=effective_expected_actor(expected_actor), failed_step="input_validation",
                ) from error
        if attachments:
            if (kind not in ("issue", "pr") or number <= 0 or edit_last or edit_comment is not None
                    or expected_body is not None or expected_updated_at is not None or create_if_none or dedupe_body or before_create is not None):
                raise _local_error(
                    "--attach requires a new issue or PR comment with a positive target; editing and deduplication are unsupported",
                    operation=operation or f"github.comment.{kind}", cause="validation_error", failed_step="input_validation",
                )
            payload = attached_comment(
                kind, number, body, attachments, repo=repo, gh_cmd=gh_cmd,
                operation=operation or f"github.comment.{kind}", retry_summaries=collected_retry_summaries,
            )
        else:
            payload = _comment_impl(
                kind,
                number,
                body,
                repo=repo,
                edit_last=edit_last,
                edit_comment=edit_comment,
                expected_body=expected_body,
                expected_updated_at=expected_updated_at,
                create_if_none=create_if_none,
                gh_cmd=gh_cmd,
                expected_actor=expected_actor,
                operation=operation,
                completed_steps=completed_steps,
                failed_step=failed_step,
                dedupe_body=dedupe_body,
                before_create=before_create,
                retry_summaries=collected_retry_summaries,
            )
        question_body = body.removeprefix(AGENT_POSTED_LINE).lstrip()
        if question_id.QUESTION_HEADING.match(question_body):
            if assigned := question_id.heading_id(question_body):
                try:
                    question_id.update(question_id.counter_path(), question_url=payload["url"], question_number=int(assigned[1:]))
                except (OSError, ValueError) as error:
                    raise _local_error(str(error), operation=operation or f"github.comment.{kind}",
                                       cause="question_numbering_failed", failed_step="bind_question_source",
                                       write_outcome="applied", payload={"url": payload["url"], "question_id": assigned}) from error
                payload["question_id"] = assigned
            else:
                payload = _number_saved_question(payload, body, gh_cmd=gh_cmd, retry_summaries=collected_retry_summaries)
        return payload
    except (OSError, ValueError) as error:
        raise _local_error(str(error), operation=operation or f"github.comment.{kind}",
                           cause="validation_error", failed_step="validate_question_identity") from error
    except CommentError as error:
        _enrich_error_with_retry_summaries(error, collected_retry_summaries)
        raise


def verify_question_number(body: str, source: str | None = None) -> None:
    question = question_id.heading_id(body.removeprefix(AGENT_POSTED_LINE).lstrip())
    if question:
        question_id.verify_number(question_id.counter_path(), int(question[1:]), source)


def _number_saved_question(payload: dict[str, Any], requested_body: str, *, gh_cmd: str,
                           retry_summaries: list[github_api_core.RetrySummary]) -> dict[str, Any]:
    """Allocate only for a confirmed saved comment; its URL owns retry identity."""
    operation = "github.comment." + payload["kind"]
    repo, number = payload["repo"], payload["number"]
    steps = [*payload["completed_steps"], "save_question"]
    assigned = None
    try:
        selected_id = payload["comment"]["id"]
        result = _call_api(
            "GET", f"/repos/{repo}/issues/comments/{selected_id}", None,
            gh_cmd=gh_cmd, operation=operation, actor=payload["actor"],
            expected_actor=payload["expected_actor"], completed_steps=steps,
            failed_step="read_saved_question", is_write=False, retry_summaries=retry_summaries,
        )
        saved = result.body
        if (not isinstance(saved, dict) or saved.get("id") != selected_id
                or saved.get("html_url") != payload["url"]
                or str((saved.get("user") or {}).get("login") or "").casefold() != payload["actor"].casefold()
                or not isinstance(saved.get("body"), str)):
            raise ValueError("Saved question readback does not match the confirmed comment")
        saved_body = saved["body"]
        if question_id.heading_id(saved_body):
            verify_question_number(saved_body, payload["url"])
            payload["question_id"] = question_id.heading_id(saved_body)
            return payload
        if _canonical_comment_body(saved_body) != _canonical_comment_body(requested_body):
            raise ValueError("Saved question changed before numbering; read it before repairing")
        assigned = "Q" + str(question_id.update(question_id.counter_path(), question_url=payload["url"]))
        numbered_body = question_id.number_heading(saved_body, assigned)
        try:
            numbered = _comment_impl(
                payload["kind"], number, numbered_body,
                repo=repo, edit_comment=selected_id, expected_body=saved_body,
                expected_updated_at=saved.get("updated_at"), gh_cmd=gh_cmd,
                expected_actor=payload["expected_actor"], operation=operation,
                completed_steps=steps, retry_summaries=retry_summaries,
            )
        except CommentError as conflict:
            if conflict.failure.cause != "comment_conflict":
                raise
            # Another numbering process may have completed the same assignment.
            # Read only: never replay a conflicting write or accept changed text.
            current = _call_api(
                "GET", f"/repos/{repo}/issues/comments/{selected_id}", None,
                gh_cmd=gh_cmd, operation=operation, actor=payload["actor"],
                expected_actor=payload["expected_actor"], completed_steps=steps,
                failed_step="reconcile_question_number", is_write=False, retry_summaries=retry_summaries,
            )
            confirmed = current.body
            if (not isinstance(confirmed, dict) or confirmed.get("id") != selected_id
                    or confirmed.get("html_url") != payload["url"]
                    or str((confirmed.get("user") or {}).get("login") or "").casefold() != payload["actor"].casefold()
                    or confirmed.get("body") != numbered_body):
                raise conflict
            numbered = _comment_payload(
                confirmed, operation=operation, kind=payload["kind"], repo=repo,
                number=number, actor=payload["actor"], expected_actor=payload["expected_actor"],
                comment_action="existing", completed_steps=[*steps, "reconcile_question_number"],
                retry_summary=current.retry_summary,
            )
        payload["comment"] = numbered["comment"]
        payload["completed_steps"] = numbered["completed_steps"]
        payload["question_id"] = assigned
        return payload
    except (OSError, ValueError) as error:
        raise _local_error(
            str(error), operation=operation, cause="question_numbering_failed",
            actor=payload["actor"], expected_actor=payload["expected_actor"],
            completed_steps=steps, failed_step="number_saved_question",
            write_outcome="applied", payload={"url": payload["url"], "question_id": assigned,
                "outcome_certainty": "confirmed_partially_applied",
                "reconciliation": {"strategy": "read_saved_question_before_exact_edit"}},
        ) from error
    except CommentError as error:
        failed_request = dict(error.api_result or {})
        certainty = "unknown" if error.failure.write_outcome == "unknown" else "confirmed_partially_applied"
        error.payload.update({"url": payload["url"], "question_id": assigned,
                              "saved_question": True, "failed_request": failed_request,
                              "outcome_certainty": certainty,
                              "recommended_next_action": "read_saved_question_before_exact_edit"})
        error.failure.write_outcome = "applied"
        error.failure.completed_steps = list(dict.fromkeys([*steps, *error.failure.completed_steps]))
        if error.api_result is not None:
            error.api_result.update({"write_outcome": "applied", "outcome_certainty": certainty,
                                     "completed_steps": error.failure.completed_steps,
                                     "failure": error.failure.as_dict()})
        raise


def read_body(path: str, *, preserve_newlines: bool = False) -> str:
    if path == "-":
        if preserve_newlines:
            return sys.stdin.buffer.read().decode("utf-8")
        return sys.stdin.read()
    try:
        with pathlib.Path(path).open(encoding="utf-8", newline="" if preserve_newlines else None) as stream:
            return stream.read()
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
    parser.add_argument("--verbatim", action="store_true", help="Relay source text or logs unchanged; not for authored planning text.")
    parser.add_argument("--attach", action="append", help="Attach FILE[#alt] through GitHub CLI; repeatable (images/videos only).")
    parser.add_argument("--edit-last", action="store_true")
    parser.add_argument("--edit-comment", type=int, help="Edit this exact timeline comment ID, after checking its thread and author.")
    parser.add_argument("--expected-body-file", help="Require an exact prior body from this UTF-8 file (including operation markers).")
    parser.add_argument("--expected-updated-at", help="Require the prior comment's exact updated_at value.")
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


terminal_failure = _terminal_failure


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
        try:
            if not args.verbatim:
                validate_planning_references(body, args.repo or "OWNER/REPO")
        except ValueError as error:
            raise _local_error(str(error), operation=operation, cause="validation_error",
                               expected_actor=expected_actor, failed_step="input_validation") from error
        payload = comment(
            args.kind,
            args.number,
            body,
            repo=args.repo,
            edit_last=args.edit_last,
            edit_comment=args.edit_comment,
            expected_body=read_body(args.expected_body_file, preserve_newlines=True) if args.expected_body_file is not None else None,
            expected_updated_at=args.expected_updated_at,
            create_if_none=args.create_if_none,
            operation=operation,
            expected_actor=expected_actor,
            attachments=args.attach,
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
