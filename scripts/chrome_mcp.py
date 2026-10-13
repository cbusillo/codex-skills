"""Plan/install the catalog Chrome server using Claude's MCP management CLI.

Only MCP details are inspected; Claude owns reading/writing its user config.
The catalog never opens login files or a credential store.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import tomllib
from pathlib import Path

SERVER = "catalog-chrome"  # Claude reserves its built-in server name.
AUTH_OVERRIDES = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
MCP_TIMEOUT = 45


def native_default(directory: Path) -> bool:
    return directory.name == ".claude" and directory.parent.resolve() == Path.home().resolve()


def home_path(value: str, home: Path) -> Path:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ValueError("Chrome homes must be nonempty single-line paths")
    path = home / value[2:] if value.startswith("~/") else home if value == "~" else Path(value)
    if not path.is_absolute():
        raise ValueError("Chrome homes must be absolute or start with ~/")
    if not path.is_dir() or (path.is_symlink() and not native_default(path)):
        raise ValueError(f"Chrome home must be an existing regular directory: {path}")
    return path


def server_entry(pinned: Path, command: str) -> dict:
    # An explicit CLAUDE_CONFIG_DIR=~/.claude uses a different login/config
    # file than the native default home. Unset it for the default account.
    default = native_default(pinned)
    # Clear caller authentication overrides by name, without reading their values.
    unset = (*AUTH_OVERRIDES, "CLAUDE_CONFIG_DIR") if default else AUTH_OVERRIDES
    return {"type": "stdio", "command": "env",
            "args": [part for key in unset for part in ("-u", key)] + [command, "--claude-in-chrome-mcp"],
            "env": {"CLAUDE_CONFIG_DIR": str(pinned)}}


def run_mcp(command: str, directory: Path, args: list[str]):
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(directory))
    if native_default(directory):
        env.pop("CLAUDE_CONFIG_DIR", None)
    # Each management call owns a fresh process group, including health-check
    # children. Never search for or kill other sessions' Claude processes.
    with subprocess.Popen([command, "mcp", *args], env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=MCP_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def managed_entry(entry: object) -> bool:
    """Accept the released PATH entry and our pinned-executable renderer only."""
    if not isinstance(entry, dict):
        return False
    env, args = entry.get("env"), entry.get("args")
    if not isinstance(env, dict) or not isinstance(args, list) or len(args) < 2:
        return False
    pinned, command = env.get("CLAUDE_CONFIG_DIR"), args[-2]
    return (isinstance(pinned, str) and Path(pinned).is_absolute()
            and isinstance(command, str) and (command == "claude" or Path(command).is_absolute())
            and entry == server_entry(Path(pinned), command))


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
    directories = list(dict.fromkeys([home_path(str(claude), home), pinned,
                                     *(home_path(value, home) for value in config["homes"])]))
    selected = shutil.which("claude")
    if not selected:
        raise ValueError("claude is required to enroll Chrome; put the intended CLI on PATH and rerun")
    # Preserve the selected launcher spelling (including a symlink); resolving
    # its target can change multicall/launcher semantics.
    command = str(Path(selected).absolute())
    desired = server_entry(pinned, command)
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
        config_file = Path.home().resolve() / ".claude.json" if native_default(directory) else directory / ".claude.json"
        if config_file.is_symlink() or (config_file.exists() and not config_file.is_file()):
            raise ValueError(f"Chrome user config must be a regular file: {config_file}")
        old = previous.get(str(directory))
        if old is not None:
            if not managed_entry(old):
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
        entry["state"] = state
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
