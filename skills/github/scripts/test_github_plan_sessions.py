# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Fake native daemon evidence, including refusal and read-only transport."""
import copy
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import web
import github_plan_sessions as SESSIONS


class NativeSessionsTests(unittest.IsolatedAsyncioTestCase):
    def test_resumed_cli_session_is_live_even_without_a_daemon(self):
        rows = SESSIONS.process_sessions("/opt/bin/codex resume holder\nrg resume holder\n/opt/bin/codex resume different", {"holder"})
        self.assertEqual([r["sessionId"] for r in rows], ["holder"])

    async def observe(self, state, *, safe=True, after=None, changed=False, mismatch=False):
        thread = {"id": "holder", "cwd": "/retained/issue-42", "path": "/fixture/transcript", "status": {"type": state}}
        if mismatch: thread["id"] = "another-session"
        reads, calls, unexpected = 0, 0, []
        async def handler(request):
            nonlocal reads
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                request = frame.json()
                method = request["method"]
                if method == "initialized": continue
                if method == "initialize": result = {}
                elif method == "thread/loaded/list": result = {"data": ["holder"], "nextCursor": None}
                elif method == "thread/read":
                    reads += 1
                    observed = copy.deepcopy(thread)
                    if reads > 1 and after: observed["status"]["type"] = after
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
            return {"safe_verdict": safe, "timestamp": "2026-10-01T02:00:00Z", "size": calls if changed else 1}
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
            os.chmod(socket_path, 0o600)
            try:
                with patch.object(SESSIONS, "native_helpers", return_value=(None, SimpleNamespace(session_status=status))):
                    result = await SESSIONS.observe_home(home, {"holder"})
                self.assertTrue(socket_path.exists())  # The observer leaves the native server intact.
            finally:
                await runner.cleanup()
        self.assertEqual(unexpected, [])
        return result

    async def test_idle_native_session_with_verified_final_handoff_is_ended(self):
        peers, states = await self.observe("idle")
        self.assertEqual(peers, [])
        self.assertEqual(states["holder"]["state"], "ended")

    async def test_active_session_overrides_completion(self):
        peers, states = await self.observe("active")
        self.assertEqual(states["holder"]["state"], "live")
        self.assertEqual(peers[0]["sessionId"], "holder")

    async def test_idle_failure_unknown_and_changed_evidence_do_not_prove_end(self):
        for kwargs in ({"safe": False}, {"changed": True}, {"mismatch": True}):
            with self.subTest(kwargs=kwargs):
                _, states = await self.observe("idle", **kwargs)
                self.assertNotIn("holder", states)
        _, states = await self.observe("systemError")
        self.assertNotIn("holder", states)

    async def test_resumed_while_reading_is_live(self):
        peers, states = await self.observe("idle", after="active")
        self.assertEqual(states["holder"]["state"], "live")
        self.assertTrue(peers)

    async def test_missing_daemon_does_not_establish_end(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(FileNotFoundError):
                await SESSIONS.observe_home(Path(folder), {"holder"})


class ObservationMergeTests(unittest.TestCase):
    def test_live_evidence_wins_across_account_homes(self):
        async def read(home, _sessions):
            return [], {"holder": {"state": "live" if home.name == "live" else "ended"}}
        with patch.object(SESSIONS.shutil, "which", return_value="codex"), \
                patch.object(SESSIONS, "codex_homes", return_value=[Path("/live"), Path("/ended")]), \
                patch.object(SESSIONS, "observe_home", side_effect=read), \
                patch.object(SESSIONS.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="")):
            _, states = SESSIONS.observe({"holder"})
        self.assertEqual(states["holder"]["state"], "live")


if __name__ == "__main__":
    unittest.main()
