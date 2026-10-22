# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5", "PyYAML==6.0.3", "tree-sitter==0.26.0", "tree-sitter-bash==0.25.1"]
# ///
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

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
        self.observer = self.directory / "observer.jsonl"
        self.exit_row = json.loads((Path(__file__).parent / "fixtures/native-exit-command.json").read_text())
        self.exit_row["timestamp"] = "2026-10-01T00:10:01Z"
        self.exit_row["payload"].update(
            started_at_ms=int(release.stamp("2026-09-30T23:59:58Z").timestamp() * 1000),
            completed_at_ms=int(release.stamp("2026-10-01T00:10:01Z").timestamp() * 1000),
        )
        self.exit_row["payload"]["item"]["command"][2] = "claude --session-id " + self.native
        self.record = {
            "session_id": self.native,
            "claim_session": "session-b",
            "harness": "claude",
            "host": "source-host",
            "transcript": str(self.transcript),
            "transcript_roots": [str(self.directory)],
            "cause": "native local exit completed",
            "retained_artifacts": [],
            "exit_record": {
                "kind": "codex_command",
                "session_id": self.exit_row["payload"]["thread_id"],
                "transcript": str(self.observer),
                "execution_id": self.exit_row["payload"]["item"]["id"],
            },
            "claim": fixtures.fixtures.OTHER,
            "claim_url": "https://github.com/owner/repo/issues/42#issuecomment-1",
            "claim_created_at": self.f.comments[0]["created_at"],
        }

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
        ]
        self.path = self.directory / "termination.json"
        self.args.termination_record = self.path
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
        self.observer.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": self.record["exit_record"]["session_id"]}})
            + "\n"
            + json.dumps(self.exit_row)
            + "\n"
        )
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in self.rows))
        os.utime(self.transcript, (1759276800, 1759276800))
        self.path.write_text(json.dumps(self.record))

    def attest(self) -> None:
        self.evidence["body"] = ended.termination.body(ended.termination.verify(self.record, self.record["claim_session"]))

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
                    self.exit_row["payload"]["item"]["status"] = "in_progress"
                    self.save()
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
                self.exit_row["payload"]["item"]["status"] = "completed"
                self.save()
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
            self.exit_row["timestamp"] = "2026-10-01T00:10:02Z"
            self.exit_row["payload"]["completed_at_ms"] += 1000
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

    def test_subagent_and_partial_source_copy_only_veto_later_activity(self) -> None:
        child = self.directory / self.native / "subagents" / "agent-example.jsonl"
        child.parent.mkdir(parents=True)
        child.write_text(
            json.dumps(
                {
                    "sessionId": self.native,
                    "isSidechain": True,
                    "type": "assistant",
                    "timestamp": "2026-10-01T00:05:00Z",
                    "message": {"content": "done"},
                }
            )
            + "\n"
        )
        os.utime(child, (1759276800, 1759276800))
        self.attest()
        child.write_text(child.read_text().replace("00:05:00", "00:15:00"))
        os.utime(child, (1759276800, 1759276800))
        with self.assertRaises(ValueError):
            self.attest()

    def test_duplicate_original_receipt_in_another_native_session_refuses(self) -> None:
        other = "f5bd6d9f-d638-4a78-a6d1-ae9f8944beca"
        copied = self.directory / f"{other}.jsonl"
        copied.write_text("".join(json.dumps(row).replace(self.native, other) + "\n" for row in self.rows))
        os.utime(copied, (1759276800, 1759276800))
        with self.assertRaises(ValueError):
            self.attest()

    def test_explicit_other_process_started_after_exit_can_keep_working(self) -> None:
        shared = ended.sessions.process_liveness()
        command = "claude --resume f5bd6d9f-d638-4a78-a6d1-ae9f8944beca"
        with (
            patch.object(ended.termination, "inventory", return_value=[(123, "ttys002", command)]),
            patch.object(shared, "process_arguments", return_value=(command.split(), True)),
            patch.object(shared, "process_started_at", return_value=release.stamp("2026-10-01T00:20:00Z")),
        ):
            self.attest()

    def test_native_claim_also_requires_unique_receipt_and_fresh_source_binding(self) -> None:
        claim = {**self.record["claim"], "session": self.native}
        self.record.update(claim_session=self.native, claim=claim)
        self.f.comments[0]["body"] = fixtures.CLAIM.marker(claim)
        self.rows[1]["message"]["content"][0]["content"] = json.dumps(
            {"ok": True, "claim": claim, "claim_comment": {"comment": {"id": 1, "url": self.record["claim_url"]}}}
        )
        self.save()
        self.attest()
        self.args.dry_run = True
        self.run_release()
        copied = self.directory / "f5bd6d9f-d638-4a78-a6d1-ae9f8944beca.jsonl"
        copied.write_text("".join(json.dumps({**row, "sessionId": copied.stem}) + "\n" for row in self.rows))
        with self.assertRaises(ValueError):
            self.attest()
        copied.unlink()
        self.f.comments[0]["body"] = fixtures.CLAIM.marker({**claim, "worker": "changed-worker"})
        with self.assertRaises(fixtures.PLAN.PlanError):
            self.run_release()
        self.f.assert_no_writes()

    def test_native_completion_requires_successful_original_event_and_claim_attachment(self) -> None:
        original = json.loads(json.dumps(self.exit_row))
        for mutation in ("failure", "bool", "event", "observer", "missing", "duplicate", "before_claim", "wrong_source"):
            with self.subTest(mutation=mutation):
                self.exit_row = json.loads(json.dumps(original))
                payload = self.exit_row["payload"]
                item = payload["item"]
                if mutation == "failure":
                    item["exit_code"] = 1
                elif mutation == "bool":
                    item["exit_code"] = False
                elif mutation == "event":
                    payload["type"] = "tool_result"
                elif mutation == "observer":
                    payload["thread_id"] = self.native
                elif mutation == "missing":
                    item["id"] += "other"
                elif mutation == "before_claim":
                    payload["started_at_ms"] = int(release.stamp("2026-10-01T00:01:00Z").timestamp() * 1000)
                elif mutation == "wrong_source":
                    item["command"][2] = "claude --resume f5bd6d9f-d638-4a78-a6d1-ae9f8944beca"
                self.save()
                if mutation == "duplicate":
                    with self.observer.open("a") as stream:
                        stream.write(json.dumps(self.exit_row) + "\n")
                with self.assertRaises(ValueError):
                    self.attest()
        self.exit_row = original
        self.save()
        self.attest()

    def test_shell_status_cannot_substitute_for_source_exit(self) -> None:
        command = "claude --session-id " + self.native
        unsupported = [
            command + " &",
            command + "; true",
            command + " | cat",
            command + " && true",
            "echo $(" + command + ")",
            "timeout 2 " + command,
            command + " > out",
            command + " --fork-session",
            command + " --session-id " + self.native,
        ]
        for text in unsupported:
            with self.subTest(text=text):
                self.exit_row["payload"]["item"]["command"][2] = text
                self.save()
                with self.assertRaises(ValueError):
                    self.attest()

    def test_inherited_native_completion_cannot_end_a_source(self) -> None:
        meta = {"type": "session_meta", "payload": {"id": self.native}}
        self.observer.write_text(json.dumps(meta) + "\n" + json.dumps(self.exit_row) + "\n")
        with self.assertRaises(ValueError):
            self.attest()

    def test_plain_or_uncertain_or_preexisting_native_process_refuses(self) -> None:
        shared = ended.sessions.process_liveness()
        other = "claude --resume f5bd6d9f-d638-4a78-a6d1-ae9f8944beca"
        for command, bounded, born in (
            ("claude", True, "2026-10-01T00:20:00Z"),
            (other, False, "2026-10-01T00:20:00Z"),
            (other, True, "2026-09-30T00:00:00Z"),
        ):
            with (
                self.subTest(command=command, bounded=bounded, born=born),
                patch.object(ended.termination, "inventory", return_value=[(123, "ttys002", command)]),
                patch.object(shared, "process_arguments", return_value=(command.split(), bounded)),
                patch.object(shared, "process_started_at", return_value=release.stamp(born)),
                self.assertRaises(ValueError),
            ):
                self.attest()

    def test_unrelated_corrupt_history_does_not_supply_claim_or_block_verification(self) -> None:
        (self.directory / "unrelated.jsonl").write_bytes(b"\xffcorrupt unrelated history")
        self.attest()

    def test_codex_without_original_exit_and_changed_exit_receipt_refuse(self) -> None:
        record = {**self.record, "harness": "codex"}
        with self.assertRaises(ValueError):
            ended.termination.verify(record, "session-b")
        data = ended.termination.evidence(self.evidence["body"], "session-b")
        data["exit_digest"] = "0" * 64
        self.evidence["body"] = ended.termination.body(data)
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
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "read",
                                "name": "Bash",
                                "input": {"command": "rg 'gh-plan.py claim 42' source.jsonl"},
                            }
                        ]
                    },
                },
                {
                    "timestamp": "2026-10-03T00:01:00Z",
                    "message": {"content": [{"type": "tool_result", "tool_use_id": "read", "content": output}]},
                },
            ]
            with self.subTest(output=output):
                self.assertFalse(ended.original_claim_output(rows, url, fixtures.fixtures.OTHER, created_at="2026-10-01T00:00:00Z"))

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
                        {**source, "harness": harness},
                        rows=[(1234, "ttys002", command)],
                        ended_at=release.stamp("2026-10-01T00:00:00Z"),
                        started_at=lambda _pid: release.stamp("2026-10-02T00:00:00Z"),
                    )

    def test_independent_registry_cannot_clear_unresolved_resume(self) -> None:
        shared = ended.sessions.process_liveness()
        for command in ("claude --continue --fork-session", "claude -r", "codex resume --last"):
            with (
                self.subTest(command=command),
                patch.object(shared, "process_arguments", return_value=(command.split(), True)),
                self.assertRaises(ValueError),
            ):
                shared.verify_process_exit(
                    {"session_id": "01a0fcce-ef3c-74e3-af74-6721809e4990", "harness": command.split()[0], "_inventory": True},
                    rows=[(1234, "ttys002", command)],
                    ended_at=release.stamp("2026-10-01T00:00:00Z"),
                    independent_harness=lambda _pid, _argv: True,
                )

    def test_backfill_keeps_unrelated_plain_and_versioned_process_behavior(self) -> None:
        shared = ended.sessions.process_liveness()
        for command in ("claude", "/opt/claude/versions/2.0.0", "codex"):
            for bounded in (True, False):
                with (
                    self.subTest(command=command, bounded=bounded),
                    patch.object(shared, "process_arguments", return_value=([command], bounded)),
                ):
                    shared.verify_process_exit(
                        {"session_id": "01a0fcce-ef3c-74e3-af74-6721809e4990", "harness": "claude", "_inventory": True},
                        rows=[(1234, "ttys002", command)],
                    )

    def test_real_native_completion_fixture_supplies_original_exit(self) -> None:
        row = json.loads((Path(__file__).parent / "fixtures/native-exit-command.json").read_text())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "observer.jsonl"
            identity = row["payload"]["thread_id"]
            path.write_text(json.dumps({"type": "session_meta", "payload": {"id": identity}}) + "\n" + json.dumps(row) + "\n")
            source = "673145b1-3867-476f-993c-d484352a8004"
            record = {
                "session_id": source,
                "exit_record": {
                    "kind": "codex_command",
                    "session_id": identity,
                    "transcript": str(path),
                    "execution_id": row["payload"]["item"]["id"],
                },
            }
            evidence = ended.original_exit(record)
            self.assertLess(evidence.started_at, evidence.ended_at)
            self.assertLessEqual(evidence.ended_at, release.stamp(row["timestamp"]))


class HistoryCoverageTests(unittest.TestCase):
    def test_configured_accounts_are_required_and_unreadable_accounts_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            explicit, first, second = (root / name for name in ("explicit", "first", "second"))
            for path in (explicit, first / "projects", second / "projects"):
                path.mkdir(parents=True)
            accounts = [{"provider": "anthropic", "env": {"CLAUDE_CONFIG_DIR": str(home)}} for home in (first, second)]
            entry = {"transcript_roots": [str(explicit)], "harness": "claude"}
            with patch.object(ended.sessions.native_helpers()[0], "load_config", return_value={"accounts": accounts}):
                self.assertEqual(
                    ended.configured_roots(entry), {path.resolve() for path in (explicit, first / "projects", second / "projects")}
                )
                (second / "projects").rmdir()
                second.rmdir()
                with self.assertRaises(ValueError):
                    ended.configured_roots(entry)
                second.mkdir()
                (second / "projects").mkdir()
                original = ended.os.scandir

                def scan(path: object) -> object:
                    if Path(path) == second / "projects":
                        raise PermissionError("unreadable account")
                    return original(path)

                with patch.object(ended.os, "scandir", side_effect=scan), self.assertRaises(PermissionError):
                    ended.configured_roots(entry)


if __name__ == "__main__":
    unittest.main()
