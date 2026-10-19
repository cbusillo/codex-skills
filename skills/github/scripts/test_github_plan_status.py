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
from github_plan_status import waits
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

    def test_scope_review_holds_survive_clear_wait_and_empty_native_graph(self) -> None:
        names = {key: f"custom:{key}" for key in ("plan", "active", "blocked", "waiting", "stale", "done")}
        for hold in ("stale", "done", None):
            with self.subTest(hold=hold):
                issue = {**self.issue, "labels": [{"name": names["plan"]},
                    *([{ "name": names[hold]}] if hold else [])]}
                before = copy.deepcopy(issue)
                result = github_plan_status.derive(issue, {"blocked_by": []}, self.clear, label_names=names)
                self.assertEqual(result["status"], "review")
                self.assertTrue(result["scope_hold"])
                self.assertNotIn("active", result["labels"])
                self.assertEqual(issue, before)

    def test_closed_issue_keeps_its_actual_person_wait_view(self) -> None:
        self.issue["state"] = "closed"
        held = github_plan_wait.reviewed(evidence="Closed source still awaits Client acceptance",
            waiting_for="Client acceptance", ends_when="Client records acceptance")
        result = self.derive([], held)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["waits"], held["waits"])
        self.assertEqual(set(result["labels"]), {"done", "waiting"})

    def test_formatted_old_fields_cannot_contradict_the_derived_view(self) -> None:
        original = "**State:** Blocked.\n- Blocked by: fixture/product#99\nNext action: Implement export.\n"
        derived = self.derive([])
        result = github_plan_status.render_status(original, derived)
        fields = dict(line.split(":", 1) for line in result.splitlines() if ":" in line)
        self.assertEqual(fields["State"].strip().casefold().rstrip("."), derived["status"])
        self.assertEqual(derived["blocked_by"], [])
        self.assertNotIn("Blocked.", result)
        self.assertNotIn("fixture/product#99", result)
        self.assertIn("Next action: Implement export.", result)

    def test_packaged_wait_resolver_accepts_numeric_identity_authority(self) -> None:
        trusted_id, other_id = 701, 702
        held = waits.reviewed(evidence="Trusted owner requested acceptance",
            waiting_for="Client acceptance", ends_when="Client records acceptance")
        clear = waits.reviewed(evidence="Acceptance reported complete")
        issue = {"user": {"id": trusted_id, "login": "same-login"}, "body": waits.marker(held)}
        comments = [{"user": {"id": other_id, "login": "same-login"}, "body": waits.marker(clear)}]
        trusted = lambda source: (source.get("user") or {}).get("id") == trusted_id
        self.assertEqual(waits.current(issue, comments, trusted), held)
        # A renamed trusted identity keeps its authority; a reused login does not.
        comments.append({"user": {"id": trusted_id, "login": "renamed-owner"}, "body": waits.marker(clear)})
        self.assertEqual(waits.current(issue, comments, trusted), clear)

    def test_label_changes_preserve_custom_nonstatus_labels_and_both_holds(self) -> None:
        names = {key: f"view:{key}" for key in github_plan_status.DEFAULT_STATUS_LABELS}
        unrelated = {"customer:requested", "priority:urgent"}
        observed = [names["plan"], names["active"], *unrelated]
        issue = {**self.issue, "labels": observed}
        held = waits.reviewed(evidence="Client's acceptance remains separate from engineering",
            waiting_for="Client acceptance", ends_when="Client records acceptance")
        derived = github_plan_status.derive(issue, {"blocked_by": [self.open]}, held, label_names=names)
        changes = github_plan_status.label_changes(observed, names, derived)
        self.assertEqual(set(changes["add"]), {names[key] for key in ("blocked", "waiting")})
        self.assertEqual(changes["remove"], [names["active"]])
        self.assertEqual(set(changes["result"]), unrelated | {names[key] for key in ("plan", "blocked", "waiting")})
        released = github_plan_status.derive({**issue, "labels": changes["result"]},
            {"blocked_by": [self.closed]}, self.clear, label_names=names)
        final = github_plan_status.label_changes(changes["result"], names, released)
        self.assertEqual(set(final["result"]), unrelated | {names[key] for key in ("plan", "active")})

    def test_render_drops_wrapped_old_fields_and_keeps_the_next_action(self) -> None:
        action = "Implement the CSV export."
        original = ("Keep this request context.\n**Blocked by:** retired prerequisite\n"
                    "    fixture/obsolete#99\nWaiting for: superseded acceptance\n"
                    "    an old sign-off that no longer applies\nNext action: " + action + "\n")
        rendered = github_plan_status.render_status(original, self.derive([]))
        for obsolete in ("retired prerequisite", "fixture/obsolete#99", "superseded acceptance", "old sign-off"):
            self.assertNotIn(obsolete, rendered)
        self.assertIn(action, rendered)
        self.assertIn("Keep this request context.", rendered)


if __name__ == "__main__":
    unittest.main()
