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
import time
from collections import Counter
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))
from skills.github.scripts import github_read

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
ANSWERED = re.compile(r"\s*[*_]*(yes|no|not yet)[*_]*\s*[,.;—–-]", re.IGNORECASE)
NEGATED = re.compile(r"\b(nothing|no|not|never)\b[^.!?\n]{0,20}$", re.IGNORECASE)
COST = re.compile(r"\b(costs?|upkeep|maintenance|ongoing effort)\b", re.IGNORECASE)
DEVELOPER = re.compile(r"\b(experienced|senior|sr\.?)\s+(professional\s+)?(developers?|devs?|engineers?)\b", re.IGNORECASE)
QUESTION_GUIDANCE = Path(__file__).resolve().parents[2] / "references/talking-with-the-director.md"
QUESTION_SECTION = "## Director Questions"
REASON = (
    "Your reply asks Chris to decide something but has no bold Q number. Every question "
    "to him starts with a bold Q### from the shared counter (talking-with-the-director.md): "
    "allocate one with `uv run <direction-skill-dir>/scripts/question_id.py next` and repeat "
    "the reply with it. If the reply asks him nothing, end the turn again unchanged."
)
LOOKUP_TIMEOUT = 3


def asks_director(text: str) -> bool:
    questions = (match[0] for match in SENTENCE.finditer(text)
                 if not ANSWERED.match(text, match.end()))
    # "Nothing is waiting on your approval" reports that there is no ask.
    return (any(NUMBERED.search(question) or ADDRESSED.search(question.strip(" \t*_-")) for question in questions)
            or any(not NEGATED.search(text, endpos=ask.start()) for ask in ASK.finditer(text)))


def missing_context(message: str) -> str | None:
    text = QUOTED.sub(" ", message)
    if not asks_director(text):
        return None
    has_context = bool(COST.search(text) and DEVELOPER.search(text))
    if not NUMBERED.search(text):
        return REASON if has_context else f"{REASON} {director_question_rule()}"
    if has_context:
        return None
    if not has_unnumbered_ask(text) and registered_records_cover(message):
        return None
    return (f"{director_question_rule()} "
            "Repeat the reply. If the reply asks nothing, end the turn again unchanged.")


def has_unnumbered_ask(text: str) -> bool:
    questions = [match[0] for match in SENTENCE.finditer(text)
                 if not ANSWERED.match(text, match.end()) and ADDRESSED.search(match[0].strip(" \t*_-"))]
    if any(not NUMBERED.search(question) for question in questions):
        return True
    for ask in ASK.finditer(text):
        if not NEGATED.search(text, endpos=ask.start()):
            start = max(text.rfind(separator, 0, ask.start()) for separator in ".!?:;\n") + 1
            if not NUMBERED.search(text[start:ask.end()]):
                return True
    return False


def read_question(url: str, deadline: float) -> dict:
    from question_record import delivery

    match = delivery.COMMENT_URL.fullmatch(url)
    if match is None:
        raise ValueError("question source link is invalid")
    reader = github_read.GitHubReader(strict_actor=True, repository=match[1], deadline_at=deadline,
                                     gh_prefix_args=github_read.automation_only_gh_prefix_args())
    comment = reader.get_json(f"/repos/{match[1]}/issues/comments/{match[3]}", step="question_relay")
    if comment["html_url"] != url:
        raise ValueError("question source identity changed")
    return {"body": comment["body"], "url": url, "author": comment["user"]["login"],
            "issue": f"https://github.com/{match[1]}/issues/{match[2]}"}


def registered_records_cover(message: str) -> bool:
    import question_record

    links = dict.fromkeys(match[0] for match in question_record.delivery.COMMENT_URL.finditer(message))
    if not links:
        return False
    try:
        queue = question_record.question_id.configured_queue()
        if not queue:
            return False
        deadline = time.time() + LOOKUP_TIMEOUT
        owner = queue.split("/", 1)[0]
        trusted = {
            owner.casefold(),
            *(author.casefold() for author in question_record.delivery.oq.default_decision_authors()),
        }
        requested_ids = {match[0].strip("*") for match in NUMBERED.finditer(QUOTED.sub(" ", message))}
        registered_ids = set()
        for url in links:
            source = read_question(url, deadline)
            if source["author"].casefold() not in trusted:
                continue
            registered_ids.add(question_record.read_record(source)["question_id"])
            if requested_ids <= registered_ids:
                return True
        return False
    except (OSError, ValueError, KeyError, TypeError, github_read.GitHubReadError, github_read.GitHubReadShapeError):
        return False


def director_question_rule() -> str:
    try:
        _, section = QUESTION_GUIDANCE.read_text(encoding="utf-8").split(f"\n{QUESTION_SECTION}\n", 1)
    except (OSError, ValueError):
        return f"Follow {QUESTION_GUIDANCE}#director-questions."
    return " ".join(section.split("\n## ", 1)[0].split())


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


def verdict(payload: object) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "Stop":
        return None, None
    if payload.get("stop_hook_active"):
        return "second-pass", None
    message = last_message(payload)
    reason = missing_context(message) if message is not None else None
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


def hook(stdin, stdout, env: Mapping[str, str]) -> int:
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
            "time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
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
