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
import datetime as dt
import json
import os
import plistlib
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import aiohttp

from agent_activity import (INBOX, LEASE_SECONDS, RETENTION_SECONDS, STATE, ActivityStore, account_id, atomic_json,
                            directory, home_key, inbox_records, native_id, private_root, read_json, renewal_report)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from skills.supervisor.scripts import account_choice

POLL_SECONDS = 30
LABEL = "com.codex-skills.agent-activity"
LIFECYCLE = {"thread/status/changed", "thread/started", "thread/closed", "turn/started", "turn/completed"}
CONTENT_NOTIFICATIONS = ["item/started", "item/completed", "item/agentMessage/delta",
                         "item/reasoning/textDelta", "item/reasoning/summaryTextDelta",
                         "item/commandExecution/outputDelta", "item/fileChange/outputDelta",
                         "turn/diff/updated", "turn/plan/updated", "rawResponseItem/completed"]


def accounts(config_path: Path | None = None) -> list[dict]:
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
    return result


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
            del self.pending[request_id]

    async def receive(self) -> None:
        try:
            async for frame in self.ws:
                if frame.type != aiohttp.WSMsgType.TEXT:
                    raise ValueError("unexpected observer frame")
                message = json.loads(frame.data)
                if not isinstance(message, dict):
                    continue
                future = self.pending.get(message.get("id")) if isinstance(message.get("id"), int) else None
                if "method" not in message and future and not future.done():
                    if "error" in message:
                        future.set_exception(ValueError("observer RPC rejected"))
                    else:
                        future.set_result(message.get("result", {}))
                elif "id" not in message and message.get("method") in LIFECYCLE:
                    # Unknown notifications and every server request are discarded.
                    # Never answer a request addressed to an interactive client.
                    try:
                        self.queue.put_nowait(message)
                    except asyncio.QueueFull:
                        raise ValueError("lifecycle queue overflow; reconnect without inferred outcomes") from None
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
            state = "attention"
        elif status == "idle":
            state = {"done": "completed", "failed": "failed", "stopped": "cancelled"}.get(row.get("state"), "quiet")
            attention = None
        elif row.get("state") in ("done", "failed", "stopped"):
            state = {"done": "completed", "failed": "failed", "stopped": "cancelled"}[row["state"]]
            attention = None
        else:
            continue
        store.emit(account, "claude", row["sessionId"], state, "claude-inventory",
                   attention=attention, renew_only=status in ("busy", "waiting"))


async def claude_poll(store: ActivityStore, account: dict, *, once: bool = False, executable: str = "claude") -> None:
    child_env = dict(os.environ)
    child_env.pop("CLAUDE_CONFIG_DIR", None)
    child_env.update(account["env"])
    while True:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(executable, "agents", "--json", env=child_env,
                                                           stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                                           limit=1024 * 1024)
            async with asyncio.timeout(10):
                chunks = []
                size = 0
                while chunk := await process.stdout.read(65536):
                    size += len(chunk)
                    if size > 1024 * 1024:
                        raise ValueError("Claude inventory exceeds bound")
                    chunks.append(chunk)
                output = b"".join(chunks)
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


async def observe(config: Path | None, *, once: bool = False,
                  state_root: Path | None = None, claude_cli: Path | None = None) -> None:
    configured = accounts(config)
    # Events go only to the private inbox; Context Panel imports them. The App Group is never written here.
    store = ActivityStore(state_root or private_root(os.environ))
    live_bindings = {f"binding-{a['homeKey']}.json" for a in configured}
    for binding in store.private.glob("binding-*.json"):
        if binding.name in live_bindings or not re.fullmatch(r"binding-[0-9a-f]{64}[.]json", binding.name) or binding.is_symlink():
            continue
        try:
            if read_json(binding).get("producerID") == "codex-skills":
                binding.unlink()
        except (OSError, ValueError):
            continue
    jobs = []
    for account in configured:
        atomic_json(store.private / f"binding-{account['homeKey']}.json",
                    {"producerID": "codex-skills", "accountID": account["accountID"]})
        if account["harness"] == "codex":
            home = Path(account["env"]["CODEX_HOME"]).expanduser()
            jobs.append(CodexObserver(store, account["accountID"], home).run(once=once))
        else:
            jobs.append(claude_poll(store, account, once=once, executable=str(claude_cli) if claude_cli else "claude"))
    results = await asyncio.gather(*jobs, return_exceptions=once)
    if any(isinstance(result, BaseException) for result in results):
        raise ValueError("one or more activity sources unavailable")


def durable_cli(name: str, override: Path | None = None) -> str:
    selected = str(override) if override else shutil.which(name)
    if not selected or not Path(selected).is_absolute():
        raise ValueError(f"{name} needs an absolute installed CLI path")
    path = Path(selected).resolve(strict=True)
    temporary = Path(tempfile.gettempdir()).resolve()
    entry = Path(selected)
    if (path.is_relative_to(temporary) or entry.is_relative_to(temporary) or
            any((p / "pyvenv.cfg").is_file() for p in (*entry.parents, *path.parents))):
        raise ValueError(f"{name} is in temporary storage or a virtual environment; select a durable CLI")
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"{name} is not an executable file")
    return selected


def install(config: Path | None, *, write: bool = False, claude_cli: Path | None = None) -> dict:
    if sys.platform != "darwin":
        raise ValueError("launchd installation requires macOS")
    # Durable jobs must not depend on a linked checkout slated for retirement.
    if (ROOT / ".git").is_file():
        raise ValueError("install from the reconciled runtime checkout, not a task worktree")
    uv = durable_cli("uv")
    claude = durable_cli("claude", claude_cli)
    private = private_root(os.environ)
    command = [uv, "run", "--no-python-downloads", str(Path(__file__).resolve()), "run", "--claude-cli", claude]
    if config:
        command.extend(["--config", str(config.resolve())])
    # A node-based Claude wrapper needs its installed node, not a shell snapshot.
    bins = [str(Path(uv).parent), str(Path(claude).parent)]
    if shutil.which("node"):
        bins.append(str(Path(durable_cli("node")).parent))
    bins.extend(["/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    job = {"Label": LABEL, "ProgramArguments": command, "RunAtLoad": True, "KeepAlive": True,
           "ThrottleInterval": 30, "EnvironmentVariables": {"CODE_HOME": str(private), "PATH": ":".join(dict.fromkeys(bins))}}
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


RENEWAL_EXIT = {"pass": 0, "fail": 3, "inconclusive": 4}


def renewal_check(state_root: Path | None, window: int, harness: str) -> dict:
    """Read the inbox only; never create, prune or write anything."""
    inbox = (state_root or private_root(os.environ)).expanduser() / STATE / INBOX
    if not inbox.is_absolute():
        raise ValueError("activity directory must be absolute")
    if any(path.is_symlink() for path in (*inbox.parents, inbox)):
        raise ValueError("activity directory contains a symlink")
    records, files = inbox_records(inbox)
    harnesses = ("codex", "claude") if harness == "both" else (harness,)
    report = renewal_report(records, dt.datetime.now(dt.timezone.utc), window, harnesses)
    report["files"].update(files)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "once", "install", "renewal-check"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--state-root", type=Path, help="isolated private state and inbox for qualification/tests")
    parser.add_argument("--claude-cli", type=Path, help="absolute durable Claude CLI, including for LaunchAgent installation")
    parser.add_argument("--write", action="store_true", help="write the previewed LaunchAgent; does not load it")
    parser.add_argument("--window", type=int, default=600, metavar="SECONDS",
                        help="renewal-check: inbox seconds to examine, above the lease and at most 24 hours")
    parser.add_argument("--harness", choices=("codex", "claude", "both"), default="both",
                        help="renewal-check: harness to judge")
    args = parser.parse_args(argv)
    if args.mode == "renewal-check":
        if not LEASE_SECONDS < args.window <= RETENTION_SECONDS:
            parser.error(f"--window must be above {LEASE_SECONDS} and at most {RETENTION_SECONDS} seconds")
        try:
            report = renewal_check(args.state_root, args.window, args.harness)
        except (OSError, ValueError):
            print("Agent activity inbox unavailable.", file=sys.stderr)
            return 1
        print(json.dumps(report))
        return RENEWAL_EXIT[report["verdict"]]
    try:
        if args.mode == "install":
            print(json.dumps(install(args.config, write=args.write, claude_cli=args.claude_cli)))
        else:
            asyncio.run(observe(args.config, once=args.mode == "once",
                                state_root=args.state_root, claude_cli=args.claude_cli))
        return 0
    except (OSError, ValueError, TimeoutError):
        print("Agent activity unavailable; check account mappings and supported local sources.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
