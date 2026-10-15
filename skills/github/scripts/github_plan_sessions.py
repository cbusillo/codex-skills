# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Bounded native ownership reads; never start, resume, or interrupt a session."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys


def native_helpers():
    directory = str(Path(__file__).resolve().parents[2] / "supervisor/scripts")
    if directory not in sys.path:
        sys.path.append(directory)
    import account_choice
    import session_record
    return account_choice, session_record


def codex_homes() -> list[Path]:
    homes = [Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))]
    try:
        config = native_helpers()[0].load_config()
        homes.extend(Path(a["env"]["CODEX_HOME"]) for a in config["accounts"] if a["provider"] == "openai")
    except (ValueError, OSError, ImportError):
        pass  # Missing optional account configuration never establishes absence.
    return list(dict.fromkeys(p.expanduser().resolve() for p in homes))


async def observe_home(home: Path, sessions: set[str]) -> tuple[list[dict], dict[str, dict]]:
    """Read the existing stock daemon over its native socket; only read RPCs."""
    import aiohttp
    import stat
    socket_path = (home / "app-server-control/app-server-control.sock").resolve(strict=True)
    info = socket_path.stat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("unsafe native session socket")
    peers, states = [], {}
    sequence = 0

    async def request(method, params):
        nonlocal sequence
        sequence += 1
        await ws.send_json({"id": sequence, "method": method, "params": params})
        while True:
            message = await ws.receive_json()
            if message.get("id") == sequence and "method" not in message:
                if "error" in message:
                    raise ValueError("native session read rejected")
                return message["result"]

    async def inspect(session):
        thread = (await request("thread/read", {"threadId": session, "includeTurns": False}))["thread"]
        if thread.get("id") != session:
            raise ValueError("native session identity mismatch")
        state = thread.get("status", {}).get("type")
        if state == "active":
            peers.append({"sessionId": session, "cwd": thread["cwd"], "state": "active", "source": "codex_session"})
            states[session] = {"state": "live", "source": "codex_app_server"}
        elif session in sessions and state in {"idle", "notLoaded"} and thread.get("path"):
            before = native_helpers()[1].session_status(
                {"session_id": session, "harness": "codex", "transcript": thread["path"]})
            # Re-read both native state and transcript; idle alone is not ended.
            after = (await request("thread/read", {"threadId": session, "includeTurns": False}))["thread"]
            verified = native_helpers()[1].session_status(
                {"session_id": session, "harness": "codex", "transcript": thread["path"]})
            if (before == verified and before["safe_verdict"] and after.get("id") == session
                    and after.get("status", {}).get("type") in {"idle", "notLoaded"}):
                states[session] = {"state": "ended", "source": "codex_app_server", "ended_at": before["timestamp"]}
            elif after.get("status", {}).get("type") == "active":
                peers.append({"sessionId": session, "cwd": after["cwd"], "state": "active", "source": "codex_session"})
                states[session] = {"state": "live", "source": "codex_app_server"}

    try:
        async with asyncio.timeout(5):
            async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(socket_path))) as client:
                async with client.ws_connect("http://localhost/", max_msg_size=1024 * 1024) as ws:
                    await request("initialize", {"clientInfo": {"name": "codex_skills_claim", "version": "1"},
                                                 "capabilities": {"experimentalApi": True}})
                    await ws.send_json({"method": "initialized"})
                    cursor, loaded = None, set()
                    for _ in range(10):
                        page = await request("thread/loaded/list", {"cursor": cursor, "limit": 100})
                        loaded.update(page["data"])
                        cursor = page.get("nextCursor")
                        if not cursor:
                            break
                    for session in [*sorted(sessions), *sorted(loaded - sessions)]:
                        try:
                            await inspect(session)
                        except (KeyError, TypeError, ValueError, OSError, ImportError):
                            continue  # No successful observation means unknown, never ended.
    except (KeyError, TypeError, ValueError, OSError, TimeoutError, aiohttp.ClientError):
        pass
    return peers, states


def process_sessions(text: str, sessions: set[str]) -> list[dict]:
    peers = []
    for line in text.splitlines():
        argv = line.split()
        if not argv or Path(argv[0]).name != "codex":
            continue
        resumed = {argv[i + 1].strip("\"'") for i, arg in enumerate(argv[:-1]) if arg in {"resume", "--resume", "-r"}}
        for session in resumed & sessions:
            peers.append({"sessionId": session, "source": "codex_process", "state": "live"})
    return peers


def observe(sessions: set[str]) -> tuple[list[dict], dict[str, dict]]:
    if not shutil.which("codex"):
        return [], {}

    async def run():
        return await asyncio.gather(*(observe_home(home, sessions) for home in codex_homes()), return_exceptions=True)

    peers, states = [], {}
    try:
        process_read = subprocess.run(["ps", "-axo", "args="], capture_output=True, text=True, timeout=5)
        if process_read.returncode == 0:
            peers.extend(process_sessions(process_read.stdout, sessions))
    except (OSError, subprocess.SubprocessError):
        pass
    for result in asyncio.run(run()):
        if isinstance(result, BaseException):
            continue
        observed, outcomes = result
        peers.extend(observed)
        for session, outcome in outcomes.items():
            if states.get(session, {}).get("state") != "live":
                states[session] = outcome
    for peer in peers:
        states[peer["sessionId"]] = {"state": "live", "source": peer["source"]}
    return peers, states
