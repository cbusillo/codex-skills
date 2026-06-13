#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused validation for segment_rollout_episodes.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


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
        severity=meta.severity,
        category=meta.category,
        destination=meta.destination,
        likely_cause=meta.likely_cause,
    )


def target() -> object:
    return SimpleNamespace(path=Path("rollout-test.jsonl"))


def test_groups_nearby_hits_and_detects_resolution(module: ModuleType) -> None:
    trace_lines = [
        module.TraceLine(line=1, snippet="Process exited with code 1", structured=True),
        module.TraceLine(line=2, snippet="retry the same command again", structured=True),
        module.TraceLine(line=3, snippet="Process exited with code 0 and tests passed", structured=True),
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


def test_retry_count_uses_context_window(module: ModuleType) -> None:
    trace_lines = [
        module.TraceLine(line=1, snippet="Process exited with code 1", structured=True),
        module.TraceLine(line=2, snippet="retry the command with corrected args", structured=True),
        module.TraceLine(line=3, snippet="Process exited with code 0 and tests passed", structured=True),
    ]
    episodes = module.build_episodes(
        target(),
        [hit(module, "repeated_command_failure", 1, "Process exited with code 1")],
        max_gap_lines=5,
        trace_lines=trace_lines,
    )
    payload = module.episode_to_json(episodes[0])
    if payload["cost"]["retry_count"] != 1:
        raise AssertionError(f"retry count should include context window lines, got {payload['cost']}")
    if payload["outcome"] != "resolved_after_retries":
        raise AssertionError(f"context retry should prevent immediate-resolution labeling, got {payload['outcome']}")


def test_success_requires_success_word_or_zero_exit(module: ModuleType) -> None:
    trace_lines = [
        module.TraceLine(line=1, snippet="Process exited with code 1", structured=True),
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


def test_false_success_flag_does_not_resolve_episode(module: ModuleType) -> None:
    trace_lines = [
        module.TraceLine(line=1, snippet='{"success": false, "error": "command failed"}', structured=True),
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
        module.TraceLine(line=1, snippet="Process exited with code 1", structured=True),
        module.TraceLine(line=6, snippet="Process exited with code 1", structured=True),
        module.TraceLine(line=8, snippet="Process exited with code 0 and tests passed", structured=True),
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

    original_iter_lines = module.ANALYZER.iter_lines
    try:
        module.ANALYZER.iter_lines = lambda *_args, **_kwargs: iter(
            [(1, "Process exited with code 1", False, True)]
        )
        hits, _trace_lines = module.collect_hits_and_lines(fake_target, args)
    finally:
        module.ANALYZER.iter_lines = original_iter_lines
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

    original_iter_lines = module.ANALYZER.iter_lines
    try:
        module.ANALYZER.iter_lines = lambda *_args, **_kwargs: iter(
            [
                (1, "Process exited with code 1", False, True),
                (2, "Process exited with code 1", False, True),
                (3, "Process exited with code 1", False, True),
            ]
        )
        hits, _trace_lines = module.collect_hits_and_lines(fake_target, args)
    finally:
        module.ANALYZER.iter_lines = original_iter_lines
    if len(hits) != 3:
        raise AssertionError("time-bounded collection should parse timestamps and retain thresholded hits")


def main() -> int:
    module = load_module()
    test_groups_nearby_hits_and_detects_resolution(module)
    test_retry_count_uses_context_window(module)
    test_success_requires_success_word_or_zero_exit(module)
    test_false_success_flag_does_not_resolve_episode(module)
    test_episode_windows_stop_at_neighbor_midpoints(module)
    test_splits_distant_hits_and_detects_user_correction(module)
    test_episode_ids_are_stable(module)
    test_subthreshold_hits_are_filtered(module)
    test_time_filters_use_analyzer_timestamp_parser(module)
    print("ok validate-segment-rollout-episodes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
