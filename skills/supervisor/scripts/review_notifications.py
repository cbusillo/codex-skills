# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Deliver review notices using the existing exact-session send routes."""

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path

from session_record import TranscriptChanged, session_status


class NotSent(ValueError):
    """Validation stopped before entering a delivery operation; recheck later."""

    def __init__(self, message, *, needs_attention=True):
        super().__init__(message)
        self.needs_attention = needs_attention


def empty_claude_composer(row):
    """Prove the supported single-line composer, without parser fallback."""
    line, screen = row["input_line"], row["screen"]
    index = line["line_index"]
    if (line["style"] != "empty" or not isinstance(line["text"], str) or line["text"].strip()
            or type(index) is not int or not 0 < index < len(screen) - 1):
        return False
    # Claude renders its marker in a separate width-two child; continuation
    # text is indented. The shared last-line parser also matches quote markers,
    # so its empty classification alone cannot prove the composer is empty.
    if not re.fullmatch(r"❯\s*", screen[index]):
        return False
    def rule(text):
        return re.fullmatch(r"[─━-]{3,}\s*", text) is not None

    return (rule(screen[index - 1]) and rule(screen[index + 1])
            and not any(rule(text) for text in screen[index + 2:]))


def prepare(entry, message, *, supervisor=False):
    # Native transcript identity must agree with the ledger on either harness.
    status = session_status(entry)
    if entry.get("supervisor_owned") is not True and not (supervisor and entry.get("role") == "supervisor"):
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
                try:
                    outcome = json.loads(result.stdout)
                except ValueError:
                    outcome = None
                if isinstance(outcome, dict) and outcome.get("ok") is False and outcome.get("not_sent") is True:
                    raise NotSent(str(outcome.get("error") or "native queue was certainly not sent"))
                raise ValueError(f"Codex review notification failed: {result.stdout.strip()} {result.stderr.strip()}".strip())
        return queue
    if entry.get("harness") != "claude" or entry.get("backend") != "tmux":
        raise ValueError("Claude review notification requires the ledger's supported tmux route")
    from session_liveness import inventory
    from tmux_terminal import Terminal, belongs_to, process_parents

    if not entry.get("tmux_socket"):
        raise ValueError("notification requires the ledger's tmux socket")
    terminal = Terminal(entry["tmux_socket"])
    identity = entry.get("tmux_session_id")
    if not isinstance(identity, str) or not re.fullmatch(rf"tmux:{re.escape(terminal.socket_name)}:[0-9a-f]{{32}}", identity):
        raise ValueError("notification requires the ledger's exact tmux launch identity")

    def verify():
        current = session_status(entry)
        if not current["at_turn_end"] or any(task.get("kind") != "scheduled" for task in current["background_tasks"]):
            raise NotSent("Claude author is not at a verified idle prompt", needs_attention=False)
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
        if not empty_claude_composer(row):
            raise NotSent("Claude full input area is not verified empty; no keys sent", needs_attention=False)
        return current, row

    before = verify()
    if verify() != before:
        raise NotSent("Claude author changed before notification; no keys sent", needs_attention=False)
    return lambda: asyncio.run(terminal.send(identity, message))


def send(entry: dict, message: str, *, supervisor: bool = False) -> None:
    from iterm_tab import SendFailure

    try:
        deliver = prepare(entry, message, supervisor=supervisor)
    except PermissionError:
        # A real permission refusal remains spent, even before delivery.
        raise
    except NotSent:
        raise
    except TranscriptChanged as error:
        raise NotSent(str(error), needs_attention=False) from error
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
