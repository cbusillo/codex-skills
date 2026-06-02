#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused validation for prepare_rollout_memory_long_context_review.py."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType


SCRIPT = Path(__file__).with_name("prepare_rollout_memory_long_context_review.py")


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prepare_rollout_memory_long_context_review", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load prepare_rollout_memory_long_context_review.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_prompts(root: Path) -> Path:
    path = root / "llm-prompts.jsonl"
    batches = [
        {"batch": 1, "candidates": [{"candidate_id": "memcand_a", "text": "Remember durable thing."}]},
        {"batch": 2, "candidates": [{"candidate_id": "memcand_b", "text": "Remember another thing."}]},
    ]
    with path.open("w", encoding="utf-8") as handle:
        for batch in batches:
            handle.write(json.dumps(batch) + "\n")
    return path


def test_prepare_payload_adds_manifest_and_source_batch() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        prompts = write_prompts(Path(tmp))
        payload, summary = module.prepare_payload(prompts, ("tiny", 10_000))
    if payload["candidate_id_manifest"] != ["memcand_a", "memcand_b"]:
        raise AssertionError(f"unexpected manifest: {payload}")
    if [item["source_batch"] for item in payload["candidates"]] != [1, 2]:
        raise AssertionError(f"source batch should be preserved: {payload}")
    if summary["candidate_count"] != 2 or summary["batch_count"] != 2:
        raise AssertionError(f"unexpected summary: {summary}")


def test_selected_note_prompt_instructs_manifest_copy() -> None:
    module = load_module()
    payload = {"candidate_id_manifest": ["memcand_a"], "candidates": []}
    prompt = module.selected_note_text(payload)
    if "Copy candidate_id_manifest verbatim" not in prompt:
        raise AssertionError(f"prompt should require manifest copy: {prompt}")
    if "implicit discards" not in prompt:
        raise AssertionError(f"prompt should describe implicit discards: {prompt}")


def test_extracts_first_json_object_from_duplicated_capture() -> None:
    module = load_module()
    capture = '{"ok":true}{"ok":true}'
    first = module.extract_first_json_object(capture)
    if first != '{"ok":true}':
        raise AssertionError(f"unexpected extracted object: {first}")


def test_extracts_first_json_object_after_leading_text() -> None:
    module = load_module()
    capture = 'Here is the JSON:\n{"ok":true}'
    first = module.extract_first_json_object(capture)
    if first != '{"ok":true}':
        raise AssertionError(f"unexpected extracted object after prose: {first}")


def test_select_batches_enforces_first_batch_budget() -> None:
    module = load_module()
    batches = [{"batch": 1, "candidates": [{"candidate_id": "memcand_a", "text": "x" * 100}]}]
    selected, input_chars = module.select_batches(batches, 10)
    if selected or input_chars:
        raise AssertionError(f"oversized first batch should not be selected: {selected}, {input_chars}")


def main() -> int:
    test_prepare_payload_adds_manifest_and_source_batch()
    test_selected_note_prompt_instructs_manifest_copy()
    test_extracts_first_json_object_from_duplicated_capture()
    test_extracts_first_json_object_after_leading_text()
    test_select_batches_enforces_first_batch_budget()
    print("ok validate-prepare-rollout-memory-long-context-review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
