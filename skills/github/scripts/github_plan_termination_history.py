# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any, NamedTuple

import github_plan_closure as closure
import github_plan_native as native
import github_plan_sessions as sessions
import github_plan_termination as termination


def claude_accounts() -> list[dict[str, Any]]:
    config = sessions.native_helpers()[0].load_config(path=closure.closed_log().with_name("supervisor.toml"))
    accounts = [a for a in config["accounts"] if a["provider"] == "anthropic"]
    if not accounts:
        raise ValueError("Claude native inventory coverage is unavailable")
    return accounts


async def verify_native(entry: dict[str, Any], snapshots: dict[str, Any]) -> None:
    if entry["harness"] != "codex":
        for account in claude_accounts():
            env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
            env.update({k: os.path.expanduser(v) for k, v in account["env"].items()})
            result = await asyncio.to_thread(
                subprocess.run, ["claude", "agents", "--json"], env=env, check=True, capture_output=True, text=True, timeout=10
            )
            agents = json.loads(result.stdout)
            if not isinstance(agents, list) or any(
                not isinstance(a, dict) or sessions.process_liveness().native_uuid(a.get("sessionId")) is None for a in agents
            ):
                raise ValueError("Claude native inventory is uncertain")
            if any(sessions.process_liveness().native_uuid(a["sessionId"]) == entry["session_id"] for a in agents):
                raise ValueError("Source Claude session is still live; preserve claims")
        return
    homes = sessions.observation_homes(strict=True, additional_homes={Path(entry["codex_home"])})
    action = closure.lease_state(entry["session_id"])
    if action.get("action_pending"):
        raise ValueError("Source has a pending native action; preserve claims")
    authenticated = native._authenticated_paths(snapshots, homes)
    if str(Path(entry["codex_home"]).resolve()) not in authenticated:
        raise ValueError("Recorded source account lacks authenticated source transcripts")
    for home in authenticated:
        async with native.connection(Path(home)) as request:
            first = await native.read_thread_facts(request, entry["session_id"])
            if first["path"] not in snapshots:
                raise ValueError("Native source path is absent from verified closure transcripts")
            second = await native.read_thread_facts(request, entry["session_id"])
            if any(facts["state"] != "notLoaded" or facts["queue"] for facts in (first, second)) or native.current_facts(
                first
            ) != native.current_facts(second):
                raise ValueError("Native source changed during ownership release")


def claim_write(text: str) -> bool:
    return bool("gh-plan.py" in text and re.search(r"\bclaim\s", text)) or "gh-comment" in text


def posted_claim(output: Any, url: str, record: dict[str, str]) -> bool:
    if isinstance(output, list) and len(output) == 1 and output[0].get("type") == "text":
        output = output[0].get("text")
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            return False
    if not isinstance(output, dict) or output.get("ok") is not True:
        return False
    comment = output.get("comment") if "legacy_reason" in record else (output.get("claim_comment") or {}).get("comment")
    matches = output.get("comment_action") == "created" if "legacy_reason" in record else output.get("claim") == record
    return bool(
        matches
        and isinstance(comment, dict)
        and type(comment.get("id")) is int
        and comment["id"] == int(url.rsplit("issuecomment-", 1)[1])
        and comment.get("url") == url
    )


def original_interval(start: str | None, end: str | None, created_at: str | None) -> bool:
    return bool(
        start
        and end
        and created_at
        and termination.stamp(start).replace(microsecond=0) <= termination.stamp(created_at) <= termination.stamp(end)
    )


def original_claim_output(records: list[dict[str, Any]], url: str, record: dict[str, str], *, created_at: str | None = None) -> bool:
    calls = {}
    for row in records:
        payload = row.get("payload") or {}
        execution = payload.get("item") or {}
        if (
            execution.get("type") == "CommandExecution"
            and execution.get("status") == "completed"
            and type(execution.get("exit_code")) is int
            and execution["exit_code"] == 0
            and claim_write(str(execution.get("command", "")))
            and isinstance(execution.get("duration"), dict)
            and original_interval(
                (
                    termination.stamp(row["timestamp"])
                    - timedelta(
                        seconds=execution["duration"].get("secs", 0), microseconds=execution["duration"].get("nanos", 0) / 1000
                    )
                ).isoformat(),
                row.get("timestamp"),
                created_at,
            )
            and posted_claim(execution.get("stdout"), url, record)
        ):
            return True
        content = (row.get("message") or {}).get("content")
        blocks = content if isinstance(content, list) else []
        inputs, outputs = [], []
        if payload.get("type") in {"function_call", "custom_tool_call"}:
            inputs.append((payload.get("call_id"), str(payload.get("arguments", payload.get("input", "")))))
        if payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
            outputs.append((payload.get("call_id"), payload.get("output")))
        for block in blocks:
            if block.get("type") == "tool_use":
                arguments = block.get("input") or {}
                inputs.append((block.get("id"), str(arguments.get("command", "")) if isinstance(arguments, dict) else ""))
            if block.get("type") == "tool_result" and not block.get("is_error"):
                outputs.append((block.get("tool_use_id"), block.get("content")))
        for identifier, text in inputs:
            if not isinstance(identifier, str) or not identifier:
                continue
            if claim_write(text):
                calls[identifier] = text, row.get("timestamp")
        for identifier, output in outputs:
            if identifier not in calls:
                continue
            if original_interval(calls[identifier][1], row.get("timestamp"), created_at) and posted_claim(output, url, record):
                return True
    return False


class TranscriptSnapshot(NamedTuple):
    records: list[dict[str, Any]]
    mtime: int
    size: int
    truncated: bool


def transcript_snapshot(entry: dict[str, Any], path: Path) -> TranscriptSnapshot:
    if path.suffix != ".zst":
        return TranscriptSnapshot(*termination.transcript_snapshot(entry, path))
    before = path.stat()
    result = subprocess.run(["zstd", "-dc", str(path)], check=True, capture_output=True, text=True, timeout=10)
    records, truncated = termination.transcript_records(entry, result.stdout.splitlines(keepends=True))
    after = path.stat()
    if closure.transcript_identity(before) != closure.transcript_identity(after):
        raise ValueError("Archived source transcript changed during closure verification")
    return TranscriptSnapshot(records, after.st_mtime_ns, after.st_size, truncated)


def configured_roots(record: dict[str, Any]) -> set[Path]:
    roots = termination._transcript_roots(record)
    config = sessions.native_helpers()[0].load_config(path=closure.closed_log().with_name("supervisor.toml"))
    provider = "anthropic" if record["harness"] == "claude" else "openai"
    accounts = [account for account in config["accounts"] if account["provider"] == provider]
    if not accounts:
        raise ValueError("Configured source account coverage is unavailable")
    variable = "CLAUDE_CONFIG_DIR" if provider == "anthropic" else "CODEX_HOME"
    for account in accounts:
        home = Path(account["env"].get(variable, str(Path.home() / ".claude"))).expanduser()
        if not home.is_dir():
            raise ValueError("Configured source account home is unavailable")
        for name in ("projects",) if provider == "anthropic" else ("sessions", "archived_sessions"):
            path = home / name
            if path.exists():
                with os.scandir(path):
                    roots.add(path.resolve(strict=True))
    return roots


def transcript_paths(record: dict[str, Any], roots: set[Path]) -> set[Path]:
    paths = {Path(record["transcript"]).resolve(strict=True)}
    session = record["session_id"]
    for root in roots:
        paths.update(path.resolve() for path in root.rglob(f"*{session}*.jsonl*"))
        if record["harness"] != "claude":
            continue
        project = root / Path(record["transcript"]).parent.name
        if not project.is_dir():
            continue
        for path in project.rglob("*.jsonl"):
            with path.open(encoding="utf-8") as stream:
                if any(json.loads(line).get("sessionId") == session for line in stream if line.strip()):
                    paths.add(path.resolve())
    return paths


def own_records(record: dict[str, Any], path: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import claim_history

    if any(row.get("isSidechain") or row.get("parentToolUseID") for row in rows):
        raise ValueError("Spawned histories cannot establish original claim ownership")
    if record["harness"] == "codex":
        metadata = rows[0].get("payload") or {}
        if (
            metadata.get("forked_from_id")
            or metadata.get("parent_thread_id")
            or claim_history.source_parent(metadata.get("source", {}))
        ):
            raise ValueError("Forked or spawned histories cannot establish original claim ownership")
    return claim_history.root_records(path, record["harness"], record["session_id"], rows)


def exit_timestamp(record: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    reader = termination.native_helpers()[0]
    if (
        record["harness"] != "claude"
        or record["exit_record"] != {"kind": "claude_local_exit"}
        or reader.strip_claude_exit_tail(rows) == rows
    ):
        raise ValueError("Original completed native exit record is unavailable; preserve claims")
    return rows[-1]["timestamp"]


def verify(record: dict[str, Any], session: str) -> dict[str, Any]:
    import hashlib

    termination._validate_metadata(record, session)
    identity = record["session_id"]
    if (
        sessions.process_liveness().native_uuid(identity) != identity
        or record["host"] != termination.socket.gethostname()
        or record.get("tmux_session_id")
        or record.get("tmux_socket")
    ):
        raise ValueError("Historical exit requires its canonical source UUID and source host without tmux fields")
    canonical = closure.source_records({identity}).get(identity)
    if closure.action_pending(identity) or canonical and (canonical.get("resumed_at") or canonical.get("resumed_generation")):
        raise ValueError("Source has resumed or has a pending action; preserve claims")
    roots = configured_roots(record)
    paths = transcript_paths(record, roots)
    snapshots = {path: transcript_snapshot(record, path) for path in paths}
    original = Path(record["transcript"]).resolve(strict=True)
    rows = own_records(record, original, snapshots[original].records)
    ended_at = exit_timestamp(record, rows)
    ended = termination.stamp(ended_at)
    if record.get("ended_at") is not None and termination.stamp(record["ended_at"]) != ended:
        raise ValueError("Recorded end time differs from the original native exit")
    now = termination.datetime.now(termination.timezone.utc)
    if ended > now:
        raise ValueError("Original exit is in the future")
    physical = {**record, "_inventory": True}
    shared = sessions.process_liveness()
    shared.verify_process_exit(physical, rows=termination.inventory(), ended_at=ended)
    last = ended
    for path, snapshot in snapshots.items():
        times = [termination.stamp(row["timestamp"]) for row in snapshot.records if row.get("timestamp")]
        last = max(last, termination._transcript_activity(times, snapshot.mtime, snapshot.truncated, now, ended))
        if exit_timestamp(record, own_records(record, path, snapshot.records)) != ended_at:
            raise ValueError("Source copies disagree about the original exit")
    if record.get("claim_session", identity) != identity:
        source = record.get("claim")
        url = record.get("claim_url")
        if not isinstance(source, dict) or source.get("session") != record["claim_session"] or not isinstance(url, str):
            raise ValueError("Alias recovery requires the exact original claim and URL")
        if not original_claim_output(rows, url, source, created_at=record.get("claim_created_at")):
            raise ValueError("Native root lacks the original claim's direct creation receipt")
    asyncio.run(verify_native(record, {str(path).removesuffix(".zst"): {} for path in snapshots}))
    shared.verify_process_exit(physical, rows=termination.inventory(), ended_at=ended)
    if (
        any(transcript_snapshot(record, path) != snapshot for path, snapshot in snapshots.items())
        or transcript_paths(record, roots) != paths
        or configured_roots(record) != roots
        or closure.source_records({identity}).get(identity) != canonical
        or closure.action_pending(identity)
    ):
        raise ValueError("Source evidence changed during termination checks")
    data = {key: record[key] for key in ("session_id", "host", "cause", "retained_artifacts")}
    for key in ("claim_session", "claim", "claim_url", "claim_created_at"):
        if key in record:
            data[key] = record[key]
    data.update(
        pid=record.get("pid"),
        ended_at=ended_at,
        checked_at=now.isoformat(),
        last_activity_at=last.isoformat(),
        terminal_state="exit recorded",
        exit_record=record["exit_record"],
        exit_digest=hashlib.sha256(json.dumps(rows[-3:], sort_keys=True).encode()).hexdigest(),
    )
    termination.validate(data, session)
    termination._verify_artifacts(data["retained_artifacts"])
    return data
