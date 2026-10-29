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
import io
import ipaddress
import json
import os
import re
import shlex
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, urlsplit

# Resolve through the install link so the catalog is found wherever it is linked from.
ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "skills"
SIMULATOR = CATALOG / "skill-creator" / "scripts" / "validate-command-policy-simulator.py"
CLAIM_LEASE = CATALOG / "github" / "scripts" / "github_plan_lease.py"
CODE_HOME_SKILLS = "$CODE_HOME/skills/"
OPERATORS = re.compile(r"^[;&|()\n]+$")
REDIRECTION = re.compile(r"[<>][<>&|]*|&>>?")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
TRANSPARENT = {"command", "exec", "time", "nohup"}
SHELL_KEYWORDS = {"{", "!", "if", "then", "elif", "else", "do", "while", "until"}
SHELLS = {"sh", "bash", "zsh"}
SCRIPT_INTERPRETERS = re.compile(r"^(?:osascript|python[\d.]*|node|swift|xdotool|xte|cliclick)$")
SCRIPT_WRAPPERS = {"sudo", "timeout", "xargs"}
DATA_ARGUMENT_HEADS = {"launchplane", "grep", "echo", "printf", "ls", "which"}
GIT_DATA_COMMANDS = {"log", "grep", "status", "diff", "show", "rev-parse", "ls-files", "ls-tree", "for-each-ref", "describe"}
GIT_VALUE_FLAGS = {"-C", "--git-dir", "--work-tree", "--namespace"}
RG_VALUE_FLAGS = {
    "-e", "--regexp", "-f", "--file", "-g", "--glob", "--iglob", "--pre-glob", "-r", "--replace",
    "--context-separator", "--field-context-separator", "--field-match-separator",
}
FIND_VALUE_FLAGS = {"-name", "-iname", "-path", "-ipath", "-regex", "-iregex", "-type", "-user", "-group", "-size", "-perm"}
FIND_EXECUTION_FLAGS = {"-exec", "-execdir", "-ok", "-okdir"}
STDIN_PROGRAM_PATHS = {"-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"}
SHELL_COMMAND_FLAG = re.compile(r"^-[A-Za-z]*c$")
GH_WRAPPER_FLAGS = {"--print-auth-account", "--require-automation-auth"}
ENV_VALUE_FLAGS = {"-u", "--unset", "-C", "--chdir"}
UV_VALUE_FLAGS = {"--python", "-p", "--extra", "--group", "--project", "--directory", "--with", "--with-editable", "--with-requirements", "--env-file"}
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


class ShellStream(io.StringIO):
    """Keep comment-ending newlines available as shell command separators."""

    def readline(self, size: int = -1) -> str:
        line = super().readline(size)
        if line.endswith("\n"):
            self.seek(self.tell() - 1)
            return line[:-1]
        return line


def shell_tokens(shell: str) -> list[str]:
    lexer = shlex.shlex(ShellStream(shell), posix=True, punctuation_chars="();<>|&\n")
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
        self.pending: list[tuple[str, bool, bool]] = []
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
                self.stack.append(("(", self.index + 2))
                self.emit(self.index + 2)
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
            elif shell.startswith("((", self.index) and (self.index == 0 or shell[self.index - 1] in " \t\r\n;&|"):
                self.stack.append(("((", None))  # An arithmetic command, as in `$((`.
                self.emit(self.index + 2)
            elif character == "(":
                self.stack.append(("(", None))
                self.emit(self.index + 1)
            elif character == ")" and context == "(":
                self.close()
            elif shell.startswith("<<<", self.index):
                self.emit(self.index + 3)  # A here-string: its word is an ordinary argument.
            elif context != "((" and shell.startswith("<<", self.index):
                self.heredoc()
            elif character == "\n" and self.pending:
                self.emit(self.index + 1)
                self.read_bodies()
            else:
                self.emit(self.index + 1)

    def close(self) -> None:
        _, start = self.stack.pop()
        # Record only outermost substitutions; parsing one finds those inside it.
        if start is not None and all(enclosing is None for _, enclosing in self.stack):
            self.substitutions.append(self.shell[start:self.index])
        self.emit(self.index + 1)

    def backtick(self) -> None:
        if self.stack and self.stack[-1][0] == "`":
            self.close()
        else:
            self.stack.append(("`", self.index + 1))
            self.emit(self.index + 1)

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
            self.pending.append(("".join(word), strip_tabs, not quoted))
        self.emit(min(cursor, len(shell)))

    def read_bodies(self) -> None:
        """Consume the body lines of each heredoc opened on the line just ended."""
        shell = self.shell
        cursor = self.index
        header = self.text
        for position, (delimiter, strip_tabs, expands) in enumerate(self.pending):
            body: list[str] = []
            while cursor < len(shell):
                end = shell.find("\n", cursor)
                end = len(shell) if end < 0 else end
                line = shell[cursor:end]
                cursor = end + 1
                if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                    break
                body.append(line)
            occurrence = -sum(entry[0] == delimiter for entry in self.pending[position:])
            self.bodies.append(("\n".join(body), expands, header, delimiter, occurrence))
        self.pending = []
        self.index = min(cursor, len(shell))


def reads_script_from_input(argv: list[str]) -> bool:
    """A shell without `-c`, even behind a wrapper such as `sudo`, or ssh may run heredoc text."""
    if argv[0] == "ssh":
        return True
    return any(
        Path(token).name in SHELLS and not any(SHELL_COMMAND_FLAG.match(argument) for argument in span[index + 1 :])
        for span in execution_spans(argv)
        for index, token in enumerate(span)
    )


def without_redirections(argv: list[str]) -> list[str]:
    arguments: list[str] = []
    cursor = 0
    while cursor < len(argv):
        token = argv[cursor]
        if token.isdecimal() and cursor + 1 < len(argv) and REDIRECTION.fullmatch(argv[cursor + 1]):
            cursor += 1
        elif REDIRECTION.fullmatch(token):
            cursor += 3 if token == "<<" and argv[cursor + 1:cursor + 2] == ["-"] else 2
        else:
            arguments.append(token)
            cursor += 1
    return arguments


def reads_program_from_stdin(argv: list[str]) -> bool:
    arguments = without_redirections(argv[1:])
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


def heredoc_script_readers(header: str, delimiter: str, occurrence: int) -> list[list[str]]:
    """Find the heredoc's consumer and downstream pipe readers, never neighboring commands."""
    try:
        tokens = shell_tokens(header)
    except ValueError:
        return []  # An enclosing substitution may have an open quote; its nested script is checked separately.
    markers = [
        index for index, token in enumerate(tokens[:-1]) if token == "<<"
        and (tokens[index + 1] in {delimiter, "-" + delimiter}
             or tokens[index + 1:index + 3] == ["-", delimiter])
    ]
    if len(markers) < -occurrence:
        return []
    marker = markers[occurrence]
    start = max((index + 1 for index in range(marker) if OPERATORS.match(tokens[index])), default=0)
    end = next((index for index in range(marker + 1, len(tokens))
                if OPERATORS.match(tokens[index]) and tokens[index] not in {"|", "|&"}), len(tokens))
    commands: list[list[str]] = [[]]
    for token in tokens[start:end]:
        if token in {"|", "|&"}:
            commands.append([])
        else:
            commands[-1].append(token)
    return script_readers(commands)


def script_readers(commands: list[list[str]]) -> list[list[str]]:
    """Normalize known interpreter wrappers and check which command consumes a stdin program."""
    readers: list[list[str]] = []
    for command in commands:
        for argv in unwrap(command):
            for index, token in enumerate(argv):
                if (index != 0 and argv[0] not in SCRIPT_WRAPPERS) or not SCRIPT_INTERPRETERS.fullmatch(Path(token).name):
                    continue
                reader = [Path(token).name, *argv[index + 1:]]
                if reads_program_from_stdin(reader):
                    readers.append(reader)
    return readers


def literal_pipeline_programs(shell: str) -> list[list[str]]:
    """Expose literal echo/printf programs to the same policies as interpreter heredocs."""
    pipelines: list[list[list[str]]] = [[[]]]
    for token in shell_tokens(shell):
        if token in {"|", "|&"}:
            pipelines[-1].append([])
        elif OPERATORS.match(token):
            pipelines.append([[]])
        else:
            pipelines[-1][-1].append(token)
    programs: list[list[str]] = []
    for pipeline in pipelines:
        if len(pipeline) < 2:
            continue
        passthrough = True
        for stage in pipeline[1:-1]:
            normalized = unwrap(stage)
            if len(normalized) != 1 or not (
                normalized[0] in [["cat"], ["cat", "-"]]
                or normalized[0][0] == "tee" and not {"--help", "--version"} & set(normalized[0])
            ):
                passthrough = False
                break
        if not passthrough:
            continue
        producers = unwrap(pipeline[0])
        if len(producers) != 1 or producers[0][0] not in {"echo", "printf"}:
            continue
        producer = producers[0]
        if writes_shell_variable(producer):
            continue  # This writes a shell variable, not stdout.
        payload = "\n".join(producer[1:]).replace("\\n", "\n").replace("\\t", "\t")
        programs.extend([*reader, payload] for reader in script_readers(pipeline[-1:]))
    return programs


def simple_commands(shell: str) -> list[list[str]]:
    """Return the argv of each simple command a shell line runs, per the module docstring."""
    script = ShellScript(shell)
    unwrapped: list[list[str]] = []
    raw_commands = shell_commands(script.text)
    for argv in raw_commands:
        unwrapped.extend(unwrap(argv))
    unwrapped.extend(nested_commands(script.substitutions))
    unwrapped.extend(literal_pipeline_programs(script.text))
    shell_reads_bodies = any(reads_script_from_input(argv) for argv in [*raw_commands, *unwrapped])
    for body, expands, header, delimiter, occurrence in script.bodies:
        # Language payloads are arguments to their interpreter, not shell
        # commands. Preserve them for regex policies (also for a cat pipeline).
        script_readers = heredoc_script_readers(header, delimiter, occurrence)
        unwrapped.extend([*argv, body] for argv in script_readers)
        if shell_reads_bodies:
            unwrapped.extend(nested_commands([body]))
        elif expands:
            unwrapped.extend(nested_commands(ShellScript(body, expanding=True).substitutions))
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


def executable_arguments(argv: list[str]) -> list[str]:
    while argv:
        head = Path(argv[0]).name
        if ASSIGNMENT.match(argv[0]) or head in TRANSPARENT | SHELL_KEYWORDS:
            argv = argv[1:]
        elif head == "env":
            argv = drop_flags(argv[1:], ENV_VALUE_FLAGS)
        elif head == "uv" and argv[1:2] == ["run"]:
            argv = drop_flags(argv[2:], UV_VALUE_FLAGS)
        else:
            break
    return argv


def writes_shell_variable(argv: list[str]) -> bool:
    return len(argv) > 1 and argv[0] == "printf" and argv[1].startswith("-v")


def git_program_arguments(argv: list[str]) -> list[list[str]] | None:
    cursor = 1
    while cursor < len(argv) and argv[cursor].startswith("-"):
        token = argv[cursor]
        if token in {"-P", "--no-pager"}:
            cursor += 1
        elif token in GIT_VALUE_FLAGS:
            cursor += 2
        elif token.startswith("-C") or "=" in token and token.split("=", 1)[0] in GIT_VALUE_FLAGS:
            cursor += 1
        else:
            return None
    if cursor >= len(argv) or argv[cursor] not in GIT_DATA_COMMANDS:
        return None
    command = argv[cursor]
    programs = []
    cursor += 1
    while cursor < len(argv) and argv[cursor] != "--":
        token = argv[cursor]
        if command == "grep" and token in {"-e", "--regexp", "-f", "--file"}:
            cursor += 2
            continue
        flag, separator, value = token.partition("=")
        if flag in {"--ext-diff", "--textconv"}:
            return None
        pager = re.match(r"^-[^-O]*O(.*)$", token) if command == "grep" else None
        if pager:
            programs.append([pager[1]] if pager[1] else [])
        elif command == "grep" and flag.startswith("--") and "--open-files-in-pager".startswith(flag):
            programs.append([value] if separator else [])
        cursor += 1
    return programs


def rg_program_arguments(argv: list[str]) -> list[list[str]]:
    programs = []
    cursor = 1
    while cursor < len(argv) and argv[cursor] != "--":
        token = argv[cursor]
        flag, separator, value = token.partition("=")
        if flag in {"--pre", "--hostname-bin"}:
            programs.append([value] if separator else argv[cursor + 1 : cursor + 2])
        cursor += 2 if token in RG_VALUE_FLAGS else 1
    return programs


def execution_spans(argv: list[str]) -> list[list[str]]:
    arguments = without_redirections(argv)
    executable = executable_arguments(arguments)
    if not executable:
        return [arguments]
    prefix = arguments[: len(arguments) - len(executable) + 1]
    head = executable[0]
    if head == "rg":
        programs = rg_program_arguments(executable)
        return [arguments, *programs] if programs else [prefix]
    if head == "git":
        programs = git_program_arguments(executable)
        if programs is None:
            return [arguments]
        return [arguments, *programs] if programs else [prefix]
    if writes_shell_variable(executable):
        return [arguments]
    if head in DATA_ARGUMENT_HEADS:
        return [prefix]
    if head != "find":
        return [arguments]
    spans = [prefix]
    cursor = 1
    ambiguous = False
    while cursor < len(executable):
        token = executable[cursor]
        if token in FIND_VALUE_FLAGS and not ambiguous:
            cursor += 2
            continue
        if token in FIND_EXECUTION_FLAGS:
            start = cursor + 1
            end = next(
                (
                    index
                    for index in range(start, len(executable))
                    if executable[index] == ";" or executable[index] == "+" and index > start and executable[index - 1] == "{}"
                ),
                len(executable),
            )
            spans.extend(execution_spans(executable[start:end]))
            cursor = cursor + 1 if ambiguous else end + 1
            continue
        if token.startswith("-"):
            ambiguous = True
        cursor += 1
    return spans


def unwrap(argv: list[str]) -> list[list[str]]:
    argv = executable_arguments(argv)
    if not argv:
        return []
    if Path(argv[0]).name == "gh-with-env-token":
        arguments = argv[1:]
        while arguments and arguments[0] in GH_WRAPPER_FLAGS:
            arguments = arguments[1:]
        if arguments[:1] != ["--check"]:
            return [["gh-with-env-token", *arguments]]
    if Path(argv[0]).name == "eval":
        return nested_commands([" ".join(argv[1:])])
    executable_argv = argv
    argv = [Path(argv[0]).name, *argv[1:]]
    if argv[0] in SHELLS:
        for index, token in enumerate(argv[1:-1], start=1):
            if SHELL_COMMAND_FLAG.match(token):  # -c, -lc, -ec ...
                return nested_commands([argv[index + 1]])
    commands = [argv]
    for span in execution_spans(executable_argv):
        shells = [index for index, token in enumerate(span[:-1]) if Path(token).name in SHELLS]
        if shells:
            flag = next((index for index in range(shells[0] + 1, len(span) - 1) if SHELL_COMMAND_FLAG.match(span[index])), None)
            if flag is not None:
                commands += nested_commands([span[flag + 1]])
    if argv[0] == "ssh":
        # The remote command runs too; the host and option values read as harmless words.
        commands += nested_commands([" ".join(argv[1:])])
    if reads_script_from_input(executable_argv) and "<<<" in argv[:-1]:
        commands += nested_commands([argv[argv.index("<<<") + 1]])
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
        for index, token in enumerate(tokens[1:-1], start=1):
            if SHELL_COMMAND_FLAG.match(token):
                if len(commands) != 1 or index + 2 != len(tokens):
                    return None
                return exception_cwd(tokens[index + 1], cwd)
        return None
    for token in (token for argv in commands for span in execution_spans(argv) for token in span):
        if Path(token).name in SHELLS:
            return None
        if token.startswith(POSTGRES_URL_VARIABLE) and loopback_database(token.removeprefix(POSTGRES_URL_VARIABLE)):
            continue
        if ASSIGNMENT.match(token):
            return None
    for raw_argv in commands:
        argv = without_redirections(raw_argv)
        while argv and (argv[0] in TRANSPARENT | SHELL_KEYWORDS | {"builtin", "function"} or ASSIGNMENT.match(argv[0])):
            wrapper = argv[0]
            argv = argv[2:] if wrapper == "function" else argv[1:]
            if wrapper in TRANSPARENT | {"builtin"}:
                argv = drop_flags(argv)
        if argv and argv[0] in {".", "source", "cd", "pushd", "popd", "eval"}:
            return None
        if writes_shell_variable(argv):
            return None
        normalized = unwrap(raw_argv)
        if not normalized or normalized[0][:1] != ["launchplane"]:
            continue
        executable_position = len(raw_argv) - len(normalized[0])
        for token in raw_argv[:executable_position + 1]:
            if (
                token in {"--active", "--no-project"}
                or token.startswith("-C")
                or token.split("=", 1)[0]
                in {"--chdir", "--directory", "--project", "--with", "--with-editable", "--with-requirements", "--env-file"}
            ):
                return None
            if (
                Path(token).name == "launchplane"
                and token != "launchplane"
            ):
                return None
        if normalized[0][:3] == POSTGRES_GATE and not names_loopback_database(raw_argv):
            return None
        if argv[:2] != ["uv", "run"]:
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


def renew_claim_lease(event: object) -> None:
    try:
        spec = importlib.util.spec_from_file_location("github_plan_lease", CLAIM_LEASE)
        assert spec and spec.loader
        lease = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lease)
        lease.renew_from_hook(event)
    except (OSError, ValueError, ImportError, AssertionError) as error:
        print(f"claim lease renewal skipped: {error!r}", file=sys.stderr)


def main() -> int:
    try:
        event = json.load(sys.stdin)
        renew_claim_lease(event)
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
