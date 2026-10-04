#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Closed-session release behavior and the six reported consumer shapes."""
from __future__ import annotations

import copy
from argparse import Namespace
from unittest.mock import patch
import unittest

import test_gh_plan_claim as fixtures
import github_plan_release as release

PLAN, CLAIM, BOT = fixtures.PLAN, fixtures.CLAIM, fixtures.TEST_BOT


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ClaimTests()
        self.f.setUp()
        self.f.comments = [self.comment(1, CLAIM.marker(fixtures.OTHER), "2026-10-01T00:00:00Z")]
        self.evidence = self.comment(88,
            "Closed session session-b\nEnded at: 2026-10-02T00:00:00Z\n"
            "Transcript-verified Safe to exit: yes; Supervisor closed this exact session.\n"
            "Handoff: https://github.com/owner/repo/issues/42#issuecomment-2",
            "2026-10-02T01:00:00Z")
        self.evidence["issue_url"] = "https://api.github.com/repos/owner/control/issues/884"
        self.args = Namespace(repo="owner/repo", issue="42", claim_comment=1,
            evidence_comment="https://github.com/owner/control/issues/884#issuecomment-88",
            role="supervisor", session="supervisor-native-session", confirm_session_ended=True,
            related_claim_comment=[], retained_pr=[], dry_run=False)
        self.commit_date = "2026-10-01T23:00:00Z"
        self.race = lambda: None

    @staticmethod
    def comment(n, body, date):
        return {"id": n, "body": body, "user": {"login": BOT}, "created_at": date, "updated_at": date,
                "html_url": f"https://github.com/owner/repo/issues/42#issuecomment-{n}"}

    def api(self, method, path, **_kwargs):
        self.assertEqual(method, "GET")
        if "issues/comments/88" in path:
            return "bot", copy.deepcopy(self.evidence)
        if "/commits/" in path:
            return "bot", {"commit": {"committer": {"date": self.commit_date}}}
        n = int(path.rsplit("/", 1)[1])
        return "bot", copy.deepcopy(next(p for p in self.f.pulls if p["number"] == n))

    def post(self, kind, number, body, **kwargs):
        result = self.f.post(kind, number, body, **kwargs)
        c = self.f.comments[-1]
        c.update(created_at="2026-10-03T00:00:00Z", updated_at="2026-10-03T00:00:00Z")
        result["comment"]["url"] = f"https://github.com/owner/repo/issues/42#issuecomment-{c['id']}"
        self.race()
        return result

    def run_release(self):
        with patch.multiple(PLAN, default_repo=lambda _: "owner/repo", get_issue=self.f.get_issue,
            EXPECTED_ACTOR=BOT, collect_paged_rest_items=self.f.read_pages,
            comment_route=lambda: ("bot", "bot-gh", BOT), emit=self.f.emitted, api_json=self.api), \
            patch.object(PLAN.github_identity, "configured_bot_logins", return_value=[BOT]), \
            patch.object(CLAIM, "local_inventory", side_effect=self.f.inventory_for), \
            patch.object(PLAN.github_comment_core, "comment", side_effect=self.post):
            PLAN.cmd_release_claim(self.args)

    def successor(self):
        self.f.args.resume_from = 1
        with patch.object(self.f, "read_api", side_effect=self.api):
            self.f.run_claim()
        self.assertTrue(self.f.emitted.call_args.args[0]["ok"])

    def test_discord_missing_release_and_ownership_followup_become_claimable(self):
        self.f.comments.append(self.comment(2, "Claimed by trial-b for capacity work.", "2026-10-01T01:00:00Z"))
        self.args.related_claim_comment = [2]
        self.run_release()
        self.successor()
        self.assertEqual(len(self.f.emitted.call_args.args[0]["claim"]), len(fixtures.OWNER))

    def test_launchplane_missing_exact_release_becomes_claimable(self):
        self.run_release()
        self.successor()

    def refresh_setup(self):
        self.f.refresh_fixture()
        for n, c in enumerate(self.f.comments):
            c.update(created_at=f"2026-10-01T0{n}:00:00Z", updated_at=f"2026-10-01T0{n}:00:00Z")
        for p in self.f.pulls:
            p["head"]["sha"] = "fixture-retained-head"
        self.args.retained_pr = [f"https://github.com/owner/repo/pull/{p['number']}" for p in self.f.pulls]

    def test_launchplane_late_release_malformed_handoff_recovers_with_new_attestation(self):
        self.refresh_setup()
        self.f.comments[2]["body"] = "trial-b handoff:\n\nReleased claim 1\nPR #99 and #100"
        self.f.comments.append(self.comment(4, "Released claim 1", "2026-10-01T03:00:00Z"))
        self.run_release()
        self.f.args.handoff_comment = self.f.comments[-1]["id"]
        self.successor()

    def test_codex_release_without_pr_attestation_recovers_with_explicit_retained_prs(self):
        self.refresh_setup()
        self.f.comments[2]["body"] = "Released claim 1\nNo PR attestation on this canonical issue."
        self.run_release()
        self.f.args.handoff_comment = self.f.comments[-1]["id"]
        self.successor()

    def test_repairshopr_embedded_release_has_explicit_supervisor_route(self):
        self.f.comments.append(self.comment(2, "Owner question: verify read-only usage?\n\nReleased claim 1", "2026-10-01T01:00:00Z"))
        self.run_release()
        self.successor()

    def test_bd_closed_native_blocker_is_accepted_without_unlinking_history(self):
        self.f.blockers = [{"number": 1165, "state": "closed"}]
        self.run_release()
        self.successor()
        self.assertEqual(self.f.blockers[0]["state"], "closed")

    def test_open_native_blocker_still_refuses(self):
        self.f.blockers = [{"number": 1165, "state": "open"}]
        self.run_release()
        with self.assertRaisesRegex(PLAN.PlanError, "resolved native blockers"):
            self.successor()

    def test_refusal_shapes_do_not_write(self):
        for mode in ("active_session", "live_retained_peer", "source_edit", "renewed_claim", "wrong_evidence_session", "missing_ended_at",
                     "foreign_evidence", "foreign_source", "unconfirmed", "releaser_is_source", "unknown_related", "unrelated_followup", "future_ended_at"):
            with self.subTest(mode=mode):
                self.setUp()
                if mode == "active_session": self.f.inventory["sessions"] = [{"sessionId": "session-b", "cwd": "/other"}]
                if mode == "live_retained_peer":
                    self.f.inventory["worktrees"] = [{"branch": fixtures.OTHER["branch"], "path": "/source"}]
                    self.f.inventory["sessions"] = [{"sessionId": "different-session", "cwd": "/source"}]
                if mode == "source_edit": self.f.comments[0]["updated_at"] = "2026-10-02T02:00:00Z"
                if mode == "renewed_claim": self.f.comments.append(self.comment(2, CLAIM.marker(fixtures.OTHER), "2026-10-02T02:00:00Z"))
                if mode == "wrong_evidence_session": self.evidence["body"] = self.evidence["body"].replace("session-b", "another")
                if mode == "missing_ended_at": self.evidence["body"] = "Closed session session-b\nSafe to exit: yes"
                if mode == "foreign_evidence": self.evidence["user"]["login"] = "other"
                if mode == "foreign_source": self.f.comments[0]["user"]["login"] = "other"
                if mode == "unconfirmed": self.args.confirm_session_ended = False
                if mode == "releaser_is_source": self.args.session = "session-b"
                if mode == "unknown_related": self.args.related_claim_comment = [99]
                if mode == "unrelated_followup":
                    self.f.comments.append(self.comment(2, "Claimed by another-worker", "2026-10-01T01:00:00Z"))
                    self.args.related_claim_comment = [2]
                if mode == "future_ended_at": self.evidence["body"] = self.evidence["body"].replace("2026-10-02T00:00:00Z", "2026-10-03T00:00:00Z")
                with self.assertRaises(PLAN.PlanError): self.run_release()
                self.f.assert_no_writes()

    def test_edited_closure_attestation_invalidates_successor_claim(self):
        self.run_release()
        self.evidence["updated_at"] = "2026-10-04T00:00:00Z"
        with self.assertRaisesRegex(PLAN.PlanError, "evidence changed"):
            self.successor()

    def test_next_keeps_closed_blocker_history_without_excluding_for_open_dependency(self):
        issue = {**self.f.issue, "repo": "owner/repo"}
        relationships = {"blocked_by": [{"number": 1165, "state": "closed"}], "blocking": [], "sub_issues": []}
        state, result = PLAN.evaluate_next_plan(issue, config=PLAN.DEFAULT_CONFIG, focus=None, relationships=relationships)
        self.assertEqual(state, "candidate")
        self.assertNotIn("exclusion", result)
        self.assertEqual(relationships["blocked_by"][0]["number"], 1165)

    def test_dry_run_is_reviewable_and_writes_nothing(self):
        self.args.dry_run = True
        self.run_release()
        self.f.assert_no_writes()
        self.assertEqual(CLAIM.released_claim_id(self.f.emitted.call_args.args[0]["release_body"]), 1)

    def test_renewed_activity_invalidates_existing_release_at_successor_claim(self):
        self.run_release()
        self.f.comments.append(self.comment(8, CLAIM.marker(fixtures.OTHER), "2026-10-04T00:00:00Z"))
        with self.assertRaisesRegex(PLAN.PlanError, "activity after"):
            self.f.args.resume_from = 1
            with patch.object(self.f, "read_api", side_effect=self.api):
                self.f.run_claim()

    def test_post_write_race_is_not_reported_as_success(self):
        self.race = lambda: self.f.comments.append(self.comment(8, CLAIM.marker(fixtures.OTHER), "2026-10-04T00:00:00Z"))
        with self.assertRaisesRegex(PLAN.PlanError, "activity after") as caught: self.run_release()
        self.assertIn("post_release", caught.exception.payload["completed_steps"])
        self.assertEqual(self.f.events.count("post"), 1)

    def test_retained_pr_updates_after_closure_refuse_before_post(self):
        self.refresh_setup()
        self.commit_date = "2026-10-02T01:00:00Z"
        with self.assertRaisesRegex(PLAN.PlanError, "commit activity after"): self.run_release()
        self.f.assert_no_writes()

    def test_unlinked_foreign_and_unmentioned_retained_prs_remain_refused(self):
        for mode in ("unlinked", "foreign", "unmentioned"):
            with self.subTest(mode=mode):
                self.setUp(); self.refresh_setup()
                if mode == "unlinked": self.f.pulls[0]["body"] = "Unrelated work"
                if mode == "foreign": self.f.pulls[0]["user"]["login"] = "someone-else"
                if mode == "unmentioned": self.args.retained_pr = self.args.retained_pr[:1]
                if mode == "unmentioned":
                    self.run_release(); self.f.args.handoff_comment = self.f.comments[-1]["id"]
                    with self.assertRaises(PLAN.PlanError): self.successor()
                else:
                    with self.assertRaises(PLAN.PlanError): self.run_release()
                    self.f.assert_no_writes()


if __name__ == "__main__":
    unittest.main()
