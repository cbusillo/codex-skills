#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Behavioral coverage for keyword-closure planning reconciliation."""
from __future__ import annotations

import copy
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import github_plan_landing as landing

SPEC = importlib.util.spec_from_file_location("landing_plan_under_test", Path(__file__).with_name("gh-plan.py"))
assert SPEC is not None and SPEC.loader is not None
PLAN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PLAN
SPEC.loader.exec_module(PLAN)
REPO = "fixture/catalog"


def graph(*numbers: int, merged: bool = True, after: str | None = None) -> dict:
    return {"data": {"repository": {"defaultBranchRef": {"name": "main"}, "pullRequest": {
        "merged": merged, "baseRefName": "main", "url": f"https://github.com/{REPO}/pull/9",
        "mergeCommit": {"oid": "a" * 40}, "closingIssuesReferences": {
            "nodes": [{"number": number, "repository": {"nameWithOwner": REPO}} for number in numbers],
            "pageInfo": {"hasNextPage": after is not None, "endCursor": after}}}}}}


class LandingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = copy.deepcopy(PLAN.DEFAULT_CONFIG)
        self.issue = {"repo": REPO, "number": 7, "state": "closed", "state_reason": "completed",
                      "labels": [{"name": self.config["labels"][key]} for key in
                                 ("plan", "active", "waiting", "blocked", "stale")],
                      "body": PLAN.PLAN_MANAGED_PROVENANCE_MARKER + "\n\n## Current Status\n\nSupervisor routes the PR.\n",
                      "user": {"login": "fixture-bot"}, "html_url": f"https://github.com/{REPO}/issues/7"}
        self.reader = Mock()
        self.reader.graphql_json.return_value = SimpleNamespace(ok=True, body=graph(7))
        self.state = Mock(side_effect=AssertionError("Must not close or reopen an issue"))
        self.labels = Mock(side_effect=self.edit_labels)
        self.body = Mock(side_effect=self.edit_body)
        self.comments = Mock(return_value={"actor": "fixture-bot"})
        patches = [patch.multiple(PLAN, load_config=Mock(return_value=self.config),
                                  get_issue=Mock(side_effect=lambda *_: ("fixture-bot", copy.deepcopy(self.issue))),
                                  close_relationship_preflight=Mock(return_value=("fixture-bot", {"result": "passed"})),
                                  comment_route=Mock(return_value=("fixture-bot", "fake-gh", "fixture-bot")),
                                  rest_edit_issue=self.body),
                   patch.object(PLAN.github_identity, "configured_bot_logins", return_value=("fixture-bot",)),
                   patch.multiple(PLAN.github_issue_core, set_issue_state=self.state, edit_issue=self.labels),
                   patch.object(PLAN.github_comment_core, "comment", self.comments),
                   patch.object(landing.subprocess, "run", return_value=SimpleNamespace(returncode=0))]
        for context in patches:
            context.start()
            self.addCleanup(context.stop)

    def edit_labels(self, _number: int, **kwargs) -> dict:
        names = set(PLAN.normalize_labels(self.issue["labels"]))
        names.difference_update(kwargs["remove_labels"] or [])
        names.update(kwargs["add_labels"] or [])
        self.issue["labels"] = [{"name": name} for name in names]
        return {"actor": "fixture-bot"}

    def edit_body(self, _repo: str, _number: int, **kwargs) -> tuple[str, dict]:
        self.issue["body"] = kwargs["body"]
        return "fixture-bot", copy.deepcopy(self.issue)

    def run_landing(self, apply: bool = True) -> dict:
        return landing.reconcile(PLAN, self.reader, REPO, 9, apply=apply)

    def test_keyword_closure_uses_real_close_cleanup_and_readback(self) -> None:
        self.issue["labels"].append({"name": "bug"})
        result = self.run_landing()
        self.assertTrue(result["ok"])
        self.assertEqual(result["issues"][0]["status"], "reconciled")
        self.assertEqual(set(PLAN.normalize_labels(self.issue["labels"])),
                         {self.config["labels"]["done"], self.config["labels"]["plan"], "bug"})
        self.assertIn(result["landing_sha"], PLAN.issue_plan_sections(self.issue)["Current Status"])
        self.state.assert_not_called()

    def test_partial_pr_or_unrelated_merged_pr_does_not_complete_parent(self) -> None:
        self.reader.graphql_json.return_value.body = graph()
        self.issue["state"] = "open"
        self.assertTrue(self.run_landing()["ok"])
        self.labels.assert_not_called()
        self.body.assert_not_called()
        self.state.assert_not_called()

    def test_open_finish_line_and_reporter_acceptance_are_review_work(self) -> None:
        for field, value, expected in (("state", "open", "review_open_finish_line"),
                                       ("user", {"login": "outside-reporter"}, "review_reporter_acceptance")):
            original = self.issue[field]
            self.issue[field] = value
            result = self.run_landing()
            self.assertFalse(result["ok"])
            self.assertEqual(result["issues"][0]["status"], expected)
            self.issue[field] = original
        self.labels.assert_not_called()
        self.body.assert_not_called()

    def test_preview_and_comment_attention_do_not_mutate(self) -> None:
        self.assertTrue(self.run_landing(apply=False)["ok"])
        with patch.object(landing.subprocess, "run", side_effect=subprocess.CalledProcessError(3, "radar")):
            self.assertFalse(self.run_landing()["ok"])
        self.labels.assert_not_called()
        self.body.assert_not_called()

    def test_failed_or_truncated_graph_read_has_no_writes(self) -> None:
        cases = [SimpleNamespace(ok=False, body=None), SimpleNamespace(ok=True, body={"errors": [{}]}),
                 SimpleNamespace(ok=True, body=graph(merged=False)),
                 SimpleNamespace(ok=True, body=graph(7, after="same"))]
        for response in cases:
            with self.subTest(response=response):
                self.reader.graphql_json.return_value = response
                with self.assertRaises(ValueError):
                    self.run_landing()
        self.labels.assert_not_called()
        self.body.assert_not_called()

    def test_readback_mismatch_is_not_success(self) -> None:
        self.labels.side_effect = lambda *_args, **_kwargs: {"actor": "fixture-bot"}
        with self.assertRaises(ValueError):
            self.run_landing()

    def test_pagination_reads_all_references_before_apply(self) -> None:
        self.reader.graphql_json.side_effect = [SimpleNamespace(ok=True, body=graph(7, after="page2")),
                                                SimpleNamespace(ok=True, body=graph(8))]
        _, refs = landing.closing_references(self.reader, REPO, 9)
        self.assertEqual(refs, [(REPO, 7), (REPO, 8)])
        self.assertEqual(self.reader.graphql_json.call_args.args[1]["after"], "page2")

    def test_only_closed_guard_prevents_close_after_concurrent_reopen(self) -> None:
        self.issue["state"] = "open"
        args = SimpleNamespace(repo=REPO, issue="7", planning_checkout=None, reason="completed", only_closed=True)
        with self.assertRaises(PLAN.PlanError):
            PLAN.cmd_close(args)
        self.labels.assert_not_called()
        self.state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
