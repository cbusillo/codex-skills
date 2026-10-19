#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Wait record authority, examples, corruption and author boundaries."""

from __future__ import annotations

import copy
import unittest
import subprocess
import sys
from pathlib import Path

import github_plan_wait as wait_record
import github_plan_body
from github_plan_status import waits


class WaitRecordTests(unittest.TestCase):
    def test_catalog_namespace_consumer_can_validate_waits(self):
        result = subprocess.run([
            sys.executable, "-I", "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from skills.github.scripts import github_plan_wait; "
            "github_plan_wait.reviewed(evidence='Namespace consumer')",
            str(Path(__file__).resolve().parents[3]),
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

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
                self.assertEqual(wait_record.without_records(example), example)

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

    def test_packaged_resolver_uses_managed_wait_and_preserves_original_request(self):
        original = {"user": {"login": "owner"}, "body": wait_record.marker(self.clear)}
        issue = {**original, "body": github_plan_body.wrap_contributor_plan(
            original, wait_record.marker(self.hold))}
        before = copy.deepcopy(issue)
        self.assertEqual(waits.current(issue, [], {"owner"}), self.hold)
        self.assertEqual(issue, before)
        malformed = {**issue, "body": issue["body"].replace(github_plan_body.PLAN_MANAGED_END, "")}
        with self.assertRaises(ValueError):
            waits.current(malformed, [], {"owner"})

    def test_pure_authority_policy_keeps_historical_and_verified_identities(self):
        trusted = waits.trusted_authors("Owner", configured_bots=("Historical-App[bot]",),
                                       automation="Current-App[bot]", expected_actor="Verified-App[bot]")
        self.assertEqual(trusted, {"owner", "historical-app[bot]", "current-app[bot]", "verified-app[bot]"})
        self.assertTrue(waits.author_is_trusted({"user": {"login": "HISTORICAL-APP[bot]"}},
                                              "owner", author_logins=trusted))
        self.assertTrue(waits.author_is_trusted({"user": {"id": 701, "login": "renamed"}},
                                              "owner", author_ids=(701,)))
        self.assertFalse(waits.author_is_trusted({"user": {"id": 702, "login": "decision-reader"}},
                                               "owner", author_logins=trusted, author_ids=(701,)))


if __name__ == "__main__":
    unittest.main()
