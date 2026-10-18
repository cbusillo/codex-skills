#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""A task branch takes its base only for a conflict or a named requirement."""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent
SHA = "a" * 40
REPO = "example/tool"

spec = importlib.util.spec_from_file_location("refresh_pr", SCRIPTS / "gh-pr.py")
assert spec and spec.loader
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class UpdateBranchTests(unittest.TestCase):
    @staticmethod
    def update(mergeable_state: str | None, required_by: str | None = None) -> list[str]:
        pr = {"number": 1, "state": "open", "mergeable_state": mergeable_state, "head": {"sha": SHA}}
        writes: list[str] = []

        def read(_method: str, _path: str, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {**pr, "head": {"sha": "b" * 40}} if writes else pr

        def write(method: str, path: str, *_args: object, **_kwargs: object) -> object:
            writes.append(f"{method} {path}")
            return argparse.Namespace(actor="bot", expected_actor="bot", status=202)

        args = argparse.Namespace(repo=REPO, pr="1", wait_seconds=0, required_by=required_by)
        with patch.object(helper, "rest_json", side_effect=read), patch.object(helper, "rest_result", side_effect=write):
            helper.cmd_update_branch(args)
        return writes

    def refusal(self, mergeable_state: str | None) -> str:
        with self.assertRaises(helper.PrHelperError) as caught:
            self.update(mergeable_state)
        self.assertIsNotNone(caught.exception.failure)
        self.assertEqual(caught.exception.failure.write_outcome, "not_started")
        return caught.exception.failure.cause

    def test_behind_a_strict_base_updates(self) -> None:
        self.assertEqual(self.update("behind"), [f"PUT /repos/{REPO}/pulls/1/update-branch"])

    def test_base_without_an_up_to_date_rule_refuses_the_rerun(self) -> None:
        for state in ("clean", "unstable", "blocked"):
            with self.subTest(state=state):
                self.assertEqual(self.refusal(state), "refresh_not_required")

    def test_named_requirement_updates_a_branch_github_does_not_report_behind(self) -> None:
        self.assertEqual(len(self.update("clean", required_by="Lint needs the base's fixed workflow")), 1)

    def test_conflict_routes_to_the_local_merge(self) -> None:
        self.assertEqual(self.refusal("dirty"), "refresh_conflicting_branch")

    def test_uncomputed_mergeability_waits(self) -> None:
        for state in (None, "unknown"):
            with self.subTest(state=state):
                self.assertEqual(self.refusal(state), "mergeability_unknown")


class LocalMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.git = shutil.which("git")
        self.assertIsNotNone(self.git)
        self.env = {
            "PATH": os.environ["PATH"], "HOME": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "CODEX_SKILLS_ENV_FILE": str(self.root / "missing.env"),
            "GIT_COMMIT_AS_BOT_NAME": "fixture-bot", "GIT_COMMIT_AS_BOT_EMAIL": "bot@example.invalid",
            "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        origin = self.root / "origin.git"
        self.run_git(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        seed = self.root / "seed"
        self.run_git(self.root, "clone", "-q", str(origin), str(seed))
        self.write(seed, "shared.txt", "base\n")
        self.run_git(seed, "add", ".")
        self.run_git(seed, "commit", "-q", "-m", "base")
        self.run_git(seed, "push", "-q", "origin", "HEAD:main")
        self.seed = seed
        self.repo = self.root / "repo"
        self.run_git(self.root, "clone", "-q", str(origin), str(self.repo))
        self.run_git(self.repo, "switch", "-q", "-c", "work/task")
        self.write(self.repo, "task.txt", "task\n")
        self.run_git(self.repo, "add", ".")
        self.run_git(self.repo, "commit", "-q", "-m", "task")

    def run_git(self, cwd: Path, *args: str) -> str:
        return subprocess.run([str(self.git), *args], cwd=cwd, env=self.env, text=True,
                              capture_output=True, check=True).stdout.strip()

    @staticmethod
    def write(repo: Path, name: str, text: str) -> None:
        (repo / name).write_text(text)

    def advance_main(self, name: str, text: str) -> None:
        self.write(self.seed, name, text)
        self.run_git(self.seed, "add", ".")
        self.run_git(self.seed, "commit", "-q", "-m", "main moves")
        self.run_git(self.seed, "push", "-q", "origin", "HEAD:main")
        self.run_git(self.repo, "fetch", "-q", "origin")

    def bot(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(SCRIPTS / "git-commit-as-bot"), *args], cwd=self.repo,
                              env=self.env, text=True, capture_output=True)

    def head_parents(self) -> int:
        return len(self.run_git(self.repo, "show", "-s", "--format=%P").split())

    def test_conflict_free_base_merge_is_refused(self) -> None:
        self.advance_main("other.txt", "main\n")
        result = self.bot("--git-command", "merge", "--no-edit", "origin/main")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("--refresh-required", result.stderr)
        self.assertEqual(self.head_parents(), 1)

    def test_conflicting_base_merge_proceeds_to_resolution(self) -> None:
        self.write(self.repo, "shared.txt", "task side\n")
        self.run_git(self.repo, "commit", "-q", "-am", "task edits shared")
        self.advance_main("shared.txt", "main side\n")
        result = self.bot("--git-command", "merge", "--no-edit", "origin/main")
        self.assertNotIn("merge train", result.stderr)
        self.assertTrue((self.repo / ".git" / "MERGE_HEAD").exists(), result.stderr)
        self.assertEqual(self.bot("--git-command", "merge", "--abort").returncode, 0)

    def test_named_requirement_merges_the_base(self) -> None:
        self.advance_main("other.txt", "main\n")
        result = self.bot("--git-command", "merge", "--refresh-required", "strict base rule", "--no-edit", "origin/main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.head_parents(), 2)
        self.assertEqual(self.run_git(self.repo, "show", "-s", "--format=%an"), "fixture-bot")

    def test_stack_parent_merge_is_not_a_base_refresh(self) -> None:
        self.run_git(self.repo, "switch", "-q", "-c", "work/parent", "origin/main")
        self.write(self.repo, "parent.txt", "parent\n")
        self.run_git(self.repo, "add", ".")
        self.run_git(self.repo, "commit", "-q", "-m", "parent")
        self.run_git(self.repo, "switch", "-q", "work/task")
        result = self.bot("--git-command", "merge", "--no-edit", "work/parent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.head_parents(), 2)

    def test_pulling_the_base_routes_to_the_checked_merge(self) -> None:
        self.advance_main("other.txt", "main\n")
        result = self.bot("--git-command", "pull", "--no-rebase", "--no-edit", "origin", "main")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("--git-command merge origin/main", result.stderr)
        self.assertEqual(self.head_parents(), 1)

    def test_implicit_upstream_base_is_checked(self) -> None:
        # A task branch started from origin/main tracks it until its first push.
        self.run_git(self.repo, "branch", "-q", "--set-upstream-to", "origin/main")
        self.advance_main("other.txt", "main\n")
        for args in (("merge", "--no-edit"), ("pull", "--no-rebase", "--no-edit"), ("rebase", "--no-autostash")):
            with self.subTest(args=args):
                result = self.bot("--git-command", *args)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("--refresh-required", result.stderr)
        self.assertEqual(self.head_parents(), 1)

    def test_missing_origin_head_still_finds_the_base(self) -> None:
        self.advance_main("other.txt", "main\n")
        # Newer Git recreates origin/HEAD on fetch, so remove it afterwards.
        self.run_git(self.repo, "remote", "set-head", "origin", "--delete")
        result = self.bot("--git-command", "merge", "--no-edit", "origin/main")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.head_parents(), 1)

    def test_conflict_free_rebase_onto_the_base_is_refused(self) -> None:
        self.advance_main("other.txt", "main\n")
        before = self.run_git(self.repo, "rev-parse", "HEAD")
        for args in (("origin/main",), ("--onto", "origin/main", "HEAD~1")):
            with self.subTest(args=args):
                result = self.bot("--git-command", "rebase", *args)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(self.run_git(self.repo, "rev-parse", "HEAD"), before)
        result = self.bot("--git-command", "rebase", "--refresh-required", "strict base rule", "origin/main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.run_git(self.repo, "rev-parse", "HEAD~1"), self.run_git(self.repo, "rev-parse", "origin/main"))

    def test_rebase_merge_backend_flag_takes_no_value(self) -> None:
        self.run_git(self.repo, "push", "-q", "-u", "origin", "work/task")
        self.advance_main("other.txt", "main\n")
        result = self.bot("--git-command", "rebase", "-m", "origin/main")
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_keep_base_rebase_is_not_a_base_refresh(self) -> None:
        self.advance_main("other.txt", "main\n")
        result = self.bot("--git-command", "rebase", "--keep-base", "origin/main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.run_git(self.repo, "rev-parse", "HEAD~1"), self.run_git(self.repo, "rev-parse", "origin/main"))

    def test_rebase_checks_the_named_branch_rather_than_head(self) -> None:
        self.advance_main("other.txt", "main\n")
        self.run_git(self.repo, "branch", "-q", "work/current", "origin/main")
        self.run_git(self.repo, "switch", "-q", "work/current")
        result = self.bot("--git-command", "rebase", "origin/main", "work/task")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.run_git(self.repo, "switch", "-q", "work/task")
        self.run_git(self.repo, "merge", "-q", "--no-edit", "origin/main")
        self.run_git(self.repo, "switch", "-q", "--detach", "HEAD~1")
        result = self.bot("--git-command", "rebase", "origin/main", "work/task")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rebasing_own_commits_is_not_a_base_refresh(self) -> None:
        self.write(self.repo, "task.txt", "task two\n")
        self.run_git(self.repo, "commit", "-q", "-am", "task two")
        self.advance_main("other.txt", "main\n")
        result = self.bot("--git-command", "rebase", "HEAD~1")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
