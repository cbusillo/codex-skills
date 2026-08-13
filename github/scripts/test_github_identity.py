#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import github_identity


def test_local_env_file_precedence_matches_shell() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        explicit = root / "explicit.env"
        explicit.write_text("CODEX_AUTOMATION_LOGIN=explicit\n", encoding="utf-8")
        code_home = root / "code"
        codex_home = root / "codex"
        home = root / "home"
        code_home.mkdir()
        codex_home.mkdir()
        (home / ".code").mkdir(parents=True)
        (code_home / "local.env").write_text("CODEX_AUTOMATION_LOGIN=code\n", encoding="utf-8")
        (codex_home / "local.env").write_text("CODEX_AUTOMATION_LOGIN=codex\n", encoding="utf-8")
        (home / ".code" / "local.env").write_text("CODEX_AUTOMATION_LOGIN=home\n", encoding="utf-8")
        values = {
            "CODEX_SKILLS_ENV_FILE": str(explicit),
            "CODE_HOME": str(code_home),
            "CODEX_HOME": str(codex_home),
            "HOME": str(home),
        }
        assert github_identity.automation_login(values) == "explicit"
        values.pop("CODEX_SKILLS_ENV_FILE")
        assert github_identity.automation_login(values) == "code"
        values.pop("CODE_HOME")
        assert github_identity.automation_login(values) == "codex"
        values.pop("CODEX_HOME")
        assert github_identity.automation_login(values) == "home"


def test_per_tool_overrides_win_over_shared_identity() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env_file = Path(directory) / "local.env"
        env_file.write_text(
            "CODEX_AUTOMATION_LOGIN=shared\n"
            "CODEX_AUTOMATION_EMAIL=shared@example.invalid\n"
            "GH_WITH_ENV_TOKEN_EXPECTED_LOGIN=github-tool\n"
            "GIT_COMMIT_AS_BOT_NAME='Git Tool'\n"
            "GIT_COMMIT_AS_BOT_EMAIL=git@example.invalid\n",
            encoding="utf-8",
        )
        values = {"CODEX_SKILLS_ENV_FILE": str(env_file)}
        assert github_identity.automation_login(values) == "github-tool"
        assert github_identity.commit_name(values) == "Git Tool"
        assert github_identity.commit_email(values) == "git@example.invalid"


def test_unconfigured_identity_is_distinct_from_fallback() -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert github_identity.automation_login() is None
        assert not github_identity.active_auth_fallback_allowed()
        with patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK": "1"}):
            assert github_identity.automation_login() is None
            assert github_identity.active_auth_fallback_allowed()


def test_git_commit_identity_uses_shared_role_and_tool_override() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env_file = Path(directory) / "local.env"
        env_file.write_text(
            "CODEX_AUTOMATION_LOGIN='Automation Bot'\n"
            "CODEX_AUTOMATION_EMAIL=automation@example.invalid\n",
            encoding="utf-8",
        )
        values = {"CODEX_SKILLS_ENV_FILE": str(env_file)}
        assert github_identity.commit_name(values) == "Automation Bot"
        assert github_identity.commit_email(values) == "automation@example.invalid"


def test_unquoted_multiword_values_are_ignored_like_invalid_shell_assignments() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env_file = Path(directory) / "local.env"
        env_file.write_text("CODEX_AUTOMATION_LOGIN=Automation Bot\n", encoding="utf-8")
        values = {"CODEX_SKILLS_ENV_FILE": str(env_file)}
        assert github_identity.automation_login(values) is None


def main() -> None:
    tests = [
        test_local_env_file_precedence_matches_shell,
        test_per_tool_overrides_win_over_shared_identity,
        test_unconfigured_identity_is_distinct_from_fallback,
        test_git_commit_identity_uses_shared_role_and_tool_override,
        test_unquoted_multiword_values_are_ignored_like_invalid_shell_assignments,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
