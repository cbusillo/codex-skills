#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Collect public-safe performance summaries from local exec-harness artifacts."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import statistics
import sys
from dataclasses import dataclass
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_HARNESS_ROOT = ROOT.parent / "code" / ".tmp" / "code-exec-harness"
DEFAULT_DURATION_BUDGET_MS = 30_000
BEGIN_TOOL_EVENT_TYPES = {"browser_open_begin", "exec_command_begin"}


@dataclass
class RunMetrics:
    run_dir_label: str
    scenario: str | None
    mode: str
    model_family: str
    temperature: str | None
    passed: bool
    returncode: int | None
    duration_ms: int | None
    duration_source: str
    command_count: int
    tool_call_count: int
    retry_count: int
    failure_count: int
    token_estimate: int | None
    token_source: str
    budget_status: str

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "run_dir_label": self.run_dir_label,
            "scenario": self.scenario,
            "mode": self.mode,
            "model_family": self.model_family,
            "temperature": self.temperature,
            "passed": self.passed,
            "returncode": self.returncode,
            "duration_source": self.duration_source,
            "command_count": self.command_count,
            "tool_call_count": self.tool_call_count,
            "retry_count": self.retry_count,
            "failure_count": self.failure_count,
            "token_source": self.token_source,
            "budget_status": self.budget_status,
        }
        if self.duration_ms is not None:
            data["duration_ms"] = self.duration_ms
        if self.token_estimate is not None:
            data["token_estimate"] = self.token_estimate
        return data


def read_json(path: pathlib.Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def iter_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    events.append(value)
    except FileNotFoundError:
        pass
    return events


def repo_relative(path_value: str | None) -> str | None:
    if not path_value:
        return None
    path = pathlib.Path(path_value)
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except (OSError, ValueError):
        return path.name


def scenario_name(path_value: str | None) -> str | None:
    relative = repo_relative(path_value)
    if relative:
        return pathlib.Path(relative).stem
    return None


def event_message(event: dict[str, Any]) -> dict[str, Any]:
    msg = event.get("msg")
    if isinstance(msg, dict):
        return msg
    return event


def non_bool_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def usage_total_from_parts(usage: dict[str, Any]) -> int | None:
    fields = ["input_tokens", "output_tokens"]
    parts: list[int] = []
    for field in fields:
        value = non_bool_int(usage.get(field))
        if value is None:
            return None
        parts.append(value)
    cached = non_bool_int(usage.get("cached_input_tokens")) or 0
    reasoning = non_bool_int(usage.get("reasoning_output_tokens")) or 0
    parts.append(cached)
    parts.append(reasoning)
    return sum(parts)


def event_completed_item_type(event: dict[str, Any]) -> str | None:
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("item_type") or item.get("type")
    return item_type if isinstance(item_type, str) else None


def count_begin_tools(events: list[dict[str, Any]]) -> int:
    count = 0
    for event in events:
        msg_type = event_message(event).get("type")
        if msg_type in BEGIN_TOOL_EVENT_TYPES:
            count += 1
    return count


def count_completed_tools(events: list[dict[str, Any]]) -> int:
    return sum(
        1
        for event in events
        if event_completed_item_type(event) in {"command_execution", "tool_call"}
    )


def count_tool_calls(events: list[dict[str, Any]]) -> int:
    return max(count_begin_tools(events), count_completed_tools(events))


def count_completed_commands(events: list[dict[str, Any]]) -> int:
    return sum(1 for event in events if event_completed_item_type(event) == "command_execution")


def token_usage_from_events(events: list[dict[str, Any]]) -> int | None:
    best: int | None = None
    for event in events:
        msg = event_message(event)
        if msg.get("type") != "token_count":
            continue
        info = msg.get("info")
        if not isinstance(info, dict):
            continue
        usage = info.get("total_token_usage")
        if not isinstance(usage, dict):
            continue
        total = non_bool_int(usage.get("total_tokens"))
        parts_total = usage_total_from_parts(usage)
        candidates = [value for value in (total, parts_total) if value is not None]
        if candidates:
            best = max(best or 0, max(candidates))
    return best


def token_usage(summary: dict[str, Any], events: list[dict[str, Any]]) -> tuple[int | None, str]:
    usage = summary.get("usage")
    if isinstance(usage, dict):
        total = non_bool_int(usage.get("total_tokens"))
        parts_total = usage_total_from_parts(usage)
        if parts_total is not None and (total is None or parts_total > total):
            return parts_total, "summary.usage.parts"
        if total is not None:
            return total, "summary.usage.total_tokens"
        if parts_total is not None:
            return parts_total, "summary.usage.parts"
    event_total = token_usage_from_events(events)
    if event_total is not None:
        return event_total, "stdout.token_count"
    return None, "unavailable"


def duration_from_summary(summary: dict[str, Any]) -> tuple[int | None, str]:
    for key in ("duration_ms", "elapsed_ms", "wall_time_ms"):
        value = summary.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value, f"summary.{key}"
    return None, "unavailable"


def duration_from_mtime(run_dir: pathlib.Path) -> tuple[int | None, str]:
    artifacts = run_dir / "artifacts"
    summary_path = artifacts / "summary.json"
    stdout_path = artifacts / "stdout.jsonl"
    try:
        start = stdout_path.stat().st_mtime
        end = summary_path.stat().st_mtime
    except OSError:
        return None, "unavailable"
    duration = max(0, int(round((end - start) * 1000)))
    return duration, "file_mtime_fallback"


def infer_mode(manifest: dict[str, Any], summary: dict[str, Any]) -> tuple[str, str]:
    fake_responses = bool(manifest.get("fake_responses"))
    gh_calls = summary.get("gh_calls")
    has_fake_gh = isinstance(gh_calls, list) and (bool(gh_calls) or summary.get("gh_state") is not None)
    if fake_responses and has_fake_gh:
        return "deterministic", "fake_responses_api+fake_gh"
    if fake_responses:
        return "deterministic", "fake_responses_api"
    if has_fake_gh:
        return "deterministic", "fake_gh"
    return "local_llm", "local_provider"


def failure_count(summary: dict[str, Any], returncode: int | None) -> int:
    count = 0
    if returncode is None:
        count += 1
    elif returncode != 0:
        count += 1
    for key in ("expectation_failures", "errors"):
        value = summary.get(key)
        if isinstance(value, list):
            count += len(value)
    return count


def collect_run(run_dir: pathlib.Path, *, duration_budget_ms: int) -> RunMetrics | None:
    artifacts = run_dir / "artifacts"
    summary = read_json(artifacts / "summary.json")
    if not isinstance(summary, dict):
        return None
    manifest = read_json(artifacts / "manifest.json")
    manifest = manifest if isinstance(manifest, dict) else {}
    events = iter_jsonl(artifacts / "stdout.jsonl")
    mode, model_family = infer_mode(manifest, summary)
    scenario = scenario_name(manifest.get("scenario") if isinstance(manifest.get("scenario"), str) else None)
    returncode = summary.get("returncode")
    returncode = returncode if isinstance(returncode, int) and not isinstance(returncode, bool) else None
    duration_ms, duration_source = duration_from_summary(summary)
    if duration_ms is None:
        duration_ms, duration_source = duration_from_mtime(run_dir)
    commands = summary.get("commands")
    command_count = len(commands) if isinstance(commands, list) else 0
    tool_call_count = count_tool_calls(events)
    if command_count == 0:
        command_count = max(
            sum(1 for event in events if event_message(event).get("type") == "exec_command_begin"),
            count_completed_commands(events),
        )
    token_estimate, token_source = token_usage(summary, events)
    failures = failure_count(summary, returncode)
    passed = failures == 0 and returncode == 0
    if duration_ms is None:
        budget_status = "unknown"
    elif duration_ms <= duration_budget_ms:
        budget_status = "within_budget"
    else:
        budget_status = "over_budget_advisory"
    return RunMetrics(
        run_dir_label=run_dir.name,
        scenario=scenario,
        mode=mode,
        model_family=model_family,
        temperature=None,
        passed=passed,
        returncode=returncode,
        duration_ms=duration_ms,
        duration_source=duration_source,
        command_count=command_count,
        tool_call_count=tool_call_count,
        retry_count=0,
        failure_count=failures,
        token_estimate=token_estimate,
        token_source=token_source,
        budget_status=budget_status,
    )


def percentile(values: list[int], percent: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    if percent == 50:
        return int(round(statistics.median(ordered)))
    index = min(len(ordered) - 1, max(0, int(round((percent / 100) * (len(ordered) - 1)))))
    return ordered[index]


def group_runs(runs: list[RunMetrics], *, duration_budget_ms: int) -> list[dict[str, Any]]:
    groups: dict[tuple[str | None, str, str], list[RunMetrics]] = {}
    for run in runs:
        groups.setdefault((run.scenario, run.mode, run.model_family), []).append(run)
    output: list[dict[str, Any]] = []
    for (scenario, mode, model_family), group in sorted(groups.items(), key=lambda item: str(item[0])):
        durations = [run.duration_ms for run in group if run.duration_ms is not None]
        tokens = [run.token_estimate for run in group if run.token_estimate is not None]
        commands = [run.command_count for run in group]
        tools = [run.tool_call_count for run in group]
        fail_count = sum(1 for run in group if not run.passed)
        data: dict[str, Any] = {
            "scenario": scenario,
            "mode": mode,
            "model_family": model_family,
            "run_count": len(group),
            "pass_count": len(group) - fail_count,
            "fail_count": fail_count,
            "failure_rate": fail_count / len(group),
            "retry_count": sum(run.retry_count for run in group),
            "retry_rate": sum(run.retry_count for run in group) / len(group),
            "duration_ms_budget": duration_budget_ms,
            "budget_status": "within_budget",
            "command_count_p50": percentile(commands, 50),
            "command_count_p95": percentile(commands, 95),
            "tool_call_count_p50": percentile(tools, 50),
            "tool_call_count_p95": percentile(tools, 95),
        }
        p50 = percentile(durations, 50)
        p95 = percentile(durations, 95)
        if p50 is not None:
            data["duration_ms_p50"] = p50
        if p95 is not None:
            data["duration_ms_p95"] = p95
        if p95 is None:
            data["budget_status"] = "unknown"
        elif p95 > duration_budget_ms:
            data["budget_status"] = "over_budget_advisory"
        token_p50 = percentile(tokens, 50)
        token_p95 = percentile(tokens, 95)
        if token_p50 is not None:
            data["token_estimate_p50"] = token_p50
        if token_p95 is not None:
            data["token_estimate_p95"] = token_p95
        output.append(data)
    return output


def discover_runs(paths: list[pathlib.Path], *, latest: int | None) -> list[pathlib.Path]:
    run_dirs: list[pathlib.Path] = []
    for path in paths:
        if (path / "artifacts" / "summary.json").is_file():
            run_dirs.append(path)
            continue
        if path.is_dir():
            run_dirs.extend(child for child in path.iterdir() if (child / "artifacts" / "summary.json").is_file())
    run_dirs = sorted(set(run_dirs), key=lambda item: item.name)
    if latest is not None:
        run_dirs = run_dirs[-latest:]
    return run_dirs


def build_report(run_dirs: list[pathlib.Path], *, duration_budget_ms: int) -> dict[str, Any]:
    runs = [run for run in (collect_run(path, duration_budget_ms=duration_budget_ms) for path in run_dirs) if run]
    fail_count = sum(1 for run in runs if not run.passed)
    report = {
        "schema_version": 1,
        "artifact_policy": "local_only",
        "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "summary": {
            "runs": len(runs),
            "passed": len(runs) - fail_count,
            "failed": fail_count,
            "failure_rate": (fail_count / len(runs)) if runs else 0.0,
            "retry_rate": 0.0,
        },
        "groups": group_runs(runs, duration_budget_ms=duration_budget_ms),
        "runs": [run.as_dict() for run in runs],
    }
    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=pathlib.Path, help="Harness run dirs or a root containing run dirs.")
    parser.add_argument("--latest", type=int, help="Only include the latest N discovered run directories.")
    parser.add_argument("--duration-budget-ms", type=int, default=DEFAULT_DURATION_BUDGET_MS)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    paths = args.paths or [DEFAULT_HARNESS_ROOT]
    run_dirs = discover_runs(paths, latest=args.latest)
    report = build_report(run_dirs, duration_budget_ms=args.duration_budget_ms)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
