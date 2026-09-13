#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused validation for segment_rollout_episodes.py."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).with_name("segment_rollout_episodes.py")


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("segment_rollout_episodes", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load segment_rollout_episodes.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def hit(module: ModuleType, signal: str, line: int, snippet: str) -> object:
    meta = next(item for item in module.ANALYZER.SIGNALS if item.name == signal)
    return module.EventHit(
        signal=signal,
        line=line,
        snippet=snippet,
        structured=True,
        tags=module.ANALYZER.signal_tags(signal, snippet, module.ANALYZER.canonical_hit_text(snippet)),
        severity=meta.severity,
        category=meta.category,
        destination=meta.destination,
        likely_cause=meta.likely_cause,
    )


def target() -> object:
    return SimpleNamespace(path=Path("rollout-test.jsonl"))


def test_groups_nearby_hits_and_detects_resolution(module: ModuleType) -> None:
    trace_lines = [
        module.TraceLine(line=1, snippet="Process exited with code 1", structured=True, failed=True, exit_code=1),
        module.TraceLine(line=2, snippet="retry the same command again", structured=True, tool_id="second-call", retry=True, kind="call"),
        module.TraceLine(line=3, snippet="Process exited with code 0 and tests passed", structured=True, succeeded=True, exit_code=0),
    ]
    episodes = module.build_episodes(
        target(),
        [
            hit(module, "repeated_command_failure", 1, "Process exited with code 1"),
            hit(module, "repeated_command_failure", 2, "retry the same command again"),
        ],
        max_gap_lines=5,
        trace_lines=trace_lines,
    )
    if len(episodes) != 1:
        raise AssertionError(f"expected one grouped episode, got {len(episodes)}")
    payload = module.episode_to_json(episodes[0])
    if payload["outcome"] != "resolved_after_retries":
        raise AssertionError(f"expected resolved_after_retries, got {payload['outcome']}")
    if payload["cost"]["retry_count"] < 1:
        raise AssertionError("retry count should be captured")


def test_episode_carries_failure_cause_tags(module: ModuleType) -> None:
    episodes = module.build_episodes(
        target(),
        [
            hit(module, "repeated_command_failure", 1, "pytest summary: 1 failed, 2 passed"),
            hit(module, "repeated_command_failure", 2, "Process exited with code 1"),
            hit(module, "repeated_command_failure", 3, "Command failed: uv not found"),
        ],
        max_gap_lines=5,
        trace_lines=[],
    )
    payload = module.episode_to_json(episodes[0])
    tags = payload.get("tag_counts", {})
    for expected in ("test_failure", "exit_code", "missing_command", "command_failed"):
        if expected not in tags:
            raise AssertionError(f"missing failure tag {expected!r}: {payload}")


def test_retry_mentions_do_not_count_as_executed_retries(module: ModuleType) -> None:
    trace_lines = [
        module.TraceLine(line=1, snippet="Process exited with code 1", structured=True, failed=True, exit_code=1),
        module.TraceLine(line=2, snippet="retry the command with corrected args", structured=True),
        module.TraceLine(line=3, snippet="Process exited with code 0 and tests passed", structured=True, succeeded=True, exit_code=0),
    ]
    episodes = module.build_episodes(
        target(),
        [hit(module, "repeated_command_failure", 1, "Process exited with code 1")],
        max_gap_lines=5,
        trace_lines=trace_lines,
    )
    payload = module.episode_to_json(episodes[0])
    if payload["cost"]["retry_count"] != 0:
        raise AssertionError(f"a prose retry intention is not an executed retry: {payload['cost']}")
    if payload["outcome"] != "resolved_after_retries":
        raise AssertionError(f"context retry should prevent immediate-resolution labeling, got {payload['outcome']}")


def test_success_requires_success_word_or_zero_exit(module: ModuleType) -> None:
    trace_lines = [
        module.TraceLine(line=1, snippet="Process exited with code 1", structured=True, failed=True, exit_code=1),
        module.TraceLine(line=2, snippet="unsuccessful attempt after tool retry", structured=True),
    ]
    episodes = module.build_episodes(
        target(),
        [hit(module, "repeated_command_failure", 1, "Process exited with code 1")],
        max_gap_lines=5,
        trace_lines=trace_lines,
    )
    payload = module.episode_to_json(episodes[0])
    if payload["outcome"] != "unresolved":
        raise AssertionError(f"unsuccessful should not be treated as success, got {payload['outcome']}")
    if payload["cost"]["failure_count"] != 1:
        raise AssertionError(f"process-exited failure should count as a failure, got {payload['cost']}")


def test_mixed_failed_and_passed_summary_stays_unresolved(module: ModuleType) -> None:
    trace_lines = [module.TraceLine(line=1, snippet="pytest summary: 1 failed, 2 passed", structured=True, failed=True, exit_code=1)]
    episodes = module.build_episodes(
        target(),
        [hit(module, "repeated_command_failure", 1, "pytest summary: 1 failed, 2 passed")],
        max_gap_lines=5,
        trace_lines=trace_lines,
    )
    payload = module.episode_to_json(episodes[0])
    if payload["outcome"] != "unresolved":
        raise AssertionError(f"mixed failure/success summary should stay unresolved, got {payload['outcome']}")


def test_false_success_flag_does_not_resolve_episode(module: ModuleType) -> None:
    trace_lines = [
        module.TraceLine(line=1, snippet='{"success": false, "error": "command failed"}', structured=True, failed=True),
    ]
    episodes = module.build_episodes(
        target(),
        [hit(module, "repeated_command_failure", 1, '"success": false error: command failed')],
        max_gap_lines=5,
        trace_lines=trace_lines,
    )
    payload = module.episode_to_json(episodes[0])
    if payload["outcome"] != "unresolved":
        raise AssertionError(f"false success flag should not resolve episode, got {payload['outcome']}")


def test_episode_windows_stop_at_neighbor_midpoints(module: ModuleType) -> None:
    trace_lines = [
        module.TraceLine(line=1, snippet="Process exited with code 1", structured=True, failed=True, exit_code=1),
        module.TraceLine(line=6, snippet="Process exited with code 1", structured=True, failed=True, exit_code=1),
        module.TraceLine(line=8, snippet="Process exited with code 0 and tests passed", structured=True, succeeded=True, exit_code=0),
    ]
    episodes = module.build_episodes(
        target(),
        [
            hit(module, "repeated_command_failure", 1, "Process exited with code 1"),
            hit(module, "repeated_command_failure", 6, "Process exited with code 1"),
        ],
        max_gap_lines=2,
        trace_lines=trace_lines,
    )
    if len(episodes) != 2:
        raise AssertionError(f"expected two episodes, got {len(episodes)}")
    first_payload = module.episode_to_json(episodes[0])
    second_payload = module.episode_to_json(episodes[1])
    if first_payload["outcome"] != "unresolved":
        raise AssertionError(f"first episode should not inherit neighbor success, got {first_payload['outcome']}")
    if second_payload["outcome"] != "resolved_after_retries":
        raise AssertionError(f"second episode should see its own success, got {second_payload['outcome']}")


def test_splits_distant_hits_and_detects_user_correction(module: ModuleType) -> None:
    episodes = module.build_episodes(
        target(),
        [
            hit(module, "repeated_command_failure", 1, "Process exited with code 1"),
            hit(module, "user_context_correction", 7, "Wait, that is the wrong issue; to be clear, inspect 339."),
        ],
        max_gap_lines=1,
    )
    if len(episodes) != 2:
        raise AssertionError(f"expected split episodes, got {len(episodes)}")
    if module.episode_to_json(episodes[1])["outcome"] != "user_corrected":
        raise AssertionError("second episode should be classified as user_corrected")


def test_episode_ids_are_stable(module: ModuleType) -> None:
    first = module.build_episodes(target(), [hit(module, "stale_results", 4, "stale results returned")], 5)[0]
    second = module.build_episodes(target(), [hit(module, "stale_results", 4, "stale results returned")], 5)[0]
    if first.episode_id != second.episode_id:
        raise AssertionError("episode ids should be deterministic")


def test_subthreshold_hits_are_filtered(module: ModuleType) -> None:
    args = SimpleNamespace(
        since=None,
        until=None,
        suppress_investigation_noise=False,
        context_chars=240,
    )
    fake_target = SimpleNamespace(path=Path("rollout-test.jsonl"), read_bytes=1000)

    original_iter_lines = module.ANALYZER.iter_records
    try:
        module.ANALYZER.iter_records = lambda *_args, **_kwargs: iter(
            [(1, {"exit_code": 1})]
        )
        hits, _trace_lines = module.collect_hits_and_lines(fake_target, args)
    finally:
        module.ANALYZER.iter_records = original_iter_lines
    if hits:
        raise AssertionError("single repeated_command_failure hit should stay below analyzer threshold")


def test_time_filters_use_analyzer_timestamp_parser(module: ModuleType) -> None:
    args = SimpleNamespace(
        since="2026-06-13T00:00:00Z",
        until="2026-06-14T00:00:00Z",
        suppress_investigation_noise=False,
        context_chars=240,
    )
    fake_target = SimpleNamespace(path=Path("rollout-test.jsonl"), read_bytes=1000)

    original_iter_lines = module.ANALYZER.iter_records
    try:
        module.ANALYZER.iter_records = lambda *_args, **_kwargs: iter(
            [
                (1, {"timestamp": "2026-06-13T01:00:00Z", "exit_code": 1}),
                (2, {"timestamp": "2026-06-13T02:00:00Z", "exit_code": 1}),
                (3, {"timestamp": "2026-06-13T03:00:00Z", "exit_code": 1}),
                (4, {"timestamp": "2026-06-14T01:00:00Z", "exit_code": 1}),
            ]
        )
        hits, _trace_lines = module.collect_hits_and_lines(fake_target, args)
    finally:
        module.ANALYZER.iter_records = original_iter_lines
    if len(hits) != 3:
        raise AssertionError("time-bounded collection should parse timestamps and retain thresholded hits")


def command_record(call_id: str, command: str) -> dict[str, object]:
    return {"type": "response_item", "payload": {
        "type": "function_call", "call_id": call_id, "name": "functions.exec_command",
        "arguments": json.dumps({"cmd": command}),
    }}


def result_record(call_id: str, code: int, text: str = "") -> dict[str, object]:
    return {"type": "response_item", "payload": {
        "type": "function_call_output", "call_id": call_id,
        "output": json.dumps({"exit_code": code, "stdout": text, "stderr": text}),
    }}


def test_real_collection_counts_results_calls_and_retries_once(module: ModuleType) -> None:
    records = []
    for index in range(4):
        call_id = f"build-{index}"
        records.extend([command_record(call_id, "build --check"), result_record(call_id, 1 if index < 3 else 0,
                        "error: first\nerror: second\nCommand failed" if index < 3 else "passed")])
        if index == 0:
            records.extend([result_record(call_id, 1, "error: first\nerror: second\nCommand failed"),
                            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "retry again"}}])
    records.extend([command_record("search", "rg needle sample.txt"), result_record("search", 1)])
    with tempfile.TemporaryDirectory() as tmp:
        trace = Path(tmp) / "trace.jsonl"
        trace.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        scan_target = SimpleNamespace(path=trace, read_bytes=trace.stat().st_size)
        args = SimpleNamespace(since=None, until=None, context_chars=240, suppress_investigation_noise=True)
        hits, lines = module.collect_hits_and_lines(scan_target, args)
        episodes = module.build_episodes(scan_target, hits, 25, lines)
        analyzer = module.ANALYZER.scan([trace], 100_000, 240, suppress_investigation_noise=True)
    if len(episodes) != 1:
        raise AssertionError(f"expected one episode, got {len(episodes)}")
    payload = module.episode_to_json(episodes[0])
    expected = {"event_count": 3, "failure_count": 3, "tool_call_count": 5, "retry_count": 3,
                "nonzero_exit_count": 4, "expected_nonzero_count": 1, "text_hint_failure_count": 0}
    if payload["cost"] != expected or analyzer["repeated_command_failure"].count != 3:
        raise AssertionError(f"analyzer/episode counts diverged or multiplied evidence: {payload['cost']}")
    if payload["schema_version"] != 2 or payload["count_semantics"] != module.ANALYZER.COUNT_SEMANTICS:
        raise AssertionError("new cost meanings must be versioned")


def test_successful_argument_literals_do_not_create_episodes(module: ModuleType) -> None:
    records = [command_record("search", "rg --count 'error:|Command failed|exit_code=1' sample.txt"),
               result_record("search", 0, "3")]
    with tempfile.TemporaryDirectory() as tmp:
        trace = Path(tmp) / "trace.jsonl"
        trace.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        scan_target = SimpleNamespace(path=trace, read_bytes=trace.stat().st_size)
        args = SimpleNamespace(since=None, until=None, context_chars=240, suppress_investigation_noise=True)
        hits, lines = module.collect_hits_and_lines(scan_target, args)
        if module.build_episodes(scan_target, hits, 25, lines) or any(line.failed for line in lines):
            raise AssertionError("a successful command's argument literals must not create a friction episode")


def test_cli_thresholds_agree_across_multiple_files(module: ModuleType) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for index in range(3):
            trace = Path(tmp) / f"trace-{index}.jsonl"
            trace.write_text(json.dumps(result_record("same-id", 1)) + "\n", encoding="utf-8")
            paths.append(trace)
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", [str(SCRIPT), *map(str, paths), *map(str, paths), "--json"]), redirect_stdout(stdout):
            module.main()
        payload = json.loads(stdout.getvalue())
        analyzer = module.ANALYZER.scan(paths, 100_000, 240)
    if payload["episode_count"] != 3 or sum(item["cost"]["failure_count"] for item in payload["episodes"]) != analyzer["repeated_command_failure"].count:
        raise AssertionError("the analyzer and segmenter must deduplicate CLI paths and apply thresholds over the same source set")


def test_cli_read_diagnostics_remain_separate_from_episodes(module: ModuleType) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "missing-private-trace.jsonl"
        scan_target = module.ANALYZER.ScanTarget(missing, 1000, 1000, False)
        for json_mode in (False, True):
            argv = [str(SCRIPT), str(missing), "--since", "2026-09-12T00:00:00Z"] + (["--json"] if json_mode else [])
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(sys, "argv", argv), mock.patch.object(module, "scan_targets", return_value=([scan_target], [])), redirect_stdout(stdout), redirect_stderr(stderr):
                module.main()
            if json_mode:
                payload = json.loads(stdout.getvalue())
                diagnostics = payload["scan_limitations"]
                if payload["episode_count"] or payload["outcome_summary"]["failed_result_count"] or stderr.getvalue():
                    raise AssertionError("read failures must not become JSON friction episodes")
            else:
                diagnostics = [json.loads(stderr.getvalue())]
                if stdout.getvalue():
                    raise AssertionError("JSONL stdout must remain an episode-only stream")
            if len(diagnostics) != 1 or diagnostics[0]["kind"] != "scanner_io_error" or str(missing) in json.dumps(diagnostics):
                raise AssertionError("read diagnostics must remain visible and redacted in either output mode")


def main() -> int:
    module = load_module()
    test_groups_nearby_hits_and_detects_resolution(module)
    test_episode_carries_failure_cause_tags(module)
    test_retry_mentions_do_not_count_as_executed_retries(module)
    test_success_requires_success_word_or_zero_exit(module)
    test_mixed_failed_and_passed_summary_stays_unresolved(module)
    test_false_success_flag_does_not_resolve_episode(module)
    test_episode_windows_stop_at_neighbor_midpoints(module)
    test_splits_distant_hits_and_detects_user_correction(module)
    test_episode_ids_are_stable(module)
    test_subthreshold_hits_are_filtered(module)
    test_time_filters_use_analyzer_timestamp_parser(module)
    test_real_collection_counts_results_calls_and_retries_once(module)
    test_successful_argument_literals_do_not_create_episodes(module)
    test_cli_thresholds_agree_across_multiple_files(module)
    test_cli_read_diagnostics_remain_separate_from_episodes(module)
    print("ok validate-segment-rollout-episodes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
