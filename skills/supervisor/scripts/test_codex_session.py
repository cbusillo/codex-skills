# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Disposable native coordinates and child adapters test coordinated resumption."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
from datetime import datetime, timezone
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import uuid
from iterm_tab import codex_daemon_endpoint

SPEC = importlib.util.spec_from_file_location("codex_session", Path(__file__).with_name("codex-session.py"))
SESSION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SESSION)
import github_plan_sessions
import github_plan_native


class ResumeTests(unittest.TestCase):
    def test_bare_unix_launch_uses_the_selected_account_endpoint(self):
        self.args.command = "launch"
        endpoint = "unix://" + str(codex_daemon_endpoint(self.home))
        for remote in (["--remote", "unix://"], ["--remote=unix://"]):
            with self.subTest(remote=remote):
                self.args.native_args = ["codex", *remote, "-C", str(self.root), "--", "literal task"]
                with patch.object(SESSION, "run_client", new_callable=AsyncMock, return_value=0) as execute:
                    self.assertEqual(SESSION.run(self.args), 0)
                native = execute.call_args.args[1]
                self.assertEqual(SESSION.closure.remote_endpoints(native), [endpoint])
                self.assertEqual(native[-4:], ["-C", str(self.root), "--", "literal task"])
        self.spawn.assert_not_called()

    def test_fresh_launch_uses_guarded_ingress_and_preserves_argv(self):
        native = ["codex", "--remote", "unix://" + str(codex_daemon_endpoint(self.home)), "--", "literal $(touch NEVER)"]
        self.args.command, self.args.native_args = "launch", ["--", *native]
        with patch.object(SESSION, "run_client", new_callable=AsyncMock, return_value=0) as execute:
            self.assertEqual(SESSION.run(self.args), 0)
        execute.assert_awaited_once_with(self.home, native)
        self.spawn.assert_not_called()

    def test_fresh_launch_cannot_hide_resume_or_use_unobserved_home(self):
        endpoint = "unix://" + str(codex_daemon_endpoint(self.home))
        self.args.command = "launch"
        with patch.object(SESSION.os, "execvpe") as execute:
            for native in (["codex", "--remote", endpoint, "resume", self.native],
                           ["codex", "--remote", endpoint, "--no-daemon"],
                           ["codex", "--remote", endpoint, "exec-server"],
                           ["codex", "--remote", "unix://", "--remote", endpoint, "--", "task"],
                           ["codex", "--remote", "unix:///unobserved", "--", "task"]):
                self.args.native_args = native
                with self.assertRaises(ValueError): SESSION.run(self.args)
            execute.assert_not_called()
        self.assertFalse(self.log.exists())

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="resume-", dir="/tmp"))).resolve()
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "CODEX_HOME": str(self.root / "account")}))
        self.home = self.root / "account"
        self.native = str(uuid.uuid4())
        path = self.home / "sessions" / (self.native + ".jsonl")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.native}}) + "\n")
        endpoint = codex_daemon_endpoint(self.home)
        endpoint.parent.mkdir()
        self.socket = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.socket.close)
        self.socket.bind(str(endpoint))
        endpoint.chmod(0o600)
        self.log = self.root / "shared/closed.json"
        self.enterContext(patch.object(SESSION.closure, "closed_log", return_value=self.log))
        self.homes = self.enterContext(patch.object(github_plan_sessions, "configured_homes", return_value=[self.home]))
        self.args = SimpleNamespace(command="resume", session_id=self.native, codex_home=self.home,
                                    message=None, message_file=None)
        self.child = MagicMock(pid=456)
        self.child.__enter__.return_value = self.child
        self.child.wait.return_value = 0
        self.queue_requests = []
        self.queue_hook = lambda: None
        self.thread_state = "notLoaded"
        @contextlib.asynccontextmanager
        async def connection(home, **_kwargs):
            async def request(method, params):
                if method == "thread/queue/add":
                    self.queue_hook()
                    self.queue_requests.append(params)
                    return {"queuedSubmission": {"id": "queued", **{key: params[key] for key in ("clientUserMessageId", "input")}}}
                if method == "thread/read":
                    return {"thread": {"id": self.native, "path": str(path), "status": {"type": self.thread_state}}}
                return {"data": [], "nextCursor": None}
            request.codex_home = str(home.resolve())
            yield request
        self.enterContext(patch.object(github_plan_native, "connection", connection))
        self.spawn = self.enterContext(patch.object(SESSION.subprocess, "Popen", return_value=self.child))
        self.enterContext(patch.object(SESSION.closure, "launch_metadata", return_value={
            "launch_pid": self.child.pid, "launch_generation": "new-generation", "launch_started_at": "2026-10-01T01:00:00Z",
            "codex_home": str(self.home)}))

    def test_queue_cancel_cli_refuses_malformed_original_without_traceback_or_state(self):
        record = self.root / "original-termination.json"
        record.write_text(json.dumps({"session_id": self.native, "harness": "codex", "private": "never echo input"}))
        record.chmod(0o600)
        output = io.StringIO()
        with patch.object(SESSION.sys, "argv", ["codex-session.py", "queue-cancel", "--session-id", self.native,
                "--codex-home", str(self.home), "--termination-record", str(record)]), contextlib.redirect_stdout(output):
            self.assertEqual(SESSION.main(), 1)
        self.assertFalse(json.loads(output.getvalue())["ok"])
        self.assertNotIn("never echo input", output.getvalue())
        self.assertFalse(self.log.exists())
        self.assertFalse(SESSION.closure.lease_path(self.native).exists())
        self.spawn.assert_not_called()

    def test_resume_records_new_generation_and_uses_exact_home_and_id(self):
        with patch.object(SESSION, "run_client", new_callable=AsyncMock, return_value=0) as guarded:
            self.assertEqual(SESSION.run(self.args), 0)
        command = guarded.call_args.args[1]
        self.assertEqual(command[-1], self.native)
        self.assertEqual(guarded.call_args.kwargs["session"], self.native)
        with SESSION.closure.resume_guard(self.native):
            SESSION.closure.resumed_launch(self.native, SESSION.closure.launch_metadata(456, "generation", str(self.home)),
                                          str(next((self.home / "sessions").glob("*.jsonl"))), "tty")
        row = json.loads(self.log.read_text())[0]
        self.assertEqual(row["resumed_generation"]["launch_pid"], self.child.pid)
        self.assertIsNone(row["closure_evidence"])
        self.args.command = "status"
        output = io.StringIO()
        with contextlib.redirect_stdout(output): SESSION.run(self.args)
        self.assertEqual(json.loads(output.getvalue())["pid"], self.child.pid)

    def test_missing_source_socket_refuses_resume_and_queue_without_an_action(self):
        codex_daemon_endpoint(self.home).unlink()
        with patch.object(SESSION, "run_client", new_callable=AsyncMock) as guarded:
            for command in ("resume", "queue"):
                with self.subTest(command=command):
                    self.args.command = command
                    with self.assertRaises(ValueError):
                        SESSION.run(self.args)
            guarded.assert_not_called()
        self.spawn.assert_not_called()
        self.assertFalse(self.log.exists())

    def test_source_validation_refuses_unconfigured_home_wrong_transcript_and_socket(self):
        with patch.object(SESSION, "run_client", new_callable=AsyncMock) as guarded:
            self.args.session_id = self.native.replace("-", "")
            with self.assertRaises(ValueError) as alias:
                SESSION.run(self.args)
            self.assertIn(self.native, str(alias.exception))
            self.args.session_id = self.native
            self.homes.return_value = []
            with self.assertRaises(ValueError):
                SESSION.run(self.args)
            self.homes.return_value = [self.home]
            transcript = next((self.home / "sessions").glob("*.jsonl"))
            original = transcript.read_text()
            transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": str(uuid.uuid4())}}) + "\n")
            with self.assertRaises(ValueError):
                SESSION.run(self.args)
            transcript.write_text(original)
            codex_daemon_endpoint(self.home).chmod(0o622)
            with self.assertRaises(ValueError):
                SESSION.run(self.args)
            guarded.assert_not_called()
        self.assertFalse(self.log.exists())

    def test_resume_preserves_unrelated_managed_launch_registration(self):
        SESSION.closure.register_launch(["codex", "--", "unrelated"], str(self.home))
        registration = json.loads(self.log.read_text())[0]
        with patch.object(SESSION, "run_client", new_callable=AsyncMock, return_value=0):
            self.assertEqual(SESSION.run(self.args), 0)
        rows = json.loads(self.log.read_text())
        self.assertIn(registration, rows)

    def test_queue_passes_literal_message_after_invalidating_old_closure(self):
        self.log.parent.mkdir()
        self.log.write_text(json.dumps([{"session_id": self.native, "closure_evidence": {"old": True}}]))
        self.log.chmod(0o600)
        def queued():
            self.assertIsNone(json.loads(self.log.read_text())[0]["closure_evidence"])
        self.queue_hook = queued
        self.thread_state = "idle"
        self.args.command, self.args.message = "queue", "literal $(touch NEVER) message"
        self.assertEqual(SESSION.run(self.args), 0)
        self.assertEqual(self.queue_requests[0]["input"][0]["text"], self.args.message)
        self.spawn.assert_not_called()

    def test_queue_to_live_worker_does_not_replace_its_launch_on_finish(self):
        import release_session_claims as release
        transcript = next((self.home / "sessions").glob("*.jsonl"))
        with transcript.open("a") as stream:
            stream.write(json.dumps({"timestamp": "2026-10-01T02:00:00Z", "type": "event_msg", "payload": {
                "type": "agent_message", "message": "Safe to exit: yes"}}) + "\n")
            stream.write(json.dumps({"timestamp": "2026-10-01T02:00:01Z", "type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
        self.thread_state = "idle"
        self.args.command, self.args.message = "queue", "continue"
        self.assertEqual(SESSION.run(self.args), 0)
        self.thread_state = "notLoaded"
        timestamp = datetime.now(timezone.utc).isoformat()
        with transcript.open("a") as stream:
            for payload in ({"type": "agent_message", "message": "Safe to exit: yes"}, {"type": "task_complete"}):
                stream.write(json.dumps({"type": "event_msg", "timestamp": timestamp, "payload": payload}) + "\n")
        worker = {"session_id": self.native, "harness": "codex", "transcript": str(transcript),
                  "pid": 789, "tty": "worker-tty", "supervisor_owned": True,
                  "launch_pid": 789, "launch_generation": "worker-generation", "launch_started_at": "2026-10-01T01:00:00Z"}
        worker["codex_home"] = str(self.home)
        with patch.object(release, "inventory", return_value=[]):
            closed = release.record_closure(self.log, worker, "supervisor")
        self.assertEqual(closed["pid"], worker["pid"])
        self.assertEqual(closed["tty"], worker["tty"])


    def test_handed_off_generation_never_starts_a_native_client(self):
        self.log.parent.mkdir()
        self.log.write_text(json.dumps([{"session_id": self.native, "successor_admitted": True}]))
        self.log.chmod(0o600)
        with self.assertRaises(ValueError):
            SESSION.run(self.args)
        self.spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
