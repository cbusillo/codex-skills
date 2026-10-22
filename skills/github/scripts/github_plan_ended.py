# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import github_plan_closure as closure
import github_plan_native as native
import github_plan_sessions as sessions
import github_plan_termination as termination

if TYPE_CHECKING:
    from github_plan_release import ReleaseFacts


def session(text: str) -> str | None:
    if "Ownership release only: yes" not in text.splitlines():
        return None
    matches = re.findall(r"(?m)^Native session: (\S+)$", text)
    if len(matches) != 1:
        raise ValueError("Ownership-only attestation requires one exact native session")
    return matches[0]


def process_rows() -> list[tuple[int, str, str]]:
    return sessions.process_liveness().inventory()


def process_started_at(pid: int) -> datetime:
    return sessions.process_liveness().process_started_at(pid)


async def verify_native(entry: dict[str, Any], snapshots: dict[str, Any]) -> None:
    if entry["harness"] != "codex":
        config = sessions.native_helpers()[0].load_config(path=closure.closed_log().with_name("supervisor.toml"))
        accounts = [a for a in config["accounts"] if a["provider"] == "anthropic"]
        if not accounts:
            raise ValueError("Claude native inventory coverage is unavailable")
        for account in accounts:
            env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
            env.update({k: os.path.expanduser(v) for k, v in account["env"].items()})
            result = subprocess.run(["claude", "agents", "--json"], env=env, check=True, capture_output=True, text=True, timeout=10)
            agents = json.loads(result.stdout)
            if not isinstance(agents, list) or any(
                not isinstance(a, dict) or sessions.process_liveness().native_uuid(a.get("sessionId")) is None for a in agents
            ):
                raise ValueError("Claude native inventory is uncertain")
            if any(a["sessionId"] == entry["session_id"] for a in agents):
                raise ValueError("Source Claude session is still live; preserve claims")
        return
    homes = sessions.observation_homes(strict=True, additional_homes={Path(entry["codex_home"])})
    action = closure.lease_state(entry["session_id"])
    if action.get("action_pending"):
        raise ValueError("Source has a pending native action; preserve claims")
    for home in homes:
        async with native.connection(home) as request:
            first = await native.read_thread_facts(request, entry["session_id"])
            snapshot = {"path": first["path"], "transcripts": snapshots}
            if first["path"] not in snapshots:
                raise ValueError("Native source path is absent from verified closure transcripts")
            native.require_quiet(first, snapshot, action)
            second = await native.read_thread_facts(request, entry["session_id"])
            native.require_quiet(second, snapshot, action)
            if native.current_facts(first) != native.current_facts(second):
                raise ValueError("Native source changed during ownership release")


def claim_write(text: str) -> bool:
    return bool(re.search(r"gh-plan\.py\s+(?:--repo\s+\S+\s+)?claim\s", text)) or "gh-comment" in text and "Claimed by " in text


def original_claim_output(records: list[dict[str, Any]], url: str) -> bool:
    writes = set()
    for row in records:
        payload = row.get("payload") or {}
        if payload.get("type") in {"function_call", "custom_tool_call"}:
            text = str(payload.get("arguments", payload.get("input", "")))
            if claim_write(text):
                writes.add(payload.get("call_id"))
        if (
            payload.get("type") in {"function_call_output", "custom_tool_call_output"}
            and payload.get("call_id") in writes
            and url in str(payload.get("output", ""))
        ):
            return True
        content = (row.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "tool_use":
                text = str(block.get("input", ""))
                if claim_write(text):
                    writes.add(block.get("id"))
            if (
                block.get("type") == "tool_result"
                and block.get("tool_use_id") in writes
                and not block.get("is_error")
                and url in str(block.get("content", ""))
            ):
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


def verify(path: Path | None, facts: ReleaseFacts, *, source_url: str) -> None:
    identity = session(facts.evidence.get("body") or "")
    if identity is None:
        if path is not None:
            raise ValueError("--closed-log requires an ownership-only closed-session attestation")
        return
    if path is None:
        raise ValueError("Ownership-only release requires --closed-log for fresh closure verification")
    shared = sessions.process_liveness()
    recorded_native = shared.native_uuid(facts.record["session"])
    if (
        shared.native_uuid(identity) != identity
        or identity == facts.releaser_session
        or recorded_native is not None
        and recorded_native != identity
    ):
        raise ValueError("Ownership-only release requires another canonical native session UUID")
    if f"Original claim: {source_url}" not in (facts.evidence.get("body") or "").splitlines():
        raise ValueError("Ownership-only evidence must bind the exact original claim URL")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o077:
        raise ValueError("Unsafe closed-session log")
    rows = json.loads(path.read_text())
    selected = closure.selected_source_rows(rows, identity)
    if len(selected) != 1:
        raise ValueError("Closed-session log must identify the exact ended source")
    entry = sessions.native_helpers()[1].normalize_ledger_entry(path, selected[0])
    if (
        entry.get("process_exit_verified") is not True
        or entry.get("resumed_at")
        or entry.get("host") != socket.gethostname()
        or termination.stamp(entry["ended_at"]) != facts.cutoff
        or facts.cutoff > datetime.now(UTC)
    ):
        raise ValueError("Closed-session log lacks verified source-host closure or the generation resumed")
    historical = {**entry, "_inventory": entry.get("pid") is None and entry.get("tty") is None}
    shared.verify_process_exit(historical, rows=process_rows(), ended_at=facts.cutoff, started_at=process_started_at)
    roots = termination._transcript_roots(entry)
    if entry["harness"] == "codex":
        for home in sessions.observation_homes(strict=True, additional_homes={Path(entry["codex_home"])}):
            roots.update((home / name).resolve() for name in ("sessions", "archived_sessions") if (home / name).is_dir())
    paths = {Path(entry["transcript"]).resolve()}
    for root in roots:
        paths.update(p.resolve() for p in root.rglob(f"*{identity}*.jsonl*") if p.name.endswith((".jsonl", ".jsonl.zst")))
    snapshots, originals = {}, {}
    bound = False
    for transcript in paths:
        records, mtime, size, truncated = transcript_snapshot(entry, transcript)
        times = [termination.stamp(row["timestamp"]) for row in records if row.get("timestamp")]
        termination._transcript_activity(times, mtime, truncated, datetime.now(UTC), facts.cutoff)
        originals[transcript] = records, mtime, size, truncated
        bound |= original_claim_output(records, source_url)
        logical_path = str(transcript).removesuffix(".zst")
        if logical_path in snapshots:
            raise ValueError("Both archived and writable copies of the same transcript exist; verify source identity")
        snapshots[logical_path] = {"status": sessions.native_helpers()[1].summarize(records, entry["harness"])}
    if not bound:
        raise ValueError("Native transcript lacks the original claim's tool receipt")
    asyncio.run(verify_native(entry, snapshots))
    shared.verify_process_exit(historical, rows=process_rows(), ended_at=facts.cutoff, started_at=process_started_at)
    if any(transcript_snapshot(entry, p) != value for p, value in originals.items()) or closure.log_identity(
        path.lstat()
    ) != closure.log_identity(before):
        raise ValueError("Source transcript or closed-session log changed during release")
