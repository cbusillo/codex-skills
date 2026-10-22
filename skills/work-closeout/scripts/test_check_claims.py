#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["tree-sitter==0.26.0", "tree-sitter-bash==0.25.1"]
# ///
"""Exercise native-session closeout against disposable GitHub and transcript records."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import sqlite3
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


def bash_call(command):
    return [{"message": {"content": [{"type": "tool_use", "name": "Bash", "id": "call", "input": {"command": command}}]}}]


class ClaimCloseoutTests(unittest.TestCase):

    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(Path, "home", return_value=self.folder))
        self.native_index = sqlite3.connect(":memory:")
        self.addCleanup(self.native_index.close)
        self.native_index.executescript(
            "CREATE TABLE threads(id TEXT,rollout_path TEXT); CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT);"
        )
        self.enterContext(
            patch.object(
                helper.history.claim_history,
                "native_databases",
                side_effect=lambda *args, **kwargs: contextlib.nullcontext([self.native_index]),
            )
        )
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
                receipt = {
                    "operation": "github.plan.claim",
                    "claim": {**SOURCE, "session": session},
                    "issue": {"repo": repo, "number": number},
                    "ok": True,
                }
                stream.write(
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {"type": "function_call_output", "output": json.dumps({"output": json.dumps(receipt)})},
                        }
                    )
                    + "\n"
                )
        return entry

    def test_recorded_claims_never_inventory_other_repositories(self) -> None:
        self.comments.append(self.release)
        report = helper.check([self.receipt_entry()])
        self.assertTrue(report["claims_clear"], report)
        self.assertEqual([r["claim_comment"] for r in report["released_claims"]], [self.source["id"]])
        paths = [call.args[0] for method in (self.reader.get_json, self.reader.paged_json) for call in method.call_args_list]
        self.assertTrue(paths)
        self.assertTrue(all(path.startswith(f"/repos/{REPO}/") for path in paths))
        self.assertFalse({f"/repos/{REPO}/issues", f"/repos/{REPO}/issues/comments"}.intersection(paths))
        self.assertEqual(report["coverage"]["repositories"], [REPO])

    def test_input_and_prose_receipt_fixtures_are_not_recorded_claims(self) -> None:
        receipt = {"claim": SOURCE, "issue": {"repo": "fixture/data", "number": 8}}
        command = "cat <<'EOF'\n" + json.dumps(receipt) + "\nEOF"
        rows = [
            {
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "data",
                    "arguments": json.dumps({"cmd": command}),
                }
            },
            {"message": {"content": [{"type": "tool_use", "name": "Bash", "id": "fixture", "input": {"command": command}}]}},
            {"type": "user", "message": {"content": json.dumps(receipt)}},
            {"type": "assistant", "message": {"content": json.dumps(receipt)}},
        ]
        for row in rows:
            entry = self.entry()
            with Path(entry["transcript"]).open("a") as stream:
                stream.write(json.dumps(row) + "\n")
            with self.subTest(row=row), patch.object(helper, "repository_reader") as reader:
                report = helper.check([entry])
                self.assertTrue(report["claims_clear"], report)
                self.assertEqual(report["coverage"]["repositories"], [])
                reader.assert_not_called()

    def test_direct_legacy_helper_receipts_remain_recorded(self) -> None:
        entry = self.entry()
        with Path(entry["transcript"]).open("a") as stream:
            stream.write(json.dumps({"claim": SOURCE, "issue": {"repo": REPO, "number": 1}}) + "\n")
        self.assertEqual(helper.recorded_targets(entry)["targets"], {(REPO, 1)})

    def test_recorded_claim_targets_include_closed_and_pr_only_work_across_sessions(self) -> None:
        records = ((REPO, 1), ("second/b", 5), ("second/b", 6))
        entries = [self.receipt_entry(records=records), self.receipt_entry("child", records=((REPO, 8),))]
        with patch.object(helper, "check_record", return_value=[]) as check:
            report = helper.check(entries)
        self.assertTrue(report["claims_clear"], report)
        self.assertEqual(
            {(c.args[1], c.args[2], c.args[3]) for c in check.call_args_list},
            {(repo, number, SESSION) for repo, number in records} | {(REPO, 8, "child")},
        )

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
        self.native_index.execute("INSERT INTO threads VALUES(?,?)", ("child", str(native / "child.jsonl")))
        self.native_index.execute("INSERT INTO thread_spawn_edges VALUES(?,?)", (SESSION, "child"))
        with patch.object(helper, "check_record", return_value=[]) as check:
            self.assertTrue(helper.check([entry])["claims_clear"])
        self.assertIn(("second/b", 5, "child"), {(c.args[1], c.args[2], c.args[3]) for c in check.call_args_list})

    def test_legacy_claude_subagent_claim_is_checked_under_the_root(self) -> None:
        entry = self.entry()
        Path(entry["transcript"]).write_text(json.dumps({"sessionId": SESSION, "type": "user"}) + "\n")
        subagents = Path(entry["transcript"]).with_suffix("") / "subagents"
        subagents.mkdir(parents=True)
        receipt = {"operation": "github.plan.claim", "claim": SOURCE, "issue": {"repo": REPO, "number": 1}}
        (subagents / "agent-fixture.jsonl").write_text(
            json.dumps(
                {
                    "sessionId": SESSION,
                    "agentId": "fixture",
                    "type": "user",
                    "message": {"content": [{"type": "tool_result", "content": json.dumps(receipt)}]},
                }
            )
            + "\n"
        )
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

    def test_indexed_child_requires_its_advertised_grandchild_and_accepts_explicit_recovery(self) -> None:
        root = self.entry()
        child = self.receipt_entry("child", records=())
        rows = [json.loads(line) for line in Path(child["transcript"]).read_text().splitlines()]
        rows[0]["payload"].update(session_id=SESSION, source={"subagent": {"thread_spawn": {"parent_thread_id": SESSION}}})
        rows.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "child",
                    "item": {
                        "type": "SubAgentActivity",
                        "kind": "started",
                        "agent_thread_id": "grandchild",
                        "agent_path": "/root/child/grandchild",
                    },
                },
            }
        )
        Path(child["transcript"]).write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.native_index.execute("INSERT INTO threads VALUES(?,?)", ("child", child["transcript"]))
        self.native_index.execute("INSERT INTO thread_spawn_edges VALUES(?,?)", (SESSION, "child"))
        with patch.object(helper, "repository_reader") as reader, self.assertRaises(ValueError):
            helper.check([root])
        reader.assert_not_called()
        grandchild = self.receipt_entry("grandchild", records=((REPO, 1),))
        rows = [json.loads(line) for line in Path(grandchild["transcript"]).read_text().splitlines()]
        rows[0]["payload"].update(session_id=SESSION, source={"subagent": {"thread_spawn": {"parent_thread_id": "child"}}})
        Path(grandchild["transcript"]).write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        with patch.object(helper, "check_record", return_value=[]) as check:
            self.assertTrue(helper.check([root, child, grandchild])["claims_clear"])
        self.assertIn("grandchild", {call.args[3] for call in check.call_args_list})

    def test_claude_saved_claim_output_is_read_and_foreign_fork_subagents_are_ignored(self) -> None:
        root = self.entry()
        path = Path(root["transcript"])
        directory = path.with_suffix("")
        output = directory / "tool-results/result.txt"
        output.parent.mkdir(parents=True)
        receipt = {"operation": "github.plan.claim", "claim": SOURCE, "issue": {"repo": REPO, "number": 1}}
        output.write_text(json.dumps(receipt))
        invocation = {
            "sessionId": SESSION,
            "message": {
                "content": [{"type": "tool_use", "name": "Bash", "id": "saved", "input": {"command": "uv run gh-plan.py claim 1"}}]
            },
        }
        path.write_text(
            json.dumps(invocation)
            + "\n"
            + json.dumps(
                {
                    "sessionId": SESSION,
                    "type": "user",
                    "toolUseResult": {"persistedOutputPath": str(output), "persistedOutputSize": output.stat().st_size},
                    "message": {"content": [{"type": "tool_result", "tool_use_id": "saved", "content": "preview omitted"}]},
                }
            )
            + "\n"
        )
        subagents = directory / "subagents"
        subagents.mkdir()
        (subagents / "agent-foreign.jsonl").write_text(json.dumps({"sessionId": "original-session", "agentId": "foreign"}) + "\n")
        self.assertFalse(helper.check([root])["claims_clear"])
        self.comments.append(self.release)
        self.assertTrue(helper.check([root])["claims_clear"])
        output.unlink()
        report = helper.check([root])
        self.assertFalse(report["claims_clear"])
        self.assertFalse(report["coverage"]["complete"])
        self.assertTrue(report["unresolved_invocations"])

    def test_legacy_yielded_claim_receipt_is_recovered_from_later_native_output(self) -> None:
        root = self.entry()
        receipt = {"operation": "github.plan.claim", "claim": SOURCE, "issue": {"repo": REPO, "number": 1}}
        rows = [
            {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "start",
                "input": 'text(await tools.exec_command({cmd:"uv run gh-plan.py --repo test/a claim 1 --session native-session"}));',
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "start",
                "output": [{"type": "input_text", "text": json.dumps({"session_id": 41, "output": ""})}],
            },
            {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "finish",
                "input": 'text(await tools.write_stdin({session_id:41,chars:""}));',
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "finish",
                "output": [{"type": "input_text", "text": json.dumps({"exit_code": 0, "output": json.dumps(receipt)})}],
            },
        ]
        with Path(root["transcript"]).open("a") as stream:
            stream.write("\n".join(json.dumps({"type": "response_item", "payload": row}) for row in rows) + "\n")
        self.assertFalse(helper.check([root])["claims_clear"])
        self.comments.append(self.release)
        self.assertTrue(helper.check([root])["claims_clear"])

    def test_polling_one_process_many_times_links_each_poll_output_once(self) -> None:
        polls = 12
        rows = [{"type": "custom_tool_call_output", "call_id": "start", "output": json.dumps({"session_id": 41})}]
        poll_input = 'text(await tools.write_stdin({session_id:41,chars:""}));'
        for poll in range(polls):
            rows.append({"type": "custom_tool_call", "name": "exec", "call_id": f"poll-{poll}", "input": poll_input})
            rows.append({"type": "custom_tool_call_output", "call_id": f"poll-{poll}", "output": json.dumps({"session_id": 41})})
        outputs = helper.history.tool_outputs([{"payload": row} for row in rows])
        self.assertEqual(len(outputs["start"]), 1 + polls)
        self.assertLessEqual(sum(map(len, outputs.values())), (1 + polls) ** 2)

    def test_literal_repository_target_survives_a_truncated_claim_result(self) -> None:
        root = self.entry()
        with Path(root["transcript"]).open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "name": "exec",
                            "call_id": "claim-call",
                            "input": 'text(await tools.exec_command({cmd:"uv run gh-plan.py --repo test/a claim 1 --session native-session"}));',
                        },
                    }
                )
                + "\n"
            )
            stream.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "call_id": "claim-call",
                            "output": 'Warning: truncated output\n{"claim":',
                        },
                    }
                )
                + "\n"
            )
        self.assertFalse(helper.check([root])["claims_clear"])
        self.comments.append(self.release)
        self.assertTrue(helper.check([root])["claims_clear"])

    def test_real_codex_activity_requires_the_child_transcript(self) -> None:
        entry = self.entry()
        activity = {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": SESSION,
                "item": {"type": "SubAgentActivity", "kind": "started", "agent_thread_id": "child", "agent_path": "/root/child"},
            },
        }
        with Path(entry["transcript"]).open("a") as stream:
            stream.write(json.dumps(activity) + "\n")
            stream.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {"type": "function_call_output", "output": json.dumps({"task_name": "/root/child"})},
                    }
                )
                + "\n"
            )
        with patch.object(helper, "repository_reader") as reader, self.assertRaises(ValueError):
            helper.check([entry])
        reader.assert_not_called()

    def test_real_claude_result_requires_its_subagent_transcript(self) -> None:
        entry = self.entry()
        Path(entry["transcript"]).write_text(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": SESSION,
                    "toolUseResult": {"status": "completed", "agentId": "agent-fixture"},
                    "message": {"content": [{"type": "tool_result", "tool_use_id": "agent-call", "content": "completed"}]},
                }
            )
            + "\n"
        )
        with patch.object(helper, "repository_reader") as reader, self.assertRaises(ValueError):
            helper.check([entry])
        reader.assert_not_called()

    def test_unrelated_corrupt_codex_rollout_cannot_block_the_session(self) -> None:
        entry = self.entry()
        native = self.folder / "codex/sessions/2026/01/01"
        native.mkdir(parents=True)
        path = native / "root.jsonl"
        Path(entry["transcript"]).rename(path)
        entry["transcript"] = str(path)
        (native / "unrelated.jsonl").write_bytes(b"\xff\xfe")
        self.assertTrue(helper.check([entry])["claims_clear"])

    def test_truncated_legacy_claim_result_cannot_mean_no_claims(self) -> None:
        entry = self.entry()
        with Path(entry["transcript"]).open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "name": "exec",
                            "call_id": "claim-call",
                            "input": 'text(await tools.exec_command({cmd:"uv run gh-plan.py claim 1 --session native-session"}));',
                        },
                    }
                )
                + "\n"
            )
            stream.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "call_id": "claim-call",
                            "output": 'Warning: truncated output\n{"claim":',
                        },
                    }
                )
                + "\n"
            )
        with patch.object(helper, "repository_reader") as reader:
            report = helper.check([entry])
        self.assertFalse(report["claims_clear"])
        self.assertTrue(report["unresolved_invocations"])
        reader.assert_not_called()

    def test_unknown_write_without_its_target_cannot_use_another_journal_entry(self) -> None:
        root = self.entry()
        helper.claim_log.record(SESSION, REPO, 2)
        result = {"operation": "github.plan.claim", "ok": False, "claim": SOURCE, "outcome_certainty": "unknown"}
        rows = [
            {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "unknown",
                "input": 'text(await tools.exec_command({cmd:"uv run gh-plan.py claim 1 --session native-session"}));',
            },
            {"type": "custom_tool_call_output", "call_id": "unknown", "output": json.dumps(result)},
        ]
        with Path(root["transcript"]).open("a") as stream:
            stream.write("\n".join(json.dumps({"type": "response_item", "payload": row}) for row in rows) + "\n")
        with patch.object(helper, "check_record", return_value=[]) as check:
            report = helper.check([root])
        self.assertFalse(report["claims_clear"])
        self.assertTrue(report["unresolved_invocations"])
        self.assertIn(2, {call.args[2] for call in check.call_args_list})

    def test_escaped_native_commands_preserve_claim_scope(self) -> None:
        for command in (
            'uv run gh-plan.py --repo="test/a" claim 1 --session="$CODEX_THREAD_ID"',
            'uv run --python 3.12 gh-plan.py --repo test/a claim 1 --session "$CODEX_THREAD_ID"',
            'uv run gh-plan.py --repo test/a claim --worker worker --session "$CODEX_THREAD_ID" 1',
        ):
            records = [
                {
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "quoted",
                        "input": 'text(await tools.exec_command({workdir:"/fixture",cmd:' + json.dumps(command) + "}));",
                    }
                }
            ]
            with self.subTest(command=command):
                self.assertEqual(helper.history.literal_targets(records, SESSION)["targets"], {(REPO, 1)})
        command = 'uv run gh-plan.py claim 1 --session "$CODEX_THREAD_ID"'
        records[0]["payload"]["input"] = "text(await tools.exec_command({cmd:" + json.dumps(command) + "}));"
        self.assertTrue(helper.history.literal_targets(records, SESSION)["unresolved_invocations"])

    def test_computed_native_claim_without_complete_output_blocks(self) -> None:
        rows = [
            {
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "computed",
                    "input": 'const command = "uv run gh-plan.py claim 1"; text(await tools.exec_command({cmd:command}));',
                }
            }
        ]
        self.assertTrue(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"])
        rows.append(
            {
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "computed",
                    "output": json.dumps({"claim": SOURCE, "issue": {"repo": REPO, "number": 1}}),
                }
            }
        )
        self.assertTrue(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"])

    def test_shell_searches_and_heredoc_text_are_not_claim_invocations(self) -> None:
        commands = [
            "rg 'gh-plan.py claim 1' .",
            "cat <<'EOF'\ngh-plan.py claim 1\nEOF",
            'git commit -m "gh-plan.py claim 1"',
            "cat <<EOF\ngh-plan.py claim 1\nEOF",
            "cat <<'EOF'\n$(uv run gh-plan.py claim 1)\nEOF",
        ]
        for command in commands:
            rows = [{"message": {"content": [{"type": "tool_use", "name": "Bash", "id": "read", "input": {"command": command}}]}}]
            with self.subTest(command=command):
                self.assertEqual(helper.history.literal_targets(rows, SESSION), {"targets": set(), "unresolved_invocations": []})

    def test_literal_loops_and_variables_are_exact_claim_targets(self) -> None:
        cases = {
            "cd /r; P=/skills/github/scripts; for i in 1 2; do uv run $P/gh-plan.py --repo test/a claim $i"
            " --session native-session 2>&1 | tail -c 900; echo; done": {(REPO, 1), (REPO, 2)},
            "cd /r && S=/skills; for i in 1 2; do uv run $S/github/scripts/gh-plan.py --repo test/a claim $i"
            " > /tmp/claim-$i.json; python3 -c 'import json,sys;print(json.load(open(sys.argv[1])))' /tmp/claim-$i.json;"
            " sleep 3; done": {(REPO, 1), (REPO, 2)},
            "S=/skills/github/scripts/gh-plan.py; cd /r && uv run $S --repo test/a claim 3 --session native-session"
            " | python3 -c 'import json,sys;print(json.load(sys.stdin))'": {(REPO, 3)},
            'for i in other/repo#2; do uv run gh-plan.py --repo test/a claim "$i"; done': {("other/repo", 2)},
            "R=test/a; for i in 1 2; do for j in 3; do uv run gh-plan.py --repo $R claim $i$j; done; done": {(REPO, 13), (REPO, 23)},
        }
        for command, targets in cases.items():
            with self.subTest(command=command):
                self.assertEqual(
                    helper.history.literal_targets(bash_call(command), SESSION), {"targets": targets, "unresolved_invocations": []}
                )

    def test_claim_values_a_shell_could_change_stay_computed(self) -> None:
        claim = 'uv run gh-plan.py --repo "$R" claim 2'
        commands = [
            "S=gh-plan.py; uv run $S claim 2",
            'for i in $(cat list); do uv run gh-plan.py --repo test/a claim "$i"; done',
            "for i in 1 2; do i=3; uv run gh-plan.py --repo test/a claim $i; done",
            f"R=test/a; builtin read R <<< other/repo; {claim}",
            f"R=test/a; command read R <<< other/repo; {claim}",
            f"R=test/a; read R < file; {claim}",
            f"R=test/a; printf -v R %s other/repo; {claim}",
            f'N=R; R=test/a; printf -v "$N" %s other/repo; {claim}',
            f"R=test/a; wait -p R; {claim}",
            f'R=test/a; eval "$SETUP"; {claim}',
            f"R=test/a; source ./env.sh; {claim}",
            f"R=test/a; R=other/repo; {claim}",
            f"{claim}; R=test/a",
            f"true && R=test/a; {claim}",
            f"R=test/a & {claim}",
            f"R=test/a; unset R; {claim}",
            f"R=test/a; f() {{ R=other/repo; }}; f; {claim}",
            f"R=test/a; export R=other/repo; {claim}",
            f"R=test/a; : ${{R:=other/repo}}; {claim}",
            f"R=test/a; IFS=/; {claim}",
            f"R=~/a; {claim}",
            f"R='test/a other/b'; {claim}",
            'R=test/b; uv run gh-plan.py --repo "${R}${SUFFIX}" claim 2',
            "S=/skills/gh-plan.py/x; uv run $S:h --repo test/a claim 2",
            f"R=test/a; bash <<'EOF'\n{claim}\nEOF",
            "bash <<'EOF' | cat\nuv run gh-plan.py claim 2\nEOF",
            "cd /r && bash <<'EOF' | tail -5\nuv run gh-plan.py claim 2\nEOF",
            "for i in 1; do uv run gh-plan.py --repo test/a claim $i; done;"
            " python3 -c 'from invoke import run; import sys; run(sys.argv[1])' \"$CMD\"",
            "python3 - <<'PY'\nimport subprocess\nsubprocess.run(['uv', 'run', path, 'claim'])  # gh-plan\nPY",
            "python3 - <<'PY'\nimport subprocess\nverb = 'claim'\n"
            "subprocess.run(['git', '-c', 'alias.claim=!uv run gh-plan.py --repo test/a claim 2', verb])\nPY",
            "SECONDS=2; sleep 2; uv run gh-plan.py --repo test/a claim $SECONDS",
            "for RANDOM in 1 2; do uv run gh-plan.py --repo test/a claim $RANDOM; done",
            "for BASH_ARGV0 in 1; do uv run gh-plan.py --repo test/a claim $BASH_ARGV0; done",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(helper.history.literal_targets(bash_call(command), SESSION)["unresolved_invocations"])

    def test_scripts_and_patches_that_only_mention_claims_are_data(self) -> None:
        commands = [
            "cd /w && cat > /tmp/probe.py <<'EOF'\nfor cmd in ['S=gh-plan.py; uv run $S --repo test/a claim 2']:\n    print(cmd)\nEOF\n"
            "uv run --with tree-sitter==0.26.0 python /tmp/probe.py 2>&1 | cut -c1-400",
            "S=/tmp/scratch; cat > $S/prompt.md <<'EOF'\nReview whether gh-plan.py claim 2 is cleared.\nEOF\n"
            "cd /m && timeout 1000 uv run scripts/review_with_model.py run --prompt-file $S/prompt.md --out $S/review.md"
            " > $S/review.json 2> $S/review.err; echo exit=$?; tail -5 $S/review.err",
            "cd /w && python3 - <<'EOF'\nfrom pathlib import Path\npath = Path('test.py')\n"
            "path.write_text(path.read_text().replace('old', 'uv run gh-plan.py --repo test/a claim 2'))\nEOF",
            "python3 - <<'PY'\nimport subprocess\np = '/x.jsonl.zst'\ns = subprocess.Popen(['zstd', '-dc', p], stdout=subprocess.PIPE, text=True)\n"
            "for line in s.stdout:\n    if 'claim ' in line and 'gh-plan' in line:\n        print(line)\nPY",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(
                    helper.history.literal_targets(bash_call(command), SESSION), {"targets": set(), "unresolved_invocations": []}
                )
        cell = (
            'text(await tools.apply_patch("*** Begin Patch\\n+Keep the source claim unchanged.\\n*** End Patch"));\n'
            'const ts=["skills/a/test_claim.py","skills/b/test_release.py"];\n'
            "await Promise.allSettled(ts.map(p=>tools.exec_command({cmd:`scripts/run-python-helper.sh ${p}`})));"
        )
        rows = [{"payload": {"type": "custom_tool_call", "name": "exec", "call_id": "cell", "input": cell}}]
        self.assertEqual(helper.history.literal_targets(rows, SESSION), {"targets": set(), "unresolved_invocations": []})
        rows[0]["payload"]["input"] = "text(await tools.apply_patch(`${note} the source claim`)); " + cell.split("\n", 1)[1]
        self.assertTrue(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"])

    def test_released_loop_claims_clear_and_a_computed_loop_stays_conditional(self) -> None:
        for values, clear in (("1 2", True), ("$(cat issues)", False)):
            root = self.entry()
            command = f"for i in {values}; do uv run gh-plan.py --repo {REPO} claim $i --session {SESSION} | tail -c 900; done"
            call = {"type": "function_call", "name": "shell_command", "call_id": "loop", "arguments": json.dumps({"command": command})}
            with Path(root["transcript"]).open("a") as stream:
                stream.write(json.dumps({"payload": call}) + "\n")
            with self.subTest(values=values), patch.object(helper, "check_record", return_value=[]) as check:
                self.assertEqual(helper.check([root])["claims_clear"], clear)
                if clear:
                    self.assertEqual({call.args[2] for call in check.call_args_list}, {1, 2})

    def test_claude_saved_output_completes_the_originating_bare_claim(self) -> None:
        root = self.entry()
        path = Path(root["transcript"])
        output = path.with_suffix("") / "tool-results/result.txt"
        output.parent.mkdir(parents=True)
        output.write_text(json.dumps({"claim": SOURCE, "issue": {"repo": REPO, "number": 1}}))
        rows = [
            {
                "sessionId": SESSION,
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "id": "saved", "input": {"command": "uv run gh-plan.py claim 1"}}
                    ]
                },
            },
            {
                "sessionId": SESSION,
                "toolUseResult": {"persistedOutputPath": str(output)},
                "message": {"content": [{"type": "tool_result", "tool_use_id": "saved", "content": "preview"}]},
            },
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.assertFalse(helper.check([root])["claims_clear"])
        self.comments.append(self.release)
        self.assertTrue(helper.check([root])["claims_clear"])

    def test_claude_foreign_saved_output_and_complete_fork_receipts_are_ignored(self) -> None:
        root = self.entry()
        path = Path(root["transcript"])
        rows = [
            {
                "sessionId": SESSION,
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "id": "copied", "input": {"command": "uv run gh-plan.py claim 1"}}
                    ]
                },
            },
            {
                "sessionId": SESSION,
                "toolUseResult": {"persistedOutputPath": str(self.folder / "foreign/tool-results/result.txt")},
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "copied",
                            "content": json.dumps({"claim": {**SOURCE, "session": "foreign"}, "issue": {"repo": REPO, "number": 1}}),
                        }
                    ]
                },
            },
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.assertTrue(helper.check([root])["claims_clear"])

    def test_claude_background_completion_reads_the_original_bash_output(self) -> None:
        root = self.entry()
        path = Path(root["transcript"])
        output = self.folder / "temporary" / SESSION / "tasks/task.output"
        output.parent.mkdir(parents=True)
        output.write_text(json.dumps({"claim": SOURCE, "issue": {"repo": REPO, "number": 1}}))
        rows = [
            {
                "sessionId": SESSION,
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "id": "background", "input": {"command": "uv run gh-plan.py claim 1"}}
                    ]
                },
            },
            {
                "sessionId": SESSION,
                "toolUseResult": {"backgroundTaskId": "task"},
                "message": {"content": [{"type": "tool_result", "tool_use_id": "background", "content": "running"}]},
            },
            {
                "sessionId": SESSION,
                "origin": {"kind": "task-notification", "producer": "session-task", "runId": "native-run"},
                "message": {
                    "content": f"<task-notification><task-id>task</task-id><tool-use-id>background</tool-use-id><output-file>{output}</output-file><status>completed</status></task-notification>"
                },
            },
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.assertFalse(helper.check([root])["claims_clear"])
        self.comments.append(self.release)
        self.assertTrue(helper.check([root])["claims_clear"])
        output.unlink()
        report = helper.check([root])
        self.assertFalse(report["claims_clear"])
        self.assertFalse(report["coverage"]["complete"])
        self.assertTrue(report["unresolved_invocations"])

    def test_explicit_claude_child_uses_its_root_saved_output_after_restore(self) -> None:
        root = self.entry()
        path = Path(root["transcript"])
        path.write_text(json.dumps({"sessionId": SESSION, "toolUseResult": {"agentId": "child"}}) + "\n")
        child = self.folder / "restored/agent-child.jsonl"
        output = path.with_suffix("") / "tool-results/result.txt"
        child.parent.mkdir(parents=True)
        output.parent.mkdir(parents=True)
        output.write_text(json.dumps({"claim": SOURCE, "issue": {"repo": REPO, "number": 1}}))
        original = self.folder / "old-host" / SESSION / "tool-results/result.txt"
        invocation = {
            "sessionId": SESSION,
            "agentId": "child",
            "message": {
                "content": [{"type": "tool_use", "name": "Bash", "id": "saved", "input": {"command": "uv run gh-plan.py claim 1"}}]
            },
        }
        child.write_text(
            json.dumps(invocation)
            + "\n"
            + json.dumps(
                {
                    "sessionId": SESSION,
                    "agentId": "child",
                    "toolUseResult": {"persistedOutputPath": str(original)},
                    "message": {"content": [{"type": "tool_result", "tool_use_id": "saved", "content": "preview"}]},
                }
            )
            + "\n"
        )
        supplied = {"session_id": SESSION, "transcript": str(child)}
        self.assertFalse(helper.check([supplied, root])["claims_clear"])
        self.comments.append(self.release)
        self.assertTrue(helper.check([supplied, root])["claims_clear"])

    def test_explicit_inline_codex_child_is_not_walked_again_as_a_root(self) -> None:
        root = self.entry()
        child = self.entry("child")
        rows = [
            {
                "type": "session_meta",
                "payload": {
                    "id": "child",
                    "session_id": SESSION,
                    "parent_thread_id": SESSION,
                    "forked_from_id": SESSION,
                    "subagent_history_start_ordinal": 2,
                    "source": {"subagent": {"thread_spawn": {"parent_thread_id": SESSION}}},
                },
            },
            {"type": "session_meta", "payload": {"id": SESSION}},
            {"type": "event_msg", "payload": {"type": "thread_settings_applied", "thread_id": "child"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "output": json.dumps({"claim": {**SOURCE, "session": "child"}, "issue": {"repo": REPO, "number": 1}}),
                },
            },
        ]
        for ordinal, row in enumerate(rows):
            row["ordinal"] = ordinal
        Path(child["transcript"]).write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.native_index.execute("INSERT INTO thread_spawn_edges VALUES(?,?)", (SESSION, "child"))
        with patch.object(helper, "check_record", return_value=[]) as check:
            self.assertTrue(helper.check([child, root])["claims_clear"])
        self.assertIn("child", {call.args[3] for call in check.call_args_list})

    def test_one_batch_receipt_cannot_hide_a_second_claim_target(self) -> None:
        for qualified in (True, False):
            repository = f"--repo {REPO} " if qualified else ""
            commands = [f"uv run gh-plan.py {repository}claim {number} --session {SESSION}" for number in (1, 2)]
            code = " ".join("text(await tools.exec_command({cmd:" + json.dumps(command) + "}));" for command in commands)
            rows = [
                {"payload": {"type": "custom_tool_call", "name": "exec", "call_id": "batch", "input": code}},
                {
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "batch",
                        "output": json.dumps({"claim": SOURCE, "issue": {"repo": REPO, "number": 1}}),
                    }
                },
            ]
            with self.subTest(qualified=qualified):
                if qualified:
                    self.assertIn((REPO, 2), helper.history.literal_targets(rows, SESSION)["targets"])
                else:
                    self.assertTrue(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"])

    def test_unrelated_missing_claude_artifacts_do_not_block(self) -> None:
        root = self.entry()
        path = Path(root["transcript"])
        for tool, command in (("Bash", "uv run pytest"), ("Read", None)):
            rows = [
                {
                    "sessionId": SESSION,
                    "message": {"content": [{"type": "tool_use", "name": tool, "id": "unrelated", "input": {"command": command}}]},
                },
                {
                    "sessionId": SESSION,
                    "toolUseResult": {"persistedOutputPath": str(self.folder / "deleted/unexpected/result.txt")},
                    "message": {"content": [{"type": "tool_result", "tool_use_id": "unrelated", "content": "preview"}]},
                },
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            with self.subTest(tool=tool):
                self.assertTrue(helper.check([root])["claims_clear"])

    def test_native_shell_adapters_recover_targets_from_truncated_results(self) -> None:
        command = f"uv run gh-plan.py --repo {REPO} claim 1 --session {SESSION}"
        payloads = [
            {"type": "function_call", "name": "shell_command", "call_id": "legacy", "arguments": json.dumps({"command": command})},
            {
                "type": "function_call",
                "name": "shell",
                "call_id": "legacy",
                "arguments": json.dumps({"command": ["zsh", "-lc", command]}),
            },
            {"type": "local_shell_call", "call_id": "legacy", "action": {"type": "exec", "command": ["zsh", "-lc", command]}},
        ]
        for payload in payloads:
            with self.subTest(tool=payload.get("name", payload["type"])):
                self.assertEqual(helper.history.literal_targets([{"payload": payload}], SESSION)["targets"], {(REPO, 1)})
        native = {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "id": "native-command",
                    "command": ["zsh", "-lc", command],
                    "status": "completed",
                    "aggregated_output": "truncated",
                    "exit_code": 0,
                },
            },
        }
        self.assertEqual(helper.history.literal_targets([native], SESSION)["targets"], {(REPO, 1)})
        native["payload"]["item"].update(command=["zsh", "-lc", "uv run gh-plan.py claim 1"], status="declined")
        self.assertEqual(helper.history.literal_targets([native], SESSION)["targets"], set())

    def test_mixed_literal_and_computed_claim_cannot_clear(self) -> None:
        rows = [
            {
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "mixed",
                    "input": 'const command = "uv run gh-plan.py claim 1"; text(await tools.exec_command({cmd:"git status"})); text(await tools.exec_command({cmd:command}));',
                }
            }
        ]
        self.assertTrue(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"])

    def test_structured_native_denial_is_prewrite_but_unknown_interrupt_is_not(self) -> None:
        root = self.entry()
        path = Path(root["transcript"])
        rows = [
            {
                "sessionId": SESSION,
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "id": "denied", "input": {"command": "uv run gh-plan.py claim 1"}}
                    ]
                },
            },
            {
                "sessionId": SESSION,
                "toolDenialKind": "user-rejected",
                "toolUseResult": "denied",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "denied",
                            "is_error": True,
                            "content": "The user doesn't want to proceed with this tool use.",
                        }
                    ]
                },
            },
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.assertTrue(helper.check([root])["claims_clear"])
        rows[-1].pop("toolDenialKind")
        rows[-1]["message"]["content"][0]["content"] = json.dumps(
            {"operation": "github.plan.claim", "ok": False, "write_outcome": "unknown", "error": "KeyboardInterrupt"}
        )
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.assertFalse(helper.check([root])["claims_clear"])
        rows[-1]["message"]["content"][0]["content"] = json.dumps(
            {"operation": "github.plan.claim", "ok": False, "write_outcome": "not_started", "failed_step": "argument_parsing"}
        )
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.assertTrue(helper.check([root])["claims_clear"])

    def test_unresolved_output_does_not_hide_a_known_held_claim(self) -> None:
        root = self.entry()
        helper.claim_log.record(SESSION, REPO, 1)
        with Path(root["transcript"]).open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "payload": {
                            "type": "function_call",
                            "name": "shell_command",
                            "call_id": "lost",
                            "arguments": json.dumps({"command": "uv run gh-plan.py claim 2"}),
                        }
                    }
                )
                + "\n"
            )
        report = helper.check([root])
        self.assertFalse(report["claims_clear"])
        self.assertFalse(report["coverage"]["complete"])
        self.assertTrue(report["blocked"])
        self.assertIn("lost", {item["call_id"] for item in report["unresolved_invocations"]})

    def test_orphan_saved_shell_output_cannot_prove_complete_claim_coverage(self) -> None:
        root = self.entry()
        path = Path(root["transcript"])
        output = path.with_suffix("") / "tool-results/result.txt"
        path.write_text(
            json.dumps(
                {
                    "sessionId": SESSION,
                    "toolUseResult": {"persistedOutputPath": str(output)},
                    "message": {"content": [{"type": "tool_result", "tool_use_id": "orphan", "content": "preview"}]},
                }
            )
            + "\n"
        )
        report = helper.check([root])
        self.assertFalse(report["claims_clear"])
        self.assertFalse(report["coverage"]["complete"])
        self.assertIn("orphan", {item["call_id"] for item in report["unresolved_invocations"]})

    def test_javascript_patch_text_is_not_a_shell_claim(self) -> None:
        root = self.entry()
        source = "text(await tools.apply_patch(`*** Begin Patch\n uv run gh-plan.py claim 5\n director's note\n*** End Patch`));"
        with Path(root["transcript"]).open("a") as stream:
            stream.write(
                json.dumps({"payload": {"type": "custom_tool_call", "name": "exec", "call_id": "patch", "input": source}}) + "\n"
            )
        with patch.object(helper, "repository_reader") as reader:
            self.assertTrue(helper.check([root])["claims_clear"])
        reader.assert_not_called()

    def test_unrecognized_shell_call_forms_remain_conditional(self) -> None:
        sources = [
            'const {exec_command} = tools; text(await exec_command({cmd:"uv run gh-plan.py claim 5"}));',
            'text(await tools["exec_command"]({cmd:"uv run gh-plan.py claim 5"}));',
            'const run = tools.exec_command; text(await run({cmd:"uv run gh-plan.py claim 5"}));',
        ]
        for source in sources:
            root = self.entry()
            with Path(root["transcript"]).open("a") as stream:
                stream.write(
                    json.dumps({"payload": {"type": "custom_tool_call", "name": "exec", "call_id": "alias", "input": source}}) + "\n"
                )
            with self.subTest(source=source):
                self.assertFalse(helper.check([root])["claims_clear"])

    def test_stored_command_cannot_hide_a_later_computed_claim(self) -> None:
        root = self.entry()
        sources = ['store("c", "uv run gh-plan.py claim 5");', 'text(await tools.exec_command({cmd:load("c")}));']
        with Path(root["transcript"]).open("a") as stream:
            for index, source in enumerate(sources):
                stream.write(
                    json.dumps({"payload": {"type": "custom_tool_call", "name": "exec", "call_id": str(index), "input": source}})
                    + "\n"
                )
        self.assertFalse(helper.check([root])["claims_clear"])

    def test_fresh_javascript_cells_do_not_inherit_earlier_local_command_values(self) -> None:
        sources = [
            'text(await tools.exec_command({cmd:"uv run gh-plan.py --repo first/a claim 5"})); // store("fixture", "data")',
            'const paths=["validator.py"]; text(await tools.exec_command({cmd:"uv run "+paths[0],workdir:"/work/claim-budget"}));',
            'text(await tools.exec_command({cmd:load("unrelated")}));',
        ]
        rows = [
            {"payload": {"type": "custom_tool_call", "name": "exec", "call_id": str(index), "input": source}}
            for index, source in enumerate(sources)
        ]
        self.assertEqual(helper.history.literal_targets(rows, SESSION), {"targets": {("first/a", 5)}, "unresolved_invocations": []})

    def test_computed_cell_receipts_keep_unobserved_claim_executions_conditional(self) -> None:
        rows = [
            {
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "receipt",
                    "input": "text(await tools.exec_command({cmd:commandFromData()}));",
                }
            },
            {
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "receipt",
                    "output": json.dumps({"claim": SOURCE, "issue": {"repo": "first/a", "number": 5}}),
                }
            },
        ]
        self.assertTrue(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"])

    def test_decode_failure_still_checks_a_known_held_claim(self) -> None:
        root = self.entry()
        helper.claim_log.record(SESSION, REPO, 1)
        with Path(root["transcript"]).open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "CommandExecution",
                                "id": "invalid",
                                "command": {"invalid": True},
                                "status": "completed",
                            },
                        },
                    }
                )
                + "\n"
            )
        report = helper.check([root])
        self.assertFalse(report["claims_clear"])
        self.assertTrue(report["blocked"])
        self.assertTrue(report["unresolved_invocations"])

    def test_a_different_invocations_receipt_or_rejection_cannot_clear_a_bare_claim(self) -> None:
        rejection = {
            "operation": "github.plan.claim",
            "ok": False,
            "write_outcome": "not_started",
            "failed_step": "argument_parsing",
        }
        receipt = {"claim": SOURCE, "issue": {"repo": "first/a", "number": 5}}
        cases = [
            ("uv run gh-plan.py --repo first/a claim 7 --bad; uv run gh-plan.py claim 5", rejection),
            ("uv run gh-plan.py claim 3 --session foreign --bad; uv run gh-plan.py claim 5", rejection),
            ("uv run gh-plan.py claim 5; uv run gh-plan.py --repo first/a claim 5", receipt),
            ("cd first; uv run gh-plan.py claim 5; cd second; uv run gh-plan.py claim 5", receipt),
            (
                'for flag in --bad1 --bad2; do uv run gh-plan.py claim 7 "$flag"; done; uv run gh-plan.py claim 5',
                [rejection, {**rejection, "error": "another invalid flag"}],
            ),
            (
                'for repo in first/a second/b; do uv run gh-plan.py --repo "$repo" claim 5; done; uv run gh-plan.py claim 5',
                [receipt, {**receipt, "issue": {"repo": "second/b", "number": 5}}],
            ),
        ]
        cases.append(
            (
                'claim_repos() { for repo in first/a second/b; do uv run gh-plan.py --repo "$repo" claim 5; done; }; claim_repos; uv run gh-plan.py claim 5',
                [receipt, {**receipt, "issue": {"repo": "second/b", "number": 5}}],
            )
        )
        for command, result in cases:
            root = self.entry()
            rows = [
                {"type": "function_call", "name": "exec_command", "call_id": "batch", "arguments": json.dumps({"cmd": command})},
                {"type": "function_call_output", "call_id": "batch", "output": json.dumps(result)},
            ]
            with Path(root["transcript"]).open("a") as stream:
                stream.write("\n".join(json.dumps({"payload": row}) for row in rows) + "\n")
            with self.subTest(command=command), patch.object(helper, "check_record", return_value=[]):
                self.assertFalse(helper.check([root])["claims_clear"])

    def test_duplicate_receipts_check_a_record_once(self) -> None:
        entry = self.receipt_entry(records=((REPO, 1), (REPO, 1)))
        with patch.object(helper, "check_record", return_value=[]) as check:
            self.assertTrue(helper.check([entry])["claims_clear"])
        self.assertEqual(check.call_count, 1)

    def test_unrecognized_shell_execution_keeps_claim_coverage_conditional(self) -> None:
        commands = [
            "timeout 120 uv run gh-plan.py claim 5",
            'GP=/fixture/gh-plan.py; uv run "$GP" claim 5',
            "bash <<'EOF'\nuv run gh-plan.py claim 5\nEOF",
            "python3 -c \"import subprocess; subprocess.run('uv run gh-plan.py claim 5', shell=True)\"",
            "python3 - <<'PY'\nimport subprocess\nsubprocess.run('uv run gh-plan.py claim 5', shell=True)\nPY",
            "printf 5 | xargs -I{} uv run gh-plan.py claim {}",
            "cat <<EOF\n$(uv run gh-plan.py claim 5)\nEOF",
            "cat <<EOF\n$(\nuv run gh-plan.py claim 5\n)\nEOF",
            "echo 'uv run gh-plan.py claim 5' | bash",
            "printf '%s' 'uv run gh-plan.py claim 5' | sh",
            "cat <<'EOF' | bash\nuv run gh-plan.py claim 5\nEOF",
        ]
        for command in commands:
            rows = [{"message": {"content": [{"type": "tool_use", "name": "Bash", "id": "wrapped", "input": {"command": command}}]}}]
            with self.subTest(command=command):
                self.assertTrue(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"])

    def test_javascript_interpolated_execution_remains_conditional(self) -> None:
        source = 'text(`${(await tools.exec_command({cmd:"uv run gh-plan.py claim 5"})).output}`);'
        rows = [{"payload": {"type": "custom_tool_call", "name": "exec", "call_id": "template", "input": source}}]
        self.assertTrue(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"])

    def test_python_fixture_data_is_distinct_from_process_execution(self) -> None:
        data = "rows=[{'cmd':'uv run gh-plan.py claim 5'}]; print(history.literal_targets(rows,SESSION))"
        cases = [
            (data, False),
            (data + "; print(sorted(history.literal_targets(rows,SESSION)))", False),
            (data + "; print(bool(history.literal_targets(rows,SESSION)))", False),
            (data + "; call = 'fixture'; exec_commands = []; print(call, exec_commands)", False),
            (data + "; subprocess.run(['git', 'status'])", False),
            (data + "; subprocess.run(command)", True),
            (data + "; from subprocess import run as launch; launch(command)", True),
            (data + "; launch = subprocess.run; launch(command)", True),
            (data + "; pool.map(subprocess.run, commands)", True),
        ]
        for source, unresolved in cases:
            for command in (
                "python3 - <<'PY'\n" + source + "\nPY",
                "python3 -c " + json.dumps(source),
                "uv run --python 3.13 python3 - <<'PY'\n" + source + "\nPY",
                "uv run --script - <<'PY'\n" + source + "\nPY",
                "PYTHONDONTWRITEBYTECODE=1 uv run --offline --no-project python - <<'PY'\n" + source + "\nPY",
            ):
                rows = [
                    {"message": {"content": [{"type": "tool_use", "name": "Bash", "id": "python", "input": {"command": command}}]}}
                ]
                with self.subTest(source=source, command=command):
                    self.assertEqual(bool(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"]), unresolved)

    def test_handoff_bodies_and_text_filters_are_data(self) -> None:
        commands = [
            "scripts/gh-issue create Follow-up --repo test/a <<'EOF'\nResume with gh-plan.py claim 5\nEOF",
            'gh pr create --body "Resume with gh-plan.py claim 5"',
            'uv run skills/github/scripts/gh-pr.py comment 5 --body "gh-plan.py claim 5"',
            "sed -n '/gh-plan.py claim/p' file",
            'jq -r ".claim" <<\'EOF\'\n{"claim":"gh-plan.py claim 5"}\nEOF',
            'uv run pytest -k "gh-plan.py claim"',
            "echo 'gh-plan.py claim 5' | python -c 'import sys; print(sys.stdin.read())'",
        ]
        for command in commands:
            rows = [{"message": {"content": [{"type": "tool_use", "name": "Bash", "id": "body", "input": {"command": command}}]}}]
            with self.subTest(command=command):
                self.assertEqual(helper.history.literal_targets(rows, SESSION), {"targets": set(), "unresolved_invocations": []})

    def test_python_process_launcher_families_keep_unknown_claims_conditional(self) -> None:
        calls = [
            'subprocess.getoutput("uv run gh-plan.py claim 5")',
            'subprocess.getstatusoutput("uv run gh-plan.py claim 5")',
            'asyncio.create_subprocess_shell("uv run gh-plan.py claim 5")',
            'asyncio.create_subprocess_exec("uv", "run", "gh-plan.py", "claim", "5")',
            'os.execv("uv", ["uv", "run", "gh-plan.py", "claim", "5"])',
            'os.execl("uv", "uv", "run", "gh-plan.py", "claim", "5")',
            'os.spawnv(os.P_WAIT, "uv", ["uv", "run", "gh-plan.py", "claim", "5"])',
            'os.posix_spawn("uv", ["uv", "run", "gh-plan.py", "claim", "5"], {})',
            'from subprocess import run as launch; commands=["uv run gh-plan.py claim 5"]; pool.map(launch, commands)',
        ]
        for source in calls:
            command = "python3 - <<'PY'\n" + source + "\nPY"
            rows = [{"message": {"content": [{"type": "tool_use", "name": "Bash", "id": "launch", "input": {"command": command}}]}}]
            with self.subTest(source=source):
                self.assertTrue(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"])

    def test_shell_header_boundaries_preserve_executable_source_and_fixture_data(self) -> None:
        claim = "uv run gh-plan.py claim 5"
        qualified = "uv run gh-plan.py --repo first/a claim 5"
        cases = [
            (f"timeout 120 bash -c '{claim}'", True, set()),
            (f"timeout 120 bash -lc '{claim}'", True, set()),
            (f"FOO=1 bash -lc '{claim}'", True, set()),
            (f"uv run bash -ec '{claim}'", True, set()),
            (f'out="$({claim})"', True, set()),
            (f'echo "$({claim})"', True, set()),
            (f'out="`{claim}`"', True, set()),
            (f"out=`{claim}`", True, set()),
            (f"out='$({claim})'", False, set()),
            (f'out="\\$({claim})"', False, set()),
            ('gh issue comment 5 --body "$(cat body.md)"', False, set()),
            (f'jq . <<< "$({claim})"', True, set()),
            (f"nohup sh -c '{claim}'", True, set()),
            (f"sudo -u user bash -c '{claim}'", True, set()),
            (f"env -i bash -c '{claim}'", True, set()),
            (f"uv run bash -c '{claim}'", True, set()),
            (f"xargs -I{{}} sh -c '{claim}'", True, set()),
            (f"find . -exec sh -c '{claim}' \\;", True, set()),
            (f"timeout 120 bash <<'EOF'\n{claim}\nEOF", True, set()),
            (f"echo '{claim}' | timeout 5 bash", True, set()),
            (f"bash <<< '{claim}'", True, set()),
            (f'grep -q ok <<<"ok"\n{qualified}', False, {("first/a", 5)}),
            (f"gh --body='cat <<EOF'\n{qualified}", False, {("first/a", 5)}),
            (f'gh --body="cat <<EOF"\n{qualified}', False, {("first/a", 5)}),
            (f"cat <<-'EOF'\n\t{claim}\n\tEOF", False, set()),
            (f"cat <<'EOF'\nfixture's \"unfinished quote | bash {claim}\nEOF", False, set()),
            (f"cat <<'EOF'; printf unrelated | bash\n{claim}\nEOF", True, set()),
            (f"cat <<\\EOF\n$({claim})\nEOF", False, set()),
            (f"cat <<'EOF' | bash -s -- 5\n{claim}\nEOF", True, set()),
            (f"echo '{claim}' | python3 - arg", True, set()),
            (f"echo '{claim}' | sh /dev/stdin", True, set()),
            (f"{{ echo '{claim}'; }} | bash", True, set()),
            (f"(echo '{claim}'; echo x) | sh", True, set()),
            (f"echo '{claim}' | cat | bash", True, set()),
            ("timeout 60 python3 -Ic \"subprocess.run('uv run gh-plan.py claim 5', shell=True)\"", True, set()),
            ("perl -le \"system('uv run gh-plan.py claim 5')\"", True, set()),
            ("ruby -we \"system('uv run gh-plan.py claim 5')\"", True, set()),
            ("bash <<'EOF'\ncd /work/claim-budget\nuv run gh-plan.py view 5\nEOF", False, set()),
            ("timeout 120 bash -c 'cd /work/claim-budget && uv run gh-plan.py view 5'", False, set()),
            ("printf '%s' 'uv run /work/claim-budget/gh-plan.py view 5' | bash", False, set()),
            ('uv run pytest -k "gh-plan.py claim" .', False, set()),
            ("cp /work/claim-budget/gh-plan.py .", False, set()),
            ('gh issue create --label source --body "gh-plan.py claim 5"', False, set()),
            (
                f"cat >body.md <<EOF\nBranch: $(git branch --show-current)\nNext: {qualified}\nDon't lose this handoff.\nEOF",
                False,
                set(),
            ),
            (f"gh issue comment 5 --body-file - <<EOF\nBranch: `git branch --show-current`\nNext: {claim}\nEOF", False, set()),
            (f"cat <<EOF\nDon't skip $({claim})\nEOF", True, set()),
            (f"cat <<'EOF' | bash -ex\n{claim}\nEOF", True, set()),
            (f"printf '%s' '{claim}' | bash -e", True, set()),
            (f"printf '%s' '{claim}' | sh -xe", True, set()),
            (f"cat <<'EOF' | bash -se\n{claim}\nEOF", True, set()),
            (f"dash -c '{qualified}'", False, {("first/a", 5)}),
            (f'''eval "$(echo '{claim}')"''', True, set()),
            (f'''bash -c "$(printf '%s' '{claim}')"''', True, set()),
            (f'''bash 2>/dev/null -c "$(printf '%s' '{claim}')"''', True, set()),
            (f"printf '%s' '{claim}' | bash 2>/dev/null -e", True, set()),
            ("uv run gh-plan.py --repo first/a 2>/dev/null claim 5", False, {("first/a", 5)}),
            (qualified + " >/dev/null", False, {("first/a", 5)}),
            (f"cat <({claim})", True, set()),
            (f"tee >({claim})", True, set()),
            (f"cat <({qualified})", False, {("first/a", 5)}),
            (f"read x < <({qualified})", False, {("first/a", 5)}),
            (f"printf '%s' 2>({qualified})", True, set()),
            (f"printf '%s' '<({claim})'", False, set()),
            (f'''bash -c 'echo "{claim}"' ''', False, set()),
            (f"""printf '%s' '{claim}' """, False, set()),
            ("python - <<'PY'\nfixture = \"fixture's unmatched quote | bash gh-plan.py claim 5\"\nprint(fixture)\nPY", False, set()),
            ("timeout 60 python3 - <<'PY'\nsubprocess.run('uv run gh-plan.py claim 5', shell=True)\nPY", True, set()),
        ]
        for command, unresolved, targets in cases:
            for javascript in (False, True):
                payload = (
                    {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "boundary",
                        "input": "text(await tools.exec_command({cmd:" + json.dumps(command) + "}));",
                    }
                    if javascript
                    else {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "boundary",
                        "arguments": json.dumps({"cmd": command}),
                    }
                )
                with self.subTest(command=command, javascript=javascript):
                    report = helper.history.literal_targets([{"payload": payload}], SESSION)
                    self.assertEqual(report["targets"], targets)
                    self.assertEqual(bool(report["unresolved_invocations"]), unresolved)

    def test_nested_heredoc_handoff_bodies_are_data(self) -> None:
        for body in (
            "Resume with gh-plan.py claim 5. Don't merge yet.",
            '1) "Resume" with gh-plan.py claim 5. $(uv run gh-plan.py --repo fixture/data claim 8)',
        ):
            for consumer in ("gh pr create --body", "git commit -m", "gh issue comment 5 --body"):
                command = consumer + " \"$(cat <<'EOF'\n" + body + '\nEOF\n)"'
                rows = [
                    {"message": {"content": [{"type": "tool_use", "name": "Bash", "id": "body", "input": {"command": command}}]}}
                ]
                with self.subTest(command=command):
                    self.assertEqual(helper.history.literal_targets(rows, SESSION), {"targets": set(), "unresolved_invocations": []})

    def test_unrelated_literal_program_paths_and_input_do_not_inherit_fixture_data(self) -> None:
        commands = [
            "printf '%s' 'gh-plan.py claim 5'; python -m compileall -q gh-plan.py",
            "cat >fixture.txt <<'EOF'\ngh-plan.py claim 5\nEOF\nuv run python patch.py",
            "printf '%s' 'gh-plan.py claim 5'; printf unrelated | bash",
        ]
        for command in commands:
            rows = [
                {
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "fixture",
                        "arguments": json.dumps({"cmd": command}),
                    }
                }
            ]
            with self.subTest(command=command):
                self.assertEqual(helper.history.literal_targets(rows, SESSION), {"targets": set(), "unresolved_invocations": []})

    def test_generated_and_process_substitution_program_channels_remain_conditional(self) -> None:
        claim = "uv run gh-plan.py claim 5"
        commands = [
            f"CMD='{claim}'; echo \"$CMD\" | bash",
            f"CMD='{claim}'; printf '%s\\n' \"$CMD\" | sh",
            f"CMD='{claim}'; bash <<< \"$CMD\"",
            f"CMD='{claim}'; bash <<EOF\n$CMD\nEOF",
            f"source <(echo '{claim}')",
            f"bash <(printf '%s' '{claim}')",
            f"bash < <(echo '{claim}')",
            f". <(cat <<'EOF'\n{claim}\nEOF\n)",
            f"CMD='{claim}'; source <(echo \"$CMD\")",
            f"CMD='{claim}'; bash < <(echo \"$CMD\")",
            f"export CMD='{claim}'; bash <<'EOF'\n$CMD\nEOF",
            f"export CMD='{claim}'; bash -c '$CMD'",
            f"CMD='{claim}'; bash 0< <(echo \"$CMD\")",
            f"CMD='{claim}'; eval '$CMD'",
            f"export CMD='{claim}'; bash <<<'$CMD'",
            f"export CMD='{claim}'; python -c 'import os; os.system(os.environ[\"CMD\"])'",
            f"export CMD='{claim}'; python <<'PY'\nimport os\nos.system(os.environ['CMD'])\nPY",
        ]
        for command in commands:
            rows = [{"message": {"content": [{"type": "tool_use", "name": "Bash", "id": "channel", "input": {"command": command}}]}}]
            with self.subTest(command=command):
                self.assertTrue(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"])

    def test_unclassified_launcher_program_strings_remain_conditional(self) -> None:
        claim = "uv run gh-plan.py claim 5"
        commands = [
            f"awk 'BEGIN{{system(\"{claim}\")}}'",
            f"ssh host '{claim}'",
            f"su -c '{claim}' user",
            f"runuser -c '{claim}' user",
            f"flock lock -c '{claim}'",
            f"script -c '{claim}'",
            f"watch '{claim}'",
            f"tmux send-keys -t pane '{claim}' Enter",
            f"tmux new-window '{claim}'",
            f"screen -X stuff '{claim}\\n'",
            f"parallel ::: '{claim}'",
            f"osascript -e 'do shell script \"{claim}\"'",
            f"php -r 'system(\"{claim}\");'",
            f"lua -e 'os.execute(\"{claim}\")'",
            f"pwsh -c '{claim}'",
            f"Rscript -e 'system(\"{claim}\")'",
        ]
        for command in commands:
            rows = [
                {
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "launcher",
                        "arguments": json.dumps({"cmd": command}),
                    }
                }
            ]
            with self.subTest(command=command):
                self.assertTrue(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"])

    def test_interactive_input_preserves_targets_and_bare_count_uncertainty(self) -> None:
        receipt = {"claim": SOURCE, "issue": {"repo": "first/a", "number": 5}}
        for command, unresolved in (
            ("uv run gh-plan.py claim 5\n", True),
            ("uv run gh-plan.py --repo first/a claim 5\n", False),
            ("", False),
        ):
            for javascript in (False, True):
                payload = (
                    {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "input",
                        "input": "text(await tools.write_stdin({session_id:41,chars:" + json.dumps(command) + "}));",
                    }
                    if javascript
                    else {
                        "type": "function_call",
                        "name": "write_stdin",
                        "call_id": "input",
                        "arguments": json.dumps({"session_id": 41, "chars": command}),
                    }
                )
                rows = [
                    {"payload": payload},
                    {"payload": {"type": "function_call_output", "call_id": "input", "output": json.dumps(receipt)}},
                ]
                with self.subTest(command=command, javascript=javascript):
                    report = helper.history.literal_targets(rows, SESSION)
                    self.assertEqual(report["targets"], {("first/a", 5)})
                    self.assertEqual(bool(report["unresolved_invocations"]), unresolved)

    def test_unknown_command_channels_keep_claims_conditional(self) -> None:
        claim = "uv run gh-plan.py claim 5"
        commands = [
            f"ssh host <<'EOF'\n{claim}\nEOF",
            f"ssh host <<< '{claim}'",
            f"echo '{claim}' | ssh host",
            f"sudo -s <<'EOF'\n{claim}\nEOF",
            f"su user <<'EOF'\n{claim}\nEOF",
            f"echo '{claim}' | xargs sh -c",
            f"echo '{claim}' | xargs -I{{}} sh -c '{{}}'",
            f"echo '{claim}' | parallel",
            f"at now <<'EOF'\n{claim}\nEOF",
            f"script -q <<'EOF'\n{claim}\nEOF",
            f"docker exec node sh -c '{claim}'",
            f"sudo -u node bash -c '{claim}'",
            f"tmux new-window -n node -c DIR '{claim}'",
            f"screen -S ruby -X stuff '{claim}'",
            f"ssh -l perl host '{claim}'",
            f"CMD='{claim}'; timeout sh -c \"$CMD\"",
            f"CMD='{claim}'; ssh host \"$CMD\"",
            f'''node -p "require('child_process').execSync('{claim}')"''',
            f'''node --print "require('child_process').execSync('{claim}')"''',
            f'''bun -p "require('child_process').execSync('{claim}')"''',
            f'''deno eval "Deno.Command('sh', {{args: ['-c', '{claim}']}}).outputSync()"''',
            f"source /dev/stdin <<'EOF'\n{claim}\nEOF",
            f". /dev/fd/0 <<< '{claim}'",
            f"bash -c 'bash' <<'EOF'\n{claim}\nEOF",
            f"sh -c 'cat | sh' <<'EOF'\n{claim}\nEOF",
            f'''export CMD='{claim}'; node -e "require('child_process').execSync(process.env.CMD)"''',
        ]
        for prefix in ("timeout 60", "env -i", "nohup", "nice -n 5", "exec", "command", "sudo -E -u user", "custom-runner"):
            commands.extend(
                f"export CMD='{claim}'; {prefix} {program}"
                for program in (
                    '''node -e "require('child_process').execSync(process.env.CMD)"''',
                    '''python3 -c "import os; os.system(os.environ['CMD'])"''',
                    "bash -c '$CMD'",
                    "bash <<'EOF'\n$CMD\nEOF",
                    "python3 - <<'PY'\nimport os; os.system(os.environ['CMD'])\nPY",
                    "eval '$CMD'",
                    "source /dev/stdin <<'EOF'\n$CMD\nEOF",
                    ". /dev/fd/0 <<<'$CMD'",
                )
            )
        commands.extend(
            f"export CMD='{claim}'; {program}"
            for program in (
                '''deno eval "new Deno.Command('sh', {args: ['-c', Deno.env.get('CMD')]}).outputSync()"''',
                '''node --eval="require('child_process').execSync(process.env.CMD)"''',
                '''node --print="require('child_process').execSync(process.env.CMD)"''',
                '''python3 -c"import os; os.system(os.environ['CMD'])"''',
                """perl -e'system($ENV{CMD})' """,
                '''custom-runner --eval="run(process.env.CMD)"''',
                """custom-runner -c'run(CMD)' """,
                "python3 -cprint ignored",
                "perl -esystem ignored",
                "bash -c -e '$CMD'",
                "bash -c -x '$CMD'",
                "bash -c -o pipefail '$CMD'",
                "bash -c -- '$CMD'",
                "bash -c --unknown-option value '$CMD'",
            )
        )
        for interpreter in (
            "node -r ts-node/register",
            "node --require ./hook.js",
            "node --import ./register.mjs",
            "perl -I lib/",
            "ruby -I ./lib",
            "python3 --unknown-option ./operand.py",
        ):
            commands.extend(
                (
                    f"{interpreter} <<'EOF'\n{claim}\nEOF",
                    f"{interpreter} <<< '{claim}'",
                    f"CMD='{claim}'; {interpreter} < program",
                )
            )
        for command in commands:
            rows = [{"message": {"content": [{"type": "tool_use", "name": "Bash", "id": "channel", "input": {"command": command}}]}}]
            with self.subTest(command=command):
                self.assertTrue(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"])

    def test_file_helper_arguments_and_input_remain_data(self) -> None:
        claim = "uv run gh-plan.py claim 5"
        commands = [
            f"skills/github/scripts/gh-issue create '{claim}' --repo o/r < body.md",
            f"skills/github/scripts/gh-comment 5 --repo o/r '{claim}'",
            f"skills/github/scripts/gh-pr.py comment 5 --body '{claim}'",
            f"uv run skills/github/scripts/gh-pr.py comment 5 --body '{claim}'",
            f"python skills/github/scripts/gh-pr.py comment 5 --body '{claim}'",
            f"skills/github/scripts/gh-issue create Follow-up --repo o/r <<'EOF'\n{claim}\nEOF",
            f"skills/github/scripts/gh-pr.py comment 5 --body-file - <<'EOF'\n{claim}\nEOF",
            f"echo '{claim}' | skills/github/scripts/gh-comment 5 --repo o/r",
            f"echo '{claim}' | uv run python -c 'import sys; print(sys.stdin.read())'",
        ]
        for command in commands:
            rows = [{"message": {"content": [{"type": "tool_use", "name": "Bash", "id": "data", "input": {"command": command}}]}}]
            with self.subTest(command=command):
                self.assertEqual(helper.history.literal_targets(rows, SESSION), {"targets": set(), "unresolved_invocations": []})

    def test_python_residual_calls_preserve_launcher_and_data_boundaries(self) -> None:
        claim = "uv run gh-plan.py claim 5"
        cases = [
            (f"from subprocess import *; run('{claim}', shell=True)", True),
            (f"command='{claim}'; from subprocess import *; run(command, shell=True)", True),
            (f"__import__('os').system('{claim}')", True),
            (f"command='{claim}'; launcher=__import__('os'); launcher.system(command)", True),
            (f"import importlib; importlib.import_module('os').system('{claim}')", True),
            (f"command='{claim}'; import importlib as loader; module=loader.import_module('os'); module.system(command)", True),
            (f"command='{claim}'; from importlib import import_module as load; module=load('os'); module.system(command)", True),
            (f"import invoke; invoke.run('{claim}')", True),
            ("import pexpect; pexpect.spawn('uv', ['run', 'gh-plan.py', 'claim', '5'])", True),
            (f"launch('{claim}')", True),
            (f"print('{claim}')", False),
            (f"import json; print(json.dumps({{'cmd':'{claim}'}}))", False),
            (f"from pathlib import Path; Path('fixture').write_text('{claim}')", False),
            (f"from pathlib import Path; p=Path('fixture'); p.write_text('{claim}')", False),
            (f"source='old'; source=source.replace('old', '{claim}'); print(source)", False),
            (f"print(' '.join(['gh-plan.py', 'claim', '5']))", False),
            (f"import ast; ast.parse({claim!r})", False),
            (f"from subprocess import *; rows='{claim}'; print(rows)", False),
            (f"loader=__import__('os'); rows='{claim}'; print(rows)", False),
            (f"with open('fixture', 'w') as stream: stream.write({claim!r})", False),
            (f"import re; re.compile({claim!r}); re.search({claim!r}, 'text')", False),
            (f"rows=[]; rows.append({{'command': {claim!r}}}); rows.extend([{claim!r}])", False),
            (f"import textwrap; source=textwrap.dedent({claim!r}); source.startswith({claim!r})", False),
            (f"self.assertIn({claim!r}, rows)", False),
            (f"import logging; logging.info({claim!r})", False),
            (f"import subprocess; subprocess.run(['rg', {claim!r}, 'skills'])", False),
            (
                f"from pathlib import Path; old=Path('file').read_text(); head=__import__('subprocess').check_output(['git','rev-parse','HEAD'],text=True).strip(); Path('out').write_text({claim!r})",
                False,
            ),
            (
                f"import importlib; module=importlib.import_module('subprocess'); module.run(['git','status']); print({claim!r})",
                False,
            ),
            (f"command={claim!r}; import invoke; invoke.run(command)", True),
            (f"command: str={claim!r}; from invoke import run; run(command)", True),
            (f"command={claim!r}; pexpect.spawn(command)", True),
            (f"command={claim!r}; __import__('subprocess').run(command,shell=True)", True),
            (f"command={claim!r}; getattr(__import__('os'), 'system')(command)", True),
            (f"source={claim!r}; print(history.has_executable_claim_source(source, 'python'))", False),
            (f"source={claim!r}; print(history.shell_groups(source))", False),
            ("n=5; command=f'uv run gh-plan.py claim {n}'; import invoke; invoke.run(command)", True),
            ("command='uv run gh-plan.py ' + 'claim 5'; import invoke; invoke.run(command)", True),
            ("n=5; command='uv run gh-plan.py claim {}'.format(n); import invoke; invoke.run(command)", True),
            ("n=5; command='uv run gh-plan.py claim %s' % n; import invoke; invoke.run(command)", True),
            ("command=['uv', 'run', 'gh-plan.py', 'claim', '5']; pexpect.spawn(command[0], command[1:])", True),
            ("command={'argv': ['uv', 'run', 'gh-plan.py', 'claim', '5']}; launch(command['argv'])", True),
            ("command='uv run gh-plan.py'; command += ' claim 5'; import invoke; invoke.run(command)", True),
            (
                "command='uv run gh-plan.py'\nif True:\n command += ' claim 5'\n import invoke\n invoke.run(command)\ncommand='other'",
                True,
            ),
            ("command='uv run gh-plan.py'\nif True:\n command += ' claim 5'\n print(command)\ncommand='other'", False),
            (f"command=''; command += {claim!r}; launch(command)", True),
            (f"(command, other)=({claim!r}, None); launch(command)", True),
            (f"[other, *command]=[None, {claim!r}]; launch(command)", True),
            (f"launch(command := [{claim!r}])", True),
            (f"command: list[str]=[{claim!r}]; launch(command)", True),
            (f"for command in [{claim!r}]: pexpect.spawn(command)", True),
            (f"async def run():\n async for command in [{claim!r}]: invoke.run(command)", True),
            (f"[pexpect.spawn(command) for command in [{claim!r}]]", True),
            (f"{{invoke.run(command) for command in [{claim!r}]}}", True),
            (f"with context({claim!r}) as command: invoke.run(command)", True),
            (f"async def run():\n async with context({claim!r}) as command: invoke.run(command)", True),
            (f"match {claim!r}:\n case command: pexpect.spawn(command)", True),
            (f"match {{'cmd': {claim!r}}}:\n case {{'cmd': command}}: invoke.run(command)", True),
            (f"match [{claim!r}]:\n case [*command]: invoke.run(command)", True),
            (f"match {{'cmd': {claim!r}}}:\n case {{**command}}: invoke.run(command)", True),
            (f"commands={{}}; commands['cmd']={claim!r}; invoke.run(commands['cmd'])", True),
            (f"self.command={claim!r}; pexpect.spawn(self.command)", True),
            (f"for command in [{claim!r}]: print(command)", False),
            (f"[str(command) for command in [{claim!r}]]", False),
            (f"commands={{}}; commands['cmd']={claim!r}; print(commands['cmd'])", False),
            (f"self.command={claim!r}; print(self.command)", False),
            (
                f"import shlex\nfor text in [{claim!r}]:\n lexer=shlex.shlex(text,posix=False,punctuation_chars=';&|()<>')\n lexer.whitespace_split=True\n lexer.commenters=''\n print(text,list(lexer))",
                False,
            ),
            (
                f"import shlex\nfor source in [{claim!r}]:\n for mode in (False,True):\n  lex=shlex.shlex(source,posix=mode)\n  print(mode,repr(source),list(lex))",
                False,
            ),
            (
                f"def show(node):\n print(node.type)\n for child in node.children: show(child)\nfor source in [{claim!r}]: show(parser.parse(source.encode()).root_node)",
                False,
            ),
            (f"source={claim!r}; shlex.shlex(source)", False),
            (f"source={claim!r}; invoke.run(shlex.shlex(source))", True),
            (f"source={claim!r}; invoke.run(shlex.split(source))", True),
            (f"source={claim!r}; invoke.run(list(shlex.shlex(source)))", True),
            (f"source={claim!r}; invoke.run(parser.parse(source.encode()).root_node.text)", True),
            (f"source={claim!r}; invoke.run(str(parser.parse(source.encode()).root_node))", True),
            (f"source={claim!r}; invoke.run(f'{{parser.parse(source.encode()).root_node}}')", True),
            (f"source={claim!r}; invoke.run('{{}}'.format(parser.parse(source.encode()).root_node))", True),
            (f"source={claim!r}; invoke.run([parser.parse(source.encode()).root_node])", True),
            (f"source={claim!r}; invoke.run(str(source))", True),
            (f"source={claim!r}; invoke.run(source.format())", True),
            (f"source={claim!r}; invoke.run(' '.join([source]))", True),
            (f"source={claim!r}; invoke.run(unknown(source).root_node)", True),
            (f"rows=[{claim!r}]; print(rows); rows.append({claim!r})", False),
            (f"rows=[{claim!r}]; print(history.literal_targets(rows, 'session'))", False),
        ]
        for source, unresolved in cases:
            command = "python - <<'PY'\n" + source + "\nPY"
            rows = [{"message": {"content": [{"type": "tool_use", "name": "Bash", "id": "python", "input": {"command": command}}]}}]
            with self.subTest(source=source):
                self.assertEqual(bool(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"]), unresolved)

    def test_native_text_pipeline_consumers_remain_data(self) -> None:
        commands = [
            r"rg -n 'gh-plan\.py claim (?:<|[0-9])' skills --glob '*.md' | head -35",
            "printf '%s' 'gh-plan.py claim 5' | sed -n '1p' | wc -l",
            "printf '%s' 'gh-plan.py claim 5' | tail -5 | cut -c1-20",
            "rg -n 'gh-plan.py claim' skills | sort | uniq -c | tr a-z A-Z | nl | column -t | less",
            "rg -n 'gh-plan.py claim' skills | pbcopy",
            "rg -n 'gh-plan.py claim' skills | diff - fixture",
            "ps -ef | grep 'gh-plan.py claim'",
            "ps -eo pid,command | grep 'gh-plan.py claim'",
        ]
        for command in commands:
            rows = [{"message": {"content": [{"type": "tool_use", "name": "Bash", "id": "text", "input": {"command": command}}]}}]
            with self.subTest(command=command):
                self.assertEqual(helper.history.literal_targets(rows, SESSION), {"targets": set(), "unresolved_invocations": []})

    def test_nonexecuting_siblings_preserve_qualified_claim_targets(self) -> None:
        commands = [
            'cd "$WT" && uv run gh-plan.py --repo first/a claim 5',
            'mkdir -p "$D"; test -f "$f"; ls "$D"; uv run gh-plan.py --repo first/a claim 5',
            'cd "$(git rev-parse --show-toplevel)" && uv run gh-plan.py --repo first/a claim 5',
            'uv run -q --directory "$WT" --project "$WT" --env-file config.env --extra test --group test gh-plan.py --repo first/a claim 5',
        ]
        commands.extend(
            prefix + "; uv run gh-plan.py --repo first/a claim 5"
            for prefix in (
                "set -euo pipefail",
                "tar -czf archive path",
                "curl -c jar https://example.invalid",
                "stat -c %s file",
                'kill -TERM "$PID"',
                "docker run -e FOO=bar image",
                "xcodebuild -scheme test",
                "find . -name '*.py'",
            )
        )
        commands.extend(
            f"bash {options} 'uv run gh-plan.py --repo first/a claim 5'"
            for options in (
                "-c -e",
                "-c -x",
                "-c -o pipefail",
                "-c +o pipefail",
                "-c -O extglob",
                "-c --",
                "-oc pipefail",
                "-Oc extglob",
            )
        )
        for command in commands:
            rows = [
                {"message": {"content": [{"type": "tool_use", "name": "Bash", "id": "qualified", "input": {"command": command}}]}}
            ]
            with self.subTest(command=command):
                self.assertEqual(
                    helper.history.literal_targets(rows, SESSION), {"targets": {("first/a", 5)}, "unresolved_invocations": []}
                )

    def test_uv_options_select_the_command_instead_of_interpreter_option_values(self) -> None:
        source = """from tree_sitter import Language,Parser
import tree_sitter_bash
source=b\'''gh pr create --body "$(cat <<'EOF'\\nResume gh-plan.py claim 5. Don't merge.\\nEOF\\n)"\'''
root=Parser(Language(tree_sitter_bash.language())).parse(source).root_node
print(root)"""
        prefixes = [
            "uv run --no-project --python /opt/homebrew/bin/python3.14 --with tree-sitter==0.26.0 --with tree-sitter-bash==0.25.1 python -",
            "uv run --python=/opt/homebrew/bin/python3.14 --with=tree-sitter python -",
            "uv run --offline --with-requirements requirements.txt --no-project python -",
            "uv run --script --python /opt/homebrew/bin/python3.14 -",
        ]
        for prefix in prefixes:
            for program, unresolved in ((source, False), ("import os; os.system('uv run gh-plan.py claim 5')", True)):
                command = prefix + " <<'PY'\n" + program + "\nPY"
                rows = [{"message": {"content": [{"type": "tool_use", "name": "Bash", "id": "uv", "input": {"command": command}}]}}]
                with self.subTest(prefix=prefix, program=program):
                    self.assertEqual(bool(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"]), unresolved)
        for option in ("--unknown-option", "--unknown-option=value"):
            command = f"uv run {option} python - <<'PY'\n" + source + "\nPY"
            rows = [{"message": {"content": [{"type": "tool_use", "name": "Bash", "id": "uv", "input": {"command": command}}]}}]
            with self.subTest(option=option):
                self.assertTrue(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"])

    def test_python_terminal_and_in_process_helpers_remain_conditional(self) -> None:
        sources = [
            "import pty; pty.spawn(['uv', 'run', 'gh-plan.py', 'claim', '5'])",
            "from pty import spawn as launch; launch(['uv', 'run', 'gh-plan.py', 'claim', '5'])",
            "import runpy, sys; sys.argv=['gh-plan.py', 'claim', '5']; runpy.run_path('gh-plan.py', run_name='__main__')",
            "from runpy import run_path as launch; import sys; sys.argv=['gh-plan.py', 'claim', '5']; launch('gh-plan.py')",
            "from os import execv as launch; launch('uv', ['uv', 'run', 'gh-plan.py', 'claim', '5'])",
            "import os; launch=os.execv; launch('uv', ['uv', 'run', 'gh-plan.py', 'claim', '5'])",
            "from os import posix_spawn as launch; launch('uv', ['uv', 'run', 'gh-plan.py', 'claim', '5'], {})",
        ]
        for source in sources:
            command = "python - <<'PY'\n" + source + "\nPY"
            rows = [
                {
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "python",
                        "arguments": json.dumps({"cmd": command}),
                    }
                }
            ]
            with self.subTest(source=source):
                self.assertTrue(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"])

    def test_interactive_program_context_survives_until_later_input(self) -> None:
        for javascript in (False, True):
            rows = []
            for index, chars in enumerate(("CMD='uv run gh-plan.py claim 5'\n", "$CMD\n")):
                payload = (
                    {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": str(index),
                        "input": "text(await tools.write_stdin({session_id:41,chars:" + json.dumps(chars) + "}));",
                    }
                    if javascript
                    else {
                        "type": "function_call",
                        "name": "write_stdin",
                        "call_id": str(index),
                        "arguments": json.dumps({"session_id": 41, "chars": chars}),
                    }
                )
                rows.append({"payload": payload})
            with self.subTest(javascript=javascript):
                self.assertTrue(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"])
        rows = [
            {
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "start",
                    "arguments": json.dumps({"cmd": "export CMD='uv run gh-plan.py claim 5'; bash", "tty": True}),
                }
            },
            {"payload": {"type": "function_call_output", "call_id": "start", "output": json.dumps({"session_id": 41})}},
            {
                "payload": {
                    "type": "function_call",
                    "name": "write_stdin",
                    "call_id": "input",
                    "arguments": json.dumps({"session_id": 41, "chars": "$CMD\n"}),
                }
            },
        ]
        self.assertTrue(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"])

    def test_interactive_context_does_not_taint_independent_shells_or_empty_polls(self) -> None:
        prefix = "text(await tools.write_stdin({session_id:41,chars:\"CMD=\\'uv run gh-plan.py claim 5\\'\\n\"}));"
        for source in (
            'const paths=["validator.py"]; await Promise.all(paths.map(path=>tools.exec_command({cmd:"uv run "+path}))); text(await tools.write_stdin({session_id:41,chars:""}));',
            'text(await tools.exec_command({cmd:"$UNRELATED"})); text(await tools.write_stdin({session_id:41,chars:""}));',
        ):
            rows = [
                {"payload": {"type": "custom_tool_call", "name": "exec", "call_id": str(index), "input": text}}
                for index, text in enumerate((prefix, source))
            ]
            with self.subTest(source=source):
                self.assertEqual(helper.history.literal_targets(rows, SESSION), {"targets": set(), "unresolved_invocations": []})

    def test_sibling_output_cannot_discharge_a_bare_claim(self) -> None:
        receipt = {"claim": SOURCE, "issue": {"repo": "first/a", "number": 5}}
        rejection = {"operation": "github.plan.claim", "ok": False, "write_outcome": "not_started"}
        commands = [
            "cat earlier-receipt.json; uv run gh-plan.py claim 5",
            "uv run gh-plan.py claim 5; cat earlier-receipt.json",
            "printf '%s' old-result; uv run gh-plan.py claim 5",
            "out=$(cat earlier-receipt.json); uv run gh-plan.py claim 5",
            "uv run gh-plan.py claim 5 | cat earlier-receipt.json",
        ]
        for command in commands:
            for output in (receipt, rejection):
                rows = [
                    {
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": "mixed",
                            "arguments": json.dumps({"cmd": command}),
                        }
                    },
                    {"payload": {"type": "function_call_output", "call_id": "mixed", "output": json.dumps(output)}},
                ]
                with self.subTest(command=command, output=output):
                    report = helper.history.literal_targets(rows, SESSION)
                    self.assertTrue(report["unresolved_invocations"])
                    self.assertEqual(report["targets"], {("first/a", 5)} if output is receipt else set())
        rows[0]["payload"]["arguments"] = json.dumps({"cmd": "uv run gh-plan.py claim 5"})
        self.assertFalse(helper.history.literal_targets(rows, SESSION)["unresolved_invocations"])

    def test_parser_failure_keeps_receipts_without_using_recovered_commands(self) -> None:
        command = "cat <<'EOF'; printf unrelated | bash\nuv run gh-plan.py --repo fixture/data claim 8\nEOF"
        rows = [
            {
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "syntax",
                    "arguments": json.dumps({"cmd": command}),
                }
            },
            {
                "payload": {
                    "type": "function_call_output",
                    "call_id": "syntax",
                    "output": json.dumps({"claim": SOURCE, "issue": {"repo": "first/a", "number": 5}}),
                }
            },
        ]
        report = helper.history.literal_targets(rows, SESSION)
        self.assertTrue(report["unresolved_invocations"])
        self.assertEqual(report["targets"], {("first/a", 5)})

    def test_pure_claim_receipts_and_qualified_mixed_claims_keep_their_targets(self) -> None:
        receipt = {"claim": SOURCE, "issue": {"repo": "first/a", "number": 5}}
        for command, targets in (
            ("uv run gh-plan.py claim 5", {("first/a", 5)}),
            ("FOO=1; { uv run gh-plan.py claim 5; }", {("first/a", 5)}),
            ("cat earlier-receipt.json; uv run gh-plan.py --repo second/b claim 5", {("first/a", 5), ("second/b", 5)}),
            ("bash -c 'uv run gh-plan.py --repo second/b claim 5'", {("first/a", 5), ("second/b", 5)}),
            ("bash <<'EOF'\nuv run gh-plan.py --repo second/b claim 5\nEOF", {("first/a", 5), ("second/b", 5)}),
        ):
            rows = [
                {
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "claim",
                        "arguments": json.dumps({"cmd": command}),
                    }
                },
                {"payload": {"type": "function_call_output", "call_id": "claim", "output": json.dumps(receipt)}},
            ]
            with self.subTest(command=command):
                self.assertEqual(helper.history.literal_targets(rows, SESSION), {"targets": targets, "unresolved_invocations": []})

    def test_raw_javascript_does_not_prove_bare_claim_execution_count(self) -> None:
        command = 'tools.exec_command({cmd:"uv run gh-plan.py claim 5",workdir})'
        sources = [
            f'for (const workdir of ["/a", "/b"]) text(await {command});',
            f"while (next()) text(await {command});",
            f'await Promise.all(["/a", "/b"].map(async workdir => {command}));',
            f'async function run(workdir) {{ return {command}; }} text(await run("/a")); text(await run("/b"));',
            f"text(await {command});",
        ]
        receipts = [{"claim": SOURCE, "issue": {"repo": repo, "number": 5}} for repo in ("first/a", "second/b")]
        rejection = {"operation": "github.plan.claim", "ok": False, "write_outcome": "not_started"}
        for source in sources:
            for result in (receipts[:1], receipts, rejection):
                rows = [
                    {"payload": {"type": "custom_tool_call", "name": "exec", "call_id": "repeat", "input": source}},
                    {"payload": {"type": "custom_tool_call_output", "call_id": "repeat", "output": json.dumps(result)}},
                ]
                with self.subTest(source=source, result=result):
                    report = helper.history.literal_targets(rows, SESSION)
                    self.assertTrue(report["unresolved_invocations"])
                    self.assertEqual(
                        report["targets"], {(item["issue"]["repo"], 5) for item in result} if isinstance(result, list) else set()
                    )
            rows = [
                {
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "qualified",
                        "input": source.replace("claim 5", "--repo first/a claim 5"),
                    }
                }
            ]
            self.assertEqual(
                helper.history.literal_targets(rows, SESSION), {"targets": {("first/a", 5)}, "unresolved_invocations": []}
            )

    def test_uncertain_javascript_reads_its_saved_receipt_targets(self) -> None:
        rows = [
            {
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "saved",
                    "input": 'text(await tools.exec_command({cmd:"uv run gh-plan.py claim 5"}));',
                }
            }
        ]
        saved = [
            {
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "saved",
                    "output": json.dumps({"claim": SOURCE, "issue": {"repo": "second/b", "number": 5}}),
                }
            }
        ]
        with patch.object(helper.history, "persisted_output", return_value=saved) as read:
            report = helper.history.literal_targets(rows, SESSION, directory=self.folder)
        read.assert_called_once()
        self.assertEqual(report["targets"], {("second/b", 5)})
        self.assertTrue(report["unresolved_invocations"])

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
