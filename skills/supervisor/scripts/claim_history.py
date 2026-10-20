# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read native child histories without inventing a second session registry."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

import claim_receipts


def delegated(records, harness):
    return any(name in {"Agent", "Task", "spawn_agent", "collaboration.spawn_agent"}
               for record in records for _, name, _ in claim_receipts.calls_and_results(record, harness)[0])


def fingerprint(records):
    return hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_parent(source):
    """Validate native nested source data before reading its parent edge."""
    if not isinstance(source, dict):
        raise ValueError("native child source is not an object")
    subagent = source.get("subagent", {})
    if not isinstance(subagent, dict):
        raise ValueError("native child subagent source is not an object")
    spawn = subagent.get("thread_spawn", {})
    if not isinstance(spawn, dict):
        raise ValueError("native child thread-spawn source is not an object")
    return spawn.get("parent_thread_id")


def inline_history(records):
    """User forks name their source without copying another identity inline."""
    metadata = [r["payload"] for r in records if r.get("type") == "session_meta"]
    return bool(metadata and ("subagent_history_start_ordinal" in metadata[0]
                              or len({meta.get("id") for meta in metadata}) > 1))


def child_records(records, harness, session, context=None):
    """Separate a verified native fork's local rows from inherited history.

    Only verified native edges supply ancestry, including a fork checking its
    own local work. Raw child rows remain available for the coverage hash.
    """
    identities = ({r.get("sessionId") for r in records if r.get("sessionId")} if harness == "claude" else
                  {r.get("payload", {}).get("id") for r in records if r.get("type") == "session_meta"})
    if harness != "codex" or context is None:
        if identities != {session}:
            raise ValueError("native transcript identity is unavailable or differs")
        return records
    if not records or records[0].get("type") != "session_meta" or records[0]["payload"].get("id") != session:
        raise ValueError("child rollout lacks its own initial native metadata")
    meta = records[0]["payload"]
    if not inline_history(records):
        if identities != {session}:
            raise ValueError("native transcript identity is unavailable or differs")
        return records
    parent, ancestors = context["parent"], context["ancestors"]
    if (meta.get("forked_from_id") != parent or meta.get("parent_thread_id") != parent
            or source_parent(meta.get("source", {})) != parent):
        raise ValueError("native fork parent differs from its verified edge")
    boundary = meta.get("subagent_history_start_ordinal")
    if (type(boundary) is not int or not 1 < boundary < len(records)
            or any(type(row.get("ordinal")) is not int or row["ordinal"] != index
                   for index, row in enumerate(records))):
        raise ValueError("native fork history boundary is unavailable or malformed")
    marker = records[boundary]
    payload = marker.get("payload", {})
    if (marker.get("type") != "event_msg" or payload.get("type") != "thread_settings_applied"
            or payload.get("thread_id") != session):
        raise ValueError("native fork history boundary does not identify its child")
    inherited = {r["payload"]["id"] for r in records[1:boundary] if r.get("type") == "session_meta"}
    local = [records[0], *records[boundary:]]
    if (parent not in inherited or not inherited <= set(ancestors)
            or any(r["payload"]["id"] != session for r in local if r.get("type") == "session_meta")):
        raise ValueError("native fork metadata differs from its verified ancestry")
    return local


def codex_homes(path):
    homes = {Path.home() / ".codex", Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))}
    homes.update(p.parent for p in path.parents if p.name in {"sessions", "archived_sessions"})
    try:
        import account_choice
        homes.update(Path(a["env"].get("CODEX_HOME", str(Path.home() / ".codex")))
                     for a in account_choice.load_config()["accounts"] if a["provider"] == "openai")
    except (OSError, ValueError, KeyError):
        pass  # The native parent index below must still establish coverage.
    return sorted({p.expanduser().resolve() for p in homes})


@contextmanager
def native_databases(path, homes=None):
    """Open the same read-only native edge indexes for roots and children."""
    with ExitStack() as stack:
        databases = []
        for home in codex_homes(path) if homes is None else homes:
            candidates = [p for p in Path(home).glob("state_*.sqlite") if re.fullmatch(r"state_\d+", p.stem)]
            if candidates:
                database = max(candidates, key=lambda p: int(p.stem.split("_")[-1]))
                try:
                    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
                except sqlite3.Error as error:
                    raise ValueError("native child index cannot be read") from error
                stack.callback(connection.close)
                databases.append(connection)
        yield databases


def root_context(path, session):
    """Prove a fork root's own rollout and complete ancestor chain natively."""
    path = Path(path).expanduser().resolve()
    ancestors, visited, current = [], set(), session
    with native_databases(path) as databases:
        while True:
            if current in visited or len(visited) >= 128:
                raise ValueError("native fork root ancestry exceeds its coverage bound")
            visited.add(current)
            matches = set()
            for database in databases:
                try:
                    rows = database.execute(
                        "SELECT t.rollout_path,t.source,e.parent_thread_id FROM threads t "
                        "LEFT JOIN thread_spawn_edges e ON e.child_thread_id=t.id WHERE t.id=?", (current,)).fetchall()
                except sqlite3.Error as error:
                    raise ValueError("native child index schema/read is unavailable") from error
                for rollout, source, parent in rows:
                    if not rollout or not source:
                        raise ValueError("native fork ancestor lacks its rollout")
                    try:
                        native_source = json.loads(source)
                    except json.JSONDecodeError:
                        # Native indexes store unit source variants (cli,
                        # vscode, exec) as bare names, but spawned sources
                        # remain JSON objects carrying the actual parent.
                        if parent is not None or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", source):
                            raise
                        native_source = source
                    if not isinstance(native_source, (str, dict)):
                        raise ValueError("native fork ancestor source is malformed")
                    source_id = source_parent(native_source) if isinstance(native_source, dict) else None
                    if source_id != parent:
                        raise ValueError("native fork ancestor parent differs from its edge")
                    matches.add((str(Path(rollout).expanduser().resolve()), parent))
            if len(matches) != 1:
                raise ValueError("native fork root ancestry is absent or differs across indexes")
            rollout, parent = matches.pop()
            if current == session and (rollout != str(path) or parent is None):
                raise ValueError("native fork root differs from its indexed rollout or edge")
            if parent is None:
                break
            ancestors.append(parent)
            current = parent
    return {"parent": ancestors[0], "ancestors": tuple(reversed(ancestors))}


def root_records(path, harness, session, records):
    """Read a fork as its own session without trusting copied identity rows."""
    if harness == "codex" and records and records[0].get("type") == "session_meta":
        meta = records[0]["payload"]
        if inline_history(records):
            if meta.get("id") != session:
                raise ValueError("native fork root identity differs")
            return child_records(records, harness, session, root_context(path, session))
    return child_records(records, harness, session)


def spawn_prefix(records, harness, child, *, agent_path=None):
    """Prove parent order at a child launch, never from wall-clock stamps."""
    calls, matches = {}, []
    names = {"Agent", "Task"} if harness == "claude" else {"spawn_agent", "collaboration.spawn_agent"}
    for index, record in enumerate(records):
        invocations, results = claim_receipts.calls_and_results(record, harness)
        for identifier, name, value in invocations:
            arguments = claim_receipts.arguments(value)
            resuming = harness == "claude" and isinstance(arguments, dict) and arguments.get("resume") is not None
            if name in names and not resuming:
                calls[identifier] = index
        if harness == "claude":
            started = record.get("toolUseResult")
            blocks = (record.get("message") or {}).get("content") or []
            if (isinstance(started, dict) and started.get("agentId") == child and len(results) == 1
                    and results[0] in calls
                    and not any(block.get("is_error") for block in blocks if isinstance(block, dict)
                                and block.get("type") == "tool_result")):
                matches.append(calls[results[0]])
            continue
        payload = record.get("payload") or {}
        if len(results) != 1 or results[0] not in calls:
            continue
        value = payload.get("output")
        try:
            value = json.loads(value) if isinstance(value, str) else value
        except ValueError:
            continue
        if (isinstance(value, dict) and not value.get("error")
                and (value.get("agent_id") == child or agent_path and value.get("task_name") == agent_path)
                and ("agent_id" not in value or value["agent_id"] == child)
                and (not agent_path or "task_name" not in value or value["task_name"] == agent_path)):
            matches.append(calls[results[0]])
    return records[:min(matches)] if matches else []


def unique_prefixes(prefixes):
    """Keep each immutable native segment once across converging histories."""
    seen, result = set(), []
    for prefix in prefixes:
        key = prefix["harness"], tuple(map(id, prefix["records"]))
        if key not in seen:
            seen.add(key)
            result.append(prefix)
    return result


def completed_prefixes(parent, records, nodes):
    """Return child prefixes linked to an observed native completion boundary.

    A quiet child can have resumed after an earlier completion. Match the
    completed turn or returned handback, never the child's current full file.
    """
    prefixes = []
    for child in nodes:
        if not child.get("parent_transcript") or child["parent_transcript"] != parent.get("transcript"):
            continue
        rows, harness = child["records"], child["harness"]
        ends = []
        if harness == "codex":
            for record in records:
                payload = record.get("payload") or {}
                item = payload.get("item") or {}
                if (record.get("type") != "event_msg" or payload.get("type") != "item_completed"
                        or payload.get("thread_id") != parent["session_id"]
                        or item.get("type") != "SubAgentActivity" or item.get("kind") != "completed"
                        or item.get("agent_thread_id") != child["session_id"]):
                    continue
                identifier = item.get("id", "")
                if not isinstance(identifier, str) or not identifier.startswith("subagent-completed-"):
                    continue
                turn = identifier.removeprefix("subagent-completed-")
                matches = [index + 1 for index, row in enumerate(rows)
                           if row.get("type") == "event_msg" and (row.get("payload") or {}).get("type") == "task_complete"
                           and (row.get("payload") or {}).get("turn_id") == turn]
                if len(matches) == 1:
                    ends.extend(matches)
        else:
            calls = {}
            key = Path(child["transcript"]).stem.removeprefix("agent-")
            for record in records:
                invocations, results = claim_receipts.calls_and_results(record, harness)
                for identifier, name, value in invocations:
                    args = claim_receipts.arguments(value)
                    if name in {"Agent", "Task"} and isinstance(args, dict) and not args.get("run_in_background"):
                        calls[identifier] = args
                report = record.get("toolUseResult")
                blocks = (record.get("message") or {}).get("content") or []
                if (not isinstance(report, dict) or report.get("agentId") != key
                        or report.get("status") != "completed" or report.get("isAsync")
                        or len(results) != 1 or results[0] not in calls
                        or any(b.get("is_error") for b in blocks if isinstance(b, dict))):
                    continue
                handback = (report.get("handbackReport") or {}).get("text")
                answer = report.get("content")
                matches, handbacks = [], {}
                for index, row in enumerate(rows):
                    uses, returned = claim_receipts.calls_and_results(row, harness)
                    for identifier, name, value in uses:
                        args = claim_receipts.arguments(value)
                        if name == "SubagentHandback" and isinstance(args, dict) and handback and args.get("message") == handback:
                            handbacks[identifier] = index
                    if (handback and row.get("toolEndsTurn") is True and len(returned) == 1 and returned[0] in handbacks
                            and not any(b.get("is_error") for b in (row.get("message") or {}).get("content", []) if isinstance(b, dict))):
                        matches.append(index + 1)
                    message = row.get("message") or {}
                    if (answer and row.get("type") == "assistant" and message.get("stop_reason") == "end_turn"
                            and message.get("content") == answer):
                        matches.append(index + 1)
                if len(set(matches)) == 1:
                    ends.extend(matches)
        if ends:
            end = max(ends)
            prefixes.extend(child.get("preceding_histories", []))
            prefixes.append({"harness": harness, "records": rows[:end]})
            prefixes.extend(completed_prefixes(child, rows[:end], nodes))
    return unique_prefixes(prefixes)


def collect(path, harness, session, records, read, *, homes=None):
    """Return root and quiet child snapshots; missing child evidence refuses.

    Codex's native edge index includes every status and points to each rollout.
    Claude's session-local subagent files retain the inherited session identity.
    The callback owns native identity and stable/complete-record validation.
    """
    path = Path(path).expanduser().resolve()
    nodes = [{"session_id": session, "harness": harness, "transcript": str(path), "records": records}]
    paths = []
    if harness == "claude":
        children = sorted((path.parent / session / "subagents").glob("agent-*.jsonl"))
        if not children and delegated(records, harness):
            raise ValueError("native delegated histories are unavailable")
        paths = [(session, child.resolve(), None) for child in children]
    else:
        with native_databases(path, homes) as databases:
            pending, visited, seen_paths = [session], set(), set()
            ancestors = root_context(path, session)["ancestors"] if inline_history(records) else ()
            ancestry = {session: ancestors}
            while pending:
                parent = pending.pop()
                if parent in visited:
                    continue
                if len(visited) >= 128:
                    raise ValueError("native child graph exceeds coverage bound")
                visited.add(parent)
                found_parent = False
                for database in databases:
                    try:
                        present = database.execute("SELECT 1 FROM threads WHERE id=?", (parent,)).fetchone()
                        if not present:
                            continue
                        found_parent = True
                        rows = database.execute(
                            "SELECT e.child_thread_id,t.rollout_path,t.source FROM thread_spawn_edges e "
                            "LEFT JOIN threads t ON t.id=e.child_thread_id WHERE e.parent_thread_id=?", (parent,)).fetchall()
                    except sqlite3.Error as error:
                        raise ValueError("native child index schema/read is unavailable") from error
                    for child_id, rollout, source in rows:
                        if not child_id or not rollout or not source:
                            raise ValueError("native child index lacks its rollout")
                        native_source = json.loads(source)
                        if source_parent(native_source) != parent:
                            raise ValueError("native child index parent differs")
                        ancestors = (*ancestry[parent], parent)
                        if child_id in ancestors or (child_id in ancestry and ancestry[child_id] != ancestors):
                            raise ValueError("native child index ancestry differs")
                        ancestry[child_id] = ancestors
                        child = Path(rollout).expanduser().resolve()
                        if (child_id, child) not in seen_paths:
                            paths.append((child_id, child, {"parent": parent, "ancestors": ancestors,
                                "agent_path": native_source.get("subagent", {}).get("thread_spawn", {}).get("agent_path")}))
                            seen_paths.add((child_id, child))
                        pending.append(child_id)
                if not found_parent and (parent != session or delegated(records, harness)):
                    raise ValueError("native parent is absent from configured child indexes")
    for child_id, child, context in paths:
        raw_records = read(child, harness, child_id, context)
        logical_records = child_records(raw_records, harness, child_id, context)
        parent = context["parent"] if context is not None else None
        if parent is not None:
            metadata = [r["payload"] for r in logical_records if r.get("type") == "session_meta"]
            if not metadata:
                raise ValueError("child rollout lacks native metadata")
            for meta in metadata:
                parents = [value for value in (meta.get("parent_thread_id"),
                           source_parent(meta.get("source", {}))) if value]
                if not parents or any(value != parent for value in parents):
                    raise ValueError("child rollout parent differs from its native edge")
        import session_record
        activity = session_record.summarize(logical_records, harness, inspect_claims=False)
        if not activity["at_turn_end"] or activity["background_tasks"]:
            raise ValueError(f"native child {child_id} still has unfinished work")
        parent_node = (next(node for node in nodes if node["session_id"] == parent) if parent is not None else nodes[0])
        preceding = []
        if harness == "codex" and inline_history(raw_records):
            boundary = raw_records[0]["payload"]["subagent_history_start_ordinal"]
            inherited = raw_records[1:boundary]
            # A copied fork-of-fork can combine native process/call IDs from
            # multiple histories. Keep its independently proven ancestor and
            # launch segments, rather than infer order from that mixed copy.
            if len({r["payload"]["id"] for r in inherited if r.get("type") == "session_meta"}) == 1:
                preceding.append({"harness": harness, "records": inherited})
        agent_path = context.get("agent_path") if context is not None else None
        if agent_path:
            meta = raw_records[0]["payload"]
            native_path = meta.get("source", {}).get("subagent", {}).get("thread_spawn", {}).get("agent_path")
            if meta.get("agent_path") != agent_path or native_path != agent_path:
                agent_path = None  # No task-name binding from an unverified child path.
        child_key = child.stem.removeprefix("agent-") if harness == "claude" else child_id
        prefix = spawn_prefix(parent_node["records"], harness, child_key, agent_path=agent_path)
        if prefix:
            preceding.append({"harness": harness, "records": prefix})
        nodes.append({"session_id": child_id, "harness": harness, "transcript": str(child),
                      "records": logical_records, "digest": fingerprint(raw_records), "preceding_histories": preceding,
                      "parent_transcript": parent_node["transcript"], "launch_prefix": prefix})
    # All sibling snapshots must be available before binding a later child's
    # launch prefix to a previously completed child's exact returned turn.
    for node in nodes[1:]:
        parent = next(n for n in nodes if n["transcript"] == node["parent_transcript"])
        node["preceding_histories"] = unique_prefixes([*parent.get("preceding_histories", []),
            *node["preceding_histories"], *completed_prefixes(parent, node.pop("launch_prefix"), nodes)])
    return nodes
