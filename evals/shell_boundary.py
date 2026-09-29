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


def git_read_only(arguments: list[str]) -> bool:
    # Skip global options such as `-C <path>` and `-c key=value` to reach the subcommand.
    while arguments[:1] in (["-C"], ["-c"]):
        arguments = arguments[2:]
    command, rest = (arguments[0], arguments[1:]) if arguments else ("", [])
    if command in {"status", "rev-parse", "diff", "log", "ls-files", "merge-base", "rev-list", "show", "cat-file", "for-each-ref"}:
        return True
    if command == "hash-object":
        return "-w" not in rest
    if command == "worktree":
        return rest[:1] == ["list"]
    if command == "config":
        return bool(rest) and rest[0] in {"--get", "--get-all", "--list", "-l"}
    if command == "branch":
        return all(option in {"--show-current", "-vv", "-v", "--list", "-a", "-r"} for option in rest)
    if command == "remote":
        return rest in ([], ["-v"]) or rest[:1] == ["get-url"]
    if command == "symbolic-ref":
        # One ref name reads it; a second argument would write it.
        return len([argument for argument in rest if not argument.startswith("-")]) <= 1
    return False


def read_only(command: str) -> bool:
    # Reads let Codex load SKILL.md through its shell tool. The fixture uses
    # read-only host sandboxing as well; this is a test stop, not a security tool.
    if any(part in command for part in ("$", "`")):
        return False
    commands: list[list[str]] = []
    for line in command.splitlines():
        # A read that falls back to another read is still a read.
        line = line.replace("2>/dev/null", "")
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        commands.append([])
        for token in lexer:
            if token in {"&&", ";", "||"}:
                commands.append([])
            elif token in {"|", "&", ">", ">>", "<", "<<", "(", ")"}:
                return False
            else:
                commands[-1].append(token)
    for tokens in commands:
        if not tokens:
            continue
        name = Path(tokens[0]).name
        if name in {"cat", "pwd", "ls", "head", "tail", "echo", "printf", "shasum", "stat", "wc", "file"}:
            continue
        if name == "sed" and tokens[1:2] == ["-n"]:
            continue
        if name == "rg" and not any(token.startswith(("--pre", "--hostname-bin")) for token in tokens):
            continue
        if name == "git" and git_read_only(tokens[1:]):
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
