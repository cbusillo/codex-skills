#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Global instructions preserve private sections and existing files on adoption."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("sync_global_instructions", Path(__file__).with_name("sync-global-instructions.py"))
assert SPEC and SPEC.loader
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class GlobalInstructionsTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
