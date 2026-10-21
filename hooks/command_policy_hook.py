#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Apply skill command policies to a supported host's shell command before it runs.

Claude Code never shows a skill's frontmatter to the model and does not enforce
it, so `policy.command_policies` would be silent on that host. This PreToolUse
hook reads the same frontmatter at run time, through the repository's policy
simulator, and blocks a matching command with the policy's message and
preferred replacement. There is no generated copy of the policies.

Contract: JSON on stdin (`tool_name`, `tool_input.command`). By default exit 2
blocks with a stderr reason. With --json a denied command returns a structured
PreToolUse deny decision on stdout and exit 0, so the registered shell launcher
can fail open on startup errors without confusing uv's exit 2 with a denial. Anything the
hook cannot read or parse lets the command run: a broken entrypoint must not
stop every shell command on the host.

Policies match an argv prefix, so the hook first removes what an agent commonly
puts in front of a tool: leading assignments, `command`/`exec`/`time`/`nohup`,
`env` with its flags and assignments, `uv run` with its flags, a directory in
front of the tool name, `gh-with-env-token`, and `sh`/`bash`/`zsh -c '...'` wrappers. This is a
guardrail for habits, not a security boundary. Forms that need real option
parsing to unwrap, such as `xargs` and `sudo`, are deliberately left alone,
though a shell script behind one is still read.

Policies match commands, not data (#671, #1014). A heredoc body is data unless
a shell, ssh or script interpreter reads it; command substitutions in an unquoted-delimiter body,
in double-quoted arguments and in arithmetic still run and are checked, as are
`eval` arguments, a shell's here-string and an ssh remote command. A `shell_regex` policy
matches only where a token of a running command starts, so a quoted argument
such as a search pattern or a prose example does not match, while `sudo gh api`
or `xargs gh api` still does.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import json
import os
import re
import shlex
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlsplit

# Resolve through the install link so the catalog is found wherever it is linked from.
ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "skills"
SIMULATOR = CATALOG / "skill-creator" / "scripts" / "validate-command-policy-simulator.py"
CODE_HOME_SKILLS = "$CODE_HOME/skills/"
OPERATORS = re.compile(r"^[;&|()\n]+$")
REPOSITORY_SLUG = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/launchplane")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
TRANSPARENT = {"command", "exec", "time", "nohup"}
SHELL_KEYWORDS = {"{", "!", "if", "then", "elif", "else", "do", "while", "until"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
SCRIPT_INTERPRETERS = re.compile(r"^(?:osascript|python[\d.]*|node|swift|xdotool|xte|cliclick)$")
SCRIPT_WRAPPERS = {"sudo", "timeout", "xargs"}
STDIN_PROGRAM_PATHS = {"-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"}
GH_WRAPPER_FLAGS = {"--print-auth-account", "--require-automation-auth"}
ENV_VALUE_FLAGS = {"-u", "--unset", "-C", "--chdir"}
UV_VALUE_FLAGS = {"--python", "-p", "--extra", "--group", "--project", "--directory", "--with", "--with-editable", "--with-requirements", "--env-file"}
# A gate whose checkout-root option takes `.` as data, not the shell `.` source command.
DOT_ROOT_OPTIONS = {
    "--control-plane-root": ["launchplane", "service", "audit-config-authority"],
    "--repo-root": ["launchplane", "action-pins"],
}
POSTGRES_GATE = ["launchplane", "ci", "postgres-integration"]
POSTGRES_URL_VARIABLE = "LAUNCHPLANE_TEST_POSTGRES_URL="
# libpq reads these query parameters and variables as the server to connect to, overriding the URL host.
POSTGRES_HOST_OVERRIDES = {"host", "hostaddr", "service"}
# UV_ENV_FILE can set those variables after this check, as `uv run --env-file` can.
POSTGRES_HOST_VARIABLES = ("PGHOSTADDR", "PGSERVICE", "UV_ENV_FILE")


def load_simulator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("command_policy_simulator", SIMULATOR)
    if spec is None or spec.loader is None:
        raise ImportError(str(SIMULATOR))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def shell_lexer(text: str, *, posix: bool = True, punctuation_chars: str = ";&|<>()\n") -> shlex.shlex:
    chars, quote, escaped, boundary, comment = [], None, False, True, False
    for char in text:
        if comment:
            if char != "\n":
                continue
            comment = False
        if escaped:
            chars.append(char)
            escaped, boundary = False, False
            continue
        if char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and boundary:
            comment = True
            continue
        chars.append(char)
        boundary = quote is None and (char.isspace() or char in ";&|()")
    lexer = shlex.shlex("".join(chars), posix=posix, punctuation_chars=punctuation_chars)
    lexer.commenters = ""
    return lexer


def shell_tokens(shell: str) -> list[str]:
    lexer = shell_lexer(shell)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    return list(lexer)


def shell_commands(shell: str) -> list[list[str]]:
    """Split with shell operators and comments using one shared lexer."""
    commands: list[list[str]] = [[]]
    for token in shell_tokens(shell):
        if OPERATORS.match(token):
            commands.append([])
        else:
            commands[-1].append(token)
    return [argv for argv in commands if argv]


class ShellScript:
    """Separate what a shell line runs from the heredoc data it carries.

    `text` is the line with every heredoc body removed. `bodies` holds each
    body, whether the shell expands it (an unquoted delimiter), its header, delimiter and redirection occurrence.
    `substitutions` holds the source of each `$(...)` and backtick command
    substitution outside single quotes, including inside double quotes, where
    the lexer keeps it as one data token although it runs. With `expanding`,
    the text is an expanding heredoc body: quotes are literal and only
    substitutions are read. This follows ordinary agent commands; it is not a
    full shell parser.
    """

    def __init__(self, shell: str, expanding: bool = False) -> None:
        self.shell = shell
        self.text_parts: list[str] = []
        self.bodies: list[tuple[str, bool, str, str, int]] = []
        self.substitutions: list[str] = []
        self.process_substitutions: dict[int, str] = {}
        self.process_starts: dict[int, str] = {}
        self.command_starts = [0]
        self.substitution_offsets: dict[int, int] = {}
        self.substitution_spans: list[tuple[int, int]] = []
        self.pending: list[tuple[str, bool, bool, int | None]] = []
        self.body_owners: dict[int, int] = {}
        self.closed_substitutions: set[int] = set()
        self.incomplete = False
        self.background = False
        # Open contexts, innermost last: "(" for a subshell or `$(`, "((" for
        # `$((` arithmetic, '"' for a double quote, "`" for a backtick, "body"
        # for an expanding heredoc body; each with the index its substitution
        # source starts at, if any.
        self.stack: list[tuple[str, int | None]] = [("body", None)] if expanding else []
        self.index = 0
        self.scan()

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    @property
    def command_text(self) -> str:
        text, cursor, parts = self.text, 0, []
        for start, end in self.substitution_spans:
            parts.extend((text[cursor:start], "${_native_substitution}"))
            cursor = end
        return "".join([*parts, text[cursor:]])

    def open_scope(self, kind: str, start: int | None, end: int) -> None:
        self.stack.append((kind, start))
        self.command_starts.append(end)
        if start is not None:
            self.substitution_offsets[start] = len(self.text)
        self.emit(end)

    def emit(self, end: int) -> None:
        self.text_parts.append(self.shell[self.index:end])
        self.index = end

    def scan(self) -> None:
        shell = self.shell
        while self.index < len(shell):
            character = shell[self.index]
            context = self.stack[-1][0] if self.stack else ""
            if character == "\\":
                self.emit(self.index + 2)
            elif shell.startswith("$((", self.index):
                self.stack.append(("((", None))  # Arithmetic: `<<` is a shift; `$(...)` still runs.
                self.emit(self.index + 3)
            elif context == "((" and shell.startswith("))", self.index):
                self.stack.pop()
                self.emit(self.index + 2)
            elif shell.startswith("$(", self.index):
                self.open_scope("(", self.index + 2, self.index + 2)
            elif context not in {'"', 'body'} and shell.startswith(("<(", ">("), self.index):
                self.process_starts[self.index + 2] = shell[self.command_starts[-1]:self.index]
                self.open_scope("(", self.index + 2, self.index + 2)
            elif character == "`":
                self.backtick()
            elif context in {'"', "body"}:
                if character == '"' and context == '"':
                    self.stack.pop()
                self.emit(self.index + 1)
            elif character == "'":
                closing = shell.find("'", self.index + 1)
                self.emit(len(shell) if closing < 0 else closing + 1)
            elif character == '"':
                self.stack.append(('"', None))
                self.emit(self.index + 1)
            elif character == "#" and (self.index == 0 or shell[self.index - 1] in " \t\r\n;&|()"):
                closing = shell.find("\n", self.index)
                self.emit(len(shell) if closing < 0 else closing)
            elif character == '&' and context != '((':
                if shell.startswith(('&&', '&>'), self.index):
                    separator = shell.startswith('&&', self.index)
                    self.emit(self.index + 2)
                    if separator:
                        self.command_starts[-1] = self.index
                else:
                    self.background |= self.index == 0 or shell[self.index - 1] not in '><|;'
                    self.emit(self.index + 1)
                    self.command_starts[-1] = self.index
            elif shell.startswith("((", self.index) and (self.index == 0 or shell[self.index - 1] in " \t\r\n;&|"):
                self.stack.append(("((", None))  # An arithmetic command, as in `$((`.
                self.emit(self.index + 2)
            elif character == "(":
                self.open_scope("(", None, self.index + 1)
            elif character == ")" and context == "(":
                self.close()
            elif shell.startswith("<<<", self.index):
                self.emit(self.index + 3)  # A here-string: its word is an ordinary argument.
            elif context != "((" and shell.startswith("<<", self.index):
                self.heredoc()
            elif character == "\n" and self.pending:
                self.emit(self.index + 1)
                self.read_bodies()
                self.command_starts[-1] = self.index
            elif context != "((" and character in ";|\n":
                length = 2 if shell.startswith(("||", "|&"), self.index) else 1
                self.emit(self.index + length)
                self.command_starts[-1] = self.index
            else:
                self.emit(self.index + 1)

    def close(self) -> None:
        _, start = self.stack.pop()
        self.command_starts.pop()
        # Record only outermost substitutions; parsing one finds those inside it.
        if start is not None and all(enclosing is None for _, enclosing in self.stack):
            if start in self.process_starts:
                self.process_substitutions[len(self.substitutions)] = self.process_starts[start]
            self.substitutions.append(self.shell[start:self.index])
            self.substitution_spans.append((self.substitution_offsets[start], len(self.text) + 1))
            self.closed_substitutions.add(start)
        self.emit(self.index + 1)

    def backtick(self) -> None:
        if self.stack and self.stack[-1][0] == "`":
            self.close()
        else:
            self.open_scope("`", self.index + 1, self.index + 1)

    def heredoc(self) -> None:
        shell = self.shell
        cursor = self.index + 2
        strip_tabs = shell.startswith("-", cursor)
        cursor += strip_tabs
        while cursor < len(shell) and shell[cursor] in " \t":
            cursor += 1
        word: list[str] = []
        quoted = False
        while cursor < len(shell) and shell[cursor] not in " \t\r\n;&|()<>":
            character = shell[cursor]
            if character in "'\"":
                closing = shell.find(character, cursor + 1)
                closing = len(shell) if closing < 0 else closing
                word.append(shell[cursor + 1:closing])
                quoted, cursor = True, closing + 1
            elif character == "\\":
                word.append(shell[cursor + 1:cursor + 2])
                quoted, cursor = True, cursor + 2
            else:
                word.append(character)
                cursor += 1
        if word:
            owner = next((start for _, start in self.stack if start is not None), None)
            self.pending.append(("".join(word), strip_tabs, not quoted, owner))
        self.emit(min(cursor, len(shell)))

    def read_bodies(self) -> None:
        """Consume the body lines of each heredoc opened on the line just ended."""
        shell = self.shell
        cursor = self.index
        header = self.text
        for position, (delimiter, strip_tabs, expands, owner) in enumerate(self.pending):
            body: list[str] = []
            while cursor < len(shell):
                end = shell.find("\n", cursor)
                end = len(shell) if end < 0 else end
                line = shell[cursor:end]
                cursor = end + 1
                if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                    break
                body.append(line)
            else:
                self.incomplete = True
            if owner is not None:
                self.body_owners[len(self.bodies)] = owner
            occurrence = -sum(entry[0] == delimiter for entry in self.pending[position:])
            self.bodies.append(("\n".join(body), expands, header, delimiter, occurrence))
        self.pending = []
        self.index = min(cursor, len(shell))


def without_redirections(argv: list[str]) -> list[str]:
    words, cursor = [], 0
    while cursor < len(argv):
        token = argv[cursor]
        if token.isdecimal() and cursor + 1 < len(argv) and argv[cursor + 1].startswith(("<", ">")):
            cursor += 1
        elif re.fullmatch(r"[<>][<>&|]*|&>>?", token):
            cursor += 3 if token == "<<" and argv[cursor + 1:cursor + 2] == ["-"] else 2
        else:
            words.append(token)
            cursor += 1
    return words


class ShellInvocation(NamedTuple):
    body: str | None
    mode: str
    script: str | None
    index: int
    options_closed: bool = False

    @property
    def stdin(self) -> bool:
        return self.mode in {"implicit", "stdin"}


class XargsInput(NamedTuple):
    replacement: str | None
    stdin_uncertain: bool
    unknown: bool
    template: tuple[str, ...]
    composed: bool = False


class Command(NamedTuple):
    argv: list[str]
    roles: set[str]
    xargs: tuple[XargsInput, ...]


def shell_invocation(argv: list[str]) -> ShellInvocation:
    argv = without_redirections(argv)
    if not argv or Path(argv[0]).name not in SHELLS:
        return ShellInvocation(None, "other", None, 0)
    cursor, command, stdin, options_closed = 1, False, False, False
    while cursor < len(argv):
        option = argv[cursor]
        if option == "--":
            options_closed = True
            cursor += 1
            break
        if not option.startswith(("-", "+")) or option == "-":
            break
        if option.startswith("--") and option not in {
            "--rcfile", "--init-file", "--noprofile", "--norc", "--posix", "--restricted",
            "--verbose", "--version", "--help", "--debugger", "--login", "--noediting",
            "--dump-po-strings", "--dump-strings",
        }:
            return ShellInvocation(None, "other", None, cursor)
        command |= option.startswith("-") and not option.startswith("--") and "c" in option[1:]
        stdin |= option.startswith("-") and not option.startswith("--") and "s" in option[1:]
        cursor += 1
        if option in {"--rcfile", "--init-file"} or not option.startswith("--") and option.endswith(("o", "O")):
            cursor += 1
    if command:
        return ShellInvocation(argv[cursor] if cursor < len(argv) else None, "inline", None, cursor, options_closed)
    if stdin or cursor < len(argv) and argv[cursor] in STDIN_PROGRAM_PATHS:
        return ShellInvocation(None, "stdin", None, cursor, options_closed)
    if cursor >= len(argv):
        return ShellInvocation(None, "implicit", None, cursor, options_closed)
    return ShellInvocation(None, "script", argv[cursor], cursor, options_closed)


def shell_c_body(argv: list[str]) -> str | None:
    return shell_invocation(argv).body


def xargs_command(argv: list[str]) -> tuple[list[str], XargsInput]:
    required = {"I", "J", "R", "S", "n", "P", "L", "s", "E", "d", "a"}
    long_values = {"--max-args", "--max-procs", "--max-chars", "--process-slot-var", "--delimiter", "--arg-file"}
    long_flags = {"--null", "--open-tty", "--interactive", "--no-run-if-empty", "--show-limits", "--verbose", "--exit", "--help", "--version"}
    cursor, replacement, uncertain, unknown = 0, None, False, False
    while cursor < len(argv) and argv[cursor].startswith("-") and argv[cursor] != "-":
        option = argv[cursor]
        cursor += 1
        if option == "--":
            break
        if option.startswith("--"):
            flag, equals, value = option.partition("=")
            if flag == "--replace":
                replacement = value if equals else "{}"
            elif flag in {"--eof", "--max-lines"}:
                pass
            elif flag in long_values:
                if not equals:
                    unknown |= cursor >= len(argv)
                    cursor += 1
                uncertain |= flag == "--arg-file"
            elif flag not in long_flags or equals:
                unknown = True
            uncertain |= flag == "--open-tty"
            continue
        for index, flag in enumerate(option[1:], 2):
            rest = option[index:]
            if flag in required:
                value = rest or (argv[cursor] if cursor < len(argv) else "")
                unknown |= not value
                cursor += not rest
                if flag in {"I", "J"}:
                    replacement = value
                uncertain |= flag == "a"
                break
            if flag in {"i", "e", "l"}:
                if flag == "i":
                    replacement = rest or "{}"
                break
            unknown |= flag not in "0oprtx"
            uncertain |= flag == "o"
    command = argv[cursor:]
    return command or ["echo"], XargsInput(replacement, uncertain, unknown, tuple(command))


def normalize_command(argv: list[str]) -> Command:
    argv, roles, frames = list(argv), set(), []
    def finish(command: list[str]) -> Command:
        return Command(command, roles, tuple(frame._replace(
            composed=frame.composed or bool(frame.template) and len(frame.template) != len(command),
            unknown=frame.unknown or bool(frame.replacement and any(frame.replacement in word
                for word in frame.template[:max(0, len(frame.template) - len(command))]))
        ) for frame in frames))
    value_flags = {
        "nice": {"-n", "--adjustment"},
        "ionice": {"-c", "-n", "-p", "-P", "-u", "--class", "--classdata", "--pid"},
        "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
        "caffeinate": {"-t", "-w"},
    }
    for _ in range(32):
        if not argv:
            return finish([])
        head = argv[0] if argv[0] == "." else Path(argv[0]).name
        tail = argv[1:]
        if ASSIGNMENT.match(argv[0]) or head in TRANSPARENT | SHELL_KEYWORDS:
            argv = tail
        elif head == "env":
            argv = drop_flags(tail, ENV_VALUE_FLAGS)
        elif head == "uv" and tail[:1] == ["run"]:
            argv = drop_flags(tail[1:], UV_VALUE_FLAGS)
        elif head == "sudo":
            roles.add(head)
            cursor, shell = 0, False
            short_values = set("ughpCTDRUrt")
            long_values = {"--user", "--group", "--host", "--prompt", "--close-from", "--command-timeout", "--chdir", "--chroot", "--other-user", "--role", "--type"}
            while cursor < len(tail) and tail[cursor].startswith("-"):
                option = tail[cursor]
                cursor += 1
                if option == "--":
                    break
                if option.startswith("--"):
                    shell |= option in {"--login", "--shell"}
                    if option == "--background":
                        roles.add("detached")
                    cursor += option in long_values
                else:
                    for position, flag in enumerate(option[1:], 1):
                        if flag in short_values:
                            cursor += position == len(option) - 1
                            break
                        shell |= flag in "is"
                        if flag == "b":
                            roles.add("detached")
            argv = tail[cursor:]
            if not without_redirections(argv):
                return finish(["sh", *argv] if shell else [head, *tail])
        elif head == "xargs":
            roles.add(head)
            argv, placement = xargs_command(tail)
            frames.append(placement)
        elif head in {"timeout", "gtimeout"}:
            roles.add(head)
            argv = drop_flags(tail, {"-k", "--kill-after", "-s", "--signal"})[1:]
        elif head in value_flags:
            roles.add(head)
            argv = drop_flags(tail, value_flags[head])
        elif head == "chrt":
            roles.add(head)
            argv = drop_flags(tail, {"-T", "-P", "-D", "--sched-runtime", "--sched-period", "--sched-deadline"})[1:]
        elif head == "docker" and (tail[:1] == ["exec"] or tail[:2] == ["compose", "exec"]):
            roles.add(head)
            options = tail[2:] if tail[:1] == ["compose"] else tail[1:]
            cursor = 0
            while cursor < len(options) and options[cursor].startswith("-"):
                option = options[cursor]
                cursor += 1
                if option == "--":
                    break
                if option.startswith("--"):
                    if option == "--detach" or option.startswith("--detach=") and option != "--detach=false":
                        roles.add("detached")
                    cursor += option in {"--user", "--workdir", "--env", "--env-file", "--detach-keys", "--index"}
                else:
                    for position, flag in enumerate(option[1:], 1):
                        if flag in "euw":
                            cursor += position == len(option) - 1
                            break
                        if flag == "d":
                            roles.add("detached")
            argv = options[cursor + 1:]
        elif head == "kubectl" and tail[:1] == ["exec"] and "--" in tail:
            roles.add(head)
            argv = tail[tail.index("--") + 1:]
        elif head == "gh-with-env-token":
            while tail and tail[0] in GH_WRAPPER_FLAGS:
                tail = tail[1:]
            return finish([head, *tail])
        else:
            return finish([head, *tail])
    raise ValueError("foreground wrapper nesting exceeds coverage")


def shell_candidates(argv: list[str], *, include_nonliteral: bool = False) -> list[list[str]]:
    if not argv:
        return []
    if argv[0] in SHELLS:
        return [argv]
    if include_nonliteral and any(char in argv[0] for char in "$`"):
        return [["sh", *argv[1:]]]
    return [argv[index:] for index, word in enumerate(argv[1:], 1) if Path(word).name in SHELLS]


def ssh_command(argv: list[str]) -> tuple[list[str], bool]:
    argv = without_redirections(argv)
    cursor, background = 1, False
    value_flags = set("BbcDEeFIiJLlmOoPpQRSWw")
    while cursor < len(argv) and argv[cursor].startswith("-"):
        option = argv[cursor]
        cursor += 1
        if option == "--":
            break
        for position, flag in enumerate(option[1:], 1):
            if flag in value_flags:
                cursor += position == len(option) - 1
                break
            background |= flag == "f"
    return argv[cursor + 1:], background


def command_input(argv: list[str], *, completion: bool = False, depth: int = 0) -> tuple[list[str], str]:
    if depth >= 16:
        raise ValueError("shell nesting exceeds coverage")
    normalized = normalize_command(argv)
    argv, frames = normalized.argv, normalized.xargs
    if not argv:
        return [], "opaque" if completion and frames else "unknown"
    name, tail = argv[0], argv[1:]
    programs, kind = [], "data"
    if name in SHELLS:
        invocation = shell_invocation(argv)
        if invocation.body is not None:
            programs.append(invocation.body)
        if invocation.stdin:
            kind = "shell"
    elif name in {"eval", "ssh"}:
        if name == "ssh":
            tail, _ = ssh_command(argv)
            try:
                kind = stdin_kind(shell_commands(" ".join(tail)), set(), completion=completion, depth=depth + 1) if tail else "shell"
            except ValueError:
                kind = "unknown"
        redirection = next((index for index, word in enumerate(tail) if word.startswith(("<", ">"))), len(tail))
        if tail[:redirection]:
            programs.append(" ".join(tail[:redirection]))
    elif name in {"source", "."}:
        words = without_redirections(tail)
        if not words or words[0] in STDIN_PROGRAM_PATHS:
            kind = "shell"
    elif name in {"su", "runuser", "script", "flock"}:
        for index, flag in enumerate(tail[:-1]):
            if flag == "--command" or flag.startswith("-") and "c" in flag[1:]:
                programs.append(tail[index + 1])
                break
        if name in {"su", "runuser"} and not programs:
            kind = "shell"
    if completion and ("$" in name or "`" in name):
        invocation = shell_invocation(shell_candidates(argv, include_nonliteral=True)[0])
        if invocation.body is not None:
            programs.append(invocation.body)
        if invocation.stdin:
            kind = "unknown"
    elif name not in SHELLS and kind == "data":
        for candidate in shell_candidates(argv, include_nonliteral=completion):
            invocation = shell_invocation(candidate)
            if invocation.body is not None:
                programs.append(invocation.body)
            if invocation.stdin:
                kind = "possible"
            if completion and any(Path(word).name == "xargs" for word in argv[:len(argv) - len(candidate)]):
                kind = "opaque"
    if frames:
        unproved = len(frames) != 1 or any(frame.unknown for frame in frames)
        unproved |= name not in SHELLS and bool(shell_candidates(argv, include_nonliteral=True))
        unproved |= any(char in name for char in "$`")
        unproved |= any(frame.replacement and frame.replacement in name for frame in frames)
        if name in SHELLS:
            invocation = shell_invocation(argv)
            words = without_redirections(argv)
            unproved |= invocation.mode == "inline" and invocation.body is None
            unproved |= any(frame.composed for frame in frames)
            unproved |= invocation.mode == "implicit" and not invocation.options_closed
            for frame in frames:
                if frame.replacement:
                    unproved |= any(frame.replacement in word for word in words[:invocation.index])
                    unproved |= invocation.body is not None and frame.replacement in invocation.body
                    unproved |= (invocation.script is not None and invocation.script.startswith(frame.replacement)
                                 and not invocation.options_closed)
                unproved |= invocation.mode == "stdin" or frame.stdin_uncertain and invocation.mode == "implicit"
            kind = "opaque" if completion and unproved else "data"
        elif unproved or name in {"eval", "ssh", "source", ".", "su", "runuser", "script", "flock"} or kind != "data":
            kind = "opaque" if completion else "data"
    if input_is_source(kind, completion=completion) and "<<<" in argv[:-1]:
        payload = argv[argv.index("<<<") + 1]
        if kind != "possible" or not any(char in payload for char in "$`"):
            programs.append(payload)
    return programs, kind


def input_is_source(kind: str, *, completion: bool) -> bool:
    return kind == "shell" or completion and kind in {"possible", "unknown"}


def reads_program_from_stdin(argv: list[str]) -> bool:
    arguments = without_redirections(argv)[1:]
    tool = argv[0]
    if tool == "xdotool":
        return len(arguments) == 1 and arguments[0] in STDIN_PROGRAM_PATHS
    if tool == "cliclick":
        return len(arguments) == 2 and arguments[0] == "-f" and arguments[1] in STDIN_PROGRAM_PATHS
    if tool == "xte":
        return not arguments
    value_flags = {"-W", "-X", "--check-hash-based-pycs"} if tool.startswith("python") else {
        "-l", "-s", "-r", "--require", "--input-type", "--loader", "--import", "-I", "-L", "-sdk", "-target",
    }
    cursor = 0
    while cursor < len(arguments):
        token = arguments[cursor]
        if token in STDIN_PROGRAM_PATHS:
            return True
        if token == "--":
            return cursor + 1 == len(arguments) or arguments[cursor + 1] in STDIN_PROGRAM_PATHS
        if token in {"-c", "-e", "-m", "-p", "--eval", "--print"} or token.startswith(("--eval=", "--print=")):
            return False
        if tool.startswith("python") and re.match(r"^-[bBdEiIOqRsSuvVx]*[cm]", token):
            return False
        if tool in {"node", "osascript"} and token.startswith(("-e", "-p")):
            return False
        if token in value_flags:
            cursor += 2
        elif token.startswith("-"):
            cursor += 1
        else:
            return False  # The named script, not stdin, supplies the program.
    return True


def heredoc_commands(header: str, delimiter: str, occurrence: int) -> list[list[str]]:
    tokens = shell_tokens(header)
    tokens = [token.rstrip("\n") if token.rstrip("\n") in {"|", "|&"} else token for token in tokens]
    tokens = [token for index, token in enumerate(tokens)
              if token != "\n" or index == 0 or tokens[index - 1] not in {"|", "|&"}]
    markers = [
        index for index, token in enumerate(tokens[:-1]) if token == "<<"
        and (tokens[index + 1] in {delimiter, "-" + delimiter}
             or tokens[index + 1:index + 3] == ["-", delimiter])
    ]
    if len(markers) < -occurrence:
        return []
    marker = markers[occurrence]
    start = max((index + 1 for index in range(marker) if OPERATORS.match(tokens[index])), default=0)
    if marker and tokens[marker - 1] == ")":
        return [[")"]]
    end = next((index for index in range(marker + 1, len(tokens))
                if OPERATORS.match(tokens[index]) and tokens[index] not in {"|", "|&"}), len(tokens))
    commands: list[list[str]] = [[]]
    for token in tokens[start:end]:
        if token in {"|", "|&"}:
            commands.append([])
        else:
            commands[-1].append(token)
    return commands


def script_readers(commands: list[list[str]]) -> list[list[str]]:
    """Normalize known interpreter wrappers and check which command consumes a stdin program."""
    readers: list[list[str]] = []
    for command in commands:
        for argv in unwrap(command, expand=False):
            for index, token in enumerate(argv):
                if (index != 0 and argv[0] not in SCRIPT_WRAPPERS) or not SCRIPT_INTERPRETERS.fullmatch(Path(token).name):
                    continue
                reader = [Path(token).name, *argv[index + 1:]]
                if reads_program_from_stdin(reader):
                    readers.append(reader)
    return readers


def pipelines(shell: str) -> list[list[list[str]]]:
    stages: list[list[list[str]]] = [[[]]]
    for token in shell_tokens(shell):
        token = token.rstrip("\n") if token.rstrip("\n") in {"|", "|&"} else token
        if token in {"|", "|&"}:
            stages[-1].append([])
        elif OPERATORS.match(token):
            stages.append([[]])
        else:
            stages[-1][-1].append(token)
    return [pipeline for pipeline in stages if any(pipeline)]


def literal_payload(command: list[str]) -> str | None:
    argv = normalize_command(command).argv
    if not argv or any("$" in word or "`" in word for word in argv):
        return None
    if argv[0] == "echo":
        words = argv[1:]
        if words[:1] == ["-n"]:
            words = words[1:]
        if any("\\" in word for word in words) or words and words[0] in {"-e", "-E"}:
            return None
        return " ".join(words)
    if argv[0] != "printf" or len(argv) < 2 or argv[1] == "-v":
        return None
    fmt, words = argv[1], argv[2:]
    if re.search(r"\\(?![nt\\])", fmt):
        return None
    fmt = re.sub(r"\\([nt\\])", lambda match: {"n": "\n", "t": "\t", "\\": "\\"}[match[1]], fmt)
    if fmt in {"%s", "%s\n", "%s\t"}:
        return "".join(word + fmt[2:] for word in words)
    return fmt if not words and "%" not in fmt else None


def pipeline_payload(stages: list[list[str]]) -> str | None:
    for stage in stages[1:]:
        argv = normalize_command(stage).argv
        if argv not in (["cat"], ["cat", "-"]) and not (argv and argv[0] == "tee" and not {"--help", "--version"} & set(argv)):
            return None
    return literal_payload(stages[0]) if stages else None


def literal_pipeline_programs(shell: str) -> list[list[str]]:
    programs = []
    for stages in pipelines(shell):
        if len(stages) > 1 and (payload := pipeline_payload(stages[:-1])) is not None:
            programs.extend([*reader, payload] for reader in script_readers(stages[-1:]))
    return programs


def written_files(command: list[str]) -> set[str]:
    paths = {str(Path(command[index + 1])) for index, token in enumerate(command[:-1])
             if token in {">", ">>", ">|", "&>"}}
    argv = normalize_command(command).argv
    if argv and argv[0] == "tee":
        operands = without_redirections(argv)[1:]
        options = True
        for word in operands:
            if word == "--" and options:
                options = False
            elif word != "-" and (not options or not word.startswith("-")):
                paths.add(str(Path(word)))
    return paths


def program_references(command: list[str], written: set[str]) -> set[str]:
    normalized = normalize_command(command)
    argv = normalized.argv
    if not argv:
        return set()
    references = {argv[0]}
    for candidate in shell_candidates(argv, include_nonliteral=True):
        invocation = shell_invocation(candidate)
        if invocation.script is not None:
            references.add(invocation.script)
            prefix = argv[:len(argv) - len(candidate)]
            proven_slot = argv[0] in SHELLS or argv[0] == "find" and bool({"-exec", "-execdir"} & set(prefix))
            if proven_slot and any(char in Path(invocation.script).name for char in "$`*?[{"):
                references.update(written)
        if normalized.xargs and (invocation.mode == "implicit" or invocation.script is not None
                and any(frame.replacement and frame.replacement in invocation.script for frame in normalized.xargs)):
            references.update(written)
    if argv[0] in {"source", "."}:
        words = without_redirections(argv)[1:]
        sources = words[:1] if words[:1] != ["--"] else words[1:2]
        references.update(sources)
        if any(char in Path(source).name for source in sources for char in "$`*?[{"):
            references.update(written)
    if command_input(command, completion=True)[1] in {"shell", "possible", "unknown"}:
        references.update(argv[index + 1] for index, word in enumerate(argv[:-1]) if word == "<")
    return {Path(path).name for path in references}


def stdin_kind(commands: list[list[str]], executed_files: set[str], *, completion: bool = False, depth: int = 0) -> str:
    if not commands or any(not argv for argv in commands):
        return "unknown"
    result = "data"
    for command in commands:
        argv = normalize_command(command).argv
        _, kind = command_input(command, completion=completion, depth=depth)
        if not argv:
            return "unknown"
        if argv[0] in {"done", "fi", "esac", "}", ")"} or kind == "shell":
            return "shell"
        if kind in {"possible", "unknown"}:
            result = "unknown"
        if {Path(path).name for path in written_files(command)} & executed_files:
            return "shell"
    return result


def execution_sources(shell: str, *, completion: bool = False, depth: int = 0,
                      executed_files: set[str] | None = None, inline: bool = False,
                      force_input: str = "data") -> Iterator[tuple[ShellScript | ValueError, list[list[str]]]]:
    if depth >= 16:
        yield ValueError("shell nesting exceeds coverage"), []
        return
    if executed_files is None:
        executed_files = set()
    if depth == 0 and "<<" in shell:
        nodes = list(execution_sources(shell, completion=True, depth=depth + 1, executed_files=set()))
        commands = [argv for node, _ in nodes if isinstance(node, ShellScript) for argv in shell_commands(node.command_text)]
        written = set().union(*(written_files(argv) for argv in commands))
        executed = set().union(*(program_references(command, written) for command in commands))
        executed_files = {Path(path).name for path in written} & executed
    script = ShellScript(shell)
    opaque = ["unfinished shell syntax"] if script.stack or script.pending or script.incomplete else []
    try:
        commands = shell_commands(script.command_text)
    except ValueError as error:
        yield error, []
        return
    inputs = [normalize_command(argv).argv for argv in commands]
    nested = []
    for index, source in enumerate(script.substitutions):
        force = "data"
        if index in script.process_substitutions:
            try:
                owner_commands = shell_commands(script.process_substitutions[index])
                if owner_commands:
                    owner_kind = command_input(owner_commands[0], completion=completion)[1]
                    if input_is_source(owner_kind, completion=completion):
                        force = owner_kind
            except ValueError:
                opaque.append("unreadable process-substitution reader")
        nested.append((source, False, force))
    for raw, argv in zip(commands, inputs):
        if not argv:
            continue
        if completion and inline and ("$" in argv[0] or "`" in argv[0]):
            opaque.append("nonliteral inline shell program")
        sources, kind = command_input(raw, completion=completion)
        if completion and kind == "opaque":
            opaque.append("unproved xargs program mapping")
        nested.extend((source, True, "data") for source in sources)
    for stages in pipelines(script.command_text):
        if len(stages) < 2:
            continue
        kind = command_input(stages[-1], completion=completion)[1]
        if not input_is_source(kind, completion=completion):
            continue
        payload = pipeline_payload(stages[:-1])
        if payload is not None:
            nested.append((payload, True, "data"))
        elif completion and kind in {"shell", "unknown"}:
            opaque.append("unresolved shell pipeline input")
    if input_is_source(force_input, completion=completion):
        payload = pipeline_payload(commands)
        if payload is not None:
            nested.append((payload, True, "data"))
        elif completion and force_input in {"shell", "unknown"} and not script.bodies:
            opaque.append("unresolved shell process-substitution input")
    readers = []
    for index, (body, expands, header, delimiter, occurrence) in enumerate(script.bodies):
        owner = script.body_owners.get(index)
        if owner is not None:
            if owner not in script.closed_substitutions:
                opaque.append("unclosed heredoc substitution")
            continue
        try:
            if header.rstrip().endswith(("|", "|&")):
                header += script.text[len(header):]
            consumers = heredoc_commands(header, delimiter, occurrence)
            readers.extend([*argv, body] for argv in script_readers(consumers))
            kind = force_input if input_is_source(force_input, completion=completion) else stdin_kind(consumers, executed_files, completion=completion)
            if input_is_source(kind, completion=completion):
                nested.append((body, False, "data"))
            elif expands:
                expanded = ShellScript(body, expanding=True)
                if expanded.stack != [("body", None)] or expanded.pending or expanded.incomplete:
                    opaque.append("unfinished heredoc expansion")
                nested.extend((source, False, "data") for source in expanded.substitutions)
        except ValueError as error:
            opaque.append(str(error))
    yield script, readers
    if opaque:
        yield ValueError("; ".join(opaque)), []
    for source, nested_inline, nested_input in nested:
        yield from execution_sources(source, completion=completion, depth=depth + 1,
                                     executed_files=executed_files, inline=nested_inline, force_input=nested_input)


def detached_command(argv: list[str]) -> bool:
    command = normalize_command(argv)
    return (command.argv[:1] == ["coproc"] or "detached" in command.roles
            or command.argv[:1] == ["ssh"] and ssh_command(command.argv)[1])


def simple_commands(shell: str) -> list[list[str]]:
    unwrapped = []
    for script, readers in execution_sources(shell):
        if isinstance(script, ValueError):
            continue
        for argv in shell_commands(script.command_text):
            unwrapped.extend(unwrap(argv, expand=False))
        unwrapped.extend(readers)
        unwrapped.extend(literal_pipeline_programs(script.command_text))
    return unwrapped


def nested_commands(sources: list[str]) -> list[list[str]]:
    """Each nested script is shorter than the text holding it, so recursion ends."""
    commands: list[list[str]] = []
    for source in sources:
        try:
            commands.extend(simple_commands(source))
        except (ValueError, RecursionError):
            continue  # An unreadable part must not let the rest of the line through.
    return commands


def drop_flags(argv: list[str], value_flags: set[str] | None = None) -> list[str]:
    while argv and argv[0].startswith("-"):
        flag = argv[0]
        argv = argv[2:] if value_flags and flag in value_flags else argv[1:]
        if flag == "--":
            break
    return argv


def unwrap(argv: list[str], *, expand: bool = True) -> list[list[str]]:
    normalized = normalize_command(argv)
    command, roles = normalized.argv, normalized.roles
    if not command:
        return []
    commands = [list(argv) if roles & {"sudo", "xargs"} else command]
    if expand:
        commands += nested_commands(command_input(command)[0])
    return commands


def runnable(token: str, skill: str) -> str:
    """Point a policy's script path at this catalog.

    Policies name scripts the way Codex resolves them: under `$CODE_HOME/skills`,
    or relative to the skill or the catalog. Claude Code sets no `CODE_HOME` and
    runs from the user's project, so neither form runs as written.
    """
    if token.startswith(CODE_HOME_SKILLS):
        candidates = [CATALOG / token.removeprefix(CODE_HOME_SKILLS)]
    elif "/" in token and not token.startswith(("/", "$", "<", "-")):
        candidates = [CATALOG / skill / token, CATALOG / token]
    else:
        return token
    return next((str(path.resolve()) for path in candidates if path.is_file()), token)


def describe(policy: dict[str, Any], skill: str, argv: list[str]) -> str:
    lines = [
        f"Blocked by the `{skill}` skill's command policy `{policy['id']}`.",
        f"Matched command: {shlex.join(argv)}",
        f"Load the `{skill}` skill before continuing ({CATALOG / skill / 'SKILL.md'}).",
    ]
    if policy.get("message"):
        lines.append(str(policy["message"]))
    for preferred in policy.get("preferred") or []:
        if preferred.get("kind") == "skill":
            lines.append(f"Use the `{preferred.get('name')}` skill: {preferred.get('purpose', '')}".rstrip(": "))
        elif preferred.get("example_argv"):
            argv = [runnable(str(token), skill) for token in preferred["example_argv"]]
            lines.append(f"Run instead: {shlex.join(argv)}")
    lines.append(f"Any remaining relative script path is inside the `{skill}` skill's base directory.")
    return "\n".join(lines)


def loopback_database(url: str) -> bool:
    """A PostgreSQL URL whose only server is this machine, so the gate cannot reach a shared database."""
    # SQLAlchemy reads a `#` as query text, where urlsplit would hide it as a fragment.
    if "#" in url or any(os.environ.get(name) for name in POSTGRES_HOST_VARIABLES):
        return False
    try:
        parts = urlsplit(url)
        if not parts.scheme.startswith("postgres") or POSTGRES_HOST_OVERRIDES & {key.lower() for key in parse_qs(parts.query)}:
            return False
        return parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname or "").is_loopback
    except ValueError:
        return False


def names_loopback_database(argv: list[str]) -> bool:
    """The PostgreSQL gate runs only against a database the command itself names; an inherited one is unseen."""
    urls = [token.removeprefix(POSTGRES_URL_VARIABLE) for token in argv if token.startswith(POSTGRES_URL_VARIABLE)]
    for index, token in enumerate(argv):
        if token == "--database-url":
            urls.append(argv[index + 1] if index + 1 < len(argv) else "")
        elif token.startswith("--database-url="):
            urls.append(token.removeprefix("--database-url="))
    return bool(urls) and all(loopback_database(url) for url in urls)


def exception_cwd(shell: str, cwd: Path) -> Path | None:
    """Accept the tool cwd or one literal leading cd joined by success-only &&.

    Other directory/project switches and executable overrides retain the block.
    The simulator independently verifies the resulting checkout's Git identity.
    """
    shell = shell.strip()
    if "$(" in shell or "`" in shell or "<<" in shell:
        return None
    prefix = re.fullmatch(
        r"\s*cd\s+(?:--\s+)?(?P<path>'[^']*'|\"[^\"]*\"|[^\s;&|()<>]+)\s*&&(?P<command>[\s\S]+)",
        shell,
    )
    if prefix:
        try:
            path_text = shlex.split(prefix["path"])[0]
        except ValueError:
            return None
        # No expansion, CDPATH lookup, or guessing after a failed cd.
        if any(character in path_text for character in "$`\\~*?["):
            return None
        target = Path(path_text)
        if not target.is_absolute() or not target.is_dir():
            return None
        if any(OPERATORS.match(token) and token != "&&" for token in shell_tokens(prefix["command"])):
            return None
        cwd = target
        shell = prefix["command"]
    try:
        commands = shell_commands(shell)
    except ValueError:
        return None
    tokens = [token for argv in commands for token in argv]
    if tokens and Path(tokens[0]).name in SHELLS:
        body = shell_c_body(tokens)
        if body is None or len(commands) != 1 or tokens[-1] != body:
            return None
        return exception_cwd(body, cwd)
    for position, token in enumerate(tokens):
        if token in {"cd", "pushd", "popd", "eval", "source", "--active", "--no-project"} or Path(token).name in SHELLS:
            return None
        if token.startswith(POSTGRES_URL_VARIABLE) and loopback_database(token.removeprefix(POSTGRES_URL_VARIABLE)):
            continue
        if token.startswith("-C") or ASSIGNMENT.match(token) or token.split("=", 1)[0] in {"-C", "--chdir", "--directory", "--project", "--with", "--with-editable", "--with-requirements", "--env-file"}:
            return None
        # A path ending in launchplane is another executable; `--repository OWNER/launchplane` names this one.
        repository = token.removeprefix("--repository=") if tokens[position - 1:position] != ["--repository"] else token
        repository_slug = repository != token or tokens[position - 1:position] == ["--repository"]
        if Path(token).name == "launchplane" and token != "launchplane" and not (repository_slug and REPOSITORY_SLUG.fullmatch(repository)):
            return None
    for argv in commands:
        while argv and argv[0] in TRANSPARENT | {"builtin"}:
            argv = argv[1:]
        normalized = unwrap(argv)
        if normalized and normalized[0][:3] == POSTGRES_GATE and not names_loopback_database(argv):
            return None
        for index, token in enumerate(argv):
            gate = DOT_ROOT_OPTIONS.get(argv[index - 1]) if index > 0 else None
            if token == "." and not (gate and normalized and normalized[0][:len(gate)] == gate):
                return None
        command = argv
        while command and command[0].startswith(POSTGRES_URL_VARIABLE):
            command = command[1:]
        if any(Path(token).name == "launchplane" for token in command) and command[:2] != ["uv", "run"]:
            return None
    return cwd if cwd.is_absolute() else None


def blocking_policy(shell: str, cwd: Path | None = None) -> tuple[dict[str, Any], str, list[str]] | None:
    simulator = load_simulator()
    catalog = {(entry["skill"], entry["id"]): entry for entry in simulator.policy_catalog()}
    context = exception_cwd(shell, cwd or Path.cwd())
    for argv in simple_commands(shell):
        wrapped_gh = argv[0] == "gh-with-env-token"
        matched_argv = ["gh", *argv[1:]] if wrapped_gh else argv
        # Regex policies see the command as written, wrapper included, from command position only.
        for match in simulator.simulate(matched_argv, cwd=context, command_argv=argv):
            policy = catalog[(match.skill, match.policy_id)]
            if wrapped_gh and policy.get("action") == "require_preferred" and any(
                Path(preferred.get("path", "")).name == "gh-with-env-token"
                for preferred in policy.get("preferred", [])
            ):
                # Some policies require only automation auth. The wrapper
                # already supplies their declared preferred route. Still check
                # the other matches (for example the issue source-edit reject).
                continue
            return policy, match.skill, argv
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if event.get("tool_name") != "Bash":
            return 0
        shell = event["tool_input"]["command"]
        blocked = blocking_policy(shell, Path(event.get("cwd") or Path.cwd()))
    except Exception as error:  # noqa: BLE001 - fail open, see module docstring
        print(f"command policy hook skipped: {error!r}", file=sys.stderr)
        return 0
    if blocked is None:
        return 0
    message = describe(*blocked)
    if "--json" in sys.argv[1:]:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": message,
        }}))
        return 0
    print(message, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
