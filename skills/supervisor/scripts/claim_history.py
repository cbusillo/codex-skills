# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read native child histories without inventing a second session registry."""
from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any
from datetime import datetime, timezone


def protobuf_fields(data: bytes) -> dict[int, Any]:
    offset = 0
    fields = {}

    def varint() -> int:
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(data):
                raise ValueError("incomplete protobuf varint")
            byte = data[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("invalid protobuf varint")

    while offset < len(data):
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError("invalid protobuf field")
        if wire == 0:
            fields[number] = varint()
            continue
        size = varint() if wire == 2 else {1: 8, 5: 4}.get(wire)
        if size is None or offset + size > len(data):
            raise ValueError("invalid protobuf length or wire type")
        fields[number] = data[offset:offset + size]
        offset += size
    return fields



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
    identities = ({r.get("sessionId") for r in records if r.get("sessionId")} if harness != "codex" else
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




AGY_HOME = Path("~/.gemini/antigravity-cli")
AGY_SETTINGS = AGY_HOME / "settings.json"

def agy_conversations(text: str) -> list[str]:
    return re.findall(r"\bCreated conversation ([0-9a-fA-F-]{36})\b", text)


def agy_log(conversation_id: Any = None, log_file: Path | None = None) -> str:
    valid_id = isinstance(conversation_id, str) and bool(re.fullmatch(r"[0-9a-fA-F-]{36}", conversation_id))
    if log_file is not None:
        try:
            text = log_file.read_text(errors="replace")
        except OSError:
            text = ""
        if text.strip():
            conversations = agy_conversations(text)
            if conversation_id is not None:
                return text if valid_id and conversations in ([], [conversation_id]) else ""
            return text if len(conversations) <= 1 else ""
    if not valid_id:
        return ""
    base = AGY_SETTINGS.expanduser().parent
    candidates = [base / "cli.log", *sorted((base / "log").glob("cli-*.log"), reverse=True)]
    for path in candidates:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        conversations = agy_conversations(text)
        if conversations == [conversation_id]:
            return text
        if conversation_id in conversations:
            return ""
    return ""

def agy_model(requested: str | None, conversation_id: Any = None, log_file: Path | None = None) -> dict[str, Any]:
    default = {"model": requested, "model_source": "requested, not reported by the CLI" if requested else "unknown"}
    log = agy_log(conversation_id, log_file)
    reroutes = list(dict.fromkeys(re.findall(r'model alias "([^"]+)" resolved to "([^"]+)"', log)))
    labels = list(dict.fromkeys(re.findall(r'Propagating selected model override to backend: label="([^"]+)"', log)))
    if len(reroutes) > 1 or len(labels) > 1:
        return default
    if reroutes and (not labels or labels[0] == reroutes[0][1]):
        return {"model": reroutes[0][1], "model_source": "reported by the CLI (rerouted)"}
    if reroutes and labels:
        return default
    if labels:
        return {"model": labels[0], "model_source": "reported by the CLI"}
    return default


def read_agy_records(path: Path) -> list[dict]:
    fields = protobuf_fields
    records = []
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as database:
            database.execute("BEGIN")
            identities = database.execute("SELECT DISTINCT cascade_id FROM trajectory_meta").fetchall()
            if identities != [(path.stem,)]:
                raise ValueError("native AGY transcript identity is unavailable or differs")
            rows = database.execute("SELECT idx, step_type, status, has_subtrajectory, step_payload FROM steps ORDER BY idx").fetchall()
        for index, (ordinal, kind, status, children, blob) in enumerate(rows):
            step = fields(blob)
            if ordinal != index or step.get(1) != kind or step.get(4) != status or children:
                raise ValueError("native AGY step or child history is unavailable")
            metadata = fields(step.get(5, b""))
            clock = fields(metadata.get(8) or metadata.get(1, b""))
            timestamp = datetime.fromtimestamp(clock[1] + clock.get(2, 0) / 1e9, timezone.utc).isoformat()
            row: dict[str, Any] = {"sessionId": path.stem, "timestamp": timestamp}
            if status != 3 and kind in {14, 15}:
                records.append({**row, "type": "user", "message": {"content": ""}})
                continue
            if kind == 14:
                row.update(type="user", message={"content": fields(step[19]).get(2, b"").decode()})
            elif kind == 15:
                response = fields(step[20])
                row.update(type="assistant", message={"content": [{"type": "text", "text": response.get(8, response.get(1, b"")).decode()}],
                           "stop_reason": "end_turn" if status == 3 and 7 not in response else None})
            else:
                call = fields(metadata.get(4, b""))
                if not call:
                    row.update(type="user", message={"content": ""})
                else:
                    name = call[2].decode()
                    if name not in {"read_file", "write_file", "edit_file", "list_dir", "grep_search", "find_by_name", "run_command"}:
                        raise ValueError(f"native AGY background work coverage is unavailable for {name}")
                    arguments = json.loads(call[3])
                    identifier = call[1].decode()
                    command = name == "run_command"
                    content = {"type": "tool_use", "id": identifier, "name": "Bash" if command else name,
                               "input": {"command": arguments["CommandLine"]} if command else arguments}
                    records.append({**row, "type": "assistant", "message": {"content": [content]}})
                    output = fields(fields(step.get(140, b"")).get(2, b"")).get(1, b"").decode()
                    row.update(type="user", message={"content": [{"type": "tool_result", "tool_use_id": identifier,
                               "content": output, "is_error": status != 3}]})
                    if command and status == 3 and (not output or "Command ID:" in output):
                        raise ValueError("native AGY background work coverage is unavailable")
            records.append(row)
    except (sqlite3.Error, KeyError, TypeError, AttributeError, UnicodeError) as error:
        raise ValueError(f"native AGY transcript unavailable: {error}") from error
    return records

