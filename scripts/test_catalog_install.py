#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "tomlkit==0.15.1",
# ]
# ///
"""Install and update behavior against fixture homes and real local Git remotes."""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import importlib.util
import io
import json
import os
import plistlib
import shutil
import shlex
import subprocess
import sys
import tempfile
import tomllib
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
        shell_source = Path(__file__).resolve().parents[1] / "skills/supervisor/scripts/interactive-accounts.zsh"
        shipped = self.catalog / shell_source.relative_to(Path(__file__).resolve().parents[1])
        shipped.parent.mkdir(parents=True)
        shipped.write_bytes(shell_source.read_bytes())
        self.home = self.root / "home"
        self.codex, self.claude = self.home / ".codex", self.home / ".claude"
        self.codex.mkdir(parents=True)
        self.claude.mkdir()
        self.sync = installer.load_sync()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(installer, "ROOT", self.catalog).start()
        mock.patch.object(installer, "load_sync", return_value=self.sync).start()

    def with_compact(self, groups: list) -> list:
        rendered = json.loads(self.sync.render_codex_hook_content({}, Path("hooks.json"), self.catalog, include_session_start=True))
        return groups + [group for group in rendered["hooks"]["SessionStart"] if group.get("matcher") == "compact"]

    @staticmethod
    def legacy_alerts():
        # Historical install input, independent of what today's renderer emits.
        return {event: [{"hooks": [{"command": "uv run /legacy/catalog/hooks/session_alert_hook.py", "statusMessage": label}]}]
                for event, label in (("Stop", "codex-skills stop alert"),
                                     ("Interrupt", "codex-skills interrupt alert"))}

    def install(self, write=True):
        return installer.install(self.home, self.codex, self.claude, write=write, updater=False)

    def install_inline_hooks(self, target, hooks, **options):
        """Write hooks as an inline TOML binding at target, install, and return the written text and receipt."""
        import tomlkit

        text = tomlkit.dumps({"hooks": hooks})
        target.write_text(text)
        return text, installer.install(self.home, self.codex, self.claude, write=True,
                                       updater=False, refresh_instructions=True, **options)

    def assert_alert_binding_skipped(self, target, hooks):
        """Check the installer refuses to rewrite this binding; return its retained event hooks."""
        original, preview = self.install_inline_hooks(target, hooks, show_diff=True)
        entry = next(item for item in preview["outputs"] if "catalog_retired_toml" in item)
        self.assertEqual(entry["state"], "skipped")
        self.assertEqual(target.read_text(), original)
        return tomllib.loads(entry["catalog_retired_toml"])["hooks"]

    def assert_reconciled_alert_binding_kept(self, target, hooks):
        """Check the installer accepts the owner's reconciled binding unchanged."""
        reconciled, receipt = self.install_inline_hooks(target, hooks)
        self.assertFalse(any(item["state"] == "skipped" for item in receipt["outputs"]))
        self.assertEqual(target.read_text(), reconciled)
        self.assertFalse((self.codex / "hooks.json").exists())

    def test_fresh_install_twice_preserves_personal_settings_and_new_skills(self):
        (self.claude / "CLAUDE.md").write_text("Keep my history.\n")
        (self.codex / "AGENTS.md").write_text("Prefer concise replies.\n")
        unrelated = {"hooks": {"SessionStart": [{"hooks": [{"command": "my-hook"}]}]}}
        (self.codex / "hooks.json").write_text(json.dumps(unrelated))
        (self.codex / "config.toml").write_text('model = "my-model"\n')
        shell_config = self.home / ".zshrc"
        shell_config.write_text("# personal shell settings\n")
        receipt = self.install()
        source_line = receipt["interactive_shell"]["source_line"]
        command_name, shipped_path = shlex.split(source_line)
        self.assertEqual(command_name, "source")
        shipped = Path(shipped_path)
        self.assertTrue(shipped.is_file())
        self.assertTrue((self.home / ".agents/skills/shared" / shipped.relative_to(self.catalog / "skills")).is_file())
        self.assertEqual(shell_config.read_text(), "# personal shell settings\n")
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

    def test_linked_personal_instructions_are_adopted_without_changing_link_or_target(self):
        target = self.root / "personal-instructions.md"
        original = "Keep my linked instructions.\n"
        target.write_text(original)
        path = self.codex / "AGENTS.md"
        path.symlink_to(target)
        before = target.stat()
        preview = self.install(write=False)
        self.assertEqual(preview["outputs"][1]["state"], "preserved")
        self.assertFalse((self.catalog / ".local").exists())
        self.assertFalse((self.claude / "CLAUDE.md").exists())
        self.install()
        self.assertIn(original.strip(), (self.claude / "CLAUDE.md").read_text())
        self.assertIn(original.strip(), (self.catalog / ".local" / "global-instructions.md").read_text())
        snapshot = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.install()
        self.assertEqual(snapshot, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertEqual(path.readlink(), target)
        self.assertEqual(target.read_text(), original)
        self.assertEqual(target.stat().st_ino, before.st_ino)
        self.assertEqual(target.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertFalse(list(self.root.rglob("*.backup-*")))

    def test_linked_catalog_instructions_refresh_target_and_receipt_when_source_updates(self):
        self.install()
        path = self.claude / "CLAUDE.md"
        target = self.root / "generated-instructions.md"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
        self.assertEqual(self.install(write=False)["outputs"][0]["state"], "current")
        original = target.read_bytes()
        (self.catalog / "instructions" / "global.md").write_text("# Shared\nNew catalog instruction.\n")
        self.assertEqual(self.install(write=False)["outputs"][0]["state"], "would_write")
        self.install()
        receipt = json.loads((self.catalog / ".local" / "catalog-install.json").read_text())
        self.assertEqual(receipt["instruction_hashes"][str(path)], installer.hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertIn("New catalog instruction.", (self.codex / "AGENTS.md").read_text())
        self.install()
        self.assertEqual(self.install(write=False)["unmanaged_instruction_sources"], [])
        self.assertEqual(path.readlink(), target)
        self.assertIn("New catalog instruction.", target.read_text())
        self.assertEqual(list(self.root.rglob("generated-instructions.md.backup-*"))[0].read_bytes(), original)

    def test_account_home_links_share_one_generated_target_through_install_and_refresh(self):
        self.install()
        target = self.codex / "AGENTS.md"
        original = target.read_bytes()
        homes = [self.home / ".codex-accounts" / name for name in ("first", "second", "third")]
        for home in homes:
            home.mkdir(parents=True)
            (home / "AGENTS.md").symlink_to(target)
        installer.install(self.home, homes[0], self.claude, write=True, updater=False)
        source = self.catalog / "instructions" / "global.md"
        source.write_text("# Shared\nUpdated account rule.\n")
        preview = installer.install(self.home, homes[0], self.claude, write=False, updater=False)
        self.assertEqual(preview["outputs"][1]["state"], "would_write")
        self.assertEqual(target.read_bytes(), original)
        installer.install(self.home, homes[0], self.claude, write=True, updater=False, refresh_instructions=True)
        expected = self.sync.render(source, self.catalog / ".local" / "global-instructions.md")
        for home in homes:
            self.assertEqual((home / "AGENTS.md").read_text(), expected)
            self.assertEqual((home / "AGENTS.md").readlink(), target)
        backups = list(target.parent.glob("AGENTS.md.backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        before = target.stat().st_mtime_ns
        installer.install(self.home, homes[0], self.claude, write=True, updater=False)
        self.assertEqual(target.stat().st_mtime_ns, before)
        self.assertEqual(list(target.parent.glob("AGENTS.md.backup-*")), backups)
        self.assertEqual((self.claude / "CLAUDE.md").read_text(), expected)

    def test_generated_target_shared_by_both_outputs_is_refreshed_once(self):
        self.install()
        target = self.claude / "CLAUDE.md"
        path = self.codex / "AGENTS.md"
        path.unlink()
        path.symlink_to(target)
        (self.catalog / "instructions" / "global.md").write_text("New shared rule.\n")
        self.install()
        self.assertEqual(path.readlink(), target)
        self.assertIn("New shared rule.", target.read_text())
        self.assertEqual(len(list(target.parent.glob("CLAUDE.md.backup-*"))), 1)
        self.install()
        self.assertEqual(len(list(target.parent.glob("CLAUDE.md.backup-*"))), 1)

    def test_generated_link_hand_edit_and_missing_private_source_are_preserved(self):
        self.install()
        path = self.codex / "AGENTS.md"
        target = self.root / "generated.md"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
        target.write_text(target.read_text() + "Hand edit.\n")
        original = target.read_bytes()
        (self.catalog / "instructions" / "global.md").write_text("New shared rule.\n")
        self.assertEqual(self.install(write=False)["outputs"][1]["state"], "skipped")
        self.install()
        self.install()
        self.assertEqual(target.read_bytes(), original)
        (self.catalog / ".local" / "global-instructions.md").unlink()
        with self.assertRaisesRegex(ValueError, "Private instruction source is missing"):
            self.install()
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(path.readlink(), target)

    def test_linked_generated_crlf_target_refreshes_with_raw_backup(self):
        self.install()
        path = self.codex / "AGENTS.md"
        target = self.root / "generated.md"
        original = path.read_bytes().replace(b"\n", b"\r\n")
        target.write_bytes(original)
        path.unlink()
        path.symlink_to(target)
        self.assertEqual(self.install(write=False)["outputs"][1]["state"], "would_write")
        self.assertEqual(target.read_bytes(), original)
        self.install()
        self.assertEqual(target.read_bytes(), (self.claude / "CLAUDE.md").read_bytes())
        self.assertEqual(list(target.parent.glob("generated.md.backup-*"))[0].read_bytes(), original)
        self.assertEqual(path.readlink(), target)

    def test_read_only_generated_targets_are_preserved_without_stopping_install(self):
        self.install()
        path = self.codex / "AGENTS.md"
        target = self.root / "generated.md"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
        original = target.read_bytes()
        (self.catalog / "instructions" / "global.md").write_text("New shared rule.\n")
        for unavailable in (target, target.parent):
            with self.subTest(unavailable=unavailable), mock.patch("os.access", side_effect=lambda p, mode: p != unavailable.resolve()):
                self.assertEqual(self.install(write=False)["outputs"][1]["state"], "skipped")
                self.install()
                self.assertEqual(target.read_bytes(), original)
                self.assertIn("New shared rule.", (self.claude / "CLAUDE.md").read_text())
                self.assertEqual(path.readlink(), target)

    def test_generated_links_refresh_after_private_source_changes(self):
        self.install()
        path = self.codex / "AGENTS.md"
        target = self.root / "generated.md"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
        self.install()
        local = self.catalog / ".local" / "global-instructions.md"
        local.write_text("New private rule.\n")
        self.install()
        self.assertIn("New private rule.", target.read_text())
        self.assertEqual(target.read_text(), (self.claude / "CLAUDE.md").read_text())

    def test_legacy_link_multiple_catalog_updates_behind_is_recognized_from_history(self):
        command("git", "init", "-q", cwd=self.catalog)
        command("git", "config", "user.name", "Fixture", cwd=self.catalog)
        command("git", "config", "user.email", "fixture@example.invalid", cwd=self.catalog)
        command("git", "add", "instructions/global.md", cwd=self.catalog)
        command("git", "commit", "-qm", "original source", cwd=self.catalog)
        self.install()
        target = self.root / "generated.md"
        target.write_bytes((self.codex / "AGENTS.md").read_bytes())
        # Model the old installer: its receipt records skipped content and the
        # newest shared source while the actual linked output remains stale.
        source = self.catalog / "instructions" / "global.md"
        for text in ("Intermediate source.\n", "Newest source.\n"):
            source.write_text(text)
            command("git", "commit", "-qam", "source update", cwd=self.catalog)
            self.install()
        path = self.codex / "AGENTS.md"
        path.unlink()
        path.symlink_to(target)
        receipt_path = self.catalog / ".local" / "catalog-install.json"
        receipt = json.loads(receipt_path.read_text())
        receipt.pop("generated_instruction_hashes", None)
        receipt["instruction_hashes"][str(path)] = installer.hashlib.sha256(target.read_bytes()).hexdigest()
        receipt_path.write_text(json.dumps(receipt))
        self.install()
        self.assertIn("Newest source.", target.read_text())
        self.assertEqual(path.readlink(), target)

    def test_target_edit_between_preview_and_apply_refuses_overwrite(self):
        self.install()
        path = self.codex / "AGENTS.md"
        target = self.root / "generated.md"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
        (self.catalog / "instructions" / "global.md").write_text("New source.\n")
        original = target.read_text()
        synchronize = self.sync.synchronize_instructions
        def concurrent_edit(content, destinations, **kwargs):
            if kwargs["write"]:
                target.write_text(original + "Concurrent hand edit.\n")
            return synchronize(content, destinations, **kwargs)
        with mock.patch.dict(self.sync.__dict__, {"synchronize_instructions": concurrent_edit}):
            with self.assertRaisesRegex(ValueError, "changed since"):
                self.install()
        self.assertIn("Concurrent hand edit.", target.read_text())
        self.assertEqual(path.readlink(), target)

    def test_linked_source_edits_are_preserved_and_private_source_can_propagate_them(self):
        target = self.root / "personal-instructions.md"
        target.write_text("Old personal rule.\n")
        path = self.codex / "AGENTS.md"
        path.symlink_to(target)
        self.install()
        target.write_text("New personal rule.\n")
        self.install()
        local = self.catalog / ".local" / "global-instructions.md"
        self.assertEqual(local.read_text(), "Old personal rule.\n")
        local.write_text("New personal rule.\n")
        self.install()
        text = (self.claude / "CLAUDE.md").read_text()
        self.assertIn("New personal rule.", text)
        self.assertNotIn("Old personal rule.", text)
        self.assertEqual(target.read_text(), "New personal rule.\n")
        self.assertEqual(path.readlink(), target)

    def test_link_to_other_selected_instruction_preserves_target_and_repeat_after_catalog_update(self):
        target = self.claude / "CLAUDE.md"
        original = "Shared personal rule.\n"
        target.write_text(original)
        path = self.codex / "AGENTS.md"
        path.symlink_to(target)
        self.install()
        self.assertEqual(target.read_text(), original)
        (self.catalog / "instructions" / "global.md").write_text("# Shared\nNew catalog rule.\n")
        self.install()
        self.install()
        self.assertEqual(target.read_text(), original)
        self.assertEqual(path.readlink(), target)
        receipt = json.loads((self.catalog / ".local" / "catalog-install.json").read_text())
        for destination in (path, target):
            self.assertEqual(receipt["instruction_hashes"][str(destination)], installer.hashlib.sha256(destination.read_bytes()).hexdigest())

    def test_new_linked_generated_destination_is_preserved_without_adopting_unknown_private_text(self):
        self.install()
        local = self.catalog / ".local" / "global-instructions.md"
        local.write_text("Authoritative private rule.\n")
        self.install()
        for index, shared in enumerate(("Old unknown catalog rules.", "# Shared\nUse the catalog.")):
            with self.subTest(shared=shared):
                codex = self.home / f"new-codex-{index}"
                codex.mkdir()
                target = self.root / f"old-generated-{index}.md"
                original = self.sync.HEADER + "\n\n" + shared + "\n\nDifferent private rule.\n"
                target.write_text(original)
                (codex / "AGENTS.md").symlink_to(target)
                preview = installer.install(self.home, codex, self.claude, write=False, updater=False, show_diff=True)
                self.assertEqual(preview["outputs"][1]["state"], "skipped")
                self.assertIn("Authoritative private rule.", preview["outputs"][1]["diff"])
                installer.install(self.home, codex, self.claude, write=True, updater=False)
                self.assertEqual(target.read_text(), original)
                self.assertEqual(local.read_text(), "Authoritative private rule.\n")

    def test_link_to_catalog_shared_source_does_not_duplicate_it_as_private_text(self):
        shared = self.catalog / "instructions" / "global.md"
        (self.codex / "AGENTS.md").symlink_to(shared)
        original = shared.read_bytes()
        self.install()
        self.assertEqual((self.catalog / ".local" / "global-instructions.md").read_text(), "")
        self.assertEqual((self.claude / "CLAUDE.md").read_text(), self.sync.render(shared, self.catalog / ".local" / "global-instructions.md"))
        self.assertEqual(shared.read_bytes(), original)

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

    def test_chrome_config_is_preflighted_before_any_host_write(self):
        local = self.catalog / ".local"
        local.mkdir()
        (local / "chrome.toml").write_text('pinned_home = "/missing-account-home"\nhomes = []\n')
        with self.assertRaisesRegex(ValueError, "nonempty homes"):
            self.install()
        self.assertFalse((self.claude / "CLAUDE.md").exists())
        self.assertFalse((self.codex / "hooks.json").exists())
        self.assertFalse((self.claude / "skills" / "shared").exists())

    def test_installer_enrolls_configured_chrome_homes_but_refresh_does_not(self):
        import chrome_mcp
        local = self.catalog / ".local"
        local.mkdir()
        pinned = self.home / "extension-account"
        pinned.mkdir()
        (local / "chrome.toml").write_text(
            f'pinned_home = {json.dumps(str(pinned))}\nhomes = [{json.dumps(str(self.claude))}]\n')
        entries = {}

        def native(cli_command, directory, args):
            self.assertEqual(cli_command, '/fixture/bin/claude')
            if args[0] == "add-json":
                entries[directory] = json.loads(args[2])
                return subprocess.CompletedProcess(args, 0, "", "")
            entry = entries.get(directory)
            if entry is None:
                return subprocess.CompletedProcess(args, 1, f'No MCP server named "{chrome_mcp.SERVER}".', "")
            assert isinstance(entry, dict)
            text = "\n".join([f"{chrome_mcp.SERVER}:",
                "  Scope: User config (available in all your projects)", "  Type: stdio",
                f"  Command: {entry['command']}", f"  Args: {' '.join(entry['args'])}", "  Environment:",
                *(f"    {key}={value}" for key, value in entry['env'].items())])
            return subprocess.CompletedProcess(args, 0, text, "")

        with mock.patch.object(chrome_mcp, "run_mcp", side_effect=native), \
                mock.patch.object(shutil, 'which', return_value='/fixture/bin/claude'):
            preview = self.install(write=False)
            self.assertEqual(entries, {})
            self.assertEqual(len(preview["chrome_mcp"]), 2)
            self.install()
            self.assertEqual(set(entries), {self.claude, pinned})
            for installed_entry in entries.values():
                self.assertEqual(installed_entry["env"]["CLAUDE_CONFIG_DIR"], str(pinned))
        (local / "chrome.toml").write_text("invalid TOML [")
        # Automatic instruction refresh never adds or updates browser access.
        installer.install(self.home, self.codex, self.claude, write=True, updater=False,
                          refresh_instructions=True)

    def test_chrome_failure_does_not_record_an_unactivated_updater(self):
        import chrome_mcp
        with mock.patch.object(chrome_mcp, 'prepare', return_value={'entries': []}), \
                mock.patch.object(chrome_mcp, 'apply', side_effect=ValueError('fixture apply failed')), \
                mock.patch.object(sys, 'platform', 'darwin'), \
                mock.patch.object(shutil, 'which', return_value='/fixture/uv'), \
                mock.patch.object(runtime, 'checkout_state', return_value={'state': 'current'}), \
                mock.patch.object(subprocess, 'run') as launchctl:
            launchctl.return_value.returncode = 1
            with self.assertRaisesRegex(ValueError, 'fixture apply failed'):
                installer.install(self.home, self.codex, self.claude, write=True, updater=True)
        self.assertFalse((self.catalog / '.local' / 'catalog-install.json').exists())
        self.assertFalse((self.home / 'Library' / 'LaunchAgents' / f'{installer.LABEL}.plist').exists())
        self.assertFalse((self.claude / 'CLAUDE.md').exists())
        self.assertFalse((self.codex / 'AGENTS.md').exists())

    def test_failed_updater_preflight_does_not_start_chrome_health_checks(self):
        import chrome_mcp
        with mock.patch.object(chrome_mcp, 'prepare') as prepare, \
                mock.patch.object(sys, 'platform', 'darwin'), \
                mock.patch.object(shutil, 'which', return_value='/fixture/uv'), \
                mock.patch.object(runtime, 'checkout_state', return_value={'state': 'blocked', 'reason': 'dirty checkout'}):
            with self.assertRaisesRegex(ValueError, 'dirty checkout'):
                installer.install(self.home, self.codex, self.claude, write=False, updater=True)
        prepare.assert_not_called()

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
        hooks = json.loads((self.codex / "hooks.json").read_text())["hooks"]
        self.assertEqual(hooks["SessionStart"], self.with_compact(tomllib.loads(config)["hooks"]["SessionStart"]))
        self.assertNotIn("SessionStart", tomllib.loads((self.codex / "config.toml").read_text()).get("hooks", {}))
        backups = list(self.codex.glob("config.toml.backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), config)

    def test_first_install_can_preserve_symlinked_inline_hooks(self):
        target = self.root / "dotfiles.toml"
        original = '[[hooks.SessionStart]]\n[[hooks.SessionStart.hooks]]\ncommand="trusted-existing-hook"\n'
        target.write_text(original)
        (self.codex / "config.toml").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "skip-codex-hooks"):
            self.install()
        result = installer.install(self.home, self.codex, self.claude, write=True, updater=False, skip_codex_hooks=True)
        self.assertTrue((self.codex / "config.toml").is_symlink())
        self.assertEqual(target.read_text(), original)
        self.assertFalse((self.codex / "hooks.json").exists())
        self.assertTrue((self.home / ".agents" / "skills" / "shared").is_symlink())
        self.assertEqual(result["migrated_events"], {})

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

    def test_refresh_retires_alerts_preserving_other_hooks_and_trust(self):
        self.install()
        hook_path = self.codex / "hooks.json"
        existing = json.loads(hook_path.read_text())
        other = {"hooks": [{"type": "command", "command": "my-stop-hook"}]}
        existing["hooks"].update(self.legacy_alerts())
        existing["hooks"]["Stop"].append(other)
        hook_path.write_text(json.dumps(existing))
        config = self.codex / "config.toml"
        config.write_text('[hooks.state]\nopaque_trust = "preserve"\n')
        installer.install(self.home, self.codex, self.claude, write=True, updater=False, refresh_instructions=True)
        upgraded = json.loads(hook_path.read_text())
        self.assertEqual(upgraded["hooks"]["Stop"], [other])
        self.assertEqual(upgraded["hooks"]["Interrupt"], [])
        self.assertEqual(upgraded["hooks"]["PreToolUse"], existing["hooks"]["PreToolUse"])
        self.assertEqual(upgraded["hooks"]["SessionStart"], existing["hooks"]["SessionStart"])
        self.assertEqual(config.read_text(), '[hooks.state]\nopaque_trust = "preserve"\n')
        first = hook_path.read_bytes()
        installer.install(self.home, self.codex, self.claude, write=True, updater=False, refresh_instructions=True)
        self.assertEqual(hook_path.read_bytes(), first)

    def test_inline_obsolete_alerts_are_reported_even_with_current_json_hooks(self):
        import tomlkit

        self.install()
        config = self.codex / "config.toml"
        original = tomlkit.dumps({"hooks": self.legacy_alerts()})
        config.write_text(original)
        receipt = installer.install(self.home, self.codex, self.claude, write=True,
                                    updater=False, refresh_instructions=True, show_diff=True)
        self.assertEqual(config.read_text(), original)
        retained = next(entry["catalog_retired_toml"] for entry in receipt["outputs"]
                        if "catalog_retired_toml" in entry)
        self.assertFalse(any(tomllib.loads(retained)["hooks"].values()))

    def test_inline_policy_binding_does_not_add_obsolete_alerts(self):
        self.install()
        (self.codex / "hooks.json").unlink()
        config = self.codex / "config.toml"
        hooks = {"PreToolUse": [{"hooks": [{"command": "policy"}]}], "state": {"opaque_trust": "preserve"}}
        self.assert_reconciled_alert_binding_kept(config, hooks)

    def test_inline_dotfiles_retirement_preview_preserves_policy_and_trust(self):
        self.install()
        (self.codex / "hooks.json").unlink()
        config = self.codex / "config.toml"
        target = self.root / "dotfiles-config.toml"
        config.symlink_to(target)
        hooks = {**self.legacy_alerts(), "PreToolUse": [{"hooks": [{"command": "policy"}]}],
                 "state": {"opaque_trust": "preserve"}}
        remaining = self.assert_alert_binding_skipped(target, hooks)
        hooks.update(remaining)
        self.assertEqual(hooks["Stop"], [])
        self.assertEqual(hooks["Interrupt"], [])
        self.assert_reconciled_alert_binding_kept(target, hooks)
        self.assertTrue(config.is_symlink())

    def test_inline_duplicate_alerts_require_replacement_in_place(self):
        import copy

        self.install()
        hook_path = self.codex / "hooks.json"
        installed = json.loads(hook_path.read_text())
        hook_path.unlink()
        config = self.codex / "config.toml"
        target = self.root / "dotfiles-config.toml"
        config.symlink_to(target)
        generated = self.legacy_alerts()
        for stale_command in (False, True):
            with self.subTest(stale_command=stale_command):
                hooks = copy.deepcopy(installed["hooks"])
                old = copy.deepcopy(generated["Stop"][0])
                if stale_command:
                    old["hooks"][0]["command"] = "old-alert-command"
                personal = {"hooks": [{"command": "my-stop"}]}
                hooks["Stop"] = [old, personal, *generated["Stop"]]
                hooks["state"] = {"opaque_trust": "preserve"}
                current = self.assert_alert_binding_skipped(target, hooks)
                # The owner removes obsolete groups and retains the real hook.
                # Native trust state is never manufactured or rewritten.
                hooks.update(current)
                self.assertEqual(hooks["Stop"], [personal])
                self.assert_reconciled_alert_binding_kept(target, hooks)
                self.assertTrue(config.is_symlink())

    def test_alert_only_inline_binding_is_evaluated_without_writes(self):
        import copy

        self.install()
        hook_path = self.codex / "hooks.json"
        hook_path.unlink()
        config = self.codex / "config.toml"
        generated = self.legacy_alerts()
        for event in generated:
            with self.subTest(event=event):
                stale = copy.deepcopy(generated[event])
                stale[0]["hooks"][0]["command"] = "old-alert-command"
                current = self.assert_alert_binding_skipped(
                    config, {event: stale, "state": {"opaque_trust": "preserve"}})
                current["state"] = {"opaque_trust": "preserve"}
                self.assert_reconciled_alert_binding_kept(config, current)

    def test_malformed_alert_group_does_not_stop_instruction_refresh(self):
        self.install()
        path = self.codex / "hooks.json"
        config = json.loads(path.read_text())
        config["hooks"]["Stop"] = [{"hooks": None}, *self.legacy_alerts()["Stop"]]
        path.write_text(json.dumps(config))
        receipt = installer.install(self.home, self.codex, self.claude, write=True, updater=False, refresh_instructions=True)
        # A group the catalog can't read is left alone; the alert still retires.
        self.assertEqual(json.loads(path.read_text())["hooks"]["Stop"], [{"hooks": None}])
        self.assertFalse([entry for entry in receipt["outputs"] if entry.get("state") == "skipped"])

    def test_inline_trust_state_datetime_does_not_stop_instruction_refresh(self):
        self.install()
        config = self.codex / "config.toml"
        original = '[hooks.state]\nreviewed_at = 2026-10-17T07:32:00Z\n'
        config.write_text(original)
        receipt = installer.install(self.home, self.codex, self.claude, write=True, updater=False, refresh_instructions=True)
        self.assertEqual(config.read_text(), original)
        self.assertFalse([entry for entry in receipt["outputs"] if entry.get("state") == "skipped"])

    def test_concurrent_hook_edit_is_preserved_and_refresh_reports_skip(self):
        self.install()
        hook_path = self.codex / "hooks.json"
        original = json.loads(hook_path.read_text())
        original["hooks"].update(self.legacy_alerts())
        hook_path.write_text(json.dumps(original))
        user_hook = {"hooks": [{"type": "command", "command": "new-user-stop"}]}
        changed = {**original, "hooks": {**original["hooks"], "Stop": [user_hook]}}
        real_sync = self.sync.synchronize
        edited = False

        def synchronize(content, destinations, **kwargs):
            nonlocal edited
            if kwargs.get("write") and self.codex / "AGENTS.md" in destinations and not edited:
                hook_path.write_text(json.dumps(changed))
                edited = True
            return real_sync(content, destinations, **kwargs)

        with mock.patch.dict(vars(self.sync), {"synchronize": synchronize}):
            receipt = installer.install(self.home, self.codex, self.claude, write=True, updater=False, refresh_instructions=True)
        self.assertTrue(edited)
        self.assertEqual(json.loads(hook_path.read_text()), changed)
        self.assertTrue(any(entry["state"] == "skipped" for entry in receipt["outputs"]))
        self.assertTrue((self.codex / "AGENTS.md").is_file())

    def test_current_dotfiles_alert_binding_clears_skip_without_writes(self):
        self.install()
        hook_path = self.codex / "hooks.json"
        original = hook_path.read_text()
        target = self.root / "dotfiles-hooks"
        hook_path.unlink()
        target.write_text(original)
        hook_path.symlink_to(target)
        receipt = installer.install(self.home, self.codex, self.claude, write=True, updater=False, refresh_instructions=True)
        self.assertFalse(any(entry["state"] == "skipped" for entry in receipt["outputs"]))
        self.assertEqual(target.read_text(), original)
        self.assertTrue(hook_path.is_symlink())
        modified = json.loads(original)
        user_stop = {"hooks": [{"command": "personal-stop"}]}
        modified["hooks"].update(self.legacy_alerts())
        modified["hooks"]["Stop"].insert(0, user_stop)
        modified["hooks"]["Stop"][-1]["hooks"][0]["command"] = "old-alert-command"
        target.write_text(json.dumps(modified))
        receipt = installer.install(self.home, self.codex, self.claude, write=True, updater=False, refresh_instructions=True)
        self.assertTrue(any(entry["state"] == "skipped" for entry in receipt["outputs"]))
        self.assertEqual(json.loads(target.read_text()), modified)
        preview = installer.install(self.home, self.codex, self.claude, write=False, updater=False, refresh_instructions=True, show_diff=True)
        entries = next(entry["catalog_retired_events"] for entry in preview["outputs"] if "catalog_retired_events" in entry)
        self.assertEqual(json.loads(target.read_text()), modified)
        # A dotfiles manager can apply this preview without hand-writing command
        # strings. Unrelated existing events stay in its authoritative source.
        for event, generated in entries.items():
            modified["hooks"][event] = generated
        target.write_text(json.dumps(modified))
        receipt = installer.install(self.home, self.codex, self.claude, write=True, updater=False, refresh_instructions=True)
        self.assertFalse(any(entry["state"] == "skipped" for entry in receipt["outputs"]))
        self.assertEqual(json.loads(target.read_text())["hooks"]["Stop"][0], user_stop)

    def test_removed_binding_in_dotfiles_symlink_is_not_refreshed(self):
        self.install()
        hook_path = self.codex / "hooks.json"
        hook_path.unlink()
        target = self.root / "dotfiles-hooks"
        target.write_text('{"hooks": {}}\n')
        hook_path.symlink_to(target)
        receipt = installer.install(self.home, self.codex, self.claude, write=True, updater=False, refresh_instructions=True)
        self.assertFalse(any(entry["state"] == "skipped" for entry in receipt["outputs"]))
        self.assertTrue(hook_path.is_symlink())
        self.assertEqual(target.read_text(), '{"hooks": {}}\n')

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

    def test_ignored_files_allow_fast_forward_but_other_untracked_files_block(self):
        ignores = self.seed / ".gitignore"
        ignores.write_text(ignores.read_text() + "/*.log\n")
        command("git", "commit", "-qam", "catalog ignores", cwd=self.seed)
        command("git", "push", "-q", "origin", "main", cwd=self.seed)
        self.assertEqual(runtime.update(self.checkout)["state"], "current")
        logs = [self.checkout / name for name in ("error.log", "tool.log")]
        for log in logs:
            log.write_text("Tool output")
        self.advance()
        self.assertEqual(runtime.update(self.checkout)["state"], "current")
        self.assertEqual(runtime.git(self.checkout, "rev-parse", "HEAD"),
                         runtime.git(self.seed, "rev-parse", "HEAD"))
        for log in logs:
            self.assertEqual(log.read_text(), "Tool output")
        nested = self.checkout / "nested"
        nested.mkdir()
        for local in (self.checkout / "new_source.py", nested / "tool.log"):
            with self.subTest(path=local.relative_to(self.checkout)):
                local.write_text("Preserve this")
                before = runtime.git(self.checkout, "rev-parse", "HEAD")
                result = runtime.update(self.checkout)
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["blocking_paths"], [local.relative_to(self.checkout).as_posix()])
                self.assertEqual(runtime.git(self.checkout, "rev-parse", "HEAD"), before)
                self.assertEqual(local.read_text(), "Preserve this")
                local.unlink()

    def test_untracked_file_escalates_at_notice_threshold_in_startup_and_updater(self):
        local = self.checkout / "new_source.py"
        local.write_text("Preserve this")
        now = dt.datetime.now(dt.timezone.utc)
        start = now - runtime.BLOCKED_NOTICE_AFTER
        with mock.patch.object(runtime.dt, "datetime", wraps=dt.datetime) as clock:
            clock.now.return_value = start
            first = runtime.status_line(self.checkout, observe=True)
            self.assertEqual(len(first.splitlines()), 1)
            clock.now.return_value = now - dt.timedelta(seconds=1)
            self.assertEqual(len(runtime.status_line(self.checkout, observe=True).splitlines()), 1)
            self.assertNotIn("warning", runtime.update(self.checkout))
            clock.now.return_value = now
            line = runtime.status_line(self.checkout, observe=True)
            self.assertGreater(len(line.splitlines()), 1)
            result = runtime.update(self.checkout)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["warning"], line)
        self.assertIn(local.name, line)
        self.assertEqual(dt.datetime.fromisoformat(result["blocked_since"]), start)
        self.assertEqual(local.read_text(), "Preserve this")

    def test_blocked_age_survives_repeated_checks_escalates_and_resets_after_recovery(self):
        local = self.checkout / "local notes.txt"
        local.write_text("Preserve this")
        first = runtime.update(self.checkout)
        self.assertNotIn("warning", first)
        self.assertIn(local.name, runtime.status_line(self.checkout))
        receipt = self.checkout / ".local" / "catalog-update.json"
        start = dt.datetime.now(dt.timezone.utc) - runtime.BLOCKED_NOTICE_AFTER - dt.timedelta(hours=2)
        first["blocked_since"] = start.isoformat()
        receipt.write_text(json.dumps(first))
        before = receipt.read_bytes()
        line = runtime.status_line(self.checkout)
        self.assertGreater(len(line.splitlines()), 1)
        self.assertIn(str(self.checkout), line)
        self.assertIn(local.name, line)
        self.assertEqual(receipt.read_bytes(), before)  # Startup remains read-only.
        second = runtime.update(self.checkout)
        self.assertEqual(dt.datetime.fromisoformat(second["blocked_since"]), start)
        self.assertIn(local.name, second["warning"])
        self.assertEqual(local.read_text(), "Preserve this")
        local.unlink()
        recovered = runtime.update(self.checkout)
        self.assertNotIn("blocked_since", recovered)
        self.assertNotIn("warning", recovered)
        self.assertEqual(runtime.status_line(self.checkout), "")
        local.write_text("Another local edit")
        new = runtime.update(self.checkout)
        self.assertNotEqual(dt.datetime.fromisoformat(new["blocked_since"]), start)
        self.assertNotIn("warning", new)

    def test_notice_threshold_and_legacy_or_invalid_receipt_times(self):
        (self.checkout / "source").write_text("Local change")
        state = runtime.checkout_state(self.checkout)
        now = dt.datetime.now(dt.timezone.utc)
        start = now - runtime.BLOCKED_NOTICE_AFTER
        self.assertEqual(runtime.blocked_notice(self.checkout, state, start, now - dt.timedelta(seconds=1)), "")
        self.assertTrue(runtime.blocked_notice(self.checkout, state, start, now))
        receipt = self.checkout / ".local" / "catalog-update.json"
        receipt.parent.mkdir()
        receipt.write_text(json.dumps({"state": "blocked", "checked_at": start.isoformat()}))
        self.assertEqual(dt.datetime.fromisoformat(runtime.update(self.checkout)["blocked_since"]), start)
        for stamp in ("invalid", now.replace(tzinfo=None).isoformat(), (now + dt.timedelta(days=2)).isoformat()):
            with self.subTest(stamp=stamp):
                receipt.write_text(json.dumps({"state": "blocked", "checked_at": stamp}))
                result = runtime.update(self.checkout)
                self.assertNotIn("warning", result)
                self.assertIsNotNone(dt.datetime.fromisoformat(result["blocked_since"]).tzinfo)

    def test_blocking_paths_handle_renames_spaces_and_control_characters(self):
        destination = "renamed\nnotes.txt"
        command("git", "mv", "source", destination, cwd=self.checkout)
        (self.checkout / "untracked notes.txt").write_text("Private content")
        (self.checkout / "résumé.txt").write_text("Local content")
        result = runtime.update(self.checkout)
        self.assertEqual(set(result["blocking_paths"]), {"source", destination, "untracked notes.txt", "résumé.txt"})
        line = runtime.status_line(self.checkout)
        self.assertIn("résumé.txt", line)
        self.assertEqual(len(line.splitlines()), 1)
        self.assertNotIn("Private content", line)

    def test_update_cli_escalates_in_existing_stderr_log_and_json_receipt(self):
        (self.checkout / "source").write_text("Local change")
        first = runtime.update(self.checkout)
        first["blocked_since"] = (dt.datetime.now(dt.timezone.utc) - runtime.BLOCKED_NOTICE_AFTER).isoformat()
        (self.checkout / ".local" / "catalog-update.json").write_text(json.dumps(first))
        script = Path(__file__).with_name("catalog_runtime.py")
        result = subprocess.run([sys.executable, str(script), "--catalog", str(self.checkout), "--update"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        state = json.loads(result.stdout)
        self.assertIn(state["warning"], result.stderr)
        self.assertIn("source", state["warning"])

    def test_path_lists_are_bounded_and_report_omitted_files(self):
        folder = self.checkout / "unpacked"
        folder.mkdir()
        for number in range(runtime.MAX_BLOCKING_PATHS + 3):
            (folder / f"file-{number:04}.txt").write_text("Local content")
        state = runtime.update(self.checkout)
        self.assertEqual(len(state["blocking_paths"]), runtime.MAX_BLOCKING_PATHS)
        self.assertEqual(state["blocking_paths_omitted"], 3)
        paths_text = runtime.blocking_paths_text(state)
        count_text = paths_text
        for path in state["blocking_paths"]:
            count_text = count_text.replace(json.dumps(path, ensure_ascii=False), "")
        self.assertIn(str(state["blocking_paths_omitted"]), count_text)
        self.assertIn(paths_text, runtime.status_line(self.checkout))
        state["blocked_since"] = (dt.datetime.now(dt.timezone.utc) - runtime.BLOCKED_NOTICE_AFTER).isoformat()
        (self.checkout / ".local" / "catalog-update.json").write_text(json.dumps(state))
        warning = runtime.update(self.checkout)["warning"]
        self.assertIn(paths_text, warning)
        for path in state["blocking_paths"]:
            self.assertIn(path, warning)
        self.assertNotIn(f"file-{runtime.MAX_BLOCKING_PATHS:04}.txt", warning)

    def test_evolving_block_keeps_observation_and_bad_receipt_keeps_paths_visible(self):
        local = self.checkout / "first.txt"
        local.write_text("Local")
        state = runtime.update(self.checkout)
        state["blocked_since"] = (dt.datetime.now(dt.timezone.utc) - runtime.BLOCKED_NOTICE_AFTER).isoformat()
        receipt = self.checkout / ".local" / "catalog-update.json"
        receipt.write_text(json.dumps(state))
        local.unlink()
        (self.checkout / "second.txt").write_text("New local")
        self.assertGreater(len(runtime.status_line(self.checkout).splitlines()), 1)
        new = runtime.update(self.checkout)
        self.assertEqual(dt.datetime.fromisoformat(new["blocked_since"]), dt.datetime.fromisoformat(state["blocked_since"]))
        self.assertIn("second.txt", new["warning"])
        self.advance()
        command("git", "fetch", "-q", "origin", cwd=self.checkout)
        for content in ("invalid JSON", "[]"):
            receipt.write_text(content)
            self.assertIn("second.txt", runtime.status_line(self.checkout))
            (self.checkout / "second.txt").unlink()
            self.assertIn("Catalog stale", runtime.status_line(self.checkout))
            (self.checkout / "second.txt").write_text("New local")

    def test_long_block_notice_reaches_both_harnesses_through_existing_startup_hook(self):
        (self.checkout / "source").write_text("Local change")
        state = runtime.update(self.checkout)
        state["blocked_since"] = (dt.datetime.now(dt.timezone.utc) - runtime.BLOCKED_NOTICE_AFTER).isoformat()
        local = self.checkout / ".local"
        (local / "catalog-update.json").write_text(json.dumps(state))
        (local / "catalog-install.json").write_text("{}")
        hook = Path(__file__).resolve().parents[1] / "hooks" / "direction_check_hook.py"
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                env = {"PATH": os.environ["PATH"], "DIRECTION_MARKER": str(local / "direction.json")}
                if harness == "claude":
                    env["CLAUDECODE"] = "1"
                result = subprocess.run([sys.executable, str(hook), "--catalog-root", str(self.checkout)],
                                        cwd=self.checkout, env=env, capture_output=True, text=True, check=True)
                line = runtime.status_line(self.checkout)
                self.assertTrue(line)
                self.assertIn(line, result.stdout)

    def test_startup_only_observations_escalate_and_clear_without_faking_updater_checks(self):
        (self.checkout / "source").write_text("Local change")
        start = dt.datetime.now(dt.timezone.utc) - runtime.BLOCKED_NOTICE_AFTER
        with mock.patch.object(runtime.dt, "datetime", wraps=dt.datetime) as clock:
            clock.now.return_value = start
            first = runtime.status_line(self.checkout, observe=True)
        self.assertEqual(len(first.splitlines()), 1)
        receipt = self.checkout / ".local" / "catalog-update.json"
        record = json.loads(receipt.read_text())
        self.assertEqual(dt.datetime.fromisoformat(record["blocked_since"]), start)
        self.assertNotIn("checked_at", record)
        self.assertGreater(len(runtime.status_line(self.checkout, observe=True).splitlines()), 1)
        (self.checkout / "source").write_text("one")
        self.assertEqual(runtime.status_line(self.checkout, observe=True), "")
        self.assertNotIn("blocked_since", json.loads(receipt.read_text()))
        (self.checkout / "source").write_text("Another change")
        self.assertEqual(len(runtime.status_line(self.checkout, observe=True).splitlines()), 1)
        self.assertNotEqual(dt.datetime.fromisoformat(json.loads(receipt.read_text())["blocked_since"]), start)

    def test_unavailable_observation_storage_does_not_hide_readable_checkout_status(self):
        self.assertEqual(runtime.status_line(self.checkout, observe=True), "")
        receipt = self.checkout / ".local" / "catalog-update.json"
        before = receipt.stat()
        self.assertEqual(runtime.status_line(self.checkout, observe=True), "")
        self.assertEqual(receipt.stat().st_mtime_ns, before.st_mtime_ns)
        with mock.patch.object(runtime, "observe_checkout", side_effect=OSError("read-only filesystem")):
            self.assertEqual(runtime.status_line(self.checkout, observe=True), "")
            (self.checkout / "source").write_text("Local change")
            self.assertIn("source", runtime.status_line(self.checkout, observe=True))

    def test_failed_checkout_observation_does_not_erase_a_known_block(self):
        (self.checkout / "source").write_text("Local change")
        state = runtime.update(self.checkout)
        start = dt.datetime.now(dt.timezone.utc) - runtime.BLOCKED_NOTICE_AFTER
        state["blocked_since"] = start.isoformat()
        (self.checkout / ".local" / "catalog-update.json").write_text(json.dumps(state))
        with mock.patch.object(runtime, "checkout_state", side_effect=subprocess.TimeoutExpired("git status", 60)):
            self.assertEqual(runtime.update(self.checkout)["state"], "error")
        recovered_read = runtime.update(self.checkout)
        self.assertEqual(dt.datetime.fromisoformat(recovered_read["blocked_since"]), start)
        self.assertTrue(recovered_read["warning"])

    def test_startup_observation_preserves_overdue_schedule_and_does_not_race_update_lock(self):
        local = self.checkout / ".local"
        local.mkdir()
        job = self.base / "job.plist"
        job.write_text("fixture")
        stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat()
        (local / "catalog-install.json").write_text(json.dumps({"scheduled_updater": True, "updater_plist": str(job), "scheduled_at": stamp}))
        self.assertIn("Catalog stale", runtime.status_line(self.checkout, observe=True))
        receipt = local / "catalog-update.json"
        record = {"state": "current", "checked_at": stamp}
        receipt.write_text(json.dumps(record))
        self.assertIn("Catalog stale", runtime.status_line(self.checkout, observe=True))
        self.assertEqual(json.loads(receipt.read_text())["checked_at"], stamp)
        before = receipt.read_bytes()
        (self.checkout / "source").write_text("Local change")
        with runtime.update_lock_path(self.checkout).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn("source", runtime.status_line(self.checkout, observe=True))
        self.assertEqual(receipt.read_bytes(), before)

    def test_installed_update_refreshes_instructions_and_exposes_new_skill(self):
        source = Path(__file__).resolve().parent
        (self.seed / "scripts").mkdir()
        for name in ("install-catalog.py", "chrome_mcp.py", "catalog_runtime.py", "sync-global-instructions.py"):
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
            # Exercise the real subprocess output -> update receipt -> status
            # path, including clearing the diagnostic after reconciliation.
            hooks.write_text("invalid JSON")
            skipped = runtime.update(self.checkout)
            self.assertEqual(skipped["state"], "current")
            self.assertIn("alert_refresh", skipped)
            self.assertIn("Catalog notice", runtime.status_line(self.checkout))
            hooks.write_text('{"hooks": {}}\n')
            fixed = runtime.update(self.checkout)
            self.assertNotIn("alert_refresh", fixed)
            self.assertEqual(runtime.status_line(self.checkout), "")
            agents = fixture_home / ".codex" / "AGENTS.md"
            target = fixture_home / "linked-generated.md"
            original = agents.read_bytes()
            target.write_bytes(original)
            agents.unlink()
            agents.symlink_to(target)
            (self.seed / "instructions" / "global.md").write_text("Next catalog instructions.\n")
            command("git", "commit", "-qam", "next catalog update", cwd=self.seed)
            command("git", "push", "-q", "origin", "main", cwd=self.seed)
            refreshed = runtime.update(self.checkout)
            self.assertNotIn("instruction_refresh", refreshed)
            self.assertEqual(runtime.status_line(self.checkout), "")
            self.assertIn("Next catalog instructions.", target.read_text())
            self.assertTrue(agents.is_symlink())
            target.write_text(target.read_text() + "Hand edit.\n")
            hooks.write_text("invalid JSON")
            both = runtime.update(self.checkout)
            self.assertIn("instruction_refresh", both)
            self.assertIn("alert_refresh", both)
            self.assertIn("instruction refresh", runtime.status_line(self.checkout))
            # The skipped alert retirement adds its own recovery to the notice.
            receipt = self.checkout / ".local" / "catalog-update.json"
            recorded = json.loads(receipt.read_text())
            line = runtime.status_line(self.checkout)
            receipt.write_text(json.dumps({key: value for key, value in recorded.items() if key != "alert_refresh"}))
            self.assertNotEqual(runtime.status_line(self.checkout), line)
            receipt.write_text(json.dumps(recorded))
            self.assertIn("Hand edit.", target.read_text())
            hooks.write_text('{"hooks": {}}\n')
            target.write_bytes((fixture_home / ".claude" / "CLAUDE.md").read_bytes())
            self.assertNotIn("instruction_refresh", runtime.update(self.checkout))
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

    def test_skipped_alert_refresh_is_visible_without_changing_checkout(self):
        self.assertEqual(runtime.update(self.checkout)["state"], "current")
        receipt = self.checkout / ".local" / "catalog-update.json"
        status = json.loads(receipt.read_text())
        status["alert_refresh"] = "skipped"
        receipt.write_text(json.dumps(status))
        install = self.checkout / ".local" / "catalog-install.json"
        install.write_text(json.dumps({"scheduled_updater": False}))
        head = command("git", "rev-parse", "HEAD", cwd=self.checkout)
        line = runtime.status_line(self.checkout)
        self.assertIn("--refresh-instructions", line)
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=self.checkout), head)
        job = self.base / "job.plist"
        job.write_text("fixture")
        install.write_text(json.dumps({"scheduled_updater": True, "updater_plist": str(job)}))
        status["checked_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
        receipt.write_text(json.dumps(status))
        self.assertIn("Catalog stale", runtime.status_line(self.checkout))
        install.write_text(json.dumps({"scheduled_updater": False}))
        status.pop("alert_refresh")
        receipt.write_text(json.dumps(status))
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
                    self.assertIn(str(hook.LOOP_PATH), output.getvalue())
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
