#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5", "websockets==17.3"]
# ///

import argparse
from contextlib import ExitStack
import json
import math
import shlex
import signal
import subprocess
import time
import threading
from pathlib import Path
from typing import Any

from session_record import load_ledger, session_status
from capacity_retry import locked_state, resume, retry_status
from finished_map import closeout_request
from foreign_watch import watch_lock
import finished_map
import review_notifications
import services
import account_choice
import codex_auth_health


def poll(ledger: Path, seen: dict, idle_seconds: float, now: float,
         capacity_state: dict | None = None, state_path: Path | None = None,
         resume_capacity: bool = False, harness: str = "codex",
         account_homes: dict[str, Path] | None = None) -> list[dict]:
    if resume_capacity and state_path is None:
        raise ValueError("capacity resume requires a persistent state path")
    notices = []
    skipped = []
    entries = load_ledger(ledger, harness=harness, skipped=skipped)
    homes = {name: home.expanduser().resolve() for name, home in (account_homes or {}).items()} if harness == "codex" else {}
    for entry in entries:
        if harness == "codex" and entry.get("codex_home"):
            home = Path(entry["codex_home"]).expanduser().resolve()
            if home not in homes.values():
                name = home.name if home.name not in homes else f"{home.name} ({codex_auth_health.home_id(home)[:8]})"
                homes[name] = home
    for notice in codex_auth_health.notices(homes):
        key = json.dumps(notice, sort_keys=True)
        if key not in seen:
            notices.append(notice)
            seen[key] = True
    for diagnostic in skipped:
        key = ("skipped", diagnostic["ledger_index"])
        value = json.dumps(diagnostic, sort_keys=True)
        if seen.get(key) != value:
            notices.append(diagnostic)
            seen[key] = value
    for entry in entries:
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
        closeout = closeout_request(entry, status)
        response = ((status["timestamp"], status["last_text"])
                    if closeout is not None and status["timestamp"] else
                    (status["mtime"], status["size"]))
        key = (response, json.dumps(retry, sort_keys=True),
               json.dumps(closeout, sort_keys=True))
        if not status["at_turn_end"]:
            seen.pop(entry["session_id"], None)
        if (
            status["at_turn_end"]
            and (closeout is not None or now - status["mtime"] >= idle_seconds)
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
                    "closeout_request": closeout,
                }
            )
    return notices


PENDING_NOTIFICATION = "pending-notification:"


def load_delivered(path: Path) -> set[str]:
    values = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("delivered state must be an array of notice identities")
    return set(values)


def save_delivered(path: Path, delivered: set[str]) -> None:
    pending = path.with_name(f"{path.name}.tmp")
    pending.write_text(json.dumps(sorted(delivered)), encoding="utf-8")
    pending.replace(path)


def reconcile_notification(args: Any) -> int:
    delivered = load_delivered(args.delivered_state)
    pending = [value for value in delivered if value.startswith(PENDING_NOTIFICATION)]
    if len(pending) != 1:
        raise ValueError("reconciliation needs exactly one retained notification attempt")
    attempt = json.loads(pending[0].removeprefix(PENDING_NOTIFICATION))
    if (not isinstance(attempt, dict) or not isinstance(attempt.get("notice"), dict)
            or not isinstance(attempt.get("recipient"), dict)):
        raise ValueError("retained notification identity is malformed; preserve it for reconciliation")
    if not args.delivery_outcome:
        print(json.dumps({"notification": attempt, "reconciliation_required": True}), flush=True)
        return 0
    if not args.evidence or not args.evidence.strip():
        raise ValueError("delivery outcome needs evidence from the exact recipient's readback")
    delivered.remove(pending[0])
    if args.delivery_outcome == "delivered":
        delivered.add(json.dumps(attempt["notice"], sort_keys=True))
    delivered.add("reconciled-notification:" + json.dumps({**attempt, "outcome": args.delivery_outcome,
                  "evidence": args.evidence}, sort_keys=True))
    save_delivered(args.delivered_state, delivered)
    print(json.dumps({"reconciled": True, "outcome": args.delivery_outcome}), flush=True)
    return 0


def notification_recipient(args: Any) -> dict:
    recipient = services.supervisor_recipient(args.notify_config)
    if any(entry["session_id"] == recipient["session_id"] for entry in load_ledger(args.ledger)):
        raise ValueError("the Supervisor recipient must not be in the watched worker ledger")
    return recipient


def defer_notice(notice: dict, seen: dict) -> None:
    if notice.get("session_id"):
        seen.pop(notice["session_id"], None)
    else:
        seen.pop(json.dumps(notice, sort_keys=True), None)


def notify_requests(args: Any, notices: list[dict], delivered: set[str], seen: dict,
                    stopped: threading.Event | None = None) -> None:
    for notice in notices:
        if stopped is not None and stopped.is_set():
            return
        request = notice.get("closeout_request") or notice.get("auth_failure")
        if not isinstance(request, dict) or not request:
            continue
        identity = json.dumps(request, sort_keys=True)
        if identity in delivered:
            continue
        message = json.dumps(notice)
        if notice.get("closeout_request"):
            candidate = finished_map.candidates(args.ledger, request["session_id"])[0]
            if "error" in candidate or candidate.get("closeout_request") != request:
                defer_notice(notice, seen)
                notice["closeout_request"] = None
                if "error" in candidate:
                    notice["error"] = candidate["error"]
                continue
            command = shlex.join(["uv", "run", str(Path(__file__).with_name("finished_map.py").resolve()),
                                  "--ledger", str(args.ledger.resolve()), "--session-id", request["session_id"]])
            message = ("Closeout request: " + json.dumps({"closeout_request": candidate["closeout_request"],
                       "needs_verification": candidate["needs_verification"]}) + "\nRead the exact candidate with: " + command
                       + "\nImmediately follow the existing finished-session verification. This notice is not exit authority.")
        with ExitStack() as stack:
            try:
                stack.enter_context(services.supervisor_lock(args.notify_config))
            except ValueError:
                defer_notice(notice, seen)
                continue
            recipient = notification_recipient(args)
            pending = PENDING_NOTIFICATION + json.dumps({"notice": request, "recipient": recipient}, sort_keys=True)
            save_delivered(args.delivered_state, delivered | {pending})
            try:
                review_notifications.send(recipient, message, supervisor=True)
            except review_notifications.NotSent as error:
                save_delivered(args.delivered_state, delivered)
                if error.needs_attention:
                    raise ValueError(str(error)) from error
                defer_notice(notice, seen)
                continue
            delivered.add(identity)
            save_delivered(args.delivered_state, delivered)


def watch(args, state: dict | None, stopped: threading.Event | None = None) -> int:
    deadline = time.monotonic() + args.deadline_seconds
    seen = {}
    delivered = load_delivered(args.delivered_state) if args.delivered_state else set()
    if any(value.startswith(PENDING_NOTIFICATION) for value in delivered):
        print(json.dumps({"error": "notification outcome requires reconciliation; inspect the exact recipient before any retry"}), flush=True)
        return 1
    homes = {}
    if args.harness == "codex":
        try:
            homes = {account["name"]: Path(account["env"]["CODEX_HOME"])
                     for account in account_choice.load_config()["accounts"] if account["provider"] == "openai"}
        except ValueError:
            homes = {}
        default = Path.home() / ".codex"
        if (default / "logs_2.sqlite").is_file() and default.resolve() not in {home.expanduser().resolve() for home in homes.values()}:
            homes["default home"] = default
    while stopped is None or not stopped.is_set():
        try:
            notices = poll(args.ledger, seen, args.idle_seconds, time.time(),
                           state, args.capacity_state, args.resume_capacity, args.harness, homes)
            if getattr(args, "persistent", False):
                notify_requests(args, notices, delivered, seen, stopped)
            for notice in notices:
                auth = notice.get("auth_failure") or notice.get("auth_diagnostic")
                if not auth or json.dumps(auth, sort_keys=True) not in delivered:
                    print(json.dumps(notice), flush=True)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
            print(json.dumps({"error": str(error)}), flush=True)
            return 1
        diagnostics = {json.dumps(notice["auth_diagnostic"], sort_keys=True)
                       for notice in notices if notice.get("auth_diagnostic")}
        if args.delivered_state and diagnostics - delivered:
            delivered.update(diagnostics)
            save_delivered(args.delivered_state, delivered)
        requests = {json.dumps(notice["closeout_request"], sort_keys=True)
                    for notice in notices if notice.get("closeout_request")}
        requests.update(json.dumps(notice["auth_failure"], sort_keys=True)
                        for notice in notices if notice.get("auth_failure"))
        if not getattr(args, "persistent", False) and args.delivered_state and requests - delivered:
            save_delivered(args.delivered_state, delivered | requests)
            return 0
        if args.once or time.monotonic() >= deadline:
            return 0
        delay = min(args.interval, max(0, deadline - time.monotonic()))
        if stopped is not None:
            stopped.wait(delay)
        else:
            time.sleep(delay)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Emit turn-end notices for one harness, exiting on an undelivered closeout request.")
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--harness", choices=("codex", "claude"), default="codex")
    parser.add_argument("--idle-seconds", type=float, default=90)
    parser.add_argument("--interval", type=float, default=45)
    parser.add_argument("--deadline-seconds", type=float, default=1800)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--persistent", action="store_true", help="deliver notices immediately and keep watching within the deadline")
    parser.add_argument("--notify-config", type=Path, help="existing service configuration with the current Supervisor binding")
    parser.add_argument("--reconcile-notification", action="store_true", help="inspect the retained attempt; record an outcome only with readback evidence")
    parser.add_argument("--delivery-outcome", choices=("delivered", "not-sent"))
    parser.add_argument("--evidence", help="exact-recipient readback evidence for an explicit reconciliation")
    parser.add_argument("--delivered-state", type=Path,
                        help="closeout requests already delivered; the watch exits on any other")
    parser.add_argument("--capacity-state", type=Path,
                        help="persistent retry budget shared with checks and the digest")
    parser.add_argument("--resume-capacity", action="store_true",
                        help="opt in to bounded exact-thread continue sends")
    args = parser.parse_args()
    if any(not math.isfinite(value) or value <= 0 for value in (args.idle_seconds, args.interval, args.deadline_seconds)):
        parser.error("timings must be finite and positive")
    if args.persistent and (not args.notify_config or not args.delivered_state or args.once):
        parser.error("--persistent needs --notify-config and --delivered-state, without --once")
    if not args.persistent and args.notify_config:
        parser.error("notification targets require --persistent")
    if (args.delivery_outcome or args.evidence) and not args.reconcile_notification:
        parser.error("delivery outcomes and evidence require --reconcile-notification")
    if args.reconcile_notification and (not args.delivered_state or args.persistent or args.resume_capacity):
        parser.error("reconciliation needs --delivered-state, without --persistent or --resume-capacity")
    if not args.once and not args.delivered_state:
        parser.error("a continuous watch requires --delivered-state PATH, one private file per harness")
    if args.resume_capacity and not args.capacity_state:
        parser.error("--resume-capacity requires --capacity-state")
    stopped = threading.Event() if args.persistent else None
    previous = {}
    try:
        if stopped is not None:
            stop = stopped.set
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, lambda _sig, _frame: stop())
        with ExitStack() as stack:
            if args.delivered_state:
                stack.enter_context(watch_lock(args.delivered_state))
            if args.reconcile_notification:
                return reconcile_notification(args)
            if args.persistent:
                notification_recipient(args)
            state = stack.enter_context(locked_state(args.capacity_state)) if args.capacity_state else None
            return watch(args, state, stopped)
    except (OSError, ValueError, TypeError) as error:
        print(json.dumps({"error": str(error)}), flush=True)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
