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

import pytest


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
    updated_at: str | None = None,
    kind: str = "issue_comment",
    author_type: str = "User",
    in_reply_to_id: int | None = None,
) -> github_unanswered_comments.Comment:
    anchor = f"discussion_r{comment_id}" if kind == "review_comment" else f"issuecomment-{comment_id}"
    return github_unanswered_comments.Comment(
        repo="example/repo",
        number=42,
        kind=kind,
        comment_id=comment_id,
        author=author,
        author_type=author_type,
        created_at=created_at,
        updated_at=updated_at or created_at,
        body=body,
        url=f"https://github.com/example/repo/issues/42#{anchor}",
        node_id=f"NODE_{comment_id}",
        in_reply_to_id=in_reply_to_id,
    )


def reaction(
    *,
    actor: str = "cbusillo",
    content: str = "EYES",
    created_at: str = "2026-07-25T15:05:00Z",
) -> github_unanswered_comments.Reaction:
    return github_unanswered_comments.Reaction(actor=actor, content=content, created_at=created_at)


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


def classify(
    comments: list[github_unanswered_comments.Comment],
    candidates: list[github_unanswered_comments.Comment],
    reactions: dict[tuple[str, int], list[github_unanswered_comments.Reaction] | None] | None = None,
) -> list[dict[str, object]]:
    reaction_map = reactions if reactions is not None else {candidate.key: [] for candidate in candidates}
    return github_unanswered_comments.comment_states_for_thread(
        thread(),
        comments,
        {candidate.key for candidate in candidates},
        {"cbusillo"},
        {"fixture-automation"},
        reaction_map,
    )


def test_generic_bot_closeout_neither_addresses_nor_acknowledges() -> None:
    external = comment(
        comment_id=1,
        author="Jimmbo",
        body="I can provide test files if that would help.",
        created_at="2026-07-25T14:59:27Z",
    )
    closeout = comment(
        comment_id=2,
        author="fixture-automation",
        body="Completed through PR #43.",
        created_at="2026-07-26T04:07:20Z",
    )

    [result] = classify([external, closeout], [external])

    assert result["attention_state"] == "needs_your_eyes"
    assert result["owner_seen"] is False
    assert result["publicly_addressed"] is False


def test_unrelated_owner_comment_does_not_clear_external_comment() -> None:
    external = comment(
        comment_id=1,
        author="outside-user",
        body="Could you clarify this behavior?",
        created_at="2026-07-25T14:59:27Z",
    )
    unrelated = comment(
        comment_id=2,
        author="cbusillo",
        body="Closing this after the release.",
        created_at="2026-07-25T15:05:00Z",
    )

    [result] = classify([external, unrelated], [external])

    assert result["attention_state"] == "needs_your_eyes"


def test_owner_permalink_reply_marks_comment_handled() -> None:
    external = comment(
        comment_id=1,
        author="outside-user",
        body="Could you clarify this behavior?",
        created_at="2026-07-25T14:59:27Z",
    )
    response = comment(
        comment_id=2,
        author="cbusillo",
        body=f"Thanks for asking. Answered here: {external.url}",
        created_at="2026-07-25T15:05:00Z",
    )

    [result] = classify([external, response], [external])

    assert result["attention_state"] == "handled"
    assert result["owner_seen"] is True
    assert result["public_response_actor"] == "owner"


def test_owner_mention_without_exact_reference_does_not_clear_comment() -> None:
    external = comment(
        comment_id=1,
        author="outside-user",
        body="Could you clarify this behavior?",
        created_at="2026-07-25T14:59:27Z",
    )
    response = comment(
        comment_id=2,
        author="cbusillo",
        body="@outside-user yes, that is the intended behavior.",
        created_at="2026-07-25T15:05:00Z",
    )

    [result] = classify([external, response], [external])

    assert result["attention_state"] == "needs_your_eyes"


def test_exact_permalink_does_not_match_longer_comment_id() -> None:
    first = comment(
        comment_id=1,
        author="first-user",
        body="First question.",
        created_at="2026-07-25T14:50:00Z",
    )
    tenth = comment(
        comment_id=10,
        author="tenth-user",
        body="Tenth question.",
        created_at="2026-07-25T14:55:00Z",
    )
    response = comment(
        comment_id=11,
        author="cbusillo",
        body=f"Answered here: {tenth.url}",
        created_at="2026-07-25T15:05:00Z",
    )

    results = classify([first, tenth, response], [first, tenth])

    assert [result["attention_state"] for result in results] == ["needs_your_eyes", "handled"]


def test_ambiguous_owner_mention_does_not_clear_multiple_comments() -> None:
    first = comment(
        comment_id=1,
        author="outside-user",
        body="First question.",
        created_at="2026-07-25T14:50:00Z",
    )
    second = comment(
        comment_id=2,
        author="outside-user",
        body="Second question.",
        created_at="2026-07-25T14:55:00Z",
    )
    response = comment(
        comment_id=3,
        author="cbusillo",
        body="@outside-user thanks for the feedback.",
        created_at="2026-07-25T15:05:00Z",
    )

    results = classify([first, second, response], [first, second])

    assert {result["attention_state"] for result in results} == {"needs_your_eyes"}


def test_bot_reply_addresses_but_does_not_prove_owner_saw_comment() -> None:
    external = comment(
        comment_id=1,
        author="outside-user",
        body="Could you clarify the expected behavior?",
        created_at="2026-07-25T14:59:27Z",
    )
    response = comment(
        comment_id=2,
        author="fixture-automation",
        body="@outside-user yes, the next release includes that behavior.",
        created_at="2026-07-25T15:05:00Z",
    )

    [result] = classify([external, response], [external])

    assert result["attention_state"] == "bot_answered_needs_your_eyes"
    assert result["owner_seen"] is False
    assert result["publicly_addressed"] is True
    assert result["public_response_actor"] == "bot"


def test_bot_inline_reply_still_needs_owner_acknowledgement() -> None:
    external = comment(
        comment_id=10,
        author="outside-user",
        body="Should this branch be covered?",
        created_at="2026-07-25T14:59:27Z",
        kind="review_comment",
    )
    response = comment(
        comment_id=11,
        author="fixture-automation",
        body="Yes, added coverage.",
        created_at="2026-07-25T15:05:00Z",
        kind="review_comment",
        in_reply_to_id=10,
    )

    [result] = github_unanswered_comments.comment_states_for_thread(
        {**thread(), "pull_request": {}},
        [external, response],
        {external.key},
        {"cbusillo"},
        {"fixture-automation"},
        {external.key: []},
    )

    assert result["attention_state"] == "bot_answered_needs_your_eyes"


def test_inline_reply_does_not_clear_multiple_external_comments_in_same_thread() -> None:
    first = comment(
        comment_id=10,
        author="outside-user",
        body="First inline question.",
        created_at="2026-07-25T14:50:00Z",
        kind="review_comment",
    )
    second = comment(
        comment_id=11,
        author="outside-user",
        body="Second inline question.",
        created_at="2026-07-25T14:55:00Z",
        kind="review_comment",
        in_reply_to_id=10,
    )
    response = comment(
        comment_id=12,
        author="cbusillo",
        body="Thanks, updated.",
        created_at="2026-07-25T15:05:00Z",
        kind="review_comment",
        in_reply_to_id=10,
    )

    results = github_unanswered_comments.comment_states_for_thread(
        {**thread(), "pull_request": {}},
        [first, second, response],
        {first.key, second.key},
        {"cbusillo"},
        {"fixture-automation"},
        {first.key: [], second.key: []},
    )

    assert {result["attention_state"] for result in results} == {"needs_your_eyes"}


@pytest.mark.parametrize("content", ["EYES", "THUMBS_UP", "HEART", "CONFUSED"])
def test_any_owner_reaction_counts_as_seen(content: str) -> None:
    external = comment(
        comment_id=1,
        author="outside-user",
        body="A useful suggestion.",
        created_at="2026-07-25T14:59:27Z",
    )

    [result] = classify(
        [external],
        [external],
        {external.key: [reaction(content=content)]},
    )

    assert result["attention_state"] == "seen_unanswered"
    assert result["owner_seen"] is True
    assert result["publicly_addressed"] is False


def test_owner_reaction_plus_bot_reply_marks_comment_handled() -> None:
    external = comment(
        comment_id=1,
        author="outside-user",
        body="A useful suggestion.",
        created_at="2026-07-25T14:59:27Z",
    )
    response = comment(
        comment_id=2,
        author="fixture-automation",
        body="@outside-user thanks; this is implemented.",
        created_at="2026-07-25T15:06:00Z",
    )

    [result] = classify(
        [external, response],
        [external],
        {external.key: [reaction(content="ROCKET")]},
    )

    assert result["attention_state"] == "handled"


def test_reaction_before_latest_edit_does_not_count_as_seen() -> None:
    external = comment(
        comment_id=1,
        author="outside-user",
        body="Edited follow-up.",
        created_at="2026-07-25T14:59:27Z",
        updated_at="2026-07-25T16:00:00Z",
    )

    [result] = classify(
        [external],
        [external],
        {external.key: [reaction(created_at="2026-07-25T15:05:00Z")]},
    )

    assert result["attention_state"] == "needs_your_eyes"


def test_removed_reaction_reopens_attention() -> None:
    external = comment(
        comment_id=1,
        author="outside-user",
        body="A useful suggestion.",
        created_at="2026-07-25T14:59:27Z",
    )

    [result] = classify([external], [external], {external.key: []})

    assert result["attention_state"] == "needs_your_eyes"


def test_external_edit_after_owner_response_reopens_attention() -> None:
    external = comment(
        comment_id=1,
        author="outside-user",
        body="Updated question.",
        created_at="2026-07-25T14:59:27Z",
        updated_at="2026-07-25T16:00:00Z",
    )
    response = comment(
        comment_id=2,
        author="cbusillo",
        body=f"Answered earlier: {external.url}",
        created_at="2026-07-25T15:05:00Z",
    )

    [result] = classify([external, response], [external])

    assert result["attention_state"] == "needs_your_eyes"


def test_new_external_followup_after_response_needs_attention() -> None:
    first = comment(
        comment_id=1,
        author="outside-user",
        body="Initial question.",
        created_at="2026-07-25T14:59:27Z",
    )
    response = comment(
        comment_id=2,
        author="cbusillo",
        body=f"Yes, that is expected: {first.url}",
        created_at="2026-07-25T15:05:00Z",
    )
    followup = comment(
        comment_id=3,
        author="outside-user",
        body="One more question.",
        created_at="2026-07-25T15:10:00Z",
    )

    results = classify([first, response, followup], [first, followup])

    assert [result["attention_state"] for result in results] == ["handled", "needs_your_eyes"]


def test_missing_reaction_coverage_is_unknown_not_clear() -> None:
    external = comment(
        comment_id=1,
        author="outside-user",
        body="A useful suggestion.",
        created_at="2026-07-25T14:59:27Z",
    )

    [result] = classify([external], [external], {})

    assert result["owner_seen"] is None
    assert result["attention_state"] == "coverage_unknown"


def test_bot_login_overlap_never_counts_as_owner_awareness() -> None:
    external = comment(
        comment_id=1,
        author="outside-user",
        body="A useful suggestion.",
        created_at="2026-07-25T14:59:27Z",
    )
    response = comment(
        comment_id=2,
        author="fixture-automation",
        body="@outside-user thanks; this is implemented.",
        created_at="2026-07-25T15:06:00Z",
    )

    [result] = github_unanswered_comments.comment_states_for_thread(
        thread(),
        [external, response],
        {external.key},
        {"fixture-automation"},
        {"fixture-automation"},
        {external.key: []},
    )

    assert result["owner_seen"] is False
    assert result["attention_state"] == "bot_answered_needs_your_eyes"


def test_unknown_external_author_is_not_filtered() -> None:
    external = comment(
        comment_id=1,
        author="first-time-contributor",
        body="I found a possible edge case.",
        created_at="2026-07-25T14:59:27Z",
    )

    assert github_unanswered_comments.is_external_comment(
        external,
        {"cbusillo"},
        {"fixture-automation"},
    )


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


def test_parse_thread_reference_supports_short_and_url_forms() -> None:
    assert github_unanswered_comments.parse_thread_reference("example/repo#42") == ("example/repo", 42)
    assert github_unanswered_comments.parse_thread_reference(
        "https://github.com/example/repo/pull/42#discussion_r123"
    ) == ("example/repo", 42)


def test_api_list_rejects_malformed_payload(monkeypatch) -> None:
    monkeypatch.setattr(github_unanswered_comments, "run_json", lambda command: {"unexpected": True})

    with pytest.raises(github_unanswered_comments.rollup.RollupError, match="invalid paginated payload"):
        github_unanswered_comments.api_list("repos/example/repo/issues/comments")


def test_collect_comment_reactions_paginates(monkeypatch) -> None:
    external = comment(
        comment_id=1,
        author="outside-user",
        body="A useful suggestion.",
        created_at="2026-07-25T14:59:27Z",
    )
    payloads = iter(
        [
            {
                "data": {
                    "node": {
                        "lastEditedAt": None,
                        "reactions": {
                            "nodes": [
                                {
                                    "content": "HEART",
                                    "createdAt": "2026-07-25T15:05:00Z",
                                    "user": {"login": "cbusillo"},
                                }
                            ],
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                        }
                    }
                }
            },
            {
                "data": {
                    "node": {
                        "lastEditedAt": None,
                        "reactions": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            },
        ]
    )
    commands: list[list[str]] = []

    def fake_run_json(command: list[str]) -> object:
        commands.append(command)
        return next(payloads)

    monkeypatch.setattr(github_unanswered_comments, "run_json", fake_run_json)

    reactions, last_edited_at = github_unanswered_comments.collect_comment_reactions(external)

    assert reactions == [reaction(content="HEART")]
    assert last_edited_at == ""
    assert any("after=cursor-1" in argument for argument in commands[1])


def test_resolve_repositories_does_not_fallback_after_empty_owner_scope(monkeypatch) -> None:
    monkeypatch.setattr(github_unanswered_comments, "run_json", lambda command: [])
    settings = github_unanswered_comments.resolve_settings(
        args(repo_owner=["missing-owner"]),
        {},
        now=datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(github_unanswered_comments.rollup.RollupError, match="resolved to no visible"):
        github_unanswered_comments.resolve_repositories(settings)


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

    with pytest.raises(github_unanswered_comments.rollup.RollupError, match="coverage is incomplete"):
        github_unanswered_comments.resolve_repositories(settings)


def test_full_history_thread_includes_old_review_body(monkeypatch) -> None:
    settings = github_unanswered_comments.resolve_settings(
        args(thread=["example/repo#42"], until="2026-07-26T20:00:00Z"),
        {},
        now=datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc),
    )
    old_review = comment(
        comment_id=80,
        author="outside-reviewer",
        body="This older review still needs attention.",
        created_at="2026-05-01T12:00:00Z",
        kind="review",
    )
    monkeypatch.setattr(github_unanswered_comments, "authenticated_login", lambda: "fixture-automation")
    monkeypatch.setattr(
        github_unanswered_comments,
        "collect_thread",
        lambda repo, number: ({**thread(), "pull_request": {}}, [old_review], []),
    )
    monkeypatch.setattr(
        github_unanswered_comments,
        "collect_candidate_reactions",
        lambda comments: ({old_review.global_key: []}, {}, []),
    )

    payload = github_unanswered_comments.collect_payload(settings)

    assert payload["coverage"]["full_history_thread_count"] == 1
    assert payload["counts"]["needs_your_eyes"] == 1
    assert payload["attention"][0]["comment_kind"] == "review"


def test_graphql_last_edit_reopens_review_body_after_reaction(monkeypatch) -> None:
    settings = github_unanswered_comments.resolve_settings(
        args(thread=["example/repo#42"], until="2026-07-26T20:00:00Z"),
        {},
        now=datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc),
    )
    review = comment(
        comment_id=80,
        author="outside-reviewer",
        body="Edited review summary.",
        created_at="2026-07-25T12:00:00Z",
        kind="review",
    )
    monkeypatch.setattr(github_unanswered_comments, "authenticated_login", lambda: "fixture-automation")
    monkeypatch.setattr(
        github_unanswered_comments,
        "collect_thread",
        lambda repo, number: ({**thread(), "pull_request": {}}, [review], []),
    )
    monkeypatch.setattr(
        github_unanswered_comments,
        "collect_candidate_reactions",
        lambda comments: (
            {review.global_key: [reaction(created_at="2026-07-25T13:00:00Z")]},
            {review.global_key: "2026-07-25T14:00:00Z"},
            [],
        ),
    )

    payload = github_unanswered_comments.collect_payload(settings)

    assert payload["attention"][0]["attention_state"] == "needs_your_eyes"


def test_reaction_failure_degrades_and_keeps_comment_visible(monkeypatch) -> None:
    settings = github_unanswered_comments.resolve_settings(
        args(thread=["example/repo#42"], until="2026-07-26T20:00:00Z"),
        {},
        now=datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc),
    )
    external = comment(
        comment_id=1,
        author="outside-user",
        body="A useful suggestion.",
        created_at="2026-07-25T14:59:27Z",
    )
    monkeypatch.setattr(github_unanswered_comments, "authenticated_login", lambda: "fixture-automation")
    monkeypatch.setattr(github_unanswered_comments, "collect_thread", lambda repo, number: (thread(), [external], []))
    monkeypatch.setattr(
        github_unanswered_comments,
        "collect_candidate_reactions",
        lambda comments: ({}, {}, [{"repo": "example/repo", "lane": "reactions", "error": "denied"}]),
    )

    payload = github_unanswered_comments.collect_payload(settings)

    assert payload["status"] == "degraded"
    assert payload["attention"][0]["attention_state"] == "coverage_unknown"


def test_markdown_distinguishes_bot_answered_and_seen_unanswered() -> None:
    payload = {
        "status": "attention",
        "window": {"since": "2026-07-01T00:00:00Z", "until": "2026-07-26T20:00:00Z"},
        "coverage": {
            "full_history_thread_count": 1,
            "repository_count": 1,
            "errors": [],
        },
        "counts": {"attention": 2, "handled": 0},
        "attention": [
            {
                "attention_state": "bot_answered_needs_your_eyes",
                "repo": "example/repo",
                "number": 42,
                "title": "Example work",
                "thread_url": "https://github.com/example/repo/issues/42",
                "author": "outside-user",
                "created_at": "2026-07-25T14:59:27Z",
                "comment_url": "https://github.com/example/repo/issues/42#issuecomment-1",
                "public_response_actor": "bot",
                "public_response_url": "https://github.com/example/repo/issues/42#issuecomment-2",
                "owner_seen_evidence": None,
                "body_excerpt": "Question",
            },
            {
                "attention_state": "seen_unanswered",
                "repo": "example/repo",
                "number": 43,
                "title": "Another work item",
                "thread_url": "https://github.com/example/repo/issues/43",
                "author": "another-user",
                "created_at": "2026-07-25T15:00:00Z",
                "comment_url": "https://github.com/example/repo/issues/43#issuecomment-3",
                "public_response_actor": None,
                "public_response_url": None,
                "owner_seen_evidence": {"kind": "reaction", "content": "HEART"},
                "body_excerpt": "Suggestion",
            },
        ],
    }

    rendered = github_unanswered_comments.render_markdown(payload)

    assert "## Bot Answered — Needs Your Eyes" in rendered
    assert "## Seen — No Public Response" in rendered
    assert "Owner reaction: `HEART`" in rendered


def test_degraded_markdown_never_claims_no_attention_as_all_clear() -> None:
    payload = {
        "status": "degraded",
        "window": {"since": "2026-07-01T00:00:00Z", "until": "2026-07-26T20:00:00Z"},
        "coverage": {
            "full_history_thread_count": 1,
            "repository_count": 1,
            "errors": [{"repo": "example/repo", "lane": "reactions", "error": "denied"}],
        },
        "counts": {"attention": 0, "handled": 0},
        "attention": [],
    }

    rendered = github_unanswered_comments.render_markdown(payload)

    assert "coverage is incomplete; this is not an all-clear" in rendered
    assert "No external comments need attention" not in rendered


def test_resolve_settings_infers_owner_and_defaults_to_thirty_days(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc)
    monkeypatch.delenv("GH_WITH_ENV_TOKEN_EXPECTED_LOGIN", raising=False)
    monkeypatch.setenv("CODEX_AUTOMATION_LOGIN", "fixture-automation")

    settings = github_unanswered_comments.resolve_settings(
        args(repo_owner=["cbusillo"]),
        {},
        now=now,
    )

    assert settings["self_logins"] == ["cbusillo"]
    assert settings["bot_logins"] == ["fixture-automation"]
    assert settings["since"] == datetime(2026, 6, 26, 20, 0, tzinfo=timezone.utc)
    assert settings["until"] == now


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
