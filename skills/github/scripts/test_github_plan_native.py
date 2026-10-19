# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Synthetic native facts and disposable daemon transport; no native client actions."""
import asyncio
import copy
import threading
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import github_plan_native as NATIVE
import github_plan_closure as CLOSURE
from test_github_plan_sessions import ReceiptFixture


class NativeFactsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = "source"
        self.path = "/fixture/source.jsonl"
        self.calls = []
        self.responses = {
            "thread/read": {"thread": {"id": self.session, "path": self.path, "status": {"type": "idle"}}},
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
            with CLOSURE.ownership(f.session):
                CLOSURE.set_action_pending(f.session, True)
                floor = CLOSURE.action_time(f.session)
                CLOSURE.set_action_pending(f.session, True, method="turn/start", home=other)
                scope = CLOSURE.action_homes(CLOSURE.lease_state(f.session))
                self.assertEqual(scope["known"], [str(other)])
                self.assertTrue(scope["unscoped"])
                self.assertEqual(CLOSURE.action_time(f.session), floor)

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
            self.assertEqual(set(result), {"queuedSubmissionId", "clientUserMessageId"})
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
