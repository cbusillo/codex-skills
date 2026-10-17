"""Check that a Claude session gets one Chrome route and Computer Use.

Chrome reaches the Agents browser only through the catalog's `catalog-chrome`
server, whose child runs in the home signed in to the extension's account. The
built-in Claude in Chrome server uses the session's own login instead, so it
cannot pair when that login differs (codex-skills#2119). Computer Use is a
built-in server that Claude Code enables per home and per project.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

CHROME_SERVER = "catalog-chrome"
BUILTIN_CHROME = "claude-in-chrome"
COMPUTER_USE = "computer-use"
# Documented: 0 starts a session with the built-in Chrome server off; it outranks the setting.
CHROME_ENV = "CLAUDE_CODE_ENABLE_CFC"
LIST_TOOL = "list_connected_browsers"


def claude_config_path(home: Path | None) -> Path:
    """The default home keeps its config beside it, alternate homes inside it."""
    return Path.home() / ".claude.json" if home is None else home / ".claude.json"


def read_claude_config(home: Path | None) -> dict:
    path = claude_config_path(home)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read {path} ({type(error).__name__})") from None
    if not isinstance(config, dict):
        raise ValueError(f"{path} is not a JSON object")
    return config


def project_entry(config: dict, directory: Path) -> dict:
    projects = config.get("projects")
    entry = projects.get(str(directory)) if isinstance(projects, dict) else None
    return entry if isinstance(entry, dict) else {}


def listed(entry: dict, key: str) -> list:
    value = entry.get(key)
    return value if isinstance(value, list) else []


def builtin_chrome_on(config: dict, project: dict, env: Mapping[str, str]) -> bool:
    if env.get(CHROME_ENV) in {"0", "1"}:
        return env[CHROME_ENV] == "1"
    return config.get("claudeInChromeDefaultEnabled") is True and BUILTIN_CHROME not in listed(
        project, "disabledMcpServers")


def claude_problems(home: Path | None, directory: Path, env: Mapping[str, str]) -> list[str]:
    """Static checks, fast enough for every session start; no process is started."""
    try:
        config = read_claude_config(home)
    except ValueError as error:
        return [str(error)]
    project = project_entry(config, directory)
    problems = []
    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or CHROME_SERVER not in servers:
        problems.append(f"no {CHROME_SERVER} server in this Claude home; install it with "
                        "scripts/install-catalog.py (README: Chrome across Claude account homes)")
    if builtin_chrome_on(config, project, env):
        problems.append(f"the built-in Claude in Chrome server is on and uses this session's own login, "
                        f"which cannot reach the Agents browser; use mcp__{CHROME_SERVER}__* tools, "
                        f"and start sessions with {CHROME_ENV}=0 and without --chrome")
    if COMPUTER_USE not in listed(project, "enabledMcpServers"):
        problems.append(f"Computer Use is off for {directory} in this Claude home "
                        "(projects[...].enabledMcpServers in .claude.json)")
    return problems


def chrome_server(home: Path | None) -> dict:
    config = read_claude_config(home)
    servers = config.get("mcpServers")
    server = servers.get(CHROME_SERVER) if isinstance(servers, dict) else None
    if not isinstance(server, dict) or not isinstance(server.get("command"), str):
        raise ValueError(f"no {CHROME_SERVER} server in {claude_config_path(home)}; install it with "
                         "scripts/install-catalog.py (README: Chrome across Claude account homes)")
    return server


def connected_browsers(server: dict, timeout: float = 45.0) -> list[dict]:
    """Start the configured server once and call only its browser listing."""
    argv = [server["command"], *[str(arg) for arg in server.get("args", [])]]
    env = {**os.environ, **{key: str(value) for key, value in (server.get("env") or {}).items()}}
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, env=env,
                                   start_new_session=True)
    except OSError as error:
        raise ValueError(f"{CHROME_SERVER} did not start ({type(error).__name__})") from None
    deadline = time.monotonic() + timeout
    stdin, stdout = process.stdin, process.stdout
    assert stdin is not None and stdout is not None

    def call(message_id: int, method: str, params: dict) -> dict:
        stdin.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "method": method,
                                        "params": params}) + "\n")
        stdin.flush()
        while (remaining := deadline - time.monotonic()) > 0:
            ready, _, _ = select.select([stdout], [], [], remaining)
            if not ready:
                break
            line = stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == message_id:
                return message
        raise ValueError(f"{CHROME_SERVER} gave no {method} reply within {timeout:g}s")

    try:
        call(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "agent-tools-check", "version": "1"}})
        stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        reply = call(2, "tools/call", {"name": LIST_TOOL, "arguments": {}})
    except (OSError, ValueError) as error:
        raise ValueError(str(error) if isinstance(error, ValueError) else
                         f"{CHROME_SERVER} closed early ({type(error).__name__})") from None
    finally:
        try:
            stdin.close()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()
    content = (reply.get("result") or {}).get("content") or []
    text = next((item.get("text") for item in content if isinstance(item, dict)), None)
    try:
        browsers = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        raise ValueError(f"{CHROME_SERVER} {LIST_TOOL} returned no browser list: {str(text)[:200]}") from None
    if not isinstance(browsers, list):
        raise ValueError(f"{CHROME_SERVER} {LIST_TOOL} returned no browser list")
    return browsers
