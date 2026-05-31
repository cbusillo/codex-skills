#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Workstation-wide Google Search Console OAuth helper."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any


CONFIG_DIR = Path.home() / ".code" / "google-search"
CLIENT_PATH = CONFIG_DIR / "oauth-client.json"
READ_TOKEN_PATH = CONFIG_DIR / "search-console-token.json"
WRITE_TOKEN_PATH = CONFIG_DIR / "search-console-write-token.json"
READ_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
WRITE_SCOPE = "https://www.googleapis.com/auth/webmasters"
DEFAULT_ACCESS_LEVEL = "read"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_ROOT = "https://searchconsole.googleapis.com/webmasters/v3"
INSPECTION_ROOT = "https://searchconsole.googleapis.com/v1"
REDACTED = "[redacted]"
RAW_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "key_string",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
}


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


def fail(message: str, code: int = 1, *, public: bool = True) -> None:
    if public:
        print(f"error: {message}", file=sys.stderr)
    else:
        print("error: Google SEO helper command failed", file=sys.stderr)
    raise SystemExit(code)


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    ensure_config_dir()
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp_path, 0o600)
    tmp_path.replace(path)


def token_path(access_level: str) -> Path:
    if access_level == "read":
        return READ_TOKEN_PATH
    if access_level == "write":
        return WRITE_TOKEN_PATH
    fail(f"unknown access level: {access_level}")


def scopes_for(access_level: str) -> list[str]:
    if access_level == "read":
        return [READ_SCOPE]
    if access_level == "write":
        return [WRITE_SCOPE]
    fail(f"unknown access level: {access_level}")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        fail(f"missing {path}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        fail(f"invalid JSON in {path}: {exc}")


def client_config() -> dict[str, Any]:
    data = load_json(CLIENT_PATH)
    if "installed" in data:
        data = data["installed"]
    elif "web" in data:
        data = data["web"]
    if not data.get("client_id") or not data.get("client_secret"):
        fail(f"{CLIENT_PATH} must contain an OAuth client_id and client_secret")
    return data


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    server: "OAuthHTTPServer"

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.oauth_code = params.get("code", [None])[0]
        self.server.oauth_error = params.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        if self.server.oauth_code:
            self.wfile.write(
                b"Google Search Console OAuth complete. You can close this tab.\n"
            )
        else:
            self.wfile.write(
                b"OAuth did not return a code. Return to the terminal for details.\n"
            )

    def log_message(self, format: str, *args: object) -> None:
        return


class OAuthHTTPServer(HTTPServer):
    oauth_code: str | None = None
    oauth_error: str | None = None


def http_json(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        fail(f"HTTP {exc.code} from {url}; response detail omitted", public=False)
    except urllib.error.URLError as exc:
        fail(f"request failed for {url}; reason omitted", public=False)


def token_request(params: dict[str, str]) -> dict[str, Any]:
    encoded = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL,
        data=encoded,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        fail(
            f"OAuth token request failed with HTTP {exc.code}; response detail omitted",
            public=False,
        )


def cmd_status(_args: argparse.Namespace) -> None:
    status = {
        "config_dir": str(CONFIG_DIR),
        "client_configured": CLIENT_PATH.exists(),
        "token_configured": READ_TOKEN_PATH.exists(),
        "scope": READ_SCOPE,
        "read_token_configured": READ_TOKEN_PATH.exists(),
        "write_token_configured": WRITE_TOKEN_PATH.exists(),
        "read_scope": READ_SCOPE,
        "write_scope": WRITE_SCOPE,
    }
    print_json(status)


def cmd_init(args: argparse.Namespace) -> None:
    source = Path(args.client_json).expanduser()
    if not source.exists():
        fail(f"OAuth client JSON not found: {source}")
    data = json.loads(source.read_text())
    ensure_config_dir()
    atomic_json(CLIENT_PATH, data)
    print(f"installed OAuth client config at {CLIENT_PATH}")


def run_auth(access_level: str) -> None:
    config = client_config()
    server = OAuthHTTPServer(("127.0.0.1", 0), OAuthCallbackHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth2callback"
    scopes = scopes_for(access_level)
    params = {
        "client_id": config["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print("Opening browser for Google OAuth consent...")
    print(f"Redirect URI: {redirect_uri}")
    print(f"Authorization URL: {auth_url}")
    webbrowser.open(auth_url)
    server.handle_request()

    if server.oauth_error:
        fail("OAuth provider returned an error; detail omitted", public=False)
    if not server.oauth_code:
        fail("OAuth flow did not return an authorization code")

    token_data = token_request(
        {
            "code": server.oauth_code,
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    )
    if "refresh_token" not in token_data:
        fail("OAuth response did not include a refresh token; rerun auth with consent")
    token_data["access_level"] = access_level
    token_data["scope_requested"] = scopes
    path = token_path(access_level)
    atomic_json(path, token_data)
    print(f"stored {access_level} Search Console token at {path}")


def cmd_auth(_args: argparse.Namespace) -> None:
    run_auth("read")


def cmd_auth_write(_args: argparse.Namespace) -> None:
    run_auth("write")


def access_token(access_level: str = DEFAULT_ACCESS_LEVEL) -> str:
    config = client_config()
    path = token_path(access_level)
    token_data = load_json(path)
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        fail(f"{path} has no refresh_token; run auth again")
    refreshed = token_request(
        {
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    )
    token_data.update(refreshed)
    token_data["refresh_token"] = refresh_token
    atomic_json(path, token_data)
    access_token_value = refreshed.get("access_token")
    if not access_token_value:
        fail("OAuth refresh response did not include an access token", public=False)
    return str(access_token_value)


def site_url(value: str) -> str:
    if value.startswith("sc-domain:"):
        return value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme and parsed.netloc:
        return value.rstrip("/") + "/"
    return f"sc-domain:{value}"


def cmd_sites(_args: argparse.Namespace) -> None:
    data = http_json(f"{API_ROOT}/sites", token=access_token())
    print_json(data)


def cmd_sitemaps(args: argparse.Namespace) -> None:
    site = urllib.parse.quote(site_url(args.site), safe="")
    data = http_json(f"{API_ROOT}/sites/{site}/sitemaps", token=access_token())
    print_json(data)


def sitemap_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        fail("sitemap URL must be fully qualified, for example https://www.example.com/sitemap.xml")
    return value


def cmd_submit_sitemap(args: argparse.Namespace) -> None:
    site = urllib.parse.quote(site_url(args.site), safe="")
    feed = urllib.parse.quote(sitemap_url(args.sitemap), safe="")
    http_json(
        f"{API_ROOT}/sites/{site}/sitemaps/{feed}",
        method="PUT",
        token=access_token("write"),
    )
    print_json(
        {
            "submitted": True,
            "site": site_url(args.site),
            "sitemap": args.sitemap,
        }
    )


def cmd_search_analytics(args: argparse.Namespace) -> None:
    site = urllib.parse.quote(site_url(args.site), safe="")
    dimensions = args.dimension or ["query"]
    body: dict[str, Any] = {
        "startDate": args.start_date,
        "endDate": args.end_date,
        "dimensions": dimensions,
        "rowLimit": args.row_limit,
    }
    if args.page:
        body["dimensionFilterGroups"] = [
            {
                "filters": [
                    {
                        "dimension": "page",
                        "operator": "equals",
                        "expression": args.page,
                    }
                ]
            }
        ]
    data = http_json(
        f"{API_ROOT}/sites/{site}/searchAnalytics/query",
        method="POST",
        token=access_token(),
        data=body,
    )
    if args.format == "json":
        print_json(data)
        return

    writer = csv.writer(sys.stdout)
    writer.writerow([*dimensions, "clicks", "impressions", "ctr", "position"])
    for row in data.get("rows", []):
        writer.writerow(
            [
                *row.get("keys", []),
                row.get("clicks"),
                row.get("impressions"),
                row.get("ctr"),
                row.get("position"),
            ]
        )


def cmd_inspect(args: argparse.Namespace) -> None:
    body = {"inspectionUrl": args.url, "siteUrl": site_url(args.site)}
    data = http_json(
        f"{INSPECTION_ROOT}/urlInspection/index:inspect",
        method="POST",
        token=access_token(),
        data=body,
    )
    print_json(data)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Google Search Console OAuth helper")
    sub = root.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="show configured files without secrets")
    status.set_defaults(func=cmd_status)

    init = sub.add_parser("init", help="install OAuth client JSON")
    init.add_argument("client_json")
    init.set_defaults(func=cmd_init)

    auth = sub.add_parser("auth", help="run browser OAuth consent flow")
    auth.set_defaults(func=cmd_auth)

    auth_write = sub.add_parser(
        "auth-write",
        help="run browser OAuth consent flow for explicit sitemap write access",
    )
    auth_write.set_defaults(func=cmd_auth_write)

    sites = sub.add_parser("sites", help="list accessible Search Console properties")
    sites.set_defaults(func=cmd_sites)

    sitemaps = sub.add_parser("sitemaps", help="list sitemaps for a property")
    sitemaps.add_argument("site", help="domain, sc-domain:domain, or URL-prefix property")
    sitemaps.set_defaults(func=cmd_sitemaps)

    submit_sitemap = sub.add_parser(
        "submit-sitemap",
        help="submit a sitemap for a property using the explicit write token",
    )
    submit_sitemap.add_argument(
        "site", help="domain, sc-domain:domain, or URL-prefix property"
    )
    submit_sitemap.add_argument("sitemap", help="fully qualified sitemap URL")
    submit_sitemap.set_defaults(func=cmd_submit_sitemap)

    analytics = sub.add_parser("search-analytics", help="query Search Analytics")
    analytics.add_argument("site", help="domain, sc-domain:domain, or URL-prefix property")
    analytics.add_argument("--start-date", required=True)
    analytics.add_argument("--end-date", required=True)
    analytics.add_argument(
        "--dimension",
        action="append",
        choices=["query", "page", "country", "device", "date", "searchAppearance"],
    )
    analytics.add_argument("--row-limit", type=int, default=1000)
    analytics.add_argument("--page", help="optional exact page URL filter")
    analytics.add_argument("--format", choices=["csv", "json"], default="csv")
    analytics.set_defaults(func=cmd_search_analytics)

    inspect = sub.add_parser("inspect", help="inspect a URL index status")
    inspect.add_argument("site", help="domain, sc-domain:domain, or URL-prefix property")
    inspect.add_argument("url", help="fully qualified URL to inspect")
    inspect.set_defaults(func=cmd_inspect)

    return root


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
