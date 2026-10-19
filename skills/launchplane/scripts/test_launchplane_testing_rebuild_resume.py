# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1"]
# ///
"""Resume preserves the original operation and uses one configured service call."""

import io
import json
import urllib.error
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from test_launchplane_helpers import contract, write_action as helper


def arguments():
    return ["testing-rebuild-resume", "--product", "example", "--operation-id", "original-operation",
        "--idempotency-key", "original-key", "--expected-updated-at", "2026-10-18T00:00:00Z",
        "--expected-attempt", "2"]


def receipt():
    return {"trace_id": "resume-trace", "operation": {
        "operation_id": "original-operation", "product": "example", "instance": "testing",
        "status": "pending", "phase": "created", "attempt": 2, "updated_at": "2026-10-18T00:00:01Z",
    }}


def invoke(response=None, *, error=None, args=None):
    output = io.StringIO()
    with (
        patch.object(helper, "prepare_operator_settings", return_value={"service_url": "https://plane.example.invalid"}) as settings,
        patch.object(helper, "request_launchplane", return_value=response, side_effect=error) as post,
        patch.object(helper, "request_launchplane_read") as read,
        redirect_stdout(output),
    ):
        code = helper.main(arguments() if args is None else args)
    return code, json.loads(output.getvalue()), post, read, settings


def test_resume_sends_original_key_and_observed_revision_once():
    response = receipt()
    code, output, post, read, _ = invoke(response)
    assert code == 0
    post.assert_called_once()
    sent = post.call_args.kwargs
    assert sent["path"] == contract.helper_command_path("testing-rebuild-resume").format(
        product=response["operation"]["product"], operation_id=response["operation"]["operation_id"])
    assert sent["idempotency_key"] == "original-key"
    assert sent["body"] == {"expected_updated_at": "2026-10-18T00:00:00Z", "expected_attempt": 2}
    assert output["result"]["operation"] == response["operation"]
    assert output["summary"]["trace_id"] == response["trace_id"]
    assert "original-key" not in json.dumps(output)
    read.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("--product", "../prod"), ("--operation-id", "../other"), ("--idempotency-key", ""),
    ("--expected-updated-at", "not-a-date"), ("--expected-attempt", "-1"),
])
def test_invalid_original_reference_refuses_before_loading_identity(field, value):
    args = arguments()
    args[args.index(field) + 1] = value
    code, _, post, read, settings = invoke(receipt(), args=args)
    assert code != 0
    settings.assert_not_called()
    post.assert_not_called()
    read.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("product", "other"), ("operation_id", "foreign-operation"), ("instance", "prod"),
    ("attempt", -1), ("attempt", True), ("updated_at", "PRIVATE_SENTINEL"),
    ("status", None), ("phase", ""),
    ("credentials", {"password": "PRIVATE_SENTINEL"}),
])
def test_foreign_or_unbounded_receipt_is_unknown_without_repeating(field, value):
    response = receipt()
    response["operation"][field] = value
    code, output, post, read, _ = invoke(response)
    assert code != 0 and output["status"] == "outcome_unknown"
    assert "PRIVATE_SENTINEL" not in json.dumps(output)
    post.assert_called_once()
    read.assert_not_called()


@pytest.mark.parametrize("error", [TimeoutError("PRIVATE_SENTINEL"), helper.ServiceResponseError({"trace_id": "lost-trace"})])
def test_lost_resume_reply_keeps_original_operation_without_automatic_retry(error):
    code, output, post, read, _ = invoke(error=error)
    assert code != 0 and output["status"] == "outcome_unknown"
    assert output["summary"]["operation_id"] == "original-operation"
    assert "PRIVATE_SENTINEL" not in json.dumps(output)
    post.assert_called_once()
    read.assert_not_called()


@pytest.mark.parametrize("status,error_code", [(403, "authorization_denied"), (409, "testing_rebuild_resume_conflict"), (404, "not_found")])
def test_refusal_retains_code_trace_and_never_uses_another_route(status, error_code):
    error = urllib.error.HTTPError("https://plane.example.invalid", status, "PRIVATE_SENTINEL", {},
        io.BytesIO(json.dumps({"trace_id": "refusal-trace", "error": {"code": error_code}}).encode()))
    code, output, post, read, _ = invoke(error=error)
    assert code != 0
    assert output["summary"]["error_code"] == error_code
    assert output["summary"]["trace_id"] == "refusal-trace"
    assert "PRIVATE_SENTINEL" not in json.dumps(output)
    if status == 403:
        assert output["status"] == "denied"
    post.assert_called_once()
    read.assert_not_called()


@pytest.mark.parametrize("extra", [["--instance", "prod"], ["--identity", "other"], ["--payload-file", "/private/arbitrary.json"]])
def test_resume_accepts_no_replacement_or_identity_override(extra):
    with patch.object(helper, "request_launchplane") as post, pytest.raises(SystemExit):
        helper.main([*arguments(), *extra])
    post.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
