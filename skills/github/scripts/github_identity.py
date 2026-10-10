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
import re
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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Iterator

import fcntl


class GitHubAppError(RuntimeError):
    """A safe-to-display GitHub App configuration or token error."""


class ContributorRepository(GitHubAppError):
    """The App is not installed on a repository of an account that did not register it.

    There the supported identity is the person's own GitHub login, used only
    when the caller opts in for the command with OWN_USER_OPT_IN.
    """

    def __init__(self, repository: str) -> None:
        super().__init__(
            f"the GitHub App is not installed on {repository}, which belongs to another "
            "account than the one that registered the App"
        )
        self.repository = repository


class NotInstalledForAutomation(GitHubAppError):
    """The App is not installed on a repository of the account that registered it."""


# app-auth exits with these statuses so the shell wrappers can tell the cases
# apart: the person's own login (a ContributorRepository), or a definite
# refusal in the registering account's repository (NotInstalledForAutomation),
# as opposed to a failed lookup (1).
CONTRIBUTOR_EXIT_STATUS = 3
NOT_INSTALLED_EXIT_STATUS = 4

# Acting as the person's own GitHub user is a stop the agent asks its Director
# about, so it runs only when the caller sets this for the command; a value in
# local.env does not count.
OWN_USER_OPT_IN = "GH_WITH_ENV_TOKEN_OWN_USER"


class GitHubAppHTTPError(GitHubAppError):
    def __init__(self, message: str, status: int, location: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.location = location


REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.\.?$)[A-Za-z0-9._-]{1,100}")


@dataclass(frozen=True)
class GitHubAppConfig:
    app_id: str
    installation_id: str
    private_key_path: pathlib.Path
    api_url: str
    cache_dir: pathlib.Path
    require_repository_installation: bool = False


def env_file_path(environ: Mapping[str, str] | None = None) -> pathlib.Path | None:
    """Return the local.env the shell wrappers load, or where setup should create one.

    An explicit CODEX_SKILLS_ENV_FILE always wins. Otherwise the first existing
    file under CODE_HOME, CODEX_HOME, then ~/.code wins, and the first candidate
    is returned when none exists yet.
    """
    values = os.environ if environ is None else environ
    if values.get("CODEX_SKILLS_ENV_FILE"):
        return pathlib.Path(values["CODEX_SKILLS_ENV_FILE"]).expanduser()
    candidates = [
        pathlib.Path(values[name]).expanduser() / "local.env"
        for name in ("CODE_HOME", "CODEX_HOME")
        if values.get(name)
    ]
    if values.get("HOME"):
        candidates.append(pathlib.Path(values["HOME"]).expanduser() / ".code" / "local.env")
    return next((path for path in candidates if path.is_file()), candidates[0] if candidates else None)


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
            parsed = shlex.split(raw_value, comments=True)
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


def github_app_prefix(repository: str | None = None, environ: Mapping[str, str] | None = None, *, reader: bool = False) -> str:
    """Choose credentials by repository owner; the bulk reader keeps its own route."""
    if reader:
        return "GITHUB_READER_APP"
    owners = configured_value("GITHUB_CLIENT_APP_OWNERS", environ=environ)
    if repository and owners:
        if not REPOSITORY_PATTERN.fullmatch(repository):
            raise GitHubAppError(f"invalid repository {repository!r}; expected OWNER/REPO")
        if repository.split("/", 1)[0].casefold() in {owner.casefold() for owner in owners.replace(",", " ").split()}:
            return "GITHUB_CLIENT_APP"
    return "GITHUB_APP"


def repository_context() -> str | None:
    """Normalize GH_REPO the same way as the shell wrapper's repository reference."""
    reference = configured_value("GH_REPO")
    if not reference:
        return None
    reference = reference.rstrip("/").removesuffix(".git")
    if reference.startswith("git@"):
        reference = reference.split(":", 1)[-1]
    return "/".join(reference.split("/")[-2:])


def github_app_config(environ: Mapping[str, str] | None = None, *, reader: bool = False,
                      repository: str | None = None) -> GitHubAppConfig | None:
    values = os.environ if environ is None else environ
    local_values = load_local_env(values)
    prefix = github_app_prefix(repository, values, reader=reader)
    names = tuple(f"{prefix}_{suffix}" for suffix in ("ID", "INSTALLATION_ID", "PRIVATE_KEY_PATH"))
    configured = {
        name: configured_value(name, environ=values, local_env=local_values)
        for name in names
    }
    present = [name for name, value in configured.items() if value]
    client = prefix == "GITHUB_CLIENT_APP"
    if not present and not client:
        return None
    required_names = (names[0], names[2]) if client else names
    if any(not configured[name] for name in required_names):
        missing = ", ".join(name for name in required_names if not configured[name])
        raise GitHubAppError(f"incomplete GitHub App configuration; missing {missing}")

    key_path = pathlib.Path(str(configured[f"{prefix}_PRIVATE_KEY_PATH"])).expanduser()
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

    configured_api_url = configured_value(f"{prefix}_API_URL", environ=values, local_env=local_values)
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
        f"{prefix}_TOKEN_CACHE_DIR",
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
        app_id=str(configured[f"{prefix}_ID"]),
        installation_id=str(configured[f"{prefix}_INSTALLATION_ID"] or ""),
        private_key_path=key_path,
        api_url=api_url,
        cache_dir=cache_dir,
        require_repository_installation=client,
    )


def reader_config_available() -> bool:
    """Presence only; validation and minting remain in the existing App path."""
    local_values = load_local_env()
    return all(configured_value(f"GITHUB_READER_APP_{suffix}", local_env=local_values)
               for suffix in ("ID", "INSTALLATION_ID", "PRIVATE_KEY_PATH"))


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
    return int.from_bytes(data[offset : offset + count]), offset + count


def _der_value(data: bytes, offset: int, expected_tag: int | None = None) -> tuple[int, bytes, int]:
    if offset >= len(data):
        raise GitHubAppError("invalid GitHub App private key")
    tag = data[offset]
    length, start = _der_length(data, offset + 1)
    end = start + length
    if end > len(data) or (expected_tag is not None and tag != expected_tag):
        raise GitHubAppError("invalid GitHub App private key")
    return tag, data[start:end], end


def _rsa_sequence(der: bytes) -> tuple[bytes, int]:
    _, sequence, end = _der_value(der, 0, 0x30)
    if end != len(der):
        raise GitHubAppError("invalid GitHub App private key")
    _, version, offset = _der_value(sequence, 0, 0x02)
    if int.from_bytes(version) != 0:
        raise GitHubAppError("unsupported GitHub App private key version")
    return sequence, offset


def _rsa_private_numbers(pem: bytes) -> tuple[int, int]:
    lines = [line.strip() for line in pem.splitlines() if not line.startswith(b"-----")]
    try:
        der = base64.b64decode(b"".join(lines), validate=True)
        sequence, offset = _rsa_sequence(der)
        tag, _, next_offset = _der_value(sequence, offset)
        if tag == 0x30:  # PKCS#8 wraps the PKCS#1 key in an octet string.
            _, wrapped, _ = _der_value(sequence, next_offset, 0x04)
            sequence, offset = _rsa_sequence(wrapped)
        _, modulus_bytes, offset = _der_value(sequence, offset, 0x02)
        _, _, offset = _der_value(sequence, offset, 0x02)
        _, private_exponent_bytes, _ = _der_value(sequence, offset, 0x02)
        return int.from_bytes(modulus_bytes), int.from_bytes(private_exponent_bytes)
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
    signature = pow(int.from_bytes(encoded), private_exponent, modulus).to_bytes(key_bytes)
    return f"{header}.{claims}.{_b64url(signature)}"


def _parse_expiry(value: str) -> int:
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).timestamp())
    except (TypeError, ValueError) as error:
        raise GitHubAppError("GitHub App token response has an invalid expiry") from error


def _cache_key(config: GitHubAppConfig, repository: str | None = None) -> str:
    key_fingerprint = hashlib.sha256(config.private_key_path.read_bytes()).hexdigest()
    installation = f"repository:{repository.casefold()}" if repository else config.installation_id
    material = "\0".join((config.api_url, config.app_id, installation, key_fingerprint))
    return hashlib.sha256(material.encode()).hexdigest()


@contextmanager
def _locked_cache(config: GitHubAppConfig, repository: str | None = None) -> Iterator[pathlib.Path]:
    try:
        cache_stat = config.cache_dir.lstat()
    except FileNotFoundError:
        config.cache_dir.mkdir(mode=0o700, parents=True)
        cache_stat = config.cache_dir.lstat()
    if not stat.S_ISDIR(cache_stat.st_mode) or cache_stat.st_uid != os.getuid():
        raise GitHubAppError("GitHub App token cache must be an owner-controlled directory")
    os.chmod(config.cache_dir, 0o700)
    cache_path = config.cache_dir / f"{_cache_key(config, repository)}.json"
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
        location = error.headers.get("Location") if error.headers else None
        raise GitHubAppHTTPError(
            f"GitHub App {operation} failed with HTTP {error.code}", error.code, location
        ) from error
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


def github_app_headers(config: GitHubAppConfig, *, now: int | None = None) -> dict[str, str]:
    """App JWT headers for verified setup/identity endpoints; never print them."""
    return _app_headers(config, now=int(time.time() if now is None else now))


def github_app_request(request: urllib.request.Request, *, operation: str) -> object:
    """Shared no-redirect JSON transport; single attempt, including manifest conversion."""
    return _request_json(request, operation=operation)


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


def _installation_payload(config: GitHubAppConfig, *, now: int) -> dict[str, Any]:
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
) -> dict[str, Any]:
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


def repository_installation_config(
    config: GitHubAppConfig, repository: str, *, now: int | None = None, required: bool = True
) -> GitHubAppConfig | None:
    """The App configuration for whichever installation covers the repository.

    Each owner installs the App separately, so a repository outside the
    configured installation's account needs that owner's installation. When
    none covers it, return None; when one is required (writes), refuse on a
    repository of the account that registered the App, and raise
    ContributorRepository elsewhere, where the person may act as their own
    GitHub user. An installation on another account's repositories does not
    make that account's other repositories the automation's.
    """
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise GitHubAppError(f"invalid repository {repository!r}; expected OWNER/REPO")
    current_time = int(time.time() if now is None else now)
    url = f"{config.api_url}/repos/{repository}/installation"
    for attempt in range(2):
        request = urllib.request.Request(url, method="GET", headers=_app_headers(config, now=current_time))
        try:
            payload = _request_json(request, operation="repository installation lookup")
            break
        except GitHubAppHTTPError as error:
            if error.status == 404 and not required:
                return None
            if error.status == 404 and attempt:
                # The installation lookup followed a rename or transfer, so the
                # account in the name given may no longer own the repository.
                raise GitHubAppError(
                    f"{repository} has moved and the GitHub App is not installed on it; "
                    "use the repository's current OWNER/REPO"
                ) from error
            if error.status == 404:
                owner = repository.split("/", 1)[0].casefold()
                if owner != registering_account(config, now=current_time):
                    raise ContributorRepository(repository) from error
                raise NotInstalledForAutomation(
                    f"the GitHub App is not installed on {repository}; "
                    "its owner must install the App there before automation can use it"
                ) from error
            # A renamed or transferred repository redirects to its ID; follow
            # that once, and only to the same API's repository installation.
            moved = _renamed_repository_installation_url(config, error.location)
            if attempt or error.status not in (301, 302, 307, 308) or moved is None:
                raise
            url = moved
    installation_id = payload.get("id") if isinstance(payload, dict) else None
    app_id = payload.get("app_id") if isinstance(payload, dict) else None
    if not isinstance(installation_id, int) or str(app_id) != config.app_id:
        raise GitHubAppError("GitHub App repository installation lookup returned the wrong installation")
    return replace(config, installation_id=str(installation_id))


def registering_account(config: GitHubAppConfig, *, now: int | None = None) -> str:
    """The account that registered the App, case-folded.

    A repository under it is the Director's, so a missing installation there is
    a refusal; anywhere else it means the person's own login.
    """
    current_time = int(time.time() if now is None else now)
    app = _request_json(
        urllib.request.Request(f"{config.api_url}/app", method="GET", headers=_app_headers(config, now=current_time)),
        operation="identity request",
    )
    owner = app.get("owner") if isinstance(app, dict) else None
    if not isinstance(owner, dict) or not isinstance(owner.get("login"), str) or not owner["login"]:
        raise GitHubAppError("GitHub App identity response is missing the App owner")
    return owner["login"].casefold()


def own_user_opted_in(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the caller opted in to acting as their own GitHub user for this command."""
    values = os.environ if environ is None else environ
    return values.get(OWN_USER_OPT_IN, "").casefold() in {"1", "true", "yes"}


def acts_as_own_user(repository: str, environ: Mapping[str, str] | None = None) -> bool:
    """Whether writes to the repository run as the person's own GitHub login.

    True only when the caller opted in for the command, a GitHub App is
    configured, it is not installed on the repository, and the account that
    registered it does not own the repository. Any other outcome, including a
    failed lookup, is False, and the write helpers then use the App or refuse.
    """
    values = os.environ if environ is None else environ
    if not own_user_opted_in(values):
        return False
    try:
        if str(configured_value("GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH", environ=values) or "").casefold() in {
            "1",
            "true",
            "yes",
        }:
            return False
        config = github_app_config(values, repository=repository)
        if config is None:
            return False
        repository_installation_config(config, repository)
    except ContributorRepository:
        return True
    except GitHubAppError:
        return False
    return False


def _renamed_repository_installation_url(config: GitHubAppConfig, location: str | None) -> str | None:
    if not location:
        return None
    api = urllib.parse.urlsplit(config.api_url)
    target = urllib.parse.urlsplit(urllib.parse.urljoin(f"{config.api_url}/", location))
    expected_path = re.fullmatch(rf"{re.escape(api.path)}/repositories/[0-9]+/installation", target.path)
    if (target.scheme, target.netloc) != (api.scheme, api.netloc) or not expected_path or target.query or target.fragment:
        return None
    return urllib.parse.urlunsplit(target)


def github_app_auth(
    config: GitHubAppConfig,
    *,
    now: int | None = None,
    refresh: bool = False,
    repository: str | None = None,
    require_installation: bool = False,
) -> tuple[str, str]:
    """Mint or reuse an installation token, for the repository's owner when given.

    Without an installation on the repository, reads use the configured
    installation (public repositories stay readable); writes are refused, or
    raise ContributorRepository outside the App's registering account.
    """
    current_time = int(time.time() if now is None else now)
    if repository:
        with _locked_cache(config, repository) as cache_path:
            cached = None if refresh else _read_cached_token(cache_path, now=current_time)
            if cached:
                return cached
            repository_config = repository_installation_config(
                config, repository, now=current_time, required=require_installation
            )
            if repository_config is not None:
                return _mint_and_cache(repository_config, cache_path, current_time)
            if config.require_repository_installation:
                raise GitHubAppError(f"the GitHub Client App is not installed on {repository}; reads require its installation on this repository")
    with _locked_cache(config) as cache_path:
        cached = None if refresh else _read_cached_token(cache_path, now=current_time)
        if cached:
            return cached
        return _mint_and_cache(config, cache_path, current_time)


def _mint_and_cache(config: GitHubAppConfig, cache_path: pathlib.Path, current_time: int) -> tuple[str, str]:
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
    parser.add_argument("--reader", action="store_true", help="Use the configured read-only App.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    app_auth = subparsers.add_parser("app-auth", help="Resolve the App bot login and installation token.")
    app_auth.add_argument(
        "--repo",
        help="OWNER/REPO whose installation to use; defaults to the configured installation.",
    )
    app_prefix = subparsers.add_parser("app-prefix", help="Print the credential prefix for a repository.")
    app_prefix.add_argument("--repo")
    app_auth.add_argument(
        "--require-installation",
        action="store_true",
        help=(
            "Refuse when no installation covers --repo instead of using the configured one (writes); "
            f"exit {CONTRIBUTOR_EXIT_STATUS} when the account that registered the App does not own --repo."
        ),
    )
    app_check = subparsers.add_parser("app-check", help="Verify the configured App installation and print its bot login.")
    app_check.add_argument("--repo", help="Verify the installation on OWNER/REPO.")
    subparsers.add_parser("app-token", help="Mint or reuse a GitHub App installation token.")
    args = parser.parse_args()
    try:
        if args.command == "app-prefix":
            print(github_app_prefix(args.repo, reader=args.reader))
            return 0
        if args.command in {"app-auth", "app-check", "app-token"}:
            config = github_app_config(reader=args.reader, repository=getattr(args, "repo", None))
            if config is None:
                raise GitHubAppError("GitHub App authentication is not configured")
            if args.command == "app-check":
                if args.repo:
                    repository_config = repository_installation_config(config, args.repo)
                    assert repository_config is not None
                    config = repository_config
                print(check_github_app_installation(config))
                return 0
            token, login = github_app_auth(
                config,
                repository=getattr(args, "repo", None) or None,
                require_installation=getattr(args, "require_installation", False),
            )
            if args.command == "app-auth":
                print(login)
            print(token)
            return 0
    except ContributorRepository as error:
        print(f"note: {error}", file=sys.stderr)
        return CONTRIBUTOR_EXIT_STATUS
    except NotInstalledForAutomation as error:
        print(f"error: GitHub App authentication failed before gh invocation: {error}", file=sys.stderr)
        return NOT_INSTALLED_EXIT_STATUS
    except GitHubAppError as error:
        print(f"error: GitHub App authentication failed before gh invocation: {error}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
