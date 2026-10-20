# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Bounded native ownership reads; never start, resume, or interrupt a session."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import subprocess
import sys

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
                # The exact authored release owns the handoff. This positive
                # native observation supplies only physical quiescence and its
                # lease; it needs neither a second verdict nor an idle receipt.
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
                # A row retained from the first snapshot can exit during
                # native observation. Keep earlier source peers independently.
                if shared.process_absent(pid):
                    continue
                return False
            # A stdio/extension server or unobserved daemon is unidentified too.
            listeners = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "--listen"]
            listeners += [arg.split("=", 1)[1] for arg in argv if arg.startswith("--listen=")]
            if (len(argv) < 2 or argv[1] != "app-server"
                    or len(listeners) != 1 or not listeners[0].startswith("unix://")
                    or str(Path(listeners[0][7:]).resolve()) not in (observed_sockets or set())):
                return False
    return True


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
        # Historical source homes are read through exact retained ownership;
        # this does not change configured-only resume or idle admission.
        for row in github_plan_closure.RECEIPTS.get().values():
            try:
                recovering = row.get("resumed_at") or github_plan_closure.action_pending(row["session_id"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if recovering:
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
    process_coverage = "unavailable" if process_text is None else "complete" if process_absence_known(process_text, observed_sockets) else "unidentified_interactive_processes"
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
                           "source_count": len(observed_sources), "unavailable_source_count": len(sources) - len(observed_sources),
                           "observed_source_incomplete": any(s["status"] == "incomplete" for s in sources)}
