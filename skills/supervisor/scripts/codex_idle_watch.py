#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Emit turn-end notices for explicit Codex sessions; notices are not completion."""

import argparse
import json
import time
from pathlib import Path

from session_record import load_ledger, session_status
from capacity_retry import locked_state, resume, retry_status


def poll(ledger: Path, seen: dict, idle_seconds: float, now: float,
         capacity_state: dict | None = None, state_path: Path | None = None,
         resume_capacity: bool = False) -> list[dict]:
    notices = []
    for entry in load_ledger(ledger):
        if entry["harness"] != "codex":
            continue
        try:
            status = session_status(entry)
            retry = None
            if capacity_state is not None:
                retry = (resume(entry, status, capacity_state, state_path, now)
                         if resume_capacity and status["capacity_stop"] else
                         retry_status(entry, status, capacity_state))
        except (OSError, ValueError, TypeError) as error:
            key = ("error", str(error))
            if seen.get(entry["session_id"]) != key:
                notices.append(
                    {
                        "session_id": entry["session_id"],
                        "error": str(error),
                        "clean": None,
                    }
                )
                seen[entry["session_id"]] = key
            continue
        key = (status["mtime"], status["size"], json.dumps(retry, sort_keys=True))
        if (
            status["at_turn_end"]
            and now - status["mtime"] >= idle_seconds
            and seen.get(entry["session_id"]) != key
        ):
            seen[entry["session_id"]] = key
            notices.append(
                {
                    "session_id": entry["session_id"],
                    "turn_end": status["turn_end"],
                    "idle_seconds": int(now - status["mtime"]),
                    "clean": None,
                    "state": status["state"],
                    "capacity_retry": retry,
                }
            )
    return notices


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--idle-seconds", type=float, default=90)
    parser.add_argument("--interval", type=float, default=45)
    parser.add_argument("--deadline-seconds", type=float, default=1800)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--capacity-state", type=Path,
                        help="persistent retry budget shared with checks and the digest")
    parser.add_argument("--resume-capacity", action="store_true",
                        help="opt in to bounded exact-thread continue sends")
    args = parser.parse_args()
    if min(args.idle_seconds, args.interval, args.deadline_seconds) <= 0:
        parser.error("timings must be positive")
    if args.resume_capacity and not args.capacity_state:
        parser.error("--resume-capacity requires --capacity-state")
    deadline = time.monotonic() + args.deadline_seconds
    seen = {}
    while True:
        try:
            if args.capacity_state:
                with locked_state(args.capacity_state) as state:
                    notices = poll(args.ledger, seen, args.idle_seconds, time.time(),
                                   state, args.capacity_state, args.resume_capacity)
            else:
                notices = poll(args.ledger, seen, args.idle_seconds, time.time())
            for notice in notices:
                print(json.dumps(notice), flush=True)
        except (OSError, ValueError, TypeError) as error:
            print(json.dumps({"error": str(error)}), flush=True)
            return 1
        if args.once or time.monotonic() >= deadline:
            return 0
        time.sleep(min(args.interval, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    raise SystemExit(main())
