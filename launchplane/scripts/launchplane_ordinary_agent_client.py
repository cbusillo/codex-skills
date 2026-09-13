#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Portable private ordinary-agent client.

This module is deliberately a consumer of Launchplane's public operation
contract.  It keeps receiver proofs and claimed credentials in a small private
file store, and exposes only bounded, redacted projections to callers.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import math
import os
import re
import secrets
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no POSIX flock
    fcntl = None  # type: ignore[assignment]

try:  # Direct execution and import from the skill's scripts directory.
    from launchplane_contract import operation_path
    from launchplane_safety import (
        LaunchplaneSafetyError,
        assert_public_safe_shape,
        build_launchplane_url,
        public_code,
        public_identifier,
        public_trace_id,
        validate_request_url,
        validate_service_url,
    )
except ImportError:  # pragma: no cover - package-style import fallback
    from .launchplane_contract import operation_path
    from .launchplane_safety import (
        LaunchplaneSafetyError,
        assert_public_safe_shape,
        build_launchplane_url,
        public_code,
        public_identifier,
        public_trace_id,
        validate_request_url,
        validate_service_url,
    )


MAX_RESPONSE_BYTES = 256 * 1024
DEFAULT_TIMEOUT = 10.0
DEFAULT_ATTEMPTS = 3
CLAIM_DOMAIN = b"launchplane:ordinary-agent-claim:v1\0"
PROOF_BYTES = 32
PROOF_LENGTH = 43

_STATE_FILE = "ordinary-agent.json"
_LOCK_FILE = ".ordinary-agent.lock"
_LOCK_ATTEMPTS = 20
_LOCK_DELAY = 0.01
_ALIAS_PATTERN = r"^[a-z0-9][a-z0-9._-]{2,127}$"


class OrdinaryAgentClientError(RuntimeError):
    """Bounded public-safe client error; never contains private values."""

    def __init__(
        self,
        code: str,
        *,
        retry_after_seconds: int | None = None,
        trace_id: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self.trace_id = trace_id


def _private_root_default() -> Path:
    if os.name == "nt":
        raise OrdinaryAgentClientError("unsupported_private_state_platform")
    configured = os.environ.get("LAUNCHPLANE_ORDINARY_AGENT_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.environ.get("XDG_STATE_HOME", "").strip():
        return Path(os.environ["XDG_STATE_HOME"]) / "launchplane" / "ordinary-agent"
    if sys_platform_is_macos():
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Launchplane"
            / "ordinary-agent"
        )
    return Path.home() / ".local" / "state" / "launchplane" / "ordinary-agent"


def sys_platform_is_macos() -> bool:
    return sys.platform == "darwin"


def _initial_session_alias(enrollment_alias: str) -> str:
    prefix = f"initial-{enrollment_alias}"
    if len(prefix) <= 128:
        return _alias(prefix, fallback="initial-session")
    digest = hashlib.sha256(enrollment_alias.encode("utf-8")).hexdigest()
    return _alias(f"initial-{digest}", fallback="initial-session")


def receiver_proof(random_bytes: Any = secrets.token_bytes) -> str:
    """Create one canonical unpadded base64url 32-byte receiver proof."""

    material = random_bytes(PROOF_BYTES)
    if not isinstance(material, bytes) or len(material) != PROOF_BYTES:
        raise OrdinaryAgentClientError("invalid_receiver_proof")
    encoded = base64.urlsafe_b64encode(material).decode("ascii").rstrip("=")
    if len(encoded) != PROOF_LENGTH or any(
        c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
        for c in encoded
    ):
        raise OrdinaryAgentClientError("invalid_receiver_proof")
    return encoded


def claim_digest(proof: str) -> str:
    """Return Launchplane's domain-separated receiver claim digest."""

    if not isinstance(proof, str) or len(proof) != PROOF_LENGTH:
        raise OrdinaryAgentClientError("invalid_receiver_proof")
    try:
        decoded = base64.urlsafe_b64decode(proof + "=")
    except (binascii.Error, ValueError):
        raise OrdinaryAgentClientError("invalid_receiver_proof") from None
    if (
        len(decoded) != PROOF_BYTES
        or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != proof
    ):
        raise OrdinaryAgentClientError("invalid_receiver_proof")
    return hashlib.sha256(CLAIM_DOMAIN + proof.encode("ascii")).hexdigest()


def _strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _alias(value: object, *, fallback: str) -> str:
    candidate = fallback if value in (None, "") else value
    if not isinstance(candidate, str) or not re.fullmatch(_ALIAS_PATTERN, candidate):
        raise OrdinaryAgentClientError("invalid_alias")
    return candidate


def _ensure_owner_private(path: Path, *, directory: bool) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    owner = getattr(os, "getuid", lambda: info.st_uid)()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != owner:
        raise OrdinaryAgentClientError("unsafe_private_state")
    if directory:
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise OrdinaryAgentClientError("unsafe_private_state")
    elif not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise OrdinaryAgentClientError("unsafe_private_state")


class PrivateStateStore:
    """Owner-only JSON store with bounded serialization and durable replace."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        if os.name == "nt" or fcntl is None or not hasattr(fcntl, "flock"):
            raise OrdinaryAgentClientError("unsupported_private_state_platform")
        self.root = (
            Path(root).expanduser() if root is not None else _private_root_default()
        )
        try:
            if not self.root.is_absolute():
                raise OrdinaryAgentClientError("private_state_path_must_be_absolute")
            self._reject_repository_path(self.root)
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            _ensure_owner_private(self.root, directory=True)
            self.root.chmod(0o700)
        except (OSError, OrdinaryAgentClientError) as exc:
            if isinstance(exc, OrdinaryAgentClientError):
                raise
            raise OrdinaryAgentClientError("private_state_unavailable") from None
        self._lock_path = self.root / _LOCK_FILE
        self._state_path = self.root / _STATE_FILE

    @staticmethod
    def _reject_repository_path(path: Path) -> None:
        try:
            resolved = path.resolve()
            parents = (resolved, *resolved.parents)
            if any((parent / ".git").exists() for parent in parents):
                raise OrdinaryAgentClientError("private_state_in_repository")
        except OSError:
            raise OrdinaryAgentClientError("private_state_unavailable") from None

    @contextmanager
    def _locked(self) -> Iterator[None]:
        fd = -1
        try:
            _ensure_owner_private(self._lock_path, directory=False)
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            owner = getattr(os, "getuid", lambda: info.st_uid)()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != owner
                or info.st_mode & 0o077
            ):
                raise OrdinaryAgentClientError("unsafe_private_state")
            os.fchmod(fd, 0o600)
        except (OSError, OrdinaryAgentClientError):
            if fd >= 0:
                os.close(fd)
            raise OrdinaryAgentClientError("private_state_unavailable") from None
        try:
            for attempt in range(_LOCK_ATTEMPTS):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if attempt + 1 == _LOCK_ATTEMPTS:
                        raise OrdinaryAgentClientError("private_state_busy")
                    time.sleep(_LOCK_DELAY)
            yield
        except OSError:
            raise OrdinaryAgentClientError("private_state_unavailable") from None
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            except OSError:
                pass

    def load(self) -> dict[str, Any]:
        with self._locked():
            return self._load_unlocked()

    def _load_unlocked(self) -> dict[str, Any]:
        try:
            _ensure_owner_private(self._state_path, directory=False)
            state_fd = os.open(self._state_path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(state_fd)
                owner = getattr(os, "getuid", lambda: info.st_uid)()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != owner
                    or info.st_mode & 0o077
                ):
                    raise OrdinaryAgentClientError("unsafe_private_state")
                raw = os.read(state_fd, MAX_RESPONSE_BYTES + 1)
            finally:
                os.close(state_fd)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise OrdinaryAgentClientError("private_state_unavailable")
            value = json.loads(raw)
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise OrdinaryAgentClientError("private_state_unavailable") from None
        if not isinstance(value, dict):
            raise OrdinaryAgentClientError("private_state_unavailable")
        return value

    def replace(self, value: Mapping[str, Any]) -> None:
        if not isinstance(value, Mapping):
            raise OrdinaryAgentClientError("private_state_unavailable")
        try:
            encoded = json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise OrdinaryAgentClientError("private_state_unavailable") from None
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise OrdinaryAgentClientError("private_state_unavailable")
        with self._locked():
            self._replace_unlocked(encoded)

    def _replace_unlocked(self, encoded: bytes) -> None:
        """Replace state while the caller holds the process lock."""

        fd = -1
        temporary = ""
        try:
            fd, temporary = tempfile.mkstemp(prefix=".ordinary-agent.", dir=self.root)
            os.fchmod(fd, 0o600)
            view = memoryview(encoded)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temporary, self._state_path)
            temporary = ""
            directory_fd = os.open(
                self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            raise OrdinaryAgentClientError("private_state_unavailable") from None
        finally:
            if fd >= 0:
                os.close(fd)
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def update(self, mutator: Any) -> dict[str, Any]:
        """Read, mutate, and durably replace state under one process lock."""

        with self._locked():
            current = self._load_unlocked()
            updated = mutator(current)
            if not isinstance(updated, dict):
                raise OrdinaryAgentClientError("private_state_unavailable")
            try:
                encoded = json.dumps(
                    updated, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                ).encode("utf-8")
            except (TypeError, ValueError):
                raise OrdinaryAgentClientError("private_state_unavailable") from None
            if len(encoded) > MAX_RESPONSE_BYTES:
                raise OrdinaryAgentClientError("private_state_unavailable")
            self._replace_unlocked(encoded)
            return updated

    def checkpoint(self, state: Mapping[str, Any]) -> None:
        """Durably checkpoint a transaction's state before its network call."""

        try:
            encoded = json.dumps(
                state, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise OrdinaryAgentClientError("private_state_unavailable") from None
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise OrdinaryAgentClientError("private_state_unavailable")
        self._replace_unlocked(encoded)

    @contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        """Hold one bounded lock across read, network, and state transition."""

        with self._locked():
            state = self._load_unlocked()
            try:
                yield state
            finally:
                try:
                    encoded = json.dumps(
                        state,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    ).encode("utf-8")
                    if len(encoded) > MAX_RESPONSE_BYTES:
                        raise OrdinaryAgentClientError("private_state_unavailable")
                    self._replace_unlocked(encoded)
                except (TypeError, ValueError):
                    raise OrdinaryAgentClientError(
                        "private_state_unavailable"
                    ) from None

    def bind(self, binding: Mapping[str, Any]) -> None:
        """Bind this private store to one normalized service endpoint."""

        if set(binding) != {"url", "origin"}:
            raise OrdinaryAgentClientError("invalid_service_binding")

        def bind_state(state: dict[str, Any]) -> dict[str, Any]:
            existing = state.get("binding")
            private_keys = {
                "enrollment",
                "enrollments",
                "credential",
                "credentials",
                "session",
                "sessions",
                "job",
                "jobs",
            }
            has_private_material = any(
                key in state and state[key] for key in private_keys
            )
            if existing is None:
                if has_private_material:
                    raise OrdinaryAgentClientError("unbound_private_state")
                state["binding"] = dict(binding)
                return state
            if existing != dict(binding):
                raise OrdinaryAgentClientError("service_binding_mismatch")
            return state

        self.update(bind_state)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        raise OrdinaryAgentClientError("unsafe_redirect")


class OrdinaryAgentClient:
    """Bounded ordinary-agent lifecycle client using one persisted alias."""

    def __init__(
        self,
        service_url: str,
        *,
        state: PrivateStateStore | None = None,
        terminal_credential: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        attempts: int = DEFAULT_ATTEMPTS,
    ) -> None:
        try:
            endpoint = validate_service_url(service_url)
        except LaunchplaneSafetyError:
            raise OrdinaryAgentClientError("invalid_service_url") from None
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or attempts <= 0
        ):
            raise OrdinaryAgentClientError("invalid_transport_limits")
        self.service_url = endpoint.url
        self._binding = {"url": endpoint.url, "origin": list(endpoint.origin)}
        self.state = state or PrivateStateStore()
        self.state.bind(self._binding)
        self.terminal_credential = terminal_credential or os.environ.get(
            "LAUNCHPLANE_TERMINAL_CREDENTIAL", ""
        )
        self.timeout = timeout
        self.attempts = attempts

    def _read(self) -> dict[str, Any]:
        return self.state.load()

    def _write(self, value: dict[str, Any]) -> None:
        self.state.replace(value)

    @staticmethod
    def _credential(value: Mapping[str, Any]) -> str:
        credential = value.get("credential")
        if not isinstance(credential, str) or not credential or len(credential) > 512:
            raise OrdinaryAgentClientError("credential_unavailable")
        return OrdinaryAgentClient._validate_credential(credential)

    @staticmethod
    def _validate_credential(credential: str) -> str:
        if not isinstance(credential, str) or not credential or len(credential) > 512:
            raise OrdinaryAgentClientError("credential_unavailable")
        try:
            encoded = credential.encode("ascii")
        except UnicodeEncodeError:
            raise OrdinaryAgentClientError("credential_unavailable") from None
        if any(byte < 0x21 or byte > 0x7E for byte in encoded):
            raise OrdinaryAgentClientError("credential_unavailable")
        return credential

    def _url(self, contract_operation_id: str, **parts: str) -> str:
        try:
            path = operation_path(contract_operation_id)
            for name, value in parts.items():
                path = path.replace(
                    "{" + name + "}", urllib.parse.quote(value, safe="")
                )
            if "{" in path or "}" in path:
                raise OrdinaryAgentClientError("invalid_operation_path")
            return build_launchplane_url(self.service_url, path)
        except (LaunchplaneSafetyError, ValueError, TypeError, KeyError):
            raise OrdinaryAgentClientError("invalid_operation_path") from None

    @staticmethod
    def _http_error_metadata(
        error: urllib.error.HTTPError,
    ) -> tuple[str, int, str | None] | None:
        if error.code != 503:
            return None
        headers = error.headers
        retry_value = headers.get("Retry-After", "") if headers else ""
        if not isinstance(retry_value, str) or not re.fullmatch(
            r"[0-9]{1,5}", retry_value.strip()
        ):
            return None
        retry_after = int(retry_value.strip())
        if retry_after > 86_400:
            return None
        try:
            raw = error.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return None
            payload = json.loads(raw.decode("utf-8"))
            detail = payload.get("error") if isinstance(payload, dict) else None
            code = public_code(detail.get("code")) if isinstance(detail, dict) else None
            assert_public_safe_shape(code)
            if not code:
                return None
            trace = (
                public_trace_id(payload.get("trace_id"))
                if isinstance(payload, dict)
                else ""
            )
        except (
            AttributeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            LaunchplaneSafetyError,
            ValueError,
        ):
            return None
        return code, retry_after, trace or None

    def _request(
        self,
        operation_id: str,
        *,
        method: str,
        parts: Mapping[str, str] | None = None,
        body: bytes = b"",
        credential: str = "",
        claim: bool = False,
    ) -> tuple[dict[str, Any], Any]:
        if credential:
            self._validate_credential(credential)
        url = self._url(operation_id, **(dict(parts) if parts else {}))
        headers = {"Accept": "application/json"}
        if method == "POST":
            headers.update(
                {"Content-Type": "application/json", "Content-Length": str(len(body))}
            )
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        try:
            request = urllib.request.Request(
                url,
                data=body if method == "POST" else None,
                headers=headers,
                method=method,
            )
        except (TypeError, ValueError):
            raise OrdinaryAgentClientError("invalid_request") from None
        opener = urllib.request.build_opener(_NoRedirect())
        for attempt in range(self.attempts):
            try:
                opened = opener.open(request, timeout=self.timeout)
                with opened as response:
                    size = response.headers.get("Content-Length")
                    if size and (not size.isdigit() or int(size) > MAX_RESPONSE_BYTES):
                        raise OrdinaryAgentClientError("response_too_large")
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise OrdinaryAgentClientError("response_too_large")
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        raise OrdinaryAgentClientError("invalid_response") from None
                    if not isinstance(payload, dict):
                        raise OrdinaryAgentClientError("invalid_response")
                    if claim and "no-store" not in {
                        part.strip().lower()
                        for part in response.headers.get("Cache-Control", "").split(",")
                    }:
                        raise OrdinaryAgentClientError("claim_cache_policy")
                    return payload, response.headers
            except OrdinaryAgentClientError:
                raise
            except urllib.error.HTTPError as exc:
                metadata = self._http_error_metadata(exc)
                if metadata is not None:
                    code, retry_after, trace_id = metadata
                    raise OrdinaryAgentClientError(
                        code,
                        retry_after_seconds=retry_after,
                        trace_id=trace_id,
                    ) from None
                if 500 <= exc.code < 600 and attempt + 1 < self.attempts:
                    continue
                raise OrdinaryAgentClientError(f"http_{exc.code}") from None
            except (
                LaunchplaneSafetyError,
                http.client.HTTPException,
                urllib.error.URLError,
                TimeoutError,
                OSError,
                ValueError,
                UnicodeError,
            ):
                if attempt + 1 >= self.attempts:
                    raise OrdinaryAgentClientError("transport_unavailable") from None
        raise OrdinaryAgentClientError("transport_unavailable")

    def _review_url(self, value: object) -> str:
        if (
            not isinstance(value, str)
            or not value
            or any(char in value for char in "\r\n")
        ):
            raise OrdinaryAgentClientError("invalid_response")
        absolute = urllib.parse.urljoin(self.service_url + "/", value)
        try:
            endpoint = validate_request_url(absolute)
        except LaunchplaneSafetyError:
            raise OrdinaryAgentClientError("invalid_response") from None
        if endpoint.origin != tuple(self._binding["origin"]):
            raise OrdinaryAgentClientError("unsafe_review_url")
        parsed = urllib.parse.urlsplit(endpoint.url)
        try:
            for key, item in urllib.parse.parse_qsl(
                parsed.query, keep_blank_values=True, strict_parsing=True
            ):
                assert_public_safe_shape({key: item})
            if parsed.fragment:
                fragment_items = urllib.parse.parse_qsl(
                    parsed.fragment, keep_blank_values=True, strict_parsing=False
                )
                if fragment_items:
                    for key, item in fragment_items:
                        assert_public_safe_shape({key: item})
                else:
                    assert_public_safe_shape(parsed.fragment)
        except (LaunchplaneSafetyError, ValueError):
            raise OrdinaryAgentClientError("unsafe_review_url") from None
        return endpoint.url

    def _operation(
        self, payload: Mapping[str, Any], *, selectors: bool = True
    ) -> dict[str, Any]:
        if (
            set(payload) != {"schema_version", "operation", "review_url"}
            or payload.get("schema_version") != 1
        ):
            raise OrdinaryAgentClientError("invalid_response")
        operation = payload.get("operation")
        if not isinstance(operation, dict):
            raise OrdinaryAgentClientError("invalid_response")
        allowed = {
            "schema_version",
            "principal_id",
            "operation_id",
            "kind",
            "status",
            "reason_code",
            "current_policy_actions",
            "current_policy_execution_profile",
            "requester_kind",
            "requester_subject",
            "requester_token_label",
            "credential_expires_at",
            "delivery_expires_at",
            "attenuation",
            "credential_id",
            "credential_version",
            "target",
            "session_id",
            "session_expires_at",
            "lease_selectors",
            "applied",
            "can_approve",
        }
        if set(operation) - allowed:
            raise OrdinaryAgentClientError("invalid_response")
        for key in (
            "principal_id",
            "operation_id",
            "kind",
            "status",
            "requester_kind",
            "requester_subject",
        ):
            if not isinstance(operation.get(key), str) or not operation[key]:
                raise OrdinaryAgentClientError("invalid_response")
        if selectors:
            raw = operation.get("lease_selectors", [])
            if not isinstance(raw, list):
                raise OrdinaryAgentClientError("invalid_response")
            for item in raw:
                if not isinstance(item, dict) or set(item) - {
                    "lease_id",
                    "action",
                    "expires_at",
                    "revoked_at",
                }:
                    raise OrdinaryAgentClientError("invalid_response")
                if not isinstance(item.get("lease_id"), str) or not isinstance(
                    item.get("action"), str
                ):
                    raise OrdinaryAgentClientError("invalid_response")
                if item["action"] not in {"self_read", "preflight", "guarded_merge"}:
                    raise OrdinaryAgentClientError("invalid_response")
        elif operation.get("lease_selectors"):
            raise OrdinaryAgentClientError("unsafe_response_shape")
        if any(
            key in operation
            for key in (
                "approval_sha256",
                "receiver_sha256",
                "effective_decision_fingerprint",
                "managed_set_id",
                "managed_rule_id",
                "credential_digest",
            )
        ):
            raise OrdinaryAgentClientError("unsafe_response_shape")
        for key in ("credential_id", "credential_version", "credential_expires_at"):
            if key not in operation:
                continue
            value = operation[key]
            if key == "credential_id":
                if value is not None:
                    try:
                        public_identifier(value)
                    except LaunchplaneSafetyError:
                        raise OrdinaryAgentClientError(
                            "unsafe_response_shape"
                        ) from None
            elif value is not None and not _strict_int(value):
                raise OrdinaryAgentClientError("unsafe_response_shape")
        try:
            safe_operation = dict(operation)
            # These are public view metadata, but the generic helper's
            # denylist intentionally rejects keys containing credential.
            for private_key in (
                "credential_id",
                "credential_expires_at",
                "credential_version",
                "requester_token_label",
            ):
                safe_operation.pop(private_key, None)
            assert_public_safe_shape(safe_operation)
        except LaunchplaneSafetyError:
            raise OrdinaryAgentClientError("unsafe_response_shape") from None
        projected = dict(operation)
        projected.pop("requester_token_label", None)
        if projected.get("credential_id") is not None:
            try:
                projected["credential_id"] = public_identifier(
                    projected["credential_id"]
                )
            except LaunchplaneSafetyError:
                raise OrdinaryAgentClientError("unsafe_response_shape") from None
        projected["review_url"] = self._review_url(payload["review_url"])
        return projected

    @staticmethod
    def _safe_public(value: Any) -> Any:
        try:
            assert_public_safe_shape(value)
        except LaunchplaneSafetyError:
            raise OrdinaryAgentClientError("unsafe_response_shape") from None
        return value

    @staticmethod
    def _decode_state_bytes(value: object) -> bytes:
        if not isinstance(value, str):
            raise OrdinaryAgentClientError("private_state_unavailable")
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise OrdinaryAgentClientError("private_state_unavailable") from None
        if len(decoded) > MAX_RESPONSE_BYTES:
            raise OrdinaryAgentClientError("private_state_unavailable")
        return decoded

    @staticmethod
    def _auth_from_state(state: Mapping[str, Any]) -> str:
        credential = None
        credentials = state.get("credentials")
        current = state.get("current_credential")
        if isinstance(credentials, dict) and isinstance(current, str):
            credential = credentials.get(current)
            if credential is None:
                raise OrdinaryAgentClientError("credential_unavailable")
        if credential is None:
            credential = state.get("credential")
        if not isinstance(credential, str) or not credential:
            raise OrdinaryAgentClientError("credential_unavailable")
        return OrdinaryAgentClient._validate_credential(credential)

    def _prepare_enrollment_state(
        self,
        state: dict[str, Any],
        proposal: Mapping[str, Any],
        *,
        alias: str | None = None,
        random_bytes: Any = secrets.token_bytes,
    ) -> dict[str, Any]:
        if not isinstance(proposal, Mapping):
            raise OrdinaryAgentClientError("invalid_enrollment")
        request = dict(proposal)
        expected_keys = {
            "descriptor_id",
            "operation_id",
            "action",
            "principal_id",
            "target",
            "github_app_id",
            "secret_binding_id",
            "credential_valid_from",
            "credential_expires_at",
            "delivery",
            "session_attenuation",
        }
        if (
            set(request) != expected_keys
            or request.get("descriptor_id") != "ordinary-agent-enrollment"
        ):
            raise OrdinaryAgentClientError("invalid_enrollment")
        operation_id = request.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise OrdinaryAgentClientError("invalid_enrollment")
        if request.get("action") not in {
            "enroll",
            "rotate_credential",
        } or not isinstance(request.get("principal_id"), str):
            raise OrdinaryAgentClientError("invalid_enrollment")
        if not all(
            _strict_int(request.get(key))
            for key in (
                "github_app_id",
                "credential_valid_from",
                "credential_expires_at",
            )
        ):
            raise OrdinaryAgentClientError("invalid_enrollment")
        delivery = request.get("delivery")
        if (
            not isinstance(delivery, Mapping)
            or set(delivery) != {"expires_at"}
            or not isinstance(delivery.get("expires_at"), int)
        ):
            raise OrdinaryAgentClientError("invalid_enrollment")
        if not _strict_int(delivery.get("expires_at")):
            raise OrdinaryAgentClientError("invalid_enrollment")
        enrollment_alias = _alias(alias, fallback=str(operation_id))
        proposal_fingerprint = hashlib.sha256(
            json.dumps(
                request, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
        ).hexdigest()
        enrollments = state.setdefault("enrollments", {})
        if not isinstance(enrollments, dict):
            raise OrdinaryAgentClientError("private_state_unavailable")
        existing = enrollments.get(enrollment_alias)
        if isinstance(existing, dict):
            if (
                existing.get("retry_key") != operation_id
                or existing.get("proposal_fingerprint") != proposal_fingerprint
            ):
                raise OrdinaryAgentClientError("idempotency_conflict")
            state["current_enrollment"] = enrollment_alias
            return existing
        proof = receiver_proof(random_bytes)
        request["delivery"] = {
            "receiver_claim_sha256": claim_digest(proof),
            "expires_at": delivery["expires_at"],
        }
        try:
            encoded = json.dumps(
                request, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise OrdinaryAgentClientError("invalid_enrollment") from None
        prepared = {
            "retry_key": operation_id,
            "proposal_fingerprint": proposal_fingerprint,
            "request_bytes_b64": base64.b64encode(encoded).decode("ascii"),
            "receiver_proof": proof,
            "canonical_operation_id": None,
            "principal_id": request.get("principal_id"),
            "status": "prepared",
        }
        enrollments[enrollment_alias] = prepared
        state["current_enrollment"] = enrollment_alias
        return prepared

    def prepare_enrollment(
        self,
        proposal: Mapping[str, Any],
        *,
        alias: str | None = None,
        random_bytes: Any = secrets.token_bytes,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal result
            result = self._prepare_enrollment_state(
                state, proposal, alias=alias, random_bytes=random_bytes
            )
            return state

        self.state.update(mutate)
        return result

    def propose_enrollment(
        self, proposal: Mapping[str, Any] | None = None, *, alias: str | None = None
    ) -> dict[str, Any]:
        operation: dict[str, Any] = {}
        with self.state.transaction() as state:
            if proposal is None:
                enrollment_alias = _alias(
                    alias, fallback=str(state.get("current_enrollment", ""))
                )
                enrollments = state.get("enrollments")
                prepared = (
                    enrollments.get(enrollment_alias)
                    if isinstance(enrollments, dict)
                    else None
                )
                if not isinstance(prepared, dict):
                    raise OrdinaryAgentClientError("enrollment_unavailable")
                state["current_enrollment"] = enrollment_alias
            else:
                enrollment_alias = _alias(
                    alias, fallback=str(proposal.get("operation_id", ""))
                )
                prepared = self._prepare_enrollment_state(
                    state, proposal, alias=enrollment_alias
                )
            body = self._decode_state_bytes(prepared["request_bytes_b64"])
            self.state.checkpoint(state)
            payload, _ = self._request(
                "propose_ordinary_agent_enrollment",
                method="POST",
                body=body,
                credential=self.terminal_credential,
            )
            operation = self._operation(payload, selectors=False)
            prepared.update(
                {
                    "canonical_operation_id": operation["operation_id"],
                    "principal_id": operation["principal_id"],
                    "status": operation["status"],
                }
            )
        return operation

    def enrollment_status(self, *, alias: str | None = None) -> dict[str, Any]:
        with self.state.transaction() as state:
            enrollment_alias = _alias(
                alias, fallback=str(state.get("current_enrollment", ""))
            )
            enrollments = state.get("enrollments")
            record = (
                enrollments.get(enrollment_alias)
                if isinstance(enrollments, dict)
                else None
            )
            if (
                not isinstance(record, dict)
                or not record.get("canonical_operation_id")
                or not record.get("principal_id")
            ):
                raise OrdinaryAgentClientError("enrollment_unavailable")
            payload, _ = self._request(
                "read_proposed_ordinary_agent_enrollment",
                method="GET",
                parts={
                    "principal_id": record["principal_id"],
                    "operation_id": record["canonical_operation_id"],
                },
                credential=self.terminal_credential,
            )
            operation = self._operation(payload, selectors=False)
            record["status"] = operation["status"]
            return operation

    def claim(self, *, alias: str | None = None) -> dict[str, str]:
        with self.state.transaction() as state:
            enrollment_alias = _alias(
                alias, fallback=str(state.get("current_enrollment", ""))
            )
            enrollments = state.get("enrollments")
            enrollment = (
                enrollments.get(enrollment_alias)
                if isinstance(enrollments, dict)
                else None
            )
            if (
                not isinstance(enrollment, dict)
                or not enrollment.get("canonical_operation_id")
                or not enrollment.get("receiver_proof")
            ):
                raise OrdinaryAgentClientError("enrollment_unavailable")
            payload, _ = self._request(
                "claim_ordinary_agent_credential",
                method="POST",
                parts={"operation_id": enrollment["canonical_operation_id"]},
                credential=enrollment["receiver_proof"],
                claim=True,
            )
            if (
                set(payload) != {"schema_version", "status", "credential"}
                or payload.get("schema_version") != 1
                or payload.get("status") != "ready"
            ):
                raise OrdinaryAgentClientError("invalid_claim_response")
            credential = self._credential(payload)
            credentials = state.setdefault("credentials", {})
            if not isinstance(credentials, dict):
                raise OrdinaryAgentClientError("private_state_unavailable")
            credential_key = str(enrollment["canonical_operation_id"])
            credentials[credential_key] = credential
            state["current_credential"] = credential_key
            # Keep the old top-level value only for a controlled migration
            # read; new records use the keyed credential collection.
            enrollment["status"] = "claimed"
            enrollment["credential_key"] = credential_key
        return {"status": "ready"}

    def propose_session(
        self, request: Mapping[str, Any] | None = None, *, alias: str | None = None
    ) -> dict[str, Any]:
        if request is not None and (
            not isinstance(request, Mapping)
            or set(request) != {"descriptor_id", "operation_id", "attenuation"}
            or request.get("descriptor_id") != "ordinary-agent-session"
            or not isinstance(request.get("attenuation"), Mapping)
        ):
            raise OrdinaryAgentClientError("invalid_session")
        operation_id = (
            request.get("operation_id") if isinstance(request, Mapping) else None
        )
        if request is not None and (
            not isinstance(operation_id, str) or not operation_id
        ):
            raise OrdinaryAgentClientError("invalid_session")
        body: bytes | None = None
        if request is not None:
            try:
                body = json.dumps(
                    dict(request),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("utf-8")
            except (TypeError, ValueError, UnicodeError):
                raise OrdinaryAgentClientError("invalid_session") from None
        with self.state.transaction() as state:
            sessions = state.setdefault("sessions", {})
            if not isinstance(sessions, dict):
                raise OrdinaryAgentClientError("private_state_unavailable")
            if request is None:
                raw_alias = alias or state.get("current_session")
                if not raw_alias:
                    raise OrdinaryAgentClientError("session_unavailable")
                session_alias = _alias(raw_alias, fallback="initial")
            else:
                session_alias = _alias(alias, fallback=str(operation_id))
            existing = sessions.get(session_alias)
            if isinstance(existing, dict):
                current_credential = state.get("current_credential")
                if (
                    current_credential is not None
                    and existing.get("credential_key") != current_credential
                ):
                    raise OrdinaryAgentClientError("session_stale")
                stored_body = existing.get("request_bytes_b64")
                if request is not None and (
                    existing.get("retry_key") != operation_id
                    or stored_body != base64.b64encode(body or b"").decode("ascii")
                ):
                    raise OrdinaryAgentClientError("idempotency_conflict")
                body = self._decode_state_bytes(stored_body)
                operation_id = existing.get("retry_key")
                if not isinstance(operation_id, str) or not operation_id:
                    raise OrdinaryAgentClientError("session_unavailable")
            elif request is None:
                raise OrdinaryAgentClientError("session_unavailable")
            else:
                existing = {
                    "retry_key": operation_id,
                    "request_bytes_b64": base64.b64encode(body or b"").decode("ascii"),
                    "canonical_operation_id": None,
                    "credential_key": state.get("current_credential"),
                }
                sessions[session_alias] = existing
                self.state.checkpoint(state)
            if not isinstance(body, bytes) or not isinstance(existing, dict):
                raise OrdinaryAgentClientError("session_unavailable")
            state["current_session"] = session_alias
            payload, _ = self._request(
                "propose_ordinary_agent_session",
                method="POST",
                body=body,
                credential=self._auth_from_state(state),
            )
            operation = self._operation(payload)
            existing.update(
                {
                    "canonical_operation_id": operation["operation_id"],
                    "status": operation["status"],
                }
            )
        return operation

    def _initial_session_state(self, state: dict[str, Any]) -> dict[str, Any]:
        sessions = state.setdefault("sessions", {})
        if not isinstance(sessions, dict):
            raise OrdinaryAgentClientError("private_state_unavailable")
        enrollments = state.get("enrollments")
        current_credential = state.get("current_credential")
        if not isinstance(enrollments, dict) or not isinstance(current_credential, str):
            raise OrdinaryAgentClientError("session_unavailable")
        candidates = [
            (name, record)
            for name, record in enrollments.items()
            if isinstance(name, str)
            and isinstance(record, dict)
            and record.get("credential_key") == current_credential
            and record.get("canonical_operation_id")
        ]
        if len(candidates) != 1:
            raise OrdinaryAgentClientError("session_unavailable")
        enrollment_alias, enrollment = candidates[0]
        session_alias = _initial_session_alias(enrollment_alias)
        record = sessions.get(session_alias)
        if record is None:
            record = {
                "canonical_operation_id": enrollment["canonical_operation_id"],
                "credential_key": current_credential,
            }
            sessions[session_alias] = record
        if not isinstance(record, dict) or not record.get("canonical_operation_id"):
            raise OrdinaryAgentClientError("session_unavailable")
        if record["canonical_operation_id"] != enrollment["canonical_operation_id"]:
            raise OrdinaryAgentClientError("session_stale")
        if record.get("credential_key") not in (None, current_credential):
            raise OrdinaryAgentClientError("session_stale")
        record["credential_key"] = current_credential
        state["current_session"] = session_alias
        return record

    def _refresh_session_state(
        self, state: dict[str, Any], session: dict[str, Any]
    ) -> dict[str, Any]:
        if (
            not isinstance(session.get("canonical_operation_id"), str)
            or not session["canonical_operation_id"]
        ):
            raise OrdinaryAgentClientError("session_unavailable")
        current_credential = state.get("current_credential")
        if (
            current_credential is not None
            and session.get("credential_key") != current_credential
        ):
            raise OrdinaryAgentClientError("session_stale")
        payload, _ = self._request(
            "read_ordinary_agent_session_operation",
            method="GET",
            parts={"operation_id": session["canonical_operation_id"]},
            credential=self._auth_from_state(state),
        )
        operation = self._operation(payload)
        session.update(
            {
                "status": operation["status"],
                "session_id": operation.get("session_id"),
                "lease_selectors": operation.get("lease_selectors", []),
            }
        )
        return operation

    def session_status(self, *, alias: str | None = None) -> dict[str, Any]:
        with self.state.transaction() as state:
            sessions = state.get("sessions")
            raw_alias = alias or state.get("current_session")
            session_alias = (
                _alias(raw_alias, fallback="initial") if raw_alias else "initial"
            )
            session = (
                sessions.get(session_alias) if isinstance(sessions, dict) else None
            )
            current_credential = state.get("current_credential")
            if (
                isinstance(session, dict)
                and current_credential is not None
                and session.get("credential_key") != current_credential
            ):
                if alias is not None:
                    raise OrdinaryAgentClientError("session_stale")
                session = None
            if not isinstance(session, dict):
                if alias is not None:
                    raise OrdinaryAgentClientError("session_unavailable")
                session = self._initial_session_state(state)
                session_alias = str(state["current_session"])
            state["current_session"] = session_alias
            return self._refresh_session_state(state, session)

    def cancel_session(self, *, alias: str | None = None) -> dict[str, Any]:
        with self.state.transaction() as state:
            sessions = state.get("sessions")
            session_alias = _alias(
                alias, fallback=str(state.get("current_session", ""))
            )
            session = (
                sessions.get(session_alias) if isinstance(sessions, dict) else None
            )
            if not isinstance(session, dict) or not session.get(
                "canonical_operation_id"
            ):
                raise OrdinaryAgentClientError("session_unavailable")
            if state.get("current_credential") is not None and session.get(
                "credential_key"
            ) != state.get("current_credential"):
                raise OrdinaryAgentClientError("session_stale")
            payload, _ = self._request(
                "cancel_ordinary_agent_session",
                method="POST",
                parts={"operation_id": session["canonical_operation_id"]},
                body=b"",
                credential=self._auth_from_state(state),
            )
            operation = self._operation(payload)
            # A cancellation acknowledgement omits selectors; retain the
            # prior normal projection for later diagnostics.
            session["status"] = operation["status"]
            if operation.get("lease_selectors"):
                session["lease_selectors"] = operation["lease_selectors"]
            return operation

    @staticmethod
    def _selector_from_session(session: Mapping[str, Any], action: str) -> str:
        selectors = session.get("lease_selectors", [])
        matches = [
            item
            for item in selectors
            if isinstance(item, dict) and item.get("action") == action
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("lease_id"), str):
            raise OrdinaryAgentClientError("lease_unavailable")
        return matches[0]["lease_id"]

    @staticmethod
    def _validate_job_intent(request: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(request)
        if "purpose" not in body or body.get("purpose") not in {
            "qualification",
            "guarded_delivery",
        }:
            raise OrdinaryAgentClientError("invalid_job")
        if "schema_version" in body and body["schema_version"] != 2:
            raise OrdinaryAgentClientError("invalid_job")
        if "session_id" in body or "lease_id" in body:
            raise OrdinaryAgentClientError("invalid_job")
        allowed = (
            {"schema_version", "purpose", "idempotency_key"}
            if body.get("purpose") == "qualification"
            else {
                "schema_version",
                "purpose",
                "idempotency_key",
                "base_sha",
                "pull_requests",
                "permitted_stack_edit_pull_requests",
                "refresh_allowance",
            }
        )
        if set(body) - allowed:
            raise OrdinaryAgentClientError("invalid_job")
        if (
            not isinstance(body.get("idempotency_key"), str)
            or not body["idempotency_key"]
        ):
            raise OrdinaryAgentClientError("invalid_job")
        if body["purpose"] == "guarded_delivery" and not {
            "base_sha",
            "pull_requests",
            "permitted_stack_edit_pull_requests",
            "refresh_allowance",
        }.issubset(body):
            raise OrdinaryAgentClientError("invalid_job")
        return body

    @staticmethod
    def _encode_json(value: Mapping[str, Any], *, error: str) -> bytes:
        try:
            return json.dumps(
                dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise OrdinaryAgentClientError(error) from None

    def admit_job(
        self, request: Mapping[str, Any] | None = None, *, alias: str | None = None
    ) -> dict[str, Any]:
        if request is not None and not isinstance(request, Mapping):
            raise OrdinaryAgentClientError("invalid_job")
        with self.state.transaction() as state:
            jobs = state.setdefault("jobs", {})
            if not isinstance(jobs, dict):
                raise OrdinaryAgentClientError("private_state_unavailable")
            job_alias = _alias(
                alias,
                fallback=str(request.get("idempotency_key", ""))
                if isinstance(request, Mapping)
                else str(state.get("current_job", "")),
            )
            existing = jobs.get(job_alias)
            if isinstance(existing, dict) and request is None:
                encoded_value = existing.get("request_bytes_b64")
                if not isinstance(encoded_value, str):
                    raise OrdinaryAgentClientError("job_unavailable")
                encoded = self._decode_state_bytes(encoded_value)
            elif isinstance(existing, dict):
                if not isinstance(request, Mapping):
                    raise OrdinaryAgentClientError("idempotency_conflict")
                incoming = self._validate_job_intent(request)
                incoming["schema_version"] = 2
                incoming_bytes = self._encode_json(incoming, error="invalid_job")
                if request.get("idempotency_key") != existing.get(
                    "idempotency_key"
                ) or existing.get("intent_bytes_b64") != base64.b64encode(
                    incoming_bytes
                ).decode("ascii"):
                    raise OrdinaryAgentClientError("idempotency_conflict")
                encoded = self._decode_state_bytes(existing.get("request_bytes_b64"))
                # Replaying an alias always uses the immutable saved body; do
                # not rebuild it from a newer session projection.
            else:
                if not isinstance(request, Mapping):
                    raise OrdinaryAgentClientError("job_unavailable")
                body = self._validate_job_intent(request)
                session_alias = _alias(
                    str(state.get("current_session", "")), fallback="initial"
                )
                sessions = state.setdefault("sessions", {})
                session = (
                    sessions.get(session_alias) if isinstance(sessions, dict) else None
                )
                if not isinstance(session, dict):
                    session = self._initial_session_state(state)
                operation = self._refresh_session_state(state, session)
                if operation.get("status") in {
                    "expired",
                    "revoked",
                    "cancelled",
                    "blocked",
                }:
                    raise OrdinaryAgentClientError("session_stale")
                purpose = body["purpose"]
                action = "preflight" if purpose == "qualification" else "guarded_merge"
                now = int(time.time())
                selectors = session.get("lease_selectors", [])
                selected = [
                    item
                    for item in selectors
                    if isinstance(item, dict) and item.get("action") == action
                ]
                if len(selected) != 1:
                    raise OrdinaryAgentClientError("lease_unavailable")
                selector = selected[0]
                if (
                    selector.get("revoked_at") is not None
                    or not _strict_int(selector.get("expires_at"))
                    or selector["expires_at"] <= now
                ):
                    raise OrdinaryAgentClientError("lease_stale")
                body["schema_version"] = 2
                intent_bytes = self._encode_json(body, error="invalid_job")
                body["session_id"] = session["session_id"]
                body["lease_id"] = self._selector_from_session(session, action)
                try:
                    encoded = json.dumps(
                        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                    ).encode("utf-8")
                except (TypeError, ValueError):
                    raise OrdinaryAgentClientError("invalid_job") from None
                existing = {
                    "idempotency_key": body["idempotency_key"],
                    "intent_bytes_b64": base64.b64encode(intent_bytes).decode("ascii"),
                    "request_bytes_b64": base64.b64encode(encoded).decode("ascii"),
                }
                jobs[job_alias] = existing
                state["current_job"] = job_alias
                self.state.checkpoint(state)
            payload, _ = self._request(
                "admit_ordinary_agent_job",
                method="POST",
                body=encoded,
                credential=self._auth_from_state(state),
            )
            self._safe_public(payload)
            request_id = payload.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                raise OrdinaryAgentClientError("invalid_response")
            existing.update({"request_id": request_id})
            state["current_job"] = job_alias
            return payload

    def resume_job(self, *, alias: str | None = None) -> dict[str, Any]:
        return self.admit_job(None, alias=alias)

    def job_status(self, *, alias: str | None = None) -> dict[str, Any]:
        with self.state.transaction() as state:
            jobs = state.get("jobs")
            job_alias = _alias(alias, fallback=str(state.get("current_job", "")))
            job = jobs.get(job_alias) if isinstance(jobs, dict) else None
            if not isinstance(job, dict) or not isinstance(job.get("request_id"), str):
                raise OrdinaryAgentClientError("job_unavailable")
            payload, _ = self._request(
                "read_ordinary_agent_job",
                method="GET",
                parts={"request_id": job["request_id"]},
                credential=self._auth_from_state(state),
            )
            self._safe_public(payload)
            job["status"] = payload.get("status")
            return payload
