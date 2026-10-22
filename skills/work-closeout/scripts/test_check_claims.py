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
            (f'out=`{claim}`', True, set()),
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
            (f"cat <<'EOF'; printf unrelated | bash\n{claim}\nEOF", False, set()),
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
