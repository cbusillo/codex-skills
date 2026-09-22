#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("gh-with-env-token")


def write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def fake_app_identity(login: str = "catalog-app[bot]") -> str:
    return (
        "import sys\n"
        f"login = {login!r}\n"
        "if sys.argv[1] == 'app-check':\n"
        "    print(login)\n"
        "else:\n"
        "    print(login)\n"
        "    print('installation-token')\n"
    )


def run_wrapper(
    env_file: Path,
    classifier: Path,
    identity: Path,
    *args: str,
    gh_command: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(env_file.parent),
        "CODEX_SKILLS_ENV_FILE": str(env_file),
        "GH_WITH_ENV_TOKEN_CLASSIFIER": str(classifier),
        "GH_WITH_ENV_TOKEN_IDENTITY_HELPER": str(identity),
        "GH_WITH_ENV_TOKEN_PYTHON": sys.executable,
    }
    if gh_command is not None:
        env["GH_WITH_ENV_TOKEN_GH"] = str(gh_command)
    return subprocess.run(
        [str(SCRIPT), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


def test_check_reports_app_identity_and_source() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file = root / "local.env"
        env_file.write_text(
            "GITHUB_APP_ID=12345\n"
            "GITHUB_APP_INSTALLATION_ID=67890\n"
            "GITHUB_APP_PRIVATE_KEY_PATH=/fake/app.pem\n"
            "CODEX_AUTOMATION_LOGIN='catalog-app[bot]'\n",
            encoding="utf-8",
        )
        identity = root / "identity.py"
        write(identity, fake_app_identity())
        classifier = root / "classifier.py"
        write(classifier, "raise AssertionError('App check should not use /user')\n")
        result = run_wrapper(env_file, classifier, identity, "--check")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "GitHub automation actor: catalog-app[bot] (source: github_app)\n"
        assert "installation-token" not in result.stdout + result.stderr


def test_check_preserves_user_token_fallback_when_app_is_absent() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file = root / "local.env"
        env_file.write_text(
            "CODEX_GITHUB_TOKEN=user-token\n"
            "CODEX_AUTOMATION_LOGIN=automation-user\n",
            encoding="utf-8",
        )
        identity = root / "identity.py"
        write(identity, "raise AssertionError('App token helper should not run')\n")
        classifier = root / "classifier.py"
        write(
            classifier,
            "import json, os\n"
            "assert os.environ.get('GH_TOKEN') == 'user-token'\n"
            "print(json.dumps({'body': {'login': 'automation-user'}}))\n",
        )
        result = run_wrapper(env_file, classifier, identity, "--check")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "GitHub automation actor: automation-user (source: user_token)\n"


def test_partial_app_configuration_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file = root / "local.env"
        env_file.write_text("GITHUB_APP_ID=12345\n", encoding="utf-8")
        unused = root / "unused.py"
        write(unused, "raise AssertionError('should not run')\n")
        result = run_wrapper(env_file, unused, unused, "--check")
        assert result.returncode == 1
        assert "incomplete GitHub App configuration" in result.stderr


def test_app_auth_failure_never_falls_back_to_active_user() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file = root / "local.env"
        env_file.write_text(
            "GITHUB_APP_ID=12345\n"
            "GITHUB_APP_INSTALLATION_ID=67890\n"
            "GITHUB_APP_PRIVATE_KEY_PATH=/fake/app.pem\n"
            "GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK=1\n",
            encoding="utf-8",
        )
        identity = root / "identity.py"
        write(identity, "raise SystemExit('mint failed')\n")
        unused = root / "unused.py"
        write(unused, "raise AssertionError('should not run')\n")
        result = run_wrapper(env_file, unused, identity, "issue", "comment", "1", gh_command=unused)
        assert result.returncode != 0
        assert "mint failed" in result.stderr


def test_empty_app_auth_response_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file = root / "local.env"
        env_file.write_text(
            "GITHUB_APP_ID=12345\n"
            "GITHUB_APP_INSTALLATION_ID=67890\n"
            "GITHUB_APP_PRIVATE_KEY_PATH=/fake/app.pem\n",
            encoding="utf-8",
        )
        identity = root / "identity.py"
        write(identity, "pass\n")
        unused = root / "unused.py"
        write(unused, "raise AssertionError('should not run')\n")
        result = run_wrapper(env_file, unused, identity, "--check")
        assert result.returncode == 1
        assert "invalid response" in result.stderr


def test_app_auth_takes_precedence_and_runs_write_as_verified_app() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file = root / "local.env"
        env_file.write_text(
            "GITHUB_APP_ID=12345\n"
            "GITHUB_APP_INSTALLATION_ID=67890\n"
            "GITHUB_APP_PRIVATE_KEY_PATH=/fake/app.pem\n"
            "CODEX_GITHUB_TOKEN=user-token\n"
            "CODEX_AUTOMATION_LOGIN='catalog-app[bot]'\n",
            encoding="utf-8",
        )
        identity = root / "identity.py"
        write(identity, fake_app_identity())
        unused = root / "unused.py"
        write(unused, "raise AssertionError('classifier should not run')\n")
        fake_gh = root / "gh"
        write(
            fake_gh,
            "#!/bin/sh\n"
            "[ \"$GH_TOKEN\" = installation-token ] || exit 41\n"
            "printf 'write-ran-as-app\\n'\n",
        )
        result = run_wrapper(env_file, unused, identity, "issue", "comment", "1", gh_command=fake_gh)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "write-ran-as-app\n"


def test_app_actor_probe_synthesizes_include_response_without_user_endpoint() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file = root / "local.env"
        env_file.write_text(
            "GITHUB_APP_ID=12345\n"
            "GITHUB_APP_INSTALLATION_ID=67890\n"
            "GITHUB_APP_PRIVATE_KEY_PATH=/fake/app.pem\n"
            "CODEX_AUTOMATION_LOGIN='catalog-app[bot]'\n",
            encoding="utf-8",
        )
        identity = root / "identity.py"
        write(identity, fake_app_identity())
        unused = root / "unused.py"
        write(unused, "raise AssertionError('GitHub user endpoint should not run')\n")
        result = run_wrapper(
            env_file,
            unused,
            identity,
            "api",
            "--method",
            "GET",
            "--include",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            "/user",
            gh_command=unused,
        )
        assert result.returncode == 0, result.stderr
        assert "HTTP/2.0 200" in result.stdout
        assert '"login":"catalog-app[bot]"' in result.stdout
        direct = run_wrapper(
            env_file,
            unused,
            identity,
            "api",
            "user",
            "--method",
            "GET",
            gh_command=unused,
        )
        assert direct.returncode == 0, direct.stderr
        assert direct.stdout == '{"login":"catalog-app[bot]","type":"Bot"}\n'


def test_app_login_mismatch_fails_closed_for_write_and_check() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file = root / "local.env"
        env_file.write_text(
            "GITHUB_APP_ID=12345\n"
            "GITHUB_APP_INSTALLATION_ID=67890\n"
            "GITHUB_APP_PRIVATE_KEY_PATH=/fake/app.pem\n"
            "CODEX_AUTOMATION_LOGIN='expected-app[bot]'\n",
            encoding="utf-8",
        )
        identity = root / "identity.py"
        write(identity, fake_app_identity("different-app[bot]"))
        unused = root / "unused.py"
        write(unused, "raise AssertionError('GitHub command should not run')\n")
        write_result = run_wrapper(env_file, unused, identity, "issue", "comment", "1", gh_command=unused)
        assert write_result.returncode == 1
        assert "different-app[bot]" in write_result.stderr
        check_result = run_wrapper(env_file, unused, identity, "--check", gh_command=unused)
        assert check_result.returncode == 1
        assert "different-app[bot]" in check_result.stderr


def test_app_login_comparison_is_case_insensitive() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file = root / "local.env"
        env_file.write_text(
            "GITHUB_APP_ID=12345\n"
            "GITHUB_APP_INSTALLATION_ID=67890\n"
            "GITHUB_APP_PRIVATE_KEY_PATH=/fake/app.pem\n"
            "CODEX_AUTOMATION_LOGIN='Catalog-App[bot]'\n",
            encoding="utf-8",
        )
        identity = root / "identity.py"
        write(identity, fake_app_identity())
        unused = root / "unused.py"
        write(unused, "raise AssertionError('classifier should not run')\n")
        result = run_wrapper(env_file, unused, identity, "--check", gh_command=unused)
        assert result.returncode == 0, result.stderr


def test_print_auth_account_reports_verified_app_without_gh_auth_status() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file = root / "local.env"
        env_file.write_text(
            "GITHUB_APP_ID=12345\n"
            "GITHUB_APP_INSTALLATION_ID=67890\n"
            "GITHUB_APP_PRIVATE_KEY_PATH=/fake/app.pem\n"
            "CODEX_AUTOMATION_LOGIN='catalog-app[bot]'\n",
            encoding="utf-8",
        )
        identity = root / "identity.py"
        write(identity, fake_app_identity())
        unused = root / "unused.py"
        write(unused, "raise AssertionError('classifier should not run')\n")
        fake_gh = root / "gh"
        write(fake_gh, "#!/bin/sh\nprintf 'command-output\\n'\n")
        result = run_wrapper(
            env_file,
            unused,
            identity,
            "--print-auth-account",
            "pr",
            "view",
            "1",
            gh_command=fake_gh,
        )
        assert result.returncode == 0, result.stderr
        assert "GitHub automation actor: catalog-app[bot] (source: github_app)" in result.stderr
        assert result.stdout == "command-output\n"


def test_app_actor_probe_does_not_fabricate_other_paths_or_writes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file = root / "local.env"
        env_file.write_text(
            "GITHUB_APP_ID=12345\n"
            "GITHUB_APP_INSTALLATION_ID=67890\n"
            "GITHUB_APP_PRIVATE_KEY_PATH=/fake/app.pem\n"
            "CODEX_AUTOMATION_LOGIN='catalog-app[bot]'\n",
            encoding="utf-8",
        )
        identity = root / "identity.py"
        write(identity, fake_app_identity())
        unused = root / "unused.py"
        write(unused, "raise AssertionError('classifier should not run')\n")
        fake_gh = root / "gh"
        write(
            fake_gh,
            "#!/bin/sh\n"
            "[ \"$GH_TOKEN\" = installation-token ] || exit 41\n"
            "printf 'real-gh-path\\n'\n",
        )
        other_path = run_wrapper(env_file, unused, identity, "api", "/user/repos", gh_command=fake_gh)
        assert other_path.returncode == 0
        assert other_path.stdout == "real-gh-path\n"
        post_user = run_wrapper(
            env_file,
            unused,
            identity,
            "api",
            "/user",
            "--method",
            "POST",
            gh_command=fake_gh,
        )
        assert post_user.returncode == 0
        assert post_user.stdout == "real-gh-path\n"


def main() -> None:
    tests = [
        test_check_reports_app_identity_and_source,
        test_check_preserves_user_token_fallback_when_app_is_absent,
        test_partial_app_configuration_fails_closed,
        test_app_auth_failure_never_falls_back_to_active_user,
        test_empty_app_auth_response_fails_closed,
        test_app_auth_takes_precedence_and_runs_write_as_verified_app,
        test_app_actor_probe_synthesizes_include_response_without_user_endpoint,
        test_app_login_mismatch_fails_closed_for_write_and_check,
        test_app_login_comparison_is_case_insensitive,
        test_print_auth_account_reports_verified_app_without_gh_auth_status,
        test_app_actor_probe_does_not_fabricate_other_paths_or_writes,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
