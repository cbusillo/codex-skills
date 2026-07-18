#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Deterministic tests for shared paged GitHub REST readers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))
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
        operation="github.read.secret_scanning_status",
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
                last_actor="shiny-code-bot",
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
                last_actor="shiny-code-bot",
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
        "github.read.secret_scanning_status",
        "github.read.repository",
    )
    for operation in operations:
        rule, error = github_read.github_api_core.operation_retry_rule(operation)
        assert error is None, (operation, error)
        assert rule is not None and rule.retry_eligibility == "safe", (operation, rule)


def test_secret_scanning_status_public_repo_is_unavailable_without_alert_request() -> None:
    responses = [
        process(include_output({"login": "shiny-code-bot"})),
        process(include_output({"private": False, "visibility": "public"})),
    ]
    with patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK": "1"}):
        reader = secret_scanning_reader()
    with patch("subprocess.run", side_effect=responses) as run:
        data = github_read.secret_scanning_status(reader, "o/r")
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
        process(include_output({"login": "shiny-code-bot"})),
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
        data = github_read.secret_scanning_status(reader, "o/r")
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
        process(include_output({"login": "shiny-code-bot"})),
        process(include_output({"private": True, "visibility": "private"})),
        process(include_output([{"secret": "first"}], headers={"link": f'<{next_page}>; rel="next"'})),
        process(include_output([{"secret": "second"}])),
    ]
    reader = secret_scanning_reader()
    with patch("subprocess.run", side_effect=responses) as run:
        data = github_read.secret_scanning_status(reader, "o/r", limit=3)
    assert data["status"] == "findings", data
    assert data["openAlertCount"] == 2, data
    followup_command = run.call_args_list[-1].args[0]
    assert any("hide_secret=true" in item for item in followup_command), followup_command
    assert any("state=open" in item for item in followup_command), followup_command


def test_secret_scanning_status_empty_alerts_is_clean() -> None:
    responses = [
        process(include_output({"login": "shiny-code-bot"})),
        process(include_output({"private": True, "visibility": "private"})),
        process(include_output([])),
    ]
    reader = secret_scanning_reader()
    with patch("subprocess.run", side_effect=responses):
        data = github_read.secret_scanning_status(reader, "o/r")
    assert data["status"] == "clean", data
    assert data["openAlertCount"] == 0, data
    assert data["openAlertCountIsLowerBound"] is False, data


def test_secret_scanning_status_permission_denied_is_unavailable_without_fallback() -> None:
    responses = [
        process(include_output({"login": "shiny-code-bot"})),
        process(include_output({"private": True, "visibility": "private"})),
        process(
            include_output({"message": "Resource not accessible by integration"}, status=403),
            returncode=1,
        ),
    ]
    with patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK": "1"}):
        reader = secret_scanning_reader()
    with patch("subprocess.run", side_effect=responses) as run:
        data = github_read.secret_scanning_status(reader, "o/r")
    assert data["status"] == "unavailable", data
    assert data["reason"] == "permission_limited", data
    assert reader.diagnostics()["degradedComponents"] == ["secret_scanning_alerts_page_1"]
    assert run.call_count == 3, run.call_args_list
    assert all(
        call.args[0][1:3] == ["--require-automation-auth", "api"]
        and "env" not in call.kwargs
        for call in run.call_args_list
    )


def test_secret_scanning_status_404_is_ambiguous_and_never_clean() -> None:
    responses = [
        process(include_output({"login": "shiny-code-bot"})),
        process(include_output({"private": True, "visibility": "private"})),
        process(include_output({"message": "Not Found"}, status=404), returncode=1),
    ]
    reader = secret_scanning_reader()
    with patch("subprocess.run", side_effect=responses):
        data = github_read.secret_scanning_status(reader, "o/r")
    assert data["status"] == "unavailable", data
    assert data["reason"] == "secret_scanning_404_ambiguous", data
    assert data["openAlertCount"] is None, data


def test_secret_scanning_status_repository_permission_failure_is_unavailable() -> None:
    responses = [
        process(include_output({"login": "shiny-code-bot"})),
        process(
            include_output({"message": "Resource not accessible by integration"}, status=403),
            returncode=1,
        ),
    ]
    reader = secret_scanning_reader()
    with patch("subprocess.run", side_effect=responses) as run:
        data = github_read.secret_scanning_status(reader, "o/r")
    assert data["status"] == "unavailable", data
    assert data["reason"] == "repository_permission_or_visibility_limited", data
    assert data["repository"]["visibility"] == "unknown", data
    assert run.call_count == 2, run.call_args_list


def test_secret_scanning_status_disabled_metadata_is_not_enabled_without_alert_request() -> None:
    responses = [
        process(include_output({"login": "shiny-code-bot"})),
        process(include_output({
            "private": True,
            "visibility": "private",
            "security_and_analysis": {"secret_scanning": {"status": "disabled"}},
        })),
    ]
    reader = secret_scanning_reader()
    with patch("subprocess.run", side_effect=responses) as run:
        data = github_read.secret_scanning_status(reader, "o/r")
    assert data["status"] == "not_enabled", data
    assert data["reason"] == "secret_scanning_disabled", data
    assert "scanningStatus" not in data["repository"], data
    assert run.call_count == 2, run.call_args_list


def test_secret_scanning_status_actor_mismatch_fails_closed_as_unavailable() -> None:
    reader = secret_scanning_reader()
    with patch("subprocess.run", return_value=process(include_output({"login": "octocat"}))) as run:
        data = github_read.secret_scanning_status(reader, "o/r")
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
        process(include_output({"login": "shiny-code-bot"})),
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
            github_read.secret_scanning_status(reader, "o/r")
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
                github_read.secret_scanning_status(reader, "o/r", limit=limit)
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
            last_actor="shiny-code-bot",
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
            last_actor="shiny-code-bot",
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
    assert diagnostics["expectedActor"] == "shiny-code-bot"
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
    ]
    reader = github_read.GitHubReader(gh_cmd="fake-gh", operation="test.checks")
    with patch("subprocess.run", side_effect=responses):
        payload = github_read.pull_request_checks(reader, "o/r", 7)
    check = payload["checkRuns"][0]
    assert check["runId"] == 22
    assert check["jobId"] == 33
    assert check["checkSuiteId"] == 44
    assert payload["summary"]["failingCount"] == 1
    assert reader.completed_steps == ["pull_request", "check_runs_page_1", "commit_statuses_page_1", "combined_status"]


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


def main() -> None:
    tests = [
        test_issue_reader_paginates_and_filters_pull_requests,
        test_present_terminal_link_header_does_not_fabricate_next_page,
        test_request_diagnostics_include_quota_and_request_id,
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
        test_explicit_active_auth_actor_is_visible_and_degraded,
        test_permission_failure_is_explicit_and_degraded,
        test_rate_limit_failure_preserves_reset_metadata,
        test_pull_request_normalization_preserves_final_merge_identity,
        test_pull_checks_share_paged_readers_and_ids,
        test_pull_checks_keep_only_latest_status_per_context,
        test_pull_checks_preserve_check_runs_when_status_permission_is_missing,
        test_shape_failure_marks_diagnostics_degraded,
        test_workflow_metadata_jobs_and_text_log_normalize,
    ]
    failed: list[str] = []
    for test in tests:
        try:
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
