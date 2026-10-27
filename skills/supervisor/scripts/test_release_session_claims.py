#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Disposable transcripts and GitHub adapters exercise session claim release."""

import json
import os
import contextlib
import io
import itertools
import fcntl
import importlib
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
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
    def test_inventory_backfill_refuses_unresolved_resume_after_the_original_exit(self) -> None:
        source = {"session_id": "01a0fcce-ef3c-74e3-af74-6721809e4990", "harness": "codex", "_inventory": True, "_backfill": True}
        cutoff = helper.release.stamp("2026-01-01T00:02:00Z")
        for command in ("codex resume --last", "codex resume"):
            with (self.subTest(command=command), patch.object(helper, "inventory", return_value=[(543, "ttys002", command)]),
                  patch.object(helper.closure, "process_started_at", return_value=cutoff + timedelta(minutes=1)),
                  patch.object(helper.closure, "guarded_launch_endpoint", return_value=None), self.assertRaises(ValueError)):
                helper.verify_process_exit(source, ended_at=cutoff)

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
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "CODEX_HOME": str(self.root / "account")}))
        remote = patch.object(helper.claim, "run_read", side_effect=lambda _args, *, cwd: f"https://github.com/test/{cwd.name}.git")
        remote.start()
        self.addCleanup(remote.stop)
        local = patch.object(helper.claim, "local_inventory", return_value={"worktrees": []})
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
                "--transcript-root", str(self.root)] + (["--apply"] if apply else [])
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

    def test_loaded_inventory_preserves_explicit_handoff_and_every_named_retained_pr(self):
        handoff_url = self.entry["handoff_url"]
        rows = [self.inventory_row(handoff_url=handoff_url),
                self.inventory_row(handoff_url=handoff_url, retained_pr_url="https://github.com/test/a/pull/13")]
        entries, errors = helper.backfill_inventory.load(rows, {"codex": [str(self.root)]})
        self.assertFalse(errors)
        self.assertEqual(len(entries[0]["_inventory_claims"]), 2)
        source = {**self.source, "issue_url": "https://api.github.com/repos/test/a/issues/1"}
        public = {**comment(99, "Session: source-session\nRetained PRs: https://github.com/test/a/pull/12 https://github.com/test/a/pull/14"),
                  "issue_url": "https://api.github.com/repos/test/a/issues/1"}
        named = {n: self.pull(n, f"repair-{n}") for n in (12, 13, 14)}
        def read(path, **_kwargs):
            if "/pulls/" in path:
                return named[int(path.rsplit("/", 1)[1])]
            if "/commits/" in path:
                return self.head_commit()
            if path.endswith("/issues/comments/99"):
                return public
            if path.endswith("/issues/comments/1"):
                return source
            return {"number": 1, "body": ""}
        self.reader.get_json.side_effect = read
        self.reader.paged_json.side_effect = lambda path, **_kw: [] if path.endswith("/pulls") else [source]
        with patch.object(helper, "comment") as write, patch.object(helper, "run_release") as command:
            report = helper.release_session(self.reader, {**entries[0], "_backfill": True}, ACTOR,
                "supervisor", [("test/a", 1)], self.root, False)
        self.assertEqual(report["handoff_url"], handoff_url)
        self.assertEqual(set(report["claims"][0]["retained_prs"]), {pull["html_url"] for pull in named.values()})
        self.assertFalse(report["issue_refusals"])
        write.assert_not_called()
        command.assert_not_called()
        # The same loaded path must not accept a changed or foreign handoff.
        original_public = dict(public)
        for changed in ({**original_public, "updated_at": "2026-01-01T00:03:00Z"},
                        {**original_public, "user": {"login": "foreign[bot]"}},
                        {**original_public, "issue_url": "https://api.github.com/repos/test/a/issues/2"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                public = changed
                helper.release_session(self.reader, {**entries[0], "_backfill": True}, ACTOR,
                    "supervisor", [("test/a", 1)], self.root, False)

    def test_inventory_conflicting_handoffs_preserve_the_whole_session_for_reconciliation(self):
        rows = [self.inventory_row(handoff_url=self.entry["handoff_url"]),
                self.inventory_row(issue=2, claim_comment_id=2,
                    handoff_url="https://github.com/test/a/issues/2#issuecomment-98")]
        original = json.loads(json.dumps(rows))
        entries, reports = helper.backfill_inventory.load(rows, {"codex": [str(self.root)]})
        self.assertFalse(entries)
        self.assertTrue(reports)
        self.assertEqual(rows, original)

    def test_loaded_inventory_noncanonical_handoff_refuses_instead_of_using_source_context(self):
        entries, reports = helper.backfill_inventory.load(
            [self.inventory_row(handoff_url="https://example.com/not-a-handoff")], {"codex": [str(self.root)]})
        if reports:
            self.assertFalse(entries)
            return
        self.source["issue_url"] = "https://api.github.com/repos/test/a/issues/1"
        self.reader.get_json.return_value = self.source
        self.reader.paged_json.side_effect = lambda path, **_kw: [] if path.endswith("/pulls") else [self.source]
        with self.assertRaises(ValueError):
            helper.release_session(self.reader, {**entries[0], "_backfill": True}, ACTOR,
                "supervisor", [("test/a", 1)], self.root, False)

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
                "--transcript-root", str(self.root)]
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
                "--owner", "test", "--confirm-session-ended",
                "--transcript-root", str(self.root)]
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

    def test_backfill_missing_checkout_refuses_before_any_apply_mutation(self):
        log = self.root / "new-log.json"
        argv = ["helper", "backfill", "--closed-log", str(log), "--supervisor-session", "supervisor",
                "--owner", "test", "--confirm-session-ended", "--apply"]
        with patch.object(helper.sys, "argv", argv), patch.object(helper, "GitHubReader") as reader, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(helper.main(), 1)
        self.assertFalse(log.exists())
        self.assertFalse(Path(str(log) + ".release.lock").exists())
        reader.assert_not_called()

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

    def test_guarded_tty_reuse_requires_a_unique_saved_source(self):
        ended = helper.release.stamp(self.entry["ended_at"])
        shared = self.root / "shared.json"
        other = {**self.entry, "pid": 999, "tty": "other-tty"}
        for rows in ([self.entry], [self.entry, other], [other, self.entry]):
            with self.subTest(rows=rows):
                shared.write_text(json.dumps(rows))
                shared.chmod(0o600)
                before = shared.read_bytes()
                with patch.object(helper.closure, "closed_log", return_value=shared), \
                        patch.object(helper, "inventory", return_value=[(456, self.entry["tty"], "codex --remote unix:///new-client")]), \
                        patch.object(helper.closure, "process_started_at", return_value=ended + timedelta(seconds=22)), \
                        patch.object(helper.closure, "guarded_launch_endpoint", return_value="/new-client"):
                    if len(rows) == 1:
                        helper.verify_process_exit(self.entry, ended_at=ended)
                    else:
                        with self.assertRaises(ValueError):
                            helper.verify_process_exit(self.entry, ended_at=ended)
                self.assertEqual(shared.read_bytes(), before)

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
        for absent in (False, True):
            with self.subTest(absent=absent), \
                    patch.object(helper, "inventory", return_value=[(456, self.entry["tty"], "codex")]), \
                    patch.object(helper.session_liveness, "process_absent", return_value=absent), \
                    patch.object(helper.closure, "process_started_at", side_effect=ValueError("start unavailable")):
                result, report = self.finish_thread([self.source, self.own_release()])
            self.assertEqual(result, 0 if absent else 1, report)

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

    def test_private_closure_uses_its_own_verified_source_instead_of_shared_same_id(self):
        import tmux_terminal
        ended = helper.release.stamp(self.entry["ended_at"])
        entry = {**self.entry, "tmux_socket": "fixture", "tmux_session_id": "tmux:fixture:" + "a" * 32,
                 "window_id": "$1", "tab_id": "@1"}
        pane = {"session_id": entry["tmux_session_id"], "pane_id": "%1", "pid": 789,
                "tmux_socket": "fixture", "window_id": "$1", "tab_id": "@1", "tty": entry["tty"],
                "dead": True, "exited_at": ended.isoformat()}
        log = self.root / "private-closure.json"
        shared = self.root / "shared-closure.json"
        shared.write_text(json.dumps([{**self.entry, "pid": 999, "tty": "other-tty"}]))
        shared.chmod(0o600)
        with patch.object(helper.closure, "closed_log", return_value=shared), \
                patch.object(tmux_terminal.Terminal, "list", return_value=[pane]):
            closed = helper.record_closure(log, entry, "supervisor")
            with patch.object(helper, "inventory", return_value=[(456, entry["tty"], "codex --remote unix:///guarded")]), \
                    patch.object(helper.closure, "guarded_launch_endpoint", return_value="/guarded"), \
                    patch.object(helper.closure, "process_started_at", return_value=ended + timedelta(seconds=22)), \
                    patch.object(tmux_terminal.Terminal, "list", side_effect=AssertionError("pane removed")):
                retried = helper.record_closure(log, entry, "supervisor")
            self.assertEqual(retried["ended_at"], closed["ended_at"])
            self.assertEqual(retried["tty_exited_at"], closed["tty_exited_at"])

    def test_ordinary_offline_finish_and_validation_do_not_need_an_unrelated_shared_store(self):
        shared = self.root / "unrelated-shared.json"
        shared.write_text("fixture unavailable store")
        shared.chmod(0o600)
        with patch.object(helper.closure, "closed_log", return_value=shared):
            closed = helper.record_closure(self.root / "private-closure.json", self.entry, "supervisor")
            self.assertTrue(helper.validate_closure(closed, "supervisor")["safe_verdict"])

    def private_tty_main(self, *, mode="finish", apply=False, prior=True, shared_kind="different", mutation=None, boundary="discovery"):
        import github_plan_native as native
        import tmux_terminal
        import socket
        case = type(self)()
        case.setUp()
        try:
            ended = helper.release.stamp(case.entry["ended_at"])
            entry = {**case.entry, "handoff_url": None if mode == "finish" else case.entry["handoff_url"]}
            if not prior:
                entry.update(tmux_socket="fixture", tmux_session_id="tmux:fixture:" + "a" * 32,
                             window_id="$1", tab_id="@1")
            pane = {"session_id": entry.get("tmux_session_id"), "pane_id": "%1", "pid": 789,
                    "tmux_socket": "fixture", "window_id": "$1", "tab_id": "@1", "tty": entry["tty"],
                    "dead": True, "exited_at": ended.isoformat()}
            ledger, private, shared = (case.root / name for name in ("ledger.json", "private.json", "shared.json"))
            ledger.write_text(json.dumps([entry]))
            if prior:
                private.write_text(json.dumps([entry]))
                private.chmod(0o600)
            shared_row = dict(entry)
            if shared_kind == "different":
                shared_row.update(pid=999, tty="other-tty")
            endpoint = case.root / "proxy.sock"
            guard_socket = socket.socket(socket.AF_UNIX)
            case.addCleanup(guard_socket.close)
            guard_socket.bind(str(endpoint))
            endpoint.chmod(0o600)
            native_argv = ["codex", "--remote", "unix://" + str(endpoint)]
            born = ended + timedelta(seconds=22)
            proxy = {"record_kind": "managed_proxy", "launch_pid": 456,
                     "launch_started_at": born.isoformat(), "codex_home": str(case.root / "home"),
                     "endpoint": str(endpoint), "argv_digest": helper.closure.argv_signature(native_argv)}
            shared.write_text("{" if shared_kind == "malformed" else json.dumps([shared_row, proxy]))
            shared.chmod(0o600)
            shared_before = shared.read_bytes()
            private_before = private.read_bytes() if private.exists() else None
            state = {"changed": False, "discovered": False}
            captured = {}
            real_record, real_process = helper.record_closure, native.recheck_source_process
            def record(*args, **kwargs):
                closed = real_record(*args, **kwargs)
                captured.setdefault("closed", closed)
                captured.setdefault("ended_at", closed["ended_at"])
                captured.setdefault("verdict_at", closed.get("verdict_at"))
                return closed
            def change():
                if state["changed"]:
                    return
                if mutation == "transcript":
                    with case.transcript.open("a") as stream:
                        stream.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                            "payload": {"type": "agent_message", "message": "Continuing work."}}) + "\n")
                    state["changed"] = True
                    return
                with helper.closure.store_lock(private, create=True):
                    if mutation == "remove":
                        private.unlink()
                    else:
                        rows = json.loads(private.read_text()) if private.exists() else []
                        selected = next((row for row in rows if row.get("session_id") == entry["session_id"]), None)
                        if selected is None:
                            selected = {key: value for key, value in captured["closed"].items() if not key.startswith("_")}
                            rows.append(selected)
                        selected["resumed_at"] = datetime.now(timezone.utc).isoformat()
                        private.write_text(json.dumps(rows))
                state["changed"] = True
            def discover(*_args, **_kwargs):
                state["discovered"] = True
                if mutation and boundary == "discovery":
                    change()
                return [("test/a", 1)], ["test/a"]
            def process(*args, **kwargs):
                result = real_process(*args, **kwargs)
                if mutation and boundary == "process" and state["discovered"]:
                    change()
                return result
            def history(*_args, **_kwargs):
                if mutation and boundary == "history":
                    change()
                return [case.source, case.own_release()]
            handoff = case.reader.get_json.return_value
            case.reader.get_json.side_effect = lambda path, **_kw: handoff if "/issues/comments/" in path else {"body": "", "number": 1}
            case.reader.paged_json.side_effect = history
            argv = ["helper", mode, "--closed-log", str(private), "--session-id", entry["session_id"],
                    "--supervisor-session", "supervisor", "--owner", "test", "--confirm-session-ended",
                    "--transcript-root", str(case.root)]
            argv += ["--ledger", str(ledger)] if mode == "finish" else ["--checkout-root", str(case.root)]
            if apply:
                argv.append("--apply")
            output = io.StringIO()
            with patch.object(helper.sys, "argv", argv), \
                    patch.object(helper.closure, "closed_log", return_value=shared), \
                    patch.object(helper, "inventory", return_value=[(456, entry["tty"], " ".join(native_argv))]), \
                    patch.object(helper.closure, "process_started_at", return_value=ended + timedelta(seconds=22)), \
                    patch.object(tmux_terminal.Terminal, "list", return_value=[pane]), \
                    patch.object(helper, "record_closure", side_effect=record), \
                    patch.object(native, "recheck_source_process", side_effect=process), \
                    patch.object(helper.github_identity, "automation_login", return_value=ACTOR), \
                    patch.object(helper, "GitHubReader", return_value=case.reader), \
                    patch.object(helper, "discover", side_effect=discover), \
                    patch.object(helper, "comment") as write, patch.object(helper, "run_release") as run, \
                    contextlib.redirect_stdout(output):
                code = helper.main()
            report = json.loads(output.getvalue())
            write.assert_not_called()
            run.assert_not_called()
            self.assertEqual(shared.read_bytes(), shared_before)
            self.assertEqual(state["changed"], mutation is not None)
            if mutation or shared_kind == "malformed":
                self.assertEqual(code, 1, report)
                self.assertFalse(report["ok"])
            else:
                self.assertEqual(code, 0, report)
                self.assertTrue(report["ok"])
                if mode == "finish":
                    self.assertEqual(report["reports"][0]["ended_at"], captured["ended_at"])
                    if private.exists():
                        saved = json.loads(private.read_text())[0]
                        self.assertEqual(saved["ended_at"], captured["ended_at"])
                        if "verdict_at" in saved:
                            self.assertEqual(saved["verdict_at"], captured["verdict_at"])
                if not apply or mode == "backfill":
                    self.assertEqual(private.read_bytes() if private.exists() else None, private_before)
        finally:
            case.doCleanups()

    def test_private_finish_preserves_its_source_through_guarded_tty_boundaries(self):
        for apply, prior, shared in itertools.product((False, True), (False, True), ("matching", "different")):
            with self.subTest(apply=apply, prior=prior, shared=shared):
                self.private_tty_main(apply=apply, prior=prior, shared_kind=shared)
        for apply, prior, boundary in itertools.product((False, True), (False, True), ("discovery", "history", "process")):
            with self.subTest(apply=apply, prior=prior, boundary=boundary):
                self.private_tty_main(apply=apply, prior=prior, mutation="change", boundary=boundary)
        for apply, boundary in itertools.product((False, True), ("discovery", "history")):
            with self.subTest(apply=apply, boundary=boundary, mutation="remove"):
                self.private_tty_main(apply=apply, mutation="remove", boundary=boundary)
        self.private_tty_main(apply=True, prior=False, mutation="remove", boundary="history")
        for apply, boundary in itertools.product((False, True), ("discovery", "process")):
            with self.subTest(apply=apply, boundary=boundary, mutation="transcript"):
                self.private_tty_main(apply=apply, mutation="transcript", boundary=boundary)

        self.private_tty_main(shared_kind="malformed")

    def test_private_backfill_uses_the_actual_closed_row_for_guarded_tty_checks(self):
        for apply, shared in itertools.product((False, True), ("matching", "different")):
            with self.subTest(apply=apply, shared=shared):
                self.private_tty_main(mode="backfill", apply=apply, shared_kind=shared)
        for apply, boundary in itertools.product((False, True), ("discovery", "process")):
            with self.subTest(apply=apply, boundary=boundary):
                self.private_tty_main(mode="backfill", apply=apply, mutation="change", boundary=boundary)
        for apply in (False, True):
            with self.subTest(apply=apply, mutation="transcript"):
                self.private_tty_main(mode="backfill", apply=apply, mutation="transcript", boundary="process")
        self.private_tty_main(mode="backfill", shared_kind="malformed")

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

    def test_finish_consumes_exact_authored_followup_releases_and_preserves_unresolved_ids(self):
        followup = comment(3, "Claimed by trial\nSession: source-session")
        authored = self.own_release(id=4, body="Released claim 3")
        result, report = self.finish_thread([self.source, followup, self.own_release(), authored])
        self.assertEqual(result, 0)
        self.assertEqual({r["claim_comment"] for r in report["reports"][0]["released_claims"]}, {1, 3})
        for replacement in (None, {**authored, "user": {"login": "foreign[bot]"}},
                            {**authored, "body": "Released claim 3\nSession: other-session"},
                            {**authored, "updated_at": "2026-01-01T00:03:00Z"}):
            with self.subTest(replacement=replacement):
                comments = [self.source, followup, self.own_release()]
                if replacement is not None:
                    comments.append(replacement)
                result, report = self.finish_thread(comments)
                self.assertEqual(result, 1)
                self.assertIn(3, [r.get("claim_comment") for r in report["reports"][0]["issue_refusals"]])

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

    def test_finish_consumes_validated_supervisor_release_after_source_end(self):
        evidence = {**comment(99, "Closed session source-session\nEnded at: 2026-01-01T00:02:00Z\nSafe to exit: yes"),
                    "issue_url": "https://api.github.com/repos/test/control/issues/9",
                    "created_at": "2026-01-01T00:04:00Z", "updated_at": "2026-01-01T00:04:00Z"}
        body = helper.release.prepare_release(self.source, evidence, [self.source], {"worktrees": []},
            actor=ACTOR, role="supervisor", releaser_session="supervisor",
            evidence_url="https://github.com/test/control/issues/9#issuecomment-99", retained_prs=[], related=[])
        receipt = self.own_release(body=body, created_at="2026-01-01T00:05:00Z", updated_at="2026-01-01T00:05:00Z")
        result, report = self.finish_thread([self.source, receipt], evidence=evidence)
        self.assertEqual(result, 0)
        self.assertFalse(report["needs_reconciliation"])
        self.assertEqual(report["reports"][0]["released_claims"][0]["claim_comment"], self.source["id"])

    def test_finish_consumes_supervisor_release_attesting_an_end_after_recorded_closure(self):
        # CS2319-C1: the attestation's Ended at follows the recorded closure by seconds.
        evidence = {**comment(99, "Closed session source-session\nEnded at: 2026-01-01T00:02:05Z\nSafe to exit: yes"),
                    "issue_url": "https://api.github.com/repos/test/control/issues/9",
                    "created_at": "2026-01-01T00:04:00Z", "updated_at": "2026-01-01T00:04:00Z"}
        body = helper.release.prepare_release(self.source, evidence, [self.source], {"worktrees": []},
            actor=ACTOR, role="supervisor", releaser_session="supervisor",
            evidence_url="https://github.com/test/control/issues/9#issuecomment-99", retained_prs=[], related=[])
        receipt = self.own_release(body=body, created_at="2026-01-01T00:05:00Z", updated_at="2026-01-01T00:05:00Z")
        result, report = self.finish_thread([self.source, receipt], evidence=evidence)
        self.assertEqual(result, 0, report)
        self.assertEqual(report["reports"][0]["released_claims"][0]["claim_comment"], self.source["id"])
        # A naked release posted after the end is still not the worker's own.
        late = self.own_release(created_at="2026-01-01T00:05:00Z", updated_at="2026-01-01T00:05:00Z")
        result, report = self.finish_thread([self.source, late], evidence=evidence)
        self.assertEqual(result, 1)

    def test_finish_invalid_supervisor_proofs_preserve_the_exact_source_without_writes(self):
        evidence = {**comment(99, "Closed session source-session\nEnded at: 2026-01-01T00:02:00Z\nSafe to exit: yes"),
                    "issue_url": "https://api.github.com/repos/test/control/issues/9",
                    "created_at": "2026-01-01T00:04:00Z", "updated_at": "2026-01-01T00:04:00Z"}
        body = helper.release.prepare_release(self.source, evidence, [self.source], {"worktrees": []},
            actor=ACTOR, role="supervisor", releaser_session="supervisor",
            evidence_url="https://github.com/test/control/issues/9#issuecomment-99", retained_prs=[], related=[])
        receipt = self.own_release(body=body, created_at="2026-01-01T00:05:00Z", updated_at="2026-01-01T00:05:00Z")
        cases = [
            (receipt, {**evidence, "updated_at": "2026-01-01T00:06:00Z"}),
            (receipt, {**evidence, "issue_url": "https://api.github.com/repos/test/control/issues/8"}),
            (receipt, {**evidence, "id": 98}),
            (receipt, {**evidence, "user": {"login": "foreign[bot]"}}),
            (receipt, {**evidence, "body": evidence["body"].replace("source-session", "other-session")}),
            ({**receipt, "body": helper.release.pending_body(body)}, evidence),
            ({**receipt, "user": {"login": "foreign[bot]"}}, evidence),
        ]
        for candidate, canonical in cases:
            with self.subTest(candidate=candidate, canonical=canonical):
                result, report = self.finish_thread([self.source, candidate], evidence=canonical)
                self.assertEqual(result, 1)
                self.assertFalse(report["reports"][0]["released_claims"])
                self.assertIn(self.source["id"], [r.get("claim_comment") for r in report["reports"][0]["issue_refusals"]])

    def test_finish_malformed_or_failed_canonical_history_never_becomes_empty(self):
        entry = {**self.entry, "_finish": True, "_finish_scope": "explicit scope"}
        failures = (["malformed comment"], ValueError("invalid canonical shape"),
                    helper.GitHubReadError("unavailable", result=SimpleNamespace(status=503), diagnostics={}))
        self.reader.get_json.return_value = {"body": ""}
        for failure in failures:
            with self.subTest(failure=failure), patch.object(helper, "comment") as write, patch.object(helper, "run_release") as run:
                self.reader.paged_json.side_effect = failure if isinstance(failure, Exception) else None
                self.reader.paged_json.return_value = failure
                report = helper.release_session(self.reader, entry, ACTOR, "supervisor", [("test/a", 1)], self.root, True)
                self.assertTrue(report["issue_refusals"])
                self.assertFalse(report["claims"])
                self.assertFalse(report["released_claims"])
                write.assert_not_called()
                run.assert_not_called()

    def test_finish_history_failure_reports_per_issue_release_and_refusal_without_writes(self):
        def read(path, **_kw):
            if path.endswith("/issues/2"):
                raise helper.GitHubReadError("temporarily unavailable", result=SimpleNamespace(status=503), diagnostics={})
            return {"body": ""}
        self.reader.get_json.side_effect = read
        self.reader.paged_json.return_value = [self.source, self.own_release()]
        entry = {**self.entry, "_finish": True, "_finish_scope": "explicit scope"}
        with patch.object(helper, "preview_guards", return_value={"status": "passed_read_only"}), \
                patch.object(helper, "comment") as write, patch.object(helper, "run_release") as run:
            report = helper.release_session(self.reader, entry, ACTOR, "supervisor", [("test/a", 2), ("test/a", 1)], self.root, False)
        self.assertEqual([r["issue"] for r in report["issue_refusals"]], [2])
        self.assertFalse(report["claims"])
        self.assertEqual([r["claim_comment"] for r in report["released_claims"]], [self.source["id"]])
        write.assert_not_called()
        run.assert_not_called()

    def test_finish_held_source_reports_exact_id_without_planning_a_release(self):
        self.reader.get_json.return_value = {"body": ""}
        self.reader.paged_json.return_value = [self.source]
        entry = {**self.entry, "_finish": True, "_finish_scope": "explicit scope"}
        with patch.object(helper, "comment") as write, patch.object(helper, "run_release") as run:
            report = helper.release_session(self.reader, entry, ACTOR, "supervisor", [("test/a", 1)], self.root, False)
        self.assertFalse(report["claims"])
        self.assertEqual(report["issue_refusals"][0]["claim_comment"], self.source["id"])
        write.assert_not_called()
        run.assert_not_called()

    def test_finish_preserves_unbound_alias_when_other_ownership_diagnostic_is_deferred(self):
        diagnostics = [comment(7, "Claimed by orphan-worker\nSession: source-session"),
                       comment(7, helper.claim.marker({**SOURCE, "worker": "foreign-worker"}), author="foreign[bot]")]
        for field in ("", "Session ID: source-session", "Native session ID: source-session", "| Session | source-session |"):
            alias = comment(9, "Claimed by trial\n" + field)
            baseline, baseline_report = self.finish_thread([self.source, alias])
            self.assertEqual(baseline, 1)
            self.assertEqual({r["claim_comment"] for r in baseline_report["reports"][0]["issue_refusals"]}, {1, 9})
            for diagnostic in diagnostics:
                for first in (False, True):
                    with self.subTest(field=field, diagnostic=diagnostic, first=first):
                        comments = [diagnostic, self.source, alias] if first else [self.source, alias, diagnostic]
                        result, report = self.finish_thread(comments)
                        self.assertEqual(result, 1)
                        refused = report["reports"][0]["issue_refusals"]
                        self.assertEqual({r["claim_comment"] for r in refused}, {1, 7, 9})
                        self.assertEqual(len(refused), 3)

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

    def test_interrupted_handoff_uses_exact_inventory_ownership_context(self):
        entry = {**self.entry, "handoff_url": None, "_backfill": True, "_inventory": True,
                 "_inventory_claims": [self.inventory_row()]}
        self.source["issue_url"] = "https://api.github.com/repos/test/a/issues/1"
        self.reader.paged_json.return_value = [self.source]
        self.reader.get_json.return_value = dict(self.source)
        found, _ = helper.inventory_source_context(self.reader, entry, ACTOR)
        self.assertEqual(found["handoff_url"], "https://github.com/test/a/issues/1#issuecomment-1")
        self.assertIn("ownership record", found["_handoff_provenance"])
        self.source["user"]["login"] = "other-identity"
        with self.assertRaises(ValueError):
            helper.inventory_source_context(self.reader, entry, ACTOR)

    def test_inventory_source_context_requires_exact_backfill_source_and_unchanged_readback(self):
        source = {**self.source, "issue_url": "https://api.github.com/repos/test/a/issues/1"}
        entry = {**self.entry, "handoff_url": None, "_backfill": True, "_inventory": True,
                 "_inventory_claims": [self.inventory_row()]}
        self.reader.paged_json.return_value = [source]
        self.reader.get_json.return_value = source
        for field in ("_backfill", "_inventory"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                helper.inventory_source_context(self.reader, {**entry, field: False}, ACTOR)
        cases = [[], [{**source, "id": 2}], [{**source, "user": {"login": "foreign[bot]"}}],
                 [{**source, "body": source["body"].replace("source-session", "other-session")}],
                 [{**source, "updated_at": "2026-01-01T00:03:00Z"}]]
        for comments in cases:
            with self.subTest(comments=comments), self.assertRaises(ValueError):
                self.reader.paged_json.return_value = comments
                helper.inventory_source_context(self.reader, entry, ACTOR)
        self.reader.paged_json.return_value = [source]
        for observed in ({**source, "issue_url": "https://api.github.com/repos/test/a/issues/2"},
                         {**source, "body": source["body"] + " changed"}):
            with self.subTest(observed=observed), self.assertRaises(ValueError):
                self.reader.get_json.return_value = observed
                helper.inventory_source_context(self.reader, entry, ACTOR)

    def test_inventory_repo_scope_preserves_excluded_rows(self):
        log = self.root / "inventory.json"
        log.write_text(json.dumps([self.inventory_row(), self.inventory_row(repo="test/b", claim_comment_id=2)]))
        argv = ["helper", "backfill", "--closed-log", str(log), "--supervisor-session", "supervisor",
                "--owner", "test", "--repo", "test/a", "--checkout-root", str(self.root),
                "--confirm-session-ended", "--transcript-root", str(self.root)]
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

    def test_closure_selects_its_source_and_preserves_unrelated_rows(self):
        log = self.root / "selected-close.json"
        unrelated = {"session_id": "unrelated-session", "harness": "invalid"}
        for previous in ([], [self.entry]):
            with self.subTest(existing=bool(previous)):
                log.write_text(json.dumps([*previous, unrelated]))
                closed = helper.record_closure(log, self.entry, "supervisor")
                self.assertEqual([row for row in json.loads(log.read_text())
                                  if row.get("session_id") != self.entry["session_id"]], [unrelated])
                self.assertEqual(helper.record_closure(log, self.entry, "supervisor")["ended_at"], closed["ended_at"])
        for rows in ({}, [None], [{}], [{"session_id": ""}],
                     [self.entry, self.entry], [{**self.entry, "harness": "invalid"}],
                     [{k: v for k, v in self.entry.items() if k != "transcript"}]):
            with self.subTest(invalid=rows):
                log.write_text(json.dumps(rows))
                before = log.read_bytes()
                with self.assertRaises((ValueError, TypeError)):
                    helper.record_closure(log, self.entry, "supervisor")
                self.assertEqual(log.read_bytes(), before)

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
        with helper.closure.ownership(SOURCE["session"]):
            helper.closure.set_action_pending(SOURCE["session"], False)
        with self.assertRaises(ValueError):
            helper.record_closure(log, self.entry, "supervisor")
        replacement = {**self.entry, **metadata, "pid": metadata["launch_pid"]}
        for key, value in (("launch_pid", 790), ("launch_generation", "another-generation"),
                           ("launch_started_at", "2026-01-01T00:01:41Z"), ("pid", 790),
                           ("tty", "other-tty"), ("codex_home", str(self.root / "other-account"))):
            with self.subTest(changed=key), self.assertRaises(ValueError):
                helper.record_closure(log, {**replacement, key: value}, "supervisor")
        closed = helper.record_closure(log, replacement, "supervisor")
        self.assertEqual(closed["launch_generation"], metadata["launch_generation"])

    def test_finish_uses_shared_default_and_preview_creates_nothing(self):
        ledger, log = self.root / "ledger.json", self.root / "shared/closed.json"
        ledger.write_text(json.dumps([self.entry]))
        for apply in (False, True):
            argv = ["helper", "finish", "--ledger", str(ledger), "--session-id", SOURCE["session"],
                    "--supervisor-session", "supervisor", "--owner", "test", "--checkout-root", str(self.root),
                    "--confirm-session-ended", "--transcript-root", str(self.root)]
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
        self.enterContext(patch.object(github_plan_sessions, "configured_homes", return_value=[self.root.resolve()]))
        self.enterContext(patch.object(helper.closure, "host_identity", return_value={"host": "fixture", "boot": "boot-a", "uid": 1}))
        log = self.root / "closed.json"
        self.enterContext(patch.object(helper.closure, "closed_log", return_value=log))
        helper.record_closure(log, self.entry, "supervisor")
        return log

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
                "--confirm-session-ended", "--transcript-root", str(self.root)]
        output = io.StringIO()
        with patch.object(helper.sys, "argv", argv), patch.object(helper.github_identity, "automation_login", return_value=ACTOR), \
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

    def test_related_alias_without_exact_session_is_not_released(self):
        for body in ("Claimed by trial", "Claimed by trial\nSession: another-session"):
            self.reader.paged_json.return_value = [self.source, comment(2, body)]
            self.assertEqual(helper.active_claims(self.reader, "test/a", 1, SOURCE["session"], ACTOR)[0][1], [])

    def test_earlier_exact_native_hand_claim_uses_issue_and_pr_backfill_release_gate(self):
        source = {**self.source, "created_at": "2026-01-01T00:00:20Z", "updated_at": "2026-01-01T00:00:20Z"}
        hand = {**comment(3, "Claimed by trial\nSession: source-session"),
                "created_at": "2026-01-01T00:00:10Z", "updated_at": "2026-01-01T00:00:10Z"}
        comments = [hand, source]
        self.reader.paged_json.side_effect = lambda path, **_kw: [] if path.endswith("/pulls") else comments
        found = helper.active_claims(self.reader, "test/a", 1, SOURCE["session"], ACTOR)
        self.assertEqual([(c["id"], related, unresolved) for c, related, unresolved in found], [(1, [3], [])])
        rows = [self.inventory_row(handoff_url=self.entry["handoff_url"]),
                self.inventory_row(claim_comment_id=3, handoff_url=self.entry["handoff_url"])]
        entries, errors = helper.backfill_inventory.load(rows, {"codex": [str(self.root)]})
        self.assertFalse(errors)
        handoff = dict(self.reader.get_json.return_value)
        for pr in (False, True):
            with self.subTest(pr=pr):
                issue = {"number": 1, "body": "", **({"pull_request": {}} if pr else {})}
                self.reader.get_json.side_effect = lambda path, **_kw: handoff if "/issues/comments/" in path else issue
                with patch.object(helper, "comment") as write, patch.object(helper, "run_release") as command:
                    report = helper.release_session(self.reader, {**entries[0], "_backfill": True}, ACTOR,
                        "supervisor", [("test/a", 1)], self.root, False)
                self.assertFalse(report["issue_refusals"])
                if pr:
                    argv = report["pr_claim_handoffs"][0]["release_argv"]
                    at = argv.index("--related-claim-comment")
                    self.assertEqual(argv[at + 1], str(hand["id"]))
                else:
                    self.assertEqual(report["claims"][0]["related_claim_comments"], [hand["id"]])
                write.assert_not_called()
                command.assert_not_called()

    def test_earlier_hand_claim_grouping_keeps_author_worker_native_and_unstructured_checks(self):
        cases = [comment(3, "Claimed by trial"), comment(3, "Claimed by trial\nSession: other-session"),
                 comment(3, "Claimed by other-worker\nSession: source-session"),
                 comment(3, "Claimed by trial\nSession: source-session", author="foreign[bot]"),
                 comment(3, helper.claim.marker(SOURCE))]
        for hand in cases:
            with self.subTest(hand=hand):
                self.reader.paged_json.return_value = [hand, self.source]
                try:
                    found = helper.active_claims(self.reader, "test/a", 1, SOURCE["session"], ACTOR)
                except ValueError:
                    continue
                self.assertNotIn(hand["id"], [identifier for _source, related, _unresolved in found for identifier in related])

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
        body = helper.release.prepare_release(self.source, evidence, [self.source], {"worktrees": []},
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
                "--confirm-session-ended", "--transcript-root", str(self.root), "--apply"]
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
