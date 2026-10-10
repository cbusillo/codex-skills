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
                    config = Mock(side_effect=lambda repo: configs[repo])
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
                    config.assert_called_once_with("fixture/target")
                    self.assertEqual(preflight.call_args.args, ("fixture/target", 7, "completed"))
                    self.assertEqual(meta.call_args.args, ("explicit-owner", "Explicit") if overrides
                                     else ("target-owner", "target-project"))
                    self.assertEqual(state.call_args.kwargs["repo"], "fixture/target")
                    self.assertEqual(labels.call_args.kwargs["repo"], "fixture/target")
                    self.assertEqual(labels.call_args.kwargs["add_labels"], ["target:done"])
                    self.assertEqual(set(labels.call_args.kwargs["remove_labels"]), set(original_labels) - {"bug"})
                    self.assertEqual(field.call_args.kwargs["value"], "Done")


if __name__ == "__main__":
    unittest.main()
