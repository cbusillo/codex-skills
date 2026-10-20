#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Stop hook: send back, once, a reply that asks the Director to decide without a bold Q number.

`hook` reads Claude Code's Stop input on stdin. Any error, or a message it
cannot read, lets the stop proceed; the settings entry's short `timeout` bounds
a stalled stdin, after which Claude Code also lets the stop proceed.
`report` prints the logged verdict counts for a date range.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import stat
import sys
from collections import Counter
from pathlib import Path
from typing import Mapping

NUMBERED = re.compile(r"[*][*]Q[0-9]{3,}\b")
# Quoted material repeats old questions; only the reply's own words count. Only a
# code fence spans lines, so a blockquote never hides the text after it.
QUOTED = re.compile(r"(?s:```.*?```)|`[^`\n]*`|^[ \t]*>.*$|\"[^\"\n]*\"|“[^”\n]*”|‘[^’\n]*’|https?://\S+",
                    re.MULTILINE)
# A clause, so a lead-in such as "One decision:" does not hide "should I ...?".
SENTENCE = re.compile(r"[^.!?:;\n]*[?]")
ADDRESSED = re.compile(
    r"\b(you|your|chris|director|ok|okay|sound good|any objections?)\b"
    r"|^\W*(should|shall|may|can|could|do|does|want|ok|okay|approve|which|go ahead|proceed|merge|land)\b",
    re.IGNORECASE,
)
# Asks that need no question mark, in the Supervisor's usual phrasings.
ASK = re.compile(
    r"\b(reply yes|say yes|say the word|let me know (if|whether|which)|your call|needs your (decision|approval|answer|ok)"
    r"|waiting on your (decision|approval|answer)|for you to decide)\b",
    re.IGNORECASE,
)
# A status report answers its own question in place: "Can the train merge yet? No, CI is running."
ANSWERED = re.compile(r"\s*[*_]*(yes|no|not yet)\b", re.IGNORECASE)
REASON = (
    "Your reply asks Chris to decide something but has no bold Q number. Every question "
    "to him starts with a bold Q### from the shared counter (talking-with-the-director.md): "
    "allocate one with `uv run <direction-skill-dir>/scripts/question_id.py next` and repeat "
    "the reply with it. If the reply asks him nothing, end the turn again unchanged."
)


def asks_director(message: str) -> bool:
    text = QUOTED.sub(" ", message)
    if NUMBERED.search(text):
        return False
    questions = (match[0].strip(" \t*_-") for match in SENTENCE.finditer(text)
                 if not ANSWERED.match(text, match.end()))
    return any(ADDRESSED.search(question) for question in questions) or bool(ASK.search(text))


def last_message(payload: Mapping) -> str | None:
    message = payload.get("last_assistant_message")
    if isinstance(message, str):
        return message
    # Older harnesses omit the field; the transcript may lag but is the only other source.
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return None
    path = Path(path).expanduser()
    if not path.is_file():  # A FIFO or device could block until the harness timeout.
        return None
    text = None
    with open(path, encoding="utf-8") as transcript:
        for line in transcript:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            content = entry.get("message", {}).get("content") if entry.get("type") == "assistant" else None
            parts = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"] \
                if isinstance(content, list) else [content] if isinstance(content, str) else []
            if any(parts):
                text = "\n".join(parts)
    return text


def verdict(payload: object) -> str | None:
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "Stop":
        return None
    if payload.get("stop_hook_active"):
        return "second-pass"
    message = last_message(payload)
    return "caught" if message is not None and asks_director(message) else "pass"


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


def hook(stdin, stdout, env: Mapping[str, str]) -> int:
    try:
        payload = json.load(stdin)
        result = verdict(payload)
    except (OSError, ValueError, TypeError, AttributeError):
        # Fail open on bad input; Claude Code also lets the stop proceed if this hook crashes.
        return 0
    if result is None:
        return 0
    try:
        session = payload.get("session_id")
        append(log_path(env), {
            "time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "session": session if isinstance(session, str) else None,
            "verdict": result,
        })
    except (OSError, ValueError):  # A lost log line is not a reason to change the decision.
        pass
    if result == "caught":
        stdout.write(json.dumps({"decision": "block", "reason": REASON}) + "\n")
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
    parser = argparse.ArgumentParser(description=__doc__)
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
