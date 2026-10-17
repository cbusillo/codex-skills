#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Ready comments round-trip through the watcher and never hide red CI."""

import argparse
import base64
import copy
import importlib.util
import json
import unittest
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
PR = {"number": 1, "state": "open", "draft": False, "labels": [], "head": {"sha": SHA}, "base": {"ref": "main"}, "user": {"login": "engineer"}}


class Reader:
    def __init__(self):
        self.pr = copy.deepcopy(PR)
        self.checks: list[dict[str, Any]] = [{"id": 1, "name": "unit", "status": "completed", "conclusion": "success"}]
        self.statuses = []
        self.runs = []
        self.comments: list[dict[str, Any]] = []
        self.config = None
        self.files = [{"filename": "src/tool.py"}]
        self.committed_at = "2026-01-01T00:05:00Z"
        self.current_pr = None
        self.pr_reads = 0
        self.unavailable = False
        self.last_result = None
        self.failed_results = []
        self.completed_steps = []
        self.actor = "engineer"
        self.expected_actor = "engineer"

    def get_json(self, path, **_kwargs):
        if path.endswith("/pulls/1"):
            self.pr_reads += 1
            return copy.deepcopy(self.current_pr if self.pr_reads > 1 and self.current_pr else self.pr)
        if path.endswith("/status"):
            return {"state": "success"}
        if "/contents/.github/github.json?ref=main" in path:
            if self.config is None:
                raise GitHubReadError("Not Found", result=ApiResult(ok=False, status=404, body=None), diagnostics={})
            return {"content": base64.b64encode(json.dumps(self.config).encode()).decode()}
        if path.endswith(f"/commits/{SHA}"):
            return {"commit": {"committer": {"date": self.committed_at}}}
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
        if path.endswith("/issues/1/comments"):
            return self.comments
        if path.endswith("/pulls/1/files"):
            return self.files
        raise AssertionError(path)

    @staticmethod
    def diagnostics():
        return {"degraded": False, "degradedComponents": []}

    @staticmethod
    def retry_summary():
        return None


class ReviewRecordTests(unittest.TestCase):
    def test_existing_session_and_supervisor_leads_classify(self):
        # First lines as sessions and the Supervisor wrote them on codex-skills PRs, Oct 2026.
        outcomes = (
            "**Review of record: OpenAI, run by the Supervisor** from its iTerm session",
            "**Review of record (OpenAI): no findings.** The Supervisor ran it",
            "Anthropic review of record: Claude Opus 5.5, no findings.",
            "EXEC-CS review of record: Anthropic `claude-opus-5-5`, read-only",
            "Anthropic **claude-opus-5-5** completed the review of record (CLI-confirmed model).",
            "Review weighed at `83f8b258`. I reproduced the finding and acted on it.",
            "Review by another model (author: Anthropic Claude Opus): OpenAI via Codex, read-only",
            "Anthropic review of record: Claude Opus 5.5, completed twice. Fixed: errors cannot hide output.",
            "Review weighed: OpenAI completed the review; no changes needed.",
            "Review weighed: OpenAI completed the review and found the failed-read bug, now fixed.",
            "Review weighed: OpenAI completed the review and found that the helper did not run required checks.",
        )
        requests = (
            "Review prompt for the review of record (provider: openai; author: anthropic).",
            "## Review request (OpenAI review of record)",
            "**Review of record needed: OpenAI.** This worker runs under the Supervisor's tmux server.",
            "Review of record requested: provider `openai`, author provider `anthropic` (Q267).",
            "Review of record still needed. The OpenAI review could not launch.",
            "Context first.\n\nThe helper fails here, so **Supervisor runs the review**:",
        )
        # Status updates and unrelated requests are neither outcomes nor model-review requests.
        neither = (
            "Review by another model is blocked by #2069; the Supervisor still needs to run it.",
            "Neither provider has completed the review of record yet.",
            "Review of record: OpenAI could not start; retrying from the Supervisor's session.",
            "Review of record: OpenAI did not complete the review.",
            "Review of record: OpenAI review is not done.",
            "Review request: please check the Client test notes.",
            "Review request for the Google login button UI.",
            "Review request: please check the provider settings UI.",
            "Review of record: pending.",
            "Review of record: OpenAI is still running.",
            "Review of record: OpenAI; could not complete the review.",
        )
        for body in neither:
            with self.subTest(body=body):
                self.assertIsNone(ready_handoff.review_record(body))
        for body in outcomes:
            with self.subTest(body=body):
                self.assertEqual(ready_handoff.review_record(body), "outcome")
        for body in requests:
            with self.subTest(body=body):
                self.assertEqual(ready_handoff.review_record(body), "request")
        self.assertIsNone(ready_handoff.review_record(f"{READY}\n\nHead: {SHA}"))


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
        standing_patch = patch.object(ready_handoff, "director_standing_acceptance", return_value=False)
        self.standing = standing_patch.start()
        self.addCleanup(standing_patch.stop)
        client_release_patch = patch.object(ready_handoff, "client_release_repository", return_value=False)
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

    def review_comment(self, body, minute, login="engineer"):
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
                    self.review_comment(status, 7)
                    self.refuse()

    def test_completed_review_with_code_defect_allows_ready(self):
        self.review_comment("Review prompt for the review of record.", 6)
        self.review_comment(
            "Review weighed: OpenAI completed the review and found that the helper did not run required checks.", 7,
        )
        self.post()

    def test_product_review_request_does_not_hold_ready(self):
        self.review_comment("Review request for the Google login button UI.", 6)
        self.post()

    def test_untrusted_review_request_does_not_hold_ready(self):
        self.review_comment("Supervisor runs the review.", 1, login="drive-by")
        self.post()

    def test_review_required_paths_need_an_outcome_after_the_head_commit(self):
        self.reader.config = {"qualityGate": {"modelReview": {"requiredPaths": ["skills/*/SKILL.md"]}}}
        self.post()
        self.writer.reset_mock()
        self.reader.files = [{"filename": "skills/github/SKILL.md"}]
        self.refuse()
        self.review_comment("Review weighed: none to act on.", 4)
        self.refuse()
        self.review_comment("Review weighed at the current head: nothing to act on.", 6)
        self.post()

    def test_renamed_review_required_path_still_needs_an_outcome(self):
        self.reader.config = {"qualityGate": {"modelReview": {"requiredPaths": ["AGENTS.md"]}}}
        self.reader.files = [{"filename": "docs/AGENTS-old.md", "previous_filename": "AGENTS.md"}]
        self.refuse()

    def test_unreadable_review_paths_refuse(self):
        self.reader.config = {"qualityGate": {"modelReview": {"requiredPaths": "AGENTS.md"}}}
        self.refuse()

    def test_waiting_for_client_invalidates_ready_without_bypassing_ci(self):
        self.args.waiting_for_client = True
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
        self.refuse()
        self.reader.checks.append({"id": 2, "name": "launchplane/owner-review", "status": "completed", "conclusion": "success"})
        _, comment = self.post()
        self.assertTrue(latest_ready(self.reader.pr, [comment], {"engineer"}))

    def test_real_client_notes_require_acceptance_without_label(self):
        for heading in ("Client", "Owner"):
            with self.subTest(heading=heading):
                self.reader.pr["body"] = f"## {heading} test notes\nTry the new feature."
                message = self.refuse()
                self.assertIn("--waiting-for-client", message)
                self.assertIn("Nothing for the Client to test: <reason>", message)
        self.args.waiting_for_client = True
        _, comment = self.post()
        self.assertFalse(latest_ready(self.reader.pr, [comment], {"engineer"}))

    def test_standing_product_notes_allow_ready_but_explicit_holds_remain(self):
        self.reader.pr["body"] = "## Client test notes\nTry the new feature."
        self.standing.return_value = True
        _, comment = self.post()
        self.standing.assert_called_once_with(REPO)
        self.assertTrue(latest_ready(self.reader.pr, [comment], {"engineer"}))
        self.reader.pr["head"]["sha"] = "b" * 40
        self.assertFalse(latest_ready(self.reader.pr, [comment], {"engineer"}))
        self.writer.reset_mock()
        self.reader.pr["labels"] = [{"name": "owner-review"}]
        self.refuse()
        self.reader.pr["labels"] = []
        self.reader.checks.append({"id": 2, "name": "launchplane/owner-review", "status": "completed", "conclusion": "skipped"})
        self.refuse()

    def test_nothing_to_test_uses_watcher_exemption(self):
        self.reader.pr["body"] = "## Client test notes\nNothing for the Client to test: automated tooling."
        self.post()

    def test_completed_neutral_advisory_allows_docs_handoff(self):
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
        for heading in ("Client", "Owner"):
            with self.subTest(heading=heading):
                self.reader.pr["body"] = f"## {heading} test notes\nNothing for the Client to test: Automated tooling."
                self.reader.pr["labels"] = []
                self.post()
                self.writer.reset_mock()
                self.reader.pr["labels"] = [{"name": "owner-review"}]
                self.assertEqual(
                    self.refuse(),
                    "The Client review label requires real Client test notes.",
                )
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
        ):
            with self.subTest(invalid=invalid):
                self.reader.pr["body"] = f"## Client test notes\n{invalid}"
                self.assertEqual(
                    self.refuse(),
                    "Use Nothing for the Client to test: <reason> with a nonempty reason.",
                )

    def test_legacy_nothing_to_test_marker(self):
        for separator in (":", "."):
            with self.subTest(separator=separator):
                self.reader.pr["body"] = f"## Owner test notes\nNothing for the owner to test{separator} Legacy tooling."
                self.post()
                self.writer.reset_mock()
        for invalid in ("Nothing for the owner to test", "Nothing for the owner to test:"):
            with self.subTest(invalid=invalid):
                self.reader.pr["body"] = f"## Owner test notes\n{invalid}"
                self.assertEqual(
                    self.refuse(),
                    "Use Nothing for the Client to test: <reason> with a nonempty reason.",
                )

    def test_inline_client_test_notes_and_missing_client_release_notes_refuse(self):
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
                message = self.refuse()
                self.assertIn("## Client test notes", message)
                self.assertIn("Nothing for the Client to test: <reason>", message)

        self.reader.pr["body"] = "Summary without any test notes section."
        self.client_release.return_value = True
        message = self.refuse()
        self.assertIn("## Client test notes", message)
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
        self.assertEqual(
            self.refuse(),
            "The Client review label requires real Client test notes.",
        )
        self.reader.pr["labels"] = []
        self.reader.pr["body"] = (
            "## Client test notes\n\n"
            "## Owner test notes\n"
            "### Owner test notes\n"
            "Nothing for the Client to test: nested heading."
        )
        self.post()

    def test_decorated_no_test_marker_and_fenced_heading_refuse(self):
        for note in (
            "- Nothing for the Client to test: bullet list.",
            "**Nothing for the Client to test:** bold prefix.",
            "> Nothing for the Client to test: blockquote.",
        ):
            with self.subTest(note=note):
                self.reader.pr["body"] = f"## Client test notes\n{note}"
                self.assertEqual(
                    self.refuse(),
                    "Write Nothing for the Client to test: <reason> as a plain notes line.",
                )
        self.reader.pr["body"] = (
            "```markdown\n## Client test notes\n"
            "Nothing for the Client to test: inside code fence.\n```\n"
        )
        self.client_release.return_value = True
        self.assertIn("## Client test notes", self.refuse())

    def test_no_test_marker_outside_or_after_real_notes_never_exempts(self):
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

    def test_cli_selects_ready_and_waiting_variant(self):
        with patch("sys.argv", ["gh-pr.py", "--repo", REPO, "ready", "1", "--waiting-for-client"]):
            args = helper.parse_args()
        self.assertIs(args.func, helper.cmd_ready)
        self.assertTrue(args.waiting_for_client)


if __name__ == "__main__":
    unittest.main()
