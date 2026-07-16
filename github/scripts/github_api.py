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
- Error classification: invalid_credentials, actor_mismatch, permission_denied,
  rest_primary_rate_limited, graphql_primary_rate_limited,
  secondary_rate_limited, not_found, validation_error, conflict,
  network_provider_failure
- Field redaction for tokens, headers, paths, private body keys
- Bounded one-call-per-process /rate_limit probe
- Configurable gh command path via gh_cmd parameter
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

SCHEMA_VERSION = 1
DEFAULT_API_VERSION = "2022-11-28"
TRANSPORT = "gh_api"
DEFAULT_GH = os.environ.get("GITHUB_API_GH") or str(pathlib.Path(__file__).with_name("gh-with-env-token"))

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
    completed_steps: list[str] = field(default_factory=list)
    failed_step: Optional[str] = None
    schema_version: int = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "ok": self.ok,
            "status": self.status,
            "transport": self.transport,
            "body": redact_body(self.body),
        }
        if self.operation:
            d["operation"] = self.operation
        if self.actor:
            d["actor"] = self.actor
        if self.expected_actor:
            d["expected_actor"] = self.expected_actor
        if self.host:
            d["host"] = self.host
        if self.request_id:
            d["request_id"] = self.request_id
        if self.rate_limit and self.rate_limit.is_populated():
            d["rate_limit"] = self.rate_limit.as_dict()
        if self.failure:
            d["failure"] = self.failure.as_dict()
        if self.completed_steps:
            d["completed_steps"] = self.completed_steps
        if self.failed_step:
            d["failed_step"] = self.failed_step
        return d


# ---------------------------------------------------------------------------
# Parsing gh --include output
# ---------------------------------------------------------------------------


def _try_parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


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
    i = 0

    # Advance to first HTTP/ status line
    while i < len(lines) and not lines[i].startswith("HTTP/"):
        i += 1

    if i >= len(lines):
        body_str = raw.strip()
        return 0, {}, _try_parse_json(body_str) if body_str else None

    # Parse status line: "HTTP/2.0 200 OK" or "HTTP/2.0 200 "
    parts = lines[i].split(None, 2)
    try:
        status = int(parts[1]) if len(parts) >= 2 else 0
    except (ValueError, IndexError):
        status = 0
    i += 1

    # Parse headers until blank line
    while i < len(lines):
        line = lines[i].rstrip()
        if not line:
            i += 1
            break
        if ":" in line:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        i += 1

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
    return str(body) if body else ""


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


def classify_legacy_failure(stderr: str, *, is_write: bool = False) -> FailureDetail:
    """Classify failures from legacy gh commands that expose no HTTP response."""
    lowered = stderr.lower()
    not_started = "not_started" if is_write else None
    if "would run as" in lowered and "expected" in lowered:
        cause = "actor_mismatch"
        disposition = "stop"
        fallback_eligible = False
    elif any(
        text in lowered
        for text in (
            "bad credentials",
            "token is invalid",
            "failed to log in",
            "authentication failed",
            "requires authentication",
            "gh auth login",
        )
    ):
        cause = "invalid_credentials"
        disposition = "requires_authorization"
        fallback_eligible = True
    elif "graphql" in lowered and "rate limit" in lowered:
        cause = "graphql_primary_rate_limited"
        disposition = "retry"
        fallback_eligible = False
    elif "secondary rate" in lowered or "retry-after" in lowered:
        cause = "secondary_rate_limited"
        disposition = "retry"
        fallback_eligible = False
    elif "rate limit" in lowered:
        cause = "rate_limited_unknown_bucket"
        disposition = "retry"
        fallback_eligible = False
    elif any(text in lowered for text in ("resource not accessible", "forbidden", "permission denied")):
        cause = "permission_denied"
        disposition = "requires_authorization"
        fallback_eligible = True
    else:
        cause = "network_provider_failure"
        disposition = "stop" if is_write else "retry"
        fallback_eligible = False
    return FailureDetail(
        cause=cause,
        message=stderr.strip() or "GitHub command failed without structured response evidence",
        retryable=disposition == "retry" and not is_write,
        fallback_eligible=fallback_eligible,
        disposition=disposition,
        write_outcome=("unknown" if cause == "network_provider_failure" else not_started),
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
        extra_headers: Additional -H headers to include in the request.

    Returns:
        Command list ready for subprocess.run.
    """
    if not path.startswith("/") and not path.startswith("http"):
        path = f"/{path}"

    cmd = [
        gh_cmd,
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


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def call_gh(
    method: str,
    path: str,
    body: Any = None,
    *,
    gh_cmd: str = DEFAULT_GH,
    api_version: str = DEFAULT_API_VERSION,
    extra_headers: Optional[dict[str, str]] = None,
    completed_steps: Optional[list[str]] = None,
    failed_step: Optional[str] = None,
    is_write: Optional[bool] = None,
    operation: Optional[str] = None,
    actor: Optional[str] = None,
    expected_actor: Optional[str] = None,
    host: Optional[str] = None,
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
        extra_headers: Additional headers to pass as ``-H name: value``.
        completed_steps: Steps already done before this call (for partial-success tracking).
        failed_step: Label for the step this call represents (appended to FailureDetail on error).
        is_write: Override write detection (default: True for POST/PUT/PATCH/DELETE).

    Returns:
        ApiResult with ok=True on 2xx, ok=False with a populated FailureDetail otherwise.
    """
    if completed_steps is None:
        completed_steps = []
    if is_write is None:
        is_write = method.upper() in ("POST", "PUT", "PATCH", "DELETE")

    if expected_actor and actor and expected_actor != actor:
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
            host=host,
            completed_steps=completed_steps,
            failed_step=failure.failed_step,
            failure=failure,
        )

    has_body = body is not None or method.upper() in ("POST", "PUT", "PATCH")
    cmd = build_gh_command(
        method,
        path,
        gh_cmd=gh_cmd,
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
        )
    except (FileNotFoundError, PermissionError) as exc:
        return ApiResult(
            ok=False,
            status=0,
            body=None,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            host=host,
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
            host=host,
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

    # gh printed nothing (network failure before HTTP response)
    if not raw_stdout and proc.returncode != 0:
        failure = classify_legacy_failure(raw_stderr, is_write=is_write)
        failure.completed_steps = completed_steps
        failure.failed_step = failed_step or "gh_invocation"
        return ApiResult(
            ok=False,
            status=0,
            body=None,
            operation=operation,
            actor=actor,
            expected_actor=expected_actor,
            host=host,
            completed_steps=completed_steps,
            failed_step=failed_step or "gh_invocation",
            failure=failure,
        )

    status, headers, parsed_body = parse_gh_include_output(raw_stdout)
    if status == 0 and proc.returncode == 0:
        status = 200
    request_id = headers.get("x-github-request-id")
    rate_limit = RateLimitInfo.from_headers(headers)

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
            host=host,
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
            host=host,
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
        host=host,
        completed_steps=completed_steps,
    )


# ---------------------------------------------------------------------------
# Rate-limit probe (bounded: at most one live call per process)
# ---------------------------------------------------------------------------

_rate_limit_cache: dict[tuple[str, str, Optional[str], Optional[str], Optional[str]], ApiResult] = {}


def rate_limit_probe(
    *,
    gh_cmd: str = DEFAULT_GH,
    api_version: str = DEFAULT_API_VERSION,
    actor: Optional[str] = None,
    expected_actor: Optional[str] = None,
    host: Optional[str] = None,
) -> ApiResult:
    """
    Fetch ``/rate_limit`` from GitHub.  At most one live call is made per
    process; subsequent calls return the cached result immediately.

    Args:
        gh_cmd: Path to the gh binary or wrapper (used only on the first call).

    Returns:
        ApiResult from ``GET /rate_limit``.
    """
    cache_key = (gh_cmd, api_version, actor, expected_actor, host)
    if cache_key in _rate_limit_cache:
        return _rate_limit_cache[cache_key]
    result = call_gh(
        "GET",
        "/rate_limit",
        gh_cmd=gh_cmd,
        api_version=api_version,
        operation="rate_limit.probe",
        actor=actor,
        expected_actor=expected_actor,
        host=host,
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
    import argparse

    parser = argparse.ArgumentParser(description="Low-level gh API wrapper with structured output.")
    parser.add_argument("--gh", default=DEFAULT_GH, help="Path to gh binary or wrapper.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("call", help="Make a single API call and print the ApiResult JSON.")
    p.add_argument("--method", default="GET")
    p.add_argument("path")
    p.add_argument("--body-file", help="Read a JSON body from a file, or '-' for stdin.")

    sub.add_parser("rate-limit", help="Probe /rate_limit and print result.")

    args = parser.parse_args()

    if args.cmd == "call":
        body_file = getattr(args, "body_file", None)
        if body_file == "-":
            body = json.loads(sys.stdin.read())
        elif body_file:
            body = json.loads(pathlib.Path(body_file).read_text(encoding="utf-8"))
        else:
            body = None
        result = call_gh(args.method, args.path, body, gh_cmd=args.gh)
    else:
        result = rate_limit_probe(gh_cmd=args.gh)

    print(json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":")))
    return 0 if (not result.failure) else 1


if __name__ == "__main__":
    raise SystemExit(main())
