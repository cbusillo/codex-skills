#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Disposable transcripts and GitHub adapters exercise session claim release."""

import json
import contextlib
import io
import fcntl
from types import SimpleNamespace
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
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.transcript = self.root / "source-session.jsonl"
        self.transcript.write_text("\n".join(json.dumps(r) for r in [
            {"type": "session_meta", "timestamp": "2026-01-01T00:00:00Z",
             "payload": {"id": SOURCE["session"]}},
            {"timestamp": "2026-01-01T00:01:00Z", "payload": {
                "type": "agent_message", "message": "Finished.\nSafe to exit: yes"}},
            {"timestamp": "2026-01-01T00:01:01Z", "payload": {"type": "task_complete"}},
        ]) + "\n")
        self.entry = {"session_id": SOURCE["session"], "harness": "codex",
                      "transcript": str(self.transcript), "pid": 123, "tty": "ttys123",
                      "supervisor_owned": True, "process_exit_verified": True,
                      "ended_at": "2026-01-01T00:02:00Z",
                      "handoff_url": "https://github.com/test/a/issues/1#issuecomment-99"}
        self.source = comment(1, "Claimed by trial\n\n" + helper.claim.marker(SOURCE))
        self.reader = Mock()
        self.reader.get_json.return_value = {**comment(99, "Handoff"),
            "issue_url": "https://api.github.com/repos/test/a/issues/1"}
        self.inventory = patch.object(helper, "inventory", return_value=[])
        self.inventory.start()
        self.addCleanup(self.inventory.stop)

    def test_finished_process_and_safe_verdict(self):
        self.assertTrue(helper.validate_closure(self.entry, "supervisor")["safe_verdict"])
        for rows in ([(123, "other", "zsh")], [(456, "ttys123", "codex")], [(456, "ttys123", "zsh -c work")]):
            with self.subTest(rows=rows), patch.object(helper, "inventory", return_value=rows):
                with self.assertRaisesRegex(ValueError, "remains"):
                    helper.validate_closure(self.entry, "supervisor")

    def test_uncertain_identity_and_unverified_closure_refuse(self):
        for changes in ({"supervisor_owned": False}, {"pid": True}, {"tty": None},
                        {"process_exit_verified": False}, {"ended_at": "2026-01-01T00:02:00"}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                helper.validate_closure({**self.entry, **changes}, "supervisor")
        with self.assertRaises(ValueError):
            helper.validate_closure(self.entry, SOURCE["session"])

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
        with self.assertRaisesRegex(ValueError, "identity changed"):
            helper.record_closure(log, {**self.entry, "pid": 789}, "supervisor")

    def test_cross_repository_discovery_includes_closed_issue_comments(self):
        def paged(path, **kwargs):
            if path == "/installation/repositories":
                return [{"full_name": "test/a"}, {"full_name": "test/b"}, {"full_name": "other/c"}]
            self.assertIn("since", kwargs["required_query_params"])
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

    def test_unrelated_malformed_marker_does_not_block_discovery(self):
        self.reader.paged_json.return_value = [{"body": "<!-- github-plan:claim invalid -->"}]
        self.assertEqual(helper.discover(self.reader, "test", [self.entry], ["test/a"])[0], [])
        self.reader.paged_json.return_value = [{"body": "source-session\n<!-- github-plan:claim invalid -->", "html_url": "trial-comment"}]
        with self.assertRaisesRegex(ValueError, "trial-comment"):
            helper.discover(self.reader, "test", [self.entry], ["test/a"])

    def test_backfill_reused_tty_is_not_source_identity_but_pid_still_refuses(self):
        old = {**self.entry, "_backfill": True}
        with patch.object(helper, "inventory", return_value=[(456, "ttys123", "codex")]):
            self.assertTrue(helper.validate_closure(old, "supervisor")["safe_verdict"])
            with self.assertRaises(ValueError):
                helper.validate_closure(self.entry, "supervisor")
        with patch.object(helper, "inventory", return_value=[(123, "other", "codex")]):
            with self.assertRaises(ValueError):
                helper.validate_closure(old, "supervisor")

    def test_backfill_reports_bad_record_and_continues_verified_session(self):
        entries = [{**self.entry, "session_id": "bad-session", "process_exit_verified": False}, self.entry]
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

    def test_dry_run_finds_retained_pr_without_writes(self):
        self.reader.paged_json.side_effect = self.plan_reader
        with patch.object(helper, "comment") as write, patch.object(helper, "run_release") as run:
            report = helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1)], self.root, False)
        write.assert_not_called()
        run.assert_not_called()
        self.assertEqual(report["claims"][0]["retained_prs"], ["https://github.com/test/a/pull/5"])
        self.assertTrue(report["claims"][0]["attestation_body"].startswith("Closed session source-session\nEnded at: 2026-01-01T00:02:00Z\nSafe to exit: yes\n"))

    def test_exact_handoff_pr_url_does_not_select_shorter_pr_number(self):
        self.reader.get_json.return_value["body"] = "Retained PR: https://github.com/test/a/pull/12"
        named = {"html_url": "https://github.com/test/a/pull/12", "number": 12,
                 "body": "Refs #1", "head": {"ref": "other-branch"}, "user": {"login": ACTOR}, "state": "open"}
        handoff = dict(self.reader.get_json.return_value)
        self.reader.get_json.side_effect = lambda path, **_kw: named if path.endswith("/pulls/12") else handoff
        def paged(path, **_kwargs):
            return [{"html_url": "https://github.com/test/a/pull/1", "head": {"ref": "other-branch"}}] if path.endswith("/pulls") else [self.source]
        self.reader.paged_json.side_effect = paged
        with patch.object(helper.claim, "pr_issue_reference", return_value=True):
            report = helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1)], self.root, False)
        self.assertEqual(report["claims"][0]["retained_prs"], [named["html_url"]])

    def test_resumed_harness_with_new_pid_and_tty_refuses_backfill(self):
        with patch.object(helper, "inventory", return_value=[(456, "another-tty", "codex resume source-session")]):
            with self.assertRaisesRegex(ValueError, "resumed harness"):
                helper.validate_closure({**self.entry, "_backfill": True}, "supervisor")

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

    def test_apply_posts_attestation_previews_then_releases(self):
        self.reader.paged_json.side_effect = self.plan_reader
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
        self.reader.paged_json.side_effect = self.plan_reader
        with patch.object(helper.claim, "run_read", return_value="https://github.com/test/a.git"), \
                patch.object(helper, "comment", return_value={"comment": {"url": self.entry["handoff_url"]}}), \
                patch.object(helper, "run_release", side_effect=ValueError("still live")) as run:
            with self.assertRaises(helper.PartialRelease) as caught:
                helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1)], self.root, True)
        self.assertEqual(caught.exception.report["claims"][0]["evidence_url"], self.entry["handoff_url"])
        self.assertEqual(run.call_count, 1)

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
            with self.assertRaisesRegex(ValueError, "checkout"):
                helper.release_session(self.reader, self.entry, ACTOR, "supervisor", [("test/a", 1)], self.root, True)
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
