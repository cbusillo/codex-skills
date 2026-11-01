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
import time
import unittest
from unittest import mock

import q_number_guard as guard
import question_record as record

NUMBERED = """**Q590 — Move the nightly sync to the CM host?** I recommend yes: it stops the
double import. On yes, the agent moves the job and checks one night's run. Ongoing cost: none. An experienced developer would do the same."""

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


# Real-shaped replies the reviews found mis-sorted.
MISSED_ASKS = [
    "> **Q590 — Keep the watcher?**\n\nShould I move the nightly sync to the CM host? I recommend yes.",
    "I recommend moving the sync to the CM host. Is that okay? Ongoing cost: none.",
    "I recommend moving the nightly sync to the CM host. Let me know if I should proceed.",
    "I recommend moving the nightly sync to the CM host. Should I proceed? No downtime is expected.",
]
PLAIN_STATUS = [
    "Can the train merge yet? No, CI is still running. I am watching it; nothing is needed from you.",
    "Yesterday Chris asked: ‘Should I retire the old watcher service?’ He approved it; it is retired.",
    "Nothing is waiting on your approval. The train is running.",
]


def stop(**fields):
    return {"session_id": "s1", "hook_event_name": "Stop", "stop_hook_active": False, **fields}


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.env = {"CODE_HOME": self.home.name}
        self.enterContext(mock.patch.object(record.question_id, "configured_queue", return_value="example/direction#9"))
        self.enterContext(mock.patch.object(record.delivery.oq, "default_decision_authors", return_value=["fixture-automation"]))
        self.original_read_question = guard.read_question
        self.read_question = self.enterContext(mock.patch.object(guard, "read_question", side_effect=ValueError("unavailable")))

    def run_hook(self, payload):
        stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
        stdout = io.StringIO()
        self.assertEqual(guard.hook(stdin, stdout, self.env), 0)
        return stdout.getvalue()

    def logged(self):
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

    def test_numbered_question_passes(self):
        self.assertEqual(self.run_hook(stop(last_assistant_message=NUMBERED)), "")
        self.assertEqual([r["verdict"] for r in self.logged()], ["pass"])

    def test_numbered_question_context_is_checked_once(self):
        for context in ("An experienced developer would keep the job.", "Ongoing cost: none.", ""):
            with self.subTest(context=context):
                message = f"**Q855** Should I move the sync? {context}"
                decision = json.loads(self.run_hook(stop(last_assistant_message=message)))
                self.assertEqual(decision["decision"], "block")
                self.assertTrue(decision["reason"])
                self.assertEqual(self.run_hook(stop(last_assistant_message=message, stop_hook_active=True)), "")

    def test_registered_structured_question_supplies_relay_context(self) -> None:
        source = self.registered_question()
        with mock.patch.object(guard, "read_question", return_value=source):
            for heading in ("**Q590 — Should we move the nightly sync?**", source["body"].splitlines()[0]):
                with self.subTest(heading=heading):
                    self.assertEqual(self.run_hook(stop(last_assistant_message=f"{heading} {source['url']}")), "")

    def test_registered_legacy_question_and_new_ask_do_not_bypass_context(self) -> None:
        source = self.registered_question()
        messages = [
            "**Q590 — Should we move the nightly sync?** Should I also replace its runner?",
            "**Q590 — Should we move the nightly sync? And should I also replace its runner?**",
            "**Q591 — Should we replace the runner?**",
            "**Q590 — Should we move the nightly sync?** Your call on the runner too.",
        ]
        for message in messages:
            with self.subTest(message=message):
                with mock.patch.object(guard, "read_question", return_value=source):
                    self.assertIn("block", self.run_hook(stop(last_assistant_message=f"{message} {source['url']}")))
        for changes in ({"body": "**Q590 — Should we move the nightly sync?**"}, {"author": "stranger"}):
            with self.subTest(changes=changes), mock.patch.object(guard, "read_question", return_value={**source, **changes}):
                self.assertIn("block", self.run_hook(stop(last_assistant_message=f"**Q590 — Should we move the nightly sync?** {source['url']}")))

    def test_unknown_registered_question_lookup_falls_back_to_context_check(self) -> None:
        source = self.registered_question()
        message = "**Q590 — Should we move the sync?**"
        self.assertIn("block", self.run_hook(stop(last_assistant_message=message)))
        self.read_question.assert_not_called()
        self.assertIn("block", self.run_hook(stop(last_assistant_message=f"{message} {source['url']}")))

    def test_direct_source_read_validates_identity_and_transport(self) -> None:
        source = self.registered_question()
        body = {"html_url": source["url"], "body": source["body"], "user": {"login": source["author"]}}
        for changed, successful in (({}, True), ({"html_url": source["url"] + "1"}, True), ({}, False)):
            with self.subTest(changed=changed, successful=successful), mock.patch.object(guard.subprocess, "Popen") as launch:
                child = launch.return_value.__enter__.return_value
                child.communicate.return_value = (json.dumps({"ok": successful, "outcome_certainty": "confirmed",
                                                             "body": {**body, **changed}}), None)
                child.returncode = 0
                with mock.patch.object(guard, "read_question", side_effect=self.original_read_question):
                    output = self.run_hook(stop(last_assistant_message=f"**Q590 — Move the sync?** {source['url']}"))
                self.assertEqual(output == "", successful and not changed)

    def test_slow_authentication_child_is_stopped_at_the_lookup_deadline(self) -> None:
        helper = Path(self.home.name, "slow-read.py")
        helper.write_text("import signal\nsignal.pause()\n")
        with mock.patch.object(guard, "API_HELPER", helper):
            with self.assertRaises(guard.subprocess.TimeoutExpired):
                self.original_read_question(self.registered_question()["url"], time.time() + 0.1)

    def test_missing_lookup_import_and_null_author_keep_the_block(self) -> None:
        source = self.registered_question()
        for failure in (ImportError("missing sibling"), AttributeError("unknown author"), SyntaxError("broken sibling")):
            with self.subTest(failure=failure), mock.patch.object(guard, "read_question", side_effect=failure):
                self.assertIn("block", self.run_hook(stop(last_assistant_message=f"**Q590 — Move the sync?** {source['url']}")))

    def test_unrelated_trusted_comment_does_not_hide_the_record(self) -> None:
        source = self.registered_question()
        other = {**source, "body": "Status update", "url": source["url"] + "1"}
        with mock.patch.object(guard, "read_question", side_effect=[other, source]):
            self.assertEqual(self.run_hook(stop(last_assistant_message=
                f"**Q590 — Move the sync?** {other['url']} {source['url']}")), "")

    def test_context_correction_uses_shared_guidance(self) -> None:
        guidance = Path(self.home.name, "guidance.md")
        for rule in ("Explain the recurring work and the alternative.", "Explain the upkeep and your recommendation."):
            guidance.write_text(f"# Guidance\n\n{guard.QUESTION_SECTION}\n\n{rule}\n\n## Other Guidance\n\nUnrelated instructions.\n")
            for message in ("**Q855** Should I move the sync?", "Should I move the sync?"):
                with mock.patch.object(guard, "QUESTION_GUIDANCE", guidance):
                    decision = json.loads(self.run_hook(stop(last_assistant_message=message)))
                self.assertEqual(decision["decision"], "block")
                self.assertIn(rule, decision["reason"])
                self.assertNotIn("Unrelated instructions.", decision["reason"])

    def test_context_accepts_common_phrasings(self):
        for cost, developer in (("No ongoing cost.", "A senior dev would keep it."),
                                ("Upkeep is one check a month.", "An experienced professional developer would simplify it."),
                                ("Maintenance is minimal.", "The sr. developer view is to remove it.")):
            with self.subTest(cost=cost, developer=developer):
                self.assertEqual(self.run_hook(stop(last_assistant_message=f"**Q855** Should I proceed? {cost} {developer}")), "")

    def test_quoted_context_does_not_satisfy_question(self):
        for quote in ('"No ongoing cost. A senior dev would keep it."',
                      "> No ongoing cost. A senior dev would keep it.",
                      "```No ongoing cost. A senior dev would keep it.```",
                      "`No ongoing cost. A senior dev would keep it.`"):
            with self.subTest(quote=quote):
                decision = json.loads(self.run_hook(stop(last_assistant_message=f"**Q855** Should I proceed?\n{quote}")))
                self.assertEqual(decision["decision"], "block")

    def test_numbered_status_and_quoted_questions_pass(self):
        for message in ("**Q855** was approved and implemented. Nothing is needed from you.",
                        "> **Q855** Should I proceed?\nThe work is done.",
                        '**Q855** is settled. Chris asked "Should I proceed?" and approved it.',
                        "**Q855** Can the train merge yet? No, CI is running."):
            with self.subTest(message=message):
                self.assertEqual(self.run_hook(stop(last_assistant_message=message)), "")

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

    def test_transcript_disappearing_after_read_keeps_original_decision(self):
        transcript = Path(self.home.name, "t.jsonl")
        transcript.write_text(json.dumps({"type": "assistant", "message": {"content": UNNUMBERED_QUESTION}}))
        read_message = guard.last_message

        def read_then_remove(payload: dict) -> str | None:
            message = read_message(payload)
            transcript.unlink()
            return message

        with mock.patch.object(guard, "last_message", side_effect=read_then_remove):
            decision = json.loads(self.run_hook(stop(transcript_path=str(transcript))))
        self.assertEqual(decision["decision"], "block")
        self.assertTrue(decision["reason"])

    def test_errors_and_unreadable_input_fail_open(self):
        fifo = Path(self.home.name, "fifo")
        os.mkfifo(fifo)  # Opening a FIFO with no writer would hang the hook.
        for payload in ("not json", stop(transcript_path="/nonexistent/t.jsonl"), stop(transcript_path=str(fifo)),
                        stop(last_assistant_message=None), {"hook_event_name": "SubagentStop",
                                                           "last_assistant_message": UNNUMBERED_ASK}):
            self.assertEqual(self.run_hook(payload), "")

    def test_unavailable_guidance_still_blocks_and_logs(self) -> None:
        guidance = Path(self.home.name, "missing-guidance.md")
        for contents in (None, "# Guidance without a question section\n"):
            if contents is not None:
                guidance.write_text(contents)
            with mock.patch.object(guard, "QUESTION_GUIDANCE", guidance):
                decision = json.loads(self.run_hook(stop(last_assistant_message="**Q855** Should I proceed?")))
            self.assertEqual(decision["decision"], "block")
            self.assertIn(str(guidance), decision["reason"])
        self.assertEqual([record["verdict"] for record in self.logged()], ["caught", "caught"])

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
