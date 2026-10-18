#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1"]
# ///
"""Native rebuild submission keeps one testing request and bounded receipts."""

import io
import json
import urllib.error
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from test_launchplane_helpers import contract, write_action as helper


def arguments(source="empty"):
    return [
        "testing-rebuild-submit", "--product", "example", "--copy-source", source,
        "--artifact-id", "current-build", "--source-git-ref", "a" * 40,
        "--idempotency-key", "original-rebuild",
    ]


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
            },
            "poll_url": "https://PRIVATE_SENTINEL.invalid/read",
        },
    }


def invoke(body=None, *, args=None, error=None):
    output = io.StringIO()
    with (
        patch.object(helper, "prepare_operator_settings", return_value={
            "service_url": "https://plane.example.invalid",
        }) as settings,
        patch.object(helper, "request_launchplane", return_value=body, side_effect=error) as post,
        patch.object(helper, "request_launchplane_read") as read,
        redirect_stdout(output),
    ):
        code = helper.main(arguments() if args is None else args)
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
])
def test_unaligned_receipt_is_unknown_without_a_retry(field, value):
    response = receipt()
    response["result"][field] = value
    code, output, post, read, _ = invoke(response)
    assert code != 0 and output["status"] == "outcome_unknown"
    post.assert_called_once()
    read.assert_not_called()
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_changed_request_in_response_does_not_acknowledge_the_wrong_build():
    response = receipt()
    response["result"]["request"]["source_git_ref"] = "b" * 40
    code, output, post, _, _ = invoke(response)
    assert code != 0 and output["status"] == "outcome_unknown"
    post.assert_called_once()


@pytest.mark.parametrize("error", [
    TimeoutError("PRIVATE_SENTINEL"),
    helper.LaunchplaneSafetyError("unsafe_redirect"),
    helper.ServiceResponseError({"http_status": 202}),
])
def test_uncertain_exchange_preserves_unknown_and_never_repeats(error):
    code, output, post, read, _ = invoke(error=error)
    assert code != 0 and output["status"] == "outcome_unknown"
    post.assert_called_once()
    read.assert_not_called()
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_malformed_accepted_response_preserves_bounded_diagnostics():
    diagnostics = {"http_status": 202, "trace_id": "launchplane_req_broken"}
    code, output, post, _, _ = invoke(error=helper.ServiceResponseError(diagnostics))
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
