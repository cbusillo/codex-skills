#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline two-App routing through real credential, cache and wrapper paths."""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

import github_api
import github_comment
import github_identity as identity
import github_issue
import github_milestone
from test_github_identity import test_private_key
from test_gh_plan_sections import PLAN


class PlanCommandContextTests(unittest.TestCase):
    def run_context(self, argv: tuple[str, ...], checkout: str | None, *,
                    inherited: str | None = None, fail: bool = False) -> tuple[list[str | None], Any]:
        args = PLAN.build_parser().parse_args(argv)
        observed = []

        def command(_args: Any) -> None:
            observed.append(os.environ.get("GH_REPO"))
            if fail:
                raise PLAN.PlanError("Synthetic command failure")

        args.func = command
        env = {"GH_REPO": inherited} if inherited is not None else {}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(PLAN, "repo_from_git", return_value=checkout) as git_repo, \
                patch.object(PLAN, "build_parser") as parser, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            parser.return_value.parse_args.return_value = args
            if fail:
                with self.assertRaises(SystemExit):
                    PLAN.main()
            else:
                PLAN.main()
            self.assertEqual(os.environ.get("GH_REPO"), inherited)
        return observed, git_repo

    def test_repository_commands_bind_inferred_context_before_dispatch(self) -> None:
        commands = (
            ("index",), ("search", "fixture"), ("search", '"user:fixture"'),
            ("next",), ("wait", "--all"), ("create", "fixture"), ("ensure-labels",),
            ("reconcile-landing", "--pr", "1"), ("milestone-list",), ("milestone-show", "1"),
            ("milestone-create", "fixture"), ("milestone-update", "1", "--title", "fixture"),
            ("milestone-close", "1"),
        )
        for command in commands:
            for inherited in (None, "client/product"):
                with self.subTest(command=command, inherited=inherited):
                    observed, _ = self.run_context(command, "host/product", inherited=inherited)
                    self.assertEqual(observed, ["host/product"])

    def test_owner_and_global_commands_preserve_scope_without_requiring_checkout(self) -> None:
        for command, checkout in (
            (("project-list", "--owner", "host"), None),
            (("search", "repo:host/product fixture"), None),
            (("search", "user:host fixture"), "client/product"),
            (("search", "org:host fixture"), None),
            (("next",), "host/direction"),
        ):
            for inherited in (None, "client/product"):
                with self.subTest(command=command, checkout=checkout, inherited=inherited):
                    observed, git_repo = self.run_context(command, checkout, inherited=inherited)
                    self.assertEqual(observed, [inherited])
                    if command[0] != "next":
                        git_repo.assert_not_called()

    def test_explicit_repository_still_binds_global_commands(self) -> None:
        for command in (("search", "user:host fixture"), ("next",),
                        ("project-list", "--owner", "host")):
            with self.subTest(command=command):
                observed, git_repo = self.run_context(("--repo", "host/direction", *command), None)
                self.assertEqual(observed, ["host/direction"])
                git_repo.assert_not_called()

    def test_failed_command_restores_inherited_context(self) -> None:
        for inherited in (None, "client/product"):
            with self.subTest(inherited=inherited):
                observed, _ = self.run_context(("index",), "host/product", inherited=inherited, fail=True)
                self.assertEqual(observed, ["host/product"])


class AppHandler(BaseHTTPRequestHandler):
    calls: list[tuple[str, str, str]] = []

    def respond(self, method: str) -> None:
        if method == "POST":
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
        jwt = self.headers["Authorization"].split()[1].split(".")[1]
        app = json.loads(base64.urlsafe_b64decode(jwt + "=" * (-len(jwt) % 4)))["iss"]
        type(self).calls.append((method, self.path, app))
        payload = None
        if self.path == "/app":
            payload = {"slug": f"app-{app}", "owner": {"login": "client"}}
        elif self.path.startswith("/app/installations/") and method == "GET":
            payload = {"id": int(app) + 100, "app_id": int(app), "app_slug": f"app-{app}"}
        elif self.path.startswith("/repos/") and self.path.endswith("/installation"):
            if "/missing/" not in self.path:
                payload = {"id": int(app) + 100, "app_id": int(app)}
        elif method == "POST":
            payload = {"token": f"fake-token-{app}", "expires_at": datetime.fromtimestamp(
                time.time() + 3600, UTC).isoformat()}
        body = json.dumps(payload).encode() if payload else b""
        self.send_response(200 if payload else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self.respond("GET")

    def do_POST(self) -> None:  # noqa: N802
        self.respond("POST")

    def log_message(self, *args: object, **kwargs: object) -> None:
        pass


def app_handler(request: Any, address: Any, server: ThreadingHTTPServer) -> AppHandler:
    return AppHandler(request, address, server)


class ClientAppTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        key = self.root / "synthetic.pem"
        key.write_text(test_private_key())
        key.chmod(0o600)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app_handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        AppHandler.calls = []
        self.env = {
            "HOME": str(self.root), "PATH": os.environ["PATH"],
            "CODEX_SKILLS_ENV_FILE": str(self.root / "missing.env"),
            "CODEX_AUTOMATION_LOGIN": "app-1[bot]",
            "GITHUB_CLIENT_APP_OWNERS": "host, SECOND-HOST",
            "GITHUB_RETRY_STATE_DIR": str(self.root / "retry"),
        }
        for prefix, app in (("GITHUB_APP", "1"), ("GITHUB_CLIENT_APP", "2"), ("GITHUB_READER_APP", "3")):
            self.env.update({f"{prefix}_ID": app, f"{prefix}_PRIVATE_KEY_PATH": str(key),
                             f"{prefix}_API_URL": f"http://127.0.0.1:{self.server.server_port}"})
            if prefix != "GITHUB_CLIENT_APP":
                self.env[f"{prefix}_INSTALLATION_ID"] = str(int(app) + 100)
        env_patch = patch.dict(os.environ, self.env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)

    @staticmethod
    def auth(repo: str, *, write: bool = False) -> tuple[str, str]:
        config = identity.github_app_config(repository=repo)
        assert config is not None
        return identity.github_app_auth(config, repository=repo, require_installation=write)

    def test_owner_routing_optional_installation_and_separate_caches(self) -> None:
        for repo in ("host/product", "Second-Host/product"):
            self.assertEqual(self.auth(repo, write=True), ("fake-token-2", "app-2[bot]"))
        self.assertEqual(self.auth("client/product"), ("fake-token-1", "app-1[bot]"))
        config = identity.github_app_config()
        assert config is not None
        self.assertEqual(identity.github_app_auth(config, repository="host/product"),
                         ("fake-token-1", "app-1[bot]"))
        calls = list(AppHandler.calls)
        self.auth("host/product")
        self.assertEqual(AppHandler.calls, calls)
        self.assertEqual(len(list((self.root / ".code/state/github-app-token").glob("*.json"))), 4)

    def test_missing_installation_read_refuses_write_keeps_own_user_route(self) -> None:
        os.environ["GITHUB_CLIENT_APP_INSTALLATION_ID"] = "102"
        with self.assertRaisesRegex(identity.GitHubAppError, "Client App is not installed on host/missing"):
            self.auth("host/missing")
        with self.assertRaises(identity.ContributorRepository):
            self.auth("host/missing", write=True)
        self.assertFalse(identity.acts_as_own_user("host/missing"))
        with patch.dict(os.environ, {identity.OWN_USER_OPT_IN: "1",
                                      "GH_WITH_ENV_TOKEN_OWN_USER_LOGIN": "own-user"}):
            self.assertTrue(identity.acts_as_own_user("host/missing"))
        self.assertFalse(any(method == "POST" for method, _, _ in AppHandler.calls))

    def test_reader_keeps_its_app_and_unconfigured_fleet_keeps_legacy_reads(self) -> None:
        config = identity.github_app_config(reader=True, repository="host/product")
        assert config is not None
        self.assertEqual(identity.github_app_auth(config, repository="host/product"),
                         ("fake-token-3", "app-3[bot]"))
        os.environ.pop("GITHUB_CLIENT_APP_OWNERS")
        self.assertEqual(self.auth("host/missing"), ("fake-token-1", "app-1[bot]"))
        self.assertEqual(identity.github_app_prefix("host/product"), "GITHUB_APP")

    def test_partial_configuration_and_key_modes_do_not_borrow_primary(self) -> None:
        os.environ.pop("GITHUB_CLIENT_APP_ID")
        with self.assertRaisesRegex(identity.GitHubAppError, "GITHUB_CLIENT_APP_ID"):
            self.auth("host/product")
        self.auth("client/product")
        os.environ["GITHUB_CLIENT_APP_ID"] = "2"
        Path(self.env["GITHUB_CLIENT_APP_PRIVATE_KEY_PATH"]).chmod(0o644)
        with self.assertRaisesRegex(identity.GitHubAppError, "mode 600"):
            self.auth("host/product")

    def fake_gh(self) -> Path:
        gh = self.root / "gh"
        gh.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "if args[:2] == ['auth', 'status']: raise SystemExit(0)\n"
            "if args[:2] == ['auth', 'token']: print('fake-user-token'); raise SystemExit(0)\n"
            "token = os.environ.get('GH_TOKEN', '')\n"
            "login = 'own-user' if token == 'fake-user-token' or not token else 'app-' + token[-1] + '[bot]'\n"
            "if args[:2] == ['api', 'user']: print(login); raise SystemExit(0)\n"
            "if '--include' in args: print('HTTP/2.0 200\\n')\n"
            "endpoint = next(('/' + a.lstrip('/') for a in args if a.lstrip('/').startswith(('repos/', 'search/'))), '')\n"
            "if endpoint.startswith('/search/issues'):\n"
            "    print(json.dumps({'items': [], 'total_count': 0, 'incomplete_results': False})); raise SystemExit(0)\n"
            "if endpoint.endswith('/milestones') or '/milestones?' in endpoint:\n"
            "    print('[]'); raise SystemExit(0)\n"
            "if endpoint.endswith('/issues') or '/issues?' in endpoint:\n"
            "    if '--method' not in args or args[args.index('--method') + 1] == 'GET': print('[]'); raise SystemExit(0)\n"
            "    issue = json.load(sys.stdin)\n"
            "    issue.update({'id': 1, 'number': 1, 'state': 'open', 'html_url': 'https://github.com/client/product/issues/1', 'user': {'login': login}})\n"
            "    print(json.dumps(issue)); raise SystemExit(0)\n"
            "if '/issues/' in endpoint and '/comments' not in endpoint:\n"
            "    issue = json.load(sys.stdin) if '--method' in args and args[args.index('--method') + 1] == 'PATCH' else {}\n"
            "    issue.update({'id': int(endpoint.rsplit('/', 1)[-1]), 'number': int(endpoint.rsplit('/', 1)[-1]), 'html_url': 'https://github.com/client/product/issues/1', 'user': {'login': login}})\n"
            "    print(json.dumps(issue)); raise SystemExit(0)\n"
            "if endpoint.endswith('/comments') or '/comments?' in endpoint:\n"
            "    if '--method' not in args or args[args.index('--method') + 1] == 'GET': print('[]'); raise SystemExit(0)\n"
            "    body = json.load(sys.stdin)['body']\n"
            "    print(json.dumps({'id': 1, 'html_url': 'https://github.com/host/missing/issues/1#issuecomment-1', 'body': body, 'user': {'login': login}, 'created_at': '2026-10-10T16:00:00Z'})); raise SystemExit(0)\n"
            "print(json.dumps({'login': login, 'token': token}))\n"
        )
        gh.chmod(0o700)
        return gh

    def wrapper(self, *args: str, own_user: bool = False) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "GH_WITH_ENV_TOKEN_GH": str(self.fake_gh()),
               "GH_WITH_ENV_TOKEN_PYTHON": sys.executable}
        if own_user:
            env[identity.OWN_USER_OPT_IN] = "1"
        return subprocess.run([str(Path(identity.__file__).with_name("gh-with-env-token")), *args],
                              env=env, capture_output=True, text=True, timeout=15)

    def test_wrapper_reads_and_writes_select_client_verified_actor(self) -> None:
        for args in (("api", "/repos/host/product"),
                     ("issue", "comment", "1", "--repo", "host/product", "--body", "fixture")):
            result = self.wrapper(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["token"], "fake-token-2")
        result = self.wrapper("api", "/repos/client/product")
        self.assertEqual(json.loads(result.stdout)["token"], "fake-token-1")
        result = self.wrapper("--reader", "api", "/repos/host/product")
        self.assertEqual(json.loads(result.stdout)["token"], "fake-token-3")

    def test_check_with_repository_context_verifies_selected_client_installation(self) -> None:
        with patch.dict(os.environ, {"GH_REPO": "host/product"}):
            result = self.wrapper("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("app-2[bot]", result.stdout)
        self.assertIn(("GET", "/app/installations/102", "2"), AppHandler.calls)

    def test_wrapper_missing_installation_refusal_and_explicit_own_user(self) -> None:
        result = self.wrapper("api", "/repos/host/missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Client App is not installed on host/missing", result.stderr)
        args = ("issue", "comment", "1", "--repo", "host/missing", "--body", "fixture")
        result = self.wrapper(*args)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GH_WITH_ENV_TOKEN_OWN_USER=1", result.stderr)
        with patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_OWN_USER_LOGIN": "own-user"}):
            result = self.wrapper(*args, own_user=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("acting as your own GitHub user", result.stderr)

    def test_client_route_preserves_explicit_expected_login_override(self) -> None:
        args = ("issue", "comment", "1", "--repo", "host/product", "--body", "fixture")
        with patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_EXPECTED_LOGIN": "different[bot]"}):
            result = self.wrapper(*args)
            self.assertNotEqual(result.returncode, 0)

            self.assertIn("expected 'different[bot]'", result.stderr)
        with patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_EXPECTED_LOGIN": "app-2[bot]"}):
            result = self.wrapper(*args)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_own_user_actor_refusal_is_not_retried_as_network_failure(self) -> None:
        os.environ.update({identity.OWN_USER_OPT_IN: "1",
                           "GH_WITH_ENV_TOKEN_OWN_USER_LOGIN": "another-person",
                           "GH_WITH_ENV_TOKEN_GH": str(self.fake_gh()),
                           "GH_WITH_ENV_TOKEN_PYTHON": sys.executable})
        wrapper = str(Path(identity.__file__).with_name("gh-with-env-token"))
        result = github_api.call_gh_with_retry(
            "GET", "/user", gh_cmd=wrapper,
            gh_prefix_args=["--write-actor-for", "host/missing"],
            operation="github.comment.create", actor="app-1[bot]",
            expected_actor="app-1[bot]")
        self.assertFalse(result.ok)
        self.assertFalse(result.failure.retryable)
        self.assertIsNotNone(result.retry_summary)
        self.assertEqual(result.retry_summary.attempts, 1)

    def test_python_request_and_comment_actor_follow_client(self) -> None:
        os.environ["GH_WITH_ENV_TOKEN_GH"] = str(self.fake_gh())
        os.environ["GH_WITH_ENV_TOKEN_PYTHON"] = sys.executable
        wrapper = str(Path(identity.__file__).with_name("gh-with-env-token"))
        actor = github_comment.authenticated_actor(gh_cmd=wrapper, operation="github.comment.create",
            expected_actor="app-1[bot]", write_repository="host/product")
        self.assertEqual(actor, "app-2[bot]")
        issue_actor = github_issue._authenticated_actor(gh_cmd=wrapper, operation="github.issue.create",
            expected_actor="app-1[bot]", write_repository="host/product")
        self.assertEqual(issue_actor, actor)
        result = github_api.call_gh_with_retry("POST", "/repos/host/product/issues/1/comments",
            {"body": "fixture"}, gh_cmd=wrapper, operation="github.comment.create",
            actor="app-1[bot]", expected_actor="app-1[bot]")
        self.assertTrue(result.ok, result)
        self.assertEqual((result.actor, result.expected_actor), (actor, actor))

    def test_global_repository_context_and_milestone_writer_use_client_actor(self) -> None:
        os.environ.update({"GH_REPO": "https://github.com/host/product", "GH_WITH_ENV_TOKEN_GH": str(self.fake_gh()),
                           "GH_WITH_ENV_TOKEN_PYTHON": sys.executable})
        wrapper = str(Path(identity.__file__).with_name("gh-with-env-token"))
        result = github_api.call_gh_with_retry("GET", "/user", gh_cmd=wrapper,
            operation="github.plan.milestone_create", actor="app-1[bot]", expected_actor="app-1[bot]")
        self.assertTrue(result.ok, result)
        self.assertEqual(result.expected_actor, "app-2[bot]")
        state = github_milestone._command_state("host/product", operation="github.plan.milestone_create",
            actor="app-1[bot]", expected_actor="app-1[bot]", gh_cmd=wrapper, verify_actor=True)
        self.assertEqual(state[1:3], ("app-2[bot]", "app-2[bot]"))
        self.assertEqual(github_comment.authenticated_actor(gh_cmd=wrapper, operation="github.comment.issue",
            expected_actor="APP-1[BOT]", write_repository="host/product"), "app-2[bot]")
        with self.assertRaises(github_comment.CommentError):
            github_comment.authenticated_actor(gh_cmd=wrapper, operation="github.comment.issue",
                expected_actor="different[bot]", write_repository="host/product")

    def plan(self, repo: str | None, issue: str = "1", *,
             checkout_repo: str = "client/product",
             command: tuple[str, ...] | None = None) -> subprocess.CompletedProcess[str]:
        # Reproduce a two-App Client fleet without a separate bulk-reader App.
        env = {name: value for name, value in os.environ.items()
               if not name.startswith("GITHUB_READER_APP_")}
        checkout = self.root / "checkout"
        if not checkout.exists():
            subprocess.run(["git", "init", "--quiet", str(checkout)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin",
                            f"https://github.com/{checkout_repo}.git"], check=True, capture_output=True)
        else:
            subprocess.run(["git", "-C", str(checkout), "remote", "set-url", "origin",
                            f"https://github.com/{checkout_repo}.git"], check=True, capture_output=True)
        return subprocess.run(
            [sys.executable, str(Path(identity.__file__).with_name("gh-plan.py")),
             *(["--repo", repo] if repo else []), *(command or ("show", issue))],
            env={**env, "GH_WITH_ENV_TOKEN_GH": str(self.fake_gh()),
                 "GH_WITH_ENV_TOKEN_PYTHON": sys.executable},
            cwd=checkout, capture_output=True, text=True, timeout=15,
        )

    def test_plan_explicit_repository_selects_app_without_global_context(self) -> None:
        self.assertNotIn("GH_REPO", os.environ)
        for repo in ("host/product", "client/product"):
            with self.subTest(repo=repo):
                result = self.plan(repo)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["issue"]["repo"], repo)
                with patch.dict(os.environ, {"GH_REPO": repo}):
                    contextual = self.plan(repo)
                self.assertEqual(contextual.returncode, 0, contextual.stdout + contextual.stderr)
                self.assertEqual(payload["last_actor"], json.loads(contextual.stdout)["last_actor"])

    def test_plan_explicit_repository_overrides_other_app_context(self) -> None:
        for repo, context in (("host/product", "client/product"), ("client/product", "host/product")):
            with self.subTest(repo=repo), patch.dict(os.environ, {"GH_REPO": context}):
                result = self.plan(repo)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["last_actor"], self.auth(repo)[1])

    def test_plan_client_repository_keeps_missing_installation_refusal(self) -> None:
        missing = self.plan("host/missing")
        self.assertNotEqual(missing.returncode, 0)
        payload = json.loads(missing.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "identity_refused")
        self.assertIn(("GET", "/repos/host/missing/installation", self.env["GITHUB_CLIENT_APP_ID"]),
                      AppHandler.calls)
        self.assertFalse(any(method == "POST" for method, _, _ in AppHandler.calls))

    def test_plan_qualified_issue_selects_its_owner_over_default_repository(self) -> None:
        for default, target in (("host/product", "client/product"), ("client/product", "host/product"),
                                (None, "host/product"), (None, "client/product")):
            for ref in (f"{target}#1", f"https://github.com/{target}/issues/1"):
                with self.subTest(default=default, ref=ref):
                    result = self.plan(default, ref)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    payload = json.loads(result.stdout)
                    self.assertEqual(payload["issue"]["repo"], target)
                    self.assertEqual(payload["last_actor"], self.auth(target)[1])

    def test_plan_inferred_repository_selects_its_owner_app(self) -> None:
        for repo in ("host/product", "client/product"):
            with self.subTest(repo=repo):
                result = self.plan(None, checkout_repo=repo)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["issue"]["repo"], repo)
                self.assertEqual(payload["last_actor"], self.auth(repo)[1])

    def test_plan_no_issue_reads_select_inferred_repository_app(self) -> None:
        for repo in ("host/product", "client/product"):
            for command in (("index",), ("search", "fixture"), ("wait", "--all"), ("milestone-list",)):
                with self.subTest(repo=repo, command=command):
                    result = self.plan(None, checkout_repo=repo, command=command)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    payload = json.loads(result.stdout)
                    self.assertEqual(payload["repo"], repo)
                    self.assertEqual(payload["last_actor"], self.auth(repo)[1])

    def test_plan_inferred_index_preserves_missing_installation_refusal(self) -> None:
        result = self.plan(None, checkout_repo="host/missing", command=("index",))
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "identity_refused")
        self.assertIn(("GET", "/repos/host/missing/installation", self.env["GITHUB_CLIENT_APP_ID"]),
                      AppHandler.calls)
        self.assertFalse(any(method == "POST" for method, _, _ in AppHandler.calls))

    def test_python_bulk_reads_use_reader_then_client_when_reader_absent(self) -> None:
        kwargs = dict(operation="github.plan.index", is_write=False, gh_cmd=github_api.DEFAULT_GH,
                      gh_prefix_args=[], actor="app-1[bot]", expected_actor="app-1[bot]",
                      repository="host/product")
        actor, expected, prefix = github_api.request_identity(**kwargs)
        self.assertEqual((actor, expected), ("app-3[bot]", "app-3[bot]"))
        self.assertIn("--reader", prefix)
        for suffix in ("ID", "INSTALLATION_ID", "PRIVATE_KEY_PATH"):
            os.environ.pop(f"GITHUB_READER_APP_{suffix}")
        actor, expected, prefix = github_api.request_identity(**kwargs)
        self.assertEqual((actor, expected), ("app-2[bot]", "app-2[bot]"))
        self.assertIn("--main-app-only", prefix)

    def test_explicit_other_repository_write_overrides_client_context(self) -> None:
        os.environ.update({"GH_REPO": "host/product", "GH_WITH_ENV_TOKEN_GH": str(self.fake_gh()),
                           "GH_WITH_ENV_TOKEN_PYTHON": sys.executable})
        wrapper = str(Path(identity.__file__).with_name("gh-with-env-token"))
        result = github_issue.create_issue("fixture", "fixture body", repo="client/product",
                                           gh_cmd=wrapper, expected_actor="app-1[bot]")
        self.assertEqual(result["actor"], "app-1[bot]")
        state = github_milestone._command_state("client/product", operation="github.plan.milestone_create",
            actor="app-1[bot]", expected_actor="app-1[bot]", gh_cmd=wrapper, verify_actor=True)
        self.assertEqual(state[1:3], ("app-1[bot]", "app-1[bot]"))
        with patch.dict(os.environ, {identity.OWN_USER_OPT_IN: "1",
                                      "GH_WITH_ENV_TOKEN_OWN_USER_LOGIN": "own-user"}):
            own = github_issue.create_issue("fixture", "fixture body", repo="upstream/missing",
                gh_cmd=wrapper, expected_actor="app-1[bot]")
            self.assertEqual(own["actor"], "own-user")
            for duplicate in ("client/missing#5", "upstream/missing#5", "host/product#5"):
                closed = github_issue.set_issue_state(1, state="closed", state_reason="duplicate",
                    repo="upstream/missing", duplicate_of=duplicate,
                    gh_cmd=wrapper, expected_actor="app-1[bot]")
                self.assertEqual(closed["actor"], "own-user")
            # The primary route's ordinary reads retain their legacy default installation.
            read = self.wrapper("api", "/repos/upstream/missing")
            self.assertEqual(json.loads(read.stdout)["token"], "fake-token-1")
            with patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH": "1"}):
                with self.assertRaises(github_issue.IssueError):
                    github_issue.create_issue("fixture", "fixture body", repo="upstream/missing",
                        gh_cmd=wrapper, expected_actor="app-1[bot]")

    def test_configured_client_login_trusts_its_managed_plan_body(self) -> None:
        _, login = self.auth("host/product", write=True)
        issue = {"body": PLAN.template_body("fixture"), "user": {"login": login}}
        with patch.multiple(PLAN, EXPECTED_ACTOR="app-1[bot]"):
            self.assertFalse(PLAN.read_plan_sections(issue)[1]["section_updates_allowed"])
            with patch.dict(os.environ, {"CODEX_AUTOMATION_BOT_LOGINS": login}):
                sections, provenance = PLAN.read_plan_sections(issue)
                self.assertTrue(provenance["section_updates_allowed"])
                self.assertTrue(sections)

    def test_own_user_comment_keeps_prerequisite_reads_on_resolved_writer(self) -> None:
        os.environ.update({identity.OWN_USER_OPT_IN: "1", "GH_WITH_ENV_TOKEN_OWN_USER_LOGIN": "own-user",
                           "GH_WITH_ENV_TOKEN_GH": str(self.fake_gh()),
                           "GH_WITH_ENV_TOKEN_PYTHON": sys.executable})
        result = github_comment.comment("issue", 1, "fixture", repo="host/missing",
                                        expected_actor="app-1[bot]")
        self.assertEqual(result["actor"], "own-user")
        self.assertEqual(result["comment"]["author"], "own-user")
        # A standalone read with that opt-in still cannot borrow an installation.
        standalone = self.wrapper("api", "/repos/host/missing", own_user=True)
        self.assertNotEqual(standalone.returncode, 0)
        with patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH": "1"}):
            with self.assertRaises(github_comment.CommentError):
                github_comment.comment("issue", 1, "fixture", repo="host/missing",
                                       expected_actor="app-1[bot]")

    def test_client_app_does_not_change_other_accounts_legacy_token_route(self) -> None:
        for prefix in ("GITHUB_APP", "GITHUB_READER_APP"):
            for suffix in ("ID", "INSTALLATION_ID", "PRIVATE_KEY_PATH"):
                os.environ.pop(f"{prefix}_{suffix}")
        os.environ["CODEX_GITHUB_TOKEN"] = "fake-token-1"
        actor, expected, prefix = github_api.request_identity(operation="github.plan.index", is_write=False,
            gh_cmd=github_api.DEFAULT_GH, gh_prefix_args=[], actor="app-1[bot]", expected_actor="app-1[bot]",
            repository="client/product")
        self.assertEqual((actor, expected, prefix), ("app-1[bot]", "app-1[bot]", []))
        result = self.wrapper("api", "/repos/client/product")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["token"], "fake-token-1")
        self.assertEqual(AppHandler.calls, [])
        fake_git = self.root / "commit-git"
        committed = self.root / "committed.json"
        remote = self.root / "remote"
        remote.write_text("https://github.com/client/product.git")
        fake_git.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            f"if sys.argv[1:3] == ['remote', 'get-url']: print(Path({str(remote)!r}).read_text())\n"
            "else:\n"
            f"    Path({str(committed)!r}).write_text(json.dumps({{'author': os.environ.get('GIT_AUTHOR_NAME')}}))\n"
        )
        fake_git.chmod(0o700)
        commit_env = {**os.environ, "CODEX_AUTOMATION_EMAIL": "bot@example.invalid",
                      "GIT_COMMIT_AS_BOT_GIT": str(fake_git), "GIT_COMMIT_AS_BOT_PYTHON": sys.executable}
        command = [str(Path(identity.__file__).with_name("git-commit-as-bot")), "-m", "fixture"]
        result = subprocess.run(command, env=commit_env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(committed.read_text())["author"], "app-1[bot]")
        remote.write_text("https://github.com/host/missing.git")
        committed.unlink()
        refused = subprocess.run(command, env=commit_env, text=True, capture_output=True, timeout=15)
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse(committed.exists())
        allowed = subprocess.run(command, env={**commit_env, identity.OWN_USER_OPT_IN: "1"},
                                 text=True, capture_output=True, timeout=15)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertIsNone(json.loads(committed.read_text())["author"])

    def test_push_helper_uses_client_token_without_a_real_push(self) -> None:
        fake_git = self.root / "git"
        pushed = self.root / "pushed.json"
        fake_git.write_text(
            f"#!{sys.executable}\n"
            "import json, os, subprocess, sys\n"
            "args = sys.argv[1:]\n"
            "if args[:1] == ['config'] or args[:2] == ['remote', 'get-url']: print('https://github.com/host/product.git')\n"
            "elif 'push' in args:\n"
            "    token = subprocess.check_output([os.environ['GIT_ASKPASS'], 'Password'], text=True).strip()\n"
            f"    open({str(pushed)!r}, 'w').write(json.dumps({{'token': token, 'args': args}}))\n"
        )
        fake_git.chmod(0o700)
        result = subprocess.run([str(Path(identity.__file__).with_name("git-push-as-bot")),
                                 "origin", "work/fixture"], text=True, capture_output=True, timeout=15,
            env={**os.environ, "GIT_PUSH_AS_BOT_GIT": str(fake_git),
                 "GIT_PUSH_AS_BOT_GH": str(self.fake_gh()), "GIT_PUSH_AS_BOT_PYTHON": sys.executable})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(pushed.read_text())["token"], "fake-token-2")


if __name__ == "__main__":
    unittest.main()
