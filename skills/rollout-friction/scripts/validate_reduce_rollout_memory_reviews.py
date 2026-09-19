#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused validation for reduce_rollout_memory_reviews.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
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
    shortlist = plan["curated_shortlist"]
    if shortlist["count"] < 1 or shortlist["updates"][0]["support_count"] != 2:
        raise AssertionError(f"shortlist should prefer supported deduped notes: {shortlist}")


def test_curated_shortlist_penalizes_stale_review_history() -> None:
    module = load_module()
    reduced = {
        "profile_notes": [
            module.annotate_note(
                {
                    "id": "keep",
                    "bucket": "profile_notes",
                    "text": "User prefers local-only review for private memory evidence.",
                    "candidate_ids": ["memcand_keep"],
                    "source_batches": ["001"],
                }
            ),
            module.annotate_note(
                {
                    "id": "stale",
                    "bucket": "profile_notes",
                    "text": "PR #123 merged with green checks after review identified a bug.",
                    "candidate_ids": ["memcand_stale"],
                    "source_batches": ["002"],
                }
            ),
        ]
    }
    shortlist = module.curate_shortlist(reduced, 1)
    if shortlist[0]["id"] != "keep" or shortlist[0]["stale_or_transient"]:
        raise AssertionError(f"shortlist should prefer durable facts over stale review history: {shortlist}")


def test_curated_shortlist_suppresses_near_duplicate_topics() -> None:
    module = load_module()
    reduced = {
        "profile_notes": [
            module.annotate_note(
                {
                    "id": "taxonomy-a",
                    "bucket": "profile_notes",
                    "text": "Canonical taxonomy: product Every Code; command is code; CODE_HOME is primary.",
                    "candidate_ids": ["memcand_a"],
                    "source_batches": ["001"],
                }
            ),
            module.annotate_note(
                {
                    "id": "taxonomy-b",
                    "bucket": "profile_notes",
                    "text": "Canonical taxonomy says Every Code is the product, code is the command, and CODE_HOME is primary.",
                    "candidate_ids": ["memcand_b"],
                    "source_batches": ["002"],
                }
            ),
        ]
    }
    shortlist = module.curate_shortlist(reduced, 5)
    if len(shortlist) != 1:
        raise AssertionError(f"near-duplicate shortlist topics should be collapsed: {shortlist}")


def test_people_updates_preserve_resolver_smoke_queries() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_json(root / "batch-001.prompt.json", {"candidates": [{"candidate_id": "memcand_person"}]})
        write_json(
            root / "batch-001.result.json",
            {
                "content": json.dumps(
                    {
                        "decisions": [
                            {
                                "candidate_id": "memcand_person",
                                "action": "note",
                                "destination": "people_updates",
                            }
                        ],
                        "people_updates": [
                            {
                                "candidate_id": "memcand_person",
                                "name": "Mike Banks",
                                "github_handle": "@Mbanks89",
                                "aliases": ["Mike", "Michael", "Micheal"],
                                "role": "planning-manager",
                                "note": "Mike owns OPW/SYO planning; GitHub handle is @Mbanks89.",
                            }
                        ],
                        "profile_notes": [],
                        "rollout_friction_notes": [],
                        "local_llm_notes": [],
                        "repo_specific_notes": [],
                        "discard_reasons": [],
                        "reviewed_candidate_ids": ["memcand_person"],
                    }
                )
            },
        )
        plan = module.reduce_reviews(root)
    people_update = plan["destinations"]["people_updates"]["updates"][0]
    queries = people_update.get("resolver_queries")
    expected = ["@Mbanks89", "Mbanks89", "Michael", "Micheal", "Mike", "Mike Banks"]
    if queries != expected:
        raise AssertionError(f"people update should preserve natural resolver queries: {queries}")
    smoke_queries = [item["query"] for item in plan["people_resolver_smoke_checks"]]
    if smoke_queries != expected:
        raise AssertionError(f"smoke checks should include deduped resolver queries: {smoke_queries}")


def test_people_dedupe_keeps_same_note_different_people_separate() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_json(
            root / "batch-001.prompt.json",
            {"candidates": [{"candidate_id": "memcand_alice"}, {"candidate_id": "memcand_bob"}]},
        )
        write_json(
            root / "batch-001.result.json",
            {
                "content": json.dumps(
                    {
                        "decisions": [
                            {
                                "candidate_id": "memcand_alice",
                                "action": "note",
                                "destination": "people_updates",
                            },
                            {
                                "candidate_id": "memcand_bob",
                                "action": "note",
                                "destination": "people_updates",
                            },
                        ],
                        "people_updates": [
                            {
                                "candidate_id": "memcand_alice",
                                "name": "Alice Example",
                                "github_handle": "@alice-example",
                                "note": "Planning manager.",
                            },
                            {
                                "candidate_id": "memcand_bob",
                                "name": "Bob Example",
                                "github_handle": "@bob-example",
                                "note": "Planning manager.",
                            },
                        ],
                        "profile_notes": [],
                        "rollout_friction_notes": [],
                        "local_llm_notes": [],
                        "repo_specific_notes": [],
                        "discard_reasons": [],
                        "reviewed_candidate_ids": ["memcand_alice", "memcand_bob"],
                    }
                )
            },
        )
        plan = module.reduce_reviews(root)
    people = plan["destinations"]["people_updates"]
    if people["count"] != 2:
        raise AssertionError(f"same-note different people updates should stay separate: {people}")


def test_people_query_extraction_splits_separators_and_filters_placeholders() -> None:
    module = load_module()
    queries = module.people_queries_from_note(
        {
            "candidate_id": "memcand_person",
            "aliases": ["Kyle/HonkHonk", "Alice | Bob", "unknown", "not provided"],
            "github_handle": "null",
            "note": "Known people aliases.",
        },
        "Known people aliases.",
    )
    expected = ["Alice", "Bob", "HonkHonk", "Kyle"]
    if queries != expected:
        raise AssertionError(f"unexpected people queries: {queries}")


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


def test_cli_can_emit_shortlist_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_json(
            root / "batch-001.prompt.json",
            {"candidates": [{"candidate_id": "memcand_a"}, {"candidate_id": "memcand_b"}, {"candidate_id": "memcand_c"}]},
        )
        write_json(root / "batch-001.result.json", {"content": json.dumps(review_content())})
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(root), "--format", "shortlist"],
            check=True,
            capture_output=True,
            text=True,
        )
    payload = json.loads(result.stdout)
    if "updates" not in payload or "destinations" in payload:
        raise AssertionError(f"shortlist format should emit curated shortlist only: {payload}")


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


def test_reduce_ignores_missing_parent_result_with_valid_child() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_json(root / "batch-006.prompt.json", {"candidates": [{"candidate_id": "memcand_parent"}]})
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
        plan = module.reduce_reviews(root, include_failed=True)
    if plan["failed_batch_count"] != 0 or plan["failed_batches"]:
        raise AssertionError(f"missing split parent result should be ignored: {plan}")


def main() -> int:
    test_reduce_validated_reviews_to_apply_plan()
    test_curated_shortlist_penalizes_stale_review_history()
    test_curated_shortlist_suppresses_near_duplicate_topics()
    test_people_updates_preserve_resolver_smoke_queries()
    test_people_dedupe_keeps_same_note_different_people_separate()
    test_people_query_extraction_splits_separators_and_filters_placeholders()
    test_reduce_reports_failed_batches()
    test_cli_can_emit_shortlist_only()
    test_reduce_ignores_failed_parent_with_valid_child()
    test_reduce_ignores_missing_parent_result_with_valid_child()
    print("ok validate-reduce-rollout-memory-reviews")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
