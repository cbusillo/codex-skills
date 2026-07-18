#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
github_api.py — Dependency-free shared diagnostics foundation for GitHub API calls via gh CLI.

Provides:
- Body-safe gh api --include REST command construction (body via JSON stdin, never on command line)
- HTTP status/headers/body parsing from gh --include output
- Versioned ApiResult / FailureDetail envelope
- Matrix-gated reset-aware retries with inherited deadlines and stderr-only progress
- Lock-safe cross-process cooldowns keyed by GitHub host, actor, and quota bucket
- Write-safe reconciliation callbacks for unknown non-idempotent outcomes
- Error classification: invalid_credentials, actor_mismatch, permission_denied,
  rest_primary_rate_limited, graphql_primary_rate_limited,
  secondary_rate_limited, not_found, validation_error, conflict,
  network_provider_failure
- Field redaction for tokens, headers, paths, private body keys
- Bounded one-call-per-process /rate_limit probe
- Configurable gh command path via gh_cmd parameter
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html as html_lib
import json
import math
import os
import pathlib
import random
import re
import secrets
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

SCHEMA_VERSION = 1
DEFAULT_API_VERSION = "2022-11-28"
TRANSPORT = "gh_api"
DEFAULT_GH = os.environ.get("GITHUB_API_GH") or str(pathlib.Path(__file__).with_name("gh-with-env-token"))
DEFAULT_HOST = os.environ.get("GH_HOST") or "github.com"
DEFAULT_OPERATION_MATRIX = pathlib.Path(__file__).resolve().parents[1] / "references/operation-matrix.toml"
DEFAULT_RETRY_MAX_WAIT_SECONDS = 3900.0
DEFAULT_RETRY_MAX_ATTEMPTS = 8
DEFAULT_RETRY_PROGRESS_SECONDS = 30.0
DEFAULT_RETRY_JITTER_SECONDS = 3.0
FLEXIBLE_RETRY_BUCKETS = frozenset({"delegated", "mixed", "unknown"})
PRIMARY_RATE_LIMIT_CAUSES = frozenset(
    {"graphql_primary_rate_limited", "rest_primary_rate_limited", "rate_limited_unknown_bucket"}
)
OPERATION_MARKER_PREFIX = "<!-- github-skill-operation:"

GraphQLOperation = Literal["query", "mutation", "subscription", "unknown"]
ReconciliationOutcome = Literal["matched", "no_match", "ambiguous", "failed"]


@dataclass(frozen=True)
class CommandContext:
    is_write: bool
    transport: str
    bucket: str
    graphql_operation: Optional[GraphQLOperation] = None


def _default_retry_state_dir() -> pathlib.Path:
    code_home = os.environ.get("CODE_HOME") or os.environ.get("CODEX_HOME")
    root = pathlib.Path(code_home).expanduser() if code_home else pathlib.Path.home() / ".code"
    return root / "state" / "github-retry"


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, default))
        return max(minimum, value) if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_optional_float(name: str) -> Optional[float]:
    value = os.environ.get(name)
    if value is None:
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except ValueError:
        return None


@dataclass(frozen=True)
class OperationRetryRule:
    operation: str
    idempotency: str
    retry_eligibility: str
    reconciliation_strategy: str
    quota_bucket: str


@dataclass(frozen=True)
class RetryPolicy:
    max_wait_seconds: float = DEFAULT_RETRY_MAX_WAIT_SECONDS
    max_attempts: int = DEFAULT_RETRY_MAX_ATTEMPTS
    base_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 60.0
    jitter_seconds: float = DEFAULT_RETRY_JITTER_SECONDS
    progress_interval_seconds: float = DEFAULT_RETRY_PROGRESS_SECONDS
    wait_slice_seconds: float = 1.0
    lock_poll_seconds: float = 0.25
    drain_seconds: float = 2.0
    stale_after_seconds: float = 7200.0
    state_dir: pathlib.Path = field(default_factory=_default_retry_state_dir)

    @classmethod
    def from_env(cls) -> "RetryPolicy":
        state_dir = pathlib.Path(
            os.environ.get("GITHUB_RETRY_STATE_DIR") or _default_retry_state_dir()
        ).expanduser()
        return cls(
            max_wait_seconds=_env_float(
                "GITHUB_RETRY_MAX_WAIT_SECONDS",
                DEFAULT_RETRY_MAX_WAIT_SECONDS,
            ),
            max_attempts=_env_int("GITHUB_RETRY_MAX_ATTEMPTS", DEFAULT_RETRY_MAX_ATTEMPTS),
            base_backoff_seconds=_env_float("GITHUB_RETRY_BASE_BACKOFF_SECONDS", 1.0),
            max_backoff_seconds=_env_float("GITHUB_RETRY_MAX_BACKOFF_SECONDS", 60.0),
            jitter_seconds=_env_float("GITHUB_RETRY_JITTER_SECONDS", DEFAULT_RETRY_JITTER_SECONDS),
            progress_interval_seconds=_env_float(
                "GITHUB_RETRY_PROGRESS_SECONDS",
                DEFAULT_RETRY_PROGRESS_SECONDS,
            ),
            wait_slice_seconds=_env_float("GITHUB_RETRY_WAIT_SLICE_SECONDS", 1.0, minimum=0.01),
            lock_poll_seconds=_env_float("GITHUB_RETRY_LOCK_POLL_SECONDS", 0.25, minimum=0.01),
            drain_seconds=_env_float("GITHUB_RETRY_DRAIN_SECONDS", 2.0),
            stale_after_seconds=_env_float("GITHUB_RETRY_STALE_SECONDS", 7200.0, minimum=1.0),
            state_dir=state_dir,
        )


def _default_retry_progress(event: dict[str, Any]) -> None:
    remaining = max(0.0, float(event.get("remaining_seconds") or 0.0))
    print(
        "retry: "
        f"{event.get('operation')} waiting {remaining:.0f}s "
        f"for {event.get('cause')} "
        f"(attempt {event.get('attempt')}/{event.get('max_attempts')})",
        file=sys.stderr,
    )


@dataclass
class RetryRuntime:
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    jitter: Callable[[float], float] = field(
        default_factory=lambda: lambda maximum: random.SystemRandom().uniform(0.0, max(0.0, maximum))
    )
    cancelled: Callable[[], bool] = field(default_factory=lambda: lambda: False)
    progress: Callable[[dict[str, Any]], None] = _default_retry_progress


@dataclass(frozen=True)
class ReconciliationDecision:
    outcome: ReconciliationOutcome
    body: Any = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReconciliationContext:
    deadline_at: float
    retry_policy: RetryPolicy
    retry_runtime: RetryRuntime


@dataclass
class RetrySummary:
    attempts: int
    elapsed_wait: float
    retry_eligible: bool
    last_actor: Optional[str]
    last_bucket: Optional[str]
    outcome_certainty: str
    reconciliation: Optional[dict[str, Any]]
    recommended_next_action: str
    effective_deadline: float
    exhausted_reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "elapsed_wait": round(self.elapsed_wait, 3),
            "retry_eligible": self.retry_eligible,
            "last_actor": self.last_actor,
            "last_bucket": self.last_bucket,
            "outcome_certainty": self.outcome_certainty,
            "reconciliation": self.reconciliation,
            "recommended_next_action": self.recommended_next_action,
            "effective_deadline": self.effective_deadline,
            "retry_exhausted_reason": self.exhausted_reason,
        }


def aggregate_retry_summaries(
    summaries: list[RetrySummary],
    *,
    failed: bool = False,
) -> Optional[RetrySummary]:
    if not summaries:
        return None
    reconciliation = next(
        (summary.reconciliation for summary in reversed(summaries) if summary.reconciliation is not None),
        None,
    )
    exhausted_reason = next(
        (summary.exhausted_reason for summary in reversed(summaries) if summary.exhausted_reason is not None),
        None,
    )
    recommended_next_action = next(
        (
            summary.recommended_next_action
            for summary in reversed(summaries)
            if summary.recommended_next_action != "none"
        ),
        summaries[-1].recommended_next_action,
    )
    certainty_priority = {
        "unknown": 4,
        "reconciled_not_applied": 3,
        "reconciled_applied": 2,
        "confirmed_not_applied": 1,
        "confirmed": 1,
        "not_applicable": 0,
    }
    outcome_certainty = (
        "not_applicable"
        if failed
        else max(
            enumerate(summaries),
            key=lambda item: (
                certainty_priority.get(item[1].outcome_certainty, 0),
                item[0],
            ),
        )[1].outcome_certainty
    )
    return RetrySummary(
        attempts=sum(summary.attempts for summary in summaries),
        elapsed_wait=sum(summary.elapsed_wait for summary in summaries),
        retry_eligible=all(summary.retry_eligible for summary in summaries),
        last_actor=summaries[-1].last_actor,
        last_bucket=summaries[-1].last_bucket,
        outcome_certainty=outcome_certainty,
        reconciliation=reconciliation,
        recommended_next_action=recommended_next_action,
        effective_deadline=min(summary.effective_deadline for summary in summaries),
        exhausted_reason=exhausted_reason,
    )


def new_operation_id() -> str:
    return secrets.token_hex(16)


def operation_marker_comment(operation_id: str) -> str:
    return f"{OPERATION_MARKER_PREFIX}{operation_id} -->"


def body_with_operation_marker(body: str, operation_id: str) -> str:
    separator = "" if body.endswith("\n") else "\n"
    return f"{body}{separator}\n{operation_marker_comment(operation_id)}\n"


def body_has_operation_marker(body: Any, operation_id: str) -> bool:
    return isinstance(body, str) and operation_marker_comment(operation_id) in body


class ArgumentParsingError(Exception):
    pass


class TerminalArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ArgumentParsingError(message)


def requested_subcommand(argv: list[str], commands: set[str]) -> str:
    for token in argv:
        if token in commands:
            return token
    return "unknown"

# ---------------------------------------------------------------------------
# Redaction constants
# ---------------------------------------------------------------------------

_REDACT_HEADER_NAMES: frozenset[str] = frozenset({
    "authorization",
    "x-github-token",
    "cookie",
    "set-cookie",
    "x-auth-token",
    "proxy-authorization",
    "x-token",
    "gh-token",
})

_REDACT_BODY_KEYS: frozenset[str] = frozenset({
    "token",
    "secret",
    "password",
    "access_token",
    "refresh_token",
    "client_secret",
    "private_key",
    "api_key",
})

_REDACT_PARAM_RE = re.compile(
    r"(?i)((?:token|secret|password|key|auth|authorization|credentials?|api_key|client_secret|private_key|access_token|refresh_token|github_token)=)[^&\s#]+",
)
_REDACT_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b")
_REDACT_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/-]+=*\b")
_CAMEL_CASE_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class RateLimitInfo:
    limit: Optional[int] = None
    remaining: Optional[int] = None
    reset: Optional[int] = None
    used: Optional[int] = None
    resource: Optional[str] = None
    retry_after: Optional[int] = None

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> "RateLimitInfo":
        def _int(key: str) -> Optional[int]:
            v = headers.get(key)
            if v is None:
                return None
            try:
                return int(v)
            except ValueError:
                return None

        return cls(
            limit=_int("x-ratelimit-limit"),
            remaining=_int("x-ratelimit-remaining"),
            reset=_int("x-ratelimit-reset"),
            used=_int("x-ratelimit-used"),
            resource=headers.get("x-ratelimit-resource"),
            retry_after=_int("retry-after"),
        )

    def is_populated(self) -> bool:
        return any(
            value is not None
            for value in (
                self.limit,
                self.remaining,
                self.reset,
                self.used,
                self.resource,
                self.retry_after,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in {
            "limit": self.limit,
            "remaining": self.remaining,
            "reset": self.reset,
            "used": self.used,
            "resource": self.resource,
            "retry_after": self.retry_after,
        }.items() if v is not None}


@dataclass
class FailureDetail:
    """Structured failure information extracted from a GitHub API error response."""
    cause: str          # classification key (see classify_error)
    message: str        # human-readable summary
    retryable: bool     # safe to retry the same call
    fallback_eligible: bool  # worth trying a different auth method
    disposition: str    # "stop" | "retry" | "requires_authorization"
    write_outcome: Optional[str] = None   # "not_started" | "rejected" | "unknown"
    completed_steps: list[str] = field(default_factory=list)
    failed_step: Optional[str] = None
    request_id: Optional[str] = None
    rate_limit: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        self.message = redact_string(str(self.message))

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "cause": self.cause,
            "message": redact_body(self.message),
            "retryable": self.retryable,
            "fallback_eligible": self.fallback_eligible,
            "disposition": self.disposition,
        }
        if self.write_outcome is not None:
            d["write_outcome"] = self.write_outcome
        if self.completed_steps:
            d["completed_steps"] = self.completed_steps
        if self.failed_step is not None:
            d["failed_step"] = self.failed_step
        if self.request_id is not None:
            d["request_id"] = self.request_id
        if self.rate_limit is not None:
            d["rate_limit"] = self.rate_limit
        return d


@dataclass
class ApiResult:
    """Versioned result/error envelope for a single gh api call."""
    ok: bool
    status: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)
    request_id: Optional[str] = None
    rate_limit: Optional[RateLimitInfo] = None
    failure: Optional[FailureDetail] = None
    operation: Optional[str] = None
    actor: Optional[str] = None
    expected_actor: Optional[str] = None
    host: Optional[str] = None
    transport: str = TRANSPORT
    bucket: Optional[str] = None
    graphql_operation: Optional[GraphQLOperation] = None
    completed_steps: list[str] = field(default_factory=list)
    failed_step: Optional[str] = None
    retry_summary: Optional[RetrySummary] = None
    schema_version: int = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        failure = self.failure
        rate_limit = self.rate_limit.as_dict() if self.rate_limit and self.rate_limit.is_populated() else None
        if rate_limit is None and failure and failure.rate_limit:
            rate_limit = failure.rate_limit
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "ok": self.ok,
            "exit_code": 0 if self.ok else 1,
            "status": self.status,
            "transport": self.transport,
            "bucket": self.bucket or (rate_limit or {}).get("resource"),
            "actor": self.actor,
            "expected_actor": self.expected_actor,
            "host": self.host,
            "request_id": self.request_id or (failure.request_id if failure else None),
            "quota": rate_limit,
            "retryable": failure.retryable if failure else False,
            "fallback_eligible": failure.fallback_eligible if failure else False,
            "retry_at": (rate_limit or {}).get("reset"),
            "retry_after": (rate_limit or {}).get("retry_after"),
            "write_outcome": failure.write_outcome if failure else None,
            "disposition": failure.disposition if failure else "complete",
            "completed_steps": self.completed_steps or (failure.completed_steps if failure else []),
            "failed_step": self.failed_step or (failure.failed_step if failure else None),
            "graphql_operation": self.graphql_operation,
            "body": redact_body(self.body),
        }
        if self.operation:
            d["operation"] = self.operation
        if rate_limit is not None:
            d["rate_limit"] = rate_limit
        if failure:
            d["failure"] = failure.as_dict()
        if self.retry_summary is not None:
            d.update(self.retry_summary.as_dict())
        return d


RETRY_TERMINAL_KEYS = frozenset({
    "attempts",
    "elapsed_wait",
    "retry_eligible",
    "last_actor",
    "last_bucket",
    "outcome_certainty",
    "reconciliation",
    "recommended_next_action",
    "effective_deadline",
    "retry_exhausted_reason",
})


_TERMINAL_RESERVED_KEYS = frozenset({
    "schema_version",
    "ok",
    "exit_code",
    "status",
    "operation",
    "transport",
    "bucket",
    "actor",
    "expected_actor",
    "host",
    "request_id",
    "quota",
    "rate_limit",
    "retryable",
    "fallback_eligible",
    "retry_at",
    "retry_after",
    "write_outcome",
    "disposition",
    "completed_steps",
    "failed_step",
    "graphql_operation",
    "failure",
    "body",
    *RETRY_TERMINAL_KEYS,
})


def terminal_envelope(
    result: ApiResult,
    payload: Any = None,
    *,
    exit_code: Optional[int] = None,
    error: Optional[str] = None,
    error_code: Optional[str] = None,
) -> dict[str, Any]:
    """Flatten helper-specific payload into the shared terminal contract."""
    envelope = result.as_dict()
    envelope.pop("body", None)
    envelope["exit_code"] = (0 if result.ok else 1) if exit_code is None else exit_code
    if isinstance(payload, dict):
        for key, value in redact_body(payload).items():
            if key == "operation" and value != envelope.get("operation"):
                envelope.setdefault("action", value)
            elif key in RETRY_TERMINAL_KEYS:
                envelope[key] = value
            elif key not in _TERMINAL_RESERVED_KEYS:
                envelope[key] = value
    elif payload is not None:
        envelope["result"] = redact_body(payload)
    if not result.ok:
        failure = result.failure
        message = error or (failure.message if failure else "GitHub helper failed")
        envelope["error"] = redact_string(message)
        envelope["error_code"] = error_code or (failure.cause if failure else "helper_error")
    return envelope


def terminal_success(
    payload: Any,
    *,
    operation: str,
    actor: Optional[str] = None,
    expected_actor: Optional[str] = None,
    host: Optional[str] = None,
    transport: str = "helper",
    bucket: Optional[str] = None,
    status: int = 0,
    request_id: Optional[str] = None,
    rate_limit: Optional[RateLimitInfo] = None,
    graphql_operation: Optional[GraphQLOperation] = None,
    completed_steps: Optional[list[str]] = None,
) -> dict[str, Any]:
    return terminal_envelope(
        ApiResult(
            ok=True,
            status=status,
            body=None,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            host=host or DEFAULT_HOST,
            transport=transport,
            bucket=bucket,
            request_id=request_id,
            rate_limit=rate_limit,
            graphql_operation=graphql_operation,
            completed_steps=completed_steps or [],
        ),
        payload,
    )


def terminal_failure(
    failure: FailureDetail,
    *,
    operation: str,
    payload: Any = None,
    actor: Optional[str] = None,
    expected_actor: Optional[str] = None,
    host: Optional[str] = None,
    transport: str = "helper",
    bucket: Optional[str] = None,
    status: int = 0,
    exit_code: int = 1,
    request_id: Optional[str] = None,
    rate_limit: Optional[RateLimitInfo] = None,
    graphql_operation: Optional[GraphQLOperation] = None,
    completed_steps: Optional[list[str]] = None,
    failed_step: Optional[str] = None,
    error: Optional[str] = None,
    error_code: Optional[str] = None,
) -> dict[str, Any]:
    return terminal_envelope(
        ApiResult(
            ok=False,
            status=status,
            body=None,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            host=host or DEFAULT_HOST,
            transport=transport,
            bucket=bucket,
            request_id=request_id or failure.request_id,
            rate_limit=rate_limit,
            failure=failure,
            graphql_operation=graphql_operation,
            completed_steps=completed_steps or failure.completed_steps,
            failed_step=failed_step or failure.failed_step,
        ),
        payload,
        exit_code=exit_code,
        error=error,
        error_code=error_code,
    )


def emit_terminal(payload: dict[str, Any], *, stderr_message: Optional[str] = None) -> int:
    """Emit exactly one JSON object on stdout and human diagnostics on stderr."""
    print(json.dumps(redact_body(payload), sort_keys=True, separators=(",", ":")))
    if stderr_message:
        print(redact_string(stderr_message), file=sys.stderr)
    return int(payload.get("exit_code", 0 if payload.get("ok") else 1))


# ---------------------------------------------------------------------------
# Parsing gh --include output
# ---------------------------------------------------------------------------


def _try_parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


_ACTIVE_AUTH_ACTOR_RE = re.compile(
    r"(?:using|retrying with) the active gh account '([^']+)'",
    re.IGNORECASE,
)


def actor_from_gh_stderr(stderr: str) -> Optional[str]:
    matches = _ACTIVE_AUTH_ACTOR_RE.findall(stderr)
    return matches[-1].strip() if matches and matches[-1].strip() else None


def active_fallback_was_authorized(stderr: str) -> bool:
    return "explicitly authorized active-auth fallback" in stderr.casefold()


def parse_gh_include_output(raw: str) -> tuple[int, dict[str, str], Any]:
    """
    Parse the combined stdout of `gh api --include`.

    Format:
        HTTP/2.0 200 \\r\\n
        header-name: value\\r\\n
        \\r\\n
        {body}

    Returns (status_code, headers_dict_lowercase_keys, parsed_body).
    body is parsed as JSON when content-type contains "json" or body starts with
    '{' / '['; otherwise returned as a string. Empty body returns None.
    """
    raw = raw.replace("\r\n", "\n")
    lines = raw.split("\n")

    status = 0
    headers: dict[str, str] = {}
    status_indices = [index for index, line in enumerate(lines) if line.startswith("HTTP/")]
    if not status_indices:
        body_str = raw.strip()
        return 0, {}, _try_parse_json(body_str) if body_str else None

    # Parse sequential 1xx/3xx response blocks only. Once a final response is
    # reached, HTTP-looking body lines belong to the body (not another block).
    i = status_indices[0]
    carried_diagnostics: dict[str, str] = {}
    while i < len(lines) and lines[i].startswith("HTTP/"):
        status_line = lines[i]
        parts = status_line.split(None, 2)
        try:
            status = int(parts[1]) if len(parts) >= 2 else 0
        except (ValueError, IndexError):
            status = 0
        i += 1
        block_headers: dict[str, str] = {}
        while i < len(lines):
            line = lines[i].rstrip()
            if not line:
                i += 1
                break
            if ":" in line:
                name, _, value = line.partition(":")
                block_headers[name.strip().lower()] = value.strip()
            i += 1
        for name, value in block_headers.items():
            if name == "x-github-request-id" or name == "retry-after" or name.startswith("x-ratelimit-"):
                carried_diagnostics[name] = value
        headers = block_headers
        next_index = i
        while next_index < len(lines) and not lines[next_index]:
            next_index += 1
        is_interim = (
            100 <= status < 200
            or 300 <= status < 400
            or "connection established" in status_line.casefold()
        )
        if is_interim and next_index < len(lines) and lines[next_index].startswith("HTTP/"):
            i = next_index
            continue
        break

    for name, value in carried_diagnostics.items():
        headers.setdefault(name, value)

    body_str = "\n".join(lines[i:]).strip()
    if not body_str:
        return status, headers, None

    content_type = headers.get("content-type", "")
    if "json" in content_type or body_str.startswith(("{", "[")):
        body = _try_parse_json(body_str)
    else:
        body = body_str

    return status, headers, body


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _extract_message(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or "")
    if not body:
        return ""
    text = str(body)
    if re.search(r"<!doctype\s+html|<html\b|<title\b|<body\b", text, re.IGNORECASE):
        parts: list[str] = []
        for tag in ("title", "h1", "p"):
            match = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", text, re.IGNORECASE | re.DOTALL)
            if not match:
                continue
            value = re.sub(r"<[^>]+>", " ", match.group(1))
            value = " ".join(html_lib.unescape(value).split())
            if value and value not in parts:
                parts.append(value)
        if parts:
            return " — ".join(parts)[:1000]
        return "GitHub returned a non-JSON HTML response"
    return " ".join(text.split())[:1000]


def _is_graphql_rate_limit_body(body: Any) -> bool:
    """Detect GraphQL rate-limit inside an HTTP-200 JSON body."""
    if not isinstance(body, dict):
        return False
    errors = body.get("errors")
    if not isinstance(errors, list):
        return False
    for err in errors:
        if not isinstance(err, dict):
            continue
        etype = str(err.get("type", "")).upper()
        emsg = str(err.get("message", "")).lower()
        if etype == "RATE_LIMITED" or "rate limit" in emsg or "rate_limited" in emsg:
            return True
    return False


def is_graphql_path(path: str) -> bool:
    normalized = path.split("?", 1)[0].rstrip("/").lower()
    return normalized == "graphql" or normalized.endswith("/graphql")


def is_repository_secret_scanning_alert_path(path: str) -> bool:
    parsed = urllib.parse.urlsplit(path)
    candidate = parsed.path
    while True:
        decoded = urllib.parse.unquote(candidate)
        if decoded == candidate:
            break
        candidate = decoded
    normalized = f"/{candidate.lstrip('/')}"
    return bool(re.fullmatch(r"/repos/[^/]+/[^/]+/secret-scanning/alerts(?:/.*)?", normalized))


def infer_api_bucket(path: str) -> str:
    if is_graphql_path(path):
        return "graphql"
    normalized = path.split("?", 1)[0].rstrip("/").lower()
    if re.search(r"(?:^|/)search(?:/|$)", normalized):
        return "search"
    return "rest_core"


def normalized_provider_bucket(resource: Optional[str]) -> Optional[str]:
    if not isinstance(resource, str):
        return None
    return {
        "core": "rest_core",
        "graphql": "graphql",
        "rest_core": "rest_core",
        "search": "search",
    }.get(resource.casefold())


def infer_graphql_operation_type(body: Any) -> GraphQLOperation:
    """Classify a GraphQL document without assuming every POST is a write."""
    document: Any = body.get("query") if isinstance(body, dict) else body
    if not isinstance(document, str):
        return "unknown"
    remaining = document.lstrip("\ufeff")
    while True:
        remaining = remaining.lstrip()
        if not remaining.startswith("#"):
            break
        _, separator, remaining = remaining.partition("\n")
        if not separator:
            return "unknown"
    match = re.match(r"(?i)(query|mutation|subscription)\b", remaining)
    if match:
        return match.group(1).lower()  # type: ignore[return-value]
    if remaining.startswith("{"):
        return "query"
    return "unknown"


def infer_is_write(
    method: str,
    path: str,
    body: Any = None,
    *,
    explicit_is_write: Optional[bool] = None,
    graphql_operation: Optional[GraphQLOperation] = None,
) -> bool:
    if explicit_is_write is not None:
        return explicit_is_write
    if is_graphql_path(path):
        operation = graphql_operation or infer_graphql_operation_type(body)
        if operation in ("query", "subscription"):
            return False
        return True
    return method.upper() in ("POST", "PUT", "PATCH", "DELETE")


def infer_gh_command_context(args: list[str], *, input_text: Optional[str] = None) -> CommandContext:
    """Infer transport and mutation posture for a delegated gh command."""
    tokens = list(args)
    while tokens:
        token = tokens[0]
        if token in ("-R", "--repo", "--hostname") and len(tokens) >= 2:
            tokens = tokens[2:]
            continue
        if token.startswith("--repo=") or token.startswith("--hostname="):
            tokens = tokens[1:]
            continue
        if token == "--":
            tokens = tokens[1:]
        break
    if not tokens:
        return CommandContext(False, "gh_cli", "unknown")

    command = tokens[0]
    subcommand = tokens[1] if len(tokens) > 1 else ""
    if command == "api":
        method = "GET"
        endpoint = ""
        graphql_document: Any = None
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if token in ("--method", "-X") and index + 1 < len(tokens):
                method = tokens[index + 1]
                index += 2
                continue
            if token.startswith("--method="):
                method = token.split("=", 1)[1]
                index += 1
                continue
            if token.startswith("-X") and token != "-X":
                method = token[2:]
                index += 1
                continue
            if token in ("-H", "--header") and index + 1 < len(tokens):
                index += 2
                continue
            if token in ("-f", "-F", "--field", "--raw-field") and index + 1 < len(tokens):
                value = tokens[index + 1]
                if value.startswith("query="):
                    graphql_document = value.split("=", 1)[1]
                index += 2
                continue
            if any(token.startswith(prefix) for prefix in ("--field=", "--raw-field=")):
                value = token.split("=", 1)[1]
                if value.startswith("query="):
                    graphql_document = value.split("=", 1)[1]
                index += 1
                continue
            if token in ("--input",) and index + 1 < len(tokens):
                if input_text:
                    graphql_document = _try_parse_json(input_text)
                index += 2
                continue
            if token.startswith("--input="):
                if input_text:
                    graphql_document = _try_parse_json(input_text)
                index += 1
                continue
            if not token.startswith("-") and not endpoint:
                endpoint = token
            index += 1
        if is_graphql_path(endpoint):
            operation = infer_graphql_operation_type(graphql_document)
            return CommandContext(
                infer_is_write(method, endpoint, graphql_document, graphql_operation=operation),
                "graphql_api",
                "graphql",
                operation,
            )
        field_write = any(
            token in ("-f", "-F", "--field", "--raw-field", "--input")
            or token.startswith(("--field=", "--raw-field=", "--input="))
            for token in tokens[1:]
        )
        return CommandContext(
            method.upper() not in ("GET", "HEAD") or field_write,
            "rest_api",
            "rest_core",
        )

    if command == "project":
        read_commands = {"list", "view", "field-list", "item-list"}
        return CommandContext(
            subcommand not in read_commands,
            "gh_cli_graphql",
            "graphql",
            "query" if subcommand in read_commands else "mutation",
        )

    write_commands: dict[str, set[str]] = {
        "issue": {"create", "edit", "close", "comment", "delete", "develop", "transfer", "pin", "unpin", "lock", "unlock", "reopen"},
        "pr": {"assign", "close", "comment", "convert-to-draft", "create", "edit", "label", "lock", "merge", "ready", "reopen", "request-review", "review", "unlock", "update-branch"},
        "run": {"approve", "cancel", "delete", "rerun"},
        "workflow": {"disable", "enable", "run"},
        "release": {"create", "upload", "edit", "delete", "delete-asset"},
        "repo": {"create", "edit", "delete", "archive", "unarchive", "fork", "deploy-key", "autolink", "sync"},
        "label": {"create", "edit", "delete", "clone"},
        "secret": {"set", "delete"},
        "variable": {"set", "delete"},
        "gist": {"create", "edit", "delete"},
    }
    is_write = subcommand in write_commands.get(command, set())
    bucket = "graphql" if command in ("issue", "pr", "label") else "mixed"
    return CommandContext(is_write, "gh_cli_wrapper", bucket)


def classify_error(
    status: int,
    headers: dict[str, str],
    body: Any,
    *,
    is_write: bool = False,
    expected_actor: Optional[str] = None,
    actual_actor: Optional[str] = None,
) -> FailureDetail:
    """Classify structured GitHub response evidence without changing actor."""
    msg = _extract_message(body)
    msg_lower = msg.lower()
    rl = RateLimitInfo.from_headers(headers)
    rl_dict = rl.as_dict() if rl.is_populated() else None
    request_id = headers.get("x-github-request-id")
    rejected = "rejected" if is_write else None
    not_started = "not_started" if is_write else None

    if expected_actor and actual_actor and expected_actor != actual_actor:
        return FailureDetail(
            cause="actor_mismatch",
            message=f"Authenticated actor '{actual_actor}' does not match expected actor '{expected_actor}'",
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome=not_started,
            request_id=request_id,
        )

    if status == 200 and _is_graphql_rate_limit_body(body):
        return FailureDetail(
            cause="graphql_primary_rate_limited",
            message="GraphQL primary rate limit exceeded (HTTP 200 errors array)",
            retryable=True,
            fallback_eligible=False,
            disposition="retry",
            write_outcome="unknown" if is_write else None,
            request_id=request_id,
            rate_limit=rl_dict,
        )

    retry_after = headers.get("retry-after")
    if status in (403, 429) and (
        retry_after
        or "secondary rate limit" in msg_lower
        or "abuse detection" in msg_lower
        or "abusive" in msg_lower
    ):
        ra_note = f" retry-after={retry_after}" if retry_after else ""
        return FailureDetail(
            cause="secondary_rate_limited",
            message=f"Secondary rate limit / abuse detection.{ra_note} {msg}".strip(),
            retryable=True,
            fallback_eligible=False,
            disposition="retry",
            write_outcome=rejected,
            request_id=request_id,
            rate_limit=rl_dict,
        )

    if status == 401:
        return FailureDetail(
            cause="invalid_credentials",
            message=f"Authentication failed: {msg}",
            retryable=False,
            fallback_eligible=True,
            disposition="requires_authorization",
            write_outcome=not_started,
            request_id=request_id,
        )

    if status == 403:
        permission_message = any(
            text in msg_lower
            for text in (
                "resource not accessible",
                "forbidden",
                "permission denied",
                "do not have permission",
                "must have",
                "requires permission",
            )
        )
        if not permission_message and "graphql" in msg_lower and (
            "rate limit" in msg_lower or "rate_limited" in msg_lower
        ):
            return FailureDetail(
                cause="graphql_primary_rate_limited",
                message=f"GraphQL rate limit exceeded (HTTP 403): {msg}",
                retryable=True,
                fallback_eligible=False,
                disposition="retry",
                write_outcome=rejected,
                request_id=request_id,
                rate_limit=rl_dict,
            )
        if not permission_message and (
            (rl.remaining is not None and rl.remaining == 0)
            or "rate limit exceeded" in msg_lower
            or "api rate limit" in msg_lower
        ):
            cause = (
                "graphql_primary_rate_limited"
                if (rl.resource or "").lower() == "graphql" or _is_graphql_rate_limit_body(body)
                else "rest_primary_rate_limited"
            )
            return FailureDetail(
                cause=cause,
                message=f"GitHub primary rate limit exceeded: {msg}",
                retryable=True,
                fallback_eligible=False,
                disposition="retry",
                write_outcome=rejected,
                request_id=request_id,
                rate_limit=rl_dict,
            )

        if _is_graphql_rate_limit_body(body):
            return FailureDetail(
                cause="graphql_primary_rate_limited",
                message=f"GraphQL rate limit exceeded (HTTP 403): {msg}",
                retryable=True,
                fallback_eligible=False,
                disposition="retry",
                write_outcome=rejected,
                request_id=request_id,
                rate_limit=rl_dict,
            )

        return FailureDetail(
            cause="permission_denied",
            message=f"Permission denied: {msg}",
            retryable=False,
            fallback_eligible=True,
            disposition="requires_authorization",
            write_outcome=not_started,
            request_id=request_id,
        )

    if status == 404:
        return FailureDetail(
            cause="not_found",
            message=f"Resource not found: {msg}",
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome=not_started,
            request_id=request_id,
        )

    if status == 409:
        return FailureDetail(
            cause="conflict",
            message=f"Conflict: {msg}",
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome=rejected,
            request_id=request_id,
        )

    if status == 422:
        return FailureDetail(
            cause="validation_error",
            message=f"Validation failed: {msg}",
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome=rejected,
            request_id=request_id,
        )

    if status == 429:
        return FailureDetail(
            cause="rest_primary_rate_limited",
            message=f"REST API rate limit (HTTP 429): {msg}",
            retryable=True,
            fallback_eligible=False,
            disposition="retry",
            write_outcome=rejected,
            request_id=request_id,
            rate_limit=rl_dict,
        )

    if status == 0 or status >= 500:
        return FailureDetail(
            cause="network_provider_failure",
            message=f"Network or provider failure (status={status}): {msg}",
            retryable=not is_write,
            fallback_eligible=False,
            disposition="retry" if not is_write else "stop",
            write_outcome="unknown" if is_write else None,
            request_id=request_id,
        )

    return FailureDetail(
        cause="unknown_error",
        message=f"Unexpected error (status={status}): {msg}",
        retryable=False,
        fallback_eligible=False,
        disposition="stop",
        write_outcome="unknown" if is_write else None,
        request_id=request_id,
    )


def _rate_limit_from_probe(
    result: Optional[ApiResult],
    *,
    preferred_resource: Optional[str] = None,
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    if result is None or not result.ok or not isinstance(result.body, dict):
        return None, None
    resources = result.body.get("resources")
    if not isinstance(resources, dict):
        return None, None
    exhausted: list[tuple[str, dict[str, Any]]] = []
    for name, value in resources.items():
        if isinstance(value, dict) and value.get("remaining") == 0:
            exhausted.append((str(name), value))
    if not exhausted:
        return None, None
    if preferred_resource is not None:
        preferred = preferred_resource.casefold()
        exhausted = [item for item in exhausted if item[0].casefold() == preferred]
        if not exhausted:
            return None, None
    for name, value in exhausted:
        if name.lower() == "graphql":
            return "graphql_primary_rate_limited", {
                "resource": "graphql",
                **{key: value.get(key) for key in ("limit", "remaining", "reset", "used") if value.get(key) is not None},
            }
    name, value = exhausted[0]
    return "rest_primary_rate_limited", {
        "resource": name,
        **{key: value.get(key) for key in ("limit", "remaining", "reset", "used") if value.get(key) is not None},
    }


def _failure_from_terminal_envelope(payload: Any, *, is_write: bool) -> Optional[FailureDetail]:
    if not isinstance(payload, dict) or payload.get("ok") is not False:
        return None
    failure = payload.get("failure")
    if not isinstance(failure, dict) or not failure.get("cause"):
        return None
    write_outcome = failure.get("write_outcome", payload.get("write_outcome"))
    if is_write and write_outcome is None:
        write_outcome = "unknown"
    completed_steps = failure.get("completed_steps", payload.get("completed_steps"))
    return FailureDetail(
        cause=str(failure["cause"]),
        message=str(failure.get("message") or payload.get("error") or "Delegated GitHub helper failed"),
        retryable=bool(failure.get("retryable", payload.get("retryable", False))),
        fallback_eligible=bool(failure.get("fallback_eligible", payload.get("fallback_eligible", False))),
        disposition=str(failure.get("disposition") or payload.get("disposition") or "stop"),
        write_outcome=str(write_outcome) if write_outcome is not None else None,
        completed_steps=[str(step) for step in completed_steps] if isinstance(completed_steps, list) else [],
        failed_step=str(failure.get("failed_step") or payload.get("failed_step") or "delegated_helper"),
        request_id=str(failure.get("request_id") or payload.get("request_id")) if failure.get("request_id") or payload.get("request_id") else None,
        rate_limit=failure.get("rate_limit") or payload.get("quota") or payload.get("rate_limit"),
    )


def classify_legacy_failure(
    stderr: str,
    *,
    stdout: str = "",
    is_write: bool = False,
    command_started: bool = True,
    rate_limit_result: Optional[ApiResult] = None,
) -> FailureDetail:
    """Classify a legacy gh failure, preferring structured evidence when present."""
    if stdout:
        delegated_failure = _failure_from_terminal_envelope(_try_parse_json(stdout.strip()), is_write=is_write)
        if delegated_failure is not None:
            return delegated_failure
        status, headers, body = parse_gh_include_output(stdout)
        if status or _is_graphql_rate_limit_body(body):
            if not _extract_message(body) and stderr.strip():
                body = {"message": stderr.strip()}
            return classify_error(status or 200, headers, body, is_write=is_write)

    message = "\n".join(part for part in (stderr.strip(), stdout.strip()) if part)
    lowered = message.lower()
    not_started = "not_started" if is_write else None
    rejected = "rejected" if is_write else None
    unknown = "unknown" if is_write else None
    rate_limit: Optional[dict[str, Any]] = None

    if "would run as" in lowered and "expected" in lowered:
        cause = "actor_mismatch"
        disposition = "stop"
        retryable = False
        fallback_eligible = False
        write_outcome = not_started
    elif any(text in lowered for text in ("deadline exceeded", "timed out", "timeout")):
        cause = "deadline_exceeded"
        disposition = "stop" if is_write else "retry"
        retryable = not is_write
        fallback_eligible = False
        write_outcome = unknown if command_started else not_started
    elif any(text in lowered for text in ("operation canceled", "operation cancelled", "context canceled", "context cancelled")):
        cause = "cancelled"
        disposition = "stop"
        retryable = False
        fallback_eligible = False
        write_outcome = unknown if command_started else not_started
    elif any(
        text in lowered
        for text in (
            "bad credentials",
            "token is invalid",
            "failed to log in",
            "authentication failed",
            "requires authentication",
            "gh auth login",
            "no automation gh token found",
        )
    ):
        cause = "invalid_credentials"
        disposition = "requires_authorization"
        retryable = False
        fallback_eligible = True
        write_outcome = not_started if "no automation gh token found" in lowered else rejected
    elif "graphql" in lowered and ("rate limit" in lowered or "rate_limited" in lowered):
        _, rate_limit = _rate_limit_from_probe(rate_limit_result, preferred_resource="graphql")
        cause = "graphql_primary_rate_limited"
        disposition = "retry"
        retryable = True
        fallback_eligible = False
        write_outcome = rejected
    elif "secondary rate" in lowered or "retry-after" in lowered or "abuse detection" in lowered:
        cause = "secondary_rate_limited"
        disposition = "retry"
        retryable = True
        fallback_eligible = False
        write_outcome = rejected
    elif "rate limit" in lowered or "rate_limited" in lowered:
        probed_cause, rate_limit = _rate_limit_from_probe(rate_limit_result)
        cause = probed_cause or "rate_limited_unknown_bucket"
        disposition = "retry"
        retryable = True
        fallback_eligible = False
        write_outcome = rejected
    elif any(text in lowered for text in ("resource not accessible", "forbidden", "permission denied")):
        cause = "permission_denied"
        disposition = "requires_authorization"
        retryable = False
        fallback_eligible = True
        write_outcome = rejected
    elif "invalid character '<'" in lowered or "unexpected character '<'" in lowered:
        cause = "network_provider_failure"
        disposition = "stop" if is_write else "retry"
        retryable = not is_write
        fallback_eligible = False
        write_outcome = unknown if command_started else not_started
        message = "GitHub returned a non-JSON provider response; HTTP status was unavailable because the legacy command omitted --include"
    else:
        cause = "network_provider_failure"
        disposition = "stop" if is_write else "retry"
        retryable = not is_write
        fallback_eligible = False
        write_outcome = unknown if command_started else not_started
    return FailureDetail(
        cause=cause,
        message=message or "GitHub command failed without structured response evidence",
        retryable=retryable,
        fallback_eligible=fallback_eligible,
        disposition=disposition,
        write_outcome=write_outcome,
        rate_limit=rate_limit,
    )


def legacy_process_result(
    returncode: int,
    stdout: str,
    stderr: str,
    *,
    operation: str,
    is_write: bool,
    actor: Optional[str] = None,
    expected_actor: Optional[str] = None,
    host: Optional[str] = None,
    transport: str = "gh_cli",
    bucket: Optional[str] = None,
    graphql_operation: Optional[GraphQLOperation] = None,
    failed_step: str = "gh_invocation",
    completed_steps: Optional[list[str]] = None,
    command_started: bool = True,
    rate_limit_result: Optional[ApiResult] = None,
) -> ApiResult:
    """Convert a completed legacy subprocess into the shared result contract."""
    parsed_body = _try_parse_json(stdout.strip()) if stdout.strip() else None
    outer_completed_steps = list(completed_steps or [])
    if returncode == 0:
        return ApiResult(
            ok=True,
            status=0,
            body=parsed_body,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            host=host or DEFAULT_HOST,
            transport=transport,
            bucket=bucket,
            graphql_operation=graphql_operation,
            completed_steps=outer_completed_steps,
        )
    failure = classify_legacy_failure(
        stderr,
        stdout=stdout,
        is_write=is_write,
        command_started=command_started,
        rate_limit_result=rate_limit_result,
    )
    resolved_completed_steps: list[str] = []
    for step in [*outer_completed_steps, *failure.completed_steps]:
        if step not in resolved_completed_steps:
            resolved_completed_steps.append(step)
    failure.completed_steps = resolved_completed_steps
    failure.failed_step = failure.failed_step or failed_step
    status, headers, body = parse_gh_include_output(stdout) if stdout else (0, {}, None)
    request_id = headers.get("x-github-request-id")
    rate_limit = RateLimitInfo.from_headers(headers)
    provider_resource = rate_limit.resource
    if provider_resource is None and isinstance(failure.rate_limit, dict):
        candidate = failure.rate_limit.get("resource")
        provider_resource = candidate if isinstance(candidate, str) else None
    result_bucket = normalized_provider_bucket(provider_resource) or bucket
    return ApiResult(
        ok=False,
        status=status,
        body=body,
        headers=headers,
        request_id=request_id,
        rate_limit=rate_limit if rate_limit.is_populated() else None,
        failure=failure,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        host=host or DEFAULT_HOST,
        transport=transport,
        bucket=result_bucket,
        graphql_operation=graphql_operation,
        completed_steps=resolved_completed_steps,
        failed_step=failed_step,
    )


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of *headers* with sensitive values replaced by '[REDACTED]'."""
    return {
        k: "[REDACTED]" if k.lower() in _REDACT_HEADER_NAMES else v
        for k, v in headers.items()
    }


def _is_sensitive_body_key(key: str) -> bool:
    normalized = _CAMEL_CASE_BOUNDARY_RE.sub("_", key)
    tokens = [token for token in re.split(r"[^a-z0-9]+", normalized.lower()) if token]
    normalized = "_".join(tokens)
    if normalized in _REDACT_BODY_KEYS:
        return True
    if any(token in {"token", "secret", "password", "credential", "credentials", "authorization"} for token in tokens):
        return True
    token_set = set(tokens)
    return bool(
        {"private", "key"} <= token_set
        or {"api", "key"} <= token_set
        or {"auth", "key"} <= token_set
    )


def redact_body(body: Any, _depth: int = 0) -> Any:
    """Recursively redact sensitive keys from a parsed JSON body."""
    if _depth > 12:
        return "[REDACTED]"
    if isinstance(body, dict):
        return {
            k: "[REDACTED]" if _is_sensitive_body_key(str(k)) else redact_body(v, _depth + 1)
            for k, v in body.items()
        }
    if isinstance(body, list):
        return [redact_body(item, _depth + 1) for item in body]
    if isinstance(body, str):
        return redact_string(body)
    return body


def redact_string(value: str) -> str:
    value = _REDACT_PARAM_RE.sub(r"\1[REDACTED]", value)
    value = _REDACT_TOKEN_RE.sub("[REDACTED]", value)
    return _REDACT_BEARER_RE.sub(r"\1[REDACTED]", value)


def redact_path(path: str) -> str:
    """Redact sensitive query-parameter values from a URL or path string."""
    return redact_string(path)


# ---------------------------------------------------------------------------
# Command construction (body-safe: body goes via stdin, never on command line)
# ---------------------------------------------------------------------------


def build_gh_command(
    method: str,
    path: str,
    *,
    gh_cmd: str = DEFAULT_GH,
    gh_prefix_args: Optional[list[str]] = None,
    api_version: str = DEFAULT_API_VERSION,
    extra_headers: Optional[dict[str, str]] = None,
    has_body: Optional[bool] = None,
) -> list[str]:
    """
    Build a body-safe ``gh api --include`` command list.

    When ``has_body`` is true, ``--input -`` is appended so the caller can
    supply JSON on stdin without exposing it in argv. By default POST, PUT, and
    PATCH carry a body; callers may also provide one for DELETE endpoints that
    require JSON input.

    Args:
        method: HTTP method string ("GET", "POST", …).
        path: API path starting with "/" or a full URL.
        gh_cmd: Path to the gh binary or wrapper (default "gh").
        gh_prefix_args: Wrapper-specific arguments inserted before ``api``.
        extra_headers: Additional -H headers to include in the request.

    Returns:
        Command list ready for subprocess.run.
    """
    if not path.startswith("/") and not path.startswith("http"):
        path = f"/{path}"

    cmd = [
        gh_cmd,
        *(gh_prefix_args or []),
        "api",
        "--method",
        method.upper(),
        "--include",
        "-H",
        f"X-GitHub-Api-Version: {api_version}",
        path,
    ]

    if has_body is None:
        has_body = method.upper() not in ("GET", "HEAD", "DELETE")
    if has_body:
        cmd.extend(["--input", "-"])

    if extra_headers:
        for name, value in extra_headers.items():
            cmd.extend(["-H", f"{name}: {value}"])

    return cmd


_operation_rule_cache: dict[pathlib.Path, tuple[int, dict[str, OperationRetryRule]]] = {}


def reset_operation_rule_cache() -> None:
    _operation_rule_cache.clear()


def operation_retry_rule(
    operation: Optional[str],
    *,
    matrix_path: pathlib.Path = DEFAULT_OPERATION_MATRIX,
) -> tuple[Optional[OperationRetryRule], Optional[str]]:
    if not operation:
        return None, "operation key is missing"
    resolved = matrix_path.resolve()
    try:
        modified = resolved.stat().st_mtime_ns
        cached = _operation_rule_cache.get(resolved)
        if cached is None or cached[0] != modified:
            with resolved.open("rb") as handle:
                data = tomllib.load(handle)
            if data.get("schema_version") != 2 or not isinstance(data.get("operations"), list):
                return None, "operation matrix schema is invalid"
            rules: dict[str, OperationRetryRule] = {}
            for item in data["operations"]:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    continue
                rules[item["id"]] = OperationRetryRule(
                    operation=item["id"],
                    idempotency=str(item.get("idempotency") or "unknown"),
                    retry_eligibility=str(item.get("retry_eligibility") or "manual"),
                    reconciliation_strategy=str(item.get("reconciliation_strategy") or "fail_closed_write"),
                    quota_bucket=str(item.get("quota_bucket") or "unknown"),
                )
            _operation_rule_cache[resolved] = (modified, rules)
        rule = _operation_rule_cache[resolved][1].get(operation)
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError) as exc:
        return None, f"operation matrix could not be loaded ({type(exc).__name__})"
    if rule is None:
        return None, f"operation '{operation}' is absent from the accepted matrix"
    return rule, None


def default_retry_policy() -> RetryPolicy:
    return RetryPolicy.from_env()


def default_retry_runtime() -> RetryRuntime:
    return RetryRuntime()


def remaining_retry_timeout_seconds(
    *,
    retry_policy: Optional[RetryPolicy] = None,
    retry_runtime: Optional[RetryRuntime] = None,
    deadline_at: Optional[float] = None,
) -> float:
    policy = retry_policy or default_retry_policy()
    runtime = retry_runtime or default_retry_runtime()
    now = runtime.now()
    inherited_deadline = deadline_at if deadline_at is not None else _env_optional_float(
        "GITHUB_RETRY_DEADLINE_AT"
    )
    effective_deadline = min(
        now + max(0.0, policy.max_wait_seconds),
        inherited_deadline if inherited_deadline is not None else float("inf"),
    )
    return max(0.0, effective_deadline - now)


@dataclass
class _CooldownLease:
    key: str
    handle: Any
    state_path: pathlib.Path


class SharedCooldownStore:
    def __init__(self, state_dir: pathlib.Path) -> None:
        self.state_dir = state_dir

    def _paths(self, key: str) -> tuple[pathlib.Path, pathlib.Path]:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.state_dir / f"{digest}.lock", self.state_dir / f"{digest}.json"

    def _prepare(self) -> None:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.state_dir.chmod(0o700)
        except OSError:
            pass

    def _open_lock(self, key: str, *, blocking: bool) -> Optional[_CooldownLease]:
        self._prepare()
        lock_path, state_path = self._paths(key)
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            lock_path.chmod(0o600)
        except OSError:
            pass
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError:
            handle.close()
            return None
        except OSError:
            handle.close()
            raise
        return _CooldownLease(key=key, handle=handle, state_path=state_path)

    def _read_state(
        self,
        lease: _CooldownLease,
        *,
        now: float,
        stale_after_seconds: float,
    ) -> Optional[dict[str, Any]]:
        try:
            raw = json.loads(lease.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            try:
                lease.state_path.unlink()
            except FileNotFoundError:
                pass
            return None
        if not isinstance(raw, dict):
            try:
                lease.state_path.unlink()
            except FileNotFoundError:
                pass
            return None
        try:
            updated_at = float(raw.get("updated_at") or 0.0)
            expires_at = float(raw.get("expires_at") or 0.0)
            ready_at = float(raw.get("ready_at") or 0.0)
        except (TypeError, ValueError):
            try:
                lease.state_path.unlink()
            except FileNotFoundError:
                pass
            return None
        if expires_at <= now or updated_at + stale_after_seconds <= now:
            try:
                lease.state_path.unlink()
            except FileNotFoundError:
                pass
            return None
        raw["ready_at"] = ready_at
        return raw

    def _write_state(self, lease: _CooldownLease, state: dict[str, Any]) -> None:
        self._prepare()
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.state_dir,
            prefix=f".{lease.state_path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
            temporary = pathlib.Path(handle.name)
        try:
            temporary.chmod(0o600)
            os.replace(temporary, lease.state_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def release(self, lease: Optional[_CooldownLease]) -> None:
        if lease is None:
            return
        try:
            fcntl.flock(lease.handle.fileno(), fcntl.LOCK_UN)
        finally:
            lease.handle.close()

    def claim(
        self,
        key: str,
        *,
        now: float,
        policy: RetryPolicy,
    ) -> tuple[Optional[_CooldownLease], Optional[float], bool]:
        lease = self._open_lock(key, blocking=False)
        if lease is None:
            return None, now + policy.lock_poll_seconds, True
        try:
            state = self._read_state(lease, now=now, stale_after_seconds=policy.stale_after_seconds)
        except OSError:
            self.release(lease)
            raise
        if state is None:
            self.release(lease)
            return None, None, False
        ready_at = float(state.get("ready_at") or 0.0)
        if ready_at > now:
            self.release(lease)
            return None, ready_at, True
        return lease, None, True

    def publish(
        self,
        key: str,
        *,
        ready_at: float,
        cause: str,
        now: float,
        policy: RetryPolicy,
        lease: Optional[_CooldownLease] = None,
        deadline: Optional[float] = None,
        runtime: Optional[RetryRuntime] = None,
    ) -> Optional[str]:
        if lease is not None and lease.key != key:
            self.release(lease)
            lease = None
        active = lease
        if active is None and deadline is None:
            active = self._open_lock(key, blocking=True)
        while active is None and deadline is not None and runtime is not None:
            if runtime.cancelled():
                return "cancelled"
            current = runtime.now()
            if current >= deadline:
                return "deadline_exceeded"
            active = self._open_lock(key, blocking=False)
            if active is not None:
                break
            duration = min(policy.lock_poll_seconds, deadline - current)
            if duration <= 0:
                return "deadline_exceeded"
            try:
                runtime.sleep(duration)
            except KeyboardInterrupt:
                return "cancelled"
        if active is None:
            return "deadline_exceeded"
        written_at = runtime.now() if runtime is not None else now
        try:
            existing = self._read_state(
                active,
                now=written_at,
                stale_after_seconds=policy.stale_after_seconds,
            )
            existing_ready_at = float(existing.get("ready_at") or 0.0) if existing else 0.0
            published_ready_at = max(ready_at, existing_ready_at)
            published_cause = (
                str(existing.get("cause") or cause)
                if existing is not None and existing_ready_at > ready_at
                else cause
            )
            self._write_state(
                active,
                {
                    "schema_version": 1,
                    "key": key,
                    "cause": published_cause,
                    "ready_at": published_ready_at,
                    "updated_at": written_at,
                    "expires_at": max(published_ready_at, written_at) + policy.stale_after_seconds,
                },
            )
        finally:
            self.release(active)
        return None

    def finish(
        self,
        *,
        now: float,
        policy: RetryPolicy,
        lease: Optional[_CooldownLease],
    ) -> None:
        if lease is None:
            return
        drain = max(0.0, policy.drain_seconds)
        try:
            self._write_state(
                lease,
                {
                    "schema_version": 1,
                    "key": lease.key,
                    "cause": "post_cooldown_drain",
                    "ready_at": now + drain,
                    "updated_at": now,
                    "expires_at": now + max(5.0, drain * 4.0),
                },
            )
        finally:
            self.release(lease)


def _cooldown_key(host: str, actor: Optional[str], bucket: str) -> str:
    return json.dumps(
        {
            "host": host.casefold(),
            "actor": (actor or "unresolved").casefold(),
            "bucket": bucket.casefold(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _retry_target(
    result: ApiResult,
    *,
    attempt: int,
    now: float,
    deadline: float,
    policy: RetryPolicy,
    runtime: RetryRuntime,
) -> float:
    failure = result.failure
    cause = failure.cause if failure else "unknown"
    rate_limit = result.rate_limit.as_dict() if result.rate_limit and result.rate_limit.is_populated() else {}
    if not rate_limit and failure and failure.rate_limit:
        rate_limit = failure.rate_limit
    reset = rate_limit.get("reset")
    retry_after = rate_limit.get("retry_after")
    if cause in {
        "rest_primary_rate_limited",
        "graphql_primary_rate_limited",
        "rate_limited_unknown_bucket",
    } and isinstance(reset, (int, float)):
        base = max(now, float(reset))
    elif isinstance(retry_after, (int, float)):
        base = now + max(0.0, float(retry_after))
    elif cause == "secondary_rate_limited" and isinstance(reset, (int, float)):
        base = max(now, float(reset))
    else:
        exponent = max(0, attempt - 1)
        base = now + min(
            max(policy.base_backoff_seconds, 0.0) * (2 ** exponent),
            max(policy.max_backoff_seconds, policy.base_backoff_seconds, 0.0),
        )
    try:
        raw_jitter = float(runtime.jitter(policy.jitter_seconds))
    except (TypeError, ValueError):
        raw_jitter = 0.0
    jitter_limit = min(
        max(0.0, policy.jitter_seconds),
        max(0.0, deadline - base - 0.001),
    )
    jitter = (
        min(max(0.0, raw_jitter), jitter_limit)
        if math.isfinite(raw_jitter)
        else 0.0
    )
    return base + jitter


def _wait_until(
    target: float,
    *,
    deadline: float,
    operation: str,
    cause: str,
    attempt: int,
    policy: RetryPolicy,
    runtime: RetryRuntime,
) -> tuple[bool, float, Optional[str]]:
    now = runtime.now()
    if target > deadline:
        return False, 0.0, "deadline_exceeded"
    if runtime.cancelled():
        return False, 0.0, "cancelled"
    elapsed = 0.0
    next_progress = now
    should_report = target - now >= policy.progress_interval_seconds > 0
    while now < target:
        if runtime.cancelled():
            return False, elapsed, "cancelled"
        if now >= deadline:
            return False, elapsed, "deadline_exceeded"
        if should_report and now >= next_progress:
            runtime.progress(
                {
                    "operation": operation,
                    "cause": cause,
                    "attempt": attempt,
                    "max_attempts": policy.max_attempts,
                    "remaining_seconds": target - now,
                    "deadline": deadline,
                }
            )
            next_progress = now + policy.progress_interval_seconds
        duration = min(policy.wait_slice_seconds, target - now, deadline - now)
        if duration <= 0:
            return False, elapsed, "deadline_exceeded"
        try:
            runtime.sleep(duration)
        except KeyboardInterrupt:
            return False, elapsed, "cancelled"
        elapsed += duration
        updated = runtime.now()
        now = updated if updated > now else now + duration
    return True, elapsed, None


def _retry_failure_result(
    result: ApiResult,
    *,
    cause: str,
    message: str,
    is_write: bool,
) -> ApiResult:
    previous = result.failure
    result.ok = False
    result.failure = FailureDetail(
        cause=cause,
        message=message,
        retryable=False,
        fallback_eligible=False,
        disposition="stop",
        write_outcome=previous.write_outcome if previous else ("unknown" if is_write else None),
        completed_steps=list(result.completed_steps),
        failed_step=result.failed_step or "retry_wait",
        request_id=result.request_id,
        rate_limit=(
            result.rate_limit.as_dict()
            if result.rate_limit and result.rate_limit.is_populated()
            else previous.rate_limit if previous else None
        ),
    )
    return result


def _local_retry_failure(
    *,
    operation: str,
    actor: Optional[str],
    expected_actor: Optional[str],
    host: str,
    bucket: str,
    is_write: bool,
    cause: str,
    message: str,
    retry_at: Optional[float] = None,
) -> ApiResult:
    rate_limit = RateLimitInfo(reset=int(retry_at), resource=bucket) if retry_at is not None else None
    failure = FailureDetail(
        cause=cause,
        message=message,
        retryable=False,
        fallback_eligible=False,
        disposition="stop",
        write_outcome="not_started" if is_write else None,
        failed_step="retry_wait",
        rate_limit=rate_limit.as_dict() if rate_limit else None,
    )
    return ApiResult(
        ok=False,
        status=0,
        body=None,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        host=host,
        bucket=bucket,
        rate_limit=rate_limit,
        failed_step="retry_wait",
        failure=failure,
    )


def subprocess_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def subprocess_timeout_result(
    *,
    operation: str,
    is_write: bool,
    actor: Optional[str],
    expected_actor: Optional[str],
    host: Optional[str],
    transport: str,
    bucket: str,
    graphql_operation: Optional[GraphQLOperation] = None,
    completed_steps: Optional[list[str]] = None,
    failed_step: str = "subprocess_timeout",
    stderr: Any = None,
) -> ApiResult:
    steps = list(completed_steps or [])
    timeout_stderr = subprocess_output_text(stderr)
    reported_actor = actor_from_gh_stderr(timeout_stderr)
    authorized_actor_change = active_fallback_was_authorized(timeout_stderr)
    resolved_actor = reported_actor or actor
    resolved_expected_actor = None if authorized_actor_change else expected_actor
    failure = FailureDetail(
        cause="deadline_exceeded",
        message="GitHub CLI call exceeded the effective retry deadline",
        retryable=False,
        fallback_eligible=False,
        disposition="stop",
        write_outcome="unknown" if is_write else None,
        completed_steps=steps,
        failed_step=failed_step,
    )
    return ApiResult(
        ok=False,
        status=0,
        body=None,
        operation=operation,
        actor=resolved_actor,
        expected_actor=resolved_expected_actor,
        host=host or DEFAULT_HOST,
        transport=transport,
        bucket=bucket,
        graphql_operation=graphql_operation,
        completed_steps=steps,
        failed_step=failed_step,
        failure=failure,
    )


def _outcome_certainty(
    result: ApiResult,
    *,
    is_write: bool,
    reconciliation: Optional[dict[str, Any]],
) -> str:
    if result.ok:
        return "reconciled_applied" if reconciliation and reconciliation.get("result") == "matched" else "confirmed"
    if not is_write:
        return "not_applicable"
    write_outcome = result.failure.write_outcome if result.failure else None
    if write_outcome in {"not_started", "rejected"}:
        return "confirmed_not_applied"
    if reconciliation and reconciliation.get("result") == "no_match":
        return "reconciled_not_applied"
    return "unknown"


def _attach_retry_summary(
    result: ApiResult,
    *,
    attempts: int,
    elapsed_wait: float,
    retry_eligible: bool,
    actor: Optional[str],
    bucket: str,
    is_write: bool,
    reconciliation: Optional[dict[str, Any]],
    recommended_next_action: str,
    effective_deadline: float,
    exhausted_reason: Optional[str] = None,
) -> ApiResult:
    result.retry_summary = RetrySummary(
        attempts=attempts,
        elapsed_wait=elapsed_wait,
        retry_eligible=retry_eligible,
        last_actor=result.actor or actor,
        last_bucket=result.bucket or bucket,
        outcome_certainty=_outcome_certainty(
            result,
            is_write=is_write,
            reconciliation=reconciliation,
        ),
        reconciliation=reconciliation,
        recommended_next_action=recommended_next_action,
        effective_deadline=effective_deadline,
        exhausted_reason=exhausted_reason,
    )
    return result


def run_with_retry(
    attempt_call: Callable[[], ApiResult],
    *,
    operation: str,
    is_write: bool,
    actor: Optional[str] = None,
    expected_actor: Optional[str] = None,
    host: Optional[str] = None,
    bucket: Optional[str] = None,
    reconcile: Optional[
        Callable[[ApiResult, ReconciliationContext], ReconciliationDecision]
    ] = None,
    retry_policy: Optional[RetryPolicy] = None,
    retry_runtime: Optional[RetryRuntime] = None,
    deadline_at: Optional[float] = None,
    matrix_path: pathlib.Path = DEFAULT_OPERATION_MATRIX,
    attempt_with_timeout: Optional[Callable[[float], ApiResult]] = None,
) -> ApiResult:
    policy = retry_policy or default_retry_policy()
    runtime = retry_runtime or default_retry_runtime()
    resolved_host = host or DEFAULT_HOST
    resolved_bucket = bucket or "unknown"
    started_at = runtime.now()
    inherited_deadline = deadline_at if deadline_at is not None else _env_optional_float(
        "GITHUB_RETRY_DEADLINE_AT"
    )
    effective_deadline = min(
        started_at + max(0.0, policy.max_wait_seconds),
        inherited_deadline if inherited_deadline is not None else float("inf"),
    )
    rule, rule_error = operation_retry_rule(operation, matrix_path=matrix_path)
    if bucket is None and rule is not None:
        resolved_bucket = rule.quota_bucket
    eligible = rule is not None and rule.retry_eligibility in {"safe", "conditional"}
    initial_reason = "cancelled" if runtime.cancelled() else (
        "deadline_exceeded" if runtime.now() >= effective_deadline else None
    )
    if initial_reason is not None:
        result = _local_retry_failure(
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            host=resolved_host,
            bucket=resolved_bucket,
            is_write=is_write,
            cause=initial_reason,
            message=(
                "GitHub operation was cancelled before the first attempt"
                if initial_reason == "cancelled"
                else "GitHub retry deadline expired before the first attempt"
            ),
        )
        return _attach_retry_summary(
            result,
            attempts=0,
            elapsed_wait=0.0,
            retry_eligible=eligible,
            actor=actor,
            bucket=resolved_bucket,
            is_write=is_write,
            reconciliation=None,
            recommended_next_action=(
                "rerun_when_ready" if initial_reason == "cancelled" else "retry_after_reported_reset"
            ),
            effective_deadline=effective_deadline,
            exhausted_reason=initial_reason,
        )

    def execute_attempt() -> ApiResult:
        if attempt_with_timeout is None:
            return attempt_call()
        remaining = max(0.0, effective_deadline - runtime.now())
        return attempt_with_timeout(remaining)

    if not eligible:
        result = execute_attempt()
        context_actor = expected_actor or actor
        if (
            result.actor
            and context_actor
            and result.actor.casefold() != context_actor.casefold()
            and result.expected_actor is not None
        ):
            result = _retry_failure_result(
                result,
                cause="actor_mismatch",
                message=(
                    f"GitHub actor changed during operation context from '{context_actor}' "
                    f"to '{result.actor}'"
                ),
                is_write=is_write,
            )
            return _attach_retry_summary(
                result,
                attempts=1,
                elapsed_wait=0.0,
                retry_eligible=False,
                actor=context_actor,
                bucket=resolved_bucket,
                is_write=is_write,
                reconciliation=None,
                recommended_next_action="start_new_authorized_actor_context",
                effective_deadline=effective_deadline,
                exhausted_reason="actor_changed",
            )
        if result.bucket and result.bucket.casefold() != resolved_bucket.casefold():
            if resolved_bucket.casefold() in FLEXIBLE_RETRY_BUCKETS:
                resolved_bucket = result.bucket
            else:
                result = _retry_failure_result(
                    result,
                    cause="retry_context_changed",
                    message=(
                        f"GitHub quota bucket changed during operation context from '{resolved_bucket}' "
                        f"to '{result.bucket}'"
                    ),
                    is_write=is_write,
                )
                return _attach_retry_summary(
                    result,
                    attempts=1,
                    elapsed_wait=0.0,
                    retry_eligible=False,
                    actor=context_actor or actor,
                    bucket=resolved_bucket,
                    is_write=is_write,
                    reconciliation=None,
                    recommended_next_action="start_new_bucket_context",
                    effective_deadline=effective_deadline,
                    exhausted_reason="bucket_changed",
                )
        if result.failure is not None:
            result.failure.disposition = "stop"
        recommendation = (
            "add_operation_to_matrix_before_retrying"
            if rule is None
            else "follow_operation_reconciliation_strategy"
        )
        if rule_error and result.failure is not None:
            result.failure.message = f"{result.failure.message}; automatic retry disabled: {rule_error}"
        return _attach_retry_summary(
            result,
            attempts=1,
            elapsed_wait=0.0,
            retry_eligible=False,
            actor=actor,
            bucket=resolved_bucket,
            is_write=is_write,
            reconciliation=None,
            recommended_next_action="none" if result.ok else recommendation,
            effective_deadline=effective_deadline,
        )

    store = SharedCooldownStore(policy.state_dir)
    attempts = 0
    elapsed_wait = 0.0
    reconciliation: Optional[dict[str, Any]] = None
    context_actor = expected_actor
    last_result: Optional[ApiResult] = None
    cooldown_error: Optional[str] = None

    def finish_cooldown(lease: Optional[_CooldownLease]) -> None:
        try:
            store.finish(now=runtime.now(), policy=policy, lease=lease)
        except OSError:
            pass

    def release_cooldown(lease: Optional[_CooldownLease]) -> None:
        try:
            store.release(lease)
        except OSError:
            pass

    while True:
        key_actor = context_actor or actor
        key = _cooldown_key(resolved_host, key_actor, resolved_bucket)
        try:
            lease, wait_target, coordinated = store.claim(key, now=runtime.now(), policy=policy)
        except OSError as exc:
            lease, wait_target, coordinated = None, None, False
            cooldown_error = f"shared cooldown state is unavailable ({type(exc).__name__})"
        if wait_target is not None:
            completed, waited, reason = _wait_until(
                wait_target,
                deadline=effective_deadline,
                operation=operation,
                cause="shared_cooldown" if coordinated else "retry_backoff",
                attempt=max(1, attempts + 1),
                policy=policy,
                runtime=runtime,
            )
            elapsed_wait += waited
            if not completed:
                result = last_result or _local_retry_failure(
                    operation=operation,
                    actor=context_actor or actor,
                    expected_actor=expected_actor,
                    host=resolved_host,
                    bucket=resolved_bucket,
                    is_write=is_write,
                    cause=reason or "deadline_exceeded",
                    message="GitHub retry wait did not complete before the effective deadline",
                    retry_at=wait_target,
                )
                if last_result is not None:
                    result = _retry_failure_result(
                        result,
                        cause=reason or "deadline_exceeded",
                        message="GitHub retry wait did not complete before the effective deadline",
                        is_write=is_write,
                    )
                return _attach_retry_summary(
                    result,
                    attempts=attempts,
                    elapsed_wait=elapsed_wait,
                    retry_eligible=True,
                    actor=context_actor or actor,
                    bucket=resolved_bucket,
                    is_write=is_write,
                    reconciliation=reconciliation,
                    recommended_next_action=(
                        "rerun_when_ready" if reason == "cancelled" else "retry_after_reported_reset"
                    ),
                    effective_deadline=effective_deadline,
                    exhausted_reason=reason,
                )
            continue

        pre_attempt_reason = "cancelled" if runtime.cancelled() else (
            "deadline_exceeded" if runtime.now() >= effective_deadline else None
        )
        if pre_attempt_reason is not None:
            release_cooldown(lease)
            result = last_result or _local_retry_failure(
                operation=operation,
                actor=context_actor or actor,
                expected_actor=expected_actor,
                host=resolved_host,
                bucket=resolved_bucket,
                is_write=is_write,
                cause=pre_attempt_reason,
                message=(
                    "GitHub operation was cancelled before the next attempt"
                    if pre_attempt_reason == "cancelled"
                    else "GitHub retry deadline expired before the next attempt"
                ),
            )
            result = _retry_failure_result(
                result,
                cause=pre_attempt_reason,
                message=(
                    "GitHub operation was cancelled before the next attempt"
                    if pre_attempt_reason == "cancelled"
                    else "GitHub retry deadline expired before the next attempt"
                ),
                is_write=is_write,
            )
            return _attach_retry_summary(
                result,
                attempts=attempts,
                elapsed_wait=elapsed_wait,
                retry_eligible=True,
                actor=context_actor or actor,
                bucket=resolved_bucket,
                is_write=is_write,
                reconciliation=reconciliation,
                recommended_next_action=(
                    "rerun_when_ready"
                    if pre_attempt_reason == "cancelled"
                    else "retry_after_reported_reset"
                ),
                effective_deadline=effective_deadline,
                exhausted_reason=pre_attempt_reason,
            )

        attempts += 1
        result = execute_attempt()
        last_result = result
        actual_actor = result.actor
        if context_actor is None and actual_actor:
            context_actor = actual_actor
        elif actual_actor and context_actor and actual_actor.casefold() != context_actor.casefold():
            if attempts == 1 and result.expected_actor is None:
                context_actor = actual_actor
            else:
                finish_cooldown(lease)
                result = _retry_failure_result(
                    result,
                    cause="actor_mismatch",
                    message=(
                        f"GitHub actor changed during retry context from '{context_actor}' "
                        f"to '{actual_actor}'"
                    ),
                    is_write=is_write,
                )
                return _attach_retry_summary(
                    result,
                    attempts=attempts,
                    elapsed_wait=elapsed_wait,
                    retry_eligible=False,
                    actor=context_actor,
                    bucket=resolved_bucket,
                    is_write=is_write,
                    reconciliation=reconciliation,
                    recommended_next_action="start_new_authorized_actor_context",
                    effective_deadline=effective_deadline,
                    exhausted_reason="actor_changed",
                )
        if result.bucket and result.bucket.casefold() != resolved_bucket.casefold():
            if resolved_bucket.casefold() in FLEXIBLE_RETRY_BUCKETS and attempts == 1:
                resolved_bucket = result.bucket
                release_cooldown(lease)
                lease = None
            else:
                finish_cooldown(lease)
                result = _retry_failure_result(
                    result,
                    cause="retry_context_changed",
                    message=(
                        f"GitHub quota bucket changed during retry context from '{resolved_bucket}' "
                        f"to '{result.bucket}'"
                    ),
                    is_write=is_write,
                )
                return _attach_retry_summary(
                    result,
                    attempts=attempts,
                    elapsed_wait=elapsed_wait,
                    retry_eligible=False,
                    actor=context_actor or actor,
                    bucket=resolved_bucket,
                    is_write=is_write,
                    reconciliation=reconciliation,
                    recommended_next_action="start_new_bucket_context",
                    effective_deadline=effective_deadline,
                    exhausted_reason="bucket_changed",
                )
        if result.ok:
            finish_cooldown(lease)
            return _attach_retry_summary(
                result,
                attempts=attempts,
                elapsed_wait=elapsed_wait,
                retry_eligible=True,
                actor=context_actor or actor,
                bucket=resolved_bucket,
                is_write=is_write,
                reconciliation=reconciliation,
                recommended_next_action="none",
                effective_deadline=effective_deadline,
            )

        failure = result.failure
        can_retry = bool(failure and failure.retryable and not is_write)
        if is_write and failure and failure.write_outcome in {"not_started", "rejected"}:
            can_retry = failure.retryable
        if is_write and failure and failure.write_outcome == "unknown":
            can_retry = (
                rule.idempotency == "idempotent"
                and failure.cause in {
                    "network_provider_failure",
                    "deadline_exceeded",
                    "graphql_primary_rate_limited",
                    "rest_primary_rate_limited",
                    "rate_limited_unknown_bucket",
                }
            )
            if reconcile is not None:
                release_cooldown(lease)
                lease = None
                reconciliation_stop = "cancelled" if runtime.cancelled() else (
                    "deadline_exceeded" if runtime.now() >= effective_deadline else None
                )
                if reconciliation_stop is not None:
                    finish_cooldown(lease)
                    reconciliation = {
                        "strategy": rule.reconciliation_strategy,
                        "result": "failed",
                        "failure": {
                            "cause": reconciliation_stop,
                            "failed_step": "reconciliation",
                        },
                    }
                    result = _retry_failure_result(
                        result,
                        cause=reconciliation_stop,
                        message=(
                            "GitHub write outcome could not be reconciled before cancellation"
                            if reconciliation_stop == "cancelled"
                            else "GitHub write outcome could not be reconciled before the effective deadline"
                        ),
                        is_write=is_write,
                    )
                    return _attach_retry_summary(
                        result,
                        attempts=attempts,
                        elapsed_wait=elapsed_wait,
                        retry_eligible=True,
                        actor=context_actor or actor,
                        bucket=resolved_bucket,
                        is_write=is_write,
                        reconciliation=reconciliation,
                        recommended_next_action="reconcile_or_retry_manually",
                        effective_deadline=effective_deadline,
                        exhausted_reason=reconciliation_stop,
                    )
                try:
                    decision = reconcile(
                        result,
                        ReconciliationContext(
                            deadline_at=effective_deadline,
                            retry_policy=policy,
                            retry_runtime=runtime,
                        ),
                    )
                except Exception as exc:
                    decision = ReconciliationDecision(
                        "failed",
                        details={"error": f"reconciliation failed ({type(exc).__name__})"},
                    )
                if not isinstance(decision, ReconciliationDecision):
                    decision = ReconciliationDecision(
                        "failed",
                        details={"error": "reconciliation returned an invalid decision"},
                    )
                reconciliation = {
                    "strategy": rule.reconciliation_strategy,
                    "result": decision.outcome,
                    **redact_body(decision.details),
                }
                if decision.outcome == "matched":
                    result.ok = True
                    result.status = result.status if 200 <= result.status < 300 else 200
                    result.body = decision.body
                    result.failure = None
                    finish_cooldown(lease)
                    return _attach_retry_summary(
                        result,
                        attempts=attempts,
                        elapsed_wait=elapsed_wait,
                        retry_eligible=True,
                        actor=context_actor or actor,
                        bucket=resolved_bucket,
                        is_write=is_write,
                        reconciliation=reconciliation,
                        recommended_next_action="none",
                        effective_deadline=effective_deadline,
                    )
                can_retry = decision.outcome == "no_match" and rule.idempotency != "non_idempotent"

        if not can_retry:
            finish_cooldown(lease)
            if result.failure is not None:
                result.failure.disposition = "stop"
            return _attach_retry_summary(
                result,
                attempts=attempts,
                elapsed_wait=elapsed_wait,
                retry_eligible=False,
                actor=context_actor or actor,
                bucket=resolved_bucket,
                is_write=is_write,
                reconciliation=reconciliation,
                recommended_next_action=(
                    "reconcile_or_retry_manually"
                    if is_write and failure and failure.write_outcome == "unknown"
                    else "inspect_last_failure"
                ),
                effective_deadline=effective_deadline,
                exhausted_reason="not_retryable",
            )
        if cooldown_error is not None:
            finish_cooldown(lease)
            if result.failure is not None:
                result.failure.disposition = "stop"
            return _attach_retry_summary(
                result,
                attempts=attempts,
                elapsed_wait=elapsed_wait,
                retry_eligible=False,
                actor=context_actor or actor,
                bucket=resolved_bucket,
                is_write=is_write,
                reconciliation=reconciliation,
                recommended_next_action="repair_retry_state_directory",
                effective_deadline=effective_deadline,
                exhausted_reason="cooldown_state_unavailable",
            )
        if attempts >= policy.max_attempts:
            finish_cooldown(lease)
            if result.failure is not None:
                result.failure.disposition = "stop"
            return _attach_retry_summary(
                result,
                attempts=attempts,
                elapsed_wait=elapsed_wait,
                retry_eligible=True,
                actor=context_actor or actor,
                bucket=resolved_bucket,
                is_write=is_write,
                reconciliation=reconciliation,
                recommended_next_action="inspect_last_failure",
                effective_deadline=effective_deadline,
                exhausted_reason="attempt_budget",
            )

        now = runtime.now()
        target = _retry_target(
            result,
            attempt=attempts,
            now=now,
            deadline=effective_deadline,
            policy=policy,
            runtime=runtime,
        )
        publish_actor = context_actor or actual_actor or actor
        publish_key = _cooldown_key(resolved_host, publish_actor, resolved_bucket)
        try:
            publish_stop = store.publish(
                publish_key,
                ready_at=target,
                cause=failure.cause if failure else "retryable_failure",
                now=now,
                policy=policy,
                lease=lease,
                deadline=effective_deadline,
                runtime=runtime,
            )
        except OSError:
            if result.failure is not None:
                result.failure.disposition = "stop"
            return _attach_retry_summary(
                result,
                attempts=attempts,
                elapsed_wait=elapsed_wait,
                retry_eligible=False,
                actor=context_actor or actor,
                bucket=resolved_bucket,
                is_write=is_write,
                reconciliation=reconciliation,
                recommended_next_action="repair_retry_state_directory",
                effective_deadline=effective_deadline,
                exhausted_reason="cooldown_state_unavailable",
            )
        if publish_stop is not None:
            result = _retry_failure_result(
                result,
                cause=publish_stop,
                message=(
                    "GitHub retry coordination was cancelled before cooldown publication"
                    if publish_stop == "cancelled"
                    else "GitHub retry deadline expired before cooldown publication"
                ),
                is_write=is_write,
            )
            return _attach_retry_summary(
                result,
                attempts=attempts,
                elapsed_wait=elapsed_wait,
                retry_eligible=True,
                actor=context_actor or actor,
                bucket=resolved_bucket,
                is_write=is_write,
                reconciliation=reconciliation,
                recommended_next_action=(
                    "rerun_when_ready" if publish_stop == "cancelled" else "retry_after_reported_reset"
                ),
                effective_deadline=effective_deadline,
                exhausted_reason=publish_stop,
            )
        completed, waited, reason = _wait_until(
            target,
            deadline=effective_deadline,
            operation=operation,
            cause=failure.cause if failure else "retryable_failure",
            attempt=attempts + 1,
            policy=policy,
            runtime=runtime,
        )
        elapsed_wait += waited
        if not completed:
            result = _retry_failure_result(
                result,
                cause=reason or "deadline_exceeded",
                message="GitHub retry wait did not complete before the effective deadline",
                is_write=is_write,
            )
            return _attach_retry_summary(
                result,
                attempts=attempts,
                elapsed_wait=elapsed_wait,
                retry_eligible=True,
                actor=context_actor or actor,
                bucket=resolved_bucket,
                is_write=is_write,
                reconciliation=reconciliation,
                recommended_next_action=(
                    "rerun_when_ready" if reason == "cancelled" else "retry_after_reported_reset"
                ),
                effective_deadline=effective_deadline,
                exhausted_reason=reason,
            )


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def call_gh(
    method: str,
    path: str,
    body: Any = None,
    *,
    gh_cmd: str = DEFAULT_GH,
    gh_prefix_args: Optional[list[str]] = None,
    api_version: str = DEFAULT_API_VERSION,
    extra_headers: Optional[dict[str, str]] = None,
    completed_steps: Optional[list[str]] = None,
    failed_step: Optional[str] = None,
    is_write: Optional[bool] = None,
    operation: Optional[str] = None,
    actor: Optional[str] = None,
    expected_actor: Optional[str] = None,
    host: Optional[str] = None,
    bucket: Optional[str] = None,
    graphql_operation: Optional[GraphQLOperation] = None,
    timeout_seconds: Optional[float] = None,
) -> ApiResult:
    """
    Execute a single GitHub REST API call via gh CLI.

    The request body (if any) is serialised to JSON and sent via stdin.
    The response is parsed from ``gh api --include`` stdout.

    Args:
        method: HTTP method ("GET", "POST", "PUT", "PATCH", "DELETE").
        path: API path (e.g. "/repos/owner/repo/pulls") or full URL.
        body: Python object to serialise as the JSON request body.
        gh_cmd: Path to the gh binary or wrapper script.
        gh_prefix_args: Wrapper-specific arguments inserted before ``api``.
        extra_headers: Additional headers to pass as ``-H name: value``.
        completed_steps: Steps already done before this call (for partial-success tracking).
        failed_step: Label for the step this call represents (appended to FailureDetail on error).
        is_write: Override write detection (default: True for POST/PUT/PATCH/DELETE).

    Returns:
        ApiResult with ok=True on 2xx, ok=False with a populated FailureDetail otherwise.
    """
    if completed_steps is None:
        completed_steps = []
    resolved_graphql_operation = graphql_operation
    if is_graphql_path(path) and resolved_graphql_operation is None:
        resolved_graphql_operation = infer_graphql_operation_type(body)
    is_write = infer_is_write(
        method,
        path,
        body,
        explicit_is_write=is_write,
        graphql_operation=resolved_graphql_operation,
    )
    resolved_bucket = bucket or infer_api_bucket(path)
    resolved_host = host or DEFAULT_HOST

    if expected_actor and actor and expected_actor.casefold() != actor.casefold():
        failure = classify_error(
            0,
            {},
            None,
            is_write=is_write,
            expected_actor=expected_actor,
            actual_actor=actor,
        )
        failure.completed_steps = completed_steps
        failure.failed_step = failed_step or "actor_verification"
        return ApiResult(
            ok=False,
            status=0,
            body=None,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            host=resolved_host,
            bucket=resolved_bucket,
            graphql_operation=resolved_graphql_operation,
            completed_steps=completed_steps,
            failed_step=failure.failed_step,
            failure=failure,
        )

    has_body = body is not None or method.upper() in ("POST", "PUT", "PATCH")
    cmd = build_gh_command(
        method,
        path,
        gh_cmd=gh_cmd,
        gh_prefix_args=gh_prefix_args,
        api_version=api_version,
        extra_headers=extra_headers,
        has_body=has_body,
    )

    stdin_bytes: Optional[bytes] = None
    if has_body:
        stdin_bytes = json.dumps(body if body is not None else {}).encode()

    try:
        proc = subprocess.run(
            cmd,
            input=stdin_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess_timeout_result(
            operation=operation or "github.api.call",
            is_write=is_write,
            actor=actor,
            expected_actor=expected_actor,
            host=resolved_host,
            transport=TRANSPORT,
            bucket=resolved_bucket,
            graphql_operation=resolved_graphql_operation,
            completed_steps=completed_steps,
            failed_step=failed_step or "subprocess_timeout",
            stderr=exc.stderr,
        )
    except (FileNotFoundError, PermissionError) as exc:
        return ApiResult(
            ok=False,
            status=0,
            body=None,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            host=resolved_host,
            bucket=resolved_bucket,
            graphql_operation=resolved_graphql_operation,
            completed_steps=completed_steps,
            failed_step=failed_step or "subprocess_launch",
            failure=FailureDetail(
                cause="network_provider_failure",
                message=f"Failed to launch GitHub CLI ({type(exc).__name__})",
                retryable=True,
                fallback_eligible=False,
                disposition="retry",
                write_outcome="not_started" if is_write else None,
                completed_steps=completed_steps,
                failed_step=failed_step or "subprocess_launch",
            ),
        )
    except OSError as exc:
        return ApiResult(
            ok=False,
            status=0,
            body=None,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            host=resolved_host,
            bucket=resolved_bucket,
            graphql_operation=resolved_graphql_operation,
            completed_steps=completed_steps,
            failed_step=failed_step or "subprocess_execution",
            failure=FailureDetail(
                cause="network_provider_failure",
                message=f"GitHub CLI execution failed ({type(exc).__name__})",
                retryable=not is_write,
                fallback_eligible=False,
                disposition="retry" if not is_write else "stop",
                write_outcome="unknown" if is_write else None,
                completed_steps=completed_steps,
                failed_step=failed_step or "subprocess_execution",
            ),
        )

    raw_stdout = proc.stdout.decode("utf-8", errors="replace")
    raw_stderr = proc.stderr.decode("utf-8", errors="replace").strip()
    reported_actor = actor_from_gh_stderr(raw_stderr)
    if reported_actor:
        actor = reported_actor
    authorized_actor_change = active_fallback_was_authorized(raw_stderr)
    if expected_actor and actor and expected_actor.casefold() != actor.casefold() and not authorized_actor_change:
        failure = classify_error(
            0,
            {},
            None,
            is_write=is_write,
            expected_actor=expected_actor,
            actual_actor=actor,
        )
        if is_write:
            failure.write_outcome = "unknown"
        failure.completed_steps = completed_steps
        failure.failed_step = failed_step or "actor_verification"
        return ApiResult(
            ok=False,
            status=0,
            body=None,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            host=resolved_host,
            bucket=resolved_bucket,
            graphql_operation=resolved_graphql_operation,
            completed_steps=completed_steps,
            failed_step=failure.failed_step,
            failure=failure,
        )
    if authorized_actor_change:
        expected_actor = None

    # gh printed nothing (network failure before HTTP response)
    if not raw_stdout and proc.returncode != 0:
        failure = classify_legacy_failure(raw_stderr, stdout=raw_stdout, is_write=is_write)
        failure.completed_steps = completed_steps
        failure.failed_step = failed_step or "gh_invocation"
        return ApiResult(
            ok=False,
            status=0,
            body=None,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            host=resolved_host,
            bucket=resolved_bucket,
            graphql_operation=resolved_graphql_operation,
            completed_steps=completed_steps,
            failed_step=failed_step or "gh_invocation",
            failure=failure,
        )

    status, headers, parsed_body = parse_gh_include_output(raw_stdout)
    if status == 0 and proc.returncode == 0:
        status = 200
    request_id = headers.get("x-github-request-id")
    rate_limit = RateLimitInfo.from_headers(headers)
    response_bucket = normalized_provider_bucket(rate_limit.resource) or resolved_bucket

    # HTTP-200 GraphQL error (rate limit embedded in success response)
    if status == 200 and _is_graphql_rate_limit_body(parsed_body):
        failure = classify_error(
            status,
            headers,
            parsed_body,
            is_write=is_write,
            expected_actor=expected_actor,
            actual_actor=actor,
        )
        failure.completed_steps = completed_steps
        failure.failed_step = failed_step or "graphql_primary_rate_limited"
        return ApiResult(
            ok=False,
            status=status,
            body=parsed_body,
            headers=headers,
            request_id=request_id,
            rate_limit=rate_limit if rate_limit.is_populated() else None,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            host=resolved_host,
            bucket=response_bucket,
            graphql_operation=resolved_graphql_operation,
            completed_steps=completed_steps,
            failed_step=failure.failed_step,
            failure=failure,
        )

    ok = 200 <= status < 300

    if not ok:
        failure = classify_error(
            status,
            headers,
            parsed_body,
            is_write=is_write,
            expected_actor=expected_actor,
            actual_actor=actor,
        )
        failure.completed_steps = completed_steps
        failure.failed_step = failed_step or f"http_{status}"
        if failure.rate_limit is None and rate_limit.is_populated():
            failure.rate_limit = rate_limit.as_dict()
        return ApiResult(
            ok=False,
            status=status,
            body=parsed_body,
            headers=headers,
            request_id=request_id,
            rate_limit=rate_limit if rate_limit.is_populated() else None,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            host=resolved_host,
            bucket=response_bucket,
            graphql_operation=resolved_graphql_operation,
            completed_steps=completed_steps,
            failed_step=failure.failed_step,
            failure=failure,
        )

    return ApiResult(
        ok=True,
        status=status,
        body=parsed_body,
        headers=headers,
        request_id=request_id,
        rate_limit=rate_limit if rate_limit.is_populated() else None,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        host=resolved_host,
        bucket=response_bucket,
        graphql_operation=resolved_graphql_operation,
        completed_steps=completed_steps,
    )


def call_gh_with_retry(
    method: str,
    path: str,
    body: Any = None,
    *,
    gh_cmd: str = DEFAULT_GH,
    gh_prefix_args: Optional[list[str]] = None,
    api_version: str = DEFAULT_API_VERSION,
    extra_headers: Optional[dict[str, str]] = None,
    completed_steps: Optional[list[str]] = None,
    failed_step: Optional[str] = None,
    is_write: Optional[bool] = None,
    operation: Optional[str] = None,
    actor: Optional[str] = None,
    expected_actor: Optional[str] = None,
    host: Optional[str] = None,
    bucket: Optional[str] = None,
    graphql_operation: Optional[GraphQLOperation] = None,
    reconcile: Optional[
        Callable[[ApiResult, ReconciliationContext], ReconciliationDecision]
    ] = None,
    retry_policy: Optional[RetryPolicy] = None,
    retry_runtime: Optional[RetryRuntime] = None,
    deadline_at: Optional[float] = None,
    matrix_path: pathlib.Path = DEFAULT_OPERATION_MATRIX,
) -> ApiResult:
    resolved_graphql_operation = graphql_operation
    if is_graphql_path(path) and resolved_graphql_operation is None:
        resolved_graphql_operation = infer_graphql_operation_type(body)
    resolved_is_write = infer_is_write(
        method,
        path,
        body,
        explicit_is_write=is_write,
        graphql_operation=resolved_graphql_operation,
    )
    resolved_bucket = bucket or infer_api_bucket(path)
    resolved_operation = operation or "github.api.call"
    def attempt(timeout_seconds: Optional[float]) -> ApiResult:
        return call_gh(
            method,
            path,
            body,
            gh_cmd=gh_cmd,
            gh_prefix_args=gh_prefix_args,
            api_version=api_version,
            extra_headers=extra_headers,
            completed_steps=completed_steps,
            failed_step=failed_step,
            is_write=resolved_is_write,
            operation=resolved_operation,
            actor=actor,
            expected_actor=expected_actor,
            host=host,
            bucket=resolved_bucket,
            graphql_operation=resolved_graphql_operation,
            timeout_seconds=timeout_seconds,
        )

    return run_with_retry(
        lambda: attempt(None),
        operation=resolved_operation,
        is_write=resolved_is_write,
        actor=actor,
        expected_actor=expected_actor,
        host=host,
        bucket=resolved_bucket,
        reconcile=reconcile,
        retry_policy=retry_policy,
        retry_runtime=retry_runtime,
        deadline_at=deadline_at,
        matrix_path=matrix_path,
        attempt_with_timeout=lambda timeout: attempt(timeout),
    )


# ---------------------------------------------------------------------------
# Rate-limit probe (bounded: at most one live call per process)
# ---------------------------------------------------------------------------

_rate_limit_cache: dict[
    tuple[str, str, Optional[str], Optional[str], Optional[str], str],
    ApiResult,
] = {}


def rate_limit_probe(
    *,
    gh_cmd: str = DEFAULT_GH,
    api_version: str = DEFAULT_API_VERSION,
    actor: Optional[str] = None,
    expected_actor: Optional[str] = None,
    host: Optional[str] = None,
    operation: str = "rate_limit.probe",
    timeout_seconds: Optional[float] = None,
) -> ApiResult:
    """
    Fetch ``/rate_limit`` from GitHub.  At most one live call is made per
    process; subsequent calls return the cached result immediately.

    Args:
        gh_cmd: Path to the gh binary or wrapper (used only on the first call).

    Returns:
        ApiResult from ``GET /rate_limit``.
    """
    cache_key = (gh_cmd, api_version, actor, expected_actor, host, operation)
    if cache_key in _rate_limit_cache:
        return _rate_limit_cache[cache_key]
    result = call_gh(
        "GET",
        "/rate_limit",
        gh_cmd=gh_cmd,
        api_version=api_version,
        operation=operation,
        actor=actor,
        expected_actor=expected_actor,
        host=host,
        timeout_seconds=timeout_seconds,
    )
    _rate_limit_cache[cache_key] = result
    return result


def reset_rate_limit_cache() -> None:
    """Clear the per-process /rate_limit cache.  Intended for tests only."""
    _rate_limit_cache.clear()


# ---------------------------------------------------------------------------
# CLI shim (module is primarily imported; main() for quick manual probing)
# ---------------------------------------------------------------------------


def main() -> int:
    parser = TerminalArgumentParser(description="Low-level gh API wrapper with structured output.")
    parser.add_argument("--gh", default=DEFAULT_GH, help="Path to gh binary or wrapper.")
    sub = parser.add_subparsers(dest="cmd", required=True, parser_class=TerminalArgumentParser)

    p = sub.add_parser("call", help="Make a single API call and print the ApiResult JSON.")
    p.add_argument("--method", default="GET")
    p.add_argument("path")
    p.add_argument("--body-file", help="Read a JSON body from a file, or '-' for stdin.")
    p.add_argument("--operation", default="github.api.call")
    p.add_argument("--actor")
    p.add_argument("--expected-actor")
    p.add_argument("--host")
    p.add_argument("--bucket")
    p.add_argument("--graphql-operation", choices=("query", "mutation", "subscription", "unknown"))
    write_group = p.add_mutually_exclusive_group()
    write_group.add_argument("--write", action="store_true")
    write_group.add_argument("--read", action="store_true")

    sub.add_parser("rate-limit", help="Probe /rate_limit and print result.")

    p = sub.add_parser("classify-legacy", help="Classify captured legacy gh stdout/stderr.")
    p.add_argument("--returncode", type=int, required=True)
    p.add_argument("--stdout-file", required=True)
    p.add_argument("--stderr-file", required=True)
    p.add_argument("--operation", required=True)
    p.add_argument("--actor")
    p.add_argument("--expected-actor")
    p.add_argument("--host")
    p.add_argument("--transport", default="gh_cli_wrapper")
    p.add_argument("--bucket")
    p.add_argument("--graphql-operation", choices=("query", "mutation", "subscription", "unknown"))
    p.add_argument("--write", action="store_true")
    p.add_argument("--not-started", action="store_true")
    p.add_argument("--probe-unknown-rate-limit", action="store_true")
    p.add_argument("--completed-step", action="append", default=[])
    p.add_argument("--failed-step", default="gh_invocation")
    p.add_argument("--forward-stderr", action="store_true")
    p.add_argument("--payload-file", help="Merge non-contract fields from a JSON object into the final envelope.")

    p = sub.add_parser("terminal-error", help="Emit a terminal envelope for local validation failure.")
    p.add_argument("--operation", required=True)
    p.add_argument("--message", required=True)
    p.add_argument("--cause", default="validation_error")
    p.add_argument("--exit-code", type=int, default=2)
    p.add_argument("--transport", default="shell_compatibility")
    p.add_argument("--bucket")
    p.add_argument("--actor")
    p.add_argument("--expected-actor")
    p.add_argument("--write-outcome", choices=("not_started", "rejected", "unknown"))
    p.add_argument("--failed-step", default="input_validation")

    try:
        args = parser.parse_args()
    except ArgumentParsingError as exc:
        command = requested_subcommand(
            sys.argv[1:],
            {"call", "rate-limit", "classify-legacy", "terminal-error"},
        )
        failure = FailureDetail(
            cause="validation_error",
            message=str(exc),
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="not_started" if "--write" in sys.argv[1:] else None,
            failed_step="argument_parsing",
        )
        return emit_terminal(
            terminal_failure(
                failure,
                operation=f"github.api.{command.replace('-', '_')}",
                transport=TRANSPORT,
                bucket="unknown",
                exit_code=2,
                failed_step="argument_parsing",
            ),
            stderr_message=f"error: {exc}",
        )

    if args.cmd == "call" and is_repository_secret_scanning_alert_path(args.path):
        failure = FailureDetail(
            cause="validation_error",
            message=(
                "Raw repository secret-scanning alert operations are disabled; use "
                "github/scripts/github_read.py --repo OWNER/REPO secret-scanning-status for reads"
            ),
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            failed_step="input_validation",
        )
        return emit_terminal(
            terminal_failure(
                failure,
                operation=args.operation,
                actor=args.actor,
                expected_actor=args.expected_actor,
                host=args.host,
                transport=TRANSPORT,
                bucket=args.bucket or "rest_core",
                exit_code=2,
                failed_step="input_validation",
            ),
            stderr_message=f"error: {failure.message}",
        )

    captured_stderr = ""
    extra_payload: Optional[dict[str, Any]] = None
    input_error = False
    try:
        if args.cmd == "call":
            body_file = getattr(args, "body_file", None)
            if body_file == "-":
                body = json.loads(sys.stdin.read())
            elif body_file:
                body = json.loads(pathlib.Path(body_file).read_text(encoding="utf-8"))
            else:
                body = None
            explicit_is_write = True if args.write else False if args.read else None
            result = call_gh_with_retry(
                args.method,
                args.path,
                body,
                gh_cmd=args.gh,
                operation=args.operation,
                actor=args.actor,
                expected_actor=args.expected_actor,
                host=args.host,
                bucket=args.bucket,
                is_write=explicit_is_write,
                graphql_operation=args.graphql_operation,
            )
        elif args.cmd == "classify-legacy":
            stdout = pathlib.Path(args.stdout_file).read_text(encoding="utf-8")
            stderr = pathlib.Path(args.stderr_file).read_text(encoding="utf-8")
            if args.payload_file:
                parsed_payload = json.loads(pathlib.Path(args.payload_file).read_text(encoding="utf-8"))
                if not isinstance(parsed_payload, dict):
                    raise ValueError("--payload-file must contain a JSON object")
                extra_payload = parsed_payload
            captured_stderr = stderr.strip()
            result = legacy_process_result(
                args.returncode,
                stdout,
                stderr,
                operation=args.operation,
                is_write=args.write,
                actor=args.actor,
                expected_actor=args.expected_actor,
                host=args.host,
                transport=args.transport,
                bucket=args.bucket,
                graphql_operation=args.graphql_operation,
                completed_steps=args.completed_step,
                failed_step=args.failed_step,
                command_started=not args.not_started,
            )
            if (
                args.probe_unknown_rate_limit
                and result.failure
                and result.failure.cause == "rate_limited_unknown_bucket"
            ):
                probe = rate_limit_probe(
                    gh_cmd=args.gh,
                    actor=args.actor,
                    expected_actor=args.expected_actor,
                    host=args.host,
                )
                result = legacy_process_result(
                    args.returncode,
                    stdout,
                    stderr,
                    operation=args.operation,
                    is_write=args.write,
                    actor=args.actor,
                    expected_actor=args.expected_actor,
                    host=args.host,
                    transport=args.transport,
                    bucket=args.bucket,
                    graphql_operation=args.graphql_operation,
                    completed_steps=args.completed_step,
                    failed_step=args.failed_step,
                    command_started=not args.not_started,
                    rate_limit_result=probe,
                )
        elif args.cmd == "terminal-error":
            failure = FailureDetail(
                cause=args.cause,
                message=args.message,
                retryable=False,
                fallback_eligible=False,
                disposition="stop",
                write_outcome=args.write_outcome,
                failed_step=args.failed_step,
            )
            result = ApiResult(
                ok=False,
                status=0,
                body=None,
                operation=args.operation,
                actor=args.actor,
                expected_actor=args.expected_actor,
                host=DEFAULT_HOST,
                transport=args.transport,
                bucket=args.bucket,
                failure=failure,
                failed_step=args.failed_step,
            )
        else:
            result = rate_limit_probe(gh_cmd=args.gh, operation="github.api.rate_limit")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        input_error = True
        failure = FailureDetail(
            cause="validation_error",
            message=f"Could not read or parse helper input ({type(exc).__name__})",
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="not_started" if getattr(args, "write", False) else None,
        )
        result = ApiResult(
            ok=False,
            status=0,
            body=None,
            operation=getattr(args, "operation", f"github.api.{args.cmd.replace('-', '_')}"),
            host=getattr(args, "host", None) or DEFAULT_HOST,
            transport=TRANSPORT,
            failure=failure,
            failed_step="input_validation",
        )
    except KeyboardInterrupt:
        failure = FailureDetail(
            cause="cancelled",
            message="Interrupted",
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="unknown" if getattr(args, "write", False) else None,
        )
        result = ApiResult(
            ok=False,
            status=0,
            body=None,
            operation=getattr(args, "operation", f"github.api.{args.cmd.replace('-', '_')}"),
            host=getattr(args, "host", None) or DEFAULT_HOST,
            transport=TRANSPORT,
            failure=failure,
            failed_step="cancelled",
        )

    output = result.as_dict()
    if extra_payload is not None:
        for key, value in redact_body(extra_payload).items():
            if key not in _TERMINAL_RESERVED_KEYS and key not in ("error", "error_code"):
                output[key] = value
    if args.cmd == "classify-legacy":
        output["exit_code"] = 2 if input_error else args.returncode
    elif args.cmd == "terminal-error":
        output["exit_code"] = args.exit_code
    stderr_parts: list[str] = []
    if args.cmd == "classify-legacy" and args.forward_stderr and captured_stderr:
        stderr_parts.append(captured_stderr)
    if result.failure:
        normalized_error = f"error: {result.failure.message}"
        if normalized_error not in stderr_parts:
            stderr_parts.append(normalized_error)
    stderr_message = "\n".join(stderr_parts) or None
    return emit_terminal(output, stderr_message=stderr_message)


if __name__ == "__main__":
    raise SystemExit(main())
