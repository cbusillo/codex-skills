#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Install and update behavior against fixture homes and real local Git remotes."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import catalog_runtime as runtime

spec = importlib.util.spec_from_file_location("installer", Path(__file__).with_name("install-catalog.py"))
assert spec and spec.loader
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def command(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = self.root / "catalog"
        self.catalog.mkdir()
        (self.catalog / "instructions").mkdir()
        (self.catalog / "instructions" / "global.md").write_text("# Shared\nUse the catalog.\n")
        (self.catalog / "hooks").mkdir()
        source = Path(__file__).resolve().parents[1] / "hooks" / "hooks.json"
        (self.catalog / "hooks" / "hooks.json").write_bytes(source.read_bytes())
        (self.catalog / "skills").mkdir()
        self.home = self.root / "home"
        self.codex, self.claude = self.home / ".codex", self.home / ".claude"
        self.codex.mkdir(parents=True)
        self.claude.mkdir()
        self.sync = installer.load_sync()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(installer, "ROOT", self.catalog).start()
        mock.patch.object(installer, "load_sync", return_value=self.sync).start()

    def install(self, write=True):
        return installer.install(self.home, self.codex, self.claude, write=write, updater=False)

    def test_fresh_install_twice_preserves_personal_settings_and_new_skills(self):
        (self.claude / "CLAUDE.md").write_text("Keep my history.\n")
        (self.codex / "AGENTS.md").write_text("Prefer concise replies.\n")
        unrelated = {"hooks": {"SessionStart": [{"hooks": [{"command": "my-hook"}]}]}}
        (self.codex / "hooks.json").write_text(json.dumps(unrelated))
        (self.codex / "config.toml").write_text('model = "my-model"\n')
        self.install()
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.install()
        after = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        for path in (self.claude / "CLAUDE.md", self.codex / "AGENTS.md"):
            text = path.read_text()
            self.assertIn("Keep my history.", text)
            self.assertIn("Prefer concise replies.", text)
        hooks = json.loads((self.codex / "hooks.json").read_text())["hooks"]
        self.assertEqual(hooks["SessionStart"][0], unrelated["hooks"]["SessionStart"][0])
        self.assertEqual((self.codex / "config.toml").read_text(), 'model = "my-model"\n')
        (self.catalog / "skills" / "new-skill").mkdir()
        self.assertTrue((self.home / ".agents" / "skills" / "new-skill").is_dir())
        self.assertEqual((self.claude / "skills" / "shared").resolve(), self.catalog.resolve())

    def test_source_update_preserves_personal_instructions_without_a_manual_merge(self):
        (self.codex / "AGENTS.md").write_text("Keep private instructions.\n")
        self.install()
        (self.catalog / "instructions" / "global.md").write_text("# New catalog instructions\n")
        self.install()
        generated = (self.codex / "AGENTS.md").read_text()
        self.assertIn("# New catalog instructions", generated)
        self.assertIn("Keep private instructions.", generated)
        self.assertNotIn("Use the catalog.", generated)

    def test_private_source_edits_and_shared_trims_do_not_restore_removed_text(self):
        (self.codex / "AGENTS.md").write_text("Private original.\n")
        self.install()
        local = self.catalog / ".local" / "global-instructions.md"
        local.write_text("Private replacement.\n")
        (self.catalog / "instructions" / "global.md").write_text("# Shared\n")
        self.install()
        content = (self.codex / "AGENTS.md").read_text()
        self.assertIn("Private replacement.", content)
        self.assertNotIn("Private original.", content)
        self.assertNotIn("Use the catalog.", content)
        self.assertEqual(local.read_text(), "Private replacement.\n")
        local.unlink()
        self.install()
        self.assertNotIn("Private replacement.", (self.codex / "AGENTS.md").read_text())

    def test_json_session_hook_is_preserved_without_duplicate(self):
        existing = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "uv run /catalog/hooks/direction_check_hook.py"}]}]}}
        (self.codex / "hooks.json").write_text(json.dumps(existing))
        self.install()
        self.assertEqual(json.loads((self.codex / "hooks.json").read_text())["hooks"]["SessionStart"], existing["hooks"]["SessionStart"])

    def test_failed_host_write_keeps_previous_base_receipt(self):
        self.install()
        receipt = self.catalog / ".local" / "catalog-global-source.md"
        previous = receipt.read_bytes()
        (self.catalog / "instructions" / "global.md").write_text("Different shared source.\n")
        original = self.sync.synchronize
        def fail_host(content, destinations, **kwargs):
            if kwargs.get("write") and self.codex / "AGENTS.md" in destinations:
                raise OSError("fixture host write failed")
            return original(content, destinations, **kwargs)
        with mock.patch.object(self.sync, "synchronize", side_effect=fail_host):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(receipt.read_bytes(), previous)
        self.install()
        self.assertIn("Different shared source", (self.codex / "AGENTS.md").read_text())

    def test_conflicting_binding_preflights_without_writes(self):
        path = self.claude / "skills" / "shared"
        path.parent.mkdir()
        path.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "Existing binding preserved"):
            self.install()
        self.assertFalse((self.home / ".agents").exists())
        self.assertFalse((self.codex / "AGENTS.md").exists())
        self.assertTrue(path.is_symlink())

    def test_invalid_hooks_and_symlink_instructions_do_not_partially_install(self):
        (self.codex / "hooks.json").write_text("not JSON")
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse((self.home / ".agents").exists())
        (self.codex / "hooks.json").unlink()
        (self.codex / "AGENTS.md").symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.install()
        self.assertFalse((self.home / ".agents").exists())

    def test_preview_writes_nothing_and_does_not_print_private_text(self):
        (self.claude / "CLAUDE.md").write_text("Private personal text")
        before = list(self.root.rglob("*"))
        result = self.install(write=False)
        self.assertEqual(before, list(self.root.rglob("*")))
        self.assertNotIn("Private personal text", json.dumps(result))

    def test_existing_legacy_session_hook_is_preserved_without_duplicate(self):
        config = '[[hooks.SessionStart]]\nmatcher = "startup"\n[[hooks.SessionStart.hooks]]\ncommand = "uv run /catalog/hooks/direction_check_hook.py"\n'
        (self.codex / "config.toml").write_text(config)
        self.install()
        self.assertNotIn("SessionStart", json.loads((self.codex / "hooks.json").read_text())["hooks"])
        self.assertEqual((self.codex / "config.toml").read_text(), config)

    def test_launchd_install_bootstraps_once_and_preserves_a_conflicting_job(self):
        def run(*, write=True):
            return installer.install(self.home, self.codex, self.claude, write=write, updater=True)
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(shutil, "which", return_value="/fixture/uv"), mock.patch.object(runtime, "checkout_state", return_value={"state": "current"}), mock.patch.object(subprocess, "run") as launchctl:
            launchctl.return_value.returncode = 1
            run()
            job = self.home / "Library" / "LaunchAgents" / f"{installer.LABEL}.plist"
            job_spec = plistlib.loads(job.read_bytes())
            self.assertEqual(job_spec["WorkingDirectory"], str(self.catalog))
            self.assertEqual(job_spec["ProgramArguments"][-1], "--update")
            self.assertTrue(any(call.args[0][1] == "bootstrap" for call in launchctl.call_args_list))
            self.assertEqual(job_spec["EnvironmentVariables"]["CODEX_HOME"], str(self.codex))
            self.assertEqual(job_spec["EnvironmentVariables"]["CLAUDE_CONFIG_DIR"], str(self.claude))
            launchctl.reset_mock()
            launchctl.return_value.returncode = 0
            before = job.read_bytes()
            run()
            self.assertEqual(job.read_bytes(), before)
            self.assertFalse(any(call.args[0][1] == "bootstrap" for call in launchctl.call_args_list))
            with mock.patch.dict(os.environ, {"PATH": "/fixture/changed"}):
                run()
            self.assertTrue(any(call.args[0][1] == "bootout" for call in launchctl.call_args_list))
            job.write_text("user-owned job")
            with self.assertRaisesRegex(ValueError, "Existing launchd job preserved"):
                run()
            self.assertEqual(job.read_text(), "user-owned job")

    def test_unknown_generated_instructions_are_reported_and_preserved(self):
        text = self.sync.HEADER + "\n\nOld catalog and private words.\n"
        (self.codex / "AGENTS.md").write_text(text)
        with self.assertRaisesRegex(ValueError, "Generated instructions differ"):
            self.install()
        self.assertEqual((self.codex / "AGENTS.md").read_text(), text)
        self.assertFalse((self.home / ".agents").exists())


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.origin = self.base / "origin.git"
        command("git", "init", "--bare", "-q", "--initial-branch=main", str(self.origin))
        self.seed = self.base / "seed"
        command("git", "clone", "-q", str(self.origin), str(self.seed))
        self.configure(self.seed)
        (self.seed / ".gitignore").write_text(".local/\n__pycache__/\n")
        (self.seed / "source").write_text("one")
        command("git", "add", ".", cwd=self.seed)
        command("git", "commit", "-qm", "first", cwd=self.seed)
        command("git", "push", "-q", "origin", "main", cwd=self.seed)
        self.checkout = self.base / "checkout"
        command("git", "clone", "-q", str(self.origin), str(self.checkout))
        self.configure(self.checkout)

    @staticmethod
    def configure(path):
        command("git", "config", "user.name", "Fixture", cwd=path)
        command("git", "config", "user.email", "fixture@example.test", cwd=path)

    def advance(self):
        (self.seed / "source").write_text("two")
        command("git", "commit", "-qam", "second", cwd=self.seed)
        command("git", "push", "-q", "origin", "main", cwd=self.seed)

    def test_clean_main_fast_forwards_and_status_goes_quiet(self):
        self.advance()
        command("git", "fetch", "-q", "origin", cwd=self.checkout)
        self.assertIn("Catalog stale", runtime.status_line(self.checkout))
        self.assertEqual(runtime.update(self.checkout)["state"], "current")
        self.assertEqual(runtime.git(self.checkout, "rev-parse", "HEAD"), runtime.git(self.seed, "rev-parse", "HEAD"))
        self.assertEqual(runtime.status_line(self.checkout), "")
        self.assertEqual(runtime.update(self.checkout)["state"], "current")

    def test_installed_update_refreshes_instructions_and_exposes_new_skill(self):
        source = Path(__file__).resolve().parent
        (self.seed / "scripts").mkdir()
        for name in ("install-catalog.py", "catalog_runtime.py", "sync-global-instructions.py"):
            shutil.copyfile(source / name, self.seed / "scripts" / name)
        (self.seed / "instructions").mkdir()
        (self.seed / "instructions" / "global.md").write_text("First shared instructions.\n")
        (self.seed / "hooks").mkdir()
        shutil.copyfile(source.parent / "hooks" / "hooks.json", self.seed / "hooks" / "hooks.json")
        (self.seed / "skills").mkdir()
        (self.seed / "skills" / "original").write_text("first")
        command("git", "add", ".", cwd=self.seed)
        command("git", "commit", "-qm", "install sources", cwd=self.seed)
        command("git", "push", "-q", "origin", "main", cwd=self.seed)
        self.assertEqual(runtime.update(self.checkout)["state"], "current")
        fixture_home = self.base / "home"
        fixture_home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(fixture_home), "CODEX_HOME": str(fixture_home / ".codex"), "CLAUDE_CONFIG_DIR": str(fixture_home / ".claude")}):
            command(sys.executable, str(self.checkout / "scripts" / "install-catalog.py"), "--write")
            (self.seed / "instructions" / "global.md").write_text("Updated shared instructions.\n")
            (self.seed / "skills" / "new").write_text("new")
            command("git", "add", ".", cwd=self.seed)
            command("git", "commit", "-qm", "catalog update", cwd=self.seed)
            command("git", "push", "-q", "origin", "main", cwd=self.seed)
            with mock.patch.dict(os.environ, {"HOME": str(self.base / "other-home"), "CODEX_HOME": "", "CLAUDE_CONFIG_DIR": ""}):
                self.assertEqual(runtime.update(self.checkout)["state"], "current")
            self.assertFalse((self.base / "other-home" / ".codex").exists())
            self.assertIn("Updated shared instructions", (fixture_home / ".codex" / "AGENTS.md").read_text())
            self.assertTrue((fixture_home / ".agents" / "skills" / "new").is_file())

    def test_dirty_untracked_off_main_detached_ahead_and_diverged_are_preserved(self):
        scenarios = ("dirty", "untracked", "branch", "detached", "ahead", "diverged")
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory(dir=self.base) as directory:
                checkout = Path(directory) / "catalog"
                command("git", "clone", "-q", str(self.origin), str(checkout))
                self.configure(checkout)
                if scenario == "dirty":
                    (checkout / "source").write_text("local")
                elif scenario == "untracked":
                    (checkout / "valuable").write_text("local")
                elif scenario == "branch":
                    command("git", "switch", "-qc", "task", cwd=checkout)
                elif scenario == "detached":
                    command("git", "checkout", "-q", "--detach", cwd=checkout)
                else:
                    (checkout / "source").write_text("local")
                    command("git", "commit", "-qam", "local", cwd=checkout)
                    if scenario == "diverged":
                        self.advance()
                        command("git", "fetch", "-q", "origin", cwd=checkout)
                head = runtime.git(checkout, "rev-parse", "HEAD")
                files = {p.name: p.read_bytes() for p in checkout.iterdir() if p.is_file()}
                self.assertIn(runtime.update(checkout)["state"], ("blocked", "error"))
                self.assertEqual(runtime.git(checkout, "rev-parse", "HEAD"), head)
                self.assertEqual({p.name: p.read_bytes() for p in checkout.iterdir() if p.is_file()}, files)
                self.assertIn("Catalog blocked", runtime.status_line(checkout))

    def test_fetch_failure_is_visible_and_recovery_clears_it(self):
        command("git", "remote", "set-url", "origin", str(self.base / "missing"), cwd=self.checkout)
        self.assertEqual(runtime.update(self.checkout)["state"], "error")
        self.assertIn("scheduled update failed", runtime.status_line(self.checkout))
        command("git", "remote", "set-url", "origin", str(self.origin), cwd=self.checkout)
        self.assertEqual(runtime.update(self.checkout)["state"], "current")
        self.assertEqual(runtime.status_line(self.checkout), "")

    def test_hook_emits_one_catalog_line_on_both_harnesses_only_when_needed(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
        from hooks import direction_check_hook as hook
        self.advance()
        command("git", "fetch", "-q", "origin", cwd=self.checkout)
        actual = runtime.status_line(self.checkout)
        (self.checkout / ".local").mkdir(exist_ok=True)
        (self.checkout / ".local" / "catalog-install.json").write_text("{}")
        for env in ({"CLAUDECODE": "1"}, {"CODEX_HOME": "/fixture"}):
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(runtime, "status_line", return_value=actual), mock.patch.object(hook, "direction_root", return_value=None), mock.patch.object(hook, "reminder", return_value=""):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(hook.main(catalog_root=self.checkout), 0)
                self.assertEqual(output.getvalue().count("Catalog stale:"), 1)
        runtime.update(self.checkout)
        self.assertEqual(runtime.status_line(self.checkout), "")


if __name__ == "__main__":
    unittest.main()
