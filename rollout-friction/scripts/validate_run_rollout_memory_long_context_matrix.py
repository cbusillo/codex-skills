#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused validation for run_rollout_memory_long_context_matrix.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from argparse import Namespace
from pathlib import Path
from types import ModuleType


SCRIPT = Path(__file__).with_name("run_rollout_memory_long_context_matrix.py")


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_rollout_memory_long_context_matrix", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load run_rollout_memory_long_context_matrix.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_prompts(root: Path) -> Path:
    path = root / "llm-prompts.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"batch": 1, "candidates": [{"candidate_id": "memcand_a", "text": "Remember x."}]}) + "\n")
    return path


def test_parse_variant() -> None:
    module = load_module()
    variant = module.parse_variant("sonnet-1m=claude:claude-sonnet-4-6[1m]")
    if variant != {"name": "sonnet-1m", "provider": "claude", "model": "claude-sonnet-4-6[1m]"}:
        raise AssertionError(f"unexpected variant: {variant}")


def test_dry_run_plans_matrix_row() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        prompts = write_prompts(Path(tmp))
        payload, summary = module.prepare_payload(prompts, ("tiny", 10_000))
        prompt = module.selected_note_text(payload)
        row = module.run_or_plan(
            payload,
            prompt,
            summary,
            {"name": "gpt", "provider": "code-llm", "model": "gpt-5.4"},
            Namespace(dry_run=True),
        )
    if row["status"] != "planned" or row["candidate_count"] != 1:
        raise AssertionError(f"unexpected dry-run row: {row}")


def test_classifies_access_and_budget_errors() -> None:
    module = load_module()
    if module.classify_error("Usage credits required for 1M context") != "blocked_access":
        raise AssertionError("expected Sonnet 1M credit error to classify as blocked_access")
    if module.classify_error("Reached maximum budget ($3)") != "budget_exceeded":
        raise AssertionError("expected budget error to classify as budget_exceeded")


def test_command_error_message_uses_stdout() -> None:
    module = load_module()
    exc = subprocess.CalledProcessError(1, ["claude"], output="Usage credits required for 1M context")
    message = module.command_error_message(exc)
    if "Usage credits required" not in message:
        raise AssertionError(f"expected stdout in command error message: {message}")


def test_default_schema_is_strict_object() -> None:
    module = load_module()
    schema = module.default_schema()
    if schema["additionalProperties"] is not False:
        raise AssertionError(f"schema should be strict: {schema}")
    if "reviewed_candidate_ids" not in schema["required"]:
        raise AssertionError(f"schema should require reviewed ids: {schema}")


def main() -> int:
    test_parse_variant()
    test_dry_run_plans_matrix_row()
    test_classifies_access_and_budget_errors()
    test_command_error_message_uses_stdout()
    test_default_schema_is_strict_object()
    print("ok validate-run-rollout-memory-long-context-matrix")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
