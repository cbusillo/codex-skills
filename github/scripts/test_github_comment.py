#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import os
import subprocess
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


def comment_body(comment_id: int, actor: str = "shiny-code-bot", *, created_at: str = "2026-07-16T12:00:00Z") -> dict[str, Any]:
    return {
        "id": comment_id,
        "html_url": f"https://github.com/owner/repo/issues/42#issuecomment-{comment_id}",
        "user": {"login": actor},
        "created_at": created_at,
        "updated_at": created_at,
    }


def with_call_stub(callback: Callable[..., github_api.ApiResult], test: Callable[[list[dict[str, Any]]], None]) -> None:
    calls: list[dict[str, Any]] = []
    original = github_comment.github_api_core.call_gh

    def stub(method: str, path: str, body: Any = None, **kwargs: Any) -> github_api.ApiResult:
        calls.append({"method": method, "path": path, "body": body, "kwargs": kwargs})
        return callback(method, path, body, **kwargs)

    github_comment.github_api_core.call_gh = stub
    try:
        test(calls)
    finally:
        github_comment.github_api_core.call_gh = original


def test_create_preserves_markdown_body() -> None:
    markdown = "## Result\n\n`literal` ${NOT_EXPANDED}\n"

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "shiny-code-bot"})
        assert method == "POST"
        assert path == "/repos/owner/repo/issues/42/comments"
        assert body == {"body": markdown}
        return success(comment_body(1001))

    def run(calls: list[dict[str, Any]]) -> None:
        payload = github_comment.comment("issue", 42, markdown, repo="owner/repo", gh_cmd="fake-gh")
        assert payload["comment_action"] == "created", payload
        assert payload["url"].endswith("#issuecomment-1001"), payload
        assert payload["completed_steps"] == ["resolve_actor", "create_comment"], payload
        assert len(calls) == 2, calls

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
    test_repo_resolution_launch_failure_is_structured,
]


def main() -> None:
    for test in TESTS:
        test()
        print(f"ok {test.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
