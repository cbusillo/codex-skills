#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Extract durable-memory candidates from local rollout/session traces.

This is a high-recall preprocessor for trusted local LLM review. It keeps raw
rollout evidence local, removes injected/system noise, builds small context
windows, and classifies candidates by possible destination before a model sees
them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_MAX_FILES = 500
DEFAULT_MAX_BYTES = 5_000_000
DEFAULT_CONTEXT_EVENTS = 2
DEFAULT_BATCH_CHARS = 36_000
DEFAULT_MAX_RECORD_CHARS = 1_800

SESSION_ROOT = Path.home() / ".code" / "sessions"
ROLL_OUT_SUFFIXES = {".jsonl", ".json", ".log", ".txt", ".md"}
STRUCTURED_TRACE_SUFFIXES = {".json", ".jsonl", ".log"}
ROLL_OUT_NAME_RE = re.compile(r"(rollout|session|runout|thread|trace|transcript)", re.I)
SKILL_DOC_NAMES = {"SKILL.md", "README.md"}

SECRET_RE = re.compile(
    r"(?is)(ghp_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+|"
    r"xox[baprs]-[A-Za-z0-9-]+|AKIA[0-9A-Z]{16}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----|"
    r"(?:api[_-]?key|token|secret|password|credential)\s*[:=]\s*[^\s,'\"]+)"
)
PATH_RE = re.compile(r"/(?:Users|home|workspace|workspaces|tmp|var|private)/[^\s,'\"]+")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
MENTION_RE = re.compile(
    r"(?<![\w/])@[A-Za-z0-9][A-Za-z0-9_.-]{1,38}\b|"
    r"<@[A-Z0-9]{2,}>|"
    r"\b[A-Za-z0-9_.-]{2,32}#[0-9]{4}\b"
)
PERSON_NAME_RE = re.compile(r"\b[A-Z][a-z]+(?:[ '-][A-Z][a-z]+){1,3}\b")
PERSON_CUE_NAME_RE = re.compile(
    r"\b(?i:(organizer|manager|reviewer|assignee|owner|contact|friend|trusted collaborator|works for|owned by|managed by|person:?|people:))\s+([A-Z][A-Za-z0-9_.-]{1,38})\b|"
    r"\b([A-Z][A-Za-z0-9_.-]{1,38})\s+(is|was|works for|owns|manages|organizes|managed|reviewed|assigned)\b",
)
BARE_HANDLE_CUE_RE = re.compile(
    r"\b(?i:(?:github|slack|discord)\s+(?:handle|username|user|id)|handle)\s+([A-Za-z][A-Za-z0-9_.-]{1,38})\b"
)
PUBLIC_PROPER_NAME_PHRASES = {
    "Every Code",
    "GitHub Actions",
    "LM Studio",
    "OpenAI Codex",
}

PERSON_RE = re.compile(
    r"\b(people|person|manager|reviewer|assignee|github handle|handle|bot owner|"
    r"bot identity|owned by|trusted|unknown actor|contact|mention style|person:)\b",
    re.I,
)
PROFILE_RE = re.compile(
    r"\b(remember|forget|prefer|preference|always|never|do not|don't|next time|"
    r"local config|local workflow|workflow preference|auth_accounts|logout|login|"
    r"remote control|branch discipline|confirm:)\b",
    re.I,
)
LOCAL_LLM_RE = re.compile(
    r"\b(local llm|lm studio|qwen3-coder-64b|qwen3-coder-next|qwen_qwen3\.5|"
    r"gpt-oss|deepseek|devstral|trusted_lan|localhost model|gpu)\b",
    re.I,
)
FRICTION_RE = re.compile(
    r"\b(friction|stale|blocked|repeated|loop|timeout|retry|wrong person|"
    r"wrong manager|auto review|auto-review|rate limit|no runs found|mergeable unknown|"
    r"shell quoting|command failed|process exited)\b",
    re.I,
)
REPO_SPECIFIC_RE = re.compile(
    r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+\.py|"
    r"[A-Za-z0-9_.-]+\.ts|[A-Za-z0-9_.-]+\.tsx|[A-Za-z0-9_.-]+\.rs|"
    r"pyproject\.toml|package\.json|SKILL\.md|AGENTS\.md|\.github/github\.json)\b",
    re.I,
)

TRANSIENT_RE = re.compile(
    r"\b(PR #?\d+|issue #?\d+|merged|closed|deployed|checks? (?:passed|green)|"
    r"commit [0-9a-f]{7,}|run \d+|job \d+|status|release v?\d)\b",
    re.I,
)
INJECTED_RE = re.compile(
    r"\b(System Status|automatic message added by system|base_instructions|Available skills|"
    r"How to use skills|token_count|model_context_window|reasoning_output_tokens)\b",
    re.I,
)


@dataclass
class Event:
    file: str
    line: int
    timestamp: str | None
    role: str
    channel: str
    text: str


@dataclass
class Candidate:
    candidate_id: str
    index: int
    destination: str
    confidence: str
    reason: str
    source_file: str
    source_line: int
    timestamp: str | None
    text: str
    context: list[dict[str, Any]] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract rollout memory candidates for local review.")
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--root", type=Path, default=SESSION_ROOT)
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--context-events", type=int, default=DEFAULT_CONTEXT_EVENTS)
    parser.add_argument("--batch-chars", type=int, default=DEFAULT_BATCH_CHARS)
    parser.add_argument("--max-record-chars", type=int, default=DEFAULT_MAX_RECORD_CHARS)
    parser.add_argument(
        "--destination",
        action="append",
        choices=["people", "profile", "local-llm", "rollout-friction", "repo-specific"],
        help="Only emit candidates for this destination. May be repeated.",
    )
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--trusted-originals", action="store_true", help="Keep original local text except obvious secrets.")
    parser.add_argument("--redact", action="store_true", help="Redact paths and person data in addition to obvious secrets.")
    parser.add_argument("--jsonl", action="store_true", help="Emit candidates as JSONL instead of JSON.")
    parser.add_argument("--prompt-jsonl", action="store_true", help="Emit local-LLM prompt batches as JSONL.")
    parser.add_argument("--output-dir", type=Path, help="Write local artifacts to this ignored/private directory.")
    args = parser.parse_args()
    if args.trusted_originals and args.redact:
        parser.error("use --trusted-originals or --redact, not both")
    args.since_ts = parse_timestamp(args.since) if args.since else None
    args.until_ts = parse_timestamp(args.until) if args.until else None
    return args


def main() -> int:
    args = parse_args()
    files = iter_candidate_files(args.paths or [args.root], args.max_files)
    candidates = extract(files, args)
    if args.destination:
        wanted = set(args.destination)
        candidates = [candidate for candidate in candidates if candidate.destination in wanted]
    if args.output_dir:
        write_artifacts(args.output_dir, files, candidates, args)
    elif args.prompt_jsonl:
        for batch in prompt_batches(candidates, args.batch_chars):
            print(json.dumps(batch, ensure_ascii=False))
    elif args.jsonl:
        for candidate in candidates:
            print(json.dumps(candidate_to_json(candidate), ensure_ascii=False))
    else:
        payload = {
            "source_file_count": len(files),
            "candidate_count": len(candidates),
            "destination_counts": destination_counts(candidates),
            "candidates": [candidate_to_json(candidate) for candidate in candidates],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


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
    explicit = sorted(unique_paths(explicit_files), key=path_mtime)
    explicit_set = set(explicit)
    discovered = [path for path in unique_paths(discovered_files) if path not in explicit_set]
    discovered = sorted(discovered, key=path_mtime)[:max_files]
    return explicit + discovered


def unique_paths(paths: list[Path]) -> list[Path]:
    return list(dict.fromkeys(paths))


def path_mtime(path: Path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


def is_candidate_file(path: Path) -> bool:
    if not path.is_file() or path.name in SKILL_DOC_NAMES:
        return False
    suffix = path.suffix.lower()
    if suffix not in ROLL_OUT_SUFFIXES:
        return False
    if suffix in STRUCTURED_TRACE_SUFFIXES:
        return True
    return bool(ROLL_OUT_NAME_RE.search(path.name) or ROLL_OUT_NAME_RE.search(str(path.parent)))


def extract(files: list[Path], args: argparse.Namespace) -> list[Candidate]:
    candidates: list[Candidate] = []
    seen: set[tuple[str, str, str]] = set()
    for path in files:
        events = list(iter_events(path, args.max_bytes, args.since_ts, args.until_ts))
        for idx, event in enumerate(events):
            classification = classify(event.text)
            if classification is None:
                continue
            destination, confidence, reason = classification
            text = clean_text(event.text, args)
            if not text:
                continue
            dedupe_key = (destination, event.file, canonical_text_digest(text))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            context = context_window(events, idx, args.context_events, args)
            candidate_id = candidate_fingerprint(destination, event, text)
            candidates.append(
                Candidate(
                    candidate_id=candidate_id,
                    index=len(candidates),
                    destination=destination,
                    confidence=confidence,
                    reason=reason,
                    source_file=clean_source_file(event.file, args),
                    source_line=event.line,
                    timestamp=event.timestamp,
                    text=text[: args.max_record_chars],
                    context=context,
                )
            )
    return candidates


def iter_events(
    path: Path,
    max_bytes: int,
    since_ts: float | None,
    until_ts: float | None,
) -> Iterable[Event]:
    try:
        data = path.read_bytes()[: max_bytes + 1]
    except OSError:
        return
    text = data[:max_bytes].decode("utf-8", errors="replace")
    for line_no, raw in enumerate(text.splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            event = Event(str(path), line_no, None, "text", "raw", raw)
            if not is_noise(event.text):
                yield event
            continue
        timestamp = find_timestamp(record)
        if not in_time_window(timestamp, since_ts, until_ts):
            continue
        yield from events_from_record(record, path, line_no, timestamp)


def events_from_record(record: Any, path: Path, line_no: int, timestamp: str | None) -> Iterable[Event]:
    if not isinstance(record, dict):
        return
    record_type = str(record.get("type") or "")
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    if record_type == "session_meta":
        return
    if record_type == "response_item" and isinstance(payload, dict):
        role = str(payload.get("role") or "")
        if role in {"user", "assistant"}:
            for text in strings_from_content(payload.get("content")):
                if not is_noise(text):
                    yield Event(str(path), line_no, timestamp, role, "message", text)
        return
    msg = payload.get("msg") if isinstance(payload, dict) else None
    if isinstance(msg, dict):
        msg_type = str(msg.get("type") or "")
        if msg_type in {"user_message", "assistant_message", "message"}:
            role = str(msg.get("role") or msg_type.replace("_message", ""))
            for text in strings_from_content(msg.get("content") or msg.get("text")):
                if not is_noise(text):
                    yield Event(str(path), line_no, timestamp, role, "message", text)
        elif msg_type in {"exec_command", "exec_result", "tool_result", "function_call_output"}:
            text = text_from_mapping(msg)
            if text and is_relevant_tool_text(text):
                yield Event(str(path), line_no, timestamp, "tool", msg_type, text)
        return
    if record_type in {"function_call_output", "tool_result"} and isinstance(payload, dict):
        text = text_from_mapping(payload)
        if text and is_relevant_tool_text(text):
            yield Event(str(path), line_no, timestamp, "tool", record_type, text)


def strings_from_content(content: Any) -> list[str]:
    values: list[str] = []
    if isinstance(content, str):
        values.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict):
                for key in ("text", "input_text", "output_text"):
                    value = item.get(key)
                    if isinstance(value, str):
                        values.append(value)
    return [value for value in values if value.strip()]


def text_from_mapping(mapping: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("text", "content", "aggregated_output", "stdout", "stderr", "error", "command"):
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)


def is_relevant_tool_text(text: str) -> bool:
    return bool(PERSON_RE.search(text) or PROFILE_RE.search(text) or LOCAL_LLM_RE.search(text) or FRICTION_RE.search(text))


def is_noise(text: str) -> bool:
    return bool(INJECTED_RE.search(text))


def classify(text: str) -> tuple[str, str, str] | None:
    if is_noise(text):
        return None
    if PERSON_RE.search(text):
        confidence = "medium" if not TRANSIENT_RE.search(text) else "low"
        return "people", confidence, "identity/contact/role/trust language"
    if LOCAL_LLM_RE.search(text):
        confidence = "medium" if not TRANSIENT_RE.search(text) else "low"
        return "local-llm", confidence, "local model or endpoint preference"
    if PROFILE_RE.search(text):
        confidence = "medium" if not TRANSIENT_RE.search(text) else "low"
        return "profile", confidence, "stable preference or local workflow language"
    if FRICTION_RE.search(text):
        confidence = "low" if TRANSIENT_RE.search(text) else "medium"
        return "rollout-friction", confidence, "workflow friction language"
    if REPO_SPECIFIC_RE.search(text):
        confidence = "low" if TRANSIENT_RE.search(text) else "medium"
        return "repo-specific", confidence, "repo path or implementation detail"
    return None


def context_window(events: list[Event], index: int, radius: int, args: argparse.Namespace) -> list[dict[str, Any]]:
    start = max(0, index - radius)
    end = min(len(events), index + radius + 1)
    result: list[dict[str, Any]] = []
    for event in events[start:end]:
        result.append(
            {
                "role": event.role,
                "channel": event.channel,
                "line": event.line,
                "text": clean_text(event.text, args)[: args.max_record_chars],
            }
        )
    return result


def clean_text(text: str, args: argparse.Namespace) -> str:
    cleaned = SECRET_RE.sub("<secret-redacted>", text)
    if args.redact:
        cleaned = PATH_RE.sub("<path-redacted>", cleaned)
        cleaned = redact_person_data(cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned.strip()


def redact_person_data(text: str) -> str:
    cleaned = EMAIL_RE.sub("<person-email-redacted>", text)
    cleaned = MENTION_RE.sub("<person-handle-redacted>", cleaned)
    cleaned = BARE_HANDLE_CUE_RE.sub(redact_bare_handle_cue, cleaned)
    cleaned = PERSON_CUE_NAME_RE.sub(redact_person_cue_name, cleaned)
    return PERSON_NAME_RE.sub(redact_person_name, cleaned)


def redact_bare_handle_cue(match: re.Match[str]) -> str:
    return match.group(0).replace(match.group(1), "<person-handle-redacted>")


def redact_person_cue_name(match: re.Match[str]) -> str:
    name = match.group(2) or match.group(3)
    return match.group(0).replace(name, "<person-name-redacted>")


def redact_person_name(match: re.Match[str]) -> str:
    phrase = match.group(0)
    if phrase in PUBLIC_PROPER_NAME_PHRASES:
        return phrase
    return "<person-name-redacted>"


def clean_source_file(path: str, args: argparse.Namespace) -> str:
    if args.redact:
        return PATH_RE.sub("<path-redacted>", path)
    return path


def canonical_text(text: str) -> str:
    return " ".join(text.casefold().split())


def canonical_text_digest(text: str) -> str:
    return hashlib.sha256(canonical_text(text).encode("utf-8")).hexdigest()


def find_timestamp(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("timestamp", "time", "createdAt", "created_at", "updatedAt", "updated_at"):
            item = value.get(key)
            if isinstance(item, str) and parse_timestamp(item) is not None:
                return item
        for child in value.values():
            found = find_timestamp(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_timestamp(child)
            if found:
                return found
    return None


def parse_timestamp(value: str | None) -> float | None:
    if not value:
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


def in_time_window(timestamp: str | None, since_ts: float | None, until_ts: float | None) -> bool:
    if since_ts is None and until_ts is None:
        return True
    parsed = parse_timestamp(timestamp)
    if parsed is None:
        return False
    if since_ts is not None and parsed < since_ts:
        return False
    if until_ts is not None and parsed > until_ts:
        return False
    return True


def prompt_batches(candidates: list[Candidate], batch_chars: int) -> Iterable[dict[str, Any]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for candidate in candidates:
        item = candidate_to_json(candidate)
        size = len(json.dumps(item, ensure_ascii=False)) + 200
        if current and current_chars + size > batch_chars:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += size
    if current:
        batches.append(current)
    for idx, batch in enumerate(batches, start=1):
        yield {
            "schema_version": 1,
            "task": (
                "Extract durable local updates from rollout memory candidates. Return JSON with decisions, "
                "people_updates, profile_notes, rollout_friction_notes, local_llm_notes, "
                "repo_specific_notes, discard_reasons, and reviewed_candidate_ids. "
                "Do not quote raw snippets. Prefer discard. "
                "Every candidate_id must appear exactly once in decisions. Each decision has "
                "candidate_id, action, destination, note, and reason. action is note or discard. "
                "Also mirror note decisions into the matching note list and discard decisions into "
                "discard_reasons. Do not put a candidate_id in both a note list and discard_reasons. "
                "People updates require explicit identity/contact/role/trust facts. Profile notes "
                "require stable user preferences or local workflow rules. Repo-specific notes belong "
                "in the relevant repo, not the central profile. For people_updates, prefer structured "
                "fields over prose when evidence supports them: name, display_name, "
                "preferred_reference, aliases, github_handle, role, organization, and note. Include "
                "both natural names and handles so the people resolver can be smoke-tested."
            ),
            "output_schema": {
                "decisions": [
                    {
                        "candidate_id": "string",
                        "action": "note|discard",
                        "destination": "people_updates|profile_notes|rollout_friction_notes|local_llm_notes|repo_specific_notes|discard_reasons",
                        "note": "string|null",
                        "reason": "string",
                    }
                ],
                "people_updates": [
                    {
                        "candidate_id": "string",
                        "name": "string|null",
                        "display_name": "string|null",
                        "preferred_reference": "string|null",
                        "aliases": ["string"],
                        "github_handle": "string|null",
                        "role": "string|null",
                        "organization": "string|null",
                        "note": "string",
                    }
                ],
                "profile_notes": [],
                "rollout_friction_notes": [],
                "local_llm_notes": [],
                "repo_specific_notes": [],
                "discard_reasons": [{"candidate_id": "string", "reason": "string"}],
                "reviewed_candidate_ids": [],
            },
            "batch": idx,
            "batch_count": len(batches),
            "allowed_model_scope": "trusted_local_or_trusted_lan",
            "candidates": batch,
        }


def candidate_to_json(candidate: Candidate) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "candidate_id": candidate.candidate_id,
        "index": candidate.index,
        "destination": candidate.destination,
        "confidence": candidate.confidence,
        "reason": candidate.reason,
        "source_file": candidate.source_file,
        "source_line": candidate.source_line,
        "timestamp": candidate.timestamp,
        "text": candidate.text,
        "context": candidate.context,
    }


def candidate_fingerprint(destination: str, event: Event, text: str) -> str:
    payload = json.dumps(
        {
            "destination": destination,
            "file": event.file,
            "line": event.line,
            "timestamp": event.timestamp,
            "text": canonical_text(text),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return "memcand_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def destination_counts(candidates: list[Candidate]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.destination] = counts.get(candidate.destination, 0) + 1
    return counts


def write_artifacts(
    output_dir: Path, files: list[Path], candidates: list[Candidate], args: argparse.Namespace
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates_path = output_dir / "candidates.jsonl"
    prompts_path = output_dir / "llm-prompts.jsonl"
    diagnostics_path = output_dir / "diagnostics.json"
    with candidates_path.open("w", encoding="utf-8") as handle:
        for candidate in candidates:
            handle.write(json.dumps(candidate_to_json(candidate), ensure_ascii=False) + "\n")
    with prompts_path.open("w", encoding="utf-8") as handle:
        for batch in prompt_batches(candidates, args.batch_chars):
            handle.write(json.dumps(batch, ensure_ascii=False) + "\n")
    diagnostics = {
        "schema_version": 1,
        "source_file_count": len(files),
        "candidate_count": len(candidates),
        "destination_counts": destination_counts(candidates),
        "input_fingerprint": fingerprint_paths(files),
        "bounds": safe_bounds(args),
        "privacy": privacy_summary(args),
        "artifacts": {
            "candidates": str(candidates_path),
            "llm_prompts": str(prompts_path),
            "diagnostics": str(diagnostics_path),
        },
    }
    diagnostics_path.write_text(json.dumps(diagnostics, indent=2, ensure_ascii=False), encoding="utf-8")
    return diagnostics


def safe_bounds(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "max_files": args.max_files,
        "max_bytes": args.max_bytes,
        "context_events": args.context_events,
        "batch_chars": args.batch_chars,
        "max_record_chars": args.max_record_chars,
        "since": args.since,
        "until": args.until,
        "destination": args.destination or [],
    }


def privacy_summary(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "trusted_originals": args.trusted_originals,
        "redact_paths": args.redact,
        "redact_person_data": args.redact,
        "obvious_secrets_stripped": True,
        "local_only": True,
    }


def fingerprint_paths(files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path).encode("utf-8", errors="replace"))
        digest.update(str(path_mtime(path)).encode("ascii"))
    return f"sha256:{digest.hexdigest()}"


if __name__ == "__main__":
    raise SystemExit(main())
