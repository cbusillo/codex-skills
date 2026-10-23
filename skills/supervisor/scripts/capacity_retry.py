#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Bound Codex capacity nudges across watcher restarts; never replay uncertain sends."""

import fcntl
import asyncio
import json
import math
import os
import socket
import stat
import subprocess
import tempfile
import time
import uuid
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from session_record import session_status
from iterm_tab import codex_daemon_endpoint

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))
import github_plan_closure
from github_plan_native import QueueNotSent, queue_input

BACKOFF_SECONDS = (60, 180, 600)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("capacity state must be an object")
    for native_id, row in state.items():
        if (not isinstance(native_id, str) or not isinstance(row, dict)
                or type(row.get("attempts")) is not int
                or not 0 <= row["attempts"] <= len(BACKOFF_SECONDS)
                or row.get("phase") not in {"waiting", "pending", "confirmed", "uncertain"}
                or not isinstance(row.get("target"), list)
                or len(row["target"]) != 2
                or not all(isinstance(value, str) for value in row["target"])
                or type(row.get("last_attempt")) not in {int, float}
                or not math.isfinite(row["last_attempt"]) or row["last_attempt"] < 0):
            raise ValueError("malformed capacity retry record")
    return state


def save_state(path: Path, state: dict) -> None:
    # A durable reservation precedes any send. Atomic replacement preserves it
    # across a crash; owner-only files can contain private target coordinates.
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def locked_state(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("another capacity watcher owns this state") from error
        yield load_state(path)


def target(entry: dict) -> list[str]:
    home = entry.get("codex_home")
    if isinstance(home, str) and home and not Path(home).expanduser().is_absolute():
        raise ValueError("codex_home must be absolute")
    return [str(Path(entry["transcript"]).resolve()),
            str(Path(home).expanduser().resolve()) if isinstance(home, str) and home else ""]


def retry_status(entry: dict, status: dict, state: dict) -> dict:
    row = state.get(entry["session_id"])
    if row and row["target"] != target(entry):
        return {"attempts": row["attempts"], "state": "target_changed"}
    attempts = row["attempts"] if row else 0
    phase = row["phase"] if row else "waiting"
    if (status["capacity_stop"] and attempts < len(BACKOFF_SECONDS)
            and phase not in {"pending", "uncertain"}):
        try:
            target_coordinates(entry)
        except (OSError, ValueError, TypeError):
            return {"attempts": attempts, "state": "blocked",
                    "reason": "Supervisor ownership, native UUID, home or daemon socket is unavailable"}
    summary = {"attempts": attempts, "state": (
        "reconciliation_required" if phase in {"pending", "uncertain"} else
        "exhausted" if attempts == len(BACKOFF_SECONDS) and status["capacity_stop"] else
        "backoff" if status["capacity_stop"] else "inactive"
    )}
    if summary["state"] == "backoff":
        stop_time = datetime.fromisoformat(status["capacity_stop"]["timestamp"])
        if stop_time.tzinfo is None:
            raise ValueError("capacity timestamp requires a timezone")
        summary["retry_at"] = max(stop_time.timestamp(), row["last_attempt"] if row else 0
                                  ) + BACKOFF_SECONDS[attempts]
        if time.time() >= summary["retry_at"]:
            summary["state"] = "retry_due"
    return summary


def target_coordinates(entry: dict) -> tuple[Path, Path]:
    if entry.get("harness") != "codex" or entry.get("supervisor_owned") is not True:
        raise ValueError("capacity resume requires an explicitly Supervisor-owned Codex session")
    # Never let queue resolve a human-readable name or use the caller's account.
    if str(uuid.UUID(entry["session_id"])) != entry["session_id"]:
        raise ValueError("capacity resume requires a canonical native UUID")
    transcript_name, home_name = target(entry)
    if not home_name:
        raise ValueError("capacity resume requires the session's configured codex_home")
    transcript, home = Path(transcript_name), Path(home_name)
    if not transcript.is_relative_to((home / "sessions").resolve()):
        raise ValueError("transcript is outside the configured home's live sessions")
    endpoint = codex_daemon_endpoint(home)
    if not stat.S_ISSOCK(endpoint.stat().st_mode):
        raise ValueError("configured home's daemon socket is unavailable")
    with socket.socket(socket.AF_UNIX) as probe:
        probe.settimeout(1)
        probe.connect(str(endpoint))
    return home, endpoint


def verified_target(entry: dict, expected: dict) -> tuple[Path, Path]:
    home, endpoint = target_coordinates(entry)
    fresh = session_status(entry)
    if fresh != expected or fresh["state"] != "provider_capacity":
        raise ValueError("session changed before capacity resume")
    return home, endpoint


def queue_continue(entry: dict, home: Path, endpoint: Path, expected: dict) -> None:
    # Reuse the catalog's preferred exact-thread nudge. No terminal composer is
    # touched, no daemon is started, and no authentication material is read.
    verified_target(entry, expected)
    if endpoint != codex_daemon_endpoint(home):
        raise ValueError("capacity queue endpoint changed")
    return asyncio.run(queue_input(home, entry["session_id"], "continue", transcript=Path(entry["transcript"])))


def readback_continue(entry: dict, before_size: int, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            # session_status rechecks native identity and a stable transcript.
            session_status(entry)
            with Path(entry["transcript"]).open("rb") as stream:
                stream.seek(before_size)
                suffix = stream.read().decode()
            records = [json.loads(line) for line in suffix.splitlines() if line.strip()]
            # A queued message lands as the turn's user message item.
            if any(record.get("type") == "response_item"
                   and record.get("payload", {}).get("type") == "message"
                   and record["payload"].get("role") == "user"
                   and record["payload"].get("content") == [{"type": "input_text", "text": "continue"}]
                   for record in records):
                return True
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def resume(entry: dict, status: dict, state: dict, path: Path, now: float) -> dict:
    summary = retry_status(entry, status, state)
    if summary["state"] not in {"backoff", "retry_due"} or entry.get("supervisor_owned") is not True:
        return summary
    native_id = entry["session_id"]
    previous = state.get(native_id)
    if now < summary["retry_at"]:
        return summary
    home, endpoint = verified_target(entry, status)
    row = {"attempts": summary["attempts"] + 1, "last_attempt": now,
           "target": target(entry), "phase": "pending"}
    state[native_id] = row
    save_state(path, state)
    try:
        queue_continue(entry, home, endpoint, status)
        row["phase"] = "confirmed" if readback_continue(entry, status["size"]) else "uncertain"
    except (github_plan_closure.LeaseBusy, QueueNotSent) as error:
        if previous is None:
            state.pop(native_id, None)
        else:
            state[native_id] = previous
        save_state(path, state)
        if isinstance(error, QueueNotSent):
            raise
        return {"attempts": summary["attempts"], "state": "ownership_busy"}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        row["phase"] = "uncertain"
    try:
        save_state(path, state)
    except OSError:
        row["phase"] = "uncertain"
        raise
    return {"attempts": row["attempts"], "state": (
        "continue_confirmed" if row["phase"] == "confirmed" else "reconciliation_required"
    )}
