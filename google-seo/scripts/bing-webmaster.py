#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Bing Webmaster Tools and IndexNow helper."""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, NoReturn


BING_API_ROOT = "https://ssl.bing.com/webmaster/api.svc/json"
INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
REDACTED = "[redacted]"
RAW_SENSITIVE_KEYS = {
    "apikey",
    "api_key",
    "authorization",
    "bing_webmaster_api_key",
    "bing_indexnow_key",
    "key",
    "password",
    "secret",
    "token",
}


def runtime_home() -> Path:
    if os.environ.get("CODE_HOME"):
        return Path(os.environ["CODE_HOME"]).expanduser()
    if os.environ.get("CODEX_HOME"):
        return Path(os.environ["CODEX_HOME"]).expanduser()
    return Path.home() / ".code"


def local_env_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    for name in ("CODE_HOME", "CODEX_HOME"):
        value = os.environ.get(name)
        if value:
            candidates.append(Path(value).expanduser() / "local.env")
    candidates.append(Path.home() / ".code" / "local.env")

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return tuple(unique)


LOCAL_ENV_CANDIDATES = local_env_candidates()


def normalized_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


SENSITIVE_KEYS = {normalized_key(key) for key in RAW_SENSITIVE_KEYS}


def redacted(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if normalized_key(key) in SENSITIVE_KEYS else redacted(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redacted(item) for item in value]
    return value


def print_json(data: Any) -> None:
    print(json.dumps(redacted(data), indent=2, sort_keys=True))


def fail(message: str, code: int = 1, *, public: bool = True) -> NoReturn:
    if public:
        print(f"error: {message}", file=sys.stderr)
    else:
        print("error: Bing SEO helper command failed", file=sys.stderr)
    raise SystemExit(code)


def parse_local_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def config_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    for path in LOCAL_ENV_CANDIDATES:
        value = parse_local_env(path).get(name)
        if value:
            return value
    return None


def required_config_value(name: str, *, message: str) -> str:
    value = config_value(name)
    if not value:
        fail(message)
    return value


def bing_api_key() -> str:
    return required_config_value(
        "BING_WEBMASTER_API_KEY", message="missing BING_WEBMASTER_API_KEY in environment or local.env"
    )


def indexnow_key() -> str:
    return required_config_value(
        "BING_INDEXNOW_KEY", message="missing BING_INDEXNOW_KEY in environment or local.env"
    )


def require_url(value: str, *, label: str = "URL") -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        fail(f"{label} must be fully qualified, for example https://www.example.com/")
    return value


def normalize_site_url(value: str) -> str:
    if value.startswith("domain:"):
        fail("Bing site URLs must be real http(s) URLs, not domain properties")
    return require_url(value, label="site URL").rstrip("/") + "/"


def host_from_url(value: str) -> str:
    parsed = urllib.parse.urlparse(require_url(value))
    if not parsed.hostname:
        fail("URL must include a host")
    host = parsed.hostname.lower()
    port = parsed.port
    if port is None:
        return host
    if (parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80):
        return host
    return f"{host}:{port}"


def normalize_host_text(value: str) -> str:
    parsed = urllib.parse.urlsplit(f"//{value}", scheme="https")
    if not parsed.hostname:
        fail("host must be a valid hostname, for example www.example.com")
    host = parsed.hostname.lower()
    port = parsed.port
    if port is None or port in {80, 443}:
        return host
    return f"{host}:{port}"


def normalize_host_argument(value: str) -> str:
    return normalize_host_text(value)


def normalize_inspection_url(value: str) -> str:
    if value.startswith("domain:"):
        return value
    return require_url(value, label="URL")


def indexnow_key_path_prefix(key_location: str) -> str:
    parsed = urllib.parse.urlparse(require_url(key_location, label="key location"))
    if not parsed.path or parsed.path.endswith("/"):
        fail("key location must point to a key file, for example https://example.com/key.txt")
    directory = posixpath.dirname(parsed.path)
    return "/" if directory in {"", "/"} else f"{directory}/"


def indexnow_key_location_host(key_location: str) -> str:
    return host_from_url(require_url(key_location, label="key location"))


def url_within_indexnow_scope(url: str, *, host: str, path_prefix: str) -> bool:
    parsed_host = host_from_url(url)
    parsed_path = urllib.parse.urlparse(require_url(url)).path
    return parsed_host == host and parsed_path.startswith(path_prefix)


def read_url_list(args: argparse.Namespace) -> list[str]:
    urls: list[str] = []
    for value in args.url or []:
        urls.append(require_url(value))
    if args.url_file:
        path = Path(args.url_file).expanduser()
        if not path.exists():
            fail(f"URL file not found: {path}")
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                urls.append(require_url(stripped))
    if not urls:
        fail("provide at least one --url or --url-file entry")
    return urls


def bing_api_url(method: str, *, query: dict[str, str] | None = None) -> str:
    params = {"apikey": bing_api_key()}
    if query:
        params.update(query)
    return f"{BING_API_ROOT}/{method}?{urllib.parse.urlencode(params)}"


def http_json(
    url: str,
    *,
    method: str = "GET",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        fail(f"HTTP {exc.code} from Bing endpoint; response detail omitted", public=False)
    except urllib.error.URLError:
        fail("Bing endpoint request failed; reason omitted", public=False)
    raise AssertionError("unreachable")


def cmd_status(_args: argparse.Namespace) -> None:
    print_json(
        {
            "bing_webmaster_api_key_configured": config_value("BING_WEBMASTER_API_KEY") is not None,
            "bing_indexnow_key_configured": config_value("BING_INDEXNOW_KEY") is not None,
            "local_env_candidates": [str(path) for path in LOCAL_ENV_CANDIDATES],
            "bing_api_root": BING_API_ROOT,
            "indexnow_endpoint": INDEXNOW_ENDPOINT,
        }
    )


def cmd_sites(_args: argparse.Namespace) -> None:
    print_json(http_json(bing_api_url("GetUserSites")))


def cmd_submit_feed(args: argparse.Namespace) -> None:
    site = normalize_site_url(args.site)
    feed = require_url(args.feed, label="feed URL")
    data = http_json(
        bing_api_url("SubmitFeed"),
        method="POST",
        data={"siteUrl": site, "feedUrl": feed},
    )
    print_json({"submitted": True, "siteUrl": site, "feedUrl": feed, "response": data})


def cmd_url_info(args: argparse.Namespace) -> None:
    site = normalize_site_url(args.site)
    url = normalize_inspection_url(args.url)
    print_json(
        http_json(
            bing_api_url("GetUrlInfo", query={"siteUrl": site, "url": url}),
        )
    )


def cmd_url_quota(args: argparse.Namespace) -> None:
    site = normalize_site_url(args.site)
    print_json(http_json(bing_api_url("GetUrlSubmissionQuota", query={"siteUrl": site})))


def cmd_submit_url(args: argparse.Namespace) -> None:
    site = normalize_site_url(args.site)
    url = require_url(args.url)
    data = http_json(
        bing_api_url("SubmitUrl"),
        method="POST",
        data={"siteUrl": site, "url": url},
    )
    print_json({"submitted": True, "siteUrl": site, "url": url, "response": data})


def cmd_submit_url_batch(args: argparse.Namespace) -> None:
    site = normalize_site_url(args.site)
    urls = read_url_list(args)
    if len(urls) > 500:
        fail("Bing SubmitUrlBatch accepts at most 500 URLs per batch")
    data = http_json(
        bing_api_url("SubmitUrlBatch"),
        method="POST",
        data={"siteUrl": site, "urlList": urls},
    )
    print_json({"submitted": True, "siteUrl": site, "count": len(urls), "response": data})


def cmd_indexnow_verify(args: argparse.Namespace) -> None:
    key = indexnow_key()
    host = normalize_host_argument(args.host) if args.host else host_from_url(args.url)
    scheme = urllib.parse.urlparse(require_url(args.url)).scheme
    display_key = key if args.reveal_key else "<BING_INDEXNOW_KEY>"
    location = args.key_location or f"{scheme}://{host}/{display_key}.txt"
    expected_body = key if args.reveal_key else "<BING_INDEXNOW_KEY>"
    print_json(
        {
            "host": host,
            "key_file_url": location,
            "expected_file_body": expected_body,
            "key_revealed": bool(args.reveal_key),
            "instructions": "Host the configured IndexNow key as UTF-8 text. Use --reveal-key only when terminal output is an acceptable place to display the key.",
        }
    )


def cmd_indexnow_submit(args: argparse.Namespace) -> None:
    urls = read_url_list(args)
    host = normalize_host_argument(args.host) if args.host else host_from_url(urls[0])
    bad_hosts = [url for url in urls if host_from_url(url) != host]
    if bad_hosts:
        fail("all IndexNow URLs must belong to the submitted host")
    key = indexnow_key()
    data: dict[str, Any] = {"host": host, "key": key, "urlList": urls}
    if args.key_location:
        key_location = require_url(args.key_location, label="key location")
        if indexnow_key_location_host(key_location) != host:
            fail("--key-location must use the same host as the submitted URLs")
        key_path_prefix = indexnow_key_path_prefix(key_location)
        if any(not url_within_indexnow_scope(url, host=host, path_prefix=key_path_prefix) for url in urls):
            fail("all IndexNow URLs must stay under the key file path prefix when --key-location is used")
        data["keyLocation"] = key_location
    response = http_json(INDEXNOW_ENDPOINT, method="POST", data=data)
    print_json({"submitted": True, "host": host, "count": len(urls), "response": response})


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Bing Webmaster Tools and IndexNow helper")
    sub = root.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="show configured Bing credentials without secrets")
    status.set_defaults(func=cmd_status)

    sites = sub.add_parser("sites", help="list Bing Webmaster Tools sites")
    sites.set_defaults(func=cmd_sites)

    feed = sub.add_parser("submit-feed", help="submit a sitemap or feed")
    feed.add_argument("site", help="verified site URL, for example https://www.example.com/")
    feed.add_argument("feed", help="fully qualified sitemap/feed URL")
    feed.set_defaults(func=cmd_submit_feed)

    info = sub.add_parser("url-info", help="get Bing index details for one URL")
    info.add_argument("site", help="verified site URL")
    info.add_argument("url", help="fully qualified URL to inspect")
    info.set_defaults(func=cmd_url_info)

    quota = sub.add_parser("url-quota", help="get Bing URL submission quota")
    quota.add_argument("site", help="verified site URL")
    quota.set_defaults(func=cmd_url_quota)

    submit_url = sub.add_parser("submit-url", help="submit one URL directly to Bing")
    submit_url.add_argument("site", help="verified site URL")
    submit_url.add_argument("url", help="fully qualified URL to submit")
    submit_url.set_defaults(func=cmd_submit_url)

    submit_batch = sub.add_parser("submit-url-batch", help="submit up to 500 URLs directly to Bing")
    submit_batch.add_argument("site", help="verified site URL")
    submit_batch.add_argument("--url", action="append", help="URL to submit; repeat for multiple")
    submit_batch.add_argument("--url-file", help="newline-delimited URL file")
    submit_batch.set_defaults(func=cmd_submit_url_batch)

    verify = sub.add_parser("indexnow-verify", help="show IndexNow key hosting instructions")
    verify.add_argument("url", help="representative URL on the target host")
    verify.add_argument("--host", help="override host, for example www.example.com")
    verify.add_argument("--key-location", help="fully qualified IndexNow key file URL")
    verify.add_argument(
        "--reveal-key",
        action="store_true",
        help="print the configured key value so it can be copied into the verification file",
    )
    verify.set_defaults(func=cmd_indexnow_verify)

    indexnow = sub.add_parser("indexnow-submit", help="submit URL changes through IndexNow")
    indexnow.add_argument("--url", action="append", help="URL to submit; repeat for multiple")
    indexnow.add_argument("--url-file", help="newline-delimited URL file")
    indexnow.add_argument("--host", help="override host, for example www.example.com")
    indexnow.add_argument("--key-location", help="fully qualified IndexNow key file URL")
    indexnow.set_defaults(func=cmd_indexnow_submit)

    return root


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
