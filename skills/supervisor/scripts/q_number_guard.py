#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import stat
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

NUMBERED = re.compile(r"^\*\*Q[0-9]+[. —–-]", re.MULTILINE)
QUOTED = re.compile(r'(?s:```.*?```)|`[^`\n]*`|^[ \t]*>.*$|"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|https?://\S+', re.MULTILINE)
SENTENCE = re.compile(r"[^.!?:;\n]*[?]")
ADDRESSED = re.compile(
    r"\b(you|your|chris|director|ok|okay|sound good|any objections?)\b"
    r"|^\W*(should|shall|may|can|could|do|does|want|ok|okay|approve|which|go ahead|proceed|merge|land)\b",
    re.IGNORECASE,
)
ANSWERED = re.compile(r"\s*[*_]*(yes|no|not yet)[*_]*\s*[,.;—–-]", re.IGNORECASE)
QUESTION_OUTPUT = re.compile(
    r"^\*\*Q[0-9]+[.] [^\n]+\*\*\n\n- \*\*Context:.*?"
    r"\nOwning issue: https://github\.com/[^\s]+\n\nOriginal question: "
    r"https://github\.com/[^\s]+/issues/[0-9]+#issuecomment-[0-9]+",
    re.MULTILINE | re.DOTALL,
)
QUESTION_COMMAND = re.compile(r"(?:^|[\s/])oq\.py['\"]?\s+(?:create|update|show-question)\b")


def asks_director(text: str) -> bool:
    return bool(NUMBERED.search(text)) or any(
        ADDRESSED.search(match[0].strip(" \t*_-")) and not ANSWERED.match(text, match.end()) for match in SENTENCE.finditer(text)
    )


def director_question_rule() -> str:
    return (
        "Create the Director question with `oq.py create --record-file FILE --owner OWNER`, "
        "or revise an unanswered one with `oq.py update`. "
        "For an existing question, run `oq.py show-question ORIGINAL_URL --chat`.\n"
        "Paste the helper's full printed question and original link unchanged, including all choices; "
        "do not send just the heading or a link. If no question was asked, end unchanged."
    )


def missing_context(message: str, printed: tuple[str, ...] = ()) -> str | None:
    for question in printed:
        message = message.replace(question, "")
    return director_question_rule() if asks_director(QUOTED.sub(" ", message)) else None


def transcript_entries(payload: Mapping) -> list[dict]:
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return []
    path = Path(path).expanduser()
    if not path.is_file():
        return []
    entries = []
    with path.open(encoding="utf-8") as transcript:
        for line in transcript:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def printed_questions(entries: list[dict]) -> tuple[str, ...]:
    calls = set()
    printed = {}
    for entry in entries:
        content = entry.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if (
                entry.get("type") == "assistant"
                and block.get("type") == "tool_use"
                and block.get("name") == "Bash"
                and QUESTION_COMMAND.search(block.get("input", {}).get("command", ""))
            ):
                calls.add(block.get("id"))
            if (
                entry.get("type") != "user"
                or block.get("type") != "tool_result"
                or block.get("tool_use_id") not in calls
                or block.get("is_error")
            ):
                continue
            result = entry.get("toolUseResult", {})
            if not isinstance(result, dict) or result.get("interrupted") or result.get("exitCode", 0) != 0:
                continue
            for match in QUESTION_OUTPUT.finditer(result.get("stdout", "")):
                question = match[0]
                printed[NUMBERED.match(question)[0]] = question
    return tuple(printed.values())


def last_message(payload: Mapping) -> str | None:
    message = payload.get("last_assistant_message")
    if isinstance(message, str):
        return message
    text = None
    for entry in transcript_entries(payload):
        content = entry.get("message", {}).get("content") if entry.get("type") == "assistant" else None
        parts = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"] \
            if isinstance(content, list) else [content] if isinstance(content, str) else []
        if any(parts):
            text = "\n".join(parts)
    return text


def verdict(payload: object) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "Stop":
        return None, None
    if payload.get("stop_hook_active"):
        return "second-pass", None
    message = last_message(payload)
    reason = missing_context(message, printed_questions(transcript_entries(payload))) if message is not None else None
    return ("caught" if reason else "pass"), reason


def log_path(env: Mapping[str, str]) -> Path:
    # The shared Code home, never a provider home, so both accounts write one log.
    return Path(env.get("CODE_HOME") or Path.home() / ".code").expanduser() / "skill-data/q-number-guard.jsonl"


def append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (json.dumps(record, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("log destination is not a regular file")
        os.write(fd, data)
    finally:
        os.close(fd)


def hook(stdin: TextIO, stdout: TextIO, env: Mapping[str, str]) -> int:
    try:
        payload = json.load(stdin)
        result, reason = verdict(payload)
    except (OSError, ValueError, TypeError, AttributeError):
        # Fail open on bad input; Claude Code also lets the stop proceed if this hook crashes.
        return 0
    if result is None:
        return 0
    try:
        session = payload.get("session_id")
        append(log_path(env), {
            "time": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "session": session if isinstance(session, str) else None,
            "verdict": result,
        })
    except (OSError, ValueError):  # A lost log line is not a reason to change the decision.
        pass
    if result == "caught":
        stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
    return 0


def report(path: Path, since: dt.date | None, until: dt.date | None) -> dict:
    counts = Counter()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                day = dt.datetime.fromisoformat(record["time"]).date()
            except (ValueError, KeyError, TypeError):
                counts["unreadable"] += 1
                continue
            if (since is None or day >= since) and (until is None or day <= until):
                counts[record.get("verdict", "unreadable")] += 1
    return {"log": str(path), "since": since and since.isoformat(), "until": until and until.isoformat(),
            "counts": {name: counts[name] for name in ("pass", "caught", "second-pass")}
            | {name: count for name, count in counts.items() if name not in ("pass", "caught", "second-pass")}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Director questions once, or report verdict counts")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("hook", help="Run as a Claude Code Stop hook (reads its JSON on stdin)")
    counts = commands.add_parser("report", help="Print verdict counts for a date range (UTC days, inclusive)")
    counts.add_argument("--since", type=dt.date.fromisoformat, help="First day, YYYY-MM-DD")
    counts.add_argument("--until", type=dt.date.fromisoformat, help="Last day, YYYY-MM-DD")
    args = parser.parse_args(argv)
    if args.command == "hook":
        return hook(sys.stdin, sys.stdout, os.environ)
    print(json.dumps(report(log_path(os.environ), args.since, args.until), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
