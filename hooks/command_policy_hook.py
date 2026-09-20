#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Apply skill command policies to a Claude Code shell command before it runs.

Claude Code never shows a skill's frontmatter to the model and does not enforce
it, so `policy.command_policies` would be silent on that host. This PreToolUse
hook reads the same frontmatter at run time, through the repository's policy
simulator, and blocks a matching command with the policy's message and
preferred replacement. There is no generated copy of the policies.

Contract: JSON on stdin (`tool_name`, `tool_input.command`). Exit 0 lets the
command run. Exit 2 blocks it and stderr is returned to the model. Anything the
hook cannot read or parse lets the command run: a broken entrypoint must not
stop every shell command on the host.

Policies match an argv prefix, so the hook first removes what an agent commonly
puts in front of a tool: leading assignments, `command`/`exec`/`time`/`nohup`,
`env` with its flags and assignments, `uv run` with its flags, a directory in
front of the tool name, and one `sh`/`bash`/`zsh -c '...'` wrapper. This is a
guardrail for habits, not a security boundary. Forms that need real option
parsing to unwrap, such as `xargs` and `sudo`, are deliberately left alone.
"""

from __future__ import annotations

import importlib.util
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
OPERATORS = re.compile(r"^[;&|()]+$")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
TRANSPARENT = {"command", "exec", "time", "nohup"}
SHELLS = {"sh", "bash", "zsh"}
SHELL_COMMAND_FLAG = re.compile(r"^-[A-Za-z]*c$")


def load_simulator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("command_policy_simulator", SIMULATOR)
    if spec is None or spec.loader is None:
        raise ImportError(str(SIMULATOR))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def simple_commands(shell: str, nested: bool = False) -> list[list[str]]:
    """Split a shell line into the argv of each simple command it runs."""
    lexer = shlex.shlex(shell, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    commands: list[list[str]] = [[]]
    for token in lexer:
        if OPERATORS.match(token):
            commands.append([])
        else:
            commands[-1].append(token)
    unwrapped: list[list[str]] = []
    for argv in commands:
        unwrapped.extend(unwrap(argv, nested))
    return unwrapped


def drop_flags(argv: list[str]) -> list[str]:
    while argv and argv[0].startswith("-"):
        argv = argv[1:]
    return argv


def unwrap(argv: list[str], nested: bool) -> list[list[str]]:
    """Return the command or commands an argv really runs, per the module docstring."""
    while argv:
        head = argv[0]
        if ASSIGNMENT.match(head) or head in TRANSPARENT:
            argv = argv[1:]
        elif head == "env":
            argv = drop_flags(argv[1:])
        elif argv[:2] == ["uv", "run"]:
            argv = drop_flags(argv[2:])
        else:
            break
    if not argv:
        return []
    argv = [Path(argv[0]).name, *argv[1:]]
    if argv[0] in SHELLS and not nested:
        for index, token in enumerate(argv[1:-1], start=1):
            if SHELL_COMMAND_FLAG.match(token):  # -c, -lc, -ec ...
                return simple_commands(argv[index + 1], nested=True)
    return [argv]


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


def describe(policy: dict[str, Any], skill: str) -> str:
    lines = [f"Blocked by the `{skill}` skill's command policy `{policy['id']}`."]
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


def blocking_policy(shell: str) -> tuple[dict[str, Any], str] | None:
    simulator = load_simulator()
    catalog = {(entry["skill"], entry["id"]): entry for entry in simulator.policy_catalog()}
    for argv in simple_commands(shell):
        match = simulator.primary_match(argv, shell)
        if match is not None:
            return catalog[(match.skill, match.policy_id)], match.skill
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if event.get("tool_name") != "Bash":
            return 0
        shell = event["tool_input"]["command"]
        blocked = blocking_policy(shell)
    except Exception as error:  # noqa: BLE001 - fail open, see module docstring
        print(f"command policy hook skipped: {error!r}", file=sys.stderr)
        return 0
    if blocked is None:
        return 0
    print(describe(*blocked), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
