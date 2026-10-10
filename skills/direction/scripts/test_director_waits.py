#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Behavior proof for full Director waits, age evidence and coverage bounds."""

from datetime import datetime, timezone
import unittest
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


if __name__ == "__main__":
    unittest.main()
