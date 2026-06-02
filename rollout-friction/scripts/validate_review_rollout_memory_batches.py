#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused validation for review_rollout_memory_batches.py."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from argparse import Namespace
from pathlib import Path
from types import ModuleType


SCRIPT = Path(__file__).with_name("review_rollout_memory_batches.py")


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("review_rollout_memory_batches", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load review_rollout_memory_batches.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_selects_batch_range_and_limit() -> None:
    module = load_module()
    batches = [{"batch": batch_no} for batch_no in range(1, 8)]
    args = Namespace(start_batch=3, end_batch=6, limit=2)
    selected = module.selected_batches(batches, args)
    if [batch["batch"] for batch in selected] != [3, 4]:
        raise AssertionError(f"unexpected selected batches: {selected}")


def test_write_summary_counts_results() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        args = Namespace(prompts=root / "prompts.jsonl", model="qwen3-coder-64b", base_url="http://127.0.0.1:1234/v1")
        summaries = [
            {"ok": True, "candidate_count": 2, "covered_count": 2, "note_count": 1, "discard_count": 1},
            {"ok": False, "candidate_count": 3, "covered_count": 1, "note_count": 0, "discard_count": 1},
        ]
        summary_path = root / "summary.json"
        module.write_summary(summary_path, args, summaries)
        with summary_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    if payload["ok_count"] != 1 or payload["failed_count"] != 1:
        raise AssertionError(f"unexpected ok/failed counts: {payload}")
    if payload["candidate_count"] != 5 or payload["covered_count"] != 3:
        raise AssertionError(f"unexpected candidate coverage counts: {payload}")


def test_split_batch_halves_candidates() -> None:
    module = load_module()
    batch = {
        "batch": 6,
        "candidates": [
            {"candidate_id": "memcand_a"},
            {"candidate_id": "memcand_b"},
            {"candidate_id": "memcand_c"},
        ],
    }
    children = module.split_batch(batch)
    if [suffix for suffix, _child in children] != ["a", "b"]:
        raise AssertionError(f"unexpected split suffixes: {children}")
    if [len(child["candidates"]) for _suffix, child in children] != [1, 2]:
        raise AssertionError(f"unexpected split sizes: {children}")
    if children[0][1]["batch"] != "6-a" or children[1][1]["batch"] != "6-b":
        raise AssertionError(f"unexpected child batch ids: {children}")


def test_batch_label_for_path() -> None:
    module = load_module()
    if module.batch_label_for_path(12) != "012":
        raise AssertionError("integer batch labels should be zero padded")
    if module.batch_label_for_path("012-a") != "012-a":
        raise AssertionError("string child labels should be preserved")


def main() -> int:
    test_selects_batch_range_and_limit()
    test_write_summary_counts_results()
    test_split_batch_halves_candidates()
    test_batch_label_for_path()
    print("ok validate-review-rollout-memory-batches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
