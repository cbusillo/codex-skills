# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets==17.2"]
# ///

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

FAILURES = {
    "expired": "Your access token could not be refreshed because your refresh token has expired. Please log out and sign in again.",
    "reused": "Your access token could not be refreshed because your refresh token was already used. Please log out and sign in again.",
    "revoked": "Your access token could not be refreshed because your refresh token was revoked. Please log out and sign in again.",
    "invalid": "Your access token could not be refreshed. Please log out and sign in again.",
}
EVENTS = {
    **FAILURES,
    "refresh_attempt": "Refreshing token",
    "reload_changed": "Skipping token refresh because auth changed after guarded reload.",
    "auth_changed": "Reloaded auth, changed: true",
    "issuer_rejection": "Failed to refresh token",
}
RESETS = {"reload_changed", "auth_changed"}
PROCESS = re.compile(r"pid:([0-9]+):[0-9a-f-]{36}")
LOG_DELIVERY_SECONDS = 11


@dataclass(frozen=True)
class DaemonIdentity:
    pid: int
    started_ns: int
    stamp: str


@dataclass(frozen=True)
class AuthStatus:
    identity: DaemonIdentity
    requested_ns: int
    auth_method: str | None
    requires_auth: bool
    account_present: bool


def read_log(home: Path) -> list[dict]:
    database = home / "logs_2.sqlite"
    if not database.is_file():
        return []
    deadline = time.monotonic() + 1
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.1) as connection:
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        rows = connection.execute(
            "SELECT id, ts, ts_nanos, level, process_uuid, feedback_log_body FROM logs "
            "WHERE target = 'codex_login::auth::manager' ORDER BY id"
        )
        events = []
        for ident, seconds, nanos, level, process, body in rows:
            if not isinstance(process, str) or not PROCESS.fullmatch(process):
                continue
            for kind, message in EVENTS.items():
                if message not in (body or "") or kind in FAILURES and level != "ERROR":
                    continue
                event = {
                    "id": ident,
                    "ts_ns": seconds * 1_000_000_000 + nanos,
                    "event": kind,
                    "message": message,
                    "process": process,
                }
                if kind == "issuer_rejection":
                    status = re.search(r"\bstatus=(\d{3})(?:\D|$)", body or "")
                    if level != "ERROR" or not status or not re.search(r"Failed to refresh token(?:\s|$)", body or ""):
                        continue
                    event["status"] = int(status[1])
                events.append(event)
        return events


def event_key(event: dict) -> tuple:
    return event["process"], event["ts_ns"], event["id"], event["event"]


def merge_events(*groups: list[dict]) -> list[dict]:
    events = {event_key(event): event for group in groups for event in group}
    return sorted(events.values(), key=lambda event: (event["ts_ns"], event["id"]))


def read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as journal:
        lines = journal.read().splitlines()
    records = []
    for line in lines:
        try:
            batch = json.loads(line)
            if isinstance(batch, dict):
                records.append(batch)
        except ValueError:
            continue
    return records


def read_journal(path: Path) -> list[dict]:
    events = []
    for batch in read_records(path):
        try:
            for event in batch.get("events", []):
                if (
                    isinstance(event, dict)
                    and event.get("event") in EVENTS
                    and isinstance(event.get("process"), str)
                    and PROCESS.fullmatch(event["process"])
                    and all(type(event.get(key)) is int for key in ("id", "ts_ns"))
                ):
                    events.append({key: event[key] for key in ("id", "ts_ns", "event", "process")})
                    events[-1]["message"] = EVENTS[event["event"]]
                    if event["event"] == "issuer_rejection" and type(event.get("status")) is int:
                        events[-1]["status"] = event["status"]
        except (ValueError, TypeError, KeyError):
            continue
    return events


def journal_status(home: Path) -> dict:
    result = {"blocked": False, "episode_ns": None, "observed_ns": 0}
    for record in read_records(home / "skill-data/supervisor-auth/evidence.jsonl"):
        status = record.get("auth_observation")
        if (
            isinstance(status, dict)
            and type(status.get("blocked")) is bool
            and type(status.get("observed_ns")) is int
            and "episode_ns" in status
            and (type(status["episode_ns"]) is int if status["blocked"] else status["episode_ns"] is None)
        ):
            result = {key: status[key] for key in result}
    return result


def record_status(home: Path, status: dict) -> bool:
    directory = home / "skill-data/supervisor-auth"
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (directory / "capture.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if journal_status(home)["observed_ns"] > status["observed_ns"]:
                return False
            with os.fdopen(os.open(directory / "evidence.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600), "a") as output:
                output.write("\n" + json.dumps({"auth_observation": status}) + "\n")
                output.flush()
                os.fsync(output.fileno())
        return True
    except OSError:
        return False


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


def recorded_processes(home: Path) -> list[str]:
    deadline = time.monotonic() + 1
    with sqlite3.connect((home / "logs_2.sqlite").as_uri() + "?mode=ro", uri=True, timeout=0.1) as connection:
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        return [
            row[0]
            for row in connection.execute("SELECT DISTINCT process_uuid FROM logs")
            if isinstance(row[0], str) and PROCESS.fullmatch(row[0])
        ]


def capture(home: Path, current: list[dict]) -> list[dict]:
    directory = home / "skill-data/supervisor-auth"
    journal = directory / "evidence.jsonl"
    previous = read_journal(journal)
    merged = merge_events(previous, current)
    if not any(event["event"] in FAILURES or event["event"] == "issuer_rejection" for event in merged):
        return merged
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with (directory / "capture.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            previous = read_journal(journal)
            known = {event_key(event) for event in previous}
            added = [event for event in current if event_key(event) not in known]
            if added:
                try:
                    processes = (
                        process_inventory()
                        if any(event["event"] in FAILURES or event["event"] == "issuer_rejection" for event in added)
                        else []
                    )
                    process_error = None
                except (OSError, subprocess.SubprocessError):
                    processes, process_error = [], "process_inventory_unavailable"
                try:
                    home_processes = recorded_processes(home)
                    home_process_error = None
                except (OSError, sqlite3.Error):
                    home_processes, home_process_error = [], "home_process_inventory_unavailable"
                batch = {
                    "captured_ns": time.time_ns(),
                    "events": added,
                    "processes": processes,
                    "process_error": process_error,
                    "home_log_processes": home_processes,
                    "home_process_error": home_process_error,
                }
                with os.fdopen(os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600), "a") as output:
                    output.write("\n" + json.dumps(batch) + "\n")
                    output.flush()
                    os.fsync(output.fileno())
            return merge_events(previous, current)
    except BlockingIOError:
        return merged


def daemon_identity(home: Path, expected_pid: int | None = None) -> DaemonIdentity:
    for name in ("daemon.pid", "app-server.pid"):
        path = home / "app-server-daemon" / name
        if not path.exists() or not stat.S_ISREG(path.lstat().st_mode):
            continue
        record = json.loads(path.read_text())
        if not isinstance(record, dict):
            continue
        pid = record.get("pid")
        identity = record.get("processIdentity", {})
        started = identity.get("startSeconds") if isinstance(identity, dict) else None
        if type(pid) is not int or pid <= 0 or type(started) is not int or expected_pid not in (None, pid):
            continue
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=1,
            check=True,
            env={"PATH": os.defpath, "LC_ALL": "C", "TZ": "UTC"},
        )
        observed = datetime.strptime(result.stdout.strip(), "%a %b %d %H:%M:%S %Y").replace(tzinfo=UTC)
        if int(observed.timestamp()) != started:
            continue
        stamp = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return DaemonIdentity(pid, started * 1_000_000_000, stamp)
    raise ValueError("daemon identity unavailable")


def peer_pid(stream: socket.socket) -> int:
    if sys.platform == "darwin":
        return stream.getsockopt(0, 0x002)
    if sys.platform == "linux":
        return struct.unpack("3i", stream.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
    raise ValueError("daemon peer identity unavailable")


def native_status(home: Path) -> AuthStatus:
    from websockets.exceptions import WebSocketException

    try:
        return read_native_status(home)
    except WebSocketException:
        raise ValueError("native auth read unavailable") from None


def read_native_status(home: Path) -> AuthStatus:
    from websockets.sync.client import unix_connect

    deadline = time.monotonic() + 2
    endpoint = home / "app-server-control/app-server-control.sock"
    with unix_connect(str(endpoint), open_timeout=0.5, close_timeout=0.1, max_size=64 * 1024) as connection:
        pid = peer_pid(connection.socket)
        identity = daemon_identity(home, pid)

        def request(ident: int, rpc_method: str, params: dict) -> dict:
            connection.send(json.dumps({"id": ident, "method": rpc_method, "params": params}))
            while True:
                message = json.loads(connection.recv(timeout=max(0.001, deadline - time.monotonic())))
                if not isinstance(message, dict):
                    raise ValueError("native auth read unavailable")
                if message.get("id") == ident:
                    if "error" in message or not isinstance(message.get("result"), dict):
                        raise ValueError("native auth read unavailable")
                    return message["result"]

        request(1, "initialize", {"clientInfo": {"name": "supervisor_auth_observer", "version": "1"}})
        connection.send(json.dumps({"method": "initialized", "params": {}}))
        requested = time.time_ns()
        result = request(2, "getAuthStatus", {"includeToken": False, "refreshToken": False})
        if "authToken" not in result or result["authToken"] is not None or type(result.get("requiresOpenaiAuth")) is not bool:
            raise ValueError("native token-free auth status unavailable")
        method = result.get("authMethod")
        if method is not None and not isinstance(method, str):
            raise ValueError("native auth mode unavailable")
        account = request(3, "account/read", {"refreshToken": False})
        if type(account.get("requiresOpenaiAuth")) is not bool or "account" not in account:
            raise ValueError("native account state unavailable")
        if account["requiresOpenaiAuth"] != result["requiresOpenaiAuth"]:
            raise ValueError("native provider changed")
        value = account["account"]
        if value is not None and (not isinstance(value, dict) or not isinstance(value.get("type"), str)):
            raise ValueError("native account state unavailable")
        if daemon_identity(home, peer_pid(connection.socket)) != identity:
            raise ValueError("daemon identity changed")
        return AuthStatus(identity, requested, method, result["requiresOpenaiAuth"], value is not None)


def current_events(events: list[dict], identity: DaemonIdentity) -> list[dict]:
    return [
        event for event in events if event["process"].startswith(f"pid:{identity.pid}:") and event["ts_ns"] >= identity.started_ns
    ]


def failure_episode(events: list[dict]) -> int | None:
    first = None
    for event in events:
        if event["event"] in RESETS:
            first = None
        elif event["event"] in FAILURES:
            first = first if first is not None else event["ts_ns"]
    return first


def observe(home: Path) -> dict:
    home = home.expanduser().resolve()
    previous = {"blocked": False, "episode_ns": None, "observed_ns": 0}
    try:
        previous = journal_status(home)
        events = capture(home, read_log(home))
        identity = daemon_identity(home)
        status = native_status(home)
        if status.identity != identity:
            return {**previous, "diagnostic": "auth_daemon_changed"}
        events = capture(home, read_log(home))
        if status.account_present or not status.requires_auth:
            if previous["blocked"] or any(event["ts_ns"] >= previous["observed_ns"] and event["event"] in FAILURES for event in events):
                if not record_status(home, {"blocked": False, "episode_ns": None, "observed_ns": status.requested_ns}):
                    return {"blocked": False, "diagnostic": "auth_observation_not_saved"}
            return {"blocked": False}
        if status.auth_method != "chatgpt":
            return {**previous, "diagnostic": "auth_mode_unknown"}
        deadline = time.monotonic() + LOG_DELIVERY_SECONDS
        while True:
            events = capture(home, read_log(home))
            selected = [event for event in current_events(events, identity) if previous["blocked"] or event["ts_ns"] >= previous["observed_ns"]]
            episode = failure_episode(selected)
            if episode is not None:
                result = {"blocked": True, "episode_ns": previous["episode_ns"] if previous["blocked"] else episode}
                if not previous["blocked"]:
                    if not record_status(home, {**result, "observed_ns": status.requested_ns}):
                        result["diagnostic"] = "auth_observation_not_saved"
                return result
            if time.monotonic() >= deadline:
                return {**previous, "diagnostic": "auth_log_delivery_unknown"}
            time.sleep(0.02)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError, ImportError, TimeoutError):
        return {**previous, "diagnostic": "auth_evidence_unavailable"}


def require_usable(home: Path) -> None:
    if observe(home).get("blocked"):
        raise ValueError(
            "Codex account needs sign-in; launches on this account are paused. Sign in to this account in Codex, then retry."
        )


def home_id(home: Path) -> str:
    return hashlib.sha256(str(home.expanduser().resolve()).encode()).hexdigest()


def notices(homes: dict[str, Path]) -> list[dict]:
    result = []
    for name, home in homes.items():
        health = observe(home)
        if health.get("blocked"):
            result.append(
                {
                    "auth_failure": {"home_id": home_id(home), "episode_ns": health["episode_ns"]},
                    "message": f"Codex {name} needs you to sign in again. Launches on this account are paused.",
                    "clean": None,
                }
            )
        elif health.get("diagnostic"):
            result.append(
                {"auth_diagnostic": {"home_id": home_id(home), "reason": health["diagnostic"]}, "account": name, "clean": None}
            )
    return result
