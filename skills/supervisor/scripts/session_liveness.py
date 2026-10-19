# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Shared process and transcript facts; callers retain closure and terminal authority."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shlex
import subprocess
import re
import uuid


def native_uuid(value):
    """Canonical identity for the UUID forms accepted by native ThreadId parsing."""
    if not isinstance(value, str):
        return None
    hyphenated = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
    if not re.fullmatch(r"[0-9a-fA-F]{32}|" + hyphenated + r"|\{" + hyphenated + r"\}|urn:uuid:" + hyphenated, value):
        return None
    return str(uuid.UUID(value))


def process_rows(output):
    rows = []
    for line in output.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3 or not fields[0].isdigit():
            raise ValueError("unparseable process inventory")
        rows.append((int(fields[0]), fields[1], fields[2]))
    return rows


def inventory():
    return process_rows(subprocess.run(
        ["ps", "-ww", "-axo", "pid=,tty=,command="], check=True, capture_output=True,
        text=True, timeout=10).stdout)


def process_started_at(pid):
    value = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], check=True,
                           capture_output=True, text=True, timeout=2,
                           env={**os.environ, "TZ": "UTC", "LC_ALL": "C"}).stdout.strip()
    return datetime.strptime(value, "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)


def idle_terminal_command(command):
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False
    name = Path(argv[0]).name.lstrip("-")
    return name == "login" or name in {"zsh", "bash", "sh", "fish", "dash"} and all(
        arg in {"-l", "-i", "-il", "-li", "--login", "--interactive"} for arg in argv[1:])


def harness_family(executable):
    name = Path(executable).name.casefold()
    if name in {"codex-session.py", "codex_session_proxy.py"}:
        return None  # Managed wrappers need their own launch binding.
    if name == "codex.js":
        return "codex"
    if name.endswith((".py", ".sh")):
        return None  # Repository utilities are not native harness executables.
    if name.startswith("codex"):
        return "codex"
    return "claude" if name in {"claude", "claude.js"} else None


def harness_arguments(command):
    # ps loses quoting. Only actual executable positions and operands before
    # the prompt separator establish identity; arbitrary prompt text does not.
    words = command.split()
    positions = words[:1]
    if words and (Path(words[0]).name in {"node", "nodejs", "bun", "deno", "sh", "bash", "zsh"}
                  or re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(words[0]).name)):
        positions = words[:2]
    index = next((i for i, arg in enumerate(positions)
                  if harness_family(arg) is not None or Path(arg).name in {"codex-session.py", "codex_session_proxy.py"}), None)
    if index is None:
        return []
    argv = words[index:]
    return argv[:argv.index("--")] if "--" in argv else argv


def invocation(argv):
    # Do not reconstruct positional options from ps: one TOML/config value
    # can contain spaces. Scan the native prefix conservatively, rather than
    # losing a real resume behind a fragment of that value. The explicit --
    # prompt separator was already removed by harness_arguments.
    if not argv:
        return [], None
    family = harness_family(argv[0])
    codex = family == "codex"
    if family is None:
        return [], None  # Wrappers require their own guarded launch receipt.
    markers = [i for i, arg in enumerate(argv)
               if arg in {"--resume", "-r"} or codex and arg == "resume"]
    resumed = [argv[i + 1] for i in markers if i + 1 < len(argv) and not argv[i + 1].startswith("-")]
    options = [arg.split("=", 1)[1] for arg in argv if arg.startswith("--resume=")]
    resumed.extend(value for value in options if value and not value.startswith("-"))
    if markers or options or family == "claude" and any(arg in {"-c", "--continue"} for arg in argv[1:]):
        return resumed, "resume"
    return [], "exec" if codex and len(argv) > 1 and argv[1] in {"exec", "e"} else None


def verify_process_exit(entry, *, rows=None, ended_at=None, started_at=None,
                        independent_harness=None, replacement_owner=None, require_tty=False):
    """One inventory per probe; unknown execution or identity always preserves work."""
    rows = inventory() if rows is None else rows
    started_at = started_at or process_started_at
    tty = entry.get("tty")
    if not isinstance(tty, str) or not tty:
        if not entry.get("_inventory"):
            raise ValueError("ledger requires verified pid and tty")
    else:
        tty = tty.removeprefix("/dev/")
    if not entry.get("_inventory") and (type(entry.get("pid")) is not int or entry["pid"] <= 0):
        raise ValueError("ledger requires verified pid and tty")
    pids = {entry.get("pid"), entry.get("launch_pid"), entry.get("_terminal_pid")}
    if any(value is not None and (type(value) is not int or value <= 0) for value in pids):
        raise ValueError("ledger requires verified pid and tty")
    same_tty = [row for row in rows if row[1] == tty]
    if require_tty and not same_tty:
        raise ValueError("no process rows match the terminal TTY; inventory is uncertain")
    source = native_uuid(entry.get("session_id")) or entry.get("session_id")
    for pid, process_tty, command in rows:
        if pid in pids:
            raise ValueError("source PID (possibly reused) or work on its TTY remains; preserve claims and verify native identity")
        argv = harness_arguments(command)
        resumed, command_kind = invocation(argv)
        identities = [native_uuid(value) or value for value in resumed]
        if source in identities:
            raise ValueError("source native session is visible in a resumed harness process")
        if process_tty != tty or idle_terminal_command(command):
            continue
        if not entry.get("_backfill"):
            try:
                born = started_at(pid) if ended_at is not None else None
                newer = ended_at is not None and ended_at < born <= datetime.now(timezone.utc)
            except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
                raise ValueError("process birth is unavailable; terminal kept") from error
            if not newer:
                raise ValueError("source PID (possibly reused) or work on its TTY remains; preserve claims and verify native identity")
        owner_known = replacement_owner is not None and replacement_owner(pid)
        if replacement_owner is not None and not owner_known:
            raise ValueError("process belongs to no verified new TTY owner; terminal kept")
        if argv and (harness_family(argv[0]) in ({entry["harness"]} if entry.get("harness") else {"codex", "claude"})
                     or Path(argv[0]).name in {"codex-session.py", "codex_session_proxy.py"}):
            explicit_other = len(resumed) == 1 and native_uuid(resumed[0]) is not None
            noninteractive = command_kind == "exec"
            guarded = independent_harness is not None and independent_harness(pid, argv)
            if not (explicit_other or noninteractive or guarded):
                raise ValueError("unidentified harness on the recorded TTY; preserve claims and verify native identity")
    return rows


def require_safe_status(status):
    """Check the declaration only after the caller independently proves exit."""
    if not status["safe_verdict"]:
        raise ValueError("latest transcript lacks an unqualified Safe to exit: yes")


def native_exit_cutoff(entry):
    """Read an exact dead pane, never a caller's proposed ledger timestamp."""
    if not entry.get("tmux_session_id"):
        return None
    from tmux_terminal import Terminal
    try:
        terminal = Terminal(entry.get("tmux_socket", "supervisor"))
        row = terminal.dead_pane(entry["tmux_session_id"], entry)
        return datetime.fromisoformat(row["exited_at"]) if row.get("exited_at") else None
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        # A cutoff only permits verified replacement work on a reused TTY.
        # Its absence retains the stricter PID/TTY path; it is not exit proof.
        return None
