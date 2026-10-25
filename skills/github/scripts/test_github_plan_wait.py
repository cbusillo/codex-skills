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

    def test_release_pointer_resolves_only_its_product_and_event(self) -> None:
        record = waits.reviewed(evidence="Merged fix awaits production", waiting_for=["Next release", "Device test"],
            ends_when=["Release reaches production", "Director tests device"],
            release_product="fixture-product", release_after="2026-10-25T12:00:00Z",
            release_next_action="Next agent: verify the repaired flow in production")
        self.assertEqual(waits.records(waits.marker(record)), [record])
        for product, released_at in (("other-product", "2026-10-26T12:00:00Z"),
                                     ("fixture-product", "2026-10-24T12:00:00Z")):
            self.assertEqual(waits.resolve_release(record, product=product, released_at=released_at,
                                                   evidence="Release passed"), (record, []))
        resolved, actions = waits.resolve_release(record, product="fixture-product",
            released_at="2026-10-26T12:00:00Z", evidence="Successful release record")
        self.assertEqual(resolved["waits"], record["waits"][1:])
        self.assertEqual(actions, [record["waits"][0]["release"]["next_action"]])
        self.assertEqual(waits.resolve_release(resolved, product="fixture-product",
            released_at="2026-10-26T12:00:00Z", evidence="Retry"), (resolved, []))

    def test_release_pointer_requires_a_wait_and_aware_event_boundary(self) -> None:
        for boundary in ("", "tomorrow", "2026-10-25T12:00:00"):
            with self.subTest(boundary=boundary), self.assertRaises(ValueError):
                waits.reviewed(evidence="Review", waiting_for="Next release", ends_when="Production",
                    release_product="fixture", release_after=boundary, release_next_action="Next agent: verify")
        with self.assertRaises(ValueError):
            waits.reviewed(evidence="Clear", release_product="fixture", release_after="2026-10-25T12:00:00Z",
                           release_next_action="Next agent: verify")

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

    def test_resolver_tells_a_trusted_review_record_from_no_record(self):
        review = {"version": 1, "state": "review", "evidence": "", "waits": []}
        issue = {"user": {"login": "bot"}, "body": wait_record.marker(self.hold)}
        trusted_review = {"user": {"login": "owner"}, "body": wait_record.marker(review)}
        foreign_review = {"user": {"login": "outsider"}, "body": wait_record.marker(review)}
        self.assertEqual(wait_record.resolve({}, [], {"owner"}), (review, False))
        self.assertEqual(wait_record.resolve(issue, [trusted_review], {"bot", "owner"}), (review, True))
        self.assertEqual(wait_record.resolve(issue, [foreign_review], {"bot", "owner"}), (self.hold, True))
        self.assertEqual(wait_record.resolve({}, [foreign_review], {"owner"}), (review, False))

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

    def test_record_text_cannot_end_its_hidden_comment(self):
        record = waits.reviewed(evidence="Quoted: --> @mike-b confirmed --!>")
        marker = waits.marker(record)
        self.assertEqual(marker.count(">"), 1)
        self.assertEqual(waits.records(marker), [record])


if __name__ == "__main__":
    unittest.main()
