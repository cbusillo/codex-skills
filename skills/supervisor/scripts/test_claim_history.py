# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import claim_history
import session_record


def records(session, parent=None):
    meta = {"id": session, "cwd": "/repo"}
    if parent:
        meta.update(parent_thread_id=parent, source={"subagent": {"thread_spawn": {"parent_thread_id": parent}}})
    return [{"type": "session_meta", "timestamp": "2026-01-01T00:00:00Z", "payload": meta},
            {"type": "event_msg", "timestamp": "2026-01-01T00:00:00Z",
             "payload": {"type": "agent_message", "message": "Safe to exit: yes"}},
            {"type": "event_msg", "timestamp": "2026-01-01T00:00:00Z", "payload": {"type": "task_complete"}}]


class NativeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home / "sessions" / "root.jsonl"
        self.child = self.home / "sessions" / "child.jsonl"
        self.root.parent.mkdir()
        self.write(self.root, records("root"))
        self.write(self.child, records("child", parent="root"))
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

    def status(self, path=None, session="root"):
        return session_record.session_status({"session_id": session, "harness": "codex", "transcript": str(path or self.root)})

    def fork_rows(self, parent_rows, parent='root', child='child'):
        local = records(child, parent=parent)
        inherited = copy.deepcopy(parent_rows)
        inherited[0]['payload']['source'] = 'vscode'
        boundary = len(inherited) + 1
        local[0]['payload'].update(forked_from_id=parent, subagent_history_start_ordinal=boundary)
        marker = {'type': 'event_msg', 'payload': {'type': 'thread_settings_applied', 'thread_id': child}}
        result = [local[0], *inherited, marker, *local[1:]]
        for index, row in enumerate(result):
            row['ordinal'] = index
        return result

    def test_fork_reads_only_its_own_local_rows_as_the_root(self):
        parent = records('root')
        rows = self.fork_rows(parent)
        self.write(self.root, parent)
        self.write(self.child, rows)
        boundary = rows[0]['payload']['subagent_history_start_ordinal']
        self.assertEqual(claim_history.root_records(self.child, 'codex', 'child', rows), [rows[0], *rows[boundary:]])
        self.assertTrue(self.status(self.child, session='child')['safe_verdict'])

    def test_fork_root_cannot_borrow_another_identity_path_or_parent_edge(self):
        parent = records('root')
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
        parent = records('root')
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
        rows = records('root')
        rows[0]['payload'].update(forked_from_id='prior-session', forked_from_ordinal_exclusive=55,
                                  source='vscode', originator='codex-tui')
        self.write(self.root, rows)
        with sqlite3.connect(self.database) as db:
            db.execute("DELETE FROM thread_spawn_edges")
            db.execute("DELETE FROM threads WHERE id='child'")
            db.execute("UPDATE threads SET source='vscode' WHERE id='root'")
        self.assertEqual(claim_history.root_records(self.root, 'codex', 'root', rows), rows)
        self.assertTrue(self.status()['safe_verdict'])
        with self.assertRaises(ValueError):
            claim_history.root_records(self.root, 'codex', 'prior-session', rows)
        rows.insert(1, {'type': 'session_meta', 'payload': {'id': 'prior-session', 'source': 'vscode'}})
        self.write(self.root, rows)
        with self.assertRaises(ValueError):
            claim_history.root_records(self.root, 'codex', 'root', rows)
        with self.assertRaises(ValueError):
            self.status()

    def test_malformed_fork_boundary_or_identity_is_refused_as_the_root(self):
        parent = records('root')
        original = self.fork_rows(parent)
        self.write(self.root, parent)
        self.write(self.child, original)
        self.status(self.child, session='child')
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
                with self.assertRaises(ValueError):
                    self.status(self.child, session='child')


if __name__ == "__main__":
    unittest.main()
