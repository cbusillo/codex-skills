# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets==17.2"]
# ///
"""GUI launch behavior using temporary account homes and fake console/agents."""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import account_choice
import gui_launch
import iterm_tab
import tmux_terminal


class GuiLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "owner"
        self.account = self.root / "account with spaces"
        self.account.mkdir()
        self.source = self.home / ".codex" / gui_launch.BUNDLED_MARKETPLACE
        self.source.mkdir(parents=True)
        self.state = self.root / "private/gui.json"
        self.addCleanup(patch.stopall)
        patch.object(Path, "home", return_value=self.home).start()
        patch.object(gui_launch, "state_path", return_value=self.state).start()
        self.unlocked = patch.object(gui_launch, "require_unlocked").start()
        self.choice = {"provider": "openai", "env": {"CODEX_HOME": str(self.account)},
                       "account_id": "openai-a", "storage_root": str(self.root)}
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.account / "config.toml").write_text(
            '[projects.' + json.dumps(str(self.repo)) + ']\ntrust_level="trusted"\n')
        # These sentinels are never read by the launcher.
        (self.account / "auth.json").write_text("opaque fixture")
        self.config_before = (self.account / "config.toml").read_bytes()
        self.command = self.root / "command"
        self.command.write_text("exec codex -C " + shlex.quote(str(self.repo)))
        self.brief = self.root / "brief"
        self.brief.write_text("GUI task: com.example.Task\nInspect the named app.")
        self.args = argparse.Namespace(command_file=[self.command], brief_file=[self.brief],
                                       account_provider="openai", account_config=None, account=None)
        patch.object(account_choice, "select", return_value=self.choice).start()
        self.daemon = patch.object(iterm_tab, "check_codex_daemon").start()

    def test_selected_home_override_reaches_agent_and_receipt_without_config_changes(self):
        fake = self.root / "codex"
        output = self.root / "argv.json"
        fake.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                        f"open({str(output)!r}, 'w').write(json.dumps([sys.argv[1:],os.getenv('CODEX_HOME')]))\n")
        fake.chmod(0o755)
        self.command.write_text("exec " + shlex.quote(str(fake)) + " -C " + shlex.quote(str(self.repo)))
        commands, choices = iterm_tab.prepare_launches(self.args)
        gui = choices[0]["gui"]
        receipt = json.loads(account_choice.record_launch(choices[0]).read_text())
        self.assertEqual(receipt["gui"], gui)
        self.assertEqual(gui["app"], "com.example.Task")
        wrapper = shlex.split(commands[0])
        command = wrapper[wrapper.index("--command") + 1]
        self.assertEqual(gui_launch.run(self.state, gui["token"], command), 0)
        argv, home = json.loads(output.read_text())
        self.assertEqual(home, str(self.account))
        source = Path(home) / self.source.relative_to(self.home / ".codex")
        overrides = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-c"]
        self.assertEqual(json.loads(overrides[-1].split("=", 1)[1]), str(source))
        self.assertEqual(source.resolve(), self.source.resolve())
        self.assertIn(self.brief.read_text(), argv[-1])
        self.assertFalse(self.state.exists())
        self.assertEqual((self.account / "config.toml").read_bytes(), self.config_before)
        self.assertEqual((self.account / "auth.json").read_text(), "opaque fixture")

    def test_nongui_launch_does_not_probe_or_create_gui_paths(self):
        self.brief.write_text("Implement source changes only.")
        with patch.object(gui_launch, "marketplace") as marketplace:
            commands, choices = iterm_tab.prepare_launches(self.args)
        marketplace.assert_not_called()
        self.unlocked.assert_not_called()
        self.assertNotIn("gui", choices[0])
        self.assertFalse((self.account / ".tmp").exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(commands[0], iterm_tab.with_account(self.command.read_text(), self.choice,
                                                            self.brief.read_text()))
        self.assertNotIn("gui", json.loads(account_choice.record_launch(self.choice).read_text()))

    def test_source_brief_quoting_gui_header_does_not_opt_in(self):
        self.brief.write_text("Implement launcher behavior for this example:\nGUI task: com.example.Task")
        _, choices = iterm_tab.prepare_launches(self.args)
        self.assertNotIn("gui", choices[0])
        self.unlocked.assert_not_called()

    def test_output_redacts_account_paths_but_retains_recovery_token(self):
        _, choices = iterm_tab.prepare_launches(self.args)
        public = account_choice.public(choices[0])
        self.assertEqual(public["gui"]["token"], choices[0]["gui"]["token"])
        self.assertEqual(public["gui"]["app"], choices[0]["gui"]["app"])
        for private_path in (str(self.account), str(self.state), str(self.source)):
            self.assertNotIn(private_path, json.dumps(public))

    def test_unsubmitted_backend_failures_release_slot_and_preserve_token(self):
        self.args.command = "new"
        self.args.window_id = "fixture"
        self.args.tmux_socket = "fixture"
        window = SimpleNamespace(window_id="fixture", async_create_tab=AsyncMock(side_effect=OSError("fake failure")))
        app = SimpleNamespace(current_terminal_window=None, terminal_windows=[window])
        terminal = Mock()
        terminal.run.side_effect = OSError("fake failure")
        for backend in ("iterm", "tmux"):
            with self.subTest(backend=backend), patch.dict(sys.modules, {"iterm2": SimpleNamespace()}), \
                    patch.object(tmux_terminal, "Terminal", return_value=terminal):
                with self.assertRaises(iterm_tab.LaunchFailure) as raised:
                    asyncio.run(iterm_tab.operate(app, self.args) if backend == "iterm"
                                else tmux_terminal.operate(self.args))
                failure = raised.exception.detail["failed_launch"]
                self.assertEqual(failure["gui_reservation"], "released_unsubmitted")
                self.assertTrue(failure["account"]["gui"]["token"])
                self.assertFalse(self.state.exists())

    def test_uncertain_submission_retains_slot(self):
        _, choices = iterm_tab.prepare_launches(self.args)
        iterm_tab.cancel_unsubmitted_gui(choices[0], {"submission": "unknown"})
        self.assertTrue(self.state.exists())
        with self.assertRaises(ValueError):
            gui_launch.reserve("com.example.Other")

    def test_long_gui_receipt_is_pruned_after_retention(self):
        _, choices = iterm_tab.prepare_launches(self.args)
        choices[0]["gui"]["marketplace"]["path"] = "/fixture/" + "long-home/" * 200
        then = datetime.now(timezone.utc) - timedelta(days=2)
        receipt = account_choice.record_launch(choices[0], now=then)
        account_choice.prune_receipts(receipt.parent, datetime.now(timezone.utc))
        self.assertFalse(receipt.exists())

    def test_release_cli_requires_verified_shutdown_and_shared_path(self):
        reserved = gui_launch.reserve("com.example.Task")
        for extra in ([], ["--state", str(self.root / "other"), "--verified-worker-ended"]):
            with patch.object(sys, "argv", ["gui_launch.py", "release", "--token", reserved["token"], *extra]):
                with self.assertRaises(SystemExit) as raised:
                    gui_launch.main()
                self.assertNotEqual(raised.exception.code, 0)
                self.assertTrue(self.state.exists())
        with patch.object(sys, "argv", ["gui_launch.py", "release", "--token", reserved["token"],
                                        "--verified-worker-ended"]):
            gui_launch.main()
        self.assertFalse(self.state.exists())

    def test_locked_preflight_refuses_before_daemon_link_and_receipt(self):
        self.unlocked.side_effect = ValueError("locked")
        with self.assertRaises(ValueError):
            iterm_tab.prepare_launches(self.args)
        self.daemon.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertFalse((self.account / ".tmp").exists())
        self.assertFalse((self.root / "Launch Receipts").exists())

    def test_worker_rechecks_lock_before_spawning_agent(self):
        reservation = gui_launch.reserve("com.example.Task")
        self.unlocked.side_effect = ValueError("locked after terminal creation")
        with patch.object(gui_launch.subprocess, "call") as child:
            with self.assertRaises(ValueError):
                gui_launch.run(self.state, reservation["token"], "fake-agent")
        child.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_single_slot_blocks_pending_and_running_workers_and_live_release(self):
        reservation = gui_launch.reserve("com.example.Task")
        with self.assertRaises(ValueError):
            gui_launch.reserve("com.example.Other")

        def running(_command):
            self.assertEqual(json.loads(self.state.read_text())["phase"], "running")
            with self.assertRaises(ValueError):
                gui_launch.reserve("com.example.Other")
            with self.assertRaises(ValueError):
                gui_launch.release(self.state, reservation["token"])
            return 9

        with patch.object(gui_launch.subprocess, "call", side_effect=running):
            self.assertEqual(gui_launch.run(self.state, reservation["token"], "fake-agent"), 9)
        next_reservation = gui_launch.reserve("com.example.Other")
        with self.assertRaises(ValueError):
            gui_launch.release(self.state, reservation["token"])
        gui_launch.release(self.state, next_reservation["token"])

    def test_invalid_gui_requests_and_batches_do_not_reserve(self):
        for brief in ("GUI task: Terminal", "GUI task: com.apple.Terminal",
                      "GUI task: com.example.One\nGUI task: com.example.Two"):
            with self.subTest(brief=brief):
                self.brief.write_text(brief)
                with self.assertRaises(ValueError):
                    iterm_tab.prepare_launches(self.args)
        self.brief.write_text("GUI task: com.example.Task")
        self.args.command_file *= 2
        self.args.brief_file *= 2
        with self.assertRaises(ValueError):
            iterm_tab.prepare_launches(self.args)
        self.assertFalse(self.state.exists())

    def test_remote_gui_refuses(self):
        self.command.write_text("codex --remote unix:// -C " + str(self.repo))
        with self.assertRaises(ValueError):
            iterm_tab.prepare_launches(self.args)
        self.daemon.assert_not_called()
        self.assertFalse((self.account / ".tmp").exists())

    def test_marketplace_is_idempotent_and_follows_default_tree_replacement(self):
        first = gui_launch.marketplace(self.account, self.home / ".codex")
        link = Path(first["path"])
        self.assertTrue(link.is_symlink())
        original_link = os.readlink(link)
        gui_launch.marketplace(self.account, self.home / ".codex")
        self.assertEqual(os.readlink(link), original_link)
        self.source.rename(self.source.with_name("old-bundle"))
        self.source.mkdir()
        (self.source / "fresh").write_text("updated bundle")
        self.assertEqual((link / "fresh").read_text(), "updated bundle")

    def test_missing_or_occupied_marketplace_preserves_data_and_releases_preparation(self):
        link = self.account / gui_launch.BUNDLED_MARKETPLACE
        link.mkdir(parents=True)
        sentinel = link / "owner-data"
        sentinel.write_text("preserve")
        with self.assertRaises(ValueError):
            iterm_tab.prepare_launches(self.args)
        self.assertFalse(self.state.exists())
        self.assertEqual(sentinel.read_text(), "preserve")
        sentinel.unlink()
        link.rmdir()
        link.symlink_to(self.root / "other")
        with self.assertRaises(ValueError):
            gui_launch.marketplace(self.account, self.home / ".codex")
        self.assertEqual(os.readlink(link), str(self.root / "other"))
        self.source.rmdir()
        with self.assertRaises(ValueError):
            gui_launch.marketplace(self.account, self.home / ".codex")

    def test_redirected_parent_is_not_written_through(self):
        elsewhere = self.root / "unrelated"
        elsewhere.mkdir()
        (self.account / ".tmp").symlink_to(elsewhere)
        with self.assertRaises(ValueError):
            gui_launch.marketplace(self.account, self.home / ".codex")
        self.assertEqual(list(elsewhere.iterdir()), [])


class ConsoleTests(unittest.TestCase):
    def test_real_parser_with_fake_ioreg_evidence(self):
        for data, allowed in (([{"IOConsoleUsers": [{"CGSSessionScreenIsLocked": False}]}], True),
                              ([{"IOConsoleLocked": False}], True),
                              ([{"IOConsoleLocked": True}], False), ([], False),
                              ([{"IOConsoleLocked": "No"}], False),
                              ([{"IOConsoleLocked": False, "CGSSessionScreenIsLocked": True}], False)):
            with self.subTest(data=data), patch.object(gui_launch.subprocess, "run", return_value=
                                                      subprocess.CompletedProcess([], 0, plistlib.dumps(data))):
                if allowed:
                    gui_launch.require_unlocked()
                else:
                    with self.assertRaises(ValueError):
                        gui_launch.require_unlocked()

    def test_unavailable_or_malformed_console_refuses(self):
        for result in (subprocess.TimeoutExpired("fake", 1), OSError("unavailable"),
                       subprocess.CompletedProcess([], 0, b"not a plist")):
            with patch.object(gui_launch.subprocess, "run") as run:
                if isinstance(result, Exception):
                    run.side_effect = result
                else:
                    run.return_value = result
                with self.assertRaises(ValueError):
                    gui_launch.require_unlocked()


if __name__ == "__main__":
    unittest.main()
