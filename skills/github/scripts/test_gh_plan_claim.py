#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Behavioral tests for claim ownership, races, and partial recovery."""

from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import github_plan_claim as CLAIM

SPEC = importlib.util.spec_from_file_location("gh_plan_claim_under_test", Path(__file__).with_name("gh-plan.py"))
assert SPEC and SPEC.loader
PLAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)

OWNER = {"worker": "trial-a", "session": "session-a", "branch": "work/issue-42", "claimed_at": "2026-10-01T00:00:00Z"}
OTHER = {**OWNER, "worker": "trial-b", "session": "session-b", "branch": "work/other-42"}


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.args = Namespace(repo="owner/repo", issue="42", worker=OWNER["worker"], session=OWNER["session"],
                              branch=OWNER["branch"], next_action="Implement the repair")
        self.issue = {"repo": "owner/repo", "number": 42, "title": "Repair", "state": "open",
                      "user": {"login": PLAN.EXPECTED_ACTOR}, "labels": [{"name": "plan:waiting"}],
                      "body": PLAN.PLAN_MANAGED_PROVENANCE_MARKER + "\n\n## Objective\n\nKeep me\n\n## Current Status\n\nState: Open, not started.\n"}
        self.comments = []
        self.inventory = {"worktrees": [], "local_branches": [], "remote_branches": [], "sessions": [],
                          "session_coverage": {"codex": {"status": "unavailable"}, "claude": {"status": "available"}}}
        self.pulls = []
        self.blockers = []
        self.events = []
        self.after_post = lambda: None
        self.after_status = lambda: None
        self.emitted = Mock()

    def get_issue(self, *_):
        self.events.append("read_issue")
        return "bot", copy.deepcopy(self.issue)

    def read_pages(self, path, **_):
        self.events.append("read_comments" if path.endswith("comments") else "read_pulls")
        items = self.comments if path.endswith("comments") else self.blockers if path.endswith("blocked_by") else self.pulls
        return "bot", copy.deepcopy(items)

    def post(self, _kind, _number, body, **_):
        self.events.append("post")
        self.comments.append({"id": len(self.comments) + 1, "body": body, "user": {"login": PLAN.EXPECTED_ACTOR}})
        self.after_post()
        return {"ok": True, "comment": {"id": self.comments[-1]["id"]}}

    def edit(self, _repo, _number, *, body):
        self.events.append("status")
        self.issue["body"] = body
        self.after_status()
        return "bot", copy.deepcopy(self.issue)

    def labels(self, _number, **kwargs):
        self.events.append("labels")
        names = PLAN.normalize_labels(self.issue["labels"])
        names = [n for n in names if n not in kwargs["remove_labels"]]
        self.issue["labels"] = [{"name": n} for n in set(names + kwargs["add_labels"])]
        return {"ok": True}

    def run_claim(self):
        with patch.multiple(PLAN, default_repo=lambda _: "owner/repo", get_issue=self.get_issue,
                            collect_paged_rest_items=self.read_pages, rest_edit_issue=self.edit,
                            comment_route=lambda: ("bot", "bot-gh", PLAN.EXPECTED_ACTOR),
                            load_config=lambda _: copy.deepcopy(PLAN.DEFAULT_CONFIG), emit=self.emitted), \
                patch.object(CLAIM, "local_inventory", return_value=self.inventory), \
                patch.object(PLAN.github_comment_core, "comment", side_effect=self.post), \
                patch.object(PLAN.github_issue_core, "edit_issue", side_effect=self.labels):
            PLAN.cmd_claim(self.args)

    def compete(self, record=OTHER):
        self.comments.append({"id": len(self.comments) + 1, "body": "Claimed by " + record["worker"] + "\n" + CLAIM.marker(record)})

    def assert_no_writes(self):
        self.assertFalse(set(self.events).intersection({"post", "status", "labels"}))

    def test_success_records_and_reads_back_before_metadata(self):
        self.run_claim()
        self.assertLess(self.events.index("post"), self.events.index("status"))
        self.assertIn("read_comments", self.events[self.events.index("post") + 1:self.events.index("status")])
        self.assertEqual(self.events[-2:], ["read_issue", "read_comments"])
        self.assertIn("Keep me", self.issue["body"])
        self.assertEqual(CLAIM.records(self.issue["body"])[0]["session"], self.args.session)
        self.assertEqual(PLAN.normalize_labels(self.issue["labels"]), ["plan:active"])
        output = self.emitted.call_args.args[0]
        self.assertFalse(output["exclusive_lock"])
        self.assertEqual(output["session_coverage"]["codex"]["status"], "unavailable")

    def test_current_status_owner_refuses_before_writes(self):
        self.issue["body"] += CLAIM.marker(OTHER)
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.payload["competing_evidence"][0]["source"], "current_status")
        self.assert_no_writes()

    def test_legacy_current_status_refuses(self):
        self.issue["body"] += "\nWorker: another session\nBranch: work/repair\n"
        with self.assertRaises(PLAN.ClassifiedPlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_stale_claim_is_not_expired(self):
        self.compete({**OTHER, "claimed_at": "2020-01-01T00:00:00Z"})
        with self.assertRaises(PLAN.ClassifiedPlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_legacy_comment_claim_refuses(self):
        self.comments.append({"id": 1, "body": "Claimed by old-worker\nSession: other"})
        with self.assertRaises(PLAN.ClassifiedPlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_competing_worktree_refuses(self):
        self.inventory["worktrees"] = [{"branch": "work/go42-repair", "path": "/artifacts/repair"}]
        with self.assertRaises(PLAN.ClassifiedPlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_competing_branch_refuses(self):
        for source in ("local_branches", "remote_branches"):
            with self.subTest(source=source):
                self.inventory[source] = ["work/go42-other"]
                with self.assertRaises(PLAN.ClassifiedPlanError):
                    self.run_claim()
                self.assert_no_writes()
                self.inventory[source] = []

    def test_open_pr_refuses(self):
        self.pulls = [{"number": 99, "body": "Refs #42", "head": {"ref": "work/repair"}}]
        with self.assertRaises(PLAN.ClassifiedPlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_peer_session_refuses(self):
        self.inventory["worktrees"] = [{"branch": "main", "path": "/artifacts/repo"}]
        self.inventory["sessions"] = [{"sessionId": "peer", "cwd": "/artifacts/repo", "name": "Fix #42", "status": "busy"}]
        with self.assertRaises(PLAN.ClassifiedPlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_write_readback_race_stops_before_status(self):
        self.after_post = self.compete
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertIn("post_claim", caught.exception.payload["completed_steps"])
        self.assertNotIn("status", self.events)
        self.assertNotIn("labels", self.events)

    def test_final_readback_race_does_not_report_success(self):
        self.after_status = self.compete
        with self.assertRaises(PLAN.ClassifiedPlanError):
            self.run_claim()
        self.emitted.assert_not_called()

    def test_retry_same_session_repairs_without_duplicate_comment(self):
        self.run_claim()
        self.inventory["local_branches"] = [OWNER["branch"]]
        self.run_claim()
        self.assertEqual(len(self.comments), 1)

    def test_same_worker_different_session_cannot_resume(self):
        self.compete({**OWNER, "session": "peer"})
        with self.assertRaises(PLAN.ClassifiedPlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_human_request_remains_unchanged_and_uses_comment(self):
        self.issue["user"] = {"login": "contributor"}
        self.issue["body"] = "Fix this please\n\n## Current Status\n\nState: Open, not started."
        original = self.issue["body"]
        self.run_claim()
        self.assertEqual(self.issue["body"], original)
        self.assertEqual(self.emitted.call_args.args[0]["current_status_location"], "claim_comment")

    def test_metadata_failure_can_resume_existing_claim(self):
        with patch.object(PLAN, "rest_edit_issue", side_effect=PLAN.PlanError("Unavailable")):
            # run_claim installs its own editor; inject failure at that seam instead.
            original = self.edit
            self.edit = Mock(side_effect=PLAN.PlanError("Unavailable"))
            with self.assertRaises(PLAN.PlanError) as caught:
                self.run_claim()
            self.assertIn("post_claim", caught.exception.payload["completed_steps"])
            self.edit = original
        self.run_claim()
        self.assertEqual(len(self.comments), 1)

    def test_unreadable_discussion_stops_without_writes(self):
        self.read_pages = Mock(side_effect=PLAN.PlanError("Unreadable comment page"))
        with self.assertRaises(PLAN.PlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_closed_issue_refuses(self):
        self.issue["state"] = "closed"
        with self.assertRaises(PLAN.PlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_open_native_blocker_is_preserved(self):
        self.blockers = [{"number": 1, "state": "open"}]
        with self.assertRaisesRegex(PLAN.PlanError, "resolved native blockers"):
            self.run_claim()
        self.assert_no_writes()

    def test_released_claim_does_not_hold_issue(self):
        self.compete()
        self.comments.append({"body": "Released by trial-b"})
        self.run_claim()

    def test_foreign_author_cannot_release_claim(self):
        self.compete()
        self.comments.append({"body": "Released by trial-b", "user": {"login": "other"}})
        with self.assertRaises(PLAN.ClassifiedPlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_plain_human_request_is_not_adopted_by_claim(self):
        self.issue["user"] = {"login": "contributor"}
        self.issue["body"] = "Fix this please"
        self.run_claim()
        self.assertEqual(self.issue["body"], "Fix this please")

    def test_malformed_record_is_not_ignored(self):
        self.comments.append({"body": "<!-- github-plan:claim nope -->"})
        with self.assertRaises(PLAN.PlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_issue_number_does_not_match_another_issue(self):
        self.inventory["local_branches"] = ["work/issue-142"]
        self.run_claim()


if __name__ == "__main__":
    unittest.main()
