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


def poll(ledger: Path, seen: dict, idle_seconds: float, now: float) -> list[dict]:
    notices = []
    for entry in load_ledger(ledger):
        if entry["harness"] != "codex":
            continue
        status = session_status(entry)
        key = (status["mtime"], status["size"])
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
    args = parser.parse_args()
    if min(args.idle_seconds, args.interval, args.deadline_seconds) <= 0:
        parser.error("timings must be positive")
    deadline = time.monotonic() + args.deadline_seconds
    seen = {}
    while True:
        try:
            for notice in poll(args.ledger, seen, args.idle_seconds, time.time()):
                print(json.dumps(notice), flush=True)
        except (OSError, ValueError, TypeError) as error:
            print(json.dumps({"error": str(error)}), flush=True)
            return 1
        if args.once or time.monotonic() >= deadline:
            return 0
        time.sleep(min(args.interval, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    raise SystemExit(main())
