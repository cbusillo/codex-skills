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
import base64
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
        "report_recipient": None,
        "people_index": None,
        "layout": None,
        "mode": None,
        "summary_level": None,
        "format": "markdown",
        "output": None,
        "limit_repos": 25,
        "limit_items": 50,
        "collection_limit_items": None,
        "release_collection_limit": None,
        "workflow_collection_limit": None,
        "include_derived_context": False,
        "context_repo_limit": None,
        "include_bots": False,
        "include_external_activity": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def empty_enrichment_response(command: list[str]) -> subprocess.CompletedProcess[str] | None:
    if command[1:3] in (["release", "list"], ["run", "list"]):
        return completed(command, [])
    if command[1:3] == ["repo", "view"]:
        return completed(command, {"description": "", "homepageUrl": "", "repositoryTopics": [], "url": ""})
    if command[1] == "api" and len(command) > 2 and str(command[2]).endswith("/readme"):
        return completed(command, {"message": "Not Found"}, returncode=1)
    return None


def write_people_index(tmp_path: Path) -> Path:
    path = tmp_path / "people.yaml"
    path.write_text(
        """
---
version: 1
people:
  - id: example-owner
    display_name: Example Owner
    preferred_reference: Owner
    aliases:
      - Example Owner
      - owner-handle
    relationship:
      kind: manager
      roles:
        - owner
        - planning-manager
    organization:
      company: Example Company
      scale: Processes many devices each year.
    contacts:
      github:
        username: owner-handle
    preferences:
      communication_style:
        technical_depth: low-to-medium
        framing: big-picture-first
        detail_preference: outcomes, cost, risk, sequencing, and customer impact
        report_guidance: Aim for one page normally and two pages on heavy days.
    trust:
      handling: Verify production data safety before accepting good enough.
""".lstrip(),
        encoding="utf-8",
    )
    return path


def write_ambiguous_people_index(tmp_path: Path) -> Path:
    path = tmp_path / "ambiguous-people.yaml"
    path.write_text(
        """
---
version: 1
people:
  - id: first-owner
    display_name: First Owner
    aliases:
      - Owner
  - id: second-owner
    display_name: Second Owner
    aliases:
      - Owner
""".lstrip(),
        encoding="utf-8",
    )
    return path


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


def test_resolve_settings_cli_overrides_config_labels() -> None:
    settings = github_work_rollup.resolve_settings(
        args(mode="standup", report_recipient="Justin"),
        {"mode": "activity", "report_recipient": "example team"},
    )

    assert settings["mode"] == "standup"
    assert settings["report_recipient"] == "Justin"


def test_collection_limits_accept_integer_like_yaml_floats() -> None:
    settings = github_work_rollup.resolve_settings(
        args(),
        {"collection_limit_items": 25.0, "release_collection_limit": 3.0, "workflow_collection_limit": 4.0},
    )

    assert settings["collection_limit_items"] == 25
    assert settings["release_collection_limit"] == 3
    assert settings["workflow_collection_limit"] == 4


def test_collection_limits_reject_yaml_booleans() -> None:
    with pytest.raises(SystemExit, match="collection_limit_items must be a positive integer"):
        github_work_rollup.resolve_settings(args(), {"collection_limit_items": True})


def test_resolve_recipient_profile_matches_people_index(tmp_path: Path) -> None:
    people_index = write_people_index(tmp_path)
    settings = github_work_rollup.resolve_settings(
        args(report_recipient="owner-handle", people_index=people_index),
        {},
    )

    profile = github_work_rollup.resolve_recipient_profile(settings)

    assert profile["preferred_reference"] == "Owner"
    assert profile["company"] == "Example Company"
    assert profile["technical_depth"] == "low-to-medium"
    assert profile["framing"] == "big-picture-first"


def test_resolve_recipient_profile_missing_index_is_nonfatal(tmp_path: Path) -> None:
    settings = github_work_rollup.resolve_settings(
        args(report_recipient="Example Owner", people_index=tmp_path / "missing.yaml"),
        {},
    )

    assert github_work_rollup.resolve_recipient_profile(settings) == {}


def test_resolve_recipient_profile_skips_ambiguous_matches(tmp_path: Path) -> None:
    settings = github_work_rollup.resolve_settings(
        args(report_recipient="Owner", people_index=write_ambiguous_people_index(tmp_path)),
        {},
    )
    settings["collection_warnings"] = []

    assert github_work_rollup.resolve_recipient_profile(settings) == {}
    assert any("matched multiple people" in warning for warning in settings["collection_warnings"])


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
        if response := empty_enrichment_response(command):
            return response
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
        if response := empty_enrichment_response(command):
            return response
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(repo=["example-org/example-repo"], subject=["cli-user"]), {})

    payload = github_work_rollup.collect_rollup(settings)

    assert payload["ok"] is True
    assert not any(command[1:5] == ["api", "--method", "GET", "search/issues"] for command in calls)
    assert any("Subject search outside configured repositories was skipped" in note for note in payload["limitations"])


def test_collect_rollup_attaches_recipient_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["auth", "status"]:
            return completed(command, "")
        if command[1:3] == ["api", "user"]:
            return completed(command, {"login": "example-user"})
        if command[1:3] == ["pr", "list"] or command[1:3] == ["issue", "list"]:
            return completed(command, [])
        if response := empty_enrichment_response(command):
            return response
        raise AssertionError(f"unexpected command: {command}")

    people_index = write_people_index(tmp_path)
    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(
        args(repo=["example-org/example-repo"], report_recipient="Example Owner", people_index=people_index),
        {},
    )

    payload = github_work_rollup.collect_rollup(settings)

    assert payload["recipient_profile"]["company"] == "Example Company"
    assert payload["recipient_profile"]["technical_depth"] == "low-to-medium"
    assert "trust_handling" not in payload["recipient_profile"]
    assert "report_guidance" not in payload["recipient_profile"]
    assert "company_scale" not in payload["recipient_profile"]


def test_collect_rollup_attaches_derived_repo_context(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    readme = base64.b64encode(
        b"# Codex Lab\n\nCodex Lab is a harness for agent workflows.\n\n## Details\nMore text."
    ).decode("ascii")

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:3] == ["auth", "status"]:
            return completed(command, "")
        if command[1:3] == ["api", "user"]:
            return completed(command, {"login": "example-user"})
        if command[1:3] == ["pr", "list"] or command[1:3] == ["issue", "list"]:
            return completed(command, [])
        if command[1:3] == ["repo", "view"]:
            return completed(
                command,
                {
                    "description": "New harness for agent workflows.",
                    "homepageUrl": "https://example.test/codex-lab",
                    "repositoryTopics": [{"name": "agents"}],
                    "url": "https://github.com/example-org/codex-lab",
                },
            )
        if command[1] == "api" and command[2] == "repos/example-org/codex-lab/readme":
            return completed(
                command,
                {
                    "content": readme,
                    "encoding": "base64",
                    "path": "README.md",
                    "sha": "abc123",
                    "html_url": "https://github.com/example-org/codex-lab/blob/main/README.md",
                },
            )
        if response := empty_enrichment_response(command):
            return response
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(
        args(repo=["example-org/codex-lab"], include_derived_context=True, context_repo_limit=5),
        {},
    )

    payload = github_work_rollup.collect_rollup(settings)

    context = payload["derived_context"]
    assert context["source"].startswith("derived from GitHub")
    assert context["repositories"][0]["repo"] == "example-org/codex-lab"
    assert context["repositories"][0]["description"] == "New harness for agent workflows."
    assert context["repositories"][0]["topics"] == ["agents"]
    assert "Codex Lab is a harness" in context["repositories"][0]["readme"]["excerpt"]
    assert context["repositories"][0]["claims"][0]["standing_context"] is True
    assert any(command[1:3] == ["repo", "view"] for command in calls)
    assert any(command[1:3] == ["api", "repos/example-org/codex-lab/readme"] for command in calls)


def test_missing_readme_context_does_not_pollute_limitations(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["auth", "status"]:
            return completed(command, "")
        if command[1:3] == ["api", "user"]:
            return completed(command, {"login": "example-user"})
        if command[1:3] == ["pr", "list"] or command[1:3] == ["issue", "list"]:
            return completed(command, [])
        if command[1:3] == ["repo", "view"]:
            return completed(
                command,
                {
                    "description": "Repo with no README.",
                    "homepageUrl": "",
                    "repositoryTopics": [],
                    "url": "https://github.com/example-org/no-readme",
                },
            )
        if command[1] == "api" and command[2] == "repos/example-org/no-readme/readme":
            return completed(command, {"message": "Not Found"}, returncode=1)
        if response := empty_enrichment_response(command):
            return response
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(
        args(repo=["example-org/no-readme"], include_derived_context=True),
        {},
    )

    payload = github_work_rollup.collect_rollup(settings)

    assert payload["derived_context"]["repositories"][0]["description"] == "Repo with no README."
    assert not any("README context" in limitation for limitation in payload["limitations"])


def test_select_context_repos_prefers_urgent_repo_over_noisy_repo() -> None:
    repos = ["example-org/noisy", "example-org/urgent"]
    items = [
        {"repo": "example-org/noisy", "bucket": "open_backlog"},
        {"repo": "example-org/noisy", "bucket": "open_backlog"},
        {"repo": "example-org/noisy", "bucket": "open_backlog"},
        {"repo": "example-org/noisy", "bucket": "open_backlog"},
        {"repo": "example-org/noisy", "bucket": "open_backlog"},
        {"repo": "example-org/noisy", "bucket": "open_backlog"},
        {"repo": "example-org/noisy", "bucket": "open_backlog"},
        {"repo": "example-org/urgent", "bucket": "needs_attention", "priority": 3},
    ]

    selected = github_work_rollup.select_context_repos(repos, items, 1)

    assert selected == ["example-org/urgent"]


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
        if response := empty_enrichment_response(command):
            return response
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(subject=["cli-user"]), {})

    payload = github_work_rollup.collect_rollup(settings)

    assert payload["buckets"] == {}


def test_subject_search_pages_until_collection_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:3] == ["auth", "status"]:
            return completed(command, "")
        if command[1:3] == ["api", "user"]:
            return completed(command, {"login": "example-user"})
        if command[1:3] == ["api", "--method"]:
            query = command[command.index("-f") + 1]
            if not query.startswith("q=author:"):
                return completed(command, {"total_count": 0, "items": []})
            page_field = next(item for item in command if item.startswith("page="))
            page = int(page_field.split("=", 1)[1])
            start = (page - 1) * 100
            count = 100 if page == 1 else 50
            return completed(
                command,
                {
                    "total_count": 150,
                    "items": [
                        {
                            "number": start + i,
                            "title": f"Subject item {start + i}",
                            "html_url": f"https://github.com/example-org/example-repo/issues/{start + i}",
                            "repository_url": "https://api.github.com/repos/example-org/example-repo",
                            "state": "open",
                            "updated_at": "2026-06-02T15:00:00Z",
                            "user": {"login": "cli-user"},
                            "labels": [],
                        }
                        for i in range(count)
                    ],
                },
            )
        if response := empty_enrichment_response(command):
            return response
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(subject=["cli-user"], limit_items=10, collection_limit_items=150), {})

    payload = github_work_rollup.collect_rollup(settings)

    search_commands = [command for command in calls if command[1:3] == ["api", "--method"] and "q=author:cli-user updated:>=2026-06-01" in command]
    assert [field for command in search_commands for field in command if field.startswith("page=")] == ["page=1", "page=2"]
    assert [field for command in search_commands for field in command if field.startswith("per_page=")] == ["per_page=100", "per_page=50"]
    assert payload["summary"]["recent_activity"] == 150
    assert not any("Subject search" in note for note in payload["limitations"])


def test_subject_search_surfaces_incomplete_results(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["auth", "status"]:
            return completed(command, "")
        if command[1:3] == ["api", "user"]:
            return completed(command, {"login": "example-user"})
        if command[1:3] == ["api", "--method"]:
            return completed(command, {"total_count": 12, "incomplete_results": True, "items": []})
        if response := empty_enrichment_response(command):
            return response
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(subject=["cli-user"]), {})

    payload = github_work_rollup.collect_rollup(settings)

    assert any("Subject search for cli-user author returned incomplete results" in note for note in payload["limitations"])


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
        if response := empty_enrichment_response(command):
            return response
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


def test_render_operator_markdown_uses_display_timezone_and_action_links() -> None:
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

    rendered = github_work_rollup.render_operator_markdown(payload)

    assert "## Operator Summary" in rendered
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
        if response := empty_enrichment_response(command):
            return response
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
        ("release", "list"),
        ("run", "list"),
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


def test_render_operator_markdown_collapsible_sources() -> None:
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
    rendered = github_work_rollup.render_operator_markdown(payload)
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

    lines = github_work_rollup.render_operator_summary(summary)

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


def test_render_priority_section_reports_hidden_open_items() -> None:
    rendered = "\n".join(
        github_work_rollup.render_priority_section(
            {
                "name": "Focus",
                "item_count": 12,
                "items": [
                    {
                        "repo": "owner/repo",
                        "number": i,
                        "title": f"Open item {i}",
                        "url": f"https://github.com/owner/repo/issues/{i}",
                        "bucket": "in_progress",
                    }
                    for i in range(10)
                ],
                "recently_completed": [],
            }
        )
    )

    assert "Open item 0" in rendered
    assert "2 more actionable open item(s)" in rendered


def test_resolve_settings_layout() -> None:
    settings = github_work_rollup.resolve_settings(args(layout="executive"), {"layout": "manager"})
    assert settings["layout"] == "executive"

    settings = github_work_rollup.resolve_settings(args(), {"layout": "manager"})
    assert settings["layout"] == "manager"

    settings = github_work_rollup.resolve_settings(args(), {})
    assert settings["layout"] == "operator"


def test_resolve_settings_rejects_removed_layout_aliases() -> None:
    with pytest.raises(SystemExit):
        github_work_rollup.resolve_settings(args(), {"layout": "standard"})
    with pytest.raises(SystemExit):
        github_work_rollup.resolve_settings(args(), {"layout": "brief"})


def test_collect_releases_and_workflows(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:3] == ["auth", "status"]:
            return completed(command, "")
        if command[1:3] == ["api", "user"]:
            return completed(command, {"login": "example-user"})
        if command[1:3] == ["pr", "list"] or command[1:3] == ["issue", "list"]:
            return completed(command, [])
        if command[1:3] == ["release", "list"]:
            return completed(command, [
                {
                    "tagName": "v1.0.0",
                    "name": "First Release",
                    "createdAt": "2026-06-02T10:00:00Z",
                    "publishedAt": "2026-06-02T10:00:00Z",
                    "isDraft": False,
                    "isPrerelease": False,
                    "url": "https://github.com/example-org/example-repo/releases/tag/v1.0.0"
                },
                {
                    "tagName": "v1.1.0-beta",
                    "name": "Beta Release",
                    "createdAt": "2026-06-02T10:00:00Z",
                    "publishedAt": "2026-06-02T10:00:00Z",
                    "isDraft": False,
                    "isPrerelease": True,
                },
                # Old release, should be filtered out
                {
                    "tagName": "v0.1.0",
                    "name": "Alpha Release",
                    "createdAt": "2026-05-01T10:00:00Z",
                    "publishedAt": "2026-05-01T10:00:00Z",
                    "isDraft": False,
                    "isPrerelease": False,
                    "url": "https://github.com/example-org/example-repo/releases/tag/v0.1.0"
                }
            ])
        if command[1:3] == ["run", "list"]:
            return completed(command, [
                {
                    "name": "Deploy production",
                    "status": "completed",
                    "conclusion": "success",
                    "createdAt": "2026-06-01T15:00:00Z",
                    "updatedAt": "2026-06-02T11:00:00Z",
                    "url": "https://github.com/example-org/example-repo/actions/runs/1"
                },
                {
                    "name": "Queued deploy",
                    "status": "in_progress",
                    "conclusion": "",
                    "createdAt": "2026-06-02T11:00:00Z",
                    "updatedAt": "2026-06-02T11:30:00Z",
                    "url": "https://github.com/example-org/example-repo/actions/runs/2"
                }
            ])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(repo=["example-org/example-repo"], layout="executive", window="24h"), {})
    payload = github_work_rollup.collect_rollup(settings)

    assert payload["layout"] == "executive"
    assert len(payload["releases"]) == 1
    assert payload["releases"][0]["tag_name"] == "v1.0.0"
    assert payload["releases"][0]["url"] == "https://github.com/example-org/example-repo/releases/tag/v1.0.0"
    assert len(payload["workflows"]) == 1
    assert payload["workflows"][0]["name"] == "Deploy production"
    assert payload["workflows"][0]["completed_at"] == "2026-06-02T11:00:00Z"
    release_commands = [command for command in calls if command[1:3] == ["release", "list"]]
    assert release_commands
    assert "url" not in release_commands[0][release_commands[0].index("--json") + 1].split(",")
    assert "--exclude-drafts" in release_commands[0]
    assert "--exclude-pre-releases" in release_commands[0]


def test_collection_uses_deep_limits_before_render_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:3] == ["auth", "status"]:
            return completed(command, "")
        if command[1:3] == ["api", "user"]:
            return completed(command, {"login": "example-user"})
        if command[1:3] == ["pr", "list"]:
            return completed(command, [])
        if command[1:3] == ["issue", "list"]:
            if command[command.index("--state") + 1] != "open":
                return completed(command, [])
            return completed(
                command,
                [
                    {
                        "number": i,
                        "title": f"Open issue {i}",
                        "url": f"https://github.com/example-org/example-repo/issues/{i}",
                        "author": {"login": "example-user"},
                        "labels": [],
                        "assignees": [],
                        "createdAt": "2026-06-02T09:00:00Z",
                        "updatedAt": "2026-06-02T10:00:00Z",
                    }
                    for i in range(3)
                ],
            )
        if command[1:3] == ["release", "list"]:
            return completed(command, [])
        if command[1:3] == ["run", "list"]:
            return completed(command, [])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(repo=["example-org/example-repo"], layout="manager", mode="backlog", limit_items=2), {})

    payload = github_work_rollup.collect_rollup(settings)

    issue_commands = [command for command in calls if command[1:3] == ["issue", "list"]]
    release_commands = [command for command in calls if command[1:3] == ["release", "list"]]
    workflow_commands = [command for command in calls if command[1:3] == ["run", "list"]]
    assert issue_commands and all(command[command.index("--limit") + 1] == "1000" for command in issue_commands)
    assert release_commands and release_commands[0][release_commands[0].index("--limit") + 1] == "1000"
    assert workflow_commands and workflow_commands[0][workflow_commands[0].index("--limit") + 1] == "1000"
    assert payload["summary"]["open_backlog"] == 3
    assert sum(len(rows) for rows in payload["buckets"].values()) == 3
    assert not any("collection" in note.casefold() and "reached" in note.casefold() for note in payload["limitations"])


def test_collection_limit_warnings_only_at_deep_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["auth", "status"]:
            return completed(command, "")
        if command[1:3] == ["api", "user"]:
            return completed(command, {"login": "example-user"})
        if command[1:3] == ["pr", "list"] or command[1:3] == ["issue", "list"]:
            return completed(command, [])
        if command[1:3] == ["release", "list"]:
            return completed(
                command,
                [
                    {
                        "tagName": f"v{i}",
                        "name": f"Release {i}",
                        "createdAt": "2026-06-02T10:00:00Z",
                        "publishedAt": "2026-06-02T10:00:00Z",
                        "isDraft": False,
                        "isPrerelease": False,
                    }
                    for i in range(3)
                ],
            )
        if command[1:3] == ["run", "list"]:
            return completed(
                command,
                [
                    {
                        "name": f"CI {i}",
                        "status": "completed",
                        "conclusion": "success",
                        "createdAt": "2026-06-02T09:00:00Z",
                        "updatedAt": "2026-06-02T10:00:00Z",
                    }
                    for i in range(4)
                ],
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(
        args(repo=["example-org/example-repo"], release_collection_limit=3, workflow_collection_limit=4),
        {},
    )

    payload = github_work_rollup.collect_rollup(settings)

    assert any("Release collection for example-org/example-repo reached 3" in note for note in payload["limitations"])
    assert any("Workflow collection for example-org/example-repo reached 4" in note for note in payload["limitations"])


def test_operator_layout_collects_same_enrichment_data(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:3] == ["auth", "status"]:
            return completed(command, "")
        if command[1:3] == ["api", "user"]:
            return completed(command, {"login": "example-user"})
        if command[1:3] == ["pr", "list"] or command[1:3] == ["issue", "list"]:
            return completed(command, [])
        if response := empty_enrichment_response(command):
            return response
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(repo=["example-org/example-repo"], layout="operator"), {})

    payload = github_work_rollup.collect_rollup(settings)

    assert payload["layout"] == "operator"
    assert any(command[1:3] == ["release", "list"] for command in calls)
    assert any(command[1:3] == ["run", "list"] for command in calls)


def test_collect_workflows_filters_by_completion_time(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return completed(
            command,
            [
                {
                    "name": "Long running deploy",
                    "status": "completed",
                    "conclusion": "success",
                    "createdAt": "2026-06-01T10:00:00Z",
                    "updatedAt": "2026-06-02T15:00:00Z",
                    "url": "https://github.com/example-org/example-repo/actions/runs/1",
                },
                {
                    "name": "Old completed deploy",
                    "status": "completed",
                    "conclusion": "success",
                    "createdAt": "2026-06-01T10:00:00Z",
                    "updatedAt": "2026-06-01T12:00:00Z",
                    "url": "https://github.com/example-org/example-repo/actions/runs/2",
                },
            ],
        )

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(repo=["example-org/example-repo"], layout="executive"), {})

    rows = github_work_rollup.collect_repo_workflows("example-org/example-repo", settings)

    assert [row["name"] for row in rows] == ["Long running deploy"]


def test_executive_activity_comparison_summarizes_previous_window() -> None:
    window = github_work_rollup.Window(
        datetime(2026, 6, 1, tzinfo=timezone.utc),
        datetime(2026, 6, 2, tzinfo=timezone.utc),
        "24h",
    )

    summary = github_work_rollup.comparison_summary(
        {"visible": 5},
        {"visible": 2},
        window,
    )

    assert summary == "Activity was higher than the previous window (+3): 5 visible items this day versus 2 in the previous day."
    previous = github_work_rollup.preceding_window(window)
    assert previous.since == datetime(2026, 5, 31, tzinfo=timezone.utc)
    assert previous.until == datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_executive_activity_comparison_suppresses_incomplete_collection() -> None:
    settings = {"layout": "executive"}
    buckets = {
        "needs_attention": [
            {
                "repo": "example-org/missing",
                "kind": "collection_error",
                "state": "open",
                "title": "Unable to collect prs for example-org/missing",
            }
        ]
    }

    comparison = github_work_rollup.collect_activity_comparison(settings, [], buckets, [], [], [])

    assert comparison == {
        "summary": "Comparison is incomplete because one or more configured sources could not be collected."
    }


def test_configured_repo_gaps_uses_exact_warning_repo_names() -> None:
    gaps = github_work_rollup.configured_repo_gaps(
        ["example-org/example-repo", "example-org/example-repo-other"],
        {},
        ["Could not collect workflow runs for example-org/example-repo-other: API unavailable for configured token"],
    )

    assert gaps == ["example-org/example-repo-other"]


def test_configured_repo_gaps_ignore_truncation_warnings() -> None:
    gaps = github_work_rollup.configured_repo_gaps(
        ["example-org/example-repo"],
        {},
        ["Workflow collection for example-org/example-repo reached 100; automation counts may be incomplete."],
    )

    assert gaps == []


def test_executive_activity_comparison_allows_truncation_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    window = github_work_rollup.Window(
        datetime(2026, 6, 1, tzinfo=timezone.utc),
        datetime(2026, 6, 2, tzinfo=timezone.utc),
        "24h",
    )
    settings = {"layout": "executive", "window": window, "collection_warnings": []}
    buckets = {
        "recently_completed": [
            {
                "repo": "example-org/example-repo",
                "number": 1,
                "title": "Ship useful work",
                "kind": "pr",
                "state": "merged",
            }
        ]
    }

    def fake_collect_activity(
        _settings: dict[str, object],
        _repos: list[str],
    ) -> tuple[list[dict[str, object]], dict[str, list[dict[str, object]]], list[dict[str, object]], list[dict[str, object]]]:
        return [], {}, [], []

    monkeypatch.setattr(github_work_rollup, "collect_activity", fake_collect_activity)

    comparison = github_work_rollup.collect_activity_comparison(
        settings,
        ["example-org/example-repo"],
        buckets,
        [],
        [],
        ["Workflow collection for example-org/example-repo reached 100; automation counts may be incomplete."],
    )

    assert comparison is not None
    assert comparison["summary"].startswith("Activity was higher than the previous window")


def test_executive_collection_surfaces_release_and_workflow_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["auth", "status"]:
            return completed(command, "")
        if command[1:3] == ["api", "user"]:
            return completed(command, {"login": "example-user"})
        if command[1:3] == ["pr", "list"] or command[1:3] == ["issue", "list"]:
            return completed(command, [])
        if command[1:3] in (["release", "list"], ["run", "list"]):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="API unavailable")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(github_work_rollup, "run", fake_run)
    settings = github_work_rollup.resolve_settings(args(repo=["example-org/example-repo"], layout="executive"), {})

    payload = github_work_rollup.collect_rollup(settings)

    assert any("Could not collect releases" in note for note in payload["limitations"])
    assert any("Could not collect workflow runs" in note for note in payload["limitations"])


def test_render_manager_brief_markdown() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-06-01T16:00:00Z", "until": "2026-06-02T16:00:00Z", "label": "24h"},
        "timezone": "America/New_York",
        "report_recipient": "Chris",
        "recipient_profile": {
            "company": "Example Company",
            "roles": ["owner"],
            "technical_depth": "low-to-medium",
            "framing": "big-picture-first",
            "detail_preference": "outcomes and risk",
        },
        "repositories": ["example-org/code"],
        "layout": "manager",
        "buckets": {
            "needs_attention": [
                {
                    "repo": "example-org/code",
                    "number": 101,
                    "title": "Choose auth direction",
                    "url": "https://github.com/example-org/code/issues/101",
                    "kind": "issue",
                    "state": "open",
                    "handoff": "github-plan for planning reconciliation",
                }
            ],
            "ready_for_review": [
                {
                    "repo": "example-org/code",
                    "number": 102,
                    "title": "Review CLI fallback",
                    "url": "https://github.com/example-org/code/pull/102",
                    "kind": "pr",
                    "state": "open",
                }
            ],
            "recently_completed": [
                {
                    "repo": "example-org/code",
                    "number": 202,
                    "title": "Ship session routing",
                    "url": "https://github.com/example-org/code/pull/202",
                    "kind": "pr",
                    "state": "merged",
                }
            ],
        },
        "priority_sections": [
            {
                "name": "Every Code",
                "items": [{"repo": "example-org/code", "title": "Choose auth direction"}],
                "recently_completed": [{"repo": "example-org/code", "title": "Ship session routing"}],
                "recently_completed_count": 4,
            }
        ],
        "limitations": ["Read-only mode"],
        "releases": [],
        "workflows": [
            {"repo": "example-org/code", "name": "CI", "status": "completed", "conclusion": "success"}
        ],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")

    assert "# GitHub Planning Brief for Chris" in rendered
    assert "active planning scope for Example Company" in rendered
    assert "technical depth: low-to-medium" in rendered
    assert "## Planning Summary" in rendered
    assert "## Today's Priorities" in rendered
    assert "## Active Work" in rendered
    assert "## Focus Areas" in rendered
    assert "Every Code**: 1 open priority item(s), 4 recent completion(s)" in rendered
    assert "## Decisions and Risks" in rendered
    assert "## Velocity" in rendered
    assert "[code#101](https://github.com/example-org/code/issues/101) Choose auth direction" in rendered
    assert "Ship session routing" in rendered
    assert "Automation was green" in rendered


def test_render_manager_brief_uses_profile_framing_without_attention_items() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-06-01T16:00:00Z", "until": "2026-06-02T16:00:00Z", "label": "24h"},
        "timezone": "America/New_York",
        "report_recipient": "Owner",
        "recipient_profile": {
            "company": "Example Company",
            "roles": ["owner"],
            "detail_preference": "outcomes and risk",
        },
        "repositories": ["example-org/code"],
        "layout": "manager",
        "buckets": {
            "in_progress": [
                {"repo": "example-org/code", "number": 1, "title": "Open work", "kind": "issue", "state": "open"}
            ]
        },
        "priority_sections": [],
        "limitations": [],
        "releases": [],
        "workflows": [],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")

    assert "based on outcomes and risk" in rendered


def test_render_payload_rejects_unknown_markdown_layout() -> None:
    with pytest.raises(ValueError):
        github_work_rollup.render_payload({"ok": True, "layout": "standard"}, "markdown")


def test_json_payload_does_not_publish_private_profile_fields() -> None:
    rendered = github_work_rollup.render_payload(
        {
            "ok": True,
            "layout": "executive",
            "recipient_profile": {
                "company": "Example Company",
                "technical_depth": "low-to-medium",
                "trust_handling": "private trust note",
                "report_guidance": "private report guidance",
            },
        },
        "json",
    )

    assert "private trust note" not in rendered
    assert "private report guidance" not in rendered


def test_render_executive_brief_markdown() -> None:
    payload = {
        "ok": True,
        "schema_version": 1,
        "script_version": 1,
        "generated_at": "2026-06-02T16:00:00Z",
        "window": {"since": "2026-06-01T16:00:00Z", "until": "2026-06-02T16:00:00Z", "label": "24h"},
        "timezone": "America/New_York",
        "report_recipient": "Justin",
        "recipient_profile": {
            "company": "Example Company",
            "roles": ["owner"],
            "technical_depth": "low-to-medium",
            "framing": "big-picture-first",
            "detail_preference": "outcomes, cost, risk, sequencing, and customer impact",
            "report_guidance": "Aim for one page normally and two pages on heavy days.",
            "trust_handling": "Private note that should not be rendered.",
        },
        "repositories": ["example-org/code", "example-org/example-skills"],
        "subjects": [],
        "summary_level": "standard",
        "mode": "activity",
        "layout": "executive",
        "buckets": {
            "needs_attention": [
                {
                    "repo": "example-org/code",
                    "number": 101,
                    "title": "Critical Bug",
                    "url": "https://github.com/example-org/code/issues/101",
                    "kind": "issue",
                    "state": "open",
                    "handoff": "Please review soon",
                }
            ],
            "recently_completed": [
                {
                    "repo": "example-org/code",
                    "number": 202,
                    "title": "Document auth flow",
                    "url": "https://github.com/example-org/code/pull/202",
                    "kind": "pr",
                    "state": "merged",
                    "labels": ["documentation"],
                },
                {
                    "repo": "example-org/example-skills",
                    "number": 203,
                    "title": "Add helper validation",
                    "url": "https://github.com/example-org/example-skills/pull/203",
                    "kind": "pr",
                    "state": "merged",
                    "labels": ["documentation"],
                },
                {
                    "repo": "example-org/code",
                    "number": 204,
                    "title": "Close abandoned auth attempt",
                    "url": "https://github.com/example-org/code/pull/204",
                    "kind": "pr",
                    "state": "closed",
                    "labels": ["cleanup"],
                }
            ]
        },
        "releases": [
            {
                "repo": "example-org/code",
                "tag_name": "v1.0.0",
                "name": "First Release",
                "published_at": "2026-06-02T10:00:00Z",
                "url": "https://github.com/example-org/code/releases/tag/v1.0.0"
            }
        ],
        "workflows": [
            {
                "repo": "example-org/code",
                "name": "Deploy production",
                "status": "completed",
                "conclusion": "failure",
                "created_at": "2026-06-02T11:00:00Z",
                "url": "https://github.com/example-org/example-repo/actions/runs/1"
            }
        ],
        "priority_sections": [
            {
                "name": "Every Code / Skills",
                "workstream": "Every Code skill reporting",
                "relationship": "Every Code skill reporting inside Every Code / Skills",
                "initiatives": ["Work Brief"],
                "items": [],
                "recently_completed": [
                    {
                        "repo": "example-org/example-skills",
                        "number": 203,
                        "title": "Add helper validation",
                        "url": "https://github.com/example-org/example-skills/pull/203",
                        "kind": "pr",
                        "state": "merged",
                    }
                ],
                "recently_completed_count": 1,
            }
        ],
        "limitations": ["Read-only mode"]
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")

    assert "# Daily Work Brief for Justin" in rendered
    assert "The visible decision is how to sequence Every Code skill reporting inside Every Code / Skills: 2 completed items, 1 cleared path, 1 release, and 1 open follow-up." in rendered
    assert "Private note" not in rendered
    assert "## Executive Summary" in rendered
    assert "## Needs Justin's Attention" in rendered
    assert "## Every Code / Skills Impact" in rendered
    assert "## Failed Runs" in rendered
    assert "## Work Items Behind This Brief" in rendered
    assert "## Questions to Decide" in rendered

    assert "[code#101](https://github.com/example-org/code/issues/101) Critical Bug" in rendered
    assert "Finished work landed; the next choices are concentrated around Critical Bug." in rendered
    assert "Every Code skill reporting inside Every Code / Skills" in rendered
    assert "Key initiatives: Work Brief." in rendered
    assert "Deploy production" in rendered
    assert "Completed during the day: code#202 Document auth flow; example-skills#203 Add helper validation." in rendered
    assert "Still visible for follow-up: code#101 Critical Bug." in rendered
    assert "Cleared or superseded paths: code#204 Close abandoned auth attempt." in rendered
    assert "No previous-window baseline was collected for this run." not in rendered
    assert "What decision would unblock code#101 Critical Bug today?" in rendered
    assert "Should failed runs in `code` Deploy production change release confidence" in rendered
    assert "Should Critical Bug stay active today, wait, or be reframed?" not in rendered

    assert "## Summary Table" not in rendered
    assert "## Repo Notes" not in rendered
    assert "## Supporting Signal" not in rendered
    assert "## Conversation Starters" not in rendered
    assert "## Decisions or Risks" not in rendered
    assert "## Source Notes" in rendered
    assert "Read-only mode" in rendered
    assert "technical depth" not in rendered
    assert "Automation needs attention because failed workflow runs were collected" not in rendered
    assert "https://github.com/example-org/code/pull/202" not in rendered
    assert "https://github.com/example-org/example-skills/pull/203" not in rendered


def test_render_executive_brief_uses_dynamic_recipient() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-06-01T16:00:00Z", "until": "2026-06-02T16:00:00Z", "label": "24h"},
        "timezone": "America/New_York",
        "report_recipient": "Example leader",
        "repositories": ["example-org/example-repo"],
        "layout": "executive",
        "buckets": {
            "needs_attention": [
                {
                    "repo": "example-org/example-repo",
                    "number": 1,
                    "title": "Decision needed",
                    "url": "https://github.com/example-org/example-repo/issues/1",
                    "kind": "issue",
                    "state": "open",
                }
            ]
        },
        "priority_sections": [{"name": "Every Code", "items": [], "recently_completed": []}],
        "limitations": [],
        "releases": [],
        "workflows": [],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")

    assert "# Daily Work Brief for Example leader" in rendered
    assert "## Needs Example leader's Attention" in rendered
    assert "What decision would unblock example-repo#1 Decision needed today?" in rendered
    assert "Justin" not in rendered


def test_render_executive_brief_preserves_workstream_identity_inside_portfolio_area() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-06-05T16:00:00Z", "until": "2026-06-12T16:00:00Z", "label": "7d"},
        "timezone": "America/New_York",
        "report_recipient": "Justin",
        "repositories": ["example-org/codex-lab"],
        "layout": "executive",
        "buckets": {
            "in_progress": [
                {
                    "repo": "example-org/codex-lab",
                    "number": 28,
                    "title": "Codex Lab MVP dogfood plan",
                    "kind": "issue",
                    "state": "open",
                },
                {
                    "repo": "example-org/codex-lab",
                    "number": 45,
                    "title": "Define Code Bridge protocol, trust, and payload contract",
                    "kind": "issue",
                    "state": "open",
                },
            ],
            "recently_completed": [
                {
                    "repo": "example-org/codex-lab",
                    "number": 52,
                    "title": "Run Codex Lab dogfood readiness sweep",
                    "kind": "issue",
                    "state": "closed",
                }
            ],
        },
        "priority_sections": [
            {
                "name": "Every Code Product Issues",
                "portfolio_area": "Every Code Product Issues",
                "workstream": "Codex Lab",
                "relationship": "Codex Lab dogfood work inside the Every Code product area",
                "initiatives": ["Code Bridge"],
                "items": [
                    {
                        "repo": "example-org/codex-lab",
                        "number": 28,
                        "title": "Codex Lab MVP dogfood plan",
                        "kind": "issue",
                        "state": "open",
                    },
                    {
                        "repo": "example-org/codex-lab",
                        "number": 45,
                        "title": "Define Code Bridge protocol, trust, and payload contract",
                        "kind": "issue",
                        "state": "open",
                    },
                ],
                "item_count": 2,
                "recently_completed": [
                    {
                        "repo": "example-org/codex-lab",
                        "number": 52,
                        "title": "Run Codex Lab dogfood readiness sweep",
                        "kind": "issue",
                        "state": "closed",
                    }
                ],
                "recently_completed_count": 1,
            }
        ],
        "limitations": [],
        "releases": [],
        "workflows": [],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")

    assert "The visible decision is how to sequence Codex Lab dogfood work inside the Every Code product area" in rendered
    assert "- **Codex Lab**: Codex Lab dogfood work inside the Every Code product area" in rendered
    assert "Key initiatives: Code Bridge." in rendered
    assert "Impact: turns finished work into a checkable result before the next cycle commits more effort." in rendered
    assert "Risk if delayed: the open items can fan out into parallel work before the decision loop closes." in rendered
    assert "Confidence: medium; based on 3 GitHub items across 1 initiative" in rendered
    assert "Every Code Product Issues MVP" not in rendered
    assert "Every Code product MVP" not in rendered


def test_render_executive_brief_infers_workstream_without_using_portfolio_name_as_alias() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-06-05T16:00:00Z", "until": "2026-06-12T16:00:00Z", "label": "7d"},
        "timezone": "America/New_York",
        "report_recipient": "Justin",
        "repositories": ["example-org/codex-lab"],
        "layout": "executive",
        "buckets": {
            "in_progress": [
                {
                    "repo": "example-org/codex-lab",
                    "number": 28,
                    "title": "Codex Lab MVP dogfood plan",
                    "kind": "issue",
                    "state": "open",
                }
            ],
            "recently_completed": [
                {
                    "repo": "example-org/codex-lab",
                    "number": 52,
                    "title": "Run Codex Lab dogfood readiness sweep",
                    "kind": "issue",
                    "state": "closed",
                }
            ],
        },
        "priority_sections": [
            {
                "name": "Every Code Product Issues",
                "items": [
                    {
                        "repo": "example-org/codex-lab",
                        "number": 28,
                        "title": "Codex Lab MVP dogfood plan",
                        "kind": "issue",
                        "state": "open",
                    }
                ],
                "item_count": 1,
                "recently_completed": [
                    {
                        "repo": "example-org/codex-lab",
                        "number": 52,
                        "title": "Run Codex Lab dogfood readiness sweep",
                        "kind": "issue",
                        "state": "closed",
                    }
                ],
                "recently_completed_count": 1,
            }
        ],
        "limitations": [],
        "releases": [],
        "workflows": [],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")

    assert "The visible decision is how to sequence Codex Lab work inside Every Code Product Issues" in rendered
    assert "- **Codex Lab**: Codex Lab work inside Every Code Product Issues" in rendered
    assert "- **Every Code Product Issues**" not in rendered


def test_render_executive_brief_infers_sentence_case_workstream_titles() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-06-05T16:00:00Z", "until": "2026-06-12T16:00:00Z", "label": "7d"},
        "timezone": "America/New_York",
        "report_recipient": "Justin",
        "repositories": ["example-org/codex-lab"],
        "layout": "executive",
        "buckets": {
            "in_progress": [
                {
                    "repo": "example-org/codex-lab",
                    "number": 28,
                    "title": "Codex lab dogfood plan",
                    "kind": "issue",
                    "state": "open",
                }
            ]
        },
        "priority_sections": [
            {
                "name": "Every Code Product Issues",
                "items": [
                    {
                        "repo": "example-org/codex-lab",
                        "number": 28,
                        "title": "Codex lab dogfood plan",
                        "kind": "issue",
                        "state": "open",
                    }
                ],
                "item_count": 1,
                "recently_completed": [],
            }
        ],
        "limitations": [],
        "releases": [],
        "workflows": [],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")

    assert "- **Codex lab**" in rendered
    assert "- **Codex**" not in rendered


def test_render_executive_brief_does_not_select_configured_initiative_as_workstream() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-06-05T16:00:00Z", "until": "2026-06-12T16:00:00Z", "label": "7d"},
        "timezone": "America/New_York",
        "report_recipient": "Justin",
        "repositories": ["example-org/codex-lab"],
        "layout": "executive",
        "buckets": {
            "in_progress": [
                {
                    "repo": "example-org/codex-lab",
                    "number": 45,
                    "title": "Code Bridge protocol for Codex Lab",
                    "kind": "issue",
                    "state": "open",
                }
            ]
        },
        "priority_sections": [
            {
                "name": "Every Code Product Issues",
                "initiatives": ["Code Bridge"],
                "items": [
                    {
                        "repo": "example-org/codex-lab",
                        "number": 45,
                        "title": "Code Bridge protocol for Codex Lab",
                        "kind": "issue",
                        "state": "open",
                    }
                ],
                "item_count": 1,
                "recently_completed": [],
            }
        ],
        "limitations": [],
        "releases": [],
        "workflows": [],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")

    assert "- **Codex Lab**: Codex Lab work inside Every Code Product Issues" in rendered
    assert "Key initiatives: Code Bridge." in rendered
    assert "- **Code Bridge**" not in rendered


def test_render_executive_brief_keeps_mixed_focus_heading_specific() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-06-05T16:00:00Z", "until": "2026-06-12T16:00:00Z", "label": "7d"},
        "timezone": "America/New_York",
        "report_recipient": "Justin",
        "repositories": ["example-org/codex-lab", "example-org/example-skills"],
        "layout": "executive",
        "buckets": {},
        "priority_sections": [
            {
                "name": "Every Code Product Issues",
                "workstream": "Codex Lab",
                "items": [],
                "recently_completed": [],
            },
            {
                "name": "Example Skill Updates",
                "workstream": "Work Brief skill",
                "items": [],
                "recently_completed": [],
            },
        ],
        "limitations": [],
        "releases": [],
        "workflows": [],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")

    assert "## Every Code Product and Skills Impact" in rendered
    assert "## Every Code Product Impact" not in rendered


def test_render_executive_brief_mixed_focus_heading_is_order_stable() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-06-05T16:00:00Z", "until": "2026-06-12T16:00:00Z", "label": "7d"},
        "timezone": "America/New_York",
        "report_recipient": "Justin",
        "repositories": ["example-org/codex-lab", "example-org/example-skills"],
        "layout": "executive",
        "buckets": {},
        "priority_sections": [
            {
                "name": "Example Skill Updates",
                "workstream": "Work Brief skill",
                "items": [],
                "recently_completed": [],
            },
            {
                "name": "Every Code Product Issues",
                "workstream": "Codex Lab",
                "items": [],
                "recently_completed": [],
            },
        ],
        "limitations": [],
        "releases": [],
        "workflows": [],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")

    assert "## Every Code Product and Skills Impact" in rendered
    assert "## Skills and Every Code Product Impact" not in rendered


def test_render_executive_brief_theme_titles_use_key_phrases_without_semicolon_soup() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-06-05T16:00:00Z", "until": "2026-06-12T16:00:00Z", "label": "7d"},
        "timezone": "America/New_York",
        "report_recipient": "Justin",
        "repositories": ["example-org/codex-lab"],
        "layout": "executive",
        "buckets": {
            "in_progress": [
                {
                    "repo": "example-org/codex-lab",
                    "number": 45,
                    "title": "Define Code Bridge protocol, trust, and payload contract",
                    "kind": "issue",
                    "state": "open",
                },
                {
                    "repo": "example-org/codex-lab",
                    "number": 28,
                    "title": "Codex Lab MVP dogfood plan",
                    "kind": "issue",
                    "state": "open",
                },
                {
                    "repo": "example-org/codex-lab",
                    "number": 63,
                    "title": "Scope owner review follow-up",
                    "kind": "issue",
                    "state": "open",
                },
            ]
        },
        "priority_sections": [],
        "limitations": [],
        "releases": [],
        "workflows": [],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")
    outcome = next(line for line in rendered.splitlines() if line.startswith("The useful signal is"))

    assert "Code Bridge, Codex Lab, and 1 more related item" in outcome
    assert ";" not in outcome
    assert "trust, and payload contract" not in outcome


def test_render_executive_brief_theme_titles_keep_title_fallbacks_per_item() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-06-05T16:00:00Z", "until": "2026-06-12T16:00:00Z", "label": "7d"},
        "timezone": "America/New_York",
        "report_recipient": "Justin",
        "repositories": ["example-org/codex-lab"],
        "layout": "executive",
        "buckets": {
            "in_progress": [
                {
                    "repo": "example-org/codex-lab",
                    "number": 11,
                    "title": "fix broken config",
                    "kind": "issue",
                    "state": "open",
                },
                {
                    "repo": "example-org/codex-lab",
                    "number": 45,
                    "title": "Define Code Bridge protocol",
                    "kind": "issue",
                    "state": "open",
                },
            ]
        },
        "priority_sections": [],
        "limitations": [],
        "releases": [],
        "workflows": [],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")
    outcome = next(line for line in rendered.splitlines() if line.startswith("The useful signal is"))

    assert "fix broken config and Code Bridge" in outcome
    assert "more related" not in outcome


def test_render_executive_brief_varies_impact_lines_by_workstream_signal() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-06-05T16:00:00Z", "until": "2026-06-12T16:00:00Z", "label": "7d"},
        "timezone": "America/New_York",
        "report_recipient": "Justin",
        "repositories": ["example-org/codex-lab", "example-org/example-skills"],
        "layout": "executive",
        "buckets": {},
        "priority_sections": [
            {
                "name": "Every Code Product Issues",
                "workstream": "Codex Lab",
                "initiatives": ["Code Bridge"],
                "items": [
                    {
                        "repo": "example-org/codex-lab",
                        "number": 28,
                        "title": "Codex Lab MVP dogfood plan",
                        "kind": "issue",
                        "state": "open",
                    }
                ],
                "item_count": 1,
                "recently_completed": [],
            },
            {
                "name": "Example Skill Updates",
                "workstream": "Work Brief skill",
                "items": [],
                "recently_completed": [
                    {
                        "repo": "example-org/example-skills",
                        "number": 203,
                        "title": "Add helper validation",
                        "kind": "pr",
                        "state": "merged",
                    }
                ],
                "recently_completed_count": 1,
            },
        ],
        "limitations": [],
        "releases": [],
        "workflows": [],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")
    bullets = [line for line in rendered.splitlines() if line.startswith("- **")]

    assert any("Impact: sequencing the open thread now keeps it from stalling neighboring priorities." in line for line in bullets)
    assert any("Impact: confirms the shipped change matches intent before attention moves on." in line for line in bullets)
    assert any("Confidence: medium; based on 1 GitHub item across 1 initiative" in line for line in bullets)
    assert len(set(bullets)) == len(bullets)


def test_render_executive_brief_thin_workstream_avoids_validation_claim() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-06-05T16:00:00Z", "until": "2026-06-12T16:00:00Z", "label": "7d"},
        "timezone": "America/New_York",
        "report_recipient": "Justin",
        "repositories": ["example-org/codex-lab"],
        "layout": "executive",
        "buckets": {},
        "priority_sections": [
            {
                "name": "Every Code Product Issues",
                "workstream": "Codex Lab",
                "items": [],
                "recently_completed": [],
            }
        ],
        "limitations": [],
        "releases": [],
        "workflows": [],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")
    bullet = next(line for line in rendered.splitlines() if line.startswith("- **Codex Lab**"))

    assert "no active signal" in bullet
    assert "concrete validation path" not in bullet
    assert "Impact:" not in bullet
    assert "Risk if delayed:" not in bullet
    assert "Confidence: low" in bullet


def test_render_executive_brief_surfaces_configured_repo_coverage_gap() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-06-01T16:00:00Z", "until": "2026-06-02T16:00:00Z", "label": "24h"},
        "timezone": "America/New_York",
        "report_recipient": "Justin",
        "repositories": ["example-org/example-repo", "example-org/missing"],
        "layout": "executive",
        "buckets": {
            "recently_completed": [
                {
                    "repo": "example-org/example-repo",
                    "number": 2,
                    "title": "Ship useful summary",
                    "kind": "pr",
                    "state": "merged",
                }
            ]
        },
        "priority_sections": [],
        "limitations": [],
        "coverage_gaps": ["example-org/missing"],
        "releases": [],
        "workflows": [],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")

    assert "Collection incomplete for `example-org/missing`; this brief may omit work from those configured sources." in rendered


def test_render_executive_brief_surfaces_coverage_gap_without_product_signal() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-06-01T16:00:00Z", "until": "2026-06-02T16:00:00Z", "label": "24h"},
        "timezone": "America/New_York",
        "report_recipient": "Justin",
        "repositories": ["example-org/missing"],
        "layout": "executive",
        "buckets": {},
        "priority_sections": [],
        "limitations": ["Could not collect issues for example-org/missing: API unavailable"],
        "coverage_gaps": ["example-org/missing"],
        "releases": [],
        "workflows": [],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")

    assert "No active work or material changes were collected" in rendered
    assert "Collection incomplete for `example-org/missing`; this brief may omit work from those configured sources." in rendered
    assert "Could not collect issues for example-org/missing: API unavailable" in rendered


def test_render_executive_brief_uses_weekly_window_language() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-05-26T16:00:00Z", "until": "2026-06-02T16:00:00Z", "label": "7d"},
        "timezone": "America/New_York",
        "report_recipient": "Justin",
        "recipient_profile": {"roles": ["owner"], "detail_preference": "outcomes and customer impact"},
        "repositories": ["example-org/example-repo"],
        "layout": "executive",
        "buckets": {
            "recently_completed": [
                {
                    "repo": "example-org/example-repo",
                    "number": 2,
                    "title": "Improve owner summaries",
                    "kind": "pr",
                    "state": "merged",
                }
            ],
            "in_progress": [
                {
                    "repo": "example-org/example-repo",
                    "number": 3,
                    "title": "Tune weekly report",
                    "kind": "issue",
                    "state": "open",
                }
            ],
        },
        "priority_sections": [{"name": "Every Code", "items": [], "recently_completed": []}],
        "limitations": [],
        "releases": [],
        "workflows": [],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")

    assert "# Weekly Work Brief for Justin" in rendered
    assert "The visible decision is how to sequence Every Code: 1 completed item and 1 open follow-up." in rendered
    assert "Looking at Improve owner summaries, did the completed work deliver what you expected" in rendered
    assert "Completed during the week: example-repo#2 Improve owner summaries." in rendered
    assert "Daily" not in rendered
    assert "focused day of work" not in rendered
    assert "tomorrow's brief" not in rendered
    assert "owner view" not in rendered


def test_render_executive_brief_uses_custom_window_language() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-05-31T16:00:00Z", "until": "2026-06-02T16:00:00Z", "label": "48h"},
        "timezone": "America/New_York",
        "report_recipient": "Justin",
        "repositories": ["example-org/example-repo"],
        "layout": "executive",
        "buckets": {
            "recently_completed": [
                {
                    "repo": "example-org/example-repo",
                    "number": 4,
                    "title": "Clarify report cadence",
                    "kind": "issue",
                    "state": "closed",
                }
            ]
        },
        "priority_sections": [],
        "limitations": [],
        "releases": [],
        "workflows": [],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")

    assert "# Current Work Brief for Justin" in rendered
    assert "Work was concentrated in `example-repo`: 1 completed item." in rendered
    assert "Looking at Clarify report cadence, did the completed work deliver what you expected" in rendered
    assert "Completed during the reporting window: example-repo#4 Clarify report cadence." in rendered
    assert "Daily" not in rendered
    assert "Weekly" not in rendered
    assert "this week" not in rendered
    assert "today" not in rendered.casefold()
    assert "tomorrow" not in rendered.casefold()


def test_render_executive_brief_does_not_treat_workflow_only_as_product_progress() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-06-01T16:00:00Z", "until": "2026-06-02T16:00:00Z", "label": "24h"},
        "timezone": "America/New_York",
        "report_recipient": "Justin",
        "repositories": ["example-org/example-repo"],
        "layout": "executive",
        "buckets": {},
        "priority_sections": [],
        "limitations": [],
        "releases": [],
        "workflows": [
            {"repo": "example-org/example-repo", "name": "CI", "status": "completed", "conclusion": "success"}
        ],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")

    assert "No product or planning movement was collected" in rendered
    assert "Automation looked healthy" not in rendered
    assert "## Failed Runs" in rendered
    assert "- None collected in this window." in rendered
    assert "## Work Items Behind This Brief" in rendered
    assert "advanced" not in rendered


def test_render_executive_brief_separates_finished_work_from_cleared_paths() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-06-01T16:00:00Z", "until": "2026-06-02T16:00:00Z", "label": "24h"},
        "timezone": "America/New_York",
        "report_recipient": "Justin",
        "repositories": ["example-org/example-repo"],
        "layout": "executive",
        "buckets": {
            "recently_completed": [
                {
                    "repo": "example-org/example-repo",
                    "number": 10,
                    "title": "Close abandoned auth attempt",
                    "kind": "pr",
                    "state": "closed",
                }
            ]
        },
        "priority_sections": [],
        "limitations": [],
        "releases": [],
        "workflows": [],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")

    assert "No product or planning movement was collected" in rendered
    assert "abandoned or superseded change paths were cleared in this window" in rendered
    assert "abandoned or superseded change paths were cleared in this window" in rendered
    assert "advanced" not in rendered
    assert "meaningful work moved forward" not in rendered
    assert "Cleared or superseded paths: example-repo#10 Close abandoned auth attempt." in rendered


def test_render_executive_brief_does_not_call_neutral_automation_green() -> None:
    payload = {
        "ok": True,
        "window": {"since": "2026-06-01T16:00:00Z", "until": "2026-06-02T16:00:00Z", "label": "24h"},
        "timezone": "America/New_York",
        "report_recipient": "Justin",
        "repositories": ["example-org/example-repo"],
        "layout": "executive",
        "buckets": {"recently_completed": []},
        "priority_sections": [],
        "limitations": [],
        "releases": [],
        "workflows": [
            {
                "repo": "example-org/example-repo",
                "name": "CodeQL",
                "status": "completed",
                "conclusion": "skipped",
                "completed_at": "2026-06-02T10:00:00Z",
            }
        ],
    }

    rendered = github_work_rollup.render_payload(payload, "markdown")

    assert "Automation ran, but the collected sample did not include a successful run." not in rendered
    assert "Automation was green" not in rendered
    assert "## Failed Runs" in rendered
    assert "- None collected in this window." in rendered


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
