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


def test_accepts_grounded_links_refs_and_source_note() -> None:
    brief = """
The review queue now centers on example-org/example-repo#42
(https://github.com/example-org/example-repo/pull/42).

Source limitation: workflow collection reached the configured cap, so workflow
counts may be incomplete.
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


def test_requires_source_note_reflection() -> None:
    errors = verify_work_brief.verify_brief(
        evidence(),
        "example-org/example-repo#42 is the next review item.",
    )

    assert errors == ["brief must include a source limitation or confidence caveat"]


def test_rejects_source_note_marker_without_matching_content() -> None:
    errors = verify_work_brief.verify_brief(
        evidence(),
        "example-org/example-repo#42 is next. Source limitation: release data was unavailable.",
    )

    assert errors == [
        "source note not reflected in brief: Workflow collection reached the configured cap; workflow counts may be incomplete."
    ]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__]))
