#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

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


def app_fixture(root: Path) -> tuple[Path, Path, Path]:
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
    return env_file, identity, unused


def run_wrapper(
    env_file: Path,
    classifier: Path,
    identity: Path,
    *args: str,
    gh_command: Path | None = None,
    extra_env: dict[str, str] | None = None,
    input_text: str | None = None,
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
        input=input_text,
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


def test_search_archive_scope_through_wrapper() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file, classifier, identity, fake_gh = user_token_fixture(root)
        fixture = SCRIPT.with_name("fixtures") / "archived-repositories.json"
        write(fake_gh, f'''#!{sys.executable}
import json, sys
from urllib.parse import parse_qs, urlsplit
args = sys.argv[1:]
queries = list(args)
for arg in args:
    queries.extend(parse_qs(urlsplit(arg).query).get("q", []))
query = " ".join(queries)
rows = json.loads({fixture.read_text()!r})
if "archived:false" in query or "--archived=false" in args:
    rows = [row for row in rows if not row["archived"]]
elif "archived:true" in query or "--archived=true" in args:
    rows = [row for row in rows if row["archived"]]
print(json.dumps({{"args": args, "repositories": [row["full_name"] for row in rows]}}))
''')
        repositories = json.loads(fixture.read_text())
        current = [row["full_name"] for row in repositories if not row["archived"]]
        archived = [row["full_name"] for row in repositories if row["archived"]]
        commands = (
            ("search", "issues", "--owner", "owner", "--state", "open"),
            ("search", "prs", "user:owner"),
            ("--hostname", "github.com", "search", "prs", "--", "-label:bug"),
            ("api", f"search/issues?{urlencode({'q': 'user:owner', 'per_page': 100})}"),
            ("api", "-X", "GET", "/search/issues", "-f", "q=user:owner"),
            ("api", "search/issues", "--method=GET", "--raw-field=q=user:owner"),
            ("api", "-XGET", "search/issues", "-Fq=user:owner"),
            ("api", "-XGET", "search/issues", "-ifq=user:owner"),
        )
        for command in commands:
            for opt_in, expected in (((), current), (("--include-archived",), current + archived)):
                result = run_wrapper(env_file, classifier, identity, *opt_in, *command, gh_command=fake_gh)
                assert result.returncode == 0, (command, result.stderr)
                payload = json.loads(result.stdout)
                assert payload["repositories"] == expected, (command, payload)
                assert "--include-archived" not in payload["args"]
        for command in (
            ("search", "issues", "archived:true"),
            ("search", "issues", "--include-prs", "archived:true"),
            ("search", "prs", "--archived=true"),
            ("api", f"search/issues?{urlencode({'q': 'archived:true'})}"),
            ("api", "-XGET", "search/issues", "-fq=archived:true"),
        ):
            result = run_wrapper(env_file, classifier, identity, *command, gh_command=fake_gh)
            assert result.returncode == 0, result.stderr
            payload = json.loads(result.stdout)
            assert payload["repositories"] == archived, (command, payload)
            assert payload["args"] == list(command)
        result = run_wrapper(env_file, classifier, identity, "search", "prs", "--include-archived", gh_command=fake_gh)
        assert json.loads(result.stdout)["repositories"] == current + archived
        result = run_wrapper(env_file, classifier, identity, "api", "search/issues?q=user:owner", "--include-archived", gh_command=fake_gh)
        assert json.loads(result.stdout)["repositories"] == current + archived
        write(identity, fake_app_identity())
        result = run_wrapper(
            env_file, classifier, identity, "--reader", "search", "issues", "user:owner", gh_command=fake_gh,
            extra_env={"GITHUB_READER_APP_ID": "123", "GITHUB_READER_APP_INSTALLATION_ID": "456",
                       "GITHUB_READER_APP_PRIVATE_KEY_PATH": "/fake/key.pem"},
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["repositories"] == current
        injected = root / "injected"
        literal = f'quotes\' " and $(touch {injected})\n`touch {injected}`'
        result = run_wrapper(env_file, classifier, identity, "search", "prs", literal, gh_command=fake_gh)
        assert literal in json.loads(result.stdout)["args"]
        assert json.loads(result.stdout)["repositories"] == current
        assert not injected.exists()
        result = run_wrapper(env_file, classifier, identity, "api", "search/issues?q=repo:{owner}/{repo}", gh_command=fake_gh)
        assert "{owner}/{repo}" in " ".join(json.loads(result.stdout)["args"])
        query_file = root / "query.txt"
        query_file.write_text("user:owner")
        for flag in ("-F", "--field", "-iF"):
            result = run_wrapper(env_file, classifier, identity, "api", "-XGET", "search/issues", flag, f"q=@{query_file}", gh_command=fake_gh)
            assert result.returncode == 0, result.stderr
            payload = json.loads(result.stdout)
            assert payload["repositories"] == current
        missing_query = root / "missing.txt"
        binary_query = root / "binary.txt"
        binary_query.write_bytes(b"\xff")
        for query_path in (missing_query, binary_query):
            result = run_wrapper(env_file, classifier, identity, "api", "-XGET", "search/issues", "-F", f"q=@{query_path}", gh_command=fake_gh)
            assert result.returncode != 0, result
            assert result.stderr
            assert "Traceback" not in result.stderr
        result = run_wrapper(env_file, classifier, identity, "api", "-XGET", "search/issues", "-F", "q=@-", gh_command=fake_gh, input_text="user:owner")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["repositories"] == current
        temporary_files = root / "wrapper-tmp"
        temporary_files.mkdir()
        result = run_wrapper(env_file, classifier, identity, "search", "prs", "user:owner", gh_command=fake_gh,
                             extra_env={"TMPDIR": str(temporary_files)})
        assert result.returncode == 0, result.stderr
        assert list(temporary_files.iterdir()) == []


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


def test_user_token_write_verifies_actor_before_running_gh() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file = root / "local.env"
        identity = root / "identity.py"
        write(identity, "raise AssertionError('App token helper should not run')\n")
        classifier = root / "classifier.py"
        fake_gh = root / "gh"
        called = root / "called"
        write(fake_gh, f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{called}'\n")
        commands = (
            ("issue", "comment", "1", "--body", "fixture"),
            ("api", "-X", "POST", "/repos/example/repo/issues/1/comments"),
            ("api", "-XPOST", "/repos/example/repo/issues/1/comments"),
            ("api", "--method=PATCH", "/repos/example/repo/issues/1"),
            ("api", "--method", "POST", "/repos/example/repo/issues/1/comments"),
        )
        for token_variable in ("CODEX_GITHUB_TOKEN", "GITHUB_TOKEN"):
            env_file.write_text(
                f"{token_variable}=user-token\nCODEX_AUTOMATION_LOGIN=automation-user\n",
                encoding="utf-8",
            )
            for command in commands:
                for observed_login in ("different-user", "automation-user"):
                    called.unlink(missing_ok=True)
                    write(
                        classifier,
                        "import json, os, sys\n"
                        "assert os.environ.get('GH_TOKEN') == 'user-token'\n"
                        "assert '--method' in sys.argv and 'GET' in sys.argv and '/user' in sys.argv\n"
                        f"print(json.dumps({{'body': {{'login': {observed_login!r}}}}}))\n",
                    )
                    result = run_wrapper(
                        env_file, classifier, identity, *command, gh_command=fake_gh,
                    )
                    if observed_login == "different-user":
                        assert result.returncode == 1, result
                        assert observed_login in result.stderr and "automation-user" in result.stderr, result
                        assert not called.exists(), result
                    else:
                        assert result.returncode == 0, result
                        assert called.read_text().splitlines() == list(command)
                    assert "user-token" not in result.stdout + result.stderr


def user_token_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    env_file = root / "local.env"
    env_file.write_text("CODEX_GITHUB_TOKEN=fixture-token\n", encoding="utf-8")
    identity = root / "identity.py"
    write(identity, "raise AssertionError('App helper should not run')\n")
    classifier = root / "classifier.py"
    write(
        classifier,
        f"from pathlib import Path\nPath({str(root / 'probed')!r}).touch()\n"
        "raise AssertionError('Actor probe should not run')\n",
    )
    fake_gh = root / "gh"
    write(
        fake_gh,
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> '{root / 'called'}'\n"
        "[ \"$GH_TOKEN\" = fixture-token ] || exit 41\nprintf 'fixture response\\n'\n",
    )
    return env_file, classifier, identity, fake_gh


def test_user_token_write_without_expected_login_refuses_before_actor_probe() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file, classifier, identity, fake_gh = user_token_fixture(root)
        result = run_wrapper(
            env_file, classifier, identity, "issue", "comment", "1", "--body", "fixture",
            gh_command=fake_gh,
        )
        assert result.returncode == 1, result
        assert not (root / "called").exists(), result
        assert not (root / "probed").exists(), result
        assert "fixture-token" not in result.stdout + result.stderr


def test_user_token_expected_login_override_takes_precedence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file, classifier, identity, fake_gh = user_token_fixture(root)
        called = root / "called"
        command = ("issue", "comment", "1", "--body", "fixture")
        for override_in_file in (False, True):
            env_file.write_text(
                "CODEX_GITHUB_TOKEN=fixture-token\nCODEX_AUTOMATION_LOGIN=default-user\n"
                + ("GH_WITH_ENV_TOKEN_EXPECTED_LOGIN=override-user\n" if override_in_file else ""),
                encoding="utf-8",
            )
            for observed_login in ("override-user", "default-user"):
                called.unlink(missing_ok=True)
                write(
                    classifier,
                    "import json, os, sys\n"
                    "assert os.environ['GH_TOKEN'] == 'fixture-token'\n"
                    "assert '--method' in sys.argv and 'GET' in sys.argv and '/user' in sys.argv\n"
                    f"print(json.dumps({{'body': {{'login': {observed_login!r}}}}}))\n",
                )
                result = run_wrapper(
                    env_file, classifier, identity, *command, gh_command=fake_gh,
                    extra_env={} if override_in_file else {"GH_WITH_ENV_TOKEN_EXPECTED_LOGIN": "override-user"},
                )
                if observed_login == "override-user":
                    assert result.returncode == 0, result
                    assert called.read_text().splitlines() == list(command)
                else:
                    assert result.returncode == 1, result
                    assert not called.exists(), result
                assert "fixture-token" not in result.stdout + result.stderr


def test_user_token_get_and_head_delegate_without_actor_verification() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file, classifier, identity, fake_gh = user_token_fixture(root)
        called = root / "called"
        for expected_login in ("", "CODEX_AUTOMATION_LOGIN=default-user\n"):
            env_file.write_text("CODEX_GITHUB_TOKEN=fixture-token\n" + expected_login, encoding="utf-8")
            methods: list[tuple[str, ...]] = [()]
            for method in ("GET", "get", "HEAD", "head"):
                methods.extend((("--method", method), (f"--method={method}",), ("-X", method), (f"-X{method}",)))
            for flags in methods:
                called.unlink(missing_ok=True)
                command = ("api", *flags, "/repos/example/repo")
                result = run_wrapper(env_file, classifier, identity, *command, gh_command=fake_gh)
                assert result.returncode == 0, result
                assert called.read_text().splitlines() == list(command)
                assert result.stdout == "fixture response\n", result
                assert not (root / "probed").exists(), result
                assert "fixture-token" not in result.stdout + result.stderr


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


def test_api_write_checks_read_arguments_as_gh_does() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file, _identity, unused = app_fixture(root)
        identity = root / "identity.py"
        write(identity, "import sys\nprint('catalog-app[bot]')\nprint('token:' + ' '.join(sys.argv[2:]))\n")
        fake_gh = root / "gh"
        write(fake_gh, "#!/bin/sh\nprintf '%s\\n' \"$GH_TOKEN\"\n")
        repo = "repos/second-owner/site/issues/1"
        query = "-fquery=query { viewer { login } }"
        # The method and field parsing itself is covered in test_github_api.py;
        # here a write must reach the App helper with --require-installation and a read without it.
        cases = {
            (repo, "-iXGET", "-iXDELETE"): True,
            ("--jq", "graphql", repo, "-fname=x", query): True,
            (repo, "-XDELETE", "-XGET"): False,
            ("--jq", "repos/x/y", repo): False,
        }
        for args, is_write in cases.items():
            result = run_wrapper(env_file, unused, identity, "api", *args, gh_command=fake_gh)
            assert result.returncode == 0, (args, result.stderr)
            installation = " --require-installation" if is_write else ""
            assert result.stdout == f"token:--repo second-owner/site{installation}\n", (args, result.stdout)


def test_write_to_another_owners_repository_without_installation_runs_as_the_person_on_opt_in() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file = root / "local.env"
        env_file.write_text(
            "GITHUB_APP_ID=12345\n"
            "GITHUB_APP_INSTALLATION_ID=67890\n"
            "GITHUB_APP_PRIVATE_KEY_PATH=/fake/app.pem\n"
            "CODEX_AUTOMATION_LOGIN='catalog-app[bot]'\nGH_WITH_ENV_TOKEN_OWN_USER_LOGIN=contributor-login\n",
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

        configured = env_file.read_text()
        for declaration in ("", "another-person", "cellmechanic-code-bot", "catalog-app[bot]"):
            env_file.write_text(configured.replace(
                "GH_WITH_ENV_TOKEN_OWN_USER_LOGIN=contributor-login\n",
                f"GH_WITH_ENV_TOKEN_OWN_USER_LOGIN={declaration}\n"))
            refused = run_wrapper(env_file, unused, identity, *write_args,
                                  gh_command=fake_gh, extra_env=opt_in)
            assert refused.returncode != 0 and not refused.stdout, refused
            assert "acting as your own GitHub user" not in refused.stderr
        # GitHub account names are case insensitive. Local configuration wins
        # over an ambient declaration, just as it does for the App login.
        env_file.write_text(configured.replace("=contributor-login", "=CONTRIBUTOR-login"))
        allowed = run_wrapper(env_file, unused, identity, *write_args,
                              gh_command=fake_gh, extra_env={**opt_in,
                              "GH_WITH_ENV_TOKEN_OWN_USER_LOGIN": "another-person"})
        assert allowed.returncode == 0, allowed.stderr
        env_file.write_text(configured)

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


def own_user_account_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    env_file = root / "local.env"
    env_file.write_text(
        "GITHUB_APP_ID=12345\n"
        "GITHUB_APP_INSTALLATION_ID=67890\n"
        "GITHUB_APP_PRIVATE_KEY_PATH=/fake/app.pem\n"
        "CODEX_AUTOMATION_LOGIN='catalog-app[bot]'\nGH_WITH_ENV_TOKEN_OWN_USER_LOGIN=contributor-login\n",
        encoding="utf-8",
    )
    # The App works for every repository, as on a public repository or one
    # with an installation; only the person's account can answer these.
    identity = root / "identity.py"
    write(identity, fake_app_identity())
    classifier = root / "classifier.py"
    write(classifier, "raise SystemExit(1)\n")
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
    return env_file, identity, classifier, fake_gh


def test_user_scoped_read_runs_as_the_person_on_opt_in() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env_file, identity, classifier, fake_gh = own_user_account_fixture(Path(directory))
        opt_in = {"GH_WITH_ENV_TOKEN_OWN_USER": "1"}

        for args in (
            ("api", "repos/energee/node-appletv-remote/subscription"),
            ("api", "/user/repos?per_page=1", "--jq", ".[].name"),
            ("api", "notifications"),
            ("api", "--method", "GET", "notifications/threads/1"),
            ("api", "-iX", "GET", "notifications"),
            ("api", "https://api.github.com/user/repos"),
            ("api", "user/repos", "-i=false"),
            ("api", "user/repos", "--input="),
            ("api", "user/repos", "-X", ""),
            ("api", "/users/contributor-login/settings/billing/usage?year=2026&month=10"),
            ("api", "users/Contributor-Login/settings/billing/usage/summary"),
        ):
            result = run_wrapper(env_file, classifier, identity, *args, gh_command=fake_gh, extra_env=opt_in)
            assert result.returncode == 0, (args, result.stderr)
            assert result.stdout == "ran-with-token:active-login\n", args
            assert "acting as your own GitHub user on the user-scoped endpoint" in result.stderr, args

            # Without the opt-in the read keeps the App, as before.
            default = run_wrapper(env_file, classifier, identity, *args, gh_command=fake_gh)
            assert default.returncode == 0, (args, default.stderr)
            assert default.stdout == "ran-with-token:installation-token\n", args

        # Repository reads, the bare actor probe and writes keep the App.
        for args in (
            ("api", "repos/energee/node-appletv-remote/contents/subscription"),
            ("api", "repos/energee/node-appletv-remote"),
            ("api", "user"),
            ("api", "users/another-person/settings/billing/usage"),
            ("api", "users/contributor-login/repos"),
        ):
            result = run_wrapper(env_file, classifier, identity, *args, gh_command=fake_gh, extra_env=opt_in)
            assert result.returncode == 0, (args, result.stderr)
            if args[-1] == "user":
                assert json.loads(result.stdout)["login"] == "catalog-app[bot]", result.stdout
            else:
                assert result.stdout == "ran-with-token:installation-token\n", (args, result.stdout)
            assert "acting as your own GitHub user" not in result.stderr, args

        # A write never takes the read route, however gh's short flags are
        # grouped (-fname=x is -f name=x; -iXPOST is -i -X POST).
        for args in (
            ("api", "user/repos", "-fname=example"),
            ("api", "-iXPOST", "user/repos"),
        ):
            result = run_wrapper(env_file, classifier, identity, *args, gh_command=fake_gh, extra_env=opt_in)
            assert result.returncode == 0, (args, result.stderr)
            assert result.stdout == "ran-with-token:installation-token\n", args
            assert "acting as your own GitHub user" not in result.stderr, args

        # Only the github.com API host names a user-scoped path.
        other_host = run_wrapper(env_file, classifier, identity, "api", "https://api.example.com/user/repos",
                                 gh_command=fake_gh, extra_env=opt_in)
        assert other_host.stdout == "ran-with-token:installation-token\n", other_host.stderr
        assert "acting as your own GitHub user" not in other_host.stderr

        # The declared-login check applies, and required automation auth wins.
        configured = env_file.read_text()
        env_file.write_text(configured.replace("=contributor-login", "=another-person"))
        refused = run_wrapper(env_file, classifier, identity, "api", "notifications",
                              gh_command=fake_gh, extra_env=opt_in)
        assert refused.returncode != 0 and not refused.stdout, refused
        env_file.write_text(configured + "GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH=1\n")
        required = run_wrapper(env_file, classifier, identity, "api", "notifications",
                               gh_command=fake_gh, extra_env=opt_in)
        assert required.stdout == "ran-with-token:installation-token\n", required.stderr
        assert "acting as your own GitHub user" not in required.stderr


def test_personal_fork_runs_as_the_person_on_opt_in() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env_file, identity, classifier, fake_gh = own_user_account_fixture(Path(directory))
        opt_in = {"GH_WITH_ENV_TOKEN_OWN_USER": "1"}

        for args, source in (
            (("repo", "fork", "zabuldon/teslajsonpy", "--clone=false", "--default-branch-only"),
             "zabuldon/teslajsonpy"),
            (("repo", "fork", "--fork-name", "renamed", "https://github.com/zabuldon/teslajsonpy"),
             "zabuldon/teslajsonpy"),
            (("repo", "fork", "--clone=false"), "the current repository"),
        ):
            result = run_wrapper(env_file, classifier, identity, *args, gh_command=fake_gh, extra_env=opt_in)
            assert result.returncode == 0, (args, result.stderr)
            assert result.stdout == "ran-with-token:active-login\n", args
            assert f"acting as your own GitHub user on a fork of {source}" in result.stderr, args

        # A fork into an organization is not the person's account.
        org_fork = run_wrapper(env_file, classifier, identity,
                               "repo", "fork", "zabuldon/teslajsonpy", "--org", "example-org",
                               gh_command=fake_gh, extra_env=opt_in)
        assert org_fork.stdout == "ran-with-token:installation-token\n", org_fork.stderr
        assert "acting as your own GitHub user" not in org_fork.stderr

        # Without the opt-in the fork never runs as the person.
        default = run_wrapper(env_file, classifier, identity,
                              "repo", "fork", "zabuldon/teslajsonpy", "--clone=false", gh_command=fake_gh)
        assert default.stdout == "ran-with-token:installation-token\n", default.stderr
        assert "acting as your own GitHub user" not in default.stderr


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
        sys.path.insert(0, str(SCRIPT.parent))
        import github_api
        import github_request_usage
        status, headers, _ = github_api.parse_gh_include_output(result.stdout)
        with patch.dict(os.environ, {"GITHUB_RETRY_STATE_DIR": str(root / "retry")}):
            github_request_usage.record_response(method="GET", path="/user", status=status,
                                                headers=headers, operation="github.actor",
                                                actor="catalog-app[bot]")
            assert github_request_usage.report(since=0)["consumers"] == []
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


def test_write_actor_probe_preserves_repository_authorization() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file = root / "local.env"
        env_file.write_text(
            "GITHUB_APP_ID=12345\nGITHUB_APP_INSTALLATION_ID=67890\n"
            "GITHUB_APP_PRIVATE_KEY_PATH=/fake/app.pem\nCODEX_AUTOMATION_LOGIN='catalog-app[bot]'\nGH_WITH_ENV_TOKEN_OWN_USER_LOGIN=contributor-login\n"
        )
        identity = root / "identity.py"
        write(identity,
              "import sys\n"
              "repo = sys.argv[sys.argv.index('--repo') + 1] if '--repo' in sys.argv else ''\n"
              "if '--require-installation' in sys.argv and repo == 'director/catalog':\n"
              "    raise SystemExit(3)\n"
              "if '--require-installation' in sys.argv and repo == 'registrar/missing':\n"
              "    print('refused missing installation in registering account', file=sys.stderr)\n"
              "    raise SystemExit(4)\n"
              "print('catalog-app[bot]')\nprint('installation-token')\n")
        unused = root / "unused.py"
        write(unused, "raise AssertionError('must not delegate a write or App /user probe')\n")
        fake_gh = root / "gh"
        write(fake_gh,
              "#!/bin/sh\n"
              "[ -z \"${GH_TOKEN:-}${GITHUB_TOKEN:-}${CODEX_GITHUB_TOKEN:-}\" ] || exit 42\n"
              "case \"$*\" in\n"
              "'api user --jq .login') echo contributor-login ;;\n"
              "'api --include /user --method GET') printf 'HTTP/2.0 200\\r\\n\\r\\n{\"login\":\"contributor-login\"}\\n' ;;\n"
              "*) exit 43 ;;\nesac\n")
        args = ("--write-actor-for", "director/catalog", "api", "--include", "/user", "--method", "GET")
        result = run_wrapper(env_file, unused, identity, *args, gh_command=fake_gh,
                             extra_env={"GH_WITH_ENV_TOKEN_OWN_USER": "1", "GH_TOKEN": "ambient-token"})
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.split("\n\n", 1)[1])["login"] == "contributor-login", result.stdout
        assert "acting as your own GitHub user on director/catalog" in result.stderr
        for opt_in, diagnostic in (
            ({}, "GH_WITH_ENV_TOKEN_OWN_USER=1"),
            ({"GH_WITH_ENV_TOKEN_OWN_USER": "1", "GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH": "1"},
             "requires configured automation authentication"),
        ):
            refused = run_wrapper(env_file, unused, identity, *args, gh_command=unused, extra_env=opt_in)
            assert refused.returncode != 0 and not refused.stdout, refused
            assert diagnostic in refused.stderr, refused.stderr
        for repo, exit_code in (("installed/catalog", 0), ("registrar/missing", 4)):
            result = run_wrapper(env_file, unused, identity, "--write-actor-for", repo, *args[2:],
                                 gh_command=unused, extra_env={"GH_WITH_ENV_TOKEN_OWN_USER": "1"})
            assert result.returncode == exit_code, result.stderr
            if exit_code == 0:
                assert json.loads(result.stdout.split("\n\n", 1)[1])["login"] == "catalog-app[bot]", result.stdout
                assert "acting as your own" not in result.stderr


def test_write_actor_probe_rejects_non_probe_commands_before_authentication() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file = root / "local.env"
        env_file.write_text(
            "GITHUB_APP_ID=12345\nGITHUB_APP_INSTALLATION_ID=67890\n"
            "GITHUB_APP_PRIVATE_KEY_PATH=/fake/app.pem\n",
            encoding="utf-8",
        )
        auth_calls = root / "auth-calls"
        unused = root / "unused.py"
        write(unused, "from pathlib import Path\n"
              f"Path({str(auth_calls)!r}).touch()\n"
              "raise AssertionError('invalid probe must fail before any authentication or command')\n")
        for args in (
            ("--write-actor-for",),
            ("--write-actor-for", "", "api", "/user"),
            ("--write-actor-for", "director/catalog", "--write-actor-for", "director/catalog", "api", "/user"),
            ("--write-actor-for", "invalid", "api", "/user"),
            ("--write-actor-for", "director/catalog", "api", "/user", "--method", "POST"),
            ("--write-actor-for", "director/catalog", "issue", "comment", "42", "--body", "x"),
            ("--write-actor-for", "director/catalog", "api", "/repos/director/catalog"),
            ("--check", "--write-actor-for", "director/catalog", "api", "/user"),
        ):
            result = run_wrapper(env_file, SCRIPT.with_name("github_api.py"), unused,
                                 *args, gh_command=unused)
            assert result.returncode == 2, result
            payload = json.loads(result.stdout)
            assert payload["failure"]["cause"] == "validation_error", payload
            assert payload["retryable"] is False and payload["fallback_eligible"] is False, payload
            assert payload["write_outcome"] == "not_started", payload
            assert payload["failed_step"] == "input_validation", payload
            assert not auth_calls.exists(), result


def test_attachment_route_enforces_limits_through_real_wrapper() -> None:
    sys.path.insert(0, str(SCRIPT.parent))
    import github_comment

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file, identity, classifier = app_fixture(root)
        owner = github_comment.ATTACHMENT_OWNER
        repo = f"{owner}/catalog"
        env_file.write_text(env_file.read_text() + f"GH_WITH_ENV_TOKEN_OWN_USER_LOGIN={owner}\n")
        image = root / "proof.png"
        image.write_bytes(b"image fixture")
        pdf = image.with_suffix(".pdf")
        pdf.write_bytes(b"%PDF-1.7")
        calls_file = root / "calls.jsonl"
        fake_gh = root / "gh"
        write(fake_gh, f"#!{sys.executable}\n" +
              "import json, os, sys\n"
              f"sys.path.insert(0, {str(SCRIPT.parent)!r})\n"
              "import github_comment\n"
              "args = sys.argv[1:]\n"
              "owner = github_comment.ATTACHMENT_OWNER\n"
              "with open(os.environ['FIXTURE_CALLS'], 'a') as stream:\n"
              "    stream.write(json.dumps([args, os.environ.get('GH_TOKEN')]) + '\\n')\n"
              "if args == ['api', 'user', '--jq', '.login']:\n"
              "    assert not os.environ.get('GH_TOKEN')\n"
              "    print(os.environ.get('FIXTURE_LOGIN', owner))\n"
              "elif args[:1] == ['help']:\n    print('--attach')\n"
              "elif args[:2] in (['issue', 'comment'], ['pr', 'comment']):\n"
              "    assert not os.environ.get('GH_TOKEN')\n"
              "    body = sys.stdin.read()\n"
              "    assert body.splitlines()[0] == github_comment.AGENT_POSTED_LINE\n"
              "    assert any(value == '--attach' for value in args)\n"
              "    print(f'https://github.com/{owner}/catalog/issues/42#issuecomment-20')\n"
              "else:\n"
              "    assert os.environ.get('GH_TOKEN') == 'installation-token'\n"
              "    path = next(arg for arg in args if arg.startswith('/'))\n"
              "    if path.endswith('/comments/20'):\n"
              "        result = {'id': 20, 'html_url': f'https://github.com/{owner}/catalog/issues/42#issuecomment-20',\n"
              "                  'user': {'login': owner}, 'body': github_comment.AGENT_POSTED_LINE + '\\n\\n![proof](https://github.com/user-attachments/assets/example)'}\n"
              "    else:\n"
              "        result = {'owner': {'login': os.environ.get('FIXTURE_OWNER', owner)}, 'full_name': f'{owner}/catalog'}\n"
              "    print('HTTP/2.0 200\\r\\ncontent-type: application/json\\r\\n\\r\\n' + json.dumps(result))\n")
        body = github_comment.AGENT_POSTED_LINE + "\n\nAttachment proof"
        args = ["issue", "comment", "42", "--repo", repo, "--body-file", "-", "--attach", str(image)]
        env = {
            "PATH": os.environ["PATH"], "HOME": directory, "CODEX_SKILLS_ENV_FILE": str(env_file),
            "GH_WITH_ENV_TOKEN_IDENTITY_HELPER": str(identity), "GH_WITH_ENV_TOKEN_PYTHON": sys.executable,
            "GH_WITH_ENV_TOKEN_GH": str(fake_gh), "GH_WITH_ENV_TOKEN_OWN_USER": "1",
            "FIXTURE_CALLS": str(calls_file), "GH_COMMENT_GH": str(SCRIPT),
            "GITHUB_RETRY_STATE_DIR": str(root / "retry-state"),
        }
        for kind in ("issue", "pr"):
            result = subprocess.run(
                [sys.executable, str(SCRIPT.with_name("github_comment.py")), kind, "42", "--repo", repo,
                 "--body-file", "-", "--attach", str(image) + "#Evidence"],
                env=env, input="Attachment proof", text=True, capture_output=True, timeout=15,
            )
            assert result.returncode == 0, (result.stdout, result.stderr)
            assert json.loads(result.stdout)["actor"] == owner
        refused = subprocess.run(
            [sys.executable, str(SCRIPT.with_name("github_comment.py")), "issue", "42", "--repo", repo,
             "--body-file", "-", "--attach", str(image)], env={**env, "FIXTURE_LOGIN": "another-person"},
            input="proof", text=True, capture_output=True, timeout=15,
        )
        assert refused.returncode != 0
        assert json.loads(refused.stdout)["write_outcome"] == "not_started", refused.stdout
        cases = [
            (args, "ordinary text", {}),
            (args, github_comment.AGENT_POSTED_LINE + "\r\n\r\nOwner question: Q123. Decide about @outside?", {}),
            ([*args[:-1], str(pdf)], body, {}),
            ([*args, "--edit-last"], body, {}),
            ([*args[:5], "--body", "replacement", *args[5:]], body, {}),
            (args, body, {"GH_WITH_ENV_TOKEN_OWN_USER": "0"}),
            (args, body, {"GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH": "1"}),
            (args, body, {"FIXTURE_LOGIN": "another-person"}),
            (args, body, {"FIXTURE_OWNER": "new-owner"}),
            ([*args[:4], "other/catalog", *args[5:]], body, {}),
        ]
        for command, requested_body, changes in cases:
            calls_file.write_text("")
            result = subprocess.run([str(SCRIPT), *command], env={**env, **changes}, input=requested_body,
                                    text=True, capture_output=True, timeout=15)
            assert result.returncode != 0, (command, changes, result.stdout)
            calls = [json.loads(line)[0] for line in calls_file.read_text().splitlines()]
            assert not any(call[:2] == ["issue", "comment"] for call in calls), calls
        original_env = env_file.read_text()
        env_file.write_text(original_env.replace(f"GH_WITH_ENV_TOKEN_OWN_USER_LOGIN={owner}",
                                                "GH_WITH_ENV_TOKEN_OWN_USER_LOGIN=another-person"))
        result = subprocess.run([str(SCRIPT), *args], env=env, input=body, text=True, capture_output=True, timeout=15)
        assert result.returncode != 0, result.stdout
        env_file.write_text(original_env + "GH_WITH_ENV_TOKEN_OWN_USER=1\n")
        result = subprocess.run([str(SCRIPT), *args], env={key: value for key, value in env.items()
                                                        if key != "GH_WITH_ENV_TOKEN_OWN_USER"},
                                input=body, text=True, capture_output=True, timeout=15)
        assert result.returncode != 0, result.stdout
        env_file.write_text(original_env)
        calls_file.write_text("")
        result = subprocess.run([str(SCRIPT), "api", "--include", "--method", "POST", f"/repos/{repo}/issues/42/comments"],
                                env=env, text=True, capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr
        calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
        assert calls[-1][1] == "installation-token", calls


def test_comment_cli_selects_own_user_through_real_wrapper() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file = root / "local.env"
        env_file.write_text(
            "GITHUB_APP_ID=12345\nGITHUB_APP_INSTALLATION_ID=67890\n"
            "GITHUB_APP_PRIVATE_KEY_PATH=/fake/app.pem\nCODEX_AUTOMATION_LOGIN='catalog-app[bot]'\nGH_WITH_ENV_TOKEN_OWN_USER_LOGIN=contributor-login\n"
        )
        identity = root / "identity.py"
        write(identity, "import os, sys\n"
              "with open(os.environ['FIXTURE_IDENTITY_CALLS'], 'a') as stream:\n"
              "    stream.write('lookup\\n')\n"
              "if '--require-installation' in sys.argv:\n    raise SystemExit(3)\n"
              "print('catalog-app[bot]')\nprint('installation-token')\n")
        fake_gh = root / "gh"
        write(fake_gh, f"#!{sys.executable}\n"
              "import json, os, sys\n"
              "args = sys.argv[1:]\n"
              "if args == ['api', 'user', '--jq', '.login']:\n"
              "    assert not os.environ.get('GH_TOKEN')\n"
              "    print('contributor-login')\n    raise SystemExit()\n"
              "method = args[args.index('--method') + 1]\n"
              "path = next(arg for arg in args if arg.startswith('/'))\n"
              "with open(os.environ['FIXTURE_CALLS'], 'a') as stream:\n"
              "    stream.write(json.dumps([method, path]) + '\\n')\n"
              "item = {'id': 20, 'user': {'login': 'contributor-login'}, 'body': 'replacement',\n"
              "'html_url': 'https://github.com/director/catalog/issues/42#issuecomment-20',\n"
              "'issue_url': 'https://api.github.com/repos/director/catalog/issues/42'}\n"
              "if path == '/user':\n"
              "    assert not os.environ.get('GH_TOKEN')\n"
              "    result = {'login': 'contributor-login'}\n"
              "elif method == 'GET' and '/issues/42/comments?' in path:\n"
              "    assert os.environ.get('GH_TOKEN') == 'installation-token'\n"
              "    result = [dict(item, id=30, user={'login': 'catalog-app[bot]'}), item]\n"
              "elif method == 'GET':\n    result = item\n"
              "else:\n"
              "    assert method == 'PATCH' and path.endswith('/comments/20')\n"
              "    assert not os.environ.get('GH_TOKEN')\n"
              "    assert json.load(sys.stdin)['body'] == 'replacement'\n"
              "    result = item\n"
              "print('HTTP/2.0 200\\r\\n' + 'content-type: application/json\\r\\n\\r\\n' + json.dumps(result))\n")
        body_file = root / "body.md"
        body_file.write_text("replacement")
        calls_file = root / "calls.jsonl"
        env = {
            "PATH": os.environ["PATH"], "HOME": directory,
            "CODEX_SKILLS_ENV_FILE": str(env_file),
            "GH_WITH_ENV_TOKEN_IDENTITY_HELPER": str(identity),
            "GH_WITH_ENV_TOKEN_PYTHON": sys.executable,
            "GH_WITH_ENV_TOKEN_GH": str(fake_gh),
            "GH_WITH_ENV_TOKEN_OWN_USER": "1", "FIXTURE_CALLS": str(calls_file),
            "GH_COMMENT_GH": str(SCRIPT),
            "GITHUB_RETRY_STATE_DIR": str(root / "retry-state"),
            "FIXTURE_IDENTITY_CALLS": str(root / "identity-calls.log"),
        }
        for options in (("--edit-last", "--create-if-none"), ("--edit-comment", "20")):
            calls_file.write_text("")
            result = subprocess.run(
                [sys.executable, str(SCRIPT.with_name("github_comment.py")), "issue", "42",
                 "--repo", "director/catalog", "--body-file", str(body_file), *options],
                env=env, text=True, capture_output=True, timeout=10,
            )
            assert result.returncode == 0, (result.stdout, result.stderr)
            payload = json.loads(result.stdout)
            assert payload["actor"] == payload["expected_actor"] == "contributor-login", payload
            assert payload["comment"]["id"] == 20, payload
            calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
            assert calls[-1] == ["PATCH", "/repos/director/catalog/issues/comments/20"], calls
            assert not any(method == "POST" for method, _path in calls), calls
        for refusal in ({"GH_WITH_ENV_TOKEN_OWN_USER": "0"},
                        {"GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH": "1"}):
            identity_calls = root / "identity-calls.log"
            identity_calls.write_text("")
            calls_file.write_text("")
            refused_env = {**env, **refusal, "GITHUB_RETRY_MAX_ATTEMPTS": "2",
                           "GITHUB_RETRY_MAX_WAIT_SECONDS": "5"}
            result = subprocess.run(
                [sys.executable, str(SCRIPT.with_name("github_comment.py")), "issue", "42",
                 "--repo", "director/catalog", "--body-file", str(body_file), "--edit-last"],
                env=refused_env, text=True, capture_output=True, timeout=10,
            )
            assert result.returncode != 0, result.stdout
            payload = json.loads(result.stdout)
            assert payload["error_code"] == "permission_denied", payload
            assert payload["retry_eligible"] is False, payload
            assert payload["retryable"] is False, payload
            assert payload["write_outcome"] == "not_started", payload
            assert payload["attempts"] == 1, payload
            assert identity_calls.read_text().splitlines() == ["lookup"], identity_calls.read_text()
            assert calls_file.read_text() == "", calls_file.read_text()


def test_comment_cli_rejects_malformed_probe_once_before_authentication() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        auth_calls = root / "auth-calls"
        unused = root / "unused"
        write(unused, f"#!{sys.executable}\nfrom pathlib import Path\n"
              f"Path({str(auth_calls)!r}).touch()\nraise SystemExit(99)\n")
        wrapper_calls = root / "wrapper-calls"
        launcher = root / "wrapper"
        write(launcher, f"#!/bin/sh\nprintf 'probe\\n' >> '{wrapper_calls}'\n"
              f"exec '{SCRIPT}' \"$@\"\n")
        env_file = root / "local.env"
        env_file.write_text(
            "GITHUB_APP_ID=12345\nGITHUB_APP_INSTALLATION_ID=67890\n"
            "GITHUB_APP_PRIVATE_KEY_PATH=/fake/app.pem\n"
            "CODEX_AUTOMATION_LOGIN='catalog-app[bot]'\n",
            encoding="utf-8",
        )
        body_file = root / "body.md"
        body_file.write_text("fixture comment", encoding="utf-8")
        cli = root / "comment-cli.py"
        # Control retry time while retaining the real wrapper and response parser.
        # The transport/outer timeouts are hang guards, separate from the virtual deadline.
        write(cli,
              "import sys\n"
              "from unittest.mock import patch\n"
              f"sys.path.insert(0, {str(SCRIPT.parent)!r})\n"
              "import github_api, github_comment\n"
              "def unexpected_sleep(duration):\n"
              "    raise AssertionError('malformed probe must not sleep or retry')\n"
              "runtime = github_api.RetryRuntime(now=lambda: 1000.0, sleep=unexpected_sleep)\n"
              "real_transport = github_api.call_gh\n"
              "def transport(*args, **kwargs):\n"
              "    kwargs['timeout_seconds'] = 30\n"
              "    return real_transport(*args, **kwargs)\n"
              "with patch.object(github_api, 'default_retry_runtime', return_value=runtime), "
              "patch.object(github_api, 'call_gh', side_effect=transport):\n"
              "    raise SystemExit(github_comment.main())\n")
        env = {
            "PATH": os.environ["PATH"], "HOME": directory,
            "CODEX_SKILLS_ENV_FILE": str(env_file),
            "GH_WITH_ENV_TOKEN_IDENTITY_HELPER": str(unused),
            "GH_WITH_ENV_TOKEN_PYTHON": sys.executable,
            "GH_WITH_ENV_TOKEN_GH": str(unused),
            "GH_COMMENT_GH": str(launcher),
            "GITHUB_RETRY_STATE_DIR": str(root / "retry-state"),
            "GITHUB_RETRY_MAX_ATTEMPTS": "2",
            "GITHUB_RETRY_MAX_WAIT_SECONDS": "5",
        }
        for repository in ("fixture/repo!", "fixture/..", "-fixture/repo"):
            wrapper_calls.write_text("", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(cli), "issue", "42",
                 f"--repo={repository}", "--body-file", str(body_file)],
                env=env, text=True, capture_output=True, timeout=60,
            )
            assert result.returncode != 0, result.stdout
            payload = json.loads(result.stdout)
            assert payload["error_code"] == "validation_error", payload
            assert payload["retryable"] is False and payload["retry_eligible"] is False, payload
            assert payload["attempts"] == 1, payload
            assert payload["elapsed_wait"] == 0.0, payload
            assert payload["write_outcome"] == "not_started", payload
            assert wrapper_calls.read_text().splitlines() == ["probe"], result
            assert not auth_calls.exists(), result


def test_invalid_probe_does_not_wait_for_open_stdin() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        unused = root / "unused.py"
        write(unused, "raise AssertionError('invalid probe must fail before authentication')\n")
        env = {
            "PATH": os.environ["PATH"], "HOME": directory,
            "CODEX_SKILLS_ENV_FILE": str(root / "missing.env"),
            "GH_WITH_ENV_TOKEN_IDENTITY_HELPER": str(unused),
            "GH_WITH_ENV_TOKEN_PYTHON": sys.executable,
            "GH_WITH_ENV_TOKEN_GH": str(unused),
        }
        with subprocess.Popen(
            [str(SCRIPT), "--write-actor-for", "fixture/repo", "api", "/user", "--input", "-"],
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        ) as process:
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            try:
                process.wait(timeout=5)
            finally:
                process.stdin.close()
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
            stdout, stderr = process.stdout.read(), process.stderr.read()
            assert process.returncode == 2, (stdout, stderr)
            assert json.loads(stdout)["failure"]["cause"] == "validation_error", stdout


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
        env_file, identity, unused = app_fixture(root)
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
        env_file, identity, unused = app_fixture(root)
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


def test_pr_create_validation_refusal_preserves_unknown_write_controls() -> None:
    refusal = (
        "pull request create failed: GraphQL: Head sha can't be blank, "
        "Base sha can't be blank, No commits between main and work/fixture, "
        "Head ref must be a branch (createPullRequest)"
    )
    cases = [
        ("could not add label: 'owner-review' not found", "", "validation_error", "not_started"),
        ("could not add label: 'timeout' not found", "", "validation_error", "not_started"),
        ("could not add label: 'owner-review' not found\nconnection reset by peer", "", "network_provider_failure", "unknown"),
        ("could not add label: 'owner-review' not found", "https://github.com/director/catalog/pull/9\n", "network_provider_failure", "unknown"),
        ("pull request create failed: could not add label: 'owner-review' not found", "", "network_provider_failure", "unknown"),
        ("could not add label: 'owner-review' not found\ncontext deadline exceeded", "", "deadline_exceeded", "unknown"),
        (refusal, "", "validation_error", "rejected"),
        (refusal.replace("work/fixture", "fix/retry-after-header"), "", "validation_error", "rejected"),
        (refusal.replace("work/fixture", "fix/rate_limited"), "", "validation_error", "rejected"),
        (refusal.replace("work/fixture", "fix/timeout"), "", "validation_error", "rejected"),
        ("HTTP 503: Service Unavailable", "", "network_provider_failure", "unknown"),
        (refusal + "\nconnection reset by peer", "", "network_provider_failure", "unknown"),
        (refusal, "https://github.com/director/catalog/pull/9\n", "network_provider_failure", "unknown"),
        (refusal.replace(" (createPullRequest)", ""), "", "network_provider_failure", "unknown"),
        (refusal.replace("Head ref must be a branch", "Internal Server Error"), "", "network_provider_failure", "unknown"),
        (refusal + "\ncontext deadline exceeded", "", "deadline_exceeded", "unknown"),
    ]
    for stderr, stdout, cause, outcome in cases:
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
            calls = root / "calls.jsonl"
            fake_gh = root / "gh"
            write(
                fake_gh,
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                f"with open({str(calls)!r}, 'a') as stream:\n"
                "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "assert os.environ['GH_TOKEN'] == 'installation-token'\n"
                "assert sys.argv[1:3] == ['pr', 'create']\n"
                f"sys.stdout.write({stdout!r})\n"
                f"sys.stderr.write({stderr!r} + '\\n')\n"
                "sys.exit(1)\n",
            )
            body = root / "body.md"
            body.write_text("Fixture body", encoding="utf-8")
            env = {
                "PATH": os.environ["PATH"],
                "HOME": str(root),
                "CODEX_SKILLS_ENV_FILE": str(env_file),
                "GH_PR_GH": str(SCRIPT),
                "GH_WITH_ENV_TOKEN_GH": str(fake_gh),
                "GH_WITH_ENV_TOKEN_CLASSIFIER": str(SCRIPT.with_name("github_api.py")),
                "GH_WITH_ENV_TOKEN_IDENTITY_HELPER": str(identity),
                "GH_WITH_ENV_TOKEN_PYTHON": sys.executable,
                "GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK": "1",
                "GITHUB_RETRY_STATE_DIR": str(root / "retry"),
            }
            result = subprocess.run(
                [sys.executable, str(SCRIPT.with_name("gh-pr.py")), "--repo", "director/catalog",
                 "create", "--title", "fixture", "--body-file", str(body),
                 "--base", "main", "--head", "work/fixture"],
                env=env, text=True, capture_output=True, timeout=30,
            )
            assert result.returncode == 1, result
            payload = json.loads(result.stdout)
            assert payload["failure"]["cause"] == cause, payload
            assert payload["write_outcome"] == outcome, payload
            assert payload["outcome_certainty"] == ("confirmed_not_applied" if outcome in {"rejected", "not_started"} else "unknown"), payload
            assert payload["fallback_eligible"] is False, payload
            assert payload["retry_eligible"] is False, payload
            assert payload["retryable"] is False, payload
            assert payload["attempts"] == 1, payload
            assert payload["actor"] == "catalog-app[bot]", payload
            if outcome == "unknown":
                assert payload["recommended_next_action"] == "follow_operation_reconciliation_strategy", payload
            assert len(calls.read_text(encoding="utf-8").splitlines()) == 1


def test_pr_edit_missing_label_stops_without_claiming_other_edits_did_not_apply() -> None:
    for edit_flags in (
        ["--add-label", "owner-review"],
        ["--title", "New title", "--add-label", "owner-review"],
        ["--remove-label", "in-progress", "--add-label", "owner-review"],
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file, identity, _ = app_fixture(root)
            calls = root / "calls.jsonl"
            fake_gh = root / "gh"
            write(
                fake_gh,
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                f"with open({str(calls)!r}, 'a') as stream:\n"
                "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "assert os.environ['GH_TOKEN'] == 'installation-token'\n"
                "assert sys.argv[1:3] == ['pr', 'edit']\n"
                "sys.stderr.buffer.write(b\"'owner-review' not found\\n\")\n"
                "sys.exit(1)\n",
            )
            env = {
                "PATH": os.environ["PATH"],
                "HOME": str(root),
                "CODEX_SKILLS_ENV_FILE": str(env_file),
                "GH_PR_GH": str(SCRIPT),
                "GH_WITH_ENV_TOKEN_GH": str(fake_gh),
                "GH_WITH_ENV_TOKEN_CLASSIFIER": str(SCRIPT.with_name("github_api.py")),
                "GH_WITH_ENV_TOKEN_IDENTITY_HELPER": str(identity),
                "GH_WITH_ENV_TOKEN_PYTHON": sys.executable,
                "GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK": "1",
                "GITHUB_RETRY_STATE_DIR": str(root / "retry"),
                "GITHUB_RETRY_MAX_ATTEMPTS": "3",
            }
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT.with_name("gh-pr.py")),
                    "--repo",
                    "director/catalog",
                    "edit",
                    "42",
                    *edit_flags,
                ],
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            assert result.returncode == 1, result
            payload = json.loads(result.stdout)
            assert payload["failure"]["cause"] == "validation_error", payload
            assert payload["write_outcome"] == "unknown", payload
            assert payload["outcome_certainty"] == "unknown", payload
            assert payload["fallback_eligible"] is False, payload
            assert payload["attempts"] == 1, payload
            assert len(calls.read_text().splitlines()) == 1


def test_permission_refusals_preserve_app_identity() -> None:
    cases = [
        (("repo", "create", "cbusillo/fixture", "--private"), "",
         "GraphQL: Resource not accessible by integration (createRepository)"),
        (("api", "-X", "POST", "user/repos"),
         json.dumps({"message": "Resource not accessible by integration"}),
         "gh: Resource not accessible by integration (HTTP 403)"),
        (("api", "--include", "-X", "POST", "user/repos"),
         'HTTP/2.0 403\nContent-Type: application/json\nX-RateLimit-Remaining: 5000\n\n'
         + json.dumps({"message": "Resource not accessible by integration"}), ""),
    ]
    for command, stdout, stderr in cases:
        for fallback in ("0", "1"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                env_file, identity, _ = app_fixture(root)
                calls = root / "calls.jsonl"
                fake_gh = root / "gh"
                write(fake_gh,
                      f"#!{sys.executable}\nimport json, os, sys\n"
                      f"with open({str(calls)!r}, 'a') as stream:\n"
                      "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                      "assert os.environ.get('GH_TOKEN') == 'installation-token'\n"
                      f"sys.stdout.write({stdout!r})\nsys.stderr.write({stderr!r})\nsys.exit(1)\n")
                result = run_wrapper(
                    env_file, SCRIPT.with_name("github_api.py"), identity, *command,
                    gh_command=fake_gh,
                    extra_env={"GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK": fallback},
                )
                assert result.returncode == 1, result
                assert [json.loads(line) for line in calls.read_text().splitlines()] == [list(command)]
                assert result.stderr, result
                assert "installation-token" not in result.stdout + result.stderr, result


def test_prewrite_receipt_requires_nonce_and_stops_at_launch() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file, identity, unused = app_fixture(root)
        write(identity, fake_app_identity("other-fixture"))
        fake_gh = root / "gh"
        write(fake_gh, f"#!{sys.executable}\nimport json,os,sys\n"
              "print(json.dumps({'nonce_inherited': 'GH_WITH_ENV_TOKEN_RECEIPT_NONCE' in os.environ}))\n"
              "sys.exit(1)\n")
        nonce = os.urandom(16).hex()
        def receipts(completed: subprocess.CompletedProcess[str]) -> list[dict]:
            found = []
            for line in completed.stderr.splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and item.get("write_outcome") == "not_started":
                    found.append(item)
            return found
        refused = run_wrapper(env_file, unused, identity, "run", "rerun", "1",
                              gh_command=fake_gh, extra_env={"GH_WITH_ENV_TOKEN_RECEIPT_NONCE": nonce})
        assert refused.returncode != 0
        assert refused.stdout == ""
        assert [item["nonce"] for item in receipts(refused)] == [nonce]
        for invalid in ("", "short", "g" * len(nonce)):
            result = run_wrapper(env_file, unused, identity, "run", "rerun", "1",
                                 gh_command=fake_gh, extra_env={"GH_WITH_ENV_TOKEN_RECEIPT_NONCE": invalid})
            assert result.returncode != 0
            assert receipts(result) == []
        read = run_wrapper(env_file, unused, identity, "run", "view", "1",
                           gh_command=fake_gh, extra_env={"GH_WITH_ENV_TOKEN_RECEIPT_NONCE": nonce})
        assert receipts(read) == []
        assert json.loads(read.stdout)["nonce_inherited"] is False
        write(identity, fake_app_identity())
        launched = run_wrapper(env_file, unused, identity, "run", "rerun", "1",
                               gh_command=fake_gh, extra_env={"GH_WITH_ENV_TOKEN_RECEIPT_NONCE": nonce})
        assert launched.returncode != 0
        assert receipts(launched) == []
        assert json.loads(launched.stdout)["nonce_inherited"] is False


def main() -> None:
    tests: list[Callable[[], None]] = [
        test_search_archive_scope_through_wrapper,
        test_check_reports_app_identity_and_source,
        test_write_actor_probe_preserves_repository_authorization,
        test_write_actor_probe_rejects_non_probe_commands_before_authentication,
        test_attachment_route_enforces_limits_through_real_wrapper,
        test_comment_cli_selects_own_user_through_real_wrapper,
        test_comment_cli_rejects_malformed_probe_once_before_authentication,
        test_invalid_probe_does_not_wait_for_open_stdin,
        test_check_preserves_user_token_fallback_when_app_is_absent,
        test_user_token_write_verifies_actor_before_running_gh,
        test_user_token_write_without_expected_login_refuses_before_actor_probe,
        test_user_token_expected_login_override_takes_precedence,
        test_user_token_get_and_head_delegate_without_actor_verification,
        test_no_token_check_fails_explicitly_without_running_gh,
        test_check_rejects_commands_before_active_auth_fallback,
        test_no_token_command_preserves_authorized_active_auth_fallback,
        test_partial_app_configuration_fails_closed,
        test_app_auth_failure_never_falls_back_to_active_user,
        test_empty_app_auth_response_fails_closed,
        test_app_auth_takes_precedence_and_runs_write_as_verified_app,
        test_app_installation_follows_the_target_repository,
        test_api_write_checks_read_arguments_as_gh_does,
        test_write_to_another_owners_repository_without_installation_runs_as_the_person_on_opt_in,
        test_user_scoped_read_runs_as_the_person_on_opt_in,
        test_personal_fork_runs_as_the_person_on_opt_in,
        test_app_actor_probe_synthesizes_include_response_without_user_endpoint,
        test_app_login_mismatch_fails_closed_for_write_and_check,
        test_app_login_comparison_is_case_insensitive,
        test_print_auth_account_reports_verified_app_without_gh_auth_status,
        test_app_actor_probe_does_not_fabricate_other_paths_or_writes,
        test_pr_create_validation_refusal_preserves_unknown_write_controls,
        test_pr_edit_missing_label_stops_without_claiming_other_edits_did_not_apply,
        test_permission_refusals_preserve_app_identity,
        test_prewrite_receipt_requires_nonce_and_stops_at_launch,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
