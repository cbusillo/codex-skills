# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Tool checks use fixture homes and fake MCP children, never an installed browser."""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import agent_tools


class ClaudeToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "account"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.config = {
            "mcpServers": {agent_tools.CHROME_SERVER: {"command": "fixture"}},
            "projects": {str(self.repo): {"enabledMcpServers": [agent_tools.COMPUTER_USE]}},
        }

    def write_config(self, config=None):
        (self.home / ".claude.json").write_text(json.dumps(self.config if config is None else config))

    def test_default_and_alternate_homes_are_checked_without_writing(self):
        for home in (None, self.home):
            with self.subTest(home=home), patch.object(Path, "home", return_value=self.root):
                path = agent_tools.claude_config_path(home)
                path.write_text(json.dumps(self.config))
                before = path.read_bytes()
                self.assertEqual(agent_tools.claude_problems(home, self.repo, {}), [])
                self.assertEqual(path.read_bytes(), before)

    def test_broken_home_and_project_settings_are_reported(self):
        self.write_config({})
        problems = " ".join(agent_tools.claude_problems(self.home, self.repo, {}))
        self.assertIn(agent_tools.CHROME_SERVER, problems)
        self.assertIn("Computer Use", problems)
        self.config["claudeInChromeDefaultEnabled"] = True
        self.write_config()
        self.assertTrue(agent_tools.claude_problems(self.home, self.repo, {}))
        self.assertEqual(agent_tools.claude_problems(self.home, self.repo, {agent_tools.CHROME_ENV: "0"}), [])
        entry = self.config["projects"][str(self.repo)]
        entry["disabledMcpServers"] = [agent_tools.BUILTIN_CHROME, agent_tools.COMPUTER_USE,
                                       agent_tools.CHROME_SERVER]
        self.write_config()
        problems = " ".join(agent_tools.claude_problems(self.home, self.repo, {}))
        self.assertIn(agent_tools.CHROME_SERVER, problems)
        self.assertIn("Computer Use", problems)
        self.assertTrue(agent_tools.builtin_chrome_on(self.config, entry, {agent_tools.CHROME_ENV: "1"}))

    def test_missing_malformed_and_unreadable_config_remain_diagnostics(self):
        path = self.home / ".claude.json"
        for value in (None, "[", "[]", "null"):
            if value is not None:
                path.write_text(value)
            with self.subTest(value=value):
                self.assertTrue(agent_tools.claude_problems(self.home, self.repo, {}))
        with patch.object(Path, "read_text", side_effect=PermissionError):
            self.assertTrue(agent_tools.claude_problems(self.home, self.repo, {}))


class BrowserProbeTests(unittest.TestCase):
    def server(self, behavior):
        script = '''import json, os, sys, time
for line in sys.stdin:
    msg=json.loads(line)
    if 'id' not in msg: continue
    method=msg['method']
    if method=='initialize':
        result={'protocolVersion':'2025-06-18','capabilities':{},'serverInfo':{'name':'fixture','version':'1'}}
    else:
        BEHAVIOR
    print(json.dumps({'jsonrpc':'2.0','method':'fixture/notification'}))
    print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}), flush=True)
'''.replace("        BEHAVIOR", "        " + behavior.replace("\n", "\n        "))
        return {"command": sys.executable, "args": ["-u", "-c", script], "env": {"FIXTURE": "selected"}}

    def test_listing_handles_notifications_and_uses_server_environment(self):
        server = self.server("assert os.environ['FIXTURE']=='selected'\nresult={'content':[{'type':'text','text':'[{\"id\":\"fixture\"}]'}]}")
        self.assertEqual(agent_tools.connected_browsers(server, timeout=2), [{"id": "fixture"}])

    def test_server_request_with_same_id_is_not_a_reply(self):
        server = self.server("print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'method':'ping'}),flush=True)\nresult={'content':[{'type':'text','text':'[]'}]}")
        self.assertEqual(agent_tools.connected_browsers(server, timeout=2), [])

    def test_empty_list_is_distinct_from_failed_or_malformed_tool(self):
        self.assertEqual(agent_tools.connected_browsers(
            self.server("result={'content':[{'type':'text','text':'[]'}]}"), timeout=2), [])
        for result in ({"isError": True, "content": [{"type": "text", "text": "private"}]},
                       {"content": [{"type": "text", "text": "private"}]},
                       {"content": [{"type": "text", "text": "{}"}]},
                       {"content": [{"type": "text", "text": "[1]"}]}):
            with self.subTest(result=result), self.assertRaises(ValueError) as error:
                agent_tools.connected_browsers(self.server("result=" + repr(result)), timeout=2)
            self.assertNotIn("private", str(error.exception))

    def test_partial_line_eof_rpc_error_and_missing_executable_are_bounded(self):
        for behavior in ("sys.stdout.write('{');sys.stdout.flush();time.sleep(30)",
                         "sys.exit(0)",
                         "print(json.dumps({'id':msg['id'],'error':{'message':'private'}}),flush=True);continue"):
            started = time.monotonic()
            with self.subTest(behavior=behavior), self.assertRaises(ValueError):
                agent_tools.connected_browsers(self.server(behavior), timeout=0.2)
            self.assertLess(time.monotonic() - started, 3)
        with self.assertRaises(ValueError):
            agent_tools.connected_browsers({"command": "/missing-fixture-server"}, timeout=0.2)

    def test_cleanup_refusal_does_not_retry_a_signal(self):
        with patch.object(os, "killpg", side_effect=PermissionError) as signal_group:
            with self.assertRaises(ValueError):
                agent_tools.connected_browsers(self.server("sys.exit(0)"), timeout=0.2)
            signal_group.assert_called_once()

    @unittest.skipUnless(hasattr(os, "fork"), "POSIX child lifetime")
    def test_timeout_reaps_a_child_holding_the_output_pipe(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "child-survived"
            behavior = ("pid=os.fork()\nif pid==0:\n"
                        "    time.sleep(0.7)\n"
                        f"    open({str(marker)!r},'w').close()\n"
                        "    os._exit(0)\nsys.exit(0)")
            with self.assertRaises(ValueError):
                agent_tools.connected_browsers(self.server(behavior), timeout=0.15)
            time.sleep(0.8)
            self.assertFalse(marker.exists())


class CodexToolTests(unittest.TestCase):
    def test_captured_cli_inventory_is_read_by_the_native_cli_parser(self):
        import subprocess
        fixture = json.loads((Path(__file__).parent / "fixtures/codex-plugin-list-cli.json").read_text())["response"]
        reply = subprocess.CompletedProcess([], 0, json.dumps(fixture), "")
        with patch.object(subprocess, "run", return_value=reply):
            plugins = agent_tools.codex_plugins(Path("/fixture/home"), Path("/fixture/source"))
        self.assertEqual(agent_tools.missing_codex_plugins(plugins), [])

    def test_existing_daemon_capture_does_not_invent_bundled_plugin_support(self):
        inventory = json.loads((Path(__file__).parent / "fixtures/codex-plugin-list-daemon.json").read_text())["response"]
        self.assertEqual(set(agent_tools.missing_codex_plugins(
            agent_tools.daemon_codex_plugins(inventory))), agent_tools.CODEX_PLUGINS)

    def test_only_installed_enabled_bundled_plugins_satisfy_discovery(self):
        plugins = [{"name": name, "installed": True, "enabled": True, "marketplaceName": "openai-bundled"}
                   for name in agent_tools.CODEX_PLUGINS]
        self.assertEqual(agent_tools.missing_codex_plugins(plugins), [])
        for key, value in (("installed", False), ("enabled", False), ("marketplaceName", "fixture-other")):
            changed = [{**p} for p in plugins]
            changed[0][key] = value
            with self.subTest(key=key):
                self.assertEqual(agent_tools.missing_codex_plugins(changed), [plugins[0]["name"]])

    def test_session_start_checks_account_relative_source_without_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            config = home / "config.toml"
            source = home / agent_tools.BUNDLED_MARKETPLACE
            source.mkdir(parents=True)
            config.write_text('[marketplaces.openai-bundled]\nsource="/other-home"\n')
            before = config.read_bytes()
            self.assertTrue(agent_tools.codex_problems(home))
            self.assertEqual(agent_tools.codex_problems(home, {agent_tools.CODEX_SOURCE_ENV: str(source)}), [])
            self.assertEqual(config.read_bytes(), before)
            config.write_text('[marketplaces.openai-bundled]\nsource=' + json.dumps(str(source)) + '\n')
            self.assertEqual(agent_tools.codex_problems(home), [])
            source.rmdir()
            self.assertTrue(agent_tools.codex_problems(home))

    def test_plugin_inventory_failure_never_becomes_success(self):
        import subprocess
        for reply in (subprocess.CompletedProcess([], 1, "private", "private"),
                      subprocess.CompletedProcess([], 0, "[]", ""),
                      subprocess.CompletedProcess([], 0, '{"installed":null}', "")):
            with patch.object(subprocess, "run", return_value=reply), self.assertRaises(ValueError) as error:
                agent_tools.codex_plugins(Path("/fixture"), Path("/fixture/source"))
            self.assertNotIn("private", str(error.exception))


if __name__ == "__main__":
    unittest.main()
