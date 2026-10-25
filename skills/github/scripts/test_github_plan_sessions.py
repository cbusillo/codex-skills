# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Fake native daemon evidence, including refusal and read-only transport."""
import asyncio
from contextlib import ExitStack, redirect_stdout
import io
import inspect
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aiohttp import web
import github_plan_sessions as SESSIONS
import github_plan_closure as CLOSURE

class ReceiptFixture:
    """A real read-only Unix daemon plus an actual shared closure receipt."""
    def __init__(self, session="holder", *, archived=False):
        self.session = session
        self.archived = archived
        self.native_state = "idle"
        self.process_rows = []
        self.commands = ""
        self.execution_items = []
        self.incomplete = False
        self.failure = None
        self.mutations = []
        self.rpc_results = {}
        self.rpc_requests = []
        self.home_requests = []
        self.parallel = False
        self.queue_items = []
        self.home_queues = {}
        self.home_states = {}
        self.socket_homes = {}
        self.approval_target = None
        self.server_request = None
        self.approval_results = []
        self.approval_on_resume = False

    def __enter__(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="lease-", dir="/tmp"))).resolve()
        self.home = self.root / "account"
        self.homes = [self.home]
        self.transcript = self.home / ("archived_sessions" if self.archived else "sessions") / ("rollout-" + self.session + ".jsonl")
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text("\n".join(json.dumps(row) for row in [
            {"type": "session_meta", "payload": {"id": self.session}},
            {"type": "event_msg", "timestamp": "2026-10-01T01:59:59Z", "payload": {"type": "task_started", "turn_id": "source-turn"}},
            {"type": "event_msg", "timestamp": "2026-10-01T02:00:00Z", "payload": {"type": "agent_message", "message": "Safe to exit: yes"}},
            {"type": "event_msg", "timestamp": "2026-10-01T02:00:01Z", "payload": {"type": "task_complete", "turn_id": "source-turn"}},
        ]) + "\n")
        self.log = self.root / ".code/skill-data/supervisor-closed-sessions.json"
        self.stack.enter_context(patch.dict(os.environ, {"HOME": str(self.root), "CODE_HOME": str(self.log.parent.parent), "CODEX_HOME": str(self.home)}))
        self.stack.enter_context(patch.object(CLOSURE, "closed_log", return_value=self.log))
        self.stack.enter_context(patch.object(CLOSURE, "host_identity", return_value={"boot": "fixture-boot", "uid": os.getuid()}))
        self.stack.enter_context(patch.object(SESSIONS.process_liveness(), "host_boot_time", return_value=None))
        self.stack.enter_context(patch.object(SESSIONS, "configured_homes", side_effect=lambda **_kw: sorted(self.homes)))
        SESSIONS.native_helpers()
        self.stack.enter_context(patch.object(SESSIONS.process_liveness(), "process_arguments",
            side_effect=lambda _pid, command: (SESSIONS.process_liveness().harness_arguments(command), True)))
        self.stack.enter_context(patch.object(SESSIONS.process_liveness(), "process_absent", return_value=False))
        import release_session_claims
        self.release = release_session_claims
        self.stack.enter_context(patch.object(self.release, "inventory", side_effect=lambda: list(self.process_rows)))
        self.entry = {"session_id": self.session, "harness": "codex", "transcript": str(self.transcript),
                      "pid": 123, "tty": "fixture-tty", "supervisor_owned": True, "codex_home": str(self.home),
                      "launch_pid": 123, "launch_generation": "source-generation", "launch_started_at": "2026-10-01T01:00:00Z"}
        self.release.record_closure(self.log, self.entry, "supervisor")
        real_run = subprocess.run
        self.stack.enter_context(patch.object(subprocess, "run", side_effect=lambda argv, **kw:
            SimpleNamespace(returncode=0, stdout=self.commands) if argv[:2] == ["ps", "-axo"] or argv[:3] == ["ps", "-ww", "-axo"] else real_run(argv, **kw)))
        ready = threading.Event()

        async def serve():
            self.loop = asyncio.get_running_loop()
            self.stop = self.loop.create_future()
            async def handler(request):
                ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024)
                await ws.prepare(request)
                native_home = self.socket_homes.get(request.transport.get_extra_info("sockname"), self.home)
                native_path = self.transcript if native_home == self.home else native_home / "sessions" / self.transcript.name
                async def respond(request):
                    if "method" not in request:
                        self.approval_results.append(request)
                        return
                    method = request["method"]
                    self.rpc_requests.append(method)
                    self.home_requests.append((str(native_home), method))
                    if method == "initialized": return
                    if method == "initialize": result = self.rpc_results.get(method, {"codexHome": str(native_home)})
                    elif method in self.rpc_results or (str(native_home), method) in self.rpc_results:
                        reply = self.rpc_results.get((str(native_home), method), self.rpc_results.get(method))
                        result = reply(request) if callable(reply) else reply
                        if inspect.isawaitable(result):
                            result = await result
                    elif method == "thread/loaded/list": result = {"data": [self.session], "nextCursor": "more" if self.incomplete else None}
                    elif method == "thread/items/list": result = {"data": [{"item": {"id": "execution-" + str(index), **item}, "turnId": "source-turn"} for index, item in enumerate(self.execution_items)], "nextCursor": None}
                    elif method == "thread/queue/list": result = {"data": self.home_queues.get(str(native_home), self.queue_items), "nextCursor": None}
                    elif method == "thread/turns/list":
                        status = SESSIONS.native_helpers()[1].session_status({**self.entry, "transcript": str(native_path)})
                        # Native completion is independent of claim disposition.
                        result = {"data": [{"id": status["turn_id"], "status": "completed" if status["turn_completed_at"] else "inProgress"}] if status["turn_id"] else [], "nextCursor": None}
                    elif method == "thread/timeline/list":
                        status = SESSIONS.native_helpers()[1].session_status({**self.entry, "transcript": str(native_path)})
                        result = {"data": [{"type": "turnStarted", "position": 0, "turnId": status["turn_id"]},
                                           {"type": "turnCompleted", "position": 1, "turnId": status["turn_id"], "status": "completed"}]
                                  if status["turn_id"] else [], "nextCursor": None}
                    elif method == "thread/backgroundTerminals/list":
                        result = {"data": [], "nextCursor": None}
                    elif method == "thread/goal/get": result = {"goal": None}
                    elif method == "thread/read": result = {"thread": {
                        "id": self.session, "cwd": "/retained/source", "path": str(native_path), "historyMode": "paginated", "status": {"type": self.home_states.get(str(native_home), self.native_state)}}}
                    elif method in {"thread/resume", "thread/fork", "turn/start", "thread/archive", "thread/queue/add"}:
                        self.mutations.append(request)
                        target = request["params"]["threadId"]
                        if self.approval_on_resume:
                            await ws.send_json({"id": request["id"], "method": "execCommandApproval",
                                                "params": {"conversationId": target}})
                        result = ({"queuedSubmission": {"id": "queued", "clientUserMessageId": request["params"].get("clientUserMessageId", "fixture-client"),
                                   "input": request["params"].get("input", [])}} if method == "thread/queue/add" else
                                  {"thread": {"id": target, "path": str(self.transcript if target == self.session else self.home / "sessions" / (target + ".jsonl"))}})
                    else: raise AssertionError("unexpected native mutation: " + method)
                    if isinstance(result, Exception):
                        await ws.send_json({"id": request["id"], "error": {"code": -32600, "message": str(result)}})
                        return
                    await ws.send_json({"id": request["id"], "result": result})
                    if method == "initialize" and (self.server_request or self.approval_target):
                        await ws.send_json(self.server_request or {"id": "approval", "method": "execCommandApproval",
                                                                  "params": {"conversationId": self.approval_target}})
                tasks = []
                try:
                    async for frame in ws:
                        if self.parallel:
                            tasks.append(asyncio.create_task(respond(frame.json())))
                        else:
                            await respond(frame.json())
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                return ws
            app = web.Application()
            app.router.add_get("/", handler)
            runner = web.AppRunner(app)
            self.runner = runner
            await runner.setup()
            path = CLOSURE.daemon_endpoint(self.home)
            path.parent.mkdir()
            await web.UnixSite(runner, str(path)).start()
            os.chmod(path, 0o600)
            ready.set()
            try: await self.stop
            finally: await runner.cleanup()

        def start():
            try: asyncio.run(serve())
            except BaseException as error:
                self.failure = error
                ready.set()
        self.thread = threading.Thread(target=start)
        self.thread.start()
        if not ready.wait(3) or self.failure:
            raise RuntimeError("fixture daemon failed") from self.failure
        return self

    def add_home(self, home, *, copy_transcript=True):
        home = home.resolve()
        copy = home / "sessions" / self.transcript.name
        home.mkdir(parents=True)
        if copy_transcript:
            copy.parent.mkdir()
            copy.write_bytes(self.transcript.read_bytes())
        endpoint = CLOSURE.daemon_endpoint(home)
        endpoint.parent.mkdir()
        self.socket_homes[str(endpoint)] = home
        async def add():
            await web.UnixSite(self.runner, str(endpoint)).start()
            endpoint.chmod(0o600)
        asyncio.run_coroutine_threadsafe(add(), self.loop).result(3)
        self.homes.append(home)
        return copy

    def __exit__(self, *_):
        self.loop.call_soon_threadsafe(self.stop.set_result, None)
        self.thread.join(3)
        self.stack.close()
        if self.thread.is_alive() or self.failure:
            raise RuntimeError("fixture daemon did not stop") from self.failure

    def managed(self, native):
        """Registry-coordinate fixture; the ingress's wire behavior has its own tests."""
        binding = self.root / "fixture-ingress.sock"
        sock = socket.socket(socket.AF_UNIX)
        self.stack.callback(sock.close)
        sock.bind(str(binding))
        binding.chmod(0o600)
        proxy = CLOSURE.register_proxy(str(self.home), str(binding))
        native[native.index("--remote") + 1] = "unix://" + str(binding)
        with patch.object(CLOSURE.os, "getpid", return_value=456):
            CLOSURE.register_launch(native, str(self.home), proxy=proxy)


class FinishPreviewIntegrationTests(unittest.TestCase):
    def pending(self, f):
        with CLOSURE.resume_guard(f.session, method="thread/resume", home=f.home):
            pass
        f.native_state = "notLoaded"

    def finish(self, f, *, apply=False, discovery=None, history=None, closed_log=None):
        helper = f.release
        ledger = f.root / "ledger.json"
        ledger.write_text(json.dumps([f.entry]))
        source = {"worker": "trial", "session": f.session, "branch": "work/trial",
                  "claimed_at": "2026-10-01T01:00:00Z"}
        comments = [{"id": identifier, "body": body, "user": {"login": "fixture-bot"},
                     "created_at": timestamp, "updated_at": timestamp}
                    for identifier, body, timestamp in (
                        (1, "Claimed by trial\n\n" + helper.claim.marker(source), source["claimed_at"]),
                        (2, "Released claim 1", "2026-10-01T02:00:02Z"))]
        reader = Mock()
        reader.get_json.return_value = {"body": "", "number": 42}
        def read(*_args, **_kwargs):
            if history:
                history(f)
            return comments
        def discover(*_args, **_kwargs):
            if discovery:
                discovery(f)
            return [("owner/repo", 42)], ["owner/repo"]
        reader.paged_json.side_effect = read
        argv = ["helper", "finish", "--ledger", str(ledger), "--session-id", f.session,
                "--supervisor-session", "supervisor", "--owner", "owner", "--repo", "owner/repo",
                "--confirm-session-ended", "--transcript-root", str(f.home / "sessions")]
        if apply:
            argv.append("--apply")
        if closed_log is not None:
            argv += ["--closed-log", str(closed_log)]
        output = io.StringIO()
        with patch.object(helper.sys, "argv", argv), \
                patch.object(helper.github_identity, "automation_login", return_value="fixture-bot"), \
                patch.object(helper, "automation_only_gh_prefix_args", return_value=[]), \
                patch.object(helper, "GitHubReader", return_value=reader), \
                patch.object(helper, "discover", side_effect=discover), \
                patch.object(helper, "comment") as write, patch.object(helper, "run_release") as run, \
                redirect_stdout(output):
            code = helper.main()
        write.assert_not_called()
        run.assert_not_called()
        self.assertFalse(f.mutations)
        return code, json.loads(output.getvalue())

    def test_invalid_private_transcript_account_reports_refusal_without_side_effects(self):
        for case in ("supervisor-ledger", "private-finish", "backfill-selected", "backfill-unrelated",
                     "valid-private-offline"):
            with self.subTest(case=case), ReceiptFixture() as f:
                closed = CLOSURE.log_rows()[0]
                invalid = {**f.entry, "transcript": "~q526-absent-account/rollout.jsonl"}
                invalid_closed = {**closed, "transcript": invalid["transcript"]}
                ledger_rows = [invalid] if case == "supervisor-ledger" else [f.entry]
                private_rows = ([closed, {**invalid_closed, "session_id": "unrelated-source"}]
                                if case == "backfill-unrelated" else
                                [invalid_closed] if case in {"private-finish", "backfill-selected"} else [closed])
                ledger = f.root / "supervisor-ledger.json"
                private = f.root / "private-closed.json"
                ledger.write_text(json.dumps(ledger_rows))
                private.write_text(json.dumps(private_rows))
                private.chmod(0o600)
                if case == "valid-private-offline":
                    f.log.write_text("{")
                mode = "backfill" if case.startswith("backfill") else "finish"
                argv = ["helper", mode, "--supervisor-session", "supervisor", "--owner", "owner",
                        "--session-id", f.session, "--confirm-session-ended", "--transcript-root",
                        str(f.home / "sessions"), "--closed-log", str(private)]
                argv += ["--ledger", str(ledger)] if mode == "finish" else ["--checkout-root", str(f.root)]
                paths = (ledger, private, f.log, CLOSURE.lease_path(f.session))
                before = {path: path.read_bytes() for path in paths}
                reader = Mock()
                output = io.StringIO()
                with patch.object(f.release.sys, "argv", argv), \
                        patch.object(f.release.github_identity, "automation_login", return_value="fixture-bot"), \
                        patch.object(f.release, "automation_only_gh_prefix_args", return_value=[]), \
                        patch.object(f.release, "GitHubReader", return_value=reader), \
                        patch.object(f.release, "discover", return_value=([], ["owner/repo"])), \
                        patch.object(f.release, "comment") as write, patch.object(f.release, "run_release") as run, \
                        patch("pwd.getpwnam", side_effect=KeyError("fixture account absent")), redirect_stdout(output):
                    code = f.release.main()
                report = json.loads(output.getvalue())
                valid = case == "valid-private-offline"
                self.assertEqual(code, 0 if valid else 1)
                self.assertEqual(report["ok"], valid)
                write.assert_not_called()
                run.assert_not_called()
                self.assertFalse(reader.method_calls)
                self.assertFalse(f.mutations)
                self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_preview_preserves_recorded_cutoff_for_absent_and_resumed_sources(self):
        import github_plan_native as native
        import tmux_terminal
        for resumed in (False, True):
            for shared in (False, True):
                for boundary in ("discovery", "history"):
                    changes = ("stable", "reaped", "replacement") + (("queue",) if shared else ())
                    if not resumed and boundary == "history":
                        changes += ("source_pid", "source_resume", "unguarded")
                    for change in changes:
                        with self.subTest(resumed=resumed, shared=shared, boundary=boundary, change=change), ReceiptFixture(
                                "11111111-2222-4333-8444-555555555555") as f:
                            cutoff = datetime(2026, 10, 1, 2, 1, tzinfo=timezone.utc)
                            end = cutoff + timedelta(minutes=2)
                            current_turn = f.transcript.read_text().splitlines()[1:]
                            CLOSURE.replace_rows([])
                            if resumed:
                                prior = SESSIONS.native_helpers()[1].read_jsonl(f.transcript)
                                for row in prior:
                                    if row.get("timestamp"):
                                        row["timestamp"] = (f.release.release.stamp(row["timestamp"]) - timedelta(minutes=2)).isoformat()
                                    if row.get("payload", {}).get("turn_id"):
                                        row["payload"]["turn_id"] = "prior-" + row["payload"]["turn_id"]
                                f.transcript.write_text("".join(json.dumps(row) + "\n" for row in prior))
                                prior_verdict = f.release.release.stamp(SESSIONS.native_helpers()[1].session_status(f.entry)["timestamp"])
                                class PriorClock(datetime):
                                    @classmethod
                                    def now(cls, tz=None):
                                        return prior_verdict + timedelta(seconds=1)
                                with patch.object(f.release, "datetime", PriorClock):
                                    f.release.record_closure(f.log, f.entry, "supervisor")
                            f.entry.update(tmux_socket="fixture", tmux_session_id="tmux:fixture:" + "a" * 32,
                                           window_id="$1", tab_id="@1")
                            births = {123: cutoff - timedelta(hours=1), 124: cutoff - timedelta(minutes=2),
                                      456: cutoff + timedelta(seconds=22),
                                      567: end + timedelta(minutes=1), 568: end + timedelta(minutes=1, seconds=1)}
                            pane = {"session_id": f.entry["tmux_session_id"], "pane_id": "%1", "pid": 789,
                                    "tmux_socket": "fixture", "window_id": "$1", "tab_id": "@1",
                                    "tty": f.entry["tty"], "dead": True, "exited_at": cutoff.isoformat()}
                            state = {"changed": False, "launched": not resumed, "panes": [pane], "parents": {456: 1}}
                            captured, process_boundaries = {}, []
                            private = f.root / "retained-private.json"
                            class ActionClock(datetime):
                                @classmethod
                                def now(cls, tz=None):
                                    return (end + timedelta(minutes=2) if state["changed"] else
                                            cutoff + timedelta(minutes=1) if state["launched"] else births[124])
                            class EndClock(datetime):
                                @classmethod
                                def now(cls, tz=None):
                                    return end
                            def register(pid):
                                endpoint = f.root / ("guard-" + str(pid) + ".sock")
                                sock = socket.socket(socket.AF_UNIX)
                                f.stack.callback(sock.close)
                                sock.bind(str(endpoint))
                                endpoint.chmod(0o600)
                                argv = ["codex", "--remote", "unix://" + str(endpoint)]
                                with patch.object(CLOSURE.os, "getpid", return_value=pid), \
                                        patch.object(CLOSURE.sys, "argv", argv):
                                    CLOSURE.register_proxy(str(f.home), str(endpoint))
                                self.assertIsNotNone(CLOSURE.guarded_launch_endpoint(pid, argv))
                                return " ".join(argv)
                            def update(_f):
                                if state["changed"] or change == "stable":
                                    return
                                state["changed"] = True
                                if change == "queue":
                                    f.queue_items = [{"id": "new-input", "clientUserMessageId": "later-client", "input": []}]
                                    return
                                state["panes"] = []
                                if change == "replacement":
                                    f.process_rows = [(567, f.entry["tty"], "zsh"), (568, f.entry["tty"], register(568))]
                                    state["parents"] = {567: 42, 568: 567, 42: 1}
                                    expected_log[:] = [f.log.read_bytes()]
                                elif change == "source_pid":
                                    f.process_rows = [(f.entry["pid"], f.entry["tty"], "codex")]
                                elif change == "source_resume":
                                    f.process_rows = [(456, f.entry["tty"], "codex resume " + f.session)]
                                elif change == "unguarded":
                                    f.process_rows = [(567, f.entry["tty"], "codex")]
                            record_closure, recheck_process = f.release.record_closure, native.recheck_source_process
                            def record(*args, **kwargs):
                                closed = record_closure(*args, **kwargs)
                                captured.update({key: closed[key] for key in ("tty_exited_at", "ended_at", "verdict_at")})
                                return closed
                            def recheck(process):
                                result = recheck_process(process)
                                if captured:
                                    process_boundaries.append((process.cutoff, process.saved))
                                return result
                            with patch.object(CLOSURE, "process_started_at", side_effect=lambda pid: births[pid]), \
                                    patch.object(CLOSURE, "datetime", ActionClock), \
                                    patch.object(f.release, "datetime", EndClock), \
                                    patch.object(tmux_terminal.Terminal, "list", side_effect=lambda _self=None: list(state["panes"])), \
                                    patch.object(tmux_terminal, "process_parents", side_effect=lambda: dict(state["parents"])):
                                if resumed:
                                    metadata = CLOSURE.launch_metadata(124, "actual-owned-resume", str(f.home))
                                    f.entry.update(metadata, pid=metadata["launch_pid"], tty="resumed-tty")
                                    pane["tty"] = f.entry["tty"]
                                    with CLOSURE.resume_guard(f.session, method="thread/resume", home=f.home):
                                        CLOSURE.resumed_launch(f.session, metadata, str(f.transcript), f.entry["tty"])
                                    selected = CLOSURE.source_records({f.session})[f.session]
                                    self.assertEqual(selected["resumed_generation"]["launch_pid"], metadata["launch_pid"])
                                    with f.transcript.open("a") as stream:
                                        stream.write("\n".join(current_turn) + "\n")
                                    if not shared:
                                        f.log.rename(private)
                                else:
                                    selected = None
                                state["launched"] = True
                                f.process_rows = [(456, f.entry["tty"], register(456))]
                                f.native_state = "notLoaded"
                                if shared and not resumed:
                                    with CLOSURE.resume_guard(f.session, method="thread/resume", home=f.home):
                                        pass
                                self.assertEqual(CLOSURE.source_records({f.session}).get(f.session), selected if shared else None)
                                lease = CLOSURE.lease_path(f.session)
                                lease_before = lease.read_bytes()
                                expected_log = [f.log.read_bytes()]
                                private_before = private.read_bytes() if private.exists() else None
                                verdict = SESSIONS.native_helpers()[1].session_status(f.entry)["timestamp"]
                                with patch.object(f.release, "record_closure", side_effect=record), \
                                        patch.object(native, "recheck_source_process", side_effect=recheck):
                                    code, report = self.finish(f, **{boundary: update}, closed_log=None if shared else private)
                                refuses = change in {"queue", "source_pid", "source_resume", "unguarded"}
                                self.assertEqual(state["changed"], change != "stable")
                                self.assertEqual(code, 1 if refuses else 0, report)
                                self.assertEqual(report["ok"], not refuses)
                                self.assertEqual(CLOSURE.action_pending(f.session), shared or resumed)
                                self.assertEqual(lease.read_bytes(), lease_before)
                                self.assertEqual(f.log.read_bytes(), expected_log[0])
                                self.assertEqual(CLOSURE.source_records({f.session}).get(f.session), selected if shared else None)
                                self.assertEqual(private.read_bytes() if private.exists() else None, private_before)
                                self.assertEqual(captured["tty_exited_at"], cutoff.isoformat())
                                self.assertEqual(captured["ended_at"], end.isoformat())
                                self.assertEqual(captured["verdict_at"], verdict)
                                if not refuses:
                                    self.assertEqual(report["reports"][0]["ended_at"], captured["ended_at"])
                                    self.assertTrue(process_boundaries)
                                    for observed_cutoff, saved in process_boundaries:
                                        self.assertEqual(observed_cutoff, cutoff)
                                        self.assertEqual(saved, selected)

    def test_pending_preview_consumes_release_without_changing_native_or_closure_state(self):
        with ReceiptFixture() as f:
            self.pending(f)
            paths = [f.log, CLOSURE.lease_path(f.session)]
            before = [path.read_bytes() for path in paths]
            code, report = self.finish(f)
            self.assertEqual(code, 0)
            self.assertTrue(report["ok"])
            self.assertEqual([row["claim_comment"] for row in report["reports"][0]["released_claims"]], [1])
            self.assertTrue(CLOSURE.action_pending(f.session))
            self.assertEqual([path.read_bytes() for path in paths], before)

    def test_pending_preview_refuses_changed_source_action_or_work_at_either_boundary(self):
        for boundary in ("discovery", "history"):
            for change in ("generation", "source", "action", "queue", "turn"):
                with self.subTest(boundary=boundary, change=change), ReceiptFixture() as f:
                    self.pending(f)
                    changed = set()
                    def update(f):
                        if change in {"generation", "source"}:
                            with CLOSURE.ownership(f.session), CLOSURE.store_lock(f.log):
                                rows = CLOSURE.log_rows()
                                if change == "generation":
                                    rows[0]["resumed_generation"] = {**f.entry, "launch_generation": "next-generation"}
                                else:
                                    rows[0]["resumed_at"] = datetime.now(timezone.utc).isoformat()
                                CLOSURE.replace_rows(rows)
                        elif change == "action":
                            with CLOSURE.ownership(f.session):
                                CLOSURE.set_action_pending(f.session, True, method="turn/start", home=f.home)
                        elif change == "queue":
                            f.queue_items = [{"id": "later-input", "clientUserMessageId": "new-client",
                                              "input": [{"type": "text", "text": "new work"}]}]
                        else:
                            with f.transcript.open("a") as stream:
                                stream.write(json.dumps({"type": "event_msg", "timestamp": datetime.now(timezone.utc).isoformat(),
                                    "payload": {"type": "task_started", "turn_id": "new-turn"}}) + "\n")
                        changed.add(change)
                    code, report = self.finish(f, **{boundary: update})
                    self.assertEqual(changed, {change})
                    self.assertEqual(code, 1)
                    self.assertFalse(report["ok"])
                    self.assertTrue(CLOSURE.action_pending(f.session))

    def test_apply_resolves_original_pending_but_preserves_a_later_action(self):
        for boundary in (None, "discovery", "history"):
            with self.subTest(boundary=boundary), ReceiptFixture() as f:
                self.pending(f)
                changed = set()
                def update(f):
                    with CLOSURE.ownership(f.session):
                        CLOSURE.set_action_pending(f.session, True, method="turn/start", home=f.home)
                    changed.add(boundary)
                code, report = self.finish(f, apply=True, **({boundary: update} if boundary else {}))
                self.assertEqual(changed, {boundary} if boundary else set())
                self.assertEqual(code, 1 if boundary else 0)
                self.assertEqual(report["ok"], boundary is None)
                self.assertEqual(CLOSURE.action_pending(f.session), boundary is not None)

    def test_preview_cannot_acquire_authority_for_an_action_added_after_record(self):
        for boundary in ("discovery", "history"):
            with self.subTest(boundary=boundary), ReceiptFixture() as f:
                f.native_state = "notLoaded"
                changed = set()
                def update(f):
                    with CLOSURE.ownership(f.session):
                        CLOSURE.set_action_pending(f.session, True, method="turn/start", home=f.home)
                    changed.add(boundary)
                code, report = self.finish(f, **{boundary: update})
                self.assertEqual(changed, {boundary})
                self.assertEqual(code, 1)
                self.assertFalse(report["ok"])
                self.assertTrue(CLOSURE.action_pending(f.session))

    def test_plain_finish_refuses_a_new_turn_at_either_boundary(self):
        for apply in (False, True):
            for boundary in ("discovery", "history"):
                with self.subTest(apply=apply, boundary=boundary), ReceiptFixture() as f:
                    changed = set()
                    def update(f):
                        with f.transcript.open("a") as stream:
                            stream.write(json.dumps({"type": "event_msg", "timestamp": datetime.now(timezone.utc).isoformat(),
                                "payload": {"type": "task_started", "turn_id": "new-turn"}}) + "\n")
                        changed.add(boundary)
                    code, report = self.finish(f, apply=apply, **{boundary: update})
                    self.assertEqual(changed, {boundary})
                    self.assertEqual(code, 1)
                    self.assertFalse(report["ok"])

    def test_pending_preview_requires_unchanged_source_and_action_through_real_proof(self):
        import github_plan_native as native
        for timing in ("snapshot_start", "snapshot_end", "proof_return", "process_recheck"):
            for kind in (None, "action", "source", "generation"):
                with self.subTest(timing=timing, kind=kind), ReceiptFixture() as f, ExitStack() as stack:
                    self.pending(f)
                    paths = [f.log, CLOSURE.lease_path(f.session)]
                    before = [path.read_bytes() for path in paths]
                    real_proof = native.prove_end
                    real_snapshot = native.source_snapshot
                    real_process = native.recheck_source_process
                    state = {"changed": False, "proof_complete": False}
                    def change():
                        if kind is None or state["changed"]:
                            return
                        with CLOSURE.ownership(f.session):
                            if kind == "action":
                                CLOSURE.set_action_pending(f.session, True, method="turn/start", home=f.home)
                            else:
                                with CLOSURE.store_lock(f.log):
                                    rows = CLOSURE.log_rows()
                                    if kind == "generation":
                                        rows[0]["resumed_generation"] = {**f.entry, "launch_generation": "later-generation"}
                                    else:
                                        rows[0]["closure_evidence"] = {"changed": "different selected row"}
                                    CLOSURE.replace_rows(rows)
                        state["changed"] = True
                    def snapshot(*args, **kwargs):
                        if timing == "snapshot_start":
                            change()
                        result = real_snapshot(*args, **kwargs)
                        if timing == "snapshot_end":
                            change()
                        return result
                    async def prove(*args, **kwargs):
                        result = await real_proof(*args, **kwargs)
                        state["proof_complete"] = True
                        if timing == "proof_return":
                            change()
                        return result
                    def process(*args, **kwargs):
                        result = real_process(*args, **kwargs)
                        if timing == "process_recheck" and state["proof_complete"]:
                            change()
                        return result
                    def install(_f):
                        stack.enter_context(patch.object(native, "prove_end", side_effect=prove))
                        stack.enter_context(patch.object(native, "source_snapshot", side_effect=snapshot))
                        stack.enter_context(patch.object(native, "recheck_source_process", side_effect=process))
                    code, report = self.finish(f, history=install)
                    self.assertEqual(state["changed"], kind is not None)
                    if kind is None:
                        self.assertEqual(code, 0)
                        self.assertTrue(report["ok"])
                        self.assertEqual([row["claim_comment"] for row in report["reports"][0]["released_claims"]], [1])
                        self.assertTrue(CLOSURE.action_pending(f.session))
                        self.assertEqual([path.read_bytes() for path in paths], before)
                    else:
                        self.assertEqual(code, 1)
                        self.assertFalse(report["ok"])

    def source_artifact(self, f, kind):
        if kind == "malformed-json":
            f.log.write_text("{")
        elif kind == "unsafe-mode":
            f.log.chmod(0o644)
        elif kind == "duplicates":
            rows = CLOSURE.log_rows() or [f.entry]
            CLOSURE.replace_rows([*rows, *rows])
        elif kind == "missing-transcript":
            CLOSURE.replace_rows([{"session_id": f.session, "harness": "codex"}])
        elif kind == "normalization-error":
            CLOSURE.replace_rows([{"session_id": f.session, "harness": "codex", "transcript": "\0"}])
        elif kind == "unknown-user":
            f.stack.enter_context(patch("pwd.getpwnam", side_effect=KeyError("fixture account absent")))
            CLOSURE.replace_rows([{"session_id": f.session, "harness": "codex",
                                   "transcript": "~q526-missing-user-2285/rollout.jsonl"}])
        elif kind == "ambiguous-unrelated":
            CLOSURE.replace_rows([{"record_kind": "not-a-registration"}])
        elif kind == "absent":
            f.log.unlink()
        elif kind == "empty":
            CLOSURE.replace_rows([])
        elif kind == "registration-only":
            CLOSURE.replace_rows([])
            CLOSURE.register_proxy(str(f.home), str(f.root / "fixture-ingress.sock"))
        else:
            raise AssertionError(kind)

    def source_bytes(self, f):
        return f.log.read_bytes() if f.log.exists() else None

    def test_unknown_shared_source_cannot_resolve_or_record_a_pending_finish(self):
        import github_plan_native as native
        for kind in ("malformed-json", "unsafe-mode", "duplicates", "missing-transcript",
                     "normalization-error", "unknown-user", "ambiguous-unrelated"):
            for mode in ("apply", "preview", "resolve"):
                with self.subTest(kind=kind, mode=mode), ReceiptFixture() as f:
                    self.pending(f)
                    self.source_artifact(f, kind)
                    source = self.source_bytes(f)
                    lease = CLOSURE.lease_path(f.session).read_bytes()
                    if mode == "resolve":
                        with CLOSURE.ownership(f.session), self.assertRaises((OSError, ValueError)):
                            proof = asyncio.run(native.prove_end(f.entry))
                            native.resolve_pending(f.session, proof)
                    else:
                        code, report = self.finish(f, apply=mode == "apply")
                        self.assertEqual(code, 1)
                        self.assertFalse(report["ok"])
                    self.assertTrue(CLOSURE.action_pending(f.session))
                    self.assertEqual(CLOSURE.lease_path(f.session).read_bytes(), lease)
                    self.assertEqual(self.source_bytes(f), source)
                    self.assertFalse(f.mutations)

    def test_known_absence_allows_first_pending_finish_and_read_only_preview(self):
        for kind in ("absent", "empty", "registration-only"):
            for apply in (False, True):
                with self.subTest(kind=kind, apply=apply), ReceiptFixture() as f:
                    self.pending(f)
                    self.source_artifact(f, kind)
                    source = self.source_bytes(f)
                    lease = CLOSURE.lease_path(f.session).read_bytes()
                    code, report = self.finish(f, apply=apply)
                    self.assertEqual(code, 0)
                    self.assertTrue(report["ok"])
                    self.assertEqual(CLOSURE.action_pending(f.session), not apply)
                    if apply:
                        closed = CLOSURE.source_records({f.session})
                        self.assertEqual(closed[f.session]["session_id"], f.session)
                    else:
                        self.assertEqual(self.source_bytes(f), source)
                        self.assertEqual(CLOSURE.lease_path(f.session).read_bytes(), lease)
                    self.assertFalse(f.mutations)

    def test_resolution_refuses_source_read_failure_after_valid_native_proof(self):
        import github_plan_native as native
        for initial in ("selected", "empty"):
            for kind in ("malformed-json", "unsafe-mode", "duplicates", "missing-transcript",
                         "normalization-error", "unknown-user", "ambiguous-unrelated", "disappeared"):
                with self.subTest(initial=initial, kind=kind), ReceiptFixture() as f, ExitStack() as stack:
                    self.pending(f)
                    if initial == "empty":
                        self.source_artifact(f, initial)
                    with CLOSURE.ownership(f.session):
                        proof = asyncio.run(native.prove_end(f.entry))
                        if kind == "disappeared":
                            read = self.interleave_source_records(f, stack, kind)
                        else:
                            self.source_artifact(f, kind)
                        source = self.source_bytes(f)
                        lease = CLOSURE.lease_path(f.session).read_bytes()
                        with self.assertRaises((OSError, ValueError)):
                            native.resolve_pending(f.session, proof)
                        if kind == "disappeared":
                            self.assertTrue(read["changed"])
                        self.assertTrue(CLOSURE.action_pending(f.session))
                        self.assertEqual(CLOSURE.lease_path(f.session).read_bytes(), lease)
                        self.assertEqual(self.source_bytes(f), source)
                        self.assertFalse(f.mutations)

    def test_resumed_source_keeps_its_saved_generation_and_refuses_unknown_metadata(self):
        import github_plan_native as native
        for corrupt in (False, True):
            for mode in ("apply", "resolve"):
                with self.subTest(corrupt=corrupt, mode=mode), ReceiptFixture() as f:
                    generation = {"codex_home": str(f.home), "launch_pid": 456,
                                  "launch_generation": "resumed-generation",
                                  "launch_started_at": f.entry["launch_started_at"]}
                    with CLOSURE.resume_guard(f.session, method="thread/resume", home=f.home):
                        CLOSURE.resumed_launch(f.session, generation, str(f.transcript), "resumed-tty")
                    f.native_state = "notLoaded"
                    if corrupt:
                        self.source_artifact(f, "malformed-json")
                    source = self.source_bytes(f)
                    lease = CLOSURE.lease_path(f.session).read_bytes()
                    if mode == "resolve":
                        with CLOSURE.ownership(f.session):
                            if corrupt:
                                with self.assertRaises((OSError, ValueError)):
                                    proof = asyncio.run(native.prove_end(f.entry))
                                    native.resolve_pending(f.session, proof)
                            else:
                                proof = asyncio.run(native.prove_end(f.entry))
                                self.assertEqual(proof["snapshot"]["process"].effective["launch_generation"],
                                                 generation["launch_generation"])
                                native.resolve_pending(f.session, proof)
                    else:
                        code, report = self.finish(f, apply=True)
                        self.assertEqual(code, 1)
                        self.assertFalse(report["ok"])
                    refuses = mode == "apply" or corrupt
                    self.assertEqual(CLOSURE.action_pending(f.session), refuses)
                    if refuses:
                        self.assertEqual(CLOSURE.lease_path(f.session).read_bytes(), lease)
                    self.assertEqual(self.source_bytes(f), source)
                    self.assertFalse(f.mutations)

    def interleave_source_records(self, f, stack, kind):
        real = CLOSURE.log_rows
        state = {"changed": False, "inside": False, "serial": 0}
        def other_closure():
            sid = "other-source-" + str(state["serial"])
            path = f.home / "sessions" / ("rollout-" + sid + ".jsonl")
            rows = f.release.session_record.read_jsonl(f.transcript)
            rows[0]["payload"]["id"] = sid
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            entry = {**f.entry, "session_id": sid, "transcript": str(path),
                     "pid": 124, "launch_pid": 124, "tty": "other-tty", "launch_generation": sid}
            f.release.record_closure(f.log, entry, "supervisor")
        def read():
            if state["inside"] or state["changed"] and kind != "continuous":
                return real()
            state["inside"] = True
            try:
                if kind in {"malformed", "disappeared"}:
                    before = f.log.read_bytes()
                    try:
                        if kind == "disappeared":
                            f.log.unlink()
                        else:
                            f.log.write_text("{")
                        state["changed"] = True
                        return real()
                    finally:
                        f.log.write_bytes(before)
                        f.log.chmod(0o600)
                rows = real()
                if kind == "selected":
                    metadata = {"codex_home": str(f.home), "launch_pid": 456,
                                "launch_generation": "later-generation", "launch_started_at": f.entry["launch_started_at"]}
                    CLOSURE.resumed_launch(f.session, metadata, str(f.transcript), "resumed-tty")
                else:
                    state["serial"] += 1
                    # This independent writer owns only its unrelated source.
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pool.submit(other_closure).result(3)
                state["changed"] = True
                return rows
            finally:
                state["inside"] = False
        stack.enter_context(patch.object(CLOSURE, "log_rows", side_effect=read))
        return state

    def run_source_records_interleaving(self, f, stack, kind, mode):
        state = {}
        def install(_f=None):
            state["read"] = self.interleave_source_records(f, stack, kind)
        if mode == "preview":
            code, report = self.finish(f, history=install)
            accepted = code == 0 and report["ok"]
        else:
            import github_plan_native as native
            with CLOSURE.ownership(f.session):
                proof = asyncio.run(native.prove_end(f.entry))
                install()
                try:
                    native.resolve_pending(f.session, proof)
                except ValueError:
                    accepted = False
                else:
                    accepted = True
        self.assertTrue(state["read"]["changed"])
        self.assertFalse(f.mutations)
        return accepted

    def test_unrelated_closure_publication_during_read_preserves_selected_source_authority(self):
        for mode in ("preview", "resolve"):
            with self.subTest(mode=mode), ReceiptFixture() as f, ExitStack() as stack:
                self.pending(f)
                before = CLOSURE.source_records({f.session})[f.session]
                lease = CLOSURE.lease_path(f.session).read_bytes()
                self.assertTrue(self.run_source_records_interleaving(f, stack, "unrelated", mode))
                stack.close()
                self.assertEqual(CLOSURE.source_records({f.session})[f.session], before)
                self.assertEqual(CLOSURE.action_pending(f.session), mode == "preview")
                if mode == "preview":
                    self.assertEqual(CLOSURE.lease_path(f.session).read_bytes(), lease)

    def test_unstable_or_malformed_shared_reads_remain_unknown(self):
        for kind in ("continuous", "malformed"):
            for mode in ("preview", "resolve"):
                with self.subTest(kind=kind, mode=mode), ReceiptFixture() as f, ExitStack() as stack:
                    self.pending(f)
                    before = CLOSURE.source_records({f.session})[f.session]
                    lease = CLOSURE.lease_path(f.session).read_bytes()
                    self.assertFalse(self.run_source_records_interleaving(f, stack, kind, mode))
                    stack.close()
                    self.assertEqual(CLOSURE.source_records({f.session})[f.session], before)
                    self.assertTrue(CLOSURE.action_pending(f.session))
                    self.assertEqual(CLOSURE.lease_path(f.session).read_bytes(), lease)

    def test_selected_generation_change_during_read_never_borrows_the_new_source(self):
        for mode in ("preview", "resolve"):
            with self.subTest(mode=mode), ReceiptFixture() as f, ExitStack() as stack:
                self.pending(f)
                before = CLOSURE.source_records({f.session})[f.session]
                lease = CLOSURE.lease_path(f.session).read_bytes()
                self.assertFalse(self.run_source_records_interleaving(f, stack, "selected", mode))
                stack.close()
                self.assertNotEqual(CLOSURE.source_records({f.session})[f.session], before)
                self.assertTrue(CLOSURE.action_pending(f.session))
                self.assertEqual(CLOSURE.lease_path(f.session).read_bytes(), lease)


class IdleReceiptIntegrationTests(unittest.TestCase):
    def test_source_without_closed_row_is_frozen_and_action_clear_preserves_freeze(self):
        with ReceiptFixture() as f:
            CLOSURE.replace_rows([])
            with CLOSURE.ownership(f.session):
                CLOSURE.set_action_pending(f.session, True)
                CLOSURE.admit_sources({f.session})
                CLOSURE.set_action_pending(f.session, False)
            with self.assertRaises(ValueError):
                with CLOSURE.resume_guard(f.session):
                    self.fail("frozen source resumed")

    def test_native_pending_queue_cannot_be_erased_by_an_old_safe_turn(self):
        with ReceiptFixture() as f:
            f.native_state = "notLoaded"
            f.queue_items = [{"id": "queued", "clientUserMessageId": "client", "input": [
                {"type": "text", "text": "synthetic pending work"}]}]
            with CLOSURE.resume_guard(f.session):
                pass
            with self.assertRaises(ValueError):
                f.release.record_closure(f.log, f.entry, "supervisor")
            self.assertTrue(CLOSURE.action_pending(f.session))

    def test_action_category_is_sticky(self):
        with ReceiptFixture() as f:
            with CLOSURE.ownership(f.session):
                CLOSURE.set_action_pending(f.session, True, method="thread/realtime/start")
                floor = CLOSURE.action_time(f.session)
                CLOSURE.set_action_pending(f.session, True, method="turn/start")
                self.assertEqual(CLOSURE.action_time(f.session), floor)

    def test_short_mutation_contention_waits_without_failing_the_second_launch(self):
        import fcntl
        with ReceiptFixture() as f, ThreadPoolExecutor(max_workers=1) as pool:
            held, release = threading.Event(), threading.Event()
            def writer():
                with CLOSURE.store_lock(f.log):
                    held.set()
                    release.wait(3)
            future = pool.submit(writer)
            self.assertTrue(held.wait(3))
            original = fcntl.flock
            def contend(stream, operation):
                try:
                    return original(stream, operation)
                except BlockingIOError:
                    release.set()
                    raise
            try:
                with patch.object(fcntl, "flock", side_effect=contend):
                    CLOSURE.register_launch(["codex", "--", "task"], str(f.home))
            finally:
                release.set()
                future.result(timeout=3)
            self.assertTrue(any(r.get("launch_pid") == os.getpid() for r in CLOSURE.log_rows()))

    def test_registration_prunes_only_confirmed_dead_runtime_rows(self):
        shared = SESSIONS.process_liveness()
        native_absent = shared.process_absent
        for kind in ("managed_launch", "managed_proxy"):
            for pid in (None, 0, -1, True, "456", 456):
                row = {"record_kind": kind, "launch_pid": pid}
                for platform in ("darwin", "linux", "win32"):
                    for outcome in (None, ProcessLookupError(), PermissionError(), OSError()):
                        with self.subTest(kind=kind, pid=pid, platform=platform, outcome=type(outcome)), \
                                patch.object(shared.sys, "platform", platform), \
                                patch.object(shared.os, "kill", side_effect=outcome):
                            absent = (platform != "win32" and type(pid) is int and pid > 0
                                      and isinstance(outcome, ProcessLookupError))
                            self.assertEqual(CLOSURE.prune_registrations([row]), [] if absent else [row])
        source = {"record_kind": "closure", "launch_pid": 456}
        with patch.object(shared.os, "kill", side_effect=ProcessLookupError):
            self.assertEqual(CLOSURE.prune_registrations([source]), [source])
        with ReceiptFixture() as f:
            with subprocess.Popen([sys.executable, "-c", "pass"]) as child:
                child.wait()
            dead = {"record_kind": "managed_launch", "launch_pid": child.pid}
            with CLOSURE.store_lock(f.log):
                CLOSURE.replace_rows([*CLOSURE.log_rows(), dead])
            with patch.object(shared, "process_absent", side_effect=native_absent):
                CLOSURE.register_launch(["codex", "--", "task"], str(f.home))
            rows = CLOSURE.log_rows()
            self.assertNotIn(dead, rows)
            self.assertTrue(any(r.get("session_id") == f.session for r in rows))

    def test_guarded_launch_binding_excludes_prompt_but_keeps_option_identity(self):
        with ReceiptFixture() as f, patch.object(CLOSURE, "process_started_at", return_value=datetime.now(timezone.utc) - timedelta(seconds=2)):
            native = ["codex", "--remote", "unix://unused", "--", "private initial prompt"]
            f.managed(native)
            endpoint = str(CLOSURE.daemon_endpoint(f.home))
            self.assertEqual(CLOSURE.guarded_launch_endpoint(456, native), endpoint)
            self.assertEqual(CLOSURE.guarded_launch_endpoint(456, [*native[:-1], "different private prompt"]), endpoint)
            self.assertIsNone(CLOSURE.guarded_launch_endpoint(456, [native[0], "--model", "changed", *native[1:]]))

class AccountHomeTests(unittest.TestCase):
    def test_runtime_and_account_overrides_cannot_hide_a_frozen_source(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"HOME": folder, "CODE_HOME": str(Path(folder) / ".code")}):
            path = CLOSURE.closed_log()
            path.parent.mkdir(parents=True)
            CLOSURE.replace_rows([{"session_id": "handed-off", "closure_evidence": {}}])
            with CLOSURE.ownership("handed-off", create=True):
                CLOSURE.admit_sources({"handed-off"})
            with patch.dict(os.environ, {"CODE_HOME": str(Path(folder) / "other-runtime"), "CODEX_HOME": str(Path(folder) / "other-account")}):
                with self.assertRaises(ValueError):
                    with CLOSURE.resume_guard("handed-off"):
                        self.fail("a divergent runtime bypassed predecessor freeze")

    def test_receipt_account_inventory_ignores_runtime_and_account_configs(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"HOME": folder}):
            root = Path(folder)
            canonical_home = root / "canonical-account"
            configs = [(CLOSURE.closed_log().with_name("supervisor.toml"), canonical_home),
                       (root / "other-runtime/skill-data/supervisor.toml", root / "runtime-account"),
                       (root / "other-account/skill-data/supervisor.toml", root / "account-local")]
            for path, home in configs:
                path.parent.mkdir(parents=True)
                path.write_text('[[accounts.account]]\nname="fixture"\nprovider="openai"\ncontext_panel_label="fixture"\n'
                                + 'env={CODEX_HOME=' + json.dumps(str(home)) + '}\n')
            expected = [canonical_home.resolve()]
            with patch.dict(os.environ, {"CODE_HOME": str(root / "other-runtime"), "CODEX_HOME": str(root / "other-account")}):
                self.assertEqual(SESSIONS.configured_homes(strict=True), expected)
                self.assertTrue({canonical_home.resolve(), (root / ".codex").resolve(),
                                 (root / "other-account").resolve()} <= set(SESSIONS.observation_homes(strict=True)))

    def test_configured_receipt_homes_do_not_depend_on_caller_account_or_order(self):
        config = {"accounts": [{"provider": "openai", "env": {"CODEX_HOME": "/fixture/b"}},
                               {"provider": "openai", "env": {"CODEX_HOME": "/fixture/a"}}]}
        helpers = SimpleNamespace(load_config=lambda **_kw: config)
        results = []
        for current in ("/fixture/a", "/fixture/b", "/fixture/unconfigured"):
            with patch.dict(os.environ, {"CODEX_HOME": current}), \
                    patch.object(SESSIONS, "native_helpers", return_value=(helpers, None)):
                results.append(SESSIONS.configured_homes(strict=True))
        self.assertTrue(all(homes == results[0] for homes in results))
        self.assertEqual(set(results[0]), {Path(a["env"]["CODEX_HOME"]).resolve() for a in config["accounts"]})
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"HOME": folder, "CODEX_HOME": "/fixture/a"}), \
                patch.object(SESSIONS, "native_helpers", return_value=(helpers, None)):
            self.assertIn((Path(folder) / ".codex").resolve(), SESSIONS.observation_homes(strict=True))

    def test_receipt_proofs_require_available_account_inventory(self):
        config = SimpleNamespace(load_config=lambda **_kw: (_ for _ in ()).throw(ValueError("missing account inventory")))
        with patch.object(SESSIONS, "native_helpers", return_value=(config, None)):
            self.assertTrue(SESSIONS.observation_homes())
            with self.assertRaisesRegex(ValueError, "missing account inventory"):
                SESSIONS.observation_homes(strict=True)

    def test_shared_log_is_independent_of_provider_account_home(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"HOME": folder}):
            first = CLOSURE.closed_log()
            with patch.dict(os.environ, {"CODE_HOME": str(Path(folder) / "other-runtime"), "CODEX_HOME": str(Path(folder) / "other-account")}):
                self.assertEqual(CLOSURE.closed_log(), first)
            self.assertTrue(first.is_relative_to(folder))
            self.assertFalse(first.exists())

if __name__ == "__main__":
    unittest.main()
