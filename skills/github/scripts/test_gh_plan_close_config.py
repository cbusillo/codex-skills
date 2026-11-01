#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline closure routing with distinct caller and target configurations."""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).with_name("gh-plan.py")
sys.path.insert(0, str(SCRIPT.parent))
import github_work_graph
import github_plan_claim
SPEC = importlib.util.spec_from_file_location("gh_plan_close_config_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PLAN: Any = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)


def comment_check(attention: list[dict] | None = None, linked: list[dict] | None = None):
    """Stub the comment radar and the linked-thread reads behind close's comment check."""
    reader = Mock()
    reader.paged_json.side_effect = lambda path, **_: (linked or []) if path.endswith("/sub_issues") else []
    radar = Mock(return_value={"status": "attention" if attention else "clear", "attention": attention or [],
                               "coverage": {"errors": []}})
    return patch.multiple(PLAN.github_close_attention, GitHubReader=Mock(return_value=reader), run_radar=radar), radar


class NotPlannedScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.issue: dict[str, Any] = {"number": 7, "user": {"login": "fixture-bot"}, "title": "Extra benchmark",
                      "body": "Agent-added requirement beyond the requested outcome.", "labels": [],
                      "milestone": {"title": "Catalog adoption"}, "created_at": "2026-10-01T00:00:00Z"}
        self.comments: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = [{"event": "milestoned", "actor": {"login": "fixture-bot"},
                        "created_at": "2026-10-02T00:00:00Z"}]
        self.reactions: list[dict[str, Any]] = []
        self.reason = "Remove the agent-added benchmark; the agreed delivery needs no benchmark."
        self.editors: list[dict[str, Any]] = []
        self.more_edits = False
        self.edit_pages: list[dict[str, Any]] = []
        self.patches = patch.multiple(PLAN,
            load_direction=Mock(return_value="## Milestones\n- `Catalog adoption` proves adoption."),
            wait_authors=Mock(return_value={"fixture", "fixture-bot"}),
            EXPECTED_ACTOR="fixture-bot",
            api_json=Mock(side_effect=self.api),
            collect_paged_rest_items=Mock(side_effect=self.pages))
        self.patches.start()
        self.addCleanup(self.patches.stop)

    def api(self, _method: str, _path: str, _body: Any = None, **kwargs: Any) -> tuple[str, dict[str, Any]]:
        step = kwargs["failed_step"]
        if step == "read_issue_last_edited":
            return "fixture-bot", {"data": {"repository": {"issue": {"lastEditedAt": "2026-10-04T00:00:00Z"}}}}
        if step == "scope_cut_editors":
            assert isinstance(_body, dict)
            cursor = _body["variables"]["cursor"]
            page = (self.edit_pages[0 if cursor is None else 1] if self.edit_pages else {
                "nodes": self.editors, "pageInfo": {"hasNextPage": self.more_edits}})
            return "fixture-bot", {"data": {"repository": {"issue": {"userContentEdits": page}}}}
        raise AssertionError(step)

    def pages(self, path: str, **_: Any) -> tuple[str, list[dict[str, Any]]]:
        if path.endswith("/timeline"):
            return "fixture-bot", self.events
        if path.endswith("/reactions"):
            return "fixture-bot", self.reactions
        if path.endswith("/comments"):
            return "fixture-bot", self.comments
        raise AssertionError(path)

    def check(self) -> dict[str, Any]:
        return PLAN.check_owner_decides_not_planned("fixture/target", 7, self.issue, scope_cut_reason=self.reason)

    def test_unadmitted_agent_requirement_can_be_cut_with_a_recorded_reason(self) -> None:
        result = self.check()
        self.assertFalse(result["required"])
        self.assertEqual(result["scope_cut_reason"], self.reason)
        self.reason += " No Director asked for this and it is not a client request; this is not a security finding."
        self.comments = [{"id": 8, "user": {"login": "fixture-bot"}, "body": self.reason}]
        self.assertFalse(self.check()["required"])

    def test_an_extra_production_build_benchmark_can_be_removed(self) -> None:
        self.issue["body"] = "Add an extra benchmark to the production build of the client library."
        self.editors = [{"editor": {"login": "fixture-bot", "__typename": "User"}}]
        self.assertFalse(self.check()["required"])

    def test_scope_cut_requires_a_reason_and_known_agent_provenance(self) -> None:
        for author, reason in (("fixture-bot", ""), ("fixture", self.reason), ("client", self.reason),
                               ("outside-user", self.reason), ("unknown-bot", self.reason)):
            with self.subTest(author=author, reason=reason):
                self.issue["user"]["login"], self.reason = author, reason
                with self.assertRaises(PLAN.PlanError):
                    self.check()

    def test_director_milestone_admission_survives_agent_reassignment(self) -> None:
        self.events.insert(0, {"event": "milestoned", "actor": {"login": "fixture"}})
        with self.assertRaises(PLAN.PlanError):
            self.check()

    def test_unknown_or_missing_milestone_history_refuses(self) -> None:
        for events in ([], [{"event": "milestoned"}],
                       [{"event": "milestoned", "actor": {"login": "outside-user"}}]):
            with self.subTest(events=events):
                self.events = events
                with self.assertRaises(PLAN.PlanError):
                    self.check()

    def test_human_discussion_and_recorded_decisions_keep_the_existing_guard(self) -> None:
        for author, body in (("fixture", "Keep this after the direction audit."),
                             ("client", "We need this behavior."), ("outside-user", "Still broken."),
                             ("fixture-bot", "Director decision Q4: Keep this requirement.")):
            with self.subTest(author=author):
                self.comments = [{"id": 9, "user": {"login": author}, "body": body,
                                  "created_at": "2026-10-03T00:00:00Z"}]
                with self.assertRaises(PLAN.PlanError):
                    self.check()

    def test_director_reaction_admitting_work_requires_a_close_decision(self) -> None:
        self.comments = [{"id": 9, "user": {"login": "fixture-bot"}, "body": "Keep this after audit."}]
        self.reactions = [{"content": "+1", "user": {"login": "fixture"}}]
        with self.assertRaises(PLAN.PlanError):
            self.check()

    def test_protected_work_cannot_use_the_agent_scope_exception(self) -> None:
        for label in ("security", "client-request", "outside-user-report", "live-site", "incident"):
            with self.subTest(label=label):
                self.issue["labels"] = [{"name": label}]
                with self.assertRaises(PLAN.PlanError):
                    self.check()
        self.issue["labels"] = []
        self.issue["body"] = "Director decision: Deliver this as part of the agreed outcome."
        with self.assertRaises(PLAN.PlanError):
            self.check()

    def test_incomplete_admission_reads_fail_before_closure(self) -> None:
        for suffix in ("/timeline", "/comments", "/reactions"):
            with self.subTest(suffix=suffix):
                def pages(path: str, **kwargs: Any) -> tuple[str, list[dict[str, Any]]]:
                    if path.endswith(suffix):
                        raise PLAN.PlanError("Admission evidence unavailable")
                    return self.pages(path, **kwargs)
                with patch.object(PLAN, "collect_paged_rest_items", side_effect=pages), self.assertRaises(PLAN.PlanError):
                    self.check()

    def test_director_activity_and_body_edits_preserve_admission(self) -> None:
        for event in ("labeled", "assigned", "renamed", "cross-referenced"):
            with self.subTest(event=event):
                self.events.append({"event": event, "actor": {"login": "fixture"}})
                with self.assertRaises(PLAN.PlanError):
                    self.check()
                self.events.pop()
        for editors, more in (([{"editor": {"login": "fixture", "__typename": "User"}}], False), ([], True)):
            self.editors, self.more_edits = editors, more
            with self.assertRaises(PLAN.PlanError):
                self.check()

    def test_recorded_director_decisions_keep_the_guard(self) -> None:
        for text in ("Director decision Q4: keep it.", "Owner decision: keep it."):
            with self.subTest(text=text):
                self.issue["body"] = text
                with self.assertRaises(PLAN.PlanError):
                    self.check()

    def test_agent_link_from_an_approved_parent_does_not_admit_the_extra_child(self) -> None:
        self.events.append({"event": "cross-referenced", "actor": {"login": "fixture-bot"},
            "source": {"issue": {"user": {"login": "fixture"}, "body": "Director decision: deliver the product."}}})
        self.assertFalse(self.check()["required"])

    def test_body_editor_pages_include_later_human_admission(self) -> None:
        self.edit_pages = [{"nodes": [{"editor": {"login": "fixture-bot", "__typename": "User"}}],
                            "pageInfo": {"hasNextPage": True, "endCursor": "next-edit-page"}},
                           {"nodes": [], "pageInfo": {"hasNextPage": False}}]
        self.assertFalse(self.check()["required"])
        self.edit_pages[1]["nodes"] = [{"editor": {"login": "fixture", "__typename": "User"}}]
        with self.assertRaises(PLAN.PlanError):
            self.check()

    def test_any_human_reaction_keeps_the_decision_guard(self) -> None:
        for author, content in (("fixture", "heart"), ("client", "+1"), ("outside-user", "rocket")):
            self.reactions = [{"content": content, "user": {"login": author}}]
            with self.assertRaises(PLAN.PlanError):
                self.check()

    def test_a_recent_director_keep_comment_is_not_a_close_decision(self) -> None:
        self.comments = [{"id": 9, "user": {"login": "fixture"}, "body": "Keep this after the audit.",
                          "created_at": "2026-10-05T00:00:00Z"}]
        with self.assertRaises(PLAN.PlanError):
            self.check()

    def test_recent_director_close_decision_still_allows_admitted_work(self) -> None:
        self.issue["user"]["login"] = "fixture"
        self.comments = [{"user": {"login": "fixture"}, "body": "Close #7 as not planned.",
                          "created_at": "2026-10-05T00:00:00Z", "html_url": "https://example.test/decision"}]
        for body in ("Close #7 as not planned.", "Close #7 as not planned.\r\nNo longer needed.",
                     "Close #7 as not planned.  \nNo longer needed."):
            self.comments[0]["body"] = body
            result = self.check()
            self.assertTrue(result["required"])
            self.assertEqual(result["comment_url"], self.comments[0]["html_url"])



class CloseConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        # These routing fixtures have no dependents; native propagation is
        # exercised with a real graph in test_github_plan_landing.py.
        context = patch.object(PLAN.github_plan_landing, "neighborhood", return_value=[])
        context.start()
        self.addCleanup(context.stop)

    @staticmethod
    def show_fixture(*, enrichment: Any = None, launch: bool = False, comments_extra: list[dict] | None = None,
                     pulls: list[dict] | None = None, action: str = "Implement export.", resume: int | str | None = None,
                     read_only: bool = False, expired_sessions: set[str] | None = None,
                     source_comments: list[dict] | None = None, admission: str | None = None,
                     admission_links: list[str] | None = None, milestone: dict | None = None,
                     track: dict | None = None, actor: str | None = None, worker: str | None = None, body_time: str | None = None,
                     ancestry: set[tuple[str, int]] | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        target = "fixture/target"
        configs = {repo: copy.deepcopy(PLAN.DEFAULT_CONFIG) for repo in ("fixture/local", target)}
        configs[target]["labels"] = {key: f"target:{key}" for key in configs[target]["labels"]}
        names = configs[target]["labels"]
        issue: dict[str, Any] = {"repo": target, "number": 7, "state": "open", "title": "Target request",
                 "html_url": f"https://github.com/{target}/issues/7", "user": {"login": "fixture"},
                 "body": f"Original human request.\n## Current Status\nState: Blocked.\nNext action: {action}",
                 "labels": [{"name": names[key]} for key in ("plan", "blocked")]}
        issue["milestone"] = milestone
        if body_time:
            issue.update(created_at=body_time, last_edited_at=body_time, updated_at="2026-10-27T00:00:00Z")
        if actor:
            issue["body"] += f"\nNext actor: {actor}"
        comments: list[dict[str, Any]] = [{"id": 1, "user": {"login": "fixture"}, "body": PLAN.github_plan_wait.marker(
            PLAN.github_plan_wait.reviewed(evidence="Target discussion reviewed; no person wait"))}]
        comments.extend(comments_extra or [])
        relationships = Mock(return_value=("fixture-bot", {"blocked_by": [], "sub_issues": []}))
        if isinstance(enrichment, Exception):
            relationships.side_effect = enrichment
        elif enrichment is not None:
            relationships.return_value = ("fixture-bot", enrichment)
        output = io.StringIO()
        parent = {**issue, "number": 6}
        with patch.multiple(PLAN, default_repo=Mock(return_value="fixture/local"),
                load_config=Mock(side_effect=lambda repo, **_: configs[repo]),
                api_json=Mock(side_effect=AssertionError("Unexpected API call in read-only show")),
                get_issue=Mock(side_effect=lambda ref, repo: ("fixture-bot", track if repo == "fixture/direction" else issue)),
                milestone_direction_repo=Mock(return_value="fixture/direction"),
                load_direction=Mock(side_effect=lambda repo: "## Milestones\n- `" + ("Launch product" if repo == "fixture/direction" else "Catalog adoption") + "` proves delivery.\n"),
                milestone_claim_ancestry=Mock(return_value=Mock(nodes=ancestry or set(), complete=True)),
                collect_paged_rest_items=Mock(return_value=("fixture-bot", comments)),
                read_issue_relationships=relationships, wait_authors=Mock(return_value={"fixture"}),
                claim_snapshot=Mock(return_value=(parent, "", source_comments or [], False))), \
                patch.object(github_plan_claim, "expired_lease_sessions", return_value=expired_sessions or set()), \
                patch.object(github_plan_claim, "local_inventory", return_value={
                    "local_branches": [], "remote_branches": [], "worktrees": [], "sessions": []}), \
                patch.object(github_work_graph, "ownership_inventory", return_value={
                    "complete": True, "pulls": pulls or [], "coverage": {}, "inventory": {}}), contextlib.redirect_stdout(output):
            PLAN.cmd_show(PLAN.build_parser().parse_args(["--repo", "fixture/local", "show", "fixture/target#7", "--full",
                                                        *(["--launch-check"] if launch else []),
                                                        *(["--worker", worker] if worker else []),
                                                        *(["--resume-from", str(resume)] if resume else []),
                                                        *(["--read-only"] if read_only else []),
                                                        *(["--admission", admission] if admission else []),
                                                        *[word for link in admission_links or [] for word in ("--admission-link", link)]]))
        return json.loads(output.getvalue()), issue, configs[target]

    def test_qualified_show_projects_with_the_target_custom_label_configuration(self) -> None:
        result, issue, target_config = self.show_fixture()
        self.assertTrue(result["ok"])
        self.assertEqual(result["issue"]["derived"]["status"], "active")
        self.assertEqual(set(result["issue"]["labels"]),
                         {target_config["labels"][key] for key in ("plan", "active")})
        self.assertEqual(result["issue"]["body"], issue["body"])
        self.assertEqual(result["issue"]["recorded_labels"], PLAN.normalize_labels(issue["labels"]))

    def test_show_preserves_the_original_read_when_graph_enrichment_is_unavailable(self) -> None:
        denied = PLAN.PlanError("Fixture dependency read denied", failure=PLAN.github_api_core.FailureDetail(
            cause="permission_denied", message="Dependency endpoint returned 403", retryable=False,
            fallback_eligible=False, disposition="stop"))
        partial = {"blocked_by": [{"repo": "fixture/gate", "number": 8, "state": "unknown"}], "sub_issues": []}
        for enrichment in (denied, partial):
            with self.subTest(enrichment=enrichment):
                result, issue, _ = self.show_fixture(enrichment=enrichment)
                self.assertTrue(result["ok"])
                self.assertEqual(result["issue"]["derived"]["status"], "review")
                self.assertEqual(result["issue"]["body"], issue["body"])
                self.assertEqual(result["issue"]["number"], issue["number"])
                self.assertEqual(len(result["issue"]["comments"]), 1)

    def test_launch_stays_refused_until_the_native_blocker_closes(self) -> None:
        blocker = {"repo": "fixture/gate", "number": 8, "state": "open"}
        for state in ("open", "open", "closed"):
            with self.subTest(state=state):
                blocker["state"] = state
                result, _, _ = self.show_fixture(launch=True, enrichment={"blocked_by": [blocker], "sub_issues": []})
                self.assertEqual(result["issue"]["launch_check"]["allowed"], state == "closed")

    def test_launch_requires_wait_resolution_and_exact_claim_release_without_retained_pr(self) -> None:
        source = {"worker": "codex", "session": "source", "branch": "work/export", "claimed_at": "2026-10-01T00:00:00Z"}
        claim = {"id": 2, "user": {"login": "fixture"}, "body": PLAN.github_plan_claim.marker(source)}
        release = {"id": 3, "user": {"login": "fixture"}, "body": "Released claim 2"}
        waiting = {"id": 4, "user": {"login": "fixture"}, "body": PLAN.github_plan_wait.marker({
            "version": 1, "state": "waiting", "evidence": "Director holds the export", "waits": [
                {"waiting_for": "Director", "ends_when": "Director resumes export"}]})}
        retained = {"number": 80, "state": "open", "title": "Export", "body": "Closes #7", "head": {"ref": source["branch"]}}
        for comments, pulls, allowed in (([claim], [], False), ([claim], [], False),
                                          ([claim, release], [retained], False), ([claim, release, waiting], [], False),
                                          ([claim, release], [], True)):
            with self.subTest(comments=comments, pulls=pulls):
                result, _, _ = self.show_fixture(launch=True, comments_extra=comments, pulls=pulls)
                self.assertEqual(result["issue"]["launch_check"]["allowed"], allowed)

    def test_launch_allows_operational_work_and_only_authored_retained_handoffs(self) -> None:
        for action in ("Audit the helper.", "Run the testing migration.", "Qualify the release."):
            result, _, _ = self.show_fixture(launch=True, action=action)
            self.assertTrue(result["issue"]["launch_check"]["allowed"])
        source = {"worker": "codex", "session": "source", "branch": "work/export", "claimed_at": "2026-10-01T00:00:00Z"}
        claim = {"id": 2, "user": {"login": "fixture"}, "body": PLAN.github_plan_claim.marker(source)}
        release = {"id": 3, "user": {"login": "fixture"}, "body": "Released claim 2\n\nRetained PR: https://github.com/fixture/target/pull/80"}
        retained: dict[str, Any] = {"number": 80, "state": "open", "user": {"login": "fixture"}, "body": "Closes #7",
                    "head": {"ref": source["branch"], "repo": {"full_name": "fixture/target"}},
                    "base": {"repo": {"full_name": "fixture/target"}}}
        for author, allowed in (("foreign", False), ("fixture", True)):
            release["user"] = {"login": author}
            result, _, _ = self.show_fixture(launch=True, comments_extra=[claim, release], pulls=[retained], resume=2)
            self.assertEqual(result["issue"]["launch_check"]["allowed"], allowed)
        for body in ("Released claim 2", "Released claim 2\n\nMerged PR: https://github.com/fixture/target/pull/80"):
            release["body"] = body
            result, _, _ = self.show_fixture(launch=True, comments_extra=[claim, release], resume=2)
            self.assertTrue(result["issue"]["launch_check"]["allowed"])
        another_owner = {"id": 4, "user": {"login": "fixture"}, "body": PLAN.github_plan_claim.marker({
            **source, "session": "other", "branch": "work/other"})}
        result, _, _ = self.show_fixture(launch=True, comments_extra=[claim, release, another_owner], resume=2)
        self.assertFalse(result["issue"]["launch_check"]["allowed"])
        for read_only in (False, True):
            result, _, _ = self.show_fixture(launch=True, read_only=read_only, pulls=[{**retained, "user": {"login": "human"}}])
            self.assertEqual(result["issue"]["launch_check"]["allowed"], read_only)
        for read_only, extra, allowed in ((False, [], False), (True, [], True), (True, [claim], False)):
            result, _, _ = self.show_fixture(launch=True, read_only=read_only, comments_extra=extra,
                enrichment={"blocked_by": [{"repo": "fixture/gate", "number": 8, "state": "open"}], "sub_issues": []})
            self.assertEqual(result["issue"]["launch_check"]["allowed"], allowed)
        for expired, allowed in ((set(), False), ({source["session"]}, True)):
            result, _, _ = self.show_fixture(launch=True, comments_extra=[claim], expired_sessions=expired)
            self.assertEqual(result["issue"]["launch_check"]["allowed"], allowed)
        release["body"] = "Released claim 2\n\nRetained PR: https://github.com/fixture/target/pull/80"
        retained["body"] += "\nCloses #6"
        for owner_comments, allowed in (([claim, release], True), ([claim, release, another_owner], False)):
            result, _, _ = self.show_fixture(launch=True, pulls=[retained], source_comments=owner_comments,
                resume="https://github.com/fixture/target/issues/6#issuecomment-2")
            self.assertEqual(result["issue"]["launch_check"]["allowed"], allowed)

    def test_launch_checks_the_claims_milestone_admission_without_writes(self) -> None:
        claim_options = ["--worker", "worker", "--session", "session", "--branch", "work/native",
                         "--next-action", "Implement the fixture scope"]
        for milestone, allowed in ((None, False), ({"title": "Catalog adoption", "state": "open"}, True),
                                   ({"title": "Catalog adoption", "state": "closed"}, False),
                                   ({"title": "Unlisted", "state": "open"}, False)):
            with self.subTest(milestone=milestone):
                result, issue, _ = self.show_fixture(launch=True, admission="milestone", milestone=milestone)
                check = result["issue"]["launch_check"]
                self.assertEqual(check["allowed"], allowed)
                if allowed:
                    admission = check["milestone_admission"]
                    self.assertEqual(admission["scope"], PLAN.github_direction_next.compact_list_issue(issue["repo"], issue))
                    parsed = PLAN.build_parser().parse_args(["claim", str(issue["number"]), *claim_options,
                                                            *admission["admission_arguments"]])
                    self.assertEqual(PLAN.claim_admission_options(parsed), ("milestone", [], None))
                else:
                    self.assertEqual(check["error"]["code"], "claim_admission_unlinked")
                    self.assertNotIn("milestone_admission", check)
        track: dict[str, Any] = {"repo": "fixture/direction", "number": 9, "html_url": "https://github.com/fixture/direction/issues/9",
                 "title": "Track: Launch", "state": "open",
                 "milestone": {"title": "Launch product", "state": "open"}}
        link = "https://github.com/fixture/direction/issues/9#issuecomment-123"
        for links, ancestry in (([link], set()), ([], {(track["repo"], track["number"])})):
            for state in ("open", "closed"):
                track["state"] = state
                result, _, _ = self.show_fixture(launch=True, admission="milestone", admission_links=links, track=track, ancestry=ancestry)
                check = result["issue"]["launch_check"]
                self.assertEqual(check["allowed"], state == "open")
                if state == "open":
                    admission = check["milestone_admission"]
                    self.assertEqual(admission["scope"], PLAN.github_direction_next.compact_list_issue(track["repo"], track))
                    parsed = PLAN.build_parser().parse_args(["claim", "7", *claim_options, *admission["admission_arguments"]])
                    self.assertEqual(parsed.admission_link, links)
        for kind in ("spare-capacity", "client-request"):
            result, _, _ = self.show_fixture(launch=True, admission=kind, admission_links=[link])
            self.assertTrue(result["issue"]["launch_check"]["allowed"])
        result, _, _ = self.show_fixture(launch=True, read_only=True, admission="milestone")
        self.assertTrue(result["issue"]["launch_check"]["allowed"])
        self.assertNotIn("milestone_admission", result["issue"]["launch_check"])

    def test_named_recipient_is_routing_work_before_an_execution_launch(self) -> None:
        for actor in ("agent:ODOO20LEAD2-1027", "agent:native-session"):
            for read_only in (False, True):
                result, _, _ = self.show_fixture(launch=True, actor=actor, read_only=read_only)
                check = result["issue"]["launch_check"]
                self.assertEqual(check["allowed"], read_only)
                self.assertTrue(any(row["source"] == "recorded_recipient" for row in check["ownership"]["recipients"]))
            result, _, _ = self.show_fixture(launch=True, actor=actor, worker=actor.removeprefix("agent:"))
            self.assertTrue(result["issue"]["launch_check"]["allowed"])

    def test_launch_uses_body_edit_time_for_the_current_assignment(self) -> None:
        result, _, _ = self.show_fixture(launch=True, actor="agent:old-worker", worker="new-worker",
            body_time="2026-10-01T00:00:00Z", comments_extra=[{
                "id": 2, "user": {"login": "fixture"}, "created_at": "2026-10-25T00:00:00Z",
                "body": "## Current Status\nNext actor: agent:new-worker"}])
        self.assertTrue(result["issue"]["launch_check"]["allowed"])

    def test_launch_returns_actionable_admission_argument_errors(self) -> None:
        for kind, links in (("own project", []), ("TBD", []), ("repeat-stop", []),
                            ("client-request", []),
                            ("milestone", ["not-a-link"])):
            with self.subTest(kind=kind, links=links):
                result, _, _ = self.show_fixture(launch=True, admission=kind, admission_links=links)
                check = result["issue"]["launch_check"]
                self.assertFalse(check["allowed"])
                self.assertTrue(check["error"]["message"])

    def test_launch_reports_the_claim_wait_refusal_even_with_valid_admission(self) -> None:
        waiting = {"id": 2, "user": {"login": "fixture"}, "body": PLAN.github_plan_wait.marker({
            "version": 1, "state": "waiting", "evidence": "Decision pending", "waits": [
                {"waiting_for": "Director", "ends_when": "Director decides"}]})}
        result, _, _ = self.show_fixture(launch=True, admission="spare-capacity", comments_extra=[waiting])
        check = result["issue"]["launch_check"]
        self.assertFalse(check["allowed"])
        self.assertEqual(check["error"]["code"], "claim_wait_unresolved")
        self.assertEqual(check["error"]["structured_wait"]["state"], "waiting")

    def test_scope_cut_reason_is_read_back_before_closing_even_if_metadata_fails(self) -> None:
        reason = "Remove an extra benchmark after reviewing its agent origin."
        events: list[str] = []
        decision = {"required": False, "reason": "agent_attested_scope_cut", "scope_cut_reason": reason}
        for fail_labels in (False, True):
            events.clear()
            def publish(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
                events.append("publish")
                return {"actor": "fixture-bot", "comment": {"id": 88}}
            def readback(*_args: Any, **_kwargs: Any) -> tuple[str, dict[str, Any]]:
                events.append("readback")
                return "fixture-bot", {"id": 88, "body": reason, "user": {"login": "fixture-bot"},
                    "issue_url": "https://api.github.com/repos/fixture/target/issues/7"}
            with patch.object(PLAN, "check_owner_decides_not_planned", return_value=decision), \
                    patch.object(PLAN, "api_json", side_effect=readback):
                code, result, state, _, _ = self.run_close(scope_issue=True, fail_labels=fail_labels, close_events=events,
                    overrides=("--reason", "not_planned", "--comment", reason),
                    trigger_comments=Mock(side_effect=publish))
            self.assertEqual(events, ["publish", "readback", "close"])
            self.assertEqual(code == 0, not fail_labels)
            state.assert_called_once()
            self.assertEqual(result["closed"]["owner_decision"]["scope_cut_reason"], reason)

    def test_scope_reason_publication_failure_leaves_the_issue_open(self) -> None:
        decision: dict[str, Any] = {"required": False, "reason": "agent_attested_scope_cut", "scope_cut_reason": "Remove extra work."}
        for failure in ("publish", "readback"):
            writer = Mock(return_value={"actor": "fixture-bot", "comment": {"id": 88}})
            if failure == "publish":
                writer.side_effect = PLAN.github_comment_core.CommentError("Publication failed", failure=PLAN.github_api_core.FailureDetail(
                    cause="transport_error", message="Publication failed", retryable=False, fallback_eligible=False,
                    disposition="stop", write_outcome="rejected"))
            with patch.object(PLAN, "check_owner_decides_not_planned", return_value=decision), \
                    patch.object(PLAN, "api_json", return_value=("fixture-bot", {"body": "Changed reason"})):
                code, _, state, _, project = self.run_close(scope_issue=True,
                    overrides=("--reason", "not_planned", "--comment", decision["scope_cut_reason"]), trigger_comments=writer)
            self.assertNotEqual(code, 0)
            state.assert_not_called()
            project.assert_not_called()

    def test_public_close_uses_resolved_target_config_and_keeps_overrides(self) -> None:
        for ref, caller in (("fixture/target#7", "fixture/local"),
                            ("https://github.com/fixture/target/issues/7", "fixture/local"),
                            ("7", "fixture/target")):
            for overrides in ((), ("--project", "Explicit", "--owner", "explicit-owner")):
                with self.subTest(ref=ref, overrides=overrides):
                    configs = {
                        repo: {"labels": {key: f"{name}:{key}" for key in
                                          ("active", "waiting", "blocked", "stale", "done")},
                               "projects": {"owner": f"{name}-owner", "default_project": f"{name}-project"}}
                        for repo, name in (("fixture/local", "local"), ("fixture/target", "target"))
                    }
                    original_labels = ["target:active", "target:waiting", "target:blocked", "target:stale", "bug"]
                    issue = {"repo": "fixture/target", "number": 7, "state": "open",
                             "html_url": "https://github.com/fixture/target/issues/7",
                             "labels": [{"name": name} for name in original_labels]}
                    config = Mock(side_effect=lambda repo, **_: configs[repo])
                    preflight = Mock(return_value=("fixture-bot", {"result": "passed"}))
                    meta = Mock(return_value=("fixture-bot", 4, {"id": "project", "title": "Project"}))
                    state = Mock(return_value={"actor": "fixture-bot"})
                    labels = Mock(return_value={"actor": "fixture-bot"})
                    field = Mock()
                    output, errors = io.StringIO(), io.StringIO()
                    with patch.multiple(PLAN,
                                        default_repo=Mock(return_value=caller), load_config=config,
                                        get_issue=Mock(return_value=("fixture-bot", issue)),
                                        collect_paged_rest_items=Mock(return_value=("fixture-bot", [
                                            {"user": {"login": "fixture"}, "body": PLAN.github_plan_wait.marker(
                                                PLAN.github_plan_wait.reviewed(evidence="Target completion reviewed"))}])),
                                        close_relationship_preflight=preflight,
                                        comment_route=Mock(return_value=("fixture-bot", "fake-gh", "fixture-bot")),
                                        project_meta=meta,
                                        project_fields=Mock(return_value={"Status": {"id": "status"}}),
                                        find_project_item=Mock(return_value={"id": "item"}),
                                        set_project_field=field), \
                            patch.multiple(PLAN.github_issue_core, set_issue_state=state, edit_issue=labels), \
                            comment_check()[0], \
                            patch.object(sys, "argv", [str(SCRIPT), "--repo", caller, "close", ref, *overrides]), \
                            contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                        PLAN.main()
                    result = json.loads(output.getvalue())
                    self.assertTrue(result["ok"], errors.getvalue())
                    self.assertEqual(config.call_args.args, ("fixture/target",))
                    self.assertEqual(preflight.call_args.args, ("fixture/target", 7, "completed"))
                    self.assertEqual(meta.call_args.args, ("explicit-owner", "Explicit") if overrides
                                     else ("target-owner", "target-project"))
                    self.assertEqual(state.call_args.kwargs["repo"], "fixture/target")
                    self.assertEqual(labels.call_args.kwargs["repo"], "fixture/target")
                    self.assertEqual(labels.call_args.kwargs["add_labels"], ["target:done"])
                    self.assertEqual(set(labels.call_args.kwargs["remove_labels"]), set(original_labels) - {"bug"})
                    self.assertEqual(field.call_args.kwargs["value"], "Done")

    def run_close(self, *, target_metadata: dict | None = None, discovered: bool = False,
                  explicit: bool = False, wrong_origin: bool = False,
                  workspace: dict | None = None, overrides: tuple[str, ...] = (),
                  fail_labels: bool = False, fail_project: bool = False,
                  blocked: bool = False, already_closed: bool = False, indexed: bool = False, trigger_comments: Mock | None = None,
                  trigger_readback: Mock | None = None,
                  comments: Any = None, scope_issue: bool = False, close_events: list[str] | None = None) -> tuple[int, dict, Mock, Mock, Mock]:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            caller, target = root / "caller", root / "elsewhere" / "target"
            caller.mkdir()
            target.mkdir(parents=True)
            if target_metadata is not None:
                metadata = target / ".github" / "github.json"
                metadata.parent.mkdir()
                metadata.write_text(json.dumps({"planning": target_metadata}))
            workspace_path = root / "workspace.json"
            if workspace is not None:
                workspace_path.write_text(json.dumps(workspace))
            issue = {"number": 7, "state": "open", "labels": [{"name": "plan:active"}],
                     "html_url": "https://github.com/fixture/target/issues/7",
                     "user": {"login": "fixture"},
                     "body": PLAN.github_plan_wait.marker(PLAN.github_plan_wait.reviewed(
                         evidence="Fixture completion and full discussion reviewed"))}
            if scope_issue:
                issue.update(user={"login": "fixture-bot"}, milestone={"title": "Catalog adoption"},
                             title="Extra benchmark", created_at="2026-10-01T00:00:00Z")
            if indexed:
                issue["labels"].append({"name": PLAN.github_plan_trigger.INDEX_LABEL})
            if already_closed:
                issue.update(state="closed", state_reason="not_planned")
            state = Mock(return_value={"actor": "fixture-bot"})
            if close_events is not None:
                close_trace = close_events
                def close_state(*_args: Any, **_kwargs: Any) -> dict[str, str]:
                    close_trace.append("close")
                    return {"actor": "fixture-bot"}
                state.side_effect = close_state
            labels = Mock(return_value={"actor": "fixture-bot"})
            if fail_labels:
                labels.side_effect = PLAN.github_issue_core.IssueError(
                    "label transport failed", failure=PLAN.github_api_core.FailureDetail(
                        cause="transport_error", message="offline fixture", retryable=False,
                        fallback_eligible=False, disposition="stop", write_outcome="rejected"))
            meta = Mock(return_value=("fixture-bot", 4, {"id": "project", "title": "Project"}))
            if fail_project:
                meta.side_effect = PLAN.PlanError("Project unavailable")
            preflight = Mock(return_value=("fixture-bot", {"result": "passed"}))
            if blocked:
                preflight.side_effect = PLAN.PlanError("Open dependency blocks closure")
            def origin(path: Path | None = None) -> str | None:
                if path == target:
                    return "fixture/wrong" if wrong_origin else "fixture/target"
                return "fixture/local" if path == caller else None
            output = io.StringIO()
            argv = [str(SCRIPT), "--repo", "fixture/local", "close", "fixture/target#7", *overrides]
            if explicit:
                argv.extend(("--planning-checkout", str(target)))
            with patch.multiple(PLAN, git_root=Mock(return_value=target if discovered else caller),
                                repo_from_git=origin, workspace_config_path=Mock(return_value=workspace_path),
                                get_issue=Mock(return_value=("fixture-bot", issue)),
                                close_relationship_preflight=preflight,
                                ensure_labels=Mock(return_value=("fixture-bot", [])),
                                collect_paged_rest_items=trigger_readback or Mock(return_value=("fixture-bot", [
                                    {"user": {"login": "fixture"}, "body": PLAN.github_plan_wait.marker(
                                        PLAN.github_plan_wait.reviewed(evidence="Target completion reviewed"))}])),
                                comment_route=Mock(return_value=("fixture-bot", "fake-gh", "fixture-bot")),
                                project_meta=meta, project_fields=Mock(return_value={"Status": {"id": "status"}}),
                                find_project_item=Mock(return_value={"id": "item"}), set_project_field=Mock()), \
                    patch.multiple(PLAN.github_issue_core, set_issue_state=state, edit_issue=labels), \
                    patch.object(PLAN.github_comment_core, "comment", trigger_comments or Mock()), \
                    (comments or comment_check()[0]), \
                    patch.object(sys, "argv", argv), contextlib.redirect_stdout(output), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = 0
                try:
                    PLAN.main()
                except SystemExit as exc:
                    assert isinstance(exc.code, int)
                    code = exc.code
            return code, json.loads(output.getvalue()), state, labels, meta

    def test_explicit_checkout_supplies_otherwise_undiscovered_target_metadata(self) -> None:
        config = {"labels": {"done": "target:finished"},
                  "projects": {"default_project": "Target", "owner": "target-owner"}}
        _, missing, _, missing_labels, missing_project = self.run_close(target_metadata=config)
        self.assertEqual(missing["planning_config"]["status"], "unavailable")
        self.assertEqual(missing_labels.call_args.kwargs["add_labels"], [PLAN.DEFAULT_CONFIG["labels"]["done"]])
        missing_project.assert_not_called()
        for explicit, discovered in ((True, False), (False, True)):
            with self.subTest(explicit=explicit):
                code, result, state, labels, project = self.run_close(
                    target_metadata=config, explicit=explicit, discovered=discovered)
                self.assertEqual(code, 0)
                self.assertTrue(result["ok"])
                self.assertEqual(result["planning_config"]["status"], "discovered")
                self.assertTrue(result["planning_config"]["path"].endswith("/.github/github.json"))
                self.assertEqual(labels.call_args.kwargs["add_labels"], [config["labels"]["done"]])
                self.assertEqual(project.call_args.args, ("target-owner", "Target"))
                state.assert_called_once()

    def test_verified_defaults_are_distinct_from_unavailable_target(self) -> None:
        for explicit, discovered in ((True, False), (False, True), (False, False)):
            with self.subTest(explicit=explicit, discovered=discovered):
                code, result, _, labels, _ = self.run_close(explicit=explicit, discovered=discovered)
                self.assertEqual(code, 0)
                self.assertEqual(result["planning_config"]["status"],
                                 "verified_defaults" if explicit or discovered else "unavailable")
                self.assertEqual(labels.call_args.kwargs["add_labels"], [PLAN.DEFAULT_CONFIG["labels"]["done"]])

    def test_unavailable_metadata_preserves_workspace_defaults_and_explicit_overrides(self) -> None:
        workspace = {"labels": {"done": "workspace:finished"},
                     "projects": {"default_project": "Workspace", "owner": "workspace-owner"}}
        for overrides in ((), ("--project", "Explicit", "--owner", "explicit-owner")):
            with self.subTest(overrides=overrides):
                code, result, _, labels, project = self.run_close(workspace=workspace, overrides=overrides)
                self.assertEqual(code, 0)
                self.assertEqual(result["planning_config"]["status"], "unavailable")
                self.assertIsNotNone(result["planning_config"]["workspace_config"])
                self.assertEqual(labels.call_args.kwargs["add_labels"], [workspace["labels"]["done"]])
                self.assertEqual(project.call_args.args, ("explicit-owner", "Explicit") if overrides
                                 else ("workspace-owner", "Workspace"))

    def test_wrong_explicit_checkout_refuses_before_mutation(self) -> None:
        code, result, state, labels, project = self.run_close(explicit=True, wrong_origin=True)
        self.assertNotEqual(code, 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["write_outcome"], "not_started")
        state.assert_not_called()
        labels.assert_not_called()
        project.assert_not_called()

    def test_unanswered_outside_comment_on_issue_or_sub_issue_refuses_before_any_write(self) -> None:
        for number in (7, 9):
            with self.subTest(number=number):
                unanswered = {"repo": "fixture/target", "number": number, "author": "client",
                              "author_class": "person", "publicly_addressed": False, "later_reply_url": None,
                              "created_at": "2026-10-17T17:33:49Z", "updated_at": "2026-10-17T17:33:49Z",
                              "comment_url": f"https://github.com/fixture/target/issues/{number}#issuecomment-1"}
                stub, radar = comment_check([unanswered], linked=[
                    {"html_url": "https://github.com/fixture/target/issues/9"}])
                code, result, state, labels, project = self.run_close(comments=stub)
                self.assertNotEqual(code, 0)
                self.assertEqual(result["failed_step"], "close_attention_preflight")
                self.assertIn(unanswered["comment_url"], result["error"])
                self.assertEqual(radar.call_args.args[0], ["fixture/target#7", "fixture/target#9"])
                state.assert_not_called()
                labels.assert_not_called()
                project.assert_not_called()

    def test_missing_reference_refusal_summary_offers_no_director_route(self) -> None:
        refusal = PLAN.github_close_attention.CloseAttentionError(
            "Cannot close fixture/target#7", cause="missing_references", payload={})
        with patch.multiple(PLAN.github_close_attention, check=Mock(side_effect=refusal)), \
                self.assertRaises(PLAN.PlanError) as refused:
            PLAN.close_attention_preflight("fixture/target", 7, {"state": "open"}, None, "gh")
        self.assertEqual(refused.exception.failure.cause, "missing_references")
        self.assertNotIn("Director", refused.exception.failure.message)

    def test_discovery_evidence_survives_partial_close_and_project_failure(self) -> None:
        code, result, state, labels, _ = self.run_close(fail_labels=True)
        self.assertNotEqual(code, 0)
        state.assert_called_once()
        self.assertEqual(result["planning_config"]["status"], "unavailable")
        self.assertTrue(result["recovery"]["issue_closed"])
        self.assertEqual(result["recovery"]["completion_metadata"], "labels_incomplete")
        code, result, state, labels, _ = self.run_close(fail_project=True, overrides=("--project", "Explicit"))
        self.assertEqual(code, 0)
        state.assert_called_once()
        self.assertTrue(result["project"]["warning"])
        self.assertEqual(result["planning_config"]["status"], "unavailable")

    def test_checkout_option_keeps_relationship_preflight(self) -> None:
        code, result, state, labels, project = self.run_close(explicit=True, blocked=True)
        self.assertNotEqual(code, 0)
        self.assertFalse(result["ok"])
        state.assert_not_called()
        labels.assert_not_called()
        project.assert_not_called()


    def test_close_persists_trigger_before_closing_and_keeps_exact_symptom(self):
        event = {"version": 1, "kind": "recurrence", "evidence": "Director deferral",
                 "symptom": "exact failure: request returned false"}
        order = []
        def published(*args, **kwargs):
            order.append("published")
            self.assertEqual(PLAN.github_plan_trigger.current({}, [{"user": {"login": "fixture"},
                             "body": args[2]}], {"fixture"}), event)
            return {"actor": "fixture-bot"}
        def readback(*args, **kwargs):
            order.append("readback")
            return "fixture", [{"user": {"login": "fixture"}, "body": PLAN.github_plan_trigger.marker(event)}]
        # The target account is fixture, so its owner-authored record is authoritative.
        code, result, state, labels, _ = self.run_close(
            overrides=("--reopen-trigger", json.dumps(event)),
            trigger_comments=Mock(side_effect=published), trigger_readback=Mock(side_effect=readback))
        self.assertEqual(code, 0, result)
        self.assertLess(order.index("published"), len(order) - 1)
        self.assertEqual(order[-1], "readback")
        state.assert_called_once()
        self.assertFalse(any(PLAN.github_plan_trigger.INDEX_LABEL in (call.kwargs.get("remove_labels") or [])
                             for call in labels.call_args_list))

    def test_failed_trigger_publication_or_readback_prevents_closure(self):
        event = {"version": 1, "kind": "date", "evidence": "Director deferral", "at": "2026-12-01"}
        for fail_publish in (True, False):
            comment = Mock(return_value={"actor": "fixture-bot"})
            if fail_publish:
                comment.side_effect = PLAN.github_comment_core.CommentError("Publication failed", failure=PLAN.github_api_core.FailureDetail(
                    cause="transport_error", message="offline fixture", retryable=False,
                    fallback_eligible=False, disposition="stop", write_outcome="rejected"))
            with self.subTest(fail_publish=fail_publish):
                code, result, state, labels, project = self.run_close(
                    overrides=("--reopen-trigger", json.dumps(event)), trigger_comments=comment)
                self.assertNotEqual(code, 0)
                self.assertFalse(result["ok"])
                state.assert_not_called()
                labels.assert_not_called()
                project.assert_not_called()

    def test_failed_discovery_index_prevents_closure(self):
        event = {"version": 1, "kind": "date", "evidence": "Director deferral", "at": "2026-12-01"}
        readback = Mock(return_value=("fixture", [{"user": {"login": "fixture"},
                       "body": PLAN.github_plan_trigger.marker(event)}]))
        code, result, state, _, _ = self.run_close(fail_labels=True,
            overrides=("--reopen-trigger", json.dumps(event)), trigger_readback=readback)
        self.assertNotEqual(code, 0)
        self.assertFalse(result["ok"])
        state.assert_not_called()

    def test_invalid_trigger_is_rejected_before_any_mutation(self):
        code, result, state, labels, project = self.run_close(overrides=("--reopen-trigger", "{}"))
        self.assertNotEqual(code, 0)
        self.assertFalse(result["ok"])
        state.assert_not_called()
        labels.assert_not_called()
        project.assert_not_called()


    def test_retire_trigger_removes_index_and_preserves_existing_closed_reason(self):
        event = {"version": 1, "state": "clear", "evidence": "Fired event reviewed; no further action"}
        comments = Mock(return_value={"actor": "fixture-bot"})
        readback = Mock(return_value=("fixture", [{"user": {"login": "fixture"},
                        "body": PLAN.github_plan_trigger.marker(event)}]))
        code, result, state, labels, _ = self.run_close(already_closed=True, indexed=True,
            overrides=("--clear-reopen-trigger", event["evidence"]), trigger_comments=comments,
            trigger_readback=readback)
        self.assertEqual(code, 0, result)
        state.assert_not_called()
        self.assertTrue(any(PLAN.github_plan_trigger.INDEX_LABEL in (call.kwargs.get("remove_labels") or [])
                            for call in labels.call_args_list))
        self.assertEqual(PLAN.github_plan_trigger.current({}, [{"user": {"login": "fixture"},
                         "body": comments.call_args.args[2]}], {"fixture"}), event)


    def test_same_clear_text_can_supersede_a_later_rearmed_event_without_an_index(self):
        clear = {"version": 1, "state": "clear", "evidence": "Reviewed; no action"}
        rearmed = {"version": 1, "kind": "date", "evidence": "New deferral", "at": "2027-01-01"}
        clear_body = "Reopen trigger: " + PLAN.github_plan_trigger.describe(clear) + "\n\n" + PLAN.github_plan_trigger.marker(clear)
        comments = [{"user": {"login": "fixture"}, "body": body} for body in
                    (clear_body, PLAN.github_plan_trigger.marker(rearmed))]
        def publish(*args, **kwargs):
            if not kwargs["dedupe_body"] or not any(c["body"] == args[2] for c in comments):
                comments.append({"user": {"login": "fixture"}, "body": args[2]})
            return {"actor": "fixture-bot"}
        code, result, state, labels, _ = self.run_close(already_closed=True,
            overrides=("--clear-reopen-trigger", clear["evidence"]),
            trigger_comments=Mock(side_effect=publish),
            trigger_readback=Mock(side_effect=lambda *a, **kw: ("fixture", list(comments))))
        self.assertEqual(code, 0, result)
        self.assertEqual(PLAN.github_plan_trigger.current({}, comments, {"fixture"}), clear)
        state.assert_not_called()
        self.assertFalse(any(PLAN.github_plan_trigger.INDEX_LABEL in (call.kwargs.get("remove_labels") or [])
                             for call in labels.call_args_list))


    def test_malformed_existing_record_returns_a_structured_failure_without_writes(self):
        bad = Mock(return_value=("fixture", [{"user": {"login": "fixture"},
                    "body": PLAN.github_plan_trigger.PREFIX + "invalid-json -->"}]))
        published = Mock()
        code, result, state, labels, _ = self.run_close(
            overrides=("--clear-reopen-trigger", "Reviewed malformed record"),
            trigger_readback=bad, trigger_comments=published)
        self.assertNotEqual(code, 0)
        self.assertFalse(result["ok"])
        published.assert_not_called()
        state.assert_not_called()
        labels.assert_not_called()


if __name__ == "__main__":
    unittest.main()
