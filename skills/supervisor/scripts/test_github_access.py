# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import foreign_watch
import github_access


def patch_attr(target: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    return patch.object(target, name, *args, **kwargs)


class GitHubAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.enterContext(
            patch.dict(
                os.environ, {"PATH": os.defpath, "CODEX_SKILLS_ENV_FILE": str(self.root / "absent.env")}, clear=True
            )
        )

    def test_plain_install_selects_cli_before_requests_and_pins_all_readers_and_writer(self) -> None:
        with patch.object(
            github_access.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "director\n", "")
        ) as login:
            self.assertEqual(github_access.select_login(), "director")
        self.assertEqual(login.call_args.kwargs["env"]["GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK"], "1")
        self.assertNotIn(github_access.github_identity.OWN_USER_OPT_IN, login.call_args.kwargs["env"])
        with patch_attr(foreign_watch, "GitHubReader") as reader:
            foreign_watch.message_reader(100, "director")
        self.assertEqual(reader.call_args.kwargs["expected_actor"], "director")
        self.assertNotIn("--require-automation-auth", reader.call_args.kwargs["gh_prefix_args"])
        env = github_access.write_environment("director")
        self.assertEqual(env["GH_WITH_ENV_TOKEN_EXPECTED_LOGIN"], "director")
        with patch.object(
            github_access.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "different\n", "")
        ):
            with self.assertRaises(ValueError):
                github_access.select_login("director")

    def test_configured_and_partial_automation_never_switch_after_denial(self) -> None:
        for configuration in (
            {"GITHUB_APP_ID": "42"},
            {"CODEX_AUTOMATION_LOGIN": "agent[bot]"},
            {"GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH": "1"},
        ):
            with (
                self.subTest(configuration=configuration),
                patch.dict(os.environ, configuration),
                patch_attr(github_access.github_read, "GitHubReader") as reader,
                patch.object(github_access.subprocess, "run") as cli,
            ):
                reader.return_value.get_json.side_effect = ValueError("configured auth refused")
                with self.assertRaises(ValueError):
                    github_access.select_login()
                self.assertIn("--require-automation-auth", reader.call_args.kwargs["gh_prefix_args"])
                cli.assert_not_called()
                with self.assertRaises(ValueError):
                    github_access.select_login("director")
                cli.assert_not_called()

    def test_service_environment_preserves_selectors_without_copying_transient_credentials(self) -> None:
        env_file = self.root / "configured.env"
        env_file.write_text("CODEX_GITHUB_TOKEN=fixture-secret\nCODEX_AUTOMATION_LOGIN=agent\n")
        with patch.dict(
            os.environ,
            {
                "CODEX_SKILLS_ENV_FILE": str(env_file),
                "CODE_HOME": str(self.root / "code"),
                "GH_CONFIG_DIR": str(self.root / "gh"),
            },
        ):
            environment = github_access.persistent_environment()
        self.assertEqual(environment["CODEX_SKILLS_ENV_FILE"], str(env_file.resolve()))
        self.assertEqual(environment["CODE_HOME"], str(self.root / "code"))
        self.assertEqual(environment["GH_CONFIG_DIR"], str(self.root / "gh"))
        self.assertNotIn("fixture-secret", str(environment))
        with (
            patch.dict(os.environ, {"GH_TOKEN": "transient-secret"}),
            patch_attr(github_access.github_read, "GitHubReader") as reader,
            patch.object(github_access.subprocess, "run") as cli,
        ):
            with self.assertRaises(ValueError):
                github_access.persistent_environment()
        reader.assert_not_called()
        cli.assert_not_called()

    def test_plain_login_lists_private_owned_repositories_with_owner_and_archive_filter(self) -> None:
        reader = Mock()
        reader.paged_json.return_value = [
            {"full_name": "director/private", "private": True, "archived": False},
            {"full_name": "director/retired", "archived": True},
            {"full_name": "other/shared", "archived": False},
        ]
        self.assertEqual(foreign_watch.installed_repositories(reader, "director", "director"), ["director/private"])
        self.assertEqual(
            foreign_watch.installed_repositories(reader, "director", "different-login"), ["director/private"]
        )


if __name__ == "__main__":
    unittest.main()
