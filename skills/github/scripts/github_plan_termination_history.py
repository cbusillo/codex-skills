# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import github_plan_closure as closure
import github_plan_sessions as sessions
import github_plan_termination as termination


def claude_accounts() -> list[dict[str, Any]]:
    config = sessions.native_helpers()[0].load_config(path=closure.closed_log().with_name("supervisor.toml"))
    accounts = [a for a in config["accounts"] if a["provider"] == "anthropic"]
    if not accounts:
        raise ValueError("Claude native inventory coverage is unavailable")
    return accounts


def native_owners(entry: dict[str, Any]) -> dict[int, dict[str, Any]]:
    owners = {}
    for account in claude_accounts():
        env = {key: value for key, value in os.environ.items() if key != "CLAUDE_CONFIG_DIR"}
        env.update({key: os.path.expanduser(value) for key, value in account["env"].items()})
        result = subprocess.run(
            ["claude", "agents", "--json", "--all"], env=env, check=True, capture_output=True, text=True, timeout=10
        )
        agents = json.loads(result.stdout)
        if not isinstance(agents, list):
            raise TypeError("Claude native inventory is uncertain")
        for agent in agents:
            if not isinstance(agent, dict) or sessions.process_liveness().native_uuid(agent.get("sessionId")) is None:
                raise ValueError("Claude native inventory is uncertain")
            if agent["sessionId"] == entry["session_id"]:
                raise ValueError("Source Claude session is still live; preserve claims")
            pid = agent.get("pid")
            if type(pid) is not int or pid <= 0 or type(agent.get("startedAt")) is not int:
                raise TypeError("Claude native process identity is uncertain")
            if pid in owners and owners[pid] != agent:
                raise ValueError("Claude process has conflicting native identities")
            owners[pid] = agent
    return owners


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
        content = (row.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "tool_use" and block.get("name") == "Bash":
                command = (block.get("input") or {}).get("command", "")
                if isinstance(command, str) and claim_write(command):
                    calls[block.get("id")] = row.get("timestamp")
            identifier = block.get("tool_use_id")
            if (
                block.get("type") == "tool_result"
                and not block.get("is_error")
                and identifier in calls
                and original_interval(calls[identifier], row.get("timestamp"), created_at)
                and posted_claim(block.get("content"), url, record)
            ):
                return True
    return False


def configured_roots(record: dict[str, Any]) -> set[Path]:
    roots = termination._transcript_roots(record)
    config = sessions.native_helpers()[0].load_config(path=closure.closed_log().with_name("supervisor.toml"))
    provider = "anthropic"
    accounts = [account for account in config["accounts"] if account["provider"] == provider]
    if not accounts:
        raise ValueError("Configured source account coverage is unavailable")
    variable = "CLAUDE_CONFIG_DIR"
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
        paths.update(path.resolve() for path in root.rglob(f"*{session}*.jsonl"))
        paths.update(path.resolve() for path in root.glob(f"**/{session}/subagents/*.jsonl"))
        project = root / Path(record["transcript"]).parent.name
        if not project.is_dir():
            continue
        for path in project.rglob("*.jsonl"):
            if any(row.get("sessionId") == session for row in candidate_rows(path)):
                paths.add(path.resolve())
    return paths


def own_records(record: dict[str, Any], path: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import claim_history

    if any(row.get("isSidechain") or row.get("parentToolUseID") for row in rows):
        raise ValueError("Spawned histories cannot establish original claim ownership")
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
    if record.get("harness") != "claude":
        raise ValueError("Historical source lacks a supported original exit record; preserve claims")
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
    snapshots = {path: termination.transcript_snapshot(record, path) for path in paths}
    original = Path(record["transcript"]).resolve(strict=True)
    rows = own_records(record, original, snapshots[original][0])
    ended_at = exit_timestamp(record, rows)
    ended = termination.stamp(ended_at)
    if record.get("ended_at") is not None and termination.stamp(record["ended_at"]) != ended:
        raise ValueError("Recorded end time differs from the original native exit")
    now = termination.datetime.now(termination.timezone.utc)
    if ended > now:
        raise ValueError("Original exit is in the future")
    verify_processes(record, ended)
    last = ended
    for snapshot in snapshots.values():
        times = [termination.stamp(row["timestamp"]) for row in snapshot[0] if row.get("timestamp")]
        last = max(last, termination._transcript_activity(times, snapshot[1], snapshot[3], now, ended))
    if record.get("claim_session", identity) != identity:
        source = record.get("claim")
        url = record.get("claim_url")
        if not isinstance(source, dict) or source.get("session") != record["claim_session"] or not isinstance(url, str):
            raise ValueError("Alias recovery requires the exact original claim and URL")
        if not original_claim_output(rows, url, source, created_at=record.get("claim_created_at")):
            raise ValueError("Native root lacks the original claim's direct creation receipt")
    refuse_duplicate_receipt(record, roots)
    verify_processes(record, ended)
    if (
        any(termination.transcript_snapshot(record, path) != snapshot for path, snapshot in snapshots.items())
        or transcript_paths(record, roots) != paths
        or configured_roots(record) != roots
        or closure.source_records({identity}).get(identity) != canonical
        or closure.action_pending(identity)
    ):
        raise ValueError("Source evidence changed during termination checks")
    refuse_duplicate_receipt(record, roots)
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


def verify_processes(record: dict[str, Any], ended: datetime) -> None:
    shared = sessions.process_liveness()
    owners = native_owners(record)

    def independent(pid: int, _argv: list[str]) -> bool:
        owner = owners.get(pid)
        if owner is None:
            return False
        try:
            return int(shared.process_started_at(pid).timestamp()) == owner["startedAt"] // 1000
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise ValueError("Native process birth is unavailable; preserve claims") from error

    shared.verify_process_exit(
        {**record, "_inventory": True, "_historical_exit": True},
        rows=termination.inventory(),
        ended_at=ended,
        independent_harness=independent,
    )


def refuse_duplicate_receipt(record: dict[str, Any], roots: set[Path]) -> None:
    if record.get("claim_session", record["session_id"]) == record["session_id"]:
        return
    for root in roots:
        for path in root.rglob("*.jsonl"):
            content = path.read_text(encoding="utf-8")
            if record["claim_url"] not in content:
                continue
            rows = candidate_rows(path)
            identities = {row.get("sessionId") for row in rows if row.get("sessionId")}
            if identities == {record["session_id"]} and not any(row.get("isSidechain") for row in rows):
                continue
            if original_claim_output(rows, record["claim_url"], record["claim"], created_at=record["claim_created_at"]):
                raise ValueError("Original claim receipt also appears in another native history; preserve claims")


def candidate_rows(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    rows = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            if index == len(lines) - 1 and not line.endswith(("\n", "\r")):
                break
            raise
        if not isinstance(row, dict):
            raise TypeError("Native transcript candidate is not an object")
        rows.append(row)
    return rows
