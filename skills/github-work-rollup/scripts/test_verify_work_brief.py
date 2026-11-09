#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.2.0",
# ]
# ///
from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

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


@pytest.mark.parametrize(
    "source,brief,accepted",
    [
        pytest.param(
            evidence,
            "\nThe review queue now centers on example-org/example-repo#42\n(https://github.com/example-org/example-repo/pull/42).\n\nConfidence caveat: automation data is partial, so workflow counts may be incomplete.\n",
            True,
            id="accepts_grounded_links_refs_and_source_caveat",
        ),
        pytest.param(
            evidence,
            "Source limitation: workflow collection reached the configured cap. See https://github.com/example-org/example-repo/pull/99.",
            False,
            id="rejects_unsupported_url",
        ),
        pytest.param(
            evidence,
            "example-org/example-repo#99 is ready. Source limitation: workflow collection reached the configured cap.",
            False,
            id="rejects_unsupported_issue_reference",
        ),
        pytest.param(
            evidence,
            "example-repo#42 is the next review item. Confidence caveat: workflow counts may be incomplete.",
            True,
            id="accepts_grounded_short_repository_reference",
        ),
        pytest.param(
            evidence,
            "example-repo#99 is ready. Source limitation: workflow collection reached the configured cap.",
            False,
            id="rejects_unsupported_short_repository_reference",
        ),
        pytest.param(
            ambiguous_short_name_evidence,
            "app#7 is the next review item. Source limitation: Read-only evidence.",
            False,
            id="rejects_ambiguous_short_repository_reference",
        ),
        pytest.param(
            ambiguous_short_name_evidence,
            "example-org/app#7 is the next review item. Source limitation: Read-only evidence.",
            True,
            id="accepts_qualified_reference_when_short_repository_name_is_ambiguous",
        ),
        pytest.param(
            evidence,
            "PR 99 is ready. Confidence caveat: workflow counts may be incomplete.",
            False,
            id="rejects_unsupported_natural_language_issue_reference",
        ),
        pytest.param(
            evidence,
            "Workflow run #123456 failed. Confidence caveat: workflow counts may be incomplete.",
            False,
            id="rejects_unsupported_workflow_run_natural_language_reference",
        ),
        pytest.param(
            ambiguous_bare_ref_evidence,
            "#7 is ready. Source limitation: Read-only evidence.",
            False,
            id="rejects_ambiguous_bare_issue_reference",
        ),
        pytest.param(
            ambiguous_bare_ref_evidence,
            "PR 7 is ready. Source limitation: Read-only evidence.",
            False,
            id="rejects_ambiguous_natural_language_issue_reference",
        ),
        pytest.param(
            ambiguous_bare_ref_evidence,
            "example-org/app-one#7 is ready. Source limitation: Read-only evidence.",
            True,
            id="accepts_qualified_reference_when_bare_reference_is_ambiguous",
        ),
        pytest.param(evidence, "example-org/example-repo#42 is the next review item.", False, id="requires_source_note_reflection"),
        pytest.param(
            evidence,
            "example-org/example-repo#42 is the next review item. Confidence is high.",
            False,
            id="rejects_generic_caveat_without_source_note_content",
        ),
        pytest.param(
            evidence,
            "example-org/example-repo#42 is the next review item. Automation totals are capped and workflow counts may be incomplete.",
            True,
            id="accepts_paraphrased_source_note_without_marker_words",
        ),
        pytest.param(
            evidence,
            "example-org/example-repo#42 is next. Workflow counts may be incomplete, but there are no source limitations.",
            False,
            id="rejects_contradicted_source_note",
        ),
    ],
)
def test_reference_and_caveat_validation(source: Callable[[], dict[str, Any]], brief: str, accepted: bool) -> None:
    assert len(verify_work_brief.verify_brief(source(), brief)) == (0 if accepted else 1)


def test_rejects_bare_ref_from_unrelated_numeric_metadata() -> None:
    metadata_evidence = evidence()
    metadata_evidence["workflow_runs"] = [
        {
            "repo": "example-org/example-repo",
            "number": 123,
            "databaseId": 456,
            "url": "https://github.com/example-org/example-repo/actions/runs/456",
        }
    ]

    errors = verify_work_brief.verify_brief(
        metadata_evidence,
        "#123 is ready. Workflow counts may be incomplete.",
    )

    assert len(errors) == 1 and "#123" in errors[0]


def test_derived_context_counts_do_not_authorize_bare_refs() -> None:
    context_evidence = evidence()
    context_evidence["derived_context"] = {
        "repositories": [
            {
                "repo": "example-org/example-repo",
                "open_count": 42,
                "completed_count": 2,
                "url": "https://github.com/example-org/example-repo",
            }
        ]
    }

    errors = verify_work_brief.verify_brief(
        context_evidence,
        "#2 should be discussed. Workflow counts may be incomplete.",
    )

    assert len(errors) == 1 and "#2" in errors[0]


@pytest.mark.parametrize("brief", [
    "Workflow run #123456 failed. Confidence caveat: workflow counts may be incomplete.",
    "Validation run [#123456](https://github.com/example-org/example-repo/actions/runs/123456) failed. Confidence caveat: workflow counts may be incomplete.",
], ids=["natural-language", "markdown-label"])
def test_grounded_workflow_run_is_not_an_issue_reference(brief: str) -> None:
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
        brief,
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

    assert len(errors) == 1


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
    raise SystemExit(pytest.main([__file__]))
