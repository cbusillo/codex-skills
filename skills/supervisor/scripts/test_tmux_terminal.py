#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
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
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import account_choice
import close_ttys
import iterm_tab
import tmux_terminal


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.terminal = tmux_terminal.Terminal("supervisor")
        self.container = "$17"
        self.command = self.terminal.view_command(self.container)
        self.app = SimpleNamespace(terminal_windows=[], buried_sessions=[], async_refresh=AsyncMock())
        self.created = []

        class Profile:
            USE_CUSTOM_COMMAND_ENABLED = "custom"

            def __init__(self):
                self.values = {}

            def __getattr__(self, name):
                if name.startswith("set_"):
                    return lambda value: self.values.update({name.removeprefix("set_"): value})
                raise AttributeError(name)

        async def create(connection, *, profile_customizations):
            self.created.append(profile_customizations.values)
            session = self.session("gateway", "/dev/ttys987", profile_customizations.values["command"])
            window = SimpleNamespace(window_id="view-window", tabs=[SimpleNamespace(sessions=[session])])
            self.app.terminal_windows.append(window)
            return window

        self.sdk = SimpleNamespace(
            async_get_app=AsyncMock(return_value=self.app),
            preferences=SimpleNamespace(async_get_preference=AsyncMock(return_value=2),
                                        PreferenceKey=SimpleNamespace(OPEN_TMUX_WINDOWS_IN="restore")),
            LocalWriteOnlyProfile=Profile, Profile=Profile,
            Window=SimpleNamespace(async_create=AsyncMock(side_effect=create)),
        )

    @staticmethod
    def session(identity, tty, command="shell"):
        # No input methods: the creation path cannot accidentally type into it.
        return SimpleNamespace(session_id=identity, async_get_variable=AsyncMock(return_value=tty),
                               async_get_profile=AsyncMock(return_value=SimpleNamespace(command=command)))

    def open(self, clients=()):
        with patch.dict(sys.modules, {"iterm2": self.sdk}), \
                patch.object(self.terminal, "control_clients", return_value=clients), \
                patch.object(self.terminal, "view_lock", return_value=nullcontext(True)):
            return asyncio.run(iterm_tab.open_tmux_view("connection", self.terminal, self.container))

    def test_no_client_creates_gateway_and_pending_gateway_prevents_second_window(self):
        result = self.open()
        self.assertEqual(result["status"], "opening")
        self.assertEqual(shlex.split(self.created[0]["command"])[-1], self.container)
        self.assertEqual(shlex.split(self.created[0]["command"])[1:3], ["-L", self.terminal.socket_name])
        self.assertIn("-CC", shlex.split(self.created[0]["command"]))
        self.assertTrue(self.created[0]["close_sessions_on_end"])
        self.assertNotIn("warning", result)
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
        self.assertEqual(result["status"], "opening")
        self.sdk.async_get_app.assert_not_called()
        self.assertFalse(self.created)

    def test_gui_timeout_is_reported_without_retry_or_agent_input(self):
        with patch.object(tmux_terminal.sys, "platform", "darwin"), \
                patch.object(tmux_terminal.subprocess, "run", side_effect=subprocess.TimeoutExpired("view", 10)) as run:
            result = self.terminal.view(self.container)
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(result["readback_required"])
        run.assert_called_once()


class PaneParserTests(unittest.TestCase):
    def setUp(self):
        self.terminal = tmux_terminal.Terminal("fixture")
        self.marker = "a" * 32
        self.dead = f"$1\t@1\t%1\texited\t/dev/ttys987\t\t1\t0\t{self.marker}\t123\n"
        self.live = "$1\t@2\t%2\tlive\t/dev/ttys986\t456\t0\t\tlive-nonce\t123\n"
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
                returncode=0, stdout=self.live.replace("\t456\t0", "\t\t0"))):
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
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.socket = "supervisor-test-" + uuid.uuid4().hex
        self.terminal = tmux_terminal.Terminal(self.socket)
        # This is the only server these tests own; never use the live supervisor server.
        self.addCleanup(lambda: self.terminal.run("kill-server", check=False))
        window = self.operate("window")
        self.window = window["window_id"]
        self.assertEqual(shlex.split(window["view_command"])[-1], self.window)
        self.agent = self.root / "claude"
        self.agent.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
print("READY:" + json.dumps({"brief": sys.argv[-1], "home": os.getenv("CLAUDE_CONFIG_DIR"), "codex_home": os.getenv("CODEX_HOME"), "move": os.getenv("CLAUDE_ACCOUNT_MOVE_ENABLED"), "cwd": os.getcwd()}), flush=True)
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
        self.fail(f"did not read {text!r} from exact pane")

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
        client = subprocess.Popen(["tmux", "-L", self.socket, "-C", "attach", "-t", self.window],
                                  stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(client.wait, timeout=5)
        self.addCleanup(client.stdin.close)
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
        client.stdin.write(b"detach-client\n")
        client.stdin.flush()
        client.wait(timeout=5)
        self.assertFalse(self.terminal.select(row["session_id"])["dead"])

    def test_parallel_view_lock_excludes_second_creator_and_recovers_after_exit(self):
        with self.terminal.view_lock(self.window) as first:
            with self.terminal.view_lock(self.window) as second:
                self.assertTrue(first)
                self.assertFalse(second)
            with self.terminal.view_lock(self.operate("window")["window_id"]) as other:
                self.assertTrue(other)
        with self.terminal.view_lock(self.window) as later:
            self.assertTrue(later)

    def test_one_view_receipt_per_batch_and_gui_failure_does_not_fail_launch(self):
        with patch.object(tmux_terminal.Terminal, "view", return_value={"status": "unavailable"}) as view:
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
