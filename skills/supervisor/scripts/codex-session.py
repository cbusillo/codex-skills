#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets==17.3", "aiohttp==3.14.5"]
# ///
"""Exact-thread Codex resume, queue, steer and interrupt under the ownership lease."""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))
import github_plan_closure as closure
from session_record import LAUNCH_LEDGER_ENV, terminal_entry
github_agent = importlib.import_module("github_agent")
native_operations = importlib.import_module("github_plan_native")
queue_input = native_operations.queue_input
read_queue = native_operations.read_queue
read_thread = native_operations.read_thread
cancel_queue = native_operations.cancel_queue
QueueNotSent = native_operations.QueueNotSent
NativeRequestRejected = native_operations.NativeRequestRejected
from codex_session_proxy import run_client, managed_argv, source_endpoint, read_launch_receipt


def recorded_lead_home(session: str, ledger: Path | None = None) -> Path:
    if ledger is None and (configured := os.environ.get(LAUNCH_LEDGER_ENV)):
        ledger = Path(configured)
    agent = github_agent.running_agent()
    identities = set(github_agent.native_sessions(agent).values())
    if ledger is None or not ledger.is_absolute() or len(identities) != 1:
        raise ValueError("queue needs --codex-home or a managed launch ledger with one current native worker")
    worker = terminal_entry(ledger, "session_id", identities.pop())
    lead = terminal_entry(ledger, "session_id", session)
    home = lead.get("codex_home")
    if (worker.get("harness") != agent or worker.get("lead_session_id") != session
            or lead.get("harness") != "codex" or not isinstance(home, str) or not Path(home).is_absolute()):
        raise ValueError("queue without --codex-home requires this worker's recorded Codex lead and source home")
    return Path(home)


def run(args) -> int:
    if args.command == "native":
        home = args.codex_home.resolve(strict=True)
        native = args.native_args[1:] if args.native_args[:1] == ["--"] else args.native_args
        rows = closure.log_rows()
        proxies = [r for r in rows if r.get("record_kind") == "managed_proxy"
                   and r.get("launch_pid") == args.proxy_parent and r.get("endpoint") == str(args.proxy)
                   and r.get("codex_home") == str(home)]
        if len(proxies) != 1 or os.getppid() != args.proxy_parent or closure.process_started_at(args.proxy_parent).isoformat() != proxies[0]["launch_started_at"]:
            raise ValueError("native client has no current guarded parent")
        if not native or Path(native[0]).name != "codex":
            raise ValueError("native bootstrap requires the selected codex executable")
        closure.register_launch(native, str(home), proxy=proxies[0])
        os.execvpe(native[0], native, {**os.environ, "CODEX_HOME": str(home)})
        return 0
    if args.command == "launch":
        home = args.codex_home.expanduser().resolve(strict=True)
        native = args.native_args
        if native[:1] == ["--"]:
            native = native[1:]
        native = managed_argv(home, native)
        if getattr(args, "launch_receipt", None):
            return asyncio.run(run_client(home, native, launch_receipt=args.launch_receipt))
        return asyncio.run(run_client(home, native))
    if args.command == "status" and getattr(args, "launch_receipt", None):
        print(json.dumps(read_launch_receipt(args.launch_receipt)))
        return 0
    canonical = str(uuid.UUID(args.session_id))
    if canonical != args.session_id:
        raise ValueError(f"use the canonical native thread UUID {canonical}")
    if args.command == "status":
        rows = [r for r in closure.log_rows() if r.get("session_id") == args.session_id] if closure.closed_log().exists() else []
        if len(rows) > 1:
            raise ValueError("native session generation is not unique")
        generation = (rows[0].get("resumed_generation") or {}) if rows else {}
        home = args.codex_home or (Path(generation["codex_home"]) if generation.get("codex_home") else None)
        status = {"session_id": args.session_id, **generation}
        if home is None:
            status["queue_error"] = "pass --codex-home to read this session's queue"
        else:
            try:
                status.update(asyncio.run(session_queue(home.expanduser().resolve(strict=True), args.session_id)))
            except (OSError, ValueError) as error:
                status["queue_error"] = str(error)
        print(json.dumps(status))
        return 0
    home = (args.codex_home or recorded_lead_home(args.session_id, args.ledger)).expanduser().resolve(strict=True)
    if args.command == "queue-cancel":
        result = asyncio.run(cancel_queue(home, args.session_id, ledger=args.ledger,
            termination_record=args.termination_record, apply=args.apply,
            queued_submission_id=args.queued_submission_id, entry_sha256=args.entry_sha256))
        print(json.dumps(result))
        return 0
    if args.command in {"queue", "steer", "interrupt"}:
        message = args.message_file.read_text() if args.message_file else args.message
        receipt = asyncio.run(queue_input(home, args.session_id, message, expected_turn_id=getattr(args, "expected_turn_id", None),
                                          interrupt=args.command == "interrupt"))
        print(json.dumps({"ok": True, **receipt}))
        return 0
    endpoint = source_endpoint(args.session_id, home, for_resume=True)
    command = ["codex", "--remote", "unix://" + str(endpoint), "resume", args.session_id]
    return asyncio.run(run_client(home, command, session=args.session_id))


async def session_queue(home: Path, session: str) -> dict:
    async with native_operations.connection(home) as request:
        thread = await read_thread(request, session)
        digests = await read_queue(request, session)
    return {"thread_state": thread["state"], "queue": [
        {"queuedSubmissionId": entry_id, "clientUserMessageId": client, "entry_sha256": digest}
        for entry_id, (client, digest) in digests.items()]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("launch", "native", "resume", "queue", "steer", "interrupt", "queue-cancel", "status"))
    parser.add_argument("--session-id")
    parser.add_argument("--launch-receipt", type=Path)
    parser.add_argument("--codex-home", type=Path,
                        help="Source account home; omit for a managed lead lookup from --ledger or the launcher")
    parser.add_argument("--expected-turn-id", help="Exact observed active turn ID; required for steer and interrupt")
    parser.add_argument("--proxy", type=Path)
    parser.add_argument("--proxy-parent", type=int)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--message")
    group.add_argument("--message-file", type=Path)
    authority = parser.add_mutually_exclusive_group()
    authority.add_argument("--ledger", type=Path, help="Supervisor ledger for queue home lookup or queue-cancel")
    authority.add_argument("--termination-record", type=Path)
    parser.add_argument("--queued-submission-id")
    parser.add_argument("--entry-sha256")
    parser.add_argument("--apply", action="store_true")
    args, native = parser.parse_known_args()
    args.native_args = native
    if args.command == "queue" and args.codex_home is not None and args.ledger is not None:
        parser.error("queue accepts only one source home: --codex-home or --ledger")
    if args.launch_receipt and (args.command not in {"launch", "status"} or args.session_id):
        parser.error("--launch-receipt belongs to launch or status, without --session-id")
    if args.command not in {"launch", "native"} and (native or not (args.session_id or args.launch_receipt)):
        parser.error("use --session-id with resume, queue, steer, interrupt or status; native argv belongs to launch")
    if args.command not in {"status", "queue"} and args.codex_home is None:
        parser.error("this command requires --codex-home; only queue to a recorded lead can use the launch ledger")
    if args.command == "native" and (args.proxy is None or args.proxy_parent is None):
        parser.error("native is the internal guarded child bootstrap")
    if args.command in {"queue", "steer"} and args.message is None and args.message_file is None:
        parser.error("queue and steer require --message or --message-file")
    if args.command in {"steer", "interrupt"} and not (args.expected_turn_id and args.expected_turn_id.strip()):
        parser.error(args.command + " requires --expected-turn-id")
    if args.command not in {"steer", "interrupt"} and args.expected_turn_id is not None:
        parser.error("--expected-turn-id belongs to steer and interrupt")
    if args.command == "queue-cancel" and args.ledger is None and args.termination_record is None:
        parser.error("queue-cancel requires --ledger or --termination-record")
    if args.command != "queue-cancel" and (
            args.ledger and args.command != "queue" or args.termination_record
            or args.apply or args.queued_submission_id or args.entry_sha256):
        parser.error("cleanup arguments belong to queue-cancel")
    try:
        return run(args)
    except QueueNotSent as error:
        refused = isinstance(error.__cause__, (PermissionError, NativeRequestRejected))
        print(json.dumps({"ok": False, "error": str(error), "not_sent": not refused}))
        return 1
    except (KeyError, TypeError):
        if args.command != "queue-cancel":
            raise
        print(json.dumps({"ok": False, "error": "Malformed source record; preserve current state."}))
        return 1
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
