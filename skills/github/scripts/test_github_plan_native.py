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
from contextlib import asynccontextmanager, contextmanager, redirect_stdout
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

    async def test_native_timeline_turn_boundaries_use_wire_identity(self):
        self.responses["thread/timeline/list"]["data"] = [
            {"type": "turnStarted", "position": 0, "turnId": "actual-turn", "startedAt": 1},
            {"type": "turnCompleted", "position": 1, "turnId": "actual-turn", "status": "completed"},
        ]
        facts = await NATIVE.read_thread_facts(self.request, self.session)
        NATIVE.require_quiet(facts, self.source, {})
        for boundary in self.responses["thread/timeline/list"]["data"]:
            original = boundary.pop("turnId")
            with self.assertRaises(ValueError):
                await NATIVE.read_thread_facts(self.request, self.session)
            boundary["turnId"] = original

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
        self.responses["thread/timeline/list"] = {"data": [], "nextCursor": "older-page",
                                                 "activeRealtimeSessionAtPageStart": "runtime-before-newest-page"}
        with self.assertRaises(ValueError):
            NATIVE.require_quiet(await NATIVE.read_thread_facts(self.request, self.session), self.source, {})
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
    def test_removed_pane_closure_proves_replacement_tree_and_preserves_source_vetoes(self):
        import uuid
        from github_plan_sessions import native_helpers
        native_helpers()
        import tmux_terminal
        for change in (None, "nonidle_root", "same_second", "orphan", "orphan_root", "missing_parent", "old_root", "source_pid",
                       "resume", "uncertain", "source_pane", "late_orphan", "unowned", "no_nonce", "unidentified_harness"):
            with self.subTest(change=change), ReceiptFixture(str(uuid.uuid4())) as f:
                CLOSURE.replace_rows([])
                with CLOSURE.resume_guard(f.session, method="turn/start", home=f.home): pass
                entry = {**f.entry, "tmux_socket": "fixture", "tmux_session_id": "tmux:fixture:" + "a" * 32,
                         "window_id": "$1", "tab_id": "@1"}
                f.native_state = "notLoaded"
                root_at = datetime.now(timezone.utc) - timedelta(minutes=1)
                f.process_rows = [(455, f.entry["tty"], "login"), (456, f.entry["tty"], "unrelated-worker")]
                if change == "nonidle_root": f.process_rows[0] = (455, f.entry["tty"], "replacement-root")
                if change == "unidentified_harness": f.process_rows[1] = (456, f.entry["tty"], "codex")
                parents = {99: 1, 455: 99, 456: 455}
                if change == "orphan": parents[456] = 1
                if change == "orphan_root": parents[455] = 1
                if change == "missing_parent": parents.pop(456)
                if change == "old_root": root_at = datetime.fromisoformat(entry["launch_started_at"])
                if change == "source_pid": f.process_rows.append((entry["pid"], "other-tty", "codex"))
                if change == "resume": f.process_rows.append((789, "other-tty", "codex resume " + f.session))
                if change == "unowned": entry["supervisor_owned"] = False
                if change == "no_nonce": entry["tmux_session_id"] = "tmux:fixture:123:%1"
                pane = {"session_id": entry["tmux_session_id"], "dead": False}
                def inventory():
                    if change == "late_orphan" and "thread/read" in f.rpc_requests:
                        parents[456] = 1
                    return list(f.process_rows)
                with patch.object(tmux_terminal.Terminal, "list", **(
                        {"side_effect": ValueError("uncertain inventory")} if change == "uncertain" else
                        {"return_value": [pane] if change == "source_pane" else []})), \
                        patch.object(tmux_terminal, "process_parents", side_effect=lambda: dict(parents)), \
                        patch.object(CLOSURE, "process_started_at", side_effect=lambda pid: root_at + timedelta(seconds=pid != 455 and change != "same_second")), \
                        patch.object(f.release, "inventory", side_effect=inventory):
                    if change in (None, "nonidle_root", "same_second"):
                        closed = f.release.record_closure(f.log, entry, "supervisor")
                        self.assertEqual(closed["tty_exited_at"], root_at.isoformat())
                        self.assertFalse(CLOSURE.action_pending(f.session))
                        self.assertEqual(f.release.record_closure(f.log, entry, "supervisor")["ended_at"], closed["ended_at"])
                    else:
                        with self.assertRaises(ValueError): f.release.record_closure(f.log, entry, "supervisor")
                        self.assertTrue(CLOSURE.action_pending(f.session))
                        self.assertEqual(json.loads(f.log.read_text()), [])
                self.assertFalse(f.mutations)

    @contextmanager
    def queue_exit_failure(self, target, sent, failure, boundary):
        """Fail actual resource cleanup only after the fixture accepted a mutation."""
        if boundary == "ownership":
            original = CLOSURE.ownership
            @contextmanager
            def ownership(*args, **kwargs):
                with original(*args, **kwargs) as held:
                    yield held
                if sent:
                    raise failure
            with patch.object(CLOSURE, "ownership", ownership):
                yield
        else:
            original = NATIVE.connection
            @asynccontextmanager
            async def connection(home, **kwargs):
                async with original(home, **kwargs) as request:
                    yield request
                if sent and ((home == target) == (boundary == "target")):
                    raise failure
            with patch.object(NATIVE, "connection", connection):
                yield

    @contextmanager
    def cancel_read_boundaries(self, *, before_metadata=None, after_metadata=None, after_facts=None):
        """Observe real reader boundaries without depending on its nested RPC count."""
        original_read, original_facts = NATIVE.read_thread, NATIVE.read_thread_facts
        facts_depth = 0
        async def facts(request, session):
            nonlocal facts_depth
            facts_depth += 1
            try:
                value = await original_facts(request, session)
            finally:
                facts_depth -= 1
            if after_facts is not None: after_facts(request.codex_home, value)
            return value
        async def metadata(request, session, **kwargs):
            direct = facts_depth == 0
            if direct and before_metadata is not None: before_metadata(request.codex_home)
            value = await original_read(request, session, **kwargs)
            if direct and after_metadata is not None: after_metadata(request.codex_home, value)
            return value
        with patch.object(NATIVE, "read_thread", side_effect=metadata), patch.object(NATIVE, "read_thread_facts", side_effect=facts):
            yield

    def cancel_setup(self, f):
        f.transcript.write_text(f.transcript.read_text().replace("Safe to exit: yes", "Interrupted work"))
        other = f.root / "other-account"
        f.add_home(other)
        f.native_state = "notLoaded"
        selected = {"id": "selected", "clientUserMessageId": "selected-client",
                    "input": [{"type": "text", "text": "private stale input"}], "opaqueFutureField": {"value": 1}}
        retained = {"id": "retained", "clientUserMessageId": "retained-client", "input": []}
        f.home_queues[str(other)] = [selected, retained]
        f.home_queues[str(f.home)] = [{"id": "other-home", "clientUserMessageId": "other-client", "input": []}]
        ledger = f.root / "original-ledger.json"
        ledger.write_text(json.dumps([f.entry]))
        ledger.chmod(0o600)
        with CLOSURE.resume_guard(f.session, method="turn/start", home=f.home): pass
        with CLOSURE.ownership(f.session): CLOSURE.admit_sources({f.session})
        deleted = []
        def remove(request):
            deleted.append(request["params"])
            f.home_queues[str(other)] = [entry for entry in f.home_queues[str(other)]
                                        if entry["id"] != request["params"]["queuedSubmissionId"]]
            return {"deleted": True}
        f.rpc_results["thread/queue/delete"] = remove
        return other, ledger, selected, retained, deleted

    def test_cancel_unloaded_frozen_pending_source_preserves_other_work_and_private_input(self):
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f:
            other, ledger, selected, retained, deleted = self.cancel_setup(f)
            before = {path: path.read_bytes() for path in (f.transcript, f.log, CLOSURE.lease_path(f.session), ledger)}
            preview = asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger))
            self.assertFalse(deleted)
            self.assertEqual(preview["codexHome"], str(other))
            self.assertEqual([entry["queuedSubmissionId"] for entry in preview["otherHomes"][str(f.home)]], ["other-home"])
            self.assertTrue(preview["otherHomes"][str(f.home)][0]["sha256"])
            self.assertNotIn(selected["input"][0]["text"], json.dumps(preview))
            digest = next(entry["sha256"] for entry in preview["entries"] if entry["queuedSubmissionId"] == selected["id"])
            result = asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger, apply=True,
                queued_submission_id=selected["id"], entry_sha256=digest))
            self.assertTrue(result["ok"])
            self.assertEqual(result["codexHome"], str(other))
            self.assertEqual(result["queuedSubmissionId"], selected["id"])
            self.assertEqual(result["remainingQueues"], {str(f.home): 1, str(other): 1})
            self.assertEqual(deleted, [{"threadId": f.session, "queuedSubmissionId": selected["id"]}])
            self.assertEqual(f.home_queues[str(other)], [retained])
            self.assertEqual(f.home_queues[str(f.home)][0]["id"], "other-home")
            self.assertEqual({path: path.read_bytes() for path in before}, before)
            with self.assertRaises(ValueError):
                with CLOSURE.resume_guard(f.session): pass
            self.assertFalse(f.mutations)

    def test_cancel_loaded_target_never_triggers_its_other_queued_input(self):
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f:
            other, ledger, selected, _retained, deleted = self.cancel_setup(f)
            preview = asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger))
            digest = next(entry["sha256"] for entry in preview["entries"] if entry["queuedSubmissionId"] == selected["id"])
            f.home_states[str(other)] = "idle"
            def unsafe_watcher(request):
                deleted.append(request)
                f.home_states[str(other)] = "active"
                return {"deleted": True}
            f.rpc_results["thread/queue/delete"] = unsafe_watcher
            with self.assertRaises(ValueError):
                asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger, apply=True,
                    queued_submission_id=selected["id"], entry_sha256=digest))
            self.assertFalse(deleted)
            self.assertEqual(f.home_states[str(other)], "idle")

    def test_cancel_other_loaded_home_requires_empty_queue_and_quiet_runtime(self):
        import uuid
        for obstruction in (None, "queue", "goal", "realtime", "background", "active", "missing_api"):
            with self.subTest(obstruction=obstruction), ReceiptFixture(str(uuid.uuid4())) as f:
                other, ledger, selected, _retained, deleted = self.cancel_setup(f)
                preview = asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger))
                digest = next(entry["sha256"] for entry in preview["entries"] if entry["queuedSubmissionId"] == selected["id"])
                f.home_states[str(f.home)] = "idle"
                if obstruction != "queue": f.home_queues[str(f.home)] = []
                if obstruction == "goal": f.rpc_results["thread/goal/get"] = {"goal": {"threadId": f.session, "status": "active"}}
                if obstruction == "background": f.rpc_results["thread/backgroundTerminals/list"] = {"data": [{"itemId": "work"}], "nextCursor": None}
                if obstruction == "active": f.home_states[str(f.home)] = "active"
                if obstruction == "missing_api": f.rpc_results["thread/goal/get"] = ValueError("fixture unavailable")
                if obstruction == "realtime":
                    with f.transcript.open("a") as stream:
                        stream.write(json.dumps({"type": "realtime_item", "timestamp": "2026-10-02T00:00:00Z", "payload": {
                            "id": "start", "realtime_session_id": "rtc", "type": "realtime_session_started"}}) + "\n")
                if obstruction:
                    with self.assertRaises(ValueError): asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger, apply=True,
                        queued_submission_id=selected["id"], entry_sha256=digest))
                    self.assertFalse(deleted)
                else:
                    result = asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger, apply=True,
                        queued_submission_id=selected["id"], entry_sha256=digest))
                    self.assertTrue(result["ok"])
                    self.assertEqual(len(deleted), 1)

    def cancel_digest(self, other, f, ledger):
        preview = asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger))
        return next(entry["sha256"] for entry in preview["entries"] if entry["queuedSubmissionId"] == "selected")

    def test_cancel_preview_without_lease_does_not_create_state(self):
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f:
            other, ledger, _selected, _retained, deleted = self.cancel_setup(f)
            CLOSURE.lease_path(f.session).unlink()
            before = {path: path.read_bytes() for path in (ledger, f.log, f.transcript)}
            preview = asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger))
            self.assertEqual(len(preview["entries"]), 2)
            self.assertFalse(CLOSURE.lease_path(f.session).exists())
            self.assertEqual({path: path.read_bytes() for path in before}, before)
            self.assertFalse(deleted)

    def test_cancel_retained_original_home_and_deconfigured_copy_authority(self):
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f:
            other, ledger, selected, _retained, deleted = self.cancel_setup(f)
            f.home_queues[str(f.home)] = f.home_queues[str(other)]
            f.homes = [other]
            preview = asyncio.run(NATIVE.cancel_queue(f.home, f.session, ledger=ledger))
            digest = next(entry["sha256"] for entry in preview["entries"] if entry["queuedSubmissionId"] == selected["id"])
            def remove(request):
                deleted.append(request)
                f.home_queues[str(f.home)] = [entry for entry in f.home_queues[str(f.home)] if entry["id"] != selected["id"]]
                return {"deleted": True}
            f.rpc_results["thread/queue/delete"] = remove
            self.assertTrue(asyncio.run(NATIVE.cancel_queue(f.home, f.session, ledger=ledger, apply=True,
                queued_submission_id=selected["id"], entry_sha256=digest))["ok"])
            self.assertEqual(len(deleted), 1)

    def test_cancel_recovery_ledger_retains_original_home_without_authorizing_other_copies(self):
        import uuid
        for target_kind in ("original", "untouched_copy"):
            with self.subTest(target=target_kind), ReceiptFixture(str(uuid.uuid4())) as f:
                original = f.home
                recovery = f.root / "recovery-account"
                recovered_path = f.add_home(recovery)
                untouched = f.root / "untouched-account"
                untouched_path = f.add_home(untouched)
                owned = {**f.entry, "codex_home": str(recovery), "transcript": str(recovered_path),
                    "pid": 456, "launch_pid": 456, "tty": "recovery-tty", "launch_generation": "recovery-generation",
                    "launch_started_at": "2026-10-02T01:00:00Z"}
                metadata = {key: owned[key] for key in ("codex_home", "launch_pid", "launch_generation", "launch_started_at")}
                with CLOSURE.resume_guard(f.session, method="thread/resume", home=recovery):
                    CLOSURE.resumed_launch(f.session, metadata, str(recovered_path), owned["tty"])
                with CLOSURE.ownership(f.session): CLOSURE.admit_sources({f.session})
                ledger = f.root / "owned-recovery-ledger.json"
                ledger.write_text(json.dumps([owned]))
                ledger.chmod(0o600)
                f.native_state = "notLoaded"
                selected = {"id": "selected", "clientUserMessageId": "selected-client", "input": []}
                retained = {"id": "retained", "clientUserMessageId": "retained-client", "input": []}
                f.home_queues[str(original)] = [selected, retained]
                f.home_queues[str(recovery)] = []
                f.home_queues[str(untouched)] = [selected]
                f.homes = [recovery]
                deleted = []
                def remove(request):
                    deleted.append(request["params"])
                    f.home_queues[str(original)] = [retained]
                    return {"deleted": True}
                f.rpc_results["thread/queue/delete"] = remove
                before = {path: path.read_bytes() for path in (ledger, f.log, f.transcript, recovered_path,
                    untouched_path, CLOSURE.lease_path(f.session))}
                if target_kind == "untouched_copy":
                    with self.assertRaises(ValueError): asyncio.run(NATIVE.cancel_queue(untouched, f.session,
                        ledger=ledger, apply=True, queued_submission_id=selected["id"], entry_sha256="0" * 64))
                    self.assertFalse(deleted)
                    self.assertEqual(f.home_queues[str(original)], [selected, retained])
                else:
                    preview = asyncio.run(NATIVE.cancel_queue(original, f.session, ledger=ledger))
                    digest = next(entry["sha256"] for entry in preview["entries"] if entry["queuedSubmissionId"] == selected["id"])
                    self.assertTrue(asyncio.run(NATIVE.cancel_queue(original, f.session, ledger=ledger, apply=True,
                        queued_submission_id=selected["id"], entry_sha256=digest))["ok"])
                    self.assertEqual(deleted, [{"threadId": f.session, "queuedSubmissionId": selected["id"]}])
                    self.assertEqual(f.home_queues[str(original)], [retained])
                self.assertEqual(f.home_queues[str(untouched)], [selected])
                self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_cancel_digest_guard_and_live_source_veto_every_tty(self):
        import uuid
        for mode in ("opaque_digest", "pid", "native_sid", "new_generation"):
            with self.subTest(mode=mode), ReceiptFixture(str(uuid.uuid4())) as f:
                other, ledger, selected, _retained, deleted = self.cancel_setup(f)
                digest = self.cancel_digest(other, f, ledger)
                if mode == "opaque_digest": selected["opaqueFutureField"]["value"] = 2
                if mode == "pid": f.process_rows = [(f.entry["pid"], "different-tty", "worker")]
                if mode == "native_sid": f.process_rows = [(456, "different-tty", "codex resume " + f.session)]
                if mode == "new_generation":
                    rows = json.loads(f.log.read_text())
                    rows[0]["resumed_at"] = "2026-10-02T00:00:00Z"
                    rows[0]["resumed_generation"] = {**f.entry, "pid": 456, "launch_pid": 456, "launch_generation": "new"}
                    CLOSURE.replace_rows(rows)
                with self.assertRaises(ValueError): asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger,
                    apply=True, queued_submission_id=selected["id"], entry_sha256=digest))
                self.assertFalse(deleted)

    def test_cancel_original_authority_cannot_inherit_inventory_or_backfill_exemptions(self):
        import uuid
        cases = [("valid", "none"), ("missing_identity", "none"), ("source_tty_occupant", "none")]
        cases += [(kind, origin) for kind in ("missing_identity", "source_tty_occupant", "inventory_marker", "backfill_marker")
                  for origin in ("ledger", "saved", "saved_generation")]
        for kind, origin in cases:
            with self.subTest(kind=kind, origin=origin), ReceiptFixture(str(uuid.uuid4())) as f:
                other, ledger, selected, _retained, deleted = self.cancel_setup(f)
                digest = self.cancel_digest(other, f, ledger)
                CLOSURE.replace_rows([])
                entry = dict(f.entry)
                marker = "_inventory" if kind in {"missing_identity", "inventory_marker"} else "_backfill"
                if kind == "valid": entry.update(_inventory=False, _backfill=False)
                if kind == "missing_identity":
                    for key in ("pid", "tty", "launch_pid"): entry.pop(key)
                if kind == "source_tty_occupant": f.process_rows = [(456, entry["tty"], "unrelated-worker")]
                if origin == "ledger": entry[marker] = True
                ledger.write_text(json.dumps([entry]))
                if origin == "saved": CLOSURE.replace_rows([{**entry, marker: True}])
                if origin == "saved_generation": CLOSURE.replace_rows([{**entry,
                    "resumed_generation": {**entry, marker: True}}])
                if kind == "valid":
                    self.assertTrue(asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger, apply=True,
                        queued_submission_id=selected["id"], entry_sha256=digest))["ok"])
                else:
                    with self.assertRaises(ValueError): asyncio.run(NATIVE.cancel_queue(other, f.session,
                        ledger=ledger, apply=True, queued_submission_id=selected["id"], entry_sha256=digest))
                self.assertEqual(len(deleted), int(kind == "valid"))

    def test_cancel_refuses_unowned_wrong_home_native_identity_and_unknown_queue(self):
        import uuid
        for mode in ("unowned", "source_home", "initialize_home", "native_id", "native_path", "queue_api"):
            with self.subTest(mode=mode), ReceiptFixture(str(uuid.uuid4())) as f:
                other, ledger, selected, _retained, deleted = self.cancel_setup(f)
                digest = self.cancel_digest(other, f, ledger)
                entry = dict(f.entry)
                if mode == "unowned": entry["supervisor_owned"] = False
                if mode == "source_home": entry["codex_home"] = str(other)
                ledger.write_text(json.dumps([entry]))
                if mode == "initialize_home": f.rpc_results["initialize"] = {"codexHome": str(f.root)}
                if mode in {"native_id", "native_path"}: f.rpc_results["thread/read"] = {"thread": {
                    "id": "different-source" if mode == "native_id" else f.session,
                    "path": str(f.root / "unbound.jsonl") if mode == "native_path" else str(f.transcript),
                    "status": {"type": "notLoaded"}}}
                if mode == "queue_api": f.rpc_results["thread/queue/list"] = ValueError("fixture queue unavailable")
                with self.assertRaises(ValueError): asyncio.run(NATIVE.cancel_queue(other, f.session,
                    ledger=ledger, apply=True, queued_submission_id=selected["id"], entry_sha256=digest))
                self.assertFalse(deleted)

    def test_cancel_selected_authority_stability_ignores_unrelated_ledger_rows(self):
        import uuid
        for changed in ("unrelated", "selected", "shared"):
            with self.subTest(changed=changed), ReceiptFixture(str(uuid.uuid4())) as f:
                other, ledger, selected, _retained, deleted = self.cancel_setup(f)
                digest = self.cancel_digest(other, f, ledger)
                injected = False
                def after_target_facts(home, _facts):
                    nonlocal injected
                    if home != str(other) or injected: return
                    injected = True
                    if changed == "shared":
                        rows = json.loads(f.log.read_text())
                        rows[0]["launch_generation"] = "changed"
                        CLOSURE.replace_rows(rows)
                    else:
                        rows = json.loads(ledger.read_text())
                        if changed == "selected": rows[0]["retained_note"] = "changed authority"
                        else: rows.append({"session_id": "unrelated", "harness": "claude", "note": "independent launch"})
                        ledger.write_text(json.dumps(rows))
                with self.cancel_read_boundaries(after_facts=after_target_facts):
                    if changed == "unrelated":
                        self.assertTrue(asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger,
                            apply=True, queued_submission_id=selected["id"], entry_sha256=digest))["ok"])
                    else:
                        with self.assertRaises(ValueError): asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger,
                            apply=True, queued_submission_id=selected["id"], entry_sha256=digest))
                self.assertEqual(len(deleted), int(changed == "unrelated"))

    def test_cancel_final_metadata_race_and_uncertain_readback_never_replay(self):
        import uuid
        for mode in ("preload", "postload", "false_ack", "malformed_ack", "disconnect", "other_changed"):
            with self.subTest(mode=mode), ReceiptFixture(str(uuid.uuid4())) as f:
                other, ledger, selected, _retained, deleted = self.cancel_setup(f)
                digest = self.cancel_digest(other, f, ledger)
                def before_metadata(home):
                    if mode == "preload" and home == str(other): f.home_states[str(other)] = "idle"
                remove = f.rpc_results["thread/queue/delete"]
                def mutation(request):
                    result = remove(request)
                    if mode == "postload": f.home_states[str(other)] = "idle"
                    if mode == "other_changed": f.home_queues[str(f.home)][0]["opaque"] = "changed"
                    if mode == "false_ack": return {"deleted": False}
                    if mode == "malformed_ack": return {"deleted": 1}
                    if mode == "disconnect": return ValueError("fixture transport unavailable")
                    return result
                f.rpc_results["thread/queue/delete"] = mutation
                with self.cancel_read_boundaries(before_metadata=before_metadata), self.assertRaises(ValueError):
                    asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger, apply=True,
                        queued_submission_id=selected["id"], entry_sha256=digest))
                self.assertEqual(len(deleted), int(mode != "preload"))
                self.assertEqual(len(f.home_queues[str(other)]), 2 if mode == "preload" else 1)

    def test_cancel_postdelete_source_failures_are_private_uncertainty_without_replay(self):
        import subprocess
        import uuid
        for failure in (subprocess.CalledProcessError(1, ["fixture", "private-argument"]),
                        subprocess.TimeoutExpired(["fixture", "private-argument"], 1),
                        KeyError("private-key"), TypeError("private-source"), OSError("private-source"),
                        ValueError("private-source"), TimeoutError("private-source"), asyncio.CancelledError("private-source")):
            with self.subTest(failure=type(failure).__name__), ReceiptFixture(str(uuid.uuid4())) as f:
                other, ledger, selected, retained, deleted = self.cancel_setup(f)
                digest = self.cancel_digest(other, f, ledger)
                original = CLOSURE.transcript_facts
                def read(*args, **kwargs):
                    if deleted: raise failure
                    return original(*args, **kwargs)
                with patch.object(CLOSURE, "transcript_facts", side_effect=read), self.assertRaises(ValueError) as refused:
                    asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger, apply=True,
                        queued_submission_id=selected["id"], entry_sha256=digest))
                self.assertEqual(len(deleted), 1)
                self.assertEqual(f.home_queues[str(other)], [retained])
                self.assertNotIn("private-", str(refused.exception))
                fresh = asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger))
                self.assertEqual([entry["queuedSubmissionId"] for entry in fresh["entries"]], [retained["id"]])
                self.assertEqual(len(deleted), 1)

    def test_cancel_cli_postsend_failure_preserves_deleted_state_without_resubmission(self):
        import sys
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f:
            from test_codex_session import SESSION
            other, ledger, selected, retained, deleted = self.cancel_setup(f)
            digest = self.cancel_digest(other, f, ledger)
            original = CLOSURE.transcript_facts
            def read(*args, **kwargs):
                if deleted: raise KeyError("private-readback-key")
                return original(*args, **kwargs)
            output = io.StringIO()
            with patch.object(CLOSURE, "transcript_facts", side_effect=read), \
                    patch.object(sys, "argv", ["codex-session.py", "queue-cancel", "--session-id", f.session,
                        "--codex-home", str(other), "--ledger", str(ledger), "--apply",
                        "--queued-submission-id", selected["id"], "--entry-sha256", digest]), redirect_stdout(output):
                self.assertEqual(SESSION.main(), 1)
            self.assertFalse(json.loads(output.getvalue())["ok"])
            self.assertNotIn("private-readback-key", output.getvalue())
            self.assertEqual(len(deleted), 1)
            self.assertEqual(f.home_queues[str(other)], [retained])
            fresh = asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger))
            self.assertEqual([entry["queuedSubmissionId"] for entry in fresh["entries"]], [retained["id"]])
            self.assertEqual(len(deleted), 1)

    def test_cancel_resource_exit_failures_preserve_uncertain_deletion_without_replay(self):
        import uuid
        for boundary in ("target", "peer", "ownership"):
            for error_type in (TimeoutError, asyncio.CancelledError, OSError, KeyboardInterrupt):
                with self.subTest(boundary=boundary, failure=error_type.__name__), ReceiptFixture(str(uuid.uuid4())) as f:
                    other, ledger, selected, retained, deleted = self.cancel_setup(f)
                    digest = self.cancel_digest(other, f, ledger)
                    preserved = {path: path.read_bytes() for path in (ledger, f.log, f.transcript, CLOSURE.lease_path(f.session))}
                    other_queue = copy.deepcopy(f.home_queues[str(f.home)])
                    failure = error_type("private-exit-details")
                    with self.queue_exit_failure(other, deleted, failure, boundary), self.assertRaises(ValueError) as refused:
                        asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger, apply=True,
                            queued_submission_id=selected["id"], entry_sha256=digest))
                    self.assertNotIn("private-", str(refused.exception))
                    self.assertEqual(len(deleted), 1)
                    self.assertEqual(f.home_queues[str(other)], [retained])
                    self.assertEqual(f.home_queues[str(f.home)], other_queue)
                    self.assertEqual({path: path.read_bytes() for path in preserved}, preserved)
                    fresh = asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger))
                    self.assertEqual([entry["queuedSubmissionId"] for entry in fresh["entries"]], [retained["id"]])
                    self.assertEqual(len(deleted), 1)

    def test_cancel_cli_resource_exit_failure_is_private_uncertainty(self):
        import sys
        import uuid
        for error_type in (TimeoutError, asyncio.CancelledError, OSError):
            with self.subTest(failure=error_type.__name__), ReceiptFixture(str(uuid.uuid4())) as f:
                from test_codex_session import SESSION
                other, ledger, selected, retained, deleted = self.cancel_setup(f)
                digest = self.cancel_digest(other, f, ledger)
                output = io.StringIO()
                with self.queue_exit_failure(other, deleted, error_type("private-exit-details"), "target"), \
                        patch.object(sys, "argv", ["codex-session.py", "queue-cancel", "--session-id", f.session,
                            "--codex-home", str(other), "--ledger", str(ledger), "--apply",
                            "--queued-submission-id", selected["id"], "--entry-sha256", digest]), redirect_stdout(output):
                    self.assertEqual(SESSION.main(), 1)
                receipt = json.loads(output.getvalue())
                self.assertFalse(receipt["ok"])
                self.assertNotIn("private-", output.getvalue())
                self.assertEqual(len(deleted), 1)
                self.assertEqual(f.home_queues[str(other)], [retained])
                self.assertEqual([entry["queuedSubmissionId"] for entry in
                    asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger))["entries"]], [retained["id"]])
                self.assertEqual(len(deleted), 1)

    def test_cancel_presend_cancellation_preserves_source_and_queue(self):
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f:
            other, ledger, selected, _retained, deleted = self.cancel_setup(f)
            digest = self.cancel_digest(other, f, ledger)
            before = copy.deepcopy(f.home_queues)
            source = {path: path.read_bytes() for path in (ledger, f.log, f.transcript, CLOSURE.lease_path(f.session))}
            failure = asyncio.CancelledError("fixture cancellation before deletion")
            def cancel(_home, _metadata):
                raise failure
            with self.cancel_read_boundaries(after_metadata=cancel), self.assertRaises(asyncio.CancelledError) as stopped:
                asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger, apply=True,
                    queued_submission_id=selected["id"], entry_sha256=digest))
            self.assertIs(stopped.exception, failure)
            self.assertFalse(deleted)
            self.assertEqual(f.home_queues, before)
            self.assertEqual({path: path.read_bytes() for path in source}, source)

    def test_cancel_final_metadata_cannot_change_source_authority(self):
        import uuid
        for boundary in ("metadata", "inventory"):
            for changed in ("unrelated", "selected", "shared", "lease", "process"):
                with self.subTest(boundary=boundary, changed=changed), ReceiptFixture(str(uuid.uuid4())) as f:
                    other, ledger, selected, _retained, deleted = self.cancel_setup(f)
                    digest = self.cancel_digest(other, f, ledger)
                    final_inventory = False
                    def mutate():
                        if changed in {"selected", "unrelated"}:
                            rows = json.loads(ledger.read_text())
                            if changed == "selected": rows[0]["launch_generation"] = "new"
                            else: rows.append({"session_id": "unrelated", "harness": "claude"})
                            ledger.write_text(json.dumps(rows))
                        if changed == "shared": CLOSURE.replace_rows([{**f.entry, "launch_generation": "new"}])
                        if changed == "lease":
                            path = CLOSURE.lease_path(f.session)
                            action = json.loads(path.read_text())
                            action["action_at"] = "2026-10-03T00:00:00Z"
                            path.write_text(json.dumps(action))
                        if changed == "process": f.process_rows = [(f.entry["pid"], "different-tty", "worker")]
                    def after_metadata(home, _metadata):
                        nonlocal final_inventory
                        if home == str(other):
                            if boundary == "metadata": mutate()
                            else: final_inventory = True
                    def inventory():
                        nonlocal final_inventory
                        if final_inventory:
                            final_inventory = False
                            mutate()
                        return list(f.process_rows)
                    with self.cancel_read_boundaries(after_metadata=after_metadata), patch.object(f.release, "inventory", side_effect=inventory):
                        if changed == "unrelated":
                            self.assertTrue(asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger, apply=True,
                                queued_submission_id=selected["id"], entry_sha256=digest))["ok"])
                        else:
                            with self.assertRaises(ValueError): asyncio.run(NATIVE.cancel_queue(other, f.session,
                                ledger=ledger, apply=True, queued_submission_id=selected["id"], entry_sha256=digest))
                    self.assertEqual(len(deleted), int(changed == "unrelated"))

    def test_cancel_original_termination_record_and_partial_tail_authority(self):
        import os
        import socket
        import uuid
        from types import SimpleNamespace
        import github_plan_termination as termination
        for mode in ("minimal", "historical", "historical_markers", "known_launch_pid", "matching_generation", "stale_generation", "partial_tail", "interior", "complete_corrupt", "wrong_host", "new_generation", "inventory_marker", "backfill_marker", "gone_artifact"):
            with self.subTest(mode=mode), ReceiptFixture(str(uuid.uuid4())) as f:
                other, _ledger, selected, _retained, deleted = self.cancel_setup(f)
                # Preserve the original object; an output attestation is insufficient.
                record = {key: value for key, value in f.entry.items() if key not in {
                    "supervisor_owned", "codex_home", "launch_pid", "launch_generation", "launch_started_at"}}
                record.update(host=socket.gethostname(), cause="fixture host crash", retained_artifacts=[],
                    ended_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                    transcript_roots=[str(home / "sessions") for home in (f.home, other)],
                    tmux_socket="fixture", tmux_session_id="tmux:fixture:" + "a" * 32)
                CLOSURE.replace_rows([f.entry] if mode in {"historical", "historical_markers", "known_launch_pid"} else [])
                if mode == "historical_markers": CLOSURE.replace_rows([{**f.entry, "_inventory": True, "_backfill": True}])
                if mode == "known_launch_pid":
                    CLOSURE.replace_rows([{**f.entry, "launch_pid": 456}])
                    f.process_rows = [(456, "different-tty", "worker")]
                if mode == "new_generation": CLOSURE.replace_rows([{**f.entry, "resumed_at": record["ended_at"],
                    "resumed_generation": {**f.entry, "pid": 456, "launch_pid": 456, "launch_generation": "new"}}])
                if mode in {"matching_generation", "stale_generation"}:
                    generation = {**f.entry, "pid": 456, "launch_pid": 456, "launch_generation": "recovery"}
                    CLOSURE.replace_rows([{**f.entry, "resumed_at": record["ended_at"], "resumed_generation": generation}])
                    if mode == "matching_generation":
                        record.update({key: generation[key] for key in ("pid", "launch_pid", "launch_generation", "launch_started_at")})
                if mode == "wrong_host": record["host"] = "different-source-host"
                if mode == "inventory_marker": record["_inventory"] = True
                if mode == "backfill_marker": record["_backfill"] = True
                if mode == "gone_artifact": record["retained_artifacts"] = [{"branch": "already-merged", "worktree": str(f.root / "removed-worktree")}]
                if mode in {"partial_tail", "interior", "complete_corrupt"}:
                    with f.transcript.open("a") as stream:
                        stream.write('{"type":' + ("\n" if mode != "partial_tail" else ""))
                        if mode == "interior": stream.write(json.dumps({"type": "session_meta", "payload": {"id": f.session}}) + "\n")
                    f.rpc_results["thread/turns/list"] = {"data": [], "nextCursor": None}
                old = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
                for path in (f.transcript, other / "sessions" / f.transcript.name): os.utime(path, (old, old))
                path = f.root / "original-termination.json"
                path.write_text(json.dumps(record))
                path.chmod(0o600)
                before = {value: value.read_bytes() for value in (path, f.transcript, f.log, CLOSURE.lease_path(f.session))}
                with patch.object(termination, "inventory", side_effect=lambda: f.process_rows), \
                        patch.object(termination, "run_terminal", return_value=SimpleNamespace(
                        returncode=0, stdout="a" * 32 + "\t1\t1\n", stderr="")):
                    if mode not in {"minimal", "historical", "historical_markers", "matching_generation", "partial_tail", "gone_artifact"}:
                        with self.assertRaises(ValueError): asyncio.run(NATIVE.cancel_queue(other, f.session, termination_record=path))
                        self.assertFalse(deleted)
                        continue
                    if mode == "gone_artifact":
                        import subprocess
                        with self.assertRaises((ValueError, subprocess.SubprocessError)): termination.verify(record, f.session)
                    preview = asyncio.run(NATIVE.cancel_queue(other, f.session, termination_record=path))
                    digest = next(entry["sha256"] for entry in preview["entries"] if entry["queuedSubmissionId"] == selected["id"])
                    self.assertTrue(asyncio.run(NATIVE.cancel_queue(other, f.session, termination_record=path,
                        apply=True, queued_submission_id=selected["id"], entry_sha256=digest))["ok"])
                self.assertEqual(len(deleted), 1)
                self.assertEqual({value: value.read_bytes() for value in before}, before)

    def test_cancel_termination_covers_archive_roots_without_granting_account_authority(self):
        import os
        import socket
        import uuid
        from types import SimpleNamespace
        import github_plan_termination as termination
        for change in ("existing_activity", "fingerprint", "new_copy"):
            with self.subTest(change=change), ReceiptFixture(str(uuid.uuid4())) as f:
                other, _ledger, selected, _retained, deleted = self.cancel_setup(f)
                CLOSURE.replace_rows([])
                archive = f.root / "retained-transcripts-outside-accounts"
                archive.mkdir()
                archived = archive / ("archive-" + f.session + ".jsonl")
                archived.write_bytes(f.transcript.read_bytes())
                record = {**f.entry, "host": socket.gethostname(), "cause": "fixture crash", "retained_artifacts": [],
                    "ended_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                    "transcript_roots": [str(f.home / "sessions"), str(archive)],
                    "tmux_socket": "fixture", "tmux_session_id": "tmux:fixture:" + "a" * 32}
                old = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
                for file in (f.transcript, other / "sessions" / f.transcript.name, archived): os.utime(file, (old, old))
                authority = f.root / "original-termination.json"
                authority.write_text(json.dumps(record))
                authority.chmod(0o600)
                with patch.object(termination, "run_terminal", return_value=SimpleNamespace(
                        returncode=0, stdout="a" * 32 + "\t1\t1\n", stderr="")):
                    with self.assertRaises(ValueError):
                        asyncio.run(NATIVE.cancel_queue(archive, f.session, termination_record=authority))
                    preview = asyncio.run(NATIVE.cancel_queue(other, f.session, termination_record=authority))
                    digest = next(entry["sha256"] for entry in preview["entries"] if entry["queuedSubmissionId"] == selected["id"])
                    seen = set()
                    def after_facts(home, _facts):
                        if home in seen and home == str(other):
                            if change == "fingerprint": archived.write_bytes(archived.read_bytes() + b" \n")
                            if change == "new_copy":
                                copy = archive / ("later-" + f.session + ".jsonl")
                                copy.write_bytes(archived.read_bytes())
                                os.utime(copy, (old, old))
                            os.utime(archived, (old, old))
                        seen.add(home)
                    if change == "existing_activity":
                        with archived.open("a") as stream: stream.write(json.dumps({"type": "event_msg",
                            "timestamp": datetime.now(timezone.utc).isoformat(), "payload": {"type": "agent_message", "message": "new activity"}}) + "\n")
                    with self.cancel_read_boundaries(after_facts=after_facts), self.assertRaises(ValueError):
                        asyncio.run(NATIVE.cancel_queue(other, f.session, termination_record=authority, apply=True,
                            queued_submission_id=selected["id"], entry_sha256=digest))
                self.assertFalse(deleted)

    def test_termination_transcript_uses_physical_lines_for_valid_unicode_and_crash_tail(self):
        import uuid
        import github_plan_termination as termination
        with ReceiptFixture(str(uuid.uuid4())) as f:
            for separator in ("\u2028", "\u2029", "\u0085"):
                for tail in ("", '{"type":'):
                    with self.subTest(separator=repr(separator), tail=bool(tail)):
                        rows = [{"type": "session_meta", "payload": {"id": f.session}},
                            {"type": "event_msg", "timestamp": "2026-10-01T02:00:00Z", "payload": {
                                "type": "agent_message", "message": "interrupted" + separator + "work"}}]
                        f.transcript.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows) + tail)
                        observed, _mtime, _size, truncated = termination.transcript_snapshot(f.entry, f.transcript)
                        self.assertEqual(observed, rows)
                        self.assertEqual(truncated, bool(tail))
            f.transcript.write_text('{"type":\n' + json.dumps(rows[0]) + "\n")
            with self.assertRaises(ValueError): termination.transcript_snapshot(f.entry, f.transcript)

    def test_cancel_guarded_reused_tty_uses_original_pane_without_clearing_pending(self):
        import os
        import socket
        import uuid
        from types import SimpleNamespace
        import github_plan_termination as termination
        with ReceiptFixture(str(uuid.uuid4())) as f:
            import tmux_terminal
            other, _ledger, selected, _retained, deleted = self.cancel_setup(f)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0)
            record = {**f.entry, "host": socket.gethostname(), "cause": "fixture terminal crash",
                "ended_at": cutoff.isoformat(), "retained_artifacts": [], "tmux_socket": "fixture",
                "tmux_session_id": "tmux:fixture:" + "a" * 32, "window_id": "$1", "tab_id": "@1",
                "transcript_roots": [str(home / "sessions") for home in (f.home, other)]}
            old = (cutoff - timedelta(days=1)).timestamp()
            for transcript in (f.transcript, other / "sessions" / f.transcript.name): os.utime(transcript, (old, old))
            path = f.root / "original-termination.json"
            path.write_text(json.dumps(record))
            path.chmod(0o600)
            values = {"session_id": "$1", "window_id": "@1", "pane_id": "%1", "pane_title": "ended",
                "pane_tty": "/dev/" + f.entry["tty"], "pane_pid": "", "pane_dead": "1", "pane_dead_status": "0",
                "@supervisor_identity": "a" * 32, "pid": "999", "pane_dead_time": str(int(cutoff.timestamp()))}
            native = "\t".join(values.get(field, "") for field in tmux_terminal.PANE_FIELDS) + "\n"
            shared = termination.native_helpers()[1]
            state = CLOSURE.lease_path(f.session).read_bytes()
            with patch.object(termination, "inventory", return_value=[(88, f.entry["tty"], "codex --remote unix:///new-client")]), \
                    patch.object(shared, "process_started_at", return_value=cutoff + timedelta(seconds=1)), \
                    patch.object(tmux_terminal.Terminal, "run", return_value=SimpleNamespace(returncode=0, stdout=native)), \
                    patch.object(tmux_terminal.Terminal, "readonly_control_supported", return_value=True), \
                    patch.object(CLOSURE, "guarded_launch_endpoint", return_value="/new-client"), \
                    patch.object(termination, "run_terminal", return_value=SimpleNamespace(
                        returncode=0, stdout="a" * 32 + "\t1\t1\n", stderr="")):
                with self.assertRaises(ValueError): termination.verify(record, f.session)
                preview = asyncio.run(NATIVE.cancel_queue(other, f.session, termination_record=path))
                digest = next(entry["sha256"] for entry in preview["entries"] if entry["queuedSubmissionId"] == selected["id"])
                self.assertTrue(asyncio.run(NATIVE.cancel_queue(other, f.session, termination_record=path, apply=True,
                    queued_submission_id=selected["id"], entry_sha256=digest))["ok"])
            self.assertEqual(len(deleted), 1)
            self.assertEqual(CLOSURE.lease_path(f.session).read_bytes(), state)

    def test_cancel_private_ledger_relative_path_and_busy_ownership(self):
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f:
            other, ledger, selected, _retained, deleted = self.cancel_setup(f)
            ledger.write_text(json.dumps([{**f.entry, "transcript": str(f.transcript.relative_to(ledger.parent)),
                                          "tty": "/dev/" + f.entry["tty"]}]))
            digest = self.cancel_digest(other, f, ledger)
            acquired, release = threading.Event(), threading.Event()
            def holder():
                with CLOSURE.ownership(f.session):
                    acquired.set()
                    release.wait(5)
            with ThreadPoolExecutor() as pool:
                task = pool.submit(holder)
                self.assertTrue(acquired.wait(2))
                try:
                    with self.assertRaises(CLOSURE.LeaseBusy): asyncio.run(NATIVE.cancel_queue(other, f.session,
                        ledger=ledger, apply=True, queued_submission_id=selected["id"], entry_sha256=digest))
                finally: release.set()
                task.result()
            self.assertFalse(deleted)
            self.assertTrue(asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger, apply=True,
                queued_submission_id=selected["id"], entry_sha256=digest))["ok"])

    def test_cancel_original_authority_allows_read_access_but_refuses_shared_write_access(self):
        import uuid
        for permissions in (0o644, 0o664):
            with self.subTest(permissions=oct(permissions)), ReceiptFixture(str(uuid.uuid4())) as f:
                other, ledger, selected, _retained, deleted = self.cancel_setup(f)
                digest = self.cancel_digest(other, f, ledger)
                ledger.chmod(permissions)
                before = ledger.read_bytes()
                if permissions & 0o022:
                    with self.assertRaises(ValueError):
                        asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger, apply=True,
                            queued_submission_id=selected["id"], entry_sha256=digest))
                    self.assertFalse(deleted)
                else:
                    self.assertTrue(asyncio.run(NATIVE.cancel_queue(other, f.session, ledger=ledger, apply=True,
                        queued_submission_id=selected["id"], entry_sha256=digest))["ok"])
                    self.assertEqual(len(deleted), 1)
                self.assertEqual(ledger.read_bytes(), before)
                self.assertEqual(ledger.stat().st_mode & 0o777, permissions)

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
        with ReceiptFixture(str(uuid.uuid4())) as f, ThreadPoolExecutor(max_workers=1) as pool:
            with CLOSURE.resume_guard(f.session, method="turn/start", home=f.home): pass
            f.process_rows = [(f.entry["pid"], f.entry["tty"], "codex")]
            actual = f.release._verify_process_exit
            def available():
                try:
                    with CLOSURE.ownership(f.session): return True
                except CLOSURE.LeaseBusy: return False
            def process(*args):
                self.assertTrue(pool.submit(available).result(2))
                return actual(*args)
            with patch.object(f.release, "_verify_process_exit", side_effect=process), \
                    patch.object(CLOSURE, "transcripts", side_effect=AssertionError("live source must not be scanned")) as scan:
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

    def test_queue_submission_exit_failure_is_private_uncertainty_without_replay(self):
        import sys
        import uuid
        for boundary in ("target", "ownership"):
            for error_type in (TimeoutError, asyncio.CancelledError, OSError):
                with self.subTest(boundary=boundary, failure=error_type.__name__), ReceiptFixture(str(uuid.uuid4())) as f:
                    from test_codex_session import SESSION
                    sent = []
                    def queued(request):
                        sent.append(request["params"])
                        entry = {"id": "added-once", **{key: request["params"][key]
                            for key in ("clientUserMessageId", "input")}}
                        f.home_queues[str(f.home)] = [entry]
                        return {"queuedSubmission": entry}
                    f.rpc_results["thread/queue/add"] = queued
                    output = io.StringIO()
                    with self.queue_exit_failure(f.home, sent, error_type("private-submission-teardown"), boundary), \
                            patch.object(sys, "argv", ["codex-session.py", "queue", "--session-id", f.session,
                                "--codex-home", str(f.home), "--message", "private-input"]), redirect_stdout(output):
                        self.assertEqual(SESSION.main(), 1)
                    receipt = json.loads(output.getvalue())
                    self.assertFalse(receipt["ok"])
                    self.assertNotIn("private-", output.getvalue())
                    self.assertEqual(len(sent), 1)
                    self.assertTrue(CLOSURE.action_pending(f.session))
                    async def readback():
                        async with NATIVE.connection(f.home) as request:
                            return await NATIVE.read_queue(request, f.session)
                    self.assertEqual(list(asyncio.run(readback())), ["added-once"])
                    self.assertEqual(len(sent), 1)

    def test_queue_submission_presend_cancellation_never_sends_input(self):
        import uuid
        with ReceiptFixture(str(uuid.uuid4())) as f:
            original = NATIVE.connection
            @asynccontextmanager
            async def connection(*args, **kwargs):
                async with original(*args, **kwargs) as request:
                    asyncio.current_task().cancel()
                    await asyncio.sleep(0)
                    yield request
            with patch.object(NATIVE, "connection", connection), self.assertRaises(asyncio.CancelledError):
                asyncio.run(NATIVE.queue_input(f.home, f.session, "private-input"))
            self.assertNotIn("thread/queue/add", f.rpc_requests)

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
