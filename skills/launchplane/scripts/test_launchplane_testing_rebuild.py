#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1"]
# ///
"""Native rebuild submission keeps one testing request and bounded receipts."""

import http.client
import io
import json
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from test_launchplane_helpers import contract, write_action as helper
import launchplane_testing_rebuild as rebuild

SERVICE_URL = "https://plane.example.invalid"


def arguments(source="empty", command=rebuild.COMMAND):
    return [
        command, "--product", "example", "--copy-source", source,
        "--artifact-id", "current-build", "--source-git-ref", "a" * 40,
        "--reason", "Rebuild the disposable testing copy",
    ] + (["--idempotency-key", "original-rebuild"] if command == rebuild.COMMAND else [])


def receipt(source="empty"):
    return {
        "status": "accepted", "trace_id": "launchplane_req_rebuild",
        "records": {"odoo_stable_target_replacement_operation_id": "original-operation"},
        "result": {
            "operation_id": "original-operation", "product": "example",
            "context": "example-testing", "instance": "testing", "status": "queued",
            "phase": "created", "idempotency_key": "original-rebuild",
            "authorization": {"private": "PRIVATE_SENTINEL"},
            "request": {
                "product": "example", "instance": "testing",
                "strategy": "replace-and-cutover", "data_source_mode": {
                    "empty": "empty", "production": "production_restore", "upstream": "upstream_restore",
                }[source],
                "artifact_id": "current-build", "source_git_ref": "a" * 40,
                "allow_empty_data": source == "empty", "verify_health": True,
                "reason": "Rebuild the disposable testing copy",
            },
            "poll_url": "https://PRIVATE_SENTINEL.invalid/read",
        },
    }


def plan_receipt(source="empty"):
    original = receipt(source)["result"]["request"]
    return {"status": "accepted", "trace_id": "launchplane_req_plan", "records": {}, "result": {
        **{key: original[key] for key in ("product", "instance", "artifact_id", "source_git_ref", "data_source_mode")},
        "status": "pass", "candidate_verification": "pending", "context": "example-testing",
        "base_url": "https://testing.example.invalid", "observed_at": "2026-10-17T00:00:00Z",
        "authorization": {"private": "PRIVATE_SENTINEL"},
    }}


def review_evidence(source="empty"):
    request, body = rebuild.intent(helper, helper.parse_args(arguments(source)))
    return {"status": "ok", "operation": rebuild.PLAN_COMMAND, "request": request,
        "result": rebuild.project_plan(helper, plan_receipt(source), body, service_url=SERVICE_URL)}


def invoke(body=None, *, args=None, error=None, reviewed=True, change_evidence=None, service_url=SERVICE_URL):
    output = io.StringIO()
    with (
        TemporaryDirectory() as directory,
        patch.object(helper, "active_repo_root", return_value=Path(directory) / "repo"),
        patch.object(helper, "prepare_operator_settings", return_value={
            "service_url": service_url,
        }) as settings,
        patch.object(helper, "request_launchplane", return_value=body, side_effect=error) as post,
        patch.object(helper, "request_launchplane_read") as read,
        redirect_stdout(output),
    ):
        (Path(directory) / "repo").mkdir()
        args = list(arguments() if args is None else args)
        if reviewed and args[0] == rebuild.COMMAND:
            evidence = review_evidence(args[args.index("--copy-source") + 1])
            if change_evidence:
                change_evidence(evidence)
            path = Path(directory) / "review.json"
            path.write_text(json.dumps(evidence))
            args += ["--reviewed-dry-run", "--expected-plan-digest", evidence["result"]["plan_sha256"],
                "--dry-run-evidence-file", str(path)]
        code = helper.main(args)
    return code, json.loads(output.getvalue()), post, read, settings


@pytest.mark.parametrize("source", ["empty", "production", "upstream"])
def test_submit_uses_the_service_testing_action_and_original_key(source):
    response = receipt(source)
    code, output, post, read, _ = invoke(response, args=arguments(source))
    assert code == 0
    post.assert_called_once()
    sent = post.call_args.kwargs
    assert sent["path"] == contract.helper_command_path("testing-rebuild-submit")
    assert sent["body"]["replacement"] == response["result"]["request"]
    assert sent["body"]["product"] == response["result"]["product"]
    assert sent["idempotency_key"] == response["result"]["idempotency_key"]
    assert output["result"]["operation"]["operation_id"] == response["result"]["operation_id"]
    assert output["summary"]["trace_id"] == response["trace_id"]
    assert "PRIVATE_SENTINEL" not in json.dumps(output)
    assert "authorization" not in output["result"]["operation"]
    read.assert_not_called()


@pytest.mark.parametrize("source", ["empty", "production", "upstream"])
def test_native_dry_run_reads_preflight_without_queueing_or_an_operation_key(source):
    response = plan_receipt(source)
    code, output, post, read, _ = invoke(response, args=arguments(source, rebuild.PLAN_COMMAND))
    assert code == 0
    post.assert_called_once()
    sent = post.call_args.kwargs
    assert sent["path"] == contract.helper_command_path(rebuild.PLAN_COMMAND)
    assert "idempotency_key" not in sent
    assert "verify_health" not in sent["body"]["replacement"]
    assert output["result"]["artifact_id"] == response["result"]["artifact_id"]
    assert output["result"]["candidate_verification"] == response["result"]["candidate_verification"]
    assert output["summary"]["trace_id"] == response["trace_id"]
    assert "operation" not in output["result"]
    assert "reason" not in output["request"]
    assert "PRIVATE_SENTINEL" not in json.dumps(output)
    read.assert_not_called()


def test_submit_requires_reviewed_evidence_before_configuration_or_transport():
    code, _, post, read, settings = invoke(receipt(), reviewed=False)
    assert code != 0
    settings.assert_not_called()
    post.assert_not_called()
    read.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("--product", "other"), ("--artifact-id", "other-build"),
    ("--source-git-ref", "b" * 40), ("--reason", "A different reason"),
])
def test_changed_intent_cannot_reuse_reviewed_evidence(field, value):
    args = arguments()
    args[args.index(field) + 1] = value
    code, _, post, _, settings = invoke(receipt(), args=args)
    assert code != 0
    settings.assert_not_called()
    post.assert_not_called()


def test_dry_run_from_another_service_cannot_authorize_submit():
    code, _, post, read, settings = invoke(receipt(), service_url="https://other.example.invalid")
    assert code != 0
    settings.assert_called_once()
    post.assert_not_called()
    read.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("candidate_verification", "verified"), ("product", "other"),
    ("data_source_mode", "existing"), ("instance", "prod"),
])
def test_dry_run_does_not_claim_a_completed_or_different_copy(field, value):
    response = helper._ServiceResponse(plan_receipt(), 202)
    response["result"][field] = value
    code, output, post, read, _ = invoke(response, args=arguments(command=rebuild.PLAN_COMMAND))
    assert code != 0
    assert output["status"] != "ok"
    assert output["summary"]["trace_id"] == response["trace_id"]
    assert output["summary"]["http_status"] == response.http_status
    post.assert_called_once()
    read.assert_not_called()
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_a_dry_run_timeout_is_a_read_failure_without_an_unknown_write():
    code, output, post, read, _ = invoke(error=TimeoutError("PRIVATE_SENTINEL"),
        args=arguments(command=rebuild.PLAN_COMMAND))
    assert code != 0
    assert output["status"] == "unavailable"
    post.assert_called_once()
    read.assert_not_called()
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize("reason", ["", " " * 4])
def test_reason_is_validated_before_configuration_or_transport(reason):
    args = arguments()
    args[args.index("--reason") + 1] = reason
    code, _, post, _, settings = invoke(receipt(), args=args)
    assert code != 0
    settings.assert_not_called()
    post.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("--product", "../prod"), ("--artifact-id", ""),
    ("--source-git-ref", "main"), ("--idempotency-key", ""),
])
def test_invalid_intent_refuses_before_configuration_or_transport(field, value):
    args = arguments()
    args[args.index(field) + 1] = value
    code, _, post, read, settings = invoke(receipt(), args=args)
    assert code != 0
    settings.assert_not_called()
    post.assert_not_called()
    read.assert_not_called()


@pytest.mark.parametrize("extra", [
    ["--instance", "prod"], ["--target-id", "production"],
    ["--no-verify-health"], ["--payload-file", "/private/arbitrary.json"],
])
def test_caller_cannot_redirect_to_production_or_bypass_health(extra):
    with patch.object(helper, "request_launchplane") as post, pytest.raises(SystemExit):
        helper.main([*arguments(), *extra])
    post.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("instance", "prod"), ("product", "foreign"),
    ("operation_id", ""), ("idempotency_key", "another-request"),
    ("request.source_git_ref", "b" * 40), ("request.reason", "Another request"),
])
def test_unaligned_receipt_is_unknown_without_a_retry(field, value):
    response = receipt()
    if field.startswith("request."):
        response["result"]["request"][field.removeprefix("request.")] = value
    else:
        response["result"][field] = value
    code, output, post, read, _ = invoke(response)
    assert code != 0 and output["status"] == "outcome_unknown"
    post.assert_called_once()
    read.assert_not_called()
    assert "PRIVATE_SENTINEL" not in json.dumps(output)
    assert "operation_id" not in output["summary"]


def test_invalid_trace_does_not_discard_an_aligned_accepted_operation():
    response = receipt()
    response["trace_id"] = "https://PRIVATE_SENTINEL.invalid/private"
    code, output, post, _, _ = invoke(response)
    assert code == 0
    assert output["result"]["operation"]["operation_id"] == response["result"]["operation_id"]
    assert "PRIVATE_SENTINEL" not in json.dumps(output)
    post.assert_called_once()


def test_aligned_receipt_keeps_its_operation_reference_when_projection_fails():
    response = receipt()
    with patch.object(helper, "_project_target_replacement_operation",
            side_effect=helper.LaunchplaneSafetyError("invalid_response")):
        code, output, post, _, _ = invoke(response)
    assert code != 0 and output["status"] == "outcome_unknown"
    assert output["summary"]["operation_id"] == response["result"]["operation_id"]
    post.assert_called_once()


def test_active_operation_conflict_exposes_only_the_pollable_blocker():
    operation = receipt()["result"]
    error = urllib.error.HTTPError(SERVICE_URL, 409, "Conflict", {}, io.BytesIO(json.dumps({
        "trace_id": "launchplane_req_busy",
        "error": {"code": "odoo_stable_target_replacement_operation_active"},
        "operation": operation,
    }).encode()))
    code, output, post, read, _ = invoke(error=error)
    assert code != 0
    assert output["result"]["operation"] == {
        key: operation[key] for key in ("operation_id", "status", "phase")
    }
    assert output["summary"]["trace_id"] == "launchplane_req_busy"
    assert "PRIVATE_SENTINEL" not in json.dumps(output)
    post.assert_called_once()
    read.assert_not_called()


@pytest.mark.parametrize("command", [rebuild.PLAN_COMMAND, rebuild.COMMAND])
@pytest.mark.parametrize(("error_code", "expected"), [
    ("testing_unavailable_ingress_unverified", "refused"),
    ("mutation_reconciliation_required", "reconciliation_required"),
    ("idempotency_key_reused", "conflict"),
])
def test_native_rebuild_refusals_preserve_state_without_another_dispatch(command: str, error_code: str, expected: str) -> None:
    message = next(iter(helper.SAFE_REFUSAL_MESSAGES["testing_unavailable_ingress_unverified"]))
    error = urllib.error.HTTPError(SERVICE_URL, 409, "Conflict", {}, io.BytesIO(json.dumps({
        "trace_id": "launchplane_req_refusal", "error": {"code": error_code, "message": message},
    }).encode()))
    code, output, post, read, _ = invoke(error=error, args=arguments(command=command))
    assert code != 0
    assert output["status"] == expected
    assert output["summary"]["error_code"] == error_code
    assert output["summary"]["trace_id"] == "launchplane_req_refusal"
    if error_code == "testing_unavailable_ingress_unverified":
        assert output["warnings"][0]["message"] == message
    post.assert_called_once()
    read.assert_not_called()


def test_uppercase_source_is_rejected_before_configuration():
    args = arguments()
    args[args.index("--source-git-ref") + 1] = "A" * 40
    code, _, post, _, settings = invoke(receipt(), args=args)
    assert code != 0
    settings.assert_not_called()
    post.assert_not_called()


def test_replay_diagnostics_distinguish_original_acceptance():
    response = {**receipt(), "replayed": True, "original_trace_id": "launchplane_req_original"}
    code, output, post, _, _ = invoke(response)
    assert code == 0
    assert output["summary"]["replayed"] is True
    assert output["summary"]["original_trace_id"] == response["original_trace_id"]
    post.assert_called_once()


@pytest.mark.parametrize("error", [
    TimeoutError("PRIVATE_SENTINEL"),
    helper.LaunchplaneSafetyError("unsafe_redirect"),
])
def test_uncertain_exchange_preserves_unknown_and_never_repeats(error):
    code, output, post, read, _ = invoke(error=error)
    assert code != 0 and output["status"] == "outcome_unknown"
    post.assert_called_once()
    read.assert_not_called()
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize("partial_body", [False, True])
def test_malformed_accepted_response_preserves_bounded_diagnostics(partial_body):
    diagnostics = {"http_status": 202}
    if partial_body:
        error = http.client.IncompleteRead(b"PRIVATE_SENTINEL")
        setattr(error, "http_status", diagnostics["http_status"])
    else:
        diagnostics["trace_id"] = "launchplane_req_broken"
        error = helper.ServiceResponseError(diagnostics)
    code, output, post, _, _ = invoke(error=error)
    assert code != 0 and output["status"] == "outcome_unknown"
    assert all(output["summary"].get(key) == value for key, value in diagnostics.items())
    post.assert_called_once()


def test_authorization_denial_retains_the_exact_code_trace_and_stops():
    error = urllib.error.HTTPError(
        "https://plane.example.invalid", 403, "Forbidden", {},
        io.BytesIO(json.dumps({
            "status": "error", "trace_id": "launchplane_req_denied",
            "error": {"code": "authorization_denied", "message": "PRIVATE_SENTINEL"},
        }).encode()),
    )
    code, output, post, read, _ = invoke(error=error)
    assert code != 0 and output["status"] == "denied"
    assert output["summary"]["error_code"] == "authorization_denied"
    assert output["summary"]["trace_id"] == "launchplane_req_denied"
    post.assert_called_once()
    read.assert_not_called()
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_missing_service_capability_is_reported_without_an_alternative_route():
    error = urllib.error.HTTPError(
        "https://plane.example.invalid", 404, "Not Found", {},
        io.BytesIO(json.dumps({
            "status": "error", "trace_id": "launchplane_req_missing",
            "error": {"code": "not_found", "message": "PRIVATE_SENTINEL"},
        }).encode()),
    )
    code, output, post, read, _ = invoke(error=error)
    assert code != 0 and output["status"] != "accepted"
    post.assert_called_once()
    read.assert_not_called()
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize("status", [302, 408, 502, 503])
def test_interrupted_http_exchange_does_not_claim_a_definitive_outcome(status):
    error = urllib.error.HTTPError(
        "https://plane.example.invalid", status, "PRIVATE_SENTINEL", {}, io.BytesIO(b""),
    )
    code, output, post, read, _ = invoke(error=error)
    assert code != 0 and output["status"] == "outcome_unknown"
    post.assert_called_once()
    read.assert_not_called()
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_plan_redirect_retains_its_safety_classification():
    error = helper.LaunchplaneSafetyError("unsafe_redirect")
    code, output, post, read, _ = invoke(args=arguments(command=rebuild.PLAN_COMMAND), error=error)
    assert code != 0 and output["status"] == "unavailable"
    assert output["warnings"][0]["code"] == error.code
    post.assert_called_once()
    read.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))


def test_rebuild_keeps_validated_identifier_names_through_plan() -> None:
    args = helper.parse_args(arguments(command=rebuild.PLAN_COMMAND))
    args.product, args.artifact_id = "kiosk-shop", "desk-build"
    request, body = rebuild.intent(helper, args)
    reply = plan_receipt()
    reply["result"].update(product=args.product, artifact_id=args.artifact_id)
    projected = rebuild.project_plan(helper, reply, body, service_url=SERVICE_URL)
    assert (request["product"], projected["artifact_id"]) == (args.product, args.artifact_id)
