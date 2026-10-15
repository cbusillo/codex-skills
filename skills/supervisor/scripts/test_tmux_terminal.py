#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["iterm2==2.25"]
# ///
"""Exercise the shared launcher against a disposable real tmux server."""

import argparse
import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import account_choice
import iterm2
import close_ttys
import iterm_tab
import tmux_terminal


class ViewTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state_path = Path(folder.name) / "view.json"
        self.terminal = tmux_terminal.Terminal("supervisor")
        no_live_tmux = patch.object(self.terminal, "run", side_effect=AssertionError("unit view tests must never query the live socket"))
        no_live_tmux.start()
        self.addCleanup(no_live_tmux.stop)
        capability = patch.object(self.terminal, "readonly_control_supported", return_value=True)
        capability.start()
        self.addCleanup(capability.stop)
        executable = patch.object(tmux_terminal.shutil, "which", return_value="/fixture/bin/tmux")
        executable.start()
        self.addCleanup(executable.stop)
        self.container = "$17"
        self.command = self.terminal.view_command(self.container)
        self.app = SimpleNamespace(terminal_windows=[], buried_sessions=[], async_refresh=AsyncMock(),
                                   current_terminal_window=None)
        self.created = []
        self.startup_input = []

        async def create(connection=None, *, profile_customizations, select=None, into=None):
            # Decode the actual pinned SDK's wire properties, layering them on
            # a default profile that would otherwise send startup input.
            properties = {"Initial Text": "source personal-config", "Send Code When Idle": True,
                          "Triggers": [{"action": "send-text"}]}
            properties.update({key: json.loads(value) for key, value in profile_customizations.values.items()})
            self.created.append(properties)
            if text := properties.get("Initial Text"):
                self.startup_input.append(text)
            session = self.session("gateway", "/dev/ttys987", properties["Command"])
            tab = SimpleNamespace(sessions=[session])
            if into:
                into.tabs.append(tab)
                return tab
            window = SimpleNamespace(window_id="view-window", window_number=7, tabs=[tab])
            async def create_tab(**kwargs):
                return await create(into=window, **kwargs)
            window.async_create_tab = AsyncMock(side_effect=create_tab)
            self.app.terminal_windows.append(window)
            return window

        self.sdk = SimpleNamespace(
            async_get_app=AsyncMock(return_value=self.app),
            preferences=SimpleNamespace(async_get_preference=AsyncMock(return_value=2),
                                        PreferenceKey=SimpleNamespace(OPEN_TMUX_WINDOWS_IN="restore")),
            LocalWriteOnlyProfile=iterm2.LocalWriteOnlyProfile, Profile=iterm2.Profile,
            Window=SimpleNamespace(async_create=AsyncMock(side_effect=create)),
        )

    @staticmethod
    def session(identity, tty, command="shell"):
        # No input methods: the creation path cannot accidentally type into it.
        return SimpleNamespace(session_id=identity, async_get_variable=AsyncMock(return_value=tty),
                               async_get_profile=AsyncMock(return_value=SimpleNamespace(command=command)))

    def open(self, clients=(), **kwargs):
        with patch.dict(sys.modules, {"iterm2": self.sdk}), \
                patch.object(self.terminal, "control_clients", return_value=clients), \
                patch.object(self.terminal, "view_lock", return_value=nullcontext(True)):
            return asyncio.run(iterm_tab.open_tmux_view("connection", self.terminal, self.container,
                                                      state_path=self.state_path, **kwargs))

    def test_no_client_creates_gateway_and_pending_gateway_prevents_second_window(self):
        result = self.open()
        self.assertEqual(result["status"], "opening")
        argv = shlex.split(self.created[0]["Command"])
        self.assertEqual(argv[argv.index("-t") + 1], self.container)
        self.assertEqual(argv[argv.index("-L") + 1], self.terminal.socket_name)
        self.assertTrue(self.created[0]["Close Sessions On End"])
        self.assertFalse(self.startup_input)
        self.assertFalse(self.created[0]["Send Code When Idle"])
        self.assertFalse(self.created[0]["Triggers"])
        self.assertNotIn("warning", result)
        self.assertTrue(result["focus_warning"])
        self.assertEqual(self.open()["status"], "opening")
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.open(["ttys987"])["status"], "attached")
        self.assertEqual(len(self.created), 1)

    def test_attached_iterm_gateway_in_buried_sessions_prevents_creation(self):
        self.app.buried_sessions = [self.session("existing-gateway", "/dev/ttys123")]
        result = self.open(["/dev/ttys123"])
        self.assertEqual(result["status"], "attached")
        self.assertEqual(result["session_id"], "existing-gateway")
        self.assertFalse(self.created)

    def test_unrelated_control_client_or_other_container_does_not_hide_missing_view(self):
        other_command = self.terminal.view_command("$18")
        self.app.buried_sessions = [self.session("other-container", "/dev/ttys124", other_command)]
        self.assertEqual(self.open(["/dev/ttys125"])["status"], "opening")
        self.assertEqual(len(self.created), 1)

    def test_non_tab_or_unknown_preference_warns_once_for_the_view(self):
        for preference in (0, 1, None):
            with self.subTest(preference=preference):
                self.app.terminal_windows.clear()
                self.state_path.unlink(missing_ok=True)
                self.sdk.preferences.async_get_preference.return_value = preference
                self.assertTrue(self.open()["warning"])
                self.assertNotIn("warning", self.open(["ttys987"]))

    def test_non_supervisor_socket_never_attempts_gui(self):
        with patch.object(tmux_terminal.subprocess, "run") as run:
            result = tmux_terminal.Terminal("supervisor-test").view(self.container)
        self.assertEqual(result["status"], "skipped")
        run.assert_not_called()

    def test_concurrent_opener_leaves_gui_to_the_lock_holder(self):
        with patch.dict(sys.modules, {"iterm2": self.sdk}), \
                patch.object(self.terminal, "view_lock", return_value=nullcontext(False)):
            result = asyncio.run(iterm_tab.open_tmux_view("connection", self.terminal, self.container))
        self.assertEqual(result["status"], "pending_elsewhere")
        self.sdk.async_get_app.assert_not_called()
        self.assertFalse(self.created)

    def test_chosen_window_number_is_remembered_by_identity_after_numbers_change(self):
        # Real SDK objects expose window_number, not the proto field number.
        # Mock only the RPC; a made-up window shape would hide accessor errors.
        chosen = iterm2.Window(None, "chosen-stable", [], None, 3)
        chosen.async_create_tab = AsyncMock()
        other = iterm2.Window(None, "other", [], None, 8)
        self.app.terminal_windows = [other, chosen]
        result = self.open(window_number=3)
        self.assertEqual(result["window_id"], chosen.window_id)
        self.assertEqual(json.loads(self.state_path.read_text())["iterm_window_id"], chosen.window_id)
        chosen.async_create_tab.assert_awaited_once()
        self.assertFalse(chosen.async_create_tab.call_args.kwargs["select"])
        chosen = iterm2.Window(None, chosen.window_id, [], None, 8)
        chosen.async_create_tab = AsyncMock()
        other = iterm2.Window(None, other.window_id, [], None, 3)
        self.app.terminal_windows = [chosen, other]
        self.assertEqual(self.open()["window_id"], chosen.window_id)
        chosen.async_create_tab.assert_awaited_once()
        self.sdk.Window.async_create.assert_not_awaited()

    def test_missing_remembered_window_never_falls_back_to_same_number_or_new_window(self):
        self.state_path.write_text(json.dumps({"iterm_window_id": "closed"}))
        with self.assertRaises(ValueError):
            self.open()
        self.assertFalse(self.created)
        with self.assertRaises(ValueError):
            self.open(window_number=3)

    def test_pending_gateway_deduplication_survives_a_different_tmux_path(self):
        self.open()
        with patch.object(tmux_terminal.shutil, "which", return_value="/alternate/bin/tmux"):
            result = self.open()
        self.assertEqual(result["status"], "opening")
        self.assertEqual(len(self.created), 1)

    def test_invalid_container_refuses_before_gui_connection(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        with patch.object(sys, "argv", ["iterm_tab.py", "--backend", "tmux", "view", "--window-id", "invalid"]), \
                patch.object(iterm2, "run_until_complete") as connect, \
                redirect_stderr(StringIO()), redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit) as caught:
                iterm_tab.main()
        self.assertNotEqual(caught.exception.code, 0)
        connect.assert_not_called()

    def test_gateway_ending_during_creation_reports_unavailable(self):
        self.sdk.Window.async_create.side_effect = None
        self.sdk.Window.async_create.return_value = None
        result = self.open()
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(result["readback_required"])
        self.assertTrue(result["next_action"])

    def test_window_memory_failure_preserves_created_view_identity(self):
        with patch.object(iterm_tab, "remember_view_window", side_effect=OSError("storage full")):
            result = self.open()
        self.assertEqual(result["status"], "opening")
        self.assertTrue(result["window_id"])
        self.assertTrue(result["window_memory_warning"])

    def test_writable_existing_view_refuses_without_creating_or_detaching(self):
        inventory = SimpleNamespace(stdout="$17\t1\t/dev/ttys987\t0\n")
        with patch.object(self.terminal, "verify_container"), \
                patch.object(self.terminal, "run", return_value=inventory) as run:
            with self.assertRaises(ValueError):
                self.terminal.control_clients(self.container)
        self.assertTrue(all(call.args[0] == "list-clients" for call in run.call_args_list))
        self.assertFalse(self.created)

    def test_unsupported_server_refuses_before_connecting_to_iterm(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        with patch.object(sys, "argv", ["iterm_tab.py", "--backend", "tmux", "view", "--window-id", "$0"]), \
                patch.object(tmux_terminal.Terminal, "verify_container"), \
                patch.object(tmux_terminal.Terminal, "readonly_control_supported", return_value=False), \
                patch.object(iterm2, "run_until_complete") as connect, \
                redirect_stderr(StringIO()), redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit):
                iterm_tab.main()
        connect.assert_not_called()

    def test_corrupt_window_memory_allows_an_explicit_new_choice(self):
        self.state_path.write_text("not json")
        with self.assertRaises(ValueError):
            self.open()
        self.assertFalse(self.created)
        chosen = iterm2.Window(None, "chosen-stable", [], None, 3)
        chosen.async_create_tab = AsyncMock()
        self.app.terminal_windows = [chosen]
        self.assertEqual(self.open(window_number=3)["window_id"], chosen.window_id)

    def test_launch_defers_unattached_view_without_any_gui_process(self):
        with patch.object(tmux_terminal.sys, "platform", "darwin"), \
                patch.object(self.terminal, "control_clients", return_value=[]), \
                patch.object(tmux_terminal.subprocess, "run") as run:
            result = self.terminal.view(self.container)
        self.assertEqual(result["status"], "deferred")
        self.assertTrue(result["readback_required"])
        retry = shlex.split(result["retry_command"])
        self.assertEqual(retry[retry.index("--backend") + 1], "tmux")
        self.assertEqual(retry[retry.index("--window-id") + 1], self.container)
        run.assert_not_called()

    def test_launch_with_attached_client_never_opens_another_gui(self):
        with patch.object(tmux_terminal.sys, "platform", "darwin"), \
                patch.object(self.terminal, "control_clients", return_value=["ttys-fixture"]), \
                patch.object(tmux_terminal.subprocess, "run") as run:
            self.assertEqual(self.terminal.view(self.container)["status"], "attached")
        run.assert_not_called()

    def test_launch_on_unsupported_server_reports_no_view_retry(self):
        with patch.object(tmux_terminal.sys, "platform", "darwin"), \
                patch.object(self.terminal, "readonly_control_supported", return_value=False):
            result = self.terminal.view(self.container)
        self.assertEqual(result["status"], "unsupported")
        self.assertNotIn("retry_command", result)


class PaneParserTests(unittest.TestCase):
    @staticmethod
    def pane_line(values):
        return "\t".join(values.get(field, "") for field in tmux_terminal.PANE_FIELDS) + "\n"

    def setUp(self):
        self.terminal = tmux_terminal.Terminal("fixture")
        capability = patch.object(self.terminal, "readonly_control_supported", return_value=True)
        capability.start()
        self.addCleanup(capability.stop)
        self.marker = "a" * 32
        common = {"session_id": "$1", "pid": "123"}
        self.dead = self.pane_line({**common, "window_id": "@1", "pane_id": "%1", "pane_title": "exited",
                                    "pane_tty": "/dev/ttys987", "pane_dead": "1", "pane_dead_status": "0",
                                    "@supervisor_identity": self.marker})
        self.live_fields = {**common, "window_id": "@2", "pane_id": "%2", "pane_title": "live",
                            "pane_tty": "/dev/ttys986", "pane_pid": "456", "pane_dead": "0",
                            "@supervisor_identity": "live-nonce"}
        self.live = self.pane_line(self.live_fields)
        self.identity = "tmux:fixture:" + self.marker

    def test_empty_dead_pid_does_not_block_inventory_read_or_live_send(self):
        def run(command, *args, **kwargs):
            if command == "list-panes":
                return SimpleNamespace(returncode=0, stdout=self.dead + self.live)
            return SimpleNamespace(returncode=0, stdout="❯ \x1b[2msuggestion\x1b[0m\n")

        with patch.object(self.terminal, "run", side_effect=run) as calls:
            rows = self.terminal.list()
            self.assertIsNone(rows[0]["pid"])
            self.assertTrue(rows[0]["dead"])
            self.assertEqual(self.terminal.read(rows[1]["session_id"])["input_line"]["style"], "dim")
            asyncio.run(self.terminal.send(rows[1]["session_id"], "hello", submit=False))
            calls.reset_mock()
            with self.assertRaises(ValueError):
                asyncio.run(self.terminal.send(self.identity, "must not be sent"))
            self.assertFalse(any(call.args[0] == "send-keys" for call in calls.call_args_list))

    def test_empty_live_pid_is_uncertain_inventory(self):
        with patch.object(self.terminal, "run", return_value=SimpleNamespace(
                returncode=0, stdout=self.pane_line({**self.live_fields, "pane_pid": ""}))):
            with self.assertRaises(ValueError):
                self.terminal.list()

    def test_empty_dead_pid_close_still_checks_ledger_pid_tty_and_siblings(self):
        entry = {"supervisor_owned": True, "tmux_session_id": self.identity, "pid": 789,
                 "tty": "ttys987", "window_id": "$1", "tab_id": "@1", "tmux_socket": "fixture"}
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / "ledger.json"
            ledger.write_text(json.dumps([entry]))
            with patch.object(self.terminal, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=self.dead + self.live)) as calls, \
                    patch.object(close_ttys, "inventory", return_value=[]):
                self.assertTrue(self.terminal.close(self.identity, ledger)["close_requested"])
            for processes in ([(789, "other-tty", "reused-pid")],
                              [(999, "ttys987", "tty-still-live")]):
                with self.subTest(processes=processes), \
                        patch.object(self.terminal, "run", return_value=SimpleNamespace(
                            returncode=0, stdout=self.dead)), \
                        patch.object(close_ttys, "inventory", return_value=processes):
                    with self.assertRaises(ValueError):
                        self.terminal.close(self.identity, ledger)

    def test_redraw_refusal_reports_typed_text_without_sending_return(self):
        row = {"pane_id": "%2", "dead": False, "title": "before"}
        with patch.object(self.terminal, "select", side_effect=[row, {**row, "title": "redrawn"}]), \
                patch.object(self.terminal, "run") as keys:
            with self.assertRaises(iterm_tab.SendFailure) as caught:
                asyncio.run(self.terminal.send("exact", "message"))
            self.assertTrue(caught.exception.detail["text_sent"])
            self.assertTrue(caught.exception.detail["input_may_contain_text"])
            self.assertEqual(caught.exception.detail["submission"], "not_attempted")
            self.assertTrue(keys.called)
            self.assertFalse(any(call.args[-1] == "Enter" for call in keys.call_args_list))

    def test_cli_refusal_is_json_on_stdout_failure_on_stderr_and_nonzero(self):
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO

        stdout, stderr = StringIO(), StringIO()
        argv = ["iterm_tab.py", "--backend", "tmux", "send", "--session-id", "exact",
                "--text", "message", "--verified-target"]
        with patch.object(sys, "argv", argv), \
                patch.object(tmux_terminal.Terminal, "send", side_effect=iterm_tab.SendFailure(
                    "exact", "not_attempted", "terminal state changed after text")), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                iterm_tab.main()
        self.assertNotEqual(caught.exception.code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["submission"], "not_attempted")
        self.assertTrue(stderr.getvalue().strip())

    def test_inventory_failure_after_typing_has_partial_send_receipt(self):
        row = {"pane_id": "%2", "dead": False}
        for error in (OSError("inventory failed"), subprocess.TimeoutExpired("tmux", 10)):
            with self.subTest(error=error), \
                    patch.object(self.terminal, "select", side_effect=[row, error]), \
                    patch.object(self.terminal, "run") as keys:
                with self.assertRaises(iterm_tab.SendFailure) as caught:
                    asyncio.run(self.terminal.send("exact", "message"))
                self.assertEqual(caught.exception.detail["submission"], "not_attempted")
                self.assertTrue(caught.exception.detail["text_sent"])
                self.assertTrue(keys.called)
                self.assertFalse(any(call.args[-1] == "Enter" for call in keys.call_args_list))

    def test_text_send_timeout_is_uncertain_and_never_sends_return(self):
        row = {"pane_id": "%2", "dead": False}
        with patch.object(self.terminal, "select", return_value=row), \
                patch.object(self.terminal, "run", side_effect=subprocess.TimeoutExpired("tmux", 10)) as keys:
            with self.assertRaises(iterm_tab.SendFailure) as caught:
                asyncio.run(self.terminal.send("exact", "message"))
            self.assertIsNone(caught.exception.detail["text_sent"])
            self.assertTrue(caught.exception.detail["input_may_contain_text"])
            self.assertEqual(caught.exception.detail["submission"], "not_attempted")
            self.assertFalse(any(call.args[-1] == "Enter" for call in keys.call_args_list))


@unittest.skipUnless(shutil.which("tmux") and Path("/bin/zsh").exists(), "requires tmux and zsh")
class TmuxTests(unittest.TestCase):
    def setUp(self):
        # Short private socket directory; Unix socket paths have a length limit.
        self.folder = tempfile.TemporaryDirectory(dir="/tmp", prefix="svtest-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        environment = patch.dict(os.environ, {"TMUX_TMPDIR": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        gui = patch.object(iterm2, "run_until_complete", side_effect=AssertionError("tests must never connect to real iTerm"))
        gui.start()
        self.addCleanup(gui.stop)
        self.socket = "supervisor-test-" + uuid.uuid4().hex
        self.terminal = tmux_terminal.Terminal(self.socket)
        # This is the only server these tests own; never use the live supervisor server.
        self.addCleanup(self.cleanup_server)
        window = self.operate("window")
        self.window = window["window_id"]
        if window["view_command"]:
            self.assertEqual(shlex.split(window["view_command"])[-1], self.window)
        else:
            self.assertFalse(self.terminal.readonly_control_supported(self.window))
        self.agent = self.root / "claude"
        self.agent.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
print("READY:" + json.dumps({"brief": sys.argv[-1], "home": os.getenv("CLAUDE_CONFIG_DIR"), "codex_home": os.getenv("CODEX_HOME"), "move": os.getenv("CLAUDE_ACCOUNT_MOVE_ENABLED"), "cwd": os.getcwd(), "code_home": os.getenv("CODE_HOME"), "uv_cache": os.getenv("UV_CACHE_DIR")}), flush=True)
for line in sys.stdin:
    if line.strip() in {"/exit", "/quit"}:
        break
    print("MESSAGE:" + repr(line), flush=True)
''')
        self.agent.chmod(0o700)
        self.command = self.root / "command"
        self.command.write_text(f"cd {shlex.quote(str(self.root))} && exec {shlex.quote(str(self.agent))}")
        self.brief = self.root / "brief"
        self.brief_text = "task's literal $(touch NEVER) `touch NEVER`\nnext\tline"
        self.brief.write_text(self.brief_text)
        self.choice = {"provider": "anthropic", "account_id": "anthropic-ab12",
                       "env": {"CLAUDE_CONFIG_DIR": str(self.root / "account")},
                       "storage_root": self.root}
        self.ledger = self.root / "ledger.json"

    def cleanup_server(self):
        self.terminal.run("kill-server", check=False)
        if sys.platform == "darwin":
            subprocess.run(["launchctl", "bootout", self.terminal.launchd_target()],
                           capture_output=True, timeout=10)
        # Only this fixture's private directory is eligible, never /tmp/tmux-UID.
        directory = self.root / f"tmux-{os.getuid()}"
        (directory / self.socket).unlink(missing_ok=True)
        for lock in directory.glob("supervisor-iterm-view-*.lock"):
            lock.unlink()

    @unittest.skipUnless(sys.platform == "darwin", "requires launchd")
    def test_server_and_worker_survive_killing_the_launching_terminal_process_group(self):
        self.cleanup_server()
        ready = self.root / "terminal-ready.json"
        code = ("import argparse,asyncio,json,pathlib,time,tmux_terminal; "
                f"r=asyncio.run(tmux_terminal.operate(argparse.Namespace(command='window',tmux_socket={self.socket!r}))); "
                f"pathlib.Path({str(ready)!r}).write_text(json.dumps(r)); time.sleep(30)")
        launcher = subprocess.Popen([sys.executable, "-c", code], cwd=Path(__file__).parent,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        self.addCleanup(launcher.stderr.close)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline and launcher.poll() is None:
                time.sleep(.05)
            self.assertTrue(ready.exists(), "simulated terminal did not create its container")
            self.window = json.loads(ready.read_text())["window_id"]
            row = self.launch()
            self.wait_screen(row["session_id"], "READY:")
            server = int(self.terminal.run("display-message", "-p", "-t", self.window, "#{pid}").stdout)
            parent = subprocess.run(["ps", "-o", "ppid=", "-p", str(server)],
                                    capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(int(parent), 1)
            os.killpg(launcher.pid, signal.SIGKILL)
            launcher.wait(timeout=5)
            self.assertFalse(self.terminal.select(row["session_id"])["dead"])
            self.assertEqual(int(self.terminal.run("display-message", "-p", "-t", self.window, "#{pid}").stdout), server)
            self.ended(row)
        finally:
            if launcher.poll() is None:
                os.killpg(launcher.pid, signal.SIGKILL)
                launcher.wait(timeout=5)

    def operate(self, command, **kwargs):
        return asyncio.run(tmux_terminal.operate(argparse.Namespace(
            command=command, tmux_socket=self.socket, **kwargs)))

    def launch(self, **kwargs):
        options = dict(window_id=self.window, command_file=[self.command], brief_file=[self.brief],
                       account_provider="anthropic", account_config=None, account=None)
        options.update(kwargs)
        with patch.object(account_choice, "select", return_value=self.choice):
            result = self.operate("new", **options)
        for row in result if isinstance(result, list) else [result]:
            # Linux can reuse a dead pane's PTY for another fixture immediately.
            # Keep this fixture's device allocated through its process checks.
            fd = os.open(row["tty"], os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
            self.addCleanup(os.close, fd)
        return result

    def wait_screen(self, identity, text):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.terminal.read(identity)
            if text in "\n".join(result["screen"]):
                return result
            time.sleep(0.05)
        self.fail(f"did not read {text!r} from fixture pane: {result['screen']!r}")

    def ended(self, row):
        asyncio.run(self.terminal.send(row["session_id"], "/quit"))
        deadline = time.monotonic() + 10
        last_error = None
        while time.monotonic() < deadline:
            if self.terminal.select(row["session_id"])["dead"]:
                # tmux's dead flag alone is not the shutdown procedure's
                # process-exit proof, especially while a Linux PTY is reaped.
                try:
                    self.terminal.verify_closed(row["session_id"], self.entry(row))
                except ValueError as error:
                    last_error = error
                else:
                    return
            time.sleep(0.05)
        self.fail(f"agent exit could not be verified: {last_error}")

    def entry(self, row):
        return {**row, "tmux_session_id": row["session_id"], "supervisor_owned": True}

    def test_launch_message_read_exit_and_close_with_account_and_literal_brief(self):
        row = self.launch()
        self.wait_screen(row["session_id"], "READY:")
        # Screen wrapping is unsuitable for literal argv evidence; capture full history joined.
        history = self.terminal.run("capture-pane", "-p", "-J", "-t", self.terminal.select(row["session_id"])["pane_id"]).stdout
        received = json.loads(next(line.split("READY:", 1)[1] for line in history.splitlines() if "READY:" in line))
        self.assertEqual(received["brief"], self.brief_text)
        self.assertEqual(received["home"], self.choice["env"]["CLAUDE_CONFIG_DIR"])
        self.assertEqual(received["move"], "1")
        self.assertEqual(Path(received["cwd"]).resolve(), self.root.resolve())
        self.assertFalse((self.root / "NEVER").exists())
        self.assertEqual(len(list((self.root / "Launch Receipts").glob("*.json"))), 1)
        self.operate("send", session_id=row["session_id"], text="hello 'quoted' $literal", text_file=None,
                     verified_target=True, no_submit=False)
        self.wait_screen(row["session_id"], "MESSAGE:")
        self.ended(row)
        self.ledger.write_text(json.dumps([self.entry(row)]))
        self.assertTrue(self.terminal.close(row["session_id"], self.ledger)["readback_required"])
        self.assertFalse(any(r["session_id"] == row["session_id"] for r in self.terminal.list()))
        self.assertTrue(any(r["window_id"] == self.window for r in self.terminal.list()))

    def test_new_window_preserves_launcher_context_and_cache_roots(self):
        environment = {"CODE_HOME": str(self.root / "code-context"), "UV_CACHE_DIR": str(self.root / "cache")}
        with patch.dict(os.environ, environment):
            row = self.launch()
        self.wait_screen(row["session_id"], "READY:")
        history = self.terminal.run("capture-pane", "-p", "-J", "-t", self.terminal.select(row["session_id"])["pane_id"]).stdout
        received = json.loads(next(line.split("READY:", 1)[1] for line in history.splitlines() if "READY:" in line))
        self.assertEqual(received["code_home"], environment["CODE_HOME"])
        self.assertEqual(received["uv_cache"], environment["UV_CACHE_DIR"])

    def test_read_faint_and_normal_prompt_from_real_tmux_capture(self):
        for styled, expected in (("\\033[2mcandidate\\033[0m", "dim"),
                                 ("\\033[22mcandidate", "non_dim"),
                                 ("\\033[7mc\\033[0m\\033[2mandidate\\033[0m", "dim"),
                                 ("\\033]8;;https://example.com\\033\\\\candidate\\033]8;;\\033\\\\", "non_dim")):
            with self.subTest(style=expected):
                pane = self.terminal.run("new-window", "-d", "-t", self.window + ":",
                                         "-P", "-F", "#{pane_id}",
                                         f"printf '❯ {styled}'; sleep 30").stdout.strip()
                row = next(r for r in self.terminal.list() if r["pane_id"] == pane)
                screen = self.wait_screen(row["session_id"], "candidate")
                self.assertEqual(screen["input_line"]["style"], expected)
                self.assertEqual(screen["input_line"]["text"], "candidate")
                self.assertEqual(screen["screen"], self.terminal.run("capture-pane", "-p", "-t", pane).stdout.splitlines())

    def test_refuses_unverified_input_and_live_or_foreign_close(self):
        row = self.launch()
        self.wait_screen(row["session_id"], "READY:")
        with self.assertRaisesRegex(ValueError, "verify"):
            self.operate("send", session_id=row["session_id"], text="wrong", text_file=None,
                         verified_target=False, no_submit=False)
        self.ledger.write_text(json.dumps([self.entry(row)]))
        with self.assertRaisesRegex(ValueError, "remains"):
            self.terminal.close(row["session_id"], self.ledger)
        self.ended(row)
        entry = self.entry(row)
        for change in ({"supervisor_owned": False}, {"tty": "different"}, {"window_id": "$999"},
                       {"tmux_socket": "different"}, {"pid": os.getpid()}):
            with self.subTest(change=change):
                self.ledger.write_text(json.dumps([{**entry, **change}]))
                with self.assertRaises(ValueError):
                    self.terminal.close(row["session_id"], self.ledger)
        self.assertTrue(self.terminal.select(row["session_id"])["dead"])

    def test_codex_uses_shared_daemon_preflight_and_selected_home(self):
        codex = self.root / "codex"
        self.agent.rename(codex)
        self.command.write_text(f"exec {shlex.quote(str(codex))} --remote unix://")
        self.choice = {**self.choice, "provider": "openai", "account_id": "openai-ab12",
                       "env": {"CODEX_HOME": str(self.root / "codex-account")}}
        before = self.terminal.list()
        with patch.object(iterm_tab, "check_codex_daemon", side_effect=ValueError("daemon unavailable")):
            with self.assertRaisesRegex(ValueError, "daemon"):
                self.launch(account_provider="openai")
        self.assertEqual(self.terminal.list(), before)
        with patch.object(iterm_tab, "check_codex_daemon") as daemon:
            row = self.launch(account_provider="openai")
            daemon.assert_called_once_with(self.choice)
        self.wait_screen(row["session_id"], "READY:")
        history = self.terminal.run("capture-pane", "-p", "-J", "-t", self.terminal.select(row["session_id"])["pane_id"]).stdout
        received = json.loads(next(line.split("READY:", 1)[1] for line in history.splitlines() if "READY:" in line))
        self.assertEqual(received["codex_home"], self.choice["env"]["CODEX_HOME"])
        self.assertEqual(received["brief"], self.brief_text)

    def test_early_worker_exit_reports_dead_pane_and_last_output(self):
        failure_status = 17
        # Exercise process exit directly, without an interpreter's teardown
        # closing its PTY before the process itself has finished.
        self.agent.write_text(f"#!{sys.executable}\nimport os\nprint('fixture bootstrap failed', flush=True)\nos._exit({failure_status})\n")
        with self.assertRaises(iterm_tab.LaunchFailure) as raised:
            self.launch()
        failed = raised.exception.detail["failed_launch"]
        self.assertEqual(raised.exception.detail["launched"], [])
        self.assertTrue(failed["receipt_written"])
        self.assertTrue(failed["startup"]["dead"])
        self.assertIn("fixture bootstrap failed", "\n".join(failed["startup"]["screen"]))
        native = self.terminal.read(failed["session_id"])
        self.assertTrue(native["dead"])
        # Some native servers expose neither cause even for a definite exit.
        # The launch must still fail and preserve all metadata they do expose.
        self.assertEqual(failed["startup"]["exit_status"], native["exit_status"])
        self.assertEqual(failed["startup"]["exit_signal"], native["exit_signal"])
        if native["exit_status"]:
            self.assertEqual(native["exit_status"], str(failure_status))

    def test_server_guard_preserves_a_pane_revived_during_close(self):
        row = self.launch()
        self.wait_screen(row["session_id"], "READY:")
        self.ended(row)
        self.ledger.write_text(json.dumps([self.entry(row)]))
        original = self.terminal.verify_closed
        checks = 0

        def revive(identity, entry):
            nonlocal checks
            verified = original(identity, entry)
            checks += 1
            if checks == 2:
                self.terminal.run("respawn-pane", "-t", verified["pane_id"], "sleep 30")
            return verified

        with patch.object(self.terminal, "verify_closed", side_effect=revive):
            result = self.terminal.close(row["session_id"], self.ledger)
        self.assertTrue(result["readback_required"])
        self.assertFalse(self.terminal.select(row["session_id"])["dead"])

    def test_exit_wait_requires_process_proof_after_tmux_reports_dead(self):
        row = self.launch()
        self.wait_screen(row["session_id"], "READY:")
        original = close_ttys.inventory
        reads = 0

        def reaping():
            nonlocal reads
            reads += 1
            if reads == 1:
                return [(row["pid"], row["tty"].removeprefix("/dev/"), "fixture-agent")]
            return original()

        with patch.object(close_ttys, "inventory", side_effect=reaping):
            self.ended(row)
        self.assertGreater(reads, 1)

    def test_split_linked_window_and_stale_identity_are_preserved(self):
        row = self.launch()
        self.wait_screen(row["session_id"], "READY:")
        self.ended(row)
        self.ledger.write_text(json.dumps([self.entry(row)]))
        pane = self.terminal.select(row["session_id"])["pane_id"]
        split = self.terminal.run("split-window", "-d", "-t", pane, "-P", "-F", "#{pane_id}").stdout.strip()
        with self.assertRaisesRegex(ValueError, "split"):
            self.terminal.close(row["session_id"], self.ledger)
        self.terminal.run("kill-pane", "-t", split)
        other = self.operate("window")["window_id"]
        self.terminal.run("link-window", "-s", row["tab_id"], "-t", other + ":")
        with self.assertRaises(ValueError):
            self.terminal.close(row["session_id"], self.ledger)
        self.terminal.run("unlink-window", "-t", other + ":1")
        self.terminal.run("set-option", "-p", "-t", pane, "@supervisor_identity", uuid.uuid4().hex)
        with self.assertRaisesRegex(ValueError, "exact"):
            asyncio.run(self.terminal.send(row["session_id"], "must not arrive"))

    def test_invalid_batch_refuses_before_panes_receipts_or_account_selection(self):
        before = self.terminal.list()
        self.brief.write_text("   ")
        with patch.object(account_choice, "select") as select:
            with self.assertRaisesRegex(ValueError, "brief"):
                self.operate("new", window_id=self.window, command_file=[self.command],
                             brief_file=[self.brief], account_provider="anthropic",
                             account_config=None, account=None)
            select.assert_not_called()
        self.assertEqual(before, self.terminal.list())
        self.assertFalse((self.root / "Launch Receipts").exists())

    def test_client_inventory_filters_plain_control_and_other_session_clients(self):
        # Real tmux -C is a protocol client, not an iTerm view (no client TTY).
        supported = self.terminal.readonly_control_supported(self.window)
        command = shlex.split(self.terminal.view_command(self.window))
        command[command.index("-CC")] = "-C"  # Pipes use the same protocol without a gateway TTY.
        client = subprocess.Popen(command,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.addCleanup(client.wait, timeout=5)
        self.addCleanup(client.stdin.close)
        self.addCleanup(client.stdout.close)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = self.terminal.run("list-clients", "-t", self.window, "-F", "#{client_control_mode}").stdout
            if "1" in rows:
                break
            time.sleep(0.05)
        else:
            self.fail("disposable protocol client did not attach")
        self.assertEqual(self.terminal.control_clients(self.window), [])
        self.assertEqual(self.terminal.control_clients(self.operate("window")["window_id"]), [])
        row = self.launch()
        self.assertEqual(row["view"]["status"], "skipped")
        self.wait_screen(row["session_id"], "READY:")
        pane = self.terminal.select(row["session_id"])["pane_id"]
        client.stdin.write(f"send-keys -t {pane} READONLYMUSTNOTARRIVE Enter\ndetach-client\n".encode())
        client.stdin.flush()
        client.wait(timeout=5)
        client.stdout.read()
        # The server may have input left to process after the client exits.
        asyncio.run(self.terminal.send(row["session_id"], "SETTLED"))
        screen = "\n".join(self.wait_screen(row["session_id"], "MESSAGE:'SETTLED" )["screen"])
        if supported:
            self.assertNotIn("READONLYMUSTNOTARRIVE", screen)
        else:
            # Older tmux does not enforce control-client read-only: the product
            # refuses GUI exposure and keeps the existing headless path usable.
            with self.assertRaises(ValueError):
                self.terminal.require_readonly_view(self.window)
        self.assertFalse(self.terminal.select(row["session_id"])["dead"])

    def test_parallel_view_lock_excludes_second_creator_and_recovers_after_exit(self):
        with self.terminal.view_lock(self.window) as first:
            with patch.dict(os.environ, {"TMPDIR": str(self.root)}), \
                    self.terminal.view_lock(self.window) as second:
                self.assertTrue(first)
                self.assertFalse(second)
            with self.terminal.view_lock(self.operate("window")["window_id"]) as other:
                self.assertTrue(other)
        with self.terminal.view_lock(self.window) as later:
            self.assertTrue(later)

    def test_one_view_receipt_per_batch_and_gui_failure_does_not_fail_launch(self):
        for failure in ({"status": "unavailable"}, OSError("view failed")):
            with self.subTest(failure=failure), patch.object(tmux_terminal.Terminal, "view") as view, \
                    patch.object(account_choice, "select_batch", return_value=[self.choice, self.choice]):
                if isinstance(failure, Exception):
                    view.side_effect = failure
                else:
                    view.return_value = failure
                rows = self.launch(command_file=[self.command, self.command], brief_file=[self.brief, self.brief])
                view.assert_called_once_with(self.window)
                self.assertEqual(rows[0]["view"]["status"], "unavailable")
                self.assertNotIn("view", rows[1])
                for row in rows:
                    self.wait_screen(row["session_id"], "READY:")

    def test_receipt_failure_retains_exact_staged_pane_without_starting_agent(self):
        with patch.object(account_choice, "record_launch", side_effect=OSError("storage full")):
            with self.assertRaises(iterm_tab.LaunchFailure) as caught:
                self.launch()
        detail = caught.exception.detail["failed_launch"]
        self.assertEqual(detail["submission"], "not_attempted")
        self.assertFalse(detail["receipt_written"])
        self.assertNotIn("READY:", "\n".join(self.terminal.read(detail["session_id"])["screen"]))
        self.assertFalse(self.terminal.select(detail["session_id"])["dead"])

    def test_batch_retains_completed_launch_and_failed_identity(self):
        count = 0
        original = account_choice.record_launch

        def receipt(choice):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("storage full")
            return original(choice)

        with patch.object(account_choice, "select_batch", return_value=[self.choice, self.choice]), \
                patch.object(account_choice, "record_launch", side_effect=receipt):
            with self.assertRaises(iterm_tab.LaunchFailure) as caught:
                self.launch(command_file=[self.command, self.command], brief_file=[self.brief, self.brief])
        detail = caught.exception.detail
        self.assertEqual(len(detail["launched"]), 1)
        self.wait_screen(detail["launched"][0]["session_id"], "READY:")
        self.assertNotEqual(detail["launched"][0]["session_id"], detail["failed_launch"]["session_id"])

    def test_cli_read_and_transcript_close_preview_without_iterm_connection(self):
        row = self.launch()
        self.wait_screen(row["session_id"], "READY:")
        scripts = Path(__file__).parent
        read = subprocess.run([sys.executable, str(scripts / "iterm_tab.py"), "--backend", "tmux",
                               "--tmux-socket", self.socket, "read", "--session-id", row["session_id"]],
                              check=True, capture_output=True, text=True, timeout=10)
        self.assertEqual(json.loads(read.stdout)["session_id"], row["session_id"])
        self.ended(row)
        native = str(uuid.uuid4())
        transcript = self.root / "transcript.jsonl"
        records = [{"type": "session_meta", "payload": {"id": native}},
                   {"type": "event_msg", "payload": {"type": "agent_message", "message": "Safe to exit: yes"}},
                   {"type": "event_msg", "payload": {"type": "task_complete"}}]
        transcript.write_text("".join(json.dumps(r) + "\n" for r in records))
        entry = {**self.entry(row), "session_id": native, "harness": "codex",
                 "transcript": str(transcript), "handoff_url": "https://github.com/example/repo/issues/1#issuecomment-2"}
        self.ledger.write_text(json.dumps([entry]))
        argv = [sys.executable, str(scripts / "close_ttys.py"), "--backend", "tmux", "--tmux-socket", self.socket,
                "--ledger", str(self.ledger), "--session-id", native, "--verified-handoff", "--verified-input"]
        preview = subprocess.run(argv, check=True, capture_output=True, text=True, timeout=10)
        self.assertTrue(json.loads(preview.stdout)["dry_run"])
        self.assertTrue(self.terminal.select(row["session_id"])["dead"])
        records[1]["payload"]["message"] = "still working"
        transcript.write_text("".join(json.dumps(r) + "\n" for r in records))
        refused = subprocess.run([*argv, "--apply"], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(refused.returncode, 0)
        self.assertTrue(self.terminal.select(row["session_id"])["dead"])
        records[1]["payload"]["message"] = "Safe to exit: yes"
        transcript.write_text("".join(json.dumps(r) + "\n" for r in records))
        applied = subprocess.run([*argv, "--apply"], check=True, capture_output=True, text=True, timeout=10)
        self.assertTrue(json.loads(applied.stdout)["closed"])


if __name__ == "__main__":
    unittest.main()
