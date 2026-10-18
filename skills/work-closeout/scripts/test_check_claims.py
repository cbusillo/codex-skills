#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise native-session closeout against disposable GitHub and transcript records."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import check_claims as helper

ACTOR = "fixture[bot]"
SESSION = "native-session"
REPO = "test/a"
SOURCE = {"worker": "worker", "session": SESSION, "branch": "work/task", "claimed_at": "2026-01-01T00:00:00Z"}


def comment(identifier, body, actor=ACTOR, at="2026-01-01T00:01:00Z"):
    return {"id": identifier, "body": body, "user": {"login": actor}, "created_at": at, "updated_at": at}


def pull(number=2, branch=SOURCE["branch"], **changes):
    return {"number": number, "html_url": f"https://github.com/{REPO}/pull/{number}",
            "body": "Fixes #1", "state": "open", "user": {"login": ACTOR},
            "head": {"ref": branch, "repo": {"full_name": REPO}},
            "base": {"repo": {"full_name": REPO}}, **changes}


class ClaimCloseoutTests(unittest.TestCase):
    def setUp(self):
        self.source = comment(10, "Claimed by worker\n\n" + helper.claim.marker(SOURCE))
        self.release = comment(11, "Released claim 10\n\nSession: " + SESSION, at="2026-01-01T00:02:00Z")
        self.comments = [self.source]
        self.issue = {"body": "", "number": 1}
        self.pulls = []
        self.reader = Mock()
        self.reader.get_json.side_effect = self.get
        self.reader.paged_json.side_effect = self.paged

    def get(self, path, **kwargs):
        if "/pulls/" in path:
            return next(p for p in self.pulls if p["number"] == int(path.rsplit("/", 1)[1]))
        return self.issue

    def paged(self, path, **kwargs):
        return self.pulls if path.endswith("/pulls") else self.comments

    def check(self):
        return helper.check_record(self.reader, REPO, 1, SESSION, ACTOR)

    def test_held_claim_blocks_even_when_hygiene_and_transcript_say_safe(self):
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                records = ([{"type": "assistant", "sessionId": SESSION, "message": {"stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "Finished.\nSafe to exit: yes"}]}}] if harness == "claude" else [
                            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Finished.\nSafe to exit: yes"}},
                            {"type": "event_msg", "payload": {"type": "task_complete"}}])
                self.assertTrue(helper.session_record.summarize(records, harness)["safe_verdict"])
                with self.assertRaises(ValueError):
                    self.check()

    def test_exact_authored_release_is_read_back(self):
        self.comments.append(self.release)
        result = self.check()
        self.assertEqual([r["claim_comment"] for r in result], [self.source["id"]])
        self.assertEqual(result[0]["retained_prs"], [])

    def test_prose_handoff_does_not_release(self):
        self.comments.append(comment(11, "Handing off to Supervisor. Safe to exit: yes"))
        with self.assertRaises(ValueError):
            self.check()

    def test_another_identity_cannot_release(self):
        self.comments.append({**self.release, "user": {"login": "other[bot]"}})
        with self.assertRaises(ValueError):
            self.check()

    def test_source_edit_invalidates_old_release(self):
        self.comments.extend([self.release])
        self.source["updated_at"] = "2026-01-01T00:03:00Z"
        with self.assertRaises(ValueError):
            self.check()

    def test_unverified_retained_pr_blocks_released_claim(self):
        self.comments.append(self.release)
        self.pulls = [pull()]
        with self.assertRaises(ValueError):
            self.check()

    def test_retained_pr_handoff_allows_exit_before_merge(self):
        self.comments.append({**self.release, "body": self.release["body"] + "\nRetained PR: " + pull()["html_url"]})
        self.pulls = [pull()]
        self.assertEqual(self.check()[0]["retained_prs"], [self.pulls[0]["html_url"]])

    def test_named_pr_must_independently_link_issue_and_match_identity(self):
        self.comments.append({**self.release, "body": self.release["body"] + "\n" + pull()["html_url"]})
        for bad in (pull(body="Unrelated"), pull(user={"login": "other[bot]"}),
                    pull(base={"repo": {"full_name": "test/other"}})):
            self.pulls = [bad]
            with self.subTest(pull=bad), self.assertRaises(ValueError):
                self.check()

    def test_every_retained_pr_needs_handoff(self):
        self.comments.append({**self.release, "body": self.release["body"] + "\n" + pull()["html_url"]})
        self.pulls = [pull(), pull(3)]
        with self.assertRaises(ValueError):
            self.check()

    def test_named_split_branch_pr_is_verified_too(self):
        self.pulls = [pull(), pull(3, branch="work/split", body="Unrelated")]
        self.comments.append({**self.release, "body": self.release["body"] + "\n" +
                              "\n".join(p["html_url"] for p in self.pulls)})
        with self.assertRaises(ValueError):
            self.check()
        self.pulls[1]["body"] = "Fixes #1"
        self.assertEqual(len(self.check()[0]["retained_prs"]), len(self.pulls))

    def test_split_handoffs_can_be_in_separate_authored_comments(self):
        self.pulls = [pull(), pull(3, branch="work/split")]
        self.comments.append({**self.release, "body": self.release["body"] + "\n" + self.pulls[0]["html_url"]})
        self.comments.append(comment(12, f"Handoff from {SOURCE['worker']}\nclaim: {self.source['id']}\nSession: {SESSION}\n{self.pulls[1]['html_url']}", at="2026-01-01T00:03:00Z"))
        self.assertEqual(set(self.check()[0]["retained_prs"]), {p["html_url"] for p in self.pulls})

    def test_bare_dependency_issue_is_not_a_retained_pr(self):
        self.comments.append({**self.release, "body": self.release["body"] + "\nBlocked on #99"})
        self.pulls = [pull(99, branch="unrelated", body="Unrelated", user={"login": "other[bot]"})]
        self.assertEqual(self.check()[0]["retained_prs"], [])

    def test_native_pr_creation_exposes_an_unnamed_split_branch(self):
        self.comments.append(self.release)
        self.pulls = [pull(3, branch="work/split")]
        with self.assertRaises(ValueError):
            helper.check_record(self.reader, REPO, 1, SESSION, ACTOR, created_prs={self.pulls[0]["html_url"]})

    def test_later_session_bound_handoff_is_supported(self):
        self.comments.append(self.release)
        self.comments.append(comment(12, f"Handoff from {SOURCE['worker']}\nclaim: {self.source['id']}\nSession: {SESSION}\n{pull()['html_url']}", at="2026-01-01T00:03:00Z"))
        self.pulls = [pull()]
        self.assertTrue(self.check())

    def test_pr_only_claim_does_not_need_a_self_link(self):
        self.issue["pull_request"] = {}
        self.pulls = [pull(1, body="Dependency bump", user={"login": "dependabot[bot]"})]
        self.comments.append({**self.release, "body": self.release["body"] + "\n" + self.pulls[0]["html_url"]})
        self.assertEqual(self.check()[0]["retained_prs"], [self.pulls[0]["html_url"]])

    def test_pr_only_refresh_can_use_a_different_task_branch(self):
        self.issue["pull_request"] = {}
        self.pulls = [pull(1, branch="dependabot/package", body="Dependency bump", user={"login": "dependabot[bot]"})]
        self.comments.append({**self.release, "body": self.release["body"] + "\n" + self.pulls[0]["html_url"]})
        self.assertTrue(self.check())

    def test_pr_only_claim_rejects_a_fork_work_record(self):
        self.issue["pull_request"] = {}
        self.pulls = [pull(1, head={"ref": SOURCE["branch"], "repo": {"full_name": "other/fork"}})]
        self.comments.append({**self.release, "body": self.release["body"] + "\n" + self.pulls[0]["html_url"]})
        with self.assertRaises(ValueError):
            self.check()

    def test_closed_pr_claim_needs_release_but_no_open_pr_handoff(self):
        self.issue["pull_request"] = {}
        self.pulls = [pull(1, state="closed")]
        with self.assertRaises(ValueError):
            self.check()
        self.comments.append(self.release)
        self.assertTrue(self.check())

    def test_status_only_ownership_is_not_nothing_to_release(self):
        self.comments = []
        self.issue["body"] = helper.claim.marker(SOURCE)
        with self.assertRaises(ValueError):
            self.check()

    def test_other_session_claims_remain_untouched(self):
        self.comments = [comment(20, helper.claim.marker({**SOURCE, "session": "other-session"}))]
        self.assertEqual(self.check(), [])
        self.reader.post_json.assert_not_called()

    def test_global_inventory_accounts_for_cross_repo_closed_issues_and_prs(self):
        entries = [("test/a", 1), ("test/b", 5), ("test/b", 6)]
        with patch.object(helper.sessions, "discover", return_value=(entries, ["test/a", "test/b"])), \
                patch.object(helper, "check_record", side_effect=[[], ValueError("held"), []]) as check:
            result = helper.check(self.reader, "test", {"session_id": SESSION, "harness": "codex"}, ACTOR)
        self.assertFalse(result["claims_clear"])
        self.assertEqual(check.call_count, len(entries))
        self.assertEqual(result["blocked"], ["held"])

    def test_complete_discovery_reads_all_owner_repositories_and_closed_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "native.jsonl"
            transcript.write_text(json.dumps({"type": "session_meta", "timestamp": SOURCE["claimed_at"], "payload": {"id": SESSION}}) + "\n")
            def paged(path, **kwargs):
                if path == "/installation/repositories":
                    return [{"full_name": "test/a"}, {"full_name": "test/b"}]
                if path.endswith("/issues"):
                    self.assertEqual(kwargs["params"]["state"], "all")
                    return []
                if path.endswith("/issues/comments"):
                    repo = "/".join(path.split("/")[2:4])
                    return [{**self.source, "issue_url": f"https://api.github.com/repos/{repo}/issues/1"}]
                return self.comments
            self.reader.paged_json.side_effect = paged
            result = helper.check(self.reader, "test", {"session_id": SESSION, "harness": "codex", "transcript": str(transcript)}, ACTOR)
        self.assertFalse(result["claims_clear"])
        self.assertEqual(len(result["blocked"]), len(result["coverage"]["repositories"]))

    def test_cli_can_finish_on_both_native_transcript_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            for harness in ("codex", "claude"):
                transcript = Path(tmp) / "native.jsonl"
                identity = {"type": "session_meta", "payload": {"id": SESSION}} if harness == "codex" else {"type": "user", "sessionId": SESSION}
                transcript.write_text(json.dumps({**identity, "timestamp": SOURCE["claimed_at"]}) + "\n")
                output = io.StringIO()
                with patch.object(helper.github_agent, "running_agent", return_value=harness), \
                        patch.object(helper.github_agent, "native_sessions", return_value={"native": SESSION}), \
                        patch.object(helper.github_identity, "automation_login", return_value=ACTOR), \
                        patch.object(helper, "GitHubReader", autospec=True, return_value=self.reader), \
                        patch.object(helper.sessions, "discover", return_value=([], [REPO])), contextlib.redirect_stdout(output):
                    code = helper.main(["--owner", "test", "--harness", harness, "--session", SESSION, "--transcript", str(transcript)])
                self.assertEqual(code, 0, output.getvalue())
                self.assertTrue(json.loads(output.getvalue())["claims_clear"])

    def test_cli_has_no_all_clear_on_partial_inventory_or_auth_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "native.jsonl"
            transcript.write_text(json.dumps({"type": "session_meta", "timestamp": "2026-01-01T00:00:00Z", "payload": {"id": SESSION}}) + "\n")
            for error in (ValueError("partial inventory"), OSError("permission denied")):
                output = io.StringIO()
                with patch.object(helper.github_agent, "native_sessions", return_value={"CODEX_THREAD_ID": SESSION}), \
                        patch.object(helper.github_identity, "automation_login", return_value=ACTOR), \
                        patch.object(helper, "GitHubReader", return_value=self.reader), \
                        patch.object(helper.sessions, "discover", side_effect=error), contextlib.redirect_stdout(output):
                    code = helper.main(["--owner", "test", "--harness", "codex", "--session", SESSION, "--transcript", str(transcript)])
                result = json.loads(output.getvalue())
                self.assertNotEqual(code, 0)
                self.assertFalse(result["claims_clear"])
                self.assertFalse(result["coverage"]["complete"])

    def test_cli_rejects_a_different_native_session_before_reading_github(self):
        with patch.object(helper.github_agent, "native_sessions", return_value={"CODEX_THREAD_ID": "other"}), \
                patch.object(helper, "GitHubReader") as reader, contextlib.redirect_stdout(io.StringIO()):
            code = helper.main(["--owner", "test", "--harness", "codex", "--session", SESSION, "--transcript", "/unused"])
        self.assertNotEqual(code, 0)
        reader.assert_not_called()

    def test_cli_refuses_missing_identity_and_wrong_harness_without_github(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "old-native.jsonl"
            transcript.write_text(json.dumps({"type": "session_meta", "timestamp": SOURCE["claimed_at"], "payload": {"id": SESSION}}) + "\n")
            for family, native in (("codex", {}), ("claude", {"native": SESSION})):
                with patch.object(helper.github_agent, "running_agent", return_value=family), \
                        patch.object(helper.github_agent, "native_sessions", return_value=native), \
                        patch.object(helper, "GitHubReader", return_value=self.reader) as reader, \
                        patch.object(helper.sessions, "discover", return_value=([], [REPO])), contextlib.redirect_stdout(io.StringIO()):
                    code = helper.main(["--owner", "test", "--harness", "codex", "--session", SESSION, "--transcript", str(transcript)])
                self.assertNotEqual(code, 0)
                reader.assert_not_called()

    def test_live_transcript_snapshot_ignores_only_an_unfinished_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "native.jsonl"
            identity = {"type": "session_meta", "payload": {"id": SESSION}}
            transcript.write_text(json.dumps(identity) + '\n{"type":')
            self.assertEqual(helper.transcript_snapshot(transcript, "codex", SESSION), [identity])
            transcript.write_text(json.dumps(identity) + '\n{"type":\n')
            with self.assertRaises(ValueError):
                helper.transcript_snapshot(transcript, "codex", SESSION)

    def test_native_receipts_add_an_account_missing_from_the_arguments(self):
        from pathlib import Path
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "supervisor/scripts"))
        from test_claim_receipts import claim as native_claim, history, SESSION as native_session
        entry = {"harness": "codex", "session_id": native_session,
                 "_transcript_records": history("codex", native_claim("test/a"), native_claim("second/b", 2, 20))}
        with patch.object(helper.sessions, "discover", side_effect=[([], ["second/b"]), ([], ["test/a"])]) as discover:
            result = helper.check(self.reader, "test", entry, ACTOR)
        self.assertEqual({call.args[1] for call in discover.call_args_list}, {"test", "second"})
        self.assertFalse(result["claims_clear"])  # Missing source readback cannot be an empty all-clear.


if __name__ == "__main__":
    unittest.main()
