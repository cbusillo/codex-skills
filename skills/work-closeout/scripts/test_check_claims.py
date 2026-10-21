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
from types import SimpleNamespace

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
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(Path, "home", return_value=self.folder))
        self.source = comment(10, "Claimed by worker\n\n" + helper.claim.marker(SOURCE))
        self.release = comment(11, "Released claim 10\n\nSession: " + SESSION, at="2026-01-01T00:02:00Z")
        self.comments = [self.source]
        self.issue = {"body": "", "number": 1}
        self.pulls = []
        self.reader = Mock()
        self.reader.requests = []
        self.reader.expected_actor = ACTOR
        self.reader.deadline_at = None
        self.reader.get_json.side_effect = self.get
        self.reader.paged_json.side_effect = self.paged
        self.inventory = self.enterContext(patch.object(helper.github_identity, "automation_repositories",
            return_value={"repositories": {REPO: [ACTOR]}, "installations": []}))
        self.enterContext(patch.object(helper, "repository_reader", return_value=self.reader))

    def get(self, path, **kwargs):
        if "/pulls/" in path:
            return next(p for p in self.pulls if p["number"] == int(path.rsplit("/", 1)[1]))
        return self.issue

    def paged(self, path, **kwargs):
        return self.pulls if path.endswith("/pulls") else self.comments

    def check(self):
        return helper.check_record(self.reader, REPO, 1, SESSION, ACTOR)

    def entry(self, session=SESSION, timestamp=SOURCE["claimed_at"]):
        path = self.folder / (session + ".jsonl")
        row = {"type": "session_meta", "payload": {"id": session}}
        if timestamp is not None:
            row["timestamp"] = timestamp
        path.write_text(json.dumps(row) + "\n")
        return {"session_id": session, "transcript": str(path)}

    def test_held_claim_blocks_without_an_exact_release(self):
        with self.assertRaises(ValueError):
            self.check()

    def test_exact_authored_release_is_read_back(self):
        self.comments.append(self.release)
        result = self.check()
        self.assertEqual([r["claim_comment"] for r in result], [self.source["id"]])
        self.assertEqual(result[0]["retained_prs"], [])

    def test_the_claim_helpers_bare_exact_recovery_release_is_accepted(self):
        self.comments.append({**self.release, "body": "Released claim 10"})
        self.assertEqual(self.check()[0]["claim_comment"], self.source["id"])

    def recovered_release(self):
        evidence = comment(88, f"Closed session {SESSION}\nEnded at: 2026-01-01T00:02:00Z\nSafe to exit: yes", at="2026-01-01T00:03:00Z")
        evidence["issue_url"] = f"https://api.github.com/repos/{REPO}/issues/1"
        body = helper.release.prepare_release(self.source, evidence, self.comments,
            {"worktrees": []}, actor=ACTOR, role="supervisor", releaser_session="supervisor-fixture",
            evidence_url=f"https://github.com/{REPO}/issues/1#issuecomment-88", retained_prs=[p["html_url"] for p in self.pulls],
            checked_prs=[p["html_url"] for p in self.pulls], related=[])
        self.comments.append(comment(89, body, at="2026-01-01T00:04:00Z"))
        get = self.get
        self.reader.get_json.side_effect = lambda path, **kw: evidence if path.endswith("/issues/comments/88") else {"commit": {"committer": {"date": "2026-01-01T00:01:30Z"}}} if "/commits/" in path else get(path, **kw)
        return evidence

    def test_verified_supervisor_recovery_can_be_read_back_by_the_source(self):
        evidence = self.recovered_release()
        self.assertEqual(self.check()[0]["claim_comment"], self.source["id"])
        evidence["updated_at"] = "2026-01-01T00:05:00Z"
        with self.assertRaises(ValueError):
            self.check()

    def test_recovered_retained_pr_is_verified_and_can_get_a_fresh_authored_handoff(self):
        self.pulls = [{**pull(), "head": {**pull()["head"], "sha": "fixture-sha"}}]
        self.recovered_release()
        self.assertEqual(self.check()[0]["retained_prs"], [self.pulls[0]["html_url"]])
        self.comments.append(comment(90, "Released claim 10\n" + self.pulls[0]["html_url"], at="2026-01-01T00:05:00Z"))
        self.assertTrue(self.check()[0]["release_url"].endswith("issuecomment-90"))

    def test_omitted_recovery_pr_needs_a_later_exact_authored_handoff(self):
        self.recovered_release()
        self.pulls = [pull(3, branch="work/computed-split", created_at="2026-01-01T00:01:30Z")]
        with self.assertRaises(ValueError):
            self.check()
        handoff = comment(90, f"Handoff from {SOURCE['worker']}\nclaim: {self.source['id']}\nSession: {SESSION}\n{self.pulls[0]['html_url']}",
                          at="2026-01-01T00:05:00Z")
        self.comments.append(handoff)
        self.assertEqual(self.check()[0]["retained_prs"], [self.pulls[0]["html_url"]])

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
        for mention in ("Blocked on #99", "Superseded by PR #99"):
            self.comments = [self.source, {**self.release, "body": self.release["body"] + "\n" + mention}]
            self.pulls = [pull(99, branch="unrelated", body="Unrelated", user={"login": "other[bot]"})]
            self.assertEqual(self.check()[0]["retained_prs"], [])

    def test_native_pr_creation_exposes_an_unnamed_split_branch(self):
        self.comments.append(self.release)
        self.pulls = [pull(3, branch="work/split")]
        with self.assertRaises(ValueError):
            helper.check_record(self.reader, REPO, 1, SESSION, ACTOR)

    def test_canonical_split_branch_pr_requires_handoff_without_a_creation_hint(self):
        self.comments.append(self.release)
        self.pulls = [pull(3, branch="work/computed-split", created_at="2026-01-01T00:01:30Z")]
        with self.assertRaises(ValueError):
            self.check()
        self.comments[-1]["body"] += "\nRetained PR: " + self.pulls[0]["html_url"]
        self.assertEqual(self.check()[0]["retained_prs"], [self.pulls[0]["html_url"]])

    def test_canonical_pr_attribution_uses_server_claim_lifetime(self):
        self.comments.append(self.release)
        # A later repeated release cannot expand the original ownership window.
        self.comments.append(comment(12, self.release["body"], at="2026-01-01T00:05:00Z"))
        for created, actor, retained in (("2026-01-01T00:00:30Z", ACTOR, False),
                                          ("2026-01-01T00:03:00Z", ACTOR, False),
                                          ("2026-01-01T00:01:30Z", "human", False),
                                          ("2026-01-01T00:01:30Z", ACTOR, True),
                                          (None, ACTOR, True)):
            with self.subTest(created=created, actor=actor):
                self.pulls = [pull(3, branch="work/independent", created_at=created, user={"login": actor})]
                if retained:
                    with self.assertRaises(ValueError):
                        self.check()
                else:
                    self.assertEqual(self.check()[0]["retained_prs"], [])

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

    def receipt_entry(self, session: str = SESSION, records: tuple = ((REPO, 1),)) -> dict:
        entry = self.entry(session)
        with Path(entry["transcript"]).open("a") as stream:
            for repo, number in records:
                receipt = {"operation": "github.plan.claim", "claim": {**SOURCE, "session": session},
                           "issue": {"repo": repo, "number": number}, "ok": True}
                stream.write(json.dumps({"type": "response_item", "payload": {
                    "type": "function_call_output", "output": json.dumps({"output": json.dumps(receipt)})}}) + "\n")
        return entry

    def test_recorded_claims_never_inventory_other_repositories(self) -> None:
        self.comments.append(self.release)
        self.inventory.side_effect = AssertionError("fleet inventory is forbidden")
        report = helper.check([self.receipt_entry()])
        self.assertTrue(report["claims_clear"], report)
        self.assertEqual([r["claim_comment"] for r in report["released_claims"]], [self.source["id"]])
        paths = [call.args[0] for method in (self.reader.get_json, self.reader.paged_json) for call in method.call_args_list]
        self.assertTrue(paths)
        self.assertTrue(all(path.startswith(f"/repos/{REPO}/issues/1") or path == f"/repos/{REPO}/pulls" for path in paths))
        self.assertEqual(report["coverage"]["repositories"], [REPO])

    def test_recorded_claim_targets_include_closed_and_pr_only_work_across_sessions(self) -> None:
        records = ((REPO, 1), ("second/b", 5), ("second/b", 6))
        entries = [self.receipt_entry(records=records), self.receipt_entry("child", records=((REPO, 8),))]
        with patch.object(helper, "check_record", return_value=[]) as check:
            report = helper.check(entries)
        self.assertTrue(report["claims_clear"], report)
        self.assertEqual({(c.args[1], c.args[2], c.args[3]) for c in check.call_args_list},
                         {(repo, number, SESSION) for repo, number in records} | {(REPO, 8, "child")})

    def test_journal_keeps_a_child_claim_when_the_root_transcript_has_no_receipt(self) -> None:
        helper.claim_log.record(SESSION, REPO, 1)
        report = helper.check([self.entry()])
        self.assertFalse(report["claims_clear"], report)
        self.comments.append(self.release)
        self.assertTrue(helper.check([self.entry()])["claims_clear"])

    def test_repeated_session_transcripts_union_their_targets(self) -> None:
        first = self.receipt_entry(records=((REPO, 1),))
        saved = self.folder / "earlier.jsonl"
        Path(first["transcript"]).rename(saved)
        first["transcript"] = str(saved)
        second = self.receipt_entry(records=((REPO, 2),))
        with patch.object(helper, "check_record", return_value=[]) as check:
            self.assertTrue(helper.check([first, second])["claims_clear"])
        self.assertEqual({call.args[2] for call in check.call_args_list}, {1, 2})

    def test_malformed_journal_blocks_before_network_reads(self) -> None:
        entry = self.receipt_entry()
        helper.claim_log.record(SESSION, REPO, 1)
        path = next(helper.claim_log.log_path(SESSION).glob("*.json"))
        path.write_text('{"truncated":')
        with patch.object(helper, "repository_reader") as reader, self.assertRaises(ValueError):
            helper.check([entry])
        reader.assert_not_called()

    def test_unrelated_truncated_transcript_line_does_not_hide_a_journaled_claim(self) -> None:
        entry = self.entry()
        with Path(entry["transcript"]).open("a") as stream:
            stream.write('{"unrelated":')
        helper.claim_log.record(SESSION, REPO, 1)
        self.assertFalse(helper.check([entry])["claims_clear"])

    def test_legacy_codex_descendant_claims_are_found_without_a_root_receipt(self) -> None:
        entry = self.entry()
        native = self.folder / "codex/sessions/2026/01/01"
        native.mkdir(parents=True)
        root = native / "root.jsonl"
        Path(entry["transcript"]).rename(root)
        entry["transcript"] = str(root)
        child = self.receipt_entry("child", records=(("second/b", 5),))
        rows = Path(child["transcript"]).read_text().splitlines()
        header = json.loads(rows[0])
        header["payload"]["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": SESSION}}}
        rows[0] = json.dumps(header)
        (native / "child.jsonl").write_text("\n".join(rows) + "\n")
        with patch.object(helper, "check_record", return_value=[]) as check:
            self.assertTrue(helper.check([entry])["claims_clear"])
        self.assertIn(("second/b", 5, "child"), {(c.args[1], c.args[2], c.args[3]) for c in check.call_args_list})

    def test_legacy_claude_subagent_claim_is_checked_under_the_root(self) -> None:
        entry = self.entry()
        Path(entry["transcript"]).write_text(json.dumps({"sessionId": SESSION, "type": "user"}) + "\n")
        subagents = Path(entry["transcript"]).with_suffix("") / "subagents"
        subagents.mkdir(parents=True)
        receipt = {"operation": "github.plan.claim", "claim": SOURCE, "issue": {"repo": REPO, "number": 1}}
        (subagents / "agent-fixture.jsonl").write_text(json.dumps({"sessionId": SESSION, "type": "user", "message": {"content": [
            {"type": "tool_result", "content": json.dumps(receipt)}]}}) + "\n")
        report = helper.check([entry])
        self.assertFalse(report["claims_clear"], report)

    def test_malformed_nested_claim_data_returns_a_json_failure(self) -> None:
        entry = self.entry()
        with Path(entry["transcript"]).open("a") as stream:
            stream.write(json.dumps({"claim": SOURCE, "issue": "unreadable"}) + "\n")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = helper.main(["--session", SESSION, "--transcript", entry["transcript"]])
        self.assertNotEqual(code, 0)
        self.assertFalse(json.loads(output.getvalue())["claims_clear"])

    def test_reader_gets_the_checks_deadline_before_its_first_read(self) -> None:
        self.comments.append(self.release)
        started = helper.time.time()
        with patch.object(helper, "repository_reader", return_value=self.reader) as factory:
            self.assertTrue(helper.check([self.receipt_entry()], read_timeout=123)["claims_clear"])
        deadline = factory.call_args.args[1]
        self.assertGreaterEqual(deadline, started + 123)
        self.assertLessEqual(deadline, helper.time.time() + 123)

    def test_product_agent_ids_are_not_native_children(self) -> None:
        entry = self.entry()
        with Path(entry["transcript"]).open("a") as stream:
            stream.write(json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output":
                json.dumps({"agent_id": "product-agent"})}}) + "\n")
        self.assertTrue(helper.check([entry])["claims_clear"])

    def test_missing_advertised_child_requires_its_native_transcript(self) -> None:
        entry = self.entry()
        with Path(entry["transcript"]).open("a") as stream:
            stream.write(json.dumps({"type": "response_item", "payload": {"type": "function_call", "name": "collaboration.spawn_agent", "call_id": "spawn"}}) + "\n")
            stream.write(json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "call_id": "spawn", "output":
                json.dumps({"agent_id": "missing-child"})}}) + "\n")
        with patch.object(helper, "repository_reader") as reader, self.assertRaises(ValueError):
            helper.check([entry])
        reader.assert_not_called()

    def test_duplicate_receipts_check_a_record_once(self) -> None:
        entry = self.receipt_entry(records=((REPO, 1), (REPO, 1)))
        with patch.object(helper, "check_record", return_value=[]) as check:
            self.assertTrue(helper.check([entry])["claims_clear"])
        self.assertEqual(check.call_count, 1)

    def test_recorded_claim_uses_the_target_repositories_writer(self) -> None:
        client_actor = "client-fixture[bot]"
        self.reader.expected_actor = client_actor
        self.source["user"]["login"] = client_actor
        self.comments.append({**self.release, "user": {"login": client_actor}})
        report = helper.check([self.receipt_entry()])
        self.assertTrue(report["claims_clear"], report)

    def test_recorded_held_claim_and_failed_reads_cannot_clear(self) -> None:
        entry = self.receipt_entry()
        report = helper.check([entry])
        self.assertFalse(report["claims_clear"])
        self.reader.get_json.side_effect = helper.GitHubReadError("unavailable", result=SimpleNamespace(status=403), diagnostics={})
        report = helper.check([entry])
        self.assertFalse(report["claims_clear"])
        self.assertFalse(report["coverage"]["complete"])

    def test_cli_empty_session_needs_no_github_reads(self) -> None:
        entry = self.entry()
        with patch.object(helper, "repository_reader") as reader, contextlib.redirect_stdout(io.StringIO()) as output:
            code = helper.main(["--owner", "test", "--session", SESSION, "--transcript", entry["transcript"]])
        self.assertEqual(code, 0, output.getvalue())
        reader.assert_not_called()
        self.inventory.assert_not_called()

    def test_cli_refuses_a_transcript_of_another_session_before_reading_github(self):
        other = self.entry("newer-session", "2026-03-01T00:00:00Z")
        for transcript in (other["transcript"], str(self.folder / "missing.jsonl")):
            with self.subTest(transcript=transcript), patch.object(helper, "GitHubReader") as reader, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                code = helper.main(["--session", SESSION, "--transcript", transcript])
            self.assertNotEqual(code, 0)
            self.assertFalse(json.loads(output.getvalue())["claims_clear"])
            reader.assert_not_called()

    def test_cli_refuses_an_unpaired_transcript_before_reading_github(self):
        entry = self.entry()
        with patch.object(helper, "GitHubReader") as reader, contextlib.redirect_stdout(io.StringIO()):
            code = helper.main(["--session", SESSION, "--session", "child", "--transcript", entry["transcript"]])
        self.assertNotEqual(code, 0)
        reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
