#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Deterministic tests for shared paged GitHub REST readers."""

from __future__ import annotations

import json
import argparse
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["CODEX_SKILLS_ENV_FILE"] = "/definitely/missing/codex-skills-test.env"
os.environ["CODEX_AUTOMATION_LOGIN"] = "fixture-automation"
os.environ["CODEX_AUTOMATION_EMAIL"] = "fixture-automation@example.invalid"
os.environ["GH_WITH_ENV_TOKEN_EXPECTED_LOGIN"] = "fixture-automation"

import github_read  # noqa: E402


def include_output(
    body: Any,
    *,
    status: int = 200,
    headers: Optional[dict[str, str]] = None,
    content_type: str = "application/json",
) -> bytes:
    values = {
        "content-type": content_type,
        "x-github-request-id": "READ:123",
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "4999",
        "x-ratelimit-reset": "1784304000",
        "x-ratelimit-used": "1",
        "x-ratelimit-resource": "core",
        **(headers or {}),
    }
    lines = [f"HTTP/2.0 {status} "]
    lines.extend(f"{name}: {value}" for name, value in values.items())
    lines.append("")
    if body is not None:
        lines.append(body if isinstance(body, str) else json.dumps(body))
    return "\n".join(lines).encode()


def process(stdout: bytes, *, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr.encode())


def secret_scanning_reader() -> github_read.GitHubReader:
    return github_read.GitHubReader(
        gh_cmd="fake-gh",
        operation="github.read.redacted_secret_scanning_status",
        gh_prefix_args=github_read.automation_only_gh_prefix_args(),
        strict_actor=True,
    )


def test_issue_reader_paginates_and_filters_pull_requests() -> None:
    first_page = [
        {"number": 1, "title": "pull", "state": "open", "pull_request": {}, "html_url": "https://example/1"},
        {"number": 2, "title": "issue", "state": "open", "labels": [], "html_url": "https://example/2", "updated_at": "2026-07-17T00:00:00Z"},
    ]
    second_page = [
        {"number": 3, "title": "issue two", "state": "open", "labels": [], "html_url": "https://example/3", "updated_at": "2026-07-17T00:00:01Z"},
    ]
    responses = [
        process(include_output(first_page, headers={"link": '<https://api.github.com/repos/o/r/issues?state=open&per_page=2&page=2>; rel="next"'})),
        process(include_output(second_page)),
    ]
    reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="test.issues")
    with patch("subprocess.run", side_effect=responses) as run:
        issues = github_read.list_issues(reader, "o/r", limit=2)
    assert [item["number"] for item in issues] == [2, 3]
    assert run.call_count == 2
    assert reader.completed_steps == ["issues_page_1", "issues_page_2"]


def test_present_terminal_link_header_does_not_fabricate_next_page() -> None:
    page = [{"id": index} for index in range(100)]
    response = process(include_output(page, headers={"link": '<https://api.github.com/items?page=1>; rel="last"'}))
    reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="test.pages")
    with patch("subprocess.run", return_value=response) as run:
        items = reader.paged_json("/items", step_prefix="items")
    assert len(items) == 100
    assert run.call_count == 1


def test_request_diagnostics_include_quota_and_request_id() -> None:
    reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="test.repository")
    response = process(include_output({"full_name": "o/r", "default_branch": "main", "delete_branch_on_merge": True}))
    with patch("subprocess.run", return_value=response):
        data = github_read.repository(reader, "o/r")
    diagnostics = reader.diagnostics()
    assert data["nameWithOwner"] == "o/r"
    assert diagnostics["requestCount"] == 1
    assert diagnostics["requests"][0]["requestId"] == "READ:123"
    assert diagnostics["quota"]["remaining"] == 4999
    assert diagnostics["degraded"] is False


def test_graphql_json_uses_bounded_shared_graphql_transport() -> None:
    response = process(
        include_output(
            {"data": {"repository": {"pullRequest": {"reviewDecision": None}}}},
            headers={"x-ratelimit-resource": "graphql"},
        )
    )
    reader = github_read.GitHubReader(
        gh_cmd="fake-gh", expected_actor="fixture-automation", operation="github.pr.watch"
    )
    with patch("subprocess.run", return_value=response) as run:
        result = reader.graphql_json(
            "query($number: Int!) { repository { pullRequest(number: $number) { reviewDecision } } }",
            {"number": 7},
            step="review_readiness",
            retry_policy=github_read.github_api_core.RetryPolicy(max_wait_seconds=1, max_attempts=1, state_dir=Path(os.environ["GITHUB_RETRY_STATE_DIR"])),
        )
    assert result.ok is True
    command = run.call_args.args[0]
    assert command[command.index("api") + 1 : command.index("--input")] == [
        "--method", "POST", "--include", "-H", f"X-GitHub-Api-Version: {github_read.github_api_core.DEFAULT_API_VERSION}", "/graphql"
    ]
    assert json.loads(run.call_args.kwargs["input"].decode())["variables"] == {"number": 7}
    assert reader.requests[-1]["bucket"] == "graphql"


def test_reader_diagnostics_marks_mixed_rest_and_graphql_buckets() -> None:
    reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="github.pr.watch")
    reader.requests = [
        {"bucket": "rest_core", "transport": "gh_api"},
        {"bucket": "graphql", "transport": "gh_api"},
    ]
    diagnostics = reader.diagnostics()
    assert diagnostics["transport"] == "mixed"
    assert diagnostics["bucket"] == "mixed"


def test_conditional_cache_reuses_304_body_and_scopes_query_and_identity() -> None:
    first = process(include_output({"value": "first"}, headers={"etag": '"v1"'}))
    not_modified = process(include_output(None, status=304, headers={"etag": '"v1"'}), returncode=1)
    changed = process(include_output({"value": "second"}, headers={"etag": '"v2"'}))
    other_page = process(include_output({"value": "page-two"}, headers={"etag": '"p2"'}))
    with tempfile.TemporaryDirectory() as cache_dir, patch.dict(os.environ, {"GITHUB_READ_CACHE_DIR": cache_dir}):
        with patch("subprocess.run", side_effect=[first, not_modified, changed, other_page]) as run:
            one = github_read.GitHubReader(gh_cmd="fake-gh", expected_actor="fixture-automation", operation="github.pr.watch", cache_enabled=True)
            assert one.get_json("/repos/o/r/pulls/1?per_page=100&page=1", step="one") == {"value": "first"}
            two = github_read.GitHubReader(gh_cmd="fake-gh", expected_actor="fixture-automation", operation="github.pr.watch", cache_enabled=True)
            # Force past the short coalescing window: this models a later poll.
            with patch.object(time, "time", return_value=time.time() + 10):
                assert two.get_json("/repos/o/r/pulls/1?per_page=100&page=1", step="two") == {"value": "first"}
            three = github_read.GitHubReader(gh_cmd="fake-gh", expected_actor="fixture-automation", operation="github.pr.watch", cache_enabled=True)
            with patch.object(time, "time", return_value=time.time() + 20):
                assert three.get_json("/repos/o/r/pulls/1?per_page=100&page=1", step="three") == {"value": "second"}
            other = github_read.GitHubReader(gh_cmd="fake-gh", expected_actor="other-actor", operation="github.pr.watch", cache_enabled=True)
            assert other.get_json("/repos/o/r/pulls/1?per_page=100&page=2", step="other") == {"value": "page-two"}
    assert run.call_count == 4
    assert any("If-None-Match: \"v1\"" in arg for arg in run.call_args_list[1].args[0])


def test_matrix_approved_reader_retries_and_reports_attempts() -> None:
    responses = [
        process(
            include_output(
                {"message": "API rate limit exceeded"},
                status=429,
                headers={
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": "0",
                },
            ),
            returncode=1,
        ),
        process(include_output({"full_name": "o/r", "default_branch": "main"})),
    ]
    reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="github.read.repository")
    with tempfile.TemporaryDirectory() as temp_dir:
        policy = github_read.github_api_core.RetryPolicy(
            max_wait_seconds=10.0,
            max_attempts=2,
            base_backoff_seconds=0.0,
            max_backoff_seconds=0.0,
            jitter_seconds=0.0,
            state_dir=Path(temp_dir),
        )
        with (
            patch("subprocess.run", side_effect=responses) as run,
            patch.object(github_read.github_api_core, "default_retry_policy", return_value=policy),
        ):
            data = github_read.repository(reader, "o/r")
    diagnostics = reader.diagnostics()
    request = diagnostics["requests"][0]
    assert data["nameWithOwner"] == "o/r", data
    assert run.call_count == 2, run.call_count
    assert request["attempts"] == 2, request
    assert request["retryEligible"] is True, request
    assert request["outcomeCertainty"] == "confirmed", request
    assert diagnostics["retry"]["attempts"] == 2, diagnostics


def test_reader_aggregates_retry_summary_across_requests() -> None:
    reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="github.ci_diagnose")
    reader.results = [
        github_read.github_api_core.ApiResult(
            ok=True,
            status=200,
            body={},
            retry_summary=github_read.github_api_core.RetrySummary(
                attempts=2,
                elapsed_wait=3.0,
                retry_eligible=True,
                last_actor="fixture-automation",
                last_bucket="rest_core",
                outcome_certainty="confirmed",
                reconciliation=None,
                recommended_next_action="none",
                effective_deadline=1100.0,
            ),
        ),
        github_read.github_api_core.ApiResult(
            ok=True,
            status=200,
            body={},
            retry_summary=github_read.github_api_core.RetrySummary(
                attempts=1,
                elapsed_wait=0.0,
                retry_eligible=True,
                last_actor="fixture-automation",
                last_bucket="rest_core",
                outcome_certainty="confirmed",
                reconciliation=None,
                recommended_next_action="none",
                effective_deadline=1090.0,
            ),
        ),
    ]
    summary = reader.retry_summary()
    assert summary is not None
    assert summary.attempts == 3, summary
    assert summary.elapsed_wait == 3.0, summary
    assert summary.effective_deadline == 1090.0, summary


def test_reader_cli_operations_are_matrix_approved() -> None:
    operations = (
        "github.read.pulls",
        "github.read.pull_checks",
        "github.read.issues",
        "github.read.workflow_runs",
        "github.read.workflow_run",
        "github.read.workflow_jobs",
        "github.read.job_log",
        "github.read.redacted_secret_scanning_status",
        "github.read.repository",
    )
    for operation in operations:
        rule, error = github_read.github_api_core.operation_retry_rule(operation)
        assert error is None, (operation, error)
        assert rule is not None and rule.retry_eligibility == "safe", (operation, rule)


def test_secret_scanning_status_public_repo_is_unavailable_without_alert_request() -> None:
    responses = [
        process(include_output({"login": "fixture-automation"})),
        process(include_output({"private": False, "visibility": "public"})),
    ]
    with patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK": "1"}):
        reader = secret_scanning_reader()
    with patch("subprocess.run", side_effect=responses) as run:
        data = github_read.redacted_secret_scanning_status(reader, "o/r")
    assert data["status"] == "unavailable", data
    assert data["reason"] == "public_repository_alert_api_unavailable", data
    assert data["openAlertCount"] is None, data
    assert "scanningStatus" not in data["repository"], data
    assert run.call_count == 2, run.call_args_list
    assert all(
        call.args[0][1:3] == ["--require-automation-auth", "api"]
        and "env" not in call.kwargs
        for call in run.call_args_list
    )


def test_secret_scanning_status_counts_findings_without_secret_material() -> None:
    responses = [
        process(include_output({"login": "fixture-automation"})),
        process(include_output({
            "private": True,
            "visibility": "private",
            "security_and_analysis": {"secret_scanning": {"status": "enabled"}},
        })),
        process(include_output([{
            "number": 7,
            "state": "open",
            "secret": "ghp_should_never_escape",
            "secret_type": "github_personal_access_token",
            "locations_url": "https://api.github.com/repos/o/r/secret-scanning/alerts/7/locations",
        }])),
    ]
    reader = secret_scanning_reader()
    with patch("subprocess.run", side_effect=responses) as run:
        data = github_read.redacted_secret_scanning_status(reader, "o/r")
    serialized = json.dumps({"data": data, "diagnostics": reader.diagnostics()})
    assert data["status"] == "findings", data
    assert data["openAlertCount"] == 1, data
    assert data["literalValuesHidden"] is True, data
    assert "alerts" not in data, data
    assert "ghp_should_never_escape" not in serialized, serialized
    command = run.call_args_list[-1].args[0]
    assert any("hide_secret=true" in item for item in command), command
    assert not any("hide_secret=True" in item for item in command), command


def test_secret_scanning_status_preserves_hidden_values_on_followup_pages() -> None:
    next_page = "https://api.github.com/repos/o/r/secret-scanning/alerts?page=2&per_page=3"
    responses = [
        process(include_output({"login": "fixture-automation"})),
        process(include_output({"private": True, "visibility": "private"})),
        process(include_output([{"secret": "first"}], headers={"link": f'<{next_page}>; rel="next"'})),
        process(include_output([{"secret": "second"}])),
    ]
    reader = secret_scanning_reader()
    with patch("subprocess.run", side_effect=responses) as run:
        data = github_read.redacted_secret_scanning_status(reader, "o/r", limit=3)
    assert data["status"] == "findings", data
    assert data["openAlertCount"] == 2, data
    serialized = json.dumps({"data": data, "diagnostics": reader.diagnostics()})
    assert "first" not in serialized and "second" not in serialized, serialized
    followup_command = run.call_args_list[-1].args[0]
    assert any("hide_secret=true" in item for item in followup_command), followup_command
    assert any("state=open" in item for item in followup_command), followup_command


def test_secret_scanning_status_empty_alerts_is_clean() -> None:
    responses = [
        process(include_output({"login": "fixture-automation"})),
        process(include_output({"private": True, "visibility": "private"})),
        process(include_output([])),
    ]
    reader = secret_scanning_reader()
    with patch("subprocess.run", side_effect=responses):
        data = github_read.redacted_secret_scanning_status(reader, "o/r")
    assert data["status"] == "clean", data
    assert data["openAlertCount"] == 0, data
    assert data["openAlertCountIsLowerBound"] is False, data


def test_secret_scanning_status_permission_denied_is_unavailable_without_fallback() -> None:
    responses = [
        process(include_output({"login": "fixture-automation"})),
        process(include_output({"private": True, "visibility": "private"})),
        process(
            include_output({"message": "Resource not accessible by integration"}, status=403),
            returncode=1,
        ),
    ]
    with patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK": "1"}):
        reader = secret_scanning_reader()
    with patch("subprocess.run", side_effect=responses) as run:
        data = github_read.redacted_secret_scanning_status(reader, "o/r")
    assert data["status"] == "unavailable", data
    assert data["reason"] == "permission_limited", data
    assert reader.diagnostics()["degradedComponents"] == ["security_alerts_page_1"]
    assert run.call_count == 3, run.call_args_list
    assert all(
        call.args[0][1:3] == ["--require-automation-auth", "api"]
        and "env" not in call.kwargs
        for call in run.call_args_list
    )


def test_secret_scanning_status_404_is_ambiguous_and_never_clean() -> None:
    responses = [
        process(include_output({"login": "fixture-automation"})),
        process(include_output({"private": True, "visibility": "private"})),
        process(include_output({"message": "Not Found"}, status=404), returncode=1),
    ]
    reader = secret_scanning_reader()
    with patch("subprocess.run", side_effect=responses):
        data = github_read.redacted_secret_scanning_status(reader, "o/r")
    assert data["status"] == "unavailable", data
    assert data["reason"] == "alert_endpoint_404_ambiguous", data
    assert data["openAlertCount"] is None, data


def test_secret_scanning_status_repository_permission_failure_is_unavailable() -> None:
    responses = [
        process(include_output({"login": "fixture-automation"})),
        process(
            include_output({"message": "Resource not accessible by integration"}, status=403),
            returncode=1,
        ),
    ]
    reader = secret_scanning_reader()
    with patch("subprocess.run", side_effect=responses) as run:
        data = github_read.redacted_secret_scanning_status(reader, "o/r")
    assert data["status"] == "unavailable", data
    assert data["reason"] == "repository_permission_or_visibility_limited", data
    assert data["repository"]["visibility"] == "unknown", data
    assert run.call_count == 2, run.call_args_list


def test_secret_scanning_status_disabled_metadata_is_not_enabled_without_alert_request() -> None:
    responses = [
        process(include_output({"login": "fixture-automation"})),
        process(include_output({
            "private": True,
            "visibility": "private",
            "security_and_analysis": {"secret_scanning": {"status": "disabled"}},
        })),
    ]
    reader = secret_scanning_reader()
    with patch("subprocess.run", side_effect=responses) as run:
        data = github_read.redacted_secret_scanning_status(reader, "o/r")
    assert data["status"] == "not_enabled", data
    assert data["reason"] == "scanning_disabled", data
    assert "scanningStatus" not in data["repository"], data
    assert run.call_count == 2, run.call_args_list


def test_secret_scanning_status_actor_mismatch_fails_closed_as_unavailable() -> None:
    reader = secret_scanning_reader()
    with patch("subprocess.run", return_value=process(include_output({"login": "octocat"}))) as run:
        data = github_read.redacted_secret_scanning_status(reader, "o/r")
    assert data["status"] == "unavailable", data
    assert data["reason"] == "actor_mismatch", data
    diagnostics = reader.diagnostics()
    assert diagnostics["actor"] == "octocat"
    assert diagnostics["requests"][0]["lastActor"] == "octocat"
    assert diagnostics["retry"]["last_actor"] == "octocat"
    assert diagnostics["degradedComponents"] == ["actor"]
    assert run.call_count == 1


def test_secret_scanning_status_rejects_actor_change_after_preflight() -> None:
    responses = [
        process(include_output({"login": "fixture-automation"})),
        process(
            include_output({"private": True, "visibility": "private"}),
            stderr=(
                "warning: automation gh request was rate-limited; explicitly authorized "
                "active-auth fallback; retrying with the active gh account 'octocat'"
            ),
        ),
    ]
    reader = secret_scanning_reader()
    with patch("subprocess.run", side_effect=responses) as run:
        try:
            github_read.redacted_secret_scanning_status(reader, "o/r")
        except github_read.GitHubReadError as exc:
            assert exc.result.failure is not None
            assert exc.result.failure.cause == "actor_mismatch"
        else:
            raise AssertionError("expected actor change to fail closed")
    assert run.call_count == 2, run.call_args_list


def test_secret_scanning_status_rejects_unbounded_limits_before_api_calls() -> None:
    reader = secret_scanning_reader()
    for limit in (0, 1001):
        with patch("subprocess.run") as run:
            try:
                github_read.redacted_secret_scanning_status(reader, "o/r", limit=limit)
            except ValueError as exc:
                assert "between 1 and 1000" in str(exc)
            else:
                raise AssertionError("expected bounded secret-scanning limit failure")
            assert run.call_count == 0


def test_secret_scanning_cli_reports_invalid_limit_as_input_validation() -> None:
    stdout = StringIO()
    stderr = StringIO()
    with (
        patch.object(
            sys,
            "argv",
            [
                "github_read.py",
                "--repo",
                "o/r",
                "secret-scanning-status",
                "--limit",
                "1001",
            ],
        ),
        patch("subprocess.run") as run,
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        exit_code = github_read.main()
    payload = json.loads(stdout.getvalue())
    assert exit_code == 2, payload
    assert payload["failure"]["cause"] == "validation_error", payload
    assert payload["failed_step"] == "input_validation", payload
    assert "between 1 and 1000" in stderr.getvalue()
    assert run.call_count == 0


def test_reader_failed_request_dominates_aggregate_certainty() -> None:
    reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="github.ci_diagnose")
    successful = github_read.github_api_core.ApiResult(
        ok=True,
        status=200,
        body={},
        retry_summary=github_read.github_api_core.RetrySummary(
            attempts=1,
            elapsed_wait=0.0,
            retry_eligible=True,
            last_actor="fixture-automation",
            last_bucket="rest_core",
            outcome_certainty="confirmed",
            reconciliation=None,
            recommended_next_action="none",
            effective_deadline=1100.0,
        ),
    )
    failed = github_read.github_api_core.ApiResult(
        ok=False,
        status=403,
        body={},
        retry_summary=github_read.github_api_core.RetrySummary(
            attempts=1,
            elapsed_wait=0.0,
            retry_eligible=False,
            last_actor="fixture-automation",
            last_bucket="rest_core",
            outcome_certainty="not_applicable",
            reconciliation=None,
            recommended_next_action="inspect_last_failure",
            effective_deadline=1090.0,
            exhausted_reason="not_retryable",
        ),
    )
    reader.results = [successful, failed]
    reader.failed_results = [failed]
    summary = reader.retry_summary()
    assert summary is not None
    assert summary.outcome_certainty == "not_applicable", summary
    assert summary.recommended_next_action == "inspect_last_failure", summary


def test_request_diagnostic_redacts_failure_messages() -> None:
    result = github_read.github_api_core.ApiResult(
        ok=False,
        status=500,
        body=None,
        failure=github_read.github_api_core.FailureDetail(
            cause="network_provider_failure",
            message="request failed token=ghp_should_never_escape",
            retryable=True,
            fallback_eligible=False,
            disposition="retry_same_actor",
        ),
    )
    diagnostic = github_read.request_diagnostic(
        result,
        method="GET",
        path="/repos/o/r",
        step="repository",
    )
    assert "ghp_should_never_escape" not in diagnostic["message"]
    assert "[REDACTED]" in diagnostic["message"]


def test_explicit_active_auth_actor_is_visible_and_degraded() -> None:
    reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="test.actor")
    response = process(
        include_output({"full_name": "o/r", "default_branch": "main", "delete_branch_on_merge": True}),
        stderr="warning: no automation gh token found; explicitly authorized active-auth fallback; using the active gh account 'octocat'",
    )
    with patch("subprocess.run", return_value=response):
        github_read.repository(reader, "o/r")
    diagnostics = reader.diagnostics()
    assert diagnostics["actor"] == "octocat"
    assert diagnostics["expectedActor"] == "fixture-automation"
    assert diagnostics["degraded"] is True
    assert diagnostics["degradedComponents"] == ["actor"]


def test_permission_failure_is_explicit_and_degraded() -> None:
    reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="test.runs")
    response = process(
        include_output({"message": "Resource not accessible by integration"}, status=403),
        returncode=1,
    )
    with patch("subprocess.run", return_value=response):
        try:
            github_read.list_workflow_runs(reader, "o/r")
        except github_read.GitHubReadError as exc:
            assert exc.result.failure is not None
            assert exc.result.failure.cause == "permission_denied"
            assert exc.diagnostics["degraded"] is True
            assert exc.diagnostics["degradedComponents"] == ["workflow_runs_page_1"]
        else:
            raise AssertionError("expected GitHubReadError")


def test_rate_limit_failure_preserves_reset_metadata() -> None:
    reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="test.rate-limit")
    response = process(
        include_output(
            {"message": "API rate limit exceeded"},
            status=403,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1784304999"},
        ),
        returncode=1,
    )
    with patch("subprocess.run", return_value=response):
        try:
            github_read.list_issues(reader, "o/r")
        except github_read.GitHubReadError as exc:
            request = exc.diagnostics["requests"][0]
            assert request["cause"] == "rest_primary_rate_limited"
            assert request["retryAt"] == 1784304999
            assert request["quota"]["remaining"] == 0
        else:
            raise AssertionError("expected GitHubReadError")


def test_pull_request_normalization_preserves_final_merge_identity() -> None:
    normalized = github_read.normalize_pull_request(
        {
            "number": 42,
            "state": "closed",
            "merged": True,
            "merged_at": "2026-07-17T19:00:00Z",
            "merge_commit_sha": "a" * 40,
            "head": {"ref": "feature", "sha": "b" * 40, "repo": {"full_name": "owner/repo"}},
            "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
        }
    )

    assert normalized["mergedAt"] == "2026-07-17T19:00:00Z"
    assert normalized["mergeCommitOid"] == "a" * 40
    assert normalized["headRefOid"] == "b" * 40


def test_pull_checks_share_paged_readers_and_ids() -> None:
    pull = {
        "number": 7,
        "title": "Demo",
        "state": "open",
        "draft": False,
        "html_url": "https://github.com/o/r/pull/7",
        "head": {"sha": "abc", "ref": "feature", "repo": {"full_name": "o/r"}},
        "base": {"ref": "main", "repo": {"full_name": "o/r"}},
    }
    checks = {
        "check_runs": [{
            "id": 11,
            "name": "tests",
            "status": "completed",
            "conclusion": "failure",
            "details_url": "https://github.com/o/r/actions/runs/22/job/33",
            "check_suite": {"id": 44},
            "app": {"name": "GitHub Actions"},
        }],
    }
    responses = [
        process(include_output(pull)),
        process(include_output(checks)),
        process(include_output([])),
        process(include_output({"state": "success"})),
        process(include_output({"workflow_runs": []})),
    ]
    reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="test.checks")
    with patch("subprocess.run", side_effect=responses):
        payload = github_read.pull_request_checks(reader, "o/r", 7)
    check = payload["checkRuns"][0]
    assert check["runId"] == 22
    assert check["jobId"] == 33
    assert check["checkSuiteId"] == 44
    assert payload["summary"]["failingCount"] == 1
    assert reader.completed_steps == ["pull_request", "check_runs_page_1", "commit_statuses_page_1", "combined_status", "check_workflow_runs_page_1"]


def test_pull_checks_keep_only_latest_status_per_context() -> None:
    pull_data = {
        "number": 7,
        "title": "Demo",
        "state": "open",
        "draft": False,
        "html_url": "https://github.com/o/r/pull/7",
        "head": {"sha": "abc", "ref": "feature", "repo": {"full_name": "o/r"}},
        "base": {"ref": "main", "repo": {"full_name": "o/r"}},
    }
    statuses = [
        {"id": 1, "context": "external-ci", "state": "failure", "updated_at": "2026-07-17T00:00:00Z"},
        {"id": 2, "context": "external-ci", "state": "success", "updated_at": "2026-07-17T00:01:00Z"},
    ]
    responses = [
        process(include_output(pull_data)),
        process(include_output({"check_runs": []})),
        process(include_output(statuses)),
        process(include_output({"state": "success"})),
        process(include_output({"workflow_runs": []})),
    ]
    reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="test.statuses")
    with patch("subprocess.run", side_effect=responses):
        payload = github_read.pull_request_checks(reader, "o/r", 7)
    assert len(payload["statuses"]) == 1
    assert payload["statuses"][0]["state"] == "success"
    assert payload["summary"]["failingCount"] == 0


def test_pull_checks_preserve_check_runs_when_status_permission_is_missing() -> None:
    pull_data = {
        "number": 7,
        "title": "Demo",
        "state": "open",
        "draft": False,
        "html_url": "https://github.com/o/r/pull/7",
        "head": {"sha": "abc", "ref": "feature", "repo": {"full_name": "o/r"}},
        "base": {"ref": "main", "repo": {"full_name": "o/r"}},
    }
    responses = [
        process(include_output(pull_data)),
        process(include_output({"check_runs": [{"id": 11, "name": "tests", "status": "completed", "conclusion": "failure"}]})),
        process(include_output({"message": "Resource not accessible by integration"}, status=403), returncode=1),
        process(include_output({"state": "failure"})),
        process(include_output({"workflow_runs": []})),
    ]
    reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="test.partial")
    with patch("subprocess.run", side_effect=responses):
        payload = github_read.pull_request_checks(reader, "o/r", 7)
    assert len(payload["checkRuns"]) == 1
    assert payload["summary"]["failingCount"] == 1
    assert payload["summary"]["countsComplete"] is False
    assert payload["summary"]["statusCount"] is None
    assert payload["summary"]["availability"]["commitStatuses"] is False
    assert reader.diagnostics()["degraded"] is True


def test_shape_failure_marks_diagnostics_degraded() -> None:
    reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="test.shape")
    with patch("subprocess.run", return_value=process(include_output({"not": "a list"}))):
        try:
            reader.paged_json("/items", step_prefix="items")
        except github_read.GitHubReadShapeError:
            pass
        else:
            raise AssertionError("expected GitHubReadShapeError")
    assert reader.diagnostics()["degraded"] is True
    assert reader.diagnostics()["degradedComponents"] == ["items"]


def test_direct_checks_inventory_workflows_before_job_checks() -> None:
    spec = importlib.util.spec_from_file_location("gh_pr_checks_fixture", Path(__file__).with_name("gh-pr.py"))
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    sha = "a" * 40
    for state, conclusion in (("queued", None), ("in_progress", None), ("completed", "failure"),
                              ("completed", "success"), (None, None), ("unavailable", None)):
        run_data = {"id": 22, "head_sha": sha, "status": state, "conclusion": conclusion}
        workflow_response = process(include_output({"workflow_runs": [run_data] if state else []}))
        if state == "unavailable":
            workflow_response = process(include_output({"message": "Resource not accessible by integration"}, status=403), returncode=1)
        responses = [process(include_output({"number": 7, "head": {"sha": sha}})),
                     process(include_output({"check_runs": []})), process(include_output([])),
                     process(include_output({"state": "pending"})), workflow_response]
        with patch("subprocess.run", side_effect=responses) as transport:
            payload = helper.cmd_checks(argparse.Namespace(repo="o/r", pr="7"))
        summary = payload["summary"]
        assert summary["pendingCount"] == int(state in {"queued", "in_progress"}), state
        assert summary["failingCount"] == int(conclusion == "failure"), state
        assert summary["countsComplete"] is (state != "unavailable"), state
        assert summary["countsAreLowerBounds"] is (state == "unavailable"), state
        if state == "unavailable":
            assert "workflowSelection" in summary["unavailableComponents"]
        command = transport.call_args.args[0]
        endpoint = next(arg for arg in command if arg.startswith("/repos/o/r/actions/runs"))
        assert f"head_sha={sha}" in endpoint


def test_workflow_metadata_jobs_and_text_log_normalize() -> None:
    responses = [
        process(include_output({
            "id": 22,
            "name": "CI",
            "display_title": "Run tests",
            "status": "completed",
            "conclusion": "failure",
            "head_branch": "feature",
            "head_sha": "abc",
            "event": "pull_request",
            "created_at": "2026-07-17T00:00:00Z",
            "html_url": "https://github.com/o/r/actions/runs/22",
            "run_attempt": 2,
        })),
        process(include_output({"jobs": [{"id": 33, "run_id": 22, "name": "tests", "status": "completed", "conclusion": "failure"}]})),
        process(b"api flags: --allow-escape-sequences"),
        process(include_output("line one\nerror: failed\n", content_type="text/plain")),
    ]
    reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="test.actions")
    with patch("subprocess.run", side_effect=responses):
        run = github_read.workflow_run(reader, "o/r", 22)
        jobs = github_read.workflow_jobs(reader, "o/r", 22)
        log = github_read.job_log(reader, "o/r", 33)
    assert run["workflowName"] == "CI"
    assert run["runAttempt"] == 2
    assert jobs[0]["id"] == 33
    assert "error: failed" in log


def test_text_logs_opt_in_and_remove_terminal_commands() -> None:
    reader = github_read.GitHubReader(
        gh_cmd="fake-wrapper", gh_prefix_args=["--reader-option"],
        operation="test.actions",
    )
    responses = [
        process(b"api flags: --allow-escape-sequences"),
        process(include_output(
            "\x1b[31merror: failed\x1b[0m\n"
            "\x1b]8;;https://example.invalid\x1b\\details\x1b]8;;\x07\n"
            "\x1b[2Jnext\x00\x07\tline",
            content_type="text/plain",
        )),
        process(include_output("second log", content_type="text/plain")),
        process(include_output({"ok": True})),
    ]
    with patch("subprocess.run", side_effect=responses) as run:
        log = github_read.job_log(reader, "o/r", 33)
        assert github_read.job_log(reader, "o/r", 34) == "second log"
        assert reader.get_json("/repos/o/r", step="metadata") == {"ok": True}
    assert log == "error: failed\ndetails\nnext\tline"
    assert run.call_args_list[0].args[0] == [
        "fake-wrapper", "--reader-option", "api", "--help",
    ]
    for call in run.call_args_list[1:3]:
        assert "--allow-escape-sequences" in call.args[0]
    assert "--allow-escape-sequences" not in run.call_args_list[3].args[0]
    assert reader.diagnostics()["degraded"] is False


def test_text_logs_support_older_cli_and_preserve_failures() -> None:
    reader = github_read.GitHubReader(gh_cmd="old-gh", operation="test.actions")
    responses = [
        process(b"api flags: --include"),
        process(include_output("\x1b[31mlegacy failure\x1b[0m", content_type="text/plain")),
        process(include_output({"message": "Forbidden"}, status=403), returncode=1),
    ]
    with patch("subprocess.run", side_effect=responses) as run:
        assert github_read.job_log(reader, "o/r", 33) == "legacy failure"
        try:
            github_read.job_log(reader, "o/r", 34)
        except github_read.GitHubReadError as exc:
            assert exc.result.status == 403
        else:
            raise AssertionError("Denied logs must remain a read failure")
    assert all("--allow-escape-sequences" not in call.args[0] for call in run.call_args_list)
    assert reader.diagnostics()["degraded"] is True


def test_text_log_capability_probe_failure_is_visible() -> None:
    for failure in (
        subprocess.TimeoutExpired(["fake-gh", "api", "--help"], 60),
        process(b"", returncode=1, stderr="private configuration detail"),
    ):
        reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="test.actions")
        responses = [failure, process(include_output("plain log", content_type="text/plain"))]
        with patch("subprocess.run", side_effect=responses):
            assert github_read.job_log(reader, "o/r", 33) == "plain log"
        diagnostics = reader.diagnostics()
        assert diagnostics["degraded"] is True
        assert "log_cli_capability" in diagnostics["degradedComponents"]
        assert "private configuration detail" not in json.dumps(diagnostics)


def test_poll_delay_respects_server_interval_and_adds_only_positive_jitter() -> None:
    assert github_read.poll_interval({"x-poll-interval": "90"}) == 90
    for value in ("nan", "inf", "-1", "bogus"):
        assert github_read.poll_interval({"x-poll-interval": value}) == 0
    with patch.object(github_read.random, "uniform", return_value=2.0):
        assert github_read.poll_delay(60, 90) == 92


def test_cache_honors_poll_interval_across_readers_and_304() -> None:
    first = process(include_output({"value": 1}, headers={"etag": '"v1"', "x-poll-interval": "60"}))
    unchanged = process(include_output(None, status=304, headers={"x-poll-interval": "120"}), returncode=1)
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"GITHUB_READ_CACHE_DIR": tmp}):
        with patch("subprocess.run", side_effect=[first, unchanged]) as run, patch.object(time, "time", return_value=1000):
            reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="github.pr.watch", cache_enabled=True)
            assert reader.get_json("/poll", step="first") == {"value": 1}
            with patch.object(time, "time", return_value=1030):
                assert reader.get_json("/poll", step="coalesced") == {"value": 1}
                assert run.call_count == 1
            with patch.object(time, "time", return_value=1061):
                assert reader.get_json("/poll", step="revalidated") == {"value": 1}
            with patch.object(time, "time", return_value=1150):
                another = github_read.GitHubReader(gh_cmd="fake-gh", operation="github.pr.watch", cache_enabled=True)
                assert another.get_json("/poll", step="later") == {"value": 1}
            assert run.call_count == 2


def test_cache_lock_wait_expires_without_remote_call() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"GITHUB_READ_CACHE_DIR": tmp}):
        reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="github.workflow.run.read",
                                         cache_enabled=True, deadline_at=time.time() + 0.1)
        cache = github_read.ConditionalResponseCache.from_reader(reader)
        assert cache is not None
        _, lock_path = cache._paths("/poll", {"Accept": "application/vnd.github+json"})
        with lock_path.open("a+") as held:
            github_read.fcntl.flock(held, github_read.fcntl.LOCK_EX)
            real_flock = github_read.fcntl.flock

            def bounded_lock(fd: int, flags: int) -> None:
                assert flags & github_read.fcntl.LOCK_NB, "deadline-bound cache read must not block indefinitely"
                real_flock(fd, flags)

            with patch("subprocess.run") as remote, patch.object(github_read.fcntl, "flock", side_effect=bounded_lock):
                started = time.monotonic()
                try:
                    reader.request("GET", "/poll", step="poll")
                except github_read.GitHubReadError as exc:
                    assert exc.result.failure is not None
                    assert exc.result.failure.cause == "deadline_exceeded", exc.result.as_dict()
                else:
                    raise AssertionError("cache lock must not outlive the request deadline")
                assert time.monotonic() - started < 5
                remote.assert_not_called()


def test_cache_storage_failure_falls_back_to_same_deadline_and_actor() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "cache"
        root.write_text("not a directory")
        reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="github.workflow.run.read",
                                         cache_enabled=True, strict_actor=True, actor="fixture-automation", deadline_at=time.time() + 10)
        with patch.dict(os.environ, {"GITHUB_READ_CACHE_DIR": str(root)}), patch(
            "subprocess.run", return_value=process(include_output({"value": 1}))
        ) as remote, patch.object(github_read.github_api_core, "call_gh_with_retry", wraps=github_read.github_api_core.call_gh_with_retry) as transport:
            result = reader.request("GET", "/poll", step="poll")
            assert result.body == {"value": 1}
            assert result.headers["x-codex-cache"] == "unavailable"
            assert remote.call_count == 1
            assert transport.call_args.kwargs["deadline_at"] == reader.deadline_at
            assert transport.call_args.kwargs["actor"] == reader.request_actor
            assert transport.call_args.kwargs["expected_actor"] == reader.expected_actor
        # Storage fallback must not return a cached success for permission errors.
        with patch.dict(os.environ, {"GITHUB_READ_CACHE_DIR": str(root)}), patch(
            "subprocess.run", return_value=process(include_output({"message": "Resource not accessible by integration"}, status=403), returncode=1)
        ):
            try:
                reader.request("GET", "/poll", step="denied")
            except github_read.GitHubReadError as exc:
                assert exc.result.failure is not None
                assert exc.result.failure.cause == "permission_denied"
            else:
                raise AssertionError("permission failure must survive storage fallback")


def test_cache_write_failure_does_not_repeat_completed_get() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"GITHUB_READ_CACHE_DIR": tmp}):
        reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="github.workflow.run.read", cache_enabled=True)
        response = process(include_output({"value": 1}, headers={"etag": '"v1"', "x-poll-interval": "60"}))
        with patch.object(github_read.ConditionalResponseCache, "_write", side_effect=OSError("disk full")), patch(
            "subprocess.run", return_value=response
        ) as remote:
            result = reader.request("GET", "/poll", step="poll")
            assert result.body == {"value": 1}
            assert result.headers["x-codex-cache"] == "unavailable"
            assert remote.call_count == 1


def test_revalidation_readers_do_not_hold_cache_lock_during_transport_wait() -> None:
    with tempfile.TemporaryDirectory() as cache_dir, patch.dict(os.environ, {"GITHUB_READ_CACHE_DIR": cache_dir}):
        entered, release = threading.Event(), threading.Event()
        first = github_read.GitHubReader(expected_actor="fixture-automation", cache_enabled=True,
                                        cache_revalidate=True, deadline_at=time.time() + 5)
        second = github_read.GitHubReader(expected_actor="fixture-automation", cache_enabled=True,
                                         cache_revalidate=True, deadline_at=time.time() + 1)
        def delayed(*_args: Any, **_kwargs: Any) -> github_read.github_api_core.ApiResult:
            entered.set()
            assert release.wait(3), "second revalidator queued behind the first transport"
            return github_read.github_api_core.ApiResult(ok=True, status=200, body={"value": 1}, headers={"etag": '"v1"'})
        response = github_read.github_api_core.ApiResult(ok=True, status=200, body={"value": 2}, headers={"etag": '"v2"'})
        def immediate(*_args: Any, **_kwargs: Any) -> github_read.github_api_core.ApiResult:
            assert time.time() < second.deadline_at, "cache lock consumed the second reader's transport deadline"
            return response
        results: list[Any] = []
        with patch.object(first, "_transport_request", side_effect=delayed), patch.object(
            second, "_transport_request", side_effect=immediate
        ) as remote:
            worker = threading.Thread(target=lambda: results.append(first.get_json("/repos/o/r/issues/1", step="one")))
            worker.start()
            try:
                assert entered.wait(1)
                assert second.get_json("/repos/o/r/issues/1", step="two") == {"value": 2}
                assert remote.call_args.kwargs["extra_headers"]["Accept"] == "application/vnd.github+json"
            finally:
                release.set()
                worker.join(timeout=3)
            assert not worker.is_alive() and results == [{"value": 1}]
        # Forced reads can complete out of order, so they must not seed a
        # watcher entry that may serve a recent body without revalidation.
        watcher = github_read.GitHubReader(expected_actor="fixture-automation", cache_enabled=True)
        latest = github_read.github_api_core.ApiResult(ok=True, status=200, body={"value": 3}, headers={"etag": '"v3"'})
        with patch.object(watcher, "_transport_request", return_value=latest) as remote:
            assert watcher.get_json("/repos/o/r/issues/1", step="watch") == {"value": 3}
            assert remote.call_count == 1
            assert "If-None-Match" not in remote.call_args.kwargs["extra_headers"]


def main() -> None:
    tests: list[Callable[[], None]] = [
        test_revalidation_readers_do_not_hold_cache_lock_during_transport_wait,
        test_cache_write_failure_does_not_repeat_completed_get,
        test_cache_lock_wait_expires_without_remote_call,
        test_cache_storage_failure_falls_back_to_same_deadline_and_actor,
        test_poll_delay_respects_server_interval_and_adds_only_positive_jitter,
        test_cache_honors_poll_interval_across_readers_and_304,
        test_issue_reader_paginates_and_filters_pull_requests,
        test_present_terminal_link_header_does_not_fabricate_next_page,
        test_request_diagnostics_include_quota_and_request_id,
        test_graphql_json_uses_bounded_shared_graphql_transport,
        test_reader_diagnostics_marks_mixed_rest_and_graphql_buckets,
        test_conditional_cache_reuses_304_body_and_scopes_query_and_identity,
        test_matrix_approved_reader_retries_and_reports_attempts,
        test_reader_aggregates_retry_summary_across_requests,
        test_reader_cli_operations_are_matrix_approved,
        test_secret_scanning_status_public_repo_is_unavailable_without_alert_request,
        test_secret_scanning_status_counts_findings_without_secret_material,
        test_secret_scanning_status_preserves_hidden_values_on_followup_pages,
        test_secret_scanning_status_empty_alerts_is_clean,
        test_secret_scanning_status_permission_denied_is_unavailable_without_fallback,
        test_secret_scanning_status_404_is_ambiguous_and_never_clean,
        test_secret_scanning_status_repository_permission_failure_is_unavailable,
        test_secret_scanning_status_disabled_metadata_is_not_enabled_without_alert_request,
        test_secret_scanning_status_actor_mismatch_fails_closed_as_unavailable,
        test_secret_scanning_status_rejects_actor_change_after_preflight,
        test_secret_scanning_status_rejects_unbounded_limits_before_api_calls,
        test_secret_scanning_cli_reports_invalid_limit_as_input_validation,
        test_reader_failed_request_dominates_aggregate_certainty,
        test_request_diagnostic_redacts_failure_messages,
        test_explicit_active_auth_actor_is_visible_and_degraded,
        test_permission_failure_is_explicit_and_degraded,
        test_rate_limit_failure_preserves_reset_metadata,
        test_pull_request_normalization_preserves_final_merge_identity,
        test_pull_checks_share_paged_readers_and_ids,
        test_pull_checks_keep_only_latest_status_per_context,
        test_pull_checks_preserve_check_runs_when_status_permission_is_missing,
        test_shape_failure_marks_diagnostics_degraded,
        test_direct_checks_inventory_workflows_before_job_checks,
        test_workflow_metadata_jobs_and_text_log_normalize,
        test_text_logs_opt_in_and_remove_terminal_commands,
        test_text_logs_support_older_cli_and_preserve_failures,
        test_text_log_capability_probe_failure_is_visible,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            with tempfile.TemporaryDirectory() as state_dir, patch.dict(os.environ, {"GITHUB_RETRY_STATE_DIR": state_dir}):
                test()
            print(f"ok {test.__name__}")
        except Exception as exc:
            print(f"FAIL {test.__name__}: {exc}", file=sys.stderr)
            failed.append(test.__name__)
    print()
    if failed:
        print(f"{len(failed)}/{len(tests)} tests FAILED", file=sys.stderr)
        raise SystemExit(1)
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
