# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import claim_history
import github_plan_claim_log as claim_log


def values(value: object) -> Iterator[dict]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from values(nested)
    elif isinstance(value, str):
        for line in value.splitlines():
            if not line.lstrip().startswith(("{", "[")):
                continue
            try:
                nested = json.loads(line)
            except ValueError:
                continue
            yield from values(nested)


def read_records(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                records.append(row)
    return records


def metadata(records: list[dict]) -> dict:
    return next(
        (row["payload"] for row in records if row.get("type") == "session_meta" and isinstance(row.get("payload"), dict)), {}
    )


def codex_children(records: list[dict]) -> set[str]:
    return {
        item["agent_thread_id"]
        for row in records
        if row.get("type") == "event_msg"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("type") == "item_completed"
        and isinstance(item := row["payload"].get("item"), dict)
        and item.get("type") == "SubAgentActivity"
        and isinstance(item.get("agent_thread_id"), str)
    }


def indexed_children(databases: list, parent: str) -> dict[str, set[Path]]:
    children: dict[str, set[Path]] = {}
    try:
        for database in databases:
            for child, path in database.execute(
                "SELECT e.child_thread_id,t.rollout_path FROM thread_spawn_edges e "
                "LEFT JOIN threads t ON t.id=e.child_thread_id WHERE e.parent_thread_id=?",
                (parent,),
            ):
                children.setdefault(child, set())
                if path:
                    children[child].add(Path(path).expanduser().resolve())
    except sqlite3.Error as error:
        raise ValueError("native child index is unreadable; restore the claiming host's native index") from error
    return children


def indexed_paths(databases: list, session: str) -> set[Path]:
    try:
        return {
            Path(path).expanduser().resolve()
            for database in databases
            for (path,) in database.execute("SELECT rollout_path FROM threads WHERE id=?", (session,))
            if path
        }
    except sqlite3.Error as error:
        raise ValueError("native rollout index is unreadable; restore it or pass the child's native transcript") from error


def entry(session: str, path: Path, records: list[dict], identity: str) -> dict:
    return {"session_id": session, "transcript": str(path), "native_child": identity, "_transcript_records": records}


def codex_entries(root: dict, supplied: dict[str, set[Path]]) -> list[dict]:
    root_id, root_path = root["session_id"], Path(root["transcript"]).resolve()
    root_identity = metadata(read_records(root_path)).get("session_id", root_id)
    expanded = []
    pending = [(root_id, None, ())]
    visited = set()
    with claim_history.native_databases(root_path) as databases:
        while pending:
            native, parent, ancestors = pending.pop()
            if native in visited:
                continue
            if len(visited) >= 4096:
                raise ValueError("native session lineage exceeds its coverage bound")
            visited.add(native)
            paths = supplied.get(native) or indexed_paths(databases, native)
            if not paths:
                raise ValueError(f"missing native transcript for child {native}; restore it or pass --session and --transcript")
            children = indexed_children(databases, native)
            for path in sorted(paths):
                records = read_records(path)
                meta = metadata(records)
                if meta.get("id") != native:
                    raise ValueError(f"native transcript identity differs for {native}")
                if parent is None:
                    records = claim_history.root_records(path, "codex", native, records)
                else:
                    if (
                        claim_history.source_parent(meta.get("source", {})) != parent
                        or meta.get("session_id", root_identity) != root_identity
                    ):
                        raise ValueError(f"native child {native} does not belong to {root_id}")
                    records = claim_history.child_records(records, "codex", native, {"parent": parent, "ancestors": ancestors})
                for claim_session in {root_id, root_identity, native}:
                    expanded.append(entry(claim_session, path, records, native))
                for child in codex_children(records):
                    children.setdefault(child, set()).update(indexed_paths(databases, child))
            for child, paths in children.items():
                if child not in supplied and paths:
                    supplied[child] = paths
                pending.append((child, native, (*ancestors, native)))
    return expanded


def claude_agent(records: list[dict]) -> str | None:
    return next((row["agentId"] for row in records if isinstance(row.get("agentId"), str)), None)


def claude_children(records: list[dict]) -> set[str]:
    return {
        result["agentId"]
        for row in records
        if isinstance(result := row.get("toolUseResult"), dict) and isinstance(result.get("agentId"), str)
    }


def persisted_output(records: list[dict], directory: Path) -> list[dict]:
    paths = set()
    for row in records:
        result = row.get("toolUseResult")
        if isinstance(result, dict) and isinstance(result.get("persistedOutputPath"), str):
            paths.add(Path(result["persistedOutputPath"]).expanduser().resolve())
        content = (row.get("message") or {}).get("content", [])
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                text = block.get("content")
                if isinstance(text, str) and text.startswith("<persisted-output>\n"):
                    match = re.search(r"(?m)^Output too large.*\nFull output saved to: ([^\n]+)$", text)
                    if match:
                        paths.add(Path(match[1]).expanduser().resolve())
    output = []
    for path in sorted(paths):
        if not path.is_relative_to(directory.resolve()):
            raise ValueError("persisted tool output is outside this session's native tool-results directory")
        output.append({"native_tool_output": path.read_text(encoding="utf-8")})
    return output


def claude_entries(root: dict, supplied: dict[str, set[Path]]) -> list[dict]:
    native, root_path = root["session_id"], Path(root["transcript"]).resolve()
    directory = root_path.with_suffix("") / "subagents"
    pending = [(candidate.resolve(), candidate.stem.removeprefix("agent-")) for candidate in directory.glob("agent-*.jsonl")]
    pending.append((root_path, None))
    visited = set()
    expanded = []
    while pending:
        path, agent = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        if len(visited) >= 4096:
            raise ValueError("native session lineage exceeds its coverage bound")
        records = read_records(path)
        own = [row for row in records if row.get("sessionId") == native]
        if not own:
            if agent is not None and any(row.get("sessionId") for row in records):
                continue
            raise ValueError(f"native transcript does not belong to {native}")
        if agent is not None and claude_agent(own) != agent:
            raise ValueError(f"native subagent identity differs for {agent}")
        outputs = persisted_output(own, root_path.with_suffix("") / "tool-results")
        expanded.append(entry(native, path, [*own, *outputs], native))
        for child in claude_children(own):
            paths = supplied.get(child) or {directory / f"agent-{child}.jsonl"}
            if not any(path.exists() for path in paths):
                foreign = list(root_path.parent.glob(f"*/subagents/agent-{child}.jsonl"))
                if foreign and all(not any(row.get("sessionId") == native for row in read_records(path)) for path in foreign):
                    continue
                raise ValueError(f"missing native subagent {child}; restore it or pass its --transcript under session {native}")
            pending.extend((path.resolve(), child) for path in paths)
    return expanded


def expand(entries: list[dict]) -> list[dict]:
    supplied: dict[str, set[Path]] = {}
    for item in entries:
        path = Path(item["transcript"]).resolve()
        records = read_records(path)
        native = metadata(records).get("id") or claude_agent(records) or item["session_id"]
        supplied.setdefault(native, set()).add(path)
    expanded = []
    for item in entries:
        records = read_records(Path(item["transcript"]))
        if metadata(records):
            expanded.extend(codex_entries(item, supplied))
        else:
            expanded.extend(claude_entries(item, supplied))
    return expanded


def claim_target(value: dict, session: str) -> tuple[str, int] | None:
    source = value.get("claim")
    if not isinstance(source, dict) or source.get("session") != session:
        return None
    receipt = value.get("claim_comment") or {}
    record = value.get("issue") or receipt
    if not isinstance(record, dict) or not isinstance(receipt, dict):
        raise ValueError("recorded claim result has an invalid target")
    if record.get("repo") and record.get("number"):
        return claim_log.target({"session": session, "repository": record["repo"], "record": record["number"]}, session)
    if receipt or "post_claim" in value.get("completed_steps", []):
        raise ValueError("recorded claim write lacks its repository and record number")
    return None


def tool_inputs(records: list[dict]) -> Iterator[tuple[str, str]]:
    for row in records:
        payload = row.get("payload")
        if isinstance(payload, dict) and payload.get("type") in {"function_call", "custom_tool_call"}:
            name = str(payload.get("name", "")).split(".")[-1].casefold()
            if name in {"exec", "exec_command", "bash", "write_stdin"}:
                raw = payload.get("arguments", payload.get("input", ""))
                yield str(payload.get("call_id", "")), raw if isinstance(raw, str) else json.dumps(raw)
        content = (row.get("message") or {}).get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash":
                    command = (block.get("input") or {}).get("command")
                    if isinstance(command, str):
                        yield str(block.get("id", "")), command


def tool_outputs(records: list[dict]) -> dict[str, list[object]]:
    outputs: dict[str, list[object]] = {}
    for row in records:
        payload = row.get("payload")
        if isinstance(payload, dict) and payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
            outputs.setdefault(str(payload.get("call_id", "")), []).append(payload.get("output"))
        content = (row.get("message") or {}).get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    outputs.setdefault(str(block.get("tool_use_id", "")), []).append(block.get("content"))
    inputs = dict(tool_inputs(records))
    for blocks in list(outputs.values()):
        yielded = {item["session_id"] for block in blocks for item in values(block) if type(item.get("session_id")) is int}
        for followup, text in inputs.items():
            if any(re.search(rf"session_id[\"']?\s*:\s*{process}(?!\d)", text) for process in yielded):
                blocks.extend(outputs.get(followup, []))
    return outputs


def literal_targets(records: list[dict], session: str, receipts: set[tuple[str, int]]) -> set[tuple[str, int]]:
    targets = set()
    outputs = tool_outputs(records)
    invocation = re.compile(
        r"gh-plan\.py[\"']?\s+(?:(?:--repo|-R)\s+[\"']?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)[\"']?\s+)?claim\s+[\"']?([^\s\"';,)]+)"
    )
    reference = re.compile(r"(?:https://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:#|/(?:issues|pull)/)([1-9]\d*)\Z")
    for identifier, text in tool_inputs(records):
        for match in invocation.finditer(text):
            tail = text[match.end() :]
            declared = re.search(r"--session\s+[\"']?([^\s\"';,)]+)", tail)
            if declared and not declared[1].startswith("$") and declared[1] != session:
                continue
            repo, target = match.groups()
            if target.startswith("-"):
                continue
            qualified = reference.fullmatch(target)
            if qualified:
                targets.add((qualified[1], int(qualified[2])))
                continue
            if repo and target.isdecimal() and int(target) > 0:
                targets.add((repo, int(target)))
                continue
            results = [item for block in outputs.get(identifier, []) for item in values(block)]
            matching = [target for item in results if (target := claim_target(item, session)) in receipts]
            if matching:
                continue
            rejected = any(
                item.get("operation") == "github.plan.claim"
                and item.get("ok") is False
                and not item.get("claim_comment")
                and "post_claim" not in item.get("completed_steps", [])
                and item.get("outcome_certainty") != "unknown"
                for item in results
            )
            if rejected:
                continue
            raise ValueError(
                f"claim invocation {identifier} lacks a complete result or literal repository target; restore its native output"
            )
    return targets
