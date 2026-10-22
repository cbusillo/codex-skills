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
from collections.abc import Iterator
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
            result = subprocess.run(["claude", "agents", "--json"], env=env, check=True, capture_output=True, text=True, timeout=10)
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


def result_objects(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from result_objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from result_objects(nested)
    elif isinstance(value, str):
        for line in value.splitlines():
            try:
                nested = json.loads(line)
            except ValueError:
                continue
            if not isinstance(nested, str):
                yield from result_objects(nested)


def posted_claim(output: Any, url: str, record: dict[str, str]) -> bool:
    identifier = int(url.rsplit("issuecomment-", 1)[1])
    for result in result_objects(output):
        if result.get("ok") is not True:
            continue
        if "legacy_reason" in record:
            comment = result.get("comment") or {}
            created = result.get("comment_action") == "created"
        else:
            comment = (result.get("claim_comment") or {}).get("comment") or {}
            created = result.get("claim") == record
        if created and type(comment.get("id")) is int and comment["id"] == identifier and comment.get("url") == url:
            return True
    return False


def projected_claim(
    output: Any, call: tuple[str, str | None], ended: str | None, *, url: str, record: dict[str, str], created_at: str | None
) -> bool:
    if "legacy_reason" in record or not created_at or not call[1] or not ended:
        return False
    if sessions.process_liveness().native_uuid(record["session"]) is not None:
        return False
    number = url.split("#", 1)[0].rsplit("/", 1)[1]
    numbered = re.search(rf"(?m)^{number} True None$", str(output)) is not None
    single_target = len(re.findall(r"\bclaim\s+", call[0])) == 1 and re.search(rf"\bclaim\s+{number}(?=\s|$)", call[0]) is not None
    loop = re.search(r"\bfor\s+([A-Za-z_]\w*)\s+in\s+([0-9 ]+)\s*;\s*do\b", call[0])
    loop_target = (
        loop is not None
        and number in loop[2].split()
        and re.search(rf"\bclaim\s+\${re.escape(loop[1])}(?=\s|$)", call[0]) is not None
    )
    if not single_target and not loop_target:
        return False
    if not numbered and not (single_target and re.search(r"(?m)^True None$", str(output))):
        return False
    if any(
        re.search(rf"--{key}\s+{re.escape(record[key])}(?=\s|['\"]|$)", call[0]) is None for key in ("session", "worker", "branch")
    ):
        return False
    return termination.stamp(call[1]).replace(microsecond=0) <= termination.stamp(created_at) <= termination.stamp(ended)


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
            if posted_claim(output, url, record) or projected_claim(
                output, calls[identifier], row.get("timestamp"), url=url, record=record, created_at=created_at
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
    canonical = closure.source_records({identity}).get(identity)
    if canonical != entry:
        raise ValueError("Closure evidence copy does not match the canonical closed-session row")
    if "legacy_reason" in facts.record and entry.get("branch") != facts.record["branch"]:
        raise ValueError("Legacy source branch must match the canonical original closure record")
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
    else:
        for account in claude_accounts():
            home = Path(account["env"].get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()
            if (home / "projects").is_dir():
                roots.add((home / "projects").resolve())
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
        bound |= original_claim_output(records, source_url, facts.record, created_at=facts.source["created_at"])
        logical_path = str(transcript).removesuffix(".zst")
        if logical_path in snapshots:
            raise ValueError("Both archived and writable copies of the same transcript exist; verify source identity")
        snapshots[logical_path] = {}
    if not bound:
        raise ValueError("Native transcript lacks the original claim's tool receipt")
    asyncio.run(verify_native(entry, snapshots))
    shared.verify_process_exit(historical, rows=process_rows(), ended_at=facts.cutoff, started_at=process_started_at)
    if (
        any(transcript_snapshot(entry, p) != value for p, value in originals.items())
        or closure.log_identity(path.lstat()) != closure.log_identity(before)
        or closure.source_records({identity}).get(identity) != canonical
    ):
        raise ValueError("Source transcript or closed-session log changed during release")
