#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Lifecycle/privacy fixtures and a real Unix websocket observer exercise."""
import asyncio
import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import uuid
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web, WSMsgType

from agent_activity import (FIELDS, LEASE_SECONDS, PRUNE_STAMP, RETENTION_SECONDS, ActivityStore, atomic_json,
                            home_key, hook_activity, inbox_records, renewal_report, timestamp, uuid7)
from observe_agent_activity import CodexObserver, project_claude, claude_poll, durable_cli, main, observe

SESSION = str(uuid.uuid4())
RUN = str(uuid.uuid4())
ACCOUNT = "anthropic-0123456789abcdef"
SECRET = "private-prompt-message-tool-command-environment-/absolute/secret"


class ActivityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = ActivityStore(self.root / "code")
        self.env = {"CODE_HOME": str(self.root / "code"), "CLAUDE_CONFIG_DIR": str(self.root / "claude")}
        atomic_json(self.store.private / f"binding-{home_key('claude', self.env)}.json",
                    {"producerID": "codex-skills", "accountID": ACCOUNT})

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

    def test_no_free_text_reaches_inbox_or_private_state(self):
        fields = ('prompt', 'last_assistant_message', 'transcript_path', 'cwd', 'tool_name', 'tool_input',
                  'command', 'environment', 'inventory_name', 'inventory_summary', 'inventory_cwd', 'codex_item')
        sentinel = {field: f'sentinel-{field}-{uuid.uuid4().hex}' for field in fields}
        env = {**self.env, 'SENTINEL_TOKEN': sentinel['environment']}
        payload = {'session_id': SESSION, 'prompt': sentinel['prompt'],
                   'last_assistant_message': sentinel['last_assistant_message'],
                   'transcript_path': sentinel['transcript_path'], 'cwd': sentinel['cwd'],
                   'tool_name': sentinel['tool_name'],
                   'tool_input': {'command': sentinel['command'], 'description': sentinel['tool_input']},
                   'message': sentinel['prompt'], 'error': sentinel['last_assistant_message']}
        for event, extra in (('UserPromptSubmit', {}), ('PermissionRequest', {}),
                             ('Notification', {'notification_type': 'permission_prompt'}),
                             ('SubagentStart', {'agent_id': 'agent-abc123'}),
                             ('SubagentStop', {'agent_id': 'agent-abc123'}), ('StopFailure', {}), ('Stop', {})):
            hook_activity({**payload, 'hook_event_name': event, **extra}, env)
        project_claude(self.store, ACCOUNT, [{'sessionId': RUN, 'status': 'busy', 'name': sentinel['inventory_name'],
                                              'summary': sentinel['inventory_summary'], 'cwd': sentinel['inventory_cwd']}])
        codex = str(uuid.uuid4())
        item = [{'type': 'agentMessage', 'text': sentinel['codex_item']}]
        frames = [{'method': 'item/agentMessage/delta', 'params': {'threadId': codex, 'delta': sentinel['codex_item']}},
                  {'method': 'item/completed', 'params': {'threadId': codex, 'item': item[0]}},
                  {'method': 'turn/started', 'params': {'threadId': codex, 'turn': {'id': RUN, 'status': 'inProgress', 'items': item}}},
                  {'method': 'turn/completed', 'params': {'threadId': codex, 'turn': {'id': RUN, 'status': 'completed', 'items': item}}}]
        observer = CodexObserver(self.store, 'openai-0123456789abcdef', self.root)

        class Frames:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if not frames:
                    raise StopAsyncIteration
                return SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(frames.pop(0)))

        async def observe_frames():
            observer.ws = Frames()
            await observer.receive()
            while not observer.queue.empty():
                await observer.handle(observer.queue.get_nowait())

        asyncio.run(observe_frames())
        records = self.events()
        self.assertEqual({r['outcomeSource'] for r in records}, {'claude-hook', 'claude-inventory', 'codex-turn'})
        for record in records:
            self.assertEqual(tuple(record), FIELDS)
        written = [p for p in (self.root / 'code').rglob('*') if p.is_file()]
        self.assertTrue(any(p.parent == self.store.private for p in written))
        for path in written:
            data = path.read_bytes()
            for value in sentinel.values():
                self.assertNotIn(value.encode(), data, path.name)

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
        target = self.root / 'elsewhere'
        target.mkdir()
        self.store.events.rmdir()
        self.store.events.symlink_to(target, target_is_directory=True)
        hook = Path(__file__).resolve().parents[1] / 'hooks' / 'session_alert_hook.py'
        env = {**os.environ, **self.env}
        env.pop('CODEX_SKILLS_HARNESS', None)
        result = subprocess.run([sys.executable, str(hook)], env=env, capture_output=True, text=True,
                                input=json.dumps({'hook_event_name': 'Stop', 'session_id': SESSION}), timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {})
        advisory = json.loads((self.root / 'code' / 'session-events.jsonl').read_text())
        self.assertEqual(advisory['event'], 'Stop')
        self.assertEqual(list(target.iterdir()), [])

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
        target = self.root / 'target'
        target.mkdir()
        link = self.root / 'link'
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            ActivityStore(link)
        state = self.root / 'redirected-state'
        state.mkdir()
        (state / 'agent-activity').symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            ActivityStore(state)
        inbox = self.root / 'redirected-inbox'
        (inbox / 'agent-activity').mkdir(parents=True)
        (inbox / 'agent-activity' / 'inbox').symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            ActivityStore(inbox)
        self.assertEqual(list(target.iterdir()), [])

    def test_inbox_is_private_and_records_are_small(self):
        loose = self.root / 'loose'
        (loose / 'agent-activity' / 'inbox').mkdir(parents=True, mode=0o755)
        (loose / 'agent-activity' / 'inbox').chmod(0o755)
        store = ActivityStore(loose)
        store.emit(ACCOUNT, 'claude', SESSION, 'working', 'claude-hook', start=True)
        self.hook('UserPromptSubmit')
        for folder in (store.private, store.events, self.store.private, self.store.events):
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        files = [*store.events.iterdir(), *self.store.events.iterdir()]
        self.assertEqual(len(files), 2)
        for path in files:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertLessEqual(path.stat().st_size, 1024)


class ProducerBoundaryTests(unittest.TestCase):
    """Only entry points shared with the earlier App Group writer, so these also run against it."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.code = self.root / 'code'
        self.state = self.code / 'agent-activity'
        self.inbox = self.state / 'inbox'
        self.app_group = self.root / 'Group Containers' / 'Context Panel'
        self.app_group.mkdir(parents=True)
        self.state.mkdir(parents=True, mode=0o700)
        self.env = {'CODE_HOME': str(self.code), 'CLAUDE_CONFIG_DIR': str(self.root / 'claude')}
        # A binding written before the inbox still names the App Group store.
        binding = self.state / f"binding-{home_key('claude', self.env)}.json"
        binding.write_text(json.dumps({'producerID': 'codex-skills', 'accountID': ACCOUNT,
                                       'storageRoot': str(self.app_group)}))

    def hook(self, event, session=SESSION):
        hook_activity({'hook_event_name': event, 'session_id': session}, dict(self.env))

    def inbox_records(self):
        return [json.loads(p.read_text()) for p in self.inbox.glob('*.json')]

    def test_hook_and_observer_write_only_the_private_inbox(self):
        self.hook('UserPromptSubmit')
        self.hook('Stop')
        hook = Path(__file__).resolve().parents[1] / 'hooks' / 'session_alert_hook.py'
        env = {**os.environ, **self.env}
        env.pop('CODEX_SKILLS_HARNESS', None)
        child = str(uuid.uuid4())
        result = subprocess.run([sys.executable, str(hook)], env=env, capture_output=True, text=True,
                                input=json.dumps({'hook_event_name': 'UserPromptSubmit', 'session_id': child}), timeout=5)
        self.assertEqual(result.returncode, 0)
        snapshot = self.root / 'snapshot.py'
        snapshot.write_text('import json\nprint(json.dumps(' + repr({
            'schemaVersion': 1, 'accounts': [{'provider': 'anthropic', 'id': ACCOUNT, 'label': 'main'}]}) + '))\n')
        config = self.root / 'supervisor.toml'
        config.write_text('[accounts]\nsnapshot_command = '
                          + json.dumps([sys.executable, str(snapshot), '--storage-root', str(self.app_group)]) + '\n'
                          '[[accounts.account]]\nname = "main"\nprovider = "anthropic"\ncontext_panel_label = "main"\n'
                          '[accounts.account.env]\nCLAUDE_CONFIG_DIR = ' + json.dumps(self.env['CLAUDE_CONFIG_DIR']) + '\n')
        inventory = self.root / 'claude-fixture'
        observed = str(uuid.uuid4())
        inventory.write_text(f'#!{sys.executable}\nimport json\n'
                             f'print(json.dumps([{{"sessionId": {observed!r}, "status": "busy"}}]))\n')
        inventory.chmod(0o700)
        asyncio.run(observe(config, once=True, state_root=self.code, claude_cli=inventory))
        self.assertEqual(list(self.app_group.rglob('*')), [])
        records = self.inbox_records()
        self.assertEqual({r['sessionID'] for r in records}, {SESSION, child, observed})
        self.assertEqual({r['outcomeSource'] for r in records}, {'claude-hook', 'claude-inventory'})
        binding = json.loads((self.state / f"binding-{home_key('claude', self.env)}.json").read_text())
        self.assertEqual(binding, {'producerID': 'codex-skills', 'accountID': ACCOUNT})

    def plant(self):
        """Day-old owned inbox and private files, plus files the prune must keep."""
        self.inbox.mkdir(mode=0o700, exist_ok=True)
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat().replace('+00:00', 'Z')
        owned = self.inbox / f'codex-skills-{uuid.uuid4()}.json'
        owned.write_text(json.dumps({'producerID': 'codex-skills', 'observedAt': old}))
        other = self.inbox / f'codex-skills-{uuid.uuid4()}.json'
        other.write_text(json.dumps({'producerID': 'another-producer', 'observedAt': old}))
        foreign = self.inbox / 'foreign.json'
        foreign.write_text(json.dumps({'producerID': 'codex-skills', 'observedAt': old}))
        key = uuid.uuid4().hex * 2
        private = self.state / f'{key}.json'
        private.write_text(json.dumps({'producerID': 'codex-skills', 'observedAt': old}))
        (self.state / f'{key[:2]}.lock').touch()
        return [owned, private], [other, foreign]

    def test_hook_prunes_owned_day_old_files_at_most_once_per_window(self):
        removable, kept = self.plant()
        self.hook('UserPromptSubmit')
        self.assertEqual([p.exists() for p in removable], [False, False])
        self.assertTrue(all(p.exists() for p in kept))
        self.assertEqual(len(self.inbox_records()), 3)  # The new event plus the two kept files.
        stamp = self.state / PRUNE_STAMP
        self.assertEqual(stamp.stat().st_mode & 0o777, 0o600)
        removable, kept = self.plant()
        self.hook('Stop')
        self.assertTrue(all(p.exists() for p in removable))  # Within the window: no second prune.
        hour_ago = stamp.stat().st_mtime - 3601
        os.utime(stamp, (hour_ago, hour_ago))
        self.hook('UserPromptSubmit')
        self.assertEqual([p.exists() for p in removable], [False, False])
        self.assertTrue(all(p.exists() for p in kept))

    def test_clock_moved_back_does_not_prune_again_but_a_far_future_stamp_is_due(self):
        self.hook('UserPromptSubmit')
        stamp = self.state / PRUNE_STAMP
        near_future = stamp.stat().st_mtime + 600
        os.utime(stamp, (near_future, near_future))
        removable, _kept = self.plant()
        self.hook('Stop')
        self.assertTrue(all(p.exists() for p in removable), "a stamp just ahead of the clock is still recent")
        far_future = stamp.stat().st_mtime + 7200
        os.utime(stamp, (far_future, far_future))
        self.hook('UserPromptSubmit')
        self.assertEqual([p.exists() for p in removable], [False, False])


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_overflow_is_a_recoverable_source_gap(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            store = ActivityStore(root / 'private')
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
            store = ActivityStore(root / 'private')
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

    async def test_claude_inventory_runs_with_the_expanded_account_home(self):
        # Account homes are configured with "~"; an unexpanded value makes the
        # CLI read a directory that does not exist and list no sessions.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            seen = root / 'seen-config-dir'
            fixture = root / 'claude-fixture'
            fixture.write_text(f'#!{sys.executable}\nimport os,pathlib\n'
                               f'pathlib.Path({str(seen)!r}).write_text(os.environ.get("CLAUDE_CONFIG_DIR", ""))\n'
                               'print("[]")\n')
            fixture.chmod(0o700)
            with unittest.mock.patch.dict(os.environ, {'HOME': str(root)}):
                await claude_poll(ActivityStore(root / 'private'),
                                  {'accountID': ACCOUNT, 'env': {'CLAUDE_CONFIG_DIR': '~/.claude-accounts/example'}},
                                  once=True, executable=str(fixture))
            self.assertEqual(seen.read_text(), str(root / '.claude-accounts' / 'example'))

    async def test_real_socket_initial_active_idle_before_terminal_and_unsubscribe(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temp:
            root = Path(temp).resolve()
            store = ActivityStore(root / 'code')
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
            store = ActivityStore(root / 'code')
            observer = CodexObserver(store, 'openai-0123456789abcdef', root)
            for status in ('failed', 'interrupted'):
                await observer.turn(SESSION, {'id': str(uuid.uuid4()), 'status': status, 'error': {'message': SECRET}})
            records = [json.loads(p.read_text()) for p in store.events.glob('*.json')]
            self.assertEqual({r['state'] for r in records}, {'failed', 'cancelled'})
            self.assertNotIn(SECRET, json.dumps(records))


class RenewalCheckTests(unittest.TestCase):
    """Fixtures come from the real writer, so they follow its schema and lease."""
    CADENCE = LEASE_SECONDS // 3
    CODEX = "openai-0123456789abcdef"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.code = Path(temporary.name).resolve() / 'code'
        self.store = ActivityStore(self.code)
        self.now = dt.datetime.now(dt.timezone.utc)

    def renewals(self, start, seconds, *, harness='claude', session=None, new_run=True):
        """A run renewed every CADENCE seconds for the given span."""
        account, source = (ACCOUNT, 'claude-inventory') if harness == 'claude' else (self.CODEX, 'codex-status')
        session = session or str(uuid.uuid4())
        records = [self.store.emit(account, harness, session, 'working', source, start=new_run and offset == 0,
                                   now=start + dt.timedelta(seconds=offset))
                   for offset in range(0, seconds + 1, self.CADENCE)]
        self.assertTrue(all(records))
        return records

    def forge(self, record, **changes):
        """A hand-written record the writer would refuse, in the writer's own field order."""
        forged = {**record, 'eventID': uuid7(self.now), **changes}
        self.assertEqual(tuple(forged), FIELDS)
        atomic_json(self.store.events / f"codex-skills-{forged['eventID']}.json", forged)

    def report(self, harnesses=('claude',)):
        records, _files = inbox_records(self.store.events)
        return renewal_report(records, self.now, RETENTION_SECONDS, harnesses)

    def check_process(self, *arguments):
        script = Path(__file__).resolve().with_name('observe_agent_activity.py')
        return subprocess.run([sys.executable, str(script), 'renewal-check', '--state-root', str(self.code), *arguments],
                              capture_output=True, text=True, timeout=30)

    def check(self, *arguments):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(['renewal-check', '--state-root', str(self.code), *arguments])
        return code, json.loads(stdout.getvalue())

    def test_renewals_inside_the_lease_pass_and_an_open_expired_run_is_not_a_failure(self):
        span = 2 * LEASE_SECONDS
        start = self.now - dt.timedelta(seconds=span + LEASE_SECONDS + self.CADENCE)
        self.renewals(start, span)
        window = 3 * span
        self.renewals(self.now - dt.timedelta(seconds=window + span + LEASE_SECONDS), span)  # Wholly before the window.
        code, report = self.check('--window', str(window), '--harness', 'claude')
        self.assertEqual((code, report['verdict']), (0, 'pass'))
        counts = report['harnesses']['claude']
        self.assertEqual((counts['runs'], counts['qualifyingRuns'], counts['lapses'], counts['expiredOpen']), (1, 1, 0, 1))
        self.assertEqual(counts['liveRecords'], span // self.CADENCE + 1)
        self.assertEqual((counts['maxGapSeconds'], counts['maxLiveSpanSeconds']), (self.CADENCE, span))
        self.assertGreater(report['files']['outOfWindow'], 0)

    def test_a_gap_past_the_lease_between_live_records_fails(self):
        first = self.renewals(self.now - dt.timedelta(seconds=4 * LEASE_SECONDS), LEASE_SECONDS)[-1]
        late = dt.datetime.fromisoformat(first['leaseExpiresAt'].replace('Z', '+00:00')) + dt.timedelta(seconds=1)
        self.assertTrue(self.store.emit(ACCOUNT, 'claude', first['sessionID'], 'working', 'claude-inventory', now=late))
        code, report = self.check('--harness', 'claude')
        self.assertEqual((code, report['verdict']), (3, 'fail'))
        self.assertEqual(report['harnesses']['claude']['lapses'], 1)

    def boundary_run(self, window, renewal_delay, span):
        """One live record just before the window, then the same run renewed inside it after renewal_delay."""
        before = self.now - dt.timedelta(seconds=window + self.CADENCE // 3)
        first = self.store.emit(ACCOUNT, 'claude', str(uuid.uuid4()), 'working', 'claude-inventory', start=True,
                                now=before)
        return self.renewals(before + dt.timedelta(seconds=renewal_delay), span,
                             session=first['sessionID'], new_run=False)

    def test_a_lapse_across_the_window_start_fails(self):
        window = 2 * LEASE_SECONDS + 3 * self.CADENCE
        inside = self.boundary_run(window, LEASE_SECONDS + self.CADENCE // 3, LEASE_SECONDS + self.CADENCE)
        code, report = self.check('--window', str(window), '--harness', 'claude')
        self.assertEqual((code, report['verdict']), (3, 'fail'))
        counts = report['harnesses']['claude']
        self.assertEqual((counts['runs'], counts['lapses'], counts['liveRecords']), (1, 1, len(inside)))

    def test_a_covered_window_start_is_not_a_lapse_and_does_not_lengthen_the_chain(self):
        window = 2 * LEASE_SECONDS + 3 * self.CADENCE
        inside = self.boundary_run(window, self.CADENCE, LEASE_SECONDS)
        code, report = self.check('--window', str(window), '--harness', 'claude')
        counts = report['harnesses']['claude']
        self.assertEqual((code, counts['lapses'], counts['runs'], counts['liveRecords']), (4, 0, 1, len(inside)))
        # Counting the record before the window would stretch this chain past the lease and qualify it.
        self.assertEqual((counts['qualifyingRuns'], counts['maxLiveSpanSeconds']), (0, LEASE_SECONDS))

    def test_wrongly_typed_fields_are_malformed_not_a_crash(self):
        template = self.store.emit(ACCOUNT, 'claude', SESSION, 'working', 'claude-inventory', start=True, now=self.now)
        (self.store.events / f"codex-skills-{template['eventID']}.json").unlink()
        changes = [{'state': []}, {'state': {}}, {'state': 5}, {'harness': []}, {'observedAt': 5}, {'eventID': {}},
                   {'producerID': []}, {'schemaVersion': True}]
        for change in changes:
            atomic_json(self.store.events / f'codex-skills-{uuid.uuid4()}.json', {**template, **change})
        result = self.check_process()
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn(str(self.code), result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report['verdict'], report['files']['malformed']), ('inconclusive', len(changes)))

    def test_duplicate_event_ids_count_once(self):
        records = self.renewals(self.now - dt.timedelta(seconds=3 * LEASE_SECONDS), 2 * LEASE_SECONDS)
        atomic_json(self.store.events / f'codex-skills-{uuid.uuid4()}.json', records[-1])
        counts = self.report()['harnesses']['claude']
        self.assertEqual((counts['runs'], counts['liveRecords']), (1, len(records)))

    def test_a_future_record_is_out_of_window(self):
        records = self.renewals(self.now - dt.timedelta(seconds=3 * LEASE_SECONDS), 2 * LEASE_SECONDS)
        later = self.now + dt.timedelta(seconds=self.CADENCE)
        self.forge(records[-1], observedAt=timestamp(later),
                   leaseExpiresAt=timestamp(later + dt.timedelta(seconds=LEASE_SECONDS)))
        report = self.report()
        self.assertEqual(report['files']['outOfWindow'], 1)
        self.assertEqual(report['harnesses']['claude']['liveRecords'], len(records))

    def test_empty_inbox_and_short_runs_are_inconclusive(self):
        code, report = self.check()
        self.assertEqual((code, report['verdict']), (4, 'inconclusive'))
        self.renewals(self.now - dt.timedelta(seconds=LEASE_SECONDS), LEASE_SECONDS - self.CADENCE)
        code, report = self.check('--harness', 'claude')
        self.assertEqual((code, report['verdict']), (4, 'inconclusive'))
        self.assertEqual(report['harnesses']['claude']['qualifyingRuns'], 0)

    def test_both_harnesses_must_pass(self):
        self.renewals(self.now - dt.timedelta(seconds=3 * LEASE_SECONDS), 2 * LEASE_SECONDS)
        self.renewals(self.now - dt.timedelta(seconds=LEASE_SECONDS), self.CADENCE, harness='codex')
        code, report = self.check()
        self.assertEqual((code, report['verdict']), (4, 'inconclusive'))
        self.assertEqual({h: c['verdict'] for h, c in report['harnesses'].items()},
                         {'claude': 'pass', 'codex': 'inconclusive'})
        self.assertEqual(self.check('--harness', 'claude')[0], 0)

    def test_a_lease_mismatch_fails(self):
        last = self.renewals(self.now - dt.timedelta(seconds=4 * LEASE_SECONDS), 2 * LEASE_SECONDS)[-1]
        observed = dt.datetime.fromisoformat(last['observedAt'].replace('Z', '+00:00')) + dt.timedelta(seconds=1)
        self.forge(last, observedAt=timestamp(observed),
                   leaseExpiresAt=timestamp(observed + dt.timedelta(seconds=LEASE_SECONDS - 1)))
        report = self.report()
        self.assertEqual(report['verdict'], 'fail')
        self.assertEqual(report['harnesses']['claude']['leaseMismatches'], 1)

    def test_quiet_long_after_a_terminal_record_is_not_a_lapse(self):
        start = self.now - dt.timedelta(seconds=10 * LEASE_SECONDS)
        last = self.renewals(start, 2 * LEASE_SECONDS)[-1]
        ended = dt.datetime.fromisoformat(last['observedAt'].replace('Z', '+00:00')) + dt.timedelta(seconds=self.CADENCE)
        done = self.store.emit(ACCOUNT, 'claude', last['sessionID'], 'completed', 'claude-inventory', now=ended)
        later = ended + dt.timedelta(seconds=5 * LEASE_SECONDS)
        self.forge(done, state='quiet', observedAt=timestamp(later),
                   leaseExpiresAt=timestamp(later + dt.timedelta(seconds=LEASE_SECONDS)))
        report = self.report()
        self.assertEqual(report['verdict'], 'pass')
        self.assertEqual((report['harnesses']['claude']['lapses'], report['harnesses']['claude']['expiredOpen']), (0, 0))

    def test_cli_output_holds_no_ids_or_file_content(self):
        records = self.renewals(self.now - dt.timedelta(seconds=3 * LEASE_SECONDS), 2 * LEASE_SECONDS, session=SESSION)
        (self.store.events / f'codex-skills-{uuid.uuid4()}.json').write_text(SECRET)
        (self.store.events / 'foreign.json').write_text(json.dumps({'note': SECRET}))
        result = self.check_process('--harness', 'claude')
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout + result.stderr
        identities = {SECRET, ACCOUNT, str(self.code)}
        for record in records:
            identities.update(record[name] for name in ('eventID', 'sessionID', 'runID'))
        self.assertEqual([value for value in identities if value in output], [])
        self.assertEqual(json.loads(result.stdout)['files'], {'outOfWindow': 0, 'ignored': 1, 'malformed': 1})

    def test_window_bounds_are_usage_errors(self):
        for window in (LEASE_SECONDS, RETENTION_SECONDS + 1):
            with self.subTest(window=window), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(['renewal-check', '--state-root', str(self.code), '--window', str(window)])
                self.assertEqual(raised.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
