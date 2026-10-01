#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Install and update behavior against fixture homes and real local Git remotes."""
from __future__ import annotations

import contextlib
import datetime as dt
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
        self.assertTrue((self.home / ".agents" / "skills" / "shared" / "new-skill").is_dir())
        self.assertEqual((self.claude / "skills" / "shared").resolve(), self.catalog.resolve())

    def test_personal_skills_coexist_and_existing_whole_catalog_binding_is_kept(self):
        skills = self.home / ".agents" / "skills"
        personal = skills / "personal" / "SKILL.md"
        personal.parent.mkdir(parents=True)
        personal.write_text("Personal instructions.\n")
        self.install()
        self.install()
        self.assertEqual(personal.read_text(), "Personal instructions.\n")
        self.assertEqual((skills / "shared").resolve(), (self.catalog / "skills").resolve())
        (skills / "shared").unlink()
        personal.unlink()
        personal.parent.rmdir()
        skills.rmdir()
        skills.symlink_to(self.catalog / "skills", target_is_directory=True)
        self.install()
        self.assertEqual(skills.resolve(), (self.catalog / "skills").resolve())
        self.assertFalse((self.catalog / "skills" / "shared").exists())

    def test_conflicting_personal_shared_binding_is_preserved_without_writes(self):
        shared = self.home / ".agents" / "skills" / "shared"
        shared.mkdir(parents=True)
        (shared / "SKILL.md").write_text("Unrelated skill.\n")
        with self.assertRaisesRegex(ValueError, "Existing binding preserved"):
            self.install()
        self.assertEqual((shared / "SKILL.md").read_text(), "Unrelated skill.\n")
        self.assertFalse((self.claude / "CLAUDE.md").exists())

    def test_shared_personal_directory_collision_is_reported_before_writes(self):
        skills = self.home / ".agents" / "skills"
        skills.mkdir(parents=True)
        (self.claude / "skills").symlink_to(skills, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "bindings collide"):
            self.install()
        self.assertFalse((skills / "shared").exists())
        self.assertFalse((self.catalog / ".local").exists())
        self.assertFalse((self.codex / "AGENTS.md").exists())

    def test_personal_directory_inside_checkout_is_preserved_without_writes(self):
        skills = self.home / ".agents" / "skills"
        skills.parent.mkdir(parents=True)
        for target in (self.catalog, self.catalog / "hooks"):
            with self.subTest(target=target):
                skills.symlink_to(target, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, "overlaps the catalog"):
                    self.install()
                self.assertFalse((target / "shared").exists())
                self.assertFalse((self.catalog / ".local").exists())
                skills.unlink()

    def test_generated_tail_with_no_private_source_is_never_guessed(self):
        path = self.codex / "AGENTS.md"
        text = self.sync.render(self.catalog / "instructions" / "global.md", self.catalog / "missing") + "\nRemoved shared paragraph.\n"
        path.write_text(text)
        with self.assertRaisesRegex(ValueError, "Ambiguous generated instructions"):
            self.install()
        self.assertEqual(path.read_text(), text)
        self.assertFalse((self.catalog / ".local").exists())

    def test_legacy_codex_home_binding_keeps_discovery_without_a_nested_link(self):
        (self.codex / "skills").symlink_to(self.catalog / "skills", target_is_directory=True)
        (self.catalog / "skills" / ".system").mkdir()
        self.install()
        self.install()
        self.assertFalse((self.home / ".agents").exists())
        self.assertEqual((self.codex / "skills").resolve(), (self.catalog / "skills").resolve())
        self.assertEqual((self.claude / "skills" / "shared").resolve(), self.catalog.resolve())

    def test_system_cache_without_legacy_binding_is_reported_before_writes(self):
        (self.catalog / "skills" / ".system").mkdir()
        with self.assertRaisesRegex(ValueError, "system cache conflicts"):
            self.install()
        self.assertFalse((self.catalog / ".local").exists())
        self.assertFalse((self.home / ".agents").exists())

    def test_legacy_binding_to_another_catalog_is_reported_before_writes(self):
        other = self.root / "other-catalog"
        (other / "skills").mkdir(parents=True)
        (other / "instructions").mkdir()
        (other / "instructions" / "global.md").write_text("Other source.\n")
        (other / "scripts").mkdir()
        (other / "scripts" / "sync-global-instructions.py").write_text("# Catalog synchronizer\n")
        legacy = self.codex / "skills"
        legacy.symlink_to(other / "skills", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Existing binding preserved"):
            self.install()
        self.assertEqual(legacy.resolve(), (other / "skills").resolve())
        self.assertFalse((self.catalog / ".local").exists())
        self.assertFalse((self.home / ".agents").exists())

    def test_new_host_generated_edits_are_preserved_on_reconfiguration(self):
        self.install()
        other = self.home / "other-codex"
        other.mkdir()
        text = (self.codex / "AGENTS.md").read_text() + "\nPrivate host edit.\n"
        (other / "AGENTS.md").write_text(text)
        with self.assertRaisesRegex(ValueError, "differ from the private source"):
            installer.install(self.home, other, self.claude, write=True, updater=False)
        self.assertEqual((other / "AGENTS.md").read_text(), text)
        self.assertFalse((other / "hooks.json").exists())

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
        with self.assertRaisesRegex(ValueError, "Private instruction source is missing"):
            self.install()
        self.assertIn("Private replacement.", (self.codex / "AGENTS.md").read_text())
        local.write_text("")
        self.install()
        self.assertNotIn("Private replacement.", (self.codex / "AGENTS.md").read_text())

    def test_json_session_hook_is_preserved_without_duplicate(self):
        existing = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": f"uv run {self.catalog / 'hooks' / 'direction_check_hook.py'}"}]}]}}
        (self.codex / "hooks.json").write_text(json.dumps(existing))
        self.install()
        self.assertEqual(json.loads((self.codex / "hooks.json").read_text())["hooks"]["SessionStart"], existing["hooks"]["SessionStart"])

    def test_failed_host_write_keeps_previous_base_receipt(self):
        self.install()
        receipt = self.catalog / ".local" / "catalog-global-source.md"
        previous = receipt.read_bytes()
        (self.catalog / "instructions" / "global.md").write_text("Different shared source.\n")
        original = getattr(self.sync, "synchronize")
        def fail_host(content, destinations, **kwargs):
            if kwargs.get("write") and self.codex / "AGENTS.md" in destinations:
                raise OSError("fixture host write failed")
            return original(content, destinations, **kwargs)
        with mock.patch.dict(vars(self.sync), {"synchronize": fail_host}):
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
        config = f'[[hooks.SessionStart]]\nmatcher = "startup"\n[[hooks.SessionStart.hooks]]\ncommand = "uv run {self.catalog / "hooks" / "direction_check_hook.py"}"\n'
        (self.codex / "config.toml").write_text(config)
        self.install()
        self.assertNotIn("SessionStart", json.loads((self.codex / "hooks.json").read_text())["hooks"])
        self.assertEqual((self.codex / "config.toml").read_text(), config)

    def test_installed_host_edits_are_reported_without_overwriting_and_can_be_adopted(self):
        self.install()
        host = self.codex / "AGENTS.md"
        edited = host.read_text() + "New personal rule.\n"
        host.write_text(edited)
        with self.assertRaisesRegex(ValueError, "Installed instructions changed"):
            self.install()
        self.assertEqual(host.read_text(), edited)
        # Explicitly reconcile the edit through the documented private source.
        local = self.catalog / ".local" / "global-instructions.md"
        local.write_text("New personal rule.\n")
        getattr(self.sync, "synchronize")(self.sync.render(self.catalog / "instructions" / "global.md", local), [host], write=True)
        self.install()
        self.assertIn("New personal rule.", (self.claude / "CLAUDE.md").read_text())

    def test_launchd_failure_keeps_install_receipts_and_can_recover_after_a_pull(self):
        def launch(command_args, **_kwargs):
            if command_args[1] == "bootstrap":
                raise subprocess.CalledProcessError(1, command_args)
            return subprocess.CompletedProcess(command_args, 1)
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(shutil, "which", return_value="/fixture/uv"), mock.patch.object(runtime, "checkout_state", return_value={"state": "current"}), mock.patch.object(subprocess, "run", side_effect=launch):
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                installer.install(self.home, self.codex, self.claude, write=True, updater=True)
        self.assertTrue((self.catalog / ".local" / "catalog-install.json").is_file())
        self.assertTrue((self.catalog / ".local" / "catalog-global-source.md").is_file())
        (self.catalog / "instructions" / "global.md").write_text("Pulled shared instructions.\n")
        self.install()
        self.assertIn("Pulled shared instructions.", (self.codex / "AGENTS.md").read_text())

    def test_stale_legacy_hook_is_reported_without_partial_install(self):
        existing = {"hooks": {"SessionStart": [{"hooks": [{"command": "uv run /deleted-worktree/hooks/direction_check_hook.py"}]}]}}
        (self.codex / "hooks.json").write_text(json.dumps(existing))
        with self.assertRaisesRegex(ValueError, "Existing session hook preserved"):
            self.install()
        self.assertFalse((self.codex / "AGENTS.md").exists())
        self.assertEqual(json.loads((self.codex / "hooks.json").read_text()), existing)

    def test_manual_sync_from_an_older_committed_source_is_adopted(self):
        command("git", "init", "-q", str(self.catalog))
        UpdateTests.configure(self.catalog)
        command("git", "add", "instructions/global.md", cwd=self.catalog)
        command("git", "commit", "-qm", "original instructions", cwd=self.catalog)
        local = self.catalog / ".local" / "global-instructions.md"
        local.parent.mkdir()
        local.write_text("Private instruction.\n")
        old = self.sync.render(self.catalog / "instructions" / "global.md", local)
        (self.codex / "AGENTS.md").write_text(old)
        (self.catalog / "instructions" / "global.md").write_text("# Shared\n")
        command("git", "commit", "-qam", "update instructions", cwd=self.catalog)
        self.install()
        content = (self.codex / "AGENTS.md").read_text()
        self.assertIn("# Shared", content)
        self.assertIn("Private instruction.", content)
        self.assertNotIn("Use the catalog.", content)

    def test_first_adoption_identifies_removed_shared_text_before_importing_private_text(self):
        command("git", "init", "-q", str(self.catalog))
        UpdateTests.configure(self.catalog)
        command("git", "add", "instructions/global.md", cwd=self.catalog)
        command("git", "commit", "-qm", "original instructions", cwd=self.catalog)
        original = self.sync.render(self.catalog / "instructions" / "global.md", self.root / "absent")
        (self.codex / "AGENTS.md").write_text(original + "Private rule.\n")
        (self.catalog / "instructions" / "global.md").write_text("# Shared\n")
        command("git", "commit", "-qam", "trim shared instructions", cwd=self.catalog)
        with self.assertRaisesRegex(ValueError, "Ambiguous generated instructions"):
            self.install()
        self.assertEqual((self.codex / "AGENTS.md").read_text(), original + "Private rule.\n")
        local = self.catalog / ".local" / "global-instructions.md"
        local.parent.mkdir()
        local.write_text("Private rule.\n")
        self.install()
        content = (self.codex / "AGENTS.md").read_text()
        self.assertIn("Private rule.", content)
        self.assertNotIn("Use the catalog.", content)
        self.assertEqual((self.catalog / ".local" / "global-instructions.md").read_text(), "Private rule.\n")

    def test_first_adoption_preserves_unsynchronized_manual_instruction_edits(self):
        local = self.catalog / ".local" / "global-instructions.md"
        local.parent.mkdir()
        local.write_text("Existing private instruction.\n")
        host = self.codex / "AGENTS.md"
        original = self.sync.render(self.catalog / "instructions" / "global.md", local)
        host.write_text(original + "Hand-added rule.\n")
        with self.assertRaisesRegex(ValueError, "Generated instructions differ"):
            self.install()
        self.assertEqual(host.read_text(), original + "Hand-added rule.\n")
        host.write_text(original)
        local.write_text("Changed private instruction.\n")
        with self.assertRaisesRegex(ValueError, "Generated instructions differ"):
            self.install()
        self.assertEqual(host.read_text(), original)

    def test_shared_text_moved_to_private_source_is_not_guessed_on_a_fresh_clone(self):
        shared = self.catalog / "instructions" / "global.md"
        shared.write_text("# Shared\n\nMoved rule.\n")
        command("git", "init", "-q", str(self.catalog))
        UpdateTests.configure(self.catalog)
        command("git", "add", "instructions/global.md", cwd=self.catalog)
        command("git", "commit", "-qm", "old shared source", cwd=self.catalog)
        original = self.sync.render(shared, self.root / "absent")
        shared.write_text("# Shared\n")
        command("git", "commit", "-qam", "move shared rule to private", cwd=self.catalog)
        host = self.codex / "AGENTS.md"
        host.write_text(original)
        with self.assertRaisesRegex(ValueError, "Ambiguous generated instructions"):
            self.install()
        self.assertEqual(host.read_text(), original)
        local = self.catalog / ".local" / "global-instructions.md"
        local.parent.mkdir()
        local.write_text("Moved rule.\n")
        self.install()
        self.assertIn("Moved rule.", host.read_text())

    def test_refresh_upgrades_bound_alerts_preserving_other_hooks_and_trust(self):
        self.install()
        hook_path = self.codex / "hooks.json"
        existing = json.loads(hook_path.read_text())
        # Emulate an older installed catalog without alert registrations.
        existing["hooks"].pop("Stop")
        existing["hooks"].pop("Interrupt")
        other = {"hooks": [{"type": "command", "command": "my-stop-hook"}]}
        existing["hooks"]["Stop"] = [other]
        hook_path.write_text(json.dumps(existing))
        config = self.codex / "config.toml"
        config.write_text('[hooks.state]\nopaque_trust = "preserve"\n')
        installer.install(self.home, self.codex, self.claude, write=True, updater=False, refresh_instructions=True)
        upgraded = json.loads(hook_path.read_text())
        self.assertEqual(upgraded["hooks"]["Stop"][0], other)
        self.assertEqual(upgraded["hooks"]["PreToolUse"], existing["hooks"]["PreToolUse"])
        self.assertEqual(upgraded["hooks"]["SessionStart"], existing["hooks"]["SessionStart"])
        self.assertEqual(len(upgraded["hooks"]["Stop"]), 2)
        self.assertEqual(len(upgraded["hooks"]["Interrupt"]), 1)
        self.assertEqual(config.read_text(), '[hooks.state]\nopaque_trust = "preserve"\n')
        first = hook_path.read_bytes()
        installer.install(self.home, self.codex, self.claude, write=True, updater=False, refresh_instructions=True)
        self.assertEqual(hook_path.read_bytes(), first)

    def test_unsafe_alert_destination_does_not_block_instruction_refresh(self):
        self.install()
        hook_path = self.codex / "hooks.json"
        hook_path.unlink()
        target = self.root / "dotfiles-hooks"
        target.write_text("preserve")
        hook_path.symlink_to(target)
        (self.catalog / "instructions" / "global.md").write_text("Updated instructions.\n")
        receipt = installer.install(self.home, self.codex, self.claude, write=True, updater=False, refresh_instructions=True)
        self.assertEqual(target.read_text(), "preserve")
        self.assertTrue(hook_path.is_symlink())
        self.assertTrue(any(entry["state"] == "skipped" for entry in receipt["outputs"]))
        self.assertIn("Updated instructions.", (self.codex / "AGENTS.md").read_text())

    def test_refresh_keeps_removed_bindings_and_hooks_removed(self):
        self.install()
        (self.claude / "skills" / "shared").unlink()
        (self.home / ".agents" / "skills" / "shared").unlink()
        (self.codex / "hooks.json").write_text('{"hooks": {}}\n')
        (self.catalog / "instructions" / "global.md").write_text("Updated instructions.\n")
        installer.install(self.home, self.codex, self.claude, write=True, updater=False, refresh_instructions=True)
        self.assertFalse((self.claude / "skills" / "shared").exists())
        self.assertFalse((self.home / ".agents" / "skills" / "shared").exists())
        self.assertEqual((self.codex / "hooks.json").read_text(), '{"hooks": {}}\n')
        self.assertIn("Updated instructions.", (self.codex / "AGENTS.md").read_text())

    def test_existing_hook_expands_home_and_shell_variables(self):
        for token in ("~", "$HOME"):
            with self.subTest(token=token), mock.patch.dict(os.environ, {"HOME": str(self.root)}):
                entry = {"hooks": {"SessionStart": [{"hooks": [{"command": f"uv run {token}/catalog/hooks/direction_check_hook.py"}]}]}}
                (self.codex / "hooks.json").write_text(json.dumps(entry))
                self.install()
                self.assertEqual(json.loads((self.codex / "hooks.json").read_text())["hooks"]["SessionStart"], entry["hooks"]["SessionStart"])

    def test_existing_env_wrapped_hook_resolves_its_own_plugin_root(self):
        hook = {"hooks": {"SessionStart": [{"hooks": [{"command": f"env CLAUDE_PLUGIN_ROOT={self.catalog} sh -c 'uv run \"${{CLAUDE_PLUGIN_ROOT}}/hooks/direction_check_hook.py\"'"}]}]}}
        (self.codex / "hooks.json").write_text(json.dumps(hook))
        self.install()
        self.assertEqual(json.loads((self.codex / "hooks.json").read_text())["hooks"]["SessionStart"], hook["hooks"]["SessionStart"])

    def test_login_shell_and_shell_options_preserve_correct_hooks(self):
        for shell in ("bash -lc", "zsh -lc", "bash -l -c", "bash --noprofile -c", "bash -o pipefail -c", "fish -c"):
            with self.subTest(shell=shell):
                entry = {"hooks": {"SessionStart": [{"hooks": [{"command": f"{shell} 'uv run {self.catalog}/hooks/direction_check_hook.py'"}]}]}}
                (self.codex / "hooks.json").write_text(json.dumps(entry))
                self.install()
                self.assertEqual(json.loads((self.codex / "hooks.json").read_text())["hooks"]["SessionStart"], entry["hooks"]["SessionStart"])

    def test_relative_hook_paths_are_not_mistaken_for_this_catalog(self):
        previous = Path.cwd()
        try:
            os.chdir(self.catalog)
            entry = {"hooks": {"SessionStart": [{"hooks": [{"command": "sh -c 'cd /old/catalog && uv run hooks/direction_check_hook.py'"}]}]}}
            (self.codex / "hooks.json").write_text(json.dumps(entry))
            with self.assertRaisesRegex(ValueError, "Existing session hook preserved"):
                self.install()
        finally:
            os.chdir(previous)
        self.assertFalse((self.codex / "AGENTS.md").exists())

    def test_explicit_host_destination_change_is_reported_and_reconfigures_the_pair(self):
        self.install()
        before = (self.codex / "AGENTS.md").read_bytes()
        other = self.home / ".codex-profile"
        preview = installer.install(self.home, other, self.claude, write=False, updater=False)
        self.assertEqual(preview["configuration_change"]["previous"]["codex"], str(self.codex))
        self.assertEqual(preview["configuration_change"]["requested"]["codex"], str(other))
        self.assertFalse(other.exists())
        installer.install(self.home, other, self.claude, write=True, updater=False)
        receipt = json.loads((self.catalog / ".local" / "catalog-install.json").read_text())
        self.assertEqual(receipt["codex"], str(other))
        self.assertEqual((self.codex / "AGENTS.md").read_bytes(), before)

    def test_preview_reports_both_unmanaged_instruction_sources_without_exposing_text(self):
        (self.claude / "CLAUDE.md").write_text("Shared rule.\nClaude rule.\n")
        (self.codex / "AGENTS.md").write_text("Shared rule.\nCodex rule.\n")
        result = self.install(write=False)
        self.assertEqual(set(result["unmanaged_instruction_sources"]), {str(self.claude / "CLAUDE.md"), str(self.codex / "AGENTS.md")})
        self.assertNotIn("Shared rule.", json.dumps(result))

    def test_a_different_home_does_not_redirect_an_existing_installation(self):
        self.install()
        receipt = self.catalog / ".local" / "catalog-install.json"
        previous = receipt.read_bytes()
        with self.assertRaisesRegex(ValueError, "Installed home differs"):
            installer.install(self.root / "fixture-home", self.root / "fixture-home/.codex", self.root / "fixture-home/.claude", write=True, updater=False)
        self.assertEqual(receipt.read_bytes(), previous)
        self.assertFalse((self.root / "fixture-home").exists())

    def test_read_only_codex_config_symlink_is_preserved(self):
        target = self.root / "managed-config.toml"
        target.write_text('model = "configured-model"\n')
        config = self.codex / "config.toml"
        config.symlink_to(target)
        self.install()
        self.assertTrue(config.is_symlink())
        self.assertEqual(target.read_text(), 'model = "configured-model"\n')

    def test_invalid_session_handlers_are_reported_before_writes(self):
        (self.codex / "hooks.json").write_text('{"hooks":{"SessionStart":[{"hooks":null}]}}')
        with self.assertRaisesRegex(ValueError, "Invalid SessionStart"):
            self.install()
        self.assertFalse((self.codex / "AGENTS.md").exists())

    def test_fixture_cli_cannot_activate_a_real_launchd_updater(self):
        script = Path(__file__).with_name("install-catalog.py")
        result = subprocess.run([sys.executable, str(script), "--home-dir", str(self.home), "--updater", "--write"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.home / ".agents").exists())

    def test_loaded_job_without_an_owned_plist_is_preserved_before_writes(self):
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(shutil, "which", return_value="/fixture/uv"), mock.patch.object(runtime, "checkout_state", return_value={"state": "current"}), mock.patch.object(subprocess, "run") as launchctl:
            launchctl.return_value.returncode = 0
            with self.assertRaisesRegex(ValueError, "Existing loaded launchd job preserved"):
                installer.install(self.home, self.codex, self.claude, write=True, updater=True)
            self.assertTrue(all(call.args[0][1] == "print" for call in launchctl.call_args_list))
            self.assertFalse((self.codex / "AGENTS.md").exists())

    def test_personal_skills_directory_symlink_is_kept(self):
        skills = self.home / ".agents" / "skills"
        skills.parent.mkdir()
        personal = self.root / "personal-skills"
        personal.mkdir()
        (personal / "SKILL.md").write_text("Personal skill.\n")
        skills.symlink_to(personal, target_is_directory=True)
        self.install()
        self.assertTrue(skills.is_symlink())
        self.assertEqual((personal / "SKILL.md").read_text(), "Personal skill.\n")
        self.assertEqual((personal / "shared").resolve(), (self.catalog / "skills").resolve())

    def test_another_catalog_directory_is_not_changed_through_a_host_link(self):
        other = self.root / "another-catalog"
        shutil.copytree(self.catalog, other)
        (other / "scripts").mkdir()
        shutil.copyfile(Path(__file__).with_name("sync-global-instructions.py"), other / "scripts" / "sync-global-instructions.py")
        skills = self.home / ".agents" / "skills"
        skills.parent.mkdir()
        skills.symlink_to(other / "skills", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Existing binding preserved"):
            self.install()
        self.assertFalse((other / "skills" / "shared").exists())

    def test_claude_whole_catalog_directory_is_not_modified(self):
        (self.catalog / "scripts").mkdir()
        shutil.copyfile(Path(__file__).with_name("sync-global-instructions.py"), self.catalog / "scripts" / "sync-global-instructions.py")
        (self.claude / "skills").symlink_to(self.catalog / "skills", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Existing catalog folder preserved"):
            self.install()
        self.assertFalse((self.catalog / "skills" / "shared").exists())

    def test_catalog_source_cannot_be_used_as_a_host_configuration_home(self):
        source = self.catalog / "AGENTS.md"
        source.write_text("Preserve repository instructions.\n")
        previous = source.read_bytes()
        with self.assertRaisesRegex(ValueError, "Host configuration overlaps"):
            installer.install(self.home, self.catalog, self.claude, write=True, updater=False)
        self.assertEqual(source.read_bytes(), previous)
        self.assertFalse((self.catalog / ".local").exists())

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
            (fixture_home / ".claude" / "skills" / "shared").unlink()
            hooks = fixture_home / ".codex" / "hooks.json"
            hooks.write_text('{"hooks": {}}\n')
            command("git", "fetch", "-q", "origin", cwd=self.checkout)
            command("git", "merge", "--ff-only", "origin/main", cwd=self.checkout)
            self.assertIn("Catalog stale", runtime.status_line(self.checkout))
            with mock.patch.dict(os.environ, {"HOME": str(self.base / "other-home"), "CODEX_HOME": "", "CLAUDE_CONFIG_DIR": ""}):
                self.assertEqual(runtime.update(self.checkout)["state"], "current")
            self.assertFalse((self.base / "other-home" / ".codex").exists())
            self.assertIn("Updated shared instructions", (fixture_home / ".codex" / "AGENTS.md").read_text())
            self.assertTrue((fixture_home / ".agents" / "skills" / "shared" / "new").is_file())
            self.assertFalse((fixture_home / ".claude" / "skills" / "shared").exists())
            self.assertEqual(hooks.read_text(), '{"hooks": {}}\n')
            self.assertEqual(runtime.status_line(self.checkout), "")

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
        self.assertIn("Catalog blocked", runtime.status_line(self.checkout))
        command("git", "remote", "set-url", "origin", str(self.origin), cwd=self.checkout)
        self.assertEqual(runtime.update(self.checkout)["state"], "current")
        self.assertEqual(runtime.status_line(self.checkout), "")

    def test_manual_update_does_not_imply_an_overdue_schedule(self):
        self.assertEqual(runtime.update(self.checkout)["state"], "current")
        receipt = self.checkout / ".local" / "catalog-update.json"
        status = json.loads(receipt.read_text())
        status["checked_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
        receipt.write_text(json.dumps(status))
        self.assertEqual(runtime.status_line(self.checkout), "")
        install = self.checkout / ".local" / "catalog-install.json"
        install.write_text(json.dumps({"scheduled_updater": False}))
        self.assertEqual(runtime.status_line(self.checkout), "")
        job = self.base / "job.plist"
        job.write_text("fixture")
        install.write_text(json.dumps({"scheduled_updater": True, "updater_plist": str(job)}))
        self.assertIn("Catalog stale", runtime.status_line(self.checkout))
        job.unlink()
        self.assertEqual(runtime.status_line(self.checkout), "")

    def test_scheduled_update_that_never_ran_becomes_visible(self):
        local = self.checkout / ".local"
        local.mkdir()
        job = self.base / "job.plist"
        job.write_text("fixture")
        installed = {"scheduled_updater": True, "updater_plist": str(job), "scheduled_at": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()}
        (local / "catalog-install.json").write_text(json.dumps(installed))
        self.assertIn("Catalog stale", runtime.status_line(self.checkout))
        installed["scheduled_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        (local / "catalog-install.json").write_text(json.dumps(installed))
        self.assertEqual(runtime.status_line(self.checkout), "")

    def test_installed_linked_worktree_reports_stale_state(self):
        command("git", "switch", "-qc", "task", cwd=self.checkout)
        linked = self.base / "linked"
        command("git", "worktree", "add", "-q", str(linked), "main", cwd=self.checkout)
        self.advance()
        command("git", "fetch", "-q", "origin", cwd=self.checkout)
        self.assertEqual(runtime.status_line(linked), "")
        (linked / ".local").mkdir()
        (linked / ".local" / "catalog-install.json").write_text("{}")
        self.assertIn("Catalog stale", runtime.status_line(linked))

    def test_malformed_status_reports_blocked_without_hiding_other_hook_output(self):
        from hooks import direction_check_hook as hook
        local = self.checkout / ".local"
        local.mkdir()
        installation = local / "catalog-install.json"
        update = local / "catalog-update.json"
        cases = (("[]", None), ("null", None), ("{}", "[]"), ("{}", "null"), ("{}", '{"checked_at": "2026-01-01T00:00:00"}'))
        for install_text, update_text in cases:
            with self.subTest(install=install_text, update=update_text):
                installation.write_text(install_text)
                if update_text is None:
                    update.unlink(missing_ok=True)
                else:
                    update.write_text(update_text)
                self.assertIn("Catalog blocked", runtime.status_line(self.checkout))
                with mock.patch.object(hook, "direction_root", return_value=self.checkout), mock.patch.object(hook, "reminder", return_value="Fixture overdue reminder."):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(hook.main(catalog_root=self.checkout), 0)
                    self.assertIn("Catalog blocked", output.getvalue())
                    self.assertIn(hook.LOOP_PATH.read_text().strip(), output.getvalue())
                    self.assertIn("Fixture overdue reminder.", output.getvalue())

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
        # This receipt was only a hook-gating fixture, not a real installation.
        (self.checkout / ".local" / "catalog-install.json").unlink()
        runtime.update(self.checkout)
        self.assertEqual(runtime.status_line(self.checkout), "")


if __name__ == "__main__":
    unittest.main()
