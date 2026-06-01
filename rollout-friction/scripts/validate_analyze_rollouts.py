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


def test_skill_guidance_does_not_count_as_github_rate_limit() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {
                    "type": "event_msg",
                    "payload": {
                        "aggregated_output": "Friction signal guidance: GitHub REST or GraphQL rate-limit pressure should be classified if observed."
                    },
                }
            ],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    unexpected = {"github_graphql_rate_limit", "github_rest_rate_limit", "generic_rate_limit"} & set(findings)
    if unexpected:
        raise AssertionError(f"skill guidance should not count as live rate-limit evidence: {sorted(unexpected)}")


def test_helper_doc_dump_does_not_count_as_github_rate_limit() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {
                    "type": "event_msg",
                    "payload": {
                        "aggregated_output": "--- name: github description: Comprehensive GitHub Expert persona; use gh helper when GraphQL rate limit pressure appears."
                    },
                }
            ],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    unexpected = {"github_graphql_rate_limit", "generic_rate_limit"} & set(findings)
    if unexpected:
        raise AssertionError(f"helper docs should not count as live rate-limit evidence: {sorted(unexpected)}")


def test_github_plan_doc_dump_does_not_count_as_github_rate_limit() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {
                    "type": "event_msg",
                    "payload": {
                        "aggregated_output": "--- name: github-plan description: Use when creating GitHub issues. Local Conventions: helper is REST-first for normal PR orientation. Before batching those operations, check rate limits when failures look quota-related. If GraphQL is exhausted but REST still works, prefer REST."
                    },
                }
            ],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    unexpected = {"github_graphql_rate_limit", "generic_rate_limit"} & set(findings)
    if unexpected:
        raise AssertionError(f"github-plan docs should not count as live rate-limit evidence: {sorted(unexpected)}")


def test_auth_login_noise_is_classified_without_generic_failure() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {
                    "type": "event_msg",
                    "payload": {
                        "aggregated_output": "remote control enrollment failed: Multi-factor authentication required; process exited with code 1"
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "aggregated_output": "remote control login recovered after logout/restart; process exited with code 1"
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "aggregated_output": "refresh_token_reused caused Authentication expired; process exited with code 1"
                    },
                },
            ],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    if "auth_login_loop" not in findings:
        raise AssertionError("auth/login loops should be classified explicitly")
    if "repeated_command_failure" in findings:
        raise AssertionError("auth/login loops should not inflate repeated command failures")


def test_nominal_remote_control_enrollment_is_not_auth_loop() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {
                    "type": "event_msg",
                    "payload": {
                        "aggregated_output": "creating new remote control enrollment: enroll_url=https://example.invalid/enroll"
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "aggregated_output": "remote control status changed to connecting"
                    },
                },
            ],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    if "auth_login_loop" in findings:
        raise AssertionError("nominal remote-control enrollment logs should not count as auth loops")


def test_local_llm_scout_timeout_signal() -> None:
    assert_signals(
        ["LM Studio local LLM private scout timed out before returning suggestions."],
        {"local_llm_scout_timeout"},
    )


def test_local_llm_scout_misuse_risk_signal() -> None:
    assert_signals(
        ["LM Studio local LLM private scout asked for raw traces and tried to decide policy routing."],
        {"local_llm_scout_misuse_risk"},
    )


def test_lm_studio_scout_strips_channel_wrappers() -> None:
    scout_script = Path(__file__).with_name("lm_studio_scout.py")
    spec = importlib.util.spec_from_file_location("lm_studio_scout", scout_script)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load lm_studio_scout.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    normalized = module.normalize_content(
        '<|channel|>final <|constrain|>JSON<|message|>{"ok":true}'
    )
    if normalized != '{"ok":true}':
        raise AssertionError(f"unexpected normalized scout content: {normalized!r}")


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
        neutral_json = root / "2026-05-20T12-00-00.json"
        neutral_jsonl = root / "2026-05-20T12-00-00.jsonl"
        neutral_log = root / "worker-output.log"
        neutral_md = root / "notes.md"
        neutral_json.write_text('{"message":"GraphQL rate limit"}\n', encoding="utf-8")
        neutral_jsonl.write_text('{"message":"GraphQL rate limit"}\n', encoding="utf-8")
        neutral_log.write_text("GraphQL rate limit\n", encoding="utf-8")
        neutral_md.write_text("GraphQL rate limit\n", encoding="utf-8")

        files = module.iter_candidate_files([root], max_files=10)

    if neutral_json not in files:
        raise AssertionError("neutral .json traces should be discoverable from a root directory")
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


def test_static_diff_payloads_do_not_count_as_failures() -> None:
    module = load_module()
    static_diff = """
diff --git a/.github/workflows/test.yml b/.github/workflows/test.yml
index 1234567..89abcde 100644
@@ -1,6 +1,6 @@
 name: Test
 on:
   push:
     branches: [main]
+    # Process exited with code 1
+    # No such file or directory
""".strip()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {"type": "tool_result", "payload": {"aggregated_output": static_diff}},
                {"type": "tool_result", "payload": {"aggregated_output": static_diff}},
                {"type": "tool_result", "payload": {"aggregated_output": static_diff}},
            ],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    unexpected = {"repeated_command_failure", "missing_dependency_or_tool"} & set(findings)
    if unexpected:
        raise AssertionError(f"static diffs should not count as execution failures: {sorted(unexpected)}")


def test_yaml_config_payloads_do_not_count_as_failures() -> None:
    module = load_module()
    workflow_config = """
name: Validate
on:
  push:
    branches: [main]
jobs:
  test:
    steps:
      - run: echo 'Command failed examples belong to docs, not runtime'
""".strip()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {"type": "tool_result", "payload": {"aggregated_output": workflow_config}},
                {"type": "tool_result", "payload": {"aggregated_output": workflow_config}},
                {"type": "tool_result", "payload": {"aggregated_output": workflow_config}},
            ],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    if "repeated_command_failure" in findings:
        raise AssertionError("static workflow YAML should not count as repeated command failure")


def test_real_failures_after_static_payload_still_count() -> None:
    module = load_module()
    config_dump = """
name: Validate
on:
  push:
    branches: [main]
jobs:
  test:
    steps:
      - run: echo 'Command failed examples belong to docs, not runtime'
""".strip()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {
                    "type": "tool_result",
                    "payload": {"aggregated_output": f"{config_dump}\nProcess exited with code 1"},
                },
                {
                    "type": "tool_result",
                    "payload": {"aggregated_output": f"{config_dump}\nerror: runtime failure after config dump"},
                },
                {
                    "type": "tool_result",
                    "payload": {"aggregated_output": f"{config_dump}\nCommand failed in runtime step after config dump"},
                },
                {
                    "type": "tool_result",
                    "payload": {"aggregated_output": f"{config_dump}\nNo such file or directory: missing-tool"},
                },
            ],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    if "repeated_command_failure" not in findings:
        raise AssertionError("real failures after a static config dump should still count")
    if "missing_dependency_or_tool" not in findings:
        raise AssertionError("missing dependencies after a static config dump should still count")


def test_partial_diff_headers_do_not_count_as_failures() -> None:
    module = load_module()
    partial_diff = """
--- a/scripts/build.sh
+++ b/scripts/build.sh
+echo 'Command failed examples belong to docs, not runtime'
+echo 'No such file or directory is documented here'
""".strip()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {"type": "tool_result", "payload": {"aggregated_output": partial_diff}},
                {"type": "tool_result", "payload": {"aggregated_output": partial_diff}},
                {"type": "tool_result", "payload": {"aggregated_output": partial_diff}},
            ],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    unexpected = {"repeated_command_failure", "missing_dependency_or_tool"} & set(findings)
    if unexpected:
        raise AssertionError(f"partial diff headers should suppress static failure examples: {sorted(unexpected)}")


def test_repeated_identical_auto_review_events_still_count_as_loop() -> None:
    module = load_module()
    review_echo = (
        "Auto Review: 1 issue(s) found. Findings: [P2] Preserve the specific error text. "
        "Merge /tmp/auto-review to apply fixes."
    )
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {"type": "event_msg", "payload": {"aggregated_output": review_echo}},
                {"type": "event_msg", "payload": {"aggregated_output": review_echo}},
                {"type": "event_msg", "payload": {"aggregated_output": review_echo}},
            ],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    if "auto_review_loop" not in findings:
        raise AssertionError("identical repeated auto-review events should still satisfy loop threshold")


def test_distinct_auto_review_events_still_count_as_loop() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {"type": "event_msg", "payload": {"aggregated_output": "Auto Review: 1 issue found for manager labels."}},
                {"type": "event_msg", "payload": {"aggregated_output": "Auto Review: 2 issues found for details file paths."}},
            ],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    if "auto_review_loop" not in findings:
        raise AssertionError("distinct auto-review events should still count as review-loop evidence")


def test_spaced_auto_review_with_diff_marker_is_preserved() -> None:
    module = load_module()
    review_with_diff = "Auto Review: 1 issue found. The finding was valid. diff --git a/foo b/foo"
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [{"type": "event_msg", "payload": {"aggregated_output": review_with_diff}}],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    if "auto_review_valid_finding" not in findings:
        raise AssertionError("spaced Auto Review text should survive static-context guard")


def test_plain_workflow_branch_failure_text_is_not_static_config() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {"message": "The workflow failed while checking branches: main; Process exited with code 1"},
                {"message": "cannot push: branches: main is protected; Command failed"},
                {"message": "name: build on: ubuntu failed with error:"},
            ],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)
    if "repeated_command_failure" not in findings:
        raise AssertionError("plain workflow/branch failure text should still count as real failures")


def test_structured_payload_counts_are_separate_from_broad_context() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {
                    "timestamp": "2026-05-28T18:00:00Z",
                    "type": "function_call_output",
                    "payload": {
                        "output": json.dumps(
                            {
                                "status": "error",
                                "error_reason": "GraphQL secondary rate limit",
                            }
                        )
                    },
                },
                {"message": "GraphQL secondary rate limit mentioned in discussion"},
            ],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)

    finding = findings.get("github_graphql_rate_limit")
    if finding is None:
        raise AssertionError("structured nested helper payload should produce GraphQL finding")
    if finding.structured_count != 1:
        raise AssertionError(f"expected one structured payload hit, got {finding.structured_count}")
    payload = module.finding_to_json(finding)
    if payload["structured_payload_count"] != 1 or payload["broad_context_count"] != 1:
        raise AssertionError(f"structured and broad counts should be separate: {payload}")
    if payload["evidence"][0]["evidence_type"] != "structured_payload":
        raise AssertionError(f"structured evidence should be labeled: {payload}")


def test_since_and_after_line_bound_scan_records() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {"timestamp": "2026-05-28T17:00:00Z", "message": "GraphQL rate limit before checkpoint"},
                {"timestamp": "2026-05-28T18:00:00Z", "message": "GraphQL rate limit after checkpoint"},
                {"timestamp": "2026-05-28T19:00:00Z", "message": "No runs found for workflow CodeQL"},
            ],
        )
        files = module.iter_candidate_files([trace], max_files=10)
        findings = module.scan(
            files,
            max_bytes=100_000,
            context_chars=240,
            since_ts=module.parse_timestamp("2026-05-28T18:30:00Z"),
        )
        after_line_findings = module.scan(
            files,
            max_bytes=100_000,
            context_chars=240,
            after_file=trace,
            after_line=1,
        )

    if "github_graphql_rate_limit" in findings:
        raise AssertionError("--since should filter older GraphQL records")
    if "github_workflow_wait_miss" not in findings:
        raise AssertionError("--since should keep fresh records in the window")
    graph = after_line_findings.get("github_graphql_rate_limit")
    if graph is None or graph.count != 1:
        raise AssertionError("--after-line should keep only records after the checkpoint")


def test_investigation_noise_suppression_preserves_structured_payloads() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {"message": "grep GraphQL rate limit in rollout-friction/scripts/analyze_rollouts.py"},
                {
                    "payload": {
                        "output": json.dumps(
                            {"status": "error", "error_reason": "GraphQL rate limit from helper"}
                        )
                    }
                },
            ],
        )
        findings = module.scan(
            [trace],
            max_bytes=100_000,
            context_chars=240,
            suppress_investigation_noise=True,
        )

    finding = findings.get("github_graphql_rate_limit")
    if finding is None:
        raise AssertionError("structured helper payload should survive noise suppression")
    if finding.count != 1 or finding.structured_count != 1:
        raise AssertionError("investigation noise should be suppressed without hiding structured payloads")


def test_nested_wrapped_helper_payload_retains_structured_context() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {
                    "timestamp": "2026-05-28T18:00:00Z",
                    "type": "function_call_output",
                    "status": "error",
                    "payload": {
                        "error_reason": "GraphQL secondary rate limit",
                        "reason": json.dumps(
                            {"status": "error", "error_reason": "GraphQL secondary rate limit"}
                        )
                    },
                }
            ],
        )
        findings = module.scan(
            [trace],
            max_bytes=100_000,
            context_chars=240,
            suppress_investigation_noise=True,
        )

    finding = findings.get("github_graphql_rate_limit")
    if finding is None:
        raise AssertionError("nested wrapped helper payload should still produce a finding")
    if finding.structured_count < 1:
        raise AssertionError(f"nested wrapped helper payload should stay structured, got {finding.structured_count}")
    payload = module.finding_to_json(finding)
    if payload["structured_payload_count"] < 1 or payload["broad_context_count"] != 0:
        raise AssertionError(f"nested wrapped helper payload should not become broad context: {payload}")
    if payload["evidence"][0]["evidence_type"] != "structured_payload":
        raise AssertionError(f"nested wrapped helper payload should be labeled structured: {payload}")


def test_non_structured_nested_payload_strings_are_preserved() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {
                    "timestamp": "2026-05-28T18:00:00Z",
                    "message": json.dumps(
                        {"message": "GraphQL secondary rate limit inside wrapped helper output"}
                    ),
                }
            ],
        )
        findings = module.scan([trace], max_bytes=100_000, context_chars=240)

    finding = findings.get("github_graphql_rate_limit")
    if finding is None:
        raise AssertionError("non-structured nested JSON message should still produce a finding")
    if finding.structured_count != 0:
        raise AssertionError(f"plain nested message should remain broad context: {finding.structured_count}")


def test_status_wrapper_does_not_make_discussion_structured() -> None:
    module = load_module()
    fragments = list(
        module.json_fragments(
            {
                "status": "ok",
                "message": "grep GraphQL rate limit in rollout-friction/scripts/analyze_rollouts.py",
            }
        )
    )

    for fragment in fragments:
        if "GraphQL" in fragment.text and fragment.structured:
            raise AssertionError(f"status-only wrapper should not mark discussion as structured: {fragments}")


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
    test_static_diff_payloads_do_not_count_as_failures()
    test_yaml_config_payloads_do_not_count_as_failures()
    test_real_failures_after_static_payload_still_count()
    test_partial_diff_headers_do_not_count_as_failures()
    test_repeated_identical_auto_review_events_still_count_as_loop()
    test_distinct_auto_review_events_still_count_as_loop()
    test_spaced_auto_review_with_diff_marker_is_preserved()
    test_plain_workflow_branch_failure_text_is_not_static_config()
    test_structured_payload_counts_are_separate_from_broad_context()
    test_since_and_after_line_bound_scan_records()
    test_investigation_noise_suppression_preserves_structured_payloads()
    test_nested_wrapped_helper_payload_retains_structured_context()
    test_non_structured_nested_payload_strings_are_preserved()
    test_status_wrapper_does_not_make_discussion_structured()
    print("ok validate-analyze-rollouts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
