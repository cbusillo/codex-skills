# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import github_plan_closure as closure
import github_plan_sessions as sessions
import github_plan_termination as termination


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
    accounts = [account for account in config["accounts"] if account["provider"] == "anthropic"]
    if not accounts:
        raise ValueError("Configured source account coverage is unavailable")
    for account in accounts:
        home = Path(account["env"].get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()
        if not home.is_dir():
            raise ValueError("Configured source account home is unavailable")
        for name in ("projects",):
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
            if session.encode() in path.read_bytes() and any(row.get("sessionId") == session for row in candidate_rows(path)):
                paths.add(path.resolve())
    return paths


def own_records(record: dict[str, Any], path: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import claim_history

    if any(row.get("isSidechain") or row.get("parentToolUseID") for row in rows):
        raise ValueError("Spawned histories cannot establish original claim ownership")
    return claim_history.root_records(path, record["harness"], record["session_id"], rows)


@dataclass(frozen=True)
class OriginalExit:
    started_at: datetime
    ended_at: datetime
    digest: str


def command_helpers() -> Any:
    directory = str(Path(__file__).resolve().parents[2] / "work-closeout/scripts")
    if directory not in sys.path:
        sys.path.append(directory)
    import claim_transcripts

    return claim_transcripts


def native_command(command: Any, identity: str) -> None:
    helper = command_helpers()
    if not isinstance(command, list) or not all(isinstance(word, str) for word in command):
        raise ValueError("Original exit requires literal native command argv")
    if len(command) == 3 and command[0] in {"/bin/zsh", "/bin/bash", "/bin/sh"} and command[1] in {"-c", "-lc"}:
        tree = helper.Parser(helper.Language(helper.tree_sitter_bash.language())).parse(command[2].encode())
        nodes = tree.root_node.named_children
        if tree.root_node.has_error or len(nodes) != 1 or nodes[0].type != "command" or helper.shell_generated(nodes[0]):
            raise ValueError("Original exit command must be one foreground literal native invocation")
        node = nodes[0]
        if any(child.type not in {"command_name", "word", "string", "raw_string"} for child in node.named_children):
            raise ValueError("Original exit command has unsupported shell syntax")
        if any(child.type in {"&", ";"} for child in tree.root_node.children):
            raise ValueError("Original exit command must run in the foreground")
        command = helper.command_argv(node)
    if len(command) < 3 or command[0] != "claude" or command[1] not in {"--session-id", "--resume"} or command[2] != identity:
        raise ValueError("Original exit command does not bind the exact source native UUID")
    index = 3
    while index < len(command):
        if command[index] == "--strict-mcp-config":
            index += 1
        elif command[index] in {"--settings", "--mcp-config"} and index + 1 < len(command):
            index += 2
        else:
            raise ValueError("Original exit command has unsupported native arguments")


def original_exit(record: dict[str, Any]) -> OriginalExit:
    import hashlib

    evidence = record["exit_record"]
    if not isinstance(evidence, dict) or set(evidence) != {"kind", "session_id", "transcript", "execution_id"}:
        raise ValueError("Original completed native exit record is unavailable; preserve claims")
    if (
        evidence["kind"] != "codex_command"
        or sessions.process_liveness().native_uuid(evidence["session_id"]) != evidence["session_id"]
    ):
        raise ValueError("Original exit requires its native observing session")
    if not isinstance(evidence["execution_id"], str) or not evidence["execution_id"]:
        raise ValueError("Original exit requires its native execution ID")
    observer = {"harness": "codex", "session_id": evidence["session_id"]}
    path = Path(evidence["transcript"])
    if not path.is_absolute():
        raise ValueError("Original exit requires its absolute native observing transcript")
    rows = own_records(observer, path, termination.transcript_snapshot(observer, path)[0])
    events = [row for row in rows if command_helpers().execution_item(row).get("id") == evidence["execution_id"]]
    if len(events) != 1:
        raise ValueError("Original exit execution is absent or ambiguous")
    row = events[0]
    payload = row.get("payload", {})
    item = command_helpers().execution_item(row)
    if (
        row.get("type") != "event_msg"
        or payload.get("type") != "item_completed"
        or payload.get("thread_id") != evidence["session_id"]
        or item.get("status") != "completed"
        or type(item.get("exit_code")) is not int
        or item["exit_code"] != 0
        or any(type(payload.get(key)) is not int for key in ("started_at_ms", "completed_at_ms"))
    ):
        raise ValueError("Original native command did not successfully complete")
    native_command(item.get("command"), record["session_id"])
    start = datetime.fromtimestamp(payload["started_at_ms"] / 1000, UTC)
    end = datetime.fromtimestamp(payload["completed_at_ms"] / 1000, UTC)
    if not start < end <= termination.stamp(row["timestamp"]) <= end + timedelta(seconds=1):
        raise ValueError("Original native command interval is uncertain")
    return OriginalExit(start, end, hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest())


def verify(record: dict[str, Any], session: str) -> dict[str, Any]:
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
    exit_evidence = original_exit(record)
    ended = exit_evidence.ended_at
    ended_at = ended.isoformat()
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
    source = record.get("claim")
    url = record.get("claim_url")
    if not isinstance(source, dict) or source.get("session") != record.get("claim_session", identity) or not isinstance(url, str):
        raise ValueError("Historical recovery requires the exact original claim and URL")
    created = record.get("claim_created_at")
    if not created or not exit_evidence.started_at <= termination.stamp(created) <= ended:
        raise ValueError("Original native execution does not enclose the claim creation; attachment is uncertain")
    if not original_claim_output(rows, url, source, created_at=created):
        raise ValueError("Native root lacks the original claim's direct creation receipt")
    refuse_duplicate_receipt(record, roots)
    verify_processes(record, ended)
    if (
        any(termination.transcript_snapshot(record, path) != snapshot for path, snapshot in snapshots.items())
        or transcript_paths(record, roots) != paths
        or configured_roots(record) != roots
        or closure.source_records({identity}).get(identity) != canonical
        or closure.action_pending(identity)
        or original_exit(record) != exit_evidence
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
        exit_digest=exit_evidence.digest,
    )
    termination.validate(data, session)
    termination._verify_artifacts(data["retained_artifacts"])
    return data


def verify_processes(record: dict[str, Any], ended: datetime) -> None:
    shared = sessions.process_liveness()
    shared.verify_process_exit(
        {**record, "_inventory": True, "_historical_exit": True},
        rows=termination.inventory(),
        ended_at=ended,
    )


def refuse_duplicate_receipt(record: dict[str, Any], roots: set[Path]) -> None:
    for root in roots:
        for path in root.rglob("*.jsonl"):
            if record["claim_url"].encode() not in path.read_bytes():
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
