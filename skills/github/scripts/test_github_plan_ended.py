# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5", "PyYAML==6.0.3", "tree-sitter==0.26.0", "tree-sitter-bash==0.25.1"]
# ///
from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import github_plan_release as release
import github_plan_termination_history as ended
import test_gh_plan_release as fixtures


class HistoricalTerminationTests(fixtures.ReleaseHarness):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(self.f.doCleanups)
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.native = "e5bd6d9f-d638-4a78-a6d1-ae9f8944beca"
        self.transcript = self.directory / f"{self.native}.jsonl"
        self.record = {
            "session_id": self.native,
            "claim_session": "session-b",
            "harness": "claude",
            "host": "source-host",
            "transcript": str(self.transcript),
            "transcript_roots": [str(self.directory)],
            "cause": "native local exit completed",
            "retained_artifacts": [],
            "exit_record": {"kind": "claude_local_exit"},
            "claim": fixtures.fixtures.OTHER,
            "claim_url": "https://github.com/owner/repo/issues/42#issuecomment-1",
            "claim_created_at": self.f.comments[0]["created_at"],
        }

        def user(text: str, at: str, **extra: object) -> dict:
            return {"sessionId": self.native, "timestamp": at, "type": "user", "message": {"content": text}, **extra}

        receipt = {
            "ok": True,
            "claim": fixtures.fixtures.OTHER,
            "claim_comment": {"comment": {"id": 1, "url": self.record["claim_url"]}},
        }
        self.rows = [
            {
                "sessionId": self.native,
                "timestamp": "2026-09-30T23:59:59Z",
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "claim",
                            "name": "Bash",
                            "input": {
                                "command": "uv run gh-plan.py claim 42 --session session-b --worker trial-b --branch work/other-42"
                            },
                        }
                    ]
                },
            },
            {
                "sessionId": self.native,
                "timestamp": "2026-10-01T00:00:01Z",
                "type": "user",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "claim", "content": json.dumps(receipt)}]},
            },
            user("<local-command-caveat>Local command output</local-command-caveat>", "2026-10-01T00:09:59Z", isMeta=True),
            user(
                "<command-name>/quit</command-name>\n<command-message>quit</command-message>\n<command-args></command-args>",
                "2026-10-01T00:10:00Z",
            ),
            user("<local-command-stdout></local-command-stdout>", "2026-10-01T00:10:01Z"),
        ]
        self.path = self.directory / "termination.json"
        self.args.termination_record = self.path
        self.native_check = self.enterContext(patch.object(ended, "verify_native", return_value=None))
        self.enterContext(patch.object(ended, "configured_roots", return_value={self.directory}))
        self.enterContext(patch.object(ended.termination, "inventory", return_value=[]))
        self.enterContext(patch.object(ended.termination.socket, "gethostname", return_value="source-host"))
        self.enterContext(patch.object(ended.closure, "closed_log", return_value=self.directory / "closed-sessions.json"))
        self.enterContext(patch.object(ended.closure, "action_pending", return_value=False))

        class Clock(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return datetime(2026, 10, 2, 0, 30, tzinfo=UTC)

        self.enterContext(patch.object(ended.termination, "datetime", Clock))
        self.save()
        self.attest()

    def save(self) -> None:
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in self.rows))
        os.utime(self.transcript, (1759276800, 1759276800))
        self.path.write_text(json.dumps(self.record))

    def attest(self) -> None:
        self.evidence["body"] = ended.termination.body(ended.termination.verify(self.record, "session-b"))

    def test_original_alias_exit_round_trips_and_releases_only_original_claim(self) -> None:
        original = dict(self.f.comments[0])
        self.run_release()
        receipt = release.receipt_for(self.f.comments[-1])
        self.assertEqual(receipt["source"], fixtures.fixtures.OTHER)
        self.assertEqual(receipt["termination"]["session_id"], self.native)
        self.assertEqual(self.f.comments[0], original)
        self.assertEqual(release.termination.evidence(self.evidence["body"], "session-b"), receipt["termination"])

    def test_incomplete_exit_copied_receipt_wrong_alias_and_future_activity_refuse(self) -> None:
        for mutation in ("typed_only", "copy", "alias", "time", "resumed", "spawned"):
            with self.subTest(mutation=mutation):
                rows = json.loads(json.dumps(self.rows))
                record = dict(self.record)
                if mutation == "typed_only":
                    rows.pop()
                if mutation == "copy":
                    block = rows[1]["message"]["content"][0]
                    block["content"] = json.dumps({"nested": json.loads(block["content"])})
                if mutation == "alias":
                    record["claim_session"] = "different-alias"
                if mutation == "time":
                    record["ended_at"] = "2026-10-01T00:10:02Z"
                if mutation == "resumed":
                    rows.append({**rows[-1], "timestamp": "2026-10-01T00:10:02Z"})
                if mutation == "spawned":
                    rows[0]["isSidechain"] = True
                self.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
                os.utime(self.transcript, (1759276800, 1759276800))
                with self.assertRaises(ValueError):
                    ended.termination.verify(record, "session-b")
        self.f.assert_no_writes()

    def test_second_account_copy_and_canonical_resume_veto_exit(self) -> None:
        second = self.directory / "second"
        second.mkdir()
        copy = second / self.transcript.name
        copy.write_text(self.transcript.read_text() + json.dumps({**self.rows[-1], "timestamp": "2026-10-01T00:11:00Z"}) + "\n")
        os.utime(copy, (1759276800, 1759276800))
        with self.assertRaises(fixtures.PLAN.PlanError):
            self.run_release()
        copy.unlink()
        (self.directory / "closed-sessions.json").write_text(
            json.dumps([{"session_id": self.native, "transcript": str(self.transcript), "resumed_at": "2026-10-01T00:12:00Z"}])
        )
        with self.assertRaises(fixtures.PLAN.PlanError):
            self.run_release()
        self.f.assert_no_writes()

    def test_live_and_uncertain_owner_and_pending_action_refuse(self) -> None:
        shared = ended.sessions.process_liveness()
        for command in ("claude --resume " + self.native, "claude --continue", "claude -r", "claude"):
            with (
                self.subTest(command=command),
                patch.object(ended.termination, "inventory", return_value=[(123, "ttys002", command)]),
                patch.object(shared, "process_arguments", return_value=(command.split(), True)),
                patch.object(shared, "process_started_at", return_value=release.stamp("2026-09-30T00:00:00Z")),
                self.assertRaises(fixtures.PLAN.PlanError),
            ):
                self.run_release()
        with patch.object(ended.closure, "action_pending", return_value=True), self.assertRaises(fixtures.PLAN.PlanError):
            self.run_release()
        self.f.assert_no_writes()

    def test_original_exit_and_claim_are_pinned_across_pending_write(self) -> None:
        def change() -> None:
            self.rows[-1]["timestamp"] = "2026-10-01T00:10:02Z"
            self.save()

        self.race = change
        with self.assertRaises(fixtures.PLAN.PlanError):
            self.run_release()
        self.assertFalse(any(release.receipt_for(comment) for comment in self.f.comments))
        self.assertNotIn("confirm_release", self.f.events)

    def test_recovered_legacy_uses_original_receipt_and_preserves_pr_proof(self) -> None:
        self.f.comments[0]["body"] = "Claimed by legacy-worker"
        self.args.legacy = True
        self.args.legacy_branch = fixtures.fixtures.OTHER["branch"]
        self.args.legacy_reason = "Original claim recorded no native session ID"
        claim = release.source_record(
            self.f.comments[0], legacy=True, legacy_branch=self.args.legacy_branch, legacy_reason=self.args.legacy_reason
        )
        self.record.update(
            claim_session=claim["session"], claim=claim, retained_artifacts=[{"branch": self.args.legacy_branch, "worktree": None}]
        )
        self.rows[0]["message"]["content"][0]["input"]["command"] = "gh-comment issue 42 --body-file claim.md"
        self.rows[1]["message"]["content"][0]["content"] = json.dumps(
            {"ok": True, "comment_action": "created", "comment": {"id": 1, "url": self.record["claim_url"]}}
        )
        self.save()
        self.evidence["body"] = ended.termination.body(ended.termination.verify(self.record, claim["session"]))
        self.args.dry_run = True
        with self.assertRaises(fixtures.PLAN.PlanError):
            self.run_release()
        self.f.assert_no_writes()
        self.f.pulls = [
            {
                "number": 99,
                "state": "open",
                "user": {"login": fixtures.BOT},
                "body": "Refs #42",
                "head": {"ref": self.args.legacy_branch, "sha": "legacy-head", "repo": {"full_name": "owner/repo"}},
                "base": {"repo": {"full_name": "owner/repo"}},
            }
        ]
        self.commit_date = "2026-10-01T00:05:00Z"
        self.run_release()
        body = self.f.emitted.call_args.args[0]["release_body"]
        self.assertIsNotNone(release.receipt_for({"body": body}))
        self.f.assert_no_writes()

    def test_codex_without_original_exit_and_changed_exit_receipt_refuse(self) -> None:
        record = {**self.record, "harness": "codex"}
        with self.assertRaises(ValueError):
            ended.termination.verify(record, "session-b")
        self.evidence["body"] = self.evidence["body"].replace('"exit_digest": "', '"exit_digest": "changed')
        with self.assertRaises(fixtures.PLAN.PlanError):
            self.run_release()
        self.f.assert_no_writes()


class ClaimReceiptBoundaryTests(unittest.TestCase):
    def test_replayed_or_quoted_receipts_do_not_bind_an_investigator(self) -> None:
        url = "https://github.com/owner/repo/issues/42#issuecomment-1"
        receipt = {"ok": True, "claim": fixtures.fixtures.OTHER, "claim_comment": {"comment": {"id": 1, "url": url}}}
        for output in (json.dumps(receipt), json.dumps({"payload": {"output": json.dumps(receipt)}})):
            rows = [
                {
                    "timestamp": "2026-10-03T00:00:00Z",
                    "payload": {"type": "function_call", "call_id": "read", "input": "rg 'gh-plan.py claim 42' source.jsonl"},
                },
                {
                    "timestamp": "2026-10-03T00:01:00Z",
                    "payload": {"type": "function_call_output", "call_id": "read", "output": output},
                },
            ]
            with self.subTest(output=output):
                self.assertFalse(ended.original_claim_output(rows, url, fixtures.fixtures.OTHER, created_at="2026-10-01T00:00:00Z"))

    def test_native_execution_receipt_requires_the_original_creation_interval(self) -> None:
        url = "https://github.com/owner/repo/issues/42#issuecomment-1"
        receipt = {"ok": True, "claim": fixtures.fixtures.OTHER, "claim_comment": {"comment": {"id": 1, "url": url}}}
        item = {
            "type": "CommandExecution",
            "command": ["sh", "-c", "gh-plan.py claim 42"],
            "status": "completed",
            "exit_code": 0,
            "stdout": json.dumps(receipt),
            "duration": {"secs": 10, "nanos": 0},
        }
        rows = [{"timestamp": "2026-10-01T00:00:10Z", "payload": {"item": item}}]
        self.assertTrue(ended.original_claim_output(rows, url, fixtures.fixtures.OTHER, created_at="2026-10-01T00:00:05Z"))
        self.assertFalse(ended.original_claim_output(rows, url, fixtures.fixtures.OTHER, created_at="2026-09-30T23:59:00Z"))
        self.assertFalse(ended.original_claim_output(rows, url, fixtures.fixtures.OTHER, created_at="2026-10-01T00:00:11Z"))

    def test_historical_process_inventory_refuses_unbound_resume_targets(self) -> None:
        shared = ended.sessions.process_liveness()
        source = {"session_id": "01a0fcce-ef3c-74e3-af74-6721809e4990", "_inventory": True}
        commands = {"codex": ("codex resume --last", "codex resume"), "claude": ("claude --continue", "claude -r")}
        for harness, invocations in commands.items():
            for command in invocations:
                with (
                    self.subTest(command=command),
                    patch.object(shared, "process_arguments", return_value=(command.split(), True)),
                    self.assertRaises(ValueError),
                ):
                    shared.verify_process_exit(
                        {**source, "harness": harness, "_historical_exit": True}, rows=[(1234, "ttys002", command)]
                    )


class NativeEndedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.entry = {"harness": "codex", "session_id": "01a0fcce-ef3c-74e3-af74-6721809e4990", "codex_home": "/account"}
        self.snapshots = {"/account/sessions/source.jsonl": {"status": {"turn_id": None}}}
        self.facts = {
            "id": "source-native",
            "path": "/account/sessions/source.jsonl",
            "state": "notLoaded",
            "queue": {},
            "background": [],
            "realtime": [],
            "goal": None,
            "turns": [],
            "executions": {"ended": True},
        }

        @asynccontextmanager
        async def connection(_home: Path) -> AsyncIterator[None]:
            yield None

        self.enterContext(patch.object(ended.native, "connection", connection))
        self.enterContext(patch.object(ended.sessions, "observation_homes", return_value=[Path("/account")]))
        self.enterContext(patch.object(ended.closure, "lease_state", return_value={}))
        self.read = self.enterContext(patch.object(ended.native, "read_thread_facts", new_callable=AsyncMock))
        self.read.return_value = self.facts

    async def test_native_absence_permits_the_independent_exit_check(self) -> None:
        await ended.verify_native(self.entry, self.snapshots)

    async def test_loaded_idle_codex_refuses_even_with_quiet_completed_history(self) -> None:
        self.facts.update(state="idle", turns=[{"id": "turn", "status": "completed"}])
        self.snapshots["/account/sessions/source.jsonl"]["status"].update(turn_id="turn", actual_turn_ids=["turn"])
        with self.assertRaises(ValueError):
            await ended.verify_native(self.entry, self.snapshots)

    async def test_active_queued_unknown_or_changed_native_owner_refuses(self) -> None:
        for changes in (
            {"state": "active"},
            {"state": "idle"},
            {"queue": {"queued": "work"}},
            {"state": "systemError"},
            {"path": "/different/source.jsonl"},
        ):
            with self.subTest(changes=changes):
                self.read.return_value = {**self.facts, **changes}
                with self.assertRaises(ValueError):
                    await ended.verify_native(self.entry, self.snapshots)
        self.read.side_effect = [self.facts, {**self.facts, "queue": {"new": "work"}}]
        with self.assertRaises(ValueError):
            await ended.verify_native(self.entry, self.snapshots)

    async def test_account_without_authenticated_source_does_not_receive_thread_reads(self) -> None:
        with patch.object(ended.sessions, "observation_homes", return_value=[Path("/account"), Path("/unrelated")]):
            await ended.verify_native(self.entry, self.snapshots)

    async def test_claude_idle_process_and_unavailable_inventory_refuse(self) -> None:
        account = {"provider": "anthropic", "env": {}}
        self.entry["harness"] = "claude"
        with patch.object(ended.sessions.native_helpers()[0], "load_config", return_value={"accounts": [account]}):
            for agents in ([{"sessionId": self.entry["session_id"], "status": "idle"}], {}, [{}]):
                with (
                    self.subTest(agents=agents),
                    patch.object(ended.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(agents))),
                    self.assertRaises(ValueError),
                ):
                    await ended.verify_native(self.entry, self.snapshots)
            with patch.object(ended.subprocess, "run", return_value=SimpleNamespace(stdout="[]")):
                await ended.verify_native(self.entry, self.snapshots)


if __name__ == "__main__":
    unittest.main()
