"""Shared Launchplane helper URL and public-output safety utilities."""
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import ipaddress
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


TOKEN_PATTERNS = (
    re.compile(r"ghp_[A-Za-z0-9_]+"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"sk-[A-Za-z0-9_-]+"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
)

DENIED_KEYS = {
    "authorization",
    "client_secret",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "headers",
    "opaque",
    "opaque_value",
    "payload",
    "plaintext",
    "plaintext_value",
    "private_key",
    "provider_environment",
    "raw_request",
    "request",
    "request_body",
    "secret",
    "token",
    "tokens",
    "value",
    "values",
    "ciphertext",
    "github_api_base_url",
}
DENIED_KEY_FRAGMENTS = (
    "apikey",
    "clientsecret",
    "credential",
    "masterkey",
    "password",
    "privatekey",
    "secret",
    "token",
)
SAFE_SECRET_METADATA_KEYS = {
    "managedsecretbindingkeys",
    "secretevidence",
    "secretchangecount",
    "secretbinding",
    "secretbindingcount",
    "secretbindingkeys",
    "secretbindings",
    "secrets",
}
SUMMARY_VALUE_DENYLIST = (
    "api_key",
    "apikey",
    "bearer ",
    "client_secret",
    "clientsecret",
    "cookie",
    "credential",
    "opaque_value",
    "private_key",
    "privatekey",
    "secret-token",
    "token=",
)
CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/#@+-]{0,255}$")
TRACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class LaunchplaneSafetyError(ValueError):
    """Public-safe helper failure represented by a compact reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LaunchplaneEndpoint:
    url: str
    origin: tuple[str, str, int]


def _compact_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", key.strip().lower())


COMPACT_DENIED_KEYS = {_compact_key(item) for item in DENIED_KEYS}


def is_denied_key(key: str) -> bool:
    compact = _compact_key(key)
    if compact in SAFE_SECRET_METADATA_KEYS:
        return False
    normalized = key.strip().lower()
    if normalized in DENIED_KEYS or compact in COMPACT_DENIED_KEYS:
        return True
    return any(fragment in compact for fragment in DENIED_KEY_FRAGMENTS)


def _redact_token_like(value: str) -> str:
    redacted = value
    for pattern in TOKEN_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def _contains_forbidden_url_char(value: str) -> bool:
    return any(ord(char) <= 32 or char == "\\" for char in value)


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _origin(parsed: urllib.parse.SplitResult) -> tuple[str, str, int]:
    host = parsed.hostname
    if not host:
        raise LaunchplaneSafetyError("invalid_service_url_host")
    try:
        port = parsed.port or _default_port(parsed.scheme.lower())
    except ValueError:
        raise LaunchplaneSafetyError("invalid_service_url_port") from None
    return (parsed.scheme.lower(), host.lower(), port)


def validate_service_url(raw_url: str) -> LaunchplaneEndpoint:
    value = raw_url.strip()
    if not value:
        raise LaunchplaneSafetyError("missing_service_url")
    if _contains_forbidden_url_char(value):
        raise LaunchplaneSafetyError("invalid_service_url")
    parsed = urllib.parse.urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        raise LaunchplaneSafetyError("invalid_service_url_absolute")
    if parsed.username is not None or parsed.password is not None:
        raise LaunchplaneSafetyError("invalid_service_url_userinfo")
    scheme = parsed.scheme.lower()
    if scheme not in {"https", "http"}:
        raise LaunchplaneSafetyError("invalid_service_url_scheme")
    origin = _origin(parsed)
    if scheme == "http" and not _is_loopback_host(origin[1]):
        raise LaunchplaneSafetyError("invalid_service_url_http")
    if parsed.query or parsed.fragment:
        raise LaunchplaneSafetyError("invalid_service_url_component")
    return LaunchplaneEndpoint(url=value.rstrip("/"), origin=origin)


def validate_request_url(raw_url: str) -> LaunchplaneEndpoint:
    value = raw_url.strip()
    if not value or _contains_forbidden_url_char(value):
        raise LaunchplaneSafetyError("invalid_request_url")
    parsed = urllib.parse.urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        raise LaunchplaneSafetyError("invalid_request_url_absolute")
    if parsed.username is not None or parsed.password is not None:
        raise LaunchplaneSafetyError("invalid_request_url_userinfo")
    scheme = parsed.scheme.lower()
    if scheme not in {"https", "http"}:
        raise LaunchplaneSafetyError("invalid_request_url_scheme")
    origin = _origin(parsed)
    if scheme == "http" and not _is_loopback_host(origin[1]):
        raise LaunchplaneSafetyError("invalid_request_url_http")
    return LaunchplaneEndpoint(url=value, origin=origin)


def build_launchplane_url(service_url: str, path: str, *, query: str = "") -> str:
    endpoint = validate_service_url(service_url)
    parsed_path = urllib.parse.urlsplit(path)
    if not path.startswith("/") or parsed_path.scheme or parsed_path.netloc:
        raise LaunchplaneSafetyError("invalid_request_path")
    url = f"{endpoint.url}{path}"
    return f"{url}?{query}" if query else url


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        original = validate_request_url(req.full_url)
        redirected = validate_request_url(urllib.parse.urljoin(req.full_url, newurl))
        if redirected.origin != original.origin:
            raise LaunchplaneSafetyError("unsafe_redirect")
        return super().redirect_request(req, fp, code, msg, headers, redirected.url)


def safe_urlopen(request: urllib.request.Request, *, timeout: float) -> Any:
    opener = urllib.request.build_opener(SameOriginRedirectHandler)
    return opener.open(request, timeout=timeout)


def assert_public_safe_shape(value: Any) -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            if is_denied_key(key):
                raise LaunchplaneSafetyError("unsafe_response_shape")
            assert_public_safe_shape(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            assert_public_safe_shape(item)
        return
    if isinstance(value, str) and _redact_token_like(value) != value:
        raise LaunchplaneSafetyError("unsafe_response_shape")


def public_code(value: object, *, default: str | None = None) -> str:
    if value in {None, ""} and default is not None:
        value = default
    if not isinstance(value, str) or not CODE_RE.fullmatch(value):
        raise LaunchplaneSafetyError("invalid_response")
    return value


def public_identifier(value: object) -> str:
    if not isinstance(value, str):
        raise LaunchplaneSafetyError("invalid_response")
    compact = " ".join(value.split())
    if (
        not compact
        or len(compact) > 256
        or _redact_token_like(compact) != compact
        or not IDENTIFIER_RE.fullmatch(compact)
    ):
        raise LaunchplaneSafetyError("invalid_response")
    return compact


def public_trace_id(value: object) -> str:
    if value in {None, ""}:
        return ""
    if not isinstance(value, str):
        raise LaunchplaneSafetyError("invalid_response")
    compact = " ".join(value.split())
    lowered = compact.lower()
    if (
        not TRACE_ID_RE.fullmatch(compact)
        or _redact_token_like(compact) != compact
        or any(fragment in lowered for fragment in SUMMARY_VALUE_DENYLIST)
    ):
        raise LaunchplaneSafetyError("invalid_response")
    return compact


def public_timestamp(value: object) -> str:
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        raise LaunchplaneSafetyError("invalid_response")
    return value


def public_summary_string(value: object, *, max_length: int = 500, allow_url: bool = False) -> str:
    if not isinstance(value, str):
        raise LaunchplaneSafetyError("invalid_response")
    compact = " ".join(value.split())
    lowered = compact.lower()
    if (
        not compact
        or len(compact) > max_length
        or _redact_token_like(compact) != compact
        or any(fragment in lowered for fragment in SUMMARY_VALUE_DENYLIST)
        or (not allow_url and "http://" in lowered)
        or (not allow_url and "https://" in lowered)
    ):
        raise LaunchplaneSafetyError("invalid_response")
    return compact


def public_url(value: object) -> str:
    if not isinstance(value, str):
        raise LaunchplaneSafetyError("invalid_response")
    endpoint = validate_request_url(value)
    if endpoint.origin[0] != "https":
        raise LaunchplaneSafetyError("invalid_response")
    return endpoint.url
