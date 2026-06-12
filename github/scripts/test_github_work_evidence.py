#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML>=6.0.0",
#     "pytest>=8.0.0",
# ]
# ///
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("github-work-evidence.py")
MODULE_SPEC = importlib.util.spec_from_file_location("github_work_evidence", MODULE_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
github_work_evidence = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = github_work_evidence
MODULE_SPEC.loader.exec_module(github_work_evidence)


def completed(command: list[str], payload: object, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")


def args(**overrides: object) -> argparse.Namespace:
    values = {
        "config": Path(".local/github-work-evidence.yaml"),
        "repo": [],
        "repo_owner": [],
        "subject": [],
        "window": "24h",
        "since": None,
        "until": None,
        "timezone": None,
        "mode": None,
        "output": None,
        "limit_repos": None,
        "collection_limit_items": None,
        "release_collection_limit": None,
        "workflow_collection_limit": None,
        "include_bots": False,
        "include_external_activity": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_resolve_evidence_settings_uses_evidence_config() -> None:
    settings = github_work_evidence.resolve_evidence_settings(
        args(repo=["example-org/example-repo"]),
        {
            "mode": "standup",
            "priority_sections": [
                {"name": "Example", "repositories": ["example-org/example-repo"]},
            ],
        },
    )

    assert settings["mode"] == "standup"
    assert settings["priority_sections"] == [
        {"name": "Example", "repositories": ["example-org/example-repo"]},
    ]


def test_default_config_path_is_evidence_owned() -> None:
    assert github_work_evidence.DEFAULT_CONFIG.name == "github-work-evidence.yaml"
    assert github_work_evidence.collector.DEFAULT_CONFIG.name == "github-work-evidence.yaml"


def test_limit_repos_can_come_from_config() -> None:
    settings = github_work_evidence.resolve_evidence_settings(
        args(repo_owner=["example-org"]),
        {"limit_repos": 100},
    )

    assert settings["limit_repos"] == 100


def test_limit_repos_cli_overrides_config() -> None:
    settings = github_work_evidence.resolve_evidence_settings(
        args(repo_owner=["example-org"], limit_repos=7),
        {"limit_repos": 100},
    )

    assert settings["limit_repos"] == 7


def test_evidence_payload_preserves_facts() -> None:
    payload = {
        "ok": True,
        "generated_at": "2026-06-12T00:00:00Z",
        "window": {"label": "24h"},
        "display_window": {"label": "24h"},
        "timezone": "UTC",
        "repositories": ["example-org/example-repo"],
        "subjects": ["dev-a"],
        "mode": "activity",
        "collection_lanes": {"repositories": True},
        "preflight": {"ok": True},
        "summary": {"recently_completed": 2},
        "buckets": {
            "recently_completed": [
                {
                    "title": "Done",
                    "url": "https://example.invalid",
                    "handoff": "repo-readiness or github for a fresh merge decision",
                }
            ]
        },
        "priority_sections": [{"name": "Plan"}],
        "releases": [{"name": "v1"}],
        "workflows": [{"name": "CI", "conclusion": "success"}],
        "limitations": ["Read-only evidence."],
    }
    settings: dict[str, object] = {}

    evidence = github_work_evidence.evidence_payload(payload, settings)

    assert evidence["kind"] == "github_work_evidence"
    assert evidence["scope"]["repositories"] == ["example-org/example-repo"]
    assert evidence["summary"] == {"recently_completed": 2}
    assert evidence["buckets"]["recently_completed"][0]["title"] == "Done"
    assert "handoff" not in evidence["buckets"]["recently_completed"][0]
    assert evidence["source_notes"] == ["Read-only evidence."]


def test_evidence_payload_preserves_source_note_wording() -> None:
    evidence = github_work_evidence.evidence_payload(
        {
            "ok": True,
            "limitations": ["No subjects configured; evidence is repository-scoped only."],
        },
        {},
    )

    assert evidence["source_notes"] == ["No subjects configured; evidence is repository-scoped only."]


def test_collector_standup_keeps_backlog_and_recent_lanes(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr(github_work_evidence.collector, "run", fake_run)
    settings = github_work_evidence.resolve_evidence_settings(
        args(repo=["example-org/example-repo"], mode="standup", until="2026-06-02T16:00:00Z"),
        {},
    )

    rows = github_work_evidence.collector.collect_repo_items("example-org/example-repo", settings)

    issue_commands = [
        command
        for command in calls
        if command[1:3] == ["issue", "list"] and command[command.index("--state") + 1] == "open"
    ]
    assert ["--search" in command for command in issue_commands] == [False, True]
    assert {row["number"] for row in rows} == {50, 214}
    assert {row["number"]: row["collection_lane"] for row in rows} == {50: "recent_activity", 214: "open_backlog"}


def test_collector_collects_release_workflow_and_limit_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
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
                        "tagName": "v1.0.0",
                        "name": "First Release",
                        "createdAt": "2026-06-02T10:00:00Z",
                        "publishedAt": "2026-06-02T10:00:00Z",
                        "isDraft": False,
                        "isPrerelease": False,
                    }
                ],
            )
        if command[1:3] == ["run", "list"]:
            return completed(
                command,
                [
                    {
                        "name": "Deploy production",
                        "status": "completed",
                        "conclusion": "success",
                        "createdAt": "2026-06-02T10:00:00Z",
                        "updatedAt": "2026-06-02T11:00:00Z",
                        "url": "https://github.com/example-org/example-repo/actions/runs/1",
                    }
                ],
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(github_work_evidence.collector, "run", fake_run)
    settings = github_work_evidence.resolve_evidence_settings(
        args(
            repo=["example-org/example-repo"],
            mode="standup",
            until="2026-06-02T16:00:00Z",
            release_collection_limit=1,
            workflow_collection_limit=1,
        ),
        {},
    )

    payload = github_work_evidence.collector.collect_work_evidence(settings)
    evidence = github_work_evidence.evidence_payload(payload, settings)

    assert evidence["releases"] == [
        {
            "name": "First Release",
            "published_at": "2026-06-02T10:00:00Z",
            "repo": "example-org/example-repo",
            "tag_name": "v1.0.0",
            "url": "https://github.com/example-org/example-repo/releases/tag/v1.0.0",
        }
    ]
    assert evidence["workflows"] == [
        {
            "completed_at": "2026-06-02T11:00:00Z",
            "conclusion": "success",
            "created_at": "2026-06-02T10:00:00Z",
            "name": "Deploy production",
            "repo": "example-org/example-repo",
            "status": "completed",
            "url": "https://github.com/example-org/example-repo/actions/runs/1",
        }
    ]
    assert "Release collection for example-org/example-repo reached 1; release counts may be incomplete." in evidence["source_notes"]
    assert "Workflow collection for example-org/example-repo reached 1; automation counts may be incomplete." in evidence["source_notes"]
    assert any(command[1:3] == ["release", "list"] for command in calls)
    assert any(command[1:3] == ["run", "list"] for command in calls)


def test_evidence_payload_strips_handoff_recursively() -> None:
    evidence = github_work_evidence.evidence_payload(
        {
            "ok": True,
            "limitations": [],
            "buckets": {
                "ready_for_review": [
                    {"title": "Review", "nested": {"handoff": "legacy prose", "keep": "fact"}}
                ]
            },
        },
        {},
    )

    assert evidence["buckets"]["ready_for_review"] == [{"title": "Review", "nested": {"keep": "fact"}}]


def test_evidence_failure_shape_is_json_only() -> None:
    evidence = github_work_evidence.evidence_failure(
        {
            "generated_at": "2026-06-12T00:00:00Z",
            "window": {"label": "24h"},
            "display_window": {"label": "24h"},
            "timezone": "UTC",
            "error": "No repositories or subjects configured.",
            "next_step": "Pass --repo.",
        }
    )

    assert evidence == {
        "ok": False,
        "schema_version": 1,
        "kind": "github_work_evidence",
        "generated_at": "2026-06-12T00:00:00Z",
        "window": {"label": "24h"},
        "display_window": {"label": "24h"},
        "timezone": "UTC",
        "error": "No repositories or subjects configured.",
        "next_step": "Pass --repo.",
        "source_notes": ["Evidence collection failed before a complete source snapshot could be built."],
    }


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__]))
