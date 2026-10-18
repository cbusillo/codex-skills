# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Reserve one Supervisor GUI worker across accounts and terminal backends."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import time
import uuid

from agent_tools import BUNDLED_MARKETPLACE


def declared_app(brief):
    brief = brief.lstrip()
    if not brief.startswith("GUI task:"):
        return None
    lines = re.findall(r"^GUI task:\s*(.*?)\s*$", brief, re.MULTILINE)
    if not lines:
        return None
    if len(lines) != 1 or not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", lines[0]):
        raise ValueError("GUI task must declare exactly one app bundle ID")
    app = lines[0]
    if app.lower() in {"com.apple.terminal", "com.googlecode.iterm2", "dev.warp.warp-stable"}:
        raise ValueError("GUI work must never target Chris's terminals")
    return app


def require_unlocked():
    """Fail closed on missing, malformed or locked console evidence; never unlock."""
    try:
        result = subprocess.run(["/usr/sbin/ioreg", "-a", "-l", "-n", "Root", "-d", "1"],
                                capture_output=True, timeout=10, check=True)
        flags = []

        def visit(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"IOConsoleLocked", "CGSSessionScreenIsLocked"}:
                        if type(item) is not bool:
                            raise ValueError("invalid console lock evidence")
                        flags.append(item)
                    else:
                        visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(plistlib.loads(result.stdout))
    except (OSError, ValueError, plistlib.InvalidFileException, subprocess.SubprocessError) as error:
        raise ValueError("screen lock state unavailable; stop GUI work") from error
    if not flags or any(flags):
        raise ValueError("screen locked or lock state unknown; stop GUI work; Chris unlocks it")


def state_path():
    # Deliberately independent of CODEX_HOME, CODE_HOME and terminal backend.
    return Path.home() / ".code" / "skill-data" / "supervisor-gui.json"


@contextmanager
def exclusive(path, wait_seconds=0):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as handle:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("another GUI worker is running; wait for its closeout") from None
                time.sleep(0.02)
        yield


def reserve(app):
    require_unlocked()
    path = state_path()
    with exclusive(path):
        if path.exists() or path.is_symlink():
            raise ValueError("GUI worker reserved or outcome unknown; inspect its launch receipt before release")
        data = {"token": uuid.uuid4().hex, "app": app, "phase": "reserved"}
        with path.open("x", encoding="utf-8") as output:
            json.dump(data, output)
        path.chmod(0o600)
    return {**data, "state_path": str(path)}


def matching(path, token):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("token") != token:
        raise ValueError("GUI reservation changed; preserve it")
    return data


def command_path(path, token):
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("invalid GUI reservation token")
    return path.with_name("supervisor-gui-" + token + ".command")


def stage_command(reservation, command):
    path, token = Path(reservation["state_path"]), reservation["token"]
    with exclusive(path):
        matching(path, token)
        target = command_path(path, token)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(command + "\n")
    return target


def release(path, token):
    # The caller must verify any submitted terminal and worker have ended.
    # A live wrapper holds this lock for its entire child lifetime.
    with exclusive(path):
        matching(path, token)
        command_path(path, token).unlink(missing_ok=True)
        path.unlink()


def marketplace(home, default_home):
    source, link = default_home / BUNDLED_MARKETPLACE, home / BUNDLED_MARKETPLACE
    if not source.is_dir():
        raise ValueError("default bundled marketplace is missing; restore it through Codex before launch")
    if home.resolve() == default_home.resolve():
        return {"path": str(link), "source": str(source), "link": "default_home"}
    # Never create paths through a redirected account parent or replace existing data.
    for parent in (home / ".tmp", home / ".tmp/bundled-marketplaces"):
        if parent.is_symlink():
            raise ValueError("bundled marketplace parent is a symlink; inspect it before launch")
    if link.is_symlink():
        if link.resolve() != source.resolve():
            raise ValueError("bundled marketplace link has another target; preserve it")
        action = "existing"
    elif link.exists():
        raise ValueError("bundled marketplace path already contains data; preserve it")
    else:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(source, target_is_directory=True)
        action = "created"
    return {"path": str(link), "source": str(source), "link": action}


def run(path, token, command_file):
    with exclusive(path, wait_seconds=2):
        data = matching(path, token)
        if data["phase"] != "reserved":
            raise ValueError("GUI reservation already consumed; never replay a launch")
        try:
            require_unlocked()
            if command_file != command_path(path, token) or not command_file.is_file():
                raise ValueError("GUI command file does not match its reservation")
            data.update(phase="running", pid=os.getpid())
            path.write_text(json.dumps(data), encoding="utf-8")
            return subprocess.call(["/bin/zsh", str(command_file)])
        finally:
            command_path(path, token).unlink(missing_ok=True)
            path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "release"))
    parser.add_argument("--state", type=Path, default=state_path())
    parser.add_argument("--token", required=True)
    parser.add_argument("--verified-worker-ended", action="store_true")
    parser.add_argument("--command-file", type=Path)
    args = parser.parse_args()
    try:
        if args.state != state_path():
            raise ValueError("GUI state must be the shared Supervisor state path")
        if args.action == "release":
            if not args.verified_worker_ended:
                raise ValueError("inspect the receipt and exact terminal; verify no submitted worker remains first")
            release(args.state, args.token)
        elif args.command_file:
            raise SystemExit(run(args.state, args.token, args.command_file))
        else:
            parser.error("run needs --command-file")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"refused: {error}\n")


if __name__ == "__main__":
    main()
