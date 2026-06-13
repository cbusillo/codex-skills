#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import argparse
import importlib.util
import io
import json
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase, main
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).with_name("google-search-console.py")
SPEC = importlib.util.spec_from_file_location("google_search_console", SCRIPT_PATH)
assert SPEC is not None
google_search_console = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(google_search_console)


class GoogleSearchConsoleHelperTest(TestCase):
    def patch_config_paths(self, stack: ExitStack, config_dir: Path) -> None:
        stack.enter_context(patch.object(google_search_console, "CONFIG_DIR", config_dir))
        stack.enter_context(
            patch.object(
                google_search_console,
                "CLIENT_PATH",
                config_dir / "oauth-client.json",
            )
        )
        stack.enter_context(
            patch.object(
                google_search_console,
                "READ_TOKEN_PATH",
                config_dir / "search-console-token.json",
            )
        )
        stack.enter_context(
            patch.object(
                google_search_console,
                "WRITE_TOKEN_PATH",
                config_dir / "search-console-write-token.json",
            )
        )

    def test_fail_keeps_public_message_details(self) -> None:
        with patch.object(google_search_console.sys, "stderr", io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                google_search_console.fail("missing example config")

        self.assertIn("missing example config", stderr.getvalue())

    def test_runtime_home_prefers_code_home(self) -> None:
        with TemporaryDirectory() as tmp:
            code_home = str(Path(tmp) / "chris-code")
            codex_home = str(Path(tmp) / "codex")

            with patch.dict(
                google_search_console.os.environ,
                {"CODE_HOME": code_home, "CODEX_HOME": codex_home},
                clear=False,
            ):
                self.assertEqual(google_search_console.runtime_home(), Path(code_home))

    def test_runtime_home_uses_codex_home_before_default(self) -> None:
        with TemporaryDirectory() as tmp:
            codex_home = str(Path(tmp) / "codex")

            with patch.dict(
                google_search_console.os.environ,
                {"CODEX_HOME": codex_home},
                clear=False,
            ):
                google_search_console.os.environ.pop("CODE_HOME", None)
                self.assertEqual(google_search_console.runtime_home(), Path(codex_home))

    def test_status_preserves_legacy_read_token_fields(self) -> None:
        rendered: list[dict[str, Any]] = []

        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "oauth-client.json").touch()
            (config_dir / "search-console-token.json").write_text(
                json.dumps({"refresh_token": "read-refresh"})
            )

            with ExitStack() as stack:
                self.patch_config_paths(stack, config_dir)
                stack.enter_context(
                    patch.object(google_search_console, "print_json", side_effect=rendered.append)
                )
                google_search_console.cmd_status(argparse.Namespace())

        self.assertEqual(rendered[0]["token_configured"], True)
        self.assertEqual(rendered[0]["scope"], google_search_console.READ_SCOPE)
        self.assertEqual(rendered[0]["read_token_configured"], True)
        self.assertEqual(rendered[0]["read_token_state"], "configured")
        self.assertEqual(rendered[0]["write_token_configured"], False)
        self.assertEqual(rendered[0]["write_token_state"], "missing")
        self.assertEqual(rendered[0]["sitemap_submission_configured"], False)

    def test_status_keeps_legacy_token_configured_read_only(self) -> None:
        rendered: list[dict[str, Any]] = []

        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "oauth-client.json").touch()
            (config_dir / "search-console-write-token.json").write_text(
                json.dumps({"refresh_token": "write-refresh"})
            )

            with ExitStack() as stack:
                self.patch_config_paths(stack, config_dir)
                stack.enter_context(
                    patch.object(google_search_console, "print_json", side_effect=rendered.append)
                )
                google_search_console.cmd_status(argparse.Namespace())

        self.assertEqual(rendered[0]["token_configured"], False)
        self.assertEqual(rendered[0]["read_token_configured"], False)
        self.assertEqual(rendered[0]["read_token_state"], "missing")
        self.assertEqual(rendered[0]["write_token_configured"], True)
        self.assertEqual(rendered[0]["write_token_state"], "configured")
        self.assertEqual(rendered[0]["sitemap_submission_configured"], True)

    def test_status_reports_valid_and_invalid_token_states(self) -> None:
        rendered: list[dict[str, Any]] = []

        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "oauth-client.json").write_text(
                json.dumps({"installed": {"client_id": "client", "client_secret": "secret"}})
            )
            (config_dir / "search-console-token.json").write_text(
                json.dumps({"refresh_token": "read-refresh"})
            )
            (config_dir / "search-console-write-token.json").write_text(
                json.dumps({"refresh_token": "write-refresh"})
            )

            def fake_token_request(params: dict[str, str]) -> dict[str, Any]:
                if params["refresh_token"] == "write-refresh":
                    raise google_search_console.OAuthTokenRequestError(400, "invalid_grant")
                return {"access_token": "access"}

            with ExitStack() as stack:
                self.patch_config_paths(stack, config_dir)
                stack.enter_context(
                    patch.object(
                        google_search_console,
                        "token_request",
                        side_effect=fake_token_request,
                    )
                )
                stack.enter_context(
                    patch.object(google_search_console, "print_json", side_effect=rendered.append)
                )
                google_search_console.cmd_status(argparse.Namespace())

        self.assertEqual(rendered[0]["read_token_state"], "valid")
        self.assertEqual(rendered[0]["write_token_state"], "invalid")
        self.assertEqual(
            rendered[0]["write_token_status"],
            {
                "configured": True,
                "state": "invalid",
                "reason": "expired_or_revoked",
                "action": "run auth-write",
            },
        )

    def test_status_reports_token_file_invalid_without_printing_token(self) -> None:
        rendered: list[dict[str, Any]] = []

        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "oauth-client.json").write_text(
                json.dumps({"installed": {"client_id": "client", "client_secret": "secret"}})
            )
            (config_dir / "search-console-token.json").write_text(
                json.dumps({"access_token": "secret-access"})
            )

            with ExitStack() as stack:
                self.patch_config_paths(stack, config_dir)
                stack.enter_context(
                    patch.object(google_search_console, "print_json", side_effect=rendered.append)
                )
                google_search_console.cmd_status(argparse.Namespace())

        self.assertEqual(rendered[0]["read_token_state"], "invalid")
        self.assertEqual(rendered[0]["read_token_status"]["reason"], "token_file_invalid")
        self.assertNotIn("secret-access", json.dumps(rendered[0]))

    def test_status_reports_non_object_token_json_as_invalid(self) -> None:
        rendered: list[dict[str, Any]] = []

        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "search-console-token.json").write_text(
                json.dumps(["secret-refresh"])
            )

            with ExitStack() as stack:
                self.patch_config_paths(stack, config_dir)
                stack.enter_context(
                    patch.object(google_search_console, "print_json", side_effect=rendered.append)
                )
                google_search_console.cmd_status(argparse.Namespace())

        self.assertEqual(rendered[0]["read_token_state"], "invalid")
        self.assertEqual(rendered[0]["read_token_status"]["reason"], "token_file_invalid")
        self.assertNotIn("secret-refresh", json.dumps(rendered[0]))

    def test_status_treats_transient_refresh_failure_as_configured(self) -> None:
        rendered: list[dict[str, Any]] = []

        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "oauth-client.json").write_text(
                json.dumps({"installed": {"client_id": "client", "client_secret": "secret"}})
            )
            (config_dir / "search-console-token.json").write_text(
                json.dumps({"refresh_token": "read-refresh"})
            )

            with ExitStack() as stack:
                self.patch_config_paths(stack, config_dir)
                stack.enter_context(
                    patch.object(
                        google_search_console,
                        "token_request",
                        side_effect=google_search_console.OAuthTokenRequestError(503),
                    )
                )
                stack.enter_context(
                    patch.object(google_search_console, "print_json", side_effect=rendered.append)
                )
                google_search_console.cmd_status(argparse.Namespace())

        self.assertEqual(rendered[0]["read_token_state"], "configured")
        self.assertEqual(rendered[0]["read_token_status"]["reason"], "oauth_refresh_http_503")
        self.assertIn("retry status later", rendered[0]["read_token_status"]["action"])

    def test_status_treats_invalid_client_refresh_failure_as_invalid_config(self) -> None:
        rendered: list[dict[str, Any]] = []

        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "oauth-client.json").write_text(
                json.dumps({"installed": {"client_id": "client", "client_secret": "secret"}})
            )
            (config_dir / "search-console-token.json").write_text(
                json.dumps({"refresh_token": "secret-refresh"})
            )

            with ExitStack() as stack:
                self.patch_config_paths(stack, config_dir)
                stack.enter_context(
                    patch.object(
                        google_search_console,
                        "token_request",
                        side_effect=google_search_console.OAuthTokenRequestError(
                            401, "invalid_client"
                        ),
                    )
                )
                stack.enter_context(
                    patch.object(google_search_console, "print_json", side_effect=rendered.append)
                )
                google_search_console.cmd_status(argparse.Namespace())

        self.assertEqual(rendered[0]["read_token_state"], "invalid")
        self.assertEqual(rendered[0]["read_token_status"]["reason"], "oauth_client_invalid")
        self.assertIn("fix OAuth client config", rendered[0]["read_token_status"]["action"])
        self.assertIn("run auth", rendered[0]["read_token_status"]["action"])
        self.assertNotIn("secret-refresh", json.dumps(rendered[0]))

    def test_status_validation_does_not_rewrite_token_file(self) -> None:
        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            token_file = config_dir / "search-console-token.json"
            (config_dir / "oauth-client.json").write_text(
                json.dumps({"installed": {"client_id": "client", "client_secret": "secret"}})
            )
            token_file.write_text(json.dumps({"refresh_token": "read-refresh"}))
            before = token_file.read_text()

            with ExitStack() as stack:
                self.patch_config_paths(stack, config_dir)
                stack.enter_context(
                    patch.object(
                        google_search_console,
                        "token_request",
                        return_value={"access_token": "new-access"},
                    )
                )
                stack.enter_context(patch.object(google_search_console, "print_json"))
                google_search_console.cmd_status(argparse.Namespace())

            self.assertEqual(token_file.read_text(), before)

    def test_auth_does_not_request_incremental_grants(self) -> None:
        captured: dict[str, str] = {}

        class FakeServer:
            server_port = 8765
            oauth_code = "code"
            oauth_error = None
            callback_timed_out = False
            timeout = None

            def handle_request(self) -> None:
                return

            def server_close(self) -> None:
                return

        def fake_open(url: str) -> None:
            parsed = google_search_console.urllib.parse.urlparse(url)
            query = google_search_console.urllib.parse.parse_qs(parsed.query)
            captured["include_granted_scopes"] = query["include_granted_scopes"][0]

        with TemporaryDirectory() as tmp:
            with ExitStack() as stack:
                self.patch_config_paths(stack, Path(tmp))
                stack.enter_context(
                    patch.object(
                        google_search_console,
                        "client_config",
                        return_value={"client_id": "client", "client_secret": "secret"},
                    )
                )
                stack.enter_context(
                    patch.object(google_search_console, "OAuthHTTPServer", return_value=FakeServer())
                )
                stack.enter_context(
                    patch.object(google_search_console.webbrowser, "open", side_effect=fake_open)
                )
                stack.enter_context(
                    patch.object(
                        google_search_console,
                        "token_request",
                        return_value={"refresh_token": "refresh"},
                    )
                )
                stack.enter_context(patch.object(google_search_console, "atomic_json"))
                google_search_console.run_auth("read")

        self.assertEqual(captured["include_granted_scopes"], "false")

    def test_auth_callback_timeout_exits_without_token_request(self) -> None:
        class FakeServer:
            server_port = 8765
            oauth_code = None
            oauth_error = None
            callback_timed_out = False
            timeout = None
            closed = False

            def handle_request(self) -> None:
                self.callback_timed_out = True

            def server_close(self) -> None:
                self.closed = True

        fake_server = FakeServer()
        token_request = None
        stderr = None

        with TemporaryDirectory() as tmp:
            with ExitStack() as stack:
                self.patch_config_paths(stack, Path(tmp))
                stack.enter_context(
                    patch.object(
                        google_search_console,
                        "client_config",
                        return_value={"client_id": "client", "client_secret": "secret"},
                    )
                )
                stack.enter_context(
                    patch.object(google_search_console, "OAuthHTTPServer", return_value=fake_server)
                )
                stack.enter_context(patch.object(google_search_console.webbrowser, "open"))
                token_request = stack.enter_context(
                    patch.object(google_search_console, "token_request")
                )
                stderr = stack.enter_context(
                    patch.object(google_search_console.sys, "stderr", io.StringIO())
                )
                with self.assertRaises(SystemExit) as exc:
                    google_search_console.run_auth("write")

        self.assertEqual(exc.exception.code, 1)
        self.assertEqual(
            fake_server.timeout,
            google_search_console.OAUTH_CALLBACK_TIMEOUT_SECONDS,
        )
        self.assertTrue(fake_server.closed)
        assert stderr is not None
        assert token_request is not None
        self.assertIn("Browser callback timed out after 5 minutes", stderr.getvalue())
        token_request.assert_not_called()

    def test_auth_invalid_client_reports_config_action(self) -> None:
        class FakeServer:
            server_port = 8765
            oauth_code = "code"
            oauth_error = None
            callback_timed_out = False
            timeout = None

            def handle_request(self) -> None:
                return

            def server_close(self) -> None:
                return

        stderr = None

        with TemporaryDirectory() as tmp:
            with ExitStack() as stack:
                self.patch_config_paths(stack, Path(tmp))
                stack.enter_context(
                    patch.object(
                        google_search_console,
                        "client_config",
                        return_value={"client_id": "client", "client_secret": "secret"},
                    )
                )
                stack.enter_context(
                    patch.object(google_search_console, "OAuthHTTPServer", return_value=FakeServer())
                )
                stack.enter_context(patch.object(google_search_console.webbrowser, "open"))
                stack.enter_context(
                    patch.object(
                        google_search_console,
                        "token_request",
                        side_effect=google_search_console.OAuthTokenRequestError(
                            401, "invalid_client"
                        ),
                    )
                )
                stderr = stack.enter_context(
                    patch.object(google_search_console.sys, "stderr", io.StringIO())
                )
                with self.assertRaises(SystemExit):
                    google_search_console.run_auth("read")

        assert stderr is not None
        self.assertIn("fix OAuth client config, then run auth", stderr.getvalue())
        self.assertNotIn("secret", stderr.getvalue())

    def test_token_paths_are_separate_by_access_level(self) -> None:
        self.assertEqual(
            google_search_console.token_path("read").name,
            "search-console-token.json",
        )
        self.assertEqual(
            google_search_console.token_path("write").name,
            "search-console-write-token.json",
        )

    def test_write_scope_is_not_used_for_read_commands_by_default(self) -> None:
        self.assertEqual(
            google_search_console.scopes_for("read"),
            ["https://www.googleapis.com/auth/webmasters.readonly"],
        )
        self.assertEqual(
            google_search_console.scopes_for("write"),
            ["https://www.googleapis.com/auth/webmasters"],
        )

    def test_access_token_invalid_grant_reports_read_auth_action(self) -> None:
        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "oauth-client.json").write_text(
                json.dumps({"installed": {"client_id": "client", "client_secret": "secret"}})
            )
            (config_dir / "search-console-token.json").write_text(
                json.dumps({"refresh_token": "secret-refresh"})
            )

            with ExitStack() as stack:
                self.patch_config_paths(stack, config_dir)
                stack.enter_context(
                    patch.object(
                        google_search_console,
                        "token_request",
                        side_effect=google_search_console.OAuthTokenRequestError(
                            400, "invalid_grant"
                        ),
                    )
                )
                stderr = stack.enter_context(
                    patch.object(google_search_console.sys, "stderr", io.StringIO())
                )
                with self.assertRaises(SystemExit):
                    google_search_console.access_token("read")

        self.assertIn("read token expired or revoked; run auth", stderr.getvalue())
        self.assertNotIn("secret-refresh", stderr.getvalue())

    def test_access_token_invalid_grant_reports_write_auth_action(self) -> None:
        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "oauth-client.json").write_text(
                json.dumps({"installed": {"client_id": "client", "client_secret": "secret"}})
            )
            (config_dir / "search-console-write-token.json").write_text(
                json.dumps({"refresh_token": "secret-refresh"})
            )

            with ExitStack() as stack:
                self.patch_config_paths(stack, config_dir)
                stack.enter_context(
                    patch.object(
                        google_search_console,
                        "token_request",
                        side_effect=google_search_console.OAuthTokenRequestError(
                            400, "invalid_grant"
                        ),
                    )
                )
                stderr = stack.enter_context(
                    patch.object(google_search_console.sys, "stderr", io.StringIO())
                )
                with self.assertRaises(SystemExit):
                    google_search_console.access_token("write")

        self.assertIn("write token expired or revoked; run auth-write", stderr.getvalue())
        self.assertNotIn("secret-refresh", stderr.getvalue())

    def test_access_token_invalid_client_reports_config_action(self) -> None:
        cases = [
            ("read", "search-console-token.json", "auth"),
            ("write", "search-console-write-token.json", "auth-write"),
        ]
        for access_level, token_file, auth_command in cases:
            with self.subTest(access_level=access_level), TemporaryDirectory() as tmp:
                config_dir = Path(tmp)
                (config_dir / "oauth-client.json").write_text(
                    json.dumps({"installed": {"client_id": "client", "client_secret": "secret"}})
                )
                (config_dir / token_file).write_text(
                    json.dumps({"refresh_token": "secret-refresh"})
                )

                stderr = None
                with ExitStack() as stack:
                    self.patch_config_paths(stack, config_dir)
                    stack.enter_context(
                        patch.object(
                            google_search_console,
                            "token_request",
                            side_effect=google_search_console.OAuthTokenRequestError(
                                401, "invalid_client"
                            ),
                        )
                    )
                    stderr = stack.enter_context(
                        patch.object(google_search_console.sys, "stderr", io.StringIO())
                    )
                    with self.assertRaises(SystemExit):
                        google_search_console.access_token(access_level)

                assert stderr is not None
                self.assertIn(
                    f"fix OAuth client config, then run {auth_command}",
                    stderr.getvalue(),
                )
                self.assertNotIn("secret-refresh", stderr.getvalue())

    def test_submit_sitemap_uses_webmasters_put_endpoint(self) -> None:
        calls = []

        def fake_http_json(url, *, method="GET", token=None, data=None):
            calls.append(
                {
                    "url": url,
                    "method": method,
                    "token": token,
                    "data": data,
                }
            )
            return {}

        args = argparse.Namespace(
            site="sellyouroutboard.com",
            sitemap="https://www.sellyouroutboard.com/sitemap.xml",
        )

        with (
            patch.object(google_search_console, "access_token", return_value="token"),
            patch.object(google_search_console, "http_json", side_effect=fake_http_json),
            patch.object(google_search_console, "print_json"),
        ):
            google_search_console.cmd_submit_sitemap(args)

        self.assertEqual(calls, [
            {
                "url": "https://searchconsole.googleapis.com/webmasters/v3/sites/sc-domain%3Asellyouroutboard.com/sitemaps/https%3A%2F%2Fwww.sellyouroutboard.com%2Fsitemap.xml",
                "method": "PUT",
                "token": "token",
                "data": None,
            }
        ])


if __name__ == "__main__":
    main()
