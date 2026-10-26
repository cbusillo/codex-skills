# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

FAILURES = {
    "expired": "Your access token could not be refreshed because your refresh token has expired. Please log out and sign in again.",
    "reused": "Your access token could not be refreshed because your refresh token was already used. Please log out and sign in again.",
    "revoked": "Your access token could not be refreshed because your refresh token was revoked. Please log out and sign in again.",
}
EVENTS = {
    **FAILURES,
    "refresh_attempt": "Refreshing token",
    "reload_changed": "Skipping token refresh because auth changed after guarded reload.",
}


def read_log(home: Path) -> dict:
    database = home / "logs_2.sqlite"
    if not database.is_file():
        return {"events": [], "processes": []}
    deadline = time.monotonic() + 1
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.1) as connection:
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        rows = connection.execute(
            "SELECT id, ts, ts_nanos, level, process_uuid, feedback_log_body FROM logs "
            "WHERE target = 'codex_login::auth::manager' ORDER BY id"
        )
        events = []
        for ident, seconds, nanos, level, process, body in rows:
            for kind, message in EVENTS.items():
                if message in (body or "") and (kind not in FAILURES or level == "ERROR"):
                    events.append(
                        {
                            "id": ident,
                            "ts_ns": seconds * 1_000_000_000 + nanos,
                            "event": kind,
                            "message": message,
                            "process": process
                            if isinstance(process, str) and re.fullmatch(r"pid:[0-9]+:[0-9a-f-]{36}", process)
                            else None,
                        }
                    )
        processes = [
            row[0]
            for row in connection.execute("SELECT DISTINCT process_uuid FROM logs")
            if isinstance(row[0], str) and re.fullmatch(r"pid:[0-9]+:[0-9a-f-]{36}", row[0])
        ]
        return {"events": events, "processes": processes}


def credential_modified_at(home: Path) -> int:
    try:
        return (home / "auth.json").stat().st_mtime_ns
    except FileNotFoundError:
        return 0


def process_inventory() -> list[dict]:
    result = subprocess.run(["ps", "-axo", "pid=,ppid=,lstart=,comm="], capture_output=True, text=True, timeout=2, check=True)
    processes = []
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=7)
        if len(fields) == 8 and ("codex" in fields[7].lower() or "context" in fields[7].lower()):
            processes.append(
                {"pid": int(fields[0]), "parent_pid": int(fields[1]), "started": " ".join(fields[2:7]), "executable": fields[7]}
            )
    return processes


def save(path: Path, data: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(data, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def observe(home: Path) -> dict:
    home = home.expanduser().resolve()
    directory = home / "skill-data/supervisor-auth"
    state_path = directory / "state.json"
    if not state_path.exists() and not (home / "logs_2.sqlite").is_file():
        return {"blocked": False}
    state = {}
    modified = 0
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if not isinstance(state, dict):
            state = {}
            raise ValueError("invalid auth evidence state")
        modified = credential_modified_at(home)
        with (directory / "state.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            modified = credential_modified_at(home)
            log = read_log(home)
            events = log["events"]
            failures = [event for event in events if event["event"] in FAILURES]
            latest = max(failures, key=lambda event: event["ts_ns"], default=None)
            if latest and latest["ts_ns"] > state.get("failure_ns", 0):
                if not state or modified != state.get("credential_modified_ns"):
                    evidence = directory / f"failure-{latest['ts_ns']}.json"
                    try:
                        processes = process_inventory()
                        process_error = None
                    except (OSError, subprocess.SubprocessError):
                        processes, process_error = [], "process_inventory_unavailable"
                    save(
                        evidence,
                        {
                            "captured_ns": time.time_ns(),
                            "events": events,
                            "home_log_processes": log["processes"],
                            "processes": processes,
                            "process_error": process_error,
                            "credential_modified_ns": modified,
                        },
                    )
                    state = {"evidence": str(evidence), "credential_modified_ns": modified, "episode_ns": latest["ts_ns"]}
                state.update(failure_ns=latest["ts_ns"], reason=latest["event"])
                save(state_path, state)
            if not state:
                return {"blocked": False}
            if not (modified or state.get("credential_modified_ns")):
                return {"blocked": False, "diagnostic": "credential_generation_unavailable"}
            return {**state, "blocked": modified <= state["failure_ns"]}
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return {**state, "blocked": bool(state) and modified <= state["failure_ns"], "diagnostic": "auth_evidence_unavailable"}


def require_usable(home: Path) -> None:
    if observe(home).get("blocked"):
        raise ValueError(
            "Codex account needs sign-in; launches on this account are paused. Sign in to this account in Codex, then retry."
        )


def notices(homes: dict[str, Path]) -> list[dict]:
    result = []
    for name, home in homes.items():
        health = observe(home)
        if health.get("blocked"):
            result.append(
                {
                    "auth_failure": {"account": name, "episode_ns": health["episode_ns"]},
                    "message": f"Codex {name} needs you to sign in again. Launches on this account are paused.",
                    "clean": None,
                }
            )
        elif health.get("diagnostic"):
            result.append({"auth_diagnostic": {"account": name, "reason": health["diagnostic"]}, "clean": None})
    return result
