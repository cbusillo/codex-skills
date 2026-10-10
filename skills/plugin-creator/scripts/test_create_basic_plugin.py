#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise plugin creation and registration through its CLI."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("create_basic_plugin.py")


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)

    def run_plugin(self, *args, cwd=None, success=True):
        result = subprocess.run([sys.executable, str(SCRIPT), "notes", *args], cwd=cwd or self.root, capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def entry(self):
        return json.loads((self.root / ".agents/plugins/marketplace.json").read_text())["plugins"][0]

    def test_registration_and_policy_update_preserve_every_plugin_file(self):
        self.run_plugin("--with-mcp", "--with-apps")
        plugin = self.root / "plugins/notes"
        manifest = plugin / ".codex-plugin/plugin.json"
        value = json.loads(manifest.read_text())
        value["description"] = "Owner content"
        manifest.write_text(json.dumps(value))
        (plugin / ".mcp.json").write_text('{"mcpServers":{"custom":{"command":"fixture"}}}')
        before = {p.relative_to(plugin): p.read_bytes() for p in plugin.rglob("*") if p.is_file()}
        self.run_plugin("--register-only")
        self.run_plugin("--register-only", "--force", "--auth-policy", "ON_USE")
        self.assertEqual(self.entry()["policy"]["authentication"], "ON_USE")
        self.assertEqual(before, {p.relative_to(plugin): p.read_bytes() for p in plugin.rglob("*") if p.is_file()})

    def test_subdirectory_defaults_use_repository_root(self):
        subdirectory = self.root / "src/deep"
        subdirectory.mkdir(parents=True)
        self.run_plugin("--with-marketplace", cwd=subdirectory)
        self.assertTrue((self.root / "plugins/notes/.codex-plugin/plugin.json").is_file())
        self.assertEqual((self.root / self.entry()["source"]["path"]).resolve(), self.root / "plugins/notes")
        self.assertFalse((subdirectory / "plugins").exists())

    def test_custom_parent_and_marketplace_resolve_to_actual_destination(self):
        parent = self.root / "custom plugin parent"
        marketplace = self.root / "another/.agents/plugins/marketplace.json"
        self.run_plugin("--path", str(parent), "--marketplace-path", str(marketplace), "--with-marketplace")
        entry = json.loads(marketplace.read_text())["plugins"][0]
        self.assertEqual((marketplace.parent.parent.parent / entry["source"]["path"]).resolve(), parent / "notes")

    def test_registration_refuses_missing_manifest_without_creating_plugin(self):
        self.run_plugin("--register-only", success=False)
        self.assertFalse((self.root / "plugins").exists())
        self.assertFalse((self.root / ".agents").exists())

    def test_duplicate_marketplace_rejects_before_scaffold_and_force_retry_succeeds(self):
        marketplace = self.root / ".agents/plugins/marketplace.json"
        marketplace.parent.mkdir(parents=True)
        marketplace.write_text(json.dumps({"plugins": [{"name": "notes", "source": {"source": "local", "path": "./old/notes"}}]}))
        before = marketplace.read_bytes()
        result = self.run_plugin("--with-marketplace", "--with-mcp", "--with-skills", success=False)
        self.assertIn("Marketplace entry", result.stderr)
        self.assertEqual(marketplace.read_bytes(), before)
        self.assertFalse((self.root / "plugins").exists())
        self.run_plugin("--with-marketplace", "--with-mcp", "--with-skills", "--force")
        self.assertTrue((self.root / "plugins/notes/.codex-plugin/plugin.json").is_file())
        self.assertTrue((self.root / "plugins/notes/.mcp.json").is_file())
        self.assertTrue((self.root / "plugins/notes/skills").is_dir())
        self.assertEqual((self.root / self.entry()["source"]["path"]).resolve(), self.root / "plugins/notes")

    def test_malformed_marketplace_leaves_no_scaffold_and_corrected_retry_succeeds(self):
        marketplace = self.root / ".agents/plugins/marketplace.json"
        marketplace.parent.mkdir(parents=True)
        for index, content in enumerate(("{", "[]", '{"interface": []}', '{"plugins": {}}')):
            with self.subTest(content=content):
                parent = self.root / f"attempt-{index}"
                marketplace.write_text(content)
                self.run_plugin("--path", str(parent), "--with-marketplace", "--with-apps", success=False)
                self.assertFalse(parent.exists())
                self.assertEqual(marketplace.read_text(), content)
                marketplace.write_text('{"plugins": []}')
                self.run_plugin("--path", str(parent), "--with-marketplace", "--with-apps")
                self.assertTrue((parent / "notes/.codex-plugin/plugin.json").is_file())
                self.assertTrue((parent / "notes/.app.json").is_file())

    def test_malformed_marketplace_preserves_existing_plugin_even_with_force(self):
        self.run_plugin("--with-mcp", "--with-apps")
        plugin = self.root / "plugins/notes"
        (plugin / ".codex-plugin/plugin.json").write_text('{"name":"notes","description":"Owner content"}')
        (plugin / ".mcp.json").write_text('{"mcpServers":{"custom":{}}}')
        before = {p.relative_to(plugin): p.read_bytes() for p in plugin.rglob("*") if p.is_file()}
        marketplace = self.root / ".agents/plugins/marketplace.json"
        marketplace.parent.mkdir(parents=True)
        marketplace.write_text("[]")
        self.run_plugin("--with-marketplace", "--with-mcp", "--with-apps", "--force", success=False)
        self.assertEqual(before, {p.relative_to(plugin): p.read_bytes() for p in plugin.rglob("*") if p.is_file()})
        self.assertEqual(marketplace.read_text(), "[]")

    def test_existing_plugin_conflict_precedes_duplicate_hint_and_registration_preserves_files(self):
        self.run_plugin("--with-marketplace", "--with-mcp", "--with-apps")
        plugin = self.root / "plugins/notes"
        manifest = plugin / ".codex-plugin/plugin.json"
        manifest.write_text('{"name":"notes","description":"Owner content"}')
        (plugin / ".mcp.json").write_text('{"mcpServers":{"custom":{}}}')
        before = {p.relative_to(plugin): p.read_bytes() for p in plugin.rglob("*") if p.is_file()}
        marketplace = self.root / ".agents/plugins/marketplace.json"
        market_before = marketplace.read_bytes()
        result = self.run_plugin("--with-marketplace", "--with-mcp", "--with-apps", success=False)
        self.assertIn(f"{manifest} already exists", result.stderr)
        self.assertEqual(marketplace.read_bytes(), market_before)
        self.assertEqual(before, {p.relative_to(plugin): p.read_bytes() for p in plugin.rglob("*") if p.is_file()})
        self.run_plugin("--register-only", "--force", "--auth-policy", "ON_USE")
        self.assertEqual(self.entry()["policy"]["authentication"], "ON_USE")
        self.assertEqual(before, {p.relative_to(plugin): p.read_bytes() for p in plugin.rglob("*") if p.is_file()})


if __name__ == "__main__":
    unittest.main()
