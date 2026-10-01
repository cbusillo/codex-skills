#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["tomlkit==0.15.1"]
# ///
"""Global instructions preserve private sections and existing files on adoption."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("sync_global_instructions", Path(__file__).with_name("sync-global-instructions.py"))
assert SPEC and SPEC.loader
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class GlobalInstructionsTests(unittest.TestCase):
    def test_mixed_hooks_migrate_with_preview_backup_trust_and_idempotence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory)
            config = codex / "config.toml"
            original = f'''# Personal setting\nmodel = "personal-model"\n
[[hooks.SessionStart]]
matcher = "startup|resume|clear"
[[hooks.SessionStart.hooks]]
type = "command"
command = "uv run {sync.ROOT / 'hooks' / 'direction_check_hook.py'}"
timeout = 15

[[hooks.Stop]]
matcher = "*"
[[hooks.Stop.hooks]]
type = "command"
command = "my-stop"

# Codex-managed trust, preserve it verbatim.
[hooks.state."source-bound-id"]
approved = true
hash = "existing-hash"
'''
            config.write_text(original)
            hook_path = codex / "hooks.json"
            hook_path.write_text(sync.render_codex_hook(hook_path))
            old_json = hook_path.read_bytes()
            expected = tomllib.loads(original)
            plan = sync.prepare_codex_hooks(codex)
            self.assertEqual(config.read_text(), original)
            self.assertEqual(hook_path.read_bytes(), old_json)
            hooks = json.loads(plan[hook_path])["hooks"]
            self.assertEqual(hooks["SessionStart"], expected["hooks"]["SessionStart"])
            self.assertEqual(hooks["Stop"], expected["hooks"]["Stop"])
            self.assertEqual(len(hooks["PreToolUse"]), 1)
            receipt = sync.write_codex_hooks(plan, codex)
            for item in receipt:
                if "backup" in item:
                    self.assertEqual(Path(item["backup"]).stat().st_mode & 0o777, 0o600)
            cleaned = tomllib.loads(config.read_text())
            self.assertEqual(cleaned["hooks"], {"state": expected["hooks"]["state"]})
            self.assertEqual(cleaned["model"], expected["model"])
            self.assertIn(original[original.index('[hooks.state.'):], config.read_text())
            second = sync.prepare_codex_hooks(codex)
            self.assertEqual(list(second), [hook_path])
            self.assertTrue(all(item["state"] == "current" for item in sync.write_codex_hooks(second, codex)))
            self.assertEqual(len(list(codex.glob("*.backup-*"))), 2)

    def test_fresh_hook_setup_registers_both_events_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory)
            first = sync.prepare_codex_hooks(codex)
            hook_path = codex / "hooks.json"
            self.assertEqual(set(json.loads(first[hook_path])["hooks"]), {"SessionStart", "PreToolUse"})
            sync.write_codex_hooks(first, codex)
            self.assertEqual(sync.prepare_codex_hooks(codex), first)
            self.assertFalse((codex / "config.toml").exists())

    def test_conflicts_malformed_hooks_and_symlink_migration_refuse_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory)
            config, hook_path = codex / "config.toml", codex / "hooks.json"
            inline = '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ncommand = "my-stop"\n'
            config.write_text(inline)
            duplicate = {"hooks": {"Stop": [{"hooks": [{"command": "my-stop"}]}]}}
            hook_path.write_text(json.dumps(duplicate))
            before = hook_path.read_bytes()
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                sync.prepare_codex_hooks(codex)
            self.assertEqual(hook_path.read_bytes(), before)
            self.assertEqual(config.read_text(), inline)
            hook_path.write_text('{"hooks":{"Stop":[null]}}')
            with self.assertRaises(ValueError):
                sync.prepare_codex_hooks(codex)
            hook_path.write_text('{}')
            target = codex / "actual.toml"
            config.rename(target)
            config.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlink"):
                sync.prepare_codex_hooks(codex)
            self.assertEqual(target.read_text(), inline)

    def test_hook_sources_changed_after_preview_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory)
            outputs = sync.prepare_codex_hooks(codex)
            config = codex / "config.toml"
            changed = '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ncommand = "added-after-preview"\n'
            config.write_text(changed)
            with self.assertRaisesRegex(ValueError, "changed during preparation"):
                sync.write_codex_hooks(outputs, codex)
            self.assertFalse((codex / "hooks.json").exists())
            self.assertEqual(config.read_text(), changed)

    def test_explicit_host_directories_receive_identical_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex, claude = root / "codex-config", root / "claude-config"
            subprocess.run([sys.executable, str(Path(sync.__file__)), "--codex-dir", str(codex), "--claude-dir", str(claude), "--local-source", str(root / "absent.md"), "--write"], check=True, capture_output=True)
            self.assertEqual((codex / "AGENTS.md").read_bytes(), (claude / "CLAUDE.md").read_bytes())

    def test_codex_registration_preserves_other_hooks_and_reuses_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            other = {"matcher": "Read", "hooks": [{"type": "command", "command": "true"}]}
            path.write_text(json.dumps({"hooks": {"PreToolUse": [other], "Stop": [other]}}))
            first = sync.render_codex_hook(path)
            path.write_text(first)
            self.assertEqual(first, sync.render_codex_hook(path))
            hooks = json.loads(first)["hooks"]
            self.assertEqual(hooks["Stop"], [other])
            self.assertEqual(hooks["PreToolUse"][0], other)
            self.assertIn("command_policy_hook.py", hooks["PreToolUse"][1]["hooks"][0]["command"])

    def test_preview_adoption_backup_and_idempotence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, local = root / "shared.md", root / "private.md"
            source.write_text("# Shared rules\n")
            local.write_text("## Host rules\nKeep this local.\n")
            targets = [root / "claude.md", root / "codex.md"]
            targets[0].write_text("original\n")
            content = sync.render(source, local)
            self.assertIn("Keep this local.", content)
            self.assertTrue(all(entry["state"] == "would_write" for entry in sync.synchronize(content, targets, write=False)))
            self.assertEqual(targets[0].read_text(), "original\n")
            self.assertFalse(targets[1].exists())
            receipt = sync.synchronize(content, targets, write=True)
            self.assertEqual(Path(receipt[0]["backup"]).read_text(), "original\n")
            self.assertEqual(targets[0].read_bytes(), targets[1].read_bytes())
            self.assertTrue(all(entry["state"] == "current" for entry in sync.synchronize(content, targets, write=True)))
            self.assertEqual(len(list(root.glob("*.backup-*"))), 1)

    def test_unsafe_second_destination_leaves_first_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first", root / "second"
            first.write_text("preserve\n")
            second.symlink_to(first)
            with self.assertRaises(ValueError):
                sync.synchronize("new\n", [first, second], write=True)
            self.assertEqual(first.read_text(), "preserve\n")



    
    def test_missing_local_source_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, local = root / "shared.md", root / "private.md"
            source.write_text("# Shared rules\n")
            targets = [root / "claude.md"]
            targets[0].write_text("HEADER\n\n# Shared rules\n\n## Host rules\nKeep this local.\n")

            # Preview reports refusal
            content_str = sync.render(source, local)
            preview = sync.synchronize(content_str, targets, write=False, local_source_missing=True, allow_missing_local=False)
            self.assertEqual(preview[0]["state"], "refused")
            self.assertIn("refusal", preview[0])

            # Write is refused
            with self.assertRaises(ValueError):
                sync.synchronize(content_str, targets, write=True, local_source_missing=True, allow_missing_local=False)

            # Runtime checkout (local source exists) writes normally
            # In a real run, content_str would include the local file content if it existed.
            # Here we just verify the flag allows the write to proceed.
            receipt = sync.synchronize(content_str, targets, write=True, local_source_missing=False, allow_missing_local=False)
            self.assertEqual(receipt[0]["state"], "written")

            # Reset target
            targets[0].write_text("HEADER\n\n# Shared rules\n\n## Host rules\nKeep this local.\n")

            # Explicit override proceeds even if local source is missing
            receipt = sync.synchronize(content_str, targets, write=True, local_source_missing=True, allow_missing_local=True)
            self.assertEqual(receipt[0]["state"], "written")

    def test_missing_local_source_refusal_leaves_first_destination_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, local = root / "shared.md", root / "private.md"
            source.write_text("# Shared rules\n")
            targets = [root / "claude.md", root / "codex.md"]
            # First target doesn't exist. Second target differs.
            targets[1].write_text("HEADER\n\n# Shared rules\n\n## Host rules\nKeep this local.\n")

            content_str = sync.render(source, local)
            with self.assertRaises(ValueError):
                sync.synchronize(content_str, targets, write=True, local_source_missing=True, allow_missing_local=False)
            self.assertFalse(targets[0].exists())


if __name__ == "__main__":
    unittest.main()
