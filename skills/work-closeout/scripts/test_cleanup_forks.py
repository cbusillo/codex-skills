#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Synthetic fork-disposition tests. No GitHub account or real repository is a fixture."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

import cleanup_forks


NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)
OLD = "2025-01-01T00:00:00Z"
RECENT = "2026-09-20T00:00:00Z"


class FakeGitHub:
    """Answers the read calls the helper makes; anything else fails the test."""

    def __init__(self, *, pushed=OLD, pulls=None, branches=None, missing=None, search=(), incomplete=False,
                 compare_error=None, parent=True):
        self.fork = {"nameWithOwner": "me/tool", "parent": {"owner": {"login": "up"}, "name": "tool"} if parent else None,
                     "pushedAt": pushed, "isArchived": False}
        self.pulls = pulls or {}
        self.branches = branches or {"main": "sha-main"}
        self.missing = missing or {}
        self.search = {"incomplete_results": incomplete, "repos": list(search)}
        self.compare_error = compare_error or {}

    def __call__(self, args):
        if args[:2] == ["repo", "list"]:
            return [self.fork]
        if args[0] != "api" or ("-X" in args and args[args.index("-X") + 1] != "GET"):
            raise AssertionError(f"unexpected non-read call {args}")
        path = next(arg for arg in args[1:] if arg.startswith(("repos/", "search/")))
        if path == "repos/up/tool":
            return {"default_branch": "main"}
        if path.startswith("repos/me/tool/branches"):
            return [[{"name": name, "commit": {"sha": sha}} for name, sha in self.branches.items()]]
        if path == "repos/up/tool/pulls":
            head = next(arg for arg in args if arg.startswith("head="))
            return [self.pulls.get(head.removeprefix("head=me:"), [])]
        if "/compare/" in path:
            sha = path.split("me:", 1)[1]
            branch = next(name for name, tip in self.branches.items() if tip == sha)
            if branch in self.compare_error:
                raise cleanup_forks.GhError(self.compare_error[branch])
            return {"ahead_by": self.missing.get(branch, 0), "status": "behind"}
        if path == "search/code":
            return self.search
        raise AssertionError(f"unexpected call {args}")


def pull(number, state, head="sha-other", created=OLD):
    return {"number": number, "state": "closed" if state == "merged" else state,
            "merged_at": OLD if state == "merged" else None, "head": {"sha": head},
            "created_at": created, "html_url": f"https://example.invalid/pull/{number}"}


def disposition(github):
    report = cleanup_forks.dispositions("me", run=github, now=NOW)
    return report, report["forks"][0]["disposition"]


class ForkDispositions(unittest.TestCase):
    def test_merged_pr_only_fork_is_a_delete_candidate(self):
        # The PR branch is still "ahead" after a squash merge; its tip matching the merged head proves delivery.
        github = FakeGitHub(pulls={"fix": [pull(7, "merged", head="sha-fix")]},
                            branches={"main": "sha-main", "fix": "sha-fix"}, missing={"fix": 2})
        report, result = disposition(github)
        self.assertEqual(result, "delete")
        self.assertTrue(report["complete"])
        self.assertFalse(report["dependents_proven_absent"])

    def test_unsent_commits_on_any_branch_archive_the_fork(self):
        github = FakeGitHub(pulls={"local": [pull(7, "closed", head="sha-local")]},
                            branches={"main": "sha-main", "local": "sha-local"}, missing={"local": 3})
        self.assertEqual(disposition(github)[1], "archive")

    def test_branch_names_never_reach_the_compare_path(self):
        github = FakeGitHub(branches={"main": "sha-main", "main#857": "sha-odd"}, missing={"main#857": 1})
        self.assertEqual(disposition(github)[1], "archive")

    def test_orphan_branch_counts_as_unsent(self):
        github = FakeGitHub(branches={"main": "sha-main", "gh-pages": "sha-pages"},
                            compare_error={"gh-pages": "No common ancestor between main and me:gh-pages."})
        report, result = disposition(github)
        self.assertEqual(result, "archive")
        self.assertTrue(report["complete"])

    def test_open_pr_keeps_the_fork_whoever_opened_it(self):
        # The lookup is by source branch, so a PR a bot or collaborator opened from the fork still counts.
        github = FakeGitHub(pulls={"main": [pull(9, "open")]})
        self.assertEqual(disposition(github)[1], "keep")

    def test_ongoing_contribution_keeps_the_fork(self):
        self.assertEqual(disposition(FakeGitHub(pushed=RECENT))[1], "keep")
        repeated = {"main": [pull(1, "merged", created=RECENT), pull(2, "merged", created=RECENT)]}
        self.assertEqual(disposition(FakeGitHub(pulls=repeated))[1], "keep")

    def test_a_referencing_repository_keeps_the_fork(self):
        github = FakeGitHub(search=["me/consumer"])
        self.assertEqual(disposition(github)[1], "keep")

    def test_incomplete_search_or_missing_upstream_is_not_evidence(self):
        for github in (FakeGitHub(incomplete=True), FakeGitHub(parent=False)):
            report, result = disposition(github)
            self.assertEqual(result, "needs-review")
            self.assertFalse(report["complete"])

    def test_unreadable_branch_needs_review_and_marks_evidence_incomplete(self):
        github = FakeGitHub(branches={"main": "sha-main", "odd": "sha-odd"}, compare_error={"odd": "HTTP 502"})
        report, result = disposition(github)
        self.assertEqual(result, "needs-review")
        self.assertFalse(report["complete"])


if __name__ == "__main__":
    unittest.main()
