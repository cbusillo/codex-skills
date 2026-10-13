#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise fork contributions without GitHub credentials or network writes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parent


class ForkIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git = shutil.which("git")
        self.assertIsNotNone(self.git)
        self.env = {
            "PATH": os.environ["PATH"], "HOME": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "CODEX_SKILLS_ENV_FILE": str(self.root / "missing.env"),
            "CODEX_AUTOMATION_LOGIN": "fixture-bot",
            "CODEX_AUTOMATION_EMAIL": "bot@example.invalid",
            "GITHUB_APP_ID": "fixture-app", "GITHUB_APP_INSTALLATION_ID": "fixture-installation",
            "GITHUB_APP_PRIVATE_KEY_PATH": str(self.root / "unused.pem"),
            "GIT_COMMIT_AS_BOT_PYTHON": sys.executable,
            "GIT_PUSH_AS_BOT_PYTHON": sys.executable,
            "IDENTITY_LOG": str(self.root / "identity.log"),
        }
        self.run_git("init", "-q")
        self.run_git("config", "user.name", "Fixture Person")
        self.run_git("config", "user.email", "person@example.invalid")
        self.run_git("remote", "add", "origin", "git@github.com:fork-owner/project.git")
        self.run_git("remote", "add", "upstream", "https://github.com/upstream-owner/project.git")
        helper = self.root / "identity.py"
        helper.write_text('''import json, os, sys
repo = sys.argv[sys.argv.index("--repo") + 1]
with open(os.environ["IDENTITY_LOG"], "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "app-prefix":
    print("GITHUB_CLIENT_APP" if repo.startswith("client-owner/") else "GITHUB_APP")
elif repo == "upstream-owner/project":
    sys.exit(3)
elif repo == "unknown-owner/project":
    sys.exit(1)
else:
    print(os.environ["CODEX_AUTOMATION_LOGIN"])
    print("fixture-installation-token")
''')
        self.env["GIT_COMMIT_AS_BOT_IDENTITY_HELPER"] = str(helper)
        self.env["GIT_PUSH_AS_BOT_IDENTITY_HELPER"] = str(helper)

    def run_git(self, *args: str) -> str:
        result = subprocess.run([str(self.git), *args], cwd=self.repo, env=self.env,
                                text=True, capture_output=True, check=True)
        return result.stdout.strip()

    def commit(self, *prefix: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(SCRIPTS / "git-commit-as-bot"), *prefix,
                               "--allow-empty", "-m", "fixture"], cwd=self.repo,
                              env=self.env | (env or {}), text=True, capture_output=True)

    def identities(self) -> list[str]:
        return self.run_git("show", "-s", "--format=%an%n%ae%n%cn%n%ce").splitlines()

    def test_explicit_upstream_uses_person_for_author_and_committer(self) -> None:
        result = self.commit("--repo", "upstream-owner/project", env={"GH_WITH_ENV_TOKEN_OWN_USER": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.identities(), ["Fixture Person", "person@example.invalid"] * 2)
        calls = [json.loads(line) for line in (self.root / "identity.log").read_text().splitlines()]
        self.assertTrue(all(call[call.index("--repo") + 1] == "upstream-owner/project" for call in calls))

    def test_origin_default_and_installed_explicit_target_keep_bot(self) -> None:
        for prefix in ((), ("--repo", "fork-owner/project")):
            with self.subTest(prefix=prefix):
                result = self.commit(*prefix, env={"GH_WITH_ENV_TOKEN_OWN_USER": "1"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.identities(), [self.env["CODEX_AUTOMATION_LOGIN"],
                                                     self.env["CODEX_AUTOMATION_EMAIL"]] * 2)

    def test_client_only_app_selection_uses_explicit_target(self) -> None:
        self.env.pop("GITHUB_APP_ID")
        self.env["GITHUB_CLIENT_APP_OWNERS"] = "client-owner"
        result = self.commit("--git-command", "commit", "--repo", "client-owner/project")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in (self.root / "identity.log").read_text().splitlines()]
        self.assertEqual([call[0] for call in calls], ["app-prefix", "app-auth"])
        self.assertTrue(all(call[call.index("--repo") + 1] == "client-owner/project" for call in calls))

    def test_upstream_refuses_without_command_approval_or_with_required_automation(self) -> None:
        env_file = self.root / "local.env"
        env_file.write_text("GH_WITH_ENV_TOKEN_OWN_USER=1\nGH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH=0\n")
        cases = ({}, {"CODEX_SKILLS_ENV_FILE": str(env_file)},
                 {"GH_WITH_ENV_TOKEN_OWN_USER": "1", "GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH": "1",
                  "CODEX_SKILLS_ENV_FILE": str(env_file)})
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.assertNotEqual(self.commit("--repo", "upstream-owner/project", env=overrides).returncode, 0)
                self.assertEqual(self.run_git("rev-list", "--all"), "")

    def test_bad_target_and_failed_lookup_never_commit(self) -> None:
        for target in ("unknown-owner/project", "owner", "owner/repo/extra", "../project",
                       "https://github.com/owner/repo", "owner/", "owner/repo with spaces"):
            with self.subTest(target=target):
                self.assertNotEqual(self.commit("--repo", target).returncode, 0)
                self.assertEqual(self.run_git("rev-list", "--all"), "")
        result = self.commit("--repo", "fork-owner/project", "--repo", "upstream-owner/project")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.run_git("rev-list", "--all"), "")

    def test_fork_push_stays_authenticated_for_destination_after_person_commit(self) -> None:
        result = self.commit("--repo", "upstream-owner/project", env={"GH_WITH_ENV_TOKEN_OWN_USER": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        git_proxy = self.root / "git-proxy"
        git_proxy.write_text(f'''#!{sys.executable}
import json, os, subprocess, sys
if "push" in sys.argv[1:]:
    token = subprocess.check_output([os.environ["GIT_ASKPASS"], "Password for https://github.com: "], text=True).strip()
    with open(os.environ["PUSH_LOG"], "w") as stream:
        json.dump({{"args": sys.argv[1:], "token": token}}, stream)
else:
    sys.exit(subprocess.call([{str(self.git)!r}, *sys.argv[1:]]))
''')
        git_proxy.chmod(0o700)
        push_log = self.root / "push.json"
        push = subprocess.run([str(SCRIPTS / "git-push-as-bot"), "-u", "origin", "HEAD"],
                              cwd=self.repo, env=self.env | {"GIT_PUSH_AS_BOT_GIT": str(git_proxy),
                                                           "PUSH_LOG": str(push_log)},
                              text=True, capture_output=True)
        self.assertEqual(push.returncode, 0, push.stderr)
        self.assertEqual(json.loads(push_log.read_text())["token"], "fixture-installation-token")
        calls = [json.loads(line) for line in (self.root / "identity.log").read_text().splitlines()]
        self.assertEqual(calls[-1][calls[-1].index("--repo") + 1], "fork-owner/project")
        self.assertEqual(self.run_git("remote", "get-url", "origin"), "git@github.com:fork-owner/project.git")
        self.assertEqual(self.identities(), ["Fixture Person", "person@example.invalid"] * 2)


if __name__ == "__main__":
    unittest.main()
