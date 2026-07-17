#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import os
from typing import Any, Callable

import github_api
import github_issue


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
    actor: str = "shiny-code-bot",
    state: str = "open",
    state_reason: str | None = None,
    body: str = "body",
    created_at: str = "2026-07-16T22:00:00Z",
) -> dict[str, Any]:
    return {
        "id": 9000 + number,
        "number": number,
        "title": "Issue title",
        "body": body,
        "created_at": created_at,
        "state": state,
        "state_reason": state_reason,
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "user": {"login": actor},
        "labels": [{"name": "plan"}],
        "assignees": [{"login": actor}],
        "milestone": {"number": 7, "title": "Sprint 7"},
    }


def with_call_stub(callback: Callable[..., github_api.ApiResult], test: Callable[[list[dict[str, Any]]], None]) -> None:
    calls: list[dict[str, Any]] = []
    original = github_issue.github_api_core.call_gh

    def stub(method: str, path: str, body: Any = None, **kwargs: Any) -> github_api.ApiResult:
        calls.append({"method": method, "path": path, "body": body, "kwargs": kwargs})
        return callback(method, path, body, **kwargs)

    github_issue.github_api_core.call_gh = stub
    try:
        test(calls)
    finally:
        github_issue.github_api_core.call_gh = original


def test_create_preserves_fields_and_emits_operation_marker() -> None:
    markdown = "## Result\n\n`literal` ${NOT_EXPANDED}\n"

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "shiny-code-bot"})
        if "/milestones?" in path:
            return success([{"number": 7, "title": "Sprint 7"}])
        assert method == "POST"
        assert path == "/repos/owner/repo/issues"
        assert body == {
            "title": "Issue title",
            "body": markdown,
            "labels": ["plan", "enhancement"],
            "assignees": ["shiny-code-bot", "octocat", "copilot-swe-agent[bot]"],
            "milestone": 7,
        }
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
        assert payload["actor"] == "shiny-code-bot", payload
        assert payload["completed_steps"] == ["resolve_actor", "resolve_milestone", "create_issue"], payload
        assert [call["method"] for call in calls] == ["GET", "GET", "POST"], calls

    with_call_stub(callback, run)


def test_create_unknown_outcome_requires_reconciliation_before_retry() -> None:
    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "shiny-code-bot"})
        if method == "POST":
            return failure(503, "Unicorn!", is_write=True)
        assert method == "GET"
        assert "creator=shiny-code-bot" in path
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
            assert reconciliation["attempted"] is True, reconciliation
            assert reconciliation["result"] == "no_match", reconciliation
        else:
            raise AssertionError("expected unknown create outcome")
        assert [call["method"] for call in calls] == ["GET", "POST", "GET"], calls

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

    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "shiny-code-bot"})
        if method == "POST":
            return failure(503, "Unicorn!", is_write=True)
        matched = issue_body(body="body", created_at="2026-07-16T22:00:01Z")
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
        assert [call["method"] for call in calls] == ["GET", "POST", "GET"], calls

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

    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "shiny-code-bot"})
        if method == "POST":
            return failure(503, "Unicorn!", is_write=True)
        assert "creator=" not in path, path
        matched = issue_body(
            actor="cbusillo",
            body="body",
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
            return success({"login": "shiny-code-bot"})
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


def test_edit_uses_rest_membership_endpoints_and_reads_after_write() -> None:
    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "shiny-code-bot"})
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
            assert body == {"assignees": ["shiny-code-bot"]}
            return success(issue_body())
        assert method == "GET"
        assert path == "/repos/owner/repo/issues/42"
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
            "edit_issue_fields",
            "add_labels",
            "remove_label",
            "add_assignees",
            "remove_assignees",
            "read_after_write",
        ], payload
        assert [call["method"] for call in calls] == [
            "GET",
            "PATCH",
            "POST",
            "DELETE",
            "POST",
            "DELETE",
            "GET",
        ], calls

    with_call_stub(callback, run)


def test_edit_partial_failure_preserves_completed_steps_and_guidance() -> None:
    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "shiny-code-bot"})
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
            assert exc.failure.completed_steps == ["resolve_actor", "edit_issue_fields"], exc.failure
            assert exc.payload["reconciliation"]["strategy"] == "read_issue_and_compare_requested_fields"
        else:
            raise AssertionError("expected partial edit failure")
        assert [call["method"] for call in calls] == ["GET", "PATCH", "POST"], calls

    with_call_stub(callback, run)


def test_close_partial_failure_preserves_comment_step() -> None:
    original_comment = github_issue.github_comment.comment

    def fake_comment(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"actor": "shiny-code-bot"}

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "shiny-code-bot"})
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
            else:
                raise AssertionError("expected close failure")
        finally:
            github_issue.github_comment.comment = original_comment
        assert [call["method"] for call in calls] == ["GET", "PATCH"], calls

    with_call_stub(callback, run)


def test_close_and_reopen_use_explicit_state_reasons() -> None:
    expected = [
        ("closed", "not_planned"),
        ("open", "reopened"),
    ]

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "shiny-code-bot"})
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
        assert [call["method"] for call in calls] == ["GET", "PATCH", "GET", "PATCH"], calls

    with_call_stub(callback, run)


def test_duplicate_close_resolves_database_id() -> None:
    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "shiny-code-bot"})
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
        return {"actor": "shiny-code-bot"}

    def callback(_method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        assert path == "/user"
        return success({"login": "shiny-code-bot"})

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
            else:
                raise AssertionError("expected invalid duplicate target")
        finally:
            github_issue.github_comment.comment = original_comment
        assert comment_called is False
        assert len(calls) == 1, calls

    with_call_stub(callback, run)


TESTS = [
    test_create_preserves_fields_and_emits_operation_marker,
    test_create_unknown_outcome_requires_reconciliation_before_retry,
    test_create_unknown_outcome_returns_unique_reconciled_issue,
    test_create_reconciliation_survives_explicit_actor_fallback,
    test_create_reconciliation_rejects_preexisting_identical_issue,
    test_edit_uses_rest_membership_endpoints_and_reads_after_write,
    test_edit_partial_failure_preserves_completed_steps_and_guidance,
    test_close_partial_failure_preserves_comment_step,
    test_close_and_reopen_use_explicit_state_reasons,
    test_duplicate_close_resolves_database_id,
    test_invalid_duplicate_target_does_not_post_comment,
    test_mutation_parsers_accept_self_contained_targets_without_repo_resolution,
    test_close_reason_and_duplicate_target_are_mutually_exclusive,
]


def main() -> None:
    for test in TESTS:
        test()
        print(f"ok {test.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
