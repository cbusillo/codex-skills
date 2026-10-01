#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline routing fixture: allow simple file reads, refuse operational commands."""

import json
import re
import shlex
import sys
from pathlib import Path


def git_read_only(arguments: list[str]) -> bool:
    # Skip global options such as `-C <path>` and `-c key=value` to reach the subcommand.
    while arguments[:1] in (["-C"], ["-c"]):
        arguments = arguments[2:]
    command, rest = (arguments[0], arguments[1:]) if arguments else ("", [])
    if any(argument.startswith("--output") for argument in rest):
        return False  # show, diff, and log can write files with --output
    if command in {"status", "rev-parse", "diff", "diff-index", "diff-files", "log", "ls-files", "merge-base", "rev-list", "show", "cat-file", "for-each-ref"}:
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
        if any(argument in {"-d", "--delete"} for argument in rest):
            return False
        return len([argument for argument in rest if not argument.startswith("-")]) <= 1
    return False


def shell_expansion(command: str) -> bool:
    """Keep quoted regex end anchors, but refuse shell expansion syntax."""
    quote = None
    escaped = False
    for index, character in enumerate(command):
        if character == "`":
            return True
        if escaped:
            escaped = False
            if character == "$":
                return True
            continue
        if character == "\\" and quote != "'":
            escaped = True
        elif character in {"'", '"'}:
            if quote == character:
                quote = None
            elif quote is None:
                quote = character
        elif character == "$":
            following = command[index + 1:index + 2]
            if quote is None or following not in {quote, "|", ")"}:
                return True
    return False


def find_read_only(arguments: list[str]) -> bool:
    # Allow the observed discovery predicates, rather than a denylist of actions.
    operands = {"-name", "-iname", "-path", "-ipath", "-type", "-maxdepth", "-mindepth"}
    flags = {"-H", "-L", "-P", "-print", "-print0", "-prune", "-o", "-or", "-a", "-and", "!", "-not", "(", ")"}
    index = 0
    depth = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "(":
            depth += 1
        elif argument == ")":
            depth -= 1
            if depth < 0:
                return False
        if argument in operands:
            index += 2
            if index > len(arguments):
                return False
        elif argument in flags or not argument.startswith("-"):
            index += 1
        else:
            return False
    return depth == 0


def sed_read_only(arguments: list[str]) -> bool:
    # Support print-only line selections, never arbitrary sed programs/options.
    if len(arguments) < 2 or arguments[0] != "-n":
        return False
    operands = arguments[2:] if arguments[1] == "-e" else arguments[1:]
    if not operands:
        return False
    address = r"[1-9][0-9]*"
    selection = rf"(?:{address}(?:\s*,\s*{address})?\s*)?p"
    return (
        re.fullmatch(rf"\s*{selection}(?:\s*;\s*{selection})*\s*;?\s*", operands[0]) is not None
        and all(not filename.startswith("-") for filename in operands[1:])
    )


def help_probe(tokens: list[str]) -> bool:
    # `uv run helper.py [subcommand] --help` reads a catalog helper's usage before calling it.
    return (tokens[:2] == ["uv", "run"] and len(tokens) in {4, 5} and tokens[2].endswith(".py")
            and tokens[-1] in {"--help", "-h"} and all(not token.startswith("-") for token in tokens[3:-1]))


def read_only(command: str) -> bool:
    # Reads let Codex load SKILL.md through its shell tool. The fixture uses
    # read-only host sandboxing as well; this is a test stop, not a security tool.
    if shell_expansion(command):
        return False
    commands: list[list[str]] = []
    for line in command.splitlines():
        # A read that falls back to another read is still a read.
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        commands.append([])
        try:
            parsed = list(lexer)
        except ValueError:
            return False
        for token in parsed:
            # A pipe between reads is still a read; each stage must pass on its own.
            if token in {"&&", ";", "||", "|"}:
                commands.append([])
            elif token in {"(", ")"} and commands[-1] and Path(commands[-1][0]).name == "find":
                commands[-1].append(token)
            elif token and all(character in "();<>|&" for character in token):
                return False
            else:
                commands[-1].append(token)
    for tokens in commands:
        if not tokens:
            continue
        name = Path(tokens[0]).name
        if name in {"cat", "pwd", "ls", "head", "tail", "echo", "printf", "shasum", "sha256sum", "sha1sum", "md5", "md5sum", "cksum", "stat", "wc", "file"}:
            continue
        if name == "sed" and sed_read_only(tokens[1:]):
            continue
        if name == "rg" and not any(token.startswith(("--pre", "--hostname-bin")) for token in tokens):
            continue
        if name == "grep":
            continue
        if name == "find" and find_read_only(tokens[1:]):
            continue
        if name == "git" and git_read_only(tokens[1:]):
            continue
        if help_probe(tokens):
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
