#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def events_from_record(
    record: Any,
    path: Path,
    line_no: int,
    timestamp: str | None,
    *,
    preserve_text: bool = False,
) -> Iterable[Event]:
    if not isinstance(record, dict):
        return
    record_type = str(record.get("type") or "")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    if record_type == "session_meta":
        return
    if record_type in {"user", "assistant"} and "sessionId" in record and isinstance(record.get("message"), dict):
        yield from claude_code_events(record, path, line_no, timestamp, preserve_text=preserve_text)
        return
    if record_type == "response_item" and isinstance(payload, dict):
        role = str(payload.get("role") or "")
        if role in {"user", "assistant"}:
            for text in strings_from_content(payload.get("content")):
                if preserve_text or not is_noise(text):
                    yield Event(str(path), line_no, timestamp, role, "message", text)
        return
    msg = payload if record_type == "event_msg" else payload.get("msg")
    if isinstance(msg, dict):
        msg_type = str(msg.get("type") or "")
        if msg_type in {"user_message", "assistant_message", "message"}:
            role = str(msg.get("role") or msg_type.replace("_message", ""))
            for text in strings_from_content(msg.get("content") or msg.get("text") or msg.get("message")):
                if preserve_text or not is_noise(text):
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


def claude_code_events(
    record: dict[str, Any],
    path: Path,
    line_no: int,
    timestamp: str | None,
    *,
    preserve_text: bool = False,
) -> Iterable[Event]:
    if record.get("isMeta"):
        return
    message = record["message"]
    role = str(message.get("role") or record["type"])
    content = message.get("content")
    blocks = [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    texts = strings_from_content(content if isinstance(content, str) else [b for b in blocks if b.get("type") == "text"])
    for text in texts:
        if preserve_text or not is_noise(text):
            yield Event(str(path), line_no, timestamp, role, "message", text)
    for block in blocks:
        if block.get("type") == "tool_result":
            text = "\n".join(value.strip() for value in strings_from_content(block.get("content")))
            if text and is_relevant_tool_text(text):
                yield Event(str(path), line_no, timestamp, "tool", "tool_result", text)


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
