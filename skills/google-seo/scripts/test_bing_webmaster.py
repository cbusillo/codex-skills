#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import argparse
import importlib.util
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest import TestCase, main
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).with_name("bing-webmaster.py")
SPEC = importlib.util.spec_from_file_location("bing_webmaster", SCRIPT_PATH)
assert SPEC is not None
bing_webmaster = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(bing_webmaster)


class BingWebmasterHelperTest(TestCase):
    def test_status_reports_booleans_without_printing_secrets(self) -> None:
        rendered: list[dict[str, Any]] = []

        def fake_config_value(name: str) -> str | None:
            return {
                "BING_WEBMASTER_API_KEY": "secret-api",
                "BING_INDEXNOW_KEY": "secret-index",
            }.get(name)

        with ExitStack() as stack:
            stack.enter_context(patch.object(bing_webmaster, "config_value", side_effect=fake_config_value))
            stack.enter_context(patch.object(bing_webmaster, "print_json", side_effect=rendered.append))
            bing_webmaster.cmd_status(argparse.Namespace())

        self.assertTrue(rendered[0]["bing_webmaster_api_key_configured"])
        self.assertTrue(rendered[0]["bing_indexnow_key_configured"])
        self.assertNotIn("secret-api", str(rendered[0]))

    def test_local_env_candidates_preserve_documented_order(self) -> None:
        with patch.dict(
            bing_webmaster.os.environ,
            {"CODE_HOME": "/tmp/code-home", "CODEX_HOME": "/tmp/codex-home"},
            clear=True,
        ):
            candidates = bing_webmaster.local_env_candidates()

        self.assertEqual(candidates[0], Path("/tmp/code-home/local.env"))
        self.assertEqual(candidates[1], Path("/tmp/codex-home/local.env"))
        self.assertEqual(candidates[2], Path.home() / ".code" / "local.env")

    def test_redaction_catches_bing_keys(self) -> None:
        data = bing_webmaster.redacted(
            {"apikey": "secret", "nested": {"BING_INDEXNOW_KEY": "secret-2", "ok": "visible"}}
        )
        self.assertEqual(data["apikey"], "[redacted]")
        self.assertEqual(data["nested"]["BING_INDEXNOW_KEY"], "[redacted]")
        self.assertEqual(data["nested"]["ok"], "visible")

    def test_submit_feed_uses_post_json_shape(self) -> None:
        calls: list[dict[str, Any]] = []
        rendered: list[dict[str, Any]] = []

        def fake_http_json(url: str, *, method: str = "GET", data: dict[str, Any] | None = None):
            calls.append({"url": url, "method": method, "data": data})
            return {"d": None}

        with ExitStack() as stack:
            stack.enter_context(patch.object(bing_webmaster, "bing_api_key", return_value="api-key"))
            stack.enter_context(patch.object(bing_webmaster, "http_json", side_effect=fake_http_json))
            stack.enter_context(patch.object(bing_webmaster, "print_json", side_effect=rendered.append))
            bing_webmaster.cmd_submit_feed(
                argparse.Namespace(
                    site="https://sellyouroutboard.com",
                    feed="https://www.sellyouroutboard.com/sitemap.xml",
                )
            )

        self.assertEqual(calls[0]["method"], "POST")
        self.assertIn("SubmitFeed?", calls[0]["url"])
        self.assertEqual(
            calls[0]["data"],
            {
                "siteUrl": "https://sellyouroutboard.com",
                "feedUrl": "https://www.sellyouroutboard.com/sitemap.xml",
            },
        )
        self.assertTrue(rendered[0]["submitted"])

    def test_normalize_site_url_preserves_verified_site_identifier(self) -> None:
        self.assertEqual(
            bing_webmaster.normalize_site_url("https://example.com"), "https://example.com"
        )
        self.assertEqual(
            bing_webmaster.normalize_site_url("https://example.com/"), "https://example.com/"
        )
        self.assertEqual(
            bing_webmaster.normalize_site_url("https://example.com/path"), "https://example.com/path"
        )

    def test_normalize_site_url_rejects_domain_properties(self) -> None:
        with self.assertRaises(SystemExit):
            bing_webmaster.normalize_site_url("domain:example.com")

    def test_url_info_allows_domain_inspection_syntax(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_http_json(url: str, *, method: str = "GET", data: dict[str, Any] | None = None):
            calls.append({"url": url, "method": method, "data": data})
            return {"d": None}

        with ExitStack() as stack:
            stack.enter_context(patch.object(bing_webmaster, "bing_api_key", return_value="api-key"))
            stack.enter_context(patch.object(bing_webmaster, "http_json", side_effect=fake_http_json))
            stack.enter_context(patch.object(bing_webmaster, "print_json"))
            bing_webmaster.cmd_url_info(argparse.Namespace(site="https://example.com", url="domain:bing.com"))

        self.assertIn("GetUrlInfo?", calls[0]["url"])
        self.assertIn("url=domain%3Abing.com", calls[0]["url"])

    def test_url_info_uses_get_query_shape(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_http_json(url: str, *, method: str = "GET", data: dict[str, Any] | None = None):
            calls.append({"url": url, "method": method, "data": data})
            return {"d": {"Url": "https://example.com/page"}}

        with ExitStack() as stack:
            stack.enter_context(patch.object(bing_webmaster, "bing_api_key", return_value="api-key"))
            stack.enter_context(patch.object(bing_webmaster, "http_json", side_effect=fake_http_json))
            stack.enter_context(patch.object(bing_webmaster, "print_json"))
            bing_webmaster.cmd_url_info(
                argparse.Namespace(site="https://example.com/", url="https://example.com/page")
            )

        self.assertEqual(calls[0]["method"], "GET")
        self.assertIsNone(calls[0]["data"])
        self.assertIn("GetUrlInfo?", calls[0]["url"])
        self.assertIn("siteUrl=https%3A%2F%2Fexample.com%2F", calls[0]["url"])
        self.assertIn("url=https%3A%2F%2Fexample.com%2Fpage", calls[0]["url"])

    def test_url_quota_uses_get_query_shape(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_http_json(url: str, *, method: str = "GET", data: dict[str, Any] | None = None):
            calls.append({"url": url, "method": method, "data": data})
            return {"d": {"DailyQuota": 100}}

        with ExitStack() as stack:
            stack.enter_context(patch.object(bing_webmaster, "bing_api_key", return_value="api-key"))
            stack.enter_context(patch.object(bing_webmaster, "http_json", side_effect=fake_http_json))
            stack.enter_context(patch.object(bing_webmaster, "print_json"))
            bing_webmaster.cmd_url_quota(argparse.Namespace(site="https://example.com/"))

        self.assertEqual(calls[0]["method"], "GET")
        self.assertIsNone(calls[0]["data"])
        self.assertIn("GetUrlSubmissionQuota?", calls[0]["url"])
        self.assertIn("siteUrl=https%3A%2F%2Fexample.com%2F", calls[0]["url"])

    def test_submit_url_uses_post_json_shape(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_http_json(url: str, *, method: str = "GET", data: dict[str, Any] | None = None):
            calls.append({"url": url, "method": method, "data": data})
            return {"d": None}

        with ExitStack() as stack:
            stack.enter_context(patch.object(bing_webmaster, "bing_api_key", return_value="api-key"))
            stack.enter_context(patch.object(bing_webmaster, "http_json", side_effect=fake_http_json))
            stack.enter_context(patch.object(bing_webmaster, "print_json"))
            bing_webmaster.cmd_submit_url(
                argparse.Namespace(site="https://example.com/", url="https://example.com/page")
            )

        self.assertEqual(calls[0]["method"], "POST")
        self.assertIn("SubmitUrl?", calls[0]["url"])
        self.assertEqual(
            calls[0]["data"],
            {"siteUrl": "https://example.com/", "url": "https://example.com/page"},
        )

    def test_submit_url_batch_limits_to_500(self) -> None:
        with self.assertRaises(SystemExit):
            with patch.object(
                bing_webmaster,
                "read_url_list",
                return_value=[f"https://example.com/{index}" for index in range(501)],
            ):
                bing_webmaster.cmd_submit_url_batch(
                    argparse.Namespace(site="https://example.com/", url=None, url_file=None)
                )

    def test_submit_url_batch_uses_post_url_list_shape(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_http_json(url: str, *, method: str = "GET", data: dict[str, Any] | None = None):
            calls.append({"url": url, "method": method, "data": data})
            return {"d": None}

        with ExitStack() as stack:
            stack.enter_context(patch.object(bing_webmaster, "bing_api_key", return_value="api-key"))
            stack.enter_context(patch.object(bing_webmaster, "http_json", side_effect=fake_http_json))
            stack.enter_context(patch.object(bing_webmaster, "print_json"))
            bing_webmaster.cmd_submit_url_batch(
                argparse.Namespace(
                    site="https://example.com/",
                    url=["https://example.com/a", "https://example.com/b"],
                    url_file=None,
                )
            )

        self.assertEqual(calls[0]["method"], "POST")
        self.assertIn("SubmitUrlBatch?", calls[0]["url"])
        self.assertEqual(
            calls[0]["data"],
            {
                "siteUrl": "https://example.com/",
                "urlList": ["https://example.com/a", "https://example.com/b"],
            },
        )

    def test_indexnow_verify_uses_placeholders_by_default(self) -> None:
        rendered: list[dict[str, Any]] = []
        with ExitStack() as stack:
            stack.enter_context(patch.object(bing_webmaster, "indexnow_key", return_value="abc123"))
            stack.enter_context(patch.object(bing_webmaster, "print_json", side_effect=rendered.append))
            bing_webmaster.cmd_indexnow_verify(
                argparse.Namespace(
                    url="https://www.example.com/page",
                    host=None,
                    key_location=None,
                    reveal_key=False,
                )
            )

        self.assertEqual(rendered[0]["host"], "www.example.com")
        self.assertEqual(rendered[0]["key_file_url"], "https://www.example.com/<BING_INDEXNOW_KEY>.txt")
        self.assertEqual(rendered[0]["expected_file_body"], "<BING_INDEXNOW_KEY>")
        self.assertFalse(rendered[0]["key_revealed"])
        self.assertNotIn("abc123", str(rendered[0]))

    def test_indexnow_verify_preserves_http_scheme(self) -> None:
        rendered: list[dict[str, Any]] = []
        with ExitStack() as stack:
            stack.enter_context(patch.object(bing_webmaster, "indexnow_key", return_value="abc123"))
            stack.enter_context(patch.object(bing_webmaster, "print_json", side_effect=rendered.append))
            bing_webmaster.cmd_indexnow_verify(
                argparse.Namespace(
                    url="http://www.example.com/page",
                    host=None,
                    key_location=None,
                    reveal_key=False,
                )
            )

        self.assertEqual(rendered[0]["key_file_url"], "http://www.example.com/<BING_INDEXNOW_KEY>.txt")

    def test_indexnow_verify_can_explicitly_reveal_key(self) -> None:
        rendered: list[dict[str, Any]] = []
        with ExitStack() as stack:
            stack.enter_context(patch.object(bing_webmaster, "indexnow_key", return_value="abc123"))
            stack.enter_context(patch.object(bing_webmaster, "print_json", side_effect=rendered.append))
            bing_webmaster.cmd_indexnow_verify(
                argparse.Namespace(
                    url="https://www.example.com/page",
                    host=None,
                    key_location=None,
                    reveal_key=True,
                )
            )

        self.assertEqual(rendered[0]["key_file_url"], "https://www.example.com/abc123.txt")
        self.assertEqual(rendered[0]["expected_file_body"], "abc123")
        self.assertTrue(rendered[0]["key_revealed"])

    def test_indexnow_submit_requires_one_host(self) -> None:
        with self.assertRaises(SystemExit):
            with patch.object(bing_webmaster, "indexnow_key", return_value="abc123"):
                bing_webmaster.cmd_indexnow_submit(
                    argparse.Namespace(
                        url=["https://a.example.com/page", "https://b.example.com/page"],
                        url_file=None,
                        host=None,
                        key_location=None,
                    )
                )

    def test_indexnow_submit_accepts_hostname_case_and_default_port_variants(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_http_json(url: str, *, method: str = "GET", data: dict[str, Any] | None = None):
            calls.append({"url": url, "method": method, "data": data})
            return {}

        with ExitStack() as stack:
            stack.enter_context(patch.object(bing_webmaster, "indexnow_key", return_value="abc123"))
            stack.enter_context(patch.object(bing_webmaster, "http_json", side_effect=fake_http_json))
            stack.enter_context(patch.object(bing_webmaster, "print_json"))
            bing_webmaster.cmd_indexnow_submit(
                argparse.Namespace(
                    url=["https://WWW.example.com/page", "https://www.example.com/other"],
                    url_file=None,
                    host=None,
                    key_location=None,
                )
            )

        self.assertEqual(calls[0]["data"]["host"], "www.example.com")

    def test_indexnow_submit_accepts_default_port_key_location_variants(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_http_json(url: str, *, method: str = "GET", data: dict[str, Any] | None = None):
            calls.append({"url": url, "method": method, "data": data})
            return {}

        with ExitStack() as stack:
            stack.enter_context(patch.object(bing_webmaster, "indexnow_key", return_value="abc123"))
            stack.enter_context(patch.object(bing_webmaster, "http_json", side_effect=fake_http_json))
            stack.enter_context(patch.object(bing_webmaster, "print_json"))
            bing_webmaster.cmd_indexnow_submit(
                argparse.Namespace(
                    url=["https://www.example.com/catalog/page"],
                    url_file=None,
                    host=None,
                    key_location="https://www.example.com:443/catalog/abc123.txt",
                )
            )

        self.assertEqual(calls[0]["data"]["keyLocation"], "https://www.example.com:443/catalog/abc123.txt")

    def test_indexnow_submit_posts_payload(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_http_json(url: str, *, method: str = "GET", data: dict[str, Any] | None = None):
            calls.append({"url": url, "method": method, "data": data})
            return {}

        with ExitStack() as stack:
            stack.enter_context(patch.object(bing_webmaster, "indexnow_key", return_value="abc123"))
            stack.enter_context(patch.object(bing_webmaster, "http_json", side_effect=fake_http_json))
            stack.enter_context(patch.object(bing_webmaster, "print_json"))
            bing_webmaster.cmd_indexnow_submit(
                argparse.Namespace(
                    url=["https://www.example.com/a", "https://www.example.com/b"],
                    url_file=None,
                    host=None,
                    key_location="https://www.example.com/abc123.txt",
                )
            )

        self.assertEqual(calls[0]["url"], "https://api.indexnow.org/indexnow")
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(
            calls[0]["data"],
            {
                "host": "www.example.com",
                "key": "abc123",
                "keyLocation": "https://www.example.com/abc123.txt",
                "urlList": ["https://www.example.com/a", "https://www.example.com/b"],
            },
        )

    def test_indexnow_submit_rejects_key_location_host_mismatch(self) -> None:
        with self.assertRaises(SystemExit):
            with patch.object(bing_webmaster, "indexnow_key", return_value="abc123"):
                bing_webmaster.cmd_indexnow_submit(
                    argparse.Namespace(
                        url=["https://www.example.com/a"],
                        url_file=None,
                        host=None,
                        key_location="https://cdn.example.com/abc123.txt",
                    )
                )

    def test_indexnow_submit_rejects_urls_outside_key_path_prefix(self) -> None:
        with self.assertRaises(SystemExit):
            with patch.object(bing_webmaster, "indexnow_key", return_value="abc123"):
                bing_webmaster.cmd_indexnow_submit(
                    argparse.Namespace(
                        url=["https://www.example.com/help/page"],
                        url_file=None,
                        host=None,
                        key_location="https://www.example.com/catalog/abc123.txt",
                    )
                )

    def test_url_within_indexnow_scope(self) -> None:
        # Positive matching (subdir prefix)
        self.assertTrue(
            bing_webmaster.url_within_indexnow_scope(
                "https://example.com/blog/posts/1", host="example.com", path_prefix="/blog/"
            )
        )
        # Boundary reject (shares substring but not trailing slash)
        self.assertFalse(
            bing_webmaster.url_within_indexnow_scope(
                "https://example.com/blog-posts/1", host="example.com", path_prefix="/blog/"
            )
        )
        # Root prefix positive match
        self.assertTrue(
            bing_webmaster.url_within_indexnow_scope(
                "https://example.com/about", host="example.com", path_prefix="/"
            )
        )

    def test_indexnow_key_path_prefix(self) -> None:
        # Key location at root
        self.assertEqual(
            bing_webmaster.indexnow_key_path_prefix("https://example.com/key.txt"), "/"
        )
        # Key location nested in directory
        self.assertEqual(
            bing_webmaster.indexnow_key_path_prefix("https://example.com/deep/path/to/key.txt"),
            "/deep/path/to/",
        )
        # Invalid key location
        with self.assertRaises(SystemExit):
            bing_webmaster.indexnow_key_path_prefix("https://example.com/")

    def test_read_url_list_formatting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "urls.txt"
            with file_path.open("w", encoding="utf-8") as handle:
                handle.write(
                    "\n  https://example.com/from-file  \n# comment line\nhttps://example.com/other-file\n"
                )
            urls = bing_webmaster.read_url_list(
                argparse.Namespace(
                    url=["https://example.com/from-arg"],
                    url_file=str(file_path),
                )
            )
        self.assertEqual(
            urls,
            [
                "https://example.com/from-arg",
                "https://example.com/from-file",
                "https://example.com/other-file",
            ],
        )

    def test_read_url_list_invalid_url_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "urls.txt"
            with file_path.open("w", encoding="utf-8") as handle:
                handle.write("invalid_url\n")
            with self.assertRaises(SystemExit):
                bing_webmaster.read_url_list(
                    argparse.Namespace(url=None, url_file=str(file_path))
                )

    def test_read_url_list_empty_fails(self) -> None:
        with self.assertRaises(SystemExit):
            bing_webmaster.read_url_list(argparse.Namespace(url=None, url_file=None))


if __name__ == "__main__":
    main()
