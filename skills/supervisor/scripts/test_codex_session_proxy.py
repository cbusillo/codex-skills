# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5", "websockets==17.3"]
# ///
"""Real argv, private sockets and a disposable native client exercise ingress ownership."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))
from test_github_plan_sessions import ReceiptFixture as HistoricalReceiptFixture
import github_plan_closure as closure
import github_plan_sessions
import codex_session_proxy as proxy


OTHER_THREAD = "01970242-7941-7f0c-b461-16aa5723fb51"


class ReceiptFixture(HistoricalReceiptFixture):
    """Native RPC fixtures use native UUIDs; historical receipt tests keep opaque IDs."""
    def __init__(self, session=None, **kwargs):
        super().__init__(str(uuid.uuid4()) if session is None else session, **kwargs)


class IngressTests(unittest.IsolatedAsyncioTestCase):
    async def test_tmux_launch_reattachment_finishes_with_or_without_a_queue_nudge(self) -> None:
        import iterm_tab
        import session_liveness

        for queued in (False, True):
            with self.subTest(queued=queued):
                f = ReceiptFixture()
                f.__enter__()
                self.addCleanup(f.__exit__)
                closure.replace_rows([])
                task, request, response, _ = await self.start_client(f, external=True)
                terminal = "tmux:supervisor:" + uuid.uuid4().hex
                processes = [(int(line.split()[0]), "?", line.split(maxsplit=1)[1])
                             for line in f.commands.splitlines() if line.split()]
                with patch.object(session_liveness, "inventory", return_value=processes):
                    receipt = iterm_tab.source_launch_metadata(
                        {"session_id": terminal, "tty": "/dev/?", "tmux_session_id": terminal},
                        {"provider": "openai", "env": {"CODEX_HOME": str(f.home)}})
                self.assertNotIn("launch_identity_unavailable", receipt)
                entry = f.release.session_record.normalize_ledger_entry(f.log, {**f.entry, **receipt, "session_id": f.session})
                try:
                    self.assertIn("result", await self.send(task, request, response, "thread/resume", f.session))
                    if queued:
                        self.assertIn("result", await self.send(task, request, response, "thread/queue/add", f.session,
                                                               input=[{"type": "text", "text": "finish"}]))
                    with f.transcript.open("a") as stream:
                        for payload in ({"type": "task_started", "turn_id": "final-turn"},
                                        {"type": "agent_message", "message": "Safe to exit: yes"},
                                        {"type": "task_complete", "turn_id": "final-turn"}):
                            stream.write(json.dumps({"type": "event_msg", "timestamp": datetime.now(timezone.utc).isoformat(),
                                                     "payload": payload}) + "\n")
                finally:
                    (f.root / "stop").touch()
                    await asyncio.wait_for(task, 8)
                closed = await asyncio.to_thread(lambda: f.release.record_closure(f.log, entry, "supervisor"))
                self.assertTrue(closed["process_exit_verified"])
                self.assertTrue((await asyncio.to_thread(lambda: f.release.validate_closure(closed, "supervisor")))["safe_verdict"])

    async def test_spawned_label_reaches_new_native_threads_before_their_first_turn(self) -> None:
        with ReceiptFixture() as f, patch.dict(os.environ, {"DISCORD_BLUE_SESSION_KIND": "spawned"}):
            forwarded = []

            def record(native_request: dict) -> dict:
                forwarded.append(native_request["params"])
                return {"thread": {"id": str(uuid.uuid4())}}

            f.rpc_results.update({"thread/start": record, "thread/fork": record})
            task, request, response, _ = await self.start_client(f)
            try:
                for method, target in (("thread/start", None), ("thread/fork", f.session)):
                    result = await self.send(task, request, response, method, target, threadSource="user")
                    self.assertIn("result", result)
                self.assertEqual([params["threadSource"] for params in forwarded], ["spawned", "spawned"])
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    def test_unlabelled_ingress_keeps_native_source_classification(self) -> None:
        with patch.dict(os.environ, {"DISCORD_BLUE_SESSION_KIND": ""}):
            for params in ({}, {"threadSource": "user"}):
                forwarded = proxy.without_memory_generation({"params": params})["params"]
                self.assertEqual(forwarded.get("threadSource"), params.get("threadSource"))

    async def test_alias_actions_refuse_before_forwarding_or_creating_another_lease(self):
        with ReceiptFixture("01a1514c-fa7f-7be3-95ba-4d25df73d07e") as f:
            forwarded = []
            def receive(request):
                forwarded.append(request)
                return {}
            f.rpc_results.update({"thread/batch": receive, "thread/action": receive})
            with closure.ownership(f.session):
                closure.admit_sources({f.session})
            task, request, response, _ = await self.start_client(f)
            try:
                before = f.log.read_bytes()
                leases = set(closure.lease_path(f.session).parent.iterdir())
                for alias in (f.session.upper(), f.session.replace("-", ""), "{" + f.session + "}", "urn:uuid:" + f.session):
                    for method, params in (("thread/resume", {"threadId": alias}),
                                           ("turn/start", {"threadId": alias}),
                                           ("thread/batch", {"threadIds": [alias]}),
                                           ("thread/action", {"conversationId": alias})):
                        with self.subTest(alias=alias, method=method):
                            result = await self.send(task, request, response, method, None, **params)
                            self.assertIn("error", result)
                            self.assertIn(f.session, result["error"]["message"])
                            self.assertFalse(f.mutations)
                            self.assertFalse(forwarded)
                            self.assertEqual(set(closure.lease_path(f.session).parent.iterdir()), leases)
                            self.assertEqual(f.log.read_bytes(), before)
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_clock_request_then_client_shutdown_preserves_only_a_fresh_verdict(self):
        from websockets.asyncio.client import unix_connect
        for action in (None, "thread/queue/add", "thread/resume", "turn/start", "item/commandExecution/requestApproval"):
            with self.subTest(action=action), ReceiptFixture(str(uuid.uuid4())) as f:
                closure.replace_rows([])  # Exercise first finish, without an older receipt.
                task, request, response, _ = await self.start_client(f)
                endpoint = next(r["endpoint"] for r in closure.log_rows() if r.get("record_kind") == "managed_proxy")

                async def server_request(method, result):
                    f.server_request = {"id": "server-request", "method": method, "params": {"threadId": f.session}}
                    before = len(f.approval_results)
                    async with unix_connect(endpoint) as ws:
                        await ws.send(json.dumps({"id": 0, "method": "initialize", "params": {}}))
                        self.assertIn("result", json.loads(await ws.recv()))
                        message = json.loads(await asyncio.wait_for(ws.recv(), 2))
                        self.assertEqual(message["method"], method)
                        await ws.send(json.dumps({"id": message["id"], "result": result}))
                        async with asyncio.timeout(3):
                            while len(f.approval_results) == before:
                                await asyncio.sleep(.01)
                    f.server_request = None

                try:
                    self.assertIn("result", await self.send(task, request, response, "turn/start", f.session))
                    with f.transcript.open("a") as stream:
                        for payload in ({"type": "agent_message", "message": "Safe to exit: yes"}, {"type": "task_complete"}):
                            stream.write(json.dumps({"type": "event_msg", "timestamp": datetime.now(timezone.utc).isoformat(), "payload": payload}) + "\n")
                    if action == "item/commandExecution/requestApproval":
                        await server_request(action, {"decision": "approved"})
                    elif action:
                        self.assertIn("result", await self.send(task, request, response, action, f.session, message="later"))
                        f.native_state = "active"  # Native routed work, not delivery time, vetoes closure.
                    before = closure.action_time(f.session)
                    await server_request("currentTime/read", {})
                    after = closure.action_time(f.session)
                    f.rpc_results["thread/unsubscribe"] = {"status": "unsubscribed"}
                    self.assertIn("result", await self.send(task, request, response, "thread/unsubscribe", f.session))
                finally:
                    (f.root / "stop").touch()
                    self.assertEqual(await asyncio.wait_for(task, 8), 0)
                if action:
                    with self.assertRaises(ValueError):
                        await asyncio.to_thread(f.release.record_closure, f.log, f.entry, "supervisor")
                    self.assertTrue(closure.action_pending(f.session))
                else:
                    closed = await asyncio.to_thread(f.release.record_closure, f.log, f.entry, "supervisor")
                    self.assertTrue(closed["process_exit_verified"])
                    self.assertFalse(closure.action_pending(f.session))
                self.assertEqual(after, before)

    async def test_actual_fast_turn_completed_before_late_ack_can_resolve(self):
        from websockets.asyncio.client import unix_connect
        with ReceiptFixture() as f:
            received, acknowledge = threading.Event(), threading.Event()
            async def delayed(_request):
                received.set()
                await asyncio.to_thread(acknowledge.wait, 5)
                return {}
            f.rpc_results["turn/start"] = delayed
            task, _, _, _ = await self.start_client(f)
            endpoint = next(r["endpoint"] for r in closure.log_rows() if r.get("record_kind") == "managed_proxy")
            try:
                async with unix_connect(endpoint) as ws:
                    await ws.send(json.dumps({"id": 10, "method": "turn/start", "params": {"threadId": f.session}}))
                    self.assertTrue(await asyncio.to_thread(received.wait, 3))
                    floor = closure.action_time(f.session)
                    with f.transcript.open("a") as stream:
                        for payload in ({"type": "task_started", "turn_id": "fast-turn"},
                                {"type": "agent_message", "message": "Safe to exit: yes"},
                                {"type": "task_complete", "turn_id": "fast-turn"}):
                            stream.write(json.dumps({"type": "event_msg", "timestamp": datetime.now(timezone.utc).isoformat(),
                                                     "payload": payload}) + "\n")
                    with self.assertRaises(closure.LeaseBusy):
                        await asyncio.to_thread(self.acquire_source, f.session)
                    acknowledge.set()
                    self.assertIn("result", json.loads(await asyncio.wait_for(ws.recv(), 3)))
                    self.assertEqual(closure.action_time(f.session), floor)
                await asyncio.to_thread(f.release.record_closure, f.log, f.entry, "supervisor")
                self.assertFalse(closure.action_pending(f.session))
            finally:
                acknowledge.set()
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_app_metadata_reads_after_queued_verdict_preserve_only_a_fresh_verdict(self):
        from websockets.asyncio.client import unix_connect
        async def receive(ws):
            return json.loads(await asyncio.wait_for(ws.recv(), 2))

        method, params, result = "app/installed", {"forceRefresh": True}, {"apps": []}
        # All app methods have classification coverage in the history-read test.
        for later in (None, "thread/queue/add", "thread/resume", "turn/start", "execCommandApproval"):
            with self.subTest(later=later), ReceiptFixture(str(uuid.uuid4())) as f:
                closure.replace_rows([])
                task, request, response, _ = await self.start_client(f)
                endpoint = next(r["endpoint"] for r in closure.log_rows() if r.get("record_kind") == "managed_proxy")
                received, acknowledge = threading.Event(), threading.Event()
                async def delayed_read(_request):
                    received.set()
                    if not await asyncio.to_thread(acknowledge.wait, 5):
                        raise TimeoutError("fixture app read acknowledgement was not released")
                    return result
                f.rpc_results[method] = delayed_read
                try:
                    self.assertIn("result", await self.send(task, request, response, "thread/queue/add", f.session, message="safe turn"))
                    self.assertIn("result", await self.send(task, request, response, "turn/start", f.session))
                    async with unix_connect(endpoint) as ws:
                        await ws.send(json.dumps({"id": "app-read", "method": method,
                                                 "params": {"threadId": f.session, **params}}))
                        self.assertTrue(await asyncio.to_thread(received.wait, 3))
                        # Native reads can finish after a fast queued turn.
                        with f.transcript.open("a") as stream:
                            for payload in ({"type": "agent_message", "message": "Safe to exit: yes"}, {"type": "task_complete"}):
                                stream.write(json.dumps({"type": "event_msg", "timestamp": datetime.now(timezone.utc).isoformat(), "payload": payload}) + "\n")
                        before = closure.action_time(f.session)
                        acknowledge.set()
                        self.assertIn("result", await receive(ws))
                        after = closure.action_time(f.session)
                        # A read started after the verdict must also preserve it.
                        await ws.send(json.dumps({"id": "later-read", "method": method,
                                                 "params": {"threadId": f.session, **params}}))
                        self.assertIn("result", await receive(ws))
                    if later == "execCommandApproval":
                        f.approval_target = f.session
                        async with unix_connect(endpoint) as ws:
                            await ws.send(json.dumps({"id": 0, "method": "initialize", "params": {}}))
                            self.assertIn("result", await receive(ws))
                            approval = await receive(ws)
                            await ws.send(json.dumps({"id": approval["id"], "result": {"decision": "approved"}}))
                            async with asyncio.timeout(3):
                                while not f.approval_results:
                                    await asyncio.sleep(.01)
                    elif later:
                        self.assertIn("result", await self.send(task, request, response, later, f.session, message="later"))
                        f.native_state = "active"
                    f.rpc_results["thread/unsubscribe"] = {"status": "unsubscribed"}
                    self.assertIn("result", await self.send(task, request, response, "thread/unsubscribe", f.session))
                finally:
                    acknowledge.set()
                    (f.root / "stop").touch()
                    self.assertEqual(await asyncio.wait_for(task, 8), 0)
                if later:
                    with self.assertRaises(ValueError):
                        await asyncio.to_thread(f.release.record_closure, f.log, f.entry, "supervisor")
                    self.assertTrue(closure.action_pending(f.session))
                else:
                    closed = await asyncio.to_thread(f.release.record_closure, f.log, f.entry, "supervisor")
                    self.assertTrue(closed["process_exit_verified"])
                    self.assertFalse(closure.action_pending(f.session))
                self.assertEqual(after, before)

    async def test_failed_queue_can_recover_notloaded_without_replaying_input(self):
        from test_codex_session import SESSION
        with ReceiptFixture(str(uuid.uuid4())) as f:
            original = next(row for row in closure.log_rows() if row.get("session_id") == f.session)
            f.rpc_results["thread/queue/add"] = ValueError("synthetic rejected queue")
            args = SimpleNamespace(command="queue", session_id=f.session, codex_home=f.home,
                                   message="status?", message_file=None)
            with patch.object(github_plan_sessions, "configured_homes", return_value=[f.home]), self.assertRaises(ValueError):
                await asyncio.to_thread(SESSION.run, args)
            self.assertTrue(closure.action_pending(f.session))
            self.assertFalse(f.mutations)
            f.native_state = "notLoaded"
            closed = await asyncio.to_thread(f.release.record_closure, f.log, f.entry, "supervisor")
            self.assertEqual(closed["ended_at"], original["ended_at"])
            self.assertFalse(closure.action_pending(f.session))
            self.assertEqual(f.rpc_requests.count("thread/queue/add"), 1)

    async def test_connected_worker_accepts_queue_and_capacity_continue_after_turn_reply(self):
        from test_codex_session import SESSION
        import capacity_retry
        with ReceiptFixture(str(uuid.uuid4())) as f:
            task, request, response, _ = await self.start_client(f)
            queue_bin = f.root / "queue-bin"
            queue_bin.mkdir()
            args = SimpleNamespace(command="queue", session_id=f.session, codex_home=f.home,
                                   message="status?", message_file=None)
            try:
                result = await self.send(task, request, response, "turn/start", f.session)
                self.assertIn("result", result)
                with patch.dict(os.environ, {"PATH": str(queue_bin)}), \
                        patch.object(github_plan_sessions, "configured_homes", return_value=[f.home]):
                    self.assertEqual(await asyncio.to_thread(SESSION.run, args), 0)
                    # Capacity target validation is covered by its full module;
                    # exercise the shared typed queue writer while this client stays connected.
                    with patch.object(capacity_retry, "verified_target", return_value=(f.home, proxy.Ingress(f.home).upstream)):
                        await asyncio.to_thread(capacity_retry.queue_continue, f.entry, f.home,
                                                proxy.Ingress(f.home).upstream, {})
                queued = [r["params"] for r in f.mutations if r["method"] == "thread/queue/add"]
                self.assertEqual(len(queued), 2)
                self.assertEqual(queued[0]["input"][0]["text"], args.message)
                self.assertTrue(all(r["threadId"] == f.session and r["input"][0]["text"].strip() for r in queued))
                self.assertFalse(task.done())
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_pending_operations_release_only_their_targets_after_out_of_order_replies(self):
        from websockets.asyncio.client import unix_connect
        with ReceiptFixture() as f:
            f.parallel = True
            gates = {i: threading.Event() for i in (10, 11, 12)}
            seen = {i: threading.Event() for i in gates}
            async def delayed(request):
                seen[request["id"]].set()
                await asyncio.to_thread(gates[request["id"]].wait, 8)
                return {}
            f.rpc_results["turn/interrupt"] = delayed
            task, _, _, _ = await self.start_client(f)
            endpoint = next(r["endpoint"] for r in closure.log_rows() if r.get("record_kind") == "managed_proxy")
            def available(session):
                try:
                    with closure.ownership(session, create=True):
                        return True
                except closure.LeaseBusy:
                    return False
            try:
                async with unix_connect(endpoint) as ws:
                    for number, target in ((10, f.session), (11, OTHER_THREAD), (12, f.session)):
                        await ws.send(json.dumps({"id": number, "method": "turn/interrupt", "params": {"threadId": target}}))
                    for event in seen.values():
                        self.assertTrue(await asyncio.to_thread(event.wait, 3))
                    self.assertFalse(await asyncio.to_thread(available, f.session))
                    self.assertFalse(await asyncio.to_thread(available, OTHER_THREAD))
                    gates[11].set()
                    self.assertEqual(json.loads(await ws.recv())["id"], 11)
                    self.assertTrue(await asyncio.to_thread(available, OTHER_THREAD))
                    self.assertFalse(await asyncio.to_thread(available, f.session))
                    gates[10].set()
                    self.assertEqual(json.loads(await ws.recv())["id"], 10)
                    self.assertFalse(await asyncio.to_thread(available, f.session))
                    gates[12].set()
                    self.assertEqual(json.loads(await ws.recv())["id"], 12)
                    self.assertTrue(await asyncio.to_thread(available, f.session))
            finally:
                for gate in gates.values(): gate.set()
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_pending_queue_blocks_closure_until_it_clears(self):
        for receipt in (False, True):
            with self.subTest(receipt=receipt), ReceiptFixture() as f:
                if not receipt:
                    closure.replace_rows([])
                f.native_state = "notLoaded"
                f.queue_items = [{"id": "pending", "clientUserMessageId": "client", "input": [{"type": "text", "text": "later"}]}]
                task, request, response, _ = await self.start_client(f)
                try:
                    self.assertIn("result", await self.send(task, request, response, "thread/queue/add", f.session, message="later"))
                    with self.assertRaises(ValueError):
                        await asyncio.to_thread(f.release.record_closure, f.log, f.entry, "supervisor")
                    self.assertTrue(closure.action_pending(f.session))
                    f.queue_items = []
                    closed = await asyncio.to_thread(f.release.record_closure, f.log, f.entry, "supervisor")
                    self.assertTrue(closed["process_exit_verified"])
                    self.assertFalse(closure.action_pending(f.session))
                finally:
                    (f.root / "stop").touch()
                    await asyncio.wait_for(task, 8)

    async def test_large_native_header_path_is_checked_then_stripped(self):
        with ReceiptFixture() as f:
            lines = f.transcript.read_text().splitlines()
            header = json.loads(lines[0])
            header["payload"]["base_instructions"] = {"text": "large native instructions " * 4000}
            f.transcript.write_text("\n".join([json.dumps(header), *lines[1:]]) + "\n")
            task, request, response, _ = await self.start_client(f)
            try:
                for method in ("thread/resume", "thread/fork"):
                    result = await self.send(task, request, response, method, f.session, path=str(f.transcript))
                    self.assertIn("result", result)
                    self.assertNotIn("path", f.mutations[-1]["params"])
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_approval_holds_its_source_until_answer_then_releases_it(self):
        from websockets.asyncio.client import unix_connect
        with ReceiptFixture() as f:
            task, _, _, _ = await self.start_client(f)
            endpoint = next(r["endpoint"] for r in closure.log_rows() if r.get("record_kind") == "managed_proxy")
            f.approval_target = OTHER_THREAD
            def available():
                try:
                    with closure.ownership(f.approval_target, create=True):
                        return True
                except closure.LeaseBusy:
                    return False
            try:
                async with unix_connect(endpoint) as ws:
                    await ws.send(json.dumps({"id": "approval", "method": "initialize", "params": {}}))
                    self.assertIn("result", json.loads(await ws.recv()))
                    approval = json.loads(await ws.recv())
                    self.assertEqual(approval["id"], "approval")
                    self.assertFalse(await asyncio.to_thread(available))
                    await ws.send(json.dumps({"id": "approval", "result": {"decision": "denied"}}))
                    async with asyncio.timeout(3):
                        while not f.approval_results:
                            await asyncio.sleep(.01)
                    self.assertTrue(await asyncio.to_thread(available))
                    self.assertTrue(closure.action_pending(f.approval_target))
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_an_earlier_target_reply_does_not_lose_later_resume_ownership(self):
        from websockets.asyncio.client import unix_connect
        with ReceiptFixture() as f:
            f.parallel = True
            gates = {i: threading.Event() for i in (10, 11)}
            seen = {i: threading.Event() for i in gates}
            async def delayed(request):
                seen[request["id"]].set()
                await asyncio.to_thread(gates[request["id"]].wait, 8)
                return {"thread": {"id": request["params"]["threadId"],
                                   "path": str(f.home / "sessions/other-worker.jsonl")}}
            f.rpc_results.update({"turn/interrupt": delayed, "thread/resume": delayed})
            task, _, _, _ = await self.start_client(f)
            endpoint = next(r["endpoint"] for r in closure.log_rows() if r.get("record_kind") == "managed_proxy")
            try:
                async with unix_connect(endpoint) as ws:
                    for identity, method, target in ((10, "turn/interrupt", f.session), (11, "thread/resume", OTHER_THREAD)):
                        await ws.send(json.dumps({"id": identity, "method": method, "params": {"threadId": target}}))
                    for event in seen.values():
                        self.assertTrue(await asyncio.to_thread(event.wait, 3))
                    gates[10].set()
                    self.assertIn("result", json.loads(await ws.recv()))
                    gates[11].set()
                    self.assertIn("result", json.loads(await ws.recv()))
                    row = next(r for r in closure.log_rows() if r.get("session_id") == OTHER_THREAD)
                    self.assertTrue(row["resumed_generation"]["launch_generation"])
            finally:
                for gate in gates.values(): gate.set()
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_native_error_releases_operation_and_next_action_rechecks_freeze(self):
        with ReceiptFixture() as f:
            seen = []
            def refuse(request):
                seen.append(request)
                return ValueError("fixture native refusal")
            f.rpc_results["turn/interrupt"] = refuse
            task, request, response, _ = await self.start_client(f)
            try:
                self.assertIn("error", await self.send(task, request, response, "turn/interrupt", f.session))
                self.assertEqual(len(seen), 1)
                def freeze():
                    with closure.ownership(f.session, create=True):
                        closure.admit_sources({f.session})
                await asyncio.to_thread(freeze)
                self.assertIn("error", await self.send(task, request, response, "turn/start", f.session))
                self.assertFalse(f.mutations)
                self.assertTrue(closure.action_pending(f.session))
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_disconnect_releases_pending_action_but_keeps_its_durable_barrier(self):
        from websockets.asyncio.client import unix_connect
        with ReceiptFixture() as f:
            f.parallel = True
            received, gate = threading.Event(), threading.Event()
            async def delayed(_request):
                received.set()
                await asyncio.to_thread(gate.wait, 5)
                return {}
            f.rpc_results["turn/interrupt"] = delayed
            task, _, _, _ = await self.start_client(f)
            endpoint = next(r["endpoint"] for r in closure.log_rows() if r.get("record_kind") == "managed_proxy")
            try:
                async with unix_connect(endpoint) as ws:
                    await ws.send(json.dumps({"id": 10, "method": "turn/interrupt", "params": {"threadId": f.session}}))
                    self.assertTrue(await asyncio.to_thread(received.wait, 3))
                    with self.assertRaises(closure.LeaseBusy):
                        await asyncio.to_thread(f.release.record_closure, f.log, f.entry, "supervisor")
                async with asyncio.timeout(3):
                    while True:
                        try:
                            await asyncio.to_thread(self.acquire_source, f.session)
                            break
                        except closure.LeaseBusy:
                            await asyncio.sleep(.01)
                self.assertTrue(closure.action_pending(f.session))
                self.assertFalse(task.done())
            finally:
                gate.set()
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_intermediate_verdict_cannot_clear_a_later_overlapping_action(self):
        from websockets.asyncio.client import unix_connect
        with ReceiptFixture() as f:
            closure.replace_rows([])
            f.parallel = True
            seen = {i: threading.Event() for i in (10, 11)}
            gate = threading.Event()
            async def delayed(request):
                seen[request["id"]].set()
                await asyncio.to_thread(gate.wait, 5)
                return {}
            f.rpc_results["turn/interrupt"] = delayed
            task, _, _, _ = await self.start_client(f)
            endpoint = next(r["endpoint"] for r in closure.log_rows() if r.get("record_kind") == "managed_proxy")
            try:
                async with unix_connect(endpoint) as ws:
                    await ws.send(json.dumps({"id": 10, "method": "turn/interrupt", "params": {"threadId": f.session}}))
                    self.assertTrue(await asyncio.to_thread(seen[10].wait, 3))
                    timestamp = datetime.now(timezone.utc).isoformat()
                    with f.transcript.open("a") as stream:
                        for payload in ({"type": "agent_message", "message": "Safe to exit: yes"}, {"type": "task_complete"}):
                            stream.write(json.dumps({"type": "event_msg", "timestamp": timestamp, "payload": payload}) + "\n")
                    await ws.send(json.dumps({"id": 11, "method": "turn/interrupt", "params": {"threadId": f.session}}))
                    self.assertTrue(await asyncio.to_thread(seen[11].wait, 3))
                async with asyncio.timeout(3):
                    while True:
                        try:
                            await asyncio.to_thread(self.acquire_source, f.session)
                            break
                        except closure.LeaseBusy:
                            await asyncio.sleep(.01)
                with self.assertRaises(ValueError):
                    await asyncio.to_thread(f.release.record_closure, f.log, f.entry, "supervisor")
                self.assertTrue(closure.action_pending(f.session))
            finally:
                gate.set()
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    @staticmethod
    def acquire_source(session):
        with closure.ownership(session, create=True):
            pass

    async def test_refused_exact_resume_removes_its_registered_proxy(self):
        with ReceiptFixture() as f:
            with closure.ownership(f.session):
                closure.admit_sources({f.session})
            popen = subprocess.Popen
            def no_client(argv, **kwargs):
                if any(str(arg).endswith("codex-session.py") for arg in argv):
                    self.fail("a refused resume started its native client")
                return popen(argv, **kwargs)
            with patch.object(subprocess, "Popen", side_effect=no_client), patch.object(
                closure, "register_proxy", wraps=closure.register_proxy
            ) as registered:
                with self.assertRaises(ValueError):
                    await proxy.run_client(f.home, ["codex"], session=f.session)
            registered.assert_called_once()
            self.assertFalse(any(r.get("record_kind") == "managed_proxy" for r in closure.log_rows()))
            with self.assertRaises(ValueError):
                with closure.resume_guard(f.session): pass

    async def test_slow_closure_verification_does_not_block_launch_or_first_turn(self):
        with ReceiptFixture() as f, ThreadPoolExecutor(max_workers=1) as pool:
            transcript = f.home / "sessions/closing-source.jsonl"
            transcript.write_text(f.transcript.read_text().replace(f.session, "closing-source"))
            entry = {**f.entry, "session_id": "closing-source", "transcript": str(transcript)}
            verifying, release = threading.Event(), threading.Event()
            validate = f.release.validate_closure
            def slow(*args, **kwargs):
                verifying.set()
                if not release.wait(12):
                    raise TimeoutError("fixture verification was not released")
                return validate(*args, **kwargs)
            with patch.object(f.release, "validate_closure", side_effect=slow):
                finish = pool.submit(f.release.record_closure, f.log, entry, "supervisor")
                try:
                    self.assertTrue(await asyncio.to_thread(verifying.wait, 3))
                    task, request, response, _ = await self.start_client(f)
                    result = await self.send(task, request, response, "turn/start", OTHER_THREAD)
                    self.assertIn("result", result)
                    self.assertFalse(finish.done())
                finally:
                    release.set()
                    await asyncio.to_thread(finish.result, 3)
                    if "task" in locals():
                        self.assertTrue(any(r.get("record_kind") == "managed_proxy" for r in closure.log_rows()))
                        (f.root / "stop").touch()
                        await asyncio.wait_for(task, 8)

    async def test_first_turn_without_receipt_does_not_wait_for_other_store_mutation(self):
        with ReceiptFixture() as f, ThreadPoolExecutor(max_workers=1) as pool:
            task, request, response, _ = await self.start_client(f)
            held, release = threading.Event(), threading.Event()
            def mutate():
                with closure.store_lock(f.log):
                    held.set()
                    release.wait(6)
            mutation = pool.submit(mutate)
            try:
                self.assertTrue(await asyncio.to_thread(held.wait, 3))
                result = await self.send(task, request, response, "turn/start", OTHER_THREAD)
                self.assertIn("result", result)
            finally:
                release.set()
                await asyncio.to_thread(mutation.result, 3)
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_failed_generation_publication_keeps_connection_and_invalidation(self):
        with ReceiptFixture() as f:
            task, request, response, _ = await self.start_client(f)
            try:
                with patch.object(closure, "resumed_launch", side_effect=closure.LeaseBusy("fixture busy store")):
                    result = await self.send(task, request, response, "thread/resume", f.session)
                self.assertIn("error", result)
                self.assertTrue(next(r for r in closure.log_rows() if r.get("session_id") == f.session)["resumed_at"])
                result = await self.send(task, request, response, "thread/read", f.session)
                self.assertIn("result", result)
                result = await self.send(task, request, response, "thread/resume", f.session)
                self.assertIn("result", result)
                self.assertTrue(next(r for r in closure.log_rows() if r.get("session_id") == f.session)["resumed_generation"])
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_terminal_metadata_failure_reaps_only_the_owned_child(self):
        with ReceiptFixture() as f:
            children = []
            rescued = threading.Event()
            popen = subprocess.Popen
            native = f.root / "codex"
            native.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(10)\n")
            native.chmod(0o700)
            def spawn(*args, **kwargs):
                child = popen(*args, **kwargs)
                if not any(str(arg).endswith("codex-session.py") for arg in args[0]):
                    return child
                children.append(child)
                # Bounds the old broken synchronous Popen.__exit__ in this
                # regression, without killing any process outside the fixture.
                def rescue():
                    if child.poll() is None:
                        rescued.set()
                        child.terminate()
                timer = threading.Timer(2, rescue)
                timer.start()
                self.addCleanup(timer.cancel)
                return child
            with patch.object(subprocess, "Popen", side_effect=spawn), patch.object(os, "isatty", return_value=True), \
                    patch.object(os, "ttyname", side_effect=OSError("fixture TTY failed")):
                with self.assertRaisesRegex(OSError, "fixture TTY failed"):
                    await proxy.run_client(f.home, [str(native)])
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            # The timer has not had to rescue a synchronously blocked exit.
            self.assertFalse(rescued.is_set())

    async def test_removed_store_does_not_replace_client_exit_status(self):
        with ReceiptFixture() as f:
            task, _, _, _ = await self.start_client(f)
            f.log.unlink()
            (f.root / "stop").touch()
            self.assertEqual(await asyncio.wait_for(task, 8), 0)

    async def start_client(self, f, *, resume=None, wrapper=False, external=False, default_unix=False):
        ready, request, response, stop = [f.root / name for name in ("ready", "request", "response", "stop")]
        child = f.root / "codex"
        child.write_text(f"#!{sys.executable}\n" +
            "import json, os, sys, time\nfrom pathlib import Path\nfrom websockets.sync.client import unix_connect\n" +
            "def publish(path, value):\n temporary=path.with_suffix('.pending');temporary.write_text(value);temporary.replace(path)\n" +
            f"ready,request,response,stop=map(Path,{[str(p) for p in (ready,request,response,stop)]!r})\n" +
            "endpoint=sys.argv[sys.argv.index('--remote')+1][7:]\n" +
            "with unix_connect(endpoint) as ws:\n"
            " ws.send(json.dumps({'id':0,'method':'initialize','params':{}}))\n ws.recv()\n"
            " publish(ready,json.dumps({'pid':os.getpid()}))\n"
            " deadline=time.monotonic()+25\n"
            " while not stop.exists() and time.monotonic()<deadline:\n"
            "  if request.exists():\n"
            "   message=request.read_text();request.unlink();ws.send(message);reply=json.loads(ws.recv())\n"
            "   while reply.get('method') and 'id' in reply:\n"
            "    ws.send(json.dumps({'id':reply['id'],'result':{'decision':'approved'}}));reply=json.loads(ws.recv())\n"
            "   publish(response,json.dumps(reply))\n"
            "  time.sleep(.01)\n")
        child.chmod(0o700)
        if wrapper:
            native_child = f.root / "native/codex"
            native_child.parent.mkdir()
            child.rename(native_child)
            child.write_text(f"#!{sys.executable}\nimport subprocess,sys\n"
                             f"raise SystemExit(subprocess.call([{str(native_child)!r},*sys.argv[1:]]))\n")
            child.chmod(0o700)
        native = [str(child), "--remote", "unix://" if default_unix else "unix://" + str(proxy.Ingress(f.home).upstream)]
        if resume:
            native += ["resume", resume]
        else:
            native += ["--", "task with  repeated spaces\nnext\tline"]
        if external:
            child = await asyncio.create_subprocess_exec(sys.executable, str(Path(proxy.__file__).with_name("codex-session.py")),
                "launch", "--codex-home", str(f.home), "--", *native)
            task = asyncio.create_task(child.wait())
        else:
            task = asyncio.create_task(proxy.run_client(f.home, native, session=resume))
        async def finish():
            if not task.done():
                if stop.parent.exists():
                    stop.touch()
                else:
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.addAsyncCleanup(finish)
        await self.wait_file(ready, task)
        pid = json.loads(ready.read_text())["pid"]
        # Read the actual host ps rendering. Do not reconstruct argv in a fixture.
        f.commands = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "pid=,args="],
                                    check=True, capture_output=True, text=True).stdout
        if external:
            parent = int(subprocess.run(["ps", "-p", str(pid), "-o", "ppid="], check=True,
                                        capture_output=True, text=True).stdout)
            f.commands = subprocess.run(["ps", "-ww", "-p", ",".join(map(str, {pid, parent, child.pid})), "-o", "pid=,args="],
                                        check=True, capture_output=True, text=True).stdout
        return task, request, response, pid

    async def wait_file(self, path, task):
        async with asyncio.timeout(8):
            while not path.exists():
                if task.done():
                    await task
                    self.fail("native client exited before wire readback")
                await asyncio.sleep(.01)

    async def send(self, task, request, response, method, session, **extra):
        response.unlink(missing_ok=True)
        temporary = request.with_suffix(".pending")
        params = {"threadId": session, **extra} if session is not None else extra
        temporary.write_text(json.dumps({"id": 2, "method": method, "params": params}))
        temporary.replace(request)
        await self.wait_file(response, task)
        return json.loads(response.read_text())

    async def test_documented_default_unix_launch_reaches_guarded_native_client(self):
        with ReceiptFixture() as f:
            task, request, response, _ = await self.start_client(f, external=True, default_unix=True)
            try:
                result = await self.send(task, request, response, "thread/read", f.session)
                self.assertIn("result", result)
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_history_reads_preserve_closure_and_ordinary_finish_retry(self):
        for frozen in (False, True):
            with self.subTest(frozen=frozen), ReceiptFixture() as f:
                methods = [
                    ("app/installed", f.session, {"forceRefresh": True}),
                    ("app/read", f.session, {"appIds": ["fixture-app"], "includeTools": True}),
                    ("app/list", f.session, {"forceRefetch": True}),
                    ("thread/turns/list", f.session, {}),
                    ("thread/items/read", f.session, {"turnId": "source-turn", "itemIds": ["source-item"]}),
                    ("thread/timeline/list", f.session, {}),
                    ("thread/searchOccurrences", f.session, {"searchTerm": "receipt"}),
                    ("thread/backgroundTerminals/list", f.session, {}),
                    ("thread/search", None, {"searchTerm": "receipt"}),
                    ("thread/realtime/listVoices", None, {}),
                ]
                f.rpc_results = {method: {"data": []} for method, _, _ in methods}
                f.rpc_results["thread/realtime/listVoices"] = {"voices": []}
                if frozen:
                    with closure.ownership(f.session):
                        closure.admit_sources({f.session})
                before = next(r for r in closure.log_rows() if r.get("session_id") == f.session)
                task, request, response, _ = await self.start_client(f)
                try:
                    for method, target, params in methods:
                        with self.subTest(method=method):
                            result = await self.send(task, request, response, method, target, **params)
                            self.assertIn("result", result)
                            self.assertEqual(next(r for r in closure.log_rows() if r.get("session_id") == f.session), before)
                            closed = await asyncio.to_thread(f.release.record_closure, f.log, f.entry, "supervisor")
                            self.assertEqual(closed["ended_at"], before["ended_at"])
                    self.assertFalse(f.mutations)
                finally:
                    (f.root / "stop").touch()
                    await asyncio.wait_for(task, 8)

    async def test_nonpersisting_housekeeping_preserves_closure_and_finish_retry(self):
        for frozen in (False, True):
            with self.subTest(frozen=frozen), ReceiptFixture() as f:
                methods = [
                    ("thread/name/set", {"name": "Finished source"}),
                    ("thread/metadata/update", {"projectId": "fixture-project", "daybreakEnabled": True}),
                    ("thread/readState/update", {"expectedRevision": "fixture-revision", "operation": {"type": "read"}}),
                    ("thread/section/move", {"sectionId": "fixture-section"}),
                    ("thread/unsubscribe", {}),
                ]
                f.rpc_results = {method: {} for method, _ in methods}
                if frozen:
                    with closure.ownership(f.session):
                        closure.admit_sources({f.session})
                before = next(r for r in closure.log_rows() if r.get("session_id") == f.session)
                task, request, response, _ = await self.start_client(f)
                try:
                    for method, params in methods:
                        with self.subTest(method=method):
                            result = await self.send(task, request, response, method, f.session, **params)
                            self.assertIn("result", result)
                            self.assertEqual(next(r for r in closure.log_rows() if r.get("session_id") == f.session), before)
                            closed = await asyncio.to_thread(f.release.record_closure, f.log, f.entry, "supervisor")
                            self.assertEqual(closed["ended_at"], before["ended_at"])
                    self.assertFalse(f.mutations)
                finally:
                    (f.root / "stop").touch()
                    await asyncio.wait_for(task, 8)

    async def test_mtime_change_allows_ordinary_finish_retry(self):
        with ReceiptFixture() as f:
            before = next(r for r in closure.log_rows() if r.get("session_id") == f.session)
            stat = f.transcript.stat()
            os.utime(f.transcript, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            closed = await asyncio.to_thread(f.release.record_closure, f.log, f.entry, "supervisor")
            self.assertEqual(closed["ended_at"], before["ended_at"])

    async def test_persisted_metadata_preserves_generation_but_refuses_stale_closure(self):
        with ReceiptFixture() as f:
            before = next(r for r in closure.log_rows() if r.get("session_id") == f.session)
            def persist_metadata(_request):
                # Legacy native gitInfo updates append a SessionMeta marker.
                marker = json.loads(f.transcript.read_text().splitlines()[0])
                marker["timestamp"] = datetime.now(timezone.utc).isoformat()
                marker["payload"]["git"] = {"branch": "updated-branch"}
                with f.transcript.open("a") as stream:
                    stream.write(json.dumps(marker) + "\n")
                return {"thread": {"id": f.session}}
            f.rpc_results["thread/metadata/update"] = persist_metadata
            task, request, response, _ = await self.start_client(f)
            try:
                result = await self.send(task, request, response, "thread/metadata/update", f.session,
                                         gitInfo={"branch": "updated-branch"})
                self.assertIn("result", result)
                self.assertEqual(next(r for r in closure.log_rows() if r.get("session_id") == f.session), before)
                with self.assertRaises(ValueError):
                    await asyncio.to_thread(f.release.record_closure, f.log, f.entry, "supervisor")
                self.assertEqual(next(r for r in closure.log_rows() if r.get("session_id") == f.session), before)
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_unarchive_preserves_generation_but_moved_rollout_refuses_stale_closure(self):
        for frozen in (False, True):
            with self.subTest(frozen=frozen), ReceiptFixture(archived=True) as f:
                f.native_state = "notLoaded"
                if frozen:
                    with closure.ownership(f.session):
                        closure.admit_sources({f.session})
                before = next(r for r in closure.log_rows() if r.get("session_id") == f.session)
                previous = f.transcript
                def unarchive(_request):
                    restored = f.home / "sessions" / f.transcript.name
                    restored.parent.mkdir(parents=True)
                    f.transcript.rename(restored)
                    os.utime(restored, None)
                    f.transcript = restored
                    return {"thread": {"id": f.session, "path": str(restored), "status": {"type": f.native_state}}}
                f.rpc_results["thread/unarchive"] = unarchive
                task, request, response, _ = await self.start_client(f)
                try:
                    result = await self.send(task, request, response, "thread/unarchive", f.session)
                    self.assertIn("result", result)
                    self.assertFalse(previous.exists())
                    self.assertTrue(f.transcript.exists())
                    self.assertEqual(next(r for r in closure.log_rows() if r.get("session_id") == f.session), before)
                    with self.assertRaises((OSError, ValueError)):
                        await asyncio.to_thread(f.release.record_closure, f.log, f.entry, "supervisor")
                    with self.assertRaises(ValueError):
                        await asyncio.to_thread(f.release.record_closure, f.log,
                                               {**f.entry, "transcript": str(f.transcript)}, "supervisor")
                finally:
                    (f.root / "stop").touch()
                    await asyncio.wait_for(task, 8)

    async def test_multiline_native_launch_tui_resume_owns_the_source(self):
        with ReceiptFixture() as f:
            f.approval_on_resume = True
            task, request, response, _ = await self.start_client(f)
            try:
                result = await self.send(task, request, response, "thread/resume", f.session)
                self.assertIn("result", result)
                row = next(r for r in closure.log_rows() if r.get("session_id") == f.session)
                self.assertTrue(row["resumed_at"])
                self.assertIsNone(row["closure_evidence"])
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_handoff_refuses_manual_tui_resume_and_turn_before_forwarding(self):
        with ReceiptFixture() as f:
            await asyncio.to_thread(closure.admit_sources, {f.session})
            task, request, response, _ = await self.start_client(f)
            try:
                for method in ("thread/resume", "turn/start", "thread/fork", "thread/archive"):
                    result = await self.send(task, request, response, method, f.session)
                    self.assertIn("error", result)
                self.assertFalse(f.mutations)
                # Reading a closed source remains permitted, without an action.
                result = await self.send(task, request, response, "thread/read", f.session)
                self.assertIn("result", result)
                with self.assertRaises(ValueError):
                    with closure.resume_guard(f.session): pass
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_guarded_resume_on_another_thread_leaves_the_source_closed(self):
        with ReceiptFixture() as f:
            task, request, response, _ = await self.start_client(f, resume=OTHER_THREAD)
            try:
                result = await self.send(task, request, response, "thread/resume", OTHER_THREAD)
                self.assertIn("result", result)
                self.assertFalse(closure.action_pending(f.session))
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_real_cli_proxy_wrapper_and_native_process_lines_are_all_recognized(self):
        from session_liveness import harness_arguments
        with ReceiptFixture() as f:
            task, _, _, _ = await self.start_client(f, wrapper=True, external=True)
            try:
                lines = [line.split() for line in f.commands.splitlines() if line.split() and line.split()[0].isdigit()]
                self.assertEqual(len(lines), 3)  # Owned proxy, waiting wrapper, native client.
                for line in lines:
                    self.assertTrue(harness_arguments(" ".join(line[1:])), "real client/helper line was ignored: " + " ".join(line[1:3]))
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_concurrent_admission_lease_refuses_tui_resume_without_sending(self):
        with ReceiptFixture() as f, ThreadPoolExecutor(max_workers=1) as pool:
            task, request, response, _ = await self.start_client(f)
            held, release = threading.Event(), threading.Event()
            def own():
                with closure.ownership(f.session):
                    held.set()
                    release.wait(5)
            owner = pool.submit(own)
            try:
                self.assertTrue(await asyncio.to_thread(held.wait, 3))
                result = await self.send(task, request, response, "thread/resume", f.session)
                self.assertIn("error", result)
                self.assertFalse(f.mutations)
                release.set()
                await asyncio.to_thread(owner.result, 3)
                result = await self.send(task, request, response, "thread/resume", f.session)
                self.assertIn("result", result)
                self.assertEqual(len(f.mutations), 1)
            finally:
                release.set()
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_server_approval_cannot_reactivate_a_handed_off_source(self):
        with ReceiptFixture() as f:
            with closure.ownership(f.session):
                closure.admit_sources({f.session})
            f.approval_target = f.session
            task, _, _, _ = await self.start_client(f)
            try:
                async with asyncio.timeout(5):
                    while not f.approval_results:
                        await asyncio.sleep(.01)
                self.assertIn("error", f.approval_results[0])
                self.assertFalse(f.mutations)
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_resume_path_cannot_override_its_claimed_identity(self):
        with ReceiptFixture() as f:
            task, request, response, _ = await self.start_client(f)
            try:
                for method in ("thread/resume", "thread/fork"):
                    for extra in ({"path": str(f.transcript)}, {"history": []}):
                        result = await self.send(task, request, response, method, OTHER_THREAD, **extra)
                        self.assertIn("error", result)
                self.assertFalse(f.mutations)
                result = await self.send(task, request, response, "thread/resume", f.session, path=str(f.transcript))
                self.assertIn("result", result)
                self.assertNotIn("path", f.mutations[0]["params"])
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_fresh_start_owns_its_returned_thread_and_malformed_identities_refuse(self):
        # The stock TUI's dynamic tools describe threadId in a JSON Schema.
        schema = {"type": "object", "properties": {"threadId": {"type": "string", "minLength": 1}}, "required": ["threadId"]}
        tools = [{"type": "namespace", "name": "codex_tui", "tools": [
            {"type": "function", "name": "read_thread", "inputSchema": schema, "deferLoading": True}]}]
        with ReceiptFixture() as f:
            started = str(uuid.uuid4())
            f.rpc_results["thread/start"] = lambda _request: {"thread": {"id": started}}
            task, request, response, _ = await self.start_client(f)
            try:
                result = await self.send(task, request, response, "thread/start", None,
                                         dynamicTools=tools, sessionStartSource=None, ephemeral=False)
                self.assertEqual(result["result"]["thread"]["id"], started)
                self.assertTrue(closure.action_pending(started))
                with closure.ownership(started) as owned:
                    self.assertTrue(owned)
                result = await self.send(task, request, response, "turn/start", started,
                                         outputSchema=schema, input=[{"type": "text", "text": "go"}])
                self.assertIn("result", result)
                # Request-side targets keep their own references beside the new thread's.
                for named in (f.session, started):
                    result = await self.send(task, request, response, "thread/start", None,
                                             config={"mcp_servers": {"x": {"env": {"threadId": named}}}})
                    self.assertIn("result", result)
                    for source in {named, started}:
                        with closure.ownership(source) as owned:
                            self.assertTrue(owned)
                f.rpc_results["thread/start"] = {"thread": {"id": ""}}
                self.assertIn("error", await self.send(task, request, response, "thread/start", None))
                for method, params in (("thread/start", {"threadId": 5}), ("thread/start", {"conversationId": ""}),
                                       ("turn/start", {}), ("turn/start", {"threadId": None}),
                                       ("turn/start", {"threadId": {"id": started}})):
                    self.assertIn("error", await self.send(task, request, response, method, None, **params))
                self.assertEqual([m["method"] for m in f.mutations], ["turn/start"])
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)


    async def test_fresh_and_forked_threads_reach_native_with_memory_generation_off(self):
        with ReceiptFixture() as f:
            forwarded = []
            def record(native_request):
                forwarded.append(native_request["params"])
                return {"thread": {"id": str(uuid.uuid4())}}
            f.rpc_results.update({"thread/start": record, "thread/fork": record})
            task, request, response, _ = await self.start_client(f)
            try:
                for config in (None, {proxy.MEMORY_SETTING: True, "features": {"x": True}}):
                    result = await self.send(task, request, response, "thread/start", None, config=config)
                    self.assertIn("result", result)
                result = await self.send(task, request, response, "thread/fork", f.session)
                self.assertIn("result", result)
                self.assertEqual([p["config"] for p in forwarded], [
                    {proxy.MEMORY_SETTING: False}, {proxy.MEMORY_SETTING: False, "features": {"x": True}}, {proxy.MEMORY_SETTING: False}])
                self.assertIn("error", await self.send(task, request, response, "thread/start", None, config=["x"]))
                self.assertEqual(len(forwarded), 3)
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

if __name__ == "__main__":
    unittest.main()
