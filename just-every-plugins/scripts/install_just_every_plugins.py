#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""Install or inspect the codex-skills Just Every plugin marketplace."""

from __future__ import annotations

from pathlib import Path
import argparse
import shutil
import subprocess
import sys


MARKETPLACE_NAME = "codex-skills-just-every"
PLUGIN_NAMES = ("ultracode", "auto-review")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install or inspect Just Every Codex plugins from codex-skills."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser(
        "install", help="Add the local marketplace and install the plugins."
    )
    install.add_argument(
        "--codex",
        default="codex",
        help="Codex CLI command to run.",
    )
    install.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without running them.",
    )
    install.add_argument(
        "--plugin",
        action="append",
        choices=PLUGIN_NAMES,
        help="Install only this plugin. May be passed more than once.",
    )

    status = subparsers.add_parser(
        "status", help="Show marketplace and plugin state from Codex."
    )
    status.add_argument(
        "--codex",
        default="codex",
        help="Codex CLI command to run.",
    )
    status.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without running them.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "install":
        return install(args)
    if args.command == "status":
        return status(args)
    raise AssertionError(f"unhandled command: {args.command}")


def install(args: argparse.Namespace) -> int:
    marketplace = marketplace_root()
    plugins = tuple(args.plugin or PLUGIN_NAMES)
    commands = [
        [args.codex, "plugin", "marketplace", "add", str(marketplace)],
    ]
    commands.extend(
        [args.codex, "plugin", "add", f"{plugin}@{MARKETPLACE_NAME}"]
        for plugin in plugins
    )
    return run_commands(commands, dry_run=args.dry_run)


def status(args: argparse.Namespace) -> int:
    commands = [
        [args.codex, "plugin", "marketplace", "list"],
        [
            args.codex,
            "plugin",
            "list",
            "--marketplace",
            MARKETPLACE_NAME,
            "--available",
            "--json",
        ],
    ]
    return run_commands(commands, dry_run=args.dry_run)


def marketplace_root() -> Path:
    skill_dir = Path(__file__).resolve().parents[1]
    root = skill_dir.parent
    path = root / ".agents" / "plugins" / "marketplace.json"
    if not path.is_file():
        raise FileNotFoundError(f"marketplace manifest not found: {path}")
    return root


def run_commands(commands: list[list[str]], *, dry_run: bool) -> int:
    if dry_run:
        for command in commands:
            print(shell_join(command))
        return 0

    for command in commands:
        if shutil.which(command[0]) is None:
            print(f"Command not found: {command[0]}", file=sys.stderr)
            return 1
        print(f"$ {shell_join(command)}")
        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError as exc:
            return exc.returncode
    return 0


def shell_join(command: list[str]) -> str:
    return " ".join(quote(arg) for arg in command)


def quote(value: str) -> str:
    if value and all(ch.isalnum() or ch in "@%_+=:,./-" for ch in value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


if __name__ == "__main__":
    raise SystemExit(main())
