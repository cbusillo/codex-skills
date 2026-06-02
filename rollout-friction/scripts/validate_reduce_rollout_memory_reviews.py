#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused validation for reduce_rollout_memory_reviews.py."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType


SCRIPT = Path(__file__).with_name("reduce_rollout_memory_reviews.py")


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("reduce_rollout_memory_reviews", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load reduce_rollout_memory_reviews.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, payload: object) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload))


def review_content() -> dict[str, object]:
    return {
        "decisions": [
            {"candidate_id": "memcand_a", "action": "note", "destination": "profile_notes"},
            {"candidate_id": "memcand_b", "action": "note", "destination": "profile_notes"},
            {"candidate_id": "memcand_c", "action": "discard", "destination": "discard_reasons"},
        ],
        "people_updates": [],
        "profile_notes": [
            {"candidate_id": "memcand_a", "note": "Prefer validated memory reviews before applying facts."},
            {"candidate_id": "memcand_b", "note": "Prefer validated memory reviews before applying facts."},
        ],
        "rollout_friction_notes": [],
        "local_llm_notes": [],
        "repo_specific_notes": [],
        "discard_reasons": [{"candidate_id": "memcand_c", "reason": "transient"}],
        "reviewed_candidate_ids": ["memcand_a", "memcand_b", "memcand_c"],
    }


def test_reduce_validated_reviews_to_apply_plan() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_json(
            root / "batch-001.prompt.json",
            {"candidates": [{"candidate_id": "memcand_a"}, {"candidate_id": "memcand_b"}, {"candidate_id": "memcand_c"}]},
        )
        write_json(root / "batch-001.result.json", {"content": json.dumps(review_content())})
        plan = module.reduce_reviews(root)
    profile = plan["destinations"]["profile_notes"]
    if profile["count"] != 1:
        raise AssertionError(f"duplicate profile notes should be deduped: {profile}")
    update = profile["updates"][0]
    if update["candidate_ids"] != ["memcand_a", "memcand_b"]:
        raise AssertionError(f"candidate ids should merge during dedupe: {update}")
    if plan["discard_count"] != 1 or plan["failed_batch_count"] != 0:
        raise AssertionError(f"unexpected reduce summary: {plan}")


def test_reduce_reports_failed_batches() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_json(root / "batch-001.prompt.json", {"candidates": [{"candidate_id": "memcand_a"}]})
        plan = module.reduce_reviews(root)
        plan_with_failed = module.reduce_reviews(root, include_failed=True)
    if plan["failed_batch_count"] != 1:
        raise AssertionError(f"missing result should be reported as failed: {plan}")
    if plan["failed_batches"]:
        raise AssertionError(f"failed details should be hidden by default: {plan}")
    if len(plan_with_failed["failed_batches"]) != 1:
        raise AssertionError(f"failed details should be included when requested: {plan_with_failed}")


def test_reduce_ignores_failed_parent_with_valid_child() -> None:
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
                    {
                        "decisions": [
                            {"candidate_id": "memcand_a", "action": "note", "destination": "profile_notes"}
                        ],
                        "people_updates": [],
                        "profile_notes": [{"candidate_id": "memcand_a", "note": "Keep split child output."}],
                        "rollout_friction_notes": [],
                        "local_llm_notes": [],
                        "repo_specific_notes": [],
                        "discard_reasons": [],
                        "reviewed_candidate_ids": ["memcand_a"],
                    }
                )
            },
        )
        plan = module.reduce_reviews(root)
    if plan["failed_batch_count"] != 0:
        raise AssertionError(f"failed parent should be ignored when child exists: {plan}")
    if plan["destinations"]["profile_notes"]["count"] != 1:
        raise AssertionError(f"valid child note should be reduced: {plan}")


def main() -> int:
    test_reduce_validated_reviews_to_apply_plan()
    test_reduce_reports_failed_batches()
    test_reduce_ignores_failed_parent_with_valid_child()
    print("ok validate-reduce-rollout-memory-reviews")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
