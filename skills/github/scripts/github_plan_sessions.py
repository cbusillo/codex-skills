# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Bounded native ownership reads; never start, resume, or interrupt a session."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys

import github_plan_closure


def native_helpers():
    directory = str(Path(__file__).resolve().parents[2] / "supervisor/scripts")
    if directory not in sys.path:
        sys.path.append(directory)
    import account_choice
    import session_record
    return account_choice, session_record


def codex_homes(*, strict: bool = False, configured_only: bool = False) -> list[Path]:
    homes = [] if configured_only else [Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))]
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
        {"session_id": session, "harness": "codex", "transcript": str(path)}) for path in paths}


async def observe_home(home: Path, sessions: set[str], proof_sessions: set[str] | None = None):
    """Read the existing stock daemon over its native socket; only read RPCs."""
    import stat
    binding = github_plan_closure.daemon_endpoint(home)
    coverage = {"status": "incomplete", "peer_complete": False, "socket": str(binding)}
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
    coverage = {"status": "incomplete", "peer_complete": False, "socket": str(socket_path)}
    proof_sessions = sessions if proof_sessions is None else proof_sessions
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

    async def executions_ended(session):
        queued = await request("thread/queue/list", {"threadId": session, "limit": 1})
        if (not isinstance(queued, dict) or not isinstance(queued.get("data"), list)
                or queued["data"] or queued.get("nextCursor") is not None):
            return False
        cursor = None
        for _ in range(100):
            page = await request("thread/items/list", {"threadId": session, "limit": 20,
                                                       "sortDirection": "desc", "cursor": cursor})
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise ValueError("native execution coverage is unavailable")
            for entry in page["data"]:
                item = entry["item"]
                if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                    raise ValueError("native execution identity is unavailable")
                if item.get("status") == "inProgress":
                    return False
                if item.get("type") in {"commandExecution", "dynamicToolCall", "mcpToolCall", "collabAgentToolCall"}:
                    if item.get("status") not in {"completed", "failed", "declined"}:
                        return False
                    if item["type"] == "commandExecution" and item.get("processId") is not None and type(item.get("exitCode")) is not int:
                        return False
            cursor = page["nextCursor"]
            if cursor is None:
                return True
            if not isinstance(cursor, str) or not cursor:
                raise ValueError("native execution continuation is unavailable")
        return False  # Truncated execution evidence never proves closure.

    active_seen = set()

    def live(session, thread):
        peers.append({"sessionId": session, "cwd": thread["cwd"], "state": "active", "source": "codex_session"})
        states[session] = {"state": "live", "source": "codex_app_server"}

    async def inspect(session, *, prove=False):
        thread = (await request("thread/read", {"threadId": session, "includeTurns": False}))["thread"]
        if thread.get("id") != session:
            raise ValueError("native session identity mismatch")
        state = thread.get("status", {}).get("type")
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
        receipt = github_plan_closure.RECEIPTS.get().get(session)
        if receipt and (receipt["closure_evidence"].get("codex_home") != str(home.resolve())
                        or not isinstance(receipt.get("transcript"), str)
                        or Path(thread.get("path") or "/").resolve() != Path(receipt["transcript"]).resolve()):
            receipt = None
        idle_proof = (state == "idle" and session not in proof_sessions and receipt is not None
                      and github_plan_closure.idle_eligible(receipt) and session not in active_seen)
        if idle_proof and prove:
            receipt = await asyncio.to_thread(github_plan_closure.verified_record, session, home)
            idle_proof = receipt is not None and await executions_ended(session)
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
        after = (await request("thread/read", {"threadId": session, "includeTurns": False}))["thread"]
        verified = await asyncio.to_thread(snapshot, session, thread["path"])
        same = before == verified and after.get("id") == session and after.get("path") == thread.get("path")
        final_state = after.get("status", {}).get("type")
        if receipt:
            current = await asyncio.to_thread(github_plan_closure.receipt_current, receipt)
            valid = same and before == receipt["closure_evidence"]["transcripts"] and current
            ended_at = receipt["ended_at"]
        else:
            valid = same and bool(before) and all(s["safe_verdict"] for s in before.values())
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
                            continue  # No successful observation means unknown, never ended.
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


def process_absence_known(text: str, observed_sockets: set[str] | None = None) -> bool:
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
            # A fresh noninteractive exec cannot attach to a pre-existing
            # thread. Interactive clients, pickers and exec-resume remain unknown.
            if shared.invocation(argv, bounded=bounded)[1] == "exec":
                continue
            if not bounded:
                return False
            # A stdio/extension server or unobserved daemon is unidentified too.
            listeners = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "--listen"]
            listeners += [arg.split("=", 1)[1] for arg in argv if arg.startswith("--listen=")]
            if (len(argv) < 2 or argv[1] != "app-server"
                    or len(listeners) != 1 or not listeners[0].startswith("unix://")
                    or str(Path(listeners[0][7:]).resolve()) not in (observed_sockets or set())):
                return False
    return True


def observe(sessions: set[str], proof_sessions: set[str] | None = None):
    with github_plan_closure.observation(sessions) as leased:
        cache_token = github_plan_closure.LOG_CACHE.set(None)
        receipts = github_plan_closure.candidate_records(leased)
        token = github_plan_closure.RECEIPTS.set(receipts)
        try:
            peers, states, coverage = _observe(sessions, proof_sessions)
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
            return peers, states, coverage
        finally:
            github_plan_closure.RECEIPTS.reset(token)
            github_plan_closure.LOG_CACHE.reset(cache_token)


def _observe(sessions: set[str], proof_sessions: set[str] | None = None):
    async def run():
        return await asyncio.gather(*(observe_home(home, sessions, proof_sessions) for home in codex_homes()), return_exceptions=True)

    peers, states = [], {}
    process_text = None
    sources = []
    try:
        process_read = subprocess.run(["ps", "-ww", "-axo", "pid=,args="], capture_output=True, text=True, timeout=5)
        if process_read.returncode == 0:
            peers.extend(process_sessions(process_read.stdout, sessions))
            process_text = process_read.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    for result in asyncio.run(run()):
        if isinstance(result, BaseException):
            sources.append({"status": "incomplete", "peer_complete": False, "socket": ""})
            continue
        observed, outcomes, coverage = result
        sources.append(coverage)
        peers.extend(observed)
        for session, outcome in outcomes.items():
            if states.get(session, {}).get("state") != "live":
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
    process_coverage = "unavailable" if process_text is None else "complete" if process_absence_known(process_text, observed_sockets) else "unidentified_interactive_processes"
    for session, outcome in list(states.items()):
        if outcome.get("state") != "ended" or outcome.get("native_state") != "idle":
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
                           "source_count": len(observed_sources), "unavailable_source_count": len(sources) - len(observed_sources),
                           "observed_source_incomplete": any(s["status"] == "incomplete" for s in sources)}
