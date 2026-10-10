#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Behavioral tests for claim ownership, races, and partial recovery."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import github_plan_claim as CLAIM

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
        self.args = Namespace(repo="owner/repo", issue="42", worker=OWNER["worker"], session=OWNER["session"],
                              branch=OWNER["branch"], next_action="Implement the repair", resume_from=None, wait_resolved=None)
        self.issue: dict[str, Any] = {"repo": "owner/repo", "number": 42, "title": "Repair", "state": "open",
                      "user": {"login": TEST_BOT}, "labels": [],
                      "body": PLAN.PLAN_MANAGED_PROVENANCE_MARKER + "\n\n## Objective\n\nKeep me\n\n## Current Status\n\nState: Open, not started.\n"}
        self.args.planning_checkout = None
        self.targets = {}
        self.closed_pulls = {}
        self.api_failure = None
        self.configs = {}
        self.planning_inventory = None
        self.target_comments = {}
        self.comments: list[dict[str, Any]] = []
        self.inventory = {"worktrees": [], "local_branches": [], "remote_branches": [], "sessions": [],
                          "session_coverage": {"codex": {"status": "unavailable"}, "claude": {"status": "available"}}}
        self.pulls = []
        self.blockers = []
        self.events = []
        self.after_post = lambda: None
        self.after_status = lambda: None
        self.during_inventory = lambda: None
        self.emitted = Mock()

    def get_issue(self, ref, repo):
        self.events.append("read_issue")
        issue = copy.deepcopy(self.targets.get(str(ref), self.issue))
        issue["repo"], issue["number"] = PLAN.issue_ref(ref, repo)
        return "bot", issue

    def read_api(self, _method, path, **_):
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
        if path == "/repos/other/plans/pulls": items = []
        return "bot", copy.deepcopy(items)

    def post(self, _kind, _number, body, **_):
        self.events.append("post")
        self.post_route = (_kind, _number, _["repo"])
        comment_id = len(self.comments) + 1
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

    def run_claim(self):
        with patch.multiple(PLAN, default_repo=lambda repo: repo or "owner/repo", get_issue=self.get_issue,
                            EXPECTED_ACTOR=TEST_BOT,
                            collect_paged_rest_items=self.read_pages, rest_edit_issue=self.edit,
                            comment_route=lambda: ("bot", "bot-gh", TEST_BOT),
                            load_config=lambda repo, **_: copy.deepcopy(self.configs.get(repo, PLAN.DEFAULT_CONFIG)), emit=self.emitted, api_json=self.read_api), \
                patch.object(CLAIM, "local_inventory", side_effect=self.inventory_for), \
                patch.object(PLAN.github_identity, "configured_bot_logins", return_value=[TEST_BOT]), \
                patch.object(PLAN.github_comment_core, "comment", side_effect=self.post), \
                patch.object(PLAN.github_issue_core, "edit_issue", side_effect=self.labels):
            PLAN.cmd_claim(self.args)

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
        self.targets["99"]["body"] += "\n\n## Current Status\n\nState: Parked.\nWaiting for: Merge train."
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
        self.assertEqual(caught.exception.code, "claim_wait_unresolved")
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
                target = self.issue if place == "issue" else self.targets[place]
                target["body"] += "\n\n## Current Status\n\nWaiting for: Approved prerequisite readback."
                if place == "issue": target["labels"] = [{"name": PLAN.DEFAULT_CONFIG["labels"]["waiting"]}]
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "claim_wait_unresolved")
                self.assert_no_writes()
                self.args.wait_resolved = "Existing brief authorizes prerequisite only; keep the draft/readback hold."
                self.run_claim()
                self.assertTrue(self.pulls[0]["draft"])
                if place == "issue":
                    status, _ = PLAN.read_plan_sections(self.issue)
                    self.assertIn("Waiting for: Approved prerequisite readback.", status["Current Status"])
                    self.assertIn(PLAN.DEFAULT_CONFIG["labels"]["waiting"], PLAN.normalize_labels(self.issue["labels"]))
                    self.assertEqual(PLAN.next_plan_status(self.issue, PLAN.DEFAULT_CONFIG), "waiting")
                else:
                    self.assertIn(f"PR #{place}:\n", self.comments[-1]["body"])
                    self.assertIn("> Waiting for: Approved prerequisite readback.", self.comments[-1]["body"])
                    self.assertIn("> Waiting for: Approved prerequisite readback.", self.issue["body"])

    def test_ordinary_recovery_records_newly_verified_pr_wait_in_current_status(self):
        self.ordinary_handoff_fixture()
        self.run_claim()
        self.targets["99"]["labels"] = [{"name": PLAN.DEFAULT_CONFIG["labels"]["waiting"]}]
        self.args.wait_resolved = "Fresh authorization for this step; preserve the new PR hold."
        self.run_claim()
        self.assertEqual(self.events.count("post"), 1)
        self.assertIn("> Labels: " + PLAN.DEFAULT_CONFIG["labels"]["waiting"], self.issue["body"])
        self.assertIn(self.args.wait_resolved, self.issue["body"])

    def test_ordinary_handoff_preserves_parks_states_and_url_continuations(self):
        for hold in ("Parked until: owner approves the retained draft",
                     "State: parked until the owner approves the draft",
                     "Waiting for: approved prerequisite readback\nhttps://github.com/owner/repo/issues/123"):
            with self.subTest(hold=hold):
                self.setUp()
                self.ordinary_handoff_fixture()
                self.issue["body"] = self.issue["body"].split("## Current Status", 1)[0] + "## Current Status\n\n" + hold
                self.args.wait_resolved = "Prerequisite authorized; preserve the recorded product hold."
                self.run_claim()
                status, _ = PLAN.read_plan_sections(self.issue)
                self.assertIn(hold, status["Current Status"])
                self.assertTrue(PLAN.github_direction_next.waiting_records(PLAN.compact_issue(self.issue), status["Current Status"]))

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
        self.args.wait_resolved = "Prerequisite authorized; retain the review hold."
        self.run_claim()
        self.assertEqual(CLAIM.records(self.issue["body"]), [self.emitted.call_args.args[0]["claim"]])
        self.assertIn("Waiting for: Owner review.", self.issue["body"])

    def test_ordinary_handoff_refuses_changed_holds_on_each_readback(self):
        for phase in ("after_post", "after_status"):
            for place in ("issue", "99", "100"):
                with self.subTest(phase=phase, place=place):
                    self.setUp()
                    self.ordinary_handoff_fixture()
                    self.args.wait_resolved = "Prior prerequisite approval; no authority for a new hold."
                    def race():
                        target = self.issue if place == "issue" else self.targets[place]
                        target["body"] += "\n\n## Current Status\n\nWaiting for: New product prerequisite."
                    setattr(self, phase, race)
                    with self.assertRaises(PLAN.PlanError) as caught: self.run_claim()
                    self.assertIn("holds changed" if place == "issue" else "waits changed", str(caught.exception))
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

    def test_legacy_current_status_refuses(self):
        self.issue["body"] += "\nWorker: another session\nBranch: work/repair\n"
        with self.assertRaises(PLAN.ClassifiedPlanError):
            self.run_claim()
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

    def test_formatted_denials_preserve_real_and_ambiguous_holders(self):
        for status in (
            "Claimed by another-worker.", "Owned by another-worker.",
            "Worker: unknown", "Session: pending", "Worker:",
            "Claimed by no one except another-worker.",
            "Not claimed by the sweep but by another-worker.",
            "No ownership is claimed by this sweep because another-worker has work/repair open.",
        ):
            for formatted in format_ownership(status):
                with self.subTest(status=formatted):
                    self.setUp()
                    self.issue["body"] += "\n" + BD_SWEEP_STATUS + "\n" + formatted
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                        self.run_claim()
                    self.assertEqual(caught.exception.code, "claim_conflict")
                    self.assert_no_writes()

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

    def test_retracted_negations_and_visible_non_link_text_still_refuse(self):
        for status in (
            "Implementation is ~~not~~ claimed by another-worker.",
            "~~No~~ implementation ownership is claimed by another-worker.",
            "Not claimed by [this sweep](another-worker holds work/repair)",
            "No implementation ownership is claimed by [this sweep][another-worker still holds work/repair].",
            "No implementation ownership is claimed by `[this sweep](another-worker)`.",
            "No implementation ownership is claimed by `[this sweep][another-worker]`.",
            "No implementation ownership is claimed by this sweep.\n[Claimed by another-worker]: work/repair",
            "No implementation ownership is claimed by this sweep.\n\n[Claimed by another-worker]:",
            "Not claimed by [this sweep][another-worker holds repair].\n\n[another-worker holds repair]:",
        ):
            with self.subTest(status=status):
                self.setUp()
                self.issue["body"] += "\n" + status
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

    def test_literal_blocks_never_hide_visible_ownership(self):
        for opener, closer in (("~~~", "~~~"), ("```", ""), ("<pre>", "</pre>")):
            for holder in ("[Owned by another-worker]: work/repair",
                           "Not claimed by [this sweep](another-worker)"):
                with self.subTest(opener=opener, holder=holder):
                    self.setUp()
                    self.issue["body"] += f"\nNot claimed by this sweep.\n\n{opener}\n\n{holder}\n{closer}"
                    with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
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

    def test_quoted_wrapped_denial_preserves_qualifiers_and_fields(self):
        for status, conflict in (
            ("> No implementation ownership is claimed by\n> this sweep.", False),
            ("> No ownership is claimed by this sweep\n> because another-worker has work/repair open.", True),
            ("> No ownership is claimed by this sweep.\n> **Worker:** another-worker", True),
        ):
            with self.subTest(status=status):
                self.setUp()
                self.issue["body"] += "\n" + status
                if conflict:
                    with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                    self.assert_no_writes()
                else:
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_formatted_responsibility_prose_is_normalized_before_classification(self):
        for formatted in format_ownership(RESPONSIBILITY_STATUS):
            with self.subTest(status=formatted):
                conflicts, _ = CLAIM.discussion_evidence(formatted, [], OWNER)
                self.assertEqual(conflicts, [])
                conflicts, _ = CLAIM.discussion_evidence(formatted + "\n**Worker:** unknown", [], OWNER)
                self.assertTrue(conflicts)

    def test_disclaimer_does_not_hide_another_status_assertion(self):
        for ownership in (
            "Implementation is claimed by another-worker.",
            "Reclaimed by another-worker after the stale release.",
            "_Claimed by another-worker_", "_Owned by another-worker_",
            "Currently owned by another-worker",
            "Worker: another-worker", "Session: another-session",
            "Worker: unknown", "Session: pending", "Worker:",
        ):
            for separator in (" ", "\n", "; "):
                with self.subTest(ownership=ownership, separator=separator):
                    self.setUp()
                    self.issue["body"] += "\n" + SWEEP_DISCLAIMERS[0] + separator + ownership
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                        self.run_claim()
                    self.assertEqual(caught.exception.code, "claim_conflict")
                    self.assert_no_writes()

    def test_uncertain_denials_remain_ownership_evidence(self):
        for status in (
            "If implementation is not claimed by the current worker, start it.",
            "If approved: no ownership is claimed by the sweep.",
            "No evidence shows whether this issue is claimed by another-worker.",
            "No objection remains; implementation is claimed by another-worker.",
            "No blockers remain and this issue is claimed by another-worker.",
            "The repair is not claimed by the sweep but by another-worker.",
            "Work is not only owned by engineering; the Supervisor is involved.",
            "Claimed by no one except another-worker.",
            "Worker: None, pending confirmation from another-session.",
        ):
            with self.subTest(status=status):
                self.setUp()
                self.issue["body"] += "\n" + status
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assert_no_writes()

    def test_matching_identity_does_not_hide_a_second_holder(self):
        for marker in ("", CLAIM.marker(OWNER)):
            for ownership in ("Implementation is claimed by another-worker.",
                              "Worker: unknown", "Session: another-session"):
                with self.subTest(marker=marker, ownership=ownership):
                    self.setUp()
                    self.issue["body"] += (
                        f"\nState: Active; owned by {OWNER['worker']}\nWorker: {OWNER['worker']}"
                        f"\nSession: {OWNER['session']}\nBranch: {OWNER['branch']}\n{marker}"
                        "\n" + SWEEP_DISCLAIMERS[0] + "\n" + ownership
                    )
                    with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                        self.run_claim()
                    self.assertEqual(caught.exception.code, "claim_conflict")
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

    def test_recorded_next_action_is_intent_and_preserves_other_assertions(self):
        self.args.next_action = "Repair the sync code owned by the importer"
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])
        self.issue["body"] += "\nImplementation is claimed by another-worker."
        with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()

    def test_recorded_wait_resolution_does_not_reclaim_a_released_holder(self):
        self.ordinary_handoff_fixture()
        self.args.wait_resolved = "Previously claimed by trial-b; exact claim 1 was released and the brief authorizes successor work."
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])
        self.issue["body"] += "\nWorker: another-worker"
        with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()

    def test_questions_and_negated_exceptions_remain_ambiguous(self):
        for status in ("Is this issue not claimed by another-worker?",
                       "Not owned by anyone other than another-worker.",
                       "No work is not owned by another-worker."):
            with self.subTest(status=status):
                self.setUp()
                self.issue["body"] += "\n" + status
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                self.assert_no_writes()

    def test_denial_does_not_hide_narrative_handoff_or_active_work(self):
        for status in (
            "Not claimed by the sweep, which handed it to another-worker.",
            "Not claimed by the sweep while another-worker finishes the repair.",
            "No implementation ownership is claimed by this sweep because another-worker has work/repair open.",
        ):
            with self.subTest(status=status):
                self.setUp()
                self.issue["body"] += "\n" + status
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                self.assert_no_writes()

    def test_second_holder_with_caller_name_prefix_still_refuses(self):
        for assertion in ("Also owned by", "Worker:", "Session:"):
            for punctuation in (".", ";", "!", "?"):
                with self.subTest(assertion=assertion, punctuation=punctuation):
                    self.setUp()
                    key = "session" if assertion == "Session:" else "worker"
                    self.issue["body"] += "\n" + CLAIM.marker(OWNER)
                    self.issue["body"] += f"\n{assertion} {OWNER[key]}{punctuation}successor"
                    with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                    self.assert_no_writes()

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

    def test_wrapped_prose_preserves_denials_and_ambiguous_continuations(self):
        for status, conflict in (
            ("No implementation ownership is claimed by\nthis sweep.", False),
            ("No implementation ownership is claimed by this sweep\nbecause another-worker has work/repair open.", True),
            ("If the review lands first, this issue is\nnot claimed by another-worker.", True),
            ("Not claimed by claude-opus-5.5 but by another-worker.", True),
            ("Not claimed by claude-opus-5.5.", False),
            ("No ownership is claimed by this sweep\nbecause another-worker opened https://github.com/owner/repo/pull/12.", True),
            ("Not claimed by the sweep\nsince another-worker resumed it at 10:54 AM ET.", True),
            ("No ownership is claimed by this sweep.\nWorker: another-worker", True),
            ("No ownership is claimed by this sweep\n* Implementation is claimed by another-worker.", True),
        ):
            with self.subTest(status=status):
                self.setUp()
                self.issue["body"] += "\n" + status
                if conflict:
                    with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                    self.assert_no_writes()
                else:
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_formatted_empty_holders_do_not_hide_uncertainty(self):
        for holder in ("**none**", "`n/a`", "_None_", "unclaimed", "not assigned", "no-one"):
            with self.subTest(holder=holder):
                self.setUp()
                self.issue["body"] += f"\nWorker: {holder}\nSession: {holder}"
                self.run_claim()
                self.assertTrue(self.emitted.call_args.args[0]["ok"])
        for holder in ("TBD", "unknown", "pending", "**none** until the prior worker confirms"):
            with self.subTest(holder=holder):
                self.setUp()
                self.issue["body"] += f"\nWorker: {holder}"
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                self.assert_no_writes()

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
                self.issue["body"] += "\r\nReclaimed by another-worker after the release."
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()

    def test_no_change_summary_does_not_hide_a_copied_claim_header(self):
        for status in ("No changes\nOwned by trial-b", "- No code changes\n  Claimed by trial-b",
                       "No work - claimed by trial-b.", "No ownership\nOwned by trial-b"):
            with self.subTest(status=status):
                self.setUp()
                self.issue["body"] += "\n" + status
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                self.assert_no_writes()

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

    def test_unstructured_execution_ownership_refuses(self):
        for status in (
            "Owned by another-worker",
            "State: Active, owned by another-worker",
            "Implementation is owned by another-worker.",
            "Next action: Continue work owned by another-worker.",
            "Claimed by another-worker",
            "Worker: another-worker",
            "Session: another-session",
            "- Owned by another-worker",
            "**Owned by** another-worker",
            "State: Active\nCurrently owned by another-worker (session-x)",
            "Status: Active; owned by another-worker",
            "Repair owned by another-worker",
            "Fix owned by another-worker",
            "PR owned by another-worker",
            "Work on v1.2 owned by another-worker",
            "Owned by Launchplane engineering for evidence",
            RESPONSIBILITY_STATUS + "\nCurrently owned by another-worker",
            RESPONSIBILITY_STATUS + " Currently owned by another-worker",
            "Entries owned by another-worker",
            "Entries owned by another-worker; records owned by engineering for evidence and Chris for disposition approval.",
            "Entries claimed by another-worker and records owned by engineering for evidence and Chris for disposition approval.",
            "Records owned by engineering and implementation owned by another-worker for evidence and Chris for disposition approval.",
            "Entries remain while implementation is owned by another-worker for evidence and Chris for disposition approval.",
            RESPONSIBILITY_STATUS.removesuffix(".") + " (session-x)",
            "Implementation and records owned by another-worker for evidence and Chris for disposition approval.",
            "Fix and remaining entries owned by another-worker for evidence and Chris for production disposition approval.",
        ):
            with self.subTest(status=status):
                self.issue["body"] = PLAN.PLAN_MANAGED_PROVENANCE_MARKER + "\n\n## Current Status\n\n" + status
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assertEqual(caught.exception.payload["competing_evidence"][0]["source"], "current_status")
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
        self.targets["99"]["body"] = self.pulls[0]["body"] + "\n\n## Current Status\n\nWaiting for: Owner acceptance."
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.code, "claim_wait_unresolved")
        self.assert_no_writes()
        self.args.wait_resolved = "Existing source authorization preserves the acceptance hold."
        self.run_claim()
        self.assertIn("Owner acceptance", self.comments[-1]["body"])

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
        with self.assertRaisesRegex(PLAN.PlanError, "resolved native blockers"):
            self.run_claim()
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
                       "missing_time", "naive_time", "second_holder", "later_claim"):
            with self.subTest(change=change):
                self.setUp()
                self.released_note_fixture()
                source, note, release = self.comments
                if change == "foreign_note": note["user"]["login"] = "other"
                if change == "foreign_release": release["user"]["login"] = "other"
                if change == "wrong_id": note["body"] = note["body"].replace("#1", "#99")
                if change == "wrong_worker": note["body"] = note["body"].replace("trial-b", "trial-c")
                if change == "different_session": note["body"] += "\nSession: another-session"
                if change == "reused_worker": self.comments.insert(1, {**source, "id": 4, "body": CLAIM.marker({**OTHER, "session": "reused-session"})})
                if change == "unreleased": self.comments.pop()
                if change == "early_release": release["created_at"] = "2026-09-30T23:59:00Z"
                if change == "edited_source": source["updated_at"] = "2026-10-01T00:03:00Z"
                if change == "edited_note": note["updated_at"] = "2026-10-01T00:03:00Z"
                if change == "missing_time": note.pop("created_at")
                if change == "naive_time": note["created_at"] = "2026-10-01T00:01:00"
                if change == "second_holder": note["body"] += "\nOwned by another-worker."
                if change == "later_claim": self.comments.append({**source, "id": 4})
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assert_no_writes()

    def test_compound_status_identity_is_history_only_after_exact_release(self):
        # launchplane#3145, LP-3145-D2 refusal reported on #1529.
        self.released_note_fixture()
        self.issue["body"] += ("Session: trial-b / session-b; executing claim released in the closeout comment.\n"
                               "Branch: work/other-42\n")
        self.run_claim()
        self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_compound_status_preserves_unproved_or_live_holders(self):
        for change in ("unreleased", "foreign_release", "edited_source", "missing_time", "other_session",
                       "second_holder", "live_session", "unreleased_comment", "marker"):
            with self.subTest(change=change):
                self.setUp()
                self.released_note_fixture()
                self.issue["body"] += "Session: trial-b / session-b; executing claim released in the closeout comment.\n"
                if change == "unreleased": self.comments.pop()
                if change == "foreign_release": self.comments[-1]["user"]["login"] = "other"
                if change == "edited_source": self.comments[0]["updated_at"] = "2026-10-01T00:03:00Z"
                if change == "missing_time": self.comments[-1].pop("created_at")
                if change == "other_session": self.issue["body"] = self.issue["body"].replace("/ session-b;", "/ other-session;")
                if change == "second_holder": self.issue["body"] += "Owned by another-worker."
                if change == "live_session": self.inventory["sessions"] = [{"sessionId": "session-b", "cwd": "/unrelated"}]
                if change == "unreleased_comment": self.comments.append({"id": 4, "body": "Claimed by another-worker", "user": {"login": TEST_BOT}})
                if change == "marker": self.issue["body"] += CLAIM.marker(OTHER)
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught: self.run_claim()
                self.assertEqual(caught.exception.code, "claim_conflict")
                self.assert_no_writes()

    def test_quoted_replaced_session_field_does_not_claim_current_issue(self):
        # launchplane#3145, LP-3145-D3: unassigned field plus quoted old field.
        history = ('The previous "Session: trial-b" line was stale and made the next claim refuse as ambiguous '
                   'ownership ([prior record](https://github.com/owner/repo/issues/42#issuecomment-1), '
                   'catalog#1529); this status replaces it.')
        for competing in ("", "\nSession: another-session", "\nOwned by another-worker", "\n" + CLAIM.marker(OTHER)):
            with self.subTest(competing=competing):
                self.setUp()
                self.issue["body"] += "Session: unassigned\nLast verified: " + history + competing
                if competing:
                    with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                    self.assert_no_writes()
                else:
                    self.run_claim()
                    self.assertTrue(self.emitted.call_args.args[0]["ok"])

    def test_incomplete_or_uncertain_historical_session_prose_still_refuses(self):
        for status in ('The previous "Session: trial-b" line was stale but ownership is unresolved.',
                       'Last verified: "Session: trial-b"',
                       'The previous "Session: trial-b" line may be stale.',
                       'Session: trial-b; previously released.'):
            with self.subTest(status=status):
                self.setUp()
                self.issue["body"] += status
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
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

    def test_other_issue_prose_does_not_hide_local_or_uncertain_ownership(self):
        for status in (
            "Blocked by: https://github.com/owner/repo/issues/42 (actively owned by trial-b).",
            "Blocked by: #198 (actively owned by trial-b).",
            "Blocked by: https://github.com/owner/upstream/issues/198 (actively owned by trial-b and another-worker).",
            "Blocked by: https://github.com/owner/upstream/issues/198 (actively owned by trial-b).\nSession: unknown",
            "Blocked by: https://github.com/owner/upstream/issues/198 (actively owned by trial-b). This issue is owned by trial-c.",
        ):
            with self.subTest(status=status):
                self.setUp()
                self.issue["body"] += status
                self.args.wait_resolved = "The upstream prerequisite landed."
                with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
                self.assert_no_writes()
        self.setUp()
        self.issue["body"] += "Blocked by: https://github.com/owner/upstream/issues/198 (actively owned by trial-b)."
        self.args.wait_resolved = "The upstream prerequisite landed."
        self.compete()
        with self.assertRaises(PLAN.ClassifiedPlanError): self.run_claim()
        self.assert_no_writes()

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
                self.assertEqual(output["previous_current_status"], previous)
                self.assertEqual(CLAIM.records(self.issue["body"]), [output["claim"]])
                self.assertIn("metadata_readback", output["completed_steps"])
                self.assertEqual(len(self.comments), 3)

    def test_resume_preserves_unproven_or_competing_status_and_artifacts(self):
        for change in ("no_resume", "wrong_author", "wrong_id", "id_suffix", "conditional_release", "before_marker", "same_time",
                       "missing_time", "naive_time", "edited_source", "different_marker", "extra_status",
                       "worker_prefix", "state_worker_prefix", "reused_inline_worker", "extra_comment", "other_branch", "live_peer"):
            with self.subTest(change=change):
                self.setUp()
                self.released_status_fixture()
                if change == "no_resume": self.args.resume_from = None
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
                if change == "extra_status": self.issue["body"] += "\nOwned by another-worker\nSession: another-session"
                if change == "worker_prefix": self.issue["body"] += "\nOwned by trial-b.review"
                if change == "state_worker_prefix": self.issue["body"] = self.issue["body"].replace("owned by trial-b.", "owned by trial-b.review.")
                if change == "reused_inline_worker": self.issue["body"] += "\nalso owned by trial-b (session s2)"
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
        self.issue["body"] += "\nWaiting for: Owner approval of the live-host check."
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
        self.args.wait_resolved = "Owner selected A in the recorded decision"
        self.run_claim()
        self.assertIn("Waiting for: owner choice between A and B", self.comments[-1]["body"])
        self.assertIn("owner choice", self.emitted.call_args.args[0]["previous_current_status"])

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

    def test_wait_formats_refuse_before_writes(self):
        for status in ("State: Waiting\nNext action: owner chooses A or B", "State: Open\n- Waiting for: owner choice",
                       "State: Open\nBlocked by: No native issue blocker;\n  waiting for an owner decision"):
            with self.subTest(status=status):
                self.issue["body"] = PLAN.PLAN_MANAGED_PROVENANCE_MARKER + "\n## Current Status\n" + status
                with self.assertRaises(PLAN.ClassifiedPlanError):
                    self.run_claim()
                self.assert_no_writes()

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

    def test_formatted_blocked_by_refuses_before_writes(self):
        for field in ("Blocked by:", "**Blocked by:**", "**Blocked by**:", "_Blocked by:_"):
            with self.subTest(field=field):
                self.setUp()
                self.issue["body"] += field + " Chris approval\nWaiting for: None.\n"
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "claim_wait_unresolved")
                self.assert_no_writes()

    def test_global_invalid_wait_selection_still_requires_actual_claim_resolution(self):
        import test_gh_plan_next as selection
        root = selection.track("someone/direction", 1, "First")
        leaf = selection.global_issue("owner/repo", 42, labels=["plan", "plan:waiting"],
            body=PLAN.PLAN_MANAGED_PROVENANCE_MARKER + "\n## Current Status\nState: Waiting.\nWaiting for: starts after First\nBlocked by: None.")
        edges = {("someone/direction", 1): selection.relationships(sub_issues=[leaf])}
        with selection.global_fixture([root], [leaf], edges) as (module, result, _reads):
            module.cmd_next(selection.next_args())
            candidate = next(item for item in result["candidates"] if item["number"] == 42)
            context = {"issues": {"owner/repo#42": selection.reviewed(candidate, category="milestone")}}
            with patch.object(module, "next_selection_context", return_value=context):
                module.cmd_next(selection.next_args())
            self.assertEqual([item["number"] for item in result["available_candidates"]], [42])
        self.issue.update(leaf)
        self.issue["user"] = {"login": TEST_BOT}
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

    def test_no_wait_prefix_does_not_hide_real_wait(self):
        statuses = (
            "Blocked by: No native issue blocker; waiting for owner decision",
            "Blocked by: No native issue blocker;\n  waiting for owner decision",
            "Blocked by: none. Waiting for owner decision.",
            "Blocked by: none. Pending physical confirmation.",
            "Blocked by: None.\nWaiting for: Physical confirmation after implementation.",
            "Blocked by: None.\nWaiting for: Nothing for read-only investigation; waiting for owner decision.",
            "Blocked by: None.\nWaiting for: Nothing for read-only investigation until owner approval.",
            "Blocked by: None.\nParked until: Nothing for read-only investigation.",
            "Blocked by: None.\n**Parked until:** Nothing for read-only investigation.",
            "Blocked by: None.\nWaiting for: None; an agent acts next after Client acceptance.",
            "Blocked by: None.\nWaiting for: None; the next actor is an agent; pending Chris approval.",
            "Blocked by: None.\nWaiting for: None; an agent acts next.\n  Waiting for Chris to approve.",
        )
        for status in statuses:
            with self.subTest(status=status):
                self.setUp()
                self.issue["body"] += status + "\n"
                with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
                    self.run_claim()
                self.assertEqual(caught.exception.code, "claim_wait_unresolved")
                self.assert_no_writes()

    def test_no_wait_text_does_not_override_native_blocker(self):
        self.issue["body"] += "Blocked by: No native issue blocker.\nWaiting for: nothing.\n"
        self.blockers = [{"state": "open", "number": 41}]
        with self.assertRaises(PLAN.PlanError):
            self.run_claim()
        self.assert_no_writes()

    def test_wait_labels_refuse_until_existing_resolution_recorded(self):
        self.issue["labels"] = [{"name": PLAN.DEFAULT_CONFIG["labels"]["waiting"]}]
        with self.assertRaises(PLAN.ClassifiedPlanError):
            self.run_claim()
        self.assert_no_writes()
        self.args.wait_resolved = "Recorded owner release on this issue"
        self.run_claim()
        self.assertEqual(PLAN.normalize_labels(self.issue["labels"]), [PLAN.DEFAULT_CONFIG["labels"]["active"]])

    def test_failed_final_readback_returns_prior_status_for_recovery(self):
        self.after_status = self.compete
        with self.assertRaises(PLAN.ClassifiedPlanError) as caught:
            self.run_claim()
        self.assertIn("State: Open, not started.", caught.exception.payload["previous_current_status"])

    def test_malformed_comment_after_post_still_has_exact_release_recovery(self):
        self.after_post = lambda: self.comments.append({"id": 2, "body": "<!-- github-plan:claim invalid -->"})
        with self.assertRaises(PLAN.PlanError) as caught:
            self.run_claim()
        self.assertEqual(caught.exception.payload["claim_recovery"]["release_own_claim"]["body"], "Released claim 1")


if __name__ == "__main__":
    unittest.main()
