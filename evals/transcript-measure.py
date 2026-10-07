# /// script
# requires-python = ">=3.12"
# ///
"""Measure how interactive Claude Code and Codex sessions load the skill that owns each step.

Reads local transcripts only and changes nothing. For each skill whose step a
session took, it reports whether the skill was loaded before the first step, how
many of its loads were re-reads, whether a later turn loaded it again before
using it, and whether it was loaded again after a compaction before its next
step. Steps are detected from the programs each shell segment runs, so text that
merely mentions a helper does not count. Loads that followed a command-policy
block for that skill are counted separately; they are recoveries, not successes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import sys
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

# Patterns match the start of a shell segment, either as written or with its helper path
# reduced to the program name ("uv run /x/gh-pr.py merge 3" also as "gh-pr.py merge 3").
STEPS = {
    "github": r"gh pr (create|merge|ready|edit|comment|view|update-branch)|git push"
    r"|gh-pr(\.py)? (--repo \S+ )?(create|merge|ready|edit|comment|view|update-branch|supersede)"
    r"|git-(commit|push)-as-bot|reconcile-runtime-checkout|gh api|gh-with-env-token (api|pr|issue)|github_api(\.py)?"
    r"|gh issue (create|edit|close|comment|view)|gh-issue|gh-comment",
    "babysit-pr": r"gh pr checks|gh run watch|gh_pr_watch|github_workflow_babysit|gh-pr(\.py)? (--repo \S+ )?checks|github-ci-diagnose",
    "github-plan": r"gh issue list|gh search issues|gh project|gh-plan(\.py)?",
    "python-uv-workflow": r"uv (sync|add|lock|remove)|pytest|pip3? install|python3? -m (pytest|pip|venv)"
    r"|uv run (--quiet )?(?!--quiet)(?!--with)(?!--no-project)(?!python3? -c)(?!\S*skills/)(?!\$)\S+",
    "infra-ops": r"ssh |systemctl |docker (-H|--context|compose|exec) |pct |qm |zfs |tailscale |npmplus-ops|private-context-check",
    "docs-lookup": r"op (read|item)|security find-(generic|internet)-password|bw get",
    "launchplane": r"launchplane(-context|-ordinary-agent|-owner-review|-write-action|-train-drive)?(\.py)?( |$)|check-agent-operator-contract",
    "model-review": r"codex exec|claude (-p|--print)|agy |gemini |review_with_model",
    "local-llm": r"lms |lm_studio_|local_codex_agent",
    "google-seo": r"lighthouse |bing-webmaster|google-search-console|google-cloud-inventory",
    "partdb": r"partdb-(read|write)",
    "direction": r"direction_(audit|mark)",
    "work-closeout": r"repo_cleanup|git worktree remove|dev-worktree retire",
}
STEP_PATTERNS = {skill: re.compile(pattern) for skill, pattern in STEPS.items()}
COMMIT = re.compile(r"git commit|git-commit-as-bot|gh pr create|gh-pr(\.py)? (--repo \S+ )?create")
CODE_FILE = re.compile(r"\.(py|ts|tsx|js|jsx|mjs|rs|go|kt|java|swift|vue|css|scss)$")
SKILL_FILE = re.compile(r"(?:^|/)([a-z0-9-]+)/SKILL\.md$")
BLOCK = re.compile(r"Blocked by the `([a-z0-9-]+)` skill")
SLASH = re.compile(r"<command-name>/?(?:[a-z-]+:)?([a-z0-9-]+)</command-name>")
HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?.*?\n.*?\n\1\b", re.S)
EXEC_CMD = re.compile(r"\bcmd\s*:\s*(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)")
OPERATORS = ("||", "&&", "|", ";", "\n", "$(", "`", ")")
FILE_READERS = ("cat", "sed", "head", "tail", "less", "bat", "nl", "awk")
READ_ONLY = (*FILE_READERS, "echo", "grep", "rg", "ls", "#", "printf", "wc", "jq", "find")
ALIASES = {"claude-in-chrome": "browser-ui-review"}
COMPACT_SUMMARY = "This session is being continued from a previous conversation"
# User-role records the harness writes itself: background-task results and command echoes.
HARNESS_NOTES = ("<task-notification>", "<bash-input>", "<bash-stdout>", "<bash-stderr>", "<local-command-")
BUILTIN = re.compile(r"<command-name>/(exit|model|compact|clear|context|cost|status|resume|config|login|logout"
                     r"|rename|help|fast|effort|usage|doctor|mcp|plugin|agents|memory|permissions)</command-name>")


@dataclass
class Session:
    harness: str
    session_id: str
    project: str
    events: list[tuple] = field(default_factory=list)


def split_unquoted(command: str) -> list[str]:
    """Split at shell operators outside quotes, so quoted search patterns stay whole."""
    parts, current, quote, index = [], [], None, 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'" and index + 1 < len(command):
            current.append(command[index:index + 2])
            index += 2
            continue
        if quote:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif operator := next((op for op in OPERATORS if command.startswith(op, index)), None):
            parts.append("".join(current))
            current = []
            index += len(operator)
            continue
        current.append(char)
        index += 1
    parts.append("".join(current))
    return parts


def segments(command: str) -> Iterator[str]:
    """Split a shell command into program segments, in order, without heredoc bodies."""
    for segment in split_unquoted(HEREDOC.sub(" ", command)):
        segment = re.sub(r"^((then|do|else|if|while|until|!|[({])\s*)+", "", segment.strip())
        segment = re.sub(r"^((\w+=\S*|export|env|time|timeout \d+\w?|cd \S+)\s+)+", "", segment)
        if segment:
            yield segment


def skill_reads(segment: str) -> list[str]:
    """Skills whose SKILL.md a file-reading segment names as a file operand."""
    try:
        words = shlex.split(segment)
    except ValueError:
        words = segment.split()
    return [skill_name(m.group(1)) for word in words[1:]
            if not word.startswith("-") and (m := SKILL_FILE.search(word))]


def program(segment: str) -> str:
    """The segment with its helper path reduced to the program name."""
    segment = re.sub(r"^uv run (--quiet )?(\S*/)?(\S+\.py)\b", r"\3", segment)
    return re.sub(r"^\S*/(\S+)", r"\1", segment)


def skill_name(raw: str) -> str:
    name = raw.split(":")[-1]
    return ALIASES.get(name, name)


def command_events(command: str, edited_code: bool) -> list[tuple]:
    """Loads (reads of a SKILL.md) and steps found in one shell command, in order."""
    events: list[tuple] = []
    for segment in segments(command):
        if segment.startswith(FILE_READERS):
            events.extend(("load", skill) for skill in skill_reads(segment))
        if segment.startswith(READ_ONLY):
            continue
        forms = (segment, program(segment))
        events.extend(("step", skill, forms[1]) for skill, rx in STEP_PATTERNS.items()
                      if any(rx.match(form) for form in forms))
        if edited_code and COMMIT.match(forms[1]):
            events.append(("step", "jetbrains-inspection", "commit after code edits: " + forms[1]))
    return events


def blocks(text: str) -> list[tuple]:
    return [("block", skill_name(m.group(1))) for m in BLOCK.finditer(text)]


def timestamp(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(errors="replace") as handle:
        for line in handle:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def is_prompt(row: dict, content: object) -> bool:
    """A user-authored prompt, not a tool result, hook note, or compaction summary."""
    if row.get("isMeta") or row.get("isCompactSummary"):
        return False
    if isinstance(content, list):
        if any(part.get("type") == "tool_result" for part in content):
            return False
        content = next((part.get("text", "") for part in content if part.get("type") == "text"), "")
    if not isinstance(content, str) or content.startswith((COMPACT_SUMMARY, *HARNESS_NOTES)):
        return False
    return not BUILTIN.search(content)


def parse_claude(path: Path, since: dt.datetime, until: dt.datetime) -> Session | None:
    rows = read_jsonl(path)
    first = next((r for r in rows if r.get("timestamp")), None)
    if not first or not since <= timestamp(first["timestamp"]) < until:
        return None
    if next((r.get("entrypoint") for r in rows if r.get("entrypoint")), None) not in ("cli", "claude-desktop"):
        return None
    session = Session("claude", path.stem, path.parent.name)
    edited_code = False
    for row in rows:
        if row.get("type") == "system" and row.get("subtype") == "compact_boundary":
            session.events.append(("compact",))
            continue
        content = row.get("message", {}).get("content")
        if row.get("type") == "user" and is_prompt(row, content):
            text = content if isinstance(content, str) else json.dumps(content)
            session.events.append(("turn",))
            session.events.extend(("load", skill_name(m.group(1))) for m in SLASH.finditer(text))
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if part.get("type") == "tool_result":
                session.events.extend(blocks(json.dumps(part.get("content"))))
            if part.get("type") != "tool_use":
                continue
            name, arguments = part.get("name", ""), part.get("input") or {}
            if name == "Skill":
                session.events.append(("load", skill_name(arguments.get("skill", ""))))
            elif name == "Read":
                session.events.extend(("load", skill_name(m.group(1))) for m in SKILL_FILE.finditer(arguments.get("file_path", "")))
            elif name in ("Edit", "Write", "NotebookEdit"):
                edited_code = edited_code or bool(CODE_FILE.search(arguments.get("file_path", "")))
            elif name == "Bash":
                session.events.extend(command_events(arguments.get("command", ""), edited_code))
    return session


def js_string(literal: str) -> str:
    """The value of a JavaScript string literal in Codex's exec source."""
    body = literal[1:-1]
    try:
        if literal[0] == '"':
            return json.loads(literal)
        if literal[0] == "'":
            return json.loads('"' + re.sub(r'(?<!\\)"', '\\"', body.replace("\\'", "'")) + '"')
    except ValueError:
        pass
    return body.replace("\\n", "\n")


def codex_commands(payload: dict) -> list[str]:
    """Shell commands in a Codex tool call, from either the exec_command or the JavaScript exec form."""
    if payload.get("type") == "custom_tool_call":
        return [js_string(literal) for literal in EXEC_CMD.findall(payload.get("input", ""))]
    try:
        arguments = json.loads(payload.get("arguments") or "{}")
    except ValueError:
        return []
    command = arguments.get("cmd") or arguments.get("command") or ""
    return [" ".join(command) if isinstance(command, list) else str(command)]


def codex_edits(payload: dict) -> bool:
    text = payload.get("input") or payload.get("arguments") or ""
    # A patch inside the JavaScript exec form keeps its newlines as "\n" escapes.
    return bool(re.search(r"\*\*\* (Add|Update) File: \S+?\.(py|ts|tsx|js|jsx|mjs|rs|go|kt|java|swift|vue|css|scss)(?!\w)", text))


def parse_codex(path: Path, since: dt.datetime, until: dt.datetime) -> Session | None:
    rows = read_jsonl(path)
    meta = next((r.get("payload", {}) for r in rows if r.get("type") == "session_meta"), {})
    if meta.get("source") not in ("cli", "vscode") or str(meta.get("originator", "")).startswith("codex_exec"):
        return None
    if not meta.get("timestamp") or not since <= timestamp(meta["timestamp"]) < until:
        return None
    session = Session("codex", str(meta.get("id") or path.stem), Path(meta.get("cwd", "")).name)
    edited_code, turn_id = False, None
    for row in rows:
        payload = row.get("payload") or {}
        kind = payload.get("type")
        if row.get("type") == "compacted":
            session.events.append(("compact",))
        elif row.get("type") == "turn_context" and payload.get("turn_id") != turn_id:
            turn_id = payload.get("turn_id")
            session.events.append(("turn",))
        elif kind in ("function_call", "custom_tool_call"):
            edited_code = edited_code or codex_edits(payload)
            for command in codex_commands(payload):
                session.events.extend(command_events(command, edited_code))
        elif kind in ("function_call_output", "custom_tool_call_output"):
            session.events.extend(blocks(json.dumps(payload.get("output"))))
    return session


def sessions(claude_roots: Iterable[Path], codex_roots: Iterable[Path], since: dt.datetime, until: dt.datetime,
             exclude: set[str]) -> Iterator[Session]:
    floor = since.timestamp() - 86400
    for root in claude_roots:
        for path in sorted(root.glob("*/*.jsonl")):
            if path.stem not in exclude and path.stat().st_mtime >= floor:
                if session := parse_claude(path, since, until):
                    yield session
    for root in codex_roots:
        for path in sorted(root.glob("**/rollout-*.jsonl")):
            if path.stat().st_mtime >= floor and not any(s in path.name for s in exclude):
                if (session := parse_codex(path, since, until)) and session.session_id not in exclude:
                    yield session


def new_counts() -> dict[str, int]:
    return dict.fromkeys(("sessions", "before", "after_block", "loads", "rereads",
                          "later_turn", "later_turn_reloaded", "after_compact", "after_compact_reloaded"), 0)


def measure(session: Session, report: dict, misses: dict) -> None:
    """Add one session's per-skill outcomes to report[harness][skill]."""
    counts = report.setdefault(session.harness, defaultdict(new_counts))
    loaded: set[str] = set()        # loaded at any earlier point in the session
    loaded_before_turn: set[str] = set()
    turn_loads: set[str] = set()     # loaded during the current turn
    compact_loads: set[str] = set()  # loaded since the last compaction
    blocked: set[str] = set()
    forced: set[str] = set()
    first_step: set[str] = set()
    turn_checked: set[str] = set()
    compact_pending: set[str] = set()
    for event in session.events:
        kind = event[0]
        if kind == "turn":
            loaded_before_turn = set(loaded)
            turn_loads.clear()
            turn_checked.clear()
        elif kind == "compact":
            compact_loads.clear()
            compact_pending = set(loaded)
        elif kind == "block":
            blocked.add(event[1])
        elif kind == "load":
            skill = event[1]
            counts[skill]["loads"] += 1
            counts[skill]["rereads"] += skill in loaded
            if skill in blocked and skill not in loaded:
                forced.add(skill)  # its first load came only after a block
            loaded.add(skill)
            turn_loads.add(skill)
            compact_loads.add(skill)
        elif kind == "step":
            skill, detail = event[1], event[2]
            row = counts[skill]
            if skill not in first_step:
                first_step.add(skill)
                row["sessions"] += 1
                if skill in loaded and skill not in forced:
                    row["before"] += 1
                else:
                    misses[(session.harness, skill)].append(f"{session.session_id} {session.project}: {detail[:110]}")
            elif skill in loaded_before_turn and skill not in turn_checked:
                row["later_turn"] += 1
                row["later_turn_reloaded"] += skill in turn_loads
            turn_checked.add(skill)
            if skill in compact_pending:
                compact_pending.discard(skill)
                row["after_compact"] += 1
                row["after_compact_reloaded"] += skill in compact_loads
    for skill in first_step:
        counts[skill]["after_block"] += skill in forced


def ratio(part: int, whole: int) -> str:
    return f"{part}/{whole} ({100 * part // whole}%)" if whole else "-"


def render(report: dict, totals: dict) -> str:
    lines = []
    for harness in sorted(report):
        lines.append(f"{harness}: {totals[harness]['sessions']} interactive sessions, "
                     f"{totals[harness]['compactions']} compactions")
        lines.append(f"  {'skill':22} {'loaded before step':>20} {'after block':>12} {'re-reads':>16} "
                     f"{'later-turn reload':>18} {'after-compact reload':>21}")
        rows = sorted(report[harness].items(), key=lambda item: (-item[1]["sessions"], item[0]))
        for skill, row in rows:
            if not row["sessions"]:
                continue
            lines.append(f"  {skill:22} {ratio(row['before'], row['sessions']):>20} {row['after_block']:>12} "
                         f"{ratio(row['rereads'], row['loads']):>16} "
                         f"{ratio(row['later_turn_reloaded'], row['later_turn']):>18} "
                         f"{ratio(row['after_compact_reloaded'], row['after_compact']):>21}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", required=True, help="ISO time; sessions that started at or after it")
    parser.add_argument("--until", default="9999-12-31T00:00:00Z", help="ISO time; sessions that started before it")
    parser.add_argument("--claude-root", type=Path, action="append",
                        help="Claude Code projects directory (default ~/.claude/projects)")
    parser.add_argument("--codex-root", type=Path, action="append",
                        help="Codex sessions directory (default $CODEX_HOME/sessions or ~/.codex/sessions)")
    parser.add_argument("--exclude-session", action="append", default=[], help="session ID to leave out, such as this one")
    parser.add_argument("--misses", action="append", default=[], metavar="SKILL",
                        help="list sessions whose first step for SKILL came before any load")
    parser.add_argument("--json", action="store_true", help="print the counts as JSON")
    args = parser.parse_args(argv)
    claude_roots = args.claude_root or [Path.home() / ".claude" / "projects"]
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    codex_roots = args.codex_root or [codex_home / "sessions"]
    report: dict = {}
    misses: dict = defaultdict(list)
    totals: dict = defaultdict(lambda: {"sessions": 0, "compactions": 0})
    for session in sessions(claude_roots, codex_roots, timestamp(args.since), timestamp(args.until),
                            set(args.exclude_session)):
        totals[session.harness]["sessions"] += 1
        totals[session.harness]["compactions"] += sum(e[0] == "compact" for e in session.events)
        measure(session, report, misses)
    if args.json:
        print(json.dumps({"totals": totals, "skills": report}, indent=2, sort_keys=True))
    else:
        print(render(report, totals))
    for skill in args.misses:
        for harness in sorted(report):
            print(f"\nmisses, {harness}, {skill}:")
            for line in misses[(harness, skill)]:
                print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
