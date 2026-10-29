#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise discovery, exact bindings and refusal-to-publish with synthetic PRs and real Git."""

import copy
import contextlib
from concurrent.futures import ThreadPoolExecutor
import errno
from io import StringIO
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pending_reviews as reviews
import review_notifications as notifications
import session_record
import github_api
from github_identity import OWN_USER_OPT_IN
import test_review_with_model as model_fixture
from github_plan_claim import marker
from ready_handoff import review_result_body

REPO = "example/tool"
BOT = "review-bot[bot]"
TRUSTED = {BOT}
SHA = "a" * 40
BRANCH = "work/change"
SESSION = "native-session"
STAMP = "2026-10-17T20:00:00Z"


def post_payload(*, ok=True, write_outcome: str | None = None, reconciled=False):
    failure = None if ok else github_api.FailureDetail(
        cause="fixture", message="delivery failed", retryable=False, fallback_eligible=False,
        disposition="stop", write_outcome=write_outcome)
    result = github_api.ApiResult(ok=ok, status=200 if ok else 500, body={}, failure=failure)
    return {"ok": ok, "outcome_certainty": github_api._outcome_certainty(
        result, is_write=True, reconciliation={"result": "matched"} if reconciled else None)}


def comment(ident, body, login=BOT) -> dict[str, Any]:
    return {"id": ident, "body": body, "user": {"login": login}, "created_at": STAMP,
            "updated_at": STAMP, "issue_url": f"https://api.github.com/repos/{REPO}/issues/7"}


def ask() -> dict[str, Any]:
    return comment(10, f"Supervisor runs the review.\nHead: {SHA}\n\nPrompt:\n\n```text\n"
                      "Review the changed helper. Give a realistic trigger and severity, or none.\n```\n")


def pull() -> dict[str, Any]:
    return {"number": 7, "state": "open", "head": {"sha": SHA, "ref": BRANCH, "repo": {"full_name": REPO}}}


def entry():
    return {"session_id": SESSION, "repository": REPO, "issue": 3,
            "supervisor_owned": True, "harness": "codex"}


class FakeIO:
    def __init__(self):
        self.pr = pull()
        self.request = ask()
        self.comments = [self.request]
        self.sources = {10: self.request}
        self.claims = [comment(5, marker({"branch": BRANCH, "session": SESSION, "worker": "worker",
                                        "claimed_at": STAMP}))]
        self.calls = []
        self.after_review = lambda: None
        self.review_error = None
        self.review_text = "No blocking findings."
        self.extra_pulls = []

    def open_pulls(self, repos):
        return {repo: self.paged_json(f"/repos/{repo}/pulls", step_prefix="open_prs", params={"state": "open"}) for repo in repos}

    def get_json(self, path, **kwargs):
        if "/issues/comments/" in path:
            return copy.deepcopy(self.sources[int(path.rsplit("/", 1)[1])])
        return copy.deepcopy(self.pr)

    def paged_json(self, path, **kwargs):
        self.calls.append(("read", path, kwargs))
        if kwargs["step_prefix"] in {"author_claims", "verify_author_claims"}:
            return copy.deepcopy(self.claims)
        if path.endswith("/pulls"):
            return [self.pr, *self.extra_pulls]
        return copy.deepcopy(self.comments)

    def review(self, author, worktree, prompt, output):
        self.calls.append(("review", author, prompt.read_text()))
        if self.review_error:
            raise self.review_error
        output.write_text(self.review_text)
        self.after_review()
        return {"ok": True, "provider": "anthropic" if author == "openai" else "openai",
                "model": "test-model", "model_source": "reported by the CLI"}

    def post(self, repo, number, body):
        self.calls.append(("post", repo, number, body.read_text()))
        self.comments.append(comment(11, body.read_text()))
        return post_payload()

    def notify(self, entry, message):
        self.calls.append(("notify", copy.deepcopy(entry), message))


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.evidence = Path(self.temp.name) / "evidence"
        self.io = FakeIO()
        self.entries = [entry()]
        self.find = patch.object(reviews, "find_worktree", return_value=Path(self.temp.name))
        self.check = patch.object(reviews, "check_worktree")
        git_patch = patch.object(reviews, "git", return_value=BRANCH)
        git_patch.start()
        self.addCleanup(git_patch.stop)
        self.find.start()
        self.check.start()
        self.addCleanup(self.find.stop)
        self.addCleanup(self.check.stop)

    def dispatch(self, apply=True):
        return reviews.dispatch(self.io, REPO, 7, self.entries, Path(self.temp.name), TRUSTED, self.evidence, apply)

    def assert_no_post(self):
        self.assertFalse(any(c[0] == "post" for c in self.io.calls))

    def test_posted_review_notifies_exact_ledger_author_once_per_report_version(self):
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                self.io = FakeIO()
                self.entries = [{**entry(), "harness": harness}]
                self.evidence = Path(self.temp.name) / harness
                self.dispatch(apply=False)
                self.assertFalse(any(c[0] == "notify" for c in self.io.calls))
                self.assertEqual(self.dispatch()["notification_status"], "sent")
                self.assertEqual(self.dispatch()["notification_status"], "sent")
                sends = [c for c in self.io.calls if c[0] == "notify"]
                self.assertEqual(len(sends), 1)
                self.assertEqual(sends[0][1], self.entries[0])
                self.assertIn(SHA, sends[0][2])
                self.io.comments[-1]["updated_at"] = "2026-10-17T21:00:00Z"
                self.dispatch()
                self.assertEqual(sum(c[0] == "notify" for c in self.io.calls), 2)
                self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)

    def test_notification_failure_or_interruption_keeps_completion_and_never_replays(self):
        from iterm_tab import SendFailure
        for failure in (ValueError("queue refused"), PermissionError("permission denied"),
                        SendFailure("terminal", "unknown", "fixture"),
                        subprocess.TimeoutExpired("send", 30), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                self.io = FakeIO()
                self.evidence = Path(self.temp.name) / type(failure).__name__
                with patch.object(self.io, "notify", side_effect=failure) as send:
                    if isinstance(failure, KeyboardInterrupt):
                        with self.assertRaises(KeyboardInterrupt):
                            self.dispatch()
                    else:
                        report = self.dispatch()
                        self.assertEqual(report["status"], "posted")
                        self.assertIn(str(failure), report["notification_error"])
                        self.assertTrue(report["needs_attention"])
                    recovered = self.dispatch()
                    self.assertEqual(recovered["status"], "already_completed")
                    self.assertEqual(recovered["notification_status"], "blocked")
                    self.assertTrue(recovered["completion_comment"])
                    self.assertEqual(send.call_count, 1)
                    if isinstance(failure, PermissionError):
                        Path(recovered["notification_journal"]).write_text("[]")
                        self.assertEqual(self.dispatch()["notification_status"], "blocked")
                        self.assertEqual(send.call_count, 1)

    def test_preflight_not_sent_is_rechecked_until_delivery_and_then_deduplicated(self):
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                self.io = FakeIO()
                self.entries = [{**entry(), "harness": harness}]
                self.evidence = Path(self.temp.name) / harness
                with patch.object(self.io, "notify", side_effect=notifications.NotSent("busy", needs_attention=False)) as send:
                    result = self.dispatch()
                    self.assertEqual(result["notification_status"], "pending")
                    self.assertFalse(result["needs_attention"])
                    self.assertEqual(json.loads(Path(result["notification_journal"]).read_text())["phase"], "not_sent")
                    pending = self.dispatch(apply=False)
                    self.assertEqual(pending["notification_error"], result["notification_error"])
                    self.assertFalse(pending["needs_attention"])
                    self.io.request["updated_at"] = "2026-10-17T21:00:00Z"
                    self.assertTrue(self.dispatch(apply=False)["needs_attention"])
                    self.io.request["updated_at"] = STAMP
                    self.assertEqual(send.call_count, 1)
                self.assertEqual(self.dispatch()["notification_status"], "sent")
                self.assertEqual(self.dispatch()["notification_status"], "sent")
                self.assertEqual(sum(c[0] == "notify" for c in self.io.calls), 1)
                self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)

    def test_delivered_report_versions_survive_retirement_without_erasing_provenance_or_retargeting(self):
        for mode in ("generated", "manual", "legacy"):
            for moved in (False, True):
                with self.subTest(mode=mode, moved=moved):
                    self.io = FakeIO()
                    self.io.pr["body"] = "Closes #3"
                    self.entries = [entry()]
                    self.evidence = Path(self.temp.name) / f"{mode}-{moved}"
                    if mode != "generated":
                        body = f"{reviews.REPORT_HEADING}\nHead: {SHA}\nRetained review."
                        if mode == "legacy":
                            body += "\n<!-- supervisor-review " + json.dumps(reviews.receipt_for(self.io.request, SHA)) + " -->"
                        self.io.comments.append(comment(11, body))
                    first = self.dispatch()
                    self.assertEqual(first["notification_status"], "sent")
                    if moved:
                        self.io.pr["head"]["sha"] = "b" * 40
                    for entries in ([], [entry(), entry()], [{**entry(), "supervisor_owned": False}],
                                    [{**entry(), "repository": "another/repo"}],
                                    [{**entry(), "session_id": "successor-native"}],
                                    [{**entry(), "pid": 7, "handoff_url": "https://github.com/example/tool/issues/3#issuecomment-1"}]):
                        self.entries = entries
                        result = self.dispatch()
                        self.assertEqual(result["notification_status"], "sent")
                        self.assertEqual(result["author_session"], first["author_session"])
                        self.assertEqual(result["author_provider"], first["author_provider"])
                        self.assertEqual(result["requested_provider_confirmed"], first["requested_provider_confirmed"])
                        self.assertEqual(result["needs_attention"], first["needs_attention"])
                    self.assertEqual(sum(c[0] == "notify" for c in self.io.calls), 1)

    def test_completed_review_never_notifies_missing_or_ambiguous_ledger_author(self):
        with patch.object(self.io, "notify", side_effect=notifications.NotSent("busy")):
            self.dispatch()
        for entries in ([], [entry(), entry()], [{**entry(), "supervisor_owned": False}],
                        [{**entry(), "repository": "another/repo"}], [{**entry(), "session_id": "successor-native"}]):
            with self.subTest(entries=entries):
                self.entries = entries
                with patch.object(self.io, "notify") as send:
                    self.assertEqual(self.dispatch()["notification_status"], "blocked")
                    send.assert_not_called()

    def test_post_readback_failure_preserves_confirmed_post_and_reports_notification_error(self):
        pages = self.io.paged_json
        def readback(path, **kwargs):
            if kwargs.get("step_prefix") == "posted_review_comments":
                raise ValueError("posted comment readback unavailable")
            return pages(path, **kwargs)
        with patch.object(self.io, "paged_json", side_effect=readback), patch.object(self.io, "notify") as send:
            result = self.dispatch()
        self.assertEqual(result["status"], "posted")
        self.assertEqual(result["notification_error"], "posted comment readback unavailable")
        self.assertTrue(result["post"]["ok"])
        self.assertNotIn("notification_journal", result)
        send.assert_not_called()
        self.assertEqual(self.dispatch()["notification_status"], "sent")
        self.assertEqual(sum(c[0] == "post" for c in self.io.calls), 1)

    def test_paid_history_recovers_post_before_notification_reservation_after_retirement(self):
        self.io.pr["body"] = "Closes #3"
        pages = self.io.paged_json
        def readback(path, **kwargs):
            if kwargs.get("step_prefix") == "posted_review_comments":
                raise ValueError("readback failed")
            return pages(path, **kwargs)
        with patch.object(self.io, "paged_json", side_effect=readback):
            self.assertEqual(self.dispatch()["status"], "posted")
        self.entries = []
        # Legacy paid files predate explicit repository metadata.
        paid = next(self.evidence.glob("*.json"))
        state = json.loads(paid.read_text())
        state.pop("repository")
        state.pop("pr")
        paid.write_text(json.dumps(state))
        for moved in (False, True):
            if moved:
                self.io.pr["head"]["sha"] = "b" * 40
            result = reviews.sweep(self.io, [], {}, TRUSTED, self.evidence, apply=True)[0]
            self.assertEqual(result["author_session"], SESSION)
            self.assertTrue(result["needs_attention"])
            self.assertEqual(result["notification_status"], "blocked")
        self.assertFalse(any(c[0] == "notify" for c in self.io.calls))

    def test_edited_reports_keep_original_author_across_successor_claims_and_conflicting_receipts(self):
        for mode in ("generated", "manual", "legacy"):
            with self.subTest(mode=mode):
                self.io, self.entries = FakeIO(), [entry()]
                self.evidence = Path(self.temp.name) / mode
                if mode != "generated":
                    body = f"{reviews.REPORT_HEADING}\nHead: {SHA}\nRetained review."
                    if mode == "legacy":
                        body += "\n<!-- supervisor-review " + json.dumps(reviews.receipt_for(self.io.request, SHA)) + " -->"
                    self.io.comments.append(comment(11, body))
                self.assertEqual(self.dispatch()["notification_status"], "sent")
                release = comment(6, "Released claim 5")
                release["created_at"] = release["updated_at"] = "2026-10-17T21:00:00Z"
                successor = comment(7, marker({"branch": BRANCH, "session": "successor", "worker": "next",
                                               "claimed_at": "2026-10-17T22:00:00Z"}))
                successor["created_at"] = successor["updated_at"] = "2026-10-17T22:00:00Z"
                self.io.claims += [release, successor]
                self.entries = [{**entry(), "session_id": "successor"}]
                report = self.io.comments[-1]
                report["updated_at"] = "2026-10-17T23:00:00Z"
                result = self.dispatch()
                self.assertEqual(result["author_session"], SESSION)
                self.assertEqual(result["notification_status"], "blocked")
                self.assertTrue(result["needs_attention"])
                if mode == "generated":
                    report["body"] = report["body"].replace(SESSION, "successor")
                    result = self.dispatch()
                    self.assertEqual(result["notification_status"], "blocked")
                    self.assertTrue(result["notification_error"])
                self.assertEqual(sum(c[0] == "notify" for c in self.io.calls), 1)

    def test_retained_notification_versions_remain_discoverable_and_spent_versions_cannot_be_replayed_by_edit(self):
        for phase in ("sent", "not_sent", "failed", "attempted", "unreadable"):
            with self.subTest(phase=phase):
                self.io, self.entries = FakeIO(), [entry()]
                self.evidence = Path(self.temp.name) / phase
                self.io.comments.append(comment(11, f"{reviews.REPORT_HEADING}\nHead: {SHA}\nManual review."))
                first = self.dispatch()
                journal = Path(first["notification_journal"])
                state = json.loads(journal.read_text())
                state.update(phase=phase, error="retained fixture diagnostic", needs_attention=True)
                journal.write_text("{broken" if phase == "unreadable" else json.dumps(state))
                result = reviews.sweep(self.io, [], {}, TRUSTED, self.evidence, apply=True)[0]
                self.assertEqual(result["notification_status"], "sent" if phase == "sent" else "blocked")
                if phase != "sent":
                    self.assertTrue(result["needs_attention"])
                self.io.comments[-1]["updated_at"] = "2026-10-17T23:00:00Z"
                result = self.dispatch()
                if phase in {"failed", "attempted", "unreadable"}:
                    self.assertEqual(result["notification_status"], "blocked")
                    self.assertEqual(sum(c[0] == "notify" for c in self.io.calls), 1)
                else:
                    self.assertEqual(result["notification_status"], "sent")

    def test_unsent_configuration_failures_are_visible_recheckable_and_do_not_mask_busy_waits(self):
        for attention in (False, True):
            with self.subTest(attention=attention):
                self.io = FakeIO()
                self.evidence = Path(self.temp.name) / str(attention)
                with patch.object(self.io, "notify", side_effect=notifications.NotSent("preflight fixture", needs_attention=attention)):
                    result = self.dispatch()
                for report in (result, self.dispatch(apply=False)):
                    self.assertEqual(report["needs_attention"], attention)
                    self.assertEqual(report["notification_status"], "blocked" if attention else "pending")
                    self.assertEqual(report["notification_error"], "preflight fixture")
                self.assertEqual(self.dispatch()["notification_status"], "sent")

    def test_discovery_is_read_only_and_apply_reports_author_then_deduplicates(self):
        report = self.dispatch(apply=False)
        self.assertEqual(report["status"], "pending")
        self.assertFalse(any(c[0] == "review" for c in self.io.calls))
        report = self.dispatch()
        self.assertEqual(report["author_session"], SESSION)
        self.assertEqual(report["status"], "posted")
        self.assertTrue(report["notification_needed"])
        recovered = self.dispatch()
        self.assertEqual(recovered["status"], "already_completed")
        self.assertTrue(recovered["requested_provider_confirmed"])
        self.assertFalse(recovered["needs_attention"])
        self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)
        body = next(c[3] for c in self.io.calls if c[0] == "post")
        self.assertIsNone(reviews.review_receipt_head(body))
        self.assertEqual(self.evidence.joinpath(next(p.name for p in self.evidence.glob("*.md"))).read_text().strip(), self.io.review_text)

    def test_late_receipt_for_another_binding_cannot_complete_a_new_scope(self):
        original = ask()
        for phase in ("before", "during"):
            for mismatch in ("request", "head"):
                with self.subTest(phase=phase, mismatch=mismatch):
                    self.io = FakeIO()
                    self.evidence = Path(self.temp.name) / f"late-{phase}-{mismatch}"
                    self.io.request = comment(20, original["body"].replace("Review the changed helper.", "Review the new scope."))
                    self.io.comments = [original, self.io.request]
                    self.io.sources[20] = self.io.request
                    receipt = reviews.receipt_for(self.io.request, SHA)
                    receipt[mismatch] = {"request": original["id"], "head": "b" * 40}[mismatch]
                    late = comment(21, f"{reviews.REPORT_HEADING}\nHead: {SHA}\nOld scope result.\n"
                                   "<!-- supervisor-review " + json.dumps(receipt) + " -->")
                    if phase == "before":
                        self.io.comments.append(late)
                    else:
                        self.io.after_review = lambda: self.io.comments.append(late)
                    self.assertEqual(self.dispatch()["status"], "posted")
                    launched = [call for call in self.io.calls if call[0] == "review"]
                    self.assertEqual(len(launched), 1)
                    self.assertIn("Review the new scope.", launched[0][2])
                    recovered = self.dispatch()
                    self.assertEqual(recovered["request"], self.io.request["id"])
                    self.assertEqual(recovered["status"], "already_completed")
                    self.assertTrue(recovered["requested_provider_confirmed"])
                    self.assertEqual(sum(call[0] == "review" for call in self.io.calls), 1)

    def test_foreign_request_missing_claim_link_has_a_supported_binding_route(self):
        foreign = comment(5, marker({"branch": BRANCH, "session": "other-run",
                                    "worker": "other", "claimed_at": STAMP}))
        pages = self.io.paged_json
        def linked_claims(path, **kwargs):
            if kwargs.get("step_prefix") == "author_claims":
                return [foreign] if path.endswith("/issues/99/comments") else []
            return pages(path, **kwargs)
        self.io.paged_json = linked_claims
        checkouts = {}
        report = reviews.sweep(self.io, self.entries, checkouts, TRUSTED, self.evidence)
        self.assertEqual(report[0]["status"], "refused")
        self.io.pr["body"] = "Refs #99"
        report = reviews.sweep(self.io, self.entries, checkouts, TRUSTED, self.evidence)
        self.assertEqual(report[0]["status"], "not_owned")
        self.assertFalse(any(call[0] in {"review", "post"} for call in self.io.calls))

    def test_weighed_notes_cannot_hide_retained_or_uncertain_launch_details(self):
        for phase in ("review_completed", "post_blocked", "post_unknown"):
            with self.subTest(phase=phase):
                self.io = FakeIO()
                self.evidence = Path(self.temp.name) / phase
                self.io.post = Mock(side_effect=reviews.PostError("uncertain fixture write"))
                with self.assertRaises(reviews.PostError):
                    self.dispatch()
                journal = next(self.evidence.glob("*.json"))
                state = json.loads(journal.read_text())
                state["phase"] = phase
                journal.write_text(json.dumps(state))
                self.io.comments.append(comment(12, review_result_body("Other findings weighed.", SHA)))
                for acknowledged in (False, True):
                    if acknowledged:
                        self.io.comments.append(comment(13, review_result_body(
                            f"Supervisor request acknowledged: {self.io.request['id']}", SHA)))
                    result = self.dispatch(apply=False)
                    self.assertEqual(result["journal"], str(journal))
                    self.assertEqual(result["journal_phase"], phase)
                    self.assertEqual(Path(result["evidence"]).read_text().strip(), self.io.review_text)
                    self.assertEqual(Path(result["response_evidence"]).read_text().strip(), self.io.review_text)
                    self.assertEqual(sum(call[0] == "review" for call in self.io.calls), 1)
                    self.assertEqual(self.io.post.call_count, 1)

    def test_claim_release_or_handoff_during_inference_blocks_post_and_retains_result(self):
        for change in ("release", "handoff"):
            with self.subTest(change=change):
                self.io = FakeIO()
                self.evidence = Path(self.temp.name) / change
                def change_claim():
                    self.io.claims = []
                    if change == "handoff":
                        self.entries.append({**entry(), "session_id": "successor", "harness": "claude"})
                        self.io.claims = [comment(6, marker({"branch": BRANCH, "session": "successor",
                                                           "worker": "successor", "claimed_at": STAMP}))]
                self.io.after_review = change_claim
                with self.assertRaises(reviews.ReviewError):
                    self.dispatch()
                journal = json.loads(next(self.evidence.glob("*.json")).read_text())
                self.assertEqual(journal["phase"], "post_blocked")
                self.assertEqual(Path(journal["evidence"]).read_text().strip(), self.io.review_text)
                self.assert_no_post()

    def test_unsupported_filesystem_reservation_refuses_before_inference(self):
        for index, error_number in enumerate({errno.ENOTSUP, errno.EOPNOTSUPP}):
            with self.subTest(error_number=error_number):
                self.io = FakeIO()
                self.evidence = Path(self.temp.name) / f"unsupported-{index}"
                unsupported = OSError(error_number, "fixture hard links unavailable")
                with patch.object(reviews.os, "link", side_effect=unsupported):
                    with self.assertRaises(reviews.ReviewError) as failure:
                        self.dispatch()
                self.assertIs(failure.exception.__cause__, unsupported)
                self.assertFalse(any(call[0] == "review" for call in self.io.calls))
                self.assertEqual(self.dispatch()["status"], "posted")

    def test_unsupported_locks_report_the_original_cause_and_permissions_stay_denials(self):
        ledger = Path(self.temp.name) / "ledger.json"
        ledger.write_text("[]")
        unsupported = OSError(errno.EOPNOTSUPP, "fixture locks unavailable")
        with patch.object(reviews.fcntl, "flock", side_effect=unsupported):
            with self.assertRaises(reviews.ReviewError) as failure:
                with reviews.run_lock(ledger):
                    self.fail("unsupported run lock admitted work")
            self.assertIs(failure.exception.__cause__, unsupported)
            with self.assertRaises(reviews.ReviewError):
                with reviews.sweep_lock(ledger, self.evidence, apply=True):
                    self.fail("unsupported sweep lock admitted work")
        with patch.object(reviews.fcntl, "flock", side_effect=[None, unsupported]):
            with self.assertRaises(reviews.ReviewError) as failure:
                with reviews.sweep_lock(ledger, self.evidence, apply=True):
                    self.fail("unsupported evidence lock admitted work")
            self.assertIs(failure.exception.__cause__, unsupported)
        denied = PermissionError(errno.EPERM, "fixture permission denial")
        with patch.object(reviews.os, "link", side_effect=denied):
            with self.assertRaises(PermissionError) as failure:
                self.dispatch()
            self.assertIs(failure.exception, denied)
        self.assertFalse(any(call[0] == "review" for call in self.io.calls))

    def test_claim_refresh_bypasses_and_updates_the_per_sweep_cache(self):
        adapter = reviews.IO.__new__(reviews.IO)
        adapter.claims = {}
        adapter.reader = Mock()
        original = self.io.claims
        successor = [comment(6, marker({"branch": BRANCH, "session": "successor",
                                       "worker": "successor", "claimed_at": STAMP}))]
        adapter.reader.paged_json.side_effect = [original, successor]
        path = f"/repos/{REPO}/issues/3/comments"
        self.assertEqual(adapter.paged_json(path, step_prefix="author_claims"), original)
        self.assertEqual(adapter.paged_json(path, step_prefix="author_claims"), original)
        self.assertEqual(adapter.paged_json(path, step_prefix="verify_author_claims"), successor)
        self.assertEqual(adapter.paged_json(path, step_prefix="author_claims"), successor)
        self.assertEqual(adapter.reader.paged_json.call_count, 2)

    def test_same_line_and_lowercase_direct_prompts_reach_the_reviewer(self):
        scopes = ("Prompt: Inspect the new scope.", "Review prompt: Inspect the new scope.",
                  "Review read-only. please read the helper and check concrete failures.")
        for index, scope in enumerate(scopes):
            with self.subTest(scope=scope):
                self.io = FakeIO()
                self.evidence = Path(self.temp.name) / f"prompt-format-{index}"
                self.io.request["body"] = f"Supervisor runs the review.\nHead: {SHA}\n{scope}"
                self.assertEqual(self.dispatch()["status"], "posted")
                prompt = next(call[2] for call in self.io.calls if call[0] == "review")
                self.assertTrue(prompt.endswith("Review prompt:\n" + (
                    "Inspect the new scope." if index < 2 else self.io.request["body"])))

    def test_generic_worker_review_requests_never_launch_or_supersede_delegation(self):
        for lead in ("Review request for another model", "Review prompt for model-review",
                     "Review of record needed: Anthropic"):
            with self.subTest(lead=lead):
                generic = comment(12, lead + f"\nHead: {SHA}\nRead the helper read-only. Check concrete failures.")
                self.io.comments = [generic]
                self.assertEqual(self.dispatch()["status"], "no_request")
                self.io.comments = [self.io.request]
                self.io.after_review = lambda: self.io.comments.append(generic)
                self.evidence = Path(self.temp.name) / lead.split()[1]
                self.assertEqual(self.dispatch()["status"], "posted")

    def test_fenced_supervisor_phrase_cannot_delegate_review(self):
        for fence in ("```", "~~~"):
            self.io.comments = [comment(12, fence + "text\n" + ask()["body"] + "\n" + fence)]
            self.assertEqual(self.dispatch()["status"], "no_request")
        self.assertFalse(any(c[0] in {"review", "post"} for c in self.io.calls))

    def test_queue_acknowledgement_is_specific_unquoted_head_bound_and_not_provider_proof(self):
        self.io.comments.append(comment(11, review_result_body("Different review weighed.", SHA)))
        for index, line in enumerate(("Supervisor request acknowledged: 999", "> Supervisor request acknowledged: 10",
                                      "```text\nSupervisor request acknowledged: 10\n```",
                                      "~~~text\nSupervisor request acknowledged: 10\n~~~")):
            self.io.comments.append(comment(12 + index, review_result_body(line, SHA)))
            self.assertTrue(self.dispatch()["needs_attention"])
        self.io.comments.append(comment(17, review_result_body("Supervisor request acknowledged: 10", SHA), "stranger"))
        self.assertTrue(self.dispatch()["needs_attention"])
        self.io.comments.append(comment(18, review_result_body("Supervisor request acknowledged: 10", "b" * 40)))
        self.assertTrue(self.dispatch()["needs_attention"])
        self.io.comments.append(comment(19, review_result_body("Supervisor request acknowledged: 10", SHA)))
        result = self.dispatch()
        self.assertEqual(result["status"], "request_acknowledged")
        self.assertFalse(result["needs_attention"])
        self.assertFalse(result["requested_provider_confirmed"])
        self.io.pr["head"]["sha"] = "b" * 40
        result = self.dispatch()
        self.assertEqual(result["completion_status"], "request_acknowledged")
        self.assertFalse(result["needs_attention"])
        self.io.request["updated_at"] = "2026-10-17T21:00:00Z"
        self.assertTrue(self.dispatch()["needs_attention"])
        later = comment(20, review_result_body("Supervisor request acknowledged: 10", SHA))
        later["created_at"] = later["updated_at"] = "2026-10-17T22:00:00Z"
        self.io.comments.append(later)
        self.assertFalse(self.dispatch()["needs_attention"])
        self.assertFalse(any(c[0] in {"review", "post"} for c in self.io.calls))

    def test_raw_then_weighed_then_new_manual_report_needs_fresh_notification(self):
        self.dispatch()
        self.io.comments.append(comment(12, review_result_body("Initial report weighed.", SHA)))
        self.io.comments.append(comment(13, f"{reviews.REPORT_HEADING}\nHead: {SHA}\nCorrected report."))
        result = self.dispatch()
        self.assertEqual(result["completion_comment"], 13)
        self.assertTrue(result["notification_needed"])
        self.io.comments.append(comment(14, review_result_body("Corrected report weighed.", SHA)))
        self.assertFalse(self.dispatch()["notification_needed"])
        self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)

    def test_prompt_and_source_bindings_survive_linked_scope_changes(self):
        self.io.request["body"] = f"Supervisor runs the review.\nHead: {SHA}\nhttps://github.com/{REPO}/pull/7#issuecomment-9"
        self.io.sources[9] = comment(9, "Prompt:\nInspect original scope.")
        original = copy.deepcopy(self.io.sources[9])
        self.io.after_review = lambda: self.io.sources[9].update(body="Prompt:\nChanged scope.")
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        journal = json.loads(next(self.evidence.glob("*.json")).read_text())
        self.assertEqual(Path(journal["prompt_evidence"]).read_text(), next(c[2] for c in self.io.calls if c[0] == "review"))
        bindings = [s["binding"] for s in journal["prompt_sources"] if s["repository"] == REPO]
        self.assertIn(list(reviews.comment_binding(original)), bindings)
        self.assert_no_post()

    def test_atomic_journal_update_keeps_last_complete_reservation_on_interruption(self):
        replace = os.replace
        for index, (phase, previous) in enumerate((("review_completed", "launch_started"), ("post_pending", "review_completed"))):
            with self.subTest(phase=phase):
                self.io = FakeIO()
                self.evidence = Path(self.temp.name) / f"atomic-{index}"
                def interrupted(source, destination):
                    if str(destination).endswith(".json") and json.loads(Path(source).read_text()).get("phase") == phase:
                        self.assertEqual(json.loads(Path(destination).read_text())["phase"], previous)
                        raise OSError("fixture interruption before replacement")
                    replace(source, destination)
                with patch.object(reviews.os, "replace", side_effect=interrupted), self.assertRaises(OSError):
                    self.dispatch()
                self.assertEqual(json.loads(next(self.evidence.glob("*.json")).read_text())["phase"], previous)
                self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)
                self.assert_no_post()
                if previous == "review_completed":
                    self.io.comments.append(comment(11, f"{reviews.REPORT_HEADING}\nHead: {SHA}\nManually reconciled report."))
                    self.assertTrue(self.dispatch()["requested_provider_confirmed"])

    def test_persistent_evidence_is_private_even_under_permissive_umask(self):
        previous = os.umask(0)
        try:
            self.dispatch()
        finally:
            os.umask(previous)
        self.assertEqual(self.evidence.stat().st_mode & 0o077, 0)
        for file in self.evidence.iterdir():
            self.assertEqual(file.stat().st_mode & 0o077, 0, file.name)

    def test_public_evidence_directory_refuses_before_inference_and_has_recovery(self):
        self.evidence.mkdir()
        self.evidence.chmod(0o755)
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.assertFalse(any(c[0] == "review" for c in self.io.calls))
        self.evidence.chmod(0o700)
        self.assertEqual(self.dispatch()["status"], "posted")

    def test_other_run_pr_claim_cannot_hide_current_run_successor_at_exact_head(self):
        successor = "work/successor"
        self.io.claims = [comment(5, marker({"branch": BRANCH, "session": "other-run", "worker": "other", "claimed_at": STAMP})),
                          comment(6, marker({"branch": successor, "session": SESSION, "worker": "worker", "claimed_at": STAMP}))]
        with patch.object(reviews, "registered_worktrees", return_value=[(Path(self.temp.name), successor, SHA)], create=True), \
                patch.object(reviews, "git", return_value=successor):
            with self.assertRaises(reviews.ReviewError):
                self.dispatch()
            self.assertFalse(any(c[0] == "review" for c in self.io.calls))
            self.io.request["body"] += f"\nAuthor session: {SESSION}\n"
            self.assertEqual(self.dispatch()["author_session"], SESSION)
        self.io.request["body"] = ask()["body"]
        with patch.object(reviews, "registered_worktrees", return_value=[(Path(self.temp.name), successor, "b" * 40)], create=True):
            self.assertEqual(self.dispatch(apply=False)["notification_status"], "sent")
            self.evidence = Path(self.temp.name) / "foreign-without-local-history"
            self.assertEqual(self.dispatch(apply=False)["status"], "not_owned")

    def test_edited_report_requires_disposition_after_its_latest_edit(self):
        self.io.comments.append(comment(11, f"{reviews.REPORT_HEADING}\nHead: {SHA}\nOriginal report."))
        weighed = comment(12, review_result_body("Original report weighed.", SHA))
        weighed["created_at"] = weighed["updated_at"] = "2026-10-17T21:00:00Z"
        self.io.comments.append(weighed)
        self.assertFalse(self.dispatch()["notification_needed"])
        self.io.comments[1]["body"] += "\nCorrected finding."
        self.io.comments[1]["updated_at"] = "2026-10-17T22:00:00Z"
        result = self.dispatch()
        self.assertTrue(result["notification_needed"])
        self.assertTrue(result["needs_attention"])
        self.assertEqual(result["completion_updated_at"], self.io.comments[1]["updated_at"])
        later = comment(13, review_result_body("Corrected report weighed.", SHA))
        later["created_at"] = later["updated_at"] = "2026-10-17T23:00:00Z"
        self.io.comments.append(later)
        self.assertFalse(self.dispatch()["notification_needed"])

    def test_explicit_outside_author_remains_not_owned_despite_unrelated_same_head_branch(self):
        successor = "work/unrelated"
        self.io.claims = [comment(5, marker({"branch": BRANCH, "session": "other-run", "worker": "other", "claimed_at": STAMP})),
                          comment(6, marker({"branch": successor, "session": SESSION, "worker": "worker", "claimed_at": STAMP}))]
        self.io.request["body"] += "\nAuthor session: other-run\n"
        with patch.object(reviews, "registered_worktrees", return_value=[(Path(self.temp.name), successor, SHA)]):
            self.assertEqual(self.dispatch()["status"], "not_owned")
        self.assertFalse(any(c[0] in {"review", "post"} for c in self.io.calls))

    def test_weighed_disposition_repeating_delegation_cannot_replace_the_served_request(self):
        self.dispatch()
        self.io.comments.append(comment(12, review_result_body("Supervisor runs the review.", SHA)))
        result = self.dispatch()
        self.assertEqual(result["request"], self.io.request["id"])
        self.assertEqual(result["status"], "already_completed")
        self.assertEqual(result["completion_comment"], 11)
        self.assertFalse(result["notification_needed"])
        self.io.pr["head"]["sha"] = "b" * 40
        self.assertEqual(self.dispatch()["status"], "completed_stale")
        self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)

    def test_disposition_repeating_delegation_during_inference_does_not_strand_paid_result(self):
        post = self.io.post
        def ordered_post(repo, number, body):
            posted = post(repo, number, body)
            self.io.comments[-1]["id"] = 13  # The actual report is created after the unrelated disposition.
            return posted
        self.io.post = ordered_post
        self.io.after_review = lambda: self.io.comments.append(comment(12, review_result_body(
            "An unrelated review was weighed, so Supervisor runs the review.", SHA)))
        result = self.dispatch()
        self.assertEqual(result["status"], "posted")
        self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)
        self.assertEqual(sum(c[0] == "post" for c in self.io.calls), 1)
        self.assertTrue(self.dispatch()["notification_needed"])

    def test_malformed_other_ledger_issue_cannot_block_an_unrelated_branch(self):
        self.entries.append({**entry(), "issue": 99, "session_id": "other"})
        other = marker({"branch": "work/unrelated", "session": "other"})
        pages = self.io.paged_json
        def claims(path, **kwargs):
            if path.endswith("/99/comments"):
                return [comment(6, other)]
            return pages(path, **kwargs)
        self.io.paged_json = claims
        self.assertEqual(self.dispatch(apply=False)["author_session"], SESSION)
        for branch in (BRANCH, "", None):
            other = marker({"branch": branch, "session": "other"})
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                self.dispatch()
        self.assertFalse(any(c[0] in {"review", "post"} for c in self.io.calls))

    def test_selection_failure_keeps_structured_metadata_without_authorizing_replay(self):
        self.io.review = reviews.IO.__new__(reviews.IO).review
        metadata = {"ok": False, "provider": "anthropic", "phase": "selection", "outcome": "unavailable",
                    "installed": False, "error": "CLI is not installed"}
        process = subprocess.CompletedProcess([], 2, json.dumps(metadata), "")
        with patch.object(reviews, "run", return_value=process) as command:
            with self.assertRaises(reviews.ReviewError):
                self.dispatch()
            journal = json.loads(next(self.evidence.glob("*.json")).read_text())
            self.assertEqual(journal["review_failure"], metadata)
            discovery = self.dispatch(apply=False)
            self.assertEqual(discovery["review_phase"], metadata["phase"])
            self.assertEqual(discovery["review_outcome"], metadata["outcome"])
            with self.assertRaises(reviews.ReviewError):
                self.dispatch()
        self.assertEqual(command.call_count, 1)
        self.assert_no_post()

    def test_raw_response_survives_metadata_failure_or_interrupt_before_validation(self):
        self.io.review = reviews.IO.__new__(reviews.IO).review
        for index, failure in enumerate((reviews.ReviewError, KeyboardInterrupt)):
            with self.subTest(failure=failure):
                self.evidence = Path(self.temp.name) / f"raw-output-{index}"
                outputs = []
                def helper(args, **kwargs):
                    output = Path(args[args.index("--out") + 1])
                    output.write_text("Completed report before metadata validation.")
                    outputs.append(output)
                    if failure is KeyboardInterrupt:
                        raise KeyboardInterrupt()
                    return subprocess.CompletedProcess(args, 0, "extra non-JSON stdout", "")
                with patch.object(reviews, "run", side_effect=helper) as command, self.assertRaises(failure):
                    self.dispatch()
                self.assertTrue(outputs[0].is_file())
                self.assertEqual(outputs[0].stat().st_mode & 0o077, 0)
                self.assertEqual(outputs[0].read_text(), "Completed report before metadata validation.")
                with self.assertRaises(reviews.ReviewError):
                    self.dispatch()
                self.assertEqual(command.call_count, 1)
                self.assert_no_post()

    def test_linked_nested_fences_preserve_the_entire_prompt_scope(self):
        for index, fence in enumerate(("````", "~~~~")):
            with self.subTest(fence=fence):
                self.io = FakeIO()
                self.evidence = Path(self.temp.name) / f"nested-fences-{index}"
                self.io.request["body"] = f"Supervisor runs the review.\nHead: {SHA}\nhttps://github.com/{REPO}/pull/7#issuecomment-9"
                scope = "Read the helper.\n```sh\nexample command\n```\nCheck concurrency after the example."
                self.io.sources[9] = comment(9, f"Prompt:\n{fence}text\n{scope}\n{fence}")
                self.dispatch()
                prompt = next(c[2] for c in self.io.calls if c[0] == "review")
                self.assertTrue(prompt.endswith("Review prompt:\n" + scope))

    def test_unclosed_or_empty_fenced_prompt_refuses_before_inference(self):
        for body in ("Prompt:\n````text\nInspect scope.\n```", "Prompt:\n```text\n\n```"):
            with self.subTest(body=body):
                self.io.request["body"] = f"Supervisor runs the review.\nHead: {SHA}\n{body}"
                with self.assertRaises(reviews.ReviewError):
                    self.dispatch()
        self.assertFalse(any(c[0] in {"review", "post"} for c in self.io.calls))

    def test_comment_reactions_and_endpoint_shape_differences_do_not_invalidate_prompt(self):
        get = self.io.get_json
        def single(path, **kwargs):
            value = get(path, **kwargs)
            if "/issues/comments/" in path:
                value["reactions"] = {"total_count": 1}
                value["endpoint_only"] = "does not affect review content"
            return value
        self.io.get_json = single
        self.io.after_review = lambda: self.io.comments[0].update(reactions={"total_count": 2})
        self.assertEqual(self.dispatch()["status"], "posted")

    def test_finished_result_is_retained_if_head_or_prompt_changes_before_post(self):
        self.io.after_review = lambda: self.io.pr["head"].update(sha="b" * 40)
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.assert_no_post()
        self.assertEqual(next(self.evidence.glob("*.md")).read_text().strip(), self.io.review_text)
        journal = json.loads(next(self.evidence.glob("*.json")).read_text())
        self.assertTrue(journal["review"]["ok"])

    def test_completed_post_recovers_notification_identity_without_worktree_or_repeat(self):
        self.dispatch()
        with patch.object(reviews, "find_worktree", side_effect=reviews.ReviewError("gone")):
            result = self.dispatch()
        self.assertEqual(result["author_session"], SESSION)
        self.assertEqual(result["author_provider"], "openai")
        self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)

    def test_quoted_or_malformed_marker_cannot_hide_the_real_receipt(self):
        self.io.review_text = 'Quoted source: `<!-- supervisor-review receipt_for(request, head) -->`'
        self.dispatch()
        # The GitHub comment helper appends its transport marker to the raw report.
        self.io.comments[-1]["body"] += "\n<!-- github-skill-operation:" + "a" * 32 + " -->\n"
        self.io.comments.insert(1, comment(9, '<!-- supervisor-review not-json -->'))
        result = self.dispatch()
        self.assertEqual(result["status"], "already_completed")
        self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)

    def test_crash_after_successful_post_recovers_author_without_replay(self):
        post = self.io.post
        def uncertain(repo, number, body):
            post(repo, number, body)
            raise OSError("process interrupted after delivery")
        self.io.post = uncertain
        with self.assertRaises(OSError):
            self.dispatch()
        result = self.dispatch()
        self.assertEqual(result["author_session"], SESSION)
        self.assertTrue(result["notification_needed"])
        self.assertTrue(result["requested_provider_confirmed"])
        self.assertEqual(sum(c[0] == "post" for c in self.io.calls), 1)

    def test_quoted_marker_does_not_hide_a_review_request(self):
        self.io.request["body"] += "\nQuoted source: `<!-- supervisor-review not-json -->`"
        self.assertEqual(self.dispatch(apply=False)["status"], "pending")

    def test_unrelated_weighed_disposition_does_not_quietly_resolve_supervisor_request(self):
        self.io.comments.append(comment(11, review_result_body("Existing review weighed.", SHA)))
        result = self.dispatch()
        self.assertEqual(result["status"], "weighed_without_review")
        self.assertFalse(result["notification_needed"])
        self.assertTrue(result["needs_attention"])
        self.assertFalse(result["requested_provider_confirmed"])
        self.assertFalse(any(c[0] == "review" for c in self.io.calls))

    def test_missing_checkout_is_irrelevant_to_prs_without_requests(self):
        self.io.comments = []
        result = reviews.sweep(self.io, self.entries, {}, TRUSTED, self.evidence)
        self.assertEqual(result, [])
        self.io.pr["head"]["repo"]["full_name"] = "external/fork"
        self.assertEqual(reviews.sweep(self.io, self.entries, {}, TRUSTED, self.evidence), [])

    def test_explicit_head_or_legacy_commit_is_not_confused_with_dates_or_numbers(self):
        for heading in (f"Head: {SHA}", f"Review read-only on {SHA}."):
            with self.subTest(heading=heading):
                self.io.request["body"] = (f"Supervisor runs the review.\n{heading}\n"
                                            "Rebased on 20261017, at 1234567.\nPrompt:\nInspect helper.")
                self.assertEqual(self.dispatch(apply=False)["status"], "pending")

    def test_incidental_and_quoted_supervisor_prose_does_not_mask_the_request(self):
        for body in ("pushed; Supervisor runs the review once CI is green", "> Supervisor runs the review.",
                     f"Review report quotes `Supervisor runs the review`.\nHead: {SHA}\nPrompt:\nRead the report."):
            with self.subTest(body=body):
                self.io.comments = [self.io.request, comment(12, body)]
                self.assertEqual(self.dispatch(apply=False)["request"], self.io.request["id"])

    def test_manual_supervisor_report_echoing_the_prompt_waits_for_disposition_without_relaunch(self):
        body = (f"**Review of record: Anthropic, run by the Supervisor** at head `{SHA}`.\n\n"
                "Report includes the original request for context:\n" + self.io.request["body"])
        self.io.comments.append(comment(12, body))
        result = self.dispatch()
        self.assertEqual(result["status"], "reported_review")
        self.assertFalse(result["requested_provider_confirmed"])
        self.assertTrue(result["notification_needed"])
        self.assertEqual(result["author_session"], SESSION)
        self.assertFalse(any(c[0] == "review" for c in self.io.calls))

    def test_author_disposition_stops_repeated_notification_reports(self):
        self.dispatch()
        self.io.comments.append(comment(12, review_result_body("Findings weighed.", SHA)))
        result = self.dispatch()
        self.assertFalse(result["notification_needed"])
        self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)

    def test_editing_an_older_request_does_not_supersede_a_newer_request(self):
        newer = comment(12, self.io.request["body"])
        newer["created_at"] = newer["updated_at"] = "2026-10-17T21:00:00Z"
        self.io.comments.append(newer)
        self.io.request["updated_at"] = "2026-10-17T22:00:00Z"
        self.assertEqual(self.dispatch(apply=False)["request"], newer["id"])

    def test_editing_a_served_request_never_repeats_inference(self):
        self.dispatch()
        self.io.request["updated_at"] = "2026-10-17T21:00:00Z"
        result = self.dispatch()
        self.assertEqual(result["status"], "already_completed")
        self.assertTrue(result["request_edited_after_review"])
        self.assertTrue(result["needs_attention"])
        self.assertIn("completion_comment", result)
        self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)


    def test_legacy_completion_preserves_report_when_claim_binding_fails(self):
        self.io.comments.append(comment(11, '<!-- supervisor-review ' + json.dumps({"request": 10, "head": SHA}) + ' -->'))
        for error in [ValueError("malformed claim"), reviews.GitHubReadError("unavailable", result=None, diagnostics={})]:
            with self.subTest(error=error), patch.object(reviews, "bind_session", side_effect=error):
                result = self.dispatch()
                self.assertEqual(result["status"], "already_completed")
                self.assertEqual(result["completion_comment"], 11)
                self.assertEqual(result["notification_error"], str(error))
                self.assertTrue(result["needs_attention"])
        self.assertFalse(any(c[0] == "review" for c in self.io.calls))

    def test_unrelated_malformed_referenced_claim_does_not_hide_valid_author(self):
        self.io.pr["body"] = "Related #99"
        pages = self.io.paged_json
        other = marker({"branch": "work/unrelated", "session": "elsewhere"})
        def references(path, **kwargs):
            if path.endswith("/99/comments"):
                return [comment(6, other)]
            return pages(path, **kwargs)
        self.io.paged_json = references
        self.assertEqual(self.dispatch(apply=False)["author_session"], SESSION)
        other = marker({"branch": BRANCH, "session": "elsewhere"})
        with self.assertRaises(ValueError):
            self.dispatch()
        self.assertFalse(any(c[0] == "review" for c in self.io.calls))

    def test_other_runs_trusted_branch_claim_is_not_this_runs_failure(self):
        self.io.claims = [comment(5, marker({"branch": BRANCH, "session": "other-run-session",
                                            "worker": "another-worker", "claimed_at": STAMP}))]
        # No checkout or local worktree is required to report an authoritative other-run claim.
        result = reviews.sweep(self.io, self.entries, {}, TRUSTED, self.evidence, apply=True)
        self.assertEqual(result[0]["status"], "not_owned")
        self.assertFalse(any(c[0] == "review" for c in self.io.calls))

    def test_overlapping_branch_claims_require_explicit_author_from_this_run(self):
        self.io.claims.append(comment(6, marker({"branch": BRANCH, "session": "other-run-session",
                                                "worker": "another-worker", "claimed_at": STAMP})))
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.assertFalse(any(c[0] == "review" for c in self.io.calls))
        self.io.request["body"] += f"\nAuthor session: {SESSION}\n"
        self.assertEqual(self.dispatch()["author_session"], SESSION)

    def test_copied_receipt_does_not_confirm_requested_provider(self):
        self.io.comments.append(comment(11, '<!-- supervisor-review ' + json.dumps({
            "request": 10, "head": SHA, "author_session": SESSION, "author_provider": "openai"}) + ' -->'))
        result = self.dispatch()
        self.assertFalse(result["requested_provider_confirmed"])
        self.assertTrue(result["needs_attention"])
        self.assertFalse(any(c[0] == "review" for c in self.io.calls))

    def test_weighed_manual_review_clears_attention_without_inventing_provider_confirmation(self):
        self.io.comments.append(comment(11, f"{reviews.REPORT_HEADING} at head {SHA}\nReport."))
        ledger = Path(self.temp.name) / "ledger.json"
        ledger.write_text(json.dumps(self.entries))
        args = ["pending_reviews", "--ledger", str(ledger), "--evidence-dir", str(self.evidence)]
        output = StringIO()
        with patch.object(sys, "argv", args), patch.object(reviews, "IO", return_value=self.io), \
                patch.object(reviews, "configured_bot_logins", return_value=[BOT]), \
                patch.object(reviews, "automation_login", return_value=BOT), contextlib.redirect_stdout(output):
            self.assertEqual(reviews.main(), 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["attention"][0]["status"], "reported_review")
        self.assertFalse(report["attention"][0]["requested_provider_confirmed"])
        self.io.comments.append(comment(12, review_result_body("Verified manual review and weighed it.", SHA)))
        output = StringIO()
        with patch.object(sys, "argv", args), patch.object(reviews, "IO", return_value=self.io), \
                patch.object(reviews, "configured_bot_logins", return_value=[BOT]), \
                patch.object(reviews, "automation_login", return_value=BOT), contextlib.redirect_stdout(output):
            self.assertEqual(reviews.main(), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["attention"], [])
        self.assertEqual(report["results"][0]["status"], "already_weighed")
        self.assertFalse(report["results"][0]["requested_provider_confirmed"])
        self.assertFalse(any(c[0] in {"review", "post"} for c in self.io.calls))

    def test_disposition_before_new_manual_report_cannot_acknowledge_that_report(self):
        self.io.comments.append(comment(11, review_result_body("Earlier review weighed.", SHA)))
        self.io.comments.append(comment(12, f"{reviews.REPORT_HEADING} at head {SHA}\nNew report."))
        result = self.dispatch()
        self.assertEqual(result["status"], "reported_review")
        self.assertTrue(result["needs_attention"])
        self.assertTrue(result["notification_needed"])
        self.io.comments.append(comment(13, review_result_body("Newer manual report checked and weighed.", SHA)))
        self.assertFalse(self.dispatch()["needs_attention"])
        self.assertFalse(any(c[0] in {"review", "post"} for c in self.io.calls))

    def test_unconfirmed_receipt_becomes_informational_after_weighing(self):
        self.io.comments.append(comment(11, '<!-- supervisor-review ' + json.dumps({
            "request": 10, "head": SHA, "author_session": SESSION, "author_provider": "openai"}) + ' -->'))
        self.assertTrue(self.dispatch()["needs_attention"])
        self.io.comments.append(comment(12, review_result_body("Provenance checked; findings weighed.", SHA)))
        result = self.dispatch()
        self.assertFalse(result["needs_attention"])
        self.assertFalse(result["requested_provider_confirmed"])
        self.assertFalse(result["notification_needed"])

    def test_final_check_failure_retains_diagnostics_and_manual_post_can_confirm_paid_review(self):
        failures = [reviews.GitHubReadError("read unavailable", result=None, diagnostics={}),
                    reviews.GitHubReadShapeError("bad shape"), KeyError("head")]
        for index, error in enumerate(failures):
            with self.subTest(error=error):
                self.evidence = Path(self.temp.name) / f"final-check-{index}"
                self.io.comments = [self.io.request]
                self.io.calls.clear()
                with patch.object(reviews, "verify_inputs", side_effect=[None, error]):
                    with self.assertRaises(type(error)):
                        self.dispatch()
                journal = json.loads(next(self.evidence.glob("*.json")).read_text())
                self.assertEqual(journal["phase"], "post_blocked")
                self.assertEqual(journal["error"], str(error))
                self.assertEqual(next(self.evidence.glob("*.md")).read_text().strip(), self.io.review_text)
                with self.assertRaises(reviews.ReviewError):
                    self.dispatch()
                self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)
                self.assert_no_post()
                # Reconciled manually after input readback, through the normal GitHub helper.
                self.io.comments.append(comment(11, f"{reviews.REPORT_HEADING}\nHead: {SHA}\n" + self.io.review_text))
                result = self.dispatch()
                self.assertEqual(result["status"], "reported_review")
                self.assertTrue(result["requested_provider_confirmed"])
                self.assertFalse(result["needs_attention"])
                self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)

    def test_discovery_reports_spent_journal_instead_of_pending_without_writes(self):
        self.evidence.mkdir()
        journal = self.evidence / (reviews.journal_key(REPO, 7, self.io.request, SHA) + ".json")
        for phase in ("launch_started", "review_failed", "review_completed", "post_blocked", "post_failed", "post_unknown"):
            with self.subTest(phase=phase):
                journal.write_text(json.dumps({"phase": phase, "error": "prior outcome"}))
                before = journal.read_bytes()
                result = self.dispatch(apply=False)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["journal_phase"], phase)
                self.assertEqual(result["error"], "prior outcome")
                self.assertTrue(result["needs_attention"])
                self.assertEqual(journal.read_bytes(), before)
                with self.assertRaises(reviews.ReviewError):
                    self.dispatch()
        self.assertFalse(any(c[0] in {"review", "post"} for c in self.io.calls))

    def test_linked_prompt_precedes_quoted_context_and_is_revalidated(self):
        self.io.request["body"] = (f"Supervisor runs the review.\nHead: {SHA}\n"
                                    "> Director: use Anthropic.\n"
                                    f"https://github.com/{REPO}/pull/7#issuecomment-9")
        self.io.sources[9] = comment(9, "Prompt:\nInspect the actual linked scope.")
        self.io.after_review = lambda: self.io.sources[9].update(body="Prompt:\nChanged scope.")
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        prompt = next(c[2] for c in self.io.calls if c[0] == "review")
        self.assertTrue(prompt.endswith("\nInspect the actual linked scope."), "the review scope comes last, after the request context")
        self.assert_no_post()

    def test_pending_report_heading_cannot_hide_real_request(self):
        self.io.comments.append(comment(12, f"{reviews.REPORT_HEADING}: pending at head {SHA}"))
        self.assertEqual(self.dispatch(apply=False)["status"], "pending")

    def test_plain_prompt_takes_precedence_over_quoted_context(self):
        self.io.request["body"] = (f"Supervisor runs the review.\nHead: {SHA}\n"
                                    "> Director: keep it productive.\nPrompt:\nInspect the real changed helper.")
        self.dispatch()
        prompt = next(c[2] for c in self.io.calls if c[0] == "review")
        self.assertTrue(prompt.endswith("\nInspect the real changed helper."), "the review scope comes last, after the request context")

    def test_failed_and_unknown_posts_preserve_diagnostics_without_replay(self):
        adapter = reviews.IO.__new__(reviews.IO)
        self.io.post = adapter.post
        cases = [(post_payload(ok=False, write_outcome="rejected"), "post_failed"),
                 (post_payload(ok=False, write_outcome="unknown"), "post_unknown"),
                 (post_payload(), "post_unknown"),
                 (post_payload(reconciled=True), "post_unknown"),
                 (None, "post_unknown")]
        for index, (payload, phase) in enumerate(cases):
            with self.subTest(payload=payload):
                self.evidence = Path(self.temp.name) / f"post-case-{index}"
                completed = subprocess.CompletedProcess([], 1, json.dumps(payload) if payload else "invalid-json", "diagnostic")
                with patch.object(reviews, "run", return_value=completed) as command:
                    for _ in range(2):
                        with self.assertRaises(reviews.ReviewError):
                            self.dispatch()
                self.assertEqual(command.call_count, 1)
                journal = json.loads(next(self.evidence.glob("*.json")).read_text())
                self.assertEqual(journal["phase"], phase)
                self.assertTrue(journal["error"])
                if payload:
                    self.assertEqual(journal["post"], payload)

    def test_served_edit_needs_a_disposition_after_the_edit(self):
        self.dispatch()
        self.io.comments.append(comment(12, review_result_body("Original findings weighed.", SHA)))
        self.io.request["updated_at"] = "2026-10-17T21:00:00Z"
        self.assertTrue(self.dispatch()["needs_attention"])
        later = comment(13, review_result_body("Edit and original scope checked; findings weighed.", SHA))
        later["created_at"] = later["updated_at"] = "2026-10-17T22:00:00Z"
        self.io.comments.append(later)
        result = self.dispatch()
        self.assertTrue(result["request_edited_after_review"])
        self.assertFalse(result["needs_attention"])
        self.assertFalse(result["notification_needed"])
        self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)

    def test_checkout_keys_are_case_insensitive(self):
        reports = reviews.sweep(self.io, self.entries, {REPO.upper(): Path(self.temp.name)}, TRUSTED, self.evidence)
        self.assertEqual([r["status"] for r in reports], ["pending"])

    def test_other_provider_from_author_harness(self):
        self.entries[0]["harness"] = "claude"
        self.dispatch()
        self.assertEqual(next(c[1] for c in self.io.calls if c[0] == "review"), "anthropic")

    def test_completed_request_survives_moved_head_without_checkout_or_repeat(self):
        self.dispatch()
        self.io.pr["head"]["sha"] = "b" * 40
        result = reviews.dispatch(self.io, REPO, 7, self.entries, None, TRUSTED, self.evidence, True)
        self.assertEqual(result["status"], "completed_stale")
        self.assertEqual(result["reviewed_head"], SHA)
        self.assertEqual(result["current_head"], self.io.pr["head"]["sha"])
        self.assertEqual(result["author_session"], SESSION)
        self.assertEqual(result["completion_comment"], 11)
        self.assertTrue(result["notification_needed"])
        self.assertTrue(result["requested_provider_confirmed"])
        self.io.comments.append(comment(12, review_result_body("Old-head review weighed.", SHA)))
        self.assertFalse(self.dispatch()["notification_needed"])
        self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)

    def test_manual_and_weighed_old_head_completion_are_preserved_without_provider_guess(self):
        self.io.comments.append(comment(11, f"{reviews.REPORT_HEADING}\nHead: {SHA}\nCompleted report."))
        self.io.pr["head"]["sha"] = "b" * 40
        self.io.request["body"] = self.io.request["body"].replace(SHA, SHA[:9])
        # The full historical report resolves an abbreviated request without guessing.
        result = self.dispatch()
        self.assertEqual(result["status"], "completed_stale")
        self.assertEqual(result["completion_comment"], 11)
        self.assertFalse(result["requested_provider_confirmed"])
        self.io.comments.append(comment(12, review_result_body("Manual review checked at its original head.", SHA)))
        result = self.dispatch()
        self.assertFalse(result["needs_attention"])
        self.assertFalse(result["notification_needed"])
        # A new request at B cannot be satisfied by completion at A.
        self.io.request = comment(13, ask()["body"].replace(SHA, self.io.pr["head"]["sha"]))
        self.io.comments.append(self.io.request)
        self.assertEqual(self.dispatch(apply=False)["status"], "pending")
        self.assertFalse(any(c[0] in {"review", "post"} for c in self.io.calls))

    def test_released_claim_is_not_a_live_author_during_handoff(self):
        self.entries.append({**entry(), "session_id": "successor", "harness": "claude"})
        release = comment(6, "Released claim 5")
        release["created_at"] = release["updated_at"] = "2026-10-17T21:00:00Z"
        successor = comment(7, marker({"branch": BRANCH, "session": "successor", "worker": "next",
                                        "claimed_at": "2026-10-17T22:00:00Z"}))
        successor["created_at"] = successor["updated_at"] = "2026-10-17T22:00:00Z"
        self.io.claims += [release, successor]
        result = self.dispatch(apply=False)
        self.assertEqual(result["author_session"], "successor")
        self.assertEqual(result["author_provider"], "anthropic")
        release["user"]["login"] = "stranger"
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        release["user"]["login"] = BOT
        release["created_at"] = "2026-10-17T19:00:00Z"
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.assertFalse(any(c[0] == "review" for c in self.io.calls))

    def test_released_outside_claim_cannot_silently_hide_missing_current_issue_binding(self):
        self.entries[0].pop("issue")
        self.io.pr["body"] = "Related #99"
        self.io.claims = [comment(5, marker({"branch": BRANCH, "session": "old-run", "worker": "old",
                                            "claimed_at": STAMP})), comment(6, "Released claim 5")]
        self.io.claims[1]["created_at"] = self.io.claims[1]["updated_at"] = "2026-10-17T21:00:00Z"
        result = reviews.sweep(self.io, self.entries, {REPO: Path(self.temp.name)}, TRUSTED, self.evidence, apply=True)
        self.assertEqual(result[0]["status"], "refused")
        self.assertFalse(any(c[0] in {"review", "post"} for c in self.io.calls))

    def test_link_only_prompt_section_follows_and_revalidates_actual_prompt(self):
        for index, fenced in enumerate((False, True)):
            with self.subTest(fenced=fenced):
                self.io = FakeIO()
                self.evidence = Path(self.temp.name) / f"linked-section-{index}"
                link = f"https://github.com/{REPO}/pull/7#issuecomment-9"
                self.io.request["body"] = f"Supervisor runs the review.\nHead: {SHA}\nPrompt:\n" + (
                    "```text\n" + link + "\n```" if fenced else link)
                self.io.sources[9] = comment(9, "Prompt:\nInspect actual linked scope.")
                self.io.after_review = lambda: self.io.sources[9].update(body="Prompt:\nChanged scope.")
                with self.assertRaises(reviews.ReviewError):
                    self.dispatch()
                prompt = next(c[2] for c in self.io.calls if c[0] == "review")
                self.assertTrue(prompt.endswith("\nInspect actual linked scope."), "the review scope comes last, after the request context")
                self.assert_no_post()

    def test_request_for_previous_head_never_launches(self):
        self.io.pr["head"]["sha"] = "b" * 40
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.assertFalse(any(c[0] == "review" for c in self.io.calls))

    def test_head_moves_while_binding_session_refuses_before_review(self):
        def find(*args):
            self.io.pr["head"]["sha"] = "b" * 40
            return Path(self.temp.name)
        with patch.object(reviews, "find_worktree", side_effect=find), self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.assertFalse(any(c[0] == "review" for c in self.io.calls))
        self.assert_no_post()

    def test_moved_head_during_review_never_posts(self):
        self.io.after_review = lambda: self.io.pr["head"].update(sha="b" * 40)
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.assert_no_post()

    def test_closed_pr_during_review_never_posts(self):
        self.io.after_review = lambda: self.io.pr.update(state="closed")
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.assert_no_post()

    def test_dirty_worktree_during_review_never_posts(self):
        with patch.object(reviews, "check_worktree", side_effect=[None, reviews.ReviewError("dirty")]):
            with self.assertRaises(reviews.ReviewError):
                self.dispatch()
        self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)
        self.assertEqual(next(self.evidence.glob("*.md")).read_text().strip(), self.io.review_text)
        self.assert_no_post()

    def test_failed_or_empty_review_never_posts(self):
        for error, text in [(reviews.ReviewError("permission denied"), ""), (None, " \n")]:
            with self.subTest(error=error):
                self.io.review_error, self.io.review_text = error, text
                self.evidence = Path(self.temp.name) / ("failure-case" if error else "empty-case")
                self.io.calls.clear()
                with self.assertRaises(reviews.ReviewError):
                    self.dispatch()
                self.assert_no_post()
                self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)
                journal = json.loads(next(self.evidence.glob("*.json")).read_text())
                self.assertEqual(journal["phase"], "review_failed")
                self.assertTrue(journal["error"])

    def test_failed_or_interrupted_launch_is_not_replayed_on_next_sweep(self):
        self.io.review_error = reviews.ReviewError("permission denied")
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.io.review_error = None
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)
        self.assert_no_post()

    def test_unknown_post_is_not_replayed_on_next_sweep(self):
        self.io.post = Mock(side_effect=reviews.ReviewError("unknown write"))
        for _ in range(2):
            with self.assertRaises(reviews.ReviewError):
                self.dispatch()
        self.assertEqual(self.io.post.call_count, 1)
        self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)
        self.assertEqual(next(self.evidence.glob("*.md")).read_text().strip(), self.io.review_text)

    def test_existing_review_prompt_heading_inline_heading_and_direct_request(self):
        bodies = [
            f"Supervisor runs the review.\nHead: {SHA}\n\nReview prompt:\nInspect the helper.",
            f"Supervisor runs the review.\nReview at new head `{SHA[:9]}`. Prompt:\n\n```text\nInspect the helper.\n```",
            f"Supervisor runs the review.\nReview read-only on {SHA}. Read the helper and tests. Check concrete failures.",
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.io.request["body"] = body
                self.assertEqual(self.dispatch(apply=False)["status"], "pending")

    def test_missing_prompt_or_head_refuses_before_launch(self):
        for body in [f"Supervisor runs the review.\nHead: {SHA}", "Supervisor runs the review.\nPrompt:\nRead the diff."]:
            with self.subTest(body=body):
                self.io.request["body"] = body
                with self.assertRaises(reviews.ReviewError):
                    self.dispatch()
                self.assertFalse(any(c[0] == "review" for c in self.io.calls))

    def test_follows_linked_prompt_and_checks_it_again_before_post(self):
        self.io.request["body"] = (f"Supervisor runs the review.\nHead: {SHA}\n"
                                   f"https://github.com/{REPO}/pull/7#issuecomment-9")
        self.io.sources[9] = comment(9, "Prompt:\nInspect helpers and surrounding files.")
        self.dispatch()
        self.assertIn("Inspect helpers and surrounding files.", next(c[2] for c in self.io.calls if c[0] == "review"))

    def test_cycle_cross_repo_link_and_untrusted_link_refuse(self):
        self.io.request["body"] = f"Supervisor runs the review.\nHead: {SHA}\nhttps://github.com/{REPO}/pull/7#issuecomment-9"
        for body, login in [(f"https://github.com/{REPO}/pull/7#issuecomment-10", BOT),
                            ("https://github.com/other/repo/issues/1#issuecomment-10", BOT),
                            ("Prompt:\nInspect helpers.", "stranger")]:
            with self.subTest(body=body, login=login):
                self.io.sources[9] = comment(9, body, login)
                with self.assertRaises(reviews.ReviewError):
                    self.dispatch()
                self.assert_no_post()

    def test_unknown_and_ambiguous_author_session_never_guesses(self):
        original = copy.deepcopy(self.io.claims)
        for claims in [[], original + [comment(6, marker({"branch": BRANCH, "session": "second",
                                                          "worker": "other", "claimed_at": STAMP}))]]:
            with self.subTest(claims=claims):
                self.io.claims = claims
                self.entries = [entry(), {**entry(), "session_id": "second"}]
                with self.assertRaises(reviews.ReviewError):
                    self.dispatch()
                self.assertFalse(any(c[0] == "review" for c in self.io.calls))

    def test_new_issue_under_brief_parent_can_supply_author_claim(self):
        self.io.pr["body"] = "Closes #99"
        claims = self.io.paged_json
        def pages(path, **kwargs):
            if kwargs.get("step_prefix") == "author_claims" and not path.endswith("/99/comments"):
                return []
            return claims(path, **kwargs)
        self.io.paged_json = pages
        self.assertEqual(self.dispatch()["author_session"], SESSION)

    def test_successor_worktree_branch_claim_binds_to_original_pr_head(self):
        successor = "work/successor"
        self.io.claims = [comment(5, marker({"branch": successor, "session": SESSION,
                                            "worker": "worker", "claimed_at": STAMP}))]
        with patch.object(reviews, "git", return_value=successor):
            with self.assertRaises(reviews.ReviewError):
                self.dispatch(apply=False)
            self.io.request["body"] += f"\nAuthor session: {SESSION}\n"
            self.assertEqual(self.dispatch()["author_session"], SESSION)

    def test_new_request_during_review_never_posts(self):
        self.io.after_review = lambda: self.io.comments.append(comment(12, self.io.request["body"]))
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.assert_no_post()

    def test_untrusted_request_does_not_trigger_inference(self):
        self.io.request["user"]["login"] = "stranger"
        self.assertEqual(self.dispatch()["status"], "no_request")
        self.assertFalse(any(c[0] == "review" for c in self.io.calls))

    def test_batched_discovery_error_does_not_hide_a_healthy_repository(self):
        entries = [entry(), {**entry(), "repository": "example/another", "session_id": "other"}]
        self.io.open_pulls = Mock(return_value={REPO: [self.io.pr], "example/another": ValueError("unavailable inventory")})
        with patch.object(reviews, "dispatch", return_value={"status": "pending"}) as dispatch:
            reports = reviews.sweep(self.io, entries, {}, TRUSTED, self.evidence)
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(dispatch.call_args.args[1], REPO)
        self.assertEqual({r["repository"]: r["status"] for r in reports}, {REPO: "pending", "example/another": "refused"})

    def test_all_run_repositories_discovered_one_failure_does_not_hide_others(self):
        entries = [entry(), {**entry(), "repository": "example/another", "session_id": "other"}]
        reports = reviews.sweep(self.io, entries, {REPO: Path(self.temp.name)}, TRUSTED, self.evidence)
        paths = {c[1] for c in self.io.calls if c[0] == "read"}
        self.assertIn(f"/repos/{REPO}/pulls", paths)
        self.assertIn("/repos/example/another/pulls", paths)
        self.assertTrue(any(r["status"] == "pending" for r in reports))
        self.assertTrue(any(r["status"] == "refused" for r in reports))


class WorktreeTests(unittest.TestCase):
    def test_real_git_worktree_binding_includes_untracked_and_branch_head_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            checkout = root / "repo"
            def git(*args):
                result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                                        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()
            git("init", "-b", "main", str(checkout))
            git("-C", str(checkout), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "--allow-empty", "-m", "fixture")
            git("-C", str(checkout), "remote", "add", "origin", f"https://github.com/{REPO}.git")
            path = root / "task"
            git("-C", str(checkout), "worktree", "add", "-b", BRANCH, str(path))
            head = git("-C", str(path), "rev-parse", "HEAD")
            git("-C", str(checkout), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "--allow-empty", "-m", "advance main")
            scratch = path / ".model-review-fixture_1"
            scratch.mkdir()
            (scratch / "diff.txt").write_text("review helper scratch")
            self.assertEqual(reviews.find_worktree(checkout, REPO, BRANCH, head), path.resolve())
            git("-C", str(path), "add", str(scratch / "diff.txt"))
            with self.assertRaises(reviews.ReviewError):
                reviews.check_worktree(path, REPO, BRANCH, head)
            git("-C", str(path), "restore", "--staged", "--", str(scratch / "diff.txt"))
            for repo, branch, sha in [("wrong/repo", BRANCH, head), (REPO, BRANCH, SHA)]:
                with self.subTest(repo=repo, branch=branch, sha=sha), self.assertRaises(reviews.ReviewError):
                    reviews.find_worktree(checkout, repo, branch, sha)
            self.assertEqual(reviews.find_worktree(checkout, REPO, "work/previous", head), path.resolve())
            second = root / "second"
            git("-C", str(checkout), "worktree", "add", "-b", "work/second", str(second), head)
            with self.assertRaises(reviews.ReviewError):
                reviews.find_worktree(checkout, REPO, "work/previous", head)
            self.assertEqual(reviews.find_worktree(checkout, REPO, "work/previous", head, second), second.resolve())
            (path / "untracked.txt").write_text("not committed")
            with self.assertRaises(reviews.ReviewError):
                reviews.find_worktree(checkout, REPO, BRANCH, head)


class LedgerTests(unittest.TestCase):
    def test_reviews_need_identity_not_a_completed_transcript(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ledger.json"
            path.write_text(json.dumps([entry(), {"supervisor_owned": False}]))
            self.assertEqual(reviews.review_entries(path), [entry()])
            for value in [[entry(), entry()], [{**entry(), "session_id": ""}],
                          [{**entry(), "repository": "invalid"}], [{**entry(), "harness": "unknown"}], {}]:
                with self.subTest(value=value):
                    path.write_text(json.dumps(value))
                    with self.assertRaises(reviews.ReviewError):
                        reviews.review_entries(path)
            path.write_text(json.dumps([{**entry(), "transcript": "sessions/author.jsonl"}]))
            self.assertEqual(reviews.review_entries(path)[0]["transcript"], str((path.parent / "sessions/author.jsonl").resolve()))


    def test_malformed_entries_are_reported_without_authorizing_them_or_hiding_other_repositories(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ledger.json"
            good = entry()
            bad = {**entry(), "session_id": "duplicate"}
            path.write_text(json.dumps([good, bad, bad, {**entry(), "session_id": "bad", "harness": "unknown"}]))
            errors = []
            self.assertEqual(reviews.review_entries(path, errors=errors), [good])
            self.assertEqual(len(errors), 3)


class RunLockTests(unittest.TestCase):
    def test_same_run_cannot_overlap_after_registry_replacement_or_with_another_evidence_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            ledger = first / "ledger.json"
            ledger.write_text("[]")
            code = ("import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
                    "import pending_reviews as r; "
                    "guard=r.run_lock(Path(sys.argv[2])); "
                    "\ntry: guard.__enter__()\nexcept BlockingIOError: raise SystemExit(3)\n"
                    "else: guard.__exit__(None,None,None)\n")
            def attempt(path):
                return subprocess.run([sys.executable, "-c", code, str(Path(reviews.__file__).parent), str(path)],
                                      capture_output=True, text=True, timeout=30).returncode
            with reviews.run_lock(ledger):
                replacement = first / "replacement.json"
                replacement.write_text("[]")
                replacement.replace(ledger)
                self.assertEqual(attempt(ledger), 3)
                self.assertEqual(attempt(second / "ledger.json"), 0)
            self.assertEqual(attempt(ledger), 0)


    def test_read_only_discovery_remains_available_while_apply_locks_are_held(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ledger, evidence = root / "ledger.json", root / "evidence"
            ledger.write_text(json.dumps([entry()]))
            evidence.mkdir()
            with reviews.run_lock(ledger), (evidence / "pending-reviews.lock").open("a") as lock:
                reviews.fcntl.flock(lock, reviews.fcntl.LOCK_EX | reviews.fcntl.LOCK_NB)
                args = ["pending_reviews", "--ledger", str(ledger), "--evidence-dir", str(evidence)]
                fake = FakeIO()
                fake.comments = []
                with patch.object(sys, "argv", args), patch.object(reviews, "IO", return_value=fake), \
                        patch.object(reviews, "configured_bot_logins", return_value=[BOT]), \
                        patch.object(reviews, "automation_login", return_value=BOT), contextlib.redirect_stdout(StringIO()):
                    self.assertEqual(reviews.main(), 0)
                self.assertFalse(any(c[0] in {"review", "post"} for c in fake.calls))

    def test_handoff_with_same_evidence_directory_cannot_overlap_across_run_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first, second, evidence = root / "first", root / "second", root / "shared-evidence"
            first.mkdir()
            second.mkdir()
            with reviews.sweep_lock(first / "ledger.json", evidence, apply=True):
                with self.assertRaises(BlockingIOError):
                    with reviews.sweep_lock(second / "ledger.json", evidence, apply=True):
                        self.fail("handoff dispatched twice")


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(self.temp).resolve()
        self.enterContext(patch.dict(os.environ, {"CODE_HOME": str(self.home)}))
        self.native = "11111111-2222-4333-8444-555555555555"
        self.transcript = self.home / "sessions" / "native.jsonl"
        self.transcript.parent.mkdir()
        self.entry = {**entry(), "session_id": self.native, "codex_home": str(self.home),
                      "transcript": str(self.transcript)}
        self.transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.native}}) + "\n")

    def claude(self):
        self.entry.update(harness="claude", backend="tmux", tmux_socket="fixture",
                          tmux_session_id="tmux:fixture:" + "a" * 32,
                          pid=789, tty="ttys123", window_id="$1", tab_id="@2")
        self.transcript.write_text(json.dumps({"type": "assistant", "sessionId": self.native,
            "message": {"content": [{"type": "text", "text": "Waiting for review."}], "stop_reason": "end_turn"}}) + "\n")
        self.row = {"session_id": self.entry["tmux_session_id"], "dead": False, "pid": 456,
                    **{key: self.entry[key] for key in ("tty", "window_id", "tab_id", "tmux_socket")},
                    "screen": ["────────", "❯ ", "────────"],
                    "input_line": {"style": "empty", "text": "", "line_index": 1}}
        self.read = self.enterContext(patch("tmux_terminal.Terminal.read", side_effect=lambda _: copy.deepcopy(self.row)))
        self.send = self.enterContext(patch("tmux_terminal.Terminal.send", new_callable=AsyncMock))
        self.enterContext(patch("session_liveness.inventory", return_value=[(789, "ttys123", "claude")]))
        self.enterContext(patch("tmux_terminal.process_parents", return_value={789: 456, 456: 1}))

    def test_codex_notification_uses_supported_queue_with_exact_home_and_native_identity(self):
        with patch.object(notifications.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as queue:
            notifications.send(self.entry, "Read review")
        argv = queue.call_args.args[0]
        self.assertIn(str(Path(reviews.__file__).with_name("codex-session.py")), argv)
        self.assertIn("queue", argv)
        self.assertEqual(argv[argv.index("--session-id") + 1], self.native)
        self.assertEqual(Path(argv[argv.index("--codex-home") + 1]).resolve(), self.home)
        message = (argv[argv.index("--message") + 1] if "--message" in argv
                   else Path(argv[argv.index("--message-file") + 1]).read_text())
        self.assertEqual(message, "Read review")
        self.assertEqual(queue.call_args.kwargs["env"]["CODEX_HOME"], str(self.home))

    def test_codex_identity_or_home_mismatch_refuses_without_queue_and_preserves_helper_denial(self):
        for changed in ({"session_id": "another-thread"}, {"supervisor_owned": False},
                        {"codex_home": None}, {"codex_home": "relative"},
                        {"codex_home": str(self.transcript.parent)}):
            with self.subTest(changed=changed), patch.object(notifications.subprocess, "run") as queue:
                with self.assertRaises(notifications.NotSent) as failure:
                    notifications.send({**self.entry, **changed}, "Read review")
                self.assertTrue(failure.exception.needs_attention)
                queue.assert_not_called()
        with patch.object(notifications.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, '{"error":"ownership lease denied"}', "")) as queue:
            with self.assertRaisesRegex(ValueError, "ownership lease denied"):
                notifications.send(self.entry, "Read review")
            self.assertEqual(queue.call_count, 1)

    def test_claude_notification_reuses_send_only_after_native_idle_and_empty_prompt_checks(self):
        self.claude()
        notifications.send(self.entry, "Read review")
        self.assertEqual(self.read.call_count, 2)
        self.send.assert_awaited_once_with(self.entry["tmux_session_id"], "Read review")

    def test_native_queue_certainly_unsent_result_remains_distinct_from_uncertainty(self) -> None:
        for certain in (False, True):
            result = subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "not_sent": certain, "error": "queue unavailable"}), "")
            with self.subTest(certain=certain), patch.object(notifications.subprocess, "run", return_value=result):
                with self.assertRaises(ValueError) as failure:
                    notifications.send(self.entry, "Read review")
                self.assertEqual(isinstance(failure.exception, notifications.NotSent), certain)

    def test_explicit_supervisor_binding_does_not_change_worker_ownership_requirement(self) -> None:
        recipient = {**self.entry, "role": "supervisor"}
        recipient.pop("supervisor_owned")
        with patch.object(notifications.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as queue:
            with self.assertRaises(notifications.NotSent):
                notifications.send(recipient, "notice")
            queue.assert_not_called()
            notifications.send(recipient, "notice", supervisor=True)
            self.assertEqual(queue.call_count, 1)
            with self.assertRaises(notifications.NotSent):
                notifications.send({**recipient, "role": "worker"}, "notice", supervisor=True)
            self.assertEqual(queue.call_count, 1)

    def test_idle_scheduled_wakeup_does_not_suppress_review_but_firing_turn_does(self):
        self.claude()
        status = {"at_turn_end": True, "background_tasks": [{"task_id": "wake", "kind": "scheduled"}]}
        with patch.object(notifications, "session_status", return_value=status):
            notifications.send(self.entry, "Read review")
        self.send.assert_awaited_once_with(self.entry["tmux_session_id"], "Read review")
        self.send.reset_mock()
        with patch.object(notifications, "session_status", return_value={**status, "at_turn_end": False}), self.assertRaises(notifications.NotSent):
            notifications.send(self.entry, "Read review")
        self.send.assert_not_called()

    def test_claude_real_prompt_parser_accepts_cursor_padding_but_never_visible_input(self):
        from iterm_tab import ansi_screen, input_line
        self.claude()
        for captured, should_send in (("────────\n❯\u00a0\x1b[7m \x1b[0m\n────────", True),
                                      ("────────\n❯ \x1b[2m   \x1b[0m\n────────", True),
                                      ("────────\n❯ old input\n────────\nold output\n────────\n❯ \n────────", True),
                                      ("────────\n❯ \x1b[7mc\x1b[0m\x1b[2mandidate\x1b[0m\n────────", False),
                                      ("────────\n❯ \x1b[7mc\x1b[0mandidate\n────────", False),
                                      ("────────\n❯  \nwrapped input\n────────", False)):
            with self.subTest(captured=captured):
                screen, styles = ansi_screen(captured)
                self.row.update(screen=screen, input_line=input_line(screen, styles))
                self.send.reset_mock()
                if should_send:
                    notifications.send(self.entry, "Read review")
                    self.send.assert_awaited_once_with(self.entry["tmux_session_id"], "Read review")
                else:
                    with self.assertRaises(notifications.NotSent):
                        notifications.send(self.entry, "Read review")
                    self.send.assert_not_called()

    def test_claude_composer_refuses_nested_prompt_lines_incomplete_frames_and_old_output_fallback(self):
        from iterm_tab import ansi_screen, input_line
        self.claude()
        samples = ["❯ \n────────", "────────\n❯ ", "────────\n❯ \n  \n────────",
                   "  ────────\n  ❯ \n  ────────", "  ────────\n❯ \n────────",
                   "────────\n❯ \n  ────────", "────────\n❯ \n────────\n────────"]
        for marker in (">", "›", "❯"):
            samples.extend(f"────────\n❯ unfinished draft\n{typed_rule}  {marker}\n────────"
                           for typed_rule in ("", "  ────────\n"))
        samples.extend(f"────────\n{marker} \n────────" for marker in (">", "›"))
        samples.extend(f"────────\n❯ \n────────\noutput\n────────\n{current}\n────────"
                       for current in ("! command", "$ command", "agent ❯ draft"))
        for captured in samples:
            with self.subTest(captured=captured):
                screen, styles = ansi_screen(captured)
                self.row.update(screen=screen, input_line=input_line(screen, styles))
                with self.assertRaises(notifications.NotSent):
                    notifications.send(self.entry, "Read review")
        self.send.assert_not_called()

    def test_claude_nonempty_unknown_wrapped_active_changed_or_wrong_target_never_sends(self):
        self.claude()
        original = copy.deepcopy(self.row)
        for change in ({"input_line": {"style": s, "text": "draft", "line_index": 1}}
                       for s in ("dim", "non_dim", "mixed", "unknown", "empty")):
            self.row = {**original, **change}
            with self.subTest(change=change), self.assertRaises(notifications.NotSent):
                notifications.send(self.entry, "Read review")
        for change in ({"screen": ["────────", "❯ ", "wrapped draft", "────────"]}, {"dead": True},
                       {"tty": "ttys999"}, {"window_id": "$9"}, {"tab_id": "@9"}):
            self.row = {**original, **change}
            with self.subTest(change=change), self.assertRaises(notifications.NotSent):
                notifications.send(self.entry, "Read review")
        self.row = original
        for change in ({"session_id": "wrong"}, {"pid": 123}, {"backend": "iterm"}, {"tmux_session_id": "%1"}):
            with self.subTest(change=change), self.assertRaises(notifications.NotSent):
                notifications.send({**self.entry, **change}, "Read review")
        for changed_status in ({"at_turn_end": False, "background_tasks": []},
                               {"at_turn_end": True, "background_tasks": [{"task_id": "live"}]}):
            with patch.object(notifications, "session_status", return_value=changed_status), self.assertRaises(notifications.NotSent):
                notifications.send(self.entry, "Read review")
        self.read.side_effect = [original, {**original, "input_line": {"style": "non_dim", "text": "new draft", "line_index": 1}}]
        with self.assertRaises(notifications.NotSent):
            notifications.send(self.entry, "Read review")
        self.send.assert_not_called()

    def test_preflight_races_and_pre_key_checks_are_not_sent_but_denials_and_send_failures_stay_spent(self):
        from iterm_tab import SendFailure
        for error in (ValueError("changed transcript"), FileNotFoundError("not yet written")):
            with patch.object(notifications, "session_status", side_effect=error), self.assertRaises(notifications.NotSent):
                notifications.send(self.entry, "Read review")
        with patch.object(notifications, "session_status", side_effect=PermissionError("denied")), self.assertRaises(PermissionError):
            notifications.send(self.entry, "Read review")
        self.claude()
        self.send.side_effect = ValueError("pre-key check")
        with self.assertRaises(notifications.NotSent):
            notifications.send(self.entry, "Read review")
        self.send.side_effect = SendFailure(self.entry["tmux_session_id"], "unknown", "fixture")
        with self.assertRaises(SendFailure):
            notifications.send(self.entry, "Read review")

    def test_native_read_races_are_quiet_but_stable_malformed_transcripts_need_attention(self):
        original = session_record.read_jsonl
        for malformed in (False, True):
            with self.subTest(malformed=malformed), patch.object(notifications.subprocess, "run") as queue:
                def racing_read(path):
                    if malformed:
                        with path.open("a") as stream:
                            stream.write('{"unfinished":')
                    else:
                        with path.open("a") as stream:
                            stream.write("\n")
                    return original(path)
                with patch.object(session_record, "read_jsonl", side_effect=racing_read):
                    with self.assertRaises(notifications.NotSent) as failure:
                        notifications.send(self.entry, "Read review")
                self.assertFalse(failure.exception.needs_attention)
                self.assertIsInstance(failure.exception.__cause__, session_record.TranscriptChanged)
                if malformed:
                    with self.assertRaises(notifications.NotSent) as failure:
                        notifications.send(self.entry, "Read review")
                    self.assertTrue(failure.exception.needs_attention)
                queue.assert_not_called()
        def denied_read(path):
            with path.open("a") as stream:
                stream.write("\n")
            raise PermissionError("fixture read denied during append")
        with patch.object(session_record, "read_jsonl", side_effect=denied_read):
            with self.assertRaises(PermissionError):
                notifications.send(self.entry, "Read review")

    def test_claude_live_waits_are_quiet_and_route_problems_require_attention(self):
        self.claude()
        for status in ({"at_turn_end": False, "background_tasks": []},
                       {"at_turn_end": True, "background_tasks": [{"task_id": "live"}]}):
            with patch.object(notifications, "session_status", return_value=status):
                with self.assertRaises(notifications.NotSent) as failure:
                    notifications.send(self.entry, "Read review")
                self.assertFalse(failure.exception.needs_attention)
        for changed in ({"backend": "iterm"}, {"tmux_socket": None}, {"tab_id": "@wrong"}):
            with self.subTest(changed=changed), self.assertRaises(notifications.NotSent) as failure:
                notifications.send({**self.entry, **changed}, "Read review")
            self.assertTrue(failure.exception.needs_attention)
        self.row["input_line"]["text"] = "draft"
        with self.assertRaises(notifications.NotSent) as failure:
            notifications.send(self.entry, "Read review")
        self.assertFalse(failure.exception.needs_attention)
        self.send.assert_not_called()


class AdapterTests(unittest.TestCase):
    def test_failed_refused_incomplete_and_empty_outputs_never_succeed(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "review.md"
            prompt = Path(folder) / "prompt.md"
            prompt.write_text("Inspect source")
            io = reviews.IO.__new__(reviews.IO)
            valid = {"ok": True, "provider": "anthropic", "response_file": str(output)}
            for changes, code, text in [({"ok": False, "error": "denied"}, 1, "partial"),
                                        ({"outcome": "refused"}, 0, "partial"),
                                        ({"outcome": "incomplete"}, 0, "partial"),
                                        ({}, 0, " "), ({"provider": "openai"}, 0, "review")]:
                with self.subTest(changes=changes, code=code, text=text):
                    output.write_text(text)
                    result = subprocess.CompletedProcess([], code, json.dumps({**valid, **changes}), "")
                    with patch.object(reviews, "run", return_value=result), self.assertRaises(reviews.ReviewError):
                        io.review("openai", Path(folder), prompt, output)

    def test_provider_selection_passes_actual_author_and_other_provider_to_policy_helper(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "result.md"
            output.write_text("none")
            io = reviews.IO.__new__(reviews.IO)
            for author, reviewer in [("openai", "anthropic"), ("anthropic", "openai")]:
                result = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "provider": reviewer,
                                                                       "response_file": str(output)}), "")
                with patch.object(reviews, "run", return_value=result) as run:
                    io.review(author, root, root / "prompt.md", output)
                    command = run.call_args.args[0]
                    self.assertEqual(command[command.index("--author-provider") + 1], author)
                    self.assertEqual(command[command.index("--provider") + 1], reviewer)

    def test_adapter_accepts_real_model_review_cli_metadata_and_completed_output(self):
        fixture = model_fixture.ReviewWithModelTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.install("claude", model_fixture.FAKE_CLAUDE)
        output = fixture.root / "result.md"
        def real_helper(args, **kwargs):
            code, helper_metadata = fixture.run_helper(*args[3:], FAKE_CLAUDE_JSON=json.dumps({
                "result": model_fixture.completed("Completed fixture review.")}))
            return subprocess.CompletedProcess(args, code, json.dumps(helper_metadata), "")
        with patch.object(reviews, "run", side_effect=real_helper):
            metadata = reviews.IO.__new__(reviews.IO).review("openai", fixture.repo, fixture.prompt, output)
        self.assertTrue(metadata["ok"])
        self.assertEqual(output.read_text().strip(), "Completed fixture review.")

    def test_review_budget_is_owned_by_model_review_helper(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "result.md"
            output.write_text("none")
            metadata = {"ok": True, "provider": "anthropic", "response_file": str(output)}
            def helper(args, **kwargs):
                if kwargs.get("timeout") is not None:
                    raise subprocess.TimeoutExpired(args, kwargs["timeout"])
                return subprocess.CompletedProcess(args, 0, json.dumps(metadata), "")
            with patch.object(reviews, "run", side_effect=helper):
                self.assertTrue(reviews.IO.__new__(reviews.IO).review("openai", root, root / "prompt.md", output)["ok"])

    def test_reconciled_comment_write_is_successful_without_replay(self):
        io = reviews.IO.__new__(reviews.IO)
        result = subprocess.CompletedProcess([], 0, json.dumps({**post_payload(reconciled=True),
                                                               "comment": {"url": "https://example.test/recovered"}}), "")
        with patch.object(reviews, "run", return_value=result) as run:
            self.assertEqual(io.post(REPO, 7, Path("/fixture/comment.md"))["comment"], json.loads(result.stdout)["comment"])
        self.assertEqual(run.call_count, 1)

    def test_poster_relays_model_references_and_build_logs_through_real_comment_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body, saved, fake_gh = root / "review.md", root / "saved.json", root / "gh"
            review = "Reviewer: issue #550; BuildKit #5 [2/4] RUN."
            body.write_text(review)
            fake_gh.write_text(
                f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
                "args = sys.argv[1:]\npath = next(arg for arg in args if arg.startswith('/'))\n"
                "method = args[args.index('--method') + 1]\n"
                "if path == '/user': print(json.dumps({'login': 'review-bot[bot]'}))\n"
                "elif method == 'GET': print('[]')\n"
                "else:\n body = json.load(sys.stdin)['body']\n"
                f" Path({str(saved)!r}).write_text(json.dumps(body))\n"
                " print(json.dumps({'id': 11, 'user': {'login': 'review-bot[bot]'}, 'body': body, "
                "'html_url': 'https://github.com/example/tool/pull/7#issuecomment-11'}))\n"
            )
            fake_gh.chmod(0o755)
            def run_comment(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                return subprocess.run([sys.executable, *command[2:]], capture_output=True, text=True, **kwargs)
            with patch.dict(os.environ, {"GH_PR_GH": str(fake_gh), "CODEX_SKILLS_ENV_FILE": str(root / "missing.env"),
                                         "CODEX_AUTOMATION_LOGIN": BOT,
                                         "GH_WITH_ENV_TOKEN_EXPECTED_LOGIN": BOT}), \
                    patch.object(reviews, "run", side_effect=run_comment):
                result = reviews.IO.__new__(reviews.IO).post(REPO, 7, body)
            self.assertTrue(result["ok"])
            self.assertTrue(json.loads(saved.read_text()).startswith(review))

    def test_post_cannot_inherit_personal_identity_approval_from_supervisor_environment(self):
        io = reviews.IO.__new__(reviews.IO)
        result = subprocess.CompletedProcess([], 0, json.dumps(post_payload()), "")
        fixture_env = {OWN_USER_OPT_IN: "1", "PATH": "/fixture/bin", "REVIEW_FIXTURE": "preserve"}
        with patch.dict(os.environ, fixture_env, clear=True), patch.object(reviews, "run", return_value=result) as command:
            io.post(REPO, 7, Path("/fixture/comment.md"))
            child_env = command.call_args.kwargs["env"]
            self.assertNotIn(OWN_USER_OPT_IN, child_env)
            self.assertEqual(child_env["REVIEW_FIXTURE"], "preserve")
            self.assertEqual(os.environ[OWN_USER_OPT_IN], "1")

    def test_post_unknown_does_not_replay(self):
        io = reviews.IO.__new__(reviews.IO)
        result = subprocess.CompletedProcess([], 1, json.dumps(post_payload(ok=False, write_outcome="unknown")), "")
        with patch.object(reviews, "run", return_value=result) as run, self.assertRaises(reviews.ReviewError):
            io.post(REPO, 7, Path("/fixture/comment.md"))
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
