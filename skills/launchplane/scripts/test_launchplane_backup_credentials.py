# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1"]
# ///
from __future__ import annotations

import io
import json
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from launchplane_backup_credentials import BINDINGS, CREDENTIAL_KEYS, PREFLIGHT
from test_launchplane_helpers import write_action as h

SCOPE = {"product": "example-product", "context": "example", "instance": "prod"}
BINDING_IDS = ("binding-private", "binding-hosts")
DIGEST = "a" * 64


def metadata(applied: bool = False) -> dict[str, Any]:
    return {
        "status": "accepted",
        "trace_id": "trace-read",
        "result": {
            **SCOPE,
            "credentials": [
                {
                    "binding_id": value,
                    "binding_key": key if applied else f"old-{index}",
                    "version_id": f"version-{index}",
                    "ciphertext": "PRIVATE_SENTINEL",
                }
                for index, (key, value) in enumerate(zip(CREDENTIAL_KEYS, BINDING_IDS, strict=True))
            ],
            "secret_value": "PRIVATE_SENTINEL",
        },
    }


def plan(applied: bool = False) -> dict[str, Any]:
    return h._ServiceResponse(
        {
            "status": "accepted",
            "trace_id": "trace-plan",
            "result": {
                "plan_sha256": DIGEST,
                "changed": True,
                "applied": applied,
                "private": "PRIVATE_SENTINEL",
                "changes": [
                    {"binding_id": value, "from_key": f"old-{index}", "to_key": key}
                    for index, (key, value) in enumerate(zip(CREDENTIAL_KEYS, BINDING_IDS, strict=True))
                ],
            },
        },
        202,
    )


def argv(command: str) -> list[str]:
    arguments = [command, *[value for key, item in SCOPE.items() for value in ("--" + key, item)]]
    if command == PREFLIGHT:
        return arguments + ["--promotion-action", "generic_web_prod_promotion"]
    if command.endswith(("-dry-run", "-apply")):
        arguments += [
            "--private-key-binding",
            BINDING_IDS[0],
            "--known-hosts-binding",
            BINDING_IDS[1],
            "--reason",
            "PRIVATE_SENTINEL",
            "--idempotency-key",
            "backup-operation-1",
        ]
    return arguments


def invoke(arguments: list[str], reads: list[Any], posts: list[Any] | None = None) -> tuple[int, dict[str, Any], Any, Any]:
    output = io.StringIO()
    with (
        patch.object(h, "prepare_operator_settings", return_value={"service_url": "https://example.invalid"}),
        patch.object(h, "request_launchplane_read", side_effect=reads) as get,
        patch.object(h, "request_launchplane", side_effect=posts or []) as post,
        redirect_stdout(output),
    ):
        code = h.main(arguments)
    payload = json.loads(output.getvalue())
    assert "PRIVATE_SENTINEL" not in json.dumps(payload)
    return code, payload, get, post


def apply_args(tmp_path: Path) -> tuple[list[str], dict[str, Any]]:
    code, evidence, _, _ = invoke(argv(BINDINGS + "-dry-run"), [metadata()], [plan()])
    assert code == 0
    path = tmp_path / "review.json"
    path.write_text(json.dumps(evidence))
    return argv(BINDINGS + "-apply") + [
        "--reviewed-dry-run",
        "--dry-run-evidence-file",
        str(path),
        "--expected-plan-digest",
        evidence["result"]["plan_sha256"],
    ], evidence


def test_metadata_is_exact_lane_and_reference_only() -> None:
    code, payload, get, post = invoke(argv(BINDINGS + "-read"), [metadata()])
    assert code == 0 and not post.called
    assert len(payload["result"]["bindings"]) == len(BINDING_IDS)
    assert get.call_args.kwargs["query"] == {}
    for field in SCOPE:
        bad = metadata()
        bad["result"][field] = "other-lane"
        code, _, _, post = invoke(argv(BINDINGS + "-read"), [bad])
        assert code != 0 and not post.called


def test_preflight_only_uses_get_and_rejects_capture() -> None:
    response = {
        "status": "accepted",
        "trace_id": "trace-preflight",
        "result": {
            "status": "pass",
            "error_code": "",
            "policy_record_id": "policy-record",
            "policy_revision": 1,
            "credential_binding_id": "capture-boundary",
            "boundary_status": "match_capture_only",
            "storage_status": "active_pbs",
            "finished_at": "2026-10-26T09:00:00Z",
            "capture_started": False,
            "provider": "PRIVATE_SENTINEL",
        },
    }
    code, payload, get, post = invoke(argv(PREFLIGHT), [response])
    assert code == 0 and payload["result"]["capture_started"] is False and not post.called
    assert get.call_args.kwargs["query"] == {**SCOPE, "promotion_action": "generic_web_prod_promotion"}
    response["result"]["capture_started"] = True
    code, _, _, post = invoke(argv(PREFLIGHT), [response])
    assert code != 0 and not post.called


def test_rebinding_preserves_service_contract_and_checks_versions(tmp_path: Path) -> None:
    arguments, _ = apply_args(tmp_path)
    code, payload, get, post = invoke(arguments, [metadata(True)], [plan(True)])
    assert code == 0 and payload["result"]["readback_matches"] is True
    assert post.call_args.kwargs["body"] == {
        "mode": "apply",
        "bindings": dict(zip(CREDENTIAL_KEYS, BINDING_IDS, strict=True)),
        "reason": "PRIVATE_SENTINEL",
        "promotion_action": "",
        "reviewed_plan_sha256": DIGEST,
    }
    assert post.call_args.kwargs["idempotency_key"] == "backup-operation-1"
    assert get.call_count == 1
    for field in ("version_id", "binding_key", "binding_id"):
        observed = metadata(True)
        observed["result"]["credentials"][0][field] = "drifted"
        code, payload, _, post = invoke(arguments, [observed], [plan(True)])
        assert code != 0 and payload["status"] == "accepted_unverified" and post.call_count == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("--product", "other"),
        ("--context", "other"),
        ("--reason", "changed"),
        ("--private-key-binding", "other-binding"),
        ("--idempotency-key", "different-key"),
        ("--expected-plan-digest", "b" * 64),
    ],
)
def test_changed_intent_or_review_never_sends_apply(tmp_path: Path, field: str, value: str) -> None:
    arguments, _ = apply_args(tmp_path)
    arguments[arguments.index(field) + 1] = value
    code, _, get, post = invoke(arguments, [])
    assert code != 0 and not post.called and not get.called


def test_unavailable_binding_never_sends_dry_run() -> None:
    observed = metadata()
    observed["result"]["credentials"].pop()
    code, _, _, post = invoke(argv(BINDINGS + "-dry-run"), [observed])
    assert code != 0 and not post.called


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),
        urllib.error.HTTPError(
            "https://example.invalid",
            503,
            "failed",
            {},
            io.BytesIO(b'{"trace_id":"trace-error","error":{"code":"unavailable","message":"PRIVATE_SENTINEL"}}'),
        ),
    ],
)
def test_uncertain_apply_never_retries(tmp_path: Path, failure: Exception) -> None:
    arguments, _ = apply_args(tmp_path)
    code, payload, get, post = invoke(arguments, [], [failure])
    assert code != 0 and payload["status"] == "outcome_unknown"
    assert post.call_count == 1 and not get.called
    assert payload["request"]["idempotency_key_sha256"]


def test_denial_and_unverified_readback_preserve_receipt(tmp_path: Path) -> None:
    arguments, _ = apply_args(tmp_path)
    denial = urllib.error.HTTPError(
        "https://example.invalid",
        403,
        "denied",
        {},
        io.BytesIO(b'{"trace_id":"trace-denied","error":{"code":"authorization_denied","message":"PRIVATE_SENTINEL"}}'),
    )
    code, payload, _, post = invoke(arguments, [], [denial])
    assert code != 0 and payload["status"] == "denied" and post.call_count == 1
    code, payload, _, post = invoke(arguments, [TimeoutError()], [plan(True)])
    assert code != 0 and payload["status"] == "accepted_unverified" and payload["result"]["applied"] is True
    assert payload["summary"]["trace_id"] == "trace-plan" and post.call_count == 1


@pytest.mark.parametrize("part", ["status", "operation", "request", "result"])
def test_ineligible_saved_review_never_sends_apply(tmp_path: Path, part: str) -> None:
    arguments, evidence = apply_args(tmp_path)
    evidence[part] = None
    Path(arguments[arguments.index("--dry-run-evidence-file") + 1]).write_text(json.dumps(evidence))
    code, _, get, post = invoke(arguments, [])
    assert code != 0 and not get.called and not post.called


def test_changed_service_and_mismatched_plan_never_verify_apply(tmp_path: Path) -> None:
    arguments, evidence = apply_args(tmp_path)
    evidence["request"]["service_url_sha256"] = "b" * 64
    Path(arguments[arguments.index("--dry-run-evidence-file") + 1]).write_text(json.dumps(evidence))
    code, _, get, post = invoke(arguments, [])
    assert code != 0 and not get.called and not post.called
    arguments, _ = apply_args(tmp_path)
    response = plan(True)
    response["result"]["plan_sha256"] = "b" * 64
    code, payload, get, post = invoke(arguments, [], [response])
    assert code != 0 and payload["status"] == "accepted_unverified" and post.call_count == 1 and not get.called


@pytest.mark.parametrize(
    "status,code", [(403, "authorization_denied"), (409, "backup_credential_conflict"), (503, "database_storage_required")]
)
def test_readback_http_failure_keeps_apply_trace(tmp_path: Path, status: int, code: str) -> None:
    arguments, _ = apply_args(tmp_path)
    failure = urllib.error.HTTPError(
        "https://example.invalid",
        status,
        "failure",
        {},
        io.BytesIO(json.dumps({"trace_id": "trace-readback", "error": {"code": code}}).encode()),
    )
    exit_code, payload, _, post = invoke(arguments, [failure], [plan(True)])
    assert exit_code != 0 and payload["status"] == "accepted_unverified"
    assert payload["summary"]["trace_id"] == "trace-plan"
    assert payload["summary"]["read_back_trace_id"] == "trace-readback"
    assert payload["summary"]["http_status"] == 202
    assert "error_code" not in payload["summary"]
    assert payload["summary"]["read_back_error_code"] == code
    assert payload["result"]["applied"] is True and post.call_count == 1


@pytest.mark.parametrize(
    "code", ["mutation_in_progress", "idempotency_key_reused", "mutation_reconciliation_required", "reconciliation_required"]
)
def test_unresolved_mutation_never_claims_refusal(tmp_path: Path, code: str) -> None:
    arguments, _ = apply_args(tmp_path)
    failure = urllib.error.HTTPError(
        "https://example.invalid",
        409,
        "failure",
        {},
        io.BytesIO(json.dumps({"trace_id": "trace-unresolved", "error": {"code": code}}).encode()),
    )
    exit_code, payload, get, post = invoke(arguments, [], [failure])
    assert exit_code != 0 and payload["status"] == "outcome_unknown"
    assert payload["summary"]["trace_id"] == "trace-unresolved"
    assert payload["request"]["idempotency_key_sha256"] and not get.called and post.call_count == 1


def test_unreadable_apply_retains_exception_diagnostics(tmp_path: Path) -> None:
    arguments, _ = apply_args(tmp_path)
    error = h.ServiceResponseError({"http_status": 202, "trace_id": "trace-unreadable"})
    exit_code, payload, _, post = invoke(arguments, [], [error])
    assert exit_code != 0 and payload["status"] == "outcome_unknown"
    assert payload["summary"]["http_status"] == 202 and payload["summary"]["trace_id"] == "trace-unreadable"
    assert post.call_count == 1


@pytest.mark.parametrize("command", [PREFLIGHT, BINDINGS + "-read", BINDINGS + "-dry-run"])
def test_transport_failure_is_unavailable(command: str) -> None:
    code, payload, _, post = invoke(argv(command), [TimeoutError()])
    assert code != 0 and payload["status"] == "unavailable" and not post.called


def test_preflight_has_its_supported_timeout_and_override() -> None:
    from launchplane_backup_credentials import PREFLIGHT_TIMEOUT_SECONDS

    assert h.parse_args(argv(PREFLIGHT)).timeout == PREFLIGHT_TIMEOUT_SECONDS
    assert h.parse_args(["--timeout", "90", *argv(PREFLIGHT)]).timeout == 90


def test_unrelated_binding_uses_standard_public_identifier_shape() -> None:
    observed = metadata()
    observed["result"]["credentials"].append(
        {"binding_id": "unrelated/id", "binding_key": "unrelated/key", "version_id": "version/other"}
    )
    code, _, _, _ = invoke(argv(BINDINGS + "-read"), [observed])
    assert code == 0


@pytest.mark.parametrize(
    "command,status",
    [(PREFLIGHT, 503), (BINDINGS + "-read", 401), (BINDINGS + "-read", 404)],
)
def test_read_http_status_uses_shared_mapping(command: str, status: int) -> None:
    error = urllib.error.HTTPError("https://example.invalid", status, "failure", {}, io.BytesIO(b"unparseable"))
    expected = h.summarize_http_error(operation=command, request={}, exc=error, provider_payload={})["status"]
    code, payload, _, post = invoke(argv(command), [error])
    assert code != 0 and payload["status"] == expected and not post.called
    assert payload["summary"]["recommendation"] == h.http_error_recommendation(expected)


def test_local_review_error_is_actionable(tmp_path: Path) -> None:
    arguments, evidence = apply_args(tmp_path)
    evidence["request"] = {}
    Path(arguments[arguments.index("--dry-run-evidence-file") + 1]).write_text(json.dumps(evidence))
    code, payload, _, post = invoke(arguments, [])
    assert code != 0 and not post.called
    assert payload["summary"]["error_code"] == "backup_review_evidence_mismatch"


@pytest.mark.parametrize("original_trace", ["trace-original", {"invalid": True}])
def test_replay_retains_request_and_completion_with_unsafe_original_trace(tmp_path: Path, original_trace: Any) -> None:
    arguments, _ = apply_args(tmp_path)
    response = plan(True)
    response.update(replayed=True, original_trace_id=original_trace)
    code, payload, _, post = invoke(arguments, [metadata(True)], [response])
    assert code == 0 and payload["summary"]["replayed"] is True
    if isinstance(original_trace, str):
        assert payload["summary"]["original_trace_id"] == original_trace
    assert payload["request"]["idempotency_key_sha256"] and post.call_count == 1
    code, payload, _, post = invoke(arguments, [TimeoutError()], [response])
    assert code != 0 and payload["status"] == "accepted_unverified"
    assert payload["summary"]["replayed"] is True
    assert payload["request"]["idempotency_key_sha256"] and post.call_count == 1




@pytest.mark.parametrize("name", ["desk-marker", "helpdesk-ssh"])
def test_ordinary_binding_reference_survives_final_projection(name: str) -> None:
    response = metadata()
    response["result"]["credentials"][0]["binding_id"] = name
    code, payload, _, post = invoke(argv(BINDINGS + "-read"), [response])
    assert code == 0 and not post.called
    assert name in {item["binding_id"] for item in payload["result"]["bindings"]}

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
