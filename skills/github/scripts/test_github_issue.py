#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
from typing import Any, Callable

os.environ["CODEX_SKILLS_ENV_FILE"] = "/definitely/missing/codex-skills-test.env"
os.environ["CODEX_AUTOMATION_LOGIN"] = "fixture-automation"
os.environ["CODEX_AUTOMATION_EMAIL"] = "fixture-automation@example.invalid"
os.environ["GH_WITH_ENV_TOKEN_EXPECTED_LOGIN"] = "fixture-automation"

import github_api
import github_issue
from github_fixture_support import with_call_stub


def success(
    body: Any,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> github_api.ApiResult:
    return github_api.ApiResult(
        ok=True,
        status=status,
        body=body,
        headers=headers or {},
        operation="github.issue.test",
        transport="rest_api",
        bucket="rest_core",
    )


def failure(status: int, body: Any, *, is_write: bool) -> github_api.ApiResult:
    detail = github_api.classify_error(status, {}, body, is_write=is_write)
    return github_api.ApiResult(
        ok=False,
        status=status,
        body=body,
        failure=detail,
        operation="github.issue.test",
        transport="rest_api",
        bucket="rest_core",
    )


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
    original_now = github_issue._utc_now
    github_issue._utc_now = lambda: github_issue.dt.datetime(
        2026,
        7,
        16,
        22,
        0,
        tzinfo=github_issue.dt.timezone.utc,
    )

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
        try:
            payload = github_issue.create_issue(
                "Issue title",
                "body",
                repo="owner/repo",
                gh_cmd="fake-gh",
            )
        finally:
            github_issue._utc_now = original_now
        assert payload["reconciled"] is True, payload
        assert payload["reconciliation"]["result"] == "matched", payload
        assert payload["completed_steps"] == ["resolve_actor", "reconcile_create"], payload
        assert payload["url"].endswith("/issues/42"), payload
        assert [call["method"] for call in calls] == ["GET", "GET", "POST", "GET"], calls

    with_call_stub(callback, run)


def test_create_reconciliation_survives_explicit_actor_fallback() -> None:
    original_now = github_issue._utc_now
    original_fallback = os.environ.get("GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK")
    github_issue._utc_now = lambda: github_issue.dt.datetime(
        2026,
        7,
        16,
        22,
        0,
        tzinfo=github_issue.dt.timezone.utc,
    )
    os.environ["GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK"] = "1"

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
        try:
            payload = github_issue.create_issue(
                "Issue title",
                "body",
                repo="owner/repo",
                gh_cmd="fake-gh",
            )
        finally:
            github_issue._utc_now = original_now
            if original_fallback is None:
                os.environ.pop("GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK", None)
            else:
                os.environ["GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK"] = original_fallback
        assert payload["reconciled"] is True, payload
        assert payload["actor"] == "cbusillo", payload
        assert payload["expected_actor"] is None, payload

    with_call_stub(callback, run)


def test_create_reconciliation_rejects_concurrent_identical_issue() -> None:
    original_now = github_issue._utc_now
    github_issue._utc_now = lambda: github_issue.dt.datetime(
        2026,
        7,
        17,
        3,
        20,
        tzinfo=github_issue.dt.timezone.utc,
    )
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
        try:
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
        finally:
            github_issue._utc_now = original_now

    with_call_stub(callback, run, allow_retry=True)


def test_create_reconciliation_rejects_preexisting_identical_issue() -> None:
    original_now = github_issue._utc_now
    github_issue._utc_now = lambda: github_issue.dt.datetime(
        2026,
        7,
        16,
        22,
        0,
        tzinfo=github_issue.dt.timezone.utc,
    )

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
        try:
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
        finally:
            github_issue._utc_now = original_now

    with_call_stub(callback, run)


def test_create_reconciliation_excludes_preexisting_same_second_issue() -> None:
    original_now = github_issue._utc_now
    github_issue._utc_now = lambda: github_issue.dt.datetime(
        2026,
        7,
        16,
        22,
        0,
        tzinfo=github_issue.dt.timezone.utc,
    )
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
        try:
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
        finally:
            github_issue._utc_now = original_now
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


def test_edit_absent_label_failure_reports_partial_writes_and_remaining_work() -> None:
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
        try:
            github_issue.edit_issue(
                42,
                repo="owner/repo",
                add_labels=["plan:waiting"],
                remove_labels=["plan:blocked", "plan:active", "later"],
                gh_cmd="fake-gh",
            )
        except github_issue.IssueError as exc:
            envelope = github_issue._terminal_failure(
                exc, "github.issue.edit", expected_actor="fixture-automation"
            )
        else:
            raise AssertionError("absent-label removal should remain a terminal failure")
        assert labels == {"plan", "plan:waiting"}, labels
        assert envelope["write_outcome"] == "partially_applied", envelope
        assert envelope["outcome_certainty"] == "confirmed_partially_applied", envelope
        assert envelope["failure"]["write_outcome"] == "partially_applied", envelope
        assert envelope["completed_steps"] == ["resolve_actor", "add_labels", "remove_label"], envelope
        assert envelope["failed_step"] == "remove_label", envelope
        assert envelope["failed_request"]["write_outcome"] == "not_started", envelope
        assert envelope["failed_request"]["outcome_certainty"] == "confirmed_not_applied", envelope
        assert envelope["retryable"] is False and envelope["fallback_eligible"] is False, envelope
        recovery = envelope["reconciliation"]
        assert recovery["completed"]["add_labels"] == ["plan:waiting"], recovery
        assert recovery["completed"]["remove_labels"] == ["plan:blocked"], recovery
        assert recovery["remaining"]["add_labels"] == [], recovery
        assert recovery["remaining"]["remove_labels"] == ["plan:active", "later"], recovery
        assert recovery["required_before_retry"] is True, recovery
        assert [call["method"] for call in calls] == ["GET", "POST", "DELETE", "DELETE"], calls
        # The caller's readback uses the supplied endpoint; no completed write is replayed.
        readback = github_issue.github_api_core.call_gh("GET", recovery["endpoint"])
        actual = {label["name"] for label in readback.body["labels"]}
        remaining_removals = [label for label in recovery["remaining"]["remove_labels"] if label in actual]
        assert remaining_removals == [], remaining_removals

    with_call_stub(callback, run, allow_retry=True)


def test_edit_first_mutation_failure_is_not_partial() -> None:
    def callback(_method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        return failure(404, {"message": "Label does not exist"}, is_write=True)

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


def _run_cli(argv: list[str], stdin: Any, *, timeout: float) -> tuple[int, dict[str, Any]]:
    result: dict[str, Any] = {}
    original_argv, original_stdin = sys.argv, sys.stdin

    def target() -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
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


TESTS = [
    test_create_preserves_fields_and_emits_operation_marker,
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
    test_edit_absent_label_failure_reports_partial_writes_and_remaining_work,
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
]


def main() -> None:
    for test in TESTS:
        test()
        print(f"ok {test.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
