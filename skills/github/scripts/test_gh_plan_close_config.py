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
                     track: dict | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        target = "fixture/target"
        configs = {repo: copy.deepcopy(PLAN.DEFAULT_CONFIG) for repo in ("fixture/local", target)}
        configs[target]["labels"] = {key: f"target:{key}" for key in configs[target]["labels"]}
        names = configs[target]["labels"]
        issue: dict[str, Any] = {"repo": target, "number": 7, "state": "open", "title": "Target request",
                 "html_url": f"https://github.com/{target}/issues/7", "user": {"login": "fixture"},
                 "body": f"Original human request.\n## Current Status\nState: Blocked.\nNext action: {action}",
                 "labels": [{"name": names[key]} for key in ("plan", "blocked")]}
        issue["milestone"] = milestone
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
                milestone_claim_ancestry=Mock(return_value=Mock(nodes=set(), complete=True)),
                collect_paged_rest_items=Mock(return_value=("fixture-bot", comments)),
                read_issue_relationships=relationships, wait_authors=Mock(return_value={"fixture"}),
                claim_snapshot=Mock(return_value=(parent, "", source_comments or [], False)),
                next_source_delivery=Mock(side_effect=AssertionError("Launch admission must not rank source delivery"))), \
                patch.object(github_plan_claim, "expired_lease_sessions", return_value=expired_sessions or set()), \
                patch.object(github_plan_claim, "local_inventory", return_value={
                    "local_branches": [], "remote_branches": [], "worktrees": [], "sessions": []}), \
                patch.object(github_work_graph, "ownership_inventory", return_value={
                    "complete": True, "pulls": pulls or [], "coverage": {}, "inventory": {}}), contextlib.redirect_stdout(output):
            PLAN.cmd_show(PLAN.build_parser().parse_args(["--repo", "fixture/local", "show", "fixture/target#7", "--full",
                                                        *(["--launch-check"] if launch else []),
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
        for milestone, allowed in ((None, False), ({"title": "Catalog adoption", "state": "open"}, True),
                                   ({"title": "Catalog adoption", "state": "closed"}, False),
                                   ({"title": "Unlisted", "state": "open"}, False)):
            with self.subTest(milestone=milestone):
                result, _, _ = self.show_fixture(launch=True, admission="milestone", milestone=milestone)
                check = result["issue"]["launch_check"]
                self.assertEqual(check["allowed"], allowed)
                if not allowed:
                    self.assertEqual(check["error"]["code"], "claim_admission_unlinked")
        track = {"title": "Track: Launch", "state": "open",
                 "milestone": {"title": "Launch product", "state": "open"}}
        link = "https://github.com/fixture/direction/issues/9#issuecomment-123"
        for state in ("open", "closed"):
            track["state"] = state
            result, _, _ = self.show_fixture(launch=True, admission="milestone", admission_links=[link], track=track)
            self.assertEqual(result["issue"]["launch_check"]["allowed"], state == "open")
        result, _, _ = self.show_fixture(launch=True, admission="spare-capacity", admission_links=[link])
        self.assertTrue(result["issue"]["launch_check"]["allowed"])
        result, _, _ = self.show_fixture(launch=True, read_only=True, admission="milestone")
        self.assertTrue(result["issue"]["launch_check"]["allowed"])

    def test_launch_reports_the_claim_wait_refusal_even_with_valid_admission(self) -> None:
        waiting = {"id": 2, "user": {"login": "fixture"}, "body": PLAN.github_plan_wait.marker({
            "version": 1, "state": "waiting", "evidence": "Decision pending", "waits": [
                {"waiting_for": "Director", "ends_when": "Director decides"}]})}
        result, _, _ = self.show_fixture(launch=True, admission="spare-capacity", comments_extra=[waiting])
        check = result["issue"]["launch_check"]
        self.assertFalse(check["allowed"])
        self.assertEqual(check["error"]["code"], "claim_wait_unresolved")
        self.assertEqual(check["error"]["structured_wait"]["state"], "waiting")

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
                  comments: Any = None) -> tuple[int, dict, Mock, Mock, Mock]:
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
            if indexed:
                issue["labels"].append({"name": PLAN.github_plan_trigger.INDEX_LABEL})
            if already_closed:
                issue.update(state="closed", state_reason="not_planned")
            state = Mock(return_value={"actor": "fixture-bot"})
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
