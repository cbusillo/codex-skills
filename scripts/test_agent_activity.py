#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Lifecycle/privacy fixtures and a real Unix websocket observer exercise."""
import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web, WSMsgType

from agent_activity import ActivityStore, atomic_json, home_key, hook_activity, uuid7
from observe_agent_activity import CodexObserver, project_claude, claude_poll, durable_cli

SESSION = str(uuid.uuid4())
RUN = str(uuid.uuid4())
ACCOUNT = "anthropic-0123456789abcdef"
SECRET = "private-prompt-message-tool-command-environment-/absolute/secret"


class ActivityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = ActivityStore(self.root / "panel", self.root / "code")
        self.env = {"CODE_HOME": str(self.root / "code"), "CLAUDE_CONFIG_DIR": str(self.root / "claude")}
        atomic_json(self.store.private / f"binding-{home_key('claude', self.env)}.json",
                    {"accountID": ACCOUNT, "storageRoot": str(self.root / "panel")})

    def events(self):
        return [json.loads(p.read_text()) for p in self.store.events.glob("*.json")]

    def hook(self, event, **fields):
        hook_activity({"hook_event_name": event, "session_id": SESSION,
                       "prompt": SECRET, "last_assistant_message": SECRET,
                       "transcript_path": SECRET, "tool_input": {"command": SECRET},
                       "tool_output": SECRET, "cwd": SECRET, "environment": {"API_KEY": SECRET}, **fields},
                      {**self.env, "API_KEY": SECRET})

    def test_hooks_keep_run_identity_and_discard_all_content(self):
        self.hook("UserPromptSubmit")
        self.hook("Notification", notification_type="permission_prompt", message=SECRET)
        self.hook("Stop", stop_hook_active=False)
        project_claude(self.store, ACCOUNT, [{'sessionId': SESSION, 'status': 'idle', 'state': 'done'}])
        records = self.events()
        self.assertEqual({r['state'] for r in records}, {"working", "attention", "quiet", "completed"})
        self.assertEqual(len({r['runID'] for r in records}), 1)
        self.assertTrue(any(r['attentionKind'] == 'approval' for r in records))
        for path in self.store.events.glob("*.json"):
            self.assertLessEqual(path.stat().st_size, 1024)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            text = path.read_text()
            self.assertNotIn(SECRET, text)
            self.assertNotIn(str(self.root), text)
        self.hook("Stop")
        self.assertEqual(sum(r['state'] == 'completed' for r in self.events()), 1)
        self.hook("UserPromptSubmit")
        self.hook("StopFailure", error=SECRET)
        self.assertEqual(len({r['runID'] for r in self.events()}), 2)
        self.assertTrue(any(r['state'] == 'failed' for r in self.events()))

    def test_subagent_parentage_and_nonterminal_reentrant_stop(self):
        self.hook("UserPromptSubmit")
        self.hook("SubagentStart", agent_id="agent-abc123")
        self.hook("SubagentStop", agent_id="agent-abc123", last_assistant_message=SECRET)
        child = [r for r in self.events() if r['parentSessionID']]
        self.assertEqual({r['parentSessionID'] for r in child}, {SESSION})
        self.assertEqual(len({r['sessionID'] for r in child}), 1)
        self.assertEqual(len({r['runID'] for r in child}), 1)
        self.assertEqual({r['state'] for r in child}, {'working', 'quiet'})
        self.hook("Stop", stop_hook_active=True)
        parents = [r for r in self.events() if r['sessionID'] == SESSION]
        self.assertTrue(all(r['state'] in ('working', 'quiet') for r in parents))
        self.assertFalse(any(r['state'] == 'completed' for r in parents))

    def test_common_child_events_do_not_change_the_parent_outcome(self):
        self.hook('UserPromptSubmit')
        self.hook('SubagentStart', agent_id='agent-abc123')
        self.hook('PermissionRequest', agent_id='agent-abc123', tool_input={'command': SECRET})
        self.hook('StopFailure', agent_id='agent-abc123', error=SECRET)
        children = [r for r in self.events() if r['parentSessionID'] == SESSION]
        self.assertEqual({r['state'] for r in children}, {'working', 'attention', 'failed'})
        self.assertEqual(len({r['runID'] for r in children}), 1)
        self.assertEqual([r['state'] for r in self.events() if r['sessionID'] == SESSION], ['working'])
        self.assertNotIn('agent-abc123', json.dumps(self.events()))
        self.assertNotIn(SECRET, json.dumps(self.events()))

    def test_inventory_renews_busy_leases_and_does_not_invent_completion(self):
        self.hook("UserPromptSubmit")
        before = self.events()[0]
        row = {"sessionId": SESSION, "status": "busy", "cwd": SECRET, "name": SECRET, "summary": SECRET}
        project_claude(self.store, ACCOUNT, [row])
        busy = next(r for r in self.events() if r['outcomeSource'] == 'claude-inventory')
        self.assertEqual(busy['runID'], before['runID'])
        self.assertGreaterEqual(busy['leaseExpiresAt'], before['leaseExpiresAt'])
        self.hook("Stop")
        project_claude(self.store, ACCOUNT, [{**row, 'status': 'idle', 'state': 'done'}])
        count = len(self.events())
        project_claude(self.store, ACCOUNT, [row])
        self.assertEqual(len(self.events()), count)  # Stop-hook process still reads busy.
        project_claude(self.store, ACCOUNT, [{**row, 'status': 'idle'}])
        self.assertTrue(any(r['state'] == 'completed' for r in self.events()))
        project_claude(self.store, ACCOUNT, [])
        self.assertEqual(len(self.events()), count)  # Missing sessions simply expire.
        project_claude(self.store, ACCOUNT, [{**row, 'status': 'waiting', 'waitingFor': 'input needed'}])
        self.hook('UserPromptSubmit')
        project_claude(self.store, ACCOUNT, [{**row, 'status': 'waiting', 'waitingFor': 'input needed'}])
        self.assertTrue(any(r['attentionKind'] == 'input' for r in self.events()))
        self.assertNotIn(SECRET, json.dumps(self.events()))

    def test_unknown_identifiers_notifications_and_disabled_sources_are_ignored(self):
        for event in ('Notification', 'PostToolUse'):
            self.hook(event, notification_type='idle_prompt')
        self.hook('UserPromptSubmit', session_id=SECRET)
        self.hook('SubagentStart', agent_id=SECRET)
        hook_activity({'hook_event_name': 'UserPromptSubmit', 'session_id': SESSION},
                      {**self.env, 'AGENT_ACTIVITY_DISABLED': '1'})
        self.assertEqual(self.events(), [])

    def test_generic_dialog_renews_attention_without_guessing_its_kind(self):
        self.hook('UserPromptSubmit')
        before = self.events()[0]
        project_claude(self.store, ACCOUNT, [{'sessionId': SESSION, 'status': 'waiting', 'waitingFor': 'dialog open'}])
        waiting = next(r for r in self.events() if r['state'] == 'attention')
        self.assertEqual(waiting['runID'], before['runID'])
        self.assertGreaterEqual(waiting['leaseExpiresAt'], before['leaseExpiresAt'])
        self.assertIsNone(waiting['attentionKind'])

    def test_temporary_cli_can_be_replaced_by_an_explicit_durable_executable(self):
        ephemeral = self.root / 'venv'
        ephemeral.mkdir()
        (ephemeral / 'pyvenv.cfg').write_text('home = fixture')
        binary = ephemeral / 'claude'
        binary.write_text('fixture')
        binary.chmod(0o700)
        with self.assertRaises(ValueError):
            durable_cli('claude', binary)
        durable = Path(sys.executable).resolve()
        self.assertEqual(durable_cli('claude', durable), str(durable))

    def test_stop_and_interrupt_need_native_done_before_success(self):
        self.hook('UserPromptSubmit')
        self.hook('Stop')
        project_claude(self.store, ACCOUNT, [{'sessionId': SESSION, 'status': 'busy'}])
        self.assertFalse(any(r['state'] == 'completed' for r in self.events()))
        self.hook('Stop', stop_hook_active=True)
        project_claude(self.store, ACCOUNT, [{'sessionId': SESSION, 'status': 'idle'}])
        self.assertEqual(sum(r['state'] == 'completed' for r in self.events()), 0)
        project_claude(self.store, ACCOUNT, [{'sessionId': SESSION, 'status': 'idle', 'state': 'done'}])
        self.assertEqual(sum(r['state'] == 'completed' for r in self.events()), 1)
        self.hook('UserPromptSubmit')
        project_claude(self.store, ACCOUNT, [{'sessionId': SESSION, 'status': 'idle'}])
        self.assertEqual(sum(r['state'] == 'completed' for r in self.events()), 1)
        self.assertTrue(any(r['state'] == 'quiet' for r in self.events()))

    def test_terminal_outcome_survives_later_quiet_and_quiet_does_not_churn(self):
        for harness, account, source in [('codex', 'openai-a', 'codex-status'), ('claude', ACCOUNT, 'claude-hook')]:
            session = str(uuid.uuid4())
            self.store.emit(account, harness, session, 'working', source, start=True)
            self.store.emit(account, harness, session, 'completed', source)
            for _ in range(3):
                self.assertIsNone(self.store.emit(account, harness, session, 'quiet', source))
        self.hook('Stop')
        count = len(self.events())
        for _ in range(3):
            project_claude(self.store, ACCOUNT, [{'sessionId': SESSION, 'status': 'idle'}])
        self.assertEqual(len(self.events()), count)

    def test_same_timestamp_transitions_fold_in_emission_order(self):
        now = dt.datetime.now(dt.timezone.utc)
        first = self.store.emit(ACCOUNT, 'claude', SESSION, 'working', 'claude-hook', run=RUN, now=now)
        last = self.store.emit(ACCOUNT, 'claude', SESSION, 'completed', 'claude-inventory', run=RUN, now=now)
        self.assertGreater(last['eventID'], first['eventID'])

    def test_activity_storage_failure_preserves_advisory_stop(self):
        bad = self.root / 'redirected-panel'
        bad.symlink_to(self.root / 'panel', target_is_directory=True)
        atomic_json(self.store.private / f"binding-{home_key('claude', self.env)}.json",
                    {'accountID': ACCOUNT, 'storageRoot': str(bad)})
        hook = Path(__file__).resolve().parents[1] / 'hooks' / 'session_alert_hook.py'
        env = {**os.environ, **self.env}
        env.pop('CODEX_SKILLS_HARNESS', None)
        result = subprocess.run([sys.executable, str(hook)], env=env, capture_output=True, text=True,
                                input=json.dumps({'hook_event_name': 'Stop', 'session_id': SESSION}), timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {})
        advisory = json.loads((self.root / 'code' / 'session-events.jsonl').read_text())
        self.assertEqual(advisory['event'], 'Stop')

    def test_uuid7_and_prune_only_owned_day_old_regular_records(self):
        now = dt.datetime.now(dt.timezone.utc)
        self.assertEqual(uuid.UUID(uuid7(now)).version, 7)
        old = self.store.emit(ACCOUNT, 'claude', SESSION, 'working', 'claude-hook',
                              now=now - dt.timedelta(days=2))
        self.store.emit(ACCOUNT, 'claude', SESSION, 'quiet', 'claude-inventory', now=now)
        foreign = self.store.events / 'foreign.json'
        foreign.write_text('{}')
        malformed = self.store.events / f'codex-skills-{uuid.uuid4()}.json'
        malformed.write_text('malformed')
        target = self.root / 'preserved'
        target.write_text('preserved')
        redirect = self.store.events / f'codex-skills-{uuid.uuid4()}.json'
        redirect.symlink_to(target)
        self.store.prune(now)
        self.assertFalse((self.store.events / f"codex-skills-{old['eventID']}.json").exists())
        self.assertTrue(foreign.exists() and malformed.exists() and redirect.is_symlink())
        self.assertEqual(target.read_text(), 'preserved')
        abandoned = str(uuid.uuid4())
        self.store.emit(ACCOUNT, 'claude', abandoned, 'working', 'claude-hook', now=now - dt.timedelta(days=2))
        private_path = self.store.private / f"{self.store.key(ACCOUNT, 'claude', abandoned)}.json"
        self.assertTrue(private_path.exists())
        self.store.prune(now)
        self.assertFalse(private_path.exists())

    def test_hook_processes_share_run_state_without_controlling_turn(self):
        hook = Path(__file__).resolve().parents[1] / 'hooks' / 'session_alert_hook.py'
        env = {**os.environ, **self.env}
        env.pop('CODEX_SKILLS_HARNESS', None)
        for event in ('UserPromptSubmit', 'Stop'):
            result = subprocess.run([sys.executable, str(hook)], env=env, capture_output=True, text=True,
                                    input=json.dumps({'hook_event_name': event, 'session_id': SESSION, 'prompt': SECRET}), timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), {})
        self.assertEqual(len({r['runID'] for r in self.events()}), 1)

    def test_redirected_store_is_refused_without_touching_target(self):
        link = self.root / 'link'
        link.symlink_to(self.root / 'panel', target_is_directory=True)
        with self.assertRaises(ValueError):
            ActivityStore(link, self.root / 'other-private')


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_overflow_is_a_recoverable_source_gap(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            store = ActivityStore(root / 'panel', root / 'private')
            observer = CodexObserver(store, 'openai-a', root)
            observer.queue = asyncio.Queue(maxsize=1)

            class Frames:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    return SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({'method': 'thread/closed', 'params': {'threadId': SESSION}}))

            observer.ws = Frames()
            with self.assertRaises(ValueError):
                await observer.receive()

    async def test_claude_reads_multiple_pipe_chunks_before_projecting(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            store = ActivityStore(root / 'panel', root / 'private')
            script = ('import json,sys,time; '
                      f'data=json.dumps([{{"sessionId":{SESSION!r},"status":"busy","name":"x"*100000}}]); '
                      'sys.stdout.write(data[:64]);sys.stdout.flush();time.sleep(.03); '
                      'sys.stdout.write(data[64:]);sys.stdout.flush()')

            fixture = root / 'claude-fixture'
            fixture.write_text(f'#!{sys.executable}\n{script}\n')
            fixture.chmod(0o700)
            await claude_poll(store, {'accountID': ACCOUNT, 'env': {}}, once=True, executable=str(fixture))
            records = [json.loads(p.read_text()) for p in store.events.glob('*.json')]
            self.assertEqual([r['state'] for r in records], ['working'])
            self.assertNotIn('name', records[0])

    async def test_real_socket_initial_active_idle_before_terminal_and_unsubscribe(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temp:
            root = Path(temp).resolve()
            store = ActivityStore(root / 'panel', root / 'code')
            home = root / 'codex'
            sock = home / 'app-server-control' / 'app-server-control.sock'
            sock.parent.mkdir(parents=True)
            calls = []
            joined = asyncio.Event()
            terminal = asyncio.Event()

            async def server(request):
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                async for frame in ws:
                    data = json.loads(frame.data)
                    if 'id' not in data:
                        continue
                    method, params = data['method'], data['params']
                    calls.append((method, params))
                    if method == 'initialize':
                        result = {}
                    elif method == 'thread/loaded/list':
                        result = {'data': [SESSION], 'nextCursor': None}
                    elif method == 'thread/read':
                        result = {'thread': {'id': SESSION, 'status': {'type': 'active'}, 'preview': SECRET,
                                             'source': {'subAgent': {'thread_spawn': {'parent_thread_id': RUN}}}}}
                    elif method == 'thread/resume':
                        result = {'thread': {'id': SESSION, 'turns': []}}
                    elif method == 'thread/turns/list':
                        result = {'data': [{'id': RUN, 'status': 'completed' if terminal.is_set() else 'inProgress',
                                            'items': [{'text': SECRET, 'command': SECRET}]}]}
                    elif method == 'thread/unsubscribe':
                        result = {'status': 'unsubscribed'}
                        joined.set()
                    else:
                        self.fail(f'unexpected request {method}')
                        raise AssertionError('unexpected observer request')
                    if method == 'thread/turns/list' and not terminal.is_set():
                        await ws.send_json({'id': data['id'], 'method': 'item/commandExecution/requestApproval',
                                            'params': {'threadId': SESSION, 'command': SECRET}})
                    await ws.send_json({'id': data['id'], 'result': result})
                    if method == 'thread/turns/list' and not terminal.is_set():
                        await ws.send_json({'method': 'item/agentMessage/delta', 'params': {'delta': SECRET}})
                        terminal.set()
                        await ws.send_json({'method': 'thread/status/changed', 'params': {'threadId': SESSION, 'status': {'type': 'idle'}}})
                        await ws.send_json({'method': 'turn/completed', 'params': {'threadId': SESSION, 'turn': {'id': RUN, 'status': 'completed', 'items': [{'text': SECRET}]}}})
                return ws

            app = web.Application()
            app.router.add_get('/', server)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.UnixSite(runner, str(sock)).start()
            sock.chmod(0o600)
            observer = CodexObserver(store, 'openai-0123456789abcdef', home)
            task = asyncio.create_task(observer.once_connected())
            try:
                await asyncio.wait_for(joined.wait(), 5)
                await asyncio.sleep(0.05)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await runner.cleanup()
            records = [json.loads(p.read_text()) for p in store.events.glob('*.json')]
            self.assertTrue(any(r['state'] == 'completed' for r in records))
            self.assertEqual({r['runID'] for r in records}, {RUN})
            self.assertEqual({r['parentSessionID'] for r in records}, {RUN})
            self.assertNotIn(SECRET, json.dumps(records))
            self.assertTrue(all(m in {'initialize', 'thread/loaded/list', 'thread/read', 'thread/resume',
                                      'thread/turns/list', 'thread/unsubscribe'} for m, _ in calls))
            self.assertEqual([p for m, p in calls if m == 'thread/resume'], [{'threadId': SESSION, 'excludeTurns': True}])

    async def test_structured_failed_and_interrupted_do_not_become_completed(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temp:
            root = Path(temp).resolve()
            store = ActivityStore(root / 'panel', root / 'code')
            observer = CodexObserver(store, 'openai-0123456789abcdef', root)
            for status in ('failed', 'interrupted'):
                await observer.turn(SESSION, {'id': str(uuid.uuid4()), 'status': status, 'error': {'message': SECRET}})
            records = [json.loads(p.read_text()) for p in store.events.glob('*.json')]
            self.assertEqual({r['state'] for r in records}, {'failed', 'cancelled'})
            self.assertNotIn(SECRET, json.dumps(records))


if __name__ == '__main__':
    unittest.main()
