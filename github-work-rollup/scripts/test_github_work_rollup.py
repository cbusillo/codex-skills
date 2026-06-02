#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest",
#     "PyYAML>=6.0.0",
# ]
# ///
# pyright: reportMissingImports=false

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("github_work_rollup.py")
MODULE_SPEC = importlib.util.spec_from_file_location("github_work_rollup", MODULE_PATH)
assert MODULE_SPEC is not None
github_work_rollup = importlib.util.module_from_spec(MODULE_SPEC)
assert MODULE_SPEC.loader is not None
sys.modules[MODULE_SPEC.name] = github_work_rollup
MODULE_SPEC.loader.exec_module(github_work_rollup)


def completed(command: list[str], payload: object, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")


def args(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "config": Path(".local/github-work-rollup.yaml"),
        "repo": [],
        "repo_owner": [],
        "subject": [],
        "window": None,
        "since": None,
        "until": "2026-06-02T16:00:00Z",
        "timezone": None,
        "mode": None,
        "summary_level": None,
        "format": "markdown",
        "output": None,
        "limit_repos": 25,
        "limit_items": 50,
        "include_bots": False,
        "include_external_activity": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_resolve_settings_cli_list_flags_override_private_config_scope() -> None:
    settings = github_work_rollup.resolve_settings(
        args(repo=["example-org/override"], window="12h", summary_level="concise"),
        {
            "timezone": "America/New_York",
            "default_window": "7d",
            "report_recipient": "example team",
            "subjects": ["config-user"],
            "repo_owners": ["private-owner"],
            "repositories": ["example-org/example-repo"],
            "mode": "backlog",
            "summary_level": "standard",
            "noise_filters": {"labels": ["dependencies"]},
        },
    )

    assert settings["timezone"] == "America/New_York"
    assert settings["report_recipient"] == "example team"
    assert settings["repositories"] == ["example-org/override"]
    assert settings["repo_owners"] == []
    assert settings["subjects"] == []
    assert settings["mode"] == "backlog"
    assert settings["summary_level"] == "concise"
    assert settings["window"].since == datetime(2026, 6, 2, 4, tzinfo=timezone.utc)


def test_resolve_settings_cli_mode_overrides_config() -> None:
    settings = github_work_rollup.resolve_settings(args(mode="standup"), {"mode": "activity"})

    assert settings["mode"] == "standup"


def test_collect_rollup_allows_subject_only_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:3] == ["auth", "status"]:
            return completed(command, "")
        if command[1:3] == ["api", "user"]:
            return completed(command, {"login": "example-user"})
        if command[1:5] == ["api", "--method", "GET", "search/issues"]:
            return completed(
                command,
                {
                    "items": [
                        {
                            "number": 42,
                            "title": "Follow-up needed",
                            "html_url": "https://github.com/example-org/example-repo/issues/42",
                            "repository_url": "https://api.github.com/repos/example-org/example-repo",
                            "state": "open",
                            "updated_at": "2026-06-02T15:00:00Z",
                            "user": {"login": "cli-user"},
                            "labels": [{"name": "needs-attention"}],
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(subject=["cli-user"]), {})

    payload = github_work_rollup.collect_rollup(settings)

    assert payload["repositories"] == []
    assert payload["buckets"]["needs_attention"][0]["repo"] == "example-org/example-repo"
    assert any(command[1:5] == ["api", "--method", "GET", "search/issues"] for command in calls)
    search_commands = [command for command in calls if command[1:3] == ["api", "--method"]]
    assert search_commands
    assert all(command[3:5] == ["GET", "search/issues"] for command in search_commands)


def test_repo_scoped_rollup_does_not_search_external_subjects_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:3] == ["auth", "status"]:
            return completed(command, "")
        if command[1:3] == ["api", "user"]:
            return completed(command, {"login": "example-user"})
        if command[1:3] == ["pr", "list"] or command[1:3] == ["issue", "list"]:
            return completed(command, [])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(repo=["example-org/example-repo"], subject=["cli-user"]), {})

    payload = github_work_rollup.collect_rollup(settings)

    assert payload["ok"] is True
    assert not any(command[1:5] == ["api", "--method", "GET", "search/issues"] for command in calls)
    assert any("Subject search outside configured repositories was skipped" in note for note in payload["limitations"])


def test_bucket_items_orders_attention_before_noise() -> None:
    settings = {"summary_level": "standard", "limit_items": 50, "noise_filters": {"labels": ["dependencies"]}}
    rows = [
        {"title": "background", "bucket": "needs_attention", "priority": -5, "updated_at": "2026-06-02T10:00:00Z"},
        {"title": "security", "bucket": "needs_attention", "priority": 10, "updated_at": "2026-06-02T09:00:00Z"},
    ]

    buckets = github_work_rollup.bucket_items(rows, settings)

    assert [row["title"] for row in buckets["needs_attention"]] == ["security", "background"]


def test_bucket_items_orders_equal_priority_by_newest_first() -> None:
    settings = {"summary_level": "standard", "limit_items": 50}
    rows = [
        {"title": "older", "bucket": "in_progress", "priority": 0, "updated_at": "2026-06-02T09:00:00Z"},
        {"title": "newer", "bucket": "in_progress", "priority": 0, "updated_at": "2026-06-02T10:00:00Z"},
    ]

    buckets = github_work_rollup.bucket_items(rows, settings)

    assert [row["title"] for row in buckets["in_progress"]] == ["newer", "older"]


def test_subject_search_filters_bots_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:3] == ["auth", "status"]:
            return completed(command, "")
        if command[1:3] == ["api", "user"]:
            return completed(command, {"login": "example-user"})
        if command[1:3] == ["api", "--method"]:
            return completed(
                command,
                {
                    "items": [
                        {
                            "number": 1,
                            "title": "Bot update",
                            "html_url": "https://github.com/example-org/example-repo/issues/1",
                            "repository_url": "https://api.github.com/repos/example-org/example-repo",
                            "state": "open",
                            "updated_at": "2026-06-02T15:00:00Z",
                            "user": {"login": "dependabot[bot]", "type": "Bot"},
                            "labels": [],
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(subject=["cli-user"]), {})

    payload = github_work_rollup.collect_rollup(settings)

    assert payload["buckets"] == {}


def test_repo_collection_filters_bots_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:3] == ["auth", "status"]:
            return completed(command, "")
        if command[1:3] == ["api", "user"]:
            return completed(command, {"login": "example-user"})
        if command[1:3] == ["pr", "list"] and "--state" in command and command[command.index("--state") + 1] == "open":
            return completed(
                command,
                [
                    {
                        "number": 2,
                        "title": "Bot PR",
                        "url": "https://github.com/example-org/example-repo/pull/2",
                        "author": {"login": "app[bot]", "type": "Bot"},
                        "labels": [],
                        "reviewDecision": "",
                        "isDraft": False,
                        "updatedAt": "2026-06-02T15:00:00Z",
                    }
                ],
            )
        if command[1:3] == ["pr", "list"] or command[1:3] == ["issue", "list"]:
            return completed(command, [])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(repo=["example-org/example-repo"]), {})

    payload = github_work_rollup.collect_rollup(settings)

    assert payload["buckets"] == {}


def test_classify_pr_keeps_draft_approved_pr_in_progress() -> None:
    assert (
        github_work_rollup.classify_pr("open", {"isDraft": True, "reviewDecision": "APPROVED"}, [])
        == "in_progress"
    )


def test_subject_pr_search_uses_pr_classification() -> None:
    item = github_work_rollup.normalize_search_item(
        {
            "number": 3,
            "title": "Blocked PR",
            "html_url": "https://github.com/example-org/example-repo/pull/3",
            "repository_url": "https://api.github.com/repos/example-org/example-repo",
            "state": "open",
            "updated_at": "2026-06-02T15:00:00Z",
            "user": {"login": "cli-user"},
            "labels": [{"name": "blocked"}],
            "pull_request": {},
        },
        "cli-user",
        "author",
        {"noise_filters": {}},
    )

    assert item["kind"] == "pr"
    assert item["bucket"] == "blocked"


def test_render_markdown_uses_display_timezone_and_action_links() -> None:
    payload = {
        "ok": True,
        "report_recipient": "example team",
        "timezone": "America/New_York",
        "window": {"since": "2026-06-02T14:00:00Z", "until": "2026-06-02T16:00:00Z", "label": "2h"},
        "display_window": {"since": "2026-06-02T10:00:00-04:00", "until": "2026-06-02T12:00:00-04:00", "label": "2h"},
        "mode": "standup",
        "repositories": ["example-org/example-repo"],
        "buckets": {
            "ready_for_merge_decision": [
                {
                    "repo": "example-org/example-repo",
                    "number": 7,
                    "title": "Ready PR",
                    "url": "https://github.com/example-org/example-repo/pull/7",
                    "collection_lane": "open_backlog",
                    "handoff": "repo-readiness or github for a fresh merge decision",
                }
            ]
        },
        "priority_sections": [],
        "limitations": [],
    }

    rendered = github_work_rollup.render_markdown(payload)

    assert "2026-06-02T10:00:00-04:00 to 2026-06-02T12:00:00-04:00" in rendered
    assert "Mode: standup" in rendered
    assert "[example-org/example-repo#7](https://github.com/example-org/example-repo/pull/7)" in rendered
    assert "Source: open backlog." in rendered
    assert "Handoff: repo-readiness or github" in rendered


def test_github_commands_are_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:3] == ["auth", "status"]:
            return completed(command, "")
        if command[1:3] == ["api", "user"]:
            return completed(command, {"login": "example-user"})
        if command[1:3] == ["repo", "list"]:
            return completed(command, [{"nameWithOwner": "example-org/example-repo", "isArchived": False}])
        if command[1:3] == ["pr", "list"] or command[1:3] == ["issue", "list"]:
            return completed(command, [])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(repo_owner=["example-org"]), {})

    github_work_rollup.collect_rollup(settings)

    allowed = {
        ("auth", "status"),
        ("api", "user"),
        ("repo", "list"),
        ("pr", "list"),
        ("issue", "list"),
    }
    assert {(command[1], command[2]) for command in calls} <= allowed
    list_commands = [command for command in calls if command[1:3] in (["pr", "list"], ["issue", "list"])]
    assert list_commands
    assert all("--search" in command for command in list_commands)
    assert all("updated:>=2026-06-01" in command for command in list_commands)


def test_activity_mode_filters_old_open_repo_items(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return completed(
            command,
            [
                {
                    "number": 214,
                    "title": "Older open work",
                    "url": "https://github.com/example-org/example-repo/issues/214",
                    "author": {"login": "example-user"},
                    "labels": [],
                    "assignees": [],
                    "createdAt": "2026-05-20T12:00:00Z",
                    "updatedAt": "2026-05-21T12:00:00Z",
                }
            ],
        )

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(repo=["example-org/example-repo"], mode="activity"), {})

    rows = github_work_rollup.collect_issues("example-org/example-repo", "open", settings)

    assert rows == []
    assert "--search" in calls[0]


def test_standup_mode_includes_old_open_repo_items_without_search_window(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return completed(
            command,
            [
                {
                    "number": 214,
                    "title": "Older open work",
                    "url": "https://github.com/example-org/example-repo/issues/214",
                    "author": {"login": "example-user"},
                    "labels": [],
                    "assignees": [],
                    "createdAt": "2026-05-20T12:00:00Z",
                    "updatedAt": "2026-05-21T12:00:00Z",
                }
            ],
        )

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(repo=["example-org/example-repo"], mode="standup"), {})

    rows = github_work_rollup.collect_issues("example-org/example-repo", "open", settings)

    assert len(rows) == 1
    assert rows[0]["number"] == 214
    assert rows[0]["collection_lane"] == "open_backlog"
    assert "--search" not in calls[0]


def test_standup_repo_collection_uses_backlog_and_recent_open_scans(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:3] == ["pr", "list"]:
            return completed(command, [])
        if command[1:3] == ["issue", "list"] and command[command.index("--state") + 1] == "open":
            if "--search" in command:
                return completed(
                    command,
                    [
                        {
                            "number": 50,
                            "title": "Older item updated recently",
                            "url": "https://github.com/example-org/example-repo/issues/50",
                            "author": {"login": "example-user"},
                            "labels": [],
                            "assignees": [],
                            "createdAt": "2026-05-01T12:00:00Z",
                            "updatedAt": "2026-06-02T15:00:00Z",
                        }
                    ],
                )
            return completed(
                command,
                [
                    {
                        "number": 214,
                        "title": "Open backlog item",
                        "url": "https://github.com/example-org/example-repo/issues/214",
                        "author": {"login": "example-user"},
                        "labels": [],
                        "assignees": [],
                        "createdAt": "2026-05-20T12:00:00Z",
                        "updatedAt": "2026-05-21T12:00:00Z",
                    }
                ],
            )
        if command[1:3] == ["issue", "list"]:
            return completed(command, [])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(repo=["example-org/example-repo"], mode="standup"), {})

    rows = github_work_rollup.collect_repo_items("example-org/example-repo", settings)

    issue_commands = [command for command in calls if command[1:3] == ["issue", "list"] and command[command.index("--state") + 1] == "open"]
    assert ["--search" in command for command in issue_commands] == [False, True]
    assert {row["number"] for row in rows} == {50, 214}
    assert {row["number"]: row["collection_lane"] for row in rows} == {50: "recent_activity", 214: "open_backlog"}


def test_standup_mode_marks_recent_open_items_as_recent_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return completed(
            command,
            [
                {
                    "number": 215,
                    "title": "Recently updated open work",
                    "url": "https://github.com/example-org/example-repo/issues/215",
                    "author": {"login": "example-user"},
                    "labels": [],
                    "assignees": [],
                    "createdAt": "2026-05-20T12:00:00Z",
                    "updatedAt": "2026-06-02T15:00:00Z",
                }
            ],
        )

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(repo=["example-org/example-repo"], mode="standup"), {})

    rows = github_work_rollup.collect_issues("example-org/example-repo", "open", settings)

    assert len(rows) == 1
    assert rows[0]["collection_lane"] == "recent_activity"


def test_standup_mode_keeps_closed_items_window_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return completed(
            command,
            [
                {
                    "number": 100,
                    "title": "Old closed issue",
                    "url": "https://github.com/example-org/example-repo/issues/100",
                    "author": {"login": "example-user"},
                    "labels": [],
                    "assignees": [],
                    "createdAt": "2026-05-10T12:00:00Z",
                    "updatedAt": "2026-05-12T12:00:00Z",
                    "closedAt": "2026-05-12T12:00:00Z",
                }
            ],
        )

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(repo=["example-org/example-repo"], mode="standup"), {})

    rows = github_work_rollup.collect_issues("example-org/example-repo", "closed", settings)

    assert rows == []
    assert "--search" in calls[0]


def test_deduplicate_items_prefers_recent_completion_lane() -> None:
    items = [
        {
            "kind": "pr",
            "repo": "owner/repo",
            "number": 10,
            "url": "https://github.com/owner/repo/pull/10",
            "title": "Merged while collecting",
            "state": "open",
            "bucket": "ready_for_review",
            "collection_lane": "open_backlog",
        },
        {
            "kind": "pr",
            "repo": "owner/repo",
            "number": 10,
            "url": "https://github.com/owner/repo/pull/10",
            "title": "Merged while collecting",
            "state": "merged",
            "bucket": "recently_completed",
            "collection_lane": "recent_completion",
        },
    ]

    deduped = github_work_rollup.deduplicate_items(items)

    assert len(deduped) == 1
    assert deduped[0]["state"] == "merged"
    assert deduped[0]["collection_lane"] == "recent_completion"


def test_deduplicate_items_merges_subject_matches() -> None:
    items = [
        {
            "kind": "pr",
            "repo": "owner/repo",
            "number": 1,
            "url": "https://github.com/owner/repo/pull/1",
            "title": "A PR",
            "state": "open",
            "review_decision": "APPROVED",
        },
        {
            "kind": "pr",
            "repo": "owner/repo",
            "number": 1,
            "url": "https://github.com/owner/repo/pull/1",
            "title": "A PR",
            "state": "open",
            "subject": "user1",
            "subject_match": "author",
        },
        {
            "kind": "pr",
            "repo": "owner/repo",
            "number": 1,
            "url": "https://github.com/owner/repo/pull/1",
            "title": "A PR",
            "state": "open",
            "subject": "user2",
            "subject_match": "commenter",
        },
    ]
    deduped = github_work_rollup.deduplicate_items(items)
    assert len(deduped) == 1
    item = deduped[0]
    assert item["review_decision"] == "APPROVED"
    assert set(item["subjects"]) == {"user1", "user2"}
    assert set(item["subject_matches"]) == {"author", "commenter"}


def test_disabled_issues_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Error: the 'owner/repo' repository has disabled issues",
        )

    monkeypatch.setattr(github_work_rollup, "run", fake_run)

    settings = {
        "limit_items": 10,
        "window": github_work_rollup.Window(
            datetime(2026, 6, 1, tzinfo=timezone.utc),
            datetime(2026, 6, 2, tzinfo=timezone.utc),
            "24h",
        ),
    }
    res = github_work_rollup.collect_issues("owner/repo", "open", settings)
    assert res == []
    assert settings["collection_warnings"] == ["Issues are disabled for owner/repo."]


def test_handoff_for_pr_closed_or_merged_is_none() -> None:
    assert github_work_rollup.handoff_for_pr("closed", {}, []) is None
    assert github_work_rollup.handoff_for_pr("merged", {}, []) is None
    assert github_work_rollup.handoff_for_pr("open", {}, []) == "babysit-pr if this PR needs active monitoring"


def test_render_markdown_collapsible_sources() -> None:
    payload = {
        "ok": True,
        "report_recipient": "Chris",
        "timezone": "America/New_York",
        "window": {"since": "2026-06-02T14:00:00Z", "until": "2026-06-02T16:00:00Z", "label": "2h"},
        "display_window": {"since": "2026-06-02T10:00:00-04:00", "until": "2026-06-02T12:00:00-04:00", "label": "2h"},
        "repositories": [f"owner/repo{i}" for i in range(10)],
        "subjects": ["example-user"],
        "buckets": {},
        "priority_sections": [],
        "limitations": [],
    }
    rendered = github_work_rollup.render_markdown(payload)
    assert "Sources: 10 repositories" in rendered
    assert "subjects: example-user" in rendered
    assert "<details>" in rendered
    assert "<summary>Show repositories</summary>" in rendered


def test_summary_counts_and_rendering_exclude_collection_warnings_from_attention() -> None:
    summary = github_work_rollup.summary_counts(
        {
            "waiting": [{"title": "wait", "collection_lane": "open_backlog"}],
            "recently_completed": [{"title": "done", "collection_lane": "recent_completion"}],
        },
        ["Issues are disabled for owner/repo."],
    )

    lines = github_work_rollup.render_executive_summary(summary)

    assert lines[0] == "No actual attention items detected."
    assert "waiting: 1" in lines[1]
    assert "completed: 1" in lines[1]
    assert "open backlog: 1" in lines[1]
    assert "collection warnings: 1" in lines[1]


def test_render_item_suppresses_completed_handoff() -> None:
    rendered = github_work_rollup.render_item(
        {
            "repo": "owner/repo",
            "number": 1,
            "title": "Merged PR",
            "url": "https://github.com/owner/repo/pull/1",
            "bucket": "recently_completed",
            "handoff": "babysit-pr if this PR needs active monitoring",
        }
    )

    assert "Handoff" not in rendered


def test_priority_sections_filter_completed_by_default() -> None:
    sections = github_work_rollup.priority_sections(
        [
            {"repo": "owner/repo", "bucket": "recently_completed", "title": "done", "updated_at": "2026-06-02T10:00:00Z"},
            {"repo": "owner/repo", "bucket": "ready_for_review", "title": "review", "updated_at": "2026-06-02T11:00:00Z"},
        ],
        {"priority_sections": [{"name": "Focus", "repositories": ["owner/repo"]}]},
    )

    assert len(sections) == 1
    assert [item["title"] for item in sections[0]["items"]] == ["review"]
    assert [item["title"] for item in sections[0]["recently_completed"]] == ["done"]


def test_render_priority_section_keeps_completed_summary_without_handoffs() -> None:
    rendered = "\n".join(
        github_work_rollup.render_priority_section(
            {
                "name": "Focus",
                "items": [],
                "recently_completed": [
                    {
                        "repo": "owner/repo",
                        "number": 1,
                        "title": "Done PR",
                        "url": "https://github.com/owner/repo/pull/1",
                        "bucket": "recently_completed",
                        "handoff": "babysit-pr if this PR needs active monitoring",
                    }
                ],
                "recently_completed_count": 4,
            }
        )
    )

    assert "No actionable open items" in rendered
    assert "### Recently Completed" in rendered
    assert "Done PR" in rendered
    assert "3 more recently completed" in rendered
    assert "Handoff" not in rendered


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
