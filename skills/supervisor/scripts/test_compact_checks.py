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
import unittest
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import catalog_landing
import check_changes
import check_operation
import goal_board
import iterm_tab
import oq
import session_closeout
import session_liveness
import tmux_terminal
import codex_session_proxy


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

    def test_board_and_questions_emit_changes_without_reprinting_private_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            board: dict[str, Any] = {
                "repository": "owner/direction", "checked_at": "first", "errors": [],
                "coverage": {"complete": True},
                "tracks": [{"url": "track", "status": "active", "read": True, "next_action": "private action",
                            "children": [{"url": "child", "status": "waiting", "unread": []}]}],
            }
            state = Path(directory) / "board.json"
            first = goal_board.board_changes(board, state, [])
            self.assertEqual(len(first["records"]), 2)
            self.assertNotIn("private action", json.dumps(first))
            board["checked_at"] = "later"
            self.assertEqual(goal_board.board_changes(board, state, [])["records"], [])
            board["tracks"][0]["children"][0]["status"] = "active"
            self.assertEqual(goal_board.board_changes(board, state, [])["records"][0]["url"], "child")
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

    def test_closeout_uses_native_shapes_and_retires_only_verified_closures(self) -> None:
        for outcome in ("complete", "finish_refused", "uncertain_exit", "close_interrupted", "close_uncertain", "resumed", "draft_before_clear", "draft_after_idle", "turn_after_idle"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entry: dict[str, Any] = {"session_id": "native", "harness": "claude", "supervisor_owned": True,
                         "backend": "tmux", "tmux_session_id": "terminal", "transcript": "relative.jsonl",
                         "pid": 8, "tty": "ttys001", "handoff_url": "https://github.com/owner/repo/issues/1#issuecomment-2"}
                if outcome in {"draft_before_clear", "draft_after_idle", "turn_after_idle"}:
                    entry["harness"] = "codex"
                transcript = root / entry["transcript"]
                safe = "Safe to exit: yes"
                records = ([{"type": "session_meta", "payload": {"id": "native"}},
                            {"type": "event_msg", "payload": {"type": "agent_message", "message": safe}},
                            {"type": "event_msg", "payload": {"type": "task_complete"}}] if entry["harness"] == "codex" else
                           [{"type": "assistant", "sessionId": "native", "message": {"content": [{"type": "text", "text": safe}], "stop_reason": "end_turn"}}])
                transcript.write_text("\n".join(json.dumps({**row, "timestamp": "2026-01-01T00:00:00Z"}) for row in records) + "\n")
                ledger = root / "ledger.json"
                ledger.write_text(json.dumps([entry]))
                normalized = {**entry, "transcript": str(root / entry["transcript"])}
                closed = {**normalized, "process_exit_verified": True, "ended_at": "2026-01-01T00:00:00Z"}
                args = argparse.Namespace(ledger=ledger, session_id="native", supervisor_session="supervisor",
                                          owner="owner", exit_seconds=1, handoff_url=entry["handoff_url"])
                live, present, calls, draft = True, True, [], False

                def command(argv: list[str], **_: Any) -> subprocess.CompletedProcess:
                    nonlocal live, present, draft
                    action = next((word for word in argv if word in {"list", "read", "clear", "send", "close"}),
                                  "claims" if any(str(word).endswith("check_claims.py") for word in argv) else "finish")
                    calls.append(action)
                    payload: Any = {"sent": True}
                    code = 0
                    if action == "claims":
                        payload = {"claims_clear": True, "coverage": {"complete": True}}
                    elif action == "finish":
                        payload = {"ok": outcome != "finish_refused", "needs_reconciliation": outcome == "finish_refused"}
                    elif action == "list":
                        payload = [{"session_id": "terminal", "tty": "/dev/ttys001"}] if present else []
                    elif action == "read":
                        payload = {"dead": False, "screen": [], "input_line": {"style": "non_dim" if draft else "dim", "text": "draft" if draft else "suggestion"}}
                    elif action == "clear":
                        self.assertFalse(draft, "clear erased a draft entered during native reads")
                    elif action == "send":
                        self.assertFalse(draft, "exit command appended to changed input")
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
                with (patch.object(subprocess, "run", side_effect=command),
                      patch.object(session_closeout, "native_idle", side_effect=idle),
                      patch.object(session_closeout.release, "handoff", return_value={"body": "Session: native"}),
                      patch.object(session_closeout.github_identity, "automation_login", return_value="agent[bot]"),
                      patch.object(session_liveness, "process_absent", side_effect=lambda _: not live),
                      patch.object(session_liveness, "inventory", return_value=[(8, "ttys001", entry["harness"]) ]),
                      patch.object(session_closeout.closure, "ownership", return_value=nullcontext()),
                      patch.object(session_closeout.closure, "store_lock", side_effect=lambda *_, **__: nullcontext()),
                      patch.object(session_closeout.closure, "source_records", return_value={"native": closed}),
                      patch.object(session_closeout.release, "transcript_roots", return_value=[str(root)]),
                      patch.object(session_closeout.release, "validate_closure")):
                    if outcome in {"draft_before_clear", "draft_after_idle", "turn_after_idle"}:
                        with self.assertRaises(check_operation.GateFailed) as failure:
                            session_closeout.closeout(args, operation)
                        self.assertEqual(failure.exception.gate, "verdict" if outcome == "turn_after_idle" else "input")
                        self.assertFalse({"send", "finish", "close"}.intersection(calls))
                        self.assertEqual(json.loads(ledger.read_text()), [entry])
                    elif outcome in {"uncertain_exit", "close_interrupted"}:
                        with self.assertRaises(check_operation.GateFailed):
                            session_closeout.closeout(args, operation)
                        failed_receipt = close_evidence / ("quit.json" if outcome == "uncertain_exit" else "close.json")
                        preserved_receipt = failed_receipt.read_bytes()
                        session_closeout.closeout(args, check_operation.Operation(close_evidence, close_identity))
                        self.assertEqual(failed_receipt.read_bytes(), preserved_receipt)
                    elif outcome in {"finish_refused", "close_uncertain", "resumed"}:
                        with self.assertRaises(check_operation.GateFailed):
                            session_closeout.closeout(args, operation)
                        self.assertEqual(json.loads(ledger.read_text()), [entry])
                    else:
                        session_closeout.closeout(args, operation)
                    if outcome in {"complete", "uncertain_exit", "close_interrupted"}:
                        self.assertEqual(json.loads(ledger.read_text()), [])
                        self.assertEqual(calls.count("send"), 1)
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
                                catalog_landing.require_closed_smokes(config, parent)
                                checked.assert_not_called()
                                parent.state["steps"][entry["harness"] + "_source"]["generation"] = "changed"
                                with self.assertRaises(check_operation.GateFailed):
                                    catalog_landing.require_closed_smokes(config, parent)
                                parent.state["steps"]["uncertain"] = {"complete": False}
                                with self.assertRaises(check_operation.GateFailed):
                                    catalog_landing.require_closed_smokes(config, parent)
                    if outcome in {"finish_refused", "close_uncertain", "resumed"}:
                        self.assertLess(calls.index("clear"), calls.index("send"))
                        self.assertEqual(calls.count("send"), 1)
                    if outcome == "finish_refused":
                        self.assertNotIn("close", calls)

    def test_retired_smokes_reuse_completed_journals_without_process_or_network_probes(self) -> None:
        for harness, recovered in (("claude", False), ("codex", False), ("codex", True)):
            for failure in (None, "quit", "close", "incomplete_quit", "incomplete_close", "uncertain_other", "generation", "retirement", "handoff", "resumed",
                            "active", "changed_active", "missing", "new_activity", "new_copy", "roots_unavailable", "native_action", "unverified_exit", "source_resume"):
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
                    check_changes.save_state(operation.evidence / "finish.json", {"exit_code": 0, "stdout": json.dumps({"ok": True, "needs_reconciliation": False}), "stderr": ""})
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
                          patch.object(session_closeout.closure, "ownership", return_value=nullcontext()),
                          patch.object(session_closeout.closure, "store_lock", side_effect=lambda *_, **__: nullcontext()),
                          patch.object(session_closeout.closure, "source_records", return_value={} if failure == "missing" else {"native": closed}),
                          patch.object(session_closeout.release, "transcript_roots", return_value=[str(copies)],
                                       side_effect=ValueError("account scope unavailable") if failure == "roots_unavailable" else None),
                          patch.object(session_closeout.closure, "require_fresh_action_verdict",
                                       side_effect=ValueError("pending native action") if failure == "native_action" else None)):
                        if failure not in {None, "quit", "close"}:
                            with self.assertRaises(check_operation.GateFailed) as refusal:
                                catalog_landing.require_closed_smokes(config, parent)
                            self.assertEqual(refusal.exception.gate, "supersede")
                            if failure == "active":
                                session_closeout.closeout(args, operation)
                                self.assertEqual(json.loads(ledger.read_text()), [])
                                catalog_landing.require_closed_smokes(config, parent)
                            else:
                                with self.assertRaises(ValueError):
                                    session_closeout.closeout(args, operation)
                        else:
                            catalog_landing.require_closed_smokes(config, parent)
                            session_closeout.closeout(args, operation)
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
        self.assertFalse(session_closeout.explicit_session("Catalog landing verified", "native"))
        self.assertTrue(session_closeout.explicit_session("Session: native", "native"))
        self.assertFalse(session_closeout.explicit_session("Session: another", "native"))

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
        launch = {"session_id": "terminal", "pid": 8, "tty": "ttys001", "backend": "tmux", "claude_native": native}
        entry = {"session_id": native["session_id"], "transcript": str(self.root / "native.jsonl"), "pid": 8, "tty": "ttys001",
                 "harness": "claude", "supervisor_owned": True, "tmux_session_id": "terminal", "backend": "tmux"}
        ledger.write_text(json.dumps([entry]))
        operation = check_operation.Operation(self.root, ["landing"])
        operation.state["steps"]["claude_launch"] = {"complete": False}
        check_operation.save_state(self.root / "claude_launch.json", {"stdout": json.dumps({"failed_launch": {"phase": "register_ledger", "launch": launch}})})
        config: dict[str, Any] = {"repo": "owner/catalog", "issue": 1, "landing_sha": "a" * 40, "ledger": str(ledger)}
        with (patch.object(iterm_tab, "register_claude_launch", return_value=entry) as register,
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
        ledger = self.root / "ledger.json"
        command, session = iterm_tab.fresh_claude_session("cd /repo && exec claude --model opus")
        self.assertEqual(iterm_tab.launch_words(command)[-2:], ["--session-id", session])
        with self.assertRaises(ValueError):
            iterm_tab.fresh_claude_session("claude --resume earlier")
        result = dict(launch)
        with patch.object(session_liveness, "inventory", return_value=[(8, "ttys001", "claude")]):
            asyncio.run(iterm_tab.register_fresh_launch(argparse.Namespace(ledger=ledger), result, {"claude_native": native}))
            second = iterm_tab.register_claude_launch(launch, native, ledger)
        self.assertEqual(result["native_session_id"], second["session_id"])
        self.assertTrue(result["ledger_recorded"])
        rows = json.loads(ledger.read_text())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tmux_session_id"], launch["session_id"])
        with self.assertRaises(ValueError):
            iterm_tab.register_claude_launch({**launch, "session_id": "another-terminal"}, native, ledger)


if __name__ == "__main__":
    unittest.main()
