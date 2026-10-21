# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise native child indexes and local no-GitHub shutdown acceptance."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import claim_history
import session_record
from test_claim_receipts import history, claim, proof, output


def records(harness, session, *values, parent=None, delegate=False):
    rows = history(harness, *values)
    for row in rows:
        row["timestamp"] = "2026-01-01T00:00:00Z"
        if harness == "claude":
            row["sessionId"] = session
        elif row.get("type") == "session_meta":
            row["payload"]["id"] = session
            if parent:
                row["payload"].update(parent_thread_id=parent, source={"subagent": {"thread_spawn": {"parent_thread_id": parent}}})
    if delegate:
        row = ({"type": "response_item", "payload": {"type": "function_call", "name": "spawn_agent", "call_id": "spawn", "arguments": "{}"}}
               if harness == "codex" else {"type": "assistant", "sessionId": session, "message": {"content": [
                   {"type": "tool_use", "name": "Agent", "id": "spawn", "input": {"prompt": "explore"}}]}})
        rows.insert(1, row)
        result = ({"type": "response_item", "payload": {"type": "function_call_output", "call_id": "spawn", "output": '{"agent_id":"child"}'}}
                  if harness == "codex" else {"type": "user", "sessionId": session, "message": {"content": [
                      {"type": "tool_result", "tool_use_id": "spawn", "content": "child completed"}]}})
        rows.insert(2, result)
    return rows


class NativeHistoryTests(unittest.TestCase):
    def test_native_handback_ends_only_the_bound_quiet_child(self):
        fixture = Path(__file__).with_name('fixtures') / 'claude-handback-ended.jsonl'
        original = session_record.read_jsonl(fixture)
        session = original[0]['sessionId']
        root = self.home / (session + '.jsonl')
        child = self.home / session / 'subagents/agent-handback.jsonl'
        for change in ('none', 'cancelled-stop', 'failed', 'error', 'wrong-call', 'wrong-assistant', 'parallel',
                       'queued', 'resumed', 'blocked-hook', 'unknown-tail', 'background'):
            with self.subTest(change=change):
                rows = copy.deepcopy(original)
                result = rows[1]
                if change == 'failed': result['toolUseResult']['success'] = False
                elif change == 'cancelled-stop':
                    rows[-1]['attachment'].update(type='hook_cancelled', hookEvent='SubagentStop', timedOut=True, timeoutMs=3000)
                elif change == 'error': result['message']['content'][0]['is_error'] = True
                elif change == 'wrong-call': result['message']['content'][0]['tool_use_id'] = 'another'
                elif change == 'wrong-assistant': result['sourceToolAssistantUUID'] = 'another'
                elif change == 'parallel':
                    rows[0]['message']['content'].append({'type': 'tool_use', 'id': 'unfinished', 'name': 'Bash', 'input': {'command': 'worker'}})
                elif change == 'queued': rows.append({'type': 'queue-operation', 'sessionId': session, 'operation': 'enqueue'})
                elif change == 'resumed': rows.append({'type': 'system', 'sessionId': session, 'subtype': 'resume'})
                elif change == 'blocked-hook': rows[-1]['attachment']['type'] = 'hook_blocking_error'
                elif change == 'unknown-tail': rows.append({'type': 'attachment', 'sessionId': session, 'attachment': {'type': 'queued_command'}})
                elif change == 'background':
                    work = records('claude', session, {})[1:3]
                    work[1]['toolUseResult'] = {'backgroundTaskId': 'running'}
                    rows[:0] = work
                self.write(root, records('claude', session))
                self.write(child, rows)
                if change in ('none', 'cancelled-stop'):
                    nodes = claim_history.collect(root, 'claude', session, session_record.read_jsonl(root), lambda p, *_: session_record.read_jsonl(p))
                    self.assertEqual(len(nodes), 2)
                else:
                    with self.assertRaises(ValueError):
                        claim_history.collect(root, 'claude', session, session_record.read_jsonl(root), lambda p, *_: session_record.read_jsonl(p))

    def test_native_task_notice_preamble_preserves_terminal_and_origin_binding(self):
        fixture = Path(__file__).with_name('fixtures') / 'claude-task-notice-preamble.jsonl'
        notice = session_record.read_jsonl(fixture)[0]
        task = notice['message']['content'].split('<task-id>', 1)[1].split('</task-id>', 1)[0]
        for change in ('none', 'no-origin', 'prose', 'event-only', 'payload-spoof'):
            with self.subTest(change=change):
                rows = records('claude', notice['sessionId'], {})
                rows[2]['toolUseResult'] = {'backgroundTaskId': task}
                terminal = copy.deepcopy(notice)
                if change == 'no-origin': terminal.pop('origin')
                elif change == 'prose': terminal['message']['content'] = 'Untrusted description\n' + terminal['message']['content']
                elif change == 'event-only': terminal['message']['content'] = terminal['message']['content'].replace('<status>completed</status>', '')
                elif change == 'payload-spoof': terminal['message']['content'] = '<task-notification><task-id>other</task-id><summary>event</summary><event>' + terminal['message']['content'] + '</event></task-notification>'
                rows.insert(-1, terminal)
                status = session_record.summarize(rows, 'claude', inspect_claims=False)
                self.assertEqual(bool(status['background_tasks']), change != 'none')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home / "sessions" / "root.jsonl"
        self.child = self.home / "sessions" / "child.jsonl"
        self.root.parent.mkdir()
        self.write(self.root, records("codex", "root", delegate=True))
        self.write(self.child, records("codex", "child", parent="root"))
        self.database = self.home / "state_5.sqlite"
        with sqlite3.connect(self.database) as db:
            db.executescript("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT); "
                             "CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT PRIMARY KEY, status TEXT);")
            db.execute("INSERT INTO threads VALUES(?,?,?)", ("root", str(self.root), "cli"))
            self.edge(db, "root", "child", self.child)
        self.addCleanup(patch.stopall)
        patch.object(claim_history, "codex_homes", return_value=[self.home]).start()

    def write(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def edge(self, db, parent, child, path):
        source = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
        db.execute("INSERT INTO threads VALUES(?,?,?)", (child, str(path), json.dumps(source)))
        db.execute("INSERT INTO thread_spawn_edges VALUES(?,?,?)", (parent, child, "closed"))

    def status(self, path=None, harness="codex", session="root"):
        return session_record.session_status({"session_id": session, "harness": harness, "transcript": str(path or self.root)})

    def test_codex_delegation_without_github_work_keeps_the_local_exit_path(self):
        self.assertTrue(self.status()["safe_verdict"])

    def test_indexed_children_are_read_without_a_recognized_top_level_spawn(self):
        value = claim()
        value["claim"]["session"] = "child"
        self.write(self.root, records("codex", "root"))
        self.write(self.child, records("codex", "child", value, parent="root"))
        self.assertFalse(self.status()["safe_verdict"])

    def test_transitive_closed_edges_are_included(self):
        grand = self.home / "sessions" / "grand.jsonl"
        self.write(grand, records("codex", "grand", parent="child"))
        with sqlite3.connect(self.database) as db:
            self.edge(db, "child", "grand", grand)
        nodes = claim_history.collect(self.root, "codex", "root", session_record.read_jsonl(self.root),
                                      lambda p, *_: session_record.read_jsonl(p))
        self.assertEqual({n["session_id"] for n in nodes}, {"root", "child", "grand"})

    def test_missing_schema_or_child_rollout_preserves_activity_but_refuses_acceptance(self):
        self.child.unlink()
        result = self.status()
        self.assertTrue(result["at_turn_end"])
        self.assertTrue(result["declared_safe_verdict"])
        self.assertFalse(result["safe_verdict"])
        self.assertEqual(result["verdict_recognition"], session_record.CLAIM_VERDICT_REFUSED)
        with sqlite3.connect(self.database) as db:
            db.execute("DROP TABLE thread_spawn_edges")
        self.assertFalse(self.status()["claim_disposition"]["clear"])

    def test_a_child_with_wrong_parent_or_running_work_is_not_clear(self):
        self.write(self.child, records("codex", "child", parent="another-parent"))
        self.assertFalse(self.status()["safe_verdict"])
        rows = records("codex", "child", parent="root")
        rows.append({"type": "event_msg", "payload": {"type": "task_started"}})
        self.write(self.child, rows)
        self.assertFalse(self.status()["safe_verdict"])

    def test_malformed_native_child_source_refuses_without_losing_turn_status(self):
        malformed = ({"subagent": "worker"}, {"subagent": {"thread_spawn": "worker"}})
        for source in malformed:
            with self.subTest(index_source=source):
                with sqlite3.connect(self.database) as db:
                    db.execute("UPDATE threads SET source=? WHERE id='child'", (json.dumps(source),))
                result = self.status()
                self.assertTrue(result["at_turn_end"])
                self.assertTrue(result["declared_safe_verdict"])
                self.assertFalse(result["safe_verdict"])
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE threads SET source=? WHERE id='child'", (
                json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": "root"}}}),))
        for source in ("cli", *malformed):
            with self.subTest(rollout_source=source):
                rows = records("codex", "child", parent="root")
                rows[0]["payload"]["source"] = source
                self.write(self.child, rows)
                result = self.status()
                self.assertTrue(result["at_turn_end"])
                self.assertTrue(result["declared_safe_verdict"])
                self.assertFalse(result["safe_verdict"])

    def test_child_owned_and_root_alias_claims_require_a_bound_parent_check(self):
        for owner_session in ("child", "root"):
            with self.subTest(owner_session=owner_session):
                value = claim()
                value["claim"]["session"] = owner_session
                self.write(self.child, records("codex", "child", value, parent="root"))
                self.write(self.root, records("codex", "root", delegate=True))
                self.assertFalse(self.status()["safe_verdict"])
                nodes = claim_history.collect(self.root, "codex", "root", session_record.read_jsonl(self.root),
                                              lambda p, *_: session_record.read_jsonl(p))
                report = {**proof(("example/a", 1, 10)), "session_id": "root",
                          "coverage": {"complete": True, "session_ids": ["root", "child"],
                                       "histories": [{k: n[k] for k in ("session_id", "transcript", "digest")} for n in nodes[1:]]}}
                self.write(self.root, records("codex", "root", report, delegate=True))
                self.assertTrue(self.status()["safe_verdict"])
                changed = records("codex", "child", value, parent="root")
                changed[-2]["payload"]["message"] = "Child completed another turn."
                self.write(self.child, changed)
                self.assertFalse(self.status()["safe_verdict"])

    def test_claude_explore_without_github_needs_only_readable_local_history(self):
        root = self.home / "claude" / "root.jsonl"
        child = root.parent / "root" / "subagents" / "agent-explore.jsonl"
        self.write(root, records("claude", "root", delegate=True))
        self.assertFalse(self.status(root, "claude")["safe_verdict"])
        self.write(child, records("claude", "root"))
        self.assertTrue(self.status(root, "claude")["safe_verdict"])
        value = claim()
        value["claim"]["session"] = "root"
        self.write(child, records("claude", "root", value))
        self.assertFalse(self.status(root, "claude")["safe_verdict"])

    def test_unrelated_deep_data_does_not_change_turn_or_claim_status(self):
        rows = records("codex", "root")
        value = {"operation": "opaque"}
        for _ in range(20):
            value = {"output": value}
        rows.insert(1, output(value, "codex"))
        self.write(self.root, rows)
        result = self.status()
        self.assertTrue(result["at_turn_end"])
        self.assertTrue(result["declared_safe_verdict"])
        self.assertTrue(result["claim_disposition"]["clear"])


    def fork_rows(self, parent_rows, *child_values, parent='root', child='child'):
        local = records('codex', child, *child_values, parent=parent)
        inherited = copy.deepcopy(parent_rows)
        inherited[0]['payload']['source'] = 'vscode'
        boundary = len(inherited) + 1
        local[0]['payload'].update(forked_from_id=parent, subagent_history_start_ordinal=boundary)
        marker = {'type': 'event_msg', 'payload': {'type': 'thread_settings_applied', 'thread_id': child}}
        result = [local[0], *inherited, marker, *local[1:]]
        for index, row in enumerate(result):
            row['ordinal'] = index
        return result

    def nodes(self):
        return claim_history.collect(self.root, 'codex', 'root', session_record.read_jsonl(self.root),
                                     lambda path, *_: session_record.read_jsonl(path))

    def passing_proof(self, nodes, *sources):
        return {**proof(*sources), 'session_id': 'root',
                'coverage': {'complete': True, 'session_ids': ['root', 'child'],
                             'histories': [{key: node[key] for key in ('session_id', 'transcript', 'digest')}
                                           for node in nodes[1:]]}}

    def test_valid_fork_with_inherited_native_metadata_has_local_exit_path(self):
        parent = records('codex', 'root', delegate=True)
        self.write(self.root, parent)
        self.write(self.child, self.fork_rows(parent))
        self.assertTrue(self.status()['safe_verdict'])
        nodes = self.nodes()
        child = next(node for node in nodes if node['session_id'] == 'child')
        self.assertEqual([r['payload']['id'] for r in child['records'] if r.get('type') == 'session_meta'], ['child'])
        self.assertFalse(claim_history.delegated(child['records'], 'codex'))
        github = str(Path(__file__).resolve().parents[2] / 'github/scripts')
        with patch.object(sys, 'path', [github, *sys.path]):
            import github_plan_native
            import github_plan_sessions
            with patch.object(github_plan_sessions, 'observation_homes', return_value=[self.home]):
                homes, copies = github_plan_native.observation_scope('child', self.child)
        self.assertIn(self.home.resolve(), homes)
        self.assertIn(str(self.child.resolve()), copies[str(self.home.resolve())])

    def test_parent_and_child_claims_require_exact_disposition_after_fork(self):
        parent_claim, child_claim = claim(), claim('example/b', 2, 20)
        parent_claim['claim']['session'] = 'root'
        child_claim['claim']['session'] = 'child'
        parent = records('codex', 'root', parent_claim, delegate=True)
        self.write(self.root, parent)
        self.write(self.child, self.fork_rows(parent, child_claim))
        self.assertFalse(self.status()['safe_verdict'])
        nodes = self.nodes()
        child = next(node for node in nodes if node['session_id'] == 'child')
        # Copied parent writer is not evaluated as a child writer/process.
        import claim_receipts
        self.assertFalse(claim_receipts.sources(child['records'], 'codex', 'root')[0])
        self.assertEqual(len(claim_receipts.sources(child['records'], 'codex', 'child')[0]), 1)
        for released in ((('example/a', 1, 10),), (('example/b', 2, 20),)):
            self.write(self.root, records('codex', 'root', parent_claim,
                                         self.passing_proof(nodes, *released), delegate=True))
            self.assertFalse(self.status()['safe_verdict'])
        self.write(self.root, records('codex', 'root', parent_claim,
                                     self.passing_proof(nodes, ('example/a', 1, 10), ('example/b', 2, 20)), delegate=True))
        self.assertTrue(self.status()['safe_verdict'])

    def test_inherited_prefix_change_invalidates_previous_coverage_digest(self):
        parent_claim = claim()
        parent_claim['claim']['session'] = 'root'
        parent = records('codex', 'root', parent_claim, delegate=True)
        child = self.fork_rows(parent)
        self.write(self.root, parent)
        self.write(self.child, child)
        nodes = self.nodes()
        self.write(self.root, records('codex', 'root', parent_claim,
                                     self.passing_proof(nodes, ('example/a', 1, 10)), delegate=True))
        self.assertTrue(self.status()['safe_verdict'])
        child[1]['payload']['cwd'] = '/different-parent-snapshot'
        self.write(self.child, child)
        self.assertFalse(self.status()['safe_verdict'])

    def test_transitive_fork_uses_only_verified_native_ancestors(self):
        parent = records('codex', 'root', delegate=True)
        child = self.fork_rows(parent)
        grand = self.home / 'sessions' / 'grand.jsonl'
        self.write(self.root, parent)
        self.write(self.child, child)
        self.write(grand, self.fork_rows(child, parent='child', child='grand'))
        with sqlite3.connect(self.database) as db:
            self.edge(db, 'child', 'grand', grand)
        self.assertTrue(self.status()['safe_verdict'])
        self.assertEqual({node['session_id'] for node in self.nodes()}, {'root', 'child', 'grand'})

    def test_fork_can_check_its_own_local_work_as_the_root(self):
        parent_claim = claim()
        parent_claim['claim']['session'] = 'root'
        parent = records('codex', 'root', parent_claim, delegate=True)
        self.write(self.root, parent)
        self.write(self.child, self.fork_rows(parent))
        self.assertTrue(self.status(self.child, session='child')['safe_verdict'])
        value = claim('example/b', 2, 20)
        value['claim']['session'] = 'child'
        self.write(self.child, self.fork_rows(parent, value))
        self.assertFalse(self.status(self.child, session='child')['safe_verdict'])
        clear = {**proof(('example/b', 2, 20)), 'session_id': 'child'}
        self.write(self.child, self.fork_rows(parent, value, clear))
        self.assertTrue(self.status(self.child, session='child')['safe_verdict'])

    def test_fork_root_with_its_own_forked_child_preserves_native_ancestor_chain(self):
        parent = records('codex', 'root', delegate=True)
        child = self.fork_rows(parent)
        grand = self.home / 'sessions' / 'grand.jsonl'
        self.write(self.root, parent)
        self.write(self.child, child)
        self.write(grand, self.fork_rows(child, parent='child', child='grand'))
        with sqlite3.connect(self.database) as db:
            self.edge(db, 'child', 'grand', grand)
        self.assertTrue(self.status(self.child, session='child')['safe_verdict'])

    def test_fork_root_cannot_borrow_another_identity_path_or_parent_edge(self):
        parent = records('codex', 'root')
        rows = self.fork_rows(parent)
        self.write(self.root, parent)
        self.write(self.child, rows)
        for session in ('root', 'unrelated-session'):
            with self.subTest(identity=session), self.assertRaises(ValueError):
                self.status(self.child, session=session)
        alternate = self.home / 'sessions' / 'copied-child.jsonl'
        self.write(alternate, rows)
        with self.assertRaises(ValueError):
            self.status(alternate, session='child')
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE thread_spawn_edges SET parent_thread_id='unrelated-session' WHERE child_thread_id='child'")
        with self.assertRaises(ValueError):
            self.status(self.child, session='child')
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE thread_spawn_edges SET parent_thread_id='root' WHERE child_thread_id='child'")
            db.execute("DELETE FROM threads WHERE id='root'")
        with self.assertRaises(ValueError):
            self.status(self.child, session='child')

    def test_bare_native_root_source_does_not_mask_malformed_or_child_sources(self):
        parent = records('codex', 'root')
        self.write(self.root, parent)
        self.write(self.child, self.fork_rows(parent))
        self.assertTrue(self.status(self.child, session='child')['safe_verdict'])
        for malformed in ('{"subagent":', '"unterminated', '[]', 'null'):
            with self.subTest(root_source=malformed):
                with sqlite3.connect(self.database) as db:
                    db.execute("UPDATE threads SET source=? WHERE id='root'", (malformed,))
                with self.assertRaises(ValueError):
                    self.status(self.child, session='child')
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE threads SET source='cli' WHERE id IN ('root','child')")
        with self.assertRaises(ValueError):
            self.status(self.child, session='child')

    def test_user_started_fork_without_inline_parent_history_keeps_plain_identity(self):
        rows = records('codex', 'root')
        rows[0]['payload'].update(forked_from_id='prior-session', forked_from_ordinal_exclusive=55,
                                  source='vscode', originator='codex-tui')
        self.write(self.root, rows)
        with sqlite3.connect(self.database) as db:
            db.execute("DELETE FROM thread_spawn_edges")
            db.execute("DELETE FROM threads WHERE id='child'")
            db.execute("UPDATE threads SET source='vscode' WHERE id='root'")
        self.assertEqual(claim_history.root_records(self.root, 'codex', 'root', rows), rows)
        self.assertEqual(len(self.nodes()), 1)
        self.assertTrue(self.status()['safe_verdict'])
        with self.assertRaises(ValueError):
            claim_history.root_records(self.root, 'codex', 'prior-session', rows)
        rows.insert(1, {'type': 'session_meta', 'payload': {'id': 'prior-session', 'source': 'vscode'}})
        self.write(self.root, rows)
        with self.assertRaises(ValueError):
            claim_history.root_records(self.root, 'codex', 'root', rows)
        with self.assertRaises(ValueError):
            self.status()

    def test_malformed_fork_boundary_or_identity_cannot_hide_local_claims(self):
        parent = records('codex', 'root', delegate=True)
        source = claim()
        source['claim']['session'] = 'child'
        original = self.fork_rows(parent, source)
        self.write(self.root, parent)
        mutations = (
            lambda rows: rows[0]['payload'].update(forked_from_id='other-parent'),
            lambda rows: rows[0]['payload'].update(parent_thread_id='other-parent'),
            lambda rows: rows[0]['payload']['source']['subagent']['thread_spawn'].update(parent_thread_id='other-parent'),
            lambda rows: rows[0]['payload'].update(subagent_history_start_ordinal=True),
            lambda rows: rows[0]['payload'].pop('subagent_history_start_ordinal'),
            lambda rows: rows[0]['payload'].update(subagent_history_start_ordinal=len(rows) - 1),
            lambda rows: rows[1]['payload'].update(id='unrelated-session'),
            lambda rows: rows[-1].pop('ordinal'),
            lambda rows: rows.append({'type': 'session_meta', 'ordinal': len(rows), 'payload': {'id': 'unrelated-session'}}),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                rows = copy.deepcopy(original)
                mutate(rows)
                self.write(self.child, rows)
                result = self.status()
                self.assertFalse(result['safe_verdict'])
                self.assertNotEqual(result['claim_disposition'].get('reason'), 'no_claim_receipts')


if __name__ == "__main__":
    unittest.main()
