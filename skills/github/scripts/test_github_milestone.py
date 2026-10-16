#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused regression tests for the shared milestone lifecycle helper."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import github_api
from github_fixture_support import with_call_stub


SCRIPT = Path(__file__).with_name("github_milestone.py")
TEST_ACTOR = "automation-gh"


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("github_milestone_under_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def create_milestone(module: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("expected_actor", TEST_ACTOR)
    return module.create_milestone(*args, **kwargs)


def update_milestone(module: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("expected_actor", TEST_ACTOR)
    return module.update_milestone(*args, **kwargs)


def close_milestone(module: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("expected_actor", TEST_ACTOR)
    return module.close_milestone(*args, **kwargs)


def milestone(number: int, *, title: str = "Sprint 1", state: str = "open", due_on: str | None = None) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "state": state,
        "description": "",
        "due_on": due_on,
        "open_issues": 0,
        "closed_issues": 0,
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
        "closed_at": None,
        "html_url": f"https://github.com/owner/repo/milestone/{number}",
    }


def api_result(
    body: Any,
    *,
    headers: dict[str, str] | None = None,
    retry_summary: github_api.RetrySummary | None = None,
) -> github_api.ApiResult:
    return github_api.ApiResult(
        ok=True,
        status=200,
        body=body,
        headers=headers or {},
        actor="automation-gh",
        expected_actor="automation-gh",
        transport="rest_api",
        bucket="rest_core",
        retry_summary=retry_summary,
    )


def reconciliation_context() -> github_api.ReconciliationContext:
    return github_api.ReconciliationContext(
        deadline_at=42.0,
        retry_policy=github_api.RetryPolicy(),
        retry_runtime=github_api.RetryRuntime(),
    )


def test_list_milestones_paginates_and_normalizes_due_on() -> None:
    module = load_module()
    calls: list[str] = []
    first_page = [milestone(number, title=f"Sprint {number}") for number in range(1, 101)]

    def fake_call(_method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        calls.append(path)
        if "&page=1" in path:
            return api_result(first_page, headers={"link": '<https://api.github.com>; rel="next"'})
        return api_result([milestone(101, due_on="2026-09-01T00:00:00+00:00")])

    with patch.object(github_api, "call_gh_with_retry", fake_call):
        result = module.list_milestones("owner/repo", state="all", limit=101)

    assert len(calls) == 2, calls
    assert result["milestones"][-1]["due_on"] == "2026-09-01T00:00:00Z"


def test_list_milestones_preserves_retry_evidence() -> None:
    module = load_module()
    summary = github_api.RetrySummary(
        attempts=2,
        elapsed_wait=1.25,
        retry_eligible=True,
        last_actor="shiny-code-bot",
        last_bucket="rest_core",
        outcome_certainty="confirmed",
        reconciliation=None,
        recommended_next_action="none",
        effective_deadline=42.0,
    )

    def fake_call(_method: str, _path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        return api_result([], retry_summary=summary)

    with patch.object(github_api, "call_gh_with_retry", fake_call):
        result = module.list_milestones("owner/repo")

    assert result["attempts"] == 2
    assert result["elapsed_wait"] == 1.25
    assert result["actor"] == "shiny-code-bot"


def test_show_milestone_resolves_exact_title() -> None:
    module = load_module()
    calls: list[str] = []

    def fake_call(_method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return api_result({"login": TEST_ACTOR})
        calls.append(path)
        if path.startswith("/repos/owner/repo/milestones?"):
            return api_result([milestone(7, title="Release 7")])
        return api_result(milestone(7, title="Release 7"))

    with patch.object(github_api, "call_gh_with_retry", fake_call):
        result = module.show_milestone("owner/repo", "Release 7")

    assert result["milestone"]["number"] == 7
    assert calls == [
        "/repos/owner/repo/milestones?state=all&per_page=100&page=1",
        "/repos/owner/repo/milestones/7",
    ]


def test_show_milestone_preserves_case_insensitive_title_fallback() -> None:
    module = load_module()

    def fake_call(_method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if "/milestones?" in path:
            return api_result([milestone(8, title="Release Candidate")])
        return api_result(milestone(8, title="Release Candidate"))

    with patch.object(github_api, "call_gh_with_retry", fake_call):
        result = module.show_milestone("owner/repo", "release candidate")

    assert result["milestone"]["number"] == 8


def test_create_is_exact_title_idempotent_no_op() -> None:
    module = load_module()
    calls: list[str] = []

    def fake_call(method: str, _path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if _path == "/user":
            return api_result({"login": TEST_ACTOR})
        calls.append(method)
        return api_result([milestone(3, title="Sprint 3")])

    with patch.object(github_api, "call_gh_with_retry", fake_call):
        result = create_milestone(module, "owner/repo", "Sprint 3")

    assert result["created"] is False
    assert result["no_op"] is True
    assert len(result["request_fingerprint"]) == 64
    assert calls == ["GET"]


def test_create_rejects_conflicting_exact_title() -> None:
    module = load_module()

    def fake_call(_method: str, _path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if _path == "/user":
            return api_result({"login": TEST_ACTOR})
        return api_result([milestone(3, title="Sprint 3", due_on="2026-09-02T00:00:00Z")])

    with patch.object(github_api, "call_gh_with_retry", fake_call):
        try:
            create_milestone(module, "owner/repo", "Sprint 3", due_on="2026-09-01")
        except module.MilestoneError as exc:
            assert exc.failure.cause == "conflict"
            assert exc.payload["requested"]["due_on"] == "2026-09-01T00:00:00Z"
        else:
            raise AssertionError("expected exact-title conflict")


def test_create_rejects_case_insensitive_title_conflict() -> None:
    module = load_module()
    calls: list[str] = []

    def fake_call(_method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        calls.append(path)
        if path == "/user":
            return api_result({"login": TEST_ACTOR})
        return api_result([milestone(3, title="Sprint 3")])

    with patch.object(github_api, "call_gh_with_retry", fake_call):
        try:
            create_milestone(module, "owner/repo", "sprint 3")
        except module.MilestoneError as exc:
            assert exc.failure.cause == "conflict"
            assert "case-insensitively" in str(exc)
        else:
            raise AssertionError("expected case-insensitive title conflict")

    assert calls == ["/user", "/repos/owner/repo/milestones?state=all&per_page=100&page=1"]


def test_create_rejects_actor_mismatch_before_milestone_reads() -> None:
    module = load_module()
    calls: list[str] = []

    def fake_call(_method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        calls.append(path)
        return api_result({"login": "unexpected-user"})

    with patch.object(github_api, "call_gh_with_retry", fake_call):
        try:
            create_milestone(module, "owner/repo", "Sprint 3")
        except module.MilestoneError as exc:
            assert exc.failure.cause == "actor_mismatch"
        else:
            raise AssertionError("expected actor mismatch")

    assert calls == ["/user"]


def test_own_user_writes_retain_repository_writer() -> None:
    module = load_module()
    repo = "contributor/project"
    personal_login = "fixture-contributor"
    for action in ("create", "update", "close"):
        writes: list[str] = []

        def fake_call(method: str, path: str, _body: Any, **kwargs: Any) -> github_api.ApiResult:
            if path == "/user":
                # Model the wrapper selecting the personal writer only for a
                # repository-aware probe; ordinary /user uses automation.
                login = personal_login if repo in (kwargs.get("gh_prefix_args") or []) else TEST_ACTOR
                result = api_result({"login": login})
                result.actor = login
                result.expected_actor = None if login == personal_login else TEST_ACTOR
                return result
            if method in {"POST", "PATCH"}:
                writes.append(method)
                result = api_result(milestone(7, title="Updated" if action == "update" else "Sprint 1",
                                              state="closed" if action == "close" else "open"))
            elif "?" in path:
                result = api_result([])
            else:
                result = api_result(milestone(7))
            if method in {"POST", "PATCH"}:
                result.actor = personal_login
                result.expected_actor = None
            else:
                result.actor = result.expected_actor = kwargs["actor"]
            return result

        def run(_calls: list[dict[str, Any]]) -> None:
            with patch.dict(os.environ, {
                "CODEX_SKILLS_ENV_FILE": "/definitely/missing/milestone-test.env",
                "GITHUB_CLIENT_APP_OWNERS": "",
                "GH_WITH_ENV_TOKEN_OWN_USER": "1",
                "GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK": "",
            }):
                kwargs = {"gh_cmd": "gh-with-env-token"}
                if action == "create":
                    result = create_milestone(module, repo, "Sprint 1", **kwargs)
                elif action == "update":
                    result = update_milestone(module, repo, "7", title="Updated", **kwargs)
                else:
                    result = close_milestone(module, repo, "7", **kwargs)
            assert result["ok"] is True, result
            assert result["actor"] == result["expected_actor"] == personal_login, result
            assert result["outcome_certainty"] == "confirmed", result
            assert result["recommended_next_action"] == "none", result
            assert len(writes) == 1, writes

        with_call_stub(fake_call, run)


def test_repository_writer_probe_rejects_wrong_actor() -> None:
    module = load_module()

    def fake_call(_method: str, _path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        result = api_result({"login": "unexpected-user"})
        result.actor = "declared-contributor"
        result.expected_actor = None
        return result

    def run(calls: list[dict[str, Any]]) -> None:
        with patch.dict(os.environ, {
            "GITHUB_CLIENT_APP_OWNERS": "",
            "GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK": "",
        }):
            try:
                create_milestone(module, "contributor/project", "Sprint 1", gh_cmd="gh-with-env-token")
            except module.MilestoneError as exc:
                assert exc.failure.cause == "actor_mismatch", exc.failure
                assert exc.failure.write_outcome == "not_started", exc.failure
            else:
                raise AssertionError("expected actor mismatch")
        assert [call["path"] for call in calls] == ["/user"], calls

    with_call_stub(fake_call, run)


def test_update_no_op_skips_patch() -> None:
    module = load_module()
    calls: list[str] = []

    def fake_call(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return api_result({"login": TEST_ACTOR})
        calls.append(method)
        if "/milestones?" in path:
            return api_result([])
        return api_result(milestone(4, title="Sprint 4"))

    with patch.object(github_api, "call_gh_with_retry", fake_call):
        result = update_milestone(module, "owner/repo", "4", title="Sprint 4")

    assert result["updated"] is False
    assert result["no_op"] is True
    assert calls == ["GET", "GET"]


def test_update_verifies_success_response_and_clear_due_on() -> None:
    module = load_module()
    patch_bodies: list[Any] = []

    def fake_call(method: str, path: str, body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return api_result({"login": TEST_ACTOR})
        if "/milestones?" in path:
            return api_result([])
        if method == "PATCH":
            patch_bodies.append(body)
            return api_result(milestone(4, title="Sprint 4", due_on=None))
        return api_result(milestone(4, title="Sprint 4", due_on="2026-09-01T00:00:00Z"))

    with patch.object(github_api, "call_gh_with_retry", fake_call):
        result = update_milestone(module, "owner/repo", "4", clear_due_on=True)

    assert result["updated"] is True
    assert result["milestone"]["due_on"] is None
    assert patch_bodies == [{"due_on": None}]


def test_update_accepts_server_coerced_due_on_time() -> None:
    module = load_module()

    def fake_call(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return api_result({"login": TEST_ACTOR})
        if "/milestones?" in path:
            return api_result([])
        if method == "PATCH":
            return api_result(milestone(4, title="Sprint 4", due_on="2026-09-01T00:00:00Z"))
        return api_result(milestone(4, title="Sprint 4", due_on=None))

    with patch.object(github_api, "call_gh_with_retry", fake_call):
        result = update_milestone(
            module,
            "owner/repo",
            "4",
            due_on="2026-09-01T12:34:56Z",
        )

    assert result["updated"] is True
    assert result["milestone"]["due_on"] == "2026-09-01T00:00:00Z"


def test_update_rejects_mismatched_success_response() -> None:
    module = load_module()

    def fake_call(method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return api_result({"login": TEST_ACTOR})
        if "/milestones?" in path:
            return api_result([])
        if method == "PATCH":
            return api_result(milestone(4, title="Sprint 4"))
        return api_result(milestone(4, title="Sprint 4"))

    with patch.object(github_api, "call_gh_with_retry", fake_call):
        try:
            update_milestone(module, "owner/repo", "4", title="Renamed")
        except module.MilestoneError as exc:
            assert exc.failure.cause == "invalid_response"
            assert exc.failure.write_outcome == "unknown"
        else:
            raise AssertionError("expected mismatched write response failure")


def test_numeric_reference_preserves_title_lookup_compatibility() -> None:
    module = load_module()
    calls: list[str] = []

    def fake_call(_method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return api_result({"login": TEST_ACTOR})
        calls.append(path)
        if "/milestones?" in path:
            return api_result([milestone(9, title="4")])
        return api_result(milestone(9, title="4"))

    with patch.object(github_api, "call_gh_with_retry", fake_call):
        result = module.show_milestone("owner/repo", "4")

    assert result["milestone"]["number"] == 9
    assert calls[-1].endswith("/milestones/9")


def test_update_rejects_close_state() -> None:
    module = load_module()
    try:
        update_milestone(module, "owner/repo", "4", state="closed")
    except module.MilestoneError as exc:
        assert exc.failure.cause == "validation_error"
        assert "milestone-close" in str(exc)
    else:
        raise AssertionError("expected close-state validation error")


def test_close_refuses_open_issue_and_pull_request() -> None:
    module = load_module()
    calls: list[str] = []

    def fake_call(_method: str, path: str, _body: Any, **_kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return api_result({"login": TEST_ACTOR})
        calls.append(path)
        if "/milestones?" in path:
            return api_result([])
        if path.endswith("/milestones/5"):
            return api_result(milestone(5, title="Release 5"))
        return api_result(
            [
                {"number": 11, "title": "Open issue", "state": "open", "html_url": "https://example/11"},
                {"number": 12, "title": "Open PR", "state": "open", "html_url": "https://example/12", "pull_request": {}},
            ]
        )

    with patch.object(github_api, "call_gh_with_retry", fake_call):
        try:
            close_milestone(module, "owner/repo", "5")
        except module.MilestoneError as exc:
            assert exc.failure.cause == "conflict"
            assert [item["type"] for item in exc.payload["blocking_items"]] == ["issue", "pull_request"]
        else:
            raise AssertionError("expected guarded close refusal")

    assert all("PATCH" not in path for path in calls)


def test_close_patches_empty_milestone() -> None:
    module = load_module()
    calls: list[tuple[str, str, Any]] = []

    def fake_call(method: str, path: str, body: Any, **kwargs: Any) -> github_api.ApiResult:
        if path == "/user":
            return api_result({"login": TEST_ACTOR})
        calls.append((method, path, body))
        if "/milestones?" in path:
            return api_result([])
        if method == "GET" and path.endswith("/milestones/6"):
            return api_result(milestone(6, title="Release 6"))
        if method == "GET":
            return api_result([])
        return api_result(milestone(6, title="Release 6", state="closed"))

    with patch.object(github_api, "call_gh_with_retry", fake_call):
        result = close_milestone(module, "owner/repo", "6")

    assert result["closed"] is True
    assert calls[-1] == ("PATCH", "/repos/owner/repo/milestones/6", {"state": "closed"})


def test_reconciliation_callbacks_match_only_verified_state() -> None:
    module = load_module()
    context = reconciliation_context()
    retry_summaries: list[github_api.RetrySummary] = []
    original_list = module._list_raw
    original_show = module._show_number

    def fake_list(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [milestone(3, title="Sprint 3"), milestone(4, title="Sprint 4")]

    def fake_show(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return module.normalize_milestone(milestone(4, title="Renamed", state="closed"))

    module._list_raw = fake_list
    module._show_number = fake_show
    try:
        create_decision = module._reconcile_by_title(
            "owner/repo",
            "Sprint 4",
            {3},
            {"title": "Sprint 4", "description": "", "due_on": None, "state": "open"},
            "fingerprint",
            operation="github.plan.milestone_create",
            actor=TEST_ACTOR,
            expected_actor=TEST_ACTOR,
            gh_cmd="gh",
            retry_summaries=retry_summaries,
        )(api_result(None), context)
        update_decision = module._reconcile_update(
            "owner/repo",
            4,
            {"title": "Renamed"},
            operation="github.plan.milestone_update",
            actor=TEST_ACTOR,
            expected_actor=TEST_ACTOR,
            gh_cmd="gh",
            retry_summaries=retry_summaries,
        )(api_result(None), context)
        close_decision = module._reconcile_close(
            "owner/repo",
            4,
            operation="github.plan.milestone_close",
            actor=TEST_ACTOR,
            expected_actor=TEST_ACTOR,
            gh_cmd="gh",
            retry_summaries=retry_summaries,
        )(api_result(None), context)
    finally:
        module._list_raw = original_list
        module._show_number = original_show

    assert create_decision.outcome == "matched"
    assert create_decision.body["number"] == 4
    assert update_decision.outcome == "matched"
    assert close_decision.outcome == "matched"


TESTS = [
    test_list_milestones_paginates_and_normalizes_due_on,
    test_list_milestones_preserves_retry_evidence,
    test_show_milestone_resolves_exact_title,
    test_show_milestone_preserves_case_insensitive_title_fallback,
    test_create_is_exact_title_idempotent_no_op,
    test_create_rejects_conflicting_exact_title,
    test_create_rejects_case_insensitive_title_conflict,
    test_create_rejects_actor_mismatch_before_milestone_reads,
    test_own_user_writes_retain_repository_writer,
    test_repository_writer_probe_rejects_wrong_actor,
    test_update_no_op_skips_patch,
    test_update_verifies_success_response_and_clear_due_on,
    test_update_accepts_server_coerced_due_on_time,
    test_update_rejects_mismatched_success_response,
    test_numeric_reference_preserves_title_lookup_compatibility,
    test_update_rejects_close_state,
    test_close_refuses_open_issue_and_pull_request,
    test_close_patches_empty_milestone,
    test_reconciliation_callbacks_match_only_verified_state,
]


def main() -> None:
    for test in TESTS:
        test()
        print(f"ok {test.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
