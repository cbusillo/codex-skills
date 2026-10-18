# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
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
import signal
import subprocess
import time
import tomllib
from collections.abc import Mapping
from pathlib import Path

CHROME_SERVER = "catalog-chrome"
BUILTIN_CHROME = "claude-in-chrome"
COMPUTER_USE = "computer-use"
# Documented: 0 starts a session with the built-in Chrome server off; it outranks the setting.
CHROME_ENV = "CLAUDE_CODE_ENABLE_CFC"
LIST_TOOL = "list_connected_browsers"
CODEX_PLUGINS = {"chrome", "browser", "computer-use"}
CODEX_BROWSER_PLUGINS = CODEX_PLUGINS - {COMPUTER_USE}
BUNDLED_MARKETPLACE = Path(".tmp/bundled-marketplaces/openai-bundled")
CODEX_SOURCE_ENV = "CODEX_CATALOG_BUNDLED_SOURCE"


def missing_codex_plugins(plugins: list[dict], required: set[str] | None = None) -> list[str]:
    enabled = {plugin.get("name") for plugin in plugins
               if isinstance(plugin, dict) and plugin.get("installed") is True and plugin.get("enabled") is True
               and plugin.get("marketplaceName") == "openai-bundled"}
    return sorted((CODEX_PLUGINS if required is None else required) - enabled)


def daemon_codex_plugins(inventory: dict) -> list[dict]:
    """The native protocol puts marketplace identity on the containing object."""
    return [{**plugin, "marketplaceName": marketplace.get("name")}
            for marketplace in inventory.get("marketplaces", [])
            for plugin in marketplace.get("plugins", [])]


def codex_problems(home: Path, env: Mapping[str, str] | None = None) -> list[str]:
    """Check on-disk setup for a direct start; launch overrides need live discovery."""
    try:
        config = tomllib.loads((home / "config.toml").read_text())
    except (OSError, ValueError) as error:
        return [f"cannot read Codex config ({type(error).__name__}); check this account's setup"]
    marketplaces = config.get("marketplaces", {})
    marketplace = marketplaces.get("openai-bundled", {}) if isinstance(marketplaces, dict) else {}
    source = marketplace.get("source") if isinstance(marketplace, dict) else None
    source = (env or {}).get(CODEX_SOURCE_ENV, source)
    expected = home / BUNDLED_MARKETPLACE
    if source != str(expected) or not expected.is_dir():
        return ["Static setup cannot verify this session's bundled marketplace; a remote daemon may use its own config. "
                "use the Supervisor local codex --no-daemon route with its account-relative source override; "
                "--remote and -p do not forward marketplace settings"]
    return []


def codex_plugins(home: Path, source: Path, directory: Path | None = None) -> list[dict]:
    """Read discovery for the same home and process overrides as a local launch."""
    override = "marketplaces.openai-bundled.source=" + json.dumps(str(source))
    try:
        completed = subprocess.run(
            ["codex", "plugin", "list", "--marketplace", "openai-bundled", "--json", "-c", override],
            env={**os.environ, "CODEX_HOME": str(home)}, cwd=directory,
            capture_output=True, text=True, timeout=15,
        )
        if completed.returncode:
            raise ValueError("plugin/list failed; check this Codex account's bundled marketplace")
        inventory = json.loads(completed.stdout)
        installed = inventory.get("installed") if isinstance(inventory, dict) else None
        if not isinstance(installed, list):
            raise ValueError("plugin/list returned no installed plugin inventory")
        return installed
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        raise ValueError("Codex plugin/list unavailable: " + type(error).__name__) from None


def claude_config_path(home: Path | None) -> Path:
    """The default home keeps its config beside it, alternate homes inside it."""
    return Path.home() / ".claude.json" if home is None else home / ".claude.json"


def read_claude_config(home: Path | None) -> dict:
    path = claude_config_path(home)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read this Claude home's config ({type(error).__name__})") from None
    if not isinstance(config, dict):
        raise ValueError("this Claude home's config is not a JSON object")
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


def claude_problems(home: Path | None, directory: Path, env: Mapping[str, str],
                    *, check_computer_use: bool = True) -> list[str]:
    """Static checks, fast enough for every session start; no process is started."""
    try:
        config = read_claude_config(home)
    except ValueError as error:
        return [str(error)]
    project = project_entry(config, directory)
    problems = []
    servers = config.get("mcpServers")
    server = servers.get(CHROME_SERVER) if isinstance(servers, dict) else None
    if not isinstance(server, dict) or not isinstance(server.get("command"), str):
        problems.append(f"no {CHROME_SERVER} server in this Claude home; install it with "
                        "scripts/install-catalog.py (README: Chrome across Claude account homes)")
    elif CHROME_SERVER in listed(project, "disabledMcpServers"):
        problems.append(f"{CHROME_SERVER} is disabled for the launch directory; enable the pinned server through /mcp")
    if builtin_chrome_on(config, project, env):
        problems.append(f"the built-in Claude in Chrome server is on and uses this session's own login, "
                        f"which cannot reach the Agents browser; use mcp__{CHROME_SERVER}__* tools, "
                        f"and start sessions with {CHROME_ENV}=0 and without --chrome")
    if check_computer_use and (COMPUTER_USE not in listed(project, "enabledMcpServers")
                              or COMPUTER_USE in listed(project, "disabledMcpServers")):
        problems.append("Computer Use is off for the launch directory in this Claude home "
                        "(projects[...].enabledMcpServers in .claude.json)")
    return problems


def chrome_server(home: Path | None) -> dict:
    config = read_claude_config(home)
    servers = config.get("mcpServers")
    server = servers.get(CHROME_SERVER) if isinstance(servers, dict) else None
    if not isinstance(server, dict) or not isinstance(server.get("command"), str):
        raise ValueError(f"no {CHROME_SERVER} server in this Claude home; install it with "
                         "scripts/install-catalog.py (README: Chrome across Claude account homes)")
    return server


def connected_browsers(server: dict, timeout: float = 15.0) -> list[dict]:
    """Start the configured server once and call only its browser listing."""
    args, extra_env = server.get("args", []), server.get("env") or {}
    if (not isinstance(args, list) or not all(isinstance(arg, str) for arg in args)
            or not isinstance(extra_env, dict)):
        raise ValueError(f"{CHROME_SERVER} has invalid args or env")
    argv = [server["command"], *args]
    env = {**os.environ, **{key: str(value) for key, value in extra_env.items()}}
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=env,
                                   start_new_session=True)
    except OSError as error:
        raise ValueError(f"{CHROME_SERVER} did not start ({type(error).__name__})") from None
    deadline = time.monotonic() + timeout
    stdin, stdout = process.stdin, process.stdout
    assert stdin is not None and stdout is not None
    buffer = b""

    def cleanup_refused() -> None:
        # Waiting does not retry a refused signal. Reap an exited child, bounded
        # even if the process cannot exit; the launcher must still report refusal.
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        raise ValueError(f"{CHROME_SERVER} probe cleanup refused: PermissionError; no signal retry") from None

    def send(message: dict) -> None:
        stdin.write((json.dumps(message) + "\n").encode())
        stdin.flush()

    def call(message_id: int, method: str, params: dict) -> dict:
        nonlocal buffer
        send({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params})
        while (remaining := deadline - time.monotonic()) > 0:
            if b"\n" not in buffer:
                ready, _, _ = select.select([stdout], [], [], remaining)
                if not ready:
                    break
                chunk = os.read(stdout.fileno(), 65536)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > 1024 * 1024:
                    raise ValueError(f"{CHROME_SERVER} reply exceeded 1 MiB")
                continue
            line, buffer = buffer.split(b"\n", 1)
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and "method" not in message and message.get("id") == message_id:
                if "error" in message or not isinstance(message.get("result"), dict):
                    raise ValueError(f"{CHROME_SERVER} {method} failed")
                return message
        raise ValueError(f"{CHROME_SERVER} gave no {method} reply within {timeout:g}s")

    try:
        call(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "agent-tools-check", "version": "1"}})
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        reply = call(2, "tools/call", {"name": LIST_TOOL, "arguments": {}})
    except (OSError, ValueError) as error:
        raise ValueError(str(error) if isinstance(error, ValueError) else
                         f"{CHROME_SERVER} closed early ({type(error).__name__})") from None
    finally:
        # The native entry can spawn a child. Reap this probe's process group,
        # including a child holding stdout after its parent exits.
        try:
            stdin.close()
        except OSError:
            pass
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            stdout.close()
            cleanup_refused()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                cleanup_refused()
            process.wait()
        finally:
            stdout.close()
    if reply["result"].get("isError"):
        raise ValueError(f"{CHROME_SERVER} {LIST_TOOL} failed; check the pinned account's connection")
    content = (reply.get("result") or {}).get("content") or []
    text = next((item.get("text") for item in content if isinstance(item, dict)), None)
    try:
        browsers = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        raise ValueError(f"{CHROME_SERVER} {LIST_TOOL} returned no browser list") from None
    if not isinstance(browsers, list) or not all(isinstance(browser, dict) for browser in browsers):
        raise ValueError(f"{CHROME_SERVER} {LIST_TOOL} returned no browser list")
    return browsers
