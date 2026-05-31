#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import argparse
import importlib.util
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
    def test_status_preserves_legacy_read_token_fields(self) -> None:
        rendered: list[dict[str, Any]] = []

        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            client_path = config_dir / "oauth-client.json"
            read_token_path = config_dir / "search-console-token.json"
            write_token_path = config_dir / "search-console-write-token.json"
            client_path.touch()
            read_token_path.touch()

            with (
                patch.object(google_search_console, "CONFIG_DIR", config_dir),
                patch.object(google_search_console, "CLIENT_PATH", client_path),
                patch.object(google_search_console, "READ_TOKEN_PATH", read_token_path),
                patch.object(google_search_console, "WRITE_TOKEN_PATH", write_token_path),
                patch.object(google_search_console, "print_json", side_effect=rendered.append),
            ):
                google_search_console.cmd_status(argparse.Namespace())

        self.assertEqual(rendered[0]["token_configured"], True)
        self.assertEqual(rendered[0]["scope"], google_search_console.READ_SCOPE)
        self.assertEqual(rendered[0]["read_token_configured"], True)
        self.assertEqual(rendered[0]["write_token_configured"], False)

    def test_status_keeps_legacy_token_configured_read_only(self) -> None:
        rendered: list[dict[str, Any]] = []

        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            client_path = config_dir / "oauth-client.json"
            read_token_path = config_dir / "search-console-token.json"
            write_token_path = config_dir / "search-console-write-token.json"
            client_path.touch()
            write_token_path.touch()

            with (
                patch.object(google_search_console, "CONFIG_DIR", config_dir),
                patch.object(google_search_console, "CLIENT_PATH", client_path),
                patch.object(google_search_console, "READ_TOKEN_PATH", read_token_path),
                patch.object(google_search_console, "WRITE_TOKEN_PATH", write_token_path),
                patch.object(google_search_console, "print_json", side_effect=rendered.append),
            ):
                google_search_console.cmd_status(argparse.Namespace())

        self.assertEqual(rendered[0]["token_configured"], False)
        self.assertEqual(rendered[0]["read_token_configured"], False)
        self.assertEqual(rendered[0]["write_token_configured"], True)

    def test_auth_does_not_request_incremental_grants(self) -> None:
        captured: dict[str, str] = {}

        class FakeServer:
            server_port = 8765
            oauth_code = "code"
            oauth_error = None

            def handle_request(self) -> None:
                return

        def fake_open(url: str) -> None:
            parsed = google_search_console.urllib.parse.urlparse(url)
            query = google_search_console.urllib.parse.parse_qs(parsed.query)
            captured["include_granted_scopes"] = query["include_granted_scopes"][0]

        with (
            patch.object(google_search_console, "client_config", return_value={"client_id": "client", "client_secret": "secret"}),
            patch.object(google_search_console, "OAuthHTTPServer", return_value=FakeServer()),
            patch.object(google_search_console.webbrowser, "open", side_effect=fake_open),
            patch.object(google_search_console, "token_request", return_value={"refresh_token": "refresh"}),
            patch.object(google_search_console, "atomic_json"),
        ):
            google_search_console.run_auth("read")

        self.assertEqual(captured["include_granted_scopes"], "false")

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
