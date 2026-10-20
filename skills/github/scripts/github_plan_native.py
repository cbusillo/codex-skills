# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Native thread facts and exact typed queue input; never load a thread to read it."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager, AsyncExitStack
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid
from typing import NamedTuple
from datetime import datetime

import github_plan_closure as closure


@asynccontextmanager
async def connection(home: Path, *, max_msg_size: int = 64 * 1024 * 1024):
    import aiohttp
    endpoint = closure.daemon_endpoint(home).resolve(strict=True)
    info = endpoint.stat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("native socket ownership is unavailable")
    sequence = 0
    try:
        async with asyncio.timeout(5):
            async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(endpoint))) as client:
                async with client.ws_connect("http://localhost/", max_msg_size=max_msg_size) as ws:
                    async def request(method, params):
                        nonlocal sequence
                        sequence += 1
                        await ws.send_json({"id": sequence, "method": method, "params": params})
                        while True:
                            try:
                                message = await ws.receive_json()
                            except aiohttp.WSMessageTypeError as error:
                                raise ValueError("native response is unavailable; preserve the uncertain operation and do not replay") from error
                            if not isinstance(message, dict):
                                raise ValueError("native response is unavailable")
                            if message.get("id") == sequence and "method" not in message:
                                if "error" in message or "result" not in message:
                                    raise ValueError("native request rejected")
                                return message["result"]
                    initialized = await request("initialize", {"clientInfo": {"name": "codex_skills_claim", "version": "1"},
                                                               "capabilities": {"experimentalApi": True}})
                    native_home = initialized.get("codexHome") if isinstance(initialized, dict) else None
                    request.codex_home = str(Path(native_home).resolve()) if isinstance(native_home, str) and Path(native_home).is_absolute() else None
                    if request.codex_home != str(home.resolve()):
                        raise ValueError("native daemon home identity disagrees with the source")
                    await ws.send_json({"method": "initialized"})
                    yield request
    except aiohttp.ClientError as error:
        if isinstance(error, (OSError, ValueError)):
            raise  # Preserve existing known-absence and ordinary refusal handling.
        raise ValueError("native transport failed; preserve the uncertain operation and do not replay") from error


async def pages(request, method, session, **params):
    cursor, seen = None, set()
    for _ in range(100):
        page = await request(method, {"threadId": session, "limit": 20, "cursor": cursor, **params})
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise ValueError("native read coverage is unavailable")
        yield page
        cursor = page.get("nextCursor")
        if cursor is None:
            return
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise ValueError("native continuation is unavailable")
        seen.add(cursor)
    raise ValueError("native read coverage is truncated")


async def read_queue(request, session):
    entries = {}
    async for page in pages(request, "thread/queue/list", session):
        for entry in page["data"]:
            if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]
                    or not isinstance(entry.get("clientUserMessageId"), str) or not isinstance(entry.get("input"), list)
                    or entry["id"] in entries):
                raise ValueError("native queue identity is unavailable")
            digest = hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":"),
                                               ensure_ascii=True, allow_nan=False).encode()).hexdigest()
            entries[entry["id"]] = (entry["clientUserMessageId"], digest)
    return entries


# Known native history variants: a future execution-shaped variant must not be
# silently interpreted as inert history by required current-work coverage.
THREAD_ITEM_TYPES = {"userMessage", "hookPrompt", "agentMessage", "functionCallOutput", "plan", "reasoning",
    "commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall", "collabAgentToolCall", "subAgentActivity",
    "webSearch", "imageView", "sleep", "imageGeneration", "enteredReviewMode", "exitedReviewMode", "contextCompaction"}


async def read_executions(request, session):
    ended, items = True, []
    async for page in pages(request, "thread/items/list", session, sortDirection="desc"):
        for entry in page["data"]:
            item = entry.get("item") if isinstance(entry, dict) else None
            if (not isinstance(item, dict) or item.get("type") not in THREAD_ITEM_TYPES
                    or not isinstance(item.get("id"), str) or not item["id"]
                    or not isinstance(entry.get("turnId"), str) or not entry["turnId"]):
                raise ValueError("native execution identity is unavailable")
            state = item.get("status")
            execution = item["type"] in {"commandExecution", "dynamicToolCall", "mcpToolCall", "collabAgentToolCall"}
            if execution and (not isinstance(item.get("id"), str) or not item["id"]
                              or not isinstance(entry.get("turnId"), str) or not entry["turnId"]):
                raise ValueError("native execution identity is unavailable")
            terminal = {"completed", "failed", "declined"} if item["type"] == "commandExecution" else {"completed", "failed"}
            if item["type"] == "collabAgentToolCall":
                terminal.add("interrupted")
            ended &= state != "inProgress" and (not execution or state in terminal)
            if item["type"] == "commandExecution" and item.get("processId") is not None:
                ended &= type(item.get("exitCode")) is int
            if execution:
                items.append((entry.get("turnId"), item.get("id"), item["type"], state,
                              item.get("processId"), item.get("exitCode")))
    return {"ended": ended, "items": items}


async def read_turns(request, session):
    turns, seen = [], set()
    async for page in pages(request, "thread/turns/list", session, sortDirection="desc", itemsView="notLoaded"):
        for turn in page["data"]:
            if (not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]
                    or turn["id"] in seen or turn.get("status") not in {"completed", "interrupted", "failed", "inProgress"}):
                raise ValueError("native actual turn identity is unavailable")
            seen.add(turn["id"])
            turns.append({key: turn.get(key) for key in ("id", "status", "startedAt", "completedAt")})
    return turns


async def optional_turns(request, session):
    turns, complete = [], False
    try:
        # One bounded newest slice suffices only when it contains an actual
        # source-turn anchor. Optional history cannot spend the end-proof budget.
        async with asyncio.timeout(.25):
            page = await request("thread/turns/list", {"threadId": session, "limit": 20,
                                                       "sortDirection": "desc", "itemsView": "notLoaded"})
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            return turns, False
        complete = page.get("nextCursor") is None
        seen = set()
        for turn in page["data"]:
            if (not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]
                    or turn["id"] in seen or turn.get("status") not in {"completed", "interrupted", "failed", "inProgress"}):
                complete = False
                continue
            seen.add(turn["id"])
            turns.append({key: turn.get(key) for key in ("id", "status", "startedAt", "completedAt")})
    except (ValueError, KeyError, TypeError, TimeoutError):
        complete = False
    return turns, complete


async def read_thread(request, session, *, include_turns=False):
    result = await request("thread/read", {"threadId": session, "includeTurns": include_turns})
    thread = result.get("thread") if isinstance(result, dict) else None
    status = thread.get("status") if isinstance(thread, dict) else None
    if (not isinstance(thread, dict) or thread.get("id") != session or not isinstance(thread.get("path"), str)
            or not Path(thread["path"]).is_absolute() or not isinstance(status, dict) or status.get("type") not in {
                "idle", "notLoaded", "active", "systemError"}):
        raise ValueError("native thread identity or state is unavailable")
    return {"id": session, "path": str(Path(thread["path"]).resolve()), "state": thread["status"]["type"],
            "history_mode": thread.get("historyMode", "legacy")}


async def read_thread_facts(request, session, *, include_active=False):
    # This serialized read fences the exact proven synchronous native routes.
    # It is not a fence for arbitrary asynchronous Core operations.
    thread = await read_thread(request, session)
    facts = {**thread, "queue": await read_queue(request, session), "turns": [],
             "executions": {"ended": True, "items": []}, "background": [], "realtime": [], "goal": None}
    if thread["state"] == "systemError" or thread["state"] == "active" and not include_active:
        return facts
    if thread["state"] == "notLoaded":
        # Persisted unfinished commands/realtime may survive a crash. Current
        # NotLoaded does not turn that history into running execution evidence.
        facts["turns"], facts["history_complete"] = await optional_turns(request, session)
        return facts
    # Loaded paginated history supports the item/timeline reads required below.
    # Its full-history read also awaits pending canonical writes and propagates
    # persistence errors before replying. Metadata-only reads do not do so.
    if thread["history_mode"] != "paginated":
        raise ValueError("loaded native history does not support required paginated reads")
    if await read_thread(request, session, include_turns=True) != thread:
        raise ValueError("native source changed during its canonical persistence read")
    facts["turns"] = await read_turns(request, session)
    facts["history_complete"] = True
    facts["executions"] = await read_executions(request, session)
    async for page in pages(request, "thread/backgroundTerminals/list", session):
        for item in page["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("itemId"), str):
                raise ValueError("native background identity is unavailable")
            facts["background"].append((item["itemId"], item.get("processId")))
    goal = await request("thread/goal/get", {"threadId": session})
    if not isinstance(goal, dict):
        raise ValueError("native scheduled work coverage is unavailable")
    goal = goal.get("goal")
    if goal is not None:
        if (not isinstance(goal, dict) or goal.get("threadId") != session or goal.get("status") not in {
                "active", "paused", "blocked", "usageLimited", "budgetLimited", "complete"}):
            raise ValueError("native scheduled work status is unavailable")
        facts["goal"] = goal["status"]
    timeline = await request("thread/timeline/list", {"threadId": session, "limit": 100})
    if not isinstance(timeline, dict) or not isinstance(timeline.get("data"), list):
        raise ValueError("native realtime coverage is unavailable")
    active = timeline.get("activeRealtimeSessionAtPageStart")
    if active is not None and not isinstance(active, str):
        raise ValueError("native realtime boundary is unavailable")
    active = {active} if active else set()
    for entry in timeline["data"]:
        if (not isinstance(entry, dict) or entry.get("type") not in {"item", "realtime", "turnStarted", "turnCompleted"}
                or type(entry.get("position")) is not int or entry["position"] < 0):
            raise ValueError("native realtime identity is unavailable")
        if entry["type"] == "item":
            item = entry.get("item")
            if (not isinstance(item, dict) or item.get("type") not in THREAD_ITEM_TYPES
                    or not isinstance(item.get("id"), str) or not item["id"]
                    or not isinstance(entry.get("turnId"), str) or not entry["turnId"]):
                raise ValueError("native timeline item identity is unavailable")
        elif entry["type"] in {"turnStarted", "turnCompleted"}:
            if (not isinstance(entry.get("turnId"), str) or not entry["turnId"] or entry["type"] == "turnCompleted"
                    and entry.get("status") not in {"completed", "interrupted", "failed", "inProgress"}):
                raise ValueError("native timeline turn identity is unavailable")
        if entry["type"] != "realtime":
            continue
        item = entry.get("item")
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
                or not isinstance(item.get("realtimeSessionId"), str) or not item["realtimeSessionId"]
                or item.get("type") not in {"realtimeSessionStarted", "realtimeSessionClosed", "transcriptSegment", "bemItemPromoted"}):
            raise ValueError("native realtime identity is unavailable")
        if item.get("type") == "realtimeSessionStarted":
            active.add(item["realtimeSessionId"])
        elif item.get("type") == "realtimeSessionClosed":
            if item.get("outcome") not in {"ended", "failed"}:
                raise ValueError("native realtime closure is unavailable")
            active.discard(item["realtimeSessionId"])
    facts["realtime"] = sorted(active)
    return facts


@contextmanager
def _queue_outcome(operation):
    """Top-level queue CLI operations preserve uncertainty through all cleanup exits."""
    sent = False
    def mark_sent():
        nonlocal sent
        sent = True
    try:
        yield mark_sent
    except BaseException as error:
        if not sent:
            raise
        raise ValueError(f"queue {operation} is uncertain; preserve state and do not replay") from error


async def queue_input(home: Path, session: str, text: str, *, transcript: Path | None = None):
    """Submit once; normalize post-send cancellation at the top-level CLI boundary."""
    from github_plan_sessions import native_helpers
    native_helpers()
    from codex_session_proxy import source_endpoint
    source_endpoint(session, home, transcript=transcript)
    client_id = str(uuid.uuid4())
    params = {"threadId": session, "clientUserMessageId": client_id,
              "input": [{"type": "text", "text": text, "text_elements": []}]}
    # Native echoes the full input. Escaped/multibyte valid text can exceed the
    # observer's fixed cap; bound this acknowledgement to the actual request.
    allowance = max(1024 * 1024, len(json.dumps(params, ensure_ascii=True).encode()) + 65536)
    with _queue_outcome("submission") as mark_sent:
        with closure.resume_guard(session, method="thread/queue/add", home=home):
            async with connection(home, max_msg_size=allowance) as request:
                mark_sent()
                result = await request("thread/queue/add", params)
                entry = result.get("queuedSubmission") if isinstance(result, dict) else None
                if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]
                        or entry.get("clientUserMessageId") != client_id or entry.get("input") != params["input"]):
                    raise ValueError("native queue acknowledgement is unavailable")
                receipt = {"queuedSubmissionId": entry["id"], "clientUserMessageId": client_id}
    return receipt


class _SourceProcess(NamedTuple):
    source: dict
    saved: dict | None
    effective: dict
    cutoff: datetime | None
    context: object = None


class _SourceEndMismatch(ValueError):
    """This verified End cannot cover the selected generation's actual facts."""


def _source_process(entry, saved, context=None, *, recorded=None):
    """Select actual source identity and its recorder-owned exit boundary."""
    from github_plan_sessions import process_liveness
    from github_plan_release import stamp
    source = {**entry, **(saved or {})}
    generation = source.get("resumed_generation")
    if generation is not None and (not isinstance(generation, dict) or not generation):
        raise ValueError("native resumed generation is unavailable")
    if context is not None:
        if (saved is None or source.get("session_id") != context.session or source.get("harness") != "codex"
                or not isinstance(generation, dict)
                or any(not generation.get(key) for key in (
                    "launch_pid", "launch_generation", "launch_started_at", "pid", "tty", "codex_home", "transcript"))
                or any(not isinstance(generation.get(key), str) or not generation[key].strip() for key in (
                    "launch_generation", "launch_started_at", "tty", "codex_home", "transcript"))
                or any(type(generation.get(key)) is not int or generation[key] <= 0 for key in ("pid", "launch_pid"))
                or generation["pid"] != generation["launch_pid"]
                or not source.get("resumed_at")
                or stamp(generation["launch_started_at"]) > stamp(source["resumed_at"])):
            raise ValueError("canonical release does not cover the retained native generation")
        if (stamp(generation["launch_started_at"]) > context.ended_at
                or stamp(source["resumed_at"]) > context.ended_at):
            raise _SourceEndMismatch("canonical release predates the retained native generation")
    effective = {**source, **(generation or {})}
    effective.pop("tty_exited_at", None)
    effective.pop("_tty_owner", None)
    if generation:
        launch_keys = ("launch_pid", "launch_generation", "launch_started_at", "pid", "tty", "codex_home")
        terminal_keys = ("tmux_session_id", "tmux_socket", "window_id", "tab_id", "iterm_session_id")
        for key in terminal_keys:
            effective.pop(key, None)
        if all(entry.get(key) == generation.get(key) for key in launch_keys):
            # Fresh terminal coordinates belong only to an owned matching
            # recovery ledger; an IDs-only observer cannot guess them.
            for key in terminal_keys:
                if key in entry:
                    effective[key] = entry[key]
    # Later finish boundaries use this invocation's recorder output, including
    # when the actual saved source is an open resumed generation or absent.
    # Initial selection never accepts a caller's cutoff as recorded evidence.
    closed = recorded if recorded is not None else (saved if not generation else None)
    if recorded is not None and (recorded.get("process_exit_verified") is not True or not recorded.get("ended_at")):
        raise ValueError("recorded closure lacks a verified process exit boundary")
    if closed and closed.get("process_exit_verified") is True and closed.get("ended_at"):
        ended = stamp(closed["ended_at"])
        cutoff = stamp(closed["tty_exited_at"]) if closed.get("tty_exited_at") else ended
        if cutoff > ended:
            raise ValueError("native TTY exit cutoff is newer than closure")
        if "tty_exited_at" in closed:
            # Preserve the recorder's representation for the independent
            # saved-row identity comparison, rather than a caller hint.
            effective["tty_exited_at"] = closed["tty_exited_at"]
    else:
        cutoff = process_liveness().native_exit_cutoff(effective)
        if cutoff is None:
            from github_plan_sessions import native_helpers
            native_helpers()
            import release_session_claims
            owner = process_liveness().replacement_tty_owner(effective, release_session_claims.inventory,
                                                           started_at=closure.process_started_at)
            if owner is not None:
                effective["_tty_owner"] = owner[:2]
                cutoff = owner[1]
        if cutoff is not None:
            effective["tty_exited_at"] = cutoff.isoformat()
    # A canonical closed-session receipt bounds this exact saved generation.
    # Keep it separate from caller hints and from persisted terminal metadata.
    if context is not None:
        if cutoff is not None and cutoff > context.ended_at:
            raise _SourceEndMismatch("known source exit is newer than the canonical closed-session evidence")
        if cutoff is None:
            cutoff = context.ended_at
    return _SourceProcess(source, saved, effective, cutoff, context)


def recheck_source_process(process):
    """Fresh process absence, using the same generation and observed cutoff."""
    from github_plan_sessions import native_helpers
    native_helpers()
    import release_session_claims
    release_session_claims._verify_process_exit(process.effective, process.cutoff, lambda: process.saved)


def verify_source_process(entry, *, _context=None):
    """Cheap shared process proof; caller timestamps never supply an exit cutoff."""
    saved = closure.source_records({entry["session_id"]}).get(entry["session_id"])
    process = _source_process(entry, saved, _context)
    recheck_source_process(process)
    return process


def _authenticated_paths(snapshots, homes):
    paths = {str(candidate.resolve()): [path for path in snapshots if any(
        Path(path).is_relative_to((candidate / root).resolve()) for root in ("sessions", "archived_sessions"))]
        for candidate in homes}
    return {home: copies for home, copies in paths.items() if copies}


def source_snapshot(entry, process=None):
    """Only file/process reads; safe to await in a cancellable worker thread."""
    from github_plan_sessions import codex_homes
    if process is None:
        process = verify_source_process(entry)
    else:
        if closure.source_records({entry["session_id"]}).get(entry["session_id"]) != process.saved:
            raise ValueError("source generation changed during native proof")
        recheck_source_process(process)
    source, saved, effective, _cutoff, context = process
    session = entry["session_id"]
    # Minimal resume metadata needs canonical closed-source authority; it never
    # acquires original launch ownership fields from the observer.
    if source.get("harness") != "codex" or (source.get("supervisor_owned") is not True and context is None):
        raise ValueError("native end proof requires the original owned source identity")
    generation = source.get("resumed_generation")
    home = Path(effective["codex_home"]).expanduser().resolve(strict=True)
    primary = Path(effective["transcript"]).resolve(strict=True)
    if generation is None and not any(primary.is_relative_to((home / root).resolve()) for root in ("sessions", "archived_sessions")):
        raise ValueError("native source home and transcript disagree")
    configured = set(codex_homes(strict=True, configured_only=True))
    if home not in configured and saved is None:
        raise ValueError("unconfigured native source lacks retained launch identity")
    scope = closure.action_homes(closure.lease_state(session))
    homes = {home, *(Path(value) for value in scope["known"])}
    if source.get("codex_home") is not None:
        homes.add(Path(source["codex_home"]).expanduser().resolve(strict=True))
    snapshots = closure.transcripts(session, str(primary), include_status=True, additional_homes=homes)
    if context is not None:
        from github_plan_release import stamp
        for snapshot in snapshots.values():
            status = snapshot["status"]
            # Delivery timestamps are not actual source activity (#2325).
            if any(status.get(key) and stamp(status[key]) > context.ended_at
                   for key in ("turn_started_at", "turn_completed_at", "message_at")):
                raise _SourceEndMismatch("source actual turn is newer than the canonical closed-session evidence")
    paths = _authenticated_paths(snapshots, homes | configured)
    # Relevant authenticated copies remain current-work coverage even if an old
    # buggy finish already cleared the marker that originally touched that home.
    homes.update(Path(value) for value in paths)
    if any(snapshot["status"]["background_tasks"] for snapshot in snapshots.values()):
        raise ValueError("source background work remains")
    return {"source": saved, "entry": source, "process": process, "home": str(home), "path": str(primary),
            "homes": sorted(str(value.resolve()) for value in homes), "home_paths": paths, "transcripts": snapshots}


def _require_runtime_quiet(facts, statuses):
    if any(status.get("background_tasks") or status.get("active_realtime_sessions")
           or status.get("realtime_coverage") is False for status in statuses):
        raise ValueError("canonical runtime work remains or its lifecycle is unavailable")
    if (not facts["executions"]["ended"] or facts["background"] or facts["realtime"] or facts["goal"] == "active"):
        raise ValueError("native execution or scheduled work remains")


def require_quiet(facts, snapshot, action):
    if facts["path"] != snapshot["path"] or facts["state"] not in {"idle", "notLoaded"}:
        raise ValueError("native source identity changed or remains active")
    if facts["queue"]:
        raise ValueError("native queued work remains")
    status = snapshot["transcripts"][snapshot["path"]]["status"]
    for copy in snapshot["transcripts"].values():
        other = copy["status"]
        order = other.get("actual_turn_ids", [])
        if (status["turn_id"] in order and other["turn_id"] in order
                and order.index(other["turn_id"]) > order.index(status["turn_id"])):
            raise ValueError("another authenticated source copy has a later actual turn")
    turns = facts["turns"]
    if facts["state"] == "notLoaded":
        # Only native order anchored at our actual source turn proves a later
        # indexed turn. Unknown/old/empty history is not a new runtime proof.
        position = next((i for i, turn in enumerate(turns) if turn["id"] == status["turn_id"]), None)
        if position is not None and position > 0:
            raise ValueError("native actual turn is later than the source verdict")
        return
    _require_runtime_quiet(facts, [status])
    order = status.get("actual_turn_ids", []) or [status["turn_id"]]
    indices = [order.index(turn["id"]) for turn in turns if turn["id"] in order]
    if (not turns or not status["turn_id"] or turns[0]["id"] not in order
            or status["turn_id"] not in order or indices != sorted(indices, reverse=True)
            or order.index(turns[0]["id"]) > order.index(status["turn_id"])
            or turns[0]["status"] == "inProgress"):
        raise ValueError("native actual turn and source final verdict disagree")
    if action.get("action_pending") and action.get("action_kind") != "routed":
        from github_plan_release import stamp
        if (not status["turn_completed_at"] or not status.get("message_at")
                or stamp(status["turn_completed_at"]) <= stamp(action["action_at"])
                or stamp(status["message_at"]) <= stamp(action["action_at"])):
            raise ValueError("opaque native action needs a fresh final safe verdict")


def local_snapshot(snapshot, facts, home):
    if facts["path"] not in snapshot["home_paths"].get(home, []):
        raise ValueError("native home/path is not an authenticated source copy")
    return {**snapshot, "path": facts["path"], "transcripts": {
        path: snapshot["transcripts"][path] for path in snapshot["home_paths"][home]}}


def home_action(action, home):
    scope = closure.action_homes(action)
    return action if scope["unscoped"] or home in scope["known"] else {}


def current_facts(value):
    result = {key: value[key] for key in ("id", "path", "state", "queue", "background", "realtime", "goal")}
    if value["state"] != "notLoaded":
        result.update(executions_ended=value["executions"]["ended"])
    return result


async def prove_end(entry, request=None, *, process=None):
    """Return immutable read facts; the owner clears its barrier after awaiting."""
    session = entry["session_id"]
    action = closure.lease_state(session)
    before = (await asyncio.to_thread(source_snapshot, entry) if process is None else
              await asyncio.to_thread(source_snapshot, entry, process))
    async with AsyncExitStack() as connections:
        readers = {}
        for home in before["homes"]:
            native = request if request is not None and getattr(request, "codex_home", None) == home else await connections.enter_async_context(connection(Path(home)))
            readers[home] = native
        first = {}
        for home, native in readers.items():
            facts = await read_thread_facts(native, session)
            require_quiet(facts, local_snapshot(before, facts, home), home_action(action, home))
            first[home] = facts
        final = {}
        for home, native in readers.items():
            facts = await read_thread_facts(native, session)
            if current_facts(first[home]) != current_facts(facts):
                raise ValueError("native source changed during end proof")
            final[home] = facts
        # The last native persistence read can commit buffered events. Read the
        # canonical source after that fence, never accept an earlier snapshot.
        try:
            after = await asyncio.to_thread(source_snapshot, entry, before["process"])
        except _SourceEndMismatch as error:
            # Qualification was already complete before the RPC sandwich.
            # New activity during it is a changed source, not another candidate.
            raise ValueError("native source changed during end proof") from error
        for home, facts in final.items():
            require_quiet(facts, local_snapshot(after, facts, home), home_action(action, home))
        if before != after:
            raise ValueError("native source changed during end proof")
    return {"snapshot": after, "facts": final[after["home"]], "homes": final, "action": action}


def _require_current_proof(session, proof):
    """Require the held lease and the source/action identity behind this proof."""
    if session not in (closure._LEASES.get() or set()):
        raise ValueError("native resolution has no ownership lease")
    if closure.lease_state(session) != proof["action"] or closure.source_records({session}).get(session) != proof["snapshot"]["source"]:
        raise ValueError("native action or source identity changed before resolution")


def resolve_pending(session, proof):
    """Only the actual lease-owning caller writes, never an off-loop reader."""
    _require_current_proof(session, proof)
    require_proof_quiet(proof)
    closure.set_action_pending(session, False)


def require_proof_quiet(proof):
    for home, facts in proof["homes"].items():
        require_quiet(facts, local_snapshot(proof["snapshot"], facts, home), home_action(proof["action"], home))


def _require_cancel_original(entry):
    if entry.get("_inventory") or entry.get("_backfill"):
        raise ValueError("queue cleanup requires original source identity without inventory or backfill exemptions")


def _cancel_selected(path, session, *, ledger=False):
    """Read one integrity-protected authority row; unrelated rows are not its identity."""
    from github_plan_sessions import native_helpers
    native_helpers()
    import session_record
    path = Path(path).expanduser().resolve(strict=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o022:
            raise ValueError("queue cleanup requires an original regular source record owned by the current user and not writable by group or others")
        value = json.load(stream)
        if closure.transcript_identity(before) != closure.transcript_identity(os.fstat(stream.fileno())):
            raise ValueError("source record changed while reading")
    if ledger:
        if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
            raise ValueError("source ledger must contain session objects")
        matches = [row for row in value if row.get("session_id") == session]
        if len(matches) != 1 or matches[0].get("supervisor_owned") is not True:
            raise ValueError("source is not uniquely Supervisor-owned in the original ledger")
        value = matches[0]
    if not isinstance(value, dict) or value.get("session_id") != session or value.get("harness") != "codex":
        raise ValueError("queue cleanup requires the original Codex source")
    _require_cancel_original(value)
    return session_record.normalize_ledger_entry(path, value)


def _cancel_saved(session):
    return closure.source_records({session}).get(session)


def _cancel_generation(entry, saved):
    """Return the matching current row as negative evidence, never ownership."""
    if saved is None:
        return
    current = saved.get("resumed_generation") if "resumed_generation" in saved else saved
    if not isinstance(current, dict) or not current:
        raise ValueError("current source generation is unavailable")
    keys = ("harness", "pid", "tty", "launch_pid", "launch_generation", "launch_started_at", "codex_home", "transcript",
            "tmux_session_id", "tmux_socket", "window_id", "tab_id", "iterm_session_id")
    def identity(key, value):
        if key in {"codex_home", "transcript"} and isinstance(value, str):
            return str(Path(value).expanduser().resolve())
        return value.removeprefix("/dev/") if key == "tty" and isinstance(value, str) else value
    for key in keys:
        if "resumed_generation" not in saved and key in {"launch_pid", "launch_generation", "launch_started_at"} and key not in entry:
            continue
        if key in current and identity(key, current[key]) != identity(key, entry.get(key)):
            raise ValueError("original source authority does not match the current generation")
    return current


def _cancel_origin(entry, action):
    from github_plan_sessions import codex_homes
    primary = Path(entry["transcript"]).resolve(strict=True)
    configured = set(codex_homes(strict=True, configured_only=True))
    known = {Path(value).resolve(strict=True) for value in closure.action_homes(action)["known"]}
    if entry.get("codex_home"):
        if not isinstance(entry["codex_home"], str) or not Path(entry["codex_home"]).expanduser().is_absolute():
            raise ValueError("original codex_home must be absolute")
        home = Path(entry["codex_home"]).expanduser().resolve(strict=True)
    else:
        matches = {candidate for candidate in configured | known if any(
            primary.is_relative_to((candidate / root).resolve()) for root in ("sessions", "archived_sessions"))}
        if len(matches) != 1:
            raise ValueError("retain the original absolute codex_home in the source record")
        home = matches.pop()
    if not any(primary.is_relative_to((home / root).resolve()) for root in ("sessions", "archived_sessions")):
        raise ValueError("original source home and transcript disagree")
    return primary, home, configured, known


def _cancel_files(entry, action, reader=None, retained_homes=(), transcript_roots=None):
    primary, home, configured, known = _cancel_origin(entry, action)
    homes = {home, *known, *retained_homes}
    snapshots = closure.transcript_facts(entry["session_id"], str(primary), include_status=True,
                                         additional_homes=homes, additional_roots=transcript_roots, reader=reader)
    paths = _authenticated_paths(snapshots, configured | homes)
    homes.update(Path(value) for value in paths)
    return {"home": str(home), "path": str(primary), "homes": sorted(str(value) for value in homes),
            "home_paths": paths, "transcripts": snapshots}


def _cancel_quiet(facts, snapshot, home, target):
    local_snapshot(snapshot, facts, home)  # Exact native home/path binding in every state.
    if facts["state"] == "notLoaded":
        return
    if home == target or facts["state"] != "idle" or facts["queue"]:
        raise ValueError("queue cleanup requires a NotLoaded target and quiet empty loaded peers")
    statuses = [snapshot["transcripts"][path]["status"] for path in snapshot["home_paths"][home]]
    _require_runtime_quiet(facts, statuses)


async def cancel_queue(home: Path, session: str, *, ledger: Path | None = None,
                       termination_record: Path | None = None, apply=False,
                       queued_submission_id: str | None = None, entry_sha256: str | None = None):
    """Delete one observed native queue ID, never load a thread or clear its barriers.

    Native delete has no digest CAS: the digest guards our last observation, not
    an unmanaged same-ID edit concurrent with the deletion. Never replay uncertainty.
    This top-level CLI coroutine normalizes cancellation after deletion may start.
    """
    import github_plan_termination as termination
    if (ledger is None) == (termination_record is None):
        raise ValueError("supply one original owned ledger or original termination record")
    if apply and (not queued_submission_id or not isinstance(entry_sha256, str) or len(entry_sha256) != 64
                  or any(character not in "0123456789abcdef" for character in entry_sha256)):
        raise ValueError("apply requires one explicit queue ID and its full entry SHA256")
    target = str(home.expanduser().resolve(strict=True))
    authority_path = ledger if ledger is not None else termination_record
    def authority():
        return _cancel_selected(authority_path, session, ledger=ledger is not None)
    with _queue_outcome("deletion") as mark_sent:
        with closure.ownership(session, create=apply) as held:
            entry, saved, action = authority(), _cancel_saved(session), closure.lease_state(session)
            reader = None if ledger is not None else lambda lines: termination.transcript_records(entry, lines)
            retained_homes = {Path(saved["codex_home"]).expanduser().resolve(strict=True)} if saved and saved.get("codex_home") else set()
            _primary, original_home, configured, _known = _cancel_origin(entry, action)
            current = _cancel_generation({**entry, "codex_home": str(original_home)}, saved)
            if Path(target) not in configured | retained_homes | {original_home}:
                raise ValueError("target must be configured or a validated retained source home")
            process = _source_process(entry, saved) if ledger is not None else None
            if process is not None:
                _require_cancel_original(process.effective)
            termination_process = None
            def check_identity():
                if authority() != entry or _cancel_saved(session) != saved or closure.lease_state(session) != action:
                    raise ValueError("source authority, generation or ownership state changed")
            def source():
                nonlocal termination_process
                check_identity()
                if process is not None:
                    recheck_source_process(process)
                elif termination_process is None:
                    termination_process = termination._verify_queue_source(entry, session,
                        latest_pid=current.get("launch_pid") if current else None)
                else:
                    termination._recheck_queue_process(termination_process)
                files = _cancel_files(entry, action, reader, retained_homes,
                                      termination_process.roots if termination_process is not None else None)
                if ledger is None:
                    from datetime import timezone
                    now, ended = datetime.now(timezone.utc), termination.stamp(entry["ended_at"])
                    for snapshot in files["transcripts"].values():
                        termination._transcript_activity([termination.stamp(snapshot["latest"])], snapshot["identity"][3],
                            snapshot["truncated"], now, ended)
                return files
            before = await asyncio.to_thread(source)
            if target not in before["homes"]:
                raise ValueError("target has no authenticated copy of the original source")
            async with AsyncExitStack() as connections:
                readers = {value: await connections.enter_async_context(connection(Path(value))) for value in before["homes"]}
                async def facts():
                    return {value: await read_thread_facts(request, session) for value, request in readers.items()}
                first = await facts()
                baseline = await asyncio.to_thread(source)  # After loaded canonical persistence fences.
                if baseline["homes"] != before["homes"] or baseline["home_paths"] != before["home_paths"]:
                    raise ValueError("authenticated source copies changed")
                for value, observed in first.items():
                    _cancel_quiet(observed, baseline, value, target)
                second = await facts()
                after = await asyncio.to_thread(source)
                if after != baseline or any(current_facts(first[value]) != current_facts(observed) for value, observed in second.items()):
                    raise ValueError("source changed during queue preview")
                for value, observed in second.items():
                    _cancel_quiet(observed, after, value, target)
                queue = second[target]["queue"]
                def identities(observed):
                    return [{"queuedSubmissionId": key, "clientUserMessageId": client, "sha256": digest}
                            for key, (client, digest) in sorted(observed.items())]
                entries = identities(queue)
                if not apply:
                    result = {"ok": True, "preview": True, "codexHome": target, "entries": entries,
                            "otherHomes": {value: identities(observed["queue"]) for value, observed in second.items() if value != target},
                            "notice": "Apply acquires ownership and repeats every check." if not held else
                                      "Apply repeats every check under ownership."}
                else:
                    if queued_submission_id not in queue or queue[queued_submission_id][1] != entry_sha256:
                        raise ValueError("selected native queue entry or digest changed")
                    # Last metadata-only read before the sole mutation. LoadedIdle delete
                    # can wake OTHER input through the daemon's revision watcher.
                    metadata = await read_thread(readers[target], session)
                    if metadata["state"] != "notLoaded" or metadata["path"] != second[target]["path"]:
                        raise ValueError("target loaded or changed before queue deletion")
                    if process is not None:
                        recheck_source_process(process)
                    else:
                        termination._recheck_queue_process(termination_process)
                    check_identity()
                    mark_sent()
                    ack = await readers[target]("thread/queue/delete", {"threadId": session,
                                                                    "queuedSubmissionId": queued_submission_id})
                    if not isinstance(ack, dict) or ack.get("deleted") is not True:
                        raise ValueError("native deletion acknowledgement is unavailable")
                    final = await facts()
                    final_source = await asyncio.to_thread(source)
                    if final_source != baseline:
                        raise ValueError("source changed after queue deletion")
                    for value, observed in final.items():
                        _cancel_quiet(observed, final_source, value, target)
                        expected = {key: item for key, item in second[value]["queue"].items()
                                    if value != target or key != queued_submission_id}
                        if observed["queue"] != expected or observed["path"] != second[value]["path"]:
                            raise ValueError("native queue readback changed")
                    result = {"ok": True, "codexHome": target, "queuedSubmissionId": queued_submission_id, "deleted": True,
                            "remainingQueues": {value: len(observed["queue"]) for value, observed in final.items()}}
    return result
