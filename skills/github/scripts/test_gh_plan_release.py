#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Consumer recovery: closed-session releases and main's authored embedded releases."""
from __future__ import annotations

import copy
import shlex
import json
import os
import re
import tempfile
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
from argparse import Namespace
from unittest.mock import Mock, patch
import unittest

import test_gh_plan_claim as fixtures
import github_plan_release as release
import github_identity
import github_comment
import github_plan_claim
import github_plan_closure

PLAN, CLAIM, BOT = fixtures.PLAN, fixtures.CLAIM, fixtures.TEST_BOT


class ReleaseHarness(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ClaimTests()
        self.f.setUp()
        self.f.comments = [self.comment(1, CLAIM.marker(fixtures.OTHER), "2026-10-01T00:00:00Z")]
        self.evidence = self.comment(88,
            "Closed session session-b\nEnded at: 2026-10-02T00:00:00Z\n"
            "Safe to exit: yes\nTranscript verified; Supervisor closed this exact session.\n"
            "Handoff: https://github.com/owner/repo/issues/42#issuecomment-2",
            "2026-10-02T01:00:00Z")
        self.evidence["issue_url"] = "https://api.github.com/repos/owner/control/issues/884"
        self.args = Namespace(repo="owner/repo", issue="42", claim_comment=1,
            evidence_comment="https://github.com/owner/control/issues/884#issuecomment-88",
            role="supervisor", session="supervisor-native-session", confirm_session_ended=True,
            related_claim_comment=[], retained_pr=[], dry_run=False)
        self.commit_date = "2026-10-01T23:00:00Z"
        self.evidence_failure = None
        self.source_branch_date = None
        self.race = lambda: None
        self.confirmation_failure = None
        self.confirmation_applied = False
        self.related_failure = None

    @staticmethod
    def comment(n, body, date):
        return {"id": n, "body": body, "user": {"login": BOT}, "created_at": date, "updated_at": date,
                "html_url": f"https://github.com/owner/repo/issues/42#issuecomment-{n}"}

    def api(self, method, path, **_kwargs):
        self.assertEqual(method, "GET")
        if path.endswith(f"issues/comments/{self.evidence['id']}"):
            if self.evidence_failure:
                raise PLAN.PlanError("Evidence unavailable", api_result={"status": self.evidence_failure})
            return "bot", copy.deepcopy(self.evidence)
        if "/git/ref/heads/" in path:
            if self.source_branch_date is None:
                raise PLAN.PlanError("No published source branch", api_result={"status": 404})
            return "bot", {"object": {"sha": "source-sha"}}
        if "/commits/" in path:
            date = self.source_branch_date if path.endswith("/source-sha") else self.commit_date
            return "bot", {"commit": {"committer": {"date": date}}}
        n = int(path.rsplit("/", 1)[1])
        return "bot", copy.deepcopy(next(p for p in self.f.pulls if p["number"] == n))

    def post(self, kind, number, body, **kwargs):
        if kwargs.get("failed_step") == "release_related_ownership" and self.related_failure:
            raise self.related_failure
        if kwargs.get("edit_comment") is not None:
            self.f.events.append("confirm_release")
            c = next(c for c in self.f.comments if c["id"] == kwargs["edit_comment"])
            guards = [(kwargs.get("expected_body"), c["body"]), (kwargs.get("expected_updated_at"), c["updated_at"])]
            self.assertTrue(any(expected is not None for expected, _ in guards))
            for expected, observed in guards:
                if expected is not None:
                    self.assertEqual(observed, expected)
            if not self.confirmation_failure or self.confirmation_applied:
                c["body"] = body
            if self.confirmation_failure:
                raise self.confirmation_failure
            return {"ok": True, "comment": {"id": c["id"], "url": c["html_url"]}}
        result = self.f.post(kind, number, body, **kwargs)
        c = self.f.comments[-1]
        c.update(created_at="2026-10-03T00:00:00Z", updated_at="2026-10-03T00:00:00Z")
        result["comment"]["url"] = f"https://github.com/owner/repo/issues/42#issuecomment-{c['id']}"
        c["html_url"] = result["comment"]["url"]
        self.race()
        return result

    def run_release(self, client_actor=None):
        with patch.multiple(PLAN, default_repo=lambda _: "owner/repo", get_issue=self.f.get_issue,
            EXPECTED_ACTOR=BOT, collect_paged_rest_items=self.f.read_pages,
            comment_route=lambda: ("bot", "gh-with-env-token" if client_actor else "bot-gh", BOT), emit=self.f.emitted, api_json=self.api), \
            patch.object(github_identity, "configured_bot_logins", return_value=[BOT]), \
            patch.object(github_identity, "github_app_prefix", return_value="GITHUB_CLIENT_APP" if client_actor else "GITHUB_APP"), \
            patch.object(github_identity, "github_app_config", return_value=object()), \
            patch.object(github_identity, "github_app_auth", return_value=("fake-token", client_actor)), \
            patch.object(github_identity, "configured_value", return_value=None), \
            patch.object(github_plan_claim, "local_inventory", side_effect=self.f.inventory_for), \
            patch.object(github_comment, "comment", autospec=True, side_effect=self.post):
            PLAN.cmd_release_claim(self.args)



class ReleaseTests(ReleaseHarness):
    def test_canonical_evidence_is_attached_once_per_phase_and_invalidated_freshly(self):
        self.args.dry_run = True
        self.run_release()
        body = self.f.emitted.call_args.args[0]["release_body"]
        first = self.comment(10, body, "2026-10-02T02:00:00Z")
        second = self.comment(11, body, "2026-10-02T03:00:00Z")
        comments = [*self.f.comments, first, second]
        evidence = copy.deepcopy(self.evidence)
        available = [evidence]
        def read(_repo, _number, _identifier):
            if not available:
                raise RuntimeError("one canonical evidence read is available in this phase")
            return available.pop()
        verified = release.verified_comments(comments, read)
        self.assertEqual([c["_release_evidence"]["id"] for c in verified if release.receipt_for(c)],
                         [evidence["id"], evidence["id"]])
        evidence["updated_at"] = "2026-10-03T00:00:00Z"
        invalidated = release.verified_comments(comments, lambda *_args: evidence)
        self.assertFalse(any(release.receipt_for(c) for c in invalidated))
        self.assertTrue(CLAIM.discussion_evidence("", invalidated, fixtures.OWNER)[0])

    def test_malformed_canonical_evidence_shape_propagates_without_hiding_ownership(self):
        self.run_release()
        for field, value in (("user", None), ("body", []), ("created_at", None), ("issue_url", None), ("id", "88")):
            with self.subTest(field=field):
                malformed = {**self.evidence, field: value}
                with self.assertRaises(ValueError):
                    release.verified_comments(self.f.comments, lambda *_args: malformed)
                before = self.f.events.count("post")
                with patch.object(self, "evidence", malformed), self.assertRaises(PLAN.PlanError):
                    self.successor()
                self.assertEqual(self.f.events.count("post"), before)
        invalid = {**self.evidence, "user": {"login": "foreign[bot]"}}
        canonical = release.verified_comments(self.f.comments, lambda *_args: invalid)
        self.assertFalse(any(release.receipt_for(c) for c in canonical))
        self.assertTrue(CLAIM.discussion_evidence("", canonical, fixtures.OWNER)[0])

    def produced_related_release(self, *, earlier_hand=False, ordinary_source_release=False):
        hand = self.comment(3, "Claimed by trial-b\nSession: session-b",
            "2026-09-30T23:59:00Z" if earlier_hand else "2026-10-01T01:00:00Z")
        if earlier_hand:
            self.f.comments.insert(0, hand)
        else:
            self.f.comments.append(hand)
        if ordinary_source_release:
            self.f.comments.append(self.comment(4, "Released claim 1", "2026-10-01T02:00:00Z"))
        self.args.related_claim_comment = [3]
        self.f.comment_id_offset = 10
        self.run_release()
        return copy.deepcopy(self.f.comments)

    def consume_related_release(self, comments, *, ended="2026-10-02T00:00:00Z", evidence_by_id=None):
        release.termination.native_helpers()
        import release_session_claims as supervisor
        reader = Mock()
        reader.get_json.side_effect = lambda path, **_kw: (
            evidence_by_id[int(path.rsplit("/", 1)[1])] if evidence_by_id is not None else self.evidence
        ) if "/issues/comments/" in path else {"body": ""}
        reader.paged_json.return_value = comments
        entry = {"session_id": "session-b", "ended_at": ended, "_finish_scope": "explicit scope"}
        return supervisor.finished_claims(reader, entry, BOT, [("owner/repo", 42)], True)

    def test_actual_supervisor_related_release_is_consumed_after_source_end(self):
        produced = self.produced_related_release()
        report = self.consume_related_release(produced)
        self.assertFalse(report["issue_refusals"])
        self.assertEqual({r["claim_comment"] for r in report["released_claims"]}, {1, 3})
        source_receipt = next(c for c in produced if release.receipt_for(c))
        related_release = next(c for c in produced if CLAIM.released_claim_id(c["body"]) == 3)
        def alter(identifier, **fields):
            return [{**c, **fields} if c["id"] == identifier else c for c in produced]
        receipt = release.receipt_for(source_receipt)
        def receipt_fields(**fields):
            prefix = "<!-- " + release.RELEASE_MARKER
            body = "\n".join(prefix + json.dumps({**receipt, **fields}) + " -->"
                if line.startswith(prefix) else line for line in source_receipt["body"].splitlines())
            self.assertNotEqual(body, source_receipt["body"])
            return body
        reordered = [c for c in produced if c["id"] != related_release["id"]]
        reordered.insert(next(i for i, c in enumerate(reordered) if c["id"] == source_receipt["id"]), related_release)
        cases = [
            alter(source_receipt["id"], body=receipt_fields(related_ids=[])),
            alter(source_receipt["id"], created_at=next(c["created_at"] for c in produced if c["id"] == 1)),
            alter(source_receipt["id"], user={"login": "foreign[bot]"}),
            alter(source_receipt["id"], body=receipt_fields(source={**receipt["source"], "session": "another-session"})),
            alter(1, user={"login": "foreign[bot]"}),
            alter(1, updated_at="2026-10-04T00:00:00Z"),
            alter(3, user={"login": "foreign[bot]"}),
            [c for c in produced if c["id"] != related_release["id"]],
            alter(related_release["id"], user={"login": "foreign[bot]"}),
            alter(source_receipt["id"], body=release.pending_body(source_receipt["body"])),
            alter(3, body="Claimed by wrong-worker\nSession: session-b"),
            alter(3, body="Claimed by trial-b\nSession: other-session", updated_at="2026-10-04T00:00:00Z"),
            alter(3, updated_at="2026-10-04T00:00:00Z"),
            reordered,
        ]
        for comments in cases:
            with self.subTest(comments=comments):
                failed = self.consume_related_release(comments)
                self.assertTrue(failed["issue_refusals"])
                self.assertNotIn(3, [r["claim_comment"] for r in failed["released_claims"]])
        changed_evidence = {**self.evidence, "updated_at": "2026-10-04T00:00:00Z"}
        with patch.object(self, "evidence", changed_evidence):
            failed = self.consume_related_release(produced)
        self.assertTrue(failed["issue_refusals"])
        self.assertNotIn(3, [r["claim_comment"] for r in failed["released_claims"]])
        # The receipt may attest a later end than the verified closure (CS2319-C1).
        accepted = self.consume_related_release(produced, ended="2026-10-01T23:00:00Z")
        self.assertFalse(accepted["issue_refusals"])
        self.assertEqual({r["claim_comment"] for r in accepted["released_claims"]}, {1, 3})

    def test_receipt_related_authority_survives_earlier_hand_worker_release_and_narrative_edits(self):
        for case in ("earlier-hand", "ordinary-source-release", "narrative", "receipt-edit", "earlier-hand-and-ordinary-release"):
            with self.subTest(case=case):
                self.setUp()
                produced = self.produced_related_release(
                    earlier_hand=case in {"earlier-hand", "earlier-hand-and-ordinary-release"},
                    ordinary_source_release=case in {"ordinary-source-release", "earlier-hand-and-ordinary-release"})
                if case == "narrative":
                    produced = [{**c, "body": "Released claim 3\n\nReconciled ownership."}
                        if CLAIM.released_claim_id(c["body"]) == 3 else c for c in produced]
                if case == "receipt-edit":
                    produced = [{**c, "body": c["body"] + "\nAdditional explanation.", "updated_at": "2026-10-05T00:00:00Z"}
                        if release.receipt_for(c) else c for c in produced]
                report = self.consume_related_release(produced)
                self.assertFalse(report["issue_refusals"])
                self.assertEqual({r["claim_comment"] for r in report["released_claims"]}, {1, 3})

    def test_structured_finish_uses_later_valid_receipt_when_first_worker_release_is_ineligible(self):
        for case in ("posted-after-end", "edited-after-end"):
            with self.subTest(case=case):
                self.setUp()
                first = self.comment(4, "Released claim 1",
                    "2026-10-02T02:00:00Z" if case == "posted-after-end" else "2026-10-01T02:00:00Z")
                if case == "edited-after-end":
                    first["updated_at"] = "2026-10-02T02:00:00Z"
                self.f.comments.append(first)
                self.f.comment_id_offset = 10
                self.run_release()
                produced = copy.deepcopy(self.f.comments)
                receipt_comment = next(c for c in produced if release.receipt_for(c))
                accepted = self.consume_related_release(produced)
                self.assertFalse(accepted["issue_refusals"])
                self.assertEqual(accepted["released_claims"][0]["handoff_url"], receipt_comment["html_url"])
                without = [c for c in produced if c["id"] != receipt_comment["id"]]
                invalid = [{**c, "body": release.pending_body(c["body"])} if c["id"] == receipt_comment["id"] else c for c in produced]
                for remaining in (without, invalid):
                    with self.subTest(remaining=remaining):
                        refused = self.consume_related_release(remaining)
                        self.assertTrue(refused["issue_refusals"])
                        self.assertFalse(refused["released_claims"])
                changed_evidence = {**self.evidence, "updated_at": "2026-10-04T00:00:00Z"}
                with patch.object(self, "evidence", changed_evidence):
                    refused = self.consume_related_release(produced)
                self.assertTrue(refused["issue_refusals"])
                self.assertFalse(refused["released_claims"])

    def test_first_finish_facts_can_be_reconciled_through_actual_producer(self):
        hand = self.comment(3, "Claimed by trial-b\nSession: session-b", "2026-10-01T01:00:00Z")
        self.f.comments.append(hand)
        selected_end = "2026-10-02T00:00:00Z"
        self.evidence["body"] = self.evidence["body"].replace(selected_end, "2026-10-02T00:30:00Z")
        before = self.consume_related_release(self.f.comments, ended=selected_end)
        self.assertEqual(before["ended_at"], selected_end)
        self.assertEqual({r["claim_comment"] for r in before["issue_refusals"]}, {1, hand["id"]})
        self.assertTrue(before.get("next_action"))
        # Source and related identity come from canonical records, never advice.
        self.args.claim_comment = self.f.comments[0]["id"]
        self.args.related_claim_comment = [hand["id"]]
        self.evidence["body"] = "\n".join(
            "Ended at: " + before["ended_at"] if line.startswith("Ended at: ") else
            "Original ownership context: " + self.f.comments[0]["html_url"] if line.startswith("Handoff: ") else line
            for line in self.evidence["body"].splitlines())
        self.f.comment_id_offset = 10
        self.run_release()
        after = self.consume_related_release(self.f.comments, ended=selected_end)
        self.assertFalse(after["issue_refusals"])
        self.assertEqual({r["claim_comment"] for r in after["released_claims"]}, {1, hand["id"]})

    def test_finish_accepts_producer_evidence_attesting_a_later_end(self):
        selected_end = "2026-10-02T00:00:00Z"
        self.evidence["body"] = self.evidence["body"].replace(selected_end, "2026-10-02T00:30:00Z")
        produced = self.produced_related_release()
        accepted = self.consume_related_release(produced, ended=selected_end)
        self.assertEqual(accepted["ended_at"], selected_end)
        self.assertFalse(accepted["issue_refusals"])
        self.assertEqual({r["claim_comment"] for r in accepted["released_claims"]}, {1, 3})

    def test_finish_reports_partial_related_publication_once_per_exact_unreleased_id(self):
        self.f.comments.extend([self.comment(identifier, "Claimed by trial-b\nSession: session-b", "2026-10-01T01:00:00Z")
                                for identifier in (3, 5)])
        self.args.related_claim_comment = [3, 5]
        self.f.comment_id_offset = 10
        post = self.post
        def partial(kind, number, body, **kwargs):
            if CLAIM.released_claim_id(body) == 5:
                raise github_comment.CommentError("Related write rejected", failure=PLAN.github_api_core.FailureDetail(
                    cause="rejected", message="Related write rejected", retryable=False,
                    fallback_eligible=False, disposition="stop", write_outcome="rejected"))
            return post(kind, number, body, **kwargs)
        with patch.object(self, "post", side_effect=partial), self.assertRaises(PLAN.PlanError):
            self.run_release()
        refused = self.consume_related_release(self.f.comments)
        self.assertEqual({r["claim_comment"] for r in refused["released_claims"]}, {1, 3})
        self.assertEqual([r.get("claim_comment") for r in refused["issue_refusals"]], [5])
        self.assertTrue(refused.get("next_action"))
        self.run_release()
        accepted = self.consume_related_release(self.f.comments)
        self.assertFalse(accepted["issue_refusals"])
        self.assertEqual({r["claim_comment"] for r in accepted["released_claims"]}, {1, 3, 5})

    def test_finish_reports_orphan_and_foreign_ownership_once_with_exact_identity(self):
        orphan = self.comment(7, "Claimed by orphan-worker\nSession: session-b", "2026-10-01T01:00:00Z")
        foreign = {**self.f.comments[0], "user": {"login": "foreign[bot]"}}
        for source in (orphan, foreign):
            with self.subTest(source=source):
                refused = self.consume_related_release([source])
                self.assertEqual([r.get("claim_comment") for r in refused["issue_refusals"]], [source["id"]])
                self.assertTrue(refused.get("next_action"))
                self.assertFalse(refused["released_claims"])

    def test_ambiguous_or_different_native_source_needs_identity_reconciliation_before_release(self):
        for source_body in (
                CLAIM.marker(fixtures.OTHER) + "\n" + CLAIM.marker({**fixtures.OTHER, "branch": "work/second-source"}),
                CLAIM.marker({**fixtures.OTHER, "session": "different-session"}) + "\nSession: session-b"):
            with self.subTest(source_body=source_body):
                self.setUp()
                self.f.comments[0]["body"] = source_body
                with self.assertRaises(PLAN.PlanError):
                    self.run_release()
                self.f.assert_no_writes()
                refused = self.consume_related_release(self.f.comments)
                self.assertEqual([r["claim_comment"] for r in refused["issue_refusals"] if "claim_comment" in r], [1])
                self.assertFalse(refused["released_claims"])

    def test_known_source_refusals_precede_retained_pr_transport_failure(self):
        cases = ("source-activity", "related-activity", "related-identity")
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                self.f.inventory["worktrees"] = [{"branch": fixtures.OTHER["branch"], "path": "/source"}]
                if case == "source-activity":
                    self.f.comments[0]["updated_at"] = "2026-10-03T00:00:00Z"
                else:
                    native = "different-session" if case == "related-identity" else "session-b"
                    at = "2026-10-03T00:00:00Z" if case == "related-activity" else "2026-10-01T01:00:00Z"
                    self.f.comments.append(self.comment(2, f"Claimed by trial-b\nSession: {native}", at))
                    self.args.related_claim_comment = [2]
                reads = []
                original = self.f.read_pages
                def pages(path, **kwargs):
                    if path.endswith("/pulls"):
                        reads.append(path)
                        raise PLAN.PlanError("fixture transport unavailable", api_result={"status": 503})
                    return original(path, **kwargs)
                with patch.object(self.f, "read_pages", side_effect=pages), self.assertRaises(PLAN.PlanError):
                    self.run_release()
                self.assertFalse(reads)
                self.f.assert_no_writes()

    def test_retained_path_pr_read_failure_refuses_before_writes(self):
        self.f.inventory["worktrees"] = [{"branch": "repair-branch", "path": "/retained"}]
        reads = []
        original = self.f.read_pages
        def pages(path, **kwargs):
            if path.endswith("/pulls"):
                reads.append(path)
                raise PLAN.PlanError("fixture transport unavailable", api_result={"status": 503})
            return original(path, **kwargs)
        with patch.object(self.f, "read_pages", side_effect=pages), self.assertRaises(PLAN.PlanError) as caught:
            self.run_release()
        self.assertEqual(PLAN.plan_error_status(caught.exception), 503)
        self.assertEqual(len(reads), 1)
        self.f.assert_no_writes()

    def test_malformed_ended_at_precedes_pr_reads_for_subject_and_legacy_paths(self):
        for legacy in (False, True):
            for ended in ("", "Ended at: invalid\n",
                          "Ended at: 2026-10-01T00:00:00Z\nEnded at: 2026-10-01T00:00:00Z\n"):
                with self.subTest(legacy=legacy, ended=ended):
                    self.setUp()
                    if legacy:
                        self.f.comments[0]["body"] = "Claimed by old-worker"
                        self.args.legacy, self.args.legacy_branch, self.args.legacy_reason = True, "old-branch", "no native ID"
                        self.evidence["body"] = ("Closed legacy claim 1\n" + ended + "Safe to exit: yes\n"
                            "Worker: old-worker\nSource branch: old-branch\nNative session unavailable: no native ID")
                    else:
                        self.f.issue["pull_request"] = {}
                        self.evidence["body"] = "Closed session session-b\n" + ended + "Safe to exit: yes"
                    def evidence_only(method, path, **kwargs):
                        if not path.endswith(f"/issues/comments/{self.evidence['id']}"):
                            self.fail("malformed closure must refuse before reading retained PRs or source commits")
                        return ReleaseHarness.api(self, method, path, **kwargs)
                    with patch.object(self, "api", side_effect=evidence_only), self.assertRaises(PLAN.PlanError):
                        self.run_release()
                    self.f.assert_no_writes()

    def pr_setup(self):
        self.f.pr_fixture("dependabot[bot]")
        self.f.pulls[0]["head"]["sha"] = "a" * 40

    def test_pr_closed_session_attestation_releases_and_allows_successor(self):
        self.pr_setup()
        self.run_release()
        self.assertTrue(self.f.emitted.call_args.args[0]["ok"])
        self.successor()
        self.assertEqual(self.f.emitted.call_args.args[0]["record_kind"], "pr")
        self.assertEqual(self.f.issue["user"]["login"], "dependabot[bot]")

    def test_closed_unmerged_pr_can_release_its_ended_session_without_reopening(self):
        self.pr_setup()
        self.f.issue["state"] = "closed"
        self.f.pulls[0]["state"] = "closed"
        self.run_release()
        self.assertTrue(self.f.emitted.call_args.args[0]["ok"])
        self.assertEqual(self.f.issue["state"], "closed")
        with self.assertRaises(PLAN.PlanError):
            self.f.run_claim()

    def test_dependabot_rebase_after_end_does_not_block_release_of_separate_source_branch(self):
        self.pr_setup()
        self.commit_date = "2026-10-02T01:00:00Z"
        self.source_branch_date = "2026-10-01T23:00:00Z"
        self.run_release()
        self.successor()
        self.assertTrue(self.f.emitted.call_args.args[0]["ok"])

    def test_pr_release_still_refuses_new_activity_on_claimed_source_branch(self):
        for same_branch in (True, False):
            with self.subTest(same_branch=same_branch):
                self.setUp()
                self.pr_setup()
                if same_branch:
                    self.f.pulls[0]["head"]["ref"] = fixtures.OTHER["branch"]
                    self.commit_date = "2026-10-02T01:00:00Z"
                else:
                    self.source_branch_date = "2026-10-02T01:00:00Z"
                with self.assertRaises(PLAN.PlanError):
                    self.run_release()
                self.f.assert_no_writes()

    def test_pr_claim_release_preserves_separate_repair_pr_linked_by_canonical_pr_url(self):
        self.pr_setup()
        self.source_branch_date = self.commit_date
        self.f.pulls.append({"number": 99, "state": "open", "user": {"login": BOT},
            "body": "Refs https://github.com/owner/repo/pull/42", "head": {"ref": fixtures.OTHER["branch"],
            "sha": "b" * 40, "repo": {"full_name": "owner/repo"}}, "base": {"repo": {"full_name": "owner/repo"}}})
        self.run_release()
        self.successor()
        self.assertEqual(self.f.pulls[1]["state"], "open")

    def test_pr_comment_attestation_url_releases_and_stays_valid_on_claim_readback(self):
        self.pr_setup()
        self.args.evidence_comment = self.args.evidence_comment.replace("/issues/884", "/pull/884")
        self.run_release()
        self.successor()
        self.assertTrue(self.f.emitted.call_args.args[0]["ok"])

    def test_pr_attestation_new_activity_or_changed_evidence_refuses(self):
        for mode in ("commit", "evidence"):
            with self.subTest(mode=mode):
                self.setUp()
                self.pr_setup()
                if mode == "commit":
                    self.f.pulls[0]["head"]["ref"] = fixtures.OTHER["branch"]
                    self.commit_date = "2026-10-02T01:00:00Z"
                else:
                    self.evidence["user"]["login"] = "stranger"
                with self.assertRaises(PLAN.PlanError):
                    self.run_release()
                self.f.assert_no_writes()

    def test_release_routes_source_identity_without_relaxing_author_checks(self):
        client_actor = "fixture-client[bot]"
        for source in (self.f.comments[0], self.evidence):
            source["user"]["login"] = client_actor
        self.args.dry_run = True
        self.run_release(client_actor)
        self.assertTrue(self.f.emitted.call_args.args[0]["ok"])
        self.f.assert_no_writes()
        self.f.comments[0]["user"]["login"] = BOT
        with self.assertRaisesRegex(PLAN.PlanError, "source claim's configured automation identity"):
            self.run_release(client_actor)
        self.f.assert_no_writes()

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
        self.assertEqual(self.f.emitted.call_args.args[0]["claim"]["session"], fixtures.OWNER["session"])

    def termination_setup(self, harness="codex"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        transcript = root / "session-b.jsonl"
        at = "2026-10-01T23:59:00Z"
        rows = ([{"type": "session_meta", "timestamp": at, "payload": {"id": "session-b"}},
                 {"type": "event_msg", "timestamp": at, "payload": {"type": "agent_message", "message": "Still working"}}]
                if harness == "codex" else
                [{"sessionId": "session-b", "timestamp": at, "type": "assistant",
                  "message": {"content": [{"type": "text", "text": "Still working"}]}}])
        transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
        os.utime(transcript, (release.stamp(at).timestamp(),) * 2)
        self.termination_record = {
            "session_id": "session-b", "harness": harness, "pid": 12345, "tty": "ttys123",
            "host": "source-host", "transcript": str(transcript), "transcript_roots": [str(root)],
            "tmux_socket": "supervisor", "tmux_session_id": "tmux:supervisor:" + "a" * 32,
            "ended_at": "2026-10-02T00:00:00Z", "cause": "terminal force-quit",
            "retained_artifacts": [{"branch": fixtures.OTHER["branch"], "worktree": str(root / "preserved")}],
        }
        preserved = root / "preserved"
        subprocess.run(["git", "init", "-q", str(preserved)], check=True)
        subprocess.run(["git", "-C", str(preserved), "symbolic-ref", "HEAD", "refs/heads/" + fixtures.OTHER["branch"]], check=True)
        self.f.inventory["local_branches"] = [fixtures.OTHER["branch"]]
        self.uncommitted_source = "uncommitted source work"
        (preserved / "unfinished.txt").write_text(self.uncommitted_source)
        self.termination_path = root / "termination.json"
        self.args.termination_record = self.termination_path
        fixed_now = release.stamp("2026-10-02T01:00:00Z")

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now.astimezone(tz or timezone.utc)

        self.cutoff_patch = patch.object(release.termination.native_helpers()[1], "native_exit_cutoff", return_value=None)
        shared = release.termination.native_helpers()[1]
        self.enterContext(patch.object(shared, "process_arguments",
            side_effect=lambda _pid, command: (shared.harness_arguments(command), True)))
        for mock in (patch.object(release.termination, "datetime", Clock),
                     patch.object(release.termination.socket, "gethostname", return_value="source-host"),
                     self.cutoff_patch,
                     patch.object(release.termination, "inventory", return_value=[]),
                     patch.object(release.termination, "run_terminal", return_value=SimpleNamespace(
                         returncode=1, stdout="", stderr="no server running on /fixture/socket"))):
            mock.start()
            self.addCleanup(mock.stop)
        self.termination_path.write_text(json.dumps(self.termination_record))
        data = release.termination.verify(self.termination_record, "session-b")
        self.evidence["body"] = release.termination.body(data)
        return transcript

    def test_terminated_session_releases_without_verdict_and_successor_can_claim(self):
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                self.setUp()
                self.termination_setup(harness)
                self.run_release()
                released = self.f.comments[-1]
                receipt = release.receipt_for(released)
                self.assertEqual(receipt["termination"]["retained_artifacts"], self.termination_record["retained_artifacts"])
                canonical = release.verified_comments(self.f.comments, lambda *_: self.evidence)
                self.assertTrue(any(release.receipt_for(c) for c in canonical))
                self.assertEqual(release._released_source_contexts(canonical), ())
                self.assertIn(self.termination_record["retained_artifacts"][0]["worktree"], released["body"])
                self.successor()
                artifact = self.termination_record["retained_artifacts"][0]
                self.assertEqual((Path(artifact["worktree"]) / "unfinished.txt").read_text(), self.uncommitted_source)

    def test_terminated_session_with_live_process_or_resumed_process_refuses(self):
        self.termination_setup()
        for processes in ([(12345, "other", "codex")], [(88, "ttys123", "claude")],
                          [(88, "other", "codex resume session-b")]):
            with self.subTest(processes=processes), patch.object(release.termination, "inventory", return_value=processes):
                with self.assertRaises(PLAN.PlanError):
                    self.run_release()
                self.f.assert_no_writes()

    def test_termination_tty_reuse_requires_native_cutoff_and_pending_action_veto(self):
        self.termination_setup()
        self.cutoff_patch.stop()
        import tmux_terminal
        shared = release.termination.native_helpers()[1]
        cutoff = release.stamp(self.termination_record["ended_at"])
        values = {"session_id": "$1", "window_id": "@1", "pane_id": "%1", "pane_title": "ended",
                  "pane_tty": "/dev/ttys123", "pane_pid": "", "pane_dead": "1", "pane_dead_status": "0",
                  "@supervisor_identity": "a" * 32, "pid": "999", "pane_dead_time": str(int(cutoff.timestamp()))}
        native = "\t".join(values.get(field, "") for field in tmux_terminal.PANE_FIELDS) + "\n"
        with patch.object(release.termination, "inventory", return_value=[(88, "ttys123", "codex --remote unix:///new-client")]), \
                patch.object(shared, "process_started_at", return_value=cutoff + timedelta(seconds=1)), \
                patch.object(tmux_terminal.Terminal, "run", return_value=SimpleNamespace(returncode=0, stdout=native)), \
                patch.object(release.termination, "run_terminal", return_value=SimpleNamespace(
                    returncode=0, stdout="a" * 32 + "\t1\t1\n", stderr="")), \
                patch.object(tmux_terminal.Terminal, "readonly_control_supported", return_value=True), \
                patch.object(github_plan_closure, "guarded_launch_endpoint", return_value="/new-client"), \
                patch.object(github_plan_closure, "action_pending", return_value=False):
            # The minimal historical record takes the strict path.
            with self.assertRaises(ValueError):
                release.termination.verify(self.termination_record, "session-b")
            self.termination_record.update(supervisor_owned=True, window_id="$1", tab_id="@1")
            self.assertEqual(shared.native_exit_cutoff(self.termination_record), cutoff)
            release.termination.verify(self.termination_record, "session-b")
            with patch.object(github_plan_closure, "action_pending", return_value=True), self.assertRaises(ValueError):
                release.termination.verify(self.termination_record, "session-b")
            with patch.object(github_plan_closure, "action_pending", return_value=True):
                process = release.termination._verify_queue_source(self.termination_record, "session-b")
                with patch.object(tmux_terminal.Terminal, "run", return_value=SimpleNamespace(returncode=0, stdout="")):
                    release.termination._recheck_queue_process(process)
            self.termination_record["tab_id"] = "@wrong"
            with self.assertRaises(ValueError):
                release.termination.verify(self.termination_record, "session-b")
            with self.assertRaises(ValueError):
                release.termination._verify_queue_source(self.termination_record, "session-b")

    def test_public_termination_rechecks_terminal_after_transcript_quietness(self):
        self.termination_setup()
        original = release.termination.transcript_snapshot
        def source(*args):
            value = original(*args)
            release.termination.run_terminal.return_value = SimpleNamespace(
                returncode=0, stdout="a" * 32 + "\t0\t1\n", stderr="")
            return value
        with patch.object(release.termination, "transcript_snapshot", side_effect=source), self.assertRaises(ValueError):
            release.termination.verify(self.termination_record, "session-b")
        self.f.assert_no_writes()

    def test_terminated_session_recent_or_resumed_transcript_refuses(self):
        for mode in ("mtime", "native_record", "other_account"):
            with self.subTest(mode=mode):
                self.setUp()
                transcript = self.termination_setup()
                recent = release.stamp("2026-10-02T00:59:00Z").timestamp()
                if mode == "mtime":
                    os.utime(transcript, (recent,) * 2)
                else:
                    target = transcript if mode == "native_record" else transcript.with_name("resumed-session-b.jsonl")
                    target.write_text(transcript.read_text() + json.dumps({"timestamp": "2026-10-02T00:59:00Z", "type": "session_meta", "payload": {"id": "session-b"}}) + "\n")
                with self.assertRaises(PLAN.PlanError):
                    self.run_release()
                self.f.assert_no_writes()

    def test_terminated_session_uncertain_identity_terminal_or_evidence_refuses(self):
        for mode in ("pid", "host", "transcript", "pane", "server_error", "missing_record", "artifacts", "source_branch"):
            with self.subTest(mode=mode):
                self.setUp()
                transcript = self.termination_setup()
                if mode == "pid": self.termination_record["pid"] = None
                if mode == "host": self.termination_record["host"] = "other-host"
                if mode == "transcript": transcript.write_text(transcript.read_text().replace("session-b", "different-session"))
                if mode in {"pane", "server_error"}:
                    release.termination.run_terminal.return_value = SimpleNamespace(
                        returncode=0 if mode == "pane" else 1,
                        stdout="a" * 32 + "\t0\t1\n", stderr="error connecting to /fixture/socket (Permission denied)")
                if mode == "missing_record": self.args.termination_record = None
                if mode == "artifacts": self.termination_record["retained_artifacts"] = []
                if mode == "source_branch":
                    artifact = self.termination_record["retained_artifacts"][0]
                    artifact["branch"] = "work/other"
                    subprocess.run(["git", "-C", artifact["worktree"], "symbolic-ref", "HEAD", "refs/heads/" + artifact["branch"]], check=True)
                    self.evidence["body"] = release.termination.body(release.termination.verify(self.termination_record, "session-b"))
                self.termination_path.write_text(json.dumps(self.termination_record))
                with self.assertRaises(PLAN.PlanError):
                    self.run_release()
                self.f.assert_no_writes()

    def test_terminated_session_dead_pane_and_edited_attestation(self):
        self.termination_setup()
        release.termination.run_terminal.return_value = SimpleNamespace(returncode=0, stdout="a" * 32 + "\t1\t1\n", stderr="")
        self.run_release()
        self.evidence["updated_at"] = "2026-10-04T00:00:00Z"
        with self.assertRaises(PLAN.PlanError):
            self.successor()

    def test_termination_uses_launch_nonce_and_distinguishes_restarted_server(self):
        self.termination_setup()
        ended = release.stamp(self.termination_record["ended_at"]).timestamp()
        for nonce, dead, started, allowed in (("a" * 32, "1", ended - 100, True),
                                              ("b" * 32, "1", ended + 100, True),
                                              ("b" * 32, "1", ended - 100, False),
                                              ("a" * 32, "0", ended + 100, False)):
            with self.subTest(nonce=nonce, dead=dead, started=started):
                release.termination.run_terminal.return_value = SimpleNamespace(
                    returncode=0, stdout=f"{nonce}\t{dead}\t{int(started)}\n", stderr="")
                if allowed:
                    self.run_release()
                    self.assertTrue(self.f.emitted.call_args.args[0]["ok"])
                else:
                    posts = self.f.events.count("post")
                    with self.assertRaises(PLAN.PlanError):
                        self.run_release()
                    self.assertEqual(self.f.events.count("post"), posts)

    def test_historical_termination_receipt_survives_later_quiet_period_policy(self):
        self.termination_setup()
        self.run_release()
        with patch.object(release.termination, "QUIET_PERIOD", timedelta(days=2)):
            self.successor()

    def test_termination_wrong_or_missing_retained_worktree_refuses(self):
        self.termination_setup()
        artifact = self.termination_record["retained_artifacts"][0]
        for path, branch in ((artifact["worktree"], "work/wrong"),
                             (str(self.termination_path.parent / "absent"), artifact["branch"])):
            with self.subTest(path=path, branch=branch):
                record = {**self.termination_record, "retained_artifacts": [{"worktree": path, "branch": branch}]}
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    release.termination.verify(record, "session-b")

    def test_crash_truncated_tail_is_preserved_but_interior_corruption_refuses(self):
        transcript = self.termination_setup()
        original = transcript.read_text()
        partial = '{"timestamp":"2026-10-01T23:59:00Z","payload":'
        for contents, allowed in ((original + partial, True), (partial + "\n" + original, False)):
            with self.subTest(allowed=allowed):
                transcript.write_text(contents)
                at = release.stamp("2026-10-01T23:59:00Z").timestamp()
                os.utime(transcript, (at,) * 2)
                if allowed:
                    self.run_release()
                    self.assertEqual(transcript.read_text(), contents)
                else:
                    posts = self.f.events.count("post")
                    with self.assertRaises(PLAN.PlanError):
                        self.run_release()
                    self.assertEqual(self.f.events.count("post"), posts)

    def test_crash_before_worktree_or_with_remote_only_branch_releases(self):
        for branch_exists in (False, True):
            with self.subTest(branch_exists=branch_exists):
                self.setUp()
                self.termination_setup()
                self.f.inventory["local_branches"] = []
                self.f.inventory["remote_branches"] = [fixtures.OTHER["branch"]] if branch_exists else []
                artifacts = [{"branch": fixtures.OTHER["branch"], "worktree": None}] if branch_exists else []
                self.termination_record["retained_artifacts"] = artifacts
                self.termination_path.write_text(json.dumps(self.termination_record))
                self.evidence["body"] = release.termination.body(release.termination.verify(self.termination_record, "session-b"))
                self.run_release()
                self.successor()

    def test_crash_during_detached_operation_keeps_work_and_branch(self):
        self.termination_setup()
        artifact = self.termination_record["retained_artifacts"][0]
        subprocess.run(["git", "-C", artifact["worktree"], "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                        "-c", "core.hooksPath=/dev/null", "commit", "-q", "--allow-empty", "-m", "fixture"], check=True)
        subprocess.run(["git", "-C", artifact["worktree"], "checkout", "--detach", "--quiet"], check=True)
        self.run_release()
        self.assertEqual((Path(artifact["worktree"]) / "unfinished.txt").read_text(), self.uncommitted_source)

    def test_crash_recovery_preserves_an_actual_conflicted_rebase(self):
        self.termination_setup()
        artifact = self.termination_record["retained_artifacts"][0]
        path = Path(artifact["worktree"])
        prefix = ["git", "-C", str(path), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]
        conflict = path / "conflict.txt"
        conflict.write_text("base\n")
        subprocess.run([*prefix, "add", "conflict.txt"], check=True)
        subprocess.run([*prefix, "commit", "-q", "-m", "base"], check=True)
        subprocess.run([*prefix, "branch", "upstream"], check=True)
        conflict.write_text("source\n")
        subprocess.run([*prefix, "commit", "-q", "-am", "source"], check=True)
        subprocess.run([*prefix, "checkout", "-q", "upstream"], check=True)
        conflict.write_text("upstream\n")
        subprocess.run([*prefix, "commit", "-q", "-am", "upstream"], check=True)
        subprocess.run([*prefix, "checkout", "-q", artifact["branch"]], check=True)
        attempted = subprocess.run([*prefix, "rebase", "upstream"], capture_output=True, text=True)
        self.assertNotEqual(attempted.returncode, 0)
        state = subprocess.run([*prefix, "rev-parse", "--git-path", "rebase-merge"], capture_output=True, text=True, check=True).stdout.strip()
        state_path = Path(state) if Path(state).is_absolute() else path / state
        self.assertTrue(state_path.is_dir())
        self.run_release()
        self.assertTrue(state_path.is_dir())
        self.assertEqual(subprocess.run([*prefix, "branch", "--list", artifact["branch"]],
                                       capture_output=True, text=True, check=True).stdout.strip(), artifact["branch"])

    def test_termination_successor_can_continue_the_preserved_branch(self):
        self.termination_setup()
        self.run_release()
        artifact = self.termination_record["retained_artifacts"][0]
        self.f.args.branch = artifact["branch"]
        self.f.inventory["worktrees"] = [{"branch": artifact["branch"], "path": artifact["worktree"]}]
        self.successor()
        self.assertEqual((Path(artifact["worktree"]) / "unfinished.txt").read_text(), self.uncommitted_source)

    def test_unrelated_detached_worktree_cannot_borrow_the_source_branch(self):
        self.termination_setup()
        artifact = self.termination_record["retained_artifacts"][0]
        prefix = ["git", "-C", artifact["worktree"], "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]
        subprocess.run([*prefix, "commit", "-q", "--allow-empty", "-m", "source"], check=True)
        subprocess.run([*prefix, "checkout", "--quiet", "--orphan", "unrelated"], check=True)
        subprocess.run([*prefix, "commit", "-q", "--allow-empty", "-m", "unrelated"], check=True)
        subprocess.run([*prefix, "checkout", "--quiet", "--detach"], check=True)
        with self.assertRaises(PLAN.PlanError):
            self.run_release()
        self.f.assert_no_writes()

    def test_termination_receipt_must_match_attested_artifacts(self):
        self.termination_setup()
        self.run_release()
        receipt = release.receipt_for(self.f.comments[-1])
        receipt["termination"]["retained_artifacts"] = []
        with self.assertRaises(ValueError):
            release.validate_evidence(receipt, self.evidence, BOT)

    def test_launchplane_missing_exact_release_becomes_claimable(self):
        self.run_release()
        self.successor()

    def test_successor_with_same_worker_keeps_confirmed_release_valid_on_readback(self):
        self.run_release()
        self.f.args.worker = fixtures.OTHER["worker"]

        def date_successor():
            self.f.comments[-1].update(created_at="2026-10-04T00:00:00Z",
                                       updated_at="2026-10-04T00:00:00Z")

        self.f.after_post = date_successor
        self.successor()
        result = self.f.emitted.call_args.args[0]
        self.assertEqual(result["claim"]["worker"], fixtures.OTHER["worker"])
        self.assertEqual(result["claim"]["session"], fixtures.OWNER["session"])
        self.assertEqual(result["claim"]["branch"], fixtures.OWNER["branch"])
        # Resume the same claim to exercise the next preflight as well.
        posts = self.f.events.count("post")
        self.successor()
        self.assertEqual(self.f.events.count("post"), posts)

    def test_released_sessions_later_claim_invalidates_release_and_blocks_successor(self):
        for changes in ({}, {"branch": "work/renewed-42"}, {"worker": "renewed-worker"}):
            with self.subTest(changes=changes):
                self.setUp()
                self.run_release()
                renewed = {**fixtures.OTHER, **changes}
                self.f.comments.append(self.comment(3,
                    f"Claimed by {renewed['worker']}\n\n" + CLAIM.marker(renewed),
                    "2026-10-04T00:00:00Z"))
                with self.assertRaisesRegex(ValueError, "ownership activity"):
                    release._check_activity(self.f.comments, self.f.comments[0],
                                            release.stamp("2026-10-02T00:00:00Z"),
                                            release.source_record(self.f.comments[0]))
                posts = self.f.events.count("post")
                with self.assertRaisesRegex(PLAN.PlanError, "ownership evidence"):
                    self.successor()
                self.assertEqual(self.f.events.count("post"), posts)
                effective = release.effective_comments(self.f.comments)
                conflicts, _ = CLAIM.discussion_evidence("", effective, fixtures.OWNER)
                self.assertIn(self.f.comments[0]["id"], [c["id"] for c in conflicts])

    def test_unstructured_followup_still_invalidates_structured_release(self):
        self.run_release()
        self.f.comments.append(self.comment(3, f"Claimed by {fixtures.OTHER['worker']}",
                                           "2026-10-04T00:00:00Z"))
        with self.assertRaisesRegex(ValueError, "ownership activity"):
            release._check_activity(self.f.comments, self.f.comments[0],
                                    release.stamp("2026-10-02T00:00:00Z"),
                                    release.source_record(self.f.comments[0]))

    def test_release_closed_issue_without_reopening_or_enabling_new_claim(self):
        self.f.issue["state"] = "closed"
        self.run_release()
        self.assertTrue(self.f.emitted.call_args.args[0]["ok"])
        self.assertEqual(self.f.issue["state"], "closed")
        with self.assertRaisesRegex(PLAN.PlanError, "open issue"):
            self.f.run_claim()

    def test_related_followup_session_must_match_source_closure(self):
        for field in ("Session: {}", "**Session:** `{}`", "> Session: {}", "Session: {}.", "Session: {} (capacity work)", "Session ID: {}", "Native session ID: {}", "session={}", "session_id: {}", "sessionId: {}", "Session-ID: {}", "Thread ID: {}", "Session — {}", "| Session | {} |", "Session: {}; PR #99", "(Session: {})"):
            for session in ("session-b", "different-native-session"):
                with self.subTest(field=field, session=session):
                    self.setUp()
                    self.f.comments.append(self.comment(2, "Claimed by trial-b\n" + field.format(session),
                                                        "2026-10-01T01:00:00Z"))
                    self.args.related_claim_comment = [2]
                    if session == "session-b":
                        self.run_release()
                        self.successor()
                    else:
                        with self.assertRaisesRegex(PLAN.PlanError, "different native session"):
                            self.run_release()
                        self.f.assert_no_writes()

    def test_related_followup_prose_is_not_a_session_field(self):
        for prose in ("session-start hook", "session-scoped work", "thread-safe cleanup",
                      "Review thread: https://github.com/owner/repo/pull/99"):
            with self.subTest(prose=prose):
                self.setUp()
                self.f.comments.append(self.comment(2, "Claimed by trial-b to fix " + prose,
                                                    "2026-10-01T01:00:00Z"))
                self.args.related_claim_comment = [2]
                self.run_release()
                self.successor()

    def test_related_source_followup_ignores_fenced_session_examples(self):
        for fence in ("```", "~~~"):
            with self.subTest(fence=fence):
                self.setUp()
                self.f.comments.append(self.comment(2,
                    f"Claimed by trial-b\nSession: session-b\n{fence}\nSession: example-session\n{fence}",
                    "2026-10-01T01:00:00Z"))
                self.args.related_claim_comment = [2]
                self.run_release()
                self.successor()

    def test_source_author_can_release_distinct_followup_by_exact_id(self):
        self.f.comments.append(self.comment(2, "Claimed by trial-b\nSession: distinct-session",
                                           "2026-10-01T01:00:00Z"))
        self.f.comments.append(self.comment(3, "Released claim 2", "2026-10-01T02:00:00Z"))
        self.run_release()
        self.successor()

    def refresh_setup(self):
        self.f.refresh_fixture()
        # The produced canonical release is the visible retained-PR handoff
        # under test; an earlier worker handoff must not attest both siblings.
        self.f.comments[2]["body"] = "Released claim 1"
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
        self.successor()

    def test_codex_release_without_pr_attestation_recovers_with_explicit_retained_prs(self):
        self.refresh_setup()
        self.f.comments[2]["body"] = "Released claim 1\nNo PR attestation on this canonical issue."
        self.run_release()
        self.successor()

    def test_retained_pr_contextual_link_remains_valid_for_release(self):
        self.refresh_setup()
        self.f.pulls[0]["body"] = "Planning issue: https://github.com/owner/repo/issues/42"
        self.run_release()
        self.successor()

    def test_repairshopr_embedded_release_is_claimable_without_supervisor(self):
        self.f.comments.append(self.comment(2, "Owner question: verify read-only usage?\n\nReleased claim 1", "2026-10-01T01:00:00Z"))
        self.successor()

    def test_bd_closed_native_blocker_is_accepted_without_unlinking_history(self):
        self.f.blockers = [{"number": 1165, "state": "closed"}]
        self.run_release()
        self.successor()
        self.assertEqual(self.f.blockers[0]["state"], "closed")

    def test_open_native_blocker_still_refuses(self):
        self.f.blockers = [{"number": 1165, "state": "open"}]
        self.run_release()
        self.f.events.clear()
        with self.assertRaises(PLAN.PlanError) as caught:
            self.successor()
        self.assertEqual(caught.exception.code, "claim_native_blockers_unresolved")
        self.f.assert_no_writes()

    def test_refusal_shapes_do_not_write(self):
        for mode in ("source_edit", "renewed_claim", "wrong_evidence_session", "missing_ended_at",
                     "foreign_evidence", "foreign_source", "unconfirmed", "releaser_is_source", "unknown_related", "unrelated_followup", "future_ended_at"):
            with self.subTest(mode=mode):
                self.setUp()
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
        with self.assertRaisesRegex(PLAN.PlanError, "ownership evidence"):
            self.successor()

    def test_next_keeps_closed_blocker_history_without_excluding_for_open_dependency(self):
        issue = {**self.f.issue, "repo": "owner/repo"}
        relationships = {"blocked_by": [{"number": 1165, "state": "closed"}], "blocking": [], "sub_issues": []}
        state, result = PLAN.evaluate_next_plan(issue, config=PLAN.DEFAULT_CONFIG, focus=None, relationships=relationships)
        self.assertEqual(state, "candidate")
        self.assertNotIn("exclusion", result)
        self.assertEqual(relationships["blocked_by"][0]["number"], 1165)

    def test_corrected_attestation_recovers_without_deleting_stale_release(self):
        self.run_release()
        self.evidence["updated_at"] = "2026-10-04T00:00:00Z"
        self.run_release()
        self.successor()

    def test_unlisted_source_branch_pr_with_new_commit_activity_refuses(self):
        self.refresh_setup()
        self.f.pulls[0]["head"]["ref"] = CLAIM.records(self.f.comments[0]["body"])[0]["branch"]
        self.args.retained_pr = []
        self.commit_date = "2026-10-02T01:00:00Z"
        with self.assertRaisesRegex(PLAN.PlanError, "commit activity after") as caught:
            self.run_release()
        self.assertNotEqual(caught.exception.payload.get("release_refusal_scope"), release.ISSUE_REFUSAL_SCOPE)
        self.f.assert_no_writes()

    def test_issue_refusal_cannot_mask_post_closure_commit_activity(self):
        self.refresh_setup()
        self.f.pulls[0]["head"]["ref"] = fixtures.OTHER["branch"]
        self.f.pulls[0]["body"] = "See #42"
        self.commit_date = "2026-10-02T01:00:00Z"
        with self.assertRaisesRegex(PLAN.PlanError, "commit activity after") as caught:
            self.run_release()
        self.assertNotEqual(caught.exception.payload.get("release_refusal_scope"), release.ISSUE_REFUSAL_SCOPE)
        self.f.assert_no_writes()

    def test_untrusted_or_example_receipt_does_not_poison_claims(self):
        for text in ("<!-- " + release.RELEASE_MARKER + "bad -->", "```\n<!-- " + release.RELEASE_MARKER + "bad -->\n```"):
            with self.subTest(text=text):
                self.setUp(); self.run_release()
                self.f.comments.append(self.comment(9, text, "2026-10-03T01:00:00Z"))
                self.successor()

    def test_missing_evidence_restores_source_conflict_without_permanent_poison(self):
        self.run_release()
        self.evidence_failure = 404
        with self.assertRaisesRegex(PLAN.PlanError, "ownership evidence"):
            self.successor()
        self.evidence_failure = None
        self.successor()

    def test_source_can_resume_after_an_invalidated_attestation(self):
        self.run_release()
        self.evidence["updated_at"] = "2026-10-04T00:00:00Z"
        for key in ("worker", "session", "branch"):
            setattr(self.f.args, key, fixtures.OTHER[key])
        with patch.object(self.f, "read_api", side_effect=self.api):
            self.f.run_claim()
        self.assertEqual(self.f.emitted.call_args.args[0]["claim"]["session"], fixtures.OTHER["session"])

    def test_evidence_location_timestamp_and_new_related_comment_refuse(self):
        for mode in ("location", "timezone", "new_related", "closed_unmerged"):
            with self.subTest(mode=mode):
                self.setUp()
                if mode == "location": self.evidence["issue_url"] = "https://api.github.com/repos/owner/control/issues/885"
                if mode == "timezone": self.evidence["body"] = self.evidence["body"].replace("2026-10-02T00:00:00Z", "2026-10-02T00:00:00")
                if mode == "new_related":
                    self.f.comments.append(self.comment(2, "Claimed by trial-b capacity work", "2026-10-02T01:00:00Z"))
                    self.args.related_claim_comment = [2]
                if mode == "closed_unmerged":
                    self.refresh_setup(); self.f.pulls[0]["state"] = "closed"
                with self.assertRaises(PLAN.PlanError): self.run_release()
                self.f.assert_no_writes()

    def test_successor_push_does_not_resurrect_the_abandoned_owner(self):
        self.refresh_setup(); self.run_release()
        self.successor()
        successor_id = self.f.comments[-1]["id"]
        self.commit_date = "2026-10-04T00:00:00Z"
        self.f.pulls[0]["head"]["sha"] = "fixture-successor-head"
        self.f.comments.append(self.comment(successor_id + 1, f"Released claim {successor_id}", "2026-10-05T00:00:00Z"))
        self.f.issue["body"] = PLAN.PLAN_MANAGED_PROVENANCE_MARKER + "\n\n## Current Status\n\nState: Released handoff."
        self.f.args.worker = "next-successor"
        self.f.args.session = "next-native-session"
        self.f.args.branch = "work/follow-on-42"
        self.successor()
        self.assertEqual(self.f.emitted.call_args.args[0]["claim"]["session"], "next-native-session")

    def test_hidden_checked_pr_is_not_a_visible_refresh_handoff(self):
        self.refresh_setup()
        self.f.pulls[0]["head"]["ref"] = CLAIM.records(self.f.comments[0]["body"])[0]["branch"]
        self.args.retained_pr = [self.args.retained_pr[1]]
        self.run_release()
        before = copy.deepcopy((self.f.comments, self.f.issue, self.f.inventory))
        with self.assertRaises(PLAN.PlanError): self.successor()
        self.assertEqual(before, (self.f.comments, self.f.issue, self.f.inventory))

    def test_dry_run_is_reviewable_and_writes_nothing(self):
        self.args.dry_run = True
        self.run_release()
        self.f.assert_no_writes()
        self.assertEqual(CLAIM.released_claim_id(self.f.emitted.call_args.args[0]["release_body"]), 1)

    def test_generated_release_preserves_conditional_words_in_source_identity(self):
        for identity in (
            {"branch": "work/refresh-after-deploy"},
            {"branch": "work/wait-until-ci", "worker": "trial-pending-review"},
        ):
            with self.subTest(identity=identity):
                self.setUp()
                source = {**fixtures.OTHER, **identity}
                self.f.comments[0]["body"] = CLAIM.marker(source)
                self.run_release()
                self.successor()
                self.assertTrue(self.f.emitted.call_args.args[0]["ok"])

    def test_source_edit_invalidates_existing_release_at_successor_claim(self):
        self.run_release()
        self.f.comments[0]["updated_at"] = "2026-10-04T00:00:00Z"
        with self.assertRaisesRegex(PLAN.PlanError, "ownership evidence"):
            self.f.args.resume_from = 1
            with patch.object(self.f, "read_api", side_effect=self.api):
                self.f.run_claim()

    def test_post_write_race_is_not_reported_as_success(self):
        self.race = lambda: self.f.comments.append(self.comment(8, CLAIM.marker(fixtures.OTHER), "2026-10-04T00:00:00Z"))
        with self.assertRaisesRegex(PLAN.PlanError, "activity after") as caught: self.run_release()
        self.assertIn("post_release", caught.exception.payload["completed_steps"])
        self.assertEqual(self.f.events.count("post"), 1)

    def test_post_write_resumption_or_evidence_change_leaves_no_usable_release(self):
        for route in ("finished", "terminated"):
            for mode in ("evidence", *(("process",) if route == "terminated" else ())):
                with self.subTest(route=route, mode=mode):
                    self.setUp()
                    if route == "terminated":
                        self.termination_setup()
                    def resume():
                        if mode == "process":
                            release.termination.inventory.return_value = [(12345, "other", "codex")]
                        else:
                            self.evidence["updated_at"] = "2026-10-04T00:00:00Z"
                    self.race = resume
                    with self.assertRaises(PLAN.PlanError) as caught:
                        self.run_release()
                    posted = self.f.comments[-1]
                    self.assertEqual(caught.exception.payload["release_comment"]["id"], posted["id"])
                    self.assertIsNone(release.receipt_for(posted))
                    self.assertIsNone(CLAIM.released_claim_id(posted["body"]))
                    self.assertNotIn("confirm_release", self.f.events)
                    # A transient process disappearing must not revive the failed release.
                    if mode == "process":
                        release.termination.inventory.return_value = []
                    self.assert_source_still_owned()
                    posts = self.f.events.count("post")
                    self.f.args.resume_from = self.args.claim_comment
                    with patch.object(self.f, "read_api", side_effect=self.api), self.assertRaises(PLAN.PlanError):
                        self.f.run_claim()
                    self.assertEqual(self.f.events.count("post"), posts)
                    if route == "terminated":
                        artifact = self.termination_record["retained_artifacts"][0]
                        self.assertEqual((Path(artifact["worktree"]) / "unfinished.txt").read_text(), self.uncommitted_source)

    def assert_source_still_owned(self):
        effective = release.effective_comments(self.f.comments)
        conflicts, _ = CLAIM.discussion_evidence("", effective, fixtures.OWNER)
        self.assertIn(self.args.claim_comment, [c["id"] for c in conflicts])

    def test_pending_release_preserves_sibling_claim_and_recorded_wait(self):
        sibling = self.comment(7, CLAIM.marker({**fixtures.OTHER, "session": "sibling-session"}), "2026-10-01T01:00:00Z")
        self.f.comments.append(sibling)
        self.f.wait = fixtures.wait_record.reviewed(
            evidence="Owner acceptance pending", waiting_for="Owner test", ends_when="Owner accepts")
        original_wait = copy.deepcopy(self.f.wait)
        self.race = lambda: self.evidence.update(updated_at="2026-10-04T00:00:00Z")
        with self.assertRaises(PLAN.PlanError):
            self.run_release()
        effective = release.effective_comments(self.f.comments)
        conflicts, _ = CLAIM.discussion_evidence("", effective, fixtures.OWNER)
        self.assertEqual({c["id"] for c in conflicts}, {self.args.claim_comment, sibling["id"]})
        with patch.object(PLAN, "wait_authors", return_value={BOT}):
            observed_wait = PLAN.current_wait(self.f.issue, [
                {"user": {"login": BOT}, "body": fixtures.wait_record.marker(self.f.wait)}, *effective], "owner/repo")
        self.assertEqual(observed_wait, original_wait)

    def test_confirmed_release_rerun_reuses_exact_receipt_without_new_writes(self):
        self.run_release()
        result = self.f.emitted.call_args.args[0]
        writes = (self.f.events.count("post"), self.f.events.count("confirm_release"))
        self.args.session = "successor-supervisor-session"
        self.run_release()
        self.assertEqual(self.f.emitted.call_args.args[0]["release_comment"]["id"], result["release_comment"]["id"])
        self.assertEqual((self.f.events.count("post"), self.f.events.count("confirm_release")), writes)
        self.args.dry_run = True
        self.run_release()
        self.assertEqual(self.f.emitted.call_args.args[0]["release_comment"]["id"], result["release_comment"]["id"])
        self.assertEqual((self.f.events.count("post"), self.f.events.count("confirm_release")), writes)

    def test_failed_pending_attempt_requires_fresh_closure_evidence_before_recovery(self):
        self.termination_setup()
        self.race = lambda: release.termination.inventory.configure_mock(return_value=[(12345, "other", "codex")])
        with self.assertRaises(PLAN.PlanError):
            self.run_release()
        writes = (self.f.events.count("post"), self.f.events.count("confirm_release"))
        self.race = lambda: None
        release.termination.inventory.return_value = []
        with self.assertRaises(PLAN.PlanError):
            self.run_release()
        self.assertEqual((self.f.events.count("post"), self.f.events.count("confirm_release")), writes)
        self.assert_source_still_owned()
        self.evidence["id"] = 89
        self.args.evidence_comment = self.args.evidence_comment.rsplit("-", 1)[0] + "-89"
        fresh_now = release.stamp("2026-10-04T01:00:00Z")
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return fresh_now.astimezone(tz or timezone.utc)
        with patch.object(release.termination, "datetime", Clock):
            self.evidence["body"] = release.termination.body(release.termination.verify(self.termination_record, "session-b"))
            self.evidence.update(created_at=fresh_now.isoformat(), updated_at=fresh_now.isoformat())
            self.run_release()
        self.successor()

    def test_related_recovery_reuses_source_when_observed_branch_pr_has_closed(self):
        self.f.comments.append(self.comment(2, "Claimed by trial-b to finish source work", "2026-10-01T01:00:00Z"))
        self.args.related_claim_comment = [2]
        self.f.pulls = [{"number": 99, "state": "open", "user": {"login": BOT}, "body": "Refs #42",
                         "head": {"ref": fixtures.OTHER["branch"], "sha": "source-head", "repo": {"full_name": "owner/repo"}},
                         "base": {"repo": {"full_name": "owner/repo"}}}]
        self.related_failure = github_comment.CommentError("Related write rejected", failure=PLAN.github_api_core.FailureDetail(
            cause="rejected", message="Related write rejected", retryable=False,
            fallback_eligible=False, disposition="stop", write_outcome="rejected"))
        with self.assertRaises(PLAN.PlanError) as caught:
            self.run_release()
        source_release = caught.exception.payload["release_comment"]["id"]
        release.validate_evidence(release.receipt_for(self.f.comments[-1]), self.evidence, BOT)
        # GitHub's open-PR observation changes; the requested handoff is unchanged.
        self.f.pulls = []
        self.related_failure = None
        posts = self.f.events.count("post")
        self.run_release()
        self.assertEqual(self.f.emitted.call_args.args[0]["release_comment"]["id"], source_release)
        self.assertEqual(self.f.events.count("post"), posts + 1)
        self.successor()

    def test_uncertain_confirmation_preserves_exact_record_and_never_replays(self):
        for route in ("finished", "terminated"):
            for applied in (False, True):
                with self.subTest(route=route, applied=applied):
                    self.setUp()
                    if route == "terminated":
                        self.termination_setup()
                    self.confirmation_applied = applied
                    self.confirmation_failure = github_comment.CommentError(
                        "Confirmation response lost", failure=PLAN.github_api_core.FailureDetail(
                            cause="transport", message="Response lost", retryable=False,
                            fallback_eligible=False, disposition="stop", write_outcome="unknown"),
                        payload={"outcome_certainty": "unknown"})
                    with self.assertRaises(PLAN.PlanError) as caught:
                        self.run_release()
                    self.assertEqual(self.f.events.count("post"), 1)
                    self.assertEqual(self.f.events.count("confirm_release"), 1)
                    posted = self.f.comments[-1]
                    self.assertEqual(caught.exception.payload["release_comment"]["id"], posted["id"])
                    self.assertEqual(caught.exception.payload["outcome_certainty"], "unknown")
                    self.assertTrue(caught.exception.payload["release_recovery_action"])
                    if applied:
                        release.validate_evidence(release.receipt_for(posted), self.evidence, BOT)
                    else:
                        self.assertIsNone(release.receipt_for(posted))
                        self.assert_source_still_owned()
                    # Fresh readback recovers an applied confirmation; an inert
                    # attempt cannot silently revive under the old evidence.
                    self.confirmation_failure = None
                    writes = (self.f.events.count("post"), self.f.events.count("confirm_release"))
                    if applied:
                        self.run_release()
                        self.assertTrue(self.f.emitted.call_args.args[0]["ok"])
                    else:
                        with self.assertRaises(PLAN.PlanError):
                            self.run_release()
                    self.assertEqual((self.f.events.count("post"), self.f.events.count("confirm_release")), writes)

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
                    self.run_release()
                    with self.assertRaises(PLAN.PlanError): self.successor()
                else:
                    with self.assertRaises(PLAN.PlanError): self.run_release()
                    self.f.assert_no_writes()


class LegacyReleaseTests(ReleaseHarness):
    def api(self, method, path, **kwargs):
        if path.endswith('/pulls/99'):
            return 'bot', copy.deepcopy(self.f.closed_pulls[99])
        if path.endswith('/commits/historical-head'):
            return 'bot', {'commit': {'committer': {'date': self.historical_commit_date}}}
        return super().api(method, path, **kwargs)

    def read_pages(self, path, **kwargs):
        actor, items = self.original_read_pages(path, **kwargs)
        if path.endswith('/pulls'):
            query = kwargs.get('query') or {}
            if query.get('head'):
                branch = query['head'].split(':', 1)[1]
                items = [p for p in items if p['head']['ref'] == branch]
            if query.get('state') == 'open':
                items = [p for p in items if p['state'] == 'open']
        return actor, items

    def legacy_setup(self, index=0):
        self.setUp()
        historical = json.loads(Path(__file__).with_name("fixtures").joinpath(
            "codex-skills-828-legacy-claims.json").read_text())
        source = historical[index]
        self.original_read_pages = self.f.read_pages
        self.f.read_pages = self.read_pages
        self.f.comments = [self.comment(source["id"], source["body"], source["created_at"])]
        self.args.claim_comment = source["id"]
        self.args.legacy = True
        self.args.legacy_branch = "work/go828-install-d0"
        self.args.legacy_reason = "The original bare claim recorded no native session ID"
        worker = source["body"].splitlines()[0].removeprefix("Claimed by ")
        self.evidence["body"] = (
            f"Closed legacy claim {source['id']}\nWorker: {worker}\n"
            f"Source branch: {self.args.legacy_branch}\n"
            f"Native session unavailable: {self.args.legacy_reason}\n"
            "Ended at: 2026-10-02T00:00:00Z\nSafe to exit: yes\n"
            "Supervisor verified closure from the handoff and checked all reachable hosts."
        )
        self.f.closed_pulls[99] = {"number": 99, "state": "closed", "merged_at": "2026-10-01T23:00:00Z",
            "user": {"login": BOT}, "body": "Closes #42", "title": "Legacy task",
            "head": {"ref": self.args.legacy_branch, "sha": "legacy-source-head", "repo": {"full_name": "owner/repo"}},
            "base": {"repo": {"full_name": "owner/repo"}}}
        self.f.pulls = [self.f.closed_pulls[99]]
        self.historical_commit_date = '2026-10-01T20:00:00Z'
        return historical, worker

    def test_both_historical_claims_require_explicit_flag_and_release_only_exact_id(self):
        for index in (0, 1):
            with self.subTest(index=index):
                historical, _ = self.legacy_setup(index)
                self.args.legacy = False
                with self.assertRaises(PLAN.PlanError): self.run_release()
                self.f.assert_no_writes()
                self.args.legacy = True
                other = historical[1-index]
                self.f.comments.append(self.comment(other['id'], other['body'], other['created_at']))
                self.run_release()
                effective = release.effective_comments(self.f.comments)
                conflicts, _ = CLAIM.discussion_evidence("", effective, fixtures.OWNER)
                self.assertEqual([c['id'] for c in conflicts], [other['id']])
                receipt = release.receipt_for(self.f.comments[-1])
                self.assertEqual(receipt['source']['legacy_reason'], self.args.legacy_reason)
                canonical = release.verified_comments(self.f.comments, lambda *_: self.evidence)
                self.assertTrue(any(release.receipt_for(c) for c in canonical))
                self.assertEqual(release._released_source_contexts(canonical), ())
                # An ordinary successor needs no fabricated resume session.
                self.f.comments.pop(-2)
                with patch.object(self.f, "read_api", side_effect=self.api): self.f.run_claim()
                self.assertTrue(self.f.emitted.call_args.args[0]['ok'])

    def test_original_attestation_requires_replacement(self):
        historical, _ = self.legacy_setup()
        self.evidence['body'] = historical[2]['body']
        with self.assertRaises(PLAN.PlanError): self.run_release()
        self.f.assert_no_writes()

    def test_legacy_cannot_reinterpret_structured_or_session_bearing_claim(self):
        for body in (CLAIM.marker(fixtures.OTHER), "Claimed by trial-b\nSession: session-b",
                     "Claimed by trial-b\n<!-- github-plan:claim broken -->", "> Claimed by trial-b"):
            self.legacy_setup()
            self.f.comments[0]['body'] = body
            with self.assertRaises(PLAN.PlanError): self.run_release()
            self.f.assert_no_writes()

    def test_legacy_guards_refuse_before_writes(self):
        for mode in ('branch', 'reason', 'worker', 'evidence_author', 'source_author', 'future',
                     'unknown_end', 'unconfirmed', 'source_edit', 'renewed', 'structured_renewed',
                     'branch_activity', 'related', 'retained_pr', 'wrong_branch'):
            with self.subTest(mode=mode):
                _, worker = self.legacy_setup()
                if mode == 'branch': self.args.legacy_branch = None
                if mode == 'reason': self.args.legacy_reason = ''
                if mode == 'worker': self.evidence['body'] = self.evidence['body'].replace(worker, 'another-worker')
                if mode == 'evidence_author': self.evidence['user']['login'] = 'stranger'
                if mode == 'source_author': self.f.comments[0]['user']['login'] = 'stranger'
                if mode == 'future': self.evidence['body'] = self.evidence['body'].replace('2026-10-02T00:00:00Z', '2026-10-03T00:00:00Z')
                if mode == 'unknown_end': self.evidence['body'] = self.evidence['body'].replace('2026-10-02T00:00:00Z', 'unknown')
                if mode == 'unconfirmed': self.args.confirm_session_ended = False
                if mode == 'source_edit': self.f.comments[0]['updated_at'] = '2026-10-04T00:00:00Z'
                if mode == 'renewed': self.f.comments.append(self.comment(2, f'**Claimed by {worker}**', '2026-10-04T00:00:00Z'))
                if mode == 'structured_renewed': self.f.comments.append(self.comment(2, CLAIM.marker({**fixtures.OTHER, 'worker': worker}), '2026-10-04T00:00:00Z'))
                if mode == 'branch_activity': self.source_branch_date = '2026-10-04T00:00:00Z'
                if mode == 'related':
                    self.f.comments.append(self.comment(2, f'Claimed by {worker}', '2026-10-01T02:00:00Z'))
                    self.args.related_claim_comment = [2]
                if mode == 'retained_pr': self.args.retained_pr = ['https://github.com/owner/repo/pull/99']
                if mode == 'wrong_branch':
                    self.evidence['body'] = self.evidence['body'].replace(self.args.legacy_branch, 'work/typo')
                    self.args.legacy_branch = 'work/typo'
                with self.assertRaises(PLAN.PlanError): self.run_release()
                self.f.assert_no_writes()

    def test_reused_legacy_worker_refuses_successor_before_posting(self):
        for lower in (False, True):
            with self.subTest(lower=lower):
                _, worker = self.legacy_setup()
                self.run_release()
                posted = self.f.events.count('post')
                self.f.args.worker = worker.lower() if lower else worker
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    with patch.object(self.f, 'read_api', side_effect=self.api): self.f.run_claim()
                self.assertEqual(caught.exception.code, 'claim_conflict')
                self.assertEqual(self.f.events.count('post'), posted)
                self.assertTrue(CLAIM.released_claim_id(release.effective_comments(self.f.comments)[-1]['body']))

    def test_legacy_pr_claim_refuses_before_writing(self):
        self.legacy_setup()
        self.f.issue['pull_request'] = {'url': 'https://api.github.com/repos/owner/repo/pulls/42'}
        with self.assertRaises(PLAN.PlanError): self.run_release()
        self.f.assert_no_writes()

    def test_closed_unmerged_source_pr_still_checks_activity(self):
        for renewed in (False, True):
            with self.subTest(renewed=renewed):
                self.legacy_setup()
                self.f.pulls[0]['merged_at'] = None
                if renewed: self.commit_date = '2026-10-04T00:00:00Z'
                if renewed:
                    with self.assertRaises(PLAN.PlanError): self.run_release()
                    self.f.assert_no_writes()
                else:
                    self.run_release()
                    self.assertTrue(self.f.emitted.call_args.args[0]['ok'])

    def test_reused_branch_history_preserves_activity_and_open_work_guards(self):
        for mode in ('closed_history', 'newer_history', 'open_unrelated'):
            with self.subTest(mode=mode):
                self.legacy_setup()
                historical = copy.deepcopy(self.f.closed_pulls[99])
                historical.update(number=100, body='Closes #999', title='Earlier unrelated task')
                historical['head']['sha'] = 'historical-head'
                self.f.pulls.append(historical)
                if mode == 'newer_history': self.historical_commit_date = '2026-10-04T00:00:00Z'
                if mode == 'open_unrelated': historical['state'] = 'open'
                if mode == 'closed_history':
                    self.run_release()
                    self.assertTrue(self.f.emitted.call_args.args[0]['ok'])
                else:
                    with self.assertRaises(PLAN.PlanError): self.run_release()
                    self.f.assert_no_writes()

    def test_reason_with_arrow_remains_inside_parseable_receipt(self):
        self.legacy_setup()
        previous_reason = self.args.legacy_reason
        self.args.legacy_reason += ' --> metadata absent'
        self.evidence['body'] = self.evidence['body'].replace(previous_reason, self.args.legacy_reason)
        self.run_release()
        body = self.f.comments[-1]['body']
        receipt_line = next(line for line in body.splitlines() if line.startswith('<!-- ' + release.RELEASE_MARKER))
        self.assertEqual(receipt_line.count('-->'), 1)
        self.assertEqual(release.receipt_for(self.f.comments[-1])['source']['legacy_reason'], self.args.legacy_reason)

    def test_legacy_branch_proof_and_retained_activity_are_required(self):
        for mode in ('missing_pr', 'foreign_pr', 'unlinked_pr', 'new_commit'):
            with self.subTest(mode=mode):
                self.legacy_setup()
                if mode == 'missing_pr': self.f.pulls = []
                if mode == 'foreign_pr': self.f.pulls[0]['user']['login'] = 'stranger'
                if mode == 'unlinked_pr': self.f.pulls[0]['body'] = 'Unrelated task'
                if mode == 'new_commit': self.commit_date = '2026-10-04T00:00:00Z'
                with self.assertRaises(PLAN.PlanError): self.run_release()
                self.f.assert_no_writes()

    def test_source_worker_comment_can_name_the_legacy_branch_pr(self):
        cases = {'named before the session ended': ('{worker} handoff: draft PR #99.', '2026-10-01T03:00:00Z', None, True),
                 'pull request URL': ('{worker} landed https://github.com/owner/repo/pull/99', '2026-10-01T03:00:00Z', None, True),
                 'named after the session ended': ('{worker} handoff: draft PR #99.', '2026-10-03T00:00:00Z', None, False),
                 'another author': ('{worker} handoff: draft PR #99.', '2026-10-01T03:00:00Z', 'stranger', False),
                 'longer worker name': ('{worker}B handoff: draft PR #99.', '2026-10-01T03:00:00Z', None, False),
                 'longer PR number': ('{worker} handoff: draft PR #990.', '2026-10-01T03:00:00Z', None, False)}
        cases['open pull request'] = ('{worker} handoff: draft PR #99.', '2026-10-01T03:00:00Z', None, True)
        for name, (body, created_at, author, released) in cases.items():
            with self.subTest(name):
                _, worker = self.legacy_setup()
                self.f.pulls[0].update(body='', title='Legacy task')
                if name == 'open pull request': self.f.pulls[0].update(state='open', merged_at=None)
                named = self.comment(2, body.format(worker=worker), created_at)
                if author: named['user']['login'] = author
                self.f.comments.append(named)
                if released:
                    self.run_release()
                    self.assertTrue(self.f.emitted.call_args.args[0]['ok'])
                else:
                    with self.assertRaises(PLAN.PlanError): self.run_release()
                    self.f.assert_no_writes()

    def test_legacy_receipt_invalidates_on_changed_source_or_evidence(self):
        for mode in ('source', 'evidence', 'missing', 'renewed'):
            self.legacy_setup()
            self.run_release()
            if mode == 'source': self.f.comments[0]['updated_at'] = '2026-10-04T00:00:00Z'
            if mode == 'evidence': self.evidence['updated_at'] = '2026-10-04T00:00:00Z'
            if mode == 'missing': self.evidence_failure = 404
            if mode == 'renewed':
                worker = self.f.comments[0]['body'].splitlines()[0].removeprefix('Claimed by ')
                self.f.comments.append(self.comment(2, f'Claimed by {worker}', '2026-10-04T00:00:00Z'))
            with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                with patch.object(self.f, 'read_api', side_effect=self.api): self.f.run_claim()
            self.assertEqual(caught.exception.code, "claim_conflict")

    def test_legacy_post_write_race_preserves_receipt_and_reports_failure(self):
        _, worker = self.legacy_setup()
        self.race = lambda: self.f.comments.append(self.comment(2, f'Claimed by {worker}', '2026-10-04T00:00:00Z'))
        with self.assertRaises(PLAN.PlanError) as caught: self.run_release()
        self.assertIn('post_release', caught.exception.payload['completed_steps'])
        self.assertEqual(self.f.events.count('post'), 1)

    def test_malformed_legacy_receipt_cannot_poison_ownership_checks(self):
        for reason in (1, ["missing"], {"why": "missing"}, None, ""):
            with self.subTest(reason=reason):
                self.legacy_setup()
                self.run_release()
                receipt = release.receipt_for(self.f.comments[-1])
                receipt['source']['legacy_reason'] = reason
                prefix = self.f.comments[-1]['body'].rsplit('<!-- ' + release.RELEASE_MARKER, 1)[0]
                forged = self.comment(100, prefix + '<!-- ' + release.RELEASE_MARKER
                    + json.dumps(receipt) + ' -->', '2026-10-03T00:00:00Z')
                self.assertIsNone(release.receipt_for(forged))
                effective = release.effective_comments([self.f.comments[0], forged])
                conflicts, _ = CLAIM.discussion_evidence('', effective, fixtures.OWNER)
                self.assertEqual([c['id'] for c in conflicts], [self.args.claim_comment])

    def test_legacy_dry_run_does_not_write(self):
        self.legacy_setup()
        self.args.dry_run = True
        self.run_release()
        self.f.assert_no_writes()
        self.assertEqual(CLAIM.released_claim_id(self.f.emitted.call_args.args[0]['release_body']), self.args.claim_comment)


class OwnReleaseTests(ReleaseHarness):
    """A worker releases its own claim through the helper, never a hand-written directive."""

    def setUp(self):
        super().setUp()
        self.handoff = Path(self.enterContext(tempfile.TemporaryDirectory())) / "handoff.md"
        # LP4214-C1 put this summary on the directive line by hand.
        self.handoff.write_text("LP4214-C1 Finish Line met: reviewed, tested and landed.\n\nSafe to exit: yes\n")
        # The releasing worker runs as the claim's own native session.
        os.environ["CLAUDE_CODE_SESSION_ID"] = fixtures.OTHER["session"]
        self.args = Namespace(repo="owner/repo", issue="42", claim_comment=1, session=fixtures.OTHER["session"],
                              handoff_file=str(self.handoff), agent="claude", dry_run=False)

    def run_own_release(self):
        with patch.multiple(PLAN, default_repo=lambda _: "owner/repo", get_issue=self.f.get_issue,
                            EXPECTED_ACTOR=BOT, collect_paged_rest_items=self.f.read_pages,
                            comment_route=lambda: ("bot", "bot-gh", BOT), emit=self.f.emitted), \
                patch.object(github_identity, "configured_bot_logins", return_value=[BOT]), \
                patch.object(github_comment, "comment", autospec=True, side_effect=self.post):
            PLAN.cmd_release(self.args)

    def test_release_posts_the_exact_directive_with_handoff_below_it(self):
        self.run_own_release()
        result = self.f.emitted.call_args.args[0]
        self.assertTrue(result["ok"])
        posted = self.f.comments[-1]
        self.assertEqual(CLAIM.released_claim_id(posted["body"]), 1)
        self.assertIn("LP4214-C1 Finish Line met", posted["body"])
        self.assertTrue(release.exact_source_release(1, posted, self.f.comments))
        self.assertEqual(result["release_comment"]["id"], posted["id"])
        # A second run finds the confirmed release and writes nothing.
        posts = self.f.events.count("post")
        self.run_own_release()
        self.assertEqual(self.f.events.count("post"), posts)
        self.assertEqual(self.f.emitted.call_args.args[0]["release_state"], "already_released")

    def test_claim_names_the_release_command_for_its_own_comment(self):
        self.f.comments = []
        self.f.run_claim()
        result = self.f.emitted.call_args.args[0]
        argv = shlex.split(result["release_command"])
        parsed = PLAN.build_parser().parse_args(argv[argv.index("--repo"):])
        self.assertIs(parsed.func, PLAN.cmd_release)
        self.assertEqual((parsed.claim_comment, parsed.session),
                         (result["claim_comment"]["comment"]["id"], result["claim"]["session"]))

    def test_claim_comment_is_searchable_by_its_session_outside_markup(self):
        self.f.comments = []
        self.f.run_claim()
        result = self.f.emitted.call_args.args[0]
        posted = next(c for c in self.f.comments if c["id"] == result["claim_comment"]["comment"]["id"])
        self.assertIn(result["claim"]["session"], re.sub(r"<!--.*?-->", "", posted["body"], flags=re.S))

    def test_release_discovers_other_branch_prs_from_source_links(self) -> None:
        self.f.pulls = [
            {"number": 99, "state": "open", "user": {"login": BOT}, "created_at": "2026-10-01T01:00:00Z",
             "body": "Refs #42", "head": {"ref": "work/another-branch", "repo": {"full_name": "owner/repo"}},
             "base": {"repo": {"full_name": "owner/repo"}}, "html_url": "https://github.com/owner/repo/pull/99"},
        ]
        cross_repo = copy.deepcopy(self.f.pulls[0])
        cross_repo.update(number=77, body="Refs owner/repo#42", html_url="https://github.com/owner/other/pull/77")
        for side in ("head", "base"):
            cross_repo[side]["repo"]["full_name"] = "owner/other"
        self.f.closed_pulls[77] = cross_repo
        self.f.timelines["/repos/owner/repo/issues/42/timeline"] = [
            {"event": "cross-referenced", "source": {"issue": {
                "number": 77, "user": {"login": BOT}, "created_at": cross_repo["created_at"],
                "pull_request": {"url": "https://api.github.com/repos/owner/other/pulls/77"}}}},
        ]
        with patch.object(PLAN, "api_json", side_effect=self.f.read_api):
            with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                self.run_own_release()
            self.assertEqual(caught.exception.code, "release_pr_attribution_required")
            self.f.assert_no_writes()
            self.args.retained_pr = [self.f.pulls[0]["html_url"], cross_repo["html_url"]]
            self.run_own_release()
        named = self.f.comments[-1]["body"]
        for pull in [*self.f.pulls, cross_repo]:
            self.assertIn(pull["html_url"], named)

    def test_release_does_not_name_unrelated_foreign_or_preclaim_prs(self) -> None:
        self.f.pulls = [
            {"number": n, "state": "open", "user": {"login": author}, "created_at": created,
             "body": body, "head": {"ref": "work/other", "repo": {"full_name": "owner/repo"}},
             "base": {"repo": {"full_name": "owner/repo"}}, "html_url": f"https://github.com/owner/repo/pull/{n}"}
            for n, author, created, body in [
                (98, BOT, "2026-10-01T01:00:00Z", "Unrelated"),
                (99, "someone-else", "2026-10-01T01:00:00Z", "Refs #42"),
                (100, BOT, "2026-09-30T01:00:00Z", "Refs #42"),
            ]
        ]
        self.run_own_release()
        for pull in self.f.pulls:
            self.assertNotIn(pull["html_url"], self.f.comments[-1]["body"])

    def test_release_leaves_the_merge_train_batch_to_the_train(self) -> None:
        self.f.pulls = [{"number": 101, "state": "open", "user": {"login": BOT}, "created_at": "2026-10-01T01:00:00Z",
                         "body": CLAIM.TRAIN_BATCH_MARKER + "batch:sha -->\n\nCloses #42",
                         "head": {"ref": CLAIM.TRAIN_BRANCH_PREFIX + "owner/repo/main/batch", "repo": {"full_name": "owner/repo"}},
                         "base": {"repo": {"full_name": "owner/repo"}}, "html_url": "https://github.com/owner/repo/pull/101"}]
        self.run_own_release()
        self.assertNotIn(self.f.pulls[0]["html_url"], self.f.comments[-1]["body"])

    def test_release_excludes_other_sessions_prs_without_transferring_ownership(self) -> None:
        url = "https://github.com/owner/repo/pull/99"
        self.f.pulls = [{"number": 99, "state": "open", "user": {"login": BOT},
            "created_at": "2026-10-01T01:00:00Z", "body": "Parent: https://github.com/owner/repo/issues/42\nCloses #43",
            "head": {"ref": "work/other-session", "repo": {"full_name": "owner/repo"}},
            "base": {"repo": {"full_name": "owner/repo"}}}]
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_own_release()
        self.assertEqual(caught.exception.code, "release_pr_attribution_required")
        self.f.assert_no_writes()
        self.args.exclude_pr = [url]
        self.run_own_release()
        self.assertNotIn(99, CLAIM.handoff_pr_numbers(self.f.comments[-1]["body"], issue_repo="owner/repo", target_repo="owner/repo"))

    def test_release_never_uses_incidental_narrative_as_pr_attribution(self) -> None:
        for created in ("2026-10-01T01:00:00Z", "2026-09-29T00:00:00Z"):
            with self.subTest(created=created):
                self.setUp()
                url = "https://github.com/owner/repo/pull/99"
                self.f.pulls = [{"number": 99, "state": "open", "user": {"login": BOT}, "created_at": created,
                    "body": "Refs #42\nCloses #43", "head": {"ref": "work/other-session", "repo": {"full_name": "owner/repo"}},
                    "base": {"repo": {"full_name": "owner/repo"}}}]
                self.handoff.write_text("Left #99 alone. It belongs to another session.")
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_own_release()
                self.assertEqual(caught.exception.code, "release_pr_attribution_required")
                self.f.assert_no_writes()
                self.args.exclude_pr = ["https://github.com/Owner/Repo/pull/99"]
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_own_release()
                self.assertEqual(caught.exception.code, "release_excluded_pr_mentioned")
                self.f.assert_no_writes()
                self.handoff.write_text("Other session's work remains with its owner.")
                self.run_own_release()
                self.assertNotIn(99, CLAIM.handoff_pr_numbers(self.f.comments[-1]["body"], issue_repo="owner/repo", target_repo="owner/repo"))

    def test_release_accepts_case_varied_explicit_attribution(self) -> None:
        self.f.pulls = [{"number": 99, "state": "open", "user": {"login": BOT}, "created_at": "2026-10-01T01:00:00Z",
            "body": "Refs #42", "head": {"ref": "work/other", "repo": {"full_name": "Owner/Repo"}},
            "base": {"repo": {"full_name": "Owner/Repo"}}}]
        self.args.retained_pr = ["https://github.com/owner/repo/pull/99"]
        self.run_own_release()
        self.assertIn(99, CLAIM.handoff_pr_numbers(self.f.comments[-1]["body"], issue_repo="owner/repo", target_repo="owner/repo"))

    def test_release_checks_older_cross_repository_narrative_references(self) -> None:
        self.handoff.write_text("Left owner/other#77 alone.")
        self.f.closed_pulls[77] = {"number": 77, "state": "open", "user": {"login": BOT},
            "created_at": "2026-09-29T00:00:00Z", "body": "Refs owner/repo#42",
            "head": {"ref": "work/other-session", "repo": {"full_name": "owner/other"}},
            "base": {"repo": {"full_name": "owner/other"}}}
        self.f.timelines["/repos/owner/repo/issues/42/timeline"] = [{"event": "cross-referenced", "source": {"issue": {
            "state": "open", "created_at": "2026-09-29T00:00:00Z", "user": {"login": BOT},
            "pull_request": {"url": "https://api.github.com/repos/owner/other/pulls/77"}}}}]
        with patch.object(PLAN, "api_json", side_effect=self.f.read_api):
            with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                self.run_own_release()
        self.assertEqual(caught.exception.code, "release_pr_attribution_required")
        self.f.assert_no_writes()
        self.args.retained_pr = ["https://github.com/Owner/Other/pull/77"]
        self.handoff.write_text("Handing off the explicitly declared PR.")
        with patch.object(PLAN, "api_json", side_effect=self.f.read_api):
            self.run_own_release()
        self.assertIn(77, CLAIM.handoff_pr_numbers(self.f.comments[-1]["body"], issue_repo="owner/repo", target_repo="owner/other"))

    def test_release_checks_merged_pr_mentions_against_successor_qualification(self) -> None:
        self.handoff.write_text("Rebased on #77 from another session.")
        self.f.closed_pulls[77] = {"number": 77, "state": "closed", "merged_at": "2026-10-01T01:00:00Z",
            "created_at": "2026-09-29T00:00:00Z", "user": {"login": BOT}, "body": "Refs #42\nCloses #43",
            "head": {"ref": "work/other-session", "repo": {"full_name": "owner/repo"}},
            "base": {"repo": {"full_name": "owner/repo"}}}
        self.f.timelines["/repos/owner/repo/issues/42/timeline"] = [{"event": "cross-referenced", "source": {"issue": {
            "state": "closed", "created_at": "2026-09-29T00:00:00Z", "user": {"login": BOT},
            "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/77"}}}}]
        with patch.object(PLAN, "api_json", side_effect=self.f.read_api):
            with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                self.run_own_release()
        self.assertEqual(caught.exception.code, "release_pr_attribution_required")
        self.f.assert_no_writes()

    def test_release_classifies_missing_inherited_proof_without_declaration_override(self) -> None:
        url = CLAIM.handoff_url("owner/repo", 42, 12)
        record = {**fixtures.OTHER, "resume_from": "11", "retained_handoff": url}
        self.f.comments[0]["body"] = CLAIM.marker(record)
        for declared in ([], ["https://github.com/owner/repo/pull/99"]):
            with self.subTest(declared=declared):
                self.args.retained_pr = declared
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_own_release()
                self.assertEqual(caught.exception.code, "release_inherited_handoff_changed")
                self.assertEqual(caught.exception.payload["retained_handoff"], url)
                self.assertEqual(caught.exception.payload["claim_comment"], self.args.claim_comment)
                self.f.assert_no_writes()

    def test_release_refuses_unverified_declared_prs(self) -> None:
        self.args.retained_pr = ["https://github.com/owner/repo/pull/99"]
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_own_release()
        self.assertEqual(caught.exception.code, "release_declared_pr_unverified")
        self.f.assert_no_writes()

    def test_release_does_not_relabel_transient_inherited_reads_as_lost_proof(self) -> None:
        failure = PLAN.PlanError("Read deadline expired")
        with patch.object(PLAN, "read_release_inherited_prs", side_effect=failure):
            with self.assertRaises(PLAN.PlanError) as caught:
                self.run_own_release()
        self.assertIs(caught.exception, failure)
        self.f.assert_no_writes()

    def test_release_refuses_a_stripped_inherited_handoff(self) -> None:
        predecessor = {**fixtures.OTHER, "session": "predecessor", "branch": "work/predecessor",
                       "claimed_at": "2026-09-28T00:00:00Z"}
        current = {**fixtures.OTHER, "resume_from": "11", "retained_handoff": CLAIM.handoff_url("owner/repo", 42, 13)}
        self.f.comments = [self.comment(11, CLAIM.marker(predecessor), predecessor["claimed_at"]),
                           self.comment(13, "Released claim 11\n\nFinished work.", "2026-09-30T00:00:00Z"),
                           self.comment(1, CLAIM.marker(current), current["claimed_at"])]
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_own_release()
        self.assertEqual(caught.exception.code, "release_inherited_handoff_changed")
        self.f.assert_no_writes()

    def test_release_cannot_exclude_its_own_source_branch(self) -> None:
        url = "https://github.com/owner/repo/pull/99"
        self.f.pulls = [{"number": 99, "state": "open", "user": {"login": BOT},
            "head": {"ref": fixtures.OTHER["branch"], "repo": {"full_name": "owner/repo"}},
            "base": {"repo": {"full_name": "owner/repo"}}}]
        self.args.exclude_pr = [url]
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_own_release()
        self.assertEqual(caught.exception.code, "release_owned_pr_not_excludable")
        self.f.assert_no_writes()

    def test_release_carries_inherited_prs_created_before_this_claim(self) -> None:
        predecessor = {**fixtures.OTHER, "session": "predecessor", "branch": "work/predecessor",
                       "claimed_at": "2026-09-28T00:00:00Z"}
        current = {**fixtures.OTHER, "resume_from": "11", "retained_handoff": CLAIM.handoff_url("owner/repo", 42, 13)}
        self.f.comments = [self.comment(11, CLAIM.marker(predecessor), predecessor["claimed_at"]),
                           self.comment(13, "Released claim 11\n\nRetained PR #99.", "2026-09-30T00:00:00Z"),
                           self.comment(1, CLAIM.marker(current), current["claimed_at"])]
        self.f.pulls = [{"number": 99, "state": "open", "user": {"login": BOT}, "created_at": "2026-09-29T00:00:00Z",
            "body": "Refs #42", "head": {"ref": "work/retained", "repo": {"full_name": "owner/repo"}},
            "base": {"repo": {"full_name": "owner/repo"}}}]
        self.f.closed_pulls[99] = self.f.pulls[0]
        with patch.object(PLAN, "api_json", side_effect=self.f.read_api):
            self.run_own_release()
        self.assertIn(99, CLAIM.handoff_pr_numbers(self.f.comments[-1]["body"], issue_repo="owner/repo", target_repo="owner/repo"))

    def test_release_discovery_uses_server_clock_and_skips_only_missing_prs(self) -> None:
        self.f.comments[0]["body"] = CLAIM.marker({**fixtures.OTHER, "claimed_at": "2026-10-01T01:00:00Z"})
        self.f.pulls = [{"number": 99, "state": "open", "user": {"login": BOT}, "created_at": "2026-10-01T00:01:00Z",
            "body": "Refs #42", "head": {"ref": "work/new-branch", "repo": {"full_name": "owner/repo"}},
            "base": {"repo": {"full_name": "owner/repo"}}}]
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_own_release()
        self.assertEqual(caught.exception.code, "release_pr_attribution_required")
        self.args.retained_pr = ["https://github.com/owner/repo/pull/99"]
        self.f.timelines["/repos/owner/repo/issues/42/timeline"] = [{"event": "cross-referenced", "source": {"issue": {
            "user": {"login": BOT}, "pull_request": {"url": "https://api.github.com/repos/owner/other/pulls/77"}}}}]
        with patch.object(PLAN, "api_json", side_effect=PLAN.PlanError("Unreadable", api_result={"status": 403})):
            with self.assertRaises(PLAN.PlanError):
                self.run_own_release()
        self.f.assert_no_writes()
        with patch.object(PLAN, "api_json", side_effect=self.f.read_api):
            self.run_own_release()
        self.assertIn(99, CLAIM.handoff_pr_numbers(self.f.comments[-1]["body"], issue_repo="owner/repo", target_repo="owner/repo"))

    def test_dry_run_shows_the_body_without_writing(self):
        self.args.dry_run = True
        self.run_own_release()
        self.f.assert_no_writes()
        self.assertEqual(CLAIM.released_claim_id(self.f.emitted.call_args.args[0]["release_body"]), 1)

    def test_release_refuses_records_this_session_does_not_own_without_writing(self):
        cases = {
            "other session": lambda: setattr(self.args, "session", fixtures.OWNER["session"]),
            "missing claim": lambda: setattr(self.args, "claim_comment", 7),
            "foreign author": lambda: self.f.comments[0].update(user={"login": "foreign[bot]"}),
            "not native": lambda: os.environ.update(CLAUDE_CODE_SESSION_ID="another-native"),
            "other family's session": lambda: (setattr(self.args, "agent", "codex"),
                                               os.environ.update(CODEX_THREAD_ID="codex-thread")),
            "receipt in handoff": lambda: self.handoff.write_text(
                "Done.\n<!-- " + release.RELEASE_MARKER + "{} -->\n"),
        }
        for name, change in cases.items():
            with self.subTest(name), patch.dict(os.environ):
                self.setUp()
                change()
                with self.assertRaises(PLAN.PlanError):
                    self.run_own_release()
                self.f.assert_no_writes()


if __name__ == "__main__":
    unittest.main()
