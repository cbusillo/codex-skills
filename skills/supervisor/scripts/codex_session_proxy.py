# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets==17.2"]
# ///
"""Per-client native RPC ingress: every attachment/action owns its thread lease."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))
import github_plan_closure as closure

READS = {"thread/read", "thread/list", "thread/loaded/list", "thread/items/list",
         "thread/goal/get", "thread/queue/list", "thread/attachment/list", "thread/attachmentOwner/list",
         "thread/turns/list", "thread/items/read", "thread/timeline/list", "thread/searchOccurrences",
         "thread/backgroundTerminals/list", "thread/search", "thread/realtime/listVoices"}
NON_THREAD_REQUESTS = {"account/chatgptAuthTokens/refresh", "attestation/generate", "currentTime/read"}


def thread_targets(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"threadId", "conversationId"}:
                if not isinstance(child, str) or not child:
                    raise ValueError("native thread identity is unavailable")
                yield child
            elif key == "threadIds":
                if not isinstance(child, list) or not all(isinstance(s, str) and s for s in child):
                    raise ValueError("native thread identities are unavailable")
                yield from child
            else:
                yield from thread_targets(child)
    elif isinstance(value, list):
        for child in value:
            yield from thread_targets(child)


class Ingress:
    def __init__(self, home: Path):
        self.home = home
        self.upstream = closure.daemon_endpoint(home)
        self.child_metadata = None
        self.child_tty = "?"

    def exact_resume(self, message):
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("threadId"), str) or not params["threadId"]:
            raise ValueError("native resume requires an exact thread identity")
        if params.get("history") is not None:
            raise ValueError("managed resume requires a native thread ID, not inline cloud history")
        if params.get("path") is not None:
            path = Path(params["path"]).resolve(strict=True)
            if not any(path.is_relative_to((self.home / root).resolve()) for root in ("sessions", "archived_sessions")):
                raise ValueError("resume path is outside the selected native home")
            with path.open() as stream:
                header = json.loads(stream.readline(65536))
            if not isinstance(header, dict) or header.get("type") != "session_meta" or not isinstance(header.get("payload"), dict) or header["payload"].get("id") != params["threadId"]:
                raise ValueError("resume path and native thread ID disagree")
            # Native path overrides can ignore threadId. Use authoritative ID
            # lookup even if that file is replaced after this identity check.
            message = {**message, "params": {k: v for k, v in params.items() if k != "path"}}
        return message

    async def connect(self, client):
        from websockets.asyncio.client import unix_connect
        from websockets.exceptions import ConnectionClosed
        # One client connection owns its attachments until it disconnects.
        # All lease enters and exits run in this one task/context.
        with ExitStack() as leases:
            owned, pending = set(), {}
            async with unix_connect(str(self.upstream), max_size=None) as native:
                reads = {asyncio.create_task(client.recv()): "client", asyncio.create_task(native.recv()): "native"}
                try:
                    while reads:
                        done, _ = await asyncio.wait(reads, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            source = reads.pop(task)
                            raw = task.result()
                            message = json.loads(raw)
                            if not isinstance(message, dict):
                                raise ValueError("native RPC must be an object")
                            if source == "client":
                                method = message.get("method")
                                if method and method not in READS:
                                    try:
                                        if method in {"thread/resume", "thread/fork"}:
                                            message = self.exact_resume(message)
                                            raw = json.dumps(message)
                                        targets = set(thread_targets(message.get("params", {})))
                                        if method.startswith(("thread/", "turn/")) and method != "thread/start" and not targets:
                                            raise ValueError("native thread action requires an exact identity")
                                        for session in sorted(targets - owned):
                                            leases.enter_context(closure.resume_guard(session))
                                            owned.add(session)
                                    except (OSError, ValueError) as error:
                                        await client.send(json.dumps({"id": message.get("id"), "error": {
                                            "code": -32600, "message": str(error)}}))
                                        reads[asyncio.create_task(client.recv())] = "client"
                                        continue  # Never forward an unowned action.
                                if method == "thread/resume" and "id" in message:
                                    pending[message["id"]] = message["params"]["threadId"]
                                await native.send(raw)
                                reads[asyncio.create_task(client.recv())] = "client"
                            else:
                                # Server approvals can also cause execution.
                                # Own their source before the client can answer.
                                if message.get("method") and "id" in message:
                                    try:
                                        targets = set(thread_targets(message.get("params", {})))
                                        if message["method"] not in NON_THREAD_REQUESTS and not targets:
                                            raise ValueError("native approval source is unavailable")
                                        for target in sorted(targets - owned):
                                            leases.enter_context(closure.resume_guard(target))
                                            owned.add(target)
                                    except (OSError, ValueError) as error:
                                        await native.send(json.dumps({"id": message["id"], "error": {"code": -32600, "message": str(error)}}))
                                        reads[asyncio.create_task(native.recv())] = "native"
                                        continue
                                # Bidirectional RPC request IDs have separate
                                # namespaces. An approval can reuse a pending
                                # client ID without completing that request.
                                session = pending.pop(message.get("id"), None) if "method" not in message else None
                                if session and "result" in message and self.child_metadata:
                                    thread = message["result"]["thread"]
                                    path = Path(thread.get("path") or "/").resolve()
                                    if thread.get("id") != session or not any(path.is_relative_to((self.home / root).resolve()) for root in ("sessions", "archived_sessions")):
                                        raise ValueError("resumed native transcript identity is unavailable")
                                    try:
                                        await asyncio.to_thread(closure.resumed_launch, session, self.child_metadata, str(path), self.child_tty)
                                    except (OSError, ValueError) as error:
                                        # Resume already happened, with invalidation persisted.
                                        # Keep the lease and connection, but expose missing
                                        # metadata so the caller can retry this exact resume.
                                        raw = json.dumps({"id": message["id"], "error": {"code": -32603,
                                            "message": "native resume succeeded but launch metadata was not recorded; preserve this source and retry the exact resume: " + str(error)}})
                                await client.send(raw)
                                reads[asyncio.create_task(native.recv())] = "native"
                except ConnectionClosed:
                    pass
                finally:
                    for task in reads:
                        task.cancel()
                    await asyncio.gather(*reads, return_exceptions=True)


async def run_client(home: Path, native: list[str], *, session: str | None = None) -> int:
    """Run one TUI through a private ingress; never start or restart its daemon."""
    from websockets.asyncio.server import unix_serve
    ingress = Ingress(home)
    import stat
    source = ingress.upstream.stat()
    if not stat.S_ISSOCK(source.st_mode) or source.st_uid != os.getuid() or source.st_mode & 0o022:
        raise ValueError("source daemon socket identity is unavailable")
    # Keep Unix socket paths short. Only this invocation owns this directory.
    with tempfile.TemporaryDirectory(prefix="codex-ingress-", dir="/tmp") as directory:
        endpoint = Path(directory) / "rpc.sock"
        async with unix_serve(ingress.connect, str(endpoint), max_size=None):
            endpoint.chmod(0o600)
            proxy = closure.register_proxy(str(home), str(endpoint))
            command = list(native)
            remote = "unix://" + str(endpoint)
            for index, arg in enumerate(command):
                if arg == "--remote":
                    command[index + 1] = remote
                    break
                if arg.startswith("--remote="):
                    command[index] = "--remote=" + remote
                    break
            else:
                command[1:1] = ["--remote", remote]
            boot = [sys.executable, str(Path(__file__).with_name("codex-session.py")), "native",
                    "--codex-home", str(home), "--proxy", str(endpoint), "--proxy-parent", str(os.getpid()), "--", *command]
            # For exact resume, invalidate before even starting the client.
            # The ingress reacquires and retains ownership before any RPC acts.
            try:
                if session:
                    with closure.resume_guard(session):
                        pass
                with subprocess.Popen(boot, env={**os.environ, "CODEX_HOME": str(home)}) as child:
                    try:
                        ingress.child_metadata = closure.launch_metadata(child.pid, proxy["launch_generation"], str(home))
                        ingress.child_tty = os.ttyname(0).removeprefix("/dev/") if os.isatty(0) else "?"
                        return await asyncio.to_thread(child.wait)
                    finally:
                        if child.poll() is None:
                            child.terminate()
                            await asyncio.to_thread(child.wait)
            finally:
                try:
                    closure.remove_registration(os.getpid(), proxy["launch_generation"])
                except (OSError, ValueError) as error:
                    print("native client cleanup incomplete: " + str(error), file=sys.stderr)
