#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
#     "PyYAML==6.0.3",
# ]
# ///

from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("github_unanswered_comments.py")
MODULE_SPEC = importlib.util.spec_from_file_location("github_unanswered_comments", MODULE_PATH)
assert MODULE_SPEC is not None
github_unanswered_comments = importlib.util.module_from_spec(MODULE_SPEC)
assert MODULE_SPEC.loader is not None
sys.modules[MODULE_SPEC.name] = github_unanswered_comments
MODULE_SPEC.loader.exec_module(github_unanswered_comments)


def comment(
    *,
    comment_id: int,
    author: str,
    body: str,
    created_at: str,
    kind: str = "issue_comment",
    author_type: str = "User",
    in_reply_to_id: int | None = None,
) -> github_unanswered_comments.Comment:
    return github_unanswered_comments.Comment(
        repo="example/repo",
        number=42,
        kind=kind,
        comment_id=comment_id,
        author=author,
        author_type=author_type,
        created_at=created_at,
        updated_at=created_at,
        body=body,
        url=f"https://github.com/example/repo/issues/42#issuecomment-{comment_id}",
        in_reply_to_id=in_reply_to_id,
    )


def thread() -> dict[str, object]:
    return {
        "title": "Example work",
        "state": "closed",
        "html_url": "https://github.com/example/repo/issues/42",
    }


def args(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "config": Path(".local/github-work-rollup.yaml"),
        "repo": [],
        "repo_owner": [],
        "thread": [],
        "self_login": [],
        "bot_login": [],
        "window": None,
        "since": None,
        "until": None,
        "limit_repos": 1000,
        "format": "markdown",
        "output": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_generic_bot_closeout_does_not_clear_external_comment() -> None:
    external = comment(
        comment_id=1,
        author="Jimmbo",
        body="I can provide test files if that would help.",
        created_at="2026-07-25T14:59:27Z",
    )
    closeout = comment(
        comment_id=2,
        author="shiny-code-bot",
        body="Completed through PR #43.",
        created_at="2026-07-26T04:07:20Z",
    )

    unanswered = github_unanswered_comments.unanswered_for_thread(
        thread(),
        [external, closeout],
        {external.key},
        {"cbusillo"},
        {"shiny-code-bot"},
    )

    assert [item["comment_id"] for item in unanswered] == [1]


def test_later_human_account_comment_clears_external_comment() -> None:
    external = comment(
        comment_id=1,
        author="Jimmbo",
        body="I can provide test files if that would help.",
        created_at="2026-07-25T14:59:27Z",
    )
    response = comment(
        comment_id=2,
        author="cbusillo",
        body="Thank you for the offer.",
        created_at="2026-07-26T04:12:33Z",
    )

    unanswered = github_unanswered_comments.unanswered_for_thread(
        thread(),
        [external, response],
        {external.key},
        {"cbusillo"},
        {"shiny-code-bot"},
    )

    assert unanswered == []


def test_bot_mention_clears_external_comment() -> None:
    external = comment(
        comment_id=1,
        author="outside-user",
        body="Could you clarify the expected behavior?",
        created_at="2026-07-25T14:59:27Z",
    )
    response = comment(
        comment_id=2,
        author="shiny-code-bot",
        body="@outside-user yes, the next release includes that behavior.",
        created_at="2026-07-25T15:05:00Z",
    )

    unanswered = github_unanswered_comments.unanswered_for_thread(
        thread(),
        [external, response],
        {external.key},
        {"cbusillo"},
        {"shiny-code-bot"},
    )

    assert unanswered == []


def test_bot_inline_reply_clears_external_review_comment() -> None:
    external = comment(
        comment_id=10,
        author="outside-user",
        body="Should this branch be covered?",
        created_at="2026-07-25T14:59:27Z",
        kind="review_comment",
    )
    response = comment(
        comment_id=11,
        author="shiny-code-bot",
        body="Yes, added coverage.",
        created_at="2026-07-25T15:05:00Z",
        kind="review_comment",
        in_reply_to_id=10,
    )

    unanswered = github_unanswered_comments.unanswered_for_thread(
        {**thread(), "pull_request": {}},
        [external, response],
        {external.key},
        {"cbusillo"},
        {"shiny-code-bot"},
    )

    assert unanswered == []


def test_bot_nested_inline_reply_uses_review_thread_root() -> None:
    external = comment(
        comment_id=11,
        author="outside-user",
        body="A follow-up in the review thread.",
        created_at="2026-07-25T14:59:27Z",
        kind="review_comment",
        in_reply_to_id=10,
    )
    response = comment(
        comment_id=12,
        author="shiny-code-bot",
        body="Handled.",
        created_at="2026-07-25T15:05:00Z",
        kind="review_comment",
        in_reply_to_id=10,
    )

    unanswered = github_unanswered_comments.unanswered_for_thread(
        {**thread(), "pull_request": {}},
        [external, response],
        {external.key},
        {"cbusillo"},
        {"shiny-code-bot"},
    )

    assert unanswered == []


def test_unknown_external_author_is_not_filtered_by_association() -> None:
    external = comment(
        comment_id=1,
        author="new-contributor",
        body="I would like to help test this.",
        created_at="2026-07-25T14:59:27Z",
    )

    assert github_unanswered_comments.is_external_comment(
        external,
        {"cbusillo"},
        {"shiny-code-bot"},
    )


def test_new_external_followup_after_response_is_unanswered() -> None:
    first = comment(
        comment_id=1,
        author="outside-user",
        body="Can I help?",
        created_at="2026-07-25T14:59:27Z",
    )
    response = comment(
        comment_id=2,
        author="cbusillo",
        body="Yes, thank you.",
        created_at="2026-07-25T15:05:00Z",
    )
    followup = comment(
        comment_id=3,
        author="outside-user",
        body="What format should I use?",
        created_at="2026-07-25T15:10:00Z",
    )

    unanswered = github_unanswered_comments.unanswered_for_thread(
        thread(),
        [first, response, followup],
        {first.key, followup.key},
        {"cbusillo"},
        {"shiny-code-bot"},
    )

    assert [item["comment_id"] for item in unanswered] == [3]


def test_external_edit_after_response_is_unanswered() -> None:
    external = comment(
        comment_id=1,
        author="outside-user",
        body="Updated question after the first reply.",
        created_at="2026-07-25T14:59:27Z",
    )
    external = github_unanswered_comments.Comment(
        **{**external.__dict__, "updated_at": "2026-07-25T15:10:00Z"}
    )
    response = comment(
        comment_id=2,
        author="cbusillo",
        body="Initial response.",
        created_at="2026-07-25T15:05:00Z",
    )

    unanswered = github_unanswered_comments.unanswered_for_thread(
        thread(),
        [external, response],
        {external.key},
        {"cbusillo"},
        {"shiny-code-bot"},
    )

    assert [item["comment_id"] for item in unanswered] == [1]


def test_comment_window_honors_until_boundary() -> None:
    external = comment(
        comment_id=1,
        author="outside-user",
        body="A later comment.",
        created_at="2026-07-26T18:00:00Z",
    )

    assert not github_unanswered_comments.comment_in_window(
        external,
        datetime(2026, 7, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 26, 17, 0, tzinfo=timezone.utc),
    )


def test_comment_created_in_window_survives_later_edit() -> None:
    external = comment(
        comment_id=1,
        author="outside-user",
        body="Created before the requested cutoff.",
        created_at="2026-07-24T12:00:00Z",
    )
    external = github_unanswered_comments.Comment(
        **{**external.__dict__, "updated_at": "2026-07-25T12:00:00Z"}
    )

    assert github_unanswered_comments.comment_in_window(
        external,
        datetime(2026, 7, 24, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 24, 23, 59, tzinfo=timezone.utc),
    )


def test_parse_thread_reference_supports_short_and_url_forms() -> None:
    assert github_unanswered_comments.parse_thread_reference("example/repo#42") == ("example/repo", 42)
    assert github_unanswered_comments.parse_thread_reference(
        "https://github.com/example/repo/pull/42#discussion_r123"
    ) == ("example/repo", 42)


def test_resolve_repositories_does_not_fallback_after_empty_owner_scope(monkeypatch) -> None:
    monkeypatch.setattr(github_unanswered_comments, "run_json", lambda command: [])
    settings = github_unanswered_comments.resolve_settings(
        args(repo_owner=["missing-owner"]),
        {},
        now=datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc),
    )

    try:
        github_unanswered_comments.resolve_repositories(settings)
    except github_unanswered_comments.rollup.RollupError as exc:
        assert "resolved to no visible" in str(exc)
    else:
        raise AssertionError("empty explicit owner scope must fail degraded")


def test_resolve_repositories_marks_owner_limit_as_incomplete(monkeypatch) -> None:
    monkeypatch.setattr(
        github_unanswered_comments,
        "run_json",
        lambda command: [{"nameWithOwner": "example/repo", "isArchived": False}],
    )
    settings = github_unanswered_comments.resolve_settings(
        args(repo_owner=["example"], limit_repos=1),
        {},
        now=datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc),
    )

    try:
        github_unanswered_comments.resolve_repositories(settings)
    except github_unanswered_comments.rollup.RollupError as exc:
        assert "coverage is incomplete" in str(exc)
    else:
        raise AssertionError("owner scans that reach the repository limit must fail degraded")


def test_full_history_thread_includes_old_review_body(monkeypatch) -> None:
    settings = github_unanswered_comments.resolve_settings(
        args(
            thread=["example/repo#42"],
            until="2026-07-26T20:00:00Z",
        ),
        {},
        now=datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc),
    )
    old_review = comment(
        comment_id=80,
        author="outside-reviewer",
        body="This older review still needs a response.",
        created_at="2026-05-01T12:00:00Z",
        kind="review",
    )
    monkeypatch.setattr(github_unanswered_comments, "authenticated_login", lambda: "shiny-code-bot")
    monkeypatch.setattr(
        github_unanswered_comments,
        "collect_thread",
        lambda repo, number: ({**thread(), "pull_request": {}}, [old_review], []),
    )

    payload = github_unanswered_comments.collect_payload(settings)

    assert payload["coverage"]["full_history_thread_count"] == 1
    assert payload["unanswered_count"] == 1
    assert payload["unanswered"][0]["comment_kind"] == "review"


def test_empty_self_review_does_not_count_as_response() -> None:
    external = comment(
        comment_id=1,
        author="outside-user",
        body="Could you answer this question?",
        created_at="2026-07-25T14:59:27Z",
    )
    empty_approval = comment(
        comment_id=2,
        author="cbusillo",
        body="",
        created_at="2026-07-25T15:05:00Z",
        kind="review",
    )

    unanswered = github_unanswered_comments.unanswered_for_thread(
        {**thread(), "pull_request": {}},
        [external, empty_approval],
        {external.key},
        {"cbusillo"},
        {"shiny-code-bot"},
    )

    assert [item["comment_id"] for item in unanswered] == [1]


def test_resolve_settings_infers_owner_and_defaults_to_thirty_days() -> None:
    now = datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc)

    settings = github_unanswered_comments.resolve_settings(
        args(repo_owner=["cbusillo"]),
        {},
        now=now,
    )

    assert settings["self_logins"] == ["cbusillo"]
    assert settings["bot_logins"] == ["shiny-code-bot"]
    assert settings["since"] == datetime(2026, 6, 26, 20, 0, tzinfo=timezone.utc)
    assert settings["until"] == now
