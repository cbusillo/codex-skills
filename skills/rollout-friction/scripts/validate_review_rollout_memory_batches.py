#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
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
        args = Namespace(prompts=root / "prompts.jsonl", model="qwen3-coder-64b")
        runtime = runtime_fixture(module)
        summaries = [
            {"ok": True, "candidate_count": 2, "covered_count": 2, "note_count": 1, "discard_count": 1},
            {"ok": False, "candidate_count": 3, "covered_count": 1, "note_count": 0, "discard_count": 1},
        ]
        summary_path = root / "summary.json"
        module.write_summary(summary_path, args, runtime, summaries)
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


def oversize_args(root: Path) -> Namespace:
    return Namespace(
        output_dir=root,
        skip_existing=False,
        retry_incomplete=0,
        base_url="http://127.0.0.1:1234/v1",
        model="qwen3-coder-64b",
        timeout=30.0,
        max_tokens=100,
        max_input_chars=10,
        temperature=0.1,
        system="test",
    )


def runtime_fixture(module: ModuleType) -> dict[str, object]:
    endpoint = module.resolve_endpoint({}, None, "http://127.0.0.1:1234/v1")
    return {
        "endpoint": endpoint,
        "role": {"name": "test"},
        "model": "qwen3-coder-64b",
        "lifecycle": {"load_policy": "jit_chat", "ttl_seconds": 300},
    }


def test_base_url_override_infers_lm_studio_provider() -> None:
    module = load_module()
    endpoint = module.resolve_endpoint({}, None, "http://127.0.0.1:1234/v1")
    if endpoint.get("provider") != "lm_studio" or "native_base_url" not in endpoint:
        raise AssertionError(f"localhost LM Studio base URL should infer native lifecycle: {endpoint}")


def test_chat_rejects_oversize_prompts() -> None:
    module = load_module()
    batch = {"batch": 1, "candidates": [{"candidate_id": "memcand_a", "text": "x" * 50}]}
    with tempfile.TemporaryDirectory() as tmp:
        try:
            module.chat(batch, oversize_args(Path(tmp)), runtime_fixture(module), retry_attempt=1)
        except module.PromptTooLargeError:
            return
    raise AssertionError("expected oversized prompt to be rejected")


def test_local_review_payload_context_stuffs_trusted_local_prompt() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        args = oversize_args(Path(tmp))
        args.max_input_chars = 1_000
        prompt = json.dumps({"batch": 1, "candidates": [{"candidate_id": "memcand_a", "text": "Remember x."}]})
        payload = module.local_review_payload(prompt, args, runtime_fixture(module), retry_attempt=1)
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        raise AssertionError(f"local review should send system and user messages: {payload}")
    system_message = messages[0]
    if system_message.get("role") != "system":
        raise AssertionError(f"local review should include a system message first: {payload}")
    user_message = messages[1]
    if user_message.get("role") != "user" or user_message.get("content") != prompt:
        raise AssertionError(f"local trusted review should inline prompt content: {payload}")
    serialized = json.dumps(payload)
    for forbidden in ("message_file", "message-file", "context_files", "context-files"):
        if forbidden in serialized:
            raise AssertionError(f"local trusted review should not use {forbidden}: {payload}")
    if payload.get("ttl") != 300:
        raise AssertionError(f"jit lifecycle should add TTL to local review payload: {payload}")


def test_local_review_payload_omits_ttl_for_api_explicit() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        args = oversize_args(Path(tmp))
        runtime = runtime_fixture(module)
        runtime["lifecycle"] = {"load_policy": "api_explicit", "ttl_seconds": 300}
        payload = module.local_review_payload("{}", args, runtime, retry_attempt=1)
    if "ttl" in payload:
        raise AssertionError(f"explicit native lifecycle must not add chat TTL: {payload}")


def test_review_batch_records_oversize_prompt_failure() -> None:
    module = load_module()
    batch = {"batch": 1, "candidates": [{"candidate_id": "memcand_a", "text": "x" * 50}]}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        summary = module.review_batch(batch, 1, oversize_args(root), runtime_fixture(module))
        validation_path = root / "batch-001.validation.json"
        with validation_path.open("r", encoding="utf-8") as handle:
            validation = json.load(handle)
    if summary["ok"]:
        raise AssertionError(f"oversize prompt should fail batch: {summary}")
    if not summary.get("prompt_too_large") or not validation.get("prompt_too_large"):
        raise AssertionError(f"oversize prompt should be recorded: {summary}, {validation}")


def main() -> int:
    test_selects_batch_range_and_limit()
    test_write_summary_counts_results()
    test_split_batch_halves_candidates()
    test_batch_label_for_path()
    test_base_url_override_infers_lm_studio_provider()
    test_chat_rejects_oversize_prompts()
    test_local_review_payload_context_stuffs_trusted_local_prompt()
    test_local_review_payload_omits_ttl_for_api_explicit()
    test_review_batch_records_oversize_prompt_failure()
    print("ok validate-review-rollout-memory-batches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
