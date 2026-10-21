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
        self.source = comment(10, "Claimed by worker\n\n" + helper.claim.marker(SOURCE))
        self.release = comment(11, "Released claim 10\n\nSession: " + SESSION, at="2026-01-01T00:02:00Z")
        self.comments = [self.source]
        self.issue = {"body": "", "number": 1}
        self.pulls = []
        self.reader = Mock()
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
        if path == "/installation/repositories":
            return [{"full_name": REPO}]
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

    def canonical_pages(self, path, **kw):
        if path == "/installation/repositories":
            return [{"full_name": REPO}]
        if path.endswith("/issues/comments"):
            return [{**c, "issue_url": f"https://api.github.com/repos/{REPO}/issues/1"} for c in self.comments]
        if path.endswith("/issues") or path.endswith("/pulls"):
            return []
        return self.comments

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

    def test_partial_installation_coverage_is_pending_until_rechecked(self):
        entries = [self.entry(SESSION, "2026-02-01T00:00:00Z"), self.entry("child", "2026-01-01T00:00:00Z")]
        installation = {"installation_id": 101, "account": {"login": "suspended-owner"},
                        "reason": "suspended installation may contain native source claims"}
        self.inventory.return_value = {"repositories": {REPO: [ACTOR]}, "installations": [installation],
                                       "pending_installations": [installation]}
        with patch.object(helper.sessions, "discover", return_value=([], [REPO])):
            pending = helper.check(self.reader, [], entries, ACTOR, repositories=["explicit/site"])
            self.assertFalse(pending["claims_clear"], pending)
            self.assertFalse(pending["coverage"]["complete"])
            self.assertTrue(any("101" in reason for reason in pending["blocked"]))
            query = self.inventory.call_args.kwargs
            self.assertEqual(query["since"], helper.release.stamp("2025-12-31T23:59:59Z"))
            self.assertEqual(query["required_repositories"], {"explicit/site"})
            self.inventory.return_value = {"repositories": {REPO: [ACTOR]}, "installations": [installation],
                                           "excluded_installations": [installation]}
            clear = helper.check(self.reader, [], entries, ACTOR, repositories=["explicit/site"])
            self.assertTrue(clear["claims_clear"], clear)
            self.assertTrue(clear["coverage"]["complete"])
            self.assertEqual(clear["coverage"]["excluded_installations"], [installation])
            helper.check(self.reader, [], [self.entry("untimed", None), self.entry(SESSION, "2026-02-01T00:00:00Z")], ACTOR)
            self.assertIsNone(self.inventory.call_args.kwargs["since"])

    def test_global_inventory_accounts_for_cross_repo_closed_issues_and_prs(self):
        records = [("test/a", 1), ("test/b", 5), ("test/b", 6)]
        self.inventory.return_value["repositories"]["test/b"] = [ACTOR]
        self.reader.paged_json.side_effect = lambda *_a, **_k: [{"full_name": "test/a"}, {"full_name": "test/b"}]
        with patch.object(helper.sessions, "discover", return_value=(records, ["test/a", "test/b"])), \
                patch.object(helper, "check_record", side_effect=[[], ValueError("held"), []]) as check:
            result = helper.check(self.reader, "test", [self.entry()], ACTOR)
        self.assertFalse(result["claims_clear"])
        self.assertEqual(check.call_count, len(records))
        self.assertTrue(any("held" in reason for reason in result["blocked"]), result)

    def test_complete_discovery_reads_all_owner_repositories_and_closed_records(self):
        def paged(path, **kwargs):
            if path == "/installation/repositories":
                return [{"full_name": "test/a"}, {"full_name": "test/b"}]
            if path.endswith("/issues"):
                return [{"number": 1, "state": "closed", "body": helper.claim.marker(SOURCE)}] if kwargs["params"].get("state") == "all" else []
            if path.endswith("/issues/comments"):
                return []
            return self.comments
        self.reader.paged_json.side_effect = paged
        result = helper.check(self.reader, "test", [self.entry()], ACTOR, repositories=["test/b"])
        self.assertFalse(result["claims_clear"])
        self.assertEqual(len(result["blocked"]), len(result["coverage"]["repositories"]))

    def test_incomplete_app_inventory_refuses(self):
        self.inventory.side_effect = helper.github_identity.GitHubAppError("inventory incomplete")
        with self.assertRaises(helper.github_identity.GitHubAppError):
            helper.check(self.reader, [], [self.entry()], ACTOR)

    def test_default_inventory_uses_its_resolved_client_actor(self):
        client_actor = "client-fixture[bot]"
        self.inventory.return_value["repositories"][REPO] = [client_actor]
        self.reader.expected_actor = client_actor
        self.source["user"]["login"] = client_actor
        self.comments.append({**self.release, "user": {"login": client_actor}})
        self.reader.paged_json.side_effect = self.canonical_pages
        report = helper.check(self.reader, "test", [self.entry()], ACTOR)
        self.assertTrue(report["claims_clear"])
        self.assertEqual([r["claim_comment"] for r in report["released_claims"]], [self.source["id"]])

    def test_discovery_finds_a_held_default_installation_claim(self):
        self.reader.paged_json.side_effect = self.canonical_pages
        report = helper.check(self.reader, [], [self.entry()], ACTOR)
        self.assertFalse(report["claims_clear"])
        self.comments.append(self.release)
        self.assertTrue(helper.check(self.reader, [], [self.entry()], ACTOR)["claims_clear"])

    def test_a_second_installation_and_its_client_actor_are_checked(self):
        foreign, client_actor = "second/b", "client-fixture[bot]"
        self.inventory.return_value = {"repositories": {REPO: [ACTOR], foreign: [client_actor]}, "installations": []}
        def rows(identifier, actor):
            source = comment(identifier, helper.claim.marker(SOURCE), actor)
            return [source, comment(identifier + 1, f"Released claim {identifier}", actor, at="2026-01-01T00:02:00Z")]
        local_comments, client_comments = rows(10, ACTOR), rows(20, client_actor)
        client = Mock(expected_actor=client_actor)
        def missing():
            return helper.GitHubReadError("fixture record unavailable", result=SimpleNamespace(status=404), diagnostics={})
        def client_get(path, **kw):
            if path != f"/repos/{foreign}/issues/2": raise missing()
            return {"body": "", "number": 2}
        def local_get(path, **kw):
            if path != f"/repos/{REPO}/issues/1": raise missing()
            return {"body": "", "number": 1}
        client.get_json.side_effect = client_get
        self.reader.get_json.side_effect = local_get
        def pages(repo, comments, endpoint, **kw):
            if endpoint == "/installation/repositories":
                if repo == foreign: raise AssertionError("foreign installation enumeration must not use the default token")
                return [{"full_name": REPO}]
            if endpoint.endswith("/issues/comments"):
                return [{**c, "issue_url": f"https://api.github.com/repos/{repo}/issues/{2 if repo == foreign else 1}"} for c in comments]
            if endpoint.endswith("/issues") or endpoint.endswith("/pulls"): return []
            return comments
        self.reader.paged_json.side_effect = lambda path, **kw: pages(REPO, local_comments, path, **kw)
        client.paged_json.side_effect = lambda path, **kw: pages(foreign, client_comments, path, **kw)
        entries = [self.entry()]
        select = lambda repo, _: client if repo == foreign else self.reader
        report = helper.check(self.reader, [], entries, ACTOR, reader_for_repo=select)
        self.assertTrue(report["claims_clear"], report)
        self.assertTrue(report["coverage"]["complete"], report)
        self.assertEqual({r["repository"] for r in report["released_claims"]}, {REPO, foreign})
        client_comments.pop()
        held = helper.check(self.reader, [], entries, ACTOR, reader_for_repo=select)
        self.assertFalse(held["claims_clear"], held)
        client_comments.append(comment(21, "Released claim 20", ACTOR, at="2026-01-01T00:02:00Z"))
        wrong_release = helper.check(self.reader, [], entries, ACTOR, reader_for_repo=select)
        self.assertFalse(wrong_release["claims_clear"], wrong_release)
        client_comments[-1]["user"]["login"] = client_actor
        self.assertTrue(helper.check(self.reader, [], entries, ACTOR, reader_for_repo=select)["claims_clear"])
        client.paged_json.side_effect = missing()
        incomplete = helper.check(self.reader, [], entries, ACTOR, reader_for_repo=select)
        self.assertFalse(incomplete["claims_clear"], incomplete)
        self.assertFalse(incomplete["coverage"]["complete"], incomplete)
        self.assertTrue(any(foreign in reason for reason in incomplete["blocked"]))
        client.paged_json.side_effect = lambda path, **kw: pages(foreign, client_comments, path, **kw)
        self.assertTrue(helper.check(self.reader, [], entries, ACTOR, reader_for_repo=select)["claims_clear"])

    def test_child_session_claims_are_read_back_with_the_parent(self):
        child = "native-child"
        self.source["body"] = helper.claim.marker({**SOURCE, "session": child})
        self.reader.paged_json.side_effect = self.canonical_pages
        entries = [self.entry(), self.entry(child)]
        self.assertFalse(helper.check(self.reader, [], entries, ACTOR)["claims_clear"])
        self.comments.append({**self.release, "body": "Released claim 10\nSession: " + child})
        report = helper.check(self.reader, [], entries, ACTOR)
        self.assertTrue(report["claims_clear"], report)
        self.assertEqual(report["session_ids"], sorted([SESSION, child]))
        self.assertEqual([r["source_session"] for r in report["released_claims"]], [child])

    def test_explicit_repository_adds_an_account_missing_from_the_arguments(self):
        with patch.object(helper.sessions, "discover", return_value=([], ["second/b"])) as discover:
            result = helper.check(self.reader, "test", [self.entry()], ACTOR, repositories=["second/b"],
                                  reader_for_repo=lambda *_: self.reader)
        self.assertEqual({call.args[1] for call in discover.call_args_list}, {"test", "second"})
        self.assertIn("second/b", result["coverage"]["repositories"])
        self.assertIn("second", result["coverage"]["owners"])

    def test_cli_applies_its_read_deadline_before_reading_github(self):
        entry = self.entry()
        def first_read(path, **kwargs):
            self.assertIsNotNone(self.reader.deadline_at)
            return self.paged(path, **kwargs)
        self.reader.paged_json.side_effect = first_read
        output = io.StringIO()
        with patch.object(helper.github_identity, "automation_login", return_value=ACTOR), \
                patch.object(helper, "GitHubReader", autospec=True, return_value=self.reader), \
                patch.object(helper.sessions, "discover", return_value=([], [REPO])), contextlib.redirect_stdout(output):
            code = helper.main(["--owner", "test", "--session", SESSION, "--transcript", entry["transcript"]])
        self.assertEqual(code, 0, output.getvalue())
        report = json.loads(output.getvalue())
        self.assertTrue(report["claims_clear"])
        self.assertEqual(report["session_ids"], [SESSION])

    def test_cli_has_no_all_clear_on_partial_inventory_or_auth_failure(self):
        entry = self.entry()
        for error in (ValueError("partial inventory"), OSError("permission denied")):
            output = io.StringIO()
            with patch.object(helper.github_identity, "automation_login", return_value=ACTOR), \
                    patch.object(helper, "GitHubReader", return_value=self.reader), \
                    patch.object(helper.sessions, "discover", side_effect=error), contextlib.redirect_stdout(output):
                code = helper.main(["--owner", "test", "--session", SESSION, "--transcript", entry["transcript"]])
            result = json.loads(output.getvalue())
            self.assertNotEqual(code, 0)
            self.assertFalse(result["claims_clear"])
            self.assertFalse(result["coverage"]["complete"])

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
