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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


DEFAULT_MAX_FILES = 25
DEFAULT_MAX_BYTES = 3_000_000
DEFAULT_CONTEXT_CHARS = 180

ROLL_OUT_SUFFIXES = {".jsonl", ".json", ".log", ".txt", ".md"}
ROLL_OUT_NAME_RE = re.compile(r"(rollout|session|runout|thread|trace|transcript)", re.I)
SECRET_RE = re.compile(
    r"(?i)(ghp_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-ant-[A-Za-z0-9_-]+|"
    r"sk-[A-Za-z0-9_-]+|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]+|"
    r"(?:export\s+)?(?:api[_-]?key|token|secret|password|credential)\s*[:=]\s*[^\s,'\"]+)"
)
PATH_RE = re.compile(
    r"(?:/(?:Users|home|var|tmp|private|Volumes|opt)/[^\s,'\"]+|[A-Za-z]:\\[^\s,'\"]+)"
)
URL_AUTH_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@[^\s]+", re.I)
HOST_RE = re.compile(r"\b(?:[a-z0-9-]+\.){2,}[a-z]{2,}\b", re.I)


@dataclass(frozen=True, slots=True)
class Signal:
    name: str
    severity: str
    category: str
    pattern: re.Pattern[str]
    destination: str
    likely_cause: str
    threshold: int = 1


@dataclass(slots=True)
class Hit:
    file: Path
    line: int
    snippet: str


@dataclass(slots=True)
class Finding:
    signal: Signal
    hits: list[Hit] = field(default_factory=list)
    files: Counter[str] = field(default_factory=Counter)

    def add(self, hit: Hit) -> None:
        self.hits.append(hit)
        self.files[str(hit.file)] += 1

    @property
    def count(self) -> int:
        return len(self.hits)


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
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan local rollout/session traces for workflow friction.")
    parser.add_argument("paths", nargs="*", type=Path, help="Files or directories to scan.")
    parser.add_argument("--root", type=Path, help="Directory to scan when no paths are provided.")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--context-chars", type=int, default=DEFAULT_CONTEXT_CHARS)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a readable report.")
    args = parser.parse_args()
    if not args.paths and not args.root:
        parser.error("provide at least one trace path or an explicit --root")
    return args


def iter_candidate_files(paths: list[Path], max_files: int) -> list[Path]:
    candidates: list[Path] = []
    for path in paths:
        expanded = path.expanduser()
        if expanded.is_file():
            candidates.append(expanded)
        elif expanded.is_dir():
            for child in expanded.rglob("*"):
                if is_candidate_file(child):
                    candidates.append(child)

    candidates = sorted(set(candidates), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return candidates[:max_files]


def is_candidate_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix.lower() not in ROLL_OUT_SUFFIXES:
        return False
    return bool(ROLL_OUT_NAME_RE.search(path.name) or ROLL_OUT_NAME_RE.search(str(path.parent)))


def redacted(text: str, context_chars: int) -> str:
    single_line = " ".join(text.strip().split())
    scrubbed = URL_AUTH_RE.sub("[REDACTED_URL_AUTH]", single_line)
    scrubbed = SECRET_RE.sub("[REDACTED_SECRET]", scrubbed)
    scrubbed = PATH_RE.sub("[REDACTED_PATH]", scrubbed)
    scrubbed = HOST_RE.sub("[REDACTED_HOST]", scrubbed)
    if len(scrubbed) > context_chars:
        return scrubbed[: context_chars - 3] + "..."
    return scrubbed


def line_text_from_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        interesting: list[str] = []
        for key in (
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
        ):
            if key in value:
                interesting.append(f"{key}={value[key]}")
        return " ".join(interesting) if interesting else json.dumps(value, sort_keys=True, default=str)
    return json.dumps(value, sort_keys=True, default=str)


def json_fragments(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        summary = line_text_from_json(value)
        if summary != json.dumps(value, sort_keys=True, default=str):
            yield summary
        for child in value.values():
            yield from json_fragments(child)
    elif isinstance(value, list):
        for child in value:
            yield from json_fragments(child)
    elif isinstance(value, str):
        yield value
    elif value is not None:
        yield json.dumps(value, sort_keys=True, default=str)


def iter_lines(path: Path, max_bytes: int) -> Iterable[tuple[int, str]]:
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        yield 0, f"error: unable to read file: {exc}"
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
            for idx, fragment in enumerate(json_fragments(parsed), start=1):
                yield idx, fragment
            return

    for idx, raw in enumerate(text.splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        if raw.startswith("{"):
            try:
                for fragment in json_fragments(json.loads(raw)):
                    yield idx, fragment
                continue
            except json.JSONDecodeError:
                pass
        yield idx, raw


def scan(files: list[Path], max_bytes: int, context_chars: int) -> dict[str, Finding]:
    findings: dict[str, Finding] = {signal.name: Finding(signal) for signal in SIGNALS}
    seen_hits: set[tuple[Path, int, str]] = set()
    for path in files:
        for line_no, text in iter_lines(path, max_bytes):
            for signal in SIGNALS:
                if signal.pattern.search(text):
                    hit_key = (path, line_no, signal.name)
                    if hit_key in seen_hits:
                        continue
                    seen_hits.add(hit_key)
                    findings[signal.name].add(
                        Hit(file=path, line=line_no, snippet=redacted(text, context_chars))
                    )
    return {
        name: finding
        for name, finding in findings.items()
        if finding.count >= finding.signal.threshold
    }


def stable_file_id(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8", errors="replace")).hexdigest()[:12]


def finding_to_json(finding: Finding) -> dict[str, Any]:
    examples = finding.hits[:3]
    return {
        "signal": finding.signal.name,
        "severity": finding.signal.severity,
        "category": finding.signal.category,
        "count": finding.count,
        "files": [
            {"id": stable_file_id(Path(path)), "hits": count}
            for path, count in finding.files.most_common()
        ],
        "evidence": [
            {"file_id": stable_file_id(hit.file), "line": hit.line, "snippet": hit.snippet}
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
        print(f"category: {finding.signal.category}")
        print(f"recommended_destination: {finding.signal.destination}")
        print(f"likely_cause: {finding.signal.likely_cause}")
        print("evidence:")
        for hit in finding.hits[:3]:
            print(f"- file_id={stable_file_id(hit.file)} line={hit.line}: {hit.snippet}")


def severity_rank(severity: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(severity, 3)


def main() -> int:
    args = parse_args()
    paths = args.paths or [args.root]
    files = iter_candidate_files(paths, args.max_files)
    findings = scan(files, args.max_bytes, args.context_chars)
    if args.json:
        emit_json(files, findings)
    else:
        emit_text(files, findings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
