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
from collections.abc import Callable
from contextlib import nullcontext
from types import SimpleNamespace
from pathlib import Path
from argparse import Namespace
from unittest.mock import Mock, patch
from typing import Any
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
    def comment(n: int, body: str, date: str) -> dict[str, Any]:
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

    def consume_related_release(
        self, comments: Any, *, ended: str = "2026-10-02T00:00:00Z", evidence_by_id: Any = None,
    ) -> dict[str, Any]:
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
        self.setUp()
        self.evidence["body"] = self.evidence["body"].replace("2026-10-02T00:00:00Z", "2026-10-02T00:30:00Z")
        accepted = self.consume_related_release(self.produced_related_release())
        self.assertEqual(accepted["ended_at"], "2026-10-02T00:00:00Z")
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

    def run_release_with_pull_read_failure(self) -> tuple[list[str], PLAN.PlanError]:
        reads: list[str] = []
        original = self.f.read_pages

        def pages(path: str, **kwargs: Any) -> Any:
            if path.endswith("/pulls"):
                reads.append(path)
                raise PLAN.PlanError("fixture transport unavailable", api_result={"status": 503})
            return original(path, **kwargs)

        with patch.object(self.f, "read_pages", side_effect=pages), self.assertRaises(PLAN.PlanError) as caught:
            self.run_release()
        self.f.assert_no_writes()
        return reads, caught.exception

    def test_known_source_refusals_precede_retained_pr_transport_failure(self) -> None:
        for case in ("source-activity", "related-activity", "related-identity"):
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
                reads, _ = self.run_release_with_pull_read_failure()
                self.assertFalse(reads)

    def test_retained_path_pr_read_failure_refuses_before_writes(self) -> None:
        self.f.inventory["worktrees"] = [{"branch": "repair-branch", "path": "/retained"}]
        reads, error = self.run_release_with_pull_read_failure()
        self.assertEqual(PLAN.plan_error_status(error), 503)
        self.assertEqual(len(reads), 1)

    def test_malformed_ended_at_precedes_pr_reads_for_subject(self):
        for ended in ("", "Ended at: invalid\n",
                      "Ended at: 2026-10-01T00:00:00Z\nEnded at: 2026-10-01T00:00:00Z\n"):
            with self.subTest(ended=ended):
                self.setUp()
                self.f.issue["pull_request"] = {}
                self.evidence["body"] = "Closed session session-b\n" + ended + "Safe to exit: yes"
                def evidence_only(method, path, **kwargs):
                    if not path.endswith(f"/issues/comments/{self.evidence['id']}"):
                        self.fail("malformed closure must refuse before reading retained PRs or source commits")
                    return ReleaseHarness.api(self, method, path, **kwargs)
                with patch.multiple(self, api=evidence_only), self.assertRaises(PLAN.PlanError):
                    self.run_release()
                self.f.assert_no_writes()

    def pr_setup(self):
        self.f.pr_fixture("dependabot[bot]")
        self.f.pulls[0]["head"]["sha"] = "a" * 40

    def test_pr_closed_session_attestation_releases_and_allows_successor(self) -> None:
        for mode in ("plain", "dependabot_rebase", "repair_pr", "pull_evidence_url"):
            with self.subTest(mode=mode):
                self.setUp()
                self.pr_setup()
                if mode == "dependabot_rebase":
                    self.commit_date = "2026-10-02T01:00:00Z"
                    self.source_branch_date = "2026-10-01T23:00:00Z"
                elif mode == "repair_pr":
                    self.source_branch_date = self.commit_date
                    self.f.pulls.append({"number": 99, "state": "open", "user": {"login": BOT},
                        "body": "Refs https://github.com/owner/repo/pull/42", "head": {"ref": fixtures.OTHER["branch"],
                        "sha": "b" * 40, "repo": {"full_name": "owner/repo"}}, "base": {"repo": {"full_name": "owner/repo"}}})
                elif mode == "pull_evidence_url":
                    self.args.evidence_comment = self.args.evidence_comment.replace("/issues/884", "/pull/884")
                self.run_release()
                self.assertTrue(self.f.emitted.call_args.args[0]["ok"])
                self.successor()
                self.assertEqual(self.f.emitted.call_args.args[0]["record_kind"], "pr")
                self.assertEqual(self.f.issue["user"]["login"], "dependabot[bot]")
                if mode == "repair_pr":
                    self.assertEqual(self.f.pulls[1]["state"], "open")

    def test_closed_unmerged_pr_can_release_its_ended_session_without_reopening(self) -> None:
        for kind in ("pr", "issue"):
            with self.subTest(kind=kind):
                self.setUp()
                if kind == "pr":
                    self.pr_setup()
                    self.f.pulls[0]["state"] = "closed"
                self.f.issue["state"] = "closed"
                self.run_release()
                self.assertTrue(self.f.emitted.call_args.args[0]["ok"])
                self.assertEqual(self.f.issue["state"], "closed")
                with self.assertRaisesRegex(PLAN.PlanError, "open issue" if kind == "issue" else "open"):
                    self.f.run_claim()

    def test_pr_release_still_refuses_new_activity_on_claimed_source_branch(self) -> None:
        for mode in ("same_branch", "source_branch", "evidence"):
            with self.subTest(mode=mode):
                self.setUp()
                self.pr_setup()
                if mode == "same_branch":
                    self.f.pulls[0]["head"]["ref"] = fixtures.OTHER["branch"]
                    self.commit_date = "2026-10-02T01:00:00Z"
                elif mode == "source_branch":
                    self.source_branch_date = "2026-10-02T01:00:00Z"
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

    def successor(self) -> None:
        self.f.args.resume_from = 1
        with patch.object(self.f, "read_api", side_effect=self.api):
            self.f.run_claim()
        self.assertTrue(self.f.emitted.call_args.args[0]["ok"])

    @staticmethod
    def git_in_worktree(worktree: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(worktree), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
            capture_output=True, text=True, check=check,
        )

    def termination_setup(self, harness: str = "codex") -> Path:
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
        self.termination_record: dict[str, Any] = {
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
                     patch.object(shared, "host_boot_time", return_value=None),
                     patch.object(release.termination, "inventory", return_value=[]),
                     patch.object(release.termination, "run_terminal", return_value=SimpleNamespace(
                         returncode=1, stdout="", stderr="no server running on /fixture/socket"))):
            mock.start()
            self.addCleanup(mock.stop)
        self.termination_path.write_text(json.dumps(self.termination_record))
        data = release.termination.verify(self.termination_record, "session-b")
        self.evidence["body"] = release.termination.body(data)
        return transcript

    def test_termination_uses_verified_native_fork_ancestry_for_copied_parent_metadata(self) -> None:
        transcript = self.termination_setup()
        import claim_history
        rows = [json.loads(line) for line in transcript.read_text().splitlines()]
        rows[0]["payload"].update(forked_from_id="native-parent", parent_thread_id="native-parent",
            subagent_history_start_ordinal=2,
            source={"subagent": {"thread_spawn": {"parent_thread_id": "native-parent"}}})
        rows[1:1] = [{"type": "session_meta", "timestamp": rows[0]["timestamp"], "payload": {"id": "native-parent"}},
            {"type": "event_msg", "timestamp": rows[0]["timestamp"],
             "payload": {"type": "thread_settings_applied", "thread_id": "session-b"}}]
        for index, row in enumerate(rows):
            row["ordinal"] = index
        context = {"parent": "native-parent", "ancestors": ("native-parent",)}
        with patch.object(claim_history, "root_context", return_value=context):
            transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
            os.utime(transcript, (release.stamp(rows[0]["timestamp"]).timestamp(),) * 2)
            self.assertEqual(release.termination.verify(self.termination_record, "session-b")["session_id"], "session-b")
            rows[1]["payload"]["id"] = "foreign-native"
            transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
            os.utime(transcript, (release.stamp(rows[0]["timestamp"]).timestamp(),) * 2)
            with self.assertRaises(ValueError):
                release.termination.verify(self.termination_record, "session-b")

    def test_terminated_session_releases_without_verdict_and_successor_can_claim(self) -> None:
        for harness in ("codex", "claude"):
            for with_worktree in (False, True):
                with self.subTest(harness=harness, with_worktree=with_worktree):
                    self.setUp()
                    self.termination_setup(harness)
                    self.run_release()
                    released = self.f.comments[-1]
                    receipt: Any = release.receipt_for(released)
                    artifact = self.termination_record["retained_artifacts"][0]
                    self.assertEqual(receipt["termination"]["retained_artifacts"], self.termination_record["retained_artifacts"])
                    canonical = release.verified_comments(self.f.comments, lambda *_: self.evidence)
                    self.assertTrue(any(release.receipt_for(c) for c in canonical))
                    self.assertEqual(release._released_source_contexts(canonical), ())
                    self.assertIn(artifact["worktree"], released["body"])
                    if with_worktree:
                        self.f.inventory["worktrees"] = [{"branch": artifact["branch"], "path": artifact["worktree"]}]
                        with patch.object(self.f, "read_api", side_effect=self.api):
                            self.f.run_claim()
                        result: dict[str, Any] = self.f.emitted.call_args.args[0]
                        self.assertTrue(result["ok"])
                        self.assertNotEqual(result["claim"]["branch"], artifact["branch"])
                    else:
                        self.successor()
                    self.assertEqual((Path(artifact["worktree"]) / "unfinished.txt").read_text(), self.uncommitted_source)

    def test_terminated_session_with_live_process_or_resumed_process_refuses(self) -> None:
        self.termination_setup()
        for processes in ([(12345, "other", "codex")], [(88, "ttys123", "claude")],
                          [(88, "other", "codex resume session-b")]):
            with self.subTest(processes=processes), patch.object(release.termination, "inventory", return_value=processes):
                with self.assertRaises(PLAN.PlanError):
                    self.run_release()
                self.f.assert_no_writes()

    def test_reboot_releases_with_reused_pid_and_tty_but_preserves_resume_and_transcript_guards(self) -> None:
        for harness, offset_minutes in (("codex", 0), ("claude", 0), ("codex", 5)):
            with self.subTest(harness=harness, offset_minutes=offset_minutes):
                self.setUp()
                transcript = self.termination_setup(harness)
                shared = release.termination.native_helpers()[1]
                booted = release.stamp(self.termination_record["ended_at"]) + timedelta(minutes=offset_minutes)
                rows = [(12345, "ttys123", "unrelated")] if offset_minutes else [(12345, "other", "unrelated"), (88, "ttys123", "unrelated")]
                with (patch.object(shared, "host_boot_time", return_value=booted),
                      patch.object(shared, "process_started_at", return_value=booted + timedelta(seconds=1)),
                      patch.object(release.termination, "inventory", return_value=rows)):
                    observed = release.termination.verify(self.termination_record, "session-b")
                    self.evidence["body"] = release.termination.body(observed)
                    self.run_release()
                    self.assertTrue(release.receipt_for(self.f.comments[-1]))
                    self.assertEqual(observed["booted_at"], booted.isoformat())
                    if offset_minutes:
                        continue
                    posts = self.f.events.count("post")
                    for command in ("codex resume session-b", "claude --resume session-b"):
                        with (self.subTest(command=command),
                              patch.object(release.termination, "inventory", return_value=[(12345, "other", command)]),
                              self.assertRaises(ValueError)):
                            release.termination.verify(self.termination_record, "session-b")
                    original = transcript.read_bytes()
                    for path in (transcript, transcript.with_name("resumed-session-b.jsonl")):
                        with self.subTest(path=path.name):
                            os.utime(transcript, ((booted - timedelta(minutes=1)).timestamp(),) * 2)
                            path.write_bytes(original)
                            os.utime(path, (booted.timestamp(),) * 2)
                            with self.assertRaises(ValueError):
                                release.termination.verify(self.termination_record, "session-b")
                    self.assertEqual(self.f.events.count("post"), posts)

    def test_missing_future_or_pre_crash_boot_keeps_strict_pid_and_tty_checks(self) -> None:
        self.termination_setup()
        shared = release.termination.native_helpers()[1]
        for booted in (None, release.stamp("2026-10-02T01:00:01Z")):
            with (self.subTest(booted=booted), patch.multiple(shared, host_boot_time=Mock(return_value=booted)),
                  patch.object(release.termination, "inventory", return_value=[(12345, "other", "unrelated")]),
                  self.assertRaises(ValueError)):
                release.termination.verify(self.termination_record, "session-b")
        self.f.assert_no_writes()
        pre_crash = release.stamp("2026-10-01T23:58:00Z")
        with patch.multiple(shared, host_boot_time=Mock(return_value=pre_crash)):
            observed = release.termination.verify(self.termination_record, "session-b")
            self.assertNotIn("booted_at", observed)
            self.evidence["body"] = release.termination.body(observed)
            self.run_release()
            self.assertTrue(release.receipt_for(self.f.comments[-1]))
            with (patch.object(release.termination, "inventory", return_value=[(12345, "other", "unrelated")]),
                  self.assertRaises(ValueError)):
                release.termination.verify(self.termination_record, "session-b")

    def test_reboot_queue_recheck_refuses_modified_transcript_or_new_generation_pid(self) -> None:
        transcript = self.termination_setup()
        shared = release.termination.native_helpers()[1]
        booted = release.stamp(self.termination_record["ended_at"])
        with patch.multiple(shared, host_boot_time=Mock(return_value=booted)):
            process = release.termination._verify_queue_source(self.termination_record, "session-b")
            os.utime(transcript, (booted.timestamp(),) * 2)
            with self.assertRaises(ValueError):
                release.termination._recheck_queue_process(process)
        self.setUp()
        self.termination_setup()
        shared = release.termination.native_helpers()[1]
        with patch.multiple(shared, host_boot_time=Mock(return_value=booted)):
            process = release.termination._verify_queue_source(self.termination_record, "session-b", latest_pid=88)
            with patch.object(release.termination, "inventory", return_value=[(88, "other", "unidentified")]):
                with self.assertRaises(ValueError):
                    release.termination._verify_queue_source(self.termination_record, "session-b", latest_pid=88)
                with self.assertRaises(ValueError):
                    release.termination._recheck_queue_process(process)

    def test_reboot_proof_requires_native_empty_proof_and_refuses_later_native_action(self) -> None:
        self.termination_setup()
        import github_plan_native
        shared = release.termination.native_helpers()[1]
        boot = release.stamp(self.termination_record["ended_at"])
        with (patch.multiple(shared, host_boot_time=Mock(return_value=boot)),
              patch.object(github_plan_closure, "ownership", return_value=nullcontext(True)),
              patch.object(github_plan_closure, "lease_state", return_value={"action_pending": True, "action_at": boot.isoformat()}),
              self.assertRaises(ValueError)):
            release.termination.verify(self.termination_record, "session-b")
        action = {"action_pending": True, "action_at": (boot - timedelta(minutes=1)).isoformat()}
        for blocked in (False, True):
            with self.subTest(blocked=blocked), patch.multiple(shared, host_boot_time=Mock(return_value=boot)), \
                    patch.object(github_plan_closure, "ownership", return_value=nullcontext(True)), \
                    patch.object(github_plan_closure, "lease_state", return_value=action), \
                    patch.object(github_plan_native, "prove_crash_end", **(
                        {"side_effect": ValueError("native source is queued")} if blocked else {"return_value": None})):
                if blocked:
                    with self.assertRaises(ValueError):
                        release.termination.verify(self.termination_record, "session-b")
                else:
                    release.termination.verify(self.termination_record, "session-b")

    def test_reboot_source_lease_covers_the_checks_after_native_proof_without_creating_missing_state(self) -> None:
        import sys
        import github_plan_native
        transcript = self.termination_setup()
        shared = release.termination.native_helpers()[1]
        boot = release.stamp(self.termination_record["ended_at"])
        lease = transcript.parent / "owned.lock"
        action = {"session_id": "session-b", "action_pending": True, "action_kind": "opaque",
                  "action_at": (boot - timedelta(minutes=1)).isoformat(), "action_homes": {"known": [], "unscoped": True}}
        lease.write_text(json.dumps(action))
        lease.chmod(0o600)
        before = lease.read_bytes()
        original_artifacts = release.termination._verify_artifacts
        def checked(artifacts: list[dict[str, Any]]) -> None:
            probe = "import fcntl,sys\nwith open(sys.argv[1]) as stream:\n try: fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)\n except BlockingIOError: sys.exit(0)\n sys.exit(1)\n"
            self.assertEqual(subprocess.run([sys.executable, "-c", probe, str(lease)], check=False, timeout=5).returncode, 0)
            original_artifacts(artifacts)
        with patch.object(shared, "host_boot_time", return_value=boot), \
                patch.object(github_plan_closure, "lease_path", return_value=lease), \
                patch.object(github_plan_native, "prove_crash_end", return_value=None):
            with patch.object(release.termination, "_verify_artifacts", side_effect=checked):
                release.termination.verify(self.termination_record, "session-b")
            self.assertEqual(lease.read_bytes(), before)
            lease.unlink()
            release.termination.verify(self.termination_record, "session-b")
            self.assertFalse(lease.exists())
            with patch.object(github_plan_closure, "lease_state", return_value=action), self.assertRaises(ValueError):
                release.termination.verify(self.termination_record, "session-b")
            with patch.object(github_plan_closure, "lease_state", side_effect=[{}, action]), self.assertRaises(ValueError):
                release.termination.verify(self.termination_record, "session-b")
            self.assertFalse(lease.exists())

    def test_termination_tty_reuse_requires_native_cutoff_and_pending_action_veto(self) -> None:
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

    def test_termination_covers_configured_account_transcripts_outside_declared_roots(self) -> None:
        transcript = self.termination_setup()
        import github_plan_sessions
        configured = Path(self.enterContext(tempfile.TemporaryDirectory()))
        sessions = configured / "sessions"
        sessions.mkdir()
        (sessions / "resumed-session-b.jsonl").write_text(transcript.read_text() + json.dumps({
            "type": "event_msg", "timestamp": "2026-10-02T00:30:00Z", "payload": {"type": "agent_message", "message": "resumed"}}) + "\n")
        with patch.object(github_plan_sessions, "observation_homes", return_value=[configured]):
            with self.assertRaises(ValueError):
                release.termination.verify(self.termination_record, "session-b")

    def test_public_termination_rechecks_terminal_after_transcript_quietness(self) -> None:
        self.termination_setup()
        original = release.termination.transcript_snapshot
        def source(*args: Any) -> Any:
            value = original(*args)
            release.termination.run_terminal.return_value = SimpleNamespace(
                returncode=0, stdout="a" * 32 + "\t0\t1\n", stderr="")
            return value
        with patch.object(release.termination, "transcript_snapshot", side_effect=source), self.assertRaises(ValueError):
            release.termination.verify(self.termination_record, "session-b")
        self.f.assert_no_writes()

    def test_terminated_session_recent_or_resumed_transcript_refuses(self) -> None:
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

    def test_terminated_session_uncertain_identity_terminal_or_evidence_refuses(self) -> None:
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

    def test_termination_uses_launch_nonce_and_distinguishes_restarted_server(self) -> None:
        self.termination_setup()
        ended = release.stamp(self.termination_record["ended_at"]).timestamp()
        for nonce, dead, started, allowed in (("a" * 32, "1", ended - 100, True),
                                              ("b" * 32, "1", ended + 100, True),
                                              ("b" * 32, "1", ended - 100, False),
                                              ("a" * 32, "0", ended + 100, False)):
            with self.subTest(nonce=nonce, dead=dead, started=started):
                self.setUp()
                self.termination_setup()
                release.termination.run_terminal.return_value = SimpleNamespace(
                    returncode=0, stdout=f"{nonce}\t{dead}\t{int(started)}\n", stderr="")
                if allowed:
                    self.run_release()
                    self.assertTrue(self.f.emitted.call_args.args[0]["ok"])
                    if nonce == "a" * 32:
                        self.evidence["updated_at"] = "2026-10-04T00:00:00Z"
                        with self.assertRaisesRegex(PLAN.PlanError, "ownership evidence"):
                            self.successor()
                        self.evidence["updated_at"] = "2026-10-02T01:00:00Z"
                        self.successor()
                else:
                    posts = self.f.events.count("post")
                    with self.assertRaises(PLAN.PlanError):
                        self.run_release()
                    self.assertEqual(self.f.events.count("post"), posts)

    def test_historical_termination_receipt_survives_later_quiet_period_policy(self) -> None:
        self.termination_setup()
        self.run_release()
        with patch.object(release.termination, "QUIET_PERIOD", timedelta(days=2)):
            self.successor()
        data = release.termination.evidence(self.evidence["body"], "session-b", apply_policy=False)
        data.update(terminal_state="exit recorded", exit_record={"kind": "codex_command"}, exit_digest="a" * 64)
        with self.assertRaises(ValueError):
            release.termination.validate(data, "session-b", apply_policy=False)

    def test_termination_wrong_or_missing_retained_worktree_refuses(self) -> None:
        self.termination_setup()
        artifact = self.termination_record["retained_artifacts"][0]
        for path, branch in ((artifact["worktree"], "work/wrong"),
                             (str(self.termination_path.parent / "absent"), artifact["branch"])):
            with self.subTest(path=path, branch=branch):
                record = {**self.termination_record, "retained_artifacts": [{"worktree": path, "branch": branch}]}
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    release.termination.verify(record, "session-b")

    def test_crash_truncated_tail_is_preserved_but_interior_corruption_refuses(self) -> None:
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

    def test_crash_before_worktree_or_with_remote_only_branch_releases(self) -> None:
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

    def test_crash_during_detached_operation_keeps_work_and_branch(self) -> None:
        self.termination_setup()
        worktree = self.termination_record["retained_artifacts"][0]["worktree"]
        self.git_in_worktree(worktree, "commit", "-q", "--allow-empty", "-m", "fixture")
        self.git_in_worktree(worktree, "checkout", "--detach", "--quiet")
        self.run_release()
        self.assertEqual((Path(worktree) / "unfinished.txt").read_text(), self.uncommitted_source)

    def test_crash_recovery_preserves_an_actual_conflicted_rebase(self) -> None:
        self.termination_setup()
        artifact = self.termination_record["retained_artifacts"][0]
        path = Path(artifact["worktree"])
        conflict = path / "conflict.txt"
        conflict.write_text("base\n")
        self.git_in_worktree(path, "add", "conflict.txt")
        self.git_in_worktree(path, "commit", "-q", "-m", "base")
        self.git_in_worktree(path, "branch", "upstream")
        conflict.write_text("source\n")
        self.git_in_worktree(path, "commit", "-q", "-am", "source")
        self.git_in_worktree(path, "checkout", "-q", "upstream")
        conflict.write_text("upstream\n")
        self.git_in_worktree(path, "commit", "-q", "-am", "upstream")
        self.git_in_worktree(path, "checkout", "-q", artifact["branch"])
        attempted = self.git_in_worktree(path, "rebase", "upstream", check=False)
        self.assertNotEqual(attempted.returncode, 0)
        state = self.git_in_worktree(path, "rev-parse", "--git-path", "rebase-merge").stdout.strip()
        state_path = Path(state) if Path(state).is_absolute() else path / state
        self.assertTrue(state_path.is_dir())
        self.run_release()
        self.assertTrue(state_path.is_dir())
        self.assertEqual(self.git_in_worktree(path, "branch", "--list", artifact["branch"]).stdout.strip(), artifact["branch"])

    def test_unrelated_detached_worktree_cannot_borrow_the_source_branch(self) -> None:
        self.termination_setup()
        worktree = self.termination_record["retained_artifacts"][0]["worktree"]
        self.git_in_worktree(worktree, "commit", "-q", "--allow-empty", "-m", "source")
        self.git_in_worktree(worktree, "checkout", "--quiet", "--orphan", "unrelated")
        self.git_in_worktree(worktree, "commit", "-q", "--allow-empty", "-m", "unrelated")
        self.git_in_worktree(worktree, "checkout", "--quiet", "--detach")
        with self.assertRaises(PLAN.PlanError):
            self.run_release()
        self.f.assert_no_writes()

    def test_termination_receipt_must_match_attested_artifacts(self) -> None:
        self.termination_setup()
        self.run_release()
        receipt = release.receipt_for(self.f.comments[-1])
        receipt["termination"]["retained_artifacts"] = []
        with self.assertRaises(ValueError):
            release.validate_evidence(receipt, self.evidence, BOT)

    def test_abandoned_release_and_followup_variants_become_claimable(self) -> None:
        for mode in ("missing_exact_release", "related_followup", "distinct_released_followup",
                     "closed_native_blocker", "conditional_branch", "conditional_worker"):
            with self.subTest(mode=mode):
                self.setUp()
                if mode == "related_followup":
                    self.f.comments.append(self.comment(2, "Claimed by trial-b for capacity work.", "2026-10-01T01:00:00Z"))
                    self.args.related_claim_comment = [2]
                elif mode == "distinct_released_followup":
                    self.f.comments.append(self.comment(2, "Claimed by trial-b\nSession: distinct-session", "2026-10-01T01:00:00Z"))
                    self.f.comments.append(self.comment(3, "Released claim 2", "2026-10-01T02:00:00Z"))
                elif mode == "closed_native_blocker":
                    self.f.blockers = [{"number": 1165, "state": "closed"}]
                elif mode == "conditional_branch":
                    self.f.comments[0].update(body=CLAIM.marker({**fixtures.OTHER, "branch": "work/refresh-after-deploy"}))
                elif mode == "conditional_worker":
                    self.f.comments[0].update(
                        body=CLAIM.marker({**fixtures.OTHER, "branch": "work/wait-until-ci", "worker": "trial-pending-review"}))
                self.run_release()
                self.successor()
                if mode == "related_followup":
                    self.assertEqual(self.f.emitted.call_args.args[0]["claim"]["session"], fixtures.OWNER["session"])
                if mode == "closed_native_blocker":
                    self.assertEqual(self.f.blockers[0]["state"], "closed")

    def test_successor_with_same_worker_keeps_confirmed_release_valid_on_readback(self) -> None:
        self.run_release()
        self.f.args.worker = fixtures.OTHER["worker"]

        def date_successor() -> None:
            self.f.comments[-1].update(created_at="2026-10-04T00:00:00Z",
                                       updated_at="2026-10-04T00:00:00Z")

        self.f.after_post = date_successor
        self.successor()
        result = self.f.emitted.call_args.args[0]
        self.assertEqual(result["claim"]["worker"], fixtures.OTHER["worker"])
        self.assertEqual(result["claim"]["session"], fixtures.OWNER["session"])
        self.assertEqual(result["claim"]["branch"], fixtures.OWNER["branch"])
        posts = self.f.events.count("post")
        self.successor()
        self.assertEqual(self.f.events.count("post"), posts)

    def test_released_sessions_later_claim_invalidates_release_and_blocks_successor(self) -> None:
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

    def test_unstructured_followup_still_invalidates_structured_release(self) -> None:
        self.run_release()
        for header in (f"Claimed by {fixtures.OTHER['worker']}", f"**Claimed by** {fixtures.OTHER['worker']}"):
            with self.subTest(header=header):
                followup = self.comment(3, header, "2026-10-04T00:00:00Z")
                with self.assertRaisesRegex(ValueError, "ownership activity"):
                    release._check_activity([*self.f.comments, followup], self.f.comments[0],
                                            release.stamp("2026-10-02T00:00:00Z"),
                                            release.source_record(self.f.comments[0]))

    def test_related_followup_session_must_match_source_closure(self) -> None:
        for field in ("Session: {}", "**Session:** `{}`", "> Session: {}", "Session: {}.", "Session: {} (capacity work)", "Session ID: {}", "Native session ID: {}", "session={}", "session_id: {}", "sessionId: {}", "Session-ID: {}", "Thread ID: {}", "Session — {}", "| Session | {} |", "Session: {}; PR #99", "(Session: {})",
                      "Handoff from session: {}", "Continuing from session: {}", "Previous session: {}", "Prior native session ID: {}"):
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

    def test_related_followup_prose_is_not_a_session_field(self) -> None:
        for prose in ("session-start hook", "session-scoped work", "thread-safe cleanup",
                      "Tooling landed this session: https://github.com/owner/repo/pull/3510 (IDE records) and "
                      "https://github.com/owner/repo/pull/3519 (claim scope).",
                      "Status for this exact session: `status` reports completion.",
                      "Work completed during our session: everything passed.",
                      "Codex session: other-label",
                      "Review thread: https://github.com/owner/repo/pull/99",
                      "Next actor: Chris performs Q914 in his existing Launchplane administrator session: "
                      "https://github.com/owner/repo/issues/42#issuecomment-7",
                      "Chris uses his browser session: https://example.test/admin",
                      "Chris uses his administrator session: [comment](https://example.test/admin)",
                      "Chris uses his administrator session: <https://example.test/admin>",
                      "Chris uses his administrator\nsession: https://example.test/admin",
                      "Chris uses his browser session: the one already open"):
            with self.subTest(prose=prose):
                self.setUp()
                self.f.comments.append(self.comment(2, "Claimed by trial-b to fix " + prose,
                                                    "2026-10-01T01:00:00Z"))
                self.args.related_claim_comment = [2]
                self.run_release()
                self.successor()

    def test_explicit_session_fields_keep_conflicting_and_malformed_values_refused(self) -> None:
        for field in ("Session: {}", "Claimed by trial-b\nsession: {}", "Worker: trial-b\nsession: {}",
                      "- Session: {}", "Handoff; Session: {}",
                      "Ownership record Native session ID: {}", "Thread ID: {}", "| Session | {} |",
                      "Claimed by trial-b Session: {}", "Claimed by trial-b, Session: {}",
                      "1. Session: {}", "Worker: trial-b. Session: {}", "Handoff: Session: {}",
                      "Claimed by: administrator Session: {}", "Worker: browser Session: {}",
                      "Owner: human Session: {}", "Next actor: human\nSession: {}",
                      "Approval: browser\nSession: {}", "his browser\n\nSession: {}"):
            for value in ("other-session", "https://example.test/admin", "unknown"):
                with self.subTest(field=field, value=value):
                    self.assertFalse(release.related_sessions_match(field.format(value), "session-b"))
            self.assertTrue(release.related_sessions_match(field.format("session-b"), "session-b"))

    def test_native_session_fields_allow_harness_labels_but_reject_ambiguous_ids(self) -> None:
        session = "81687039-a90a-4107-a144-29bd815ce6ba"
        other = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        for field in ("Session: {}", "- **Session:** Claude Code `{}`", "Native session ID: Codex {}",
                      "| Session | Claude Code `{}` |", "Thread ID: {} (Claude Code)",
                      "Session: Codex CLI `{}`", "Session: Claude Code CLI {}", "Session: Codex desktop {}",
                      "Session: Antigravity {}", "Handoff from session: {}"):
            with self.subTest(field=field):
                self.assertTrue(release.related_sessions_match(field.format(session), session, require_explicit=True))
                for value in (other, "prefix-" + session, session + "-suffix", session + " or " + other):
                    self.assertFalse(release.related_sessions_match(field.format(value), session, require_explicit=True))
        for body in ("No session field", f"```\nSession: {session}\n```", f"Session: {session}\nSession: {other}",
                     f"Previous session: {session}", f"Prior native session ID: {session}",
                     f"Previous Codex session: {session}", f"Prior Claude Code session: {session}",
                     f"previous-session: {session}", f"Earlier session: {session}", f"Original session: {session}",
                     f"Superseded session: {session}",
                     f"Session: {session}\nPrevious session: {other}"):
            self.assertFalse(release.related_sessions_match(body, session, require_explicit=True))
        for label in ("Previous session", "Prior native session ID", "Previous Codex session"):
            self.assertTrue(release.related_sessions_match(f"{label}: {session}", session))
            self.assertFalse(release.related_sessions_match(f"{label}: {other}", session))
            self.assertTrue(release.related_sessions_match(f"{label}: session-b", "session-b"))
            self.assertFalse(release.related_sessions_match(f"{label}: other-label", "session-b"))
        self.assertTrue(release.related_sessions_match("No session field", session))
        self.assertTrue(release.related_sessions_match(
            f"Changes in this session:\n- Finished the work.\n| Session | |\n- Session: Claude Code `{session}`", session,
            require_explicit=True,
        ))

    def test_session_prose_is_not_a_standalone_ownership_assertion(self) -> None:
        for prose in (
            "Tooling landed this session: https://github.com/owner/repo/pull/3510",
            "Earlier in this session: the IDE records landed.",
            "Summary: tooling landed during this\nsession: https://github.com/owner/repo/pull/3510",
            "Summary: we did\nthe work in this\nsession: all green",
            "Next actor: Chris uses his existing administrator session: https://example.test/admin",
            "Chris uses his administrator session: [comment](https://example.test/admin)",
            "Chris uses his administrator session: <https://example.test/admin>",
            "Chris uses his administrator\nsession: https://example.test/admin",
            "Chris uses his browser session: the one already open",
            "Chris uses his prior browser session: bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        ):
            self.assertFalse(CLAIM.has_ownership_assertion(prose))
        for field in ("Session: https://example.test/admin", "Native session: https://example.test/admin",
                      "Claimed by worker Session: https://example.test/admin",
                      "Next actor: human\nSession: other-session", "his browser\n\nSession: other-session"):
            self.assertTrue(CLAIM.has_ownership_assertion(field))

    def test_related_source_followup_ignores_fenced_session_examples(self) -> None:
        for fence in ("```", "~~~"):
            with self.subTest(fence=fence):
                self.setUp()
                self.f.comments.append(self.comment(2,
                    f"Claimed by trial-b\nSession: session-b\n{fence}\nSession: example-session\n{fence}",
                    "2026-10-01T01:00:00Z"))
                self.args.related_claim_comment = [2]
                self.run_release()
                self.successor()

    def refresh_setup(self) -> None:
        self.f.refresh_fixture()
        self.f.comments[2]["body"] = "Released claim 1"
        for n, c in enumerate(self.f.comments):
            c.update(created_at=f"2026-10-01T0{n}:00:00Z", updated_at=f"2026-10-01T0{n}:00:00Z")
        for p in self.f.pulls:
            p["head"]["sha"] = "fixture-retained-head"
        self.args.retained_pr = [f"https://github.com/owner/repo/pull/{p['number']}" for p in self.f.pulls]

    def test_retained_pr_malformed_or_contextual_handoff_recovers_with_attestation(self) -> None:
        for mode in ("malformed_late_handoff", "missing_pr_attestation", "contextual_link"):
            with self.subTest(mode=mode):
                self.setUp()
                self.refresh_setup()
                if mode == "malformed_late_handoff":
                    self.f.comments[2]["body"] = "trial-b handoff:\n\nReleased claim 1\nPR #99 and #100"
                    self.f.comments.append(self.comment(4, "Released claim 1", "2026-10-01T03:00:00Z"))
                if mode == "missing_pr_attestation":
                    self.f.comments[2]["body"] = "Released claim 1\nNo PR attestation on this canonical issue."
                if mode == "contextual_link":
                    self.f.pulls[0]["body"] = "Planning issue: https://github.com/owner/repo/issues/42"
                self.run_release()
                self.successor()

    def test_repairshopr_embedded_release_is_claimable_without_supervisor(self) -> None:
        self.f.comments.append(self.comment(2, "Owner question: verify read-only usage?\n\nReleased claim 1", "2026-10-01T01:00:00Z"))
        self.successor()

    def test_open_native_blocker_still_refuses(self) -> None:
        self.f.blockers = [{"number": 1165, "state": "open"}]
        self.run_release()
        self.f.events.clear()
        with self.assertRaises(PLAN.PlanError) as caught:
            self.successor()
        self.assertEqual(caught.exception.code, "claim_native_blockers_unresolved")
        self.f.assert_no_writes()

    def test_refusal_shapes_do_not_write(self) -> None:
        modes: tuple[str, ...] = (
            "source_edit", "renewed_claim", "wrong_evidence_session", "missing_ended_at",
            "foreign_evidence", "foreign_source", "unconfirmed", "releaser_is_source",
            "unknown_related", "unrelated_followup", "future_ended_at",
            "location", "timezone", "new_related", "closed_unmerged",
        )
        for mode in modes:
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
                if mode == "location": self.evidence["issue_url"] = "https://api.github.com/repos/owner/control/issues/885"
                if mode == "timezone": self.evidence["body"] = self.evidence["body"].replace("2026-10-02T00:00:00Z", "2026-10-02T00:00:00")
                if mode == "new_related":
                    self.f.comments.append(self.comment(2, "Claimed by trial-b capacity work", "2026-10-02T01:00:00Z"))
                    self.args.related_claim_comment = [2]
                if mode == "closed_unmerged":
                    self.refresh_setup()
                    self.f.pulls[0]["state"] = "closed"
                with self.assertRaises(PLAN.PlanError): self.run_release()
                self.f.assert_no_writes()

    def test_next_keeps_closed_blocker_history_without_excluding_for_open_dependency(self) -> None:
        issue = {**self.f.issue, "repo": "owner/repo"}
        relationships = {"blocked_by": [{"number": 1165, "state": "closed"}], "blocking": [], "sub_issues": []}
        state, result = PLAN.evaluate_next_plan(issue, config=PLAN.DEFAULT_CONFIG, focus=None, relationships=relationships)
        self.assertEqual(state, "candidate")
        self.assertNotIn("exclusion", result)
        self.assertEqual(relationships["blocked_by"][0]["number"], 1165)

    def test_invalidated_attestation_or_source_edit_blocks_successor_until_recovered(self) -> None:
        self.run_release()
        self.evidence["updated_at"] = "2026-10-04T00:00:00Z"
        with self.assertRaisesRegex(PLAN.PlanError, "ownership evidence"):
            self.successor()
        self.f.args.resume_from = None
        for key in ("worker", "session", "branch"):
            setattr(self.f.args, key, fixtures.OTHER[key])
        with patch.object(self.f, "read_api", side_effect=self.api):
            self.f.run_claim()
        self.assertEqual(self.f.emitted.call_args.args[0]["claim"]["session"], fixtures.OTHER["session"])
        self.setUp()
        self.run_release()
        self.evidence["updated_at"] = "2026-10-04T00:00:00Z"
        self.run_release()
        self.successor()
        self.setUp()
        self.run_release()
        self.evidence_failure = 404
        with self.assertRaisesRegex(PLAN.PlanError, "ownership evidence"):
            self.successor()
        self.evidence_failure = None
        self.successor()
        self.setUp()
        self.run_release()
        self.f.comments[0]["updated_at"] = "2026-10-04T00:00:00Z"
        with self.assertRaisesRegex(PLAN.PlanError, "ownership evidence"):
            self.successor()

    def test_post_closure_commit_activity_refuses_before_post(self) -> None:
        for mode in ("retained", "unlisted_source_branch", "issue_refusal_masked"):
            with self.subTest(mode=mode):
                self.setUp()
                self.refresh_setup()
                if mode == "unlisted_source_branch":
                    self.f.pulls[0]["head"]["ref"] = CLAIM.records(self.f.comments[0]["body"])[0]["branch"]
                    self.args.retained_pr = []
                if mode == "issue_refusal_masked":
                    self.f.pulls[0]["head"]["ref"] = fixtures.OTHER["branch"]
                    self.f.pulls[0]["body"] = "See #42"
                self.commit_date = "2026-10-02T01:00:00Z"
                with self.assertRaisesRegex(PLAN.PlanError, "commit activity after") as caught:
                    self.run_release()
                if mode != "retained":
                    self.assertNotEqual(caught.exception.payload.get("release_refusal_scope"), release.ISSUE_REFUSAL_SCOPE)
                self.f.assert_no_writes()

    def test_untrusted_or_example_receipt_does_not_poison_claims(self) -> None:
        for text in ("<!-- " + release.RELEASE_MARKER + "bad -->", "```\n<!-- " + release.RELEASE_MARKER + "bad -->\n```"):
            with self.subTest(text=text):
                self.setUp(); self.run_release()
                self.f.comments.append(self.comment(9, text, "2026-10-03T01:00:00Z"))
                self.successor()

    def test_successor_push_does_not_resurrect_the_abandoned_owner(self) -> None:
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

    def test_hidden_checked_pr_is_not_a_visible_refresh_handoff(self) -> None:
        self.refresh_setup()
        self.f.pulls[0]["head"]["ref"] = CLAIM.records(self.f.comments[0]["body"])[0]["branch"]
        self.args.retained_pr = [self.args.retained_pr[1]]
        self.run_release()
        before = copy.deepcopy((self.f.comments, self.f.issue, self.f.inventory))
        with self.assertRaises(PLAN.PlanError): self.successor()
        self.assertEqual(before, (self.f.comments, self.f.issue, self.f.inventory))

    def test_dry_run_is_reviewable_and_writes_nothing(self) -> None:
        self.args.dry_run = True
        self.run_release()
        self.f.assert_no_writes()
        self.assertEqual(CLAIM.released_claim_id(self.f.emitted.call_args.args[0]["release_body"]), 1)

    def test_post_write_race_is_not_reported_as_success(self) -> None:
        self.race = lambda: self.f.comments.append(self.comment(8, CLAIM.marker(fixtures.OTHER), "2026-10-04T00:00:00Z"))
        with self.assertRaisesRegex(PLAN.PlanError, "activity after") as caught: self.run_release()
        self.assertIn("post_release", caught.exception.payload["completed_steps"])
        self.assertEqual(self.f.events.count("post"), 1)

    def test_post_write_resumption_or_evidence_change_leaves_no_usable_release(self) -> None:
        for route in ("finished", "terminated"):
            for mode in ("evidence", *(("process",) if route == "terminated" else ())):
                with self.subTest(route=route, mode=mode):
                    self.setUp()
                    if route == "terminated":
                        self.termination_setup()
                    def resume() -> None:
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

    def assert_source_still_owned(self) -> None:
        effective = release.effective_comments(self.f.comments)
        conflicts, _ = CLAIM.discussion_evidence("", effective, fixtures.OWNER)
        self.assertIn(self.args.claim_comment, [c["id"] for c in conflicts])

    def test_pending_release_preserves_sibling_claim_and_recorded_wait(self) -> None:
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

    def test_confirmed_release_rerun_reuses_exact_receipt_without_new_writes(self) -> None:
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

    def test_failed_pending_attempt_requires_fresh_closure_evidence_before_recovery(self) -> None:
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

    def test_related_recovery_reuses_source_when_observed_branch_pr_has_closed(self) -> None:
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
        self.f.pulls = []
        self.related_failure = None
        posts = self.f.events.count("post")
        self.run_release()
        self.assertEqual(self.f.emitted.call_args.args[0]["release_comment"]["id"], source_release)
        self.assertEqual(self.f.events.count("post"), posts + 1)
        self.successor()

    def test_uncertain_confirmation_preserves_exact_record_and_never_replays(self) -> None:
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
                    self.confirmation_failure = None
                    writes = (self.f.events.count("post"), self.f.events.count("confirm_release"))
                    if applied:
                        self.run_release()
                        self.assertTrue(self.f.emitted.call_args.args[0]["ok"])
                    else:
                        with self.assertRaises(PLAN.PlanError):
                            self.run_release()
                    self.assertEqual((self.f.events.count("post"), self.f.events.count("confirm_release")), writes)

    def test_unlinked_foreign_and_unmentioned_retained_prs_remain_refused(self) -> None:
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


class OwnReleaseTests(ReleaseHarness):
    """A worker releases its own claim through the helper, never a hand-written directive."""

    def setUp(self) -> None:
        super().setUp()
        self.handoff = Path(self.enterContext(tempfile.TemporaryDirectory())) / "handoff.md"
        # LP4214-C1 put this summary on the directive line by hand.
        self.handoff.write_text("LP4214-C1 Finish Line met: reviewed, tested and landed.\n\nNext actor: agent:integrator\n\nSafe to exit: yes\n")
        # The releasing worker runs as the claim's own native session.
        os.environ["CLAUDE_CODE_SESSION_ID"] = fixtures.OTHER["session"]
        self.args = Namespace(repo="owner/repo", issue="42", claim_comment=1, session=fixtures.OTHER["session"],
                              handoff_file=str(self.handoff), agent="claude", dry_run=False, next_actor="agent:integrator")

    def run_own_release(self) -> None:
        with patch.multiple(PLAN, default_repo=lambda _: "owner/repo", get_issue=self.f.get_issue,
                            EXPECTED_ACTOR=BOT, collect_paged_rest_items=self.f.read_pages, rest_edit_issue=self.f.edit,
                            comment_route=lambda: ("bot", "bot-gh", BOT), emit=self.f.emitted), \
                patch.object(github_identity, "configured_bot_logins", return_value=[BOT]), \
                patch.object(github_comment, "comment", autospec=True, side_effect=self.post):
            PLAN.cmd_release(self.args)

    @staticmethod
    def candidate_pr(
        number: int,
        *,
        state: str = "open",
        author: str = BOT,
        created_at: str = "2026-10-01T01:00:00Z",
        body: str = "Refs #42\nCloses #43",
        ref: str = "work/neighbor",
        repo: str = "owner/repo",
        merged_at: str | None = None,
    ) -> dict[str, Any]:
        pull = fixtures._pull_fixture(number, ref, state=state, author=author, repo=repo, body=body)
        pull.update(created_at=created_at, html_url=f"https://github.com/{repo.lower()}/pull/{number}")
        if merged_at is not None:
            pull["merged_at"] = merged_at
        return pull

    def add_timeline_pr(self, pull: dict[str, Any], repo: str) -> None:
        self.f.closed_pulls[pull["number"]] = pull
        self.f.timelines["/repos/owner/repo/issues/42/timeline"] = [{
            "event": "cross-referenced",
            "source": {"issue": {
                "number": pull["number"],
                "state": pull["state"],
                "created_at": pull["created_at"],
                "user": {"login": pull["user"]["login"]},
                "pull_request": {"url": f"https://api.github.com/repos/{repo}/pulls/{pull['number']}"},
            }},
        }]

    def setup_inherited_claim(self, handoff_body: str, **current_extra: str) -> dict[str, str]:
        predecessor = {**fixtures.OTHER, "session": "predecessor", "branch": "work/predecessor",
                       "claimed_at": "2026-09-28T00:00:00Z"}
        current = {**fixtures.OTHER, "resume_from": "11",
                   "retained_handoff": CLAIM.handoff_url("owner/repo", 42, 13), **current_extra}
        self.f.comments = [
            self.comment(11, CLAIM.marker(predecessor), predecessor["claimed_at"]),
            self.comment(13, handoff_body, "2026-09-30T00:00:00Z"),
            self.comment(1, CLAIM.marker(current), current["claimed_at"]),
        ]
        return predecessor

    def test_release_posts_the_exact_directive_with_handoff_below_it(self) -> None:
        self.run_own_release()
        result = self.f.emitted.call_args.args[0]
        self.assertTrue(result["ok"])
        posted = self.f.comments[-1]
        self.assertEqual(CLAIM.released_claim_id(posted["body"]), 1)
        self.assertTrue(release.related_sessions_match(posted["body"], self.args.session))
        self.assertFalse(release.related_sessions_match(posted["body"], "other-" + self.args.session))
        self.assertIn("LP4214-C1 Finish Line met", posted["body"])
        self.assertTrue(release.exact_source_release(1, posted, self.f.comments))
        self.assertEqual(result["release_comment"]["id"], posted["id"])
        posts = self.f.events.count("post")
        self.args.next_actor = None
        self.handoff.write_text("No new handoff.")
        self.run_own_release()
        self.assertEqual(self.f.events.count("post"), posts)
        self.assertEqual(self.f.emitted.call_args.args[0]["release_state"], "already_released")

    def test_release_on_a_reporter_envelope_posts_status_without_editing_the_body(self) -> None:
        self.f.issue["user"] = {"login": "outside-reporter"}
        self.f.issue["body"] = PLAN.github_plan_body.wrap_contributor_plan(
            {"body": "Original report"}, "## Current Status\n\nNext actor: agent\nNext action: Investigate.")
        envelope = self.f.issue["body"]
        with patch.multiple(PLAN, next_status_body_time=Mock(return_value="2026-01-01T00:00:00+00:00")):
            self.run_own_release()
        self.assertTrue(self.f.emitted.call_args.args[0]["ok"])
        self.assertEqual(self.f.issue["body"], envelope)
        status = PLAN.section_map(self.f.comments[-1]["body"])["Current Status"]
        self.assertEqual(PLAN.github_plan_next_actor.read(status), self.args.next_actor)

    def test_release_keeps_legacy_actor_narrative(self) -> None:
        self.f.issue["body"] += "\nNext actor: Supervisor routes the remaining source.\n"
        self.handoff.write_text("Next actor: Supervisor takes the next source step.")
        self.run_own_release()
        status = PLAN.section_map(self.f.issue["body"])["Current Status"]
        self.assertEqual(PLAN.github_plan_next_actor.read(status), self.args.next_actor)
        self.assertIn("Supervisor routes the remaining source.", status)
        self.assertIn("Supervisor takes the next source step.", self.f.comments[-1]["body"])

    def test_release_requires_actor_and_updates_status_before_releasing(self) -> None:
        self.args.next_actor = None
        self.handoff.write_text("Next action: Continue source work.")
        with self.assertRaises(PLAN.PlanError):
            self.run_own_release()
        self.assertNotIn("post", self.f.events)
        self.args.next_actor = "agent:integrator"
        original = self.f.issue["body"]
        self.run_own_release()
        status = PLAN.section_map(self.f.issue["body"])["Current Status"]
        self.assertEqual(PLAN.github_plan_next_actor.read(status), self.args.next_actor)
        self.assertEqual(PLAN.github_plan_next_actor.write(status, "agent:integrator"), status)
        self.assertIn(original.split("## Current Status")[0], self.f.issue["body"])
        self.assertLess(self.f.events.index("status"), self.f.events.index("post"))

    def test_status_failure_keeps_claim_owned(self) -> None:
        with patch.object(self.f, "edit", side_effect=PLAN.PlanError("Status write failed")):
            with self.assertRaises(PLAN.PlanError):
                self.run_own_release()
        self.assertNotIn("post", self.f.events)

    def test_preserved_status_actor_agrees_with_normal_routing(self) -> None:
        for markup in ("<details>\nNext actor: agent:old-owner\n</details>",
                       "> Prior handoff\nNext actor: person:example",
                       "> Prior handoff\nNext actor: person:example\n\nNext actor: agent:old-owner"):
            with self.subTest(markup=markup):
                self.f.doCleanups()
                self.setUp()
                self.addCleanup(self.f.doCleanups)
                self.f.issue["body"] += "\n" + markup + "\n"
                self.run_own_release()
                status = PLAN.github_next_queue.current_status(
                    PLAN.github_direction_next.discussion_snapshot(self.f.issue, self.f.comments, complete=True),
                    {BOT.casefold()})
                self.assertEqual(PLAN.github_plan_next_actor.read(status), self.args.next_actor)
                self.assertIn(markup.replace("agent:old-owner", self.args.next_actor), self.f.issue["body"])

    def test_release_preserves_literal_brief_and_neighbor_sections(self) -> None:
        brief = ("Brief:\n```text\nGUI task: example.app\n## Scope\nNext actor: person:example\n"
                 + CLAIM.marker(fixtures.OWNER) + "\n```")
        scope = "## Scope\n\nKeep the neighboring section.\n"
        for from_comment in (False, True):
            for dry_run in (False, True):
                with self.subTest(from_comment=from_comment, dry_run=dry_run):
                    self.f.doCleanups()
                    self.setUp()
                    self.addCleanup(self.f.doCleanups)
                    self.args.dry_run = dry_run
                    self.f.issue["body"] += "\nNext actor: agent:old-owner\n"
                    self.f.issue["last_edited_at"] = "2026-10-01T00:00:00Z"
                    if from_comment:
                        self.f.comments.append(self.comment(99, "## Current Status\nNext actor: agent:old-owner\n" + brief,
                                                            "2026-10-02T00:00:00Z"))
                    else:
                        self.f.issue["body"] += "\n" + brief + "\n"
                    self.f.issue["body"] += "\n" + scope
                    self.run_own_release()
                    body = self.f.emitted.call_args.args[0]["status_body"] if dry_run else self.f.issue["body"]
                    self.assertIn(brief, body)
                    self.assertIn(scope.strip(), body)
                    status = PLAN.section_map(body)["Current Status"]
                    self.assertEqual(PLAN.github_plan_next_actor.read(status), self.args.next_actor)
                    self.assertEqual(CLAIM.records(status), [])

    def test_pr_release_keeps_body_and_publishes_status_with_actor(self) -> None:
        self.f.issue["pull_request"] = {}
        self.f.issue["body"] += "\nNext actor: agent:old-owner\n"
        original = self.f.issue["body"]
        with patch.object(PLAN, "api_json", side_effect=self.f.read_api):
            self.run_own_release()
        self.assertEqual(self.f.issue["body"], original)
        self.assertTrue(release.related_sessions_match(self.f.comments[-1]["body"], self.args.session))
        self.assertFalse(release.related_sessions_match(self.f.comments[-1]["body"], "other-" + self.args.session))
        status = PLAN.section_map(self.f.comments[-1]["body"])["Current Status"]
        self.assertEqual(PLAN.github_plan_next_actor.read(status), "agent:integrator")
        self.assertEqual(CLAIM.records(self.f.comments[-1]["body"]), [])
        self.assertEqual(self.f.comments[-1]["body"].count("LP4214-C1 Finish Line met"), 1)

    def test_release_dry_run_previews_status_and_body_without_writing(self) -> None:
        self.args.dry_run = True
        self.run_own_release()
        result = self.f.emitted.call_args.args[0]
        status = PLAN.section_map(result["status_body"])["Current Status"]
        self.assertEqual(PLAN.github_plan_next_actor.read(status), self.args.next_actor)
        self.assertEqual(CLAIM.released_claim_id(result["release_body"]), 1)
        self.f.assert_no_writes()

    def test_supplement_does_not_claim_later_successor_prs(self) -> None:
        self.run_own_release()
        earlier = self.candidate_pr(99)
        later = self.candidate_pr(120, created_at="2026-10-04T00:00:00Z", body="Closes #42", ref="work/successor")
        self.f.pulls = [earlier, later]
        self.args.exclude_pr = [earlier["html_url"]]
        self.run_own_release()
        self.assertNotIn(120, CLAIM.handoff_pr_numbers(self.f.comments[-1]["body"], issue_repo="owner/repo", target_repo="owner/repo"))

    def test_handoff_requires_qualified_references_before_release(self) -> None:
        for text in ("Read owner/other#550 after landing, then confirm #550.", "Read PR550 after landing.",
                     "Do the rank &#35;1 item first.", "Verify -#77.", "Verify .#77."):
            self.handoff.write_text(text)
            with self.assertRaises(PLAN.PlanError):
                self.run_own_release()
            self.assertNotIn("post", self.f.events)
        self.handoff.write_text("Read owner/other#550 after landing, then confirm owner/other#550 and owner/repo#42.")
        self.run_own_release()
        self.assertIn(self.handoff.read_text(), self.f.comments[-1]["body"])

    def test_claim_names_the_release_command_for_its_own_comment(self) -> None:
        self.f.comments = []
        self.f.run_claim()
        result = self.f.emitted.call_args.args[0]
        argv = shlex.split(result["release_command"])
        parsed = PLAN.build_parser().parse_args(argv[argv.index("--repo"):])
        self.assertIs(parsed.func, PLAN.cmd_release)
        self.assertEqual((parsed.claim_comment, parsed.session),
                         (result["claim_comment"]["comment"]["id"], result["claim"]["session"]))
        status = PLAN.section_map(self.f.issue["body"])["Current Status"]
        self.assertEqual(PLAN.github_plan_next_actor.read(status), f"agent:{result['claim']['session']}")

        posted = next(c for c in self.f.comments if c["id"] == result["claim_comment"]["comment"]["id"])
        self.assertIn(result["claim"]["session"], re.sub(r"<!--.*?-->", "", posted["body"], flags=re.S))

    def test_release_discovers_other_branch_prs_from_source_links(self) -> None:
        self.f.pulls = [self.candidate_pr(99, body="Refs #42", ref="work/another-branch")]
        cross_repo = self.candidate_pr(77, body="Refs owner/repo#42", ref="work/another-branch", repo="owner/other")
        self.add_timeline_pr(cross_repo, "owner/other")
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

    def test_release_does_not_name_unrelated_foreign_preclaim_or_train_prs(self) -> None:
        self.f.pulls = [
            self.candidate_pr(98, body="Unrelated", ref="work/other"),
            self.candidate_pr(99, author="someone-else", body="Refs #42", ref="work/other"),
            self.candidate_pr(100, created_at="2026-09-30T01:00:00Z", body="Refs #42", ref="work/other"),
            self.candidate_pr(101, body=CLAIM.TRAIN_BATCH_MARKER + "batch:sha -->\n\nCloses #42",
                              ref=CLAIM.TRAIN_BRANCH_PREFIX + "owner/repo/main/batch"),
        ]
        self.run_own_release()
        for pull in self.f.pulls:
            self.assertNotIn(pull["html_url"], self.f.comments[-1]["body"])

    def test_release_excludes_other_sessions_prs_without_transferring_ownership(self) -> None:
        pull = self.candidate_pr(99, body="Parent: https://github.com/owner/repo/issues/42\nCloses #43", ref="work/other-session")
        self.f.pulls = [pull]
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_own_release()
        self.assertEqual(caught.exception.code, "release_pr_attribution_required")
        self.f.assert_no_writes()
        self.args.exclude_pr = [pull["html_url"]]
        self.run_own_release()
        self.assertNotIn(99, CLAIM.handoff_pr_numbers(self.f.comments[-1]["body"], issue_repo="owner/repo", target_repo="owner/repo"))
        self.assertEqual(release.excluded_prs(self.f.comments, self.f.comments[0], issue_repo="owner/repo"), {pull["html_url"]})

    def test_released_source_can_add_checked_exclusions_without_rewriting_its_release(self) -> None:
        self.run_own_release()
        original = copy.deepcopy(self.f.comments[-1])
        pull = self.candidate_pr(99)
        self.f.pulls = [pull]
        self.args.exclude_pr = [pull["html_url"]]
        self.run_own_release()
        self.assertEqual(self.f.comments[-2], original)
        released = CLAIM.released_source(self.f.comments, self.args.claim_comment)
        self.assertEqual(CLAIM.handoff_candidates(self.f.comments, released)[-1], self.f.comments[-1])
        self.assertEqual(release.excluded_prs(self.f.comments, self.f.comments[0], issue_repo="owner/repo"), {pull["html_url"]})
        posts = self.f.events.count("post")
        self.f.pulls = []
        self.run_own_release()
        self.assertEqual(self.f.events.count("post"), posts)

    def test_supplement_cannot_discard_previously_retained_work_after_posting(self) -> None:
        pull = self.candidate_pr(99)
        self.f.pulls = [pull]
        self.args.retained_pr = [pull["html_url"]]
        self.run_own_release()
        before = copy.deepcopy(self.f.comments)
        self.args.retained_pr = []
        self.args.exclude_pr = [pull["html_url"]]
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_own_release()
        self.assertEqual(caught.exception.code, "release_excluded_pr_mentioned")
        self.assertEqual(self.f.comments, before)

    def test_cross_repo_exclusion_does_not_reinterpret_legacy_local_references(self) -> None:
        self.run_own_release()
        self.f.comments[-1]["body"] += "\n\nContext: PR7"
        self.f.comments[-1]["body"] += "\n\n" + release.exclusion_marker(1, {"https://github.com/other/lib/pull/7"})
        self.assertEqual(release.excluded_prs(self.f.comments, self.f.comments[0], issue_repo="owner/repo"),
                         {"https://github.com/other/lib/pull/7"})

    def test_malformed_prior_attribution_stops_with_a_structured_error_before_posting(self) -> None:
        self.run_own_release()
        self.f.comments[-1]["body"] += "\n\n<!-- " + release.EXCLUSIONS_MARKER + "invalid -->"
        before = copy.deepcopy(self.f.comments)
        self.args.retained_pr = ["https://github.com/owner/repo/pull/99"]
        with self.assertRaises(PLAN.PlanError) as caught:
            self.run_own_release()
        self.assertEqual(caught.exception.payload["completed_steps"], ["release_preflight"])
        self.assertEqual(self.f.comments, before)

    def test_release_refuses_unverified_or_owned_pr_declarations(self) -> None:
        cases = (
            ("unverified_excluded", "release_excluded_pr_unverified"),
            ("unverified_declared", "release_declared_pr_unverified"),
            ("owned_source_branch", "release_owned_pr_not_excludable"),
        )
        for mode, expected_code in cases:
            with self.subTest(mode=mode):
                self.setUp()
                if mode == "unverified_excluded":
                    self.args.exclude_pr = ["https://github.com/owner/repo/pull/999"]
                elif mode == "unverified_declared":
                    self.args.retained_pr = ["https://github.com/owner/repo/pull/99"]
                else:
                    self.f.pulls = [self.candidate_pr(99, ref=fixtures.OTHER["branch"])]
                    self.args.exclude_pr = ["https://github.com/owner/repo/pull/99"]
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_own_release()
                self.assertEqual(caught.exception.code, expected_code)
                self.f.assert_no_writes()

    def test_release_never_uses_incidental_narrative_as_pr_attribution(self) -> None:
        for created in ("2026-10-01T01:00:00Z", "2026-09-29T00:00:00Z"):
            with self.subTest(created=created):
                self.setUp()
                self.f.pulls = [self.candidate_pr(99, created_at=created, ref="work/other-session")]
                self.handoff.write_text("Left owner/repo#99 alone. It belongs to another session.")
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
        self.f.pulls = [self.candidate_pr(99, body="Refs #42", ref="work/other", repo="Owner/Repo")]
        self.args.retained_pr = ["https://github.com/owner/repo/pull/99"]
        self.run_own_release()
        self.assertIn(99, CLAIM.handoff_pr_numbers(self.f.comments[-1]["body"], issue_repo="owner/repo", target_repo="owner/repo"))

    def test_release_checks_older_cross_repo_and_merged_pr_narrative_references(self) -> None:
        self.handoff.write_text("Left owner/other#77 alone.")
        cross_pull = self.candidate_pr(77, created_at="2026-09-29T00:00:00Z", body="Refs owner/repo#42",
                                       ref="work/other-session", repo="owner/other")
        self.add_timeline_pr(cross_pull, "owner/other")
        with patch.multiple(PLAN, api_json=self.f.read_api), self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_own_release()
        self.assertEqual(caught.exception.code, "release_pr_attribution_required")
        self.f.assert_no_writes()
        self.args.retained_pr = ["https://github.com/Owner/Other/pull/77"]
        self.handoff.write_text("Handing off the explicitly declared PR.")
        with patch.multiple(PLAN, api_json=self.f.read_api):
            self.run_own_release()
        self.assertIn(77, CLAIM.handoff_pr_numbers(self.f.comments[-1]["body"], issue_repo="owner/repo", target_repo="owner/other"))
        self.setUp()
        self.handoff.write_text("Rebased on owner/repo#77 from another session.")
        merged_pull = self.candidate_pr(77, state="closed", merged_at="2026-10-01T01:00:00Z",
                                        created_at="2026-09-29T00:00:00Z", ref="work/other-session")
        self.add_timeline_pr(merged_pull, "owner/repo")
        with patch.multiple(PLAN, api_json=self.f.read_api), self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_own_release()
        self.assertEqual(caught.exception.code, "release_pr_attribution_required")
        self.f.assert_no_writes()

    def test_release_classifies_missing_or_stripped_inherited_proof(self) -> None:
        url = CLAIM.handoff_url("owner/repo", 42, 12)
        self.f.comments[0]["body"] = CLAIM.marker({**fixtures.OTHER, "resume_from": "11", "retained_handoff": url})
        for declared in ([], ["https://github.com/owner/repo/pull/99"]):
            with self.subTest(declared=declared):
                self.args.retained_pr = declared
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_own_release()
                self.assertEqual(caught.exception.code, "release_inherited_handoff_changed")
                self.assertEqual(caught.exception.payload["retained_handoff"], url)
                self.assertEqual(caught.exception.payload["claim_comment"], self.args.claim_comment)
                self.f.assert_no_writes()
        self.setUp()
        self.setup_inherited_claim("Released claim 11\n\nFinished work.")
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_own_release()
        self.assertEqual(caught.exception.code, "release_inherited_handoff_changed")
        self.f.assert_no_writes()

    def test_release_does_not_relabel_transient_inherited_reads_as_lost_proof(self) -> None:
        failure = PLAN.PlanError("Read deadline expired")
        with patch.object(PLAN, "read_release_inherited_prs", side_effect=failure):
            with self.assertRaises(PLAN.PlanError) as caught:
                self.run_own_release()
        self.assertIs(caught.exception, failure)
        self.f.assert_no_writes()

    def test_release_requalifies_inherited_local_source_without_requiring_a_future_pr(self) -> None:
        for changed in (False, True):
            with self.subTest(changed=changed):
                self.setUp()
                head = "a" * 40
                predecessor = self.setup_inherited_claim(
                    f"Released claim 11\n\nSource: `work/predecessor` at **{head}**",
                    retained_source_head=head,
                )
                self.f.comments.insert(1, self.comment(12, f"Released claim {self.f.comments[0]['id']}", "2026-09-29T00:00:00Z"))
                self.f.inventory.update(local_branches=[], remote_branches=[predecessor["branch"]], worktrees=[])
                if changed:
                    self.f.comments[2]["body"] = self.f.comments[2]["body"].replace(head, "b" * 40)
                before = copy.deepcopy(self.f.inventory)
                with patch.object(github_plan_claim, "local_inventory", side_effect=ValueError("Run claim from its repository")):
                    if changed:
                        with self.assertRaises(PLAN.ClassifiedPlanError) as stopped:
                            self.run_own_release()
                        self.assertEqual(stopped.exception.code, "release_inherited_handoff_changed")
                        self.f.assert_no_writes()
                    else:
                        self.run_own_release()
                        self.assertTrue(self.f.emitted.call_args.args[0]["ok"])
                self.assertEqual(self.f.inventory, before)

    def test_release_carries_inherited_prs_created_before_this_claim(self) -> None:
        self.setup_inherited_claim("Released claim 11\n\nRetained PR #99.")
        self.f.pulls = [self.candidate_pr(99, created_at="2026-09-29T00:00:00Z", body="Refs #42", ref="work/retained")]
        self.f.closed_pulls[99] = self.f.pulls[0]
        with patch.object(PLAN, "api_json", side_effect=self.f.read_api):
            self.run_own_release()
        self.assertIn(99, CLAIM.handoff_pr_numbers(self.f.comments[-1]["body"], issue_repo="owner/repo", target_repo="owner/repo"))

    def test_release_discovery_uses_server_clock_and_skips_only_missing_prs(self) -> None:
        self.f.comments[0]["body"] = CLAIM.marker({**fixtures.OTHER, "claimed_at": "2026-10-01T01:00:00Z"})
        self.f.pulls = [self.candidate_pr(99, created_at="2026-10-01T00:01:00Z", body="Refs #42", ref="work/new-branch")]
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

    def test_release_refuses_records_this_session_does_not_own_without_writing(self) -> None:
        cases: dict[str, Callable[[], Any]] = {
            "other session": lambda: setattr(self.args, "session", fixtures.OWNER["session"]),
            "missing claim": lambda: setattr(self.args, "claim_comment", 7),
            "foreign author": lambda: self.f.comments[0].update(user={"login": "foreign[bot]"}),
            "not native": lambda: os.environ.update(CLAUDE_CODE_SESSION_ID="another-native"),
            "other family's session": lambda: (setattr(self.args, "agent", "codex"),
                                               os.environ.update(CODEX_THREAD_ID="codex-thread")),
            "receipt in handoff": lambda: self.handoff.write_text(
                "Done.\n<!-- " + release.RELEASE_MARKER + "{} -->\n"),
            "exclusion receipt in handoff": lambda: self.handoff.write_text(
                release.exclusion_marker(1, {"https://github.com/owner/repo/pull/99"})),
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
