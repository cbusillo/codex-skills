# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""tmux backend for iterm_tab; launchd startup and an explicit read-only view."""

import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

import account_choice
import iterm_tab

# 3.7 added send-keys enforcement for read-only control clients. Older servers
# still support headless workers, but must never be exposed as read-only views.
READONLY_CONTROL_MINIMUM = (3, 7)
LAUNCH_ENVIRONMENT_KEYS = (
    "PATH", "CODE_HOME", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_TOOL_DIR",
    "UV_TOOL_BIN_DIR", "CARGO_HOME", "RUSTUP_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
    "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE", "SSH_AUTH_SOCK", "SSH_AGENT_PID",
)
PANE_FIELDS = ("session_id", "window_id", "pane_id", "pane_title", "pane_tty",
               "pane_pid", "pane_dead", "pane_dead_status", "pane_dead_signal", "@supervisor_identity", "pid", "pane_dead_time")


def target_identity(row: dict) -> dict:
    return {key: value for key, value in row.items() if key != "title"}


def process_parents():
    """Read ancestry only; missing or malformed process evidence preserves the pane."""
    result = subprocess.run(["ps", "-axo", "pid=,ppid="], check=True,
                            capture_output=True, text=True, timeout=10)
    parents = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not all(field.isdigit() for field in fields):
            raise ValueError("unparseable process ancestry")
        pid, parent = map(int, fields)
        if pid <= 0 or pid in parents:
            raise ValueError("uncertain process ancestry")
        parents[pid] = parent
    if not parents:
        raise ValueError("process ancestry unavailable")
    return parents


def belongs_to(pid, root, parents):
    visited = set()
    while pid in parents and pid not in visited:
        if pid == root:
            return True
        visited.add(pid)
        pid = parents[pid]
    return False


class Terminal:
    def __init__(self, socket_name="supervisor"):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", socket_name):
            raise ValueError("tmux socket must be a simple dedicated server name")
        self.socket_name = socket_name

    def run(self, *argv, check=True):
        result = subprocess.run(
            ["tmux", "-N", "-L", self.socket_name, *argv],
            capture_output=True, text=True, timeout=10,
        )
        if check and result.returncode:
            diagnostic = ": command too long" if result.stderr.strip() == "command too long" else ""
            raise ValueError(f"tmux {argv[0]} failed{diagnostic}; list and read before retrying")
        return result

    def send_keys(self, pane, *keys):
        # tmux otherwise chooses the most recently attached UI client and
        # inherits its read-only restriction, even for this independent command.
        # An explicit empty -c sends only to the verified pane, without a UI
        # client context. The view's own send-keys commands stay read-only.
        client = ("-c", "") if self.readonly_control_supported(pane) else ()
        return self.run("send-keys", *client, "-t", pane, *keys)

    def readonly_control_supported(self, target):
        # Check the running server, which may predate an upgraded client binary.
        version = self.run("display-message", "-p", "-t", target, "#{version}").stdout.strip()
        match = re.fullmatch(r"(\d+)\.(\d+)[a-z]?", version)
        return bool(match and tuple(map(int, match.groups())) >= READONLY_CONTROL_MINIMUM)

    def require_readonly_view(self, container):
        if not self.readonly_control_supported(container):
            raise ValueError(self.readonly_view_requirement())

    @staticmethod
    def readonly_view_requirement():
        minimum = ".".join(map(str, READONLY_CONTROL_MINIMUM))
        return f"read-only views require a tmux {minimum}+ server; keep workers headless until the normal server shutdown/update boundary"

    def launchd_target(self):
        # Include the socket directory: isolated tests never share a service.
        directory = os.path.realpath(os.environ.get("TMUX_TMPDIR", "/tmp"))
        identity = hashlib.sha256((directory + "/" + self.socket_name).encode()).hexdigest()[:20]
        return f"gui/{os.getuid()}/com.codex-skills.tmux.{identity}"

    def ensure_server(self):
        probe = self.run("list-sessions", check=False)
        if probe.returncode == 0 or probe.stderr.strip() == "no sessions":
            return "existing"
        if not any(message in probe.stderr for message in ("no server running", "No such file or directory")):
            raise ValueError("tmux server state is uncertain; inspect before creating a container")
        if sys.platform != "darwin":
            return "native"
        executable = shutil.which("tmux")
        if not executable:
            raise ValueError("tmux executable unavailable")
        target = self.launchd_target()
        loaded = subprocess.run(["launchctl", "print", target], capture_output=True, timeout=10)
        if loaded.returncode:
            # A transient login-domain service, not a saved login item. tmux's
            # native foreground mode keeps it owned by launchd, outside iTerm's
            # process tree. No saved tmux or iTerm configuration is modified.
            definition = {"Label": target.rsplit("/", 1)[1],
                          "ProgramArguments": [executable, "-D", "-L", self.socket_name],
                          "RunAtLoad": True,
                          "EnvironmentVariables": {key: os.environ[key] for key in
                              ("PATH", "TMUX_TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "XDG_CONFIG_HOME")
                              if key in os.environ}}
            with tempfile.NamedTemporaryFile(suffix=".plist") as handle:
                plistlib.dump(definition, handle)
                handle.flush()
                submitted = subprocess.run(["launchctl", "bootstrap", target.rsplit("/", 1)[0], handle.name],
                                           capture_output=True, timeout=10)
            if submitted.returncode:
                # A simultaneous creator may have registered the same service.
                loaded = subprocess.run(["launchctl", "print", target], capture_output=True, timeout=10)
                if loaded.returncode:
                    raise ValueError(f"launchd startup failed; inspect {target}; no terminal-owned fallback")
        # No -k: this can start a stopped registration or reuse a concurrent
        # creator's running process, but never kill/restart a live server.
        subprocess.run(["launchctl", "kickstart", target], capture_output=True, timeout=10, check=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            probe = self.run("list-sessions", check=False)
            if probe.returncode == 0 or probe.stderr.strip() == "no sessions":
                return "launchd"
            time.sleep(0.05)
        raise ValueError(f"launchd server not ready; inspect {target}; no terminal-owned fallback")

    def verify_container(self, container):
        if not re.fullmatch(r"\$[0-9]+", container):
            raise ValueError("window id must be the exact tmux session id from window/list")
        if not any(r["window_id"] == container for r in self.list()):
            raise ValueError("window id does not identify a tmux session")

    def view_command(self, container):
        if not re.fullmatch(r"\$[0-9]+", container):
            raise ValueError("window id must be the exact tmux session id from window/list")
        executable = shutil.which("tmux")
        if not executable:
            raise ValueError("tmux executable unavailable")
        # New iTerm commands do not inherit the launcher's shell PATH.
        return shlex.join([executable, "-N", "-L", self.socket_name, "-CC", "attach", "-r", "-t", container])

    def control_clients(self, container):
        self.verify_container(container)
        result = self.run("list-clients", "-t", container, "-F",
                          "#{session_id}\t#{client_control_mode}\t#{client_tty}\t#{client_readonly}")
        clients = []
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if (len(fields) != 4 or fields[0] != container or fields[1] not in {"0", "1"}
                    or fields[3] not in {"0", "1"}):
                raise ValueError("tmux client inventory is malformed")
            if fields[1] == "1" and fields[2]:
                if fields[3] != "1":
                    raise ValueError("existing control view is writable; the Director may use Shell > tmux > Detach before requesting a read-only view")
                clients.append(fields[2])
        return clients

    @staticmethod
    def view_command_matches(candidate, command):
        try:
            actual, expected = shlex.split(candidate), shlex.split(command)
        except (TypeError, ValueError):
            return False
        # Two launch environments may resolve tmux through different paths.
        return bool(actual) and os.path.basename(actual[0]) == "tmux" and actual[1:] == expected[1:]

    @contextmanager
    def view_lock(self, container):
        self.verify_container(container)
        identity = self.run("display-message", "-p", "-t", container,
                            "#{pid}\t#{socket_path}").stdout.strip().split("\t")
        if len(identity) != 2 or not identity[0].isdecimal() or not os.path.isabs(identity[1]):
            raise ValueError("tmux server identity unavailable")
        server, socket_path = identity
        path = os.path.join(os.path.dirname(socket_path),
                            f"supervisor-iterm-view-{server}-{container[1:]}.lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            metadata = os.fstat(lock.fileno())
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise ValueError("view lock is not private to this user")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True

    def view(self, container):
        if self.socket_name != "supervisor" or sys.platform != "darwin":
            return {"status": "skipped", "reason": "automatic view is macOS Supervisor-only"}
        if not self.readonly_control_supported(container):
            return {"status": "unsupported", "reason": self.readonly_view_requirement()}
        retry = shlex.join(["uv", "run", iterm_tab.__file__, "--backend", "tmux",
                            "view", "--window-id", container])
        clients = self.control_clients(container)
        if clients:
            return {"status": "attached", "client_count": len(clients)}
        # The native integration may select restored tabs independently of the
        # gateway's select=False. Opening is explicit, as permitted by #1854;
        # launches must not connect to or drive the Director's GUI.
        return {"status": "deferred", "reason": "view is opt-in because native tmux tabs may move focus",
                "retry_command": retry, "readback_required": True,
                "next_action": "Open view on request; choose the iTerm window once with --iterm-window-number."}

    def list(self):
        fields = PANE_FIELDS
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
            window, tab, pane, title, tty, pid, dead, status, death_signal, identity, server, died = values
            if not pid and dead != "1":
                raise ValueError("live tmux pane has no PID; inventory unavailable")
            identity = identity or f"{server}:{pane}"
            rows.append({"window_id": window, "tab_id": tab,
                         "session_id": f"tmux:{self.socket_name}:{identity}",
                         "title": title, "tty": tty, "pane_id": pane,
                         "pid": int(pid) if pid else None, "dead": dead == "1", "exit_status": status,
                         "exit_signal": death_signal,
                         "exited_at": datetime.fromtimestamp(int(died), timezone.utc).isoformat() if died.isdigit() else None,
                         "backend": "tmux", "tmux_socket": self.socket_name})
        return rows

    def select(self, identity):
        matches = [r for r in self.list() if r["session_id"] == identity]
        if len(matches) != 1:
            raise ValueError(f"expected one exact tmux session; found {len(matches)}")
        return matches[0]

    def read(self, identity):
        row = self.select(identity)
        captured = self.run("capture-pane", "-p", "-e", "-t", row["pane_id"]).stdout
        screen, styles = iterm_tab.ansi_screen(captured)
        return {**row, "screen": screen, "input_line": iterm_tab.input_line(screen, styles)}

    async def send(self, identity, text, submit=True, allow_tabs=False):
        with iterm_tab.input_preflight():
            iterm_tab.validate_text(text, allow_tabs=allow_tabs)
            row = self.select(identity)
            iterm_tab.validate_resume_input(text, tty=row.get("tty"))
            if row["dead"]:
                raise ValueError("agent process has exited; no input was sent")
        if "\n" in text or "\t" in text:
            text = "\x1b[200~" + text + "\x1b[201~"
        try:
            self.send_keys(row["pane_id"], "-l", "--", text)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            raise iterm_tab.SendFailure(identity, "not_attempted", "text send outcome unknown", text_sent=None) from error
        if submit:
            await asyncio.sleep(0.4)
            try:
                if target_identity(self.select(identity)) != target_identity(row):
                    raise ValueError("terminal state changed after text")
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                raise iterm_tab.SendFailure(identity, "not_attempted", str(error)) from error
            try:
                self.send_keys(row["pane_id"], "Enter")
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                raise iterm_tab.SendFailure(identity, "unknown", str(error)) from error

    def dead_pane(self, identity, entry, *, panes=None):
        if not re.fullmatch(r"tmux:" + re.escape(self.socket_name) + r":[0-9a-f]{32}", identity or ""):
            raise ValueError("terminal has no Supervisor launch identity")
        panes = self.list() if panes is None else panes
        matches = [pane for pane in panes if pane["session_id"] == identity]
        if len(matches) != 1:
            raise ValueError("expected one exact owned tmux pane")
        row = matches[0]
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
        siblings = [r for r in panes if r["tab_id"] == row["tab_id"]]
        if len(siblings) != 1:
            raise ValueError("split or linked tmux window; terminal kept")
        cutoff = datetime.fromisoformat(row["exited_at"]) if row["exited_at"] else None
        if cutoff is not None:
            try:
                started = datetime.fromisoformat(entry["launch_started_at"]) if entry.get("launch_started_at") is not None else None
            except TypeError as error:
                raise ValueError("native pane launch time is invalid") from error
            if (cutoff.tzinfo is None or cutoff > datetime.now(timezone.utc)
                    or started is not None and (started.tzinfo is None or started > cutoff)):
                raise ValueError("native pane exit time differs from this launch generation")
        return row

    def verify_closed(self, identity, entry):
        import session_liveness

        panes = self.list()
        row = self.dead_pane(identity, entry, panes=panes)
        rows = session_liveness.inventory()
        tty = row["tty"].removeprefix("/dev/")
        jobs = [pid for pid, job_tty, _ in rows if job_tty == tty]
        replacement = None
        if jobs:
            owners = [pane for pane in panes if pane["pane_id"] != row["pane_id"]
                      and not pane["dead"] and pane["tty"].removeprefix("/dev/") == tty]
            if len(owners) != 1 or owners[0]["pid"] not in jobs:
                raise ValueError("TTY ownership is uncertain; terminal kept")
            parents = process_parents()
            replacement = lambda pid: belongs_to(pid, owners[0]["pid"], parents)
        session_liveness.verify_process_exit(
            {**entry, "_terminal_pid": row["pid"]}, rows=rows,
            ended_at=datetime.fromisoformat(row["exited_at"]) if row["exited_at"] else None,
            replacement_owner=replacement, scope="terminal")
        # A dead pane has no input consumer. Never kill a live shell to clear it.
        return row

    def close(self, identity, ledger):
        import session_record
        entry = session_record.terminal_entry(ledger, "tmux_session_id", identity)
        before = self.verify_closed(identity, entry)
        if self.verify_closed(identity, entry) != before:
            raise ValueError("terminal state changed; terminal kept")
        marker = identity.removeprefix(f"tmux:{self.socket_name}:")
        if not re.fullmatch(r"[0-9a-f]{32}", marker):
            raise ValueError("terminal has no Supervisor launch identity")
        # Check liveness and the launch nonce inside the server as well. Remove
        # only the dead pane, so a concurrent split never kills a sibling.
        condition = "#{&&:#{pane_dead},#{==:#{@supervisor_identity}," + marker + "}}"
        try:
            self.run("if-shell", "-F", "-t", before["pane_id"], condition,
                     "kill-pane -t " + before["pane_id"])
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            raise iterm_tab.CloseUncertain("native close may have been sent; inspect the exact pane without replay") from error
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
            raise iterm_tab.InputRefused("verify the target, input line and prompt before --verified-target")
        if args.command == "clear":
            row = terminal.select(args.session_id)
            if row["dead"]:
                raise ValueError("agent process has exited; no input was sent")
            terminal.send_keys(row["pane_id"], "C-u")
        else:
            with iterm_tab.input_preflight():
                text = args.text_file.read_text(encoding="utf-8") if args.text_file else args.text
                row = terminal.select(args.session_id)
                ended = iterm_tab.exit_observer(text, row.get("tty")) if not args.no_submit else None
            await terminal.send(args.session_id, text, submit=False)

            async def read_message() -> dict:
                if target_identity(terminal.select(args.session_id)) != target_identity(row):
                    raise ValueError("terminal state changed after text")
                return terminal.read(args.session_id)

            async def press_message(key: str) -> None:
                if target_identity(terminal.select(args.session_id)) != target_identity(row):
                    raise iterm_tab.SendFailure(args.session_id, "not_attempted", "terminal state changed after text")
                terminal.send_keys(row["pane_id"], "Tab" if key == "\t" else "Enter")

            return await iterm_tab.submit_message(args.session_id, text, not args.no_submit, read_message, press_message, ended)
        return {"session_id": args.session_id, "sent": True, "readback_required": True}
    if args.command == "window":
        # One tmux session is the dedicated Supervisor container; each task is a window.
        name = "supervisor-" + uuid.uuid4().hex[:12]
        startup = terminal.ensure_server()
        # Non-macOS retains native tmux startup; macOS must never start a server
        # in the terminal's process tree, even if it vanishes during creation.
        if startup == "native":
            created = subprocess.run(["tmux", "-L", terminal.socket_name, "new-session", "-d", "-s", name,
                                      "-n", "control", "-P", "-F", "#{session_id}"],
                                     capture_output=True, text=True, timeout=10, check=True)
        else:
            created = terminal.run("new-session", "-d", "-s", name, "-n", "control", "-P", "-F", "#{session_id}")
        window = created.stdout.strip()
        supported = terminal.readonly_control_supported(window)
        result = {"window_id": window, "backend": "tmux", "tmux_socket": args.tmux_socket,
                  "server_startup": startup, "view_command": terminal.view_command(window) if supported else None}
        if not supported:
            result["view_warning"] = terminal.readonly_view_requirement()
        return result

    terminal.verify_container(args.window_id)
    commands, choices = iterm_tab.prepare_launches(args)
    results = []
    for command, choice in zip(commands, choices):
        progress = {"window_id": args.window_id, "tab_id": None, "session_id": None,
                    "receipt_written": False, "submission": "not_attempted", "phase": "create_tab",
                    "backend": "tmux", "tmux_socket": args.tmux_socket}
        if choice:
            progress["account"] = account_choice.public(choice)
        launch_script = None
        try:
            identity = uuid.uuid4().hex
            gate = "start-" + identity
            # The command cannot execute until after remain-on-exit and the receipt.
            # sh replaces itself with zsh; the pane ends when this invocation ends.
            staged = ("IFS= read -r supervisor_start; "
                      f'[ "$supervisor_start" = {shlex.quote(gate)} ] && '
                      f"exec /bin/zsh -c {shlex.quote(command)}")
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="supervisor-launch-",
                                             suffix=".sh", delete=False) as handle:
                launch_script = Path(handle.name)
                handle.write('rm -f -- "$0" || exit\n' + staged + "\n")
            environment = [argument for key in LAUNCH_ENVIRONMENT_KEYS if key in os.environ
                           for argument in ("-e", key + "=" + os.environ[key])]
            created = terminal.run("new-window", "-d", "-t", args.window_id + ":", "-n", "agent",
                                   *environment,
                                   "-P", "-F", "#{window_id}\t#{pane_id}",
                                   "exec /bin/sh " + shlex.quote(str(launch_script))).stdout.strip().split("\t")
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
            progress.update(submission="submitted", phase="verify_startup")

            async def read_startup():
                try:
                    snapshot = terminal.read(sid)
                except (ValueError, OSError, subprocess.SubprocessError) as error:
                    raise iterm_tab.StartupFailure({"dead": None, "screen": [],
                                                   "observation_error": type(error).__name__}) from error
                return {key: snapshot[key] for key in ("dead", "exit_status", "exit_signal", "screen")}

            await iterm_tab.verify_startup(read_startup, prompt_window=iterm_tab.startup_prompt_window(choice),
                                           submitted=command)
            result = {key: row[key] for key in ("window_id", "tab_id", "session_id", "tty", "pid", "backend", "tmux_socket")}
            if choice:
                result["account"] = account_choice.public(choice)
            iterm_tab.source_launch_metadata(result, choice)
            progress.update(phase="register_ledger", launch=result)
            await iterm_tab.register_fresh_launch(args, result, choice)
            results.append(result)
        except Exception as error:
            if launch_script is not None and progress["tab_id"] is None:
                launch_script.unlink(missing_ok=True)
            iterm_tab.cancel_unsubmitted_gui(choice, progress)
            raise iterm_tab.LaunchFailure(results, progress, error) from error
        if len(results) == 1:
            try:
                result["view"] = terminal.view(args.window_id)
            except Exception as error:
                result["view"] = {"status": "unavailable", "reason": str(error) if isinstance(error, ValueError) else type(error).__name__,
                                  "readback_required": True,
                                  "next_action": "Inspect iTerm, then use view --window-id; do not relaunch agents."}
    return results[0] if len(results) == 1 else results
