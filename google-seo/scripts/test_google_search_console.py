#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import argparse
import importlib.util
import io
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
            (config_dir / "search-console-token.json").touch()

            with ExitStack() as stack:
                self.patch_config_paths(stack, config_dir)
                stack.enter_context(
                    patch.object(google_search_console, "print_json", side_effect=rendered.append)
                )
                google_search_console.cmd_status(argparse.Namespace())

        self.assertEqual(rendered[0]["token_configured"], True)
        self.assertEqual(rendered[0]["scope"], google_search_console.READ_SCOPE)
        self.assertEqual(rendered[0]["read_token_configured"], True)
        self.assertEqual(rendered[0]["write_token_configured"], False)
        self.assertEqual(rendered[0]["sitemap_submission_configured"], False)

    def test_status_keeps_legacy_token_configured_read_only(self) -> None:
        rendered: list[dict[str, Any]] = []

        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "oauth-client.json").touch()
            (config_dir / "search-console-write-token.json").touch()

            with ExitStack() as stack:
                self.patch_config_paths(stack, config_dir)
                stack.enter_context(
                    patch.object(google_search_console, "print_json", side_effect=rendered.append)
                )
                google_search_console.cmd_status(argparse.Namespace())

        self.assertEqual(rendered[0]["token_configured"], False)
        self.assertEqual(rendered[0]["read_token_configured"], False)
        self.assertEqual(rendered[0]["write_token_configured"], True)
        self.assertEqual(rendered[0]["sitemap_submission_configured"], True)

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
