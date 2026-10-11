#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Behavior proof for full Director waits, age evidence and coverage bounds."""

from datetime import datetime, timezone
import unittest
from contextlib import redirect_stdout
import io
import json
from unittest.mock import Mock, patch
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import director_waits

NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def issue(number=1, reason="Director to approve", labels=None, extra=""):
    return {"number": number, "state": "open", "labels": labels or [],
            "title": "Wait", "html_url": f"https://github.com/owner/repo/issues/{number}",
            "repository_url": "https://api.github.com/repos/owner/repo",
            "body": "## Current Status\nWaiting for: " + reason + "\n" + extra}


class WaitTests(unittest.TestCase):
    def test_all_status_labels_and_asked_questions_are_included(self):
        records = [issue(1), issue(2, labels=[{"name": "plan:active"}]),
                   issue(3, labels=[{"name": "plan:waiting"}], extra="Owner question: existing question"),
                   issue(4, "Chris to resume", labels=[{"name": "direction"}]),
                   issue(5, "owner to choose")]
        rows = director_waits.wait_rows("owner/repo", records, ["owner", "Director", "Chris"], "owner", NOW)
        self.assertEqual([row["number"] for row in rows], [1, 2, 3, 4, 5])
        self.assertEqual(rows[3]["wait_kind"], "director_hold")

    def test_negations_agent_waits_and_later_director_steps_are_excluded(self):
        records = [issue(reason="No Director decision needed"), issue(reason="Agent; nothing from owner"),
                   issue(reason="Client testing, then Director"),
                   {**issue(), "state": "closed"}, {**issue(), "pull_request": {}}]
        self.assertEqual(director_waits.wait_rows("owner/repo", records, ["owner", "Director"], "owner", NOW), [])

    def test_age_and_verification_distinguish_maintained_and_unverified_waits(self):
        rows = director_waits.wait_rows("owner/repo", [issue(extra=
            "Waiting since: 2026-10-01\nLast verified: October 5, 2026, source read"), issue(2)],
            ["owner", "Director"], "owner", NOW)
        self.assertEqual(rows[0]["wait_age_days"], 8)
        self.assertFalse(rows[0]["possibly_stale"])
        self.assertTrue(rows[1]["possibly_stale"])
        self.assertTrue(rows[1]["wait_age_unknown"])
        self.assertIsNone(rows[1]["wait_age_days"])

    def test_search_covers_repositories_without_label_or_question_filter(self):
        reader = Mock()
        records = [issue(), {**issue(2), "repository_url": "https://api.github.com/repos/owner/second"}]
        reader.request.return_value = SimpleNamespace(body={
            "items": records, "total_count": 2, "incomplete_results": False})
        result = director_waits.collect_query(reader, "owner", ["owner", "Director"], NOW)
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["waits"]), 2)
        self.assertEqual(reader.request.call_count, 1)
        self.assertEqual(reader.request.call_args.kwargs["bucket"], "search")
        query = parse_qs(urlsplit(reader.request.call_args.args[1]).query)["q"][0]
        self.assertIn("user:owner", query)
        self.assertNotIn("label:", query)
        self.assertNotIn("repo:", query)

    def test_search_metadata_limits_and_partial_failure_preserve_rows(self):
        for metadata in ({"total_count": 2, "incomplete_results": False},
                         {"total_count": 1, "incomplete_results": True},
                         {"total_count": 1}):
            reader = Mock()
            reader.request.return_value = SimpleNamespace(body={"items": [issue()], **metadata})
            result = director_waits.collect_query(reader, "owner", ["Director"], NOW, 1)
            self.assertFalse(result["complete"])
            self.assertEqual(len(result["waits"]), 1)
            self.assertTrue(result["errors"])
        reader = Mock()
        reader.request.side_effect = [SimpleNamespace(body={
            "items": [issue(i) for i in range(100)], "total_count": 101,
            "incomplete_results": False}), director_waits.GitHubReadShapeError("quota exhausted")]
        result = director_waits.collect_query(reader, "owner", ["Director"], NOW)
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["waits"]), 100)

    def test_pagination_and_duplicate_or_changed_total_detection(self):
        first = {"items": [issue(i) for i in range(100)], "total_count": 101, "incomplete_results": False}
        for second, complete in (({"items": [issue(101)], "total_count": 101, "incomplete_results": False}, True),
                                 ({"items": [issue(1)], "total_count": 101, "incomplete_results": False}, False),
                                 ({"items": [issue(101)], "total_count": 102, "incomplete_results": False}, False)):
            reader = Mock()
            reader.request.side_effect = [SimpleNamespace(body=first), SimpleNamespace(body=second)]
            result = director_waits.collect_query(reader, "owner", ["Director"], NOW)
            self.assertEqual(result["complete"], complete)
            self.assertEqual(reader.request.call_count, 2)

    def test_wait_begins_after_verification_or_verification_is_unusable(self):
        for verified, stale in (("2026-09-30", True), ("2026-10-05", False),
                                ("Not yet verified", True)):
            row = director_waits.wait_rows("owner/repo", [issue(extra=
                f"Waiting since: 2026-10-01\nLast verified: {verified}")], ["Director"], "owner", NOW)[0]
            self.assertEqual(row["possibly_stale"], stale)

    def test_headingless_age_and_product_owner_disambiguation(self):
        headingless = {**issue(extra="Waiting since: 2026-10-01\nLast verified: 2026-10-05"),
                       "body": "Waiting for: Director to choose\nWaiting since: 2026-10-01\nLast verified: 2026-10-05"}
        rows = director_waits.wait_rows("admin/repo", [headingless,
            issue(2, "product owner Client to accept"), issue(3, "owner to choose")],
            ["admin", "Director", "owner"], "admin", NOW)
        self.assertEqual([row["number"] for row in rows], [1, 3])
        self.assertEqual(rows[0]["wait_age_days"], 8)
        self.assertFalse(rows[0]["possibly_stale"])
        self.assertTrue(rows[1]["verification_unknown"])

    def test_verified_field_uses_shared_bold_and_comment_normalization(self):
        sample = issue(extra="Waiting since: 2026-10-01\n<!-- Last verified: 2026-10-09 -->\n"
                       "**Last verified:** Oct 5, 2026")
        sample["updated_at"] = "2026-10-08T12:00:00Z"
        row = director_waits.wait_rows("admin/repo", [sample], ["Director"], "admin", NOW)[0]
        self.assertTrue(row["last_verified"].startswith("2026-10-05"))
        self.assertEqual(row["recorded_at"], sample["updated_at"])
        self.assertEqual(director_waits.wait_rows("admin/repo", [issue(reason="upstream owner to merge"),
            issue(reason="code owner review")], ["admin", "Director", "owner"], "admin", NOW), [])

    def test_search_failure_or_malformed_item_keeps_explicit_scope(self):
        reader = Mock()
        reader.request.side_effect = director_waits.GitHubReadShapeError("unavailable")
        result = director_waits.collect_query(reader, "owner", ["Director"], NOW)
        self.assertFalse(result["complete"])
        self.assertIn("scope", result)
        reader.request.side_effect = None
        reader.request.return_value = SimpleNamespace(body={"total_count": 2,
            "incomplete_results": False, "items": [None, issue()]})
        result = director_waits.collect_query(reader, "owner", ["Director"], NOW)
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["waits"]), 1)

    def test_unqualified_owner_within_reason_and_multiple_wait_age(self):
        records = [issue(1, "decision from the owner on pricing"), issue(2, "approval by the owner"),
                   issue(3, "product owner Client, then Director")]
        rows = director_waits.wait_rows("admin/repo", records, ["admin", "owner", "Director"], "admin", NOW)
        self.assertEqual([row["number"] for row in rows], [1, 2])
        sample = issue(extra="Waiting since: 2026-09-01\nWaiting for: Client testing\nWaiting for: Director to decide")
        self.assertTrue(director_waits.wait_rows("admin/repo", [sample], ["owner", "Director"], "admin", NOW)[0]["wait_age_unknown"])

    def test_parked_and_waiting_searches_merge_without_losing_waits(self):
        parked = {**issue(2), "body": "## Current Status\nParked until: Director selects it"}
        reader = Mock()
        reader.request.side_effect = [SimpleNamespace(body={"items": [issue()], "total_count": 1, "incomplete_results": False}),
            SimpleNamespace(body={"items": [issue(), parked], "total_count": 2, "incomplete_results": False})]
        report = director_waits.collect(reader, "owner", ["Director"], NOW)
        self.assertTrue(report["complete"])
        self.assertEqual([row["number"] for row in report["waits"]], [1, 2])
        self.assertEqual(reader.request.call_count, 2)

    def test_product_wait_cannot_hide_director_decision_or_hold(self):
        for reason in ("Chris to approve", "Chris to resume"):
            mixed = issue(reason="product owner Client to accept", extra="Waiting for: " + reason)
            row = director_waits.wait_rows("admin/repo", [mixed], ["owner", "Director", "Chris"], "admin", NOW)[0]
            self.assertEqual(row["waiting_for"], reason)

    def test_old_verification_exposes_age_without_inventing_a_policy_threshold(self):
        row = director_waits.wait_rows("owner/repo", [issue(extra="Last verified: 2026-03-01")],
                                      ["Director"], "owner", NOW)[0]
        self.assertEqual(row["verification_age_days"], (NOW - datetime(2026, 3, 1, tzinfo=timezone.utc)).days)

    def test_no_index_reports_alias_gap_and_explicit_name_is_supported(self):
        people = {"status": "ok", "director_names": [], "director_ambiguous": False}
        for extra, expected in (([], 1), (["--director-name", "Pat"], 0)):
            with self.subTest(extra=extra), patch.object(director_waits.sys, "argv",
                    ["director_waits.py", "--owner", "admin", *extra]), \
                 patch.object(director_waits.attention, "people_identities", return_value=people), \
                 patch.object(director_waits, "GitHubReader"), \
                 patch.object(director_waits, "collect", return_value={"complete": True, "errors": [], "waits": []}), \
                 redirect_stdout(io.StringIO()) as output:
                self.assertEqual(director_waits.main(), expected)
                result = json.loads(output.getvalue())
                self.assertEqual(result["complete"], expected == 0)


if __name__ == "__main__":
    unittest.main()
