# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "websockets==17.2",
# ]
# ///
"""Per-client native RPC ingress: every attachment/action owns its thread lease."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import stat

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))
import github_plan_closure as closure
import session_liveness

# App thread IDs select effective configuration, not execution ownership.
READS = {"app/installed", "app/list", "app/read", "thread/read", "thread/list", "thread/loaded/list", "thread/items/list",
         "thread/goal/get", "thread/queue/list", "thread/attachment/list", "thread/attachmentOwner/list",
         "thread/turns/list", "thread/items/read", "thread/timeline/list", "thread/searchOccurrences",
         "thread/backgroundTerminals/list", "thread/search", "thread/realtime/listVoices"}
# Server housekeeping does not attach execution, even when a clock read names
# its source thread. Track replies without invalidating that thread's verdict.
NON_ACTION_REQUESTS = {"account/chatgptAuthTokens/refresh", "attestation/generate", "currentTime/read"}
# These change stored presentation state or detach this subscriber, without
# attaching execution. Archive is deliberately excluded: it shuts down the
# spawn subtree, including descendants that may have independently resumed.
HOUSEKEEPING = {"thread/unarchive", "thread/name/set", "thread/metadata/update",
                "thread/readState/update", "thread/unsubscribe", "thread/section/move"}
# JSON Schema payloads (dynamic tools, turn output, elicitation) describe
# fields such as threadId; they never name a thread.
SCHEMAS = {"inputSchema", "outputSchema", "requestedSchema"}


def stopped_daemon_remedy(home):
    """Name the existing native start route without performing daemon lifecycle work."""
    command = shlex.join(["env", f"CODEX_HOME={home}", "codex", "app-server", "daemon", "start"])
    return (f"keep this source account; verify whether its daemon is stopped; only if stopped, "
            f"run {command}, then retry; never stop or restart a live daemon")


def source_endpoint(session, home, *, transcript=None):
    """Validate native recovery identity without taking a lease or changing state."""
    from github_plan_sessions import configured_homes
    import session_record
    require_native_identity(session)
    if home not in configured_homes(strict=True):
        raise ValueError("resume requires the source's configured account home")
    paths = [p for root in ("sessions", "archived_sessions") for p in (home / root).rglob(f"*{session}*.jsonl")]
    if transcript is not None:
        explicit = Path(transcript).resolve(strict=True)
        if not any(explicit.is_relative_to((home / root).resolve()) for root in ("sessions", "archived_sessions")):
            raise ValueError("source transcript is outside the selected native home")
        paths.append(explicit)
    if not paths:
        raise ValueError("source native transcript is unavailable")
    for path in dict.fromkeys(paths):
        session_record.session_status({"session_id": session, "harness": "codex", "transcript": str(path)})
    if closure.closed_log().exists():
        closure.resume_record(session, closure.log_rows())
    endpoint = closure.daemon_endpoint(home)
    try:
        info = endpoint.stat()
    except FileNotFoundError as error:
        raise ValueError(f"source daemon socket unavailable for CODEX_HOME={home}; "
                         + stopped_daemon_remedy(home)) from error
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("source daemon socket identity is unavailable")
    return endpoint


def require_native_identity(value):
    canonical = session_liveness.native_uuid(value)
    if canonical != value or not isinstance(value, str):
        raise ValueError("use the canonical native thread UUID" + (f" {canonical}" if canonical else ""))
    return value


def thread_targets(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in SCHEMAS or (key in {"threadId", "conversationId", "threadIds"} and child is None):
                # Optional identities may be null. Thread actions with no
                # target still refuse at the request check.
                continue
            if key in {"threadId", "conversationId"}:
                if not isinstance(child, str) or not child:
                    raise ValueError("native thread identity is unavailable")
                yield require_native_identity(child)
            elif key == "threadIds":
                if not isinstance(child, list) or not all(isinstance(s, str) and s for s in child):
                    raise ValueError("native thread identities are unavailable")
                yield from (require_native_identity(identity) for identity in child)
            else:
                yield from thread_targets(child)
    elif isinstance(value, list):
        for child in value:
            yield from thread_targets(child)


def managed_argv(home: Path, native: list[str]) -> list[str]:
    """Validate and normalize before launch preflight can create any terminal."""
    home = home.expanduser().resolve()
    native = list(native)
    if not native or Path(native[0]).name != "codex":
        raise ValueError("launch needs a direct fresh codex invocation")
    flags = native[1:native.index("--")] if "--" in native else native[1:]
    valued = {"-c", "--config", "--enable", "--disable", "--remote", "-i", "--image",
              "-m", "--model", "--local-provider", "-p", "--profile", "-s", "--sandbox",
              "-C", "--cd", "--add-dir", "-a", "--ask-for-approval"}
    switches = {"--strict-config", "--oss", "--approve-for-me", "--search", "--no-alt-screen",
                "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"}
    index = 0
    while index < len(flags):
        flag = flags[index]
        if flag in valued and index + 1 < len(flags):
            index += 2
        elif ("=" in flag and flag.split("=", 1)[0] in valued) or flag in switches:
            index += 1
        else:
            raise ValueError("managed launch needs fresh TUI options and a prompt after --; use the exact-thread helper for resume/queue")
    endpoint = "unix://" + str(closure.daemon_endpoint(home).resolve())
    remotes = [flags[i + 1] for i, a in enumerate(flags[:-1]) if a == "--remote"]
    remotes += [a.split("=", 1)[1] for a in flags if a.startswith("--remote=")]
    if len(remotes) == 1 and (remotes[0] == "unix://" or
            remotes[0].startswith("unix:///") and "unix://" + str(Path(remotes[0][7:]).resolve()) == endpoint):
        for index, flag in enumerate(native[:native.index("--")] if "--" in native else native):
            if flag == "--remote":
                native[index + 1] = endpoint
            elif flag.startswith("--remote="):
                native[index] = "--remote=" + endpoint
        remotes = [endpoint]
    if not remotes:
        native[1:1] = ["--remote", endpoint]
        remotes = [endpoint]
    if remotes != [endpoint]:
        raise ValueError("managed launch needs the selected account's exact --remote endpoint; use that endpoint or a local --no-daemon launch")
    return native


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
        require_native_identity(params["threadId"])
        if params.get("history") is not None:
            raise ValueError("managed resume requires a native thread ID, not inline cloud history")
        if params.get("path") is not None:
            path = Path(params["path"]).resolve(strict=True)
            if not any(path.is_relative_to((self.home / root).resolve()) for root in ("sessions", "archived_sessions")):
                raise ValueError("resume path is outside the selected native home")
            with path.open() as stream:
                header = json.loads(stream.readline())
            if not isinstance(header, dict) or header.get("type") != "session_meta" or not isinstance(header.get("payload"), dict) or header["payload"].get("id") != params["threadId"]:
                raise ValueError("resume path and native thread ID disagree")
            # Native path overrides can ignore threadId. Use authoritative ID
            # lookup even if that file is replaced after this identity check.
            message = {**message, "params": {k: v for k, v in params.items() if k != "path"}}
        return message

    async def connect(self, client):
        from websockets.asyncio.client import unix_connect
        from websockets.exceptions import ConnectionClosed
        # Each operation owns its targets through acknowledgement. References
        # keep overlapping operations protected even when replies arrive out of order.
        leases, requests, approvals = {}, {}, {}

        def release(targets):
            for target in targets:
                guard, count = leases[target]
                if count == 1:
                    del leases[target]
                    guard.__exit__(None, None, None)
                else:
                    leases[target] = guard, count - 1

        def acquire(targets, method):
            entered = set()
            try:
                for target in sorted(targets):
                    if target in leases:
                        guard, count = leases[target]
                        closure.set_action_pending(target, True, method=method, home=self.home)
                        leases[target] = guard, count + 1
                    else:
                        guard = closure.resume_guard(target, method=method, home=self.home)
                        guard.__enter__()
                        leases[target] = guard, 1
                    entered.add(target)
                return entered
            except BaseException:
                release(entered)
                raise

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
                        identity, method = message.get("id"), message.get("method")
                        if source == "client":
                            if not method:
                                if identity not in approvals:
                                    raise ValueError("native approval response has no guarded request")
                                targets, approval_method = approvals.pop(identity)
                                for target in targets:
                                    closure.set_action_pending(target, True, method=approval_method, home=self.home)
                                await native.send(raw)
                                release(targets)
                                reads[asyncio.create_task(client.recv())] = "client"
                                continue
                            try:
                                if identity is not None and identity in requests:
                                    raise ValueError("native request ID is already pending")
                                targets, session = set(), None
                                if method not in READS and method not in HOUSEKEEPING:
                                    if method in {"thread/resume", "thread/fork"}:
                                        message = self.exact_resume(message)
                                        raw = json.dumps(message)
                                    targets = set(thread_targets(message.get("params", {})))
                                    if method.startswith(("thread/", "turn/")) and method != "thread/start" and not targets:
                                        raise ValueError("native thread action requires an exact identity")
                                    if targets and identity is None:
                                        raise ValueError("native action needs a request ID for acknowledgement")
                                if method == "thread/resume":
                                    session = message["params"]["threadId"]
                                guarded = acquire(targets, method)
                            except (OSError, ValueError) as error:
                                await client.send(json.dumps({"id": identity, "error": {
                                    "code": -32600, "message": str(error)}}))
                                reads[asyncio.create_task(client.recv())] = "client"
                                continue
                            if identity is not None:
                                requests[identity] = guarded, session, method == "thread/start", method
                            await native.send(raw)
                            reads[asyncio.create_task(client.recv())] = "client"
                        else:
                            if method and identity is not None:
                                try:
                                    if identity in approvals:
                                        raise ValueError("native approval ID is already pending")
                                    targets = set(thread_targets(message.get("params", {})))
                                    if method not in NON_ACTION_REQUESTS and not targets:
                                        raise ValueError("native approval source is unavailable")
                                    approvals[identity] = acquire(set() if method in NON_ACTION_REQUESTS else targets, method), method
                                except (OSError, ValueError) as error:
                                    await native.send(json.dumps({"id": identity, "error": {"code": -32600, "message": str(error)}}))
                                    reads[asyncio.create_task(native.recv())] = "native"
                                    continue
                            # Bidirectional ID namespaces remain independent.
                            unguarded = set(), None, False, None
                            guarded, session, started, action_method = requests.pop(identity, unguarded) if not method else unguarded
                            owned = set()  # Separate references: start params may name the same thread.
                            try:
                                if started and "result" in message:
                                    # A fresh thread has no identity until native
                                    # acknowledgement. Own it before the client
                                    # learns it, as every later action does.
                                    thread = message["result"].get("thread") if isinstance(message["result"], dict) else None
                                    target = thread.get("id") if isinstance(thread, dict) else None
                                    try:
                                        if not isinstance(target, str) or not target:
                                            raise ValueError("started native thread identity is unavailable")
                                        require_native_identity(target)
                                        owned = acquire({target}, "thread/start")
                                    except (OSError, ValueError) as error:
                                        raw = json.dumps({"id": identity, "error": {"code": -32603,
                                            "message": "native thread start succeeded but its ownership lease was not created: " + str(error)}})
                                if action_method not in closure.ROUTED_METHODS:
                                    for target in guarded | owned:
                                        closure.set_action_pending(target, True, method=action_method, home=self.home)
                                if session and "result" in message and self.child_metadata:
                                    thread = message["result"]["thread"]
                                    path = Path(thread.get("path") or "/").resolve()
                                    if thread.get("id") != session or not any(path.is_relative_to((self.home / root).resolve()) for root in ("sessions", "archived_sessions")):
                                        raise ValueError("resumed native transcript identity is unavailable")
                                    try:
                                        # Publish in the actual lease-owning context. A
                                        # cancelled worker must never outlive its flock.
                                        closure.resumed_launch(session, self.child_metadata, str(path), self.child_tty)
                                    except (OSError, ValueError) as error:
                                        # Native resume happened. Durable invalidation
                                        # remains until verified closure, including when
                                        # metadata publication needs an exact retry.
                                        raw = json.dumps({"id": identity, "error": {"code": -32603,
                                            "message": "native resume succeeded but launch metadata was not recorded; preserve this source and retry the exact resume: " + str(error)}})
                                await client.send(raw)
                            finally:
                                release(guarded)
                                release(owned)
                            reads[asyncio.create_task(native.recv())] = "native"
            except ConnectionClosed:
                pass
            finally:
                for task in reads:
                    task.cancel()
                await asyncio.gather(*reads, return_exceptions=True)
                for guard, _count in list(leases.values()):
                    guard.__exit__(None, None, None)
                leases.clear()


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
            # The ingress reacquires ownership for every RPC before it acts.
            try:
                if session:
                    with closure.resume_guard(session, method="thread/resume", home=home):
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
