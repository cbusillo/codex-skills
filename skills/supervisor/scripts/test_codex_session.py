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
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import uuid
from iterm_tab import codex_daemon_endpoint
import session_liveness
import session_record
from session_record import LAUNCH_LEDGER_ENV

SPEC = importlib.util.spec_from_file_location("codex_session", Path(__file__).with_name("codex-session.py"))
SESSION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SESSION)
import github_plan_sessions
import github_plan_native
github_agent = importlib.import_module("github_agent")


class ResumeTests(unittest.TestCase):
    def test_queue_uses_the_recorded_lead_home_only_for_its_current_managed_worker(self) -> None:
        for harness in ("claude", "codex", "agy"):
            with self.subTest(harness=harness):
                self.assert_managed_worker_queue(harness)

    def test_note_to_a_handed_off_lead_is_refused_naming_the_successor(self) -> None:
        worker, successor = str(uuid.uuid4()), str(uuid.uuid4())
        ledger = self.root / "launch-ledger.json"
        ledger.write_text(json.dumps([
            {"session_id": worker, "harness": "codex", "supervisor_owned": True, "lead_session_id": successor},
            {"session_id": self.native, "harness": "codex", "supervisor_owned": True, "codex_home": str(self.home)}]))
        argv = ["codex-session.py", "queue", "--session-id", self.native, "--message", "https://github.com/fixture/repo/issues/7"]
        output = io.StringIO()
        with patch.dict(os.environ, {LAUNCH_LEDGER_ENV: str(ledger)}), \
                patch.dict(github_agent.__dict__, {"ancestor_agent": MagicMock(return_value=None)}):
            for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CODEX_THREAD_ID", "ANTIGRAVITY_SOURCE_METADATA"):
                os.environ.pop(name, None)
            with patch.dict(os.environ, {"CODEX_SESSION_ID": worker}), patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(output):
                code = SESSION.main()
        self.assertEqual(code, 1)
        self.assertIn(successor, output.getvalue())
        self.assertFalse(self.queue_requests)

    def assert_managed_worker_queue(self, harness: str) -> None:
        worker = str(uuid.uuid4())
        ledger = self.root / "launch-ledger.json"
        nested_lead_unreachable_without_a_worker_row = {
            "session_id": self.native, "harness": "codex", "supervisor_owned": True, "codex_home": str(self.home),
            "lead_session_id": str(uuid.uuid4())}
        rows = [{"session_id": worker, "harness": harness, "supervisor_owned": True, "lead_session_id": self.native},
                nested_lead_unreachable_without_a_worker_row]
        argv = ["codex-session.py", "queue", "--session-id", self.native, "--message", "https://github.com/fixture/repo/issues/7"]
        self.thread_state = "idle"

        def execute() -> dict:
            output = io.StringIO()
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
                code = SESSION.main()
            result = json.loads(output.getvalue())
            self.assertEqual(code, 0 if result["ok"] else 1)
            return result

        source_var = {"claude": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_SESSION_ID", "agy": "ANTIGRAVITY_SOURCE_METADATA"}[harness]
        source = json.dumps({"tool": {"conversationId": worker}}) if harness == "agy" else worker
        with patch.dict(os.environ, {LAUNCH_LEDGER_ENV: str(ledger)}), \
                patch.dict(github_agent.__dict__, {"ancestor_agent": MagicMock(return_value=None)}):
            for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CODEX_SESSION_ID", "CODEX_THREAD_ID", "ANTIGRAVITY_SOURCE_METADATA"):
                os.environ.pop(name, None)
            os.environ[source_var] = source
            ledger.write_text(json.dumps(rows))
            self.assertTrue(execute()["ok"])
            self.assertEqual(len(self.queue_requests), 1)
            self.queue_requests.clear()
            wrong_source = json.dumps({"tool": {"conversationId": str(uuid.uuid4())}}) if harness == "agy" else str(uuid.uuid4())
            invalid_sources = [wrong_source]
            if harness == "agy":
                invalid_sources.extend(["", "malformed", "null", "[]", "{}", '{"tool": []}',
                                        json.dumps({"tool": {"conversationId": {"id": worker}}}),
                                        json.dumps({"tool": {"conversationId": [worker, str(uuid.uuid4())]}})])
            for invalid_source in invalid_sources:
                with self.subTest(source=invalid_source), patch.dict(os.environ, {source_var: invalid_source}):
                    self.assertFalse(execute()["ok"])
                    self.assertFalse(self.queue_requests)
            if harness == "agy":
                for nested in github_agent.FAMILIES:
                    with self.subTest(nested=nested), patch.dict(
                            github_agent.__dict__, {"ancestor_agent": MagicMock(return_value=nested)}):
                        self.assertFalse(execute()["ok"])
                        self.assertFalse(self.queue_requests)
                for inherited in ({"CODEX_THREAD_ID": str(uuid.uuid4())},
                                  {"CLAUDE_CODE_SESSION_ID": str(uuid.uuid4())},
                                  {"CODEX_SESSION_ID": worker, "CLAUDE_CODE_SESSION_ID": worker}):
                    with self.subTest(inherited=inherited), patch.dict(os.environ, inherited):
                        self.assertFalse(execute()["ok"])
                        self.assertFalse(self.queue_requests)
            if harness == "codex":
                with patch.dict(os.environ, {"CODEX_THREAD_ID": str(uuid.uuid4())}):
                    self.assertFalse(execute()["ok"])
                    self.assertFalse(self.queue_requests)
            with patch.dict(os.environ):
                os.environ.pop(source_var)
                self.assertFalse(execute()["ok"])
                self.assertFalse(self.queue_requests)
            for changed in ([rows[1]], [*rows, rows[0]], [*rows, rows[1]],
                            [{**rows[0], "supervisor_owned": False}, rows[1]],
                            [{**rows[0], "harness": "other"}, rows[1]],
                            [{**rows[0], "lead_session_id": "other"}, rows[1]],
                            [rows[0], {**rows[1], "supervisor_owned": False}],
                            [rows[0], {**rows[1], "harness": "claude"}],
                            [rows[0], {**rows[1], "codex_home": "relative-home"}],
                            [rows[0], {**rows[1], "codex_home": None}]):
                with self.subTest(rows=changed):
                    ledger.write_text(json.dumps(changed))
                    self.assertFalse(execute()["ok"])
                    self.assertFalse(self.queue_requests)
            ledger.write_text("malformed")
            self.assertFalse(execute()["ok"])
            ledger.unlink()
            self.assertFalse(execute()["ok"])
            os.environ.pop(LAUNCH_LEDGER_ENV)
            self.assertFalse(execute()["ok"])
            self.assertFalse(self.queue_requests)
            ledger.write_text(json.dumps(rows))
            argv.extend(["--ledger", ledger.name])
            with contextlib.chdir(self.root):
                self.assertFalse(execute()["ok"])
                self.assertFalse(self.queue_requests)
            argv[-1] = str(ledger)
            self.assertTrue(execute()["ok"])
            self.assertEqual(len(self.queue_requests), 1)
            self.queue_requests.clear()
            argv.extend(["--codex-home", str(self.home)])
            with patch.object(sys, "argv", argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                SESSION.main()

    def test_unrecorded_root_caller_queues_only_to_one_exact_top_level_lead(self) -> None:
        supervisor, worker, other = (str(uuid.uuid4()) for _ in range(3))
        ledger = self.root / "launch-ledger.json"
        lead = {"session_id": self.native, "harness": "codex", "supervisor_owned": True, "tab_id": "@105",
                "codex_home": str(self.home)}
        reused_tab = [{"session_id": other, "harness": "codex", "supervisor_owned": True, "tab_id": "@287",
                       "codex_home": str(self.home)},
                      {"session_id": worker, "harness": "agy", "supervisor_owned": True, "tab_id": "@287",
                       "lead_session_id": self.native}]
        argv = ["codex-session.py", "queue", "--session-id", self.native, "--ledger", str(ledger), "--message", "notice"]
        self.thread_state = "idle"

        def execute(ledger_rows: list, environment: dict[str, str]) -> bool:
            ledger.write_text(json.dumps(ledger_rows))
            output = io.StringIO()
            with patch.object(sys, "argv", argv), patch.dict(os.environ, environment), contextlib.redirect_stdout(output):
                code = SESSION.main()
            delivered = bool(self.queue_requests)
            self.assertEqual(code == 0, delivered)
            self.queue_requests.clear()
            return delivered

        root = {"CODEX_THREAD_ID": supervisor, "CODEX_SESSION_ID": supervisor}
        with patch.dict(os.environ), patch.dict(github_agent.__dict__, {"ancestor_agent": MagicMock(return_value=None)}):
            for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CODEX_SESSION_ID", "CODEX_THREAD_ID",
                         "ANTIGRAVITY_SOURCE_METADATA", LAUNCH_LEDGER_ENV):
                os.environ.pop(name, None)
            self.assertTrue(execute([lead], root))
            self.assertTrue(execute([lead, *reused_tab], root))
            self.assertTrue(execute([lead], {"CLAUDE_CODE_SESSION_ID": supervisor}))
            for rows, caller in (
                    ([lead, {**lead, "tab_id": "@106"}], root),
                    ([{**lead, "lead_session_id": other}], root),
                    ([{**lead, "supervisor_owned": False}], root),
                    ([{**lead, "harness": "claude"}], root),
                    ([{**lead, "codex_home": "relative-home"}], root),
                    ([{**lead, "session_id": other}], root),
                    ([lead], {"CODEX_THREAD_ID": supervisor, "CODEX_SESSION_ID": other}),
                    ([lead], {}),
                    ([lead, {"session_id": supervisor, "harness": "codex", "supervisor_owned": False}], root),
                    ([lead, *[{"session_id": supervisor, "harness": "codex", "supervisor_owned": True}] * 2], root),
                    ([lead, {"session_id": supervisor, "harness": "codex", "supervisor_owned": True,
                             "lead_session_id": other}], root),
                    ([lead, "malformed"], root)):
                with self.subTest(rows=rows, caller=caller):
                    self.assertFalse(execute(rows, caller))
            other_leads_worker = {"session_id": supervisor, "harness": "codex", "supervisor_owned": True,
                                  "lead_session_id": other}
            rows = [lead, other_leads_worker]
            ledger_vanishing_after_first_read = MagicMock(side_effect=[rows, [], [], []])
            with patch.dict(SESSION.__dict__, {"read_ledger": ledger_vanishing_after_first_read}), \
                    patch.object(session_record, "read_ledger", ledger_vanishing_after_first_read):
                self.assertFalse(execute(rows, root))
            reused_home = self.root / "reused"
            reused_home.mkdir()
            self.assertFalse(execute([{**lead, "codex_home": str(reused_home)}], root))
            self.thread_state = "notLoaded"
            self.assertFalse(execute([lead], root))

    def test_queue_cli_preserves_certain_absence_but_never_relabels_permission_or_native_refusal(self) -> None:
        for cause, not_sent in ((ValueError("not loaded"), True), (PermissionError("denied"), False),
                                (TimeoutError(), True), (SESSION.NativeRequestRejected("rejected"), False)):
            error = SESSION.QueueNotSent(str(cause))
            error.__cause__ = cause
            output = io.StringIO()
            with self.subTest(cause=type(cause)), patch.object(sys, "argv", ["codex-session.py", "queue",
                    "--session-id", self.native, "--codex-home", str(self.home), "--message", "notice"]), \
                    patch.dict(SESSION.__dict__, {"run": MagicMock(side_effect=error)}), contextlib.redirect_stdout(output):
                self.assertEqual(SESSION.main(), 1)
            self.assertEqual(json.loads(output.getvalue())["not_sent"], not_sent)
            self.assertTrue(json.loads(output.getvalue())["error"])

    def test_generic_launch_and_resume_preserve_the_callers_label(self) -> None:
        observed = []

        async def native_client(*args: object, **kwargs: object) -> int:
            observed.append(os.environ.get("DISCORD_BLUE_SESSION_KIND"))
            return 0

        for command in ("launch", "resume"):
            for label in (None, "spawned"):
                with self.subTest(command=command, label=label), patch.dict(os.environ):
                    os.environ.pop("DISCORD_BLUE_SESSION_KIND", None)
                    if label is not None:
                        os.environ["DISCORD_BLUE_SESSION_KIND"] = label
                    self.args.command = command
                    self.args.native_args = ["codex", "--", "fixture"]
                    with patch.object(SESSION, "run_client", side_effect=native_client):
                        self.assertEqual(SESSION.run(self.args), 0)
                    self.assertEqual(observed[-1], label)

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
        self.pending = []
        @contextlib.asynccontextmanager
        async def connection(home, **_kwargs):
            async def request(method, params):
                if method == "thread/queue/add":
                    self.queue_hook()
                    self.queue_requests.append(params)
                    return {"queuedSubmission": {"id": "queued", **{key: params[key] for key in ("clientUserMessageId", "input")}}}
                if method == "thread/read":
                    return {"thread": {"id": self.native, "path": str(path), "status": {"type": self.thread_state}}}
                if method == "thread/queue/start":
                    return {"turn": {"id": "queue-started-turn"}}
                if method == "thread/queue/list":
                    return {"data": self.pending, "nextCursor": None}
                return {"data": [], "nextCursor": None}
            request.codex_home = str(home.resolve())
            yield request
        self.enterContext(patch.object(github_plan_native, "connection", connection))
        self.enterContext(patch.object(session_liveness, "inventory", return_value=[]))
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


    def test_status_of_a_fresh_launch_reports_its_queue_instead_of_failing(self):
        self.thread_state = "idle"
        self.pending = [{"id": "held", "clientUserMessageId": "client", "input": []}]
        output = io.StringIO()
        with patch.object(sys, "argv", ["codex-session.py", "status", "--session-id", self.native,
                "--codex-home", str(self.home)]), contextlib.redirect_stdout(output):
            self.assertEqual(SESSION.main(), 0)
        status = json.loads(output.getvalue())
        self.assertFalse(self.log.exists())
        self.assertEqual(status["thread_state"], "idle")
        self.assertEqual([entry["queuedSubmissionId"] for entry in status["queue"]], ["held"])

    def test_handed_off_generation_never_starts_a_native_client(self):
        self.log.parent.mkdir()
        self.log.write_text(json.dumps([{"session_id": self.native, "successor_admitted": True}]))
        self.log.chmod(0o600)
        with self.assertRaises(ValueError):
            SESSION.run(self.args)
        self.spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
