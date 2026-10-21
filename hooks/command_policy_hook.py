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


class LiteralToken(str):
    pass


class ShellLexer(shlex.shlex):
    def __init__(self, text: str, *, posix: bool, punctuation_chars: str, literals: dict[str, str]) -> None:
        super().__init__(text, posix=posix, punctuation_chars=punctuation_chars)
        self.literals = literals

    def get_token(self) -> str | None:
        token = super().get_token()
        if token is None:
            return None
        literal = False
        for protected, value in self.literals.items():
            if protected in token:
                token = token.replace(protected, value)
                literal = True
        return LiteralToken(token) if literal else token


def is_operator(token: str) -> bool:
    return not isinstance(token, LiteralToken) and bool(OPERATORS.match(token))


def is_redirection(token: str) -> bool:
    return not isinstance(token, LiteralToken) and bool(re.fullmatch(r"[<>][<>&|]*|&>>?", token))


def shell_lexer(text: str, *, posix: bool = True, punctuation_chars: str = ";&|<>()\n") -> shlex.shlex:
    chars, quote, escaped, boundary, comment = [], None, False, True, False
    prefix = "__codex_shell_literal_"
    while prefix in text:
        prefix += "_"
    protected = {char: f"{prefix}{ord(char)}__" for char in punctuation_chars} if posix else {}
    for char in text:
        if comment:
            if char != "\n":
                continue
            comment = False
        if escaped:
            if char == "\n":
                chars.pop()
            else:
                chars.append(protected.get(char, char))
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
        chars.append(protected.get(char, char) if quote else char)
        boundary = quote is None and (char.isspace() or char in ";&|()")
    lexer = ShellLexer("".join(chars), posix=posix, punctuation_chars=punctuation_chars,
                       literals={value: char for char, value in protected.items()})
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
        if is_operator(token):
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
        self.process_substitutions: set[int] = set()
        self.process_starts: set[int] = set()
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
        for index, (start, end) in enumerate(self.substitution_spans):
            parts.extend((text[cursor:start], substitution_marker(index)))
            cursor = end
        return "".join([*parts, text[cursor:]])

    def open_scope(self, kind: str, start: int | None, end: int) -> None:
        self.stack.append((kind, start))
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
                self.process_starts.add(self.index + 2)
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
                    self.emit(self.index + 2)
                else:
                    self.background |= self.index == 0 or shell[self.index - 1] not in '><|;'
                    self.emit(self.index + 1)
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
            elif context != "((" and character in ";|\n":
                length = 2 if shell.startswith(("||", "|&"), self.index) else 1
                self.emit(self.index + length)
            else:
                self.emit(self.index + 1)

    def close(self) -> None:
        _, start = self.stack.pop()
        # Record only outermost substitutions; parsing one finds those inside it.
        if start is not None and all(enclosing is None for _, enclosing in self.stack):
            if start in self.process_starts:
                self.process_substitutions.add(len(self.substitutions))
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
        elif is_redirection(token):
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
    initializers: tuple[str, ...] = ()

    @property
    def stdin(self) -> bool:
        return self.mode in {"implicit", "stdin"}


class XargsInput(NamedTuple):
    replacement: str | None
    stdin_uncertain: bool
    unknown: bool
    template: tuple[str, ...]
    composed: bool = False
    stdin_inherited: bool = False


class Command(NamedTuple):
    argv: list[str]
    roles: set[str]
    xargs: tuple[XargsInput, ...]


class SshInvocation(NamedTuple):
    remote: list[str]
    background: bool
    stdin: bool


class ExecutionSource(NamedTuple):
    text: str
    inline: bool = False
    input_kind: str = "data"
    exact: bool = False
    mapping: tuple[XargsInput, ...] = ()


class BoundInput(NamedTuple):
    commands: list[list[str]]
    active: bool


def shell_invocation(argv: list[str]) -> ShellInvocation:
    argv = without_redirections(argv)
    if not argv or Path(argv[0]).name not in SHELLS:
        return ShellInvocation(None, "other", None, 0)
    cursor, command, stdin, options_closed = 1, False, False, False
    initializers = []
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
        if option in {"--rcfile", "--init-file"} and cursor + 1 < len(argv):
            initializers.append(argv[cursor + 1])
        cursor += 1
        if option in {"--rcfile", "--init-file"} or not option.startswith("--") and option.endswith(("o", "O")):
            cursor += 1
    if command:
        return ShellInvocation(argv[cursor] if cursor < len(argv) else None, "inline", None, cursor, options_closed, tuple(initializers))
    if stdin or cursor < len(argv) and argv[cursor] in STDIN_PROGRAM_PATHS:
        return ShellInvocation(None, "stdin", None, cursor, options_closed, tuple(initializers))
    if cursor >= len(argv):
        return ShellInvocation(None, "implicit", None, cursor, options_closed, tuple(initializers))
    return ShellInvocation(None, "script", argv[cursor], cursor, options_closed, tuple(initializers))


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
        elif head == "flock":
            cursor = 0
            while cursor < len(tail) and tail[cursor].startswith("-"):
                option = tail[cursor]
                if option in {"-c", "--command"} or option.startswith("--command="):
                    return finish([head, *tail])
                cursor += 2 if option in {"-w", "--timeout", "-E", "--conflict-exit-code"} else 1
                if option == "--":
                    break
            child = tail[cursor + 1:]
            if not child or child[0] in {"-c", "--command"}:
                return finish([head, *tail])
            roles.add(head)
            argv = child
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


def shell_candidates(argv: list[str], *, include_nonliteral: bool = False, hints: bool = True) -> list[list[str]]:
    if not argv:
        return []
    if argv[0] in SHELLS:
        return [argv]
    if include_nonliteral and any(char in argv[0] for char in "$`"):
        return [["sh", *argv[1:]]]
    if not hints or argv[0] == "find":
        return []
    return [argv[index:] for index, word in enumerate(argv[1:], 1) if Path(word).name in SHELLS]


def ssh_command(argv: list[str]) -> SshInvocation:
    argv = without_redirections(argv)
    cursor, background, stdin = 1, False, True
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
            stdin &= flag not in "nf"
    return SshInvocation(argv[cursor + 1:], background, stdin)


def command_input(argv: list[str], *, completion: bool = False, depth: int = 0,
                  executed_files: set[str] | None = None, hints: bool = True,
                  mapping: tuple[XargsInput, ...] = ()) -> tuple[list[str], str]:
    if depth >= 16:
        raise ValueError("shell nesting exceeds coverage")
    normalized = normalize_command(argv)
    argv, frames = normalized.argv, (*mapping, *normalized.xargs)
    executed_files = executed_files or set()
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
            invocation = ssh_command(argv)
            tail = invocation.remote
            kind = "shell" if not tail and invocation.stdin else "data"
        redirection = next((index for index, word in enumerate(tail) if word.startswith(("<", ">"))), len(tail))
        if tail[:redirection]:
            programs.append(" ".join(tail[:redirection]))
    elif name in {"source", "."}:
        words = without_redirections(tail)
        if words[:1] == ["--"]:
            words = words[1:]
        if not words or words[0] in STDIN_PROGRAM_PATHS:
            kind = "shell"
    elif name in {"su", "runuser", "script", "flock"}:
        for index, flag in enumerate(tail[:-1]):
            if flag == "--command" or flag.startswith("-") and "c" in flag[1:]:
                programs.append(tail[index + 1])
                break
        if name in {"su", "runuser"} and not programs:
            kind = "shell"
    elif name == "find":
        kinds = [command_input(child, completion=completion, depth=depth + 1,
                              executed_files=executed_files, hints=False, mapping=find_mapping(child))[1]
                 for child in find_commands(argv)]
        kind = combine_input_kinds(kinds)
    if programs and name in SHELLS | {"eval", "ssh", "su", "runuser", "script", "flock"}:
        if name != "ssh" or ssh_command(argv).stdin:
            kind = combine_input_kinds([inherited_input(source, executed_files, completion=completion, depth=depth + 1)
                                        for source in programs])
    if completion and ("$" in name or "`" in name):
        invocation = shell_invocation(shell_candidates(argv, include_nonliteral=True)[0])
        if invocation.body is not None:
            programs.append(invocation.body)
        if invocation.stdin:
            kind = "unknown"
    elif name not in SHELLS | {"ssh"} and kind == "data":
        for candidate in shell_candidates(argv, include_nonliteral=completion):
            invocation = shell_invocation(candidate)
            if invocation.body is not None:
                programs.append(invocation.body)
            if completion and any(Path(word).name == "xargs" for word in argv[:len(argv) - len(candidate)]):
                kind = "opaque"
    if frames:
        unproved = len(frames) != 1 or any(frame.unknown for frame in frames)
        unproved |= name not in SHELLS and bool(shell_candidates(argv, include_nonliteral=True, hints=hints))
        unproved |= any(char in name for char in "$`")
        unproved |= any(frame.replacement and frame.replacement in name for frame in frames)
        unproved |= any(frame.replacement and frame.replacement in source for frame in frames for source in programs)
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
                unproved |= invocation.mode == "stdin" and not frame.stdin_inherited or frame.stdin_uncertain and invocation.mode == "implicit"
            kind = "opaque" if completion and unproved else kind if all(frame.stdin_inherited for frame in frames) else "data"
        elif unproved or name in {"eval", "ssh", "source", ".", "su", "runuser", "script", "flock"} or kind != "data":
            kind = "opaque" if completion else "data"
    if input_is_source(kind, completion=completion) and "<<<" in argv[:-1]:
        payload = argv[argv.index("<<<") + 1]
        programs.append(payload)
    return programs, kind


def input_is_source(kind: str, *, completion: bool) -> bool:
    return kind == "shell" or completion and kind == "unknown"


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


def heredoc_commands(header: str, delimiter: str, occurrence: int) -> BoundInput:
    tokens = shell_tokens(header)
    tokens = [token.rstrip("\n") if is_operator(token) and token.rstrip("\n") in {"|", "|&"} else token for token in tokens]
    tokens = [token for index, token in enumerate(tokens)
              if token != "\n" or index == 0 or tokens[index - 1] not in {"|", "|&"}]
    markers = [
        index for index, token in enumerate(tokens[:-1]) if is_redirection(token) and token == "<<"
        and (tokens[index + 1] in {delimiter, "-" + delimiter}
             or tokens[index + 1:index + 3] == ["-", delimiter])
    ]
    if len(markers) < -occurrence:
        return BoundInput([], False)
    marker = markers[occurrence]
    start = max((index + 1 for index in range(marker) if is_operator(tokens[index])), default=0)
    if marker and tokens[marker - 1] == ")":
        return BoundInput([[")"]], True)
    end = next((index for index in range(marker + 1, len(tokens))
                if is_operator(tokens[index]) and tokens[index] not in {"|", "|&"}), len(tokens))
    commands: list[list[str]] = [[]]
    for token in tokens[start:end]:
        if is_operator(token) and token in {"|", "|&"}:
            commands.append([])
        else:
            commands[-1].append(token)
    return BoundInput(commands, input_redirection(commands[0]) == marker - start)


def script_readers(commands: list[list[str]]) -> list[list[str]]:
    """Normalize known interpreter wrappers and check which command consumes a stdin program."""
    readers: list[list[str]] = []
    for position, command in enumerate(commands):
        if position and (input_redirection(command) is not None or fd0_role(commands[position - 1]) == "blocks"):
            break
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
        token = token.rstrip("\n") if is_operator(token) and token.rstrip("\n") in {"|", "|&"} else token
        if is_operator(token) and token in {"|", "|&"}:
            stages[-1].append([])
        elif is_operator(token):
            stages.append([[]])
        else:
            stages[-1][-1].append(token)
    return [pipeline for pipeline in stages if any(pipeline)]


def literal_payload(command: list[str]) -> str | None:
    argv = without_redirections(normalize_command(command).argv)
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
        if len(stages) > 1 and input_redirection(stages[-1]) is None and (payload := pipeline_payload(stages[:-1])) is not None:
            programs.extend([*reader, payload] for reader in script_readers(stages[-1:]))
    return programs


def written_files(command: list[str]) -> set[str]:
    paths = {str(Path(command[index + 1])) for index, token in enumerate(command[:-1])
             if is_redirection(token) and token in {">", ">>", ">|", "&>"}}
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


def find_commands(argv: list[str]) -> list[list[str]]:
    if argv[:1] != ["find"]:
        return []
    children, cursor = [], 1
    while cursor < len(argv):
        if argv[cursor] not in {"-exec", "-execdir"}:
            cursor += 1
            continue
        start = cursor + 1
        end = next((index for index in range(start, len(argv))
                    if argv[index] == ";" or argv[index] == "+" and argv[index - 1] == "{}"), len(argv))
        if start < end:
            children.append(argv[start:end])
        cursor = end + 1
    return children


def find_mapping(argv: list[str]) -> tuple[XargsInput, ...]:
    return (XargsInput("{}", False, False, tuple(argv), stdin_inherited=True),) if any("{}" in word for word in argv) else ()


def program_references(command: list[str], written: set[str], *, depth: int = 0) -> set[str]:
    normalized = normalize_command(command)
    argv = normalized.argv
    if not argv:
        return set()
    references = {argv[0]}
    for child in find_commands(argv):
        references.update(program_references(child, written, depth=depth + 1))
    for candidate in shell_candidates(argv, include_nonliteral=True, hints=False):
        invocation = shell_invocation(candidate)
        scripts = list(invocation.initializers)
        if invocation.script is not None:
            scripts.append(invocation.script)
        references.update(scripts)
        if argv[0] in SHELLS and any(char in Path(path).name for path in scripts for char in "$`*?[{"):
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
    if command_input(command, completion=True, depth=depth)[1] in {"shell", "unknown"}:
        references.update(argv[index + 1] for index, word in enumerate(argv[:-1]) if is_redirection(word) and word == "<")
    return {Path(path).name for path in references}


def input_redirection(argv: list[str]) -> int | None:
    binding, cursor = None, 0
    while cursor < len(argv):
        token = argv[cursor]
        if not is_redirection(token):
            cursor += 1
            continue
        descriptor = int(argv[cursor - 1]) if cursor and argv[cursor - 1].isdecimal() else (0 if token.startswith("<") else 1)
        target = argv[cursor + 1:cursor + 2]
        preserves_input = token in {"<&", ">&"} and target == ["0"] or token in {"<", "<>"} and bool(target) and target[0] in STDIN_PROGRAM_PATHS
        if descriptor == 0 and not preserves_input:
            binding = cursor
        cursor += 3 if token == "<<" and argv[cursor + 1:cursor + 2] == ["-"] else 2
    return binding


def fd0_role(command: list[str]) -> str:
    argv = without_redirections(normalize_command(command).argv)
    if not argv:
        return "unknown"
    if argv[0] in {"echo", "printf"}:
        return "blocks"
    if argv[0] == "cat":
        operands = drop_flags(argv[1:])
        if not operands or all(word in STDIN_PROGRAM_PATHS for word in operands):
            return "forwards"
        return "unknown" if any(word in STDIN_PROGRAM_PATHS for word in operands) else "blocks"
    if argv[0] == "tee" and not {"--help", "--version"} & set(argv):
        return "forwards"
    return "unknown"


def combine_input_kinds(kinds: list[str]) -> str:
    return next((kind for kind in ("opaque", "shell", "unknown") if kind in kinds), "data")


def inherited_input(source: str, executed_files: set[str], *, completion: bool, depth: int) -> str:
    if depth >= 16:
        return "unknown"
    try:
        script = ShellScript(source)
        chains = pipelines(script.command_text)
        commands = [command for chain in chains for command in chain]
        written = set().union(*(written_files(command) for command in commands))
        referenced = set().union(*(program_references(command, written, depth=depth) for command in commands))
        executed = executed_files | ({Path(path).name for path in written} & referenced)
        return combine_input_kinds([stdin_kind(chain, executed, completion=completion, depth=depth, inherited=True)
                                    for chain in chains if chain and input_redirection(chain[0]) is None])
    except ValueError:
        return "unknown"


def stdin_kind(commands: list[list[str]], executed_files: set[str], *, completion: bool = False,
               depth: int = 0, inherited: bool = False, destination_kind: str = "data") -> str:
    if not commands or any(not argv for argv in commands):
        return "unknown"
    transformed = False
    for index, command in enumerate(commands):
        if index and input_redirection(command) is not None:
            return "data"
        argv = normalize_command(command).argv
        _, kind = command_input(command, completion=completion, depth=depth, executed_files=executed_files)
        if not argv:
            if inherited and all(word in SHELL_KEYWORDS for word in command):
                continue
            return "unknown"
        if not inherited and argv[0] in {"done", "fi", "esac", "}", ")"}:
            return "shell"
        if kind != "data":
            if transformed and kind in {"shell", "unknown", "opaque"}:
                return "opaque" if completion else "shell"
            return kind
        role = fd0_role(command)
        if {Path(path).name for path in written_files(command)} & executed_files:
            if role != "blocks":
                return "shell" if role == "forwards" or not completion else "opaque"
        if role == "blocks":
            return "data"
        transformed |= role == "unknown"
    if transformed and destination_kind in {"shell", "unknown"}:
        return "opaque" if completion else "shell"
    return destination_kind


def substitution_marker(index: int) -> str:
    return f"${{_native_substitution_{index}}}"


def process_input(script: ShellScript, index: int, commands: list[list[str]], executed_files: set[str], *, completion: bool) -> str:
    start, _ = script.substitution_spans[index]
    if script.text[start:start + 1] != "<":
        return "data"
    marker = substitution_marker(index)
    command = next((command for command in commands if any(marker in word for word in command)), [])
    argv = normalize_command(command).argv
    if not argv:
        return "unknown"
    binding = input_redirection(command)
    if binding is not None and command[binding + 1:binding + 2] == [marker]:
        return command_input(command, completion=completion, executed_files=executed_files)[1]
    for candidate in shell_candidates(argv, include_nonliteral=completion, hints=False):
        invocation = shell_invocation(candidate)
        if invocation.script == marker or marker in invocation.initializers:
            return "shell" if argv[0] in SHELLS else "unknown"
    source_words = without_redirections(argv)[1:]
    if source_words[:1] == ["--"]:
        source_words = source_words[1:]
    if argv[0] in {"source", "."} and source_words[:1] == [marker]:
        return "shell"
    if marker in without_redirections(argv):
        sources, _ = command_input(command, executed_files=executed_files)
        if any(re.search(r"\$(?:[0-9@*]|\{(?:\d+|[@*])\})|\bfor\s+\w+\s*(?:;|\n|\bdo\b)", source) for source in sources):
            return "unknown" if completion else "shell"
    return "data"


def execution_sources(shell: str, *, completion: bool = False, depth: int = 0,
                      executed_files: set[str] | None = None, inline: bool = False,
                      force_input: str = "data", exact: bool = False,
                      mapping: tuple[XargsInput, ...] = ()) -> Iterator[tuple[ShellScript | ValueError, list[list[str]]]]:
    if depth >= 16:
        yield ValueError("shell nesting exceeds coverage"), []
        return
    if executed_files is None:
        executed_files = set()
    if depth == 0:
        nodes = list(execution_sources(shell, completion=True, depth=depth + 1, executed_files=set()))
        discovered = [argv for node, _ in nodes if isinstance(node, ShellScript) for argv in shell_commands(node.command_text)]
        written = set().union(*(written_files(argv) for argv in discovered))
        executed = set().union(*(program_references(command, written) for command in discovered))
        executed_files = {Path(path).name for path in written} & executed
    script = ShellScript(shell)
    opaque = ["unfinished shell syntax"] if script.stack or script.pending or script.incomplete else []
    try:
        commands = shell_commands(script.command_text)
    except ValueError as error:
        yield error, []
        return
    inputs = [normalize_command(argv).argv for argv in commands]
    nested: list[ExecutionSource] = []
    for index, source in enumerate(script.substitutions):
        force = "data"
        if index in script.process_substitutions:
            try:
                force = process_input(script, index, commands, executed_files, completion=completion)
            except ValueError:
                opaque.append("unreadable process-substitution reader")
        nested.append(ExecutionSource(source, input_kind=force))
    for raw, argv in zip(commands, inputs):
        if not argv:
            continue
        if completion and inline and ("$" in argv[0] or "`" in argv[0]):
            opaque.append("nonliteral inline shell program")
        sources, kind = command_input(raw, completion=completion, executed_files=executed_files, hints=not exact, mapping=mapping)
        if completion and kind == "opaque":
            opaque.append("unproved shell input or argument mapping")
        nested.extend(ExecutionSource(source, inline=True) for source in sources)
        nested.extend(ExecutionSource(shlex.join(child), inline=True, exact=True, mapping=find_mapping(child))
                      for child in find_commands(argv))
    for stages in pipelines(script.command_text):
        if len(stages) < 2:
            continue
        if input_redirection(stages[-1]) is not None:
            continue
        kind = command_input(stages[-1], completion=completion, executed_files=executed_files)[1]
        if not input_is_source(kind, completion=completion):
            continue
        payload = pipeline_payload(stages[:-1])
        if payload is not None:
            nested.append(ExecutionSource(payload, inline=True))
        elif completion and kind in {"shell", "unknown"}:
            opaque.append("unresolved shell pipeline input")
    if input_is_source(force_input, completion=completion):
        payload = pipeline_payload(commands)
        if payload is not None:
            nested.append(ExecutionSource(payload, inline=True))
        elif completion and force_input in {"shell", "unknown"} and not script.bodies:
            opaque.append("unresolved shell process-substitution input")
    readers, bound_commands = [], []
    for index, (body, expands, header, delimiter, occurrence) in enumerate(script.bodies):
        owner = script.body_owners.get(index)
        if owner is not None:
            if owner not in script.closed_substitutions:
                opaque.append("unclosed heredoc substitution")
            continue
        try:
            if header.rstrip().endswith(("|", "|&")):
                header += script.text[len(header):]
            bound = heredoc_commands(header, delimiter, occurrence)
            if bound.active:
                bound_commands.extend(bound.commands)
                readers.extend([*argv, body] for argv in script_readers(bound.commands))
            kind = stdin_kind(bound.commands, executed_files, completion=completion, destination_kind=force_input) if bound.active else "data"
            if completion and kind == "opaque":
                opaque.append("unresolved heredoc transformation")
            if input_is_source(kind, completion=completion):
                nested.append(ExecutionSource(body))
            elif expands:
                expanded = ShellScript(body, expanding=True)
                if expanded.stack != [("body", None)] or expanded.pending or expanded.incomplete:
                    opaque.append("unfinished heredoc expansion")
                nested.extend(ExecutionSource(source) for source in expanded.substitutions)
        except ValueError as error:
            opaque.append(str(error))
    if completion and force_input in {"shell", "unknown"} and pipeline_payload(commands) is None:
        if any(command not in bound_commands for command in commands):
            opaque.append("unresolved process-substitution producer")
    yield script, readers
    if opaque:
        yield ValueError("; ".join(opaque)), []
    for source in nested:
        yield from execution_sources(source.text, completion=completion, depth=depth + 1,
                                     executed_files=executed_files, inline=source.inline, force_input=source.input_kind,
                                     exact=source.exact, mapping=source.mapping)


def detached_command(argv: list[str]) -> bool:
    command = normalize_command(argv)
    return (command.argv[:1] == ["coproc"] or "detached" in command.roles
            or command.argv[:1] == ["ssh"] and ssh_command(command.argv).background
            or any(detached_command(child) for child in find_commands(command.argv)))


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
        if any(is_operator(token) and token != "&&" for token in shell_tokens(prefix["command"])):
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
