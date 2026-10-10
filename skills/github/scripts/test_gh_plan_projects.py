#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline public Project command tests; never contact GitHub."""

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
SPEC = importlib.util.spec_from_file_location("gh_plan_projects_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PLAN: Any = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)


class ProjectTests(unittest.TestCase):
    def run_command(self, *args: str, config: dict | None = None) -> tuple[int, dict, Mock]:
        output, errors = io.StringIO(), io.StringIO()
        project_meta = Mock(side_effect=AssertionError("Unexpected Project access"))
        with patch.multiple(PLAN,
                            default_repo=Mock(return_value="fixture/repo"),
                            load_config=Mock(return_value=config or {}),
                            get_issue=Mock(return_value=("fixture-bot[bot]", {
                                "repo": "fixture/repo", "html_url": "https://github.com/fixture/repo/issues/7",
                            })),
                            project_meta=project_meta), \
                patch.object(sys, "argv", [str(SCRIPT), "project-set", "7", *args]), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = 0
            try:
                PLAN.main()
            except SystemExit as exc:
                assert isinstance(exc.code, int)
                code = exc.code
        return code, json.loads(output.getvalue()), project_meta

    def test_no_fields_needs_no_project_configuration_or_access(self) -> None:
        for args, config in (((), {}), (("--project", "4"), {}),
                             ((), {"projects": {"default_project": "4"}})):
            with self.subTest(args=args, config=config):
                code, result, project = self.run_command(*args, config=config)
                self.assertEqual(code, 0)
                self.assertTrue(result["ok"])
                self.assertEqual(result["updated"], {})
                project.assert_not_called()

    def test_requested_fields_still_require_project_configuration(self) -> None:
        for field in ("--focus", "--manager", "--finish-line"):
            with self.subTest(field=field):
                code, result, project = self.run_command(field, "Requested")
                self.assertNotEqual(code, 0)
                self.assertFalse(result["ok"])
                self.assertEqual(result["write_outcome"], "not_started")
                project.assert_not_called()

    def test_explicit_edit_keeps_field_and_target_routing(self) -> None:
        setter = Mock(return_value={"updated": {"Focus": "Next"}})
        with patch.object(PLAN, "set_project_fields", setter):
            code, result, _ = self.run_command("--project", "4", "--owner", "target", "--focus", "Next")
        self.assertEqual(code, 0)
        self.assertEqual(result["updated"], {"Focus": "Next"})
        self.assertEqual(setter.call_args.kwargs["owner"], "target")
        self.assertEqual(setter.call_args.kwargs["project_ref"], "4")
        self.assertEqual(setter.call_args.kwargs["focus"], "Next")


if __name__ == "__main__":
    unittest.main()
