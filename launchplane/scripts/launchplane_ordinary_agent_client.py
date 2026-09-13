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
import fcntl
import hashlib
import json
import os
import secrets
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:  # Direct execution and import from the skill's scripts directory.
    from launchplane_contract import operation_path
    from launchplane_safety import (
        LaunchplaneSafetyError,
        assert_public_safe_shape,
        build_launchplane_url,
        validate_service_url,
    )
except ImportError:  # pragma: no cover - package-style import fallback
    from .launchplane_contract import operation_path
    from .launchplane_safety import (
        LaunchplaneSafetyError,
        assert_public_safe_shape,
        build_launchplane_url,
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


class OrdinaryAgentClientError(RuntimeError):
    """Bounded public-safe client error; never contains private values."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _private_root_default() -> Path:
    configured = os.environ.get("LAUNCHPLANE_ORDINARY_AGENT_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        if base:
            return Path(base) / "Launchplane" / "ordinary-agent"
    if os.environ.get("XDG_STATE_HOME", "").strip():
        return Path(os.environ["XDG_STATE_HOME"]) / "launchplane" / "ordinary-agent"
    if sys_platform_is_macos():
        return Path.home() / "Library" / "Application Support" / "Launchplane" / "ordinary-agent"
    return Path.home() / ".local" / "state" / "launchplane" / "ordinary-agent"


def sys_platform_is_macos() -> bool:
    return os.sys.platform == "darwin"


def receiver_proof(random_bytes: Any = secrets.token_bytes) -> str:
    """Create one canonical unpadded base64url 32-byte receiver proof."""

    material = random_bytes(PROOF_BYTES)
    if not isinstance(material, bytes) or len(material) != PROOF_BYTES:
        raise OrdinaryAgentClientError("invalid_receiver_proof")
    encoded = base64.urlsafe_b64encode(material).decode("ascii").rstrip("=")
    if len(encoded) != PROOF_LENGTH or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in encoded):
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
    if len(decoded) != PROOF_BYTES or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != proof:
        raise OrdinaryAgentClientError("invalid_receiver_proof")
    return hashlib.sha256(CLAIM_DOMAIN + proof.encode("ascii")).hexdigest()


def _strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _ensure_owner_private(path: Path, *, directory: bool) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise OrdinaryAgentClientError("unsafe_private_state")
    if directory:
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise OrdinaryAgentClientError("unsafe_private_state")
    elif not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise OrdinaryAgentClientError("unsafe_private_state")


class PrivateStateStore:
    """Small owner-only JSON store with atomic durable replacement."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root).expanduser() if root is not None else _private_root_default()
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            _ensure_owner_private(self.root, directory=True)
            self.root.chmod(0o700)
        except (OSError, OrdinaryAgentClientError) as exc:
            if isinstance(exc, OrdinaryAgentClientError):
                raise
            raise OrdinaryAgentClientError("private_state_unavailable") from None
        self._lock_path = self.root / _LOCK_FILE
        self._state_path = self.root / _STATE_FILE

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            _ensure_owner_private(self._lock_path, directory=False)
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
        except (OSError, OrdinaryAgentClientError):
            raise OrdinaryAgentClientError("private_state_unavailable") from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
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
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        except (TypeError, ValueError):
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
            directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
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
                encoded = json.dumps(updated, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            except (TypeError, ValueError):
                raise OrdinaryAgentClientError("private_state_unavailable") from None
            if len(encoded) > MAX_RESPONSE_BYTES:
                raise OrdinaryAgentClientError("private_state_unavailable")
            self._replace_unlocked(encoded)
            return updated


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
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
            validate_service_url(service_url)
        except LaunchplaneSafetyError:
            raise OrdinaryAgentClientError("invalid_service_url") from None
        if timeout <= 0 or attempts <= 0:
            raise OrdinaryAgentClientError("invalid_transport_limits")
        self.service_url = service_url.rstrip("/")
        self.state = state or PrivateStateStore()
        self.terminal_credential = terminal_credential or os.environ.get("LAUNCHPLANE_TERMINAL_CREDENTIAL", "")
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
        return credential

    def _url(self, contract_operation_id: str, **parts: str) -> str:
        try:
            path = operation_path(contract_operation_id)
            for name, value in parts.items():
                path = path.replace("{" + name + "}", urllib.parse.quote(value, safe=""))
            if "{" in path or "}" in path:
                raise OrdinaryAgentClientError("invalid_operation_path")
            return build_launchplane_url(self.service_url, path)
        except (LaunchplaneSafetyError, ValueError):
            raise OrdinaryAgentClientError("invalid_operation_path") from None

    def _request(self, operation_id: str, *, method: str, body: bytes = b"", credential: str = "", claim: bool = False) -> tuple[dict[str, Any], Any]:
        url = self._url(operation_id)
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        if method == "POST":
            headers["Content-Length"] = str(len(body))
        request = urllib.request.Request(url, data=body if method == "POST" else None, headers=headers, method=method)
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
                    if claim:
                        cache = response.headers.get("Cache-Control", "")
                        if "no-store" not in {part.strip().lower() for part in cache.split(",")}: 
                            raise OrdinaryAgentClientError("claim_cache_policy")
                    return payload, response.headers
            except OrdinaryAgentClientError:
                raise
            except urllib.error.HTTPError as exc:
                if 500 <= exc.code < 600 and attempt + 1 < self.attempts:
                    continue
                raise OrdinaryAgentClientError(f"http_{exc.code}") from None
            except (LaunchplaneSafetyError, urllib.error.URLError, TimeoutError, OSError):
                if attempt + 1 >= self.attempts:
                    raise OrdinaryAgentClientError("transport_unavailable") from None
        raise OrdinaryAgentClientError("transport_unavailable")

    @staticmethod
    def _operation(payload: Mapping[str, Any], *, selectors: bool = True) -> dict[str, Any]:
        operation = payload.get("operation")
        if not isinstance(operation, dict):
            raise OrdinaryAgentClientError("invalid_response")
        allowed = {"schema_version", "principal_id", "operation_id", "kind", "status", "reason_code", "current_policy_actions", "requester_kind", "requester_subject", "credential_expires_at", "delivery_expires_at", "attenuation", "credential_id", "credential_version", "target", "session_id", "session_expires_at", "lease_selectors", "applied", "can_approve"}
        if set(operation) - allowed:
            raise OrdinaryAgentClientError("invalid_response")
        for key in ("principal_id", "operation_id", "kind", "status", "requester_kind", "requester_subject"):
            if not isinstance(operation.get(key), str) or not operation[key]:
                raise OrdinaryAgentClientError("invalid_response")
        if selectors:
            raw = operation.get("lease_selectors", [])
            if not isinstance(raw, list):
                raise OrdinaryAgentClientError("invalid_response")
            for item in raw:
                if not isinstance(item, dict) or set(item) - {"lease_id", "action", "expires_at", "revoked_at"}:
                    raise OrdinaryAgentClientError("invalid_response")
                if not isinstance(item.get("lease_id"), str) or not isinstance(item.get("action"), str):
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
        try:
            safe_operation = dict(operation)
            # These are public view metadata, but the generic helper's
            # denylist intentionally rejects keys containing credential.
            for private_key in ("credential_id", "credential_expires_at", "credential_version"):
                safe_operation.pop(private_key, None)
            assert_public_safe_shape(safe_operation)
        except LaunchplaneSafetyError:
            raise OrdinaryAgentClientError("unsafe_response_shape") from None
        return operation

    @staticmethod
    def _safe_public(value: Any) -> Any:
        try:
            assert_public_safe_shape(value)
        except LaunchplaneSafetyError:
            raise OrdinaryAgentClientError("unsafe_response_shape") from None
        return value

    def _auth(self) -> str:
        credential = self._read().get("credential")
        if not isinstance(credential, str) or not credential:
            raise OrdinaryAgentClientError("credential_unavailable")
        return credential

    def prepare_enrollment(self, proposal: Mapping[str, Any], *, random_bytes: Any = secrets.token_bytes) -> dict[str, Any]:
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
        if set(request) != expected_keys or request.get("descriptor_id") != "ordinary-agent-enrollment":
            raise OrdinaryAgentClientError("invalid_enrollment")
        operation_id = request.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise OrdinaryAgentClientError("invalid_enrollment")
        if request.get("action") not in {"enroll", "rotate_credential"} or not isinstance(request.get("principal_id"), str):
            raise OrdinaryAgentClientError("invalid_enrollment")
        if not all(
            _strict_int(request.get(key))
            for key in ("github_app_id", "credential_valid_from", "credential_expires_at")
        ):
            raise OrdinaryAgentClientError("invalid_enrollment")
        delivery = request.get("delivery")
        if not isinstance(delivery, Mapping) or set(delivery) != {"expires_at"} or not isinstance(delivery.get("expires_at"), int):
            raise OrdinaryAgentClientError("invalid_enrollment")
        if not _strict_int(delivery.get("expires_at")):
            raise OrdinaryAgentClientError("invalid_enrollment")
        proposal_fingerprint = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        def prepare_state(state: dict[str, Any]) -> dict[str, Any]:
            existing = state.get("enrollment")
            if isinstance(existing, dict) and existing.get("retry_key") == operation_id:
                if existing.get("proposal_fingerprint") != proposal_fingerprint:
                    raise OrdinaryAgentClientError("idempotency_conflict")
                return state
            if isinstance(existing, dict) and existing:
                raise OrdinaryAgentClientError("enrollment_exists")
            proof = receiver_proof(random_bytes)
            request["delivery"] = {"receiver_claim_sha256": claim_digest(proof), "expires_at": delivery["expires_at"]}
            encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            state["enrollment"] = {"retry_key": operation_id, "proposal_fingerprint": proposal_fingerprint, "request_bytes_b64": base64.b64encode(encoded).decode("ascii"), "receiver_proof": proof, "canonical_operation_id": None, "principal_id": request.get("principal_id"), "status": "prepared"}
            return state

        state = self.state.update(prepare_state)
        return state["enrollment"]

    def propose_enrollment(self, proposal: Mapping[str, Any]) -> dict[str, Any]:
        prepared = self.prepare_enrollment(proposal)
        body = base64.b64decode(prepared["request_bytes_b64"])
        payload, _ = self._request("propose_ordinary_agent_enrollment", method="POST", body=body, credential=self.terminal_credential)
        operation = self._operation(payload, selectors=False)
        state = self._read()
        state["enrollment"].update({"canonical_operation_id": operation["operation_id"], "principal_id": operation["principal_id"], "status": operation["status"]})
        self._write(state)
        return operation

    def enrollment_status(self) -> dict[str, Any]:
        record = self._read().get("enrollment")
        if not isinstance(record, dict) or not record.get("canonical_operation_id") or not record.get("principal_id"):
            raise OrdinaryAgentClientError("enrollment_unavailable")
        url = self._url("read_proposed_ordinary_agent_enrollment", principal_id=record["principal_id"], operation_id=record["canonical_operation_id"])
        payload, _ = self._request_url(url, method="GET", credential=self.terminal_credential)
        return self._operation(payload, selectors=False)

    def _request_url(self, url: str, *, method: str, body: bytes = b"", credential: str = "", claim: bool = False) -> tuple[dict[str, Any], Any]:
        # Keep operation-ID routing in _url; this small adapter handles paths
        # with two placeholders without adding a second route map.
        parsed = urllib.parse.urlsplit(url)
        if parsed.query or parsed.fragment:
            raise OrdinaryAgentClientError("invalid_operation_path")
        headers = {"Accept": "application/json"}
        if method == "POST":
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        request = urllib.request.Request(url, data=body if method == "POST" else None, headers=headers, method=method)
        opener = urllib.request.build_opener(_NoRedirect())
        for attempt in range(self.attempts):
            try:
                opened = opener.open(request, timeout=self.timeout)
                with opened as response:
                    if response.headers.get("Content-Length", "").isdigit() and int(response.headers["Content-Length"]) > MAX_RESPONSE_BYTES:
                        raise OrdinaryAgentClientError("response_too_large")
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise OrdinaryAgentClientError("response_too_large")
                    payload = json.loads(raw.decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise OrdinaryAgentClientError("invalid_response")
                    if claim and "no-store" not in {part.strip().lower() for part in response.headers.get("Cache-Control", "").split(",")}:
                        raise OrdinaryAgentClientError("claim_cache_policy")
                    return payload, response.headers
            except OrdinaryAgentClientError:
                raise
            except urllib.error.HTTPError as exc:
                if 500 <= exc.code < 600 and attempt + 1 < self.attempts:
                    continue
                raise OrdinaryAgentClientError(f"http_{exc.code}") from None
            except (LaunchplaneSafetyError, urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
                if attempt + 1 >= self.attempts:
                    raise OrdinaryAgentClientError("transport_unavailable") from None
        raise OrdinaryAgentClientError("transport_unavailable")

    def claim(self) -> dict[str, str]:
        state = self._read()
        enrollment = state.get("enrollment")
        if not isinstance(enrollment, dict) or not enrollment.get("canonical_operation_id") or not enrollment.get("receiver_proof"):
            raise OrdinaryAgentClientError("enrollment_unavailable")
        url = self._url("claim_ordinary_agent_credential", operation_id=enrollment["canonical_operation_id"])
        payload, _ = self._request_url(url, method="POST", credential=enrollment["receiver_proof"], claim=True)
        if set(payload) != {"schema_version", "status", "credential"} or payload.get("schema_version") != 1 or payload.get("status") != "ready":
            raise OrdinaryAgentClientError("invalid_claim_response")
        credential = self._credential(payload)
        state["credential"] = credential
        state["enrollment"]["status"] = "claimed"
        self._write(state)
        return {"status": "ready"}

    def propose_session(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if (
            set(request) != {"descriptor_id", "operation_id", "attenuation"}
            or request.get("descriptor_id") != "ordinary-agent-session"
            or not isinstance(request.get("attenuation"), Mapping)
        ):
            raise OrdinaryAgentClientError("invalid_session")
        operation_id = request.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise OrdinaryAgentClientError("invalid_session")
        try:
            body = json.dumps(dict(request), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        except (TypeError, ValueError):
            raise OrdinaryAgentClientError("invalid_session") from None
        state = self._read()
        existing = state.get("session")
        if isinstance(existing, dict) and existing:
            if existing.get("retry_key") != operation_id:
                raise OrdinaryAgentClientError("session_exists")
            stored_body = existing.get("request_bytes_b64")
            if not isinstance(stored_body, str) or stored_body != base64.b64encode(body).decode("ascii"):
                raise OrdinaryAgentClientError("idempotency_conflict")
        else:
            state["session"] = {
                "retry_key": operation_id,
                "request_bytes_b64": base64.b64encode(body).decode("ascii"),
                "canonical_operation_id": operation_id,
            }
            self._write(state)
        payload, _ = self._request("propose_ordinary_agent_session", method="POST", body=body, credential=self._auth())
        operation = self._operation(payload)
        state = self._read()
        state["session"].update({"canonical_operation_id": operation["operation_id"], "status": operation["status"]})
        self._write(state)
        return operation

    def session_status(self) -> dict[str, Any]:
        session = self._read().get("session")
        if not isinstance(session, dict) or not session.get("canonical_operation_id"):
            raise OrdinaryAgentClientError("session_unavailable")
        url = self._url("read_ordinary_agent_session_operation", operation_id=session["canonical_operation_id"])
        payload, _ = self._request_url(url, method="GET", credential=self._auth())
        operation = self._operation(payload)
        state = self._read()
        state["session"].update({"status": operation["status"], "session_id": operation.get("session_id"), "lease_selectors": operation.get("lease_selectors", [])})
        self._write(state)
        return operation

    def cancel_session(self) -> dict[str, Any]:
        session = self._read().get("session")
        if not isinstance(session, dict) or not session.get("canonical_operation_id"):
            raise OrdinaryAgentClientError("session_unavailable")
        url = self._url("cancel_ordinary_agent_session", operation_id=session["canonical_operation_id"])
        payload, _ = self._request_url(url, method="POST", body=b"", credential=self._auth())
        operation = self._operation(payload, selectors=False)
        state = self._read()
        # The cancellation acknowledgement intentionally omits selectors;
        # retain the last ordinary status projection for later diagnostics.
        state["session"].update({"status": operation["status"]})
        self._write(state)
        return operation

    def _selector(self, action: str) -> str:
        session = self._read().get("session")
        if not isinstance(session, dict) or not session.get("session_id"):
            self.session_status()
            session = self._read().get("session")
        selectors = session.get("lease_selectors", []) if isinstance(session, dict) else []
        matches = [item for item in selectors if isinstance(item, dict) and item.get("action") == action]
        if len(matches) != 1 or not isinstance(matches[0].get("lease_id"), str):
            raise OrdinaryAgentClientError("lease_unavailable")
        return matches[0]["lease_id"]

    def admit_job(self, request: Mapping[str, Any], *, action: str = "preflight") -> dict[str, Any]:
        if not isinstance(request, Mapping):
            raise OrdinaryAgentClientError("invalid_job")
        state = self._read().get("session")
        if not isinstance(state, dict) or not state.get("session_id"):
            self.session_status()
            state = self._read().get("session")
        if not isinstance(state, dict) or not isinstance(state.get("session_id"), str):
            raise OrdinaryAgentClientError("session_unavailable")
        body = dict(request)
        if "purpose" not in body or body.get("purpose") not in {"qualification", "guarded_delivery"}:
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
        if not isinstance(body.get("idempotency_key"), str) or not body["idempotency_key"]:
            raise OrdinaryAgentClientError("invalid_job")
        if body["purpose"] == "guarded_delivery" and not {
            "base_sha",
            "pull_requests",
            "permitted_stack_edit_pull_requests",
            "refresh_allowance",
        }.issubset(body):
            raise OrdinaryAgentClientError("invalid_job")
        body["schema_version"] = 2
        body["session_id"] = state["session_id"]
        body["lease_id"] = self._selector(action)
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        idempotency_key = body.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise OrdinaryAgentClientError("invalid_job")
        current_job = self._read().get("job")
        if isinstance(current_job, dict) and current_job:
            if current_job.get("idempotency_key") != idempotency_key:
                raise OrdinaryAgentClientError("job_exists")
            if current_job.get("request_bytes_b64") != base64.b64encode(encoded).decode("ascii"):
                raise OrdinaryAgentClientError("idempotency_conflict")
        else:
            state_with_job = self._read()
            state_with_job["job"] = {
                "idempotency_key": idempotency_key,
                "request_bytes_b64": base64.b64encode(encoded).decode("ascii"),
            }
            self._write(state_with_job)
        payload, _ = self._request("admit_ordinary_agent_job", method="POST", body=encoded, credential=self._auth())
        self._safe_public(payload)
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise OrdinaryAgentClientError("invalid_response")
        state = self._read()
        state["job"].update({"request_id": request_id, "idempotency_key": idempotency_key})
        self._write(state)
        return payload

    def job_status(self) -> dict[str, Any]:
        job = self._read().get("job")
        if not isinstance(job, dict) or not isinstance(job.get("request_id"), str):
            raise OrdinaryAgentClientError("job_unavailable")
        url = self._url("read_ordinary_agent_job", request_id=job["request_id"])
        payload, _ = self._request_url(url, method="GET", credential=self._auth())
        self._safe_public(payload)
        return payload
