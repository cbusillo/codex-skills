#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise alert output, concurrent append and catalog-rendered commands locally."""
import concurrent.futures
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).with_name("session_alert_hook.py")
ROOT = HOOK.parent.parent
spec = importlib.util.spec_from_file_location("sync", ROOT / "scripts" / "sync-global-instructions.py")
assert spec and spec.loader
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class SessionAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.shared = self.home / "shared"
        self.env = {**os.environ, "HOME": str(self.home), "CODE_HOME": str(self.shared), "CODEX_SKILLS_HARNESS": "codex"}
        self.events = self.shared / "session-events.jsonl"

    def run_hook(self, payload, *, env=None, command=None):
        return subprocess.run(command or [sys.executable, str(HOOK)], input=json.dumps(payload),
                              capture_output=True, text=True, env=env or self.env, timeout=5)

    def records(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def test_stop_and_interrupt_are_advisory_and_discard_content(self):
        for event in ("Stop", "Interrupt"):
            result = self.run_hook({"hook_event_name": event, "session_id": "session", "turn_id": "turn",
                                    "stop_hook_active": True, "last_assistant_message": "secret", "transcript_path": "/secret"})
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), {})
        records = self.records()
        self.assertEqual([r["event"] for r in records], ["Stop", "Interrupt"])
        for record in records:
            self.assertTrue(record["advisory"])
            self.assertIsNone(record["clean"])
            self.assertEqual(record["harness"], "codex")
            self.assertEqual(record["session_id"], "session")
            self.assertEqual(record["turn_id"], "turn")
            self.assertNotIn("secret", json.dumps(record))
        self.assertTrue(records[0]["stop_hook_active"])
        self.assertEqual(stat.S_IMODE(self.events.stat().st_mode), 0o600)

    def test_repeated_stop_is_not_deduplicated_into_completion(self):
        payload = {"hook_event_name": "Stop", "session_id": "s", "turn_id": "t"}
        self.run_hook(payload)
        self.run_hook({**payload, "stop_hook_active": True})
        self.assertEqual(len(self.records()), 2)
        self.assertTrue(all(r["clean"] is None for r in self.records()))

    def test_malformed_or_unrelated_input_does_not_write(self):
        for payload in (None, [], {}, {"hook_event_name": "SubagentStop", "session_id": "s"},
                        {"hook_event_name": "Stop", "session_id": 7}):
            self.assertEqual(self.run_hook(payload).returncode, 0)
        proc = subprocess.run([sys.executable, str(HOOK)], input="broken", text=True, capture_output=True, env=self.env)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout), {})
        self.assertFalse(self.events.exists())

    def test_concurrent_hooks_keep_complete_lines(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda n: self.run_hook({"hook_event_name": "Stop", "session_id": str(n)}), range(32)))
        self.assertTrue(all(r.returncode == 0 for r in results))
        self.assertEqual({r["session_id"] for r in self.records()}, {str(n) for n in range(32)})

    def test_unwritable_and_redirected_destinations_do_not_control_turn(self):
        self.shared.mkdir()
        target = self.home / "target"
        target.write_text("preserve")
        self.events.symlink_to(target)
        result = self.run_hook({"hook_event_name": "Stop", "session_id": "s"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {})
        self.assertEqual(target.read_text(), "preserve")
        self.assertIn("unavailable", result.stderr)
        self.events.unlink()
        self.events.mkdir()
        self.assertEqual(self.run_hook({"hook_event_name": "Interrupt", "session_id": "s"}).returncode, 0)

    def test_catalog_commands_run_on_both_harnesses_in_shared_stream(self):
        source = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
        env = {**self.env, "CLAUDE_PLUGIN_ROOT": str(ROOT)}
        env.pop("CODEX_SKILLS_HARNESS")
        claude = source["Stop"][0]["hooks"][0]["command"]
        self.assertEqual(self.run_hook({"hook_event_name": "Stop", "session_id": "claude"}, env=env, command=["sh", "-c", claude]).returncode, 0)
        rendered = json.loads(sync.render_codex_hook(self.home / "hooks.json", ROOT))["hooks"]
        for event in ("Stop", "Interrupt"):
            command = rendered[event][0]["hooks"][0]["command"]
            proc = self.run_hook({"hook_event_name": event, "session_id": "codex"}, command=["sh", "-c", command])
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([r["harness"] for r in self.records()], ["claude", "codex", "codex"])

    def test_alerts_can_be_disabled_independently_on_both_harnesses(self):
        for harness in ("codex", "claude"):
            env = {**self.env, "CODEX_SKILLS_HARNESS": harness, "SESSION_ALERTS_DISABLED": "1"}
            result = self.run_hook({"hook_event_name": "Stop", "session_id": "s"}, env=env)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), {})
        self.assertFalse(self.events.exists())
        self.run_hook({"hook_event_name": "Stop", "session_id": "s"})
        self.assertEqual(len(self.records()), 1)

    def test_relative_shared_home_is_skipped_without_writing_in_repository(self):
        result = self.run_hook({"hook_event_name": "Stop", "session_id": "s"}, env={**self.env, "CODE_HOME": "."})
        self.assertEqual(result.returncode, 0)
        self.assertIn("unavailable", result.stderr)
        self.assertFalse(self.events.exists())

    def test_host_homes_do_not_split_default_shared_stream(self):
        env = {**self.env, "CODEX_HOME": str(self.home / "codex"), "CLAUDE_CONFIG_DIR": str(self.home / "claude")}
        env.pop("CODE_HOME")
        self.run_hook({"hook_event_name": "Stop", "session_id": "s"}, env=env)
        self.assertTrue((self.home / ".code" / "session-events.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
