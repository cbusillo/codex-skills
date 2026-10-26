#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise pilot parsers and terminal behavior with disposable transcripts/adapters."""

import argparse
import contextlib
import io
import asyncio
import errno
import json
import subprocess
import shlex
import socket
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import account_choice
import session_liveness
import codex_idle_watch
import finished_map
import iterm_tab
from codex_session_proxy import MEMORY_SETTING
import oq
import session_record
import status


def claude(text, **extra) -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": "2026-10-03T00:00:00Z",
        "message": {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn", **extra},
    }


def event(kind, **extra):
    return {
        "type": "event_msg",
        "timestamp": "2026-10-03T00:00:00Z",
        "payload": {"type": kind, **extra},
    }


class TranscriptTests(unittest.TestCase):
    def test_claude_exit_farewell_preserves_only_completed_closeout(self):
        closeout = "Done.\nSafe to exit: yes"
        prefix = [
            {"type": "user", "message": {"content": "Close out."}},
            claude(closeout),
        ]
        tail = [
            {"type": "user", "isMeta": True, "message": {"content":
                "<local-command-caveat>local command</local-command-caveat>"}},
            {"type": "user", "message": {"content":
                "<command-name>/exit</command-name>\n"
                "<command-message>exit</command-message>\n<command-args></command-args>"}},
            {"type": "user", "message": {"content":
                "<local-command-stdout>Catch you later!</local-command-stdout>"}},
        ]
        with tempfile.TemporaryDirectory() as folder:
            transcript = Path(folder) / "session.jsonl"
            entry = {"harness": "claude", "session_id": "fixture", "transcript": str(transcript)}

            def read(records):
                transcript.write_text("".join(
                    json.dumps({**record, "sessionId": "fixture"}) + "\n" for record in records
                ))
                return session_record.session_status(entry)

            for farewell in ("Catch you later!", "Goodbye!\nSee you soon.", "(no content)"):
                with self.subTest(farewell=farewell):
                    output = {"type": "user", "message": {"content":
                        f"<local-command-stdout>{farewell}</local-command-stdout>"}}
                    result = read(prefix + tail[:2] + [output])
                    self.assertTrue(result["safe_verdict"])
                    self.assertEqual(result["last_text"], closeout)
                    self.assertEqual(result["state"], "turn_ended")

            user = {"type": "user", "message": {"content": "Continue working."}}
            for records in (
                prefix + tail[1:],  # Missing the local-command caveat.
                prefix + tail[:2],  # Incomplete exit sequence.
                prefix + [tail[-1]],  # Standalone stdout is still a user turn.
                prefix + [tail[1], tail[0], tail[2]],
                prefix + [user] + tail,
                prefix + tail + [user],
                prefix + [claude("Continuing.", stop_reason="tool_use")] + tail,
                prefix + tail + [claude("Continuing.", stop_reason="tool_use")],
                prefix[:1] + [claude("Still working.")] + tail,
            ):
                with self.subTest(records=records):
                    self.assertFalse(read(records)["safe_verdict"])
            for stdout in (
                "Catch you later!",
                "<local-command-stdout>Goodbye!",  # Unclosed wrapper.
                tail[-1]["message"]["content"] + "\nContinue working.",
                tail[-1]["message"]["content"] * 2,
            ):
                with self.subTest(stdout=stdout):
                    output = {"type": "user", "message": {"content": stdout}}
                    self.assertFalse(read(prefix + tail[:2] + [output])["safe_verdict"])

    def test_claude_compaction_after_closeout_preserves_only_that_verdict(self):
        # Record shapes from an idle automatic compaction after a close-out (#2068).
        closeout = "Done.\nSafe to exit: yes"
        prefix = [{"type": "user", "message": {"content": "Close out."}}, claude(closeout)]
        compaction = [
            {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted",
             "compactMetadata": {"trigger": "auto"}},
            {"type": "user", "isCompactSummary": True, "isVisibleInTranscriptOnly": True,
             "message": {"content": "This session is being continued from a previous conversation."}},
            {"type": "attachment", "attachment": {"type": "hook_success", "hookName": "SessionStart:compact"}},
            {"type": "system", "subtype": "informational",
             "content": "Compacted while idle, before the prompt cache expired"},
        ]
        exit_tail = [
            {"type": "user", "isMeta": True, "message": {"content":
                "<local-command-caveat>local command</local-command-caveat>"}},
            {"type": "user", "message": {"content":
                "<command-name>/exit</command-name>\n"
                "<command-message>exit</command-message>\n<command-args></command-args>"}},
            {"type": "user", "message": {"content": "<local-command-stdout>Bye!</local-command-stdout>"}},
        ]
        for records in (prefix + compaction, prefix + compaction + exit_tail):
            with self.subTest(records=records):
                result = session_record.summarize(records, "claude")
                self.assertTrue(result["safe_verdict"])
                self.assertEqual(result["last_text"], closeout)
        summary_alone = compaction[1:]  # A summary record without its boundary.
        for records in (
            prefix + summary_alone,
            prefix + compaction + [{"type": "user", "message": {"content": "Continue working."}}],
            prefix + compaction + [claude("Picking the work back up.")],
            prefix + compaction + [claude("Continuing.", stop_reason="tool_use")] + exit_tail,
            prefix[:1] + [claude("Still working.")] + compaction,
        ):
            with self.subTest(records=records):
                self.assertFalse(session_record.summarize(records, "claude")["safe_verdict"])

    def test_status_recognizes_standalone_bold_final_verdict(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for harness, records in (
                ("claude", [{"sessionId": "fixture", **claude("Done.\n**Safe to exit: yes.**")}]),
                ("codex", [{"type": "session_meta", "payload": {"id": "fixture"}},
                           event("agent_message", message="Done.\n**Safe to exit: yes.**"),
                           event("task_complete")]),
            ):
                transcript = root / f"{harness}.jsonl"
                transcript.write_text("\n".join(json.dumps(record) for record in records))
                ledger = root / "ledger.json"
                ledger.write_text(json.dumps([{"harness": harness, "session_id": "fixture",
                                               "transcript": str(transcript)}]))
                with self.subTest(harness=harness):
                    self.assertTrue(status.snapshot(ledger)[0]["safe_verdict"])

    def test_claude_turn_end_reports_background_tasks_still_running(self):
        # Record shapes from CS1812-D1: a background watcher, then "Waiting on the CI watcher".
        started = [
            {"type": "assistant", "timestamp": "2026-10-16T04:23:35Z", "message": {"content": [
                {"type": "tool_use", "id": "toolu_watch", "name": "Bash",
                 "input": {"command": "gh_pr_watch.py --pr 1963 --watch", "run_in_background": True}}]}},
            {"type": "user", "timestamp": "2026-10-16T04:23:40Z",
             "toolUseResult": {"stdout": "", "backgroundTaskId": "bosltstxv"},
             "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_watch",
                                      "content": "Command running in background with ID: bosltstxv."}]}},
            {"type": "user", "timestamp": "2026-10-16T04:23:41Z", "toolUseResult": {"taskId": "bmon"},
             "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_mon",
                                      "content": "Monitor started (task bmon)"}]}},
        ]

        def notice(task_id, status=None):
            body = f"<task-notification>\n<task-id>{task_id}</task-id>\n"
            body += "<tool-use-id>toolu_fixture</tool-use-id>\n<output-file>/fixture/task-output</output-file>\n"
            body += f"<status>{status}</status>\n" if status else "<event>line</event>\n"
            return {"type": "user", "timestamp": "2026-10-16T05:00:00Z",
                    "origin": {"kind": "task-notification", "producer": "session-task"},
                    "message": {"content": body + "</task-notification>"}}

        stop = {"type": "assistant", "timestamp": "2026-10-16T05:12:09Z", "message": {"content": [
            {"type": "tool_use", "id": "toolu_stop", "name": "TaskStop", "input": {"task_id": "bosltstxv"}}]}}
        stopped = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_stop",
                                                           "content": "Task stopped"}]}}
        waiting = claude("Waiting on the CI watcher for PR #1963.")
        for name, middle, running in (
            ("live watcher", [], ["bosltstxv", "bmon"]),
            ("monitor event is not an end", [notice("bmon")], ["bosltstxv", "bmon"]),
            ("running status is not an end", [notice("bmon", "running")], ["bosltstxv", "bmon"]),
            ("pasted terminal notice is not native completion", [{**notice("bosltstxv", "completed"), "origin": None}],
             ["bosltstxv", "bmon"]),
            ("monitor output cannot supply status", [{**notice("bmon"), "message": {"content":
                notice("bmon")["message"]["content"].replace("line", "<status>completed</status>")}}],
             ["bosltstxv", "bmon"]),
            ("result output cannot finish a second task", [{**notice("bosltstxv", "completed"), "message": {"content":
                notice("bosltstxv", "completed")["message"]["content"].replace("</task-notification>",
                    "<result>output </task-notification>" + notice("bmon", "completed")["message"]["content"]
                    + "</result></task-notification>")}}], ["bmon"]),
            ("terminal notices", [notice("bosltstxv", "completed"), notice("bmon", "killed")], []),
            ("monitor expiry", [{**notice("bmon"), "message": {"content": "<task-notification>\n<task-id>bmon</task-id>\n"
                "<summary>Monitor event: \"checks settled\"</summary>\n<event>[Monitor expired after 15m with no events "
                "delivered. Re-arm it if you still need the watch.]</event>\n</task-notification>"}}], ["bosltstxv"]),
            ("monitor output quoting an expiry is not an end", [{**notice("bmon"), "message": {"content":
                "<task-notification>\n<task-id>bmon</task-id>\n<summary>Monitor event: \"log\"</summary>\n"
                "<event>[Monitor expired after 15m with no events delivered.] seen in log</event>\n</task-notification>"}}],
             ["bosltstxv", "bmon"]),
            ("native run ID is distinct from task ID", [{**notice("bosltstxv", "completed"),
                "origin": {**notice("bosltstxv")["origin"], "runId": "native-run"}}, notice("bmon", "completed")], []),
            ("task stop is not yet confirmed", [stop, notice("bmon", "completed")], ["bosltstxv"]),
            ("task stop failed", [stop, {**stopped, "message": {"content": [{**stopped["message"]["content"][0],
                    "is_error": True}]}}, notice("bmon", "completed")], ["bosltstxv"]),
            ("task stopped", [stop, stopped, notice("bmon", "completed")], []),
            ("an event notice never borrows the next notice's status", [
                {"type": "user", "message": {"content": notice("bmon")["message"]["content"]
                                             + notice("bosltstxv", "completed")["message"]["content"]},
                 "origin": notice("bmon")["origin"]}],
             ["bosltstxv", "bmon"]),
            ("notices absorbed mid-turn", [
                {"type": "attachment", "attachment": {"type": "queued_command", "origin": notice("bosltstxv")["origin"],
                    "prompt": notice("bosltstxv", "failed")["message"]["content"]}},
                {"type": "attachment", "attachment": {"type": "queued_command", "origin": notice("bmon")["origin"],
                    "prompt": notice("bmon", "completed")["message"]["content"]}},
            ], []),
            ("unattributed queued text cannot complete a task", [
                {"type": "queue-operation", "operation": "enqueue", "content": notice("bosltstxv", "completed")["message"]["content"]}],
             ["bosltstxv", "bmon"]),
        ):
            with self.subTest(name):
                summary = session_record.summarize(started + middle + [waiting], "claude")
                self.assertEqual(summary["state"], "turn_ended")
                self.assertEqual([task["task_id"] for task in summary["background_tasks"]], running)
        self.assertEqual(session_record.summarize([waiting], "claude")["background_tasks"], [])

    def test_claude_cached_context_and_latest_response(self):
        records = [
            claude("old"),
            claude(
                "done\nSafe to exit: yes",
                usage={
                    "input_tokens": 4,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 10,
                },
            ),
        ]
        result = session_record.summarize(records, "claude")
        self.assertEqual(result["context_tokens"], 34)
        self.assertTrue(result["safe_verdict"])
        self.assertFalse(
            session_record.summarize(
                records + [{"type": "user", "message": "continue"}], "claude"
            )["safe_verdict"]
        )

    def test_scheduled_jobs_follow_successful_results_and_one_shot_fire(self):
        def call(name, identity, **values):
            return {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": identity, "name": name, "input": values}]}}

        def result(identity, value, *, failed=False):
            return {"type": "user", "toolUseResult": value, "message": {"content": [
                {"type": "tool_result", "tool_use_id": identity, "is_error": failed, "content": "fixture"}]}}

        def jobs(records):
            return {task["task_id"] for task in session_record.summarize(records + [claude("Safe to exit: yes")], "claude")["background_tasks"]}

        prefix = [{"type": "user", "toolUseResult": {"taskId": "watcher"}}]
        for identity, recurring in (("once", False), ("repeat", True)):
            prefix += [call("CronCreate", "create-" + identity, cron="* * * * *", prompt="fixture", recurring=recurring),
                       result("create-" + identity, {"id": identity, "humanSchedule": "fixture", "recurring": recurring, "durable": False})]
        self.assertEqual(jobs(prefix), {"watcher", "once", "repeat"})
        fire = [{"type": "system", "subtype": "scheduled_task_fire", "taskId": "once"},
                {"type": "user", "scheduledTaskId": "once", "scheduledFireId": "fire", "turnOrigin": "scheduled"},
                {"type": "system", "subtype": "scheduled_task_fire", "taskId": "repeat"}]
        self.assertEqual(jobs(prefix + fire[:1]), {"watcher", "repeat"})
        self.assertEqual(jobs(prefix + fire[1:2]), {"watcher", "repeat"})
        self.assertEqual(jobs(prefix + fire[2:]), {"watcher", "once", "repeat"})
        self.assertEqual(jobs(prefix + fire), {"watcher", "repeat"})
        self.assertEqual(jobs(prefix + fire + prefix[1:]), {"watcher", "repeat"})
        deleting = prefix + [call("CronDelete", "delete", id="repeat")]
        self.assertEqual(jobs(deleting + [result("delete", {"id": "repeat"}, failed=True)]), {"watcher", "once", "repeat"})
        self.assertEqual(jobs(deleting + [result("delete", {"id": "different"})]), {"watcher", "once", "repeat"})
        self.assertEqual(jobs(deleting + [result("delete", {"id": "repeat"})]), {"watcher", "once"})
        listing = prefix + [call("CronList", "list")]
        self.assertEqual(jobs(listing + [result("list", {"jobs": []})]), {"watcher"})
        self.assertEqual(jobs(listing + [result("list", {"jobs": "unknown"})]), {"watcher", "once", "repeat"})
        ambiguous = result("list", {"jobs": []})
        ambiguous["message"]["content"].append({"type": "tool_result", "tool_use_id": "other", "content": "fixture"})
        self.assertEqual(jobs(listing + [ambiguous]), {"watcher", "once", "repeat"})

    def test_negative_quoted_and_conditional_verdicts(self):
        for text in (
            "> Safe to exit: yes",
            'I quoted "Safe to exit: yes"',
            "Safe to exit: yes, if CI passes",
            "Safe to exit: conditional",
            "Safe to exit: no",
            "Safe to exit: yes — if CI passes",
            "```text\nSafe to exit: yes",
            "Safe to exit: yes\nStill doing work",
        ):
            with self.subTest(text=text):
                self.assertFalse(
                    session_record.summarize([claude(text)], "claude")["safe_verdict"]
                )

    def test_cron_list_reconciles_unknown_placeholder_and_finished_candidacy(self):
        records = [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "create", "name": "CronCreate", "input": {}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "create", "content": "replayed text"}]}},
        ]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            transcript, ledger = root / "native.jsonl", root / "ledger.json"
            ledger.write_text(json.dumps([{"session_id": "source", "harness": "claude", "supervisor_owned": True, "transcript": "native.jsonl"}]))
            def candidate():
                transcript.write_text("".join(json.dumps({**r, "sessionId": "source"}) + "\n" for r in [*records, claude("Safe to exit: yes")]))
                return finished_map.candidates(ledger)[0]
            before = candidate()
            self.assertFalse(before["candidate"])
            self.assertIsNone(before["closeout_request"])
            self.assertEqual([task["task_id"] for task in before["closeout_blockers"]], ["cron-create:create"])
            records += [
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "list", "name": "CronList", "input": {}}]}},
                {"type": "user", "toolUseResult": {"jobs": []}, "message": {"content": [{"type": "tool_result", "tool_use_id": "list", "content": "No jobs"}]}},
            ]
            after = candidate()
            self.assertEqual(after["background_tasks"], [])
            self.assertTrue(after["candidate"])

    def test_standard_closeout_bullets_and_bold_labels(self):
        for text in (
            "- Safe to exit: yes",
            "- **Safe to exit:** yes",
            "* **Safe to exit: yes**",
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    session_record.summarize([claude(text)], "claude")["safe_verdict"]
                )
        for text in (
            "> - **Safe to exit:** yes",
            "- **Safe to exit:** yes, if CI passes",
        ):
            with self.subTest(text=text):
                self.assertFalse(
                    session_record.summarize([claude(text)], "claude")["safe_verdict"]
                )

    def test_tool_use_after_verdict_invalidates_it(self):
        record = claude("Safe to exit: yes")
        record["message"]["content"].append({"type": "tool_use", "name": "Bash"})
        self.assertFalse(session_record.summarize([record], "claude")["safe_verdict"])

    def test_claude_split_records_require_terminal_stop_reason(self):
        for reason in ("tool_use", "max_tokens", "stop_sequence", None):
            with self.subTest(stop_reason=reason), tempfile.TemporaryDirectory() as folder:
                transcript = Path(folder) / "session.jsonl"
                ledger = Path(folder) / "ledger.json"
                entry = {"session_id": "fixture", "harness": "claude",
                         "transcript": str(transcript), "supervisor_owned": True}
                ledger.write_text(json.dumps([entry]))
                records: list[dict[str, Any]] = [{"sessionId": "fixture", **claude("Safe to exit: yes", stop_reason=reason)}]
                transcript.write_text("\n".join(json.dumps(r) for r in records))
                summary = session_record.summarize(records, "claude")
                self.assertFalse(summary["at_turn_end"])
                self.assertFalse(finished_map.candidates(ledger)[0]["candidate"])
                records.append(claude("", stop_reason="tool_use"))
                records[-1]["message"]["content"] = [{"type": "tool_use", "name": "Bash"}]
                transcript.write_text("\n".join(json.dumps(r) for r in records))
                self.assertFalse(finished_map.candidates(ledger)[0]["candidate"])
                transcript.write_text(json.dumps({"sessionId": entry["session_id"], **claude("Safe to exit: yes")}))
                self.assertTrue(finished_map.candidates(ledger)[0]["candidate"])

    def test_claude_missing_stop_reason_is_not_completion_evidence(self):
        record = claude("Safe to exit: yes")
        del record["message"]["stop_reason"]
        self.assertFalse(session_record.summarize([record], "claude")["safe_verdict"])

    def test_same_line_closeout_prefaces(self):
        for text in (
            "No Director action needed. Safe to exit: yes.",
            "No Director decision needed. **Safe to exit: yes.**",
        ):
            for harness, records in (
                ("claude", [claude(text)]),
                ("codex", [event("agent_message", message=text), event("task_complete")]),
            ):
                with self.subTest(text=text, harness=harness):
                    result = session_record.summarize(records, harness)
                    self.assertTrue(result["safe_verdict"])
                    self.assertEqual(result["verdict_recognition"], "recognized")
                    self.assertEqual(result["last_text"], text)
        for text in (
            'No Director action needed. "Safe to exit: yes."',
            "> No Director action needed. Safe to exit: yes.",
            "Not No Director action needed. Safe to exit: yes.",
            "If CI passes: No Director action needed. Safe to exit: yes.",
            "No Director action needed. Safe to exit: yes for this check only.",
            "No Director decision needed. Safe to exit: yes, if CI passes.",
            "No Director action needed. Safe to exit: yes. Still working.",
            "Other final prose. Safe to exit: yes.",
        ):
            with self.subTest(text=text):
                result = session_record.summarize([claude(text)], "claude")
                self.assertFalse(result["safe_verdict"])
                self.assertEqual(result["verdict_recognition"], "unrecognized")
        self.assertEqual(session_record.summarize([claude("Working")], "claude")
                         ["verdict_recognition"], "absent")

    def test_same_line_verdict_retains_citation_and_activity_guards(self):
        fixture = session_record.read_jsonl(
            Path(__file__).parent / "fixtures/codex-closeout-memory-citation.jsonl"
        )
        response = fixture[1]["payload"]["content"][0]["text"]
        citation = response[response.index("<oai-mem-citation>"):]
        text = "No Director action needed. Safe to exit: yes.\n\n" + citation
        records = [event("agent_message", message=text), event("task_complete")]
        self.assertTrue(session_record.summarize(records, "codex")["safe_verdict"])
        for tail in (event("turn_aborted"), event("task_started"),
                     event("user_message", message="continue"), event("function_call")):
            with self.subTest(tail=tail):
                self.assertFalse(session_record.summarize(records + [tail], "codex")
                                 ["safe_verdict"])
        for suffix in (citation + "Still working", citation + citation,
                       citation.replace("</oai-mem-citation>", "")):
            with self.subTest(suffix=suffix):
                self.assertFalse(session_record.summarize(
                    [event("agent_message", message="No Director action needed. "
                           "Safe to exit: yes.\n\n" + suffix), event("task_complete")],
                    "codex")["safe_verdict"])

    def test_terminal_verdict_outside_completed_fences(self):
        for text in (
            "Verified with:\n```sh\nuv run test_supervisor.py\n```\nSafe to exit: yes",
            "~~~sh\ncommand\n~~~\n**Safe to exit: yes.**",
            "````sh\n```\n`````\nSafe to exit: yes",
            "Done.\n   ~~~sh\ncommand\n   ~~~~  \nSafe to exit: yes",
            "```sh\n~~~\n```\n~~~text\nhello\n~~~\nSafe to exit: yes",
            "- ```sh\n  command\n  ```\nSafe to exit: yes",
            "Done.\n  - ```sh\n    command\n    ```\nSafe to exit: yes",
            "1. Verified with:\n    ```sh\n    command\n    ```\nSafe to exit: yes",
            "- Verified with:\n\t```sh\n\tcommand\n\t```\nSafe to exit: yes",
            "> ```sh\n> command\n> ```\nSafe to exit: yes",
        ):
            for harness, records in (
                ("claude", [claude(text)]),
                ("codex", [event("agent_message", message=text), event("task_complete")]),
            ):
                with self.subTest(text=text, harness=harness):
                    self.assertTrue(session_record.summarize(records, harness)["safe_verdict"])
        for text in (
            "```text\nSafe to exit: yes\n```",
            "~~~text\nSafe to exit: yes",
            "````text\n```\nSafe to exit: yes",
            "```sh\n~~~\nSafe to exit: yes",
            "```sh\n``` trailing text\nSafe to exit: yes",
            "```text\n    ```\nSafe to exit: yes",
            "```text\n\t```\nSafe to exit: yes",
            "- ```text\nSafe to exit: yes",
            "1. ~~~text\nSafe to exit: yes",
            "- ```sh\ncommand\n```\nSafe to exit: yes",
            "- Verified:\n  ```sh\n  command\n```\nSafe to exit: yes",
            "- Verified:\n    ```sh\n      ```\nSafe to exit: yes",
            "Output:\n    Safe to exit: yes",
            "Output:\n\tSafe to exit: yes",
            "> ```sh\n> command\n> ```\n```\nSafe to exit: yes",
            "- Outer\n  - Inner\n  ```sh\n  command\n```\nSafe to exit: yes",
            "- - Inner\n  ```sh\n  command\n```\nSafe to exit: yes",
            "Output:\n \tSafe to exit: yes",
            "```sh\ncommand\n```\n> Safe to exit: yes",
            "~~~sh\ncommand\n~~~\nNot Safe to exit: yes",
            "```sh\ncommand\n```\nSafe to exit: yes if CI passes",
            "```sh\ncommand\n```\nSafe to exit: yes for this check only",
        ):
            with self.subTest(text=text):
                self.assertFalse(session_record.summarize([claude(text)], "claude")["safe_verdict"])

    def test_codex_turn_end_abort_and_resume(self):
        records = [
            event("task_started"),
            event("agent_message", message="Safe to exit: yes"),
            event("task_complete"),
        ]
        self.assertTrue(session_record.summarize(records, "codex")["safe_verdict"])
        for tail in (
            event("turn_aborted"),
            event("task_started"),
            event("user_message", message="continue"),
        ):
            self.assertFalse(
                session_record.summarize(records + [tail], "codex")["safe_verdict"]
            )

    def test_memory_citation_tail_preserves_verdict_constraints(self):
        records = session_record.read_jsonl(
            Path(__file__).parent / "fixtures/codex-closeout-memory-citation.jsonl"
        )
        response = records[1]["payload"]["content"][0]["text"]
        citation = response[response.index("<oai-mem-citation>"):]
        self.assertTrue(session_record.summarize(records, "codex")["safe_verdict"])
        for text in (
            "> Safe to exit: yes",
            '"Safe to exit: yes"',
            "Not Safe to exit: yes",
            "Safe to exit: no",
            "Safe to exit: yes, if CI passes",
            "If CI passes:\n  Safe to exit: yes, conditional",
            "```text\nSafe to exit: yes",
            "Safe to exit: yes\nStill doing work",
        ):
            with self.subTest(text=text):
                self.assertFalse(session_record.summarize(
                    [event("agent_message", message=text + "\n\n" + citation),
                     event("task_complete")], "codex"
                )["safe_verdict"])
        for suffix in (
            citation + "Still doing work",
            citation.replace("</oai-mem-citation>", ""),
            citation.replace("<oai-mem-citation>", "> <oai-mem-citation>"),
            citation + citation,
        ):
            with self.subTest(suffix=suffix):
                self.assertFalse(session_record.summarize(
                    [event("agent_message", message="Safe to exit: yes\n\n" + suffix),
                     event("task_complete")], "codex"
                )["safe_verdict"])
        self.assertFalse(session_record.summarize(
            [event("agent_message", message=citation), event("task_complete")],
            "codex"
        )["safe_verdict"])
        for tail in (event("turn_aborted"), event("task_started"),
                     event("user_message", message="continue"), event("function_call")):
            with self.subTest(tail=tail):
                self.assertFalse(session_record.summarize(records + [tail], "codex")["safe_verdict"])

    def test_codex_response_item_text_and_usage(self):
        records = [
            event(
                "token_count",
                info={
                    "last_token_usage": {"input_tokens": 12},
                    "total_token_usage": {"total_tokens": 100},
                },
            ),
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Safe to exit: yes"}],
                },
            },
            event("task_complete"),
        ]
        result = session_record.summarize(records, "codex")
        self.assertTrue(result["safe_verdict"])
        self.assertEqual(result["context_tokens"], 12)
        self.assertEqual(result["total_tokens"], 100)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transcript = self.root / "session.jsonl"
        self.ledger = self.root / "ledger.json"
        self.entry = {
            "session_id": "02b123-new-prefix",
            "harness": "codex",
            "transcript": "session.jsonl",
            "supervisor_owned": True,
            "pid": 123,
            "tty": "ttys001",
            "iterm_session_id": "term1",
            "handoff_url": "https://github.com/example/repo/issues/1#issuecomment-2",
        }
        self.write(
            [
                event("agent_message", message="Safe to exit: yes"),
                event("task_complete"),
            ]
        )

    def write(self, records):
        records = [
            {"type": "session_meta", "payload": {"id": self.entry["session_id"]}}
        ] + records
        self.transcript.write_text(
            "".join(json.dumps(record) + "\n" for record in records)
        )
        self.ledger.write_text(json.dumps([self.entry]))
        os.utime(self.transcript, (1000, 1000))

    def test_claude_local_exit_tail_allows_exited_tab_close(self):
        self.entry["harness"] = "claude"
        records = [
            claude("Safe to exit: yes"),
            {
                "type": "user",
                "isMeta": True,
                "message": {
                    "content": "<local-command-caveat>local command</local-command-caveat>"
                },
            },
            {
                "type": "user",
                "message": {
                    "content": "<command-name>/exit</command-name>\n<command-message>exit</command-message>\n<command-args></command-args>"
                },
            },
            {
                "type": "user",
                "message": {
                    "content": "<local-command-stdout>(no content)</local-command-stdout>"
                },
            },
        ]
        for record in records:
            record["sessionId"] = self.entry["session_id"]
        self.write(records)
        for kind in ("/compact", "/clear"):
            changed = records.copy()
            changed[-2] = {
                "type": "user",
                "message": {"content": f"<command-name>{kind}</command-name>"},
            }
            self.assertFalse(
                session_record.summarize(changed, "claude")["safe_verdict"]
            )
        self.assertFalse(
            session_record.summarize(
                records + [{"type": "user", "message": {"content": "continue"}}],
                "claude",
            )["safe_verdict"]
        )

    def test_bad_watched_transcript_does_not_hide_healthy_session(self):
        bad = {**self.entry, "session_id": "bad", "transcript": "missing.jsonl"}
        self.ledger.write_text(json.dumps([bad, self.entry]))
        seen = {}
        notices = codex_idle_watch.poll(self.ledger, seen, 90, 1200)
        self.assertIn("error", notices[0])
        self.assertEqual(notices[1]["turn_end"], "task_complete")
        self.assertEqual(codex_idle_watch.poll(self.ledger, seen, 90, 1200), [])

    def test_malformed_nested_transcripts_are_isolated(self):
        bad_path = self.root / "bad.jsonl"
        for malformed in (
            {"type": "event_msg", "payload": ["unexpected"]},
            {"type": "event_msg", "payload": []},
            {"type": "event_msg", "payload": {"type": []}},
            {"type": "assistant", "message": "unexpected"},
            {"type": "assistant", "message": {"content": ["unexpected"]}},
            {"type": "assistant", "message": {"content": [{"text": []}]}},
            {"type": "assistant", "message": {"usage": []}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": []}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": ["unexpected"]}}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": []}},
            {"type": "session_meta", "payload": {"id": []}},
            {"type": "assistant", "sessionId": []},
        ):
            with self.subTest(malformed=malformed):
                bad_path.write_text("\n".join(json.dumps(r) for r in (
                    {"type": "session_meta", "payload": {"id": "bad"}}, malformed,
                    event("agent_message", message="Safe to exit: yes"),
                    event("task_complete"))) + "\n")
                bad = {**self.entry, "session_id": "bad", "transcript": "bad.jsonl"}
                self.ledger.write_text(json.dumps([bad, self.entry]))
                for consumer in (status.snapshot, finished_map.candidates,
                                 lambda ledger: codex_idle_watch.poll(ledger, {}, 90, 1200)):
                    results = consumer(self.ledger)
                    self.assertIn("error", results[0])
                    self.assertFalse(results[0].get("safe_verdict", False))
                    self.assertFalse(results[0].get("candidate", False))
                    self.assertEqual(results[1]["session_id"], self.entry["session_id"])
                    self.assertEqual(results[1]["turn_end"], "task_complete")

    def test_malformed_ledger_remains_a_top_level_error(self):
        self.ledger.write_text(json.dumps({"sessions": [self.entry]}))
        for consumer in (status.snapshot, finished_map.candidates,
                         lambda ledger: codex_idle_watch.poll(ledger, {}, 90, 1200)):
            with self.assertRaises(TypeError):
                consumer(self.ledger)

    def test_watch_mixed_ledger_reports_skips_and_keeps_codex_notices(self):
        others = [{"session_id": "gemini", "harness": "agy"},
                  {"session_id": "claude-tmux", "harness": "claude", "backend": "tmux"}]
        for other in others:
            with self.subTest(other=other):
                self.ledger.write_text(json.dumps([other, self.entry]))
                seen = {}
                results = codex_idle_watch.poll(self.ledger, seen, 90, 1200)
                skipped = next(r for r in results if r["session_id"] == other["session_id"])
                self.assertTrue(skipped["skipped"])
                self.assertIn("reason", skipped)
                self.assertIsNone(skipped["clean"])
                notice = next(r for r in results if r["session_id"] == self.entry["session_id"])
                self.assertEqual(notice["turn_end"], "task_complete")
                self.assertEqual(codex_idle_watch.poll(self.ledger, seen, 90, 1200), [])
        self.ledger.write_text(json.dumps([*others, self.entry]))
        results = codex_idle_watch.poll(self.ledger, {}, 90, 1200)
        self.assertEqual(len(results), len(others) + 1)
        # Shared readers that decide completion/closure retain strict validation.
        with self.assertRaises(ValueError):
            session_record.load_ledger(self.ledger)

    def test_watch_still_validates_selected_codex_entries(self):
        for bad in ({"harness": "codex", "session_id": "missing-transcript"},
                    {**self.entry, "session_id": ""}, self.entry):
            with self.subTest(bad=bad):
                self.ledger.write_text(json.dumps([
                    {"harness": "agy", "session_id": "gemini"}, bad, self.entry]))
                with self.assertRaises(ValueError):
                    codex_idle_watch.poll(self.ledger, {}, 90, 1200)

    def test_native_agent_message_without_message_field_stays_readable(self):
        self.write([event("reasoning", content=None), event("token_count", info=None),
                    event("token_count", info={"last_token_usage": None, "total_token_usage": None}),
                    event("agent_message", content=[{"type": "text", "text": "done"}]),
                    event("task_complete")])
        result = status.snapshot(self.ledger)[0]
        self.assertNotIn("error", result)
        self.assertFalse(result["safe_verdict"])

    def test_unknown_account_transcript_is_invalid_input_in_normalizer_and_loader(self):
        entry = {**self.entry, "transcript": "~q526-absent-account/rollout.jsonl"}
        self.ledger.write_text(json.dumps([entry]))
        before = self.ledger.read_bytes()
        for normalize in (lambda: session_record.normalize_ledger_entry(self.ledger, entry),
                          lambda: session_record.load_ledger(self.ledger)):
            with self.subTest(consumer=normalize), patch("pwd.getpwnam", side_effect=KeyError("fixture account absent")):
                with self.assertRaises(ValueError):
                    normalize()
                self.assertEqual(self.ledger.read_bytes(), before)

    def test_relative_path_and_duplicate_identity(self):
        self.assertEqual(
            session_record.load_ledger(self.ledger)[0]["transcript"],
            str(self.transcript.resolve()),
        )
        self.ledger.write_text(json.dumps([self.entry, self.entry]))
        with self.assertRaises(ValueError):
            session_record.load_ledger(self.ledger)

    def test_wrong_transcript_identity_is_not_a_candidate(self):
        self.entry["session_id"] = "another-thread"
        self.ledger.write_text(json.dumps([self.entry]))
        self.assertFalse(finished_map.candidates(self.ledger)[0]["candidate"])

    def test_iterm_tty_path_is_normalized_for_process_matching(self):
        self.entry["tty"] = "/dev/ttys001"
        self.ledger.write_text(json.dumps([self.entry]))
        self.assertEqual(session_record.load_ledger(self.ledger)[0]["tty"], "ttys001")

    def test_partial_write_is_error_not_finished(self):
        self.transcript.write_text("{unfinished")
        self.assertIn("error", status.snapshot(self.ledger)[0])
        self.assertFalse(finished_map.candidates(self.ledger)[0]["candidate"])
        self.assertIn("error", codex_idle_watch.poll(self.ledger, {}, 90, 1200)[0])

    def test_owned_candidate_and_director_session(self):
        self.assertTrue(finished_map.candidates(self.ledger)[0]["candidate"])
        self.entry["supervisor_owned"] = False
        self.ledger.write_text(json.dumps([self.entry]))
        self.assertFalse(finished_map.candidates(self.ledger)[0]["candidate"])

    def test_watch_dedup_resume_and_abort_notice(self):
        seen = {}
        self.write([event("agent_message", message="Waiting for the next step."),
                    event("task_complete")])
        self.assertEqual(codex_idle_watch.poll(self.ledger, seen, 90, 1050), [])
        notice = codex_idle_watch.poll(self.ledger, seen, 90, 1200)
        self.assertEqual(notice[0]["session_id"], self.entry["session_id"])
        self.assertIsNone(notice[0]["clean"])
        self.assertIsNone(notice[0]["closeout_request"])
        self.assertEqual(codex_idle_watch.poll(self.ledger, seen, 90, 1200), [])
        self.write([event("task_complete"), event("task_started")])
        self.assertEqual(codex_idle_watch.poll(self.ledger, seen, 90, 1200), [])
        self.write([event("task_started"), event("turn_aborted")])
        self.assertEqual(
            codex_idle_watch.poll(self.ledger, {}, 90, 1200)[0]["turn_end"],
            "turn_aborted",
        )

    def test_finished_notice_routes_immediately_without_idle_delay(self):
        seen = {}
        notices = codex_idle_watch.poll(self.ledger, seen, 90, 1001)
        self.assertEqual(len(notices), 1)
        notice = notices[0]
        request = notice["closeout_request"]
        self.assertEqual(request, finished_map.candidates(self.ledger)[0]["closeout_request"])
        self.assertEqual(request["session_id"], self.entry["session_id"])
        self.assertEqual(request["handoff_url"], self.entry["handoff_url"])
        self.assertEqual(request.get("verdict_at"), finished_map.candidates(self.ledger)[0]["timestamp"])
        self.assertIsNone(notice["clean"])
        self.assertEqual(codex_idle_watch.poll(self.ledger, seen, 90, 1002), [])
        # The notice is historical evidence: fresh verification must reject new work.
        self.write([event("agent_message", message="Safe to exit: yes"),
                    event("task_complete"), event("task_started")])
        self.assertFalse(finished_map.candidates(self.ledger)[0]["candidate"])
        self.assertEqual(codex_idle_watch.poll(self.ledger, seen, 90, 1200), [])

    def test_closeout_metadata_does_not_repeat_request_but_later_verdict_does(self):
        seen = {}
        self.assertEqual(len(codex_idle_watch.poll(self.ledger, seen, 90, 1001)), 1)
        with self.transcript.open("a") as stream:
            stream.write(json.dumps(event("token_count", info={"total_token_usage": {"total_tokens": 99}})) + "\n")
        os.utime(self.transcript, (1002, 1002))
        self.assertEqual(codex_idle_watch.poll(self.ledger, seen, 90, 1003), [])
        # A whole new turn can finish between polls with the same final text.
        with self.transcript.open("a") as stream:
            for record in [event("task_started"), event("agent_message", message="Safe to exit: yes"),
                           {**event("task_complete"), "timestamp": "2026-10-03T00:01:00Z"}]:
                stream.write(json.dumps(record) + "\n")
        os.utime(self.transcript, (1004, 1004))
        self.assertEqual(len(codex_idle_watch.poll(self.ledger, seen, 90, 1005)), 1)

    def test_observed_new_work_rearms_closeout_with_same_response(self):
        seen = {}
        self.assertEqual(len(codex_idle_watch.poll(self.ledger, seen, 90, 1001)), 1)
        self.write([event("task_started")])
        self.assertEqual(codex_idle_watch.poll(self.ledger, seen, 90, 1002), [])
        self.write([event("agent_message", message="Safe to exit: yes"), event("task_complete")])
        self.assertEqual(len(codex_idle_watch.poll(self.ledger, seen, 90, 1003)), 1)

    def test_early_closeout_requires_owned_completed_unqualified_response(self):
        for text, tail, owned in (
            ("Safe to exit: yes", "task_complete", False),
            ("Still working.", "task_complete", True),
            ('"Safe to exit: yes"', "task_complete", True),
            ("Safe to exit: yes, when CI passes", "task_complete", True),
            ("Safe to exit: yes", "turn_aborted", True),
            ("Safe to exit: yes", "task_started", True),
        ):
            with self.subTest(text=text, tail=tail, owned=owned):
                self.entry["supervisor_owned"] = owned
                self.write([event("agent_message", message=text), event(tail)])
                self.assertEqual(codex_idle_watch.poll(self.ledger, {}, 90, 1001), [])
                self.assertIsNone(finished_map.candidates(self.ledger)[0].get("closeout_request"))

    def test_notice_rechecks_changed_ledger_ownership(self):
        seen = {}
        self.entry["supervisor_owned"] = False
        self.ledger.write_text(json.dumps([self.entry]))
        self.assertIsNone(codex_idle_watch.poll(self.ledger, seen, 90, 1200)[0].get("closeout_request"))
        self.entry["supervisor_owned"] = True
        self.ledger.write_text(json.dumps([self.entry]))
        notices = codex_idle_watch.poll(self.ledger, seen, 90, 1201)
        self.assertEqual(len(notices), 1)
        self.assertIsNotNone(notices[0]["closeout_request"])

    def watch_args(self, **overrides) -> SimpleNamespace:
        return SimpleNamespace(**{
            "ledger": self.ledger, "idle_seconds": 90, "interval": 45, "deadline_seconds": 1800,
            "once": False, "capacity_state": None, "resume_capacity": False, "harness": "codex",
            "delivered_state": self.root / "delivered.json", **overrides})

    def test_watch_exits_on_closeout_request_so_background_caller_wakes(self):
        self.write([event("agent_message", message="Still working."), event("task_started")])
        sleeps = []

        def worker_finishes_between_polls(seconds: float) -> None:
            sleeps.append(seconds)
            self.assertEqual(len(sleeps), 1, "watch kept polling after a closeout request")
            self.write([event("agent_message", message="Safe to exit: yes"), event("task_complete")])

        output = io.StringIO()
        with patch.object(codex_idle_watch.time, "sleep", side_effect=worker_finishes_between_polls), \
                contextlib.redirect_stdout(output):
            self.assertEqual(codex_idle_watch.watch(self.watch_args(), None), 0)
        notices = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(notices[-1]["closeout_request"]["session_id"], self.entry["session_id"])

    def run_watch_to_deadline(self) -> tuple[int, list[float], list[dict]]:
        clock = iter(range(0, 10_000, 45))
        sleeps = []
        output = io.StringIO()
        with patch.object(codex_idle_watch.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(codex_idle_watch.time, "sleep", side_effect=sleeps.append), \
                contextlib.redirect_stdout(output):
            result = codex_idle_watch.watch(self.watch_args(deadline_seconds=180), None)
        return result, sleeps, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_watch_wakes_once_per_closeout_request_across_rearms(self):
        result, sleeps, notices = self.run_watch_to_deadline()
        self.assertEqual((result, sleeps), (0, []))
        self.assertIsNotNone(notices[0]["closeout_request"])
        result, sleeps, notices = self.run_watch_to_deadline()
        self.assertEqual(result, 0)
        self.assertGreater(len(sleeps), 1)
        self.assertEqual(len(notices), 1)
        self.write([event("agent_message", message="Safe to exit: yes"),
                    {**event("task_complete"), "timestamp": "2026-10-03T00:05:00Z"}])
        _, sleeps, _ = self.run_watch_to_deadline()
        self.assertEqual(sleeps, [])

    def test_watch_selects_claude_sessions_for_closeout(self):
        self.entry["harness"] = "claude"
        self.transcript.write_text(json.dumps(
            {**claude("Safe to exit: yes"), "sessionId": self.entry["session_id"]}) + "\n")
        self.ledger.write_text(json.dumps([self.entry]))
        self.assertTrue(codex_idle_watch.poll(self.ledger, {}, 90, 1001)[0]["skipped"])
        notice = codex_idle_watch.poll(self.ledger, {}, 90, 1001, harness="claude")[0]
        self.assertEqual(notice["closeout_request"]["harness"], "claude")

    def test_claude_native_notice_uses_exact_session_and_shared_route(self):
        self.entry["harness"] = "claude"
        records = [{**claude("Safe to exit: yes"), "sessionId": self.entry["session_id"]}]
        self.transcript.write_text("".join(json.dumps(r) + "\n" for r in records))
        self.ledger.write_text(json.dumps([self.entry]))
        command = [sys.executable, str(Path(finished_map.__file__)),
                   "--ledger", str(self.ledger), "--session-id", self.entry["session_id"]]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        candidate = json.loads(result.stdout)[0]
        self.assertTrue(candidate["candidate"])
        self.assertEqual(candidate["closeout_request"]["harness"], self.entry["harness"])
        # A native idle notice arriving after a new user turn is no longer finish evidence.
        with self.transcript.open("a") as stream:
            stream.write(json.dumps({"type": "user", "sessionId": self.entry["session_id"],
                                     "message": {"content": "Continue working."}}) + "\n")
        self.assertIsNone(finished_map.candidates(self.ledger, self.entry["session_id"])[0]["closeout_request"])
        result = subprocess.run(command[:-1] + ["other-session"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("error", json.loads(result.stdout))

    def test_claude_closeout_with_live_background_task_stays_open(self):
        self.entry["harness"] = "claude"
        records = [
            {"type": "user", "toolUseResult": {"taskId": "running-ci"}},
            claude("Safe to exit: yes"),
        ]
        self.transcript.write_text("".join(json.dumps({**r, "sessionId": self.entry["session_id"]}) + "\n"
                                          for r in records))
        self.ledger.write_text(json.dumps([self.entry]))
        candidate = finished_map.candidates(self.ledger)[0]
        self.assertFalse(candidate["candidate"])
        self.assertIsNone(candidate["closeout_request"])
        self.assertEqual(candidate.get("closeout_blockers"), candidate["background_tasks"])
        self.assertTrue(candidate["closeout_blockers"])
        self.assertTrue(candidate["next_action"])


    def test_memory_citation_fixture_through_status_candidates_and_close(self):
        fixture = Path(__file__).parent / "fixtures/codex-closeout-memory-citation.jsonl"
        for preface in (
            "", "No Director action needed. ", "No Director decision needed. ",
            "Verified with:\n```sh\ncommand\n```\n",
            "Verified with:\n~~~sh\ncommand\n~~~\nNo Director action needed. ",
            "Verified with:\n```sh\ncommand\n```\nNo Director decision needed. ",
        ):
            with self.subTest(preface=preface):
                records = session_record.read_jsonl(fixture)
                text = records[1]["payload"]["content"][0]["text"]
                records[1]["payload"]["content"][0]["text"] = text.replace(
                    "Safe to exit: yes", preface + "Safe to exit: yes"
                )
                self.transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
                result = status.snapshot(self.ledger)[0]
                self.assertTrue(result["safe_verdict"])
                self.assertIn("<oai-mem-citation>", result["last_text"])
                self.assertTrue(finished_map.candidates(self.ledger)[0]["candidate"])




class QuestionTests(unittest.TestCase):
    def test_both_spellings_preserve_answer_authority_and_question_link(self):
        for question_role in ("Director", "Owner"):
            for decision_role in ("Director", "Owner"):
                with self.subTest(question=question_role, decision=decision_role):
                    question = {
                        "id": 123,
                        "url": "https://example.test/questions/123",
                        "body": f"**{question_role} question:** proceed?",
                        "author": "bot",
                    }
                    answers = [
                        {"author": "outsider", "body": f"{decision_role} decision: yes 123", "url": "foreign"},
                        {"author": "bot", "body": f"{decision_role} decision: yes 1234", "url": "unlinked"},
                        {"author": "bot", "body": "yes 123", "url": "unrecorded"},
                        {"author": "bot", "body": "Director decision needed: see 123", "url": "needed"},
                        {"author": "bot", "body": "Director decisions still open: 123", "url": "open"},
                    ]
                    # Even the Director's linked follow-up question is not an answer.
                    answers.extend(
                        {
                            "id": comment_id,
                            "author": "director",
                            "body": f"{role} question: about 123?",
                            "url": role,
                        }
                        for comment_id, role in enumerate(("Director", "Owner"), start=124)
                    )
                    self.assertEqual(
                        oq.questions([question, *answers], "director", {"bot"})[0]["status"],
                        "needs_review",
                    )
                    for body in (
                        f"**{decision_role} decision:** yes 123",
                        f"{decision_role} decision, recorded: {question['url']}",
                        f"**{decision_role} decision** on {question['url']}: yes",
                        f"{decision_role} decision (recorded in chat): yes 123",
                    ):
                        answer = {"author": "bot", "body": body, "url": "recorded"}
                        result = oq.questions([question, *answers, answer], "director", {"bot"})[0]
                        self.assertEqual(result["answers"], ["recorded"])
                        self.assertEqual(result["status"], "answered")
                    direct = {"author": "director", "body": "yes 123", "url": "direct"}
                    self.assertEqual(
                        oq.questions([question, direct], "director", {"bot"})[0]["answers"],
                        ["direct"],
                    )

    def test_all_questions_and_only_linked_authorized_answers(self):
        q1 = {
            "id": 1,
            "url": "https://example.test/1",
            "author": "bot",
            "body": "Owner question: first?",
        }
        q2 = {
            "id": 2,
            "url": "https://example.test/2",
            "author": "bot",
            "body": "Owner question: second?",
        }
        comments = [
            q1,
            q2,
            {
                "author": "outsider",
                "body": "Owner decision https://example.test/1",
                "url": "a",
            },
            {"author": "owner", "body": "Unrelated reply", "url": "b"},
            {
                "author": "bot",
                "body": "**Owner decision** on https://example.test/2: yes",
                "url": "c",
            },
        ]
        result = oq.questions(comments, "owner", {"bot"})
        self.assertEqual([q["status"] for q in result], ["needs_review", "answered"])
        self.assertEqual(result[1]["answers"], ["c"])

    def test_bold_question_heading_is_included(self):
        comment = {
            "id": 1,
            "url": "https://example.test/1",
            "body": "**Owner question:** proceed?",
            "author": "bot",
        }
        self.assertEqual(len(oq.questions([comment], "owner", {"bot"})), 1)

    def test_paged_helper_failure_never_means_no_questions(self):
        with (
            patch.object(
                oq.subprocess,
                "run",
                return_value=SimpleNamespace(stdout=json.dumps({"ok": False})),
            ),
            self.assertRaises(ValueError),
        ):
            oq.fetch("example/repo#1")
        with self.assertRaises(ValueError):
            oq.fetch("repo#1")

    def test_complete_cached_discussion_preserves_question_inventory(self):
        comments = [{"id": 1, "url": "question", "author": "bot",
                     "body": "Owner question: proceed?"},
                    {"id": 2, "url": "answer", "author": "owner", "body": "question yes"}]
        envelope = {"ok": True, "outcome_certainty": "confirmed", "disposition": "complete",
                    "exit_code": 0, "recommended_next_action": "none", "issue": {"comments": comments}}
        for certainty in ("confirmed", "not_applicable"):
            with self.subTest(certainty=certainty), patch.object(oq.subprocess, "run",
                return_value=SimpleNamespace(stdout=json.dumps({**envelope, "outcome_certainty": certainty,
                    "recommended_next_action": "inspect_last_failure" if certainty == "not_applicable" else "none"}))):
                fetched = oq.fetch("example/repo#1")
                self.assertEqual(fetched, comments)
                self.assertEqual(oq.questions(fetched, "owner", {"bot"})[0]["status"], "answered")
        for override in ({"ok": False}, {"outcome_certainty": "unknown"},
                         {"disposition": "stop"}, {"exit_code": 1},
                         {"issue": {"comments": {}}}):
            with self.subTest(override=override), patch.object(oq.subprocess, "run",
                return_value=SimpleNamespace(stdout=json.dumps({**envelope, **override}))), self.assertRaises(ValueError):
                oq.fetch("example/repo#1")


NOW = datetime(2026, 10, 3, 16, tzinfo=timezone.utc)
ACCOUNTS_TOML = """
[accounts]
snapshot_command = ["context-panel-snapshot"]
reserve = 0.05

[[accounts.account]]
name = "main"
provider = "anthropic"
context_panel_label = "Main"
env = { CLAUDE_CONFIG_DIR = "~/claude-main" }
reserve = 0.2

[[accounts.account]]
name = "spare"
provider = "anthropic"
context_panel_configuration_id = "cfg-spare"
env = { CLAUDE_CONFIG_DIR = "/accounts/spare dir" }
"""


def account_row(label, remaining, resets, state="available", cfg="cfg"):
    """resets: (five-hour, weekly) reset times, or one unlabeled window's."""
    labels = ["Claude 5-hour", "Claude weekly"] if len(resets) == 2 else ["Claude"]
    return {
        "provider": "anthropic",
        "label": label,
        "configurationID": cfg,
        "id": "anthropic-def" if cfg == "cfg-spare" else "anthropic-abc",
        "state": state,
        "remainingFraction": remaining,
        "windows": [
            {"label": name, "naturalResetAt": reset} for name, reset in zip(labels, resets)
        ],
    }


def accounts_config(text=ACCOUNTS_TOML):
    return account_choice.validate_config(
        __import__("tomllib").loads(text)["accounts"]
    )


GOOGLE_TOML = """
[accounts]
snapshot_command = ["/Applications/Context Panel.app/Contents/MacOS/Context Panel", "--account-snapshot"]

[[accounts.account]]
name = "agy-login"
provider = "google"
context_panel_label = "Gemini"
single_login = true
env = {}
"""


def google_snapshot():
    row = {**account_row("Gemini", .5, []), "provider": "google", "id": "google-abc"}
    return {"schemaVersion": 1, "accounts": [row],
            "answers": {"useNext": [{"provider": row["provider"], "accountID": row["id"]}]},
            "ranking": [{"provider": row["provider"], "launchOrder": [row["id"], row["id"]]}]}


def choose_from(rows, recommendations=None, **extra):
    snapshot = {"schemaVersion": 1, "accounts": rows}
    if recommendations is not None:
        snapshot["answers"] = {"useNext": recommendations}
    return account_choice.choose(
        "anthropic", accounts_config(), snapshot, None, now=NOW, **extra
    )


class AccountChoiceTests(unittest.TestCase):
    @staticmethod
    def snapshot(provider="anthropic", order=None, stale=False, age: float = 0) -> dict[str, Any]:
        rows = [account_row("main", .01, []), account_row("spare", .04, [], cfg="cfg-spare")]
        for row in rows:
            row["provider"] = provider
            row["id"] = row["id"].replace("anthropic", provider)
        order = order if order is not None else [rows[1]["id"], rows[0]["id"], rows[1]["id"]]
        return {"schemaVersion": 1, "accounts": rows,
                "answers": {"useNext": [] if stale else [{"provider": provider, "accountID": rows[1]["id"]}]},
                "ranking": [{"provider": provider, "launchOrder": order,
                             "basedOnStaleReadings": stale,
                             "readingsObservedAt": (NOW - timedelta(minutes=age)).isoformat()}]}

    def test_use_next_is_authoritative_despite_config_reserves_order_and_row_state(self):
        for provider in account_choice.ACCOUNT_VARIABLES:
            config = accounts_config()
            for account in config["accounts"]:
                account["provider"] = provider
            snapshot = self.snapshot(provider)
            snapshot["accounts"][1].update(state="limited", useLast=True)
            # The tightest (model-only) window may be empty while CP ranks the main window.
            snapshot["accounts"][1]["remainingFraction"] = 0
            choice = account_choice.choose(provider, config, snapshot, None, now=NOW)
            self.assertEqual((choice["name"], choice["source"]), ("spare", "context-panel"))
            self.assertEqual(choice["account_id"], provider + "-def")
            config["accounts"].reverse()
            self.assertEqual(account_choice.choose(provider, config, snapshot, None, now=NOW)["name"], "spare")

    def test_batch_follows_published_order_including_repeated_accounts(self):
        snapshot = self.snapshot()
        choices = account_choice.choose_batch("anthropic", accounts_config(), snapshot, None, count=3, now=NOW)
        self.assertEqual([c["name"] for c in choices], ["spare", "main", "spare"])
        with self.assertRaisesRegex(ValueError, "smaller batch"):
            account_choice.choose_batch("anthropic", accounts_config(), snapshot, None, count=4, now=NOW)

    def test_stale_list_uses_observation_age_and_refuses_at_thirty_minutes(self):
        for age in (0, 29.99):
            snapshot = self.snapshot(stale=True, age=age)
            choice = account_choice.choose("anthropic", accounts_config(), snapshot, None, now=NOW)
            self.assertEqual((choice["name"], choice["source"]), ("spare", "context-panel-stale"))
        for age in (30, 31, -1):
            with self.subTest(age=age), self.assertRaisesRegex(ValueError, "not under 30 minutes"):
                account_choice.choose("anthropic", accounts_config(), self.snapshot(stale=True, age=age), None, now=NOW)
        snapshot = self.snapshot(stale=True)
        snapshot["ranking"][0].pop("readingsObservedAt")
        with self.assertRaisesRegex(ValueError, "not under 30 minutes"):
            account_choice.choose("anthropic", accounts_config(), snapshot, None, now=NOW)

    def test_no_choice_refuses_and_reports_next_capacity_without_private_fallback(self):
        snapshot = self.snapshot(order=[])
        snapshot["answers"]["useNext"] = []
        snapshot["ranking"][0]["nextCapacityAt"] = "2026-10-04T03:00:00Z"
        with self.assertRaisesRegex(ValueError, "next capacity at 2026-10-04T03:00:00Z"):
            account_choice.choose("anthropic", accounts_config(), snapshot, None, now=NOW)
        with self.assertRaisesRegex(ValueError, "snapshot unavailable.*exited 1"):
            account_choice.choose("anthropic", accounts_config(), None, "snapshot command exited 1")

    def test_ambiguous_or_unconfigured_choices_refuse_without_substitution(self):
        for change, error in (("missing", "single account row"), ("unconfigured", "single configured account"),
                              ("duplicates", "ambiguous"), ("rowduplicates", "single account row")):
            snapshot = self.snapshot()
            if change == "missing":
                snapshot["answers"]["useNext"][0]["accountID"] = "anthropic-123"
            elif change == "unconfigured":
                snapshot["accounts"][1]["configurationID"] = "unknown"
            elif change == "duplicates":
                snapshot["answers"]["useNext"] *= 2
            else:
                snapshot["accounts"].append(dict(snapshot["accounts"][1]))
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, error):
                account_choice.choose("anthropic", accounts_config(), snapshot, None, now=NOW)

    def test_explicit_account_overrides_ranking_but_needs_snapshot_receipt_id(self):
        choice = account_choice.choose("anthropic", accounts_config(), self.snapshot(), None, now=NOW, name="main")
        self.assertEqual((choice["name"], choice["source"], choice["account_id"]), ("main", "named", "anthropic-abc"))
        with self.assertRaisesRegex(ValueError, "snapshot unavailable"):
            account_choice.choose("anthropic", accounts_config(), None, "reader failed", name="main")
        with self.assertRaisesRegex(ValueError, "named missing"):
            account_choice.choose("anthropic", accounts_config(), self.snapshot(), None, name="missing")

    def test_configuration_errors_and_plain_reset_lines_reported(self):
        snapshot = self.snapshot()
        snapshot["configurationErrors"] = ["multipleUseLast:anthropic"]
        snapshot["resetPrompts"] = [{"provider": "anthropic", "accountID": "anthropic-abc", "line": "Apply main's reset now"}]
        choice = choose_from(snapshot["accounts"], snapshot["answers"]["useNext"])
        self.assertEqual(choice["name"], "spare")
        choice = account_choice.choose("anthropic", accounts_config(), snapshot, None, now=NOW)
        self.assertEqual(choice["configuration_errors"], snapshot["configurationErrors"])
        self.assertEqual(choice["reset_prompts"], ["Apply main's reset now"])
        snapshot["answers"]["useNext"] = []
        with self.assertRaisesRegex(ValueError, "(?s)no anthropic choice.*multipleUseLast.*Apply main"):
            account_choice.choose("anthropic", accounts_config(), snapshot, None, now=NOW)

    @staticmethod
    def windowed(snapshot, limiting, five_hour, weekly, weekly_burn):
        row = snapshot["accounts"][1]
        row.update(remainingFraction=min(five_hour, weekly), limitingWindowLabel=limiting, windows=[
            {"id": "anthropic-5h", "label": "Claude 5-hour", "period": "fiveHour", "remainingFraction": five_hour,
             "naturalResetAt": "2026-10-03T05:00:00Z", "burn": None},
            {"id": "anthropic-1w", "label": "Claude weekly", "period": "weekly", "remainingFraction": weekly,
             "naturalResetAt": "2026-10-09T00:00:00Z",
             "burn": {"unitsPerHour": weekly_burn, "observedDurationHours": 12, "sampleCount": 8}}])
        return snapshot

    def test_report_keeps_five_hour_and_weekly_capacity_separate(self):
        snapshot = self.windowed(self.snapshot(), "5-hour", .63, .89, 1.5)
        choice = account_choice.choose("anthropic", accounts_config(), snapshot, None, now=NOW)
        self.assertEqual(choice["name"], "spare")
        self.assertEqual((choice["remaining_fraction"], choice["limiting_window_label"]), (.63, "5-hour"))
        five_hour, weekly = choice["window_capacity"]["five_hour"], choice["window_capacity"]["weekly"]
        self.assertEqual([w["remaining_fraction"] for w in five_hour], [.63])
        self.assertEqual([w["remaining_fraction"] for w in weekly], [.89])
        self.assertEqual(account_choice.public(choice)["window_capacity"], choice["window_capacity"])

    def test_switching_limiting_window_never_becomes_a_weekly_burn_sample(self):
        readings = [self.windowed(self.snapshot(), "5-hour", .30, .89, 1.5),
                    self.windowed(self.snapshot(), "Weekly", .95, .88, 1.5)]
        choices = [account_choice.choose("anthropic", accounts_config(), s, None, now=NOW) for s in readings]
        self.assertEqual([c["limiting_window_label"] for c in choices], ["5-hour", "Weekly"])
        weekly = [c["window_capacity"]["weekly"][0] for c in choices]
        self.assertEqual([w["remaining_fraction"] for w in weekly], [.89, .88])
        self.assertEqual([w["burn_units_per_hour"] for w in weekly], [1.5, 1.5])
        self.assertIsNone(choices[0]["window_capacity"]["five_hour"][0]["burn_units_per_hour"])

    def test_model_windows_sharing_a_period_are_all_retained(self):
        snapshot = self.windowed(self.snapshot(), "Weekly", .9, .4, 1.0)
        snapshot["accounts"][1]["windows"].append(
            {"id": "anthropic-opus", "label": "Opus weekly", "period": "weekly", "remainingFraction": .7})
        choice = account_choice.choose("anthropic", accounts_config(), snapshot, None, now=NOW)
        self.assertEqual([w["label"] for w in choice["window_capacity"]["weekly"]], ["Claude weekly", "Opus weekly"])

    def test_snapshots_without_window_periods_report_explicit_unknowns(self):
        snapshot = self.snapshot()
        snapshot["accounts"][1]["windows"] = [{"label": "Claude weekly", "naturalResetAt": "2026-10-09T00:00:00Z"}]
        choice = account_choice.choose("anthropic", accounts_config(), snapshot, None, now=NOW)
        self.assertEqual((choice["name"], choice["remaining_fraction"]), ("spare", .04))
        self.assertIsNone(choice["limiting_window_label"])
        self.assertEqual(choice["window_capacity"], {"five_hour": None, "weekly": None})
        snapshot["accounts"][1]["windows"] = [{"label": "Odd", "period": {}}, {"label": "Daily", "period": "daily"}]
        choice = account_choice.choose("anthropic", accounts_config(), snapshot, None, now=NOW)
        self.assertEqual((choice["name"], choice["window_capacity"]), ("spare", {"five_hour": [], "weekly": []}))

    def test_snapshot_read_failures_mean_unavailable(self):
        for stdout, code, reason in (("{", 0, "not JSON"), ('{"schemaVersion":2}', 0, "version 1"), ("", 1, "exited 1")):
            with self.subTest(reason=reason):
                snapshot, problem = account_choice.read_snapshot(["reader"], runner=Mock(return_value=SimpleNamespace(stdout=stdout, returncode=code)))
                self.assertIsNone(snapshot)
                self.assertIn(reason, problem)
        snapshot, problem = account_choice.read_snapshot(["reader"], runner=Mock(side_effect=FileNotFoundError()))
        self.assertIsNone(snapshot)
        self.assertIn("could not run", problem)

    def test_private_config_location_legacy_reserves_ignored_and_env_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "skill-data/supervisor.toml"
            path.parent.mkdir()
            path.write_text(ACCOUNTS_TOML)
            config = account_choice.load_config(env={"CODE_HOME": folder}, home=Path("/nowhere"))
            self.assertEqual([a["name"] for a in config["accounts"]], ["main", "spare"])
            self.assertTrue(all("reserve" not in a for a in config["accounts"]))
            for text, error in ((ACCOUNTS_TOML.replace('context_panel_label = "Main"', ''), "exactly one"),
                                (ACCOUNTS_TOML.replace('provider = "anthropic"', 'provider = "openai"'), "must set CODEX_HOME"),
                                (ACCOUNTS_TOML.replace('CLAUDE_CONFIG_DIR = "~/claude-main"', 'CLAUDE_CONFIG_DIR = false'), "env must map")):
                path.write_text(text)
                with self.assertRaisesRegex(ValueError, error):
                    account_choice.load_config(env={"CODE_HOME": folder}, home=Path("/nowhere"))

    def test_default_claude_profile_has_no_env_values(self):
        config = accounts_config(ACCOUNTS_TOML.replace('env = { CLAUDE_CONFIG_DIR = "~/claude-main" }', 'env = {}'))
        for name in (None, "main"):
            snapshot = self.snapshot()
            snapshot["answers"]["useNext"][0]["accountID"] = "anthropic-abc"
            choice = account_choice.choose("anthropic", config, snapshot, None, name=name)
            self.assertEqual(account_choice.public(choice)["env_keys"], [])

    def test_google_requires_explicit_single_login_and_empty_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "supervisor.toml"
            path.write_text(GOOGLE_TOML)
            config = account_choice.load_config(path)
            self.assertEqual(config["accounts"][0]["env"], {})
            for text in (GOOGLE_TOML.replace("single_login = true", ""),
                         GOOGLE_TOML.replace("single_login = true", "single_login = false"),
                         GOOGLE_TOML.replace("env = {}", ""),
                         GOOGLE_TOML.replace("env = {}", 'env = { GOOGLE_HOME = "/pretend" }')):
                path.write_text(text)
                with self.subTest(text=text), self.assertRaises(ValueError):
                    account_choice.load_config(path)
            duplicate = GOOGLE_TOML.split("[[accounts.account]]", 1)[1].replace("agy-login", "second")
            path.write_text(GOOGLE_TOML + "\n[[accounts.account]]" + duplicate)
            with self.assertRaises(ValueError):
                account_choice.load_config(path)

    def test_google_follows_context_panel_and_reports_existing_login(self):
        config, snapshot = accounts_config(GOOGLE_TOML), google_snapshot()
        choice = account_choice.choose("google", config, snapshot, None, now=NOW)
        self.assertEqual(choice["account_id"], snapshot["accounts"][0]["id"])
        self.assertEqual(choice["source"], "context-panel")
        self.assertEqual(account_choice.public(choice)["account_binding"], account_choice.GOOGLE_ACCOUNT_BINDING)
        self.assertEqual(account_choice.public(choice)["env_keys"], [])
        batch = account_choice.choose_batch("google", config, snapshot, None, count=2, now=NOW)
        self.assertEqual([c["account_id"] for c in batch], snapshot["ranking"][0]["launchOrder"])
        # Other providers' rows do not make the one Google login ambiguous.
        snapshot["accounts"].append(account_row("main", .9, []))
        self.assertEqual(account_choice.choose("google", config, snapshot, None)["account_id"], choice["account_id"])
        with self.assertRaises(ValueError):
            account_choice.choose("google", config, snapshot, None, name=choice["name"])
        snapshot["answers"]["useNext"] = []
        with self.assertRaises(ValueError):
            account_choice.choose("google", config, snapshot, None)
        with self.assertRaises(ValueError):
            account_choice.choose("google", config, None, "reader failed")

    def test_google_refuses_multiple_snapshot_logins_and_unmatched_choice(self):
        config = accounts_config(GOOGLE_TOML)
        snapshot = google_snapshot()
        snapshot["accounts"].append({**snapshot["accounts"][0], "id": "google-def", "label": "Other"})
        with self.assertRaises(ValueError):
            account_choice.choose("google", config, snapshot, None)
        snapshot = google_snapshot()
        snapshot["answers"]["useNext"][0]["accountID"] = "google-def"
        with self.assertRaises(ValueError):
            account_choice.choose("google", config, snapshot, None)
        snapshot = google_snapshot()
        snapshot["accounts"][0]["label"] = "Different login"
        with self.assertRaises(ValueError):
            account_choice.choose("google", config, snapshot, None)

    def test_select_preview_and_receipts_use_readers_storage_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = accounts_config()
            config["snapshot_command"] = ["reader", "--storage-root", str(root)]
            with patch.object(account_choice, "load_config", return_value=config), patch.object(account_choice, "read_snapshot", return_value=(self.snapshot(), None)):
                choice = account_choice.select("anthropic")
            self.assertEqual(list(root.iterdir()), [])  # preview makes no receipt
            path = account_choice.record_launch(choice, NOW)
            receipt = json.loads(path.read_text())
            self.assertEqual(receipt, {"schemaVersion": 1, "provider": "anthropic", "accountID": "anthropic-def", "launchedAt": "2026-10-03T16:00:00Z"})
            self.assertEqual(path.parent, root / "Launch Receipts")
            self.assertLess(path.stat().st_size, 1024)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])
            self.assertNotIn(str(root), json.dumps(account_choice.public(choice)))

    def test_atomic_receipt_and_pruning_preserve_other_writers_and_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            choice = account_choice.choose("anthropic", accounts_config(), self.snapshot(), None)
            choice["storage_root"] = Path(folder)
            old = account_choice.record_launch(choice, NOW - timedelta(days=1))
            directory = old.parent
            other = directory / "20261001T160000Z-other.json"
            other.write_bytes(old.read_bytes())
            symlink = directory / "20261001T160000Z-supervisor-11111111111111111111111111111111.json"
            symlink.symlink_to(other)
            recent = account_choice.record_launch(choice, NOW - timedelta(hours=23))
            self.assertTrue(old.exists())
            original_replace = account_choice.os.replace
            observed = []
            def replace(source, target):
                self.assertEqual(source.parent, target.parent)
                self.assertFalse(target.exists())
                self.assertEqual(json.loads(source.read_text())["accountID"], "anthropic-def")
                observed.append(target)
                original_replace(source, target)
            with patch.object(account_choice.os, "replace", side_effect=replace):
                account_choice.record_launch(choice, NOW)
            self.assertEqual(len(observed), 1)
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(other.exists())
            self.assertTrue(symlink.is_symlink())

    def test_receipt_rename_failure_leaves_no_partial_json_or_temp_file(self):
        with tempfile.TemporaryDirectory() as folder:
            choice = account_choice.choose("anthropic", accounts_config(), self.snapshot(), None)
            choice["storage_root"] = Path(folder)
            with patch.object(account_choice.os, "replace", side_effect=OSError("disk failure")), self.assertRaises(OSError):
                account_choice.record_launch(choice, NOW)
            self.assertEqual(list((Path(folder) / "Launch Receipts").iterdir()), [])

    def test_app_group_receipts_route_through_the_readers_record_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = root / "Context Panel"
            app.write_text("#!/bin/sh\nexit 0\n")
            app.chmod(0o700)
            config = accounts_config()
            config["snapshot_command"] = [str(app), "--account-snapshot"]
            with patch.object(account_choice, "load_config", return_value=config), \
                    patch.object(account_choice, "read_snapshot", return_value=(self.snapshot(), None)):
                choice = account_choice.select("anthropic")
            self.assertEqual(choice["receipt_app"], str(app))
            self.assertNotIn(str(app), json.dumps(account_choice.public(choice)))
            account_choice.prepare_launch(choice)
            name = "20261003T160000Z-supervisor-" + "a" * 32 + ".json"
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, name + "\n", ""))
            choice["storage_root"] = root
            choice["gui"] = {"app": "Codex"}
            path = account_choice.record_launch(choice, NOW, runner=runner)
            argv, options = runner.call_args.args[0], runner.call_args.kwargs
            self.assertEqual(argv, [str(app), "--record-launch", "--launcher", "supervisor"])
            self.assertEqual(json.loads(options["input"]), {"schemaVersion": 1, "provider": "anthropic",
                                                            "accountID": "anthropic-def",
                                                            "launchedAt": "2026-10-03T16:00:00Z"})
            self.assertLessEqual(len(options["input"].encode()), 1024)
            self.assertEqual(path, root / "Launch Receipts" / name)
            self.assertFalse((root / "Launch Receipts").exists())  # the app wrote it, not this process
            gui = json.loads((root / "Supervisor GUI Receipts" / name).read_text())
            self.assertEqual(gui["gui"], {"app": "Codex"})

    def test_app_writer_failures_refuse_without_a_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            choice = account_choice.choose("anthropic", accounts_config(), self.snapshot(), None)
            choice.update(storage_root=root, receipt_app=str(root / "missing"))
            with self.assertRaises(ValueError):
                account_choice.prepare_launch(choice)
            outcomes = {
                "receipt is not current": subprocess.CompletedProcess([], 64, "", "receipt is not current\n"),
                "storage unavailable": subprocess.CompletedProcess([], 1, "", "storage unavailable\n"),
                "silent failure": subprocess.CompletedProcess([], 2, "", ""),
                "receipt outside the store": subprocess.CompletedProcess([], 0, "../elsewhere.json\n", ""),
                "timeout": subprocess.TimeoutExpired(["app"], 30),
                "missing app": FileNotFoundError(2, "missing"),
            }
            for reason, outcome in outcomes.items():
                with self.subTest(reason=reason):
                    runner = Mock(side_effect=outcome) if isinstance(outcome, BaseException) else Mock(return_value=outcome)
                    with self.assertRaises(ValueError):
                        account_choice.record_launch({**choice, "gui": {"app": "Codex"}}, NOW, runner=runner)
            self.assertEqual(list(root.iterdir()), [])


# Launch fixtures carry the rules link every Supervisor brief needs.
BRIEF = f"brief under {iterm_tab.BRIEF_RULES_URL}"


def fixture_briefs(command_files):
    files = command_files if isinstance(command_files, list) else [command_files]
    briefs = [path.with_suffix(".brief") for path in files]
    for path in briefs:
        path.write_text(BRIEF)
    return briefs


def account_launch_args(command_file):
    return argparse.Namespace(
        command="new", window_id="chosen", command_file=command_file, brief_file=fixture_briefs(command_file),
        account_provider="anthropic", account=None, account_config=None,
    )


class ProcessArgumentsTests(unittest.TestCase):
    source = "01a1514c-fa7f-7be3-95ba-4d25df73d07e"
    other = "01970242-7941-7f0c-b461-16aa5723fb51"

    def setUp(self):
        # Kernel existence is a fixture fact, never a host PID assumption.
        self.enterContext(patch.object(session_liveness.os, "kill", return_value=None))

    def test_kernel_boot_time_reads_both_hosts_and_unknown_keeps_strict_route(self) -> None:
        boot_seconds = 1792934190
        expected = datetime.fromtimestamp(boot_seconds, timezone.utc)
        with (patch.object(session_liveness.sys, "platform", "darwin"),
              patch.object(session_liveness.subprocess, "run", return_value=SimpleNamespace(
                  stdout=f"{{ sec = {boot_seconds}, usec = 167419 }}\n")) as kernel):
            self.assertEqual(session_liveness.host_boot_time(), expected)
            kernel.return_value.stdout = "unavailable"
            self.assertIsNone(session_liveness.host_boot_time())
            kernel.side_effect = OSError("unavailable")
            self.assertIsNone(session_liveness.host_boot_time())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(session_liveness.sys, "platform", "linux"), patch.object(session_liveness, "PROC_ROOT", root):
                (root / "stat").write_text(f"cpu 1 2 3\nbtime {boot_seconds}\n")
                self.assertEqual(session_liveness.host_boot_time(), expected)
                (root / "stat").write_text("cpu 1 2 3\nbtime unknown\n")
                self.assertIsNone(session_liveness.host_boot_time())

    def test_kernel_process_details_bind_executable_incarnation_and_owner(self):
        import ctypes
        import struct
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            executable = root / "native-bin"
            executable.touch()
            process = root / "proc" / "123"
            process.mkdir(parents=True)
            (process / "exe").symlink_to(executable)
            (process / "stat").write_text("123 (native process) S 42 " + "0 " * 17 + "789")
            with patch.object(session_liveness.sys, "platform", "linux"), \
                    patch.object(session_liveness, "PROC_ROOT", root / "proc"):
                observed = session_liveness.native_process_details(123)
                self.assertEqual(observed["executable"], str(executable))
                self.assertEqual(observed["ppid"], 42)
                self.assertEqual(observed["identity"]["startTicks"], int((process / "stat").read_text().split()[-1]))
                (process / "stat").write_text("123 (native process) Z 42 " + "0 " * 17 + "789")
                with self.assertRaises(ValueError):
                    session_liveness.native_process_details(123)
            library = Mock()
            path_bytes = os.fsencode(executable) + b"\0"
            kernel_identity = struct.pack("=16sQQQQQ", b"", 789, 42, 0, 0, 0)
            def path(_pid, output, _size):
                ctypes.memmove(output, path_bytes, len(path_bytes))
                return len(path_bytes)
            def info(_pid, _flavor, _arg, output, size):
                ctypes.memmove(output, kernel_identity, len(kernel_identity))
                return len(kernel_identity) if size == len(kernel_identity) else 0
            library.proc_pidpath.side_effect = path
            library.proc_pidinfo.side_effect = info
            with patch.object(session_liveness.sys, "platform", "darwin"), \
                    patch.object(ctypes, "CDLL", return_value=library), \
                    patch.object(session_liveness.subprocess, "run", return_value=SimpleNamespace(stdout=f"{os.getuid()} 42")) as process_read:
                observed = session_liveness.native_process_details(123)
                self.assertEqual(observed["executable"], str(executable))
                self.assertEqual(observed["identity"]["uniqueId"], struct.unpack("=16sQQQQQ", kernel_identity)[1])
                self.assertEqual(observed["ppid"], 42)
                process_read.return_value.stdout = f"{os.getuid() + 1} 42"
                with self.assertRaises(ValueError):
                    session_liveness.native_process_details(123)
                library.proc_pidinfo.return_value = 0
                library.proc_pidinfo.side_effect = None
                with self.assertRaises(OSError):
                    session_liveness.native_process_details(123)

    def test_spaced_cli_paths_keep_native_resume_and_unavailable_argv_vetoes(self):
        for prefix in (["/Tools With Spaces/bin/codex"], ["node", "/Application Support/bin/codex.js"],
                       ["node", "--inspect", "/Application Support/bin/codex.js"]):
            argv = [*prefix, "resume", self.source]
            for available in (True, False):
                kwargs = {"return_value": argv} if available else {"side_effect": OSError("argv unavailable")}
                with self.subTest(prefix=prefix, available=available), patch.object(session_liveness, "native_arguments", **kwargs):
                    with self.assertRaises(ValueError):
                        session_liveness.verify_process_exit(
                            {"session_id": self.source, "pid": 123, "tty": "source-tty"},
                            rows=[(456, "other", " ".join(argv))])

    def test_command_data_never_supplies_native_harness_ownership(self):
        data = "resume " + self.source
        cases = (["git", "-C", "/repo/codex-skills", "status", data],
                 ["rg", "codex " + data, "fixture.log"],
                 ["gh-plan", "--repo", "cbusillo/codex-skills", "show", "2285", data],
                 ["python3", "-I", "tool.py", "--repo", "cbusillo/codex-skills", data],
                 ["python3", "-m", "module", "/repo/codex-skills", data],
                 ["zsh", "-lc", "git -C /repo/codex-skills status"],
                 ["node", "--eval", "codex " + data])
        for argv in cases:
            for available in (True, False):
                kwargs = {"return_value": argv} if available else {"side_effect": OSError("argv unavailable")}
                with self.subTest(argv=argv, available=available), patch.object(session_liveness, "native_arguments", **kwargs):
                    command = " ".join(argv)
                    session_liveness.verify_process_exit(
                        {"session_id": self.source, "pid": 123, "tty": "source-tty"},
                        rows=[(456, "other", command)])

    def test_versioned_claude_resume_can_veto_but_cannot_grant_authority(self):
        executable = "/fixture/claude/versions/1.2.3"
        for available in (True, False):
            argv = [executable, "--resume", self.source]
            kwargs = {"return_value": argv} if available else {"side_effect": OSError("argv unavailable")}
            with self.subTest(available=available), patch.object(session_liveness, "native_arguments", **kwargs):
                with self.assertRaises(ValueError):
                    session_liveness.verify_process_exit(
                        {"session_id": self.source, "harness": "claude", "pid": 123, "tty": "source-tty"},
                        rows=[(456, "other", " ".join(argv))])
        with self.assertRaises(ValueError):
            self.probe([executable, "--resume", self.other], harness="claude")
        self.probe([executable, "--resume", self.other], harness="claude", guarded=True)

    def test_node_preload_values_do_not_hide_or_supply_the_main_harness(self):
        for preload in (["-r", "bootstrap.js"], ["--require", "bootstrap.js"],
                        ["-rbootstrap.js"], ["--require=bootstrap.js"],
                        ["--import", "bootstrap.js"], ["--loader=bootstrap.js"],
                        ["--experimental-loader", "bootstrap.js"]):
            for available in (True, False):
                for script in ("/fixture/claude.js", "/fixture/codex.js"):
                    resume = ["--resume", self.source] if script.endswith("claude.js") else ["resume", self.source]
                    argv = ["node", *preload, script, *resume]
                    kwargs = {"return_value": argv} if available else {"side_effect": OSError("argv unavailable")}
                    with self.subTest(preload=preload, available=available, script=script), patch.object(session_liveness, "native_arguments", **kwargs):
                        with self.assertRaises(ValueError):
                            session_liveness.verify_process_exit(
                                {"session_id": self.source, "pid": 123, "tty": "source-tty"},
                                rows=[(456, "other", " ".join(argv))])

    def probe(self, argv, *, tty="source-tty", scope="session", guarded=False, harness="codex"):
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
        entry = {"session_id": self.source, "harness": harness, "pid": 123, "tty": "source-tty"}
        with patch.object(session_liveness, "native_arguments", return_value=argv, create=True) as read:
            result = session_liveness.verify_process_exit(entry, rows=[(456, tty, " ".join(argv))],
                ended_at=cutoff, started_at=lambda _pid: cutoff + timedelta(seconds=1),
                independent_harness=lambda *_args: guarded, scope=scope)
        return result, read

    def test_actual_boundaries_distinguish_quoted_prompts_from_native_commands(self):
        for word in ("resume", "exec"):
            with self.subTest(word=word), self.assertRaisesRegex(ValueError, "unidentified harness"):
                self.probe(["codex", word + " " + self.other])
        self.probe(["codex", "resume", self.other])
        self.probe(["codex", "exec", "fresh prompt"])
        self.probe(["codex", "exec", "-"])
        self.probe(["codex", "resume", self.other, "-"])
        with self.assertRaisesRegex(ValueError, "unidentified harness"):
            self.probe(["codex", "--model", "model resume " + self.other])

    def test_real_resume_after_separator_vetoes_the_source(self):
        for prefix in (["codex", "resume"], ["codex", "exec", "resume"], ["codex", "e", "resume", "--json"]):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError, "resumed harness"):
                self.probe([*prefix, "--", self.source, "prompt"], tty="other")

    def test_last_and_prompt_uuids_never_supply_an_exact_other_exemption(self):
        for prefix in (["codex", "resume"], ["codex", "exec", "resume"]):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError, "unidentified harness"):
                self.probe([*prefix, "--last", self.other])
            self.probe([*prefix, "--last", self.source], tty="other")
        self.probe(["codex", "resume", self.other, "prompt " + self.source])
        self.probe(["codex", "resume " + self.source], tty="other")

    def test_unavailable_boundaries_grant_no_invocation_exemption(self):
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
        entry = {"session_id": self.source, "harness": "codex", "pid": 123, "tty": "source-tty"}
        for command in ("codex resume " + self.other, "codex exec -- fresh prompt"):
            with self.subTest(command=command), patch.object(session_liveness, "native_arguments", side_effect=OSError("argv unavailable"), create=True):
                kwargs = dict(rows=[(456, entry["tty"], command)], ended_at=cutoff,
                              started_at=lambda _pid: cutoff + timedelta(seconds=1))
                with self.assertRaisesRegex(ValueError, "unidentified harness"):
                    session_liveness.verify_process_exit(entry, **kwargs)
                session_liveness.verify_process_exit(entry, independent_harness=lambda *_args: True, **kwargs)

    def test_uncertain_resume_options_keep_source_operands_after_separator(self):
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
        entry = {"session_id": self.source, "harness": "codex", "pid": 123, "tty": "source-tty"}
        cases = ((["codex", "-m", "M", "resume", "--", self.source], False),
                 (["codex", "exec", "--json", "resume", "--", self.source], False),
                 (["codex", "--unlisted-option", "resume", "--", self.source], True))
        for argv, available in cases:
            kwargs = {"return_value": argv} if available else {"side_effect": OSError("argv unavailable")}
            with self.subTest(argv=argv, available=available), patch.object(session_liveness, "native_arguments", **kwargs):
                for tty in (entry["tty"], "other"):
                    with self.subTest(tty=tty), self.assertRaisesRegex(ValueError, "resumed harness"):
                        session_liveness.verify_process_exit(entry, rows=[(456, tty, " ".join(argv))],
                            ended_at=cutoff, started_at=lambda _pid: cutoff + timedelta(seconds=1),
                            independent_harness=lambda *_args: True)

    def test_uncertain_delimiters_do_not_hide_source_veto_candidates(self):
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
        cases = ((["claude", "--mcp-config", "--", "--resume", self.source], True),
                 (["claude", "--append-system-prompt", "keep -- terse", "--resume", self.source], False),
                 (["claude", "-pr", self.source], False),
                 (["claude", "-cr", self.source], False),
                 (["claude", "-pcr", self.source], False),
                 (["codex", "-c", 'key="a -- b"', "resume", self.source], False))
        for argv, available in cases:
            kwargs = {"return_value": argv} if available else {"side_effect": OSError("argv unavailable")}
            entry = {"session_id": self.source, "harness": argv[0], "pid": 123, "tty": "source-tty"}
            with self.subTest(argv=argv, available=available), patch.object(session_liveness, "native_arguments", **kwargs):
                for tty in (entry["tty"], "other"):
                    with self.subTest(tty=tty), self.assertRaisesRegex(ValueError, "resumed harness"):
                        session_liveness.verify_process_exit(entry, rows=[(456, tty, " ".join(argv))],
                            ended_at=cutoff, started_at=lambda _pid: cutoff + timedelta(seconds=1),
                            independent_harness=lambda *_args: True)
                self.assertIsNone(session_liveness.invocation(argv, bounded=available)[2])

    def test_known_native_separator_keeps_fresh_prompt_boundary(self):
        for argv in (["codex", "--", "please resume " + self.source],
                     ["claude", "--", "please --resume " + self.source]):
            with self.subTest(argv=argv):
                self.probe(argv, tty="other", harness=argv[0])
                with self.assertRaisesRegex(ValueError, "unidentified harness"):
                    self.probe(argv, harness=argv[0])
                # The same flattened text cannot prove where the prompt began.
                candidates, kind, exact = session_liveness.invocation(" ".join(argv).split(), bounded=False)
                self.assertIn(self.source, candidates)
                self.assertNotEqual(kind, "exec")
                self.assertIsNone(exact)

    def test_claude_attached_resume_supplies_only_source_veto_candidates(self):
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
        entry = {"session_id": self.source, "harness": "claude", "pid": 123, "tty": "source-tty"}
        for available in (False, True):
            argv = ["claude", "-r" + self.source]
            kwargs = {"return_value": argv} if available else {"side_effect": OSError("argv unavailable")}
            with self.subTest(available=available), patch.object(session_liveness, "native_arguments", **kwargs):
                for tty in (entry["tty"], "other"):
                    with self.subTest(tty=tty), self.assertRaisesRegex(ValueError, "resumed harness"):
                        session_liveness.verify_process_exit(entry, rows=[(456, tty, " ".join(argv))],
                            ended_at=cutoff, started_at=lambda _pid: cutoff + timedelta(seconds=1),
                            independent_harness=lambda *_args: True)
        # The same token can be another option's value. Its addition must not
        # create unrelated-session authority merely because argv is available.
        for argv in (["claude", "-r" + self.other], ["claude", "--append-system-prompt", "-r" + self.other]):
            with self.subTest(argv=argv), patch.object(session_liveness, "native_arguments", return_value=argv), \
                    self.assertRaisesRegex(ValueError, "unidentified harness"):
                session_liveness.verify_process_exit(entry, rows=[(456, entry["tty"], " ".join(argv))],
                    ended_at=cutoff, started_at=lambda _pid: cutoff + timedelta(seconds=1))

    def test_claude_exact_resume_respects_required_values_and_command_position(self):
        unsafe = (["--append-system-prompt", "--resume", self.other],
                  ["--append-system-prompt", "-r", self.other],
                  ["--append-system-prompt", "--resume=" + self.other],
                  ["--append-system-prompt", "-r" + self.other],
                  ["-r=" + self.other], ["-pr=" + self.other],
                  ["--unknown-option", "--resume", self.other],
                  ["agents", "--resume", self.other],
                  ["--", "--resume", self.other])
        for args in unsafe:
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, "unidentified harness"):
                self.probe(["claude", *args], harness="claude")
        supported = (["--resume", self.other], ["-r", self.other], ["--resume=" + self.other],
                     ["--model", "synthetic-model", "--resume", self.other],
                     ["--verbose", "--debug=synthetic-filter", "-r", self.other],
                     ["--append-system-prompt", "--resume", "--resume", self.other],
                     ["--append-system-prompt", "-r" + self.source, "--resume", self.other])
        for args in supported[:-1]:
            with self.subTest(args=args):
                self.probe(["claude", *args], harness="claude")
        # The conservative candidate veto may still see a source in a prompt.
        # Exact identity parsing itself must consume that option's value.
        self.assertEqual(session_liveness.invocation(["claude", *supported[-1]])[2], self.other)

    def test_claude_required_separator_value_does_not_hide_real_source_resume(self):
        argv = ["claude", "--append-system-prompt", "--", "--resume", self.source]
        with self.assertRaisesRegex(ValueError, "resumed harness"):
            self.probe(argv, tty="other", guarded=True, harness="claude")
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
        entry = {"session_id": self.source, "harness": "claude", "pid": 123, "tty": "source-tty"}
        with patch.object(session_liveness, "native_arguments", side_effect=OSError("argv unavailable")):
            with self.assertRaisesRegex(ValueError, "resumed harness"):
                session_liveness.verify_process_exit(entry, rows=[(456, "other", " ".join(argv))],
                    ended_at=cutoff, independent_harness=lambda *_args: True)
            self.assertIsNone(session_liveness.invocation(["claude", "--resume", self.other], bounded=False)[2])

    def test_claude_boolean_cluster_preserves_resume_veto_but_debug_owns_its_value(self):
        for available in (False, True):
            argv = ["claude", "-pr" + self.source]
            kwargs = {"return_value": argv} if available else {"side_effect": OSError("argv unavailable")}
            with self.subTest(available=available), patch.object(session_liveness, "native_arguments", **kwargs):
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
                entry = {"session_id": self.source, "harness": "claude", "pid": 123, "tty": "source-tty"}
                for tty in (entry["tty"], "other"):
                    with self.subTest(tty=tty), self.assertRaisesRegex(ValueError, "resumed harness"):
                        session_liveness.verify_process_exit(entry, rows=[(456, tty, " ".join(argv))],
                            ended_at=cutoff, started_at=lambda _pid: cutoff + timedelta(seconds=1),
                            independent_harness=lambda *_args: True)
        self.assertEqual(session_liveness.invocation(["claude", "-dr" + self.source])[0], [])
        with self.assertRaisesRegex(ValueError, "unidentified harness"):
            self.probe(["claude", "-pr" + self.other], harness="claude")

    def test_physical_scope_never_reads_argv_identity(self):
        _result, read = self.probe(["codex", "resume", self.source], scope="terminal")
        read.assert_not_called()

    def test_known_options_and_late_last_preserve_native_positional_roles(self):
        for prefix in (["codex", "--model", "model with spaces", "resume"],
                       ["codex", "-c", 'notify=["hello", "world"]', "exec", "--json", "resume"],
                       ["codex", "resume", "--model=other model"]):
            with self.subTest(prefix=prefix):
                self.probe([*prefix, self.other, "prompt " + self.source])
        self.probe(["codex", "resume", self.other, "-i", "image.png"])
        for tail in ([self.other, "--last"], [self.other, "prompt", "--last"]):
            with self.subTest(tail=tail), self.assertRaisesRegex(ValueError, "unidentified harness"):
                self.probe(["codex", "resume", *tail])

    def test_darwin_buffer_decodes_exact_argc_and_never_the_environment(self):
        argv = ["codex", "resume " + self.other, "", "--"]
        data = len(argv).to_bytes(4, sys.byteorder) + b"/fixture/codex\0\0\0" + b"\0".join(arg.encode() for arg in argv) + b"\0"
        # Invalid UTF-8 in the trailing environment would fail if it were decoded.
        self.assertEqual(session_liveness.darwin_arguments(data + b"DO_NOT_READ=\xff\0"), argv)
        for malformed in (b"", b"\0" * 4, data[:4] + b"/fixture/codex\0codex"):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                session_liveness.darwin_arguments(malformed)

    def test_linux_reader_preserves_boundaries_and_refuses_truncation(self):
        from unittest.mock import mock_open
        argv = ["codex", "resume " + self.other]
        data = b"\0".join(arg.encode() for arg in argv) + b"\0"
        with patch.object(sys, "platform", "linux"), patch.object(Path, "open", mock_open(read_data=data)):
            self.assertEqual(session_liveness.native_arguments(456), argv)
        for malformed in (b"", data[:-1], b"x" * (session_liveness.ARGUMENT_BYTES + 1)):
            with self.subTest(size=len(malformed)), patch.object(sys, "platform", "linux"), \
                    patch.object(Path, "open", mock_open(read_data=malformed)), self.assertRaises(ValueError):
                session_liveness.native_arguments(456)

    def test_darwin_native_reader_honors_kernel_size_and_argument_count(self):
        argv = ["codex", "resume", self.other]
        data = len(argv).to_bytes(4, sys.byteorder) + b"/fixture/codex\0\0" + b"\0".join(arg.encode() for arg in argv) + b"\0DO_NOT_READ=\xff\0"
        def sysctl(_mib, _count, destination, size, _new, _newsize):
            size._obj.value = len(data)
            if destination is not None:
                session_liveness.ctypes.memmove(destination, data, len(data))
            return 0
        library = SimpleNamespace(sysctl=Mock(side_effect=sysctl))
        with patch.object(sys, "platform", "darwin"), patch.object(session_liveness.ctypes, "CDLL", return_value=library):
            self.assertEqual(session_liveness.native_arguments(456), argv)


class TabCloseTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        opt_in = patch.object(iterm_tab, "CHROME_OPT_IN", Path(self.folder.name) / "absent")
        opt_in.start()
        self.addCleanup(opt_in.stop)
        self.ledger = Path(self.folder.name) / "ledger.json"
        self.entry = {"supervisor_owned": True, "iterm_session_id": "owned", "pid": 123,
                      "tty": "ttys001", "window_id": "supervisor", "tab_id": "tab"}
        self.ledger.write_text(json.dumps([self.entry]))
        self.session = SimpleNamespace(session_id="owned", async_close=AsyncMock(),
                                       async_get_variable=AsyncMock(return_value="/dev/ttys001"))
        self.tab = SimpleNamespace(tab_id="tab", sessions=[self.session])
        self.window = SimpleNamespace(window_id="supervisor", tabs=[self.tab])
        self.app = SimpleNamespace(connection=object(), terminal_windows=[self.window],
                                   async_refresh=AsyncMock())
        self.prompt = SimpleNamespace(state="editing", command="", unique_id="prompt")
        self.api = SimpleNamespace(PromptState=SimpleNamespace(EDITING="editing"),
                                   async_get_last_prompt=AsyncMock(return_value=self.prompt))
        self.rows = [(20, "ttys001", "-zsh"), (21, "ttys001", "/usr/bin/login -fpl user")]
        self.addCleanup(patch.stopall)
        patch.dict("sys.modules", {"iterm2": self.api}).start()
        self.real_inventory = session_liveness.inventory
        self.inventory = patch.object(session_liveness, "inventory", return_value=self.rows).start()
        patch.object(session_liveness, "process_arguments", side_effect=lambda _pid, command:
                     (session_liveness.harness_arguments(command), True)).start()

    def close(self, identity="owned"):
        return asyncio.run(iterm_tab.close_session(self.app, identity, self.ledger))

    def test_only_owned_idle_empty_exact_tab_closes_nonforce(self):
        self.assertTrue(self.close()["close_requested"])
        self.session.async_close.assert_awaited_once_with(force=False)
        self.assertEqual(self.inventory.call_count, 2)
        self.assertEqual(self.api.async_get_last_prompt.await_count, 2)

    def test_pidless_idle_iterm_close_retains_prompt_and_launch_pid_guards(self):
        entry = {key: value for key, value in self.entry.items() if key != "pid"}
        self.ledger.write_text(json.dumps([entry]))
        self.assertTrue(self.close()["close_requested"])
        self.session.async_close.assert_awaited_once_with(force=False)
        for change, rows, command in (
            ({"pid": False}, self.rows, ""), ({"pid": 0}, self.rows, ""),
            ({"pid": "123"}, self.rows, ""), ({"pid": {}}, self.rows, ""),
            ({"launch_pid": 456}, [*self.rows, (456, "other", "codex")], ""),
            ({}, [*self.rows, (456, self.entry["tty"], "codex")], ""),
            ({}, [], ""), ({}, self.rows, "draft"),
        ):
            with self.subTest(change=change, rows=rows, command=command):
                self.session.async_close.reset_mock()
                self.ledger.write_text(json.dumps([{**entry, **change}]))
                self.inventory.return_value = rows
                self.prompt.command = command
                with self.assertRaises(ValueError):
                    self.close()
                self.session.async_close.assert_not_awaited()

    def test_interrupted_terminal_cleanup_retains_physical_close_contract(self):
        transcript = self.ledger.parent / "interrupted.jsonl"
        transcript.write_text(json.dumps({"sessionId": "source", "type": "user", "message": {"content": "interrupted"}}) + "\n")
        self.ledger.write_text(json.dumps([{**self.entry, "session_id": "source", "harness": "claude", "transcript": "interrupted.jsonl"}]))
        self.assertTrue(self.close()["close_requested"])
        self.session.async_close.assert_awaited_once_with(force=False)

    def test_physical_close_does_not_stop_a_source_resumed_on_another_tty(self):
        self.ledger.write_text(json.dumps([{**self.entry, "session_id": "source", "harness": "codex"}]))
        self.inventory.return_value = [*self.rows, (456, "another-tty", "codex resume source")]
        self.assertTrue(self.close()["close_requested"])
        self.session.async_close.assert_awaited_once_with(force=False)

    def test_resume_candidates_do_not_prove_an_unrelated_session(self):
        entry = {**self.entry, "pid": 123, "session_id": "source", "harness": "codex"}
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
        for command in ("codex please resume notes", "codex resume --last", "claude --continue", "claude -c"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                session_liveness.verify_process_exit(
                    {**entry, "harness": "claude" if command.startswith("claude") else "codex"},
                    rows=[(456, entry["tty"], command)], ended_at=cutoff,
                    started_at=lambda _pid: cutoff + timedelta(seconds=1), replacement_owner=lambda _pid: True)
        for command in ("codex-aarch64-apple-darwin resume source", "codex-aarch64-apple-darwin exec resume source"):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "resumed harness"):
                session_liveness.verify_process_exit(entry, rows=[(456, "other", command)])

    def test_native_uuid_alias_resumes_are_the_source_on_every_tty(self):
        source = "01a1514c-fa7f-7be3-95ba-4d25df73d07e"
        entry = {**self.entry, "session_id": source, "harness": "codex"}
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
        for alias in (source.upper(), source.replace("-", ""), "{" + source.upper() + "}", "urn:uuid:" + source.upper()):
            for tty in (entry["tty"], "other"):
                with self.subTest(alias=alias, tty=tty), self.assertRaisesRegex(ValueError, "resumed harness"):
                    session_liveness.verify_process_exit(entry, rows=[(456, tty, "codex resume " + alias)],
                        ended_at=cutoff, started_at=lambda _pid: cutoff + timedelta(seconds=1))

    def test_codex_resume_options_keep_source_ids_separate_from_option_values(self):
        source = "01a1514c-fa7f-7be3-95ba-4d25df73d07e"
        other = "01a14296-2820-7da0-979b-51f158dfeb7b"
        entry = {**self.entry, "session_id": source, "harness": "codex"}
        for command in (
            f"codex exec resume --json {source}", f"codex e resume -m model {source}",
            f"codex resume --all {source}", f"node /bin/codex.js resume --model {other} {source}",
            f"codex exec resume --model={other} {source}",
            f'codex resume -c notify=true {source}',
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                session_liveness.verify_process_exit(entry, rows=[(456, "other", command)])
        for command in (
            f"codex resume -m {source} {other}", f"codex resume --model={source} {other}",
            f"codex resume -i {source} --last", f"codex resume --json {other} -- {source}",
            f"codex exec -- resume --json {source}",
        ):
            with self.subTest(command=command):
                session_liveness.verify_process_exit(entry, rows=[(456, "other", command)])
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
        for command in (f"codex resume -m {other} --last", f"codex resume --unknown-value {other} --last"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                session_liveness.verify_process_exit(entry, rows=[(456, entry["tty"], command)],
                    ended_at=cutoff, started_at=lambda _pid: cutoff + timedelta(seconds=1))
        session_liveness.verify_process_exit(entry, rows=[(456, entry["tty"], f"codex resume -m {source} {other}")],
            ended_at=cutoff, started_at=lambda _pid: cutoff + timedelta(seconds=1))

    def test_repository_codex_scripts_are_unrelated_replacement_work(self):
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
        for command in ("python /repo/codex_idle_watch.py", "sh /repo/codex-watch.sh"):
            with self.subTest(command=command):
                session_liveness.verify_process_exit({**self.entry, "harness": "codex"},
                    rows=[(456, self.entry["tty"], command)], ended_at=cutoff,
                    started_at=lambda _pid: cutoff + timedelta(seconds=1))

    def test_disappeared_tty_process_needs_kernel_absence_for_session_proof(self) -> None:
        entry = {**self.entry, "pid": 123, "session_id": "source", "harness": "codex"}
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
        for scope, absent in (("session", True), ("session", False), ("terminal", True)):
            with self.subTest(scope=scope, absent=absent), \
                    patch.object(session_liveness, "process_arguments", return_value=([], True)), \
                    patch.object(session_liveness, "process_absent", return_value=absent):
                started = Mock(side_effect=subprocess.CalledProcessError(1, ["ps"]))
                if scope == "session" and absent:
                    session_liveness.verify_process_exit(entry, rows=[(456, entry["tty"], "short-helper")],
                        ended_at=cutoff, started_at=started, scope=scope)
                else:
                    with self.assertRaises(ValueError):
                        session_liveness.verify_process_exit(entry, rows=[(456, entry["tty"], "short-helper")],
                            ended_at=cutoff, started_at=started, scope=scope)

    def test_managed_wrapper_needs_its_guard_on_the_source_tty(self):
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
        for name in ("codex-session.py", "codex_session_proxy.py"):
            for guarded in (False, True):
                with self.subTest(name=name, guarded=guarded):
                    probe = lambda: session_liveness.verify_process_exit({**self.entry, "harness": "codex"},
                        rows=[(456, self.entry["tty"], "python /repo/" + name + " launch")], ended_at=cutoff,
                        started_at=lambda _pid: cutoff + timedelta(seconds=1), independent_harness=lambda *_args: guarded)
                    if guarded:
                        probe()
                    else:
                        with self.assertRaisesRegex(ValueError, "unidentified harness"):
                            probe()

    def test_inventory_keeps_long_resume_operands(self):
        source = "01a1514c-fa7f-7be3-95ba-4d25df73d07e"
        self.inventory.side_effect = self.real_inventory
        def ps(argv, **_kwargs):
            # Model ps's truncated command column unless unlimited width is requested.
            command = "/" + "long-vendor-path/" * 8 + "codex resume " + source
            return SimpleNamespace(stdout="456 other " + (command if "-ww" in argv else command[:79]))
        with patch.object(session_liveness.subprocess, "run", side_effect=ps), self.assertRaisesRegex(ValueError, "resumed harness"):
            session_liveness.verify_process_exit({**self.entry, "session_id": source})

    def test_malformed_native_cutoff_falls_back_to_strict_process_proof(self):
        import tmux_terminal
        entry = {**self.entry, "tmux_socket": "fixture", "tmux_session_id": "tmux:fixture:" + "a" * 32,
                 "launch_started_at": 5}
        pane = {"session_id": entry["tmux_session_id"], "pane_id": "%1", "pid": 789,
                "tmux_socket": "fixture", "window_id": entry["window_id"], "tab_id": entry["tab_id"],
                "tty": entry["tty"], "dead": True, "exited_at": "2026-10-01T02:00:00Z"}
        for row in (pane, {key: value for key, value in pane.items() if key != "tab_id"}):
            with self.subTest(row=row), patch.object(tmux_terminal.Terminal, "list", return_value=[row]):
                self.assertIsNone(session_liveness.native_exit_cutoff(entry))
                with self.assertRaises(ValueError):
                    session_liveness.verify_process_exit(entry, rows=[(456, entry["tty"], "codex exec -- new task")])

    def test_unknown_peers_on_other_ttys_remain_the_callers_confirmation(self):
        entry = {**self.entry, "pid": 123, "session_id": "source"}
        for command, harness in (("codex resume --last", "codex"), ("claude --continue", "claude")):
            with self.subTest(command=command):
                session_liveness.verify_process_exit({**entry, "harness": harness}, rows=[(456, "other", command)])
        self.assertEqual(session_liveness.invocation(["claude", "--continue"]), ([], "resume", None))
        self.assertEqual(session_liveness.invocation(["claude", "-c"]), ([], "resume", None))

    def test_resume_identity_is_read_only_from_harness_operands(self):
        entry = {**self.entry, "session_id": "source", "pid": 123}
        for command in ("echo codex resume source", "python worker.py resume source", "codex -- resume source", "codex exec -- resume source"):
            with self.subTest(command=command):
                session_liveness.verify_process_exit(entry, rows=[(456, "other", command)])
        for command in ("codex resume source -- prompt", "bun /bin/claude.js --resume=source", "node /bin/claude --resume source",
                        'codex -c notify=true resume source',
                        "codex exec resume source", "codex e resume source", "node /bin/codex.js exec resume source"):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "resumed harness"):
                session_liveness.verify_process_exit(entry, rows=[(456, "other", command)])

    def test_missing_source_pid_is_uncertainty_for_every_ordinary_probe(self):
        for value in (None, False, 0, -1, "123"):
            with self.subTest(pid=value), self.assertRaises(ValueError):
                session_liveness.verify_process_exit({**self.entry, "pid": value}, rows=self.rows)

    def test_resume_picker_is_not_fresh_exec_and_explicit_prompt_is_unrelated(self):
        entry = {**self.entry, "harness": "codex", "session_id": "source"}
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
        born = cutoff + timedelta(seconds=1)
        for command in ("codex exec resume --last", "codex e resume --last", "node /bin/codex.js exec resume --last"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                session_liveness.verify_process_exit(entry, rows=[(456, self.entry["tty"], command)],
                                                     ended_at=cutoff, started_at=lambda _pid: born)
        for command in ("codex exec -- resume source", "codex e -- resume source"):
            with self.subTest(command=command):
                session_liveness.verify_process_exit(entry, rows=[(456, self.entry["tty"], command)],
                                                     ended_at=cutoff, started_at=lambda _pid: born)

    def test_verified_exit_uses_final_verdict_independently_of_recorded_jobs(self):
        status = {"safe_verdict": True, "background_tasks": [{"task_id": "wake", "kind": "scheduled"}]}
        session_liveness.require_safe_status(status)
        with self.assertRaises(ValueError):
            session_liveness.require_safe_status({**status, "safe_verdict": False})

    def test_refuses_missing_duplicate_director_or_moved_identity(self):
        for entries in ([], [self.entry, self.entry],
                        [{**self.entry, "supervisor_owned": False}],
                        [{**self.entry, "tty": "ttys999"}],
                        [{**self.entry, "tty": None}],
                        [{**self.entry, "window_id": "director"}],
                        [{**self.entry, "tab_id": "other"}]):
            with self.subTest(entries=entries):
                self.ledger.write_text(json.dumps(entries))
                with self.assertRaises(ValueError):
                    self.close()
        self.ledger.write_text(json.dumps([self.entry]))
        with self.assertRaises(ValueError):
            self.close("own")
        self.session.async_close.assert_not_awaited()

    def test_refuses_split_tab_jobs_scripts_or_uncertain_inventory(self):
        self.tab.sessions.append(SimpleNamespace(session_id="other"))
        with self.assertRaisesRegex(ValueError, "split"):
            self.close()
        self.tab.sessions.pop()
        for rows in ([], [(20, "ttys002", "-zsh")],
                     self.rows + [(99, "ttys001", "codex")],
                     self.rows + [(99, "ttys001", "sleep 60")],
                     [(20, "ttys001", "zsh -c task")]):
            with self.subTest(rows=rows):
                self.inventory.return_value = rows
                with self.assertRaises(ValueError):
                    self.close()
        self.session.async_close.assert_not_awaited()

    def test_refuses_input_unknown_prompt_and_changes_before_close(self):
        for prompt in (None, SimpleNamespace(state="running", command=""),
                       SimpleNamespace(state="editing", command="typed text"),
                       SimpleNamespace(state="editing", command=" "),
                       SimpleNamespace(state="editing", command=None)):
            with self.subTest(prompt=prompt):
                self.api.async_get_last_prompt.return_value = prompt
                with self.assertRaises(ValueError):
                    self.close()
        self.api.async_get_last_prompt.return_value = self.prompt
        self.inventory.side_effect = [self.rows, self.rows + [(99, "ttys001", "codex")]]
        with self.assertRaises(ValueError):
            self.close()
        self.inventory.side_effect = None
        self.api.async_get_last_prompt.side_effect = [self.prompt,
            SimpleNamespace(state="editing", command="", unique_id="new")]
        with self.assertRaisesRegex(ValueError, "changed"):
            self.close()
        self.session.async_close.assert_not_awaited()

    def test_reused_recorded_agent_pid_preserves_tab(self):
        self.ledger.write_text(json.dumps([{**self.entry, "pid": 99}]))
        self.inventory.return_value = self.rows + [(99, "ttys999", "codex")]
        with self.assertRaisesRegex(ValueError, "PID"):
            self.close()
        self.session.async_close.assert_not_awaited()

    def test_claude_exec_replaces_launch_shell(self):
        choice = {"provider": "anthropic", "env": {}}
        with tempfile.TemporaryDirectory() as folder:
            probe = Path(folder) / "claude"
            probe.write_text('#!/bin/sh\nprintf "%s\\n" "$$" "$CLAUDE_ACCOUNT_MOVE_ENABLED" "$1"\n')
            probe.chmod(0o700)
            brief = "brief has && ; exec in text"
            command = iterm_tab.with_account(
                f"cd / && exec {shlex.quote(str(probe))} {shlex.quote(brief)}", choice)
            child = subprocess.Popen(["/bin/sh", "-c", command], stdout=subprocess.PIPE, text=True,
                env={k: v for k, v in os.environ.items() if k != "CLAUDE_ACCOUNT_MOVE_ENABLED"})
            output, _ = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0)
            self.assertEqual(output.splitlines(), [str(child.pid), "1", brief])

    def test_returning_claude_launch_does_not_mark_later_commands(self):
        choice = {"provider": "anthropic", "env": {}}
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        probe = Path(folder.name) / "claude"
        probe.write_text('#!/bin/sh\nprintf "%s " "$CLAUDE_ACCOUNT_MOVE_ENABLED"\n')
        probe.chmod(0o700)
        command = iterm_tab.with_account(shlex.quote(str(probe)), choice)
        result = subprocess.run(["/bin/sh", "-c",
            command + '\nprintf "%s" "${CLAUDE_ACCOUNT_MOVE_ENABLED-unset}"'],
            env={k: v for k, v in os.environ.items() if k != "CLAUDE_ACCOUNT_MOVE_ENABLED"},
            capture_output=True, text=True, timeout=5, check=True)
        self.assertEqual(result.stdout, "1 unset")
        for text in ('cd / && exec claude brief',
                     'cd / &&exec claude "brief has && punctuation"',
                     '. /integration && exec claude "brief has ; punctuation"'):
            self.assertIsNotNone(iterm_tab.scoped_exec(text))
        for text in ('claude "exec task"', 'exec claude task; printf later',
                     '( exec claude task )', 'claude task'):
            self.assertIsNone(iterm_tab.scoped_exec(text))
        command = iterm_tab.with_account('cd /nonexistent-supervisor-test-1627 && exec claude', choice)
        result = subprocess.run(["/bin/sh", "-c", command + '\nprintf "%s" "${CLAUDE_ACCOUNT_MOVE_ENABLED-unset}"'],
            env={k: v for k, v in os.environ.items() if k != "CLAUDE_ACCOUNT_MOVE_ENABLED"},
            capture_output=True, text=True, timeout=5, check=True)
        self.assertEqual(result.stdout, "unset")


def project_fixture_toml(directory):
    return f'[projects.{json.dumps(str(directory.resolve()))}]\ntrust_level = "trusted"\n'


def write_codex_project_fixture(directory, home):
    """Create isolated project trust for launch tests, without any account login."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(project_fixture_toml(directory))


class CodexProjectTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        opt_in = patch.object(iterm_tab, "CHROME_OPT_IN", Path(folder.name) / "absent")
        opt_in.start()
        self.addCleanup(opt_in.stop)

    def test_codex_cannot_skip_preflight_through_another_provider(self):
        for provider in ("anthropic", "google"):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                iterm_tab.check_codex_project("codex --remote unix:// -C /", {"provider": provider, "env": {}})
        iterm_tab.check_codex_project("claude", {"provider": "anthropic", "env": {}})

    def test_git_main_root_trust_covers_worktree_and_subdirectories(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            repository = root / "repository"
            subprocess.run(["git", "init", str(repository)], capture_output=True, check=True)
            subprocess.run(["git", "-C", str(repository), "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                            "commit", "--allow-empty", "-m", "fixture"], capture_output=True, check=True)
            worktree = root / "worktree"
            subprocess.run(["git", "-C", str(repository), "worktree", "add", "--detach", str(worktree)],
                           capture_output=True, check=True)
            home = root / "selected"
            write_codex_project_fixture(repository, home)
            choice = {"provider": "openai", "env": {"CODEX_HOME": str(home)}}
            for directory in (repository / "subdirectory", worktree / "subdirectory", worktree):
                directory.mkdir(exist_ok=True)
                with self.subTest(directory=directory):
                    iterm_tab.check_codex_project("codex -C " + shlex.quote(str(directory)), choice)
            config = home / "config.toml"
            baseline = project_fixture_toml(repository)
            for override in ('trust_level = "untrusted"', ""):
                config.write_text(baseline + f'\n[projects.{json.dumps(str(worktree))}]\n{override}\n')
                with self.subTest(override=override), self.assertRaises(ValueError):
                    iterm_tab.check_codex_project("codex -C " + shlex.quote(str(worktree)), choice)
            config.write_text(baseline)
            # A copied pointer must not inherit trust from the real worktree's repository.
            impostor = root / "impostor"
            impostor.mkdir()
            (impostor / ".git").write_bytes((worktree / ".git").read_bytes())
            with self.assertRaises(ValueError):
                iterm_tab.check_codex_project("codex -C " + shlex.quote(str(impostor)), choice)

    def test_original_alias_trust_and_canonical_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            directory = root / "repository"
            directory.mkdir()
            alias = root / "alias"
            alias.symlink_to(directory, target_is_directory=True)
            home = root / "selected"
            home.mkdir()
            config = home / "config.toml"
            baseline = f'[projects.{json.dumps(str(alias))}]\ntrust_level = "trusted"\n'
            config.write_text(baseline)
            choice = {"provider": "openai", "env": {"CODEX_HOME": str(home)}}
            command = "codex -C " + shlex.quote(str(alias))
            iterm_tab.check_codex_project(command, choice)
            config.write_text(baseline + f'\n[projects.{json.dumps(str(directory))}]\ntrust_level = "untrusted"\n')
            with self.assertRaises(ValueError):
                iterm_tab.check_codex_project(command, choice)

    def test_directory_options_and_selected_home_trust(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repository = root / "brief's repository"
            repository.mkdir()
            home = root / "selected"
            choice = {"provider": "openai", "env": {"CODEX_HOME": str(home)}}
            write_codex_project_fixture(repository, home)
            before = (home / "config.toml").read_bytes()
            for options in (["-C", str(repository)], ["--cd", str(repository)],
                            ["--cd=" + str(repository)], ["-C" + str(repository)]):
                with self.subTest(options=options), patch.dict(os.environ, {"CODEX_HOME": "/wrong-home"}):
                    iterm_tab.check_codex_project("codex --remote unix:// " + shlex.join(options), choice)
            self.assertEqual((home / "config.toml").read_bytes(), before)

    def test_missing_relative_duplicate_and_nonexistent_directories_refuse(self):
        choice = {"provider": "openai", "env": {"CODEX_HOME": "/unused"}}
        for command in ("cd / && exec codex --remote unix://", "codex -C", "codex -C .",
                        "codex -C / -C /", "codex -C /nonexistent-supervisor-test-1903",
                        "codex -- --cd /", "claude -C /"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                iterm_tab.check_codex_project(command, choice)

    def test_untrusted_missing_and_parent_only_trust_refuse_without_config_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            repository = root / "repository"
            repository.mkdir()
            home = root / "selected"
            home.mkdir()
            config = home / "config.toml"
            choice = {"provider": "openai", "env": {"CODEX_HOME": str(home)}}
            command = "codex -C " + shlex.quote(str(repository))
            for contents in (None, "", f'[projects.{json.dumps(str(root))}]\ntrust_level = "trusted"',
                             f'[projects.{json.dumps(str(repository))}]\ntrust_level = "untrusted"',
                             "projects = []", f'projects = {{{json.dumps(str(repository))} = "trusted"}}'):
                if contents is not None:
                    config.write_text(contents)
                with self.subTest(contents=contents), self.assertRaises(ValueError) as caught:
                    iterm_tab.check_codex_project(command, choice)
                self.assertIn(str(home), str(caught.exception))
                self.assertIn(str(repository), str(caught.exception))
                if contents is None:
                    self.assertFalse(config.exists())
                else:
                    self.assertEqual(config.read_text(), contents)

    def test_malformed_or_unreadable_config_refuses_without_private_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.toml"
            choice = {"provider": "openai", "env": {"CODEX_HOME": str(root)}}
            config.write_text('private = "DO_NOT_PRINT\n')
            command = "codex -C " + shlex.quote(str(root))
            with self.assertRaises(ValueError) as caught:
                iterm_tab.check_codex_project(command, choice)
            self.assertNotIn("DO_NOT_PRINT", str(caught.exception))
            with patch.object(Path, "open", side_effect=PermissionError), self.assertRaises(ValueError):
                iterm_tab.check_codex_project(command, choice)

    def test_untrusted_later_batch_item_prevents_daemons_and_receipts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repository = root / "repository"
            repository.mkdir()
            homes = [root / "trusted", root / "untrusted"]
            write_codex_project_fixture(repository, homes[0])
            command = "codex --remote unix:// -C " + shlex.quote(str(repository))
            choices = [{"provider": "openai", "env": {"CODEX_HOME": str(home)}} for home in homes]
            files = [root / "first", root / "second"]
            for path in files:
                path.write_text(command)
            args = account_launch_args(files)
            args.account_provider = "openai"
            with (patch.object(account_choice, "select_batch", return_value=choices),
                  patch.object(iterm_tab, "check_codex_daemon") as daemon,
                  patch.object(account_choice, "prepare_launch") as prepare,
                  patch.object(account_choice, "record_launch") as receipt,
                  self.assertRaises(ValueError)):
                iterm_tab.prepare_launches(args)
            daemon.assert_not_called()
            prepare.assert_not_called()
            receipt.assert_not_called()

    def test_prepared_command_preserves_explicit_directory_and_brief(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as folder:
            root = Path(folder).resolve()
            repository = root / "brief's repository"
            repository.mkdir()
            fake_codex = root / "codex"
            fake_codex.write_text(
                f'#!{sys.executable}\nimport argparse, json, os\n'
                'parser = argparse.ArgumentParser()\n'
                'parser.add_argument("--remote")\nparser.add_argument("-C", "--cd")\n'
                'parser.add_argument("-c", action="append")\nparser.add_argument("prompt")\nargs = parser.parse_args()\n'
                'os.chdir(args.cd)\nprint(json.dumps([os.getcwd(), args.prompt]))\n'
            )
            fake_codex.chmod(0o700)
            home = root / "selected"
            write_codex_project_fixture(repository, home)
            endpoint = iterm_tab.codex_daemon_endpoint(home)
            endpoint.parent.mkdir()
            native_socket = self.enterContext(socket.socket(socket.AF_UNIX))
            native_socket.bind(str(endpoint))
            endpoint.chmod(0o600)
            launch = root / "launch"
            launch.write_text(f"{shlex.quote(str(fake_codex))} -C {shlex.quote(str(repository))}")
            code = root / ".code"
            (code / "skill-data").mkdir(parents=True)
            (code / "skill-data/supervisor.toml").write_text(
                '[[accounts.account]]\nname="fixture"\nprovider="openai"\ncontext_panel_label="fixture"\n'
                + 'env={CODEX_HOME=' + json.dumps(str(home)) + '}\n')
            args = account_launch_args(launch)
            args.account_provider = "openai"
            choice = {"provider": "openai", "env": {"CODEX_HOME": str(home)}}
            with (patch.object(account_choice, "select", return_value=choice),
                  patch.object(iterm_tab, "check_codex_daemon"),
                  patch.object(account_choice, "prepare_launch")):
                commands, _ = iterm_tab.prepare_launches(args)
            result = subprocess.run(["/bin/sh", "-c", commands[0]], cwd=root,
                                    env={**os.environ, "HOME": str(root), "CODE_HOME": str(code)},
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), [str(repository.resolve()), args.brief_file[0].read_text()])


def launch_terminal_fixture():
    """One account-selected launch target; each test keeps its own mocks and state."""
    terminal = SimpleNamespace(session_id="new", async_send_text=AsyncMock())
    window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock(
        return_value=SimpleNamespace(tab_id="tab", current_session=terminal)))
    app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
    return terminal, window, app


def git_fixture(*args):
    subprocess.run(["git", *args], capture_output=True, check=True)


def git_repository(path):
    git_fixture("init", str(path))
    git_fixture("-C", str(path), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "fixture")
    return path


def claude_projects(config):
    return json.loads(config.read_text())["projects"]


class ClaudeTrustTests(unittest.TestCase):
    """Folder trust in every configured Claude home, with fixture homes only (codex-skills#2100)."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.user = self.root / "user"
        self.developer = self.user / "Developer"
        self.worktrees = self.root / "worktrees"
        for path in (self.developer, self.worktrees):
            path.mkdir(parents=True)
        self.homes = [self.root / "claude-main", self.root / "claude-spare"]
        self.configs = [home / ".claude.json" for home in self.homes] + [self.user / ".claude.json"]
        for config in self.configs:
            config.parent.mkdir(exist_ok=True)
            config.write_text(json.dumps({
                "oauthAccount": {"emailAddress": "fixture@example.invalid"},
                "projects": {str(self.developer): {"hasTrustDialogAccepted": True}},
            }))
            config.chmod(0o600)
        accounts = [{"name": "main", "provider": "anthropic", "env": {"CLAUDE_CONFIG_DIR": str(self.homes[0])}},
                    {"name": "spare", "provider": "anthropic", "env": {"CLAUDE_CONFIG_DIR": str(self.homes[1])}},
                    {"name": "codex", "provider": "openai", "env": {"CODEX_HOME": str(self.root / "codex")}}]
        self.choice = {"provider": "anthropic", "env": accounts[0]["env"]}
        for target in (patch.object(Path, "home", return_value=self.user),
                       patch.object(iterm_tab, "CHROME_OPT_IN", self.root / "absent-opt-in"),
                       patch.object(iterm_tab.claude_trust, "worktree_root", return_value=self.worktrees),
                       patch.object(account_choice, "load_config", return_value={"accounts": accounts}),
                       patch.object(account_choice, "select", side_effect=lambda *_: self.choice),
                       patch.object(iterm_tab, "check_codex_daemon"),
                       patch.object(account_choice, "prepare_launch")):
            target.start()
            self.addCleanup(target.stop)

    def launch(self, directory):
        command = self.root / "command"
        command.write_text(f"cd {shlex.quote(str(directory))} && exec claude")
        return iterm_tab.prepare_launches(account_launch_args(command))

    def test_repository_under_developer_is_trusted_in_every_claude_home(self):
        # The October 17 stop: ~/Developer itself was trusted, but not the repository inside it.
        repository = git_repository(self.developer / "odoo-enterprise-docker")
        (repository / "addons").mkdir()
        config = self.configs[1]
        data = json.loads(config.read_text())
        data["projects"][str(repository)] = {"allowedTools": ["Read"]}
        config.write_text(json.dumps(data))
        data = json.loads(self.configs[2].read_text())
        del data["projects"]
        self.configs[2].write_text(json.dumps(data))
        self.launch(repository / "addons")
        for config in self.configs:
            with self.subTest(config=config):
                data = json.loads(config.read_text())
                self.assertIs(data["projects"][str(repository)]["hasTrustDialogAccepted"], True)
                self.assertEqual(data["oauthAccount"], {"emailAddress": "fixture@example.invalid"})
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(claude_projects(self.configs[1])[str(repository)]["allowedTools"], ["Read"])
        self.assertFalse((self.root / "codex").exists())

    def test_worktree_of_developer_repository_trusts_main_and_worktree_roots(self):
        repository = git_repository(self.developer / "codex-skills")
        worktree = self.worktrees / "codex-skills" / "task"
        git_fixture("-C", str(repository), "worktree", "add", "--detach", str(worktree))
        self.launch(worktree)
        for config in self.configs:
            with self.subTest(config=config):
                projects = claude_projects(config)
                self.assertIs(projects[str(repository)]["hasTrustDialogAccepted"], True)
                self.assertIs(projects[str(worktree)]["hasTrustDialogAccepted"], True)

    def test_folders_outside_developer_refuse_before_any_write_or_launch_step(self):
        outside = git_repository(self.root / "outside")
        foreign = self.worktrees / "foreign"
        git_fixture("-C", str(outside), "worktree", "add", "--detach", str(foreign))
        escape = self.developer / "escape"
        escape.symlink_to(outside, target_is_directory=True)
        plain = self.worktrees / "plain"
        plain.mkdir()
        disguised = self.worktrees / "disguised"
        git_fixture("-C", str(outside), "worktree", "add", "--detach", str(disguised))
        pointer = (disguised / ".git").read_text().split(":", 1)[1].strip()
        (disguised / ".git").write_text(f"gitdir: {escape / Path(pointer).relative_to(outside)}\n")
        before = [config.read_bytes() for config in self.configs]
        for directory in (outside, foreign, escape, plain, disguised, self.developer.parent):
            with (self.subTest(directory=directory),
                  patch.object(account_choice, "prepare_launch") as prepare,
                  self.assertRaisesRegex(ValueError, "not trusted") as caught):
                self.launch(directory)
            prepare.assert_not_called()
            self.assertIn(str(self.configs[0]), str(caught.exception))
            self.assertEqual([config.read_bytes() for config in self.configs], before)

    def test_folder_outside_developer_launches_when_the_selected_home_already_trusts_it(self):
        outside = self.root / "outside"
        outside.mkdir()
        data = json.loads(self.configs[0].read_text())
        data["projects"][str(outside)] = {"hasTrustDialogAccepted": True}
        self.configs[0].write_text(json.dumps(data))
        before = [config.read_bytes() for config in self.configs]
        commands, _ = self.launch(outside)
        self.assertIn(str(outside), commands[0])
        self.assertEqual([config.read_bytes() for config in self.configs], before)

    def session_save(self):
        # Another Claude session saves a refreshed login from its own view of the file,
        # which does not include this launch's trust key.
        data = json.loads(self.configs[0].read_text())
        data["oauthAccount"] = {"emailAddress": "refreshed@example.invalid"}
        saved = self.homes[0] / ".claude.json.tmp.session"
        saved.write_text(json.dumps(data))
        os.replace(saved, self.configs[0])

    def assert_both_saves_kept(self, repository):
        data = json.loads(self.configs[0].read_text())
        self.assertEqual(data["oauthAccount"], {"emailAddress": "refreshed@example.invalid"})
        self.assertIs(data["projects"][str(repository)]["hasTrustDialogAccepted"], True)
        self.assertEqual(sorted(path.name for path in self.homes[0].iterdir()), [".claude.json"])

    def test_session_save_just_before_the_swap_is_kept(self):
        repository = git_repository(self.developer / "repository")
        exchange = iterm_tab.claude_trust.exchange
        calls = []

        def save_then_exchange(source, target):
            if not calls and Path(target) == self.configs[0]:
                self.session_save()
            calls.append(target)
            exchange(source, target)

        with patch.object(iterm_tab.claude_trust, "exchange", side_effect=save_then_exchange):
            self.launch(repository)
        self.assert_both_saves_kept(repository)

    def test_session_save_just_after_the_swap_is_kept(self):
        repository = git_repository(self.developer / "repository")
        exchange = iterm_tab.claude_trust.exchange
        calls = []

        def exchange_then_save(source, target):
            exchange(source, target)
            if not calls and Path(target) == self.configs[0]:
                self.session_save()
            calls.append(target)

        with patch.object(iterm_tab.claude_trust, "exchange", side_effect=exchange_then_save):
            self.launch(repository)
        self.assert_both_saves_kept(repository)

    def test_missing_directory_or_home_config_refuses_without_partial_writes(self):
        command = self.root / "command"
        command.write_text("exec claude")
        with self.assertRaisesRegex(ValueError, "cd /absolute/repository"):
            iterm_tab.prepare_launches(account_launch_args(command))
        repository = git_repository(self.developer / "repository")
        self.configs[1].unlink()
        before = [self.configs[0].read_bytes(), self.configs[2].read_bytes()]
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.launch(repository)
        self.assertEqual([self.configs[0].read_bytes(), self.configs[2].read_bytes()], before)
        self.assertFalse(self.configs[1].exists())

    def test_unused_default_home_is_not_created(self):
        repository = git_repository(self.developer / "repository")
        self.configs[2].unlink()
        self.launch(repository)
        self.assertFalse(self.configs[2].exists())
        for config in self.configs[:2]:
            with self.subTest(config=config):
                self.assertIs(claude_projects(config)[str(repository)]["hasTrustDialogAccepted"], True)


class TerminalTests(unittest.TestCase):
    def test_fresh_codex_and_claude_launches_export_the_spawned_label(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for harness in ("codex", "claude"):
                command = root / harness
                command.write_text(harness)
                args = account_launch_args(command)
                args.account_provider = None
                commands, _ = iterm_tab.prepare_launches(args)
                probe = commands[0].replace(harness + " ", "capture ", 1)
                result = subprocess.run(["/bin/sh", "-c", 'capture() { printf "%s" "$DISCORD_BLUE_SESSION_KIND"; }; ' + probe],
                                        capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout, "spawned")
                after = subprocess.run(["/bin/sh", "-c", 'unset DISCORD_BLUE_SESSION_KIND; capture() { :; }; ' + probe
                                        + '; printf "%s" "${DISCORD_BLUE_SESSION_KIND-unlabelled}"'],
                                       capture_output=True, text=True, check=True)
                self.assertEqual(after.stdout, "unlabelled")

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        opt_in = patch.object(iterm_tab, "CHROME_OPT_IN", Path(folder.name) / "absent")
        opt_in.start()
        self.addCleanup(opt_in.stop)
        # These cases exercise selection/input mechanics. Protocol and startup
        # observation have their own fake-adapter regressions.
        daemon = patch.object(iterm_tab, "inspect_codex_daemon")
        daemon.start()
        self.addCleanup(daemon.stop)

        async def submit(app, session, command, **_):
            await iterm_tab.send(session, command, allow_tabs=True)

        startup = patch.object(iterm_tab, "submit_iterm_launch", side_effect=submit)
        startup.start()
        self.addCleanup(startup.stop)
        # Claude folder trust has its own tests (ClaudeTrustTests); keep host homes untouched.
        trust = patch.object(iterm_tab, "claude_trust_writes", return_value=[])
        trust.start()
        self.addCleanup(trust.stop)

    def assert_file_settings_argument(self, command, path, brief):
        args = shlex.split(command)
        separator = args.index("--")
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--settings")
        options, _ = parser.parse_known_args(args[:separator])
        self.assertEqual(options.settings, str(path))
        self.assertEqual(args[separator + 1:], [brief])

    def test_claude_launch_sets_a_launch_window_so_the_autocompact_offer_cannot_block(self):
        def autocompact(command):
            args = shlex.split(command)
            separator = args.index("--")
            parser = argparse.ArgumentParser(add_help=False)
            parser.add_argument("--autocompact", action="append")
            options, _ = parser.parse_known_args(args[:separator])
            self.assertEqual(args[separator + 1:], ["brief"])
            return options.autocompact

        self.assertEqual(autocompact(iterm_tab.append_brief("cd /work && exec claude", "brief")), ["auto"])
        for option in ("--autocompact 500k", "--autocompact=500k"):
            with self.subTest(option=option):
                self.assertEqual(autocompact(iterm_tab.append_brief(f"claude {option}", "brief")), ["500k"])
        self.assertNotIn("--autocompact", iterm_tab.append_brief("codex --remote unix://", "brief"))

    def test_every_claude_account_home_launches_without_the_question_picker_or_auto_memory(self):
        default_home = ACCOUNTS_TOML.replace('env = { CLAUDE_CONFIG_DIR = "~/claude-main" }', 'env = {}')
        homes = [(text, name) for text in (ACCOUNTS_TOML, default_home) for name in ("main", "spare")]
        with tempfile.TemporaryDirectory() as folder:
            fake_claude = Path(folder) / "claude"
            fake_claude.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            fake_claude.chmod(0o700)
            launch = f"cd / && exec {shlex.quote(str(fake_claude))}"
            commands = [iterm_tab.append_brief(launch, "brief")]
            for text, name in homes:
                choice = account_choice.choose("anthropic", accounts_config(text), AccountChoiceTests().snapshot(), None, name=name)
                commands.append(iterm_tab.with_account(launch, choice, "brief"))
            for command in commands:
                with self.subTest(command=command):
                    args = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, check=True).stdout.splitlines()
                    separator = args.index("--")
                    parser = argparse.ArgumentParser(add_help=False)
                    parser.add_argument("--disallowedTools", action="append")
                    parser.add_argument("--settings")
                    options, _ = parser.parse_known_args(args[:separator])
                    self.assertEqual(options.disallowedTools, [iterm_tab.CLAUDE_QUESTION_TOOL])
                    self.assertIs(json.loads(options.settings)[iterm_tab.CLAUDE_AUTO_MEMORY_SETTING], False)
                    self.assertEqual(args[separator + 1:], ["brief"])
        self.assertNotIn(iterm_tab.CLAUDE_QUESTION_TOOL, iterm_tab.append_brief("codex --remote unix://", "brief"))

    def test_codex_launch_turns_off_memory_generation_over_the_command_file(self):
        for launch in ("codex --remote unix://", f"cd /work && exec codex -c {MEMORY_SETTING}=true"):
            with self.subTest(launch=launch):
                args = shlex.split(iterm_tab.append_brief(launch, "brief"))
                flags = args[:args.index("--")]
                overrides = [flags[i + 1] for i, flag in enumerate(flags[:-1]) if flag == "-c"]
                self.assertEqual(overrides[-1], f"{MEMORY_SETTING}=false")
                self.assertEqual(args[-1], "brief")

    def test_claude_launch_preserves_existing_settings_and_overrides_unattended_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = {"model": "fixture-model", "permissions": {"allow": ["Read"]},
                        iterm_tab.CLAUDE_SUGGESTION_SETTING: True, iterm_tab.CLAUDE_AUTO_MEMORY_SETTING: True}
            path = root / "settings.json"
            path.write_text(json.dumps(settings))
            for option in ("--settings " + shlex.quote(json.dumps(settings)),
                           "--settings=" + shlex.quote(json.dumps(settings))):
                with self.subTest(option=option):
                    command = iterm_tab.append_brief(f"cd {shlex.quote(str(root))} && exec claude {option}", "brief")
                    args = shlex.split(command)
                    # Claude's single-value option uses the final occurrence.
                    index = max(i for i, arg in enumerate(args) if arg == "--settings")
                    received = json.loads(args[index + 1])
                    self.assertEqual(received["model"], settings["model"])
                    self.assertEqual(received["permissions"], settings["permissions"])
                    self.assertIs(received[iterm_tab.CLAUDE_SUGGESTION_SETTING], False)
                    self.assertIs(received[iterm_tab.CLAUDE_AUTO_MEMORY_SETTING], False)
                    self.assertEqual(args[-1], "brief")

            with self.assertRaises(ValueError):
                iterm_tab.append_brief(f"cd {shlex.quote(str(root))} && exec claude --settings settings.json", "brief")
            settings[iterm_tab.CLAUDE_SUGGESTION_SETTING] = False
            path.write_text(json.dumps(settings))
            with self.assertRaises(ValueError):
                iterm_tab.append_brief(f"cd {shlex.quote(str(root))} && exec claude --settings settings.json", "brief")
            settings[iterm_tab.CLAUDE_AUTO_MEMORY_SETTING] = False
            settings["env"] = {"FIXTURE_PRIVATE_VALUE": "opaque-fixture-value"}
            path.write_text(json.dumps(settings))
            command = iterm_tab.append_brief(f"cd {shlex.quote(str(root))} && exec claude --settings settings.json", "brief")
            self.assertNotIn(settings["env"]["FIXTURE_PRIVATE_VALUE"], command)
            self.assert_file_settings_argument(command, "settings.json", "brief")
            self.assertEqual(json.loads(path.read_text()), settings)

    def test_iterm_read_preserves_screen_when_style_data_is_unavailable(self):
        text = "❯ draft"
        for line in (SimpleNamespace(string=text, string_at=lambda x: text[x]),
                     SimpleNamespace(string=text, string_at=lambda x: "", style_at=lambda x: None)):
            screen = SimpleNamespace(number_of_lines=1, line=lambda i: line)
            self.assertEqual(iterm_tab.iterm_input_line(screen)["style"], "unknown")

    def test_claude_settings_file_uses_explicit_directory_and_stays_opaque(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "settings.json"
            path.write_text(json.dumps(dict.fromkeys(iterm_tab.CLAUDE_UNATTENDED_SETTINGS, False)))
            with self.assertRaises(ValueError):
                iterm_tab.append_brief("claude --settings settings.json", "brief")
            with self.assertRaises(ValueError):
                iterm_tab.append_brief("cd relative&&exec claude --settings settings.json", "brief")
            command = iterm_tab.append_brief(f"cd {shlex.quote(str(root))}&&exec claude --settings settings.json", "brief")
            self.assert_file_settings_argument(command, "settings.json", "brief")
            command = iterm_tab.append_brief(f"claude --settings {shlex.quote(str(path))}", "brief")
            self.assertIn(shlex.quote(str(path)), command)

    def test_claude_launch_overrides_inherited_suggestion_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            child = Path(folder) / "claude"
            child.write_text(f'#!{sys.executable}\nimport os\nprint(os.getenv({iterm_tab.CLAUDE_SUGGESTION_ENV!r}))\n')
            child.chmod(0o700)
            with patch.dict(os.environ, {iterm_tab.CLAUDE_SUGGESTION_ENV: "true"}):
                command = iterm_tab.with_account(f"exec {shlex.quote(str(child))}", {"provider": "anthropic", "env": {}}, "brief")
                result = subprocess.run(["/bin/sh", "-c", command], check=True, capture_output=True, text=True)
            self.assertNotEqual(result.stdout.strip(), "true")
            with self.assertRaises(ValueError):
                iterm_tab.with_account(f"env {iterm_tab.CLAUDE_SUGGESTION_ENV}=true claude", {"provider": "anthropic", "env": {}}, "brief")

    def test_inverse_cursor_suggestion_and_normal_typed_text_on_both_backends(self):
        for rest_dim, expected in ((True, "dim"), (False, "non_dim")):
            ansi = "❯ \x1b[7mc\x1b[0m" + ("\x1b[2m" if rest_dim else "") + "andidate\x1b[0m"
            lines, styles = iterm_tab.ansi_screen(ansi)
            self.assertEqual(iterm_tab.input_line(lines, styles)["style"], expected)
            text = "❯ candidate"
            line = SimpleNamespace(string=text, string_at=lambda x: text[x],
                style_at=lambda x: SimpleNamespace(faint=rest_dim and x > 2, inverse=x == 2))
            screen = SimpleNamespace(number_of_lines=1, line=lambda i: line)
            self.assertEqual(iterm_tab.iterm_input_line(screen)["style"], expected)

    def test_initial_iterm_text_failure_reports_unknown_delivery(self):
        terminal = SimpleNamespace(session_id="exact", async_send_text=AsyncMock(side_effect=RuntimeError("RPC failed")))
        with self.assertRaises(iterm_tab.SendFailure) as caught:
            asyncio.run(iterm_tab.send(terminal, "draft"))
        self.assertIsNone(caught.exception.detail["text_sent"])
        self.assertTrue(caught.exception.detail["input_may_contain_text"])
        self.assertEqual(caught.exception.detail["submission"], "not_attempted")

    def test_tmux_prompt_styles_reset_mixed_and_color_components(self):
        for captured, expected in (("❯ \x1b[2myes\x1b[0m", "dim"),
                                   ("❯ yes", "non_dim"),
                                   ("❯ \x1b[2myes\x1b[22m typed", "mixed"),
                                   ("\x1b[2mstatus\x1b[0m\n❯ yes", "non_dim"),
                                   ("❯ \x1b[38;2;2;22;0myes", "non_dim"),
                                   ("❯ \x1b[38:2:2:22:0myes", "non_dim"),
                                   ("❯ ", "empty"), ("no prompt", "unknown")):
            with self.subTest(captured=captured):
                lines, styles = iterm_tab.ansi_screen(captured)
                result = iterm_tab.input_line(lines, styles)
                self.assertEqual(result["style"], expected)
                self.assertFalse(result["director_input_verified"])

    def test_iterm_read_uses_cell_faint_style_and_missing_style_is_unknown(self):
        for faint, expected in ((True, "dim"), (False, "non_dim"), (None, "unknown")):
            text = "❯ yes"
            line = SimpleNamespace(string=text, string_at=lambda x: text[x],
                                   style_at=lambda x: None if faint is None else SimpleNamespace(faint=faint, inverse=False))
            screen = SimpleNamespace(number_of_lines=1, line=lambda i: line)
            terminal = SimpleNamespace(session_id="exact", async_get_screen_contents=AsyncMock(return_value=screen))
            app = SimpleNamespace(terminal_windows=[SimpleNamespace(tabs=[SimpleNamespace(sessions=[terminal])])])
            with self.subTest(faint=faint):
                result = asyncio.run(iterm_tab.operate(app, argparse.Namespace(command="read", session_id="exact")))
                self.assertEqual(result["screen"], [text])
                self.assertEqual(result["input_line"]["style"], expected)
                self.assertFalse(result["input_line"]["director_input_verified"])

    def test_return_failure_reports_unknown_submission_with_typed_text(self):
        terminal = SimpleNamespace(session_id="exact", async_send_text=AsyncMock(
            side_effect=[None, RuntimeError("RPC failed")]))
        with self.assertRaises(iterm_tab.SendFailure) as caught:
            asyncio.run(iterm_tab.send(terminal, "draft"))
        self.assertEqual(caught.exception.detail["submission"], "unknown")
        self.assertTrue(caught.exception.detail["text_sent"])
    def test_google_launch_loads_config_preserves_brief_and_writes_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config_path = root / "supervisor.toml"
            config_path.write_text(GOOGLE_TOML)
            child = root / "agy"
            child.write_text(f'#!{sys.executable}\nimport argparse\n'
                             'parser = argparse.ArgumentParser()\n'
                             'parser.add_argument("--prompt-interactive", required=True)\n'
                             'print(parser.parse_args().prompt_interactive, end="")\n')
            child.chmod(0o700)
            launch = root / "launch.txt"
            launch.write_text("cd / && exec " + shlex.quote(str(child)))
            args = account_launch_args(launch)
            args.account_provider, args.account_config = "google", config_path
            brief = "--Google brief\nKeep $(touch injected) literal.\n" + BRIEF
            args.brief_file[0].write_text(brief)
            terminal, window, app = launch_terminal_fixture()
            with (patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
                  patch.object(account_choice, "read_snapshot", return_value=(google_snapshot(), None)),
                  patch.object(account_choice, "receipt_route", return_value={"storage_root": root})):
                result = asyncio.run(iterm_tab.operate(app, args))
            sent = terminal.async_send_text.await_args_list[0].args[0]
            command = sent.removeprefix("\x1b[200~").removesuffix("\x1b[201~")
            completed = subprocess.run(["/bin/sh", "-c", command], cwd=root,
                                       capture_output=True, text=True, check=True)
            self.assertEqual(completed.stdout, brief)
            self.assertEqual(result["account"]["account_binding"], account_choice.GOOGLE_ACCOUNT_BINDING)
            self.assertEqual(result["account"]["env_keys"], [])
            receipts = list((root / "Launch Receipts").glob("*.json"))
            self.assertEqual(len(receipts), 1)
            self.assertEqual(json.loads(receipts[0].read_text())["accountID"], result["account"]["account_id"])

    def test_google_wrong_harness_or_home_override_refuses_before_tabs_and_receipts(self):
        choice = account_choice.choose("google", accounts_config(GOOGLE_TOML), google_snapshot(), None)
        with tempfile.TemporaryDirectory() as folder:
            launch = Path(folder) / "launch.txt"
            terminal, window, app = launch_terminal_fixture()
            for command in ("codex", "claude", "HOME=/alt agy", "cd / && env HOME=/alt exec agy",
                            "CODEX_HOME=/alt agy"):
                launch.write_text(command)
                args = account_launch_args(launch)
                args.account_provider = "google"
                with (self.subTest(command=command),
                      patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
                      patch.object(account_choice, "select", return_value=choice),
                      patch.object(account_choice, "prepare_launch") as prepare,
                      patch.object(account_choice, "record_launch") as receipt,
                      self.assertRaises(ValueError)):
                    asyncio.run(iterm_tab.operate(app, args))
                prepare.assert_not_called()
                receipt.assert_not_called()
                window.async_create_tab.assert_not_called()

    def test_brief_cannot_be_discarded_by_a_comment_or_shell_wrapper(self):
        for command in ("codex --remote unix:// # launch", "bash -c 'codex'",
                        "codex --remote unix://;", "codex --remote unix:// &&",
                        "codex --remote unix:// && exit", "dash -c 'codex'",
                        "/bin/ksh -c 'codex'"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                iterm_tab.append_brief(command, "task")

    def test_settings_paths_do_not_count_as_shell_wrappers(self):
        command = iterm_tab.append_brief("cd /work/sh && exec codex --add-dir /work/zsh", "task")
        self.assertEqual(shlex.split(command)[-1], "task")

    def test_quoted_settings_keep_shell_punctuation_as_data(self):
        for settings in ('claude --allowedTools="Bash(git status)"', "codex -c key='a;b'",
                         "codex -c key='a #b'", "cd '/work/space dir' && exec codex"):
            with self.subTest(settings=settings):
                command = iterm_tab.append_brief(settings, "task")
                self.assertEqual(shlex.split(command)[-1], "task")

    def test_brief_reaches_agent_as_one_literal_argument(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            child = root / "claude"
            child.write_text(f'#!{sys.executable}\nimport json, sys\n'
                             'args = sys.argv[1:]\n'
                             'prompt = args[args.index("--") + 1:]\n'
                             'print(len(prompt))\nprint(prompt[0])\n')
            child.chmod(0o700)
            launch = root / "launch.txt"
            launch.write_text("cd / && exec " + shlex.quote(str(child)))
            brief = root / "brief.txt"
            content = "--Don't substitute $(touch injected) or `touch injected`\n\tFollow the issue.\n" + BRIEF
            brief.write_text(content)
            args = account_launch_args(launch)
            args.brief_file = [brief]
            terminal, window, app = launch_terminal_fixture()
            with (
                patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
                patch.object(account_choice, "select", return_value={"provider": "anthropic", "env": {}}),
                patch.object(account_choice, "prepare_launch"),
                patch.object(account_choice, "record_launch"),
            ):
                asyncio.run(iterm_tab.operate(app, args))
            sent = terminal.async_send_text.await_args_list[0].args[0]
            command = sent.removeprefix("\x1b[200~").removesuffix("\x1b[201~")
            result = subprocess.run(["/bin/sh", "-c", command], cwd=root,
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, "1\n" + content + "\n")
            self.assertFalse((root / "injected").exists())

    def test_invalid_batch_brief_refuses_every_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = [root / "first.txt", root / "second.txt"]
            for path in files:
                path.write_text("codex --remote unix://")
            args = account_launch_args(files)
            args.brief_file[1].write_text("")
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            with (
                patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
                patch.object(account_choice, "select_batch") as select,
                patch.object(account_choice, "record_launch") as receipt,
            ):
                for briefs in (args.brief_file, args.brief_file[:1], None):
                    args.brief_file = briefs
                    with self.subTest(briefs=briefs), self.assertRaises(ValueError):
                        asyncio.run(iterm_tab.operate(app, args))
                    select.assert_not_called()
                    receipt.assert_not_called()
                    window.async_create_tab.assert_not_awaited()

    def test_missing_or_empty_brief_refuses_before_launch_side_effects(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launch = root / "launch.txt"
            launch.write_text("codex --remote unix://")
            brief = root / "brief.txt"
            terminal, window, app = launch_terminal_fixture()
            args = argparse.Namespace(command="new", window_id="chosen", command_file=[launch],
                                      brief_file=[brief], account_provider="openai",
                                      account=None, account_config=None)
            with (
                patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
                patch.object(account_choice, "select", return_value={"provider": "openai", "env": {}}) as select,
                patch.object(account_choice, "prepare_launch") as prepare,
                patch.object(account_choice, "record_launch") as receipt,
                patch.object(iterm_tab, "check_codex_daemon") as daemon,
            ):
                rules = iterm_tab.brief_rules_block()
                middle = len(rules) // 2
                edited = rules[:middle] + " edited " + rules[middle:]
                copy = root / "old" / "skills" / "supervisor" / "references" / "brief-rules.md"
                copy.parent.mkdir(parents=True)
                copy.write_text(f"<!-- brief-rules:start -->\n{edited}\n<!-- brief-rules:end -->\n")
                stale = (None, "", "   \n", "Do the task.",
                         "Rules: https://github.com/o/r/blob/0a1b2c3/skills/supervisor/references/brief-rules.md",
                         f"Rules: {iterm_tab.BRIEF_RULES_URL}.old", f"Rules: {iterm_tab.BRIEF_RULES_URL}%2Eold",
                         f"Rules: {iterm_tab.BRIEF_RULES_URL}/other.md", f"Rules: {iterm_tab.BRIEF_RULES}/other.md",
                         "Rules: skills/supervisor/references/brief-rules.md",
                         f"Rules: {copy}", f"Rules: {root / 'missing' / copy.relative_to(root / 'old')}",
                         "Do the task.\n" + edited)
                for content in stale:
                    if content is not None:
                        brief.write_text(content)
                    with self.subTest(content=content), self.assertRaises((ValueError, OSError)):
                        asyncio.run(iterm_tab.operate(app, args))
                    select.assert_not_called()
                    prepare.assert_not_called()
                    receipt.assert_not_called()
                    daemon.assert_not_called()
                    window.async_create_tab.assert_not_awaited()
                    terminal.async_send_text.assert_not_awaited()

    def test_brief_links_or_includes_the_current_rules(self):
        rules = iterm_tab.brief_rules_block()
        for brief in (f"Do the task.\nRules for this brief: {iterm_tab.BRIEF_RULES_URL}",
                      f"Read {iterm_tab.BRIEF_RULES_URL}. Then start.",
                      f"Do the task. Rules: ({iterm_tab.BRIEF_RULES}#rules-every-brief-carries)",
                      "Do the task.\n\n" + " ".join(rules.split())):
            with self.subTest(brief=brief):
                iterm_tab.validate_brief_rules(brief)

    def test_codex_daemon_preflight_checks_selected_home_before_tabs_and_receipts(self):
        # Short disposable paths fit macOS's Unix socket path limit.
        with tempfile.TemporaryDirectory(dir="/tmp") as folder:
            root = Path(folder)
            home = root / "selected home's"
            endpoint = home / "app-server-control" / "app-server-control.sock"
            endpoint.parent.mkdir(parents=True)
            choice = {"provider": "openai", "name": "selected", "source": "context-panel",
                      "reason": "use next", "env": {"CODEX_HOME": str(home)}}
            launch = root / "launch.txt"
            write_codex_project_fixture(root, home)
            launch.write_text("codex --remote unix:// -C " + shlex.quote(str(root)))
            terminal = SimpleNamespace(session_id="new", async_send_text=AsyncMock())
            tab = SimpleNamespace(tab_id="tab", current_session=terminal)
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock(return_value=tab))
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            args = argparse.Namespace(command="new", window_id="chosen", command_file=launch, brief_file=fixture_briefs(launch),
                                      account_provider="openai", account=None, account_config=None)
            with (
                patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
                patch.dict(os.environ, {"CODEX_HOME": str(root / "inherited")}),
                patch.object(account_choice, "select", return_value=choice),
                patch.object(account_choice, "prepare_launch") as prepare,
                patch.object(account_choice, "record_launch") as receipt,
                patch.object(iterm_tab.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as start,
            ):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as daemon:
                    daemon.bind(str(endpoint))
                    daemon.listen()
                    result = asyncio.run(iterm_tab.operate(app, args))
                    self.assertEqual(result["session_id"], "new")
                    self.assertEqual(start.call_args.args[0], ["codex", "app-server", "daemon", "start"])
                    self.assertEqual(start.call_args.kwargs["env"]["CODEX_HOME"], str(home))
                    window.async_create_tab.assert_awaited_once_with(select=False)
                    receipt.assert_called_once_with(choice)
                    sent = terminal.async_send_text.await_args_list[0].args[0]
                    self.assertIn(f"export CODEX_HOME={shlex.quote(str(home))}", sent)
                # A closed listener leaves a stale socket, then remove it for the reboot case.
                for state in ("stale", "missing"):
                    if state == "missing":
                        endpoint.unlink()
                    window.async_create_tab.reset_mock()
                    terminal.async_send_text.reset_mock()
                    receipt.reset_mock()
                    prepare.reset_mock()
                    with self.subTest(state=state), self.assertRaises(ValueError) as refused:
                        asyncio.run(iterm_tab.operate(app, args))
                    reason = str(refused.exception)
                    self.assertIn(str(home), reason)
                    recovery = reason.split("run ", 1)[1].split(", then retry", 1)[0]
                    self.assertEqual(shlex.split(recovery), [f"CODEX_HOME={home}", "codex",
                                                           "app-server", "daemon", "start"])
                    window.async_create_tab.assert_not_awaited()
                    terminal.async_send_text.assert_not_awaited()
                    receipt.assert_not_called()
                    prepare.assert_not_called()

    def test_codex_batch_checks_all_homes_before_first_launch(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as folder:
            root = Path(folder)
            home = root / "ready"
            endpoint = home / "app-server-control" / "app-server-control.sock"
            endpoint.parent.mkdir(parents=True)
            choices = [{"provider": "openai", "env": {"CODEX_HOME": str(h)}}
                       for h in (home, root / "down")]
            files = [root / "first.txt", root / "second.txt"]
            for choice in choices:
                write_codex_project_fixture(root, Path(choice["env"]["CODEX_HOME"]))
            for path in files:
                path.write_text("codex --remote unix:// -C " + shlex.quote(str(root)))
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            args = argparse.Namespace(command="new", window_id="chosen", command_file=files, brief_file=fixture_briefs(files),
                                      account_provider="openai", account=None, account_config=None)
            with (
                socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as daemon,
                patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
                patch.object(account_choice, "select_batch", return_value=choices),
                patch.object(account_choice, "prepare_launch") as prepare,
                patch.object(account_choice, "record_launch") as receipt,
                patch.object(iterm_tab.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as start,
            ):
                daemon.bind(str(endpoint))
                daemon.listen()
                with self.assertRaisesRegex(ValueError, str(root / "down")):
                    asyncio.run(iterm_tab.operate(app, args))
                window.async_create_tab.assert_not_awaited()
                prepare.assert_not_called()
                receipt.assert_not_called()
                self.assertEqual([call.kwargs["env"]["CODEX_HOME"] for call in start.call_args_list],
                                 [choice["env"]["CODEX_HOME"] for choice in choices])

    def test_codex_daemon_timeout_refuses_and_closes_probe(self):
        probe = Mock()
        probe.connect.side_effect = TimeoutError()
        adapter = Mock()
        adapter.__enter__ = Mock(return_value=probe)
        adapter.__exit__ = Mock(return_value=False)
        with (
            patch.object(iterm_tab.socket, "socket", return_value=adapter),
            patch.object(iterm_tab.subprocess, "run", return_value=SimpleNamespace(returncode=0)),
        ):
            with self.assertRaisesRegex(ValueError, "TimeoutError"):
                iterm_tab.check_codex_daemon({"provider": "openai", "env": {"CODEX_HOME": "/selected"}})
        self.assertGreater(probe.settimeout.call_args.args[0], 0)
        adapter.__exit__.assert_called_once()

    def test_codex_native_start_recovers_missing_socket(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as folder:
            home = Path(folder) / "selected"
            endpoint = home / "app-server-control" / "app-server-control.sock"
            endpoint.parent.mkdir(parents=True)
            choice = {"provider": "openai", "env": {"CODEX_HOME": str(home)}}
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as daemon:
                def native_start(argv, **kwargs):
                    self.assertFalse(endpoint.exists())
                    self.assertEqual(argv, ["codex", "app-server", "daemon", "start"])
                    self.assertEqual(kwargs["env"]["CODEX_HOME"], str(home))
                    daemon.bind(str(endpoint))
                    daemon.listen()
                    return SimpleNamespace(returncode=0)

                with patch.object(iterm_tab.subprocess, "run", side_effect=native_start) as start:
                    self.assertIsNone(iterm_tab.check_codex_daemon(choice))
                    start.assert_called_once()

    def test_codex_start_failure_refuses_before_socket_tabs_and_receipts(self):
        failures = [
            (SimpleNamespace(returncode=1, stderr="native startup failed", stdout=""), "native startup failed"),
            (SimpleNamespace(returncode=2, stderr="", stdout="startup diagnostic"), "startup diagnostic"),
            (FileNotFoundError("codex unavailable"), "FileNotFoundError"),
            (subprocess.TimeoutExpired("codex", 30), "TimeoutExpired"),
        ]
        with tempfile.TemporaryDirectory() as folder:
            launch = Path(folder) / "launch.txt"
            choice = {"provider": "openai", "env": {"CODEX_HOME": str(Path(folder) / "selected")}}
            write_codex_project_fixture(Path(folder), Path(choice["env"]["CODEX_HOME"]))
            launch.write_text("codex --remote unix:// -C " + shlex.quote(folder))
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            args = argparse.Namespace(command="new", window_id="chosen", command_file=launch, brief_file=fixture_briefs(launch),
                                      account_provider="openai", account=None, account_config=None)
            for failure, reason in failures:
                probe = Mock()
                with (
                    self.subTest(reason=reason),
                    patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
                    patch.object(account_choice, "select", return_value=choice),
                    patch.object(account_choice, "prepare_launch") as prepare,
                    patch.object(account_choice, "record_launch") as receipt,
                    patch.object(iterm_tab.subprocess, "run") as start,
                ):
                    if isinstance(failure, Exception):
                        start.side_effect = failure
                    else:
                        start.return_value = failure
                    # Initialize asyncio's own socketpair before mocking the daemon probe.
                    with asyncio.Runner() as runner:
                        runner.get_loop()
                        with patch.object(socket, "socket", probe), self.assertRaisesRegex(ValueError, reason) as refused:
                            runner.run(iterm_tab.operate(app, args))
                    self.assertIn(choice["env"]["CODEX_HOME"], str(refused.exception))
                    self.assertGreater(start.call_args.kwargs["timeout"], 0)
                    probe.assert_not_called()
                    window.async_create_tab.assert_not_awaited()
                    prepare.assert_not_called()
                    receipt.assert_not_called()

    def test_claude_preflight_never_starts_codex(self):
        with patch.object(iterm_tab.subprocess, "run") as start, patch.object(iterm_tab.socket, "socket") as probe:
            self.assertIsNone(iterm_tab.check_codex_daemon({"provider": "anthropic", "env": {}}))
        start.assert_not_called()
        probe.assert_not_called()

    def test_default_claude_launch_clears_inherited_profile_after_cd(self):
        config = accounts_config(ACCOUNTS_TOML.replace(
            'env = { CLAUDE_CONFIG_DIR = "~/claude-main" }', 'env = {}'
        ))
        choice = account_choice.choose("anthropic", config, AccountChoiceTests().snapshot(), None, name="main")
        with tempfile.TemporaryDirectory() as folder:
            fake_claude = Path(folder) / "claude"
            fake_claude.write_text('#!/bin/sh\nprintf "%s\\n" "${CLAUDE_CONFIG_DIR+set}" "$1"\n')
            fake_claude.chmod(0o700)
            command = iterm_tab.with_account(f'cd / && {shlex.quote(str(fake_claude))} brief', choice)
            launched = subprocess.run(
                ["/bin/sh", "-c", command], capture_output=True, text=True, check=True,
                env={**os.environ, "CLAUDE_CONFIG_DIR": "/inherited/alternate"},
            )
            self.assertEqual(launched.stdout.splitlines(), ["", "brief"])
        self.assertIn(account_choice.ACCOUNT_VARIABLES["anthropic"],
                      iterm_tab.account_settings("env CLAUDE_CONFIG_DIR=/other claude",
                                                 {account_choice.ACCOUNT_VARIABLES["anthropic"]}))
        with self.assertRaises(ValueError):
            iterm_tab.with_account("env CLAUDE_CONFIG_DIR=/other claude", choice)

    def test_exact_session_and_ambiguity(self):
        one = SimpleNamespace(session_id="id1")
        two = SimpleNamespace(session_id="id2")
        app = SimpleNamespace(
            terminal_windows=[
                SimpleNamespace(tabs=[SimpleNamespace(sessions=[one, two])])
            ]
        )
        self.assertIs(iterm_tab.select_session(app, "id1"), one)
        with self.assertRaises(ValueError):
            iterm_tab.select_session(app, "id")
        two.session_id = "id1"
        with self.assertRaises(ValueError):
            iterm_tab.select_session(app, "id1")

    def test_send_separate_return_and_broadcast_suppression(self):
        terminal = SimpleNamespace(async_send_text=AsyncMock())
        asyncio.run(iterm_tab.send(terminal, "brief"))
        self.assertEqual(terminal.async_send_text.await_args_list[0].args, ("brief",))
        self.assertEqual(terminal.async_send_text.await_args_list[1].args, ("\r",))
        self.assertTrue(
            all(
                call.kwargs["suppress_broadcast"]
                for call in terminal.async_send_text.await_args_list
            )
        )
        with self.assertRaises(ValueError):
            asyncio.run(iterm_tab.send(terminal, "\x1bunsafe"))

    def test_multiline_message_uses_bracketed_paste(self):
        terminal = SimpleNamespace(async_send_text=AsyncMock())
        asyncio.run(iterm_tab.send(terminal, "two\nlines"))
        self.assertEqual(
            terminal.async_send_text.await_args_list[0].args,
            ("\x1b[200~two\nlines\x1b[201~",),
        )

    def test_launch_explicit_window_without_moving_focus(self):
        terminal = SimpleNamespace(session_id="new", async_send_text=AsyncMock())
        tab = SimpleNamespace(tab_id="newtab", current_session=terminal)
        previous = SimpleNamespace(async_select=AsyncMock())
        window = SimpleNamespace(
            window_id="chosen", async_create_tab=AsyncMock(return_value=tab)
        )
        app = SimpleNamespace(
            terminal_windows=[window],
            current_terminal_window=SimpleNamespace(current_tab=previous),
        )
        with tempfile.TemporaryDirectory() as folder:
            command = Path(folder) / "launch.txt"
            command.write_text("codex\n")
            args = argparse.Namespace(
                command="new", window_id="chosen", command_file=command, brief_file=fixture_briefs(command)
            )
            with patch.dict("sys.modules", {"iterm2": SimpleNamespace()}):
                result = asyncio.run(iterm_tab.operate(app, args))
        self.assertEqual(result["session_id"], "new")
        window.async_create_tab.assert_awaited_once_with(select=False)
        previous.async_select.assert_not_awaited()
        self.assertEqual(terminal.async_send_text.await_count, 2)
        self.assertEqual(terminal.async_send_text.await_args_list[-1].args, ("\r",))

    def test_late_session_launches_only_in_refreshed_created_tab(self):
        terminal = SimpleNamespace(session_id="late", async_send_text=AsyncMock())
        created = SimpleNamespace(tab_id="newtab", current_session=None)
        # iTerm can replace hierarchy objects during a refresh.
        refreshed = SimpleNamespace(tab_id="newtab", current_session=terminal)
        previous = SimpleNamespace(async_select=AsyncMock(), async_send_text=AsyncMock())
        window = SimpleNamespace(
            window_id="chosen", async_create_tab=AsyncMock(return_value=created)
        )
        app = SimpleNamespace(
            terminal_windows=[window],
            current_terminal_window=SimpleNamespace(current_tab=previous),
            async_refresh=AsyncMock(),
            get_tab_by_id=Mock(side_effect=[created, refreshed]),
        )
        with tempfile.TemporaryDirectory() as folder:
            command = Path(folder) / "launch.txt"
            command.write_text("codex\n")
            args = argparse.Namespace(command="new", window_id="chosen", command_file=command, brief_file=fixture_briefs(command))
            with patch.dict("sys.modules", {"iterm2": SimpleNamespace()}):
                result = asyncio.run(iterm_tab.operate(app, args))
        self.assertEqual(result["session_id"], "late")
        self.assertEqual(app.async_refresh.await_count, 2)
        self.assertTrue(all(call.args == ("newtab",) for call in app.get_tab_by_id.call_args_list))
        window.async_create_tab.assert_awaited_once_with(select=False)
        previous.async_select.assert_not_awaited()
        previous.async_send_text.assert_not_awaited()
        self.assertEqual(terminal.async_send_text.await_count, 2)
        self.assertEqual(terminal.async_send_text.await_args_list[-1].args, ("\r",))

    def test_session_wait_timeout_covers_stalled_refresh(self):
        async def stalled_refresh():
            await asyncio.Future()

        tab = SimpleNamespace(tab_id="newtab", current_session=None)
        for refresh in (AsyncMock(), AsyncMock(side_effect=stalled_refresh)):
            with self.subTest(stalled=bool(refresh.side_effect)):
                app = SimpleNamespace(
                    async_refresh=refresh,
                    get_tab_by_id=Mock(return_value=tab),
                )
                with self.assertRaisesRegex(ValueError, "newtab.*no session after"):
                    asyncio.run(iterm_tab.wait_for_session(app, tab, timeout=0.01))

    def test_launch_timeout_never_sends_or_moves_focus(self):
        previous = SimpleNamespace(async_select=AsyncMock(), async_send_text=AsyncMock())
        tab = SimpleNamespace(tab_id="newtab", current_session=None)
        window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock(return_value=tab))
        app = SimpleNamespace(
            terminal_windows=[window],
            current_terminal_window=SimpleNamespace(current_tab=previous),
            async_refresh=AsyncMock(),
            get_tab_by_id=Mock(return_value=tab),
        )
        wait = iterm_tab.wait_for_session

        async def short_wait(target_app, target_tab):
            return await wait(target_app, target_tab, timeout=0.01)

        with tempfile.TemporaryDirectory() as folder:
            command = Path(folder) / "launch.txt"
            command.write_text("codex\n")
            args = argparse.Namespace(command="new", window_id="chosen", command_file=command, brief_file=fixture_briefs(command))
            with (
                patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
                patch.object(iterm_tab, "wait_for_session", side_effect=short_wait),
                patch.object(iterm_tab, "send", new_callable=AsyncMock) as send,
                self.assertRaisesRegex(ValueError, "newtab.*no session after"),
            ):
                asyncio.run(iterm_tab.operate(app, args))
            send.assert_not_awaited()
        window.async_create_tab.assert_awaited_once_with(select=False)
        previous.async_select.assert_not_awaited()
        previous.async_send_text.assert_not_awaited()

    def test_closed_new_tab_does_not_fall_back_to_another_session(self):
        terminal = SimpleNamespace(async_send_text=AsyncMock())
        app = SimpleNamespace(
            async_refresh=AsyncMock(),
            get_tab_by_id=Mock(return_value=None),
            current_terminal_window=SimpleNamespace(current_tab=SimpleNamespace(current_session=terminal)),
        )
        for tab in (None, SimpleNamespace(tab_id="closed", current_session=None)):
            with self.subTest(tab=tab), self.assertRaises(ValueError):
                asyncio.run(iterm_tab.wait_for_session(app, tab))
        terminal.async_send_text.assert_not_awaited()

    def test_codex_remote_launch_inherits_selected_home_after_cd(self):
        rows = [account_row("main", 0.6, []), account_row("spare", 0.5, [], cfg="cfg-spare")]
        for row in rows:
            row["provider"] = "openai"
            row["id"] = row["id"].replace("anthropic", "openai")
        config = accounts_config(ACCOUNTS_TOML.replace("anthropic", "openai").replace("CLAUDE_CONFIG_DIR", "CODEX_HOME"))
        choice = account_choice.choose("openai", config, {
            "accounts": rows,
            "answers": {"useNext": [{"provider": "openai", "accountID": "openai-def"}]},
        }, None, now=NOW)
        # Stand in for the CLI without reading auth or starting a real session.
        with tempfile.TemporaryDirectory() as folder:
            fake_codex = Path(folder) / "codex"
            fake_codex.write_text('#!/bin/sh\nprintf "%s\\n" "$CODEX_HOME" "$@"\n')
            fake_codex.chmod(0o700)
            settings = ["--remote", "unix://", "-c", 'model="test-model"', "-c", "model_reasoning_effort=medium"]
            command = iterm_tab.with_account(f"cd / && {shlex.quote(str(fake_codex))} {shlex.join(settings)}", choice)
            launched = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, check=True)
        self.assertEqual(launched.stdout.splitlines(), [choice["env"]["CODEX_HOME"], *settings])

    def test_launch_on_chosen_account_prefixes_env_only(self):
        terminal = SimpleNamespace(session_id="new", async_send_text=AsyncMock())
        tab = SimpleNamespace(tab_id="newtab", current_session=terminal)
        window = SimpleNamespace(
            window_id="chosen", async_create_tab=AsyncMock(return_value=tab)
        )
        app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
        choice = account_choice.decision(
            accounts_config()["accounts"][1], "context-panel", "resets soonest"
        )
        with tempfile.TemporaryDirectory() as folder:
            command = Path(folder) / "launch.txt"
            child = Path(folder) / "claude"
            child.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                             'args = sys.argv[1:]\n'
                             'print(os.getenv("CLAUDE_CONFIG_DIR"))\n'
                             'print(os.getenv("CLAUDE_ACCOUNT_MOVE_ENABLED"))\n'
                             'print(args[args.index("--") + 1])\n')
            child.chmod(0o700)
            command.write_text("cd / && " + shlex.quote(str(child)) + "\n")
            args = account_launch_args(command)
            with (
                patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
                patch.object(account_choice, "select", return_value=choice),
                patch.object(account_choice, "record_launch") as receipt,
                patch.object(account_choice, "prepare_launch"),
            ):
                result = asyncio.run(iterm_tab.operate(app, args))
                command.write_text("env CLAUDE_CONFIG_DIR=/other claude\n")
                with self.assertRaisesRegex(ValueError, "already sets"):
                    asyncio.run(iterm_tab.operate(app, args))
            sent = terminal.async_send_text.await_args_list[0].args[0]
            if sent.startswith("\x1b[200~"):
                sent = sent.removeprefix("\x1b[200~").removesuffix("\x1b[201~")
            launched = subprocess.run(["/bin/sh", "-c", sent], capture_output=True, text=True, check=True)
            self.assertEqual(launched.stdout.splitlines(), [choice["env"]["CLAUDE_CONFIG_DIR"], "1", BRIEF])
        window.async_create_tab.assert_awaited_once_with(select=False)
        receipt.assert_called_once_with(choice)
        self.assertEqual(result["account"]["env_keys"], ["CLAUDE_CONFIG_DIR"])
        self.assertNotIn("env", result["account"])

    def test_batch_creates_one_count_only_receipt_before_each_command(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = [root / "first.txt", root / "second.txt"]
            for index, path in enumerate(files):
                path.write_text(f"claude brief-{index}")
            snapshot = AccountChoiceTests().snapshot()
            config = accounts_config()
            config["snapshot_command"] = ["reader", "--storage-root", str(root)]
            counts_at_send = []
            async def submit(_text, **_kwargs):
                counts_at_send.append(len(list((root / "Launch Receipts").glob("*.json"))))
            terminals = [SimpleNamespace(session_id=f"session-{i}", async_send_text=AsyncMock(side_effect=submit)) for i in range(2)]
            tabs = [SimpleNamespace(tab_id=f"tab-{i}", current_session=t) for i, t in enumerate(terminals)]
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock(side_effect=tabs))
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            args = account_launch_args(files)
            with patch.dict("sys.modules", {"iterm2": SimpleNamespace()}), patch.object(account_choice, "load_config", return_value=config), patch.object(account_choice, "read_snapshot", return_value=(snapshot, None)):
                results = asyncio.run(iterm_tab.operate(app, args))
            self.assertEqual([r["account"]["name"] for r in results], ["spare", "main"])
            self.assertEqual(counts_at_send, [1, 1, 2, 2])
            receipts = [json.loads(p.read_text()) for p in (root / "Launch Receipts").glob("*.json")]
            self.assertCountEqual([r["accountID"] for r in receipts], ["anthropic-def", "anthropic-abc"])
            for receipt in receipts:
                self.assertEqual(set(receipt), {"schemaVersion", "provider", "accountID", "launchedAt"})

    def test_new_cli_requires_provider_before_connecting_to_iterm(self):
        fake = SimpleNamespace(run_until_complete=Mock())
        with patch.dict("sys.modules", {"iterm2": fake}), patch.object(sys, "argv", ["iterm_tab.py", "new", "--window-id", "window", "--command-file", "launch.txt", "--brief-file", "brief.txt"]):
            with self.assertRaises(SystemExit) as failed:
                iterm_tab.main()
        self.assertEqual(failed.exception.code, 2)
        fake.run_until_complete.assert_not_called()

    def test_account_setters_reject_override_but_briefs_and_other_variables_work(self):
        choice = account_choice.decision(accounts_config()["accounts"][1], "context-panel", "test")
        for command in ("CLAUDE_CONFIG_DIR=/other claude brief", "cd / && env CLAUDE_CONFIG_DIR=/other claude brief",
                        "export CLAUDE_CONFIG_DIR=/other && claude brief", "declare -x CLAUDE_CONFIG_DIR=/other; claude brief",
                        "unset CLAUDE_CONFIG_DIR && claude brief", "env -u CLAUDE_CONFIG_DIR claude brief",
                        "env --unset=CLAUDE_CONFIG_DIR claude brief", "env -uCLAUDE_CONFIG_DIR claude brief",
                        "exec env CLAUDE_CONFIG_DIR=/other claude brief", "command env CLAUDE_CONFIG_DIR=/other claude brief",
                        "time CLAUDE_CONFIG_DIR=/other claude brief", "{ CLAUDE_CONFIG_DIR=/other claude brief; }",
                        "zsh -lc 'CLAUDE_CONFIG_DIR=/other claude brief'", "eval 'CLAUDE_CONFIG_DIR=/other claude brief'",
                        "env -P /usr/bin CLAUDE_CONFIG_DIR=/other claude brief", "env -S 'CLAUDE_CONFIG_DIR=/other claude brief'",
                        "env -i claude brief", "env - claude brief", "readonly CLAUDE_CONFIG_DIR=/other; claude brief",
                        "CLAUDE_CONFIG_DIR+=/other claude brief",
                        "bash --noprofile --norc -c 'CLAUDE_CONFIG_DIR=/other claude brief'",
                        "env TERM=xterm-256color zsh -lc 'CLAUDE_CONFIG_DIR=/other claude brief'",
                        "env FOO=1 env CLAUDE_CONFIG_DIR=/other claude brief",
                        "FOO=1 exec env CLAUDE_CONFIG_DIR=/other claude brief",
                        "fish -c 'set -gx CLAUDE_CONFIG_DIR /other; claude brief'",
                        "sh -c -- 'CLAUDE_CONFIG_DIR=/other claude brief'",
                        "zsh -c -l 'CLAUDE_CONFIG_DIR=/other claude brief'",
                        "fish --command 'set -gx CLAUDE_CONFIG_DIR /other; claude brief'",
                        "fish --command='set -gx CLAUDE_CONFIG_DIR /other; claude brief'",
                        "cd /x; and set -gx CLAUDE_CONFIG_DIR /other; and claude brief",
                        "env -S'CLAUDE_CONFIG_DIR=/other claude brief'",
                        "env --split-string='CLAUDE_CONFIG_DIR=/other claude brief'"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.assertIn(account_choice.ACCOUNT_VARIABLES["anthropic"],
                              iterm_tab.account_settings(command, {account_choice.ACCOUNT_VARIABLES["anthropic"]}))
                iterm_tab.with_account(command, choice)
        for command in ("bash --noprofile --norc -c 'claude brief'",
                        "env TERM=xterm-256color claude 'Discuss CLAUDE_CONFIG_DIR=/x'",
                        "fish -c 'set -gx TERM xterm; claude brief'"):
            with self.subTest(allowed=command):
                self.assertFalse(iterm_tab.account_settings(
                    command, account_choice.ACCOUNT_VARIABLES.values()))
        with self.assertRaisesRegex(ValueError, "already sets CODEX_HOME"):
            iterm_tab.with_account("CODEX_HOME=/manual codex brief", choice)
        command = "MY_CLAUDE_CONFIG_DIR=example claude 'Discuss CLAUDE_CONFIG_DIR=/example and CODEX_HOME=/example'"
        prepared = iterm_tab.with_account(command, choice)
        with tempfile.TemporaryDirectory() as folder:
            cli = Path(folder) / "claude"
            cli.write_text('#!/bin/sh\nprintf "%s\\n" "$CLAUDE_CONFIG_DIR" "$1"\n')
            cli.chmod(0o700)
            result = subprocess.run(["/bin/sh", "-c", prepared], env={**os.environ, "PATH": folder + ":" + os.environ["PATH"]}, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.splitlines(), [choice["env"]["CLAUDE_CONFIG_DIR"], "Discuss CLAUDE_CONFIG_DIR=/example and CODEX_HOME=/example"])
        with tempfile.TemporaryDirectory() as folder:
            cli = Path(folder) / "claude"
            cli.write_text('#!/bin/sh\nprintf "%s\\n" "$CLAUDE_CONFIG_DIR" "$1"\n')
            cli.chmod(0o700)
            brief = Path(folder) / "brief.txt"
            content = "Don't change CLAUDE_CONFIG_DIR=/example; discuss the setting."
            brief.write_text(content)
            prepared = iterm_tab.with_account(f'claude "$(cat {shlex.quote(str(brief))})"', choice)
            result = subprocess.run(["/bin/sh", "-c", prepared], env={**os.environ, "PATH": folder + ":" + os.environ["PATH"]}, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.splitlines(), [choice["env"]["CLAUDE_CONFIG_DIR"], content])

    def test_manual_account_environment_without_provider_refuses_before_tabs(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "launch.txt"
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            args = argparse.Namespace(command="new", window_id="chosen", command_file=path, brief_file=fixture_briefs(path),
                                      account_provider=None, account=None, account_config=None)
            for command in ("env CODEX_HOME=/manual codex brief", "export CLAUDE_CONFIG_DIR=/manual && claude brief"):
                path.write_text(command)
                with patch.dict("sys.modules", {"iterm2": SimpleNamespace()}), self.assertRaisesRegex(ValueError, "account settings need --account-provider"):
                    asyncio.run(iterm_tab.operate(app, args))
            window.async_create_tab.assert_not_awaited()

    def test_codex_daemon_launch_without_provider_refuses_before_tabs(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "launch.txt"
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            args = argparse.Namespace(command="new", window_id="chosen", command_file=path, brief_file=fixture_briefs(path),
                                      account_provider=None, account=None, account_config=None)
            for command in ("codex --remote unix:///daemon.sock -C /work", "cd /work && exec codex --remote=unix://"):
                path.write_text(command)
                with patch.dict("sys.modules", {"iterm2": SimpleNamespace()}), self.assertRaisesRegex(ValueError, "--account-provider openai"):
                    asyncio.run(iterm_tab.operate(app, args))
            window.async_create_tab.assert_not_awaited()

    def test_unwritable_receipt_root_refuses_before_any_tab_and_creates_no_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "launch.txt"
            path.write_text("claude brief")
            choice = account_choice.choose("anthropic", accounts_config(), AccountChoiceTests().snapshot(), None)
            choice["storage_root"] = root / "missing-store"
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            args = account_launch_args(path)
            with (
                patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
                patch.object(account_choice, "select", return_value=choice),
                self.assertRaises(ValueError) as failed,
            ):
                asyncio.run(iterm_tab.operate(app, args))
            window.async_create_tab.assert_not_awaited()
            self.assertNotIn(str(root), str(failed.exception))
            self.assertFalse(choice["storage_root"].exists())
            choice["storage_root"] = root
            account_choice.prepare_launch(choice)
            self.assertEqual(list((root / "Launch Receipts").iterdir()), [])
            with patch.object(account_choice.os, "fsync", side_effect=OSError("disk full")):
                with self.assertRaises(ValueError):
                    account_choice.prepare_launch(choice)
            self.assertEqual(list((root / "Launch Receipts").iterdir()), [])

    def test_explicit_root_refused_by_macos_names_the_app_route(self):
        with tempfile.TemporaryDirectory() as folder:
            choice = {"storage_root": Path(folder)}
            reasons = {}
            for code in (errno.EPERM, errno.EACCES):
                refusal = PermissionError(code, os.strerror(code))
                with (patch.object(account_choice.tempfile, "NamedTemporaryFile", side_effect=refusal),
                      self.assertRaises(ValueError) as failed):
                    account_choice.prepare_launch(choice)
                self.assertNotIn(folder, str(failed.exception))
                reasons[code] = str(failed.exception)
            self.assertIn("--record-launch", reasons[errno.EPERM])
            self.assertNotIn("--record-launch", reasons[errno.EACCES])

    def test_partial_batch_failure_preserves_started_and_failed_launch_identities(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = [root / "first.txt", root / "second.txt"]
            for path in files:
                path.write_text("claude brief")
            choices = account_choice.choose_batch("anthropic", accounts_config(), AccountChoiceTests().snapshot(), None, count=2)
            for choice in choices:
                choice["storage_root"] = root
            terminals = [SimpleNamespace(session_id=f"session-{i}", async_send_text=AsyncMock()) for i in range(2)]
            tabs = [SimpleNamespace(tab_id=f"tab-{i}", current_session=t) for i, t in enumerate(terminals)]
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock(side_effect=tabs))
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            args = account_launch_args(files)
            with patch.dict("sys.modules", {"iterm2": SimpleNamespace()}), patch.object(account_choice, "select_batch", return_value=choices), patch.object(account_choice, "record_launch", side_effect=[root / "receipt.json", PermissionError("private-root")]):
                try:
                    asyncio.run(iterm_tab.operate(app, args))
                    self.fail("second launch must fail")
                except (ValueError, OSError) as error:
                    detail = getattr(error, "detail", {})
            self.assertEqual(detail["launched"][0]["session_id"], "session-0")
            self.assertEqual(detail["failed_launch"]["tab_id"], "tab-1")
            self.assertFalse(detail["failed_launch"]["receipt_written"])
            self.assertEqual(detail["failed_launch"]["submission"], "not_attempted")
            self.assertNotIn("private-root", json.dumps(detail))
            terminals[1].async_send_text.assert_not_awaited()

    def test_receipt_failure_prevents_command_submission(self):
        terminal, window, app = launch_terminal_fixture()
        choice = account_choice.choose("anthropic", accounts_config(), AccountChoiceTests().snapshot(), None)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "launch.txt"
            path.write_text("claude brief")
            args = account_launch_args(path)
            with patch.dict("sys.modules", {"iterm2": SimpleNamespace()}), patch.object(account_choice, "select", return_value=choice), patch.object(account_choice, "record_launch", side_effect=OSError("disk full")), patch.object(account_choice, "prepare_launch"), self.assertRaises(iterm_tab.LaunchFailure):
                asyncio.run(iterm_tab.operate(app, args))
            terminal.async_send_text.assert_not_awaited()

    def test_uncertain_submit_retains_receipt_and_reports_exact_tab(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "launch.txt"
            path.write_text("claude brief")
            choice = account_choice.choose("anthropic", accounts_config(), AccountChoiceTests().snapshot(), None)
            choice["storage_root"] = root
            terminal = SimpleNamespace(session_id="session", async_send_text=AsyncMock(side_effect=Exception("private RPC payload")))
            tab = SimpleNamespace(tab_id="tab", current_session=terminal)
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock(return_value=tab))
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            args = account_launch_args(path)
            with (
                patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
                patch.object(account_choice, "select", return_value=choice),
                self.assertRaises(iterm_tab.LaunchFailure) as failed,
            ):
                asyncio.run(iterm_tab.operate(app, args))
            self.assertEqual(len(list((root / "Launch Receipts").glob("*.json"))), 1)
            self.assertEqual(failed.exception.detail["failed_launch"]["submission"], "unknown")
            self.assertTrue(failed.exception.detail["failed_launch"]["receipt_written"])
            self.assertEqual(failed.exception.detail["failed_launch"]["tab_id"], "tab")
            self.assertNotIn("private RPC payload", json.dumps(failed.exception.detail))

    def test_batch_refusal_and_bad_second_command_create_no_tabs_or_receipts(self):
        window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
        app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
        with tempfile.TemporaryDirectory() as folder:
            files = [Path(folder) / "first.txt", Path(folder) / "second.txt"]
            files[0].write_text("claude brief")
            files[1].write_text("env CLAUDE_CONFIG_DIR=/other claude")
            args = account_launch_args(files)
            choices = account_choice.choose_batch("anthropic", accounts_config(), AccountChoiceTests().snapshot(), None, count=2)
            with (
                patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
                patch.object(account_choice, "select_batch", return_value=choices),
                patch.object(account_choice, "record_launch") as receipt,
                patch.object(account_choice, "prepare_launch"),
                self.assertRaisesRegex(ValueError, "already sets"),
            ):
                asyncio.run(iterm_tab.operate(app, args))
            window.async_create_tab.assert_not_awaited()
            receipt.assert_not_called()
            with patch.dict("sys.modules", {"iterm2": SimpleNamespace()}), patch.object(account_choice, "select_batch", side_effect=ValueError("no choice")), self.assertRaises(ValueError):
                asyncio.run(iterm_tab.operate(app, args))
            window.async_create_tab.assert_not_awaited()

    def test_invalid_launch_file_creates_no_tab(self):
        window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
        app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "launch.txt"
            args = argparse.Namespace(
                command="new", window_id="chosen", command_file=path, brief_file=fixture_briefs(path)
            )
            for control in ("\x1b", "\t"):
                path.write_text("codex --remote unix://" + control)
                with (
                    self.subTest(control=control),
                    patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
                    self.assertRaises(ValueError),
                ):
                    asyncio.run(iterm_tab.operate(app, args))
        window.async_create_tab.assert_not_awaited()

    def test_unverified_send_refuses_without_keys(self):
        terminal = SimpleNamespace(session_id="term", async_send_text=AsyncMock())
        app = SimpleNamespace(
            terminal_windows=[
                SimpleNamespace(tabs=[SimpleNamespace(sessions=[terminal])])
            ]
        )
        args = argparse.Namespace(
            command="send", session_id="term", verified_target=False
        )
        with self.assertRaises(ValueError):
            asyncio.run(iterm_tab.operate(app, args))
        terminal.async_send_text.assert_not_awaited()

    def test_missing_window_refuses_without_launch(self):
        app = SimpleNamespace(terminal_windows=[], current_terminal_window=None)
        args = argparse.Namespace(command="new", window_id="missing")
        # Import is only needed by actual window creation, not target validation.
        with (
            patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
            self.assertRaises(ValueError),
        ):
            asyncio.run(iterm_tab.operate(app, args))

    def test_process_verification_preserves_scripts_missing_tty_and_malformed_inventory(self):
        entry = {"pid": 123, "tty": "ttys001"}
        for command in ("bash build.sh", "zsh -c 'while :; do :; done'", "/bin/sh /tmp/task.sh"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                session_liveness.verify_process_exit(entry, rows=[(20, "ttys001", command)])
        session_liveness.verify_process_exit(entry, rows=[(20, "ttys001", "-zsh"), (21, "ttys001", "/usr/bin/login -fp user")])
        with self.assertRaises(ValueError):
            session_liveness.verify_process_exit(entry, rows=[(20, "ttys999", "-zsh")], require_tty=True)
        self.assertEqual(session_liveness.process_rows("123 ttys001 /bin/claude\n"), [(123, "ttys001", "/bin/claude")])
        with self.assertRaises(ValueError):
            session_liveness.process_rows("unexpected")
        with self.assertRaises(ValueError):
            session_liveness.verify_process_exit({}, rows=[])


if __name__ == "__main__":
    unittest.main()
