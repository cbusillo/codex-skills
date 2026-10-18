# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Deliver review notices using the existing exact-session send routes."""

import asyncio
import os
import re
import subprocess
from pathlib import Path

from session_record import session_status


class NotSent(ValueError):
    """Validation stopped before entering a delivery operation; recheck later."""


def prepare(entry, message):
    # Native transcript identity must agree with the ledger on either harness.
    status = session_status(entry)
    if entry.get("supervisor_owned") is not True:
        raise ValueError("notification requires an explicitly Supervisor-owned session")
    if entry["harness"] == "codex":
        home = entry.get("codex_home")
        if not isinstance(home, str) or not Path(home).expanduser().is_absolute():
            raise ValueError("notification requires the ledger's absolute codex_home")
        home = Path(home).expanduser().resolve(strict=True)
        if not Path(status["transcript"]).resolve().is_relative_to((home / "sessions").resolve()):
            raise ValueError("notification transcript is outside the ledger's Codex home")
        # The supported helper verifies the native UUID, managed source endpoint
        # and ownership lease. It never falls back to terminal input or a daemon.
        def queue():
            result = subprocess.run(
                ["uv", "run", str(Path(__file__).with_name("codex-session.py")), "queue",
                 "--session-id", entry["session_id"], "--codex-home", str(home), "--message", message],
                env={**os.environ, "CODEX_HOME": str(home)}, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=30, check=False)
            if result.returncode:
                raise ValueError(f"Codex review notification failed: {result.stdout.strip()} {result.stderr.strip()}".strip())
        return queue
    if entry.get("harness") != "claude" or entry.get("backend") != "tmux":
        raise ValueError("Claude review notification requires the ledger's supported tmux route")
    from close_ttys import inventory
    from tmux_terminal import Terminal, belongs_to, process_parents

    terminal = Terminal(entry.get("tmux_socket", "supervisor"))
    identity = entry.get("tmux_session_id")
    if not isinstance(identity, str) or not re.fullmatch(rf"tmux:{re.escape(terminal.socket_name)}:[0-9a-f]{{32}}", identity):
        raise ValueError("notification requires the ledger's exact tmux launch identity")

    def verify():
        current = session_status(entry)
        if not current["at_turn_end"] or current["background_tasks"]:
            raise ValueError("Claude author is not at a verified idle prompt")
        row = terminal.read(identity)
        if row["dead"]:
            raise ValueError("Claude author terminal has exited")
        for key in ("tty", "window_id", "tab_id", "tmux_socket"):
            actual, expected = row[key], entry.get(key)
            if key == "tty" and isinstance(expected, str):
                actual, expected = actual.removeprefix("/dev/"), expected.removeprefix("/dev/")
            if actual != expected:
                raise ValueError(f"notification terminal {key} differs from ledger")
        processes = inventory()
        pid = entry.get("pid")
        matching = [p for p in processes if p[0] == pid and p[1] == row["tty"].removeprefix("/dev/")]
        if (type(pid) is not int or pid <= 0 or len(matching) != 1
                or not belongs_to(pid, row["pid"], process_parents())):
            raise ValueError("notification agent PID is not verified in the ledger's tmux pane")
        line = row["input_line"]
        if line["style"] != "empty" or line["text"] != "":
            raise ValueError("Claude input line is not verified empty; no keys sent")
        # The shared parser identifies only the first composer line. Require its
        # closing separator immediately below, so wrapped input cannot be hidden.
        following = row["screen"][line["line_index"] + 1:]
        if not following or not re.fullmatch(r"\s*[─━-]{3,}\s*", following[0]):
            raise ValueError("Claude full input area is not verified empty; no keys sent")
        return current, row

    before = verify()
    if verify() != before:
        raise ValueError("Claude author changed before notification; no keys sent")
    return lambda: asyncio.run(terminal.send(identity, message))


def send(entry, message):
    from iterm_tab import SendFailure

    try:
        deliver = prepare(entry, message)
    except PermissionError:
        # A real permission refusal remains spent, even before delivery.
        raise
    except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError) as error:
        raise NotSent(str(error)) from error
    try:
        deliver()
    except SendFailure:
        raise  # Text or Return may have reached the terminal.
    except ValueError as error:
        if entry["harness"] == "claude":
            # Terminal.send wraps every post-key failure in SendFailure. Its
            # remaining ValueErrors are its own pre-key validation checks.
            raise NotSent(str(error)) from error
        raise  # A Codex queue call was entered; never replay its refusal.
