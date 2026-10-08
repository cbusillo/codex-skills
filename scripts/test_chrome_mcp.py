#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Chrome enrollment behavior with fake native management and isolated homes."""
import json
import os
import subprocess
import tempfile
import unittest
from typing import Any
from pathlib import Path
from unittest import mock

import chrome_mcp as chrome


class ChromeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = self.root / "catalog"
        (self.catalog / ".local").mkdir(parents=True)
        self.home = self.root / "host"
        self.default, self.other, self.pinned = [self.home / name for name in (".claude", "other", "extension")]
        for path in (self.default, self.other, self.pinned):
            path.mkdir(parents=True)
        self.configure(self.pinned)
        self.servers: dict[Path, dict[str, dict[str, Any]]] = {
            path: {"personal": {"command": "keep-me"}}
            for path in (self.default, self.other, self.pinned)
        }
        self.calls = []
        self.fail_add = False
        self.fail_get = False
        self.patch = mock.patch.object(chrome, "run_mcp", side_effect=self.native)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def configure(self, pinned):
        (self.catalog / ".local" / "chrome.toml").write_text(
            f'pinned_home = {json.dumps(str(pinned))}\nhomes = [{json.dumps(str(self.other))}]\n')

    def native(self, command, directory, args):
        self.assertEqual(command, "claude")
        self.calls.append((directory, args[0]))
        entries = self.servers[directory]
        status, stdout = 0, ""
        if args[0] == "get":
            entry = entries.get(chrome.SERVER)
            if self.fail_get:
                status, stdout = 1, "Could not load configuration"
            elif entry is None:
                status, stdout = 1, f'No MCP server named "{chrome.SERVER}". Configured servers: personal'
            else:
                stdout = "\n".join([
                    f"{chrome.SERVER}:", "  Scope: User config (available in all your projects)",
                    "  Status: ✔ Connected", f"  Type: {entry['type']}",
                    f"  Command: {entry['command']}", f"  Args: {' '.join(entry['args'])}",
                    "  Environment:", *(f"    {key}={value}" for key, value in entry['env'].items())])
        elif args[0] == "remove":
            self.assertEqual(args[-2:], ["--scope", "user"])
            del entries[chrome.SERVER]
        elif args[0] == "add-json":
            self.assertEqual(args[-2:], ["--scope", "user"])
            entry = json.loads(args[2])
            if self.fail_add and entry["env"]["CLAUDE_CONFIG_DIR"] != str(self.pinned):
                status = 1
            else:
                self.assertNotIn(chrome.SERVER, entries)
                entries[chrome.SERVER] = entry
        return subprocess.CompletedProcess(args, status, stdout, "")

    def prepare(self):
        plan = chrome.prepare(self.catalog, self.home, self.default)
        assert plan is not None
        return plan

    def test_preview_then_install_all_homes_and_repeat_without_mutations(self):
        plan = self.prepare()
        self.assertTrue(all(entry['state'] == 'create' for entry in plan['entries']))
        self.assertFalse((self.catalog / '.local' / 'chrome-install.json').exists())
        self.assertTrue(all(set(entries) == {'personal'} for entries in self.servers.values()))
        chrome.apply(plan)
        desired = chrome.server_entry(self.pinned, 'claude')
        for entries in self.servers.values():
            self.assertEqual(entries[chrome.SERVER], desired)
            self.assertEqual(entries['personal'], {'command': 'keep-me'})
        self.calls.clear()
        chrome.apply(self.prepare())
        self.assertTrue(all(action == 'get' for _, action in self.calls))

    def test_pinned_home_move_updates_only_receipt_owned_entries(self):
        chrome.apply(self.prepare())
        self.configure(self.other)
        chrome.apply(self.prepare())
        for directory in (self.default, self.other):
            self.assertEqual(self.servers[directory][chrome.SERVER]['env']['CLAUDE_CONFIG_DIR'], str(self.other))
        # Dropped homes remain intact, so the installer never silently removes access.
        self.assertEqual(self.servers[self.pinned][chrome.SERVER]['env']['CLAUDE_CONFIG_DIR'], str(self.pinned))

    def test_failed_update_restores_previous_entry_and_receipt(self):
        chrome.apply(self.prepare())
        receipt = (self.catalog / '.local' / 'chrome-install.json').read_bytes()
        self.configure(self.other)
        self.fail_add = True
        with self.assertRaisesRegex(ValueError, 'Could not install'):
            chrome.apply(self.prepare())
        self.assertEqual(self.servers[self.default][chrome.SERVER], chrome.server_entry(self.pinned, 'claude'))
        self.assertEqual((self.catalog / '.local' / 'chrome-install.json').read_bytes(), receipt)

    def test_readback_timeout_recovers_by_inspecting_existing_entry(self):
        plan = self.prepare()
        native = self.native

        def time_out_readback(command, directory, args):
            if args[0] == 'get' and chrome.SERVER in self.servers[directory]:
                raise subprocess.TimeoutExpired('fixture-cli', 45)
            return native(command, directory, args)

        with mock.patch.object(chrome, 'run_mcp', side_effect=time_out_readback):
            with self.assertRaises(subprocess.TimeoutExpired):
                chrome.apply(plan)
        self.assertIn(chrome.SERVER, self.servers[self.default])
        self.calls.clear()
        chrome.apply(self.prepare())
        self.assertNotIn((self.default, 'add-json'), self.calls)

    def test_native_and_explicit_alias_homes_are_enrolled_separately(self):
        self.default.rmdir()
        self.default.symlink_to(self.other, target_is_directory=True)
        self.configure(self.default)
        with mock.patch.object(Path, 'home', return_value=self.home):
            plan = chrome.prepare(self.catalog, self.home, self.other)
            assert plan is not None
            self.assertEqual(plan['entries'][0]['path'], self.other)
            self.assertIn(self.default, [entry['path'] for entry in plan['entries']])
            self.assertIn('CLAUDE_CONFIG_DIR', plan['desired']['args'])
            identity = self.home / '.claude.json'
            identity.symlink_to(self.root / 'opaque-identity')
            with self.assertRaisesRegex(ValueError, 'regular file'):
                chrome.prepare(self.catalog, self.home, self.other)

    def test_hand_edit_and_local_scope_shadow_refuse_before_writes(self):
        chrome.apply(self.prepare())
        self.servers[self.other][chrome.SERVER]['env']['EXTRA'] = 'personal-setting'
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, 'preserved'):
            self.prepare()
        self.assertTrue(all(action == 'get' for _, action in self.calls))
        desired = chrome.server_entry(self.pinned, 'claude')
        details = self.native('claude', self.default, ['get']).stdout
        self.assertFalse(chrome.matches_details(details.replace('User config (available in all your projects)', 'Local config'), desired))

    def test_bad_cli_read_is_not_treated_as_missing(self):
        self.fail_get = True
        with self.assertRaisesRegex(ValueError, 'Could not inspect'):
            self.prepare()
        self.assertTrue(all(action == 'get' for _, action in self.calls))

    def test_symlink_user_config_refuses_without_opening_it(self):
        target = self.root / 'private-identity'
        target.write_text('must stay opaque')
        (self.other / '.claude.json').symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            self.prepare()
        self.assertTrue(all(action == 'get' for _, action in self.calls))

    def test_credential_files_are_not_opened_and_unconfigured_install_is_opt_out(self):
        for path in self.servers:
            (path / '.claude.json').write_text('{not readable login data')
        chrome.apply(self.prepare())  # Only the native CLI owns those files.
        (self.catalog / '.local' / 'chrome.toml').unlink()
        self.calls.clear()
        self.assertIsNone(chrome.prepare(self.catalog, self.home, self.default))
        self.assertEqual(self.calls, [])

    def test_native_management_process_uses_destination_home_without_changing_parent(self):
        self.patch.stop()
        command = self.root / 'fake-claude'
        command.write_text('#!/bin/sh\nprintf "%s" "$CLAUDE_CONFIG_DIR"\n')
        command.chmod(0o700)
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.other)}):
            result = chrome.run_mcp(str(command), self.pinned, ['get', chrome.SERVER])
            self.assertEqual(result.stdout, str(self.pinned))
            self.assertEqual(os.environ['CLAUDE_CONFIG_DIR'], str(self.other))

    def test_browser_child_uses_pinned_home_and_drops_caller_auth_overrides(self):
        command = self.root / 'fake-browser-cli'
        command.write_text('#!/bin/sh\nprintf "%s|%s|%s|%s" "${CLAUDE_CONFIG_DIR-unset}" "${CLAUDE_CODE_OAUTH_TOKEN-unset}" "${ANTHROPIC_API_KEY-unset}" "${ANTHROPIC_AUTH_TOKEN-unset}"\n')
        command.chmod(0o700)
        parent = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.other),
                      CLAUDE_CODE_OAUTH_TOKEN='fixture-only', ANTHROPIC_API_KEY='fixture-only',
                      ANTHROPIC_AUTH_TOKEN='fixture-only')
        with mock.patch.object(Path, 'home', return_value=self.home):
            for pinned, expected in ((self.pinned, str(self.pinned)), (self.default, 'unset')):
                entry = chrome.server_entry(pinned, str(command))
                child = subprocess.run([entry['command'], *entry['args']],
                                       env={**parent, **entry['env']}, capture_output=True, text=True, check=True)
                self.assertEqual(child.stdout, f'{expected}|unset|unset|unset')

    def test_native_default_management_unsets_inherited_account_directory(self):
        self.patch.stop()
        command = self.root / 'fake-claude'
        command.write_text('#!/bin/sh\nprintf "%s" "${CLAUDE_CONFIG_DIR-unset}"\n')
        command.chmod(0o700)
        with mock.patch.object(Path, 'home', return_value=self.home), mock.patch.dict(
                os.environ, {'CLAUDE_CONFIG_DIR': str(self.other)}):
            result = chrome.run_mcp(str(command), self.default, ['get', chrome.SERVER])
            self.assertEqual(result.stdout, 'unset')

    def test_symlink_component_in_host_home_preserves_default_cli_semantics(self):
        alias = self.root / 'alias-home'
        alias.symlink_to(self.home, target_is_directory=True)
        self.patch.stop()
        command = self.root / 'fake-claude'
        command.write_text('#!/bin/sh\nprintf "%s" "${CLAUDE_CONFIG_DIR-unset}"\n')
        command.chmod(0o700)
        with mock.patch.object(Path, 'home', return_value=alias):
            self.assertTrue(chrome.native_default(self.default))
            result = chrome.run_mcp(str(command), self.default, ['get', chrome.SERVER])
            self.assertEqual(result.stdout, 'unset')

    def test_fixture_default_does_not_use_the_host_default_login(self):
        entry = chrome.server_entry(self.default, 'claude')
        self.assertFalse(chrome.native_default(self.default))
        # The fixture path remains explicit rather than falling back to the host login.
        self.assertNotIn('CLAUDE_CONFIG_DIR', entry['args'])
        self.assertEqual(entry['env']['CLAUDE_CONFIG_DIR'], str(self.default))


if __name__ == '__main__':
    unittest.main()
