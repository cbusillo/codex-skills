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
from contextlib import ExitStack, contextmanager
import fcntl
import json
import importlib
import math
import os
from pathlib import Path
from collections.abc import Iterator, Mapping
from typing import Any
import plistlib
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid


SERVICES = ("enqueue", "drive", "foreign", "throughput")
IDLE_SERVICES = ("idle-codex", "idle-claude")
OPTIONAL_SERVICES = IDLE_SERVICES + ("catalog",)
LABEL_PREFIX = "com.codex-skills.supervisor"

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))


def load_config(path):
    config = json.loads(path.read_text())
    if (not isinstance(config, dict) or not {"uv", "services"} <= set(config)
            or set(config) - {"uv", "services", "supervisor"}):
        raise ValueError("configuration requires uv and services")
    absolute_path(config["uv"])
    if (not isinstance(config["services"], dict) or not config["services"]
            or set(config["services"]) - set(SERVICES + OPTIONAL_SERVICES)):
        raise ValueError("configure at least one supported service")
    for value in config["services"].values():
        if not isinstance(value, dict) or not {"argv", "cwd", "restart_seconds"} <= set(value) <= {
            "argv", "cwd", "restart_seconds", "failure_argv"
        }:
            raise ValueError("each service requires argv, cwd and restart_seconds")
        argv = value["argv"]
        if not isinstance(argv, list) or not argv or any(
            not isinstance(arg, str) or not arg or "\0" in arg for arg in argv
        ):
            raise ValueError("argv must be a nonempty argument array")
        absolute_path(argv[0])
        if "failure_argv" in value:
            failure = value["failure_argv"]
            if not isinstance(failure, list) or not failure or any(not isinstance(arg, str) or not arg for arg in failure):
                raise ValueError("failure_argv must be a nonempty argument array accepting Markdown on stdin")
            absolute_path(failure[0])
        absolute_path(value["cwd"])
        delay = value["restart_seconds"]
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay <= 0:
            raise ValueError("restart_seconds must be finite and positive")
    if "supervisor" in config:
        validate_supervisor(config["supervisor"])
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
            "ExitTimeOut": 180,
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


def validate_supervisor(recipient: Any) -> None:
    from session_liveness import native_uuid

    if (not isinstance(recipient, dict) or recipient.get("role") != "supervisor"
            or recipient.get("harness") not in {"codex", "claude"}
            or not isinstance(recipient.get("session_id"), str)
            or native_uuid(recipient["session_id"]) != recipient["session_id"]
            or "supervisor_owned" in recipient):
        raise ValueError("Supervisor binding requires its actual native identity and explicit role")
    absolute_path(recipient.get("transcript"))
    if recipient["harness"] == "codex":
        absolute_path(recipient.get("codex_home"))
        return
    socket = recipient.get("tmux_socket")
    if (recipient.get("backend") != "tmux" or not isinstance(socket, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", socket)
            or not isinstance(recipient.get("tmux_session_id"), str)
            or not re.fullmatch(rf"tmux:{re.escape(socket)}:[0-9a-f]{{32}}", recipient["tmux_session_id"])
            or type(recipient.get("pid")) is not int or recipient["pid"] <= 0
            or any(not isinstance(recipient.get(key), str) or not recipient[key]
                   for key in ("tty", "window_id", "tab_id"))):
        raise ValueError("Claude Supervisor binding requires its exact managed tmux route")


@contextmanager
def supervisor_lock(config_path: Path) -> Iterator[None]:
    with ExitStack() as stack:
        try:
            stack.enter_context(service_lock(config_path.resolve(), "notifications"))
        except ValueError as error:
            raise ValueError("notification or binding in progress; retry bind after it finishes") from error
        yield


def supervisor_recipient(config_path: Path) -> dict:
    recipient = load_config(config_path).get("supervisor")
    if not isinstance(recipient, dict):
        raise ValueError("bind the current Supervisor before starting idle notifications")
    return dict(recipient)


def claude_supervisor_route(tmux_socket: str) -> dict:
    from session_liveness import harness_arguments, harness_family, inventory
    from tmux_terminal import Terminal, belongs_to, process_parents

    parents, processes = process_parents(), inventory()
    pid, visited = os.getppid(), set()
    source: tuple[int, str, str] | None = None
    while pid in parents and pid not in visited:
        visited.add(pid)
        matches = [row for row in processes if row[0] == pid]
        if len(matches) > 1:
            raise ValueError("caller process identity is ambiguous")
        if matches:
            arguments = harness_arguments(matches[0][2])
            if arguments and harness_family(arguments[0]) == "claude":
                source = matches[0]
                break
        pid = parents[pid]
    if source is None:
        raise ValueError("current Claude harness ancestor is unavailable")
    panes = [row for row in Terminal(tmux_socket).list()
             if not row["dead"] and isinstance(row.get("session_id"), str)
             and re.fullmatch(rf"tmux:{re.escape(tmux_socket)}:[0-9a-f]{{32}}", row["session_id"])
             and row["tty"].removeprefix("/dev/") == source[1].removeprefix("/dev/")
             and belongs_to(source[0], row["pid"], parents)]
    if len(panes) != 1:
        raise ValueError("current Claude Supervisor needs exactly one live managed tmux pane")
    pane = panes[0]
    return {**{key: pane[key] for key in ("backend", "tmux_socket", "tty", "window_id", "tab_id")},
            "pid": source[0], "tmux_session_id": pane["session_id"]}


def bind_supervisor(config_path: Path, transcript: Path | None = None, tmux_socket: str = "supervisor",
                    environ: Mapping[str, str] = os.environ) -> dict:
    github_agent = importlib.import_module("github_agent")
    from session_record import session_status

    agent = github_agent.running_agent(environ=None if environ is os.environ else environ)
    native = github_agent.native_sessions(agent, environ=environ)
    identity = (native.get("CODEX_THREAD_ID") or native.get("CODEX_SESSION_ID")
                if agent == "codex" else native.get("CLAUDE_CODE_SESSION_ID"))
    if not identity:
        raise ValueError("current harness native identity is unavailable; bind from the Supervisor session")
    from session_liveness import native_uuid
    if native_uuid(identity) != identity:
        raise ValueError("current Supervisor native identity must be canonical")
    if agent == "codex" and native.get("CODEX_SESSION_ID", identity) != identity:
        raise ValueError("a Codex child cannot bind itself as the root Supervisor")
    home_root = Path(environ.get("HOME", str(Path.home())))
    variable, fallback, directory = (("CODEX_HOME", ".codex", "sessions") if agent == "codex"
                                     else ("CLAUDE_CONFIG_DIR", ".claude", "projects"))
    home = Path(environ.get(variable) or home_root / fallback).expanduser().resolve(strict=True)
    if transcript is None:
        paths = {path.resolve() for path in (home / directory).rglob(f"*{identity}*.jsonl")}
        if len(paths) != 1:
            raise ValueError("current Supervisor transcript is not unique; supply its exact --transcript path")
        transcript = paths.pop()
    recipient: dict = {"role": "supervisor", "harness": agent, "session_id": identity,
                       "transcript": str(transcript.expanduser().resolve(strict=True))}
    if agent == "codex":
        from codex_session_proxy import source_endpoint
        source_endpoint(identity, home, transcript=Path(recipient["transcript"]))
        recipient["codex_home"] = str(home)
    else:
        if not Path(recipient["transcript"]).is_relative_to((home / "projects").resolve()):
            raise ValueError("current Claude transcript must be in its account's projects directory")
        recipient.update(claude_supervisor_route(tmux_socket))
    session_status(recipient)
    validate_supervisor(recipient)
    config_path = config_path.resolve()
    with supervisor_lock(config_path):
        config = load_config(config_path)
        config["supervisor"] = recipient
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=config_path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                os.fchmod(stream.fileno(), 0o600)
                json.dump(config, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(config_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return recipient


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


def notify_failure(settings: dict, marker: Path, name: str) -> None:
    if not settings.get("failure_argv"):
        return
    from check_changes import save_state

    notice = marker.with_suffix(".notice.json")
    if notice.exists():
        return
    save_state(notice, {"complete": False})
    message = f"Supervisor service {name} FAILED and is stopped for reconciliation. Inspect its retained logs and verification comments.\n"
    sent = subprocess.run(settings["failure_argv"], input=message, cwd=settings["cwd"], capture_output=True, text=True, timeout=120)
    save_state(notice, {"complete": sent.returncode == 0, "exit_code": sent.returncode,
                        "stdout": sent.stdout, "stderr": sent.stderr})
    if sent.returncode:
        raise ValueError("failure notification incomplete; reconcile the retained notice before clearing")


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
    run.add_argument("--service", choices=SERVICES + OPTIONAL_SERVICES, required=True)
    clear = sub.add_parser("clear", help="clear a stop marker after reconciliation")
    clear.add_argument("--config", type=Path, required=True)
    clear.add_argument("--service", choices=SERVICES + OPTIONAL_SERVICES, required=True)
    bind = sub.add_parser("bind", help="bind notifications to this actual Supervisor session")
    bind.add_argument("--config", type=Path, required=True)
    bind.add_argument("--transcript", type=Path, help="exact source path when own-account discovery is ambiguous")
    bind.add_argument("--tmux-socket", default="supervisor")
    args = parser.parse_args(argv)
    try:
        if args.command == "bind":
            try:
                recipient = bind_supervisor(args.config, args.transcript, args.tmux_socket)
            except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
                emit("binding_unavailable", reason=str(error))
                return 1
            emit("supervisor_bound", harness=recipient["harness"], session_id=recipient["session_id"])
            return 0
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
                notice = marker.with_suffix(".notice.json")
                if notice.exists():
                    notice.rename(notice.with_name(f"{notice.stem}.{uuid.uuid4().hex}.json"))
                marker.unlink(missing_ok=True)
                emit("reconciled", service=args.service)
            return 0
        stopped = threading.Event()
        previous = {}
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda _sig, _frame: stopped.set())
        try:
            with service_lock(config_path, args.service):
                try:
                    if marker.exists():
                        emit("reconciliation_required", reason="previous command did not finish cleanly")
                        return 1
                    return supervise(settings, stopped, marker, config["uv"])
                finally:
                    if marker.exists():
                        notify_failure(settings, marker, args.service)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        # Do not emit child arguments, configuration or provider error bodies.
        emit("reconciliation_required", reason="service configuration or launch failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
