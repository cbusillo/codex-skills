#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Ready comments round-trip through the watcher and never hide red CI."""

import argparse
import copy
import importlib.util
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from github_read import GitHubReadShapeError
from ready_handoff import READY, handoff_body, latest_ready

spec = importlib.util.spec_from_file_location("ready_pr", Path(__file__).with_name("gh-pr.py"))
assert spec and spec.loader
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

SHA = "a" * 40
REPO = "example/tool"


class Reader:
    def __init__(self):
        self.pr = {"number": 1, "state": "open", "draft": False, "labels": [], "head": {"sha": SHA}}
        self.checks: list[dict[str, Any]] = [{"id": 1, "name": "unit", "status": "completed", "conclusion": "success"}]
        self.statuses = []
        self.runs = []
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
        raise AssertionError(path)

    @staticmethod
    def diagnostics():
        return {"degraded": False, "degradedComponents": []}

    @staticmethod
    def retry_summary():
        return None


class ReadyTests(unittest.TestCase):
    def setUp(self):
        self.reader = Reader()
        self.args = argparse.Namespace(repo=REPO, pr="1", waiting_for_client=False)
        self.reader_patch = patch.object(helper.github_read_core, "GitHubReader", return_value=self.reader)
        self.writer_patch = patch.object(helper.github_comment_core, "comment", return_value={"ok": True, "url": "fixture"})
        self.reader_factory = self.reader_patch.start()
        self.writer = self.writer_patch.start()
        self.addCleanup(self.reader_patch.stop)
        self.addCleanup(self.writer_patch.stop)
        helper.CURRENT_OPERATION = "github.pr.ready"

    def post(self):
        result = helper.cmd_ready(self.args)
        body = self.writer.call_args.args[2]
        self.assertEqual(self.writer.call_args.kwargs["operation"], "github.pr.ready")
        self.assertEqual(self.writer.call_args.kwargs["repo"], REPO)
        return result, {"body": body, "user": {"login": "engineer"}, "id": 1, "created_at": "2026-01-01T00:00:00Z"}

    def refuse(self):
        with self.assertRaises(helper.PrHelperError):
            helper.cmd_ready(self.args)
        self.writer.assert_not_called()

    def test_posted_body_round_trips_and_push_invalidates(self):
        result, comment = self.post()
        self.assertEqual(result["headSha"], SHA)
        self.assertEqual(comment["body"], f"{READY}\n\nHead: {SHA}")
        self.assertTrue(latest_ready(self.reader.pr, [comment], {"engineer"}))
        self.assertIs(self.reader_factory.call_args.kwargs["cache_enabled"], False)
        self.reader.pr["head"]["sha"] = "b" * 40
        self.assertFalse(latest_ready(self.reader.pr, [comment], {"engineer"}))

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
                self.reader.pr = {"number": 1, "state": "open", "draft": False, "labels": [], "head": {"sha": SHA}, **change}
                self.refuse()

    def test_push_or_hold_during_reads_refuses(self):
        for change in ({"head": {"sha": "b" * 40}}, {"labels": [{"name": "merge-blocked"}]}, {"labels": [{"name": "owner-review"}]}):
            with self.subTest(change=change):
                self.reader.pr_reads = 0
                self.reader.current_pr = {**self.reader.pr, **change}
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
                self.refuse()
        self.args.waiting_for_client = True
        _, comment = self.post()
        self.assertFalse(latest_ready(self.reader.pr, [comment], {"engineer"}))

    def test_nothing_to_test_uses_watcher_exemption(self):
        self.reader.pr["body"] = "## Client test notes\nNothing for the Client to test: automated tooling."
        self.post()

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
