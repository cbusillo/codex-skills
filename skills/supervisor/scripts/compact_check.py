#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import argparse
import json
import time
from pathlib import Path

from capacity_retry import load_state, retry_status
from check_changes import changes
from session_record import load_ledger, session_status


def snapshot(ledger: Path, capacity_state: Path | None = None, *, entries: list[dict] | None = None) -> list[dict]:
    state = load_state(capacity_state) if capacity_state else None
    results = []
    for entry in load_ledger(ledger) if entries is None else entries:
        try:
            result = session_status(entry)
            result["idle_seconds"] = max(0, int(time.time() - result["mtime"]))
            if state is not None:
                result["capacity_retry"] = retry_status(entry, result, state)
            results.append(result)
        except (OSError, ValueError, TypeError) as error:
            results.append({"session_id": entry["session_id"], "error": str(error)})
    return results


class SessionAbsent(ValueError):
    pass


def closeout_request(entry: dict, status: dict) -> dict | None:
    if entry.get("supervisor_owned") is not True or not status["safe_verdict"] or status["background_tasks"]:
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
    entries = load_ledger(ledger)
    if session_id is not None:
        entries = [entry for entry in entries if entry["session_id"] == session_id]
        if len(entries) != 1:
            raise SessionAbsent("notice must match exactly one native ledger session")
    results = snapshot(ledger, entries=entries)
    for entry, result in zip(entries, results, strict=True):
        if "error" in result:
            result["candidate"] = False
            continue
        result["closeout_request"] = closeout_request(entry, result)
        result["candidate"] = result["closeout_request"] is not None
        result["closeout_blockers"] = (
            result["background_tasks"] if entry.get("supervisor_owned") is True and result["safe_verdict"] else []
        )
        if result["closeout_blockers"]:
            result["next_action"] = "ask_worker_to_resolve_background_tasks"
        result["handoff_url"] = entry.get("handoff_url")
        result["needs_verification"] = ["issue handoff", "process and tab identity", "input line", "pending prompt"]
    return results


def check(ledger: Path, state: Path, capacity_state: Path | None = None) -> dict:
    entries = {entry["session_id"]: entry for entry in load_ledger(ledger)}
    sources = {ledger.resolve(), *(Path(entry["transcript"]).resolve() for entry in entries.values())}
    if capacity_state:
        sources.add(capacity_state.resolve())
    if state.resolve() in sources:
        raise ValueError("check state must be separate from its sources")
    rows = []
    for result in snapshot(ledger, capacity_state, entries=list(entries.values())):
        entry = entries[result["session_id"]]
        row = {
            "session_id": result["session_id"],
            "harness": entry["harness"],
            "transcript": entry["transcript"],
            "handoff_url": entry.get("handoff_url"),
        }
        if "error" in result:
            row.update(error=result["error"], action="inspect_coverage_gap")
        else:
            request = closeout_request(entry, result)
            action = (
                "verify_finished_session"
                if request
                else "resolve_background_tasks"
                if result["safe_verdict"] and result["background_tasks"]
                else "provider_capacity"
                if result["capacity_stop"]
                else "inspect_idle_session"
                if result["state"] == "turn_ended"
                else None
            )
            row.update(
                state=result["state"],
                action=action,
                timestamp=result["timestamp"],
                mtime=result["mtime"],
                size=result["size"],
                context_tokens=result["context_tokens"],
                safe_verdict=result["safe_verdict"],
                background_tasks=result["background_tasks"],
                capacity_retry=result.get("capacity_retry"),
            )
            if result.get("realtime_coverage") is False:
                row.update(error="native realtime history coverage is incomplete", action="inspect_coverage_gap")
        rows.append(row)
    coverage = {
        "discovered": len(entries),
        "read": sum("error" not in row for row in rows),
        "complete": not any("error" in row for row in rows),
    }
    return {
        "sessions": changes(
            rows, state, [str(ledger.resolve()), str(capacity_state)], key="session_id", complete=coverage["complete"]
        ),
        "coverage": coverage,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read changed sessions and action candidates once; never authorize closure.")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--capacity-state", type=Path)
    args = parser.parse_args()
    try:
        report = check(args.ledger, args.state, args.capacity_state)
        for row in report["sessions"]:
            print(json.dumps(row, separators=(",", ":")))
        print(json.dumps({"coverage": report["coverage"]}))
        return int(not report["coverage"]["complete"])
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({"coverage": {"complete": False}, "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
