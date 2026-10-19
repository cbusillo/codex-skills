# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read native child histories without inventing a second session registry."""
from __future__ import annotations

from contextlib import ExitStack
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


def child_records(records, harness, session, context=None):
    """Separate a verified native fork's local rows from inherited history.

    Only the native edge traversal supplies ancestry. Root reads keep their
    strict identity check, and raw rows remain available for the coverage hash.
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
    if "forked_from_id" not in meta and "subagent_history_start_ordinal" not in meta:
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
            pending, visited, seen_paths = [session], set(), set()
            ancestry = {session: ()}
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
                            paths.append((child_id, child, {"parent": parent, "ancestors": ancestors}))
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
        nodes.append({"session_id": child_id, "harness": harness, "transcript": str(child),
                      "records": logical_records, "digest": fingerprint(raw_records)})
    return nodes
