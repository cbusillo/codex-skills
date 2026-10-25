#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline presentation tests; no GitHub or Discord requests."""

import contextlib
import io
import sys
import types
import unittest
from unittest import mock

import digest_summary as digest
import discord_digest as sender

FULL = "https://github.com/example/catalog/issues/4#issuecomment-999"


def report(rows, complete=False):
    lines = ["Daily stranded-work sweep", "Observed: 2026-10-16T12:00:00+00:00",
             f"Coverage: {'complete within selected scope' if complete else 'INCOMPLETE'}; {len(rows)} candidates."]
    for kind, number, age, actor, title in rows:
        lines.extend([f"- {kind} ({age}): https://github.com/example/lab/issues/{number} — {actor}. Detail.",
                      f"  Name: {title}"])
    return "\n".join(lines) + "\n"


class SummaryTests(unittest.TestCase):
    def test_large_report_sends_one_short_message_with_every_person_wait_and_full_link(self):
        rows = [("merged_link_needs_review", i, f"{i}h", "Supervisor", f"Task {i}") for i in range(1, 501)]
        rows += [("aged_structured_wait", 600 + i, "age unknown", actor, f"Review choice {i}")
                 for i, actor in enumerate(("Chris", "Director / Supervisor", "cbusillo", "Owner", "Chris/Supervisor"))]
        source = report(rows)
        with mock.patch.object(sender, "webhook_url", return_value="http://localhost/api/webhooks/1/fixture-secret"), \
                mock.patch.object(sys, "stdin", io.StringIO(source)), \
                mock.patch.object(digest, "archive", return_value=FULL) as archive, \
                mock.patch.object(sender, "deliver") as deliver:
            self.assertEqual(sender.main([]), 0)
        archive.assert_called_once_with(source, sender.DEFAULT_TRACKING_ISSUE)
        deliver.assert_called_once()
        message = deliver.call_args.args[0]
        self.assertEqual(len(list(sender.chunks(message))), 1)
        self.assertLess(digest.units(message), 2000)
        self.assertIn(FULL, message)
        self.assertIn("2026-10-16", message)
        self.assertIn("INCOMPLETE", message)
        self.assertRegex(message, r"(?m)^merged_link_needs_review\W+500$")
        for row in rows[500:]:
            self.assertIn(row[4], message)
            self.assertIn(f"/issues/{row[1]}", message)
        for number in range(496, 501):
            self.assertIn(f"/issues/{number}>", message)
        self.assertNotIn("/issues/495>", message)

    def test_five_oldest_per_other_kind_and_unknown_last(self):
        rows = [(kind, base + i, f"{i}h", "Repository agent", "Item")
                for kind, base in (("overdue_promise", 100), ("passed_dated_wait", 200)) for i in range(1, 10)]
        rows.append(("overdue_promise", 999, "age unknown", "Supervisor", "Unknown age"))
        message = digest.summary(report(list(reversed(rows))), FULL)
        for base in (100, 200):
            for i in range(5, 10):
                self.assertIn(f"/issues/{base + i}>", message)
            self.assertNotIn(f"/issues/{base + 4}>", message)
        self.assertNotIn("/issues/999>", message)

    def test_empty_complete_report(self):
        message = digest.summary(report([], complete=True), FULL)
        self.assertIn("complete within selected scope", message)
        self.assertIn(FULL, message)
        self.assertNotIn("example/lab/issues/", message)
        # An empty person queue still says so on its heading line, not a bare heading.
        self.assertRegex(message, r"(?m)^Waiting on [^:\n]*:[ \t]*\S")

    def test_person_queue_overflow_sends_one_message_with_every_wait_attached(self) -> None:
        rows = [("unanswered_question", i, "age unknown", "Chris", "🧭" * 100) for i in range(51)]
        source = report(rows)
        with mock.patch.object(sender, "webhook_url", return_value="http://localhost/api/webhooks/1/fixture-secret"), \
                mock.patch.object(sys, "stdin", io.StringIO(source)), \
                mock.patch.object(digest, "archive", return_value=FULL) as archive, \
                mock.patch.object(sender, "deliver") as deliver:
            self.assertEqual(sender.main([]), 0)
        archive.assert_called_once_with(source, sender.DEFAULT_TRACKING_ISSUE)
        deliver.assert_called_once()
        message, _, attachment = deliver.call_args.args
        self.assertLess(digest.units(message), 2000)
        self.assertIn(FULL, message)
        self.assertEqual(attachment, source)
        self.assertIn(str(len(rows)), message)
        for row in rows:
            self.assertIn(row[4], attachment)
            self.assertIn(f"/issues/{row[1]}", attachment)

    def test_malformed_report_never_posts_and_names_the_failure(self) -> None:
        source = report([]).replace("0 candidates", "1 candidates")
        error = io.StringIO()
        with mock.patch.object(sender, "webhook_url", return_value="http://localhost/api/webhooks/1/fixture-secret"), \
                mock.patch.object(sys, "stdin", io.StringIO(source)), \
                mock.patch.object(digest, "archive") as archive, \
                mock.patch.object(sender, "deliver") as deliver, contextlib.redirect_stderr(error):
            self.assertEqual(sender.main([]), 1)
        archive.assert_not_called()
        deliver.assert_not_called()
        self.assertEqual(error.getvalue().strip(), "digest_report_invalid")

    def test_stage_failures_name_the_operation_without_exception_values(self) -> None:
        for operation, failure in (("webhook_url", "digest_webhook_read_failed"),
                                   ("validate", "digest_report_validation_failed"),
                                   ("deliver", "digest_delivery_failed")):
            error = io.StringIO()
            with mock.patch.object(sender, "webhook_url", return_value="http://localhost/api/webhooks/1/fixture-secret"), \
                    mock.patch.object(sys, "stdin", io.StringIO("plain report")), \
                    mock.patch.object(sender, operation, side_effect=OSError("private credential")), \
                    contextlib.redirect_stderr(error):
                self.assertEqual(sender.main([]), 1)
            self.assertEqual(error.getvalue().strip(), failure)
        for operation, failure in (("presentation", "digest_summary_failed"), ("archive", "digest_archive_failed")):
            error = io.StringIO()
            with mock.patch.object(sender, "webhook_url", return_value="http://localhost/api/webhooks/1/fixture-secret"), \
                    mock.patch.object(sys, "stdin", io.StringIO(report([]))), \
                    mock.patch.object(digest, operation, side_effect=ValueError("private credential")), \
                    contextlib.redirect_stderr(error):
                self.assertEqual(sender.main([]), 1)
            self.assertEqual(error.getvalue().strip(), failure)

    def test_archive_failure_never_sends_discord(self):
        with mock.patch.object(sender, "webhook_url", return_value="http://localhost/api/webhooks/1/fixture-secret"), \
                mock.patch.object(sys, "stdin", io.StringIO(report([]))), \
                mock.patch.object(digest, "archive", side_effect=ValueError("digest_archive_failed")), \
                mock.patch.object(sender, "deliver") as deliver, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sender.main([]), 1)
        deliver.assert_not_called()

    def test_archive_uses_existing_bot_transport_and_deduplication(self):
        comment = mock.Mock(return_value={"url": FULL})
        module = types.SimpleNamespace(comment=comment, CommentError=RuntimeError)
        with mock.patch.dict(sys.modules, {"github_comment": module}):
            self.assertEqual(digest.archive("full list", "example/catalog#4"), FULL)
            self.assertEqual(digest.archive("full list", "example/catalog#4"), FULL)
        comment.assert_called_with("issue", 4, "full list", repo="example/catalog", dedupe_body=True)


if __name__ == "__main__":
    unittest.main()
