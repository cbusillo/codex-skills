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
    def test_shorthand_redirected_client_claim_is_inventoried_with_its_own_actor(self):
        from test_claim_receipts import history, claim as native_claim, output, SESSION as native_session
        foreign, number, client_actor = "client/repo", 7, "client-fixture[bot]"
        local = [comment(10, helper.claim.marker({**SOURCE, "session": native_session})),
                 comment(11, "Released claim 10", at="2026-01-01T00:02:00Z")]
        held = comment(20, helper.claim.marker({**SOURCE, "session": native_session}), client_actor)
        client_comments = [held]
        client = Mock(expected_actor=client_actor)
        client.get_json.return_value = {"body": "", "number": number}
        client.paged_json.side_effect = lambda path, **kw: [] if path.endswith("/pulls") else client_comments
        self.reader.paged_json.side_effect = lambda path, **kw: [{"full_name": REPO}] if path == "/installation/repositories" else [] if path.endswith("/pulls") else local
        for harness, suffix in ((h, s) for h in ("codex", "claude") for s in ("", " && python3 - <<'PY'\nprint('ok')\nPY")):
            rows = history(harness, native_claim(REPO), native_claim(foreign, number, 20))
            command = f'uv run /catalog/gh-plan.py claim {foreign}#{number} > claim.json' + suffix
            if harness == "codex":
                rows[3]["payload"]["arguments"] = json.dumps({"cmd": command})
            else:
                rows[3]["message"]["content"][0]["input"] = {"command": command}
            rows[4] = output({}, harness, "tool-1")
            for row in rows: row["timestamp"] = SOURCE["claimed_at"]
            entry = {"session_id": native_session, "harness": harness, "_transcript_records": rows}
            with patch.object(helper.sessions, "discover", side_effect=lambda reader, owner, entries, repos: ({(REPO, 1)} if repos == [REPO] else set(), repos)):
                report = helper.check(self.reader, [], entry, ACTOR, reader_for_repo=lambda repo, _: client if repo == foreign else self.reader)
                self.assertFalse(report["claims_clear"], report)
                self.assertIn(foreign, report["coverage"]["repositories"])
                client_comments.append(comment(21, "Released claim 20", client_actor, at="2026-01-01T00:02:00Z"))
                released = helper.check(self.reader, [], entry, ACTOR, reader_for_repo=lambda repo, _: client if repo == foreign else self.reader)
                self.assertTrue(released["claims_clear"], released)
                self.assertTrue(released["coverage"]["complete"])
                self.assertEqual({r["repository"] for r in released["released_claims"]}, {REPO, foreign})
                client_comments.pop()

    def setUp(self):
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

    def test_a_confirmed_no_write_refusal_does_not_make_inventory_incomplete(self):
        from test_claim_receipts import history, SESSION as native_session
        for harness in ("codex", "claude"):
            refused = {"operation": helper.claim_receipts.CLAIM_OPERATION, "ok": False,
                       "write_outcome": "not_started", "completed_steps": []}
            rows = history(harness, refused)
            command = "cd /repo && uv run scripts/gh-plan.py claim 1 2>&1"
            if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
            else: rows[1]["message"]["content"][0]["input"] = {"command": command}
            for row in rows: row["timestamp"] = SOURCE["claimed_at"]
            entry = {"session_id": native_session, "harness": harness, "_transcript_records": rows}
            with patch.object(helper.sessions, "discover", return_value=([], [REPO])):
                report = helper.check(self.reader, [], entry, ACTOR)
            self.assertTrue(report["claims_clear"], report)
            self.assertTrue(report["coverage"]["complete"])

    def test_pending_ownership_is_refused_before_remote_inventory(self):
        from test_claim_receipts import history, SESSION as native_session
        for harness in ("codex", "claude"):
            for command in ("uv run scripts/gh-plan.py --repo test/a claim 1 > claim.json",
                            "uv run scripts/gh-plan.py --repo test/a claim 1 &",
                            "uv run scripts/gh-plan.py claim test/a#1 > claim.json 2>&1 &",
                            "nohup gh pr create --title x &"):
                rows = history(harness, {"operation": helper.claim_receipts.CLAIM_OPERATION})
                if harness == "codex":
                    rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    rows[2]["payload"]["output"] = "Process running with session ID 123" if command.endswith("json") else ""
                else:
                    rows[1]["message"]["content"][0]["input"] = {"command": command}
                    rows[2]["message"]["content"][0]["content"] = "Process running with session ID 123" if command.endswith("json") else ""
                entry = {"session_id": native_session, "harness": harness, "_transcript_records": rows}
                with self.assertRaises(ValueError):
                    helper.check(self.reader, [], entry, ACTOR)
        self.reader.get_json.assert_not_called()
        self.reader.paged_json.assert_not_called()

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

    def test_the_claim_helpers_bare_exact_recovery_release_is_accepted(self):
        self.comments.append({**self.release, "body": "Released claim 10"})
        self.assertEqual(self.check()[0]["claim_comment"], self.source["id"])

    def recovered_release(self):
        evidence = comment(88, f"Closed session {SESSION}\nEnded at: 2026-01-01T00:02:00Z\nSafe to exit: yes", at="2026-01-01T00:03:00Z")
        evidence["issue_url"] = f"https://api.github.com/repos/{REPO}/issues/1"
        body = helper.release.prepare_release(self.source, evidence, self.comments,
            {"sessions": [], "worktrees": []}, actor=ACTOR, role="supervisor", releaser_session="supervisor-fixture",
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
        self.reader.paged_json.side_effect = lambda *_a, **_k: [{"full_name": "test/a"}, {"full_name": "test/b"}]
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
                    return [{"number": 1, "state": "closed", "body": helper.claim.marker(SOURCE)}] if kwargs["params"].get("state") == "all" else []
                if path.endswith("/issues/comments"):
                    return []  # Closed status discovery is the only route.
                return self.comments
            self.reader.paged_json.side_effect = paged
            result = helper.check(self.reader, "test", {"session_id": SESSION, "harness": "codex", "transcript": str(transcript)}, ACTOR)
        self.assertFalse(result["claims_clear"])
        self.assertEqual(len(result["blocked"]), len(result["coverage"]["repositories"]))

    def test_default_installation_prevents_vacuous_no_receipt_all_clear(self):
        self.reader.paged_json.side_effect = lambda *_a, **_k: []
        with self.assertRaises(ValueError):
            helper.check(self.reader, [], {"session_id": SESSION, "harness": "codex"}, ACTOR)

    def test_default_inventory_uses_its_resolved_client_actor(self):
        client_actor = "client-fixture[bot]"
        self.reader.expected_actor = client_actor
        self.source["user"]["login"] = client_actor
        self.comments.append({**self.release, "user": {"login": client_actor}})
        def pages(path, **kw):
            if path == "/installation/repositories": return [{"full_name": REPO}]
            if path.endswith("/issues/comments"):
                return [{**c, "issue_url": f"https://api.github.com/repos/{REPO}/issues/1"} for c in self.comments]
            if path.endswith("/issues") or path.endswith("/pulls"): return []
            return self.comments
        self.reader.paged_json.side_effect = pages
        entry = {"harness": "codex", "session_id": SESSION, "_transcript_records": [
            {"type": "session_meta", "timestamp": SOURCE["claimed_at"], "payload": {"id": SESSION}}]}
        report = helper.check(self.reader, "test", entry, ACTOR)
        self.assertTrue(report["claims_clear"])
        self.assertEqual([r["claim_comment"] for r in report["released_claims"]], [self.source["id"]])

    def test_missing_receipts_still_discover_a_held_default_installation_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "native.jsonl"
            path.write_text(json.dumps({"type": "session_meta", "timestamp": SOURCE["claimed_at"], "payload": {"id": SESSION}}) + "\n")
            def pages(endpoint, **kw):
                if endpoint == "/installation/repositories": return [{"full_name": REPO}]
                if endpoint.endswith("/issues/comments"):
                    return [{**self.source, "issue_url": f"https://api.github.com/repos/{REPO}/issues/1"}]
                return [] if endpoint.endswith("/issues") else self.comments
            self.reader.paged_json.side_effect = pages
            report = helper.check(self.reader, [], {"harness": "codex", "session_id": SESSION, "transcript": str(path)}, ACTOR)
            self.assertFalse(report["claims_clear"])

    def test_uncertain_renewal_reads_its_original_record_outside_the_time_window(self):
        from test_claim_receipts import history
        value = {"operation": helper.claim_receipts.CLAIM_OPERATION, "ok": False, "write_outcome": "unknown",
                 "claim": {"session": SESSION}, "issue": {"repo": REPO, "number": 1}}
        records = history("codex", value)
        for record in records: record["timestamp"] = "2026-01-02T00:00:00Z"
        self.comments.append(self.release)
        entry = {"session_id": SESSION, "harness": "codex", "_transcript_records": records}
        original_pages = self.paged
        self.reader.paged_json.side_effect = lambda path, **kw: [] if path.endswith("/issues/comments") or path.endswith("/issues") else original_pages(path, **kw)
        report = helper.check(self.reader, [], entry, ACTOR)
        self.assertTrue(report["claims_clear"], report)
        self.assertTrue(report["receipt_reconciliation"])
        self.assertEqual([r["claim_comment"] for r in report["released_claims"]], [self.source["id"]])

    def test_a_second_installation_and_its_client_actor_are_checked(self):
        from test_claim_receipts import claim as native_claim, history, SESSION as native_session
        foreign, client_actor = "second/b", "client-fixture[bot]"
        native = [native_claim(REPO), native_claim(foreign, 2, 20)]
        entry = {"harness": "codex", "session_id": native_session, "_transcript_records": history("codex", *native)}
        for record in entry["_transcript_records"]: record["timestamp"] = SOURCE["claimed_at"]
        def rows(repo, identifier, actor):
            source = comment(identifier, helper.claim.marker({**SOURCE, "session": native_session}), actor)
            return [source, comment(identifier + 1, f"Released claim {identifier}", actor, at="2026-01-01T00:02:00Z")]
        local_comments, client_comments = rows(REPO, 10, ACTOR), rows(foreign, 20, client_actor)
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
        report = helper.check(self.reader, [], entry, ACTOR, reader_for_repo=lambda repo, _: client if repo == foreign else self.reader)
        self.assertTrue(report["claims_clear"], report)
        self.assertEqual({r["repository"] for r in report["released_claims"]}, {REPO, foreign})
        records = history("claude", native[0])
        records[-1:-1] = [
            {"type": "assistant", "sessionId": native_session, "message": {"content": [{"type": "tool_use", "name": "Bash", "id": "redirected-child", "input": {"command": "cd /foreign-repo && uv run /catalog/gh-plan.py claim 2 > claim.json"}}]}},
            {"type": "user", "sessionId": native_session, "message": {"content": [{"type": "tool_result", "tool_use_id": "redirected-child", "content": ""}]}},
        ]
        for record in records: record["timestamp"] = SOURCE["claimed_at"]
        entry = {"harness": "claude", "session_id": native_session, "_transcript_records": records}
        with self.assertRaises(ValueError):
            helper.check(self.reader, [], entry, ACTOR, reader_for_repo=lambda *_: client)
        entry["_extra_repositories"] = [foreign]
        opaque = helper.check(self.reader, [], entry, ACTOR, reader_for_repo=lambda *_: client)
        self.assertFalse(opaque["claims_clear"])  # Inventory cannot prove the lost invocation's scope.
        self.assertFalse(opaque["coverage"]["complete"])
        wrong = Mock(expected_actor=client_actor)
        wrong.paged_json.return_value = []
        wrong.get_json.return_value = {"body": "", "number": 2}
        entry["_extra_repositories"] = ["unrelated/x"]
        self.assertFalse(helper.check(self.reader, [], entry, ACTOR, reader_for_repo=lambda *_: wrong)["claims_clear"])
        wrong.get_json.side_effect = missing()
        self.assertFalse(helper.check(self.reader, [], entry, ACTOR, reader_for_repo=lambda *_: wrong)["claims_clear"])
        entry["_extra_repositories"] = [foreign]
        client_comments.pop()  # The explicit route must still refuse a held claim.
        self.assertFalse(helper.check(self.reader, [], entry, ACTOR, reader_for_repo=lambda *_: client)["claims_clear"])

    def test_an_older_released_claim_cannot_resolve_an_opaque_command(self):
        from test_claim_receipts import claim as native_claim, history, SESSION as native_session
        self.source["body"] = helper.claim.marker({**SOURCE, "session": native_session})
        self.comments.append(self.release)
        self.release["body"] = "Released claim 10\n\nSession: " + native_session
        def pages(path, **kw):
            if path == "/installation/repositories": return [{"full_name": REPO}]
            if path.endswith("/issues/comments"):
                return [{**c, "issue_url": f"https://api.github.com/repos/{REPO}/issues/1"} for c in self.comments]
            if path.endswith("/issues") or path.endswith("/pulls"): return []
            return self.comments
        self.reader.paged_json.side_effect = pages
        for harness in ("codex", "claude"):
            rows = history(harness, native_claim(REPO), native_claim(REPO))
            command = "cd /foreign && uv run /catalog/gh-plan.py claim 1 --worker worker --branch work/task > claim.json"
            if harness == "codex":
                rows[3]["payload"]["arguments"] = json.dumps({"cmd": command})
                rows[4]["payload"]["output"] = ""
            else:
                rows[3]["message"]["content"][0]["input"] = {"command": command}
                rows[4]["message"]["content"][0]["content"] = ""
            for row in rows: row["timestamp"] = SOURCE["claimed_at"]
            entry = {"harness": harness, "session_id": native_session, "_transcript_records": rows,
                     "_extra_repositories": [REPO]}
            report = helper.check(self.reader, [], entry, ACTOR)
            self.assertFalse(report["claims_clear"], report)
            self.assertFalse(report["coverage"]["complete"])
            # The same problem inside one native call must also remain opaque.
            batched = f"uv run /catalog/gh-plan.py claim 1; {command}"
            if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": batched})
            else: rows[1]["message"]["content"][0]["input"] = {"command": batched}
            entry["_transcript_records"] = rows[:3] + rows[5:]
            self.assertFalse(helper.check(self.reader, [], entry, ACTOR)["claims_clear"])

    def test_codex_child_thread_ownership_is_included_in_the_parent_readback(self):
        from test_claim_history import records, claim_history
        from test_claim_receipts import claim as native_claim
        child = "native-child"
        value = native_claim(REPO)
        value["claim"]["session"] = child
        root_records = records("codex", SESSION, delegate=True)
        child_records = records("codex", child, value, parent=SESSION)
        self.source["body"] = helper.claim.marker({**SOURCE, "session": child})
        def pages(path, **kw):
            if path == "/installation/repositories": return [{"full_name": REPO}]
            if path.endswith("/issues/comments"):
                return [{**c, "issue_url": f"https://api.github.com/repos/{REPO}/issues/1"} for c in self.comments]
            if path.endswith("/issues") or path.endswith("/pulls"): return []
            return self.comments
        self.reader.paged_json.side_effect = pages
        nodes = [{"session_id": SESSION, "harness": "codex", "records": root_records},
                 {"session_id": child, "harness": "codex", "records": child_records,
                  "transcript": "/fixture/child.jsonl", "digest": claim_history.fingerprint(child_records)}]
        entry = {"session_id": SESSION, "harness": "codex", "_claim_histories": nodes,
                 "_transcript_records": root_records + child_records, "_extra_repositories": [REPO]}
        self.assertFalse(helper.check(self.reader, [], entry, ACTOR)["claims_clear"])
        self.comments.append({**self.release, "body": "Released claim 10\nSession: " + child})
        report = helper.check(self.reader, [], entry, ACTOR)
        self.assertTrue(report["claims_clear"], report)
        self.assertEqual(set(report["coverage"]["session_ids"]), {SESSION, child})
        self.assertEqual([r["source_session"] for r in report["released_claims"]], [child])

    def test_claude_sidechain_claims_are_included_in_the_native_inventory(self):
        from test_claim_receipts import history, claim as native_claim
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / (SESSION + ".jsonl")
            parent.write_text(json.dumps({"type": "user", "sessionId": SESSION}) + "\n")
            child = Path(tmp) / SESSION / "subagents" / "agent-fixture.jsonl"
            child.parent.mkdir(parents=True)
            value = native_claim(REPO)
            value["claim"]["session"] = SESSION
            records = history("claude", value)
            # Native delegated records carry the parent's inherited identity.
            for record in records: record["sessionId"] = SESSION
            child.write_text("".join(json.dumps(r) + "\n" for r in records))
            sources, errors = helper.claim_receipts.sources(helper.receipt_history(parent, "claude", SESSION), "claude", SESSION)
            self.assertFalse(errors)
            self.assertEqual([(r["repository"], r["claim_comment"]) for r in sources], [(REPO, 10)])

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
            result = helper.check(self.reader, "test", entry, ACTOR, reader_for_repo=lambda *_: self.reader)
        self.assertEqual({call.args[1] for call in discover.call_args_list}, {"test", "second"})
        self.assertFalse(result["claims_clear"])  # Missing source readback cannot be an empty all-clear.


if __name__ == "__main__":
    unittest.main()
