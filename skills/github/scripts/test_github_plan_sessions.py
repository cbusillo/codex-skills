# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Fake native daemon evidence, including refusal and read-only transport."""
import copy
import json
import os
import socket
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import web
import github_plan_sessions as SESSIONS
import github_plan_closure as CLOSURE


class NativeSessionsTests(unittest.IsolatedAsyncioTestCase):
    def test_resumed_cli_session_is_live_even_without_a_daemon(self):
        for command in ("/opt/bin/codex resume holder", "codex --remote unix://fixture resume holder", "node /opt/bin/codex.js resume holder", "codex exec resume holder"):
            self.assertEqual([r["sessionId"] for r in SESSIONS.process_sessions(command, {"holder"})], ["holder"])
        for command in ("rg resume holder", "rg codex resume holder", "codex exec please resume holder", "codex please resume holder"):
            self.assertEqual(SESSIONS.process_sessions(command, {"holder"}), [])

    async def observe(self, state, *, safe=True, after=None, changed=False, mismatch=False, with_peer=False, unsafe=False,
                      receipt=None, proof_sessions=None):
        thread = {"id": "holder", "cwd": "/retained/issue-42", "path": "/fixture/transcript", "status": {"type": state}}
        if mismatch: thread["id"] = "another-session"
        reads, calls, unexpected, observed_ids = 0, 0, [], set()
        async def handler(request):
            nonlocal reads
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                request = frame.json()
                method = request["method"]
                if method == "initialized": continue
                if method == "initialize": result = {}
                elif method == "thread/loaded/list": result = {"data": ["holder", "peer"] if with_peer else ["holder"], "nextCursor": None}
                elif method == "thread/read":
                    reads += 1
                    observed = copy.deepcopy(thread)
                    observed_ids.add(request["params"]["threadId"])
                    if request["params"]["threadId"] == "peer": observed.update(id="peer", status={"type": "idle"})
                    if reads > 2 and after: observed["status"]["type"] = after
                    result = {"thread": observed}
                else:
                    unexpected.append(method)
                    await ws.close()
                    break
                await ws.send_json({"id": request["id"], "result": result})
            return ws
        def status(_entry):
            nonlocal calls
            calls += 1
            if with_peer: self.assertIn("peer", observed_ids)
            return {"/fixture/transcript": {"safe_verdict": safe, "timestamp": "2026-10-01T02:00:00Z", "size": calls if changed else 1}}
        # AF_UNIX paths are short on macOS; /var/folders can exceed that limit.
        with tempfile.TemporaryDirectory(prefix="claim-", dir="/tmp") as folder:
            home = Path(folder)
            socket_path = home / "app-server-control/app-server-control.sock"
            socket_path.parent.mkdir()
            app = web.Application()
            app.router.add_get("/", handler)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.UnixSite(runner, str(socket_path)).start()
            os.chmod(socket_path, 0o666 if unsafe else 0o600)
            try:
                with patch.object(SESSIONS, "transcript_snapshot", side_effect=lambda *_args: status(None)), \
                        patch.object(CLOSURE, "verified_record", side_effect=receipt if isinstance(receipt, list) else None,
                                     return_value=None if isinstance(receipt, list) else receipt):
                    peers, states, coverage = await SESSIONS.observe_home(home, {"holder"}, proof_sessions)
            finally:
                await runner.cleanup()
        self.assertEqual(unexpected, [])
        self.assertEqual(coverage["peer_complete"], not (mismatch or unsafe))
        if unsafe: self.assertEqual(coverage["status"], "incomplete")
        return peers, states

    async def test_unloaded_native_session_with_verified_final_handoff_is_candidate(self):
        peers, states = await self.observe("notLoaded")
        self.assertEqual(peers, [])
        self.assertEqual(states["holder"]["state"], "ended")

    async def test_released_unloaded_session_can_use_fresh_shared_closure(self):
        peers, states = await self.observe("notLoaded", receipt={"session_id": "holder"}, proof_sessions=set())
        self.assertFalse(peers)
        self.assertEqual(states["holder"]["source"], "supervisor_closure")
        _, states = await self.observe("notLoaded", proof_sessions=set())
        self.assertNotIn("holder", states)

    async def test_changed_closure_cannot_prove_an_unloaded_session_ended(self):
        _, states = await self.observe("notLoaded", receipt=[{"session_id": "holder"}, None])
        self.assertNotIn("holder", states)

    async def test_active_session_overrides_completion(self):
        for state in ("active", "idle", "systemError"):
            peers, states = await self.observe(state)
            self.assertEqual(states["holder"]["state"], "live")
            self.assertEqual(peers[0]["sessionId"], "holder")

    async def test_idle_failure_unknown_and_changed_evidence_do_not_prove_end(self):
        for kwargs in ({"safe": False}, {"changed": True}, {"mismatch": True}):
            with self.subTest(kwargs=kwargs):
                _, states = await self.observe("notLoaded", **kwargs)
                self.assertNotIn("holder", states)

    async def test_resumed_while_reading_is_live(self):
        peers, states = await self.observe("notLoaded", after="active")
        self.assertEqual(states["holder"]["state"], "live")
        self.assertTrue(peers)

    async def test_loaded_peers_are_read_before_claim_transcript_work(self):
        peers, _ = await self.observe("notLoaded", with_peer=True)
        self.assertIn("peer", [peer["sessionId"] for peer in peers])

    async def test_missing_daemon_does_not_establish_end(self):
        with tempfile.TemporaryDirectory() as folder:
            _, states, coverage = await SESSIONS.observe_home(Path(folder), {"holder"})
        self.assertFalse(states)
        self.assertEqual(coverage["status"], "unavailable")

    async def test_unsafe_existing_socket_is_incomplete(self):
        peers, states = await self.observe("idle", unsafe=True)
        self.assertEqual((peers, states), ([], {}))

    async def test_non_listening_stale_socket_is_unavailable(self):
        with tempfile.TemporaryDirectory(prefix="claim-", dir="/tmp") as folder:
            home = Path(folder)
            path = home / "app-server-control/app-server-control.sock"
            path.parent.mkdir()
            with socket.socket(socket.AF_UNIX) as stale:
                stale.bind(str(path))
                os.chmod(path, 0o600)
                peers, states, coverage = await SESSIONS.observe_home(home, {"holder"})
        self.assertEqual((peers, states), ([], {}))
        self.assertEqual(coverage["status"], "unavailable")


class ObservationMergeTests(unittest.TestCase):
    def test_receipt_proofs_require_available_account_inventory(self):
        config = SimpleNamespace(load_config=lambda: (_ for _ in ()).throw(ValueError("missing account inventory")))
        with patch.object(SESSIONS, "native_helpers", return_value=(config, None)):
            self.assertTrue(SESSIONS.codex_homes())
            with self.assertRaisesRegex(ValueError, "missing account inventory"):
                SESSIONS.codex_homes(strict=True)

    def test_shared_log_is_independent_of_provider_account_home(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"CODE_HOME": folder}):
            first = CLOSURE.closed_log()
            with patch.dict(os.environ, {"CODEX_HOME": str(Path(folder) / "other-account")}):
                self.assertEqual(CLOSURE.closed_log(), first)
            self.assertTrue(first.is_relative_to(folder))
            self.assertFalse(first.exists())

    def test_session_specific_closure_allows_unrelated_clients_but_requires_process_read(self):
        async def read(_home, _sessions, _proof):
            return [], {"holder": {"state": "ended", "source": "supervisor_closure"}}, {
                "status": "available", "peer_complete": True, "socket": "/fixture/socket"}
        for returncode in (0, 1):
            with self.subTest(returncode=returncode), \
                    patch.object(SESSIONS, "codex_homes", return_value=[Path("/fixture")]), \
                    patch.object(SESSIONS, "observe_home", side_effect=read), \
                    patch.object(SESSIONS.subprocess, "run", return_value=SimpleNamespace(
                        returncode=returncode, stdout="codex --remote unix:///unrelated/socket")):
                _, states, _ = SESSIONS.observe({"holder"})
            self.assertEqual(states.get("holder", {}).get("state") == "ended", returncode == 0)

    def test_daemon_reads_do_not_require_codex_on_path(self):
        async def read(_home, _sessions, _proof):
            return [{"sessionId": "holder", "source": "codex_session"}], {"holder": {"state": "live"}}, {"status": "available", "peer_complete": True, "socket": "/fixture/socket"}
        with patch.dict(os.environ, {"PATH": ""}), \
                patch.object(SESSIONS, "codex_homes", return_value=[Path("/fixture")]), \
                patch.object(SESSIONS, "observe_home", side_effect=read), \
                patch.object(SESSIONS.subprocess, "run", side_effect=FileNotFoundError):
            peers, states, coverage = SESSIONS.observe({"holder"})
        self.assertEqual(states["holder"]["state"], "live")
        self.assertTrue(peers)
        self.assertEqual(coverage["process_coverage"], "unavailable")

    def test_app_server_process_requires_its_exact_observed_socket(self):
        socket_path = str(Path("/fixture/socket").resolve())
        self.assertTrue(SESSIONS.process_absence_known(f"codex app-server --listen unix://{socket_path}", {socket_path}))
        self.assertFalse(SESSIONS.process_absence_known("codex app-server", {socket_path}))
        self.assertFalse(SESSIONS.process_absence_known("codex app-server --listen unix:///other/socket", {socket_path}))

    def test_incomplete_peer_scan_is_reported_and_cannot_prove_native_end(self):
        async def read(_home, _sessions, _proof):
            return [], {"holder": {"state": "ended"}}, {"status": "incomplete", "peer_complete": False, "socket": "/fixture/socket"}
        with patch.object(SESSIONS, "codex_homes", return_value=[Path("/fixture")]), \
                patch.object(SESSIONS, "observe_home", side_effect=read), \
                patch.object(SESSIONS.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="")):
            _, states, coverage = SESSIONS.observe({"holder"})
        self.assertNotIn("holder", states)
        self.assertTrue(coverage["observed_source_incomplete"])
        self.assertEqual(coverage["status"], "incomplete")

    def test_snapshot_reads_archived_sibling_transcripts_for_the_same_native_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            history = [
                {"type": "session_meta", "payload": {"id": "holder"}},
                {"type": "event_msg", "timestamp": "2026-10-01T02:00:00Z", "payload": {"type": "agent_message", "message": "Safe to exit: yes"}},
                {"type": "event_msg", "timestamp": "2026-10-01T02:00:01Z", "payload": {"type": "task_complete"}},
            ]
            primary = home / "sessions/rollout-holder.jsonl"
            sibling = home / "archived_sessions/rollout-holder.jsonl"
            for path, records in ((primary, history), (sibling, [*history, {
                "type": "event_msg", "timestamp": "2026-10-01T03:00:00Z", "payload": {"type": "task_started"}}])):
                path.parent.mkdir()
                path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
            with patch.object(SESSIONS, "codex_homes", return_value=[home]):
                snapshot = SESSIONS.transcript_snapshot("holder", str(primary))
            self.assertTrue(snapshot[str(primary.resolve())]["safe_verdict"])
            self.assertFalse(snapshot[str(sibling.resolve())]["safe_verdict"])

    def test_unknown_interactive_process_or_missing_process_inventory_prevents_native_end(self):
        async def read(_home, _sessions, _proof): return [], {"holder": {"state": "ended"}}, {"status": "available", "peer_complete": True, "socket": "/fixture/socket"}
        for command in ("codex resume --last", "codex --remote unix://fixture", "codex", "node /opt/codex-cli", "codex app-server", "codex app-server --listen unix:///unobserved/socket", ""):
            for returncode in (0, 1):
                with self.subTest(command=command, returncode=returncode), \
                        patch.object(SESSIONS, "codex_homes", return_value=[Path("/fixture")]), \
                        patch.object(SESSIONS, "observe_home", side_effect=read), \
                        patch.object(SESSIONS.subprocess, "run", return_value=SimpleNamespace(returncode=returncode, stdout=command)):
                    _, states, _ = SESSIONS.observe({"holder"})
                    self.assertEqual(states.get("holder", {}).get("state") == "ended", not command and returncode == 0)

    def test_live_evidence_wins_across_account_homes(self):
        async def read(home, _sessions, _proof):
            return [], {"holder": {"state": "live" if home.name == "live" else "ended"}}, {"status": "available", "peer_complete": True, "socket": str(home / "socket")}
        with patch.object(SESSIONS, "codex_homes", return_value=[Path("/live"), Path("/ended")]), \
                patch.object(SESSIONS, "observe_home", side_effect=read), \
                patch.object(SESSIONS.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="")):
            _, states, _ = SESSIONS.observe({"holder"})
        self.assertEqual(states["holder"]["state"], "live")


if __name__ == "__main__":
    unittest.main()
