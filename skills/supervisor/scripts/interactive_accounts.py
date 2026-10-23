#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Generate interactive zsh functions and launch the real harness binary."""

import argparse
import hashlib
import json
import os
import re
import runpy
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

import account_choice
from session_liveness import CLAUDE_RESUME_VALUE_OPTIONS

HARNESSES = {"claude": "anthropic", "codex": "openai"}
CLAUDE_SUBCOMMANDS = {
    "agents", "attach", "auth", "auto-mode", "daemon", "doctor", "gateway", "import", "install", "logs",
    "mcp", "plugin", "plugins", "purge", "remote-control", "respawn", "rm", "setup-token", "stop", "kill", "ultrareview",
    "update", "upgrade", "self-hosted-runner",
}
CLAUDE_VALUE_OPTIONS = CLAUDE_RESUME_VALUE_OPTIONS | {
    "--add-dir", "--advisor", "--agents", "--allowedTools", "--allowed-tools", "--channels",
    "--append-subagent-system-prompt", "--append-subagent-system-prompt-file", "--autocompact", "--betas",
    "--dangerously-load-development-channels", "--disallowedTools", "--disallowed-tools", "--environment", "--exec", "--fallback-model",
    "--file", "--json-schema", "--max-budget-usd", "--max-turns", "--mcp-config", "--name", "-n",
    "--permission-prompt-tool", "--permission-prompts", "--plugin-dir", "--plugin-url", "--ref",
    "--remote-control-session-name-prefix", "--session-id",
    "--teammate-mode", "--tools",
}
CLAUDE_DISPATCH_OPTIONS = {"--desktop", "--cloud", "--remote", "--environment", "--exec", "--init-only"}


def prepare_claude_arguments(arguments: list[str], directory: Path) -> list[str]:
    if arguments and arguments[0] in CLAUDE_SUBCOMMANDS:
        return arguments
    daemon = list(arguments)
    while daemon and daemon[0] in {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"}:
        daemon.pop(0)
    if daemon[:1] == ["daemon"]:
        return arguments
    prepared, custom_files = [], []
    tail = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            tail = arguments[index:]
            break
        option, equals, value = argument.partition("=")
        if option in CLAUDE_DISPATCH_OPTIONS or option in {"--help", "-h", "--version", "-v"}:
            return arguments
        tokens = [argument]
        if option in CLAUDE_VALUE_OPTIONS:
            if not equals:
                index += 1
                if index == len(arguments):
                    raise ValueError(f"Claude {option} requires a value")
                value = arguments[index]
                tokens.append(value)
            if option == "--append-system-prompt-file":
                if not value:
                    raise ValueError(f"Claude {option} requires a file path")
                custom_files.append(value)
            elif option != "--system-prompt-snapshot":
                prepared.extend(tokens)
        else:
            prepared.extend(tokens)
        index += 1
    renderer_path = Path(__file__).resolve().parents[3] / "hooks/direction_check_hook.py"
    try:
        renderer = runpy.run_path(str(renderer_path))
        content = renderer["direction_content"](directory, include_loop=False)
    except Exception as error:
        raise ValueError(f"cannot prepare Claude direction from {renderer_path}: {error}") from error
    if not content:
        return arguments
    cache = Path(os.environ.get("CODE_HOME", str(Path.home() / ".code"))) / "skill-data/direction-prompts"
    custom_content = []
    for custom in custom_files:
        custom_path = directory / Path(custom).expanduser()
        try:
            if custom_path.resolve().is_relative_to(cache.expanduser().resolve()):
                continue
            custom_content.append(custom_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as error:
            raise ValueError(f"cannot read Claude custom prompt {custom_path}: {error}") from error
    content = "\n\n".join([*custom_content, content])
    path = (cache.expanduser() / f"{hashlib.sha256(content.encode()).hexdigest()}.md").absolute()
    try:
        write_cache(path, content)
    except OSError as error:
        raise ValueError(f"cannot write Claude direction prompt {path}: {error}") from error
    return [*prepared, "--append-system-prompt-file", str(path), "--system-prompt-snapshot", "off", *tail]


def exec_native(binary: str, arguments: list[str], environment: dict[str, str]) -> None:
    executable = shutil.which(binary, path=environment.get("PATH"))
    if executable is None:
        raise ValueError(f"real {binary} binary is not on PATH")
    os.environ.clear()
    os.environ.update(environment)
    if Path(binary).name == "claude":
        arguments = prepare_claude_arguments(arguments, Path.cwd())
    os.execvpe(executable, [executable, *arguments], environment)


def variants(config=None):
    """Use the Supervisor's Context Panel mappings; never keep another account list."""
    helper = shlex.quote(str(Path(__file__).resolve()))
    lines = []
    for harness, provider in HARNESSES.items():
        lines.append(f'{harness}() {{ command uv run --quiet --no-project --script {helper} launch {harness} -- "$@"; }}')
        for account in (config or {}).get("accounts", []):
            if account["provider"] != provider:
                continue
            name = account["name"]
            # Function names must be usable literally at the terminal, and cannot be code.
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", name):
                raise ValueError("account names for shell variants must contain letters, digits, '_' or '-'")
            function = name if name.startswith(harness + "-") else f"{harness}-{name}"
            lines.append(f'{function}() {{ command uv run --quiet --no-project --script {helper} launch {harness} --named {shlex.quote(name)} -- "$@"; }}')
    return "\n".join(lines)


def account_argument(argv):
    """Consume only our account selector, leaving native arguments byte-for-byte intact."""
    remaining, name = [], None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            remaining.extend(argv[index:])
            break
        if arg == "--account":
            index += 1
            if index == len(argv) or not argv[index]:
                raise ValueError("--account needs a configured account name")
            name = argv[index]
        elif arg.startswith("--account="):
            name = arg.split("=", 1)[1]
            if not name:
                raise ValueError("--account needs a configured account name")
        else:
            remaining.append(arg)
        index += 1
    return name, remaining


def state_path(provider):
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "codex-skills" / f"interactive-{provider}.json"


def read_last(path, variable):
    try:
        last = json.loads(path.read_text())
        if (isinstance(last, dict) and isinstance(last.get("name"), str)
                and isinstance(last.get("env"), dict)
                and set(last["env"]) <= {variable}
                and all(isinstance(v, str) and v for v in last["env"].values())):
            return last
    except (OSError, ValueError):
        pass
    return None


def write_cache(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def selection(harness, argv, named=None, env=None, config=None, snapshot=None, problem=None, last=None):
    env = os.environ if env is None else env
    provider = HARNESSES[harness]
    variable = account_choice.ACCOUNT_VARIABLES[provider]
    flag, argv = account_argument(argv)
    name = named or flag
    if name:
        accounts = [a for a in (config or {}).get("accounts", [])
                    if a["provider"] == provider and a["name"] == name]
        if len(accounts) != 1:
            raise ValueError(f"no configured {harness} account named {name}")
        chosen = {"name": name, "env": accounts[0]["env"]}
        reason = "named account" if named else "explicit --account"
    elif env.get(variable):
        accounts = [a for a in (config or {}).get("accounts", [])
                    if a["provider"] == provider and a["env"].get(variable) == env[variable]]
        chosen = {"name": accounts[0]["name"] if len(accounts) == 1 else "explicit",
                  "env": {variable: env[variable]}}
        reason = f"explicit {variable}"
    else:
        try:
            if config is None:
                raise ValueError(problem or "account configuration unavailable")
            choice = account_choice.choose(provider, config, snapshot, problem)
            chosen = {"name": choice["name"], "env": choice["env"]}
            reason = "Context Panel next" if choice["source"] == "context-panel" else choice["reason"]
        except ValueError:
            chosen = last or {"name": "default", "env": {}}
            reason = "Context Panel unavailable; falling back to last account" if last else "Context Panel unavailable; no last account, using default"
    label = chosen["name"]
    if not label.startswith(harness + "-"):
        label = f"{harness}-{label}"
    return chosen, argv, f"using {label} ({reason})"


def launch(harness, argv, named=None):
    provider = HARNESSES[harness]
    variable = account_choice.ACCOUNT_VARIABLES[provider]
    flag, _ = account_argument(argv)
    config, snapshot, problem = None, None, None
    try:
        config = account_choice.load_config()
    except ValueError as error:
        problem = str(error)
    if config and not (named or flag or os.environ.get(variable)):
        snapshot, problem = account_choice.read_snapshot(config["snapshot_command"])
    path = state_path(provider)
    last = read_last(path, variable) if path.exists() else None
    chosen, arguments, banner = selection(harness, argv, named, config=config,
                                          snapshot=snapshot, problem=problem, last=last)
    child_env = dict(os.environ)
    child_env.pop(variable, None)
    child_env.update(chosen["env"])
    if variable in child_env:
        child_env[variable] = str(Path(child_env[variable]).expanduser())
    try:
        # Last-used state contains launch settings only, never login material.
        write_cache(path, json.dumps({"name": chosen["name"], "env": {variable: child_env[variable]} if variable in child_env else {}}))
    except OSError:
        banner = banner[:-1] + "; could not save last account)"
    print(banner, file=sys.stderr, flush=True)
    exec_native(harness, arguments, child_env)


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("variants")
    start = subparsers.add_parser("launch", allow_abbrev=False)
    start.add_argument("harness", choices=HARNESSES)
    start.add_argument("--named")
    native = subparsers.add_parser("native", allow_abbrev=False)
    native.add_argument("arguments", nargs=argparse.REMAINDER)
    args, argv = parser.parse_known_args()
    try:
        if args.action == "variants":
            try:
                config = account_choice.load_config()
            except ValueError:
                config = None
            print(variants(config))
        elif args.action == "native":
            arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
            if not arguments:
                raise ValueError("native launch requires a binary")
            exec_native(arguments[0], arguments[1:], dict(os.environ))
        else:
            launch(args.harness, argv[1:] if argv[:1] == ["--"] else argv, args.named)
    except (OSError, ValueError) as error:
        parser.exit(1, f"native launch refused: {error}\n")


if __name__ == "__main__":
    main()
