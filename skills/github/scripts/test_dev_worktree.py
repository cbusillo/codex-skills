#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise creation and host routing using only temporary repositories and config."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("dev_worktree.py")


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.repo = self.base / "source repo"
        self.repo.mkdir()
        # The main directory name is also the repo slug; spaces are supported in ancestors.
        self.repo = self.repo / "sample"
        self.repo.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "DEV_WORKTREE_"))}
        self.env.update(HOME=str(self.home), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        self.git("init", "-b", "main")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "Initial")
        self.initial = self.git("rev-parse", "HEAD")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "Later")
        self.source_head = self.git("rev-parse", "HEAD")

    def git(self, *args, repo=None):
        return subprocess.run(["git", "-C", str(repo or self.repo), *args], env=self.env, capture_output=True, text=True, check=True).stdout.strip()

    def run_helper(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], env=self.env, capture_output=True, text=True)

    def settings(self, value):
        path = self.home / ".config/codex-skills/worktrees.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def assert_refused(self, result, task="task"):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn(f"refs/heads/work/{task}", self.git("for-each-ref", "--format=%(refname)").splitlines())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.source_head)

    def test_default_creates_branch_at_explicit_start_outside_source(self):
        result = self.run_helper(self.repo, "task", self.initial)
        self.assertEqual(result.returncode, 0, result.stderr)
        destination = Path(result.stdout.strip())
        self.assertTrue(destination.is_dir())
        self.assertTrue(destination.is_relative_to(self.home))
        self.assertFalse(destination.is_relative_to(self.repo))
        self.assertEqual(self.git("rev-parse", "HEAD", repo=destination), self.initial)
        self.assertEqual(self.git("branch", "--show-current", repo=destination), "work/task")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.source_head)

    def test_configured_root_and_linked_source_keep_main_repo_name(self):
        root = self.base / "work tree root"
        self.settings({"root": str(root)})
        first = self.run_helper(self.repo, "one", "HEAD")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_helper(first.stdout.strip(), "two", self.initial)
        self.assertEqual(second.returncode, 0, second.stderr)
        destination = Path(second.stdout.strip())
        self.assertEqual(destination, root / self.repo.name / "two")
        self.assertEqual(self.git("rev-parse", "HEAD", repo=destination), self.initial)

    def test_explicit_config_override_and_tilde_root(self):
        config = self.base / "host.json"
        config.write_text(json.dumps({"root": "~/tasks"}))
        self.env["DEV_WORKTREE_CONFIG"] = str(config)
        result = self.run_helper(self.repo, "task", "HEAD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), self.home / "tasks" / self.repo.name / "task")

    def test_existing_destination_file_directory_and_dangling_link_are_preserved(self):
        root = self.base / "tasks"
        self.settings({"root": str(root)})
        parent = root / self.repo.name
        parent.mkdir(parents=True)
        for task in ("file", "directory", "link"):
            with self.subTest(task=task):
                target = parent / task
                if task == "file":
                    target.write_text("preserve")
                elif task == "directory":
                    target.mkdir()
                else:
                    target.symlink_to(self.base / "absent")
                self.assert_refused(self.run_helper(self.repo, task, "HEAD"), task)
                self.assertTrue(os.path.lexists(target))
        self.assertEqual((parent / "file").read_text(), "preserve")

    def test_existing_branch_is_preserved(self):
        self.git("branch", "work/task", self.initial)
        result = self.run_helper(self.repo, "task", "HEAD")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("rev-parse", "work/task"), self.initial)

    def test_traversal_and_option_like_slugs_and_starts_refuse(self):
        for task, start in (("../escape", "HEAD"), ("nested/task", "HEAD"), ("-bad", "HEAD"), ("task", "--help"), ("task", "missing")):
            with self.subTest(task=task, start=start):
                self.assert_refused(self.run_helper(self.repo, task, start), task)

    def test_root_inside_source_or_another_repository_refuses(self):
        other = self.base / "other"
        other.mkdir()
        self.git("init", repo=other)
        for root in (self.repo / "nested", other / "nested"):
            with self.subTest(root=root):
                self.settings({"root": str(root)})
                self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))
                self.assertFalse(root.exists())

    def test_symlink_into_source_and_symlink_parent_refuse(self):
        alias = self.base / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        self.settings({"root": str(alias / "tasks")})
        self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))
        root = self.base / "tasks"
        root.mkdir()
        (root / self.repo.name).symlink_to(self.home, target_is_directory=True)
        self.settings({"root": str(root)})
        self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))
        self.assertFalse((self.home / "task").exists())

    def test_missing_explicit_and_malformed_config_do_not_fall_back(self):
        self.env["DEV_WORKTREE_CONFIG"] = str(self.base / "missing.json")
        self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))
        del self.env["DEV_WORKTREE_CONFIG"]
        for value in ([], {"root": None}, {"root": "relative"}, {"rot": "typo"}, {"hostHelper": "/missing"}):
            with self.subTest(value=value):
                self.settings(value)
                self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))
        config = self.settings({})
        config.write_text("invalid json")
        self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))
        self.assertFalse((self.home / "Developer").exists())

    def test_host_helper_receives_literal_creation_and_retirement_args(self):
        host = self.base / "host helper"
        receipt = self.base / "args.json"
        host.write_text(f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\nPath({str(receipt)!r}).write_text(json.dumps(sys.argv[1:]))\n")
        host.chmod(0o700)
        root = self.base / "unmounted/tasks"
        config = self.settings({"root": str(root), "hostHelper": str(host)})
        before = config.read_bytes()
        for args in ((str(self.repo), "task", "HEAD~1"), ("retire", "--dry-run", str(root / "some task"))):
            with self.subTest(args=args):
                result = self.run_helper(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(receipt.read_text()), list(args))
                self.assertFalse(root.parent.exists())
        self.assertEqual(config.read_bytes(), before)

    def test_host_failure_or_missing_helper_never_uses_portable_fallback(self):
        host = self.base / "host"
        root = self.base / "unmounted/tasks"
        self.settings({"root": str(root), "hostHelper": str(host)})
        self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))
        host.write_text("#!/bin/sh\nexit 7\n")
        host.chmod(0o700)
        result = self.run_helper(self.repo, "task", "HEAD")
        self.assertEqual(result.returncode, 7)
        self.assertFalse(root.parent.exists())

    def test_self_delegation_refuses(self):
        self.settings({"root": str(self.base / "tasks"), "hostHelper": str(SCRIPT)})
        self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))


if __name__ == "__main__":
    unittest.main()
