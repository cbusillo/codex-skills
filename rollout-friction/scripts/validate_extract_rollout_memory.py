#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused validation for extract_rollout_memory.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from argparse import Namespace
from pathlib import Path
from types import ModuleType


SCRIPT = Path(__file__).with_name("extract_rollout_memory.py")


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("extract_rollout_memory", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load extract_rollout_memory.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_trace(root: Path, records: list[dict[str, object]]) -> Path:
    path = root / "rollout-test.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def args(**overrides: object):
    module = load_module()
    defaults: dict[str, object] = {
        "max_bytes": 100_000,
        "context_events": 1,
        "max_record_chars": 500,
        "batch_chars": 10_000,
        "max_files": 100,
        "since": None,
        "until": None,
        "destination": None,
        "trusted_originals": True,
        "redact": False,
        "since_ts": None,
        "until_ts": None,
    }
    defaults.update(overrides)
    return Namespace(**defaults), module


def response_item(role: str, text: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "timestamp": "2026-06-01T12:00:00Z",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
        },
    }


def tool_output(text: str) -> dict[str, object]:
    return {
        "type": "function_call_output",
        "timestamp": "2026-06-01T12:00:01Z",
        "payload": {"stdout": text},
    }


def test_skips_session_meta_base_instructions() -> None:
    namespace, module = args()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "base_instructions": {
                            "text": "Available skills mention people and local LLM and manager routing."
                        }
                    },
                },
                response_item("user", "Remember that qwen3-coder-64b is preferred for rollout extraction."),
            ],
        )
        candidates = module.extract([trace], namespace)
    if len(candidates) != 1:
        raise AssertionError(f"expected one real candidate, got {len(candidates)}")
    if candidates[0].destination != "local-llm":
        raise AssertionError(f"expected local-llm candidate, got {candidates[0].destination}")


def test_context_window_preserves_neighboring_turns() -> None:
    namespace, module = args(context_events=1)
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                response_item("user", "We should make future scans destination-aware."),
                response_item("assistant", "I will remember that as a workflow preference."),
                response_item("user", "Never dump transient PR status into local memory."),
            ],
        )
        candidates = module.extract([trace], namespace)
    destinations = {candidate.destination for candidate in candidates}
    if "profile" not in destinations:
        raise AssertionError(f"expected profile destination, got {destinations}")
    never_candidate = next(candidate for candidate in candidates if "Never dump" in candidate.text)
    if len(never_candidate.context) < 2:
        raise AssertionError("context window should include neighboring turn")


def test_classifies_people_local_llm_and_friction() -> None:
    namespace, module = args()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                response_item("user", "Kyle/HonkHonk is a trusted collaborator, but ask before adding trust notes."),
                response_item("user", "qwen3-coder-64b is our preferred local LLM extraction model."),
                tool_output("Auto Review loop repeated and blocked the branch until confirm: git switch was used."),
            ],
        )
        candidates = module.extract([trace], namespace)
    destinations = [candidate.destination for candidate in candidates]
    for expected in ("people", "local-llm", "rollout-friction"):
        if expected not in destinations:
            raise AssertionError(f"missing {expected} from {destinations}")


def test_redact_mode_removes_paths_but_keeps_trusted_originals() -> None:
    redact_args, module = args(redact=True, trusted_originals=False)
    trusted_args, _module = args(redact=False, trusted_originals=True)
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [response_item("user", "Remember local config path /Users/example/Developer/.code/local.env.")],
        )
        redacted = module.extract([trace], redact_args)[0].text
        redacted_source = module.extract([trace], redact_args)[0].source_file
        original_candidate = module.extract([trace], trusted_args)[0]
        original = original_candidate.text
    if "/Users/example" in redacted:
        raise AssertionError(f"redact mode leaked path: {redacted}")
    if "/" in redacted_source and "<path-redacted>" not in redacted_source:
        raise AssertionError(f"redact mode leaked source path: {redacted_source}")
    if "/Users/example" not in original:
        raise AssertionError(f"trusted originals should preserve path: {original}")
    if str(trace) != original_candidate.source_file:
        raise AssertionError(f"trusted originals should preserve source path: {original_candidate.source_file}")


def test_prompt_batches_emit_destination_aware_task() -> None:
    namespace, module = args()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(Path(tmp), [response_item("user", "Remember local LLM preference qwen3-coder-64b.")])
        candidates = module.extract([trace], namespace)
    batches = list(module.prompt_batches(candidates, batch_chars=10_000))
    if len(batches) != 1:
        raise AssertionError(f"expected one batch, got {len(batches)}")
    task = batches[0]["task"]
    if "people_updates" not in task or "discard" not in task:
        raise AssertionError(f"prompt task should be destination-aware: {task}")
    if "github_handle" not in task or "aliases" not in task:
        raise AssertionError(f"people prompt should request structured identity fields: {task}")
    people_schema = batches[0]["output_schema"]["people_updates"][0]
    for expected_key in ("name", "aliases", "github_handle", "role", "organization"):
        if expected_key not in people_schema:
            raise AssertionError(f"people schema missing {expected_key}: {people_schema}")
    if batches[0].get("allowed_model_scope") != "trusted_local_or_trusted_lan":
        raise AssertionError(f"prompt should be local/trusted scoped: {batches[0]}")


def test_candidate_ids_are_stable() -> None:
    namespace, module = args()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(Path(tmp), [response_item("user", "Remember local LLM preference qwen3-coder-64b.")])
        first = module.extract([trace], namespace)
        second = module.extract([trace], namespace)
    if first[0].candidate_id != second[0].candidate_id:
        raise AssertionError("candidate ids should be stable across identical reruns")
    if not first[0].candidate_id.startswith("memcand_"):
        raise AssertionError(f"unexpected candidate id shape: {first[0].candidate_id}")


def test_dedupe_keeps_distinct_long_common_prefixes() -> None:
    namespace, module = args(max_record_chars=2_000)
    prefix = "Remember " + ("shared workflow detail " * 60)
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                response_item("user", prefix + "first durable preference for qwen3-coder-64b."),
                response_item("user", prefix + "second durable preference for qwen3-coder-next."),
            ],
        )
        candidates = module.extract([trace], namespace)
    if len(candidates) != 2:
        raise AssertionError(f"expected both same-prefix candidates to survive dedupe, got {len(candidates)}")


def test_classifies_repo_specific_details() -> None:
    namespace, module = args()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [response_item("user", "In skill-creator/scripts/build.py keep artifact scans local.")],
        )
        candidates = module.extract([trace], namespace)
    destinations = {candidate.destination for candidate in candidates}
    if "repo-specific" not in destinations:
        raise AssertionError(f"expected repo-specific destination, got {destinations}")


def test_output_dir_writes_local_artifacts() -> None:
    namespace, module = args()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        trace = write_trace(root, [response_item("user", "Remember local LLM preference qwen3-coder-64b.")])
        out_dir = root / "artifacts"
        candidates = module.extract([trace], namespace)
        module.write_artifacts(out_dir, [trace], candidates, namespace)
        candidates_path = out_dir / "candidates.jsonl"
        prompts_path = out_dir / "llm-prompts.jsonl"
        diagnostics_path = out_dir / "diagnostics.json"
        for path in (candidates_path, prompts_path, diagnostics_path):
            if not path.exists():
                raise AssertionError(f"missing artifact {path}")
        first_candidate = json.loads(candidates_path.read_text(encoding="utf-8").splitlines()[0])
        if first_candidate.get("schema_version") != 1 or "candidate_id" not in first_candidate:
            raise AssertionError(f"candidate artifact missing schema/id: {first_candidate}")
        diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
        if diagnostics["privacy"]["local_only"] is not True:
            raise AssertionError(f"diagnostics should mark local_only: {diagnostics}")
        if diagnostics["candidate_count"] != 1:
            raise AssertionError(f"expected one artifact candidate: {diagnostics}")


def test_output_dir_cli_is_quiet() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        trace = write_trace(root, [response_item("user", "Remember local LLM preference qwen3-coder-64b.")])
        out_dir = root / "artifacts"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(trace), "--trusted-originals", "--output-dir", str(out_dir)],
            capture_output=True,
            text=True,
            check=True,
        )
    if result.stdout:
        raise AssertionError(f"output-dir mode should not print local artifact diagnostics: {result.stdout}")


def test_destination_filter_matches_cli_behavior() -> None:
    namespace, module = args(destination=["local-llm"])
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        trace = write_trace(
            root,
            [
                response_item("user", "Remember that qwen3-coder-64b is preferred."),
                response_item("user", "Never dump transient PR status into memory."),
            ],
        )
        candidates = module.extract([trace], namespace)
        wanted = set(namespace.destination)
        filtered = [candidate for candidate in candidates if candidate.destination in wanted]
    if not filtered or {candidate.destination for candidate in filtered} != {"local-llm"}:
        raise AssertionError(f"destination filtering should isolate local-llm: {filtered}")


def main() -> int:
    test_skips_session_meta_base_instructions()
    test_context_window_preserves_neighboring_turns()
    test_classifies_people_local_llm_and_friction()
    test_redact_mode_removes_paths_but_keeps_trusted_originals()
    test_prompt_batches_emit_destination_aware_task()
    test_candidate_ids_are_stable()
    test_dedupe_keeps_distinct_long_common_prefixes()
    test_classifies_repo_specific_details()
    test_output_dir_writes_local_artifacts()
    test_output_dir_cli_is_quiet()
    test_destination_filter_matches_cli_behavior()
    print("ok validate-extract-rollout-memory")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
