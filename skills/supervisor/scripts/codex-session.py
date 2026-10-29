#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets==17.3", "aiohttp==3.14.5"]
# ///
"""Exact-thread Codex resume and queue operations under the ownership lease."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))
import github_plan_closure as closure
from github_plan_native import queue_input, cancel_queue
from codex_session_proxy import run_client, managed_argv, source_endpoint


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
        return asyncio.run(run_client(home, native))
    if args.command == "status" and getattr(args, "launch_generation", None):
        print(json.dumps(closure.launch_status(args.launch_generation, args.codex_home)))
        return 0
    canonical = str(uuid.UUID(args.session_id))
    if canonical != args.session_id:
        raise ValueError(f"use the canonical native thread UUID {canonical}")
    if args.command == "status":
        rows = [r for r in closure.log_rows() if r.get("session_id") == args.session_id]
        if len(rows) != 1:
            raise ValueError("native session generation is not unique")
        print(json.dumps({"session_id": args.session_id, **rows[0].get("resumed_generation", {})}))
        return 0
    home = args.codex_home.expanduser().resolve(strict=True)
    if args.command == "queue-cancel":
        result = asyncio.run(cancel_queue(home, args.session_id, ledger=args.ledger,
            termination_record=args.termination_record, apply=args.apply,
            queued_submission_id=args.queued_submission_id, entry_sha256=args.entry_sha256))
        print(json.dumps(result))
        return 0
    if args.command == "queue":
        message = args.message_file.read_text() if args.message_file else args.message
        receipt = asyncio.run(queue_input(home, args.session_id, message))
        print(json.dumps({"ok": True, **receipt}))
        return 0
    endpoint = source_endpoint(args.session_id, home)
    command = ["codex", "--remote", "unix://" + str(endpoint), "resume", args.session_id]
    return asyncio.run(run_client(home, command, session=args.session_id))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("launch", "native", "resume", "queue", "queue-cancel", "status"))
    parser.add_argument("--session-id")
    parser.add_argument("--launch-generation")
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--proxy", type=Path)
    parser.add_argument("--proxy-parent", type=int)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--message")
    group.add_argument("--message-file", type=Path)
    authority = parser.add_mutually_exclusive_group()
    authority.add_argument("--ledger", type=Path)
    authority.add_argument("--termination-record", type=Path)
    parser.add_argument("--queued-submission-id")
    parser.add_argument("--entry-sha256")
    parser.add_argument("--apply", action="store_true")
    args, native = parser.parse_known_args()
    args.native_args = native
    if args.launch_generation and (args.command != "status" or args.session_id or not args.codex_home):
        parser.error("--launch-generation needs status --codex-home, without --session-id")
    if args.command not in {"launch", "native"} and (native or not (args.session_id or args.launch_generation)):
        parser.error("use --session-id with resume, queue or status; native argv belongs to launch")
    if args.command != "status" and args.codex_home is None:
        parser.error("resume and queue require --codex-home")
    if args.command == "native" and (args.proxy is None or args.proxy_parent is None):
        parser.error("native is the internal guarded child bootstrap")
    if args.command == "queue" and args.message is None and args.message_file is None:
        parser.error("queue requires --message or --message-file")
    if args.command == "queue-cancel" and args.ledger is None and args.termination_record is None:
        parser.error("queue-cancel requires --ledger or --termination-record")
    if args.command != "queue-cancel" and (args.ledger or args.termination_record or args.apply or args.queued_submission_id or args.entry_sha256):
        parser.error("cleanup arguments belong to queue-cancel")
    try:
        return run(args)
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
