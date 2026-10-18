# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Disposable native coordinates and child adapters test coordinated resumption."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid

SPEC = importlib.util.spec_from_file_location("codex_session", Path(__file__).with_name("codex-session.py"))
SESSION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SESSION)


class ResumeTests(unittest.TestCase):
    def test_fresh_launch_registers_before_native_exec_and_preserves_argv(self):
        native = ["codex", "--remote", "unix://" + str(SESSION.codex_daemon_endpoint(self.home)), "--", "literal $(touch NEVER)"]
        self.args.command, self.args.native_args = "launch", ["--", *native]
        def execute(executable, argv, env):
            self.assertEqual(executable, native[0])
            self.assertEqual(argv, native)
            self.assertEqual(env["CODEX_HOME"], str(self.home))
            rows = json.loads(self.log.read_text())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["launch_pid"], self.child.pid)
            self.assertNotIn(native[-1], self.log.read_text())
        with patch.object(SESSION.os, "execvpe", side_effect=execute) as execute:
            self.assertEqual(SESSION.run(self.args), 0)
        execute.assert_called_once()
        self.spawn.assert_not_called()

    def test_fresh_launch_cannot_hide_resume_or_use_unobserved_home(self):
        endpoint = "unix://" + str(SESSION.codex_daemon_endpoint(self.home))
        self.args.command = "launch"
        with patch.object(SESSION.os, "execvpe") as execute:
            for native in (["codex", "--remote", endpoint, "resume", self.native],
                           ["codex", "--remote", endpoint, "--no-daemon"],
                           ["codex", "--remote", endpoint, "exec-server"],
                           ["codex", "--remote", "unix:///unobserved", "--", "task"]):
                self.args.native_args = native
                with self.assertRaises(ValueError): SESSION.run(self.args)
            execute.assert_not_called()
        self.assertFalse(self.log.exists())

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="resume-", dir="/tmp"))).resolve()
        self.home = self.root / "account"
        self.native = str(uuid.uuid4())
        path = self.home / "sessions" / (self.native + ".jsonl")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.native}}) + "\n")
        endpoint = SESSION.codex_daemon_endpoint(self.home)
        endpoint.parent.mkdir()
        self.socket = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.socket.close)
        self.socket.bind(str(endpoint))
        endpoint.chmod(0o600)
        self.log = self.root / "shared/closed.json"
        self.enterContext(patch.object(SESSION.closure, "closed_log", return_value=self.log))
        self.enterContext(patch.object(SESSION, "codex_homes", return_value=[self.home]))
        self.args = SimpleNamespace(command="resume", session_id=self.native, codex_home=self.home,
                                    message=None, message_file=None)
        self.child = MagicMock(pid=456)
        self.child.__enter__.return_value = self.child
        self.child.wait.return_value = 0
        self.spawn = self.enterContext(patch.object(SESSION.subprocess, "Popen", return_value=self.child))
        self.enterContext(patch.object(SESSION.closure, "launch_metadata", return_value={
            "launch_pid": self.child.pid, "launch_generation": "new-generation", "launch_started_at": "2026-10-01T01:00:00Z",
            "codex_home": str(self.home)}))

    def test_resume_records_new_generation_and_uses_exact_home_and_id(self):
        self.assertEqual(SESSION.run(self.args), 0)
        args, kwargs = self.spawn.call_args
        self.assertEqual(args[0][-1], self.native)
        self.assertEqual(kwargs["env"]["CODEX_HOME"], str(self.home))
        row = json.loads(self.log.read_text())[0]
        self.assertEqual(row["resumed_generation"]["launch_pid"], self.child.pid)
        self.assertIsNone(row["closure_evidence"])
        self.args.command = "status"
        output = io.StringIO()
        with contextlib.redirect_stdout(output): SESSION.run(self.args)
        self.assertEqual(json.loads(output.getvalue())["pid"], self.child.pid)

    def test_resume_preserves_unrelated_managed_launch_registration(self):
        SESSION.closure.register_launch(["codex", "--", "unrelated"], str(self.home))
        registration = json.loads(self.log.read_text())[0]
        self.assertEqual(SESSION.run(self.args), 0)
        rows = json.loads(self.log.read_text())
        self.assertIn(registration, rows)
        self.assertEqual(len([r for r in rows if r.get("session_id") == self.native]), 1)

    def test_queue_passes_literal_message_after_invalidating_old_closure(self):
        self.log.parent.mkdir()
        self.log.write_text(json.dumps([{"session_id": self.native, "closure_evidence": {"old": True}}]))
        self.log.chmod(0o600)
        def spawn(*_args, **_kw):
            self.assertIsNone(json.loads(self.log.read_text())[0]["closure_evidence"])
            return self.child
        self.spawn.side_effect = spawn
        self.args.command, self.args.message = "queue", "literal $(touch NEVER) message"
        self.assertEqual(SESSION.run(self.args), 0)
        command = self.spawn.call_args.args[0]
        self.assertEqual(command[command.index("--message") + 1], self.args.message)

    def test_handed_off_generation_never_starts_a_native_client(self):
        self.log.parent.mkdir()
        self.log.write_text(json.dumps([{"session_id": self.native, "successor_admitted": True}]))
        self.log.chmod(0o600)
        with self.assertRaises(ValueError):
            SESSION.run(self.args)
        self.spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
