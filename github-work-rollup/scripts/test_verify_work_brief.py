#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest>=8.0.0",
# ]
# ///
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("verify_work_brief.py")
MODULE_SPEC = importlib.util.spec_from_file_location("verify_work_brief", MODULE_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
verify_work_brief = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = verify_work_brief
MODULE_SPEC.loader.exec_module(verify_work_brief)


def evidence() -> dict[str, object]:
    return {
        "kind": "github_work_evidence",
        "source_notes": ["Workflow collection reached the configured cap; workflow counts may be incomplete."],
        "buckets": {
            "needs_review": [
                {
                    "repo": "example-org/example-repo",
                    "number": 42,
                    "title": "Add brief verifier",
                    "url": "https://github.com/example-org/example-repo/pull/42",
                }
            ]
        },
    }


def ambiguous_short_name_evidence() -> dict[str, object]:
    return {
        "kind": "github_work_evidence",
        "source_notes": ["Read-only evidence."],
        "buckets": {
            "ready_for_review": [
                {
                    "repo": "example-org/app",
                    "number": 7,
                    "url": "https://github.com/example-org/app/pull/7",
                },
                {
                    "repo": "other-org/app",
                    "number": 8,
                    "url": "https://github.com/other-org/app/pull/8",
                },
            ]
        },
    }


def ambiguous_bare_ref_evidence() -> dict[str, object]:
    return {
        "kind": "github_work_evidence",
        "source_notes": ["Read-only evidence."],
        "buckets": {
            "ready_for_review": [
                {
                    "repo": "example-org/app-one",
                    "number": 7,
                    "url": "https://github.com/example-org/app-one/pull/7",
                },
                {
                    "repo": "other-org/app-two",
                    "number": 7,
                    "url": "https://github.com/other-org/app-two/pull/7",
                },
            ]
        },
    }


def test_accepts_grounded_links_refs_and_source_caveat() -> None:
    brief = """
The review queue now centers on example-org/example-repo#42
(https://github.com/example-org/example-repo/pull/42).

Confidence caveat: automation data is partial, so workflow counts may be incomplete.
"""

    assert verify_work_brief.verify_brief(evidence(), brief) == []


def test_rejects_unsupported_url() -> None:
    errors = verify_work_brief.verify_brief(
        evidence(),
        "Source limitation: workflow collection reached the configured cap. See https://github.com/example-org/example-repo/pull/99.",
    )

    assert errors == [
        "unsupported URL not present in evidence: https://github.com/example-org/example-repo/pull/99"
    ]


def test_rejects_unsupported_issue_reference() -> None:
    errors = verify_work_brief.verify_brief(
        evidence(),
        "example-org/example-repo#99 is ready. Source limitation: workflow collection reached the configured cap.",
    )

    assert "unsupported issue/PR reference not present in evidence: example-org/example-repo#99" in errors
    assert "unsupported issue/PR reference not present in evidence: #99" not in errors


def test_accepts_grounded_short_repository_reference() -> None:
    errors = verify_work_brief.verify_brief(
        evidence(),
        "example-repo#42 is the next review item. Confidence caveat: workflow counts may be incomplete.",
    )

    assert errors == []


def test_rejects_unsupported_short_repository_reference() -> None:
    errors = verify_work_brief.verify_brief(
        evidence(),
        "example-repo#99 is ready. Source limitation: workflow collection reached the configured cap.",
    )

    assert "unsupported issue/PR reference not present in evidence: example-repo#99" in errors
    assert "unsupported issue/PR reference not present in evidence: #99" not in errors


def test_rejects_ambiguous_short_repository_reference() -> None:
    errors = verify_work_brief.verify_brief(
        ambiguous_short_name_evidence(),
        "app#7 is the next review item. Source limitation: Read-only evidence.",
    )

    assert "ambiguous short repository reference; use owner/repo form: app#7" in errors


def test_accepts_qualified_reference_when_short_repository_name_is_ambiguous() -> None:
    errors = verify_work_brief.verify_brief(
        ambiguous_short_name_evidence(),
        "example-org/app#7 is the next review item. Source limitation: Read-only evidence.",
    )

    assert errors == []


def test_rejects_unsupported_natural_language_issue_reference() -> None:
    errors = verify_work_brief.verify_brief(
        evidence(),
        "PR 99 is ready. Confidence caveat: automation data is partial.",
    )

    assert "unsupported issue/PR reference not present in evidence: 99" in errors


def test_ignores_workflow_run_natural_language_reference() -> None:
    workflow_evidence = evidence()
    workflow_evidence["workflow_runs"] = [
        {
            "repo": "example-org/example-repo",
            "databaseId": 123456,
            "url": "https://github.com/example-org/example-repo/actions/runs/123456",
        }
    ]

    errors = verify_work_brief.verify_brief(
        workflow_evidence,
        "Workflow run #123456 failed. Confidence caveat: workflow counts may be incomplete.",
    )

    assert errors == []


def test_rejects_unsupported_workflow_run_natural_language_reference() -> None:
    errors = verify_work_brief.verify_brief(
        evidence(),
        "Workflow run #123456 failed. Confidence caveat: workflow counts may be incomplete.",
    )

    assert "unsupported issue/PR reference not present in evidence: #123456" in errors


def test_ignores_actions_run_markdown_link_label() -> None:
    workflow_evidence = evidence()
    workflow_evidence["workflow_runs"] = [
        {
            "repo": "example-org/example-repo",
            "databaseId": 123456,
            "url": "https://github.com/example-org/example-repo/actions/runs/123456",
        }
    ]

    errors = verify_work_brief.verify_brief(
        workflow_evidence,
        "Validation run [#123456](https://github.com/example-org/example-repo/actions/runs/123456) failed. Confidence caveat: workflow counts may be incomplete.",
    )

    assert errors == []


def test_rejects_ambiguous_bare_issue_reference() -> None:
    errors = verify_work_brief.verify_brief(
        ambiguous_bare_ref_evidence(),
        "#7 is ready. Source limitation: Read-only evidence.",
    )

    assert "ambiguous bare issue/PR reference; use owner/repo form: #7" in errors


def test_rejects_ambiguous_natural_language_issue_reference() -> None:
    errors = verify_work_brief.verify_brief(
        ambiguous_bare_ref_evidence(),
        "PR 7 is ready. Source limitation: Read-only evidence.",
    )

    assert "ambiguous bare issue/PR reference; use owner/repo form: 7" in errors


def test_accepts_qualified_reference_when_bare_reference_is_ambiguous() -> None:
    errors = verify_work_brief.verify_brief(
        ambiguous_bare_ref_evidence(),
        "example-org/app-one#7 is ready. Source limitation: Read-only evidence.",
    )

    assert errors == []


def test_accepts_plan_context_refs() -> None:
    plan_context = [
        {
            "issue": {
                "repo": "example-org/example-repo",
                "number": 99,
                "url": "https://github.com/example-org/example-repo/issues/99",
            }
        }
    ]

    errors = verify_work_brief.verify_brief(
        evidence(),
        "Issue 99 frames the plan. See https://github.com/example-org/example-repo/issues/99. Confidence caveat: workflow counts may be incomplete.",
        plan_context=plan_context,
    )

    assert errors == []


def test_requires_source_note_reflection() -> None:
    errors = verify_work_brief.verify_brief(
        evidence(),
        "example-org/example-repo#42 is the next review item.",
    )

    assert errors == ["brief must include a source limitation or confidence caveat"]


def test_rejects_generic_caveat_without_source_note_content() -> None:
    errors = verify_work_brief.verify_brief(
        evidence(),
        "example-org/example-repo#42 is the next review item. Confidence is high.",
    )

    assert errors == [
        "brief must reflect source note: Workflow collection reached the configured cap; workflow counts may be incomplete."
    ]


def test_rejects_contradicted_source_note() -> None:
    errors = verify_work_brief.verify_brief(
        evidence(),
        "example-org/example-repo#42 is next. There are no source limitations.",
    )

    assert errors == ["brief contradicts evidence source limitations"]


def test_accepts_grouped_repetitive_source_notes() -> None:
    grouped_evidence = {
        "kind": "github_work_evidence",
        "source_notes": [
            "Issues are disabled for example-org/archive-one.",
            "Issues are disabled for example-org/archive-two.",
            "Workflow collection for example-org/app-one reached 1000; automation counts may be incomplete.",
            "Workflow collection for example-org/app-two reached 1000; automation counts may be incomplete.",
        ],
    }

    errors = verify_work_brief.verify_brief(
        grouped_evidence,
        "Source confidence: issues are disabled in two archived repos, and workflow counts are capped so automation counts may be incomplete.",
    )

    assert errors == []


def test_accepts_repository_only_scope_for_no_subjects_source_note() -> None:
    scoped_evidence = {
        "kind": "github_work_evidence",
        "limitations": ["No subjects configured; rollup is repository-scoped only."],
    }

    errors = verify_work_brief.verify_brief(
        scoped_evidence,
        "Confidence caveat: Scope is example-org/example-repo repository only; no person-specific subject signal is included.",
    )

    assert errors == []


def test_rejects_missing_short_source_note() -> None:
    short_note_evidence = {
        "kind": "github_work_evidence",
        "limitations": ["API down"],
    }

    errors = verify_work_brief.verify_brief(
        short_note_evidence,
        "Source caveat: workflow data may be partial.",
    )

    assert errors == ["brief must reflect source note: API down"]


def test_accepts_reflected_short_source_note() -> None:
    short_note_evidence = {
        "kind": "github_work_evidence",
        "limitations": ["API down"],
    }

    errors = verify_work_brief.verify_brief(
        short_note_evidence,
        "Source caveat: API down, so workflow data may be partial.",
    )

    assert errors == []


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__]))
