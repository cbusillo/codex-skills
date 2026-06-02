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
        summary["prompt_sha256"] = module.sha256_text(prompt)
        row = module.run_or_plan(
            payload,
            prompt,
            summary,
            {"name": "gpt", "provider": "code-llm", "model": "gpt-5.4"},
            Namespace(dry_run=True),
        )
    if row["status"] != "planned" or row["candidate_count"] != 1:
        raise AssertionError(f"unexpected dry-run row: {row}")


def test_prompt_too_large_row() -> None:
    module = load_module()
    error = module.PromptTooLargeError("too big")
    summary = module.prompt_too_large_summary(Path("prompts.jsonl"), ("tiny", 10), error)
    row = module.prompt_too_large_row(
        summary,
        {"name": "gpt", "provider": "code-llm", "model": "gpt-5.4"},
        error,
    )
    if row["status"] != "prompt_too_large" or row["prompt_chars"] != 0:
        raise AssertionError(f"expected prompt_too_large: {row}")


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


def test_existing_rows_are_keyed_by_budget_and_variant() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "results.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "budget_name": "quarter",
                        "variant": {"name": "gpt", "provider": "code-llm", "model": "gpt-5.4"},
                        "prompt_sha256": "abc",
                        "status": "passed",
                    }
                )
                + "\n"
            )
        rows = module.read_existing_rows(path)
    key = ("quarter", "gpt", "code-llm", "gpt-5.4")
    if key not in rows or rows[key][0]["status"] != "passed":
        raise AssertionError(f"unexpected existing rows: {rows}")


def test_existing_rows_are_not_keyed_by_alias_only() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "results.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for provider, model in [("code-llm", "gpt-5.4"), ("claude", "opus")]:
                handle.write(
                    json.dumps(
                        {
                            "budget_name": "quarter",
                            "variant": {"name": "same", "provider": provider, "model": model},
                            "prompt_sha256": model,
                            "status": "passed",
                        }
                    )
                    + "\n"
                )
        rows = module.read_existing_rows(path)
    if len(rows) != 2:
        raise AssertionError(f"same variant alias should not collapse distinct provider/model rows: {rows}")


def test_existing_rows_preserve_history_for_resume() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "results.jsonl"
        variant = {"name": "gpt", "provider": "code-llm", "model": "gpt-5.4"}
        with path.open("w", encoding="utf-8") as handle:
            for status, prompt_sha256 in [("passed", "abc"), ("failed_json", "abc"), ("passed", "other")]:
                handle.write(
                    json.dumps(
                        {
                            "budget_name": "quarter",
                            "variant": variant,
                            "prompt_sha256": prompt_sha256,
                            "status": status,
                        }
                    )
                    + "\n"
                )
        rows = module.read_existing_rows(path)
    key = ("quarter", "gpt", "code-llm", "gpt-5.4")
    reusable = module.reusable_existing_row(rows[key], {"passed"}, {"prompt_sha256": "abc"})
    if reusable is None or reusable["prompt_sha256"] != "abc" or reusable["status"] != "passed":
        raise AssertionError(f"should find older matching passed row in append-only history: {rows}")


def test_skip_existing_defaults_to_requested_statuses() -> None:
    module = load_module()
    summary = {"prompt_sha256": "abc"}
    if module.reusable_existing_row([{"status": "passed", "prompt_sha256": "abc"}], {"passed"}, summary) is None:
        raise AssertionError("passed rows should skip by default")
    if module.reusable_existing_row([{"status": "failed_json", "prompt_sha256": "abc"}], {"passed"}, summary) is not None:
        raise AssertionError("failed rows should not skip by default")
    if module.reusable_existing_row([{"status": "passed", "prompt_sha256": "old"}], {"passed"}, summary) is not None:
        raise AssertionError("passed rows from a different prompt should not skip")
    if module.reusable_existing_row([{"status": "passed"}], {"passed"}, summary) is not None:
        raise AssertionError("legacy rows without prompt fingerprints should not skip")


def test_retry_status_removes_default_skip_status() -> None:
    module = load_module()
    skip_statuses = module.parse_status_filter([], module.DEFAULT_SKIP_STATUSES) - module.parse_status_filter(
        ["passed"], set()
    )
    if module.reusable_existing_row(
        [{"status": "passed", "prompt_sha256": "abc"}], skip_statuses, {"prompt_sha256": "abc"}
    ) is not None:
        raise AssertionError("retry-status should make passed rows runnable again")


def main() -> int:
    test_parse_variant()
    test_dry_run_plans_matrix_row()
    test_prompt_too_large_row()
    test_classifies_access_and_budget_errors()
    test_command_error_message_uses_stdout()
    test_default_schema_is_strict_object()
    test_existing_rows_are_keyed_by_budget_and_variant()
    test_existing_rows_are_not_keyed_by_alias_only()
    test_existing_rows_preserve_history_for_resume()
    test_skip_existing_defaults_to_requested_statuses()
    test_retry_status_removes_default_skip_status()
    print("ok validate-run-rollout-memory-long-context-matrix")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
