#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Deterministic tests for shared paged GitHub REST readers."""

from __future__ import annotations

import json
import subprocess
import sys
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
