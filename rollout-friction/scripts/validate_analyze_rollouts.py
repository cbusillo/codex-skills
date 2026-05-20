#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused validation for analyze_rollouts.py signal detection."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType


SCRIPT = Path(__file__).with_name("analyze_rollouts.py")


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("analyze_rollouts", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load analyze_rollouts.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_trace(root: Path, lines: list[dict[str, object]]) -> Path:
    path = root / "rollout-test.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")
    return path


def assert_signals(texts: list[str], expected: set[str]) -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [{"type": "event_msg", "payload": {"aggregated_output": text}} for text in texts],
        )
        files = module.iter_candidate_files([trace], max_files=10)
        findings = module.scan(files, max_bytes=100_000, context_chars=240)
    actual = set(findings)
    missing = expected - actual
    if missing:
        raise AssertionError(f"missing signals {sorted(missing)} from {sorted(actual)}")


def test_github_wait_and_rollup_signals() -> None:
    assert_signals(
        [
            "No runs found for workflow 'CodeQL' on feature/example",
            '{"mergeable":"UNKNOWN","statusCheckRollup":[{"name":"Analyze","status":"IN_PROGRESS"}]}',
            '{"mergeable":"MERGEABLE","statusCheckRollup":[{"name":"CodeQL","status":"QUEUED"}]}',
        ],
        {"github_workflow_wait_miss", "github_pr_rollup_lag"},
    )


def test_command_and_shell_friction_signals() -> None:
    assert_signals(
        [
            "Blocked git switch creating or detaching a branch. Resend with 'confirm:' if requested.",
            "confirm: git switch -c feature/example",
            "zsh:1: unmatched \"",
            "Process exited with code 1",
            "Process exited with code 1",
            "Process exited with code 1",
        ],
        {"blocked_git_safety_prompt", "shell_quoting_or_parse_error", "repeated_command_failure"},
    )


def test_auto_review_valid_finding_signal() -> None:
    assert_signals(
        ["Auto Review: 1 issue found. The finding was legitimate and the fix was applied."],
        {"auto_review_valid_finding"},
    )


def test_nested_json_fragments_count_once_per_line() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {
                    "type": "event_msg",
                    "message": "Process exited with code 1",
                    "payload": {"aggregated_output": "Process exited with code 1"},
                }
            ],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    if "repeated_command_failure" in findings:
        raise AssertionError("duplicate nested fragments should not satisfy repeated failure threshold")


def test_pretty_json_object_counts_as_one_record() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = Path(tmp) / "session-pretty.json"
        trace.write_text(
            json.dumps(
                {
                    "message": "Process exited with code 1",
                    "payload": {"aggregated_output": "Process exited with code 1"},
                    "nested": [{"stderr": "Process exited with code 1"}],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    if "repeated_command_failure" in findings:
        raise AssertionError("one pretty JSON object should count as one logical record")


def test_pretty_json_array_counts_top_level_records() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = Path(tmp) / "session-array.json"
        trace.write_text(
            json.dumps(
                [
                    {"message": "Process exited with code 1"},
                    {"message": "Process exited with code 1"},
                    {"message": "Process exited with code 1"},
                ],
                indent=2,
            ),
            encoding="utf-8",
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    if "repeated_command_failure" not in findings:
        raise AssertionError("three top-level JSON records should satisfy repeated failure threshold")


def test_explicit_files_are_not_capped_by_directory_limit() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        directory_trace = root / "session-directory.jsonl"
        explicit_trace = root / "explicit-note.txt"
        directory_trace.write_text('{"message":"directory trace"}\n', encoding="utf-8")
        explicit_trace.write_text("explicit trace without rollout name\n", encoding="utf-8")

        files = module.iter_candidate_files([root, explicit_trace], max_files=0)

    if files != [explicit_trace]:
        raise AssertionError(f"explicit files should bypass directory max_files cap, got {files!r}")


def test_skill_docs_are_not_directory_candidates() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "rollout-friction"
        root.mkdir()
        skill_doc = root / "SKILL.md"
        real_trace = root / "session-trace.md"
        skill_doc.write_text("GraphQL rate limit docs example\n", encoding="utf-8")
        real_trace.write_text("GraphQL rate limit real trace\n", encoding="utf-8")

        files = module.iter_candidate_files([root], max_files=10)

    if skill_doc in files:
        raise AssertionError("skill docs should not be discovered as rollout trace candidates")
    if real_trace not in files:
        raise AssertionError("non-doc trace markdown should remain discoverable")


def test_redaction_covers_local_path_shapes() -> None:
    module = load_module()
    snippet = module.redacted(
        "paths: ~/Library/token ./relative/path ../parent/path rollout-friction/scripts/analyze_rollouts.py",
        context_chars=500,
    )
    leaked = [
        fragment
        for fragment in (
            "~/Library/token",
            "./relative/path",
            "../parent/path",
            "rollout-friction/scripts/analyze_rollouts.py",
        )
        if fragment in snippet
    ]
    if leaked:
        raise AssertionError(f"local path fragments leaked after redaction: {leaked!r} in {snippet!r}")


def test_scanner_io_errors_do_not_count_as_command_failures() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "missing-rollout.jsonl"
        findings = module.scan([missing, missing, missing], max_bytes=100_000, context_chars=240)
    if "repeated_command_failure" in findings:
        raise AssertionError("scanner I/O errors should not be classified as command failures")


def main() -> int:
    test_github_wait_and_rollup_signals()
    test_command_and_shell_friction_signals()
    test_auto_review_valid_finding_signal()
    test_nested_json_fragments_count_once_per_line()
    test_pretty_json_object_counts_as_one_record()
    test_pretty_json_array_counts_top_level_records()
    test_explicit_files_are_not_capped_by_directory_limit()
    test_skill_docs_are_not_directory_candidates()
    test_redaction_covers_local_path_shapes()
    test_scanner_io_errors_do_not_count_as_command_failures()
    print("ok validate-analyze-rollouts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
