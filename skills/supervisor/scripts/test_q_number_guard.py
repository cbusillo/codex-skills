#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise the Q-number Stop hook on reply shapes the Supervisor actually sends."""

import io
import json
import os
from pathlib import Path
import tempfile
import unittest

import q_number_guard as guard

NUMBERED = """**Q590 — Move the nightly sync to the CM host?** I recommend yes: it stops the
double import. On yes, the agent moves the job and checks one night's run. Ongoing cost: none."""

UNNUMBERED_QUESTION = """Two PRs are green and handed to the train.

One decision: should I move the nightly sync to the CM host tonight? I recommend yes; it stops
the double import, and an experienced developer would do the same."""

UNNUMBERED_ASK = """I recommend enabling the hook only in the direction repository, not user-wide.
Reply yes and I'll open both PRs."""

STATUS_WITH_QUOTED_QUESTION = """Status, 9:40 PM ET:
- codex-skills#2402 merged; runtime checkout is current.
- direction#96 is waiting on CI.

Still open from yesterday:
> Should I retire the old watcher service?

Chris answered "Should we keep the canary?" with "yes, keep it". Why did CI fail earlier? A flaky
runner; the rerun passed. See https://github.com/OWNER/REPO/actions?query=branch%3Amain for the log.
Nothing is needed from you."""


# Real-shaped replies the first review found mis-sorted.
MISSED_ASKS = [
    "> **Q590 — Keep the watcher?**\n\nShould I move the nightly sync to the CM host? I recommend yes.",
    "I recommend moving the sync to the CM host. Is that okay? Ongoing cost: none.",
    "I recommend moving the nightly sync to the CM host. Let me know if I should proceed.",
]
PLAIN_STATUS = [
    "Can the train merge yet? No, CI is still running. I am watching it; nothing is needed from you.",
    "Yesterday Chris asked: ‘Should I retire the old watcher service?’ He approved it; it is retired.",
]


def stop(**fields):
    return {"session_id": "s1", "hook_event_name": "Stop", "stop_hook_active": False, **fields}


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.env = {"CODE_HOME": self.home.name}

    def run_hook(self, payload):
        stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
        stdout = io.StringIO()
        self.assertEqual(guard.hook(stdin, stdout, self.env), 0)
        return stdout.getvalue()

    def logged(self):
        path = guard.log_path(self.env)
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_numbered_question_passes(self):
        self.assertEqual(self.run_hook(stop(last_assistant_message=NUMBERED)), "")
        self.assertEqual([r["verdict"] for r in self.logged()], ["pass"])

    def test_unnumbered_recommendation_question_blocks_once(self):
        decision = json.loads(self.run_hook(stop(last_assistant_message=UNNUMBERED_QUESTION)))
        self.assertEqual(decision["decision"], "block")
        self.assertIn("Q", decision["reason"])
        self.assertEqual(self.run_hook(stop(last_assistant_message=UNNUMBERED_QUESTION,
                                            stop_hook_active=True)), "")
        self.assertEqual([r["verdict"] for r in self.logged()], ["caught", "second-pass"])

    def test_unnumbered_ask_without_question_mark_blocks(self):
        self.assertEqual(json.loads(self.run_hook(stop(last_assistant_message=UNNUMBERED_ASK)))["decision"],
                         "block")

    def test_status_report_with_quoted_old_question_passes(self):
        self.assertEqual(self.run_hook(stop(last_assistant_message=STATUS_WITH_QUOTED_QUESTION)), "")

    def test_review_shapes_are_sorted(self):
        for message in MISSED_ASKS:
            self.assertIn("block", self.run_hook(stop(last_assistant_message=message)), message)
        for message in PLAIN_STATUS:
            self.assertEqual(self.run_hook(stop(last_assistant_message=message)), "", message)

    def test_log_records_no_message_text(self):
        self.run_hook(stop(last_assistant_message=UNNUMBERED_QUESTION))
        record = self.logged()[0]
        self.assertEqual((set(record), record["session"], record["verdict"]), ({"time", "session", "verdict"}, "s1", "caught"))
        self.assertNotIn("nightly", guard.log_path(self.env).read_text())

    def test_transcript_is_read_when_the_message_field_is_missing(self):
        transcript = Path(self.home.name, "t.jsonl")
        lines = [{"type": "user", "message": {"content": "go"}},
                 {"type": "assistant", "message": {"content": [{"type": "text", "text": UNNUMBERED_QUESTION}]}},
                 {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}]
        transcript.write_text("\n".join(map(json.dumps, lines)) + "\nnot json\n")
        self.assertIn("block", self.run_hook(stop(transcript_path=str(transcript))))

    def test_errors_and_unreadable_input_fail_open(self):
        fifo = Path(self.home.name, "fifo")
        os.mkfifo(fifo)  # Opening a FIFO with no writer would hang the hook.
        for payload in ("not json", stop(transcript_path="/nonexistent/t.jsonl"), stop(transcript_path=str(fifo)),
                        stop(last_assistant_message=None), {"hook_event_name": "SubagentStop",
                                                           "last_assistant_message": UNNUMBERED_ASK}):
            self.assertEqual(self.run_hook(payload), "")

    def test_unwritable_log_still_blocks(self):
        Path(self.home.name, "skill-data").write_text("a file where the directory should be")
        self.assertIn("block", self.run_hook(stop(last_assistant_message=UNNUMBERED_ASK)))

    def test_report_counts_a_date_range(self):
        path = guard.log_path(self.env)
        path.parent.mkdir(parents=True)
        path.write_text("\n".join(json.dumps({"time": time, "session": "s", "verdict": verdict}) for time, verdict in [
            ("2026-10-19T23:00:00+00:00", "pass"), ("2026-10-20T01:00:00+00:00", "caught"),
            ("2026-10-20T01:00:05+00:00", "second-pass"), ("2026-10-21T02:00:00+00:00", "pass")]) + "\ngarbage\n")
        result = guard.report(path, guard.dt.date(2026, 10, 20), guard.dt.date(2026, 10, 20))
        self.assertEqual(result["counts"], {"pass": 0, "caught": 1, "second-pass": 1, "unreadable": 1})


if __name__ == "__main__":
    unittest.main()
