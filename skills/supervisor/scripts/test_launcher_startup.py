#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets==17.2"]
# ///
"""Launcher admission and early-exit regressions; no live daemon or desktop."""

import argparse
import asyncio
import json
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


class DaemonTests(unittest.TestCase):
    def inspect(self, *, draining=False, pages=None, states=None, probe=None, fail=None):
        pages = pages or {None: {"data": ["detached-worker"], "nextCursor": None}}
        states = states or {"detached-worker": "active"}
        calls = []
        pending = []
        ws = Mock()

        def send(raw):
            message = json.loads(raw)
            calls.append(message)
            if "id" not in message:
                return
            method, params = message["method"], message["params"]
            if method == fail:
                response = {"error": {"code": -1, "message": "unavailable"}}
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
        with patch.object(client, "unix_connect") as connect:
            connect.return_value.__enter__.return_value = ws
            iterm_tab.inspect_codex_daemon(Path("/fixture/account"))
        return calls

    def test_healthy_busy_daemon_is_reused_without_loading_or_creating_a_thread(self):
        calls = self.inspect()
        self.assertTrue(any(c["method"] == "thread/read" for c in calls))
        resume = next(c for c in calls if c["method"] == "thread/resume")
        # Invalid native identity cannot resume anybody else's real thread.
        import uuid
        with self.assertRaises(ValueError):
            uuid.UUID(resume["params"]["threadId"])
        self.assertFalse(any(c["method"] in {"thread/start", "turn/start", "turn/interrupt"} for c in calls))

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

    def test_unavailable_or_unknown_inventory_and_admission_refuse(self):
        for kwargs in ({"fail": "thread/loaded/list"}, {"fail": "thread/read"},
                       {"states": {"detached-worker": "future-state"}},
                       {"probe": {"error": {"code": -32600, "message": "different refusal"}}},
                       {"probe": {"result": {}}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.inspect(**kwargs)

    def test_socket_timeout_refuses_without_lifecycle_mutation(self):
        with (patch.object(client, "unix_connect", side_effect=TimeoutError),
              patch.object(iterm_tab.subprocess, "run") as run):
            with self.assertRaises(TimeoutError):
                iterm_tab.inspect_codex_daemon(Path("/fixture/account"))
            run.assert_not_called()

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

    async def test_alive_after_observation_passes(self):
        await iterm_tab.verify_startup(AsyncMock(return_value={"dead": False, "screen": ["ready"]}), timeout=0)

    async def test_tmux_failed_launch_retains_receipt_identity_and_output(self):
        row = {"window_id": "$1", "tab_id": "@1", "pane_id": "%1", "tty": "fixture",
               "pid": 1, "backend": "tmux", "tmux_socket": "fixture"}
        terminal = Mock()
        terminal.run.return_value.stdout = "@1\t%1"
        terminal.select.return_value = row
        terminal.send = AsyncMock()
        terminal.read.return_value = {"dead": True, "exit_status": "1", "screen": ["thread/start failed"]}
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

    async def test_iterm_agent_exit_without_exec_ends_the_launch_shell(self):
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
            result = subprocess.run(["/bin/zsh", "-c", text], capture_output=True, text=True, timeout=2)
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


if __name__ == "__main__":
    unittest.main()
