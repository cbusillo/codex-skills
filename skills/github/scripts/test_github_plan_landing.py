#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Behavioral coverage for keyword-closure planning reconciliation."""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import github_plan_landing as landing
import github_plan_wait

SPEC = importlib.util.spec_from_file_location("landing_plan_under_test", Path(__file__).with_name("gh-plan.py"))
assert SPEC is not None and SPEC.loader is not None
PLAN: Any = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PLAN
SPEC.loader.exec_module(PLAN)
REPO = "fixture/catalog"


@contextlib.contextmanager
def exact_personal_route(route):
    with patch.dict(os.environ, {"GH_PLAN_SKIP_BOT": "1" if route == "skip_bot" else "0",
                                "GH_PLAN_ALLOW_ACTIVE_FIRST": "1" if route == "active_first" else "0"}), \
            patch.object(PLAN.github_identity, "own_user_opted_in", return_value=route == "own_user"), \
            patch.object(PLAN.github_identity, "active_auth_fallback_allowed", return_value=route == "active_fallback"), \
            patch.object(PLAN.github_identity, "acts_as_own_user", side_effect=AssertionError("No provider identity lookup")):
        yield


def graph(*numbers: int, merged: bool = True, after: str | None = None) -> dict:
    return {"data": {"repository": {"defaultBranchRef": {"name": "main"}, "pullRequest": {
        "merged": merged, "baseRefName": "main", "url": f"https://github.com/{REPO}/pull/9",
        "mergeCommit": {"oid": "a" * 40}, "closingIssuesReferences": {
            "nodes": [{"number": number, "repository": {"nameWithOwner": REPO}} for number in numbers],
            "pageInfo": {"hasNextPage": after is not None, "endCursor": after}}}}}}


class LandingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = copy.deepcopy(PLAN.DEFAULT_CONFIG)
        self.issue: dict[str, Any] = {"repo": REPO, "number": 7, "state": "closed", "state_reason": "completed",
                      "labels": [{"name": self.config["labels"][key]} for key in
                                 ("plan", "active", "waiting", "blocked", "stale")],
                      "body": PLAN.PLAN_MANAGED_PROVENANCE_MARKER + "\n" + github_plan_wait.marker(
                          github_plan_wait.reviewed(evidence="Fixture finish-line review; no waits"))
                          + "\n\n## Current Status\n\nSupervisor routes the PR.\n",
                      "user": {"login": "fixture-bot"}, "html_url": f"https://github.com/{REPO}/issues/7"}
        self.reader = Mock()
        self.reader.graphql_json.return_value = SimpleNamespace(ok=True, body=graph(7))
        self.state = Mock(side_effect=AssertionError("Must not close or reopen an issue"))
        self.labels = Mock(side_effect=self.edit_labels)
        self.body = Mock(side_effect=self.edit_body)
        self.comments = Mock(return_value={"actor": "fixture-bot"})
        patches = [patch.multiple(PLAN, load_config=Mock(side_effect=self.load_config),
                                  get_issue=Mock(side_effect=lambda *_: ("fixture-bot", copy.deepcopy(self.issue))),
                                  close_relationship_preflight=Mock(return_value=("fixture-bot", {"result": "passed"})),
                                  comment_route=Mock(return_value=("fixture-bot", "fake-gh", "fixture-bot")),
                                  collect_paged_rest_items=Mock(return_value=("fixture-bot", [])),
                                  read_issue_relationships=Mock(return_value=("fixture-bot", {
                                      "blocked_by": [], "blocking": [], "sub_issues": []})),
                                  read_next_parent=Mock(return_value=None),
                                  rest_edit_issue=self.body),
                   patch.object(PLAN.github_identity, "configured_bot_logins", return_value=("fixture-bot",)),
                   patch.multiple(PLAN.github_issue_core, set_issue_state=self.state, edit_issue=self.labels),
                   patch.object(PLAN.github_comment_core, "comment", self.comments),
                   patch.object(landing.subprocess, "run", return_value=SimpleNamespace(returncode=0))]
        for context in patches:
            context.start()
            self.addCleanup(context.stop)

    def load_config(self, *_args, discovery=None, **_kwargs):
        if discovery is not None:
            discovery.update(status="discovered")
        return self.config

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
        result = self.run_landing()
        self.assertFalse(result["ok"])
        self.assertTrue(result["issues"][0]["writes_started"])
        self.assertIn("close", result["issues"][0])

    def test_partial_label_failure_preserves_close_recovery(self) -> None:
        self.labels.side_effect = PLAN.github_issue_core.IssueError("offline label write failure", failure=
            PLAN.github_api_core.FailureDetail(cause="transport_error", message="offline fixture", retryable=False,
                                               fallback_eligible=False, disposition="stop", write_outcome="rejected"))
        result = self.run_landing()
        self.assertFalse(result["ok"])
        entry = result["issues"][0]
        self.assertTrue(entry["writes_started"])
        self.assertTrue(entry["error"]["recovery"]["issue_closed"])
        self.state.assert_not_called()

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

    def test_cli_preserves_review_required_failure(self) -> None:
        outcome = {"ok": False, "issues": [{"number": 7, "status": "review_reporter_acceptance"}]}
        output = io.StringIO()
        with patch.object(PLAN.github_plan_landing, "reconcile", return_value=outcome), \
                patch.object(PLAN.github_read, "GitHubReader"), \
                patch.object(sys, "argv", [str(Path(__file__).with_name("gh-plan.py")), "--repo", REPO, "reconcile-landing", "--pr", "9"]), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as stopped:
            PLAN.main()
        self.assertNotEqual(stopped.exception.code, 0)
        result = json.loads(output.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"], outcome["issues"])

    def test_waits_missing_records_and_later_foreign_clear_never_complete(self) -> None:
        original = copy.deepcopy(self.issue)
        for record in (None, github_plan_wait.reviewed(evidence="Still requires acceptance",
                                                       waiting_for="Client", ends_when="Client accepts")):
            self.issue.update(copy.deepcopy(original))
            self.issue["body"] = PLAN.PLAN_MANAGED_PROVENANCE_MARKER + (
                "\n" + github_plan_wait.marker(record) if record else "")
            foreign_clear = {"user": {"login": "outside"}, "body": github_plan_wait.marker(
                github_plan_wait.reviewed(evidence="Untrusted claim"))}
            PLAN.collect_paged_rest_items.return_value = ("fixture-bot", [foreign_clear])
            result = self.run_landing()
            self.assertEqual(result["issues"][0]["status"], "review_wait_record")
            self.assertEqual(set(PLAN.normalize_labels(self.issue["labels"])),
                             {self.config["labels"][key] for key in ("plan", "done", "waiting")})
            self.assertEqual(self.issue["state"], "closed")
            self.assertNotIn("section", result["issues"][0])
        self.body.assert_not_called()
        self.comments.assert_not_called()
        self.state.assert_not_called()

    def test_reference_only_open_source_is_reported_without_closing(self) -> None:
        self.issue["state"] = "open"
        response = graph()
        response["data"]["repository"]["pullRequest"]["body"] = "Refs #7; source partial, acceptance remains here."
        self.reader.graphql_json.return_value.body = response
        result = self.run_landing()
        self.assertTrue(result["ok"])
        self.assertTrue(result["review_required"])
        self.assertFalse(result["issues"][0]["closing_reference"])
        self.assertEqual(result["issues"][0]["status"], "review_open_finish_line")
        self.labels.assert_not_called()
        self.state.assert_not_called()

    def test_week_examples_and_dependent_waits(self) -> None:
        fixtures = json.loads(Path(__file__).with_name("test-fixtures").joinpath("landing/stuck-week.json").read_text())
        for name, fixture in fixtures.items():
            with self.subTest(name=name):
                repo = fixture.get("source_repo", fixture["repo"])
                number = fixture["number"]
                self.issue.update(repo=fixture["repo"], number=number, state=fixture["state"], state_reason="completed",
                                  html_url=f"https://github.com/{fixture['repo']}/issues/{number}")
                self.issue["body"] = (PLAN.PLAN_MANAGED_PROVENANCE_MARKER + "\n" + github_plan_wait.marker(
                    github_plan_wait.reviewed(evidence=fixture["evidence"])) + "\n\n## Finish Line\n\n"
                    + fixture["finish_line"] + "\n\n## Current Status\n\n" + fixture["status"])
                self.issue["labels"] = [{"name": self.config["labels"][key]} for key in fixture.get("label_keys", ["plan", "active"])]
                original_body = self.issue["body"]
                response = graph()
                pull = response["data"]["repository"]["pullRequest"]
                pull.update(body=fixture.get("reference", ""), url=f"https://github.com/{repo}/pull/{fixture['pr']}")
                self.reader.graphql_json.return_value.body = response
                events = ([{"event": "cross-referenced", "source": {"issue": copy.deepcopy(self.issue)}}]
                          if fixture.get("cross_reference") else [])
                PLAN.collect_paged_rest_items.side_effect = lambda path, **_: ("fixture-bot", events if path.endswith("timeline") else [])
                entry = {}
                if name == "opw218":
                    PLAN.read_issue_relationships.return_value = ("fixture-bot", {
                        "blocked_by": [{"repo": repo, "number": fixture["source"], "state": "closed"}], "sub_issues": []})
                    landing.reconcile_related(PLAN, fixture["repo"], number, apply=True, entry=entry)
                    self.assertEqual(self.issue["state"], "open")
                    self.assertEqual(self.issue["body"], original_body)
                    self.assertEqual(set(PLAN.normalize_labels(self.issue["labels"])),
                                     {self.config["labels"]["plan"], self.config["labels"]["active"]})
                    again = {}
                    landing.reconcile_related(PLAN, fixture["repo"], number, apply=True, entry=again)
                    self.assertEqual(again["status"], "unchanged")
                else:
                    result = landing.reconcile(PLAN, self.reader, repo, fixture["pr"], apply=True)
                    entry = next(item for item in result["issues"] if item["number"] == number)
                    self.assertFalse(entry["closing_reference"])
                    self.assertEqual(self.issue["state"], fixture["state"])
                    self.assertEqual(self.issue["body"], original_body)
                self.assertEqual(entry["status"], fixture["expected"])
        self.state.assert_not_called()

    def test_related_labels_follow_waits_and_all_current_blockers(self) -> None:
        self.issue["state"] = "open"
        self.issue["labels"] = [{"name": self.config["labels"][key]} for key in ("plan", "blocked")]
        wait = github_plan_wait.reviewed(evidence="Parent retains its test wait", waiting_for="Director",
                                        ends_when="Director reports the test")
        self.issue["body"] = PLAN.PLAN_MANAGED_PROVENANCE_MARKER + "\n" + github_plan_wait.marker(wait)
        body = self.issue["body"]
        for blockers, key in (([], "waiting"), ([{"repo": REPO, "number": 8, "state": "open"}], "blocked")):
            PLAN.read_issue_relationships.return_value = ("fixture-bot", {"blocked_by": blockers, "sub_issues": []})
            entry = {}
            landing.reconcile_related(PLAN, REPO, 7, apply=True, entry=entry)
            self.assertIn(self.config["labels"][key], PLAN.normalize_labels(self.issue["labels"]))
            self.assertIn(self.config["labels"]["waiting"], PLAN.normalize_labels(self.issue["labels"]))
            self.assertEqual(self.issue["body"], body)
            self.assertEqual(self.issue["state"], "open")
        self.state.assert_not_called()

    def test_last_blocker_closure_replaces_stale_blocked_and_waiting_labels(self) -> None:
        self.issue["state"] = "open"
        self.issue["labels"] = [{"name": self.config["labels"][key]} for key in ("plan", "blocked", "waiting")]
        self.issue["body"] += "\nBlocked by: #6\nWaiting for: old test, resolved in structured evidence\n"
        original_body = self.issue["body"]
        PLAN.read_issue_relationships.return_value = ("fixture-bot", {
            "blocked_by": [{"repo": REPO, "number": 6, "state": "closed"}], "sub_issues": []})
        entry = {}
        landing.reconcile_related(PLAN, REPO, 7, apply=True, entry=entry)
        self.assertEqual(entry["derived_status"], "active")
        self.assertEqual(entry["open_blockers"], [])
        self.assertEqual(set(PLAN.normalize_labels(self.issue["labels"])),
                         {self.config["labels"][key] for key in ("plan", "active")})
        self.assertEqual(self.issue["body"], original_body)
        self.state.assert_not_called()

    def test_closed_dependent_gets_done_labels_without_body_or_state_writes(self) -> None:
        self.issue["labels"] = [{"name": self.config["labels"][key]} for key in ("plan", "active", "blocked")]
        before_body = self.issue["body"]
        entry = {}
        landing.reconcile_related(PLAN, REPO, 7, apply=True, entry=entry)
        self.assertEqual(set(PLAN.normalize_labels(self.issue["labels"])),
                         {self.config["labels"][key] for key in ("plan", "done")})
        self.assertEqual(self.issue["state"], "closed")
        self.assertEqual(self.issue["body"], before_body)
        self.body.assert_not_called()
        self.state.assert_not_called()

    def test_related_scope_holds_are_not_activated_by_a_neighbor_landing(self) -> None:
        for hold in ("stale", "done", None):
            with self.subTest(hold=hold):
                self.issue.update(state="open", labels=[{"name": self.config["labels"]["plan"]},
                    *([{ "name": self.config["labels"][hold]}] if hold else [])])
                before = copy.deepcopy(self.issue)
                self.labels.reset_mock()
                entry = {}
                landing.reconcile_related(PLAN, REPO, 7, apply=True, entry=entry)
                self.assertEqual(self.issue, before)
                self.assertTrue(entry["status"].startswith("review_"), entry)
                self.labels.assert_not_called()
        self.body.assert_not_called()
        self.state.assert_not_called()

    def test_closed_related_wait_remains_visible_after_metadata_reconciliation(self) -> None:
        held = github_plan_wait.reviewed(evidence="Closed source retains its actual acceptance wait",
            waiting_for="Client acceptance", ends_when="Client records acceptance")
        self.issue["body"] = PLAN.PLAN_MANAGED_PROVENANCE_MARKER + "\n" + github_plan_wait.marker(held)
        body = self.issue["body"]
        landing.reconcile_related(PLAN, REPO, 7, apply=True, entry={})
        self.assertEqual(set(PLAN.normalize_labels(self.issue["labels"])),
                         {self.config["labels"][key] for key in ("plan", "done", "waiting")})
        self.assertEqual(self.issue["body"], body)
        self.assertEqual(self.issue["state"], "closed")
        self.body.assert_not_called()
        self.state.assert_not_called()

    def test_write_failure_receipt_accounts_for_every_native_target(self) -> None:
        targets = [(REPO, number) for number in (8, 9, 10)]
        attempted = []
        def reconcile_target(_plan, repo, number, *, apply, entry):
            attempted.append((repo, number))
            if number == 8:
                entry["writes_started"] = True
                raise PLAN.PlanError("Target label write denied", failure=PLAN.github_api_core.FailureDetail(
                    cause="permission_denied", message="Fixture target posting authority unavailable",
                    retryable=False, fallback_eligible=False, disposition="stop", write_outcome="rejected"))
            entry["status"] = "unchanged"
        with patch.object(landing, "neighborhood", return_value=targets), \
                patch.object(landing, "reconcile_related", side_effect=reconcile_target):
            receipt = landing.reconcile_dependents(PLAN, REPO, 7, apply=True)
        rows = {(row["repo"], row["number"]): row for row in receipt["issues"]}
        self.assertEqual(set(rows), set(targets))
        skipped = set(targets) - set(attempted)
        for target in skipped:
            self.assertEqual(rows[target]["status"], "not_attempted")
        self.assertEqual(receipt["coverage"], "partial" if skipped else "complete")

    def native_receipt(self, mode, targets, reconcile_target):
        with patch.object(landing, "neighborhood", return_value=targets), \
                patch.object(landing, "reconcile_related", side_effect=reconcile_target), \
                patch.object(landing, "reconcile_issue", side_effect=lambda *_args, entry, **_kwargs: entry.update(status="unchanged")):
            if mode == "close":
                return landing.reconcile_dependents(PLAN, REPO, 7, apply=True)
            return self.run_landing()

    def test_denied_target_preserves_failure_skips_its_repo_and_continues_other_authority(self) -> None:
        targets = [("fixture/restricted", 8), ("FIXTURE/RESTRICTED", 9), ("fixture/authorized", 10)]
        failure = PLAN.github_api_core.FailureDetail(cause="permission_denied",
            message="Fixture target rejects label writes", retryable=False, fallback_eligible=False,
            disposition="stop", write_outcome="rejected")
        for mode in ("close", "landing"):
            with self.subTest(mode=mode):
                attempted = []
                def reconcile_target(_plan, repo, number, *, apply, entry):
                    self.assertNotIn((repo, number), attempted, "A denied target must not be retried")
                    attempted.append((repo, number))
                    if number == 8:
                        entry["writes_started"] = True
                        raise PLAN.PlanError("Target label write denied", failure=failure,
                                             payload={"trace_id": "fixture-denial-trace"})
                    entry["status"] = "unchanged"
                receipt = self.native_receipt(mode, targets, reconcile_target)
                rows = {(row["repo"], row["number"]): row for row in receipt["issues"] if row["role"] == "related"}
                self.assertEqual(set(rows), set(targets))
                self.assertEqual(rows[targets[0]]["status"], "review_posting_authority")
                self.assertEqual(rows[targets[0]]["error"]["failure"], failure.as_dict())
                self.assertEqual(rows[targets[0]]["error"]["trace_id"], "fixture-denial-trace")
                self.assertEqual(rows[targets[1]]["status"], "not_attempted")
                self.assertNotIn(targets[1], attempted)
                self.assertIn(targets[2], attempted)
                self.assertEqual(rows[targets[2]]["status"], "unchanged")
                self.assertTrue(receipt["ok"])
                self.assertEqual(receipt["coverage"], "partial")

    def test_unknown_write_outcome_stops_and_lists_every_remaining_target(self) -> None:
        targets = [(REPO, 8), (REPO, 9), ("fixture/authorized", 10)]
        failure = PLAN.github_api_core.FailureDetail(cause="transport_error",
            message="Fixture write acknowledgement was lost", retryable=False, fallback_eligible=False,
            disposition="stop", write_outcome="unknown")
        for mode in ("close", "landing"):
            with self.subTest(mode=mode):
                attempted = []
                def reconcile_target(_plan, repo, number, *, apply, entry):
                    self.assertNotIn((repo, number), attempted, "An unknown write outcome must not be retried")
                    attempted.append((repo, number))
                    entry["writes_started"] = True
                    raise PLAN.PlanError("Label write outcome unknown", failure=failure)
                receipt = self.native_receipt(mode, targets, reconcile_target)
                rows = {(row["repo"], row["number"]): row for row in receipt["issues"] if row["role"] == "related"}
                self.assertEqual(set(rows), set(targets))
                self.assertEqual(set(attempted), {targets[0]})
                self.assertEqual(rows[targets[0]]["error"]["failure"], failure.as_dict())
                for target in targets[1:]:
                    self.assertEqual(rows[target]["status"], "not_attempted")
                self.assertFalse(receipt["ok"])
                self.assertEqual(receipt["coverage"], "partial")

    def test_single_read_and_write_denial_are_partial_authority_reviews(self) -> None:
        targets = [("fixture/restricted", 8)]
        failure = PLAN.github_api_core.FailureDetail(cause="permission_denied",
            message="Fixture endpoint denies the current request", retryable=False,
            fallback_eligible=False, disposition="stop", write_outcome="rejected")
        for mode in ("close", "landing"):
            for phase in ("read", "write"):
                with self.subTest(mode=mode, phase=phase):
                    attempted = []
                    def reconcile_target(_plan, repo, number, *, apply, entry):
                        attempted.append((repo, number))
                        if phase == "write":
                            entry["writes_started"] = True
                        raise PLAN.PlanError("Fixture request denied", failure=failure,
                                             payload={"trace_id": "single-target-denial"})
                    receipt = self.native_receipt(mode, targets, reconcile_target)
                    rows = [row for row in receipt["issues"] if row["role"] == "related"]
                    self.assertEqual(attempted, targets)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["status"], "review_posting_authority")
                    self.assertEqual(rows[0]["error"]["failure"], failure.as_dict())
                    self.assertEqual(rows[0]["error"]["trace_id"], "single-target-denial")
                    self.assertTrue(receipt["ok"])
                    self.assertEqual(receipt["coverage"], "partial")

    def test_readback_conflict_is_local_to_its_target(self) -> None:
        targets = [(REPO, 8), (REPO, 9), ("fixture/authorized", 10)]
        for mode in ("close", "landing"):
            with self.subTest(mode=mode):
                attempted = []
                def reconcile_target(_plan, repo, number, *, apply, entry):
                    attempted.append((repo, number))
                    if number == 8:
                        entry["writes_started"] = True
                        raise ValueError("Fixture changed while checking label readback")
                    entry["status"] = "unchanged"
                receipt = self.native_receipt(mode, targets, reconcile_target)
                rows = {(row["repo"], row["number"]): row for row in receipt["issues"] if row["role"] == "related"}
                self.assertEqual(set(attempted), set(targets))
                self.assertEqual(rows[targets[0]]["status"], "reconciliation_failed")
                self.assertTrue(all(rows[target]["status"] == "unchanged" for target in targets[1:]))
                self.assertFalse(receipt["ok"])
                self.assertEqual(receipt["coverage"], "partial")

    def test_remaining_blocker_is_the_only_derived_blocker(self) -> None:
        self.issue["state"] = "open"
        self.issue["labels"] = [{"name": self.config["labels"][key]} for key in ("plan", "active")]
        closed = {"repo": REPO, "number": 5, "state": "closed"}
        remaining = {"repo": "fixture/product", "number": 6, "state": "open"}
        PLAN.read_issue_relationships.return_value = ("fixture-bot", {
            "blocked_by": [closed, remaining], "sub_issues": []})
        entry = {}
        original_body = self.issue["body"]
        landing.reconcile_related(PLAN, REPO, 7, apply=True, entry=entry)
        self.assertEqual(entry["derived_status"], "blocked")
        self.assertEqual(entry["open_blockers"], [remaining])
        self.assertEqual(set(PLAN.normalize_labels(self.issue["labels"])),
                         {self.config["labels"][key] for key in ("plan", "blocked")})
        self.assertEqual(self.issue["body"], original_body)
        self.state.assert_not_called()

    def test_close_entrypoint_recomputes_native_parent_and_cross_repo_dependent(self) -> None:
        product = "fixture/product"
        self.issue.update(state="open", state_reason=None)
        records = {(REPO, 7): self.issue}
        for repo, number in ((REPO, 8), (product, 9)):
            record = copy.deepcopy(self.issue)
            record.update(repo=repo, number=number, html_url=f"https://github.com/{repo}/issues/{number}",
                          labels=[{"name": self.config["labels"][key]} for key in ("plan", "blocked")])
            records[repo, number] = record
        original_bodies = {key: record["body"] for key, record in records.items()}
        PLAN.get_issue.side_effect = lambda number, repo: ("fixture-bot", copy.deepcopy(records[repo, int(number)]))
        PLAN.read_next_parent.side_effect = lambda repo, number: (
            {"repo": REPO, "number": 8, "state": "open"} if (repo, number) == (REPO, 7) else None)

        def relationships(repo, number, **_kwargs):
            return "fixture-bot", {
                "blocked_by": ([] if (repo, number) == (REPO, 7) else [
                    {"repo": REPO, "number": 7, "state": records[REPO, 7]["state"]}]),
                "blocking": ([{"repo": product, "number": 9, "state": "open"}]
                             if (repo, number) == (REPO, 7) else []), "sub_issues": []}

        def labels(number, **kwargs):
            record = records[kwargs["repo"], number]
            names = set(PLAN.normalize_labels(record["labels"])) - set(kwargs["remove_labels"] or [])
            record["labels"] = [{"name": name} for name in names | set(kwargs["add_labels"] or [])]
            return {"actor": "fixture-bot"}

        def close(number, **kwargs):
            record = records[kwargs["repo"], number]
            record.update(state=kwargs["state"], state_reason=kwargs["state_reason"])
            return {"actor": "fixture-bot"}

        PLAN.read_issue_relationships.side_effect = relationships
        self.labels.side_effect = labels
        self.state.side_effect = close
        with contextlib.redirect_stdout(io.StringIO()):
            PLAN.cmd_close(SimpleNamespace(repo=REPO, issue="7", reason="completed", body=None,
                                           body_file=None, project=None, owner=None))
        self.assertEqual(records[REPO, 7]["state"], "closed")
        for key in ((REPO, 8), (product, 9)):
            self.assertEqual(records[key]["state"], "open")
            self.assertEqual(set(PLAN.normalize_labels(records[key]["labels"])),
                             {self.config["labels"][name] for name in ("plan", "active")})
        self.assertEqual({key: record["body"] for key, record in records.items()}, original_bodies)

    def test_malformed_closed_wait_and_unstatused_open_plan_require_review(self) -> None:
        original = copy.deepcopy(self.issue)
        self.issue["body"] += "\n" + github_plan_wait.PREFIX + "malformed -->"
        closed_body = self.issue["body"]
        entry = {}
        landing.reconcile_related(PLAN, REPO, 7, apply=True, entry=entry)
        self.assertEqual(entry["status"], "review_wait_record")
        self.assertEqual(set(PLAN.normalize_labels(self.issue["labels"])),
                         {self.config["labels"][key] for key in ("plan", "done", "waiting")})
        self.assertEqual(entry["projection_status"], "reconciled")
        self.assertEqual(self.issue["body"], closed_body)
        self.issue.update(original)
        self.issue["state"] = "open"
        self.issue["labels"] = [{"name": self.config["labels"]["plan"]}]
        original = copy.deepcopy(self.issue)
        entry = {}
        landing.reconcile_related(PLAN, REPO, 7, apply=True, entry=entry)
        self.assertTrue(entry["status"].startswith("review_"), entry)
        self.assertEqual(self.issue, original)
        self.state.assert_not_called()

    def test_keyword_closure_retains_custody_and_other_status_fields(self) -> None:
        custody = "Evidence: https://github.com/fixture/catalog/issues/7#issuecomment-123\nRetained artifacts: /fixture/retained/task\nNext action: Supervisor adopts retained artifacts."
        self.issue["body"] += custody
        self.assertTrue(self.run_landing()["ok"])
        self.assertIn(custody, self.issue["body"])
        after = self.issue["body"]
        self.assertTrue(self.run_landing()["ok"])
        self.assertEqual(self.issue["body"], after)

    def test_timeline_only_mentions_are_informational_and_do_not_expand_graph(self) -> None:
        self.reader.graphql_json.return_value.body = graph()
        PLAN.collect_paged_rest_items.side_effect = lambda path, **_: ("fixture-bot", [
            {"event": "cross-referenced", "source": {"issue": copy.deepcopy(self.issue)}}]
            if path.endswith("timeline") else [])
        self.issue["state"] = "open"
        original = copy.deepcopy(self.issue)
        result = self.run_landing()
        self.assertTrue(result["ok"])
        self.assertTrue(result["review_required"])
        self.assertEqual(result["issues"][0]["status"], "review_reference_only")
        PLAN.read_issue_relationships.assert_not_called()
        self.assertEqual(self.issue, original)
        self.labels.assert_not_called()

    def test_explicit_partial_source_keeps_finish_line_review_when_also_dependent(self) -> None:
        dependent = copy.deepcopy(self.issue)
        dependent.update(number=8, state="open", state_reason=None,
                         labels=[{"name": self.config["labels"][key]} for key in ("plan", "blocked")])
        PLAN.get_issue.side_effect = lambda number, repo: ("fixture-bot", copy.deepcopy(
            dependent if int(number) == 8 else self.issue))
        response = graph(7)
        response["data"]["repository"]["pullRequest"]["body"] = "Fixes #7. Refs #8 (partial API only)."
        self.reader.graphql_json.return_value.body = response
        PLAN.read_issue_relationships.return_value = ("fixture-bot", {
            "blocked_by": [], "sub_issues": [], "blocking": [{"repo": REPO, "number": 8, "state": "open"}]})
        def edit(number, **kwargs):
            if number != 8:
                return self.edit_labels(number, **kwargs)
            names = set(PLAN.normalize_labels(dependent["labels"])) - set(kwargs["remove_labels"] or [])
            dependent["labels"] = [{"name": name} for name in names | set(kwargs["add_labels"] or [])]
            return {"actor": "fixture-bot"}
        self.labels.side_effect = edit
        result = self.run_landing()
        entry = next(entry for entry in result["issues"] if entry["number"] == 8 and entry["role"] == "source")
        self.assertEqual(entry["role"], "source")
        self.assertEqual(entry["status"], "review_open_finish_line")
        related = next(entry for entry in result["issues"] if entry["number"] == 8 and entry["role"] == "related")
        self.assertEqual(related["status"], "reconciled")
        self.assertTrue(result["ok"])
        self.assertTrue(result["review_required"])
        self.assertEqual(dependent["state"], "open")
        self.state.assert_not_called()

    def test_human_parent_preserves_body_and_derives_labels_from_trusted_wait(self) -> None:
        parent = copy.deepcopy(self.issue)
        parent.update(number=8, state="open", state_reason=None, user={"login": "reporter"},
                      labels=[{"name": self.config["labels"][key]} for key in ("plan", "blocked")])
        PLAN.get_issue.side_effect = lambda number, repo: ("fixture-bot", copy.deepcopy(
            parent if int(number) == 8 else self.issue))
        PLAN.read_next_parent.return_value = {"repo": REPO, "number": 8, "state": "open"}
        PLAN.collect_paged_rest_items.side_effect = lambda path, **_: ("fixture-bot", [{
            "user": {"login": "fixture-bot"}, "body": github_plan_wait.marker(
                github_plan_wait.reviewed(evidence="Parent discussion reviewed; no unresolved person/event hold"))}]
            if path.endswith("comments") else [])
        def edit_parent(number, **kwargs):
            if number != 8:
                return self.edit_labels(number, **kwargs)
            names = set(PLAN.normalize_labels(parent["labels"])) - set(kwargs["remove_labels"] or [])
            parent["labels"] = [{"name": name} for name in names | set(kwargs["add_labels"] or [])]
            return {"actor": "fixture-bot"}
        self.labels.side_effect = edit_parent
        before = copy.deepcopy(parent)
        result = self.run_landing()
        entry = next(entry for entry in result["issues"] if entry["number"] == 8)
        self.assertEqual(entry["status"], "reconciled")
        self.assertTrue(result["ok"])
        self.assertEqual(parent["body"], before["body"])
        self.assertEqual(parent["state"], "open")

    def test_related_readback_mismatch_and_concurrent_wait_fail(self) -> None:
        self.issue["state"] = "open"
        self.issue["labels"] = [{"name": self.config["labels"][key]} for key in ("plan", "blocked")]
        self.labels.side_effect = lambda *_args, **_kwargs: {"actor": "fixture-bot"}
        with self.assertRaises(ValueError):
            landing.reconcile_related(PLAN, REPO, 7, apply=True, entry={})
        self.labels.reset_mock()
        def changed(*args, **kwargs):
            self.issue["body"] += "\n" + github_plan_wait.marker(github_plan_wait.reviewed(
                evidence="New wait", waiting_for="Director", ends_when="Test complete"))
            return False
        with patch.object(landing, "comment_attention", side_effect=changed), self.assertRaises(ValueError):
            landing.reconcile_related(PLAN, REPO, 7, apply=True, entry={})
        self.labels.assert_not_called()

    def test_parent_and_cross_repo_dependent_discovery_is_bounded_and_deduplicated(self) -> None:
        target = {"repo": "fixture/product", "number": 8, "state": "open"}
        PLAN.read_issue_relationships.return_value = ("fixture-bot", {"blocking": [target]})
        PLAN.read_next_parent.return_value = target
        PLAN.read_issue_relationships.side_effect = lambda repo, number, **_: ("fixture-bot", {"blocking": [
            target if repo == REPO else {"repo": "fixture/product", "number": 9, "state": "open"}]})
        self.assertEqual(landing.neighborhood(PLAN, REPO, [(REPO, 7)]), [(target["repo"], target["number"])])
        PLAN.read_issue_relationships.side_effect = None
        PLAN.read_issue_relationships.return_value = ("fixture-bot", {"blocking": [
            {"repo": REPO, "number": number} for number in range(100, 201)]})
        with self.assertRaises(ValueError):
            landing.neighborhood(PLAN, REPO, [(REPO, 7)])

    def test_foreign_first_source_cannot_change_neighborhood_owner(self) -> None:
        target = {"repo": REPO, "number": 8, "state": "open"}
        PLAN.read_issue_relationships.return_value = ("fixture-bot", {"blocking": [target]})
        related = landing.neighborhood(PLAN, REPO, [("foreign/product", 2), (REPO, 7), (REPO, 8)])
        self.assertEqual(related, [(REPO, 8)])
        self.assertTrue(all(call.args[0] == REPO for call in PLAN.read_issue_relationships.call_args_list))

    def test_unverified_target_config_and_non_plan_labels_never_write(self) -> None:
        self.issue["state"] = "open"
        PLAN.load_config.side_effect = lambda *_args, **_kwargs: self.config
        entry = {}
        landing.reconcile_related(PLAN, "fixture/product", 7, apply=True, entry=entry)
        self.assertEqual(entry["status"], "review_planning_config")
        self.labels.assert_not_called()
        for key in ("audit", "own_project"):
            issue = {**self.issue, "labels": [{"name": self.config["labels"][key]}]}
            self.assertFalse(landing.planning_issue(issue, self.config))

    def test_incomplete_discovery_and_relationships_never_write(self) -> None:
        PLAN.collect_paged_rest_items.side_effect = PLAN.PlanError("Timeline unavailable")
        with self.assertRaises(PLAN.PlanError):
            self.run_landing()
        PLAN.collect_paged_rest_items.side_effect = None
        PLAN.read_issue_relationships.side_effect = PLAN.PlanError("Relationships unavailable")
        with self.assertRaises(PLAN.PlanError):
            self.run_landing()
        self.labels.assert_not_called()
        self.body.assert_not_called()

    def test_landing_repairs_cross_repository_dependent_without_closing_parent(self) -> None:
        product = "fixture/product"
        dependent = copy.deepcopy(self.issue)
        dependent.update(repo=product, number=8, state="open", state_reason=None,
                         html_url=f"https://github.com/{product}/issues/8",
                         labels=[{"name": self.config["labels"][key]} for key in ("plan", "blocked")])
        records = {(REPO, 7): self.issue, (product, 8): dependent}
        PLAN.get_issue.side_effect = lambda number, repo: ("fixture-bot", copy.deepcopy(records[repo, int(number)]))
        def relationships(repo, number, **kwargs):
            return "fixture-bot", {"blocked_by": ([{"repo": REPO, "number": 7, "state": "closed"}]
                                                  if repo == product else []),
                                   "blocking": ([{"repo": product, "number": 8, "state": "open"}]
                                                if repo == REPO else []), "sub_issues": []}
        PLAN.read_issue_relationships.side_effect = relationships
        PLAN.read_next_parent.side_effect = lambda repo, number: ({"repo": product, "number": 8, "state": "open"}
                                                                 if repo == REPO else None)
        # The dependent also mentioned the PR; timeline discovery must not
        # suppress its native-dependent label repair.
        PLAN.collect_paged_rest_items.side_effect = lambda path, **_: ("fixture-bot", [
            {"event": "cross-referenced", "source": {"issue": copy.deepcopy(dependent)}}]
            if path.endswith("timeline") else [])
        def labels(number, **kwargs):
            issue = records[kwargs["repo"], number]
            names = set(PLAN.normalize_labels(issue["labels"]))
            names.difference_update(kwargs["remove_labels"] or [])
            names.update(kwargs["add_labels"] or [])
            issue["labels"] = [{"name": name} for name in names]
            return {"actor": "fixture-bot"}
        self.labels.side_effect = labels
        before = copy.deepcopy(dependent)
        preview = self.run_landing(apply=False)
        self.assertTrue(preview["ok"])
        self.labels.assert_not_called()
        result = self.run_landing()
        self.assertTrue(result["ok"])
        related = [entry for entry in result["issues"] if entry["role"] == "related"]
        self.assertEqual(len(related), 1)
        self.assertEqual(related[0]["status"], "reconciled")
        self.assertEqual(dependent["state"], before["state"])
        self.assertEqual(dependent["body"], before["body"])
        self.assertEqual(set(PLAN.normalize_labels(dependent["labels"])),
                         {self.config["labels"][key] for key in ("plan", "active")})
        again = self.run_landing()
        self.assertTrue(again["ok"])
        self.assertEqual(next(entry for entry in again["issues"] if entry["repo"] == product and entry["number"] == 8)["status"], "unchanged")
        self.state.assert_not_called()


class MutationProjectionTests(unittest.TestCase):
    """Supported writes must leave persisted labels consistent with the graph."""

    def setUp(self) -> None:
        self.config = copy.deepcopy(PLAN.DEFAULT_CONFIG)
        self.config["projects"] = {"enabled": False}
        self.records = {}
        self.discussion = {}
        self.blockers = {}
        self.fact_writes = []
        self.add_issue(7)
        self.add_issue(8)
        patches = [
            patch.multiple(PLAN, default_repo=Mock(return_value=REPO),
                load_config=self.load_config, get_issue=self.get_issue,
                collect_paged_rest_items=self.read_comments, read_issue_relationships=self.relationships,
                read_next_parent=Mock(return_value=None), api_json=self.api,
                comment_route=Mock(return_value=("fixture-bot", "fake-gh", "fixture-bot")),
                ensure_labels=Mock(return_value=("fixture-bot", [])),
                find_existing_plan_issues=Mock(return_value=("fixture-bot", [])),
                close_relationship_preflight=Mock(return_value=("fixture-bot", {"result": "passed"}))),
            patch.object(PLAN.github_identity, "configured_bot_logins", return_value=("fixture-bot",)),
            patch.multiple(PLAN.github_issue_core, edit_issue=self.edit_labels,
                           set_issue_state=self.set_state, create_issue=self.create),
            patch.object(PLAN.github_comment_core, "comment", self.comment),
            patch.object(landing.subprocess, "run", return_value=SimpleNamespace(returncode=0)),
        ]
        for context in patches:
            context.start()
            self.addCleanup(context.stop)

    def add_issue(self, number, *, label_keys=("plan", "active"), wait=None, repo=REPO):
        record = wait or github_plan_wait.reviewed(evidence="Full fixture discussion reviewed")
        self.records[number] = {
            "repo": repo, "id": number + 100, "number": number, "state": "open",
            "title": "Fixture plan", "user": {"login": "fixture-bot"},
            "html_url": f"https://github.com/{repo}/issues/{number}",
            "body": PLAN.PLAN_MANAGED_PROVENANCE_MARKER + "\n" + github_plan_wait.marker(record),
            "labels": [{"name": self.config["labels"][key]} for key in label_keys] + [{"name": "bug"}],
        }
        self.discussion[number] = []
        self.blockers[number] = set()

    def load_config(self, _repo, *, discovery=None, **_kwargs):
        if discovery is not None:
            discovery.update(status="discovered")
        return copy.deepcopy(self.config)

    def get_issue(self, ref, repo):
        issue_repo, number = PLAN.issue_ref(str(ref), repo)
        assert issue_repo == self.records[number]["repo"]
        return "fixture-bot", copy.deepcopy(self.records[number])

    def read_comments(self, path, **_kwargs):
        number = int(path.split("/issues/")[1].split("/")[0])
        if path.endswith("/timeline"):
            return "fixture-bot", []
        assert path.endswith("/comments"), path
        return "fixture-bot", copy.deepcopy(self.discussion[number])

    def relationships(self, repo, number, **_kwargs):
        assert repo == self.records[number]["repo"]
        def reference(target):
            issue = self.records[target]
            return {"repo": issue["repo"], "number": target, "state": issue["state"]}
        return "fixture-bot", {"blocked_by": [reference(n) for n in sorted(self.blockers[number])],
            "blocking": [reference(n) for n, blockers in self.blockers.items() if number in blockers],
            "sub_issues": []}

    def api(self, method, path, payload=None, **_kwargs):
        prefix, _, tail = path.partition("/dependencies/blocked_by")
        assert tail == "" or tail.startswith("/"), path
        number = int(prefix.rsplit("/", 1)[1])
        self.fact_writes.append((method, path, copy.deepcopy(payload)))
        if method == "POST":
            target_id = payload["issue_id"]
            target = next(n for n, issue in self.records.items() if issue["id"] == target_id)
            self.blockers[number].add(target)
        elif method == "DELETE":
            target_id = int(tail[1:])
            target = next(n for n, issue in self.records.items() if issue["id"] == target_id)
            self.blockers[number].discard(target)
        else:
            raise AssertionError((method, path, payload))
        return "fixture-bot", {}

    def edit_labels(self, number, *, repo, add_labels=None, remove_labels=None, **_kwargs):
        assert repo == self.records[number]["repo"]
        names = set(PLAN.normalize_labels(self.records[number]["labels"]))
        names.difference_update(remove_labels or [])
        names.update(add_labels or [])
        self.records[number]["labels"] = [{"name": name} for name in sorted(names)]
        return {"actor": "fixture-bot"}

    def set_state(self, number, *, repo, state, state_reason, **_kwargs):
        assert repo == self.records[number]["repo"]
        self.records[number].update(state=state, state_reason=state_reason)
        return {"actor": "fixture-bot"}

    def comment(self, _kind, number, body, *, repo, **_kwargs):
        assert repo == self.records[number]["repo"]
        self.discussion[number].append({"id": len(self.discussion[number]) + 1,
            "user": {"login": "fixture-bot"}, "body": body})
        return {"actor": "fixture-bot"}

    def create(self, title, body, *, repo, labels, **_kwargs):
        assert repo == REPO
        self.add_issue(9)
        issue = self.records[9]
        issue.update(title=title, body=body, labels=[{"name": name} for name in labels])
        return {**copy.deepcopy(issue), "url": issue["html_url"], "actor": "fixture-bot"}

    def command(self, *argv):
        args = PLAN.build_parser().parse_args(["--repo", REPO, *argv])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            args.func(args)
        result = json.loads(output.getvalue())
        self.assertTrue(result["ok"], result)
        return result

    def assert_labels(self, number, *keys, extras=("bug",)):
        self.assertEqual(set(PLAN.normalize_labels(self.records[number]["labels"])),
                         {self.config["labels"][key] for key in keys} | set(extras))

    def test_wait_records_person_hold_and_refreshes_persisted_labels(self):
        original = self.records[7]["body"]
        self.command("wait", "7", "--waiting-for", "Client acceptance", "--ends-when",
                     "Client accepts the current head", "--evidence", "Client requested acceptance")
        observed = PLAN.current_wait(self.records[7], self.discussion[7], REPO)
        self.assertEqual(observed["state"], "waiting")
        self.assert_labels(7, "plan", "waiting")
        self.assertEqual(self.records[7]["state"], "open")
        self.assertEqual(self.records[7]["body"], original)

    def test_wait_clear_refreshes_stale_labels_from_remaining_native_blockers(self):
        for blocked in (False, True):
            with self.subTest(blocked=blocked):
                self.add_issue(7, label_keys=("plan", "blocked", "waiting"), wait=github_plan_wait.reviewed(
                    evidence="Client still testing", waiting_for="Client", ends_when="Client accepts"))
                if blocked:
                    self.blockers[7].add(8)
                self.command("wait", "7", "--clear", "--evidence", "Client accepted; all holds reviewed")
                self.assertEqual(PLAN.current_wait(self.records[7], self.discussion[7], REPO)["state"], "clear")
                self.assert_labels(7, "plan", "blocked" if blocked else "active")

    def test_link_refreshes_the_blocked_side_in_both_relationship_directions(self):
        for relationship, source, target in (("blocked-by", "7", "8"), ("blocks", "8", "7")):
            with self.subTest(relationship=relationship):
                self.add_issue(7)
                original = self.records[7]["body"]
                self.command("link", source, relationship, target)
                self.assertEqual(self.blockers[7], {8})
                self.assert_labels(7, "plan", "blocked")
                self.assert_labels(8, "plan", "active")
                self.assertEqual(self.records[7]["body"], original)
                self.assertEqual(self.records[7]["state"], "open")

    def test_unlink_last_blocker_refreshes_the_blocked_side_in_both_directions(self):
        for relationship, source, target in (("blocked-by", "7", "8"), ("blocks", "8", "7")):
            with self.subTest(relationship=relationship):
                self.add_issue(7, label_keys=("plan", "blocked"))
                self.blockers[7].add(8)
                original = self.records[7]["body"]
                self.command("unlink", source, relationship, target)
                self.assertEqual(self.blockers[7], set())
                self.assert_labels(7, "plan", "active")
                self.assert_labels(8, "plan", "active")
                self.assertEqual(self.records[7]["body"], original)
                self.assertEqual(self.records[7]["state"], "open")

    def test_create_with_actual_wait_persists_waiting_instead_of_default_active(self):
        self.command("create", "Await acceptance", "--waiting-for", "Client acceptance", "--ends-when",
                     "Client accepts", "--wait-evidence", "Client requested a test")
        observed = PLAN.current_wait(self.records[9], self.discussion[9], REPO)
        self.assertEqual(observed["state"], "waiting")
        self.assert_labels(9, "plan", "waiting", extras=())
        self.assertEqual(self.records[9]["state"], "open")

    def test_direct_close_keeps_actual_wait_alongside_done(self):
        self.add_issue(7, label_keys=("plan", "active", "waiting"), wait=github_plan_wait.reviewed(
            evidence="Client acceptance remains after delivery", waiting_for="Client acceptance",
            ends_when="Client accepts"))
        original = self.records[7]["body"]
        self.command("close", "7")
        self.assertEqual(self.records[7]["state"], "closed")
        self.assertEqual(PLAN.current_wait(self.records[7], self.discussion[7], REPO)["state"], "waiting")
        self.assert_labels(7, "plan", "done", "waiting")
        self.assertEqual(self.records[7]["body"], original)

    def test_status_preview_and_apply_change_only_the_derived_labels(self):
        self.add_issue(7, label_keys=("plan", "blocked", "waiting"))
        before = copy.deepcopy((self.records, self.discussion, self.blockers, self.fact_writes))
        preview = self.command("reconcile-status", "7")
        self.assertEqual((self.records, self.discussion, self.blockers, self.fact_writes), before)
        self.assertEqual(preview["issues"][0]["derived"]["status"], "active")
        self.command("reconcile-status", "7", "--apply")
        self.assert_labels(7, "plan", "active")
        self.assertEqual((self.discussion, self.blockers, self.fact_writes), before[1:])
        self.assertEqual(self.records[7]["state"], before[0][7]["state"])
        self.assertEqual(self.records[7]["body"], before[0][7]["body"])
        self.assertEqual(self.records[8], before[0][8])

    def test_committed_edge_projection_failure_recovers_without_repeating_the_edge(self):
        failure = PLAN.github_api_core.FailureDetail(cause="transport_error", message="Fixture label write rejected",
            retryable=False, fallback_eligible=False, disposition="stop", write_outcome="rejected",
            request_id="fixture-label-request")
        before = copy.deepcopy((self.records[7]["body"], self.discussion))
        with patch.object(PLAN.github_issue_core, "edit_issue", side_effect=PLAN.github_issue_core.IssueError(
                failure.message, failure=failure)), self.assertRaises(PLAN.PlanError) as caught:
            self.command("link", "7", "blocked-by", "8")
        self.assertEqual(self.blockers[7], {8})
        self.assert_labels(7, "plan", "active")
        receipt = caught.exception.payload
        self.assertEqual(receipt["projection"]["issues"][0]["error"]["failure"], failure.as_dict())
        self.assertEqual(receipt["recovery"]["issue"], {"repo": REPO, "number": 7})
        fact_writes = copy.deepcopy(self.fact_writes)
        # Execute the returned supported recovery, rather than replaying link.
        recovery = PLAN.build_parser().parse_args(shlex.split(receipt["recovery"]["next_action"])[1:])
        with contextlib.redirect_stdout(io.StringIO()):
            recovery.func(recovery)
        self.assertEqual(self.fact_writes, fact_writes)
        self.assertEqual(self.blockers[7], {8})
        self.assert_labels(7, "plan", "blocked")
        self.assertEqual((self.records[7]["body"], self.discussion), before)
        self.assertEqual(self.records[7]["state"], "open")

    def test_direct_close_with_unreviewed_wait_repairs_labels_and_refreshes_dependents(self):
        self.add_issue(7, label_keys=("plan", "active", "waiting"))
        self.records[7]["body"] = github_plan_wait.without_records(self.records[7]["body"])
        self.add_issue(8, label_keys=("plan", "blocked"))
        self.blockers[8].add(7)
        before = copy.deepcopy((self.records[7]["body"], self.records[8]["body"], self.blockers))
        evidence = "Completion evidence retained while legacy wait needs review."
        result = self.command("close", "7", "--comment", evidence)
        self.assertEqual(self.records[7]["state"], "closed")
        self.assert_labels(7, "plan", "done", "waiting")
        self.assert_labels(8, "plan", "active")
        self.assertEqual(self.records[8]["state"], "open")
        self.assertEqual((self.records[7]["body"], self.records[8]["body"], self.blockers), before)
        self.assertEqual(self.discussion[7][0]["body"], evidence)
        self.assertEqual(self.discussion[8], [])
        self.assertEqual(result["source_projection"]["status"], "review_wait_record")
        self.assertEqual(result["source_projection"]["projection_status"], "reconciled")
        self.assertEqual(result["coverage"], "partial")
        self.assertTrue(result["review_required"])

    def test_confirmed_close_with_label_permission_denial_requires_existing_authority(self):
        before = copy.deepcopy((self.records[7]["labels"], self.records[7]["body"], self.discussion))
        failure = PLAN.github_api_core.FailureDetail(cause="permission_denied",
            message="Fixture source label endpoint denied", retryable=False, fallback_eligible=False,
            disposition="stop", write_outcome="rejected", request_id="fixture-source-denial")
        with patch.object(PLAN.github_issue_core, "edit_issue", side_effect=PLAN.github_issue_core.IssueError(
                failure.message, failure=failure)), self.assertRaises(PLAN.PlanError) as caught:
            self.command("close", "7")
        self.assertEqual(self.records[7]["state"], "closed")
        self.assertEqual((self.records[7]["labels"], self.records[7]["body"], self.discussion), before)
        self.assertIs(caught.exception.failure, failure)
        recovery = caught.exception.payload["recovery"]
        self.assertTrue(recovery["issue_closed"])
        self.assertFalse(recovery["retry_safe"])
        self.assertEqual(recovery["next_action"], "existing_authorized_target_session")
        command = PLAN.build_parser().parse_args(shlex.split(recovery["projection_command"])[1:])
        self.assertEqual(command.command, "reconcile-status")
        self.assertTrue(command.include_dependents and command.apply)

    def test_exact_personal_routes_repair_explicit_source_but_hold_automatic_dependents(self):
        for route in ("own_user", "active_fallback", "skip_bot", "active_first"):
            with self.subTest(route=route), exact_personal_route(route):
                self.add_issue(7)
                self.add_issue(8, label_keys=("plan", "blocked"))
                self.blockers[8].add(7)
                result = self.command("close", "7")
                self.assert_labels(7, "plan", "done")
                self.assert_labels(8, "plan", "blocked")
                self.assertEqual(result["dependents"]["issues"][0]["status"], "review_posting_authority")
                self.records[7]["labels"] = [{"name": self.config["labels"][key]} for key in ("plan", "active")]
                before = copy.deepcopy((self.records, self.discussion, self.blockers))
                preview = self.command("reconcile-status", "7", "--include-dependents")
                self.assertEqual((self.records, self.discussion, self.blockers), before)
                self.assertTrue(all(row["projection_status"] == "would_reconcile" for row in preview["issues"]))
                applied = self.command("reconcile-status", "7", "--include-dependents", "--apply")
                self.assert_labels(7, "plan", "done", extras=())
                self.assert_labels(8, "plan", "blocked")
                self.assertEqual(next(row for row in applied["issues"] if row["number"] == 8)["status"],
                                 "review_posting_authority")

    def test_landing_personal_routes_hold_all_discovered_tasks_but_allow_preview(self):
        for route in ("own_user", "active_fallback", "skip_bot", "active_first"):
            with self.subTest(route=route), exact_personal_route(route):
                self.add_issue(7)
                self.records[7].update(state="closed", state_reason="completed")
                self.add_issue(8, label_keys=("plan", "blocked"))
                self.blockers[8].add(7)
                reader = Mock()
                reader.graphql_json.return_value = SimpleNamespace(ok=True, body=graph(7))
                before = copy.deepcopy((self.records, self.discussion, self.blockers))
                applied = landing.reconcile(PLAN, reader, REPO, 9, apply=True)
                self.assertEqual({row["role"] for row in applied["issues"]}, {"source", "related"})
                self.assertTrue(all(row["status"] == "review_posting_authority" for row in applied["issues"]))
                preview = landing.reconcile(PLAN, reader, REPO, 9, apply=False)
                self.assertTrue(all(row["status"] == "would_reconcile" for row in preview["issues"]))
                self.assertEqual((self.records, self.discussion, self.blockers), before)

    def test_waiting_source_keeps_the_actual_projection_reason_and_preview(self):
        held = github_plan_wait.reviewed(evidence="Client still tests the completed delivery",
            waiting_for="Client acceptance", ends_when="Client accepts")
        for wait in (held, None):
            for reason in ("config", "radar", "preview"):
                with self.subTest(wait=wait, reason=reason):
                    self.add_issue(7, label_keys=("plan", "active", "waiting"), wait=held)
                    self.records[7].update(state="closed", state_reason="completed")
                    if wait is None:
                        self.records[7]["body"] = github_plan_wait.without_records(self.records[7]["body"])
                    before = copy.deepcopy((self.records[7], self.discussion))
                    def config(repo, *, discovery=None, **kwargs):
                        if discovery is not None:
                            discovery.update(status="unavailable" if reason == "config" else "discovered")
                        return copy.deepcopy(self.config)
                    entry = {}
                    with patch.object(PLAN, "load_config", side_effect=config), \
                            patch.object(landing, "comment_attention", return_value=reason == "radar"):
                        landing.reconcile_issue(PLAN, REPO, 7, graph(7)["data"]["repository"]["pullRequest"],
                                                apply=reason != "preview", entry=entry)
                    self.assertTrue(entry["wait_review_required"])
                    self.assertEqual(entry["status"], {"config": "review_planning_config",
                        "radar": "review_comments_or_coverage", "preview": "review_wait_record"}[reason])
                    if reason == "preview":
                        self.assertEqual(entry["projection_status"], "would_reconcile")
                    self.assertEqual((self.records[7], self.discussion), before)

    def independent_dependents(self):
        self.add_issue(8, label_keys=("plan", "blocked"))
        self.add_issue(9, label_keys=("plan", "blocked"), repo="fixture/independent")
        self.blockers[8].add(7)
        self.blockers[9].add(7)

    def read_error(self, cause, *, outcome=None):
        return PLAN.PlanError("Fixture source discussion read failed",
            failure=PLAN.github_api_core.FailureDetail(cause=cause, message="Fixture exact discussion read failure",
                retryable=False, fallback_eligible=False, disposition="stop", write_outcome=outcome,
                request_id="fixture-discussion-request"), payload={"trace_id": "fixture-discussion-trace"})

    def test_transient_source_wait_read_repairs_lifecycle_posts_comment_and_refreshes_dependents(self):
        self.add_issue(7, label_keys=("plan", "active", "waiting"))
        self.independent_dependents()
        error = self.read_error("network_provider_failure")
        def comments(path, **kwargs):
            if "/issues/7/comments" in path:
                raise error
            return self.read_comments(path, **kwargs)
        with patch.object(PLAN, "collect_paged_rest_items", side_effect=comments):
            result = self.command("close", "7", "--comment", "Requested completion evidence.")
        self.assert_labels(7, "plan", "done", "waiting")
        self.assert_labels(8, "plan", "active")
        self.assert_labels(9, "plan", "active")
        self.assertEqual(self.discussion[7][0]["body"], "Requested completion evidence.")
        evidence = json.loads(result["source_projection"]["derived"]["structured_wait"]["evidence"])
        self.assertEqual(evidence["failure"], error.failure.as_dict())
        self.assertEqual(evidence["trace_id"], error.payload["trace_id"])
        self.assertEqual(result["coverage"], "partial")

    def test_closed_related_transient_wait_read_repairs_labels_and_keeps_exact_failure(self):
        self.add_issue(7, label_keys=("plan", "active", "waiting"))
        self.records[7]["state"] = "closed"
        error = self.read_error("network_provider_failure")
        with patch.object(PLAN, "collect_paged_rest_items", side_effect=error):
            result = self.command("reconcile-status", "7", "--apply")
        self.assert_labels(7, "plan", "done", "waiting")
        self.assertEqual(result["coverage"], "partial")
        row = result["issues"][0]
        self.assertTrue(row["ok"])
        self.assertFalse(row["verified"])
        self.assertEqual(row["projection_status"], "reconciled")
        self.assertEqual(json.loads(row["wait"]["evidence"])["failure"], error.failure.as_dict())
        self.assertEqual(self.discussion[7], [])

    def test_source_comment_read_denial_skips_same_repo_and_continues_independent_repo(self):
        self.independent_dependents()
        before = copy.deepcopy(self.records[7]["labels"])
        error = self.read_error("permission_denied", outcome="rejected")
        def comments(path, **kwargs):
            if "/issues/7/comments" in path:
                raise error
            return self.read_comments(path, **kwargs)
        with patch.object(PLAN, "collect_paged_rest_items", side_effect=comments), self.assertRaises(PLAN.PlanError) as caught:
            self.command("close", "7", "--comment", "Evidence must not post after denial.")
        self.assertEqual(self.records[7]["state"], "closed")
        self.assertEqual(self.records[7]["labels"], before)
        self.assertEqual(self.discussion[7], [])
        self.assert_labels(8, "plan", "blocked")
        self.assert_labels(9, "plan", "active")
        result = caught.exception.payload
        self.assertEqual(result["source_projection"]["status"], "review_posting_authority")
        self.assertEqual(result["source_projection"]["error"]["failure"], error.failure.as_dict())
        self.assertFalse(result["recovery"]["retry_safe"])
        self.assertEqual(result["recovery"]["next_action"], "existing_authorized_target_session")
        self.assertEqual(next(row for row in result["dependents"]["issues"] if row["number"] == 8)["status"],
                         "not_attempted")

    def test_source_label_denial_continues_independent_repo_but_unknown_write_stops_every_target(self):
        for cause, outcome in (("permission_denied", "rejected"), ("permission_denied", "unknown")):
            with self.subTest(outcome=outcome):
                self.add_issue(7)
                self.independent_dependents()
                failure = PLAN.github_api_core.FailureDetail(cause=cause, message="Fixture source label failure",
                    retryable=False, fallback_eligible=False, disposition="stop", write_outcome=outcome)
                def edit(number, **kwargs):
                    if number == 7:
                        raise PLAN.github_issue_core.IssueError(failure.message, failure=failure)
                    return self.edit_labels(number, **kwargs)
                with patch.object(PLAN.github_issue_core, "edit_issue", side_effect=edit), self.assertRaises(PLAN.PlanError) as caught:
                    self.command("close", "7", "--comment", "No comment after source write refusal.")
                self.assertEqual(self.records[7]["state"], "closed")
                self.assertEqual(self.discussion[7], [])
                self.assert_labels(8, "plan", "blocked")
                self.assert_labels(9, "plan", "active" if outcome == "rejected" else "blocked")
                rows = {row["number"]: row for row in caught.exception.payload["dependents"]["issues"]}
                self.assertEqual(set(rows), {8, 9})
                self.assertEqual(rows[8]["status"], "not_attempted")
                if outcome == "unknown":
                    self.assertEqual(rows[9]["status"], "not_attempted")

    def test_explicit_fact_projection_denials_fail_without_losing_the_committed_fact(self):
        commands = (
            ("wait", "7", "--clear", "--evidence", "Client accepted"),
            ("create", "New request", "--waiting-for", "Client", "--ends-when", "Client accepts",
             "--wait-evidence", "Client requested acceptance"),
            ("link", "7", "blocked-by", "8"),
            ("unlink", "7", "blocked-by", "8"),
        )
        failure = PLAN.github_api_core.FailureDetail(cause="permission_denied", message="Explicit target labels denied",
            retryable=False, fallback_eligible=False, disposition="stop", write_outcome="rejected")
        for argv in commands:
            with self.subTest(command=argv[0]):
                if argv[0] == "wait":
                    self.add_issue(7, label_keys=("plan", "waiting"), wait=github_plan_wait.reviewed(
                        evidence="Client requested acceptance", waiting_for="Client", ends_when="Client accepts"))
                else:
                    self.add_issue(7, label_keys=("plan", "blocked") if argv[0] == "unlink" else ("plan", "active"))
                self.add_issue(8)
                if argv[0] == "unlink":
                    self.blockers[7].add(8)
                with patch.object(PLAN.github_issue_core, "edit_issue", side_effect=PLAN.github_issue_core.IssueError(
                        failure.message, failure=failure)), self.assertRaises(PLAN.PlanError) as caught:
                    self.command(*argv)
                result = caught.exception.payload
                row = result["projection"]["issues"][0]
                self.assertFalse(result["projection"]["ok"])
                self.assertFalse(row["ok"])
                self.assertTrue(row["required"])
                self.assertEqual(row["error"]["failure"], failure.as_dict())
                if argv[0] == "wait":
                    self.assertEqual(PLAN.current_wait(self.records[7], self.discussion[7], REPO)["state"], "clear")
                elif argv[0] == "create":
                    self.assertEqual(PLAN.current_wait(self.records[9], self.discussion[9], REPO)["state"], "waiting")
                else:
                    self.assertEqual(self.blockers[7], {8} if argv[0] == "link" else set())

    def test_pending_close_comment_resume_preserves_exact_note_without_closing_twice(self):
        note = "Completion evidence: literal \\u0027 and ${VALUE}.\nRetain café capture.\n"
        closed = []
        def state(number, **kwargs):
            closed.append((number, kwargs["state"]))
            return self.set_state(number, **kwargs)
        failure = PLAN.github_api_core.FailureDetail(cause="transport_error", message="Label write rejected before note",
            retryable=False, fallback_eligible=False, disposition="stop", write_outcome="rejected")
        with patch.object(PLAN.github_issue_core, "set_issue_state", side_effect=state):
            with patch.object(PLAN.github_issue_core, "edit_issue", side_effect=PLAN.github_issue_core.IssueError(
                    failure.message, failure=failure)), self.assertRaises(PLAN.PlanError) as caught:
                self.command("close", "7", "--reason", "completed", "--owner", "fixture-owner", "--comment", note)
            pending = caught.exception.payload["recovery"]["pending_comment"]
            self.assertEqual(pending["write_outcome"], "not_attempted")
            self.assertEqual(self.discussion[7], [])
            resume = PLAN.build_parser().parse_args(pending["resume_argv"][1:])
            self.assertEqual(resume.body, note)
            self.assertEqual(resume.owner, "fixture-owner")
            self.assertEqual(resume.reason, "completed")
            with contextlib.redirect_stdout(io.StringIO()):
                resume.func(resume)
        self.assertEqual(closed, [(7, "closed")])
        self.assertEqual([comment["body"] for comment in self.discussion[7]], [note])
        self.assert_labels(7, "plan", "done")

    def test_status_recovery_uses_verified_checkout_for_every_same_repo_snapshot(self):
        self.check_verified_checkout("reconcile-status")

    def test_close_uses_verified_checkout_for_every_same_repo_snapshot(self):
        self.check_verified_checkout("close")

    def check_verified_checkout(self, command):
        self.config["labels"] = {key: "target:" + key for key in self.config["labels"]}
        self.add_issue(7)
        self.add_issue(8, label_keys=("plan", "blocked"))
        self.records[7].update(state="closed", state_reason="completed")
        other = copy.deepcopy(self.config)
        other["labels"] = {key: "other:" + key for key in other["labels"]}
        self.add_issue(9, repo="fixture/other", label_keys=("plan", "blocked"))
        self.records[9]["labels"] = [{"name": other["labels"][key]} for key in ("plan", "blocked")]
        self.blockers[8] = self.blockers[9] = {7}
        observations = []
        with tempfile.TemporaryDirectory() as checkout:
            def config(repo, *, checkout=None, discovery=None):
                observations.append((repo, checkout))
                if discovery is not None:
                    discovery.update(status="discovered")
                return copy.deepcopy(self.config if repo == REPO else other)
            with patch.object(PLAN, "repo_from_git", return_value="fixture/wrong"), self.assertRaises(PLAN.PlanError):
                self.command(command, "7", "--planning-checkout=" + checkout)
            self.assert_labels(7, "plan", "active")
            with patch.object(PLAN, "repo_from_git", return_value=REPO), patch.object(PLAN, "load_config", side_effect=config):
                argv = [command, "7", "--planning-checkout=" + checkout]
                if command == "reconcile-status":
                    argv.extend(["--include-dependents", "--apply"])
                result = self.command(*argv)
                if command == "reconcile-status":
                    resume = PLAN.build_parser().parse_args(shlex.split(result["recovery"]["next_action"])[1:])
                    with contextlib.redirect_stdout(io.StringIO()):
                        resume.func(resume)
            self.assertTrue(all(path == checkout for repo, path in observations if repo == REPO), observations)
            self.assertTrue(any(repo == "fixture/other" for repo, _ in observations), observations)
            self.assertTrue(all(path is None for repo, path in observations if repo == "fixture/other"), observations)
        self.assert_labels(7, "plan", "done")
        self.assert_labels(8, "plan", "active")
        self.assertEqual(set(PLAN.normalize_labels(self.records[9]["labels"])), {other["labels"][key] for key in ("plan", "active")})
        self.assertEqual(self.fact_writes, [])

    def test_planning_close_discovery_failure_preserves_classified_row_and_close_evidence(self):
        failure = PLAN.github_api_core.FailureDetail(cause="permission_denied", message="Source neighborhood denied",
            retryable=False, fallback_eligible=False, disposition="stop", request_id="fixture-close-neighborhood")
        errors = [PLAN.PlanError(failure.message, failure=failure), KeyError("state")]
        for error in errors:
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as checkout, \
                    patch.object(PLAN, "repo_from_git", return_value=REPO):
                self.add_issue(7)
                self.add_issue(8, label_keys=("plan", "blocked"))
                self.blockers[8] = {7}
                state_events = []
                def state(number, **kwargs):
                    state_events.append((number, kwargs["state"]))
                    return self.set_state(number, **kwargs)
                note = "Source completion evidence"
                with patch.object(PLAN.github_issue_core, "set_issue_state", side_effect=state):
                    with patch.object(landing, "neighborhood", side_effect=error), self.assertRaises(PLAN.PlanError) as caught:
                        self.command("close", "7", "--comment", note, "--planning-checkout=" + checkout)
                    payload = caught.exception.payload
                    self.assertEqual(payload["closed"]["number"], 7)
                    self.assertEqual(self.records[7]["state"], "closed")
                    row = payload["dependents"]["issues"][0]
                    self.assertEqual((row["repo"], row["number"]), (REPO, 7))
                    self.assertTrue(row["required"])
                    self.assertFalse(row["ok"])
                    detail = getattr(error, "failure", None)
                    expected = PLAN.github_plan_status.failure_disposition(
                        detail.cause if detail else None, detail.write_outcome if detail else None,
                        writes_started=False, validation_error=isinstance(error, ValueError))
                    self.assertEqual(row["status"], expected["status"])
                    if detail is not None:
                        self.assertEqual(row["error"]["failure"], detail.as_dict())
                        self.assertTrue(expected["repository_denied"])
                        self.assertTrue(row["next_action"])
                    recovery = PLAN.build_parser().parse_args(row["recovery"]["argv"][1:])
                    self.assertTrue(recovery.include_dependents)
                    self.assertEqual(recovery.planning_checkout, checkout)
                    # A later authorized invocation acts after the mocked failure
                    # has resolved; the denied close invocation never retries.
                    with contextlib.redirect_stdout(io.StringIO()):
                        recovery.func(recovery)
                self.assertEqual(state_events, [(7, "closed")])
                self.assertEqual([comment["body"] for comment in self.discussion[7]], [note])
                self.assert_labels(8, "plan", "active")

    def test_pending_close_comment_option_shaped_body_can_resume(self):
        note = "-capture"
        failure = PLAN.github_api_core.FailureDetail(cause="transport_error", message="Label rejected before note",
            retryable=False, fallback_eligible=False, disposition="stop", write_outcome="rejected")
        with patch.multiple(PLAN, project_meta=Mock(return_value=("fixture-bot", 1, {"id": "project", "title": "-capture"})),
                project_fields=Mock(return_value={}), find_project_item=Mock(return_value={"id": "item"})), \
                patch.object(PLAN.pathlib.Path, "is_dir", return_value=True), patch.object(PLAN, "repo_from_git", return_value=REPO), \
                patch.object(PLAN.github_issue_core, "edit_issue", side_effect=PLAN.github_issue_core.IssueError(
                failure.message, failure=failure)), self.assertRaises(PLAN.PlanError) as caught:
            self.command("close", "7", "--comment=" + note, "--owner=-owner", "--project=-capture", "--planning-checkout=-checkout")
        pending = caught.exception.payload["recovery"]["pending_comment"]
        resume = PLAN.build_parser().parse_args(pending["resume_argv"][1:])
        self.assertEqual(resume.body, note)
        self.assertEqual((resume.owner, resume.project, resume.planning_checkout), ("-owner", "-capture", "-checkout"))
        projection = PLAN.build_parser().parse_args(shlex.split(caught.exception.payload["recovery"]["projection_command"])[1:])
        self.assertEqual(projection.planning_checkout, resume.planning_checkout)
        with patch.multiple(PLAN, project_meta=Mock(return_value=("fixture-bot", 1, {"id": "project", "title": "-capture"})),
                project_fields=Mock(return_value={}), find_project_item=Mock(return_value={"id": "item"})), \
                patch.object(PLAN.pathlib.Path, "is_dir", return_value=True), patch.object(PLAN, "repo_from_git", return_value=REPO), \
                contextlib.redirect_stdout(io.StringIO()):
            resume.func(resume)
        self.assertEqual([comment["body"] for comment in self.discussion[7]], [note])

    def test_unknown_close_comment_outcome_retains_uncertainty_and_pending_note(self):
        note = "Retain this exact completion note.\nSecond paragraph."
        failure = PLAN.github_api_core.FailureDetail(cause="network_provider_failure", message="Comment outcome unknown",
            retryable=False, fallback_eligible=False, disposition="stop", write_outcome="unknown")
        error = PLAN.github_comment_core.CommentError(failure.message, failure=failure,
            payload={"reconciliation": {"strategy": "read_comments_for_exact_body", "result": "unavailable"}})
        with patch.object(PLAN.github_comment_core, "comment", side_effect=error), self.assertRaises(PLAN.PlanError) as caught:
            self.command("close", "7", "--comment", note)
        self.assertEqual(self.records[7]["state"], "closed")
        self.assertEqual(caught.exception.failure.write_outcome, "unknown")
        pending = caught.exception.payload["recovery"]["pending_comment"]
        self.assertEqual(pending["write_outcome"], "unknown")
        resume = PLAN.build_parser().parse_args(pending["resume_argv"][1:])
        self.assertEqual(resume.body, note)
        self.assertEqual(caught.exception.payload["reconciliation"], error.payload["reconciliation"])


if __name__ == "__main__":
    unittest.main()
