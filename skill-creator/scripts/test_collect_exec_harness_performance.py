#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused tests for collect_exec_harness_performance.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).with_name("collect_exec_harness_performance.py")


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("collect_exec_harness_performance", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load collect_exec_harness_performance.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def install_fake_artifacts(module: Any, artifacts: dict[str, dict[str, Any]]) -> None:
    def fake_read_json(path: Path) -> Any | None:
        run = artifacts.get(path.parents[1].name)
        if not run:
            return None
        if path.name == "summary.json":
            return run["summary"]
        if path.name == "manifest.json":
            return run["manifest"]
        return None

    def fake_iter_jsonl(path: Path) -> list[dict[str, Any]]:
        run = artifacts.get(path.parents[1].name)
        if not run:
            return []
        return list(run.get("events", []))

    module.read_json = fake_read_json
    module.iter_jsonl = fake_iter_jsonl
    module.duration_from_mtime = lambda run_dir: (None, "unavailable")


def test_collects_deterministic_metrics_without_private_paths() -> None:
    module = load_module()
    scenario = module.ROOT / "skill-creator/evaluations/exec-harness/fake-gh-plan-index.json"
    run_dir = Path("/local/harness/20260604-120000-fake-gh-plan-index")
    install_fake_artifacts(
        module,
        {
            run_dir.name: {
                "summary": {
                    "returncode": 0,
                    "duration_ms": 1234,
                    "commands": [],
                    "gh_calls": [{"argv": ["issue", "list"]}],
                    "expectation_failures": [],
                    "errors": [],
                    "usage": {"total_tokens": 0},
                },
                "manifest": {"fake_responses": True, "scenario": str(scenario)},
                "events": [
                    {"msg": {"type": "exec_command_begin"}},
                    {"msg": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 0}}}},
                ],
            }
        },
    )
    report = module.build_report([run_dir], duration_budget_ms=5_000)

    assert report["summary"] == {
        "runs": 1,
        "passed": 1,
        "failed": 0,
        "failure_rate": 0.0,
        "retry_rate": 0.0,
    }
    run = report["runs"][0]
    assert run["run_dir_label"] == "20260604-120000-fake-gh-plan-index"
    assert run["scenario"] == "fake-gh-plan-index"
    assert run["mode"] == "deterministic"
    assert run["model_family"] == "fake_responses_api+fake_gh"
    assert run["duration_ms"] == 1234
    assert run["command_count"] == 1
    assert run["tool_call_count"] == 1
    assert run["budget_status"] == "within_budget"
    assert "/Users/" not in json.dumps(report)


def test_marks_failures_and_budget_overages_as_advisory() -> None:
    module = load_module()
    run_dir = Path("/local/harness/20260604-120100-local-model-scenario")
    install_fake_artifacts(
        module,
        {
            run_dir.name: {
                "summary": {
                    "returncode": 1,
                    "duration_ms": 60_000,
                    "commands": [],
                    "gh_calls": [],
                    "expectation_failures": ["missing text"],
                    "errors": [],
                    "usage": {
                        "total_tokens": 17,
                        "input_tokens": 10,
                        "cached_input_tokens": 3,
                        "output_tokens": 5,
                        "reasoning_output_tokens": 2,
                    },
                },
                "manifest": {"fake_responses": False, "scenario": "/tmp/local-model-scenario.json"},
                "events": [{"msg": {"type": "browser_open_begin"}}],
            }
        },
    )
    report = module.build_report([run_dir], duration_budget_ms=30_000)

    assert report["summary"]["failed"] == 1
    run = report["runs"][0]
    assert run["passed"] is False
    assert run["failure_count"] == 2
    assert run["mode"] == "local_llm"
    assert run["token_estimate"] == 20
    assert run["token_source"] == "summary.usage.parts"
    assert run["budget_status"] == "over_budget_advisory"
    assert report["groups"][0]["budget_status"] == "over_budget_advisory"


def test_missing_returncode_counts_as_failure() -> None:
    module = load_module()
    run_dir = Path("/local/harness/20260604-120200-missing-returncode")
    install_fake_artifacts(
        module,
        {
            run_dir.name: {
                "summary": {"duration_ms": 10, "commands": [], "gh_calls": [], "usage": {}},
                "manifest": {"fake_responses": False},
                "events": [],
            }
        },
    )
    report = module.build_report([run_dir], duration_budget_ms=30_000)

    run = report["runs"][0]
    assert run["passed"] is False
    assert run["failure_count"] == 1
    assert report["summary"]["failed"] == 1


def test_empty_gh_calls_still_indicates_fake_gh_mode() -> None:
    module = load_module()
    run_dir = Path("/local/harness/20260604-120300-fake-gh-no-calls")
    install_fake_artifacts(
        module,
        {
            run_dir.name: {
                "summary": {
                    "returncode": 1,
                    "duration_ms": 10,
                    "commands": [],
                    "gh_calls": [],
                    "gh_state": {"issues": {}},
                    "usage": {},
                },
                "manifest": {"fake_responses": False},
                "events": [],
            }
        },
    )
    report = module.build_report([run_dir], duration_budget_ms=30_000)

    run = report["runs"][0]
    assert run["mode"] == "deterministic"
    assert run["model_family"] == "fake_gh"


def test_top_level_jsonl_events_count_tools_and_tokens() -> None:
    module = load_module()
    run_dir = Path("/local/harness/20260604-120400-top-level-events")
    install_fake_artifacts(
        module,
        {
            run_dir.name: {
                "summary": {
                    "returncode": 0,
                    "duration_ms": 10,
                    "commands": [],
                    "gh_calls": [],
                    "usage": {},
                },
                "manifest": {"fake_responses": False},
                "events": [
                    {"type": "exec_command_begin"},
                    {"type": "browser_open_begin"},
                    {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "total_tokens": 15,
                                "input_tokens": 10,
                                "cached_input_tokens": 4,
                                "output_tokens": 3,
                                "reasoning_output_tokens": 2,
                            }
                        },
                    },
                ],
            }
        },
    )
    report = module.build_report([run_dir], duration_budget_ms=30_000)

    run = report["runs"][0]
    assert run["command_count"] == 1
    assert run["tool_call_count"] == 2
    assert run["token_estimate"] == 19
    assert run["token_source"] == "stdout.token_count"


def test_top_level_item_completed_command_counts_as_tool() -> None:
    module = load_module()
    run_dir = Path("/local/harness/20260604-120500-item-completed-command")
    install_fake_artifacts(
        module,
        {
            run_dir.name: {
                "summary": {"returncode": 0, "duration_ms": 10, "commands": [], "gh_calls": [], "usage": {}},
                "manifest": {"fake_responses": False},
                "events": [
                    {"type": "item.completed", "item": {"type": "command_execution", "command": "date"}}
                ],
            }
        },
    )
    report = module.build_report([run_dir], duration_budget_ms=30_000)

    run = report["runs"][0]
    assert run["tool_call_count"] == 1


def test_aggregates_groups() -> None:
    module = load_module()
    runs = [
        module.RunMetrics("run-a", "same", "deterministic", "fake_responses_api", None, True, 0, 100, "summary.duration_ms", 1, 2, 0, 0, 10, "summary.usage.total_tokens", "within_budget"),
        module.RunMetrics("run-b", "same", "deterministic", "fake_responses_api", None, True, 0, 300, "summary.duration_ms", 3, 4, 0, 0, 30, "summary.usage.total_tokens", "within_budget"),
    ]
    groups = module.group_runs(runs, duration_budget_ms=500)

    assert groups[0]["run_count"] == 2
    assert groups[0]["duration_ms_p50"] == 200
    assert groups[0]["duration_ms_p95"] == 300
    assert groups[0]["command_count_p50"] == 2
    assert groups[0]["tool_call_count_p95"] == 4
    assert groups[0]["token_estimate_p50"] == 20


def main() -> None:
    tests = [
        test_collects_deterministic_metrics_without_private_paths,
        test_marks_failures_and_budget_overages_as_advisory,
        test_missing_returncode_counts_as_failure,
        test_empty_gh_calls_still_indicates_fake_gh_mode,
        test_top_level_jsonl_events_count_tools_and_tokens,
        test_top_level_item_completed_command_counts_as_tool,
        test_aggregates_groups,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")


if __name__ == "__main__":
    main()
