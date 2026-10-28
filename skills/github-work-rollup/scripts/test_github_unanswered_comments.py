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
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

os.environ["CODEX_SKILLS_ENV_FILE"] = "/definitely/missing/codex-skills-test.env"


MODULE_PATH = Path(__file__).with_name("github_unanswered_comments.py")
MODULE_SPEC = importlib.util.spec_from_file_location("github_unanswered_comments", MODULE_PATH)
assert MODULE_SPEC is not None
github_unanswered_comments = importlib.util.module_from_spec(MODULE_SPEC)
assert MODULE_SPEC.loader is not None
sys.modules[MODULE_SPEC.name] = github_unanswered_comments
MODULE_SPEC.loader.exec_module(github_unanswered_comments)


@pytest.fixture(autouse=True)
def no_host_people_index(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the default people index lookup off the host's private files."""
    monkeypatch.setenv("CODE_HOME", str(tmp_path / "missing-code-home"))
    monkeypatch.chdir(tmp_path)


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
        "state": "open",
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
        "people_index": None,
        "window": None,
        "since": None,
        "until": None,
        "limit_repos": 1000,
        "format": "markdown",
        "output": None,
        "mention_approval": None,
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


@pytest.mark.parametrize("state", ["open", "closed"])
@pytest.mark.parametrize("reply_author", ["cbusillo", "fixture-automation"])
def test_radar_flags_only_comments_without_a_targeted_reply(
    monkeypatch: pytest.MonkeyPatch, state: str, reply_author: str,
) -> None:
    answered = comment(comment_id=1, author="outside-user", body="Can I send a sheet?", created_at="2026-07-25T10:00:00Z")
    unanswered = comment(comment_id=2, author="outside-user", body="Which format?", created_at="2026-07-25T10:30:00Z")
    reply = comment(comment_id=3, author=reply_author, body=f"Yes, please: {answered.url}",
                    created_at="2026-07-25T11:00:00Z")
    monkeypatch.setattr("github_unanswered_comments.authenticated_login", lambda: "fixture-automation")
    monkeypatch.setattr("github_unanswered_comments.collect_thread", lambda repo, number: (
        {**thread(), "state": state, "closed_at": "2026-07-25T12:00:00Z" if state == "closed" else None},
        [answered, unanswered, reply], [],
    ))
    monkeypatch.setattr("github_unanswered_comments.collect_candidate_reactions", lambda comments, deadline: (
        {answered.global_key: [], unanswered.global_key: []}, {}, [],
    ))
    settings = github_unanswered_comments.resolve_settings(
        args(thread=["example/repo#42"], self_login=["cbusillo"], bot_login=["fixture-automation"],
             until="2026-07-26T20:00:00Z", include_handled=True), {},
    )

    payload = github_unanswered_comments.collect_payload(settings)

    assert payload["status"] == "attention"
    assert [row["comment_url"] for row in payload["attention"]] == [unanswered.url]
    assert [row["comment_url"] for row in payload["handled"]] == [answered.url]


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


def test_later_reply_skips_planning_records_and_follows_the_latest_edit() -> None:
    external = comment(
        comment_id=1,
        author="outside-user",
        body="Can I send a corrected sheet?",
        created_at="2026-07-25T14:59:27Z",
        updated_at="2026-07-25T16:00:00Z",
    )
    before_edit = comment(comment_id=2, author="fixture-automation", body="Thanks, reading it now.",
                          created_at="2026-07-25T15:30:00Z")
    record = comment(comment_id=3, author="fixture-automation",
                     body='Structured wait review\n\n<!-- github-plan:wait {"state": "clear"} -->',
                     created_at="2026-07-25T17:00:00Z")
    decision = comment(comment_id=4, author="fixture-automation", body="Director decision: keep going.",
                       created_at="2026-07-25T17:30:00Z")

    [unanswered] = classify([external, before_edit, record, decision], [external])
    reply = comment(comment_id=5, author="fixture-automation", body="Yes, please attach it here.",
                    created_at="2026-07-25T18:00:00Z")
    [answered] = classify([external, before_edit, record, decision, reply], [external])

    assert unanswered["later_reply_url"] is None
    assert answered["later_reply_url"] == reply.url


REPLY_TIMELINES = {
    # name: (outside comment created/updated, reply author/body/created/updated, answered)
    "reply posted after": (("15:00", "15:00"), ("fixture-automation", "Yes, attach it.", "16:00", "16:00"), True),
    "owner reply posted after": (("15:00", "15:00"), ("cbusillo", "Yes, attach it.", "16:00", "16:00"), True),
    "older agent comment edited after": (("15:00", "15:00"), ("fixture-automation", "CI: green", "14:00", "16:00"), False),
    "older mention edited after": (
        ("15:00", "15:00"), ("fixture-automation", "@outside-user CI: green", "14:00", "16:00"), False),
    "reply before the outside edit": (("15:00", "17:00"), ("fixture-automation", "Yes, attach it.", "16:00", "16:00"), False),
    "reply before, edited after the outside edit": (
        ("15:00", "17:00"), ("fixture-automation", "Yes, attach it.", "16:00", "18:00"), False),
    "reply after the outside edit": (("15:00", "17:00"), ("fixture-automation", "Yes, attach it.", "18:00", "18:00"), True),
    "same second, posted second": (("15:00", "15:00"), ("fixture-automation", "Yes, attach it.", "15:00", "15:00"), True),
    "outside person's own follow-up": (("15:00", "15:00"), ("someone-else", "Me too.", "16:00", "16:00"), False),
}


@pytest.mark.parametrize("name", REPLY_TIMELINES)
def test_later_reply_counts_only_a_reply_posted_after_the_latest_version(name: str) -> None:
    (outside_created, outside_updated), (author, body, created, updated), answered = REPLY_TIMELINES[name]
    external = comment(comment_id=1, author="outside-user", body="Can I send a corrected sheet?",
                       created_at=f"2026-07-25T{outside_created}:00Z", updated_at=f"2026-07-25T{outside_updated}:00Z")
    reply = comment(comment_id=2, author=author, body=body,
                    created_at=f"2026-07-25T{created}:00Z", updated_at=f"2026-07-25T{updated}:00Z")

    [result] = classify([external, reply], [external])

    assert result["later_reply_url"] == (reply.url if answered else None)


def test_same_second_ids_of_different_kinds_do_not_order_a_reply() -> None:
    # Issue comments and inline review comments have separate ID sequences.
    at = "2026-07-25T17:00:00Z"
    inline = comment(comment_id=2500, author="outside-user", body="Wrong file?", created_at=at, kind="review_comment")
    agent = comment(comment_id=3500, author="fixture-automation", body="CI: green", created_at=at)
    until = datetime(2026, 7, 26, tzinfo=timezone.utc)
    agents = {"fixture-automation"}

    assert github_unanswered_comments.later_reply_for(inline, [inline, agent], agents, until) is None
    assert not github_unanswered_comments.response_is_after(agent, inline, until)


def test_an_edit_fetched_after_the_scan_cutoff_is_still_the_latest_version() -> None:
    external = comment(comment_id=1, author="outside-user", body="Corrected: use the new sheet.",
                       created_at="2026-07-25T16:00:00Z", updated_at="2026-07-25T18:00:01Z")
    reply = comment(comment_id=2, author="fixture-automation", body="Thanks.", created_at="2026-07-25T17:00:00Z")
    until = datetime(2026, 7, 25, 18, tzinfo=timezone.utc)

    assert github_unanswered_comments.later_reply_for(external, [external, reply], {"fixture-automation"}, until) is None


def test_planning_record_linking_a_comment_does_not_answer_it() -> None:
    external = comment(comment_id=1, author="outside-user", body="Can I send a corrected sheet?",
                       created_at="2026-07-25T14:59:27Z")
    record = comment(comment_id=2, author="fixture-automation",
                     body=f"Structured wait review for {external.url}\n\n<!-- github-plan:wait {{\"state\": \"clear\"}} -->",
                     created_at="2026-07-25T16:00:00Z")

    [result] = classify([external, record], [external])

    assert result["publicly_addressed"] is False
    assert result["later_reply_url"] is None


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


def test_bot_reply_handles_comment_without_owner_awareness() -> None:
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

    assert result["attention_state"] == "handled"
    assert result["owner_seen"] is False
    assert result["publicly_addressed"] is True
    assert result["public_response_actor"] == "bot"


def test_bot_inline_reply_handles_comment_without_owner_awareness() -> None:
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

    assert result["attention_state"] == "handled"


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
    assert result["attention_state"] == "handled"


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

    reactions, last_edit = github_unanswered_comments.collect_comment_reactions(external)

    assert reactions == [reaction(content="HEART")]
    assert last_edit is None
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
        lambda comments, deadline: ({old_review.global_key: []}, {}, []),
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
        lambda comments, deadline: (
            {review.global_key: [reaction(created_at="2026-07-25T13:00:00Z")]},
            {review.global_key: github_unanswered_comments.Edit("2026-07-25T14:00:00Z", "outside-reviewer")},
            [],
        ),
    )

    payload = github_unanswered_comments.collect_payload(settings)

    assert payload["attention"][0]["attention_state"] == "needs_your_eyes"


def test_no_cutoff_classifies_comments_fetched_after_the_scan_end(monkeypatch) -> None:
    def payload(**overrides: object) -> dict[str, Any]:
        settings = github_unanswered_comments.resolve_settings(
            args(thread=["example/repo#42"], **overrides), {}, now=datetime(2026, 7, 25, 18, tzinfo=timezone.utc))
        return github_unanswered_comments.collect_payload(settings)
    reply = comment(comment_id=1, author="fixture-automation", body="Send it whenever.", created_at="2026-07-25T17:00:00Z")
    posted = comment(comment_id=2, author="outside-user", body="Correction: use the new sheet.",
                     created_at="2026-07-25T18:00:01Z")
    monkeypatch.setattr(github_unanswered_comments, "authenticated_login", lambda: "fixture-automation")
    monkeypatch.setattr(github_unanswered_comments, "collect_thread", lambda repo, number: (thread(), [reply, posted], []))
    monkeypatch.setattr(github_unanswered_comments, "collect_candidate_reactions",
                        lambda comments, deadline: ({posted.global_key: []}, {}, []))

    assert payload()["attention"] == []
    [row] = payload(no_cutoff=True)["attention"]
    assert row["comment_url"] == posted.url
    assert row["later_reply_url"] is None


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
        lambda comments, deadline: ({}, {}, [{"repo": "example/repo", "lane": "reactions", "error": "denied"}]),
    )

    payload = github_unanswered_comments.collect_payload(settings)

    assert payload["status"] == "degraded"
    assert payload["attention"][0]["attention_state"] == "coverage_unknown"


def test_markdown_distinguishes_unseen_and_seen_unanswered() -> None:
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
                "attention_state": "needs_your_eyes",
                "repo": "example/repo",
                "number": 42,
                "title": "Example work",
                "thread_url": "https://github.com/example/repo/issues/42",
                "author": "outside-user",
                "created_at": "2026-07-25T14:59:27Z",
                "comment_url": "https://github.com/example/repo/issues/42#issuecomment-1",
                "public_response_actor": None,
                "public_response_url": None,
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

    sections = rendered.split("\n## ")[1:]
    for item in payload["attention"]:
        matching = [section for section in sections if item["comment_url"] in section]
        assert len(matching) == 1
        assert sum(other["comment_url"] in matching[0] for other in payload["attention"]) == 1


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


PEOPLE_YAML = """
version: 1
people:
  - id: director
    display_name: Pat Director
    preferred_reference: Pat
    contacts:
      github:
        username: cbusillo
        bot_usernames: [director-code-bot]
  - id: client
    display_name: Casey Client
    preferred_reference: Casey
    contacts:
      github:
        username: casey-client
        bot_usernames: [client-code-bot]
"""


def people_index(tmp_path: Path) -> Path:
    path = tmp_path / "people.yaml"
    path.write_text(PEOPLE_YAML, encoding="utf-8")
    return path


def opening(
    *,
    number: int = 880,
    author: str = "outside-contributor",
    body: str = "Fix quoting in the mux command.",
    created_at: str = "2026-07-20T17:08:11Z",
    pull_request: bool = True,
    author_type: str = "User",
) -> dict[str, object]:
    kind = "pull" if pull_request else "issues"
    item: dict[str, object] = {
        "id": 5_000_000 + number,
        "number": number,
        "node_id": f"PR_{number}" if pull_request else f"I_{number}",
        "title": "Fix quoting in mux_video_audio_subs",
        "state": "open",
        "user": {"login": author, "type": author_type},
        "body": body,
        "created_at": created_at,
        "updated_at": created_at,
        "html_url": f"https://github.com/example/repo/{kind}/{number}",
        "labels": [],
    }
    if pull_request:
        item["pull_request"] = {"url": f"https://api.github.com/repos/example/repo/pulls/{number}"}
    return item


def opening_comment(item: dict[str, object]) -> github_unanswered_comments.Comment:
    result = github_unanswered_comments.normalize_opening_post("example/repo", item)
    assert result is not None
    return result


EMPTY_SEARCH = {"items": [], "total_count": 0, "incomplete_results": False}


class FakeGitHub:
    """Answers the radar's gh api calls from fixtures keyed by endpoint path."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, command: list[str]) -> object:
        if command[1:3] == ["repo", "view"]:
            return {"isArchived": False}
        endpoint = command[2]
        self.calls.append(endpoint)
        if endpoint == "user":
            return {"login": "fixture-automation"}
        if endpoint == "graphql":
            return {"data": {"node": {"lastEditedAt": None, "reactions": {
                "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        path = endpoint.split("?", 1)[0]
        payload = self.routes.get(path, EMPTY_SEARCH if path == "search/issues" else [])
        if callable(payload):
            payload = payload(endpoint)
        return [payload] if "--slurp" in command else payload


def portfolio_settings(**overrides: object) -> dict[str, object]:
    return github_unanswered_comments.resolve_settings(
        args(
            repo=["example/repo"],
            self_login=["cbusillo"],
            bot_login=["fixture-automation"],
            until="2026-07-26T20:00:00Z",
            **overrides,
        ),
        {},
        now=datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc),
    )


def test_outside_pull_request_opened_in_window_needs_attention(monkeypatch) -> None:
    # Reproduces BD_to_AVP#880: an outside contributor opens a PR and nobody comments.
    pull = opening()
    fake = FakeGitHub({"repos/example/repo/issues": [pull], "repos/example/repo/issues/880": pull})
    monkeypatch.setattr(github_unanswered_comments, "run_json", fake)

    payload = github_unanswered_comments.collect_payload(portfolio_settings())

    assert payload["status"] == "attention"
    [item] = payload["attention"]
    assert (item["number"], item["comment_kind"], item["thread_kind"]) == (880, "issue_body", "pull_request")
    assert item["attention_state"] == "needs_your_eyes"
    assert item["author_class"] == "person"
    assert "opened it" in github_unanswered_comments.render_markdown(payload)


def test_owner_or_automation_opened_threads_are_not_external(monkeypatch) -> None:
    own = opening(number=1, author="cbusillo", pull_request=False)
    automation = opening(number=2, author="fixture-automation")
    app = opening(number=3, author="some-app[bot]", author_type="Bot")
    fake = FakeGitHub({"repos/example/repo/issues": [own, automation, app]})
    monkeypatch.setattr(github_unanswered_comments, "run_json", fake)

    payload = github_unanswered_comments.collect_payload(portfolio_settings())

    assert payload["status"] == "clear"
    assert payload["coverage"]["candidate_thread_count"] == 0


def test_opened_threads_stop_at_window_start_and_skip_after_until(monkeypatch) -> None:
    pages = {
        1: [opening(number=4, created_at="2026-07-27T00:00:00Z"), opening(number=3, created_at="2026-07-25T00:00:00Z")],
        2: [opening(number=2, created_at="2026-07-10T00:00:00Z"), opening(number=1, created_at="2026-06-01T00:00:00Z")],
        3: [opening(number=0, created_at="2026-07-12T00:00:00Z")],
    }
    requested: list[int] = []

    def fake_run_json(command: list[str]) -> object:
        page = int(command[2].rsplit("page=", 1)[1])
        requested.append(page)
        return pages[page]

    monkeypatch.setattr(github_unanswered_comments, "run_json", fake_run_json)

    items = github_unanswered_comments.opened_threads(
        "example/repo",
        datetime(2026, 7, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc),
        per_page=2,
    )

    assert [item["number"] for item in items] == [3, 2]
    assert requested == [1, 2]


def test_full_history_thread_includes_outside_opening_post(monkeypatch) -> None:
    pull = opening(created_at="2026-01-01T00:00:00Z")
    fake = FakeGitHub({"repos/example/repo/issues/880": pull})
    monkeypatch.setattr(github_unanswered_comments, "run_json", fake)
    settings = github_unanswered_comments.resolve_settings(
        args(thread=["example/repo#880"], until="2026-07-26T20:00:00Z"),
        {},
        now=datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc),
    )

    payload = github_unanswered_comments.collect_payload(settings)

    assert [item["comment_kind"] for item in payload["attention"]] == ["issue_body"]


def test_empty_outside_description_still_counts_as_opened() -> None:
    post = opening_comment(opening(body=""))

    assert github_unanswered_comments.is_external_comment(post, {"cbusillo"}, {"fixture-automation"})


def test_owner_reaction_and_bot_mention_handle_opening_post() -> None:
    post = opening_comment(opening())
    reply = comment(
        comment_id=2,
        author="fixture-automation",
        body="@outside-contributor thanks, this is merged.",
        created_at="2026-07-21T00:00:00Z",
    )

    [result] = github_unanswered_comments.comment_states_for_thread(
        opening(),
        [post, reply],
        {post.key},
        {"cbusillo"},
        {"fixture-automation"},
        {post.key: [reaction(created_at="2026-07-21T01:00:00Z")]},
    )

    assert result["attention_state"] == "handled"


def test_owner_link_to_another_comment_does_not_answer_opening_post() -> None:
    post = opening_comment(opening())
    other = comment(comment_id=7, author="outside-contributor", body="Ping.", created_at="2026-07-21T00:00:00Z")
    reply = comment(
        comment_id=8,
        author="cbusillo",
        body=f"See {post.url}#issuecomment-7",
        created_at="2026-07-22T00:00:00Z",
    )
    exact = comment(
        comment_id=9,
        author="cbusillo",
        body=f"Answering {post.url}#issue-{post.comment_id}",
        created_at="2026-07-23T00:00:00Z",
    )

    def state(responses: list[github_unanswered_comments.Comment]) -> object:
        results = github_unanswered_comments.comment_states_for_thread(
            opening(), [post, other, *responses], {post.key}, {"cbusillo"}, {"fixture-automation"}, {post.key: []},
        )
        return results[0]["attention_state"]

    assert state([reply]) == "needs_your_eyes"
    assert state([reply, exact]) == "handled"


def test_opening_post_does_not_make_later_bot_mention_ambiguous() -> None:
    post = opening_comment(opening(created_at="2026-07-20T00:00:00Z"))
    followup = comment(comment_id=7, author="outside-contributor", body="Any update?", created_at="2026-07-21T00:00:00Z")
    reply = comment(
        comment_id=8,
        author="fixture-automation",
        body="@outside-contributor yes, it ships Friday.",
        created_at="2026-07-22T00:00:00Z",
    )

    [result] = classify([post, followup, reply], [followup])

    assert result["attention_state"] == "handled"


def test_people_index_classifies_automation_accounts(tmp_path: Path) -> None:
    identities = github_unanswered_comments.people_identities({"cbusillo"}, people_index(tmp_path))

    assert identities["director_names"] == ["Pat Director", "Pat"]
    automation = identities["automation"]
    assert github_unanswered_comments.author_class("Client-Code-Bot", automation) == ("automation", "Casey")
    assert github_unanswered_comments.author_class("director-code-bot", automation) == ("automation", "Pat")
    assert github_unanswered_comments.author_class("unknown-code-bot", automation) == ("possible_automation", None)
    assert github_unanswered_comments.author_class("casey-client", automation) == ("person", None)


def test_people_index_automation_is_labeled_never_excluded(monkeypatch, tmp_path: Path) -> None:
    by_director_bot = opening(number=1, author="director-code-bot")
    by_client_bot = opening(number=2, author="client-code-bot")
    fake = FakeGitHub({
        "repos/example/repo/issues": [by_director_bot, by_client_bot],
        "repos/example/repo/issues/1": by_director_bot,
        "repos/example/repo/issues/2": by_client_bot,
    })
    monkeypatch.setattr(github_unanswered_comments, "run_json", fake)

    payload = github_unanswered_comments.collect_payload(portfolio_settings(people_index=people_index(tmp_path)))

    labels = {item["number"]: (item["author_class"], item["automation_for"]) for item in payload["attention"]}
    assert labels == {1: ("automation", "Pat"), 2: ("automation", "Casey")}
    assert "(automation for Casey)" in github_unanswered_comments.render_markdown(payload)


def test_invalid_people_index_degrades_coverage(monkeypatch, tmp_path: Path) -> None:
    index = tmp_path / "people.yaml"
    index.write_text("people: [{id: Not A Slug}]\n", encoding="utf-8")
    monkeypatch.setattr(github_unanswered_comments, "run_json", FakeGitHub({}))

    payload = github_unanswered_comments.collect_payload(portfolio_settings(people_index=index))

    assert payload["status"] == "degraded"
    assert payload["coverage"]["errors"][0]["lane"] == "people_index"


def waiting_issue(waiting_for: str, number: int = 141) -> dict[str, object]:
    return {
        "number": number,
        "title": "Ready for testing",
        "html_url": f"https://github.com/example/repo/issues/{number}",
        "updated_at": "2026-07-25T00:00:00Z",
        "labels": [{"name": "plan"}, {"name": "plan:waiting"}],
        "body": f"## Goal\n\nTest it.\n\n## Current Status\n\nState: Waiting.\nNext action: Pat approves.\n"
                f"Waiting for: {waiting_for}\nLast verified: 2026-07-25.\n",
    }


@pytest.mark.parametrize(
    ("waiting_for", "kind"),
    [
        ("Pat's approval of the testing plan.", "decision"),
        ("@cbusillo to confirm the cutover.", "decision"),
        ("Director decision on the plan.", "decision"),
        ("Casey to post the backup screenshot.", None),
        ("None.", None),
        ("Patience from the vendor.", None),
        ("Casey's testing, then Pat's go-live approval.", None),
        ("Pat's policy review, then Casey's release acceptance.", "decision"),
        ("Supervisor reconciliation. No Director decision is open.", None),
        ("upstream support; nothing needed from Pat for this trial.", None),
        ("Mediaforce to leave development (cbusillo/mediaforce).", None),
        ("Pat/operator to supply the Search Console access.", "decision"),
        # Hold wording stays listed, because it can also describe a real decision.
        ("Pat to resume the parked workstream.", "director_hold"),
        ("Pat selects it.", "director_hold"),
        ("Pat approval to resume deployment.", "director_hold"),
    ],
)
def test_director_wait_reason_classifies_waits_naming_the_director(waiting_for: str, kind: str | None) -> None:
    terms = ["cbusillo", "Pat Director", "Pat", "Director"]

    result = github_unanswered_comments.director_wait_reason(
        "example/repo", waiting_issue(waiting_for), terms, {"cbusillo"}
    )

    assert (result[1] if result else None) == kind


def test_question_gap_distinguishes_missing_open_and_answered_questions() -> None:
    question = comment(comment_id=10, author="fixture-automation",
                       body="Owner question: may we start testing?", created_at="2026-07-24T00:00:00Z")
    unlinked = comment(comment_id=11, author="cbusillo", body="Looks fine.", created_at="2026-07-24T01:00:00Z")
    answer = comment(comment_id=12, author="cbusillo",
                     body=f"Yes: {question.url}", created_at="2026-07-24T02:00:00Z")
    gap = github_unanswered_comments.question_gap

    assert gap([unlinked], {"cbusillo"}, {"fixture-automation"}) == "no_question"
    assert gap([question, unlinked], {"cbusillo"}, {"fixture-automation"}) is None
    assert gap([question, answer], {"cbusillo"}, {"fixture-automation"}) == "questions_answered"


def test_unasked_wait_on_director_is_reported(monkeypatch, tmp_path: Path) -> None:
    asked = waiting_issue("Pat, Q48.", number=2887)
    unasked = waiting_issue("Pat's review of the release.", number=141)
    question = {
        "id": 99, "issue_url": "https://api.github.com/repos/example/repo/issues/2887",
        "user": {"login": "fixture-automation", "type": "User"}, "body": "Owner question: Q48, delete the hooks?",
        "created_at": "2026-07-24T00:00:00Z", "html_url": "https://github.com/example/repo/issues/2887#issuecomment-99",
    }
    fake = FakeGitHub({
        "repos/example/repo/issues": lambda endpoint: [asked, unasked] if "plan%3Awaiting" in endpoint else [],
        "repos/example/repo/issues/2887/comments": [question],
    })
    monkeypatch.setattr(github_unanswered_comments, "run_json", fake)

    payload = github_unanswered_comments.collect_payload(portfolio_settings(people_index=people_index(tmp_path)))

    assert payload["status"] == "attention"
    assert [(row["number"], row["question_gap"]) for row in payload["unasked_director_waits"]] == [(141, "no_question")]
    rendered = github_unanswered_comments.render_markdown(payload)
    assert "## Waiting On You — No Question Asked" in rendered
    assert "No external comments need attention" not in rendered


def test_director_hold_is_listed_without_raising_attention(monkeypatch, tmp_path: Path) -> None:
    held = waiting_issue("Pat to resume the parked workstream.", number=31)
    fake = FakeGitHub({"repos/example/repo/issues": lambda endpoint: [held] if "plan%3Awaiting" in endpoint else []})
    monkeypatch.setattr(github_unanswered_comments, "run_json", fake)

    payload = github_unanswered_comments.collect_payload(portfolio_settings(people_index=people_index(tmp_path)))

    assert payload["status"] == "clear"
    assert [(row["number"], row["wait_kind"]) for row in payload["unasked_director_waits"]] == [(31, "director_hold")]
    assert "Worded As A Hold" in github_unanswered_comments.render_markdown(payload)


def test_named_people_index_that_is_missing_degrades_coverage(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(github_unanswered_comments, "run_json", FakeGitHub({}))

    payload = github_unanswered_comments.collect_payload(portfolio_settings(people_index=tmp_path / "typo.yaml"))

    assert payload["status"] == "degraded"
    assert payload["coverage"]["errors"][0]["lane"] == "people_index"


def test_missing_default_people_index_is_normal_but_reported(monkeypatch) -> None:
    monkeypatch.setattr(github_unanswered_comments, "run_json", FakeGitHub({}))

    payload = github_unanswered_comments.collect_payload(portfolio_settings())

    assert payload["status"] == "clear"
    assert "does not name the Director" in github_unanswered_comments.render_markdown(payload)


def test_scanning_a_client_repository_uses_no_director_names(monkeypatch, tmp_path: Path) -> None:
    # Every scanned repository owner counts as the Director, so the Client's
    # repository makes two people match; neither one's names may be used.
    unasked = waiting_issue("Casey's review of the release.", number=7)
    fake = FakeGitHub({"repos/example/repo/issues": lambda endpoint: [unasked] if "plan%3Awaiting" in endpoint else []})
    monkeypatch.setattr(github_unanswered_comments, "run_json", fake)
    settings = github_unanswered_comments.resolve_settings(
        args(repo=["example/repo", "casey-client/project"], self_login=["cbusillo"], people_index=people_index(tmp_path)),
        {},
        now=datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc),
    )

    payload = github_unanswered_comments.collect_payload(settings)

    assert payload["status"] == "clear"
    assert payload["unasked_director_waits"] == []
    assert "several people in the people index match" in github_unanswered_comments.render_markdown(payload)


def test_named_empty_people_index_is_valid(tmp_path: Path) -> None:
    index = tmp_path / "people.yaml"
    index.write_text("people: []\n", encoding="utf-8")

    identities = github_unanswered_comments.people_identities({"cbusillo"}, index)

    assert identities["status"] != "error"


@pytest.fixture
def app_ready_discussion():
    # codex-skills#825: App-ready note, a public response, Q309 and its decision.
    external = comment(comment_id=1, author="Mbanks89", body="The App is ready.", created_at="2026-07-25T10:00:00Z")
    reply = comment(comment_id=2, author="fixture-automation", body=f"Thanks, recorded: {external.url}", created_at="2026-07-25T11:00:00Z")
    question = comment(comment_id=3, author="fixture-automation", body="Director question: Q309. Use the App from the comment above?", created_at="2026-07-25T12:00:00Z")
    decision = comment(comment_id=4, author="fixture-automation", body="Director decision: Q309. Chris said yes.", created_at="2026-07-25T13:00:00Z")
    return external, reply, question, decision


def test_supervisor_above_question_records_awareness(app_ready_discussion):
    external, *discussion = app_ready_discussion
    [result] = classify([external, *discussion], [external])
    assert result["attention_state"] == "handled"
    assert result["owner_seen_evidence"]["url"] == discussion[1].url


@pytest.mark.parametrize("prefix", ["Director question:", "Director decision:"])
def test_linked_director_record_covers_only_linked_comment(prefix):
    first = comment(comment_id=1, author="outside", body="One", created_at="2026-07-25T10:00:00Z")
    second = comment(comment_id=2, author="outside", body="Two", created_at="2026-07-25T11:00:00Z")
    record = comment(comment_id=3, author="fixture-automation", body=f"{prefix} Q309 about {second.url} above", created_at="2026-07-25T12:00:00Z")
    results = classify([first, second, record], [first, second])
    assert [row["owner_seen"] for row in results] == [False, True]
    assert results[1]["attention_state"] == "seen_unanswered"
    assert results[1]["publicly_addressed"] is False


def test_above_record_stops_at_previous_record_and_does_not_answer():
    old = comment(comment_id=1, author="outside", body="Old", created_at="2026-07-25T10:00:00Z")
    previous = comment(comment_id=2, author="fixture-automation", body="Director question: Q308. Unrelated work?", created_at="2026-07-25T11:00:00Z")
    new = comment(comment_id=3, author="outside", body="New", created_at="2026-07-25T12:00:00Z")
    decision = comment(comment_id=4, author="fixture-automation", body="Director decision: Q309. Accepted the comments above.", created_at="2026-07-25T13:00:00Z")
    later = comment(comment_id=5, author="outside", body="Later", created_at="2026-07-25T14:00:00Z")
    results = classify([old, previous, new, decision, later], [old, new, later])
    assert [row["attention_state"] for row in results] == ["needs_your_eyes", "seen_unanswered", "needs_your_eyes"]


def test_external_edits_and_untrusted_records_do_not_prove_awareness(app_ready_discussion):
    external, _, question, _ = app_ready_discussion
    edited = github_unanswered_comments.replace(external, updated_at="2026-07-25T14:00:00Z")
    untrusted = github_unanswered_comments.replace(question, author="outside")
    assert classify([edited, question], [edited])[0]["owner_seen"] is False
    assert classify([external, untrusted], [external])[0]["owner_seen"] is False


def test_include_handled_lists_closed_thread_comments_apart_from_attention(monkeypatch) -> None:
    external = comment(comment_id=1, author="outside-user", body="Corrected sheet attached.",
                       created_at="2026-07-25T10:00:00Z")
    closed = {**thread(), "state": "closed", "closed_at": "2026-07-25T12:00:00Z"}
    monkeypatch.setattr(github_unanswered_comments, "authenticated_login", lambda: "fixture-automation")
    reply = comment(comment_id=2, author="example", body=f"Thanks: {external.url}",
                    created_at="2026-07-25T11:00:00Z")
    monkeypatch.setattr("github_unanswered_comments.collect_thread", lambda repo, number: (closed, [external, reply], []))
    monkeypatch.setattr(github_unanswered_comments, "collect_candidate_reactions",
                        lambda comments, deadline: ({external.global_key: []}, {}, []))

    def payload(**overrides: object) -> dict[str, Any]:
        settings = github_unanswered_comments.resolve_settings(
            args(thread=["example/repo#42"], until="2026-07-26T20:00:00Z", **overrides), {},
            now=datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc))
        return github_unanswered_comments.collect_payload(settings)

    included = payload(include_handled=True)
    assert included["attention"] == []
    assert [row["comment_url"] for row in included["handled"]] == [external.url]
    assert included["handled"][0]["handled_reason"] == "closed_with_response"
    assert "handled" not in payload()


@pytest.mark.parametrize("config,authenticated", [
    ({}, "fixture-automation"),
    ({"subjects": ["example"]}, "fixture-automation"),
    ({"comment_self_logins": ["example"]}, "fixture-automation"),
    ({}, "example"),
], ids=["repository owner", "subjects", "comment_self_logins", "authenticated login"])
def test_director_login_keeps_another_accounts_own_comment_outside(monkeypatch, config, authenticated) -> None:
    by_thread_owner = comment(comment_id=1, author="example", body="Corrected sheet attached.",
                              created_at="2026-07-25T10:00:00Z")
    monkeypatch.setattr(github_unanswered_comments, "authenticated_login", lambda: authenticated)
    monkeypatch.setattr(github_unanswered_comments, "collect_thread",
                        lambda repo, number: (thread(), [by_thread_owner], []))
    monkeypatch.setattr(github_unanswered_comments, "collect_candidate_reactions",
                        lambda comments, deadline: ({by_thread_owner.global_key: []}, {}, []))

    def attention(**overrides: object) -> list[object]:
        settings = github_unanswered_comments.resolve_settings(
            args(thread=["example/repo#42"], until="2026-07-26T20:00:00Z", **overrides), config,
            now=datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc))
        return [row["comment_url"] for row in github_unanswered_comments.collect_payload(settings)["attention"]]

    assert attention(director_login="cbusillo") == [by_thread_owner.url]
    assert attention() == []


def test_closed_bot_answer_is_handled_without_personal_awareness(app_ready_discussion):
    external, reply, *_ = app_ready_discussion
    [result] = github_unanswered_comments.comment_states_for_thread(
        {**thread(), "state": "closed"}, [external, reply], {external.key},
        {"cbusillo"}, {"fixture-automation"}, {external.key: []},
    )
    assert result["attention_state"] == "handled"
    assert result["owner_seen"] is False
    assert result["publicly_addressed"] is True


def api_comment(comment_id, author, created_at, body="Reply", **extra):
    return {"id": comment_id, "user": {"login": author, "type": "User"},
            "node_id": f"NODE_{comment_id}", "issue_url": "https://api.github.com/repos/outside/project/issues/33",
            "created_at": created_at, "updated_at": created_at, "body": body,
            "html_url": f"https://github.com/outside/project/issues/33#issuecomment-{comment_id}", **extra}


MENTION_APPROVAL = "https://github.com/cbusillo/codex-skills/issues/2073#issuecomment-99"


def search_item(repo, number, updated="2026-07-24T00:00:00Z"):
    return {"repository_url": f"https://api.github.com/repos/{repo}", "number": number,
            "html_url": f"https://github.com/{repo}/issues/{number}", "updated_at": updated,
            "user": {"login": "maintainer"}}


@pytest.fixture(autouse=True)
def director_search(monkeypatch):
    """Answer Director-login reads offline; tests set the mentions it finds."""
    state = {"items": [], "calls": []}
    def get(endpoint):
        state["calls"].append(endpoint)
        if endpoint == "user":
            return {"login": "cbusillo"}
        return {"items": state["items"], "total_count": len(state["items"]), "incomplete_results": False}
    monkeypatch.setattr(github_unanswered_comments.director_mentions, "director_get", get)
    return state


@pytest.fixture
def outbound_fixture():
    # Outside authored issue (hass-uplift-desk#33) and commented PR (MyQ#1).
    issue = opening(number=33, author="cbusillo", pull_request=False)
    pull = opening(number=1, author="maintainer")
    mentioned = opening(number=2, author="maintainer")
    for item in (issue, pull, mentioned):
        item["html_url"] = f"https://github.com/outside/project/issues/{item['number']}"
        item["repository_url"] = "https://api.github.com/repos/outside/project"
    routes = {
        "search/issues": {"items": [issue, pull, mentioned], "total_count": 3, "incomplete_results": False},
        "repos/outside/project/issues/33": issue,
        "repos/outside/project/issues/33/comments": [
            api_comment(1, "maintainer", "2026-07-21T00:00:00Z"),
            api_comment(2, "fixture-automation", "2026-07-22T00:00:00Z"),
        ],
        "repos/outside/project/issues/1": pull,
        "repos/outside/project/issues/1/comments": [
            api_comment(3, "maintainer", "2026-07-21T00:00:00Z"),
            api_comment(4, "cbusillo", "2026-07-22T00:00:00Z"),
            api_comment(5, "maintainer", "2026-07-23T00:00:00Z"),
            api_comment(6, "maintainer", "2026-07-27T00:00:00Z"),
        ],
        "repos/outside/project/pulls/1/comments": [api_comment(7, "reviewer", "2026-07-24T00:00:00Z", pull_request_url="https://api.github.com/repos/outside/project/pulls/1")],
        "repos/outside/project/pulls/1/reviews": [api_comment(8, "reviewer", "2026-07-24T01:00:00Z", submitted_at="2026-07-24T01:00:00Z")],
        "repos/outside/project/issues/2": mentioned,
        "repos/outside/project/issues/2/comments": [api_comment(9, "maintainer", "2026-07-24T00:00:00Z", "@cbusillo can you help?")],
        "repos/cbusillo/codex-skills/issues/comments/99": {
            "id": 99, "user": {"login": "cbusillo"}, "created_at": "2026-07-20T00:00:00Z",
            "issue_url": "https://api.github.com/repos/cbusillo/codex-skills/issues/2073",
            "body": "Q100 yes"},
        "repos/cbusillo/codex-skills/issues/2073/comments": [{
            "id": 98, "user": {"login": "fixture-automation"}, "created_at": "2026-07-19T00:00:00Z",
            "body": "Owner question: **Q100** — may the radar search GitHub for mentions "
                    "of you under your own login, unattended?"}],
    }
    for number in (1, 2, 33):
        for item in routes[f"repos/outside/project/issues/{number}/comments"]:
            item["issue_url"] = f"https://api.github.com/repos/outside/project/issues/{number}"
            item["html_url"] = f"https://github.com/outside/project/issues/{number}#issuecomment-{item['id']}"
    return routes


def outbound_settings(**overrides):
    return github_unanswered_comments.resolve_settings(
        args(**{"outbound": True, "self_login": ["cbusillo"], "bot_login": ["fixture-automation"],
                "until": "2026-07-26T20:00:00Z", "mention_approval": MENTION_APPROVAL, **overrides}), {},
    )


def test_outbound_lists_mentions_outside_scope_including_private_and_mention_only_threads(
    monkeypatch, outbound_fixture, director_search,
):
    # outside/project#2 only mentions the Director; client/private#4 is visible only to their login.
    director_search["items"] = [search_item("outside/project", 2), search_item("outside/project", 1),
                                search_item("client/private", 4),
                                search_item("client/private", 5, updated="2026-07-27T00:00:00Z")]
    monkeypatch.setattr(github_unanswered_comments, "run_json", FakeGitHub(outbound_fixture))
    payload = github_unanswered_comments.collect_payload(outbound_settings())
    assert payload["status"] == "attention"
    assert [(row["repository"], row["number"]) for row in payload["director_mentions"]] == [
        ("outside/project", 2), ("client/private", 4)]
    assert all(row["untrusted"] for row in payload["director_mentions"])
    rendered = github_unanswered_comments.render_markdown(payload)
    assert "https://github.com/client/private/issues/4" in rendered
    from urllib.parse import parse_qs, urlparse
    query = parse_qs(urlparse(director_search["calls"][1]).query)["q"][0]
    assert "-user:cbusillo" in query
    assert "..2026-07-26T20:00:00Z" in query  # the report's --until bounds the search


def test_outbound_withdrawn_mention_approval_reports_the_gap(monkeypatch, outbound_fixture, director_search):
    question = "Q100"
    outbound_fixture["repos/cbusillo/codex-skills/issues/2073/comments"] += [
        {"id": 100, "user": {"login": "cbusillo"}, "created_at": "2026-07-21T00:00:00Z", "body": f"{question} no"}]
    monkeypatch.setattr(github_unanswered_comments, "run_json", FakeGitHub(outbound_fixture))
    payload = github_unanswered_comments.collect_payload(outbound_settings())
    assert payload["status"] == "degraded"
    assert [error["lane"] for error in payload["coverage"]["errors"]] == ["director_mentions"]
    assert director_search["calls"] == []


def test_outbound_without_mention_approval_reports_the_gap(monkeypatch, outbound_fixture, director_search):
    director_search["items"] = [search_item("client/private", 4)]
    monkeypatch.setattr(github_unanswered_comments, "run_json", FakeGitHub(outbound_fixture))
    payload = github_unanswered_comments.collect_payload(outbound_settings(mention_approval=None))
    assert payload["status"] == "degraded"
    assert [error["lane"] for error in payload["coverage"]["errors"]] == ["director_mentions"]
    assert payload["director_mentions"] == []
    assert director_search["calls"] == []
    assert "Coverage Gaps" in github_unanswered_comments.render_markdown(payload)


def test_outbound_lists_authored_and_commented_threads_after_last_human_activity(monkeypatch, outbound_fixture):
    fake = FakeGitHub(outbound_fixture)
    monkeypatch.setattr(github_unanswered_comments, "run_json", fake)
    payload = github_unanswered_comments.collect_payload(outbound_settings())
    assert payload["status"] == "attention"
    assert {item["comment_id"] for item in payload["attention"]} == {1, 5, 7, 8}
    assert all(item["attention_state"] == "outbound_reply" for item in payload["attention"])
    rendered = github_unanswered_comments.render_markdown(payload)
    assert all(item["comment_url"] in rendered for item in payload["attention"])
    assert not any(call.startswith("repos/example") for call in fake.calls)


def test_outbound_last_human_edit_clears_earlier_replies(monkeypatch, outbound_fixture):
    outbound_fixture["repos/outside/project/issues/1/comments"][1]["updated_at"] = "2026-07-25T00:00:00Z"
    fake = FakeGitHub(outbound_fixture)
    def with_opening_edit(command):
        result = fake(command)
        if command[2] == "graphql" and ("id=I_33" in command or "id=NODE_4" in command):
            result["data"]["node"]["lastEditedAt"] = "2026-07-25T00:00:00Z"
            result["data"]["node"]["editor"] = {"login": "cbusillo"}
        return result
    monkeypatch.setattr(github_unanswered_comments, "run_json", with_opening_edit)
    payload = github_unanswered_comments.collect_payload(outbound_settings())
    assert payload["status"] == "clear"
    assert payload["attention"] == []


@pytest.mark.parametrize("search_result", [
    {"items": [], "total_count": 1, "incomplete_results": True},
    {"items": [], "total_count": 5, "incomplete_results": False},
    {"unexpected": []},
])
def test_outbound_partial_or_malformed_search_is_degraded(monkeypatch, search_result):
    monkeypatch.setattr(github_unanswered_comments, "run_json", FakeGitHub({"search/issues": search_result}))
    payload = github_unanswered_comments.collect_payload(outbound_settings())
    assert payload["status"] == "degraded"
    assert payload["coverage"]["errors"]


@pytest.mark.parametrize("total,expected_pages,degraded", [(101, 2, False), (1001, 10, True)])
def test_outbound_search_paginates_and_reports_search_cap(monkeypatch, total, expected_pages, degraded):
    from urllib.parse import parse_qs, urlparse
    pages = []
    def search(command):
        query = parse_qs(urlparse(command[2]).query)
        page = int(query["page"][0])
        pages.append(page)
        rows = [{"number": number, "repository_url": "https://api.github.com/repos/outside/project"}
                for number in range((page - 1) * 100, min(page * 100, total))]
        return {"items": rows, "total_count": total, "incomplete_results": False}
    monkeypatch.setattr(github_unanswered_comments, "run_json", search)
    found, errors = github_unanswered_comments.outbound_threads({"cbusillo"}, outbound_settings())
    assert len(pages) == expected_pages
    assert len(found) == min(total, 1000)
    assert bool(errors) is degraded


def test_outbound_read_failure_preserves_attention_and_degrades(monkeypatch, outbound_fixture):
    fake = FakeGitHub(outbound_fixture)
    def denied(command):
        if command[2] == "repos/outside/project/issues/1":
            raise github_unanswered_comments.rollup.RollupError("Not visible")
        return fake(command)
    monkeypatch.setattr(github_unanswered_comments, "run_json", denied)
    payload = github_unanswered_comments.collect_payload(outbound_settings())
    assert payload["status"] == "degraded"
    assert {item["comment_id"] for item in payload["attention"]} == {1}


@pytest.mark.parametrize("status,exit_name", [("clear", "EXIT_CLEAR"), ("attention", "EXIT_ATTENTION"), ("degraded", "EXIT_DEGRADED")])
def test_outbound_uses_existing_exit_contract(monkeypatch, capsys, status, exit_name):
    monkeypatch.setattr(github_unanswered_comments, "parse_args", lambda: args(outbound=True, format="json"))
    monkeypatch.setattr(github_unanswered_comments.rollup, "load_config", lambda path: {})
    monkeypatch.setattr(github_unanswered_comments, "collect_payload", lambda settings: {"status": status})
    assert github_unanswered_comments.main() == getattr(github_unanswered_comments, exit_name)
    assert status in capsys.readouterr().out


def test_closed_unanswered_messages_still_need_attention(monkeypatch):
    closed = opening(number=33, author="outside", pull_request=False)
    closed.update(state="closed", closed_at="2026-07-25T00:00:00Z")
    monkeypatch.setattr(github_unanswered_comments, "run_json", FakeGitHub({
        "repos/example/repo/issues": [closed], "repos/example/repo/issues/33": closed,
    }))
    payload = github_unanswered_comments.collect_payload(portfolio_settings())
    assert payload["status"] == "attention"
    assert payload["counts"]["handled_closed"] == 0
    assert len(payload["attention"]) == 1
    assert closed["title"] in github_unanswered_comments.render_markdown(payload)


@pytest.mark.parametrize("prefix", [
    *github_unanswered_comments.director_questions.QUESTION_PREFIXES,
    *github_unanswered_comments.director_questions.DECISION_PREFIXES,
])
def test_awareness_accepts_existing_record_prefixes(prefix, app_ready_discussion):
    external, *_ = app_ready_discussion
    record = comment(comment_id=2, author="fixture-automation", body=f"**{prefix}** the comment above", created_at="2026-07-25T12:00:00Z")
    [result] = classify([external, record], [external])
    assert result["owner_seen"] is True
    assert result["publicly_addressed"] is False


def test_quoted_question_inside_ordinary_comment_is_not_awareness(app_ready_discussion):
    external, *_ = app_ready_discussion
    record = comment(comment_id=2, author="fixture-automation", body="Example of a future question:\nDirector question: about the comment above", created_at="2026-07-25T12:00:00Z")
    assert classify([external, record], [external])[0]["owner_seen"] is False


def test_outbound_with_portfolio_scope_does_not_adopt_repository_owners(monkeypatch, outbound_fixture):
    # A maintainer's later update must not hide a reply to the Director.
    outbound_fixture["repos/outside/project/issues/1/comments"].append(
        api_comment(10, "outside", "2026-07-25T00:00:00Z", issue_url="https://api.github.com/repos/outside/project/issues/1"),
    )
    fake = FakeGitHub(outbound_fixture)
    monkeypatch.setattr(github_unanswered_comments, "run_json", fake)
    settings = outbound_settings()
    settings.update(repositories=["outside/other"], scan_repository_scope=True)
    payload = github_unanswered_comments.collect_payload(settings)
    assert {item["comment_id"] for item in payload["attention"]} == {1, 5, 7, 8, 10}


def test_closed_full_history_scan_uses_requested_closed_thread_rules(monkeypatch):
    closed = opening(number=42, author="outside", pull_request=False, body="I can provide test files.")
    closed.update(state="closed", closed_at="2026-07-25T00:00:00Z")
    monkeypatch.setattr(github_unanswered_comments, "run_json", FakeGitHub({"repos/example/repo/issues/42": closed}))
    settings = github_unanswered_comments.resolve_settings(args(thread=["example/repo#42"], self_login=["cbusillo"], until="2026-07-26T20:00:00Z"), {})
    payload = github_unanswered_comments.collect_payload(settings)
    assert payload["status"] == "attention"
    assert payload["counts"]["handled_closed"] == 0


@pytest.mark.parametrize("scope_repo", ["outside/project", "Outside/Project"])
def test_explicit_thread_scope_wins_over_outbound_discovery(monkeypatch, outbound_fixture, scope_repo):
    fake = FakeGitHub(outbound_fixture)
    def case_insensitive_api(command):
        command = list(command)
        command[2] = command[2].replace(scope_repo, "outside/project")
        return fake(command)
    monkeypatch.setattr(github_unanswered_comments, "run_json", case_insensitive_api)
    settings = outbound_settings()
    settings["threads"] = [(scope_repo, 1)]
    payload = github_unanswered_comments.collect_payload(settings)
    rows = [row for row in payload["attention"] if row["number"] == 1]
    assert any(row["comment_id"] == 3 for row in rows)  # Before Chris's unrelated activity.
    assert all(row["attention_state"] != "outbound_reply" for row in rows)


def test_incidental_url_does_not_disable_above_awareness(app_ready_discussion):
    external, *_ = app_ready_discussion
    question = comment(comment_id=2, author="fixture-automation", body="Director question: Should we use https://example.com/docs based on the comment above?", created_at="2026-07-25T12:00:00Z")
    assert classify([external, question], [external])[0]["owner_seen"] is True


@pytest.mark.parametrize("editor", ["maintainer", "fixture-automation", ""])
def test_others_edits_do_not_advance_outbound_human_cutoff(monkeypatch, outbound_fixture, editor):
    fake = FakeGitHub(outbound_fixture)
    def edited_by_other(command):
        result = fake(command)
        if command[2] == "graphql" and ("id=I_33" in command or "id=NODE_4" in command):
            result["data"]["node"].update(lastEditedAt="2026-07-25T00:00:00Z", editor={"login": editor})
        return result
    monkeypatch.setattr(github_unanswered_comments, "run_json", edited_by_other)
    payload = github_unanswered_comments.collect_payload(outbound_settings())
    assert {item["comment_id"] for item in payload["attention"]} == {1, 5, 7, 8}


def wait_record_comment(number: int, comment_id: int, author: str, waiting_for: str | None = None,
                        marker: str | None = None) -> dict[str, object]:
    if marker is None:
        record = {"version": 1, "state": "waiting" if waiting_for else "clear", "evidence": "Recorded on the issue.",
                  "waits": [{"waiting_for": waiting_for, "ends_when": "Pat answers"}] if waiting_for else []}
        marker = "<!-- github-plan:wait " + json.dumps(record, sort_keys=True) + " -->"
    return {
        "id": comment_id, "issue_url": f"https://api.github.com/repos/example/repo/issues/{number}",
        "user": {"login": author, "type": "User"}, "body": "Wait recorded.\n\n" + marker,
        "created_at": "2026-07-24T00:00:00Z", "html_url": f"https://github.com/example/repo/issues/{number}#issuecomment-{comment_id}",
    }


def labeled_waiting(endpoint: str) -> list[dict[str, object]]:
    return [waiting_issue("Pat's review of the release.")] if "plan%3Awaiting" in endpoint else []


def structured_wait_rows(monkeypatch, tmp_path: Path, routes: dict[str, object]) -> dict[str, object]:
    monkeypatch.setattr(github_unanswered_comments, "run_json", FakeGitHub(routes))
    return github_unanswered_comments.collect_payload(portfolio_settings(people_index=people_index(tmp_path)))


def test_comment_only_structured_hold_without_waiting_label_is_reported(monkeypatch, tmp_path: Path) -> None:
    # The repository owner is a trusted wait author; the label write never happened.
    quiet = {**waiting_issue("Nothing.", number=52), "labels": [{"name": "plan"}, {"name": "plan:active"}],
             "state": "open", "repository_url": "https://api.github.com/repos/example/repo",
             "body": "## Current Status\n\nState: Active.\nNext action: Ship it.\n"}
    payload = structured_wait_rows(monkeypatch, tmp_path, {
        "search/issues": {"items": [quiet], "total_count": 1, "incomplete_results": False},
        "repos/example/repo/issues/52/comments": [wait_record_comment(52, 5, "example", "Pat's approval of the release.")],
    })

    assert payload["status"] == "attention"
    assert [(row["number"], row["wait_kind"], row["wait_source"], row["wait_review_required"])
            for row in payload["unasked_director_waits"]] == [(52, "decision", "structured_wait", False)]


def test_later_trusted_clear_removes_stale_prose_wait(monkeypatch, tmp_path: Path) -> None:
    payload = structured_wait_rows(monkeypatch, tmp_path, {
        "repos/example/repo/issues": labeled_waiting,
        "repos/example/repo/issues/141/comments": [wait_record_comment(141, 5, "example")],
    })

    assert payload["status"] == "clear"
    assert payload["unasked_director_waits"] == []


def test_untrusted_clear_cannot_release_a_trusted_wait(monkeypatch, tmp_path: Path) -> None:
    payload = structured_wait_rows(monkeypatch, tmp_path, {
        "repos/example/repo/issues": labeled_waiting,
        "repos/example/repo/issues/141/comments": [
            wait_record_comment(141, 5, "example", "Pat's review of the release."),
            wait_record_comment(141, 6, "outside-contributor"),
        ],
    })

    assert payload["status"] == "attention"
    assert [(row["number"], row["wait_source"]) for row in payload["unasked_director_waits"]] == [(141, "structured_wait")]


def test_malformed_trusted_record_keeps_prose_wait_for_review(monkeypatch, tmp_path: Path) -> None:
    payload = structured_wait_rows(monkeypatch, tmp_path, {
        "repos/example/repo/issues": labeled_waiting,
        "repos/example/repo/issues/141/comments": [
            wait_record_comment(141, 5, "example", marker="<!-- github-plan:wait {not json -->")],
    })

    assert payload["status"] == "degraded"
    assert [error["lane"] for error in payload["coverage"]["errors"]] == ["director_waits/141"]
    assert [(row["number"], row["wait_review_required"]) for row in payload["unasked_director_waits"]] == [(141, True)]


def test_unread_wait_discussion_or_partial_wait_search_degrades_coverage(monkeypatch, tmp_path: Path) -> None:
    quiet = {**waiting_issue("Nothing.", number=52), "labels": [], "state": "open",
             "repository_url": "https://api.github.com/repos/example/repo", "body": "Plain issue."}

    def unreadable(endpoint: str) -> object:
        raise github_unanswered_comments.rollup.RollupError("comments unavailable")

    for routes, lane in (
        ({"search/issues": {"items": [quiet], "total_count": 1, "incomplete_results": False},
          "repos/example/repo/issues/52/comments": unreadable}, "director_waits/52"),
        ({"search/issues": {"items": [], "total_count": 0, "incomplete_results": True}}, "director_waits_search"),
    ):
        payload = structured_wait_rows(monkeypatch, tmp_path, routes)
        assert payload["status"] == "degraded"
        assert [(error["repo"], error["lane"]) for error in payload["coverage"]["errors"]] == [
            ("example/repo" if lane != "director_waits_search" else "example", lane)]


MALFORMED_WAIT = "<!-- github-plan:wait {not json -->"


@pytest.mark.parametrize("case", ["malformed_after_hold", "malformed_before_hold", "malformed_contributor_envelope"])
def test_unreadable_trusted_record_keeps_a_comment_hold_as_unresolved(monkeypatch, tmp_path: Path, case: str) -> None:
    # The body prose names someone else, so only the comment records a hold on Pat.
    issue = waiting_issue("Client's testing of the build.")
    hold = wait_record_comment(141, 5, "example", "Pat's approval of the release.")
    malformed = wait_record_comment(141, 6, "example", marker=MALFORMED_WAIT)
    if case == "malformed_contributor_envelope":
        issue = {**issue, "body": "<!-- github-plan:managed:start -->\n" + str(issue["body"])}
    comments = {"malformed_after_hold": [hold, malformed], "malformed_before_hold": [malformed, hold],
                "malformed_contributor_envelope": [hold]}[case]
    payload = structured_wait_rows(monkeypatch, tmp_path, {
        "repos/example/repo/issues": lambda endpoint: [issue] if "plan%3Awaiting" in endpoint else [],
        "repos/example/repo/issues/141/comments": comments,
    })

    assert payload["status"] == "degraded"
    assert [error["lane"] for error in payload["coverage"]["errors"]] == ["director_waits/141"]
    assert [(row["number"], row["wait_kind"], row["wait_source"], row["wait_review_required"])
            for row in payload["unasked_director_waits"]] == [(141, "unresolved", "unresolved_structured_wait", True)]
    rendered = github_unanswered_comments.render_markdown(payload)
    assert all(row["url"] in rendered for row in payload["unasked_director_waits"]), "the unresolved wait is shown"


REVIEW_WAIT = '<!-- github-plan:wait {"evidence": "", "state": "review", "version": 1, "waits": []} -->'


@pytest.mark.parametrize("reviewer", ["example", "outside-contributor", None])
def test_trusted_review_record_after_a_hold_is_an_unresolved_wait(monkeypatch, tmp_path: Path, reviewer: str | None) -> None:
    # No prose records a wait; only the comments do.
    quiet = {**waiting_issue("Nothing.", number=52), "labels": [], "state": "open",
             "repository_url": "https://api.github.com/repos/example/repo", "body": "Plain issue."}
    comments = [wait_record_comment(52, 5, "example", "Pat's approval of the release.")]
    if reviewer:
        comments.append(wait_record_comment(52, 6, reviewer, marker=REVIEW_WAIT))
    payload = structured_wait_rows(monkeypatch, tmp_path, {
        "search/issues": {"items": [quiet], "total_count": 1, "incomplete_results": False},
        "repos/example/repo/issues/52/comments": comments,
    })

    assert payload["coverage"]["errors"] == []
    expected = {"example": (52, "unresolved", "unresolved_structured_wait", True),
                "outside-contributor": (52, "decision", "structured_wait", False),
                None: (52, "decision", "structured_wait", False)}[reviewer]
    assert [(row["number"], row["wait_kind"], row["wait_source"], row["wait_review_required"])
            for row in payload["unasked_director_waits"]] == [expected]


def test_issue_with_no_wait_record_or_prose_is_not_a_wait(monkeypatch, tmp_path: Path) -> None:
    quiet = {**waiting_issue("Nothing.", number=52), "labels": [], "state": "open",
             "repository_url": "https://api.github.com/repos/example/repo", "body": "Plain issue."}
    payload = structured_wait_rows(monkeypatch, tmp_path, {
        "search/issues": {"items": [quiet], "total_count": 1, "incomplete_results": False},
        "repos/example/repo/issues/52/comments": [],
    })

    assert payload["coverage"]["errors"] == []
    assert payload["unasked_director_waits"] == []


def test_labeled_wait_reads_a_new_comment_hold_before_search_indexes_it(monkeypatch, tmp_path: Path) -> None:
    # Search has not indexed the new comment yet, and the prose names someone else.
    issue = waiting_issue("Client's testing of the build.")
    payload = structured_wait_rows(monkeypatch, tmp_path, {
        "repos/example/repo/issues": lambda endpoint: [issue] if "plan%3Awaiting" in endpoint else [],
        "repos/example/repo/issues/141/comments": [wait_record_comment(141, 5, "example", "Pat's approval of the release.")],
    })

    assert payload["status"] == "attention"
    assert [(row["number"], row["wait_kind"], row["wait_source"])
            for row in payload["unasked_director_waits"]] == [(141, "decision", "structured_wait")]


def outside_result(number: int) -> dict[str, object]:
    # A matching issue outside the scanned repositories still counts toward the total.
    return {"number": number, "state": "open", "repository_url": "https://api.github.com/repos/example/other"}


@pytest.mark.parametrize("pages", [
    pytest.param({1: {"items": [outside_result(n) for n in range(1, 101)], "total_count": 101, "incomplete_results": False},
                  2: {"items": [outside_result(100)], "total_count": 101, "incomplete_results": False}},
                 id="duplicate_across_pages"),
    pytest.param({1: {"items": [outside_result(n) for n in range(1, 101)], "total_count": 101, "incomplete_results": False},
                  2: {"items": [outside_result(101)], "total_count": 100, "incomplete_results": False}},
                 id="total_changed"),
    pytest.param({1: {"items": [], "total_count": 0}}, id="incomplete_results_missing"),
])
def test_structured_wait_search_pagination_gaps_degrade_coverage(monkeypatch, tmp_path: Path, pages) -> None:
    def search(endpoint: str) -> object:
        return pages[int(parse_qs(urlsplit(endpoint).query)["page"][0])]

    payload = structured_wait_rows(monkeypatch, tmp_path, {"search/issues": search})

    assert payload["status"] == "degraded"
    assert [(error["repo"], error["lane"]) for error in payload["coverage"]["errors"]] == [("example", "director_waits_search")]



def test_stuck_gh_call_times_out_as_a_coverage_error(monkeypatch) -> None:
    monkeypatch.setattr(github_unanswered_comments, "GH_CALL_TIMEOUT_SECONDS", 0.2)
    started = time.monotonic()

    with pytest.raises(github_unanswered_comments.rollup.RollupError, match="Timed out"):
        github_unanswered_comments.run_json([sys.executable, "-c", "import time; time.sleep(30)"])

    assert time.monotonic() - started < 10


FAKE_GH = """
import json, sys, time

stuck, thread = sys.argv[1], json.loads(sys.argv[2])
endpoint = sys.argv[4]
if stuck in endpoint:
    time.sleep(120)
if endpoint == "user":
    print(json.dumps({"login": "fixture-automation"}))
elif endpoint == "graphql":
    print(json.dumps({"data": {"node": {"lastEditedAt": None, "reactions": {
        "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}))
elif "--slurp" in sys.argv:
    print("[[]]")
else:
    print(json.dumps(thread))
"""

RADAR_WITH_SHORT_DEADLINE = """
import importlib.util, sys

spec = importlib.util.spec_from_file_location("github_unanswered_comments", sys.argv[1])
radar = sys.modules[spec.name] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(radar)
radar.DISCUSSION_READ_DEADLINE_SECONDS = 1
sys.argv = [sys.argv[1], "--thread", "example/repo#1", "--self-login", "cbusillo", "--format", "json"]
sys.exit(radar.main())
"""


@pytest.mark.parametrize(("stuck_endpoint", "lane"), [("repos/example/repo/issues/1", "thread"), ("graphql", "reactions/")])
def test_stuck_discussion_read_ends_the_radar_at_the_deadline(tmp_path: Path, stuck_endpoint: str, lane: str) -> None:
    # The stuck gh call is never released: the radar process itself must exit.
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        f'exec "{sys.executable}" -c \'{FAKE_GH}\' "{stuck_endpoint}" \'{json.dumps(opening(number=1, author="outside", pull_request=False))}\' "$@"\n'
    )
    fake_gh.chmod(0o755)
    env = {**os.environ, "GITHUB_UNANSWERED_COMMENTS_GH": str(fake_gh), "CODE_HOME": str(tmp_path / "missing")}
    started = time.monotonic()

    result = subprocess.run(
        [sys.executable, "-c", RADAR_WITH_SHORT_DEADLINE, str(MODULE_PATH)],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60,
    )

    assert time.monotonic() - started < 30, result.stderr
    assert result.stdout, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "degraded"
    assert any(
        error["lane"].startswith(lane) and "deadline" in error["error"] for error in payload["coverage"]["errors"]
    ), payload["coverage"]["errors"]

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
