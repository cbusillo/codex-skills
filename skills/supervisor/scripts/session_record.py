#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read explicit Supervisor ledgers and harness transcripts; never discover accounts."""

from __future__ import annotations

import json
import re
from pathlib import Path

MEMORY_CITATION_TAIL = re.compile(
    r"^<oai-mem-citation>(?:(?!</?oai-mem-citation>).)*"
    r"^</oai-mem-citation>\s*\Z",
    re.MULTILINE | re.DOTALL,
)
SAFE = re.compile(r"^(?:\*\*)?Safe to exit: yes\.?(?:\*\*)?$")


def outside_fences(lines: list[str]) -> bool:
    """The final verdict must be outside a completed Markdown fence."""
    fence = None
    minimum_indent = 0
    list_indent = None
    for line in lines:
        line = line.expandtabs(4)
        indent = len(line) - len(line.lstrip(" "))
        listed_line = re.match(r"^( *(?:(?:[-+*]|[0-9]+[.)]) +)+)", line)
        if fence is None:
            if listed_line:
                list_indent = len(listed_line[1])
            elif line.strip() and list_indent is not None and indent < list_indent:
                list_indent = None
        match = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if not match and fence is None:
            listed = re.match(r"^(\s*(?:(?:[-+*]|[0-9]+[.)])\s+)+)(`{3,}|~{3,})(.*)$", line)
            if listed:
                # A list fence closes relative to its content column. A flush-left
                # marker can open a new outer block and cannot prove closure.
                prefix, fence, _ = listed.groups()
                minimum_indent = len(prefix.expandtabs(4))
                continue
        if not match:
            continue
        marker, rest = match.groups()
        if fence is None:
            fence = marker
            minimum_indent = list_indent if list_indent is not None else (indent if indent > 3 else 0)
        elif (marker[0] == fence[0] and len(marker) >= len(fence) and not rest.strip()
              and re.fullmatch(r" {" + str(minimum_indent) + "," + str(minimum_indent + 3)
                               + "}" + re.escape(marker) + r"[ \t]*", line)):
            fence = None
    return fence is None


def read_jsonl(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"{path.name}:{number}: incomplete or invalid JSON"
                ) from error
            if not isinstance(record, dict):
                raise TypeError(f"{path.name}:{number}: record is not an object")
            records.append(record)
    return records


def load_ledger(path: Path) -> list[dict]:
    """Paths are relative to the ledger, never to the caller's working directory."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise TypeError("ledger must be an array of session objects")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise TypeError("ledger entry must be an object")
        if entry.get("harness") not in {"claude", "codex"}:
            raise ValueError("ledger harness must be claude or codex")
        for key in ("session_id", "transcript"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise ValueError(f"ledger requires {key}")
        if entry["session_id"] in seen:
            raise ValueError("duplicate session_id")
        seen.add(entry["session_id"])
        if isinstance(entry.get("tty"), str):
            entry["tty"] = entry["tty"].removeprefix("/dev/")
        transcript = Path(entry["transcript"]).expanduser()
        entry["transcript"] = str((path.parent / transcript).resolve())
    return entries


def text_content(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        c.get("text", "")
        for c in content
        if isinstance(c, dict) and isinstance(c.get("text"), str)
    )


def strip_claude_exit_tail(records: list[dict]) -> list[dict]:
    """Only the complete local /exit sequence may follow a close-out response."""
    if len(records) < 3 or not all(r.get("type") == "user" for r in records[-3:]):
        return records
    caveat, command, output = records[-3:]
    text = text_content(command.get("message", {}).get("content"))
    exit_command = re.fullmatch(
        r"<command-name>/exit</command-name>\s*<command-message>exit</command-message>"
        r"\s*<command-args></command-args>",
        text.strip(),
    )
    caveat_text = text_content(caveat.get("message", {}).get("content"))
    output_text = text_content(output.get("message", {}).get("content"))
    if (
        caveat.get("isMeta") is True
        and caveat_text.startswith("<local-command-caveat>")
        and caveat_text.endswith("</local-command-caveat>")
        and exit_command
        and output_text.strip()
        == "<local-command-stdout>(no content)</local-command-stdout>"
    ):
        return records[:-3]
    return records


def summarize(records: list[dict], harness: str) -> dict:
    result = {
        "last_text": "",
        "timestamp": None,
        "context_tokens": None,
        "total_tokens": None,
        "turn_end": None,
        "at_turn_end": False,
        "safe_verdict": False,
    }
    if harness == "claude":
        records = strip_claude_exit_tail(records)
    for record in records:
        payload = record.get("payload") or {}
        kind = payload.get("type")
        if harness == "claude":
            message = record.get("message") or {}
            if record.get("type") == "user":
                result.update(last_text="", at_turn_end=False)
            if record.get("type") == "assistant":
                usage = message.get("usage")
                if isinstance(usage, dict):
                    result["context_tokens"] = sum(
                        usage.get(k, 0) or 0
                        for k in (
                            "input_tokens",
                            "cache_read_input_tokens",
                            "cache_creation_input_tokens",
                        )
                    )
                text = text_content(message.get("content"))
                # Tool use after a verdict invalidates it, even in the same message.
                blocks = message.get("content") or []
                tool_use = isinstance(blocks, list) and any(
                    isinstance(c, dict) and c.get("type") == "tool_use" for c in blocks
                )
                result.update(
                    last_text=text,
                    timestamp=record.get("timestamp"),
                    at_turn_end=not tool_use,
                )
        else:
            if kind == "token_count":
                info = payload.get("info") or {}
                result["context_tokens"] = (info.get("last_token_usage") or {}).get(
                    "input_tokens"
                )
                result["total_tokens"] = (info.get("total_token_usage") or {}).get(
                    "total_tokens"
                )
            if kind in {
                "task_started",
                "user_message",
                "function_call",
                "custom_tool_call",
            }:
                result.update(at_turn_end=False, last_text="", turn_end=None)
            if kind == "agent_message":
                result.update(
                    last_text=payload.get("message", ""),
                    timestamp=record.get("timestamp"),
                )
            if kind == "message" and payload.get("role") == "assistant":
                result.update(
                    last_text=text_content(payload.get("content")),
                    timestamp=record.get("timestamp"),
                )
            if kind in {"task_complete", "turn_aborted"}:
                result.update(
                    at_turn_end=True, turn_end=kind, timestamp=record.get("timestamp")
                )
    # Citation metadata may follow the verdict; retain the raw response for readers.
    verdict_text = MEMORY_CITATION_TAIL.sub("", result["last_text"])
    lines = verdict_text.rstrip().splitlines()
    result["safe_verdict"] = bool(
        lines
        and SAFE.fullmatch(re.sub(r"^[-*] ", "", lines[-1].strip()).replace("**", ""))
        and outside_fences(lines)
        and result["at_turn_end"]
        and result["turn_end"] != "turn_aborted"
    )
    return result


def session_status(entry: dict) -> dict:
    path = Path(entry["transcript"])
    before = path.stat()
    records = read_jsonl(path)
    identities = {
        record.get("sessionId") for record in records if record.get("sessionId")
    }
    if entry["harness"] == "codex":
        identities = {
            record.get("payload", {}).get("id")
            for record in records
            if record.get("type") == "session_meta"
        }
    if identities != {entry["session_id"]}:
        raise ValueError("transcript identity does not match the ledger session")
    result = summarize(records, entry["harness"])
    after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise ValueError("transcript changed during read; check again")
    result.update(
        session_id=entry["session_id"],
        harness=entry["harness"],
        transcript=str(path),
        mtime=after.st_mtime,
        size=after.st_size,
    )
    return result
