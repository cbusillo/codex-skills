#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.2.0"]
# ///
"""Continuation apply binds its saved review; readback binds the continued operation."""
import io
import json
import urllib.error
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from test_launchplane_helpers import write_action as h

DIGEST = "d" * 64
REASON = "PRIVATE_REASON_SENTINEL original promotion refused before dispatch."
SERVICE = "https://plane.example.invalid"


def argv(command="review", evidence="", digest=DIGEST, reason=REASON):
    args = ["release-continuation-" + command, "--product", "site", "--decision-record-id", "decision-1", "--reason", reason]
    if command == "apply":
        args += ["--expected-digest", digest, "--review-evidence-file", evidence]
    return args


def continuation(status="ready", **changes):
    body = {
        "trace_id": "trace-test", "decision_record_id": "decision-1", "operation_id": "promotion-op-1",
        "review_digest": DIGEST, "status": status, "provider_mutation": False, "apply_authorized": True,
    }
    return {**body, **changes}


def operation(status="pending", **changes):
    view = {
        "operation_id": "promotion-op-1", "product": "site", "context": "site", "instance": "prod", "attempt": 0,
        "created_at": "2026-11-11T17:00:00Z", "started_at": "", "updated_at": "2026-11-11T21:00:00Z",
        "finished_at": "", "error_code": "", "error_message": "", "request_id": "request-1",
        "status": status, "phase": "created", "result": None,
    }
    return {"status": "accepted", "trace_id": "trace-read", "operation": {**view, **changes}}


def http_error(status, code):
    body = {"trace_id": "trace-err", "error": {"code": code, "message": "PRIVATE_REASON_SENTINEL"}}
    return urllib.error.HTTPError(SERVICE, status, "Failure", {}, io.BytesIO(json.dumps(body).encode()))


def invoke(args, posts=(), reads=(), service=SERVICE):
    output = io.StringIO()
    with patch.object(h, "prepare_operator_settings", return_value={"service_url": service, "token": "t"}), \
            patch.object(h, "request_launchplane", side_effect=list(posts)) as post, \
            patch.object(h, "request_launchplane_read", side_effect=list(reads)) as read, redirect_stdout(output):
        code = h.main(args)
    return code, json.loads(output.getvalue()), post.call_args_list, read.call_args_list


def saved(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


def reviewed(tmp_path):
    code, review, posts, _ = invoke(argv(), [continuation()])
    assert code == 0 and review["result"]["review_digest"] == DIGEST
    assert posts[0].kwargs["body"] == {"reason": REASON, "expected_digest": "", "apply": False}
    assert posts[0].kwargs["path"] == "/v1/products/site/releases/decision-1/backup-continuation"
    assert REASON not in json.dumps(review)
    return saved(tmp_path, "review.json", review)


@pytest.mark.parametrize("status", ["queued", "replayed"])
def test_apply_sends_exact_review_and_readback_binds_the_continued_operation(tmp_path, status):
    code, applied, posts, _ = invoke(argv("apply", reviewed(tmp_path)), [continuation(status)])
    assert code == 0 and applied["result"]["continuation_status"] == status
    assert posts[0].kwargs["body"] == {"reason": REASON, "expected_digest": DIGEST, "apply": True}
    assert "read back" in applied["summary"]["recommendation"]
    evidence = saved(tmp_path, "apply.json", applied)
    code, readback, _, reads = invoke(["odoo-promotion-operation-read", "--continuation-evidence-file", evidence, "--context", "site"], reads=[operation()])
    assert code == 0 and readback["result"]["status"] == "pending"
    assert reads[0].kwargs["path"] == "/v1/odoo-prod-promotions/operations/promotion-op-1"
    assert reads[0].kwargs["query"] == {"product": "site", "context": "site"}


@pytest.mark.parametrize("change", ["reason", "digest", "service", "missing"])
def test_changed_or_missing_review_refuses_before_sending(tmp_path, change):
    evidence = reviewed(tmp_path)
    args = argv("apply", evidence)
    service = SERVICE
    if change == "reason":
        args = argv("apply", evidence, reason="A different reason")
    elif change == "digest":
        args = argv("apply", evidence, digest="e" * 64)
    elif change == "service":
        service = "https://other.example.invalid"
    else:
        args = argv("apply", str(tmp_path / "absent.json"))
    code, result, posts, _ = invoke(args, service=service)
    assert code != 0 and result["status"] == "invalid" and not posts


def test_stale_review_and_refusals_are_not_success(tmp_path):
    evidence = reviewed(tmp_path)
    code, result, _, _ = invoke(argv("apply", evidence), [http_error(409, "backup_continuation_review_changed")])
    assert code != 0 and result["status"] == "stale_review"
    code, result, _, _ = invoke(argv("apply", evidence), [http_error(409, "odoo_original_effects_not_proven_absent")])
    assert code != 0 and result["status"] == "refused"
    assert result["summary"]["error_code"] == "odoo_original_effects_not_proven_absent"
    assert "PRIVATE_REASON_SENTINEL" not in json.dumps(result)


UNMATCHED = {"changed_digest": {"review_digest": "f" * 64}, "changed_operation": {"operation_id": "other-op"}}


@pytest.mark.parametrize("failure", [http_error(502, "bad_gateway"), urllib.error.URLError("reset"), *UNMATCHED])
def test_lost_or_unmatched_apply_response_is_unknown_and_resubmission_replays(tmp_path, failure):
    evidence = reviewed(tmp_path)
    reply = continuation("queued", **UNMATCHED[failure]) if isinstance(failure, str) else failure
    code, result, _, _ = invoke(argv("apply", evidence), [reply])
    assert code != 0 and result["status"] == "outcome_unknown"
    assert "identical apply" in result["summary"]["recommendation"]
    code, result, _, _ = invoke(argv("apply", evidence), [continuation("replayed")])
    assert code == 0 and result["result"]["continuation_status"] == "replayed"


def test_review_reports_an_identity_that_cannot_apply():
    code, result, _, _ = invoke(argv(), [continuation(apply_authorized=False)])
    assert code == 0 and [item["code"] for item in result["warnings"]] == ["apply_not_authorized"]


def read_args(tmp_path):
    return ["odoo-promotion-operation-read", "--continuation-evidence-file", reviewed(tmp_path), "--context", "site"]


def test_readback_shows_terminal_and_unknown_effect_states_truthfully(tmp_path):
    result = {"context": "site", "from_instance": "testing", "to_instance": "prod", "request_id": "request-1",
              "run_status": "fail", "input_status": "ready", "backup_status": "pass", "promotion_status": "fail",
              "deployment_record_id": "deploy-1", "error_message": "PRIVATE_REASON_SENTINEL"}
    reply = operation("fail", phase="failed", error_code="odoo_promotion_failed", error_message="PRIVATE_REASON_SENTINEL", result=result)
    code, projected, _, _ = invoke(read_args(tmp_path), reads=[reply])
    assert code == 0 and projected["result"]["status"] == "fail"
    assert projected["result"]["result"]["promotion_status"] == "fail"
    assert projected["result"]["result"]["deployment_status"] == "skipped"
    assert "PRIVATE_REASON_SENTINEL" not in json.dumps(projected)
    code, projected, _, _ = invoke(read_args(tmp_path), reads=[operation("reconciliation_required", phase="promotion_started")])
    assert code == 0 and "reconciliation_required" in [item["code"] for item in projected["warnings"]]


@pytest.mark.parametrize("reply", [
    operation(operation_id="other-op"),
    operation(context="other"),
    {"status": "accepted", "trace_id": "trace-read", "operation": {**operation()["operation"], "reason": "", "target_artifact_id": "a"}},
    {"status": "ok", "trace_id": "trace-read", "operation": {"operation_id": "promotion-op-1", "status": "pending"}, "result": {}},
])
def test_readback_refuses_another_operation_family_or_scope(tmp_path, reply):
    code, result, _, _ = invoke(read_args(tmp_path), reads=[reply])
    assert code != 0 and result["status"] == "invalid_response"
    assert "not evidence of absence" in result["summary"]["recommendation"]


def test_readback_not_found_is_never_absence(tmp_path):
    code, result, _, _ = invoke(read_args(tmp_path), reads=[http_error(404, "not_found")])
    assert code != 0 and result["status"] == "not_found"
    assert "never proves" in result["summary"]["recommendation"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
