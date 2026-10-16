#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Render a daily launchd sweep or run it once through an existing sender.

No Discord client or credentials live here. Delivery receives Markdown on stdin
and may use {report} in an argument to read the retained file instead.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import plistlib
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

import services

LABEL = "com.codex-skills.supervisor.stranded"


def load_config(path):
    config = json.loads(path.read_text())
    required = {"uv", "catalog", "state_dir", "scan_args", "delivery_argv", "hour", "minute"}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError("daily configuration fields are missing or unknown")
    for key in ("uv", "catalog", "state_dir"):
        services.absolute_path(config[key])
    for key in ("scan_args", "delivery_argv"):
        args = config[key]
        if not isinstance(args, list) or any(not isinstance(a, str) or not a or "\0" in a for a in args):
            raise ValueError("commands require argument arrays")
    if not config["delivery_argv"]:
        raise ValueError("existing delivery command is required")
    services.absolute_path(config["delivery_argv"][0])
    for key, limit in (("hour", 24), ("minute", 60)):
        if type(config[key]) is not int or not 0 <= config[key] < limit:
            raise ValueError("invalid daily schedule")
    # These options belong to the runner, not the private scope selection.
    if any(a.split("=", 1)[0] in {"--stranded-format", "--stranded-work-only"} for a in config["scan_args"]):
        raise ValueError("the runner selects stranded Markdown output")
    return config


def render(config_path, output):
    config_path = config_path.resolve()
    config = load_config(config_path)
    catalog = Path(config["catalog"])
    runner = catalog / "skills/supervisor/scripts/stranded_service.py"
    if not runner.is_file():
        raise ValueError("catalog does not contain the daily runner")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = Path(config["state_dir"])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    job = {
        "Label": LABEL,
        "ProgramArguments": [config["uv"], "run", "--no-project", "--no-cache", str(runner),
                             "run", "--config", str(config_path)],
        "WorkingDirectory": str(catalog),
        "EnvironmentVariables": {"PATH": f"{Path(config['uv']).parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "StartCalendarInterval": {"Hour": config["hour"], "Minute": config["minute"]},
        "RunAtLoad": False,
        "KeepAlive": False,
        "ExitTimeOut": 30,
        "StandardOutPath": str(state / "runner.jsonl"),
        "StandardErrorPath": str(state / "runner.stderr.log"),
    }
    target = output / f"{LABEL}.plist"
    with target.open("wb") as stream:
        plistlib.dump(job, stream)
    target.chmod(0o600)
    return target


def command(argv, cwd, stdin, stdout, stderr, stopped, timeout):
    child = subprocess.Popen(argv, cwd=cwd, stdin=stdin, stdout=stdout, stderr=stderr,
                             start_new_session=True)
    try:
        deadline = time.monotonic() + timeout
        while child.poll() is None:
            if stopped.wait(0.1) or time.monotonic() >= deadline:
                raise TimeoutError("daily command stopped or timed out")
        return child.returncode
    finally:
        services.stop_child(child)


def run_once(config_path, stopped):
    config = load_config(config_path)
    state = Path(config["state_dir"])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Lock and reservation follow the persistent Supervisor service pattern.
    # A sender can succeed just before the runner dies: never replay blindly.
    with services.service_lock(state / "daily", "stranded"):
        marker = services.reconciliation_marker(state / "daily", "stranded")
        if marker.exists():
            raise ValueError("previous delivery needs reconciliation")
        day = dt.datetime.now().astimezone().date().isoformat()
        receipt = state / f"{day}.delivered.json"
        if receipt.exists():
            services.emit("already_delivered", day=day)
            return 0
        report = state / f"{day}.md"
        catalog = Path(config["catalog"])
        scan = [config["uv"], "run", "--no-project", "--no-cache",
                str(catalog / "skills/direction/scripts/direction_audit.py"),
                *config["scan_args"], "--stranded-work-only", "--stranded-format", "markdown"]
        with report.open("wb") as out, (state / f"{day}.scan.stderr").open("wb") as err:
            scan_code = command(scan, catalog, subprocess.DEVNULL, out, err, stopped, 900)
        services.emit("scanned", exit_code=scan_code, report=str(report))
        if scan_code not in (0, 3) or not report.stat().st_size:
            raise ValueError("scan did not produce a digest")
        reservation = {"day": day, "scan_exit_code": scan_code, "report": str(report)}
        marker.write_text(json.dumps(reservation) + "\n")
        delivery = [a.replace("{report}", str(report)) for a in config["delivery_argv"]]
        with report.open("rb") as source, (state / f"{day}.delivery.stdout").open("wb") as out, \
                (state / f"{day}.delivery.stderr").open("wb") as err:
            code = command(delivery, catalog, source, out, err, stopped, 120)
        if code:
            raise ValueError("delivery failed; reconcile before replay")
        receipt.write_text(json.dumps({**reservation, "delivery_exit_code": code}) + "\n")
        marker.unlink()
        services.emit("delivered", scan_exit_code=scan_code, receipt=str(receipt))
        # launchd need not retry an incomplete scan after successful delivery.
        # Its nonzero status remains in the retained receipt and report.
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("render", help="write a plist only; never install or start")
    prepare.add_argument("--output", type=Path, required=True)
    for name in ("render", "run", "clear"):
        item = prepare if name == "render" else sub.add_parser(name)
        item.add_argument("--config", type=Path, required=True)
        if name == "clear":
            item.add_argument("--delivered-url", help="record a verified Discord message instead of replaying")
    args = parser.parse_args(argv)
    os.umask(0o077)
    stopped = threading.Event()
    previous = {sig: signal.signal(sig, lambda _sig, _frame: stopped.set())
                for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        if args.command == "render":
            services.emit("rendered", path=str(render(args.config, args.output)))
            return 0
        if args.command == "clear":
            config = load_config(args.config)
            state = Path(config["state_dir"])
            with services.service_lock(state / "daily", "stranded"):
                marker = services.reconciliation_marker(state / "daily", "stranded")
                if args.delivered_url:
                    if not re.fullmatch(r"https://discord\.com/channels/(?:@me|[0-9]+)/[0-9]+/[0-9]+", args.delivered_url):
                        raise ValueError("an exact Discord message URL is required")
                    reservation = json.loads(marker.read_text())
                    day = dt.date.fromisoformat(reservation["day"]).isoformat()
                    (state / f"{day}.delivered.json").write_text(json.dumps(
                        {**reservation, "delivery_url": args.delivered_url, "reconciled": True}) + "\n")
                marker.unlink(missing_ok=True)
            services.emit("reconciled")
            return 0
        return run_once(args.config.resolve(), stopped)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        services.emit("reconciliation_required", reason="inspect retained scan and delivery evidence")
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
