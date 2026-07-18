#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
test_github_api.py — Deterministic regression tests for github_api.py.

All GitHub transport is faked via subprocess fixtures.  No live calls are made.
Run with:
    python3 github/scripts/test_github_api.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Load module under test
# ---------------------------------------------------------------------------

_MODULE_PATH = Path(__file__).with_name("github_api.py")


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("github_api_under_test", _MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {_MODULE_PATH}")
    mod = importlib.util.module_from_spec(spec)
    # Python 3.14+ @dataclass introspects sys.modules[cls.__module__]; register first.
    sys.modules["github_api_under_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_api = _load_module()

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _fake_proc(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout.encode(),
        stderr=stderr.encode(),
    )


def _include_output(
    status: int = 200,
    headers: Optional[dict[str, str]] = None,
    body: Any = None,
    http_version: str = "2.0",
) -> str:
    """Construct synthetic gh api --include output."""
    hdr_map = {
        "content-type": "application/json; charset=utf-8",
        "x-github-request-id": "AABB:1234:CCDD",
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "4999",
        "x-ratelimit-reset": "1700000000",
        "x-ratelimit-used": "1",
        "x-ratelimit-resource": "core",
    }
    if headers:
        hdr_map.update(headers)

    lines = [f"HTTP/{http_version} {status} "]
    for k, v in hdr_map.items():
        lines.append(f"{k}: {v}")
    lines.append("")  # blank line separating headers from body
    if body is not None:
        lines.append(json.dumps(body))
    return "\n".join(lines)


def _call(
    method: str = "GET",
    path: str = "/repos/owner/repo",
    body: Any = None,
    *,
    fake_stdout: str = "",
    fake_stderr: str = "",
    returncode: int = 0,
    completed_steps: Optional[list[str]] = None,
    failed_step: Optional[str] = None,
    is_write: Optional[bool] = None,
) -> Any:
    """Run call_gh with a fake subprocess.run."""
    proc = _fake_proc(fake_stdout, fake_stderr, returncode)
    with patch("subprocess.run", return_value=proc):
        _api.reset_rate_limit_cache()
        return _api.call_gh(
            method,
            path,
            body,
            completed_steps=completed_steps,
            failed_step=failed_step,
            is_write=is_write,
        )


# ---------------------------------------------------------------------------
# parse_gh_include_output
# ---------------------------------------------------------------------------


def test_parse_success_200_json_body() -> None:
    raw = _include_output(200, body={"id": 42, "title": "hello"})
    status, headers, body = _api.parse_gh_include_output(raw)
    assert status == 200, status
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert headers["x-github-request-id"] == "AABB:1234:CCDD"
    assert body == {"id": 42, "title": "hello"}


def test_parse_204_no_body() -> None:
    raw = _include_output(204, body=None)
    status, headers, body = _api.parse_gh_include_output(raw)
    assert status == 204, status
    assert body is None


def test_parse_404_error_body() -> None:
    raw = _include_output(404, body={"message": "Not Found", "documentation_url": "https://docs.github.com"})
    status, headers, body = _api.parse_gh_include_output(raw)
    assert status == 404, status
    assert isinstance(body, dict)
    assert body["message"] == "Not Found"


def test_parse_crlf_line_endings() -> None:
    raw = "HTTP/2.0 200 \r\ncontent-type: application/json\r\n\r\n{\"ok\": true}"
    status, headers, body = _api.parse_gh_include_output(raw)
    assert status == 200, status
    assert body == {"ok": True}


def test_parse_http1_status_line() -> None:
    raw = _include_output(201, http_version="1.1")
    status, _, _ = _api.parse_gh_include_output(raw)
    assert status == 201, status


def test_parse_non_json_body_returned_as_string() -> None:
    raw = "HTTP/2.0 200 \ncontent-type: text/plain\n\nhello world"
    status, headers, body = _api.parse_gh_include_output(raw)
    assert status == 200, status
    assert body == "hello world"


def test_parse_bare_json_without_status_line() -> None:
    # Fallback: no HTTP/ prefix at all
    raw = '{"fallback": true}'
    status, headers, body = _api.parse_gh_include_output(raw)
    assert status == 0, status
    assert body == {"fallback": True}


def test_parse_rate_limit_headers_extracted() -> None:
    raw = _include_output(
        200,
        headers={
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "99",
            "x-ratelimit-reset": "1700000099",
            "x-ratelimit-used": "4901",
            "x-ratelimit-resource": "graphql",
        },
        body={},
    )
    _, headers, _ = _api.parse_gh_include_output(raw)
    assert headers["x-ratelimit-remaining"] == "99"
    assert headers["x-ratelimit-resource"] == "graphql"


def test_parse_uses_final_http_response_block() -> None:
    raw = (
        "HTTP/1.1 100 Continue\r\n"
        "x-interim: true\r\n"
        "\r\n"
        "HTTP/2.0 200 OK\r\n"
        "content-type: application/json\r\n"
        "x-github-request-id: final-request\r\n"
        "\r\n"
        '{"ok":true}'
    )
    status, headers, body = _api.parse_gh_include_output(raw)
    assert status == 200, status
    assert headers["x-github-request-id"] == "final-request"
    assert body == {"ok": True}


def test_parse_final_text_body_can_contain_http_status_lines() -> None:
    raw = (
        "HTTP/2.0 200 OK\r\n"
        "content-type: text/plain\r\n"
        "x-github-request-id: log-request\r\n"
        "\r\n"
        "setup\n"
        "HTTP/1.1 503 from application output\n"
        "failure"
    )
    status, headers, body = _api.parse_gh_include_output(raw)
    assert status == 200, status
    assert headers["x-github-request-id"] == "log-request"
    assert body == "setup\nHTTP/1.1 503 from application output\nfailure"


def test_parse_final_text_body_can_start_with_http_status_line() -> None:
    raw = (
        "HTTP/2.0 200 OK\r\n"
        "content-type: text/plain\r\n"
        "x-github-request-id: log-request\r\n"
        "\r\n"
        "HTTP/1.1 503 from application output\n"
        "failure"
    )
    status, headers, body = _api.parse_gh_include_output(raw)
    assert status == 200, status
    assert headers["x-github-request-id"] == "log-request"
    assert body == "HTTP/1.1 503 from application output\nfailure"


def test_parse_redirect_retains_github_diagnostic_headers() -> None:
    raw = (
        "HTTP/2.0 302 Found\r\n"
        "x-github-request-id: github-request\r\n"
        "x-ratelimit-limit: 5000\r\n"
        "x-ratelimit-remaining: 4998\r\n"
        "x-ratelimit-reset: 1700000000\r\n"
        "location: https://storage.example.invalid/log\r\n"
        "\r\n"
        "HTTP/2.0 200 OK\r\n"
        "content-type: text/plain\r\n"
        "\r\n"
        "job log"
    )
    status, headers, body = _api.parse_gh_include_output(raw)
    assert status == 200, status
    assert headers["x-github-request-id"] == "github-request"
    assert headers["x-ratelimit-remaining"] == "4998"
    assert body == "job log"


# ---------------------------------------------------------------------------
# build_gh_command (body-safety)
# ---------------------------------------------------------------------------


def test_build_get_command_no_stdin_flag() -> None:
    cmd = _api.build_gh_command("GET", "/repos/owner/repo")
    assert "--input" not in cmd
    assert "-" not in cmd
    assert "--include" in cmd
    assert "--method" in cmd
    assert cmd[cmd.index("--method") + 1] == "GET"


def test_build_post_command_uses_stdin() -> None:
    cmd = _api.build_gh_command("POST", "/repos/owner/repo/issues")
    idx = cmd.index("--input")
    assert cmd[idx + 1] == "-", "body must come from stdin, not command line"


def test_build_patch_command_uses_stdin() -> None:
    cmd = _api.build_gh_command("PATCH", "/repos/owner/repo/pulls/1")
    assert "--input" in cmd


def test_build_delete_command_no_stdin_flag() -> None:
    cmd = _api.build_gh_command("DELETE", "/repos/owner/repo/git/refs/heads/old")
    assert "--input" not in cmd


def test_build_delete_command_can_use_json_stdin() -> None:
    cmd = _api.build_gh_command("DELETE", "/sub_issue", gh_cmd="gh", has_body=True)
    assert cmd[-2:] == ["--input", "-"]


def test_build_command_prepends_slash_to_bare_path() -> None:
    cmd = _api.build_gh_command("GET", "repos/owner/repo")
    path_arg = cmd[-1]
    assert path_arg.startswith("/"), f"Expected leading slash, got {path_arg!r}"


def test_build_command_custom_gh_cmd() -> None:
    cmd = _api.build_gh_command("GET", "/rate_limit", gh_cmd="./gh-with-env-token")
    assert cmd[0] == "./gh-with-env-token"


def test_build_command_extra_headers() -> None:
    cmd = _api.build_gh_command("GET", "/rate_limit", extra_headers={"X-GitHub-Api-Version": "2022-11-28"})
    assert "-H" in cmd
    h_idx = cmd.index("-H")
    assert "X-GitHub-Api-Version: 2022-11-28" == cmd[h_idx + 1]


def test_build_command_includes_default_api_version() -> None:
    cmd = _api.build_gh_command("GET", "/rate_limit", gh_cmd="gh")
    assert f"X-GitHub-Api-Version: {_api.DEFAULT_API_VERSION}" in cmd


def test_build_post_body_never_on_command_line() -> None:
    """The body must not appear as any argv token — it belongs on stdin."""
    sensitive_body = {"token": "ghp_secret_value", "title": "my PR"}
    cmd = _api.build_gh_command("POST", "/repos/owner/repo/pulls")
    for token in cmd[1:]:
        assert "ghp_secret_value" not in token
        assert "secret" not in token.lower() or token.startswith("-H")


# ---------------------------------------------------------------------------
# call_gh: success envelope
# ---------------------------------------------------------------------------


def test_call_gh_success_returns_ok_result() -> None:
    stdout = _include_output(200, body={"number": 7})
    result = _call("GET", "/repos/owner/repo/pulls/7", fake_stdout=stdout)
    assert result.ok is True
    assert result.status == 200
    assert result.body == {"number": 7}
    assert result.failure is None
    assert result.schema_version == _api.SCHEMA_VERSION


def test_call_gh_result_asdict_has_version() -> None:
    stdout = _include_output(201, body={"id": 1})
    result = _call("POST", "/repos/owner/repo/issues", body={"title": "bug"}, fake_stdout=stdout)
    d = result.as_dict()
    assert d["schema_version"] == _api.SCHEMA_VERSION
    assert d["ok"] is True
    assert d["status"] == 201
    assert "failure" not in d


def test_call_gh_success_extracts_request_id() -> None:
    stdout = _include_output(200, headers={"x-github-request-id": "REQ:ID:12"}, body={})
    result = _call("GET", "/rate_limit", fake_stdout=stdout)
    assert result.request_id == "REQ:ID:12"


def test_call_gh_success_extracts_rate_limit() -> None:
    stdout = _include_output(
        200,
        headers={
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "4321",
            "x-ratelimit-reset": "1700000000",
            "x-ratelimit-used": "679",
        },
        body={"resources": {}},
    )
    result = _call("GET", "/rate_limit", fake_stdout=stdout)
    assert result.rate_limit is not None
    assert result.rate_limit.remaining == 4321
    assert result.rate_limit.limit == 5000


def test_call_gh_surfaces_explicit_active_auth_actor() -> None:
    stdout = _include_output(200, body={"ok": True})
    result = _call(
        "GET",
        "/repos/owner/repo",
        fake_stdout=stdout,
        fake_stderr="warning: no automation gh token found; explicitly authorized active-auth fallback; using the active gh account 'octocat'",
    )
    assert result.actor == "octocat"


def test_call_gh_reported_actor_replaces_initial_actor_for_authorized_fallback() -> None:
    proc = _fake_proc(
        stdout=_include_output(200, body={"ok": True}),
        stderr=(
            "warning: no automation gh token found; explicitly authorized active-auth fallback; "
            "using the active gh account 'octocat'"
        ),
    )
    with patch("subprocess.run", return_value=proc):
        result = _api.call_gh(
            "GET",
            "/repos/owner/repo",
            actor="shiny-code-bot",
            expected_actor=None,
        )
    assert result.ok is True, result.as_dict()
    assert result.actor == "octocat", result.as_dict()


def test_call_gh_unannounced_actor_change_fails_closed_after_write() -> None:
    proc = _fake_proc(
        stdout=_include_output(201, body={"id": 42}),
        stderr="warning: routing changed; using the active gh account 'octocat'",
    )
    with patch("subprocess.run", return_value=proc):
        result = _api.call_gh(
            "POST",
            "/repos/owner/repo/issues",
            {"title": "demo"},
            actor="shiny-code-bot",
            expected_actor="shiny-code-bot",
        )
    assert result.ok is False, result.as_dict()
    assert result.failure.cause == "actor_mismatch", result.failure
    assert result.failure.write_outcome == "unknown", result.failure


def test_call_gh_post_sends_body_as_json_stdin() -> None:
    """Verify the body is serialised as JSON bytes on stdin, not in argv."""
    captured_calls: list[dict] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:  # type: ignore
        captured_calls.append({"cmd": cmd, "input": kwargs.get("input")})
        return _fake_proc(
            stdout=_include_output(201, body={"id": 99}),
            returncode=0,
        )

    with patch("subprocess.run", side_effect=fake_run):
        _api.call_gh("POST", "/repos/owner/repo/issues", {"title": "my issue", "body": "details"})

    assert len(captured_calls) == 1
    call = captured_calls[0]
    # argv must not contain the body content
    for tok in call["cmd"]:
        assert "my issue" not in tok
    # body goes via stdin as JSON
    stdin_data = json.loads(call["input"].decode())
    assert stdin_data["title"] == "my issue"


def test_call_gh_forwards_explicit_subprocess_environment() -> None:
    explicit_environment = {"PATH": "/tmp", "GH_TOKEN": "test-token"}
    proc = _fake_proc(stdout=_include_output(200, body={"ok": True}))
    with patch("subprocess.run", return_value=proc) as run:
        result = _api.call_gh(
            "GET",
            "/repos/owner/repo",
            subprocess_env=explicit_environment,
        )
    assert result.ok is True, result.as_dict()
    assert run.call_args.kwargs["env"] is explicit_environment


def test_call_gh_timeout_marks_started_write_outcome_unknown() -> None:
    timeout = subprocess.TimeoutExpired(cmd=["gh", "api"], timeout=0.25)
    with patch("subprocess.run", side_effect=timeout):
        result = _api.call_gh(
            "POST",
            "/repos/owner/repo/issues",
            {"title": "demo"},
            operation="github.issue.create",
            timeout_seconds=0.25,
        )
    payload = result.as_dict()
    assert result.ok is False, payload
    assert result.failure.cause == "deadline_exceeded", result.failure
    assert result.failure.write_outcome == "unknown", result.failure
    assert payload["failed_step"] == "subprocess_timeout", payload


def test_call_gh_timeout_preserves_authorized_fallback_actor() -> None:
    stderr = (
        "warning: no automation gh token found; explicitly authorized active-auth fallback; "
        "using the active gh account 'octocat'"
    )
    timeout = subprocess.TimeoutExpired(
        cmd=["gh", "api"],
        timeout=0.25,
        stderr=stderr.encode(),
    )
    with patch("subprocess.run", side_effect=timeout):
        result = _api.call_gh(
            "GET",
            "/repos/owner/repo",
            actor="shiny-code-bot",
            expected_actor="shiny-code-bot",
            timeout_seconds=0.25,
        )
    assert result.actor == "octocat", result.as_dict()
    assert result.expected_actor is None, result.as_dict()
    assert result.failure.cause == "deadline_exceeded", result.failure


def test_call_gh_uses_provider_reported_search_bucket() -> None:
    stdout = _include_output(
        200,
        headers={"x-ratelimit-resource": "search"},
        body={"items": []},
    )
    result = _call("GET", "/search/issues?q=repo%3Aowner%2Frepo", fake_stdout=stdout)
    assert result.bucket == "search", result.as_dict()


def test_retry_fails_closed_on_provider_bucket_mismatch() -> None:
    stdout = _include_output(
        200,
        headers={"x-ratelimit-resource": "search"},
        body={"items": []},
    )
    proc = _fake_proc(stdout=stdout)
    with tempfile.TemporaryDirectory() as temp_dir, patch("subprocess.run", return_value=proc):
        result = _api.call_gh_with_retry(
            "GET",
            "/search/issues?q=repo%3Aowner%2Frepo",
            operation="github.api.call",
            bucket="rest_core",
            retry_policy=retry_policy(Path(temp_dir), max_attempts=1),
        )
    assert result.ok is False, result.as_dict()
    assert result.failure.cause == "retry_context_changed", result.failure
    assert result.retry_summary.last_bucket == "search", result.retry_summary


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def test_classify_401_invalid_credentials() -> None:
    stdout = _include_output(
        401,
        headers={"x-ratelimit-limit": "0"},
        body={"message": "Bad credentials"},
    )
    result = _call("GET", "/repos/owner/repo", fake_stdout=stdout, returncode=1)
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.cause == "invalid_credentials"
    assert result.failure.fallback_eligible is True
    assert result.failure.retryable is False
    assert result.failure.disposition == "requires_authorization"


def test_classify_403_actor_mismatch() -> None:
    with patch("subprocess.run") as run:
        result = _api.call_gh(
            "POST",
            "/repos/owner/repo/issues",
            body={"title": "test"},
            actor="active-user",
            expected_actor="automation-bot",
        )
    run.assert_not_called()
    assert result.failure is not None
    assert result.failure.cause == "actor_mismatch"
    assert result.failure.write_outcome == "not_started"
    assert result.failure.fallback_eligible is False


def test_classify_403_permission_denied() -> None:
    stdout = _include_output(
        403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-limit": "5000"},
        body={"message": "You do not have permission to do this"},
    )
    result = _call("POST", "/repos/owner/repo/releases", fake_stdout=stdout, returncode=1)
    assert result.failure is not None
    assert result.failure.cause == "permission_denied"
    assert result.failure.write_outcome == "not_started"
    assert result.failure.disposition == "requires_authorization"


def test_classify_403_rest_rate_limit_via_remaining_zero() -> None:
    stdout = _include_output(
        403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-limit": "5000"},
        body={"message": "API rate limit exceeded for installation"},
    )
    result = _call("GET", "/repos/owner/repo/pulls", fake_stdout=stdout, returncode=1)
    assert result.failure is not None
    assert result.failure.cause == "rest_primary_rate_limited"
    assert result.failure.retryable is True
    assert result.failure.fallback_eligible is False
    assert result.failure.rate_limit is not None
    assert result.failure.rate_limit["remaining"] == 0


def test_classify_429_rest_rate_limit() -> None:
    stdout = _include_output(429, body={"message": "Too many requests"})
    result = _call("GET", "/repos/owner/repo", fake_stdout=stdout, returncode=1)
    assert result.failure is not None
    assert result.failure.cause == "rest_primary_rate_limited"
    assert result.failure.retryable is True


def test_classify_403_secondary_throttle_via_retry_after() -> None:
    stdout = _include_output(
        403,
        headers={"retry-after": "60"},
        body={"message": "You have exceeded a secondary rate limit"},
    )
    result = _call("POST", "/repos/owner/repo/pulls", fake_stdout=stdout, returncode=1)
    assert result.failure is not None
    assert result.failure.cause == "secondary_rate_limited"
    assert result.failure.retryable is True
    assert result.failure.fallback_eligible is False
    assert "60" in result.failure.message
    payload = result.as_dict()
    assert payload["failure"]["rate_limit"]["retry_after"] == 60


def test_retry_after_on_503_remains_provider_failure() -> None:
    stdout = _include_output(
        503,
        headers={"retry-after": "30"},
        body={"message": "Service temporarily unavailable"},
    )
    result = _call("GET", "/repos/owner/repo", fake_stdout=stdout, returncode=1)
    assert result.failure is not None
    assert result.failure.cause == "network_provider_failure"
    assert result.as_dict()["failure"]["rate_limit"]["retry_after"] == 30


def test_html_503_is_summarized_without_json_parse_noise() -> None:
    html = """<!DOCTYPE html><html><head><title>Unicorn! &middot; GitHub</title></head>
    <body><p>Sorry about that. Please try refreshing.</p></body></html>"""
    stdout = _include_output(
        503,
        headers={"content-type": "text/html", "x-github-request-id": "REQ-503"},
        body=html,
    )
    result = _call("GET", "/user", fake_stdout=stdout, returncode=1)
    assert result.failure is not None
    assert result.failure.cause == "network_provider_failure"
    assert result.failure.message.endswith("Unicorn! · GitHub — Sorry about that. Please try refreshing.")
    assert "invalid character '<'" not in result.failure.message
    assert len(result.failure.message) < 200
    assert result.request_id == "REQ-503"


def test_classify_403_secondary_throttle_via_message() -> None:
    stdout = _include_output(
        403,
        body={"message": "You have triggered an abuse detection mechanism"},
    )
    result = _call("POST", "/repos/owner/repo/issues", body={}, fake_stdout=stdout, returncode=1)
    assert result.failure is not None
    assert result.failure.cause == "secondary_rate_limited"


def test_permission_403_wins_over_incidental_zero_remaining() -> None:
    stdout = _include_output(
        403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-limit": "5000"},
        body={"message": "Resource not accessible by integration"},
    )
    result = _call("GET", "/repos/owner/private", fake_stdout=stdout, returncode=1)
    assert result.failure is not None
    assert result.failure.cause == "permission_denied"
    assert result.failure.retryable is False


def test_classify_200_graphql_rate_limit() -> None:
    gql_body = {
        "data": None,
        "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded for viewer."}],
    }
    stdout = _include_output(200, headers={"x-ratelimit-resource": "graphql"}, body=gql_body)
    result = _call("POST", "/graphql", body={"query": "{ viewer { login } }"}, fake_stdout=stdout)
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.cause == "graphql_primary_rate_limited"
    assert result.failure.retryable is True
    assert result.failure.fallback_eligible is False
    assert result.failure.disposition == "retry"


def test_classify_404_not_found() -> None:
    stdout = _include_output(404, body={"message": "Not Found"})
    result = _call("GET", "/repos/owner/repo/pulls/9999", fake_stdout=stdout, returncode=1)
    assert result.failure is not None
    assert result.failure.cause == "not_found"
    assert result.failure.retryable is False
    assert result.failure.fallback_eligible is False
    assert result.failure.disposition == "stop"


def test_classify_422_validation_error() -> None:
    stdout = _include_output(422, body={"message": "Validation Failed", "errors": [{"code": "missing_field"}]})
    result = _call("POST", "/repos/owner/repo/issues", body={"title": ""}, fake_stdout=stdout, returncode=1)
    assert result.failure is not None
    assert result.failure.cause == "validation_error"
    assert result.failure.write_outcome == "rejected"
    assert result.failure.disposition == "stop"


def test_classify_409_conflict() -> None:
    stdout = _include_output(409, body={"message": "Merge conflict"})
    result = _call("PUT", "/repos/owner/repo/pulls/1/merge", body={}, fake_stdout=stdout, returncode=1)
    assert result.failure is not None
    assert result.failure.cause == "conflict"
    assert result.failure.write_outcome == "rejected"


def test_classify_500_network_error() -> None:
    stdout = _include_output(500, body={"message": "Internal Server Error"})
    result = _call("GET", "/repos/owner/repo", fake_stdout=stdout, returncode=1)
    assert result.failure is not None
    assert result.failure.cause == "network_provider_failure"
    assert result.failure.retryable is True


def test_write_provider_failure_is_unknown_and_not_directly_retryable() -> None:
    stdout = _include_output(502, body={"message": "Bad Gateway"})
    result = _call("POST", "/repos/owner/repo/issues", body={}, fake_stdout=stdout, returncode=1)
    assert result.failure is not None
    assert result.failure.cause == "network_provider_failure"
    assert result.failure.write_outcome == "unknown"
    assert result.failure.retryable is False


def test_classify_no_output_returncode_nonzero() -> None:
    result = _call("GET", "/rate_limit", fake_stdout="", fake_stderr="connection refused", returncode=1)
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.cause == "network_provider_failure"
    assert "connection refused" in result.failure.message


def test_legacy_graphql_rate_limit_does_not_offer_identity_fallback() -> None:
    result = _call(
        "GET",
        "/graphql",
        fake_stdout="",
        fake_stderr="GraphQL: API rate limit already exceeded",
        returncode=1,
    )
    assert result.failure is not None
    assert result.failure.cause == "graphql_primary_rate_limited"
    assert result.failure.fallback_eligible is False


def test_classify_subprocess_launch_failure() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError("/private/path/to/gh")):
        result = _api.call_gh("GET", "/repos/owner/repo")
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.cause == "network_provider_failure"
    assert result.failure.message == "Failed to launch GitHub CLI (FileNotFoundError)"
    assert "/private/path" not in result.failure.message
    assert result.failure.failed_step == "subprocess_launch"


def test_write_subprocess_launch_failure_is_safe_to_retry() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError("/private/path/to/gh")):
        result = _api.call_gh("POST", "/repos/owner/repo/issues", {"title": "demo"})
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.cause == "network_provider_failure"
    assert result.failure.retryable is True
    assert result.failure.disposition == "retry"
    assert result.failure.write_outcome == "not_started"


def test_write_subprocess_oserror_after_launch_is_unknown() -> None:
    with patch("subprocess.run", side_effect=OSError("I/O failure after process start")):
        result = _api.call_gh("POST", "/repos/owner/repo/issues", {"title": "demo"})
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.cause == "network_provider_failure"
    assert result.failure.retryable is False
    assert result.failure.disposition == "stop"
    assert result.failure.write_outcome == "unknown"
    assert result.failure.failed_step == "subprocess_execution"


# ---------------------------------------------------------------------------
# Failure envelope fields (write_outcome, completed_steps, failed_step)
# ---------------------------------------------------------------------------


def test_failure_envelope_write_outcome_read_request() -> None:
    stdout = _include_output(403, body={"message": "Forbidden"})
    result = _call("GET", "/repos/owner/repo", fake_stdout=stdout, returncode=1, is_write=False)
    assert result.failure is not None
    assert result.failure.write_outcome is None


def test_failure_envelope_completed_steps_preserved() -> None:
    stdout = _include_output(422, body={"message": "Validation Failed"})
    result = _call(
        "POST",
        "/repos/owner/repo/issues",
        body={},
        fake_stdout=stdout,
        returncode=1,
        completed_steps=["fetch_pr", "update_body"],
        failed_step="add_label",
    )
    assert result.failure is not None
    assert result.failure.completed_steps == ["fetch_pr", "update_body"]
    assert result.failure.failed_step == "add_label"


def test_failure_envelope_asdict_omits_empty_completed_steps() -> None:
    stdout = _include_output(404, body={"message": "Not Found"})
    result = _call("GET", "/repos/owner/repo/pulls/1", fake_stdout=stdout, returncode=1)
    d = result.failure.as_dict()  # type: ignore[union-attr]
    assert "completed_steps" not in d


def test_failure_envelope_asdict_includes_request_id() -> None:
    stdout = _include_output(401, headers={"x-github-request-id": "XY:ZZ:00"}, body={"message": "Bad credentials"})
    result = _call("GET", "/repos/owner/repo", fake_stdout=stdout, returncode=1)
    d = result.failure.as_dict()  # type: ignore[union-attr]
    assert d.get("request_id") == "XY:ZZ:00"


# ---------------------------------------------------------------------------
# Partial success: completed_steps on GraphQL rate-limit
# ---------------------------------------------------------------------------


def test_partial_success_graphql_rate_limit_carries_completed_steps() -> None:
    gql_body = {
        "data": None,
        "errors": [{"type": "RATE_LIMITED", "message": "Rate limited"}],
    }
    stdout = _include_output(200, headers={"x-ratelimit-resource": "graphql"}, body=gql_body)

    proc = _fake_proc(stdout=stdout)
    with patch("subprocess.run", return_value=proc):
        _api.reset_rate_limit_cache()
        result = _api.call_gh(
            "POST",
            "/graphql",
            {"query": "{ viewer { login } }"},
            completed_steps=["preflight_check"],
            failed_step="graphql_project_query",
        )

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.cause == "graphql_primary_rate_limited"
    assert result.failure.completed_steps == ["preflight_check"]
    assert result.failure.failed_step == "graphql_project_query"


def test_graphql_anonymous_query_is_read_only() -> None:
    stdout = _include_output(
        200,
        headers={"x-ratelimit-resource": "graphql"},
        body={"data": None, "errors": [{"type": "RATE_LIMITED", "message": "Rate limited"}]},
    )
    result = _call("POST", "/graphql", {"query": "{ viewer { login } }"}, fake_stdout=stdout, returncode=1)
    assert result.graphql_operation == "query"
    assert result.bucket == "graphql"
    assert result.failure is not None
    assert result.failure.write_outcome is None
    assert result.failure.retryable is True


def test_graphql_mutation_is_write_aware() -> None:
    stdout = _include_output(
        200,
        headers={"x-ratelimit-resource": "graphql"},
        body={"data": None, "errors": [{"type": "RATE_LIMITED", "message": "Rate limited"}]},
    )
    result = _call(
        "POST",
        "/graphql",
        {"query": "mutation UpdateIssue { updateIssue(input: {}) { clientMutationId } }"},
        fake_stdout=stdout,
        returncode=1,
    )
    assert result.graphql_operation == "mutation"
    assert result.failure is not None
    assert result.failure.write_outcome == "unknown"


def test_unknown_graphql_document_fails_closed_as_write() -> None:
    assert _api.infer_graphql_operation_type({"query": "fragment Fields on User { login }"}) == "unknown"
    assert _api.infer_is_write("POST", "/graphql", {"query": "fragment Fields on User { login }"}) is True


def test_structured_legacy_response_wins_over_stderr_phrase() -> None:
    stdout = _include_output(
        403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-resource": "core"},
        body={"message": "API rate limit exceeded"},
    )
    failure = _api.classify_legacy_failure(
        "The token in GH_TOKEN is invalid",
        stdout=stdout,
        is_write=False,
    )
    assert failure.cause == "rest_primary_rate_limited"
    assert failure.fallback_eligible is False


def test_delegated_terminal_envelope_preserves_failure_evidence() -> None:
    stdout = json.dumps({
        "schema_version": 1,
        "ok": False,
        "failure": {
            "cause": "permission_denied",
            "message": "Permission denied",
            "retryable": False,
            "fallback_eligible": True,
            "disposition": "requires_authorization",
            "write_outcome": "not_started",
            "completed_steps": ["resolve_actor"],
            "failed_step": "create_comment",
            "request_id": "request-123",
        },
    })
    failure = _api.classify_legacy_failure(
        "outer wrapper failed",
        stdout=stdout,
        is_write=True,
    )
    assert failure.cause == "permission_denied"
    assert failure.write_outcome == "not_started"
    assert failure.completed_steps == ["resolve_actor"]
    assert failure.failed_step == "create_comment"
    assert failure.request_id == "request-123"


def test_legacy_process_result_merges_outer_and_delegated_failure_evidence() -> None:
    stdout = json.dumps({
        "schema_version": 1,
        "ok": False,
        "failure": {
            "cause": "not_found",
            "message": "Selected comment was deleted",
            "retryable": False,
            "fallback_eligible": False,
            "disposition": "stop",
            "write_outcome": "rejected",
            "completed_steps": ["resolve_actor", "list_comments_page_1"],
            "failed_step": "update_comment",
        },
    })
    result = _api.legacy_process_result(
        1,
        stdout,
        "outer wrapper failed",
        operation="github.issue.close",
        is_write=True,
        completed_steps=["update_labels"],
        failed_step="post_close_comment",
    )
    assert result.completed_steps == ["update_labels", "resolve_actor", "list_comments_page_1"], result
    assert result.failed_step == "post_close_comment", result
    assert result.failure is not None
    assert result.failure.completed_steps == result.completed_steps
    assert result.failure.failed_step == "update_comment"


def test_legacy_write_rate_limit_is_rejected_and_retryable() -> None:
    failure = _api.classify_legacy_failure(
        "GraphQL: API rate limit already exceeded",
        is_write=True,
    )
    assert failure.cause == "graphql_primary_rate_limited"
    assert failure.write_outcome == "rejected"
    assert failure.retryable is True


def test_legacy_unknown_rate_limit_uses_probe_bucket() -> None:
    probe = _api.ApiResult(
        ok=True,
        status=200,
        body={"resources": {"core": {"remaining": 4999}, "graphql": {"remaining": 0, "reset": 1700000000}}},
    )
    failure = _api.classify_legacy_failure(
        "API rate limit exceeded",
        rate_limit_result=probe,
    )
    assert failure.cause == "graphql_primary_rate_limited"
    assert failure.rate_limit == {"resource": "graphql", "remaining": 0, "reset": 1700000000}


def test_legacy_graphql_rate_limit_uses_probe_reset() -> None:
    probe = _api.ApiResult(
        ok=True,
        status=200,
        body={"resources": {"core": {"remaining": 0}, "graphql": {"remaining": 0, "reset": 1700000000}}},
    )
    result = _api.legacy_process_result(
        1,
        "",
        "GraphQL: API rate limit already exceeded",
        operation="github.plan.project_list",
        is_write=False,
        bucket="mixed",
        rate_limit_result=probe,
    )
    assert result.failure is not None
    assert result.failure.cause == "graphql_primary_rate_limited"
    assert result.failure.rate_limit == {
        "resource": "graphql",
        "remaining": 0,
        "reset": 1700000000,
    }
    assert result.bucket == "graphql"


def test_legacy_provider_bucket_replaces_requested_bucket() -> None:
    stdout = _include_output(
        429,
        headers={
            "x-ratelimit-resource": "search",
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": "2000",
        },
        body={"message": "API rate limit exceeded"},
    )
    result = _api.legacy_process_result(
        1,
        stdout,
        "",
        operation="github.api.call",
        is_write=False,
        bucket="rest_core",
    )
    assert result.bucket == "search", result.as_dict()


def test_legacy_deadline_and_cancellation_are_distinct() -> None:
    deadline = _api.classify_legacy_failure("request timed out", is_write=True)
    cancelled = _api.classify_legacy_failure("operation cancelled", is_write=False)
    assert deadline.cause == "deadline_exceeded"
    assert deadline.write_outcome == "unknown"
    assert cancelled.cause == "cancelled"
    assert cancelled.retryable is False


def test_legacy_html_parse_error_is_summarized_as_provider_failure() -> None:
    result = _api.legacy_process_result(
        1,
        "",
        "invalid character '<' looking for beginning of value",
        operation="github.api.legacy_html",
        is_write=False,
    )
    assert result.failure is not None
    assert result.failure.cause == "network_provider_failure"
    assert result.failure.retryable is True
    assert "legacy command omitted --include" in result.failure.message
    assert "invalid character" not in result.failure.message


def test_infer_gh_command_context_distinguishes_project_reads_and_writes() -> None:
    read_context = _api.infer_gh_command_context(["project", "item-list", "12", "--owner", "owner"])
    write_context = _api.infer_gh_command_context(["project", "item-edit", "--id", "PVTI_1"])
    assert read_context.is_write is False
    assert read_context.graphql_operation == "query"
    assert write_context.is_write is True
    assert write_context.graphql_operation == "mutation"


def test_terminal_envelopes_have_stable_fields_and_redaction() -> None:
    success = _api.terminal_success(
        {
            "ok": True,
            "actor": "automation-gh",
            "plans": [],
            "attempts": 2,
            "elapsed_wait": 3.0,
            "retry_eligible": True,
            "last_actor": "automation-gh",
            "last_bucket": "rest_core",
            "outcome_certainty": "confirmed",
            "reconciliation": None,
            "recommended_next_action": "none",
            "effective_deadline": 1000.0,
            "retry_exhausted_reason": None,
        },
        operation="github.plan.index",
        actor="automation-gh",
        expected_actor="shiny-code-bot",
        transport="rest_api",
        bucket="rest_core",
    )
    assert success["schema_version"] == _api.SCHEMA_VERSION
    assert success["exit_code"] == 0
    assert success["disposition"] == "complete"
    assert success["plans"] == []
    assert success["attempts"] == 2
    assert success["elapsed_wait"] == 3.0
    assert success["last_actor"] == "automation-gh"
    assert success["recommended_next_action"] == "none"
    assert "body" not in success

    failure = _api.FailureDetail(
        cause="invalid_credentials",
        message="token=synthetic-secret failed",
        retryable=False,
        fallback_eligible=True,
        disposition="requires_authorization",
        write_outcome="rejected",
    )
    error = _api.terminal_failure(
        failure,
        operation="github.pr.create",
        payload={"detail": "Authorization: Bearer synthetic-secret"},
        transport="gh_cli_wrapper",
        bucket="mixed",
    )
    serialized = json.dumps(error)
    assert "synthetic-secret" not in serialized
    assert error["error_code"] == "invalid_credentials"
    assert error["write_outcome"] == "rejected"
    assert error["fallback_eligible"] is True


def test_cli_argument_failure_emits_terminal_envelope() -> None:
    stdout = StringIO()
    stderr = StringIO()
    with patch.object(sys, "argv", ["github_api.py", "call"]), redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = _api.main()
    payload = json.loads(stdout.getvalue())
    assert exit_code == 2
    assert payload["exit_code"] == 2
    assert payload["operation"] == "github.api.call"
    assert payload["failure"]["cause"] == "validation_error"
    assert "required" in stderr.getvalue()


def test_repository_secret_scanning_alert_path_detection() -> None:
    assert _api.is_repository_secret_scanning_alert_path(
        "/repos/owner/repo/secret-scanning/alerts?state=open"
    )
    assert _api.is_repository_secret_scanning_alert_path(
        "https://api.github.com/repos/owner/repo/secret-scanning/alerts/7/locations"
    )
    assert _api.is_repository_secret_scanning_alert_path(
        "/repos/owner/repo/%73ecret-scanning%252Falerts"
    )
    assert _api.is_repository_secret_scanning_alert_path(
        "/repos/owner/repo/%25252573ecret-scanning%2525252Falerts"
    )
    assert _api.is_repository_secret_scanning_alert_path(
        "/repos/owner/repo/secret-scanning/alerts#fragment"
    )
    assert not _api.is_repository_secret_scanning_alert_path(
        "/orgs/owner/secret-scanning/alerts"
    )
    assert not _api.is_repository_secret_scanning_alert_path(
        "/repos/owner/repo/code-scanning/alerts"
    )


def test_call_cli_refuses_raw_repository_secret_scanning_operations() -> None:
    for method in ("GET", "PATCH"):
        stdout = StringIO()
        stderr = StringIO()
        with (
            patch.object(
                sys,
                "argv",
                [
                    "github_api.py",
                    "call",
                    "--method",
                    method,
                    "/repos/owner/repo/secret-scanning/alerts?hide_secret=true",
                ],
            ),
            patch("subprocess.run") as run,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = _api.main()
        payload = json.loads(stdout.getvalue())
        assert exit_code == 2, payload
        assert payload["failure"]["cause"] == "validation_error", payload
        assert payload["failed_step"] == "input_validation", payload
        assert "secret-scanning-status" in stderr.getvalue()
        assert run.call_count == 0


def test_rate_limit_cli_uses_public_operation_id() -> None:
    proc = _fake_proc(stdout=_include_output(200, body={"resources": {"core": {"remaining": 4999}}}))
    stdout = StringIO()
    stderr = StringIO()
    _api.reset_rate_limit_cache()
    with (
        patch.object(sys, "argv", ["github_api.py", "--gh", "fake-gh", "rate-limit"]),
        patch("subprocess.run", return_value=proc),
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        exit_code = _api.main()
    payload = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert payload["operation"] == "github.api.rate_limit"
    assert stderr.getvalue() == ""


def test_classify_legacy_rejects_malformed_payload_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        stdout_file = root / "stdout"
        stderr_file = root / "stderr"
        payload_file = root / "payload.json"
        stdout_file.write_text("ok\n", encoding="utf-8")
        stderr_file.write_text("", encoding="utf-8")
        payload_file.write_text("[]", encoding="utf-8")
        stdout = StringIO()
        stderr = StringIO()
        argv = [
            "github_api.py",
            "classify-legacy",
            "--returncode",
            "0",
            "--stdout-file",
            str(stdout_file),
            "--stderr-file",
            str(stderr_file),
            "--payload-file",
            str(payload_file),
            "--operation",
            "github.issue.close",
        ]
        with patch.object(sys, "argv", argv), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = _api.main()

    payload = json.loads(stdout.getvalue())
    assert exit_code == 2
    assert payload["exit_code"] == 2
    assert payload["failure"]["cause"] == "validation_error"
    assert payload["failed_step"] == "input_validation"
    assert "JSON object" not in stderr.getvalue()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redact_headers_removes_authorization() -> None:
    headers = {
        "authorization": "Bearer ghp_secret",
        "content-type": "application/json",
        "x-github-token": "ghs_another_secret",
    }
    redacted = _api.redact_headers(headers)
    assert redacted["authorization"] == "[REDACTED]"
    assert redacted["x-github-token"] == "[REDACTED]"
    assert redacted["content-type"] == "application/json"


def test_redact_body_removes_token_keys() -> None:
    body = {
        "token": "ghp_abc123",
        "access_token": "gho_xyz",
        "name": "my-app",
        "nested": {"secret": "top_secret", "label": "ok"},
    }
    redacted = _api.redact_body(body)
    assert redacted["token"] == "[REDACTED]"
    assert redacted["access_token"] == "[REDACTED]"
    assert redacted["name"] == "my-app"
    assert redacted["nested"]["secret"] == "[REDACTED]"
    assert redacted["nested"]["label"] == "ok"


def test_redact_body_removes_sensitive_key_variants() -> None:
    body = {
        "github_token": "synthetic-one",
        "secret_key": "synthetic-two",
        "clientSecret": "synthetic-three",
        "private_key_id": "synthetic-four",
        "credentials": "synthetic-five",
        "monkey": "safe",
    }
    redacted = _api.redact_body(body)
    for key in ("github_token", "secret_key", "clientSecret", "private_key_id", "credentials"):
        assert redacted[key] == "[REDACTED]", (key, redacted)
    assert redacted["monkey"] == "safe"


def test_failure_message_is_redacted_before_consumer_access() -> None:
    failure = _api.FailureDetail(
        cause="network_provider_failure",
        message="request failed with token=synthetic-secret",
        retryable=False,
        fallback_eligible=False,
        disposition="stop",
    )
    assert "synthetic-secret" not in failure.message
    assert "[REDACTED]" in failure.message


def test_failure_message_redacts_credential_and_authorization_params() -> None:
    failure = _api.FailureDetail(
        cause="network_provider_failure",
        message="credentials=synthetic-one authorization=synthetic-two",
        retryable=False,
        fallback_eligible=False,
        disposition="stop",
    )
    assert "synthetic-one" not in failure.message
    assert "synthetic-two" not in failure.message


def test_redact_body_handles_list() -> None:
    body = [{"token": "secret"}, {"label": "ok"}]
    redacted = _api.redact_body(body)
    assert redacted[0]["token"] == "[REDACTED]"
    assert redacted[1]["label"] == "ok"


def test_redact_body_redacts_query_param_in_string() -> None:
    body = "https://api.github.com/repos?token=ghp_abc&other=fine"
    redacted = _api.redact_body(body)
    assert "ghp_abc" not in redacted
    assert "[REDACTED]" in redacted
    assert "other=fine" in redacted


def test_redact_body_redacts_token_shaped_values_in_messages() -> None:
    redacted = _api.redact_body("request failed for ghp_syntheticToken123 and Bearer abc.def.ghi")
    assert "ghp_syntheticToken123" not in redacted
    assert "abc.def.ghi" not in redacted


def test_redact_path_redacts_query_param() -> None:
    path = "/some/endpoint?api_key=SECRET&page=1"
    redacted = _api.redact_path(path)
    assert "SECRET" not in redacted
    assert "[REDACTED]" in redacted
    assert "page=1" in redacted


def test_redact_body_non_sensitive_dict_unchanged() -> None:
    body = {"title": "PR title", "state": "open", "number": 42}
    redacted = _api.redact_body(body)
    assert redacted == body


def test_result_envelope_redacts_body_and_exposes_context() -> None:
    result = _api.ApiResult(
        ok=True,
        status=200,
        body={"token": "synthetic-secret", "name": "safe"},
        operation="github.api.call",
        actor="automation-bot",
        expected_actor="automation-bot",
        host="github.example",
    )
    payload = result.as_dict()
    assert payload["schema_version"] == 1
    assert payload["body"]["token"] == "[REDACTED]"
    assert payload["body"]["name"] == "safe"
    assert payload["operation"] == "github.api.call"
    assert payload["actor"] == "automation-bot"
    assert payload["host"] == "github.example"


# ---------------------------------------------------------------------------
# Rate-limit probe (bounded, one call per process)
# ---------------------------------------------------------------------------


def test_rate_limit_probe_calls_get_rate_limit() -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:  # type: ignore
        calls.append(cmd)
        return _fake_proc(
            stdout=_include_output(200, body={"resources": {"core": {"limit": 5000, "remaining": 4000}}}),
        )

    _api.reset_rate_limit_cache()
    with patch("subprocess.run", side_effect=fake_run):
        result1 = _api.rate_limit_probe()
        result2 = _api.rate_limit_probe()

    # Only one live subprocess call despite two probe calls
    assert len(calls) == 1, f"Expected 1 call, got {len(calls)}"
    assert result1 is result2  # same cached object
    assert result1.ok is True
    assert "/rate_limit" in calls[0]


def test_rate_limit_probe_cache_reset() -> None:
    calls: list[int] = []

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:  # type: ignore
        calls.append(1)
        return _fake_proc(stdout=_include_output(200, body={}))

    _api.reset_rate_limit_cache()
    with patch("subprocess.run", side_effect=fake_run):
        _api.rate_limit_probe()

    _api.reset_rate_limit_cache()
    with patch("subprocess.run", side_effect=fake_run):
        _api.rate_limit_probe()

    assert len(calls) == 2, "Cache reset should allow a second live call"


def test_rate_limit_probe_error_is_cached() -> None:
    """Even a failed probe is cached to avoid hammering a rate-limited API."""
    calls: list[int] = []

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:  # type: ignore
        calls.append(1)
        return _fake_proc(stdout="", stderr="connection refused", returncode=1)

    _api.reset_rate_limit_cache()
    with patch("subprocess.run", side_effect=fake_run):
        r1 = _api.rate_limit_probe()
        r2 = _api.rate_limit_probe()

    assert len(calls) == 1
    assert r1.ok is False
    assert r1 is r2


def test_rate_limit_probe_cache_is_actor_scoped() -> None:
    calls: list[int] = []

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:  # type: ignore
        calls.append(1)
        return _fake_proc(stdout=_include_output(200, body={}))

    _api.reset_rate_limit_cache()
    with patch("subprocess.run", side_effect=fake_run):
        _api.rate_limit_probe(gh_cmd="gh", actor="bot-a")
        _api.rate_limit_probe(gh_cmd="gh", actor="bot-b")

    assert len(calls) == 2


def test_rate_limit_probe_cache_ignores_remaining_timeout_budget() -> None:
    calls: list[float | None] = []

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:  # type: ignore
        calls.append(kwargs.get("timeout"))
        return _fake_proc(stdout=_include_output(200, body={}))

    _api.reset_rate_limit_cache()
    with patch("subprocess.run", side_effect=fake_run):
        first = _api.rate_limit_probe(gh_cmd="gh", actor="bot", timeout_seconds=5.0)
        second = _api.rate_limit_probe(gh_cmd="gh", actor="bot", timeout_seconds=4.0)

    assert calls == [5.0], calls
    assert first is second


# ---------------------------------------------------------------------------
# Output envelope completeness
# ---------------------------------------------------------------------------


def test_success_asdict_no_failure_key() -> None:
    stdout = _include_output(200, body={"id": 1})
    result = _call("GET", "/repos/owner/repo/issues/1", fake_stdout=stdout)
    d = result.as_dict()
    assert "failure" not in d
    assert d["ok"] is True
    assert d["status"] == 200


def test_success_asdict_includes_completed_steps() -> None:
    stdout = _include_output(200, body={"id": 1})
    result = _call(
        "GET",
        "/repos/owner/repo/issues/1",
        fake_stdout=stdout,
        completed_steps=["resolve_repo"],
    )
    assert result.as_dict()["completed_steps"] == ["resolve_repo"]


def test_error_asdict_has_failure_key() -> None:
    stdout = _include_output(404, body={"message": "Not Found"})
    result = _call("GET", "/repos/owner/repo/issues/999", fake_stdout=stdout, returncode=1)
    d = result.as_dict()
    assert d["ok"] is False
    assert "failure" in d
    assert d["failure"]["cause"] == "not_found"
    assert d["failure"]["disposition"] == "stop"


def test_asdict_rate_limit_present_on_success() -> None:
    stdout = _include_output(200, body={})
    result = _call("GET", "/rate_limit", fake_stdout=stdout)
    d = result.as_dict()
    assert "rate_limit" in d
    rl = d["rate_limit"]
    assert rl["limit"] == 5000
    assert rl["remaining"] == 4999


def test_asdict_rate_limit_in_failure_when_exhausted() -> None:
    stdout = _include_output(
        403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-limit": "5000", "x-ratelimit-reset": "1700000001"},
        body={"message": "API rate limit exceeded"},
    )
    result = _call("GET", "/repos/owner/repo", fake_stdout=stdout, returncode=1)
    d = result.as_dict()
    assert "failure" in d
    failure = d["failure"]
    assert failure["cause"] == "rest_primary_rate_limited"
    assert failure.get("rate_limit") is not None
    assert failure["rate_limit"]["remaining"] == 0


def test_operation_marker_is_hidden_and_detectable() -> None:
    body = _api.body_with_operation_marker("## Result\n", "a" * 32)
    assert body.startswith("## Result\n")
    assert _api.operation_marker_comment("a" * 32) in body
    assert _api.body_has_operation_marker(body, "a" * 32) is True
    assert _api.body_has_operation_marker(body, "b" * 32) is False


def test_aggregate_retry_summaries_prefers_write_certainty() -> None:
    def summary(certainty: str) -> Any:
        return _api.RetrySummary(
            attempts=1,
            elapsed_wait=0.0,
            retry_eligible=True,
            last_actor="shiny-code-bot",
            last_bucket="rest_core",
            outcome_certainty=certainty,
            reconciliation=None,
            recommended_next_action="none",
            effective_deadline=2000.0,
        )

    aggregate = _api.aggregate_retry_summaries(
        [summary("not_applicable"), summary("confirmed")]
    )
    assert aggregate is not None
    assert aggregate.outcome_certainty == "confirmed"


class FakeRetryClock:
    def __init__(self, current: float = 1000.0) -> None:
        self.current = current
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.current

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.current += duration


def retry_policy(state_dir: Path, **overrides: Any) -> Any:
    values = {
        "max_wait_seconds": 30.0,
        "max_attempts": 4,
        "base_backoff_seconds": 1.0,
        "max_backoff_seconds": 8.0,
        "jitter_seconds": 0.0,
        "progress_interval_seconds": 10.0,
        "wait_slice_seconds": 1.0,
        "lock_poll_seconds": 0.1,
        "drain_seconds": 0.0,
        "stale_after_seconds": 60.0,
        "state_dir": state_dir,
    }
    values.update(overrides)
    return _api.RetryPolicy(**values)


def retry_runtime(
    clock: FakeRetryClock,
    *,
    cancelled: Any = None,
    progress: Any = None,
) -> Any:
    return _api.RetryRuntime(
        now=clock.now,
        sleep=clock.sleep,
        jitter=lambda _maximum: 0.0,
        cancelled=cancelled or (lambda: False),
        progress=progress or (lambda _event: None),
    )


def retry_failure(
    cause: str,
    *,
    retryable: bool,
    write_outcome: Optional[str] = None,
    actor: str = "shiny-code-bot",
    bucket: str = "rest_core",
    reset: Optional[int] = None,
    retry_after: Optional[int] = None,
) -> Any:
    rate_limit = _api.RateLimitInfo(
        reset=reset,
        retry_after=retry_after,
        resource=bucket,
    )
    failure = _api.FailureDetail(
        cause=cause,
        message=cause,
        retryable=retryable,
        fallback_eligible=False,
        disposition="retry" if retryable else "stop",
        write_outcome=write_outcome,
        rate_limit=rate_limit.as_dict(),
    )
    return _api.ApiResult(
        ok=False,
        status=429 if "rate_limited" in cause else 503,
        body={"message": cause},
        actor=actor,
        expected_actor=actor,
        host="github.com",
        bucket=bucket,
        rate_limit=rate_limit,
        failure=failure,
    )


def retry_success(*, actor: str = "shiny-code-bot", bucket: str = "rest_core") -> Any:
    return _api.ApiResult(
        ok=True,
        status=200,
        body={"ok": True},
        actor=actor,
        expected_actor=actor,
        host="github.com",
        bucket=bucket,
    )


def test_retry_waits_until_primary_reset_and_succeeds() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        calls = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                return retry_failure(
                    "rest_primary_rate_limited",
                    retryable=True,
                    reset=1010,
                )
            return retry_success()

        result = _api.run_with_retry(
            attempt,
            operation="github.api.rate_limit",
            is_write=False,
            actor="shiny-code-bot",
            expected_actor="shiny-code-bot",
            bucket="rest_core",
            retry_policy=retry_policy(Path(temp_dir)),
            retry_runtime=retry_runtime(clock),
        )
        payload = result.as_dict()
        assert result.ok is True, payload
        assert calls == 2, calls
        assert payload["attempts"] == 2, payload
        assert payload["elapsed_wait"] == 10.0, payload
        assert payload["last_actor"] == "shiny-code-bot", payload
        assert payload["last_bucket"] == "rest_core", payload


def test_retry_honors_secondary_retry_after() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        calls = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                return retry_failure(
                    "secondary_rate_limited",
                    retryable=True,
                    retry_after=4,
                )
            return retry_success()

        result = _api.run_with_retry(
            attempt,
            operation="github.api.rate_limit",
            is_write=False,
            actor="shiny-code-bot",
            bucket="rest_core",
            retry_policy=retry_policy(Path(temp_dir)),
            retry_runtime=retry_runtime(clock),
        )
        assert result.ok is True, result.as_dict()
        assert sum(clock.sleeps) == 4.0, clock.sleeps


def test_retry_idempotent_write_recovers_from_unknown_network_failure() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        calls = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                return retry_failure(
                    "network_provider_failure",
                    retryable=False,
                    write_outcome="unknown",
                )
            return retry_success()

        result = _api.run_with_retry(
            attempt,
            operation="github.issue.edit",
            is_write=True,
            actor="shiny-code-bot",
            bucket="rest_core",
            retry_policy=retry_policy(
                Path(temp_dir),
                base_backoff_seconds=0.0,
                max_backoff_seconds=0.0,
            ),
            retry_runtime=retry_runtime(clock),
        )
        payload = result.as_dict()
        assert result.ok is True, payload
        assert calls == 2, calls
        assert payload["attempts"] == 2, payload
        assert payload["outcome_certainty"] == "confirmed", payload


def test_retry_outer_deadline_wins_without_second_call() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        calls = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            return retry_failure(
                "rest_primary_rate_limited",
                retryable=True,
                reset=1010,
            )

        result = _api.run_with_retry(
            attempt,
            operation="github.api.rate_limit",
            is_write=False,
            actor="shiny-code-bot",
            bucket="rest_core",
            deadline_at=1005.0,
            retry_policy=retry_policy(Path(temp_dir)),
            retry_runtime=retry_runtime(clock),
        )
        payload = result.as_dict()
        assert calls == 1, calls
        assert result.failure.cause == "deadline_exceeded", result.failure
        assert payload["retry_exhausted_reason"] == "deadline_exceeded", payload
        assert payload["effective_deadline"] == 1005.0, payload


def test_expired_deadline_blocks_first_write_attempt() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        calls = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            return retry_success()

        result = _api.run_with_retry(
            attempt,
            operation="github.issue.edit",
            is_write=True,
            actor="shiny-code-bot",
            bucket="rest_core",
            deadline_at=999.0,
            retry_policy=retry_policy(Path(temp_dir)),
            retry_runtime=retry_runtime(clock),
        )
        payload = result.as_dict()
        assert calls == 0, calls
        assert payload["attempts"] == 0, payload
        assert payload["write_outcome"] == "not_started", payload
        assert payload["retry_exhausted_reason"] == "deadline_exceeded", payload


def test_precancelled_operation_blocks_first_attempt() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        calls = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            return retry_success()

        result = _api.run_with_retry(
            attempt,
            operation="github.api.rate_limit",
            is_write=False,
            actor="shiny-code-bot",
            bucket="rest_core",
            retry_policy=retry_policy(Path(temp_dir)),
            retry_runtime=retry_runtime(clock, cancelled=lambda: True),
        )
        payload = result.as_dict()
        assert calls == 0, calls
        assert payload["attempts"] == 0, payload
        assert payload["retry_exhausted_reason"] == "cancelled", payload


def test_jitter_is_clamped_to_preserve_feasible_deadline() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        calls = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                return retry_failure(
                    "rest_primary_rate_limited",
                    retryable=True,
                    reset=1005,
                )
            return retry_success()

        runtime = _api.RetryRuntime(
            now=clock.now,
            sleep=clock.sleep,
            jitter=lambda _maximum: 2.0,
            cancelled=lambda: False,
            progress=lambda _event: None,
        )
        result = _api.run_with_retry(
            attempt,
            operation="github.api.rate_limit",
            is_write=False,
            actor="shiny-code-bot",
            bucket="rest_core",
            deadline_at=1006.0,
            retry_policy=retry_policy(Path(temp_dir), jitter_seconds=3.0),
            retry_runtime=runtime,
        )
        assert result.ok is True, result.as_dict()
        assert calls == 2, calls
        assert clock.current < 1006.0, clock.current


def test_retry_timeout_callback_receives_remaining_deadline() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        observed: list[float] = []

        result = _api.run_with_retry(
            lambda: retry_success(),
            operation="github.api.rate_limit",
            is_write=False,
            actor="shiny-code-bot",
            bucket="rest_core",
            deadline_at=1005.0,
            retry_policy=retry_policy(Path(temp_dir)),
            retry_runtime=retry_runtime(clock),
            attempt_with_timeout=lambda timeout: observed.append(timeout) or retry_success(),
        )
        assert result.ok is True, result.as_dict()
        assert observed == [5.0], observed


def test_retry_inherited_deadline_environment_wins() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()

        with patch.dict(os.environ, {"GITHUB_RETRY_DEADLINE_AT": "1002"}):
            result = _api.run_with_retry(
                lambda: retry_failure(
                    "rest_primary_rate_limited",
                    retryable=True,
                    reset=1010,
                ),
                operation="github.api.rate_limit",
                is_write=False,
                actor="shiny-code-bot",
                bucket="rest_core",
                retry_policy=retry_policy(Path(temp_dir)),
                retry_runtime=retry_runtime(clock),
            )
        payload = result.as_dict()
        assert result.failure.cause == "deadline_exceeded", result.failure
        assert payload["effective_deadline"] == 1002.0, payload


def test_retry_cancellation_interrupts_wait() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        calls = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            return retry_failure(
                "rest_primary_rate_limited",
                retryable=True,
                reset=1010,
            )

        result = _api.run_with_retry(
            attempt,
            operation="github.api.rate_limit",
            is_write=False,
            actor="shiny-code-bot",
            bucket="rest_core",
            retry_policy=retry_policy(Path(temp_dir)),
            retry_runtime=retry_runtime(
                clock,
                cancelled=lambda: clock.current >= 1002.0,
            ),
        )
        payload = result.as_dict()
        assert calls == 1, calls
        assert result.failure.cause == "cancelled", result.failure
        assert payload["elapsed_wait"] == 2.0, payload
        assert payload["recommended_next_action"] == "rerun_when_ready", payload


def test_retry_progress_is_periodic_and_stderr_only() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        calls = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                return retry_failure(
                    "rest_primary_rate_limited",
                    retryable=True,
                    reset=1031,
                )
            return retry_success()

        runtime = _api.RetryRuntime(
            now=clock.now,
            sleep=clock.sleep,
            jitter=lambda _maximum: 0.0,
            cancelled=lambda: False,
            progress=_api._default_retry_progress,
        )
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = _api.run_with_retry(
                attempt,
                operation="github.api.rate_limit",
                is_write=False,
                actor="shiny-code-bot",
                bucket="rest_core",
                retry_policy=retry_policy(
                    Path(temp_dir),
                    max_wait_seconds=40.0,
                    wait_slice_seconds=5.0,
                    progress_interval_seconds=10.0,
                ),
                retry_runtime=runtime,
            )
        assert result.ok is True, result.as_dict()
        assert stdout.getvalue() == "", stdout.getvalue()
        lines = [line for line in stderr.getvalue().splitlines() if line]
        assert len(lines) == 4, lines
        assert all(line.startswith("retry: github.api.rate_limit waiting") for line in lines), lines


def test_retry_never_repeats_authentication_failure() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        calls = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            return retry_failure("invalid_credentials", retryable=False)

        result = _api.run_with_retry(
            attempt,
            operation="github.api.rate_limit",
            is_write=False,
            actor="shiny-code-bot",
            bucket="rest_core",
            retry_policy=retry_policy(Path(temp_dir)),
            retry_runtime=retry_runtime(clock),
        )
        assert calls == 1, calls
        assert result.as_dict()["retry_eligible"] is False, result.as_dict()


def test_authorized_actor_change_starts_distinct_retry_context() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        calls = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                result = retry_failure(
                    "secondary_rate_limited",
                    retryable=True,
                    actor="octocat",
                    retry_after=0,
                )
                result.expected_actor = None
                return result
            result = retry_success(actor="octocat")
            result.expected_actor = None
            return result

        result = _api.run_with_retry(
            attempt,
            operation="github.api.rate_limit",
            is_write=False,
            actor="shiny-code-bot",
            expected_actor="shiny-code-bot",
            bucket="rest_core",
            retry_policy=retry_policy(Path(temp_dir)),
            retry_runtime=retry_runtime(clock),
        )
        payload = result.as_dict()
        assert result.ok is True, payload
        assert calls == 2, calls
        assert payload["last_actor"] == "octocat", payload
        assert payload["attempts"] == 2, payload


def test_unknown_operation_fails_closed_without_repeat() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        calls = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            return retry_failure("network_provider_failure", retryable=True)

        result = _api.run_with_retry(
            attempt,
            operation="github.unknown.operation",
            is_write=False,
            actor="shiny-code-bot",
            bucket="rest_core",
            retry_policy=retry_policy(Path(temp_dir)),
            retry_runtime=retry_runtime(clock),
        )
        payload = result.as_dict()
        assert calls == 1, calls
        assert payload["retry_eligible"] is False, payload
        assert payload["disposition"] == "stop", payload
        assert payload["recommended_next_action"] == "add_operation_to_matrix_before_retrying", payload


def test_manual_non_idempotent_operation_never_retries() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        calls = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            return retry_failure(
                "secondary_rate_limited",
                retryable=True,
                write_outcome="rejected",
                retry_after=1,
            )

        result = _api.run_with_retry(
            attempt,
            operation="github.pr.create",
            is_write=True,
            actor="shiny-code-bot",
            bucket="mixed",
            retry_policy=retry_policy(Path(temp_dir)),
            retry_runtime=retry_runtime(clock),
        )
        payload = result.as_dict()
        assert calls == 1, calls
        assert payload["retry_eligible"] is False, payload
        assert payload["disposition"] == "stop", payload


def test_manual_operation_still_rejects_actor_mismatch() -> None:
    result = _api.run_with_retry(
        lambda: retry_success(actor="octocat"),
        operation="github.pr.create",
        is_write=True,
        actor="shiny-code-bot",
        expected_actor="shiny-code-bot",
        bucket="rest_core",
    )
    assert result.ok is False, result.as_dict()
    assert result.failure.cause == "actor_mismatch", result.failure
    assert result.as_dict()["retry_eligible"] is False


def test_manual_operation_still_rejects_bucket_mismatch() -> None:
    result = _api.run_with_retry(
        lambda: retry_success(bucket="search"),
        operation="github.pr.create",
        is_write=True,
        actor="shiny-code-bot",
        expected_actor="shiny-code-bot",
        bucket="rest_core",
    )
    assert result.ok is False, result.as_dict()
    assert result.failure.cause == "retry_context_changed", result.failure
    assert result.as_dict()["retry_eligible"] is False


def test_unknown_write_reconciliation_match_prevents_duplicate_call() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        calls = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            return retry_failure(
                "network_provider_failure",
                retryable=False,
                write_outcome="unknown",
            )

        result = _api.run_with_retry(
            attempt,
            operation="github.plan.create",
            is_write=True,
            actor="shiny-code-bot",
            bucket="rest_core",
            reconcile=lambda _result, _context: _api.ReconciliationDecision(
                "matched",
                body={"number": 42},
                details={"marker": "abc"},
            ),
            retry_policy=retry_policy(Path(temp_dir)),
            retry_runtime=retry_runtime(clock),
        )
        payload = result.as_dict()
        assert result.ok is True, payload
        assert result.body == {"number": 42}, result.body
        assert calls == 1, calls
        assert payload["outcome_certainty"] == "reconciled_applied", payload
        assert payload["reconciliation"]["result"] == "matched", payload


def test_reconciliation_receives_parent_deadline_context() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        policy = retry_policy(Path(temp_dir))
        runtime = retry_runtime(clock)
        observed: list[_api.ReconciliationContext] = []

        result = _api.run_with_retry(
            lambda: retry_failure(
                "network_provider_failure",
                retryable=False,
                write_outcome="unknown",
            ),
            operation="github.comment.issue",
            is_write=True,
            actor="shiny-code-bot",
            bucket="rest_core",
            deadline_at=1005.0,
            reconcile=lambda _result, context: observed.append(context)
            or _api.ReconciliationDecision("no_match"),
            retry_policy=policy,
            retry_runtime=runtime,
        )
        assert result.ok is False, result.as_dict()
        assert len(observed) == 1, observed
        assert observed[0].deadline_at == 1005.0, observed[0]
        assert observed[0].retry_policy is policy, observed[0]
        assert observed[0].retry_runtime is runtime, observed[0]


def test_reconciliation_does_not_start_after_parent_deadline() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        reconciliations = 0

        def attempt() -> Any:
            clock.current = 1005.0
            return retry_failure(
                "deadline_exceeded",
                retryable=False,
                write_outcome="unknown",
            )

        def reconcile(_result: Any, _context: Any) -> Any:
            nonlocal reconciliations
            reconciliations += 1
            return _api.ReconciliationDecision("matched", body={"id": 1})

        result = _api.run_with_retry(
            attempt,
            operation="github.comment.issue",
            is_write=True,
            actor="shiny-code-bot",
            bucket="rest_core",
            deadline_at=1005.0,
            reconcile=reconcile,
            retry_policy=retry_policy(Path(temp_dir)),
            retry_runtime=retry_runtime(clock),
        )
        payload = result.as_dict()
        assert reconciliations == 0, reconciliations
        assert payload["retry_exhausted_reason"] == "deadline_exceeded", payload
        assert payload["reconciliation"]["result"] == "failed", payload


def test_reconciliation_releases_matured_outer_cooldown_lease() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        state_dir = Path(temp_dir)
        policy = retry_policy(
            state_dir,
            max_wait_seconds=5.0,
            base_backoff_seconds=0.0,
            max_backoff_seconds=0.0,
        )
        clock = FakeRetryClock()
        store = _api.SharedCooldownStore(state_dir)
        key = _api._cooldown_key("github.com", "shiny-code-bot", "rest_core")
        store.publish(
            key,
            ready_at=1000.0,
            cause="rest_primary_rate_limited",
            now=999.0,
            policy=policy,
        )
        nested_calls = 0

        def reconcile(_result: Any, context: _api.ReconciliationContext) -> Any:
            nonlocal nested_calls

            def nested_attempt() -> Any:
                nonlocal nested_calls
                nested_calls += 1
                return retry_success()

            nested = _api.run_with_retry(
                nested_attempt,
                operation="github.api.rate_limit",
                is_write=False,
                actor="shiny-code-bot",
                bucket="rest_core",
                deadline_at=context.deadline_at,
                retry_policy=context.retry_policy,
                retry_runtime=context.retry_runtime,
            )
            assert nested.ok is True, nested.as_dict()
            return _api.ReconciliationDecision("no_match")

        result = _api.run_with_retry(
            lambda: retry_failure(
                "network_provider_failure",
                retryable=False,
                write_outcome="unknown",
            ),
            operation="github.comment.issue",
            is_write=True,
            actor="shiny-code-bot",
            expected_actor="shiny-code-bot",
            bucket="rest_core",
            reconcile=reconcile,
            retry_policy=policy,
            retry_runtime=retry_runtime(clock),
        )
        assert result.ok is False, result.as_dict()
        assert nested_calls == 1, nested_calls
        assert result.retry_summary.reconciliation["result"] == "no_match", result.retry_summary


def test_unknown_non_idempotent_write_fails_closed_after_no_match() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        calls = 0
        reconciliations = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            return retry_failure(
                "network_provider_failure",
                retryable=False,
                write_outcome="unknown",
            )

        def reconcile(_result: Any, _context: Any) -> Any:
            nonlocal reconciliations
            reconciliations += 1
            return _api.ReconciliationDecision("no_match", details={"marker": "abc"})

        result = _api.run_with_retry(
            attempt,
            operation="github.plan.create",
            is_write=True,
            actor="shiny-code-bot",
            bucket="rest_core",
            reconcile=reconcile,
            retry_policy=retry_policy(Path(temp_dir)),
            retry_runtime=retry_runtime(clock),
        )
        payload = result.as_dict()
        assert result.ok is False, payload
        assert calls == 1, calls
        assert reconciliations == 1, reconciliations
        assert payload["attempts"] == 1, payload
        assert payload["reconciliation"]["result"] == "no_match", payload
        assert payload["outcome_certainty"] == "reconciled_not_applied", payload
        assert payload["retry_eligible"] is False, payload


def test_rejected_non_idempotent_write_can_retry_without_reconciliation() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = FakeRetryClock()
        calls = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                return retry_failure(
                    "secondary_rate_limited",
                    retryable=True,
                    write_outcome="rejected",
                    retry_after=0,
                )
            return retry_success()

        result = _api.run_with_retry(
            attempt,
            operation="github.issue.create",
            is_write=True,
            actor="shiny-code-bot",
            bucket="rest_core",
            retry_policy=retry_policy(Path(temp_dir)),
            retry_runtime=retry_runtime(clock),
        )
        payload = result.as_dict()
        assert result.ok is True, payload
        assert calls == 2, calls
        assert payload["attempts"] == 2, payload
        assert payload["reconciliation"] is None, payload


def test_shared_cooldown_blocks_second_process_before_remote_call() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        state_dir = Path(temp_dir)
        store = _api.SharedCooldownStore(state_dir)
        key = _api._cooldown_key("github.com", "shiny-code-bot", "rest_core")
        lease = store._open_lock(key, blocking=True)
        assert lease is not None
        script = (
            "import json, pathlib, sys; "
            "sys.path.insert(0, 'github/scripts'); "
            "import github_api; "
            f"store=github_api.SharedCooldownStore(pathlib.Path({str(state_dir)!r})); "
            f"policy=github_api.RetryPolicy(state_dir=pathlib.Path({str(state_dir)!r}), lock_poll_seconds=0.25); "
            f"lease,target,coordinated=store.claim({key!r}, now=1000.0, policy=policy); "
            "print(json.dumps({'lease': lease is not None, 'target': target, 'coordinated': coordinated}))"
        )
        try:
            proc = subprocess.run(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[2],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
        finally:
            store.release(lease)
        payload = json.loads(proc.stdout)
        assert payload == {"lease": False, "target": 1000.25, "coordinated": True}, payload


def test_shared_cooldown_suppresses_remote_call_when_deadline_is_shorter() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        state_dir = Path(temp_dir)
        policy = retry_policy(state_dir)
        clock = FakeRetryClock()
        store = _api.SharedCooldownStore(state_dir)
        key = _api._cooldown_key("github.com", "shiny-code-bot", "rest_core")
        store.publish(
            key,
            ready_at=1010.0,
            cause="rest_primary_rate_limited",
            now=1000.0,
            policy=policy,
        )
        calls = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            return retry_success()

        result = _api.run_with_retry(
            attempt,
            operation="github.api.rate_limit",
            is_write=False,
            actor="shiny-code-bot",
            bucket="rest_core",
            deadline_at=1005.0,
            retry_policy=policy,
            retry_runtime=retry_runtime(clock),
        )
        payload = result.as_dict()
        assert calls == 0, calls
        assert payload["attempts"] == 0, payload
        assert payload["retry_at"] == 1010, payload
        assert payload["retry_exhausted_reason"] == "deadline_exceeded", payload


def test_shared_cooldown_reaching_deadline_blocks_first_remote_call() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        state_dir = Path(temp_dir)
        policy = retry_policy(state_dir)
        clock = FakeRetryClock()
        store = _api.SharedCooldownStore(state_dir)
        key = _api._cooldown_key("github.com", "shiny-code-bot", "rest_core")
        store.publish(
            key,
            ready_at=1005.0,
            cause="rest_primary_rate_limited",
            now=1000.0,
            policy=policy,
        )
        calls = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            return retry_success()

        result = _api.run_with_retry(
            attempt,
            operation="github.api.rate_limit",
            is_write=False,
            actor="shiny-code-bot",
            bucket="rest_core",
            deadline_at=1005.0,
            retry_policy=policy,
            retry_runtime=retry_runtime(clock),
        )
        payload = result.as_dict()
        assert calls == 0, calls
        assert payload["attempts"] == 0, payload
        assert payload["retry_exhausted_reason"] == "deadline_exceeded", payload
        lease, target, coordinated = store.claim(key, now=1005.0, policy=policy)
        assert lease is not None, (target, coordinated)
        store.release(lease)


def test_shared_cooldown_keys_do_not_block_other_buckets() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        state_dir = Path(temp_dir)
        policy = retry_policy(state_dir)
        store = _api.SharedCooldownStore(state_dir)
        search_key = _api._cooldown_key("github.com", "shiny-code-bot", "search")
        rest_key = _api._cooldown_key("github.com", "shiny-code-bot", "rest_core")
        store.publish(
            search_key,
            ready_at=1010.0,
            cause="rest_primary_rate_limited",
            now=1000.0,
            policy=policy,
        )
        lease, target, coordinated = store.claim(rest_key, now=1000.0, policy=policy)
        assert lease is None
        assert target is None
        assert coordinated is False


def test_cooldown_publication_honors_deadline_when_lock_is_busy() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        state_dir = Path(temp_dir)
        policy = retry_policy(state_dir, lock_poll_seconds=0.1)
        clock = FakeRetryClock()
        store = _api.SharedCooldownStore(state_dir)
        store._open_lock = lambda _key, *, blocking: None  # type: ignore[method-assign]
        reason = store.publish(
            _api._cooldown_key("github.com", "shiny-code-bot", "rest_core"),
            ready_at=1010.0,
            cause="rest_primary_rate_limited",
            now=1000.0,
            policy=policy,
            deadline=1000.25,
            runtime=retry_runtime(clock),
        )
        assert reason == "deadline_exceeded", reason
        assert clock.current == 1000.25, clock.current


def test_cooldown_publication_never_shortens_existing_reset() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        state_dir = Path(temp_dir)
        policy = retry_policy(state_dir)
        store = _api.SharedCooldownStore(state_dir)
        key = _api._cooldown_key("github.com", "shiny-code-bot", "rest_core")
        store.publish(
            key,
            ready_at=1010.0,
            cause="rest_primary_rate_limited",
            now=1000.0,
            policy=policy,
        )
        store.publish(
            key,
            ready_at=1005.0,
            cause="secondary_rate_limited",
            now=1001.0,
            policy=policy,
        )
        lease, target, coordinated = store.claim(key, now=1001.0, policy=policy)
        assert lease is None
        assert coordinated is True
        assert target == 1010.0, target


def test_cooldown_claim_releases_lease_after_state_read_error() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        state_dir = Path(temp_dir)
        policy = retry_policy(state_dir)
        store = _api.SharedCooldownStore(state_dir)
        original_read_state = store._read_state

        def fail_read(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError("synthetic state failure")

        store._read_state = fail_read  # type: ignore[method-assign]
        try:
            try:
                store.claim("test-key", now=1000.0, policy=policy)
            except OSError:
                pass
            else:
                raise AssertionError("state read failure must propagate")
        finally:
            store._read_state = original_read_state  # type: ignore[method-assign]
        lease, target, coordinated = store.claim("test-key", now=1000.0, policy=policy)
        assert lease is None
        assert target is None
        assert coordinated is False


def test_unavailable_cooldown_storage_fails_closed_before_repeat() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        blocked = Path(temp_dir) / "not-a-directory"
        blocked.write_text("blocked", encoding="utf-8")
        clock = FakeRetryClock()
        calls = 0

        def attempt() -> Any:
            nonlocal calls
            calls += 1
            return retry_failure("network_provider_failure", retryable=True)

        result = _api.run_with_retry(
            attempt,
            operation="github.api.rate_limit",
            is_write=False,
            actor="shiny-code-bot",
            bucket="rest_core",
            retry_policy=retry_policy(blocked),
            retry_runtime=retry_runtime(clock),
        )
        payload = result.as_dict()
        assert calls == 1, calls
        assert payload["retry_eligible"] is False, payload
        assert payload["retry_exhausted_reason"] == "cooldown_state_unavailable", payload
        assert payload["recommended_next_action"] == "repair_retry_state_directory", payload


def test_stale_cooldown_state_is_ignored() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        state_dir = Path(temp_dir)
        policy = retry_policy(state_dir)
        store = _api.SharedCooldownStore(state_dir)
        key = _api._cooldown_key("github.com", "shiny-code-bot", "rest_core")
        store.publish(
            key,
            ready_at=1010.0,
            cause="rest_primary_rate_limited",
            now=1000.0,
            policy=policy,
        )
        _, state_path = store._paths(key)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["expires_at"] = 999.0
        state_path.write_text(json.dumps(state), encoding="utf-8")
        lease, target, coordinated = store.claim(key, now=1000.0, policy=policy)
        assert lease is None
        assert target is None
        assert coordinated is False


def test_malformed_cooldown_state_is_removed() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        state_dir = Path(temp_dir)
        policy = retry_policy(state_dir)
        store = _api.SharedCooldownStore(state_dir)
        key = _api._cooldown_key("github.com", "shiny-code-bot", "rest_core")
        _, state_path = store._paths(key)
        state_path.write_text(
            json.dumps({"updated_at": "bad", "expires_at": 2000, "ready_at": 1500}),
            encoding="utf-8",
        )
        lease, target, coordinated = store.claim(key, now=1000.0, policy=policy)
        assert lease is None
        assert target is None
        assert coordinated is False
        assert state_path.exists() is False


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> None:
    tests = [
        # parse_gh_include_output
        test_parse_success_200_json_body,
        test_parse_204_no_body,
        test_parse_404_error_body,
        test_parse_crlf_line_endings,
        test_parse_http1_status_line,
        test_parse_non_json_body_returned_as_string,
        test_parse_bare_json_without_status_line,
        test_parse_rate_limit_headers_extracted,
        test_parse_uses_final_http_response_block,
        test_parse_final_text_body_can_contain_http_status_lines,
        test_parse_final_text_body_can_start_with_http_status_line,
        test_parse_redirect_retains_github_diagnostic_headers,
        # build_gh_command (body safety)
        test_build_get_command_no_stdin_flag,
        test_build_post_command_uses_stdin,
        test_build_patch_command_uses_stdin,
        test_build_delete_command_no_stdin_flag,
        test_build_delete_command_can_use_json_stdin,
        test_build_command_prepends_slash_to_bare_path,
        test_build_command_custom_gh_cmd,
        test_build_command_extra_headers,
        test_build_command_includes_default_api_version,
        test_build_post_body_never_on_command_line,
        # call_gh success envelope
        test_call_gh_success_returns_ok_result,
        test_call_gh_result_asdict_has_version,
        test_call_gh_success_extracts_request_id,
        test_call_gh_success_extracts_rate_limit,
        test_call_gh_surfaces_explicit_active_auth_actor,
        test_call_gh_reported_actor_replaces_initial_actor_for_authorized_fallback,
        test_call_gh_unannounced_actor_change_fails_closed_after_write,
        test_call_gh_post_sends_body_as_json_stdin,
        test_call_gh_forwards_explicit_subprocess_environment,
        test_call_gh_timeout_marks_started_write_outcome_unknown,
        test_call_gh_timeout_preserves_authorized_fallback_actor,
        test_call_gh_uses_provider_reported_search_bucket,
        test_retry_fails_closed_on_provider_bucket_mismatch,
        # error classification
        test_classify_401_invalid_credentials,
        test_classify_403_actor_mismatch,
        test_classify_403_permission_denied,
        test_classify_403_rest_rate_limit_via_remaining_zero,
        test_classify_429_rest_rate_limit,
        test_classify_403_secondary_throttle_via_retry_after,
        test_retry_after_on_503_remains_provider_failure,
        test_html_503_is_summarized_without_json_parse_noise,
        test_classify_403_secondary_throttle_via_message,
        test_permission_403_wins_over_incidental_zero_remaining,
        test_classify_200_graphql_rate_limit,
        test_classify_404_not_found,
        test_classify_422_validation_error,
        test_classify_409_conflict,
        test_classify_500_network_error,
        test_write_provider_failure_is_unknown_and_not_directly_retryable,
        test_classify_no_output_returncode_nonzero,
        test_legacy_graphql_rate_limit_does_not_offer_identity_fallback,
        test_classify_subprocess_launch_failure,
        test_write_subprocess_launch_failure_is_safe_to_retry,
        test_write_subprocess_oserror_after_launch_is_unknown,
        # failure envelope fields
        test_failure_envelope_write_outcome_read_request,
        test_failure_envelope_completed_steps_preserved,
        test_failure_envelope_asdict_omits_empty_completed_steps,
        test_failure_envelope_asdict_includes_request_id,
        # partial success
        test_partial_success_graphql_rate_limit_carries_completed_steps,
        test_graphql_anonymous_query_is_read_only,
        test_graphql_mutation_is_write_aware,
        test_unknown_graphql_document_fails_closed_as_write,
        test_structured_legacy_response_wins_over_stderr_phrase,
        test_delegated_terminal_envelope_preserves_failure_evidence,
        test_legacy_process_result_merges_outer_and_delegated_failure_evidence,
        test_legacy_write_rate_limit_is_rejected_and_retryable,
        test_legacy_unknown_rate_limit_uses_probe_bucket,
        test_legacy_graphql_rate_limit_uses_probe_reset,
        test_legacy_provider_bucket_replaces_requested_bucket,
        test_legacy_deadline_and_cancellation_are_distinct,
        test_legacy_html_parse_error_is_summarized_as_provider_failure,
        test_infer_gh_command_context_distinguishes_project_reads_and_writes,
        test_terminal_envelopes_have_stable_fields_and_redaction,
        test_cli_argument_failure_emits_terminal_envelope,
        test_repository_secret_scanning_alert_path_detection,
        test_call_cli_refuses_raw_repository_secret_scanning_operations,
        test_rate_limit_cli_uses_public_operation_id,
        test_classify_legacy_rejects_malformed_payload_file,
        # redaction
        test_redact_headers_removes_authorization,
        test_redact_body_removes_token_keys,
        test_redact_body_removes_sensitive_key_variants,
        test_failure_message_is_redacted_before_consumer_access,
        test_failure_message_redacts_credential_and_authorization_params,
        test_redact_body_handles_list,
        test_redact_body_redacts_query_param_in_string,
        test_redact_body_redacts_token_shaped_values_in_messages,
        test_redact_path_redacts_query_param,
        test_redact_body_non_sensitive_dict_unchanged,
        test_result_envelope_redacts_body_and_exposes_context,
        # probe caching
        test_rate_limit_probe_calls_get_rate_limit,
        test_rate_limit_probe_cache_reset,
        test_rate_limit_probe_error_is_cached,
        test_rate_limit_probe_cache_is_actor_scoped,
        test_rate_limit_probe_cache_ignores_remaining_timeout_budget,
        # output envelope
        test_success_asdict_no_failure_key,
        test_success_asdict_includes_completed_steps,
        test_error_asdict_has_failure_key,
        test_asdict_rate_limit_present_on_success,
        test_asdict_rate_limit_in_failure_when_exhausted,
        test_operation_marker_is_hidden_and_detectable,
        test_aggregate_retry_summaries_prefers_write_certainty,
        # reset-aware retries
        test_retry_waits_until_primary_reset_and_succeeds,
        test_retry_honors_secondary_retry_after,
        test_retry_idempotent_write_recovers_from_unknown_network_failure,
        test_retry_outer_deadline_wins_without_second_call,
        test_expired_deadline_blocks_first_write_attempt,
        test_precancelled_operation_blocks_first_attempt,
        test_jitter_is_clamped_to_preserve_feasible_deadline,
        test_retry_timeout_callback_receives_remaining_deadline,
        test_retry_inherited_deadline_environment_wins,
        test_retry_cancellation_interrupts_wait,
        test_retry_progress_is_periodic_and_stderr_only,
        test_retry_never_repeats_authentication_failure,
        test_authorized_actor_change_starts_distinct_retry_context,
        test_unknown_operation_fails_closed_without_repeat,
        test_manual_non_idempotent_operation_never_retries,
        test_manual_operation_still_rejects_actor_mismatch,
        test_manual_operation_still_rejects_bucket_mismatch,
        test_unknown_write_reconciliation_match_prevents_duplicate_call,
        test_reconciliation_receives_parent_deadline_context,
        test_reconciliation_does_not_start_after_parent_deadline,
        test_reconciliation_releases_matured_outer_cooldown_lease,
        test_unknown_non_idempotent_write_fails_closed_after_no_match,
        test_rejected_non_idempotent_write_can_retry_without_reconciliation,
        test_shared_cooldown_blocks_second_process_before_remote_call,
        test_shared_cooldown_suppresses_remote_call_when_deadline_is_shorter,
        test_shared_cooldown_reaching_deadline_blocks_first_remote_call,
        test_shared_cooldown_keys_do_not_block_other_buckets,
        test_cooldown_publication_honors_deadline_when_lock_is_busy,
        test_cooldown_publication_never_shortens_existing_reset,
        test_cooldown_claim_releases_lease_after_state_read_error,
        test_unavailable_cooldown_storage_fails_closed_before_repeat,
        test_stale_cooldown_state_is_ignored,
        test_malformed_cooldown_state_is_removed,
    ]

    failed: list[str] = []
    for test in tests:
        _api.reset_rate_limit_cache()
        try:
            test()
            print(f"ok {test.__name__}")
        except Exception as exc:
            print(f"FAIL {test.__name__}: {exc}", file=sys.stderr)
            failed.append(test.__name__)

    print()
    if failed:
        print(f"{len(failed)}/{len(tests)} tests FAILED", file=sys.stderr)
        sys.exit(1)
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
