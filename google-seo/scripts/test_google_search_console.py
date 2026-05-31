#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import argparse
import importlib.util
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).with_name("google-search-console.py")
SPEC = importlib.util.spec_from_file_location("google_search_console", SCRIPT_PATH)
assert SPEC is not None
google_search_console = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(google_search_console)


class GoogleSearchConsoleHelperTest(TestCase):
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
