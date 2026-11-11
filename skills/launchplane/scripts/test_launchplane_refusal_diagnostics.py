# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.2.0", "websockets==17.4"]
# ///
from __future__ import annotations

import io
import json
import urllib.error
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import launchplane_migrated_upload as upload
import pytest
from test_launchplane_helpers import _calendar_payload, _run_main
from test_launchplane_helpers import write_action as h
from test_launchplane_migrated_upload import fixture, invoke


def response() -> dict[str, Any]:
    return {
        "trace_id": "launchplane_req_refusal_fixture",
        "error": {
            "code": "runtime_key_retired",
            "reason": "retired_key_conflict",
            "message": "Use schema version 2 and the complete retirement list without the key.",
        },
        "details": {
            "waiting_on": "updated_retirement_list",
            "wait_reason": "retirement_list_missing",
            "next_actor": "repository_agent",
            "next_action": "Review the corrected dry run.",
        },
        "payload": {"password": "PRIVATE_SENTINEL"},
    }


def http_error(body: object, status: int = 400) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://plane.example.invalid", status, "PRIVATE_SENTINEL", Message(), io.BytesIO(json.dumps(body).encode())
    )


def test_shared_http_refusal_retains_service_explanation() -> None:
    command = "product-config-dry-run"
    body = response()
    output = io.StringIO()
    with redirect_stdout(output):
        h.emit_http_error_payload(operation=command, request={}, exc=http_error(body))
    summary = json.loads(output.getvalue())["summary"]
    assert summary["error_code"] == body["error"]["code"]
    assert summary["error_reason"] == body["error"]["reason"]
    assert summary["error_message"] == body["error"]["message"]
    for key in ("waiting_on", "wait_reason", "next_actor", "next_action"):
        assert summary[key] == body["details"][key]
    assert "PRIVATE_SENTINEL" not in output.getvalue()


@pytest.mark.parametrize("field", ["reason", "waiting_on", "wait_reason", "next_actor"])
@pytest.mark.parametrize("value", ["current_deployment_unsettled", "Newer_Deployment", "Director", "Client"])
def test_structured_explanations_keep_valid_codes(field: str, value: str) -> None:
    body = response()
    source = body["error"] if field == "reason" else body["details"]
    source[field] = value
    output = h.summarize_http_error(operation="product-config-dry-run", request={}, exc=http_error(body))
    assert output["summary"]["error_reason" if field == "reason" else field] == value


def test_non_controller_nested_code_cannot_clear_an_unknown_outcome() -> None:
    import launchplane_testing_candidate_release as candidate
    from test_launchplane_testing_candidate_release import argv
    from test_launchplane_testing_candidate_release import invoke as invoke_candidate

    code = next(iter(candidate.CANDIDATE_REFUSALS))
    body = {"result": {"error": {"code": code, "message": "A prerequisite is missing."}}}
    status, output, calls = invoke_candidate(argv("readback"), http_error(body, 503))
    assert status != 0 and len(calls) == 1
    assert output["status"] == "outcome_unknown"
    assert output["summary"].get("error_code") != code
    assert output["summary"]["error_message"] == body["result"]["error"]["message"]


@pytest.mark.parametrize("mode", ["collect", "clear", "prepare-rebuild"])
def test_disposable_lane_uncertain_reply_keeps_its_explanation(mode: str) -> None:
    from test_launchplane_disposable_lane import invoke as invoke_lane

    body = response()
    code, output, calls, _ = invoke_lane(mode, responses=[http_error(body, 503)])
    assert code != 0 and len(calls) == 1
    assert output["status"] == "outcome_unknown"
    assert output["summary"]["error_message"] == body["error"]["message"]
    assert output["summary"]["error_reason"] == body["error"]["reason"]


@pytest.mark.parametrize("applied", [False, True])
def test_private_ingress_uncertain_reply_keeps_the_failing_exchange_explanation(applied: bool) -> None:
    from test_launchplane_testing_private_ingress import invoke as invoke_ingress
    from test_launchplane_testing_private_ingress import receipt

    body = response()
    replies = [receipt("apply")] if applied else []
    code, output, calls, _ = invoke_ingress("apply", responses=[*replies, http_error(body, 503)])
    assert code != 0 and len(calls) == len(replies) + 1
    assert output["status"] == ("accepted_unverified" if applied else "outcome_unknown")
    assert output["summary"][("read_back_" if applied else "") + "error_message"] == body["error"]["message"]


@pytest.mark.parametrize("location", ["error", "result.error", "result.blocking_reason"])
def test_nested_controller_refusal_uses_the_shared_explanation(location: str) -> None:
    body = response()
    if location != "error":
        body["result"] = {location.split(".")[1]: body.pop("error")}
    output = h.summarize_http_error(operation="merge-train-controller-run-once", request={}, exc=http_error(body))
    nested = body["error"] if location == "error" else body["result"][location.split(".")[1]]
    assert output["summary"]["error_message"] == nested["message"]
    assert output["summary"]["error_code"] == nested["code"]


@pytest.mark.parametrize("field", ["reason", "message", "waiting_on", "wait_reason", "next_actor", "next_action"])
@pytest.mark.parametrize(
    "private",
    [
        "password=PRIVATE_SENTINEL",
        "Bearer PRIVATE_SENTINEL",
        "ghp_PRIVATE_SENTINEL",
        "https://private.invalid/PRIVATE_SENTINEL",
        "/private/PRIVATE_SENTINEL/input.json",
        r"C:\private\PRIVATE_SENTINEL\input.json",
        "../PRIVATE_SENTINEL/input.json",
        '{"customer": "PRIVATE_SENTINEL"}',
        '["PRIVATE_SENTINEL", "other"]',
        "-----BEGIN " + "PRIVATE KEY----- PRIVATE_SENTINEL",
        "private-PRIVATE_SENTINEL.example.invalid",
        "payload='PRIVATE_SENTINEL'",
    ],
)
def test_refusal_prose_redacts_private_data_without_losing_neighboring_code(field: str, private: str) -> None:
    body = response()
    source = body["error"] if field in {"reason", "message"} else body["details"]
    source[field] = "Waiting: " + private
    output = h.summarize_http_error(operation="product-config-dry-run", request={}, exc=http_error(body))
    rendered = json.dumps(output)
    assert "PRIVATE_SENTINEL" not in rendered
    assert output["summary"]["error_code"] == body["error"]["code"]
    assert output["summary"]["trace_id"] == body["trace_id"]


@pytest.mark.parametrize("bad", [None, {}, [], 42, "", "word " * 101])
def test_malformed_message_keeps_code_and_marks_withheld_explanations(bad: object) -> None:
    body = response()
    body["error"] = {"code": "migrated_source_upload_incomplete", "message": bad}
    output = h.summarize_http_error(operation="testing-rebuild", request={}, exc=http_error(body, 409))
    assert output["summary"]["error_code"] == body["error"]["code"]
    assert "error_message" not in output["summary"]
    assert "error_reason" not in output["summary"]
    assert output["status"] == "refused"
    assert bool(output["summary"].get("explanation_withheld")) == (bad is not None and bad != "")


def test_prose_withheld_by_privacy_is_distinct_from_an_absent_explanation() -> None:
    body = response()
    body["error"] = {"code": "invalid_request", "message": "Authorization: Basic PRIVATE_SENTINEL"}
    output = h.summarize_http_error(operation="product-config-dry-run", request={}, exc=http_error(body))
    assert output["summary"]["explanation_withheld"] is True
    assert "error_message" not in output["summary"]
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize(
    "private",
    [
        "password: correct horse battery",
        "Authorization: Basic dXNlcjpwYXNz",
        "Contact jdoe@example.invalid",
        "Read /private/fixture/data",
        "Wait for localhost:5432",
    ],
)
def test_controller_result_and_summary_share_the_service_prose_privacy_policy(private: str) -> None:
    source = {"code": "not_ready", "message": private}
    result = h._project_merge_train_blocking_reason(source)
    diagnostics = h.service_response_diagnostics({"error": source})
    assert result["message"] != private
    assert diagnostics.get("error_message", "[redacted]") == result["message"]


@pytest.mark.parametrize("status", [409, 503])
def test_private_upload_get_keeps_the_service_refusal_without_sending_bytes(tmp_path: Path, status: int) -> None:
    args, _, _ = fixture(tmp_path)
    body = response()
    connection = Mock()
    connection.getresponse.return_value = Mock(status=status, read=Mock(return_value=json.dumps(body).encode()))
    output = io.StringIO()
    with (
        patch.object(upload, "PrivateHTTPSConnection", return_value=connection),
        patch.object(
            h, "prepare_operator_settings", return_value={"service_url": "https://plane.example.invalid", "token": "fixture"}
        ),
        patch.object(upload, "private_send") as send,
        redirect_stdout(output),
    ):
        code = upload.execute(h, args)
    summary = json.loads(output.getvalue())["summary"]
    assert code == (1 if status >= 500 else 2)
    assert summary["error_code"] == body["error"]["code"]
    assert summary["error_message"] == body["error"]["message"]
    assert summary["http_status"] == status
    send.assert_not_called()
    assert connection.request.call_count == 1
    assert connection.request.call_args.args[0] == "GET"
    connection.close.assert_called_once()


def test_upload_emits_shared_diagnostics_without_changing_refusal_or_retrying(tmp_path: Path) -> None:
    args, _, _ = fixture(tmp_path)
    body = response()
    failure = upload.UploadRefused("migrated_upload_refused", h.service_response_diagnostics(body))
    transport = Mock(side_effect=failure)
    code, output = invoke(args, transport)
    assert code == 2 and output["status"] == "refused"
    assert output["summary"]["error_message"] == body["error"]["message"]
    assert output["summary"]["error_code"] == body["error"]["code"]
    transport.assert_called_once()


def test_product_config_dispatch_reports_the_service_correction(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(_calendar_payload()))
    body = response()
    code, output, posts, reads = _run_main(["product-config-dry-run", "--payload-file", str(path)], post=http_error(body))
    assert code == 1 and len(posts) == 1 and not reads
    assert output["summary"]["error_message"] == body["error"]["message"]
    assert output["summary"]["error_code"] == body["error"]["code"]


@pytest.mark.parametrize("processor", ["withdrawal", "candidate_release", "release_request"])
def test_outcome_wrappers_preserve_diagnostics_without_taking_service_advice_as_authority(processor: str) -> None:
    import launchplane_release_request as release
    import launchplane_review_withdrawal as withdrawal
    import launchplane_testing_candidate_release as candidate

    body = response()
    diagnostics = h.service_response_diagnostics(body)
    output = io.StringIO()
    with redirect_stdout(output):
        if processor == "release_request":
            code = release._finish(h, release.Intent("example", "", "", "", "", {}), "refused", diagnostics=diagnostics)
        elif processor == "candidate_release":
            code = candidate.finish(
                h, operation="testing-candidate-release-dry-run", request={}, status="refused", diagnostics=diagnostics
            )
        else:
            code = withdrawal.finish(
                h, operation="product-review-withdrawal-dry-run", request={}, status="refused", diagnostics=diagnostics
            )
    result = json.loads(output.getvalue())
    assert code != 0 and result["status"] == "refused"
    assert result["summary"]["error_reason"] == body["error"]["reason"]
    assert result["summary"]["error_message"] == body["error"]["message"]
    assert result["summary"]["recommendation"] != body["details"]["next_action"]
    assert "refusal_reason" not in result["summary"]


def test_invalid_outer_error_does_not_hide_safe_nested_controller_diagnostics() -> None:
    body = response()
    body["result"] = {"blocking_reason": body["error"]}
    body["error"] = {"code": "ghp_PRIVATE_SENTINEL", "message": {"private": "PRIVATE_SENTINEL"}}
    output = h.summarize_http_error(operation="merge-train-controller-run-once", request={}, exc=http_error(body, 409))
    assert output["summary"]["error_code"] == body["result"]["blocking_reason"]["code"]
    assert output["summary"]["error_message"] == body["result"]["blocking_reason"]["message"]
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize("handshake", [True, False])
def test_websocket_refusal_preserves_available_reason_and_never_sends_bytes(handshake: bool) -> None:
    from websockets.datastructures import Headers
    from websockets.exceptions import ConnectionClosed, InvalidStatus
    from websockets.frames import Close
    from websockets.http11 import Response
    from websockets.sync import client

    body = response()
    failure = (
        InvalidStatus(Response(409, "Refused", Headers(), json.dumps(body).encode()))
        if handshake
        else ConnectionClosed(Close(1008, body["error"]["message"]), None)
    )
    raw = Mock(__enter__=Mock(return_value=Mock()), __exit__=Mock(return_value=False))
    websocket = Mock(__enter__=Mock(side_effect=failure), __exit__=Mock(return_value=False))
    with (
        patch.object(upload.socket, "create_connection", return_value=raw),
        patch.object(client, "connect", return_value=websocket),
        pytest.raises(upload.UploadRefused) as caught,
    ):
        upload.private_send(
            h,
            {"service_url": "https://plane.example.invalid", "token": "fixture"},
            "10.0.0.2",
            "/v1/fixture",
            timeout=10,
            connect_timeout=10,
            handle=io.BytesIO(b"private bytes"),
            offset=0,
            prefix="a" * 64,
            remaining=13,
        )
    assert caught.value.response_diagnostics["error_message"] == body["error"]["message"]
    if handshake:
        assert caught.value.response_diagnostics["error_code"] == body["error"]["code"]
        assert caught.value.response_diagnostics["http_status"] == 409
    websocket.send.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
