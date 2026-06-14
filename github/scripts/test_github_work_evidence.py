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
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("github-work-evidence.py")
MODULE_SPEC = importlib.util.spec_from_file_location("github_work_evidence", MODULE_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
github_work_evidence = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = github_work_evidence
MODULE_SPEC.loader.exec_module(github_work_evidence)


def args(**overrides: object) -> argparse.Namespace:
    values = {
        "config": Path("missing.yaml"),
        "repo": [],
        "repo_owner": [],
        "subject": [],
        "window": "24h",
        "since": None,
        "until": None,
        "timezone": None,
        "mode": None,
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
    values.update(overrides)
    return argparse.Namespace(**values)


def test_resolve_evidence_settings_ignores_audience_config() -> None:
    settings = github_work_evidence.resolve_evidence_settings(
        args(repo=["example-org/example-repo"]),
        {
            "layout": "executive",
            "summary_level": "detailed",
            "report_recipient": "Chris",
            "people_index": ".local/people.yaml",
            "output_path": ".local/latest.md",
            "mode": "standup",
        },
    )

    assert settings["layout"] == "operator"
    assert settings["summary_level"] == "standard"
    assert settings["report_recipient"] == "GitHub evidence"
    assert settings["people_index"].endswith(".local/github-work-evidence.no-people.yaml")
    assert settings["mode"] == "standup"
    assert settings["include_derived_context"] is False
    assert settings["evidence_ignored_config_keys"] == [
        "layout",
        "output_path",
        "people_index",
        "report_recipient",
        "summary_level",
    ]


def test_evidence_payload_removes_audience_fields_and_preserves_facts() -> None:
    payload = {
        "ok": True,
        "generated_at": "2026-06-12T00:00:00Z",
        "window": {"label": "24h"},
        "display_window": {"label": "24h"},
        "timezone": "UTC",
        "report_recipient": "Chris",
        "recipient_profile": {"display_name": "Chris", "private_notes": "secret"},
        "repositories": ["example-org/example-repo"],
        "subjects": ["dev-a"],
        "mode": "activity",
        "layout": "executive",
        "summary_level": "detailed",
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
        "derived_context": {
            "schema_version": 1,
            "repositories": [{"repo": "example-org/example-repo", "description": "Agent harness."}],
        },
        "releases": [{"name": "v1"}],
        "workflows": [{"name": "CI", "conclusion": "success"}],
        "limitations": ["Read-only evidence."],
    }
    settings = {"evidence_ignored_config_keys": ["layout", "report_recipient"]}

    evidence = github_work_evidence.evidence_payload(payload, settings)

    assert evidence["kind"] == "github_work_evidence"
    assert evidence["scope"]["repositories"] == ["example-org/example-repo"]
    assert evidence["summary"] == {"recently_completed": 2}
    assert evidence["buckets"]["recently_completed"][0]["title"] == "Done"
    assert evidence["derived_context"]["repositories"][0]["description"] == "Agent harness."
    assert "handoff" not in evidence["buckets"]["recently_completed"][0]
    assert evidence["source_notes"] == [
        "Read-only evidence.",
        "Ignored audience/report-rendering config keys for evidence-only output: layout, report_recipient.",
    ]
    assert "report_recipient" not in evidence
    assert "recipient_profile" not in evidence
    assert "layout" not in evidence
    assert "summary_level" not in evidence


def test_resolve_evidence_settings_can_request_derived_context() -> None:
    settings = github_work_evidence.resolve_evidence_settings(
        args(repo=["example-org/example-repo"], include_derived_context=True, context_repo_limit=3),
        {},
    )

    assert settings["include_derived_context"] is True
    assert settings["context_repo_limit"] == 3


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
