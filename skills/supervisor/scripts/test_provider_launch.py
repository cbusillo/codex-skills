#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Provider/harness consistency, without real accounts or terminals."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import account_choice
import iterm_tab
import tmux_terminal
import close_ttys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))


HARNESS_CHOICES = (
    ("openai", "codex", {"CODEX_HOME": "/fixture/codex"}),
    ("anthropic", "claude", {"CLAUDE_CONFIG_DIR": "/fixture/claude"}),
    ("google", "agy", {}),
)


class ProviderLaunchTests(unittest.TestCase):
    def test_managed_launch_preserves_direct_prefix_directory_and_literal_prompt(self):
        choice = {"provider": "openai", "env": {"CODEX_HOME": "/fixture/account"}}
        prepared = iterm_tab.with_account("cd /fixture && exec env TERM=fixture codex --remote unix:///fixture", choice,
                                         "literal $(touch NEVER)", coordinate=True)
        # Parse the shell sections rather than asserting a rendered command.
        parts = shlex.split(prepared)
        self.assertEqual(parts[parts.index("cd") + 1], "/fixture")
        self.assertIn("TERM=fixture", parts)
        native = parts[parts.index("--") + 1:]
        self.assertEqual(native, ["codex", "--remote", "unix:///fixture", "--", "literal $(touch NEVER)"])

    def test_codex_launch_records_account_and_verified_process_generation(self):
        import github_plan_closure
        source = {"session_id": "terminal-generation", "tty": "/dev/fixture-tty"}
        choice = {"provider": "openai", "env": {"CODEX_HOME": "/fixture/account"}}
        with patch.object(close_ttys, "inventory", return_value=[(456, "fixture-tty", "codex --remote unix:///observed"),
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
        with patch.object(close_ttys, "inventory", return_value=[(456, "fixture", "claude --model opus")]):
            asyncio.run(iterm_tab.send(target, "/resume source", submit=False))
        self.assertEqual(target.async_send_text.call_args.args[0], "/resume source")
        pasted = "The command is:\n/resume source"
        asyncio.run(iterm_tab.send(target, pasted, submit=False))
        self.assertIn(pasted, target.async_send_text.call_args.args[0])
        with patch.object(close_ttys, "inventory", return_value=[(456, "fixture", "codex --remote unix:///fixture")]):
            with self.assertRaises(ValueError):
                asyncio.run(iterm_tab.send(target, "/resume source", submit=False))

    def test_cold_launch_uses_live_proxy_identity_before_native_exec(self):
        import github_plan_closure as closure
        from datetime import datetime, timezone
        born = datetime(2026, 10, 1, tzinfo=timezone.utc)
        source = {"session_id": "terminal-generation", "tty": "/dev/fixture"}
        choice = {"provider": "openai", "env": {"CODEX_HOME": "/fixture/account"}}
        with patch.object(close_ttys, "inventory", return_value=[
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


if __name__ == "__main__":
    unittest.main()
