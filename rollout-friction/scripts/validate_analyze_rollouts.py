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


def test_json_object_summary_preserves_multi_field_signals() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {
                    "type": "tool_result",
                    "content": {
                        "mergeable": "UNKNOWN",
                        "statusCheckRollup": [{"name": "Analyze", "status": "IN_PROGRESS"}],
                    },
                },
                {
                    "type": "tool_result",
                    "content": {
                        "mergeable": "MERGEABLE",
                        "statusCheckRollup": [{"name": "CodeQL", "status": "QUEUED"}],
                    },
                },
            ],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    if "github_pr_rollup_lag" not in findings:
        raise AssertionError("structured JSON PR rollup records should preserve combined-field matches")


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


def test_structured_json_record_counts_distinct_repeated_hits() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = Path(tmp) / "session-distinct.json"
        trace.write_text(
            json.dumps(
                {
                    "message": "Process exited with code 1",
                    "payload": {"aggregated_output": "Command failed in build"},
                    "nested": [{"stderr": "error: lint failed"}],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    if "repeated_command_failure" not in findings:
        raise AssertionError("distinct repeated failures inside one JSON record should satisfy threshold")


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


def test_neutral_structured_traces_are_directory_candidates() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        neutral_jsonl = root / "2026-05-20T12-00-00.jsonl"
        neutral_log = root / "worker-output.log"
        neutral_md = root / "notes.md"
        neutral_jsonl.write_text('{"message":"GraphQL rate limit"}\n', encoding="utf-8")
        neutral_log.write_text("GraphQL rate limit\n", encoding="utf-8")
        neutral_md.write_text("GraphQL rate limit\n", encoding="utf-8")

        files = module.iter_candidate_files([root], max_files=10)

    if neutral_jsonl not in files:
        raise AssertionError("neutral .jsonl traces should be discoverable from a root directory")
    if neutral_log not in files:
        raise AssertionError("neutral .log traces should be discoverable from a root directory")
    if neutral_md in files:
        raise AssertionError("neutral markdown notes should not be discovered without trace context")


def test_redaction_covers_local_path_shapes() -> None:
    module = load_module()
    snippet = module.redacted(
        "paths: ~/Library/token ./relative/path ../parent/path "
        "rollout-friction/scripts/analyze_rollouts.py /etc/service/token "
        "/workspace/app/session.jsonl localhost host.docker.internal api.service.local",
        context_chars=500,
    )
    leaked = [
        fragment
        for fragment in (
            "~/Library/token",
            "./relative/path",
            "../parent/path",
            "rollout-friction/scripts/analyze_rollouts.py",
            "/etc/service/token",
            "/workspace/app/session.jsonl",
            "localhost",
            "host.docker.internal",
            "api.service.local",
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


def test_meta_echoes_do_not_create_findings() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {
                    "message": "Review only the provided code change scope. Identify critical bugs, regressions, security risks, or incorrect assumptions.",
                },
                {
                    "message": '{"signal":"github_graphql_rate_limit","severity":"high","recommended_destination":"fix-script-or-helper","likely_cause":"GraphQL rate limit"}',
                },
                {
                    "message": "---\\nname: github\\ndescription: GitHub skill docs mention REST rate limit and GraphQL rate limit.",
                },
                {
                    "message": "(error|failed|blocked|timeout|timed out|rate limit|GraphQL|retry|rerun|stale)",
                },
                {
                    "message": "I used `rollout-friction` read-only. Recent bounded scan found GraphQL rate limit findings.",
                },
                {
                    "message": "1. Patch `rollout-friction/scripts/analyze_rollouts.py` so it catches GraphQL rate limit and No runs found signals.",
                },
                {
                    "message": "@@ -10,4 +10,2 @@ -Command failed in copied diff text",
                },
                {
                    "message": "exit_code=2",
                },
                {
                    "message": '<user_action> <context>User initiated a review task.</context> <action>review</action>',
                },
                {
                    "message": '{"findings":[{"title":"[P2] Recurse into nested JSON fields","body":"GraphQL rate limit"}]}',
                },
                {
                    "message": "❌ Validate New Code: 2 issue(s) shellcheck error",
                },
            ],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    if findings:
        raise AssertionError(f"meta echoes should not create findings, got {sorted(findings)}")


def test_real_trace_evidence_survives_meta_echo_filter() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {"message": "GraphQL API returned secondary rate limit while polling projects"},
                {"message": "No runs found for workflow 'CodeQL' on feature/example"},
                {"message": "Process exited with code 1"},
                {"message": "Process exited with code 1"},
                {"message": "Process exited with code 1"},
            ],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    for expected in (
        "github_graphql_rate_limit",
        "github_workflow_wait_miss",
        "repeated_command_failure",
    ):
        if expected not in findings:
            raise AssertionError(f"real trace evidence should still report {expected}")


def main() -> int:
    test_github_wait_and_rollup_signals()
    test_json_object_summary_preserves_multi_field_signals()
    test_command_and_shell_friction_signals()
    test_auto_review_valid_finding_signal()
    test_nested_json_fragments_count_once_per_line()
    test_pretty_json_object_counts_as_one_record()
    test_structured_json_record_counts_distinct_repeated_hits()
    test_pretty_json_array_counts_top_level_records()
    test_explicit_files_are_not_capped_by_directory_limit()
    test_skill_docs_are_not_directory_candidates()
    test_neutral_structured_traces_are_directory_candidates()
    test_redaction_covers_local_path_shapes()
    test_scanner_io_errors_do_not_count_as_command_failures()
    test_meta_echoes_do_not_create_findings()
    test_real_trace_evidence_survives_meta_echo_filter()
    print("ok validate-analyze-rollouts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
