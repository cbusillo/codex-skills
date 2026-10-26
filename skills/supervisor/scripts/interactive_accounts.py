#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Generate interactive zsh functions and launch the real harness binary."""

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

import account_choice
import codex_auth_health

HARNESSES = {"claude": "anthropic", "codex": "openai"}


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


def remember(path, chosen):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(chosen, output)
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
    binary = shutil.which(harness)
    if binary is None:
        raise ValueError(f"real {harness} binary is not on PATH")
    child_env = dict(os.environ)
    child_env.pop(variable, None)
    child_env.update(chosen["env"])
    if variable in child_env:
        child_env[variable] = str(Path(child_env[variable]).expanduser())
    if harness == "codex" and arguments[:1] not in (["login"], ["logout"]):
        codex_auth_health.require_usable(Path(child_env.get(variable, str(Path.home() / ".codex"))))
    try:
        # Last-used state contains launch settings only, never login material.
        remember(path, {"name": chosen["name"], "env": {variable: child_env[variable]} if variable in child_env else {}})
    except OSError:
        banner = banner[:-1] + "; could not save last account)"
    print(banner, file=sys.stderr, flush=True)
    os.execvpe(binary, [binary, *arguments], child_env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("variants")
    start = subparsers.add_parser("launch")
    start.add_argument("harness", choices=HARNESSES)
    start.add_argument("--named")
    args, argv = parser.parse_known_args()
    try:
        if args.action == "variants":
            try:
                config = account_choice.load_config()
            except ValueError:
                config = None
            print(variants(config))
        else:
            launch(args.harness, argv[1:] if argv[:1] == ["--"] else argv, args.named)
    except (OSError, ValueError) as error:
        parser.exit(1, f"interactive account launch refused: {error}\n")


if __name__ == "__main__":
    main()
