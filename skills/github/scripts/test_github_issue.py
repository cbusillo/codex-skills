#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import pathlib
import shlex
import sys
import tempfile
import threading
import time
import subprocess
import urllib.parse
from typing import Any
from unittest.mock import patch

os.environ["CODEX_SKILLS_ENV_FILE"] = "/definitely/missing/codex-skills-test.env"
os.environ["CODEX_AUTOMATION_LOGIN"] = "fixture-automation"
os.environ["CODEX_AUTOMATION_EMAIL"] = "fixture-automation@example.invalid"
os.environ["GH_WITH_ENV_TOKEN_EXPECTED_LOGIN"] = "fixture-automation"

import github_api
import github_issue
import github_plan_status
from github_fixture_support import api_failure, api_success, with_call_stub


def success(
    body: Any,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> github_api.ApiResult:
    return api_success(body, operation="github.issue.test", status=status, headers=headers)


def failure(status: int, body: Any, *, is_write: bool) -> github_api.ApiResult:
    return api_failure(status, body, operation="github.issue.test", is_write=is_write)


def issue_body(
    number: int = 42,
    *,
    actor: str = "fixture-automation",
    state: str = "open",
    state_reason: str | None = None,
    body: str = "body",
    created_at: str = "2026-07-16T22:00:00Z",
    updated_at: str = "2026-07-17T03:20:00Z",
) -> dict[str, Any]:
    return {
        "id": 9000 + number,
        "number": number,
        "title": "Issue title",
        "body": body,
        "created_at": created_at,
        "updated_at": updated_at,
        "state": state,
        "state_reason": state_reason,
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "user": {"login": actor},
        "labels": [{"name": "plan"}],
        "assignees": [{"login": actor}],
        "milestone": {"number": 7, "title": "Sprint 7"},
    }


def test_create_preserves_fields_and_emits_operation_marker() -> None:
    markdown = "## Result\n\n`literal` ${NOT_EXPANDED}\n"

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        if "/milestones?" in path:
            return success([{"number": 7, "title": "Sprint 7"}])
        if method == "GET":
            return success([])
        assert method == "POST"
        assert path == "/repos/owner/repo/issues"
        assert body["title"] == "Issue title"
        assert body["body"].startswith(markdown)
        assert github_api.OPERATION_MARKER_PREFIX in body["body"]
        assert body["labels"] == ["plan", "enhancement"]
        assert body["assignees"] == ["fixture-automation", "octocat", "copilot-swe-agent[bot]"]
        assert body["milestone"] == 7
        return success(issue_body(), status=201)

    def run(calls: list[dict[str, Any]]) -> None:
        payload = github_issue.create_issue(
            "Issue title",
            markdown,
            repo="owner/repo",
            labels=["plan, enhancement"],
            assignees=["@me", "octocat", "@copilot"],
            milestone="Sprint 7",
            gh_cmd="fake-gh",
        )
        marker = payload["operation_marker"]
        assert marker["kind"] == "request_fingerprint", marker
        assert len(marker["value"]) == 64, marker
        assert len(marker["operation_id"]) == 32, marker
        assert payload["actor"] == "fixture-automation", payload
        assert payload["updated_at"] == "2026-07-17T03:20:00Z", payload
        assert payload["completed_steps"] == ["resolve_actor", "resolve_milestone", "create_issue"], payload
        assert [call["method"] for call in calls] == ["GET", "GET", "GET", "POST"], calls

    with_call_stub(callback, run)


def test_own_user_create_reports_success_without_duplicate_retry() -> None:
    personal_login = "fixture-contributor"

    def callback(method: str, path: str, body: Any, **kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            assert "owner/repo" in kwargs["gh_prefix_args"], kwargs
            result = success({"login": personal_login})
        elif method == "GET":
            return success([])
        else:
            assert method == "POST", method
            assert body["assignees"] == [personal_login], body
            result = success(issue_body(actor=personal_login), status=201)
        # The wrapper has authorized the declared personal login for this repo.
        result.actor = personal_login
        result.expected_actor = None
        return result

    def run(calls: list[dict[str, Any]]) -> None:
        with (
            patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_OWN_USER": "1"}),
            patch.object(sys, "argv", ["gh-issue", "create", "Issue title", "--repo", "owner/repo", "--assignee", "@me"]),
            patch.object(sys, "stdin", io.StringIO("body")),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            exit_code = github_issue.main()
        payload = json.loads(stdout.getvalue())
        assert exit_code == 0 and payload["ok"] is True, payload
        assert payload["actor"] == payload["expected_actor"] == personal_login, payload
        assert payload["outcome_certainty"] == "confirmed", payload
        assert payload["recommended_next_action"] == "none", payload
        assert payload["failed_step"] is None, payload
        assert sum(call["method"] == "POST" for call in calls) == 1, calls

    with_call_stub(callback, run)


def test_own_user_create_rejects_wrong_response_author() -> None:
    personal_login = "fixture-contributor"

    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            result = success({"login": personal_login})
        elif method == "GET":
            return success([])
        else:
            result = success(issue_body(actor="unexpected-user"), status=201)
        result.actor = personal_login
        result.expected_actor = None
        return result

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            github_issue.create_issue("Issue title", "body", repo="owner/repo", gh_cmd="fake-gh")
        except github_issue.IssueError as exc:
            assert exc.failure.cause == "actor_mismatch", exc.failure
            assert exc.failure.retryable is False, exc.failure
        else:
            raise AssertionError("expected response actor mismatch")
        assert sum(call["method"] == "POST" for call in calls) == 1, calls

    with_call_stub(callback, run)


def test_own_user_sibling_writes_use_resolved_personal_actor() -> None:
    personal_login = "fixture-contributor"
    for command in ("edit", "close", "reopen"):
        def callback(method: str, path: str, body: Any, **kwargs: Any) -> github_api.ApiResult:
            if path == "/user":
                assert "owner/repo" in kwargs["gh_prefix_args"], kwargs
                result = success({"login": personal_login})
            else:
                assert kwargs["expected_actor"] == personal_login, kwargs
                result = success(issue_body(actor=personal_login, state="closed" if command == "close" else "open"))
            result.actor = personal_login
            result.expected_actor = None
            return result

        def run(calls: list[dict[str, Any]]) -> None:
            args = ["gh-issue", command, "42", "--repo", "owner/repo"]
            if command == "edit":
                args += ["--title", "New title"]
            with (
                patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_OWN_USER": "1"}),
                patch.object(sys, "argv", args),
                patch.object(sys, "stdin", io.StringIO("")),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                exit_code = github_issue.main()
            payload = json.loads(stdout.getvalue())
            assert (exit_code != 0) == (command == "close"), payload
            assert payload["ok"] == (command != "close"), payload
            if command == "close":
                assert payload["recovery"]["issue_closed"] is True, payload
            assert payload["actor"] == payload["expected_actor"] == personal_login, payload
            assert sum(call["method"] == "PATCH" for call in calls) == 1, calls

        with_call_stub(callback, run)


def test_create_unknown_outcome_requires_reconciliation_before_retry() -> None:
    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "POST":
            return failure(503, "Unicorn!", is_write=True)
        assert method == "GET"
        if "creator=" in path:
            assert "creator=fixture-automation" in path
        return success([])

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            github_issue.create_issue(
                "Issue title",
                "body",
                repo="owner/repo",
                gh_cmd="fake-gh",
            )
        except github_issue.IssueError as exc:
            assert exc.failure.cause == "network_provider_failure", exc.failure
            assert exc.failure.write_outcome == "unknown", exc.failure
            reconciliation = exc.payload["reconciliation"]
            assert reconciliation["required_before_retry"] is True, reconciliation
            assert len(reconciliation["request_fingerprint"]) == 64, reconciliation
            assert reconciliation["result"] == "no_match", reconciliation
            assert exc.payload["attempts"] == 4, exc.payload
            assert exc.payload["recommended_next_action"] == "reconcile_or_retry_manually", exc.payload
        else:
            raise AssertionError("expected unknown create outcome")
        assert [call["method"] for call in calls] == ["GET", "GET", "POST", "GET"], calls

    with_call_stub(callback, run)


def test_create_unknown_outcome_returns_unique_reconciled_issue() -> None:
    get_calls = 0
    submitted_body = ""

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        nonlocal get_calls, submitted_body
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "POST":
            submitted_body = str(body["body"])
            return failure(503, "Unicorn!", is_write=True)
        get_calls += 1
        if get_calls == 1:
            return success([])
        matched = issue_body(body=submitted_body, created_at="2026-07-16T21:59:58Z")
        matched["labels"] = []
        matched["assignees"] = []
        matched["milestone"] = None
        return success([matched])

    def run(calls: list[dict[str, Any]]) -> None:
        with patch.object(
            github_issue, "_utc_now",
            return_value=github_issue.dt.datetime(2026, 7, 16, 22, 0, tzinfo=github_issue.dt.timezone.utc),
        ):
            payload = github_issue.create_issue(
                "Issue title",
                "body",
                repo="owner/repo",
                gh_cmd="fake-gh",
            )
        assert payload["reconciled"] is True, payload
        assert payload["reconciliation"]["result"] == "matched", payload
        assert payload["completed_steps"] == ["resolve_actor", "reconcile_create"], payload
        assert payload["url"].endswith("/issues/42"), payload
        assert [call["method"] for call in calls] == ["GET", "GET", "POST", "GET"], calls

    with_call_stub(callback, run)


def test_create_reconciliation_survives_explicit_actor_fallback() -> None:
    get_calls = 0
    submitted_body = ""

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        nonlocal get_calls, submitted_body
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "POST":
            submitted_body = str(body["body"])
            result = failure(503, "Unicorn!", is_write=True)
            result.actor = "cbusillo"
            result.expected_actor = None
            return result
        get_calls += 1
        if get_calls == 1:
            assert "creator=" not in path, path
            return success([])
        assert "creator=cbusillo" in path, path
        matched = issue_body(
            actor="cbusillo",
            body=submitted_body,
            created_at="2026-07-16T22:00:01Z",
        )
        matched["labels"] = []
        matched["assignees"] = []
        matched["milestone"] = None
        return success([matched])

    def run(_calls: list[dict[str, Any]]) -> None:
        with (
            patch.object(
                github_issue, "_utc_now",
                return_value=github_issue.dt.datetime(2026, 7, 16, 22, 0, tzinfo=github_issue.dt.timezone.utc),
            ),
            patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK": "1"}),
        ):
            payload = github_issue.create_issue(
                "Issue title",
                "body",
                repo="owner/repo",
                gh_cmd="fake-gh",
            )
        assert payload["reconciled"] is True, payload
        assert payload["actor"] == "cbusillo", payload
        assert payload["expected_actor"] is None, payload

    with_call_stub(callback, run)


def test_create_reconciliation_rejects_concurrent_identical_issue() -> None:
    get_calls = 0

    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        nonlocal get_calls
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "POST":
            return failure(503, "Unicorn!", is_write=True)
        get_calls += 1
        if get_calls == 1:
            return success([])
        concurrent = issue_body(
            body=github_api.body_with_operation_marker("body", "b" * 32),
            created_at="2026-07-17T03:20:00Z",
        )
        concurrent["labels"] = []
        concurrent["assignees"] = []
        concurrent["milestone"] = None
        return success([concurrent])

    def run(_calls: list[dict[str, Any]]) -> None:
        with patch.object(
            github_issue, "_utc_now",
            return_value=github_issue.dt.datetime(2026, 7, 17, 3, 20, tzinfo=github_issue.dt.timezone.utc),
        ):
            try:
                github_issue.create_issue(
                    "Issue title",
                    "body",
                    repo="owner/repo",
                    gh_cmd="fake-gh",
                )
            except github_issue.IssueError as exc:
                assert exc.payload["reconciliation"]["result"] == "no_match", exc.payload
            else:
                raise AssertionError("a concurrent invocation's marker must not satisfy reconciliation")

    with_call_stub(callback, run, allow_retry=True)


def test_create_reconciliation_rejects_preexisting_identical_issue() -> None:
    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "POST":
            return failure(503, "Unicorn!", is_write=True)
        preexisting = issue_body(body="body", created_at="2026-07-16T21:59:59Z")
        preexisting["labels"] = []
        preexisting["assignees"] = []
        preexisting["milestone"] = None
        return success([preexisting])

    def run(_calls: list[dict[str, Any]]) -> None:
        with patch.object(
            github_issue, "_utc_now",
            return_value=github_issue.dt.datetime(2026, 7, 16, 22, 0, tzinfo=github_issue.dt.timezone.utc),
        ):
            try:
                github_issue.create_issue(
                    "Issue title",
                    "body",
                    repo="owner/repo",
                    gh_cmd="fake-gh",
                )
            except github_issue.IssueError as exc:
                assert exc.payload["reconciliation"]["result"] == "no_match", exc.payload
            else:
                raise AssertionError("pre-existing issue must not satisfy reconciliation")

    with_call_stub(callback, run)


def test_create_reconciliation_excludes_preexisting_same_second_issue() -> None:
    preexisting = issue_body(body="original body", created_at="2026-07-16T22:00:00Z")
    preexisting["labels"] = []
    preexisting["assignees"] = []
    preexisting["milestone"] = None
    post_calls = 0
    get_calls = 0

    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        nonlocal get_calls, post_calls
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "POST":
            post_calls += 1
            return failure(503, "Unicorn!", is_write=True)
        get_calls += 1
        if get_calls > 1:
            return success([{**preexisting, "body": "body"}])
        return success([preexisting])

    def run(_calls: list[dict[str, Any]]) -> None:
        with patch.object(
            github_issue, "_utc_now",
            return_value=github_issue.dt.datetime(2026, 7, 16, 22, 0, tzinfo=github_issue.dt.timezone.utc),
        ):
            try:
                github_issue.create_issue(
                    "Issue title",
                    "body",
                    repo="owner/repo",
                    gh_cmd="fake-gh",
                )
            except github_issue.IssueError as exc:
                reconciliation = exc.payload["reconciliation"]
                assert reconciliation["result"] == "no_match", reconciliation
                assert reconciliation["preexisting_issue_ids"] == [9042], reconciliation
            else:
                raise AssertionError("pre-existing same-second issue must not satisfy reconciliation")
        assert post_calls == 1, post_calls

    with_call_stub(callback, run, allow_retry=True)


def test_unknown_retry_enabled_issue_create_fails_closed_after_no_match() -> None:
    for operation in ("github.issue.create", "github.plan.create"):
        post_calls = 0

        def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
            nonlocal post_calls
            if path == "/user":
                return success({"login": "fixture-automation"})
            if method == "GET":
                if "creator=" in path:
                    assert "creator=fixture-automation" in path, path
                return success([])
            post_calls += 1
            return failure(503, "Unicorn!", is_write=True)

        def run(calls: list[dict[str, Any]]) -> None:
            try:
                github_issue.create_issue(
                    "Issue title",
                    "body",
                    repo="owner/repo",
                    gh_cmd="fake-gh",
                    operation=operation,
                )
            except github_issue.IssueError as exc:
                assert exc.failure.write_outcome == "unknown", exc.failure
                assert exc.payload["reconciliation"]["result"] == "no_match", exc.payload
                assert exc.payload["retry_eligible"] is False, exc.payload
            else:
                raise AssertionError(f"{operation} must fail closed after no-match reconciliation")
            assert post_calls == 1, post_calls
            assert [call["method"] for call in calls] == ["GET", "GET", "POST", "GET"], calls

        with_call_stub(callback, run, allow_retry=True)


def test_rejected_retry_enabled_issue_create_can_retry() -> None:
    for operation in ("github.issue.create", "github.plan.create"):
        post_calls = 0

        def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
            nonlocal post_calls
            if path == "/user":
                return success({"login": "fixture-automation"})
            if method == "GET":
                return success([])
            assert method == "POST", (method, path)
            post_calls += 1
            if post_calls == 1:
                return failure(429, "API rate limit exceeded", is_write=True)
            return success(issue_body(), status=201)

        def run(calls: list[dict[str, Any]]) -> None:
            payload = github_issue.create_issue(
                "Issue title",
                "body",
                repo="owner/repo",
                gh_cmd="fake-gh",
                operation=operation,
            )
            assert payload["attempts"] == 4, payload
            assert payload["reconciliation"] is None, payload
            assert payload["operation_marker"]["kind"] == "request_fingerprint", payload
            assert [call["method"] for call in calls] == ["GET", "GET", "POST", "POST"], calls

        with_call_stub(callback, run, allow_retry=True)


def test_edit_uses_rest_membership_endpoints_and_reads_after_write() -> None:
    issue_reads = 0

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        nonlocal issue_reads
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "PATCH":
            assert body == {"body": "replacement", "title": "New title", "milestone": None}
            return success(issue_body())
        if path.endswith("/labels") and method == "POST":
            assert body == {"labels": ["enhancement"]}
            return success([{"name": "enhancement"}])
        if "/labels/plan" in path:
            assert method == "DELETE"
            return success([])
        if path.endswith("/assignees") and method == "POST":
            assert body == {"assignees": ["octocat"]}
            return success(issue_body())
        if path.endswith("/assignees") and method == "DELETE":
            assert body == {"assignees": ["fixture-automation"]}
            return success(issue_body())
        assert method == "GET"
        assert path == "/repos/owner/repo/issues/42"
        issue_reads += 1
        return success(issue_body())

    def run(calls: list[dict[str, Any]]) -> None:
        payload = github_issue.edit_issue(
            42,
            body="replacement",
            title="New title",
            repo="owner/repo",
            add_labels=["enhancement"],
            remove_labels=["plan"],
            add_assignees=["octocat"],
            remove_assignees=["@me"],
            remove_milestone=True,
            gh_cmd="fake-gh",
        )
        assert payload["completed_steps"] == [
            "resolve_actor",
            "read_issue_ownership",
            "edit_issue_fields",
            "add_labels",
            "remove_label",
            "add_assignees",
            "remove_assignees",
            "read_after_write",
        ], payload
        assert payload["attempts"] == 8, payload
        assert payload["outcome_certainty"] == "confirmed", payload
        assert [call["method"] for call in calls] == [
            "GET",
            "GET",
            "PATCH",
            "POST",
            "DELETE",
            "POST",
            "DELETE",
            "GET",
        ], calls
        assert issue_reads == 2, issue_reads

    with_call_stub(callback, run)


def test_edit_partial_failure_preserves_completed_steps_and_guidance() -> None:
    issue_read = False

    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        nonlocal issue_read
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "GET":
            issue_read = True
            return success(issue_body())
        if method == "PATCH":
            return success(issue_body())
        return failure(503, "Unicorn!", is_write=True)

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            github_issue.edit_issue(
                42,
                body="replacement",
                repo="owner/repo",
                add_labels=["enhancement"],
                gh_cmd="fake-gh",
            )
        except github_issue.IssueError as exc:
            assert exc.failure.failed_step == "add_labels", exc.failure
            assert exc.failure.completed_steps == [
                "resolve_actor",
                "read_issue_ownership",
                "edit_issue_fields",
            ], exc.failure
            assert exc.payload["reconciliation"]["strategy"] == "read_issue_and_compare_requested_fields"
            assert exc.payload["attempts"] == 4, exc.payload
            assert exc.payload["outcome_certainty"] == "unknown", exc.payload
            envelope = github_issue._terminal_failure(
                exc, "github.issue.edit", expected_actor="fixture-automation"
            )
            assert envelope["write_outcome"] == "partially_applied", envelope
            assert envelope["outcome_certainty"] == "unknown", envelope
            assert envelope["failed_request"]["write_outcome"] == "unknown", envelope
            assert envelope["failed_request"]["outcome_certainty"] == "unknown", envelope
            classified = github_api.classify_error(503, {}, "Unicorn!", is_write=True)
            assert envelope["retryable"] == classified.retryable, envelope
            assert envelope["fallback_eligible"] == classified.fallback_eligible, envelope
            recovery = envelope["reconciliation"]
            assert recovery["completed"]["field_values"] == {"body": "replacement"}, recovery
            assert recovery["remaining"]["field_values"] == {}, recovery
            assert recovery["remaining"]["add_labels"] == ["enhancement"], recovery
        else:
            raise AssertionError("expected partial edit failure")
        assert [call["method"] for call in calls] == ["GET", "GET", "PATCH", "POST"], calls
        assert issue_read

    with_call_stub(callback, run)


def test_edit_absent_label_removal_completes_requested_edit() -> None:
    labels = {"plan", "plan:blocked"}

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "GET":
            issue = issue_body()
            issue["labels"] = [{"name": label} for label in sorted(labels)]
            return success(issue)
        if method == "POST":
            labels.update(body["labels"])
            return success([{"name": label} for label in sorted(labels)])
        assert method == "DELETE"
        label = github_issue.urllib.parse.unquote(path.rsplit("/", 1)[1])
        if label not in labels:
            return failure(404, {"message": "Label does not exist"}, is_write=True)
        labels.remove(label)
        return success([])

    def run(calls: list[dict[str, Any]]) -> None:
        payload = github_issue.edit_issue(
            42,
            repo="owner/repo",
            add_labels=["plan:waiting"],
            remove_labels=["plan:blocked", "plan:active", "later"],
            gh_cmd="fake-gh",
        )
        assert labels == {"plan", "plan:waiting"}, labels
        assert payload["outcome_certainty"] == "confirmed", payload
        assert payload["completed_steps"].count("remove_label") == 3, payload
        assert [call["method"] for call in calls] == [
            "GET", "POST", "DELETE", "DELETE", "GET", "DELETE", "GET", "GET",
        ], calls

    with_call_stub(callback, run, allow_retry=True)


def test_edit_retried_label_removal_confirms_unseen_success() -> None:
    deletes = 0

    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        nonlocal deletes
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "DELETE":
            assert path.endswith("/labels/plan%3Awaiting%2Ftest"), path
            deletes += 1
            if deletes == 1:
                # GitHub applied the deletion, but its response was lost.
                return failure(503, "Lost response", is_write=True)
            return failure(404, {"message": "Label does not exist"}, is_write=True)
        assert method == "GET" and path == "/repos/owner/repo/issues/42", path
        return success(issue_body())

    def run(calls: list[dict[str, Any]]) -> None:
        payload = github_issue.edit_issue(
            42, repo="owner/repo", remove_labels=["plan:waiting/test"], gh_cmd="fake-gh",
        )
        assert payload["outcome_certainty"] == "confirmed", payload
        assert payload["recommended_next_action"] == "none", payload
        assert payload["retry_exhausted_reason"] is None, payload
        assert payload["completed_steps"] == [
            "resolve_actor", "reconcile_remove_label", "remove_label", "read_after_write",
        ], payload
        assert payload["attempts"] == len(calls) == 5, (payload, calls)
        assert [call["method"] for call in calls] == ["GET", "DELETE", "DELETE", "GET", "GET"], calls

    with_call_stub(callback, run, allow_retry=True)


def test_edit_label_removal_requires_readable_absence() -> None:
    for readback in (
        failure(403, {"message": "Resource not accessible by integration"}, is_write=False),
        failure(404, {"message": "Not Found"}, is_write=False),
        success({"number": 42}),
        success({"number": 42, "labels": [None]}),
        success({"number": 42, "labels": [{"name": ""}]}),
        success({"number": 99, "labels": []}),
        success({"number": 42, "labels": [{"name": "PLAN:WAITING"}]}),
    ):
        def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
            if path == "/user":
                return success({"login": "fixture-automation"})
            if method == "DELETE":
                return failure(404, {"message": "Label does not exist"}, is_write=True)
            assert method == "GET" and path == "/repos/owner/repo/issues/42", path
            return readback

        def run(calls: list[dict[str, Any]]) -> None:
            try:
                github_issue.edit_issue(
                    42, repo="owner/repo", remove_labels=["plan:waiting"], gh_cmd="fake-gh",
                )
            except github_issue.IssueError as exc:
                assert "remove_label" not in exc.failure.completed_steps, exc.failure
                assert exc.failure.failed_step in {"remove_label", "reconcile_remove_label"}, exc.failure
                envelope = github_issue._terminal_failure(
                    exc, "github.issue.edit", expected_actor="fixture-automation",
                )
                assert envelope["attempts"] == len(calls), (envelope, calls)
                assert exc.api_result["attempts"] == len(calls), (exc.api_result, calls)
            else:
                raise AssertionError(f"unconfirmed absence reported as success: {readback}")
            assert [call["method"] for call in calls] == ["GET", "DELETE", "GET"], calls

        with_call_stub(callback, run)


def test_edit_label_removal_does_not_reconcile_denials_or_unrelated_404() -> None:
    for status, message in ((401, "Bad credentials"), (403, "Resource not accessible by integration"), (404, "Not Found")):
        def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
            if path == "/user":
                return success({"login": "fixture-automation"})
            assert method == "DELETE", (method, path)
            return failure(status, {"message": message}, is_write=True)

        def run(calls: list[dict[str, Any]]) -> None:
            try:
                github_issue.edit_issue(42, repo="owner/repo", remove_labels=["absent"], gh_cmd="fake-gh")
            except github_issue.IssueError as exc:
                assert exc.api_result["status"] == status, exc.api_result
            else:
                raise AssertionError(f"unexpected success after {status}")
            assert [call["method"] for call in calls] == ["GET", "DELETE"], calls

        with_call_stub(callback, run)


def test_edit_failure_after_reconciled_removal_preserves_remaining_intent() -> None:
    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "GET":
            return success(issue_body())
        if method == "DELETE":
            return failure(404, {"message": "Label does not exist"}, is_write=True)
        assert method == "POST" and path.endswith("/assignees"), (method, path)
        return failure(403, {"message": "Resource not accessible by integration"}, is_write=True)

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            github_issue.edit_issue(
                42, repo="owner/repo", remove_labels=["absent"],
                add_assignees=["octocat"], gh_cmd="fake-gh",
            )
        except github_issue.IssueError as exc:
            payload = github_issue._terminal_failure(
                exc, "github.issue.edit", expected_actor="fixture-automation",
            )
        else:
            raise AssertionError("assignee denial should stop the edit")
        assert payload["failed_step"] == "add_assignees", payload
        assert payload["write_outcome"] == "partially_applied", payload
        assert payload["outcome_certainty"] == "confirmed_partially_applied", payload
        assert payload["reconciliation"]["completed"]["remove_labels"] == ["absent"], payload
        assert payload["reconciliation"]["remaining"]["add_assignees"] == ["octocat"], payload
        assert [call["method"] for call in calls] == ["GET", "DELETE", "GET", "POST"], calls

    with_call_stub(callback, run)


def test_edit_first_mutation_failure_is_not_partial() -> None:
    def callback(_method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        return failure(404, {"message": "Not Found"}, is_write=True)

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            github_issue.edit_issue(42, repo="owner/repo", remove_labels=["absent"], gh_cmd="fake-gh")
        except github_issue.IssueError as exc:
            envelope = github_issue._terminal_failure(
                exc, "github.issue.edit", expected_actor="fixture-automation"
            )
            assert envelope["write_outcome"] == "not_started", envelope
            assert envelope["outcome_certainty"] == "confirmed_not_applied", envelope
        else:
            raise AssertionError("expected first-mutation failure")
        assert [call["method"] for call in calls] == ["GET", "DELETE"], calls

    with_call_stub(callback, run)


def test_edit_failed_readback_preserves_confirmed_writes() -> None:
    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "POST":
            return success(issue_body())
        return failure(404, {"message": "Not Found"}, is_write=False)

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            github_issue.edit_issue(
                42, repo="owner/repo", add_assignees=["octocat"], gh_cmd="fake-gh"
            )
        except github_issue.IssueError as exc:
            envelope = github_issue._terminal_failure(
                exc, "github.issue.edit", expected_actor="fixture-automation"
            )
            assert envelope["write_outcome"] == "applied", envelope
            assert envelope["outcome_certainty"] == "confirmed", envelope
            assert envelope["failed_step"] == "read_after_write", envelope
            assert envelope["failed_request"]["write_outcome"] is None, envelope
            assert envelope["failed_request"]["outcome_certainty"] == "not_applicable", envelope
            assert envelope["reconciliation"]["completed"]["add_assignees"] == ["octocat"], envelope
            assert not any(envelope["reconciliation"]["remaining"].values()), envelope
        else:
            raise AssertionError("expected readback failure")
        assert [call["method"] for call in calls] == ["GET", "POST", "GET"], calls

    with_call_stub(callback, run)


def test_edit_malformed_readback_preserves_confirmed_writes() -> None:
    for readback in ({}, "<html>proxy response</html>"):
        def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
            if path == "/user":
                return success({"login": "fixture-automation"})
            if method == "POST":
                return success([{"name": "enhancement"}])
            return success(readback)

        def run(calls: list[dict[str, Any]]) -> None:
            try:
                github_issue.edit_issue(
                    42, repo="owner/repo", add_labels=["enhancement"], gh_cmd="fake-gh"
                )
            except github_issue.IssueError as exc:
                envelope = github_issue._terminal_failure(
                    exc, "github.issue.edit", expected_actor="fixture-automation"
                )
                assert envelope["write_outcome"] == "applied", envelope
                assert envelope["outcome_certainty"] == "confirmed", envelope
                assert envelope["failed_step"] == "parse_issue_response", envelope
                assert envelope["failed_request"]["outcome_certainty"] == "not_applicable", envelope
                recovery = envelope["reconciliation"]
                assert recovery["endpoint"] == "/repos/owner/repo/issues/42", recovery
                assert recovery["completed"]["add_labels"] == ["enhancement"], recovery
                assert not any(recovery["remaining"].values()), recovery
            else:
                raise AssertionError("expected malformed readback failure")
            assert [call["method"] for call in calls] == ["GET", "POST", "GET"], calls

        with_call_stub(callback, run)


def test_edit_partial_failure_preserves_actor_recovery_guidance() -> None:
    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method in {"GET", "PATCH"}:
            return success(issue_body())
        result = failure(503, "Unicorn!", is_write=True)
        result.actor = "other-fixture"
        result.expected_actor = "fixture-automation"
        return result

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            github_issue.edit_issue(
                42, repo="owner/repo", title="Updated", add_labels=["plan:waiting"], gh_cmd="fake-gh"
            )
        except github_issue.IssueError as exc:
            envelope = github_issue._terminal_failure(
                exc, "github.issue.edit", expected_actor="fixture-automation"
            )
            assert envelope["error_code"] == "actor_mismatch", envelope
            assert envelope["write_outcome"] == "partially_applied", envelope
            assert envelope["recommended_next_action"] == "start_new_authorized_actor_context", envelope
            assert envelope["failed_request"]["recommended_next_action"] == "start_new_authorized_actor_context", envelope
            assert envelope["retryable"] is False and envelope["fallback_eligible"] is False, envelope
            assert envelope["disposition"] == "stop", envelope
            assert envelope["reconciliation"]["completed"]["field_values"] == {"title": "Updated"}, envelope
        else:
            raise AssertionError("expected actor-context refusal")
        assert [call["method"] for call in calls] == ["GET", "GET", "PATCH", "POST"], calls

    with_call_stub(callback, run, allow_retry=True)


def test_edit_rejects_cross_author_source_content_without_override() -> None:
    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        assert method == "GET"
        return success(issue_body(actor="human-owner"))

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            github_issue.edit_issue(
                42,
                body="replacement",
                title="New title",
                repo="owner/repo",
                gh_cmd="fake-gh",
            )
        except github_issue.IssueError as exc:
            assert exc.failure.cause == "authorization_denied", exc.failure
            assert exc.failure.failed_step == "authorize_source_content_edit", exc.failure
            assert exc.payload["issue_author"] == "human-owner", exc.payload
        else:
            raise AssertionError("cross-author source-content edit should fail closed")
        assert [call["method"] for call in calls] == ["GET", "GET"], calls

    with_call_stub(callback, run)


def test_edit_allows_explicit_cross_author_source_content_override() -> None:
    issue_reads = 0

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        nonlocal issue_reads
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "GET":
            issue_reads += 1
            return success(issue_body(actor="human-owner"))
        assert method == "PATCH"
        assert body == {"body": "replacement"}
        return success(issue_body(actor="human-owner", body="replacement"))

    def run(calls: list[dict[str, Any]]) -> None:
        payload = github_issue.edit_issue(
            42,
            body="replacement",
            repo="owner/repo",
            cross_author_source_edit_reason="User explicitly requested restoration from issue history.",
            gh_cmd="fake-gh",
        )

        assert "authorize_cross_author_source_edit" in payload["completed_steps"], payload
        assert [call["method"] for call in calls] == ["GET", "GET", "PATCH", "GET"], calls
        assert issue_reads == 2, issue_reads

    with_call_stub(callback, run)


def test_metadata_only_edit_does_not_require_source_content_ownership() -> None:
    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        if path.endswith("/labels") and method == "POST":
            assert body == {"labels": ["plan"]}
            return success([{"name": "plan"}])
        assert method == "GET"
        return success(issue_body(actor="human-owner"))

    def run(calls: list[dict[str, Any]]) -> None:
        payload = github_issue.edit_issue(
            42,
            repo="owner/repo",
            add_labels=["plan"],
            gh_cmd="fake-gh",
        )

        assert "read_issue_ownership" not in payload["completed_steps"], payload
        assert [call["method"] for call in calls] == ["GET", "POST", "GET"], calls

    with_call_stub(callback, run)


def test_edit_local_validation_preserves_prior_retry_summary() -> None:
    def callback(_method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        assert path == "/user"
        return success({"login": "fixture-automation"})

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            github_issue.edit_issue(
                42,
                repo="owner/repo",
                add_labels=["plan"],
                remove_labels=["plan"],
                gh_cmd="fake-gh",
            )
        except github_issue.IssueError as exc:
            assert exc.failure.cause == "validation_error", exc.failure
            assert exc.payload["attempts"] == 1, exc.payload
            assert exc.api_result is not None
            assert exc.api_result["attempts"] == 1, exc.api_result
        else:
            raise AssertionError("expected local edit validation failure")
        assert len(calls) == 1, calls

    with_call_stub(callback, run)


def test_close_partial_failure_preserves_comment_step() -> None:
    original_comment = github_issue.github_comment.comment

    def fake_comment(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"actor": "fixture-automation"}

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        assert method == "PATCH"
        assert path == "/repos/owner/repo/issues/42"
        assert body == {"state": "closed", "state_reason": "completed"}
        return failure(503, "Unicorn!", is_write=True)

    def run(calls: list[dict[str, Any]]) -> None:
        github_issue.github_comment.comment = fake_comment
        try:
            try:
                github_issue.set_issue_state(
                    42,
                    state="closed",
                    state_reason="completed",
                    repo="owner/repo",
                    comment_body="closing",
                    gh_cmd="fake-gh",
                )
            except github_issue.IssueError as exc:
                assert exc.failure.failed_step == "close_issue", exc.failure
                assert exc.failure.completed_steps == ["post_close_comment"], exc.failure
                assert exc.payload["reconciliation"]["expected_state"] == "closed"
                assert exc.payload["attempts"] == 2, exc.payload
            else:
                raise AssertionError("expected close failure")
        finally:
            github_issue.github_comment.comment = original_comment
        assert [call["method"] for call in calls] == ["GET", "PATCH"], calls

    with_call_stub(callback, run)


def test_reopen_comment_uses_non_idempotent_comment_retry_policy() -> None:
    post_calls = 0

    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        nonlocal post_calls
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "GET" and "/comments?" in path:
            return success([])
        if method == "POST" and path.endswith("/comments"):
            post_calls += 1
            return failure(503, "Unicorn!", is_write=True)
        raise AssertionError(f"unexpected call: {method} {path}")

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            github_issue.set_issue_state(
                42,
                state="open",
                state_reason="reopened",
                repo="owner/repo",
                comment_body="reopening",
                gh_cmd="fake-gh",
            )
        except github_issue.IssueError as exc:
            assert exc.failure.write_outcome == "unknown", exc.failure
            assert exc.payload["reconciliation"]["result"] == "no_match", exc.payload
            assert exc.payload["attempts"] == 5, exc.payload
        else:
            raise AssertionError("unknown reopen comment must fail closed")
        comment_calls = [call for call in calls if "/comments" in call["path"]]
        assert all(
            call["kwargs"]["operation"] == "github.comment.issue"
            for call in comment_calls
        ), comment_calls
        assert post_calls == 1, post_calls
        assert not any(call["method"] == "PATCH" for call in calls), calls

    with_call_stub(callback, run, allow_retry=True)


def test_close_and_reopen_use_explicit_state_reasons() -> None:
    expected = [
        ("closed", "not_planned"),
        ("open", "reopened"),
    ]

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        assert method == "PATCH"
        state, reason = expected.pop(0)
        assert body == {"state": state, "state_reason": reason}
        return success(issue_body(state=state, state_reason=reason))

    def run(calls: list[dict[str, Any]]) -> None:
        closed = github_issue.set_issue_state(
            42,
            state="closed",
            state_reason="not_planned",
            repo="owner/repo",
            gh_cmd="fake-gh",
        )
        reopened = github_issue.set_issue_state(
            42,
            state="open",
            state_reason="reopened",
            repo="owner/repo",
            gh_cmd="fake-gh",
        )
        assert closed["state_reason"] == "not_planned", closed
        assert reopened["state_reason"] == "reopened", reopened
        assert closed["completed_steps"] == ["close_issue"], closed
        assert reopened["completed_steps"] == ["reopen_issue"], reopened
        assert closed["attempts"] == 2, closed
        assert reopened["attempts"] == 2, reopened
        assert closed["outcome_certainty"] == "confirmed", closed
        assert reopened["outcome_certainty"] == "confirmed", reopened
        assert [call["method"] for call in calls] == ["GET", "PATCH", "GET", "PATCH"], calls

    with_call_stub(callback, run)


def test_duplicate_close_resolves_database_id() -> None:
    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "GET":
            assert path == "/repos/owner/repo/issues/41"
            return success({"id": 9041, "number": 41})
        assert body == {
            "state": "closed",
            "state_reason": "duplicate",
            "duplicate_issue_id": 9041,
        }
        return success(issue_body(state="closed", state_reason="duplicate"))

    def run(calls: list[dict[str, Any]]) -> None:
        payload = github_issue.set_issue_state(
            42,
            state="closed",
            state_reason="duplicate",
            repo="owner/repo",
            duplicate_of="41",
            gh_cmd="fake-gh",
        )
        assert payload["completed_steps"] == ["resolve_duplicate_issue", "close_issue"], payload
        assert [call["method"] for call in calls] == ["GET", "GET", "PATCH"], calls

    with_call_stub(callback, run)


def test_mutation_parsers_accept_self_contained_targets_without_repo_resolution() -> None:
    parser = github_issue.build_parser()
    targets = (
        "https://github.example.test/owner/repo/issues/42",
        "owner/repo#42",
    )
    original_resolve_repo = github_issue._resolve_repo

    def fail_repo_resolution(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("self-contained targets must not resolve an ambient repository")

    github_issue._resolve_repo = fail_repo_resolution
    try:
        for command in ("edit", "close", "reopen"):
            for target in targets:
                args = parser.parse_args([command, target])
                assert args.number == target, args
                assert github_issue._resolve_cli_target(
                    args.number,
                    args.repo,
                    operation=f"github.issue.{command}",
                ) == ("owner/repo", 42)
    finally:
        github_issue._resolve_repo = original_resolve_repo


def test_close_reason_and_duplicate_target_are_mutually_exclusive() -> None:
    parser = github_issue.build_parser()
    try:
        parser.parse_args(["close", "42", "--reason", "not_planned", "--duplicate-of", "41"])
    except github_api.ArgumentParsingError:
        return
    raise AssertionError("close must reject --reason with --duplicate-of")


def test_invalid_duplicate_target_does_not_post_comment() -> None:
    original_comment = github_issue.github_comment.comment
    comment_called = False

    def fake_comment(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal comment_called
        comment_called = True
        return {"actor": "fixture-automation"}

    def callback(_method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        assert path == "/user"
        return success({"login": "fixture-automation"})

    def run(calls: list[dict[str, Any]]) -> None:
        github_issue.github_comment.comment = fake_comment
        try:
            try:
                github_issue.set_issue_state(
                    42,
                    state="closed",
                    state_reason="duplicate",
                    repo="owner/repo",
                    comment_body="duplicate close",
                    duplicate_of="not-an-issue",
                    gh_cmd="fake-gh",
                )
            except github_issue.IssueError as exc:
                assert exc.failure.failed_step == "resolve_duplicate_issue", exc.failure
                assert exc.failure.write_outcome == "not_started", exc.failure
                assert exc.payload["attempts"] == 1, exc.payload
            else:
                raise AssertionError("expected invalid duplicate target")
        finally:
            github_issue.github_comment.comment = original_comment
        assert comment_called is False
        assert len(calls) == 1, calls

    with_call_stub(callback, run)


def _state_and_label_callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
    if path == "/user":
        return success({"login": "fixture-automation"})
    if "/comments" in path and method == "GET":
        return success([])
    if path.endswith("/comments"):
        assert method == "POST", (method, path)
        return success({"id": 1, "html_url": "https://github.com/owner/repo/issues/42#issuecomment-1", "body": body["body"]}, status=201)
    if path.endswith("/labels") and method == "POST":
        return success([{"name": label} for label in body["labels"]])
    if "/labels/" in path:
        assert method == "DELETE", (method, path)
        return success([])
    if method == "PATCH" and "state" in body:
        return success(issue_body(state=body["state"], state_reason=body["state_reason"]))
    if method == "PATCH":
        return success(issue_body(body=body.get("body", "body")))
    assert method == "GET", (method, path)
    return success(issue_body())


def _run_cli(argv: list[str], stdin: Any, *, timeout: float, planning: bool = False) -> tuple[int, dict[str, Any]]:
    result: dict[str, Any] = {}
    original_argv, original_stdin = sys.argv, sys.stdin

    def target() -> None:
        output = io.StringIO()
        # Existing stdin tests isolate input selection; stateful projection tests
        # opt into the real CLI adapter and original API transport below.
        projection = contextlib.nullcontext() if planning else patch.object(github_issue, "refresh_after_close",
            return_value={"ok": True, "coverage": "complete", "issues": []})
        with contextlib.redirect_stdout(output), projection:
            result["exit_code"] = github_issue.main()
        result["payload"] = json.loads(output.getvalue())

    sys.argv, sys.stdin = ["github_issue.py", *argv], stdin
    try:
        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(timeout)
    finally:
        sys.argv, sys.stdin = original_argv, original_stdin
    assert not worker.is_alive(), f"{argv[0]} did not return within {timeout}s"
    return result["exit_code"], result["payload"]


def test_state_and_edit_commands_return_with_open_idle_stdin() -> None:
    commands = (
        ["edit", "owner/repo#42", "--add-label", "plan:waiting", "--remove-label", "plan:active"],
        ["close", "owner/repo#42", "--reason", "completed"],
        ["reopen", "owner/repo#42"],
    )

    def run(calls: list[dict[str, Any]]) -> None:
        for argv in commands:
            read_fd, write_fd = os.pipe()
            idle_stdin = os.fdopen(read_fd, encoding="utf-8")
            try:
                exit_code, payload = _run_cli(
                    argv,
                    idle_stdin,
                    timeout=github_issue.IMPLICIT_STDIN_WAIT_SECONDS + 5,
                )
            finally:
                # Close the writer first so a helper stuck reading sees EOF
                # and the test fails instead of deadlocking on close.
                os.close(write_fd)
                idle_stdin.close()
            assert exit_code == 0, payload
            assert payload["ok"] is True, payload
        assert not [call for call in calls if "/comments" in call["path"]], calls
        assert not [call for call in calls if call["method"] == "PATCH" and "body" in (call["body"] or {})], calls

    with_call_stub(_state_and_label_callback, run)


def test_explicit_comment_file_waits_for_slow_stdin_and_reads_paths() -> None:
    def run(calls: list[dict[str, Any]]) -> None:
        read_fd, write_fd = os.pipe()

        def slow_writer() -> None:
            time.sleep(github_issue.IMPLICIT_STDIN_WAIT_SECONDS + 0.5)
            os.write(write_fd, b"Closing from a slow producer.")
            os.close(write_fd)

        writer = threading.Thread(target=slow_writer, daemon=True)
        writer.start()
        with os.fdopen(read_fd, encoding="utf-8") as slow_stdin:
            exit_code, payload = _run_cli(
                ["close", "owner/repo#42", "--comment-file", "-"],
                slow_stdin,
                timeout=github_issue.IMPLICIT_STDIN_WAIT_SECONDS + 10,
            )
        writer.join()
        assert exit_code == 0, payload

        with tempfile.TemporaryDirectory() as temp_dir:
            comment_path = pathlib.Path(temp_dir) / "comment.md"
            comment_path.write_text("Reopening from a file.", encoding="utf-8")
            exit_code, payload = _run_cli(
                ["reopen", "owner/repo#42", "--comment-file", str(comment_path)],
                io.StringIO(""),
                timeout=10,
            )
        assert exit_code == 0, payload

        comments = [call["body"]["body"] for call in calls if call["method"] == "POST" and call["path"].endswith("/comments")]
        assert len(comments) == 2, calls
        assert comments[0].startswith("Closing from a slow producer."), comments
        assert comments[1].startswith("Reopening from a file."), comments

    with_call_stub(_state_and_label_callback, run)


def test_edit_body_file_can_clear_the_body() -> None:
    def run(calls: list[dict[str, Any]]) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            empty_body = pathlib.Path(temp_dir) / "empty.md"
            empty_body.write_text("", encoding="utf-8")
            exit_code, payload = _run_cli(
                ["edit", "owner/repo#42", "--body-file", str(empty_body)],
                io.StringIO(""),
                timeout=10,
            )
        assert exit_code == 0, payload
        patches = [call["body"] for call in calls if call["method"] == "PATCH"]
        assert patches == [{"body": ""}], calls

    with_call_stub(_state_and_label_callback, run)


def test_close_rejects_comment_with_comment_file() -> None:
    parser = github_issue.build_parser()
    for command in ("close", "reopen"):
        try:
            parser.parse_args([command, "42", "--comment", "text", "--comment-file", "-"])
        except github_api.ArgumentParsingError:
            continue
        raise AssertionError(f"{command} accepted both --comment and --comment-file")


@contextlib.contextmanager
def generic_close_fixture(*, reject_labels: bool | str = False, verified_checkout: bool = True):
    """Use the original API mock across the CLI and fresh planning adapter."""
    names = github_plan_status.DEFAULT_STATUS_LABELS
    clear = github_plan_status.wait_records.reviewed(evidence="Dependent discussion fully reviewed")
    records = {number: issue_body(number, body="Original request.") for number in (42, 43)}
    records[42]["labels"] = [{"name": "bug"}]
    records[43].update(body="Dependent request.\n" + github_plan_status.wait_records.marker(clear),
                       user={"login": "owner"}, labels=[{"name": names[key]} for key in ("plan", "blocked")])
    comments = {42: [], 43: []}
    state_writes, comment_writes = [], []
    def callback(method, path, body, **kwargs):
        if path == "/user":
            return success({"login": "fixture-automation"})
        route = urllib.parse.urlsplit(path).path
        prefix, separator, suffix = route.partition("/issues/")
        assert separator and prefix == "/repos/owner/repo", (method, path)
        parts = suffix.split("/")
        number = int(parts[0])
        issue = records[number]
        if method == "GET":
            if len(parts) == 1:
                return success(copy.deepcopy(issue))
            if parts[1] == "comments":
                return success(copy.deepcopy(comments[number]))
            if parts[1] == "parent":
                return failure(404, {"message": "Not Found"}, is_write=False)
            if parts[1] == "sub_issues":
                return success([])
            if parts[1:] == ["dependencies", "blocking"]:
                return success([copy.deepcopy(records[43])] if number == 42 else [])
            if parts[1:] == ["dependencies", "blocked_by"]:
                return success([copy.deepcopy(records[42])] if number == 43 else [])
        if method == "PATCH" and len(parts) == 1:
            assert number == 42 and set(body) == {"state", "state_reason"}, (number, body)
            issue.update(body)
            state_writes.append((number, body["state"]))
            return success(copy.deepcopy(issue))
        if method == "POST" and parts[1:] == ["comments"]:
            item = {"id": 1, "html_url": f"https://github.com/owner/repo/issues/{number}#issuecomment-1",
                    "body": body["body"], "user": {"login": "fixture-automation"}}
            comments[number].append(item)
            comment_writes.append(number)
            return success(copy.deepcopy(item), status=201)
        if number in (42, 43) and parts[1] == "labels":
            if callback.reject_labels == "source" and number == 42:
                return failure(403, {"message": "Explicit source labels denied"}, is_write=True)
            if callback.reject_labels is True and number == 43:
                return failure(422, {"message": "Fixture label operation rejected"}, is_write=True)
            labels = {item["name"] for item in issue["labels"]}
            if method == "DELETE":
                labels.remove(urllib.parse.unquote(parts[2]))
            else:
                assert method == "POST", (method, path)
                labels.update(body["labels"])
            issue["labels"] = [{"name": value} for value in sorted(labels)]
            return success(copy.deepcopy(issue["labels"]))
        raise AssertionError((method, path, body))
    callback.reject_labels = reject_labels
    with tempfile.TemporaryDirectory() as directory:
        checkout = pathlib.Path(directory)
        metadata = checkout / ".github/github.json"
        metadata.parent.mkdir()
        metadata.write_text(json.dumps({"planning": {"labels": dict(names), "projects": {"enabled": False}}}))
        def git(command, **kwargs):
            if not verified_checkout:
                return subprocess.CompletedProcess(command, 1, "", "No matching checkout")
            if command == ["git", "rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(command, 0, str(checkout), "")
            if command == ["git", "remote", "get-url", "origin"]:
                return subprocess.CompletedProcess(command, 0, "https://github.com/owner/repo.git", "")
            raise AssertionError((command, kwargs))
        with patch.object(subprocess, "run", git), \
                patch.object(github_issue.github_identity, "acts_as_own_user", return_value=False), \
                patch.object(github_issue.github_identity, "active_auth_fallback_allowed", return_value=False):
            yield callback, records, comments, state_writes, comment_writes


def test_generic_cli_close_refreshes_a_nonplan_blockers_dependent_without_fact_edits() -> None:
    with generic_close_fixture() as (callback, records, comments, states, posts):
        dependent = copy.deepcopy(records[43])
        def run(calls):
            code, payload = _run_cli(["close", "owner/repo#42", "--comment", "Source delivered."], io.StringIO(""), timeout=10, planning=True)
            assert code == 0, payload
            assert payload["projection"]["coverage"] == "complete", payload
            # Diagnostics describe actual transport work across close, reads,
            # label changes and their readback, without losing close certainty.
            assert payload["attempts"] == len(calls), (payload, calls)
            assert payload["diagnostics"]["projection"], payload
            assert payload["attempts"] > payload["diagnostics"]["close"]["attempts"], payload
            assert payload["outcome_certainty"] == payload["diagnostics"]["close"]["outcome_certainty"] == "confirmed"
        with_call_stub(callback, run)
        assert states == [(42, "closed")], states
        assert posts == [42], posts
        assert comments[43] == []
        names = github_plan_status.DEFAULT_STATUS_LABELS
        assert {item["name"] for item in records[43]["labels"]} == {names[key] for key in ("plan", "active")}
        assert {key: value for key, value in records[43].items() if key != "labels"} == {
            key: value for key, value in dependent.items() if key != "labels"}


def test_generic_close_without_a_matching_checkout_preserves_configuration_review() -> None:
    for labels in ([], [{"name": "bug"}], [{"name": github_plan_status.DEFAULT_STATUS_LABELS["active"]}]):
        with generic_close_fixture(verified_checkout=False) as (callback, records, comments, states, posts):
            records[42]["labels"] = labels
            def run(calls):
                code, payload = _run_cli(["close", "owner/repo#42"], io.StringIO(""), timeout=10, planning=True)
                canonical_plan = bool(labels and labels[0]["name"] != "bug")
                assert (code != 0) == canonical_plan, payload
                assert payload["state"] == "closed", payload
                assert payload["projection"]["coverage"] == "partial", payload
                source = next(row for row in payload["projection"]["issues"] if row["number"] == 42)
                assert source["status"] == "review_planning_config", source
                assert source["required"] == canonical_plan, source
                assert not [call for call in calls if call["method"] not in {"GET", "PATCH"}], calls
            with_call_stub(callback, run)
            assert states == [(42, "closed")] and not posts
            assert records[42]["labels"] == labels


def test_generic_cli_projection_failure_preserves_confirmed_close_and_metadata_recovery() -> None:
    with generic_close_fixture(reject_labels=True) as (callback, records, comments, states, posts):
        def run(calls):
            code, payload = _run_cli(["close", "owner/repo#42", "--comment", "Source delivered."], io.StringIO(""), timeout=10, planning=True)
            assert code != 0 and not payload["ok"], payload
            assert payload["state"] == "closed", payload
            assert payload["recovery"]["issue_closed"] is True, payload
            assert payload["projection"]["coverage"] == "partial", payload
            import test_github_plan_graph
            plan = test_github_plan_graph.load_plan()
            def request(method, path, body=None, **kwargs):
                try:
                    result = github_issue._call_api(method, path, body, gh_cmd=github_issue.DEFAULT_GH,
                        operation="github.plan.reconcile_status", actor="fixture-automation",
                        expected_actor="fixture-automation", completed_steps=[],
                        failed_step=kwargs.get("failed_step") or "fixture_recovery", is_write=method != "GET",
                        retry_summaries=[])
                except github_issue.IssueError as error:
                    raise plan.PlanError(str(error), failure=error.failure,
                                         api_result=error.api_result, payload=error.payload) from error
                return result.actor or "fixture-automation", result.body
            plan.api_json = request
            plan.comment_route = lambda: ("fixture-automation", github_issue.DEFAULT_GH, "fixture-automation")
            plan.planning_comment_attention = lambda *_: False
            callback.reject_labels = False
            args = plan.build_parser().parse_args(shlex.split(payload["recovery"]["next_action"])[1:])
            with contextlib.redirect_stdout(io.StringIO()):
                args.func(args)
            names = github_plan_status.DEFAULT_STATUS_LABELS
            assert {item["name"] for item in records[43]["labels"]} == {names[key] for key in ("plan", "active")}
        with_call_stub(callback, run)
        assert records[42]["state"] == "closed"
        assert records[43]["state"] == "open"
        assert states == [(42, "closed")] and posts == [42], (states, posts)


def test_generic_closed_source_transient_comment_read_repairs_lifecycle_and_keeps_wait_review() -> None:
    with generic_close_fixture() as (callback, records, comments, states, posts):
        names = github_plan_status.DEFAULT_STATUS_LABELS
        records[42]["labels"] = [{"name": names[key]} for key in ("plan", "active", "waiting")]
        failures = []
        def transient(method, path, body, **kwargs):
            if method == "GET" and "/issues/42/comments" in path and records[42]["state"] == "closed":
                result = failure(503, {"message": "Fixture discussion transient failure"}, is_write=False)
                result.failure.request_id = "fixture-generic-discussion"
                failures.append(result.failure)
                return result
            return callback(method, path, body, **kwargs)
        def run(calls):
            code, payload = _run_cli(["close", "owner/repo#42", "--comment", "Source delivered."], io.StringIO(""), timeout=10, planning=True)
            assert code == 0 and payload["projection"]["coverage"] == "partial", payload
            source = next(row for row in payload["projection"]["issues"] if row["number"] == 42)
            assert source["status"] == "review_wait_record", source
            evidence = json.loads(source["wait"]["evidence"])
            assert evidence["failure"] == failures[-1].as_dict(), evidence
        with_call_stub(transient, run)
        assert {item["name"] for item in records[42]["labels"]} == {names[key] for key in ("plan", "done", "waiting")}
        assert {item["name"] for item in records[43]["labels"]} == {names[key] for key in ("plan", "active")}
        assert states == [(42, "closed")] and posts == [42], (states, posts)
        assert comments[43] == []


def test_generic_explicit_source_projection_denial_is_a_failed_required_outcome() -> None:
    with generic_close_fixture() as (callback, records, comments, states, posts):
        names = github_plan_status.DEFAULT_STATUS_LABELS
        records[42].update(labels=[{"name": names[key]} for key in ("plan", "active")],
            user={"login": "owner"}, body=github_plan_status.wait_records.marker(
                github_plan_status.wait_records.reviewed(evidence="Source completion reviewed")))
        callback.reject_labels = "source"
        def run(calls):
            code, payload = _run_cli(["close", "owner/repo#42"], io.StringIO(""), timeout=10, planning=True)
            assert code != 0 and not payload["ok"], payload
            source = next(row for row in payload["projection"]["issues"] if row["number"] == 42)
            assert source["required"] and not source["ok"], source
            assert source["status"] == "review_posting_authority", source
            assert payload["state"] == "closed" and payload["recovery"]["issue_closed"] is True, payload
        with_call_stub(callback, run)
        assert states == [(42, "closed")] and posts == [], (states, posts)
        assert {item["name"] for item in records[43]["labels"]} == {names[key] for key in ("plan", "blocked")}


def test_generic_missing_checkout_read_failure_keeps_required_source_and_provider_cause() -> None:
    for personal in (False, True):
        for status in (403, 503):
            with generic_close_fixture(verified_checkout=False) as (callback, records, comments, states, posts):
                def denied(method, path, body, **kwargs):
                    if method == "GET" and path.endswith("/issues/42") and records[42]["state"] == "closed":
                        result = failure(status, {"message": "Resource not accessible by integration" if status == 403 else "Service unavailable"}, is_write=False)
                        result.failure.request_id = "fixture-post-close-read"
                        return result
                    return callback(method, path, body, **kwargs)
                def run(calls):
                    with patch.object(github_issue.github_identity, "own_user_opted_in", return_value=personal):
                        code, payload = _run_cli(["close", "owner/repo#42"], io.StringIO(""), timeout=10, planning=True)
                    assert code != 0 and payload["state"] == "closed", payload
                    source = payload["projection"]["issues"][0]
                    assert source["number"] == 42 and source["required"] and not source["ok"], source
                    provider = source["error"]["failure"]
                    expected = github_plan_status.failure_disposition(provider["cause"], provider.get("write_outcome"), writes_started=False)
                    assert source["status"] == expected["status"], source
                    assert provider["request_id"] == "fixture-post-close-read", source
                    assert source["recovery"]["issue"] == {"repo": "owner/repo", "number": 42}, source
                    assert not [call for call in calls if "/issues/43" in call["path"] or "/labels" in call["path"]], calls
                with_call_stub(denied, run)
                assert states == [(42, "closed")] and not posts


def test_exact_personal_close_optin_never_fans_out_when_later_identity_lookup_disagrees() -> None:
    for lifecycle in (None, "active", "done"):
        with generic_close_fixture() as (callback, records, comments, states, posts):
            before = copy.deepcopy(records[43])
            if lifecycle:
                records[42]["labels"] = [{"name": github_plan_status.DEFAULT_STATUS_LABELS[key]} for key in ("plan", lifecycle)]
            def run(calls):
                with patch.object(github_issue.github_identity, "own_user_opted_in", return_value=True):
                    code, payload = _run_cli(["close", "owner/repo#42"], io.StringIO(""), timeout=10, planning=True)
                assert (code != 0) == (lifecycle == "active"), payload
                assert payload["projection"]["status"] == "review_posting_authority", payload
                assert payload["projection"]["coverage"] == "partial", payload
                assert not [call for call in calls if "/issues/43" in call["path"] or "/labels" in call["path"]], calls
            with_call_stub(callback, run)
            assert records[43] == before
            assert states == [(42, "closed")] and posts == [], (states, posts)


TESTS = [
    test_create_preserves_fields_and_emits_operation_marker,
    test_own_user_create_reports_success_without_duplicate_retry,
    test_own_user_create_rejects_wrong_response_author,
    test_own_user_sibling_writes_use_resolved_personal_actor,
    test_create_unknown_outcome_requires_reconciliation_before_retry,
    test_create_unknown_outcome_returns_unique_reconciled_issue,
    test_create_reconciliation_survives_explicit_actor_fallback,
    test_create_reconciliation_rejects_concurrent_identical_issue,
    test_create_reconciliation_rejects_preexisting_identical_issue,
    test_create_reconciliation_excludes_preexisting_same_second_issue,
    test_unknown_retry_enabled_issue_create_fails_closed_after_no_match,
    test_rejected_retry_enabled_issue_create_can_retry,
    test_edit_uses_rest_membership_endpoints_and_reads_after_write,
    test_edit_partial_failure_preserves_completed_steps_and_guidance,
    test_edit_absent_label_removal_completes_requested_edit,
    test_edit_retried_label_removal_confirms_unseen_success,
    test_edit_label_removal_requires_readable_absence,
    test_edit_label_removal_does_not_reconcile_denials_or_unrelated_404,
    test_edit_failure_after_reconciled_removal_preserves_remaining_intent,
    test_edit_first_mutation_failure_is_not_partial,
    test_edit_failed_readback_preserves_confirmed_writes,
    test_edit_malformed_readback_preserves_confirmed_writes,
    test_edit_partial_failure_preserves_actor_recovery_guidance,
    test_edit_rejects_cross_author_source_content_without_override,
    test_edit_allows_explicit_cross_author_source_content_override,
    test_metadata_only_edit_does_not_require_source_content_ownership,
    test_edit_local_validation_preserves_prior_retry_summary,
    test_close_partial_failure_preserves_comment_step,
    test_reopen_comment_uses_non_idempotent_comment_retry_policy,
    test_close_and_reopen_use_explicit_state_reasons,
    test_duplicate_close_resolves_database_id,
    test_invalid_duplicate_target_does_not_post_comment,
    test_mutation_parsers_accept_self_contained_targets_without_repo_resolution,
    test_close_reason_and_duplicate_target_are_mutually_exclusive,
    test_state_and_edit_commands_return_with_open_idle_stdin,
    test_explicit_comment_file_waits_for_slow_stdin_and_reads_paths,
    test_edit_body_file_can_clear_the_body,
    test_close_rejects_comment_with_comment_file,
    test_generic_cli_close_refreshes_a_nonplan_blockers_dependent_without_fact_edits,
    test_generic_close_without_a_matching_checkout_preserves_configuration_review,
    test_generic_cli_projection_failure_preserves_confirmed_close_and_metadata_recovery,
    test_generic_closed_source_transient_comment_read_repairs_lifecycle_and_keeps_wait_review,
    test_generic_explicit_source_projection_denial_is_a_failed_required_outcome,
    test_generic_missing_checkout_read_failure_keeps_required_source_and_provider_cause,
    test_exact_personal_close_optin_never_fans_out_when_later_identity_lookup_disagrees,
]


def main() -> None:
    for test in TESTS:
        test()
        print(f"ok {test.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
