#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pytest",
# ]
# ///
"""Hermetic lifecycle tests for the portable ordinary-agent client."""

from __future__ import annotations

import base64
import json
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Self
from unittest.mock import patch

import pytest
from launchplane_ordinary_agent_client import (
    OrdinaryAgentClient,
    OrdinaryAgentClientError,
    PrivateStateStore,
    claim_digest,
    receiver_proof,
)


class _Response:
    def __init__(self, payload: dict[str, object], headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {"Content-Type": "application/json"}
        self._body = json.dumps(payload).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self._body


class _Opener:
    def __init__(self, responses: list[object], observed: list[urllib.request.Request]) -> None:
        self.responses = responses
        self.observed = observed

    def open(self, request: urllib.request.Request, *, timeout: float) -> _Response:
        assert timeout > 0
        self.observed.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, _Response)
        return response


def _operation(
    operation_id: str,
    *,
    status: str = "approved",
    session_id: str | None = None,
    selectors: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "principal_id": "ordinary-agent",
        "operation_id": operation_id,
        "kind": "existing",
        "status": status,
        "requester_kind": "ordinary_agent",
        "requester_subject": "ordinary-agent",
        "credential_expires_at": 2_000_000_000,
        "credential_id": "credential",
        "credential_version": 1,
        "target": {"repository_id": 1, "repository": "owner/repository", "base_branch": "main"},
        "session_id": session_id,
        "lease_selectors": selectors or [],
    }


def _enrollment() -> dict[str, object]:
    return {
        "descriptor_id": "ordinary-agent-enrollment",
        "operation_id": "retry-key-2366",
        "action": "enroll",
        "principal_id": "ordinary-agent",
        "target": {"repository_id": 1, "repository": "owner/repository", "base_branch": "main"},
        "github_app_id": 123,
        "secret_binding_id": "binding",
        "credential_valid_from": 1_000,
        "credential_expires_at": 2_000,
        "delivery": {"expires_at": 1_500},
        "session_attenuation": None,
    }


def test_receiver_proof_is_canonical_and_claim_is_domain_separated() -> None:
    proof = receiver_proof(lambda size: b"x" * size)
    assert len(proof) == 43
    assert base64.urlsafe_b64decode(proof + "=") == b"x" * 32
    assert len(claim_digest(proof)) == 64


def test_enrollment_saves_proof_before_network_and_retries_exact_bytes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = PrivateStateStore(directory)
        observed: list[urllib.request.Request] = []
        responses = [urllib.error.URLError("lost response")] * 3
        opener = _Opener(responses, observed)
        client = OrdinaryAgentClient("http://127.0.0.1:8123", state=store, terminal_credential="terminal")
        with patch.object(OrdinaryAgentClient, "prepare_enrollment", wraps=client.prepare_enrollment) as prepare, patch.object(urllib.request, "build_opener", return_value=opener):
            with pytest.raises(OrdinaryAgentClientError, match="transport_unavailable"):
                client.propose_enrollment(_enrollment())
            prepared = store.load()["enrollment"]
            assert Path(directory, "ordinary-agent.json").stat().st_mode & 0o077 == 0
        retry_observed: list[urllib.request.Request] = []
        retry_opener = _Opener(
            [_Response({"operation": _operation("canonical-enrollment", status="pending")})],
            retry_observed,
        )
        with patch.object(urllib.request, "build_opener", return_value=retry_opener):
            operation = client.propose_enrollment(_enrollment())
        assert prepare.call_count == 1
        assert operation["operation_id"] == "canonical-enrollment"
        assert observed[0].data == retry_observed[0].data
        assert prepared["canonical_operation_id"] is None
        assert observed[0].get_header("Authorization") == "Bearer terminal"


def test_claim_requires_no_store_and_stores_credential_before_ready() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = PrivateStateStore(directory)
        client = OrdinaryAgentClient("http://127.0.0.1:8123", state=store)
        client.prepare_enrollment(_enrollment(), random_bytes=lambda size: b"p" * size)
        state = store.load()
        state["enrollment"]["canonical_operation_id"] = "canonical-enrollment"
        client.state.replace(state)
        response = _Response({"schema_version": 1, "status": "ready", "credential": "synthetic-credential"}, {"Cache-Control": "no-store"})
        opener = _Opener([response], [])
        with patch.object(urllib.request, "build_opener", return_value=opener):
            assert client.claim() == {"status": "ready"}
        assert store.load()["credential"] == "synthetic-credential"
        assert "synthetic-credential" not in repr(client.claim)


def test_session_selects_returned_lease_and_job_status_uses_returned_request_id() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = PrivateStateStore(directory)
        store.replace({"credential": "ordinary-credential"})
        client = OrdinaryAgentClient("http://127.0.0.1:8123", state=store)
        responses = [
            _Response({"operation": _operation("canonical-session", status="approved", session_id="session")}),
            _Response({"operation": _operation("canonical-session", status="approved", session_id="session", selectors=[{"lease_id": "service-lease", "action": "preflight", "expires_at": 2_000}])}),
            _Response({"request_id": "service-request", "status": "pending"}),
            _Response({"request_id": "service-request", "status": "completed"}),
        ]
        observed: list[urllib.request.Request] = []
        opener = _Opener(responses, observed)
        attenuation = {"actions": ["preflight"], "session_expires_at": 1_900, "lease_expires_at": 1_800, "action_limit": 1, "pull_request_limit": 0, "refresh_allowance": 0}
        with patch.object(urllib.request, "build_opener", return_value=opener):
            client.propose_session({"descriptor_id": "ordinary-agent-session", "operation_id": "session-retry", "attenuation": attenuation})
            client.session_status()
            result = client.admit_job({"purpose": "qualification", "idempotency_key": "job-retry"})
            assert client.job_status()["status"] == "completed"
        assert result["request_id"] == "service-request"
        assert json.loads(observed[2].data or b"{}")["lease_id"] == "service-lease"
        assert observed[3].full_url.endswith("/v1/agent/ordinary-agent-jobs/service-request")


def test_cancel_accepts_empty_selector_ack_and_keeps_diagnostic_projection() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = PrivateStateStore(directory)
        store.replace(
            {
                "credential": "ordinary-credential",
                "session": {
                    "canonical_operation_id": "canonical-session",
                    "session_id": "session",
                    "lease_selectors": [
                        {"lease_id": "revoked-lease", "action": "preflight", "expires_at": 10, "revoked_at": 11}
                    ],
                },
            }
        )
        client = OrdinaryAgentClient("http://127.0.0.1:8123", state=store)
        opener = _Opener(
            [_Response({"operation": _operation("canonical-session", status="cancelled")})],
            [],
        )
        with patch.object(urllib.request, "build_opener", return_value=opener):
            assert client.cancel_session()["status"] == "cancelled"
        assert store.load()["session"]["lease_selectors"][0]["revoked_at"] == 11


def test_claim_redirect_and_cache_policy_are_rejected_without_secret_fallback() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = PrivateStateStore(directory)
        client = OrdinaryAgentClient("http://127.0.0.1:8123", state=store)
        client.prepare_enrollment(_enrollment(), random_bytes=lambda size: b"r" * size)
        state = store.load()
        state["enrollment"]["canonical_operation_id"] = "canonical-enrollment"
        client.state.replace(state)
        opener = _Opener([_Response({"schema_version": 1, "status": "ready", "credential": "secret"})], [])
        with patch.object(urllib.request, "build_opener", return_value=opener), pytest.raises(OrdinaryAgentClientError, match="claim_cache_policy"):
            client.claim()
        assert "secret" not in json.dumps(store.load())
