#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline section update tests for literal text and pre-write failures."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).with_name("gh-plan.py")
sys.path.insert(0, str(SCRIPT.parent))
import github_identity

SPEC = importlib.util.spec_from_file_location("gh_plan_sections_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PLAN: Any = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)

LITERAL_TEXTS = (
    r"Unicode example: \u0027 and \U00000027",
    r"Regex example: \1, \9, and \g<name>",
    r"Paths: C:\new\test and \\server\share",
    "```python\npattern = r\"\\d+\\s+\"\n```\nTrailing backslash: \\",
)


class SectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.issue = {
            "repo": "owner/repo", "number": 42, "title": "Fixture", "state": "open",
            "user": {"login": "fixture-bot[bot]"}, "labels": [],
            "body": "## Objective\n\nKeep this.\n\n## Finish Line\n\nOld text.\n\n## Scope\n\nKeep that.\n",
        }

    def run_update(self, *body_args: str, stdin: str = "", **overrides: Any) -> tuple[int, dict, Mock, str]:
        output, errors = io.StringIO(), io.StringIO()
        edit = Mock(side_effect=lambda _repo, _number, *, body: ("fixture-bot[bot]", {**self.issue, "body": body}))
        replacements = {"default_repo": Mock(return_value="owner/repo"),
                        "get_issue": Mock(return_value=("fixture-bot[bot]", self.issue)),
                        "rest_edit_issue": edit, "EXPECTED_ACTOR": "fixture-bot[bot]", **overrides}
        with patch.multiple(PLAN, **replacements), \
                patch.object(github_identity, "configured_bot_logins", return_value=["fixture-bot[bot]"]), \
                patch.object(sys, "argv", [str(SCRIPT), "update-section", "42", "Finish Line", *body_args]), \
                patch.object(sys, "stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = 0
            try:
                PLAN.main()
            except SystemExit as exc:
                code = int(exc.code)
        return code, json.loads(output.getvalue()), edit, errors.getvalue()

    def test_literal_text_survives_body_file_and_stdin_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            body_file = Path(directory) / "body.md"
            for content in LITERAL_TEXTS:
                body_file.write_text(content, encoding="utf-8")
                for args, stdin in ((("--body", content), ""),
                                    (("--body-file", str(body_file)), ""),
                                    (("--body-file", "-"), content)):
                    with self.subTest(content=content, args=args[:1]):
                        code, result, edit, errors = self.run_update(*args, stdin=stdin)
                        self.assertEqual(code, 0, errors)
                        self.assertTrue(result["ok"])
                        edit.assert_called_once()
                        self.assertEqual(edit.call_args.kwargs["body"], self.issue["body"].replace("Old text.", content))

    def test_contributor_request_survives_literal_section_updates(self) -> None:
        self.issue["user"] = {"login": "outside-contributor"}
        self.issue["body"] = "Original request with `\\u0027`.\n"
        for content in LITERAL_TEXTS:
            with self.subTest(content=content):
                # First append the managed section, then replace it in the envelope.
                for _ in range(2):
                    code, _, edit, errors = self.run_update("--body", content)
                    self.assertEqual(code, 0, errors)
                    self.issue["body"] = edit.call_args.kwargs["body"]
                    original = PLAN.marked_block(self.issue["body"], PLAN.PLAN_ORIGINAL_START, PLAN.PLAN_ORIGINAL_END)
                    self.assertIsNotNone(original)
                    self.assertEqual(original[2], "Original request with `\\u0027`.\n")
                    managed = PLAN.contributor_plan_body(self.issue)
                    self.assertEqual(managed, f"## Finish Line\n\n{content}")

    def assert_prewrite_failure(self, args: tuple[str, ...], step: str | None = None, **overrides: Any) -> None:
        code, result, edit, errors = self.run_update(*args, **overrides)
        self.assertNotEqual(code, 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["operation"], "github.plan.update_section")
        self.assertEqual(result["write_outcome"], "not_started")
        self.assertFalse(result["retryable"])
        self.assertFalse(result["fallback_eligible"])
        self.assertNotIn("Traceback", errors)
        edit.assert_not_called()
        if step:
            self.assertEqual(result["failed_step"], step)
            self.assertEqual(result["error_code"], "validation_error")

    def test_unreadable_body_file_fails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "invalid.md"
            invalid.write_bytes(b"\xff")
            for path in (Path(directory) / "missing.md", Path(directory), invalid):
                with self.subTest(path=path):
                    self.assert_prewrite_failure(("--body-file", str(path)), "read_body")

    def test_section_preparation_failure_is_structured(self) -> None:
        self.assert_prewrite_failure(("--body", "Valid content"), "section_replacement",
                                     replace_issue_plan_section=Mock(side_effect=re.error("Invalid section pattern")))

    def test_reserved_markers_still_fail_before_mutation(self) -> None:
        self.assert_prewrite_failure(("--body", PLAN.PLAN_MANAGED_START))

    def test_ambiguous_contributor_body_still_fails_before_mutation(self) -> None:
        self.issue["user"] = {"login": "outside-contributor"}
        self.assert_prewrite_failure(("--body", "New content"))

    def test_write_failures_keep_their_transport_classification(self) -> None:
        failure = PLAN.github_api_core.FailureDetail(cause="transport_error", message="Write outcome unknown",
                                                   retryable=False, fallback_eligible=False, disposition="stop",
                                                   write_outcome="unknown", failed_step="edit_issue")
        code, result, _, _ = self.run_update("--body", "Valid content",
                                            rest_edit_issue=Mock(side_effect=PLAN.PlanError("Write outcome unknown", failure=failure)))
        self.assertNotEqual(code, 0)
        self.assertEqual(result["failure"]["cause"], "transport_error")
        self.assertEqual(result["write_outcome"], "unknown")
        self.assertEqual(result["failed_step"], "edit_issue")


if __name__ == "__main__":
    unittest.main()
