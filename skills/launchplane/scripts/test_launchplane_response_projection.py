#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1"]
# ///
"""Exercise preview and saved-review projection through fake HTTP responses."""

import http.client
import io
import json
import urllib.error
from contextlib import redirect_stdout
from email.message import Message
from unittest.mock import patch

import pytest

from test_launchplane_helpers import owner_review, write_action as helper


SETTINGS = {"service_url": "https://service.example.invalid", "token": "PRIVATE_SENTINEL"}
TRACE = "launchplane_req_projection_fixture"
SERVICE_CODE = "product_review_unavailable"
PREVIEW_ARGS = [
    "preview-feedback-remediation", "--mode", "dry-run", "--product", "example-site",
    "--context", "example-preview", "--repository", "example/site", "--pull-request-url",
    "https://github.com/example/site/pull/42", "--terminal-status", "destroyed",
    "--reason", "Check existing absent feedback", "--related-issue", "example/service#1",
    "--idempotency-key", "example-projection-dry-run",
]


class Response(io.BytesIO):
    def __init__(self, body, status):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.status = status


def preview_response(login="example-delivery[bot]"):
    # Producer's absent-comment App actor shape, with private observation fields.
    return {"status": "accepted", "trace_id": TRACE, "records": {}, "result": {
        "schema_version": 1, "remediation_id": "example-remediation", "mode": "dry-run",
        "planned_action": "none", "outcome": "planned", "companion_feedback_id": "",
        "observation": {"state": "absent", "comment_id": 0, "comment_url": "",
                        "comment_author_id": 0, "comment_author_login": "",
                        "token_actor_id": 123, "token_actor_login": login,
                        "digest_sha256": "a" * 64, "marker": "FOREIGN_MARKER",
                        "excerpt": "PRIVATE_SENTINEL"},
    }}


def invoke(path, body, status=200, *, http_error=False, interrupted=False, mode="dry-run",
           transport_error=None):
    module = helper if path == "preview" else owner_review.operator
    response = Response(body, status)
    failure = transport_error or (urllib.error.HTTPError(
        SETTINGS["service_url"], status, "PRIVATE_SENTINEL", Message(), response
    ) if http_error else None)
    preview_args = list(PREVIEW_ARGS)
    preview_args[preview_args.index("--mode") + 1] = mode
    if mode == "apply":
        preview_args.append("--reviewed-dry-run")
    stdout = io.StringIO()
    with patch.object(module, "safe_urlopen", side_effect=failure, return_value=response) as transport, \
         patch.object(helper, "prepare_operator_settings", return_value=SETTINGS), \
         patch.object(owner_review, "resolve_settings", return_value=SETTINGS), \
         redirect_stdout(stdout):
        if interrupted:
            response.read = lambda: (_ for _ in ()).throw(OSError("PRIVATE_SENTINEL"))
        code = helper.main(preview_args) if path == "preview" else owner_review.main([
            "--repo", "example/site", "--pr", "42"])
    rendered = stdout.getvalue()
    assert "PRIVATE_SENTINEL" not in rendered
    assert "FOREIGN_MARKER" not in rendered
    assert SETTINGS["service_url"] not in rendered
    assert transport.call_count == 1
    assert transport.call_args.args[0].method == ("POST" if path == "preview" else "GET")
    return code, json.loads(rendered)


def diagnostics(path, payload):
    return payload["summary"] if path == "preview" else payload


@pytest.mark.parametrize("state", ["absent", "present"])
@pytest.mark.parametrize("mode", ["apply", "dry-run"])
def test_preview_projects_app_actor_and_comment_author_without_private_observation(state, mode):
    response = preview_response()
    response["result"]["mode"] = mode
    response["result"]["observation"]["state"] = state
    author = "example-commenter[bot]" if state == "present" else ""
    response["result"]["observation"]["comment_author_login"] = author
    code, payload = invoke("preview", response, mode=mode)
    assert code == 0
    observed = payload["result"]["observation"]
    assert observed["state"] == state
    assert observed["github_actor_login"] == response["result"]["observation"]["token_actor_login"]
    assert observed["comment_author_login"] == (author or "unknown")
    assert payload["summary"]["trace_id"] == response["trace_id"]


@pytest.mark.parametrize("login", ["example[bot]extra", "[bot]", "example[admin]",
                                  "example/site[bot]", "example ghp_planted[bot]", "ghp_planted[bot]",
                                  "sk-planted[bot]", {"token": "PRIVATE_SENTINEL"}])
def test_preview_invalid_actor_keeps_failure_trace_and_status(login):
    code, payload = invoke("preview", preview_response(login))
    assert code == 1
    assert payload["result"] == {}
    assert payload["summary"]["trace_id"] == TRACE
    assert payload["summary"]["http_status"] == 200
    assert helper.public_code(payload["summary"]["projection_error_code"])


@pytest.mark.parametrize("path", ["preview", "review"])
@pytest.mark.parametrize("http_error", [True, False])
def test_failed_service_response_keeps_code_trace_and_http_status(path, http_error):
    response = {"status": "error", "trace_id": TRACE, "error": {
        "code": SERVICE_CODE, "message": "PRIVATE_SENTINEL", "url": SETTINGS["service_url"]},
        "token": "PRIVATE_SENTINEL"}
    status = 403 if http_error else 200
    code, payload = invoke(path, response, status, http_error=http_error)
    assert code == 1
    retained = diagnostics(path, payload)
    assert retained["error_code"] == response["error"]["code"]
    assert retained["trace_id"] == response["trace_id"]
    assert retained["http_status" if path == "preview" else "status_code"] == status


@pytest.mark.parametrize("path", ["preview", "review"])
@pytest.mark.parametrize("bad_field", ["code", "trace"])
def test_invalid_diagnostic_does_not_drop_its_safe_neighbor(path, bad_field):
    response = {"trace_id": TRACE, "error": {"code": SERVICE_CODE}}
    if bad_field == "code":
        response["error"]["code"] = "ghp_planted"
    else:
        response["trace_id"] = "https://private.example.invalid/PRIVATE_SENTINEL"
    code, payload = invoke(path, response, 403, http_error=True)
    assert code == 1
    retained = diagnostics(path, payload)
    assert retained["http_status" if path == "preview" else "status_code"] == 403
    if bad_field == "code":
        assert retained["trace_id"] == TRACE
        assert retained.get("error_code") != response["error"]["code"]
    else:
        assert retained["error_code"] == SERVICE_CODE
        assert not retained.get("trace_id")
    assert "ghp_planted" not in json.dumps(payload)


@pytest.mark.parametrize("path", ["preview", "review"])
@pytest.mark.parametrize("body", [b'{broken PRIVATE_SENTINEL', b'\xffPRIVATE_SENTINEL', b'[]'])
@pytest.mark.parametrize("http_error", [True, False])
def test_unparseable_service_response_keeps_only_available_http_status(path, body, http_error):
    status = 502 if http_error else 200
    code, payload = invoke(path, body, status, http_error=http_error)
    assert code == 1
    retained = diagnostics(path, payload)
    assert retained["http_status" if path == "preview" else "status_code"] == status
    assert not retained.get("trace_id")


@pytest.mark.parametrize("path", ["preview", "review"])
def test_interrupted_response_retains_received_http_status(path):
    code, payload = invoke(path, b"", interrupted=True)
    assert code == 1
    assert diagnostics(path, payload)["http_status" if path == "preview" else "status_code"] == 200


@pytest.mark.parametrize("path", ["preview", "review"])
def test_legitimate_credential_error_code_is_not_provider_prose(path):
    response = {"trace_id": TRACE, "error": {"code": "invalid_credentials", "message": "PRIVATE_SENTINEL"}}
    code, payload = invoke(path, response, 401, http_error=True)
    assert code == 1
    assert diagnostics(path, payload)["error_code"] == response["error"]["code"]


def test_preview_accepts_managed_user_actor_login():
    response = preview_response("example_org")
    code, payload = invoke("preview", response)
    assert code == 0
    assert payload["result"]["observation"]["github_actor_login"] == response["result"]["observation"]["token_actor_login"]


@pytest.mark.parametrize("mode", ["apply", "dry-run"])
@pytest.mark.parametrize("fault", ["opposite_mode", "missing_mode", "unknown_mode",
                                  "rejected", "pending", "missing_status"])
def test_preview_success_must_match_requested_operation(mode, fault):
    response = preview_response()
    response["result"]["mode"] = mode
    if fault == "opposite_mode":
        response["result"]["mode"] = "dry-run" if mode == "apply" else "apply"
    elif fault == "missing_mode":
        del response["result"]["mode"]
    elif fault == "unknown_mode":
        response["result"]["mode"] = "PRIVATE_SENTINEL"
    elif fault == "missing_status":
        del response["status"]
    else:
        response["status"] = fault
    code, payload = invoke("preview", response, mode=mode)
    assert code == 1
    assert payload["status"] == ("accepted_unverified" if mode == "apply" else "invalid")
    assert payload["records"] == {} and payload["result"] == {}
    assert payload["summary"]["http_status"] == 200
    assert payload["summary"]["trace_id"] == response["trace_id"]
    assert helper.public_code(payload["summary"]["projection_error_code"])
    if mode == "apply":
        assert payload["warnings"][0]["code"] == "apply_response_unverified"
        assert payload["summary"]["recommendation"]


def test_interrupted_apply_response_preserves_uncertainty_and_received_status():
    code, payload = invoke("preview", b"", interrupted=True, mode="apply")
    assert code == 1
    assert payload["status"] == "outcome_unknown"
    assert payload["warnings"][0]["code"] == "apply_outcome_unknown"
    assert payload["summary"]["http_status"] == 200


@pytest.mark.parametrize("mode", ["apply", "dry-run"])
@pytest.mark.parametrize("fault", ["unknown_field", "unsafe_actor", "unsafe_trace", "json", "utf8", "shape"])
def test_preview_invalid_success_preserves_apply_uncertainty(mode, fault):
    response = preview_response()
    response["result"]["mode"] = mode
    if fault == "unknown_field":
        response["result"]["unexpected"] = "PRIVATE_SENTINEL"
    elif fault == "unsafe_actor":
        response["result"]["observation"]["token_actor_login"] = "ghp_planted[bot]"
    elif fault == "unsafe_trace":
        response["trace_id"] = "https://private.example.invalid/PRIVATE_SENTINEL"
    else:
        response = {"json": b'{broken PRIVATE_SENTINEL',
                    "utf8": b'\xffPRIVATE_SENTINEL', "shape": b'[]'}[fault]
    code, payload = invoke("preview", response, mode=mode)
    assert code == 1
    assert payload["status"] == ("accepted_unverified" if mode == "apply" else "invalid")
    assert payload["records"] == {} and payload["result"] == {}
    summary = payload["summary"]
    assert summary["http_status"] == 200
    assert summary.get("trace_id", "") == (
        TRACE if isinstance(response, dict) and fault != "unsafe_trace" else ""
    )
    assert helper.public_code(summary["projection_error_code"])
    assert "ghp_planted" not in json.dumps(payload)
    if mode == "apply":
        assert payload["warnings"][0]["code"] == "apply_response_unverified"
        assert summary["recommendation"]


@pytest.mark.parametrize("mode", ["apply", "dry-run"])
def test_pre_response_transport_refusal_is_not_labeled_a_projection_failure(mode):
    code, payload = invoke("preview", b"", transport_error=helper.LaunchplaneSafetyError(
        "invalid_request_url_scheme"
    ), mode=mode)
    assert code == 1
    assert payload["status"] == "invalid"
    assert "projection_error_code" not in payload["summary"]
    assert "http_status" not in payload["summary"]


@pytest.mark.parametrize("key", ["retry-✓", "retry\r\nPRIVATE_SENTINEL"])
def test_apply_header_encoding_failure_is_not_an_uncertain_response(key):
    def encode_headers(request, **_kwargs):
        connection = http.client.HTTPConnection("service.example.invalid")
        connection.putrequest("POST", "/")
        for name, value in request.header_items():
            connection.putheader(name, value)
        pytest.fail("Expected header rejection before opening a connection")

    args = list(PREVIEW_ARGS)
    args[args.index("--mode") + 1] = "apply"
    args[args.index("--idempotency-key") + 1] = key
    args.append("--reviewed-dry-run")
    stdout = io.StringIO()
    with patch.object(helper, "prepare_operator_settings", return_value=SETTINGS), \
         patch.object(helper, "safe_urlopen", side_effect=encode_headers), redirect_stdout(stdout):
        code = helper.main(args)
    rendered = stdout.getvalue()
    payload = json.loads(rendered)
    assert code == 1 and payload["status"] == "invalid"
    assert "projection_error_code" not in payload["summary"]
    assert "http_status" not in payload["summary"]
    assert "PRIVATE_SENTINEL" not in rendered


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
