#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Local rollout/session friction scanner.

The scanner is intentionally conservative: it emits compact, redacted findings
from local traces instead of trying to reconstruct a full private transcript.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, NamedTuple


DEFAULT_MAX_FILES = 25
DEFAULT_MAX_BYTES = 3_000_000
DEFAULT_CONTEXT_CHARS = 180

ROLL_OUT_SUFFIXES = {".jsonl", ".json", ".log", ".txt", ".md"}
STRUCTURED_TRACE_SUFFIXES = {".json", ".jsonl", ".log"}
ROLL_OUT_NAME_RE = re.compile(r"(rollout|session|runout|thread|trace|transcript)", re.I)
SKILL_DOC_NAMES = {"SKILL.md", "README.md"}
SECRET_RE = re.compile(
    r"(?i)(ghp_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-ant-[A-Za-z0-9_-]+|"
    r"sk-[A-Za-z0-9_-]+|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]+|"
    r"(?:export\s+)?(?:api[_-]?key|token|secret|password|credential)\s*[:=]\s*[^\s,'\"]+)"
)
PATH_RE = re.compile(
    r"(?:"
    r"~(?:/[^\s,'\"]+)?|"
    r"/(?:"
    r"Users|home|var|tmp|private|Volumes|opt|etc|usr|bin|sbin|lib|lib64|"
    r"srv|run|mnt|media|dev|proc|sys|workspace|workspaces|app"
    r")/[^\s,'\"]+|"
    r"(?:\.\.?/)+[^\s,'\"]+|"
    r"(?:[A-Za-z0-9_.-]+/){2,}[A-Za-z0-9_.-]+|"
    r"[A-Za-z]:\\[^\s,'\"]+"
    r")"
)
URL_AUTH_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@[^\s]+", re.I)
HOST_RE = re.compile(r"\b(?:[a-z0-9-]+\.){2,}[a-z]{2,}\b", re.I)
LOCAL_HOST_RE = re.compile(
    r"\b(?:localhost|host\.docker\.internal|[a-z0-9-]+\.(?:local|localhost|internal|test))\b",
    re.I,
)
META_ECHO_RE = re.compile(
    r"Review only the provided code change scope\. Identify critical bugs|"
    r"<user_action>\s*<context>User initiated a review task\.|"
    r"^Auto Review$|"
    r"^@@\s+-\d+,\d+\s+\+\d+,\d+\s+@@|"
    r"^exit_code=(?:1|2|128)$|"
    r"^\u274c Validate New Code:|"
    r"^Traceback \(most recent call last\):|"
    r"\"findings\"\s*:\s*\[\s*\{\s*\"title\"\s*:\s*\"\[P\d\]|"
    r"\b(recommended_destination|likely_cause|scanned_files)\b|"
    r"\b(signal|severity|category|evidence)\b[^\n]{0,160}\b(recommended_destination|likely_cause)\b|"
    r"\b(GitHub REST or GraphQL rate-limit pressure|GitHub REST usage also hit quota|"
    r"A workflow likely used GraphQL-heavy GitHub commands|rate-limit pressure\b[^\n]{0,160}\bskill)\b|"
    r"---\s*name:\s*github\s+description:[^\n]{0,240}\b(GraphQL|rate limit)\b|"
    r"---\s*name:\s*github-plan\s+description:[^\n]{0,1200}\b(GraphQL|rate limit)\b|"
    r"\b(comprehensive GitHub Expert persona|gh helper|GitHub helper)\b[^\n]{0,240}\b(GraphQL|rate limit)\b|"
    r"\b(helper is REST-first|Before batching those operations, check rate limits)\b|"
    r"^\s*---\s*(?:\\n|\s)+name:\s+[a-z0-9_-]+\s*(?:\\n|\s)+description:|"
    r"^\s*\([^\n]{0,80}error\|failed\|blocked\|timeout\|timed out\|rate limit\|GraphQL\|retry|"
    r"^I used `rollout-friction` read-only\.|"
    r"^\d+\.\s+(?:Patch|Add tests|Add regression|Document|Investigate)\b[^\n]{0,240}"
    r"(?:GraphQL|rate limit|No runs found|mergeable UNKNOWN|blocked|Auto Review)",
    re.I,
)
INVESTIGATION_NOISE_RE = re.compile(
    r"\b(analyze_rollouts\.py|validate_analyze_rollouts\.py|rollout-friction)\b[^\n]{0,240}"
    r"\b(GraphQL|rate limit|error|failed|blocked|timeout|grep|pattern|signal)\b|"
    r"\bgrep\b[^\n]{0,160}\b(GraphQL|rate limit|error|failed|blocked|timeout)\b|"
    r"\bassistant\b[^\n]{0,160}\b(discussion|summary|mentioned|investigation)\b",
    re.I,
)
AUTH_LOGIN_NOISE_RE = re.compile(
    r"\b("
    r"remote control|refresh_token_reused|Authentication expired|token_revoked|"
    r"Multi-factor authentication required|auth recovery|login problem|"
    r"logout/restart|iPhone now shows"
    r")\b",
    re.I,
)
DIFF_OR_STATIC_CONTEXT_RE = re.compile(
    r"\bdiff --git\b|(?:^|\n)\s*(?:[A-Za-z_]+\=)?@@\s+-\d|"
    r"(?:^|\n)\s*(?:[A-Za-z_]+\=)?index [0-9a-f]{7,}\.{2}[0-9a-f]{7,}\b|"
    r"(?:^|\n)\s*(?:[A-Za-z_]+\=)?--- [ab]/|(?:^|\n)\s*(?:[A-Za-z_]+\=)?\+\+\+ [ab]/|"
    r"(?:^|\n)\s*(?:[A-Za-z_]+\=)?name:\s+[^\n]{0,80}\n\s*on:\s*[^\n]*(?:\n|$)|"
    r"(?:^|\n)\s*(?:[A-Za-z_]+\=)?push:\s*\n\s*branches:\b",
    re.I,
)
AUTO_REVIEW_TEXT_RE = re.compile(r"\bauto[-\s]?review\b", re.I)
STATIC_CONFIG_LINE_RE = re.compile(
    r"^\s*(?:[-+]\s*)?(?:#|[-*]\s+|"
    r"(?:name|on|push|pull_request|branches|jobs|steps|run|uses|with|env|permissions|"
    r"workflow_dispatch|concurrency|strategy|matrix|needs|if|shell|working-directory)\s*:|"
    r"-\s*(?:run|uses|name|with)\s*:)",
    re.I,
)
INTERESTING_JSON_KEYS = (
    "type",
    "message",
    "text",
    "content",
    "aggregated_output",
    "stdout",
    "stderr",
    "formatted_output",
    "error",
    "exit_code",
    "status",
    "command",
    "error_reason",
    "capture_incomplete",
    "cleanup",
    "wait",
    "mergeable",
    "mergeable_state",
    "statusCheckRollup",
)
DEDUP_VALUE_PREFIXES = (
    "message=",
    "text=",
    "content=",
    "aggregated_output=",
    "stdout=",
    "stderr=",
    "formatted_output=",
    "error=",
    "command=",
)
STRUCTURED_PAYLOAD_KEYS = {
    "error_reason",
    "capture_incomplete",
    "cleanup",
    "wait",
    "exit_code",
    "error",
}
NESTED_JSON_STRING_KEYS = {
    "output",
    "aggregated_output",
    "stdout",
    "stderr",
    "formatted_output",
    "content",
    "message",
}


class Signal:
    __slots__ = (
        "name",
        "severity",
        "category",
        "pattern",
        "destination",
        "likely_cause",
        "threshold",
    )

    name: str
    severity: str
    category: str
    pattern: re.Pattern[str]
    destination: str
    likely_cause: str
    threshold: int

    def __init__(
        self,
        name: str,
        severity: str,
        category: str,
        pattern: re.Pattern[str],
        destination: str,
        likely_cause: str,
        threshold: int = 1,
    ) -> None:
        self.name = name
        self.severity = severity
        self.category = category
        self.pattern = pattern
        self.destination = destination
        self.likely_cause = likely_cause
        self.threshold = threshold


class Hit:
    __slots__ = ("file", "line", "snippet", "structured")

    file: Path
    line: int
    snippet: str
    structured: bool

    def __init__(self, file: Path, line: int, snippet: str, structured: bool = False) -> None:
        self.file = file
        self.line = line
        self.snippet = snippet
        self.structured = structured


class Finding:
    __slots__ = ("signal", "hits", "files")

    signal: Signal

    def __init__(self, signal: Signal) -> None:
        self.signal = signal
        self.hits: list[Hit] = []
        self.files: Counter[str] = Counter()

    def add(self, hit: Hit) -> None:
        self.hits.append(hit)
        self.files[str(hit.file)] += 1

    @property
    def count(self) -> int:
        return len(self.hits)

    @property
    def structured_count(self) -> int:
        return sum(1 for hit in self.hits if hit.structured)


class Fragment(NamedTuple):
    text: str
    summary: bool
    structured: bool = False


SIGNALS: list[Signal] = [
    Signal(
        "github_graphql_rate_limit",
        "high",
        "tool-pressure",
        re.compile(r"graphql[^\n]{0,120}(rate limit|quota|exhaust|secondary rate)|rate limit[^\n]{0,120}graphql", re.I),
        "fix-script-or-helper",
        "A workflow likely used GraphQL-heavy GitHub commands or polling under quota pressure.",
    ),
    Signal(
        "github_rest_rate_limit",
        "high",
        "tool-pressure",
        re.compile(r"\bREST\b[^\n]{0,120}(rate limit|quota|exhaust|secondary rate)|rate limit[^\n]{0,120}\bREST\b", re.I),
        "fix-script-or-helper",
        "GitHub REST usage also hit quota or secondary limits; polling or broad listing may need throttling/cache behavior.",
    ),
    Signal(
        "generic_rate_limit",
        "medium",
        "tool-pressure",
        re.compile(r"rate limit|quota exceeded|secondary rate|too many requests|HTTP 429", re.I),
        "investigate-repo-workflow",
        "A tool or service reported quota pressure; classify the specific service before changing durable behavior.",
        threshold=2,
    ),
    Signal(
        "auth_login_loop",
        "medium",
        "environment-friction",
        re.compile(
            r"refresh_token_reused|Authentication expired|token_revoked|"
            r"Multi-factor authentication required|auth recovery|"
            r"remote control[^\n]{0,160}(failed|forbidden|expired|revoked|reused|auth recovery|authentication required|login problem)",
            re.I,
        ),
        "move-to-local-config",
        "Login or account state explained apparent tool failures; keep private details local unless a reusable diagnostic would prevent repeated investigation.",
        threshold=2,
    ),
    Signal(
        "auto_review_loop",
        "high",
        "review-friction",
        re.compile(r"Auto Review|auto-review|worktree path: .*/auto-review|Merge .*auto-review", re.I),
        "fix-harness",
        "Review feedback repeated enough to create workflow drag; inspect whether findings were stale, valid, or caused by missing invariants.",
        threshold=2,
    ),
    Signal(
        "stale_results",
        "medium",
        "validation-friction",
        re.compile(r"stale_results|results_may_be_stale|cached findings withheld", re.I),
        "fix-harness",
        "A validator returned stale evidence, which can mislead readiness decisions.",
    ),
    Signal(
        "user_context_correction",
        "medium",
        "context-drift",
        re.compile(r"\b(we were talking about|you forgot|you never|that isn't what|not what I asked|why did you)\b", re.I),
        "promote-to-skill",
        "The user corrected task focus or memory; inspect for a durable instruction, closeout checklist, or harness reminder.",
    ),
    Signal(
        "repeated_command_failure",
        "medium",
        "execution-friction",
        re.compile(
            r"exit_code\s*[=:]\s*[1-9]|\"exit_code\"\s*:\s*[1-9]|Command failed|"
            r"process exited with code [1-9]|error:",
            re.I,
        ),
        "fix-script-or-helper",
        "Commands or tools failed repeatedly; repeated failures often deserve a helper, guardrail, or clearer skill instruction.",
        threshold=3,
    ),
    Signal(
        "github_workflow_wait_miss",
        "medium",
        "tool-pressure",
        re.compile(r"No runs found for workflow|gh_run_wait[^\n]{0,120}No runs found", re.I),
        "fix-script-or-helper",
        "A workflow wait helper could not resolve the intended Actions run; prefer PR/check-run oriented waiting when workflow names are unstable.",
    ),
    Signal(
        "github_pr_rollup_lag",
        "medium",
        "tool-pressure",
        re.compile(
            r"mergeable[\"'=:\s]+UNKNOWN|mergeable_state[\"'=:\s]+unknown|"
            r"statusCheckRollup[^\n]{0,240}(IN_PROGRESS|QUEUED|in_progress|queued)|"
            r"CodeQL[^\n]{0,240}(IN_PROGRESS|QUEUED|in_progress|queued)",
            re.I,
        ),
        "fix-script-or-helper",
        "PR readiness depended on lagging mergeability or check-rollup state; a PR-aware wait path may reduce manual polling.",
        threshold=2,
    ),
    Signal(
        "blocked_git_safety_prompt",
        "low",
        "execution-friction",
        re.compile(r"Blocked git (switch|checkout)|Resend with 'confirm:'|confirm: git", re.I),
        "investigate-repo-workflow",
        "Git safety prompts protected worktrees but added retry friction; inspect whether branch setup can be more deliberate before command execution.",
    ),
    Signal(
        "shell_quoting_or_parse_error",
        "low",
        "execution-friction",
        re.compile(r"zsh:[^\n]*(unmatched|parse error)|shell quoting|unexpected EOF", re.I),
        "fix-script-or-helper",
        "A shell command failed before doing useful work; structured helper arguments or safer quoting could avoid the retry.",
    ),
    Signal(
        "auto_review_valid_finding",
        "low",
        "review-friction",
        re.compile(r"auto-review[^\n]{0,240}(legitimate|valid|applied|fix)|Auto Review: [1-9] issue", re.I),
        "ignore-noise",
        "Auto-review created an extra decision point but produced useful feedback; usually no durable change is needed unless loops recur.",
    ),
    Signal(
        "missing_dependency_or_tool",
        "medium",
        "environment-friction",
        re.compile(r"command not found|No such file or directory|module not found|No open .* project matched|Unable to load", re.I),
        "move-to-local-config",
        "The environment was missing a tool, file, project route, or dependency; decide whether this is local config or harness setup.",
    ),
    Signal(
        "repetition_or_stuckness",
        "medium",
        "agent-loop",
        re.compile(r"Repetition detected|stuck state|duplicate items|high prompt growth|context drift", re.I),
        "fix-harness",
        "The runtime or transcript indicated loop/stuck behavior; inspect session metrics and prompts.",
    ),
    Signal(
        "local_llm_scout_timeout",
        "low",
        "agent-loop",
        re.compile(r"\b(LM Studio|local LLM|private scout|scout)\b[^\n]{0,180}\b(timeout|timed out)", re.I),
        "ignore-noise",
        "A local model scout timed out; treat this as expected optional-tool friction unless repeated timeouts require benchmark or preload guidance.",
    ),
    Signal(
        "local_llm_scout_misuse_risk",
        "medium",
        "agent-loop",
        re.compile(
            r"\b(LM Studio|local LLM|private scout|scout)\b[^\n]{0,180}\b(raw traces|routing|policy|promotion|drift|unbounded)",
            re.I,
        ),
        "promote-to-skill",
        "A local model scout appeared to cross advisory boundaries; promote guardrails, not private trace details.",
    ),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan local rollout/session traces for workflow friction.")
    parser.add_argument("paths", nargs="*", type=Path, help="Files or directories to scan.")
    parser.add_argument("--root", type=Path, help="Directory to scan when no paths are provided.")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--context-chars", type=int, default=DEFAULT_CONTEXT_CHARS)
    parser.add_argument("--since", help="Only scan timestamped JSON records at or after this ISO timestamp.")
    parser.add_argument("--until", help="Only scan timestamped JSON records before or at this ISO timestamp.")
    parser.add_argument("--after-file", type=Path, help="Apply --after-line only to this file path.")
    parser.add_argument("--after-line", type=int, help="Only scan records or lines after this line/record number.")
    parser.add_argument(
        "--suppress-investigation-noise",
        action="store_true",
        help="Skip conservative self-referential analyzer/grep discussion lines while preserving raw helper payloads.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a readable report.")
    args = parser.parse_args()
    if not args.paths and not args.root:
        parser.error("provide at least one trace path or an explicit --root")
    if args.after_line is not None and args.after_line < 0:
        parser.error("--after-line must be non-negative")
    args.since_ts = parse_timestamp_arg(args.since, "--since") if args.since else None
    args.until_ts = parse_timestamp_arg(args.until, "--until") if args.until else None
    return args


def parse_timestamp_arg(value: str, flag: str) -> float:
    parsed = parse_timestamp(value)
    if parsed is None:
        raise SystemExit(f"error: {flag} must be an ISO timestamp, got {value!r}")
    return parsed


def iter_candidate_files(paths: list[Path], max_files: int) -> list[Path]:
    explicit_files: list[Path] = []
    discovered_files: list[Path] = []
    for path in paths:
        expanded = path.expanduser()
        if expanded.is_file():
            explicit_files.append(expanded)
        elif expanded.is_dir():
            for child in expanded.rglob("*"):
                if is_candidate_file(child):
                    discovered_files.append(child)

    explicit = sorted(unique_paths(explicit_files), key=path_mtime, reverse=True)
    explicit_set = set(explicit)
    discovered = [path for path in unique_paths(discovered_files) if path not in explicit_set]
    discovered = sorted(discovered, key=path_mtime, reverse=True)[:max_files]
    return explicit + discovered


def unique_paths(paths: list[Path]) -> list[Path]:
    return list(dict.fromkeys(paths))


def path_mtime(path: Path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


def is_candidate_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.name in SKILL_DOC_NAMES:
        return False
    suffix = path.suffix.lower()
    if suffix not in ROLL_OUT_SUFFIXES:
        return False
    if suffix in STRUCTURED_TRACE_SUFFIXES:
        return True
    return bool(ROLL_OUT_NAME_RE.search(path.name) or ROLL_OUT_NAME_RE.search(str(path.parent)))


def redacted(text: str, context_chars: int) -> str:
    single_line = " ".join(text.strip().split())
    scrubbed = URL_AUTH_RE.sub("[REDACTED_URL_AUTH]", single_line)
    scrubbed = SECRET_RE.sub("[REDACTED_SECRET]", scrubbed)
    scrubbed = PATH_RE.sub("[REDACTED_PATH]", scrubbed)
    scrubbed = LOCAL_HOST_RE.sub("[REDACTED_HOST]", scrubbed)
    scrubbed = HOST_RE.sub("[REDACTED_HOST]", scrubbed)
    if len(scrubbed) > context_chars:
        return scrubbed[: context_chars - 3] + "..."
    return scrubbed


def line_text_from_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        interesting: list[str] = []
        for key in INTERESTING_JSON_KEYS:
            if key in value:
                interesting.append(f"{key}={value[key]}")
        return " ".join(interesting) if interesting else json.dumps(value, sort_keys=True, default=str)
    return json.dumps(value, sort_keys=True, default=str)


def json_fragments(value: Any, structured_context: bool = False) -> Iterable[Fragment]:
    if isinstance(value, dict):
        structured = structured_context or is_structured_payload(value)
        summary = line_text_from_json(value)
        for key, child in value.items():
            if isinstance(child, str) and key in NESTED_JSON_STRING_KEYS:
                nested = parse_nested_json_object(child)
                if nested is not None:
                    raw_fragment = Fragment(f"{key}={child}", False, structured)
                    if is_meta_echo(raw_fragment.text):
                        yield raw_fragment
                    elif structured:
                        for fragment in json_fragments(nested, True):
                            yield Fragment(fragment.text, fragment.summary, True)
                    elif key in NESTED_JSON_STRING_KEYS and contains_structured_payload(nested):
                        yield from json_fragments(nested, True)
                    else:
                        yield from json_fragments(nested, structured)
                    continue
            if key in INTERESTING_JSON_KEYS and not isinstance(child, dict | list):
                yield Fragment(f"{key}={child}", False, structured)
                continue
            yield from json_fragments(child, structured)
        if summary != json.dumps(value, sort_keys=True, default=str):
            yield Fragment(summary, True, structured)
    elif isinstance(value, list):
        for child in value:
            yield from json_fragments(child, structured_context)
    elif isinstance(value, str):
        yield Fragment(value, False, structured_context)
    elif value is not None:
        yield Fragment(json.dumps(value, sort_keys=True, default=str), False, structured_context)


def is_structured_payload(value: dict[str, Any]) -> bool:
    return bool(STRUCTURED_PAYLOAD_KEYS & set(value.keys()))


def contains_structured_payload(value: Any) -> bool:
    if isinstance(value, dict):
        if is_structured_payload(value):
            return True
        return any(contains_structured_payload(child) for child in value.values())
    if isinstance(value, list):
        return any(contains_structured_payload(child) for child in value)
    return False


def parse_nested_json_object(text: str) -> Any | None:
    stripped = text.strip()
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict | list):
        return parsed
    return None


def parse_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def record_timestamp(value: Any) -> float | None:
    if isinstance(value, dict):
        for key in ("timestamp", "time", "createdAt", "created_at", "updatedAt", "updated_at"):
            parsed = parse_timestamp(value.get(key))
            if parsed is not None:
                return parsed
        for child in value.values():
            parsed = record_timestamp(child)
            if parsed is not None:
                return parsed
    elif isinstance(value, list):
        for child in value:
            parsed = record_timestamp(child)
            if parsed is not None:
                return parsed
    return None


def in_time_window(value: Any, since_ts: float | None, until_ts: float | None) -> bool:
    if since_ts is None and until_ts is None:
        return True
    timestamp = record_timestamp(value)
    if timestamp is None:
        return False
    if since_ts is not None and timestamp < since_ts:
        return False
    if until_ts is not None and timestamp > until_ts:
        return False
    return True


def after_checkpoint(path: Path, line_no: int, after_file: Path | None, after_line: int | None) -> bool:
    if after_line is None:
        return True
    if after_file is not None and path.resolve() != after_file.expanduser().resolve():
        return True
    return line_no > after_line


def top_level_json_records(value: Any) -> Iterable[tuple[int, Any]]:
    if isinstance(value, list):
        for idx, item in enumerate(value, start=1):
            yield idx, item
    else:
        yield 1, value


def iter_lines(
    path: Path,
    max_bytes: int,
    since_ts: float | None = None,
    until_ts: float | None = None,
    after_file: Path | None = None,
    after_line: int | None = None,
) -> Iterable[tuple[int, str, bool, bool]]:
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        yield 0, f"scanner_io_error unable to read file: {exc}", False, False
        return

    if len(data) > max_bytes:
        data = data[:max_bytes]
    text = data.decode("utf-8", errors="replace")
    if path.suffix.lower() == ".json":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            for idx, record in top_level_json_records(parsed):
                if not after_checkpoint(path, idx, after_file, after_line):
                    continue
                if not in_time_window(record, since_ts, until_ts):
                    continue
                for fragment in json_fragments(record):
                    yield idx, fragment.text, fragment.summary, fragment.structured
            return

    for idx, raw in enumerate(text.splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        if not after_checkpoint(path, idx, after_file, after_line):
            continue
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if not in_time_window(parsed, since_ts, until_ts):
                    continue
                for fragment in json_fragments(parsed):
                    yield idx, fragment.text, fragment.summary, fragment.structured
                continue
            except json.JSONDecodeError:
                pass
        if since_ts is not None or until_ts is not None:
            continue
        yield idx, raw, False, False


def scan(
    files: list[Path],
    max_bytes: int,
    context_chars: int,
    since_ts: float | None = None,
    until_ts: float | None = None,
    after_file: Path | None = None,
    after_line: int | None = None,
    suppress_investigation_noise: bool = False,
) -> dict[str, Finding]:
    findings: dict[str, Finding] = {signal.name: Finding(signal) for signal in SIGNALS}
    seen_hits: set[tuple[Path, int, str, str, int]] = set()
    for path in files:
        line_signal_texts: dict[tuple[Path, int, str], set[str]] = {}
        for line_no, text, is_summary, is_structured in iter_lines(
            path, max_bytes, since_ts, until_ts, after_file, after_line
        ):
            if is_meta_echo(text):
                continue
            if suppress_investigation_noise and not is_structured and is_investigation_noise(text):
                continue
            matched_auth_login_noise = bool(AUTH_LOGIN_NOISE_RE.search(text))
            for signal in SIGNALS:
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
                canonical_text = canonical_hit_text(text)
                for occurrence, match in enumerate(signal.pattern.finditer(text)):
                    if should_skip_signal_match(signal.name, text, canonical_text, match):
                        continue
                    line_signal_key = (path, line_no, signal.name)
                    line_texts = line_signal_texts.setdefault(line_signal_key, set())
                    if is_summary and summary_only_repeats_seen_values(canonical_text, line_texts):
                        continue
                    hit_key = (path, line_no, signal.name, canonical_text, occurrence)
                    if hit_key in seen_hits:
                        continue
                    seen_hits.add(hit_key)
                    line_texts.add(canonical_text)
                    findings[signal.name].add(
                        Hit(
                            file=path,
                            line=line_no,
                            snippet=redacted(text, context_chars),
                            structured=is_structured,
                        )
                    )
    return {
        name: finding
        for name, finding in findings.items()
        if finding.count >= finding.signal.threshold
    }


def canonical_hit_text(text: str) -> str:
    normalized = " ".join(text.strip().split())
    lowered = normalized.lower()
    for prefix in DEDUP_VALUE_PREFIXES:
        if lowered.startswith(prefix):
            return normalized[len(prefix) :]
    return normalized


def should_skip_signal_match(
    signal_name: str,
    text: str,
    canonical_text: str,
    match: re.Match[str],
) -> bool:
    if signal_name in {"repeated_command_failure", "missing_dependency_or_tool"}:
        return looks_like_static_match_context(text, match)
    if signal_name in {"auto_review_loop", "auto_review_valid_finding"}:
        return looks_like_static_diff_or_config(text) and AUTO_REVIEW_TEXT_RE.search(canonical_text) is None
    return False


def looks_like_static_diff_or_config(text: str) -> bool:
    return bool(DIFF_OR_STATIC_CONTEXT_RE.search(text))


def looks_like_static_match_context(text: str, match: re.Match[str]) -> bool:
    if not looks_like_static_diff_or_config(text):
        return False
    line_start = text.rfind("\n", 0, match.start()) + 1
    line_end = text.find("\n", match.end())
    if line_end == -1:
        line_end = len(text)
    line = text[line_start:line_end]
    return looks_like_static_diff_line(line) or looks_like_static_config_line(line)


def looks_like_static_diff_line(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith(("diff --git", "index ", "@@ ", "--- a/", "+++ b/", "+", "-"))


def looks_like_static_config_line(line: str) -> bool:
    return bool(STATIC_CONFIG_LINE_RE.search(line))


def summary_only_repeats_seen_values(summary: str, seen_texts: set[str]) -> bool:
    if not seen_texts:
        return False
    summary_lower = summary.lower()
    return all(seen.lower() in summary_lower for seen in seen_texts)


def is_meta_echo(text: str) -> bool:
    normalized = canonical_hit_text(" ".join(text.strip().split()))
    return bool(META_ECHO_RE.search(normalized))


def is_investigation_noise(text: str) -> bool:
    normalized = canonical_hit_text(" ".join(text.strip().split()))
    return bool(INVESTIGATION_NOISE_RE.search(normalized))


def stable_file_id(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8", errors="replace")).hexdigest()[:12]


def finding_to_json(finding: Finding) -> dict[str, Any]:
    examples = finding.hits[:3]
    return {
        "signal": finding.signal.name,
        "severity": finding.signal.severity,
        "category": finding.signal.category,
        "count": finding.count,
        "structured_payload_count": finding.structured_count,
        "broad_context_count": finding.count - finding.structured_count,
        "files": [
            {"id": stable_file_id(Path(path)), "hits": count}
            for path, count in finding.files.most_common()
        ],
        "evidence": [
            {
                "file_id": stable_file_id(hit.file),
                "line": hit.line,
                "snippet": hit.snippet,
                "evidence_type": "structured_payload" if hit.structured else "broad_context",
            }
            for hit in examples
        ],
        "likely_cause": finding.signal.likely_cause,
        "recommended_destination": finding.signal.destination,
    }


def emit_json(files: list[Path], findings: dict[str, Finding]) -> None:
    payload = {
        "ok": True,
        "scanned_files": [
            {"id": stable_file_id(path), "suffix": path.suffix, "bytes": path.stat().st_size}
            for path in files
            if path.exists()
        ],
        "findings": [finding_to_json(finding) for finding in findings.values()],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def emit_text(files: list[Path], findings: dict[str, Finding]) -> None:
    print(f"Scanned {len(files)} file(s).")
    if not findings:
        print("No friction signals met reporting thresholds.")
        return

    for finding in sorted(findings.values(), key=lambda f: (severity_rank(f.signal.severity), -f.count)):
        print()
        print(f"[{finding.signal.severity}] {finding.signal.name} ({finding.count} hit(s))")
        if finding.structured_count:
            print(f"structured_payload_hits: {finding.structured_count}")
            print(f"broad_context_hits: {finding.count - finding.structured_count}")
        print(f"category: {finding.signal.category}")
        print(f"recommended_destination: {finding.signal.destination}")
        print(f"likely_cause: {finding.signal.likely_cause}")
        print("evidence:")
        for hit in finding.hits[:3]:
            evidence_type = "structured_payload" if hit.structured else "broad_context"
            print(f"- file_id={stable_file_id(hit.file)} line={hit.line} type={evidence_type}: {hit.snippet}")


def severity_rank(severity: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(severity, 3)


def main() -> int:
    args = parse_args()
    paths = args.paths or [args.root]
    files = iter_candidate_files(paths, args.max_files)
    findings = scan(
        files,
        args.max_bytes,
        args.context_chars,
        since_ts=args.since_ts,
        until_ts=args.until_ts,
        after_file=args.after_file,
        after_line=args.after_line,
        suppress_investigation_noise=args.suppress_investigation_noise,
    )
    if args.json:
        emit_json(files, findings)
    else:
        emit_text(files, findings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
