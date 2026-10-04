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
    extra_env: dict[str, str] | None = None,
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
    env.update(extra_env or {})
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


def test_no_token_check_fails_explicitly_without_running_gh() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        unused = root / "unused.py"
        write(unused, "raise AssertionError('should not run')\n")
        fake_gh = root / "gh"
        called = root / "called"
        write(fake_gh, f"#!/bin/sh\ntouch '{called}'\nprintf 'GitHub CLI help\\n'\n")
        for extra_env in (
            {"GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK": "1"},
            {"GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK": "1", "CODEX_AUTOMATION_LOGIN": "expected-bot"},
            {},
            {"GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK": "1", "GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH": "1"},
        ):
            result = run_wrapper(root / "missing.env", unused, unused, "--check",
                                 gh_command=fake_gh, extra_env=extra_env)
            assert result.returncode == 1, result
            assert result.stdout == ""
            assert "configure" in result.stderr
            assert not called.exists(), result


def test_check_rejects_commands_before_active_auth_fallback() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        unused = root / "unused.py"
        write(unused, "raise AssertionError('should not run')\n")
        fake_gh = root / "gh"
        called = root / "called"
        write(fake_gh, f"#!/bin/sh\ntouch '{called}'\n")
        result = run_wrapper(root / "missing.env", unused, unused,
                             "--check", "issue", "comment", "1", "--body", "x",
                             gh_command=fake_gh,
                             extra_env={"GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK": "1"})
        assert result.returncode == 2, result
        assert "--check does not accept a gh command" in result.stderr
        assert not called.exists(), result


def test_no_token_command_preserves_authorized_active_auth_fallback() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        unused = root / "unused.py"
        write(unused, "raise AssertionError('should not run')\n")
        fake_gh = root / "gh"
        write(fake_gh, "#!/bin/sh\n"
              "[ -z \"${GH_TOKEN:-}${GITHUB_TOKEN:-}${CODEX_GITHUB_TOKEN:-}\" ] || exit 41\n"
              "if [ \"$*\" = 'api user --jq .login' ]; then echo active-login; exit 0; fi\n"
              "[ \"$*\" = 'pr view 1' ] || exit 42\n"
              "printf 'pull request details\\n'\n")
        result = run_wrapper(root / "missing.env", unused, unused, "pr", "view", "1",
                             gh_command=fake_gh,
                             extra_env={"GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK": "1"})
        assert result.returncode == 0, result.stderr
        assert result.stdout == "pull request details\n"
        assert "'active-login'" in result.stderr


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


def test_app_installation_follows_the_target_repository() -> None:
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
        write(
            identity,
            "import sys\n"
            "args = sys.argv[1:]\n"
            "if args == ['app-auth', '--repo', 'other-owner/uncovered', '--require-installation']:\n"
            "    raise SystemExit('the GitHub App is not installed on other-owner/uncovered')\n"
            "print('catalog-app[bot]')\n"
            "print('token:' + ' '.join(args[1:]))\n",
        )
        unused = root / "unused.py"
        write(unused, "raise AssertionError('classifier should not run')\n")
        fake_gh = root / "gh"
        write(fake_gh, "#!/bin/sh\nprintf '%s\\n' \"$GH_TOKEN\"\n")

        cases = {
            ("api", "repos/second-owner/site/issues/1/comments", "--method", "POST", "-f", "body=x"):
                "token:--repo second-owner/site --require-installation",
            ("issue", "comment", "1", "-R", "github.com/second-owner/site", "--body", "x"):
                "token:--repo second-owner/site --require-installation",
            ("api", "/repos/third-party/library/contents/README.md"): "token:--repo third-party/library",
            ("api", "repos/{owner}/{repo}/pulls"): "token:",
            ("issue", "comment", "1", "--body", "repos/alice/tools", "-R", "bob/site"):
                "token:--repo bob/site --require-installation",
            ("issue", "comment", "1", "-R", "bob/site", "--body", "https://github.com/alice/tools/issues/2"):
                "token:--repo bob/site --require-installation",
            ("pr", "merge", "--squash", "https://github.com/second-owner/site/pull/2"):
                "token:--repo second-owner/site --require-installation",
            ("issue", "comment", "1", "-Rbob/site", "--body", "hello"):
                "token:--repo bob/site --require-installation",
            ("issue", "comment", "1", "--body", "repos/alice/tools"): "token:",
            ("api", "--jq", "repos/x/y", "repos/second-owner/site"): "token:--repo second-owner/site",
            ("pr", "view", "1", "-R", "git@github.com:second-owner/site.git"): "token:--repo second-owner/site",
            ("issue", "comment", "1", "-R", "first/tools", "-R", "second-owner/site", "--body", "x"):
                "token:--repo second-owner/site --require-installation",
            ("issue", "comment", "1", "-R=second-owner/site", "--body", "x"):
                "token:--repo second-owner/site --require-installation",
            ("issue", "comment", "https://github.com/second-owner/site/issues/1", "-R", "first/tools", "--body", "x"):
                "token:--repo second-owner/site --require-installation",
            ("api", "graphql", "-f", "query=query { viewer { login } }"): "token:",
        }
        for args, token in cases.items():
            result = run_wrapper(env_file, unused, identity, *args, gh_command=fake_gh)
            assert result.returncode == 0, (args, result.stderr)
            assert result.stdout == f"{token}\n", (args, result.stdout)

        refused = run_wrapper(
            env_file, unused, identity,
            "api", "repos/other-owner/uncovered/issues/1/comments", "--method", "POST", "-f", "body=x",
            gh_command=unused,
        )
        assert refused.returncode != 0
        assert "not installed on other-owner/uncovered" in refused.stderr


def test_write_to_another_owners_repository_without_installation_runs_as_the_person_on_opt_in() -> None:
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
        # github_identity.CONTRIBUTOR_EXIT_STATUS for the contributor repository.
        write(
            identity,
            "import sys\n"
            "if '--repo' in sys.argv and sys.argv[sys.argv.index('--repo') + 1] == 'director/catalog':\n"
            "    raise SystemExit(3)\n"
            "print('catalog-app[bot]')\n"
            "print('installation-token')\n",
        )
        unused = root / "unused.py"
        write(unused, "raise AssertionError('classifier should not run')\n")
        fake_gh = root / "gh"
        write(
            fake_gh,
            "#!/bin/sh\n"
            "if [ \"$*\" = 'api user --jq .login' ]; then\n"
            "  [ -z \"${GH_TOKEN:-}\" ] && echo contributor-login && exit 0\n"
            "  exit 42\n"
            "fi\n"
            "printf 'ran-with-token:%s\\n' \"${GH_TOKEN:-active-login}\"\n",
        )
        write_args = ("issue", "comment", "1", "-R", "director/catalog", "--body", "x")
        opt_in = {"GH_WITH_ENV_TOKEN_OWN_USER": "1"}

        result = run_wrapper(env_file, unused, identity, *write_args, gh_command=fake_gh, extra_env=opt_in)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "ran-with-token:active-login\n"
        assert "acting as your own GitHub user on director/catalog" in result.stderr
        assert "'contributor-login'" in result.stderr

        # Without the caller's opt-in, including one only in local.env, refuse
        # and name the opt-in.
        for opted_in_env_file in (False, True):
            if opted_in_env_file:
                env_file.write_text(env_file.read_text() + "GH_WITH_ENV_TOKEN_OWN_USER=1\n")
            refused = run_wrapper(env_file, unused, identity, *write_args, gh_command=fake_gh)
            assert refused.returncode != 0
            assert refused.stdout == ""
            assert "GH_WITH_ENV_TOKEN_OWN_USER=1" in refused.stderr
            assert "acting as your own GitHub user" not in refused.stderr

        env_file.write_text(env_file.read_text() + "GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH=1\n")
        required = run_wrapper(env_file, unused, identity, *write_args, gh_command=fake_gh, extra_env=opt_in)
        assert required.returncode != 0
        assert required.stdout == ""
        assert "refusing to act as your own GitHub user" in required.stderr


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
        test_no_token_check_fails_explicitly_without_running_gh,
        test_check_rejects_commands_before_active_auth_fallback,
        test_no_token_command_preserves_authorized_active_auth_fallback,
        test_partial_app_configuration_fails_closed,
        test_app_auth_failure_never_falls_back_to_active_user,
        test_empty_app_auth_response_fails_closed,
        test_app_auth_takes_precedence_and_runs_write_as_verified_app,
        test_app_installation_follows_the_target_repository,
        test_write_to_another_owners_repository_without_installation_runs_as_the_person_on_opt_in,
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
