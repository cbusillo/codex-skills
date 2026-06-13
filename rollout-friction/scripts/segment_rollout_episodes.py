#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Group rollout friction signal hits into reviewable episodes."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
ANALYZER_PATH = SCRIPT_DIR / "analyze_rollouts.py"


def load_analyzer() -> Any:
    spec = importlib.util.spec_from_file_location("analyze_rollouts", ANALYZER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load analyze_rollouts.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ANALYZER = load_analyzer()
SIGNALS_BY_NAME = {signal.name: signal for signal in ANALYZER.SIGNALS}
SUCCESS_RE = re.compile(
    r"\b(exit[_ -]?code\s*[:=]?\s*0|process exited with code 0|passed|succeeded|success|green|mergeable)\b",
    re.I,
)
FALSE_SUCCESS_RE = re.compile(r"(?i)(?:\bsuccess\b|['\"]success['\"])\s*[:=]\s*(?:false|0|null|no)\b")
FAILURE_RE = re.compile(
    r"\b(error|failed|failure|exit[_ -]?code\s*[:=]?\s*[1-9][0-9]*|process exited with code [1-9][0-9]*|timed out|timeout|blocked|rate limit|stale)\b",
    re.I,
)
USER_CORRECTION_RE = re.compile(
    r"\b(wrong issue|wrong task|not what i asked|i meant|to be clear|disagree|you are wrong|please stop|hold on)\b",
    re.I,
)
TOOL_HINT_RE = re.compile(r"\b(shell|command|tool|gh|git|uv run|pytest|jq|curl|agent|browser|apply_patch)\b", re.I)


@dataclass
class EventHit:
    signal: str
    line: int
    snippet: str
    structured: bool
    severity: str
    category: str
    destination: str
    likely_cause: str


@dataclass
class TraceLine:
    line: int
    snippet: str
    structured: bool


@dataclass
class Episode:
    source_file: Path
    source_file_id: str
    start_line: int
    end_line: int
    hits: list[EventHit] = field(default_factory=list)
    event_count: int = 0
    retry_count: int = 0
    tool_call_count: int = 0
    failure_count: int = 0
    outcome: str = "unknown"
    outcome_evidence_line: int | None = None
    outcome_evidence_snippet: str | None = None
    cost_score: int = 0
    episode_id: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Group rollout friction hits into episodes.")
    parser.add_argument("paths", nargs="*", type=Path, help="Files or directories to scan.")
    parser.add_argument("--paths-file", type=Path, help="Read newline- or NUL-delimited trace paths from this file.")
    parser.add_argument("--root", type=Path, help="Directory to scan when no paths are provided.")
    parser.add_argument("--max-files", type=int, default=ANALYZER.DEFAULT_MAX_FILES)
    parser.add_argument("--max-bytes", type=int, default=ANALYZER.DEFAULT_MAX_BYTES)
    parser.add_argument("--max-file-bytes", type=int)
    parser.add_argument("--context-chars", type=int, default=240)
    parser.add_argument("--max-gap-lines", type=int, default=25)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--suppress-investigation-noise", action="store_true")
    parser.add_argument("--json", action="store_true", help="Emit a JSON object instead of JSONL episodes.")
    args = parser.parse_args()
    if args.max_file_bytes is None:
        args.max_file_bytes = args.max_bytes
    return args


def load_paths(args: argparse.Namespace) -> list[Path]:
    paths = list(args.paths)
    if args.paths_file:
        paths.extend(ANALYZER.load_paths_file(args.paths_file, argparse.ArgumentParser()))
    if not paths and args.root:
        paths = [args.root]
    if not paths:
        raise SystemExit("provide at least one trace path, --paths-file, or --root")
    return paths


def scan_targets(args: argparse.Namespace) -> tuple[list[Any], list[Any]]:
    files, limitations = ANALYZER.iter_candidate_files(load_paths(args), args.max_files)
    targets, scan_limitations = ANALYZER.plan_scan_targets(files, args.max_bytes, args.max_file_bytes)
    return targets, [*limitations, *scan_limitations]


def collect_hits_and_lines(target: Any, args: argparse.Namespace) -> tuple[list[EventHit], list[TraceLine]]:
    since_ts = ANALYZER.parse_timestamp_arg(args.since, "--since") if args.since else None
    until_ts = ANALYZER.parse_timestamp_arg(args.until, "--until") if args.until else None
    hits: list[EventHit] = []
    trace_lines: list[TraceLine] = []
    seen: set[tuple[int, str, str, int]] = set()
    line_signal_texts: dict[tuple[int, str], set[str]] = {}
    for line_no, text, is_summary, is_structured in ANALYZER.iter_lines(
        target.path, target.read_bytes, since_ts, until_ts, None, None
    ):
        if ANALYZER.is_meta_echo(text):
            continue
        if args.suppress_investigation_noise and not is_structured and ANALYZER.is_suppressed_noise(text):
            continue
        snippet = ANALYZER.redacted(text, args.context_chars)
        trace_lines.append(TraceLine(line=line_no, snippet=snippet, structured=is_structured))
        matched_auth_login_noise = bool(ANALYZER.AUTH_LOGIN_NOISE_RE.search(text))
        canonical_text = ANALYZER.canonical_hit_text(text)
        for signal in ANALYZER.SIGNALS:
            if matched_auth_login_noise and signal.name in {
                "github_graphql_rate_limit",
                "github_rest_rate_limit",
                "repeated_command_failure",
                "missing_dependency_or_tool",
                "generic_rate_limit",
            }:
                continue
            if signal.name == "auth_login_loop" and not matched_auth_login_noise:
                continue
            for occurrence, match in enumerate(signal.pattern.finditer(text)):
                if ANALYZER.should_skip_signal_match(signal.name, text, canonical_text, match):
                    continue
                line_key = (line_no, signal.name)
                line_texts = line_signal_texts.setdefault(line_key, set())
                if is_summary and ANALYZER.summary_only_repeats_seen_values(canonical_text, line_texts):
                    continue
                hit_key = (line_no, signal.name, canonical_text, occurrence)
                if hit_key in seen:
                    continue
                seen.add(hit_key)
                line_texts.add(canonical_text)
                hits.append(
                    EventHit(
                        signal=signal.name,
                        line=line_no,
                        snippet=snippet,
                        structured=is_structured,
                        severity=signal.severity,
                        category=signal.category,
                        destination=signal.destination,
                        likely_cause=signal.likely_cause,
                    )
                )
    signal_counts: dict[str, int] = {}
    for item in hits:
        signal_counts[item.signal] = signal_counts.get(item.signal, 0) + 1
    filtered_hits = [
        item for item in hits if signal_counts[item.signal] >= getattr(SIGNALS_BY_NAME[item.signal], "threshold", 1)
    ]
    return sorted(filtered_hits, key=lambda hit: hit.line), trace_lines


def build_episodes(
    target: Any,
    hits: list[EventHit],
    max_gap_lines: int,
    trace_lines: list[TraceLine] | None = None,
) -> list[Episode]:
    if not hits:
        return []
    episodes: list[Episode] = []
    current = Episode(
        source_file=target.path,
        source_file_id=ANALYZER.stable_file_id(target.path),
        start_line=hits[0].line,
        end_line=hits[0].line,
        hits=[hits[0]],
    )
    for hit in hits[1:]:
        if hit.line - current.end_line <= max_gap_lines:
            current.hits.append(hit)
            current.end_line = hit.line
            continue
        episodes.append(current)
        current = Episode(
            source_file=target.path,
            source_file_id=ANALYZER.stable_file_id(target.path),
            start_line=hit.line,
            end_line=hit.line,
            hits=[hit],
        )
    episodes.append(current)
    for index, episode in enumerate(episodes):
        previous_episode = episodes[index - 1] if index else None
        next_episode = episodes[index + 1] if index + 1 < len(episodes) else None
        finalize_episode(episode, trace_lines or [], max_gap_lines, previous_episode, next_episode)
    return episodes


def finalize_episode(
    episode: Episode,
    trace_lines: list[TraceLine],
    max_gap_lines: int,
    previous_episode: Episode | None = None,
    next_episode: Episode | None = None,
) -> None:
    window = outcome_window(episode, trace_lines, max_gap_lines, previous_episode, next_episode)
    episode.event_count = len(episode.hits)
    episode.retry_count = count_matching(window, r"\b(retry|rerun|again|confirm|attempt)\b")
    episode.tool_call_count = sum(1 for item in window if TOOL_HINT_RE.search(item.snippet))
    episode.failure_count = sum(1 for item in window if FAILURE_RE.search(item.snippet))
    episode.outcome, episode.outcome_evidence_line, episode.outcome_evidence_snippet = detect_outcome(episode, window)
    episode.cost_score = cost_score(episode)
    episode.episode_id = episode_fingerprint(episode)


def count_matching(items: list[TraceLine], pattern: str) -> int:
    regex = re.compile(pattern, re.I)
    return sum(1 for item in items if regex.search(item.snippet))


def outcome_window(
    episode: Episode,
    trace_lines: list[TraceLine],
    max_gap_lines: int,
    previous_episode: Episode | None = None,
    next_episode: Episode | None = None,
) -> list[TraceLine]:
    if not trace_lines:
        return [TraceLine(line=hit.line, snippet=hit.snippet, structured=hit.structured) for hit in episode.hits]
    start = max(0, episode.start_line - max_gap_lines)
    end = episode.end_line + max_gap_lines
    if previous_episode is not None:
        midpoint = (previous_episode.end_line + episode.start_line) // 2
        start = max(start, midpoint + 1)
    if next_episode is not None:
        midpoint = (episode.end_line + next_episode.start_line) // 2
        end = min(end, midpoint)
    return [item for item in trace_lines if start <= item.line <= end]


def detect_outcome(episode: Episode, window: list[TraceLine]) -> tuple[str, int | None, str | None]:
    for hit in episode.hits:
        if hit.signal == "user_context_correction":
            return "user_corrected", hit.line, hit.snippet
    for item in window:
        if USER_CORRECTION_RE.search(item.snippet):
            return "user_corrected", item.line, item.snippet
    for item in reversed(window):
        if is_success_snippet(item.snippet):
            outcome = "resolved_after_retries" if episode.failure_count or episode.retry_count else "resolved_immediately"
            return outcome, item.line, item.snippet
    if episode.failure_count or episode.retry_count:
        return "unresolved", episode.hits[-1].line, episode.hits[-1].snippet
    return "unknown", None, None


def is_success_snippet(snippet: str) -> bool:
    return bool(SUCCESS_RE.search(snippet) and not FAILURE_RE.search(snippet) and not FALSE_SUCCESS_RE.search(snippet))


def cost_score(episode: Episode) -> int:
    severity_weight = {"high": 20, "medium": 10, "low": 4}
    max_severity = max((severity_weight.get(hit.severity, 1) for hit in episode.hits), default=1)
    unresolved = 12 if episode.outcome == "unresolved" else 0
    corrected = 8 if episode.outcome == "user_corrected" else 0
    return (
        max_severity
        + episode.event_count
        + episode.retry_count * 4
        + episode.tool_call_count * 2
        + episode.failure_count * 3
        + unresolved
        + corrected
    )


def episode_fingerprint(episode: Episode) -> str:
    payload = {
        "file_id": episode.source_file_id,
        "start_line": episode.start_line,
        "end_line": episode.end_line,
        "signals": sorted({hit.signal for hit in episode.hits}),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:20]
    return f"ep_{digest}"


def primary_hit(episode: Episode) -> EventHit:
    severity_rank = {"high": 3, "medium": 2, "low": 1}
    return max(episode.hits, key=lambda hit: (severity_rank.get(hit.severity, 0), -hit.line))


def episode_to_json(episode: Episode) -> dict[str, Any]:
    primary = primary_hit(episode)
    return {
        "schema_version": 1,
        "episode_id": episode.episode_id,
        "source_file_id": episode.source_file_id,
        "source_suffix": episode.source_file.suffix,
        "start_line": episode.start_line,
        "end_line": episode.end_line,
        "primary_signal": primary.signal,
        "signals": sorted({hit.signal for hit in episode.hits}),
        "severity": primary.severity,
        "category": primary.category,
        "recommended_destination": primary.destination,
        "likely_cause": primary.likely_cause,
        "outcome": episode.outcome,
        "outcome_evidence_line": episode.outcome_evidence_line,
        "outcome_evidence_snippet": episode.outcome_evidence_snippet,
        "cost": {
            "event_count": episode.event_count,
            "retry_count": episode.retry_count,
            "tool_call_count": episode.tool_call_count,
            "failure_count": episode.failure_count,
        },
        "cost_score": episode.cost_score,
        "hits": [
            {
                "signal": hit.signal,
                "line": hit.line,
                "snippet": hit.snippet,
                "evidence_type": "structured_payload" if hit.structured else "broad_context",
            }
            for hit in episode.hits
        ],
    }


def main() -> int:
    args = parse_args()
    targets, limitations = scan_targets(args)
    episodes: list[Episode] = []
    for target in targets:
        hits, trace_lines = collect_hits_and_lines(target, args)
        episodes.extend(build_episodes(target, hits, args.max_gap_lines, trace_lines))
    payloads = [episode_to_json(episode) for episode in episodes]
    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "episode_count": len(payloads),
                    "scan_limitations": [ANALYZER.limitation_to_json(limitation) for limitation in limitations],
                    "episodes": payloads,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    for payload in payloads:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
