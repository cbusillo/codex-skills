#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline closure routing with distinct caller and target configurations."""

from __future__ import annotations

import contextlib
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
SPEC = importlib.util.spec_from_file_location("gh_plan_close_config_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PLAN: Any = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)


class CloseConfigTests(unittest.TestCase):
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
                                        close_relationship_preflight=preflight,
                                        comment_route=Mock(return_value=("fixture-bot", "fake-gh", "fixture-bot")),
                                        project_meta=meta,
                                        project_fields=Mock(return_value={"Status": {"id": "status"}}),
                                        find_project_item=Mock(return_value={"id": "item"}),
                                        set_project_field=field), \
                            patch.multiple(PLAN.github_issue_core, set_issue_state=state, edit_issue=labels), \
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
                  blocked: bool = False, trigger_comments: Mock | None = None,
                  trigger_readback: Mock | None = None) -> tuple[int, dict, Mock, Mock, Mock]:
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
                     "html_url": "https://github.com/fixture/target/issues/7"}
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
                                collect_paged_rest_items=trigger_readback or Mock(return_value=("fixture-bot", [])),
                                comment_route=Mock(return_value=("fixture-bot", "fake-gh", "fixture-bot")),
                                project_meta=meta, project_fields=Mock(return_value={"Status": {"id": "status"}}),
                                find_project_item=Mock(return_value={"id": "item"}), set_project_field=Mock()), \
                    patch.multiple(PLAN.github_issue_core, set_issue_state=state, edit_issue=labels), \
                    patch.object(PLAN.github_comment_core, "comment", trigger_comments or Mock()), \
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

    def test_discovery_evidence_survives_partial_close_and_project_failure(self) -> None:
        code, result, state, _, _ = self.run_close(fail_labels=True)
        self.assertNotEqual(code, 0)
        state.assert_called_once()
        self.assertEqual(result["planning_config"]["status"], "unavailable")
        self.assertTrue(result["recovery"]["issue_closed"])
        self.assertEqual(result["recovery"]["completion_metadata"], "labels_incomplete")
        code, result, state, _, _ = self.run_close(fail_project=True, overrides=("--project", "Explicit"))
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
        code, result, state, _, _ = self.run_close(
            overrides=("--reopen-trigger", json.dumps(event)),
            trigger_comments=Mock(side_effect=published), trigger_readback=Mock(side_effect=readback))
        self.assertEqual(code, 0, result)
        self.assertEqual(order, ["published", "readback"])
        state.assert_called_once()
        self.assertIn("reopen_trigger_readback", result["completed_steps"])

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

    def test_invalid_trigger_is_rejected_before_any_mutation(self):
        code, result, state, labels, project = self.run_close(overrides=("--reopen-trigger", "{}"))
        self.assertNotEqual(code, 0)
        self.assertFalse(result["ok"])
        state.assert_not_called()
        labels.assert_not_called()
        project.assert_not_called()


if __name__ == "__main__":
    unittest.main()
