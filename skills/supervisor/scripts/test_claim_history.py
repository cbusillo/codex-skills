# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise native child indexes and local no-GitHub shutdown acceptance."""
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
            db.execute("INSERT INTO threads VALUES(?,?,?)", ("root", str(self.root), json.dumps("cli")))
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


if __name__ == "__main__":
    unittest.main()
