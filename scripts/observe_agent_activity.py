#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Observe existing Codex daemons and Claude's supported session inventory."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import plistlib
import shutil
import stat
import sys
from pathlib import Path

import aiohttp

from agent_activity import ActivityStore, atomic_json, directory, home_key, native_id, account_id

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "supervisor" / "scripts"))
import account_choice  # noqa: E402

POLL_SECONDS = 30
LABEL = "com.codex-skills.agent-activity"
LIFECYCLE = {"thread/status/changed", "thread/started", "thread/closed", "turn/started", "turn/completed"}
CONTENT_NOTIFICATIONS = ["item/started", "item/completed", "item/agentMessage/delta",
                         "item/reasoning/textDelta", "item/reasoning/summaryTextDelta",
                         "item/commandExecution/outputDelta", "item/fileChange/outputDelta",
                         "turn/diff/updated", "turn/plan/updated", "rawResponseItem/completed"]


def accounts(config_path: Path | None = None) -> tuple[list[dict], Path]:
    config = account_choice.load_config(config_path)
    snapshot, problem = account_choice.read_snapshot(config["snapshot_command"])
    if problem or not snapshot:
        raise ValueError("Context Panel account snapshot unavailable")
    result = []
    homes = set()
    for configured in config["accounts"]:
        harness = {"openai": "codex", "anthropic": "claude"}.get(configured["provider"])
        if not harness:
            continue
        rows = [r for r in snapshot["accounts"] if isinstance(r, dict) and r.get("provider") == configured["provider"]]
        matches = account_choice.matching_rows(configured, rows)
        if len(matches) != 1 or not account_id(matches[0].get("id"), harness):
            raise ValueError("account configuration does not match one opaque snapshot row")
        key = home_key(harness, configured["env"])
        if key in homes:
            raise ValueError("multiple accounts select the same harness home")
        homes.add(key)
        result.append({"harness": harness, "accountID": matches[0]["id"], "env": configured["env"], "homeKey": key})
    return result, account_choice.storage_root(config["snapshot_command"])


class CodexObserver:
    """Subscriptions never send turn, tool, approval or configuration mutations."""
    def __init__(self, store: ActivityStore, account: str, home: Path):
        self.store, self.account, self.home = store, account, home
        self.pending: dict[int, asyncio.Future] = {}
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.sequence = 0
        self.joined: set[str] = set()
        self.active: dict[str, dict] = {}
        self.ws = None

    async def request(self, method: str, params: dict) -> dict:
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.ws.send_json({"id": request_id, "method": method, "params": params})
            async with asyncio.timeout(10):
                return await future
        finally:
            self.pending.pop(request_id, None)

    async def receive(self) -> None:
        try:
            async for frame in self.ws:
                if frame.type != aiohttp.WSMsgType.TEXT:
                    raise ValueError("unexpected observer frame")
                message = json.loads(frame.data)
                if not isinstance(message, dict):
                    continue
                future = self.pending.get(message.get("id")) if isinstance(message.get("id"), int) else None
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(ValueError("observer RPC rejected"))
                    else:
                        future.set_result(message.get("result", {}))
                elif "id" not in message and message.get("method") in LIFECYCLE:
                    # Unknown notifications and every server request are discarded.
                    # Never answer a request addressed to an interactive client.
                    self.queue.put_nowait(message)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ValueError("observer disconnected"))

    def emit(self, session: str, state: str, source: str, **fields) -> None:
        known = self.active.get(session, {})
        self.store.emit(self.account, "codex", session, state, source,
                        run=fields.get("run") or known.get("run"), parent=known.get("parent"),
                        attention=fields.get("attention"))

    async def inspect(self, session: str) -> None:
        result = await self.request("thread/read", {"threadId": session, "includeTurns": False})
        thread = result.get("thread", {})
        status = thread.get("status", {})
        source = thread.get("source", {})
        subagent = source.get("subAgent", {}) if isinstance(source, dict) else {}
        spawn = subagent.get("thread_spawn", {}) if isinstance(subagent, dict) else {}
        self.active.setdefault(session, {})["parent"] = native_id(spawn.get("parent_thread_id"))
        await self.status(session, status)

    async def join(self, session: str) -> None:
        if session in self.joined:
            return
        # Only a currently active, loaded thread is joined, with no overrides.
        await self.request("thread/resume", {"threadId": session, "excludeTurns": True})
        self.joined.add(session)
        page = await self.request("thread/turns/list", {"threadId": session, "limit": 1, "itemsView": "summary"})
        turns = page.get("data", [])
        if turns:
            await self.turn(session, turns[0])

    async def release(self, session: str) -> None:
        if session in self.joined:
            await self.request("thread/unsubscribe", {"threadId": session})
            self.joined.discard(session)

    async def status(self, session: str, status: dict) -> None:
        kind = status.get("type")
        if kind == "active":
            flags = status.get("activeFlags", [])
            attention = "approval" if "waitingOnApproval" in flags else "input" if "waitingOnUserInput" in flags else None
            self.active.setdefault(session, {})["state"] = "attention" if attention else "working"
            self.active[session]["attention"] = attention
            await self.join(session)
            if session in self.active:
                self.emit(session, "attention" if attention else "working", "codex-status", attention=attention)
        elif kind in ("idle", "notLoaded", "systemError"):
            # Stock sends idle before turn/completed. Keep the subscription until
            # a structured terminal turn arrives (poll catches a join/finish race).
            if session in self.joined:
                page = await self.request("thread/turns/list", {"threadId": session, "limit": 1, "itemsView": "summary"})
                for turn in page.get("data", []):
                    await self.turn(session, turn)
            else:
                self.emit(session, "quiet", "codex-status")
                self.active.pop(session, None)

    async def turn(self, session: str, turn: dict) -> None:
        run = native_id(turn.get("id"))
        if not run:
            return
        state = {"inProgress": "working", "completed": "completed", "failed": "failed", "interrupted": "cancelled"}.get(turn.get("status"))
        if not state:
            return
        if state == "working":
            self.active.setdefault(session, {})["run"] = run
            self.emit(session, state, "codex-turn", run=run)
        else:
            self.emit(session, state, "codex-turn", run=run)
            self.active.pop(session, None)
            await self.release(session)

    async def handle(self, message: dict) -> None:
        params = message.get("params", {})
        method = message["method"]
        if method == "thread/started":
            session = native_id(params.get("thread", {}).get("id"))
            if session:
                await self.inspect(session)
            return
        session = native_id(params.get("threadId"))
        if not session:
            return
        if method == "thread/status/changed":
            await self.status(session, params.get("status", {}))
        elif method in ("turn/started", "turn/completed"):
            await self.turn(session, params.get("turn", {}))
        elif method == "thread/closed":
            self.active.pop(session, None)
            self.joined.discard(session)  # Closure without an outcome expires to stale.

    async def poll(self) -> None:
        cursor = None
        while True:
            page = await self.request("thread/loaded/list", {"cursor": cursor, "limit": 100})
            for session in page.get("data", []):
                if native_id(session):
                    await self.inspect(session)
            cursor = page.get("nextCursor")
            if not cursor:
                break

    async def once_connected(self, *, once: bool = False) -> None:
        self.queue = asyncio.Queue(maxsize=512)
        socket_path = self.home / "app-server-control" / "app-server-control.sock"
        # Stock daemon management publishes a symlink to its versioned socket.
        # Verify the actual endpoint's owner/type/mode, not the link's mode.
        socket_path = socket_path.resolve(strict=True)
        info = socket_path.stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("unsafe observer socket")
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(socket_path))) as client:
            async with client.ws_connect("http://localhost/", max_msg_size=1024 * 1024) as ws:
                self.ws = ws
                receiver = asyncio.create_task(self.receive())
                try:
                    await self.request("initialize", {"clientInfo": {"name": "codex_skills_activity", "version": "1"},
                                                      "capabilities": {"experimentalApi": True,
                                                                       "optOutNotificationMethods": CONTENT_NOTIFICATIONS}})
                    await ws.send_json({"method": "initialized"})
                    await self.poll()
                    if once:
                        return
                    deadline = asyncio.get_running_loop().time() + POLL_SECONDS
                    while not receiver.done():
                        remaining = max(0, deadline - asyncio.get_running_loop().time())
                        try:
                            async with asyncio.timeout(remaining):
                                await self.handle(await self.queue.get())
                        except TimeoutError:
                            await self.poll()
                            self.store.prune()
                            deadline = asyncio.get_running_loop().time() + POLL_SECONDS
                    await receiver
                finally:
                    receiver.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await receiver
                    self.active.clear()
                    self.joined.clear()

    async def run(self, *, once: bool = False) -> None:
        while True:
            try:
                await self.once_connected(once=once)
            except (OSError, ValueError, TimeoutError, aiohttp.ClientError):
                print("Codex activity source unavailable; existing leases will expire.", file=sys.stderr)
                if once:
                    raise
            if once:
                return
            await asyncio.sleep(POLL_SECONDS)


def project_claude(store: ActivityStore, account: str, rows: object) -> None:
    if not isinstance(rows, list):
        raise ValueError("Claude inventory is not an array")
    for row in rows:
        if not isinstance(row, dict) or not native_id(row.get("sessionId")):
            continue
        status = row.get("status")
        if status == "busy":
            state, attention = "working", None
        elif status == "waiting":
            attention = {"permission prompt": "approval", "sandbox request": "approval",
                         "input needed": "input"}.get(row.get("waitingFor"))
            if not attention:
                continue  # Generic dialogs do not prove approval or input.
            state = "attention"
        elif status == "idle":
            state, attention = "quiet", None
        else:
            continue
        store.emit(account, "claude", row["sessionId"], state, "claude-inventory",
                   attention=attention, renew_only=status in ("busy", "waiting"))


async def claude_poll(store: ActivityStore, account: dict, *, once: bool = False) -> None:
    child_env = dict(os.environ)
    child_env.pop("CLAUDE_CONFIG_DIR", None)
    child_env.update(account["env"])
    while True:
        process = None
        try:
            process = await asyncio.create_subprocess_exec("claude", "agents", "--json", env=child_env,
                                                           stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                                           limit=1024 * 1024)
            async with asyncio.timeout(10):
                output = await process.stdout.read(1024 * 1024 + 1)
                if len(output) > 1024 * 1024:
                    raise ValueError("Claude inventory exceeds bound")
                await process.wait()
            if process.returncode != 0:
                raise ValueError("Claude inventory unavailable")
            project_claude(store, account["accountID"], json.loads(output))
            store.prune()
        except (OSError, ValueError, TimeoutError):
            print("Claude activity source unavailable; existing leases will expire.", file=sys.stderr)
            if once:
                raise
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
        if once:
            return
        await asyncio.sleep(POLL_SECONDS)


async def observe(config: Path | None, *, once: bool = False, storage: Path | None = None,
                  state_root: Path | None = None) -> None:
    configured, root = accounts(config)
    private = state_root or Path(os.environ.get("CODE_HOME") or Path.home() / ".code").expanduser()
    store = ActivityStore(storage or root, private)
    jobs = []
    for account in configured:
        atomic_json(store.private / f"binding-{account['homeKey']}.json",
                    {"accountID": account["accountID"], "storageRoot": str(storage or root)})
        if account["harness"] == "codex":
            home = Path(account["env"]["CODEX_HOME"]).expanduser()
            jobs.append(CodexObserver(store, account["accountID"], home).run(once=once))
        else:
            jobs.append(claude_poll(store, account, once=once))
    results = await asyncio.gather(*jobs, return_exceptions=once)
    if any(isinstance(result, BaseException) for result in results):
        raise ValueError("one or more activity sources unavailable")


def install(config: Path | None, *, write: bool = False) -> dict:
    if sys.platform != "darwin":
        raise ValueError("launchd installation requires macOS")
    uv = shutil.which("uv")
    if not uv:
        raise ValueError("uv is unavailable")
    private = Path(os.environ.get("CODE_HOME") or Path.home() / ".code").expanduser()
    command = [uv, "run", "--no-python-downloads", str(Path(__file__).resolve()), "run"]
    if config:
        command.extend(["--config", str(config.resolve())])
    job = {"Label": LABEL, "ProgramArguments": command, "RunAtLoad": True, "KeepAlive": True,
           "ThrottleInterval": 30, "EnvironmentVariables": {"CODE_HOME": str(private), "PATH": os.environ.get("PATH", "")}}
    destination = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if write:
        directory(destination.parent)
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or destination.read_bytes() != plistlib.dumps(job):
                raise ValueError("existing activity job preserved; inspect it before replacing")
        else:
            destination.write_bytes(plistlib.dumps(job))
            destination.chmod(0o600)
    return {"written": write, "launchAgent": str(destination), "job": job}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "once", "install"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--storage-root", type=Path, help="explicit qualification/test store")
    parser.add_argument("--state-root", type=Path, help="isolated private state for qualification/tests")
    parser.add_argument("--write", action="store_true", help="write the previewed LaunchAgent; does not load it")
    args = parser.parse_args()
    try:
        if args.mode == "install":
            print(json.dumps(install(args.config, write=args.write)))
        else:
            asyncio.run(observe(args.config, once=args.mode == "once", storage=args.storage_root, state_root=args.state_root))
        return 0
    except (OSError, ValueError, TimeoutError):
        print("Agent activity unavailable; check account mappings and supported local sources.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
