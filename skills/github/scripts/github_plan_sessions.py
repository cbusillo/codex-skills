# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Bounded native ownership reads; never start, resume, or interrupt a session."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, contextmanager
from contextvars import ContextVar
import os
import json
import stat
from pathlib import Path
import subprocess
import sys
import time

import github_plan_closure
import github_plan_native

_SOURCE_CONTEXTS = ContextVar("canonical_source_contexts", default=())
_SOURCE_READBACK = ContextVar("canonical_source_readback", default=None)
_PENDING_PROOFS = ContextVar("pending_native_proofs", default=None)


@contextmanager
def _source_readback(readback, contexts=()):
    """The claim owner supplies canonical reads; native observation never does."""
    token = _SOURCE_READBACK.set(readback)
    context_token = _SOURCE_CONTEXTS.set(tuple(contexts))
    try:
        yield
    finally:
        _SOURCE_CONTEXTS.reset(context_token)
        _SOURCE_READBACK.reset(token)


def _source_home(source):
    return Path((source.get("resumed_generation") or source)["codex_home"]).resolve()


def native_helpers():
    directory = str(Path(__file__).resolve().parents[2] / "supervisor/scripts")
    if directory not in sys.path:
        sys.path.append(directory)
    import account_choice
    import session_record
    return account_choice, session_record


def codex_homes(*, strict: bool = False, configured_only: bool = False) -> list[Path]:
    homes = [] if configured_only else [Path.home() / ".codex", Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))]
    try:
        config = native_helpers()[0].load_config(
            path=github_plan_closure.closed_log().with_name("supervisor.toml")) if configured_only else native_helpers()[0].load_config()
        homes.extend(Path(a["env"]["CODEX_HOME"]) for a in config["accounts"] if a["provider"] == "openai")
    except (ValueError, OSError, ImportError):
        if strict:
            raise
        pass  # Missing optional account configuration never establishes absence.
    resolved = set(p.expanduser().resolve() for p in homes)
    if strict and not resolved:
        raise ValueError("configured Codex homes are unavailable")
    return sorted(resolved)


def transcript_snapshot(session: str, primary: str) -> dict[str, dict]:
    paths = {Path(primary).resolve()}
    for home in codex_homes():
        for root in (home / "sessions", home / "archived_sessions"):
            paths.update(path.resolve() for path in root.rglob(f"*{session}*.jsonl"))
    return {str(path): native_helpers()[1].session_status(
        {"session_id": session, "harness": "codex", "transcript": str(path)}, inspect_claims=False) for path in paths}


async def observe_home(home: Path, sessions: set[str], proof_sessions: set[str] | None = None):
    """Read the existing stock daemon over its native socket; only read RPCs."""
    import stat
    binding = github_plan_closure.daemon_endpoint(home)
    coverage = {"status": "incomplete", "peer_complete": False, "socket": str(binding), "home": str(home.resolve())}
    try:
        socket_path = binding.resolve(strict=True)
        info = socket_path.stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            return [], {}, coverage
        import aiohttp
    except FileNotFoundError:
        return [], {}, {**coverage, "status": "unavailable"}
    except (OSError, ImportError):
        return [], {}, coverage
    peers, states = [], {}
    coverage = {"status": "incomplete", "peer_complete": False, "socket": str(socket_path), "home": str(home.resolve())}
    proof_sessions = sessions if proof_sessions is None else proof_sessions
    active_seen = set()

    def live(session, thread):
        peers.append({"sessionId": session, "cwd": thread["cwd"], "state": "active", "source": "codex_session",
                      "home": str(home.resolve()), "native_state": thread.get("status", {}).get("type")})
        states[session] = {"state": "live", "source": "codex_app_server"}

    async def inspect(session, *, prove=False):
        thread = (await request("thread/read", {"threadId": session, "includeTurns": False}))["thread"]
        if thread.get("id") != session:
            raise ValueError("native session identity mismatch")
        if isinstance(thread.get("path"), str) and Path(thread["path"]).is_absolute():
            thread = {**thread, "path": str(Path(thread["path"]).resolve())}
        state = thread.get("status", {}).get("type")
        source = github_plan_closure.RECEIPTS.get().get(session)
        recovering = source and (github_plan_closure.action_pending(session) or source.get("resumed_at"))
        if prove and recovering:
            # A recovering generation must never fall through to the ordinary
            # authored-release path in another account, even after a clear.
            if _source_home(source) != home.resolve():
                return
            if state not in {"idle", "notLoaded"}:
                live(session, thread)
                return
            contexts = ([c for c in _SOURCE_CONTEXTS.get() if c.session == session]
                        if source.get("supervisor_owned") is not True else []) or [None]
            # A known-live source keeps its action lease available. End-only
            # candidate qualification may advance, but process failures may not.
            for first, context in enumerate(contexts):
                try:
                    process = await asyncio.to_thread(github_plan_native.verify_source_process, source, _context=context)
                    break
                except github_plan_native._SourceEndMismatch:
                    continue
            else:
                live(session, thread)
                return
            with github_plan_closure.pending_observation(session) as acquired:
                if not acquired:
                    live(session, thread)
                    return
                action = github_plan_closure.lease_state(session)
                proof = None
                for index in range(first, len(contexts)):
                    if (github_plan_closure.source_records({session}).get(session) != source
                            or github_plan_closure.lease_state(session) != action):
                        break
                    try:
                        if index != first:
                            process = await asyncio.to_thread(github_plan_native.verify_source_process, source,
                                                               _context=contexts[index])
                        proof = await github_plan_native.prove_end(source, request, process=process)
                        break
                    except github_plan_native._SourceEndMismatch:
                        continue
                if proof is None:
                    live(session, thread)
                    return
                if process.context is not None:
                    candidates = _PENDING_PROOFS.get()
                    if candidates is None or _SOURCE_READBACK.get() is None:
                        raise ValueError("canonical source resolution needs its owner's readback")
                    candidates[session] = (proof, thread["cwd"])
                    return
                # This write stays in the async lease owner, after all worker reads.
                github_plan_native.resolve_pending(session, proof)
                peers[:] = [peer for peer in peers if peer["sessionId"] != session]
                states[session] = {"state": "ended", "source": "native_resolution", "resolved_native": True,
                                   "ended_at": source.get("ended_at"), "native_state": proof["facts"]["state"],
                                   "covered_homes": sorted(proof["homes"]),
                                   "cwd": thread["cwd"], "leased_closure": False}
            return
        if state in {"active", "systemError"}:
            active_seen.add(session)
        if prove and state == "notLoaded":
            # Missing/slow queue evidence must also defeat authored-release
            # fallback, which does not otherwise need an ended receipt.
            previous = states.get(session)
            pending_peer = {"sessionId": session, "cwd": thread["cwd"], "state": "active", "source": "native_queue"}
            peers.append(pending_peer)
            states[session] = {"state": "live", "source": "native_queue"}
            try:
                queued = await request("thread/queue/list", {"threadId": session, "limit": 1})
                empty = (isinstance(queued, dict) and isinstance(queued.get("data"), list)
                         and not queued["data"] and queued.get("nextCursor") is None)
            except Exception:
                empty = False
            if not empty:
                return
            peers.remove(pending_peer)
            if previous is None:
                states.pop(session, None)
            else:
                states[session] = previous
            if session not in proof_sessions:
                # Exact authored releases retain the original metadata route.
                states[session] = {"state": "ended", "source": "codex_app_server",
                                   "native_state": state, "cwd": thread["cwd"], "leased_closure": False}
                return
        receipt = github_plan_closure.RECEIPTS.get().get(session)
        if receipt and not isinstance(receipt.get("closure_evidence"), dict):
            receipt = None
        if receipt and (receipt["closure_evidence"].get("codex_home") != str(home.resolve())
                        or not isinstance(receipt.get("transcript"), str)
                        or Path(thread.get("path") or "/").resolve() != Path(receipt["transcript"]).resolve()):
            receipt = None
        idle_proof = (state == "idle" and session not in proof_sessions and receipt is not None
                      and github_plan_closure.idle_eligible(receipt) and session not in active_seen)
        if idle_proof and prove:
            receipt = await asyncio.to_thread(github_plan_closure.verified_record, session, home)
            idle_proof = receipt is not None
            initial_facts = await github_plan_native.read_thread_facts(request, session) if idle_proof else None
            if not idle_proof:
                receipt = None
        else:
            receipt = None
            idle_proof = False
        if not prove or not thread.get("path") or not (
            idle_proof or state == "notLoaded" and session in proof_sessions
        ):
            if state in {"active", "idle", "systemError"}:
                live(session, thread)
            elif state != "notLoaded":
                raise ValueError("native session state is unknown")
            return
        # File work runs off the shared event loop; one slow home must not starve
        # the other daemons' native reads. The source lease spans this sandwich.
        snapshot = github_plan_closure.transcripts if receipt else transcript_snapshot
        before = receipt["closure_evidence"]["transcripts"] if receipt else await asyncio.to_thread(snapshot, session, thread["path"])
        final_facts = await github_plan_native.read_thread_facts(request, session) if receipt else None
        after = ({"id": final_facts["id"], "path": final_facts["path"], "status": {"type": final_facts["state"]}}
                 if receipt else (await request("thread/read", {"threadId": session, "includeTurns": False}))["thread"])
        verified = await asyncio.to_thread(snapshot, session, thread["path"], **({"include_status": True} if receipt else {}))
        # Status is transient metadata from this same authenticated parse. The
        # persisted receipt still binds every source byte through its hash.
        fingerprints = {path: {key: value for key, value in item.items() if key != "status"}
                        for path, item in verified.items()} if receipt else verified
        same = before == fingerprints and after.get("id") == session and after.get("path") == thread.get("path")
        final_state = after.get("status", {}).get("type")
        if receipt:
            source = {"path": thread["path"], "transcripts": verified}
            github_plan_native.require_quiet(initial_facts, source, {})
            github_plan_native.require_quiet(final_facts, source, {})
            same = same and github_plan_native.current_facts(initial_facts) == github_plan_native.current_facts(final_facts)
            current = await asyncio.to_thread(github_plan_closure.receipt_current, receipt)
            valid = same and before == receipt["closure_evidence"]["transcripts"] and current
            ended_at = receipt["ended_at"]
        else:
            valid = same and bool(before) and all(s.get("declared_safe_verdict", s["safe_verdict"]) for s in before.values())
            ended_at = max((s["timestamp"] for s in before.values()), default=None)
        if valid and final_state == state and (state == "notLoaded" or idle_proof):
            peers[:] = [peer for peer in peers if peer["sessionId"] != session]
            states[session] = {"state": "ended", "source": "supervisor_closure" if receipt else "codex_app_server",
                               "ended_at": ended_at, "native_state": state, "cwd": thread["cwd"],
                               "leased_closure": bool(receipt)}
        elif final_state in {"active", "idle", "systemError"}:
            live(session, after)

    try:
        async with asyncio.timeout(5):
            async with github_plan_native.connection(home) as request:
                cursor, loaded = None, set()
                for _ in range(10):
                    page = await request("thread/loaded/list", {"cursor": cursor, "limit": 100})
                    loaded.update(page["data"])
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
                loaded_ok = not cursor
                # Observe every loaded peer before any potentially large
                # transcript scan. Released claims need metadata only.
                for session in sorted(loaded):
                    try:
                        await inspect(session)
                    except (KeyError, TypeError, ValueError, AttributeError, OSError, ImportError):
                        loaded_ok = False
                        peers.append({"sessionId": session, "cwd": "", "state": "unknown", "source": "codex_session"})
                        states[session] = {"state": "live", "source": "codex_session"}
                coverage.update(status="available" if loaded_ok else "incomplete", peer_complete=loaded_ok)
                for session in sorted(sessions):
                    try:
                        await inspect(session, prove=coverage["peer_complete"])
                    except (KeyError, TypeError, ValueError, AttributeError, OSError, ImportError):
                        continue  # The aggregate recovery check owns an unresolved source.
    except aiohttp.ClientConnectorError as error:
        if isinstance(error.os_error, (ConnectionRefusedError, FileNotFoundError)):
            coverage.update(status="unavailable", reason="socket_not_listening")
    except (KeyError, TypeError, ValueError, OSError, TimeoutError, aiohttp.ClientError):
        pass
    return peers, states, coverage


def process_liveness():
    native_helpers()
    import session_liveness
    return session_liveness


def pending_ownership(histories, historical_states, *, deadline_at=None):
    """Resolve uncertainty now; historical tool text is never a permanent veto."""
    uncertain = [(node, state) for node, state in zip(histories, historical_states) if state["pending"]]
    if not uncertain:
        return []
    identities = {node["session_id"] for node, _ in uncertain}
    try:
        peers, current, coverage = observe(identities)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        peers, current, coverage = [], {}, {}
    pending = []
    for node, _ in uncertain:
        session = node["session_id"]
        if (current.get(session, {}).get("state") == "ended"
                or _physical_quiescent(node, peers, coverage, deadline_at=deadline_at)):
            continue  # Physical end clears uncertainty, never a GitHub claim.
        if node is histories[0] and _current_check_quiet(node, deadline_at=deadline_at):
            continue
        pending.append({"session_id": session,
                        "reason": "ownership is pending current native completion or verified session exit",
                        "native_coverage": {key: coverage.get(key) for key in
                            ("status", "peer_complete", "process_coverage", "source_count", "unavailable_source_count")}})
    return pending


def _physical_quiescent(node, peers, coverage, *, deadline_at=None):
    """Read-time uncertainty only: no closure, barrier or successor authority."""
    session = node["session_id"]
    if node["harness"] != "codex":
        return False
    try:
        leased = github_plan_closure.lease_path(session).exists()
        with github_plan_closure.ownership(session) as acquired:
            if not acquired and leased:
                return False
            github_plan_closure.require_canonical_identity(session)
            source = github_plan_closure.source_records({session}).get(session)
            action = github_plan_closure.lease_state(session)
            if (coverage.get("peer_complete") is not True or coverage.get("process_coverage") != "complete"
                    or any(peer.get("sessionId") == session and (
                        peer.get("source") == "codex_process" or peer.get("native_state") in {"active", "systemError"}) for peer in peers)):
                return False
            configured = set(codex_homes(strict=True, configured_only=True))
            primary = Path(node["transcript"]).resolve()
            homes = {home for home in configured if any(primary.is_relative_to((home / root).resolve())
                                                       for root in ("sessions", "archived_sessions"))}
            if not homes:
                return False
            observed_sockets = set(coverage.get("observed_sockets", []))
            homes.update(home for home in codex_homes(strict=True)
                         if str(github_plan_closure.daemon_endpoint(home).resolve()) in observed_sockets
                         and any(primary.is_relative_to((home / root).resolve()) for root in ("sessions", "archived_sessions")))
            homes.update(Path(home) for home in github_plan_closure.action_homes(action)["known"])
            if source is not None:
                for identity in (source, source.get("resumed_generation")):
                    if isinstance(identity, dict) and identity.get("codex_home"):
                        homes.add(Path(identity["codex_home"]).expanduser().resolve())
                github_plan_native.verify_source_process(source)
            sockets = {str(github_plan_closure.daemon_endpoint(home).resolve()) for home in homes}
            def absent():
                result = subprocess.run(["ps", "-ww", "-axo", "pid=,args="], capture_output=True, text=True, timeout=5)
                return (result.returncode == 0 and not process_sessions(result.stdout, {session})
                        and process_absence_known(result.stdout, sockets, homes=homes))
            async def read():
                async with asyncio.timeout(github_plan_native.read_timeout(deadline_at)):
                    for home in sorted(homes):
                        async with github_plan_native.connection(home, deadline_at=deadline_at) as request:
                            before = await github_plan_native.read_thread_facts(request, session)
                            after = await github_plan_native.read_thread_facts(request, session)
                            if (before["state"] not in {"idle", "notLoaded"} or before["queue"]
                                    or not any(Path(before["path"]).is_relative_to((home / root).resolve())
                                               for root in ("sessions", "archived_sessions"))
                                    or github_plan_native.current_facts(before) != github_plan_native.current_facts(after)):
                                return False
                            if before["state"] == "idle":
                                github_plan_native._require_runtime_quiet(before, [])
                                github_plan_native._require_runtime_quiet(after, [])
                                if not before["turns"] or any(turn["status"] == "inProgress" for turn in before["turns"]):
                                    return False
                    return bool(homes)
            if not absent() or not asyncio.run(read()) or not absent():
                return False
            if source is not None:
                github_plan_native.verify_source_process(source)
            return ((deadline_at is None or time.time() < deadline_at)
                    and github_plan_closure.source_records({session}).get(session) == source
                    and github_plan_closure.lease_state(session) == action
                    and github_plan_closure.lease_path(session).exists() == leased)
    except (OSError, ValueError, KeyError, TypeError, ImportError, TimeoutError, subprocess.SubprocessError):
        return False


def _current_check_quiet(node, *, deadline_at=None):
    """A live Codex check may exclude only its own identified native execution."""
    if node["harness"] != "codex" or not node.get("transcript"):
        return False
    import claim_receipts
    calls = [call for record in node["records"]
             for call in claim_receipts.calls_and_results(record, "codex")[0]]
    if not calls:
        return False
    launch_pending = (calls[-1][1] in {"exec", "functions.exec"}
                      and claim_receipts.single_composed_call(calls[-1][2], "exec_command")
                      and claim_receipts.isolated_command(calls[-1][1], calls[-1][2], "check_claims.py"))
    if not launch_pending and claim_receipts.current_check_process(node["records"], node["records"]) is None:
        return False
    import claim_history
    primary = Path(node["transcript"]).resolve()
    async def read():
        until = deadline_at if deadline_at is not None else time.time() + github_plan_native.read_timeout()
        async with asyncio.timeout(github_plan_native.read_timeout(until)):
            while True:
                try:
                    records = native_helpers()[1].read_jsonl(primary)
                except ValueError:
                    await asyncio.sleep(0.05)
                    continue  # A live append can be incomplete until startup lands.
                current = claim_history.root_records(primary, "codex", node["session_id"], records)
                process = claim_receipts.current_check_process(node["records"], current)
                if process is not None:
                    break
                if current[:len(node["records"])] != node["records"]:
                    return False
                for appended in current[len(node["records"]):]:
                    calls, results = claim_receipts.calls_and_results(appended, "codex")
                    if calls or results or (appended.get("payload") or {}).get("type") in {"task_started", "user_message"}:
                        return False
                await asyncio.sleep(0.05)
        homes = [home for home in codex_homes(strict=True, configured_only=True)
                 if any(primary.is_relative_to((home / root).resolve()) for root in ("sessions", "archived_sessions"))]
        if not homes:
            return False
        async with asyncio.timeout(github_plan_native.read_timeout(until)), AsyncExitStack() as connections:
            readers = {home: await connections.enter_async_context(
                github_plan_native.connection(home, deadline_at=until)) for home in sorted(set(homes))}
            # Shared history locates candidates, not the live owner. Fence the
            # whole authenticated set so a change in an earlier copy is visible.
            first = {home: await github_plan_native.read_thread_facts(request, node["session_id"], include_active=True)
                     for home, request in readers.items()}
            owners = 0
            for home, request in readers.items():
                before = first[home]
                after = await github_plan_native.read_thread_facts(request, node["session_id"], include_active=True)
                if (before["path"] != str(primary) or before["queue"]
                        or github_plan_native.current_facts(before) != github_plan_native.current_facts(after)):
                    return False
                if before["state"] == "active":
                    background = before["background"]
                    if (before != after or not before["executions"]["ended"] or len(background) != 1
                            or background[0][1] != process or not background[0][0]
                            or before["realtime"] or before["goal"] == "active"):
                        return False
                    owners += 1
                elif before["state"] in {"idle", "notLoaded"}:
                    github_plan_native._require_runtime_quiet(before, [])
                    github_plan_native._require_runtime_quiet(after, [])
                else:
                    return False
            return (owners == 1 and claim_receipts.current_check_process(node["records"], claim_history.root_records(
                        primary, "codex", node["session_id"], native_helpers()[1].read_jsonl(primary))) == process
                    and time.time() < until)
    try:
        return asyncio.run(read())
    except (OSError, ValueError, KeyError, TypeError, ImportError, TimeoutError, subprocess.SubprocessError):
        return False


def process_sessions(text: str, sessions: set[str]) -> list[dict]:
    peers = []
    shared = process_liveness()
    for line in text.splitlines():
        fields = line.split(None, 1)
        pid = int(fields[0]) if fields and fields[0].isdigit() and len(fields) == 2 else None
        command = fields[1] if pid is not None else line
        argv, bounded = shared.process_arguments(pid, command)
        if not argv or shared.harness_family(argv[0]) != "codex":
            continue
        resumed, _kind, _exact = shared.invocation(argv, bounded=bounded)
        identities = {shared.native_uuid(value) or value for value in resumed}
        for session in sorted(sessions):
            if (shared.native_uuid(session) or session) not in identities:
                continue
            peers.append({"sessionId": session, "source": "codex_process", "state": "live"})
    return peers


def process_absence_known(text: str, observed_sockets: set[str] | None = None, *, homes=None) -> bool:
    shared = process_liveness()
    for line in text.splitlines():
        fields = line.split(None, 1)
        pid = int(fields[0]) if fields and fields[0].isdigit() else None
        command = fields[1] if pid is not None and len(fields) == 2 else line
        argv, bounded = shared.process_arguments(pid, command)
        if argv and (shared.harness_family(argv[0]) == "codex"
                     or Path(argv[0]).name in {"codex-session.py", "codex_session_proxy.py"}):
            if pid is not None and github_plan_closure.managed_launch(pid, argv, observed_sockets or set()):
                continue
            if bounded and pid is not None and native_daemon_process(pid, argv, observed_sockets or set(), homes=homes):
                continue
            # A fresh noninteractive exec cannot attach to a pre-existing
            # thread. Interactive clients, pickers and exec-resume remain unknown.
            if shared.invocation(argv, bounded=bounded)[1] == "exec":
                continue
            if not bounded:
                # A row retained from the first snapshot can exit during
                # native observation. Keep earlier source peers independently.
                if shared.process_absent(pid):
                    continue
                return False
            # A listener argument is not proof that this process owns it.
            # Unbound servers and clients retain the conservative veto.
            return False
    return True


def native_daemon_process(pid, argv, sockets, *, homes=None):
    """Bind native maintenance/stdio children to an independently observed home.

    A helper name alone grants nothing. The native PID records, kernel process
    incarnation, executable, parent and authenticated home must all agree.
    """
    shared = process_liveness()

    def record(path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or info.st_size > 65536):
                raise ValueError("native PID record is unsafe")
            value = json.loads(stream.read(65537))
            if github_plan_closure.log_identity(path.lstat()) != github_plan_closure.log_identity(info):
                raise ValueError("native PID record changed")
            return value

    def matches(row, details):
        identity = row.get("processIdentity")
        return (type(row.get("pid")) is int and row["pid"] == details["pid"] and isinstance(identity, dict)
                and identity.get("bootId") == github_plan_closure.host_identity()["boot"]
                and all(identity.get(key) == value for key, value in details["identity"].items()))

    def daemon(args):
        # Native managed launch uses the default control socket. Other shapes
        # keep the existing explicit-listener path or remain unidentified.
        return (args[1:4] == ["app-server", "--listen", "unix://"]
                and len(set(args[4:])) == len(args[4:])
                and set(args[4:]) <= {"--analytics-default-enabled", "--managed-daemon"}
                and "--managed-daemon" in args[4:])

    try:
        details = shared.native_process_details(pid)
        if shared.native_arguments(pid) != argv or str(Path(argv[0]).resolve()) != details["executable"]:
            return False
        child = Path(argv[0]).name == "codex-code-mode-host"
        if child and argv[1:] not in ([], ["--listen", "grpc+stdio://"], ["--listen", "stdio://"]):
            return False
        updater = argv[1:] == ["app-server", "daemon", "pid-update-loop"]
        if not (child or updater or daemon(argv)):
            return False
        parent = shared.native_process_details(details["ppid"]) if child else details
        daemon_args = shared.native_arguments(parent["pid"]) if child else argv
        for home in codex_homes(strict=True) if homes is None else homes:
            if str(github_plan_closure.daemon_endpoint(home).resolve()) not in sockets:
                continue
            directory = home / "app-server-daemon"
            try:
                pairs = [("daemon.pid", "daemon-updater.pid"), ("app-server.pid", "app-server-updater.pid")]
                present = [pair for pair in pairs if (directory / pair[0]).exists()]
                if len(present) != 1:
                    continue
                server_name, updater_name = present[0]
                selected_path = directory / (updater_name if updater else server_name)
                original = record(selected_path)
                selected = parent if child else details
                if not matches(original, selected):
                    continue
                if updater:
                    server = record(directory / server_name)
                    parent = shared.native_process_details(server["pid"])
                    daemon_args = shared.native_arguments(parent["pid"])
                    if not matches(server, parent) or parent["executable"] != details["executable"]:
                        continue
                if (not daemon(daemon_args) or Path(parent["executable"]).name != "codex"
                        or str(Path(daemon_args[0]).resolve()) != parent["executable"]
                        or child and str(Path(parent["executable"]).with_name("codex-code-mode-host")) != details["executable"]):
                    continue
                return (shared.native_process_details(pid) == details
                        and shared.native_process_details(parent["pid"]) == parent
                        and shared.native_arguments(pid) == argv
                        and shared.native_arguments(parent["pid"]) == daemon_args
                        and record(selected_path) == original
                        and (not updater or record(directory / server_name) == server))
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                continue
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, subprocess.SubprocessError):
        pass
    return False


def observe(sessions: set[str], proof_sessions: set[str] | None = None, *, _source_contexts=()):
    retained = set()
    with github_plan_closure.observation(sessions, retain=retained) as leased:
        cache_token = github_plan_closure.LOG_CACHE.set(None)
        receipts = {}
        source_error = None
        try:
            sources = github_plan_closure.source_records(sessions)
        except (OSError, ValueError, KeyError, TypeError) as error:
            sources = {}
            source_error = str(error)
        for session, row in sources.items():
            try:
                if session in leased or row.get("resumed_at") or github_plan_closure.action_pending(session):
                    receipts[session] = row
            except (OSError, ValueError):
                continue
        token = github_plan_closure.RECEIPTS.set(receipts)
        contexts = list(_SOURCE_CONTEXTS.get())
        contexts.extend(context for context in _source_contexts if context not in contexts)
        context_token = _SOURCE_CONTEXTS.set(tuple(contexts))
        candidates = {}
        proof_token = _PENDING_PROOFS.set(candidates)
        try:
            peers, states, coverage = _observe(sessions, proof_sessions)
            github_plan_closure.inherit_observed_leases()
            for session, (proof, cwd) in candidates.items():
                covered = set(proof["homes"])
                def idle_copy(peer):
                    return peer.get("native_state") == "idle" and peer.get("home") in covered
                if (not coverage["peer_complete"] or coverage["process_coverage"] != "complete"
                        or any(peer.get("sessionId") == session and not idle_copy(peer) for peer in peers)):
                    continue
                # Keep canonical errors outside the asynchronous observation
                # catch: the claim reports their real class, not native_action.
                _SOURCE_READBACK.get()(proof["snapshot"]["process"].context)
                try:
                    current = github_plan_native.source_snapshot(proof["snapshot"]["entry"], proof["snapshot"]["process"])
                    if current != proof["snapshot"]:
                        raise ValueError("native source changed during canonical readback")
                    github_plan_native.resolve_pending(session, proof)
                except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
                    peers.append({"sessionId": session, "cwd": cwd, "state": "unknown", "source": "native_recovery"})
                    states[session] = {"state": "live", "source": "native_recovery"}
                    continue
                peers[:] = [peer for peer in peers if not (peer.get("sessionId") == session and idle_copy(peer))]
                states[session] = {"state": "ended", "source": "native_resolution", "resolved_native": True,
                                   "ended_at": proof["snapshot"]["process"].context.ended_at.isoformat(),
                                   "native_state": proof["facts"]["state"], "covered_homes": sorted(covered),
                                   "cwd": cwd, "leased_closure": False}
            for session, source in receipts.items():
                if ((source.get("resumed_at") or github_plan_closure.action_pending(session))
                        and not states.get(session, {}).get("resolved_native")):
                    if not any(peer.get("sessionId") == session and peer.get("source") == "native_recovery" for peer in peers):
                        peers.append({"sessionId": session, "cwd": "/", "state": "unknown", "source": "native_recovery"})
                    states[session] = {"state": "live", "source": "native_recovery"}
            for session in sessions:
                try:
                    github_plan_closure.require_canonical_identity(session)
                except ValueError as error:
                    diagnostic = {"source": "native_identity", "error": str(error),
                                  "canonical_session_id": process_liveness().native_uuid(session)}
                    peers.append({"sessionId": session, "cwd": "/", "state": "unknown", **diagnostic})
                    states[session] = {"state": "live", **diagnostic}
                    continue
                try:
                    blocked = (github_plan_closure.action_pending(session)
                               or session not in leased and github_plan_closure.lease_path(session).exists())
                except (OSError, ValueError):
                    blocked = True
                if blocked:
                    peers.append({"sessionId": session, "cwd": "/", "state": "active", "source": "native_action"})
                    states[session] = {"state": "live", "source": "native_action"}
            if source_error is not None:
                for session in sessions:
                    peers.append({"sessionId": session, "cwd": "/", "state": "unknown", "source": "native_recovery", "error": source_error})
                    states[session] = {"state": "live", "source": "native_recovery", "error": source_error}
            unreleased = sessions if proof_sessions is None else proof_sessions
            retained.update(session for session, state in states.items()
                            if state.get("state") == "ended" and session not in unreleased)
            return peers, states, coverage
        finally:
            _PENDING_PROOFS.reset(proof_token)
            _SOURCE_CONTEXTS.reset(context_token)
            github_plan_closure.RECEIPTS.reset(token)
            github_plan_closure.LOG_CACHE.reset(cache_token)


def _observe(sessions: set[str], proof_sessions: set[str] | None = None):
    async def run():
        homes = set(codex_homes())
        for session in sessions:
            try:
                scope = github_plan_closure.action_homes(github_plan_closure.lease_state(session))
                homes.update(Path(home) for home in scope["known"])
            except (OSError, ValueError):
                pass  # The canonical-identity/action barrier below reports it.
        # Historical source homes are read through exact retained ownership;
        # this does not change configured-only resume or idle admission.
        for row in github_plan_closure.RECEIPTS.get().values():
            if row.get("harness") != "codex":
                continue
            for identity in (row, row.get("resumed_generation")):
                if not isinstance(identity, dict):
                    continue
                try:
                    if identity.get("codex_home"):
                        homes.add(Path(identity["codex_home"]).resolve())
                except (OSError, ValueError, TypeError):
                    continue
        return await asyncio.gather(*(observe_home(home, sessions, proof_sessions) for home in sorted(homes)), return_exceptions=True)

    peers, states, resolutions = [], {}, {}
    process_text = None
    sources = []
    try:
        process_read = subprocess.run(["ps", "-ww", "-axo", "pid=,args="], capture_output=True, text=True, timeout=5)
        if process_read.returncode == 0:
            peers.extend(process_sessions(process_read.stdout, sessions))
            process_text = process_read.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    results = asyncio.run(run())
    github_plan_closure.inherit_observed_leases()
    for result in results:
        if isinstance(result, BaseException):
            sources.append({"status": "incomplete", "peer_complete": False, "socket": ""})
            continue
        observed, outcomes, coverage = result
        sources.append(coverage)
        peers.extend(observed)
        for session, outcome in outcomes.items():
            if outcome.get("resolved_native"):
                resolutions[session] = outcome
            if states.get(session, {}).get("state") != "live":
                states[session] = outcome
    peers = [peer for peer in peers if not (peer.get("native_state") == "idle"
             and peer.get("home") in resolutions.get(peer["sessionId"], {}).get("covered_homes", []))]
    for session, outcome in resolutions.items():
        states[session] = outcome
    for peer in peers:
        states[peer["sessionId"]] = {"state": "live", "source": peer["source"]}
    try:
        final_process = subprocess.run(["ps", "-ww", "-axo", "pid=,args="], capture_output=True, text=True, timeout=5)
        if final_process.returncode != 0:
            process_text = None
        elif process_text is not None:
            process_text += "\n" + final_process.stdout
            final_peers = process_sessions(final_process.stdout, sessions)
            peers.extend(final_peers)
            for peer in final_peers:
                states[peer["sessionId"]] = {"state": "live", "source": peer["source"]}
    except (OSError, subprocess.SubprocessError):
        process_text = None
    observed_sources = [s for s in sources if s["status"] != "unavailable"]
    peer_complete = bool(observed_sources) and all(s["peer_complete"] for s in observed_sources)
    observed_sockets = {s["socket"] for s in sources if s["peer_complete"]}
    observed_homes = {Path(s["home"]) for s in sources if s["peer_complete"] and s.get("home")}
    process_coverage = "unavailable" if process_text is None else "complete" if process_absence_known(
        process_text, observed_sockets, homes=observed_homes) else "unidentified_interactive_processes"
    for session, outcome in list(states.items()):
        if outcome.get("state") != "ended" or outcome.get("native_state") != "idle":
            continue
        if outcome.get("resolved_native"):
            continue
        entry = github_plan_closure.RECEIPTS.get().get(session)
        valid = peer_complete and process_coverage == "complete" and entry is not None
        if valid:
            try:
                github_plan_closure.native_process_check(entry)
                valid = github_plan_closure.receipt_current(entry)
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                valid = False
        if not valid:
            peers.append({"sessionId": session, "cwd": outcome["cwd"], "state": "active", "source": "codex_session"})
            states[session] = {"state": "live", "source": "codex_session"}
    states = {session: state for session, state in states.items()
              if state["state"] != "ended" or (peer_complete and process_coverage == "complete")}
    return peers, states, {"status": "available" if peer_complete else "incomplete" if observed_sources else "unavailable",
                           "peer_complete": peer_complete, "process_coverage": process_coverage,
                           "observed_sockets": sorted(observed_sockets),
                           "source_count": len(observed_sources), "unavailable_source_count": len(sources) - len(observed_sources),
                           "observed_source_incomplete": any(s["status"] == "incomplete" for s in sources)}
