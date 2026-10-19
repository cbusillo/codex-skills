#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Replay a sanitized recorded capacity stop with disposable queue adapters."""

import copy
from contextlib import asynccontextmanager
import json
import os
import socket
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import capacity_retry
import codex_idle_watch
import session_record
import status
import github_plan_native
import github_plan_sessions

NATIVE_CONNECTION = github_plan_native.connection

FIXTURE = Path(__file__).parent / "fixtures" / "codex-capacity-stop.jsonl"
# The records Codex writes when `codex queue` delivers a message to an idle thread.
QUEUED_CONTINUE = Path(__file__).parent / "fixtures" / "codex-queued-continue.jsonl"


class CapacityTests(unittest.TestCase):
    def test_native_transport_failure_records_uncertain_and_never_replays(self):
        import aiohttp
        with patch.object(github_plan_native, "connection", NATIVE_CONNECTION), \
                patch.object(aiohttp.ClientSession, "ws_connect", side_effect=aiohttp.ClientConnectionError("synthetic transport failure")) as connect:
            result = capacity_retry.resume(self.entry, session_record.session_status(self.entry), {}, self.state_path, self.now + 100000)
            self.assertEqual(result["state"], "reconciliation_required")
            stored = capacity_retry.load_state(self.state_path)
            self.assertEqual(stored[self.native_id]["phase"], "uncertain")
            capacity_retry.resume(self.entry, session_record.session_status(self.entry), stored, self.state_path, self.now + 200000)
            self.assertEqual(connect.call_count, 1)
            self.assertTrue(capacity_retry.github_plan_closure.action_pending(self.native_id))

    def test_ownership_busy_before_send_does_not_spend_or_uncertainly_record_an_attempt(self):
        state = {}
        now = self.now + 100000
        with patch.object(capacity_retry.time, "time", return_value=now), patch.object(
                capacity_retry, "queue_continue", side_effect=capacity_retry.github_plan_closure.LeaseBusy("owned")):
            result = capacity_retry.resume(self.entry, session_record.session_status(self.entry), state, self.state_path, now)
        self.assertEqual(result["attempts"], 0)
        self.assertFalse(state)
        self.assertEqual(capacity_retry.load_state(self.state_path), {})

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.enterContext(patch.object(capacity_retry.github_plan_closure, "closed_log", return_value=self.root / "closed.json"))
        self.home = self.root / "account"
        self.transcript = self.home / "sessions" / "fixture.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.records = session_record.read_jsonl(FIXTURE)
        self.transcript.write_text(FIXTURE.read_text())
        self.native_id = self.records[0]["payload"]["id"]
        self.entry = {"session_id": self.native_id, "harness": "codex",
                      "transcript": str(self.transcript), "codex_home": str(self.home),
                      "supervisor_owned": True}
        self.ledger = self.root / "ledger.json"
        self.ledger.write_text(json.dumps([self.entry]))
        self.state_path = self.root / "capacity.json"
        self.now = datetime.fromisoformat(self.records[-1]["timestamp"]).timestamp()
        os.utime(self.transcript, (self.now, self.now))
        endpoint = capacity_retry.codex_daemon_endpoint(self.home)
        endpoint.parent.mkdir()
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.bind(str(endpoint))
        self.socket.listen(128)
        self.addCleanup(self.socket.close)
        self.enterContext(patch.object(github_plan_sessions, "codex_homes", return_value=[self.home]))
        self.queue_requests = []
        self.queue_hook = lambda: None
        @asynccontextmanager
        async def connection(home, **_kwargs):
            async def request(method, params):
                self.assertEqual(method, "thread/queue/add")
                self.queue_hook()
                self.queue_requests.append(params)
                return {"queuedSubmission": {"id": "queued", **{key: params[key] for key in ("clientUserMessageId", "input")}}}
            request.codex_home = str(home.resolve())
            yield request
        self.enterContext(patch.object(github_plan_native, "connection", connection))

    def append(self, kind, **fields):
        record = {"type": "event_msg", "timestamp": datetime.fromtimestamp(
            self.now, timezone.utc).isoformat(), "payload": {"type": kind, **fields}}
        with self.transcript.open("a") as stream:
            stream.write(json.dumps(record) + "\n")
        os.utime(self.transcript, (self.now, self.now))

    def queued_continue(self):
        with self.transcript.open("a") as stream:
            stream.write(QUEUED_CONTINUE.read_text())
        os.utime(self.transcript, (self.now, self.now))

    def capacity_again(self):
        payload = copy.deepcopy(self.records[-1]["payload"])
        kind = payload.pop("type")
        self.append(kind, **payload)

    def continue_sent(self, *_):
        reservation = capacity_retry.load_state(self.state_path)[self.native_id]
        self.assertEqual(reservation["phase"], "pending")
        self.queued_continue()
        self.capacity_again()

    def test_recorded_terminal_error_is_visible_without_agent_text(self):
        result = status.snapshot(self.ledger)[0]
        self.assertEqual(result["state"], "provider_capacity")
        self.assertTrue(result["at_turn_end"])
        self.assertFalse(result["safe_verdict"])
        self.assertEqual(result["last_text"], "")
        self.assertIsNotNone(result["capacity_stop"])

    def test_capacity_queue_invalidates_closure_and_refuses_handed_off_generation(self):
        log = capacity_retry.github_plan_closure.closed_log()
        log.write_text(json.dumps([{"session_id": self.native_id, "closure_evidence": {"old": True}}]))
        log.chmod(0o600)
        home, endpoint = capacity_retry.verified_target(self.entry, session_record.session_status(self.entry))
        def sent():
            self.assertIsNone(json.loads(log.read_text())[0]["closure_evidence"])
        self.queue_hook = sent
        capacity_retry.queue_continue(self.entry, home, endpoint, session_record.session_status(self.entry))
        rows = json.loads(log.read_text())
        rows[0]["successor_admitted"] = True
        log.write_text(json.dumps(rows))
        before = len(self.queue_requests)
        with self.assertRaises(ValueError):
            capacity_retry.queue_continue(self.entry, home, endpoint, session_record.session_status(self.entry))
        self.assertEqual(len(self.queue_requests), before)

    def link_shared_sessions(self):
        sessions = self.home / "sessions"
        shared = self.root / "shared-sessions"
        sessions.rename(shared)
        sessions.symlink_to(shared, target_is_directory=True)
        return shared

    def test_symlinked_live_sessions_resume_with_native_home_and_socket(self):
        shared = self.link_shared_sessions()
        # The ledger can retain either the account path or the resolved path.
        for transcript in (self.transcript, shared / self.transcript.name):
            with self.subTest(transcript=transcript):
                entry = {**self.entry, "transcript": str(transcript)}
                expected = session_record.session_status(entry)
                with patch.object(capacity_retry.time, "time", return_value=self.now + 100000):
                    self.assertEqual(capacity_retry.retry_status(entry, expected, {})["state"],
                                     "retry_due")
                home, endpoint = capacity_retry.verified_target(entry, expected)
                capacity_retry.queue_continue(entry, home, endpoint, expected)
                self.assertEqual(self.queue_requests[-1]["threadId"], self.native_id)

    def test_symlinked_live_sessions_still_reject_outside_transcripts_and_bad_binding(self):
        shared = self.link_shared_sessions()
        outside = self.root / "outside.jsonl"
        outside.write_text(FIXTURE.read_text())
        escape = shared / "escape.jsonl"
        escape.symlink_to(outside)
        expected = session_record.session_status(self.entry)
        for changed in ({"transcript": str(outside)}, {"transcript": str(escape)},
                        {"supervisor_owned": False}, {"session_id": "readable-title"},
                        {"codex_home": str(self.root)}):
            with self.subTest(changed=changed):
                with self.assertRaises((ValueError, OSError)):
                    capacity_retry.verified_target({**self.entry, **changed}, expected)
        capacity_retry.codex_daemon_endpoint(self.home).unlink()
        with self.assertRaises(OSError):
            capacity_retry.verified_target(self.entry, expected)

    def test_other_activity_and_errors_do_not_trigger_retries(self):
        text = self.records[-1]["payload"]["error"]["message"]
        for records in (
            self.records[:-1] + [{"type": "event_msg", "payload": {
                "type": "agent_message", "message": text}}],
            self.records + [{"type": "event_msg", "payload": {"type": "task_started"}}],
            self.records + [{"type": "event_msg", "payload": {"type": "user_message"}}],
            self.records + [{"type": "event_msg", "payload": {"type": "turn_aborted"}}],
            self.records[:-1] + [{"type": "event_msg", "payload": {
                "type": "task_complete", "error": {"message": "Rate limit reached",
                                                      "codex_error_info": "usage_limit_exceeded"}}}],
            self.records[:-1] + [{"type": "event_msg", "payload": {
                "type": "task_complete", "error": {"message": "Service unavailable",
                                                      "codex_error_info": "server_overloaded"}}}],
        ):
            with self.subTest(records=records[-1]):
                summary = session_record.summarize(records, "codex")
                self.assertIsNone(summary["capacity_stop"])
                with patch.object(capacity_retry, "queue_continue") as queue:
                    result = capacity_retry.resume(self.entry, summary, {}, self.state_path,
                                                   self.now + 10000)
                queue.assert_not_called()
                self.assertEqual(result["state"], "inactive")
        self.assertIsNone(session_record.summarize(self.records, "claude")["capacity_stop"])

    def test_backoff_three_tries_restart_and_digest_exhaustion(self):
        for attempt, delay in enumerate(capacity_retry.BACKOFF_SECONDS):
            with capacity_retry.locked_state(self.state_path) as state:
                summary = session_record.session_status(self.entry)
                with patch.object(capacity_retry, "queue_continue", side_effect=self.continue_sent) as queue:
                    self.now += delay - 1
                    before = capacity_retry.resume(self.entry, summary, state, self.state_path, self.now)
                    self.assertEqual(before["attempts"], attempt)
                    queue.assert_not_called()
                    self.now += 1
                    after = capacity_retry.resume(self.entry, summary, state, self.state_path, self.now)
                    self.assertEqual(after["state"], "continue_confirmed")
                    self.assertEqual(after["attempts"], attempt + 1)
                    queue.assert_called_once()
            # Reload from disk every time, as a bounded service restart does.
            self.assertEqual(capacity_retry.load_state(self.state_path)[self.native_id]["attempts"],
                             attempt + 1)
        with capacity_retry.locked_state(self.state_path) as state:
            with patch.object(capacity_retry, "queue_continue") as queue:
                result = capacity_retry.resume(self.entry, session_record.session_status(self.entry),
                                               state, self.state_path, self.now + 100000)
            queue.assert_not_called()
            self.assertEqual(result["state"], "exhausted")
        self.assertEqual(status.snapshot(self.ledger, self.state_path)[0]["capacity_retry"], result)
        # File idleness uses this disposable file's clock, not the archived trace.
        notice = codex_idle_watch.poll(self.ledger, {}, 90, self.transcript.stat().st_mtime + 100000,
                                      capacity_retry.load_state(self.state_path))[0]
        self.assertEqual(notice["capacity_retry"]["state"], "exhausted")

    def test_uncertain_send_and_crash_reservation_never_replay(self):
        for phase in ("pending", "uncertain"):
            state = {self.native_id: {"attempts": 1, "phase": phase,
                     "last_attempt": self.now, "target": capacity_retry.target(self.entry)}}
            capacity_retry.save_state(self.state_path, state)
            with patch.object(capacity_retry, "queue_continue") as queue:
                result = capacity_retry.resume(self.entry, session_record.session_status(self.entry),
                                               capacity_retry.load_state(self.state_path),
                                               self.state_path, self.now + 100000)
            queue.assert_not_called()
            self.assertEqual(result["state"], "reconciliation_required")

    def test_queue_failure_or_missing_readback_preserves_reservation(self):
        for error in (subprocess.TimeoutExpired("queue", 10), None):
            with self.subTest(error=error):
                state = {}
                with patch.object(capacity_retry, "queue_continue", side_effect=error), \
                     patch.object(capacity_retry, "readback_continue", return_value=False):
                    result = capacity_retry.resume(self.entry, session_record.session_status(self.entry),
                                                   state, self.state_path, self.now + 100000)
                self.assertEqual(result["state"], "reconciliation_required")
                self.assertEqual(capacity_retry.load_state(self.state_path)[self.native_id]["phase"],
                                 "uncertain")

    def test_verification_refuses_other_home_identity_or_new_activity_before_send(self):
        original = session_record.session_status(self.entry)
        for changed in ({"codex_home": str(self.root)}, {"supervisor_owned": False},
                        {"session_id": "readable-title"}, {"codex_home": ""}):
            with self.subTest(changed=changed), patch.object(capacity_retry, "queue_continue") as queue:
                with self.assertRaises((ValueError, OSError)):
                    capacity_retry.verified_target({**self.entry, **changed}, original)
                queue.assert_not_called()
        self.append("user_message", message="owner work")
        with self.assertRaises(ValueError):
            capacity_retry.verified_target(self.entry, original)

    def test_unowned_session_and_read_only_watcher_send_nothing(self):
        self.ledger.write_text(json.dumps([{**self.entry, "supervisor_owned": False}]))
        with patch.object(capacity_retry, "queue_continue") as queue:
            codex_idle_watch.poll(self.ledger, {}, 90, self.now + 100000, {}, self.state_path, True)
            self.ledger.write_text(json.dumps([self.entry]))
            codex_idle_watch.poll(self.ledger, {}, 90, self.now + 100000, {}, self.state_path)
        queue.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_state_lock_and_invalid_budget_refuse(self):
        with capacity_retry.locked_state(self.state_path):
            with self.assertRaises(ValueError):
                with capacity_retry.locked_state(self.state_path):
                    self.fail("concurrent capacity watcher was allowed")
        self.state_path.write_text(json.dumps({self.native_id: {"attempts": -1}}))
        with self.assertRaises(ValueError):
            capacity_retry.load_state(self.state_path)

    def test_changed_target_and_unavailable_socket_are_reported_without_sends(self):
        state = {self.native_id: {"attempts": 1, "phase": "confirmed",
                 "last_attempt": self.now, "target": capacity_retry.target(self.entry)}}
        changed = {**self.entry, "codex_home": str(self.root)}
        with patch.object(capacity_retry, "queue_continue") as queue:
            result = capacity_retry.resume(changed, session_record.session_status(self.entry),
                                           state, self.state_path, self.now + 100000)
            self.assertEqual(result["state"], "target_changed")
            capacity_retry.codex_daemon_endpoint(self.home).unlink()
            self.now += 100000
            result = capacity_retry.resume(self.entry, session_record.session_status(self.entry),
                                           {}, self.state_path, self.now)
            self.assertEqual(result["state"], "blocked")
            self.assertEqual(status.snapshot(self.ledger, self.state_path)[0]["capacity_retry"]["state"],
                             "blocked")
        queue.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_stale_socket_and_overdue_retry_are_visible_without_cli_dependency(self):
        summary = session_record.session_status(self.entry)
        with patch.object(capacity_retry.time, "time", return_value=self.now + 100000):
            due = capacity_retry.retry_status(self.entry, summary, {})
        self.assertEqual(due["state"], "retry_due")
        self.assertGreater(due["retry_at"], self.now)
        self.socket.close()
        self.assertEqual(capacity_retry.retry_status(self.entry, summary, {})["state"], "blocked")

    def test_failed_post_send_save_stops_this_watcher_too(self):
        state = {}
        original_save = capacity_retry.save_state
        writes = 0

        def save(path, data):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("fixture disk failure")
            original_save(path, data)

        with patch.object(capacity_retry, "save_state", side_effect=save), \
             patch.object(capacity_retry, "queue_continue", side_effect=self.continue_sent):
            with self.assertRaises(OSError):
                capacity_retry.resume(self.entry, session_record.session_status(self.entry),
                                      state, self.state_path, self.now + 100000)
        self.assertEqual(state[self.native_id]["phase"], "uncertain")
        self.assertEqual(capacity_retry.load_state(self.state_path)[self.native_id]["phase"], "pending")
        with patch.object(capacity_retry, "queue_continue") as queue:
            result = capacity_retry.resume(self.entry, session_record.session_status(self.entry),
                                           state, self.state_path, self.now + 200000)
        queue.assert_not_called()
        self.assertEqual(result["state"], "reconciliation_required")

    def test_queue_uses_verified_native_target_and_home(self):
        expected = session_record.session_status(self.entry)
        home, endpoint = capacity_retry.verified_target(self.entry, expected)
        capacity_retry.queue_continue(self.entry, home, endpoint, expected)
        params = self.queue_requests[-1]
        self.assertEqual(params["threadId"], self.entry["session_id"])
        self.assertEqual(len(self.queue_requests), 1)

    def test_readback_requires_new_continue_from_same_native_transcript(self):
        size = self.transcript.stat().st_size
        self.append("agent_message", message="continue")
        for role, text in (("assistant", "continue"), ("user", "owner work")):
            with self.transcript.open("a") as stream:
                stream.write(json.dumps({"type": "response_item", "payload": {
                    "type": "message", "role": role,
                    "content": [{"type": "input_text", "text": text}]}}) + "\n")
        self.assertFalse(capacity_retry.readback_continue(self.entry, size, timeout=0))
        before_continue = self.transcript.stat().st_size
        self.queued_continue()
        self.assertTrue(capacity_retry.readback_continue(self.entry, size, timeout=0))
        self.assertFalse(capacity_retry.readback_continue(
            self.entry, self.transcript.stat().st_size, timeout=0))
        self.assertTrue(capacity_retry.readback_continue(self.entry, before_continue, timeout=0))
        self.assertFalse(capacity_retry.readback_continue({**self.entry, "session_id": "other"},
                                                        size, timeout=0))


if __name__ == "__main__":
    unittest.main()
