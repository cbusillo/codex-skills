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
front of the tool name, `gh-with-env-token`, and one `sh`/`bash`/`zsh -c '...'` wrapper. This is a
guardrail for habits, not a security boundary. Forms that need real option
parsing to unwrap, such as `xargs` and `sudo`, are deliberately left alone.

Policies match commands, not data (#671, #1014). A heredoc body is data unless
a shell or ssh reads it; command substitutions in an unquoted-delimiter body,
in double-quoted arguments and in arithmetic still run and are checked, as are
`eval` arguments, a shell's here-string and an ssh remote command. A `shell_regex` policy
matches only where a token of a running command starts, so a quoted argument
such as a search pattern or a prose example does not match, while `sudo gh api`
or `xargs gh api` still does.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shlex
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

# Resolve through the install link so the catalog is found wherever it is linked from.
ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "skills"
SIMULATOR = CATALOG / "skill-creator" / "scripts" / "validate-command-policy-simulator.py"
CODE_HOME_SKILLS = "$CODE_HOME/skills/"
OPERATORS = re.compile(r"^[;&|()\n]+$")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
TRANSPARENT = {"command", "exec", "time", "nohup"}
SHELL_KEYWORDS = {"{", "!", "if", "then", "elif", "else", "do", "while", "until"}
SHELLS = {"sh", "bash", "zsh"}
SHELL_COMMAND_FLAG = re.compile(r"^-[A-Za-z]*c$")
GH_WRAPPER_FLAGS = {"--print-auth-account", "--require-automation-auth"}
ENV_VALUE_FLAGS = {"-u", "--unset", "-C", "--chdir"}
UV_VALUE_FLAGS = {"--python", "-p", "--extra", "--group", "--project", "--directory", "--with", "--with-editable", "--with-requirements"}


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
    body and whether the shell expands it (an unquoted delimiter).
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
        self.bodies: list[tuple[str, bool]] = []
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
            elif character == "(":
                self.stack.append(("(", None))
                self.emit(self.index + 1)
            elif character == ")" and context == "(":
                self.close()
            elif context != "((" and shell.startswith("<<", self.index) and not shell.startswith("<<<", self.index):
                self.heredoc()
            elif character == "\n" and self.pending:
                self.emit(self.index + 1)
                self.read_bodies()
            else:
                self.emit(self.index + 1)

    def close(self) -> None:
        _, start = self.stack.pop()
        if start is not None:
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
        for delimiter, strip_tabs, expands in self.pending:
            body: list[str] = []
            while cursor < len(shell):
                end = shell.find("\n", cursor)
                end = len(shell) if end < 0 else end
                line = shell[cursor:end]
                cursor = end + 1
                if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                    break
                body.append(line)
            self.bodies.append(("\n".join(body), expands))
        self.pending = []
        self.index = min(cursor, len(shell))


def reads_script_from_input(argv: list[str]) -> bool:
    """A shell without `-c`, or ssh, may run its stdin or a file, so heredoc text may run."""
    if argv[0] == "ssh":
        return True
    return argv[0] in SHELLS and not any(SHELL_COMMAND_FLAG.match(token) for token in argv[1:])


def simple_commands(shell: str, nested: bool = False) -> list[list[str]]:
    """Return the argv of each simple command a shell line runs, per the module docstring."""
    script = ShellScript(shell)
    unwrapped: list[list[str]] = []
    for argv in shell_commands(script.text):
        unwrapped.extend(unwrap(argv, nested))
    unwrapped.extend(nested_commands(script.substitutions, nested))
    shell_reads_bodies = any(reads_script_from_input(argv) for argv in unwrapped)
    for body, expands in script.bodies:
        if shell_reads_bodies:
            unwrapped.extend(nested_commands([body], nested))
        elif expands:
            unwrapped.extend(nested_commands(ShellScript(body, expanding=True).substitutions, nested))
    return unwrapped


def nested_commands(sources: list[str], nested: bool) -> list[list[str]]:
    commands: list[list[str]] = []
    for source in sources:
        try:
            commands.extend(simple_commands(source, nested))
        except ValueError:
            continue  # An unreadable part must not let the rest of the line through.
    return commands


def drop_flags(argv: list[str], value_flags: set[str] | None = None) -> list[str]:
    while argv and argv[0].startswith("-"):
        flag = argv[0]
        argv = argv[2:] if value_flags and flag in value_flags else argv[1:]
        if flag == "--":
            break
    return argv


def unwrap(argv: list[str], nested: bool) -> list[list[str]]:
    """Return the command or commands an argv really runs, per the module docstring."""
    while argv:
        head = Path(argv[0]).name
        if ASSIGNMENT.match(argv[0]) or head in TRANSPARENT | SHELL_KEYWORDS:
            argv = argv[1:]
        elif head == "env":
            argv = drop_flags(argv[1:], ENV_VALUE_FLAGS)
        elif head == "uv" and argv[1:2] == ["run"]:
            argv = drop_flags(argv[2:], UV_VALUE_FLAGS)
        elif head == "gh-with-env-token":
            arguments = argv[1:]
            while arguments and arguments[0] in GH_WRAPPER_FLAGS:
                arguments = arguments[1:]
            if arguments[:1] == ["--check"]:
                break  # Auth preflight exits without executing gh.
            return [["gh-with-env-token", *arguments]]
        elif head == "eval":
            # Its quoted arguments are the command, not data.
            return simple_commands(" ".join(argv[1:]), nested)
        else:
            break
    if not argv:
        return []
    argv = [Path(argv[0]).name, *argv[1:]]
    if argv[0] in SHELLS and not nested:
        for index, token in enumerate(argv[1:-1], start=1):
            if SHELL_COMMAND_FLAG.match(token):  # -c, -lc, -ec ...
                return simple_commands(argv[index + 1], nested=True)
    commands = [argv]
    shells = [index for index, token in enumerate(argv[1:-1], start=1) if Path(token).name in SHELLS]
    if shells and not nested:
        # A wrapper left in place (`sudo sh -c`, `xargs bash -c`) still runs the script.
        flag = next((index for index in range(shells[0] + 1, len(argv) - 1) if SHELL_COMMAND_FLAG.match(argv[index])), None)
        if flag is not None:
            commands += nested_commands([argv[flag + 1]], nested=True)
    if argv[0] == "ssh":
        # The remote command runs too; the host and option values read as harmless words.
        commands += nested_commands([" ".join(argv[1:])], nested)
    if reads_script_from_input(argv) and "<<<" in argv[:-1]:
        commands += nested_commands([argv[argv.index("<<<") + 1]], nested)
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
    for token in tokens:
        if token in {"cd", "pushd", "popd", "eval", "source", "--active", "--no-project"} or Path(token).name in SHELLS:
            return None
        if token.startswith("-C") or ASSIGNMENT.match(token) or token.split("=", 1)[0] in {"-C", "--chdir", "--directory", "--project", "--with", "--with-editable", "--with-requirements"}:
            return None
        if Path(token).name == "launchplane" and token != "launchplane":
            return None
    for argv in commands:
        while argv and argv[0] in TRANSPARENT | {"builtin"}:
            argv = argv[1:]
        normalized = unwrap(argv, nested=True)
        for index, token in enumerate(argv):
            if token == "." and not (
                index > 0 and argv[index - 1] == "--control-plane-root"
                and normalized and normalized[0][:3] == ["launchplane", "service", "audit-config-authority"]
            ):
                return None
        if "launchplane" in argv and argv[:2] != ["uv", "run"]:
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
