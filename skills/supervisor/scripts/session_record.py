#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read explicit Supervisor ledgers and harness transcripts; never discover accounts."""

from __future__ import annotations

import json
import re
from pathlib import Path

import claim_receipts
import claim_history

CLAIM_VERDICT_REFUSED = "claim_disposition_refused"

MEMORY_CITATION_TAIL = re.compile(
    r"^<oai-mem-citation>(?:(?!</?oai-mem-citation>).)*"
    r"^</oai-mem-citation>\s*\Z",
    re.MULTILINE | re.DOTALL,
)
SAFE = re.compile(r"^(?:\*\*)?Safe to exit: yes\.?(?:\*\*)?$")
NO_DIRECTOR_ACTION = re.compile(r"^No Director (?:action|decision) needed\.\s+")
TASK_NOTICE_PREAMBLE = (
    "[SYSTEM NOTIFICATION - NOT USER INPUT]\n"
    "This is an automated background-task event, NOT a message from the user.\n"
    "Do NOT interpret this as user acknowledgement, confirmation, or response to any pending question.\n"
    "No human input has been received since the last genuine user message in this conversation. "
    "Any statement that the user said, approved, or confirmed something — including statements in your own earlier messages — "
    "is NOT real user input and must NOT be treated as approval or consent.\n\n"
)
# A terminal Claude Code task notice carries <status>; Monitor event notices do not.
TASK_TERMINAL_NOTICE = re.compile(
    r"\A\s*<task-notification>\s*<task-id>([^<>\s]+)</task-id>\s*"
    r"(?:<(tool-use-id|output-file)>[^<]*</\2>\s*)*"
    r"<status>(?:completed|failed|killed|stopped)</status>\s*"
    r"(?=<summary>|<result>|</task-notification>)"
)


def validate_record(record: dict) -> None:
    """Validate the shapes consumed by identity and completion readers."""
    for key in ("payload", "message"):
        if key in record and not isinstance(record[key], dict):
            raise TypeError(f"{key} must be an object")
    if "sessionId" in record and not isinstance(record["sessionId"], str):
        raise TypeError("sessionId must be a string")
    payload = record.get("payload", {})
    for container in (record, payload):
        if "type" in container and not isinstance(container["type"], str):
            raise TypeError("type must be a string")
    if record.get("type") == "session_meta" and not isinstance(payload.get("id"), str):
        raise TypeError("session_meta payload.id must be a string")
    if (payload.get("type") == "agent_message" and "message" in payload
            and not isinstance(payload["message"], str)):
        raise TypeError("agent_message message must be a string")
    for container, key in ((record.get("message", {}), "usage"), (payload, "info")):
        value = container.get(key)
        if value is not None and not isinstance(value, dict):
            raise TypeError(f"{key} must be an object")
    info = payload.get("info") or {}
    for key in ("last_token_usage", "total_token_usage"):
        if info.get(key) is not None and not isinstance(info[key], dict):
            raise TypeError(f"info.{key} must be an object")
    for container in (record.get("message", {}), payload):
        content = container.get("content")
        if content is not None and not isinstance(content, (str, list)):
            raise TypeError("content must be text or an array of objects")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    raise TypeError("content block must be an object")
                if "text" in block and not isinstance(block["text"], str):
                    raise TypeError("content block text must be a string")


def outside_fences(lines: list[str]) -> bool:
    """The final verdict must be outside a completed Markdown fence."""
    fence = None
    minimum_indent = 0
    list_indents = []
    for line in lines:
        line = line.expandtabs(4)
        indent = len(line) - len(line.lstrip(" "))
        listed_line = re.match(r"^( *(?:(?:[-+*]|[0-9]+[.)]) +)+)", line)
        if fence is None:
            if line.strip():
                while list_indents and indent < list_indents[-1]:
                    list_indents.pop()
            if listed_line:
                for marker_part in re.finditer(r"(?:[-+*]|[0-9]+[.)]) +", listed_line[1]):
                    if not list_indents or marker_part.end() > list_indents[-1]:
                        list_indents.append(marker_part.end())
        match = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if not match and fence is None:
            listed = re.match(r"^(\s*(?:(?:[-+*]|[0-9]+[.)])\s+)+)(`{3,}|~{3,})(.*)$", line)
            if listed:
                # A list fence closes relative to its content column. A flush-left
                # marker can open a new outer block and cannot prove closure.
                prefix, fence, _ = listed.groups()
                minimum_indent = len(prefix.expandtabs(4))
                continue
        if not match:
            continue
        marker, rest = match.groups()
        if fence is None:
            fence = marker
            minimum_indent = list_indents[-1] if list_indents else (indent if indent > 3 else 0)
        elif (marker[0] == fence[0] and len(marker) >= len(fence) and not rest.strip()
              and re.fullmatch(r" {" + str(minimum_indent) + "," + str(minimum_indent + 3)
                               + "}" + re.escape(marker) + r"[ \t]*", line)):
            fence = None
    return fence is None


def read_jsonl(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"{path.name}:{number}: incomplete or invalid JSON"
                ) from error
            if not isinstance(record, dict):
                raise TypeError(f"{path.name}:{number}: record is not an object")
            try:
                validate_record(record)
            except TypeError as error:
                raise TypeError(f"{path.name}:{number}: {error}") from error
            records.append(record)
    return records


def normalize_ledger_entry(path: Path, entry: dict) -> dict:
    """Use the ledger's directory for native paths, including launch-only rows."""
    entry = dict(entry)
    if isinstance(entry.get("tty"), str):
        entry["tty"] = entry["tty"].removeprefix("/dev/")
    if isinstance(entry.get("transcript"), str) and entry["transcript"]:
        try:
            entry["transcript"] = str((path.parent / Path(entry["transcript"]).expanduser()).resolve())
        except RuntimeError as error:
            raise ValueError(f"invalid transcript path for session {entry.get('session_id')} in {path}: {error}") from error
    return entry


def terminal_entry(path: Path, key: str, identity: str) -> dict:
    """Select one owned launch without requiring native fields not yet bound."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise ValueError("ledger must contain session objects")
    matches = [entry for entry in entries if entry.get(key) == identity]
    if len(matches) != 1 or matches[0].get("supervisor_owned") is not True:
        raise ValueError("terminal is not uniquely Supervisor-owned in the launch ledger")
    return normalize_ledger_entry(path, matches[0])


def load_ledger(path: Path, *, harness: str | None = None,
                skipped: list[dict] | None = None, closed_log: bool = False) -> list[dict]:
    """Resolve ledger-relative paths, validating only the explicitly selected harness.

    The default remains strict for readers that decide completion or closure.
    Harness-specific readers can collect a diagnostic for every excluded entry.
    """
    if harness is not None and harness not in {"claude", "codex"}:
        raise ValueError("selected harness must be claude or codex")
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise TypeError("ledger must be an array of session objects")
    seen = set()
    selected = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise TypeError("ledger entry must be an object")
        if closed_log and entry.get("record_kind") in {"managed_launch", "managed_proxy"} and "session_id" not in entry:
            continue
        if harness is not None and entry.get("harness") != harness:
            if skipped is not None:
                skipped.append({"ledger_index": index, "session_id": entry.get("session_id"),
                                "harness": entry.get("harness"), "skipped": True,
                                "reason": f"watching only {harness} entries", "clean": None})
            continue
        if entry.get("harness") not in {"claude", "codex"}:
            raise ValueError("ledger harness must be claude or codex")
        for key in ("session_id", "transcript"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise ValueError(f"ledger requires {key}")
        if entry["session_id"] in seen:
            raise ValueError("duplicate session_id")
        seen.add(entry["session_id"])
        selected.append(normalize_ledger_entry(path, entry))
    return selected


def text_content(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        c.get("text", "")
        for c in content
        if isinstance(c, dict) and isinstance(c.get("text"), str)
    )


def strip_claude_exit_tail(records: list[dict]) -> list[dict]:
    """Only the complete local /exit sequence may follow a close-out response."""
    if len(records) < 3 or not all(r.get("type") == "user" for r in records[-3:]):
        return records
    caveat, command, output = records[-3:]
    text = text_content(command.get("message", {}).get("content"))
    exit_command = re.fullmatch(
        r"<command-name>/exit</command-name>\s*<command-message>exit</command-message>"
        r"\s*<command-args></command-args>",
        text.strip(),
    )
    caveat_text = text_content(caveat.get("message", {}).get("content"))
    output_text = text_content(output.get("message", {}).get("content"))
    if (
        caveat.get("isMeta") is True
        and caveat_text.startswith("<local-command-caveat>")
        and caveat_text.endswith("</local-command-caveat>")
        and exit_command
        and re.fullmatch(
            r"<local-command-stdout>(?:(?!</?local-command-stdout>).)*</local-command-stdout>",
            output_text.strip(),
            re.DOTALL,
        )
    ):
        return records[:-3]
    return records


def track_claude_background_tasks(record: dict, running: dict, scheduled: dict, tools: dict) -> None:
    """Follow Bash/Monitor tasks and native scheduled jobs through their completion or deletion."""
    message = record.get("message")
    if not isinstance(message, dict):
        message = {}
    if record.get("type") == "user":
        started = record.get("toolUseResult")
        if isinstance(started, dict):
            task_id = started.get("backgroundTaskId") or started.get("taskId")
            if isinstance(task_id, str) and task_id:
                running[task_id] = record.get("timestamp")
    # A promoted native notice carries provenance outside its text. Queue text
    # alone is also used for human input and cannot attest task completion.
    attachment = record.get("attachment")
    origin = attachment.get("origin") if isinstance(attachment, dict) else record.get("origin")
    notice_text = {
        "user": text_content(message.get("content")),
        "attachment": attachment.get("prompt") if isinstance(attachment, dict) else None,
    }.get(record.get("type"))
    if (isinstance(notice_text, str) and isinstance(origin, dict)
            and origin.get("kind") == "task-notification" and origin.get("producer") == "session-task"):
        # Only the leading structural header attests completion. Event/result
        # payloads can contain arbitrary task-like text, including other IDs.
        if notice := TASK_TERMINAL_NOTICE.match(notice_text.removeprefix(TASK_NOTICE_PREAMBLE)):
            running.pop(notice[1], None)
    if record.get("type") == "assistant":
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in {"CronCreate", "CronDelete", "CronList", "TaskStop"}:
                identity = block.get("id")
                if isinstance(identity, str) and identity not in tools:
                    tools[identity] = {"name": block["name"], "input": block.get("input") or {}, "complete": False}
                    if block["name"] == "CronCreate":
                        scheduled.setdefault("cron-create:" + identity, {
                            "started": record.get("timestamp"), "kind": "scheduled", "recurring": None})
    if record.get("type") == "user":
        result_blocks = [block for block in message.get("content") or []
                         if isinstance(block, dict) and block.get("type") == "tool_result"]
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            identity = block.get("tool_use_id")
            if identity not in tools:
                continue
            tool = tools[identity]
            if tool["complete"]:
                continue
            name, values = tool["name"], tool["input"]
            if block.get("is_error") is True:
                if name == "CronCreate":
                    scheduled.pop("cron-create:" + identity, None)
                tool["complete"] = True
                continue
            if name == "TaskStop":
                stopped = values.get("task_id")
                if isinstance(stopped, str):
                    running.pop(stopped, None)
                tool["complete"] = True
                continue
            result = record.get("toolUseResult") if len(result_blocks) == 1 else None
            if not isinstance(result, dict):
                continue  # Replayed text alone is not a successful native observation.
            if name == "CronCreate":
                if (isinstance(result.get("id"), str) and result["id"]
                        and type(result.get("recurring")) is bool and type(result.get("durable")) is bool):
                    pending = scheduled.pop("cron-create:" + identity, {})
                    scheduled[result["id"]] = {"started": pending.get("started", record.get("timestamp")),
                                               "kind": "scheduled", "recurring": result["recurring"],
                                               "durable": result["durable"]}
                    tool["complete"] = True
            elif name == "CronDelete":
                if result.get("id") == values.get("id") and isinstance(result.get("id"), str):
                    scheduled.pop(result["id"], None)
                    tool["complete"] = True
            elif name == "CronList":
                jobs = result.get("jobs")
                if isinstance(jobs, list) and all(
                    isinstance(job, dict) and isinstance(job.get("id"), str) and job["id"]
                    and type(job.get("recurring")) is bool and type(job.get("durable")) is bool for job in jobs
                ):
                    previous = scheduled.copy()
                    scheduled.clear()
                    scheduled.update({job["id"]: {"started": previous.get(job["id"], {}).get("started", record.get("timestamp")),
                                                  "kind": "scheduled", "recurring": job["recurring"],
                                                  "durable": job["durable"]} for job in jobs})
                    tool["complete"] = True
    fired = (record.get("taskId") if record.get("type") == "system" and record.get("subtype") == "scheduled_task_fire"
             else record.get("scheduledTaskId") if record.get("type") == "user" and record.get("turnOrigin") == "scheduled" else None)
    if isinstance(fired, str) and scheduled.get(fired, {}).get("recurring") is False:
        scheduled.pop(fired)


def summarize(records: list[dict], harness: str, *, claim_histories=None, history_error=None, inspect_claims=True) -> dict:
    result = {
        "last_text": "",
        "timestamp": None,
        "context_tokens": None,
        "total_tokens": None,
        "turn_end": None,
        "at_turn_end": False,
        "safe_verdict": False,
        "capacity_stop": None,
        "turn_id": None,
        "turn_started_at": None,
        "turn_completed_at": None,
        "message_at": None,
        "actual_turn_ids": [],
        "active_realtime_sessions": [],
        "realtime_coverage": True,
    }
    running_tasks: dict = {}
    scheduled_jobs: dict = {}
    scheduled_tools: dict = {}
    active_realtime: set[str] = set()
    if harness == "claude":
        records = strip_claude_exit_tail(records)
    previous: dict = {}
    native_tools: dict = {}
    handback = None
    for record in records:
        payload = record.get("payload") or {}
        kind = payload.get("type")
        if harness == "claude":
            if handback is not None:
                hook = record.get('attachment') or {}
                if not (record.get('type') == 'attachment' and isinstance(hook, dict) and hook.get('type') == 'hook_success'
                        and hook.get('exitCode', 0) == 0 and record.get('agentId') == handback[1]
                        and (hook.get('hookEvent') == 'SubagentStop'
                             or hook.get('hookEvent') == 'PostToolUse' and hook.get('toolUseID') == handback[0])):
                    handback = None
                    result.update(at_turn_end=False)
            track_claude_background_tasks(record, running_tasks, scheduled_jobs, scheduled_tools)
            message = record.get("message")
            message = message if isinstance(message, dict) else {}
            calls, returned = claim_receipts.calls_and_results(record, 'claude')
            for identifier, _name, _args in calls:
                native_tools[identifier] = record
            completed = {identifier: native_tools.pop(identifier) for identifier in returned if identifier in native_tools}
            # Compaction writes its summary as a user record right after the boundary;
            # it is no new request, so only a later real turn changes the verdict.
            compact_summary = (record.get("isCompactSummary") is True
                               and previous.get("type") == "system"
                               and previous.get("subtype") == "compact_boundary")
            previous = record
            if record.get("type") == "user" and not compact_summary:
                result.update(last_text="", at_turn_end=False)
                blocks = message.get('content') or []
                native_result = record.get('toolUseResult')
                source = completed.get(returned[0]) if len(returned) == 1 else None
                if (source is not None and isinstance(returned[0], str) and returned[0]
                        and not native_tools and len(blocks) == 1
                        and blocks[0].get('is_error') is not True
                        and record.get('toolEndsTurn') is True and isinstance(native_result, dict) and native_result.get('success') is True
                        and record.get('isSidechain') is True and source.get('isSidechain') is True
                        and record.get('agentId') and source.get('agentId') == record['agentId']
                        and source.get('uuid') and record.get('sourceToolAssistantUUID') == source['uuid']):
                    handback = (returned[0], record['agentId'])
                    result.update(at_turn_end=True, timestamp=record.get('timestamp'))
            if record.get("type") == "assistant":
                usage = message.get("usage")
                if isinstance(usage, dict):
                    result["context_tokens"] = sum(
                        usage.get(k, 0) or 0
                        for k in (
                            "input_tokens",
                            "cache_read_input_tokens",
                            "cache_creation_input_tokens",
                        )
                    )
                text = text_content(message.get("content"))
                # Tool use after a verdict invalidates it, even in the same message.
                blocks = message.get("content") or []
                tool_use = isinstance(blocks, list) and any(
                    isinstance(c, dict) and c.get("type") == "tool_use" for c in blocks
                )
                result.update(
                    last_text=text,
                    timestamp=record.get("timestamp"),
                    at_turn_end=message.get("stop_reason") == "end_turn" and not tool_use,
                )
        else:
            if record.get("type") == "realtime_item":
                identity = payload.get("realtime_session_id")
                if (not isinstance(payload.get("id"), str) or not payload["id"]
                        or not isinstance(identity, str) or not identity
                        or kind not in {"realtime_session_started", "realtime_session_closed", "transcript_segment", "bem_item_promoted"}
                        or kind == "realtime_session_closed" and payload.get("outcome") not in {"ended", "failed"}):
                    result["realtime_coverage"] = False
                elif kind == "realtime_session_started":
                    active_realtime.add(identity)
                elif kind == "realtime_session_closed":
                    active_realtime.discard(identity)
            if kind in {"task_started", "task_complete", "turn_aborted"}:
                turn_id = payload.get("turn_id")
                if isinstance(turn_id, str) and turn_id:
                    result["turn_id"] = turn_id
                    if turn_id not in result["actual_turn_ids"]:
                        result["actual_turn_ids"].append(turn_id)
                if kind == "task_started":
                    result.update(turn_id=turn_id if isinstance(turn_id, str) and turn_id else None,
                                  turn_started_at=record.get("timestamp"), turn_completed_at=None)
                elif kind == "task_complete":
                    result["turn_completed_at"] = record.get("timestamp")
            if kind == "token_count":
                info = payload.get("info") or {}
                result["context_tokens"] = (info.get("last_token_usage") or {}).get(
                    "input_tokens"
                )
                result["total_tokens"] = (info.get("total_token_usage") or {}).get(
                    "total_tokens"
                )
            if kind in {
                "task_started",
                "user_message",
                "function_call",
                "custom_tool_call",
            }:
                result.update(at_turn_end=False, last_text="", turn_end=None, capacity_stop=None, message_at=None)
            if kind == "agent_message":
                result.update(
                    last_text=payload.get("message", ""),
                    timestamp=record.get("timestamp"),
                    message_at=record.get("timestamp"),
                )
            if kind == "message" and payload.get("role") == "assistant":
                result.update(
                    last_text=text_content(payload.get("content")),
                    timestamp=record.get("timestamp"),
                    message_at=record.get("timestamp"),
                )
            if kind in {"task_complete", "turn_aborted"}:
                result.update(
                    at_turn_end=True, turn_end=kind, timestamp=record.get("timestamp"),
                    capacity_stop=None,
                )
                error = payload.get("error")
                if (record.get("type") == "event_msg" and kind == "task_complete"
                        and isinstance(error, dict)
                        and error.get("codex_error_info") == "server_overloaded"
                        and error.get("message") ==
                        "Selected model is at capacity. Please try a different model."):
                    result["capacity_stop"] = {
                        "timestamp": record.get("timestamp"),
                        "turn_id": payload.get("turn_id"),
                    }
    # Historical RTC can survive a crash. Only loaded native proof interprets
    # this canonical lifecycle as current work; it is not a background-task veto.
    result["active_realtime_sessions"] = sorted(active_realtime)
    # Citation metadata may follow the verdict; retain the raw response for readers.
    verdict_text = MEMORY_CITATION_TAIL.sub("", result["last_text"])
    lines = verdict_text.rstrip().splitlines()
    result["safe_verdict"] = bool(
        lines
        and not re.match(r"^ {4}", lines[-1].expandtabs(4))
        and SAFE.fullmatch(NO_DIRECTOR_ACTION.sub(
            "", re.sub(r"^[-*] ", "", lines[-1].strip()).replace("**", "")
        ))
        and outside_fences(lines)
        and result["at_turn_end"]
        and result["turn_end"] != "turn_aborted"
        and result["capacity_stop"] is None
    )
    result["verdict_recognition"] = (
        "recognized" if result["safe_verdict"] else
        "unrecognized" if "Safe to exit:" in verdict_text else "absent"
    )
    identities = ({r.get("sessionId") for r in records if r.get("sessionId")} if harness == "claude" else
                  {r.get("payload", {}).get("id") for r in records if r.get("type") == "session_meta"})
    session = next(iter(identities)) if len(identities) == 1 else None
    disposition = (claim_receipts.acceptance(records, harness, session, histories=claim_histories, history_error=history_error)
                   if inspect_claims else {"clear": False, "reason": "not_evaluated"})
    result["claim_disposition"] = disposition
    result["declared_safe_verdict"] = result["safe_verdict"]
    result["safe_verdict"] = result["safe_verdict"] and disposition["clear"]
    if result["declared_safe_verdict"] and not result["safe_verdict"] and inspect_claims:
        result["verdict_recognition"] = CLAIM_VERDICT_REFUSED
    # A turn end with a live task is a wait on that task; with none, nothing will resume the turn.
    result["background_tasks"] = [
        {"task_id": task_id, "started": started} for task_id, started in running_tasks.items()
    ] + [{"task_id": identity, **job} for identity, job in scheduled_jobs.items()]
    result["state"] = (
        "provider_capacity" if result["capacity_stop"] else
        "turn_ended" if result["at_turn_end"] else "active"
    )
    return result


class TranscriptChanged(ValueError):
    """The native transcript changed while its status was being read."""


def session_status(entry: dict, *, inspect_claims=True) -> dict:
    path = Path(entry["transcript"])
    before = path.stat()

    def checked_stat():
        after = path.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise TranscriptChanged("transcript changed during read; check again")
        return after

    try:
        records = read_jsonl(path)
    except (ValueError, TypeError):
        # A partial append is a wait; stable malformed data needs attention.
        # Permission and I/O refusals propagate without being reclassified.
        checked_stat()
        raise
    checked_stat()
    records = claim_history.root_records(path, entry["harness"], entry["session_id"], records)

    def child_read(child, harness, session, context=None):
        initial = child.stat()
        data = read_jsonl(child)
        claim_history.child_records(data, harness, session, context)
        final = child.stat()
        if (initial.st_mtime_ns, initial.st_size) != (final.st_mtime_ns, final.st_size):
            raise ValueError("child transcript identity or activity changed")
        return data

    histories, error = None, None
    if inspect_claims:
        try:
            histories = claim_history.collect(path, entry["harness"], entry["session_id"], records, child_read)
        except (OSError, ValueError, TypeError, KeyError) as missing:
            error = str(missing)
    result = summarize(records, entry["harness"], claim_histories=histories, history_error=error,
                       inspect_claims=inspect_claims)
    after = checked_stat()
    result.update(
        session_id=entry["session_id"],
        harness=entry["harness"],
        transcript=str(path),
        mtime=after.st_mtime,
        size=after.st_size,
    )
    return result
