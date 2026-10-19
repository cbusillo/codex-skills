# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise native child indexes and local no-GitHub shutdown acceptance."""
import copy
import json
from pathlib import Path
import sqlite3
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


def completed_child_records(harness, repo="example/a"):
    """Native completion formats bind an exact child turn, not its later file."""
    source = claim(repo)
    source["claim"]["session"] = "child" if harness == "codex" else "root"
    child = records(harness, source["claim"]["session"], source, parent="root" if harness == "codex" else None)
    parent = records(harness, "root", delegate=True)
    if harness == "codex":
        child[-1]["payload"]["turn_id"] = "completed-turn"
        parent[-2:-2] = [{"type": "event_msg", "payload": {"type": "item_completed", "thread_id": "root",
            "item": {"type": "SubAgentActivity", "kind": "completed", "agent_thread_id": "child",
                     "id": "subagent-completed-completed-turn"}}}]
    else:
        parent[2]["toolUseResult"] = {"agentId": "child", "status": "completed",
                                     "content": child[-1]["message"]["content"]}
    return parent, child


class NativeHistoryTests(unittest.TestCase):
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

    def preceding_claims(self, node, sessions=('root',)):
        return {(source['repository'], source['record'], source['claim_comment'])
                for segment in node.get('preceding_histories', [])
                for session in sessions
                for source in claim_history.claim_receipts.sources(segment['records'], segment['harness'], session)[0]}

    def test_completed_child_prefix_excludes_claims_from_later_resumed_turns(self):
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                parent, child = completed_child_records(harness)
                later = claim("example/b", 2, 20)
                later["claim"]["session"] = "child" if harness == "codex" else "root"
                resumed = records(harness, later["claim"]["session"], later, parent="root" if harness == "codex" else None)
                if harness == "claude":
                    resumed[-1]["message"]["content"] = [{"type": "text", "text": "Later turn finished"}]
                child.extend(resumed[1:])
                root = self.root if harness == "codex" else self.home / "claude" / "root.jsonl"
                path = self.child if harness == "codex" else root.parent / "root" / "subagents" / "agent-child.jsonl"
                self.write(root, parent)
                self.write(path, child)
                nodes = claim_history.collect(root, harness, "root", parent, lambda p, *_: session_record.read_jsonl(p))
                segments = claim_history.completed_prefixes(nodes[0], parent, nodes)
                self.assertEqual(self.preceding_claims({"preceding_histories": segments}, sessions=("root", "child")),
                                 {("example/a", 1, 10)})
                # A completion with an unrelated turn/child/call cannot supply
                # the otherwise quiet child's records as an earlier prefix.
                if harness == "codex":
                    parent[-3]["payload"]["item"]["id"] = "subagent-completed-unrelated-turn"
                else:
                    parent[2]["message"]["content"][0]["tool_use_id"] = "unrelated-call"
                self.assertFalse(claim_history.completed_prefixes(nodes[0], parent, nodes))

    def test_later_sibling_inherits_only_natively_completed_child_claims(self):
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                parent, child = completed_child_records(harness)
                launch = records(harness, "root", delegate=True)[1:3]
                if harness == "codex":
                    launch[0]["payload"]["call_id"] = "spawn-sibling"
                    launch[1]["payload"].update(call_id="spawn-sibling", output='{"agent_id":"sibling"}')
                    sibling = self.home / "sessions" / "sibling.jsonl"
                    with sqlite3.connect(self.database) as db:
                        self.edge(db, "root", "sibling", sibling)
                else:
                    launch[0]["message"]["content"][0]["id"] = "spawn-sibling"
                    launch[1]["message"]["content"][0]["tool_use_id"] = "spawn-sibling"
                    launch[1]["toolUseResult"] = {"agentId": "sibling"}
                    sibling = self.home / "claude" / "root" / "subagents" / "agent-sibling.jsonl"
                end = 2 if harness == "codex" else 1
                parent[-end:-end] = launch
                root = self.root if harness == "codex" else self.home / "claude" / "root.jsonl"
                path = self.child if harness == "codex" else root.parent / "root" / "subagents" / "agent-child.jsonl"
                self.write(root, parent)
                self.write(path, child)
                self.write(sibling, records(harness, "sibling" if harness == "codex" else "root",
                                            parent="root" if harness == "codex" else None))
                nodes = claim_history.collect(root, harness, "root", parent, lambda p, *_: session_record.read_jsonl(p))
                node = next(n for n in nodes if n["transcript"] == str(sibling.resolve()))
                self.assertEqual(self.preceding_claims(node, sessions=("root", "child")), {("example/a", 1, 10)})

    def test_sequential_completed_siblings_do_not_multiply_ancestor_prefixes(self):
        for harness in ('codex', 'claude'):
            with self.subTest(harness=harness):
                count = 12
                end = 2 if harness == 'codex' else 1
                root = self.root if harness == 'codex' else self.home / 'claude' / 'root.jsonl'
                parent = records(harness, 'root')
                body = parent[:-end]
                if harness == 'codex':
                    with sqlite3.connect(self.database) as db:
                        db.execute('DELETE FROM thread_spawn_edges')
                        db.execute("DELETE FROM threads WHERE id != 'root'")
                for index in range(count):
                    key = 'child-' + str(index)
                    launch, child = completed_child_records(harness)
                    launch = json.loads(json.dumps(launch).replace('"child"', json.dumps(key)).replace('"spawn"', json.dumps('spawn-' + str(index))))
                    child = json.loads(json.dumps(child).replace('"child"', json.dumps(key))) if harness == 'codex' else child
                    if harness == 'codex':
                        launch[2]['payload']['output'] = json.dumps({'agent_id': key})
                    body.extend(launch[1:-end])
                    path = (self.home / 'sessions' / (key + '.jsonl') if harness == 'codex'
                            else root.parent / 'root' / 'subagents' / ('agent-' + key + '.jsonl'))
                    self.write(path, child)
                    if harness == 'codex':
                        with sqlite3.connect(self.database) as db:
                            self.edge(db, 'root', key, path)
                parent = body + parent[-end:]
                self.write(root, parent)
                nodes = claim_history.collect(root, harness, 'root', parent, lambda p, *_: session_record.read_jsonl(p))
                for node in nodes[1:]:
                    prefixes = node['preceding_histories']
                    identities = [(p['harness'], tuple(map(id, p['records']))) for p in prefixes]
                    self.assertEqual(len(identities), len(set(identities)))
                    self.assertLessEqual(len(prefixes), 2 * count)
                completed = claim_history.completed_prefixes(nodes[0], parent, nodes)
                identities = [(p['harness'], tuple(map(id, p['records']))) for p in completed]
                self.assertEqual(len(identities), len(set(identities)))
                # Every native child completion is still present, once.
                completed_ends = {id(p['records'][-1]) for p in completed if p['records']}
                self.assertTrue(all(id(n['records'][-1]) in completed_ends for n in nodes[1:]))

    def test_claude_completed_handback_binds_its_terminal_tool_result(self):
        parent, child = completed_child_records("claude")
        use = {"type": "assistant", "sessionId": "root", "message": {"content": [
            {"type": "tool_use", "id": "handback", "name": "SubagentHandback", "input": {"message": "Finished child work"}}]}}
        returned = {"type": "user", "sessionId": "root", "toolEndsTurn": True, "message": {"content": [
            {"type": "tool_result", "tool_use_id": "handback", "content": "returned"}]}}
        child[-1:-1] = [use, returned]
        parent[2]["toolUseResult"].pop("content")
        parent[2]["toolUseResult"]["handbackReport"] = {"text": "Finished child work"}
        root = self.home / "claude" / "root.jsonl"
        path = root.parent / "root" / "subagents" / "agent-child.jsonl"
        self.write(root, parent)
        self.write(path, child)
        nodes = claim_history.collect(root, "claude", "root", parent, lambda p, *_: session_record.read_jsonl(p))
        prefixes = claim_history.completed_prefixes(nodes[0], parent, nodes)
        self.assertEqual(self.preceding_claims({"preceding_histories": prefixes}), {("example/a", 1, 10)})
        returned["message"]["content"][0]["tool_use_id"] = "unrelated-handback"
        nodes[1]["records"] = child
        self.assertFalse(claim_history.completed_prefixes(nodes[0], parent, nodes))

    def test_completed_child_includes_only_its_bound_completed_grandchild_prefix(self):
        parent, child = completed_child_records("codex")
        child = records("codex", "child", parent="root")
        child[-1]["payload"]["turn_id"] = "completed-turn"
        value = claim("example/b", 2, 20)
        value["claim"]["session"] = "grand"
        grand = records("codex", "grand", value, parent="child")
        grand[-1]["payload"]["turn_id"] = "grand-turn"
        child[-2:-2] = [{"type": "event_msg", "payload": {"type": "item_completed", "thread_id": "child",
            "item": {"type": "SubAgentActivity", "kind": "completed", "agent_thread_id": "grand",
                     "id": "subagent-completed-grand-turn"}}}]
        path = self.home / "sessions" / "grand.jsonl"
        self.write(self.root, parent)
        self.write(self.child, child)
        self.write(path, grand)
        with sqlite3.connect(self.database) as db:
            self.edge(db, "child", "grand", path)
        nodes = self.nodes()
        prefixes = claim_history.completed_prefixes(nodes[0], parent, nodes)
        self.assertEqual(self.preceding_claims({"preceding_histories": prefixes}, sessions=("root", "child", "grand")),
                         {("example/b", 2, 20)})

    def test_plain_spawn_precedence_ends_at_the_bound_launch_not_its_result(self):
        early, late = claim(), claim('example/b', 2, 20)
        for value in (early, late):
            value['claim']['session'] = 'root'
        root = records('codex', 'root', early, late, delegate=True)
        launch, result = root[1:3]
        root[1:] = [*root[3:5], launch, *root[5:7], result, *root[7:]]
        self.write(self.root, root)
        child = next(node for node in self.nodes() if node['session_id'] == 'child')
        self.assertEqual(self.preceding_claims(child), {('example/a', 1, 10)})

    def test_fork_precedence_uses_verified_inherited_snapshot_only(self):
        early, late = claim(), claim('example/b', 2, 20)
        for value in (early, late):
            value['claim']['session'] = 'root'
        snapshot = records('codex', 'root', early)
        self.write(self.root, records('codex', 'root', early, late))
        self.write(self.child, self.fork_rows(snapshot))
        child = next(node for node in self.nodes() if node['session_id'] == 'child')
        self.assertEqual(self.preceding_claims(child), {('example/a', 1, 10)})

    def test_unbound_or_foreign_spawn_result_does_not_supply_claim_precedence(self):
        value = claim()
        value['claim']['session'] = 'root'
        original = records('codex', 'root', value, delegate=True)
        launch, result = original[1:3]
        original[1:] = [*original[3:5], launch, result, *original[5:]]
        for mutation in ('missing', 'foreign-child', 'foreign-call', 'read-call'):
            with self.subTest(mutation=mutation):
                root = copy.deepcopy(original)
                if mutation == 'missing':
                    root.pop(4)
                elif mutation == 'foreign-child':
                    root[4]['payload']['output'] = '{"agent_id":"other-child"}'
                elif mutation == 'foreign-call':
                    root[4]['payload']['call_id'] = 'unrelated-call'
                else:
                    root[3]['payload']['name'] = 'exec_command'
                self.write(self.root, root)
                child = next(node for node in self.nodes() if node['session_id'] == 'child')
                self.assertFalse(self.preceding_claims(child))

    def test_claude_spawn_precedence_binds_native_agent_id_to_its_tool_result(self):
        root = self.home / 'claude' / 'root.jsonl'
        child = root.parent / 'root' / 'subagents' / 'agent-explore.jsonl'
        value = claim()
        value['claim']['session'] = 'root'
        parent = records('claude', 'root', value, delegate=True)
        launch, result = parent[1:3]
        result['toolUseResult'] = {'agentId': 'explore'}
        parent[1:] = [*parent[3:5], launch, result, *parent[5:]]
        self.write(root, parent)
        self.write(child, records('claude', 'root'))
        nodes = claim_history.collect(root, 'claude', 'root', parent, lambda p, *_: session_record.read_jsonl(p))
        self.assertEqual(self.preceding_claims(nodes[1]), {('example/a', 1, 10)})
        parent[4]['toolUseResult']['agentId'] = 'other-agent'
        nodes = claim_history.collect(root, 'claude', 'root', parent, lambda p, *_: session_record.read_jsonl(p))
        self.assertFalse(self.preceding_claims(nodes[1]))
        parent[4]['toolUseResult']['agentId'] = 'explore'
        parent[4]['message']['content'][0]['tool_use_id'] = 'unrelated-call'
        nodes = claim_history.collect(root, 'claude', 'root', parent, lambda p, *_: session_record.read_jsonl(p))
        self.assertFalse(self.preceding_claims(nodes[1]))

    def test_claude_resume_does_not_prove_a_claim_precedes_child_creation(self):
        root = self.home / 'claude' / 'root.jsonl'
        child = root.parent / 'root' / 'subagents' / 'agent-explore.jsonl'
        value = claim()
        value['claim']['session'] = 'root'
        parent = records('claude', 'root', value, delegate=True)
        launch, result = parent[1:3]
        launch['message']['content'][0]['input']['resume'] = 'explore'
        result['toolUseResult'] = {'agentId': 'explore'}
        parent[1:] = [*parent[3:5], launch, result, *parent[5:]]
        self.write(root, parent)
        self.write(child, records('claude', 'root'))
        nodes = claim_history.collect(root, 'claude', 'root', parent, lambda p, *_: session_record.read_jsonl(p))
        self.assertFalse(self.preceding_claims(nodes[1]))

    def test_native_task_name_spawn_binding_requires_matching_index_and_child_metadata(self):
        value = claim()
        value['claim']['session'] = 'root'
        parent = records('codex', 'root', value, delegate=True)
        launch, result = parent[1:3]
        result['payload']['output'] = '{"task_name":"/root/explore"}'
        parent[1:] = [*parent[3:5], launch, result, *parent[5:]]
        child = records('codex', 'child', parent='root')
        child[0]['payload']['agent_path'] = '/root/explore'
        child[0]['payload']['source']['subagent']['thread_spawn']['agent_path'] = '/root/explore'
        self.write(self.root, parent)
        self.write(self.child, child)
        with sqlite3.connect(self.database) as db:
            source = {'subagent': {'thread_spawn': {'parent_thread_id': 'root', 'agent_path': '/root/explore'}}}
            db.execute("UPDATE threads SET source=? WHERE id='child'", (json.dumps(source),))
        self.assertEqual(self.preceding_claims(self.nodes()[1]), {('example/a', 1, 10)})
        child[0]['payload']['agent_path'] = '/root/unrelated'
        self.write(self.child, child)
        self.assertFalse(self.preceding_claims(self.nodes()[1]))
        child[0]['payload']['agent_path'] = '/root/explore'
        self.write(self.child, child)
        parent[4]['payload']['output'] = '{"task_name":"/root/explore","agent_id":"other-child"}'
        self.write(self.root, parent)
        self.assertFalse(self.preceding_claims(self.nodes()[1]))

    def test_transitive_precedence_keeps_parent_and_child_call_namespaces_separate(self):
        root_claim, child_claim = claim(), claim('example/b', 2, 20)
        root_claim['claim']['session'] = 'root'
        child_claim['claim']['session'] = 'child'
        root = records('codex', 'root', root_claim, delegate=True)
        child = records('codex', 'child', child_claim, parent='root', delegate=True)
        for rows, descendant in ((root, 'child'), (child, 'grand')):
            launch, result = rows[1:3]
            result['payload']['output'] = json.dumps({'agent_id': descendant})
            rows[1:] = [*rows[3:5], launch, result, *rows[5:]]
        grand = self.home / 'sessions' / 'grand.jsonl'
        self.write(self.root, root)
        self.write(self.child, child)
        self.write(grand, records('codex', 'grand', parent='child'))
        with sqlite3.connect(self.database) as db:
            self.edge(db, 'child', 'grand', grand)
        node = next(node for node in self.nodes() if node['session_id'] == 'grand')
        self.assertEqual(self.preceding_claims(node, sessions=('root', 'child')),
                         {('example/a', 1, 10), ('example/b', 2, 20)})

    def test_nested_fork_precedence_omits_ambiguous_copied_call_namespaces(self):
        root_claim, child_claim = claim(), claim('example/b', 2, 20)
        root_claim['claim']['session'] = 'root'
        child_claim['claim']['session'] = 'child'
        root = records('codex', 'root', root_claim)
        child = self.fork_rows(root, child_claim)
        grand = self.home / 'sessions' / 'grand.jsonl'
        self.write(self.root, root)
        self.write(self.child, child)
        self.write(grand, self.fork_rows(child, parent='child', child='grand'))
        with sqlite3.connect(self.database) as db:
            self.edge(db, 'child', 'grand', grand)
        node = next(node for node in self.nodes() if node['session_id'] == 'grand')
        # The root proof has its own validated segment. The parent claim is
        # present only in a nested snapshot sharing tool IDs with that root.
        self.assertEqual(self.preceding_claims(node, sessions=('root', 'child')), {('example/a', 1, 10)})

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
