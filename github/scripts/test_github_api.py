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
    for token in cmd:
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
    stdout = _include_output(200, body=gql_body)
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
    stdout = _include_output(200, body=gql_body)

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
        {"ok": True, "actor": "automation-gh", "plans": []},
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
        test_call_gh_post_sends_body_as_json_stdin,
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
        test_legacy_deadline_and_cancellation_are_distinct,
        test_legacy_html_parse_error_is_summarized_as_provider_failure,
        test_infer_gh_command_context_distinguishes_project_reads_and_writes,
        test_terminal_envelopes_have_stable_fields_and_redaction,
        test_cli_argument_failure_emits_terminal_envelope,
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
        # output envelope
        test_success_asdict_no_failure_key,
        test_success_asdict_includes_completed_steps,
        test_error_asdict_has_failure_key,
        test_asdict_rate_limit_present_on_success,
        test_asdict_rate_limit_in_failure_when_exhausted,
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
