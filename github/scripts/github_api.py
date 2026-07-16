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

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

SCHEMA_VERSION = 1
DEFAULT_API_VERSION = "2022-11-28"
TRANSPORT = "gh_api"
DEFAULT_GH = os.environ.get("GITHUB_API_GH") or str(pathlib.Path(__file__).with_name("gh-with-env-token"))
DEFAULT_HOST = os.environ.get("GH_HOST") or "github.com"

GraphQLOperation = Literal["query", "mutation", "subscription", "unknown"]


@dataclass(frozen=True)
class CommandContext:
    is_write: bool
    transport: str
    bucket: str
    graphql_operation: Optional[GraphQLOperation] = None


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
        return d


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

    # Proxies and redirects can emit multiple response blocks. The final block
    # is authoritative; earlier 1xx/3xx headers must not replace the result.
    i = status_indices[-1]

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


def is_graphql_path(path: str) -> bool:
    normalized = path.split("?", 1)[0].rstrip("/").lower()
    return normalized == "graphql" or normalized.endswith("/graphql")


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


def _rate_limit_from_probe(result: Optional[ApiResult]) -> tuple[Optional[str], Optional[dict[str, Any]]]:
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
        bucket=bucket,
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
    bucket: Optional[str] = None,
    graphql_operation: Optional[GraphQLOperation] = None,
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
    resolved_bucket = bucket or ("graphql" if is_graphql_path(path) else "rest_core")
    resolved_host = host or DEFAULT_HOST

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
            bucket=resolved_bucket,
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
            bucket=resolved_bucket,
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
        bucket=resolved_bucket,
        graphql_operation=resolved_graphql_operation,
        completed_steps=completed_steps,
    )


# ---------------------------------------------------------------------------
# Rate-limit probe (bounded: at most one live call per process)
# ---------------------------------------------------------------------------

_rate_limit_cache: dict[tuple[str, str, Optional[str], Optional[str], Optional[str], str], ApiResult] = {}


def rate_limit_probe(
    *,
    gh_cmd: str = DEFAULT_GH,
    api_version: str = DEFAULT_API_VERSION,
    actor: Optional[str] = None,
    expected_actor: Optional[str] = None,
    host: Optional[str] = None,
    operation: str = "rate_limit.probe",
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
            result = call_gh(
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
