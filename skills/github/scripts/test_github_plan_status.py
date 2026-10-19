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
                self.assertEqual(result["status"], "hold")
                self.assertEqual(result["scope_status"], hold or "unstatused")
                self.assertTrue(result["scope_hold"])
                self.assertFalse(result["wait_review_required"])
                self.assertFalse(result["labels_writable"])
                self.assertNotIn("active", result["labels"])
                self.assertEqual(issue, before)

    def test_package_prohibits_label_changes_for_unread_waits_and_scope_holds(self) -> None:
        names = github_plan_status.DEFAULT_STATUS_LABELS
        review = waits.current({}, [], set())
        cases = [
            ("open", [names["plan"], names["waiting"]], review),
            ("open", [names["plan"], names["stale"], names["active"]], self.clear),
            ("open", [names["plan"], names["done"], names["active"]], self.clear),
            ("open", [names["plan"]], self.clear),
        ]
        for state, statuses, record in cases:
            with self.subTest(state=state, labels=statuses):
                recorded = ["z:preserved", *statuses, "a:preserved"]
                issue = {**self.issue, "state": state, "labels": recorded}
                derived = github_plan_status.derive(issue, {"blocked_by": [self.open]}, record)
                changes = github_plan_status.label_changes(recorded, names, derived)
                self.assertEqual(changes["add"], [])
                self.assertEqual(changes["remove"], [])
                self.assertEqual(changes["result"], recorded)
                self.assertEqual(github_plan_status.project_labels(recorded, names, derived), recorded)
                self.assertFalse(derived["labels_writable"])

    def test_closed_unread_wait_repairs_lifecycle_without_erasing_waiting(self) -> None:
        names = github_plan_status.DEFAULT_STATUS_LABELS
        review = waits.current({}, [], set())
        recorded = [names[key] for key in ("plan", "active", "blocked", "waiting", "stale")] + ["bug"]
        derived = github_plan_status.derive({**self.issue, "state": "closed", "labels": recorded},
            {"blocked_by": []}, review)
        changes = github_plan_status.label_changes(recorded, names, derived)
        self.assertTrue(derived["labels_writable"])
        self.assertTrue(derived["wait_review_required"])
        self.assertEqual(set(changes["result"]), {names[key] for key in ("plan", "done", "waiting")} | {"bug"})
        outcome = github_plan_status.projection_outcome(derived, "reconciled")
        self.assertFalse(outcome["verified"])
        self.assertEqual(outcome["projection_status"], "reconciled")

    def test_explicit_projection_success_is_distinct_from_full_completion_and_optional_review(self) -> None:
        cases = [
            ("review_posting_authority", True, None, False, False),
            ("review_posting_authority", False, None, False, True),
            ("review_wait_record", True, "preserved", False, False),
            ("review_wait_record", True, "reconciled", False, True),
            ("review_wait_record", True, "reconciled", True, False),
        ]
        for status, required, projection, completion, ok in cases:
            with self.subTest(status=status, required=required, projection=projection, completion=completion):
                row = github_plan_status.classify_outcome(status, required=required,
                    projection_status=projection, completion_required=completion)
                self.assertEqual(row["ok"], ok)
                self.assertFalse(row["verified"])
                receipt = github_plan_status.receipt_summary([row])
                self.assertEqual(receipt["ok"], ok)
                self.assertEqual(receipt["coverage"], "partial")
        # A previously optimistic verification field cannot erase an explicit
        # failed required outcome when receipts are combined.
        denied = github_plan_status.classify_outcome("review_posting_authority", required=True)
        self.assertFalse(github_plan_status.receipt_summary([{**denied, "verified": True}])["ok"])

    def test_closed_issue_keeps_its_actual_person_wait_view(self) -> None:
        self.issue["state"] = "closed"
        held = github_plan_wait.reviewed(evidence="Closed source still awaits Client acceptance",
            waiting_for="Client acceptance", ends_when="Client records acceptance")
        result = self.derive([], held)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["waits"], held["waits"])
        self.assertEqual(set(result["labels"]), {"done", "waiting"})

    def test_formatted_old_fields_cannot_contradict_the_derived_view(self) -> None:
        original = ("**State:** Blocked.\n- Blocked by: fixture/product#99\n"
                    "Waiting since: superseded timestamp\nParked until: superseded request\n"
                    "Next action: Implement export.\n")
        derived = self.derive([])
        result = github_plan_status.render_status(original, derived)
        fields = dict(line.split(":", 1) for line in result.splitlines() if ":" in line)
        self.assertEqual(fields["State"].strip().casefold().rstrip("."), derived["status"])
        self.assertEqual(derived["blocked_by"], [])
        self.assertNotIn("Blocked.", result)
        self.assertNotIn("fixture/product#99", result)
        self.assertNotIn("superseded", result)
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

    def test_rest_and_graphql_bot_authors_resolve_the_same_trusted_wait(self) -> None:
        login = "fixture-app"
        bot_login = login + "[bot]"
        held = waits.reviewed(evidence="Historical automation recorded Client acceptance",
            waiting_for="Client acceptance", ends_when="Client accepts")
        sources = [
            {"user": {"login": bot_login, "type": "Bot", "id": 701}},
            {"author": {"login": login, "__typename": "Bot", "databaseId": 701}},
        ]
        for author in sources:
            with self.subTest(author=author):
                source = {**author, "body": waits.marker(held)}
                self.assertTrue(waits.author_is_trusted(source, "owner", author_logins=(bot_login,)))
                self.assertTrue(waits.author_is_trusted(source, "owner", author_ids=(701,)))
                self.assertEqual(waits.current({}, [source], {bot_login}), held)
                predicate = lambda value: waits.author_is_trusted(value, "owner", author_logins=(bot_login,))
                self.assertEqual(waits.current({}, [source], predicate), held)
        impostor = {"author": {"login": login, "__typename": "User", "databaseId": 702},
                    "body": waits.marker(self.clear)}
        self.assertFalse(waits.author_is_trusted(impostor, "owner", author_logins=(bot_login,), author_ids=(701,)))
        issue = {**sources[0], "body": waits.marker(held)}
        self.assertEqual(waits.current(issue, [impostor], {bot_login}), held)

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

    def test_render_field_boundaries_preserve_independent_context(self) -> None:
        action = "Implement export without replacing retained evidence."
        obsolete = "retired provider sign-off"
        marker = waits.marker(self.clear)
        boundaries = {
            "next action": f"Next action: {action}\n",
            "evidence": "Evidence: Retain the original capture.\n",
            "heading": "### Recovery\nPreserve the retained artifact.\n",
            "marker": marker + "\n",
            "blank": "\nPreserve this separate recovery paragraph.\n",
        }
        for indentation in ("", "  "):
            for kind, boundary in boundaries.items():
                with self.subTest(indentation=indentation, boundary=kind):
                    retained = "\n".join(indentation + line if line else ""
                                         for line in boundary.split("\n"))
                    original = "Waiting for: superseded request\n" + indentation + obsolete + "\n" + retained
                    rendered = github_plan_status.render_status(original, self.derive([]))
                    self.assertNotIn(obsolete, rendered)
                    self.assertIn(retained, rendered)

    def test_render_preserves_fenced_status_examples_as_authored_context(self) -> None:
        for fence in ("```text", "~~~text"):
            with self.subTest(fence=fence):
                closing = fence[:3]
                example = fence + "\nState: Historical example\nBlocked by: example/product#91\n" + closing
                action = "Next action: Implement export."
                original = example + "\n" + action
                rendered = github_plan_status.render_status(original, self.derive([]))
                self.assertIn(example, rendered)
                self.assertIn(action, rendered)


if __name__ == "__main__":
    unittest.main()
