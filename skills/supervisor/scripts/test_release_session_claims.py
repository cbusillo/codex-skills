#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Disposable transcripts and GitHub adapters exercise session claim release."""

import json
import contextlib
import io
import itertools
import fcntl
import importlib
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import release_session_claims as helper

ACTOR = "test-automation[bot]"
SOURCE = {"worker": "trial", "session": "source-session", "branch": "work/trial",
          "claimed_at": "2026-01-01T00:00:00Z"}


def comment(number, body, author=ACTOR):
    return {"id": number, "body": body, "user": {"login": author},
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}


class ReleaseTests(unittest.TestCase):
    def test_optional_admission_proof_is_not_a_dependency_of_release_retry(self):
        log = self.root / "closed.json"
        saved = {**self.entry, "closure_evidence": {"host": "old-boot"}}
        log.write_text(json.dumps([saved]))
        for unavailable in (ValueError("account set changed"), TimeoutError("scan budget"), OSError("config unreadable")):
            with self.subTest(unavailable=type(unavailable).__name__), patch.object(helper.closure, "evidence", side_effect=unavailable):
                result = helper.record_closure(log, self.entry, "supervisor")
                self.assertEqual(result["ended_at"], saved["ended_at"])

    def setUp(self):
        self.enterContext(patch.object(helper.session_liveness, "process_arguments",
            side_effect=lambda _pid, command: (helper.session_liveness.harness_arguments(command), True)))
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        remote = patch.object(helper.claim, "run_read", side_effect=lambda _args, *, cwd: f"https://github.com/test/{cwd.name}.git")
        remote.start()
        self.addCleanup(remote.stop)
        local = patch.object(helper.claim, "local_inventory", return_value={"sessions": [], "worktrees": [], "session_coverage": {}})
        local.start()
        self.addCleanup(local.stop)
        self.transcript = self.root / "source-session.jsonl"
        self.transcript.write_text("\n".join(json.dumps(r) for r in [
            {"type": "session_meta", "timestamp": "2026-01-01T00:00:00Z",
             "payload": {"id": SOURCE["session"], "cwd": "/repo"}},
            {"timestamp": "2026-01-01T00:01:00Z", "payload": {
                "type": "agent_message", "message": "Finished.\nSafe to exit: yes"}},
            {"timestamp": "2026-01-01T00:01:01Z", "payload": {"type": "task_complete"}},
        ]) + "\n")
        self.entry = {"session_id": SOURCE["session"], "harness": "codex",
                      "transcript": str(self.transcript), "pid": 123, "tty": "ttys123",
                      "supervisor_owned": True, "process_exit_verified": True,
                      "ended_at": "2026-01-01T00:02:00Z",
                      "handoff_url": "https://github.com/test/a/issues/1#issuecomment-99"}
        started = patch.object(helper.closure, "process_started_at", return_value=
                               helper.release.stamp(self.entry["ended_at"]) - timedelta(seconds=1))
        started.start()
        self.addCleanup(started.stop)
        self.source = comment(1, "Claimed by trial\n\n" + helper.claim.marker(SOURCE))
        self.reader = Mock()
        self.reader.get_json.return_value = {**comment(99, "Handoff"),
            "issue_url": "https://api.github.com/repos/test/a/issues/1"}
        self.inventory = patch.object(helper, "inventory", return_value=[])
        self.inventory.start()
        self.addCleanup(self.inventory.stop)

    def test_bulk_release_reports_active_pr_claim_with_supported_manual_route(self):
        handoff = dict(self.reader.get_json.return_value)
        self.reader.get_json.side_effect = lambda path, **_kw: handoff if "/issues/comments/" in path else {"number": 1, "pull_request": {}, "body": "Bump dependencies"}
        self.reader.paged_json.return_value = [self.source]
        report = helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1)], self.root, False)
        self.assertEqual(report["pr_claim_handoffs"][0]["claim_comments"], [self.source["id"]])
        route = report["pr_claim_handoffs"][0]["release_argv"]
        self.assertIn("release-claim", route)
        self.assertIn("https://github.com/test/a/pull/1", route)
        self.assertIn("--evidence-comment", route)
        self.assertEqual(report["claims"], [])

    def test_bulk_release_does_not_report_a_pr_claim_already_authored_released(self):
        handoff = dict(self.reader.get_json.return_value)
        self.reader.get_json.side_effect = lambda path, **_kw: handoff if "/issues/comments/" in path else {"number": 1, "pull_request": {}, "body": "Bump dependencies"}
        self.reader.paged_json.return_value = [self.source, comment(2, "Released claim 1")]
        report = helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1)], self.root, False)
        self.assertEqual(report["pr_claim_handoffs"], [])

    def test_main_requires_reconciliation_when_pr_handoffs_remain(self):
        log = self.root / "closed.json"
        log.write_text(json.dumps([self.entry]))
        for apply in (False, True):
            argv = ["helper", "backfill", "--closed-log", str(log), "--supervisor-session", "supervisor",
                "--owner", "test", "--checkout-root", str(self.root), "--confirm-session-ended",
                "--verified-handoff", "--transcript-root", str(self.root)] + (["--apply"] if apply else [])
            output = io.StringIO()
            with patch.object(helper.sys, "argv", argv), \
                    patch.object(helper.github_identity, "automation_login", return_value=ACTOR), \
                    patch.object(helper, "GitHubReader", return_value=self.reader), \
                    patch.object(helper, "discover", return_value=([("test/a", 1)], ["test/a"])), \
                    patch.object(helper, "release_session", return_value={"pr_claim_handoffs": [{"claim_comments": [1]}]}), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(helper.main(), 1)
            result = json.loads(output.getvalue())
            self.assertFalse(result["ok"])
            self.assertTrue(result["needs_reconciliation"])

    def test_pr_release_handoff_refuses_a_checkout_for_another_repository(self):
        handoff = dict(self.reader.get_json.return_value)
        self.reader.get_json.side_effect = lambda path, **_kw: handoff if "/issues/comments/" in path else {"pull_request": {}, "body": "Bump dependencies"}
        self.reader.paged_json.return_value = [self.source]
        with patch.object(helper.claim, "run_read", return_value="https://github.com/other/a.git"):
            report = helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1)], self.root, False)
        self.assertFalse(report["pr_claim_handoffs"])
        self.assertTrue(report["issue_refusals"])

    def test_finished_process_and_safe_verdict(self):
        self.assertTrue(helper.validate_closure(self.entry, "supervisor")["safe_verdict"])
        for rows in ([(123, "other", "zsh")], [(456, "ttys123", "codex")], [(456, "ttys123", "zsh -c work")]):
            with self.subTest(rows=rows), patch.object(helper, "inventory", return_value=rows):
                with self.assertRaisesRegex(ValueError, "remains"):
                    helper.validate_closure(self.entry, "supervisor")

    def test_claude_compaction_after_the_verdict_still_allows_release(self):
        transcript = self.root / "claude-source.jsonl"
        verdict = {"type": "assistant", "timestamp": "2026-01-01T00:01:00Z", "message": {
            "content": [{"type": "text", "text": "Finished.\nSafe to exit: yes"}], "stop_reason": "end_turn"}}
        compaction = [
            {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-01-01T00:01:30Z"},
            {"type": "user", "isCompactSummary": True, "timestamp": "2026-01-01T00:01:31Z",
             "message": {"content": "This session is being continued from a previous conversation."}},
        ]
        entry = {**self.entry, "harness": "claude", "transcript": str(transcript)}
        transcript.write_text("".join(json.dumps({**r, "sessionId": SOURCE["session"]}) + "\n" for r in [verdict] + compaction))
        self.assertTrue(helper.validate_closure(entry, "supervisor")["safe_verdict"])
        resumed = {"type": "assistant", "timestamp": "2026-01-01T00:01:40Z", "message": {
            "content": [{"type": "text", "text": "Picking the work back up."}], "stop_reason": "end_turn"}}
        transcript.write_text("".join(json.dumps({**r, "sessionId": SOURCE["session"]}) + "\n" for r in [verdict] + compaction + [resumed]))
        with self.assertRaisesRegex(ValueError, "lacks an unqualified"):
            helper.validate_closure(entry, "supervisor")

    def inventory_row(self, **changes):
        return {"repo": "test/a", "issue": 1, "claim_comment_id": 1,
                "session_id": SOURCE["session"], "ended_at": self.entry["ended_at"],
                "safe_exit_yes": True, "still_running": False, "handoff_url": None,
                "retained_pr_url": None, **changes}

    def test_verified_inventory_groups_claims_without_inventing_pid_or_tty(self):
        rows = [self.inventory_row(), self.inventory_row(issue=2, claim_comment_id=2), self.inventory_row()]
        entries, reports = helper.backfill_inventory.load(rows, {"codex": [str(self.root)], "claude": [str(self.root)]})
        self.assertFalse(reports)
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0]["_inventory_claims"]), 2)
        self.assertNotIn("pid", entries[0])
        self.assertNotIn("tty", entries[0])
        self.assertTrue(helper.validate_closure(entries[0], "supervisor")["safe_verdict"])
        with patch.object(helper, "inventory", return_value=[(789, "different", "node /bin/claude --resume source-session")]):
            with self.assertRaisesRegex(ValueError, "resumed harness"):
                helper.validate_closure(entries[0], "supervisor")

    def test_bad_inventory_row_preserves_all_claims_of_that_session(self):
        for row in (self.inventory_row(still_running=True), self.inventory_row(safe_exit_yes=False),
                    self.inventory_row(ended_at="2026-01-01T00:04:00Z")):
            entries, reports = helper.backfill_inventory.load([self.inventory_row(), row], {"codex": [str(self.root)]})
            self.assertFalse(entries)
            self.assertTrue(reports)

    def test_finish_does_not_trust_inventory_flags_from_a_ledger(self):
        ledger, log = self.root / "ledger.json", self.root / "closed.json"
        ledger.write_text(json.dumps([{**self.entry, "pid": None, "_inventory": True,
                                      "safe_exit_yes": True, "still_running": False}]))
        argv = ["helper", "finish", "--ledger", str(ledger), "--closed-log", str(log),
                "--session-id", SOURCE["session"], "--supervisor-session", "supervisor",
                "--owner", "test", "--checkout-root", str(self.root), "--confirm-session-ended",
                "--verified-handoff", "--transcript-root", str(self.root)]
        with patch.object(helper.sys, "argv", argv), \
                patch.object(helper.github_identity, "automation_login", return_value=ACTOR), \
                patch.object(helper, "comment") as write, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(helper.main(), 1)
        write.assert_not_called()
        with self.assertRaisesRegex(ValueError, "single timezone"):
            helper.validate_closure({**self.entry, "ended_at": "2026-01-01 00:02:00+00:00"}, "supervisor")

    def finish_thread(self, comments, *, verdict="Safe to exit: yes", body="", scoped=False, evidence=None, apply=True):
        ledger, log = self.root / "ledger.json", self.root / "closed.json"
        ledger.write_text(json.dumps([{**self.entry, "handoff_url": None}]))
        log.write_text(json.dumps([{**self.entry, "handoff_url": None}]))
        log.chmod(0o600)
        records = helper.session_record.read_jsonl(self.transcript)
        records[1]["payload"]["message"] = "Finished.\n" + verdict
        self.transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        self.reader.paged_json.side_effect = lambda path, **_kw: comments if path.endswith("/issues/1/comments") else []
        self.reader.get_json.side_effect = lambda path, **_kw: evidence if evidence and path.endswith("/issues/comments/99") else {"body": body, "number": 1}
        argv = ["helper", "finish", "--ledger", str(ledger), "--closed-log", str(log),
                "--session-id", SOURCE["session"], "--supervisor-session", "supervisor",
                "--owner", "test", "--checkout-root", str(self.root), "--confirm-session-ended",
                "--verified-handoff", "--handoff-thread", "test/a#2", "--transcript-root", str(self.root)]
        if apply:
            argv.append("--apply")
        if scoped:
            argv += ["--repo", "test/a"]
        output = io.StringIO()
        with patch.object(helper.sys, "argv", argv), \
                patch.object(helper.github_identity, "automation_login", return_value=ACTOR), \
                patch.object(helper, "GitHubReader", return_value=self.reader), \
                patch.object(helper, "discover", return_value=([("test/a", 1)], ["test/a"])), \
                patch.object(helper, "comment") as write, patch.object(helper, "run_release") as run, \
                contextlib.redirect_stdout(output):
            result = helper.main()
        write.assert_not_called()
        run.assert_not_called()
        return result, json.loads(output.getvalue())

    def own_release(self, **changes):
        return {**comment(2, "Released claim 1\n\nEngineering handed to Supervisor."),
                "created_at": "2026-01-01T00:01:00Z", "updated_at": "2026-01-01T00:01:00Z", **changes}

    def test_finish_recognizes_self_released_claim_from_its_original_comment(self):
        result, report = self.finish_thread([self.source, self.own_release()], body=self.source["body"])
        self.assertEqual(result, 0)
        self.assertFalse(report["needs_reconciliation"])
        session = report["reports"][0]
        self.assertEqual(session["claims"], [])
        self.assertEqual(session["released_claims"][0]["claim_comment"], self.source["id"])
        self.assertEqual(session["handoff_url"], "https://github.com/test/a/issues/1#issuecomment-2")

    def test_finish_read_only_session_has_nothing_to_release(self):
        result, report = self.finish_thread([])
        self.assertEqual(result, 0)
        self.assertEqual(report["reports"][0]["released_claims"], [])
        self.assertFalse(report["needs_reconciliation"])

    def test_finish_retry_uses_original_closure_to_distinguish_tty_reuse(self):
        ended = helper.release.stamp(self.entry["ended_at"])
        for apply in (False, True):
            wrapper = "python codex-session.py launch -- codex --remote unix:///new-client"
            for command, guarded in (("python gh_pr_watch.py", False), ("codex exec fresh-job", False),
                                     ("codex resume 11111111-1111-4111-8111-111111111111", False), ("claude -c", False),
                                     (wrapper, False), (wrapper, True)):
                for born, expected in ((ended - timedelta(seconds=1), 1), (ended, 1),
                                       (ended + timedelta(seconds=22), 0),
                                       (datetime.now(timezone.utc) + timedelta(days=1), 1)):
                    if command == wrapper and not guarded:
                        expected = 1
                    with self.subTest(apply=apply, command=command, born=born, guarded=guarded), \
                            patch.object(helper, "inventory", return_value=[(456, self.entry["tty"], command)]), \
                            patch.object(helper.closure, "process_started_at", return_value=born), \
                            patch.object(helper.closure, "closed_log", return_value=self.root / "closed.json"), \
                            patch.object(helper.closure, "guarded_launch_endpoint", return_value="/new-client" if guarded else None):
                        result, report = self.finish_thread([self.source, self.own_release()], apply=apply)
                        self.assertEqual(result, expected, report)
                        saved = json.loads((self.root / "closed.json").read_text())[0]
                        self.assertEqual(saved["ended_at"], self.entry["ended_at"])

    def test_finish_retry_never_excuses_recorded_pids_or_native_resume(self):
        self.entry["launch_pid"] = 124
        ended = helper.release.stamp(self.entry["ended_at"])
        for pid, tty, command in ((self.entry["pid"], "other-tty", "codex"),
                                  (self.entry["launch_pid"], "other-tty", "shell"),
                                  *((456, self.entry["tty"], command) for command in (
                                      "codex resume source-session", "codex resume --last", "codex resume",
                                      "codex"))):
            for born in (ended - timedelta(seconds=1), ended + timedelta(seconds=22)):
                with self.subTest(pid=pid, command=command, born=born), \
                        patch.object(helper, "inventory", return_value=[(pid, tty, command)]), \
                        patch.object(helper.closure, "process_started_at", return_value=born):
                    result, report = self.finish_thread([])
                    self.assertEqual(result, 1, report)

    def test_process_exit_only_suspects_reattachment_by_the_sources_harness(self):
        ended = helper.release.stamp(self.entry["ended_at"])
        commands = {"codex": ("codex", "codex resume --last"),
                    "claude": ("claude -c", "node /bin/claude.js")}
        for source in commands:
            for occupant, variants in commands.items():
                for command in variants:
                    for born in (ended - timedelta(seconds=1), ended, ended + timedelta(seconds=22)):
                        with self.subTest(source=source, command=command, born=born), \
                                patch.object(helper, "inventory", return_value=[(456, self.entry["tty"], command)]), \
                                patch.object(helper.closure, "process_started_at", return_value=born):
                            if source == occupant or born <= ended:
                                with self.assertRaises(ValueError):
                                    helper.verify_process_exit({**self.entry, "harness": source}, ended_at=ended)
                            else:
                                helper.verify_process_exit({**self.entry, "harness": source}, ended_at=ended)

    def test_finish_retry_preserves_claims_when_tty_start_is_unavailable(self):
        with patch.object(helper, "inventory", return_value=[(456, self.entry["tty"], "codex")]), \
                patch.object(helper.closure, "process_started_at", side_effect=ValueError("start unavailable")):
            result, report = self.finish_thread([self.source, self.own_release()])
        self.assertEqual(result, 1, report)

    def test_new_closure_does_not_trust_ledger_end_to_excuse_existing_work(self):
        ended = helper.release.stamp(self.entry["ended_at"])
        with patch.object(helper, "inventory", side_effect=[[(456, self.entry["tty"], "python gh_pr_watch.py")], []]), \
                patch.object(helper.closure, "process_started_at", return_value=ended + timedelta(seconds=22)):
            with self.assertRaises(ValueError):
                helper.record_closure(self.root / "new-closure.json", self.entry, "supervisor")
        self.assertFalse((self.root / "new-closure.json").exists())

    def test_new_closure_rejects_worker_born_during_the_first_inventory(self):
        observed = datetime.now(timezone.utc)
        log = self.root / "new-closure.json"
        with patch.object(helper, "datetime") as clock, \
                patch.object(helper, "inventory", return_value=[(456, self.entry["tty"], "exit-hook")]), \
                patch.object(helper.closure, "process_started_at", return_value=observed + timedelta(seconds=1)):
            clock.now.side_effect = itertools.chain([observed], itertools.repeat(observed + timedelta(seconds=2)))
            with self.assertRaises(ValueError):
                helper.record_closure(log, self.entry, "supervisor")
        self.assertFalse(log.exists())

    def test_first_recovery_finish_uses_native_dead_pane_cutoff_then_retains_it(self):
        import tmux_terminal
        ended = helper.release.stamp(self.entry["ended_at"])
        entry = {**self.entry, "tmux_socket": "fixture", "tmux_session_id": "tmux:fixture:" + "a" * 32,
                 "window_id": "$1", "tab_id": "@1", "tty_exited_at": "2099-01-01T00:00:00Z",
                 "launch_pid": 123, "launch_generation": "new-generation",
                 "launch_started_at": (ended - timedelta(minutes=1)).isoformat(), "codex_home": str(self.root)}
        pane = {"session_id": entry["tmux_session_id"], "pane_id": "%1", "pid": 789,
                "tmux_socket": "fixture", "window_id": "$1", "tab_id": "@1", "tty": entry["tty"],
                "dead": True, "exited_at": ended.isoformat()}
        for recovered in (False, True):
            log = self.root / f"recovery-{recovered}.json"
            if recovered:
                metadata = {key: entry[key] for key in (
                    "launch_pid", "launch_generation", "launch_started_at", "pid", "tty", "codex_home")}
                log.write_text(json.dumps([{**self.entry, "pid": 122, "tty": "old-tty",
                                            "resumed_at": (ended - timedelta(minutes=1)).isoformat(),
                                            "resumed_generation": metadata}]))
            with self.subTest(recovery=recovered), \
                    patch.object(tmux_terminal.Terminal, "list", return_value=[pane]), \
                    patch.object(helper, "inventory", return_value=[(456, entry["tty"], "codex --remote unix:///new-client")]), \
                    patch.object(helper.closure, "process_started_at", return_value=ended + timedelta(seconds=22)), \
                    patch.object(helper.closure, "guarded_launch_endpoint", return_value="/new-client"):
                closed = helper.record_closure(log, entry, "supervisor")
                self.assertEqual(closed["tty_exited_at"], ended.isoformat())
                self.assertGreater(helper.release.stamp(closed["ended_at"]), ended + timedelta(seconds=22))
                with patch.object(tmux_terminal.Terminal, "list", side_effect=AssertionError("dead pane already removed")):
                    retried = helper.record_closure(log, entry, "supervisor")
                self.assertEqual(retried["ended_at"], closed["ended_at"])
                self.assertEqual(retried["tty_exited_at"], closed["tty_exited_at"])

    def test_missing_dead_pane_uses_strict_exit_check_without_accepting_caller_cutoff(self):
        import tmux_terminal
        entry = {**self.entry, "tmux_socket": "fixture", "tmux_session_id": "tmux:fixture:" + "a" * 32,
                 "tty_exited_at": "2099-01-01T00:00:00Z"}
        for error in (ValueError("pane missing"), OSError("inventory unavailable")):
            with self.subTest(error=type(error).__name__), patch.object(tmux_terminal.Terminal, "dead_pane", side_effect=error):
                log = self.root / f"fallback-{type(error).__name__}.json"
                closed = helper.record_closure(log, entry, "supervisor")
                self.assertNotIn("tty_exited_at", closed)
                with patch.object(helper, "inventory", return_value=[(456, entry["tty"], "codex exec -- new work")]), self.assertRaises(ValueError):
                    helper.record_closure(self.root / "must-not-publish.json", entry, "supervisor")
                self.assertFalse((self.root / "must-not-publish.json").exists())

    def test_scheduled_jobs_do_not_change_post_exit_finish_policy(self):
        entry = {**self.entry, "harness": "claude"}
        records = [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "create", "name": "CronCreate", "input": {}}]}},
            {"type": "user", "toolUseResult": {"id": "wake", "recurring": True, "durable": False},
             "message": {"content": [{"type": "tool_result", "tool_use_id": "create", "content": "fixture"}]}},
            {"type": "assistant", "timestamp": "2026-01-01T00:01:00Z", "message": {
                "stop_reason": "end_turn", "content": [{"type": "text", "text": "Safe to exit: yes"}]}},
        ]
        self.transcript.write_text("".join(json.dumps({**r, "sessionId": SOURCE["session"]}) + "\n" for r in records))
        helper.validate_closure(entry, "supervisor")

    def test_finish_self_release_with_session_field_and_progress_note(self):
        released = self.own_release(body="Released claim 1\n\nSession: source-session\nEngineering handoff")
        progress = comment(3, "Session: source-session\nProgress: affected tests passed")
        result, report = self.finish_thread([self.source, released, progress])
        self.assertEqual(result, 0)
        self.assertEqual([r["claim_comment"] for r in report["reports"][0]["released_claims"]], [self.source["id"]])
        self.assertFalse(report["needs_reconciliation"])

    def test_finish_scoped_no_claims_qualifies_its_completion(self):
        result, unscoped = self.finish_thread([])
        self.assertEqual(result, 0)
        result, scoped = self.finish_thread([], scoped=True)
        self.assertEqual(result, 0)
        self.assertNotEqual(scoped["reports"][0]["status"], unscoped["reports"][0]["status"])
        self.assertEqual(scoped["reports"][0]["coverage"], scoped["coverage"])
        self.assertNotEqual(scoped["coverage"], unscoped["coverage"])

    def test_finish_preserves_held_foreign_and_late_claims(self):
        other = comment(3, "Claimed by other\n\n" + helper.claim.marker({**SOURCE, "session": "other-session"}))
        cases = [
            [self.source],
            [self.source, other, self.own_release(body="Released claim 3")],
            [self.source, self.own_release(user={"login": "foreign[bot]"})],
            [self.source, self.own_release(body="Released claim 1\n\nSession: other-session")],
            [{**self.source, "user": {"login": "foreign[bot]"}}, self.own_release()],
            [self.source, self.own_release(created_at="2026-01-01T00:03:00Z", updated_at="2026-01-01T00:03:00Z")],
            [self.source, self.own_release(updated_at="2026-01-01T00:03:00Z")],
            [self.source, self.own_release(created_at=self.source["created_at"])],
            [self.source, self.own_release(), comment(4, "Claimed by trial\n\n" + helper.claim.marker(SOURCE))],
        ]
        for comments in cases:
            with self.subTest(comments=comments):
                result, report = self.finish_thread(comments)
                self.assertEqual(result, 1)
                self.assertTrue(report["needs_reconciliation"])

    def test_finish_supervisor_receipt_is_not_a_session_authored_handoff(self):
        evidence = {**comment(99, "Closed session source-session\nEnded at: 2026-01-01T00:02:00Z\nSafe to exit: yes"),
                    "created_at": "2026-01-01T00:04:00Z", "updated_at": "2026-01-01T00:04:00Z"}
        body = helper.release.prepare_release(self.source, evidence, [self.source], {"sessions": [], "worktrees": []},
            actor=ACTOR, role="supervisor", releaser_session="supervisor", evidence_url=self.entry["handoff_url"], retained_prs=[], related=[])
        receipt = self.own_release(body=body, created_at="2026-01-01T00:05:00Z", updated_at="2026-01-01T00:05:00Z")
        self.entry["ended_at"] = "2026-01-01T00:06:00Z"
        result, report = self.finish_thread([self.source, receipt], evidence=evidence)
        self.assertEqual(result, 1)
        self.assertTrue(report["needs_reconciliation"])

    def test_finish_history_failure_preserves_per_issue_release_and_refusal(self):
        handoff = dict(self.reader.get_json.return_value)
        def read(path, **_kw):
            if path.endswith("/issues/2"):
                raise helper.GitHubReadError("temporarily unavailable", result=SimpleNamespace(status=503), diagnostics={})
            return handoff
        self.reader.get_json.side_effect = read
        self.reader.paged_json.side_effect = lambda path, **_kw: [] if path.endswith("/pulls") else [self.source]
        entry = {**self.entry, "_finish": True, "_finish_scope": "explicit scope"}
        with patch.object(helper, "preview_guards", return_value={"status": "passed_read_only"}), \
                patch.object(helper, "comment") as write, patch.object(helper, "run_release") as run:
            report = helper.release_session(self.reader, entry, ACTOR, "supervisor", [("test/a", 2), ("test/a", 1)], self.root, False)
        self.assertEqual([r["issue"] for r in report["issue_refusals"]], [2])
        self.assertEqual([r["claim_comment"] for r in report["claims"]], [self.source["id"]])
        write.assert_not_called()
        run.assert_not_called()

    def test_finish_status_only_ownership_is_not_a_read_only_session(self):
        result, report = self.finish_thread([], body="Claimed by trial\nSession: source-session")
        self.assertEqual(result, 1)
        self.assertTrue(report["needs_reconciliation"])
        result, report = self.finish_thread([self.source, self.own_release()],
            body=helper.claim.marker({**SOURCE, "branch": "work/unreleased"}))
        self.assertEqual(result, 1)
        self.assertTrue(report["needs_reconciliation"])

    def test_finish_noop_paths_keep_the_unqualified_verdict_gate(self):
        for comments in ([], [self.source, self.own_release()]):
            for verdict in ("Safe to exit: yes, if checks pass", "Safe to exit: yes.\nOne more line"):
                with self.subTest(comments=comments, verdict=verdict):
                    result, report = self.finish_thread(comments, verdict=verdict)
                    self.assertEqual(result, 1)
                    self.assertFalse(report["ok"])

    def test_handoff_discovery_uses_native_comment_write_not_shared_bot_read(self):
        receipt = json.dumps({"comment_action": "created", "comment": {"url": self.entry["handoff_url"]}})
        records = helper.session_record.read_jsonl(self.transcript)
        records.insert(1, {"timestamp": "2026-01-01T00:00:20Z", "payload": {"type": "function_call", "call_id": "write", "arguments": json.dumps({"cmd": "gh-comment issue 1 --body-file handoff.md"})}})
        records.insert(2, {"timestamp": "2026-01-01T00:00:30Z", "payload": {"type": "function_call_output", "call_id": "write", "output": receipt}})
        records.insert(2, {"timestamp": "2026-01-01T00:00:40Z", "payload": {"type": "function_call_output", "output": "Read https://github.com/test/a/issues/1#issuecomment-123"}})
        self.transcript.write_text("\n".join(json.dumps(r) for r in records))
        entry, _ = helper.discover_handoff(self.reader, {**self.entry, "handoff_url": None}, ACTOR)
        self.assertEqual(entry["handoff_url"], self.entry["handoff_url"])
        self.assertEqual(self.reader.get_json.call_count, 1)

    def test_legacy_shell_array_preserves_bound_write_receipt(self):
        records = helper.session_record.read_jsonl(self.transcript)
        records.insert(1, {"payload": {"type": "function_call", "call_id": "write",
            "arguments": json.dumps({"command": ["bash", "-lc", "gh-comment issue 1 --body-file handoff.md"]})}})
        records.insert(2, {"payload": {"type": "function_call_output", "call_id": "write",
            "output": json.dumps({"comment_action": "created", "comment": {"url": self.entry["handoff_url"]}})}})
        self.transcript.write_text("\n".join(json.dumps(r) for r in records))
        found, _ = helper.discover_handoff(self.reader, {**self.entry, "handoff_url": None}, ACTOR)
        self.assertEqual(found["handoff_url"], self.entry["handoff_url"])

    def test_progress_note_does_not_become_exact_session_closeout(self):
        for text in ("source-session: tests completed", "Other session handed off; source-session still running",
                     "Engineering handoff\nSession: another-session\nAlso source-session"):
            self.reader.paged_json.return_value = [comment(10, text)]
            with self.assertRaisesRegex(ValueError, "no authored handoff"):
                helper.discover_handoff(self.reader, {**self.entry, "handoff_url": None,
                    "_handoff_thread": ("test/a", 1)}, ACTOR)

    def test_legacy_abbreviated_write_receipt_and_unbound_final_link(self):
        records = helper.session_record.read_jsonl(self.transcript)
        records.insert(1, {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "body", "input": {"file_path": "handoff.md", "content": "Handoff"}}]}})
        records.insert(2, {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "write", "input": {"command": "gh-comment -R test/a --body-file handoff.md issue 1 | rg url"}}]}})
        records.insert(3, {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "write", "content": f'"url":"{self.entry["handoff_url"]}"\n0', "is_error": False}]}})
        # This models receipt shapes only; harness identity and safe exit remain Codex.
        self.transcript.write_text("\n".join(json.dumps(r) for r in records))
        found, _ = helper.discover_handoff(self.reader, {**self.entry, "handoff_url": None}, ACTOR)
        self.assertEqual(found["handoff_url"], self.entry["handoff_url"])
        records = helper.session_record.read_jsonl(self.transcript)
        records[3]["message"]["content"][0]["tool_use_id"] = "unrelated-read"
        records[-2]["payload"]["message"] = f"See {self.entry['handoff_url']}\nSafe to exit: yes"
        self.transcript.write_text("\n".join(json.dumps(r) for r in records))
        self.reader.paged_json.return_value = []
        with self.assertRaisesRegex(ValueError, "no authored handoff"):
            helper.discover_handoff(self.reader, {**self.entry, "handoff_url": None}, ACTOR)

    def summarized_write(self, command, output, started="2025-12-31T23:59:59.400Z", finished="2026-01-01T00:00:01.200Z",
                         body="Handoff: the lane's passwords moved.\nNext: land it.", posted=None):
        # Each call starts from the base transcript, so subtests stay independent.
        self.__dict__.setdefault("base_transcript", self.transcript.read_text())
        self.transcript.write_text(self.base_transcript)
        records = helper.session_record.read_jsonl(self.transcript)
        records[1:1] = [
            # The session writes the comment body itself before posting it.
            {"type": "assistant", "timestamp": started, "message": {"content": [
                {"type": "tool_use", "id": "body", "input": {"file_path": "c.md", "content": body}}]}},
            {"type": "assistant", "timestamp": started, "message": {"content": [
                {"type": "tool_use", "id": "write", "input": {"command": command}}]}},
            # Claude Code repeats tool blocks in untimestamped records.
            {"type": "api-request-blob", "message": {"content": [
                {"type": "tool_use", "id": "write", "input": {"command": command}}]}},
            {"type": "user", "timestamp": finished, "message": {"content": [
                {"type": "tool_result", "tool_use_id": "write", "content": output, "is_error": False}]}},
        ]
        self.transcript.write_text("\n".join(json.dumps(r) for r in records))
        self.reader.paged_json.return_value = []
        # The comment helper appends its operation marker to the posted body.
        self.reader.get_json.return_value = {**self.reader.get_json.return_value,
                                             "body": f"{posted or body}\n\n<!-- github-skill-operation:0123abcd -->"}
        return {**self.entry, "handoff_url": None}

    def test_summarized_write_created_during_the_call_is_bound(self):
        other = "https://github.com/test/a/issues/1#issuecomment-98"
        summary = "python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(\"url\"))'"
        shapes = {
            # LP3912-D2: heredoc body with an apostrophe, then two writes in one call.
            "two writes after a heredoc": (
                f"cat > c.md <<'EOF'\nHandoff: the lane's passwords moved.\nNext: land it.\nEOF\ngh-comment -R test/a --body-file c.md issue 2 | {summary}; "
                f"gh-comment -R test/a --body-file c.md issue 1 | {summary}",
                f"confirmed {other}\nconfirmed {self.entry['handoff_url']}"),
            # CS2069-C1: the write chained with a read whose summary prints a dict.
            "write chained with a read": (
                f"gh-comment issue 1 -R test/a --body-file c.md | {summary}; gh-pr.py --repo test/a checks 2 | {summary}",
                f"True None {self.entry['handoff_url']}\n{{'summary': {{'pendingCount': 3}}}}"),
            "body on stdin": (
                f"gh-comment issue 1 --body-file - <<'EOF' | {summary}\nHandoff: the lane's passwords moved.\nNext: land it.\nEOF",
                f"True None {self.entry['handoff_url']}"),
            "documented gh-pr.py argument order": (
                f"gh-pr.py --repo test/a comment 1 --body-file c.md | {summary}",
                f"True None {self.entry['handoff_url']}"),
        }
        for name, (command, output) in shapes.items():
            with self.subTest(name):
                found, _ = helper.discover_handoff(self.reader, self.summarized_write(command, output), ACTOR)
                self.assertEqual(found["handoff_url"], self.entry["handoff_url"])

    def test_another_sessions_comment_created_during_the_call_is_not_bound(self):
        # Session B posts with the same bot while session A's write is chained with a read.
        command = "gh-comment issue 1 --body-file c.md >/dev/null; gh api repos/test/a/issues/1/comments"
        outputs = {"bare URL": self.entry["handoff_url"], "read JSON": json.dumps([{"html_url": self.entry["handoff_url"]}])}
        bodies = {"names its own session": "B's handoff\nSession: session-B", "names no session": "B's handoff"}
        for (shape, output), (name, other_body) in itertools.product(outputs.items(), bodies.items()):
            with self.subTest(shape=shape, body=name):
                entry = self.summarized_write(command, output)
                self.reader.get_json.return_value = {**self.reader.get_json.return_value, "body": other_body}
                with self.assertRaisesRegex(ValueError, "no authored handoff"):
                    helper.discover_handoff(self.reader, entry, ACTOR)

    def test_text_the_session_typed_but_did_not_post_does_not_bind(self):
        # Another session's comment, printed by a chained read, matches text this session only searched for or typed.
        shapes = {
            "search argument": ("rg -F 'Engineering handoff: ready to merge' notes.md; ", "Engineering handoff: ready to merge"),
            "loop terminator": ("for f in a b; do echo $f; done; ", "done"),
        }
        for name, (prefix, other_body) in shapes.items():
            with self.subTest(name):
                entry = self.summarized_write(
                    f"{prefix}gh-comment issue 1 --body-file c.md >/dev/null; gh api repos/test/a/issues/comments/99 --jq .html_url",
                    self.entry["handoff_url"], posted=other_body)
                with self.assertRaisesRegex(ValueError, "no authored handoff"):
                    helper.discover_handoff(self.reader, entry, ACTOR)

    def test_body_file_edited_after_writing_binds_its_final_text(self):
        entry = self.summarized_write("gh-comment issue 1 --body-file /repo/c.md | jq -r .url", self.entry["handoff_url"],
                                      body="Draft handoff", posted="Final handoff")
        records = helper.session_record.read_jsonl(self.transcript)
        records.insert(2, {"type": "assistant", "timestamp": "2025-12-31T23:59:59.400Z", "message": {"content": [
            {"type": "tool_use", "id": "edit", "input": {"file_path": "c.md", "old_string": "Draft", "new_string": "Final"}}]}})
        self.transcript.write_text("\n".join(json.dumps(r) for r in records))
        found, _ = helper.discover_handoff(self.reader, entry, ACTOR)
        self.assertEqual(found["handoff_url"], self.entry["handoff_url"])

    def test_unrelated_command_heredoc_does_not_bind_a_read_comment(self):
        commands = (
            "rg -F -f - notes.md <<'EOF'\nOther handoff\nEOF\n"
            "gh-comment issue 1 --body-file c.md >/dev/null; gh api repos/test/a/issues/1/comments",
            "gh-comment issue 1 3<<'EOF' >/dev/null\nOther handoff\nEOF\n"
            "gh api repos/test/a/issues/1/comments",
        )
        for command in commands:
            with self.subTest(command=command):
                entry = self.summarized_write(command, self.entry["handoff_url"], posted="Other handoff")
                with self.assertRaisesRegex(ValueError, "no authored handoff"):
                    helper.discover_handoff(self.reader, entry, ACTOR)

    def test_different_absolute_body_paths_do_not_bind_by_basename(self):
        entry = self.summarized_write(
            "gh-comment issue 1 --body-file /tmp/c.md >/dev/null; gh api repos/test/a/issues/1/comments",
            self.entry["handoff_url"], posted="Other handoff")
        records = helper.session_record.read_jsonl(self.transcript)
        records[1]["message"]["content"][0]["input"].update(file_path="/repo/notes/c.md", content="Other handoff")
        self.transcript.write_text("\n".join(json.dumps(r) for r in records))
        with self.assertRaisesRegex(ValueError, "no authored handoff"):
            helper.discover_handoff(self.reader, entry, ACTOR)

    def test_literal_shell_variable_body_path_binds(self):
        entry = self.summarized_write(
            'body=/tmp/c.md; gh-comment issue 1 --body-file "$body" | jq -r .comment.url',
            self.entry["handoff_url"])
        records = helper.session_record.read_jsonl(self.transcript)
        records[1]["message"]["content"][0]["input"]["file_path"] = "/tmp/c.md"
        self.transcript.write_text("\n".join(json.dumps(r) for r in records))
        found, _ = helper.discover_handoff(self.reader, entry, ACTOR)
        self.assertEqual(found["handoff_url"], self.entry["handoff_url"])

    def test_recorded_workdir_resolves_relative_body_paths(self):
        for orchestrated, cwd in itertools.product((False, True), ("/repo/notes", "/tmp")):
            with self.subTest(orchestrated=orchestrated, cwd=cwd):
                entry = self.summarized_write("gh-comment issue 1 --body-file c.md", self.entry["handoff_url"])
                records = helper.session_record.read_jsonl(self.transcript)
                records[1]["message"]["content"][0]["input"]["file_path"] = "/repo/notes/c.md"
                args = {"cmd": "gh-comment issue 1 --body-file c.md", "workdir": cwd}
                # Replace both copies of the native call, retaining its result.
                records[2] = {"timestamp": records[2]["timestamp"], "payload": {
                    "type": "custom_tool_call" if orchestrated else "function_call", "call_id": "write",
                    "input" if orchestrated else "arguments": (
                        f'await tools.exec_command({{cmd: {json.dumps(args["cmd"])}, workdir: {json.dumps(cwd)}}})'
                        if orchestrated else json.dumps(args))}}
                records[3] = {"payload": {"type": "function_call_output", "call_id": "write", "output": self.entry["handoff_url"]}}
                del records[4]
                self.transcript.write_text("\n".join(json.dumps(r) for r in records))
                if cwd == "/repo/notes":
                    found, _ = helper.discover_handoff(self.reader, entry, ACTOR)
                    self.assertEqual(found["handoff_url"], self.entry["handoff_url"])
                else:
                    with self.assertRaisesRegex(ValueError, "no authored handoff"):
                        helper.discover_handoff(self.reader, entry, ACTOR)

    def test_relative_body_path_without_recorded_cwd_is_ambiguous(self):
        entry = self.summarized_write("gh-comment issue 1 --body-file c.md", self.entry["handoff_url"])
        records = helper.session_record.read_jsonl(self.transcript)
        del records[0]["payload"]["cwd"]
        self.transcript.write_text("\n".join(json.dumps(r) for r in records))
        with self.assertRaisesRegex(ValueError, "no authored handoff"):
            helper.discover_handoff(self.reader, entry, ACTOR)

    def test_body_path_variable_must_be_known_and_expandable(self):
        for path in ("'$body'", '"$unknown"'):
            with self.subTest(path=path):
                entry = self.summarized_write(
                    f"body=/repo/c.md; gh-comment issue 1 --body-file {path}", self.entry["handoff_url"])
                with self.assertRaisesRegex(ValueError, "no authored handoff"):
                    helper.discover_handoff(self.reader, entry, ACTOR)

    def test_untracked_redirect_contents_do_not_preserve_an_old_file_body(self):
        for command in ("printf changed > c.md", "cat other.md > c.md <<'EOF'\nOther handoff\nEOF"):
            with self.subTest(command=command):
                entry = self.summarized_write(
                    f"{command}\ngh-comment issue 1 --body-file c.md >/dev/null; gh api repos/test/a/issues/1/comments",
                    self.entry["handoff_url"])
                with self.assertRaisesRegex(ValueError, "no authored handoff"):
                    helper.discover_handoff(self.reader, entry, ACTOR)

    def test_codex_orchestrated_heredoc_body_is_bound(self):
        # Codex functions.exec records the shell command as an escaped JavaScript string literal.
        command = "cat > c.md <<'EOF'\nEngineering handoff\nReady for merge\nEOF\ngh-comment issue 1 --body-file c.md | jq -r .comment.url"
        records = helper.session_record.read_jsonl(self.transcript)
        records[1:1] = [
            {"timestamp": "2025-12-31T23:59:59.400Z", "payload": {"type": "custom_tool_call", "call_id": "write",
                "input": f"await tools.exec_command({{cmd: {json.dumps(command)}}})"}},
            {"timestamp": "2026-01-01T00:00:01.200Z", "payload": {"type": "custom_tool_call_output", "call_id": "write",
                "output": f"{self.entry['handoff_url']}\n{{}}"}},
        ]
        self.transcript.write_text("\n".join(json.dumps(r) for r in records))
        self.reader.paged_json.return_value = []
        self.reader.get_json.return_value = {**self.reader.get_json.return_value, "body": "Engineering handoff\nReady for merge"}
        found, _ = helper.discover_handoff(self.reader, {**self.entry, "handoff_url": None}, ACTOR)
        self.assertEqual(found["handoff_url"], self.entry["handoff_url"])

    def test_a_written_comment_that_names_another_session_is_not_its_handoff(self):
        # For example, a Supervisor attestation about the session it closed.
        entry = self.summarized_write("gh-comment issue 1 --body-file c.md", self.entry["handoff_url"],
                                      body="Closed session\nSession: session-B\nSafe to exit: yes")
        with self.assertRaisesRegex(ValueError, "no authored handoff"):
            helper.discover_handoff(self.reader, entry, ACTOR)

    def test_a_read_before_the_write_in_one_call_does_not_hide_the_write(self):
        summary = "python3 -c 'import json,sys; print(json.load(sys.stdin)[\"comment\"][\"url\"])'"
        entry = self.summarized_write(f"gh-pr.py --repo test/a checks 2; gh-comment issue 1 --body-file c.md | {summary}",
                                      f"{{'summary': {{'pendingCount': 3}}}}\n{self.entry['handoff_url']}")
        found, _ = helper.discover_handoff(self.reader, entry, ACTOR)
        self.assertEqual(found["handoff_url"], self.entry["handoff_url"])

    def test_url_printed_by_a_write_but_created_outside_the_call_is_not_bound(self):
        # A read chained into the write may print an older shared-bot comment, bare or in JSON.
        for output in (self.entry["handoff_url"], f"{self.entry['handoff_url']}\n{{}}"):
            with self.subTest(output=output):
                entry = self.summarized_write(
                    "gh-comment issue 1 --body-file c.md >/dev/null; gh api repos/test/a/issues/comments/99 --jq .html_url",
                    output, started="2026-01-01T00:00:30Z", finished="2026-01-01T00:00:35Z")
                with self.assertRaisesRegex(ValueError, "no authored handoff"):
                    helper.discover_handoff(self.reader, entry, ACTOR)

    def test_unplanned_active_inventory_followup_is_not_an_all_clear(self):
        entry = {**self.entry, "_inventory": True, "safe_exit_yes": True, "still_running": False,
                 "_inventory_claims": [self.inventory_row(claim_comment_id=2)]}
        self.reader.paged_json.return_value = [self.source, comment(2, "Claimed by trial\nSession: source-session")]
        report = helper.release_session(self.reader, entry, ACTOR, "supervisor", [("test/a", 1)], self.root, False)
        self.assertFalse(report["claims"])
        self.assertTrue(report["issue_refusals"])

    def test_missing_inventory_claim_and_pr_number_are_reported(self):
        entry = {**self.entry, "_inventory": True, "safe_exit_yes": True, "still_running": False,
                 "_inventory_claims": [self.inventory_row()]}
        handoff = dict(self.reader.get_json.return_value)
        for issue in ({"body": ""}, {"body": "", "pull_request": {}}):
            self.reader.get_json.side_effect = lambda path, **_kw: issue if path.endswith("/issues/1") else handoff
            self.reader.paged_json.return_value = []
            report = helper.release_session(self.reader, entry, ACTOR, "supervisor", [("test/a", 1)], self.root, False)
            self.assertFalse(report["claims"])
            self.assertTrue(report["issue_refusals"])

    def test_read_description_does_not_make_an_old_receipt_a_write(self):
        records = helper.session_record.read_jsonl(self.transcript)
        records.insert(1, {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "read", "input": {
            "command": "gh api repos/test/a/issues/comments/99 --jq .html_url", "description": "Verify gh-comment issue 1 posted"}}]}})
        records.insert(2, {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "read", "content": self.entry["handoff_url"]}]}})
        self.transcript.write_text("\n".join(json.dumps(r) for r in records))
        self.reader.paged_json.return_value = []
        with self.assertRaisesRegex(ValueError, "no authored handoff"):
            helper.discover_handoff(self.reader, {**self.entry, "handoff_url": None}, ACTOR)

    def test_malformed_historic_command_and_saved_receipt_are_not_writes(self):
        records = helper.session_record.read_jsonl(self.transcript)
        records.insert(1, {"payload": {"type": "custom_tool_call", "call_id": "read",
            "input": 'await tools.exec_command({cmd: "bad\ncommand"})'}})
        records.insert(2, {"payload": {"type": "function_call", "call_id": "read",
            "arguments": json.dumps({"cmd": "cat gh-comment issue 1"})}})
        records.insert(3, {"payload": {"type": "function_call_output", "call_id": "read",
            "output": json.dumps({"comment_action": "created", "comment": {"url": self.entry["handoff_url"]}})}})
        self.transcript.write_text("\n".join(json.dumps(r) for r in records))
        self.reader.paged_json.return_value = []
        with self.assertRaisesRegex(ValueError, "no authored handoff"):
            helper.discover_handoff(self.reader, {**self.entry, "handoff_url": None}, ACTOR)

    def test_truncated_orchestration_with_many_quotes_does_not_hang(self):
        records = helper.session_record.read_jsonl(self.transcript)
        records.insert(1, {"payload": {"type": "custom_tool_call", "call_id": "truncated",
            "input": 'await tools.exec_command({cmd: ' + '""' * 80}})
        self.transcript.write_text("\n".join(json.dumps(r) for r in records))
        # Keep a regressed parser from hanging the test runner indefinitely.
        script = """
import json, sys
from unittest.mock import Mock
import release_session_claims as helper
try:
    helper.discover_handoff(Mock(), json.loads(sys.argv[1]), sys.argv[2])
except ValueError as error:
    assert 'no authored handoff' in str(error), error
else:
    raise AssertionError('truncated command cannot establish authorship')
"""
        result = subprocess.run([sys.executable, "-c", script,
                                 json.dumps({**self.entry, "handoff_url": None}), ACTOR],
                                cwd=Path(helper.__file__).parent, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_interrupted_handoff_uses_exact_inventory_ownership_context(self):
        entry = {**self.entry, "handoff_url": None, "_inventory_claims": [self.inventory_row()]}
        self.reader.paged_json.return_value = [self.source]
        self.reader.get_json.return_value = {**self.source, "issue_url": "https://api.github.com/repos/test/a/issues/1"}
        found, _ = helper.discover_handoff(self.reader, entry, ACTOR)
        self.assertEqual(found["handoff_url"], "https://github.com/test/a/issues/1#issuecomment-1")
        self.assertIn("ownership record", found["_handoff_provenance"])
        self.source["user"]["login"] = "other-identity"
        with self.assertRaisesRegex(ValueError, "no authored handoff"):
            helper.discover_handoff(self.reader, entry, ACTOR)

    def test_inventory_repo_scope_preserves_excluded_rows(self):
        log = self.root / "inventory.json"
        log.write_text(json.dumps([self.inventory_row(), self.inventory_row(repo="test/b", claim_comment_id=2)]))
        argv = ["helper", "backfill", "--closed-log", str(log), "--supervisor-session", "supervisor",
                "--owner", "test", "--repo", "test/a", "--checkout-root", str(self.root),
                "--confirm-session-ended", "--verified-handoff", "--transcript-root", str(self.root)]
        output = io.StringIO()
        with patch.object(helper.sys, "argv", argv), \
                patch.object(helper.github_identity, "automation_login", return_value=ACTOR), \
                patch.object(helper, "release_session", return_value={"session_id": SOURCE["session"], "claims": []}) as run, \
                contextlib.redirect_stdout(output):
            self.assertEqual(helper.main(), 1)
        self.assertEqual(run.call_args.args[4], [("test/a", 1)])
        self.assertEqual(json.loads(output.getvalue())["reports"][0]["status"], "out_of_scope")

    def test_explicit_inventory_pr_with_bad_issue_link_is_preserved_as_refusal(self):
        entry = {**self.entry, "_inventory": True, "safe_exit_yes": True, "still_running": False,
                 "_inventory_claims": [self.inventory_row(retained_pr_url="https://github.com/test/a/pull/12")]}
        named = self.pull(12, "other-branch")
        named["body"] = "Part of #1"
        handoff = dict(self.reader.get_json.return_value)
        self.reader.get_json.side_effect = lambda path, **_kw: self.head_commit() if "/commits/" in path else named if "/pulls/" in path else handoff
        self.reader.paged_json.side_effect = lambda path, **_kw: [] if path.endswith("/pulls") else [self.source]
        report = helper.release_session(self.reader, entry, ACTOR, "supervisor", [("test/a", 1)], self.root, False)
        self.assertEqual(report["claims"][0]["retained_prs"], [named["html_url"]])
        self.assertEqual(report["claims"][0]["release_preflight"]["status"], "refused")
        self.assertTrue(report["issue_refusals"])

    def test_shared_bot_summary_needs_exact_native_id_and_original_timestamp(self):
        bound = {**comment(10, "Engineering handoff\nSession: source-session"),
                 "html_url": "https://github.com/test/a/issues/1#issuecomment-10",
                 "issue_url": "https://api.github.com/repos/test/a/issues/1"}
        self.reader.paged_json.return_value = [bound, comment(11, "Same shared bot; another session")]
        self.reader.get_json.return_value = bound
        row = {**self.entry, "handoff_url": None, "_inventory_claims": [self.inventory_row()]}
        found, _ = helper.discover_handoff(self.reader, row, ACTOR)
        self.assertEqual(found["handoff_url"], bound["html_url"])
        bound["updated_at"] = "2026-01-01T00:03:00Z"
        with self.assertRaisesRegex(ValueError, "no authored handoff"):
            helper.discover_handoff(self.reader, row, ACTOR)

    def test_inventory_names_retained_pr_in_dry_run_and_new_head_refuses(self):
        entry = {**self.entry, "_inventory": True, "safe_exit_yes": True, "still_running": False,
                 "_inventory_claims": [self.inventory_row(retained_pr_url="https://github.com/test/a/pull/12")]}
        named = self.pull(12, "other-branch")
        handoff = dict(self.reader.get_json.return_value)
        commit = self.head_commit()
        self.reader.get_json.side_effect = lambda path, **_kw: commit if "/commits/" in path else named if "/pulls/" in path else handoff
        self.reader.paged_json.side_effect = lambda path, **_kw: [] if path.endswith("/pulls") else [self.source]
        with patch.object(helper, "comment") as write, patch.object(helper, "run_release") as command:
            report = helper.release_session(self.reader, entry, ACTOR, "supervisor", [("test/a", 1)], self.root, False)
            self.assertEqual(report["claims"][0]["retained_prs"], [named["html_url"]])
            self.assertEqual(report["claims"][0]["release_preflight"]["status"], "passed_read_only")
            commit["commit"]["committer"]["date"] = "2026-01-01T00:03:00Z"
            with self.assertRaisesRegex(helper.PartialRelease, "commit activity after"):
                helper.release_session(self.reader, entry, ACTOR, "supervisor", [("test/a", 1)], self.root, False)
            write.assert_not_called()
            command.assert_not_called()

    def test_uncertain_identity_and_unverified_closure_refuse(self):
        for changes in ({"supervisor_owned": False}, {"pid": True}, {"tty": None},
                        {"process_exit_verified": False}, {"ended_at": "2026-01-01T00:02:00"}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                helper.validate_closure({**self.entry, **changes}, "supervisor")
        with self.assertRaises(ValueError):
            helper.validate_closure(self.entry, SOURCE["session"])

    def test_record_closure_uses_verified_verdict_time_and_preserves_it_on_retry(self):
        observed = helper.session_record.session_status(self.entry)
        log = self.root / "timed-close.json"
        supplied = {**self.entry, "verdict_at": self.entry["ended_at"]}
        closed = helper.record_closure(log, supplied, "supervisor")
        self.assertEqual(closed["verdict_at"], observed["timestamp"])
        self.assertEqual(json.loads(log.read_text())[0]["verdict_at"], observed["timestamp"])
        retried = helper.record_closure(log, supplied, "supervisor")
        self.assertEqual(retried["verdict_at"], closed["verdict_at"])
        self.assertEqual(retried["ended_at"], closed["ended_at"])

    def test_recorded_verdict_time_must_match_transcript_but_legacy_log_still_works(self):
        observed = helper.validate_closure(self.entry, "supervisor")
        helper.validate_closure({**self.entry, "verdict_at": observed["timestamp"]}, "supervisor")
        with self.assertRaises(ValueError):
            helper.validate_closure({**self.entry, "verdict_at": self.entry["ended_at"]}, "supervisor")

    def test_conditional_verdict_and_resumed_transcript_refuse(self):
        original = self.transcript.read_text()
        self.transcript.write_text(original.replace("Safe to exit: yes", "Safe to exit: yes, if CI passes"))
        with self.assertRaisesRegex(ValueError, "unqualified"):
            helper.validate_closure(self.entry, "supervisor")
        self.transcript.write_text(original.replace("00:01:01", "00:03:00"))
        with self.assertRaisesRegex(ValueError, "resumed"):
            helper.validate_closure(self.entry, "supervisor")

    def test_closure_retry_preserves_actual_end(self):
        log = self.root / "closed.json"
        first = helper.record_closure(log, self.entry, "supervisor")
        second = helper.record_closure(log, self.entry, "supervisor")
        self.assertEqual(first["ended_at"], second["ended_at"])
        self.assertEqual(len(json.loads(log.read_text())), 1)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(helper.record_closure(log, self.entry, "supervisor", persist=False)["ended_at"], first["ended_at"])
        with self.assertRaisesRegex(ValueError, "identity changed"):
            helper.record_closure(log, {**self.entry, "pid": 789}, "supervisor")

    def test_optional_admission_proof_failure_does_not_stop_verified_closure(self):
        log = self.root / "closed.json"
        entry = {**self.entry, "codex_home": str(self.root)}
        with patch.object(helper.closure, "evidence", side_effect=ValueError("account inventory unavailable")):
            closed = helper.record_closure(log, entry, "supervisor")
        self.assertTrue(helper.validate_closure(closed, "supervisor")["safe_verdict"])
        self.assertIsNone(closed.get("closure_evidence"))

    def test_adding_account_home_does_not_rewrite_or_block_a_legacy_receipt(self):
        log = self.root / "closed.json"
        original = helper.record_closure(log, self.entry, "supervisor")
        result = helper.record_closure(log, {**self.entry, "codex_home": str(self.root)}, "supervisor")
        self.assertEqual(result["ended_at"], original["ended_at"])
        self.assertIsNone(result.get("closure_evidence"))

    def test_resumed_source_requires_its_new_launch_generation_before_reclosing(self):
        log = self.shared_closure()
        metadata = {"launch_pid": 789, "launch_generation": "resumed-generation",
                    "launch_started_at": "2026-01-01T00:01:40Z", "codex_home": str(self.root.resolve())}
        with helper.closure.resume_guard(SOURCE["session"]):
            helper.closure.resumed_launch(SOURCE["session"], metadata, str(self.transcript), self.entry["tty"])
        with self.transcript.open("a") as stream:
            for payload in ({"type": "agent_message", "message": "Safe to exit: yes"}, {"type": "task_complete"}):
                stream.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "payload": payload}) + "\n")
        with self.assertRaises(ValueError):
            helper.record_closure(log, self.entry, "supervisor")
        replacement = {**self.entry, **metadata, "pid": metadata["launch_pid"]}
        closed = helper.record_closure(log, replacement, "supervisor")
        self.assertTrue(helper.closure.idle_eligible(closed))
        self.assertEqual(closed["launch_generation"], metadata["launch_generation"])

    def test_finish_uses_shared_default_and_preview_creates_nothing(self):
        ledger, log = self.root / "ledger.json", self.root / "shared/closed.json"
        ledger.write_text(json.dumps([self.entry]))
        for apply in (False, True):
            argv = ["helper", "finish", "--ledger", str(ledger), "--session-id", SOURCE["session"],
                    "--supervisor-session", "supervisor", "--owner", "test", "--checkout-root", str(self.root),
                    "--confirm-session-ended", "--verified-handoff", "--transcript-root", str(self.root)]
            if apply:
                argv.append("--apply")
            with patch.object(helper.sys, "argv", argv), \
                    patch.object(helper.closure, "closed_log", return_value=log), \
                    patch.object(helper.github_identity, "automation_login", return_value=ACTOR), \
                    patch.object(helper, "GitHubReader", return_value=self.reader), \
                    patch.object(helper, "discover", return_value=([("test/a", 1)], ["test/a"])), \
                    patch.object(helper, "release_session", return_value={}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(helper.main(), 0)
            self.assertEqual(log.exists(), apply)
            if apply:
                self.assertEqual(json.loads(log.read_text())[0]["session_id"], SOURCE["session"])
            else:
                self.assertFalse(log.parent.exists())

    def shared_closure(self):
        import github_plan_sessions
        sessions = self.root / "sessions"
        sessions.mkdir()
        self.transcript.rename(sessions / self.transcript.name)
        self.transcript = sessions / self.transcript.name
        self.entry.update(transcript=str(self.transcript), codex_home=str(self.root))
        self.enterContext(patch.object(github_plan_sessions, "codex_homes", return_value=[self.root.resolve()]))
        self.enterContext(patch.object(helper.closure, "host_identity", return_value={"host": "fixture", "boot": "boot-a", "uid": 1}))
        log = self.root / "closed.json"
        self.enterContext(patch.object(helper.closure, "closed_log", return_value=log))
        helper.record_closure(log, self.entry, "supervisor")
        return log

    def test_shared_receipt_is_fresh_read_only_evidence(self):
        log = self.shared_closure()
        before = log.read_bytes(), log.stat().st_mtime_ns
        result = helper.closure.verified_record(SOURCE["session"], self.root)
        self.assertEqual(result["session_id"], SOURCE["session"])
        self.assertEqual((log.read_bytes(), log.stat().st_mtime_ns), before)
        with patch.object(helper, "inventory", return_value=[(123, "another-tty", "unrelated-process")]):
            self.assertIsNone(helper.closure.verified_record(SOURCE["session"], self.root))
        with patch.object(helper, "inventory", return_value=[(456, "other-tty", "codex --remote unix:///other")]):
            self.assertIsNotNone(helper.closure.verified_record(SOURCE["session"], self.root))

    def test_shared_receipt_refuses_resumed_and_rewritten_transcripts(self):
        self.shared_closure()
        original = self.transcript.read_text()
        self.transcript.write_text(original + json.dumps({"timestamp": "2026-01-01T00:01:30Z",
            "payload": {"type": "user_message", "message": "resume"}}) + "\n")
        self.assertIsNone(helper.closure.verified_record(SOURCE["session"], self.root))
        self.transcript.write_text(original)
        # Even a rewrite restoring the old final verdict invalidates the receipt.
        self.assertIsNone(helper.closure.verified_record(SOURCE["session"], self.root))

    def test_new_transcript_copy_invalidates_shared_receipt(self):
        import github_plan_sessions
        self.shared_closure()
        other = self.root / "other-account"
        archived = other / "archived_sessions"
        archived.mkdir(parents=True)
        (archived / self.transcript.name).write_bytes(self.transcript.read_bytes())
        with patch.object(github_plan_sessions, "codex_homes", return_value=[self.root.resolve(), other.resolve()]):
            self.assertIsNone(helper.closure.verified_record(SOURCE["session"], self.root))

    def test_shared_receipt_refuses_home_boot_malformed_and_missing_evidence(self):
        log = self.shared_closure()
        original = log.read_text()
        self.assertIsNone(helper.closure.verified_record(SOURCE["session"], self.root / "another-home"))
        with patch.object(helper.closure, "host_identity", return_value={"host": "fixture", "boot": "boot-b", "uid": 1}):
            self.assertIsNone(helper.closure.verified_record(SOURCE["session"], self.root))
        saved = json.loads(original)[0]
        malformed = [json.dumps([{**saved, "closure_evidence": value}]) for value in (None, "invalid", [])]
        malformed.append(json.dumps([{**saved, "pid": None, "_inventory": True,
                                      "safe_exit_yes": True, "still_running": False}]))
        for value in ("{", "{}", "[null]", "[]", json.dumps([saved, saved]), *malformed):
            log.write_text(value)
            self.assertIsNone(helper.closure.verified_record(SOURCE["session"], self.root))
        log.write_text(original)
        log.chmod(0o644)
        self.assertIsNone(helper.closure.verified_record(SOURCE["session"], self.root))
        log.unlink()
        self.assertIsNone(helper.closure.verified_record(SOURCE["session"], self.root))
        self.assertFalse(log.exists())

    def test_cross_repository_discovery_includes_closed_issue_comments(self):
        def paged(path, **kwargs):
            if path == "/installation/repositories":
                return [{"full_name": "test/a"}, {"full_name": "test/b"}, {"full_name": "other/c"}]
            self.assertIn("since", kwargs["required_query_params"])
            if path.endswith("/issues"):
                return []
            repo = path.split("/")[3]
            return [{**self.source, "issue_url": f"https://api.github.com/repos/test/{repo}/issues/1"}]
        self.reader.paged_json.side_effect = paged
        issues, repos = helper.discover(self.reader, "test", [self.entry])
        self.assertEqual(issues, [("test/a", 1), ("test/b", 1)])
        self.assertEqual(repos, ["test/a", "test/b"])
        with self.assertRaises(ValueError):
            helper.discover(self.reader, "test", [self.entry], ["other/c"])

    def test_complete_inventory_failure_does_not_become_empty(self):
        self.reader.paged_json.side_effect = helper.GitHubReadShapeError("partial page")
        with self.assertRaises(helper.GitHubReadShapeError):
            helper.discover(self.reader, "test", [self.entry])

    def test_empty_owner_inventory_is_not_success(self):
        self.reader.paged_json.return_value = [{"full_name": "other/repo"}]
        with self.assertRaisesRegex(ValueError, "no repositories"):
            helper.discover(self.reader, "test", [self.entry])

    def test_unrelated_malformed_marker_does_not_block_discovery(self):
        self.reader.paged_json.return_value = [{"body": "<!-- github-plan:claim invalid -->"}]
        self.assertEqual(helper.discover(self.reader, "test", [self.entry], ["test/a"])[0], [])
        self.reader.paged_json.return_value = [{"body": "source-session\n<!-- github-plan:claim invalid -->", "html_url": "trial-comment"}]
        with self.assertRaisesRegex(ValueError, "trial-comment"):
            helper.discover(self.reader, "test", [self.entry], ["test/a"])

    def test_backfill_reused_tty_is_not_source_identity_but_pid_still_refuses(self):
        old = {**self.entry, "_backfill": True}
        with patch.object(helper, "inventory", return_value=[(456, "ttys123", "codex resume 11111111-1111-4111-8111-111111111111")]):
            self.assertTrue(helper.validate_closure(old, "supervisor")["safe_verdict"])
            with self.assertRaises(ValueError):
                helper.validate_closure(self.entry, "supervisor")
        for command, harness in (("codex resume --last", "codex"), ("claude -c", "claude"), ("node /bin/claude.js", "claude")):
            with patch.object(helper, "inventory", return_value=[(456, "ttys123", command)]):
                with self.assertRaises(ValueError):
                    helper.verify_process_exit({**old, "harness": harness}, ended_at=helper.release.stamp(old["ended_at"]))
        with patch.object(helper, "inventory", return_value=[(123, "other", "codex")]):
            with self.assertRaises(ValueError):
                helper.validate_closure(old, "supervisor")

    def test_backfill_reports_bad_record_and_continues_verified_session(self):
        entries = [{**self.entry, "session_id": "bad-session", "process_exit_verified": False}, self.entry]
        (self.root / "closed.json").write_text(json.dumps(entries))
        argv = ["helper", "backfill", "--closed-log", str(self.root / "closed.json"),
                "--owner", "test", "--checkout-root", str(self.root), "--supervisor-session", "supervisor",
                "--confirm-session-ended", "--verified-handoff", "--transcript-root", str(self.root)]
        output = io.StringIO()
        with patch.object(helper.sys, "argv", argv), patch.object(helper.github_identity, "automation_login", return_value=ACTOR), \
                patch.object(helper.session_record, "load_ledger", return_value=entries), \
                patch.object(helper, "discover", return_value=([], ["test/a"])), \
                patch.object(helper, "release_session", return_value={"session_id": "source-session", "claims": []}) as run, \
                contextlib.redirect_stdout(output):
            self.assertEqual(helper.main(), 1)
        self.assertEqual(run.call_count, 1)
        result = json.loads(output.getvalue())
        self.assertIn("error", result["reports"][0])
        self.assertEqual(result["reports"][1]["session_id"], "source-session")

    def test_exact_authored_release_and_legacy_release_are_respected(self):
        for body in ("Released claim 1", "Released by trial"):
            self.reader.paged_json.return_value = [self.source, comment(2, body)]
            self.assertEqual(helper.active_claims(self.reader, "test/a", 1, SOURCE["session"], ACTOR), [])
        for body, author in (("Released claim 1 if done", ACTOR), ("Released claim 1", "other")):
            self.reader.paged_json.return_value = [self.source, comment(2, body, author)]
            self.assertEqual(len(helper.active_claims(self.reader, "test/a", 1, SOURCE["session"], ACTOR)), 1)

    def test_foreign_claim_and_unstructured_source_report_without_release(self):
        self.reader.paged_json.return_value = [{**self.source, "user": {"login": "human"}}]
        with self.assertRaisesRegex(ValueError, "another identity"):
            helper.active_claims(self.reader, "test/a", 1, SOURCE["session"], ACTOR)

    def test_released_source_with_unreleased_followup_requires_reconciliation(self):
        self.reader.paged_json.return_value = [self.source,
            comment(2, "Claimed by trial\nSession: source-session"), comment(3, "Released claim 1")]
        with self.assertRaisesRegex(ValueError, "hand-written claim 2"):
            helper.active_claims(self.reader, "test/a", 1, SOURCE["session"], ACTOR)
        self.reader.paged_json.return_value.append(comment(4, "Released claim 2"))
        self.assertEqual(helper.active_claims(self.reader, "test/a", 1, SOURCE["session"], ACTOR), [])
        self.reader.paged_json.return_value = [comment(2, "Claimed by trial\nSession: source-session")]
        with self.assertRaisesRegex(ValueError, "hand-written"):
            helper.active_claims(self.reader, "test/a", 1, SOURCE["session"], ACTOR)

    def test_related_followup_is_passed_to_existing_release_gate(self):
        self.reader.paged_json.return_value = [self.source, comment(2, "Claimed by trial\nSession: source-session")]
        found = helper.active_claims(self.reader, "test/a", 1, SOURCE["session"], ACTOR)
        self.assertEqual(found[0][1], [2])

    def test_related_alias_without_exact_session_or_before_source_is_not_released(self):
        for body in ("Claimed by trial", "Claimed by trial\nSession: another-session"):
            self.reader.paged_json.return_value = [self.source, comment(2, body)]
            self.assertEqual(helper.active_claims(self.reader, "test/a", 1, SOURCE["session"], ACTOR)[0][1], [])
        self.reader.paged_json.return_value = [comment(2, "Claimed by trial\nSession: source-session"), self.source]
        self.assertEqual(helper.active_claims(self.reader, "test/a", 1, SOURCE["session"], ACTOR)[0][1], [])

    def test_unavailable_or_foreign_handoff_is_not_accepted(self):
        self.reader.get_json.return_value["user"]["login"] = "human"
        with self.assertRaisesRegex(ValueError, "handoff identity"):
            helper.handoff(self.reader, self.entry, ACTOR)

    def plan_reader(self, path, **_kwargs):
        if path.endswith("/pulls"):
            return [{"html_url": "https://github.com/test/a/pull/5", "head": {"ref": SOURCE["branch"]}}]
        return [self.source]

    @staticmethod
    def pull(number, branch):
        return {"html_url": f"https://github.com/test/a/pull/{number}", "number": number,
                "body": "Refs #1", "user": {"login": ACTOR}, "state": "open",
                "head": {"ref": branch, "sha": "fixture-head", "repo": {"full_name": "test/a"}},
                "base": {"repo": {"full_name": "test/a"}}}

    @staticmethod
    def head_commit():
        return {"commit": {"committer": {"date": "2026-01-01T00:01:00Z"}}}

    def test_dry_run_finds_retained_pr_without_writes(self):
        self.reader.paged_json.side_effect = self.plan_reader
        handoff = dict(self.reader.get_json.return_value)
        self.reader.get_json.side_effect = lambda path, **_kw: self.head_commit() if "/commits/" in path else self.pull(5, SOURCE["branch"]) if "/pulls/" in path else handoff
        with patch.object(helper, "comment") as write, patch.object(helper, "run_release") as run:
            report = helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1)], self.root, False)
        write.assert_not_called()
        run.assert_not_called()
        self.assertEqual(report["claims"][0]["retained_prs"], ["https://github.com/test/a/pull/5"])
        self.assertTrue(report["claims"][0]["attestation_body"].startswith("Closed session source-session\nEnded at: 2026-01-01T00:02:00Z\nSafe to exit: yes\n"))

    def test_exact_handoff_pr_url_does_not_select_shorter_pr_number(self):
        self.reader.get_json.return_value["body"] = "Retained PR: https://github.com/test/a/pull/12"
        named = self.pull(12, "other-branch")
        handoff = dict(self.reader.get_json.return_value)
        self.reader.get_json.side_effect = lambda path, **_kw: self.head_commit() if "/commits/" in path else named if path.endswith("/pulls/12") else handoff
        def paged(path, **_kwargs):
            return [{"html_url": "https://github.com/test/a/pull/1", "head": {"ref": "other-branch"}}] if path.endswith("/pulls") else [self.source]
        self.reader.paged_json.side_effect = paged
        with patch.object(helper.claim, "pr_issue_reference", return_value=True):
            report = helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1)], self.root, False)
        self.assertEqual(report["claims"][0]["retained_prs"], [named["html_url"]])

    def test_resumed_harness_with_new_pid_and_tty_refuses_backfill(self):
        for command in ("codex resume source-session", "node /bin/claude --resume source-session", "bun /bin/claude.js --resume=source-session"):
            with patch.object(helper, "inventory", return_value=[(456, "another-tty", command)]):
                with self.assertRaisesRegex(ValueError, "resumed harness"):
                    helper.validate_closure({**self.entry, "_backfill": True}, "supervisor")

    def test_unrelated_harness_prompt_quoting_is_not_a_process_parser_error(self):
        with patch.object(helper, "inventory", return_value=[(456, "another-tty", "codex exec don't change anything")]):
            self.assertTrue(helper.validate_closure(self.entry, "supervisor")["safe_verdict"])

    def test_newer_native_metadata_in_original_or_other_account_refuses(self):
        original = self.transcript.read_text()
        metadata = json.dumps({"type": "session_meta", "timestamp": "2026-01-01T00:03:00Z", "payload": {"id": "source-session"}}) + "\n"
        other = self.root / "another-account"
        other.mkdir()
        (other / self.transcript.name).write_text(original + metadata)
        with self.assertRaisesRegex(ValueError, "configured native transcript"):
            helper.validate_closure({**self.entry, "_transcript_roots": [str(other)]}, "supervisor")
        self.transcript.write_text(original + metadata)
        with self.assertRaisesRegex(ValueError, "configured native transcript"):
            helper.validate_closure(self.entry, "supervisor")

    def test_roots_use_existing_account_mapping_or_explicit_paths(self):
        config = {"accounts": [{"provider": "openai", "env": {"CODEX_HOME": str(self.root)}},
                                {"provider": "anthropic", "env": {"CLAUDE_CONFIG_DIR": str(self.root / "claude")}}]}
        with patch.object(helper.account_choice, "load_config", return_value=config):
            self.assertEqual(helper.transcript_roots("codex", None, []), [str(self.root / "sessions"), str(self.root / "archived_sessions")])
            self.assertEqual(helper.transcript_roots("claude", None, []), [str(self.root / "claude/projects")])
        with patch.object(helper.account_choice, "load_config", side_effect=ValueError("missing")):
            self.assertEqual(helper.transcript_roots("codex", None, [self.root]), [str(self.root.resolve())])

    def test_deleted_attestation_restores_claim_and_does_not_poison_other_session(self):
        evidence = {**comment(99, "Closed session source-session\nEnded at: 2026-01-01T00:02:00Z\nSafe to exit: yes"),
                    "created_at": "2026-01-01T00:04:00Z", "updated_at": "2026-01-01T00:04:00Z"}
        body = helper.release.prepare_release(self.source, evidence, [self.source], {"sessions": [], "worktrees": []},
             actor=ACTOR, role="supervisor", releaser_session="supervisor", evidence_url=self.entry["handoff_url"], retained_prs=[], related=[])
        self.reader.paged_json.return_value = [self.source, comment(2, body)]
        self.reader.get_json.side_effect = helper.GitHubReadError("deleted", result=SimpleNamespace(status=404), diagnostics={})
        self.assertEqual(len(helper.active_claims(self.reader, "test/a", 1, SOURCE["session"], ACTOR)), 1)
        self.assertEqual(helper.active_claims(self.reader, "test/a", 1, "independent-session", ACTOR), [])

    def test_foreign_or_closed_unmerged_handoff_pr_is_not_retained(self):
        handoff = dict(self.reader.get_json.return_value)
        handoff["body"] = "https://github.com/test/a/pull/12"
        for pr in ({"user": {"login": "human"}, "state": "open"},
                   {"user": {"login": ACTOR}, "state": "closed", "merged_at": None}):
            named = {"html_url": "https://github.com/test/a/pull/12", "head": {"ref": "other-branch"}, **pr}
            self.reader.get_json.side_effect = lambda path, **_kw: named if path.endswith("/pulls/12") else handoff
            self.reader.paged_json.side_effect = lambda path, **_kw: [] if path.endswith("/pulls") else [self.source]
            with patch.object(helper.claim, "pr_issue_reference", return_value=True):
                report = helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1)], self.root, False)
            self.assertEqual(report["claims"][0]["retained_prs"], [])

    def test_finish_apply_records_private_log_and_lock_refuses_second_operation(self):
        ledger, log = self.root / "ledger.json", self.root / "closed.json"
        ledger.write_text(json.dumps([self.entry]))
        argv = ["helper", "finish", "--ledger", str(ledger), "--closed-log", str(log), "--session-id", "source-session",
                "--owner", "test", "--checkout-root", str(self.root), "--supervisor-session", "supervisor",
                "--confirm-session-ended", "--verified-handoff", "--transcript-root", str(self.root), "--apply"]
        with patch.object(helper.sys, "argv", argv), patch.object(helper.github_identity, "automation_login", return_value=ACTOR), \
                patch.object(helper, "discover", return_value=([], ["test/a"])), \
                patch.object(helper, "release_session", return_value={"session_id": "source-session", "claims": []}) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(helper.main(), 0)
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            first = json.loads(log.read_text())[0]["ended_at"]
            self.assertEqual(helper.main(), 0)
            self.assertEqual(json.loads(log.read_text())[0]["ended_at"], first)
            with Path(str(log) + ".lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(helper.main(), 1)
            self.assertEqual(run.call_count, 2)

    def test_issue_refused_in_preflight_gets_no_attestation(self):
        self.reader.paged_json.side_effect = lambda path, **_kw: [] if path.endswith("/pulls") else [self.source]
        with patch.object(helper.claim, "run_read", return_value="https://github.com/test/a.git"), \
                patch.object(helper, "preview_guards", side_effect=helper.release.IssueReleaseRefusal("wrong issue")), \
                patch.object(helper, "comment") as write, patch.object(helper, "run_release") as run:
            report = helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1)], self.root, True)
        self.assertTrue(report["issue_refusals"])
        write.assert_not_called()
        run.assert_not_called()

    def test_session_wide_preflight_checks_every_plan_before_first_write(self):
        self.reader.paged_json.side_effect = lambda path, **_kw: [] if path.endswith("/pulls") else [self.source]
        with patch.object(helper.claim, "run_read", side_effect=lambda _args, *, cwd: f"https://github.com/test/{cwd.name}.git"), \
                patch.object(helper, "preview_guards", side_effect=[{"status": "passed_read_only"}, ValueError("later retained head is newer")]), \
                patch.object(helper, "comment") as write, patch.object(helper, "run_release") as run:
            with self.assertRaisesRegex(helper.PartialRelease, "later retained head"):
                helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1), ("test/b", 1)], self.root, True)
        write.assert_not_called()
        run.assert_not_called()

    def test_apply_posts_attestation_previews_then_releases(self):
        self.reader.paged_json.side_effect = self.plan_reader
        handoff = dict(self.reader.get_json.return_value)
        self.reader.get_json.side_effect = lambda path, **_kw: self.head_commit() if "/commits/" in path else self.pull(5, SOURCE["branch"]) if "/pulls/" in path else handoff
        calls = []
        def write(*_args, **_kwargs):
            calls.append("attestation")
            return {"comment": {"url": self.entry["handoff_url"]}}
        def run(*args):
            calls.append("preview" if args[-1] else "release")
            return {"release_body": "Released claim 1", "ok": True}
        with patch.object(helper.claim, "run_read", return_value="https://github.com/test/a.git"), \
                patch.object(helper, "comment", side_effect=write), patch.object(helper, "run_release", side_effect=run):
            report = helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1)], self.root, True)
        self.assertEqual(calls, ["attestation", "preview", "release"])
        self.assertTrue(report["claims"][0]["release"]["ok"])

    def test_partial_preview_failure_retains_attestation_and_stops(self):
        self.reader.paged_json.side_effect = lambda path, **_kw: [] if path.endswith("/pulls") else [self.source]
        handoff = dict(self.reader.get_json.return_value)
        self.reader.get_json.side_effect = lambda path, **_kw: self.head_commit() if "/commits/" in path else self.pull(5, SOURCE["branch"]) if "/pulls/" in path else handoff
        with patch.object(helper.claim, "run_read", side_effect=lambda _args, *, cwd: f"https://github.com/test/{cwd.name}.git"), \
                patch.object(helper, "comment", return_value={"comment": {"url": self.entry["handoff_url"]}}), \
                patch.object(helper, "run_release", side_effect=ValueError("commit activity after closure")) as run:
            with self.assertRaises(helper.PartialRelease) as caught:
                helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1), ("test/b", 1)], self.root, True)
        self.assertEqual(caught.exception.report["claims"][0]["evidence_url"], self.entry["handoff_url"])
        self.assertEqual(run.call_count, 1)

    def test_confirmed_issue_preview_refusal_continues_independent_claim(self):
        self.reader.paged_json.side_effect = lambda path, **_kw: [] if path.endswith("/pulls") else [self.source]
        def remote(_args, *, cwd):
            return f"https://github.com/test/{cwd.name}.git"
        def run(*args):
            if args[0] == "test/a":
                raise helper.ConfirmedIssueRefusal({"error": "retained work conflict", "release_refusal_scope": helper.release.ISSUE_REFUSAL_SCOPE})
            return {"release_body": "Released claim 1", "ok": True}
        with patch.object(helper.claim, "run_read", side_effect=remote), \
                patch.object(helper, "comment", return_value={"comment": {"url": self.entry["handoff_url"]}}), \
                patch.object(helper, "run_release", side_effect=run):
            report = helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1), ("test/b", 1)], self.root, True)
        self.assertEqual(report["issue_refusals"][0]["repository"], "test/a")
        self.assertTrue(report["claims"][1]["release"]["ok"])

    def test_live_process_after_attestation_prevents_release(self):
        self.reader.paged_json.side_effect = self.plan_reader
        def write(*_args, **_kwargs):
            self.transcript.write_text(self.transcript.read_text() + json.dumps({"payload": {"type": "task_started"}}) + "\n")
            return {"comment": {"url": self.entry["handoff_url"]}}
        with patch.object(helper.claim, "run_read", return_value="https://github.com/test/a.git"), \
                patch.object(helper, "comment", side_effect=write), patch.object(helper, "run_release") as run:
            with self.assertRaises(helper.PartialRelease):
                helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1)], self.root, True)
        run.assert_not_called()

    def test_wrong_checkout_refuses_before_attestation(self):
        self.reader.paged_json.side_effect = self.plan_reader
        with patch.object(helper.claim, "run_read", return_value="https://github.com/other/a.git"), patch.object(helper, "comment") as write:
            report = helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1)], self.root, True)
            self.assertIn("checkout", report["issue_refusals"][0]["error"])
        write.assert_not_called()

    def test_discovery_finds_status_only_native_ownership(self):
        self.reader.paged_json.side_effect = lambda path, **_kw: [{"number": 9, "body": "Claimed by trial\nSession: source-session"}] if path.endswith("/issues") else []
        self.assertEqual(helper.discover(self.reader, "test", [self.entry], ["test/a"])[0], [("test/a", 9)])

    def test_legacy_and_status_only_sources_are_reported_instead_of_skipped(self):
        for field in ("Session: source-session", "Session ID: source-session"):
            self.reader.paged_json.return_value = [comment(2, "Claimed by trial\n" + field)]
            report = helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1)], self.root, False)
            self.assertEqual(len(report["issue_refusals"]), 1)
            self.assertEqual(report["claims"], [])
        self.reader.paged_json.return_value = []
        self.reader.get_json.return_value["body"] = "Claimed by trial\nSession: source-session"
        report = helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1)], self.root, False)
        self.assertIn("status-only", report["issue_refusals"][0]["error"])

    def test_foreign_issue_does_not_block_independent_claim_release(self):
        foreign = {**self.source, "user": {"login": "human"}}
        def paged(path, **_kwargs):
            if path.endswith("/pulls"):
                return []
            return [foreign] if "/test/a/" in path else [self.source]
        self.reader.paged_json.side_effect = paged
        with patch.object(helper.claim, "run_read", return_value="https://github.com/test/b.git"), \
                patch.object(helper, "comment", return_value={"comment": {"url": self.entry["handoff_url"]}}) as write, \
                patch.object(helper, "run_release", return_value={"release_body": "Released claim 1", "ok": True}):
            report = helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1), ("test/b", 1)], self.root, True)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(report["issue_refusals"][0]["repository"], "test/a")
        self.assertEqual(report["claims"][0]["repository"], "test/b")

    def test_release_subprocess_argv_is_accepted_by_the_actual_plan_parser(self):
        plan = importlib.import_module("gh-plan")
        envelope = {"ok": True, "outcome_certainty": "confirmed"}
        result = SimpleNamespace(returncode=0, stdout=json.dumps(envelope))
        with patch.object(helper.subprocess, "run", return_value=result) as run:
            self.assertEqual(helper.run_release("test/a", 1, 2, [3], ["https://github.com/test/a/pull/4"], self.entry["handoff_url"], "supervisor", self.root, True), envelope)
        parsed = plan.build_parser().parse_args(run.call_args.args[0][3:])
        self.assertEqual(parsed.claim_comment, 2)
        self.assertEqual(parsed.related_claim_comment, [3])
        self.assertEqual(parsed.retained_pr, ["https://github.com/test/a/pull/4"])
        self.assertTrue(parsed.confirm_session_ended and parsed.dry_run)
        self.assertEqual(run.call_args.kwargs["cwd"], self.root / "a")

    def test_release_envelope_distinguishes_reconciliation_and_issue_refusals(self):
        args = ("test/a", 1, 2, [], [], self.entry["handoff_url"], "supervisor", self.root)
        success = {"ok": True, "outcome_certainty": "reconciled_applied"}
        with patch.object(helper.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(success))):
            self.assertEqual(helper.run_release(*args, False), success)
        refusal = {"ok": False, "release_refusal_scope": helper.release.ISSUE_REFUSAL_SCOPE}
        with patch.object(helper.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout=json.dumps(refusal))):
            with self.assertRaises(helper.ConfirmedIssueRefusal):
                helper.run_release(*args, True)
            with self.assertRaises(ValueError) as caught:
                helper.run_release(*args, False)
            self.assertNotIsInstance(caught.exception, helper.ConfirmedIssueRefusal)
        refusal["release_refusal_scope"] = "session_or_unknown"
        with patch.object(helper.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout=json.dumps(refusal))):
            with self.assertRaises(ValueError) as caught:
                helper.run_release(*args, True)
            self.assertNotIsInstance(caught.exception, helper.ConfirmedIssueRefusal)


if __name__ == "__main__":
    unittest.main()
