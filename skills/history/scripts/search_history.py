#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Search local session messages; exact private evidence, never policy authority."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Reuse the catalog's transcript decoding and message extraction.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rollout-friction" / "scripts"))
from analyze_rollouts import iter_records  # noqa: E402
from extract_rollout_memory import events_from_record  # noqa: E402

SIGNALS = {
    "changed-decision": re.compile(r"\b(changed? my mind|instead|actually|reverse|no longer|previously|used to)\b", re.I),
    "direction-friction": re.compile(r"\bDIRECTION(?:\.md)?\b|\bstop boundar|\bapproval\b|\bceremony\b", re.I),
    "frustration": re.compile(r"\b(frustrat\w*|annoy\w*|ridiculous|stop asking|why (?:do|are) you|wast\w* (?:my )?time)\b", re.I),
}
INJECTED_PREFIXES = ("# AGENTS.md instructions", "<environment_context>", "<INSTRUCTIONS>", "<instructions>")


def discover_roots(home: Path, environ: dict[str, str]) -> list[Path]:
    roots = [home / ".codex" / "sessions", home / ".codex" / "archived_sessions", home / ".claude" / "projects"]
    for name, suffixes in (("CODEX_HOME", ("sessions", "archived_sessions")),
                           ("CLAUDE_CONFIG_DIR", ("projects",))):
        if environ.get(name):
            roots.extend(Path(environ[name]).expanduser() / suffix for suffix in suffixes)
    for base, suffixes in ((".codex-accounts", ("sessions", "archived_sessions")),
                           (".claude-accounts", ("projects",))):
        accounts = home / base
        if accounts.is_dir():
            for account in sorted(accounts.iterdir()):
                if account.is_dir():
                    roots.extend(account / suffix for suffix in suffixes)
    return list(dict.fromkeys(path.resolve() for path in roots))


def positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def bound(value: str) -> float:
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use an ISO timestamp with timezone") from exc
    if date.tzinfo is None:
        raise argparse.ArgumentTypeError("use an ISO timestamp with timezone")
    return date.timestamp()


def source_seconds(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        date = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return date.timestamp() if date.tzinfo is not None else None


def eastern(timestamp: str | None) -> str | None:
    seconds = source_seconds(timestamp)
    if seconds is None:
        return None
    local = datetime.fromtimestamp(seconds, ZoneInfo("America/New_York"))
    return local.strftime("%Y-%m-%d %I:%M:%S %p ET").replace(" 0", " ")


def candidate_files(root: Path):
    if root.is_file():
        yield root
        return
    # Stable traversal within each root; one candidate per root per round keeps
    # an older, large account from consuming every provider's search allowance.
    def failed_walk(error: OSError):
        raise error

    for directory, directories, filenames in os.walk(root, onerror=failed_walk):
        directories.sort()
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if path.suffix.lower() in {".jsonl", ".json"} and path.is_file():
                yield path


def scan(roots: list[Path], *, queries: list[str], signals: list[str], since: float | None = None,
         until: float | None = None, max_files: int = 500, max_bytes: int = 5_000_000,
         max_hits: int = 200) -> dict:
    limitations: list[dict] = []
    files: dict[Path, None] = {}
    pending = deque()
    for root in roots:
        root = root.expanduser().resolve()
        if not root.exists():
            limitations.append({"kind": "missing_root", "source": str(root)})
            continue
        if root.is_file() and root.suffix.lower() not in {".jsonl", ".json"}:
            limitations.append({"kind": "unsupported_file", "source": str(root)})
            continue
        pending.append((root, iter(candidate_files(root))))
    while pending:
        root, paths = pending.popleft()
        try:
            for path in paths:
                path = path.resolve()
                if path in files:
                    continue
                if len(files) >= max_files:
                    limitations.append({"kind": "file_limit", "source": str(root)})
                    break
                files[path] = None
                pending.append((root, paths))
                break
        except OSError as exc:
            limitations.append({"kind": "root_io_error", "source": str(root), "error": str(exc)})

    hits: list[dict] = []
    mirrors = 0
    for path in files:
        try:
            if path.stat().st_size > max_bytes:
                limitations.append({"kind": "byte_limit", "source": str(path)})
        except OSError as exc:
            limitations.append({"kind": "file_io_error", "source": str(path), "error": str(exc)})
            continue
        session = None
        provider = "unknown"
        sidechain = False
        candidates: list[dict] = []
        counts = {"unsupported_records": 0, "malformed_records": 0, "unknown_message_timestamps": 0}
        for line, record in iter_records(path, max_bytes):
            if not isinstance(record, dict):
                counts["malformed_records"] += 1
                continue
            payload = record.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            kind = record.get("type")
            if kind == "session_meta":
                session = payload.get("id") or session
                provider = "codex"
                sidechain = bool(isinstance(payload.get("source"), dict) and "subagent" in payload["source"])
                continue
            if "sessionId" in record:
                session = record["sessionId"]
                provider = "claude"
            elif kind in {"response_item", "event_msg", "turn_context"} or isinstance(payload.get("msg"), dict):
                provider = "codex"
            else:
                counts["unsupported_records"] += 1
            timestamp = record.get("timestamp")
            timestamp = timestamp if isinstance(timestamp, str) else None
            seconds = source_seconds(timestamp)
            if seconds is None:
                timestamp = None
            if record.get("isMeta") or record.get("isCompactSummary"):
                continue
            for event in events_from_record(record, path, line, timestamp, preserve_text=True):
                if event.channel != "message" or event.role not in {"user", "assistant"}:
                    continue
                if event.text.lstrip().startswith(INJECTED_PREFIXES):
                    continue
                if seconds is None:
                    counts["unknown_message_timestamps"] += 1
                    if since is not None or until is not None:
                        continue
                if seconds is not None and ((since is not None and seconds < since) or
                                            (until is not None and seconds > until)):
                    continue
                tags = [name for name, pattern in SIGNALS.items() if pattern.search(event.text)]
                if queries and not any(query.casefold() in event.text.casefold() for query in queries):
                    continue
                if signals and not set(signals).intersection(tags):
                    continue
                candidates.append({"provider": provider, "session": session, "source": str(path),
                                   "position": line, "position_kind": "record_index" if path.suffix.lower() == ".json" else "line",
                                   "timestamp": timestamp, "time_et": eastern(timestamp), "role": event.role,
                                   "attribution": "user_candidate" if event.role == "user" else "agent_text",
                                   "sidechain": sidechain or bool(record.get("isSidechain")),
                                   "text": event.text, "signals": tags, "record_type": kind,
                                   "record_id": record.get("uuid") or payload.get("id")})
        # Codex often writes a user turn twice. Suppress only its nearby mirrored
        # event_msg when a matching response_item exists; repeated turns survive.
        canonical: dict[tuple[str, str], list[float]] = {}
        for item in candidates:
            seconds = source_seconds(item["timestamp"])
            if item["record_type"] == "response_item" and seconds is not None:
                canonical.setdefault((item["role"], item["text"]), []).append(seconds)
        for item in candidates:
            seconds = source_seconds(item["timestamp"])
            times = canonical.get((item["role"], item["text"]), [])
            if item["record_type"] == "event_msg" and seconds is not None and any(abs(seconds - t) < 2 for t in times):
                mirrors += 1
                continue
            hits.append(item)
        limitations.extend({"kind": kind, "source": str(path), "count": count} for kind, count in counts.items() if count)
    hits.sort(key=lambda item: (source_seconds(item["timestamp"]) if item["timestamp"] else float("-inf"),
                               item["source"], item["position"]))
    total_hits = len(hits)
    if total_hits > max_hits:
        limitations.append({"kind": "hit_limit", "omitted": total_hits - max_hits})
    return {"roots": [str(root) for root in roots], "files_scanned": len(files),
            "matched_messages": total_hits, "mirrored_messages_omitted": mirrors,
            "hits": hits[:max_hits], "limitations": limitations,
            "coverage_complete": not limitations, "privacy": "private local evidence; do not publish"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, action="append", help="Explicit source root; repeatable; replaces discovery")
    parser.add_argument("--query", action="append", default=[], help="Literal case-insensitive topic; repeated queries match any")
    parser.add_argument("--signal", choices=sorted(SIGNALS), action="append", default=[])
    parser.add_argument("--since", type=bound)
    parser.add_argument("--until", type=bound)
    parser.add_argument("--max-files", type=positive, default=500)
    parser.add_argument("--max-bytes", type=positive, default=5_000_000)
    parser.add_argument("--max-hits", type=positive, default=200)
    args = parser.parse_args()
    if not args.query and not args.signal:
        parser.error("provide --query or --signal to bound the search")
    if args.since is not None and args.until is not None and args.since > args.until:
        parser.error("--since must precede --until")
    roots = args.root or discover_roots(Path.home(), dict(os.environ))
    print(json.dumps(scan(roots, queries=args.query, signals=args.signal, since=args.since, until=args.until,
                          max_files=args.max_files, max_bytes=args.max_bytes, max_hits=args.max_hits), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
