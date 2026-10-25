#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Replay the week's stranded records and bounded read failures offline."""
import base64
import copy
import datetime as dt
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from types import SimpleNamespace
from typing import Any
import sys

import direction_audit as audit
import direction_stranded as sweep
import digest_summary
from skills.github.scripts import github_api


class StrandedTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(Path(__file__).with_name("fixtures").joinpath("stranded-week-2026-10-15.json").read_text())
        self.records = copy.deepcopy(self.fixture["records"])
        self.now = dt.datetime.fromisoformat(self.fixture["now"])
        self.trusted = {"shiny-code-app[bot]", "cbusillo"}

    def report(self, **kwargs):
        return sweep.report(self.records, now=self.now, age_hours=2, trusted=self.trusted,
                            owners={"cbusillo"}, **kwargs)

    def test_week_examples_are_reported_without_mutation(self):
        before = copy.deepcopy(self.records)
        report = self.report()
        self.assertEqual({r["number"] for r in report["findings"]},
                         {r["item"]["number"] for r in self.records})
        self.assertTrue(all(r["age_hours"] >= 2 and r["next_actor"] and r["url"] for r in report["findings"]))
        self.assertEqual(self.records, before)
        self.assertTrue(report["complete"])

    def test_related_thread_decision_resolves_question_but_bot_chatter_does_not(self):
        q = self.records[0]["comments"][0]
        answer = {"id": 99, "created_at": "2026-10-15T18:00:00Z", "body": "Q327 yes", "user": {"login": "shiny-code-app[bot]"}, "html_url": "https://github.com/cbusillo/direction/issues/1#issuecomment-99"}
        self.records[1]["comments"].append(answer)
        self.assertIn(3604, {r["number"] for r in self.report()["findings"]})
        answer["body"] = "Director decision: Q327 yes"
        self.assertNotIn(3604, {r["number"] for r in self.report()["findings"]})
        answer["user"]["login"] = "untrusted"
        self.assertIn(3604, {r["number"] for r in self.report()["findings"]})
        answer["user"]["login"] = "cbusillo"
        answer["body"] = f"Done: {q['html_url']}"
        self.assertNotIn(3604, {r["number"] for r in self.report()["findings"]})

    def test_presentation_names_questions_and_issues_from_their_sources(self):
        for record in self.records:
            record["item"]["title"] = f"Task {record['item']['number']}"
        result = self.report()
        question = next(r for r in result["findings"] if r["kind"] == "unanswered_question")
        source = next(c for record in self.records for c in record["comments"]
                      if c["html_url"] == question["url"])
        self.assertEqual(question["title"], sweep.question_body(source).partition("\n")[0])
        issue = next(r for r in result["findings"] if r["kind"] == "merged_link_needs_review")
        source_item = next(r["item"] for r in self.records if r["item"]["number"] == issue["number"])
        self.assertEqual(issue["title"], source_item["title"])
        rendered = sweep.markdown(result)
        self.assertIn(question["title"], rendered)
        self.assertIn(issue["title"], rendered)

    def test_closed_research_promise_survives_issue_closure_until_linked_report(self):
        promise = self.records[4]["comments"][0]
        self.assertIn(1791, {r["number"] for r in self.report()["findings"]})
        self.records[4]["comments"].append({"id": 100, "created_at": "2026-10-15T13:36:21Z",
            "user": {"login": "shiny-code-app[bot]"}, "body": f"Comparison posted for Q249: {promise['html_url']}",
            "html_url": "https://github.com/cbusillo/codex-skills/issues/1791#issuecomment-100"})
        self.assertNotIn(1791, {r["number"] for r in self.report()["findings"]})

    def test_unnumbered_question_context_does_not_borrow_another_question_id(self):
        self.records[1]["comments"][0]["body"] += "\n\nQ319 is a separate browser-sign-in question."
        self.records[0]["comments"].append({"id": 101, "created_at": "2026-10-15T18:00:00Z",
            "user": {"login": "shiny-code-app[bot]"}, "body": "Director decision: Q319 yes",
            "html_url": "https://github.com/cbusillo/launchplane/issues/3604#issuecomment-101"})
        self.assertIn(3639, {r["number"] for r in self.report()["findings"]})

    def test_valid_new_blocker_and_person_wait_preserve_blocked_view(self):
        record = self.records[3]
        record["blockers"].append({"state": "open"})
        self.assertNotIn(218, {r["number"] for r in self.report()["findings"]})
        record["blockers"].pop()
        record["item"]["body"] = sweep.github_plan_wait.marker(sweep.github_plan_wait.reviewed(
            evidence="Actual Director wait", waiting_for="Director", ends_when="A recorded decision arrives"))
        rows = [r for r in self.report()["findings"] if r["number"] == 218]
        self.assertFalse(any(r["kind"] == "stale_blocked_view" for r in rows))
        self.assertTrue(any(r["kind"] == "unasked_director_wait" for r in rows))
        self.assertTrue(all(r["age_hours"] is None for r in rows))

    def test_refs_and_partial_scope_are_review_only_and_other_branch_is_not_completion(self):
        record = self.records[2]
        row = next(r for r in self.report()["findings"] if r["number"] == 3678)
        self.assertEqual(row["kind"], "merged_link_needs_review")
        record["linked_pulls"][0]["body"] = "Fixes cbusillo/launchplane#3678"
        row = next(r for r in self.report()["findings"] if r["number"] == 3678)
        self.assertEqual(row["kind"], "open_after_completing_merge")
        self.assertTrue(row["review_required"])
        record["linked_pulls"][0]["base"]["ref"] = "release"
        self.assertNotIn(3678, {r["number"] for r in self.report()["findings"]})

    def test_push_and_renewed_wait_invalidate_current_head_ready(self):
        record = self.records[5]
        record["pull"]["head"]["sha"] = "c" * 40
        self.assertNotIn(1632, {r["number"] for r in self.report()["findings"]})
        record["pull"]["head"]["sha"] = "a" * 40
        record["comments"].append({"id": 200, "created_at": "2026-10-15T18:00:00Z", "user": {"login": "shiny-code-app[bot]"}, "body": "Engineering ready; waiting for a defect fix", "html_url": "https://github.com/cbusillo/codex-skills/pull/1632#issuecomment-200"})
        self.assertNotIn(1632, {r["number"] for r in self.report()["findings"]})

    def test_threshold_uses_original_event_and_unknown_age_stays_visible(self):
        self.records[0]["comments"][0]["created_at"] = (self.now - dt.timedelta(hours=1)).isoformat()
        self.assertNotIn(3604, {r["number"] for r in self.report()["findings"]})
        self.records[0]["comments"][0]["created_at"] = "invalid"
        row = next(r for r in self.report()["findings"] if r["number"] == 3604)
        self.assertIsNone(row["age_hours"])
        self.assertEqual(row["age_basis"], "unknown")

    def test_finished_worktrees_are_grouped_per_repository_after_the_threshold(self):
        self.records = []
        old = (self.now - dt.timedelta(days=1)).isoformat()
        new = (self.now - dt.timedelta(hours=1)).isoformat()
        row = {"branch": "work/x", "basis": "PR merged", "pull": None, "dirty": False}
        report = self.report(worktrees=[
            {**row, "repo": "o/a", "task": "landed", "since": old},
            {**row, "repo": "o/a", "task": "edited", "since": old, "dirty": True},
            {**row, "repo": "o/a", "task": "just-started", "since": new},
            {**row, "repo": "o/b", "task": "undated", "since": None}])
        rows = {r["repo"]: r for r in report["findings"] if r["kind"] == "finished_worktree"}
        self.assertEqual({repo: [e["task"] for e in r["evidence"]] for repo, r in rows.items()},
                         {"o/a": ["landed", "edited"], "o/b": ["undated"]})
        dirty = sum(e["dirty"] for e in rows["o/a"]["evidence"])
        self.assertEqual(dirty, 1)
        # The shown counts agree with the evidence; the sentence around them is free.
        self.assertRegex(rows["o/a"]["detail"], rf"\b{dirty}\b")
        self.assertRegex(rows["o/a"]["title"], rf"\b{len(rows['o/a']['evidence'])}\b")
        self.assertIn(rows["o/a"]["title"], sweep.markdown(report))

    def test_coverage_failure_is_in_markdown_even_with_zero_findings(self):
        self.records = []
        report = self.report(gaps=[{"source": "comments", "reason": "unavailable"}])
        self.assertFalse(report["complete"])
        rendered = sweep.markdown(report)
        self.assertIn(report["coverage_gaps"][0]["source"], rendered)
        self.assertIn(report["coverage_gaps"][0]["reason"], rendered)

    def test_policy_label_is_read_from_the_prs_actual_base_target(self):
        record = self.records[5]
        record["pull"]["base"] = {"ref": "integration"}
        record["train_policy"] = {"targets": [{"baseBranch": "integration", "readyLabel": "ship-this"}]}
        # The train admits current-head Ready itself, so the label does not change the finding.
        row = next(r for r in self.report()["findings"] if r["number"] == 1632)
        self.assertEqual(row["kind"], "aged_ready_handoff")
        record["item"]["labels"] = [{"name": "ship-this"}]
        row = next(r for r in self.report()["findings"] if r["number"] == 1632)
        self.assertEqual(row["kind"], "aged_ready_handoff")
        record["pull"]["base"]["ref"] = "unregistered"
        row = next(r for r in self.report()["findings"] if r["number"] == 1632)
        self.assertEqual(row["kind"], "ready_handoff_routing_unknown")

    def test_conflicting_ready_is_flagged_before_the_age_threshold(self):
        record = self.records[5]
        handoff = max(record["comments"], key=lambda c: c["created_at"])
        handoff["created_at"] = handoff["updated_at"] = (self.now - dt.timedelta(minutes=30)).isoformat()
        self.assertNotIn(1632, {r["number"] for r in self.report()["findings"]})
        record["pull"]["mergeable"] = False
        row = next(r for r in self.report()["findings"] if r["number"] == 1632)
        self.assertEqual(row["kind"], "conflicting_ready_handoff")
        record["pull"]["mergeable"] = None
        record["pull"]["mergeable_state"] = "dirty"
        self.assertEqual(next(r for r in self.report()["findings"] if r["number"] == 1632)["kind"],
                         "conflicting_ready_handoff")

    def test_delivery_view_evidence_is_preserved_and_contract_failure_stays_visible(self):
        url = self.records[0]["comments"][0]["html_url"]
        delivery = {"url": url, "delivery": "acknowledged", "registration_url": url + "-registration",
                    "delivery_url": "https://discord.com/channels/1/2/3", "resurface": False}
        result = self.report(delivery_view={"complete": True, "questions": [delivery]})
        row = next(r for r in result["findings"] if r["number"] == 3604)
        self.assertEqual(row["evidence"][0]["delivery_url"], delivery["delivery_url"])
        self.assertFalse(row["evidence"][0]["resurface"])
        self.assertFalse(self.report(delivery_view={"complete": False, "questions": []})["complete"])

    def test_unnumbered_question_repair_reason_reaches_the_sweep(self):
        from skills.supervisor.scripts import question_delivery
        source = self.records[1]["comments"][0]
        url = source["html_url"]
        view = question_delivery.inventory(
            [{**source, "url": url, "author": sweep.login(source)}], [], "cbusillo", self.trusted, now=self.now)
        result = self.report(delivery_view=view)
        gap = next(g for g in result["coverage_gaps"] if g["reason"] == "queue_view_incomplete")
        self.assertEqual(gap.get("unnumbered_sources"), [url])

    def test_umbrella_merged_refs_are_one_candidate_per_issue(self):
        record = self.records[2]
        newer = {**record["linked_pulls"][0], "merged_at": "2026-10-15T20:00:00Z",
                 "html_url": "https://github.com/cbusillo/launchplane/pull/3700"}
        record["linked_pulls"].append(newer)
        rows = [r for r in self.report()["findings"] if r["number"] == 3678]
        self.assertEqual(len(rows), 1)
        self.assertIn(newer["html_url"], rows[0]["evidence"])


class SettledQuestionTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = json.loads(Path(__file__).with_name("fixtures").joinpath("stranded-week-2026-10-15.json").read_text())
        self.records = [fixture["records"][0]]
        self.now = dt.datetime.fromisoformat(fixture["now"])
        self.question = self.records[0]["comments"][0]
        self.question["body"] = "Owner question: Q611. May the rehearsal continue?"
        self.records[0]["comments"] = [self.question]
        self.answer = {"id": 900, "created_at": "2026-10-15T18:00:00Z",
                       "body": "Q611 withdrawn by the Supervisor. Existing authority covers this.",
                       "user": {"login": "shiny-code-app[bot]"},
                       "html_url": "https://github.com/cbusillo/direction/issues/98#issuecomment-900"}

    def question_findings(self, **kwargs: Any) -> list[dict]:
        result = sweep.report(self.records, now=self.now, age_hours=2, trusted={"shiny-code-app[bot]"},
                              owners={"cbusillo"}, **kwargs)
        return [row for row in result["findings"]
                if row["kind"] in {"unanswered_question", "registered_question"}]

    def test_existing_supervisor_settlements_remove_both_digest_question_kinds(self) -> None:
        for body in (
            "Q611 withdrawn by the Supervisor. Existing authority covers this.",
            "Supervisor answer to Q611 (this is the Supervisor's brief wording, not a Director decision). Yes: continue.",
            "Supervisor answer to Q611: this is already decided, so it does not go to Chris.\n\nQ645's Director decision authorizes this.",
        ):
            for open_inventory in (True, False):
                with self.subTest(body=body, open_inventory=open_inventory):
                    self.answer["body"] = body
                    url = self.question["html_url"]
                    delivery = {"complete": True, "questions": [
                        {"url": url, "registration_url": url + "-registration"}]}
                    self.records[0]["comments"] = [self.question, self.answer] if open_inventory else []
                    related = [] if open_inventory else [self.question, self.answer]
                    self.assertEqual(self.question_findings(delivery_view=delivery, related_comments=related), [])

    def test_chatter_untrusted_earlier_and_unrelated_settlements_leave_question_flagged(self) -> None:
        for changes in (
            {"body": "Q611 still needs an answer. Q645 is already decided."},
            {"body": "Supervisor answer to Q6110: continue."},
            {"body": "Q645 withdrawn by the Supervisor. Q611 is a separate question."},
            {"body": "> Q611 withdrawn by the Supervisor."},
            {"body": "Supervisor answer to Q611: continue.", "user": {"login": "untrusted"}},
            {"created_at": "2026-10-01T18:00:00Z"},
            {"created_at": "invalid"},
        ):
            with self.subTest(changes=changes):
                self.records[0]["comments"] = [self.question, {**self.answer, **changes}]
                self.assertEqual([r["url"] for r in self.question_findings()], [self.question["html_url"]])
        self.question["body"] = "Owner question: May we continue? Q611 is context."
        self.records[0]["comments"] = [self.question, self.answer]
        self.assertEqual([r["url"] for r in self.question_findings()], [self.question["html_url"]])


class MergedReviewTests(unittest.TestCase):
    """Q406: only an actual post-merge review earns a temporary suppression."""

    def setUp(self):
        fixture = json.loads(Path(__file__).with_name("fixtures").joinpath("stranded-week-2026-10-15.json").read_text())
        self.records = copy.deepcopy(fixture["records"])
        self.trusted = {"shiny-code-app[bot]", "cbusillo"}
        self.record = self.records[2]
        self.records = [self.record]
        self.pull = self.record["linked_pulls"][0]
        self.merged = dt.datetime.fromisoformat(self.pull["merged_at"])
        self.reviewed = self.merged + dt.timedelta(hours=1)
        self.record["item"]["body"] = "## Current Status\nState: More implementation remains."
        self.record["status_body_updated_at"] = self.reviewed.isoformat()
        self.now = self.reviewed + dt.timedelta(days=1)

    def report(self):
        return sweep.report(self.records, now=self.now, age_hours=2, trusted=self.trusted,
                            owners={"cbusillo"})

    def kinds(self):
        return [r["kind"] for r in self.report()["findings"]]

    def comment(self, body, when, author="shiny-code-app[bot]"):
        self.record["comments"].append({"id": len(self.record["comments"]) + 1,
            "body": body, "created_at": when.isoformat(), "user": {"login": author},
            "html_url": "https://github.com/cbusillo/launchplane/issues/3678#issuecomment-1"})

    def test_reviewed_after_merge_suppresses_then_reflags_at_exact_fourteen_day_boundary(self):
        self.assertEqual(self.kinds(), [])
        self.now = self.reviewed + dt.timedelta(days=14) - dt.timedelta(microseconds=1)
        self.assertEqual(self.kinds(), [])
        self.now += dt.timedelta(microseconds=1)
        self.assertEqual(self.kinds(), ["merged_link_needs_review"])

    def test_stale_equal_missing_or_unknown_status_flags_despite_new_issue_activity(self):
        self.now = self.merged + dt.timedelta(minutes=1)
        self.record["item"]["updated_at"] = self.now.isoformat()
        self.comment("Routine chatter", self.now)
        for when in (self.merged - dt.timedelta(days=1), self.merged, None):
            with self.subTest(when=when):
                self.record["status_body_updated_at"] = when.isoformat() if when else None
                self.assertEqual(self.kinds(), ["merged_link_needs_review"])
        self.record["item"]["body"] = "No status section"
        self.record["status_body_updated_at"] = self.now.isoformat()
        self.assertEqual(self.kinds(), ["merged_link_needs_review"])

    def test_completing_merge_flags_immediately_despite_post_merge_review(self):
        for scope in ("Closes #3678", "Fixes cbusillo/launchplane#3678",
                      "Resolves https://github.com/cbusillo/launchplane/issues/3678"):
            with self.subTest(scope=scope):
                self.pull["body"] = scope
                self.now = self.merged + dt.timedelta(seconds=1)
                self.assertEqual(self.kinds(), ["open_after_completing_merge"])
                self.assertTrue(self.report()["findings"][0]["review_required"])

    def test_newer_trusted_activity_renews_only_an_existing_post_merge_review(self):
        activity = self.reviewed + dt.timedelta(days=10)
        self.comment("Implementation continues", activity)
        self.now = self.reviewed + dt.timedelta(days=14)
        self.assertEqual(self.kinds(), [])
        self.now = activity + dt.timedelta(days=14)
        self.assertEqual(self.kinds(), ["merged_link_needs_review"])
        self.record["status_body_updated_at"] = self.merged.isoformat()
        self.now = activity
        self.assertEqual(self.kinds(), ["merged_link_needs_review"])

    def test_label_activity_and_untrusted_status_do_not_renew_review(self):
        self.now = self.reviewed + dt.timedelta(days=14)
        self.record["item"]["updated_at"] = self.now.isoformat()
        self.comment("## Current Status\nState: More work", self.now, author="untrusted")
        self.assertEqual(self.kinds(), ["merged_link_needs_review"])

    def test_trusted_comment_status_and_structured_review_suppress_without_body_status(self):
        self.record["item"]["body"] = "Original request"
        for body in ("## Current Status\nState: More work", sweep.github_plan_wait.marker(
                sweep.github_plan_wait.reviewed(evidence="Reviewed after merge; work remains"))):
            with self.subTest(body=body):
                self.record["comments"] = []
                self.comment(body, self.reviewed)
                self.assertEqual(self.kinds(), [])
                self.now = self.reviewed + dt.timedelta(days=14)
                self.assertEqual(self.kinds(), ["merged_link_needs_review"])
                self.now = self.reviewed + dt.timedelta(days=1)

    def test_edit_of_status_comment_uses_edit_time(self):
        self.record["item"]["body"] = "Request"
        self.comment("## Current Status\nState: More work", self.merged - dt.timedelta(days=1))
        self.record["comments"][0]["updated_at"] = self.reviewed.isoformat()
        self.assertEqual(self.kinds(), [])

    def test_new_merge_after_review_reflags_and_completing_merge_wins_aggregation(self):
        later = {**self.pull, "merged_at": (self.reviewed + dt.timedelta(hours=1)).isoformat()}
        self.record["linked_pulls"].append(later)
        self.assertEqual(self.kinds(), ["merged_link_needs_review"])
        self.pull["body"] = "Closes #3678"
        self.assertEqual(self.kinds(), ["open_after_completing_merge"])
        self.record["item"]["state"] = "closed"
        self.assertEqual(self.kinds(), [])


REGISTER = Path(__file__).with_name("fixtures").joinpath("operational-register-2026-10-14.txt").read_text()


class OverdueTriggerTests(unittest.TestCase):
    """Dated triggers that passed without anyone raising them (codex-skills#716, #790)."""

    def setUp(self):
        # 11:30 PM ET on October 14, when the register's misses were found by hand.
        self.now = dt.datetime(2026, 10, 15, 3, 30, tzinfo=dt.timezone.utc)
        self.register = {"repo": "example/ops", "path": "docs/register.md", "default_branch": "main", "text": REGISTER}

    def report(self, records=(), **kwargs):
        return sweep.report(list(records), now=self.now, age_hours=2, trusted={"shiny-code-app[bot]", "example"},
                            owners={"example"}, registers=[self.register], **kwargs)

    def flagged(self, report=None):
        report = report or self.report()
        return {(row["kind"], row["url"].rpartition("#")[2]) for row in report["findings"]}

    def titles(self, *names):
        entries = {e["title"]: e for e in sweep.register_entries(REGISTER)}
        return [sweep.anchor(entries[name]["title"]) for name in names]

    def test_overdue_check_and_owner_decision_are_reported_and_future_or_event_checks_are_not(self):
        camera, host = self.titles("Camera bridge rollback artifacts", "Host name resolution")
        report = self.report()
        self.assertEqual(self.flagged(report), {("overdue_operational_check", camera),
                                                ("operational_check_waits_on_owner", host)})
        self.assertTrue(report["complete"])
        self.assertTrue(all(row["url"].startswith("https://github.com/example/ops/blob/main/docs/register.md#")
                            for row in report["findings"]))
        self.assertIn("2026-10-13", sweep.markdown(report))

    def test_resolved_entry_removed_from_register_is_no_longer_reported(self):
        camera, host = self.titles("Camera bridge rollback artifacts", "Host name resolution")
        self.register["text"] = REGISTER.split("### Camera bridge rollback artifacts")[0]
        self.assertEqual(self.flagged(), {("operational_check_waits_on_owner", host)})

    def test_dated_check_is_reported_once_its_date_passes(self):
        garage, roaming = self.titles("Garage close and arrival observation", "Access point roaming fault")
        self.now = dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc)
        kinds = self.flagged()
        self.assertIn(("overdue_operational_check", garage), kinds)
        self.assertIn(("overdue_operational_check", roaming), kinds)

    def test_register_wording_variants_still_report(self):
        camera, host = self.titles("Camera bridge rollback artifacts", "Host name resolution")
        self.register["text"] = (REGISTER.replace("**Next check**:", "**Next check:**")
                                 .replace("the overdue 2026-10-13 observation", "October 13, 2026")
                                 .replace("- **", "* **")
                                 .replace("the owner decides on", "waiting for the Director to decide on"))
        self.assertEqual(self.flagged(), {("overdue_operational_check", camera),
                                          ("operational_check_waits_on_owner", host)})

    def test_entry_without_next_check_is_a_coverage_gap(self):
        self.register["text"] = "### Unscheduled\n\n- **Current action**: look at it.\n"
        report = self.report()
        self.assertFalse(report["complete"])
        self.assertEqual(report["findings"], [])

    def issue(self, number, status=None, *, structured=None, comments=()):
        body = f"## Current Status\n\n{status}\n" if status else ""
        if structured:
            body += sweep.github_plan_wait.marker(structured)
        return {"repo": "example/catalog", "comments": list(comments), "item": {
            "number": number, "state": "open", "user": {"login": "example"}, "body": body,
            "html_url": f"https://github.com/example/catalog/issues/{number}"}}

    def test_passed_parked_until_and_waiting_for_dates_are_reported(self):
        records = [
            self.issue(1, "State: parked.\nParked until: October 13, 2026 (3 days of validated running)."),
            self.issue(2, "Waiting for: the measurement after 2026-10-14T22:24:00Z."),
            self.issue(3, "Parked until: 2026-10-30, when the window opens."),
            self.issue(4, "Waiting for: None for the local checks (2026-10-12 scope comment)."),
            self.issue(5, "Waiting for: the Client's DNS change; since 2026-10-01"),
            self.issue(6, "Parked until: `next` skips a due follow-up a second time."),
        ]
        rows = [r for r in self.report(records)["findings"] if r["kind"] == "passed_dated_wait"]
        self.assertEqual({r["number"] for r in rows}, {1, 2})
        self.assertTrue(all(r["age_hours"] >= 2 and r["next_actor"] for r in rows))

    def test_structured_wait_record_supersedes_prose(self):
        reviewed = sweep.github_plan_wait.reviewed
        records = [
            self.issue(1, "Parked until: 2026-10-13.", structured=reviewed(evidence="Released by the Director")),
            self.issue(2, None, structured=reviewed(evidence="Parked", waiting_for="A quiet week of backups",
                                                    ends_when="2026-10-14, compare consecutive backups")),
            self.issue(3, None, structured=reviewed(evidence="Parked", waiting_for="A quiet week of backups",
                                                    ends_when="2026-10-20, compare consecutive backups")),
        ]
        rows = [r for r in self.report(records)["findings"] if r["kind"] == "passed_dated_wait"]
        self.assertEqual({r["number"] for r in rows}, {2})

    def test_next_action_routed_to_the_supervisor_is_listed_oldest_first_per_repository(self):
        """codex-skills#2024: 199 issues handed their next step back to the Supervisor unseen."""
        records = [
            self.issue(1, "Next action: Supervisor routes PR #9 through the merge train."),
            self.issue(2, "- **Next action:** The Supervisor brings Q426 to Chris."),
            self.issue(3, "Next action: Next agent refreshes PR #9 against main."),
            self.issue(4, "State: active.\nWaiting for: the Supervisor's ledger week."),
        ]
        other = self.issue(5, "Next action: When claim 61 goes quiet, the Supervisor releases it.")
        other["repo"] = "example/other"
        records.append(other)
        for record, edited in zip(records, ["2026-10-13T00:00:00Z", "2026-10-11T00:00:00Z", "2026-10-11T00:00:00Z",
                                            "2026-10-11T00:00:00Z", "2026-10-12T00:00:00Z"]):
            record["status_body_updated_at"] = edited
        result = self.report(records)
        rows = [r for r in result["findings"] if r["kind"] == "supervisor_next_action"]
        self.assertEqual([(r["repo"], r["number"]) for r in rows],
                         [("example/catalog", 2), ("example/other", 5), ("example/catalog", 1)])
        self.assertEqual(rows[0]["evidence"], ["The Supervisor brings Q426 to Chris."])
        self.assertEqual(result["supervisor_next_action_by_repo"], {"example/catalog": 2, "example/other": 1})
        text = sweep.markdown(result)
        # The digest still parses the report with the new kind and count line.
        self.assertEqual(sum(r["kind"] == "supervisor_next_action" for r in digest_summary.parse(text)[2]), 3)
        # A fresh hand-back stays quiet until the age threshold; an unknown edit time is still listed.
        records[0]["status_body_updated_at"] = (self.now - dt.timedelta(hours=1)).isoformat()
        records[1].pop("status_body_updated_at")
        rows = [r for r in self.report(records)["findings"] if r["kind"] == "supervisor_next_action"]
        self.assertEqual([(r["number"], r["age_basis"]) for r in rows], [(2, "unknown"), (5, "recorded_event")])

    def test_waits_on_the_director_without_an_open_question_are_listed_oldest_first(self):
        # https://github.com/cbusillo/codex-skills/issues/2579
        reviewed = sweep.github_plan_wait.reviewed
        def comment(number, cid, days_ago, body):
            return {"id": cid, "created_at": (self.now - dt.timedelta(days=days_ago)).isoformat(),
                    "user": {"login": "shiny-code-app[bot]"}, "body": body,
                    "html_url": f"https://github.com/example/catalog/issues/{number}#issuecomment-{cid}"}
        def waiting(number, cid, days_ago, actor, evidence="Refused install; by-hand steps in an earlier comment"):
            return comment(number, cid, days_ago, sweep.github_plan_wait.marker(
                reviewed(evidence=evidence, waiting_for=actor, ends_when="The helper is installed")))
        question = comment(5, 50, 6, "Director question: Q900. Run `make install` here? After it runs, worktrees retire.")
        records = [
            self.issue(1, comments=[waiting(1, 10, 5, "Chris or the Supervisor")]),
            self.issue(2, comments=[waiting(2, 20, 3, "Director")]),
            self.issue(3, comments=[comment(3, 31, 4, "Director question: Q901. Install it?"),
                                    waiting(3, 30, 4, "Director")]),
            self.issue(4, comments=[waiting(4, 40, 4, "Director", evidence=f"Asked in {question['html_url']}")]),
            self.issue(5, comments=[question]),
            self.issue(6, comments=[waiting(6, 60, 4, "The Client's DNS change")]),
            self.issue(7, comments=[comment(7, 71, 2, "Director question: Q902. Install it?"),
                                    comment(7, 72, 1.9, "Director decision: Q902 yes, installed by hand."),
                                    waiting(7, 70, 2, "Director")]),
            self.issue(8, comments=[waiting(8, 80, 4, "Supervisor")]),
            self.issue(9, comments=[waiting(9, 90, 4.5, "Director", evidence="Asked in https://github.com/example/catalog/issues/10#issuecomment-91")]),
            self.issue(10, comments=[comment(10, 91, 4.6, "Director question: Q903. Install it?")]),
            self.issue(11, comments=[waiting(11, 110, 3.5, "Director", evidence=f"Asked in {question['html_url']}0")]),
            self.issue(12, comments=[waiting(12, 120, 4, "The upstream repository owner releases a fix")]),
            self.issue(13, comments=[waiting(13, 130, 4, "example/catalog#5 lands")]),
            self.issue(14, comments=[waiting(14, 140, 1, "Director approval to resume deployment")]),
            self.issue(15, comments=[comment(15, 151, 1.5, "Director decision: park this until the trip ends."),
                                     waiting(15, 150, 1, "Chris to resume after the trip")]),
            self.issue(16, comments=[comment(16, 161, 1.5, 'Director decision: "try it for a few days before the App Store"'),
                                     waiting(16, 160, 1, "Chris to dogfood 1.0.76", evidence=(
                                         "https://github.com/example/catalog/issues/16#issuecomment-161"))]),
        ]
        records[9]["item"]["state"] = "closed"
        result = self.report(records, director_names=("Chris",))
        rows = [r for r in result["findings"] if r["kind"] == "unasked_director_wait"]
        self.assertEqual([r["number"] for r in rows], [1, 9, 11, 2, 7, 14])
        self.assertTrue(all(r["url"].endswith(f"#issuecomment-{r['number']}0") for r in rows))
        others = {(r["kind"], r["number"]) for r in result["findings"] if r["kind"] != "unasked_director_wait"}
        self.assertIn(("aged_structured_wait", 8), others)
        self.assertFalse(any(number in {1, 2, 3, 4, 6, 7} for kind, number in others if kind == "aged_structured_wait"))
        parsed = [r for r in digest_summary.parse(sweep.markdown(result))[2] if r["kind"] == "unasked_director_wait"]
        self.assertEqual([r["person"] for r in parsed], [True] * 6)

    def test_next_action_counts_only_the_supervisor_as_an_actor(self):
        """codex-skills#2096: a path or description naming the Supervisor is not a handoff to it."""
        routed = [
            "Supervisor; existing fleet owner records the event result.",
            "The Supervisor (who holds the enqueue log) posts the receipt here.",
            "At the recorded event the Supervisor posts both counts.",
            "CP806-C1 follows CI, waits for the Supervisor's OpenAI review on #875, then runs ready.",
            "After CI, handoff to the Supervisor for landing.",
        ]
        quiet = [
            "Next agent fixes skills/supervisor/scripts/digest_summary.py.",
            "Agent selects the next fixture cluster; the five duplicated supervisor terminal graphs are resolved.",
            "An agent replaces Supervisor accepted-first hand ordering with automatic batching.",
            "Next agent fixes the Supervisor scripts and runs their tests.",
            "The merge train lands PR #3854; nothing further for the Supervisor to route.",
        ]
        for text in routed:
            self.assertEqual(sweep.supervisor_next_action(f"## Current Status\nNext action: {text}"), text)
        for text in quiet:
            self.assertIsNone(sweep.supervisor_next_action(f"## Current Status\nNext action: {text}"), text)

    def test_status_fields_stay_on_their_own_line(self):
        """codex-skills#2096: an empty field never reads the next field as its value."""
        body = ("## Current Status\nState: Active.\nNext action:\nBlocked by: Supervisor decision\n"
                "Last verified:\n\n<!-- github-plan:wait {\"evidence\": \"Filed Oct 17, 2026\"} -->")
        self.assertIsNone(sweep.supervisor_next_action(body))
        self.assertIsNone(sweep.verified_at(body))


class TrackTests(unittest.TestCase):
    now = dt.datetime(2026, 10, 16, 23, 0, tzinfo=dt.timezone.utc)

    def track(self, verified, children):
        status = f"## Current Status\n\nState: live\nLast verified: {verified}\n" if verified else "## Current Status\n\nState: live\n"
        return {"repo": "cbusillo/direction", "comments": [], "track_children": children,
                "item": {"number": 3, "state": "open", "title": "Track: CM website live and used", "body": status,
                         "html_url": "https://github.com/cbusillo/direction/issues/3"}}

    def child(self, *, closed_at=None, comments=()):
        return {"item": {"number": 91, "state": "closed" if closed_at else "open", "closed_at": closed_at,
                         "html_url": "https://github.com/cbusillo/odoo-tenant-cm-website/issues/91"},
                "comments": [{"id": n, "user": {"login": who}, "body": body, "created_at": when,
                              "html_url": f"https://github.com/cbusillo/odoo-tenant-cm-website/issues/91#issuecomment-{n}"}
                             for n, (who, body, when) in enumerate(comments)]}

    def flagged(self, record):
        return [r for r in sweep.report([record], now=self.now, age_hours=2, trusted={"shiny-code-app[bot]"},
                                        owners={"cbusillo"})["findings"] if r["kind"] == "stale_track_status"]

    def test_director_input_on_a_child_after_last_verified_flags_the_track(self):
        # The October 16 CM case: verified 2:20 PM ET, Director input on #91 at 3:50 PM ET.
        later = self.child(comments=[("cbusillo", "G-DLBT1G42QR", "2026-10-16T19:50:00Z")])
        rows = self.flagged(self.track("October 16, 2026, approximately 2:16–2:20 PM ET, CMLIVE-D1", [later]))
        self.assertEqual([r["evidence"] for r in rows], [[later["comments"][0]["html_url"]]])
        self.assertFalse(self.flagged(self.track("October 16, 2026, 4:05 PM ET", [later])))

    def test_child_closure_and_recorded_decisions_count_but_bot_chatter_does_not(self):
        verified = "2026-10-16T15:00:00Z"
        self.assertTrue(self.flagged(self.track(verified, [self.child(closed_at="2026-10-16T16:00:00Z")])))
        decision = self.child(comments=[("shiny-code-app[bot]", "Director decision (Q1): yes", "2026-10-16T16:00:00Z")])
        self.assertTrue(self.flagged(self.track(verified, [decision])))
        chatter = self.child(comments=[("shiny-code-app[bot]", "Claimed by worker", "2026-10-16T16:00:00Z"),
                                       ("someone", "Director decision: no", "2026-10-16T16:00:00Z")])
        self.assertFalse(self.flagged(self.track(verified, [chatter])))

    def test_recent_changes_and_same_day_bare_dates_stay_quiet(self):
        recent = self.child(closed_at=(self.now - dt.timedelta(hours=1)).isoformat())
        self.assertFalse(self.flagged(self.track("2026-10-16T15:00:00Z", [recent])))
        same_day = self.child(closed_at="2026-10-16T16:00:00Z")
        self.assertFalse(self.flagged(self.track("October 16, 2026", [same_day])))
        self.assertTrue(self.flagged(self.track("October 15, 2026", [same_day])))
        self.assertTrue(self.flagged(self.track(None, [same_day])))

    def test_iso_offsets_and_fractions_are_exact(self):
        self.assertEqual(sweep.verified_at("Last verified: 2026-10-16T20:00:00.123+02:00"),
                         dt.datetime(2026, 10, 16, 18, 0, 0, 123000, tzinfo=dt.timezone.utc))
        child = self.child(closed_at="2026-10-16T19:00:00Z")
        self.assertTrue(self.flagged(self.track("2026-10-16T20:00:00+02:00", [child])))


class FakeReader:
    def __init__(self):
        self.paths = []
        self.remaining = 1000
        self.limit = None
        self.pages = {}
        self.repositories = {"repos/test/repo": {"archived": False, "default_branch": "main"}}
        self.files = {}

    def graphql_json(self, query, variables, *, step) -> SimpleNamespace:
        raise AssertionError("unconfigured GraphQL read")

    def get_json(self, path, *, step):
        self.paths.append(path)
        if path == "rate_limit":
            return {"resources": {"core": {"remaining": self.remaining, "limit": self.limit}}}
        if path in self.repositories:
            return self.repositories[path]
        if "/contents/" in path:
            if path not in self.files:
                raise sweep.github_read.GitHubReadError("Resource not found", result=SimpleNamespace(
                    failure=SimpleNamespace(cause="not_found")), diagnostics={})
            return {"type": "file", "content": base64.b64encode(self.files[path].encode()).decode()}
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        key = (parsed.path, int(query.get("page", [1])[0]), query.get("state", [None])[0])
        if key in self.pages:
            return copy.deepcopy(self.pages[key])
        if (parsed.path.startswith("repos/") and
                (parsed.path.endswith(("/issues", "/comments", "/timeline", "/blocked_by")))):
            return []
        if path in {"repos/target/repo/issues/2", "repos/test/missing"}:
            raise unavailable("not_found")
        raise AssertionError(f"unconfigured fake read: {path}")


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.reader = FakeReader()
        self.now = dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc)

    def collect(self, **kwargs):
        return sweep.collect(["test/repo"], reader=self.reader, now=self.now, **kwargs)

    def test_track_children_are_read_once_and_only_changed_ones_outside_inventory(self):
        self.reader.repositories["repos/cbusillo/direction"] = {"archived": False, "default_branch": "main"}
        body = "## Current Status\n\nLast verified: 2026-10-15T14:00:00+02:00\n"
        self.reader.pages[("repos/cbusillo/direction/issues", 1, "open")] = [
            {"number": 3, "state": "open", "title": "Track: CM website live and used", "body": body, "comments": 0},
            {"number": 4, "state": "open", "title": "Not a track", "body": body, "comments": 0}]
        child = {"repository_url": "https://api.github.com/repos/cbusillo/odoo-tenant-cm-website", "state": "open",
                 "html_url": "https://github.com/cbusillo/odoo-tenant-cm-website/issues/91"}
        self.reader.pages[("repos/cbusillo/direction/issues/3/sub_issues", 1, None)] = [
            {**child, "number": 91, "comments": 2, "updated_at": "2026-10-15T18:00:00Z"},
            {**child, "number": 92, "comments": 2, "updated_at": "2026-10-14T18:00:00Z"}]
        comments = "repos/cbusillo/odoo-tenant-cm-website/issues/91/comments"
        self.reader.pages[(comments, 1, None)] = [
            {"id": 1, "user": {"login": "cbusillo"}, "body": "Go live now.", "created_at": "2026-10-15T18:00:00Z",
             "html_url": "https://github.com/cbusillo/odoo-tenant-cm-website/issues/91#issuecomment-1"}]
        report = sweep.collect(["cbusillo/direction"], reader=self.reader, now=self.now)
        rows = [r for r in report["findings"] if r["kind"] == "stale_track_status"]
        self.assertEqual([(r["number"], r["evidence"]) for r in rows],
                         [(3, ["https://github.com/cbusillo/odoo-tenant-cm-website/issues/91#issuecomment-1"])])
        reads = [urlsplit(p) for p in self.reader.paths]
        self.assertEqual([p.path for p in reads if p.path.endswith("/sub_issues")], ["repos/cbusillo/direction/issues/3/sub_issues"])
        self.assertEqual([parse_qs(p.query).get("since") for p in reads if "cm-website" in p.path],
                         [["2026-10-15T12:00:00Z"]])

    def test_finished_worktree_pr_reads_share_the_sweep_budget(self):
        self.reader.pages[("repos/test/repo/pulls", 1, "all")] = [
            {"state": "closed", "merged_at": "2026-10-15T00:00:00Z", "closed_at": "2026-10-15T00:00:00Z"}]
        seen = []

        def finished(root, pulls_for, include):
            # A narrow sweep scans only its own repositories.
            self.assertEqual((include("test/repo"), include("other/repo")), (True, False))
            seen.append(pulls_for("test/repo", "work/x")[0]["state"])
            try:
                pulls_for("test/repo", "work/y")
            except LookupError as exc:
                return [{"repo": "test/repo", "task": "x", "branch": "work/x", "basis": "PR merged", "dirty": False,
                         "pull": None, "since": "2026-10-15T00:00:00Z"}], [{"source": "work/y", "reason": str(exc)}]
            raise AssertionError("second PR read should exceed the budget")

        with patch.object(sweep.audit, "fetch_paginated", return_value=([], False)):
            baseline = self.collect()["requests"]
            self.reader.paths.clear()
            # One request beyond the inventory's own reads: the first PR read fits, the second does not.
            with patch.object(sweep.landed_worktrees, "finished", finished):
                report = self.collect(worktree_root=Path("/unused"), max_requests=baseline + 1)
        self.assertEqual(seen, ["closed"])
        self.assertIn("head=test:work%2Fx", self.reader.paths[-1])
        self.assertEqual([r["kind"] for r in report["findings"]], ["finished_worktree"])
        self.assertIn({"source": "work/y", "reason": "request_budget_exhausted"}, report["coverage_gaps"])

    def test_full_pr_page_for_a_worktree_branch_is_a_coverage_gap(self):
        self.reader.pages[("repos/test/repo/pulls", 1, "all")] = [{"state": "closed"}] * 100

        def finished(root, pulls_for, include):
            with self.assertRaisesRegex(LookupError, "page_limit"):
                pulls_for("test/repo", "work/x")
            return [], [{"source": "work/x", "reason": "page_limit"}]

        with patch.object(sweep.landed_worktrees, "finished", finished), \
                patch.object(sweep.audit, "fetch_paginated", return_value=([], False)):
            self.assertFalse(self.collect(worktree_root=Path("/unused"))["complete"])

    def test_second_comment_page_is_read_and_closed_history_inventoried(self):
        self.reader.pages[("repos/test/repo/issues", 1, "open")] = [
            {"number": 1, "state": "open", "user": {"login": "test"}}]
        path = "repos/test/repo/issues/1/comments"
        self.reader.pages[(path, 1, None)] = [
            {"id": n, "body": "", "created_at": "2026-10-14T00:00:00Z", "user": {"login": "test"},
             "html_url": f"https://github.com/test/repo/issues/1#issuecomment-{n}"} for n in range(100)]
        self.reader.pages[(path, 2, None)] = [
            {"id": 101, "body": "Owner question: Q100 Should this continue?", "created_at": "2026-10-14T01:00:00Z", "user": {"login": "test"}, "html_url": "https://github.com/test/repo/issues/1#issuecomment-101"}]
        report = self.collect()
        self.assertFalse([gap for gap in report["coverage_gaps"] if gap["source"] != "question_delivery"])
        self.assertEqual(len(report["findings"]), 1)
        self.assertTrue(any(urlsplit(p).path == path and parse_qs(urlsplit(p).query).get("page") == ["2"] for p in self.reader.paths))
        self.assertTrue(any(parse_qs(urlsplit(p).query).get("state") == ["closed"]
                            and "since" in parse_qs(urlsplit(p).query) for p in self.reader.paths))

    def test_quota_reserve_and_request_limit_expose_unread_repositories(self):
        self.reader.remaining = 0
        report = self.collect()
        self.assertFalse(report["complete"])
        self.assertTrue(any(s["repo"] == "test/repo" for g in report["coverage_gaps"] for s in g.get("scopes", [])))
        self.assertEqual(self.reader.paths, ["rate_limit"])
        self.reader.remaining = 1000
        self.reader.paths.clear()
        report = self.collect(max_requests=2)
        self.assertFalse(report["complete"])
        self.assertLessEqual(len(self.reader.paths), 2)

    def test_page_cap_and_malformed_page_cannot_report_complete(self):
        path = ("repos/test/repo/issues", 1, "open")
        self.reader.pages[path] = [{"number": n, "state": "open"} for n in range(1, 101)]
        report = self.collect(max_pages=1)
        self.assertFalse(report["complete"])
        self.assertTrue(any(g["reason"] == "page_limit" for g in report["coverage_gaps"]))
        self.reader.pages[path] = {"unexpected": "object"}
        result = self.collect()
        self.assertTrue(any(g["source"].startswith(path[0]) for g in result["coverage_gaps"]))

    def test_audit_mode_reuses_collector_and_does_not_stamp_direction(self):
        with patch.object(sweep, "collect", return_value={"complete": True}) as collect, \
                patch.object(audit.github_read, "GitHubReader"), \
                patch.object(audit, "record_audit") as mark, patch("builtins.print"):
            self.assertEqual(audit.main(["--stranded-work-only", "--repo", "test/repo"]), 0)
        collect.assert_called_once()
        mark.assert_not_called()

    def test_owner_discovery_uses_existing_inventory_and_rotates_daily(self):
        with patch.object(audit, "owner_repositories", return_value=({"test/repo": {}}, False)) as discover:
            result = sweep.collect([], reader=self.reader, owner="test", now=self.now)
        discover.assert_called_once()
        self.assertEqual(result["repositories"], ["test/repo"])
        self.assertFalse(any(g["source"] == "test" for g in result["coverage_gaps"]))
        for day in (0, 1):
            reader = FakeReader()
            with patch.object(audit, "owner_repositories", return_value=({"test/other": {}, "test/repo": {}}, False)):
                # A one-read budget exposes the first repository without live metadata.
                result = sweep.collect([], reader=reader, owner="test", now=self.now + dt.timedelta(days=day), max_requests=1)
            sources = [s["repo"] for g in result["coverage_gaps"] for s in g.get("scopes", [])]
            if day == 0:
                first = sources[0]
            else:
                self.assertNotEqual(sources[0], first)

    def test_budget_aggregates_unread_records_instead_of_flooding_digest(self):
        self.reader.pages[("repos/test/repo/issues", 1, "open")] = [
            {"number": n, "state": "open"} for n in range(1, 60)]
        result = self.collect(max_requests=6)
        unread = [g for g in result["coverage_gaps"] if g.get("count")]
        self.assertEqual(len(unread), 1)
        self.assertEqual(unread[0]["count"] + result["attempted_records"],
                         len(self.reader.pages[("repos/test/repo/issues", 1, "open")]))
        self.assertLess(len(result["coverage_gaps"]), 5)

    def test_cross_repo_merged_pr_is_checked_against_its_own_default_branch(self):
        original = self.reader.get_json
        def read(path, *, step):
            if path == "repos/source/repo":
                return {"archived": False, "default_branch": "integration"}
            if path == "repos/source/repo/pulls/2":
                return {"body": "Fixes test/repo#1", "merged_at": "2026-10-14T01:00:00Z",
                        "base": {"ref": "integration"}, "html_url": "https://github.com/source/repo/pull/2"}
            return original(path, step=step)
        self.reader.get_json = read
        self.reader.pages[("repos/test/repo/issues", 1, "open")] = [{"number": 1, "state": "open"}]
        self.reader.pages[("repos/test/repo/issues/1/timeline", 1, None)] = [{"event": "cross-referenced", "source": {
            "issue": {"number": 2, "pull_request": {"url": "https://api.github.com/repos/source/repo/pulls/2"}, "repository_url": "https://api.github.com/repos/source/repo"}}}]
        result = self.collect()
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["kind"], "open_after_completing_merge")

    def test_supervisor_routed_status_is_aged_from_its_body_edit(self):
        self.reader.pages[("repos/test/repo/issues", 1, "open")] = [{
            "number": 1, "state": "open", "body": "## Current Status\nNext action: Supervisor routes PR #2.",
            "user": {"login": "test"}, "updated_at": self.now.isoformat()}]
        self.reader.graphql_json = lambda *args, **kwargs: SimpleNamespace(ok=True, body={"data": {"repository": {
            "issue": {"lastEditedAt": "2026-10-14T00:00:00Z", "createdAt": "2026-10-01T00:00:00Z"}}}})
        rows = self.collect()["findings"]
        self.assertEqual([(r["kind"], r["age_hours"]) for r in rows], [("supervisor_next_action", 48.0)])

    def test_body_status_uses_bounded_edit_read_not_issue_activity(self):
        original = self.reader.get_json
        self.reader.pages[("repos/test/repo/issues", 1, "open")] = [{
            "number": 1, "state": "open", "body": "## Current Status\nState: More work",
            "user": {"login": "test"}, "updated_at": self.now.isoformat()}]
        self.reader.pages[("repos/test/repo/issues/1/timeline", 1, None)] = [{
            "event": "cross-referenced", "source": {"issue": {"number": 2,
                "pull_request": {"url": "https://api.github.com/repos/test/repo/pulls/2"},
                "repository_url": "https://api.github.com/repos/test/repo"}}}]
        def read(path, *, step):
            if path == "repos/test/repo/pulls/2":
                self.reader.paths.append(path)
                return {"body": "Refs #1", "merged_at": "2026-10-14T01:00:00Z", "base": {"ref": "main"}}
            return original(path, step=step)
        self.reader.get_json = read
        reads_before_status = []
        def edit(value, **kwargs):
            reads_before_status.append(len(self.reader.paths))
            return SimpleNamespace(ok=True, body={"data": {"repository": {"issue": {
                "lastEditedAt": value, "createdAt": "2026-10-01T00:00:00Z"}}}})
        self.reader.graphql_json = lambda *args, **kwargs: edit("2026-10-15T01:00:00Z")
        self.assertEqual(self.collect()["findings"], [])
        self.reader.graphql_json = lambda *args, **kwargs: edit(None)
        self.assertEqual(self.collect()["findings"][0]["kind"], "merged_link_needs_review")
        self.reader.graphql_json = lambda *args, **kwargs: SimpleNamespace(ok=False)
        result = self.collect()
        self.assertEqual(result["findings"][0]["kind"], "merged_link_needs_review")
        self.assertTrue(any(g["source"] == "test/repo#1/status" for g in result["coverage_gaps"]))
        # Exhaust the observed pre-status budget without pinning the collector's read count.
        with patch.object(self.reader, "graphql_json", side_effect=AssertionError("unbudgeted read")):
            limited = self.collect(max_requests=reads_before_status[0])
        self.assertTrue(any(g["source"] == "test/repo#1/status" and g["reason"] == "request_budget_exhausted"
                            for g in limited["coverage_gaps"]))

    def test_post_merge_status_edit_by_the_primary_automation_login_earns_the_quiet_period(self):
        self.reader.pages[("repos/test/repo/issues", 1, "open")] = [{
            "number": 1, "state": "open", "body": "## Current Status\nState: More work",
            "user": {"login": "fixture-app[bot]"}, "updated_at": self.now.isoformat()}]
        self.reader.pages[("repos/test/repo/issues/1/timeline", 1, None)] = [{
            "event": "cross-referenced", "source": {"issue": {"number": 2,
                "pull_request": {"url": "https://api.github.com/repos/test/repo/pulls/2"},
                "repository_url": "https://api.github.com/repos/test/repo"}}}]
        original = self.reader.get_json
        self.reader.get_json = lambda path, *, step: (
            {"body": "Refs #1", "merged_at": "2026-10-14T01:00:00Z", "base": {"ref": "main"}}
            if path == "repos/test/repo/pulls/2" else original(path, step=step))
        self.reader.graphql_json = lambda *args, **kwargs: SimpleNamespace(ok=True, body={"data": {"repository": {
            "issue": {"lastEditedAt": "2026-10-15T01:00:00Z", "createdAt": "2026-10-01T00:00:00Z"}}}})
        for primary, kinds in (("fixture-app[bot]", []), (None, ["merged_link_needs_review"])):
            with self.subTest(primary=primary), \
                    patch.object(sweep.github_identity, "configured_bot_logins", return_value=()), \
                    patch.object(sweep.github_identity, "automation_login", return_value=primary):
                self.assertEqual([r["kind"] for r in self.collect()["findings"]], kinds)

    def test_expired_policy_read_does_not_launch_a_helper(self):
        with patch.object(sweep.subprocess, "run") as run:
            self.assertIsNone(sweep.train_policy("test/repo", deadline_at=0))
        run.assert_not_called()

    def test_queue_adapter_normalizes_rest_comments_without_host_state(self):
        fixture = json.loads(Path(__file__).with_name("fixtures").joinpath("stranded-week-2026-10-15.json").read_text())
        question = fixture["records"][0]["comments"][0]
        def inventory(source, queue, owner, authors, now=None, *, question_urls=None):
            self.assertEqual(queue[0]["url"], question["html_url"])
            self.assertEqual(queue[0]["author"], question["user"]["login"])
            return {"complete": True, "questions": []}
        self.reader.pages[("repos/test/repo/issues/123/comments", 1, None)] = [question]
        with patch.dict(sys.modules, {"skills.supervisor.scripts.question_delivery": SimpleNamespace(inventory=inventory, records=lambda *_: [])}):
            result = self.collect(question_queue="test/repo#123")
        self.assertTrue(result["complete"], result)

    def test_transport_quota_or_deadline_stop_is_aggregated_immediately(self):
        for cause in ("quota_reserve", "deadline_exceeded"):
            with self.subTest(cause=cause):
                reader = FakeReader()
                reader.pages[("repos/test/repo/issues", 1, "open")] = [{"number": n, "state": "open"} for n in range(1, 50)]
                reader.pages[("repos/test/repo/issues", 1, "open")][0].update({
                    "user": {"login": "test"}, "body": sweep.github_plan_wait.marker(sweep.github_plan_wait.reviewed(
                        evidence="A later comment may clear this body marker", waiting_for="Supervisor", ends_when="Reply recorded"))})
                original = reader.get_json
                def read(path, *, step):
                    if "/comments?" in path:
                        reader.paths.append(path)
                        raise sweep.github_read.GitHubReadError("Stopped bulk read", result=SimpleNamespace(
                            failure=SimpleNamespace(cause=cause)), diagnostics={})
                    return original(path, step=step)
                reader.get_json = read
                result = sweep.collect(["test/repo"], reader=reader, now=self.now)
                self.assertTrue(any(g["reason"] == cause for g in result["coverage_gaps"]))
                self.assertTrue(any(g.get("count", 0) > 40 for g in result["coverage_gaps"]))
                self.assertLess(len(reader.paths), 8)
                self.assertLess(len(result["coverage_gaps"]), 8)
                self.assertFalse(any(row["kind"] == "aged_structured_wait" for row in result["findings"]))

    def test_all_repositories_open_work_precedes_closed_history(self):
        self.reader.repositories["repos/test/other"] = {"archived": False, "default_branch": "main"}
        result = sweep.collect(["test/repo", "test/other"], reader=self.reader, now=self.now)
        states = [parse_qs(urlsplit(p).query)["state"][0] for p in self.reader.paths
                  if "state=" in p and "labels" not in parse_qs(urlsplit(p).query)]
        self.assertTrue(states and "open" in states and "closed" in states)
        self.assertLess(max(i for i, state in enumerate(states) if state == "open"),
                        min(i for i, state in enumerate(states) if state == "closed"))
        self.assertEqual(result["checked_records"], 0)

    def test_zero_comment_records_skip_comment_reads(self):
        self.reader.pages[("repos/test/repo/issues", 1, "open")] = [{"number": 1, "state": "open", "comments": 0}]
        self.collect()
        self.assertFalse(any("/comments" in p for p in self.reader.paths))

    def test_many_unread_repositories_have_one_gap_with_both_states(self):
        reader = FakeReader()
        reader.remaining = 0
        repos = [f"test/repo{n}" for n in range(40)]
        result = sweep.collect(repos, reader=reader, now=self.now)
        scope_gaps = [g for g in result["coverage_gaps"] if g.get("scopes")]
        self.assertEqual(len(scope_gaps), 1)
        self.assertEqual({s["repo"] for s in scope_gaps[0]["scopes"]}, set(repos))
        for repo in repos:
            self.assertTrue({"open", "reopen_triggers", "closed"} <=
                            {s["state"] for s in scope_gaps[0]["scopes"] if s["repo"] == repo})
        self.assertLess(len(result["coverage_gaps"]), 4)

    def test_queue_recovery_precedes_history_and_unread_is_not_missing(self):
        urls = [f"https://github.com/test/repo/issues/{n}#issuecomment-{n}" for n in (1, 2, 3)]
        def inventory(source, queue, owner, authors, now=None, *, question_urls=None):
            known = {c["url"] for c in source}
            missing = [u for u in urls[:2] if u not in known]
            rows = [{"url": u, "status": "needs_review" if u in known else "missing_source",
                     "created_at": "2026-10-14T00:00:00Z", "registration_url": "https://github.com/test/repo/issues/123#issuecomment-100",
                     "delivery": "unacknowledged", "resurface": True} for u in urls[:2]]
            self.assertEqual(question_urls, set(urls))
            return {"complete": not (missing or unnumbered), "questions": rows, "missing_sources": missing,
                    "unnumbered_sources": [urls[0]] if unnumbered else []}
        module = SimpleNamespace(inventory=inventory, records=lambda *_: [{"question_url": u} for u in urls],
                                 settled=lambda url, *_: url == urls[-1],
                                 source_item=lambda url: "test/repo#" + url.split("/issues/")[1].split("#")[0])
        for unnumbered, budget in ((False, 800), (False, 3), (True, 800), (True, 3)):
            reader = FakeReader()
            for n, url in enumerate(urls[:2], 1):
                reader.pages[(f"repos/test/repo/issues/{n}/comments", 1, None)] = [{"id": n, "html_url": url,
                    "created_at": "2026-10-14T00:00:00Z", "body": "Owner question: Should we continue?", "user": {"login": "test"}}]
            with patch.dict(sys.modules, {"skills.supervisor.scripts.question_delivery": module}):
                result = sweep.collect(["test/repo"], reader=reader, now=self.now,
                                       question_queue="test/repo#123", max_requests=budget)
            self.assertTrue(any(row["kind"] == "registered_question" for row in result["findings"]))
            self.assertFalse(any("/issues/3/comments" in path for path in reader.paths))
            if unnumbered:
                gap = next(g for g in result["coverage_gaps"] if g["reason"] == "queue_view_incomplete")
                self.assertEqual(gap.get("unnumbered_sources"), [urls[0]])
            elif budget == 800:
                self.assertTrue(result["complete"], result)
                queue_index = next(i for i, path in enumerate(reader.paths) if "/issues/123/comments" in path)
                history_index = next(i for i, path in enumerate(reader.paths) if "state=closed" in path)
                self.assertLess(queue_index, history_index)
            else:
                self.assertTrue(any(g["reason"] == "unread_registered_sources" for g in result["coverage_gaps"]))
                self.assertFalse(any(g.get("missing_sources") for g in result["coverage_gaps"]))
                self.assertFalse(any(row["url"] == urls[1] for row in result["findings"]))

    def test_register_is_read_from_the_path_the_repository_declares(self):
        metadata = "repos/test/repo/contents/.github/github.json"
        result = self.collect()
        self.assertEqual(result["operational_registers"], [])
        self.assertFalse([g for g in result["coverage_gaps"] if g["source"] != "question_delivery"])
        self.reader.files[metadata] = json.dumps({"docs": {"openOperationalIssues": "ops/follow-ups.md"}})
        self.reader.files["repos/test/repo/contents/ops/follow-ups.md"] = REGISTER
        result = self.collect()
        self.assertEqual(result["operational_registers"], ["test/repo:ops/follow-ups.md"])
        self.assertTrue(any(r["kind"] == "overdue_operational_check" and "/blob/main/ops/follow-ups.md#" in r["url"]
                            for r in result["findings"]))
        for declared, files in (("ops/missing.md", {}), ("../outside.md", {})):
            reader = FakeReader()
            reader.files = {metadata: json.dumps({"docs": {"openOperationalIssues": declared}}), **files}
            result = sweep.collect(["test/repo"], reader=reader, now=self.now)
            self.assertFalse(result["complete"])
            self.assertEqual(result["operational_registers"], [])

    def test_sweep_reserve_agrees_with_the_shared_transport(self):
        self.reader.limit = 8000
        reserve = sweep.github_request_usage.bulk_read_reserve(self.reader.limit)
        self.reader.remaining = reserve
        result = self.collect()
        self.assertEqual(result["quota_reserve"], reserve)
        self.assertEqual(self.reader.paths, ["rate_limit"])


def unavailable(cause):
    return sweep.github_read.GitHubReadError("Unavailable", result=github_api.ApiResult(
        ok=False, status=404, body={}, failure=github_api.FailureDetail(
            cause=cause, message="offline fixture", retryable=False,
            fallback_eligible=False, disposition="stop")), diagnostics={})


class ReopenInventoryTests(unittest.TestCase):
    def setUp(self):
        self.reader = FakeReader()
        self.now = dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc)

    def event(self, kind="date", **fields):
        return {"version": 1, "kind": kind, "evidence": "Deferral decision", **(fields or {"at": "2026-10-16"})}

    def issue(self, number, event=None, **fields):
        return {"number": number, "state": "closed", "user": {"login": "test"},
                "body": sweep.github_plan_trigger.marker(event) if event else "",
                "comments": 0, "updated_at": "2020-01-01T00:00:00Z", **fields}

    def collect(self, issues, **kwargs):
        self.reader.pages[("repos/test/repo/issues", 1, "closed")] = issues
        return sweep.collect(["test/repo"], reader=self.reader, now=self.now, **kwargs)

    def test_old_closed_issues_are_checked_without_age_threshold_or_history_cutoff(self):
        result = self.collect([self.issue(1, self.event()),
            self.issue(2, self.event(at="2026-10-17")),
            self.issue(3, self.event("recurrence", symptom="date 2020-01-01 causes exact error")),
            self.issue(4, self.event(), state="open"),
            self.issue(5, self.event(), pull_request={})], age_hours=1000)
        rows = [r for r in result["findings"] if r["kind"] == "reopen_trigger_fired"]
        self.assertEqual([r["number"] for r in rows], [1])
        self.assertEqual(rows[0]["evidence"][0]["trigger"], self.event())

    def test_comment_trigger_on_second_page_replaces_body_and_foreign_record_cannot_override(self):
        future = self.event(at="2027-01-01")
        comments = [{"id": n, "body": "", "created_at": "2026-01-01T00:00:00Z", "user": {"login": "test"},
                     "html_url": f"https://github.com/test/repo/issues/1#issuecomment-{n}"} for n in range(1, 101)]
        path = "repos/test/repo/issues/1/comments"
        self.reader.pages[(path, 1, None)] = comments
        self.reader.pages[(path, 2, None)] = [{**comments[0], "id": 101,
            "body": sweep.github_plan_trigger.marker(self.event())}]
        issue = self.issue(1, future, comments=101)
        self.assertEqual(len(self.collect([issue])["findings"]), 1)
        self.reader.pages[(path, 2, None)][0]["user"] = {"login": "outsider"}
        self.assertEqual(self.collect([issue])["findings"], [])

    def test_unread_comments_never_fall_back_to_an_old_fired_trigger(self):
        original = self.reader.get_json
        def read(path, *, step):
            if "/comments?" in path:
                raise unavailable("forbidden")
            return original(path, step=step)
        self.reader.get_json = read
        result = self.collect([self.issue(1, self.event(), comments=1)])
        self.assertFalse(result["complete"])
        self.assertEqual(result["findings"], [])
        self.assertTrue(any("comments" in g["source"] for g in result["coverage_gaps"]))

    def test_unreadable_target_and_malformed_record_are_coverage_gaps(self):
        result = self.collect([self.issue(1, self.event("issue_closed", url="https://github.com/target/repo/issues/2")),
                               self.issue(2, {"version": 1, "kind": "date"})])
        self.assertEqual(result["findings"], [])
        self.assertFalse(result["complete"])
        self.assertEqual({g["source"] for g in result["coverage_gaps"] if g["reason"] == "reopen_trigger_unverifiable"},
                         {"test/repo#1", "test/repo#2"})

    def test_owner_inventory_covers_archived_repositories_and_reports_unreadable_repositories(self):
        self.reader.repositories["repos/test/repo"]["archived"] = True
        self.reader.pages[("repos/test/repo/issues", 1, "closed")] = [self.issue(1, self.event())]
        self.reader.pages[("installation/repositories", 1, None)] = {"repositories": [
            {"full_name": repo, "owner": {"login": "test"}, "archived": True}
            for repo in ("test/repo", "test/missing")]}
        result = sweep.collect([], reader=self.reader, owner="test", now=self.now)
        self.assertEqual({r["repo"] for r in result["findings"]}, {"test/repo"})
        self.assertTrue(any(g["source"] == "test/missing" for g in result["coverage_gaps"]))
        self.assertFalse(result["complete"])


    def test_index_finds_old_trigger_with_bounded_reads_and_preserves_recent_promises(self):
        original = self.reader.get_json
        trigger_issue = self.issue(1, self.event())
        promise = self.issue(2, comments=1, updated_at=self.now.isoformat())
        def read(path, *, step):
            parsed = urlsplit(path)
            query = parse_qs(parsed.query)
            if parsed.path == "repos/test/repo/issues" and query.get("state") == ["closed"]:
                self.reader.paths.append(path)
                # The old labeled issue and the recently updated promise are
                # separate populations. Unrelated old history costs no reads.
                return [trigger_issue] if query.get("labels") == [sweep.github_plan_trigger.INDEX_LABEL] else [promise]
            return original(path, step=step)
        self.reader.get_json = read
        self.reader.pages[("repos/test/repo/issues/2/comments", 1, None)] = [{
            "id": 1, "body": "Director decision: comparison promised\nDue: 2026-10-15T00:00:00Z",
            "user": {"login": "test"}, "created_at": "2026-10-14T00:00:00Z",
            "html_url": "https://github.com/test/repo/issues/2#issuecomment-1"}]
        result = self.collect([], max_requests=12)
        self.assertEqual({(r["kind"], r["number"]) for r in result["findings"]},
                         {("reopen_trigger_fired", 1), ("overdue_promise", 2)})
        self.assertFalse(any("budget" in g["reason"] for g in result["coverage_gaps"]))

    def test_primary_automation_author_matches_close_even_without_bot_alias_list(self):
        issue = self.issue(1, self.event(), user={"login": "primary-bot"})
        with patch.object(sweep.github_plan_wait.github_identity, "configured_bot_logins", return_value=()), \
                patch.object(sweep.github_plan_wait.github_identity, "automation_login", return_value="primary-bot"):
            result = self.collect([issue])
        self.assertEqual([r["number"] for r in result["findings"]], [1])

    def test_unread_release_listing_is_tied_to_the_closed_issue(self):
        event = self.event("release_published", repo="target/repo", minimum_tag="v2.0")
        original = self.reader.get_json
        def read(path, *, step):
            if path.startswith("repos/target/repo/releases"):
                raise unavailable("not_found")
            return original(path, step=step)
        self.reader.get_json = read
        result = self.collect([self.issue(1, event)])
        self.assertFalse(result["complete"])
        self.assertEqual(result["findings"], [])
        self.assertTrue(any(g["source"] == "test/repo#1" and g["reason"] == "reopen_trigger_unverifiable"
                            for g in result["coverage_gaps"]))


    def test_trigger_discovery_is_not_starved_by_open_discussion_budget(self):
        self.reader.pages[("repos/test/repo/issues", 1, "open")] = [
            {"number": n, "state": "open"} for n in range(2, 90)]
        result = self.collect([self.issue(1, self.event())], max_requests=8)
        self.assertEqual([r["number"] for r in result["findings"] if r["kind"] == "reopen_trigger_fired"], [1])
        self.assertFalse(result["complete"])
        self.assertTrue(any("budget" in g["reason"] for g in result["coverage_gaps"]))


    def test_qualifying_release_on_first_full_page_fires_without_reading_old_pages(self):
        event = self.event("release_published", repo="target/repo", minimum_tag="v2.0")
        original = self.reader.get_json
        release_reads = []
        def read(path, *, step):
            if path.startswith("repos/target/repo/releases"):
                release_reads.append(path)
                return [{"draft": False, "prerelease": False, "published_at": self.now.isoformat(),
                         "tag_name": "v2.1"}] * 100
            return original(path, step=step)
        self.reader.get_json = read
        result = self.collect([self.issue(1, event)], max_pages=1)
        self.assertEqual([r["number"] for r in result["findings"]], [1])
        self.assertEqual(len(release_reads), 1)
        self.assertFalse(any(g["reason"] == "reopen_trigger_unverifiable" for g in result["coverage_gaps"]))


    def test_director_alias_cannot_change_the_shared_trigger_authority(self):
        result = self.collect([self.issue(1, self.event(), user={"login": "separate-director"})],
                              director="separate-director")
        self.assertEqual(result["findings"], [])
        self.assertTrue(any(g["reason"] == "reopen_trigger_unverifiable" for g in result["coverage_gaps"]))


if __name__ == "__main__":
    unittest.main()
