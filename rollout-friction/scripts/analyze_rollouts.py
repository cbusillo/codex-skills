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
import shlex
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, NamedTuple


DEFAULT_MAX_FILES = 25
DEFAULT_MAX_BYTES = 3_000_000
DEFAULT_CONTEXT_CHARS = 180
TEXT_LIMITATION_DISPLAY_LIMIT = 8
COUNT_SEMANTICS = "normalized_events_v2"

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
INJECTED_CONTEXT_NOISE_RE = re.compile(
    r"(?:\bAGENTS\.md instructions\b|<INSTRUCTIONS>|</INSTRUCTIONS>|<environment_context>|"
    r"Available skills|How to use skills|Knowledge cutoff|Current date|approval_policy|"
    r"sandbox_mode|You are Codex|You are an AI assistant|Desired oververbosity|"
    r"Review only the provided code change scope|User initiated a review task)|"
    r"(?:^|\s)```[a-z0-9_-]*(?:\s|$)|"
    r"^(?:def|class|import|from|const|let|var|function)\s+[A-Za-z_][A-Za-z0-9_]*|"
    r"\b(?:apply_patch|Begin Patch|End Patch|diff --git)\b",
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
RATE_LIMIT_GUIDANCE_RE = re.compile(
    r"\b("
    r"if\s+(?:GraphQL|REST|[^\n]{0,40}rate limit)|"
    r"when\s+(?:GraphQL|REST|[^\n]{0,40}rate limit)|"
    r"before\s+[^\n]{0,80}\bcheck rate limits?\b|"
    r"do not keep retrying|prefer REST|fallback to REST|"
    r"rate[- ]limit pressure should be classified|"
    r"use .*helper.*rate limit|"
    r"usage:\s+[^\n]{0,120}\brate-limit\b|"
    r"Handle GitHub [^\n]{0,120}\brate limits\b|"
    r"purpose:|description:|match:|argv_prefix:"
    r")",
    re.I,
)
RATE_LIMIT_PLACEHOLDER_RE = re.compile(
    r"\b(?:GITHUB|TOKEN|SECRET|API[_-]?KEY|PASSWORD)[A-Z0-9_\-]*\[REDACTED_SECRET\]"
    r"|\[REDACTED_SECRET\][A-Z0-9_\-]*(?:LIMIT|QUOTA|RATE)",
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
AUTO_REVIEW_LOOP_EVIDENCE_RE = re.compile(
    r"Auto Review:\s*[1-9][0-9]*\s+issue|"
    r"Background auto-review completed|"
    r"Detached proposal at .*/auto-review|"
    r"Worktree path:\s*.*/auto-review|"
    r"branch=auto-review|"
    r"auto[-\s]?review[^\n]{0,180}\b(detached proposal|worktree path|merge this worktree|cherry-pick|re-validate|revalidate|open PR)\b",
    re.I,
)
AUTO_REVIEW_POSITIVE_RE = re.compile(
    r"auto[-\s]?review[^\n]{0,180}\b(merged|passed|clean|no issues|useful|valid|applied|fixed)\b",
    re.I,
)
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
COMMAND_FAILURE_TAG_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("missing_command", re.compile(r"\b(command not found|[A-Za-z0-9_.-]+ not found|not found:|No such file or directory)\b", re.I)),
    ("missing_module", re.compile(r"\b(module not found|No module named|cannot find module|ModuleNotFoundError)\b", re.I)),
    ("timeout", re.compile(r"\b(timed out|timeout)\b", re.I)),
    ("permission", re.compile(r"\b(permission denied|operation not permitted|EACCES)\b", re.I)),
    ("network", re.compile(r"\b(ECONNREFUSED|connection refused|connection reset|DNS|ENOTFOUND|network)\b", re.I)),
    ("test_failure", re.compile(r"\b(pytest|failed tests?|test failed|[1-9][0-9]* failed[, ]+[0-9]+ passed)\b", re.I)),
    ("lint_or_format", re.compile(r"\b(lint|eslint|ruff|mypy|prettier|black|format check)\b", re.I)),
    ("git_failure", re.compile(r"\b(git|merge conflict|non-fast-forward|protected branch|working tree)\b", re.I)),
    ("github_cli", re.compile(r"\b(gh |GitHub|GraphQL|REST|mergeable|statusCheckRollup)\b", re.I)),
    ("exit_code", re.compile(r"\b(exit[_ -]?code\s*[:=]?\s*[1-9][0-9]*|process exited with code [1-9][0-9]*)\b", re.I)),
)


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
    __slots__ = ("file", "line", "snippet", "structured", "tags", "event_id", "outcome_basis")

    file: Path
    line: int
    snippet: str
    structured: bool
    tags: tuple[str, ...]

    def __init__(self, file: Path, line: int, snippet: str, structured: bool = False, tags: Iterable[str] = (),
                 event_id: str = "", outcome_basis: str | None = None) -> None:
        self.file = file
        self.line = line
        self.snippet = snippet
        self.structured = structured
        self.tags = tuple(tags)
        self.event_id = event_id
        self.outcome_basis = outcome_basis


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


class ScanFindings(dict[str, Finding]):
    def __init__(self, findings: dict[str, Finding], events: list[TraceEvent]) -> None:
        super().__init__(findings)
        self.events = events


class Fragment(NamedTuple):
    text: str
    summary: bool
    structured: bool = False


@dataclass
class TraceEvent:
    """One result, invocation, or context record; fragments are evidence, not events."""

    line: int
    event_id: str
    kind: str
    fragments: list[Fragment] = field(default_factory=list)
    tool_id: str | None = None
    command: str | list[str] | None = None
    exit_code: int | None = None
    failed: bool = False
    succeeded: bool = False
    expected_nonzero: bool = False
    retry: bool = False
    outcome_basis: str | None = None
    file_id: str = ""

    def evidence_text(self) -> str:
        texts = list(dict.fromkeys(canonical_hit_text(item.text) for item in self.fragments if not item.summary))
        if self.exit_code is not None:
            code_text = f"exit_code={self.exit_code}"
            texts = [code_text, *(text for text in texts if text != code_text)]
        return "\n".join(texts)


class Limitation(NamedTuple):
    kind: str
    message: str
    file: Path | None = None
    file_count: int | None = None
    byte_count: int | None = None
    limit: int | None = None


class ScanTarget(NamedTuple):
    path: Path
    read_bytes: int
    file_bytes: int
    truncated: bool


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
        re.compile(r"(?:Command guard:\s*)?Blocked git (switch|checkout)", re.I),
        "investigate-repo-workflow",
        "Git safety prompts protected worktrees but added retry friction; inspect whether branch setup can be more deliberate before command execution.",
        threshold=4,
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
    parser.add_argument(
        "--paths-file",
        type=Path,
        help="Read newline- or NUL-delimited trace paths from this file; use '-' for stdin.",
    )
    parser.add_argument("--root", type=Path, help="Directory to scan when no paths are provided.")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="Total bytes to read across all scanned files.")
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        help="Maximum bytes to read from any one file; defaults to --max-bytes.",
    )
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
    paths_from_file = load_paths_file(args.paths_file, parser) if args.paths_file else []
    args.paths = paths_from_file + args.paths
    validate_path_arguments(args.paths, parser)
    if not args.paths and not args.root:
        parser.error("provide at least one trace path, --paths-file, or an explicit --root")
    if args.max_files < 0:
        parser.error("--max-files must be non-negative")
    if args.max_bytes < 0:
        parser.error("--max-bytes must be non-negative")
    if args.max_file_bytes is not None and args.max_file_bytes < 0:
        parser.error("--max-file-bytes must be non-negative")
    if args.max_file_bytes is None:
        args.max_file_bytes = args.max_bytes
    if args.after_line is not None and args.after_line < 0:
        parser.error("--after-line must be non-negative")
    args.since_ts = parse_timestamp_arg(args.since, "--since") if args.since else None
    args.until_ts = parse_timestamp_arg(args.until, "--until") if args.until else None
    return args


def load_paths_file(path: Path, parser: argparse.ArgumentParser) -> list[Path]:
    try:
        if str(path) == "-":
            content = sys.stdin.buffer.read()
        else:
            content = path.expanduser().read_bytes()
    except OSError as exc:
        parser.error(f"unable to read --paths-file {path}: {exc}")
    if not content.strip(b"\0\n\r\t "):
        return []
    parts = content.split(b"\0") if b"\0" in content else content.splitlines()
    paths: list[Path] = []
    for raw in parts:
        text = raw.decode("utf-8", errors="surrogateescape").strip()
        if text:
            paths.append(Path(text))
    return paths


def validate_path_arguments(paths: list[Path], parser: argparse.ArgumentParser) -> None:
    for path in paths:
        text = str(path)
        if not any(char.isspace() for char in text):
            continue
        tokens = shlex.split(text)
        if len(tokens) < 2:
            continue
        existing = [token for token in tokens if Path(token).expanduser().exists()]
        if len(existing) >= 2:
            parser.error(
                "one positional path argument appears to contain multiple existing paths separated by whitespace; "
                "pass each path as a separate argv entry or use --paths-file"
            )


def parse_timestamp_arg(value: str, flag: str) -> float:
    parsed = parse_timestamp(value)
    if parsed is None:
        raise SystemExit(f"error: {flag} must be an ISO timestamp, got {value!r}")
    return parsed


def iter_candidate_files(paths: list[Path], max_files: int) -> tuple[list[Path], list[Limitation]]:
    explicit_files: list[Path] = []
    discovered_files: list[Path] = []
    limitations: list[Limitation] = []
    missing_inputs = 0
    skipped_directory_candidates = 0
    directory_entry_limit = max(max_files * 200, 1000)
    for path in paths:
        expanded = path.expanduser()
        if expanded.is_file():
            explicit_files.append(expanded)
        elif expanded.is_dir():
            visited_entries = 0
            for child in expanded.rglob("*"):
                visited_entries += 1
                if visited_entries > directory_entry_limit:
                    limitations.append(
                        Limitation(
                            "file_discovery_limit",
                            "directory walk stopped after the bounded discovery entry limit",
                            file=expanded,
                            file_count=visited_entries - 1,
                            limit=directory_entry_limit,
                        )
                    )
                    break
                if is_candidate_file(child):
                    if len(discovered_files) >= max_files:
                        skipped_directory_candidates += 1
                        break
                    discovered_files.append(child)
        else:
            missing_inputs += 1

    if missing_inputs:
        limitations.append(
            Limitation(
                "missing_path",
                "one or more explicit input paths did not exist or were not readable as files/directories",
                file_count=missing_inputs,
            )
        )

    explicit = sorted(unique_paths(explicit_files), key=path_mtime, reverse=True)
    explicit_set = set(explicit)
    discovered = [path for path in unique_paths(discovered_files) if path not in explicit_set]
    if skipped_directory_candidates:
        limitations.append(
            Limitation(
                "file_count_limit",
                "directory scan stopped after --max-files candidate files",
                file_count=skipped_directory_candidates,
                limit=max_files,
            )
        )
    discovered = sorted(discovered, key=path_mtime, reverse=True)[:max_files]
    return explicit + discovered, limitations


def plan_scan_targets(
    files: list[Path],
    max_bytes: int,
    max_file_bytes: int,
) -> tuple[list[ScanTarget], list[Limitation]]:
    targets: list[ScanTarget] = []
    limitations: list[Limitation] = []
    remaining = max_bytes
    skipped_after_budget = 0
    for path in files:
        try:
            file_bytes = path.stat().st_size
        except OSError as exc:
            limitations.append(
                Limitation(
                    "scanner_io_error",
                    f"unable to stat file before scan: {exc}",
                    file=path,
                )
            )
            continue
        if remaining <= 0:
            skipped_after_budget += 1
            continue
        read_bytes = min(file_bytes, max_file_bytes, remaining)
        truncated = file_bytes > read_bytes
        if truncated:
            limitation_kind = "per_file_byte_limit" if read_bytes == max_file_bytes else "total_byte_limit"
            limitations.append(
                Limitation(
                    limitation_kind,
                    "file was partially scanned because a byte limit was reached",
                    file=path,
                    byte_count=file_bytes,
                    limit=max_file_bytes if limitation_kind == "per_file_byte_limit" else max_bytes,
                )
            )
        targets.append(ScanTarget(path, read_bytes, file_bytes, truncated))
        remaining -= read_bytes
    if skipped_after_budget:
        limitations.append(
            Limitation(
                "total_byte_limit",
                "some candidate files were skipped after the total scan byte budget was exhausted",
                file_count=skipped_after_budget,
                limit=max_bytes,
            )
        )
    return targets, limitations


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


def iter_records(path: Path, max_bytes: int) -> Iterable[tuple[int, Any]]:
    if max_bytes <= 0:
        return
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        yield 0, f"scanner_io_error unable to read file: {exc}"
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
            yield from top_level_json_records(parsed)
            return

    for idx, raw in enumerate(text.splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                yield idx, parsed
                continue
            except json.JSONDecodeError:
                pass
        yield idx, raw


def iter_lines(
    path: Path,
    max_bytes: int,
    since_ts: float | None = None,
    until_ts: float | None = None,
    after_file: Path | None = None,
    after_line: int | None = None,
) -> Iterable[tuple[int, str, bool, bool]]:
    """Compatibility fragment view; consumers must not count these as events."""
    for line, record in iter_records(path, max_bytes):
        if after_checkpoint(path, line, after_file, after_line) and in_time_window(record, since_ts, until_ts):
            for fragment in json_fragments(record):
                yield line, fragment.text, fragment.summary, fragment.structured


CALL_TYPES = {"function_call", "custom_tool_call", "tool_call", "tool_use", "exec_command_begin"}
RESULT_TYPES = {"function_call_output", "custom_tool_call_output", "tool_result", "tool_call_end", "exec_command_end"}
CONTEXT_TYPES = {"user_message", "agent_message", "assistant_message", "reasoning", "session_meta", "turn_context", "compacted"}
ERROR_STATUSES = {"error", "failed", "failure", "timeout", "timed_out", "cancelled"}
SUCCESS_STATUSES = {"ok", "success", "succeeded", "completed"}
EXIT_STATUS_RE = re.compile(r"\b(?:exit[_ -]?code\s*[:=]?\s*|process exited with code\s+)(-?\d+)\b", re.I)
SUCCESS_TEXT_RE = re.compile(r"\b(passed|succeeded|success|green|mergeable)\b", re.I)
FALSE_SUCCESS_RE = re.compile(r"(?:\bsuccess\b|['\"]success['\"])\s*[:=]\s*(?:false|0|null|no)\b", re.I)


def explicit_outcome(value: Any) -> bool:
    return isinstance(value, dict) and bool(
        {"exit_code", "error", "error_reason", "isError", "is_error", "success"} & value.keys()
        or str(value.get("status", "")).lower() in ERROR_STATUSES | SUCCESS_STATUSES
    )


def result_payloads(value: Any, slot: str = "") -> list[tuple[Any, str]]:
    """Unwrap result envelopes, stopping at a status before inspecting printed output.

    Only explicit batch containers split a result into child outcomes. Fields such
    as stdout/stderr and synthetic summaries remain evidence of that same result.
    """
    if isinstance(value, str):
        nested = parse_nested_json_object(value)
        return result_payloads(nested, slot) if nested is not None else []
    if isinstance(value, list):
        return [part for index, child in enumerate(value) for part in result_payloads(child, f"{slot}/{index}")]
    if not isinstance(value, dict):
        return []
    for key in ("results", "tool_results"):
        if isinstance(value.get(key), list):
            parts = result_payloads(value[key], f"{slot}/{key}")
            if parts:
                return parts
    if explicit_outcome(value):
        return [(value, slot)]
    for key in ("payload", "result", "output", "content", "data"):
        if key in value:
            parts = result_payloads(value[key], slot)
            if parts:
                if len(parts) == 1 and isinstance(parts[0][0], dict):
                    payload, child_slot = parts[0]
                    inherited = {key: value[key] for key in ("call_id", "tool_call_id", "cmd", "command") if key in value}
                    parts = [({**inherited, **payload}, child_slot)]
                return parts
    return [(value, slot)] if value.get("type") in RESULT_TYPES else []


def command_value(value: Any) -> str | list[str] | None:
    if not isinstance(value, dict):
        return None
    for key in ("cmd", "command"):
        command = value.get(key)
        if isinstance(command, str) or (isinstance(command, list) and all(isinstance(item, str) for item in command)):
            return command
    arguments = value.get("arguments")
    if isinstance(arguments, str):
        arguments = parse_nested_json_object(arguments)
    return command_value(arguments) if isinstance(arguments, dict) else None


def simple_argv(command: str | list[str] | None) -> list[str] | None:
    if isinstance(command, list):
        return command
    if not isinstance(command, str) or any(char in command for char in "|;&<>\n`$"):
        return None
    try:
        return shlex.split(command)
    except ValueError:
        return None


def expected_search_status(command: str | list[str] | None, exit_code: int | None, tool_error: bool) -> bool:
    argv = simple_argv(command)
    return bool(exit_code == 1 and not tool_error and argv and Path(argv[0]).name in {"rg", "grep"})


def output_error_hint(payload: Any) -> bool:
    values = [payload] if isinstance(payload, str) else [
        payload[key] for key in ("output", "stdout", "stderr", "aggregated_output", "formatted_output", "content", "message")
        if isinstance(payload, dict) and key in payload
    ]
    return any(re.search(r"\berror:|\bfatal:|\bcommand failed\b|\bpermission denied\b|\boperation not permitted\b|\bno such file or directory\b",
                         fragment.text, re.I) for value in values for fragment in json_fragments(value))


def text_outcome(fragments: list[Fragment], *, typed_result: bool = False) -> tuple[int | None, bool, bool]:
    """Read tool-result text or, without result provenance, legacy text hints."""
    failure_signal = next(signal for signal in SIGNALS if signal.name == "repeated_command_failure")
    failed = False
    succeeded = False
    exit_code = None
    for fragment in fragments:
        text = fragment.text
        canonical = canonical_hit_text(text)
        terminal_text = typed_result and EXIT_STATUS_RE.fullmatch(canonical) is not None
        if fragment.summary or (is_meta_echo(text) and not terminal_text):
            continue
        if is_suppressed_noise(text) and not looks_like_static_diff_or_config(text):
            continue
        for match in EXIT_STATUS_RE.finditer(text):
            if not looks_like_static_match_context(text, match):
                exit_code = int(match.group(1))
        if not AUTH_LOGIN_NOISE_RE.search(text):
            failed |= any(not should_skip_signal_match(failure_signal.name, text, canonical, match)
                          for match in failure_signal.pattern.finditer(text))
        succeeded |= bool(SUCCESS_TEXT_RE.search(text) and not FALSE_SUCCESS_RE.search(text))
    if exit_code is not None:
        return exit_code, exit_code != 0, exit_code == 0
    return None, failed, succeeded and not failed


def set_outcome(event: TraceEvent, payload: Any, *, typed_result: bool = False) -> None:
    if event.kind in {"call", "context"}:
        return
    code = payload.get("exit_code") if isinstance(payload, dict) else None
    event.exit_code = code if isinstance(code, int) and not isinstance(code, bool) else None
    status = str(payload.get("status", "")).lower() if isinstance(payload, dict) else ""
    tool_error = isinstance(payload, dict) and bool(
        payload.get("error") or payload.get("error_reason") or payload.get("isError") is True
        or payload.get("is_error") is True or payload.get("success") is False or status in ERROR_STATUSES
    )
    if event.exit_code is not None or tool_error or status in SUCCESS_STATUSES or (isinstance(payload, dict) and payload.get("success") is True):
        event.outcome_basis = "result_status"
        event.expected_nonzero = expected_search_status(event.command, event.exit_code, tool_error or output_error_hint(payload))
        event.failed = tool_error or (event.exit_code is not None and event.exit_code != 0 and not event.expected_nonzero)
        event.succeeded = not event.failed
    else:
        if isinstance(payload, dict) and (
            ("exit_code" in payload and payload["exit_code"] is None)
            or payload.get("session_id") is not None
            or status in {"running", "in_progress", "queued", "pending"}
        ):
            return
        event.exit_code, event.failed, event.succeeded = text_outcome(event.fragments, typed_result=typed_result)
        event.expected_nonzero = expected_search_status(event.command, event.exit_code, output_error_hint(payload))
        if event.expected_nonzero:
            event.failed, event.succeeded = False, True
        if event.failed or event.succeeded or event.exit_code is not None:
            event.outcome_basis = "result_text" if typed_result else "text_hint"


def normalize_events(
    path: Path, max_bytes: int, since_ts: float | None = None, until_ts: float | None = None,
    after_file: Path | None = None, after_line: int | None = None,
) -> list[TraceEvent]:
    calls: dict[str, str | list[str] | None] = {}
    previous_failure: dict[str, bool] = {}
    invocation_retries: dict[str, bool] = {}
    events: dict[str, TraceEvent] = {}
    session_id = ""

    def identity(value: str) -> str:
        return hashlib.sha256(f"{stable_file_id(path)}:{session_id}:{value}".encode()).hexdigest()[:20]

    for line, record in iter_records(path, max_bytes):
        if isinstance(record, dict) and record.get("type") in {"session_meta", "thread.started"}:
            metadata = record.get("payload", {})
            next_session = (str(record.get("thread_id", "")) if record.get("type") == "thread.started"
                            else str(metadata.get("id", "")) if isinstance(metadata, dict) else "")
            if next_session and next_session != session_id:
                session_id = next_session
                calls.clear()
                previous_failure.clear()
                invocation_retries.clear()
        value = record
        native_phase = ""
        native_context = False
        if (isinstance(record, dict) and record.get("type") in {"item.started", "item.updated", "item.completed"}
                and record.get("role") not in {"user", "assistant", "system", "developer"}
                and isinstance(record.get("item"), dict)):
            value = record["item"]
            native_phase = str(record["type"])
            native_context = value.get("type") != "command_execution"
        while (isinstance(value, dict) and value.get("type") in {"response_item", "event_msg"}
               and value.get("role") not in {"user", "assistant", "system", "developer"}
               and isinstance(value.get("payload"), dict)):
            value = value["payload"]
        kind = str(value.get("type", "")) if isinstance(value, dict) else ""
        if native_phase and not native_context:
            kind = "exec_command_begin" if native_phase == "item.started" else "exec_command_end"
        role = value.get("role") if isinstance(value, dict) else None
        call_id = str(value.get("call_id") or value.get("tool_call_id") or "") if isinstance(value, dict) else ""
        if native_phase and not native_context:
            call_id = str(value.get("id") or "")
        command = command_value(value)
        if kind in CALL_TYPES:
            if call_id:
                calls[call_id] = command
            parts = [(value, "", "call")]
        elif (native_context or role in {"user", "assistant", "system", "developer"} or kind in CONTEXT_TYPES
              or (isinstance(value, str) and value.lstrip().startswith(("{", "[")))):
            parts = [(value, "", "context")]
        else:
            payloads = result_payloads(value)
            parts = [(payload, slot, "result") for payload, slot in payloads] if payloads else [
                (value, "", "result" if kind in RESULT_TYPES or role == "tool" else "legacy")
            ]
        for payload, slot, event_kind in parts:
            payload_call_id = str(payload.get("call_id") or payload.get("tool_call_id") or "") if isinstance(payload, dict) else ""
            child_call_id = payload_call_id or call_id
            identity_slot = "" if payload_call_id else slot
            event_command = command_value(payload) or command or calls.get(child_call_id)
            tool_id = identity(f"tool:{child_call_id}:{identity_slot}" if child_call_id else f"tool:{line}:{slot}") if event_kind in {"call", "result"} else None
            event_id = identity(f"{event_kind}:{child_call_id}:{identity_slot}" if child_call_id else f"{event_kind}:{line}:{slot}")
            event = TraceEvent(line, event_id, event_kind, list(json_fragments(payload)), tool_id, event_command)
            event.file_id = stable_file_id(path)
            typed_result = (kind in RESULT_TYPES or role == "tool"
                            or (isinstance(payload, dict) and payload.get("type") in RESULT_TYPES))
            set_outcome(event, payload, typed_result=typed_result)
            # Correlate preceding calls even when the requested checkpoint excludes them.
            signature = json.dumps(simple_argv(event_command) or event_command, sort_keys=True) if event_command else None
            if tool_id and tool_id not in invocation_retries:
                invocation_retries[tool_id] = bool(signature and previous_failure.get(signature))
            event.retry = invocation_retries.get(tool_id, False)
            if signature and event.outcome_basis:
                previous_failure[signature] = event.failed
            if not after_checkpoint(path, line, after_file, after_line) or not in_time_window(record, since_ts, until_ts):
                continue
            previous = events.get(event_id)
            priority = {None: 0, "text_hint": 1, "result_text": 2, "result_status": 3}
            if previous is None or (priority[event.outcome_basis], event.exit_code is not None) > (priority[previous.outcome_basis], previous.exit_code is not None):
                events[event_id] = event
    return sorted(events.values(), key=lambda event: event.line)


def collect_event_hits(path: Path, events: list[TraceEvent], context_chars: int,
                       suppress_investigation_noise: bool = False) -> dict[str, Finding]:
    findings = {signal.name: Finding(signal) for signal in SIGNALS}
    seen_hits: set[tuple[int, str, str, int]] = set()
    line_signal_texts: dict[tuple[int, str], set[str]] = {}
    for event in events:
        line_no = event.line
        if event.failed:
            evidence = event.evidence_text()
            findings["repeated_command_failure"].add(Hit(
                path, line_no, redacted(evidence, context_chars), event.outcome_basis in {"result_status", "result_text"},
                signal_tags("repeated_command_failure", evidence, evidence), event.event_id, event.outcome_basis,
            ))
        for text, is_summary, is_structured in event.fragments:
            if is_meta_echo(text):
                continue
            if suppress_investigation_noise and not is_structured and is_suppressed_noise(text):
                continue
            matched_auth_login_noise = bool(AUTH_LOGIN_NOISE_RE.search(text))
            for signal in SIGNALS:
                if signal.name == "repeated_command_failure":
                    continue
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
                    tags = signal_tags(signal.name, text, canonical_text)
                    line_signal_key = (line_no, signal.name)
                    line_texts = line_signal_texts.setdefault(line_signal_key, set())
                    if is_summary and summary_only_repeats_seen_values(canonical_text, line_texts):
                        continue
                    hit_key = (line_no, signal.name, canonical_text, occurrence)
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
                            tags=tags,
                            event_id=event.event_id,
                        )
                    )
    return findings


def scan(
    files: list[Path] | list[ScanTarget],
    max_bytes: int,
    context_chars: int,
    since_ts: float | None = None,
    until_ts: float | None = None,
    after_file: Path | None = None,
    after_line: int | None = None,
    suppress_investigation_noise: bool = False,
) -> dict[str, Finding]:
    findings = {signal.name: Finding(signal) for signal in SIGNALS}
    all_events: list[TraceEvent] = []
    seen_paths: set[Path] = set()
    for file_or_target in files:
        path = file_or_target.path if isinstance(file_or_target, ScanTarget) else file_or_target
        if path in seen_paths:
            continue
        seen_paths.add(path)
        read_bytes = file_or_target.read_bytes if isinstance(file_or_target, ScanTarget) else max_bytes
        events = normalize_events(path, read_bytes, since_ts, until_ts, after_file, after_line)
        all_events.extend(events)
        for name, finding in collect_event_hits(path, events, context_chars, suppress_investigation_noise).items():
            for hit in finding.hits:
                findings[name].add(hit)
    return ScanFindings({
        name: finding
        for name, finding in findings.items()
        if finding.count >= finding.signal.threshold
    }, all_events)


def outcome_summary(events: list[TraceEvent]) -> dict[str, Any]:
    return {
        "failed_result_count": sum(event.failed for event in events),
        "text_hint_failure_count": sum(event.failed and event.outcome_basis == "text_hint" for event in events),
        "nonzero_exit_count": sum(event.exit_code is not None and event.exit_code != 0 for event in events),
        "expected_nonzero_count": sum(event.expected_nonzero for event in events),
        "expected_nonzero_evidence": [
            {"event_id": event.event_id, "file_id": event.file_id, "line": event.line, "exit_code": event.exit_code}
            for event in events if event.expected_nonzero
        ][:3],
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
    if signal_name in {"github_graphql_rate_limit", "github_rest_rate_limit", "generic_rate_limit"}:
        return looks_like_rate_limit_false_positive(text, canonical_text, match)
    if signal_name in {"repeated_command_failure", "missing_dependency_or_tool"}:
        return looks_like_static_match_context(text, match)
    if signal_name == "auto_review_loop" and not is_auto_review_loop_evidence(canonical_text):
        return True
    if signal_name in {"auto_review_loop", "auto_review_valid_finding"}:
        return looks_like_static_diff_or_config(text) and AUTO_REVIEW_TEXT_RE.search(canonical_text) is None
    return False


def looks_like_rate_limit_false_positive(text: str, canonical_text: str, match: re.Match[str]) -> bool:
    if looks_like_static_match_context(text, match):
        return True
    line = matched_line(text, match)
    combined = f"{canonical_text}\n{line}"
    if looks_like_live_rate_limit_evidence(combined):
        return False
    if looks_like_flattened_static_payload(combined):
        return True
    if RATE_LIMIT_PLACEHOLDER_RE.search(combined):
        return True
    if RATE_LIMIT_GUIDANCE_RE.search(combined):
        return True
    return False


def looks_like_flattened_static_payload(text: str) -> bool:
    return bool(
        re.search(r"\bdiff --git\b|\bindex [0-9a-f]{7,}\.{2}[0-9a-f]{7,}\b|\b@@ -\d+", text)
        or re.search(r"\b(purpose|description|match|argv_prefix):\b", text, re.I)
        or re.search(r"\bUse only when the user explicitly requests\b", text, re.I)
        or re.search(r"\busage:\s+[^\n]{0,160}\brate-limit\b", text, re.I)
        or re.search(r"\bconfig\.toml\b|\[\[agents\]\]", text, re.I)
        or re.search(r"(?:^|\n|\s)#\s+[^\n]{0,160}\b(heuristics|checklist|guidance)\b", text, re.I)
        or re.search(r"\b[A-Za-z_][A-Za-z0-9_]*\s*:\s*(?:dict|list|tuple|set)\[", text)
    )


def looks_like_live_rate_limit_evidence(text: str) -> bool:
    return bool(
        re.search(
            r"\b(HTTP\s*429|429\b|quota exceeded|quota exhausted|secondary rate limit|"
            r"API rate limit exceeded|rate limit exceeded|too many requests|abuse detection|"
            r"X-RateLimit-Remaining\s*[:=]\s*0|quota remaining\s*[:=]\s*0|"
            r"rate[- ]limit remaining\s*[:=]\s*0)\b",
            text,
            re.I,
        )
    )


def is_auto_review_loop_evidence(text: str) -> bool:
    return bool(AUTO_REVIEW_LOOP_EVIDENCE_RE.search(text) and not AUTO_REVIEW_POSITIVE_RE.search(text))


def signal_tags(signal_name: str, text: str, canonical_text: str) -> tuple[str, ...]:
    if signal_name != "repeated_command_failure":
        return ()
    combined = f"{canonical_text}\n{text}"
    tags = [tag for tag, pattern in COMMAND_FAILURE_TAG_RULES if pattern.search(combined)]
    if re.search(r"\bCommand failed\b", combined, re.I):
        tags.append("command_failed")
    if re.search(r"Traceback \(most recent call last\)", combined):
        tags.append("python_traceback")
    if re.search(r"zsh:[^\n]*(unmatched|parse error)|unexpected EOF", combined, re.I):
        tags.append("shell_parse")
    if re.search(r"\b(rate limit|HTTP 429|quota exceeded|secondary rate)\b", combined, re.I):
        tags.append("rate_limit")
    return tuple(sorted(set(tags)))


def looks_like_static_diff_or_config(text: str) -> bool:
    return bool(DIFF_OR_STATIC_CONTEXT_RE.search(text))


def looks_like_static_match_context(text: str, match: re.Match[str]) -> bool:
    if not looks_like_static_diff_or_config(text):
        return False
    line = matched_line(text, match)
    return looks_like_static_diff_line(line) or looks_like_static_config_line(line)


def matched_line(text: str, match: re.Match[str]) -> str:
    line_start = text.rfind("\n", 0, match.start()) + 1
    line_end = text.find("\n", match.end())
    if line_end == -1:
        line_end = len(text)
    return text[line_start:line_end]


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


def is_suppressed_noise(text: str) -> bool:
    normalized = canonical_hit_text(" ".join(text.strip().split()))
    return bool(INVESTIGATION_NOISE_RE.search(normalized) or INJECTED_CONTEXT_NOISE_RE.search(normalized))


def stable_file_id(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8", errors="replace")).hexdigest()[:12]


def finding_to_json(finding: Finding) -> dict[str, Any]:
    examples = finding.hits[:3]
    tag_counts = Counter(tag for hit in finding.hits for tag in hit.tags)
    return {
        "signal": finding.signal.name,
        "severity": finding.signal.severity,
        "category": finding.signal.category,
        "count": finding.count,
        "count_unit": "result_events" if finding.signal.name == "repeated_command_failure" else "text_matches",
        "count_semantics": COUNT_SEMANTICS,
        "structured_payload_count": finding.structured_count,
        "broad_context_count": finding.count - finding.structured_count,
        "tag_counts": dict(sorted(tag_counts.items())),
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
                "tags": list(hit.tags),
                "event_id": hit.event_id,
                "outcome_basis": hit.outcome_basis,
            }
            for hit in examples
        ],
        "likely_cause": finding.signal.likely_cause,
        "recommended_destination": finding.signal.destination,
    }


def scan_summary(targets: list[ScanTarget], limitations: list[Limitation]) -> dict[str, Any]:
    limitation_counts = Counter(limitation.kind for limitation in limitations)
    truncated_file_count = sum(1 for target in targets if target.truncated)
    skipped_file_count = sum(limitation.file_count or 0 for limitation in limitations if limitation.kind in {"file_count_limit", "total_byte_limit"})
    scanned_bytes = sum(target.read_bytes for target in targets)
    total_candidate_bytes = sum(target.file_bytes for target in targets)
    return {
        "scan_degraded": bool(limitations),
        "limitation_counts": dict(sorted(limitation_counts.items())),
        "truncated_file_count": truncated_file_count,
        "skipped_file_count": skipped_file_count,
        "scanned_bytes": scanned_bytes,
        "candidate_bytes_seen": total_candidate_bytes,
    }


def limitation_to_json(limitation: Limitation) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": limitation.kind,
        "message": limitation.message,
    }
    if limitation.file is not None:
        payload["file_id"] = stable_file_id(limitation.file)
        payload["suffix"] = limitation.file.suffix
    if limitation.file_count is not None:
        payload["file_count"] = limitation.file_count
    if limitation.byte_count is not None:
        payload["byte_count"] = limitation.byte_count
    if limitation.limit is not None:
        payload["limit"] = limitation.limit
    return payload


def emit_json(targets: list[ScanTarget], findings: dict[str, Finding], limitations: list[Limitation]) -> None:
    payload = {
        "schema_version": 2,
        "count_semantics": COUNT_SEMANTICS,
        "ok": True,
        "scan_summary": scan_summary(targets, limitations),
        "outcome_summary": outcome_summary(getattr(findings, "events", [])),
        "scanned_files": [
            {
                "id": stable_file_id(target.path),
                "suffix": target.path.suffix,
                "bytes": target.file_bytes,
                "scanned_bytes": target.read_bytes,
                "truncated": target.truncated,
            }
            for target in targets
        ],
        "scan_limitations": [limitation_to_json(limitation) for limitation in limitations],
        "findings": [finding_to_json(finding) for finding in findings.values()],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def emit_text(targets: list[ScanTarget], findings: dict[str, Finding], limitations: list[Limitation]) -> None:
    print(f"count_semantics: {COUNT_SEMANTICS}; command failures count results, other signals count text matches")
    outcomes = outcome_summary(getattr(findings, "events", []))
    print(f"nonzero_exits: {outcomes['nonzero_exit_count']}; expected_nonzero: {outcomes['expected_nonzero_count']}; text_hint_failures: {outcomes['text_hint_failure_count']}")
    print(f"Scanned {len(targets)} file(s).")
    if limitations:
        summary = scan_summary(targets, limitations)
        print(
            "Scan degraded: "
            f"{summary['truncated_file_count']} truncated, "
            f"{summary['skipped_file_count']} skipped; use --json for full details."
        )
        print("Scan limitations:")
        for limitation in limitations[:TEXT_LIMITATION_DISPLAY_LIMIT]:
            suffix = f" file_id={stable_file_id(limitation.file)}" if limitation.file is not None else ""
            count = f" file_count={limitation.file_count}" if limitation.file_count is not None else ""
            limit = f" limit={limitation.limit}" if limitation.limit is not None else ""
            print(f"- {limitation.kind}:{suffix}{count}{limit} {limitation.message}")
        if len(limitations) > TEXT_LIMITATION_DISPLAY_LIMIT:
            omitted_count = len(limitations) - TEXT_LIMITATION_DISPLAY_LIMIT
            print(f"- {omitted_count} additional limitation(s) omitted from text output; use --json for full details.")
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
        tag_counts = Counter(tag for hit in finding.hits for tag in hit.tags)
        if tag_counts:
            tags = ", ".join(f"{tag}={count}" for tag, count in sorted(tag_counts.items()))
            print(f"tags: {tags}")
        print("evidence:")
        for hit in finding.hits[:3]:
            evidence_type = "structured_payload" if hit.structured else "broad_context"
            tags = f" tags={','.join(hit.tags)}" if hit.tags else ""
            print(f"- file_id={stable_file_id(hit.file)} line={hit.line} type={evidence_type}{tags}: {hit.snippet}")


def severity_rank(severity: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(severity, 3)


def main() -> int:
    args = parse_args()
    paths = args.paths or [args.root]
    files, limitations = iter_candidate_files(paths, args.max_files)
    targets, scan_limitations = plan_scan_targets(files, args.max_bytes, args.max_file_bytes)
    limitations.extend(scan_limitations)
    findings = scan(
        targets,
        args.max_bytes,
        args.context_chars,
        since_ts=args.since_ts,
        until_ts=args.until_ts,
        after_file=args.after_file,
        after_line=args.after_line,
        suppress_investigation_noise=args.suppress_investigation_noise,
    )
    if args.json:
        emit_json(targets, findings, limitations)
    else:
        emit_text(targets, findings, limitations)
    return 0


if __name__ == "__main__":
    sys.exit(main())
