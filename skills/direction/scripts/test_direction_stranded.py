#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Replay the week's stranded records and bounded read failures offline."""
import copy
import datetime as dt
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import direction_audit as audit
import direction_stranded as sweep


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

    def test_closed_research_promise_survives_issue_closure_until_linked_report(self):
        promise = self.records[4]["comments"][0]
        self.assertIn(1791, {r["number"] for r in self.report()["findings"]})
        self.records[4]["comments"].append({"id": 100, "created_at": "2026-10-15T13:36:21Z",
            "user": {"login": "shiny-code-app[bot]"}, "body": f"Comparison posted for Q249: {promise['html_url']}",
            "html_url": "https://github.com/cbusillo/codex-skills/issues/1791#issuecomment-100"})
        self.assertNotIn(1791, {r["number"] for r in self.report()["findings"]})

    def test_valid_new_blocker_and_person_wait_preserve_blocked_view(self):
        record = self.records[3]
        record["blockers"].append({"state": "open"})
        self.assertNotIn(218, {r["number"] for r in self.report()["findings"]})
        record["blockers"].pop()
        record["item"]["body"] = sweep.github_plan_wait.marker(sweep.github_plan_wait.reviewed(
            evidence="Actual Director wait", waiting_for="Director", ends_when="A recorded decision arrives"))
        rows = [r for r in self.report()["findings"] if r["number"] == 218]
        self.assertFalse(any(r["kind"] == "stale_blocked_view" for r in rows))
        self.assertTrue(any(r["next_actor"] == "Director" for r in rows))

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

    def test_coverage_failure_is_in_markdown_even_with_zero_findings(self):
        self.records = []
        report = self.report(gaps=[{"source": "comments", "reason": "unavailable"}])
        self.assertFalse(report["complete"])
        self.assertIn("INCOMPLETE", sweep.markdown(report))
        self.assertIn("comments", sweep.markdown(report))


class FakeReader:
    def __init__(self):
        self.paths = []
        self.remaining = 1000
        self.pages = {}

    def get_json(self, path, *, step):
        self.paths.append(path)
        if path == "rate_limit":
            return {"resources": {"core": {"remaining": self.remaining}}}
        if path == "repos/test/repo":
            return {"archived": False, "default_branch": "main"}
        return copy.deepcopy(self.pages.get(path, []))


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.reader = FakeReader()
        self.now = dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc)

    def collect(self, **kwargs):
        return sweep.collect(["test/repo"], reader=self.reader, now=self.now, **kwargs)

    def test_second_comment_page_is_read_and_closed_history_inventoried(self):
        self.reader.pages["repos/test/repo/issues?state=open&per_page=100&page=1"] = [
            {"number": 1, "state": "open", "user": {"login": "test"}}]
        path = "repos/test/repo/issues/1/comments"
        self.reader.pages[path + "?per_page=100&page=1"] = [
            {"id": n, "body": "", "created_at": "2026-10-14T00:00:00Z", "user": {"login": "test"},
             "html_url": f"https://github.com/test/repo/issues/1#issuecomment-{n}"} for n in range(100)]
        self.reader.pages[path + "?per_page=100&page=2"] = [
            {"id": 101, "body": "Owner question: Q100 Should this continue?", "created_at": "2026-10-14T01:00:00Z", "user": {"login": "test"}, "html_url": "https://github.com/test/repo/issues/1#issuecomment-101"}]
        report = self.collect()
        self.assertEqual(report["coverage_gaps"], [{"source": "question_delivery", "reason": "question_queue_not_selected"}])
        self.assertEqual(len(report["findings"]), 1)
        self.assertIn(path + "?per_page=100&page=2", self.reader.paths)
        self.assertTrue(any("state=closed&since=" in p for p in self.reader.paths))

    def test_quota_reserve_and_request_limit_expose_unread_repositories(self):
        self.reader.remaining = 250
        report = self.collect()
        self.assertFalse(report["complete"])
        self.assertEqual(self.reader.paths, ["rate_limit"])
        self.reader.remaining = 1000
        self.reader.paths.clear()
        report = self.collect(max_requests=2)
        self.assertFalse(report["complete"])
        self.assertLessEqual(len(self.reader.paths), 2)

    def test_page_cap_and_malformed_page_cannot_report_complete(self):
        path = "repos/test/repo/issues?state=open&per_page=100&page=1"
        self.reader.pages[path] = [{"number": n, "state": "open"} for n in range(1, 101)]
        report = self.collect(max_pages=1)
        self.assertFalse(report["complete"])
        self.assertTrue(any(g["reason"] == "page_limit" for g in report["coverage_gaps"]))
        self.reader.pages[path] = {"unexpected": "object"}
        self.assertFalse(self.collect()["complete"])

    def test_audit_mode_reuses_collector_and_does_not_stamp_direction(self):
        with patch.object(sweep, "collect", return_value={"complete": True}) as collect, \
                patch.object(audit, "record_audit") as mark, patch("builtins.print"):
            self.assertEqual(audit.main(["--stranded-work-only", "--repo", "test/repo"]), 0)
        collect.assert_called_once()
        mark.assert_not_called()


if __name__ == "__main__":
    unittest.main()
