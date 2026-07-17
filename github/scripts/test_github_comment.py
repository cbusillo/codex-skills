#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
from typing import Any, Callable

import github_api
import github_comment


def success(body: Any, *, headers: dict[str, str] | None = None) -> github_api.ApiResult:
    return github_api.ApiResult(
        ok=True,
        status=200,
        body=body,
        headers=headers or {},
        operation="github.comment.test",
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
        operation="github.comment.test",
        transport="rest_api",
        bucket="rest_core",
    )


def comment_body(
    comment_id: int,
    actor: str = "shiny-code-bot",
    *,
    created_at: str = "2026-07-16T12:00:00Z",
    body: str = "body",
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "html_url": f"https://github.com/owner/repo/issues/42#issuecomment-{comment_id}",
        "user": {"login": actor},
        "body": body,
        "created_at": created_at,
        "updated_at": created_at,
    }


def with_call_stub(
    callback: Callable[..., github_api.ApiResult],
    test: Callable[[list[dict[str, Any]]], None],
    *,
    allow_retry: bool = False,
) -> None:
    calls: list[dict[str, Any]] = []
    original = github_comment.github_api_core.call_gh
    original_policy = github_comment.github_api_core.default_retry_policy

    def stub(method: str, path: str, body: Any = None, **kwargs: Any) -> github_api.ApiResult:
        calls.append({"method": method, "path": path, "body": body, "kwargs": kwargs})
        return callback(method, path, body, **kwargs)

    with tempfile.TemporaryDirectory() as temp_dir:
        github_comment.github_api_core.call_gh = stub
        github_comment.github_api_core.default_retry_policy = lambda: github_api.RetryPolicy(
            max_wait_seconds=10.0,
            max_attempts=2 if allow_retry else 1,
            base_backoff_seconds=0.0,
            max_backoff_seconds=0.0,
            jitter_seconds=0.0,
            state_dir=pathlib.Path(temp_dir),
        )
        try:
            test(calls)
        finally:
            github_comment.github_api_core.call_gh = original
            github_comment.github_api_core.default_retry_policy = original_policy


def test_create_preserves_markdown_body() -> None:
    markdown = "## Result\n\n`literal` ${NOT_EXPANDED}\n"

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "shiny-code-bot"})
        if method == "GET":
            return success([])
        assert method == "POST"
        assert path == "/repos/owner/repo/issues/42/comments"
        assert body == {"body": markdown}
        return success(comment_body(1001))

    def run(calls: list[dict[str, Any]]) -> None:
        payload = github_comment.comment("issue", 42, markdown, repo="owner/repo", gh_cmd="fake-gh")
        assert payload["comment_action"] == "created", payload
        assert payload["url"].endswith("#issuecomment-1001"), payload
        assert payload["completed_steps"] == ["resolve_actor", "create_comment"], payload
        assert [call["method"] for call in calls] == ["GET", "GET", "POST"], calls

    with_call_stub(callback, run)


def test_edit_last_paginates_and_selects_latest_actor_comment() -> None:
    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "shiny-code-bot"})
        if "&page=1" in path:
            return success(
                [comment_body(1, "someone-else")],
                headers={"link": '<https://api.github.com/example?page=2>; rel="next"'},
            )
        if "&page=2" in path:
            return success([
                comment_body(2, created_at="2026-07-16T12:00:00Z"),
                comment_body(3, created_at="2026-07-16T13:00:00Z"),
            ])
        assert method == "PATCH"
        assert path == "/repos/owner/repo/issues/comments/3"
        assert body == {"body": "replacement"}
        return success(comment_body(3, created_at="2026-07-16T13:00:00Z"))

    def run(calls: list[dict[str, Any]]) -> None:
        payload = github_comment.comment(
            "pr",
            42,
            "replacement",
            repo="owner/repo",
            edit_last=True,
            gh_cmd="fake-gh",
        )
        assert payload["comment_action"] == "updated", payload
        assert payload["comment"]["id"] == 3, payload
        assert payload["completed_steps"][-1] == "update_comment", payload
        assert [call["method"] for call in calls] == ["GET", "GET", "GET", "PATCH"], calls

    with_call_stub(callback, run)


def test_edit_last_without_existing_comment_fails_closed() -> None:
    def callback(_method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "shiny-code-bot"})
        return success([comment_body(1, "someone-else")])

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            github_comment.comment(
                "issue",
                42,
                "replacement",
                repo="owner/repo",
                edit_last=True,
                gh_cmd="fake-gh",
            )
        except github_comment.CommentError as exc:
            assert exc.failure.cause == "comment_not_found", exc.failure
            assert exc.failure.write_outcome == "not_started", exc.failure
        else:
            raise AssertionError("expected comment_not_found")
        assert not any(call["method"] in ("POST", "PATCH") for call in calls), calls

    with_call_stub(callback, run)


def test_create_if_none_creates_only_when_initial_lookup_is_empty() -> None:
    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "shiny-code-bot"})
        if method == "GET":
            return success([])
        assert method == "POST"
        return success(comment_body(4))

    def run(calls: list[dict[str, Any]]) -> None:
        payload = github_comment.comment(
            "pr",
            42,
            "new comment",
            repo="owner/repo",
            edit_last=True,
            create_if_none=True,
            gh_cmd="fake-gh",
        )
        assert payload["comment_action"] == "created", payload
        assert payload["completed_steps"][-1] == "create_comment", payload
        assert [call["method"] for call in calls] == ["GET", "GET", "POST"], calls

    with_call_stub(callback, run)


def test_deletion_race_never_falls_back_to_create() -> None:
    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "shiny-code-bot"})
        if method == "GET":
            return success([comment_body(5)])
        assert method == "PATCH"
        return failure(404, {"message": "Not Found"}, is_write=True)

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            github_comment.comment(
                "issue",
                42,
                "replacement",
                repo="owner/repo",
                edit_last=True,
                create_if_none=True,
                gh_cmd="fake-gh",
            )
        except github_comment.CommentError as exc:
            assert exc.failure.cause == "not_found", exc.failure
            assert exc.payload["selected_comment_id"] == 5, exc.payload
            assert exc.payload["reconciliation"]["creation_skipped"] is True, exc.payload
        else:
            raise AssertionError("expected deletion-race failure")
        assert [call["method"] for call in calls] == ["GET", "GET", "PATCH"], calls

    with_call_stub(callback, run)


def test_actor_mismatch_blocks_mutation() -> None:
    def callback(_method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        assert path == "/user"
        return success({"login": "unexpected-user"})

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            github_comment.comment("issue", 42, "body", repo="owner/repo", gh_cmd="fake-gh")
        except github_comment.CommentError as exc:
            assert exc.failure.cause == "actor_mismatch", exc.failure
            assert exc.failure.write_outcome == "not_started", exc.failure
        else:
            raise AssertionError("expected actor mismatch")
        assert len(calls) == 1, calls

    with_call_stub(callback, run)


def test_create_if_none_requires_edit_last() -> None:
    try:
        github_comment.comment(
            "issue",
            42,
            "body",
            repo="owner/repo",
            create_if_none=True,
            gh_cmd="fake-gh",
        )
    except github_comment.CommentError as exc:
        assert exc.failure.cause == "validation_error", exc.failure
        assert exc.failure.write_outcome == "not_started", exc.failure
    else:
        raise AssertionError("expected validation error")


def test_explicit_active_fallback_accepts_and_reports_actual_actor() -> None:
    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "cbusillo"})
        if method == "GET":
            return success([])
        assert method == "POST"
        return success(comment_body(8, "cbusillo"))

    def run(_calls: list[dict[str, Any]]) -> None:
        original = os.environ.get("GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK")
        os.environ["GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK"] = "1"
        try:
            payload = github_comment.comment("issue", 42, "body", repo="owner/repo", gh_cmd="fake-gh")
        finally:
            if original is None:
                os.environ.pop("GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK", None)
            else:
                os.environ["GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK"] = original
        assert payload["actor"] == "cbusillo", payload
        assert payload["expected_actor"] is None, payload

    with_call_stub(callback, run)


def test_active_fallback_reports_response_actor_after_route_switch() -> None:
    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "shiny-code-bot"})
        if method == "GET":
            return success([])
        assert method == "POST"
        return success(comment_body(9, "cbusillo"))

    def run(_calls: list[dict[str, Any]]) -> None:
        original = os.environ.get("GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK")
        os.environ["GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK"] = "1"
        try:
            payload = github_comment.comment("issue", 42, "body", repo="owner/repo", gh_cmd="fake-gh")
        finally:
            if original is None:
                os.environ.pop("GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK", None)
            else:
                os.environ["GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK"] = original
        assert payload["actor"] == "cbusillo", payload
        assert payload["comment"]["author"] == "cbusillo", payload
        assert payload["expected_actor"] is None, payload

    with_call_stub(callback, run)


def test_unknown_create_no_match_fails_closed_without_repeat() -> None:
    post_calls = 0

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        nonlocal post_calls
        if path == "/user":
            return success({"login": "shiny-code-bot"})
        if method == "GET":
            assert path.startswith("/repos/owner/repo/issues/42/comments?"), path
            return success([])
        post_calls += 1
        return failure(503, "Unicorn!", is_write=True)

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            github_comment.comment(
                "issue",
                42,
                "retry-safe body",
                repo="owner/repo",
                gh_cmd="fake-gh",
            )
        except github_comment.CommentError as exc:
            assert exc.failure.write_outcome == "unknown", exc.failure
            assert exc.payload["reconciliation"]["result"] == "no_match", exc.payload
            assert exc.payload["retry_eligible"] is False, exc.payload
        else:
            raise AssertionError("unknown comment create must fail closed after no-match reconciliation")
        assert post_calls == 1, post_calls
        assert [call["method"] for call in calls] == ["GET", "GET", "POST", "GET"], calls

    with_call_stub(callback, run, allow_retry=True)


def test_rejected_create_retries_without_reconciliation() -> None:
    post_calls = 0

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        nonlocal post_calls
        if path == "/user":
            return success({"login": "shiny-code-bot"})
        if method == "GET":
            return success([])
        assert method == "POST", (method, path)
        post_calls += 1
        if post_calls == 1:
            return failure(429, "API rate limit exceeded", is_write=True)
        return success(comment_body(1010, body=str(body["body"])))

    def run(calls: list[dict[str, Any]]) -> None:
        payload = github_comment.comment(
            "issue",
            42,
            "retry-safe body",
            repo="owner/repo",
            gh_cmd="fake-gh",
        )
        assert payload["attempts"] == 4, payload
        assert payload["reconciliation"] is None, payload
        assert payload["operation_marker"]["kind"] == "request_fingerprint", payload
        assert [call["method"] for call in calls] == ["GET", "GET", "POST", "POST"], calls

    with_call_stub(callback, run, allow_retry=True)


def test_create_unknown_outcome_returns_reconciled_comment_without_retry() -> None:
    original_now = github_comment._utc_now
    github_comment._utc_now = lambda: github_comment.dt.datetime(
        2026,
        7,
        17,
        18,
        30,
        tzinfo=github_comment.dt.timezone.utc,
    )
    post_calls = 0
    get_calls = 0

    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        nonlocal get_calls, post_calls
        if path == "/user":
            return success({"login": "shiny-code-bot"})
        if method == "POST":
            post_calls += 1
            return failure(503, "Unicorn!", is_write=True)
        get_calls += 1
        if get_calls == 1:
            return success([])
        return success([
            comment_body(
                1011,
                created_at="2026-07-17T18:29:58Z",
                body="reconciled body",
            )
        ])

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            payload = github_comment.comment(
                "pr",
                42,
                "reconciled body",
                repo="owner/repo",
                gh_cmd="fake-gh",
                operation="github.pr.comment",
            )
        finally:
            github_comment._utc_now = original_now
        assert post_calls == 1, post_calls
        assert payload["attempts"] == 4, payload
        assert payload["outcome_certainty"] == "reconciled_applied", payload
        assert payload["reconciliation"]["result"] == "matched", payload
        assert payload["completed_steps"][-1] == "reconcile_create_comment", payload
        assert [call["method"] for call in calls] == ["GET", "GET", "POST", "GET"], calls

    with_call_stub(callback, run, allow_retry=True)


def test_create_reconciliation_uses_explicit_fallback_actor_context() -> None:
    original_now = github_comment._utc_now
    original_fallback = os.environ.get("GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK")
    github_comment._utc_now = lambda: github_comment.dt.datetime(
        2026,
        7,
        17,
        18,
        45,
        tzinfo=github_comment.dt.timezone.utc,
    )
    os.environ["GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK"] = "1"
    post_calls = 0
    get_calls = 0

    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        nonlocal get_calls, post_calls
        if path == "/user":
            return success({"login": "shiny-code-bot"})
        if method == "POST":
            post_calls += 1
            result = failure(503, "Unicorn!", is_write=True)
            result.actor = "cbusillo"
            result.expected_actor = None
            return result
        get_calls += 1
        if get_calls == 1:
            return success([])
        return success([
            comment_body(
                1012,
                actor="cbusillo",
                created_at="2026-07-17T18:45:01Z",
                body="fallback body",
            )
        ])

    def run(_calls: list[dict[str, Any]]) -> None:
        try:
            payload = github_comment.comment(
                "issue",
                42,
                "fallback body",
                repo="owner/repo",
                gh_cmd="fake-gh",
            )
        finally:
            github_comment._utc_now = original_now
            if original_fallback is None:
                os.environ.pop("GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK", None)
            else:
                os.environ["GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK"] = original_fallback
        assert post_calls == 1, post_calls
        assert payload["actor"] == "cbusillo", payload
        assert payload["expected_actor"] is None, payload
        assert payload["reconciliation"]["result"] == "matched", payload

    with_call_stub(callback, run, allow_retry=True)


def test_create_reconciliation_excludes_preexisting_same_second_comment() -> None:
    original_now = github_comment._utc_now
    github_comment._utc_now = lambda: github_comment.dt.datetime(
        2026,
        7,
        17,
        19,
        0,
        tzinfo=github_comment.dt.timezone.utc,
    )
    existing = comment_body(
        1013,
        created_at="2026-07-17T19:00:00Z",
        body="original body",
    )
    post_calls = 0
    get_calls = 0

    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        nonlocal get_calls, post_calls
        if path == "/user":
            return success({"login": "shiny-code-bot"})
        if method == "POST":
            post_calls += 1
            return failure(503, "Unicorn!", is_write=True)
        get_calls += 1
        if get_calls > 1:
            return success([{**existing, "body": "same-second body"}])
        return success([existing])

    def run(_calls: list[dict[str, Any]]) -> None:
        try:
            try:
                github_comment.comment(
                    "issue",
                    42,
                    "same-second body",
                    repo="owner/repo",
                    gh_cmd="fake-gh",
                )
            except github_comment.CommentError as exc:
                reconciliation = exc.payload["reconciliation"]
                assert reconciliation["result"] == "no_match", reconciliation
                assert reconciliation["preexisting_comment_ids"] == [1013], reconciliation
            else:
                raise AssertionError("pre-existing same-second comment must not satisfy reconciliation")
        finally:
            github_comment._utc_now = original_now
        assert post_calls == 1, post_calls

    with_call_stub(callback, run, allow_retry=True)


def test_repo_resolution_launch_failure_is_structured() -> None:
    original_run = github_comment.subprocess.run
    original_repo = os.environ.pop("GH_REPO", None)

    def fake_run(command: list[str], **_kwargs: Any) -> Any:
        if command[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(command, 1, "", "no remote")
        raise FileNotFoundError(command[0])

    github_comment.subprocess.run = fake_run
    try:
        try:
            github_comment.resolve_repo(None, gh_cmd="missing-gh", operation="github.comment.issue")
        except github_comment.CommentError as exc:
            assert exc.failure.cause == "subprocess_launch_failure", exc.failure
            assert exc.failure.failed_step == "resolve_repository", exc.failure
            assert exc.api_result is not None
        else:
            raise AssertionError("expected structured launch failure")
    finally:
        github_comment.subprocess.run = original_run
        if original_repo is not None:
            os.environ["GH_REPO"] = original_repo


TESTS = [
    test_create_preserves_markdown_body,
    test_edit_last_paginates_and_selects_latest_actor_comment,
    test_edit_last_without_existing_comment_fails_closed,
    test_create_if_none_creates_only_when_initial_lookup_is_empty,
    test_deletion_race_never_falls_back_to_create,
    test_actor_mismatch_blocks_mutation,
    test_create_if_none_requires_edit_last,
    test_explicit_active_fallback_accepts_and_reports_actual_actor,
    test_active_fallback_reports_response_actor_after_route_switch,
    test_unknown_create_no_match_fails_closed_without_repeat,
    test_rejected_create_retries_without_reconciliation,
    test_create_unknown_outcome_returns_reconciled_comment_without_retry,
    test_create_reconciliation_uses_explicit_fallback_actor_context,
    test_create_reconciliation_excludes_preexisting_same_second_comment,
    test_repo_resolution_launch_failure_is_structured,
]


def main() -> None:
    for test in TESTS:
        test()
        print(f"ok {test.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
