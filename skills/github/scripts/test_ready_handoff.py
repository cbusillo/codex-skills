#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Ready comments round-trip through the watcher and never hide red CI."""

import argparse
import base64
import copy
import importlib.util
import json
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from github_api import ApiResult
from github_read import GitHubReadError, GitHubReadShapeError
from ready_handoff import READY, handoff_body, latest_ready
import ready_handoff

spec = importlib.util.spec_from_file_location("ready_pr", Path(__file__).with_name("gh-pr.py"))
assert spec and spec.loader
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

SHA = "a" * 40
REPO = "example/tool"
OWNER_REVIEW_ACCEPTED = {"id": 2, "name": "launchplane/owner-review", "status": "completed", "conclusion": "success"}
PR = {"number": 1, "state": "open", "draft": False, "labels": [], "changed_files": 1, "head": {"sha": SHA}, "base": {"ref": "main"}, "user": {"login": "engineer"}}


class Reader:
    def __init__(self):
        self.pr = copy.deepcopy(PR)
        self.checks: list[dict[str, Any]] = [{"id": 1, "name": "unit", "status": "completed", "conclusion": "success"}]
        self.statuses = []
        self.runs = []
        self.jobs = []
        self.comments: list[dict[str, Any]] = []
        self.config = None
        self.files = [{"filename": "src/tool.py"}]
        self.current_pr = None
        self.pr_reads = 0
        self.unavailable = False
        self.last_result = None
        self.failed_results = []
        self.completed_steps = []
        self.actor = "engineer"
        self.expected_actor = "engineer"
        self.issues: dict[int, dict[str, Any]] = {}
        self.sub_issues: dict[int, list[dict[str, Any]]] = {}
        self.closing: list[int] = []
        self.issue_comments: dict[int, dict[str, Any]] = {}

    def get_json(self, path, **_kwargs):
        if path.endswith("/pulls/1"):
            self.pr_reads += 1
            return copy.deepcopy(self.current_pr if self.pr_reads > 1 and self.current_pr else self.pr)
        if path.endswith("/status"):
            return {"state": "success"}
        if f"/contents/{ready_handoff.REVIEW_CONFIG_PATH}?ref=main" in path:
            if self.config is None:
                raise GitHubReadError("Not Found", result=ApiResult(ok=False, status=404, body=None), diagnostics={})
            return {"content": base64.b64encode(json.dumps(self.config).encode()).decode()}
        if path.startswith(f"/repos/{REPO}/issues/comments/"):
            return copy.deepcopy(self.issue_comments[int(path.rsplit("/", 1)[1])])
        if path.startswith(f"/repos/{REPO}/issues/"):
            number = int(path.rsplit("/", 1)[1])
            if number not in self.issues:
                raise GitHubReadError("Not Found", result=ApiResult(ok=False, status=404, body=None), diagnostics={})
            return copy.deepcopy(self.issues[number])
        raise AssertionError(path)

    def paged_json(self, path, **_kwargs):
        if path.endswith("/check-runs"):
            if self.unavailable:
                raise GitHubReadShapeError("fixture check read unavailable")
            return self.checks
        if path.endswith("/statuses"):
            return self.statuses
        if path.endswith("/actions/runs"):
            return self.runs
        if path.endswith("/jobs"):
            return self.jobs
        if path.endswith("/issues/1/comments"):
            return self.comments
        if path.endswith("/pulls/1/files"):
            return self.files
        if path.endswith("/sub_issues"):
            return self.sub_issues.get(int(path.split("/")[-2]), [])
        if path.endswith("/comments"):
            return []
        raise AssertionError(path)

    def graphql_json(self, _query, variables, **_kwargs):
        assert variables["number"] == 1
        nodes = [{"number": number, "repository": {"nameWithOwner": REPO}} for number in self.closing]
        pull = {"merged": False, "url": "fixture", "closingIssuesReferences": {"nodes": nodes, "pageInfo": {"hasNextPage": False}}}
        return ApiResult(ok=True, status=200, body={"data": {"repository": {"pullRequest": pull}}})

    @staticmethod
    def diagnostics():
        return {"degraded": False, "degradedComponents": []}

    @staticmethod
    def retry_summary():
        return None


class ReviewRecordTests(unittest.TestCase):
    def test_explicit_result_accepts_all_weighed_disposition_formats(self):
        for body in (
            "Acted on: the cache bug.\nDeclined: the wording nit, not reproducible.",
            "Review weighed: two findings deferred to #123",
            "Review weighed: one finding unresolved (could not test), tracked in #456",
            "Review weighed: declined both findings: not reproducible.",
        ):
            with self.subTest(body=body):
                record = ready_handoff.review_result_body(body, SHA)
                self.assertEqual(ready_handoff.review_record(record), "outcome")
                self.assertEqual(ready_handoff.review_receipt_head(record), SHA)

    def test_prose_does_not_attest_completion_or_weighing(self):
        for body in (
            "Review of record: OpenAI reviewed 3 of 10 files; no findings. 7 files remain.",
            "Review of record: OpenAI launched\n\nPreliminary findings: none yet; still running.",
            "Review of record: OpenAI, run by the Supervisor\n\n3 findings: not yet weighed.",
            "Review weighed: no findings.",
        ):
            with self.subTest(body=body):
                self.assertIsNone(ready_handoff.review_record(f"{body}\n\nHead: {SHA}"))

    def test_request_leads_remain_supported_without_product_or_quote_false_holds(self):
        for body in ("Review prompt for the Anthropic review of record.",
                     "Review of record needed: OpenAI.",
                     "The helper fails, so **Supervisor runs the review**:"):
            self.assertEqual(ready_handoff.review_record(body), "request")
        for body in ("Review request: OpenAI API key docs", "> Supervisor runs the review.",
                     "No model review is needed; the Supervisor runs the review only for skill paths."):
            self.assertIsNone(ready_handoff.review_record(body))

    def test_quoted_malformed_or_unbound_completion_is_not_evidence(self):
        record = ready_handoff.review_result_body("Acted on: all findings.", SHA)
        for body in ("> " + record, "```\n" + record + "\n```", record.replace(SHA, SHA[:7]),
                     record.replace(ready_handoff.REVIEW_RESULT, "Review result: pending")):
            self.assertNotEqual(ready_handoff.review_record(body), "outcome")


class ReviewCommentTests(unittest.TestCase):
    def args(self, head):
        return argparse.Namespace(repo=REPO, pr="1", body_file="fixture", reviewed_head=head,
            edit_last=False, edit_comment=None, expected_body_file=None,
            expected_updated_at=None, create_if_none=False)

    def test_existing_comment_writer_posts_head_bound_disposition(self):
        body = "Acted on: cache bug.\nDeclined: wording nit."
        with (patch.object(helper, "resolve_pr", return_value=(REPO, 1)),
              patch.object(helper, "read_text_file", return_value=body),
              patch.object(helper, "shared_comment", return_value={"url": "fixture"}) as writer):
            helper.cmd_comment(self.args(SHA))
        posted = writer.call_args.args[3]
        self.assertEqual(posted, ready_handoff.review_result_body(body, SHA))
        self.assertEqual(ready_handoff.review_receipt_head(posted), SHA)

    def test_invalid_head_and_edit_mode_refuse_before_writing(self):
        for head, edit in (("short", False), (SHA, True)):
            args = self.args(head)
            args.edit_last = edit
            with (patch.object(helper, "resolve_pr", return_value=(REPO, 1)),
                  patch.object(helper, "read_text_file", return_value="Acted on: all."),
                  patch.object(helper, "shared_comment") as writer):
                with self.assertRaises(helper.PrHelperError):
                    helper.cmd_comment(args)
                writer.assert_not_called()


class ReadyTests(unittest.TestCase):
    def setUp(self):
        self.reader = Reader()
        self.args = argparse.Namespace(repo=REPO, pr="1", waiting_for_client=False, allow_no_checks=False)
        self.reader_patch = patch.object(helper.github_read_core, "GitHubReader", return_value=self.reader)
        self.writer_patch = patch.object(helper.github_comment_core, "comment", return_value={"ok": True, "url": "fixture"})
        self.reader_factory = self.reader_patch.start()
        self.writer = self.writer_patch.start()
        self.addCleanup(self.reader_patch.stop)
        self.addCleanup(self.writer_patch.stop)
        states = ready_handoff.acceptance_reader()
        self.client_repository = states.ClientRelease(states.CLIENT)
        client_release_patch = patch.object(ready_handoff, "client_release", return_value=states.ClientRelease(states.NO_CLIENT))
        self.client_release = client_release_patch.start()
        self.addCleanup(client_release_patch.stop)
        helper.CURRENT_OPERATION = "github.pr.ready"

    def post(self):
        result = helper.cmd_ready(self.args)
        body = self.writer.call_args.args[2]
        self.assertEqual(self.writer.call_args.kwargs["operation"], "github.pr.ready")
        self.assertEqual(self.writer.call_args.kwargs["repo"], REPO)
        return result, {"body": body, "user": {"login": "engineer"}, "id": 1, "created_at": "2026-01-01T00:00:00Z"}

    def refuse(self) -> str:
        with self.assertRaises(helper.PrHelperError) as caught:
            helper.cmd_ready(self.args)
        self.writer.assert_not_called()
        return str(caught.exception)

    def test_posted_body_round_trips_and_push_invalidates(self):
        result, comment = self.post()
        self.assertEqual(result["headSha"], SHA)
        self.assertEqual(comment["body"], f"{READY}\n\nHead: {SHA}")
        self.assertTrue(latest_ready(self.reader.pr, [comment], {"engineer"}))
        provider_comment = {**comment, "body": helper.github_api_core.body_with_operation_marker(comment["body"], "fixture")}
        self.assertTrue(latest_ready(self.reader.pr, [provider_comment], {"engineer"}))
        self.assertIs(self.reader_factory.call_args.kwargs["cache_enabled"], False)
        self.reader.pr["head"]["sha"] = "b" * 40
        self.assertFalse(latest_ready(self.reader.pr, [comment], {"engineer"}))
        self.assertFalse(latest_ready(self.reader.pr, [provider_comment], {"engineer"}))

    def test_empty_checks_wait_for_ci_with_supported_no_ci_route(self):
        self.reader.checks = []
        self.refuse()
        self.args.allow_no_checks = True
        self.post()

    def test_no_ci_option_never_bypasses_pending_checks(self):
        self.args.allow_no_checks = True
        self.reader.checks[0]["status"] = "queued"
        self.refuse()

    def test_pending_and_failed_checks_refuse(self):
        for status, conclusion in (("queued", None), ("in_progress", None), ("completed", "failure"), ("completed", "cancelled")):
            with self.subTest(status=status, conclusion=conclusion):
                self.reader.checks[0].update(status=status, conclusion=conclusion)
                self.refuse()

    def test_legacy_statuses_refuse(self):
        for state in ("pending", "failure", "error"):
            with self.subTest(state=state):
                self.reader.statuses = [{"id": 2, "context": "legacy-ci", "state": state}]
                self.refuse()

    def test_unfinished_or_failed_workflow_without_job_checks_refuses(self):
        for status, conclusion in (("queued", None), ("completed", "failure")):
            with self.subTest(status=status):
                self.reader.runs = [{"id": 3, "workflow_id": 2, "head_sha": SHA, "event": "pull_request", "status": status, "conclusion": conclusion}]
                self.refuse()

    def test_stuck_workflow_settles_from_aged_completed_jobs(self) -> None:
        self.reader.runs = [{
            "id": 3, "workflow_id": 2, "run_number": 1, "run_attempt": 2,
            "check_suite_id": 4, "head_repository": {"id": 5}, "head_branch": "feature",
            "head_sha": SHA, "event": "pull_request", "status": "in_progress",
        }]
        job = {
            "id": 6, "run_id": 3, "run_attempt": 2, "head_sha": SHA,
            "status": "completed", "conclusion": "success", "completed_at": "2026-10-22T19:00:00Z",
        }
        now = datetime.fromisoformat(job["completed_at"]).timestamp() + helper.github_read_core.WORKFLOW_SETTLEMENT_GRACE_SECONDS
        with patch.object(helper.github_read_core.time, "time", return_value=now):
            for conclusion in ("success", "neutral", "skipped"):
                with self.subTest(conclusion=conclusion):
                    self.reader.jobs = [{**job, "conclusion": conclusion}]
                    result, _ = self.post()
                    self.assertEqual(result["summary"]["unfinishedWorkflowRunCount"], 0)
                    settlement = result["summary"]["jobLevelSettlements"][0]
                    self.assertEqual(settlement["runId"], 3)

    def test_stuck_workflow_never_hides_unsettled_or_failed_jobs(self) -> None:
        self.reader.runs = [{
            "id": 3, "workflow_id": 2, "run_number": 1, "run_attempt": 2,
            "check_suite_id": 4, "head_repository": {"id": 5}, "head_branch": "feature",
            "head_sha": SHA, "event": "pull_request", "status": "in_progress",
        }]
        job = {
            "id": 6, "run_id": 3, "run_attempt": 2, "head_sha": SHA,
            "status": "completed", "conclusion": "success", "completed_at": "2026-10-22T19:00:00Z",
        }
        now = datetime.fromisoformat(job["completed_at"]).timestamp() + helper.github_read_core.WORKFLOW_SETTLEMENT_GRACE_SECONDS
        recent = datetime.fromtimestamp(now, tz=datetime.fromisoformat(job["completed_at"]).tzinfo).isoformat()
        inventories = [
            [], [{**job, "status": "in_progress"}], [{**job, "conclusion": "failure"}],
            [{**job, "conclusion": None}], [{**job, "completed_at": recent}],
            [{**job, "completed_at": None}], [{**job, "completed_at": "invalid"}],
            [{**job, "completed_at": "2026-10-22T19:00:00"}],
            [{**job, "run_attempt": 1}], [{**job, "run_id": 99}], [{**job, "head_sha": "other"}],
            [job, {**job, "id": 7, "status": "queued"}],
        ]
        with patch.object(helper.github_read_core.time, "time", return_value=now):
            for jobs in inventories:
                with self.subTest(jobs=jobs):
                    self.reader.jobs = jobs
                    self.refuse()

    def test_incomplete_reads_refuse(self):
        self.reader.unavailable = True
        self.refuse()

    def test_hold_closed_and_draft_refuse(self):
        for change in ({"labels": [{"name": "merge-blocked"}]}, {"state": "closed"}, {"draft": True}):
            with self.subTest(change=change):
                self.reader.pr = {**PR, **change}
                self.refuse()

    def test_conflicting_base_refuses(self):
        for change in ({"mergeable": False, "mergeable_state": "dirty"}, {"mergeable": False}, {"mergeable_state": "dirty"}):
            with self.subTest(change=change):
                self.reader.pr_reads = 0
                self.reader.pr = {**PR, **change}
                self.assertIn("babysit-pr Conflicts", self.refuse())

    def test_conflict_found_by_fresh_read_refuses(self):
        self.reader.current_pr = {**self.reader.pr, "mergeable": False, "mergeable_state": "dirty"}
        self.refuse()

    def test_unknown_or_clean_mergeability_posts(self):
        for change in ({"mergeable": None, "mergeable_state": "unknown"}, {"mergeable": True, "mergeable_state": "clean"}, {"mergeable": True, "mergeable_state": "blocked"}):
            with self.subTest(change=change):
                self.reader.pr_reads = 0
                self.reader.pr = {**PR, **change}
                self.post()

    def test_push_or_hold_during_reads_refuses(self):
        for change in ({"head": {"sha": "b" * 40}}, {"labels": [{"name": "merge-blocked"}]}, {"labels": [{"name": "owner-review"}]}):
            with self.subTest(change=change):
                self.reader.pr_reads = 0
                self.reader.current_pr = {**self.reader.pr, **change}
                self.refuse()

    def review_comment(self, body, minute, login="engineer", *, reviewed_head=None):
        if reviewed_head is not None:
            body = ready_handoff.review_result_body(body, reviewed_head)
        self.reader.comments.append({
            "id": len(self.reader.comments) + 1, "user": {"login": login}, "body": body,
            "created_at": f"2026-01-01T00:{minute:02d}:00Z",
        })

    def test_pending_review_of_record_refuses_until_a_later_outcome(self):
        self.review_comment("Review prompt for the OpenAI review of record. **Supervisor runs the review.**", 1)
        self.refuse()
        self.review_comment("Review by another model is blocked by #2069; the Supervisor still needs to run it.", 2)
        self.refuse()
        self.review_comment("**Review of record: OpenAI, run by the Supervisor** at head `aaaaaaa`.", 3)
        self.refuse()
        self.review_comment("Acted on: both findings. Declined: none.", 3, reviewed_head=SHA)
        self.post()
        self.writer.reset_mock()
        self.review_comment("Review of record still needed after the fix push.", 4)
        self.refuse()

    def test_unfinished_status_never_clears_a_requested_or_required_review(self):
        for status in (
            "Review of record: pending.",
            "Review of record: OpenAI is still running.",
            "Review of record: OpenAI; could not complete the review.",
        ):
            for requested in (False, True):
                with self.subTest(status=status, requested=requested):
                    self.reader.comments = []
                    self.reader.config = {"qualityGate": {"modelReview": {"requiredPaths": ["AGENTS.md"]}}}
                    self.reader.files = [{"filename": "AGENTS.md"}]
                    if requested:
                        self.review_comment("Review prompt for the review of record.", 6)
                    self.review_comment(f"{status}\n\nHead: {SHA}", 7)
                    self.refuse()

    def test_completed_review_with_code_defect_allows_ready(self):
        self.review_comment("Review prompt for the review of record.", 6)
        self.review_comment(
            f"Review weighed: OpenAI completed the review and found that the helper did not run required checks; fixed.\n\nHead: {SHA}", 7, reviewed_head=SHA,
        )
        self.post()

    def test_multiline_fixed_code_refusal_is_a_completed_disposition(self):
        self.review_comment("Review prompt for the review of record.", 6)
        self.review_comment(f"Review of record: OpenAI\n\nHead: {SHA}\n\nFixed: the helper declined to review drafts.", 7, reviewed_head=SHA)
        self.post()

    def test_product_review_request_does_not_hold_ready(self):
        self.review_comment("Review request for the Google login button UI.", 6)
        self.post()

    def test_supervisor_delegation_is_distinct_from_generic_requests_and_code_examples(self):
        for body in ("Review request for another model", "Review prompt for model-review"):
            self.assertEqual(ready_handoff.review_record(body), "request")
            self.assertFalse(ready_handoff.supervisor_review_request(body))
        directive = "Supervisor runs the review."
        self.assertTrue(ready_handoff.supervisor_review_request(directive))
        for example in (f"> {directive}", f"    {directive}", f"```text\n{directive}\n```",
                        f"~~~text\n{directive}\n~~~", f"<!-- {directive} -->"):
            self.assertFalse(ready_handoff.supervisor_review_request(example))
            self.assertIsNone(ready_handoff.review_record(example))

    def test_untrusted_review_request_does_not_hold_ready(self):
        self.review_comment("Supervisor runs the review.", 1, login="drive-by")
        self.post()

    def test_review_required_paths_need_an_outcome_for_the_current_head(self):
        self.reader.config = {"qualityGate": {"modelReview": {"requiredPaths": ["skills/*/SKILL.md"]}}}
        self.post()
        self.writer.reset_mock()
        self.reader.files = [{"filename": "skills/github/SKILL.md"}]
        self.refuse()
        self.review_comment(f"Review weighed: none to act on.\n\nHead: {'b' * 40}", 4, reviewed_head="b" * 40)
        self.refuse()
        self.review_comment(f"Review weighed at `{SHA}`: nothing to act on.", 6, reviewed_head=SHA)
        self.post()

    def test_push_invalidates_a_requested_review_without_required_paths(self):
        self.review_comment("Review prompt for the review of record.", 1)
        self.review_comment("Acted on: all findings.", 2, reviewed_head=SHA)
        self.post()
        self.writer.reset_mock()
        changed_head = "b" * 40
        self.reader.pr["head"]["sha"] = changed_head
        self.refuse()
        self.review_comment("Deferred: tracked with reasons after weighing the fix.", 3, reviewed_head=changed_head)
        self.post()

    def test_old_review_cannot_cover_a_previously_unpushed_commit(self):
        self.reader.config = {"qualityGate": {"modelReview": {"requiredPaths": ["AGENTS.md"]}}}
        self.reader.files = [{"filename": "AGENTS.md"}]
        # B may be committed before this review but pushed after it. Dates do
        # not identify the reviewed source; the outcome covers only A.
        self.review_comment(f"Review weighed: no findings.\n\nHead: {'b' * 40}", 6, reviewed_head="b" * 40)
        self.refuse()
        self.review_comment(f"Review weighed: no findings.\n\nHead: {SHA}", 7, reviewed_head=SHA)
        self.post()

    def test_no_head_or_untrusted_owner_outcome_is_not_current_evidence(self):
        self.reader.config = {"qualityGate": {"modelReview": {"requiredPaths": ["AGENTS.md"]}}}
        self.reader.files = [{"filename": "AGENTS.md"}]
        self.review_comment("Review weighed: no findings.", 6)
        self.refuse()
        self.review_comment(f"Review weighed: no findings.\n\nHead: {SHA}", 7, login="repository-owner", reviewed_head=SHA)
        self.refuse()

    def test_provider_refusal_and_progress_with_a_head_cannot_clear_ready(self):
        self.reader.config = {"qualityGate": {"modelReview": {"requiredPaths": ["AGENTS.md"]}}}
        self.reader.files = [{"filename": "AGENTS.md"}]
        for body in (
            "Review of record: OpenAI declined to review (policy).",
            "Review of record: OpenAI reviewed 3 of 10 files; no findings. 7 files remain.",
            "Review of record: OpenAI launched\n\nPreliminary findings: none yet; still running.",
            "Review of record: OpenAI, run by the Supervisor\n\n3 findings: not weighed yet.",
        ):
            with self.subTest(body=body):
                self.reader.comments = []
                self.review_comment(f"{body}\n\nHead: {SHA}", 7)
                self.refuse()

    def test_timing_summary_and_explicit_head_allow_ready(self):
        self.review_comment("Review prompt for the review of record.", 6)
        self.review_comment(f"Review weighed: OpenAI, after two retries, completed the review; no findings.\n\nHead: {SHA}", 7, reviewed_head=SHA)
        self.post()

    def test_bare_date_or_hex_word_cannot_identify_a_reviewed_head(self):
        self.review_comment("Review prompt for the review of record.", 6)
        for marker in ("20261017", "deadbeef"):
            with self.subTest(marker=marker):
                self.reader.pr["head"]["sha"] = marker + "a" * (40 - len(marker))
                self.reader.comments = self.reader.comments[:1]
                self.review_comment(f"Review weighed at {marker}: no findings.", 7)
                self.refuse()
                self.review_comment(f"Review weighed at `{self.reader.pr['head']['sha']}`: no findings.", 8, reviewed_head=self.reader.pr["head"]["sha"])
                self.post()
                self.writer.reset_mock()

    def test_incomplete_or_missing_file_count_refuses_required_path_coverage(self):
        self.reader.config = {"qualityGate": {"modelReview": {"requiredPaths": ["AGENTS.md"]}}}
        self.reader.files = [{"filename": "src/tool.py"}]
        # A matching path may be omitted by GitHub's 3000-file ceiling.
        self.reader.pr["changed_files"] = 3001
        self.refuse()
        self.reader.pr.pop("changed_files")
        self.refuse()

    def test_renamed_review_required_path_still_needs_an_outcome(self):
        self.reader.config = {"qualityGate": {"modelReview": {"requiredPaths": ["AGENTS.md"]}}}
        self.reader.files = [{"filename": "docs/AGENTS-old.md", "previous_filename": "AGENTS.md"}]
        self.refuse()

    def test_nonobject_review_configuration_refuses_without_traceback(self):
        for config in ([], "bad", {"qualityGate": [1]}, {"qualityGate": {"modelReview": [1]}}):
            with self.subTest(config=config):
                self.reader.config = config
                self.refuse()

    def test_unreadable_review_paths_refuse(self):
        self.reader.config = {"qualityGate": {"modelReview": {"requiredPaths": "AGENTS.md"}}}
        self.refuse()

    def test_waiting_for_client_invalidates_ready_without_bypassing_ci(self):
        self.args.waiting_for_client = True
        self.reader.pr["body"] = "## Client test notes\nClient change: a new feature.\nTry it."
        self.reader.pr["labels"] = [{"name": "owner-review"}]
        self.reader.checks.append({"id": 2, "name": "launchplane/owner-review", "status": "in_progress", "conclusion": None})
        _, waiting = self.post()
        ready = {**waiting, "id": 0, "body": handoff_body(SHA)}
        self.assertFalse(latest_ready(self.reader.pr, [ready, waiting], {"engineer"}))
        self.writer.reset_mock()
        self.reader.checks[0]["status"] = "queued"
        self.refuse()
        self.reader.checks[0].update(status="completed", conclusion="failure")
        self.refuse()

    def test_owner_review_requires_success_for_ready(self):
        self.reader.pr["labels"] = [{"name": "owner-review"}]
        self.assertIn("Client test notes are missing", self.refuse())
        self.reader.pr["body"] = "## Client test notes\nClient change: a new feature.\nTry it."
        self.assertIn("--waiting-for-client", self.refuse())
        self.reader.checks.append({"id": 2, "name": "launchplane/owner-review", "status": "completed", "conclusion": "success"})
        _, comment = self.post()
        self.assertTrue(latest_ready(self.reader.pr, [comment], {"engineer"}))

    def test_real_client_notes_require_acceptance_without_label(self):
        self.client_release.return_value = self.client_repository
        for heading in ("Client", "Owner"):
            with self.subTest(heading=heading):
                self.reader.pr["body"] = f"## {heading} test notes\nClient change: a new feature.\nTry the new feature."
                message = self.refuse()
                self.assertIn("--waiting-for-client", message)
                self.assertIn(ready_handoff.NOTHING_TO_TEST, message)
        self.args.waiting_for_client = True
        _, comment = self.post()
        self.assertFalse(latest_ready(self.reader.pr, [comment], {"engineer"}))

    def test_nothing_to_test_uses_watcher_exemption(self):
        self.reader.pr["body"] = "## Client test notes\nNothing for the Client to test: automated tooling."
        self.post()

    def test_engineering_handoff_refuses_notes_asking_the_client_to_check_at_release(self):
        release_steps = f"{ready_handoff.ENGINEERING_RELEASE_TESTS}\n\nAfter the real-data import, check the imported parts."
        self.client_release.return_value = self.client_repository
        self.reader.checks.append({"id": 2, "name": "launchplane/owner-review", "status": "completed", "conclusion": "neutral"})
        for notes in (release_steps, f"### #10\n{release_steps}\n### #11\nNothing for the Client to test: internal tooling."):
            for waiting in (False, True):
                with self.subTest(notes=notes, waiting=waiting):
                    self.reader.pr["body"] = f"## Client test notes\n{notes}"
                    self.args.waiting_for_client = waiting
                    self.assertIn(ready_handoff.NOTHING_TO_TEST, self.refuse())

    def test_client_handoff_needs_a_named_client_change(self):
        self.args.waiting_for_client = True
        for body in (
            "## Client test notes\nNothing for the Client to test: importer safeguards.",
            "## Client test notes\nEngineering should run the importer checks.",
            "## Client test notes\n<!-- Client change: draft -->\nTry the preview.",
            "## Client test notes\n```\nClient change: example\n```\nTry the preview.",
            "## Client test notes\nClient change:\nTry the preview.",
            "Client change: outside the notes.\n\n## Client test notes\nTry the preview.",
        ):
            with self.subTest(body=body):
                self.reader.pr["body"] = body
                self.assertIn(ready_handoff.CLIENT_CHANGE, self.refuse())
        self.reader.pr["body"] = "## Client test notes\nClient change: the parts list shows supplier costs.\n\nOpen a part."
        self.post()

    def test_client_steps_without_named_change_refuse_even_after_acceptance(self):
        self.reader.checks.append(dict(OWNER_REVIEW_ACCEPTED))
        self.reader.pr["body"] = "## Client test notes\nOpen a part and check its supplier costs."
        self.assertIn(ready_handoff.CLIENT_CHANGE, self.refuse())
        self.reader.pr["body"] = "## Client test notes\n- Client change: parts show supplier costs.\n- Open a part."
        self.post()

    def test_each_notes_unit_keeps_one_matching_statement(self):
        self.client_release.return_value = self.client_repository
        for notes in (
            "Nothing for the Client to test: tooling.\n\nClient change: new button.",
            "Client change: new button.\nCheck it.\nNothing for the Client to test: tools.",
            "### #10\nClient change: supplier column.\nCheck it.\n### #11\nApprove the new checkout screen.",
            "### #10\nClient change: supplier column.\nCheck it.\n### #11\nNothing for the Client to test: tools.\nClient change: new button.",
        ):
            for waiting in (False, True):
                with self.subTest(notes=notes, waiting=waiting):
                    self.reader.pr["body"] = f"## Client test notes\n{notes}"
                    self.args.waiting_for_client = waiting
                    self.refuse()

    def test_engineering_reasons_any_shape_and_mixed_batches_stay_supported(self):
        for reason in ("importer safeguards\nthat only engineering runs.", "internal maintenance:\n- CI caches dependencies.\n\nNo screen changes."):
            with self.subTest(reason=reason):
                self.reader.pr["body"] = f"## Client test notes\nNothing for the Client to test: {reason}"
                self.post()
                self.writer.reset_mock()
        self.args.waiting_for_client = True
        for notes in (
            "### #10\nClient change: supplier column.\nCheck it.\n### #11\nNothing for the Client to test: tools.",
            "Client change: help shows batch examples.\nOpen Help.\n```markdown\n### #10\nExample\n```",
            f"Client change: help shows examples.\nRead them:\n> {ready_handoff.NOTHING_TO_TEST}: tools.\n> {ready_handoff.ENGINEERING_RELEASE_TESTS}",
            f"Client change: help shows examples.\nRead them:\n\n    {ready_handoff.NOTHING_TO_TEST}: tools.",
        ):
            with self.subTest(notes=notes):
                self.reader.pr["body"] = f"## Client test notes\n{notes}"
                self.post()
                self.writer.reset_mock()

    def test_engineering_marker_needs_plain_first_line_and_release_steps(self):
        self.client_release.return_value = self.client_repository
        marker = ready_handoff.ENGINEERING_RELEASE_TESTS
        for notes in (marker, marker + "\nTODO", marker + "\n<!-- add steps -->", "- " + marker + "\nCheck parts.", "```\n" + marker + "\n```\nCheck parts.", "Check parts.\n" + marker):
            with self.subTest(notes=notes):
                self.reader.pr["body"] = f"## Client test notes\n{notes}"
                self.refuse()

    def test_completed_neutral_advisory_allows_docs_handoff(self):
        self.client_release.return_value = self.client_repository
        self.reader.pr["body"] = "## Client test notes\nNothing for the Client to test: documentation only."
        self.reader.checks.append({"id": 2, "name": "launchplane/owner-review", "status": "completed", "conclusion": "neutral"})
        _, comment = self.post()
        self.assertTrue(latest_ready(self.reader.pr, [comment], {"engineer"}))
        for change in (
            {"labels": [{"name": "owner-review"}]},
            {"body": "## Client test notes\nTry the preview."},
        ):
            with self.subTest(change=change):
                self.writer.reset_mock()
                original = copy.deepcopy(self.reader.pr)
                self.reader.pr.update(change)
                self.refuse()
                self.reader.pr = original

    def test_pending_or_rejected_review_still_blocks_no_test_notes(self):
        self.reader.pr["body"] = "## Client test notes\nNothing for the Client to test: documentation only."
        review = {"id": 2, "name": "launchplane/owner-review"}
        self.reader.checks.append(review)
        for status, conclusion in (("in_progress", None), ("completed", "failure"), ("completed", "skipped")):
            with self.subTest(status=status, conclusion=conclusion):
                review.update(status=status, conclusion=conclusion)
                self.refuse()

    def test_ready_format_variants_keep_exact_head_requirement(self):
        for first in (READY, f"{READY}.", f"**{READY}**", f"**{READY}.**"):
            for head in (SHA, f"`{SHA}`", f"**{SHA}**"):
                with self.subTest(first=first, head=head):
                    ready = {"body": f"{first}\n\nHead: {head}", "user": {"login": "engineer"}, "id": 1, "created_at": "2026-01-01T00:00:00Z"}
                    self.assertTrue(latest_ready(self.reader.pr, [ready], {"engineer"}))
                    self.assertFalse(latest_ready(self.reader.pr, [ready], {"someone-else"}))
                    pushed = {**self.reader.pr, "head": {"sha": "b" * 40}}
                    self.assertFalse(latest_ready(pushed, [ready], {"engineer"}))
                    waiting = {**ready, "id": 2, "body": handoff_body(SHA, waiting_for_client=True)}
                    self.assertFalse(latest_ready(self.reader.pr, [ready, waiting], {"engineer"}))

    def test_bold_renewed_wait_invalidates_formatted_ready(self):
        ready = {"body": f"**{READY}**\n\nHead: **{SHA}**", "user": {"login": "engineer"}, "id": 1, "created_at": "2026-01-01T00:00:00Z"}
        for first in ("**Engineering ready; waiting for a defect fix**", "**Engineering ready; waiting for the Client's acceptance.**"):
            with self.subTest(first=first):
                waiting = {**ready, "id": 2, "body": f"{first}\n\nHead: {SHA}"}
                self.assertFalse(latest_ready(self.reader.pr, [ready, waiting], {"engineer"}))
                waiting["updated_at"] = "2026-01-01T00:05:00Z"
                newer_ready = {**ready, "id": 3, "created_at": "2026-01-01T00:01:00Z"}
                self.assertFalse(latest_ready(self.reader.pr, [waiting, newer_ready], {"engineer"}))

    def test_ready_requires_one_full_head_even_with_formatting(self):
        for head_line in ("", f"Head: {SHA[:8]}", f"Head: {SHA}a", f"Head: **{SHA}**a", f"Head: **{SHA}`", f"Head: {SHA}\nHead: **{SHA}**"):
            with self.subTest(head_line=head_line):
                ready = {"body": f"**{READY}**\n\n{head_line}", "user": {"login": "engineer"}, "id": 1, "created_at": "2026-01-01T00:00:00Z"}
                self.assertFalse(latest_ready(self.reader.pr, [ready], {"engineer"}))

    def test_nothing_to_test_requires_colon_reason_and_preserves_acceptance_gates(self):
        # Accepted, so only the review label's demand for real notes can refuse.
        self.reader.checks.append(dict(OWNER_REVIEW_ACCEPTED))
        for heading in ("Client", "Owner"):
            with self.subTest(heading=heading):
                self.reader.pr["body"] = f"## {heading} test notes\nNothing for the Client to test: Automated tooling."
                self.reader.pr["labels"] = []
                self.post()
                self.writer.reset_mock()
                self.reader.pr["labels"] = [{"name": "owner-review"}]
                self.refuse()
        self.reader.pr["labels"] = []
        for invalid in (
            "Nothing for the Client to test",
            "Nothing for the Client to test:",
            "Nothing for the Client to test:   ",
            "Nothing for the Client to test. Documentation-only change.",
            "Nothing for the Client to test; automated tooling.",
            "Nothing for the Client to test, automated tooling.",
            "Nothing for the Client to test - placeholder",
            "Nothing for the Client to tests: extra s.",
            "Nothing for the Client to test in this source-only repair; duplicate tools removed.",
            "nothing for the client to test: wrong case.",
            # Naming the Client instead of the role read as real steps (odoo-tenant-cm#214).
            "Nothing for Justin to test. No screen, menu or data changes.",
            "Nothing for Justin to test: no screen changes.",
            "Nothing to test: documentation only.",
            "Nothing new to test: documentation only.",
            "- Nothing else for Justin to test.",
        ):
            with self.subTest(invalid=invalid):
                self.reader.pr["body"] = f"## Client test notes\n{invalid}"
                self.assertIn(ready_handoff.NOTHING_TO_TEST, self.refuse())

    def test_steps_that_start_with_nothing_still_need_the_client(self):
        for steps in (
            "Nothing changed in checkout; open the cart to test the new button.",
            "Nothing happens when you try to test checkout. Verify that Pay now opens the form.",
        ):
            with self.subTest(steps=steps):
                intent = ready_handoff.client_test_notes(steps)
                self.assertTrue(intent.review_required)
                self.assertFalse(intent.error)

    def test_legacy_nothing_to_test_marker(self):
        self.client_release.return_value = self.client_repository
        for separator in (":", "."):
            with self.subTest(separator=separator):
                self.reader.pr["body"] = f"## Owner test notes\nNothing for the owner to test{separator} Legacy tooling."
                self.post()
                self.writer.reset_mock()
        for invalid in ("Nothing for the owner to test", "Nothing for the owner to test:"):
            with self.subTest(invalid=invalid):
                self.reader.pr["body"] = f"## Owner test notes\n{invalid}"
                self.assertIn(ready_handoff.NOTHING_TO_TEST, self.refuse())

    def test_inline_client_test_notes_and_missing_client_release_notes_refuse(self):
        self.client_release.return_value = self.client_repository
        for body in (
            "Summary\n\nClient test notes: Nothing for the Client to test; this only prepares local gitignore rules.",
            "Summary\n\nClient testing: none required for this repo-hygiene change.",
            "Summary\n\nClient test notes: Nothing for the Client to test in this source-only repair.",
            "Summary\n\n**Client test notes:** Nothing for the Client to test: automated tooling.",
            "Summary\n\n- Client test notes: Try the new command.",
            "## Client test notes\n\nNone",
            "## Client test notes\n\nN/A.",
            "## Client test notes\n\nTODO - add steps",
            "## Client test notes\n\nTBD - later",
        ):
            with self.subTest(body=body):
                self.reader.pr["body"] = body
                self.assertEqual(self.refuse(), ready_handoff.MISSING_CLIENT_TEST_NOTES_MESSAGE)

        self.reader.pr["body"] = "Summary without any test notes section."
        self.client_release.return_value = self.client_repository
        self.assertEqual(self.refuse(), ready_handoff.MISSING_CLIENT_TEST_NOTES_MESSAGE)
        self.reader.pr["user"] = {"login": "dependabot[bot]", "type": "Bot"}
        self.post()

    def test_batch_sections_and_nested_headings_match_train_rules(self):
        self.reader.pr["body"] = (
            "## Client test notes\n"
            "Intro for batch.\n"
            "### #10\nTry feature A.\n"
            "### #11\nNothing for the Client to test: internal refactor.\n"
        )
        self.reader.pr["labels"] = [{"name": "owner-review"}]
        self.reader.checks.append(dict(OWNER_REVIEW_ACCEPTED))
        self.refuse()
        self.reader.pr["labels"] = []
        self.reader.pr["body"] = (
            "## Client test notes\n\n"
            "## Owner test notes\n"
            "### Owner test notes\n"
            "Nothing for the Client to test: nested heading."
        )
        self.post()

    def test_decorated_no_test_marker_and_fenced_heading_refuse(self):
        self.client_release.return_value = self.client_repository
        for note in (
            "- Nothing for the Client to test: bullet list.",
            "**Nothing for the Client to test:** bold prefix.",
            "> Nothing for the Client to test: blockquote.",
        ):
            with self.subTest(note=note):
                self.reader.pr["body"] = f"## Client test notes\n{note}"
                self.assertIn(ready_handoff.NOTHING_TO_TEST, self.refuse())
        self.reader.pr["body"] = (
            "```markdown\n## Client test notes\n"
            "Nothing for the Client to test: inside code fence.\n```\n"
        )
        self.client_release.return_value = self.client_repository
        self.assertEqual(self.refuse(), ready_handoff.MISSING_CLIENT_TEST_NOTES_MESSAGE)

    def test_no_test_marker_outside_or_after_real_notes_never_exempts(self):
        self.client_release.return_value = self.client_repository
        for body in (
            "Nothing for the owner to test: outside section\n\n## Client test notes\nTry the preview.",
            "## Client test notes\nTry the preview.\nNothing for the Client to test: second line.",
            "## Client test notes\nTry the preview.\n\n## History\nNothing for the owner to test: history",
            "## Client test notes\n\n## History\nNothing for the Client to test: history.",
            "## Client test notes\nNothing for the owner to test: first section\n\n## Owner test notes\nTry the preview.",
        ):
            with self.subTest(body=body):
                self.reader.pr["body"] = body
                self.refuse()

    def test_client_rejection_still_refuses_waiting_handoff(self):
        self.args.waiting_for_client = True
        self.reader.checks.append({"id": 2, "name": "launchplane/owner-review", "status": "completed", "conclusion": "failure"})
        self.refuse()

    def link_open_issue(self, number=7):
        self.reader.pr["body"] = f"Refs #{number}"
        self.reader.issues[number] = {"number": number, "state": "open", "body": "", "user": {"login": "engineer"}}

    def test_open_referenced_issue_without_follow_up_refuses_naming_it(self):
        self.link_open_issue()
        message = self.refuse()
        self.assertIn(f"{REPO}#7", message)
        self.assertIn(f"Closes {REPO}#7", message)

    def test_closing_the_referenced_issue_allows_ready(self):
        self.link_open_issue()
        self.reader.closing = [7]
        self.post()

    def test_open_sub_issue_owns_the_remaining_steps(self):
        self.link_open_issue()
        self.reader.sub_issues[7] = [{"number": 8, "state": "closed"}]
        self.refuse()
        self.reader.sub_issues[7].append({"number": 9, "state": "open"})
        self.post()

    def test_recorded_event_wait_owns_the_remaining_step(self):
        self.link_open_issue()
        record = helper.github_plan_wait.reviewed(evidence="Release pending", waiting_for="Client", ends_when="Client accepts the release")
        self.reader.issues[7]["user"] = {"login": "example"}
        self.reader.issues[7]["body"] = helper.github_plan_wait.marker(record)
        self.post()

    def test_director_decision_wait_needs_its_question_before_ready(self):
        self.link_open_issue()
        self.reader.issues[7]["user"] = {"login": "example"}
        question = {"id": 55, "user": {"login": "example"}, "body": "Director question: Q812. Approve the live install?"}
        self.reader.issue_comments[55] = question
        for evidence, refused in (("Recorded in the session", True),
                                  (f"https://github.com/{REPO}/issues/7#issuecomment-55", False)):
            with self.subTest(evidence=evidence):
                record = helper.github_plan_wait.reviewed(evidence=evidence, waiting_for="Director approves the live install",
                                                          ends_when="Director replies")
                self.reader.issues[7]["body"] = helper.github_plan_wait.marker(record)
                with patch.object(helper.director_question_gate, "configured_queue", return_value=None):
                    if refused:
                        self.assertIn("Director question:", self.refuse())
                    else:
                        self.post()

    def test_cancelled_question_read_stops_ready_instead_of_refusing(self):
        self.link_open_issue()
        self.reader.issues[7]["user"] = {"login": "example"}
        record = helper.github_plan_wait.reviewed(evidence=f"https://github.com/{REPO}/issues/7#issuecomment-55",
                                                  waiting_for="Director approves the live install", ends_when="Director replies")
        self.reader.issues[7]["body"] = helper.github_plan_wait.marker(record)
        failure = helper.github_api_core.FailureDetail(cause="cancelled", message="cancelled", retryable=False,
                                                       fallback_eligible=False, disposition="stop")
        cancelled = GitHubReadError("cancelled", result=ApiResult(ok=False, status=0, body=None, failure=failure), diagnostics={})
        read = self.reader.get_json

        def cancel_comment_reads(path, **kwargs):
            if "/issues/comments/" in path:
                raise cancelled
            return read(path, **kwargs)

        with patch.object(self.reader, "get_json", side_effect=cancel_comment_reads), \
                patch.object(helper.director_question_gate, "configured_queue", return_value=None):
            message = self.refuse()
        self.assertIn("unavailable", message)
        self.assertNotIn("Director question:", message)

    def test_closed_issues_pull_requests_and_other_owners_need_no_follow_up(self):
        self.reader.pr["body"] = "Refs #7, #8 and other/tool#9"
        self.reader.issues[7] = {"number": 7, "state": "closed", "body": ""}
        self.reader.issues[8] = {"number": 8, "state": "open", "body": "", "pull_request": {}}
        self.post()

    def test_qualified_reference_in_the_title_is_a_linked_issue(self):
        self.link_open_issue()
        self.reader.pr["body"] = ""
        self.reader.pr["title"] = f"{REPO}#7: first slice"
        self.refuse()

    def test_unreadable_linked_issue_refuses(self):
        self.reader.pr["body"] = "Refs #7"
        self.assertIn("unavailable", self.refuse())

    def test_cli_selects_ready_and_waiting_variant(self):
        with patch("sys.argv", ["gh-pr.py", "--repo", REPO, "ready", "1", "--waiting-for-client"]):
            args = helper.parse_args()
        self.assertIs(args.func, helper.cmd_ready)
        self.assertTrue(args.waiting_for_client)


class SupervisorReceiptTests(unittest.TestCase):
    def test_receipt_parses_through_a_package_style_import(self):
        # Supervisor helpers import this module as skills.github.scripts.ready_handoff
        # without the scripts directory on their path.
        root = Path(__file__).resolve().parents[3]
        body = f"{ready_handoff.SUPERVISOR_REPORT_HEADING}\n\n<!-- supervisor-review {json.dumps({'request': 7, 'head': SHA})} -->"
        code = ("import json, sys\n"
                "sys.path.insert(0, sys.argv[2])\n"
                "from skills.github.scripts import ready_handoff\n"
                "print(json.dumps(ready_handoff.raw_receipt(sys.argv[1])))")
        result = subprocess.run([sys.executable, "-I", "-c", code, body, str(root)],
                                cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"request": 7, "head": SHA})


if __name__ == "__main__":
    unittest.main()
