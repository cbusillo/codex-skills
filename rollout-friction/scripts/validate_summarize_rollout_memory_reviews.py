#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused validation for summarize_rollout_memory_reviews.py."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType


SCRIPT = Path(__file__).with_name("summarize_rollout_memory_reviews.py")


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("summarize_rollout_memory_reviews", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load summarize_rollout_memory_reviews.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, payload: object) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload))


def valid_content(candidate_id: str = "memcand_a") -> dict[str, object]:
    return {
        "decisions": [{"candidate_id": candidate_id, "action": "note", "destination": "local_llm_notes"}],
        "people_updates": [],
        "profile_notes": [],
        "rollout_friction_notes": [],
        "local_llm_notes": [{"candidate_id": candidate_id, "note": "test"}],
        "repo_specific_notes": [],
        "discard_reasons": [],
        "reviewed_candidate_ids": [candidate_id],
    }


def test_summarizes_complete_review_dir() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_json(root / "batch-001.prompt.json", {"candidates": [{"candidate_id": "memcand_a"}]})
        write_json(
            root / "batch-001.result.json",
            {
                "content": json.dumps(
                    valid_content()
                )
            },
        )
        summary = module.summarize(root)
    if summary["ok_count"] != 1 or summary["failed_count"] != 0:
        raise AssertionError(f"unexpected summary status: {summary}")
    if summary["candidate_count"] != 1 or summary["covered_count"] != 1:
        raise AssertionError(f"unexpected coverage summary: {summary}")


def test_marks_missing_result_failed() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_json(root / "batch-001.prompt.json", {"candidates": [{"candidate_id": "memcand_a"}]})
        summary = module.summarize(root)
    if summary["failed_count"] != 1:
        raise AssertionError(f"missing result should fail: {summary}")


def test_accepts_split_child_batch_labels() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_json(root / "batch-006-a.prompt.json", {"candidates": [{"candidate_id": "memcand_a"}]})
        write_json(
            root / "batch-006-a.result.json",
            {
                "content": json.dumps(
                    valid_content()
                )
            },
        )
        summary = module.summarize(root)
    if summary["summaries"][0]["batch"] != "006-a":
        raise AssertionError(f"split child batch label should be preserved: {summary}")


def test_split_children_supersede_failed_parent() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_json(root / "batch-006.prompt.json", {"candidates": [{"candidate_id": "memcand_parent"}]})
        write_json(root / "batch-006.result.json", {"content": "not json"})
        write_json(root / "batch-006-a.prompt.json", {"candidates": [{"candidate_id": "memcand_a"}]})
        write_json(
            root / "batch-006-a.result.json",
            {
                "content": json.dumps(
                    valid_content()
                )
            },
        )
        summary = module.summarize(root)
    if summary["failed_count"] != 0 or summary["superseded_parent_count"] != 1:
        raise AssertionError(f"split child should supersede failed parent: {summary}")


def main() -> int:
    test_summarizes_complete_review_dir()
    test_marks_missing_result_failed()
    test_accepts_split_child_batch_labels()
    test_split_children_supersede_failed_parent()
    print("ok validate-summarize-rollout-memory-reviews")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
