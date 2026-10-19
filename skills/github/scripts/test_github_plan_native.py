# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Synthetic native facts and disposable daemon transport; no native client actions."""
import asyncio
import copy
import threading
import json
import io
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import github_plan_native as NATIVE
import github_plan_closure as CLOSURE
from test_github_plan_sessions import ReceiptFixture


class NativeFactsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = "source"
        self.path = "/fixture/source.jsonl"
        self.calls = []
        self.responses = {
            "thread/read": {"thread": {"id": self.session, "path": self.path, "historyMode": "paginated", "status": {"type": "idle"}}},
            "thread/queue/list": {"data": [], "nextCursor": None},
            "thread/items/list": {"data": [], "nextCursor": None},
            "thread/turns/list": {"data": [{"id": "actual-turn", "status": "completed"}], "nextCursor": None},
            "thread/backgroundTerminals/list": {"data": [], "nextCursor": None},
            "thread/goal/get": {"goal": None},
            "thread/timeline/list": {"data": [], "nextCursor": None},
        }
        self.source = {"path": self.path, "transcripts": {self.path: {"status": {
            "turn_id": "actual-turn", "turn_completed_at": "2026-10-19T00:00:00Z"}}}}

    async def request(self, method, params):
        self.calls.append((method, params))
        result = self.responses[method]
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return await result(params)
        return copy.deepcopy(result)

    async def test_loaded_current_work_and_actual_turn_association(self):
        facts = await NATIVE.read_thread_facts(self.request, self.session)
        NATIVE.require_quiet(facts, self.source, {})
        for changed in ("background", "goal", "realtime", "turn", "execution"):
            with self.subTest(changed=changed):
                altered = copy.deepcopy(facts)
                if changed == "background": altered["background"] = [("command", "handle")]
                elif changed == "goal": altered["goal"] = "active"
                elif changed == "realtime": altered["realtime"] = ["realtime"]
                elif changed == "turn": altered["turns"][0]["id"] = "different-actual-turn"
                else: altered["executions"]["ended"] = False
                with self.assertRaises(ValueError): NATIVE.require_quiet(altered, self.source, {})

    async def test_loaded_persistence_read_failure_cannot_use_old_quiet_history(self):
        previous = self.responses["thread/read"]
        async def thread(params):
            if params.get("includeTurns"):
                raise ValueError("synthetic canonical persistence failed")
            return copy.deepcopy(previous)
        self.responses["thread/read"] = thread
        with self.assertRaisesRegex(ValueError, "persistence failed"):
            await NATIVE.read_thread_facts(self.request, self.session)

    async def test_malformed_realtime_and_native_status_never_prove_quiet(self):
        for item in ({"realtimeSessionId": "runtime"},
                     {"id": "item", "realtimeSessionId": "runtime", "type": "unknownFutureRealtime"}):
            self.responses["thread/timeline/list"] = {"data": [{"type": "realtime", "position": 1, "item": item}]}
            with self.subTest(item=item), self.assertRaises(ValueError):
                await NATIVE.read_thread_facts(self.request, self.session)
        self.responses["thread/read"]["thread"]["status"] = None
        with self.assertRaises(ValueError): await NATIVE.read_thread_facts(self.request, self.session)

    async def test_unknown_required_item_and_timeline_variants_refuse(self):
        for method, entry in (("thread/items/list", {"turnId": "actual-turn", "item": {
                "id": "future", "type": "unknownFutureExecution", "status": "running"}}),
                ("thread/timeline/list", {"position": 1, "type": "unknownFutureTimeline"})):
            with self.subTest(method=method):
                self.responses[method]["data"] = [entry]
                with self.assertRaises(ValueError):
                    await NATIVE.read_thread_facts(self.request, self.session)
                self.responses[method]["data"] = []

    async def test_notloaded_optional_index_is_bounded_and_not_current_work(self):
        self.responses["thread/read"]["thread"]["status"] = {"type": "notLoaded"}
        async def slow(_params):
            await asyncio.sleep(2)
            return {"data": [], "nextCursor": None}
        self.responses["thread/turns/list"] = slow
        async with asyncio.timeout(1):
            facts = await NATIVE.read_thread_facts(self.request, self.session)
        NATIVE.require_quiet(facts, self.source, {})
        self.assertFalse(facts["history_complete"])
        self.assertNotIn("thread/items/list", [method for method, _ in self.calls])
        self.assertNotIn("thread/goal/get", [method for method, _ in self.calls])

    async def test_notloaded_native_order_must_anchor_a_later_actual_turn(self):
        self.responses["thread/read"]["thread"]["status"] = {"type": "notLoaded"}
        for turns in ([], [{"id": "older-turn", "status": "interrupted"}],
                      [{"id": "unknown-index-turn", "status": "interrupted"}]):
            self.responses["thread/turns/list"]["data"] = turns
            NATIVE.require_quiet(await NATIVE.read_thread_facts(self.request, self.session), self.source, {})
        self.responses["thread/turns/list"]["data"] = [
            {"id": "later-turn", "status": "interrupted"}, {"id": "actual-turn", "status": "completed"}]
        with self.assertRaises(ValueError):
            NATIVE.require_quiet(await NATIVE.read_thread_facts(self.request, self.session), self.source, {})
        self.responses["thread/turns/list"]["data"].append({"id": "malformed", "status": None})
        with self.assertRaises(ValueError):
            NATIVE.require_quiet(await NATIVE.read_thread_facts(self.request, self.session), self.source, {})

    async def test_older_safe_copy_does_not_replace_primary_actual_turn_authority(self):
        primary = self.source["transcripts"][self.path]["status"]
        primary["actual_turn_ids"] = ["older-turn", "actual-turn"]
        self.source["transcripts"]["/fixture/older-copy.jsonl"] = {"status": {
            "turn_id": "older-turn", "actual_turn_ids": ["older-turn"],
            "turn_completed_at": "2026-10-01T00:00:00Z"}}
        facts = await NATIVE.read_thread_facts(self.request, self.session)
        NATIVE.require_quiet(facts, self.source, {})
        self.source["transcripts"]["/fixture/older-copy.jsonl"]["status"].update(
            turn_id="later-copy-turn", actual_turn_ids=["older-turn", "actual-turn", "later-copy-turn"])
        with self.assertRaises(ValueError): NATIVE.require_quiet(facts, self.source, {})

    async def test_loaded_index_may_lag_completed_source_but_actual_order_cannot_conflict(self):
        self.source["transcripts"][self.path]["status"]["actual_turn_ids"] = ["older-turn", "actual-turn"]
        self.responses["thread/turns/list"]["data"] = [{"id": "older-turn", "status": "completed"}]
        NATIVE.require_quiet(await NATIVE.read_thread_facts(self.request, self.session), self.source, {})

        for rows in ([{"id": "unanchored", "status": "completed"}],
                     [{"id": "older-turn", "status": "completed"}, {"id": "actual-turn", "status": "completed"}]):
            self.responses["thread/turns/list"]["data"] = rows
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                NATIVE.require_quiet(await NATIVE.read_thread_facts(self.request, self.session), self.source, {})
        self.responses["thread/turns/list"]["data"] = [{"id": "actual-turn", "status": "interrupted"}]
        NATIVE.require_quiet(await NATIVE.read_thread_facts(self.request, self.session), self.source, {})

    async def test_opaque_floor_requires_safe_message_as_well_as_actual_completion(self):
        status = self.source["transcripts"][self.path]["status"]
        status.update(message_at="2026-10-19T00:00:00Z", turn_completed_at="2026-10-19T00:00:02Z")
        action = {"action_pending": True, "action_kind": "opaque", "action_at": "2026-10-19T00:00:01Z"}
        facts = await NATIVE.read_thread_facts(self.request, self.session)
        with self.assertRaises(ValueError): NATIVE.require_quiet(facts, self.source, action)
        status["message_at"] = "2026-10-19T00:00:02Z"
        NATIVE.require_quiet(facts, self.source, action)

    async def test_queue_digest_covers_full_input_and_repeated_cursor_refuses(self):
        entry = {"id": "queued", "clientUserMessageId": "client", "input": [{"type": "text", "text": "one"}]}
        self.responses["thread/queue/list"]["data"] = [entry]
        first = await NATIVE.read_queue(self.request, self.session)
        entry["input"][0]["text"] = "two"
        second = await NATIVE.read_queue(self.request, self.session)
        self.assertNotEqual(first, second)
        self.assertNotIn("two", json.dumps(second))
        self.responses["thread/queue/list"] = {"data": [], "nextCursor": "repeated"}
        with self.assertRaises(ValueError): await NATIVE.read_queue(self.request, self.session)

    async def test_terminal_interrupted_collaborator_is_not_running_execution(self):
        self.responses["thread/items/list"]["data"] = [{"turnId": "old-turn", "item": {
            "id": "collaborator", "type": "collabAgentToolCall", "status": "interrupted"}}]
        self.assertTrue((await NATIVE.read_executions(self.request, self.session))["ended"])


class NativeQueueTests(unittest.TestCase):
    def test_finish_keeps_one_native_cutoff_through_proof_and_later_safe_turn(self):
        import uuid
        from github_plan_sessions import native_helpers
        native_helpers()
        import tmux_terminal
        for phase in ("first", "resumed", "later"):
            with self.subTest(phase=phase), ReceiptFixture(str(uuid.uuid4())) as f:
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
                entry = {**f.entry, "tmux_socket": "fixture", "tmux_session_id": "tmux:fixture:" + "a" * 32,
                         "window_id": "$1", "tab_id": "@1", "tty_exited_at": "2099-01-01T00:00:00Z"}
                pane = {"session_id": entry["tmux_session_id"], "pane_id": "%1", "pid": 789,
                        "tmux_socket": "fixture", "window_id": "$1", "tab_id": "@1", "tty": entry["tty"],
                        "dead": True, "exited_at": cutoff.isoformat()}
                if phase == "first": CLOSURE.replace_rows([])
                if phase == "later":
                    CLOSURE.replace_rows([])
                    with patch.object(tmux_terminal.Terminal, "list", return_value=[pane]):
                        original = f.release.record_closure(f.log, entry, "supervisor")
                with CLOSURE.resume_guard(f.session, method="turn/start", home=f.home):
                    if phase == "resumed":
                        entry["launch_generation"] = "new-generation"
                        metadata = {key: entry[key] for key in ("codex_home", "launch_pid", "launch_generation", "launch_started_at")}
                        CLOSURE.resumed_launch(f.session, metadata, str(f.transcript), entry["tty"])
                if phase == "later":
                    later = datetime.now(timezone.utc).isoformat()
                    with f.transcript.open("a") as stream:
                        for payload in ({"type": "task_started", "turn_id": "later-safe-turn"},
                                        {"type": "agent_message", "message": "Safe to exit: yes"},
                                        {"type": "task_complete", "turn_id": "later-safe-turn"}):
                            stream.write(json.dumps({"type": "event_msg", "timestamp": later, "payload": payload}) + "\n")
                    alias = f.root / "alias-account"
                    alias.symlink_to(f.home, target_is_directory=True)
                    entry.update(codex_home=str(alias), transcript=str(alias / "sessions" / f.transcript.name),
                                 tty="/dev/" + f.entry["tty"])
                f.native_state = "notLoaded"
                f.process_rows = [(456, f.entry["tty"], "unrelated-worker")]
                reads = []
                def panes():
                    reads.append(True)
                    return [pane] if phase != "later" and len(reads) == 1 else []
                with patch.object(tmux_terminal.Terminal, "list", side_effect=panes), \
                        patch.object(CLOSURE, "process_started_at", return_value=cutoff + timedelta(seconds=1)):
                    closed = f.release.record_closure(f.log, entry, "supervisor")
                    self.assertEqual(closed["tty_exited_at"], cutoff.isoformat())
                    if phase == "later": self.assertGreater(closed["verdict_at"], original["verdict_at"])
                    self.assertFalse(CLOSURE.action_pending(f.session))
                    retried = f.release.record_closure(f.log, entry, "supervisor")
                    self.assertEqual(retried["ended_at"], closed["ended_at"])
                    self.assertEqual(retried["tty_exited_at"], closed["tty_exited_at"])
                self.assertFalse(f.mutations)

    def test_saved_native_cutoff_preserves_guarded_replacement_identity(self):
        import uuid
        from github_plan_sessions import native_helpers
        native_helpers()
        import tmux_terminal
        with ReceiptFixture(str(uuid.uuid4())) as f:
            CLOSURE.replace_rows([])
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
            entry = {**f.entry, "tmux_socket": "fixture", "tmux_session_id": "tmux:fixture:" + "a" * 32,
                     "window_id": "$1", "tab_id": "@1"}
            pane = {"session_id": entry["tmux_session_id"], "pane_id": "%1", "pid": 789,
                    "tmux_socket": "fixture", "window_id": "$1", "tab_id": "@1", "tty": entry["tty"],
                    "dead": True, "exited_at": cutoff.isoformat()}
            with patch.object(tmux_terminal.Terminal, "list", return_value=[pane]), \
                    patch.object(CLOSURE, "evidence", side_effect=ValueError("fixture optional receipt unavailable")):
                closed = f.release.record_closure(f.log, entry, "supervisor")
            f.native_state = "notLoaded"
            self.assertIsNone(CLOSURE.verified_record(f.session, f.home))
            native = ["codex", "--remote", "unix://" + str(CLOSURE.daemon_endpoint(f.home)), "--", "new task"]
            with patch.object(CLOSURE, "process_started_at", return_value=cutoff + timedelta(seconds=1)):
                f.managed(native)
                f.commands = "456 " + " ".join(native)
                f.process_rows = [(456, entry["tty"], " ".join(native))]
                self.assertTrue(f.release.validate_closure(closed, "supervisor")["safe_verdict"])
                NATIVE.verify_source_process({**entry, "tty_exited_at": "2099-01-01T00:00:00Z"})
                self.assertEqual(f.observe()[1][f.session]["state"], "ended")
                f.process_rows = [(456, entry["tty"], "codex --remote unix:///unregistered")]
                with self.assertRaises(ValueError): NATIVE.verify_source_process(entry)
            self.assertFalse(f.mutations)

    def test_finish_rechecks_source_process_after_native_resolution(self):
        import uuid
        for command in ("source_pid", "source_resume"):
            with self.subTest(command=command), ReceiptFixture(str(uuid.uuid4())) as f:
                with CLOSURE.resume_guard(f.session, method="turn/start", home=f.home): pass
                resolve = NATIVE.resolve_pending
                def raced(session, proof):
                    resolve(session, proof)
                    f.process_rows = ([(f.entry["pid"], "other-tty", "unrelated-process")] if command == "source_pid" else
                                      [(456, "other-tty", "codex resume " + f.session)])
                with patch.object(NATIVE, "resolve_pending", side_effect=raced), self.assertRaises(ValueError):
                    f.release.record_closure(f.log, f.entry, "supervisor")
                self.assertFalse(f.mutations)

    def test_new_launch_cannot_borrow_previous_generation_cutoff(self):
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
            saved = CLOSURE.log_rows()
            saved[0]["tty_exited_at"] = cutoff.isoformat()
            CLOSURE.replace_rows(saved)
            entry = {**f.entry, "launch_generation": "new-generation",
                     "launch_started_at": (cutoff + timedelta(minutes=1)).isoformat(),
                     "tty_exited_at": cutoff.isoformat()}
            with CLOSURE.resume_guard(f.session, method="thread/resume", home=f.home):
                metadata = {key: entry[key] for key in ("codex_home", "launch_pid", "launch_generation", "launch_started_at")}
                CLOSURE.resumed_launch(f.session, metadata, str(f.transcript), entry["tty"])
            f.native_state = "notLoaded"
            f.process_rows = [(456, entry["tty"], "unrelated-worker")]
            with patch.object(CLOSURE, "process_started_at", return_value=cutoff + timedelta(seconds=1)), self.assertRaises(ValueError):
                f.release.record_closure(f.log, entry, "supervisor")
            self.assertTrue(CLOSURE.action_pending(f.session))
            self.assertFalse(f.mutations)

    def test_later_safe_turn_cannot_publish_changed_caller_identity_with_old_cutoff(self):
        import uuid
        for field in ("pid", "tty", "harness", "transcript", "codex_home", "launch_pid", "launch_generation", "launch_started_at", "supervisor_owned",
                      "tmux_session_id", "tmux_socket", "window_id", "tab_id", "iterm_session_id"):
            with self.subTest(field=field), ReceiptFixture(str(uuid.uuid4())) as f:
                with CLOSURE.resume_guard(f.session, method="turn/start", home=f.home): pass
                later = datetime.now(timezone.utc).isoformat()
                with f.transcript.open("a") as stream:
                    for payload in ({"type": "task_started", "turn_id": "later-safe-turn"},
                                    {"type": "agent_message", "message": "Safe to exit: yes"},
                                    {"type": "task_complete", "turn_id": "later-safe-turn"}):
                        stream.write(json.dumps({"type": "event_msg", "timestamp": later, "payload": payload}) + "\n")
                entry = dict(f.entry)
                if field in {"pid", "launch_pid"}: entry[field] = 456
                elif field == "harness": entry[field] = "claude"
                elif field == "supervisor_owned": entry[field] = False
                elif field == "launch_started_at": entry[field] = later
                elif field == "codex_home": entry[field] = str(f.root / "other-account")
                elif field == "transcript":
                    alias = f.transcript.with_name("other-copy.jsonl")
                    alias.write_bytes(f.transcript.read_bytes())
                    entry[field] = str(alias)
                else: entry[field] = "other-generation-or-tty"
                if field == "pid": f.process_rows = [(entry[field], "other-tty", "new-unchecked-worker")]
                before = CLOSURE.source_records({f.session})[f.session]
                with self.assertRaises(ValueError): f.release.record_closure(f.log, entry, "supervisor")
                self.assertEqual(CLOSURE.source_records({f.session})[f.session], before)
                self.assertFalse(f.mutations)

    def test_recovered_closure_keeps_runtime_flags_transient(self):
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f:
            original = CLOSURE.source_records({f.session})[f.session]
            with CLOSURE.resume_guard(f.session, method="turn/start", home=f.home): pass
            runtime = {"_transcript_roots": [str(f.transcript.parent)], "_backfill": True}
            closed = f.release.record_closure(f.log, {**f.entry, **runtime}, "supervisor")
            self.assertEqual(closed["ended_at"], original["ended_at"])
            self.assertFalse(CLOSURE.action_pending(f.session))
            persisted = CLOSURE.source_records({f.session})[f.session]
            self.assertTrue(set(runtime).isdisjoint(persisted))
            f.process_rows = [(456, f.entry["tty"], "old-unrelated-command")]
            with patch.object(CLOSURE, "process_started_at", return_value=datetime.fromisoformat(original["ended_at"]) - timedelta(seconds=1)), self.assertRaises(ValueError):
                f.release.validate_closure(persisted, "supervisor")
            self.assertFalse(f.mutations)

    def test_pending_observation_cannot_borrow_old_pane_for_new_generation_on_reused_tty(self):
        import uuid
        from github_plan_sessions import native_helpers
        native_helpers()
        import tmux_terminal
        with ReceiptFixture(str(uuid.uuid4())) as f:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
            entry = {**f.entry, "tmux_socket": "fixture", "tmux_session_id": "tmux:fixture:" + "a" * 32,
                     "window_id": "$1", "tab_id": "@1"}
            CLOSURE.replace_rows([])
            with patch.object(tmux_terminal.Terminal, "list", return_value=[]):
                f.release.record_closure(f.log, entry, "supervisor")
            metadata = {key: entry[key] for key in ("codex_home", "launch_pid", "launch_generation", "launch_started_at")}
            metadata.update(launch_pid=789, launch_generation="new-generation",
                            launch_started_at=datetime.now(timezone.utc).isoformat())
            with CLOSURE.resume_guard(f.session, method="thread/resume", home=f.home):
                CLOSURE.resumed_launch(f.session, metadata, str(f.transcript), entry["tty"])
            pane = {"session_id": entry["tmux_session_id"], "pane_id": "%1", "pid": entry["launch_pid"],
                    "tmux_socket": "fixture", "window_id": "$1", "tab_id": "@1", "tty": entry["tty"],
                    "dead": True, "exited_at": cutoff.isoformat()}
            f.native_state = "notLoaded"
            f.process_rows = [(456, entry["tty"], "unrelated-worker")]
            with patch.object(tmux_terminal.Terminal, "list", return_value=[pane]), \
                    patch.object(CLOSURE, "process_started_at", return_value=cutoff + timedelta(seconds=1)):
                _, states, _ = f.observe()
            self.assertNotEqual(states.get(f.session, {}).get("state"), "ended")
            self.assertTrue(CLOSURE.action_pending(f.session))
            self.assertFalse(f.mutations)

    def test_pending_canonical_events_committed_by_either_fence_are_rechecked(self):
        import uuid
        for commit_at in (1, 2):
            with self.subTest(commit_at=commit_at), ReceiptFixture(str(uuid.uuid4())) as f:
                with CLOSURE.resume_guard(f.session, method="turn/start", home=f.home): pass
                fences = []
                def thread(request):
                    if request["params"].get("includeTurns"):
                        fences.append(True)
                        if len(fences) == commit_at:
                            with f.transcript.open("a") as stream:
                                for kind in ("task_started", "task_complete"):
                                    stream.write(json.dumps({"type": "event_msg", "timestamp": "2026-10-02T00:00:00Z",
                                        "payload": {"type": kind, "turn_id": "unsafe-later-turn"}}) + "\n")
                    return {"thread": {"id": f.session, "path": str(f.transcript), "status": {"type": "idle"},
                                       "historyMode": "paginated"}}
                f.rpc_results["thread/read"] = thread
                f.rpc_results["thread/turns/list"] = {"data": [{"id": "source-turn", "status": "completed"}], "nextCursor": None}
                with CLOSURE.ownership(f.session), self.assertRaises(ValueError):
                    asyncio.run(NATIVE.prove_end(f.entry))
                self.assertTrue(CLOSURE.action_pending(f.session))
                self.assertFalse(f.mutations)

    def test_loaded_persistence_response_can_cover_a_long_source_history(self):
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f:
            with CLOSURE.resume_guard(f.session, method="turn/start", home=f.home): pass
            def thread(request):
                value = {"id": f.session, "path": str(f.transcript), "status": {"type": "idle"}, "historyMode": "paginated"}
                if request["params"].get("includeTurns"):
                    value["turns"] = [{"id": "source-turn", "status": "completed", "items": [
                        {"id": "long-message", "type": "agentMessage", "text": "source history " * 100000}]}]
                return {"thread": value}
            f.rpc_results["thread/read"] = thread
            with CLOSURE.ownership(f.session):
                proof = asyncio.run(NATIVE.prove_end(f.entry))
                NATIVE.resolve_pending(f.session, proof)
            self.assertFalse(CLOSURE.action_pending(f.session))
            self.assertFalse(f.mutations)

    def test_ordinary_idle_receipt_uses_complete_current_work_and_canonical_realtime(self):
        import uuid
        for work in ("realtime", "closed_realtime", "goal", "completed_goal", "background", "unknown_goal", "later_goal"):
            with self.subTest(work=work), ReceiptFixture(str(uuid.uuid4())) as f:
                if "realtime" in work:
                    rows = [{"type": "realtime_item", "timestamp": "2026-10-02T00:00:00Z", "payload": {
                        "id": "start", "realtime_session_id": "rtc", "type": "realtime_session_started"}}]
                    if work == "closed_realtime":
                        rows.append({"type": "realtime_item", "timestamp": "2026-10-02T00:00:01Z", "payload": {
                            "id": "closed", "realtime_session_id": "rtc", "type": "realtime_session_closed", "outcome": "ended"}})
                    with f.transcript.open("a") as stream:
                        stream.write("".join(json.dumps(row) + "\n" for row in rows))
                    CLOSURE.replace_rows([])
                    f.release.record_closure(f.log, f.entry, "supervisor")
                if work in {"goal", "completed_goal", "unknown_goal"}:
                    f.rpc_results["thread/goal/get"] = {"goal": {"threadId": f.session,
                        "status": {"goal": "active", "completed_goal": "complete", "unknown_goal": "unknown"}[work]}}
                if work == "background":
                    f.rpc_results["thread/backgroundTerminals/list"] = {"data": [{"itemId": "background", "processId": "worker"}], "nextCursor": None}
                if work == "later_goal":
                    calls = []
                    def goal(_request):
                        calls.append(True)
                        return {"goal": {"threadId": f.session, "status": "active"} if len(calls) > 1 else None}
                    f.rpc_results["thread/goal/get"] = goal
                _peers, states, _coverage = f.observe()
                self.assertEqual(states.get(f.session, {}).get("state") == "ended", work in {"closed_realtime", "completed_goal"})
                self.assertFalse(CLOSURE.action_pending(f.session))
                self.assertFalse(f.mutations)

    def test_ordinary_idle_receipt_accepts_the_same_canonical_native_path(self):
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f:
            alias = f.home / "sessions" / "source-alias.jsonl"
            alias.symlink_to(f.transcript)
            f.rpc_results["thread/read"] = {"thread": {"id": f.session, "path": str(alias),
                "cwd": "/retained/source", "status": {"type": "idle"}, "historyMode": "paginated"}}
            _peers, states, _coverage = f.observe()
            self.assertEqual(states.get(f.session, {}).get("state"), "ended")
            self.assertFalse(f.mutations)

    def test_canonical_realtime_lifecycle_overrides_lagging_loaded_index(self):
        import uuid
        for native_state, outcome in (("idle", None), ("idle", "ended"), ("idle", "failed"), ("notLoaded", None)):
            with self.subTest(native_state=native_state, outcome=outcome), ReceiptFixture(str(uuid.uuid4())) as f:
                with CLOSURE.resume_guard(f.session, method="turn/start", home=f.home): pass
                records = [{"timestamp": "2026-10-02T00:00:00Z", "type": "realtime_item", "payload": {
                    "id": "rtc-start", "realtime_session_id": "rtc", "type": "realtime_session_started"}},
                    {"timestamp": "2026-10-02T00:00:01Z", "type": "event_msg", "payload": {
                        "type": "task_started", "turn_id": "later-source-turn"}},
                    {"timestamp": "2026-10-02T00:00:02Z", "type": "event_msg", "payload": {
                        "type": "agent_message", "message": "Safe to exit: yes"}},
                    {"timestamp": "2026-10-02T00:00:03Z", "type": "event_msg", "payload": {
                        "type": "task_complete", "turn_id": "later-source-turn"}}]
                if outcome:
                    records.append({"timestamp": "2026-10-02T00:00:04Z", "type": "realtime_item", "payload": {
                        "id": "rtc-close", "realtime_session_id": "rtc", "type": "realtime_session_closed", "outcome": outcome}})
                with f.transcript.open("a") as stream:
                    stream.write("".join(json.dumps(record) + "\n" for record in records))
                f.native_state = native_state
                f.rpc_results["thread/turns/list"] = {"data": [{"id": "source-turn", "status": "completed"}], "nextCursor": None}
                with CLOSURE.ownership(f.session):
                    if native_state == "idle" and outcome is None:
                        with self.assertRaises(ValueError):
                            asyncio.run(NATIVE.prove_end(f.entry))
                        self.assertTrue(CLOSURE.action_pending(f.session))
                    else:
                        proof = asyncio.run(NATIVE.prove_end(f.entry))
                        NATIVE.resolve_pending(f.session, proof)
                        self.assertFalse(CLOSURE.action_pending(f.session))
                self.assertFalse(f.mutations)

    def test_loaded_unknown_canonical_realtime_cannot_prove_quiet(self):
        import uuid
        for payload in ({"id": "future", "realtime_session_id": "rtc", "type": "unknown_realtime"},
                        {"id": "bad-close", "realtime_session_id": "rtc", "type": "realtime_session_closed", "outcome": "unknown"},
                        {"id": "bad-start", "type": "realtime_session_started"}):
            with self.subTest(payload=payload), ReceiptFixture(str(uuid.uuid4())) as f:
                with CLOSURE.resume_guard(f.session, method="turn/start", home=f.home): pass
                with f.transcript.open("a") as stream:
                    stream.write(json.dumps({"timestamp": "2026-10-02T00:00:00Z", "type": "realtime_item", "payload": payload}) + "\n")
                with CLOSURE.ownership(f.session), self.assertRaises(ValueError):
                    asyncio.run(NATIVE.prove_end(f.entry))
                self.assertTrue(CLOSURE.action_pending(f.session))

    def test_live_pending_source_never_scans_transcripts_under_its_action_lease(self):
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f:
            with CLOSURE.resume_guard(f.session, method="turn/start", home=f.home): pass
            f.process_rows = [(f.entry["pid"], f.entry["tty"], "codex")]
            with patch.object(CLOSURE, "transcripts", side_effect=AssertionError("live source must not be scanned")) as scan:
                _, states, _ = f.observe()
            scan.assert_not_called()
            self.assertNotEqual(states.get(f.session, {}).get("state"), "ended")
            self.assertTrue(CLOSURE.action_pending(f.session))
            with CLOSURE.ownership(f.session) as available:
                self.assertTrue(available)

    def test_action_racing_unleased_process_check_does_not_retain_new_observation_lease(self):
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f, ThreadPoolExecutor(max_workers=1) as pool:
            actual = f.release._verify_process_exit
            raced = False
            def process(entry, *args):
                nonlocal raced
                result = actual(entry, *args)
                if not raced:
                    raced = True
                    with CLOSURE.resume_guard(f.session, method="turn/start", home=f.home): pass
                return result
            def available():
                try:
                    with CLOSURE.ownership(f.session): return True
                except CLOSURE.LeaseBusy: return False
            with patch.object(f.release, "_verify_process_exit", side_effect=process):
                with CLOSURE.observation({f.session}) as acquired:
                    self.assertNotIn(f.session, acquired)
                    self.assertTrue(pool.submit(available).result(2))
            self.assertTrue(CLOSURE.action_pending(f.session))

    def test_first_pending_finish_uses_native_cutoff_and_never_caller_hints(self):
        import uuid
        from github_plan_sessions import native_helpers
        native_helpers()
        import tmux_terminal
        for obstruction in (None, "missing_pane", "source_pid", "source_resume"):
            with self.subTest(obstruction=obstruction), ReceiptFixture(str(uuid.uuid4())) as f:
                CLOSURE.replace_rows([])
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
                entry = {**f.entry, "tmux_socket": "fixture", "tmux_session_id": "tmux:fixture:" + "a" * 32,
                         "window_id": "$1", "tab_id": "@1"}
                if obstruction == "missing_pane":
                    entry.update(tty_exited_at=cutoff.isoformat(), ended_at=cutoff.isoformat())
                pane = {"session_id": entry["tmux_session_id"], "pane_id": "%1", "pid": 789,
                        "tmux_socket": "fixture", "window_id": "$1", "tab_id": "@1", "tty": entry["tty"],
                        "dead": True, "exited_at": cutoff.isoformat()}
                with CLOSURE.resume_guard(f.session, method="turn/start", home=f.home): pass
                f.native_state = "notLoaded"
                f.process_rows = [(456, entry["tty"], "new-unrelated-worker")]
                if obstruction == "source_pid": f.process_rows = [(entry["pid"], "other", "codex")]
                if obstruction == "source_resume": f.process_rows = [(456, "other", "codex resume " + f.session)]
                with patch.object(tmux_terminal.Terminal, "list", return_value=[] if obstruction == "missing_pane" else [pane]), \
                        patch.object(CLOSURE, "process_started_at", return_value=cutoff + timedelta(seconds=1)):
                    if obstruction:
                        with self.assertRaises(ValueError): f.release.record_closure(f.log, entry, "supervisor")
                        self.assertTrue(CLOSURE.action_pending(f.session))
                        self.assertEqual(CLOSURE.log_rows(), [])
                    else:
                        closed = f.release.record_closure(f.log, entry, "supervisor")
                        self.assertEqual(closed["tty_exited_at"], cutoff.isoformat())
                        self.assertFalse(CLOSURE.action_pending(f.session))
                if not obstruction:
                    with patch.object(tmux_terminal.Terminal, "list", side_effect=AssertionError("pane already removed")), \
                            patch.object(CLOSURE, "process_started_at", return_value=cutoff + timedelta(seconds=1)):
                        retried = f.release.record_closure(f.log, entry, "supervisor")
                    self.assertEqual(retried["ended_at"], closed["ended_at"])

    def test_transport_failure_is_safe_cli_error_and_cancellation_still_propagates(self):
        import aiohttp
        import sys
        import uuid
        from test_codex_session import SESSION
        for error in (aiohttp.ClientConnectionError("synthetic transport failure"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__), ReceiptFixture(str(uuid.uuid4())) as f:
                output = io.StringIO()
                with patch.object(aiohttp.ClientSession, "ws_connect", side_effect=error) as connect, \
                        patch.object(sys, "argv", ["codex-session.py", "queue", "--session-id", f.session,
                            "--codex-home", str(f.home), "--message", "synthetic private input"]), redirect_stdout(output):
                    if isinstance(error, asyncio.CancelledError):
                        with self.assertRaises(asyncio.CancelledError): SESSION.main()
                    else:
                        self.assertEqual(SESSION.main(), 1)
                        self.assertFalse(json.loads(output.getvalue())["ok"])
                    self.assertEqual(connect.call_count, 1)
                    self.assertTrue(CLOSURE.action_pending(f.session))
                self.assertNotIn("synthetic private input", output.getvalue())
                self.assertFalse(f.mutations)

    def test_nontext_native_frames_are_private_uncertain_cli_errors(self):
        import aiohttp
        import sys
        import uuid
        from test_codex_session import SESSION
        for kind in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.BINARY):
            with self.subTest(kind=kind), ReceiptFixture(str(uuid.uuid4())) as f:
                output = io.StringIO()
                frame = aiohttp.WSMessage(kind, b"synthetic private wire data", None)
                with patch.object(aiohttp.ClientWebSocketResponse, "receive", new=AsyncMock(return_value=frame)), \
                        patch.object(sys, "argv", ["codex-session.py", "queue", "--session-id", f.session,
                            "--codex-home", str(f.home), "--message", "synthetic input"]), redirect_stdout(output):
                    self.assertEqual(SESSION.main(), 1)
                self.assertFalse(json.loads(output.getvalue())["ok"])
                self.assertNotIn("synthetic private wire data", output.getvalue())
                self.assertTrue(CLOSURE.action_pending(f.session))
                self.assertFalse(f.mutations)

    def test_cancelled_read_worker_cannot_clear_a_later_owner_action(self):
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f:
            with CLOSURE.resume_guard(f.session): pass
            entered, release, finished = (threading.Event() for _ in range(3))
            original = NATIVE.source_snapshot
            def delayed(entry):
                entered.set()
                if not release.wait(3): raise TimeoutError("synthetic read was not released")
                try: return original(entry)
                finally: finished.set()
            async def run():
                with CLOSURE.ownership(f.session), patch.object(NATIVE, "source_snapshot", side_effect=delayed):
                    proof = asyncio.create_task(NATIVE.prove_end(f.entry))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    proof.cancel()
                    with self.assertRaises(asyncio.CancelledError): await proof
                with CLOSURE.ownership(f.session):
                    CLOSURE.set_action_pending(f.session, True, method="thread/realtime/start", home=f.home)
                    current = CLOSURE.lease_path(f.session).read_bytes()
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 2))
                self.assertEqual(CLOSURE.lease_path(f.session).read_bytes(), current)
            try: asyncio.run(run())
            finally: release.set()

    def test_lease_home_union_refuses_before_corrupting_its_own_read_bound(self):
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f:
            with CLOSURE.ownership(f.session):
                refused = False
                for number in range(100):
                    before = CLOSURE.lease_path(f.session).read_bytes()
                    try:
                        CLOSURE.set_action_pending(f.session, True, home=f.root / ("account" + str(number) + "x" * 200))
                    except ValueError:
                        refused = True
                        self.assertEqual(CLOSURE.lease_path(f.session).read_bytes(), before)
                        self.assertTrue(CLOSURE.action_pending(f.session))
                        break
                self.assertTrue(refused)

    def test_other_source_home_queue_vetoes_resolution_with_or_without_legacy_scope(self):
        import uuid
        for scoped in (False, True):
            with self.subTest(scoped=scoped), ReceiptFixture(str(uuid.uuid4())) as f:
                other = f.root / "other-account"
                f.add_home(other)
                f.native_state = "notLoaded"
                with CLOSURE.resume_guard(f.session, method="turn/start" if scoped else None,
                                          home=f.home if scoped else None):
                    pass
                f.home_queues[str(other)] = [{"id": "stale-other-home", "clientUserMessageId": "client",
                                             "input": [{"type": "text", "text": "stale input"}]}]
                with self.assertRaises(ValueError):
                    f.release.record_closure(f.log, f.entry, "supervisor")
                self.assertTrue(CLOSURE.action_pending(f.session))
                f.home_queues[str(other)] = []
                _, states, _ = f.observe()
                self.assertEqual(states.get(f.session, {}).get("state"), "ended")
                self.assertFalse(CLOSURE.action_pending(f.session))

    def test_owned_first_resume_metadata_is_not_promoted_to_observer_ownership(self):
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f:
            CLOSURE.replace_rows([])
            metadata = {key: f.entry[key] for key in ("codex_home", "launch_pid", "launch_generation", "launch_started_at")}
            with CLOSURE.resume_guard(f.session, method="thread/resume", home=f.home):
                CLOSURE.resumed_launch(f.session, metadata, str(f.transcript), f.entry["tty"])
            f.native_state = "notLoaded"
            _, states, _ = f.observe()
            self.assertNotEqual(states.get(f.session, {}).get("state"), "ended")
            self.assertTrue(CLOSURE.action_pending(f.session))
            closed = f.release.record_closure(f.log, f.entry, "supervisor")
            self.assertTrue(closed["process_exit_verified"])
            self.assertFalse(CLOSURE.action_pending(f.session))

    def test_moved_generation_preserves_old_copy_and_reads_its_deconfigured_home(self):
        import uuid
        from github_plan_sessions import native_helpers
        native_helpers()
        import codex_session_proxy
        for legacy_path in (False, True):
            with self.subTest(legacy_path=legacy_path), ReceiptFixture(str(uuid.uuid4())) as f:
                other = f.root / "other-account"
                moved_path = f.add_home(other)
                rows = CLOSURE.log_rows()
                rows[0]["tmux_session_id"] = "%original"
                CLOSURE.replace_rows(rows)
                moved = {**f.entry, "codex_home": str(other), "transcript": str(moved_path),
                         "launch_generation": "new-generation", "tmux_session_id": "%recovery"}
                metadata = {key: moved[key] for key in ("codex_home", "launch_pid", "launch_generation", "launch_started_at")}
                with CLOSURE.resume_guard(f.session, method="thread/resume", home=other):
                    CLOSURE.resumed_launch(f.session, metadata, str(moved_path), moved["tty"])
                if legacy_path:
                    rows = CLOSURE.log_rows()
                    rows[0]["resumed_generation"].pop("transcript")
                    CLOSURE.replace_rows(rows)
                f.homes.remove(other)
                f.native_state = "notLoaded"
                # Historical proof reads do not grant a configured resume home.
                with self.assertRaises(ValueError): codex_session_proxy.source_endpoint(f.session, other)
                seen = []
                def cutoff(entry):
                    seen.append(entry)
                    return None
                import github_plan_sessions
                with patch.object(github_plan_sessions.process_liveness(), "native_exit_cutoff", side_effect=cutoff):
                    closed = f.release.record_closure(f.log, moved, "supervisor")
                self.assertTrue(closed["process_exit_verified"])
                self.assertFalse(CLOSURE.action_pending(f.session))
                self.assertTrue(seen)
                self.assertTrue(all(entry.get("tmux_session_id") == "%recovery" for entry in seen))

    def test_legacy_scope_is_sticky_when_a_new_scoped_action_adds_another_home(self):
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f:
            other = f.root / "other-account"
            copied = f.add_home(other)
            with CLOSURE.ownership(f.session):
                CLOSURE.set_action_pending(f.session, True)
                floor = CLOSURE.action_time(f.session)
                later = datetime.now(timezone.utc).isoformat()
                with f.transcript.open("a") as stream:
                    for payload in ({"type": "task_started", "turn_id": "fresh-source-turn"},
                                    {"type": "agent_message", "message": "Safe to exit: yes"},
                                    {"type": "task_complete", "turn_id": "fresh-source-turn"}):
                        stream.write(json.dumps({"type": "event_msg", "timestamp": later, "payload": payload}) + "\n")
                CLOSURE.set_action_pending(f.session, True, method="turn/start", home=f.home)
                self.assertEqual(CLOSURE.action_time(f.session), floor)
                # The earlier unscoped action still needs a fresh safe turn in
                # the other authenticated home, despite this new scoped action.
                with self.assertRaises(ValueError): asyncio.run(NATIVE.prove_end(f.entry))
                self.assertTrue(CLOSURE.action_pending(f.session))
                copied.write_bytes(f.transcript.read_bytes())
                proof = asyncio.run(NATIVE.prove_end(f.entry))
                NATIVE.resolve_pending(f.session, proof)
                self.assertFalse(CLOSURE.action_pending(f.session))
            self.assertFalse(f.mutations)

    def test_large_escaped_queue_input_uses_request_sized_ack_and_returns_no_input(self):
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f:
            sent = []
            def queued(request):
                sent.append(request["params"])
                self.assertTrue(CLOSURE.action_pending(f.session))
                return {"queuedSubmission": {"id": "queued-one", **{key: request["params"][key]
                    for key in ("clientUserMessageId", "input")}}}
            f.rpc_results["thread/queue/add"] = queued
            text = "\x01" * 180000
            result = asyncio.run(NATIVE.queue_input(f.home, f.session, text))
            self.assertEqual(sent[0]["input"][0]["text"], text)
            self.assertEqual(result["queuedSubmissionId"], "queued-one")
            self.assertNotIn("input", result)
            self.assertNotIn(json.dumps(text), json.dumps(result))
            self.assertEqual(len(sent), 1)

    def test_malformed_queue_ack_is_uncertain_without_replay(self):
        import uuid
        for identity, text in (("", "synthetic input"), ("queued", "different input")):
            with self.subTest(identity=identity), ReceiptFixture(str(uuid.uuid4())) as f:
                def queued(request):
                    return {"queuedSubmission": {"id": identity, "clientUserMessageId": request["params"]["clientUserMessageId"],
                        "input": [{"type": "text", "text": text, "text_elements": []}]}}
                f.rpc_results["thread/queue/add"] = queued
                with self.assertRaises(ValueError): asyncio.run(NATIVE.queue_input(f.home, f.session, "synthetic input"))
                self.assertEqual(f.rpc_requests.count("thread/queue/add"), 1)
                self.assertTrue(CLOSURE.action_pending(f.session))


if __name__ == "__main__":
    unittest.main()
