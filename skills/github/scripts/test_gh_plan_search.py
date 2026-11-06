#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Behavioral tests for cross-repository planning search."""

from __future__ import annotations

import importlib.util
import sys
import unittest
import urllib.parse
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).with_name("gh-plan.py")
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("gh_plan_search_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PLAN: Any = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)


def issue(repo: str, number: int = 1) -> dict:
    return {
        "repository_url": f"https://api.github.com/repos/{repo}",
        "html_url": f"https://github.com/{repo}/issues/{number}",
        "number": number,
        "state": "open",
        "milestone": {"title": "Search milestone"},
    }


class SearchTests(unittest.TestCase):
    def search(self, query: str, results: list[dict], *options: str, current_repo: str | None = "owner/current") -> tuple[str, dict]:
        args = PLAN.build_parser().parse_args([*options, "search", query])

        def search_response(_method: str, request_path: str, **_kwargs: Any) -> tuple[str, dict]:
            qualifiers = urllib.parse.parse_qs(urllib.parse.urlsplit(request_path).query)["q"][0].split()
            items = [
                item
                for item in results
                if ("is:issue" not in qualifiers or "pull_request" not in item)
                and ("is:pr" not in qualifiers or "pull_request" in item)
            ]
            return "automation-gh", {"items": items}

        read = Mock(side_effect=search_response)
        emit = Mock()
        default_repo = Mock(return_value=current_repo)
        with patch.dict(
            PLAN.__dict__,
            {
                "default_repo": default_repo,
                "repo_from_git": lambda: current_repo,
                "api_json": read,
                "emit": emit,
            },
        ):
            PLAN.cmd_search(args)
        if current_repo is None:
            default_repo.assert_not_called()
        path = urllib.parse.urlsplit(read.call_args.args[1])
        self.assertEqual(path.path, "/search/issues")
        self.assertEqual(read.call_args.kwargs["bucket"], "search")
        return urllib.parse.parse_qs(path.query)["q"][0], emit.call_args.args[0]

    def test_search_finds_draft_pull_requests_unless_caller_selects_issues(self) -> None:
        pull = {
            **issue("owner/other", 2),
            "html_url": "https://github.com/owner/other/pull/2",
            "pull_request": {"url": "https://api.github.com/repos/owner/other/pulls/2"},
            "draft": True,
        }
        for query, expected in (("bug", ["issue", "pull_request"]), ("bug is:pr", ["pull_request"]), ("bug is:issue", ["issue"])):
            with self.subTest(query=query):
                _, output = self.search(query, [issue("owner/current"), pull])
                self.assertEqual([item["record_type"] for item in output["issues"]], expected)
                if "pull_request" in expected:
                    self.assertEqual(output["issues"][-1]["url"], pull["html_url"])
                    self.assertEqual(output["issues"][-1]["repo"], "owner/other")

    def test_scoped_queries_keep_their_scope_and_result_repositories(self) -> None:
        for query in (
            "repo:owner/other bug", "user:owner bug", "org:team bug",
            "(repo:owner/other OR repo:owner/third) bug", 'repo:"owner/other" bug',
        ):
            with self.subTest(query=query):
                q, output = self.search(query, [issue("owner/other"), issue("owner/third", 2)])
                self.assertIn(query, q)
                self.assertEqual([item["repo"] for item in output["issues"]], ["owner/other", "owner/third"])
                self.assertEqual(output["issues"][0]["milestone"], "Search milestone")

    def test_plain_and_negative_only_queries_use_the_current_repository(self) -> None:
        for query in ("bug", "-repo:owner/other bug", '"mentions repo:owner/other"'):
            with self.subTest(query=query):
                q, output = self.search(query, [issue("owner/current")])
                self.assertIn(query, q)
                self.assertIn("repo:owner/current", q.split())
                self.assertEqual(output["issues"][0]["repo"], "owner/current")

    def test_explicit_repo_is_added_without_mislabeling_other_results(self) -> None:
        q, output = self.search("repo:owner/other bug", [issue("owner/other")], "--repo", "owner/chosen")
        self.assertTrue({"repo:owner/other", "repo:owner/chosen"}.issubset(q.split()))
        self.assertEqual(output["repo"], "owner/chosen")
        self.assertEqual(output["issues"][0]["repo"], "owner/other")

    def test_repository_can_come_from_issue_url_and_unset_milestone_stays_null(self) -> None:
        result = issue("owner/other")
        del result["repository_url"]
        result["milestone"] = None
        _, output = self.search("user:owner", [result])
        self.assertEqual(output["issues"][0]["repo"], "owner/other")
        self.assertIsNone(output["issues"][0]["milestone"])

    def test_missing_repository_fails_instead_of_guessing(self) -> None:
        with self.assertRaisesRegex(PLAN.PlanError, "omitted the issue repository"):
            self.search("user:owner", [{"number": 1}])

    def test_scoped_search_works_without_a_current_checkout(self) -> None:
        q, output = self.search("repo:owner/other bug", [issue("owner/other")], current_repo=None)
        self.assertIn("repo:owner/other", q.split())
        self.assertIsNone(output["repo"])
        self.assertEqual(output["issues"][0]["repo"], "owner/other")


if __name__ == "__main__":
    unittest.main()
