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


def transcript_snapshot(session: str, primary: str) -> dict[str, dict]:
    paths = {Path(primary).resolve()}
    for home in codex_homes():
        for root in (home / "sessions", home / "archived_sessions"):
            paths.update(path.resolve() for path in root.rglob(f"*{session}*.jsonl"))
    return {str(path): native_helpers()[1].session_status(
        {"session_id": session, "harness": "codex", "transcript": str(path)}) for path in paths}


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
        if state in {"active", "idle", "systemError"}:
            peers.append({"sessionId": session, "cwd": thread["cwd"], "state": "active", "source": "codex_session"})
            states[session] = {"state": "live", "source": "codex_app_server"}
        elif session in sessions and state == "notLoaded" and thread.get("path"):
            before = transcript_snapshot(session, thread["path"])
            # A final turn is insufficient: the thread must be unloaded, every
            # known native transcript stable, and process absence checked below.
            after = (await request("thread/read", {"threadId": session, "includeTurns": False}))["thread"]
            verified = transcript_snapshot(session, thread["path"])
            if (before == verified and all(s["safe_verdict"] for s in before.values()) and after.get("id") == session
                    and after.get("status", {}).get("type") == "notLoaded"):
                states[session] = {"state": "ended", "source": "codex_app_server",
                                   "ended_at": max(s["timestamp"] for s in before.values())}
            elif after.get("status", {}).get("type") in {"active", "idle", "systemError"}:
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
        names = [Path(arg).name.casefold() for arg in argv[:3]]
        executable = next((i for i, name in enumerate(names) if name.startswith("codex")), None)
        if executable is None:
            continue
        # Command text inside an exec prompt is not CLI syntax. Ambiguous
        # interactive invocations remain unknown rather than guessing an ID.
        index = executable + 1
        values = {"-c", "--config", "--remote", "-C", "--cd", "-p", "--profile", "-m", "--model",
                  "-i", "--image", "--add-dir", "-s", "--sandbox", "-a", "--ask-for-approval", "--enable", "--disable"}
        while index < len(argv) and argv[index].startswith("-") and argv[index] != "--":
            index += 2 if argv[index] in values else 1
        resumed = {argv[index + 1].strip("\"'")} if (
            index + 1 < len(argv) and argv[index] == "resume" and not argv[index + 1].startswith("-")) else set()
        for session in resumed & sessions:
            peers.append({"sessionId": session, "source": "codex_process", "state": "live"})
    return peers


def process_absence_known(text: str) -> bool:
    for line in text.splitlines():
        argv = line.split()
        names = [Path(arg).name.casefold() for arg in argv[:2]]
        if any(name.startswith("codex") for name in names):
            # App-server processes are accounted for by exact thread reads.
            # A TUI, --last/picker, wrapper or remote client cannot be ruled out.
            if len(argv) < 2 or argv[1] != "app-server":
                return False
    return True


def observe(sessions: set[str]) -> tuple[list[dict], dict[str, dict]]:
    if not shutil.which("codex"):
        return [], {}

    async def run():
        return await asyncio.gather(*(observe_home(home, sessions) for home in codex_homes()), return_exceptions=True)

    peers, states = [], {}
    absent = False
    try:
        process_read = subprocess.run(["ps", "-axo", "args="], capture_output=True, text=True, timeout=5)
        if process_read.returncode == 0:
            peers.extend(process_sessions(process_read.stdout, sessions))
            absent = process_absence_known(process_read.stdout)
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
    if not absent:
        states = {session: state for session, state in states.items() if state["state"] != "ended"}
    return peers, states
