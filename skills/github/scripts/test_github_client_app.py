#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline two-App routing through real credential, cache and wrapper paths."""
from __future__ import annotations

import base64
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
from test_github_identity import test_private_key


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
        with patch.dict(os.environ, {identity.OWN_USER_OPT_IN: "1"}):
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

    def test_wrapper_missing_installation_refusal_and_explicit_own_user(self) -> None:
        result = self.wrapper("api", "/repos/host/missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Client App is not installed on host/missing", result.stderr)
        args = ("issue", "comment", "1", "--repo", "host/missing", "--body", "fixture")
        result = self.wrapper(*args)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GH_WITH_ENV_TOKEN_OWN_USER=1", result.stderr)
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
