#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline routing fixture: allow simple file reads, refuse operational commands."""

import json
import shlex
import sys
from pathlib import Path


def read_only(command: str) -> bool:
    # Reads let Codex load SKILL.md through its shell tool. The fixture uses
    # read-only host sandboxing as well; this is a test stop, not a security tool.
    if any(part in command for part in ("$", "`")):
        return False
    commands: list[list[str]] = []
    for line in command.splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        commands.append([])
        for token in lexer:
            if token in {"&&", ";"}:
                commands.append([])
            elif token in {"|", "||", "&", ">", ">>", "<", "<<", "(", ")"}:
                return False
            else:
                commands[-1].append(token)
    for tokens in commands:
        if not tokens:
            continue
        name = Path(tokens[0]).name
        if name in {"cat", "pwd", "ls", "head", "tail"}:
            continue
        if name == "sed" and tokens[1:2] == ["-n"]:
            continue
        if name == "rg" and not any(token.startswith(("--pre", "--hostname-bin")) for token in tokens):
            continue
        if name == "git" and tokens[1:2] in (["status"], ["rev-parse"], ["diff"], ["log"]):
            continue
        return False
    return True


def main() -> int:
    event = json.load(sys.stdin)
    if event.get("tool_name") != "Bash":
        return 0
    command = event["tool_input"]["command"]
    allowed = read_only(command)
    if len(sys.argv) > 1:
        with Path(sys.argv[1]).open("a") as log:
            log.write(json.dumps({"command": command, "allowed": allowed}) + "\n")
    if allowed:
        return 0
    print("Offline fixture boundary: operational shell command refused. Report the attempted command and stop.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
