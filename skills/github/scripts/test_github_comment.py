#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import sys
import datetime as dt
import math
import urllib.parse
from typing import Any, Callable
from unittest.mock import patch

os.environ["CODEX_SKILLS_ENV_FILE"] = "/definitely/missing/codex-skills-test.env"
os.environ["CODEX_AUTOMATION_LOGIN"] = "fixture-automation"
os.environ["CODEX_AUTOMATION_EMAIL"] = "fixture-automation@example.invalid"
os.environ["GH_WITH_ENV_TOKEN_EXPECTED_LOGIN"] = "fixture-automation"

import github_api
import github_comment
from github_fixture_support import api_failure, api_success, with_call_stub


def success(body: Any, *, headers: dict[str, str] | None = None) -> github_api.ApiResult:
    return api_success(body, operation="github.comment.test", headers=headers)


def failure(status: int, body: Any, *, is_write: bool) -> github_api.ApiResult:
    return api_failure(status, body, operation="github.comment.test", is_write=is_write)


def comment_body(
    comment_id: int,
    actor: str = "fixture-automation",
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


def test_create_preserves_markdown_body() -> None:
    markdown = "## Result\n\n`literal` ${NOT_EXPANDED}\n"

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "GET":
            return success([])
        assert method == "POST"
        assert path == "/repos/owner/repo/issues/42/comments"
        assert body["body"].startswith(markdown)
        assert github_api.body_has_operation_marker(
            body["body"],
            body["body"].split(github_api.OPERATION_MARKER_PREFIX, 1)[1].split(" ", 1)[0],
        )
        return success(comment_body(1001))

    def run(calls: list[dict[str, Any]]) -> None:
        payload = github_comment.comment("issue", 42, markdown, repo="owner/repo", gh_cmd="fake-gh")
        assert payload["comment_action"] == "created", payload
        assert payload["url"].endswith("#issuecomment-1001"), payload
        assert payload["completed_steps"] == ["resolve_actor", "create_comment"], payload
        assert [call["method"] for call in calls] == ["GET", "GET", "POST"], calls

    with_call_stub(callback, run)


def test_dedupe_body_reuses_existing_actor_comment_without_write() -> None:
    markdown = "Completed with evidence.\n"
    existing = comment_body(
        1002,
        body=github_api.body_with_operation_marker(markdown, "a" * 32),
    )

    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "GET":
            return success([comment_body(1001, "someone-else", body=markdown), existing])
        raise AssertionError("deduplicated comment must not write")

    def run(calls: list[dict[str, Any]]) -> None:
        payload = github_comment.comment(
            "issue",
            42,
            markdown,
            repo="owner/repo",
            gh_cmd="fake-gh",
            dedupe_body=True,
        )
        assert payload["comment_action"] == "existing", payload
        assert payload["deduplicated"] is True, payload
        assert payload["comment"]["id"] == 1002, payload
        assert payload["completed_steps"] == ["resolve_actor", "reuse_existing_comment"], payload
        assert [call["method"] for call in calls] == ["GET", "GET"], calls

    with_call_stub(callback, run)


def test_edit_last_paginates_and_selects_latest_actor_comment() -> None:
    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
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
            return success({"login": "fixture-automation"})
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
            assert exc.payload["attempts"] == 2, exc.payload
            assert exc.payload["outcome_certainty"] == "confirmed", exc.payload
            assert exc.api_result is not None
            assert exc.api_result["attempts"] == 2, exc.api_result
        else:
            raise AssertionError("expected comment_not_found")
        assert not any(call["method"] in ("POST", "PATCH") for call in calls), calls

    with_call_stub(callback, run)


def test_create_if_none_creates_only_when_initial_lookup_is_empty() -> None:
    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
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
            return success({"login": "fixture-automation"})
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


def test_own_user_write_reports_resolved_actor_and_rejects_wrong_author() -> None:
    for author in ("contributor", "unexpected-user"):
        def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
            if path == "/user":
                return success({"login": "fixture-automation"})
            if method == "GET":
                return success([])
            result = success(comment_body(10, author))
            result.actor = "contributor"
            result.expected_actor = None  # Authorized own-user notice from the wrapper.
            return result

        def run(calls: list[dict[str, Any]]) -> None:
            with patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_OWN_USER": "1"}):
                try:
                    payload = github_comment.comment("pr", 42, "body", repo="owner/repo", gh_cmd="fake-gh")
                except github_comment.CommentError as exc:
                    assert author == "unexpected-user", exc
                    assert exc.failure.cause == "actor_mismatch", exc.failure
                    assert exc.failure.write_outcome == "unknown", exc.failure
                else:
                    assert author == "contributor", payload
                    assert payload["actor"] == payload["expected_actor"] == author, payload
                    assert payload["outcome_certainty"] == "confirmed", payload
                assert sum(call["method"] == "POST" for call in calls) == 1, calls

        with_call_stub(callback, run)


def test_own_user_opt_in_does_not_override_automation_response_context() -> None:
    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "GET":
            return success([])
        result = success(comment_body(11, "unexpected-user"))
        result.actor = result.expected_actor = "fixture-automation"
        return result

    def run(calls: list[dict[str, Any]]) -> None:
        with patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_OWN_USER": "1"}):
            try:
                github_comment.comment("pr", 42, "body", repo="owner/repo", gh_cmd="fake-gh")
            except github_comment.CommentError as exc:
                assert exc.failure.cause == "actor_mismatch", exc.failure
            else:
                raise AssertionError("expected actor mismatch")
        assert sum(call["method"] == "POST" for call in calls) == 1, calls

    with_call_stub(callback, run)


def test_own_user_invalid_response_reports_resolved_actor() -> None:
    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "GET":
            return success([])
        result = success({})
        result.actor = "contributor"
        result.expected_actor = None
        return result

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            github_comment.comment("pr", 42, "body", repo="owner/repo", gh_cmd="fake-gh")
        except github_comment.CommentError as exc:
            assert exc.failure.cause == "invalid_response", exc.failure
            assert exc.failure.write_outcome == "unknown", exc.failure
            assert exc.api_result is not None
            assert exc.api_result["actor"] == exc.api_result["expected_actor"] == "contributor", exc.api_result
        else:
            raise AssertionError("expected invalid response")
        assert sum(call["method"] == "POST" for call in calls) == 1, calls

    with_call_stub(callback, run)


def test_edit_preserves_comment_author_after_authorized_route_switch() -> None:
    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "GET":
            return success([comment_body(14)])
        assert method == "PATCH"
        result = success(comment_body(14))  # Editing does not change the original author.
        result.actor = "contributor"
        result.expected_actor = None
        return result

    def run(calls: list[dict[str, Any]]) -> None:
        payload = github_comment.comment(
            "pr", 42, "body", repo="owner/repo", gh_cmd="fake-gh", edit_last=True
        )
        assert payload["comment"]["author"] == "fixture-automation", payload
        assert payload["actor"] == "contributor", payload
        assert payload["outcome_certainty"] == "confirmed", payload
        assert [call["method"] for call in calls] == ["GET", "GET", "PATCH"], calls

    with_call_stub(callback, run)


def test_own_user_actor_is_resolved_before_edit_selection_and_dedupe() -> None:
    for mode in ("edit_last", "edit_comment", "dedupe_body"):
        contributor_comment = comment_body(20, "contributor", body="replacement")
        contributor_comment["issue_url"] = "https://api.github.com/repos/owner/repo/issues/42"

        def callback(method: str, path: str, _body: Any, **kwargs: Any) -> github_api.ApiResult:
            if path == "/user":
                if kwargs.get("gh_prefix_args") != ["--write-actor-for", "owner/repo"]:
                    return success({"login": "fixture-automation"})
                result = success({"login": "contributor"})
                result.actor = "contributor"
                result.expected_actor = None
                return result
            assert kwargs["actor"] == kwargs["expected_actor"] == "contributor", kwargs
            if method == "GET" and "/issues/comments/" not in path:
                return success([comment_body(30), contributor_comment, comment_body(40, "foreign")])
            assert path == "/repos/owner/repo/issues/comments/20", path
            return success(contributor_comment)

        def run(calls: list[dict[str, Any]]) -> None:
            options = {mode: 20 if mode == "edit_comment" else True}
            with patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_OWN_USER": "1"}):
                result = github_comment.comment(
                    "issue", 42, "replacement", repo="owner/repo", gh_cmd="fake-gh", **options
                )
            assert result["actor"] == result["expected_actor"] == "contributor", result
            assert result["comment"]["id"] == 20, result
            assert not any(call["method"] == "POST" for call in calls), calls
            assert sum(call["method"] == "PATCH" for call in calls) == (mode != "dedupe_body"), calls

        with_call_stub(callback, run)


def test_resolved_own_user_rejects_foreign_exact_comment_before_write() -> None:
    def callback(_method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            result = success({"login": "contributor"})
            result.actor = "contributor"
            result.expected_actor = None
            return result
        result = comment_body(20)
        result["issue_url"] = "https://api.github.com/repos/owner/repo/issues/42"
        return success(result)

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            github_comment.comment("issue", 42, "replacement", repo="owner/repo", edit_comment=20, gh_cmd="fake-gh")
        except github_comment.CommentError as exc:
            assert exc.failure.cause == "actor_mismatch", exc.failure
            assert exc.failure.failed_step == "validate_exact_comment", exc.failure
        else:
            raise AssertionError("foreign comment must fail before PATCH")
        assert all(call["method"] == "GET" for call in calls), calls

    with_call_stub(callback, run)


def test_resolved_own_user_failure_keeps_fingerprint_and_reconciliation_actor() -> None:
    def callback(method: str, path: str, _body: Any, **kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            result = success({"login": "contributor"})
            result.actor = "contributor"
            result.expected_actor = None
            return result
        if method == "GET":
            return success([])
        assert kwargs["actor"] == kwargs["expected_actor"] == "contributor", kwargs
        return failure(503, "Unicorn!", is_write=True)

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            github_comment.comment("issue", 42, "replacement", repo="owner/repo", gh_cmd="fake-gh")
        except github_comment.CommentError as exc:
            assert exc.payload["reconciliation"]["actor"] == "contributor", exc.payload
            assert exc.payload["operation_marker"]["value"] == github_comment._comment_fingerprint(
                "owner/repo", 42, "contributor", "replacement"
            ), exc.payload
        else:
            raise AssertionError("failed write must preserve actor evidence")
        assert sum(call["method"] == "POST" for call in calls) == 1, calls

    with_call_stub(callback, run)


def test_own_user_unknown_write_reconciles_without_duplicate() -> None:
    submitted_body = ""
    get_calls = 0

    def callback(method: str, path: str, body: Any, **kwargs: Any) -> github_api.ApiResult:
        nonlocal submitted_body, get_calls
        if path == "/user":
            assert kwargs["gh_prefix_args"] == ["--write-actor-for", "owner/repo"], kwargs
            result = success({"login": "contributor"})
            result.actor = "contributor"
            result.expected_actor = None
            return result
        if method == "POST":
            assert kwargs["actor"] == kwargs["expected_actor"] == "contributor", kwargs
            submitted_body = body["body"]
            result = failure(503, "Unicorn!", is_write=True)
            result.actor = "contributor"
            result.expected_actor = None
            return result
        get_calls += 1
        if get_calls == 1:
            return success([])
        assert kwargs["actor"] == kwargs["expected_actor"] == "contributor", kwargs
        result = success([comment_body(
            12, "contributor", body=submitted_body, created_at="2026-07-17T18:45:01Z"
        )])
        result.actor = "contributor"
        result.expected_actor = None
        return result

    def run(calls: list[dict[str, Any]]) -> None:
        with patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_OWN_USER": "1"}), patch.object(
            github_comment, "_utc_now", return_value=github_comment.dt.datetime(
                2026, 7, 17, 18, 45, tzinfo=github_comment.dt.timezone.utc
            )
        ):
            payload = github_comment.comment("pr", 42, "body", repo="owner/repo", gh_cmd="fake-gh")
        assert payload["actor"] == payload["expected_actor"] == "contributor", payload
        assert payload["outcome_certainty"] == "reconciled_applied", payload
        assert payload["reconciliation"]["actor"] == "contributor", payload
        assert sum(call["method"] == "POST" for call in calls) == 1, calls

    with_call_stub(callback, run, allow_retry=True)


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
            return success({"login": "fixture-automation"})
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
            return success({"login": "fixture-automation"})
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
            return success({"login": "fixture-automation"})
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
    submitted_body = ""

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        nonlocal get_calls, post_calls, submitted_body
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "POST":
            post_calls += 1
            submitted_body = str(body["body"])
            return failure(503, "Unicorn!", is_write=True)
        get_calls += 1
        if get_calls == 1:
            return success([])
        return success([
            comment_body(
                1011,
                created_at="2026-07-17T18:29:58Z",
                body=submitted_body,
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
    submitted_body = ""

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        nonlocal get_calls, post_calls, submitted_body
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "POST":
            post_calls += 1
            submitted_body = str(body["body"])
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
                body=submitted_body,
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


def test_create_reconciliation_rejects_concurrent_identical_comment() -> None:
    original_now = github_comment._utc_now
    github_comment._utc_now = lambda: github_comment.dt.datetime(
        2026,
        7,
        17,
        3,
        20,
        tzinfo=github_comment.dt.timezone.utc,
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
        return success([
            comment_body(
                1014,
                body=github_api.body_with_operation_marker("same body", "b" * 32),
            )
        ])

    def run(_calls: list[dict[str, Any]]) -> None:
        try:
            try:
                github_comment.comment(
                    "issue",
                    42,
                    "same body",
                    repo="owner/repo",
                    gh_cmd="fake-gh",
                )
            except github_comment.CommentError as exc:
                assert exc.payload["reconciliation"]["result"] == "no_match", exc.payload
            else:
                raise AssertionError("a concurrent invocation's marker must not satisfy reconciliation")
        finally:
            github_comment._utc_now = original_now

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
            return success({"login": "fixture-automation"})
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


def exact_comment(comment_id: int, **kwargs: Any) -> dict[str, Any]:
    return {**comment_body(comment_id, **kwargs), "issue_url": "https://api.github.com/repos/owner/repo/issues/42"}


def test_exact_edits_keep_interleaved_session_comments_separate() -> None:
    comments = {11: exact_comment(11, body="session A"), 12: exact_comment(12, body="session B")}
    markdown = "## Session A\n\n`literal` ${NOT_EXPANDED}\n"

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        comment_id = int(path.rsplit("/", 1)[1])
        if method == "GET":
            return success(comments[comment_id].copy())
        assert method == "PATCH"
        comments[comment_id]["body"] = body["body"]
        return success(comments[comment_id].copy())

    def run(calls: list[dict[str, Any]]) -> None:
        for kind, comment_id, prior, replacement in (("issue", 11, "session A", markdown), ("pr", 12, "session B", "B updated\n")):
            # Another session posts after this session saved its target.
            comments[13] = exact_comment(13, body="unrelated later post")
            result = github_comment.comment(kind, 42, replacement, repo="owner/repo", edit_comment=comment_id, expected_body=prior, gh_cmd="fake-gh")
            assert result["selected_comment_id"] == comment_id, result
            assert result["comment"]["id"] == comment_id, result
        assert comments[11]["body"] == markdown, comments
        assert comments[12]["body"] == "B updated\n", comments
        assert comments[13]["body"] == "unrelated later post", comments
        assert not any("?" in call["path"] or call["method"] == "POST" for call in calls), calls

    with_call_stub(callback, run)


def test_exact_edit_refuses_wrong_thread_author_and_stale_expectations() -> None:
    cases = [
        ({"issue_url": "https://api.github.com/repos/owner/repo/issues/43"}, {}, "comment_target_mismatch"),
        ({"issue_url": "https://api.github.com/repos/other/repo/issues/42"}, {}, "comment_target_mismatch"),
        ({"id": 99}, {}, "comment_target_mismatch"),
        ({"user": {"login": "someone-else"}}, {}, "actor_mismatch"),
        ({"user": None}, {}, "actor_mismatch"),
        ({"body": "changed"}, {"expected_body": "body"}, "comment_conflict"),
        ({"body": "body\n"}, {"expected_body": "body"}, "comment_conflict"),
        ({"updated_at": "2026-07-16T13:00:00Z"}, {"expected_updated_at": "2026-07-16T12:00:00Z"}, "comment_conflict"),
    ]
    for changes, expectations, cause in cases:
        def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
            assert method == "GET", (method, path)
            if path == "/user":
                return success({"login": "fixture-automation"})
            return success({**exact_comment(11), **changes})

        def run(calls: list[dict[str, Any]]) -> None:
            try:
                github_comment.comment("issue", 42, "replacement", repo="owner/repo", edit_comment=11, gh_cmd="fake-gh", **expectations)
            except github_comment.CommentError as exc:
                assert exc.failure.cause == cause, exc.failure
                assert exc.failure.write_outcome == "not_started", exc.failure
                assert exc.payload["selected_comment_id"] == 11, exc.payload
                assert "body" not in exc.payload, exc.payload
            else:
                raise AssertionError("expected exact edit refusal")
            assert len(calls) == 2, calls

        with_call_stub(callback, run)


def test_exact_edit_accepts_matching_version_and_empty_prior_body() -> None:
    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "GET":
            return success(exact_comment(11, body=""))
        assert body == {"body": "replacement"}, body
        return success(exact_comment(11, body="replacement"))

    def run(_calls: list[dict[str, Any]]) -> None:
        result = github_comment.comment("pr", 42, "replacement", repo="OWNER/REPO", edit_comment=11, expected_body="", expected_updated_at="2026-07-16T12:00:00Z", gh_cmd="fake-gh")
        assert result["comment_action"] == "updated", result

    with_call_stub(callback, run)


def test_exact_edit_never_replays_patch_or_creates_on_failure() -> None:
    for status in (404, 429, 503):
        def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
            if path == "/user":
                return success({"login": "fixture-automation"})
            if method == "GET":
                return success(exact_comment(11))
            assert method == "PATCH", method
            return failure(status, {"message": "Not Found" if status == 404 else "API rate limit exceeded" if status == 429 else "Unicorn!"}, is_write=True)

        def run(calls: list[dict[str, Any]]) -> None:
            try:
                github_comment.comment("issue", 42, "replacement", repo="owner/repo", edit_comment=11, expected_body="body", gh_cmd="fake-gh")
            except github_comment.CommentError as exc:
                assert exc.payload["selected_comment_id"] == 11, exc.payload
            else:
                raise AssertionError("expected patch failure")
            assert [call["method"] for call in calls] == ["GET", "GET", "PATCH"], calls

        with_call_stub(callback, run, allow_retry=True)


def test_exact_edit_missing_target_never_selects_another_comment() -> None:
    def callback(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        assert method == "GET", method
        if path == "/user":
            return success({"login": "fixture-automation"})
        return failure(404, {"message": "Not Found"}, is_write=False)

    def run(calls: list[dict[str, Any]]) -> None:
        try:
            github_comment.comment("pr", 42, "replacement", repo="owner/repo", edit_comment=11, gh_cmd="fake-gh")
        except github_comment.CommentError as exc:
            assert exc.failure.write_outcome == "not_started", exc.failure
            assert exc.payload["selected_comment_id"] == 11, exc.payload
        else:
            raise AssertionError("expected missing target refusal")
        assert len(calls) == 2, calls

    with_call_stub(callback, run)


def test_exact_edit_invalid_combinations_fail_before_network() -> None:
    cases = [{"edit_comment": 0}, {"edit_comment": True}, {"edit_comment": "11"}, {"edit_comment": 11, "edit_last": True}, {"edit_comment": 11, "create_if_none": True}, {"edit_comment": 11, "dedupe_body": True}, {"expected_body": "old"}, {"expected_updated_at": "old"}]
    def callback(*_args: Any, **_kwargs: Any) -> github_api.ApiResult:
        raise AssertionError("invalid arguments must not use the network")

    def run(_calls: list[dict[str, Any]]) -> None:
        for kwargs in cases:
            try:
                github_comment.comment("issue", 42, "replacement", repo="owner/repo", gh_cmd="fake-gh", **kwargs)
            except github_comment.CommentError as exc:
                assert exc.failure.cause == "validation_error", exc.failure
            else:
                raise AssertionError("expected input refusal")

    with_call_stub(callback, run)


def test_exact_comment_cli_preserves_files_and_conflict_envelope() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        prior = root / "prior.md"
        prior.write_bytes(b"prior\r\n")
        replacement = root / "replacement.md"
        replacement.write_text("## Result\n\n`literal` ${NO_EXPANSION}\n", encoding="utf-8")
        saved = root / "saved.md"
        fake_gh = root / "fake-gh"
        fake_gh.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "path = next(x for x in args if x.startswith('/'))\n"
            "method = args[args.index('--method') + 1]\n"
            "if path == '/user': print(json.dumps({'login': 'fixture-automation'}))\n"
            "else:\n"
            " assert path == '/repos/owner/repo/issues/comments/11', args\n"
            " body = 'prior\\r\\n'\n"
            " if method == 'PATCH':\n"
            "  body = json.load(sys.stdin)['body']\n"
            "  pathlib.Path(os.environ['COMMENT_TEST_SAVED']).write_text(body, encoding='utf-8')\n"
            " print(json.dumps({'id': 11, 'issue_url': 'https://api.github.com/repos/owner/repo/issues/42', 'html_url': 'https://github.com/owner/repo/issues/42#issuecomment-11', 'user': {'login': 'fixture-automation'}, 'body': body, 'updated_at': '2026-07-16T12:00:00Z'}))\n",
            encoding="utf-8",
        )
        fake_gh.chmod(0o755)
        env = {**os.environ, "GH_COMMENT_GH": str(fake_gh), "GH_PR_GH": str(fake_gh), "COMMENT_TEST_SAVED": str(saved), "GITHUB_RETRY_STATE_DIR": str(root / "retry-state")}
        scripts = pathlib.Path(__file__).parent
        for script, prefix in (("github_comment.py", ["issue", "42", "--repo", "owner/repo"]), ("gh-pr.py", ["--repo", "owner/repo", "comment", "42"])):
            argv = [sys.executable, str(scripts / script), *prefix, "--edit-comment", "11", "--body-file", str(replacement), "--expected-body-file", str(prior), "--expected-updated-at", "2026-07-16T12:00:00Z"]
            proc = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
            assert proc.returncode == 0, (proc.stdout, proc.stderr)
            result = json.loads(proc.stdout)
            assert result["selected_comment_id"] == 11, result
            assert saved.read_text(encoding="utf-8") == replacement.read_text(encoding="utf-8")
            saved.unlink()
            prior.write_text("stale\n", encoding="utf-8")
            proc = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
            assert proc.returncode != 0, proc.stdout
            result = json.loads(proc.stdout)
            assert result["write_outcome"] == "not_started", result
            assert result["selected_comment_id"] == 11, result
            assert not saved.exists()
            prior.write_bytes(b"prior\r\n")


def test_exact_edit_supports_enterprise_api_thread_urls() -> None:
    original_host = github_api.DEFAULT_HOST
    github_api.DEFAULT_HOST = "github.example.test"
    try:
        def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
            if path == "/user":
                return success({"login": "fixture-automation"})
            result = exact_comment(11)
            result["issue_url"] = "https://github.example.test/api/v3/repos/owner/repo/issues/42"
            return success(result)

        def run(_calls: list[dict[str, Any]]) -> None:
            result = github_comment.comment("issue", 42, "replacement", repo="owner/repo", edit_comment=11, gh_cmd="fake-gh")
            assert result["selected_comment_id"] == 11, result

        with_call_stub(callback, run)
    finally:
        github_api.DEFAULT_HOST = original_host


def test_append_filters_old_pages_but_dedupe_and_edit_last_keep_full_history() -> None:
    old = [comment_body(1000 + index, created_at="2026-07-15T12:00:00Z", body=f"old-{index}")
           for index in range(github_comment.PER_PAGE * 3 + 10)]
    for options, expected_action in [({}, "created"), ({"dedupe_body": True}, "existing"),
                                      ({"edit_last": True}, "updated")]:
        def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
            if path == "/user":
                return success({"login": "fixture-automation"})
            if method == "GET":
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
                page = int(query["page"][0])
                if "since" in query:
                    floor = dt.datetime(2026, 7, 16, 12, tzinfo=dt.timezone.utc) - dt.timedelta(
                        seconds=github_comment.RECONCILIATION_CLOCK_SKEW_SECONDS + 1)
                    assert query["since"] == [github_comment._format_timestamp(floor)], query
                    return success([])
                offset = (page - 1) * github_comment.PER_PAGE
                batch = old[offset:offset + github_comment.PER_PAGE]
                headers = {"link": '<https://api.github.com/next>; rel="next"'} if offset + len(batch) < len(old) else {}
                return success(batch, headers=headers)
            return success(comment_body(2000, body=body["body"]))

        def run(calls: list[dict[str, Any]]) -> None:
            with patch("github_comment._utc_now", return_value=dt.datetime(2026, 7, 16, 12, tzinfo=dt.timezone.utc)):
                result = github_comment.comment("issue", 42, "old-0", repo="owner/repo", gh_cmd="fake-gh", **options)
            assert result["comment_action"] == expected_action, result
            pages = [call for call in calls if call["method"] == "GET" and call["path"] != "/user"]
            assert len(pages) == (math.ceil(len(old) / github_comment.PER_PAGE) if options else 1), pages

        with_call_stub(callback, run)


def test_recent_reconciliation_includes_clock_skew_boundary_and_paginates() -> None:
    clock = dt.datetime(2026, 7, 16, 12, tzinfo=dt.timezone.utc)
    boundary = clock - dt.timedelta(seconds=github_comment.RECONCILIATION_CLOCK_SKEW_SECONDS)
    posted = ""
    pages = []

    def callback(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        nonlocal posted
        if path == "/user":
            return success({"login": "fixture-automation"})
        if method == "POST":
            posted = body["body"]
            return failure(503, "Unicorn!", is_write=True)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        since = github_comment._parse_timestamp(query["since"][0])
        # GitHub's updated-since comparison is exclusive. The created_at
        # boundary remains inclusive in the reconciliation contract.
        assert since < boundary, query
        page = int(query["page"][0])
        if not posted:
            return success([])
        pages.append(page)
        if page == 1:
            return success([comment_body(55, "foreign", body=posted)],
                           headers={"link": '<https://api.github.com/next>; rel="next"'})
        return success([comment_body(56, created_at=github_comment._format_timestamp(boundary), body=posted)])

    def run(calls: list[dict[str, Any]]) -> None:
        with patch("github_comment._utc_now", return_value=clock):
            result = github_comment.comment("issue", 42, "new", repo="owner/repo", gh_cmd="fake-gh")
        assert result["comment"]["id"] == 56, result
        assert pages == [1, 2], pages
        assert sum(call["method"] == "POST" for call in calls) == 1, calls

    with_call_stub(callback, run, allow_retry=True)


TESTS: list[Callable[[], None]] = [
    test_recent_reconciliation_includes_clock_skew_boundary_and_paginates,
    test_append_filters_old_pages_but_dedupe_and_edit_last_keep_full_history,
    test_own_user_actor_is_resolved_before_edit_selection_and_dedupe,
    test_resolved_own_user_rejects_foreign_exact_comment_before_write,
    test_resolved_own_user_failure_keeps_fingerprint_and_reconciliation_actor,
    test_exact_edit_supports_enterprise_api_thread_urls,
    test_exact_comment_cli_preserves_files_and_conflict_envelope,
    test_exact_edits_keep_interleaved_session_comments_separate,
    test_exact_edit_refuses_wrong_thread_author_and_stale_expectations,
    test_exact_edit_accepts_matching_version_and_empty_prior_body,
    test_exact_edit_never_replays_patch_or_creates_on_failure,
    test_exact_edit_missing_target_never_selects_another_comment,
    test_exact_edit_invalid_combinations_fail_before_network,
    test_create_preserves_markdown_body,
    test_dedupe_body_reuses_existing_actor_comment_without_write,
    test_edit_last_paginates_and_selects_latest_actor_comment,
    test_edit_last_without_existing_comment_fails_closed,
    test_create_if_none_creates_only_when_initial_lookup_is_empty,
    test_deletion_race_never_falls_back_to_create,
    test_actor_mismatch_blocks_mutation,
    test_own_user_write_reports_resolved_actor_and_rejects_wrong_author,
    test_own_user_opt_in_does_not_override_automation_response_context,
    test_own_user_invalid_response_reports_resolved_actor,
    test_edit_preserves_comment_author_after_authorized_route_switch,
    test_own_user_unknown_write_reconciles_without_duplicate,
    test_create_if_none_requires_edit_last,
    test_explicit_active_fallback_accepts_and_reports_actual_actor,
    test_active_fallback_reports_response_actor_after_route_switch,
    test_unknown_create_no_match_fails_closed_without_repeat,
    test_rejected_create_retries_without_reconciliation,
    test_create_unknown_outcome_returns_reconciled_comment_without_retry,
    test_create_reconciliation_uses_explicit_fallback_actor_context,
    test_create_reconciliation_rejects_concurrent_identical_comment,
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
