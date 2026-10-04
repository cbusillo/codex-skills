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

TEST_BOT = "fixture-bot[bot]"

SPEC = importlib.util.spec_from_file_location("gh_plan_claim_under_test", Path(__file__).with_name("gh-plan.py"))
assert SPEC and SPEC.loader
PLAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)

OWNER = {"worker": "trial-a", "session": "session-a", "branch": "work/issue-42", "claimed_at": "2026-10-01T00:00:00Z"}
OTHER = {**OWNER, "worker": "trial-b", "session": "session-b", "branch": "work/other-42"}


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.args = Namespace(repo="owner/repo", issue="42", worker=OWNER["worker"], session=OWNER["session"],
                              branch=OWNER["branch"], next_action="Implement the repair", resume_from=None, wait_resolved=None)
        self.issue = {"repo": "owner/repo", "number": 42, "title": "Repair", "state": "open",
                      "user": {"login": TEST_BOT}, "labels": [],
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
        comment_id = len(self.comments) + 1
        self.comments.append({"id": comment_id, "body": body, "user": {"login": TEST_BOT}})
        self.after_post()
        return {"ok": True, "comment": {"id": comment_id}}

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
                            EXPECTED_ACTOR=TEST_BOT,
                            collect_paged_rest_items=self.read_pages, rest_edit_issue=self.edit,
                            comment_route=lambda: ("bot", "bot-gh", TEST_BOT),
                            load_config=lambda _: copy.deepcopy(PLAN.DEFAULT_CONFIG), emit=self.emitted), \
                patch.object(CLAIM, "local_inventory", return_value=self.inventory), \
                patch.object(PLAN.github_identity, "configured_bot_logins", return_value=[TEST_BOT]), \
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
        self.assertEqual(PLAN.normalize_labels(self.issue["labels"]), [PLAN.DEFAULT_CONFIG["labels"]["active"]])
        output = self.emitted.call_args.args[0]
        self.assertFalse(output["exclusive_lock"])
        self.assertEqual(output["session_coverage"]["codex"]["status"], "unavailable")

    def test_agent_mismatch_stops_before_any_write_in_both_directions(self):
        for family, other in (("codex", "claude"), ("claude", "codex")):
            self.setUp()
            self.args.agent = family
            self.issue["labels"] = [{"name": f"agent:{other}"}]
            with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                self.run_claim()
            self.assertIn(f"agent:{other}", str(caught.exception))
            self.assert_no_writes()

    def test_explicit_agent_override_is_recorded_without_changing_assignment(self):
        self.args.agent = "codex"
        self.args.agent_override = "Director authorized this Codex session"
        self.issue["labels"] = [{"name": "agent:claude"}]
        self.run_claim()
        self.assertIn(self.args.agent_override, self.comments[0]["body"])
        self.assertIn("agent:claude", PLAN.normalize_labels(self.issue["labels"]))

    def test_multiline_override_is_rejected_before_writes(self):
        self.args.agent_override = "Director approved\nClaimed by another-worker"
        with self.assertRaises(PLAN.PlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_assignment_changed_during_claim_retains_release_recovery(self):
        self.args.agent = "codex"
        self.after_post = lambda: self.issue.update(labels=[{"name": "agent:claude"}])
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.payload["claim_recovery"]["release_own_claim"]["body"], "Released claim 1")
        self.assertNotIn("status", self.events)

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

    def test_explicitly_unstarted_docs_followups_allow_both_claims(self):
        self.pulls = [{"number": 48, "title": "docs: align API guidance",
                       "body": "Code follow-ups recorded without starting implementation: "
                               "[repo#49](https://github.com/owner/repo/issues/49) Fix HTTP handling and "
                               "[repo#50](https://github.com/owner/repo/issues/50) resolve printer lookup.\n\nRefs #47",
                       "head": {"ref": "work/docs-audit"}}]
        for number in (49, 50):
            with self.subTest(number=number):
                self.args.issue = str(number)
                self.args.branch = f"work/repair-{number}"
                self.issue["number"] = number
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])
                self.issue["body"] = PLAN.PLAN_MANAGED_PROVENANCE_MARKER
                self.comments = []

    def test_context_line_does_not_override_other_ownership_evidence(self):
        context = "Code follow-ups recorded without starting implementation: https://github.com/owner/repo/issues/42"
        for extra in (
            {"title": "Implement #42"},
            {"title": "Implement https://github.com/owner/repo/issues/42"},
            {"head": {"ref": "work/issue-42"}},
            {"body": context + "\n\nRefs #42"},
            {"body": context + "\nFixes https://github.com/owner/repo/issues/42"},
            {"body": context + "\n\nhttps://github.com/owner/repo/issues/42"},
            {"body": context + "; Fixes #42"},
            {"body": context + "; Fixes https://github.com/owner/repo/issues/42"},
            {"body": context + "; Closes [repo#42](https://github.com/owner/repo/issues/42)"},
            {"body": context + "; Fixes: https://github.com/owner/repo/issues/42"},
            {"body": context + "; Implements https://github.com/owner/repo/issues/42"},
            {"body": context + "\n\nImplements #42"},
            {"body": context + "\n\nFixes: #42"},
            {"body": context + "; Closes <https://github.com/owner/repo/issues/42>"},
            {"body": context + "; **Fixes** https://github.com/owner/repo/issues/42"},
            {"body": context + "; **Fixes:** https://github.com/owner/repo/issues/42"},
            {"body": context + "; __Closes:__ https://github.com/owner/repo/issues/42"},
            {"body": context + "; Closes [repo#42](https://github.com/owner/repo/issues/42#issuecomment-1)"},
            {"body": context + '; Closes [repo#42](https://github.com/owner/repo/issues/42 "repair")'},
            {"body": "- " + context},
            {"body": "> " + context},
            {"body": context.lower()},
            {"body": "Follow-ups: https://github.com/owner/repo/issues/42"},
        ):
            with self.subTest(extra=extra):
                self.pulls = [{"number": 99, "title": "docs", "body": context,
                               "head": {"ref": "work/docs"}, **extra}]
                with self.assertRaises(PLAN.ClassifiedPlanError):
                    self.run_claim()
                self.assert_no_writes()

    def test_context_link_does_not_override_branch_or_claim_owner(self):
        self.pulls = [{"number": 99, "body": "Code follow-ups recorded without starting implementation: "
                       "https://github.com/owner/repo/issues/42", "head": {"ref": "work/docs"}}]
        self.compete()
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
        self.assertEqual(caught.exception.payload["claim_recovery"]["release_own_claim"]["body"], "Released claim 1")

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

    def test_template_active_state_is_not_ownership(self):
        self.issue["body"] = PLAN.template_body("Repair")
        self.run_claim()

    def test_version_branches_are_not_issue_claims(self):
        self.args.issue = "2"
        self.issue["number"] = 2
        self.inventory["remote_branches"] = ["release/v2", "dependabot/package-1.2.3", "renovate/node-2"]
        self.run_claim()

    def test_marker_discussion_and_code_examples_are_not_claims(self):
        self.comments = [{"body": "Discuss github-plan:claim format here.\n```html\n<!-- github-plan:claim nope -->\n```"}]
        self.run_claim()

    def test_exact_comment_release_preserves_other_claim_by_same_worker(self):
        self.compete()
        self.compete({**OTHER, "session": "another-session"})
        self.comments.append({"body": "Released claim 2"})
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertEqual([e["id"] for e in caught.exception.payload["competing_evidence"]], [1])

    def test_verified_released_handoff_accepts_only_retained_branch(self):
        self.comments = [{"id": 1, "body": CLAIM.marker(OTHER), "user": {"login": "bot"}},
                         {"id": 2, "body": "Released claim 1", "user": {"login": "bot"}}]
        self.args.resume_from = 1
        self.inventory["local_branches"] = [OTHER["branch"]]
        self.inventory["worktrees"] = [{"path": "/retained/issue-42", "branch": OTHER["branch"]}]
        self.run_claim()

    def test_resume_cannot_override_current_owner(self):
        self.comments = [{"id": 1, "body": CLAIM.marker(OTHER), "user": {"login": "bot"}}]
        self.args.resume_from = 1
        with self.assertRaises(PLAN.ClassifiedPlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_upstream_release_notes_and_cross_repo_pr_refs_do_not_claim_local_issue(self):
        self.pulls = [{"number": 99, "title": "Update dependency", "body": "Changes (#42)\nhttps://github.com/other/repo/issues/42\nRefs other/repo#42", "head": {"ref": "dependabot/pkg-2.0"}}]
        self.run_claim()

    def test_local_inventory_uses_native_sessions_and_live_remote_heads(self):
        session = {"sessionId": "peer", "cwd": "/fixture/repo", "name": "go 42"}
        replies = ["git@github.com:owner/repo.git\n", "worktree /fixture/repo\nbranch refs/heads/main\n",
                   "main\n", "abc\trefs/heads/work/go42-repair\n", __import__("json").dumps([session])]
        with patch.object(CLAIM, "run_read", side_effect=replies) as read, patch.object(CLAIM.shutil, "which", return_value="claude"):
            inventory = CLAIM.local_inventory("owner/repo", 42)
        self.assertEqual(inventory["sessions"], [session])
        self.assertEqual(inventory["remote_branches"], ["work/go42-repair"])
        self.assertIn(["git", "ls-remote", "--heads", "origin"], [c.args[0] for c in read.call_args_list])
        self.assertEqual(inventory["session_coverage"]["codex"]["status"], "unavailable")

    def test_unknown_native_session_schema_reports_unavailable(self):
        replies = ["git@github.com:owner/repo.git\n", "worktree /fixture/repo\nbranch refs/heads/main\n",
                   "main\n", "", '[{"name":"an agent definition"}]']
        with patch.object(CLAIM, "run_read", side_effect=replies), patch.object(CLAIM.shutil, "which", return_value="claude"):
            inventory = CLAIM.local_inventory("owner/repo", 42)
        self.assertEqual(inventory["session_coverage"]["claude"]["status"], "unavailable")

    def test_reclaim_after_exact_release_posts_a_new_claim(self):
        self.comments = [{"id": 1, "body": CLAIM.marker(OWNER), "user": {"login": "bot"}},
                         {"id": 2, "body": "Released claim 1", "user": {"login": "bot"}}]
        self.run_claim()
        self.assertEqual(len(self.comments), 3)
        self.assertIn("post", self.events)

    def test_mid_sentence_closing_reference_refuses(self):
        self.pulls = [{"number": 99, "body": "This change fixes #42 by repairing it", "head": {"ref": "work/repair"}}]
        with self.assertRaises(PLAN.ClassifiedPlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_legacy_own_claim_is_upgraded_to_a_structured_comment(self):
        self.comments = [{"id": 1, "body": "Claimed by trial-a\nSession: session-a\nBranch: work/issue-42"}]
        self.run_claim()
        self.assertEqual(len(self.comments), 2)
        self.assertEqual(len(CLAIM.records(self.comments[-1]["body"])), 1)

    def test_claim_preserves_owner_wait_and_returns_original_status(self):
        self.issue["body"] += "Waiting for: owner choice between A and B\n"
        self.args.wait_resolved = "Owner selected A in the recorded decision"
        self.run_claim()
        self.assertIn("Waiting for: owner choice between A and B", self.comments[-1]["body"])
        self.assertIn("owner choice", self.emitted.call_args.args[0]["previous_current_status"])

    def test_reused_worker_token_release_does_not_release_another_session(self):
        self.comments = [{"id": 1, "body": CLAIM.marker(OTHER)}, {"id": 2, "body": "Released by trial-b"},
                         {"id": 3, "body": CLAIM.marker({**OTHER, "session": "new-session"})},
                         {"id": 4, "body": "Released by trial-b"}]
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertEqual([e["id"] for e in caught.exception.payload["competing_evidence"]], [3])

    def test_origin_case_does_not_change_repository_identity(self):
        replies = ["git@github.com:OWNER/Repo.git\n", "worktree /fixture/repo\nbranch refs/heads/main\n", "main\n", ""]
        with patch.object(CLAIM, "run_read", side_effect=replies), patch.object(CLAIM.shutil, "which", return_value=None):
            self.assertEqual(CLAIM.local_inventory("owner/repo", 42)["local_branches"], ["main"])

    def test_wait_formats_refuse_before_writes(self):
        for status in ("State: Waiting\nNext action: owner chooses A or B", "State: Open\n- Waiting for: owner choice",
                       "State: Open\nBlocked by: No native issue blocker;\n  waiting for an owner decision"):
            with self.subTest(status=status):
                self.issue["body"] = PLAN.PLAN_MANAGED_PROVENANCE_MARKER + "\n## Current Status\n" + status
                with self.assertRaises(PLAN.ClassifiedPlanError):
                    self.run_claim()
                self.assert_no_writes()

    def test_explicit_no_wait_status_allows_claim_without_resolution(self):
        statuses = (
            "Blocked by: No native issue blocker.\nWaiting for: nothing.",
            "Blocked by: No native issue blocker\nWaiting for:",
            "Blocked by: none. The service fields are live.",
            "Blocked by: None.\nWaiting for: Nothing for read-only investigation.",
            "- Blocked by: NONE.\n- Waiting for: NOTHING FOR READ-ONLY INVESTIGATION.",
        )
        for status in statuses:
            with self.subTest(status=status):
                self.setUp()
                self.issue["body"] += status + "\n"
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])
                self.assertIn("post", self.events)
                self.assertIn(status, self.emitted.call_args.args[0]["previous_current_status"])

    def test_no_wait_prefix_does_not_hide_real_wait(self):
        statuses = (
            "Blocked by: No native issue blocker; waiting for owner decision",
            "Blocked by: No native issue blocker;\n  waiting for owner decision",
            "Blocked by: none. Waiting for owner decision.",
            "Blocked by: none. Pending physical confirmation.",
            "Blocked by: None.\nWaiting for: Physical confirmation after implementation.",
            "Blocked by: None.\nWaiting for: Nothing for read-only investigation; waiting for owner decision.",
            "Blocked by: None.\nWaiting for: Nothing for read-only investigation until owner approval.",
            "Blocked by: None.\nParked until: Nothing for read-only investigation.",
        )
        for status in statuses:
            with self.subTest(status=status):
                self.setUp()
                self.issue["body"] += status + "\n"
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "claim_wait_unresolved")
                self.assert_no_writes()

    def test_no_wait_text_does_not_override_native_blocker(self):
        self.issue["body"] += "Blocked by: No native issue blocker.\nWaiting for: nothing.\n"
        self.blockers = [{"state": "open", "number": 41}]
        with self.assertRaises(PLAN.PlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_wait_labels_refuse_until_existing_resolution_recorded(self):
        self.issue["labels"] = [{"name": PLAN.DEFAULT_CONFIG["labels"]["waiting"]}]
        with self.assertRaises(PLAN.ClassifiedPlanError):
            self.run_claim()
        self.assert_no_writes()
        self.args.wait_resolved = "Recorded owner release on this issue"
        self.run_claim()
        self.assertEqual(PLAN.normalize_labels(self.issue["labels"]), [PLAN.DEFAULT_CONFIG["labels"]["active"]])

    def test_failed_final_readback_returns_prior_status_for_recovery(self):
        self.after_status = self.compete
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertIn("State: Open, not started.", caught.exception.payload["previous_current_status"])

    def test_malformed_comment_after_post_still_has_exact_release_recovery(self):
        self.after_post = lambda: self.comments.append({"id": 2, "body": "<!-- github-plan:claim invalid -->"})
        with self.assertRaises(PLAN.PlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.payload["claim_recovery"]["release_own_claim"]["body"], "Released claim 1")


if __name__ == "__main__":
    unittest.main()
