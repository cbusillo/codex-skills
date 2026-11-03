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
import shutil
import subprocess
import sys
import tempfile
import threading
import time


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
            "argv", "cwd", "restart_seconds"
        }:
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
    if "supervisor" in config:
        validate_supervisor(config["supervisor"])
    return config


def absolute_path(value):
    if not isinstance(value, str) or "\0" in value or not Path(value).is_absolute():
        raise ValueError("paths must be absolute")
    return Path(value)


def bind_launched_supervisor(config_path: Path, entry: dict) -> dict:
    return store_supervisor(config_path, verify_managed_supervisor(entry))


def verify_managed_supervisor(entry: dict) -> dict:
    from session_record import session_status
    from session_liveness import process_started_at

    if (entry.get("supervisor_owned") is not True and entry.get("role") != "supervisor") or entry.get("backend") != "tmux":
        raise ValueError("Supervisor startup requires a registered managed launch")
    fields = ("harness", "session_id", "transcript", "backend", "tmux_socket", "tmux_session_id",
              "pid", "tty", "window_id", "tab_id")
    recipient = {key: entry[key] for key in fields}
    recipient["role"] = "supervisor"
    started_at = process_started_at(entry["pid"]).isoformat()
    if entry.get("launch_started_at", started_at) != started_at:
        raise ValueError("Supervisor launch process generation changed")
    recipient["launch_started_at"] = started_at
    if entry["harness"] == "codex":
        from codex_session_proxy import source_endpoint
        from github_plan_closure import guarded_launch_endpoint
        from session_liveness import native_arguments
        recipient["codex_home"] = entry["codex_home"]
        endpoint = source_endpoint(entry["session_id"], Path(entry["codex_home"]), transcript=Path(entry["transcript"]))
        if guarded_launch_endpoint(entry["pid"], native_arguments(entry["pid"])) != str(endpoint):
            raise ValueError("Supervisor managed Codex generation changed")
    else:
        route = claude_supervisor_route(entry["tmux_socket"], source_pid=entry["pid"])
        for key, value in route.items():
            actual = recipient[key].removeprefix("/dev/") if key == "tty" else recipient[key]
            expected = value.removeprefix("/dev/") if key == "tty" else value
            if actual != expected:
                raise ValueError("Supervisor launch terminal identity changed")
    validate_supervisor(recipient)
    session_status(recipient)
    return recipient


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


def claude_supervisor_route(tmux_socket: str, *, source_pid: int | None = None) -> dict:
    from session_liveness import harness_arguments, harness_family, inventory
    from tmux_terminal import Terminal, belongs_to, process_parents

    parents, processes = process_parents(), inventory()
    pid, visited = source_pid if source_pid is not None else os.getppid(), set()
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
        if source_pid is not None:
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
    return store_supervisor(config_path, recipient)


def store_supervisor(config_path: Path, recipient: dict) -> dict:
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


def host_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=20)


def checked_host_command(argv: list[str]) -> None:
    result = host_command(argv)
    if result.returncode:
        raise ValueError(f"service manager failed: {result.stderr.strip()}")


def launchd_status(target: str) -> subprocess.CompletedProcess[str]:
    result = host_command(["launchctl", "print", target])
    if result.returncode and "Could not find service" not in result.stderr:
        raise ValueError(f"service status unavailable: {result.stderr.strip()}")
    return result


def systemd_value(value: str) -> str:
    if any(character in value for character in "\r\n\0"):
        raise ValueError("service paths and arguments must be single lines")
    return json.dumps(value.replace("%", "%%"), ensure_ascii=False)


def prepare_service_host(config_path: Path, catalog: Path, output: Path, name: str) -> dict:
    config_path = config_path.resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    label = f"{LABEL_PREFIX}.{name}"
    if sys.platform == "darwin":
        render(config_path, catalog, output)
        staged = output / f"{label}.plist"
        target = f"gui/{os.getuid()}/{label}"
        loaded = launchd_status(target)
        expected = plistlib.loads(staged.read_bytes())["ProgramArguments"]
        if loaded.returncode == 0:
            block = re.search(r"(?ms)^\s*arguments = \{\n(.*?)^\s*\}", loaded.stdout)
            if not block or [line.strip() for line in block[1].splitlines()] != expected:
                raise ValueError("loaded service belongs to another configuration; preserve it")
        installed = Path.home() / "Library/LaunchAgents" / staged.name
        if installed.exists() and plistlib.loads(installed.read_bytes())["ProgramArguments"] != expected:
            raise ValueError("installed service belongs to another configuration; preserve it")
        return {"backend": "launchd", "target": target, "staged": str(staged), "installed": str(installed)}
    if sys.platform != "linux" or not shutil.which("systemctl"):
        raise ValueError("persistent Supervisor services require launchd or a systemd user manager")
    config = load_config(config_path)
    logs = output / "logs"
    logs.mkdir(exist_ok=True, mode=0o700)
    argv = [config["uv"], "run", "--no-project", "--no-cache", str(catalog / "skills/supervisor/scripts/services.py"),
            "run", "--config", str(config_path), "--service", name]
    content = ("[Unit]\nDescription=Managed Supervisor reader\n[Service]\nType=exec\n"
               + "ExecStart=:" + " ".join(map(systemd_value, argv)) + "\n"
               + f"WorkingDirectory={systemd_value(str(catalog))}\n"
               + f"Environment={systemd_value('PATH=' + os.environ.get('PATH', os.defpath))}\n"
               + f"StandardOutput=append:{str(logs / (name + '.jsonl')).replace('%', '%%')}\n"
               + f"StandardError=append:{str(logs / (name + '.stderr.log')).replace('%', '%%')}\n"
               + "Restart=no\nTimeoutStopSec=180\n[Install]\nWantedBy=default.target\n")
    staged = output / f"{label}.service"
    staged.write_text(content)
    installed = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user" / staged.name
    loaded = host_command(["systemctl", "--user", "show", staged.name, "--property=FragmentPath", "--value"])
    if loaded.returncode and not any(text in loaded.stderr for text in ("not found", "could not be found")):
        raise ValueError(f"service status unavailable: {loaded.stderr.strip()}")
    if loaded.returncode == 0 and loaded.stdout.strip():
        if Path(loaded.stdout.strip()).read_text() != content:
            raise ValueError("loaded service belongs to another configuration; preserve it")
    if installed.exists() and installed.read_text() != content:
        raise ValueError("installed service belongs to another configuration; preserve it")
    return {"backend": "systemd", "target": staged.name, "staged": str(staged), "installed": str(installed)}


def service_host_active(host: dict) -> bool:
    if host["backend"] == "launchd":
        result = launchd_status(host["target"])
        return result.returncode == 0 and re.search(r"(?m)^\s*state = running\s*$", result.stdout) is not None
    result = host_command(["systemctl", "--user", "is-active", host["target"]])
    state = result.stdout.strip()
    if state not in {"active", "inactive", "failed", "unknown", "activating", "deactivating", "reloading", "refreshing", "maintenance"}:
        raise ValueError(f"service status unavailable: {result.stderr.strip()}")
    return state in {"active", "reloading", "refreshing"}


def ensure_service_host(host: dict, config_path: Path, name: str) -> dict:
    if service_host_active(host):
        return host
    if reconciliation_marker(config_path, name).exists():
        raise ValueError("service is stopped for reconciliation; inspect its logs, then use services.py clear before retrying")
    installed = Path(host["installed"])
    installed.parent.mkdir(parents=True, exist_ok=True)
    if not installed.exists():
        with installed.open("xb") as stream:
            stream.write(Path(host["staged"]).read_bytes())
        installed.chmod(0o600)
    if host["backend"] == "launchd":
        if launchd_status(host["target"]).returncode:
            checked_host_command(["launchctl", "bootstrap", host["target"].rsplit("/", 1)[0], str(installed)])
        checked_host_command(["launchctl", "kickstart", host["target"]])
    else:
        checked_host_command(["systemctl", "--user", "daemon-reload"])
        checked_host_command(["systemctl", "--user", "enable", "--now", host["target"]])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if service_host_active(host):
            return host
        time.sleep(0.1)
    raise ValueError("service startup is unconfirmed; reconcile its existing manager status and logs")


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
