"""Plan/install the catalog Chrome server using Claude's MCP management CLI.

Only MCP details are inspected; Claude owns reading/writing its user config.
The catalog never opens login files or a credential store.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import tomllib
from pathlib import Path

SERVER = "catalog-chrome"  # Claude reserves its built-in server name.
AUTH_OVERRIDES = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def home_path(value: str, home: Path) -> Path:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ValueError("Chrome homes must be nonempty single-line paths")
    path = home / value[2:] if value.startswith("~/") else home if value == "~" else Path(value)
    if not path.is_absolute():
        raise ValueError("Chrome homes must be absolute or start with ~/")
    if not path.is_dir() or (path.is_symlink() and path != home / ".claude"):
        raise ValueError(f"Chrome home must be an existing regular directory: {path}")
    return path


def server_entry(pinned: Path, command: str, home: Path | None = None) -> dict:
    home = home or Path.home()
    # An explicit CLAUDE_CONFIG_DIR=~/.claude uses a different login/config
    # file than the native default home. Unset it for the default account.
    default = pinned == home / ".claude"
    # Clear caller authentication overrides by name, without reading their values.
    unset = (*AUTH_OVERRIDES, "CLAUDE_CONFIG_DIR") if default else AUTH_OVERRIDES
    return {"type": "stdio", "command": "env",
            "args": [part for key in unset for part in ("-u", key)] + [command, "--claude-in-chrome-mcp"],
            "env": {"CLAUDE_CONFIG_DIR": str(pinned)}}


def run_mcp(command: str, directory: Path, args: list[str]):
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(directory))
    if directory == Path.home() / ".claude":
        env.pop("CLAUDE_CONFIG_DIR", None)
    return subprocess.run([command, "mcp", *args], env=env, capture_output=True,
                          text=True, timeout=45)


def matches_details(text: str, entry: dict) -> bool:
    """Compare the supported CLI's MCP-only projection, ignoring connection status."""
    lines = text.splitlines()
    required = [f"{SERVER}:", "  Scope: User config (available in all your projects)",
                "  Type: stdio", f"  Command: {entry['command']}",
                f"  Args: {' '.join(entry['args'])}", "  Environment:"]
    env_lines = [line for line in lines if line.startswith("    ")]
    return all(line in lines for line in required) and env_lines == [
        f"    {key}={value}" for key, value in entry["env"].items()
    ]


def inspect_entry(command: str, directory: Path, desired: dict, previous: dict | None) -> str:
    result = run_mcp(command, directory, ["get", SERVER])
    if result.returncode:
        # Never interpret a timeout, rejected config or failed CLI as absence.
        if result.returncode == 1 and (result.stdout + result.stderr).startswith(f'No MCP server named "{SERVER}".'):
            return "create"
        raise ValueError(f"Could not inspect Chrome MCP entry in {directory}")
    if matches_details(result.stdout, desired):
        return "current"
    if previous and matches_details(result.stdout, previous):
        return "update"
    raise ValueError(f"Existing Chrome MCP entry preserved in {directory}; inspect it with claude mcp get {SERVER} before reconciling")


def prepare(root: Path, home: Path, claude: Path) -> dict | None:
    config_path = root / ".local" / "chrome.toml"
    if config_path.is_symlink() or (config_path.exists() and not config_path.is_file()):
        raise ValueError("Chrome private config must be a regular file")
    if not config_path.exists():
        return None  # Opt-in; a catalog pull never configures an account.
    config = tomllib.loads(config_path.read_text())
    if set(config) != {"pinned_home", "homes"} or not isinstance(config["homes"], list) or not config["homes"]:
        raise ValueError("chrome.toml requires pinned_home and a nonempty homes list")
    pinned = home_path(config["pinned_home"], home)
    # The outer installer resolves destinations for its bindings. Restore the
    # native default spelling, whose user config lives outside that directory.
    if claude == (home / ".claude").resolve():
        claude = home / ".claude"
    directories = list(dict.fromkeys([home_path(str(claude), home), pinned,
                                     *(home_path(value, home) for value in config["homes"])]))
    command = "claude"
    desired = server_entry(pinned, command, home)
    receipt_path = root / ".local" / "chrome-install.json"
    if receipt_path.is_symlink() or (receipt_path.exists() and not receipt_path.is_file()):
        raise ValueError("Chrome installation receipt must be a regular file")
    previous = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
    if not isinstance(previous, dict):
        raise ValueError("Invalid Chrome installation receipt")
    entries = []
    for directory in directories:
        if directory.resolve().is_relative_to(root.resolve()):
            raise ValueError("Chrome configuration overlaps the catalog checkout")
        # lstat only: never open the file containing account identity.
        config_file = home / ".claude.json" if directory == home / ".claude" else directory / ".claude.json"
        if config_file.is_symlink() or (config_file.exists() and not config_file.is_file()):
            raise ValueError(f"Chrome user config must be a regular file: {config_file}")
        old = previous.get(str(directory))
        if old is not None:
            old_env = old.get("env") if isinstance(old, dict) else None
            old_pin = old_env.get("CLAUDE_CONFIG_DIR") if isinstance(old_env, dict) else None
            if not isinstance(old_pin, str) or not Path(old_pin).is_absolute() or old != server_entry(Path(old_pin), command, home):
                raise ValueError("Invalid managed Chrome MCP entry in receipt")
        entries.append({"path": directory, "state": inspect_entry(command, directory, desired, old), "previous": old})
    return {"command": command, "desired": desired, "entries": entries, "receipt_path": receipt_path,
            "receipt": previous}


def apply(plan: dict) -> list[dict]:
    """Recheck each destination, use native management, and record successful homes."""
    command, desired = plan["command"], plan["desired"]
    for entry in plan["entries"]:
        directory, previous = entry["path"], entry["previous"]
        state = inspect_entry(command, directory, desired, previous)
        if state == "update":
            removed = run_mcp(command, directory, ["remove", SERVER, "--scope", "user"])
            if removed.returncode:
                raise ValueError(f"Could not remove managed Chrome entry in {directory}")
        if state != "current":
            added = run_mcp(command, directory, ["add-json", SERVER, json.dumps(desired), "--scope", "user"])
            if added.returncode:
                if state == "update":
                    restored = run_mcp(command, directory, ["add-json", SERVER, json.dumps(previous), "--scope", "user"])
                    if restored.returncode:
                        raise ValueError(f"Chrome update and restoration failed in {directory}; restore the previous entry from {plan['receipt_path']}")
                raise ValueError(f"Could not install Chrome entry in {directory}; earlier homes may already be installed")
            if inspect_entry(command, directory, desired, previous) != "current":
                raise ValueError(f"Chrome entry readback failed in {directory}")
        plan["receipt"][str(directory)] = desired
        with tempfile.NamedTemporaryFile(mode="w", dir=plan["receipt_path"].parent, delete=False) as staged:
            staged.write(json.dumps(plan["receipt"], indent=2) + "\n")
        Path(staged.name).replace(plan["receipt_path"])
    return [{"path": str(entry["path"]), "state": entry["state"]} for entry in plan["entries"]]
