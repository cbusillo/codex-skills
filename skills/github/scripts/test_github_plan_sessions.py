# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Fake native daemon evidence, including refusal and read-only transport."""
import copy
import asyncio
from contextlib import ExitStack, redirect_stdout
import io
import inspect
import json
import os
import socket
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
        self.stack.enter_context(patch.dict(os.environ, {"HOME": str(self.root), "CODE_HOME": str(self.log.parent.parent)}))
        self.stack.enter_context(patch.object(CLOSURE, "closed_log", return_value=self.log))
        self.stack.enter_context(patch.object(CLOSURE, "host_identity", return_value={"boot": "fixture-boot", "uid": os.getuid()}))
        self.stack.enter_context(patch.object(SESSIONS, "codex_homes", side_effect=lambda **_kw: sorted(self.homes)))
        SESSIONS.native_helpers()
        self.stack.enter_context(patch.object(SESSIONS.process_liveness(), "process_arguments",
            side_effect=lambda _pid, command: (SESSIONS.process_liveness().harness_arguments(command), True)))
        import release_session_claims
        self.release = release_session_claims
        self.stack.enter_context(patch.object(self.release, "inventory", side_effect=lambda: list(self.process_rows)))
        self.entry = {"session_id": self.session, "harness": "codex", "transcript": str(self.transcript),
                      "pid": 123, "tty": "fixture-tty", "supervisor_owned": True, "codex_home": str(self.home),
                      "launch_pid": 123, "launch_generation": "source-generation", "launch_started_at": "2026-10-01T01:00:00Z"}
        self.release.record_closure(self.log, self.entry, "supervisor")
        real_run = SESSIONS.subprocess.run
        self.stack.enter_context(patch.object(SESSIONS.subprocess, "run", side_effect=lambda argv, **kw:
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

    def observe(self, proof_sessions=None):
        return SESSIONS.observe({self.session}, set() if proof_sessions is None else proof_sessions)

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
                        closed = CLOSURE.verified_record(f.session)
                        self.assertIsNotNone(closed)
                        self.assertEqual(closed["session_id"], f.session)
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

    def test_observation_keeps_unknown_cleared_resumed_source_live_without_retaining_lease(self):
        import github_plan_native as native
        for moved, queued in ((False, False), (True, True)):
            for kind in (None, "unsafe-mode", "malformed-json", "duplicates", "unknown-user"):
                with self.subTest(moved=moved, queued=queued, kind=kind), ReceiptFixture() as f:
                    home = f.root / "retained-account" if moved else f.home
                    transcript = f.add_home(home) if moved else f.transcript
                    metadata = {"codex_home": str(home), "launch_pid": 456,
                                "launch_generation": "actual-resume",
                                "launch_started_at": f.entry["launch_started_at"]}
                    with CLOSURE.resume_guard(f.session, method="thread/resume", home=home):
                        CLOSURE.resumed_launch(f.session, metadata, str(transcript), "resumed-tty")
                    f.native_state = "notLoaded"
                    current = {**f.entry, **metadata, "pid": metadata["launch_pid"],
                               "tty": "resumed-tty", "transcript": str(transcript)}
                    with CLOSURE.ownership(f.session):
                        native.resolve_pending(f.session, asyncio.run(native.prove_end(current)))
                    self.assertFalse(CLOSURE.action_pending(f.session))
                    if moved:
                        f.homes = [f.home]
                    if queued:
                        f.home_queues[str(home)] = [{"id": "queued", "clientUserMessageId": "client",
                            "input": [{"type": "text", "text": "queued after prior resolution"}]}]
                    if kind is not None:
                        self.source_artifact(f, kind)
                        with self.assertRaises(ValueError) as failed_source:
                            CLOSURE.source_records({f.session})
                        source_error = str(failed_source.exception)
                    source = self.source_bytes(f)
                    lease = CLOSURE.lease_path(f.session).read_bytes()
                    f.home_requests.clear()
                    def available():
                        try:
                            with CLOSURE.ownership(f.session) as acquired:
                                return acquired
                        except CLOSURE.LeaseBusy:
                            return False
                    with CLOSURE.admission(), ThreadPoolExecutor(max_workers=1) as pool:
                        outer_observation = CLOSURE._OBSERVATION.get()
                        peers, states, coverage = f.observe()
                        self.assertIs(CLOSURE._OBSERVATION.get(), outer_observation)
                        available_after_observe = pool.submit(available).result(2)
                        self.assertEqual(coverage["process_coverage"], "complete")
                        self.assertIn((str(f.home), "thread/queue/list"), f.home_requests)
                        if kind is not None:
                            self.assertEqual(states[f.session]["state"], "live")
                            self.assertEqual(states[f.session]["error"], source_error)
                            self.assertTrue(any(peer.get("sessionId") == f.session
                                                and peer.get("error") == source_error for peer in peers))
                        else:
                            self.assertEqual(states[f.session]["state"], "live" if queued else "ended")
                            self.assertIn((str(home), "thread/queue/list"), f.home_requests)
                        self.assertEqual(available_after_observe, kind is not None or queued)
                    self.assertFalse(CLOSURE.action_pending(f.session))
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
    def recovery_history(self, fixture, *, delegated):
        reader = SESSIONS.native_helpers()[1]
        records = reader.read_jsonl(fixture.transcript)
        receipts = reader.claim_receipts
        command = "uv run /catalog/gh-plan.py --repo example/task claim 1"
        invocation = {"type": "function_call", "name": "exec_command", "call_id": "claim",
                      "arguments": json.dumps({"cmd": command})}
        result = {"operation": receipts.CLAIM_OPERATION, "ok": True,
                  "claim": {"session": fixture.session},
                  "claim_comment": {"url": "https://github.com/example/task/issues/1#issuecomment-10"}}
        if delegated:
            invocation.update(name="collaboration.spawn_agent", arguments=json.dumps({"task_name": "read-only"}))
            result = {"task_name": "/root/read-only"}
        proof = {"operation": receipts.CHECK_OPERATION, "ok": True, "claims_clear": True,
                 "session_id": fixture.session, "harness": "codex", "coverage": {"complete": True},
                 "released_claims": [{"repository": "example/task", "record": 1, "claim_comment": 10}]}
        activity = [invocation, {"type": "function_call_output", "call_id": "claim", "output": json.dumps(result)},
                    {"type": "function_call", "name": "exec_command", "call_id": "check",
                     "arguments": json.dumps({"cmd": "uv run /catalog/check_claims.py"})},
                    {"type": "function_call_output", "call_id": "check", "output": json.dumps(proof)}]
        records[1:1] = [{"type": "response_item", "timestamp": "2026-10-01T01:59:59Z", "payload": item}
                        for item in activity]
        fixture.transcript.write_text("".join(json.dumps(row) + "\n" for row in records))
        return records

    def test_post_action_recovery_accepts_fresh_declaration_without_pre_exit_identity_or_children(self):
        for delegated in (False, True):
            with self.subTest(delegated=delegated), ReceiptFixture() as f:
                records = self.recovery_history(f, delegated=delegated)
                after = datetime(2026, 10, 1, 1, 59, 58, tzinfo=timezone.utc)
                with patch.object(CLOSURE, "action_time", return_value=after):
                    # The fresh tail omits session_meta and cannot independently
                    # establish live closeout acceptance or delegated histories.
                    tail = records[1:]
                    status = SESSIONS.native_helpers()[1].summarize(tail, "codex")
                    self.assertFalse(status["safe_verdict"])
                    self.assertTrue(status["declared_safe_verdict"])
                    closed = f.release.record_closure(f.log, f.entry, "supervisor")
                    self.assertTrue(closed["process_exit_verified"])
                    f.process_rows = [(f.entry["pid"], f.entry["tty"], "codex worker")]
                    with self.assertRaises(ValueError):
                        f.release.record_closure(f.log, f.entry, "supervisor")
                with patch.object(CLOSURE, "action_time", return_value=datetime(2026, 10, 1, 2, 0, 2, tzinfo=timezone.utc)):
                    with self.assertRaises(ValueError):
                        CLOSURE.require_fresh_action_verdict(f.entry)

    def test_idle_successor_proof_accepts_original_declaration_without_pre_exit_claim_check(self):
        for delegated in (False, True):
            with self.subTest(delegated=delegated), ReceiptFixture() as f:
                records = self.recovery_history(f, delegated=delegated)
                if not delegated:
                    records = [r for r in records if (r.get("payload") or {}).get("call_id") != "check"]
                    f.transcript.write_text("".join(json.dumps(row) + "\n" for row in records))
                status = SESSIONS.native_helpers()[1].summarize(records, "codex")
                self.assertFalse(status["safe_verdict"])
                self.assertTrue(status["declared_safe_verdict"])
                f.log.unlink()
                closed = f.release.record_closure(f.log, f.entry, "supervisor")
                self.assertIn("closure_evidence", closed)
                self.assert_ended(f)
                with f.transcript.open("a") as stream:
                    stream.write(json.dumps({"type": "event_msg", "timestamp": "2026-10-01T02:00:02Z",
                                             "payload": {"type": "task_started"}}) + "\n")
                self.assert_live(f)

    def test_owned_recovery_ignores_absent_thread_in_unassociated_daemon(self):
        with ReceiptFixture() as f:
            other = f.root / "unassociated"
            f.add_home(other, copy_transcript=False)
            f.rpc_results[(str(other), "thread/loaded/list")] = {"data": [], "nextCursor": None}
            f.rpc_results[(str(other), "thread/read")] = ValueError("thread not found")
            with CLOSURE.resume_guard(f.session, method="thread/resume", home=f.home): pass
            f.native_state = "notLoaded"
            before = CLOSURE.source_records({f.session})[f.session]
            with CLOSURE.admission():
                _, states, coverage = f.observe()
                self.assertTrue(coverage["peer_complete"])
                self.assertEqual(states[f.session]["state"], "ended")
            self.assertFalse(list(other.rglob("*.jsonl")))
            self.assertIn((str(other), "thread/read"), f.home_requests)
            self.assertFalse(CLOSURE.action_pending(f.session))
            self.assertEqual(CLOSURE.source_records({f.session})[f.session], before)

    def test_ordinary_observation_authenticates_initialize_home_despite_shared_transcripts(self):
        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch), ReceiptFixture() as f:
                other = f.root / "other-account"
                copied = other / "sessions" / f.transcript.name
                copied.parent.mkdir(parents=True)
                copied.symlink_to(f.transcript)
                f.homes.append(other)
                CLOSURE.replace_rows([])
                f.release.record_closure(f.log, f.entry, "supervisor")
                reported_home = other if mismatch else f.root / "canonical-home-alias"
                if not mismatch: reported_home.symlink_to(f.home, target_is_directory=True)
                f.rpc_results["initialize"] = {"codexHome": str(reported_home)}
                _, states, coverage = f.observe()
                self.assertEqual(states.get(f.session, {}).get("state") == "ended", not mismatch)
                self.assertEqual(coverage["observed_source_incomplete"], mismatch)
                self.assertFalse(f.mutations)

    def test_notloaded_observation_resolves_aborted_prelaunch_without_new_generation(self):
        with ReceiptFixture() as f:
            original = CLOSURE.log_rows()[0]
            with CLOSURE.resume_guard(f.session):
                pass
            invalidated = CLOSURE.log_rows()[0]
            self.assertIsNone(invalidated.get("resumed_generation"))
            f.native_state = "notLoaded"
            peers, states, _coverage = f.observe({f.session})
            self.assertEqual(states.get(f.session, {}).get("state"), "ended")
            self.assertFalse(peers)
            self.assertFalse(CLOSURE.action_pending(f.session))
            self.assertEqual(CLOSURE.log_rows()[0], invalidated)
            closed = f.release.record_closure(f.log, f.entry, "supervisor")
            self.assertEqual(closed["ended_at"], original["ended_at"])
            self.assertFalse(f.mutations)

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

    def test_action_category_is_sticky_and_admission_requires_all_leases_first(self):
        with ReceiptFixture() as f:
            with CLOSURE.ownership(f.session):
                CLOSURE.set_action_pending(f.session, True, method="thread/realtime/start")
                floor = CLOSURE.action_time(f.session)
                CLOSURE.set_action_pending(f.session, True, method="turn/start")
                self.assertEqual(CLOSURE.action_time(f.session), floor)
                before = CLOSURE.lease_path(f.session).read_bytes()
                with self.assertRaises(ValueError):
                    CLOSURE.admit_sources({f.session, "missing-source"})
                self.assertEqual(CLOSURE.lease_path(f.session).read_bytes(), before)

    def test_pending_resolution_reuses_native_cutoff_and_retains_guarded_source_for_admission(self):
        with ReceiptFixture() as f:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
            rows = CLOSURE.log_rows()
            rows[0]["tty_exited_at"] = cutoff.isoformat()
            CLOSURE.replace_rows(rows)
            with CLOSURE.resume_guard(f.session): pass
            f.native_state = "notLoaded"
            native = ["codex", "--remote", "unix://" + str(CLOSURE.daemon_endpoint(f.home)), "--", "new task"]
            def available():
                try:
                    with CLOSURE.ownership(f.session): return True
                except CLOSURE.LeaseBusy: return False
            with patch.object(CLOSURE, "process_started_at", return_value=cutoff + timedelta(seconds=1)):
                f.managed(native)
                f.commands = "456 " + " ".join(native)
                f.process_rows = [(456, "fixture-tty", " ".join(native))]
                with CLOSURE.admission(), ThreadPoolExecutor(max_workers=1) as pool:
                    self.assertEqual(f.observe()[1][f.session]["state"], "ended")
                    self.assertFalse(pool.submit(available).result(2))
                    CLOSURE.admit_sources({f.session})
            with self.assertRaises(ValueError):
                with CLOSURE.resume_guard(f.session): pass

    def test_failed_or_globally_incomplete_pending_observation_releases_only_new_guard(self):
        for failure in ("native-work", "global-process"):
            with self.subTest(failure=failure), ReceiptFixture() as f:
                with CLOSURE.resume_guard(f.session, method="turn/start", home=f.home): pass
                if failure == "native-work":
                    f.rpc_results["thread/goal/get"] = {"goal": {"threadId": f.session, "status": "active"}}
                else:
                    f.native_state = "notLoaded"
                    f.commands = "456 codex resume"
                def available(session):
                    try:
                        with CLOSURE.ownership(session, create=True): return True
                    except CLOSURE.LeaseBusy: return False
                with CLOSURE.ownership("preowned", create=True), CLOSURE.admission(), ThreadPoolExecutor(max_workers=1) as pool:
                    self.assertNotEqual(f.observe()[1].get(f.session, {}).get("state"), "ended")
                    self.assertTrue(pool.submit(available, f.session).result(2))
                    self.assertFalse(pool.submit(available, "preowned").result(2))

    def test_observation_borrows_only_the_live_direct_guard_lifetime(self):
        with ReceiptFixture() as f, ThreadPoolExecutor(max_workers=1) as pool:
            f.native_state = "notLoaded"
            with CLOSURE.resume_guard(f.session, method="thread/resume", home=f.home): pass
            held, release = threading.Event(), threading.Event()
            def foreign_owner():
                with CLOSURE.ownership(f.session) as acquired:
                    held.set()
                    release.wait(3)
                    return acquired
            with CLOSURE.admission():
                with CLOSURE.ownership(f.session):
                    self.assertEqual(f.observe()[1][f.session]["state"], "ended")
                    self.assertEqual(f.observe()[1][f.session]["state"], "ended")
                future = pool.submit(foreign_owner)
                try:
                    self.assertTrue(held.wait(2))
                    self.assertEqual(f.observe()[1][f.session]["state"], "live")
                finally:
                    release.set()
                self.assertTrue(future.result(2))

    def test_recovering_moved_account_observes_original_unconfigured_daemon(self):
        with ReceiptFixture() as f:
            resumed_home = f.root / "resumed-account"
            resumed_transcript = f.add_home(resumed_home)
            metadata = {"codex_home": str(resumed_home), "launch_pid": 456,
                        "launch_generation": "moved-account", "launch_started_at": f.entry["launch_started_at"]}
            with CLOSURE.resume_guard(f.session, method="thread/resume", home=resumed_home):
                CLOSURE.resumed_launch(f.session, metadata, str(resumed_transcript), "resumed-tty")
            before = CLOSURE.source_records({f.session})[f.session]
            f.native_state = "notLoaded"
            f.commands = "789 codex app-server --listen unix://" + str(CLOSURE.daemon_endpoint(f.home))
            with patch.object(SESSIONS, "codex_homes", return_value=[resumed_home]), CLOSURE.admission():
                for _ in range(2):
                    _, states, coverage = f.observe()
                    self.assertEqual(coverage["process_coverage"], "complete")
                    self.assertEqual(coverage["source_count"], 2)
                    self.assertEqual(states[f.session]["state"], "ended")
            self.assertEqual(CLOSURE.source_records({f.session})[f.session], before)

    def test_bad_recovery_home_does_not_hide_the_other_known_home(self):
        for broken in ("original", "generation"):
            with self.subTest(broken=broken), ReceiptFixture() as f:
                resumed_home = f.root / "resumed-account"
                resumed_transcript = f.add_home(resumed_home)
                metadata = {"codex_home": str(resumed_home), "launch_pid": 456,
                            "launch_generation": "moved-account", "launch_started_at": f.entry["launch_started_at"]}
                with CLOSURE.resume_guard(f.session, method="thread/resume", home=resumed_home):
                    CLOSURE.resumed_launch(f.session, metadata, str(resumed_transcript), "resumed-tty")
                rows = CLOSURE.log_rows()
                if broken == "original": rows[0]["codex_home"] = ["invalid"]
                else: rows[0]["resumed_generation"]["codex_home"] = ["invalid"]
                CLOSURE.replace_rows(rows)
                good_home = resumed_home if broken == "original" else f.home
                f.native_state = "active"
                with patch.object(SESSIONS, "codex_homes", return_value=[]):
                    peers, states, _ = f.observe()
                self.assertTrue(any(peer.get("home") == str(good_home) for peer in peers))
                self.assertEqual(states[f.session]["state"], "live")
                self.assertTrue(CLOSURE.action_pending(f.session))

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
        with ReceiptFixture() as f:
            with SESSIONS.subprocess.Popen([sys.executable, "-c", "pass"]) as child:
                child.wait()
            dead = {"record_kind": "managed_launch", "launch_pid": child.pid}
            with CLOSURE.store_lock(f.log):
                CLOSURE.replace_rows([*CLOSURE.log_rows(), dead])
            CLOSURE.register_launch(["codex", "--", "task"], str(f.home))
            rows = CLOSURE.log_rows()
            self.assertNotIn(dead, rows)
            self.assertTrue(any(r.get("session_id") == f.session for r in rows))

    def test_registration_without_guarded_attachment_is_not_process_coverage(self):
        with ReceiptFixture() as f:
            native = ["codex", "--remote", "unix://" + str(CLOSURE.daemon_endpoint(f.home)), "--", "task"]
            born = datetime(2026, 10, 1, tzinfo=timezone.utc)
            with patch.object(CLOSURE.os, "getpid", return_value=456), patch.object(CLOSURE, "process_started_at", return_value=born):
                CLOSURE.register_launch(native, str(f.home))
                f.commands = "456 " + " ".join(native)
                self.assert_live(f)

    def test_managed_unrelated_interactive_client_has_exact_process_birth_and_observed_home(self):
        with ReceiptFixture() as f:
            native = ["codex", "--remote", "unix://" + str(CLOSURE.daemon_endpoint(f.home)), "--", "unrelated task"]
            born = datetime(2026, 10, 1, tzinfo=timezone.utc)
            with patch.object(CLOSURE, "process_started_at", return_value=born):
                f.managed(native)
                endpoint = str(CLOSURE.daemon_endpoint(f.home))
                self.assertEqual(CLOSURE.guarded_launch_endpoint(456, native), endpoint)
                self.assertFalse(CLOSURE.managed_launch(456, native, set()))
                self.assertTrue(CLOSURE.managed_launch(456, native, {endpoint}))
                f.commands = "456 " + " ".join(native)
                self.assert_ended(f)
                f.commands = "456 python3 /fixture/codex " + " ".join(native[1:])
                self.assert_ended(f)
                f.release.record_closure(f.log, f.entry, "supervisor")
                self.assert_ended(f)
                with patch.object(CLOSURE, "process_started_at", return_value=datetime(2026, 10, 2, tzinfo=timezone.utc)):
                    self.assert_live(f)
                f.commands = "456 codex --remote unix:///changed -- task"
                self.assert_live(f)
            self.assertNotIn("unrelated task", f.log.read_text())

    def assert_ended(self, fixture):
        peers, states, _ = fixture.observe()
        self.assertFalse(peers)
        self.assertEqual(states[fixture.session]["state"], "ended")

    def assert_live(self, fixture):
        peers, states, _ = fixture.observe()
        self.assertTrue(any(peer["sessionId"] == fixture.session for peer in peers))
        self.assertEqual(states[fixture.session]["state"], "live")

    def test_real_idle_receipt_allows_fresh_exec_but_refuses_unknown_clients(self):
        with ReceiptFixture() as f:
            f.commands = "codex exec fresh-unrelated-work"
            self.assert_ended(f)
            for command in ("codex --remote unix:///unobserved", "codex -C /fixture/gui -- GUI task", "codex resume --last", "codex resume", "codex app-server"):
                f.commands = command
                self.assert_live(f)

    def test_active_process_pid_reuse_and_surviving_worker_defeat_idle_receipt(self):
        with ReceiptFixture() as f:
            self.assert_ended(f)
            f.native_state = "active"
            self.assert_live(f)
            f.native_state = "idle"
            for rows in ([(123, "new-tty", "unrelated-reused-pid")], [(456, "fixture-tty", "surviving-worker")]):
                f.process_rows = rows
                with patch.object(CLOSURE, "process_started_at", return_value=datetime(2026, 10, 1, tzinfo=timezone.utc)):
                    self.assert_live(f)

    def test_running_native_execution_or_incomplete_inventory_keeps_source_live(self):
        with ReceiptFixture() as f:
            self.assert_ended(f)
            f.execution_items = [{"type": "commandExecution", "status": "inProgress", "processId": "worker", "exitCode": None}]
            self.assert_live(f)
            f.execution_items = []
            f.incomplete = True
            self.assert_live(f)

    def test_new_tty_occupant_after_closure_does_not_own_the_old_generation(self):
        with ReceiptFixture() as f:
            ended = datetime.now(timezone.utc) - timedelta(minutes=1)
            rows = json.loads(f.log.read_text())
            rows[0]["ended_at"] = ended.isoformat()
            f.log.write_text(json.dumps(rows))
            self.assert_ended(f)
            f.process_rows = [(456, "fixture-tty", "new-unrelated-worker")]
            with patch.object(CLOSURE, "process_started_at", return_value=ended + timedelta(seconds=1)):
                self.assert_ended(f)
            with patch.object(CLOSURE, "process_started_at", return_value=datetime.now(timezone.utc) + timedelta(days=1)):
                self.assert_live(f)

            native = ["codex", "--remote", "unix://" + str(CLOSURE.daemon_endpoint(f.home)), "--", "new task"]
            with patch.object(CLOSURE, "process_started_at", return_value=ended + timedelta(seconds=1)):
                f.managed(native)
                f.commands = "456 " + " ".join(native)
                f.process_rows = [(456, "fixture-tty", " ".join(native))]
                self.assert_ended(f)
                closed = f.release.record_closure(f.log, f.entry, "supervisor")
                self.assertTrue(f.release.validate_closure({
                    **closed, "handoff_url": "https://github.com/test/repo/issues/1#issuecomment-2"
                }, "supervisor")["safe_verdict"])
                with self.assertRaises(ValueError):
                    f.release.validate_closure({**closed, "ended_at": datetime.now(timezone.utc).isoformat()}, "supervisor")
                # A changed argv cannot borrow the guarded client's identity.
                f.process_rows = [(456, "fixture-tty", "codex --remote unix:///unverified")]
                self.assert_live(f)
                f.process_rows = [(456, "fixture-tty", " ".join(native))]
                # A pending action defeats the stale closure even before any
                # transcript or shared receipt update becomes visible.
                with CLOSURE.ownership(f.session):
                    CLOSURE.set_action_pending(f.session, True)
                    with self.assertRaises(ValueError):
                        f.release.validate_closure(closed, "supervisor")
                    CLOSURE.set_action_pending(f.session, False)
                self.assertTrue(f.release.validate_closure(closed, "supervisor")["safe_verdict"])
                # An actual guarded resume invalidates that same saved entry.
                with CLOSURE.resume_guard(f.session):
                    pass
                with self.assertRaises(ValueError):
                    f.release.validate_closure(closed, "supervisor")

    def test_observation_normalizes_saved_paths_without_rewriting_or_weakening_receipt(self):
        for spelling in ("absolute_alias", "relative"):
            with self.subTest(spelling=spelling), ReceiptFixture() as f:
                ended = datetime.now(timezone.utc) - timedelta(minutes=1)
                rows = CLOSURE.log_rows()
                source = next(row for row in rows if row.get("session_id") == f.session)
                source["ended_at"] = ended.isoformat()
                source["tty"] = "/dev/fixture-tty"
                alias = f.root / "aliased-account"
                alias.symlink_to(f.home, target_is_directory=True)
                source["transcript"] = (str(alias / "sessions" / f.transcript.name) if spelling == "absolute_alias"
                                        else os.path.relpath(f.transcript, f.log.parent))
                CLOSURE.replace_rows(rows)
                native = ["codex", "--remote", "unix://" + str(CLOSURE.daemon_endpoint(f.home)), "--", "new task"]
                with patch.object(CLOSURE, "process_started_at", return_value=ended + timedelta(seconds=1)):
                    f.managed(native)
                    f.commands = "456 " + " ".join(native)
                    f.process_rows = [(456, "fixture-tty", " ".join(native))]
                    saved = f.log.read_bytes()
                    transcript = f.transcript.read_bytes()
                    self.assert_ended(f)

                    verified = CLOSURE.verified_record(f.session, f.home)
                    self.assertIsNotNone(verified)
                    self.assertEqual(verified["transcript"], str(f.transcript))
                    self.assertEqual(verified["tty"], "fixture-tty")
                    self.assertTrue(CLOSURE.receipt_current(verified))
                    self.assertEqual(CLOSURE.source_records({f.session})[f.session]["transcript"], str(f.transcript))
                    self.assertEqual(f.log.read_bytes(), saved)
                    for change in ("generation", "transcript", "pending"):
                        with self.subTest(change=change):
                            if change == "generation":
                                changed = json.loads(saved)
                                next(row for row in changed if row.get("session_id") == f.session)["launch_generation"] = "changed"
                                CLOSURE.replace_rows(changed)
                            elif change == "transcript":
                                f.transcript.write_bytes(transcript + json.dumps({"type": "event_msg",
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                    "payload": {"type": "task_started"}}).encode() + b"\n")
                            else:
                                with CLOSURE.ownership(f.session):
                                    CLOSURE.set_action_pending(f.session, True)
                            before = f.log.read_bytes()
                            self.assert_live(f)
                            self.assertIsNone(CLOSURE.verified_record(f.session, f.home))
                            self.assertEqual(f.log.read_bytes(), before)
                            f.log.write_bytes(saved)
                            f.transcript.write_bytes(transcript)
                            with CLOSURE.ownership(f.session):
                                CLOSURE.set_action_pending(f.session, False)
                    self.assertFalse(f.mutations)

    def test_guarded_launch_binding_excludes_prompt_but_keeps_option_identity(self):
        with ReceiptFixture() as f, patch.object(CLOSURE, "process_started_at", return_value=datetime.now(timezone.utc) - timedelta(seconds=2)):
            native = ["codex", "--remote", "unix://unused", "--", "private initial prompt"]
            f.managed(native)
            endpoint = str(CLOSURE.daemon_endpoint(f.home))
            self.assertEqual(CLOSURE.guarded_launch_endpoint(456, native), endpoint)
            self.assertEqual(CLOSURE.guarded_launch_endpoint(456, [*native[:-1], "different private prompt"]), endpoint)
            self.assertIsNone(CLOSURE.guarded_launch_endpoint(456, [native[0], "--model", "changed", *native[1:]]))

    def test_observation_uses_saved_native_exit_before_later_closure_record_time(self):
        with ReceiptFixture() as f:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
            rows = json.loads(f.log.read_text())
            rows[0]["tty_exited_at"] = cutoff.isoformat()
            f.log.write_text(json.dumps(rows))
            f.process_rows = [(456, "fixture-tty", "new-unrelated-worker")]
            with patch.object(CLOSURE, "process_started_at", return_value=cutoff + timedelta(seconds=22)):
                self.assert_ended(f)

    def test_newer_generation_or_malformed_receipt_keeps_idle_live(self):
        with ReceiptFixture() as f:
            self.assert_ended(f)
            original = f.log.read_text()
            rows = json.loads(original)
            rows[0]["launch_generation"] = "new-generation"
            f.log.write_text(json.dumps(rows))
            self.assert_live(f)
            f.log.write_text("{malformed")
            self.assert_live(f)

    def test_cross_account_copy_and_new_transcript_activity_defeat_idle_receipt(self):
        with ReceiptFixture() as f:
            self.assert_ended(f)
            other = f.root / "other-account"
            copy = other / "archived_sessions" / f.transcript.name
            copy.parent.mkdir(parents=True)
            copy.write_bytes(f.transcript.read_bytes())
            f.homes.append(other)
            self.assert_live(f)
            f.homes.remove(other)
            with f.transcript.open("a") as stream:
                stream.write(json.dumps({"type": "event_msg", "timestamp": "2099-01-01T00:00:00Z", "payload": {"type": "task_started"}}) + "\n")
            self.assert_live(f)

    def test_missing_ownership_lease_never_creates_one_during_observation(self):
        with ReceiptFixture() as f:
            self.assert_ended(f)
            path = CLOSURE.lease_path(f.session)
            path.unlink()
            self.assert_live(f)
            self.assertFalse(path.exists())

    def test_resume_invalidates_the_receipt_before_any_transcript_write(self):
        with ReceiptFixture() as f:
            self.assert_ended(f)
            with CLOSURE.resume_guard(f.session):
                self.assert_live(f)
            self.assert_live(f)

    def test_recorded_live_worker_is_not_leased_through_claim_round_trips(self):
        with ReceiptFixture() as f:
            f.process_rows = [(f.entry["pid"], f.entry["tty"], "codex worker")]
            with CLOSURE.admission():
                self.assert_live(f)
                def own_action():
                    with CLOSURE.resume_guard(f.session):
                        pass
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(own_action).result(timeout=3)

    def test_device_prefixed_tty_blocks_exit_without_leasing_the_live_worker(self):
        with ReceiptFixture() as f:
            rows = CLOSURE.log_rows()
            row = next(r for r in rows if r.get("session_id") == f.session)
            row["tty"] = "/dev/" + f.entry["tty"]
            CLOSURE.replace_rows(rows)
            f.process_rows = [(456, f.entry["tty"], "codex worker")]
            from github_plan_release import stamp
            with patch.object(CLOSURE, "process_started_at", return_value=stamp(f.entry["launch_started_at"])):
                with self.assertRaises(ValueError):
                    f.release.verify_process_exit(row, ended_at=stamp(row["ended_at"]))
                with CLOSURE.admission():
                    self.assert_live(f)
                    def own_action():
                        with CLOSURE.resume_guard(f.session):
                            pass
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pool.submit(own_action).result(timeout=3)

    def test_resume_and_admission_share_a_lease_and_handoff_disables_old_resume(self):
        with ReceiptFixture() as f:
            with CLOSURE.admission():
                self.assert_ended(f)
                def competing_resume():
                    with CLOSURE.resume_guard(f.session): pass
                with ThreadPoolExecutor(max_workers=1) as pool:
                    with self.assertRaises(CLOSURE.LeaseBusy):
                        pool.submit(competing_resume).result(timeout=3)
                CLOSURE.admit_sources({f.session})
            with self.assertRaises(ValueError):
                with CLOSURE.resume_guard(f.session): pass

    def test_native_uuid_alias_cannot_select_another_lease_or_frozen_generation(self):
        with ReceiptFixture("01a1514c-fa7f-7be3-95ba-4d25df73d07e") as f:
            with CLOSURE.ownership(f.session):
                CLOSURE.admit_sources({f.session})
            before = f.log.read_bytes()
            leases = set(CLOSURE.lease_path(f.session).parent.iterdir())
            for alias in (f.session.upper(), f.session.replace("-", ""), "{" + f.session + "}", "urn:uuid:" + f.session):
                with self.subTest(alias=alias):
                    with self.assertRaises(ValueError):
                        CLOSURE.resume_record(alias, CLOSURE.log_rows())
                    with self.assertRaises(ValueError):
                        with CLOSURE.resume_guard(alias):
                            self.fail("an alias acquired authority over the native source")
                    self.assertEqual(set(CLOSURE.lease_path(f.session).parent.iterdir()), leases)
                    self.assertEqual(f.log.read_bytes(), before)
                    peers, states, _ = SESSIONS.observe({alias})
                    identity = next(peer for peer in peers if peer["sessionId"] == alias)
                    self.assertEqual(identity["source"], "native_identity")
                    self.assertEqual(identity["canonical_session_id"], f.session)
                    self.assertEqual(states[alias]["source"], "native_identity")
                    self.assertEqual(set(CLOSURE.lease_path(f.session).parent.iterdir()), leases)
                    self.assertEqual(f.log.read_bytes(), before)


class NativeSessionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        shared = SESSIONS.process_liveness()
        self.enterContext(patch.object(shared, "process_arguments",
            side_effect=lambda _pid, command: (shared.harness_arguments(command), True)))

    def test_native_uuid_alias_processes_report_the_requested_source_identity(self):
        source = "01a1514c-fa7f-7be3-95ba-4d25df73d07e"
        for alias in (source.upper(), source.replace("-", ""), "{" + source + "}", "urn:uuid:" + source.upper()):
            with self.subTest(alias=alias):
                peers = SESSIONS.process_sessions("456 codex resume " + alias, {source})
                self.assertEqual([peer["sessionId"] for peer in peers], [source])

    def test_repository_codex_scripts_do_not_hide_process_absence(self):
        for command in ("456 python /repo/codex_idle_watch.py", "456 sh /repo/codex-watch.sh"):
            with self.subTest(command=command):
                self.assertTrue(SESSIONS.process_absence_known(command))
                self.assertEqual(SESSIONS.process_sessions(command, {"holder"}), [])

    def test_release_binary_resume_identifies_the_source(self):
        command = "456 /opt/bin/codex-aarch64-apple-darwin exec resume holder"
        self.assertEqual([row["sessionId"] for row in SESSIONS.process_sessions(command, {"holder"})], ["holder"])
        self.assertFalse(SESSIONS.process_absence_known(command))

    def test_resume_options_preserve_exact_source_process_observation(self):
        source = "01a1514c-fa7f-7be3-95ba-4d25df73d07e"
        other = "01a14296-2820-7da0-979b-51f158dfeb7b"
        for command in (f"codex exec resume --json {source}", f"codex e resume -m model {source}",
                        f"codex resume --all {source}", f"codex resume --model {other} {source}",
                        f"codex resume --model={other} {source}"):
            with self.subTest(command=command):
                self.assertEqual([row["sessionId"] for row in SESSIONS.process_sessions(command, {source})], [source])
                self.assertFalse(SESSIONS.process_absence_known(command))
        for command in (f"codex resume --model {source} {other}", f"codex resume --model={source} {other}",
                        f"codex resume -i {source} --last", f"codex resume --all {other} -- {source}"):
            with self.subTest(command=command):
                self.assertEqual(SESSIONS.process_sessions(command, {source}), [])

    def test_resumed_cli_session_is_live_even_without_a_daemon(self):
        for command in ("/opt/bin/codex resume holder", "codex --remote unix://fixture resume holder", "node /opt/bin/codex.js resume holder", "codex exec resume holder", "codex e resume holder",
                        'codex -c notify=true resume holder'):
            self.assertEqual([r["sessionId"] for r in SESSIONS.process_sessions(command, {"holder"})], ["holder"])
        for command in ("rg resume holder", "rg codex resume holder", "codex exec -- please resume holder", "codex -- please resume holder"):
            self.assertEqual(SESSIONS.process_sessions(command, {"holder"}), [])

    def test_resume_pickers_prevent_process_absence_but_explicit_exec_prompts_do_not(self):
        for command in ("codex exec resume --last", "codex e resume --last", "node /opt/bin/codex.js e resume --last"):
            with self.subTest(command=command):
                self.assertFalse(SESSIONS.process_absence_known(command))
        for command in ("codex exec -- please resume holder", "codex e -- resume holder", "node /opt/bin/codex.js exec -- resume holder"):
            with self.subTest(command=command):
                self.assertTrue(SESSIONS.process_absence_known(command))

    def test_wrapper_absence_requires_guarded_launch_identity(self):
        for wrapper in ("codex-session.py", "codex_session_proxy.py"):
            with self.subTest(wrapper=wrapper):
                command = f"456 python /fixture/{wrapper} exec -- resume holder"
                with patch.object(CLOSURE, "managed_launch", return_value=False) as managed:
                    self.assertFalse(SESSIONS.process_absence_known(command))
                    managed.assert_called_once_with(456, [f"/fixture/{wrapper}", "exec", "--", "resume", "holder"], set())
                with patch.object(CLOSURE, "managed_launch", return_value=True):
                    self.assertTrue(SESSIONS.process_absence_known(command))
                self.assertEqual(SESSIONS.process_sessions(command, {"holder"}), [])

    async def observe(self, state, *, safe=True, after=None, changed=False, mismatch=False, with_peer=False, unsafe=False,
                      receipt=None, proof_sessions=None, receipt_changed=False, snapshot_status=None):
        thread = {"id": "holder", "cwd": "/retained/issue-42", "path": "/fixture/transcript", "historyMode": "paginated", "status": {"type": state}}
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
                if method == "initialize": result = {"codexHome": str(home)}
                elif method in {"thread/items/list", "thread/queue/list", "thread/backgroundTerminals/list", "thread/timeline/list"}: result = {"data": [], "nextCursor": None}
                elif method == "thread/turns/list": result = {"data": [{"id": "actual-turn", "status": "completed"}], "nextCursor": None}
                elif method == "thread/goal/get": result = {"goal": None}
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
        def status(_entry, *, include_status=False):
            nonlocal calls
            calls += 1
            if with_peer: self.assertIn("peer", observed_ids)
            result = {"/fixture/transcript": {**(snapshot_status or {}), "safe_verdict": safe if snapshot_status is None else snapshot_status["safe_verdict"], "timestamp": "2026-10-01T02:00:00Z",
                                             "size": calls + int(bool(receipt)) if changed else 1}}
            if include_status:
                result["/fixture/transcript"]["status"] = {"turn_id": "actual-turn", "actual_turn_ids": ["actual-turn"]}
            return result
        # AF_UNIX paths are short on macOS; /var/folders can exceed that limit.
        with tempfile.TemporaryDirectory(prefix="claim-", dir="/tmp") as folder:
            home = Path(folder)
            socket_path = CLOSURE.daemon_endpoint(home)
            socket_path.parent.mkdir()
            app = web.Application()
            app.router.add_get("/", handler)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.UnixSite(runner, str(socket_path)).start()
            os.chmod(socket_path, 0o666 if unsafe else 0o600)
            token = None
            try:
                fingerprint = {"/fixture/transcript": {"safe_verdict": safe, "timestamp": "2026-10-01T02:00:00Z", "size": 1}}
                record = {"session_id": "holder", "transcript": thread["path"], "launch_pid": 123,
                          "launch_generation": "fixture-generation", "launch_started_at": "2026-10-01T01:00:00Z",
                          "ended_at": "2026-10-01T02:01:00Z", "closure_evidence": {
                              "codex_home": str(home.resolve()), "transcripts": fingerprint}} if receipt else None
                token = CLOSURE.RECEIPTS.set({"holder": record} if record else {})
                with patch.object(SESSIONS, "transcript_snapshot", side_effect=lambda *_args: status(None)), \
                        patch.object(CLOSURE, "transcripts", side_effect=lambda *_args, **kw: status(None, **kw)), \
                        patch.object(CLOSURE, "verified_record", return_value=record), \
                        patch.object(CLOSURE, "receipt_current", return_value=not receipt_changed):
                    peers, states, coverage = await SESSIONS.observe_home(home, {"holder"}, proof_sessions)
            finally:
                if token is not None: CLOSURE.RECEIPTS.reset(token)
                await runner.cleanup()
        self.assertEqual(unexpected, [])
        self.assertEqual(coverage["peer_complete"], not (mismatch or unsafe))
        if unsafe: self.assertEqual(coverage["status"], "incomplete")
        return peers, states

    async def test_unloaded_native_session_with_verified_final_handoff_is_candidate(self):
        peers, states = await self.observe("notLoaded")
        self.assertEqual(peers, [])
        self.assertEqual(states["holder"]["state"], "ended")

    async def test_ended_claim_recovery_does_not_require_pre_exit_release(self):
        session_record = SESSIONS.native_helpers()[1]
        from test_claim_receipts import history, claim
        declared = session_record.summarize(history("codex", claim()), "codex")
        self.assertFalse(declared["safe_verdict"])
        peers, states = await self.observe("notLoaded", snapshot_status=declared)
        self.assertEqual(peers, [])
        self.assertEqual(states["holder"]["state"], "ended")

    async def test_released_idle_session_with_verified_closure_is_ended(self):
        peers, states = await self.observe("idle", receipt=True, proof_sessions=set())
        self.assertFalse(peers)
        self.assertEqual(states["holder"]["state"], "ended")

    async def test_idle_admission_requires_released_claim_and_unchanged_closure(self):
        for kwargs in ({}, {"receipt": True}, {"receipt": True, "proof_sessions": set(), "receipt_changed": True},
                       {"receipt": True, "proof_sessions": set(), "changed": True}):
            with self.subTest(kwargs=kwargs):
                peers, states = await self.observe("idle", **kwargs)
                self.assertTrue(peers)
                self.assertNotEqual(states.get("holder", {}).get("state"), "ended")

    async def test_idle_closure_cannot_override_active_or_resumed_status(self):
        for state, after in (("active", None), ("systemError", None), ("idle", "active")):
            with self.subTest(state=state, after=after):
                peers, states = await self.observe(state, receipt=True, proof_sessions=set(), after=after)
                self.assertTrue(peers)
                self.assertEqual(states["holder"]["state"], "live")

    async def test_active_session_overrides_completion(self):
        for state in ("active", "idle", "systemError"):
            peers, states = await self.observe(state)
            self.assertEqual(states["holder"]["state"], "live")
            self.assertEqual(peers[0]["sessionId"], "holder")

    async def test_idle_failure_unknown_and_changed_evidence_do_not_prove_end(self):
        for kwargs in ({"safe": False}, {"changed": True}):
            with self.subTest(kwargs=kwargs):
                _, states = await self.observe("notLoaded", **kwargs)
                self.assertNotEqual(states.get("holder", {}).get("state"), "ended")
        _, states = await self.observe("notLoaded", mismatch=True)
        self.assertNotEqual(states.get("holder", {}).get("state"), "ended")

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
            path = CLOSURE.daemon_endpoint(home)
            path.parent.mkdir()
            with socket.socket(socket.AF_UNIX) as stale:
                stale.bind(str(path))
                os.chmod(path, 0o600)
                peers, states, coverage = await SESSIONS.observe_home(home, {"holder"})
        self.assertEqual((peers, states), ([], {}))
        self.assertEqual(coverage["status"], "unavailable")


class ObservationMergeTests(unittest.TestCase):

    def test_pending_without_a_closeout_record_is_rechecked_from_live_native_evidence(self):
        with ReceiptFixture() as f:
            with CLOSURE.resume_guard(f.session, method="turn/start", home=f.home):
                pass
            CLOSURE.replace_rows([])
            f.transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": f.session}}) + "\n")
            before = CLOSURE.lease_path(f.session).read_bytes()
            node = {"session_id": f.session, "harness": "codex", "transcript": str(f.transcript), "records": []}
            def pending():
                return SESSIONS.pending_ownership([node], [{"pending": True}])
            f.native_state = "active"
            self.assertTrue(pending())
            f.native_state = "notLoaded"
            self.assertFalse(pending())
            self.assertNotEqual(f.observe()[1][f.session]["state"], "ended")
            f.queue_items = [{"id": "new-work"}]
            self.assertTrue(pending())
            f.queue_items = []
            self.assertFalse(pending())
            self.assertEqual(CLOSURE.lease_path(f.session).read_bytes(), before)
            self.assertFalse(f.mutations)

    def test_live_claude_uncertainty_cannot_use_an_old_physical_exit(self):
        node = {"session_id": "live-claude", "harness": "claude"}
        with patch.object(CLOSURE, "ownership") as ownership:
            self.assertFalse(SESSIONS._physical_quiescent(node, [], {}))
        ownership.assert_not_called()

    def test_unmanaged_completed_native_child_needs_positive_current_coverage(self):
        with ReceiptFixture("11111111-2222-4333-8444-555555555555") as f:
            CLOSURE.replace_rows([])
            CLOSURE.lease_path(f.session).unlink()
            node = {"session_id": f.session, "harness": "codex", "transcript": str(f.transcript), "records": []}
            complete = {"peer_complete": True, "process_coverage": "complete"}
            self.assertTrue(SESSIONS._physical_quiescent(node, [], complete))
            self.assertFalse(CLOSURE.lease_path(f.session).exists())
            f.commands = "456 codex resume " + f.session
            self.assertFalse(SESSIONS._physical_quiescent(node, [], complete))

            f.commands = ""
            self.assertFalse(SESSIONS._physical_quiescent(node, [], {**complete, "peer_complete": False}))
            f.execution_items = [{"id": "working", "type": "commandExecution", "status": "inProgress"}]
            self.assertFalse(SESSIONS._physical_quiescent(node, [], complete))
            f.execution_items = []
            other = f.root / "unavailable-retained-home"
            other.mkdir()
            with CLOSURE.resume_guard(f.session, method="turn/start", home=other):
                pass
            self.assertFalse(SESSIONS._physical_quiescent(node, [], complete))

    def test_active_check_binds_its_native_background_handle_instead_of_its_call_id(self):
        import claim_receipts
        from test_claim_receipts import cell_header, composed_result
        with ReceiptFixture("11111111-2222-4333-8444-555555555555") as f:
            f.native_state = "active"
            original = [json.loads(line) for line in f.transcript.read_text().splitlines()][:2]
            original.append({"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "call-check",
                "name": "exec", "input": 'text(await tools.exec_command({cmd:"uv run check_claims.py"}));'}})
            started = composed_result({"session_id": 123, "output": ""}, "call-check")
            started["payload"]["output"].insert(0, cell_header())
            current = [*original, started]
            f.transcript.write_text("".join(json.dumps(row) + "\n" for row in current))
            node = {"session_id": f.session, "harness": "codex", "transcript": str(f.transcript), "records": original}
            pair = {"itemId": "exec-native-check", "processId": "123"}
            f.rpc_results["thread/backgroundTerminals/list"] = {"data": [pair], "nextCursor": None}
            # Real native capture: the running command is absent from items;
            # only the background registry contains its exec-* identity.
            self.assertTrue(SESSIONS._current_check_quiet(node))
            def append_poll(_request):
                poll = {"payload": {"type": "custom_tool_call", "call_id": "poll-check", "name": "exec",
                    "input": 'text(await tools.write_stdin({session_id:123}));'}}
                result = composed_result({"session_id": 123, "output": ""}, "poll-check")
                result["payload"]["output"].insert(0, cell_header())
                f.transcript.write_text("".join(json.dumps(row) + "\n" for row in [*current, poll, result]))
                return {"data": [pair], "nextCursor": None}
            f.rpc_results["thread/backgroundTerminals/list"] = append_poll
            self.assertTrue(SESSIONS._current_check_quiet(node))
            f.transcript.write_text("".join(json.dumps(row) + "\n" for row in current))
            f.rpc_results["thread/backgroundTerminals/list"] = {"data": [pair], "nextCursor": None}
            for pairs in ([], [{**pair, "processId": "other"}], [pair, pair],
                          [pair, {"itemId": "exec-other", "processId": "456"}]):
                f.rpc_results["thread/backgroundTerminals/list"]["data"] = pairs
                self.assertFalse(SESSIONS._current_check_quiet(node))
            f.rpc_results["thread/backgroundTerminals/list"]["data"] = [pair]
            f.queue_items = [{"id": "queued", "clientUserMessageId": "new", "input": []}]
            self.assertFalse(SESSIONS._current_check_quiet(node))
            f.queue_items = []
            f.execution_items = [{"id": "other", "type": "dynamicToolCall", "status": "inProgress"}]
            self.assertFalse(SESSIONS._current_check_quiet(node))
            f.execution_items = []
            extra = {"payload": {"type": "function_call", "call_id": "other", "name": "exec_command",
                                 "arguments": json.dumps({"cmd": "ls"})}}
            f.transcript.write_text("".join(json.dumps(row) + "\n" for row in [*current, extra]))
            self.assertFalse(SESSIONS._current_check_quiet(node))
            self.assertIsNone(claim_receipts.current_check_process(original, [*current, extra]))
            f.transcript.write_text("".join(json.dumps(row) + "\n" for row in current))
            import time
            self.assertFalse(SESSIONS._current_check_quiet(node, deadline_at=time.time() - 1))
            async def delayed_thread(_request):
                await asyncio.sleep(1.4)
                return {"thread": {"id": f.session, "path": str(f.transcript),
                    "historyMode": "paginated", "status": {"type": "active"}}}
            f.rpc_results["thread/read"] = delayed_thread
            # The caller's remaining budget covers the entire native sandwich;
            # the former shorter connection/outer budgets failed this read.
            self.assertTrue(SESSIONS._current_check_quiet(node, deadline_at=time.time() + 15))

    def setUp(self):
        shared = SESSIONS.process_liveness()
        self.enterContext(patch.object(shared, "process_arguments",
            side_effect=lambda _pid, command: (shared.harness_arguments(command), True)))

    def test_malformed_action_barrier_blocks_native_end_proof_without_writing(self):
        with ReceiptFixture() as f:
            f.native_state = "notLoaded"
            path = CLOSURE.lease_path(f.session)
            path.write_text("incomplete action evidence")
            before = {p: p.read_bytes() for p in f.log.parent.rglob("*") if p.is_file()}
            _, states, _ = f.observe({f.session})
            self.assertEqual(states[f.session]["state"], "live")
            self.assertIsNone(CLOSURE.verified_record(f.session, f.home))
            self.assertEqual({p: p.read_bytes() for p in f.log.parent.rglob("*") if p.is_file()}, before)

    def test_nonempty_or_missing_queue_evidence_blocks_even_authored_release_fallback(self):
        for native in ("idle", "notLoaded"):
            for queued in ({"data": [{"id": "pending"}], "nextCursor": None},
                           {"data": [], "nextCursor": "more"}, {}):
                with self.subTest(native=native, queued=queued), ReceiptFixture() as f:
                    f.native_state = native
                    f.rpc_results["thread/queue/list"] = queued
                    _, states, _ = f.observe()
                    self.assertEqual(states[f.session]["state"], "live")

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
                self.assertEqual(SESSIONS.codex_homes(strict=True, configured_only=True), expected)

    def test_configured_receipt_homes_do_not_depend_on_caller_account_or_order(self):
        config = {"accounts": [{"provider": "openai", "env": {"CODEX_HOME": "/fixture/b"}},
                               {"provider": "openai", "env": {"CODEX_HOME": "/fixture/a"}}]}
        helpers = SimpleNamespace(load_config=lambda **_kw: config)
        results = []
        for current in ("/fixture/a", "/fixture/b", "/fixture/unconfigured"):
            with patch.dict(os.environ, {"CODEX_HOME": current}), \
                    patch.object(SESSIONS, "native_helpers", return_value=(helpers, None)):
                results.append(SESSIONS.codex_homes(strict=True, configured_only=True))
        self.assertTrue(all(homes == results[0] for homes in results))
        self.assertEqual(set(results[0]), {Path(a["env"]["CODEX_HOME"]).resolve() for a in config["accounts"]})

    def test_receipt_proofs_require_available_account_inventory(self):
        config = SimpleNamespace(load_config=lambda: (_ for _ in ()).throw(ValueError("missing account inventory")))
        with patch.object(SESSIONS, "native_helpers", return_value=(config, None)):
            self.assertTrue(SESSIONS.codex_homes())
            with self.assertRaisesRegex(ValueError, "missing account inventory"):
                SESSIONS.codex_homes(strict=True)

    def test_shared_log_is_independent_of_provider_account_home(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"HOME": folder}):
            first = CLOSURE.closed_log()
            with patch.dict(os.environ, {"CODE_HOME": str(Path(folder) / "other-runtime"), "CODEX_HOME": str(Path(folder) / "other-account")}):
                self.assertEqual(CLOSURE.closed_log(), first)
            self.assertTrue(first.is_relative_to(folder))
            self.assertFalse(first.exists())

    def test_session_specific_closure_allows_unrelated_clients_but_requires_process_read(self):
        async def read(_home, _sessions, _proof):
            return [], {"holder": {"state": "ended"}}, {
                "status": "available", "peer_complete": True, "socket": "/fixture/socket"}
        for returncode in (0, 1):
            with self.subTest(returncode=returncode), \
                    patch.object(SESSIONS, "codex_homes", return_value=[Path("/fixture")]), \
                    patch.object(SESSIONS, "observe_home", side_effect=read), \
                    patch.object(SESSIONS.subprocess, "run", return_value=SimpleNamespace(
                        returncode=returncode, stdout="codex exec fresh-unrelated-work")):
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
            self.assertTrue(snapshot[str(primary.resolve())]["declared_safe_verdict"])
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
