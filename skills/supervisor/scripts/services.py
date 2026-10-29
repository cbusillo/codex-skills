#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Render Supervisor launchd jobs or run one persistent bounded-command service.

Rendering never installs or starts a service. A nonzero child exit stops the
service for reconciliation rather than replaying a possibly uncertain write.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import threading
import time

SERVICES = ("enqueue", "drive", "foreign", "throughput")
IDLE_SERVICES = ("idle-codex", "idle-claude")
LABEL_PREFIX = "com.codex-skills.supervisor"


def load_config(path):
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or set(config) != {"uv", "services"}:
        raise ValueError("configuration requires uv and services")
    absolute_path(config["uv"])
    if (not isinstance(config["services"], dict) or not set(SERVICES) <= set(config["services"])
            or set(config["services"]) - set(SERVICES + IDLE_SERVICES)):
        raise ValueError("configure enqueue, drive, foreign and throughput; idle-codex and idle-claude are optional")
    for value in config["services"].values():
        if not isinstance(value, dict) or set(value) != {"argv", "cwd", "restart_seconds"}:
            raise ValueError("each service requires argv, cwd and restart_seconds")
        argv = value["argv"]
        if not isinstance(argv, list) or not argv or any(
            not isinstance(arg, str) or not arg or "\0" in arg for arg in argv
        ):
            raise ValueError("argv must be a nonempty argument array")
        absolute_path(argv[0])
        absolute_path(value["cwd"])
        delay = value["restart_seconds"]
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay <= 0:
            raise ValueError("restart_seconds must be finite and positive")
    return config


def absolute_path(value):
    if not isinstance(value, str) or "\0" in value or not Path(value).is_absolute():
        raise ValueError("paths must be absolute")
    return Path(value)


def render(config_path, catalog, output):
    config_path = config_path.resolve()
    config = load_config(config_path)
    runner = catalog.resolve() / "skills/supervisor/scripts/services.py"
    if not runner.is_file():
        raise ValueError("catalog does not contain the service runner")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_dir = output / "logs"
    log_dir.mkdir(exist_ok=True, mode=0o700)
    for name in config["services"]:
        label = f"{LABEL_PREFIX}.{name}"
        job = {
            "Label": label,
            "ProgramArguments": [config["uv"], "run", "--no-project", "--no-cache", str(runner),
                                 "run", "--config", str(config_path), "--service", name],
            "WorkingDirectory": str(catalog.resolve()),
            "EnvironmentVariables": {"PATH": f"{Path(config['uv']).parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
            "RunAtLoad": True,
            "KeepAlive": False,
            "ExitTimeOut": 30,
            "StandardOutPath": str(log_dir / f"{name}.jsonl"),
            "StandardErrorPath": str(log_dir / f"{name}.stderr.log"),
        }
        path = output / f"{label}.plist"
        with path.open("wb") as stream:
            plistlib.dump(job, stream)
        path.chmod(0o600)
    return output


def emit(event, **fields):
    print(json.dumps({"event": event, "time": time.time(), **fields}), flush=True)


@contextmanager
def service_lock(config_path, name):
    path = config_path.with_name(f"{config_path.name}.{name}.lock")
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("another process owns this service") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def stop_child(child):
    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def reconciliation_marker(config_path, name):
    return config_path.with_name(f"{config_path.name}.{name}.reconcile")


def supervise(settings, stopped, marker, uv_executable):
    """Keep successful bounded jobs running; preserve failures for the Supervisor."""
    while not stopped.is_set():
        # Survives a runner crash, forced stop or host restart mid-command.
        marker.touch(mode=0o600)
        # Only the long-running uv child needs a temporary cache. Its short
        # helper calls keep normal caching instead of inheriting UV_NO_CACHE.
        child_argv = settings["argv"]
        if child_argv[0] == uv_executable and "--no-cache" not in child_argv[1:]:
            child_argv = [uv_executable, "--no-cache", *child_argv[1:]]
        child = subprocess.Popen(child_argv, cwd=settings["cwd"], start_new_session=True)
        emit("started", pid=child.pid)
        try:
            while child.poll() is None:
                if stopped.wait(0.25):
                    stop_child(child)
                    emit("stopped", exit_code=child.returncode)
                    if child.returncode:
                        emit("reconciliation_required", exit_code=child.returncode)
                        return 1
                    marker.unlink()
                    return 0
            code = child.returncode
        finally:
            stop_child(child)
        emit("completed", exit_code=code)
        if code:
            emit("reconciliation_required", exit_code=code)
            return 1
        marker.unlink()
        stopped.wait(settings["restart_seconds"])
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("render", help="write plists only; never install or start")
    prepare.add_argument("--config", type=Path, required=True)
    prepare.add_argument("--catalog", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("run", help="launchd entry point")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--service", choices=SERVICES + IDLE_SERVICES, required=True)
    clear = sub.add_parser("clear", help="clear a stop marker after reconciliation")
    clear.add_argument("--config", type=Path, required=True)
    clear.add_argument("--service", choices=SERVICES + IDLE_SERVICES, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            emit("rendered", directory=str(render(args.config, args.catalog, args.output)))
            return 0
        config = load_config(args.config)
        settings = config["services"].get(args.service)
        if settings is None:
            raise ValueError("requested service is not configured")
        config_path = args.config.resolve()
        marker = reconciliation_marker(config_path, args.service)
        if args.command == "clear":
            with service_lock(config_path, args.service):
                marker.unlink(missing_ok=True)
                emit("reconciled", service=args.service)
            return 0
        stopped = threading.Event()
        previous = {}
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda _sig, _frame: stopped.set())
        try:
            with service_lock(config_path, args.service):
                if marker.exists():
                    emit("reconciliation_required", reason="previous command did not finish cleanly")
                    return 1
                return supervise(settings, stopped, marker, config["uv"])
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        # Do not emit child arguments, configuration or provider error bodies.
        emit("reconciliation_required", reason="service configuration or launch failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
