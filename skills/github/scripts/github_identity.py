#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Small shared identity/configuration helpers for GitHub skills."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import shlex
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterator

import fcntl


class GitHubAppError(RuntimeError):
    """A safe-to-display GitHub App configuration or token error."""


@dataclass(frozen=True)
class GitHubAppConfig:
    app_id: str
    installation_id: str
    private_key_path: pathlib.Path
    api_url: str
    cache_dir: pathlib.Path


def env_file_path(environ: Mapping[str, str] | None = None) -> pathlib.Path | None:
    values = os.environ if environ is None else environ
    if values.get("CODEX_SKILLS_ENV_FILE"):
        return pathlib.Path(values["CODEX_SKILLS_ENV_FILE"]).expanduser()
    if values.get("CODE_HOME"):
        return pathlib.Path(values["CODE_HOME"]).expanduser() / "local.env"
    if values.get("CODEX_HOME"):
        return pathlib.Path(values["CODEX_HOME"]).expanduser() / "local.env"
    if values.get("HOME"):
        return pathlib.Path(values["HOME"]).expanduser() / ".code" / "local.env"
    return None


def load_local_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    path = env_file_path(environ)
    if path is None or not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
            continue
        try:
            parsed = shlex.split(raw_value, comments=True, posix=True)
        except ValueError:
            continue
        if len(parsed) > 1 or "$" in raw_value or "`" in raw_value:
            continue
        values[key] = parsed[0] if parsed else ""
    return values


def configured_value(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
    local_env: Mapping[str, str] | None = None,
) -> str | None:
    values = os.environ if environ is None else environ
    local_values = local_env if local_env is not None else load_local_env(values)
    merged = dict(values)
    merged.update(local_values)
    value = merged.get(name)
    value = str(value or "").strip()
    return value or None


def automation_login(environ: Mapping[str, str] | None = None) -> str | None:
    values = os.environ if environ is None else environ
    local_values = load_local_env(values)
    return configured_value(
        "GH_WITH_ENV_TOKEN_EXPECTED_LOGIN",
        environ=values,
        local_env=local_values,
    ) or configured_value(
        "CODEX_AUTOMATION_LOGIN",
        environ=values,
        local_env=local_values,
    )


def configured_bot_logins(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    raw = configured_value("CODEX_AUTOMATION_BOT_LOGINS", environ=environ)
    if not raw:
        return ()
    return tuple(dict.fromkeys(item.strip() for item in raw.replace(",", " ").split() if item.strip()))


def active_auth_fallback_allowed(environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    if str(values.get("GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH") or "").casefold() in {
        "1",
        "true",
        "yes",
    }:
        return False
    local_values = load_local_env(values)
    merged = dict(values)
    merged.update(local_values)
    if str(merged.get("GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH") or "").casefold() in {
        "1",
        "true",
        "yes",
    }:
        return False
    raw = merged.get("GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK")
    return str(raw or "").casefold() in {
        "1",
        "true",
        "yes",
    }


def github_app_config(environ: Mapping[str, str] | None = None) -> GitHubAppConfig | None:
    values = os.environ if environ is None else environ
    local_values = load_local_env(values)
    names = (
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
    )
    configured = {
        name: configured_value(name, environ=values, local_env=local_values)
        for name in names
    }
    present = [name for name, value in configured.items() if value]
    if not present:
        return None
    if len(present) != len(names):
        missing = ", ".join(name for name in names if not configured[name])
        raise GitHubAppError(f"incomplete GitHub App configuration; missing {missing}")

    key_path = pathlib.Path(str(configured["GITHUB_APP_PRIVATE_KEY_PATH"])).expanduser()
    try:
        key_stat = key_path.lstat()
    except FileNotFoundError as error:
        raise GitHubAppError("GitHub App private key path is not a regular file") from error
    if not stat.S_ISREG(key_stat.st_mode):
        raise GitHubAppError("GitHub App private key path is not a regular file")
    if key_stat.st_uid != os.getuid():
        raise GitHubAppError("GitHub App private key must be owned by the current user")
    key_mode = stat.S_IMODE(key_stat.st_mode)
    if key_mode & 0o077:
        raise GitHubAppError("GitHub App private key must be owner-readable only (mode 600)")

    configured_api_url = configured_value("GITHUB_APP_API_URL", environ=values, local_env=local_values)
    generic_api_url = configured_value("GITHUB_API_URL", environ=values, local_env=local_values)
    gh_host = configured_value("GH_HOST", environ=values, local_env=local_values)
    if not configured_api_url and not generic_api_url and gh_host and gh_host != "github.com":
        raise GitHubAppError("set GITHUB_APP_API_URL when GH_HOST is not github.com")
    api_url = (configured_api_url or generic_api_url or "https://api.github.com").rstrip("/")
    parsed_api_url = urllib.parse.urlsplit(api_url)
    loopback = parsed_api_url.hostname in {"127.0.0.1", "::1", "localhost"}
    if (
        parsed_api_url.scheme not in ({"http", "https"} if loopback else {"https"})
        or not parsed_api_url.hostname
        or parsed_api_url.username
        or parsed_api_url.password
        or parsed_api_url.query
        or parsed_api_url.fragment
    ):
        raise GitHubAppError("GitHub App API URL must be HTTPS (or loopback HTTP for tests)")
    cache_override = configured_value(
        "GITHUB_APP_TOKEN_CACHE_DIR",
        environ=values,
        local_env=local_values,
    )
    if cache_override:
        cache_dir = pathlib.Path(cache_override).expanduser()
    elif values.get("CODE_HOME"):
        cache_dir = pathlib.Path(values["CODE_HOME"]).expanduser() / "state" / "github-app-token"
    elif values.get("CODEX_HOME"):
        cache_dir = pathlib.Path(values["CODEX_HOME"]).expanduser() / "state" / "github-app-token"
    elif values.get("HOME"):
        cache_dir = pathlib.Path(values["HOME"]).expanduser() / ".code" / "state" / "github-app-token"
    else:
        raise GitHubAppError("cannot resolve GitHub App token cache without a Code home")

    return GitHubAppConfig(
        app_id=str(configured["GITHUB_APP_ID"]),
        installation_id=str(configured["GITHUB_APP_INSTALLATION_ID"]),
        private_key_path=key_path,
        api_url=api_url,
        cache_dir=cache_dir,
    )


def _b64url(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _der_length(data: bytes, offset: int) -> tuple[int, int]:
    first = data[offset]
    offset += 1
    if first < 0x80:
        return first, offset
    count = first & 0x7F
    if count == 0 or count > 4 or offset + count > len(data):
        raise GitHubAppError("unsupported GitHub App private key encoding")
    return int.from_bytes(data[offset : offset + count], "big"), offset + count


def _der_value(data: bytes, offset: int, expected_tag: int | None = None) -> tuple[int, bytes, int]:
    if offset >= len(data):
        raise GitHubAppError("invalid GitHub App private key")
    tag = data[offset]
    length, start = _der_length(data, offset + 1)
    end = start + length
    if end > len(data) or (expected_tag is not None and tag != expected_tag):
        raise GitHubAppError("invalid GitHub App private key")
    return tag, data[start:end], end


def _rsa_private_numbers(pem: bytes) -> tuple[int, int]:
    lines = [line.strip() for line in pem.splitlines() if not line.startswith(b"-----")]
    try:
        der = base64.b64decode(b"".join(lines), validate=True)
        _, sequence, end = _der_value(der, 0, 0x30)
        if end != len(der):
            raise GitHubAppError("invalid GitHub App private key")
        _, first, offset = _der_value(sequence, 0, 0x02)
        if int.from_bytes(first, "big") != 0:
            raise GitHubAppError("unsupported GitHub App private key version")
        tag, second, _ = _der_value(sequence, offset)
        if tag == 0x30:  # PKCS#8 wraps the PKCS#1 key in an octet string.
            _, wrapped, _ = _der_value(sequence, _der_value(sequence, offset)[2], 0x04)
            _, sequence, wrapped_end = _der_value(wrapped, 0, 0x30)
            if wrapped_end != len(wrapped):
                raise GitHubAppError("invalid GitHub App private key")
            _, first, offset = _der_value(sequence, 0, 0x02)
            if int.from_bytes(first, "big") != 0:
                raise GitHubAppError("unsupported GitHub App private key version")
        _, modulus_bytes, offset = _der_value(sequence, offset, 0x02)
        _, _, offset = _der_value(sequence, offset, 0x02)
        _, private_exponent_bytes, _ = _der_value(sequence, offset, 0x02)
        return int.from_bytes(modulus_bytes, "big"), int.from_bytes(private_exponent_bytes, "big")
    except (IndexError, ValueError) as error:
        raise GitHubAppError("invalid GitHub App private key") from error


def github_app_jwt(config: GitHubAppConfig, *, now: int | None = None) -> str:
    issued_at = int(time.time() if now is None else now)
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64url(
        json.dumps(
            {"iat": issued_at - 60, "exp": issued_at + 9 * 60, "iss": config.app_id},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode("ascii")
    modulus, private_exponent = _rsa_private_numbers(config.private_key_path.read_bytes())
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(signing_input).digest()
    key_bytes = (modulus.bit_length() + 7) // 8
    padding_size = key_bytes - len(digest_info) - 3
    if padding_size < 8:
        raise GitHubAppError("GitHub App RSA private key is too small")
    encoded = b"\x00\x01" + (b"\xff" * padding_size) + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded, "big"), private_exponent, modulus).to_bytes(key_bytes, "big")
    return f"{header}.{claims}.{_b64url(signature)}"


def _parse_expiry(value: str) -> int:
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).timestamp())
    except (TypeError, ValueError) as error:
        raise GitHubAppError("GitHub App token response has an invalid expiry") from error


def _cache_key(config: GitHubAppConfig) -> str:
    key_fingerprint = hashlib.sha256(config.private_key_path.read_bytes()).hexdigest()
    material = "\0".join((config.api_url, config.app_id, config.installation_id, key_fingerprint))
    return hashlib.sha256(material.encode()).hexdigest()


@contextmanager
def _locked_cache(config: GitHubAppConfig) -> Iterator[pathlib.Path]:
    try:
        cache_stat = config.cache_dir.lstat()
    except FileNotFoundError:
        config.cache_dir.mkdir(mode=0o700, parents=True)
        cache_stat = config.cache_dir.lstat()
    if not stat.S_ISDIR(cache_stat.st_mode) or cache_stat.st_uid != os.getuid():
        raise GitHubAppError("GitHub App token cache must be an owner-controlled directory")
    os.chmod(config.cache_dir, 0o700)
    cache_path = config.cache_dir / f"{_cache_key(config)}.json"
    lock_path = cache_path.with_suffix(".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "r+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if cache_path.exists():
            cache_stat = cache_path.lstat()
            if not stat.S_ISREG(cache_stat.st_mode) or cache_stat.st_uid != os.getuid():
                raise GitHubAppError("GitHub App token cache file is not owner-controlled")
            os.chmod(cache_path, 0o600)
        yield cache_path


def _read_cached_token(cache_path: pathlib.Path, *, now: int) -> tuple[str, str] | None:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        token = payload.get("token")
        login = payload.get("login")
        expires_at = int(payload.get("expires_at", 0))
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    if token and login and expires_at > now + 300:
        return str(token), str(login)
    return None


def _write_cached_token(cache_path: pathlib.Path, token: str, login: str, expires_at: int) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{cache_path.name}.", dir=cache_path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {"token": token, "login": login, "expires_at": expires_at},
                stream,
                separators=(",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, cache_path)
        os.chmod(cache_path, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _request_json(request: urllib.request.Request, *, operation: str) -> object:
    request = urllib.request.Request(
        request.full_url,
        data=request.data,
        method=request.method,
        headers=dict(request.header_items()),
    )
    try:
        with urllib.request.build_opener(_NoRedirectHandler()).open(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise GitHubAppError(f"GitHub App {operation} failed with HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise GitHubAppError(f"GitHub App {operation} failed") from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise GitHubAppError(f"GitHub App {operation} response was not valid JSON") from error
    return payload


def _app_headers(config: GitHubAppConfig, *, now: int) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_app_jwt(config, now=now)}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "codex-skills-gh-with-env-token",
    }


def _request_app_login(config: GitHubAppConfig, *, now: int) -> str:
    request = urllib.request.Request(
        f"{config.api_url}/app",
        method="GET",
        headers=_app_headers(config, now=now),
    )
    payload = _request_json(request, operation="identity request")
    slug = payload.get("slug") if isinstance(payload, dict) else None
    if not isinstance(slug, str) or not slug:
        raise GitHubAppError("GitHub App identity response is missing the App slug")
    return f"{slug}[bot]"


def _installation_payload(config: GitHubAppConfig, *, now: int) -> dict[str, object]:
    request = urllib.request.Request(
        f"{config.api_url}/app/installations/{config.installation_id}",
        method="GET",
        headers=_app_headers(config, now=now),
    )
    payload = _request_json(request, operation="installation check")
    if not isinstance(payload, dict):
        raise GitHubAppError("GitHub App installation check returned an invalid response")
    installation_id = payload.get("id")
    app_id = payload.get("app_id")
    slug = payload.get("app_slug")
    if str(installation_id) != config.installation_id or str(app_id) != config.app_id:
        raise GitHubAppError("GitHub App installation check returned the wrong installation")
    if not isinstance(slug, str) or not slug:
        raise GitHubAppError("GitHub App installation check is missing the App slug")
    return payload


def _check_app_installation(config: GitHubAppConfig, *, now: int) -> str:
    return f"{_installation_payload(config, now=now)['app_slug']}[bot]"


def github_app_installation_metadata(
    config: GitHubAppConfig, *, now: int | None = None
) -> dict[str, object]:
    """Read verified installation grants without exposing credentials or tokens."""
    payload = _installation_payload(config, now=int(time.time() if now is None else now))
    permissions = payload.get("permissions")
    account = payload.get("account")
    if (
        not isinstance(permissions, dict)
        or any(not isinstance(key, str) or not key or not isinstance(value, str)
               or value not in {"read", "write", "admin"}
               for key, value in permissions.items())
        or not isinstance(account, dict)
        or not isinstance(account.get("login"), str)
        or not account.get("login")
        or account.get("type") not in ("User", "Organization")
        or payload.get("repository_selection") not in ("all", "selected")
    ):
        raise GitHubAppError("GitHub App installation metadata is malformed")
    return {
        "app_id": payload["app_id"],
        "installation_id": payload["id"],
        "actor": f"{payload['app_slug']}[bot]",
        "account": {"login": account["login"], "type": account["type"]},
        "permissions": dict(permissions),
        "repository_selection": payload["repository_selection"],
        "suspended": payload.get("suspended_at") is not None,
    }


def _request_installation_token(config: GitHubAppConfig, *, now: int) -> tuple[str, int]:
    endpoint = f"{config.api_url}/app/installations/{config.installation_id}/access_tokens"
    request = urllib.request.Request(
        endpoint,
        data=b"{}",
        method="POST",
        headers=_app_headers(config, now=now),
    )
    payload = _request_json(request, operation="token request")
    token = payload.get("token") if isinstance(payload, dict) else None
    expires_at = payload.get("expires_at") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token or not isinstance(expires_at, str):
        raise GitHubAppError("GitHub App token response is missing token or expiry")
    return token, _parse_expiry(expires_at)


def github_app_auth(
    config: GitHubAppConfig, *, now: int | None = None, refresh: bool = False
) -> tuple[str, str]:
    current_time = int(time.time() if now is None else now)
    with _locked_cache(config) as cache_path:
        cached = None if refresh else _read_cached_token(cache_path, now=current_time)
        if cached:
            return cached
        login = _request_app_login(config, now=current_time)
        token, expires_at = _request_installation_token(config, now=current_time)
        if expires_at <= current_time + 300:
            raise GitHubAppError("GitHub App token response expires too soon")
        _write_cached_token(cache_path, token, login, expires_at)
        return token, login


def github_app_installation_token(config: GitHubAppConfig, *, now: int | None = None) -> str:
    return github_app_auth(config, now=now)[0]


def check_github_app_installation(config: GitHubAppConfig, *, now: int | None = None) -> str:
    return _check_app_installation(config, now=int(time.time() if now is None else now))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Resolve shared GitHub automation identity.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("app-auth", help="Resolve the App bot login and installation token.")
    subparsers.add_parser("app-check", help="Verify the configured App installation and print its bot login.")
    subparsers.add_parser("app-token", help="Mint or reuse a GitHub App installation token.")
    args = parser.parse_args()
    try:
        if args.command in {"app-auth", "app-check", "app-token"}:
            config = github_app_config()
            if config is None:
                raise GitHubAppError("GitHub App authentication is not configured")
            if args.command == "app-check":
                print(check_github_app_installation(config))
                return 0
            token, login = github_app_auth(config)
            if args.command == "app-auth":
                print(login)
            print(token)
            return 0
    except GitHubAppError as error:
        print(f"error: GitHub App authentication failed before gh invocation: {error}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
