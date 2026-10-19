# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Native thread facts and exact typed queue input; never load a thread to read it."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, AsyncExitStack
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

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


async def read_thread_facts(request, session):
    # This serialized read fences the exact proven synchronous native routes.
    # It is not a fence for arbitrary asynchronous Core operations.
    thread = await read_thread(request, session)
    facts = {**thread, "queue": await read_queue(request, session), "turns": [],
             "executions": {"ended": True, "items": []}, "background": [], "realtime": [], "goal": None}
    if thread["state"] in {"active", "systemError"}:
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
            if (not isinstance(entry.get("turn_id"), str) or not entry["turn_id"] or entry["type"] == "turnCompleted"
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


async def queue_input(home: Path, session: str, text: str, *, transcript: Path | None = None):
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
    with closure.resume_guard(session, method="thread/queue/add", home=home):
        async with connection(home, max_msg_size=allowance) as request:
            if request.codex_home != str(home.resolve()):
                raise ValueError("native queue daemon home identity disagrees with the source")
            result = await request("thread/queue/add", params)
            entry = result.get("queuedSubmission") if isinstance(result, dict) else None
            if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]
                    or entry.get("clientUserMessageId") != client_id or entry.get("input") != params["input"]):
                raise ValueError("native queue acknowledgement is unavailable; do not replay")
            return {"queuedSubmissionId": entry["id"], "clientUserMessageId": client_id}


def verify_source_process(entry):
    """Cheap shared process proof; caller timestamps never supply an exit cutoff."""
    from github_plan_sessions import native_helpers, process_liveness
    from github_plan_release import stamp
    native_helpers()
    import release_session_claims
    session = entry["session_id"]
    saved = closure.source_records({session}).get(session)
    source = {**entry, **(saved or {})}
    generation = source.get("resumed_generation")
    if generation is not None and (not isinstance(generation, dict) or not generation):
        raise ValueError("native resumed generation is unavailable")
    effective = {**source, **(generation or {})}
    effective.pop("tty_exited_at", None)
    if generation:
        launch_keys = ("launch_pid", "launch_generation", "launch_started_at", "pid", "tty", "codex_home")
        if all(entry.get(key) == generation.get(key) for key in launch_keys):
            # Fresh terminal coordinates belong only to an owned matching
            # recovery ledger; an IDs-only observer cannot guess them.
            for key in ("tmux_session_id", "tmux_socket", "window_id", "tab_id", "iterm_session_id"):
                if key in entry:
                    effective[key] = entry[key]
    if not generation and saved and saved.get("process_exit_verified") is True and saved.get("ended_at"):
        ended = stamp(saved["ended_at"])
        cutoff = stamp(saved["tty_exited_at"]) if saved.get("tty_exited_at") else ended
        if cutoff > ended:
            raise ValueError("native TTY exit cutoff is newer than closure")
    else:
        cutoff = process_liveness().native_exit_cutoff(effective)
    release_session_claims.verify_process_exit(effective, ended_at=cutoff)
    return source, saved, effective


def source_snapshot(entry):
    """Only file/process reads; safe to await in a cancellable worker thread."""
    from github_plan_sessions import codex_homes
    source, saved, effective = verify_source_process(entry)
    session = entry["session_id"]
    # A minimal resume row is identity metadata, not ownership. An explicitly
    # owned caller may supply its original launch provenance; observation cannot.
    if source.get("harness") != "codex" or source.get("supervisor_owned") is not True:
        raise ValueError("native end proof requires the original owned source identity")
    generation = source.get("resumed_generation")
    home = Path(effective["codex_home"]).expanduser().resolve(strict=True)
    original_home = Path(source["codex_home"]).expanduser().resolve(strict=True)
    primary = Path(effective["transcript"]).resolve(strict=True)
    if generation is None and not any(primary.is_relative_to((home / root).resolve()) for root in ("sessions", "archived_sessions")):
        raise ValueError("native source home and transcript disagree")
    configured = set(codex_homes(strict=True, configured_only=True))
    if home not in configured and saved is None:
        raise ValueError("unconfigured native source lacks retained launch identity")
    scope = closure.action_homes(closure.lease_state(session))
    homes = {home, original_home, *(Path(value) for value in scope["known"])}
    snapshots = closure.transcripts(session, str(primary), include_status=True, additional_homes=homes)
    paths = {}
    for candidate in sorted(homes | configured):
        matches = [path for path in snapshots if any(Path(path).is_relative_to((candidate / root).resolve())
                                                    for root in ("sessions", "archived_sessions"))]
        if matches:
            paths[str(candidate.resolve())] = matches
    # Relevant authenticated copies remain current-work coverage even if an old
    # buggy finish already cleared the marker that originally touched that home.
    homes.update(Path(value) for value in paths)
    if any(snapshot["status"]["background_tasks"] for snapshot in snapshots.values()):
        raise ValueError("source background work remains")
    return {"source": saved, "entry": source, "home": str(home), "path": str(primary),
            "homes": sorted(str(value.resolve()) for value in homes), "home_paths": paths, "transcripts": snapshots}


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
    if status.get("active_realtime_sessions") or status.get("realtime_coverage") is False:
        raise ValueError("canonical realtime work remains or its lifecycle is unavailable")
    if (not facts["executions"]["ended"] or facts["background"] or facts["realtime"]
            or facts["goal"] == "active"):
        raise ValueError("native execution or scheduled work remains")
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


async def prove_end(entry, request=None):
    """Return immutable read facts; the owner clears its barrier after awaiting."""
    session = entry["session_id"]
    action = closure.lease_state(session)
    before = await asyncio.to_thread(source_snapshot, entry)
    async with AsyncExitStack() as connections:
        readers = {}
        for home in before["homes"]:
            native = request if request is not None and getattr(request, "codex_home", None) == home else await connections.enter_async_context(connection(Path(home)))
            if getattr(native, "codex_home", None) != home:
                raise ValueError("native daemon home identity disagrees with the source")
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
        after = await asyncio.to_thread(source_snapshot, entry)
        for home, facts in final.items():
            require_quiet(facts, local_snapshot(after, facts, home), home_action(action, home))
        if before != after:
            raise ValueError("native source changed during end proof")
    return {"snapshot": after, "facts": final[after["home"]], "homes": final, "action": action}


def resolve_pending(session, proof):
    """Only the actual lease-owning caller writes, never an off-loop reader."""
    if session not in (closure._LEASES.get() or set()):
        raise ValueError("native resolution has no ownership lease")
    if closure.lease_state(session) != proof["action"] or closure.source_records({session}).get(session) != proof["snapshot"]["source"]:
        raise ValueError("native action or source identity changed before resolution")
    require_proof_quiet(proof)
    closure.set_action_pending(session, False)


def require_proof_quiet(proof):
    for home, facts in proof["homes"].items():
        require_quiet(facts, local_snapshot(proof["snapshot"], facts, home), home_action(proof["action"], home))
