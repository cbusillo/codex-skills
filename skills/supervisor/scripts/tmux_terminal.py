# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""tmux backend for iterm_tab; no GUI connection or account-choice policy."""

import asyncio
import json
import os
import re
import shlex
import subprocess
import uuid

import account_choice
import iterm_tab


class Terminal:
    def __init__(self, socket_name="supervisor"):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", socket_name):
            raise ValueError("tmux socket must be a simple dedicated server name")
        self.socket_name = socket_name

    def run(self, *argv, check=True):
        result = subprocess.run(
            ["tmux", "-L", self.socket_name, *argv],
            capture_output=True, text=True, timeout=10,
        )
        if check and result.returncode:
            # tmux diagnostics can echo the command/brief; retain targets, not text.
            raise ValueError(f"tmux {argv[0]} failed; list and read before retrying")
        return result

    def list(self):
        fields = ("session_id", "window_id", "pane_id", "pane_title", "pane_tty",
                  "pane_pid", "pane_dead", "pane_dead_status", "@supervisor_identity", "pid")
        result = self.run("list-panes", "-a", "-F", "\t".join("#{" + f + "}" for f in fields), check=False)
        if result.returncode:
            # A missing server is empty; other failures are uncertain inventory.
            if "no server running" in result.stderr or "No such file or directory" in result.stderr:
                return []
            raise ValueError("tmux inventory unavailable")
        rows = []
        for line in result.stdout.splitlines():
            values = line.split("\t")
            if len(values) != len(fields):
                raise ValueError("tmux inventory is malformed")
            window, tab, pane, title, tty, pid, dead, status, identity, server = values
            identity = identity or f"{server}:{pane}"
            rows.append({"window_id": window, "tab_id": tab,
                         "session_id": f"tmux:{self.socket_name}:{identity}",
                         "title": title, "tty": tty, "pane_id": pane,
                         "pid": int(pid), "dead": dead == "1", "exit_status": status,
                         "backend": "tmux", "tmux_socket": self.socket_name})
        return rows

    def select(self, identity):
        matches = [r for r in self.list() if r["session_id"] == identity]
        if len(matches) != 1:
            raise ValueError(f"expected one exact tmux session; found {len(matches)}")
        return matches[0]

    def read(self, identity):
        row = self.select(identity)
        screen = self.run("capture-pane", "-p", "-t", row["pane_id"]).stdout.splitlines()
        return {**row, "screen": screen}

    async def send(self, identity, text, submit=True, allow_tabs=False):
        iterm_tab.validate_text(text, allow_tabs=allow_tabs)
        row = self.select(identity)
        if row["dead"]:
            raise ValueError("agent process has exited; no input was sent")
        if "\n" in text or "\t" in text:
            text = "\x1b[200~" + text + "\x1b[201~"
        self.run("send-keys", "-t", row["pane_id"], "-l", "--", text)
        if submit:
            await asyncio.sleep(0.4)
            if self.select(identity) != row:
                raise ValueError("terminal state changed after text; inspect before submitting or retrying")
            self.run("send-keys", "-t", row["pane_id"], "Enter")

    def verify_closed(self, identity, entry):
        from close_ttys import inventory

        row = self.select(identity)
        if entry.get("supervisor_owned") is not True or entry.get("tmux_session_id") != identity:
            raise ValueError("exact terminal is not Supervisor-owned in the launch ledger")
        if not isinstance(entry.get("pid"), int) or entry["pid"] <= 0:
            raise ValueError("ledger requires the verified agent PID")
        for key in ("tty", "window_id", "tab_id", "tmux_socket"):
            expected = entry.get(key)
            actual = row[key]
            if key == "tty" and isinstance(expected, str):
                expected, actual = expected.removeprefix("/dev/"), actual.removeprefix("/dev/")
            if expected != actual:
                raise ValueError(f"terminal {key} differs from launch ledger")
        if not row["dead"]:
            raise ValueError("agent process remains; terminal kept")
        siblings = [r for r in self.list() if r["tab_id"] == row["tab_id"]]
        if len(siblings) != 1:
            raise ValueError("split or linked tmux window; terminal kept")
        rows = inventory()
        tty = row["tty"].removeprefix("/dev/")
        if any(pid in {entry["pid"], row["pid"]} or job_tty == tty for pid, job_tty, _ in rows):
            raise ValueError("process remains or PID was reused; terminal kept")
        # A dead pane has no input consumer. Never kill a live shell to clear it.
        return row

    def close(self, identity, ledger):
        entries = json.loads(ledger.read_text(encoding="utf-8"))
        if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
            raise ValueError("ledger must contain session objects")
        matches = [e for e in entries if e.get("tmux_session_id") == identity]
        if len(matches) != 1:
            raise ValueError("terminal is not unique in the launch ledger")
        before = self.verify_closed(identity, matches[0])
        if self.verify_closed(identity, matches[0]) != before:
            raise ValueError("terminal state changed; terminal kept")
        marker = identity.removeprefix(f"tmux:{self.socket_name}:")
        if not re.fullmatch(r"[0-9a-f]{32}", marker):
            raise ValueError("terminal has no Supervisor launch identity")
        # Check liveness and the launch nonce inside the server as well. Remove
        # only the dead pane, so a concurrent split never kills a sibling.
        condition = "#{&&:#{pane_dead},#{==:#{@supervisor_identity}," + marker + "}}"
        self.run("if-shell", "-F", "-t", before["pane_id"], condition,
                 "kill-pane -t " + before["pane_id"])
        return {"session_id": identity, "close_requested": True, "readback_required": True}


async def operate(args):
    terminal = Terminal(args.tmux_socket)
    if args.command == "list":
        return terminal.list()
    if args.command == "read":
        return terminal.read(args.session_id)
    if args.command == "close":
        return terminal.close(args.session_id, args.ledger)
    if args.command in {"send", "clear"}:
        if not args.verified_target:
            raise ValueError("verify the target, input line and prompt before --verified-target")
        if args.command == "clear":
            row = terminal.select(args.session_id)
            if row["dead"]:
                raise ValueError("agent process has exited; no input was sent")
            terminal.run("send-keys", "-t", row["pane_id"], "C-u")
        else:
            text = args.text_file.read_text(encoding="utf-8") if args.text_file else args.text
            await terminal.send(args.session_id, text, not args.no_submit)
        return {"session_id": args.session_id, "sent": True, "readback_required": True}
    if args.command == "window":
        # One tmux session is the dedicated Supervisor container; each task is a window.
        name = "supervisor-" + uuid.uuid4().hex[:12]
        window = terminal.run("new-session", "-d", "-s", name, "-n", "control", "-P", "-F", "#{session_id}").stdout.strip()
        return {"window_id": window, "backend": "tmux", "tmux_socket": args.tmux_socket,
                "view_command": f"tmux -L {args.tmux_socket} -CC attach -t {shlex.quote(window)}"}

    if not re.fullmatch(r"\$[0-9]+", args.window_id):
        raise ValueError("window id must be the exact tmux session id from window/list")
    if not any(r["window_id"] == args.window_id for r in terminal.list()):
        raise ValueError("window id does not identify a tmux session")
    commands, choices = iterm_tab.prepare_launches(args)
    results = []
    for command, choice in zip(commands, choices):
        progress = {"window_id": args.window_id, "tab_id": None, "session_id": None,
                    "receipt_written": False, "submission": "not_attempted", "phase": "create_tab",
                    "backend": "tmux", "tmux_socket": args.tmux_socket}
        if choice:
            progress["account"] = account_choice.public(choice)
        try:
            identity = uuid.uuid4().hex
            gate = "start-" + identity
            # The command cannot execute until after remain-on-exit and the receipt.
            # sh replaces itself with zsh; the pane ends when this invocation ends.
            staged = ("IFS= read -r supervisor_start; "
                      f'[ "$supervisor_start" = {shlex.quote(gate)} ] && '
                      f"exec /bin/zsh -c {shlex.quote(command)}")
            created = terminal.run("new-window", "-d", "-t", args.window_id + ":", "-n", "agent",
                                   "-e", "PATH=" + os.environ.get("PATH", ""),
                                   "-P", "-F", "#{window_id}\t#{pane_id}",
                                   "exec /bin/sh -c " + shlex.quote(staged)).stdout.strip().split("\t")
            if len(created) != 2:
                raise ValueError("new terminal identity unavailable")
            tab, pane = created
            progress.update(tab_id=tab, pane_id=pane, phase="prepare_terminal")
            terminal.run("set-option", "-w", "-t", tab, "remain-on-exit", "on")
            terminal.run("set-option", "-p", "-t", pane, "@supervisor_identity", identity)
            sid = f"tmux:{args.tmux_socket}:{identity}"
            row = terminal.select(sid)
            progress.update(session_id=sid, phase="write_receipt")
            if choice:
                account_choice.record_launch(choice)
                progress["receipt_written"] = True
            progress.update(submission="unknown", phase="submit_command")
            await terminal.send(sid, gate)
            result = {key: row[key] for key in ("window_id", "tab_id", "session_id", "tty", "pid", "backend", "tmux_socket")}
            if choice:
                result["account"] = account_choice.public(choice)
            results.append(result)
        except Exception as error:
            raise iterm_tab.LaunchFailure(results, progress, error) from error
    return results[0] if len(results) == 1 else results
