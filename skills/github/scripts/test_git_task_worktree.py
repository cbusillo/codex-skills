# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
INTEGRATION_COMMANDS = ("commit", "merge", "pull", "rebase", "cherry-pick", "revert", "am")


class TaskWorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.primary = self.root / "primary"
        self.task = self.root / "my task"
        self.peer = self.root / "peer"
        self.git = shutil.which("git")
        self.assertIsNotNone(self.git)
        self.log = self.root / "writes.jsonl"
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "CODEX_SKILLS_ENV_FILE": str(self.root / "missing.env"),
            "CODEX_AUTOMATION_LOGIN": "fixture-bot",
            "CODEX_AUTOMATION_EMAIL": "bot@example.invalid",
            "CODEX_GITHUB_TOKEN": "fixture-token",
            "WRITE_LOG": str(self.log),
        }
        self.run_git(self.root, "init", "-q", "-b", "main", str(self.primary))
        self.run_git(
            self.primary,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "seed",
        )
        self.run_git(self.primary, "remote", "add", "origin", "https://github.com/example/project.git")
        self.run_git(self.primary, "worktree", "add", "-qb", "work/mine", str(self.task))
        self.run_git(self.primary, "worktree", "add", "-qb", "work/peer", str(self.peer))
        proxy = self.root / "git-proxy"
        proxy.write_text(
            f"#!{sys.executable}\n"
            "import json, os, subprocess, sys\n"
            "from pathlib import Path\n"
            f"commands = {INTEGRATION_COMMANDS!r}\n"
            "args = sys.argv[1:]\n"
            "if args[0] in commands or 'push' in args:\n"
            "    with Path(os.environ['WRITE_LOG']).open('a') as stream:\n"
            "        stream.write(json.dumps(args) + '\\n')\n"
            "else:\n"
            f"    sys.exit(subprocess.call([{self.git!r}, *args]))\n"
        )
        proxy.chmod(0o700)
        gh = self.root / "gh-proxy"
        gh.write_text("#!/bin/sh\nprintf '%s\\n' fixture-bot\n")
        gh.chmod(0o700)
        self.env.update(GIT_COMMIT_AS_BOT_GIT=str(proxy), GIT_PUSH_AS_BOT_GIT=str(proxy), GIT_PUSH_AS_BOT_GH=str(gh))

    def run_git(self, directory: Path, *args: str) -> str:
        return subprocess.run(
            [str(self.git), "-C", str(directory), *args], env=self.env, check=True, capture_output=True, text=True
        ).stdout.strip()

    def invoke(
        self, directory: Path, helper: str, *args: str, expected: Path | None = None, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        prefix = ["--worktree", str(expected)] if expected is not None else []
        return subprocess.run(
            [str(SCRIPTS / helper), *prefix, *args],
            cwd=directory,
            env=self.env | (environment or {}),
            capture_output=True,
            text=True,
        )

    def assert_refuses(self, directory: Path, expected: Path, *, environment: dict[str, str] | None = None) -> None:
        before = self.run_git(self.primary, "config", "--local", "--list")
        private_config = self.root / "refused.env"
        private_config.write_text('printf "loaded\\n" >> "$WRITE_LOG"\n')
        environment = {"CODEX_SKILLS_ENV_FILE": str(private_config)} | (environment or {})
        for helper, arguments in (
            ("git-commit-as-bot", ("--allow-empty", "-m", "unexpected")),
            ("git-push-as-bot", ("origin", "HEAD")),
        ):
            with self.subTest(helper=helper, directory=directory, expected=expected):
                result = self.invoke(directory, helper, *arguments, expected=expected, environment=environment)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn(str(expected), result.stderr)
                self.assertFalse(self.log.exists())
                self.assertEqual(self.run_git(self.primary, "config", "--local", "--list"), before)

    def test_failed_cd_and_another_workers_worktree_refuse_before_mutation(self) -> None:
        for directory in (self.primary, self.peer, self.root):
            self.assert_refuses(directory, self.task)

    def test_primary_and_linked_runtime_default_branch_cannot_be_declared_as_task(self) -> None:
        self.assert_refuses(self.primary, self.primary)
        self.run_git(self.primary, "switch", "-qc", "work/primary")
        self.assert_refuses(self.primary, self.primary)
        runtime = self.root / "runtime"
        self.run_git(self.primary, "worktree", "add", "-q", str(runtime), "main")
        self.assert_refuses(runtime, runtime)

    def test_missing_relative_nested_and_unavailable_expected_paths_refuse(self) -> None:
        for helper, arguments in (("git-commit-as-bot", ("-m", "unexpected")), ("git-push-as-bot", ("origin", "HEAD"))):
            result = self.invoke(self.task, helper, *arguments)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--worktree", result.stderr)
        self.assert_refuses(self.task, Path("../my task"))
        self.assert_refuses(self.task, self.root / "missing")
        nested = self.task / "nested"
        nested.mkdir()
        self.assert_refuses(nested, nested)

    def test_git_redirection_cannot_make_a_different_checkout_the_task(self) -> None:
        self.assert_refuses(self.task, self.task, environment={"GIT_DIR": str(self.primary / ".git")})
        self.assert_refuses(self.task, self.task, environment={"GIT_WORK_TREE": str(self.peer)})

    def test_subdirectories_and_symlink_aliases_keep_all_integration_modes(self) -> None:
        nested = self.task / "nested"
        nested.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(self.task, target_is_directory=True)
        for command in INTEGRATION_COMMANDS:
            with self.subTest(command=command):
                result = self.invoke(nested, "git-commit-as-bot", "--git-command", command, "--continue", expected=alias)
                self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(calls, [[command, "--continue"] for command in INTEGRATION_COMMANDS])

    def test_default_and_named_remote_pushes_reach_git_from_own_worktree(self) -> None:
        self.run_git(self.task, "remote", "add", "fork", "https://github.com/example/project.git")
        for arguments in (("-u", "origin", "HEAD"), ("--remote", "fork", "--delete", "work/old")):
            result = self.invoke(self.task, "git-push-as-bot", *arguments, expected=self.task)
            self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(
            [call[call.index("push") + 1 :] for call in calls], [["-u", "origin", "HEAD"], ["fork", "--delete", "work/old"]]
        )


if __name__ == "__main__":
    unittest.main()
