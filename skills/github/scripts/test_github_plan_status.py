#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Behavioral coverage for status derived from native blockers and reviewed waits."""
from __future__ import annotations

import copy
import unittest

import github_plan_status
import github_plan_wait


class DerivedStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.issue = {"repo": "fixture/product", "number": 7, "state": "open",
                      "labels": [{"name": "plan:blocked"}, {"name": "plan:waiting"}],
                      "body": "## Current Status\nBlocked by: #5\nWaiting for: obsolete prose"}
        self.closed = {"repo": "fixture/product", "number": 5, "state": "closed"}
        self.open = {"repo": "fixture/prerequisite", "number": 6, "state": "open"}
        self.clear = github_plan_wait.reviewed(evidence="Full discussion reviewed; no person or event waits")

    def derive(self, blockers, wait=None):
        return github_plan_status.derive(self.issue,
            {"blocked_by": blockers, "blocking": [], "sub_issues": []}, self.clear if wait is None else wait)

    def test_last_native_blocker_closing_makes_work_active_despite_old_copies(self) -> None:
        before = copy.deepcopy(self.issue)
        result = self.derive([self.closed])
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["blocked_by"], [])
        self.assertEqual(result["waits"], [])
        self.assertEqual(set(result["labels"]), {"active"})
        self.assertEqual(self.issue, before)

    def test_only_open_native_blockers_are_reported(self) -> None:
        result = self.derive([self.closed, self.open])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blocked_by"], [self.open])
        self.assertEqual(set(result["labels"]), {"blocked"})

    def test_person_wait_survives_the_last_native_blocker(self) -> None:
        wait = github_plan_wait.reviewed(evidence="Director's current test request",
            waiting_for="Director", ends_when="Director reports the hands-on result")
        result = self.derive([self.closed], wait)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["blocked_by"], [])
        self.assertEqual(result["waits"], wait["waits"])
        self.assertEqual(set(result["labels"]), {"waiting"})

    def test_native_blocker_and_person_wait_are_both_preserved(self) -> None:
        wait = github_plan_wait.reviewed(evidence="Separate unresolved acceptance",
            waiting_for="Client", ends_when="Client accepts the release")
        result = self.derive([self.open], wait)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blocked_by"], [self.open])
        self.assertEqual(result["waits"], wait["waits"])
        self.assertEqual(set(result["labels"]), {"blocked", "waiting"})

    def test_unreviewed_wait_never_activates_work(self) -> None:
        unreviewed = github_plan_wait.current(self.issue, [], set())
        result = self.derive([self.closed], unreviewed)
        self.assertEqual(result["status"], "review")
        self.assertNotIn("active", result["labels"])

    def test_closed_issue_cannot_be_activated_by_an_empty_graph(self) -> None:
        self.issue["state"] = "closed"
        result = self.derive([])
        self.assertEqual(result["status"], "done")
        self.assertEqual(set(result["labels"]), {"done"})

    def test_formatted_old_fields_cannot_contradict_the_derived_view(self) -> None:
        original = "**State:** Blocked.\n- Blocked by: fixture/product#99\nNext action: Implement export.\n"
        result = github_plan_status.render_status(original, self.derive([]))
        self.assertIn("State: active.", result)
        self.assertIn("Blocked by: None.", result)
        self.assertNotIn("Blocked.", result)
        self.assertNotIn("fixture/product#99", result)
        self.assertIn("Next action: Implement export.", result)


if __name__ == "__main__":
    unittest.main()
