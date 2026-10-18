# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5", "websockets==17.2"]
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
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))
from test_github_plan_sessions import ReceiptFixture
import github_plan_closure as closure
import codex_session_proxy as proxy


class IngressTests(unittest.IsolatedAsyncioTestCase):
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
            self.assertTrue(next(r for r in closure.log_rows() if r.get("session_id") == f.session)["successor_admitted"])

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
                    result = await self.send(task, request, response, "turn/start", "brand-new-thread")
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
                result = await self.send(task, request, response, "turn/start", "brand-new-thread")
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

    async def test_child_metadata_failure_reaps_only_the_owned_child(self):
        with ReceiptFixture() as f:
            children = []
            rescued = threading.Event()
            popen, metadata = subprocess.Popen, closure.launch_metadata
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
            def capture(pid, *args):
                if pid != os.getpid():
                    raise OSError("fixture ps failed")
                return metadata(pid, *args)
            with patch.object(subprocess, "Popen", side_effect=spawn), patch.object(closure, "launch_metadata", side_effect=capture):
                with self.assertRaisesRegex(OSError, "fixture ps failed"):
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
                _, states, coverage = await asyncio.to_thread(f.observe)
                self.assertEqual(coverage["process_coverage"], "complete")
                self.assertEqual(states[f.session]["state"], "ended")
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_history_reads_preserve_closure_and_ordinary_finish_retry(self):
        for frozen in (False, True):
            with self.subTest(frozen=frozen), ReceiptFixture() as f:
                methods = [
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

    async def test_metadata_and_detach_preserve_closure_and_finish_retry(self):
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
                            f.native_state = "active"
                            _, states, _ = await asyncio.to_thread(f.observe)
                            self.assertEqual(states[f.session]["state"], "live")
                            f.native_state = "idle"
                    self.assertFalse(f.mutations)
                finally:
                    (f.root / "stop").touch()
                    await asyncio.wait_for(task, 8)

    async def test_unarchive_preserves_generation_but_moved_rollout_invalidates_receipt(self):
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
                    self.assertIsNone(await asyncio.to_thread(closure.verified_record, f.session, f.home))
                    # The existing unloaded proof can read the restored rollout;
                    # it does not reinterpret the stale idle-closure fingerprint.
                    _, states, _ = await asyncio.to_thread(f.observe, {f.session})
                    self.assertEqual(states[f.session]["native_state"], "notLoaded")
                    self.assertEqual(states[f.session]["state"], "ended")
                    self.assertFalse(states[f.session]["leased_closure"])
                finally:
                    (f.root / "stop").touch()
                    await asyncio.wait_for(task, 8)

    async def test_multiline_native_launch_is_known_but_tui_resume_owns_the_source(self):
        with ReceiptFixture() as f:
            f.approval_on_resume = True
            task, request, response, pid = await self.start_client(f)
            try:
                _, states, coverage = await asyncio.to_thread(f.observe)
                self.assertEqual(coverage["process_coverage"], "complete")
                self.assertEqual(states[f.session]["state"], "ended")
                result = await self.send(task, request, response, "thread/resume", f.session)
                self.assertIn("result", result)
                _, states, _ = await asyncio.to_thread(f.observe)
                self.assertEqual(states[f.session]["state"], "live")
                row = next(r for r in closure.log_rows() if r.get("session_id") == f.session)
                self.assertEqual(row["resumed_generation"]["pid"], pid)
                self.assertTrue(row["resumed_at"])
                self.assertIsNone(row["closure_evidence"])
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_handoff_refuses_manual_tui_resume_and_turn_before_forwarding(self):
        with ReceiptFixture() as f:
            def admit():
                with closure.admission():
                    _, states, _ = f.observe()
                    self.assertEqual(states[f.session]["state"], "ended")
                    closure.admit_sources({f.session})
            await asyncio.to_thread(admit)
            task, request, response, _ = await self.start_client(f)
            try:
                for method in ("thread/resume", "turn/start", "thread/fork", "thread/archive"):
                    result = await self.send(task, request, response, method, f.session)
                    self.assertIn("error", result)
                self.assertFalse(f.mutations)
                # Reading a closed source remains permitted, without an action.
                result = await self.send(task, request, response, "thread/read", f.session)
                self.assertIn("result", result)
                self.assertTrue(next(r for r in closure.log_rows() if r.get("session_id") == f.session)["successor_admitted"])
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_guarded_resume_on_another_thread_does_not_block_idle_proof(self):
        with ReceiptFixture() as f:
            task, request, response, _ = await self.start_client(f, resume="other-thread")
            try:
                result = await self.send(task, request, response, "thread/resume", "other-thread")
                self.assertIn("result", result)
                _, states, coverage = await asyncio.to_thread(f.observe)
                self.assertEqual(coverage["process_coverage"], "complete")
                self.assertEqual(states[f.session]["state"], "ended")
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_guarded_native_child_of_waiting_wrapper_is_known(self):
        with ReceiptFixture() as f:
            task, _, _, _ = await self.start_client(f, wrapper=True)
            try:
                _, states, coverage = await asyncio.to_thread(f.observe)
                self.assertEqual(coverage["process_coverage"], "complete")
                self.assertEqual(states[f.session]["state"], "ended")
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)

    async def test_real_cli_proxy_wrapper_and_native_process_lines_are_all_observed(self):
        from github_plan_sessions import codex_argv_start
        with ReceiptFixture() as f:
            task, _, _, _ = await self.start_client(f, wrapper=True, external=True)
            try:
                lines = [line.split() for line in f.commands.splitlines() if line.split() and line.split()[0].isdigit()]
                self.assertEqual(len(lines), 3)  # Owned proxy, waiting wrapper, native client.
                for line in lines:
                    self.assertIsNotNone(codex_argv_start(line[1:]), "real client/helper line was ignored: " + " ".join(line[1:3]))
                _, states, coverage = await asyncio.to_thread(f.observe)
                self.assertEqual(coverage["process_coverage"], "complete")
                self.assertEqual(states[f.session]["state"], "ended")
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
                        result = await self.send(task, request, response, method, "other-thread", **extra)
                        self.assertIn("error", result)
                self.assertFalse(f.mutations)
                result = await self.send(task, request, response, "thread/resume", f.session, path=str(f.transcript))
                self.assertIn("result", result)
                self.assertNotIn("path", f.mutations[0]["params"])
            finally:
                (f.root / "stop").touch()
                await asyncio.wait_for(task, 8)


if __name__ == "__main__":
    unittest.main()
