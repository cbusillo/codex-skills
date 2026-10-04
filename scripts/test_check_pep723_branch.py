#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise the branch guard against real Git histories."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from check_pep723_branch import AUTOMATION_EMAIL, AUTOMATION_TRAILER, check_branch


class BranchGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.previous)
        self.addCleanup(self.temporary.cleanup)
        self.run_git("init", "-b", "main")
        self.run_git("config", "user.name", "Fixture human")
        self.run_git("config", "user.email", "human@example.test")
        self.commit_file("dependency.py", "old", "initial")
        self.run_git("switch", "-c", "automation")
        self.commit_file("dependency.py", "new", AUTOMATION_TRAILER, bot=True)

    @staticmethod
    def run_git(*args: str) -> str:
        environment = os.environ.copy()
        for key in tuple(environment):
            if key.startswith("GIT_"):
                environment.pop(key)
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        return subprocess.check_output(
            ["git", *args], text=True, stderr=subprocess.STDOUT, env=environment
        ).strip()

    def commit_file(self, name: str, content: str, message: str, *, bot: bool = False) -> None:
        Path(name).write_text(content)
        self.run_git("add", name)
        author = ["--author", f"Fixture bot <{AUTOMATION_EMAIL}>"] if bot else []
        self.run_git("commit", *author, "-m", message)

    def update_base(self, name: str = "base.txt", content: str = "base") -> None:
        self.run_git("switch", "main")
        self.commit_file(name, content, "base change")
        self.run_git("switch", "automation")

    def test_accepts_automation_and_repeated_clean_base_merges(self) -> None:
        check_branch("HEAD", "main")
        for index in range(3):
            self.update_base(content=str(index))
            self.run_git("merge", "--no-ff", "main", "-m", "Update branch")
            check_branch("HEAD", "main")

    def test_rejects_human_edit_before_or_after_base_merge(self) -> None:
        self.commit_file("human.txt", "keep me", "human edit")
        with self.assertRaises(ValueError):
            check_branch("HEAD", "main")
        self.update_base()
        self.run_git("merge", "--no-ff", "main", "-m", "Update branch")
        with self.assertRaises(ValueError):
            check_branch("HEAD", "main")
        self.commit_file("human.txt", "still keep me", "human edit after merge")
        with self.assertRaises(ValueError):
            check_branch("HEAD", "main")

    def test_rejects_changes_in_merge_commit_even_with_bot_identity(self) -> None:
        self.update_base()
        self.run_git("merge", "--no-ff", "--no-commit", "main")
        self.commit_file("human.txt", "merge edit", AUTOMATION_TRAILER, bot=True)
        with self.assertRaises(ValueError):
            check_branch("HEAD", "main")

    def test_rejects_non_base_merge(self) -> None:
        self.run_git("switch", "-c", "human")
        self.commit_file("human.txt", "keep me", "human edit")
        self.run_git("switch", "automation")
        self.run_git("merge", "--no-ff", "human", "-m", "merge human branch")
        with self.assertRaises(subprocess.CalledProcessError):
            check_branch("HEAD", "main")

    def test_rejects_conflict_resolution(self) -> None:
        self.update_base("dependency.py", "conflicting")
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_git("merge", "--no-ff", "main")
        self.commit_file("dependency.py", "manual resolution", "resolve conflict")
        with self.assertRaises(subprocess.CalledProcessError):
            check_branch("HEAD", "main")

    def test_rejects_missing_marker_or_wrong_author(self) -> None:
        self.commit_file("dependency.py", "bot without marker", "update", bot=True)
        with self.assertRaises(ValueError):
            check_branch("HEAD", "main")
        self.commit_file("dependency.py", "human with marker", AUTOMATION_TRAILER)
        with self.assertRaises(ValueError):
            check_branch("HEAD", "main")


if __name__ == "__main__":
    unittest.main()
