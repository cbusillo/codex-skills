#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1"]
# ///
"""Private ingress callers bind review, recovery and independent observations."""

import http.client
import io
import json
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from test_launchplane_helpers import write_action as h
import launchplane_testing_private_ingress as ingress

SERVICE = "https://plane.example.invalid"
PLAN = "a" * 64


def private_request(operation="create"):
    return {"product": "example", "context": "example-testing", "instance": "testing",
        "endpoint_key": "private-health", "edge_endpoint_key": "testing-edge", "operation": operation,
        "access_list_id": 7, "certificate_id": 8, "reason": "PRIVATE_SENTINEL"}


def receipt(mode, operation="create", **updates):
    # Producer TestingPrivateIngressAdapter._result (Launchplane PR #3991).
    # Keep this response independent of the consumer's field allowlist.
    return {"status": "accepted", "trace_id": "launchplane_req_private_ingress", "records": {},
        "result": {"product": "example", "context": "example-testing", "instance": "testing",
            "endpoint_key": "private-health", "operation": operation,
            "plan_sha256": PLAN, "applied": mode != "dry-run", "deployment_performed": False,
            "transport_verified": False, **updates}}


def invoke(mode="dry-run", *, body=None, responses=None, edit_evidence=None, service=SERVICE, operation="create", reviewed=True):
    output = io.StringIO()
    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "repo").mkdir()
        payload = root / "request.json"
        evidence_path = root / "review.json"
        payload.write_text(json.dumps(private_request(operation)))
        with (patch.object(h, "active_repo_root", return_value=root / "repo"),
            patch.object(h, "prepare_operator_settings", return_value={"service_url": SERVICE}) as settings,
            patch.object(h, "request_launchplane", return_value=receipt("dry-run", operation)) as post,
            redirect_stdout(output)):
            argv = [ingress.PREFIX + "dry-run", "--payload-file", str(payload)]
            code = h.main(argv)
            assert code == 0, output.getvalue()
            evidence = json.loads(output.getvalue())
            if edit_evidence:
                edit_evidence(evidence)
            evidence_path.write_text(json.dumps(evidence))
            payload.write_text(json.dumps(private_request(operation) if body is None else body))
            output.seek(0)
            output.truncate()
            post.reset_mock()
            settings.reset_mock()
            settings.return_value = {"service_url": service}
            post.side_effect = responses if responses is not None else [receipt(mode, operation), receipt("readback", operation)]
            argv[0] = ingress.PREFIX + mode
            if mode != "dry-run":
                argv += ["--expected-plan-digest", PLAN, "--dry-run-evidence-file", str(evidence_path)]
                if reviewed:
                    argv += ["--reviewed-dry-run"]
            if mode == "apply":
                argv += ["--idempotency-key", "original-key"]
            code = h.main(argv)
            return code, json.loads(output.getvalue()), list(post.call_args_list), settings.call_count


@pytest.mark.parametrize("operation", ["create", "disable"])
@pytest.mark.parametrize("mode", ["dry-run", "apply", "readback"])
def test_service_call_and_readback_bind_exact_private_intent(operation, mode):
    code, output, calls, _ = invoke(mode, operation=operation)
    assert code == 0
    assert [call.kwargs["body"]["mode"] for call in calls] == (["apply", "readback"] if mode == "apply" else [mode])
    for call in calls:
        body = call.kwargs["body"]
        assert call.kwargs["path"] == h.helper_command_path(ingress.PREFIX + mode).format(product="example")
        assert {**{key: value for key, value in body.items() if key not in {"mode", "reviewed_plan_sha256"}}, "product": "example"} == private_request(operation)
        assert call.kwargs["idempotency_key"] == ("original-key" if body["mode"] == "apply" else "")
        if mode != "dry-run":
            assert body["reviewed_plan_sha256"] == PLAN
    assert output["result"]["transport_verified"] is False
    assert output["result"]["deployment_performed"] is False
    if mode == "apply":
        assert output["result"]["read_back_matches"] is True
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize("field,value", [("product", "other"), ("context", "other"), ("endpoint_key", "other"),
    ("edge_endpoint_key", "other"), ("operation", "disable"), ("access_list_id", 9), ("certificate_id", 10), ("reason", "changed")])
def test_changed_private_intent_cannot_reuse_review(field, value):
    body = private_request()
    body[field] = value
    code, _, calls, settings = invoke("apply", body=body)
    assert code != 0 and calls == [] and settings == 0


@pytest.mark.parametrize("field,value", [("instance", "prod"), ("product", "../escape"), ("mode", "apply"),
    ("reviewed_plan_sha256", PLAN), ("url", "https://private.example.invalid"), ("certificate_id", True),
    ("access_list_id", 0), ("reason", " ")])
def test_invalid_or_provider_input_refuses_before_configuration(field, value):
    body = private_request()
    body[field] = value
    code, _, calls, settings = invoke("dry-run", body=body)
    assert code != 0 and calls == [] and settings == 0


@pytest.mark.parametrize("change", [
    lambda e: e.update(operation="other-dry-run"),
    lambda e: e.update(status="denied"),
    lambda e: e["result"].update(applied=True),
    lambda e: e["result"].update(mode="apply"),
    lambda e: e["result"].update(edge_endpoint_key="other"),
    lambda e: e["result"].update(plan_sha256="b" * 64),
    lambda e: e["result"].update(review_binding_sha256="b" * 64),
    lambda e: e["request"].update(payload_digest="b" * 64),
])
def test_ineligible_review_never_reaches_transport(change):
    code, _, calls, settings = invoke("apply", edit_evidence=change)
    assert code != 0 and calls == [] and settings == 0


def test_review_acknowledgment_and_service_binding_are_required():
    code, _, calls, settings = invoke("apply", reviewed=False)
    assert code != 0 and calls == [] and settings == 0
    code, _, calls, _ = invoke("apply", service="https://different.example.invalid")
    assert code != 0 and calls == []


@pytest.mark.parametrize("updates", [{"context": "foreign"}, {"endpoint_key": "foreign"}, {"operation": "disable"},
    {"plan_sha256": "b" * 64}, {"applied": False}, {"transport_verified": True},
    {"deployment_performed": True}, {"url": "https://PRIVATE_SENTINEL.invalid"}])
def test_unaligned_apply_cannot_be_reported_verified(updates):
    code, output, calls, _ = invoke("apply", responses=[receipt("apply", **updates)])
    assert code != 0 and output["status"] == "outcome_unknown" and len(calls) == 1
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize("failure", [TimeoutError("PRIVATE_SENTINEL"), http.client.IncompleteRead(b"PRIVATE_SENTINEL")])
def test_interruption_keeps_unknown_or_accepted_apply_without_retry(failure):
    class PartialResponse:
        status = 202

        def read(self):
            raise failure

    try:
        h._read_service_response(PartialResponse())
    except (OSError, http.client.HTTPException) as error:
        failure = error
    code, output, calls, _ = invoke("apply", responses=[failure])
    assert code != 0 and output["status"] == "outcome_unknown" and len(calls) == 1
    assert output["summary"]["http_status"] == 202
    code, output, calls, _ = invoke("apply", responses=[receipt("apply"), failure])
    assert code != 0 and output["status"] == "accepted_unverified" and len(calls) == 2
    assert output["result"]["applied"] is True
    assert output["summary"]["read_back_http_status"] == 202
    assert output["summary"]["trace_id"] == receipt("apply")["trace_id"]
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_readback_checks_its_own_scope_and_digest():
    readback = receipt("readback", context="foreign")
    readback["trace_id"] = "launchplane_req_readback_mismatch"
    code, output, calls, _ = invoke("apply", responses=[receipt("apply"), readback])
    assert code != 0 and output["status"] == "accepted_unverified" and len(calls) == 2
    assert output["result"].get("read_back_matches") is not True
    assert output["summary"]["read_back_trace_id"] == readback["trace_id"]
    assert output["summary"]["trace_id"] == receipt("apply")["trace_id"]


def test_original_key_replay_is_not_replanned_and_readback_is_independent():
    response = receipt("apply")
    response.update(replayed=True, original_trace_id="launchplane_req_original")
    code, output, calls, _ = invoke("apply", responses=[response, receipt("readback")])
    assert code == 0 and len(calls) == 2
    assert output["summary"]["replayed"] is True
    assert output["summary"]["original_trace_id"] == response["original_trace_id"]


@pytest.mark.parametrize("field", ["readback_trace", "original_trace"])
def test_invalid_optional_trace_does_not_invalidate_verified_effects(field):
    response, readback = receipt("apply"), receipt("readback")
    response["replayed"] = True
    if field == "original_trace":
        response["original_trace_id"] = {"private": "PRIVATE_SENTINEL"}
    else:
        readback["trace_id"] = {"private": "PRIVATE_SENTINEL"}
    code, output, calls, _ = invoke("apply", responses=[response, readback])
    assert code == 0 and output["status"] == "accepted" and len(calls) == 2
    assert output["result"]["read_back_matches"] is True
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize("mode", ["dry-run", "readback"])
@pytest.mark.parametrize("malformed_json", [False, True])
def test_failed_observation_keeps_safe_service_diagnostics(mode, malformed_json):
    response = h._ServiceResponse(receipt(mode, context="foreign"), 202)
    failure = h.ServiceResponseError(h.service_response_diagnostics(response)) if malformed_json else response
    code, output, calls, _ = invoke(mode, responses=[failure])
    assert code != 0 and len(calls) == 1
    assert output["summary"]["trace_id"] == response["trace_id"]
    assert output["summary"]["http_status"] == response.http_status


def test_original_key_can_end_with_explicit_replan_after_absence_proof():
    code, output, calls, _ = invoke("apply", responses=[receipt("apply", applied=False,
        replan_required=True, previous_effects_absent=True)])
    assert code != 0 and output["status"] == "replan_required" and len(calls) == 1
    assert output["result"]["previous_effects_absent"] is True


@pytest.mark.parametrize("updates", [{"replan_required": True, "applied": False},
    {"previous_effects_absent": True, "applied": False},
    {"replan_required": True, "previous_effects_absent": True}])
def test_incomplete_or_contradictory_replan_never_permits_a_new_key(updates):
    code, output, calls, _ = invoke("apply", responses=[receipt("apply", **updates)])
    assert code != 0 and output["status"] == "outcome_unknown" and len(calls) == 1


@pytest.mark.parametrize("mode", ["dry-run", "readback"])
def test_observation_failure_is_not_an_unknown_write(mode):
    code, output, calls, _ = invoke(mode, responses=[TimeoutError("PRIVATE_SENTINEL")])
    assert code != 0 and len(calls) == 1
    assert output["status"] not in {"accepted", "accepted_unverified", "outcome_unknown"}
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize("status", [403, 409, 503, 408])
@pytest.mark.parametrize("phase", ["apply", "readback"])
@pytest.mark.parametrize("service_diagnostics", [True, False])
def test_http_errors_preserve_trace_and_never_switch_route_or_retry(status, phase, service_diagnostics):
    response = {"trace_id": "launchplane_req_refusal", "error": {
        "code": "authorization_denied" if status == 403 else "reconcile_required", "message": "PRIVATE_SENTINEL",
    }} if service_diagnostics else {}
    failure = urllib.error.HTTPError(SERVICE, status, "PRIVATE_SENTINEL", {}, io.BytesIO(json.dumps(response).encode()))
    code, output, calls, _ = invoke("apply", responses=([receipt("apply")] if phase == "readback" else []) + [failure])
    if not service_diagnostics and (phase == "readback" or status in {503, 408}):
        prefix = "read_back_" if phase == "readback" else ""
        assert output["summary"][prefix + "http_status"] == status
        assert prefix + "error_code" not in output["summary"]
        assert prefix + "trace_id" not in output["summary"]
        assert output["status"] == ("accepted_unverified" if phase == "readback" else "outcome_unknown")
        return
    if not service_diagnostics:
        assert output["status"] == h._status_for_http_error(status, {})
        return
    assert code != 0 and len(calls) == (2 if phase == "readback" else 1)
    if phase == "readback":
        assert output["status"] == "accepted_unverified"
        assert output["summary"]["trace_id"] == receipt("apply")["trace_id"]
        assert output["summary"]["read_back_trace_id"] == "launchplane_req_refusal"
        assert output["summary"]["read_back_http_status"] == status
        assert output["summary"]["read_back_error_code"] == ("authorization_denied" if status == 403 else "reconcile_required")
        assert "PRIVATE_SENTINEL" not in json.dumps(output)
        return
    assert output["summary"]["trace_id"] == "launchplane_req_refusal"
    assert output["summary"]["error_code"] == ("authorization_denied" if status == 403 else "reconcile_required")
    if status in {503, 408}:
        assert output["status"] == "outcome_unknown"
    else:
        assert output["status"] == h._status_for_http_error(status, {
            "error": {"code": output["summary"]["error_code"]}})
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize("mode", ["dry-run", "readback"])
def test_observation_redirect_retains_its_safety_classification(mode):
    error = h.LaunchplaneSafetyError("unsafe_redirect")
    code, output, calls, _ = invoke(mode, responses=[error])
    assert code != 0 and len(calls) == 1
    assert output["status"] == "unavailable"
    assert output["warnings"][0]["code"] == error.code


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
