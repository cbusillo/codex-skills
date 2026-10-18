#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Map explicit sessions to close-out candidates, never to automatic exit authority."""

import argparse
import json
from pathlib import Path

from session_record import load_ledger, session_status


def closeout_request(entry: dict, status: dict) -> dict | None:
    """Route a completed response to verification, never grant exit authority."""
    if (entry.get("supervisor_owned") is not True or not status["safe_verdict"]
            or status["background_tasks"]):
        return None
    return {
        "action": "verify_finished_session",
        "session_id": entry["session_id"],
        "harness": entry["harness"],
        "handoff_url": entry.get("handoff_url"),
        "verdict_at": status["timestamp"],
        "procedure": "skills/supervisor/SKILL.md",
    }


def candidates(ledger: Path, session_id: str | None = None) -> list[dict]:
    results = []
    entries = load_ledger(ledger)
    if session_id is not None:
        entries = [entry for entry in entries if entry["session_id"] == session_id]
        if len(entries) != 1:
            raise ValueError("notice must match exactly one native ledger session")
    for entry in entries:
        try:
            status = session_status(entry)
            status["closeout_request"] = closeout_request(entry, status)
            status["candidate"] = status["closeout_request"] is not None
            status["closeout_blockers"] = (
                status["background_tasks"] if entry.get("supervisor_owned") is True
                and status["safe_verdict"] else []
            )
            if status["closeout_blockers"]:
                status["next_action"] = "ask_worker_to_resolve_background_tasks"
            status["handoff_url"] = entry.get("handoff_url")
            status["needs_verification"] = [
                "issue handoff",
                "process and tab identity",
                "input line",
                "pending prompt",
            ]
            results.append(status)
        except (OSError, ValueError, TypeError) as error:
            results.append(
                {
                    "session_id": entry["session_id"],
                    "candidate": False,
                    "error": str(error),
                }
            )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--session-id", help="verify the exact native session named by a turn-end notice")
    args = parser.parse_args()
    try:
        results = candidates(args.ledger, args.session_id)
    except (OSError, ValueError, TypeError) as error:
        print(json.dumps({"error": str(error)}))
        return 1
    print(json.dumps(results, indent=2))
    return int(any("error" in result for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
