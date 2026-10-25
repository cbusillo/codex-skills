#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets==17.2"]
# ///
"""Provider/harness consistency, without real accounts or terminals."""

import argparse
import asyncio
import errno
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

import account_choice
import agent_tools
import iterm_tab
from codex_session_proxy import MEMORY_SETTING
import tmux_terminal
import session_liveness
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))


HARNESS_CHOICES = (
    ("openai", "codex", {"CODEX_HOME": "/fixture/codex"}),
    ("anthropic", "claude", {"CLAUDE_CONFIG_DIR": "/fixture/claude"}),
    ("google", "agy", {}),
)


class ResumeLaunchTests(unittest.TestCase):
    def test_preparation_read_deadline_leaves_account_probes_and_writes_their_own_bounds(self) -> None:
        def select(*args: object) -> dict:
            time.sleep(0.1)
            return self.choice

        self.select.side_effect = select
        self.prepare.side_effect = lambda *args: time.sleep(0.1)
        with patch.object(iterm_tab, "LAUNCH_TIMEOUT", 0.05):
            commands, choices = iterm_tab.prepare_launches(self.args)
        self.assertEqual(choices, [self.choice])
        self.assertEqual(len(commands), 1)
        self.prepare.assert_called_once_with(self.choice)

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp", prefix="resume-tab-"))).resolve()
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "CODEX_HOME": str(self.root / "account")}))
        self.home = self.root / "account"
        self.session = str(uuid.uuid4())
        transcript = self.home / "sessions" / (self.session + ".jsonl")
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.session}}) + "\n")
        self.endpoint = iterm_tab.codex_daemon_endpoint(self.home)
        self.endpoint.parent.mkdir()
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(self.endpoint))
        self.endpoint.chmod(0o600)
        self.entry = {"session_id": self.session, "harness": "codex", "supervisor_owned": True,
                      "codex_home": str(self.home), "transcript": str(transcript)}
        self.ledger = self.root / "ledger.json"
        self.ledger.write_text(json.dumps([self.entry]))
        self.args = SimpleNamespace(resume_session_id=self.session, ledger=self.ledger,
                                    command_file=None, brief_file=None, account_provider="openai",
                                    account_config=None, account=None)
        self.account = {"provider": "openai", "name": "original", "env": {"CODEX_HOME": str(self.home)}}
        self.choice = {**self.account, "storage_root": self.root, "account_id": "openai-ab12"}
        self.enterContext(patch.object(account_choice, "load_config", return_value={"accounts": [self.account]}))
        self.select = self.enterContext(patch.object(account_choice, "select", return_value=self.choice))
        self.prepare = self.enterContext(patch.object(account_choice, "prepare_launch"))
        self.inspect = self.enterContext(patch.object(iterm_tab, "inspect_codex_daemon"))
        self.start = self.enterContext(patch.object(iterm_tab, "check_codex_daemon"))
        import github_plan_sessions
        self.enterContext(patch.object(github_plan_sessions, "configured_homes", return_value=[self.home]))
        import github_plan_closure
        self.log = self.root / "closed.json"
        self.enterContext(patch.object(github_plan_closure, "closed_log", return_value=self.log))

    def test_resume_uses_exact_source_helper_and_existing_receipt_route(self):
        commands, choices = iterm_tab.prepare_launches(self.args)
        argv = shlex.split(commands[0])
        helper = str(Path(iterm_tab.__file__).with_name("codex-session.py").resolve())
        self.assertIn(helper, argv)
        self.assertEqual(argv[argv.index("--session-id") + 1], self.session)
        self.assertEqual(argv[argv.index("--codex-home") + 1], str(self.home))
        self.select.assert_called_once_with("openai", None, self.account["name"])
        self.prepare.assert_called_once_with(choices[0])
        self.inspect.assert_called_once()
        self.assertEqual(self.inspect.call_args.args[0], self.home)
        self.start.assert_not_called()
        self.assertEqual(choices[0]["resume_session_id"], self.session)

    def test_frozen_or_duplicate_source_refuses_before_receipts_and_daemon_reads(self):
        for rows in ([{"session_id": self.session, "successor_admitted": True}],
                     [{"session_id": self.session}, {"session_id": self.session}]):
            with self.subTest(rows=rows):
                self.log.write_text(json.dumps(rows))
                self.log.chmod(0o600)
                original = self.log.read_bytes()
                with self.assertRaises(ValueError):
                    iterm_tab.prepare_launches(self.args)
                self.assertEqual(self.log.read_bytes(), original)
        self.select.assert_not_called()
        self.prepare.assert_not_called()
        self.inspect.assert_not_called()

    def test_tilde_ledger_home_matches_the_same_source_account(self):
        self.ledger.write_text(json.dumps([{**self.entry, "codex_home": "~/account"}]))
        with patch.dict(os.environ, {"HOME": str(self.root)}):
            commands, choices = iterm_tab.prepare_launches(self.args)
        argv = shlex.split(commands[0])
        self.assertEqual(argv[argv.index("--codex-home") + 1], str(self.home))
        self.assertEqual(choices[0]["env"], self.account["env"])

    def test_daemon_failures_stay_on_source_and_refuse_before_receipt_preparation(self):
        from websockets.exceptions import InvalidHandshake, ConnectionClosedError
        for failure in (InvalidHandshake("fixture handshake"), ConnectionClosedError(None, None),
                        iterm_tab.DaemonRefusal("draining", 2), TimeoutError(),
                        ConnectionRefusedError(errno.ECONNREFUSED, "fixture"),
                        FileNotFoundError(errno.ENOENT, "fixture")):
            with self.subTest(failure=type(failure).__name__):
                self.inspect.side_effect = failure
                with self.assertRaises(ValueError) as caught:
                    iterm_tab.prepare_launches(self.args)
                self.assertIn(str(self.home), str(caught.exception))
                self.assertNotIn("--account", str(caught.exception))
        self.prepare.assert_not_called()
        self.start.assert_not_called()

    def test_missing_source_socket_refuses_before_account_or_launch_effects(self):
        self.endpoint.unlink()
        with self.assertRaises(ValueError):
            iterm_tab.prepare_launches(self.args)
        self.select.assert_not_called()
        self.prepare.assert_not_called()
        self.inspect.assert_not_called()
        self.start.assert_not_called()

    def test_bad_identity_and_launch_options_refuse_before_receipt_preparation(self):
        for changes in ({"supervisor_owned": False}, {"harness": "claude"}, {"codex_home": "relative"},
                        {"session_id": str(uuid.uuid4())}, {"transcript": str(self.root / "foreign.jsonl")}):
            with self.subTest(changes=changes):
                self.ledger.write_text(json.dumps([{**self.entry, **changes}]))
                with self.assertRaises(ValueError):
                    iterm_tab.prepare_launches(self.args)
        self.ledger.write_text(json.dumps([self.entry, self.entry]))
        with self.assertRaises(ValueError):
            iterm_tab.prepare_launches(self.args)
        self.ledger.write_text(json.dumps([self.entry]))
        for field, value in (("account_provider", "anthropic"), ("account", "other"),
                             ("command_file", [self.root / "command"]), ("brief_file", [self.root / "brief"]),
                             ("require_browser", True), ("ledger", None)):
            with self.subTest(field=field), patch.object(self.args, field, value, create=True):
                with self.assertRaises(ValueError):
                    iterm_tab.prepare_launches(self.args)
        self.select.assert_not_called()
        self.prepare.assert_not_called()

    def test_bad_transcript_socket_and_account_selection_have_no_launch_effects(self):
        transcript = Path(self.entry["transcript"])
        original = transcript.read_text()
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": str(uuid.uuid4())}}) + "\n")
        with self.assertRaises(ValueError):
            iterm_tab.prepare_launches(self.args)
        transcript.write_text(original)
        self.endpoint.chmod(0o622)
        with self.assertRaises(ValueError):
            iterm_tab.prepare_launches(self.args)
        self.endpoint.chmod(0o600)
        self.select.return_value = {**self.choice, "env": {"CODEX_HOME": str(self.root)}}
        with self.assertRaises(ValueError):
            iterm_tab.prepare_launches(self.args)
        self.inspect.assert_not_called()
        self.prepare.assert_not_called()

    def test_resume_metadata_keeps_the_guarded_generation_and_terminal_identity(self):
        import github_plan_closure as closure
        from datetime import datetime, timezone
        born = datetime.now(timezone.utc)
        generation = {"pid": 456, "launch_pid": 456, "tty": "fixture-tty", "codex_home": str(self.home),
                      "launch_generation": "guarded-generation", "launch_started_at": born.isoformat()}
        self.choice["resume_session_id"] = self.session
        with patch.object(closure, "log_rows", return_value=[{"session_id": self.session, "resumed_generation": generation}]), \
                patch.object(closure, "process_started_at", return_value=born):
            result = iterm_tab.source_launch_metadata({"session_id": "terminal-id", "tty": "/dev/fixture-tty"}, self.choice)
            self.assertEqual({key: result[key] for key in generation}, generation)
            self.assertEqual(result["native_session_id"], self.session)
            self.assertEqual(result["session_id"], "terminal-id")
            for change in ({"tty": "wrong-tty"}, {"codex_home": str(self.root)}, {"launch_started_at": "old"}):
                with self.subTest(change=change), patch.object(closure, "log_rows", return_value=[
                        {"session_id": self.session, "resumed_generation": {**generation, **change}}]):
                    failed = iterm_tab.source_launch_metadata({"session_id": "terminal-id", "tty": "/dev/fixture-tty"}, self.choice)
                    self.assertTrue(failed["launch_identity_unavailable"])


class ProviderLaunchTests(unittest.TestCase):
    def setUp(self):
        # Generic launch cases must not discover the host's private opt-in.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        opt_in = patch.object(iterm_tab, "CHROME_OPT_IN", Path(temp.name) / "absent")
        opt_in.start()
        self.addCleanup(opt_in.stop)

    def test_required_browser_needs_a_supported_provider_before_account_selection(self):
        for provider in (None, "google"):
            with tempfile.TemporaryDirectory() as folder:
                command = Path(folder) / "launch"
                brief = Path(folder) / "brief"
                command.write_text("agy")
                brief.write_text("Read the approved page")
                args = SimpleNamespace(command_file=[command], brief_file=[brief],
                                       account_provider=provider, require_browser=True)
                with patch.object(account_choice, "select") as select:
                    with self.assertRaises(ValueError):
                        iterm_tab.prepare_launches(args)
                    select.assert_not_called()

    def test_conflicting_claude_flags_refuse_before_browser_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            opt = Path(folder) / "opt"
            opt.touch()
            with (patch.object(iterm_tab, "CHROME_OPT_IN", opt),
                  patch.object(agent_tools, "connected_browsers") as probe):
                with self.assertRaises(ValueError):
                    iterm_tab.check_claude_tools("cd / && exec claude --chrome",
                                                {"provider": "anthropic", "env": {}})
                probe.assert_not_called()

    def test_claude_project_lookup_resolves_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            repo, alias, home = (root / name for name in ("repo", "alias", "home"))
            repo.mkdir()
            home.mkdir()
            alias.symlink_to(repo, target_is_directory=True)
            opt = root / "opt"
            opt.touch()
            (home / ".claude.json").write_text(json.dumps({
                "mcpServers": {agent_tools.CHROME_SERVER: {"command": "fixture"}},
                "projects": {str(repo): {"disabledMcpServers": [agent_tools.CHROME_SERVER]}}}))
            choice = {"provider": "anthropic", "env": {"CLAUDE_CONFIG_DIR": str(home)}}
            with (patch.object(iterm_tab, "CHROME_OPT_IN", opt),
                  patch.object(agent_tools, "connected_browsers", return_value=[{}]) as probe):
                with self.assertRaises(ValueError):
                    iterm_tab.check_claude_tools(f"cd {shlex.quote(str(alias))} && exec claude",
                                                choice, require_browser=True)
                iterm_tab.check_claude_tools(f"cd {shlex.quote(str(alias))} && exec claude", choice)
                self.assertEqual(len(choice["tools"]["warnings"]), 1)
                probe.assert_not_called()
                for command in ("claude", "cd relative && claude"):
                    with self.assertRaises(ValueError):
                        iterm_tab.check_claude_tools(command, choice, require_browser=True)

    def test_launch_output_omits_target_project_and_expands_home_for_redaction(self):
        choice = {"provider": "anthropic", "env": {"CLAUDE_CONFIG_DIR": "~/fixture-account"},
                  "tool_directory": "/private/fixture-project",
                  "tools": {"warnings": [str(Path.home() / "fixture-account") + " unavailable"]}}
        result = json.dumps(account_choice.public(choice))
        self.assertNotIn("fixture-project", result)
        self.assertNotIn(str(Path.home()), result)

    def test_managed_launch_preserves_direct_prefix_directory_and_literal_prompt(self):
        choice = {"provider": "openai", "env": {"CODEX_HOME": "/fixture/account"}}
        endpoint = "unix://" + str(iterm_tab.codex_daemon_endpoint(Path(choice["env"]["CODEX_HOME"])))
        prepared = iterm_tab.with_account("cd /fixture && exec env TERM=fixture codex --remote " + shlex.quote(endpoint), choice,
                                         "literal $(touch NEVER)", coordinate=True)
        # Parse the shell sections rather than asserting a rendered command.
        parts = shlex.split(prepared)
        self.assertEqual(parts[parts.index("cd") + 1], "/fixture")
        self.assertIn("TERM=fixture", parts)
        native = parts[parts.index("--") + 1:]
        self.assertEqual(native, ["codex", "--remote", endpoint, "-c", f"{MEMORY_SETTING}=false",
                                  "--", "literal $(touch NEVER)"])

    def test_codex_launch_records_account_and_verified_process_generation(self):
        import github_plan_closure
        source = {"session_id": "terminal-generation", "tty": "/dev/fixture-tty"}
        choice = {"provider": "openai", "env": {"CODEX_HOME": "/fixture/account"}}
        with patch.object(session_liveness, "inventory", return_value=[(456, "fixture-tty", "codex --remote unix:///observed"),
                                                              (457, "fixture-tty", "python3 /fixture/codex-session.py launch --codex-home /fixture")]), \
                patch.object(github_plan_closure, "process_started_at") as started:
            from datetime import datetime, timezone
            started.return_value = datetime(2026, 10, 1, tzinfo=timezone.utc)
            result = iterm_tab.source_launch_metadata(source, choice)
        self.assertEqual(result["pid"], 456)
        self.assertTrue(result["launch_generation"])
        self.assertIsInstance(result["launch_generation"], str)
        self.assertEqual(result["codex_home"], choice["env"]["CODEX_HOME"])

    def test_terminal_resume_cannot_bypass_coordination(self):
        with self.assertRaises(ValueError):
            iterm_tab.validate_resume_input("/resume source")

    def test_claude_resume_and_pasted_reference_keep_their_own_meaning(self):
        target = SimpleNamespace(session_id="fixture-target", async_get_variable=AsyncMock(return_value="/dev/fixture"),
                                 async_send_text=AsyncMock())
        with patch.object(session_liveness, "inventory", return_value=[(456, "fixture", "claude --model opus")]):
            asyncio.run(iterm_tab.send(target, "/resume source", submit=False))
        self.assertEqual(target.async_send_text.call_args.args[0], "/resume source")
        pasted = "The command is:\n/resume source"
        asyncio.run(iterm_tab.send(target, pasted, submit=False))
        self.assertIn(pasted, target.async_send_text.call_args.args[0])
        with patch.object(session_liveness, "inventory", return_value=[(456, "fixture", "codex --remote unix:///fixture")]):
            with self.assertRaises(ValueError):
                asyncio.run(iterm_tab.send(target, "/resume source", submit=False))

    def test_cold_launch_uses_live_proxy_identity_before_native_exec(self):
        import github_plan_closure as closure
        from datetime import datetime, timezone
        born = datetime(2026, 10, 1, tzinfo=timezone.utc)
        source = {"session_id": "terminal-generation", "tty": "/dev/fixture"}
        choice = {"provider": "openai", "env": {"CODEX_HOME": "/fixture/account"}}
        with patch.object(session_liveness, "inventory", return_value=[
                (456, "fixture", "python3 /fixture/codex-session.py launch --codex-home /fixture/account"),
                (457, "fixture", "python3 /fixture/codex-session.py native --codex-home /fixture/account")]), \
                patch.object(closure, "process_started_at", return_value=born), \
                patch.object(closure, "log_rows", return_value=[{"record_kind": "managed_proxy", "launch_pid": 456,
                    "codex_home": choice["env"]["CODEX_HOME"], "launch_started_at": born.isoformat()}]):
            result = iterm_tab.source_launch_metadata(source, choice)
        self.assertEqual(result["pid"], 456)
        self.assertNotIn("launch_identity_unavailable", result)

    def test_wrong_harness_refuses_with_or_without_a_brief(self):
        for provider, expected, env in HARNESS_CHOICES:
            for _, agent, _ in HARNESS_CHOICES:
                if agent == expected:
                    continue
                for command in (agent, f"cd / && exec /fixture/{agent}"):
                    for brief in (None, "literal brief"):
                        with self.subTest(provider=provider, command=command, brief=brief):
                            with self.assertRaises(ValueError):
                                iterm_tab.with_account(command, {"provider": provider, "env": env}, brief)

    def test_mismatched_later_batch_item_refuses_before_side_effects_on_both_backends(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = [root / "first", root / "second"]
            brief = root / "brief"
            brief.write_text("literal task")
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            for provider, expected, env in HARNESS_CHOICES:
                choice = {"provider": provider, "env": env}
                for _, wrong, _ in HARNESS_CHOICES:
                    if wrong == expected:
                        continue
                    files[0].write_text(expected)
                    files[1].write_text(f"cd / && exec /fixture/{wrong}")
                    args = argparse.Namespace(command="new", window_id="chosen", command_file=files,
                                              brief_file=[brief, brief], account_provider=provider,
                                              account_config=None, account=None, tmux_socket="fixture")
                    for backend in ("iterm", "tmux"):
                        terminal = Mock()
                        with (self.subTest(provider=provider, wrong=wrong, backend=backend),
                              patch.dict(sys.modules, {"iterm2": SimpleNamespace()}),
                              patch.object(tmux_terminal, "Terminal", return_value=terminal),
                              patch.object(account_choice, "select_batch", return_value=[choice, choice]),
                              patch.object(iterm_tab, "check_codex_project"),
                              patch.object(iterm_tab, "check_codex_daemon") as daemon,
                              patch.object(account_choice, "prepare_launch") as prepare,
                              patch.object(account_choice, "record_launch") as receipt):
                            with self.assertRaises(ValueError):
                                asyncio.run(iterm_tab.operate(app, args) if backend == "iterm"
                                            else tmux_terminal.operate(args))
                            daemon.assert_not_called()
                            prepare.assert_not_called()
                            receipt.assert_not_called()
                            window.async_create_tab.assert_not_called()
                            terminal.run.assert_not_called()

    def test_matching_commands_execute_with_selected_environment_and_literal_brief(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            brief = "--task's $(touch NEVER) `touch NEVER`\nnext\tline"
            for provider, agent, env in HARNESS_CHOICES:
                child = root / agent
                child.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                                 'print(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}))\n')
                child.chmod(0o700)
                executable = shlex.quote(str(child))
                for command in (executable, "exec " + executable,
                                "command " + executable, "env TERM=fixture " + executable,
                                f"cd {shlex.quote(str(root))} && exec {executable}"):
                    with self.subTest(provider=provider, command=command):
                        prepared = iterm_tab.with_account(command, {"provider": provider, "env": env}, brief)
                        completed = subprocess.run(["/bin/sh", "-c", prepared], cwd=root,
                                                   env={**os.environ, "CLAUDE_CONFIG_DIR": "/inherited"},
                                                   check=True, capture_output=True, text=True)
                        received = json.loads(completed.stdout)
                        if agent == "agy":
                            self.assertEqual(received["argv"], ["--prompt-interactive=" + brief])
                        else:
                            self.assertEqual(received["argv"][-2:], ["--", brief])
                        for key, value in env.items():
                            self.assertEqual(received["env"][key], value)
            self.assertFalse((root / "NEVER").exists())

    def test_pinned_chrome_launch_exports_single_route_and_refuses_explicit_override(self):
        with tempfile.TemporaryDirectory() as folder:
            opt_in = Path(folder) / "chrome.toml"
            opt_in.touch()
            child = Path(folder) / "claude"
            child.write_text(f'#!{sys.executable}\nimport os,json\nprint(json.dumps(dict(os.environ)))\n')
            child.chmod(0o700)
            choice = {"provider": "anthropic", "env": {}}
            with patch.object(iterm_tab, "CHROME_OPT_IN", opt_in):
                command = iterm_tab.with_account(str(child), choice)
                result = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, check=True,
                                        env={**os.environ, agent_tools.CHROME_ENV: "1"})
                self.assertEqual(json.loads(result.stdout)[agent_tools.CHROME_ENV], "0")
                for command in ("claude --chrome", f"{agent_tools.CHROME_ENV}=1 claude"):
                    with self.subTest(command=command), self.assertRaises(ValueError):
                        iterm_tab.with_account(command, choice)

    def test_browser_preflight_checks_selected_home_and_caches_batch_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            opt_in = root / "chrome.toml"
            opt_in.touch()
            repo = (root / "repository with spaces").resolve()
            home = root / "selected"
            home.mkdir()
            server = {"command": "fixture"}
            (home / ".claude.json").write_text(json.dumps({
                "mcpServers": {agent_tools.CHROME_SERVER: server},
                "projects": {str(repo): {}}}))
            choice = {"provider": "anthropic", "env": {"CLAUDE_CONFIG_DIR": str(home)}}
            command = "cd " + shlex.quote(str(repo)) + " && claude"
            with (patch.object(iterm_tab, "CHROME_OPT_IN", opt_in),
                  patch.dict(sys.modules, {"iterm2": SimpleNamespace()}),
                  patch.object(agent_tools, "connected_browsers", return_value=[{"id": "fixture"}]) as probe):
                cache = {}
                for _ in range(2):
                    iterm_tab.check_claude_tools(command, choice, probes=cache, require_browser=True)
                self.assertEqual(probe.call_count, 1)
                probe.assert_called_with(server)
                self.assertEqual(choice["tools"]["warnings"], [])
                self.assertEqual(choice["tools"]["browsers"], 1)
                probe.return_value = []
                with self.assertRaises(ValueError):
                    iterm_tab.check_claude_tools(command, choice, require_browser=True)
                iterm_tab.check_claude_tools(command, choice)
                self.assertTrue(choice["tools"]["warnings"])
                probe.reset_mock()
                iterm_tab.check_claude_tools(command, choice, without_browser=True)
                probe.assert_not_called()

    def test_required_browser_refuses_before_receipt_or_terminal_on_both_backends(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launch, brief, opt_in = (root / name for name in ("launch", "brief", "chrome.toml"))
            launch.write_text("claude")
            brief.write_text("Read one page in the Agents browser.")
            opt_in.touch()
            choice = {"provider": "anthropic", "env": {}}
            args = argparse.Namespace(command="new", window_id="chosen", command_file=[launch],
                                      brief_file=[brief], account_provider="anthropic", account_config=None,
                                      account=None, tmux_socket="fixture", require_browser=True)
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            with (patch.object(iterm_tab, "CHROME_OPT_IN", opt_in),
                  patch.dict(sys.modules, {"iterm2": SimpleNamespace()}),
                  patch.object(agent_tools, "claude_problems", return_value=[]),
                  patch.object(agent_tools, "chrome_server", return_value={"command": "fixture"}),
                  patch.object(agent_tools, "connected_browsers", return_value=[]),
                  patch.object(account_choice, "select", return_value=choice),
                  patch.object(account_choice, "prepare_launch") as prepare,
                  patch.object(account_choice, "record_launch") as receipt,
                  patch.object(tmux_terminal, "Terminal") as terminal):
                for backend in ("iterm", "tmux"):
                    with self.subTest(backend=backend), self.assertRaises(ValueError):
                        asyncio.run(iterm_tab.operate(app, args) if backend == "iterm"
                                    else tmux_terminal.operate(args))
                prepare.assert_not_called()
                receipt.assert_not_called()
                terminal.return_value.run.assert_not_called()
                window.async_create_tab.assert_not_called()

    def test_local_codex_uses_selected_marketplace_and_preserves_account_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / ".codex" / agent_tools.BUNDLED_MARKETPLACE
            source.mkdir(parents=True)
            account = root / "account with spaces"
            account.mkdir()
            config = account / "config.toml"
            config.write_text("# preserve fixture config\n")
            before = config.read_bytes()
            auth = account / "auth.json"
            auth.write_text("opaque fixture; never read by launcher")
            opt_in = root / "chrome.toml"
            opt_in.touch()
            plugins = [{"name": n, "marketplaceName": "openai-bundled", "installed": True, "enabled": True}
                       for n in agent_tools.CODEX_PLUGINS]
            choice = {"provider": "openai", "env": {"CODEX_HOME": str(account)}}
            with (patch.object(iterm_tab, "CHROME_OPT_IN", opt_in),
                  patch.object(Path, "home", return_value=root),
                  patch.object(agent_tools, "codex_plugins", return_value=plugins) as inventory):
                command = iterm_tab.prepare_codex_tools("codex -C /fixture/repo", choice, required=True)
                link = account / agent_tools.BUNDLED_MARKETPLACE
                self.assertEqual(link.resolve(), source.resolve())
                self.assertEqual(config.read_bytes(), before)
                self.assertTrue(auth.is_file())
                inventory.assert_called_once_with(account, link, Path("/fixture/repo"))
                words = shlex.split(command)
                self.assertIn("--no-daemon", words)
                import tomllib
                override = tomllib.loads(words[words.index("-c") + 1])
                self.assertEqual(override["marketplaces"]["openai-bundled"]["source"], str(link))
                self.assertEqual(choice["env"][agent_tools.CODEX_SOURCE_ENV], str(link))
                inventory.return_value = []
                with self.assertRaises(ValueError):
                    iterm_tab.prepare_codex_tools("codex -C /fixture/repo", choice, required=True)
                iterm_tab.prepare_codex_tools("codex -C /fixture/repo", choice)
                self.assertTrue(choice["tools"]["warnings"])
                inventory.side_effect = ValueError("fixture discovery unavailable")
                command = iterm_tab.prepare_codex_tools("codex -C /fixture/repo", choice)
                self.assertIn("--no-daemon", shlex.split(command))
                self.assertEqual(choice["tools"]["route"], "local")
                self.assertTrue(choice["tools"]["warnings"])
                inventory.side_effect = None
                command = iterm_tab.prepare_codex_tools("codex --no-daemon -C /fixture/repo", choice)
                self.assertEqual(shlex.split(command).count("--no-daemon"), 1)
                fallback = {"provider": "openai", "env": {"CODEX_HOME": str(account)}}
                with patch.object(iterm_tab.gui_launch, "marketplace", side_effect=ValueError("fixture setup unavailable")):
                    command = iterm_tab.prepare_codex_tools("codex -C /fixture/repo", fallback)
                self.assertNotIn("--no-daemon", shlex.split(command))
                self.assertEqual(fallback["tools"]["route"], "daemon")
                with patch.object(iterm_tab.gui_launch, "marketplace", side_effect=ValueError("fixture setup unavailable")):
                    command = iterm_tab.prepare_codex_tools("codex --no-daemon -C /fixture/repo", fallback)
                self.assertEqual(fallback["tools"]["route"], "local")
                self.assertIn("--no-daemon", shlex.split(command))
                with patch.object(iterm_tab, "CHROME_OPT_IN", root / "absent"):
                    fresh = {"provider": "openai", "env": {"CODEX_HOME": str(account)}}
                    iterm_tab.prepare_codex_tools("codex --no-daemon -C /fixture/repo", fresh)
                self.assertEqual(fresh["tools"]["route"], "local")
                inventory.reset_mock()
                remote = "codex --remote unix:// -C /fixture/repo"
                self.assertEqual(iterm_tab.prepare_codex_tools(remote, choice, required=True), remote)
                inventory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
