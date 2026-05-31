#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Generic NPMplus operations engine for the infra-ops skill.

Environment-specific facts are supplied by a private context provider resolved
from ~/.code/local-context.toml [docs].local_infra. This script must not grow
private defaults such as hostnames, proxy host ids, env file paths, or remote
validation commands.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "npmplus.ops.v1"
DEFAULT_PROFILE = "default"
DEFAULT_CONTEXT_PROVIDER = Path("scripts/infra-context.py")
DEFAULT_TIMEOUT_SECONDS = 15
LOCAL_CONTEXT_PATH = Path.home() / ".code" / "local-context.toml"
AUTH_REQUEST_NONE = {None, "", "none"}
VALID_URL_SCHEMES = {"http", "https"}
ALLOWED_LIFECYCLE_ACTIONS = {"proxy-host-enable", "proxy-host-disable"}
PUBLIC_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class OpsError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    identity: str
    secret: str
    timeout: int


@dataclass(frozen=True)
class NpmplusContext:
    private_repo: Path
    profile: str
    env_file: Path
    base_url_env: str
    identity_env: str
    secret_env: str
    refs: dict[str, dict[str, Any]]
    default_pilot_ref: str | None
    allowed_apply_actions: set[str]


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def public_alias(value: str) -> str:
    if not PUBLIC_ALIAS_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "must be a public-safe alias: lowercase letters, numbers, and hyphens"
        )
    return value


def load_local_infra_repo(local_context_path: Path = LOCAL_CONTEXT_PATH) -> Path:
    try:
        with local_context_path.open("rb") as handle:
            config = tomllib.load(handle)
    except FileNotFoundError:
        raise OpsError("private infra context is not configured") from None
    except tomllib.TOMLDecodeError:
        raise OpsError("local context file is not valid TOML") from None

    docs = config.get("docs")
    if not isinstance(docs, dict):
        raise OpsError("local context is missing [docs]")
    raw_path = docs.get("local_infra")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise OpsError("local context is missing [docs].local_infra")

    private_repo = Path(raw_path).expanduser()
    if not private_repo.is_dir():
        raise OpsError("configured private infra repo path is not available")
    return private_repo


def run_context_provider(private_repo: Path, provider: Path, profile: str) -> dict[str, Any]:
    provider_path = provider if provider.is_absolute() else private_repo / provider
    if ".." in provider.parts:
        raise OpsError("context provider must stay inside the private infra repo")
    try:
        provider_path.resolve(strict=True).relative_to(private_repo.resolve(strict=True))
    except ValueError:
        raise OpsError("context provider must live inside the private infra repo") from None
    except FileNotFoundError:
        raise OpsError("private NPMplus context provider is not available") from None
    if not provider_path.is_file():
        raise OpsError("private NPMplus context provider is not available")

    command = [sys.executable, str(provider_path), "npmplus", "--profile", profile, "--format", "json"]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise OpsError("private NPMplus context provider timed out; output redacted") from None
    if result.returncode != 0:
        raise OpsError("private NPMplus context provider failed; output redacted")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise OpsError("private NPMplus context provider returned invalid JSON") from None
    if not isinstance(value, dict):
        raise OpsError("private NPMplus context provider returned invalid context")
    return value


def resolve_private_path(private_repo: Path, raw_path: Any, field_name: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise OpsError(f"context {field_name} must be a relative path")
    candidate = Path(raw_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise OpsError(f"context {field_name} must stay inside the private repo")
    return private_repo / candidate


def validate_env_name(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OpsError(f"context {field_name} must be an environment variable name")
    if not all(part.isalnum() or part == "_" for part in value):
        raise OpsError(f"context {field_name} has an invalid environment variable name")
    if value[0].isdigit():
        raise OpsError(f"context {field_name} has an invalid environment variable name")
    return value


def load_context(private_repo: Path, provider: Path, profile: str) -> NpmplusContext:
    raw_context = run_context_provider(private_repo, provider, profile)
    if raw_context.get("schema_version") != SCHEMA_VERSION:
        raise OpsError("private NPMplus context has an unsupported schema version")

    api = raw_context.get("api")
    if not isinstance(api, dict):
        raise OpsError("private NPMplus context is missing api settings")

    refs = raw_context.get("refs")
    if not isinstance(refs, dict):
        raise OpsError("private NPMplus context is missing refs")
    normalized_refs: dict[str, dict[str, Any]] = {}
    for name, spec in refs.items():
        if not isinstance(name, str) or not PUBLIC_ALIAS_RE.fullmatch(name):
            raise OpsError("private NPMplus context contains an invalid ref name")
        if not isinstance(spec, dict) or spec.get("kind") != "proxy_host":
            raise OpsError(f"private NPMplus context ref {name!r} must be a proxy_host")
        host_id = spec.get("id")
        if not isinstance(host_id, int) or host_id <= 0:
            raise OpsError(f"private NPMplus context ref {name!r} has an invalid id")
        normalized_refs[name] = {"kind": "proxy_host", "id": host_id}

    pilot = raw_context.get("pilot")
    default_pilot_ref = None
    if pilot is not None:
        if not isinstance(pilot, dict):
            raise OpsError("private NPMplus context pilot settings must be an object")
        raw_default_ref = pilot.get("default_ref")
        if raw_default_ref is not None:
            if not isinstance(raw_default_ref, str) or raw_default_ref not in normalized_refs:
                raise OpsError("private NPMplus context pilot default_ref is invalid")
            default_pilot_ref = raw_default_ref

    raw_policy = raw_context.get("policy")
    policy = raw_policy if isinstance(raw_policy, dict) else {}
    raw_allowed = policy.get("allowed_apply_actions", [])
    if not isinstance(raw_allowed, list) or not all(isinstance(item, str) for item in raw_allowed):
        raise OpsError("private NPMplus context allowed_apply_actions must be a list of strings")
    allowed_apply_actions = set(raw_allowed) & ALLOWED_LIFECYCLE_ACTIONS

    return NpmplusContext(
        private_repo=private_repo,
        profile=profile,
        env_file=resolve_private_path(private_repo, api.get("env_file"), "api.env_file"),
        base_url_env=validate_env_name(api.get("base_url_env"), "api.base_url_env"),
        identity_env=validate_env_name(api.get("identity_env"), "api.identity_env"),
        secret_env=validate_env_name(api.get("secret_env"), "api.secret_env"),
        refs=normalized_refs,
        default_pilot_ref=default_pilot_ref,
        allowed_apply_actions=allowed_apply_actions,
    )


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        raise OpsError("private NPMplus env file is not available")

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        if "=" not in line:
            raise OpsError(f"private NPMplus env file has an invalid line: {line_number}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip()
        if not key:
            raise OpsError(f"private NPMplus env file has an empty key: {line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'\"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def validate_proxy_host(host: dict[str, Any]) -> None:
    if not isinstance(host.get("enabled"), bool):
        raise OpsError("NPMplus proxy host response is missing enabled state")


def load_api_config(context: NpmplusContext, timeout: int) -> ApiConfig:
    env_values = parse_env_file(context.env_file)
    merged = dict(env_values)
    for key in (context.base_url_env, context.identity_env, context.secret_env):
        if os.environ.get(key):
            merged[key] = os.environ[key]

    missing = [
        key
        for key in (context.base_url_env, context.identity_env, context.secret_env)
        if not merged.get(key)
    ]
    if missing:
        raise OpsError("private NPMplus API configuration is incomplete")

    base_url = merged[context.base_url_env].rstrip("/")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in VALID_URL_SCHEMES or not parsed.netloc:
        raise OpsError("private NPMplus base URL is invalid")

    return ApiConfig(
        base_url=base_url,
        identity=merged[context.identity_env],
        secret=merged[context.secret_env],
        timeout=timeout,
    )


class NpmplusClient:
    def __init__(self, config: ApiConfig) -> None:
        self.config = config
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> Any:
        url = urllib.parse.urljoin(f"{self.config.base_url}/", path.lstrip("/"))
        payload: bytes | None = None
        headers = {"Accept": "application/json, */*;q=0.8"}
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=payload, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=self.config.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                if not raw.strip():
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            exc.read()
            raise OpsError(f"NPMplus API returned HTTP {exc.code}; response body redacted") from None
        except urllib.error.URLError:
            raise OpsError("NPMplus API request failed; target redacted") from None
        except json.JSONDecodeError:
            raise OpsError("NPMplus API returned invalid JSON") from None

    def authenticate(self) -> dict[str, Any]:
        payload = self.request(
            "POST",
            "/api/tokens",
            body={"identity": self.config.identity, "secret": self.config.secret},
        )
        token_cookie_present = any(cookie.name == "token" for cookie in self.cookies)
        if not token_cookie_present:
            raise OpsError("NPMplus authentication did not return a token cookie")
        return {
            "ok": True,
            "payload_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
            "token_cookie_present": token_cookie_present,
        }

    def get_proxy_host(self, host_id: int) -> dict[str, Any]:
        value = self.request("GET", f"/api/nginx/proxy-hosts/{host_id}")
        if not isinstance(value, dict):
            raise OpsError("NPMplus returned an invalid proxy host response")
        validate_proxy_host(value)
        return value

    def list_proxy_hosts(self) -> list[dict[str, Any]]:
        value = self.request("GET", "/api/nginx/proxy-hosts")
        if not isinstance(value, list):
            raise OpsError("NPMplus returned an invalid proxy host list response")
        invalid_count = sum(1 for item in value if not isinstance(item, dict))
        if invalid_count:
            raise OpsError("NPMplus proxy host list contained invalid items")
        return value

    def lifecycle(self, host_id: int, action: str) -> dict[str, Any]:
        if action not in {"enable", "disable"}:
            raise OpsError("unsupported proxy host lifecycle action")
        result = self.request("POST", f"/api/nginx/proxy-hosts/{host_id}/{action}")
        reread = self.get_proxy_host(host_id)
        expected_enabled = action == "enable"
        if bool(reread.get("enabled")) is not expected_enabled:
            raise OpsError("proxy host lifecycle action did not reach requested state")
        return {
            "endpoint_result_type": type(result).__name__,
            "host": summarize_proxy_host(reread),
            "operation": action,
        }


def summarize_proxy_host(host: dict[str, Any], *, target_ref: str | None = None) -> dict[str, Any]:
    domain_names = host.get("domain_names")
    locations = host.get("locations")
    auth_request = host.get("npmplus_auth_request")
    http3 = host.get("http3_support")
    summary: dict[str, Any] = {
        "access_list_id_present": host.get("access_list_id") not in (None, 0, ""),
        "auth_request": "none" if auth_request in AUTH_REQUEST_NONE else "configured",
        "certificate_id_present": host.get("certificate_id") not in (None, 0, ""),
        "domain_count": len(domain_names) if isinstance(domain_names, list) else 0,
        "enabled": bool(host.get("enabled")),
        "http2": bool(host.get("http2_support")),
        "http3": None if http3 is None else bool(http3),
        "location_count": len(locations) if isinstance(locations, list) else 0,
        "noindex": bool(host.get("npmplus_noindex")),
    }
    if target_ref is not None:
        summary["target_ref"] = target_ref
    return summary


def summarize_proxy_host_inventory(hosts: list[dict[str, Any]]) -> dict[str, Any]:
    enabled = [host for host in hosts if host.get("enabled")]
    configured_auth = [
        host for host in hosts if host.get("npmplus_auth_request") not in AUTH_REQUEST_NONE
    ]
    return {
        "auth_request_configured_count": len(configured_auth),
        "count": len(hosts),
        "disabled_count": len(hosts) - len(enabled),
        "enabled_count": len(enabled),
    }


def resolve_ref(context: NpmplusContext, target_ref: str) -> int:
    ref = context.refs.get(target_ref)
    if ref is None:
        raise OpsError(f"unknown private NPMplus target ref: {target_ref}")
    host_id = ref.get("id")
    if not isinstance(host_id, int) or host_id <= 0:
        raise OpsError("private NPMplus target ref is invalid")
    return host_id


def build_context(args: argparse.Namespace) -> NpmplusContext:
    private_repo = load_local_infra_repo(args.local_context)
    return load_context(private_repo, args.context_provider, args.profile)


def build_client(args: argparse.Namespace, context: NpmplusContext) -> NpmplusClient:
    client = NpmplusClient(load_api_config(context, args.timeout))
    client.authenticate()
    return client


def cmd_context_check(args: argparse.Namespace) -> None:
    context = build_context(args)
    print_json(
        {
            "allowed_apply_actions": sorted(context.allowed_apply_actions),
            "default_pilot_ref_present": context.default_pilot_ref is not None,
            "ok": True,
            "profile": context.profile,
            "ref_count": len(context.refs),
            "schema_version": SCHEMA_VERSION,
        }
    )


def cmd_auth_check(args: argparse.Namespace) -> None:
    context = build_context(args)
    client = NpmplusClient(load_api_config(context, args.timeout))
    print_json(client.authenticate())


def cmd_proxy_host_get(args: argparse.Namespace) -> None:
    context = build_context(args)
    client = build_client(args, context)
    host_id = resolve_ref(context, args.host_ref)
    print_json(summarize_proxy_host(client.get_proxy_host(host_id), target_ref=args.host_ref))


def cmd_proxy_hosts_list(args: argparse.Namespace) -> None:
    context = build_context(args)
    client = build_client(args, context)
    print_json(summarize_proxy_host_inventory(client.list_proxy_hosts()))


def cmd_pilot_status(args: argparse.Namespace) -> None:
    context = build_context(args)
    target_ref = args.host_ref or context.default_pilot_ref
    if target_ref is None:
        raise OpsError("pilot-status requires --host-ref or private default_pilot_ref")
    client = build_client(args, context)
    host_id = resolve_ref(context, target_ref)
    print_json(
        {
            "inventory": summarize_proxy_host_inventory(client.list_proxy_hosts()),
            "target": summarize_proxy_host(client.get_proxy_host(host_id), target_ref=target_ref),
        }
    )


def cmd_lifecycle(args: argparse.Namespace) -> None:
    command_name = f"proxy-host-{args.lifecycle_action}"
    context = build_context(args)
    if args.apply and command_name not in context.allowed_apply_actions:
        raise OpsError("private NPMplus context does not allow this apply action")

    client = build_client(args, context)
    host_id = resolve_ref(context, args.host_ref)
    if not args.apply:
        print_json(
            {
                "apply": False,
                "planned_operation": args.lifecycle_action,
                "target": summarize_proxy_host(client.get_proxy_host(host_id), target_ref=args.host_ref),
            }
        )
        return

    print_json(
        {
            "apply": True,
            **client.lifecycle(host_id, args.lifecycle_action),
        }
    )


def add_context_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        type=public_alias,
        help="private NPMplus profile alias",
    )
    parser.add_argument(
        "--local-context",
        type=Path,
        default=LOCAL_CONTEXT_PATH,
        help="local context TOML containing [docs].local_infra",
    )
    parser.add_argument(
        "--context-provider",
        type=Path,
        default=DEFAULT_CONTEXT_PROVIDER,
        help="private repo relative context provider path",
    )


def add_api_args(parser: argparse.ArgumentParser) -> None:
    add_context_args(parser)
    parser.add_argument("--timeout", type=positive_int, default=DEFAULT_TIMEOUT_SECONDS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generic redacted NPMplus operations engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    context_check = subparsers.add_parser("context-check", help="validate private NPMplus context")
    add_context_args(context_check)
    context_check.set_defaults(func=cmd_context_check)

    auth = subparsers.add_parser("auth-check", help="authenticate using private NPMplus context")
    add_api_args(auth)
    auth.set_defaults(func=cmd_auth_check)

    get_host = subparsers.add_parser("proxy-host-get", help="read a redacted proxy host summary")
    add_api_args(get_host)
    get_host.add_argument(
        "--host-ref",
        required=True,
        type=public_alias,
        help="private context target ref",
    )
    get_host.set_defaults(func=cmd_proxy_host_get)

    list_hosts = subparsers.add_parser("proxy-hosts-list", help="read redacted proxy host inventory")
    add_api_args(list_hosts)
    list_hosts.set_defaults(func=cmd_proxy_hosts_list)

    pilot = subparsers.add_parser("pilot-status", help="read redacted inventory and pilot target summaries")
    add_api_args(pilot)
    pilot.add_argument(
        "--host-ref",
        type=public_alias,
        help="private context target ref; defaults to private pilot default",
    )
    pilot.set_defaults(func=cmd_pilot_status)

    for action in ("enable", "disable"):
        lifecycle = subparsers.add_parser(
            f"proxy-host-{action}",
            help=f"{action} a private proxy host ref only when --apply is present",
        )
        add_api_args(lifecycle)
        lifecycle.add_argument(
            "--host-ref",
            required=True,
            type=public_alias,
            help="private context target ref",
        )
        lifecycle.add_argument("--apply", action="store_true", help="perform the lifecycle write")
        lifecycle.set_defaults(func=cmd_lifecycle, lifecycle_action=action)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except OpsError as exc:
        fail(str(exc))
    except Exception:
        fail("unexpected NPMplus engine failure; details redacted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
