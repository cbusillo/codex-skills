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

    def test_native_receipt_must_prove_visible_attachment(self):
        request = self.fields()
        with patch.object(reminder_save.sys, "platform", "darwin"), patch.object(reminder_save.subprocess, "run") as run:
            for receipt in ({"status": "partial", "reminder_id": "owned-id"},
                            {"status": "manual_required"}, {"status": "refused"},
                            {"status": "saved", "visible_url_verified": False}):
                run.return_value = subprocess.CompletedProcess([], 0 if receipt["status"] == "saved" else 1, json.dumps(receipt), "")
                with self.assertRaises(ValueError):
                    reminder_save.native(request)
            run.return_value = subprocess.CompletedProcess([], 0, '{"status":"saved","visible_url_verified":true}', "")
            self.assertTrue(reminder_save.native(request)["visible_url_verified"])
            self.assertEqual(json.loads(run.call_args.kwargs["input"])["url"], request["url"])
            self.assertFalse(run.call_args.kwargs.get("shell", False))
            run.return_value = subprocess.CompletedProcess([], 1, "", "compiler failed")
            with self.assertRaisesRegex(ValueError, "compiler failed"):
                reminder_save.native(request)

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


if __name__ == "__main__":
    unittest.main()
