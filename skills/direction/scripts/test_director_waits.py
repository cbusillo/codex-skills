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

import director_waits

NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def issue(number=1, reason="Director to approve", labels=None, extra=""):
    return {"number": number, "state": "open", "labels": labels or [],
            "title": "Wait", "html_url": f"https://github.com/owner/repo/issues/{number}",
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

    def test_age_uses_explicit_start_and_flags_older_than_verification(self):
        rows = director_waits.wait_rows("owner/repo", [issue(extra=
            "Waiting since: 2026-10-01\nLast verified: October 5, 2026, source read"), issue(2)],
            ["owner", "Director"], "owner", NOW)
        self.assertEqual(rows[0]["wait_age_days"], 8)
        self.assertTrue(rows[0]["possibly_stale"])
        self.assertTrue(rows[1]["wait_age_unknown"])
        self.assertIsNone(rows[1]["wait_age_days"])

    def test_collection_has_no_label_filter_and_preserves_failed_coverage(self):
        reader = Mock()
        reader.paged_json.side_effect = [[{"full_name": "owner/first"}, {"full_name": "owner/second"},
                                         {"full_name": "other/ignored"}],
                                        [issue()], director_waits.GitHubReadShapeError("unavailable")]
        with patch.object(director_waits.github_identity, "github_app_config", return_value={}):
            result = director_waits.collect(reader, "owner", ["owner", "Director"], NOW)
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["waits"]), 1)
        self.assertEqual(reader.paged_json.call_args_list[1].kwargs["params"], {"state": "open"})
        self.assertEqual(len(reader.paged_json.call_args_list), 3)

    def test_limits_are_incomplete_not_empty_or_complete(self):
        reader = Mock()
        reader.paged_json.side_effect = [[{"full_name": "owner/repo"}, {"full_name": "owner/next"}],
                                        [issue(), issue(2)]]
        with patch.object(director_waits.github_identity, "github_app_config", return_value={}):
            result = director_waits.collect(reader, "owner", ["owner", "Director"], NOW, 1, 1)
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual(len(result["waits"]), 1)

    def test_headingless_age_and_product_owner_disambiguation(self):
        headingless = {**issue(extra="Waiting since: 2026-10-01\nLast verified: 2026-10-05"),
                       "body": "Waiting for: Director to choose\nWaiting since: 2026-10-01\nLast verified: 2026-10-05"}
        rows = director_waits.wait_rows("admin/repo", [headingless,
            issue(2, "product owner Client to accept"), issue(3, "owner to choose")],
            ["admin", "Director", "owner"], "admin", NOW)
        self.assertEqual([row["number"] for row in rows], [1, 3])
        self.assertEqual(rows[0]["wait_age_days"], 8)
        self.assertTrue(rows[0]["possibly_stale"])
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

    def test_discovery_failure_preserves_scope_and_archived_waits_are_marked(self):
        reader = Mock()
        with patch.object(director_waits.github_identity, "github_app_config", side_effect=
                          director_waits.github_identity.GitHubAppError("incomplete identity")):
            result = director_waits.collect(reader, "owner", ["Director"], NOW)
        self.assertFalse(result["complete"])
        self.assertIn("scope", result)
        self.assertEqual(result["repositories_scanned"], [])
        reader.paged_json.side_effect = [[{"full_name": "owner/repo", "archived": True}], [issue()]]
        with patch.object(director_waits.github_identity, "github_app_config", return_value={}):
            result = director_waits.collect(reader, "owner", ["Director"], NOW)
        self.assertTrue(result["waits"][0]["repository_archived"])

    def test_unqualified_owner_within_reason_and_multiple_wait_age(self):
        records = [issue(1, "decision from the owner on pricing"), issue(2, "approval by the owner"),
                   issue(3, "product owner Client, then Director")]
        rows = director_waits.wait_rows("admin/repo", records, ["admin", "owner", "Director"], "admin", NOW)
        self.assertEqual([row["number"] for row in rows], [1, 2])
        sample = issue(extra="Waiting since: 2026-09-01\nWaiting for: Client testing\nWaiting for: Director to decide")
        self.assertTrue(director_waits.wait_rows("admin/repo", [sample], ["owner", "Director"], "admin", NOW)[0]["wait_age_unknown"])

    def test_no_index_reports_alias_gap_and_explicit_name_is_supported(self):
        people = {"status": "no_index", "director_names": [], "director_ambiguous": False}
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
