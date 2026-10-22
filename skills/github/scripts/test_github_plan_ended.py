# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5", "PyYAML==6.0.3", "tree-sitter==0.26.0", "tree-sitter-bash==0.25.1"]
# ///
from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import github_plan_ended as ended
import github_plan_release as release
import test_gh_plan_release as fixtures


class EndedClaimTests(fixtures.ReleaseHarness):
    def setUp(self) -> None:
        super().setUp()
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.native = "01a0fcce-ef3c-74e3-af74-6721809e4990"
        self.transcript = self.directory / f"{self.native}.jsonl"
        self.rows = [
            {"type": "session_meta", "payload": {"id": self.native}, "timestamp": "2026-10-01T00:00:00Z"},
            {
                "type": "response_item",
                "timestamp": "2026-10-01T00:00:30Z",
                "payload": {
                    "type": "function_call",
                    "call_id": "claim",
                    "name": "exec_command",
                    "arguments": "gh-plan.py claim 42 --worker trial-b --session session-b",
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-10-01T00:01:00Z",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "claim",
                    "output": "https://github.com/owner/repo/issues/42#issuecomment-1",
                },
            },
        ]
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in self.rows))
        os.utime(self.transcript, (1759276800, 1759276800))
        self.log = self.directory / "closed-sessions.json"
        self.entry = {
            "session_id": self.native,
            "harness": "codex",
            "transcript": str(self.transcript),
            "transcript_roots": [str(self.directory)],
            "codex_home": str(self.directory),
            "pid": 123456,
            "tty": "ttys001",
            "host": socket.gethostname(),
            "ended_at": "2026-10-02T00:00:00Z",
            "process_exit_verified": True,
        }
        self.save_log()
        self.args.closed_log = self.log
        self.evidence["body"] = self.evidence["body"].replace("Safe to exit: yes", self.closure_lines())
        self.enterContext(patch.object(ended, "process_rows", return_value=[]))
        self.enterContext(patch.object(ended.sessions, "observation_homes", return_value=[self.directory]))
        self.native_check = self.enterContext(patch.object(ended, "verify_native", return_value=None))

    def closure_lines(self) -> str:
        return (
            f"Ownership release only: yes\nNative session: {self.native}\n"
            "Original claim: https://github.com/owner/repo/issues/42#issuecomment-1"
        )

    def save_log(self) -> None:
        self.log.write_text(json.dumps([self.entry]))
        self.log.chmod(0o600)

    def test_ended_alias_releases_without_rewriting_source_or_original_verdict(self) -> None:
        self.run_release()
        receipt = release.receipt_for(self.f.comments[-1])
        self.assertEqual(receipt["source"], fixtures.fixtures.OTHER)
        self.assertEqual(receipt["ended_session"], self.native)
        canonical = release.verified_comments(self.f.comments, lambda *_: self.evidence)
        conflicts, _ = fixtures.CLAIM.discussion_evidence("", canonical, fixtures.fixtures.OWNER)
        self.assertFalse(conflicts)
        self.assertGreaterEqual(self.native_check.call_count, 2)

    def test_live_or_uncertain_process_refuses_without_release_write(self) -> None:
        for rows in ([(self.entry["pid"], "ttys001", "codex")], [(123457, "ttys001", "codex")]):
            with (
                self.subTest(rows=rows),
                patch.object(ended, "process_rows", return_value=rows),
                patch.object(ended, "process_started_at", side_effect=ValueError("uncertain source")),
            ):
                with self.assertRaises(fixtures.PLAN.PlanError):
                    self.run_release()
                self.f.assert_no_writes()

    def test_incomplete_or_resumed_closure_refuses_without_write(self) -> None:
        for changes in (
            {"process_exit_verified": False},
            {"resumed_at": "2026-10-03T00:00:00Z"},
            {"session_id": "different"},
            {"host": "other-host"},
        ):
            with self.subTest(changes=changes):
                with patch.dict(self.entry, changes):
                    self.save_log()
                    with self.assertRaises(fixtures.PLAN.PlanError):
                        self.run_release()
                    self.f.assert_no_writes()

    def test_native_activity_refuses_even_after_original_process_exit(self) -> None:
        self.native_check.side_effect = ValueError("native queued work remains")
        with self.assertRaises(fixtures.PLAN.PlanError):
            self.run_release()
        self.f.assert_no_writes()

    def test_live_owner_on_readback_leaves_only_a_pending_record(self) -> None:
        self.race = lambda: setattr(self.native_check, "side_effect", ValueError("source resumed"))
        with self.assertRaises(fixtures.PLAN.PlanError):
            self.run_release()
        self.assertTrue(self.f.comments[-1]["body"].startswith("Pending release of claim"))
        self.assertIsNone(release.receipt_for(self.f.comments[-1]))

    def test_ended_legacy_claim_does_not_adopt_later_successor_branch_commits(self) -> None:
        self.f.comments[0]["body"] = "Claimed by legacy-worker"
        self.args.legacy = True
        self.args.legacy_branch = "work/original"
        self.args.legacy_reason = "Original bare claim; native identity recovered from its receipt"
        self.evidence["body"] = (
            f"Closed legacy claim 1\nWorker: legacy-worker\nSource branch: {self.args.legacy_branch}\n"
            f"Native session unavailable: {self.args.legacy_reason}\n"
            f"Ended at: {self.entry['ended_at']}\n{self.closure_lines()}"
        )
        self.source_branch_date = "2026-10-03T00:00:00Z"
        self.run_release()
        self.assertEqual(release.receipt_for(self.f.comments[-1])["ended_session"], self.native)

    def test_transcript_without_original_receipt_or_with_new_activity_refuses(self) -> None:
        original = self.transcript.read_text()
        for text in (
            original.replace("issuecomment-1", "issuecomment-2"),
            original.replace("2026-10-01T00:01:00Z", "2026-10-03T00:01:00Z"),
        ):
            with self.subTest(text=text):
                self.transcript.write_text(text)
                os.utime(self.transcript, (1759276800, 1759276800))
                with self.assertRaises(fixtures.PLAN.PlanError):
                    self.run_release()
                self.f.assert_no_writes()

    def test_missing_log_or_original_claim_binding_refuses(self) -> None:
        for mode in ("missing_log", "unbound"):
            with self.subTest(mode=mode):
                self.args.closed_log = None if mode == "missing_log" else self.log
                if mode == "unbound":
                    self.evidence["body"] = self.evidence["body"].replace("issuecomment-1", "issuecomment-2")
                with self.assertRaises(fixtures.PLAN.PlanError):
                    self.run_release()
                self.f.assert_no_writes()

    def test_verified_historical_exit_does_not_need_missing_terminal_coordinates(self) -> None:
        self.entry.pop("pid")
        self.entry.pop("tty")
        self.save_log()
        self.run_release()
        self.assertEqual(release.receipt_for(self.f.comments[-1])["ended_session"], self.native)

    def test_native_claim_receipts_bind_writes_rather_than_reads_or_quoted_examples(self) -> None:
        url = "https://github.com/owner/repo/issues/42#issuecomment-1"
        for kind in ("function_call", "custom_tool_call"):
            for command in ("gh-plan.py claim 42", "gh-plan.py --repo owner/repo claim 42"):
                with self.subTest(kind=kind, command=command):
                    rows = [
                        {"payload": {"type": kind, "call_id": "write", "input": command}},
                        {"payload": {"type": kind + "_output", "call_id": "write", "output": url}},
                    ]
                    self.assertTrue(ended.original_claim_output(rows, url))
                    rows[0]["payload"]["input"] = "gh-plan.py show 42"
                    self.assertFalse(ended.original_claim_output(rows, url))


class NativeEndedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.entry = {"harness": "codex", "session_id": "01a0fcce-ef3c-74e3-af74-6721809e4990", "codex_home": "/account"}
        self.snapshots = {"/account/source.jsonl": {"status": {"turn_id": None}}}
        self.facts = {
            "id": "source-native",
            "path": "/account/source.jsonl",
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

    async def test_current_native_absence_accepts_a_qualified_source_verdict(self) -> None:
        await ended.verify_native(self.entry, self.snapshots)
        self.assertEqual(self.read.await_count, 2)

    async def test_active_queued_unknown_or_changed_native_owner_refuses(self) -> None:
        for changes in (
            {"state": "active"},
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

    async def test_claude_idle_process_and_unavailable_inventory_refuse(self) -> None:
        account = {"provider": "anthropic", "env": {}}
        self.entry["harness"] = "claude"
        with patch.object(ended.sessions.native_helpers()[0], "load_config", return_value={"accounts": [account]}):
            for agents in ([{"sessionId": self.entry["session_id"], "status": "idle"}], {}, [{}]):
                with (
                    self.subTest(agents=agents),
                    patch.object(ended.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(agents))),
                ):
                    with self.assertRaises(ValueError):
                        await ended.verify_native(self.entry, self.snapshots)
            with patch.object(ended.subprocess, "run", return_value=SimpleNamespace(stdout="[]")):
                await ended.verify_native(self.entry, self.snapshots)


if __name__ == "__main__":
    unittest.main()
