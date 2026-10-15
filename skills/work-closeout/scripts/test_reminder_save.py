#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline writer behavior tests; no host permissions or real reminder writes."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import reminder_link
import reminder_save

SESSION = "12345678-1234-1234-1234-123456789abc"
ISSUE = "https://github.com/example/repo/issues/42"
PR = "https://github.com/example/repo/pull/43"


class ReminderSaveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "repo ' & $(touch bad);é"
        self.directory.mkdir()

    def fields(self, harness="codex", **overrides):
        args = dict(harness=harness, directory=self.directory, session=SESSION,
                    prompt=None, list_name="test list", due="2026-10-20T09:00",
                    timezone="America/New_York", links=[ISSUE, PR])
        args.update(overrides)
        return reminder_save.plan(**args)

    def test_shared_generator_url_and_clean_notes_for_both_providers(self):
        for harness, provider in (("codex", "Codex"), ("claude", "Claude")):
            fields = self.fields(harness)
            self.assertEqual(fields["url"], reminder_link.prepare(harness, self.directory, SESSION, None)["url"])
            lines = fields["notes"].splitlines()
            self.assertEqual(len(lines), 1 + len({ISSUE, PR}))
            self.assertIn(provider, lines[0])
            self.assertIn(SESSION, lines[0])
            self.assertEqual(lines[1:], [ISSUE, PR])
            self.assertNotIn(fields["marker"], fields["notes"])
            self.assertNotIn(fields["fallback"], fields["notes"])
            self.assertIn(ISSUE.rsplit("/", 1)[1], fields["title"])
            self.assertIn(provider, fields["title"])

    def test_fresh_prompt_stays_in_link_and_out_of_notes(self):
        prompt = "--search ' quote & $(touch bad); `touch bad`\né"
        fields = self.fields("claude", session=None, prompt=prompt, links=[])
        self.assertEqual(len(fields["notes"].splitlines()), 1)
        self.assertIn("Claude", fields["notes"])
        self.assertNotIn(SESSION, fields["notes"])
        self.assertEqual(parse_qs(urlsplit(fields["url"]).query)["d"], [str(self.directory.resolve())])
        self.assertNotIn(prompt, fields["notes"])
        self.assertFalse((self.directory / "bad").exists())

    def test_bad_schedule_and_links_refused(self):
        for overrides in ({"due": "2026-03-08T02:30"}, {"due": "2026-11-01T01:30"},
                          {"due": "2026-10-20T09:00Z"}, {"due": "2026-10-20"}, {"repeat_days": 0},
                          {"links": [ISSUE + "\nFallback: command"]},
                          {"title": "first\nsecond"}, {"list_name": " "}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.fields(**overrides)
        fields = self.fields(repeat_days=2, links=[ISSUE, ISSUE])
        self.assertEqual(fields["repeat_days"], 2)
        self.assertEqual(fields["notes"].splitlines()[1:], [ISSUE])

    def writer(self, reminders, final=None, crash=None):
        """Fake native writer: one lookup line, then a receipt for the decision it is sent."""
        test = self

        class Writer:
            def __init__(self, argv, **kwargs):
                test.assertFalse(kwargs.get("shell", False))
                self.sent, self.returncode = [], None
                self.stdin = self
                self.stdout = self
                self.lines = [json.dumps({"status": "lookup", "reminders": reminders})]
                if crash:
                    kwargs["stderr"].write(crash)
                    self.lines = []
                test.writers.append(self)

            def write(self, text):
                self.sent.append(json.loads(text))
                if len(self.sent) == 2:
                    action = self.sent[1]["action"]
                    receipt = final or {"status": "deleted" if action == "delete" else "saved",
                                        "visible_url_verified": True, "created": action == "create"}
                    self.lines.append(json.dumps(receipt))

            def readline(self):
                return self.lines.pop(0) + "\n" if self.lines else ""

            def close(self):
                if len(self.sent) == 1:  # no decision: the real writer refuses unchanged
                    self.lines.append(json.dumps({"status": "refused", "created": False}))

            def flush(self):
                pass

            def wait(self):
                self.returncode = 0 if json.loads(self.lines[-1] if self.lines else "{}").get("status") in (None, "saved", "deleted") else 1
                return self.returncode

            kill = flush

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        self.writers = []
        return patch.object(reminder_save.subprocess, "Popen", Writer)

    def request(self, **overrides):
        request = {k: v for k, v in self.fields().items() if k != "fallback"}
        request.update({"update_id": None, "delete_id": None, "preserve_existing": False, **overrides})
        return request

    def item(self, item_id, completed, same_target=True, title=None):
        return {"id": item_id, "completed": completed, "same_target": same_target,
                "title": self.fields()["title"] if title is None else title}

    def save(self, reminders, **overrides):
        with patch.object(reminder_save.sys, "platform", "darwin"), self.writer(reminders):
            return reminder_save.native(self.request(**overrides))

    def decisions(self):
        return [writer.sent[1:] for writer in self.writers]

    def test_completed_session_reminder_does_not_block_a_new_one(self):
        done = self.item("done", completed=True)
        receipt = self.save([done, self.item("done-too", completed=True)])
        self.assertTrue(receipt["created"])
        self.assertEqual(self.decisions(), [[{"action": "create", "id": None}]])
        for overrides in ({"update_id": "done"}, {"delete_id": "done"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.save([done], **overrides)
            self.assertEqual(self.decisions(), [[]])

    def test_incomplete_match_is_still_found_beside_completed_ones(self):
        reminders = [self.item("done", completed=True), self.item("open", completed=False),
                     self.item("other", completed=False, same_target=False, title="other")]
        with self.assertRaises(ValueError) as refused:
            self.save(reminders)
        self.assertEqual(json.loads(str(refused.exception))["reminder_id"], "open")
        self.assertEqual(self.decisions(), [[]])
        self.save(reminders, update_id="open")
        self.assertEqual(self.decisions(), [[{"action": "update", "id": "open"}]])
        self.save(reminders, delete_id="open")
        self.assertEqual(self.decisions(), [[{"action": "delete", "id": "open"}]])

    def test_ambiguous_targets_are_refused_without_a_decision(self):
        title = self.fields()["title"]
        for reminders, overrides in (
                ([self.item("a", False), self.item("b", False)], {}),
                ([self.item("a", False), self.item("b", False)], {"update_id": "a"}),
                ([self.item("other", False, same_target=False, title=title)], {}),
                ([self.item("done", True)], {"update_id": "missing"})):
            with self.subTest(reminders=reminders, overrides=overrides), self.assertRaises(ValueError):
                self.save(reminders, **overrides)
            self.assertEqual(self.decisions(), [[]])

    def test_native_receipt_must_prove_visible_attachment(self):
        with patch.object(reminder_save.sys, "platform", "darwin"):
            for receipt in ({"status": "partial", "reminder_id": "owned-id"},
                            {"status": "manual_required"}, {"status": "refused"},
                            {"status": "saved", "visible_url_verified": False}):
                with self.subTest(receipt=receipt), self.writer([], final=receipt), self.assertRaises(ValueError):
                    reminder_save.native(self.request())
            with self.writer([]):
                self.assertTrue(reminder_save.native(self.request())["visible_url_verified"])
            self.assertEqual(self.writers[0].sent[0]["url"], self.request()["url"])
            with self.writer([], crash="compiler failed"), self.assertRaisesRegex(ValueError, "compiler failed"):
                reminder_save.native(self.request())

    def test_cli_dry_run_never_writes_and_apply_omits_fallback(self):
        argv = ["reminder_save.py", "--harness", "codex", "--directory", str(self.directory),
                "--session-id", SESSION, "--list", "test", "--due", "2026-10-20T09:00",
                "--timezone", "America/New_York", "--link", ISSUE]
        with patch.object(reminder_save, "native") as backend:
            for flags in ([], ["--apply", "--update-id", "owned-id"]):
                backend.return_value = {"status": "saved", "visible_url_verified": True}
                output = io.StringIO()
                with patch.object(reminder_save.sys, "argv", argv + flags), contextlib.redirect_stdout(output):
                    self.assertEqual(reminder_save.main(), 0)
                if flags:
                    request = backend.call_args.args[0]
                    self.assertEqual(request["update_id"], "owned-id")
                    self.assertNotIn("fallback", request)
                    self.assertIn(SESSION, request["notes"].splitlines()[0])
                    self.assertEqual(request["notes"].splitlines()[1:], [ISSUE])
                else:
                    backend.assert_not_called()
                    self.assertEqual(json.loads(output.getvalue())["status"], "dry_run")

    def test_repair_requires_update_id_and_dry_run_never_writes(self):
        argv = ["reminder_save.py", "--harness", "claude", "--directory", str(self.directory),
                "--session-id", SESSION, "--list", "test", "--due", "2026-10-20T09:00",
                "--timezone", "America/New_York", "--preserve-existing"]
        with patch.object(reminder_save, "native") as backend:
            for flags in (["--apply"], ["--apply", "--delete-id", "owned-id"]):
                with patch.object(reminder_save.sys, "argv", argv + flags), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(reminder_save.main(), 1)
            backend.assert_not_called()
            output = io.StringIO()
            with patch.object(reminder_save.sys, "argv", argv + ["--update-id", "owned-id"]), contextlib.redirect_stdout(output):
                self.assertEqual(reminder_save.main(), 0)
            backend.assert_not_called()
            dry_run = json.loads(output.getvalue())
            self.assertEqual(dry_run["update_id"], "owned-id")
            self.assertTrue(dry_run["preserve_existing"])
            backend.return_value = {"status": "saved", "visible_url_verified": True, "preserved_existing": True}
            with patch.object(reminder_save.sys, "argv", argv + ["--update-id", "owned-id", "--apply"]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reminder_save.main(), 0)
            request = backend.call_args.args[0]
            self.assertEqual(request["update_id"], dry_run["update_id"])
            self.assertTrue(request["preserve_existing"])


if __name__ == "__main__":
    unittest.main()
