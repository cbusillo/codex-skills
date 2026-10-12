#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Wait record authority, examples, corruption and author boundaries."""

from __future__ import annotations

import copy
import unittest

import github_plan_wait as wait_record


class WaitRecordTests(unittest.TestCase):
    def setUp(self):
        self.clear = wait_record.reviewed(evidence="Full issue and discussion reviewed; no hold remains")
        self.hold = wait_record.reviewed(evidence="Director requested testing", waiting_for="Director's test",
                                  ends_when="Director records acceptance")

    def test_published_wait_and_release_round_trip(self):
        for record in (self.clear, self.hold):
            with self.subTest(record=record):
                self.assertEqual(wait_record.records(wait_record.marker(record)), [record])

    def test_quoted_fenced_and_html_examples_cannot_clear_a_hold(self):
        marker = wait_record.marker(self.clear)
        for example in ("> " + marker, "    " + marker, "```json\n" + marker + "\n```",
                        "~~~\n" + marker + "\n~~~", "<pre>\n" + marker + "\n</pre>",
                        "<details>\n" + marker + "\n</details>"):
            with self.subTest(example=example):
                self.assertEqual(wait_record.records(example), [])

    def test_foreign_comment_cannot_clear_an_authorized_wait(self):
        issue = {"user": {"login": "bot"}, "body": wait_record.marker(self.hold)}
        comments = [{"user": {"login": "outsider"}, "body": wait_record.marker(self.clear)}]
        self.assertEqual(wait_record.current(issue, comments, {"bot", "owner"}), self.hold)
        comments.append({"user": {"login": "owner"}, "body": wait_record.marker(self.clear)})
        self.assertEqual(wait_record.current(issue, comments, {"bot", "owner"}), self.clear)

    def test_prose_edit_does_not_erase_the_last_review_record(self):
        issue = {"user": {"login": "bot"}, "body": "Waiting for: None; agents act next"}
        comments = [{"user": {"login": "bot"}, "body": wait_record.marker(self.hold)},
                    {"user": {"login": "bot"}, "body": "Everything is released; continue"}]
        self.assertEqual(wait_record.current(issue, comments, {"bot"}), self.hold)

    def test_malformed_or_duplicate_authorized_records_refuse(self):
        for body in (wait_record.PREFIX + "{} -->", wait_record.PREFIX + "not-json -->",
                     wait_record.marker(self.clear) + "\n" + wait_record.marker(self.hold),
                     wait_record.marker(self.clear)[:-3]):
            with self.subTest(body=body), self.assertRaises(ValueError):
                wait_record.current({"user": {"login": "bot"}, "body": body}, [], {"bot"})

    def test_corrupt_wait_is_never_interpreted_as_clear(self):
        for key, value in (("version", 999), ("state", "clear"), ("waits", []),
                           ("evidence", ""), ("state", [])):
            with self.subTest(key=key, value=value):
                malformed = copy.deepcopy(self.hold)
                malformed[key] = value
                with self.assertRaises(ValueError): wait_record.records(wait_record.marker(malformed))

    def test_multiple_waits_survive_review_and_readback(self):
        wait = copy.deepcopy(self.hold)
        wait["waits"].extend(wait_record.reviewed(evidence="Client requested pricing decision",
            waiting_for="Client's pricing decision", ends_when="Client records chosen price")["waits"])
        self.assertEqual(wait_record.records(wait_record.marker(wait)), [wait])


if __name__ == "__main__":
    unittest.main()
