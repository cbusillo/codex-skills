#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused validation for validate_rollout_memory_llm_results.py."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType


SCRIPT = Path(__file__).with_name("validate_rollout_memory_llm_results.py")


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("validate_rollout_memory_llm_results", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load validate_rollout_memory_llm_results.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, payload: object) -> Path:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload))
    return path


def prompt_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "candidates": [
            {"candidate_id": "memcand_a", "text": "Remember qwen3-coder-64b."},
            {"candidate_id": "memcand_b", "text": "One-off status update."},
        ],
    }


def result_payload(content: dict[str, object]) -> dict[str, object]:
    return {"ok": True, "content": json.dumps(content)}


def complete_content() -> dict[str, object]:
    return {
        "decisions": [
            {"candidate_id": "memcand_a", "action": "note", "destination": "local_llm_notes"},
            {"candidate_id": "memcand_b", "action": "discard", "destination": "discard_reasons"},
        ],
        "people_updates": [],
        "profile_notes": [],
        "rollout_friction_notes": [],
        "local_llm_notes": [{"candidate_id": "memcand_a", "note": "qwen3-coder-64b preference"}],
        "repo_specific_notes": [],
        "discard_reasons": [{"candidate_id": "memcand_b", "reason": "transient"}],
        "reviewed_candidate_ids": ["memcand_a", "memcand_b"],
    }


def test_accepts_complete_result() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        summary = module.validate(
            write_json(root / "prompt.json", prompt_payload()),
            write_json(root / "result.json", result_payload(complete_content())),
        )
    if not summary["ok"]:
        raise AssertionError(f"expected complete result to pass: {summary}")
    if summary["covered_count"] != 2 or summary["note_count"] != 1:
        raise AssertionError(f"unexpected complete summary: {summary}")


def test_rejects_missing_candidate_coverage() -> None:
    module = load_module()
    content = complete_content()
    content["discard_reasons"] = []
    content["reviewed_candidate_ids"] = ["memcand_a"]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        summary = module.validate(
            write_json(root / "prompt.json", prompt_payload()),
            write_json(root / "result.json", result_payload(content)),
        )
    if summary["ok"]:
        raise AssertionError(f"expected missing candidate coverage to fail: {summary}")
    if summary["missing_candidate_ids"] != ["memcand_b"]:
        raise AssertionError(f"expected memcand_b missing: {summary}")


def test_reviewed_id_without_disposition_is_incomplete() -> None:
    module = load_module()
    content = complete_content()
    content["discard_reasons"] = []
    content["reviewed_candidate_ids"] = ["memcand_a", "memcand_b"]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        summary = module.validate(
            write_json(root / "prompt.json", prompt_payload()),
            write_json(root / "result.json", result_payload(content)),
        )
    if summary["ok"]:
        raise AssertionError(f"expected reviewed-only candidate to fail: {summary}")
    if summary["missing_disposition_candidate_ids"] != ["memcand_b"]:
        raise AssertionError(f"expected memcand_b missing disposition: {summary}")


def test_implicit_discard_mode_allows_reviewed_omissions() -> None:
    module = load_module()
    content = complete_content()
    content["decisions"] = [
        {"candidate_id": "memcand_a", "action": "note", "destination": "local_llm_notes"},
    ]
    content["discard_reasons"] = []
    content["reviewed_candidate_ids"] = ["memcand_a", "memcand_b"]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        prompt_path = write_json(root / "prompt.json", prompt_payload())
        result_path = write_json(root / "result.json", result_payload(content))
        strict = module.validate(prompt_path, result_path)
        implicit = module.validate(prompt_path, result_path, allow_implicit_discards=True)
    if strict["ok"]:
        raise AssertionError(f"strict validation should reject omitted dispositions: {strict}")
    if not implicit["ok"]:
        raise AssertionError(f"implicit discard mode should accept reviewed omissions: {implicit}")
    if implicit["implicit_discard_count"] != 1:
        raise AssertionError(f"unexpected implicit discard count: {implicit}")


def test_rejects_duplicate_reviewed_ids() -> None:
    module = load_module()
    content = complete_content()
    content["reviewed_candidate_ids"] = ["memcand_a", "memcand_a", "memcand_b"]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        summary = module.validate(
            write_json(root / "prompt.json", prompt_payload()),
            write_json(root / "result.json", result_payload(content)),
        )
    if summary["ok"]:
        raise AssertionError(f"expected duplicate reviewed id to fail: {summary}")
    if summary["duplicate_reviewed_candidate_ids"] != ["memcand_a"]:
        raise AssertionError(f"expected memcand_a duplicate reviewed id: {summary}")


def test_rejects_overlapping_note_and_discard() -> None:
    module = load_module()
    content = complete_content()
    content["discard_reasons"] = [
        {"candidate_id": "memcand_a", "reason": "also discarded"},
        {"candidate_id": "memcand_b", "reason": "transient"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        summary = module.validate(
            write_json(root / "prompt.json", prompt_payload()),
            write_json(root / "result.json", result_payload(content)),
        )
    if summary["ok"]:
        raise AssertionError(f"expected overlapping disposition to fail: {summary}")
    if summary["overlapping_disposition_candidate_ids"] != ["memcand_a"]:
        raise AssertionError(f"expected memcand_a overlapping disposition: {summary}")


def test_rejects_duplicate_decisions() -> None:
    module = load_module()
    content = complete_content()
    content["decisions"] = [
        {"candidate_id": "memcand_a", "action": "note", "destination": "local_llm_notes"},
        {"candidate_id": "memcand_a", "action": "note", "destination": "local_llm_notes"},
        {"candidate_id": "memcand_b", "action": "discard", "destination": "discard_reasons"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        summary = module.validate(
            write_json(root / "prompt.json", prompt_payload()),
            write_json(root / "result.json", result_payload(content)),
        )
    if summary["ok"]:
        raise AssertionError(f"expected duplicate decisions to fail: {summary}")
    if summary["duplicate_decision_candidate_ids"] != ["memcand_a"]:
        raise AssertionError(f"expected memcand_a duplicate decision: {summary}")


def test_rejects_missing_decision() -> None:
    module = load_module()
    content = complete_content()
    content["decisions"] = [
        {"candidate_id": "memcand_a", "action": "note", "destination": "local_llm_notes"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        summary = module.validate(
            write_json(root / "prompt.json", prompt_payload()),
            write_json(root / "result.json", result_payload(content)),
        )
    if summary["ok"]:
        raise AssertionError(f"expected missing decision to fail: {summary}")
    if summary["missing_decision_candidate_ids"] != ["memcand_b"]:
        raise AssertionError(f"expected memcand_b missing decision: {summary}")


def test_rejects_unknown_decision_ids() -> None:
    module = load_module()
    content = complete_content()
    content["decisions"].append(
        {"candidate_id": "memcand_unknown", "action": "discard", "destination": "discard_reasons"}
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        summary = module.validate(
            write_json(root / "prompt.json", prompt_payload()),
            write_json(root / "result.json", result_payload(content)),
        )
    if summary["ok"]:
        raise AssertionError(f"expected unknown decision id to fail: {summary}")
    if summary["unknown_candidate_ids"] != ["memcand_unknown"]:
        raise AssertionError(f"expected memcand_unknown to be reported unknown: {summary}")


def test_rejects_missing_decisions_array() -> None:
    module = load_module()
    content = complete_content()
    del content["decisions"]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        try:
            module.validate(
                write_json(root / "prompt.json", prompt_payload()),
                write_json(root / "result.json", result_payload(content)),
            )
        except module.ValidationError as exc:
            if "decisions" not in str(exc):
                raise AssertionError(f"expected decisions missing error, got {exc}") from exc
            return
    raise AssertionError("expected missing decisions array to fail validation")


def main() -> int:
    test_accepts_complete_result()
    test_rejects_missing_candidate_coverage()
    test_reviewed_id_without_disposition_is_incomplete()
    test_implicit_discard_mode_allows_reviewed_omissions()
    test_rejects_duplicate_reviewed_ids()
    test_rejects_overlapping_note_and_discard()
    test_rejects_duplicate_decisions()
    test_rejects_missing_decision()
    test_rejects_unknown_decision_ids()
    test_rejects_missing_decisions_array()
    print("ok validate-validate-rollout-memory-llm-results")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
