#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Hermetic behavioral tests for the portable ordinary-agent client."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self
from unittest import TestCase, main
from unittest.mock import patch

from launchplane_ordinary_agent_client import (
    OrdinaryAgentClient,
    OrdinaryAgentClientError,
    PrivateStateStore,
    claim_digest,
    receiver_proof,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ordinary-agent-responses.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
RESPONSES = FIXTURE["responses"]


class Response:
    def __init__(
        self, payload: dict[str, object], headers: dict[str, str] | None = None
    ) -> None:
        self.headers = headers or {"Content-Type": "application/json"}
        self._body = json.dumps(payload).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self._body


class Opener:
    def __init__(
        self,
        responses: list[object],
        observed: list[urllib.request.Request],
        before_open=None,
    ) -> None:
        self.responses = responses
        self.observed = observed
        self.before_open = before_open

    def open(self, request: urllib.request.Request, *, timeout: float) -> Response:
        if self.before_open:
            self.before_open(request)
        self.observed.append(request)
        assert timeout > 0
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, Response)
        return response


def response(name: str) -> Response:
    return Response(RESPONSES[name])


def provider_wait_error(
    *, code: str = "provider_readiness_in_progress", retry_after: str = "12"
) -> urllib.error.HTTPError:
    body = json.dumps(
        {
            "trace_id": "launchplane_req_fixture123",
            "error": {"code": code, "message": "provider details are hidden"},
        }
    ).encode()
    return urllib.error.HTTPError(
        "http://127.0.0.1:8123/v1/agent/ordinary-agent-jobs",
        503,
        "Service Unavailable",
        {"Retry-After": retry_after, "Content-Type": "application/json"},
        io.BytesIO(body),
    )


def enrollment() -> dict[str, object]:
    return {
        "descriptor_id": "ordinary-agent-enrollment",
        "operation_id": "retry-key-2366",
        "action": "enroll",
        "principal_id": "fixture-agent",
        "target": {
            "repository_id": 123,
            "repository": "example/project",
            "base_branch": "main",
        },
        "github_app_id": 123,
        "secret_binding_id": "binding",
        "credential_valid_from": 1_999_999_900,
        "credential_expires_at": 2_000_001_000,
        "delivery": {"expires_at": 2_000_000_120},
        "session_attenuation": None,
    }


def credential_state(
    store: PrivateStateStore, value: str = "synthetic-credential"
) -> None:
    store.update(
        lambda state: {
            **state,
            "credentials": {"fixture": value},
            "current_credential": "fixture",
        }
    )


class OrdinaryAgentClientTests(TestCase):
    def test_proof_domain_and_canonical_encoding(self) -> None:
        proof = receiver_proof(lambda size: b"x" * size)
        self.assertEqual(len(proof), 43)
        self.assertEqual(base64.urlsafe_b64decode(proof + "="), b"x" * 32)
        self.assertEqual(
            claim_digest(proof),
            FIXTURE["receiver_claim_vector"]["receiver_claim_sha256"],
        )
        self.assertNotEqual(
            claim_digest(proof), hashlib.sha256(proof.encode()).hexdigest()
        )

    def test_enrollment_state_precedes_network_and_resume_uses_saved_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PrivateStateStore(directory)
            observed: list[urllib.request.Request] = []
            checked = {"before": False}

            def before_open(_request: urllib.request.Request) -> None:
                checked["before"] = "enrollments" in json.loads(
                    Path(directory, "ordinary-agent.json").read_text()
                )

            opener = Opener(
                [urllib.error.URLError("lost response")] * 3, observed, before_open
            )
            client = OrdinaryAgentClient(
                "http://127.0.0.1:8123", state=store, terminal_credential="terminal"
            )
            with (
                patch.object(urllib.request, "build_opener", return_value=opener),
                self.assertRaisesRegex(
                    OrdinaryAgentClientError, "transport_unavailable"
                ),
            ):
                client.propose_enrollment(enrollment())
            self.assertTrue(checked["before"])
            saved = store.load()["enrollments"]["retry-key-2366"]
            retry_requests: list[urllib.request.Request] = []
            retry_opener = Opener([response("enrollment_pending")], retry_requests)
            with patch.object(
                urllib.request, "build_opener", return_value=retry_opener
            ):
                operation = client.propose_enrollment(None)
            self.assertEqual(operation["operation_id"], "fixture-enrollment")
            self.assertEqual(observed[0].data, retry_requests[0].data)
            self.assertIsNone(saved["canonical_operation_id"])
            self.assertEqual(
                operation["review_url"],
                "http://127.0.0.1:8123/ui/engineering/privileged-operations?principal_id=fixture-agent&operation_id=fixture-enrollment",
            )

    def test_corrupt_saved_enrollment_returns_bounded_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PrivateStateStore(directory)
            client = OrdinaryAgentClient(
                "http://127.0.0.1:8123", state=store, terminal_credential="terminal"
            )
            store.update(
                lambda state: {
                    **state,
                    "current_enrollment": "retry-key-2366",
                    "enrollments": {
                        "retry-key-2366": {
                            "canonical_operation_id": "fixture-enrollment",
                            "principal_id": "fixture-agent",
                        }
                    },
                }
            )
            with self.assertRaisesRegex(
                OrdinaryAgentClientError, "enrollment_unavailable"
            ):
                client.propose_enrollment()

    def test_private_state_is_bound_before_credentials_are_used(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            unbound = PrivateStateStore(directory)
            unbound.update(lambda state: {**state, "credential": "synthetic"})
            with self.assertRaisesRegex(
                OrdinaryAgentClientError, "unbound_private_state"
            ):
                OrdinaryAgentClient("http://127.0.0.1:8123", state=unbound)
        with tempfile.TemporaryDirectory() as directory:
            store = PrivateStateStore(directory)
            OrdinaryAgentClient("http://127.0.0.1:8123", state=store)
            with self.assertRaisesRegex(
                OrdinaryAgentClientError, "service_binding_mismatch"
            ):
                OrdinaryAgentClient("http://127.0.0.1:8124", state=store)

    def test_non_ascii_credential_is_rejected_before_header_construction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PrivateStateStore(directory)
            client = OrdinaryAgentClient("http://127.0.0.1:8123", state=store)
            with patch.object(urllib.request, "build_opener") as build_opener:
                with self.assertRaisesRegex(
                    OrdinaryAgentClientError, "credential_unavailable"
                ):
                    client._request(
                        "read_ordinary_agent_job",
                        method="GET",
                        parts={"request_id": "job"},
                        credential="bad-\u2603",
                    )
                build_opener.assert_not_called()

    def test_repository_relative_private_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "checkout"
            (checkout / ".git").mkdir(parents=True)
            with self.assertRaisesRegex(
                OrdinaryAgentClientError, "private_state_in_repository"
            ):
                PrivateStateStore(checkout / ".state")

    def test_overlapping_commands_fail_bounded_and_do_not_overwrite_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PrivateStateStore(directory)
            first = OrdinaryAgentClient(
                "http://127.0.0.1:8123", state=store, terminal_credential="terminal"
            )
            second = OrdinaryAgentClient(
                "http://127.0.0.1:8123", state=store, terminal_credential="terminal"
            )
            entered = threading.Event()
            release = threading.Event()

            class BlockingOpener(Opener):
                def open(
                    self, request: urllib.request.Request, *, timeout: float
                ) -> Response:
                    entered.set()
                    release.wait(timeout=2)
                    return response("enrollment_pending")

            result: list[object] = []

            def run_first() -> None:
                try:
                    result.append(first.propose_enrollment(enrollment()))
                except OrdinaryAgentClientError as exc:
                    result.append(exc)

            thread = threading.Thread(target=run_first)
            with patch.object(
                urllib.request, "build_opener", return_value=BlockingOpener([], [])
            ):
                thread.start()
                self.assertTrue(entered.wait(timeout=2))
                with self.assertRaisesRegex(
                    OrdinaryAgentClientError, "private_state_busy"
                ):
                    second.propose_enrollment(None)
                release.set()
                thread.join(timeout=2)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["operation_id"], "fixture-enrollment")

    def test_claim_custody_and_initial_session_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PrivateStateStore(directory)
            client = OrdinaryAgentClient(
                "http://127.0.0.1:8123", state=store, terminal_credential="terminal"
            )
            client.prepare_enrollment(
                enrollment(), random_bytes=lambda size: b"p" * size
            )

            def mark_claimed(state: dict[str, object]) -> dict[str, object]:
                enrollments = state["enrollments"]
                assert isinstance(enrollments, dict)
                enrollments["retry-key-2366"].update(
                    {
                        "canonical_operation_id": "fixture-enrollment",
                        "status": "claimed",
                    }
                )
                state["current_enrollment"] = "retry-key-2366"
                return state

            store.update(mark_claimed)
            claim_opener = Opener(
                [Response(RESPONSES["claim"], {"Cache-Control": "no-store"})], []
            )
            with patch.object(
                urllib.request, "build_opener", return_value=claim_opener
            ):
                self.assertEqual(client.claim(), {"status": "ready"})
            state = store.load()
            self.assertEqual(
                state["credentials"]["fixture-enrollment"],
                RESPONSES["claim"]["credential"],
            )
            session_opener = Opener([response("initial_session_issued")], [])
            with patch.object(
                urllib.request, "build_opener", return_value=session_opener
            ):
                operation = client.session_status()
            self.assertEqual(operation["session_id"], "fixture-session")
            self.assertEqual(
                operation["lease_selectors"][0]["lease_id"], "fixture-lease-preflight"
            )
            self.assertEqual(
                store.load()["sessions"]["initial-retry-key-2366"][
                    "canonical_operation_id"
                ],
                "fixture-enrollment",
            )

    def test_claim_custody_survives_enrollment_status_before_session_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PrivateStateStore(directory)
            client = OrdinaryAgentClient(
                "http://127.0.0.1:8123", state=store, terminal_credential="terminal"
            )
            observed: list[urllib.request.Request] = []
            responses = [
                response("enrollment_pending"),
                Response(RESPONSES["claim"], {"Cache-Control": "no-store"}),
                response("enrollment_approved_terminal"),
                response("initial_session_issued"),
            ]
            with patch.object(
                urllib.request, "build_opener", return_value=Opener(responses, observed)
            ):
                proposal = enrollment()
                proposal["session_attenuation"] = RESPONSES["initial_session_issued"][
                    "operation"
                ]["attenuation"]
                client.prepare_enrollment(
                    proposal, random_bytes=lambda size: b"p" * size
                )
                client.propose_enrollment()
                self.assertEqual(client.claim(), {"status": "ready"})
                self.assertEqual(client.enrollment_status()["status"], "approved")
                self.assertEqual(
                    client.session_status()["session_id"], "fixture-session"
                )
            state = store.load()
            self.assertEqual(
                state["enrollments"]["retry-key-2366"]["status"], "approved"
            )
            self.assertEqual(
                state["enrollments"]["retry-key-2366"]["credential_key"],
                "fixture-enrollment",
            )
            self.assertEqual(
                [request.headers.get("Authorization") for request in observed],
                [
                    "Bearer terminal",
                    "Bearer "
                    + state["enrollments"]["retry-key-2366"]["receiver_proof"],
                    "Bearer terminal",
                    "Bearer synthetic-ordinary-credential",
                ],
            )

    def test_claim_without_no_store_keeps_proof_and_does_not_store_credential(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PrivateStateStore(directory)
            client = OrdinaryAgentClient(
                "http://127.0.0.1:8123", state=store, terminal_credential="terminal"
            )
            client.prepare_enrollment(
                enrollment(), random_bytes=lambda size: b"n" * size
            )
            store.update(
                lambda state: (
                    state["enrollments"]["retry-key-2366"].update(
                        {"canonical_operation_id": "fixture-enrollment"}
                    )
                    or state
                )
            )
            with (
                patch.object(
                    urllib.request,
                    "build_opener",
                    return_value=Opener([Response(RESPONSES["claim"])], []),
                ),
                self.assertRaisesRegex(OrdinaryAgentClientError, "claim_cache_policy"),
            ):
                client.claim()
            state = store.load()
            self.assertNotIn("credentials", state)
            self.assertEqual(
                state["enrollments"]["retry-key-2366"]["receiver_proof"],
                receiver_proof(lambda size: b"n" * size),
            )

    def test_new_sessions_and_jobs_use_purpose_lease_and_saved_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PrivateStateStore(directory)
            client = OrdinaryAgentClient("http://127.0.0.1:8123", state=store)
            credential_state(store)
            attenuation = {
                "actions": ["preflight", "guarded_merge"],
                "session_expires_at": 2_000_000_600,
                "lease_expires_at": 2_000_000_300,
                "action_limit": 4,
                "pull_request_limit": 2,
                "refresh_allowance": 1,
            }
            responses = [
                response("session_issued"),
                response("session_issued"),
                response("job_pending"),
            ]
            observed: list[urllib.request.Request] = []
            with patch.object(
                urllib.request, "build_opener", return_value=Opener(responses, observed)
            ):
                client.propose_session(
                    {
                        "descriptor_id": "ordinary-agent-session",
                        "operation_id": "session-retry",
                        "attenuation": attenuation,
                    },
                    alias="second-session",
                )
                result = client.admit_job(
                    {
                        "purpose": "qualification",
                        "idempotency_key": "qualification-job",
                    },
                    alias="qualification-job",
                )
            self.assertEqual(result["request_id"], "fixture-job")
            self.assertEqual(
                json.loads(observed[-1].data)["lease_id"], "fixture-lease-preflight"
            )
            self.assertEqual(
                store.load()["jobs"]["qualification-job"]["request_id"], "fixture-job"
            )
            with patch.object(
                urllib.request,
                "build_opener",
                return_value=Opener([response("job_completed")], observed),
            ):
                self.assertEqual(
                    client.job_status(alias="qualification-job")["status"], "completed"
                )

            lost_observed: list[urllib.request.Request] = []
            lost = Opener(
                [response("session_issued")]
                + [urllib.error.URLError("lost response")] * 3,
                lost_observed,
            )
            guarded = {
                "purpose": "guarded_delivery",
                "idempotency_key": "guarded-job",
                "base_sha": "a" * 40,
                "pull_requests": [{"number": 1, "head_sha": "b" * 40}],
                "permitted_stack_edit_pull_requests": [1],
                "refresh_allowance": 0,
            }
            with (
                patch.object(urllib.request, "build_opener", return_value=lost),
                self.assertRaisesRegex(
                    OrdinaryAgentClientError, "transport_unavailable"
                ),
            ):
                client.admit_job(guarded, alias="guarded-job")
            saved = store.load()["jobs"]["guarded-job"]["request_bytes_b64"]
            retry_observed: list[urllib.request.Request] = []
            with patch.object(
                urllib.request,
                "build_opener",
                return_value=Opener([response("job_pending")], retry_observed),
            ):
                client.resume_job(alias="guarded-job")
            with self.assertRaisesRegex(
                OrdinaryAgentClientError, "idempotency_conflict"
            ):
                client.admit_job(
                    {"purpose": "qualification", "idempotency_key": "guarded-job"},
                    alias="guarded-job",
                )
            self.assertEqual(
                json.loads(base64.b64decode(saved))["lease_id"],
                "fixture-lease-guarded_merge",
            )
            self.assertEqual(retry_observed[0].data, base64.b64decode(saved))

    def test_lost_session_proposal_resumes_saved_bytes_without_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PrivateStateStore(directory)
            client = OrdinaryAgentClient("http://127.0.0.1:8123", state=store)
            credential_state(store)
            request = {
                "descriptor_id": "ordinary-agent-session",
                "operation_id": "lost-session",
                "attenuation": {"actions": ["preflight"]},
            }
            first_observed: list[urllib.request.Request] = []
            with (
                patch.object(
                    urllib.request,
                    "build_opener",
                    return_value=Opener(
                        [urllib.error.URLError("lost response")] * 3,
                        first_observed,
                    ),
                ),
                self.assertRaisesRegex(
                    OrdinaryAgentClientError, "transport_unavailable"
                ),
            ):
                client.propose_session(request, alias="lost-session")
            saved = store.load()["sessions"]["lost-session"]
            self.assertIsNone(saved["canonical_operation_id"])
            saved_body = base64.b64decode(saved["request_bytes_b64"])
            second_observed: list[urllib.request.Request] = []
            with patch.object(
                urllib.request,
                "build_opener",
                return_value=Opener([response("session_issued")], second_observed),
            ):
                result = client.propose_session(None, alias="lost-session")
            self.assertEqual(result["operation_id"], "fixture-session-operation")
            self.assertEqual(second_observed[0].data, saved_body)

    def test_session_proposal_missing_handle_is_rejected_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PrivateStateStore(directory)
            client = OrdinaryAgentClient("http://127.0.0.1:8123", state=store)
            credential_state(store)
            store.update(
                lambda state: {
                    **state,
                    "sessions": {
                        "missing-handle": {
                            "retry_key": "missing-handle",
                            "request_bytes_b64": base64.b64encode(b"{}").decode(),
                            "canonical_operation_id": None,
                            "credential_key": "fixture",
                        }
                    },
                }
            )
            with (
                patch.object(urllib.request, "build_opener") as build_opener,
                self.assertRaisesRegex(OrdinaryAgentClientError, "session_unavailable"),
            ):
                client.session_status(alias="missing-handle")
            build_opener.assert_not_called()

    def test_stale_session_has_no_stronger_credential_fallback_and_cancel_keeps_diagnostics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PrivateStateStore(directory)
            client = OrdinaryAgentClient("http://127.0.0.1:8123", state=store)
            credential_state(store)
            store.update(
                lambda state: {
                    **state,
                    "sessions": {
                        "initial": {
                            "canonical_operation_id": "fixture-session-operation",
                            "credential_key": "fixture",
                            "session_id": "fixture-session",
                            "lease_selectors": RESPONSES["session_issued"]["operation"][
                                "lease_selectors"
                            ],
                        }
                    },
                    "current_session": "initial",
                }
            )
            stale = Opener([response("session_revoked")], [])
            with (
                patch.object(urllib.request, "build_opener", return_value=stale),
                self.assertRaisesRegex(OrdinaryAgentClientError, "session_stale"),
            ):
                client.admit_job(
                    {"purpose": "qualification", "idempotency_key": "stale-job"},
                    alias="stale-job",
                )
            cancel = Opener([response("cancel_acknowledgement")], [])
            with patch.object(urllib.request, "build_opener", return_value=cancel):
                client.cancel_session()
            self.assertEqual(
                store.load()["sessions"]["initial"]["lease_selectors"][0]["revoked_at"],
                2_000_000_030,
            )

    def test_cancel_accepts_populated_revoked_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PrivateStateStore(directory)
            client = OrdinaryAgentClient("http://127.0.0.1:8123", state=store)
            credential_state(store)
            store.update(
                lambda state: {
                    **state,
                    "sessions": {
                        "cancel-me": {
                            "canonical_operation_id": "fixture-session-operation",
                            "credential_key": "fixture",
                            "lease_selectors": [],
                        }
                    },
                    "current_session": "cancel-me",
                }
            )
            with patch.object(
                urllib.request,
                "build_opener",
                return_value=Opener([response("session_revoked")], []),
            ):
                result = client.cancel_session()
            self.assertEqual(result["status"], "revoked")
            self.assertEqual(
                len(store.load()["sessions"]["cancel-me"]["lease_selectors"]), 2
            )

    def test_provider_wait_returns_guidance_once_and_resumes_exact_job_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PrivateStateStore(directory)
            client = OrdinaryAgentClient("http://127.0.0.1:8123", state=store)
            credential_state(store)
            store.update(
                lambda state: {
                    **state,
                    "sessions": {
                        "initial": {
                            "canonical_operation_id": "fixture-session-operation",
                            "credential_key": "fixture",
                            "session_id": "fixture-session",
                        }
                    },
                    "current_session": "initial",
                }
            )
            guarded = {
                "purpose": "guarded_delivery",
                "idempotency_key": "provider-wait-job",
                "base_sha": "a" * 40,
                "pull_requests": [{"number": 1, "head_sha": "b" * 40}],
                "permitted_stack_edit_pull_requests": [1],
                "refresh_allowance": 0,
            }
            first_observed: list[urllib.request.Request] = []
            with (
                patch.object(
                    urllib.request,
                    "build_opener",
                    return_value=Opener(
                        [response("session_issued"), provider_wait_error()],
                        first_observed,
                    ),
                ),
                self.assertRaisesRegex(
                    OrdinaryAgentClientError, "provider_readiness_in_progress"
                ) as caught,
            ):
                client.admit_job(guarded, alias="provider-wait-job")
            self.assertEqual(caught.exception.retry_after_seconds, 12)
            self.assertEqual(caught.exception.trace_id, "launchplane_req_fixture123")
            self.assertEqual(len(first_observed), 2)
            saved = store.load()["jobs"]["provider-wait-job"]["request_bytes_b64"]
            second_observed: list[urllib.request.Request] = []
            with patch.object(
                urllib.request,
                "build_opener",
                return_value=Opener([response("job_pending")], second_observed),
            ):
                client.resume_job(alias="provider-wait-job")
            self.assertEqual(second_observed[0].data, base64.b64decode(saved))

    def test_poisoned_provider_metadata_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PrivateStateStore(directory)
            client = OrdinaryAgentClient("http://127.0.0.1:8123", state=store)
            errors = [provider_wait_error(code="ghp_poison")]
            observed: list[urllib.request.Request] = []
            with (
                patch.object(
                    urllib.request,
                    "build_opener",
                    return_value=Opener(errors, observed),
                ),
                self.assertRaisesRegex(OrdinaryAgentClientError, "http_503") as caught,
            ):
                client._request(
                    "read_ordinary_agent_job",
                    method="GET",
                    parts={"request_id": "job"},
                )
            self.assertEqual(caught.exception.retry_after_seconds, 12)
            self.assertEqual(len(observed), 1)
            self.assertIsNone(caught.exception.trace_id)
            self.assertNotIn("ghp_poison", str(caught.exception))

    def test_public_operation_metadata_and_review_query_reject_poisoned_values(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = OrdinaryAgentClient(
                "http://127.0.0.1:8123", state=PrivateStateStore(directory)
            )
            payload = json.loads(json.dumps(RESPONSES["session_issued"]))
            payload["operation"]["credential_id"] = "ghp_poison"
            with self.assertRaisesRegex(
                OrdinaryAgentClientError, "unsafe_response_shape"
            ):
                client._operation(payload)
            payload = json.loads(json.dumps(RESPONSES["session_issued"]))
            payload["review_url"] += "&credential=ordinary-secret"
            with self.assertRaisesRegex(OrdinaryAgentClientError, "unsafe_review_url"):
                client._operation(payload)

    def test_initial_session_follows_active_claimed_enrollment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PrivateStateStore(directory)
            client = OrdinaryAgentClient("http://127.0.0.1:8123", state=store)
            store.update(
                lambda state: {
                    **state,
                    "credentials": {"credential-two": "ordinary-two"},
                    "current_credential": "credential-two",
                    "enrollments": {
                        "first-enrollment": {
                            "status": "claimed",
                            "credential_key": "credential-one",
                            "canonical_operation_id": "fixture-one",
                        },
                        "second-enrollment": {
                            "status": "claimed",
                            "credential_key": "credential-two",
                            "canonical_operation_id": "fixture-two",
                        },
                    },
                    "current_enrollment": "first-enrollment",
                    "sessions": {
                        "initial-first-enrollment": {
                            "canonical_operation_id": "fixture-one",
                            "credential_key": "credential-one",
                        }
                    },
                    "current_session": "initial-first-enrollment",
                }
            )
            observed: list[urllib.request.Request] = []
            with patch.object(
                urllib.request,
                "build_opener",
                return_value=Opener([response("initial_session_issued")], observed),
            ):
                client.session_status()
            self.assertTrue(
                observed[0].full_url.endswith(
                    "/v1/agent/ordinary-agent-session-proposals/fixture-two"
                )
            )
            self.assertEqual(
                store.load()["current_session"], "initial-second-enrollment"
            )

    def test_real_local_redirect_is_rejected(self) -> None:
        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.send_response(302)
                self.send_header("Location", "/redirected")
                self.end_headers()

            def log_message(self, *_args: object) -> None:
                return None

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                store = PrivateStateStore(directory)
                client = OrdinaryAgentClient(
                    f"http://127.0.0.1:{server.server_port}", state=store
                )
                client.prepare_enrollment(
                    enrollment(), random_bytes=lambda size: b"r" * size
                )
                store.update(
                    lambda state: (
                        state["enrollments"]["retry-key-2366"].update(
                            {"canonical_operation_id": "fixture-enrollment"}
                        )
                        or state
                    )
                )
                with self.assertRaisesRegex(
                    OrdinaryAgentClientError, "unsafe_redirect"
                ):
                    client.claim()
        finally:
            server.shutdown()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
