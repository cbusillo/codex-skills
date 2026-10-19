#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Behavioral tests for claim ownership, races, and partial recovery."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from itertools import product
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import github_agent
import github_plan_claim as CLAIM
import github_plan_wait as wait_record
import github_identity
import github_comment
import github_issue

TEST_BOT = "fixture-bot[bot]"

SPEC = importlib.util.spec_from_file_location("gh_plan_claim_under_test", Path(__file__).with_name("gh-plan.py"))
assert SPEC and SPEC.loader
PLAN: Any = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)

OWNER = {"worker": "trial-a", "session": "session-a", "branch": "work/issue-42", "claimed_at": "2026-10-01T00:00:00Z"}
OTHER = {**OWNER, "worker": "trial-b", "session": "session-b", "branch": "work/other-42"}
RESPONSIBILITY_STATUS = (
    "After these proposals, 61 OPW and 57 CM provider-only entries would remain, "
    "owned by Launchplane engineering for evidence and Chris for production disposition approval."
)
SWEEP_DISCLAIMERS = (
    "Scope and recovery: No timing, code change or not-planned closure is claimed by this sweep; keep all coverage and injected-clock rules.",
    "Scope: no implementation ownership is claimed by this sweep.",
    "No claims, implementation or worktrees were created. This issue is not claimed by the audit session.",
    "The repair was never owned by the sweep; source work can begin after a fresh claim.",
    "Implementation is not yet claimed by a worker. No implementation or execution ownership has been claimed by this sweep.",
    "Worker: None\nSession: unassigned\nNext action: recheck ownership before creating a worktree.",
    "**Claimed by:** no one\n**Worker:** none\n**Session:** n/a",
    "The sweep releases claim bookkeeping to the next agent; no ownership is claimed by this sweep.",
)
# Exact historical BD_to_AVP#744 status paragraph preserved in codex-skills#1309
# comment 6022593581. The context-panel#726 sweep status is SWEEP_DISCLAIMERS[0].
BD_SWEEP_STATUS = (
    "Preserved sweep ownership evidence (the claim helper flagged this exact negative assertion as ambiguous): "
    "`no implementation ownership is claimed by this sweep.` No competing positive implementation claim "
    "appears in this issue's discussion; available session inventory is partial and is not proof of "
    "exclusive availability. The helper refusal was not bypassed or retried."
)


def format_ownership(text: str) -> tuple[str, ...]:
    """Common status formatting, including formatting inside an assertion."""
    return (
        text, f"`{text}`", f"``{text}``", f"**{text}**", f"_{text}_",
        f"***{text}***", f"[{text}](https://example.com/status)",
        f"[{text}][status]\n\n[status]: https://example.com/status", "> " + text.replace("\n", "\n> "),
        text.replace("claimed by", "**claimed** by").replace("owned by", "owned **by**"),
        text.replace("this sweep", "[`this sweep`](https://example.com/status)"),
    )


class ClaimTests(unittest.TestCase):
    def setUp(self):
        # Fixture sessions are labels; the harness running the tests supplies its own native IDs.
        self.enterContext(patch.dict(os.environ))
        for name in (n for names in github_agent.NATIVE_SESSION_VARS.values() for n in names):
            os.environ.pop(name, None)
        self.args =Namespace(repo="owner/repo", issue="42", worker=OWNER["worker"], session=OWNER["session"],
                              branch=OWNER["branch"], next_action="Implement the repair", resume_from=None, wait_resolved=None,
                              agent="codex")
        self.issue: dict[str, Any] = {"repo": "owner/repo", "number": 42, "title": "Repair", "state": "open",
                      "user": {"login": TEST_BOT}, "labels": [],
                      "body": PLAN.PLAN_MANAGED_PROVENANCE_MARKER + "\n\n## Objective\n\nKeep me\n\n## Current Status\n\nState: Open, not started.\n"}
        self.args.planning_checkout = None
        self.targets = {}
        self.closed_pulls = {}
        self.parent = None
        self.children = {}
        self.timelines = {}
        self.history_nodes = {}
        self.history_reads = []
        self.api_failure = None
        self.configs = {}
        self.planning_inventory = None
        self.target_comments = {}
        self.comments: list[dict[str, Any]] = []
        self.comment_id_offset = 0
        self.inventory: dict[str, Any] = {"worktrees": [], "local_branches": [], "remote_branches": [], "sessions": [],
                          "session_coverage": {"codex": {"status": "unavailable"}, "claude": {"status": "available"}}}
        self.pulls = []
        self.blockers = []
        self.events = []
        self.after_post = lambda: None
        self.after_status = lambda: None
        self.during_inventory = lambda: None
        self.observe_inventory = lambda inventory, _comments: inventory
        self.emitted = Mock()
        self.wait = wait_record.reviewed(evidence="Fixture's full history and ownership reviewed; no person/event hold")

    def get_issue(self, ref, repo):
        self.events.append("read_issue")
        issue = copy.deepcopy(self.targets.get(str(ref), self.issue))
        issue["repo"], issue["number"] = PLAN.issue_ref(ref, repo)
        return "bot", issue

    def read_api(self, _method, path, payload=None, **options):
        if path == "/graphql":
            self.history_reads.append((_method, copy.deepcopy(payload), options))
            response = self.history_nodes[payload["variables"]["id"]]
            if isinstance(response, Exception):
                raise response
            return "bot", copy.deepcopy(response)
        if path.endswith("/parent"):
            if self.api_failure:
                raise PLAN.PlanError("Unavailable", api_result=self.api_failure)
            if self.parent is None:
                raise PLAN.PlanError("Not found", api_result={"status": 404})
            return "bot", copy.deepcopy(self.parent)
        n = int(path.rsplit("/", 1)[1])
        if self.api_failure:
            raise PLAN.PlanError("Unavailable", api_result=self.api_failure)
        if n not in self.closed_pulls:
            raise PLAN.PlanError("Not found", api_result={"status": 404})
        return "bot", copy.deepcopy(self.closed_pulls[n])

    def inventory_for(self, repo, _number, **_):
        self.during_inventory()
        if repo == "other/plans" and self.planning_inventory is not None:
            return copy.deepcopy(self.planning_inventory)
        return copy.deepcopy(self.inventory)

    def read_pages(self, path, **_):
        self.events.append("read_comments" if path.endswith("comments") else "read_pulls")
        items = self.target_comments.get(path, self.comments) if path.endswith("comments") else self.blockers if path.endswith("blocked_by") else self.pulls
        if path.endswith("/sub_issues"):
            items = self.children.get(path, [])
        if path.endswith("/timeline"):
            items = self.timelines.get(path, [])
        if path.endswith("comments") and self.wait is not None:
            items = [{"id": -1, "user": {"login": TEST_BOT}, "body": wait_record.marker(self.wait)}, *items]
        if path == "/repos/other/plans/pulls": items = []
        return "bot", copy.deepcopy(items)

    def post(self, _kind, _number, body, **_):
        self.events.append("post")
        self.post_route = (_kind, _number, _["repo"])
        comment_id = self.comment_id_offset + len(self.comments) + 1
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

    def run_claim(self, *, route=None, command=None):
        # Wait-review ownership fixtures isolate record publication/readback;
        # persisted label projection has stateful mutation tests of its own.
        projection = ({"refresh_planning_targets": lambda *_a, **_k: {
            "ok": True, "coverage": "complete", "issues": []}} if command is PLAN.cmd_wait else {})
        with patch.multiple(PLAN, default_repo=lambda repo: repo or "owner/repo", get_issue=self.get_issue,
                            EXPECTED_ACTOR=TEST_BOT,
                            collect_paged_rest_items=self.read_pages, rest_edit_issue=self.edit,
                            comment_route=route or (lambda: ("bot", "bot-gh", TEST_BOT)),
                            load_config=lambda repo, **_: copy.deepcopy(self.configs.get(repo, PLAN.DEFAULT_CONFIG)),
                            emit=self.emitted, api_json=self.read_api, **projection), \
                patch.object(CLAIM, "local_inventory", side_effect=self.inventory_for), \
                patch.object(CLAIM, "observe_sessions", side_effect=self.observe_inventory), \
                patch.object(github_identity, "configured_bot_logins", return_value=[]), \
                patch.object(github_comment, "comment", side_effect=self.post), \
                patch.object(github_issue, "edit_issue", side_effect=self.labels):
            (command or PLAN.cmd_claim)(self.args)

    def pr_fixture(self, author=TEST_BOT):
        self.issue.update(pull_request={}, html_url="https://github.com/owner/repo/pull/42")
        self.issue["user"] = {"login": author}
        self.issue["body"] = "Bump dependencies" if author == "dependabot[bot]" else "Repair the tool"
        self.pulls = [{"number": 42, "state": "open", "user": {"login": author},
                       "title": self.issue["title"], "body": self.issue["body"],
                       "head": {"ref": "dependabot/python" if author == "dependabot[bot]" else "work/tool-repair",
                                "repo": {"full_name": "owner/repo"}},
                       "base": {"repo": {"full_name": "owner/repo"}}}]

    def test_dependabot_pr_can_be_claimed_without_a_tracking_issue(self):
        self.pr_fixture("dependabot[bot]")
        original = copy.deepcopy(self.issue)
        self.inventory["remote_branches"] = [self.pulls[0]["head"]["ref"]]
        self.run_claim()
        result = self.emitted.call_args.args[0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["record_kind"], "pr")
        self.assertEqual(self.issue, original)
        self.assertEqual(self.post_route, ("pr", 42, "owner/repo"))
        self.assertTrue(CLAIM.same_owner(CLAIM.records(self.comments[-1]["body"])[0], result["claim"]))
        self.run_claim()
        self.assertEqual(len(self.comments), 1)

    def test_bare_pr_can_be_claimed_and_keeps_its_description_and_labels(self):
        self.pr_fixture()
        original = copy.deepcopy(self.issue)
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])
        self.assertEqual(self.issue, original)
        self.assertEqual(self.post_route[0], "pr")

    def test_pr_competing_claim_and_source_worktree_refuse_before_writing(self):
        for competing in ("claim", "worktree"):
            with self.subTest(competing=competing):
                self.setUp()
                self.pr_fixture()
                if competing == "claim":
                    self.compete()
                else:
                    self.inventory["worktrees"] = [{"branch": self.pulls[0]["head"]["ref"], "path": "/source"}]
                with self.assertRaises(PLAN.ClassifiedPlanError):
                    self.run_claim()
                self.assert_no_writes()

    def test_pr_closing_during_claim_retains_recovery(self):
        self.pr_fixture()
        self.after_post = lambda: self.issue.update(state="closed")
        with self.assertRaises(PLAN.PlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.payload["claim_recovery"]["release_own_claim"]["comment_id"], 1)
        self.assertNotIn("status", self.events)
        self.assertNotIn("labels", self.events)

    def test_pr_with_canonical_issue_cannot_bypass_that_issues_claim(self):
        self.pr_fixture()
        self.pulls[0]["body"] = "Closes #99"
        self.targets["99"] = {**copy.deepcopy(self.issue), "number": 99}
        del self.targets["99"]["pull_request"]
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.code, "claim_issue_record_required")
        self.assert_no_writes()

    def test_pr_source_worktree_appearing_on_readback_stops_with_release_receipt(self):
        self.pr_fixture()
        self.after_post = lambda: self.inventory["worktrees"].append(
            {"branch": self.pulls[0]["head"]["ref"], "path": "/new-source"})
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.payload["claim_recovery"]["release_own_claim"]["comment_id"], 1)

    def test_pr_exact_author_release_allows_successor_and_foreign_release_does_not(self):
        for author in (TEST_BOT, "stranger"):
            with self.subTest(author=author):
                self.setUp()
                self.pr_fixture()
                self.comments = [
                    {"id": 1, "body": CLAIM.marker(OTHER), "user": {"login": TEST_BOT}},
                    {"id": 2, "body": "Released claim 1", "user": {"login": author}},
                ]
                self.args.resume_from = 1
                if author == TEST_BOT:
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])
                else:
                    with self.assertRaises(PLAN.PlanError):
                        self.run_claim()
                    self.assert_no_writes()

    def test_claim_accepts_pr_url_and_issue_parser_remains_issue_only(self):
        args = PLAN.build_parser().parse_args(["claim", "https://github.com/owner/repo/pull/42",
            "--worker", OWNER["worker"], "--session", OWNER["session"], "--branch", OWNER["branch"],
            "--next-action", "Repair"])
        self.assertEqual(PLAN.claim_reference(args).issue, "owner/repo#42")
        with self.assertRaises(PLAN.PlanError):
            PLAN.issue_ref(args.issue, "owner/repo")

    def test_pr_comment_wait_and_fork_require_supported_resolution(self):
        for mode in ("wait", "fork"):
            with self.subTest(mode=mode):
                self.setUp()
                self.pr_fixture()
                if mode == "wait":
                    self.wait = wait_record.reviewed(evidence="Recorded test request", waiting_for="Chris's test", ends_when="Acceptance recorded")
                else:
                    self.pulls[0]["head"]["repo"]["full_name"] = "stranger/repo"
                with self.assertRaises(PLAN.PlanError):
                    self.run_claim()
                self.assert_no_writes()
                if mode == "wait":
                    self.args.wait_resolved = "Chris recorded acceptance of this head on the issue."
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_pr_other_peer_on_owned_nonnumeric_branch_still_refuses(self):
        self.pr_fixture()
        self.args.branch = self.pulls[0]["head"]["ref"]
        self.comments = [{"id": 1, "body": CLAIM.marker({**OWNER, "branch": self.args.branch}), "user": {"login": TEST_BOT}}]
        self.inventory["worktrees"] = [{"branch": self.args.branch, "path": "/pr-source"}]
        self.inventory["sessions"] = [{"sessionId": "other-peer", "cwd": "/pr-source"}]
        with self.assertRaises(PLAN.ClassifiedPlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_pr_disappearing_from_open_inventory_keeps_readback_recovery(self):
        self.pr_fixture()
        self.after_post = lambda: self.pulls.clear()
        with self.assertRaises(PLAN.PlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.payload["claim_recovery"]["release_own_claim"]["comment_id"], 1)

    def compete(self, record=None):
        if record is None:
            record = OTHER
        self.comments.append({"id": len(self.comments) + 1, "body": "Claimed by " + record["worker"] + "\n" + CLAIM.marker(record)})

    def assert_no_writes(self):
        self.assertFalse(set(self.events).intersection({"post", "status", "labels"}))

    def refresh_fixture(self):
        source = {**OTHER, "branch": "work/issue-42-original"}
        self.args.refresh_pr = "https://github.com/owner/repo/pull/99"
        self.args.handoff_comment = 3
        self.args.resume_from = 1
        self.comments = [
            {"id": 1, "body": CLAIM.marker(source), "user": {"login": TEST_BOT}},
            {"id": 2, "body": "trial-b implementation split under claim 1, session session-b: work/issue-42-audits owns audits; work/issue-42-fixtures will own fixtures", "user": {"login": TEST_BOT}},
            {"id": 3, "body": "Released claim 1\n\nSource handoff: PR #99 and #100 are reviewed; work is finished.", "user": {"login": TEST_BOT}},
        ]
        self.pulls = [
            {"number": n, "state": "open", "user": {"login": TEST_BOT}, "title": "Repair", "body": "Refs #42", "head": {"ref": branch, "repo": {"full_name": "owner/repo"}},
             "base": {"repo": {"full_name": "owner/repo"}}}
            for n, branch in [(99, "work/issue-42-audits"), (100, "work/issue-42-fixtures")]
        ]
        for pull in self.pulls:
            n = pull["number"]
            self.targets[str(n)] = {**copy.deepcopy(self.issue), "number": n, "pull_request": {}, "body": "Refs #42"}
            self.target_comments[f"/repos/owner/repo/issues/{n}/comments"] = []
        self.inventory["local_branches"] = [p["head"]["ref"] for p in self.pulls]
        self.inventory["remote_branches"] = list(self.inventory["local_branches"])
        self.inventory["worktrees"] = [{"path": "/retained/" + p["head"]["ref"].split("/")[-1], "branch": p["head"]["ref"]} for p in self.pulls]

    def test_authorized_split_pr_refresh_claim_succeeds_with_release_and_handoff(self):
        self.refresh_fixture()
        self.run_claim()
        output = self.emitted.call_args.args[0]
        self.assertEqual(output["claim"]["refresh_pr"], self.args.refresh_pr)
        self.assertIn("Conflict-only refresh:", self.comments[-1]["body"])
        self.assertTrue(CLAIM.same_owner(CLAIM.records(self.issue["body"])[0], output["claim"]))

    def test_refresh_supports_original_branch_and_cross_repository_planning_issue(self):
        self.refresh_fixture()
        self.args.issue = "other/plans#42"
        self.args.planning_checkout = "/verified/plans"
        self.planning_inventory = {"worktrees": [], "local_branches": [], "remote_branches": [], "sessions": []}
        self.comments[2]["body"] = "Released claim 1\nHandoff: owner/repo#99 and https://github.com/owner/repo/pull/100"
        previous = self.pulls[0]["head"]["ref"]
        self.pulls[0]["head"]["ref"] = CLAIM.records(self.comments[0]["body"])[0]["branch"]
        for key in ("local_branches", "remote_branches"):
            self.inventory[key] = [self.pulls[0]["head"]["ref"] if b == previous else b for b in self.inventory[key]]
        self.inventory["worktrees"][0]["branch"] = self.pulls[0]["head"]["ref"]
        for pull in self.pulls:
            pull["body"] = "Refs other/plans#42"
        self.run_claim()
        self.assertEqual(self.emitted.call_args.args[0]["claim"]["refresh_pr"], self.args.refresh_pr)

    def test_refresh_cross_repository_bare_pr_number_is_not_evidence(self):
        self.refresh_fixture()
        self.args.issue = "other/plans#42"
        self.args.planning_checkout = "/verified/plans"
        self.planning_inventory = {"worktrees": [], "local_branches": [], "remote_branches": [], "sessions": []}
        for pull in self.pulls: pull["body"] = "Refs other/plans#42"
        with self.assertRaises(PLAN.PlanError): self.run_claim()
        self.assert_no_writes()

    def test_refresh_fork_cannot_hide_behind_retained_branch_name(self):
        self.refresh_fixture()
        self.pulls.append({**copy.deepcopy(self.pulls[0]), "number": 101})
        self.pulls[-1]["head"]["repo"]["full_name"] = "stranger/repo"
        self.targets["101"] = {**self.targets["99"], "number": 101}
        self.target_comments["/repos/owner/repo/issues/101/comments"] = []
        with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
        self.assert_no_writes()

    def cross_repository_fixture(self):
        self.refresh_fixture()
        self.args.issue = "other/plans#42"
        self.args.planning_checkout = "/verified/plans"
        self.planning_inventory = {"worktrees": [], "local_branches": [], "remote_branches": [], "sessions": []}
        self.comments[2]["body"] = "Released claim 1\nHandoff: owner/repo#99 and owner/repo#100"
        for pull in self.pulls: pull["body"] = "Refs other/plans#42"

    def test_cross_repository_pr_must_link_qualified_canonical_issue(self):
        self.cross_repository_fixture()
        self.pulls[0]["body"] = "Fixes #42"
        with self.assertRaises(PLAN.PlanError): self.run_claim()
        self.assert_no_writes()

    def test_cross_repository_target_local_issue_artifacts_are_unrelated(self):
        self.cross_repository_fixture()
        self.inventory["local_branches"].append("work/unrelated-issue-42")
        self.pulls.append({"number": 101, "body": "Fixes #42", "head": {"ref": "work/unrelated-issue-42"}})
        self.run_claim()
        self.assertEqual(self.post_route, ("issue", 42, "other/plans"))

    def test_cross_repository_planning_inventory_preserves_actual_owner(self):
        self.cross_repository_fixture()
        self.planning_inventory["worktrees"] = [{"branch": "work/other-42", "path": "/planning/issue-42"}]
        with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
        self.assert_no_writes()

    def test_refresh_merged_sibling_remains_retained_without_cleanup(self):
        self.refresh_fixture()
        sibling = self.pulls.pop()
        sibling.update(state="closed", merged_at="2026-10-02T01:00:00Z")
        self.closed_pulls[sibling["number"]] = sibling
        self.targets[str(sibling["number"])]["state"] = "closed"
        self.run_claim()
        self.assertIn(sibling["head"]["ref"], self.inventory["local_branches"])

    def test_refresh_closed_unmerged_sibling_still_refuses_retained_artifacts(self):
        self.refresh_fixture()
        sibling = self.pulls.pop()
        sibling.update(state="closed", merged_at=None)
        self.closed_pulls[sibling["number"]] = sibling
        self.targets[str(sibling["number"])]["state"] = "closed"
        with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
        self.assert_no_writes()

    def test_refresh_case_variant_recovers_same_scope(self):
        self.refresh_fixture()
        self.args.refresh_pr = "https://github.com/Owner/Repo/pull/99"
        self.run_claim()
        self.args.refresh_pr = self.args.refresh_pr.lower()
        self.run_claim()
        self.assertEqual(self.events.count("post"), 1)

    def test_generic_bot_rollup_cannot_become_session_handoff(self):
        self.refresh_fixture()
        self.comments.append({"id": 4, "body": "Status: claim 1 covered PR #99 and #100", "user": {"login": TEST_BOT}})
        self.args.handoff_comment = 4
        with self.assertRaises(PLAN.PlanError): self.run_claim()
        self.assert_no_writes()

    def test_standalone_source_session_handoff_requires_explicit_provenance(self):
        self.refresh_fixture()
        self.comments.append({"id": 4, "body": "Handoff from trial-b\nSource claim 1; Session: session-b; PR #99 and #100", "user": {"login": TEST_BOT}})
        self.args.handoff_comment = 4
        self.run_claim()

    def historical_direction_fixture(self, stage, *, refresh):
        fixture = json.loads(Path(__file__).with_name("fixtures").joinpath(
            f"direction-43-{stage}-handoff.json").read_text())
        self.args.repo = fixture["repo"]
        self.args.issue = str(fixture["issue"])
        self.args.branch = "work/cs-1469-replay"
        self.comments = [{**comment, "user": {"login": comment["author"]}}
                         for comment in fixture["comments"]]
        source, release, handoff = self.comments
        branch = CLAIM.records(source["body"])[0]["branch"]
        number = 44 if stage == "d2" else 46
        retained_branch = "work/dir-43-d1" if stage == "d2" else branch
        self.args.resume_from = source["id"]
        self.args.handoff_comment = handoff["id"]
        self.args.refresh_pr = f"https://github.com/{fixture['repo']}/pull/{number}" if refresh else None
        self.pulls = [{"number": number, "state": "open", "user": source["user"],
                       "title": "Direction proposal", "body": "Refs #43",
                       "head": {"ref": retained_branch, "repo": {"full_name": fixture["repo"]}},
                       "base": {"repo": {"full_name": fixture["repo"]}}}]
        self.targets[str(number)] = {**copy.deepcopy(self.issue), "state": "open",
                                     "pull_request": {}, "body": "Refs #43"}
        self.target_comments[f"/repos/{fixture['repo']}/issues/{number}/comments"] = []
        self.inventory["local_branches"] = [branch, retained_branch]
        self.inventory["remote_branches"] = [retained_branch]
        self.inventory["worktrees"] = [{"branch": retained_branch, "path": f"/retained/{stage}"}]

    def test_verbatim_direction_handoffs_accept_successor_and_released_source_pr_refresh(self):
        for stage, modes in (("d2", (False, True)), ("d4", (True,))):
            for refresh in modes:
                with self.subTest(stage=stage, refresh=refresh):
                    self.setUp()
                    self.historical_direction_fixture(stage, refresh=refresh)
                    self.run_claim()
                    result = self.emitted.call_args.args[0]
                    self.assertTrue(result["ok"])
                    self.assertIn("metadata_readback", result["completed_steps"])
                    self.assertEqual(result["claim"].get("refresh_pr"), self.args.refresh_pr)

    def test_real_direction_refresh_preserves_release_pr_and_live_ownership_guards(self):
        for change in ("plain", "missing_release", "conditional_release", "foreign_release",
                       "unnamed_pr", "foreign_pr", "live_peer", "other_claim", "other_pr", "different_branch", "foreign_handoff", "before_release", "named_split", "malformed_header"):
            with self.subTest(change=change):
                self.setUp()
                self.historical_direction_fixture("d4", refresh=True)
                source, release, handoff = self.comments
                if change == "plain":
                    self.args.resume_from = self.args.handoff_comment = self.args.refresh_pr = None
                if change in ("missing_release", "conditional_release"):
                    directive = f"Released claim {source['id']}"
                    replacement = "Source work complete" if change == "missing_release" else directive + " once PR #46 merges"
                    release["body"] = release["body"].replace(directive, replacement)
                if change == "foreign_release":
                    release["user"] = {"login": "stranger"}
                if change == "unnamed_pr":
                    handoff["body"] = handoff["body"].replace("direction#46", "direction#47").replace("/pull/46", "/pull/47")
                if change == "foreign_pr": self.pulls[0]["user"] = {"login": "stranger"}
                if change == "foreign_handoff": handoff["user"] = {"login": "stranger"}
                if change == "different_branch": self.pulls[0]["head"]["ref"] = "work/another-source"
                if change == "live_peer":
                    self.inventory["sessions"] = [{"sessionId": "live-source", "cwd": "/retained/d4"}]
                if change == "other_claim": self.compete({**OTHER, "session": "other-live"})
                if change == "other_pr":
                    self.pulls.append({**copy.deepcopy(self.pulls[0]), "number": 47})
                if change == "before_release": self.comments[:] = [source, handoff, release]
                if change == "named_split":
                    sibling: dict[str, Any] = {**copy.deepcopy(self.pulls[0]), "number": 47}
                    sibling["head"]["ref"] = "work/split-43"
                    self.pulls.append(sibling)
                    handoff["body"] += "\nAlso retain direction#47."
                if change == "malformed_header": handoff["body"] = "Handoff from: codex-DIR-43-D4\n\n" + handoff["body"]
                with self.assertRaises(PLAN.PlanError) as caught: self.run_claim()
                expected = {
                    "unnamed_pr": "identify the exact released source claim",
                    "different_branch": "identify the exact released source claim",
                    "malformed_header": "identify the exact released source claim",
                    "foreign_handoff": "comment by the source claim author",
                    "foreign_pr": "attested in the released handoff",
                    "before_release": "release before or in the handoff",
                }.get(change, "Another worker or ambiguous ownership")
                self.assertIn(expected, str(caught.exception))
                self.assert_no_writes()

    def test_unmodified_d4_handoff_is_only_a_source_pr_refresh(self):
        self.historical_direction_fixture("d4", refresh=False)
        with self.assertRaisesRegex(PLAN.PlanError, "identify the exact released source claim"):
            self.run_claim()
        self.assert_no_writes()

    def test_source_pr_refresh_binds_record_without_inventing_session_prose(self):
        # The historical launchplane#2693 unbound opening remains unusable
        # for split handoffs. Source-branch PR refresh binds through the source
        # record instead; the caller still verifies the real source session.
        self.historical_direction_fixture("d4", refresh=True)
        self.comments[-1]["body"] = "LP-2693-D1 handoff: PR #46; source work finished."
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_malformed_source_refresh_headers_cannot_bypass_identity_proof(self):
        for header in ("Handoff from: codex-DIR-43-D4", "handoff from codex-DIR-43-D4",
                       "  Handoff from codex-DIR-43-D4"):
            with self.subTest(header=header):
                self.setUp()
                self.historical_direction_fixture("d4", refresh=True)
                self.comments[-1]["body"] = header + "\n\n" + self.comments[-1]["body"]
                with self.assertRaisesRegex(PLAN.PlanError, "identify the exact released source claim"):
                    self.run_claim()
                self.assert_no_writes()

    def test_release_directive_is_exact_and_independent_of_handoff_narrative(self):
        for final in (False, True):
            with self.subTest(final=final):
                self.setUp()
                narrative = ("Their lock names the supported dev-worktree retire command for eventual retirement "
                             "after merged/closed disposition and ownership/content checks.\n\n"
                             "PR #99 must merge first. Then the next worker may claim.")
                release = narrative + "\n\nReleased claim 1" if final else "Released claim 1\n\n" + narrative
                self.released_status_fixture(release)
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_conditional_or_missing_release_directive_refuses_without_writes(self):
        for directive in ("Released claim 1 once CI passes", "Released claim 1. Takes effect upon merge.",
                          "Released claim 1 if PR #99 merges", "Source claim 1 is released"):
            for final in (False, True):
                with self.subTest(directive=directive, final=final):
                    self.setUp()
                    release = "Source work finished.\n\n" + directive if final else directive + "\nSource work finished."
                    self.released_status_fixture(release)
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                    self.assertEqual(caught.exception.code, "claim_conflict")
                    self.assert_no_writes()

    def direction_handoff_fixture(self, *, spelling="claim:", refresh=False):
        self.refresh_fixture()
        source = {**CLAIM.records(self.comments[0]["body"])[0],
                  "worker": "codex-DIR-43-D2", "session": "01a11bcc-2c5e-7872-9ff9-428d4f244d98"}
        self.comments[0].update(id=6061486703, body=CLAIM.marker(source))
        self.comments[2]["body"] = "Released claim 6061486703"
        self.args.resume_from = 6061486703
        if not refresh:
            self.args.refresh_pr = None
        # direction#43 comment 6061912802's exact opening and source line.
        self.comments.append({"id": 6061912802, "user": {"login": TEST_BOT}, "body": (
            "Handoff from codex-DIR-43-D2\n\n"
            f"Source {spelling} 6061486703; native session: 01a11bcc-2c5e-7872-9ff9-428d4f244d98. "
            "The executing claim is released. **Q85 revision preparation is complete**:\n\n"
            "- PR #99: current-head CI passed.\n- PR #100: current-head CI passed.\n\n"
            "**Next:** Supervisor brings the pair back to Chris for his own direction approval. "
            "No PR labels, merges or train operations occurred."
        )})
        self.args.handoff_comment = 6061912802

    def test_direction_source_claim_spellings_allow_released_handoff(self):
        for refresh in (False, True):
            for spelling in ("claim:", "claim"):
                with self.subTest(refresh=refresh, spelling=spelling):
                    self.setUp()
                    self.direction_handoff_fixture(spelling=spelling, refresh=refresh)
                    self.run_claim()
                    output = self.emitted.call_args.args[0]
                    self.assertTrue(output["ok"])
                    self.assertEqual(self.events.count("post"), 1)
                    if refresh:
                        self.assertEqual(output["claim"]["refresh_pr"], self.args.refresh_pr)
                    else:
                        self.assertTrue(output["claim"]["retained_handoff"].endswith("#issuecomment-6061912802"))

    def test_direction_source_claim_colon_preserves_provenance_and_release_guards(self):
        for refresh in (False, True):
            for change in ("wrong_id", "id_prefix", "no_release", "foreign_author", "wrong_session"):
                with self.subTest(refresh=refresh, change=change):
                    self.setUp()
                    self.direction_handoff_fixture(refresh=refresh)
                    handoff = self.comments[-1]
                    if change == "wrong_id":
                        handoff["body"] = handoff["body"].replace("claim: 6061486703", "claim: 6061486704")
                    if change == "id_prefix":
                        handoff["body"] = handoff["body"].replace("claim: 6061486703", "claim: 60614867030")
                    if change == "no_release":
                        self.comments[2]["body"] = "Source preparation complete."
                    if change == "foreign_author":
                        handoff["user"]["login"] = "stranger"
                    if change == "wrong_session":
                        handoff["body"] = handoff["body"].replace("01a11bcc-2c5e-7872-9ff9-428d4f244d98", "different-session")
                    with self.assertRaises(PLAN.PlanError):
                        self.run_claim()
                    self.assert_no_writes()

    def separate_handoff_fixture(self, prose, *, refresh=True):
        self.refresh_fixture()
        if not refresh:
            self.args.refresh_pr = None
        self.comments.append({
            "id": 4, "body": "Handoff from trial-b\nSource claim 1; Session: session-b; PR #99 and #100\n\n" + prose,
            "user": {"login": TEST_BOT},
        })
        self.args.handoff_comment = 4

    def test_separate_unconditional_handoff_keeps_downstream_gates_usable(self):
        for prose in (
            "PR #99 merged this afternoon. Then the next worker may claim.",
            "Source work landed. Then the next worker may claim. Follow-up PR #100 merges tomorrow.",
            "Ownership transfers immediately.",
            "Supervisor routes PR #99 after PR #100 merges. Keep the worktree until landing.",
            "After PR #99 lands, close out the issue. When resuming, rebase onto main.",
            "Fix is effective across repos.",
        ):
            for refresh in (False, True):
                with self.subTest(prose=prose, refresh=refresh):
                    self.setUp()
                    self.separate_handoff_fixture(prose, refresh=refresh)
                    before = copy.deepcopy((self.pulls, self.inventory))
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])
                    self.assertEqual(before, (self.pulls, self.inventory))

    def test_handoff_claim_identity_does_not_defer_unconditional_pr_notes(self):
        for body in (
            "Released claim 1\nHandoff: PR #99 and #100, both rebased after #98 merged.",
            "Handoff from trial-b\nSource claim 1, session session-b: PR #99 and #100 pending review",
        ):
            for refresh in (False, True):
                with self.subTest(body=body, refresh=refresh):
                    self.setUp()
                    self.separate_handoff_fixture("", refresh=refresh)
                    self.comments[3]["body"] = body
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_cross_repository_uses_canonical_planning_label_configuration(self):
        self.cross_repository_fixture()
        self.configs["other/plans"] = copy.deepcopy(PLAN.DEFAULT_CONFIG)
        self.configs["other/plans"]["labels"]["active"] = "custom-active"
        self.run_claim()
        self.assertIn(self.configs["other/plans"]["labels"]["active"], PLAN.normalize_labels(self.issue["labels"]))

    def test_retained_sibling_metadata_denial_stops_without_writing(self):
        self.refresh_fixture()
        self.pulls.pop()
        self.api_failure = {"status": 403}
        with self.assertRaises(PLAN.PlanError): self.run_claim()
        self.assert_no_writes()

    def test_refresh_preserves_target_recorded_wait(self):
        self.refresh_fixture()
        previous = PLAN.section_map(self.issue["body"])["Current Status"]
        self.target_comments["/repos/owner/repo/issues/99/comments"].append({"id": 88, "user": {"login": TEST_BOT}, "body": wait_record.marker(wait_record.reviewed(evidence="Client asked to hold", waiting_for="Client test", ends_when="Accepted head"))})
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
        self.assertEqual(caught.exception.code, "claim_wait_unresolved")
        self.assertEqual(caught.exception.payload["previous_current_status"], previous)
        self.assert_no_writes()
        self.args.wait_resolved = "Existing conflict-refresh authorization resolves this wait for source work only."
        self.run_claim()

    def test_planning_checkout_inventory_reads_and_verifies_exact_git_identity(self):
        planning_path = Path("/verified/plans")
        def git_read(argv, *, cwd=None):
            self.assertEqual(cwd, planning_path)
            return "https://github.com/other/plans.git" if argv[1] == "remote" else ""
        with patch.object(CLAIM, "run_read", side_effect=git_read), patch.object(CLAIM.shutil, "which", return_value=None):
            inventory = CLAIM.local_inventory("other/plans", 42, cwd=planning_path)
        self.assertEqual(inventory["issue"], 42)
        with patch.object(CLAIM, "run_read", return_value="https://github.com/wrong/repo.git"):
            with self.assertRaises(ValueError): CLAIM.local_inventory("other/plans", 42, cwd=planning_path)

    def test_mixed_case_issue_url_remains_competing_ownership(self):
        self.pulls = [{"number": 101, "body": "Fixes https://github.com/Owner/Repo/issues/42", "head": {"ref": "work/unrelated"}}]
        with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
        self.assert_no_writes()

    def test_repo_configuration_identity_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / ".github" / "github.json"
            config.parent.mkdir()
            config.write_text('{"planning":{"labels":{"active":"custom-active"}}}')
            with patch.object(PLAN, "git_root", return_value=root), patch.object(PLAN, "repo_from_git", return_value="Owner/Some-Repo"), \
                    patch.object(PLAN, "workspace_config_path", return_value=root / "missing-workspace-config"):
                resolved = PLAN.load_config("owner/some-repo")
                self.assertEqual(PLAN.repo_config_path("owner/some-repo"), config)
            self.assertEqual(resolved["labels"]["active"], "custom-active")

    def test_closed_superseded_pr_without_artifacts_does_not_hold_refresh(self):
        self.refresh_fixture()
        self.comments[-1]["body"] += " Closed #98 in favor of #99."
        old = {**copy.deepcopy(self.pulls[0]), "number": 98, "state": "closed", "merged_at": None,
               "head": {"ref": "work/old", "repo": {"full_name": "owner/repo"}}}
        self.closed_pulls[98] = old
        self.targets["98"] = {**self.targets["99"], "number": 98, "state": "closed"}
        self.target_comments["/repos/owner/repo/issues/98/comments"] = []
        self.run_claim()
        self.target_comments["/repos/owner/repo/issues/98/comments"].append({"id": 40, "body": CLAIM.marker(OTHER)})
        with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()

    def test_closed_superseded_pr_still_preserves_unaccounted_artifacts(self):
        self.refresh_fixture()
        self.comments[-1]["body"] += " Closed #98 in favor of #99."
        old = {**copy.deepcopy(self.pulls[0]), "number": 98, "state": "closed", "merged_at": None,
               "head": {"ref": "work/old", "repo": {"full_name": "owner/repo"}}}
        self.closed_pulls[98] = old
        self.targets["98"] = {**self.targets["99"], "number": 98, "state": "closed"}
        self.target_comments["/repos/owner/repo/issues/98/comments"] = []
        self.inventory["local_branches"].append(old["head"]["ref"])
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
        self.assertEqual(caught.exception.payload["competing_evidence"][0]["source"], "closed_pr_artifacts")
        self.assert_no_writes()

    def test_five_launchplane_finished_handoffs_allow_successor_claims(self):
        cases = json.loads(Path(__file__).with_name("fixtures").joinpath("launchplane-3109-ended-ownership.json").read_text())
        for case in cases:
            with self.subTest(case=case["case"]):
                self.setUp()
                historical = copy.deepcopy(case["comments"])
                record = CLAIM.records(historical[0]["body"])[0]
                self.args.issue, self.args.branch = "3109", "work/successor-3109"
                self.issue["number"] = 3109
                self.comments = historical
                self.issue["body"] += "\nSession: " + record["session"] + " (claim released at handoff).\n" + CLAIM.marker(record)
                self.inventory["local_branches"] = [record["branch"]]
                self.inventory["remote_branches"] = [record["branch"]]
                self.inventory["worktrees"] = [{"branch": record["branch"], "path": "/retained/" + record["branch"].split("/")[-1]}]
                if case["closed_pr"]:
                    # Replay the older closed PR alongside a separately handed-off open PR.
                    self.refresh_fixture()
                    self.comments.extend(historical)
                    self.comments[2]["body"] += "\nClosed #3110 is retained research input."
                    self.pulls[0]["body"] = self.pulls[1]["body"] = "Refs #3109"
                    old = {**copy.deepcopy(self.pulls[0]), "number": 3110, "state": "closed", "merged_at": None,
                           "head": {"ref": record["branch"], "repo": {"full_name": "owner/repo"}}}
                    # Authorship stays bound to the actual historical release.
                    old["user"]["login"] = historical[0]["user"]["login"]
                    self.closed_pulls[3110] = old
                    self.targets["3110"] = {**self.targets["99"], "number": 3110, "state": "closed"}
                    self.inventory["local_branches"].append(record["branch"])
                    self.inventory["remote_branches"].append(record["branch"])
                    self.inventory["worktrees"].append({"branch": record["branch"], "path": "/retained/lp-3109-d1"})
                self.run_claim()
                result = self.emitted.call_args.args[0]
                self.assertTrue(result["ok"])
                retained = next(r for r in result["retained_artifacts"] if r["claim_id"] == historical[0]["id"])
                self.assertEqual(retained["branch"], record["branch"])
                self.assertTrue(retained["worktrees"])

    def test_historical_leftovers_also_allow_independent_plain_claims(self):
        cases = json.loads(Path(__file__).with_name("fixtures").joinpath("launchplane-3109-ended-ownership.json").read_text())
        for case in cases:
            with self.subTest(case=case["case"]):
                self.setUp()
                self.args.issue = "3109"
                self.args.branch = "work/" + case["case"].lower()
                self.issue["number"] = 3109
                self.comments = copy.deepcopy(case["comments"])
                source = CLAIM.records(self.comments[0]["body"])[0]
                path = "/retained/" + source["branch"].split("/")[-1]
                self.inventory["local_branches"] = [source["branch"]]
                self.inventory["remote_branches"] = [source["branch"]]
                self.inventory["worktrees"] = [{"branch": source["branch"], "path": path}]
                self.run_claim()
                result = self.emitted.call_args.args[0]
                self.assertTrue(result["ok"])
                self.assertNotIn("refresh_pr", result["claim"])
                self.assertNotIn("resume_from", result["claim"])
                self.assertEqual(result["retained_artifacts"][0]["worktrees"], [path])
                self.assertEqual(result["retained_artifacts"][0]["branch"], source["branch"])

    def ended_holder_fixture(self, *, released=True):
        self.comments = [
            {"id": 71, "body": CLAIM.marker(OTHER), "user": {"login": TEST_BOT}, "created_at": OTHER["claimed_at"]},
            {"id": 72, "body": "Released claim 71\n\nRetire the retained worktree after acceptance and ownership checks.",
             "user": {"login": TEST_BOT}, "created_at": "2026-10-01T01:00:00Z"},
        ][:2 if released else 1]
        self.inventory["worktrees"] = [{"branch": OTHER["branch"], "path": "/retained/other-42"}]
        self.inventory["local_branches"] = self.inventory["remote_branches"] = [OTHER["branch"]]

    def test_native_finished_handoff_still_requires_an_exact_release(self):
        self.ended_holder_fixture(released=False)
        self.inventory["session_states"] = {OTHER["session"]: {"state": "ended", "ended_at": "2026-10-01T02:00:00Z"}}
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.code, "claim_conflict")
        self.assert_no_writes()

    def test_idle_loaded_released_source_can_be_claimed_and_cannot_resume_after_handoff(self):
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_closure
        self.ended_holder_fixture()
        self.observe_inventory = CLAIM.observe_sessions
        self.args.resume_from = 71
        with ReceiptFixture(OTHER["session"]) as fixture:
            native = ["codex", "--remote", "unix://" + str(github_plan_closure.daemon_endpoint(fixture.home)), "--", "unrelated task"]
            from datetime import datetime, timezone
            born = datetime(2026, 10, 1, tzinfo=timezone.utc)
            with patch.object(github_plan_closure, "process_started_at", return_value=born):
                fixture.managed(native)
                fixture.commands = "456 " + " ".join(native)
                self.run_claim()
            self.assertTrue(self.emitted.call_args.args[0]["ok"])
            self.assertTrue(self.emitted.call_args.args[0]["retained_artifacts"])
            with self.assertRaises(ValueError):
                with github_plan_closure.resume_guard(OTHER["session"]): pass

    def test_idle_loaded_source_with_unidentified_client_still_refuses_claim(self):
        from test_github_plan_sessions import ReceiptFixture
        self.ended_holder_fixture()
        self.observe_inventory = CLAIM.observe_sessions
        with ReceiptFixture(OTHER["session"]) as fixture:
            fixture.commands = "codex --remote unix:///unobserved"
            with self.assertRaises(PLAN.ClassifiedPlanError):
                self.run_claim()
        self.assert_no_writes()

    def test_live_or_unknown_holder_returns_coordination_before_any_write(self):
        for state in ("unknown", "live", "released_live", "released_other_peer"):
            with self.subTest(state=state):
                self.setUp()
                self.ended_holder_fixture(released=state.startswith("released"))
                if state in {"live", "released_live"}:
                    self.inventory["session_states"] = {OTHER["session"]: {"state": "live"}}
                    self.inventory["sessions"] = [{"sessionId": OTHER["session"], "cwd": "/retained/other-42", "source": "codex_session"}]
                if state == "released_other_peer":
                    self.inventory["sessions"] = [{"sessionId": "another-native-session", "cwd": "/retained/other-42/subdir"}]
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assert_no_writes()
                holder = caught.exception.payload["coordination"][0]
                self.assertEqual(holder["session"], "another-native-session" if state == "released_other_peer" else OTHER["session"])
                self.assertEqual(holder["branch"], OTHER["branch"])
                self.assertTrue(holder.get("claim_url") or holder.get("work_record"))

    def test_ended_artifact_branch_cannot_be_reused_or_hide_a_new_claim(self):
        for change in ("same_branch", "new_claim", "late_native_evidence"):
            with self.subTest(change=change):
                self.setUp()
                self.ended_holder_fixture(released=change != "late_native_evidence")
                if change == "same_branch": self.args.branch = OTHER["branch"]
                if change == "new_claim": self.compete({**OTHER, "session": "new-session"})
                if change == "late_native_evidence":
                    self.comments[0]["updated_at"] = "2026-10-01T03:00:00Z"
                    self.inventory["session_states"] = {OTHER["session"]: {"state": "ended", "ended_at": "2026-10-01T02:00:00Z"}}
                with self.assertRaises(PLAN.PlanError): self.run_claim()
                self.assert_no_writes()

    def test_resumed_native_holder_refuses_during_claim_readback(self):
        self.ended_holder_fixture()
        self.after_post = lambda: self.inventory["sessions"].append(
            {"sessionId": OTHER["session"], "cwd": "/retained/other-42", "source": "codex_session"})
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
        self.assertIn("post", self.events)
        self.assertNotIn("status", self.events)
        self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])

    def test_incomplete_observed_native_peer_scan_preserves_retained_ownership(self):
        self.ended_holder_fixture()
        self.inventory["session_coverage"]["codex_claims"] = {"status": "incomplete", "observed_source_incomplete": True}
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
        self.assert_no_writes()
        self.assertEqual(caught.exception.payload["competing_evidence"][0]["source"], "native_session_inventory")

    def test_deleted_historical_branch_can_be_reused_without_reporting_phantom_artifacts(self):
        self.ended_holder_fixture()
        self.inventory.update(worktrees=[], local_branches=[], remote_branches=[])
        self.args.branch = OTHER["branch"]
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])
        self.assertEqual(self.emitted.call_args.args[0]["retained_artifacts"], [])

    def test_unread_existing_native_source_reaches_the_released_artifact_guard(self):
        import github_plan_sessions
        self.ended_holder_fixture()
        self.observe_inventory = CLAIM.observe_sessions
        with patch.object(github_plan_sessions, "observe", return_value=([], {}, {
                "status": "incomplete", "observed_source_incomplete": True})):
            with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
        self.assert_no_writes()

    def test_handoff_sees_native_holder_recorded_only_on_the_retained_pr(self):
        self.refresh_fixture()
        target = {**OTHER, "session": "target-native-session", "branch": "work/pr-only-99"}
        self.target_comments["/repos/owner/repo/issues/99/comments"] = [
            {"id": 77, "body": CLAIM.marker(target), "user": {"login": TEST_BOT}, "created_at": OTHER["claimed_at"]},
            {"id": 78, "body": "Released claim 77", "user": {"login": TEST_BOT}, "created_at": "2026-10-01T01:00:00Z"},
        ]
        def observe(inventory, comments):
            if any(r["session"] == target["session"] for c in comments for r in CLAIM.records(c.get("body") or "")):
                inventory["sessions"].append({"sessionId": target["session"], "cwd": "/separate/primary", "source": "codex_session"})
            return inventory
        self.observe_inventory = observe
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
        self.assertEqual(caught.exception.payload["coordination"][0]["claim_url"],
                         "https://github.com/owner/repo/issues/99#issuecomment-77")
        self.assert_no_writes()
        self.assertEqual(caught.exception.payload["competing_evidence"][0]["session"], target["session"])

    def test_incomplete_handoff_inventory_names_the_retained_branch_and_source(self):
        self.refresh_fixture()
        self.inventory["session_coverage"]["codex_claims"] = {"observed_source_incomplete": True}
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
        self.assert_no_writes()
        evidence = caught.exception.payload["competing_evidence"][0]
        self.assertIn(evidence["branch"], self.inventory["local_branches"])
        self.assertEqual(evidence["session"], OTHER["session"])

    def test_handoff_with_deleted_retained_branches_ignores_unrelated_incomplete_source(self):
        self.refresh_fixture()
        self.args.refresh_pr = None
        self.inventory.update(worktrees=[], local_branches=[], remote_branches=[])
        self.inventory["session_coverage"]["codex_claims"] = {"observed_source_incomplete": True}
        for pull in self.pulls:
            pull.update(state="closed", merged_at="2026-10-02T01:00:00Z")
            self.targets[str(pull["number"])]["state"] = "closed"
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_non_listening_native_source_does_not_revive_released_artifact_ownership(self):
        import github_plan_sessions
        self.ended_holder_fixture()
        self.observe_inventory = CLAIM.observe_sessions
        with patch.object(github_plan_sessions, "observe", return_value=([], {}, {
                "status": "unavailable", "observed_source_incomplete": False})):
            self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])
        self.assertTrue(self.emitted.call_args.args[0]["retained_artifacts"])

    def test_final_readback_coordination_names_the_new_holder(self):
        self.after_status = self.compete
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
        holder = caught.exception.payload["coordination"][0]
        self.assertEqual(holder["session"], OTHER["session"])
        self.assertEqual(holder["worker"], OTHER["worker"])

    def test_explicit_planning_checkout_supplies_custom_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / ".github" / "github.json"
            config.parent.mkdir()
            source = {"planning": {"labels": {"active": "custom-active"}}}
            config.write_text(json.dumps(source))
            with patch.object(PLAN, "git_root", return_value=None), patch.object(PLAN, "repo_from_git", side_effect=lambda p: "other/plans" if p == root else None), \
                    patch.object(PLAN, "workspace_config_path", return_value=root / "missing-workspace-config"):
                resolved = PLAN.load_config("other/plans", checkout=root)
            self.assertEqual(resolved["labels"]["active"], source["planning"]["labels"]["active"])

    def test_refresh_requires_own_new_task_branch(self):
        for branch in ("work/issue-42-original", "work/issue-42-audits", "work/issue-42-fixtures"):
            with self.subTest(branch=branch):
                self.setUp()
                self.refresh_fixture()
                self.args.branch = branch
                with self.assertRaises(PLAN.PlanError): self.run_claim()
                self.assert_no_writes()

    def test_new_pr_on_released_source_branch_is_not_retained_ownership(self):
        for author in (TEST_BOT, "stranger"):
            with self.subTest(author=author):
                self.setUp()
                self.refresh_fixture()
                self.pulls.append({**copy.deepcopy(self.pulls[0]), "number": 101,
                                   "user": {"login": author},
                                   "head": {"ref": "work/issue-42-original", "repo": {"full_name": "owner/repo"}}})
                self.targets["101"] = {**self.targets["99"], "number": 101}
                self.target_comments["/repos/owner/repo/issues/101/comments"] = []
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                self.assert_no_writes()

    def test_plain_resume_still_refuses_released_split_pr_artifacts(self):
        self.refresh_fixture()
        self.args.refresh_pr = self.args.handoff_comment = None
        with self.assertRaises(PLAN.ClassifiedPlanError):
            self.run_claim()
        self.assert_no_writes()

    def ordinary_handoff_fixture(self):
        self.refresh_fixture()
        self.args.refresh_pr = None
        self.args.next_action = "Perform the separately authorized prerequisite before any PR refresh"
        source_branch = CLAIM.records(self.comments[0]["body"])[0]["branch"]
        sibling = self.pulls.pop()
        old_branch = sibling["head"]["ref"]
        sibling["head"]["ref"] = source_branch
        sibling.update(state="closed", merged_at="2026-10-02T01:00:00Z")
        self.closed_pulls[sibling["number"]] = sibling
        self.targets[str(sibling["number"])]["state"] = "closed"
        for key in ("local_branches", "remote_branches"):
            self.inventory[key] = [source_branch if b == old_branch else b for b in self.inventory[key]]
        self.inventory["worktrees"][1]["branch"] = source_branch
        self.pulls[0]["draft"] = True

    def test_ordinary_successor_accepts_two_retained_branches_before_pr_refresh(self):
        self.ordinary_handoff_fixture()
        before = copy.deepcopy((self.inventory, self.pulls, self.closed_pulls))
        self.run_claim()
        output = self.emitted.call_args.args[0]
        self.assertNotIn("refresh_pr", output["claim"])
        self.assertEqual(output["claim"]["resume_from"], str(self.args.resume_from))
        self.assertTrue(output["claim"]["retained_handoff"].endswith(f"#issuecomment-{self.args.handoff_comment}"))
        self.assertIn(self.args.next_action, self.comments[-1]["body"])
        self.assertNotIn("Conflict-only refresh:", self.comments[-1]["body"])
        self.assertEqual(before, (self.inventory, self.pulls, self.closed_pulls))
        self.assertEqual(output["session_coverage"]["codex"]["status"], "unavailable")

    def no_pr_resume_fixture(self):
        self.ordinary_handoff_fixture()
        source = CLAIM.records(self.comments[0]["body"])[0]
        self.pulls, self.closed_pulls = [], {}
        self.inventory.update(local_branches=[source["branch"]], remote_branches=[source["branch"]],
                              worktrees=[{"branch": source["branch"], "path": "/retained"}])
        self.comments[-1]["body"] = (
            f"Released claim {self.args.resume_from}\n\n"
            f"Preserved pushed WIP on `{source['branch']}`. No PR opened.")
        self.comments[0]["created_at"] = source["claimed_at"]
        self.comments[-1]["created_at"] = "2026-10-02T01:00:00Z"
        return source

    def released_native_fixture(self, fixture, *, worker_release=False):
        """Real resume metadata and canonical release producer, without launch ownership."""
        from datetime import datetime, timezone
        import github_plan_closure as closure
        import github_plan_release as release
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 1, 1, 1, tzinfo=timezone.utc)
        closure.replace_rows([])
        metadata = {key: fixture.entry[key] for key in (
            "codex_home", "launch_pid", "launch_generation", "launch_started_at")}
        with patch.object(closure, "datetime", Clock):
            with closure.resume_guard(fixture.session, method="thread/resume", home=fixture.home):
                closure.resumed_launch(fixture.session, metadata, str(fixture.transcript), fixture.entry["tty"])
        fixture.native_state = "notLoaded"
        source = self.no_pr_resume_fixture()
        source["session"] = fixture.session
        original = {"id": 1, "body": CLAIM.marker(source), "user": {"login": TEST_BOT},
                    "created_at": source["claimed_at"], "updated_at": source["claimed_at"]}
        evidence = {"id": 88, "user": {"login": TEST_BOT}, "created_at": "2026-10-01T02:02:00Z",
                    "updated_at": "2026-10-01T02:02:00Z",
                    "issue_url": "https://api.github.com/repos/owner/control/issues/884",
                    "body": f"Closed session {fixture.session}\nEnded at: 2026-10-01T02:01:00Z\nSafe to exit: yes\n"}
        body = release.prepare_release(original, evidence, [original], self.inventory,
            actor=TEST_BOT, role="supervisor", releaser_session="supervisor-native",
            evidence_url="https://github.com/owner/control/issues/884#issuecomment-88", retained_prs=[], related=[])
        self.comments = [original]
        if worker_release:
            self.comments.append({"id": 2, "body": "Released claim 1", "user": {"login": TEST_BOT},
                                  "created_at": "2026-10-01T02:00:30Z"})
        self.comments.append({"id": 3, "body": body, "user": {"login": TEST_BOT},
                              "created_at": "2026-10-01T02:03:00Z"})
        self.comment_id_offset = 3
        self.args.resume_from = 1
        self.args.handoff_comment = self.args.refresh_pr = None
        self.args.branch = source["branch"]
        original_api = self.read_api
        def api(method, path, *args, **kwargs):
            if path == f"/repos/owner/control/issues/comments/{evidence['id']}":
                return "bot", copy.deepcopy(evidence)
            return original_api(method, path, *args, **kwargs)
        self.read_api = api
        self.observe_inventory = CLAIM.observe_sessions
        return evidence

    def test_canonical_release_recovers_minimal_generation_through_all_claim_phases(self):
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_closure as closure
        with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
            fixture.add_home(fixture.root / "other-account")
            self.released_native_fixture(fixture)
            before = closure.source_records({fixture.session})[fixture.session]
            def frozen_before_post():
                self.assertTrue(closure.lease_state(fixture.session)["successor_admitted"])
            self.after_post = frozen_before_post
            self.run_claim()
            self.assertTrue(self.emitted.call_args.args[0]["ok"])
            self.assertIn("metadata_readback", self.emitted.call_args.args[0]["completed_steps"])
            self.assertFalse(closure.action_pending(fixture.session))
            self.assertEqual(closure.source_records({fixture.session})[fixture.session], before)

    def test_released_generation_rechecks_canonical_authority_after_native_await(self):
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_closure as closure
        import github_plan_native as native
        with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
            evidence = self.released_native_fixture(fixture)
            original = native.prove_end
            completed = []
            async def prove(*args, **kwargs):
                proof = await original(*args, **kwargs)
                completed.append(True)
                evidence["updated_at"] = "2026-10-01T03:00:00Z"
                return proof
            with patch.object(native, "prove_end", side_effect=prove), self.assertRaises(PLAN.PlanError):
                self.run_claim()
            self.assertTrue(completed)
            self.assert_no_writes()
            self.assertTrue(closure.action_pending(fixture.session))
            self.assertFalse(closure.lease_state(fixture.session).get("successor_admitted", False))

    def test_recovering_proof_failure_cannot_fall_back_to_other_home_worker_release(self):
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_native as native
        with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
            fixture.add_home(fixture.root / "other-account")
            evidence = self.released_native_fixture(fixture, worker_release=True)
            original = native.prove_end
            calls = []
            async def prove(*args, **kwargs):
                calls.append(True)
                if len(calls) > 1:
                    raise ValueError("native recovering proof unavailable")
                return await original(*args, **kwargs)
            self.after_post = lambda: evidence.update(updated_at="2026-10-01T03:00:00Z")
            with patch.object(native, "prove_end", side_effect=prove), self.assertRaises(PLAN.PlanError):
                self.run_claim()
            self.assertIn("post", self.events)
            self.assertNotIn("status", self.events)
            self.assertNotIn("labels", self.events)

    def test_released_minimal_generation_reused_tty_uses_verified_end(self):
        from datetime import datetime, timezone
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_closure as closure
        import github_plan_native as native_proof
        with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
            self.released_native_fixture(fixture)
            native = ["codex", "--remote", "unix://" + str(closure.daemon_endpoint(fixture.home)), "--", "new task"]
            with patch.object(closure, "process_started_at", return_value=datetime(2026, 10, 1, 3, tzinfo=timezone.utc)):
                fixture.managed(native)
                fixture.commands = "456 " + " ".join(native)
                fixture.process_rows = [(456, fixture.entry["tty"], " ".join(native))]
                # Identity-only metadata has no trusted end for the common
                # verifier; only the canonical source receipt supplies it.
                with self.assertRaises(ValueError):
                    native_proof.verify_source_process(closure.source_records({fixture.session})[fixture.session])
                self.run_claim()
            self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_released_generation_accepts_fresh_equivalent_and_later_phase_authority(self):
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_release as release
        import github_plan_native as native
        for when in ("during-await", "after-post"):
            with self.subTest(when=when):
                self.setUp()
                with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
                    evidence = self.released_native_fixture(fixture)
                    def replace():
                        if when == "after-post":
                            evidence["id"] = 89
                            evidence["created_at"] = "2026-10-01T03:02:00Z"
                            evidence["updated_at"] = "2026-10-01T03:02:00Z"
                            evidence["body"] = (f"Closed session {fixture.session}\n"
                                                "Ended at: 2026-10-01T03:01:00Z\nSafe to exit: yes\n")
                        original = self.comments[0]
                        body = release.prepare_release(original, evidence, self.comments, self.inventory,
                            actor=TEST_BOT, role="direction", releaser_session="fresh-direction-native",
                            evidence_url=f"https://github.com/owner/control/issues/884#issuecomment-{evidence['id']}",
                            retained_prs=[], related=[])
                        self.comments[:] = [c for c in self.comments if c.get("id") != 3]
                        self.comments.append({"id": 100, "body": body + "\nExplanation edited.",
                            "user": {"login": TEST_BOT}, "created_at": "2026-10-01T03:03:00Z",
                            "updated_at": "2026-10-01T04:00:00Z"})
                        self.comments.append({"id": 101, "body": "Unrelated progress report.", "user": {"login": TEST_BOT}})
                    original_prove = native.prove_end
                    replaced = []
                    async def prove(*args, **kwargs):
                        proof = await original_prove(*args, **kwargs)
                        if when == "during-await" and not replaced:
                            replaced.append(True)
                            replace()
                        return proof
                    if when == "after-post": self.after_post = replace
                    with patch.object(native, "prove_end", side_effect=prove): self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])
                    self.assertTrue(any(c.get("id") == 100 for c in self.comments))

    def test_released_generation_exclusions_preserve_pending_without_writes(self):
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_closure as closure
        import github_plan_release as release
        changes = ("worker-only", "pending", "missing-generation", "missing-pid", "missing-launch", "bad-launch", "mismatched-pid",
                   "late-launch", "late-resume", "wrong-home", "late-actual-turn", "incomplete-native",
                   "unidentified-process", "changed-source", "wrong-evidence-author")
        for changed in changes:
            with self.subTest(changed=changed):
                self.setUp()
                with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
                    evidence = self.released_native_fixture(fixture, worker_release=True)
                    rows = closure.log_rows()
                    generation = rows[0]["resumed_generation"]
                    if changed == "worker-only": self.comments.pop()
                    elif changed == "pending":
                        self.comments[-1]["body"] = self.comments[-1]["body"].replace(
                            "Released claim 1", "Pending release of claim 1").replace(release.RELEASE_MARKER, release.PENDING_MARKER)
                    elif changed == "missing-generation": rows[0].pop("resumed_generation")
                    elif changed == "missing-pid": generation.pop("pid")
                    elif changed == "missing-launch": generation.pop("launch_generation")
                    elif changed == "bad-launch": generation["launch_generation"] = ["invalid"]
                    elif changed == "mismatched-pid": generation["pid"] = 789
                    elif changed == "late-launch": generation["launch_started_at"] = "2026-10-01T03:00:00Z"
                    elif changed == "late-resume": rows[0]["resumed_at"] = "2026-10-01T03:00:00Z"
                    elif changed == "wrong-home": generation["codex_home"] = str(fixture.root / "not-owned")
                    elif changed == "late-actual-turn":
                        with fixture.transcript.open("a") as out:
                            for kind, payload in (("task_started", {"turn_id": "later"}),
                                                  ("agent_message", {"message": "Safe to exit: yes"}),
                                                  ("task_complete", {"turn_id": "later"})):
                                out.write(json.dumps({"type": "event_msg", "timestamp": "2026-10-01T03:00:00Z",
                                                      "payload": {"type": kind, **payload}}) + "\n")
                    elif changed == "incomplete-native": fixture.incomplete = True
                    elif changed == "unidentified-process": fixture.commands = "456 codex resume"
                    elif changed == "changed-source": self.comments[0]["updated_at"] = "2026-10-01T03:00:00Z"
                    elif changed == "wrong-evidence-author": evidence["user"]["login"] = "foreign"
                    closure.replace_rows(rows)
                    with self.assertRaises(PLAN.PlanError): self.run_claim()
                    self.assert_no_writes()
                    self.assertTrue(closure.action_pending(fixture.session))

    def test_canonical_generation_needs_its_owner_readback_not_only_validated_comments(self):
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_closure as closure
        import github_plan_release as release
        with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
            evidence = self.released_native_fixture(fixture)
            canonical = release.verified_comments(self.comments, lambda *_: copy.deepcopy(evidence))
            observed = CLAIM.observe_sessions(copy.deepcopy(self.inventory), canonical)
            self.assertEqual(observed["session_states"][fixture.session]["state"], "live")
            recovery = [peer for peer in observed["sessions"]
                        if peer.get("sessionId") == fixture.session and peer.get("source") == "native_recovery"]
            self.assertEqual(len(recovery), 1)
            self.assertTrue(closure.action_pending(fixture.session))
            self.run_claim()
            self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_canonical_recovery_preserves_existing_wait_and_rival_classification(self):
        from test_github_plan_sessions import ReceiptFixture
        for kind in ("wait", "rival"):
            with self.subTest(kind=kind):
                self.setUp()
                with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
                    self.released_native_fixture(fixture)
                    if kind == "wait":
                        self.args.wait_resolved = None
                        self.wait = wait_record.reviewed(evidence="Client's acceptance remains pending",
                            waiting_for="Client acceptance", ends_when="Client accepts")
                    else:
                        self.comments.append({"id": 75, "body": CLAIM.marker({**OTHER, "worker": "rival", "session": "rival-native"}),
                                              "user": {"login": TEST_BOT}})
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                    self.assertEqual(caught.exception.code, "claim_wait_unresolved" if kind == "wait" else "claim_conflict")
                    self.assert_no_writes()

    def test_later_canonical_end_can_cover_the_actual_generation_turn(self):
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_release as release
        with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
            evidence = self.released_native_fixture(fixture)
            with fixture.transcript.open("a") as out:
                for kind, payload in (("task_started", {"turn_id": "later"}),
                                      ("agent_message", {"message": "Safe to exit: yes"}),
                                      ("task_complete", {"turn_id": "later"})):
                    out.write(json.dumps({"type": "event_msg", "timestamp": "2026-10-01T02:30:00Z",
                                          "payload": {"type": kind, **payload}}) + "\n")
            later = {**evidence, "id": 89, "created_at": "2026-10-01T03:02:00Z", "updated_at": "2026-10-01T03:02:00Z",
                     "body": f"Closed session {fixture.session}\nEnded at: 2026-10-01T03:01:00Z\nSafe to exit: yes\n"}
            body = release.prepare_release(self.comments[0], later, self.comments, self.inventory,
                actor=TEST_BOT, role="supervisor", releaser_session="supervisor-native",
                evidence_url="https://github.com/owner/control/issues/884#issuecomment-89", retained_prs=[], related=[])
            self.comments.append({"id": 100, "body": body, "user": {"login": TEST_BOT}, "created_at": "2026-10-01T03:03:00Z"})
            original_api = self.read_api
            self.read_api = lambda method, path, *a, **k: ("bot", copy.deepcopy(later)) if path.endswith(
                "/issues/comments/89") else original_api(method, path, *a, **k)
            self.run_claim()
            self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_released_generation_passive_delivery_keeps_actual_turn_authority(self):
        from test_github_plan_sessions import ReceiptFixture
        with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
            self.released_native_fixture(fixture)
            with fixture.transcript.open("a") as out:
                out.write(json.dumps({"type": "response_item", "timestamp": "2026-10-01T03:00:00Z", "payload": {
                    "type": "message", "role": "user", "content": [{"type": "input_text",
                        "text": "<subagent_notification>Delivered a result.</subagent_notification>"}]}}) + "\n")
            self.run_claim()
            self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_released_generation_postproof_changes_and_read_errors_do_not_clear(self):
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_closure as closure
        import github_plan_native as native
        for changed in ("process", "source-row", "action", "canonical-read", "closed-target"):
            with self.subTest(changed=changed):
                self.setUp()
                with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
                    self.released_native_fixture(fixture)
                    original_prove, original_api = native.prove_end, self.read_api
                    altered = []
                    def api(*args, **kwargs):
                        if altered and changed == "canonical-read":
                            raise PLAN.PlanError("Unavailable", api_result={"status": 503})
                        return original_api(*args, **kwargs)
                    self.read_api = api
                    async def prove(*args, **kwargs):
                        proof = await original_prove(*args, **kwargs)
                        altered.append(True)
                        if changed == "process": fixture.process_rows = [(123, fixture.entry["tty"], "codex")]
                        elif changed == "source-row":
                            rows = closure.log_rows()
                            rows[0]["resumed_generation"]["launch_generation"] = "changed-generation"
                            closure.replace_rows(rows)
                        elif changed == "action":
                            closure.set_action_pending(fixture.session, True, method="turn/start", home=fixture.home)
                        elif changed == "closed-target": self.issue["state"] = "closed"
                        return proof
                    with patch.object(native, "prove_end", side_effect=prove), self.assertRaises(PLAN.PlanError):
                        self.run_claim()
                    self.assertTrue(altered)
                    self.assert_no_writes()
                    self.assertTrue(closure.action_pending(fixture.session))

    def test_canonical_end_cannot_predate_a_known_source_exit(self):
        from datetime import datetime, timezone
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_sessions as sessions
        for minute in (0, 2):
            with self.subTest(exit_minute=minute):
                self.setUp()
                with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
                    self.released_native_fixture(fixture)
                    cutoff = datetime(2026, 10, 1, 2, minute, tzinfo=timezone.utc)
                    with patch.object(sessions.process_liveness(), "native_exit_cutoff", return_value=cutoff):
                        if minute == 2:
                            with self.assertRaises(PLAN.PlanError): self.run_claim()
                            self.assert_no_writes()
                        else:
                            self.run_claim()
                            self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_canonical_recovery_observes_retained_pr_source_and_cannot_hide_renewal(self):
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_release as release
        import github_plan_closure as closure
        for renewed in (False, True):
            with self.subTest(renewed=renewed):
                self.setUp()
                with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
                    evidence = self.released_native_fixture(fixture)
                    original = {**self.comments[0], "id": 71}
                    worker = {"id": 72, "body": "Released claim 71", "user": {"login": TEST_BOT},
                              "created_at": "2026-10-01T02:00:30Z"}
                    body = release.prepare_release(original, evidence, [original, worker], self.inventory,
                        actor=TEST_BOT, role="supervisor", releaser_session="supervisor-native",
                        evidence_url="https://github.com/owner/control/issues/884#issuecomment-88", retained_prs=[], related=[])
                    confirmed = {"id": 73, "body": body, "user": {"login": TEST_BOT},
                                 "created_at": "2026-10-01T02:03:00Z"}
                    self.ordinary_handoff_fixture()
                    self.args.branch = OWNER["branch"]
                    self.target_comments["/repos/owner/repo/issues/99/comments"] = [original, worker, confirmed]
                    if renewed: original["updated_at"] = "2026-10-01T03:00:00Z"
                    if renewed:
                        with self.assertRaises(PLAN.PlanError): self.run_claim()
                        self.assert_no_writes()
                        self.assertTrue(closure.action_pending(fixture.session))
                    else:
                        self.run_claim()
                        self.assertFalse(closure.action_pending(fixture.session))
                        self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_canonical_recovery_covers_native_child_owner_callback(self):
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_release as release
        import github_plan_closure as closure
        for renewed in (False, True):
            with self.subTest(renewed=renewed):
                self.setUp()
                with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
                    evidence = self.released_native_fixture(fixture)
                    child_source = CLAIM.records(self.comments[0]["body"])[0]
                    child_source.update(branch="work/child-source", resume_from="71",
                                        retained_handoff="https://github.com/owner/repo/issues/42#issuecomment-72")
                    original = {**self.comments[0], "body": CLAIM.marker(child_source)}
                    worker = {"id": 2, "body": "Released claim 1", "user": {"login": TEST_BOT},
                              "created_at": "2026-10-01T02:00:30Z"}
                    body = release.prepare_release(original, evidence, [original, worker], self.inventory,
                        actor=TEST_BOT, role="supervisor", releaser_session="supervisor-native",
                        evidence_url="https://github.com/owner/control/issues/884#issuecomment-88", retained_prs=[], related=[])
                    confirmed = {"id": 3, "body": body, "user": {"login": TEST_BOT},
                                 "created_at": "2026-10-01T02:03:00Z"}
                    parent_source = {**OTHER, "session": "parent-source", "worker": "parent-worker", "branch": "work/parent"}
                    self.comments = [{"id": 71, "body": CLAIM.marker(parent_source), "user": {"login": TEST_BOT},
                                      "created_at": parent_source["claimed_at"]},
                                     {"id": 72, "body": "Released claim 71", "user": {"login": TEST_BOT},
                                      "created_at": "2026-10-01T02:00:30Z"}]
                    self.args.resume_from, self.args.branch = 71, "work/new-successor"
                    self.inventory.update(local_branches=[child_source["branch"]], remote_branches=[child_source["branch"]],
                                          worktrees=[{"branch": child_source["branch"], "path": "/retained"}])
                    self.comment_id_offset = 100
                    self.targets["43"] = {**copy.deepcopy(self.issue), "number": 43,
                                          "state": "closed",
                                          "body": "## Current Status\n\n" + CLAIM.marker(child_source)}
                    self.children["/repos/owner/repo/issues/42/sub_issues"] = [self.targets["43"]]
                    self.target_comments["/repos/owner/repo/issues/43/comments"] = [original, worker, confirmed]
                    if renewed: original["updated_at"] = "2026-10-01T03:00:00Z"
                    if renewed:
                        with self.assertRaises(PLAN.PlanError): self.run_claim()
                        self.assert_no_writes()
                        self.assertTrue(closure.action_pending(fixture.session))
                    else:
                        self.run_claim()
                        self.assertFalse(closure.action_pending(fixture.session))
                        self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_canonical_minimal_source_supports_the_retained_handoff_route(self):
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_release as release
        import github_plan_closure as closure
        with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
            evidence = self.released_native_fixture(fixture)
            source = self.comments[0]
            self.ordinary_handoff_fixture()
            self.args.branch = OWNER["branch"]
            self.comments[0] = source
            branches = {p["head"]["ref"] for p in [*self.pulls, *self.closed_pulls.values()]}
            self.comments[-1]["body"] = release.prepare_release(source, evidence, self.comments, self.inventory,
                actor=TEST_BOT, role="supervisor", releaser_session="supervisor-native",
                evidence_url="https://github.com/owner/control/issues/884#issuecomment-88",
                retained_prs=[f"https://github.com/owner/repo/pull/{n}" for n in (99, 100)], related=[],
                retained_branches=branches)
            self.comments[-1].update(created_at="2026-10-01T02:03:00Z", updated_at="2026-10-01T02:03:00Z")
            self.run_claim()
            result = self.emitted.call_args.args[0]
            self.assertTrue(result["ok"])
            self.assertTrue(result["claim"]["retained_handoff"].endswith("#issuecomment-3"))
            self.assertFalse(closure.action_pending(fixture.session))

    def shared_native_family_fixture(self, fixture, *, current="child", end_on="child"):
        import github_plan_release as release
        evidence = self.released_native_fixture(fixture)
        child_record = CLAIM.records(self.comments[0]["body"])[0]
        child_record.update(branch="work/child", resume_from="71",
                            retained_handoff="https://github.com/owner/repo/issues/41#issuecomment-72")
        parent_record = {**OTHER, "session": fixture.session, "branch": "work/parent"}
        child = [{**self.comments[0], "body": CLAIM.marker(child_record)},
                 {"id": 2, "body": "Released claim 1", "user": {"login": TEST_BOT}, "created_at": "2026-10-01T02:00:30Z"}]
        parent = [{"id": 71, "body": CLAIM.marker(parent_record), "user": {"login": TEST_BOT},
                   "created_at": parent_record["claimed_at"]},
                  {"id": 72, "body": "Released claim 71", "user": {"login": TEST_BOT}, "created_at": "2026-10-01T02:00:30Z"}]
        self.inventory.update(local_branches=["work/child", "work/parent"], remote_branches=["work/child", "work/parent"],
                              worktrees=[{"branch": "work/child", "path": "/retained"}])
        selected = child if end_on == "child" else parent
        body = release.prepare_release(selected[0], evidence, selected, self.inventory, actor=TEST_BOT,
            role="supervisor", releaser_session="supervisor-native",
            evidence_url="https://github.com/owner/control/issues/884#issuecomment-88", retained_prs=[], related=[])
        selected.append({"id": 3 if end_on == "child" else 73, "body": body, "user": {"login": TEST_BOT},
                         "created_at": "2026-10-01T02:03:00Z"})
        parent_issue = {**copy.deepcopy(self.issue), "number": 41,
                        "body": "## Current Status\n\n" + CLAIM.marker(parent_record)}
        child_issue = {**copy.deepcopy(self.issue), "number": 42,
                       "body": "## Current Status\n\n" + CLAIM.marker(child_record)}
        self.children["/repos/owner/repo/issues/41/sub_issues"] = [child_issue]
        self.args.branch, self.comment_id_offset = "work/fresh-successor", 200
        if current == "child":
            self.issue, self.comments = child_issue, child
            self.targets["41"] = parent_issue
            self.target_comments["/repos/owner/repo/issues/41/comments"] = parent
            self.args.issue, self.args.resume_from = "42", 1
        else:
            self.issue, self.comments = parent_issue, parent
            self.targets["42"] = child_issue
            self.target_comments["/repos/owner/repo/issues/42/comments"] = child
            self.args.issue, self.args.resume_from = "41", 71
        return evidence, parent, child

    def test_native_end_is_shared_across_existing_family_in_each_direction(self):
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_closure as closure
        for current, end_on in (("child", "child"), ("parent", "child"), ("child", "parent")):
            with self.subTest(current=current, end_on=end_on):
                self.setUp()
                with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
                    self.shared_native_family_fixture(fixture, current=current, end_on=end_on)
                    before = closure.source_records({fixture.session})[fixture.session]
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])
                    self.assertIn("metadata_readback", self.emitted.call_args.args[0]["completed_steps"])
                    self.assertEqual(closure.source_records({fixture.session})[fixture.session], before)
                    self.assertTrue(closure.lease_state(fixture.session)["successor_admitted"])

    def test_shared_native_end_does_not_release_an_unreleased_parent(self):
        from test_github_plan_sessions import ReceiptFixture
        with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
            _, parent, _ = self.shared_native_family_fixture(fixture)
            parent.pop()
            with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
            self.assertEqual(caught.exception.code, "claim_conflict")
            self.assert_no_writes()

    def test_shared_authority_owner_is_read_freshly_after_native_await(self):
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_native as native
        import github_plan_closure as closure
        with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
            evidence, _, _ = self.shared_native_family_fixture(fixture, current="parent")
            original, completed = native.prove_end, []
            async def prove(*args, **kwargs):
                proof = await original(*args, **kwargs)
                completed.append(True)
                evidence["updated_at"] = "2026-10-01T03:00:00Z"
                return proof
            with patch.object(native, "prove_end", side_effect=prove), self.assertRaises(PLAN.PlanError): self.run_claim()
            self.assertTrue(completed)
            self.assert_no_writes()
            self.assertTrue(closure.action_pending(fixture.session))

    def test_transitive_and_historical_descendant_end_reaches_current_native_proof(self):
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_release as release
        import github_plan_closure as closure
        for historical in (False, True):
            with self.subTest(historical=historical):
                self.setUp()
                with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
                    evidence, _, child = self.shared_native_family_fixture(fixture, current="parent")
                    child.pop()
                    record = {**CLAIM.records(child[0]["body"])[0], "branch": "work/leaf", "resume_from": "1",
                              "retained_handoff": "https://github.com/owner/repo/issues/42#issuecomment-2"}
                    original = {**child[0], "id": 91, "body": CLAIM.marker(record)}
                    worker = {**child[1], "id": 92, "body": "Released claim 91"}
                    body = release.prepare_release(original, evidence, [original, worker], self.inventory,
                        actor=TEST_BOT, role="supervisor", releaser_session="supervisor-native",
                        evidence_url="https://github.com/owner/control/issues/884#issuecomment-88", retained_prs=[], related=[])
                    leaf = {**copy.deepcopy(self.targets["42"]), "number": 43,
                            "body": "## Current Status\n\n" + CLAIM.marker(record)}
                    self.targets["43"] = leaf
                    self.target_comments["/repos/owner/repo/issues/43/comments"] = [original, worker,
                        {"id": 93, "body": body, "user": {"login": TEST_BOT}, "created_at": "2026-10-01T02:03:00Z"}]
                    if historical:
                        self.timelines["/repos/owner/repo/issues/42/timeline"] = [
                            {"event": "sub_issue_added", "sub_issue": leaf}]
                    else:
                        self.children["/repos/owner/repo/issues/42/sub_issues"] = [leaf]
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])
                    self.assertTrue(closure.lease_state(fixture.session)["successor_admitted"])

    def test_shared_native_authority_is_fresh_in_each_publication_phase(self):
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_release as release
        for phase, change in product(("after_post", "after_status"), ("withdraw", "evidence", "replace")):
            with self.subTest(phase=phase, change=change):
                self.setUp()
                with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
                    evidence, _, child = self.shared_native_family_fixture(fixture, current="parent")
                    def change_authority():
                        child.pop()
                        if change == "evidence":
                            evidence["updated_at"] = "2026-10-01T04:00:00Z"
                        if change != "withdraw":
                            if change == "replace":
                                evidence["id"] = 89
                                evidence["created_at"] = evidence["updated_at"] = "2026-10-01T03:02:00Z"
                                evidence["body"] = (f"Closed session {fixture.session}\n"
                                                    "Ended at: 2026-10-01T03:01:00Z\nSafe to exit: yes\n")
                            body = release.prepare_release(child[0], evidence, child, self.inventory,
                                actor=TEST_BOT, role="direction", releaser_session="fresh-direction-native",
                                evidence_url=f"https://github.com/owner/control/issues/884#issuecomment-{evidence['id']}",
                                retained_prs=[], related=[])
                            if change == "evidence":
                                # Keep the formerly valid receipt, not a new attestation.
                                body = saved_receipt
                            child.append({"id": 100, "body": body, "user": {"login": TEST_BOT},
                                          "created_at": "2026-10-01T03:03:00Z"})
                    saved_receipt = child[-1]["body"]
                    setattr(self, phase, change_authority)
                    if change == "replace":
                        self.run_claim()
                        self.assertTrue(self.emitted.call_args.args[0]["ok"])
                    else:
                        with self.assertRaises(PLAN.PlanError): self.run_claim()
                        self.assertIn("post", self.events)
                        if phase == "after_post":
                            self.assertNotIn("status", self.events)
                            self.assertNotIn("labels", self.events)
                        else:
                            self.assertIn("status", self.events)
                            self.assertIn("labels", self.events)

    def test_retained_pr_end_is_available_before_current_native_observation(self):
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_release as release
        import github_plan_closure as closure
        with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
            evidence = self.released_native_fixture(fixture)
            source = self.comments[0]
            self.ordinary_handoff_fixture()
            self.args.branch = OWNER["branch"]
            self.comments[0] = source
            self.comments[1]["body"] = "Released claim 1"
            self.comments[1]["created_at"] = "2026-10-01T02:00:30Z"
            self.comments[2]["created_at"] = "2026-10-01T02:00:40Z"
            original = {**source, "id": 71}
            worker = {**self.comments[1], "id": 72, "body": "Released claim 71"}
            body = release.prepare_release(original, evidence, [original, worker], self.inventory,
                actor=TEST_BOT, role="supervisor", releaser_session="supervisor-native",
                evidence_url="https://github.com/owner/control/issues/884#issuecomment-88", retained_prs=[], related=[])
            self.target_comments["/repos/owner/repo/issues/99/comments"] = [original, worker,
                {"id": 73, "body": body, "user": {"login": TEST_BOT}, "created_at": "2026-10-01T02:03:00Z"}]
            self.run_claim()
            self.assertTrue(self.emitted.call_args.args[0]["ok"])
            self.assertTrue(closure.lease_state(fixture.session)["successor_admitted"])

    def test_multiple_canonical_ends_do_not_retry_native_runtime_or_source_failures(self):
        from test_github_plan_sessions import ReceiptFixture
        import github_plan_native as native
        import github_plan_closure as closure
        for failure in ("queue", "runtime", "read", "source-change"):
            with self.subTest(failure=failure):
                self.setUp()
                with ReceiptFixture("11111111-2222-4333-8444-555555555555") as fixture:
                    self.released_native_fixture(fixture)
                    self.comments.append({**self.comments[-1], "id": 100})
                    calls, snapshots = [], []
                    original_prove, original_snapshot = native.prove_end, native.source_snapshot
                    if failure == "queue": fixture.queue_items = [{"id": "pending", "input": []}]
                    elif failure == "runtime": fixture.native_state = "idle"; fixture.execution_items = [{"status": "running", "type": "commandExecution"}]
                    async def prove(*args, **kwargs):
                        calls.append(True)
                        if failure == "read": raise ValueError("canonical native read unavailable")
                        return await original_prove(*args, **kwargs)
                    def snapshot(*args, **kwargs):
                        snapshots.append(True)
                        if failure == "source-change" and len(snapshots) == 2:
                            with fixture.transcript.open("a") as out:
                                for kind, payload in (("task_started", {"turn_id": "changed"}),
                                                      ("agent_message", {"message": "Safe to exit: yes"}),
                                                      ("task_complete", {"turn_id": "changed"})):
                                    out.write(json.dumps({"type": "event_msg", "timestamp": "2026-10-01T03:00:00Z",
                                                          "payload": {"type": kind, **payload}}) + "\n")
                        return original_snapshot(*args, **kwargs)
                    with patch.object(native, "prove_end", side_effect=prove), patch.object(native, "source_snapshot", side_effect=snapshot):
                        with self.assertRaises(PLAN.PlanError): self.run_claim()
                    self.assertEqual(len(calls), 1)
                    self.assertTrue(closure.action_pending(fixture.session))
                    self.assert_no_writes()

    def test_no_pr_handoff_refusal_supplies_existing_resume_route(self):
        source = self.no_pr_resume_fixture()
        with self.assertRaises(PLAN.PlanError) as caught:
            self.run_claim()
        self.assert_no_writes()
        route = caught.exception.payload["successor_route"]
        resumed = PLAN.build_parser().parse_args([
            "claim", self.args.issue, "--worker", self.args.worker, "--session", self.args.session,
            "--next-action", self.args.next_action,
            "--resume-from", str(route["resume_from"]), "--branch", route["branch"]])
        self.assertEqual(resumed.resume_from, self.args.resume_from)
        self.assertEqual(resumed.branch, source["branch"])
        self.assertIsNone(resumed.handoff_comment)
        self.assertIsNone(resumed.refresh_pr)
        self.args = resumed
        self.run_claim()
        self.assertIn("metadata_readback", self.emitted.call_args.args[0]["completed_steps"])

    def test_no_pr_hint_never_routes_around_a_retained_pr(self):
        for kind in ("source_open", "foreign_author", "unlinked", "foreign_repo", "named_other_branch", "closed_unmerged"):
            with self.subTest(kind=kind):
                self.setUp()
                source = self.no_pr_resume_fixture()
                pull = {"number": 99, "state": "open", "user": {"login": TEST_BOT},
                        "body": "Refs #42", "title": "Retained work",
                        "head": {"ref": source["branch"], "repo": {"full_name": self.args.repo}},
                        "base": {"repo": {"full_name": self.args.repo}}}
                if kind != "source_open": self.comments[-1]["body"] += " Retain PR #99."
                if kind == "foreign_author": pull["user"]["login"] = "stranger"
                if kind == "unlinked": pull["body"] = "Unrelated"
                if kind == "foreign_repo": pull["head"]["repo"]["full_name"] = "stranger/repo"
                if kind == "named_other_branch":
                    pull["head"]["ref"] = "work/other"
                    pull["body"] = "Refs #41"
                if kind == "closed_unmerged":
                    pull["state"] = "closed"
                    self.closed_pulls[pull["number"]] = pull
                else: self.pulls = [pull]
                with self.assertRaises(PLAN.PlanError) as caught:
                    self.run_claim()
                self.assertNotIn("successor_route", caught.exception.payload)
                self.assert_no_writes()

    def test_no_pr_handoff_edit_after_publication_offers_only_claim_recovery(self):
        for phase in ("after_post", "after_status"):
            with self.subTest(phase=phase):
                self.setUp()
                self.ordinary_handoff_fixture()
                def edit_handoff():
                    next(c for c in self.comments if c["id"] == self.args.handoff_comment)["body"] = (
                        f"Released claim {self.args.resume_from}\n\nPreserved pushed WIP. No PR opened.")
                setattr(self, phase, edit_handoff)
                with self.assertRaises(PLAN.PlanError) as caught:
                    self.run_claim()
                payload = caught.exception.payload
                self.assertIn("post", self.events)
                if phase == "after_status": self.assertIn("status", self.events)
                self.assertNotIn("successor_route", payload)
                self.assertEqual(payload["claim_recovery"]["release_own_claim"]["comment_id"],
                                 payload["claim_comment"]["comment"]["id"])

    def test_no_pr_hint_cannot_redirect_a_native_child(self):
        self.parent_handoff_fixture()
        self.target_comments["/repos/owner/repo/issues/41/comments"][-1]["body"] = "Released claim 1\n\nPushed WIP, no PR."
        self.pulls, self.closed_pulls = [], {}
        with self.assertRaises(PLAN.PlanError) as caught:
            self.run_claim()
        self.assertNotIn("successor_route", caught.exception.payload)
        self.assert_no_writes()

    def test_no_pr_resume_rechecks_rivals_holds_and_other_artifacts(self):
        for phase, change in product(("preflight", "after_post", "after_status"),
                                     ("source", "peer", "claim", "branch", "blocker", "wait")):
            with self.subTest(phase=phase, change=change):
                self.setUp()
                source = self.no_pr_resume_fixture()
                self.args.handoff_comment = None
                self.args.branch = source["branch"]
                def invalidate():
                    if change == "source": self.inventory["sessions"] = [{"sessionId": source["session"], "cwd": "/elsewhere"}]
                    if change == "peer": self.inventory["sessions"] = [{"sessionId": "peer", "cwd": "/retained/subdir"}]
                    if change == "claim": self.compete({**OTHER, "session": "rival"})
                    if change == "branch": self.inventory["remote_branches"].append("work/issue-42-extra")
                    if change == "blocker": self.blockers = [{"state": "open"}]
                    if change == "wait": self.record_wait()
                if phase == "preflight": invalidate()
                else: setattr(self, phase, invalidate)
                with self.assertRaises(PLAN.PlanError) as caught:
                    self.run_claim()
                if phase == "preflight": self.assert_no_writes()
                else: self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])

    def parent_handoff_fixture(self):
        self.ordinary_handoff_fixture()
        self.parent = {"number": 41, "state": "open", "html_url": "https://github.com/owner/repo/issues/41"}
        parent_wait = wait_record.reviewed(evidence="Client qualification is still pending",
                                          waiting_for="Client qualification", ends_when="Client accepts")
        source = CLAIM.records(self.comments[0]["body"])[0]
        self.comments[0]["created_at"] = source["claimed_at"]
        self.comments[-1]["created_at"] = "2026-10-02T00:00:00Z"
        self.targets["41"] = {**copy.deepcopy(self.issue), **self.parent,
                              "labels": [{"name": PLAN.DEFAULT_CONFIG["labels"]["waiting"]}],
                              "body": (f"## Current Status\n\nState: Active; owned by {source['worker']}.\n"
                                       f"Session: {source['session']}\nBranch: {source['branch']}\n"
                                       + CLAIM.marker(source) + "\n" + wait_record.marker(parent_wait))}
        self.target_comments["/repos/owner/repo/issues/41/comments"] = self.comments
        self.comment_id_offset = max(c["id"] for c in self.comments)
        self.comments = []
        self.children["/repos/owner/repo/issues/41/sub_issues"] = [copy.deepcopy(self.issue)]
        for pull in [*self.pulls, *self.closed_pulls.values()]:
            pull["body"] = "Refs #41"

    def test_native_child_resumes_parent_handoff_and_preserves_parent_waits(self):
        self.parent_handoff_fixture()
        before = copy.deepcopy((self.targets, self.target_comments, self.inventory, self.pulls))
        self.run_claim()
        output = self.emitted.call_args.args[0]
        self.assertTrue(output["ok"])
        self.assertIn("metadata_readback", output["completed_steps"])
        self.assertEqual(output["claim"]["retained_handoff"],
                         f"{self.parent['html_url']}#issuecomment-{self.args.handoff_comment}")
        self.assertEqual(before, (self.targets, self.target_comments, self.inventory, self.pulls))
        self.assertEqual(self.post_route, ("issue", self.issue["number"], self.issue["repo"]))
        self.inventory["local_branches"].append(self.args.branch)
        self.inventory["remote_branches"].append(self.args.branch)
        self.inventory["worktrees"].append({"branch": self.args.branch, "path": "/successor"})
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])
        self.assertEqual(len(self.comments), 1)  # Same-session recovery reuses its claim.

    def omitted_history_child(self, child):
        node_id = "fixture-sub-issue-added-event"
        event = {"event": "sub_issue_added", "node_id": node_id, "sub_issue": None}
        self.history_nodes[node_id] = {"data": {"node": {
            "id": node_id, "__typename": "SubIssueAddedEvent", "subIssue": {
                "id": f"fixture-issue-{child['number']}", "number": child["number"],
                "state": child["state"].upper(), "title": child["title"],
                "url": f"https://github.com/owner/repo/issues/{child['number']}",
                "repository": {"nameWithOwner": "owner/repo"}}}}}
        self.timelines["/repos/owner/repo/issues/41/timeline"] = [event]
        return event, self.history_nodes[node_id]

    def test_native_child_resolves_omitted_rest_history_through_exact_event(self):
        for missing_key in (False, True):
            with self.subTest(missing_key=missing_key):
                self.setUp()
                self.parent_handoff_fixture()
                parent_before = copy.deepcopy((self.targets, self.target_comments))
                event, _ = self.omitted_history_child(self.issue)
                if missing_key:
                    event.pop("sub_issue")
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])
                self.assertEqual(parent_before, (self.targets, self.target_comments))
                self.assertTrue(self.history_reads)
                for _, payload, _ in self.history_reads:
                    self.assertEqual(payload["variables"]["id"], event["node_id"])

    def test_hydrated_history_keeps_detached_live_child_ownership(self):
        for phase in ("preflight", "after_post", "after_status"):
            with self.subTest(phase=phase):
                self.setUp()
                self.parent_handoff_fixture()
                successor = {**OTHER, "session": "detached-child-session", "branch": "work/detached-source",
                             "resume_from": str(self.args.resume_from),
                             "retained_handoff": f"{self.parent['html_url']}#issuecomment-{self.args.handoff_comment}"}
                sibling = {**copy.deepcopy(self.issue), "number": 43,
                           "body": "## Current Status\n\n" + CLAIM.marker(successor)}
                self.targets["43"] = sibling
                self.target_comments["/repos/owner/repo/issues/43/comments"] = [
                    {"id": 88, "body": CLAIM.marker(successor), "user": {"login": TEST_BOT},
                     "created_at": successor["claimed_at"]}]
                def detached():
                    self.omitted_history_child(sibling)
                if phase == "preflight":
                    detached()
                else:
                    setattr(self, phase, detached)
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assertEqual(caught.exception.payload["competing_evidence"][0]["record"], successor)
                self.assertTrue(self.history_reads)
                if phase == "preflight":
                    self.assert_no_writes()
                else:
                    self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])

    def test_unresolved_history_event_refuses_without_claim_writes(self):
        for failure in ("unreadable", "errors", "wrong_id", "wrong_type", "missing_child",
                        "missing_issue_id", "repository", "number", "state", "url"):
            with self.subTest(failure=failure):
                self.setUp()
                self.parent_handoff_fixture()
                event, response = self.omitted_history_child(self.issue)
                node = response["data"]["node"]
                if failure == "unreadable":
                    self.history_nodes[event["node_id"]] = PLAN.PlanError("History unavailable", api_result={"status": 403})
                elif failure == "errors":
                    response["errors"] = [{"message": "History unavailable"}]
                elif failure == "wrong_id":
                    node["id"] = "another-event"
                elif failure == "wrong_type":
                    node["__typename"] = "IssueComment"
                elif failure == "missing_child":
                    node["subIssue"] = None
                else:
                    node["subIssue"][{"missing_issue_id": "id", "repository": "repository"}.get(failure, failure)] = None
                with self.assertRaises(PLAN.PlanError):
                    self.run_claim()
                self.assert_no_writes()

    def test_unresolved_history_after_claim_write_requires_exact_release(self):
        for phase, failure in product(("after_post", "after_status"), ("unreadable", "missing_child")):
            with self.subTest(phase=phase, failure=failure):
                self.setUp()
                self.parent_handoff_fixture()
                event, response = self.omitted_history_child(self.issue)
                def unavailable():
                    if failure == "unreadable":
                        self.history_nodes[event["node_id"]] = PLAN.PlanError("History unavailable", api_result={"status": 403})
                    else:
                        response["data"]["node"]["subIssue"] = None
                setattr(self, phase, unavailable)
                with self.assertRaises(PLAN.PlanError) as caught:
                    self.run_claim()
                self.assertIn("post_claim", caught.exception.payload["completed_steps"])
                self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])

    def nested_handoff_fixture(self):
        self.parent_handoff_fixture()
        middle = {**OTHER, "session": "middle-session", "branch": "work/middle",
                  "resume_from": str(self.args.resume_from),
                  "retained_handoff": f"{self.parent['html_url']}#issuecomment-{self.args.handoff_comment}"}
        self.targets["43"] = {**copy.deepcopy(self.issue), "number": 43, "id": 543,
                              "body": "## Current Status\n\n" + CLAIM.marker(middle)}
        self.target_comments["/repos/owner/repo/issues/43/comments"] = [
            {"id": 88, "body": CLAIM.marker(middle), "user": {"login": TEST_BOT},
             "created_at": middle["claimed_at"]},
            {"id": 89, "body": "Released claim 88\nRetained PRs: #99 #100",
             "user": {"login": TEST_BOT}, "created_at": "2026-10-02T00:00:00Z"}]
        self.children["/repos/owner/repo/issues/41/sub_issues"] = [self.targets["43"]]
        self.children["/repos/owner/repo/issues/43/sub_issues"] = [copy.deepcopy(self.issue)]
        self.parent = {"number": 43, "state": "open", "html_url": "https://github.com/owner/repo/issues/43"}
        self.args.resume_from, self.args.handoff_comment = 88, 89
        for pull in [*self.pulls, *self.closed_pulls.values()]:
            pull["body"] = "Refs #41 and #43"
        self.comment_id_offset = 89

    def test_transitive_family_refuses_holders_in_both_directions_at_every_phase(self):
        for claimant, phase in product(("root", "uncle", "leaf_root_holder", "leaf_uncle_holder"),
                                      ("preflight", "after_post", "after_status")):
            with self.subTest(claimant=claimant, phase=phase):
                self.setUp()
                self.nested_handoff_fixture()
                holder = {**OTHER, "session": "family-holder", "branch": "work/family-holder"}
                holder_number = 42 if claimant in {"root", "uncle"} else (41 if claimant == "leaf_root_holder" else 45)
                if holder_number != 41:
                    holder.update(resume_from="88" if holder_number == 42 else "1",
                                  retained_handoff=f"https://github.com/owner/repo/issues/{43 if holder_number == 42 else 41}#issuecomment-{89 if holder_number == 42 else 3}")
                if claimant in {"root", "uncle"}:
                    self.targets["42"] = copy.deepcopy(self.issue)
                    self.target_comments["/repos/owner/repo/issues/42/comments"] = []
                    if claimant == "root":
                        self.issue = self.targets.pop("41")
                        self.comments = self.target_comments.pop("/repos/owner/repo/issues/41/comments")
                        self.args.issue, self.args.resume_from, self.args.handoff_comment = "41", 1, None
                        self.args.wait_resolved = "Separate source work; preserve qualification wait"
                        source_branch = CLAIM.records(self.comments[0]["body"])[0]["branch"]
                        self.pulls[0]["head"]["ref"] = source_branch
                        self.inventory.update(worktrees=[], local_branches=[], remote_branches=[])
                    else:
                        self.issue = {**copy.deepcopy(self.issue), "number": 45}
                        self.args.issue, self.args.resume_from, self.args.handoff_comment = "45", 1, 3
                        self.parent = {"number": 41, "state": "open", "html_url": "https://github.com/owner/repo/issues/41"}
                if holder_number == 45:
                    self.targets["45"] = {**copy.deepcopy(self.issue), "number": 45, "body": "## Current Status\n\nState: Unclaimed"}
                    self.children["/repos/owner/repo/issues/41/sub_issues"].append(self.targets["45"])
                def activate():
                    self.targets[str(holder_number)]["body"] = "## Current Status\n\n" + CLAIM.marker(holder)
                    self.target_comments.setdefault(f"/repos/owner/repo/issues/{holder_number}/comments", []).append(
                        {"id": 90, "body": CLAIM.marker(holder), "user": {"login": TEST_BOT},
                         "created_at": holder["claimed_at"]})
                if phase == "preflight": activate()
                else: setattr(self, phase, activate)
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assertEqual(caught.exception.payload["coordination"][0]["claim_url"],
                                 f"https://github.com/owner/repo/issues/{holder_number}#issuecomment-90")
                if phase == "preflight": self.assert_no_writes()
                else: self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])

    def test_transitive_family_clear_chain_and_same_session_recovery(self):
        self.nested_handoff_fixture()
        before = copy.deepcopy((self.targets, self.target_comments, self.children))
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])
        self.inventory["worktrees"].append({"branch": self.args.branch, "path": "/leaf"})
        self.inventory["local_branches"].append(self.args.branch)
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(before, (self.targets, self.target_comments, self.children))

    def test_child_handoff_checks_its_existing_descendants_before_posting(self):
        for state in ("active", "released", "released_live"):
            with self.subTest(state=state):
                self.setUp()
                self.parent_handoff_fixture()
                earlier = {**OTHER, "session": "earlier-child-work", "branch": "work/earlier-child-work"}
                self.comments = [
                    {"id": 50, "body": CLAIM.marker(earlier), "user": {"login": TEST_BOT}, "created_at": earlier["claimed_at"]},
                    {"id": 51, "body": "Released claim 50", "user": {"login": TEST_BOT}, "created_at": "2026-10-02T00:00:00Z"}]
                descendant = {**OTHER, "session": "earlier-descendant", "branch": "work/earlier-descendant", "resume_from": "50",
                              "retained_handoff": "https://github.com/owner/repo/issues/42#issuecomment-51"}
                self.targets["44"] = {**copy.deepcopy(self.issue), "number": 44,
                                      "body": "## Current Status\n\n" + CLAIM.marker(descendant)}
                self.children["/repos/owner/repo/issues/42/sub_issues"] = [self.targets["44"]]
                self.target_comments["/repos/owner/repo/issues/44/comments"] = [
                    {"id": 52, "body": CLAIM.marker(descendant), "user": {"login": TEST_BOT}, "created_at": descendant["claimed_at"]}]
                if state.startswith("released"):
                    self.target_comments["/repos/owner/repo/issues/44/comments"].append(
                        {"id": 53, "body": "Released claim 52", "user": {"login": TEST_BOT}, "created_at": "2026-10-02T00:00:00Z"})
                if state == "released_live":
                    self.inventory["sessions"].append({"sessionId": descendant["session"], "cwd": "/elsewhere"})
                if state == "released":
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])
                else:
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                    self.assertEqual(caught.exception.code, "claim_conflict")
                    self.assertEqual(caught.exception.payload["coordination"][0]["claim_url"],
                                     "https://github.com/owner/repo/issues/44#issuecomment-52")
                    self.assert_no_writes()

    def test_released_family_old_parent_url_refuses_resume_and_allows_independent_work(self):
        for route, phase in product(("parent", "sibling"), ("preflight", "after_post", "after_status")):
            with self.subTest(route=route, phase=phase):
                self.setUp()
                self.parent_handoff_fixture()
                self.targets["41"]["id"] = 541
                inherited = {**OTHER, "session": "released-child", "branch": "work/released-child", "resume_from": "1",
                             "retained_handoff": "https://github.com/owner/repo/issues/41#issuecomment-3"}
                self.targets["43"] = {**copy.deepcopy(self.issue), "number": 43, "id": 543,
                                      "body": "## Current Status\n\n" + CLAIM.marker(inherited)}
                self.children["/repos/owner/repo/issues/41/sub_issues"].append(self.targets["43"])
                self.target_comments["/repos/owner/repo/issues/43/comments"] = [
                    {"id": 88, "body": CLAIM.marker(inherited), "user": {"login": TEST_BOT}, "created_at": inherited["claimed_at"]},
                    {"id": 89, "body": "Released claim 88", "user": {"login": TEST_BOT}, "created_at": "2026-10-02T00:00:00Z"}]
                if route == "parent":
                    self.issue = self.targets.pop("41")
                    self.comments = self.target_comments.pop("/repos/owner/repo/issues/41/comments")
                    self.args.issue, self.args.handoff_comment = "41", None
                    self.args.wait_resolved = "Independent source follow-through; qualification wait remains"
                    source_branch = CLAIM.records(self.comments[0]["body"])[0]["branch"]
                    self.pulls[0]["head"]["ref"] = source_branch
                    self.inventory.update(worktrees=[], local_branches=[], remote_branches=[])
                def rename_history():
                    inherited["retained_handoff"] = "https://github.com/old/repo/issues/41#issuecomment-3"
                    self.targets["43"]["body"] = "## Current Status\n\n" + CLAIM.marker(inherited)
                    self.target_comments["/repos/owner/repo/issues/43/comments"][0]["body"] = CLAIM.marker(inherited)
                if phase == "preflight": rename_history()
                else: setattr(self, phase, rename_history)
                read_api = self.read_api
                def read(method, path, **kwargs):
                    if path == "/repos/old/repo/issues/41":
                        return "bot", {"id": 541, "number": 41}  # Redirect identifies the parent, not the child.
                    return read_api(method, path, **kwargs)
                with patch.object(self, "read_api", side_effect=read):
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                    self.assertEqual(caught.exception.code, "claim_resume_lineage_invalid")
                    if phase == "preflight":
                        self.assert_no_writes()
                        self.issue = {**copy.deepcopy(self.issue), "number": 46, "id": 546, "labels": [],
                                      "body": "## Current Status\n\nState: Open, not started."}
                        self.comments = []
                        self.args.issue, self.args.resume_from, self.args.handoff_comment = "46", None, None
                        self.args.branch = "work/independent-46"
                        with patch.object(self, "read_api", side_effect=read) as reads:
                            self.run_claim()
                        self.assertTrue(self.emitted.call_args.args[0]["ok"])
                        self.assertNotIn("retained_handoff", self.emitted.call_args.args[0]["claim"])
                        self.assertFalse(any(call.args[1] == "/repos/old/repo/issues/41" for call in reads.call_args_list))
                    else:
                        self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])

    def test_transitive_family_follows_later_plain_claims_and_native_history(self):
        for state in ("active", "released", "native_ended", "released_live", "incomplete"):
            with self.subTest(state=state):
                self.setUp()
                self.nested_handoff_fixture()
                inherited = {**OTHER, "session": "leaf-old", "branch": "work/leaf-old",
                             "resume_from": "88", "retained_handoff": "https://github.com/owner/repo/issues/43#issuecomment-89"}
                plain = {**OTHER, "session": "leaf-plain", "branch": "work/leaf-plain"}
                self.targets["44"] = {**copy.deepcopy(self.issue), "number": 44,
                                      "body": "## Current Status\n\n" + CLAIM.marker(plain)}
                comments = [
                    {"id": 90, "body": CLAIM.marker(inherited), "user": {"login": TEST_BOT}, "created_at": inherited["claimed_at"]},
                    {"id": 91, "body": "Released claim 90", "user": {"login": TEST_BOT}, "created_at": "2026-10-02T00:00:00Z"},
                    {"id": 92, "body": CLAIM.marker(plain), "user": {"login": TEST_BOT}, "created_at": plain["claimed_at"]}]
                if state in {"released", "released_live", "incomplete"}:
                    comments.append({"id": 93, "body": "Released claim 92", "user": {"login": TEST_BOT},
                                     "created_at": "2026-10-02T00:00:00Z"})
                self.target_comments["/repos/owner/repo/issues/44/comments"] = comments
                self.timelines["/repos/owner/repo/issues/43/timeline"] = [
                    {"event": "sub_issue_added", "sub_issue": self.targets["44"]}]
                self.inventory["worktrees"].append({"branch": plain["branch"], "path": "/leaf-peer"})
                if state == "native_ended":
                    self.inventory["session_states"] = {plain["session"]: {"state": "ended", "ended_at": "2026-10-02T00:00:00Z"}}
                if state == "released_live":
                    self.inventory["sessions"].append({"sessionId": "foreign-peer", "cwd": "/leaf-peer/subdir"})
                if state == "incomplete":
                    self.observe_inventory = lambda inv, observed_comments: ({**inv, "session_coverage": {
                        "codex_claims": {"observed_source_incomplete": True}}}
                        if any(c.get("id") == 92 for c in observed_comments) else inv)
                if state == "released":
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])
                else:
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                    self.assertEqual(caught.exception.code, "claim_conflict")
                    if state == "incomplete":
                        self.assertEqual(caught.exception.payload["coordination"][0]["work_record"],
                                         "https://github.com/owner/repo/issues/44")
                    else:
                        self.assertEqual(caught.exception.payload["coordination"][0]["claim_url"],
                                         "https://github.com/owner/repo/issues/44#issuecomment-92")
                    self.assert_no_writes()

    def test_transitive_family_unreadable_ancestor_and_malformed_intermediate_refuse(self):
        for failure, phase in product(("unreadable", "lineage", "history"), ("preflight", "after_post", "after_status")):
            with self.subTest(failure=failure, phase=phase):
                self.setUp()
                self.nested_handoff_fixture()
                failed = False
                def invalidate():
                    nonlocal failed
                    failed = True
                    if failure == "lineage":
                        comments = self.target_comments["/repos/owner/repo/issues/43/comments"]
                        record = CLAIM.records(comments[0]["body"])[0]
                        comments.extend([
                            {"id": 80, "body": CLAIM.marker({**record, "resume_from": "unknown", "session": "malformed-history"}),
                             "user": {"login": TEST_BOT}, "created_at": record["claimed_at"]},
                            {"id": 81, "body": "Released claim 80", "user": {"login": TEST_BOT},
                             "created_at": "2026-10-02T00:00:00Z"}])
                    if failure == "history":
                        self.timelines["/repos/owner/repo/issues/41/timeline"] = [{"event": "sub_issue_added"}]
                if phase == "preflight": invalidate()
                else: setattr(self, phase, invalidate)
                get_issue = self.get_issue
                def read(ref, repo):
                    if failed and failure == "unreadable" and str(ref) == "41":
                        raise PLAN.PlanError("Ancestor unreadable", api_result={"status": 403})
                    return get_issue(ref, repo)
                with patch.object(self, "get_issue", side_effect=read):
                    with self.assertRaises(PLAN.PlanError) as caught: self.run_claim()
                if failure == "unreadable": self.assertEqual(caught.exception.api_result["status"], 403)
                else: self.assertEqual(caught.exception.code, "claim_resume_lineage_invalid" if failure == "lineage" else "claim_child_history_invalid")
                if phase == "preflight": self.assert_no_writes()
                else: self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])

    def test_transitive_family_cycles_terminate_without_hiding_ownership(self):
        for active in (False, True):
            with self.subTest(active=active):
                self.setUp()
                self.nested_handoff_fixture()
                cycle = {**OTHER, "session": "cycle-session", "branch": "work/cycle",
                         "resume_from": "88", "retained_handoff": "https://github.com/owner/repo/issues/43#issuecomment-89"}
                root_comments = self.target_comments["/repos/owner/repo/issues/41/comments"]
                root_comments.extend([
                    {"id": 70, "body": CLAIM.marker(cycle), "user": {"login": TEST_BOT}, "created_at": cycle["claimed_at"]},
                    {"id": 71, "body": "Released claim 70", "user": {"login": TEST_BOT}, "created_at": "2026-10-02T00:00:00Z"}])
                self.timelines["/repos/owner/repo/issues/43/timeline"] = [
                    {"event": "sub_issue_added", "sub_issue": self.targets["41"]}]
                if active:
                    self.inventory["sessions"].append({"sessionId": cycle["session"], "cwd": "/elsewhere"})
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                    self.assertEqual(caught.exception.code, "claim_conflict")
                    self.assertEqual(caught.exception.payload["coordination"][0]["claim_url"],
                                     "https://github.com/owner/repo/issues/41#issuecomment-70")
                    self.assert_no_writes()
                else:
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_transitive_family_self_identity_is_contextual(self):
        for alias in ("current", "proved", "claimed_issue"):
            with self.subTest(alias=alias):
                self.setUp()
                self.nested_handoff_fixture()
                self.issue["id"] = 542
                historic = {**OTHER, "session": "historic-middle", "branch": "work/historic-middle", "resume_from": "obsolete",
                            "retained_handoff": ("https://github.com/owner/repo/issues/43#issuecomment-10" if alias == "current"
                                                 else "https://github.com/old/repo/issues/7#issuecomment-10")}
                self.target_comments["/repos/owner/repo/issues/43/comments"].extend([
                    {"id": 80, "body": CLAIM.marker(historic), "user": {"login": TEST_BOT}, "created_at": historic["claimed_at"]},
                    {"id": 81, "body": "Released claim 80", "user": {"login": TEST_BOT}, "created_at": "2026-10-02T00:00:00Z"}])
                read_api = self.read_api
                def read(method, path, **kwargs):
                    if path == "/repos/old/repo/issues/7":
                        return "bot", {"id": self.targets["43"]["id"] if alias == "proved" else self.issue["id"]}
                    return read_api(method, path, **kwargs)
                with patch.object(self, "read_api", side_effect=read):
                    if alias == "claimed_issue":
                        with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                        self.assertEqual(caught.exception.code, "claim_resume_lineage_invalid")
                        self.assert_no_writes()
                    else:
                        self.run_claim()
                        self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_transitive_family_traverses_the_claimed_node_to_its_descendants(self):
        self.nested_handoff_fixture()
        inherited = {**OTHER, "session": "leaf-old", "branch": "work/leaf-old", "resume_from": "88",
                     "retained_handoff": "https://github.com/owner/repo/issues/43#issuecomment-89"}
        self.comments = [
            {"id": 90, "body": CLAIM.marker(inherited), "user": {"login": TEST_BOT}, "created_at": inherited["claimed_at"]},
            {"id": 91, "body": "Released claim 90", "user": {"login": TEST_BOT}, "created_at": "2026-10-02T00:00:00Z"}]
        self.args.resume_from, self.args.handoff_comment = None, None  # Independent fresh claim still sees inherited family.
        descendant = {**OTHER, "session": "descendant", "branch": "work/descendant", "resume_from": "90",
                      "retained_handoff": "https://github.com/owner/repo/issues/42#issuecomment-91"}
        self.targets["44"] = {**copy.deepcopy(self.issue), "number": 44,
                              "body": "## Current Status\n\n" + CLAIM.marker(descendant)}
        self.children["/repos/owner/repo/issues/42/sub_issues"] = [self.targets["44"]]
        self.target_comments["/repos/owner/repo/issues/44/comments"] = [
            {"id": 92, "body": CLAIM.marker(descendant), "user": {"login": TEST_BOT}, "created_at": descendant["claimed_at"]}]
        # No retained artifacts are requested by the fresh claim.
        self.pulls, self.closed_pulls = [], {}
        self.inventory.update(worktrees=[], local_branches=[], remote_branches=[])
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
        self.assertEqual(caught.exception.code, "claim_conflict")
        self.assertEqual(caught.exception.payload["coordination"][0]["claim_url"],
                         "https://github.com/owner/repo/issues/44#issuecomment-92")
        self.assert_no_writes()

    def test_parent_handoff_refuses_an_unreleased_native_child_successor(self):
        for claimant in ("sibling", "parent", "parent_without_handoff"):
            with self.subTest(claimant=claimant):
                self.setUp()
                self.parent_handoff_fixture()
                successor = {**OWNER, "worker": "first-child", "session": "child-session",
                             "branch": "work/source-followup", "resume_from": str(self.args.resume_from),
                             "retained_handoff": f"{self.parent['html_url']}#issuecomment-{self.args.handoff_comment}"}
                self.targets["43"] = {**copy.deepcopy(self.issue), "number": 43,
                                      "body": "## Current Status\n\n" + CLAIM.marker(successor)}
                self.children["/repos/owner/repo/issues/41/sub_issues"].append(self.targets["43"])
                self.target_comments["/repos/owner/repo/issues/43/comments"] = [
                    {"id": 88, "body": CLAIM.marker(successor), "user": {"login": TEST_BOT}}]
                if claimant.startswith("parent"):
                    self.issue = self.targets.pop("41")
                    self.comments = self.target_comments.pop("/repos/owner/repo/issues/41/comments")
                    self.args.issue = "41"
                    self.args.wait_resolved = "Source-only follow-through; preserve parent qualification waits"
                if claimant == "parent_without_handoff":
                    self.args.handoff_comment = None
                    self.pulls[0]["head"]["ref"] = CLAIM.records(self.comments[0]["body"])[0]["branch"]
                    self.inventory.update(worktrees=[], local_branches=[], remote_branches=[])
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assertEqual(caught.exception.payload["competing_evidence"][0]["record"], successor)
                self.assert_no_writes()

    def test_parent_handoff_rechecks_new_sibling_ownership_on_both_readbacks(self):
        for phase in ("after_post", "after_status"):
            with self.subTest(phase=phase):
                self.setUp()
                self.parent_handoff_fixture()
                successor = {**OTHER, "session": "sibling-session", "resume_from": str(self.args.resume_from),
                             "retained_handoff": f"{self.parent['html_url']}#issuecomment-{self.args.handoff_comment}"}
                def race():
                    sibling = {**copy.deepcopy(self.issue), "number": 43,
                               "body": "## Current Status\n\n" + CLAIM.marker(successor)}
                    self.targets["43"] = sibling
                    self.children["/repos/owner/repo/issues/41/sub_issues"].append(sibling)
                    self.target_comments["/repos/owner/repo/issues/43/comments"] = [
                        {"id": 88, "body": CLAIM.marker(successor), "user": {"login": TEST_BOT}}]
                setattr(self, phase, race)
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])

    def test_detached_native_child_remains_visible_until_its_claim_ends(self):
        fixture = json.loads(Path(__file__).with_name("fixtures").joinpath(
            "launchplane-3948-subissue-timeline.json").read_text())
        # Replay the captured REST event unchanged, including its child identity.
        child = fixture["events"][0]["sub_issue"]
        peer_number, peer_repo = child["number"], child["repository"]["full_name"]
        for phase in ("preflight", "after_post", "after_status"):
            for ended in (False, True):
                with self.subTest(phase=phase, ended=ended):
                    self.setUp()
                    self.parent_handoff_fixture()
                    successor = {**OTHER, "session": "detached-child-session", "branch": "work/detached-source",
                                 "resume_from": str(self.args.resume_from),
                                 "retained_handoff": f"{self.parent['html_url']}#issuecomment-{self.args.handoff_comment}"}
                    sibling = {**copy.deepcopy(child), "body": "## Current Status\n\n" + CLAIM.marker(successor)}
                    comments = [{"id": 88, "body": CLAIM.marker(successor), "user": {"login": TEST_BOT},
                                 "created_at": successor["claimed_at"]}]
                    if ended:
                        comments.append({"id": 89, "body": "Released claim 88", "user": {"login": TEST_BOT},
                                         "created_at": "2026-10-02T00:00:00Z"})
                    def detached():
                        self.targets[str(peer_number)] = sibling
                        self.target_comments[f"/repos/{peer_repo}/issues/{peer_number}/comments"] = comments
                        self.timelines["/repos/owner/repo/issues/41/timeline"] = fixture["events"]
                    if phase == "preflight": detached()
                    else: setattr(self, phase, detached)
                    if ended:
                        self.run_claim()
                        self.assertTrue(self.emitted.call_args.args[0]["ok"])
                    else:
                        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                            self.run_claim()
                        self.assertEqual(caught.exception.code, "claim_conflict")
                        self.assertEqual(caught.exception.payload["competing_evidence"][0]["record"], successor)
                        self.assertEqual(caught.exception.payload["coordination"][0]["claim_url"],
                                         f"https://github.com/{peer_repo}/issues/{peer_number}#issuecomment-88")
                        if phase == "preflight": self.assert_no_writes()
                        else: self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])

    def test_unreadable_or_malformed_child_history_blocks_both_resume_routes(self):
        for route, status in product(("child_handoff", "plain_resume"), (403, 404, "malformed")):
            with self.subTest(route=route, status=status):
                self.setUp()
                self.parent_handoff_fixture()
                self.timelines["/repos/owner/repo/issues/41/timeline"] = [
                    {"event": "sub_issue_added", "sub_issue": {"number": 43, "state": "open",
                     "repository": {"full_name": "owner/repo"}}}]
                if status == "malformed":
                    self.timelines["/repos/owner/repo/issues/41/timeline"][0]["sub_issue"] = None
                if route == "plain_resume":
                    self.issue = self.targets.pop("41")
                    self.comments = self.target_comments.pop("/repos/owner/repo/issues/41/comments")
                    self.args.issue, self.args.handoff_comment = "41", None
                    self.args.wait_resolved = "Separate source follow-through; qualification wait stays on the parent"
                    self.pulls[0]["head"]["ref"] = CLAIM.records(self.comments[0]["body"])[0]["branch"]
                    self.inventory.update(worktrees=[], local_branches=[], remote_branches=[])
                get_issue = self.get_issue
                def unreadable_child(ref, repo):
                    if str(ref) == "43":
                        raise PLAN.PlanError("Child unreadable", api_result={"status": status})
                    return get_issue(ref, repo)
                with patch.object(self, "get_issue", side_effect=unreadable_child):
                    with self.assertRaises(PLAN.PlanError) as caught:
                        self.run_claim()
                    if status == "malformed":
                        self.assertEqual(caught.exception.code, "claim_child_history_invalid")
                    else:
                        self.assertEqual(caught.exception.api_result["status"], status)
                    self.assert_no_writes()

    def test_parent_handoff_checks_child_ownership_after_plain_resume(self):
        for phase, state in product(("preflight", "after_post", "after_status"),
                                    ("same_branch", "fresh_branch", "released", "native_ended", "released_live")):
            with self.subTest(phase=phase, state=state):
                self.setUp()
                self.parent_handoff_fixture()
                inherited = {**OTHER, "session": "first-child", "branch": "work/source-followup",
                             "resume_from": str(self.args.resume_from),
                             "retained_handoff": f"{self.parent['html_url']}#issuecomment-{self.args.handoff_comment}"}
                # Plain same-record resume has no source/handoff metadata, and can use a fresh branch.
                resumed = {**OTHER, "worker": "second-child", "session": "next-child",
                           "branch": inherited["branch"] if state == "same_branch" else "work/source-next",
                           "claimed_at": "2026-10-03T00:00:00Z"}
                comments = [
                    {"id": 88, "body": CLAIM.marker(inherited), "user": {"login": TEST_BOT},
                     "created_at": inherited["claimed_at"]},
                    {"id": 89, "body": "Released claim 88", "user": {"login": TEST_BOT},
                     "created_at": "2026-10-02T00:00:00Z"},
                    {"id": 90, "body": CLAIM.marker(resumed), "user": {"login": TEST_BOT},
                     "created_at": resumed["claimed_at"]}]
                if state in {"released", "released_live"}:
                    comments.append({"id": 91, "body": "Released claim 90", "user": {"login": TEST_BOT},
                                     "created_at": "2026-10-04T00:00:00Z"})
                def successor():
                    sibling = {**copy.deepcopy(self.issue), "number": 43,
                               "body": "## Current Status\n\n" + CLAIM.marker(resumed)}
                    self.targets["43"] = sibling
                    self.children["/repos/owner/repo/issues/41/sub_issues"].append(sibling)
                    self.target_comments["/repos/owner/repo/issues/43/comments"] = comments
                    if state == "native_ended":
                        self.inventory["session_states"] = {resumed["session"]: {
                            "state": "ended", "ended_at": "2026-10-04T00:00:00Z"}}
                    if state == "released_live":
                        self.inventory["sessions"] = [{"sessionId": resumed["session"], "cwd": "/another-host"}]
                if phase == "preflight": successor()
                else: setattr(self, phase, successor)
                if state == "released":
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])
                else:
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                        self.run_claim()
                    self.assertEqual(caught.exception.code, "claim_conflict")
                    self.assertEqual(caught.exception.payload["coordination"][0]["claim_url"],
                                     "https://github.com/owner/repo/issues/43#issuecomment-90")
                    if phase == "preflight": self.assert_no_writes()
                    else: self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])

    def test_parent_plain_resume_checks_children_of_every_parent_claim(self):
        for phase, selected, history in product(("preflight", "after_post", "after_status"),
                                                ("original", "later"), ("current_url", "old_url", "missing_claim")):
            with self.subTest(phase=phase, selected=selected, history=history):
                self.setUp()
                self.parent_handoff_fixture()
                self.issue = self.targets.pop("41")
                self.comments = self.target_comments.pop("/repos/owner/repo/issues/41/comments")
                original = CLAIM.records(self.comments[0]["body"])[0]
                later = {**OTHER, "worker": "root-resumer", "session": "root-resume-session",
                         "branch": original["branch"], "claimed_at": "2026-10-03T00:00:00Z"}
                self.comments.extend([
                    {"id": 90, "body": CLAIM.marker(later), "user": {"login": TEST_BOT}, "created_at": later["claimed_at"]},
                    {"id": 91, "body": "Released claim 90", "user": {"login": TEST_BOT},
                     "created_at": "2026-10-04T00:00:00Z"}])
                self.args.issue, self.args.handoff_comment = "41", None
                self.args.resume_from = 1 if selected == "original" else 90
                self.issue["body"] = "## Current Status\n\n" + CLAIM.marker(original if selected == "original" else later)
                self.issue["labels"] = []
                self.children["/repos/owner/repo/issues/41/sub_issues"] = []
                for pull in [*self.pulls, *self.closed_pulls.values()]: pull["head"]["ref"] = original["branch"]
                self.inventory.update(local_branches=[original["branch"]], remote_branches=[original["branch"]],
                                      worktrees=[{"branch": original["branch"], "path": "/retained/root"}])
                child_source, child_handoff = (90, 91) if selected == "original" else (1, 3)
                if history == "missing_claim":
                    self.comments = [c for c in self.comments if c["id"] not in (
                        {90, 91} if selected == "original" else {1, 2, 3})]
                handoff_repo = "old/repo" if history == "old_url" else "owner/repo"
                successor = {**OTHER, "session": "child-holder", "branch": "work/child-source",
                             "resume_from": str(child_source),
                             "retained_handoff": f"https://github.com/{handoff_repo}/issues/41#issuecomment-{child_handoff}"}
                def child():
                    self.targets["43"] = {**copy.deepcopy(self.issue), "number": 43,
                                          "body": "## Current Status\n\n" + CLAIM.marker(successor)}
                    self.children["/repos/owner/repo/issues/41/sub_issues"].append(self.targets["43"])
                    self.target_comments["/repos/owner/repo/issues/43/comments"] = [
                        {"id": 88, "body": CLAIM.marker(successor), "user": {"login": TEST_BOT}}]
                if phase == "preflight": child()
                else: setattr(self, phase, child)
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assertEqual(caught.exception.payload["coordination"][0]["claim_url"],
                                 "https://github.com/owner/repo/issues/43#issuecomment-88")
                if phase == "preflight": self.assert_no_writes()
                else: self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])

    def test_child_resume_rechecks_the_original_source_family(self):
        for phase, holder, route, own_session in product(
                ("preflight", "after_post", "after_status"),
                ("parent", "parent_unrelated", "sibling", "none", "foreign_tree", "source_live", "source_unreleased"),
                ("plain", "handoff", "fresh", "refresh"),
                ("absent", "requester", "source_requester", "observed_requester")):
            with self.subTest(phase=phase, holder=holder, route=route, own_session=own_session):
                self.setUp()
                self.parent_handoff_fixture()
                inherited = {**OTHER, "session": "first-child", "branch": "work/source-followup",
                             "resume_from": str(self.args.resume_from),
                             "retained_handoff": f"{self.parent['html_url']}#issuecomment-{self.args.handoff_comment}"}
                self.issue["number"], self.args.issue = 43, "43"
                self.issue["body"] = "## Current Status\n\n" + CLAIM.marker(inherited)
                self.children["/repos/owner/repo/issues/41/sub_issues"] = [copy.deepcopy(self.issue)]
                self.comments = [
                    {"id": 88, "body": CLAIM.marker(inherited), "user": {"login": TEST_BOT},
                     "created_at": inherited["claimed_at"]},
                    {"id": 89, "body": "Released claim 88", "user": {"login": TEST_BOT},
                     "created_at": "2026-10-02T00:00:00Z"}]
                self.args.resume_from, self.args.handoff_comment = 88, None
                if route in {"handoff", "refresh"}:
                    self.args.handoff_comment = 89
                    self.comments[-1]["body"] += "\n\nSource handoff: PR #99 and #100 are reviewed; work is finished."
                    for pull in [*self.pulls, *self.closed_pulls.values()]:
                        pull["body"] += "\nRefs #43"
                if route == "refresh":
                    self.args.refresh_pr = "https://github.com/owner/repo/pull/99"
                if route == "fresh":
                    self.args.resume_from = None
                if own_session not in {"absent", "observed_requester"}:
                    if own_session == "source_requester":
                        self.args.session = CLAIM.records(
                            self.target_comments["/repos/owner/repo/issues/41/comments"][0]["body"])[0]["session"]
                    self.inventory["sessions"] = [{"sessionId": self.args.session,
                                                   "cwd": self.inventory["worktrees"][1]["path"]}]
                    self.inventory["session_states"] = {self.args.session: {"state": "live"}}
                if own_session == "observed_requester":
                    def observed(inv, _comments):
                        return {**inv, "sessions": [*inv["sessions"], {"sessionId": self.args.session,
                                "cwd": inv["worktrees"][1]["path"]}],
                                "session_states": {**inv.get("session_states", {}), self.args.session: {"state": "live"}}}
                    self.observe_inventory = observed
                resumed_elsewhere = {**inherited, "session": "new-source-holder", "branch": "work/source-elsewhere"}
                def successor():
                    if holder in {"parent", "parent_unrelated"}:
                        parent_claim = (resumed_elsewhere if holder == "parent" else
                                        {key: value for key, value in resumed_elsewhere.items()
                                         if key not in {"resume_from", "retained_handoff"}})
                        self.target_comments["/repos/owner/repo/issues/41/comments"].append(
                            {"id": 90, "body": CLAIM.marker(parent_claim), "user": {"login": TEST_BOT}})
                    elif holder == "sibling":
                        sibling = {**copy.deepcopy(self.issue), "number": 44,
                                   "body": "## Current Status\n\n" + CLAIM.marker(resumed_elsewhere)}
                        self.targets["44"] = sibling
                        self.children["/repos/owner/repo/issues/41/sub_issues"].append(sibling)
                        self.target_comments["/repos/owner/repo/issues/44/comments"] = [
                            {"id": 90, "body": CLAIM.marker(resumed_elsewhere), "user": {"login": TEST_BOT}}]
                    elif holder == "foreign_tree":
                        self.inventory["sessions"].append({"sessionId": "foreign-peer",
                                                           "cwd": self.inventory["worktrees"][1]["path"]})
                    elif holder == "source_live":
                        earlier = {**OTHER, "session": "earlier-source", "branch": "work/source-earlier"}
                        self.target_comments["/repos/owner/repo/issues/41/comments"].extend([
                            {"id": 70, "body": CLAIM.marker(earlier), "user": {"login": TEST_BOT},
                             "created_at": earlier["claimed_at"]},
                            {"id": 71, "body": "Released claim 70", "user": {"login": TEST_BOT},
                             "created_at": "2026-10-02T00:00:00Z"}])
                        self.inventory["sessions"].append({"sessionId": earlier["session"], "cwd": "/elsewhere"})
                    elif holder == "source_unreleased":
                        self.target_comments["/repos/owner/repo/issues/41/comments"].pop()
                if phase == "preflight": successor()
                else: setattr(self, phase, successor)
                if holder == "none":
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])
                    self.inventory["local_branches"].append(self.args.branch)
                    self.inventory["worktrees"].append({"branch": self.args.branch, "path": "/resumed-child"})
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])
                    self.assertEqual(len(self.comments), 3)
                    continue
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                if holder == "foreign_tree" and route in {"handoff", "refresh"} and phase != "preflight":
                    # Existing retained-artifact readback rejects this activity
                    # before the inherited-family check, on the claimed record.
                    self.assertIn("claude_session", [entry["source"] for entry in
                                  caught.exception.payload["competing_evidence"]])
                    self.assertEqual(caught.exception.payload["coordination"][0]["work_record"],
                                     "https://github.com/owner/repo/issues/43")
                    self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])
                    continue
                record_number = 44 if holder == "sibling" else self.parent["number"]
                claim_id = (70 if holder == "source_live" else
                            self.target_comments["/repos/owner/repo/issues/41/comments"][0]["id"]
                            if holder in {"foreign_tree", "source_unreleased"} else 90)
                self.assertEqual(caught.exception.payload["coordination"][0]["claim_url"],
                                 f"https://github.com/owner/repo/issues/{record_number}#issuecomment-{claim_id}")
                if phase == "preflight": self.assert_no_writes()
                else: self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])

    def test_cross_repository_refresh_checks_the_planning_source_family(self):
        for phase, scope, activity in product(("preflight", "after_post", "after_status"),
                                               ("root", "child", "grandchild", "ancestor"),
                                               ("planning", "runtime", "none", "planning_requester", "runtime_requester")):
            with self.subTest(phase=phase, scope=scope, activity=activity):
                self.setUp()
                self.cross_repository_fixture()
                source = CLAIM.records(self.comments[0]["body"])[0]
                source.update(resume_from="70", retained_handoff="https://github.com/other/plans/issues/41#issuecomment-71")
                self.comments[0]["body"] = CLAIM.marker(source)
                root = {**OTHER, "session": "planning-source", "branch": "work/planning-source"}
                self.targets["41"] = {**copy.deepcopy(self.issue), "number": 41,
                                      "body": "## Current Status\n\n" + CLAIM.marker(root)}
                self.target_comments["/repos/other/plans/issues/41/comments"] = [
                    {"id": 70, "body": CLAIM.marker(root), "user": {"login": TEST_BOT}, "created_at": root["claimed_at"]},
                    {"id": 71, "body": "Released claim 70", "user": {"login": TEST_BOT},
                     "created_at": "2026-10-02T00:00:00Z"}]
                self.children["/repos/other/plans/issues/41/sub_issues"] = [{**copy.deepcopy(self.issue), "repo": "other/plans"}]
                holder, holder_number, holder_claim = root, 41, 70
                if scope == "ancestor":
                    root.update(resume_from="60", retained_handoff="https://github.com/other/plans/issues/40#issuecomment-61")
                    self.targets["41"]["repo"] = "other/plans"
                    self.targets["41"]["body"] = "## Current Status\n\n" + CLAIM.marker(root)
                    self.target_comments["/repos/other/plans/issues/41/comments"][0]["body"] = CLAIM.marker(root)
                    holder = {**OTHER, "session": "ancestor-source", "branch": "work/ancestor-source"}
                    holder_number, holder_claim = 40, 60
                    self.targets["40"] = {**copy.deepcopy(self.issue), "repo": "other/plans", "number": 40,
                                          "body": "## Current Status\n\n" + CLAIM.marker(holder)}
                    self.target_comments["/repos/other/plans/issues/40/comments"] = [
                        {"id": 60, "body": CLAIM.marker(holder), "user": {"login": TEST_BOT}, "created_at": holder["claimed_at"]},
                        {"id": 61, "body": "Released claim 60", "user": {"login": TEST_BOT}, "created_at": "2026-10-02T00:00:00Z"}]
                    self.children["/repos/other/plans/issues/40/sub_issues"] = [self.targets["41"]]
                if scope in {"child", "grandchild"}:
                    holder = {**OTHER, "session": "child-source", "branch": "work/child-source",
                              "resume_from": str(self.args.resume_from),
                              "retained_handoff": "https://github.com/other/plans/issues/42#issuecomment-3"}
                    holder_number, holder_claim = 44, 88
                    self.targets["44"] = {**copy.deepcopy(self.issue), "repo": "other/plans", "number": 44,
                                          "body": "## Current Status\n\n" + CLAIM.marker(holder)}
                    self.children["/repos/other/plans/issues/42/sub_issues"] = [self.targets["44"]]
                    self.target_comments["/repos/other/plans/issues/44/comments"] = [
                        {"id": 88, "body": CLAIM.marker(holder), "user": {"login": TEST_BOT}, "created_at": holder["claimed_at"]},
                        {"id": 89, "body": "Released claim 88", "user": {"login": TEST_BOT},
                         "created_at": "2026-10-02T00:00:00Z"}]
                if scope == "grandchild":
                    holder = {**OTHER, "session": "grandchild-source", "branch": "work/grandchild-source",
                              "resume_from": "88", "retained_handoff": "https://github.com/other/plans/issues/44#issuecomment-89"}
                    holder_number, holder_claim = 45, 90
                    self.targets["45"] = {**copy.deepcopy(self.issue), "repo": "other/plans", "number": 45,
                                          "body": "## Current Status\n\n" + CLAIM.marker(holder)}
                    self.children["/repos/other/plans/issues/44/sub_issues"] = [self.targets["45"]]
                    self.target_comments["/repos/other/plans/issues/45/comments"] = [
                        {"id": 90, "body": CLAIM.marker(holder), "user": {"login": TEST_BOT}, "created_at": holder["claimed_at"]},
                        {"id": 91, "body": "Released claim 90", "user": {"login": TEST_BOT}, "created_at": "2026-10-02T00:00:00Z"}]
                def live():
                    inv = self.planning_inventory if activity.startswith("planning") else self.inventory
                    if activity != "none":
                        path = "/planning/source-tree" if activity.startswith("planning") else "/runtime/source-tree"
                        inv["worktrees"].append({"branch": holder["branch"], "path": path})
                        inv["sessions"].append({"sessionId": self.args.session if activity.endswith("requester") else "foreign-peer",
                                                "cwd": path})
                if phase == "preflight": live()
                else: setattr(self, phase, live)
                if activity == "none" or activity.endswith("requester"):
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])
                else:
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                        self.run_claim()
                    self.assertEqual(caught.exception.code, "claim_conflict")
                    self.assertEqual(caught.exception.payload["coordination"][0]["claim_url"],
                                     f"https://github.com/other/plans/issues/{holder_number}#issuecomment-{holder_claim}")
                    if phase == "preflight": self.assert_no_writes()
                    else: self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])

    def test_plain_resume_refuses_malformed_inherited_lineage(self):
        for malformed in ({"retained_handoff": 7}, {"resume_from": "unknown"},
                          {"retained_handoff": "https://github.com/other/repo/issues/41#issuecomment-3"}):
            with self.subTest(malformed=malformed):
                self.setUp()
                self.parent_handoff_fixture()
                inherited = {**OTHER, "resume_from": str(self.args.resume_from),
                             "retained_handoff": f"{self.parent['html_url']}#issuecomment-{self.args.handoff_comment}",
                             **malformed}
                self.comments = [
                    {"id": 88, "body": CLAIM.marker(inherited), "user": {"login": TEST_BOT}},
                    {"id": 89, "body": "Released claim 88", "user": {"login": TEST_BOT}}]
                self.args.resume_from, self.args.handoff_comment = 88, None
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "claim_resume_lineage_invalid")
                self.assert_no_writes()

    def test_same_record_handoff_history_uses_issue_identity_before_lineage(self):
        for route, identity, phase in product(
                ("plain", "handoff"),
                ("current", "renamed", "transferred", "other_issue", "missing_id", "both_ids_missing", "unreadable"),
                ("preflight", "after_post", "after_status")):
            with self.subTest(route=route, identity=identity, phase=phase):
                self.setUp()
                if route == "handoff": self.ordinary_handoff_fixture()
                else: self.released_status_fixture()
                self.issue["id"] = 500
                source = CLAIM.records(self.comments[0]["body"])[0]
                source.update(resume_from="obsolete", retained_handoff=(
                    "https://github.com/owner/repo/issues/42#issuecomment-2" if identity == "current" else
                    f"https://github.com/old/repo/issues/{7 if identity == 'transferred' else 42}#issuecomment-2"))
                self.comments[0]["body"] = CLAIM.marker(source)
                self.issue["body"] = self.issue["body"].replace(CLAIM.marker(OTHER), CLAIM.marker(source))
                alias = {"id": self.issue["id"], "number": self.issue["number"]}
                failed = False
                def invalidate():
                    nonlocal failed
                    failed = True
                    if identity == "both_ids_missing":
                        self.issue.pop("id", None)
                        alias.pop("id", None)
                if identity in {"other_issue", "missing_id", "both_ids_missing", "unreadable"}:
                    if phase == "preflight": invalidate()
                    else: setattr(self, phase, invalidate)
                read_api = self.read_api
                def read_alias(method, path, **kwargs):
                    if path.startswith("/repos/old/repo/issues/"):
                        if failed and identity == "unreadable":
                            raise PLAN.PlanError("Alias unreadable", api_result={"status": 403})
                        if failed and identity == "other_issue": return "bot", {**alias, "id": alias["id"] + 1}
                        if failed and identity == "missing_id": return "bot", {"number": alias["number"]}
                        return "bot", copy.deepcopy(alias)
                    return read_api(method, path, **kwargs)
                with patch.object(self, "read_api", side_effect=read_alias):
                    if identity in {"current", "renamed", "transferred"}:
                        self.run_claim()
                        self.assertTrue(self.emitted.call_args.args[0]["ok"])
                    else:
                        with self.assertRaises(PLAN.PlanError) as caught:
                            self.run_claim()
                        if identity == "unreadable": self.assertEqual(caught.exception.api_result["status"], 403)
                        else: self.assertEqual(caught.exception.code, "claim_resume_lineage_invalid")
                        if phase == "preflight": self.assert_no_writes()
                        else: self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])

    def test_unreadable_old_self_reference_allows_only_independent_fresh_work(self):
        for status in (403, 404):
            with self.subTest(status=status):
                self.setUp()
                self.released_status_fixture()
                self.issue["id"] = 500
                source = {**OTHER, "resume_from": "1",
                          "retained_handoff": "https://github.com/old/repo/issues/42#issuecomment-2"}
                self.comments[0]["body"] = CLAIM.marker(source)
                self.issue["body"] = self.issue["body"].replace(CLAIM.marker(OTHER), CLAIM.marker(source))
                read_api = self.read_api
                def unreadable(method, path, **kwargs):
                    if path.startswith("/repos/old/repo/issues/"):
                        raise PLAN.PlanError("Old reference unreadable", api_result={"status": status})
                    return read_api(method, path, **kwargs)
                with patch.object(self, "read_api", side_effect=unreadable) as reads:
                    with self.assertRaises(PLAN.PlanError) as caught: self.run_claim()
                    self.assertEqual(caught.exception.api_result["status"], status)
                    self.assert_no_writes()
                    reads.reset_mock()
                    self.args.resume_from = None
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])
                    self.assertNotIn("resume_from", self.emitted.call_args.args[0]["claim"])
                    self.assertNotIn("retained_handoff", self.emitted.call_args.args[0]["claim"])
                    self.assertNotEqual(self.emitted.call_args.args[0]["claim"]["branch"], source["branch"])
                    self.assertFalse(any(call.args[1].startswith("/repos/old/repo/issues/") for call in reads.call_args_list))

    def test_native_child_history_errors_refuse_before_writing(self):
        for failure in ("malformed", "unreadable"):
            with self.subTest(failure=failure):
                self.setUp()
                self.parent_handoff_fixture()
                if failure == "malformed":
                    self.timelines["/repos/owner/repo/issues/41/timeline"] = [{"event": "sub_issue_added"}]
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                        self.run_claim()
                    self.assertEqual(caught.exception.code, "claim_child_history_invalid")
                else:
                    read_pages = self.read_pages
                    def unavailable(path, **kwargs):
                        if path.endswith("/timeline"):
                            raise PLAN.PlanError("History unreadable", api_result={"status": 403})
                        return read_pages(path, **kwargs)
                    with patch.object(self, "read_pages", side_effect=unavailable):
                        with self.assertRaises(PLAN.PlanError) as caught:
                            self.run_claim()
                    self.assertEqual(caught.exception.api_result["status"], 403)
                self.assert_no_writes()

    def test_parent_artifact_refusals_coordinate_on_the_source_record(self):
        for artifact in ("branch", "unmentioned_pr"):
            with self.subTest(artifact=artifact):
                self.setUp()
                self.parent_handoff_fixture()
                if artifact == "branch":
                    self.inventory["local_branches"].append("work/issue-41-unknown")
                else:
                    self.pulls.append({**self.pulls[0], "number": 101})
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assertEqual(caught.exception.payload["coordination"][0]["work_record"], self.parent["html_url"])
                self.assert_no_writes()

    def test_parent_handoff_distinguishes_ended_unrelated_and_live_child_work(self):
        for state in ("released", "native_ended", "unrelated", "released_live", "released_live_tree", "incomplete",
                      "closed_unreleased", "closed_released"):
            with self.subTest(state=state):
                self.setUp()
                self.parent_handoff_fixture()
                successor = {**OTHER, "worker": "first-child", "session": "child-session", "branch": "work/source-followup",
                             "resume_from": str(self.args.resume_from),
                             "retained_handoff": f"{self.parent['html_url']}#issuecomment-{self.args.handoff_comment}"}
                if state == "unrelated":
                    successor["resume_from"] = "12345"
                    successor["retained_handoff"] = f"https://github.com/owner/repo/issues/{self.parent['number']}0#issuecomment-12346"
                sibling = {**copy.deepcopy(self.issue), "number": 43,
                           "body": "## Current Status\n\n" + CLAIM.marker(successor)}
                if state.startswith("closed_"):
                    sibling["state"] = "closed"
                self.targets["43"] = sibling
                self.children["/repos/owner/repo/issues/41/sub_issues"].append(sibling)
                comments = [{"id": 88, "body": CLAIM.marker(successor), "user": {"login": TEST_BOT},
                             "created_at": successor["claimed_at"]}]
                if state not in {"native_ended", "unrelated", "closed_unreleased"}:
                    comments.append({"id": 89, "body": "Released claim 88", "user": {"login": TEST_BOT},
                                     "created_at": "2026-10-02T00:00:00Z"})
                self.target_comments["/repos/owner/repo/issues/43/comments"] = comments
                self.inventory["local_branches"].append(successor["branch"])
                self.inventory["worktrees"].append({"branch": successor["branch"], "path": "/sibling"})
                if state == "native_ended":
                    self.inventory["session_states"] = {successor["session"]: {"state": "ended", "ended_at": "2026-10-02T00:00:00Z"}}
                if state.startswith("released_live"):
                    self.inventory["sessions"] = [{"sessionId": successor["session"] if state == "released_live" else "peer",
                                                   "cwd": "/elsewhere" if state == "released_live" else "/sibling/subdir"}]
                if state == "incomplete":
                    self.observe_inventory = lambda inv, observed_comments: ({**inv, "session_coverage": {
                        "codex_claims": {"observed_source_incomplete": True}}}
                        if any(c.get("id") == 88 for c in observed_comments) else inv)
                if state in {"released", "unrelated", "closed_released"}:
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])
                else:
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                        self.run_claim()
                    self.assertEqual(caught.exception.code, "claim_conflict")
                    expected = ("native_session_inventory" if state == "incomplete" else
                                "current_status" if state in {"closed_unreleased", "native_ended"} else "native_child_session")
                    self.assertIn(expected, [entry["source"] for entry in caught.exception.payload["competing_evidence"]])
                    self.assert_no_writes()

    def test_parent_handoff_checks_other_released_parent_sessions(self):
        for phase in ("preflight", "after_post", "after_status"):
            with self.subTest(phase=phase):
                self.setUp()
                self.parent_handoff_fixture()
                earlier = {**OTHER, "session": "earlier-session", "branch": "work/earlier-source"}
                self.target_comments["/repos/owner/repo/issues/41/comments"][:0] = [
                    {"id": 70, "body": CLAIM.marker(earlier), "user": {"login": TEST_BOT}, "created_at": earlier["claimed_at"]},
                    {"id": 71, "body": "Released claim 70", "user": {"login": TEST_BOT}, "created_at": "2026-10-02T00:00:00Z"}]
                def live_peer():
                    self.inventory["sessions"] = [{"sessionId": earlier["session"], "cwd": "/elsewhere"}]
                if phase == "preflight": live_peer()
                else: setattr(self, phase, live_peer)
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assertEqual(caught.exception.payload["competing_evidence"][0]["session"], earlier["session"])
                self.assertEqual(caught.exception.payload["coordination"][0]["claim_url"],
                                 "https://github.com/owner/repo/issues/41#issuecomment-70")
                if phase == "preflight": self.assert_no_writes()
                else: self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])

    def test_parent_handoff_accounts_for_its_own_ended_artifacts(self):
        for proof in ("release", "native"):
            with self.subTest(proof=proof):
                self.setUp()
                self.parent_handoff_fixture()
                earlier = {**OTHER, "session": "earlier-session", "branch": "work/issue-41-earlier"}
                source = {"id": 70, "body": CLAIM.marker(earlier), "user": {"login": TEST_BOT},
                          "created_at": earlier["claimed_at"]}
                history = [source]
                if proof == "release":
                    history.append({"id": 71, "body": "Released claim 70", "user": {"login": TEST_BOT},
                                    "created_at": "2026-10-02T00:00:00Z"})
                else:
                    self.inventory["session_states"] = {earlier["session"]: {
                        "state": "ended", "ended_at": "2026-10-02T00:00:00Z"}}
                self.target_comments["/repos/owner/repo/issues/41/comments"][:0] = history
                self.inventory["local_branches"].append(earlier["branch"])
                self.inventory["remote_branches"].append(earlier["branch"])
                self.inventory["worktrees"].append({"branch": earlier["branch"], "path": "/old-source"})
                before = copy.deepcopy(self.inventory)
                if proof == "release":
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])
                else:
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                        self.run_claim()
                    self.assertEqual(caught.exception.code, "claim_conflict")
                    self.assert_no_writes()
                self.assertEqual(self.inventory, before)

    def test_native_parent_resume_does_not_expand_conflict_only_refresh(self):
        self.parent_handoff_fixture()
        self.args.refresh_pr = "https://github.com/owner/repo/pull/99"
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.code, "claim_resume_source_missing")
        self.assert_no_writes()

    def test_parent_resume_preserves_release_handoff_and_ownership_refusals(self):
        self.parent_handoff_fixture()
        self.run_claim()  # Establish the supported route before testing its independent refusals.
        conflicts = {"missing_pr_link": "open_pr", "live_source": "source_session", "live_tree": "claude_session",
                     "other_claim": "comment", "child_claim": "comment", "unmentioned_pr": "open_pr",
                     "parent_status": "current_status"}
        reasons = {"no_link": "claim_native_parent_required", "foreign_parent": "claim_native_parent_required",
                   "no_release": "claim_resume_source_unreleased", "foreign_release": "claim_resume_source_unreleased",
                   "foreign_handoff": "claim_handoff_author_mismatch", "child_blocker": "claim_native_blockers_unresolved",
                   "no_handoff": "claim_resume_source_missing", "wrong_parent": "claim_resume_source_missing"}
        for change in ("no_link", "unreadable_link", "foreign_parent", "no_release", "foreign_release",
                       "foreign_handoff", "missing_pr_link", "live_source", "live_tree", "other_claim",
                       "child_claim", "unmentioned_pr", "child_blocker", "child_wait", "no_handoff",
                       "wrong_parent", "parent_status", "retained_pr_wait"):
            with self.subTest(change=change):
                self.setUp()
                self.parent_handoff_fixture()
                source = self.target_comments["/repos/owner/repo/issues/41/comments"]
                if change == "no_link": self.parent = None
                if change == "wrong_parent":
                    self.parent["number"] = 40
                    self.targets["40"] = {**copy.deepcopy(self.issue), "number": 40}
                    self.target_comments["/repos/owner/repo/issues/40/comments"] = []
                if change == "no_handoff": self.args.handoff_comment = None
                if change == "unreadable_link": self.api_failure = {"status": 403}
                if change == "foreign_parent": self.parent["html_url"] = "https://github.com/other/repo/issues/41"
                if change == "no_release": source[-1]["body"] = "Source handoff: PR #99 and #100"
                if change == "foreign_release": source[-1]["user"] = {"login": "stranger"}
                if change == "foreign_handoff":
                    source.append({"id": 4, "body": source[-1]["body"], "user": {"login": "stranger"}})
                    self.args.handoff_comment = 4
                if change == "missing_pr_link": self.pulls[0]["body"] = "Repair another issue"
                if change == "live_source": self.inventory["sessions"] = [{"sessionId": OTHER["session"], "cwd": "/elsewhere"}]
                if change == "live_tree": self.inventory["sessions"] = [{"sessionId": "peer", "cwd": self.inventory["worktrees"][0]["path"]}]
                if change == "other_claim": source.append({"id": 4, "body": CLAIM.marker({**OTHER, "session": "third"}), "user": {"login": TEST_BOT}})
                if change == "child_claim": self.comments.append({"id": 88, "body": CLAIM.marker(OTHER), "user": {"login": TEST_BOT}})
                if change == "unmentioned_pr": self.pulls.append({**self.pulls[0], "number": 101})
                if change == "child_blocker": self.blockers = [{"state": "open"}]
                if change == "child_wait": self.wait = wait_record.reviewed(evidence="Child needs acceptance", waiting_for="Client", ends_when="Client accepts")
                if change == "parent_status": self.targets["41"]["body"] += "\n" + CLAIM.marker({**OTHER, "session": "third"})
                if change == "retained_pr_wait":
                    self.targets["99"]["labels"] = [{"name": PLAN.DEFAULT_CONFIG["labels"]["waiting"]}]
                    self.record_wait(target="99")
                with self.assertRaises(PLAN.PlanError) as caught:
                    self.run_claim()
                if change in conflicts:
                    self.assertEqual(caught.exception.code, "claim_conflict")
                    self.assertIn(conflicts[change], [entry["source"] for entry in caught.exception.payload["competing_evidence"]])
                elif change in {"child_wait", "retained_pr_wait"}:
                    self.assertEqual(caught.exception.code, "claim_wait_unresolved")
                elif change == "unreadable_link":
                    self.assertEqual(caught.exception.api_result["status"], 403)
                else:
                    self.assertEqual(caught.exception.code, reasons[change])
                self.assert_no_writes()

    def test_parent_resume_rechecks_native_link_and_proofs_on_both_readbacks(self):
        for phase in ("after_post", "after_status"):
            for change in ("link", "reparent", "release", "parent_claim", "source_peer", "child_peer"):
                with self.subTest(phase=phase, change=change):
                    self.setUp()
                    self.parent_handoff_fixture()
                    def race():
                        source = self.target_comments["/repos/owner/repo/issues/41/comments"]
                        if change == "link": self.parent = None
                        if change == "reparent":
                            assert self.parent is not None
                            self.parent["number"] = 40
                        if change == "release": source[-1]["body"] = "Source handoff: PR #99 and #100"
                        if change == "parent_claim": source.append({"id": 80, "body": CLAIM.marker({**OTHER, "session": "third"}), "user": {"login": TEST_BOT}})
                        if change == "source_peer": self.inventory["sessions"] = [{"sessionId": OTHER["session"], "cwd": "/elsewhere"}]
                        if change == "child_peer": self.compete()
                    setattr(self, phase, race)
                    with self.assertRaises(PLAN.PlanError) as caught:
                        self.run_claim()
                    self.assertIn("post", self.events)
                    self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])
                    if change in {"parent_claim", "source_peer", "child_peer"}:
                        self.assertEqual(caught.exception.code, "claim_conflict")
                        expected = "source_session" if change == "source_peer" else "comment"
                        self.assertIn(expected, [entry["source"] for entry in caught.exception.payload["competing_evidence"]])
                    else:
                        reason = {"link": "claim_native_parent_required", "reparent": "claim_native_parent_changed",
                                  "release": "claim_resume_source_unreleased"}[change]
                        self.assertEqual(caught.exception.code, reason)

    def cleanup_handoff_fixture(self):
        self.ordinary_handoff_fixture()
        # Historical full release from launchplane#3109 comment 6025762772.
        # Remap only record/PR identities to the offline claim fixture.
        release = Path(__file__).with_name("fixtures").joinpath("launchplane-3109-d3-release.md").read_text()
        source_id = int(release.splitlines()[0].removeprefix("Released claim "))
        self.assertEqual(CLAIM.released_claim_id(release), source_id)
        release = release.replace("6025344226", "1").replace("#3111", "#99").replace("/pull/3111", "/pull/99")
        release = release.replace("#3110", "#100")
        self.assertEqual(CLAIM.released_claim_id(release), self.args.resume_from)
        self.comments[2]["body"] = release

    def test_cleanup_handoff_accepts_ordinary_successor_without_retiring_artifacts(self):
        self.cleanup_handoff_fixture()
        before = copy.deepcopy((self.inventory, self.pulls, self.closed_pulls))
        self.run_claim()
        output = self.emitted.call_args.args[0]
        self.assertTrue(output["ok"])
        self.assertIn("metadata_readback", output["completed_steps"])
        self.assertEqual(before, (self.inventory, self.pulls, self.closed_pulls))

    def test_ordinary_handoff_preserves_issue_and_every_retained_pr_wait(self):
        for place in ("issue", "99", "100"):
            with self.subTest(place=place):
                self.setUp()
                self.ordinary_handoff_fixture()
                wait = wait_record.reviewed(evidence="Director requested prerequisite readback", waiting_for="Approved prerequisite readback", ends_when="Director accepts proof")
                if place == "issue":
                    self.wait = wait
                    self.issue["labels"] = [{"name": PLAN.DEFAULT_CONFIG["labels"]["waiting"]}]
                else:
                    self.target_comments[f"/repos/owner/repo/issues/{place}/comments"].append({"id": 88, "user": {"login": TEST_BOT}, "body": wait_record.marker(wait)})
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "claim_wait_unresolved")
                self.assert_no_writes()
                self.args.wait_resolved = "Existing brief authorizes prerequisite only; keep the draft/readback hold."
                self.run_claim()
                self.assertTrue(self.pulls[0]["draft"])
                self.assertEqual(len(wait_record.records(self.comments[-1]["body"])), 1)
                if place == "issue":
                    status, _ = PLAN.read_plan_sections(self.issue)
                    self.assertEqual(wait_record.records(status["Current Status"]), [wait])
                    self.assertIn(PLAN.DEFAULT_CONFIG["labels"]["waiting"], PLAN.normalize_labels(self.issue["labels"]))
                    self.assertEqual(PLAN.next_plan_status(self.issue, PLAN.DEFAULT_CONFIG), "waiting")
                else:
                    self.assertIn(f"https://github.com/owner/repo/pull/{place}", self.comments[-1]["body"])
                    self.assertIn(wait_record.marker(wait), self.issue["body"])

    def test_ordinary_recovery_records_newly_verified_pr_wait_in_current_status(self):
        self.ordinary_handoff_fixture()
        self.run_claim()
        self.targets["99"]["labels"] = [{"name": PLAN.DEFAULT_CONFIG["labels"]["waiting"]}]
        self.record_wait(target="99")
        self.args.wait_resolved = "Fresh authorization for this step; preserve the new PR hold."
        self.run_claim()
        self.assertEqual(self.events.count("post"), 1)
        self.assertIn("> Labels: " + PLAN.DEFAULT_CONFIG["labels"]["waiting"], self.issue["body"])
        self.assertIn(self.args.wait_resolved, self.issue["body"])

    def test_ordinary_handoff_checks_visible_source_session_outside_retained_tree(self):
        self.ordinary_handoff_fixture()
        source_session = CLAIM.records(self.comments[0]["body"])[0]["session"]
        self.inventory["sessions"] = [{"sessionId": source_session, "cwd": "/elsewhere"}]
        with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
        self.assert_no_writes()

    def test_native_blocker_added_on_either_readback_still_refuses(self):
        for phase in ("after_post", "after_status"):
            with self.subTest(phase=phase):
                self.setUp()
                self.ordinary_handoff_fixture()
                setattr(self, phase, lambda: self.blockers.append({"number": 1209, "state": "open"}))
                with self.assertRaises(PLAN.PlanError) as caught: self.run_claim()
                self.assertIn("native blockers", str(caught.exception))
                self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])
                if phase == "after_post": self.assertNotIn("status", self.events)

    def test_ordinary_handoff_refuses_new_wait_label_on_either_readback(self):
        for phase in ("after_post", "after_status"):
            with self.subTest(phase=phase):
                self.setUp()
                self.ordinary_handoff_fixture()
                self.args.wait_resolved = "Existing prerequisite authorization."
                setattr(self, phase, lambda: self.issue["labels"].append({"name": PLAN.DEFAULT_CONFIG["labels"]["blocked"]}))
                with self.assertRaises(PLAN.PlanError) as caught: self.run_claim()
                self.assertIn("holds changed", str(caught.exception))
                self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])

    def test_ordinary_handoff_ignores_unrelated_pr_status_progress_during_readback(self):
        self.ordinary_handoff_fixture()
        self.targets["99"]["body"] += "\n\n## Current Status\n\nWaiting for: prerequisite.\nLast verified: yesterday"
        self.args.wait_resolved = "Prerequisite step authorized; hold remains."
        self.after_post = lambda: self.targets["99"].update(body=self.targets["99"]["body"].replace("yesterday", "today"))
        self.run_claim()

    def test_ordinary_handoff_preserves_hold_added_during_slow_inventory_read(self):
        self.ordinary_handoff_fixture()
        self.args.wait_resolved = "Previously authorized prerequisite only."
        def concurrent_hold():
            if "post" in self.events:
                self.issue["body"] += "\n\n## Current Status\n\nWaiting for: New decision on prerequisite."
        self.during_inventory = concurrent_hold
        with self.assertRaises(PLAN.PlanError) as caught: self.run_claim()
        self.assertIn("Issue body changed", str(caught.exception))
        self.assertIn("Waiting for: New decision on prerequisite.", self.issue["body"])
        self.assertNotIn("status", self.events)
        self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])

    def test_ordinary_handoff_does_not_copy_released_marker_as_hold_continuation(self):
        self.ordinary_handoff_fixture()
        self.comments[0]["created_at"] = self.comments[0]["updated_at"] = "2026-10-01T00:00:00Z"
        self.comments[2]["created_at"] = "2026-10-02T00:00:00Z"
        source = CLAIM.records(self.comments[0]["body"])[0]
        self.issue["body"] = (self.issue["body"].split("## Current Status", 1)[0]
                              + "## Current Status\n\nWaiting for: Owner review.\n" + CLAIM.marker(source))
        self.wait = wait_record.reviewed(evidence="Director review requested", waiting_for="Owner review", ends_when="Current head accepted")
        self.args.wait_resolved = "Prerequisite authorized; retain the review hold."
        self.run_claim()
        self.assertEqual(CLAIM.records(self.issue["body"]), [self.emitted.call_args.args[0]["claim"]])
        self.assertEqual(wait_record.records(self.issue["body"]), [self.wait])

    def test_ordinary_handoff_refuses_changed_holds_on_each_readback(self):
        for phase in ("after_post", "after_status"):
            for place in ("issue", "99", "100"):
                with self.subTest(phase=phase, place=place):
                    self.setUp()
                    self.ordinary_handoff_fixture()
                    self.args.wait_resolved = "Prior prerequisite approval; no authority for a new hold."
                    def race():
                        record = {"id": 900, "user": {"login": TEST_BOT}, "body": wait_record.marker(wait_record.reviewed(evidence="New Director request", waiting_for="New product prerequisite", ends_when="Director accepts"))}
                        if place == "issue":
                            self.comments.append(record)
                        else:
                            self.target_comments[f"/repos/owner/repo/issues/{place}/comments"].append(record)
                    setattr(self, phase, race)
                    with self.assertRaises(PLAN.PlanError) as caught: self.run_claim()
                    self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])
                    if phase == "after_post": self.assertNotIn("status", self.events)

    def test_ordinary_handoff_preserves_native_blocker(self):
        self.ordinary_handoff_fixture()
        self.args.wait_resolved = "Approved prerequisite step; not resolution of source blocker."
        self.blockers = [{"state": "open", "number": 1209}]
        with self.assertRaises(PLAN.PlanError): self.run_claim()
        self.assert_no_writes()

    def test_ordinary_handoff_rejects_unverified_source_and_artifacts(self):
        for change in ("no_release", "wrong_claim", "foreign_release", "generic_handoff",
                       "missing_pr", "foreign_pr", "missing_link", "fork", "closed_unmerged",
                       "new_pr", "extra_branch", "new_task_uses_source", "new_task_uses_second"):
            with self.subTest(change=change):
                self.setUp()
                self.ordinary_handoff_fixture()
                if change == "no_release": self.comments[-1]["body"] = "Handoff from trial-b\nclaim 1, session-b, #99 #100"
                if change == "wrong_claim": self.comments[-1]["body"] = "Released claim 9\n#99 #100"
                if change == "foreign_release": self.comments[-1]["user"]["login"] = "stranger"
                if change == "generic_handoff":
                    self.comments.append({"id": 4, "body": "Generic bot rollup: #99 #100", "user": {"login": TEST_BOT}})
                    self.args.handoff_comment = 4
                if change == "missing_pr": self.comments[-1]["body"] = "Released claim 1\nPR #100"
                if change == "foreign_pr": self.pulls[0]["user"]["login"] = "stranger"
                if change == "missing_link": self.pulls[0]["body"] = "No issue reference"
                if change == "fork": self.pulls[0]["head"]["repo"]["full_name"] = "stranger/repo"
                if change == "closed_unmerged":
                    unmerged = self.pulls.pop()
                    unmerged.update(state="closed", merged_at=None)
                    self.closed_pulls[unmerged["number"]] = unmerged
                    self.targets[str(unmerged["number"])]["state"] = "closed"
                if change == "new_pr": self.pulls.append({**copy.deepcopy(self.pulls[0]), "number": 101})
                if change == "extra_branch": self.inventory["remote_branches"].append("work/issue-42-unmentioned")
                if change == "new_task_uses_source": self.args.branch = self.closed_pulls[100]["head"]["ref"]
                if change == "new_task_uses_second": self.args.branch = self.pulls[0]["head"]["ref"]
                with self.assertRaises(PLAN.PlanError): self.run_claim()
                self.assert_no_writes()

    def test_ordinary_handoff_checks_claims_and_live_peers_on_each_branch(self):
        for place in ("issue", "99", "100", "peer_original", "peer_second", "peer_descendant"):
            with self.subTest(place=place):
                self.setUp()
                self.ordinary_handoff_fixture()
                if place == "issue": self.compete()
                elif place in ("99", "100"):
                    self.target_comments[f"/repos/owner/repo/issues/{place}/comments"] = [{"id": 40, "body": CLAIM.marker(OTHER)}]
                else:
                    index = 1 if place == "peer_original" else 0
                    cwd = self.inventory["worktrees"][index]["path"]
                    if place == "peer_descendant": cwd += "/src"
                    self.inventory["sessions"] = [{"sessionId": "live-peer", "cwd": cwd}]
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                self.assert_no_writes()

    def test_ordinary_handoff_readbacks_preserve_racing_claims_and_artifacts(self):
        for phase in ("after_post", "after_status"):
            for change in ("claim", "branch", "pr"):
                with self.subTest(phase=phase, change=change):
                    self.setUp()
                    self.ordinary_handoff_fixture()
                    def race():
                        if change == "claim":
                            self.target_comments["/repos/owner/repo/issues/100/comments"].append({"id": 40, "body": CLAIM.marker(OTHER)})
                        elif change == "branch":
                            self.inventory["remote_branches"].append("work/issue-42-race")
                        else:
                            self.pulls.append({"number": 101, "body": "Refs #42", "head": {"ref": "work/issue-42-race"}})
                    setattr(self, phase, race)
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                    recovery = caught.exception.payload["claim_recovery"]["release_own_claim"]
                    self.assertEqual(recovery["comment_id"], self.comments[-1]["id"])
                    self.assertEqual(recovery["body"], f"Released claim {self.comments[-1]['id']}")
                    if phase == "after_post": self.assertNotIn("status", self.events)

    def test_ordinary_handoff_recovery_cannot_drop_or_replace_proof(self):
        for change in ("handoff", "source", "plain", "refresh"):
            with self.subTest(change=change):
                self.setUp()
                self.ordinary_handoff_fixture()
                self.run_claim()
                self.run_claim()
                self.assertEqual(self.events.count("post"), 1)
                if change == "handoff": self.args.handoff_comment = 2
                if change == "source": self.args.resume_from = 2
                if change == "plain": self.args.handoff_comment = None
                if change == "refresh": self.args.refresh_pr = "https://github.com/owner/repo/pull/99"
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                self.assertEqual(self.events.count("post"), 1)

    def test_refresh_accepts_compact_pr_handoff_with_verified_source(self):
        # PR #<n> already worked; keep it as the compatibility control.
        for spelling in ("PR{number}", "PR #{number}"):
            with self.subTest(spelling=spelling):
                self.setUp()
                self.refresh_fixture()
                self.comments[-1]["body"] = (
                    "Released claim 1\n\nSource delivered "
                    + spelling.format(number=99) + " and " + spelling.format(number=100)
                    + "; Supervisor continues CI."
                )
                before = copy.deepcopy((self.pulls, self.inventory))
                self.run_claim()
                output = self.emitted.call_args.args[0]
                self.assertTrue(output["ok"])
                self.assertEqual(output["claim"]["refresh_pr"], self.args.refresh_pr)
                self.assertEqual(before, (self.pulls, self.inventory))

    def test_compact_pr_handoff_keeps_release_author_and_ownership_guards(self):
        for change in ("no_release", "foreign_handoff", "active_claim"):
            with self.subTest(change=change):
                self.setUp()
                self.refresh_fixture()
                self.comments[-1]["body"] = "Released claim 1\n\nSource delivered PR99 and PR100."
                self.assertEqual(CLAIM.handoff_pr_numbers(
                    self.comments[-1]["body"], issue_repo=self.args.repo, target_repo=self.args.repo), {99, 100})
                if change == "no_release":
                    self.comments[-1]["body"] = "Source delivered PR99 and PR100."
                elif change == "foreign_handoff":
                    self.comments[-1]["user"]["login"] = "stranger"
                else:
                    self.compete()
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                evidence = caught.exception.payload["competing_evidence"]
                self.assertEqual([(item["source"], item["id"]) for item in evidence],
                                 [("comment", 4 if change == "active_claim" else 1)])
                self.assert_no_writes()

    def test_compact_pr_handoff_is_local_and_does_not_match_other_identifiers(self):
        for text in ("PR99", "PR #99"):
            self.assertEqual(CLAIM.handoff_pr_numbers(text, issue_repo="owner/repo", target_repo="other/repo"), set())
        for text in ("somePR99", "PR99suffix", "other/repo/PR99", "PR0", "PR099",
                     "cs-pr99-split", "phase-PR99", "cs.pr99", "PR99-split"):
            with self.subTest(text=text):
                self.assertEqual(CLAIM.handoff_pr_numbers(text, issue_repo="owner/repo", target_repo="owner/repo"), set())

    def test_refresh_requires_all_evidence_flags(self):
        for flag in ("handoff_comment", "resume_from"):
            with self.subTest(flag=flag):
                self.setUp()
                self.refresh_fixture()
                setattr(self.args, flag, None)
                with self.assertRaises(PLAN.PlanError):
                    self.run_claim()
                self.assert_no_writes()

    def test_refresh_rejects_incomplete_or_foreign_handoff(self):
        for change in ("no_release", "foreign_release", "foreign_handoff", "missing_target", "foreign_pr", "unbound_handoff", "missing_link", "fork", "closed", "before_release"):
            with self.subTest(change=change):
                self.setUp()
                self.refresh_fixture()
                if change == "no_release": self.comments[-1]["body"] = "Source handoff: PR #99 and #100"
                if change == "foreign_release":
                    self.comments[-1]["body"] = "Released claim 1"
                    self.comments[-1]["user"]["login"] = "stranger"
                    self.comments.append({"id": 4, "body": "Source handoff: PR #99 and #100", "user": {"login": TEST_BOT}})
                    self.args.handoff_comment = 4
                if change == "foreign_handoff": self.comments[-1]["user"]["login"] = "stranger"
                if change == "missing_target": self.comments[-1]["body"] = "Released claim 1\nPR #100"
                if change == "foreign_pr": self.pulls[0]["user"]["login"] = "stranger"
                if change == "unbound_handoff":
                    self.comments.append({"id": 4, "body": "Unrelated handoff: PR #99 and #100", "user": {"login": TEST_BOT}})
                    self.args.handoff_comment = 4
                if change == "missing_link": self.pulls[0]["body"] = "No implementation reference"
                if change == "fork": self.pulls[0]["head"]["repo"]["full_name"] = "stranger/repo"
                if change == "closed": self.pulls.pop(0)
                if change == "before_release":
                    self.comments[1]["body"] += "\nPR #99"
                    self.args.handoff_comment = 2
                with self.assertRaises(PLAN.PlanError): self.run_claim()
                self.assert_no_writes()

    def test_refresh_retains_new_or_unaccounted_artifacts(self):
        self.refresh_fixture()
        self.pulls.append({"number": 101, "body": "Refs #42", "head": {"ref": "work/other-42"}})
        with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
        self.assert_no_writes()

    def test_refresh_active_ownership_refuses_on_issue_target_or_sibling(self):
        for place in ("issue", "target", "sibling", "session"):
            with self.subTest(place=place):
                self.setUp()
                self.refresh_fixture()
                if place == "issue": self.compete()
                if place in ("target", "sibling"):
                    n = 99 if place == "target" else 100
                    self.target_comments[f"/repos/owner/repo/issues/{n}/comments"] = [{"id": 40, "body": CLAIM.marker(OTHER)}]
                if place == "session":
                    self.inventory["sessions"] = [{"sessionId": "active-peer", "cwd": self.inventory["worktrees"][0]["path"], "name": "plain name", "state": "running"}]
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                self.assert_no_writes()

    def test_refresh_readback_race_releases_only_new_claim(self):
        self.refresh_fixture()
        self.after_post = lambda: self.target_comments["/repos/owner/repo/issues/99/comments"].append({"id": 40, "body": CLAIM.marker(OTHER)})
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
        self.assertEqual(caught.exception.payload["claim_recovery"]["release_own_claim"]["comment_id"], 4)
        self.assertNotIn("status", self.events)

    def test_refresh_readback_new_pr_is_competing_evidence(self):
        self.refresh_fixture()
        self.after_post = lambda: self.pulls.append({"number": 101, "body": "Refs #42", "head": {"ref": "work/new-42"}})
        with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
        self.assertNotIn("status", self.events)

    def test_refresh_metadata_recovery_preserves_scope(self):
        self.refresh_fixture()
        self.run_claim()
        self.run_claim()
        self.assertEqual(self.events.count("post"), 1)
        self.args.refresh_pr = self.args.handoff_comment = None
        with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()

    def test_success_records_and_reads_back_before_metadata(self):
        self.run_claim()
        self.assertLess(self.events.index("post"), self.events.index("status"))
        self.assertIn("read_comments", self.events[self.events.index("post") + 1:self.events.index("status")])
        self.assertIn("read_issue", self.events[self.events.index("labels") + 1:])
        self.assertIn("read_comments", self.events[self.events.index("labels") + 1:])
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

    def test_remote_codex_claim_detects_family_without_explicit_agent(self):
        self.args.agent = None
        self.args.session = "worker"
        self.issue["labels"] = [{"name": "agent:codex"}]
        inherited = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "supervisor",
                     "CODEX_THREAD_ID": "worker"}
        with patch.dict(os.environ, inherited, clear=True), patch.object(os, "getppid", return_value=10), \
                patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, "1 /opt/app-server/bin/codex\n", "")):
            self.run_claim()
        self.assertIn("post", self.events)
        self.assertIn("status", self.events)
        self.assertEqual(self.emitted.call_args.args[0]["claim"]["session"], self.args.session)

    def test_claim_refuses_a_session_label_that_is_not_the_running_native_id(self):
        nested = {"CLAUDE_CODE_SESSION_ID": "supervisor", "CODEX_THREAD_ID": "worker"}
        cases = (("claude", {"CLAUDE_CODE_SESSION_ID": "claude-native"}, "CS2072-C1", "CLAUDE_CODE_SESSION_ID=claude-native"),
                 ("codex", {"CODEX_THREAD_ID": "codex-thread"}, "CS2072-C1", "CODEX_THREAD_ID=codex-thread"),
                 # A worker never claims under its launching Supervisor's inherited ID.
                 ("codex", nested, "supervisor", "CODEX_THREAD_ID=worker"))
        for family, env, session, named in cases:
            self.setUp()
            self.args.agent, self.args.session = family, session
            with self.subTest(family=family, session=session), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "session_not_native")
                self.assertIn(named, str(caught.exception))
            self.assert_no_writes()

    def test_claim_accepts_native_ids_and_unreadable_ones(self):
        cases = (("claude", {"CLAUDE_CODE_SESSION_ID": "claude-native"}, "claude-native"),
                 ("codex", {"CODEX_THREAD_ID": "thread", "CODEX_SESSION_ID": "root"}, "thread"),
                 ("codex", {"CODEX_THREAD_ID": "thread", "CODEX_SESSION_ID": "root"}, "root"),
                 # Harnesses that expose no native ID keep claiming as before.
                 ("claude", {"CLAUDECODE": "1", "CODEX_THREAD_ID": "outer"}, "desktop-label"),
                 (None, {"CLAUDECODE": "1", "CODEX_THREAD_ID": "outer"}, "unidentified-harness"))
        for family, env, session in cases:
            self.setUp()
            self.args.agent, self.args.session = family, session
            with self.subTest(family=family, session=session), patch.dict(os.environ, env, clear=True), \
                    patch.object(github_agent, "ancestor_agent", return_value=None):
                self.run_claim()
                self.assertEqual(self.emitted.call_args.args[0]["claim"]["session"], session)

    def test_inner_claude_or_unknown_harness_cannot_claim_codex_assignment(self):
        for executable, expected in (("/opt/bin/claude", "claude"), ("/opt/claude/versions/2.1.296", None)):
            self.setUp()
            self.args.agent = None
            self.issue["labels"] = [{"name": "agent:codex"}]
            original_run = subprocess.run
            answers = iter([subprocess.CompletedProcess([], 0, f"20 {executable}\n", ""),
                            subprocess.CompletedProcess([], 0, "1 /opt/bin/codex\n", "")])

            def run(command, **kwargs):
                return next(answers) if command[0] == "ps" else original_run(command, **kwargs)

            with self.subTest(executable=executable), \
                    patch.dict(os.environ, {"CLAUDECODE": "1", "CODEX_THREAD_ID": "outer"}, clear=True), \
                    patch.object(os, "getppid", return_value=10), \
                    patch.object(subprocess, "run", side_effect=run):
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "agent_assignment_mismatch")
                self.assertEqual(caught.exception.payload["running_agent"], expected)
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

    def test_legacy_current_status_requires_review(self):
        self.wait = None
        self.issue["body"] += "\nWorker: another session\nBranch: work/repair\n"
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertTrue(caught.exception.payload["review_required"])
        self.assert_no_writes()

    def test_sweep_disclaimers_allow_a_fresh_claim(self):
        for status in SWEEP_DISCLAIMERS:
            with self.subTest(status=status):
                self.setUp()
                self.issue["body"] += "\n" + status
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])
                self.assertIn("post", self.events)

    def test_real_sweep_statuses_allow_claims_with_markdown(self):
        for status in (BD_SWEEP_STATUS, SWEEP_DISCLAIMERS[0],
                       "no implementation ownership is claimed by this sweep."):
            variants = (status,) if status == BD_SWEEP_STATUS else format_ownership(status)
            for formatted in variants:
                with self.subTest(status=formatted):
                    self.setUp()
                    self.issue["body"] += "\n" + formatted
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])
                    self.assertIn("metadata_readback", self.emitted.call_args.args[0]["completed_steps"])

    def test_formatted_legacy_claim_comments_still_refuse(self):
        for formatted in format_ownership("Claimed by another-worker\nSession: another-session"):
            if formatted.startswith("> "):
                continue
            with self.subTest(comment=formatted):
                self.setUp()
                self.issue["body"] += "\n" + BD_SWEEP_STATUS
                self.comments = [{"id": 1, "body": formatted, "user": {"login": TEST_BOT}}]
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                self.assert_no_writes()

        for comment in ("**Claimed by:** another-worker", "**claimed by:** unknown"):
            with self.subTest(comment=comment):
                self.setUp()
                self.comments = [{"id": 1, "body": comment, "user": {"login": TEST_BOT}}]
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                self.assert_no_writes()

    def test_quote_does_not_claim_on_behalf_of_comment_author(self):
        self.compete()
        self.comments += [
            {"id": 2, "body": "> **Claimed by another-worker**\n\nIs this still active?", "user": {"login": "reader"}},
            {"id": 3, "body": "Released claim 1"},
        ]
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_defined_reference_link_denials_allow_fresh_claim(self):
        for holder, definition in (("[this sweep][Prior Sweep]", "[prior sweep]"),
                                   ("[this sweep][]", "[THIS SWEEP]"),
                                   ("[this sweep]", "[THIS SWEEP]")):
            with self.subTest(holder=holder):
                self.setUp()
                self.issue["body"] += f"\nNot claimed by {holder}.\n\n{definition}: https://example.com/status"
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_legacy_holder_formatting_preserves_authored_release(self):
        for holder in (f"`{OTHER['worker']}`", f"**{OTHER['worker']}**", f"_{OTHER['worker']}_"):
            with self.subTest(holder=holder):
                self.setUp()
                self.comments = [
                    {"id": 1, "body": f"Claimed by {holder}", "user": {"login": TEST_BOT}},
                    {"id": 2, "body": f"Released by {holder}", "user": {"login": TEST_BOT}},
                ]
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])
                self.setUp()
                self.compete()
                self.comments.append({"id": 2, "body": f"Released by {holder}"})
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                self.assert_no_writes()

    def test_legacy_alias_preserves_distinct_sessions_and_exact_id_recovery(self):
        for holder in ("old-worker", "`old-worker`", "**old-worker**"):
            for structured_first in (False, True):
                with self.subTest(holder=holder, structured_first=structured_first):
                    self.setUp()
                    first = f"Claimed by {holder}\n\nSession: old-session-a\nBranch: work/old-a"
                    if structured_first:
                        first += "\n" + CLAIM.marker({**OTHER, "worker": "old-worker", "session": "old-session-a"})
                    self.comments = [
                        {"id": 1, "body": first, "user": {"login": TEST_BOT}},
                        {"id": 2, "body": f"Claimed by {holder}\n\nSession: old-session-b\nBranch: work/old-b",
                         "user": {"login": TEST_BOT}},
                        {"id": 3, "body": f"Released by {holder}", "user": {"login": TEST_BOT}},
                    ]
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                        self.run_claim()
                    self.assertEqual({1, 2}, {e.get("id") for e in caught.exception.payload["competing_evidence"]})
                    self.assert_no_writes()
                    self.comments.append({"id": 4, "body": "Released claim 1", "user": {"login": TEST_BOT}})
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                        self.run_claim()
                    self.assertEqual({2}, {e.get("id") for e in caught.exception.payload["competing_evidence"]})
                    self.assert_no_writes()
                    self.comments.append({"id": 5, "body": "Released claim 2", "user": {"login": TEST_BOT}})
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_legacy_alias_preserves_list_inline_and_missing_session_identity(self):
        for template in (
            "Claimed by old-worker\n- Session: {session}\n- Branch: work/old",
            "Claimed by old-worker Session: {session}\nBranch: work/old",
            "Claimed by old-worker\nSession: Codex desktop {session}\nBranch: work/old",
            "Claimed by old-worker\nBranch: work/old",
            *(f"Claimed by old-worker\nSession: {value}\nBranch: work/old"
              for value in ("", "\t", "unknown", "n/a", "-", "TBD", "<session>", "none", "unassigned",
                            "not recorded", "unavailable", "pending", "?", "(none)", "—", "<session-id>")),
        ):
            with self.subTest(template=template):
                self.setUp()
                self.comments = [
                    {"id": 1, "body": template.format(session="old-session-a"), "user": {"login": TEST_BOT}},
                    {"id": 2, "body": template.format(session="old-session-b"), "user": {"login": TEST_BOT}},
                    {"id": 3, "body": "Released by old-worker", "user": {"login": TEST_BOT}},
                ]
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual({1, 2}, {e.get("id") for e in caught.exception.payload["competing_evidence"]})
                self.assert_no_writes()
                self.comments.extend([
                    {"id": 4, "body": "Released claim 1", "user": {"login": TEST_BOT}},
                    {"id": 5, "body": "Released claim 2", "user": {"login": TEST_BOT}},
                ])
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_legacy_alias_still_releases_repeated_same_session(self):
        for session in ("old-session", "Codex desktop A"):
            with self.subTest(session=session):
                self.setUp()
                self.comments = [
                    {"id": 1, "body": f"Claimed by old-worker Session: {session}\nBranch: work/old",
                     "user": {"login": TEST_BOT}},
                    {"id": 2, "body": f"Claimed by **old-worker**\n**Session:** `{session}`\nBranch: work/old",
                     "user": {"login": TEST_BOT}},
                    {"id": 3, "body": "Released by old-worker", "user": {"login": TEST_BOT}},
                ]
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_legacy_alias_keeps_author_chronology_and_unknown_identity_guards(self):
        for extra, succeeds in (
            ({"id": 2, "body": "Claimed by old-worker\nSession: other-session", "user": {"login": "another-author"}}, False),
            ({"id": 2, "body": "Claimed by old-worker", "user": {"login": TEST_BOT}}, False),
            ({"id": 2, "body": "> Claimed by old-worker\n> Session: quoted-session", "user": {"login": TEST_BOT}}, True),
        ):
            with self.subTest(extra=extra):
                self.setUp()
                self.comments = [
                    {"id": 1, "body": "Claimed by old-worker\nSession: old-session", "user": {"login": TEST_BOT}},
                    extra,
                    {"id": 3, "body": "Released by old-worker", "user": {"login": TEST_BOT}},
                ]
                if succeeds:
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])
                else:
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                        self.run_claim()
                    expected = {2} if extra["user"]["login"] != TEST_BOT else {1, 2}
                    self.assertEqual(expected, {e.get("id") for e in caught.exception.payload["competing_evidence"]})
                    self.assert_no_writes()
        self.setUp()
        self.comments = [
            {"id": 1, "body": "Released by old-worker", "user": {"login": TEST_BOT}},
            {"id": 2, "body": "Claimed by old-worker\nSession: old-session", "user": {"login": TEST_BOT}},
        ]
        with self.assertRaises(PLAN.ClassifiedPlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_formatted_legacy_release_cannot_bypass_reused_worker_guard(self):
        for holder in (OTHER["worker"], f"`{OTHER['worker']}`", f"**{OTHER['worker']}**"):
            with self.subTest(holder=holder):
                self.setUp()
                self.comments = [
                    {"id": 1, "body": CLAIM.marker(OTHER), "user": {"login": TEST_BOT}},
                    {"id": 2, "body": CLAIM.marker({**OTHER, "session": "second-session"}), "user": {"login": TEST_BOT}},
                    {"id": 3, "body": "Released claim 1", "user": {"login": TEST_BOT}},
                    {"id": 4, "body": "Released claim 2", "user": {"login": TEST_BOT}},
                    {"id": 5, "body": f"Claimed by {OTHER['worker']}", "user": {"login": TEST_BOT}},
                    {"id": 6, "body": f"Released by {holder}", "user": {"login": TEST_BOT}},
                ]
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                self.assert_no_writes()

    def test_linked_denial_preserves_valid_destinations_and_titles(self):
        for destination in (
            'https://example.com/status', 'https://example.com/status_(prior)',
            'https://example.com/status "prior sweep"', "https://example.com/status 'prior sweep'",
            '<https://example.com/prior sweep>', '',
        ):
            with self.subTest(destination=destination):
                self.setUp()
                self.issue["body"] += f"\nNot claimed by [this sweep]({destination})."
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_formatted_denial_never_releases_structured_claims(self):
        for formatted in format_ownership("No implementation ownership is claimed by this sweep."):
            for source in ("status", "comment"):
                with self.subTest(status=formatted, source=source):
                    self.setUp()
                    self.issue["body"] += "\n" + formatted
                    if source == "status": self.issue["body"] += "\n" + CLAIM.marker(OTHER)
                    else: self.compete()
                    with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                    self.assert_no_writes()

    def test_disclaimer_does_not_clear_independent_ownership(self):
        for source in ("status_marker", "comment", "legacy_comment", "branch", "worktree", "pr"):
            with self.subTest(source=source):
                self.setUp()
                self.issue["body"] += "\n" + SWEEP_DISCLAIMERS[0]
                if source == "status_marker": self.issue["body"] += "\n" + CLAIM.marker(OTHER)
                elif source == "comment": self.compete()
                elif source == "legacy_comment":
                    self.comments = [{"id": 1, "body": "Claimed by another-worker\nSession: another-session"}]
                elif source == "branch": self.inventory["remote_branches"] = ["work/issue-42-other"]
                elif source == "worktree":
                    self.inventory["worktrees"] = [{"branch": "work/issue-42-other", "path": "/retained/repair"}]
                else: self.pulls = [{"number": 99, "state": "open", "body": "Refs #42", "head": {"ref": "work/repair"}}]
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assert_no_writes()

    def test_non_ownership_comment_is_not_a_claim_or_release(self):
        for comment in ("Claimed by no one\n\nThe sweep only reconciled stale waits.",
                        "No claims, implementation or worktrees. Not claimed by this sweep.",
                        "The sweep releases claim bookkeeping to a future worker."):
            with self.subTest(comment=comment):
                self.setUp()
                self.comments = [{"id": 1, "body": comment, "user": {"login": TEST_BOT}}]
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])
                self.setUp()
                self.compete()
                self.comments.append({"id": 2, "body": comment, "user": {"login": TEST_BOT}})
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                self.assert_no_writes()

    def test_matching_legacy_comment_preserves_second_holder(self):
        self.comments = [{"id": 1, "body": (
            f"Claimed by {OWNER['worker']}\nSession: {OWNER['session']}\nBranch: {OWNER['branch']}"
            "\nNo implementation ownership is claimed by this sweep. Worker: another-worker"
        )}]
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.code, "claim_conflict")
        self.assert_no_writes()

    def test_generated_identity_accepts_valid_punctuation_tokens(self):
        for token in ("claude-opus-5.5", "capacity;run", "capacity!run", "capacity?run", "worker__repair", "ci_worker", "run_session", "_worker"):
            for field in ("worker", "session"):
                with self.subTest(token=token, field=field):
                    self.setUp()
                    setattr(self.args, field, token)
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])
                    self.assertIn("metadata_readback", self.emitted.call_args.args[0]["completed_steps"])

    def test_common_sweep_negations_and_formatting_allow_fresh_claims(self):
        for status in (
            "This issue has not been claimed by the sweep.",
            "Implementation has never been owned by the audit session.",
            "This task isn't claimed by this sweep.",
            "This issue is not currently claimed by a worker.",
            "The repair is no longer owned by the audit session.",
            "No ownership claimed by this sweep.",
            "**Scope:** No implementation ownership is claimed by this sweep.",
            "Current status: No work is claimed by the sweep.",
            "Sweep note: No implementation ownership is claimed by this sweep.",
            "* No ownership is claimed by this sweep.",
            "Worker: nobody\nSession: no one",
            "**Not** claimed by this sweep.",
            "No longer owned by this sweep.",
            "No implementation ownership is claimed by the direction sweep.",
            "Not claimed by this sweep run.",
            "Unclaimed by this sweep.", "Work remains unowned by the sweep.",
            "Disowned by this sweep.",
        ):
            with self.subTest(status=status):
                self.setUp()
                self.issue["body"] += "\n" + status
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_crlf_current_status_supports_structured_and_legacy_recovery(self):
        for structured in (True, False):
            with self.subTest(structured=structured):
                self.setUp()
                self.run_claim()
                if not structured:
                    self.issue["body"] = "\n".join(line for line in self.issue["body"].splitlines()
                                                    if not line.startswith("<!-- " + CLAIM.MARKER))
                self.issue["body"] = self.issue["body"].replace("\n", "\r\n")
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])
                self.issue["body"] += "\r\n" + CLAIM.marker(OTHER)
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()

    def test_responsibility_status_with_released_claim_allows_new_claim(self):
        self.issue["body"] += "\n" + RESPONSIBILITY_STATUS
        self.comments = [
            {"id": 1, "body": "Claimed by " + OTHER["worker"] + "\n" + CLAIM.marker(OTHER),
             "user": {"login": TEST_BOT}},
            {"id": 2, "body": "Released claim 1", "user": {"login": TEST_BOT}},
        ]
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])
        self.assertIn("post", self.events)

    def test_responsibility_status_does_not_release_real_claims(self):
        self.issue["body"] += "\n" + RESPONSIBILITY_STATUS
        self.compete()
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.payload["competing_evidence"][0]["source"], "comment")
        self.assert_no_writes()

    def test_scoped_record_and_resource_responsibility_is_not_ownership(self):
        for status in (
            "Remaining records are owned by engineering for evidence and Chris for disposition approval",
            "Remaining resources are owned by engineering for evidence and Chris for production disposition approval.",
            "Entries owned by another-worker for evidence and Chris for disposition approval.",
        ):
            with self.subTest(status=status):
                conflicts, owned = CLAIM.discussion_evidence(status, [], OWNER)
                self.assertEqual(conflicts, [])
                self.assertEqual(owned, [])

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

    def test_codex_982_claim_ignores_1398_review_context(self):
        self.args.repo = "cbusillo/codex-skills"
        self.args.issue = "982"
        self.issue["number"] = 982
        self.pulls = [{"number": 1398, "title": "fix: preserve repo-relative rollout memory paths",
                       "body": "Refs #1351\n\nAn unrelated intermittent local-model output-limit failure "
                               "recurred, then passed standalone and in the full catalog rerun; "
                               "[#982](https://github.com/cbusillo/codex-skills/issues/982#issuecomment-6051054495) owns it.",
                       "head": {"ref": "work/cs-1351-d1"}}]
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])
        self.assertEqual(self.post_route, ("issue", 982, self.args.repo))

    def test_launchplane_3109_successor_ignores_3132_direction_context(self):
        self.ordinary_handoff_fixture()
        self.args.repo = "cbusillo/launchplane"
        self.args.issue = "3109"
        self.issue["number"] = 3109
        for name in ("comments", "pulls", "targets", "target_comments", "closed_pulls"):
            value = json.loads(json.dumps(getattr(self, name)).replace("owner/repo", self.args.repo).replace("#42", "#3109"))
            if name == "closed_pulls": value = {int(key): item for key, item in value.items()}
            setattr(self, name, value)
        self.pulls.append({"number": 3132, "title": "Direction: product-scoped grants and live production lanes",
                           "body": "Only `DIRECTION.md` changes.\n\nRuntime/docs qualification stays on "
                                   "[launchplane#3109](https://github.com/cbusillo/launchplane/issues/3109) scoped reads/plans "
                                   "and [launchplane#2467](https://github.com/cbusillo/launchplane/issues/2467) ordinary operation.\n\n"
                                   "Refs https://github.com/cbusillo/direction/issues/43",
                           "head": {"ref": "work/dir-43-d1"}})
        retained = copy.deepcopy((self.pulls, self.closed_pulls, self.inventory))
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])
        self.assertEqual(self.post_route, ("issue", 3109, self.args.repo))
        self.assertEqual((self.pulls, self.closed_pulls, self.inventory), retained)

    def test_contextual_pr_keeps_genuine_competing_pr_refusal(self):
        self.pulls = [{"number": 99, "body": "Review notes: https://github.com/owner/repo/issues/42 owns it.",
                       "head": {"ref": "work/docs"}},
                      {"number": 100, "body": "Fixes #42", "head": {"ref": "work/repair"}}]
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.code, "claim_conflict")
        self.assertEqual(caught.exception.payload["competing_evidence"],
                         [{"source": "open_pr", "number": 100, "branch": "work/repair"}])
        self.assert_no_writes()

    def test_implementation_reference_list_keeps_each_competing_issue(self):
        for body in (
            "Refs https://github.com/owner/repo/issues/10, https://github.com/owner/repo/issues/42",
            "Fixes #10 and https://github.com/owner/repo/issues/42",
            "Closes [first](https://github.com/other/repo/issues/10), and [repair](https://github.com/owner/repo/issues/42)",
            "Refs #10, #42",
        ):
            with self.subTest(body=body):
                self.setUp()
                self.pulls = [{"number": 99, "body": body, "head": {"ref": "work/repair"}}]
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.payload["competing_evidence"],
                                 [{"source": "open_pr", "number": 99, "branch": "work/repair"}])
                self.assert_no_writes()

    def test_unrelated_implementation_list_does_not_claim_context_link(self):
        self.pulls = [{"number": 99,
                       "body": "Refs #10, #11 and other/repo#42\n\nRuntime/docs qualification stays on https://github.com/owner/repo/issues/42",
                       "head": {"ref": "work/docs"}}]
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_retained_handoff_keeps_contextual_issue_links_and_waits(self):
        self.ordinary_handoff_fixture()
        self.pulls[0]["body"] = "Planning issue: https://github.com/owner/repo/issues/42"
        self.targets["99"]["body"] = self.pulls[0]["body"]
        self.target_comments["/repos/owner/repo/issues/99/comments"].append({"id": 88, "user": {"login": TEST_BOT}, "body": wait_record.marker(wait_record.reviewed(evidence="Owner requested acceptance", waiting_for="Owner acceptance", ends_when="Current preview accepted"))})
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.code, "claim_wait_unresolved")
        self.assert_no_writes()
        self.args.wait_resolved = "Existing source authorization preserves the acceptance hold."
        self.run_claim()
        held_wait = wait_record.records(self.target_comments["/repos/owner/repo/issues/99/comments"][-1]["body"])[0]
        self.assertIn(wait_record.marker(held_wait), self.issue["body"])

    def test_cross_repository_planning_claim_branch_remains_competing(self):
        self.cross_repository_fixture()
        source = CLAIM.records(self.comments[0]["body"])[0]
        source["branch"] = "work/runtime-authorization"
        self.comments[0]["body"] = CLAIM.marker(source)
        self.pulls = [pull for pull in self.pulls if pull["head"]["ref"] != source["branch"]]
        original_reader = self.read_pages
        def pages(path, **kwargs):
            if path == "/repos/other/plans/pulls":
                return "bot", [{"number": 200, "body": "Planning issue: https://github.com/other/plans/issues/42",
                                "head": {"ref": source["branch"]}}]
            return original_reader(path, **kwargs)
        self.read_pages = pages
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertIn({"source": "open_pr", "number": 200, "branch": source["branch"]},
                      caught.exception.payload["competing_evidence"])
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
        ):
            with self.subTest(extra=extra):
                self.pulls = [{"number": 99, "title": "docs", "body": context,
                               "head": {"ref": "work/docs"}, **extra}]
                with self.assertRaises(PLAN.ClassifiedPlanError):
                    self.run_claim()
                self.assert_no_writes()

    def test_contextual_body_links_allow_claim_without_special_line(self):
        url = "https://github.com/Owner/Repo/issues/42"
        for body in (url, f"Follow-ups: {url}", f"- Follow-ups: {url}",
                     f"> Runtime/docs qualification stays on {url}"):
            with self.subTest(body=body):
                self.setUp()
                self.pulls = [{"number": 99, "body": body, "head": {"ref": "work/docs"}}]
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_released_claim_branch_still_identifies_competing_pr(self):
        source = {**OTHER, "branch": "work/runtime-authorization"}
        self.comments = [{"id": 1, "body": CLAIM.marker(source), "user": {"login": TEST_BOT}},
                         {"id": 2, "body": "Released claim 1", "user": {"login": TEST_BOT}}]
        self.pulls = [{"number": 99, "body": "Runtime qualification stays on https://github.com/owner/repo/issues/42",
                       "head": {"ref": source["branch"]}}]
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.payload["competing_evidence"],
                         [{"source": "open_pr", "number": 99, "branch": source["branch"]}])
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
        with self.assertRaises(PLAN.PlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.code, "claim_native_blockers_unresolved")
        self.assert_no_writes()

    def released_note_fixture(self):
        self.comments = [
            {"id": 1, "body": CLAIM.marker(OTHER), "user": {"login": TEST_BOT},
             "created_at": "2026-10-01T00:00:00Z"},
            {"id": 2, "body": "Claimed by trial-b; structured claim #1 was confirmed and read back. "
             "Scope check: skipping the entire item; no repository or host files changed.",
             "user": {"login": TEST_BOT}, "created_at": "2026-10-01T00:01:00Z"},
            {"id": 3, "body": "Released claim 1", "user": {"login": TEST_BOT},
             "created_at": "2026-10-01T00:02:00Z"},
        ]

    def test_exact_released_source_accounts_for_its_explanatory_note(self):
        # shiny-infra-ops#338 comments 5943974336, 5943979826, 5943980314.
        self.released_note_fixture()
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_released_note_requires_exact_identity_author_and_chronology(self):
        for change in ("foreign_note", "foreign_release", "wrong_id", "wrong_worker", "different_session",
                       "reused_worker", "unreleased", "early_release", "edited_source", "edited_note",
                       "edited_source_before_release", "missing_time", "naive_time", "second_holder", "later_claim"):
            with self.subTest(change=change):
                self.setUp()
                self.released_note_fixture()
                source, note, release = self.comments
                if change == "foreign_note": note["user"]["login"] = "other"
                if change == "foreign_release": release["user"]["login"] = "other"
                if change == "wrong_id": note["body"] = note["body"].replace("#1", "#99")
                if change == "wrong_worker": note["body"] = note["body"].replace("trial-b", "trial-c")
                if change == "different_session": note["body"] += "\nSession: another-session"
                if change == "reused_worker":
                    self.comments[1:1] = [
                        {**source, "id": 4, "body": CLAIM.marker({**OTHER, "session": "reused-session"}),
                         "created_at": "2026-10-01T00:00:05Z"},
                        {**release, "id": 5, "body": "Released claim 4", "created_at": "2026-10-01T00:00:10Z"},
                    ]
                if change == "unreleased": self.comments.pop()
                if change == "early_release": release["created_at"] = "2026-09-30T23:59:00Z"
                if change == "edited_source": source["updated_at"] = "2026-10-01T00:03:00Z"
                if change == "edited_source_before_release":
                    source["updated_at"] = "2026-10-01T00:01:30Z"
                    source["body"] = CLAIM.marker({**OTHER, "session": "edited-session"})
                if change == "edited_note": note["updated_at"] = "2026-10-01T00:03:00Z"
                if change == "missing_time": note.pop("created_at")
                if change == "naive_time": note["created_at"] = "2026-10-01T00:01:00"
                if change == "second_holder": note["body"] += "\nOwned by another-worker."
                if change == "later_claim": self.comments.append({**source, "id": 4})
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assert_no_writes()

    def test_closed_session_release_does_not_account_for_post_closure_notes(self):
        import github_plan_release as release_helper
        for note_time in ("2026-10-01T00:00:30Z", "2026-10-01T00:01:30Z"):
            with self.subTest(note_time=note_time):
                self.setUp()
                self.released_note_fixture()
                source, note, release = self.comments
                note["created_at"] = note_time
                evidence = {"id": 88, "user": {"login": TEST_BOT}, "created_at": "2026-10-01T00:01:40Z",
                            "body": "Closed session session-b\nEnded at: 2026-10-01T00:01:00Z\nSafe to exit: yes",
                            "issue_url": "https://api.github.com/repos/owner/control/issues/884"}
                self.closed_pulls[88] = evidence
                release["body"] = release_helper.prepare_release(
                    source, evidence, self.comments[:2], self.inventory, actor=TEST_BOT, role="supervisor",
                    releaser_session="supervisor-native", evidence_url="https://github.com/owner/control/issues/884#issuecomment-88",
                    retained_prs=[], related=[],
                )
                if note_time > "2026-10-01T00:01:00Z":
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                    self.assertEqual(caught.exception.code, "claim_conflict")
                    self.assert_no_writes()
                else:
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def attested_handoff_fixture(self, *, elsewhere=False, terminated=False):
        # codex-skills#2111: claim 6241211301, note 6241527690, evidence 6243185845, release 6243187633.
        import github_plan_release as release_helper
        import github_plan_termination as termination
        source = {"id": 1, "body": CLAIM.marker(OTHER), "user": {"login": TEST_BOT},
                  "created_at": "2026-10-01T00:00:00Z"}
        note = {"id": 2, "body": "Claimed by trial-alias as a repeat-stop. Implementation: PR #7. "
                "The review of record is pending with the Supervisor.",
                "user": {"login": TEST_BOT}, "created_at": "2026-10-01T00:01:00Z"}
        body = "Closed session session-b\nEnded at: 2026-10-01T00:01:30Z\nSafe to exit: yes"
        if terminated:
            body = termination.body({"session_id": "session-b", "pid": 4242, "host": "fixture-host", "cause": "host crash",
                                     "retained_artifacts": [], "last_activity_at": "2026-10-01T00:01:20Z",
                                     "ended_at": "2026-10-01T00:01:30Z", "checked_at": "2026-10-01T00:07:00Z",
                                     "terminal_state": "pane dead"})
        thread = "owner/control/issues/884" if elsewhere else "owner/repo/issues/42"
        evidence = {"id": 88, "user": {"login": TEST_BOT},
                    "created_at": "2026-10-01T00:07:10Z" if terminated else "2026-10-01T00:01:40Z",
                    "body": body + "\nHandoff: https://github.com/owner/repo/issues/42#issuecomment-2",
                    "issue_url": f"https://api.github.com/repos/{thread}"}
        self.comments = [source, note] + ([] if elsewhere else [evidence])
        self.closed_pulls[88] = evidence
        self.comments.append({"id": 3, "user": {"login": TEST_BOT}, "created_at": "2026-10-01T00:08:00Z",
            "body": release_helper.prepare_release(
                source, evidence, self.comments, self.inventory, actor=TEST_BOT, role="supervisor",
                releaser_session="supervisor-native", evidence_url=f"https://github.com/{thread}#issuecomment-88",
                retained_prs=[], related=[])})

    def test_attested_handoff_note_is_accounted_for_by_the_supervisor_release(self):
        for elsewhere, terminated in ((False, False), (True, False), (False, True)):
            with self.subTest(elsewhere=elsewhere, terminated=terminated):
                self.setUp()
                self.attested_handoff_fixture(elsewhere=elsewhere, terminated=terminated)
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_external_or_terminated_evidence_must_still_name_the_note(self):
        for elsewhere, terminated in ((True, False), (False, True)):
            with self.subTest(elsewhere=elsewhere, terminated=terminated):
                self.setUp()
                self.attested_handoff_fixture(elsewhere=elsewhere, terminated=terminated)
                self.closed_pulls[88]["body"] = self.closed_pulls[88]["body"].replace("issuecomment-2", "issuecomment-99")
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assert_no_writes()

    def test_attested_handoff_note_requires_exact_evidence_author_session_and_chronology(self):
        for change in ("unnamed_note", "other_comment_named", "foreign_note", "foreign_evidence", "other_session_evidence",
                       "note_names_other_session", "note_after_end", "note_edited_after_end", "note_before_source",
                       "worker_release", "unreleased", "second_holder"):
            with self.subTest(change=change):
                self.setUp()
                self.attested_handoff_fixture()
                source, note, evidence, release = self.comments
                if change == "unnamed_note": evidence["body"] = evidence["body"].rsplit("\n", 1)[0]
                if change == "other_comment_named": evidence["body"] = evidence["body"].replace("issuecomment-2", "issuecomment-99")
                if change == "foreign_note": note["user"]["login"] = "other"
                if change == "foreign_evidence": evidence["user"] = {"login": "other"}
                if change == "other_session_evidence": evidence["body"] = evidence["body"].replace("session-b", "session-z")
                if change == "note_names_other_session": note["body"] += "\nSession: another-session"
                if change == "note_after_end": note["created_at"] = "2026-10-01T00:01:35Z"
                if change == "note_edited_after_end": note["updated_at"] = "2026-10-01T00:01:35Z"
                if change == "note_before_source":
                    self.comments[:2] = [note, source]
                    note["created_at"] = "2026-09-30T23:59:00Z"
                if change == "worker_release": release["body"] = "Released claim 1"
                if change == "unreleased": self.comments.pop()
                if change == "second_holder": note["body"] += "\nOwned by another-worker."
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assert_no_writes()

    def test_readback_rechecks_live_released_status_sessions(self):
        for phase in ("after_post", "after_status"):
            with self.subTest(phase=phase):
                self.setUp()
                self.released_note_fixture()
                self.inventory["sessions"] = [{"sessionId": "session-b", "cwd": "/unrelated"}]
                def add_status_holder():
                    self.issue["body"] += "\nSession: trial-b / session-b; executing claim released in the closeout comment."
                setattr(self, phase, add_status_holder)
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assert_no_writes()

    def test_retained_pr_history_cannot_hide_live_source_session(self):
        self.refresh_fixture()
        pr_owner = {**OTHER, "worker": "pr-worker", "session": "pr-session"}
        self.target_comments["/repos/owner/repo/issues/99/comments"] = [
            {"id": 71, "body": CLAIM.marker(pr_owner), "user": {"login": TEST_BOT},
             "created_at": "2026-10-01T00:00:00Z"},
            {"id": 72, "body": "Released claim 71", "user": {"login": TEST_BOT},
             "created_at": "2026-10-01T00:02:00Z"},
        ]
        self.targets["99"]["body"] += ("\n\n## Current Status\n\n"
            "Session: pr-worker / pr-session; executing claim released in the closeout comment.")
        self.inventory["sessions"] = [{"sessionId": "pr-session", "cwd": "/unrelated"}]
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
        self.assertEqual(caught.exception.code, "claim_conflict")
        self.assertEqual({row["session"] for row in caught.exception.payload["competing_evidence"]},
                         {pr_owner["session"]})
        self.assert_no_writes()

    def test_compound_status_identity_is_history_only_after_exact_release(self):
        # launchplane#3145, LP-3145-D2 refusal reported on #1529.
        self.released_note_fixture()
        self.issue["body"] += ("Session: trial-b / session-b; executing claim released in the closeout comment.\n"
                               "Branch: work/other-42\n")
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_compound_status_preserves_unproved_or_live_holders(self):
        for change in ("unreleased", "foreign_release", "live_session", "unreleased_comment", "different_marker"):
            with self.subTest(change=change):
                self.setUp()
                self.released_note_fixture()
                self.comments.pop(1)  # Isolate the status guard from explanatory-note refusal.
                self.issue["body"] += "Session: trial-b / session-b; executing claim released in the closeout comment.\n"
                if change == "unreleased": self.comments.pop()
                if change == "foreign_release": self.comments[-1]["user"]["login"] = "other"
                if change == "live_session": self.inventory["sessions"] = [{"sessionId": "session-b", "cwd": "/unrelated"}]
                if change == "unreleased_comment": self.comments.append({"id": 4, "body": "Claimed by another-worker", "user": {"login": TEST_BOT}})
                if change == "different_marker": self.issue["body"] += CLAIM.marker({**OTHER, "session": "another-session"})
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assert_no_writes()

    def test_upstream_holder_and_failed_claim_history_do_not_claim_this_issue(self):
        # launchplane#3266 names upstream odoo-devkit#198, not a local holder.
        for upstream in ("owner/upstream/issues/198", "owner/upstream/issues/42", "owner/repo/issues/198"):
            with self.subTest(upstream=upstream):
                self.setUp()
                self.issue["body"] += (
                    "State: Blocked; implementation unstarted and unclaimed. LP-D1 prerequisite reconciliation "
                    "completed; claim attempt refused with claim_wait_unresolved and write_outcome=not_started.\n"
                    f"Blocked by: https://github.com/{upstream} (actively owned by UPSTREAM-D1). "
                    "This is the sole remaining native execution prerequisite.\nWaiting for: None.\n"
                )
                self.args.wait_resolved = "Verified upstream prerequisite landed; no native blocker remains."
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def retained_pr_fixture(self, status: str = ""):
        """A released source claim whose authored release names its open PR #99."""
        source = {**OTHER, "branch": "work/retained-42"}
        self.comments = [
            {"id": 1, "body": "Claimed by trial-b\n" + CLAIM.marker(source), "user": {"login": TEST_BOT},
             "created_at": "2026-10-01T00:00:00Z"},
            {"id": 2, "body": "Released claim 1\n\nHandoff: PR #99 is green; ready for the merge train.",
             "user": {"login": TEST_BOT}, "created_at": "2026-10-01T00:02:00Z"},
        ]
        self.pulls = [{"number": 99, "state": "open", "user": {"login": TEST_BOT}, "title": "Repair", "body": "Refs #42",
                       "head": {"ref": source["branch"], "repo": {"full_name": "owner/repo"}},
                       "base": {"repo": {"full_name": "owner/repo"}}}]
        self.targets["99"] = {**copy.deepcopy(self.issue), "number": 99, "pull_request": {}, "body": "Refs #42"}
        self.target_comments["/repos/owner/repo/issues/99/comments"] = []
        self.issue["body"] += status
        return source

    def test_released_handoff_pr_routes_plain_claim_to_successor(self):
        # launchplane#3145, REC-LP-D1: open PR #3303 retained by released claim 6099392163.
        self.retained_pr_fixture("Session: unassigned\nBranch: work/retained-42 (retained for open PR #99)\n")
        self.run_claim()
        output = self.emitted.call_args.args[0]
        self.assertTrue(output["ok"])
        self.assertEqual((output["claim"]["resume_from"], output["claim"]["branch"]), ("1", OWNER["branch"]))
        self.assertTrue(output["claim"]["retained_handoff"].endswith("#issuecomment-2"))

    def test_admission_freezes_sources_proven_only_by_retained_pr_comments(self):
        import uuid
        from test_github_plan_sessions import ReceiptFixture
        observe = CLAIM.observe_sessions
        closure = PLAN.github_plan_closure
        admit = closure.admit_sources
        for phase in ("preflight", "after_post", "after_status"):
            with self.subTest(phase=phase), ReceiptFixture(str(uuid.uuid4())) as f:
                self.setUp()
                self.retained_pr_fixture()
                f.native_state = "notLoaded"
                target = {**OTHER, "session": f.session, "branch": "work/retained-42"}
                def appear():
                    self.target_comments["/repos/owner/repo/issues/99/comments"] = [
                        {"id": 77, "body": CLAIM.marker(target), "user": {"login": TEST_BOT}, "created_at": target["claimed_at"]},
                        {"id": 78, "body": "Released claim 77", "user": {"login": TEST_BOT}, "created_at": "2026-10-01T01:00:00Z"}]
                if phase == "preflight": appear()
                else: setattr(self, phase, appear)
                self.observe_inventory = observe
                admitted = []
                def freeze(sessions):
                    if f.session in sessions and not admitted:
                        if phase == "preflight": self.assertNotIn("post", self.events)
                        elif phase == "after_post": self.assertNotIn("status", self.events)
                        else: self.assertIn("status", self.events)
                        admitted.append(f.session)
                    return admit(sessions)
                with patch.object(closure, "admit_sources", side_effect=freeze): self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])
                self.assertIn(f.session, admitted)
                with self.assertRaises(ValueError):
                    with closure.resume_guard(f.session): pass
                self.assertFalse(f.mutations)

    def test_exact_released_source_is_frozen_without_an_optional_idle_receipt(self):
        import uuid
        from test_github_plan_sessions import ReceiptFixture
        closure = PLAN.github_plan_closure
        observe = CLAIM.observe_sessions
        for native, receipt in (("notLoaded", True), ("notLoaded", False), ("idle", True)):
            with self.subTest(native=native, receipt=receipt), ReceiptFixture(str(uuid.uuid4())) as f:
                self.setUp()
                if not receipt:
                    closure.replace_rows([])
                f.native_state = native
                source = {**OTHER, "session": f.session}
                unrelated = {**OTHER, "session": "unrelated-source", "branch": "work/unrelated"}
                self.comments = [
                    {"id": 1, "body": CLAIM.marker(source), "user": {"login": TEST_BOT}, "created_at": source["claimed_at"]},
                    {"id": 2, "body": "Released claim 1", "user": {"login": TEST_BOT}, "created_at": "2026-10-01T00:02:00Z"},
                    {"id": 3, "body": CLAIM.marker(unrelated), "user": {"login": TEST_BOT}, "created_at": unrelated["claimed_at"]},
                    {"id": 4, "body": "Released claim 3", "user": {"login": TEST_BOT}, "created_at": "2026-10-01T00:02:00Z"}]
                self.observe_inventory = observe
                for session in (OWNER["session"], unrelated["session"]):
                    with closure.ownership(session, create=True): pass
                def probe():
                    with self.assertRaises(closure.LeaseBusy):
                        with closure.ownership(f.session): pass
                    for session in (OWNER["session"], unrelated["session"]):
                        with closure.ownership(session) as available:
                            self.assertTrue(available)
                def during_post():
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pool.submit(probe).result(2)
                self.after_post = during_post
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])
                with self.assertRaises(ValueError):
                    with closure.resume_guard(f.session): pass
                for session in (OWNER["session"], unrelated["session"]):
                    with closure.resume_guard(session): pass
                self.assertFalse(f.mutations)

    def test_removed_pane_closure_allows_resume_only_successor_and_freezes_source(self):
        import uuid
        from datetime import datetime, timedelta, timezone
        from test_github_plan_sessions import ReceiptFixture
        from github_plan_sessions import native_helpers
        native_helpers()
        import tmux_terminal
        closure = PLAN.github_plan_closure
        with ReceiptFixture(str(uuid.uuid4())) as f:
            closure.replace_rows([])
            with closure.resume_guard(f.session, method="turn/start", home=f.home): pass
            entry = {**f.entry, "tmux_socket": "fixture", "tmux_session_id": "tmux:fixture:" + "a" * 32,
                     "window_id": "$1", "tab_id": "@1"}
            born = datetime.now(timezone.utc) - timedelta(minutes=1)
            f.native_state = "notLoaded"
            f.process_rows = [(455, f.entry["tty"], "login"), (456, f.entry["tty"], "unrelated-worker")]
            with patch.object(tmux_terminal.Terminal, "list", return_value=[]), \
                    patch.object(tmux_terminal, "process_parents", return_value={99: 1, 455: 99, 456: 455}), \
                    patch.object(closure, "process_started_at", side_effect=lambda pid: born + timedelta(seconds=pid != 455)):
                f.release.record_closure(f.log, entry, "supervisor")
                self.setUp()
                source = {**OTHER, "session": f.session}
                self.comments = [
                    {"id": 1, "body": CLAIM.marker(source), "user": {"login": TEST_BOT}, "created_at": source["claimed_at"]},
                    {"id": 2, "body": "Released claim 1", "user": {"login": TEST_BOT}, "created_at": "2026-10-01T03:00:00Z"}]
                self.args.resume_from = 1
                self.args.branch = source["branch"]
                self.observe_inventory = CLAIM.observe_sessions
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])
                self.assertEqual(self.emitted.call_args.args[0]["claim"]["branch"], source["branch"])
                with self.assertRaises(ValueError):
                    with closure.resume_guard(f.session): pass
                self.assertFalse(f.mutations)

    def test_real_released_source_with_current_or_unknown_work_refuses_without_lease_contention(self):
        import uuid
        from test_github_plan_sessions import ReceiptFixture
        observe = CLAIM.observe_sessions
        closure = PLAN.github_plan_closure
        for obstruction in ("process", "queue", "queue_unavailable", "pending", "busy"):
            with self.subTest(obstruction=obstruction), ReceiptFixture(str(uuid.uuid4())) as f:
                self.setUp()
                f.native_state = "notLoaded"
                if obstruction == "process":
                    f.process_rows = [(f.entry["pid"], f.entry["tty"], "codex resume " + f.session)]
                    f.commands = str(f.entry["pid"]) + " codex resume " + f.session
                elif obstruction == "queue": f.queue_items = [{"id": "queued"}]
                elif obstruction == "queue_unavailable": f.rpc_results["thread/queue/list"] = ValueError("fixture queue unavailable")
                elif obstruction == "pending":
                    with closure.resume_guard(f.session, method="turn/start", home=f.home): pass
                    f.native_state = "active"
                source = {**OTHER, "session": f.session}
                self.comments = [
                    {"id": 1, "body": CLAIM.marker(source), "user": {"login": TEST_BOT}, "created_at": source["claimed_at"]},
                    {"id": 2, "body": "Released claim 1", "user": {"login": TEST_BOT}, "created_at": "2026-10-01T00:02:00Z"}]
                self.observe_inventory = observe
                def claim():
                    with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                if obstruction == "busy":
                    with closure.ownership(f.session), ThreadPoolExecutor(max_workers=1) as pool:
                        pool.submit(claim).result(3)
                else:
                    claim()
                self.assert_no_writes()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    def available():
                        with closure.ownership(f.session) as acquired: return acquired
                    self.assertTrue(pool.submit(available).result(2))
                self.assertFalse(f.mutations)

    def test_released_claude_closed_row_allows_reused_tty_but_preserves_live_peer_veto(self):
        import uuid
        from datetime import datetime, timedelta, timezone
        from test_github_plan_sessions import ReceiptFixture
        observe = CLAIM.observe_sessions
        closure = PLAN.github_plan_closure
        for live in (False, True):
            with self.subTest(live=live), ReceiptFixture(str(uuid.uuid4())) as f:
                self.setUp()
                f.transcript.write_text(json.dumps({"sessionId": f.session, "type": "assistant",
                    "timestamp": "2026-10-01T02:00:00Z", "message": {"stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "Safe to exit: yes"}]}}) + "\n")
                closure.replace_rows([])
                closed = f.release.record_closure(f.log, {**f.entry, "harness": "claude"}, "supervisor")
                born = datetime.fromisoformat(closed["ended_at"]) + timedelta(microseconds=1)
                f.process_rows = [(456, f.entry["tty"], "claude")]
                f.commands = "456 claude"
                f.rpc_results["thread/loaded/list"] = {"data": [], "nextCursor": None}
                f.rpc_results["thread/read"] = ValueError("fixture has no Codex thread for this Claude ID")
                source = {**OTHER, "session": f.session}
                self.comments = [
                    {"id": 1, "body": CLAIM.marker(source), "user": {"login": TEST_BOT}, "created_at": source["claimed_at"]},
                    {"id": 2, "body": "Released claim 1", "user": {"login": TEST_BOT}, "created_at": "2026-10-01T03:00:00Z"}]
                self.observe_inventory = observe
                if live: self.inventory["sessions"] = [{"sessionId": f.session, "source": "claude_session"}]
                with patch.object(closure, "process_started_at", return_value=min(born, datetime.now(timezone.utc))):
                    if live:
                        with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                        self.assert_no_writes()
                    else:
                        self.run_claim()
                        self.assertTrue(self.emitted.call_args.args[0]["ok"])
                        with closure.resume_guard(f.session): pass
                self.assertFalse(f.mutations)

    def test_admission_freezes_leased_ownership_observed_only_on_family_peer(self):
        import uuid
        from test_github_plan_sessions import ReceiptFixture
        observe = CLAIM.observe_sessions
        closure = PLAN.github_plan_closure
        admit = closure.admit_sources
        for busy, phase in product((False, True), ("preflight", "after_post", "after_status")):
            with self.subTest(busy=busy, phase=phase), ReceiptFixture(str(uuid.uuid4())) as f:
                self.setUp()
                self.parent_handoff_fixture()
                f.native_state = "notLoaded"
                peer = {**OTHER, "session": f.session, "branch": "work/family-idle-source", "resume_from": "1",
                        "retained_handoff": "https://github.com/owner/repo/issues/41#issuecomment-3"}
                self.targets["43"] = {**copy.deepcopy(self.issue), "number": 43,
                                      "body": "## Current Status\n\n" + CLAIM.marker(peer)}
                def appear():
                    self.children["/repos/owner/repo/issues/41/sub_issues"].append(self.targets["43"])
                    self.target_comments["/repos/owner/repo/issues/43/comments"] = [
                        {"id": 88, "body": CLAIM.marker(peer), "user": {"login": TEST_BOT}, "created_at": peer["claimed_at"]},
                        {"id": 89, "body": "Released claim 88", "user": {"login": TEST_BOT}, "created_at": "2026-10-02T00:00:00Z"}]
                if phase == "preflight": appear()
                else: setattr(self, phase, appear)
                self.observe_inventory = observe
                admitted = []
                def freeze(sessions):
                    if peer["session"] in sessions:
                        if not admitted:
                            if phase == "preflight": self.assertNotIn("post", self.events)
                            elif phase == "after_post": self.assertNotIn("status", self.events)
                            else: self.assertIn("status", self.events)
                        admitted.append(peer["session"])
                        if busy: raise closure.LeaseBusy("fixture publication held")
                    return admit(sessions)
                with patch.object(closure, "admit_sources", side_effect=freeze):
                    if busy:
                        with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                        self.assertEqual(caught.exception.code, "claim_closure_unavailable")
                        if phase == "preflight": self.assert_no_writes()
                        else: self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])
                    else:
                        self.run_claim()
                        self.assertTrue(self.emitted.call_args.args[0]["ok"])
                        self.assertIn(peer["session"], admitted)
                if not busy:
                    with self.assertRaises(ValueError):
                        with closure.resume_guard(f.session): pass
                self.assertFalse(f.mutations)

    def test_busy_closure_publication_is_classified_before_claim_writes(self):
        with patch.object(PLAN.github_plan_closure, "admit_sources", side_effect=PLAN.github_plan_closure.LeaseBusy("held")):
            with self.assertRaises(PLAN.ClassifiedPlanError):
                self.run_claim()
        self.assert_no_writes()

    def test_released_handoff_route_preserves_real_and_unproved_holders(self):
        for change in ("unreleased", "foreign_release", "unnamed_pr", "foreign_pr", "second_pr", "live_session",
                       "second_claim"):
            with self.subTest(change=change):
                self.setUp()
                self.retained_pr_fixture("Session: unassigned\n")
                if change == "unreleased": self.comments.pop()
                if change == "foreign_release": self.comments[1]["user"] = {"login": "other"}
                if change == "unnamed_pr": self.comments[1]["body"] = "Released claim 1\n\nHandoff: source work is green."
                if change == "foreign_pr": self.pulls[0]["user"] = {"login": "stranger"}
                if change == "second_pr":
                    self.pulls.append({**copy.deepcopy(self.pulls[0]), "number": 100,
                                       "head": {"ref": "work/issue-42-other", "repo": {"full_name": "owner/repo"}}})
                if change == "live_session": self.inventory["sessions"] = [{"sessionId": "session-b", "cwd": "/unrelated"}]
                if change == "second_claim": self.compete({**OTHER, "worker": "trial-c", "session": "session-c"})
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assert_no_writes()

    def test_released_handoff_route_reports_its_refusal_and_waits(self):
        self.retained_pr_fixture()
        self.pulls[0]["user"] = {"login": "stranger"}
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
        route = caught.exception.payload["successor_route"]
        self.assertEqual((route["resume_from"], route["handoff_comment"]), (1, 2))
        self.assertIn("refused", route)
        self.setUp()
        self.retained_pr_fixture()
        self.target_comments["/repos/owner/repo/issues/99/comments"].append({"id": 88, "user": {"login": TEST_BOT}, "body": wait_record.marker(wait_record.reviewed(evidence="Client review requested", waiting_for="Client acceptance of the preview", ends_when="Current head accepted"))})
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
        self.assertEqual(caught.exception.code, "claim_wait_unresolved")
        self.assertEqual(caught.exception.payload["successor_route"], {"resume_from": 1, "handoff_comment": 2})
        self.assert_no_writes()

    def test_released_holder_state_prose_resumes_and_routes(self):
        # discord-blue#213, DUI-213-D4: "State: Active; owned by DUI-213-D3. The question-picker slice ...".
        for flags in ("explicit", "plain"):
            with self.subTest(flags=flags):
                self.setUp()
                source = self.retained_pr_fixture()
                self.issue["body"] += ("State: Active; owned by trial-b. The question slice is ready in PR #99; "
                                       "approvals and dashboard remain.\nSession: session-b\nBranch: work/retained-42\n"
                                       + CLAIM.marker(source) + "\n")
                if flags == "explicit":
                    self.args.resume_from, self.args.handoff_comment = 1, 2
                self.run_claim()
                self.assertEqual(self.emitted.call_args.args[0]["claim"]["resume_from"], "1")

    def test_completed_session_field_is_history_after_exact_release(self):
        # odoo-docker#113, REC-ODOO-D1: "Completed session: 01a12387-... (ODD-113-D1)."
        for field in ("Completed session: session-b (trial-b).", "Previous session: session-b"):
            with self.subTest(field=field):
                self.setUp()
                self.released_note_fixture()
                self.comments.pop(1)
                self.issue["body"] += f"State: Active; engineering complete, Supervisor handoff.\n{field}\nBranch: work/other-42\n"
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def completed_handoff_fixture(self):
        """Immutable #828 claim, release and refusal evidence from October 12."""
        fixture = json.loads(Path(__file__).with_name("fixtures").joinpath(
            "context-panel-828-released-status.json").read_text())
        self.comments = [fixture["source"], fixture["release"]]
        for comment in self.comments:
            comment["user"]["login"] = TEST_BOT
        self.issue["body"] += fixture["refusal"]["body"].split("“", 1)[1].split("”", 1)[0] + "\n"
        return CLAIM.records(self.comments[0]["body"])[0]

    def test_completed_handoff_explanation_allows_successor_claim(self):
        for same_session in (False, True):
            with self.subTest(same_session=same_session):
                self.setUp()
                source = self.completed_handoff_fixture()
                if same_session:
                    self.args.worker, self.args.session = source["worker"], source["session"]
                    self.inventory["sessions"] = [{"sessionId": source["session"], "cwd": "/unrelated"}]
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_completed_handoff_explanation_allows_explicit_retained_successor(self):
        source = self.completed_handoff_fixture()
        self.args.repo, self.args.issue = "cbusillo/context-panel", "828"
        self.args.worker, self.args.session = source["worker"], source["session"]
        self.args.resume_from, self.args.handoff_comment = (c["id"] for c in self.comments)
        self.closed_pulls[831] = {
            "number": 831, "state": "closed", "merged_at": "2026-10-12T07:48:00Z",
            "user": {"login": TEST_BOT}, "body": "Refs #828",
            "head": {"ref": source["branch"], "repo": {"full_name": self.args.repo}},
            "base": {"repo": {"full_name": self.args.repo}},
        }
        self.targets["831"] = {**self.closed_pulls[831], "pull_request": {}}
        self.target_comments[f"/repos/{self.args.repo}/issues/831/comments"] = []
        self.inventory["local_branches"] = [source["branch"]]
        self.run_claim()
        result = self.emitted.call_args.args[0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["claim"]["resume_from"], str(self.comments[0]["id"]))

    def test_completed_handoff_explanation_preserves_release_and_ownership_guards(self):
        for change in ("unreleased", "foreign_release", "live_session", "later_claim"):
            with self.subTest(change=change):
                self.setUp()
                source = self.completed_handoff_fixture()
                if change == "unreleased": self.comments.pop()
                if change == "foreign_release": self.comments[-1]["user"]["login"] = "other"
                if change == "live_session": self.inventory["sessions"] = [{"sessionId": source["session"], "cwd": "/unrelated"}]
                if change == "later_claim": self.comments.append({"id": 4, "body": CLAIM.marker(OTHER), "user": {"login": TEST_BOT}})
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assert_no_writes()

    def test_completed_session_field_preserves_unproved_or_live_holders(self):
        for change in ("unreleased", "foreign_release", "live_session"):
            with self.subTest(change=change):
                self.setUp()
                self.released_note_fixture()
                self.comments.pop(1)
                field = "Completed session: session-b (trial-b)."
                if change == "unreleased": self.comments.pop()
                if change == "foreign_release": self.comments[-1]["user"]["login"] = "other"
                if change == "live_session": self.inventory["sessions"] = [{"sessionId": "session-b", "cwd": "/unrelated"}]
                self.issue["body"] += field + "\n"
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assert_no_writes()

    def test_other_issue_holder_sentence_does_not_claim_this_issue(self):
        # launchplane#3318, LP-3318-Q: Last verified names #3314's holder.
        for ref in ("#3314", "owner/repo#3314", "https://github.com/owner/repo/issues/3314", "owner/other#42",
                    "Issue #3314"):
            with self.subTest(ref=ref):
                self.setUp()
                self.issue["body"] += (f"Last verified: October 10, 2026. {ref} Q151 was approved today and is owned by "
                                       "LP-3314-D1; its declined single-Accept recommendation must not be implemented.\n")
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])

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

    def test_conditional_legacy_releases_preserve_real_claims(self):
        releases = (
            "Released by trial-b if CI passes",
            "Released by trial-b once PR #99 merges",
            "Released by trial-b pending owner approval",
            "Released by trial-b; release takes effect after landing",
            "Released by trial-b. If CI passes, the next worker may claim.",
            "Released by trial-b\nif CI passes",
            "Released by trial-b\r\nonce PR #99 merges",
            "Released by trial-b\nSource work is finished; release is pending CI.",
            "Released by trial-b\n\nThis only takes effect after PR #99 merges; do not claim until then.",
            "Released by trial-b\n\nIf CI passes, the next worker may claim.",
            "Released by **trial-b** if CI passes",
            "> Released by trial-b\n\nHistorical handoff, not a new release.",
        )
        for source in (CLAIM.marker(OTHER), "Claimed by trial-b\nSession: session-b\nBranch: work/other-42"):
            for release in releases:
                with self.subTest(source=source, release=release):
                    self.setUp()
                    self.comments = [
                        {"id": 1, "body": source, "user": {"login": TEST_BOT}},
                        {"id": 2, "body": release, "user": {"login": TEST_BOT}},
                    ]
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                        self.run_claim()
                    self.assertIn(1, [e.get("id") for e in caught.exception.payload["competing_evidence"]])
                    self.assert_no_writes()
                    # An ambiguous older record is recovered by its author,
                    # without deleting history or broadening the worker alias.
                    self.comments.append({"id": 3, "body": "Released claim 1\n\n"
                                          "Source session finished; the retained PR still follows its own gates.",
                                          "user": {"login": TEST_BOT}})
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_standalone_legacy_release_allows_authorized_successor(self):
        for release in (
            "Released by trial-b",
            "Released by trial-b \t\r\n",
            "Released by trial-b\n\n<!-- github-skill-operation:abc123 -->\n",
        ):
            with self.subTest(release=release):
                self.setUp()
                self.comments = [
                    {"id": 1, "body": CLAIM.marker(OTHER), "user": {"login": TEST_BOT}},
                    {"id": 2, "body": release, "user": {"login": TEST_BOT}},
                ]
                self.run_claim()
                self.assertIn("metadata_readback", self.emitted.call_args.args[0]["completed_steps"])

    def test_legacy_release_does_not_clear_later_claim_or_independent_artifacts(self):
        for evidence in ("later_claim", "branch", "worktree", "pr"):
            with self.subTest(evidence=evidence):
                self.setUp()
                self.comments = [
                    {"id": 1, "body": CLAIM.marker(OTHER), "user": {"login": TEST_BOT}},
                    {"id": 2, "body": "Released by trial-b", "user": {"login": TEST_BOT}},
                ]
                if evidence == "later_claim":
                    self.comments.append({"id": 3, "body": CLAIM.marker(OTHER), "user": {"login": TEST_BOT}})
                elif evidence == "branch":
                    self.inventory["remote_branches"] = ["work/issue-42-prior"]
                elif evidence == "worktree":
                    self.inventory["worktrees"] = [{"path": "/retained/issue-42", "branch": OTHER["branch"]}]
                else:
                    self.pulls = [{"number": 99, "body": "Refs #42", "head": {"ref": OTHER["branch"]}}]
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                self.assert_no_writes()

    def test_legacy_retained_branch_requires_unambiguous_release(self):
        for release, allowed in (
            ("Released by trial-b\n\n<!-- github-skill-operation:abc123 -->", True),
            ("Released by trial-b\nif CI passes", False),
            ("Released by trial-b once PR #99 merges", False),
            ("Released by trial-b\n\nSource session finished. Retain work/other-42 for the successor.", False),
        ):
            with self.subTest(release=release):
                self.setUp()
                self.args.resume_from = 1
                self.comments = [
                    {"id": 1, "body": CLAIM.marker(OTHER), "user": {"login": TEST_BOT}},
                    {"id": 2, "body": release, "user": {"login": TEST_BOT}},
                ]
                self.inventory["local_branches"] = [OTHER["branch"]]
                self.inventory["worktrees"] = [{"path": "/retained/issue-42", "branch": OTHER["branch"]}]
                if allowed:
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])
                else:
                    with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                    self.assert_no_writes()
                    self.comments.append({"id": 3, "body": "Released claim 1\n\nSource session finished.",
                                          "user": {"login": TEST_BOT}})
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])

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

    def released_status_fixture(self, release="Released claim 1"):
        self.args.resume_from = 1
        self.comments = [
            {"id": 1, "body": CLAIM.marker(OTHER), "user": {"login": TEST_BOT},
             "created_at": OTHER["claimed_at"]},
            {"id": 2, "body": release, "user": {"login": TEST_BOT},
             "created_at": "2026-10-01T00:01:00Z"},
        ]
        self.issue["body"] += (
            f"State: Active; owned by {OTHER['worker']}.\nSession: {OTHER['session']}\n"
            f"Branch: {OTHER['branch']}\n" + CLAIM.marker(OTHER)
        )
        self.inventory["local_branches"] = [OTHER["branch"]]
        self.inventory["worktrees"] = [{"path": "/retained/issue-42", "branch": OTHER["branch"]}]

    def test_resume_replaces_exact_released_status_and_posts_successor_claim(self):
        for release in ("Released claim 1", "Released claim 1.\nSource work landed."):
            with self.subTest(release=release):
                self.setUp()
                self.released_status_fixture(release)
                previous = PLAN.read_plan_sections(self.issue)[0]["Current Status"]
                self.run_claim()
                output = self.emitted.call_args.args[0]
                self.assertIn(previous, output["previous_current_status"])
                self.assertEqual(CLAIM.records(self.issue["body"]), [output["claim"]])
                self.assertIn("metadata_readback", output["completed_steps"])
                self.assertEqual(len(self.comments), 3)

    def test_resume_preserves_unproven_or_competing_status_and_artifacts(self):
        for change in ("wrong_author", "wrong_id", "id_suffix", "conditional_release", "before_marker", "same_time",
                       "missing_time", "naive_time", "edited_source", "different_marker", "extra_comment", "other_branch", "live_peer"):
            with self.subTest(change=change):
                self.setUp()
                self.released_status_fixture()
                if change == "wrong_author": self.comments[1]["user"]["login"] = "someone-else"
                if change == "wrong_id": self.comments[1]["body"] = "Released claim 11"
                if change == "id_suffix": self.comments[1]["body"] = "Released claim 1.other"
                if change == "conditional_release": self.comments[1]["body"] = "Released claim 1 once PR #99 merges"
                if change == "before_marker": self.comments[1]["created_at"] = "2026-09-30T00:00:00Z"
                if change == "same_time": self.comments[1]["created_at"] = OTHER["claimed_at"]
                if change == "missing_time": del self.comments[1]["created_at"]
                if change == "naive_time": self.comments[1]["created_at"] = "2026-10-01T00:01:00"
                if change == "edited_source": self.comments[0]["updated_at"] = "2026-10-01T00:02:00Z"
                if change == "different_marker":
                    self.issue["body"] = self.issue["body"].replace(CLAIM.marker(OTHER), CLAIM.marker({**OTHER, "session": "new-session"}))
                if change == "extra_comment": self.compete({**OTHER, "session": "new-session"})
                if change == "other_branch": self.inventory["local_branches"].append("work/other-issue-42")
                if change == "live_peer": self.inventory["sessions"] = [{"sessionId": "peer", "cwd": "/retained/issue-42"}]
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                self.assert_no_writes()

    def test_resume_readback_refuses_new_status_owner_after_post(self):
        self.released_status_fixture()
        self.after_post = lambda: self.issue.update(body=self.issue["body"] + "\n" + CLAIM.marker({**OTHER, "session": "peer"}))
        with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
        self.assertIn("post", self.events)
        self.assertNotIn("status", self.events)

    def test_resume_final_readback_refuses_reintroduced_released_marker(self):
        self.released_status_fixture()
        self.after_status = lambda: self.issue.update(body=PLAN.replace_issue_plan_section(self.issue, "Current Status", CLAIM.marker(OTHER)))
        with self.assertRaises(PLAN.PlanError): self.run_claim()
        self.assertIn("status", self.events)
        self.assertFalse(self.emitted.called)

    def test_resume_preserves_read_only_status_and_claims_in_comment(self):
        self.released_status_fixture()
        self.issue["user"]["login"] = "contributor"
        original = self.issue["body"].replace(PLAN.PLAN_MANAGED_PROVENANCE_MARKER, "")
        self.issue["body"] = original
        self.run_claim()
        self.assertEqual(self.issue["body"], original)
        self.assertEqual(self.emitted.call_args.args[0]["current_status_location"], "claim_comment")
        self.assertNotIn("status", self.events)

    def test_period_release_works_without_status_and_with_refresh_handoff(self):
        self.comments = [{"id": 1, "body": CLAIM.marker(OTHER), "user": {"login": TEST_BOT}},
                         {"id": 2, "body": "Released claim 1.\nWork finished.", "user": {"login": TEST_BOT}}]
        self.run_claim()
        self.setUp()
        self.refresh_fixture()
        self.comments[2]["body"] = "Released claim 1.\nHandoff: PR #99 and #100."
        self.run_claim()

    def test_conditional_release_does_not_authorize_retained_branch_or_refresh(self):
        self.refresh_fixture()
        self.comments[2]["body"] = "Released claim 1 once PR #99 merges\nHandoff: PR #99 and #100."
        with self.assertRaises(PLAN.PlanError): self.run_claim()
        self.assert_no_writes()

    def test_embedded_release_recovers_reported_finished_session(self):
        for role in ("Director", "Owner"):
            self.setUp()
            # The release follows a question/handoff, regardless of role spelling.
            self.comments = [
                {"id": 1, "body": CLAIM.marker(OTHER), "user": {"login": TEST_BOT}},
                {"id": 2, "body": f"{role} question: Verify live-host usage?\n\n"
                 "Source session finished without implementation.\n\nReleased claim 1\n\n"
                 "<!-- github-skill-operation:ebe51a4db859c979eaa963fb10af11b5 -->\n",
                 "user": {"login": TEST_BOT}},
                {"id": 3, "body": f"{role} decision: The later read-only inventory is approved; no host changes.",
                 "user": {"login": "owner"}},
            ]
            self.run_claim()
            self.assertEqual(len(self.comments), 4)
            self.assertIn("claim_readback", self.emitted.call_args.args[0]["completed_steps"])
            # Release is ownership proof. Task authority is verified by the caller;
            # the helper gates recorded waits, not the presence of decision comments.
            self.setUp()
            self.comments = [
                {"id": 1, "body": CLAIM.marker(OTHER), "user": {"login": TEST_BOT}},
                {"id": 2, "body": f"{role} question: Verify live-host usage?\n\nReleased claim 1",
                 "user": {"login": TEST_BOT}},
            ]
            self.run_claim()
            self.assertIn("metadata_readback", self.emitted.call_args.args[0]["completed_steps"])

    def test_embedded_release_resumes_exact_status_with_retained_branch(self):
        self.released_status_fixture("Finished session handoff.\r\n\r\nReleased claim 1.")
        self.run_claim()
        self.assertEqual(CLAIM.records(self.issue["body"]), [self.emitted.call_args.args[0]["claim"]])

    def test_unconditional_final_release_with_effective_branch_name_recovers(self):
        self.released_status_fixture(
            "Source work is finished; branch work/cs-1400-effective-cache is retained.\n\nReleased claim 1"
        )
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_embedded_release_preserves_ownership_and_artifact_guards(self):
        for change in ("foreign", "wrong_id", "earlier", "other_claim", "artifact", "live_peer"):
            with self.subTest(change=change):
                self.setUp()
                self.released_status_fixture("Finished session handoff.\n\nReleased claim 1")
                if change == "foreign": self.comments[1]["user"]["login"] = "stranger"
                if change == "wrong_id": self.comments[1]["body"] += "1"
                if change == "earlier":
                    self.comments.reverse()
                    self.issue["body"] = PLAN.template_body("Repair")
                    self.args.resume_from = None
                    self.inventory = {**self.inventory, "local_branches": [], "worktrees": []}
                if change == "other_claim": self.compete({**OTHER, "session": "peer"})
                if change == "artifact": self.inventory["local_branches"].append("work/other-issue-42")
                if change == "live_peer": self.inventory["sessions"] = [{"sessionId": "peer", "cwd": "/retained/issue-42"}]
                with self.assertRaises(PLAN.PlanError): self.run_claim()
                self.assert_no_writes()

    def test_release_examples_and_incidental_text_do_not_reclaim(self):
        for release in (
            "> Released claim 1", "    Released claim 1", "`Released claim 1`",
            "- Released claim 1", "Example: Released claim 1", "Released claim 1 once CI passes",
            "Released claim 1. Work finished.", "Released claim 1.other",
            "```text\n\nReleased claim 1\n```", "~~~\n\nReleased claim 1\n~~~",
            "```text\n\nReleased claim 1", "~~~~text\n~~~\n\nReleased claim 1",
            "<!-- Example:\n\nReleased claim 1", "Released claim 1\n\nOther handoff prose.",
            "If you approve, post:\n\nReleased claim 1", "After PR #99 merges:\n\nReleased claim 1",
            "After PR #99 merges:\r\n\r\nReleased claim 1",
            "Owner question: approve?\n\nIf approved, post:\n \nReleased claim 1",
            "Post this:\n\t\nReleased claim 1",
            "<pre>\n\nReleased claim 1", "<details><summary>Example</summary>\n\nReleased claim 1",
            "<blockquote>\n\nReleased claim 1",
        ):
            with self.subTest(release=release):
                self.setUp()
                self.comments = [
                    {"id": 1, "body": CLAIM.marker(OTHER), "user": {"login": TEST_BOT}},
                    {"id": 2, "body": "Source handoff.\n\n" + release, "user": {"login": TEST_BOT}},
                ]
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                self.assert_no_writes()

    def test_refresh_embedded_release_requires_explicit_source_identity(self):
        self.refresh_fixture()
        self.comments[2]["body"] = (
            "Handoff from trial-b\nSource claim 1; Session: session-b; PR #99 and #100.\n\nReleased claim 1"
        )
        self.run_claim()
        self.assertEqual(self.emitted.call_args.args[0]["claim"]["refresh_pr"], self.args.refresh_pr)

    def test_late_release_does_not_validate_reported_malformed_handoff(self):
        # launchplane#2693: release recognition must not erase handoff identity.
        for embedded in (False, True):
            with self.subTest(embedded=embedded):
                self.setUp()
                self.refresh_fixture()
                self.comments[2]["body"] = "LP-2693-D1 handoff: PR #99 and #100; source work finished."
                if embedded: self.comments[2]["body"] += "\n\nReleased claim 1"
                self.comments.append({"id": 4, "body": "Released claim 1", "user": {"login": TEST_BOT}})
                with self.assertRaises(PLAN.PlanError): self.run_claim()
                self.assert_no_writes()
                # Recovery is a new same-author handoff bound to the source record,
                # after its release, not rewriting the old comment or source claim.
                self.comments.append({"id": 5, "body": "Handoff from trial-b\nSource claim 1; Session: session-b; PR #99 and #100", "user": {"login": TEST_BOT}})
                self.args.handoff_comment = 5
                self.run_claim()
                output = self.emitted.call_args.args[0]
                self.assertEqual(output["claim"]["refresh_pr"], self.args.refresh_pr)
                self.assertEqual(CLAIM.records(self.comments[-1]["body"]), [output["claim"]])

    def test_embedded_release_does_not_resolve_recorded_owner_wait(self):
        self.released_status_fixture("Finished session handoff.\n\nReleased claim 1")
        self.wait = wait_record.reviewed(evidence="Director asked to test first", waiting_for="Owner approval of the live-host check", ends_when="Director records acceptance")
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
        self.assertEqual(caught.exception.code, "claim_wait_unresolved")
        self.assert_no_writes()

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
        previous = PLAN.section_map(self.issue["body"])["Current Status"]
        self.args.wait_resolved = "Owner selected A in the recorded decision"
        self.run_claim()
        self.assertEqual(self.emitted.call_args.args[0]["previous_current_status"], previous)
        self.assertEqual(CLAIM.records(self.comments[-1]["body"])[0]["session"], self.args.session)

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

    def test_explicit_no_wait_status_allows_claim_without_resolution(self):
        statuses = (
            "Blocked by: No native issue blocker.\nWaiting for: nothing.",
            "Blocked by: No native issue blocker\nWaiting for:",
            "Blocked by: none. The service fields are live.",
            "Blocked by: None.\nWaiting for: Nothing for read-only investigation.",
            "- Blocked by: NONE.\n- Waiting for: NOTHING FOR READ-ONLY INVESTIGATION.",
            "Blocked by: None.\nWaiting for: None; the next actor is an agent.",
            "Blocked by: None.\nWaiting for: None; an agent acts next.",
            "Blocked by: None.\nWaiting for: nothing; this is agent work.",
            "Blocked by: No native issue blocker recorded for this source task.\nWaiting for: None; an executing agent acts next.",
            "Blocked by: No native issue blocker; engineering qualification remains.\nWaiting for: None; an agent acts next, so keep plan:active.",
            "Blocked by: No native issue blocker recorded for the offline repair.\nWaiting for: None; source validation is agent work.",
            "Blocked by: None; independent of the installer.\nWaiting for: Nothing. Engineering can proceed.",
            "Blocked by: None.\nWaiting for: None; engineering to test the copy.",
            "**Blocked by:** None.\n**Waiting for:** None.",
        )
        for status in statuses:
            with self.subTest(status=status):
                self.setUp()
                self.issue["body"] += status + "\n"
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])
                self.assertIn("post", self.events)
                self.assertIn(status, self.emitted.call_args.args[0]["previous_current_status"])

    def test_global_invalid_wait_selection_still_requires_actual_claim_resolution(self):
        import test_gh_plan_next as selection
        root = selection.track("someone/direction", 1, "First")
        leaf = selection.global_issue("owner/repo", 42, wait_reviewed=False, labels=["plan", "plan:waiting"],
            body=PLAN.PLAN_MANAGED_PROVENANCE_MARKER + "\n## Current Status\nState: Waiting.\nWaiting for: starts after First\nBlocked by: None.")
        edges = {("someone/direction", 1): selection.relationships(sub_issues=[leaf])}
        with selection.global_fixture([root], [leaf], edges) as (module, result, _reads):
            module.cmd_next(selection.next_args())
            candidate = next(item for item in result["candidates"] if item["number"] == 42)
            context = {"issues": {"owner/repo#42": selection.reviewed(candidate, category="milestone")}}
            with patch.object(module, "next_selection_context", return_value=context):
                module.cmd_next(selection.next_args())
            self.assertEqual(result["available_candidates"], [])
            self.assertEqual([item["number"] for item in result["candidates"]], [42])
        self.issue.update(leaf)
        self.issue["user"] = {"login": TEST_BOT}
        self.wait = None  # The legacy issue has not received its structured review.
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.code, "claim_wait_unresolved")
        self.assert_no_writes()
        self.args.wait_resolved = "Full milestone path and discussion verified: ordering First is scheduling, not a person/event hold; Director already authorized this work. No native blockers or competing ownership."
        self.run_claim()
        receipt = self.emitted.call_args.args[0]
        self.assertTrue(receipt["ok"])
        self.assertIn("post_claim", receipt["completed_steps"])
        self.assertIn("starts after First", receipt["previous_current_status"])
        self.assertIn(self.args.wait_resolved, self.comments[-1]["body"])
        self.assertIn("plan:active", PLAN.normalize_labels(self.issue["labels"]))
        self.assertNotIn("plan:waiting", PLAN.normalize_labels(self.issue["labels"]))

    def test_no_wait_text_does_not_override_native_blocker(self):
        self.issue["body"] += "Blocked by: No native issue blocker.\nWaiting for: nothing.\n"
        self.blockers = [{"state": "open", "number": 41}]
        with self.assertRaises(PLAN.PlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_stale_wait_label_does_not_override_reviewed_clear_record(self):
        self.issue["labels"] = [{"name": PLAN.DEFAULT_CONFIG["labels"]["waiting"]}]
        self.run_claim()
        self.assertEqual(PLAN.normalize_labels(self.issue["labels"]), [PLAN.DEFAULT_CONFIG["labels"]["active"]])

    def test_failed_final_readback_returns_prior_status_for_recovery(self):
        self.after_status = self.compete
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertIn("State: Open, not started.", caught.exception.payload["previous_current_status"])

    def record_wait(self, waiting_for="Director's device test", *, target=None):
        wait = wait_record.reviewed(evidence="Director explicitly requested this hold", waiting_for=waiting_for,
                             ends_when="Director records acceptance on this issue")
        comment = {"id": 900, "user": {"login": TEST_BOT}, "body": wait_record.marker(wait)}
        if target is None:
            self.comments.append(comment)
        else:
            self.target_comments[f"/repos/owner/repo/issues/{target}/comments"].append(comment)
        return wait

    def test_legacy_prose_waits_and_owners_are_reported_before_any_claim_write(self):
        for prose in ("Waiting for: None; Chris decides the scope", "Owned by another session",
                      "Blocked by: None for source work", "State: Active; Supervisor acts next"):
            with self.subTest(prose=prose):
                self.setUp()
                self.wait = None
                self.issue["body"] += prose
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertTrue(caught.exception.payload["review_required"])
                self.assert_no_writes()

    def test_creation_record_moves_into_current_status_without_duplicate_body_records(self):
        self.wait = None
        self.issue["body"] = PLAN.template_body("Repair") + "\n\n" + wait_record.marker(
            wait_record.reviewed(evidence="Creation request reviewed; no hold"))
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])
        self.assertEqual(len(wait_record.records(self.issue["body"])), 1)

    def test_record_move_preserves_original_contributor_marker_examples(self):
        self.wait = None
        self.issue["user"]["login"] = "owner"
        original = "Original report contains a historical record:\n" + wait_record.marker(
            wait_record.reviewed(evidence="Historical example", waiting_for="Earlier test", ends_when="Recorded earlier result"))
        self.issue["body"] = original
        self.issue["body"] = PLAN.replace_issue_plan_section(self.issue, "Current Status",
            "State: Active\n" + wait_record.marker(wait_record.reviewed(evidence="Full original report reviewed; resolved")))
        self.run_claim()
        preserved = PLAN.marked_block(self.issue["body"], PLAN.PLAN_ORIGINAL_START, PLAN.PLAN_ORIGINAL_END)
        self.assertEqual(preserved[2], original)
        current = PLAN.contributor_plan_body(self.issue)
        self.assertEqual(len(wait_record.records(current)), 1)

    def test_reviewed_status_and_comment_prose_cannot_create_a_wait_or_an_owner(self):
        self.issue["body"] += "\nOwned by historical-worker\nWaiting for: None; Chris decides the scope"
        self.comments.append({"id": 31, "user": {"login": TEST_BOT},
                              "body": "## Current Status\n\nOwned by Supervisor; waiting for historical test"})
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_structured_wait_refuses_despite_reassuring_status_and_comment_prose(self):
        wait = self.record_wait()
        self.issue["body"] += "\nWaiting for: None.\nBlocked by: None."
        self.comments.append({"id": 901, "user": {"login": TEST_BOT}, "body": "All waits are released; agents may go"})
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.payload["structured_wait"], wait)
        self.assertFalse(caught.exception.payload["review_required"])
        self.assert_no_writes()

    def test_normal_claim_checks_new_structured_waits_on_both_readbacks_even_with_resolution(self):
        for phase in ("after_post", "after_status"):
            with self.subTest(phase=phase):
                self.setUp()
                self.args.wait_resolved = "Earlier absence reviewed; grants no new hold resolution"
                setattr(self, phase, self.record_wait)
                with self.assertRaises(PLAN.PlanError) as caught:
                    self.run_claim()
                self.assertIn("release_own_claim", caught.exception.payload["claim_recovery"])

    def test_oct12_refusals_replay_with_reviewed_absence_and_keep_competing_claims(self):
        fixtures = json.loads(Path(__file__).with_name("fixtures").joinpath("oct12-claim-refusals.json").read_text())
        for fixture in fixtures:
            for mode in ("reviewed", "legacy", "wait", "competitor"):
                with self.subTest(case=fixture["case"], mode=mode):
                    self.setUp()
                    self.issue["body"] += fixture["status"]
                    # Replay published records unchanged; the fixture bot name is
                    # remapped only for the transport's configured author.
                    self.comments = copy.deepcopy(fixture["comments"])
                    for comment in self.comments:
                        comment["user"]["login"] = TEST_BOT
                    if mode == "legacy": self.wait = None
                    if mode == "wait": self.record_wait()
                    if mode == "competitor": self.compete()
                    if mode == "reviewed":
                        self.run_claim()
                        self.assertTrue(self.emitted.call_args.args[0]["ok"])
                        self.assertIn("metadata_readback", self.emitted.call_args.args[0]["completed_steps"])
                    else:
                        with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                        self.assertEqual(caught.exception.code, "claim_conflict" if mode == "competitor" else "claim_wait_unresolved")
                        self.assert_no_writes()

    def test_wait_review_reports_full_legacy_evidence_without_changing_it(self):
        self.wait = None
        self.issue["body"] += "\nWaiting for: Chris decides scope"
        self.comments.append({"id": 14, "user": {"login": TEST_BOT}, "body": "Do not start until I approve"})
        self.args.all = self.args.clear = False
        self.args.waiting_for = self.args.ends_when = self.args.evidence = None
        original = copy.deepcopy((self.issue, self.comments))
        self.run_claim(command=PLAN.cmd_wait)
        result = self.emitted.call_args.args[0]
        self.assertTrue(result["review_required"])
        self.assertEqual(result["review_evidence"]["body"], self.issue["body"])
        self.assertEqual(result["review_evidence"]["comments"], self.comments)
        self.assertEqual((self.issue, self.comments), original)
        self.assert_no_writes()

    def test_closed_landing_can_review_and_resolve_wait_without_reopening_or_claiming(self):
        self.issue["state"] = "closed"
        self.wait = None
        self.args.all = self.args.clear = False
        self.args.waiting_for = self.args.ends_when = self.args.evidence = None
        original = copy.deepcopy(self.issue)
        self.run_claim(command=PLAN.cmd_wait)
        self.assertTrue(self.emitted.call_args.args[0]["review_required"])
        self.assert_no_writes()
        self.args.clear = True
        self.args.evidence = "Full completed finish line and Director acceptance reviewed"
        self.run_claim(command=PLAN.cmd_wait)
        self.assertEqual(self.emitted.call_args.args[0]["structured_wait"]["state"], "clear")
        self.assertEqual(self.issue, original)
        self.assertNotIn("status", self.events)
        self.assertNotIn("labels", self.events)
        with self.assertRaises(PLAN.PlanError):
            self.run_claim()

    def test_wait_review_records_real_hold_then_evidenced_release_without_editing_prose(self):
        self.wait = None
        self.args.all = self.args.clear = False
        self.args.waiting_for = "Chris's device test"
        self.args.ends_when = "Chris records the test result"
        self.args.evidence = "Director's recorded device-test request"
        original = copy.deepcopy(self.issue)
        self.run_claim(command=PLAN.cmd_wait)
        with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
        self.assertEqual(self.issue, original)
        self.args.waiting_for = self.args.ends_when = None
        self.args.clear = True
        self.args.evidence = "Chris recorded acceptance of the device test"
        self.run_claim(command=PLAN.cmd_wait)
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_wait_review_requires_evidence_and_an_ending_condition_before_writes(self):
        self.args.all = self.args.clear = False
        self.args.waiting_for = "Client accepts"
        self.args.ends_when = self.args.evidence = None
        with self.assertRaises(PLAN.PlanError): self.run_claim(command=PLAN.cmd_wait)
        self.assert_no_writes()

    def test_wait_review_preserves_a_concurrent_record_and_reports_partial_write(self):
        self.args.all = False
        self.args.clear = True
        self.args.waiting_for = self.args.ends_when = None
        self.args.evidence = "Earlier hold resolved"
        self.after_post = self.record_wait
        with self.assertRaises(PLAN.PlanError) as caught: self.run_claim(command=PLAN.cmd_wait)
        self.assertEqual(caught.exception.payload["completed_steps"], ["post_wait"])
        self.assertIn("comment", caught.exception.payload)
        terminal = PLAN.github_api_core.terminal_failure(caught.exception.failure,
            operation="github.plan.wait", payload=caught.exception.payload)
        self.assertEqual(terminal["completed_steps"], caught.exception.payload["completed_steps"])
        self.assertIsNone(terminal["write_outcome"])
        self.assertEqual(wait_record.records(self.comments[-1]["body"])[0]["state"], "waiting")

    def test_new_issue_creation_records_all_waits_or_reviewed_absence(self):
        for flags in (("--wait-clear",), ("--waiting-for", "Director test", "--ends-when", "Test accepted",
                      "--waiting-for", "Client price", "--ends-when", "Price confirmed")):
            with self.subTest(flags=flags):
                args = PLAN.build_parser().parse_args(["--repo", "owner/repo", "create", "Repair",
                    *flags, "--wait-evidence", "Full request reviewed"])
                create = Mock(return_value={"ok": True, "repo": "owner/repo", "number": 99,
                                            "url": "https://github.com/owner/repo/issues/99"})
                with patch.multiple(PLAN, default_repo=lambda repo: repo,
                        load_config=lambda repo: PLAN.DEFAULT_CONFIG,
                        find_existing_plan_issues=lambda *a, **k: ("bot", []),
                        ensure_labels=lambda *a, **k: ("bot", []),
                        refresh_planning_targets=lambda *a, **k: {"ok": True, "coverage": "complete", "issues": []},
                        comment_route=lambda: ("bot", "bot-gh", TEST_BOT), emit=Mock()), \
                        patch.object(github_issue, "create_issue", create):
                    PLAN.cmd_create(args)
                wait = wait_record.records(create.call_args.args[1])[0]
                self.assertEqual(len(wait["waits"]), len(args.waiting_for or []))
                self.assertEqual(wait["evidence"], args.wait_evidence)

    def test_wait_inventory_reports_each_unreviewed_open_issue_and_never_drops_prose(self):
        self.args.all = True
        self.args.issue = None
        self.args.clear = False
        self.args.waiting_for = self.args.ends_when = self.args.evidence = None
        pending = {**self.issue, "number": 43, "body": "Waiting for: Human test", "html_url": "https://github.com/owner/repo/issues/43"}
        reviewed = {**self.issue, "body": wait_record.marker(self.wait)}
        foreign = {**self.issue, "number": 44, "user": {"login": "someone"}, "body": wait_record.marker(self.wait)}
        closed_pr = {**self.issue, "number": 45, "pull_request": {}}
        def pages(path, **_kwargs):
            return "bot", [pending, reviewed, foreign, closed_pr] if path == "/repos/owner/repo/issues" else []
        with patch.multiple(PLAN, default_repo=lambda repo: repo, collect_paged_rest_items=pages, emit=self.emitted), \
                patch.object(github_identity, "configured_bot_logins", return_value=[TEST_BOT]):
            PLAN.cmd_wait(self.args)
        result = self.emitted.call_args.args[0]
        self.assertEqual({row["number"] for row in result["review_required"]}, {43, 44})
        self.assertEqual(result["checked"], 3)
        self.assertEqual(result["writes"], [])

    def test_malformed_comment_after_post_still_has_exact_release_recovery(self):
        self.after_post = lambda: self.comments.append({"id": 2, "body": "<!-- github-plan:claim invalid -->"})
        with self.assertRaises(PLAN.PlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.payload["claim_recovery"]["release_own_claim"]["body"], "Released claim 1")

    def test_milestone_admission_needs_no_graph_certificate(self):
        link = "https://github.com/owner/direction/issues/9#issuecomment-123"
        self.args.admission, self.args.admission_link = "milestone", [link]
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])
        self.assertIn(link, self.comments[-1]["body"])

    def test_identity_refused_required_blocker_read_refuses_claim_without_writes(self):
        failure = PLAN.github_api_core.FailureDetail(
            cause="identity_refused", message="Client App cannot read the blocker evidence",
            retryable=False, fallback_eligible=False, disposition="stop", write_outcome="not_started")
        error = PLAN.PlanError(failure.message, failure=failure)
        original = self.read_pages

        def pages(path, **kwargs):
            if path.endswith("/dependencies/blocked_by"):
                raise error
            return original(path, **kwargs)

        with patch.object(self, "read_pages", side_effect=pages), self.assertRaises(PLAN.PlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.failure.cause, failure.cause)
        self.assert_no_writes()

    def admit(self, kind, links=(), unlinked=None):
        self.args.admission, self.args.admission_link, self.args.admission_unlinked = kind, list(links), unlinked
        self.run_claim()

    def test_milestone_admission_records_optional_context(self):
        context = "The Director's Track is in a different account"
        self.admit("milestone", unlinked=context)
        self.assertIn(context, self.comments[-1]["body"])
        self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_partial_claim_recovery_reuses_existing_admission(self):
        self.args.admission = "milestone"
        link = "https://github.com/owner/direction/issues/9"
        self.args.admission_link = [link]
        with patch.object(self, "edit", side_effect=PLAN.PlanError("Status write failed")):
            with self.assertRaises(PLAN.PlanError):
                self.run_claim()
        original = copy.deepcopy(self.comments)
        self.run_claim()
        self.assertEqual(self.comments, original)
        self.assertIn(link, self.comments[-1]["body"])
        self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_repeat_stop_needs_both_occurrences_and_spare_capacity_is_named(self):
        first = "https://github.com/owner/repo/issues/5"
        for links in ((), (first,), (first, first)):
            self.setUp()
            with self.assertRaises(PLAN.PlanError):
                self.admit("repeat-stop", links)
            self.assert_no_writes()
        self.setUp()
        self.admit("repeat-stop", (first, "https://github.com/owner/other/pull/6"))
        self.assertIn(CLAIM.admission_text("repeat-stop", [first, "https://github.com/owner/other/pull/6"]), self.comments[-1]["body"])
        self.setUp()
        self.admit("spare-capacity")
        self.assertIn(CLAIM.admission_text("spare-capacity", []), self.comments[-1]["body"])

    def test_admission_details_require_the_kind_they_describe(self):
        for kind, links, unlinked in ((None, ("https://github.com/owner/repo/issues/5",), None),
                                      ("spare-capacity", (), "reason"),
                                      ("milestone", ("not a url",), None)):
            self.setUp()
            with self.assertRaises(PLAN.PlanError):
                self.admit(kind, links, unlinked)
            self.assert_no_writes()

    def test_admission_comment_links_keep_the_exact_evidence(self):
        first = "https://github.com/owner/repo/issues/5#issuecomment-123"
        for second in (
            "https://github.com/owner/other/pull/6#issuecomment-456",
            "https://github.com/owner/repo/issues/5#issuecomment-456",
        ):
            with self.subTest(second=second):
                self.setUp()
                self.admit("repeat-stop", (first, second, first.replace("/issues/", "/pull/")))
                self.assertIn(CLAIM.admission_text("repeat-stop", [first, second]), self.comments[-1]["body"])
                self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_spellings_of_one_citation_count_as_one_occurrence(self):
        # GitHub serves one number's issue and PR paths, and owner/repo case-insensitively.
        for first, second in (
            ("https://github.com/owner/repo/issues/5#issuecomment-123",
             "https://github.com/owner/repo/pull/5#issuecomment-123"),
            ("https://github.com/owner/repo/issues/5#issuecomment-123",
             "https://github.com/Owner/Repo/issues/5#issuecomment-123"),
            ("https://github.com/owner/repo/issues/5", "https://github.com/owner/repo/pull/5"),
        ):
            with self.subTest(second=second):
                self.setUp()
                with self.assertRaises(PLAN.PlanError):
                    self.admit("repeat-stop", (first, second))
                self.assert_no_writes()

    def test_invalid_admission_links_refuse_before_writes(self):
        parent = "https://github.com/owner/repo/issues/5"
        for link in (
            parent + "#issuecomment-0", parent + "#issuecomment-nope", parent + "#other-123",
            parent + "#issuecomment-123/extra", parent + "?query=1#issuecomment-123",
            parent + "#issuecomment-123\n", parent.replace("https:", "http:"),
            parent.replace("github.com", "github.com.example.org"),
            "https://github.com/owner/repo/issues/comments/123",
        ):
            with self.subTest(link=link):
                self.setUp()
                with self.assertRaises(PLAN.PlanError):
                    self.admit("spare-capacity", (link,))
                self.assert_no_writes()

    def test_comment_admission_preserves_ownership_and_wait_refusals(self):
        link = "https://github.com/owner/repo/issues/5#issuecomment-123"
        for blocked, code in (
            ("ownership", "claim_conflict"),
            ("wait", "claim_wait_unresolved"),
            ("blocker", "claim_native_blockers_unresolved"),
            ("human_claim", "claim_conflict"),
        ):
            with self.subTest(blocked=blocked):
                self.setUp()
                if blocked == "ownership":
                    self.inventory["worktrees"] = [{"path": "/retained/issue-42", "branch": OTHER["branch"]}]
                elif blocked == "blocker":
                    self.blockers = [{"state": "open"}]
                elif blocked == "human_claim":
                    self.issue["user"]["login"] = "human"
                    self.comments = [{"id": 1, "body": CLAIM.marker(OTHER), "user": {"login": "human"}}]
                else:
                    self.wait = wait_record.reviewed(evidence="Device test pending", waiting_for="Director",
                                                     ends_when="Device acceptance recorded")
                with self.assertRaises(PLAN.PlanError) as caught:
                    self.admit("milestone", (link,))
                self.assertEqual(caught.exception.code, code)
                self.assert_no_writes()


class GraphQLRepositoryIdentityTests(unittest.TestCase):
    def test_source_node_reads_select_its_app_independently_of_command_repository(self):
        for read, source_repo, command_repo in product(
                ("history", "owner_decision", "delivery"), ("client/data", "owner/repo"), ("client/command", "owner/command")):
            with self.subTest(read=read, source_repo=source_repo, command_repo=command_repo), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                environment = {"HOME": folder, "PATH": os.environ["PATH"], "GH_REPO": command_repo,
                               "CODEX_SKILLS_ENV_FILE": str(root / "missing.env"),
                               "CODEX_AUTOMATION_LOGIN": TEST_BOT, "GITHUB_CLIENT_APP_OWNERS": "client",
                               "GITHUB_RETRY_STATE_DIR": str(root / "retry"),
                               "GITHUB_REQUEST_USAGE_DIR": str(root / "usage")}
                client_actor = "fixture-client-app[bot]"
                expected_actor = client_actor if source_repo.startswith("client/") else TEST_BOT
                event = {"event": "sub_issue_added", "node_id": "fixture-event", "sub_issue": None}
                child = {"id": "fixture-child", "number": 43, "state": "OPEN", "title": "Child",
                         "url": f"https://github.com/{source_repo}/issues/43",
                         "repository": {"nameWithOwner": source_repo}}
                comment = {"id": 7, "node_id": "fixture-comment",
                           "body": "Director decision: Close #42 as not planned.",
                           "created_at": "2026-10-02T00:00:00Z", "user": {"login": TEST_BOT}}
                node = ({"id": event["node_id"], "__typename": "SubIssueAddedEvent", "subIssue": child}
                        if read == "history" else
                        {"databaseId": comment["id"], "body": comment["body"],
                         "createdAt": comment["created_at"], "lastEditedAt": None})
                authentications, executions = [], []

                def authenticate(_config, *, repository=None, **_):
                    authentications.append(repository)
                    actor = client_actor if github_identity.github_app_prefix(repository) == "GITHUB_CLIENT_APP" else TEST_BOT
                    return "synthetic-token", actor

                def execute(command, **kwargs):
                    # Exercise the wrapper's repository/App selection at the process boundary.
                    with patch.dict(os.environ, kwargs.get("env") or os.environ.copy(), clear=True):
                        repository = github_identity.repository_context()
                        actor = (client_actor if github_identity.github_app_prefix(repository) == "GITHUB_CLIENT_APP"
                                 else TEST_BOT)
                    executions.append((repository, actor))
                    body = ({"data": {"node": node, "repository": {"issue": {
                        "createdAt": "2026-10-01T00:00:00Z", "lastEditedAt": None}}}}
                            if actor == expected_actor else {"errors": [{"message": "Wrong App"}]})
                    return subprocess.CompletedProcess(command, 0, "HTTP/2.0 200\n\n" + json.dumps(body), f"acting as {actor}")

                reactions = [{"id": 8, "content": "+1", "created_at": "2026-10-03T00:00:00Z",
                              "user": {"login": source_repo.split("/")[0]}}]
                with patch.dict(os.environ, environment, clear=True), \
                        patch.multiple(PLAN, BOT_GH=Path(__file__).with_name("gh-with-env-token"),
                                       EXPECTED_ACTOR=TEST_BOT, CURRENT_OPERATION="github.plan.claim", NEXT_READS=None), \
                        patch.object(github_identity, "github_app_config", return_value=object()), \
                        patch.object(github_identity, "github_app_auth", side_effect=authenticate), \
                        patch.object(PLAN.subprocess, "run", side_effect=execute), \
                        patch.object(PLAN, "load_direction", return_value="## Milestones\n- `Inventory` proves the trial."), \
                        patch.object(PLAN, "collect_paged_rest_items", side_effect=lambda path, **_: (
                            "bot", reactions if path.endswith("/reactions") else [comment])):
                    if read == "history":
                        result = PLAN.read_handoff_history_child(event, source_repo)
                        self.assertEqual(result["html_url"], child["url"])
                    elif read == "owner_decision":
                        result = PLAN.check_owner_decides_not_planned(source_repo, 42, {
                            "milestone": {"title": "Inventory"}, "created_at": "2026-10-01T00:00:00Z"})
                        self.assertEqual(result["reaction_id"], reactions[0]["id"])
                    else:
                        result = PLAN.next_source_delivery({"repo": source_repo, "number": 42, "discussion": {
                            "body": "## Current Status\n\nState: Open.", "comments": [{"author": TEST_BOT,
                                "body": "## Current Status\n\nState: Waiting.", "created_at": "2026-10-02T00:00:00Z"}]}},
                            {TEST_BOT})
                        self.assertTrue(result["complete"])
                        self.assertEqual(result["body_recorded_at"], "2026-10-01T00:00:00Z")
                    self.assertTrue(executions)
                    self.assertTrue(all(execution == (source_repo, expected_actor) for execution in executions))
                    self.assertTrue(all(repository == source_repo for repository in authentications))
                    self.assertEqual(os.environ["GH_REPO"], command_repo)


if __name__ == "__main__":
    unittest.main()
