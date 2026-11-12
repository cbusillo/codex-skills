#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import argparse
import asyncio
import json
import subprocess
import tempfile
import time
import unittest
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import catalog_landing
import check_changes
import check_operation
import codex_session_proxy
import iterm_tab
import oq
import session_closeout
import session_liveness
import tmux_terminal


def patch_attr(target: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    return patch.object(target, name, *args, **kwargs)


class ChangesTests(unittest.TestCase):
    def test_incomplete_discovery_cannot_remove_records_and_recovers_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            rows = [{"url": "record", "status": "active"}]
            self.assertEqual(check_changes.changes(rows, state, ["source"], key="url", complete=True), rows)
            self.assertEqual(check_changes.changes(rows, state, ["source"], key="url", complete=True), [])
            self.assertEqual(check_changes.changes([], state, ["source"], key="url", complete=False), [])
            self.assertTrue(check_changes.changes([], state, ["source"], key="url", complete=True)[0]["removed"])
            self.assertNotIn("active", state.read_text())

    def test_questions_emit_changes_without_reprinting_private_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            questions: dict[str, Any] = {
                "complete": True,
                "questions": [{"url": "question", "question": "private question", "age_seconds": 1}],
            }
            state = Path(directory) / "questions.json"
            self.assertNotIn("private question", json.dumps(oq.question_changes(questions, state, [])))
            questions["questions"][0]["age_seconds"] = 500
            self.assertEqual(oq.question_changes(questions, state, [])["questions"], [])
            questions["questions"][0]["question"] = "edited question"
            self.assertTrue(oq.question_changes(questions, state, [])["questions"])
            self.assertNotIn("edited question", state.read_text())


class OperationTests(unittest.TestCase):
    def test_unresolved_cell_decision_preserves_the_audit_and_refuses_other_failures(self) -> None:
        transcript = self.root / "source.jsonl"
        transcript.write_text(json.dumps({"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "opaque-cell", "input": "eval(source)"}}) + "\n")
        entry: dict[str, Any] = {"session_id": "native", "harness": "codex", "transcript": str(transcript),
                 "repository": "owner/repo", "issue": 1}
        report = {"ok": False, "claims_clear": False, "session_ids": ["native"],
                  "coverage": {"complete": False, "search_errors": []}, "blocked": [],
                  "released_claims": [{"source_session": "native", "claim_comment": 42}],
                  "handoff_comments": [{"source_session": "native", "comment": {"id": 43}}],
                  "unresolved_invocations": [{"session": "native", "transcript": str(transcript),
                                              "call_id": "opaque-cell", "reason": "computed", "code_mode": True}]}
        args = argparse.Namespace(owner="owner", unresolved_cells_decision=session_closeout.UNRESOLVED_CELLS_DECISION)
        decision = {"user": {"login": "agent[bot]"}, "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z"}
        for obstruction in ("none", "held", "search", "error", "shell", "recovery", "decision", "edited", "wrong_url"):
            with self.subTest(obstruction=obstruction):
                checked = json.loads(json.dumps(report))
                current = dict(entry)
                if obstruction == "held":
                    checked["blocked"] = ["held claim"]
                elif obstruction == "search":
                    checked["coverage"]["search_errors"] = ["unavailable"]
                elif obstruction == "error":
                    checked["error"] = "source read failed"
                elif obstruction == "shell":
                    checked["unresolved_invocations"][0]["code_mode"] = False
                elif obstruction == "recovery":
                    current["recovery"] = {"kind": "gone"}
                operation = check_operation.Operation(self.root / obstruction, ["native"])
                audit_path = operation.evidence / "claims.json"
                audit_path.write_text(json.dumps({"exit_code": 1, "stdout": json.dumps(checked)}))
                before = audit_path.read_bytes()
                selected_args = argparse.Namespace(**vars(args))
                if obstruction == "wrong_url":
                    selected_args.unresolved_cells_decision = "https://github.com/owner/repo/issues/1#issuecomment-99"
                reader = SimpleNamespace(get_json=lambda *_, **__: {**decision, **(
                    {"user": {"login": "foreign"}} if obstruction == "decision" else
                    {"updated_at": "2026-01-02T00:00:00Z"} if obstruction == "edited" else {})})
                with (patch.object(session_closeout, "GitHubReader", return_value=reader),
                      patch.object(session_closeout.github_identity, "automation_login", return_value="agent[bot]"),
                      patch.object(operation, "run") as post):
                    if obstruction != "none":
                        with self.assertRaises(ValueError):
                            session_closeout.unresolved_cell_claims(current, selected_args, operation, checked)
                        post.assert_not_called()
                        continue
                    self.assertEqual(session_closeout.claim_evidence(current, operation, "handoff_comments"), [])
                    result = session_closeout.unresolved_cell_claims(current, selected_args, operation, checked)
                    self.assertEqual(result, report)
                    self.assertEqual(audit_path.read_bytes(), before)
                    for key in ("released_claims", "handoff_comments"):
                        self.assertEqual(session_closeout.claim_evidence(current, operation, key), report[key])
                    operation.state["steps"]["unresolved_cells_note"] = {"complete": False}
                    changed = {**checked, "unresolved_invocations": [{**checked["unresolved_invocations"][0], "call_id": "another-cell"}]}
                    with self.assertRaises(ValueError):
                        session_closeout.unresolved_cell_claims(current, selected_args, operation, changed)
                    note = (operation.evidence / "unresolved-cells.md").read_text()
                    for fact in ("opaque-cell", "computed", args.unresolved_cells_decision, "native"):
                        self.assertIn(fact, note)
                    post.assert_not_called()

    def test_recovery_accepts_a_newer_verified_handoff_in_the_same_launch(self) -> None:
        ledger = self.root / "ledger.json"
        source = {"session_id": "native", "harness": "claude", "supervisor_owned": True, "transcript": "native.jsonl",
                  "launch_started_at": "2026-01-01T00:00:00Z", "handoff_url": "https://github.com/test/a/issues/1#issuecomment-95"}
        args = argparse.Namespace(ledger=ledger, session_id="native", handoff_url="https://github.com/test/a/issues/1#issuecomment-99")

        class Reader:
            def __init__(self, replies: dict[str, dict]) -> None:
                self.replies = replies

            def get_json(self, path: str, **_: Any) -> dict:
                return self.replies[path.rsplit("/", 1)[-1]]

        for newer in (False, True):
            ledger.write_text(json.dumps([source]))
            entry = session_closeout.normalize_ledger_entry(ledger, source)
            entry["recovery"] = {"kind": "handoff"}
            previous = {"created_at": "2026-01-02T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z",
                        "body": "Session: native\nEarlier progress", "user": {"login": "agent[bot]"},
                        "issue_url": "https://api.github.com/repos/test/a/issues/1"}
            stamp = "2026-01-03T00:00:00Z" if newer else "2026-01-01T00:01:00Z"
            current = {**previous, "created_at": stamp, "updated_at": stamp, "body": "Session: native\nSafe to exit: yes"}

            with (self.subTest(newer=newer),
                  patch.object(session_closeout, "GitHubReader", return_value=Reader({"99": current, "95": previous})),
                  patch.object(session_closeout.github_identity, "automation_login", return_value="agent[bot]"),
                  patch.object(session_closeout.closure, "source_records", return_value={})):
                operation = check_operation.Operation(self.root / str(newer), [str(ledger), "native"])
                if newer:
                    session_closeout.verify_handoff(entry, args, operation, retired=False)
                    self.assertEqual(json.loads(ledger.read_text())[0]["handoff_url"], args.handoff_url)
                else:
                    with self.assertRaises(ValueError):
                        session_closeout.verify_handoff(entry, args, operation, retired=False)
                    self.assertEqual(json.loads(ledger.read_text()), [source])

    def test_recovery_retires_only_the_gone_claim_free_row_and_replays_custody(self) -> None:
        ledger = self.root / "ledger.json"
        transcript = self.root / "source.jsonl"
        transcript.write_text(json.dumps({"type": "user", "sessionId": "native", "timestamp": "2026-01-01T00:00:00Z",
                                          "message": {"content": "unfinished request"}}) + "\n")
        source = {"session_id": "native", "harness": "claude", "supervisor_owned": True,
                  "pid": 123, "tty": "ttys001", "transcript": str(transcript), "backend": "tmux",
                  "tmux_session_id": "terminal"}
        other = {**source, "session_id": "other", "pid": 124}
        ledger.write_text(json.dumps([source, other]))
        log = self.root / "closed.json"
        args = argparse.Namespace(ledger=ledger, session_id="native", supervisor_session="supervisor", owner="test",
                                  recover_ended=True, successor_session=None, handoff_url=None, exit_seconds=.1)
        claims = {"ok": True, "claims_clear": True, "coverage": {"complete": True}, "session_ids": ["native"],
                  "blocked": [], "unresolved_invocations": []}
        release = session_closeout.release

        def run(_operation: check_operation.Operation, gate: str, _command: list[str], **_: Any) -> dict | list:
            if gate == "claims":
                return claims
            if gate == "finish":
                release.record_recovery(log, {**source, "recovery": {"kind": "gone"}}, "supervisor")
                return {"ok": True, "needs_reconciliation": False}
            self.assertIn(gate, ("terminals", "disappearance"))
            return []

        with (patch.object(check_operation.Operation, "run", run),
              patch.object(session_closeout.github_identity, "automation_login", return_value="fixture[bot]"),
              patch.object(session_closeout.closure, "closed_log", return_value=log),
              patch.object(session_closeout.closure, "ownership", return_value=nullcontext()),
              patch.object(session_closeout.session_liveness, "process_absent", return_value=True),
              patch.object(session_closeout.session_liveness, "inventory", return_value=[]),
              patch_attr(release, "inventory", return_value=[]),
              patch.object(release, "recovery_claims", return_value=claims),
              patch.object(release, "transcript_roots", return_value=[str(self.root)])):
            operation = check_operation.Operation(self.root / "recovery", [str(ledger), "native"])
            session_closeout.closeout(args, operation)
            self.assertEqual(json.loads(ledger.read_text()), [other])
            closed = json.loads(log.read_text())[0]
            self.assertTrue(all(closed[key] == value for key, value in source.items()))
            self.assertNotIn("verdict_at", closed)
            session_closeout.closeout(args, operation)
            transcript.write_text(transcript.read_text() + json.dumps({"type": "user", "sessionId": "native",
                "timestamp": "2026-01-02T00:00:00Z", "message": {"content": "resumed"}}) + "\n")
            with self.assertRaises(ValueError):
                session_closeout.closeout(args, operation)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def test_uncertain_mutation_is_not_replayed_after_restart(self) -> None:
        operation = check_operation.Operation(self.root, ["session"])
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("native", 1)) as run:
            with self.assertRaises(check_operation.GateFailed):
                operation.run("quit", ["native"], once=True)
            operation = check_operation.Operation(self.root, ["session"])
            with self.assertRaises(check_operation.GateFailed):
                operation.run("quit", ["native"], once=True)
            self.assertEqual(run.call_count, 1)

    def test_confirmed_not_started_comment_can_retry_without_waiving_uncertainty(self) -> None:
        entry = {"repository": "owner/repo", "issue": 1}
        for outcome in ("not_started", "unknown"):
            operation = check_operation.Operation(self.root / outcome, ["session"])
            with (patch.object(subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 1, json.dumps({"write_outcome": outcome}), ""),
                subprocess.CompletedProcess([], 0, json.dumps({"ok": True}), "")]) as run):
                with self.assertRaises(check_operation.GateFailed):
                    session_closeout.post_unresolved_cells(entry, operation)
                operation = check_operation.Operation(self.root / outcome, ["session"])
                if outcome == "not_started":
                    session_closeout.post_unresolved_cells(entry, operation)
                    self.assertEqual(run.call_count, 2)
                else:
                    with self.assertRaises(check_operation.GateFailed):
                        session_closeout.post_unresolved_cells(entry, operation)
                    self.assertEqual(run.call_count, 1)

    def test_finish_waits_for_full_source_exit_without_replaying_quit(self) -> None:
        for obstruction in (
            "immediate",
            "delayed_pid",
            "flushing_pid",
            "delayed_tty",
            "persistent_pid",
            "persistent_tty",
            "reused_pid",
            "uncertain",
            "changed_ledger",
            "changed_native",
        ):
            with self.subTest(obstruction=obstruction):
                self._check_finish_exit_wait(obstruction)

    def _check_finish_exit_wait(self, obstruction: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript, ledger, closed_log = (root / name for name in ("source.jsonl", "ledger.json", "closed.json"))
            transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "native",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "message": {"content": "Safe to exit: yes", "stop_reason": "end_turn"},
                    }
                )
                + "\n"
            )
            completed_transcript = transcript.read_text()
            entry = {
                "session_id": "native",
                "harness": "claude",
                "supervisor_owned": True,
                "backend": "tmux",
                "tmux_session_id": "terminal",
                "transcript": str(transcript),
                "pid": 8,
                "tty": "ttys001",
            }
            ledger.write_text(json.dumps([entry]))
            args = argparse.Namespace(
                ledger=ledger, session_id="native", supervisor_session="supervisor", owner="owner", exit_seconds=1
            )
            operation = check_operation.Operation(root / "evidence", [str(ledger), "native"])
            operation.state["steps"]["quit"] = {"complete": True, "result": {"delivery": "session_ended"}}
            original_quit = dict(operation.state["steps"]["quit"])
            rows = (
                []
                if obstruction in {"immediate", "uncertain"}
                else [(8, "other-tty", "claude")]
                if obstruction == "reused_pid"
                else [(8, "ttys001", "claude")]
                if obstruction.endswith("pid")
                else [(9, "ttys001", "node fixture")]
            )
            saved, calls, elapsed = {}, [], 0.0

            def advance(seconds: float) -> None:
                nonlocal elapsed
                elapsed += seconds
                if obstruction.startswith("delayed"):
                    rows.clear()
                elif obstruction == "flushing_pid":
                    if transcript.read_text() == completed_transcript:
                        transcript.write_text(completed_transcript + '{"type": "user",')
                    else:
                        transcript.write_text(completed_transcript)
                        rows.clear()
                elif obstruction == "changed_ledger":
                    ledger.write_text(json.dumps([{**entry, "supervisor_owned": False}]))
                    rows.clear()
                elif obstruction == "changed_native":
                    transcript.write_text(
                        json.dumps(
                            {
                                "type": "user",
                                "sessionId": "native",
                                "timestamp": "2026-01-01T00:01:00Z",
                                "message": {"content": "new work"},
                            }
                        )
                        + "\n"
                    )
                    rows.clear()

            def inventory() -> list[tuple[int, str, str]]:
                if obstruction == "uncertain":
                    raise OSError("fixture process inventory unavailable")
                return list(rows)

            def run(gate: str, _command: list[str], **_: Any) -> Any:
                calls.append(gate)
                if gate == "claims":
                    return {"claims_clear": True, "coverage": {"complete": True}}
                if gate == "finish":
                    self.assertFalse(rows, "finish reached before independent source exit proof")
                    self.assertEqual(json.loads(ledger.read_text()), [entry])
                    self.assertTrue(session_closeout.ready(entry, "supervisor")["safe_verdict"])
                    closed = session_closeout.release.record_closure(closed_log, entry, "supervisor")
                    saved["native"] = closed
                    return {"ok": True, "needs_reconciliation": False}
                self.assertIn(gate, {"terminals", "disappearance"})
                return []

            clock = SimpleNamespace(monotonic=lambda: elapsed, sleep=advance, time=time.time)
            with (
                patch.object(operation, "run", side_effect=run),
                patch.object(session_closeout, "verify_handoff"),
                patch_attr(session_closeout, "time", clock),
                patch.object(session_closeout.github_identity, "automation_login", return_value="agent[bot]"),
                patch.object(session_closeout.closure, "ownership", return_value=nullcontext()),
                patch.object(session_closeout.closure, "source_records", side_effect=lambda _: dict(saved)),
                patch.object(session_closeout.release, "transcript_roots", return_value=[str(root)]),
                patch_attr(session_closeout.release, "inventory", side_effect=inventory),
                patch.object(session_liveness, "inventory", side_effect=inventory),
                patch.object(
                    session_liveness,
                    "process_absent",
                    side_effect=lambda pid: obstruction == "reused_pid" or all(row[0] != pid for row in rows),
                ),
                patch.object(session_liveness, "native_exit_cutoff", return_value=None),
                patch.object(session_liveness, "process_arguments", return_value=(["node", "fixture"], True)),
                patch.object(session_closeout.closure, "process_started_at", return_value=datetime(2025, 1, 1, tzinfo=UTC)),
            ):
                if obstruction in {"immediate", "delayed_pid", "delayed_tty", "flushing_pid"}:
                    session_closeout.closeout(args, operation)
                    self.assertIn("finish", calls)
                    self.assertEqual(json.loads(ledger.read_text()), [])
                    self.assertEqual(elapsed == 0, obstruction == "immediate")
                else:
                    with self.assertRaises(check_operation.GateFailed) as failure:
                        session_closeout.closeout(args, operation)
                    if obstruction not in {"changed_ledger", "changed_native"}:
                        self.assertEqual(failure.exception.gate, "process_exit")
                        self.assertEqual(elapsed, args.exit_seconds)
                    self.assertNotIn("finish", calls)
                    self.assertNotIn("close", calls)
                    self.assertFalse(closed_log.exists())
                    self.assertTrue(json.loads(ledger.read_text()))
                self.assertEqual(operation.state["steps"]["quit"], original_quit)
                self.assertLessEqual(elapsed, args.exit_seconds)

    def test_closeout_uses_native_shapes_and_retires_only_verified_closures(self) -> None:
        for outcome in ("complete", "codex_complete", "unresolved_complete", "unresolved_note_failed", "unresolved_handoff_failed", "google_complete", "harness_changed", "finish_refused", "finish_timeout", "uncertain_exit", "close_interrupted", "close_uncertain", "resumed", "draft_before_clear", "draft_after_idle", "turn_after_idle"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entry: dict[str, Any] = {"session_id": "native", "harness": "claude", "supervisor_owned": True,
                         "backend": "tmux", "tmux_session_id": "terminal", "transcript": "relative.jsonl",
                         "pid": 8, "tty": "ttys001", "handoff_url": "https://github.com/owner/repo/issues/1#issuecomment-2"}
                decision = outcome.startswith("unresolved_") or outcome in {"draft_before_clear", "draft_after_idle", "turn_after_idle", "finish_timeout"}
                if decision or outcome == "codex_complete":
                    entry["harness"] = "codex"
                    entry.update(repository="owner/repo", issue=1)
                elif outcome == "google_complete":
                    entry["harness"] = "agy"
                transcript = root / entry["transcript"]
                safe = "Safe to exit: yes"
                records = ([{"type": "session_meta", "payload": {"id": "native"}},
                            {"type": "event_msg", "payload": {"type": "agent_message", "message": safe}},
                            {"type": "event_msg", "payload": {"type": "task_complete"}}] if entry["harness"] == "codex" else
                           [{"type": "assistant", "sessionId": "native", "message": {"content": [{"type": "text", "text": safe}], "stop_reason": "end_turn"}}])
                if decision:
                    records.insert(1, {"type": "response_item", "payload": {"type": "custom_tool_call",
                        "name": "exec", "call_id": "opaque-cell", "input": "eval(source)"}})
                transcript.write_text("\n".join(json.dumps({**row, "timestamp": "2026-01-01T00:00:00Z"}) for row in records) + "\n")
                ledger = root / "ledger.json"
                ledger.write_text(json.dumps([entry]))
                normalized = {**entry, "transcript": str(root / entry["transcript"])}
                closed = {**normalized, "process_exit_verified": True, "ended_at": "2026-01-01T00:00:00Z"}
                args = argparse.Namespace(ledger=ledger, session_id="native", supervisor_session="supervisor",
                                          owner="owner", exit_seconds=1, handoff_url=entry["handoff_url"],
                                          unresolved_cells_decision=session_closeout.UNRESOLVED_CELLS_DECISION if decision else None)
                live, present, calls, draft = True, True, [], False

                def command(argv: list[str], **_: Any) -> subprocess.CompletedProcess:
                    nonlocal live, present, draft
                    action = next((word for word in argv if word in {"list", "read", "clear", "send", "close"}),
                                  "claims" if any(str(word).endswith("check_claims.py") for word in argv) else
                                  "note" if any(str(word).endswith("gh-comment") for word in argv) else "finish")
                    calls.append(action)
                    payload: Any = {"sent": True}
                    code = 0
                    if action == "claims":
                        payload = audit if decision else {"claims_clear": True, "coverage": {"complete": True}}
                        code = int(decision)
                    elif action == "note":
                        self.assertNotIn("send", calls)
                        self.assertIn("opaque-cell", (operation.evidence / "unresolved-cells.md").read_text())
                        code = int(outcome == "unresolved_note_failed")
                    elif action == "finish":
                        payload = {"ok": outcome != "finish_refused", "needs_reconciliation": outcome == "finish_refused"}
                        if outcome == "finish_timeout" and calls.count("finish") == 1:
                            payload, code = {"ok": False, "error": "native thread/read timed out"}, 1
                    elif action == "list":
                        payload = [{"session_id": "terminal", "tty": "/dev/ttys001"}] if present else []
                    elif action == "read":
                        payload = {"dead": False, "screen": [], "input_line": {"style": "non_dim" if draft else "dim", "text": "draft" if draft else "suggestion"}}
                    elif action == "clear":
                        self.assertFalse(draft, "clear erased a draft entered during native reads")
                    elif action == "send":
                        self.assertFalse(draft, "exit command appended to changed input")
                        if entry["harness"] == "agy":
                            self.assertIsNotNone(iterm_tab.exit_observer(argv[argv.index("--text") + 1], entry["tty"]))
                        live = False
                        if outcome == "uncertain_exit":
                            payload, code = {"status": "send_failed"}, 1
                    elif action == "close":
                        present = outcome == "close_uncertain"
                        code = 1 if outcome == "close_interrupted" else code
                        if outcome == "resumed":
                            closed["resumed_at"] = "2026-01-01T00:01:00Z"
                    return subprocess.CompletedProcess(argv, code, json.dumps(payload), "")

                async def idle(_: dict) -> None:
                    nonlocal draft
                    draft = outcome == ("draft_after_idle" if "clear" in calls else "draft_before_clear")
                    if outcome == "turn_after_idle" and "clear" in calls:
                        with transcript.open("a") as stream:
                            stream.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "later"}}) + "\n")

                close_evidence = root / (entry["harness"] + "-closeout")
                close_identity = [str(ledger.resolve()), entry["session_id"]]
                operation = check_operation.Operation(close_evidence, close_identity)
                audit = {"ok": False, "claims_clear": False, "session_ids": ["native"], "blocked": [],
                         "coverage": {"complete": False, "search_errors": []}, "unresolved_invocations": [{
                             "session": "native", "transcript": str(transcript), "call_id": "opaque-cell", "reason": "computed", "code_mode": True}]}
                reader = SimpleNamespace(get_json=lambda *_, **__: {"user": {"login": "agent[bot]"},
                    "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"})
                with (patch.object(subprocess, "run", side_effect=command),
                      patch.object(session_closeout, "GitHubReader", return_value=reader),
                      patch.object(session_closeout, "native_idle", side_effect=idle) as idle_read,
                      patch.object(session_closeout.release, "handoff", return_value={"body": "Session: native"},
                                   side_effect=ValueError("handoff unavailable") if outcome == "unresolved_handoff_failed" else None),
                      patch.object(session_closeout.github_identity, "automation_login", return_value="agent[bot]"),
                      patch.object(session_liveness, "process_absent", side_effect=lambda _: not live),
                      patch.object(session_liveness, "inventory", return_value=[(8, "ttys001", "agy" if outcome == "harness_changed" else entry["harness"]) ]),
                      patch_attr(session_closeout.release, "inventory", return_value=[]),
                      patch.object(session_closeout.closure, "ownership", return_value=nullcontext()),
                      patch.object(session_closeout.closure, "store_lock", side_effect=lambda *_, **__: nullcontext()),
                      patch.object(session_closeout.closure, "source_records", return_value={"native": closed}),
                      patch.object(session_closeout.release, "transcript_roots", return_value=[str(root)]),
                      patch.object(session_closeout.release, "validate_closure")):
                    if outcome in {"unresolved_note_failed", "unresolved_handoff_failed"}:
                        with self.assertRaises(check_operation.GateFailed):
                            session_closeout.closeout(args, operation)
                        self.assertFalse({"send", "finish", "close"}.intersection(calls))
                        self.assertEqual(json.loads(ledger.read_text()), [entry])
                        if outcome == "unresolved_handoff_failed":
                            self.assertNotIn("note", calls)
                    elif outcome in {"draft_before_clear", "draft_after_idle", "turn_after_idle", "harness_changed"}:
                        with self.assertRaises(check_operation.GateFailed) as failure:
                            session_closeout.closeout(args, operation)
                        self.assertEqual(failure.exception.gate, "process" if outcome == "harness_changed" else "verdict" if outcome == "turn_after_idle" else "input")
                        self.assertFalse({"send", "finish", "close"}.intersection(calls))
                        self.assertEqual(json.loads(ledger.read_text()), [entry])
                    elif outcome in {"uncertain_exit", "close_interrupted", "finish_timeout"}:
                        with self.assertRaises(check_operation.GateFailed):
                            session_closeout.closeout(args, operation)
                        failed_receipt = close_evidence / ("quit.json" if outcome in {"uncertain_exit", "finish_timeout"} else "close.json")
                        preserved_receipt = failed_receipt.read_bytes()
                        idle_calls = idle_read.call_count
                        session_closeout.closeout(args, check_operation.Operation(close_evidence, close_identity))
                        self.assertEqual(failed_receipt.read_bytes(), preserved_receipt)
                        if outcome == "finish_timeout":
                            self.assertEqual(idle_read.call_count, idle_calls)
                            self.assertEqual(calls.count("finish"), 2)
                    elif outcome in {"finish_refused", "close_uncertain", "resumed"}:
                        with self.assertRaises(check_operation.GateFailed):
                            session_closeout.closeout(args, operation)
                        self.assertEqual(json.loads(ledger.read_text()), [entry])
                    else:
                        session_closeout.closeout(args, operation)
                    if outcome in {"complete", "codex_complete", "unresolved_complete", "google_complete", "uncertain_exit", "close_interrupted", "finish_timeout"}:
                        self.assertEqual(json.loads(ledger.read_text()), [])
                        self.assertEqual(calls.count("send"), 1)
                        if decision:
                            self.assertEqual(calls.count("note"), 1)
                            self.assertLess(calls.index("note"), calls.index("send"))
                        calls.clear()
                        session_closeout.closeout(args, check_operation.Operation(close_evidence, close_identity))
                        if outcome == "complete":
                            parent = check_operation.Operation(root / "catalog", ["landing"])
                            original = parent.evidence / (entry["harness"] + "-closeout")
                            close_evidence.rename(original)
                            parent.state["steps"][entry["harness"] + "_launch"] = {"complete": True}
                            catalog_landing.record_smoke_source(parent, entry["harness"], normalized, ledger)
                            config = {"ledger": str(ledger), "runtime_worktree": str(root), "repo": "owner/repo",
                                      "supervisor_session": args.supervisor_session}

                            with patch.object(parent, "run") as checked:
                                catalog_landing.require_closed_smokes(config, parent, {})
                                checked.assert_not_called()
                                parent.state["steps"][entry["harness"] + "_source"]["generation"] = "changed"
                                with self.assertRaises(check_operation.GateFailed):
                                    catalog_landing.require_closed_smokes(config, parent, {})
                                parent.state["steps"]["uncertain"] = {"complete": False}
                                with self.assertRaises(check_operation.GateFailed):
                                    catalog_landing.require_closed_smokes(config, parent, {})
                    if outcome in {"finish_refused", "close_uncertain", "resumed"}:
                        self.assertLess(calls.index("clear"), calls.index("send"))
                        self.assertEqual(calls.count("send"), 1)
                    if outcome == "finish_refused":
                        self.assertNotIn("close", calls)

    def test_lead_handoff_closeout_moves_workers_and_status_actors_to_the_successor(self) -> None:
        for retained, moved, uncertain in (([5], ["5"], False), ([5, 6], ["5"], False), ([6], [], False), ([5], [], True)):
            with self.subTest(retained=retained, uncertain=uncertain), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                launched = "2026-01-01T00:00:00+00:00"
                lead: dict[str, Any] = {
                    "session_id": "native", "harness": "claude", "supervisor_owned": True, "backend": "tmux",
                    "tmux_session_id": "terminal", "transcript": "native.jsonl", "pid": 8, "tty": "ttys001",
                    "repository": "owner/repo", "issue": 1, "worker_cap": 4, "worker_launches": {},
                    "launch_started_at": launched, "handoff_url": "https://github.com/owner/repo/issues/1#issuecomment-2"}
                successor = {**lead, "session_id": "successor", "tmux_session_id": "other", "pid": 9, "tty": "ttys002",
                             "transcript": "successor.jsonl", "launch_started_at": "2026-01-02T00:00:00+00:00", "handoff_url": None}
                worker = {**lead, "session_id": "worker", "tmux_session_id": "third", "pid": 10, "tty": "ttys003",
                          "transcript": "worker.jsonl", "worker_cap": None, "lead_session_id": "native", "issue": 7}
                (root / "native.jsonl").write_text(json.dumps({
                    "type": "assistant", "sessionId": "native", "timestamp": launched,
                    "message": {"content": [{"type": "text", "text": "Safe to exit: yes"}], "stop_reason": "end_turn"}}) + "\n")
                ledger = root / "ledger.json"
                ledger.write_text(json.dumps([lead, successor, worker]))
                closed = {**lead, "transcript": str(root / "native.jsonl"), "process_exit_verified": True, "ended_at": launched}
                args = argparse.Namespace(ledger=ledger, session_id="native", supervisor_session="supervisor", owner="owner",
                                          exit_seconds=1, handoff_url=lead["handoff_url"], successor_session=None)
                bodies = {5: "## Current Status\n\nNext actor: agent:native\nNext action: review the slice\n",
                          6: "## Current Status\n\nSession: native\nNext actor: agent:supervisor\n"}
                live, updates = True, []

                def command(argv: list[str], **_: Any) -> subprocess.CompletedProcess:
                    nonlocal live
                    payload: Any = {"sent": True}
                    code = 0
                    if "update-section" in argv:
                        updates.append((argv[argv.index("update-section") + 1], argv[argv.index("--next-actor") + 1]))
                        bodies[5] = bodies[5].replace("agent:native", "agent:successor")
                    elif any(str(word).endswith("check_claims.py") for word in argv):
                        payload = {"claims_clear": True, "coverage": {"complete": True}}
                    elif any(str(word).endswith("release_session_claims.py") for word in argv):
                        naming = [number for number in retained if "native" in bodies[number]]
                        payload = {"ok": not naming, "needs_reconciliation": bool(naming), "reports": [{
                            "session_id": "native",
                            "retained_status_claims": [{"repository": "owner/repo", "issue": number} for number in naming]}]}
                        code = int(bool(naming))
                    elif "list" in argv:
                        payload = [{"session_id": "terminal", "tty": "/dev/ttys001"}] if live else []
                    elif "read" in argv:
                        payload = {"dead": False, "screen": [], "input_line": {"style": "dim", "text": ""}}
                    elif "send" in argv:
                        live = False
                    return subprocess.CompletedProcess(argv, code, json.dumps(payload), "")

                class Reader:
                    def __init__(self, **_: Any) -> None:
                        pass

                    @staticmethod
                    def get_json(path: str, **_: Any) -> dict:
                        return {"body": bodies[int(path.rsplit("/", 1)[-1])]}

                with (patch.object(subprocess, "run", side_effect=command),
                      patch.object(session_closeout, "GitHubReader", Reader),
                      patch.object(session_closeout, "native_idle", AsyncMock()),
                      patch.object(session_closeout.release, "handoff_association", return_value=lead["handoff_url"]),
                      patch.object(session_closeout.release, "handoff",
                                   return_value={"body": "Session: native\nSafe to exit: yes", "created_at": launched}),
                      patch.object(session_closeout.github_identity, "automation_login", return_value="agent[bot]"),
                      patch.object(session_liveness, "process_absent", side_effect=lambda pid: pid == 8 and not live),
                      patch.object(session_liveness, "inventory", return_value=[(8, "ttys001", "claude")]),
                      patch.object(session_liveness, "process_started_at", return_value=datetime.fromisoformat(launched)),
                      patch_attr(session_closeout.release, "inventory", return_value=[]),
                      patch.object(session_closeout.closure, "ownership", return_value=nullcontext()),
                      patch.object(session_closeout.closure, "store_lock", side_effect=lambda *_, **__: nullcontext()),
                      patch.object(session_closeout.closure, "source_records", return_value={"native": closed}),
                      patch.object(session_closeout.release, "transcript_roots", return_value=[str(root)]),
                      patch.object(session_closeout.release, "validate_finished")):
                    operation = check_operation.Operation(root / "closeout", [str(ledger.resolve()), "native"])
                    named_otherwise = 6 in retained or uncertain
                    if uncertain:
                        operation.state["steps"]["repoint-owner-repo-5"] = {"complete": False}
                    if named_otherwise:
                        with self.assertRaises(check_operation.GateFailed) as failure:
                            session_closeout.closeout(args, operation)
                        self.assertEqual(failure.exception.gate, "repoint" if uncertain else "finish")
                    else:
                        session_closeout.closeout(args, operation)
                self.assertEqual(updates, [(number, "agent:successor") for number in moved])
                rows = {row["session_id"]: row for row in json.loads(ledger.read_text())}
                self.assertEqual(rows["worker"]["lead_session_id"], "successor")
                self.assertEqual("native" in rows, named_otherwise)

    def test_lead_successor_is_the_one_later_lead_on_its_anchor(self) -> None:
        ledger = self.root / "ledger.json"
        lead = {"session_id": "native", "supervisor_owned": True, "repository": "owner/repo", "issue": 1,
                "worker_cap": 4, "launch_started_at": "2026-01-01T00:00:00+00:00"}
        later = {**lead, "session_id": "successor", "launch_started_at": "2026-01-02T00:00:00+00:00"}
        worker_on_anchor = {**later, "session_id": "worker", "worker_cap": None}
        args = argparse.Namespace(ledger=ledger, successor_session=None)
        with patch.object(session_closeout, "load_ledger", side_effect=lambda _: rows):
            rows = [lead, worker_on_anchor]
            self.assertIsNone(session_closeout.handoff_successor(lead, args))
            self.assertIsNone(session_closeout.handoff_successor({**worker_on_anchor, "worker_cap": None}, args))
            rows = [lead, later, worker_on_anchor]
            self.assertEqual(session_closeout.handoff_successor(lead, args), "successor")
            rows = [lead, later, {**later, "session_id": "third"}]
            with self.assertRaises(check_operation.GateFailed):
                session_closeout.handoff_successor(lead, args)
            self.assertEqual(session_closeout.handoff_successor(lead, argparse.Namespace(ledger=ledger, successor_session="third")),
                             "third")
            rows = [lead, {**later, "issue": 2}]
            with self.assertRaises(check_operation.GateFailed):
                session_closeout.handoff_successor(lead, argparse.Namespace(ledger=ledger, successor_session="successor"))
            with patch.object(session_closeout.closure, "source_records", return_value={"retired": {**later, "session_id": "retired"}}):
                rows = [lead]
                with self.assertRaises(check_operation.GateFailed):
                    session_closeout.handoff_successor(lead, argparse.Namespace(ledger=ledger, successor_session="retired"))
                self.assertIsNone(session_closeout.handoff_successor(
                    {**lead, "recovery": {"kind": "handoff", "successor_session": "retired"}}, args))
            rows = [lead, later, worker_on_anchor]
            for source, selected in ((worker_on_anchor, "successor"), (lead, "worker")):
                with self.assertRaises(check_operation.GateFailed):
                    session_closeout.handoff_successor(source, argparse.Namespace(ledger=ledger, successor_session=selected))
        for source_lead, present in (({**lead, "worker_launches": {"batch": 1}}, [later]), (lead, [])):
            ledger.write_text(json.dumps([source_lead, *present, {"session_id": "worker", "lead_session_id": "native"}]))
            with patch.object(session_closeout.closure, "store_lock", side_effect=lambda *_, **__: nullcontext()), \
                    self.assertRaises(check_operation.GateFailed):
                session_closeout.adopt_workers(ledger, "native", "successor")
            self.assertEqual(json.loads(ledger.read_text())[-1]["lead_session_id"], "native")

    def test_retired_smokes_reuse_completed_journals_without_process_or_network_probes(self) -> None:
        for harness, recovered in (("claude", False), ("codex", False), ("codex", True)):
            for failure in (None, "quit", "close", "incomplete_quit", "incomplete_close", "uncertain_other", "generation", "retirement", "handoff", "resumed",
                            "active", "changed_active", "missing", "new_activity", "new_copy", "roots_unavailable", "native_action", "unverified_exit", "source_resume", "retained_terminal", "retained_live", "retained_changed", "retained_uncertain", "retained_finish", "retained_report", "retained_reconciliation", "retained_missing"):
                if failure and failure.startswith("retained_") and (harness != "codex" or recovered):
                    continue
                with self.subTest(harness=harness, recovered=recovered, failure=failure), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    ledger = root / "ledger.json"
                    ledger.write_text("[]")
                    transcript = root / "source.jsonl"
                    safe = "Safe to exit: yes"
                    records = ([{"type": "session_meta", "timestamp": "2026-01-01T00:00:00Z", "payload": {"id": "native"}},
                                {"type": "event_msg", "timestamp": "2026-01-01T00:00:01Z",
                                 "payload": {"type": "agent_message", "message": safe}},
                                {"type": "event_msg", "timestamp": "2026-01-01T00:00:02Z", "payload": {"type": "task_complete"}}]
                               if harness == "codex" else
                               [{"type": "assistant", "sessionId": "native", "timestamp": "2026-01-01T00:00:01Z",
                                 "message": {"content": [{"type": "text", "text": safe}], "stop_reason": "end_turn"}}])
                    transcript.write_text("\n".join(json.dumps(row) for row in records) + "\n")
                    closed: dict[str, Any] = {"session_id": "native", "harness": harness, "supervisor_owned": True,
                              "backend": "tmux", "tmux_session_id": "old-terminal", "transcript": str(transcript),
                              "pid": 8, "tty": "ttys003", "process_exit_verified": True, "ended_at": "2026-01-01T00:01:00Z",
                              "handoff_url": "https://github.com/owner/catalog/issues/1#issuecomment-2"}
                    parent = check_operation.Operation(root / "attempt", ["landing"])
                    source = {**closed, "pid": 7, "tmux_session_id": "original-terminal"} if recovered else closed
                    parent.state["steps"][harness + "_launch"] = {"complete": True, "result": {
                        **source, "session_id": source["tmux_session_id"], "native_session_id": source["session_id"]}}
                    catalog_landing.record_smoke_source(parent, harness, source, ledger)
                    identity = [str(ledger.resolve()), closed["session_id"]]
                    if recovered:
                        original = check_operation.Operation(parent.evidence / "codex-closeout", identity)
                        original.state.update(generation=parent.state["steps"]["codex_source"]["generation"],
                                              handoff={"url": source["handoff_url"]}, steps={"quit": {"complete": True}})
                        check_changes.save_state(original.path, original.state)
                    operation = check_operation.Operation(parent.evidence / (harness + "-closeout" + ("-recovery" if recovered else "")), identity)
                    generation = check_changes.fingerprint({key: value for key, value in session_closeout.release.closure_identity(closed).items()
                                                            if key != "handoff_url"})
                    operation.state.update(generation=generation, retired_generation=generation,
                                           handoff={"url": closed["handoff_url"], "session_id": closed["session_id"]},
                                           steps={"quit": {"complete": True, "result": {"session_id": "old-terminal", "sent": True}},
                                                  "close": {"complete": True, "result": {"session_id": "old-terminal", "close_requested": True}}})
                    check_changes.save_state(operation.evidence / "finish.json", {"exit_code": 0, "stdout": json.dumps({"ok": True, "needs_reconciliation": False,
                                                                                             "reports": [{"session_id": closed["session_id"]}]}), "stderr": ""})
                    check_changes.save_state(operation.evidence / "disappearance.json", {"exit_code": 0, "stdout": "[]", "stderr": ""})
                    if failure in {"quit", "close", "incomplete_quit", "incomplete_close"}:
                        operation.state["steps"][failure.removeprefix("incomplete_")]["complete"] = False
                        if failure.startswith("incomplete_"):
                            operation.state.pop("retired_generation")
                    elif failure == "uncertain_other":
                        operation.state["steps"]["other_mutation"] = {"complete": False}
                    elif failure in {"generation", "retirement"}:
                        operation.state["generation" if failure == "generation" else "retired_generation"] = "other"
                    elif failure == "handoff":
                        closed["handoff_url"] = "https://github.com/owner/catalog/issues/1#issuecomment-3"
                    elif failure == "resumed":
                        closed["resumed_at"] = "2026-01-01T00:02:00Z"
                    elif failure == "unverified_exit":
                        closed["process_exit_verified"] = False
                    elif failure in {"active", "changed_active"}:
                        ledger.write_text(json.dumps([{**closed, **({"pid": 99} if failure == "changed_active" else {})}]))
                    elif failure == "new_activity":
                        with transcript.open("a") as stream:
                            stream.write(json.dumps({**records[-1], "timestamp": "2026-01-01T00:02:00Z"}) + "\n")
                    copies = root / "other-account"
                    if failure == "new_copy":
                        copies.mkdir()
                        (copies / "native.jsonl").write_text(json.dumps({**records[-1], "timestamp": "2026-01-01T00:02:00Z"}) + "\n")
                    retaining = bool(failure and failure.startswith("retained_"))
                    if retaining:
                        operation.state.pop("retired_generation")
                        operation.state["steps"].pop("close")
                        ledger.write_text(json.dumps([{**closed, **({"pid": 99} if failure == "retained_changed" else {})}]))
                        if failure == "retained_uncertain":
                            operation.state["steps"]["quit"]["complete"] = False
                    if failure == "retained_finish":
                        check_changes.save_state(operation.evidence / "finish.json", {"exit_code": 1, "stdout": "{}"})
                    if failure in {"retained_report", "retained_reconciliation"}:
                        check_changes.save_state(operation.evidence / "finish.json", {"exit_code": 0, "stdout": json.dumps({
                            "ok": True, "needs_reconciliation": failure == "retained_reconciliation",
                            "reports": [{"session_id": "other" if failure == "retained_report" else closed["session_id"]}]})})
                    if failure == "retained_missing":
                        (operation.evidence / "finish.json").unlink()
                    ledger_before = ledger.read_bytes()
                    check_changes.save_state(operation.path, operation.state)
                    preserved = {path: path.read_bytes() for path in parent.evidence.rglob("*") if path.is_file()}
                    args = argparse.Namespace(ledger=ledger, session_id="native", supervisor_session="controller", owner="owner")
                    config = {"ledger": str(ledger), "runtime_worktree": str(root), "repo": "owner/catalog", "supervisor_session": "controller"}
                    with (patch.object(subprocess, "run", side_effect=AssertionError("external commands forbidden")),
                          patch.object(session_closeout.session_liveness, "inventory", return_value=[
                              (8, "ttys003", harness), (9, "ttys004", harness + (" resume " if harness == "codex" else " --resume ")
                                                       + ("native" if failure == "source_resume" else "other"))]),
                          patch.object(session_closeout.session_liveness, "process_arguments",
                                       side_effect=lambda _pid, command: (session_liveness.harness_arguments(command), True)),
                          patch.object(session_closeout.session_liveness, "process_absent", side_effect=AssertionError("retired PID probe forbidden")),
                          patch.object(session_closeout.session_liveness, "process_started_at", side_effect=AssertionError("retired birth probe forbidden")),
                          patch.object(session_closeout.session_liveness, "generation_ended", return_value=True),
                          patch.object(session_closeout.release, "verify_process_exit",
                                       side_effect=ValueError("original process remains") if failure == "retained_live" else None),
                          patch.object(session_closeout.closure, "ownership", return_value=nullcontext()),
                          patch.object(session_closeout.closure, "store_lock", side_effect=lambda *_, **__: nullcontext()),
                          patch.object(session_closeout.closure, "source_records", return_value={} if failure == "missing" else {"native": closed}),
                          patch.object(session_closeout.release, "transcript_roots", return_value=[str(copies)],
                                       side_effect=ValueError("account scope unavailable") if failure == "roots_unavailable" else None),
                          patch.object(session_closeout.closure, "require_fresh_action_verdict",
                                       side_effect=ValueError("pending native action") if failure == "native_action" else None)):
                        if failure not in {None, "quit", "close", "retained_terminal"}:
                            with self.assertRaises(check_operation.GateFailed) as refusal:
                                catalog_landing.require_closed_smokes(config, parent, {})
                            self.assertEqual(refusal.exception.gate, "supersede")
                            if failure == "active":
                                session_closeout.closeout(args, operation)
                                self.assertEqual(json.loads(ledger.read_text()), [])
                                catalog_landing.require_closed_smokes(config, parent, {})
                            elif not retaining:
                                with self.assertRaises(ValueError):
                                    session_closeout.closeout(args, operation)
                        else:
                            catalog_landing.require_closed_smokes(config, parent, {})
                            if not retaining:
                                session_closeout.closeout(args, operation)
                    if retaining:
                        self.assertEqual(ledger.read_bytes(), ledger_before)
                    self.assertEqual({path: path.read_bytes() for path in parent.evidence.rglob("*") if path.is_file()}, preserved)

    def test_handoff_observation_requires_a_new_completed_turn(self) -> None:
        original = {"state": "turn_ended", "size": 5, "last_text": "landing", "safe_verdict": False}
        final = {**original, "size": 10, "safe_verdict": True, "last_text": "Posted the handoff. Safe to exit: yes"}
        with (
            patch_attr(catalog_landing, "session_status", side_effect=[original, final]),
            patch.object(catalog_landing.time, "sleep"),
        ):
            self.assertEqual(catalog_landing.observe({}, "landing", after_size=5, final=True), final)

    def test_definite_refusal_can_retry_but_uncertain_send_cannot(self) -> None:
        operation = check_operation.Operation(self.root, ["target"])
        refused = subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "mutation_started": False}), "")
        success = subprocess.CompletedProcess([], 0, json.dumps({"close_requested": True}), "")
        with patch.object(subprocess, "run", side_effect=[refused, success]):
            with self.assertRaises(check_operation.GateFailed):
                operation.run("close", ["native"], once=True)
            operation = check_operation.Operation(self.root, ["target"])
            self.assertTrue(operation.run("close", ["native"], once=True)["close_requested"])

    def test_unpresented_questions_and_positive_handoff_identity(self) -> None:
        report: dict[str, Any] = {"complete": True, "questions": [{"url": "q", "question": "question", "resurface": True}]}
        for _ in range(2):
            self.assertTrue(oq.question_changes(report, self.root / "questions.json", ["source"])["questions"])
        for body in ("Catalog landing verified", '<!-- github-plan:abandoned-release {"source": {"session": "native"}} -->',
                     '<!-- github-plan:termination {"session_id": "native"} -->', "Previous session: native; continuing"):
            self.assertFalse(session_closeout.explicit_session(body, "native"))
        self.assertTrue(session_closeout.explicit_session("Explicit handoff from native session native (worker).", "native"))
        self.assertTrue(session_closeout.explicit_session("Session: native", "native"))
        self.assertFalse(session_closeout.explicit_session("Session: another", "native"))
        self.assertTrue(session_closeout.explicit_session("Session: native\n\n> Session: another", "native"))
        self.assertFalse(session_closeout.explicit_session("> Session: native\n\nSession: another", "native"))

    def test_managed_proxy_and_dim_suggestions_use_shared_native_rules(self) -> None:
        proxy = codex_session_proxy.__file__
        assert isinstance(proxy, str)
        self.assertTrue(session_liveness.source_harness([proxy, "native"], "codex"))
        self.assertFalse(session_liveness.source_harness([proxy, "native"], "claude"))
        self.assertTrue(iterm_tab.empty_agent_input({"input_line": {"style": "dim", "text": "a different suggestion"}}))
        self.assertFalse(iterm_tab.empty_agent_input({"input_line": {"style": "mixed", "text": "draft"}}))

    def test_single_launch_result_reaches_acknowledgement_without_relaunch(self) -> None:
        ledger = self.root / "ledger.json"
        entry = {"session_id": "native", "harness": "codex", "transcript": "fixture", "tmux_session_id": "terminal"}
        ledger.write_text(json.dumps([entry]))
        config = {"repo": "owner/catalog", "issue": 1, "landing_sha": "a" * 40, "ledger": str(ledger),
                  "command_files": {"codex": str(self.root / "command.txt")}, "window_id": "container"}
        launches = 0

        def command(argv: list[str], **_: Any) -> subprocess.CompletedProcess:
            nonlocal launches
            launches += 1
            return subprocess.CompletedProcess(argv, 0, json.dumps({"session_id": "terminal", "native_session_id": "native"}), "")

        with (patch.object(subprocess, "run", side_effect=command),
              patch.object(catalog_landing, "observe", side_effect=check_operation.GateFailed("response", "fixture"))):
            for _ in range(2):
                with self.assertRaises(check_operation.GateFailed) as failure:
                    catalog_landing.verify_smoke(config, check_operation.Operation(self.root, ["landing"]), "codex", "openai", self.root)
                self.assertEqual(failure.exception.gate, "response")
        self.assertEqual(launches, 1)
        iterm_tab.validate_brief_rules((self.root / "codex-brief.txt").read_text())

    def test_retained_claude_registration_resumes_without_relaunch(self) -> None:
        ledger = self.root / "ledger.json"
        native = {"session_id": "native-claude", "home": str(self.root)}
        launch = {"session_id": "terminal", "pid": 8, "tty": "ttys001", "backend": "tmux", "file_native": native}
        entry = {"session_id": native["session_id"], "transcript": str(self.root / "native.jsonl"), "pid": 8, "tty": "ttys001",
                 "harness": "claude", "supervisor_owned": True, "tmux_session_id": "terminal", "backend": "tmux"}
        ledger.write_text(json.dumps([entry]))
        operation = check_operation.Operation(self.root, ["landing"])
        operation.state["steps"]["claude_launch"] = {"complete": False}
        check_operation.save_state(self.root / "claude_launch.json", {"stdout": json.dumps({"failed_launch": {"phase": "register_ledger", "launch": launch}})})
        config: dict[str, Any] = {"repo": "owner/catalog", "issue": 1, "landing_sha": "a" * 40, "ledger": str(ledger)}
        with (patch.object(iterm_tab, "register_file_launch", return_value=entry) as register,
              patch.object(subprocess, "run") as run,
              patch.object(catalog_landing, "observe", side_effect=check_operation.GateFailed("response", "fixture"))):
            with self.assertRaises(check_operation.GateFailed) as failure:
                catalog_landing.verify_smoke(config, operation, "claude", "anthropic", self.root)
            self.assertEqual(failure.exception.gate, "response")
            run.assert_not_called()
        register.assert_called_once_with(launch, native, ledger)
        self.assertTrue(operation.state["steps"]["claude_launch"]["complete"])

    def test_native_request_and_registered_launch_resolve_without_replay(self) -> None:
        ledger = self.root / "ledger.json"
        native: dict[str, Any] = {"session_id": "native", "transcript": str(self.root / "native.jsonl"), "pid": 8, "tty": "ttys001",
                  "codex_home": str(self.root), "launch_pid": 8, "launch_generation": "generation", "launch_started_at": "birth"}
        launch = {"session_id": "terminal", "native_receipt": str(self.root / "receipt"), **{k: v for k, v in native.items() if k != "session_id"}}
        entry = {**native, "harness": "codex", "supervisor_owned": True, "tmux_session_id": "terminal"}
        ledger.write_text(json.dumps([entry]))
        operation = check_operation.Operation(self.root, ["landing"])
        operation.state["steps"]["codex_launch"] = {"complete": False}
        check_operation.save_state(self.root / "codex_launch.json", {"stdout": json.dumps({"failed_launch": {"phase": "register_ledger", "launch": launch}})})
        config: dict[str, Any] = {"repo": "owner/catalog", "issue": 1, "landing_sha": "a" * 40, "ledger": str(ledger)}
        with (patch_attr(catalog_landing, "read_launch_receipt", return_value=native),
              patch.object(subprocess, "run") as run,
              patch.object(catalog_landing, "observe", side_effect=check_operation.GateFailed("response", "fixture"))):
            with self.assertRaises(check_operation.GateFailed) as failure:
                catalog_landing.verify_smoke(config, operation, "codex", "openai", self.root)
            self.assertEqual(failure.exception.gate, "response")
            run.assert_not_called()
        operation.state["steps"].update(codex_handoff_request={"complete": True}, codex_acknowledgement={"size": 1},
                                        codex_request_time={"value": 0})
        config["supervisor_session"] = "supervisor"
        comments = [{"id": number, "created_at": "2026-01-01T00:00:00Z", "user": {"login": "agent[bot]"},
                     "body": "Session: native " + config["landing_sha"], "html_url": "comment-" + str(number)} for number in (1, 2)]
        with (patch.object(catalog_landing, "observe"),
              patch_attr(catalog_landing, "GitHubReader", return_value=SimpleNamespace(paged_json=lambda *_, **__: comments)),
              patch.object(catalog_landing.github_identity, "automation_login", return_value="agent[bot]"),
              patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"ok":true}', "")) as close):
            self.assertEqual(catalog_landing.verify_smoke(config, operation, "codex", "openai", self.root), "comment-2")
            self.assertEqual(close.call_count, 1)
            operation.state["steps"].pop("codex_closeout")
            ledger.write_text("[]")
            self.assertEqual(catalog_landing.verify_smoke(config, operation, "codex", "openai", self.root), "comment-2")
            self.assertEqual(close.call_count, 2)
        for harness, row in (("codex", {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "request"}]}}),
                             ("claude", {"type": "user", "message": {"role": "user", "content": "request"}})):
            with self.subTest(harness=harness):
                Path(native["transcript"]).write_text(json.dumps({**row, "timestamp": "2026-01-01T00:00:00Z"}) + "\n")
                self.assertTrue(catalog_landing.request_seen(entry, "request", 0))
                self.assertFalse(catalog_landing.request_seen(entry, "different request", 0))

    def test_native_idle_refuses_work_in_another_authenticated_copy(self) -> None:
        with (patch.object(session_closeout.closure, "lease_state", return_value={}),
              patch.object(session_closeout.closure, "transcripts", return_value={"other": {"status": {"background_tasks": ["task"]}}})):
            with self.assertRaises(check_operation.GateFailed):
                asyncio.run(session_closeout.native_idle({"harness": "codex", "codex_home": str(self.root),
                                                          "session_id": "native", "transcript": "source"}))

    def test_send_preflight_is_distinct_from_partial_delivery(self) -> None:
        for text in ("/quit", "/exit"):
            for draft, footer in (("unfinished " + text, []), (text + " unfinished", []), (None, []), (text, ["tab to queue message"])):
                read = AsyncMock(return_value={"screen": ["› " + (draft or ""), *footer],
                                               "input_line": {"line_index": 0, "style": "non_dim", "text": draft}})
                press = AsyncMock()
                with patch_attr(iterm_tab.asyncio, "sleep", new=AsyncMock()), self.assertRaises(iterm_tab.SendFailure):
                    asyncio.run(iterm_tab.submit_message("exact", text, True, read, press))
                press.assert_not_awaited()
        class Terminal:
            dead = True
            writes: list = []

            def select(self, _: str) -> dict:
                return {"dead": self.dead, "tty": "ttys001", "pane_id": "pane"}

            def send_keys(self, *_: Any) -> None:
                self.writes.append("attempt")
                raise ValueError("native write outcome unknown")

        terminal = Terminal()
        with self.assertRaises(iterm_tab.InputRefused):
            asyncio.run(tmux_terminal.Terminal.send(terminal, "terminal", "/exit"))
        self.assertFalse(terminal.writes)
        terminal.dead = False
        with self.assertRaises(iterm_tab.SendFailure):
            asyncio.run(tmux_terminal.Terminal.send(terminal, "terminal", "/exit"))
        self.assertTrue(terminal.writes)

    def test_claude_registration_is_idempotent_and_preserves_other_terminal_identity(self) -> None:
        native = {"session_id": "native-claude", "home": str(self.root)}
        transcript = self.root / "projects" / "fixture" / (native["session_id"] + ".jsonl")
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({
            "sessionId": native["session_id"], "type": "assistant",
            "message": {"content": [{"type": "text", "text": "acknowledged"}], "stop_reason": "end_turn"},
        }) + "\n")
        launch = {"session_id": "owned-terminal", "pid": 8, "tty": "/dev/ttys001", "backend": "tmux"}
        ledger = self.root / "new-location" / "ledger.json"
        command, session = iterm_tab.fresh_claude_session("cd /repo && exec claude --model opus")
        self.assertEqual(iterm_tab.launch_words(command)[-2:], ["--session-id", session])
        with self.assertRaises(ValueError):
            iterm_tab.fresh_claude_session("claude --resume earlier")
        result = dict(launch)
        born = datetime(2026, 11, 11, tzinfo=UTC)
        with patch.object(session_liveness, "inventory", return_value=[(8, "ttys001", "claude")]), \
                patch.object(session_liveness, "process_started_at", return_value=born):
            asyncio.run(iterm_tab.register_fresh_launch(argparse.Namespace(ledger=ledger), result, {"claude_native": native}))
            second = iterm_tab.register_file_launch(launch, native, ledger)
        self.assertEqual(result["native_session_id"], second["session_id"])
        self.assertTrue(result["ledger_recorded"])
        rows = json.loads(ledger.read_text())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tmux_session_id"], launch["session_id"])
        self.assertEqual(rows[0]["launch_started_at"], born.isoformat())
        with self.assertRaises(ValueError):
            iterm_tab.register_file_launch({**launch, "session_id": "another-terminal"}, native, ledger)

    def test_claude_registration_excludes_only_verified_browser_helper(self) -> None:
        native = {"session_id": "native-claude", "home": str(self.root)}
        transcript = self.root / "projects" / "fixture" / "native-claude.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"sessionId": native["session_id"], "type": "assistant"}) + "\n")
        launch = {"session_id": "owned-terminal", "tty": "/dev/ttys001", "backend": "tmux"}
        agent = ["/tools/claude", "--session-id", native["session_id"]]
        helper = ["/tools/claude", "--claude-in-chrome-mcp"]
        diagnostic = [*helper, iterm_tab.agent_tools.CHROME_DEBUG_FLAG]
        for label, child, command, available, success in (
            ("helper", helper, " ".join(helper), True, True),
            ("diagnostic", diagnostic, " ".join(diagnostic), True, True),
            ("diagnostic-extra", [*diagnostic, "--session-id", "other"],
             " ".join([*diagnostic, "--session-id", "other"]), True, False),
            ("two-agents", agent, " ".join(agent), True, False),
            ("unavailable", helper, " ".join(helper), False, False),
            ("mismatch", helper, "/other/claude --claude-in-chrome-mcp", True, False),
            ("extra-arguments", [*helper, "--session-id", "other"], " ".join(helper), True, False),
        ):
            ledger = self.root / (label + ".json")
            def arguments(pid: int) -> list[str]:
                if pid == 8:
                    return agent
                if not available:
                    raise OSError("unavailable")
                return child
            with (self.subTest(label=label),
                  patch.object(session_liveness, "inventory", return_value=[(8, "ttys001", " ".join(agent)), (9, "ttys001", command)]),
                  patch.object(session_liveness, "process_started_at", return_value=datetime(2026, 11, 11, tzinfo=UTC)),
                  patch.object(session_liveness, "native_arguments", side_effect=arguments)):
                if success:
                    self.assertEqual(iterm_tab.register_file_launch(launch, native, ledger)["pid"], 8)
                else:
                    with self.assertRaises(ValueError):
                        iterm_tab.register_file_launch(launch, native, ledger)
                    self.assertFalse(ledger.exists())


if __name__ == "__main__":
    unittest.main()
