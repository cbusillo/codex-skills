#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import stat
import tempfile
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import github_identity


def der_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(encoded)]) + encoded


def der_value(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + der_length(len(value)) + value


def der_integer(value: int) -> bytes:
    encoded = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    if encoded[0] & 0x80:
        encoded = b"\x00" + encoded
    return der_value(0x02, encoded)


def is_probable_prime(candidate: int) -> bool:
    if candidate < 2 or candidate % 2 == 0:
        return candidate == 2
    divisor = candidate - 1
    shifts = 0
    while divisor % 2 == 0:
        shifts += 1
        divisor //= 2
    for base in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if base >= candidate:
            continue
        value = pow(base, divisor, candidate)
        if value in (1, candidate - 1):
            continue
        for _ in range(shifts - 1):
            value = pow(value, 2, candidate)
            if value == candidate - 1:
                break
        else:
            return False
    return True


def next_prime(candidate: int) -> int:
    candidate |= 1
    while not is_probable_prime(candidate):
        candidate += 2
    return candidate


def test_private_key() -> str:
    public_exponent = 65_537
    p = next_prime((1 << 287) + 0x123456789ABCDEF)
    q = next_prime((1 << 287) + 0xFEDCBA987654321)
    phi = (p - 1) * (q - 1)
    assert math.gcd(public_exponent, phi) == 1
    private_exponent = pow(public_exponent, -1, phi)
    values = (
        0,
        p * q,
        public_exponent,
        private_exponent,
        p,
        q,
        private_exponent % (p - 1),
        private_exponent % (q - 1),
        pow(q, -1, p),
    )
    der = der_value(0x30, b"".join(der_integer(value) for value in values))
    body = base64.b64encode(der).decode("ascii")
    wrapped = "\n".join(body[index : index + 64] for index in range(0, len(body), 64))
    label = "RSA " + "PRIVATE KEY"
    return f"-----BEGIN {label}-----\n{wrapped}\n-----END {label}-----\n"


class TokenHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, str]] = []
    identity_requests: list[dict[str, str]] = []
    expiries: list[int] = []

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        type(self).identity_requests.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
        })
        if self.path == "/app":
            payload = json.dumps({"slug": "catalog-app"}).encode()
        elif self.path == "/app/installations/67890":
            payload = json.dumps({"id": 67890, "app_id": 12345, "app_slug": "catalog-app"}).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        type(self).requests.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
        })
        index = len(type(self).requests)
        expires_at = type(self).expiries[min(index - 1, len(type(self).expiries) - 1)]
        payload = json.dumps({
            "token": f"installation-token-{index}",
            "expires_at": datetime.fromtimestamp(expires_at, tz=UTC).isoformat().replace("+00:00", "Z"),
        }).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        pass


class RedirectHandler(BaseHTTPRequestHandler):
    redirected = False

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path == "/app":
            self.send_response(302)
            self.send_header("Location", "/redirected")
            self.end_headers()
            return
        type(self).redirected = True
        payload = b'{"slug":"redirected-app"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        pass


def app_environment(root: Path, api_url: str) -> dict[str, str]:
    key = root / "app.pem"
    key.write_text(test_private_key(), encoding="utf-8")
    key.chmod(0o600)
    return {
        "HOME": str(root),
        "GITHUB_APP_ID": "12345",
        "GITHUB_APP_INSTALLATION_ID": "67890",
        "GITHUB_APP_PRIVATE_KEY_PATH": str(key),
        "GITHUB_APP_API_URL": api_url,
        "GITHUB_APP_TOKEN_CACHE_DIR": str(root / "cache"),
    }


def test_local_env_file_precedence_matches_shell() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        explicit = root / "explicit.env"
        explicit.write_text("CODEX_AUTOMATION_LOGIN=explicit\n", encoding="utf-8")
        code_home = root / "code"
        codex_home = root / "codex"
        home = root / "home"
        code_home.mkdir()
        codex_home.mkdir()
        (home / ".code").mkdir(parents=True)
        (code_home / "local.env").write_text("CODEX_AUTOMATION_LOGIN=code\n", encoding="utf-8")
        (codex_home / "local.env").write_text("CODEX_AUTOMATION_LOGIN=codex\n", encoding="utf-8")
        (home / ".code" / "local.env").write_text("CODEX_AUTOMATION_LOGIN=home\n", encoding="utf-8")
        values = {
            "CODEX_SKILLS_ENV_FILE": str(explicit),
            "CODE_HOME": str(code_home),
            "CODEX_HOME": str(codex_home),
            "HOME": str(home),
        }
        assert github_identity.automation_login(values) == "explicit"
        values.pop("CODEX_SKILLS_ENV_FILE")
        assert github_identity.automation_login(values) == "code"
        values.pop("CODE_HOME")
        assert github_identity.automation_login(values) == "codex"
        values.pop("CODEX_HOME")
        assert github_identity.automation_login(values) == "home"


def test_per_tool_overrides_win_over_shared_identity() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env_file = Path(directory) / "local.env"
        env_file.write_text(
            "CODEX_AUTOMATION_LOGIN=shared\n"
            "CODEX_AUTOMATION_EMAIL=shared@example.invalid\n"
            "GH_WITH_ENV_TOKEN_EXPECTED_LOGIN=github-tool\n"
            "GIT_COMMIT_AS_BOT_NAME='Git Tool'\n"
            "GIT_COMMIT_AS_BOT_EMAIL=git@example.invalid\n",
            encoding="utf-8",
        )
        values = {"CODEX_SKILLS_ENV_FILE": str(env_file)}
        assert github_identity.automation_login(values) == "github-tool"


def test_unconfigured_identity_is_distinct_from_fallback() -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert github_identity.automation_login() is None
        assert not github_identity.active_auth_fallback_allowed()
        with patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK": "1"}):
            assert github_identity.automation_login() is None
            assert github_identity.active_auth_fallback_allowed()


def test_local_require_automation_auth_overrides_process_fallback() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env_file = Path(directory) / "local.env"
        env_file.write_text(
            "GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH=1\n",
            encoding="utf-8",
        )
        values = {
            "CODEX_SKILLS_ENV_FILE": str(env_file),
            "GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK": "1",
        }
        assert not github_identity.active_auth_fallback_allowed(values)


def test_unquoted_multiword_values_are_ignored_like_invalid_shell_assignments() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env_file = Path(directory) / "local.env"
        env_file.write_text("CODEX_AUTOMATION_LOGIN=Automation Bot\n", encoding="utf-8")
        values = {"CODEX_SKILLS_ENV_FILE": str(env_file)}
        assert github_identity.automation_login(values) is None


def test_configured_bot_logins_support_quoted_space_separated_values() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env_file = Path(directory) / "local.env"
        env_file.write_text(
            "CODEX_AUTOMATION_BOT_LOGINS='dependabot[bot] release-bot'\n",
            encoding="utf-8",
        )
        values = {"CODEX_SKILLS_ENV_FILE": str(env_file)}
        assert github_identity.configured_bot_logins(values) == (
            "dependabot[bot]",
            "release-bot",
        )


def test_shell_expansion_values_are_ignored_in_python_parser() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env_file = Path(directory) / "local.env"
        env_file.write_text("CODEX_AUTOMATION_LOGIN='${ORG}-bot'\n", encoding="utf-8")
        values = {"CODEX_SKILLS_ENV_FILE": str(env_file)}
        assert github_identity.automation_login(values) is None


def test_github_app_token_is_minted_cached_and_refreshed() -> None:
    initial = 1_700_000_000
    TokenHandler.requests = []
    TokenHandler.identity_requests = []
    TokenHandler.expiries = [initial + 3_600, initial + 7_200]
    server = ThreadingHTTPServer(("127.0.0.1", 0), TokenHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = app_environment(root, f"http://127.0.0.1:{server.server_port}")
            config = github_identity.github_app_config(values)
            assert config is not None

            first = github_identity.github_app_installation_token(config, now=initial)
            cached = github_identity.github_app_installation_token(config, now=initial + 600)
            refreshed = github_identity.github_app_installation_token(config, now=initial + 3_301)

            assert first == cached == "installation-token-1"
            assert refreshed == "installation-token-2"
            assert len(TokenHandler.requests) == 2
            assert len(TokenHandler.identity_requests) == 2
            assert all(item["path"] == "/app" for item in TokenHandler.identity_requests)
            assert all(item["path"] == "/app/installations/67890/access_tokens" for item in TokenHandler.requests)
            assert all(item["authorization"].startswith("Bearer eyJ") for item in TokenHandler.requests)
            cache_files = list((root / "cache").glob("*.json"))
            assert len(cache_files) == 1
            assert stat.S_IMODE(cache_files[0].stat().st_mode) == 0o600
            assert stat.S_IMODE((root / "cache").stat().st_mode) == 0o700
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_github_app_configuration_is_all_or_nothing() -> None:
    with tempfile.TemporaryDirectory() as directory:
        values = {"HOME": directory, "GITHUB_APP_ID": "12345"}
        try:
            github_identity.github_app_config(values)
        except github_identity.GitHubAppError as error:
            assert "missing GITHUB_APP_INSTALLATION_ID, GITHUB_APP_PRIVATE_KEY_PATH" in str(error)
        else:
            raise AssertionError("partial GitHub App configuration was accepted")


def test_github_app_private_key_rejects_group_or_world_access() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        values = app_environment(root, "https://api.github.invalid")
        Path(values["GITHUB_APP_PRIVATE_KEY_PATH"]).chmod(0o644)
        try:
            github_identity.github_app_config(values)
        except github_identity.GitHubAppError as error:
            assert "mode 600" in str(error)
        else:
            raise AssertionError("an overexposed GitHub App private key was accepted")


def test_github_app_jwt_has_a_valid_pkcs1_signature() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        values = app_environment(root, "https://api.github.test")
        config = github_identity.github_app_config(values)
        assert config is not None
        jwt = github_identity.github_app_jwt(config, now=1_700_000_000)
        header, claims, encoded_signature = jwt.split(".")
        signature = base64.urlsafe_b64decode(encoded_signature + "==")
        modulus, _ = github_identity._rsa_private_numbers(config.private_key_path.read_bytes())
        recovered = pow(int.from_bytes(signature, "big"), 65_537, modulus).to_bytes(len(signature), "big")
        digest_info = bytes.fromhex("3031300d060960864801650304020105000420")
        digest_info += hashlib.sha256(f"{header}.{claims}".encode()).digest()
        expected = b"\x00\x01" + (b"\xff" * (len(signature) - len(digest_info) - 3)) + b"\x00" + digest_info
        assert recovered == expected


def test_github_app_api_url_rejects_non_loopback_http() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        values = app_environment(root, "http://example.invalid")
        try:
            github_identity.github_app_config(values)
        except github_identity.GitHubAppError as error:
            assert "must be HTTPS" in str(error)
        else:
            raise AssertionError("cleartext non-loopback GitHub App API URL was accepted")


def test_github_app_jwt_accepts_pkcs8_wrapped_key() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        values = app_environment(root, "https://api.github.test")
        key_path = Path(values["GITHUB_APP_PRIVATE_KEY_PATH"])
        pem_lines = [line for line in key_path.read_text(encoding="utf-8").splitlines() if not line.startswith("-----")]
        pkcs1 = base64.b64decode("".join(pem_lines))
        rsa_oid = der_value(0x06, bytes.fromhex("2a864886f70d010101"))
        algorithm = der_value(0x30, rsa_oid + der_value(0x05, b""))
        pkcs8 = der_value(0x30, der_integer(0) + algorithm + der_value(0x04, pkcs1))
        body = base64.b64encode(pkcs8).decode("ascii")
        wrapped = "\n".join(body[index : index + 64] for index in range(0, len(body), 64))
        label = "PRIVATE " + "KEY"
        key_path.write_text(f"-----BEGIN {label}-----\n{wrapped}\n-----END {label}-----\n", encoding="utf-8")
        key_path.chmod(0o600)
        config = github_identity.github_app_config(values)
        assert config is not None
        assert github_identity.github_app_jwt(config, now=1_700_000_000).count(".") == 2


def test_github_app_identity_request_does_not_follow_redirects() -> None:
    RedirectHandler.redirected = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = app_environment(root, f"http://127.0.0.1:{server.server_port}")
            config = github_identity.github_app_config(values)
            assert config is not None
            try:
                github_identity.github_app_auth(config, now=1_700_000_000)
            except github_identity.GitHubAppError as error:
                assert "HTTP 302" in str(error)
            else:
                raise AssertionError("GitHub App identity redirect was followed")
            assert RedirectHandler.redirected is False
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_github_app_check_verifies_current_installation_without_cache() -> None:
    TokenHandler.requests = []
    TokenHandler.identity_requests = []
    TokenHandler.expiries = [1_700_003_600]
    server = ThreadingHTTPServer(("127.0.0.1", 0), TokenHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = app_environment(root, f"http://127.0.0.1:{server.server_port}")
            config = github_identity.github_app_config(values)
            assert config is not None
            assert github_identity.check_github_app_installation(config, now=1_700_000_000) == "catalog-app[bot]"
            assert TokenHandler.identity_requests == [{
                "path": "/app/installations/67890",
                "authorization": TokenHandler.identity_requests[0]["authorization"],
            }]
            assert TokenHandler.identity_requests[0]["authorization"].startswith("Bearer eyJ")
            assert TokenHandler.requests == []
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_github_app_requires_explicit_api_url_for_enterprise_host() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        values = app_environment(root, "https://api.github.com")
        values.pop("GITHUB_APP_API_URL")
        values["GH_HOST"] = "github.example.test"
        try:
            github_identity.github_app_config(values)
        except github_identity.GitHubAppError as error:
            assert "set GITHUB_APP_API_URL" in str(error)
        else:
            raise AssertionError("enterprise GitHub host defaulted to api.github.com")


def main() -> None:
    tests = [
        test_local_env_file_precedence_matches_shell,
        test_per_tool_overrides_win_over_shared_identity,
        test_unconfigured_identity_is_distinct_from_fallback,
        test_local_require_automation_auth_overrides_process_fallback,
        test_unquoted_multiword_values_are_ignored_like_invalid_shell_assignments,
        test_shell_expansion_values_are_ignored_in_python_parser,
        test_configured_bot_logins_support_quoted_space_separated_values,
        test_github_app_token_is_minted_cached_and_refreshed,
        test_github_app_configuration_is_all_or_nothing,
        test_github_app_private_key_rejects_group_or_world_access,
        test_github_app_jwt_has_a_valid_pkcs1_signature,
        test_github_app_api_url_rejects_non_loopback_http,
        test_github_app_jwt_accepts_pkcs8_wrapped_key,
        test_github_app_identity_request_does_not_follow_redirects,
        test_github_app_check_verifies_current_installation_without_cache,
        test_github_app_requires_explicit_api_url_for_enterprise_host,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
