#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets==17.2"]
# ///
"""Launcher admission and early-exit regressions; no live daemon or desktop."""

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from websockets.sync import client

import iterm_tab
import tmux_terminal

ADMISSION_OPEN = json.loads((Path(__file__).parent / "fixtures" / "codex-admission-open.json").read_text())["response"]
RPC_FAILURE = {"error": {"code": -1, "message": "server-payload-sentinel:private-native-detail"}}


class DaemonTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.opt = Path(folder.name) / "opt-in"
        opt_in = patch.object(iterm_tab, "CHROME_OPT_IN", self.opt)
        opt_in.start()
        self.addCleanup(opt_in.stop)

    def test_captured_native_plugin_inventory_passes_required_browser_check(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/codex-plugin-list-local.json").read_text())["response"]
        choice = {"provider": "openai", "env": {"CODEX_HOME": "/fixture/account"},
                  "require_browser": True, "tool_directory": "/fixture/project"}
        with (patch.object(iterm_tab.subprocess, "run", return_value=SimpleNamespace(returncode=0)),
              patch.object(iterm_tab.socket, "socket"),
              patch.object(iterm_tab, "inspect_codex_daemon", return_value=fixture)):
            iterm_tab.check_codex_daemon(choice)
        self.assertEqual(choice["tools"]["warnings"], [])

    def inspect(self, *, draining=False, pages=None, states=None, probe=None, fail=None,
                directory=None, inventory_latency=0):
        pages = pages or {None: {"data": ["detached-worker"], "nextCursor": None}}
        states = states or {"detached-worker": "active"}
        calls = []
        pending = []
        now = 0
        ws = Mock()

        def send(raw):
            nonlocal now
            message = json.loads(raw)
            calls.append(message)
            if "id" not in message:
                return
            method, params = message["method"], message["params"]
            if method in {"thread/loaded/list", "thread/read"}:
                now += inventory_latency
            if method == fail:
                response = RPC_FAILURE
            elif method == "thread/loaded/list":
                response = {"result": pages[params["cursor"]]}
            elif method == "thread/read":
                response = {"result": {"thread": {"status": {"type": states[params["threadId"]]}}}}
            elif method == "thread/resume":
                response = probe if probe is not None else ({"error": {
                    "code": -32600, "message": "Server is draining; retry after reconnecting"}}
                    if draining else {key: value for key, value in ADMISSION_OPEN.items() if key != "id"})
            else:
                response = {"result": {}}
            pending.append(json.dumps({"id": message["id"], **response}))

        ws.send.side_effect = send
        ws.recv.side_effect = lambda **_: pending.pop(0)
        with (patch.object(client, "unix_connect") as connect,
              patch.object(iterm_tab.time, "monotonic", side_effect=lambda: now)):
            connect.return_value.__enter__.return_value = ws
            inventory = iterm_tab.inspect_codex_daemon(Path("/fixture/account"), directory=directory)
        self.inventory = inventory
        return calls

    def test_healthy_busy_daemon_is_reused_without_loading_or_creating_a_thread(self):
        calls = self.inspect()
        resume = next(c for c in calls if c["method"] == "thread/resume")
        # Invalid native identity cannot resume anybody else's real thread.
        import uuid
        with self.assertRaises(ValueError):
            uuid.UUID(resume["params"]["threadId"])
        self.assertFalse(any(c["method"] in {"thread/start", "turn/start", "turn/interrupt"} for c in calls))
        self.assertFalse(any(c["method"] == "plugin/list" for c in calls))

    def test_optional_inventory_uses_target_project_and_reports_its_own_failure(self):
        calls = self.inspect(fail="plugin/list", directory="/fixture/project")
        inventory = next(c for c in calls if c["method"] == "plugin/list")
        self.assertEqual(inventory["params"]["cwds"], ["/fixture/project"])
        self.assertTrue(self.inventory["inventory_error"])

    def test_source_daemon_skips_plugins_and_required_browser_refuses_missing_plugins(self):
        self.opt.touch()
        for required in (False, True):
            choice = {"provider": "openai", "env": {"CODEX_HOME": "/fixture/account"},
                      "require_browser": required, "tool_directory": "/fixture/project"}
            with (patch.object(iterm_tab.subprocess, "run", return_value=SimpleNamespace(returncode=0)),
                  patch.object(iterm_tab.socket, "socket"),
                  patch.object(iterm_tab, "inspect_codex_daemon", return_value={"marketplaces": []}) as inspect):
                if required:
                    with self.assertRaises(ValueError):
                        iterm_tab.check_codex_daemon(choice)
                else:
                    iterm_tab.check_codex_daemon(choice)
                    self.assertEqual(choice.get("tools", {}).get("warnings", []), [])
                    inspect.assert_called_once_with(Path("/fixture/account"), directory=None)

    def test_open_admission_does_not_read_unrelated_loaded_threads(self):
        for directory in (None, "/fixture/project"):
            with self.subTest(directory=directory):
                calls = self.inspect(directory=directory)
                self.assertFalse(any(c["method"] in {"thread/loaded/list", "thread/read"} for c in calls))
                self.assertEqual(any(c["method"] == "plugin/list" for c in calls), directory is not None)

    def test_draining_daemon_with_detached_active_worker_refuses(self):
        with self.assertRaises(iterm_tab.DaemonRefusal) as raised:
            self.inspect(draining=True)
        self.assertEqual(raised.exception.state, "draining")
        self.assertEqual(raised.exception.active_threads, 1)

    def test_draining_daemon_with_no_active_threads_still_refuses(self):
        with self.assertRaises(iterm_tab.DaemonRefusal) as raised:
            self.inspect(draining=True, states={"detached-worker": "idle"})
        self.assertEqual(raised.exception.active_threads, 0)

    def test_active_threads_are_counted_across_all_pages(self):
        pages = {None: {"data": ["idle"], "nextCursor": "page2"},
                 "page2": {"data": ["running"], "nextCursor": None}}
        with self.assertRaises(iterm_tab.DaemonRefusal) as raised:
            self.inspect(draining=True, pages=pages, states={"idle": "idle", "running": "active"})
        self.assertEqual(raised.exception.active_threads, 1)

    def test_draining_inventory_failure_stays_draining_with_unknown_count(self):
        for kwargs in ({"fail": "thread/loaded/list"}, {"fail": "thread/read"},
                       {"states": {"detached-worker": "future-state"}},
                       {"inventory_latency": 10**6},
                       {"pages": {None: {"data": None}}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(iterm_tab.DaemonRefusal) as raised:
                self.inspect(draining=True, **kwargs)
            self.assertEqual(raised.exception.state, "draining")
            self.assertIsNone(raised.exception.active_threads)
            self.assertIsNotNone(raised.exception.inventory_error)

    def test_partial_inventory_never_reports_a_complete_active_count(self):
        pages = {None: {"data": ["running"], "nextCursor": "next"},
                 "next": {"data": None}}
        with self.assertRaises(iterm_tab.DaemonRefusal) as raised:
            self.inspect(draining=True, pages=pages, states={"running": "active"})
        self.assertEqual(raised.exception.state, "draining")
        self.assertIsNone(raised.exception.active_threads)

    def test_unknown_or_malformed_admission_refuses(self):
        for probe in ({"error": {"code": -32600, "message": "different refusal"}},
                      {"result": {}}, {"error": None}, {"error": {"message": []}}):
            with self.subTest(probe=probe), self.assertRaises(iterm_tab.DaemonRefusal) as raised:
                self.inspect(probe=probe)
            self.assertEqual(raised.exception.state, "unknown")
            self.assertIsNone(raised.exception.active_threads)

    def test_draining_read_failure_retains_rpc_code_without_server_payload(self):
        with self.assertRaises(iterm_tab.DaemonRefusal) as raised:
            self.inspect(draining=True, fail="thread/read")
        self.assertIn("thread/read", raised.exception.inventory_error)
        self.assertIn(str(RPC_FAILURE["error"]["code"]), raised.exception.inventory_error)
        self.assertNotIn(RPC_FAILURE["error"]["message"], raised.exception.inventory_error)
        clauses = str(raised.exception).split("; ")
        self.assertEqual(len(clauses), len(set(clauses)))

    def test_admission_timeout_names_the_method_without_lifecycle_mutation(self):
        ws = Mock()
        ws.recv.side_effect = [json.dumps({"id": 1, "result": {}}), TimeoutError]
        with (patch.object(client, "unix_connect") as connect,
              patch.object(iterm_tab.subprocess, "run") as run):
            connect.return_value.__enter__.return_value = ws
            with self.assertRaisesRegex(ValueError, "thread/resume") as raised:
                iterm_tab.inspect_codex_daemon(Path("/fixture/account"))
            self.assertIsInstance(raised.exception.__cause__, TimeoutError)
            run.assert_not_called()

    def test_socket_timeout_refuses_without_lifecycle_mutation(self):
        with (patch.object(client, "unix_connect", side_effect=TimeoutError),
              patch.object(iterm_tab.subprocess, "run") as run):
            with self.assertRaises(TimeoutError):
                iterm_tab.inspect_codex_daemon(Path("/fixture/account"))
            run.assert_not_called()

    def test_transport_timeout_refuses_for_selected_home_without_restart(self):
        choice = {"provider": "openai", "env": {"CODEX_HOME": "/fixture/account"}}
        with (patch.object(iterm_tab.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run,
              patch.object(iterm_tab.socket, "socket"),
              patch.object(iterm_tab, "inspect_codex_daemon", side_effect=TimeoutError)):
            with self.assertRaises(ValueError) as raised:
                iterm_tab.check_codex_daemon(choice)
            self.assertIsInstance(raised.exception.__cause__, TimeoutError)
            self.assertIn(choice["env"]["CODEX_HOME"], str(raised.exception))
            self.assertEqual([c.args[0][-1] for c in run.call_args_list], ["start"])

    def test_draining_preflight_never_restarts_and_refuses_before_receipt(self):
        choice = {"provider": "openai", "env": {"CODEX_HOME": "/fixture/account"}}
        with (patch.object(iterm_tab.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run,
              patch.object(iterm_tab.socket, "socket") as sock,
              patch.object(iterm_tab, "inspect_codex_daemon", side_effect=ValueError("draining; 1 active thread")),
              patch.object(iterm_tab.account_choice, "record_launch") as receipt):
            with self.assertRaises(ValueError):
                iterm_tab.check_codex_daemon(choice)
            self.assertEqual([c.args[0][-1] for c in run.call_args_list], ["start"])
            receipt.assert_not_called()
            sock.return_value.__exit__.assert_called_once()


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_hook_review_is_blocked_even_though_terminal_is_alive(self):
        snapshot = {"dead": False, "screen": [
            "2 hooks are new or changed", "Source: /fixture/account/hooks.json",
            "› 1. Trust all and continue", "  2. Continue without trusting",
        ]}
        with self.assertRaises(iterm_tab.StartupFailure) as raised:
            await iterm_tab.verify_startup(AsyncMock(return_value=snapshot), timeout=0)
        self.assertEqual(raised.exception.snapshot["blocked_prompt"], "hook_trust")
        self.assertFalse(raised.exception.snapshot["dead"])
        self.assertIn("hook_trust", str(raised.exception))
        failure = iterm_tab.LaunchFailure([], {"session_id": "fixture"}, raised.exception)
        self.assertEqual(failure.detail["status"], "launch_blocked")
        self.assertEqual(failure.detail["failed_launch"]["startup"]["screen"], snapshot["screen"])

    async def test_bordered_hook_review_frame_is_blocked(self):
        snapshot = {"dead": False, "screen": [
            "╭──────────────────────────────╮", "│ 2 hooks are new or changed   │",
            "│ › 1. Trust all and continue  │", "│   2. Continue without trusting │",
        ]}
        with self.assertRaises(iterm_tab.StartupFailure) as raised:
            await iterm_tab.verify_startup(AsyncMock(return_value=snapshot), timeout=0)
        self.assertEqual(raised.exception.snapshot["blocked_prompt"], "hook_trust")

    async def test_codex_hook_review_after_short_check_still_blocks_launch(self):
        # Codex draws its first screen seconds after exec; the prompt must not
        # be missed because the immediate-exit check has already passed.
        row = {"window_id": "$1", "tab_id": "@1", "pane_id": "%1", "tty": "fixture",
               "pid": 1, "backend": "tmux", "tmux_socket": "fixture"}
        terminal = Mock()
        terminal.run.return_value.stdout = "@1\t%1"
        terminal.select.return_value = row
        terminal.send = AsyncMock()
        starting = {"dead": False, "exit_status": "", "exit_signal": "", "screen": []}
        prompt = {**starting, "screen": ["› 1. Trust all and continue", "2. Continue without trusting"]}
        reads = 0

        def prompt_after_three_reads(_):
            nonlocal reads
            reads += 1
            return prompt if reads > 3 else starting

        terminal.read.side_effect = prompt_after_three_reads
        args = argparse.Namespace(command="new", tmux_socket="fixture", window_id="$1")
        choice = {"provider": "openai", "env": {}}
        original = iterm_tab.verify_startup

        async def observe(read, **options):
            # Without the short-check window, only the Codex prompt watch remains.
            return await original(read, timeout=0, **options)

        with (patch.object(tmux_terminal, "Terminal", return_value=terminal),
              patch.object(iterm_tab, "prepare_launches", return_value=(["agent"], [choice])),
              patch.object(iterm_tab.account_choice, "public", return_value={}),
              patch.object(iterm_tab.account_choice, "record_launch"),
              patch.object(iterm_tab, "verify_startup", side_effect=observe)):
            with self.assertRaises(iterm_tab.LaunchFailure) as raised:
                await tmux_terminal.operate(args)
        self.assertEqual(raised.exception.detail["status"], "launch_blocked")

    async def test_running_turn_ends_codex_prompt_watch(self):
        snapshot = {"dead": False, "screen": ["• Working (0s • esc to interrupt)"]}
        read = AsyncMock(return_value=snapshot)
        await asyncio.wait_for(iterm_tab.verify_startup(read, timeout=0, prompt_window=60), 5)
        read.assert_awaited_once()

    async def test_echoed_brief_quoting_status_row_does_not_end_prompt_watch(self):
        # iTerm echoes the submitted command, brief included, before Codex draws.
        submitted = "codex -- 'Find why esc to interrupt shows\n• Working (0s • esc to interrupt)\nearly'"
        echo = ["% codex -- 'Find why esc to interrupt shows", "• Working (0s • esc to interrupt)", "early'"]
        prompt = [*echo, "› 1. Trust all and continue", "  2. Continue without trusting"]
        read = AsyncMock(side_effect=[{"dead": False, "screen": echo}] * 3 + [{"dead": False, "screen": prompt}])
        with self.assertRaises(iterm_tab.StartupFailure) as raised:
            await iterm_tab.verify_startup(read, timeout=0, prompt_window=5, submitted=submitted)
        self.assertEqual(raised.exception.snapshot["blocked_prompt"], "hook_trust")

    async def test_death_during_prompt_watch_still_collects_exit_status(self):
        read = AsyncMock(side_effect=[{"dead": False, "screen": []},
                                      {"dead": True, "exit_status": "", "screen": ["last output"]},
                                      {"dead": True, "exit_status": "1", "screen": ["last output"]}])
        with self.assertRaises(iterm_tab.StartupFailure) as raised:
            await iterm_tab.verify_startup(read, timeout=0.05, prompt_window=5)
        self.assertEqual(raised.exception.snapshot["exit_status"], "1")

    async def test_brief_mentioning_hook_trust_without_review_choices_passes(self):
        snapshot = {"dead": False, "screen": [
            "Fix 2 hooks are new or changed launch detection",
            'The report quotes "Trust all and continue" and "Continue without trusting".',
            "› working on issue 2018",
        ]}
        self.assertIsNone(await iterm_tab.verify_startup(AsyncMock(return_value=snapshot), timeout=0))

    async def test_dead_pane_reports_exit_status_and_last_output(self):
        snapshot = {"dead": True, "exit_status": "1", "screen": ["bootstrap failed"]}
        with self.assertRaises(iterm_tab.StartupFailure) as raised:
            await iterm_tab.verify_startup(AsyncMock(return_value=snapshot), timeout=0)
        self.assertEqual(raised.exception.snapshot, snapshot)

    async def test_live_agent_brief_quoting_draining_error_is_not_a_failure(self):
        snapshot = {"dead": False, "screen": [
            "Investigate Error: Server is draining; retry after reconnecting",
            "Failed to start a fresh session through the app server",
        ]}
        await iterm_tab.verify_startup(AsyncMock(return_value=snapshot), timeout=0)

    async def test_exit_during_observation_is_detected(self):
        read = AsyncMock(side_effect=[{"dead": False, "screen": []},
                                      {"dead": True, "screen": ["died later"]}])
        with patch.object(iterm_tab.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaises(iterm_tab.StartupFailure):
                await iterm_tab.verify_startup(read)
        self.assertEqual(read.await_count, 2)

    async def test_dead_pane_collects_exit_status_after_pty_eof(self):
        ready = {"dead": True, "exit_status": "1", "screen": ["fixture failure"]}
        read = AsyncMock(side_effect=[{**ready, "exit_status": ""}, ready])
        with patch.object(iterm_tab.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaises(iterm_tab.StartupFailure) as raised:
                await iterm_tab.verify_startup(read)
        self.assertEqual(raised.exception.snapshot, ready)
        self.assertEqual(read.await_count, 2)

    async def test_dead_pane_without_exit_status_still_fails_at_deadline(self):
        read = AsyncMock(return_value={"dead": True, "exit_status": "", "screen": ["fixture failure"]})
        with self.assertRaises(iterm_tab.StartupFailure):
            await iterm_tab.verify_startup(read, timeout=0)

    async def test_signal_death_reports_cause_without_waiting_for_exit_code(self):
        snapshot = {"dead": True, "exit_status": "", "exit_signal": "TERM", "screen": ["last output"]}
        with patch.object(iterm_tab.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(iterm_tab.StartupFailure) as raised:
                await iterm_tab.verify_startup(AsyncMock(return_value=snapshot))
        self.assertEqual(raised.exception.snapshot, snapshot)
        sleep.assert_not_awaited()

    async def test_later_read_failure_preserves_confirmed_death_and_output(self):
        snapshot = {"dead": True, "exit_status": "", "screen": ["last output"]}
        read = AsyncMock(side_effect=[snapshot, ValueError("pane unavailable")])
        with patch.object(iterm_tab.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaises(iterm_tab.StartupFailure) as raised:
                await iterm_tab.verify_startup(read)
        self.assertTrue(raised.exception.snapshot["dead"])
        self.assertEqual(raised.exception.snapshot["screen"], snapshot["screen"])
        self.assertIn("observation_error", raised.exception.snapshot)

    async def test_alive_after_observation_passes(self):
        await iterm_tab.verify_startup(AsyncMock(return_value={"dead": False, "screen": ["ready"]}), timeout=0)

    async def test_tmux_failed_launch_retains_receipt_identity_and_output(self):
        row = {"window_id": "$1", "tab_id": "@1", "pane_id": "%1", "tty": "fixture",
               "pid": 1, "backend": "tmux", "tmux_socket": "fixture"}
        terminal = Mock()
        terminal.run.return_value.stdout = "@1\t%1"
        terminal.select.return_value = row
        terminal.send = AsyncMock()
        terminal.read.return_value = {"dead": True, "exit_status": "1", "exit_signal": "", "screen": ["thread/start failed"]}
        args = argparse.Namespace(command="new", tmux_socket="fixture", window_id="$1")
        choice = {"provider": "openai", "env": {}}
        with (patch.object(tmux_terminal, "Terminal", return_value=terminal),
              patch.object(iterm_tab, "prepare_launches", return_value=(["fake-agent"], [choice])),
              patch.object(iterm_tab.account_choice, "public", return_value={}),
              patch.object(iterm_tab.account_choice, "record_launch") as receipt):
            with self.assertRaises(iterm_tab.LaunchFailure) as raised:
                await tmux_terminal.operate(args)
        detail = raised.exception.detail
        self.assertEqual(detail["launched"], [])
        self.assertTrue(detail["failed_launch"]["receipt_written"])
        self.assertEqual(detail["failed_launch"]["startup"]["exit_status"], "1")
        self.assertEqual(detail["failed_launch"]["startup"]["screen"], ["thread/start failed"])
        self.assertEqual(detail["failed_launch"]["submission"], "submitted")
        receipt.assert_called_once_with(choice)
        terminal.view.assert_not_called()

    async def test_tmux_hook_review_stops_batch_after_first_submitted_worker(self):
        row = {"window_id": "$1", "tab_id": "@1", "pane_id": "%1", "tty": "fixture",
               "pid": 1, "backend": "tmux", "tmux_socket": "fixture"}
        terminal = Mock()
        terminal.run.return_value.stdout = "@1\t%1"
        terminal.select.return_value = row
        terminal.send = AsyncMock()
        terminal.read.return_value = {"dead": False, "exit_status": "", "exit_signal": "",
                                     "screen": ["› 1. Trust all and continue", "2. Continue without trusting"]}
        args = argparse.Namespace(command="new", tmux_socket="fixture", window_id="$1")
        choice = {"provider": "openai", "env": {}}
        with (patch.object(tmux_terminal, "Terminal", return_value=terminal),
              patch.object(iterm_tab, "prepare_launches", return_value=(["agent1", "agent2"], [choice, choice])),
              patch.object(iterm_tab.account_choice, "public", return_value={}),
              patch.object(iterm_tab.account_choice, "record_launch") as receipt):
            with self.assertRaises(iterm_tab.LaunchFailure) as raised:
                await tmux_terminal.operate(args)
        self.assertEqual(raised.exception.detail["status"], "launch_blocked")
        self.assertEqual(raised.exception.detail["launched"], [])
        self.assertTrue(raised.exception.detail["failed_launch"]["receipt_written"])
        self.assertEqual(raised.exception.detail["failed_launch"]["submission"], "submitted")
        self.assertIn("session_id", raised.exception.detail["failed_launch"])
        receipt.assert_called_once_with(choice)
        terminal.send.assert_awaited_once()

    async def test_iterm_uses_termination_monitor_without_a_desktop_connection(self):
        queue = asyncio.Queue()
        monitor = SimpleNamespace(async_get=queue.get)
        context = Mock()
        context.__aenter__ = AsyncMock(return_value=monitor)
        context.__aexit__ = AsyncMock(return_value=False)
        screen = SimpleNamespace(number_of_lines=1, line=lambda i: SimpleNamespace(string="last output"))
        session = SimpleNamespace(session_id="exact", async_get_screen_contents=AsyncMock(return_value=screen))
        app = SimpleNamespace(connection=object(), async_refresh=AsyncMock(),
                              terminal_windows=[SimpleNamespace(tabs=[SimpleNamespace(sessions=[session])])])

        async def send(*args, **kwargs):
            queue.put_nowait("unrelated")
            queue.put_nowait(session.session_id)
            await asyncio.sleep(0)

        with (patch.dict("sys.modules", {"iterm2": SimpleNamespace(SessionTerminationMonitor=Mock(return_value=context))}),
              patch.object(iterm_tab, "send", side_effect=send)):
            with self.assertRaises(iterm_tab.StartupFailure) as raised:
                await iterm_tab.submit_iterm_launch(app, session, "fake-agent")
        self.assertEqual(raised.exception.snapshot["screen"], ["last output"])
        context.__aexit__.assert_awaited_once()

    async def test_iterm_agent_exit_replaces_interactive_shell_without_exec_in_file(self):
        queue = asyncio.Queue()
        context = Mock()
        context.__aenter__ = AsyncMock(return_value=SimpleNamespace(async_get=queue.get))
        context.__aexit__ = AsyncMock(return_value=False)
        output = []
        screen = SimpleNamespace(number_of_lines=1, line=lambda i: SimpleNamespace(string=output[0]))
        session = SimpleNamespace(session_id="exact", async_get_screen_contents=AsyncMock(return_value=screen))
        app = SimpleNamespace(connection=object(), async_refresh=AsyncMock(),
                              terminal_windows=[SimpleNamespace(tabs=[SimpleNamespace(sessions=[session])])])

        async def send(target, text, **kwargs):
            # The trailing command would run if the interactive shell returned
            # after the worker exited. -f excludes the host's startup files.
            result = subprocess.run(["/bin/zsh", "-fic", text + "; printf SHELL_RESUMED"],
                                    capture_output=True, text=True, timeout=2,
                                    env={**os.environ, "ZDOTDIR": folder})
            self.assertEqual(result.returncode, 7)
            output.append(result.stdout.strip())
            queue.put_nowait(target.session_id)
            await asyncio.sleep(0)

        with tempfile.TemporaryDirectory() as folder:
            agent = Path(folder) / "fixture agent"
            agent.write_text('#!/bin/sh\nprintf "%s\\n" "$1"\nexit 7\n')
            agent.chmod(0o700)
            # No exec: the launcher must still replace the shell and see exit.
            command = shlex.join([str(agent), "brief mentioning Server is draining"])
            with (patch.dict("sys.modules", {"iterm2": SimpleNamespace(SessionTerminationMonitor=Mock(return_value=context))}),
                  patch.object(iterm_tab, "send", side_effect=send)):
                with self.assertRaises(iterm_tab.StartupFailure):
                    await iterm_tab.submit_iterm_launch(app, session, command)
        self.assertEqual(output, ["brief mentioning Server is draining"])

    async def test_unrelated_termination_does_not_fail_live_iterm_launch(self):
        queue = asyncio.Queue()
        queue.put_nowait("unrelated")
        monitor = SimpleNamespace(async_get=queue.get)
        context = Mock()
        context.__aenter__ = AsyncMock(return_value=monitor)
        context.__aexit__ = AsyncMock(return_value=False)
        screen = SimpleNamespace(number_of_lines=0)
        session = SimpleNamespace(session_id="exact", async_get_screen_contents=AsyncMock(return_value=screen))
        app = SimpleNamespace(connection=object(), async_refresh=AsyncMock(),
                              terminal_windows=[SimpleNamespace(tabs=[SimpleNamespace(sessions=[session])])])
        original = iterm_tab.verify_startup

        async def observe(read, **options):
            await asyncio.sleep(0)
            return await original(read, timeout=0, **options)

        with (patch.dict("sys.modules", {"iterm2": SimpleNamespace(SessionTerminationMonitor=Mock(return_value=context))}),
              patch.object(iterm_tab, "send", new_callable=AsyncMock),
              patch.object(iterm_tab, "verify_startup", side_effect=observe)):
            await iterm_tab.submit_iterm_launch(app, session, "fake-agent")

    async def test_iterm_echo_of_brief_ending_in_status_row_does_not_end_prompt_watch(self):
        queue = asyncio.Queue()
        context = Mock()
        context.__aenter__ = AsyncMock(return_value=SimpleNamespace(async_get=queue.get))
        context.__aexit__ = AsyncMock(return_value=False)
        brief = "Check why the launcher saw this row:\n• Working (0s • esc to interrupt)"
        command = "codex -C /tmp -- " + shlex.quote(brief)
        # Captured from zsh at 60 columns after a bracketed paste of the launch text.
        echo = ["% exec /bin/zsh -c 'codex -C /tmp -- '\"'\"'Check why the laun",
                "cher saw this row:",
                "• Working (0s • esc to interrupt)'\"'\"''"]
        prompt = [*echo, "› 1. Trust all and continue", "  2. Continue without trusting"]

        def contents(lines):
            return SimpleNamespace(number_of_lines=len(lines), line=lambda i: SimpleNamespace(string=lines[i]))

        session = SimpleNamespace(session_id="exact", async_get_screen_contents=AsyncMock(
            side_effect=[contents(echo)] * 3 + [contents(prompt)]))
        app = SimpleNamespace(connection=object(), async_refresh=AsyncMock(),
                              terminal_windows=[SimpleNamespace(tabs=[SimpleNamespace(sessions=[session])])])
        original = iterm_tab.verify_startup

        async def observe(read, **options):
            return await original(read, timeout=0, **options)

        with (patch.dict("sys.modules", {"iterm2": SimpleNamespace(SessionTerminationMonitor=Mock(return_value=context))}),
              patch.object(iterm_tab, "send", new_callable=AsyncMock),
              patch.object(iterm_tab, "verify_startup", side_effect=observe)):
            with self.assertRaises(iterm_tab.StartupFailure) as raised:
                await iterm_tab.submit_iterm_launch(app, session, command, prompt_window=5)
        self.assertEqual(raised.exception.snapshot["blocked_prompt"], "hook_trust")

    async def test_failed_iterm_monitor_reports_unknown_and_never_dead(self):
        context = Mock()
        context.__aenter__ = AsyncMock(return_value=SimpleNamespace(async_get=AsyncMock(side_effect=OSError)))
        context.__aexit__ = AsyncMock(return_value=False)
        session = SimpleNamespace(session_id="exact")
        app = SimpleNamespace(connection=object(), async_refresh=AsyncMock(),
                              terminal_windows=[SimpleNamespace(tabs=[SimpleNamespace(sessions=[session])])])

        async def send(*args, **kwargs):
            await asyncio.sleep(0)

        with (patch.dict("sys.modules", {"iterm2": SimpleNamespace(SessionTerminationMonitor=Mock(return_value=context))}),
              patch.object(iterm_tab, "send", side_effect=send)):
            with self.assertRaises(iterm_tab.StartupFailure) as raised:
                await iterm_tab.submit_iterm_launch(app, session, "fake-agent")
        self.assertIsNone(raised.exception.snapshot["dead"])


if __name__ == "__main__":
    unittest.main()
