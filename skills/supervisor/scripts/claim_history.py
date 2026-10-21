# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read native child histories without inventing a second session registry."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
import re
import sqlite3
import sys



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
    github = str(Path(__file__).resolve().parents[2] / "github/scripts")
    if github not in sys.path:
        sys.path.append(github)
    from github_plan_sessions import observation_homes
    return observation_homes(additional_homes={p.parent for p in path.parents if p.name in {"sessions", "archived_sessions"}})


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


