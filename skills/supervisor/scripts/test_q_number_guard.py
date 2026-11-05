#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise the Q-number Stop hook on reply shapes the Supervisor actually sends."""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import q_number_guard as guard
import question_record as record

UNNUMBERED_QUESTION = """Two PRs are green and handed to the train.

One decision: should I move the nightly sync to the CM host tonight? I recommend yes; it stops
the double import, and an experienced developer would do the same."""

STATUS_WITH_QUOTED_QUESTION = """Status, 9:40 PM ET:
- codex-skills#2402 merged; runtime checkout is current.
- direction#96 is waiting on CI.

Still open from yesterday:
> Should I retire the old watcher service?

Chris answered "Should we keep the canary?" with "yes, keep it". Why did CI fail earlier? A flaky
runner; the rerun passed. See https://github.com/OWNER/REPO/actions?query=branch%3Amain for the log.
Nothing is needed from you."""


# Real-shaped replies the reviews found mis-sorted.
MISSED_ASKS = [
    "> **Q590 — Keep the watcher?**\n\nShould I move the nightly sync to the CM host? I recommend yes.",
    "I recommend moving the sync to the CM host. Is that okay? Ongoing cost: none.",
    "I recommend moving the nightly sync to the CM host. Should I proceed? No downtime is expected.",
]
PLAIN_STATUS = [
    "Can the train merge yet? No, CI is still running. I am watching it; nothing is needed from you.",
    "Yesterday Chris asked: ‘Should I retire the old watcher service?’ He approved it; it is retired.",
    "Nothing is waiting on your approval. The train is running.",
]


def stop(**fields: object) -> dict:
    return {"session_id": "s1", "hook_event_name": "Stop", "stop_hook_active": False, **fields}


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.env = {"CODE_HOME": self.home.name}

    def run_hook(self, payload: dict | str) -> str:
        stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
        stdout = io.StringIO()
        self.assertEqual(guard.hook(stdin, stdout, self.env), 0)
        return stdout.getvalue()

    def logged(self) -> list[dict]:
        path = guard.log_path(self.env)
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    @staticmethod
    def registered_question(number: str = "Q590") -> dict:
        value = {
            "version": record.VERSION,
            "issue": "https://github.com/example/product/issues/3",
            "decision": "Should we move the nightly sync?",
            "basis": "The current host duplicates imports.",
            "options": [
                {"answer": "Move", "changes": "Move the job", "cost": 0, "next": "Check the next run"},
                {"answer": "Wait", "changes": "Keep the job", "cost": "Daily review", "next": "Review failures"},
            ],
            "recommendation": "Move",
            "comparison": "An experienced developer would remove the duplicate import.",
            "manual": "Answer with one option",
        }
        url = value["issue"] + "#issuecomment-17"
        source = {"body": record.question_id.number_heading(record.render(value, legacy=True), number), "url": url,
                  "author": "fixture-automation", "issue": value["issue"]}
        return source

    def test_approval_status_does_not_ask_the_director(self) -> None:
        for message in (
            "It needs your approval.",
            "The PR is a 4-line add, waiting on your approval.",
            "That change would need your approval, so it can wait.",
        ):
            with self.subTest(message=message):
                self.assertEqual(self.run_hook(stop(last_assistant_message=message)), "")

    def question_output(self, number: str = "Q590") -> str:
        view = record.read_record(self.registered_question(number))
        return record.chat_text(view)

    def transcript(self, output: str, command: str = "create", *, failed: bool = False) -> Path:
        path = Path(self.home.name, "transcript.jsonl")
        entries = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "id": "question",
                            "input": {"command": f"uv run /catalog/skills/supervisor/scripts/oq.py {command}"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "toolUseResult": {"stdout": output, "stderr": "", "interrupted": False},
                "message": {"content": [{"type": "tool_result", "tool_use_id": "question", "is_error": failed, "content": output}]},
            },
        ]
        path.write_text("\n".join(map(json.dumps, entries)))
        return path

    def test_full_question_from_create_update_and_read_passes_unchanged(self) -> None:
        output = self.question_output()
        for command in ("create", "update", "show-question"):
            with self.subTest(command=command):
                transcript = self.transcript(output + "\nRegistration: unchanged\n", command)
                for message in (output, "The first PR landed.\n\n" + output):
                    self.assertEqual(self.run_hook(stop(last_assistant_message=message, transcript_path=str(transcript))), "")

    def test_shortened_changed_or_unregistered_question_blocks(self) -> None:
        output = self.question_output()
        transcript = self.transcript(output)
        messages = [
            output.splitlines()[0],
            "- **Q590. Move the nightly sync to the CM host?**",
            "Still open: **Q300 — Retire the old watcher?**",
            "**Q300.** Retire the old watcher?",
            "**Q590. Storage: retire the old watcher?**",
            output.splitlines()[0] + "\n\nOriginal question: " + self.registered_question()["url"],
            output.replace("Move the job", "Delete the job"),
            output.replace("Q590.", "Q591."),
            output + "\nShould I also replace its runner?",
        ]
        for message in messages:
            with self.subTest(message=message):
                decision = json.loads(self.run_hook(stop(last_assistant_message=message, transcript_path=str(transcript))))
                self.assertEqual(decision["decision"], "block")
                self.assertTrue(decision["reason"])
        self.assertIn("block", self.run_hook(stop(last_assistant_message=output)))

    def test_unrelated_failed_and_dry_run_outputs_cannot_register_a_question(self) -> None:
        output = self.question_output()
        for command, failed, printed in (
            ("create", True, output),
            ("other-helper", False, output),
            ("create --dry-run", False, output.replace("Q590. ", "")),
        ):
            with self.subTest(command=command, failed=failed):
                transcript = self.transcript(printed, command, failed=failed)
                self.assertIn("block", self.run_hook(stop(last_assistant_message=printed if "--dry-run" in command else output, transcript_path=str(transcript))))

    def test_latest_revision_replaces_earlier_printed_text(self) -> None:
        output = self.question_output()
        transcript = self.transcript(output)
        original = transcript.read_text()
        revised = output.replace("Move the job", "Move both jobs").replace("Q590.", "Q590 —")
        transcript.write_text(original + "\n" + original.replace("Move the job", "Move both jobs").replace("Q590.", "Q590 —"))
        self.assertEqual(self.run_hook(stop(last_assistant_message=revised, transcript_path=str(transcript))), "")
        self.assertIn("block", self.run_hook(stop(last_assistant_message=output, transcript_path=str(transcript))))

    def test_numbered_status_and_quoted_questions_pass(self) -> None:
        for message in ("**Q1276.** Approved and merged.", "**Q1276 — Catalog milestone:** approved",
                        "**Q855** was approved and implemented. Nothing is needed from you.",
                        "> **Q855** Should I proceed?\nThe work is done.",
                        '**Q855** is settled. Chris asked "Should I proceed?" and approved it.',
                        "**Q855** Can the train merge yet? No, CI is running."):
            with self.subTest(message=message):
                self.assertEqual(self.run_hook(stop(last_assistant_message=message)), "")

    def test_unnumbered_recommendation_question_blocks_once(self) -> None:
        decision = json.loads(self.run_hook(stop(last_assistant_message=UNNUMBERED_QUESTION)))
        self.assertEqual(decision["decision"], "block")
        self.assertEqual(self.run_hook(stop(last_assistant_message=UNNUMBERED_QUESTION,
                                            stop_hook_active=True)), "")
        self.assertEqual([r["verdict"] for r in self.logged()], ["caught", "second-pass"])

    def test_status_report_with_quoted_old_question_passes(self) -> None:
        self.assertEqual(self.run_hook(stop(last_assistant_message=STATUS_WITH_QUOTED_QUESTION)), "")

    def test_review_shapes_are_sorted(self) -> None:
        for message in MISSED_ASKS:
            self.assertIn("block", self.run_hook(stop(last_assistant_message=message)), message)
        for message in PLAIN_STATUS:
            self.assertEqual(self.run_hook(stop(last_assistant_message=message)), "", message)

    def test_log_records_no_message_text(self) -> None:
        self.run_hook(stop(last_assistant_message=UNNUMBERED_QUESTION))
        record = self.logged()[0]
        self.assertEqual((set(record), record["session"], record["verdict"]), ({"time", "session", "verdict"}, "s1", "caught"))
        self.assertNotIn("nightly", guard.log_path(self.env).read_text())

    def test_transcript_is_read_when_the_message_field_is_missing(self) -> None:
        transcript = Path(self.home.name, "t.jsonl")
        lines = [
            {"type": "user", "message": {"content": "go"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": UNNUMBERED_QUESTION}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
        ]
        transcript.write_text("\n".join(map(json.dumps, lines)) + "\nnot json\n")
        self.assertIn("block", self.run_hook(stop(transcript_path=str(transcript))))

    def test_malformed_entries_do_not_disable_question_checks(self) -> None:
        output = self.question_output()
        transcript = self.transcript(output)
        entries = [json.loads(line) for line in transcript.read_text().splitlines()]
        malformed = [{"type": "assistant", "message": value} for value in (None, "old text", [])]
        malformed += [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": value}]}}
            for value in (None, [], {"command": []})
        ]
        malformed.append(
            {
                "type": "user",
                "toolUseResult": {"stdout": None},
                "message": {"content": [{"type": "tool_result", "tool_use_id": "question"}]},
            }
        )
        transcript.write_text("\n".join(map(json.dumps, entries + malformed)))
        self.assertEqual(self.run_hook(stop(last_assistant_message=output, transcript_path=str(transcript))), "")
        self.assertIn("block", self.run_hook(stop(last_assistant_message=UNNUMBERED_QUESTION, transcript_path=str(transcript))))

    def test_legacy_helper_output_and_format_changes_pass_without_template_matching(self) -> None:
        output = self.question_output()
        legacy = "**Q300. Retire the old watcher?**\n\nKeep or retire it.\n\nOriginal question: " + self.registered_question()["url"]
        for printed in (legacy, output.replace("Context:", "Reason:").replace("Owning issue:", "Source issue:")):
            with self.subTest(printed=printed):
                transcript = self.transcript(printed, "show-question --chat")
                self.assertEqual(self.run_hook(stop(last_assistant_message=printed, transcript_path=str(transcript))), "")

    def test_errors_and_unreadable_input_fail_open(self) -> None:
        fifo = Path(self.home.name, "fifo")
        os.mkfifo(fifo)  # Opening a FIFO with no writer would hang the hook.
        for payload in (
            "not json",
            stop(transcript_path="/nonexistent/t.jsonl"),
            stop(transcript_path=str(fifo)),
            stop(last_assistant_message=None),
            {"hook_event_name": "SubagentStop", "last_assistant_message": UNNUMBERED_QUESTION},
        ):
            self.assertEqual(self.run_hook(payload), "")

    def test_unwritable_log_still_blocks(self) -> None:
        Path(self.home.name, "skill-data").write_text("a file where the directory should be")
        self.assertIn("block", self.run_hook(stop(last_assistant_message=UNNUMBERED_QUESTION)))

    def test_report_counts_a_date_range(self) -> None:
        path = guard.log_path(self.env)
        path.parent.mkdir(parents=True)
        path.write_text("\n".join(json.dumps({"time": time, "session": "s", "verdict": verdict}) for time, verdict in [
            ("2026-10-19T23:00:00+00:00", "pass"), ("2026-10-20T01:00:00+00:00", "caught"),
            ("2026-10-20T01:00:05+00:00", "second-pass"), ("2026-10-21T02:00:00+00:00", "pass")]) + "\ngarbage\n")
        result = guard.report(path, guard.dt.date(2026, 10, 20), guard.dt.date(2026, 10, 20))
        self.assertEqual(result["counts"], {"pass": 0, "caught": 1, "second-pass": 1, "unreadable": 1})


if __name__ == "__main__":
    unittest.main()
