#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1"]
# ///
"""Withdrawal apply binds a reviewed dry run and verifies a service readback."""
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from test_launchplane_helpers import write_action as h

HEAD = "a" * 40
PLAN = "b" * 64
OPERATION_KEY = "withdraw-1"
REASON = "PRIVATE_REASON_SENTINEL Director rerouted the PR."
DECISION = "https://github.com/example/direction/issues/1#issuecomment-20"


def argv(mode="dry-run", evidence="", repo="example/site", operation_key=OPERATION_KEY):
    args = ["product-review-withdrawal-" + mode, "--product", "site", "--repo", repo, "--pr", "42", "--head", HEAD, "--reason", REASON, "--director-decision", DECISION]
    if mode != "dry-run":
        args += ["--expected-plan-digest", PLAN, "--idempotency-key", operation_key]
    if mode == "apply":
        args += ["--reviewed-dry-run", "--dry-run-evidence-file", evidence]
    return args


def response(applied=False, readback=False, repo="example/site"):
    result = {"applied": applied, "plan_sha256": PLAN, "head_sha": HEAD}
    if applied:
        result.update(history_url=f"https://github.com/{repo}/pull/42#issuecomment-100", history_marker="<!-- launchplane:review-withdrawal:" + "c" * 64 + " -->")
    if readback:
        result["readback_matches"] = True
    return {"status": "accepted", "trace_id": "trace-test", "records": {}, "result": result}


def invoke(args, replies):
    output = io.StringIO()
    with patch.object(h, "prepare_operator_settings", return_value={"service_url": "https://plane.example.invalid"}), patch.object(h, "request_launchplane", side_effect=replies) as post, redirect_stdout(output):
        code = h.main(args)
    return code, json.loads(output.getvalue()), post.call_args_list


@pytest.mark.parametrize("repo,operation_key", [("example/site", OPERATION_KEY), ("example/kiosk-app", "withdraw-kiosk-app-42")])
def test_dry_run_and_apply_readback_bind_exact_request(tmp_path, repo, operation_key):
    code, dry, calls = invoke(argv(repo=repo), [response(repo=repo)])
    assert code == 0 and calls[0].kwargs["body"]["mode"] == "dry-run"
    assert not calls[0].kwargs.get("idempotency_key")
    assert REASON not in json.dumps(dry)
    evidence = tmp_path / "dry.json"
    evidence.write_text(json.dumps(dry))
    code, applied, calls = invoke(argv("apply", str(evidence), repo=repo, operation_key=operation_key), [response(True, repo=repo), response(True, True, repo=repo)])
    assert code == 0 and applied["result"]["readback_matches"] is True
    assert [call.kwargs["body"]["mode"] for call in calls] == ["apply", "readback"]
    assert all(call.kwargs["idempotency_key"] == operation_key for call in calls)
    assert calls[0].kwargs["body"]["director_decision_url"] == DECISION
    assert REASON not in json.dumps(applied)
    assert applied["result"]["history_url"] == response(True, repo=repo)["result"]["history_url"]
    assert applied["summary"]["idempotency_key"] == operation_key
    code, readback, calls = invoke(argv("readback", repo=repo, operation_key=operation_key), [response(True, True, repo=repo)])
    assert code == 0 and readback["result"]["readback_matches"] is True and len(calls) == 1


def test_changed_reason_refuses_before_network(tmp_path):
    _, dry, _ = invoke(argv(), [response()])
    evidence = tmp_path / "dry.json"
    evidence.write_text(json.dumps(dry))
    args = argv("apply", str(evidence))
    args[args.index(REASON)] = "Different decision reason"
    code, result, calls = invoke(args, [])
    assert code != 0 and not calls
    assert result["status"] != "ok"


def test_readback_mismatch_is_not_success(tmp_path):
    _, dry, _ = invoke(argv(), [response()])
    evidence = tmp_path / "dry.json"
    evidence.write_text(json.dumps(dry))
    mismatch = response(True, True)
    mismatch["result"]["readback_matches"] = False
    code, result, calls = invoke(argv("apply", str(evidence)), [response(True), mismatch])
    assert code != 0 and result["status"] != "ok"
    assert len(calls) == 2


def test_unsettled_apply_is_unknown_and_terminal_replan_needs_a_new_review(tmp_path):
    import urllib.error

    _, dry, _ = invoke(argv(), [response()])
    evidence = tmp_path / "dry.json"
    evidence.write_text(json.dumps(dry))
    body = {"trace_id": "trace-test", "error": {"code": "product_review_withdrawal_unsettled", "message": "PRIVATE_REASON_SENTINEL"}}
    error = urllib.error.HTTPError("https://plane.example.invalid", 409, "Conflict", {}, io.BytesIO(json.dumps(body).encode()))
    code, result, _ = invoke(argv("apply", str(evidence)), [error])
    assert code != 0 and result["status"] == "outcome_unknown"
    assert REASON not in json.dumps(result)
    replan = response()
    replan["result"]["replan_required"] = True
    code, result, calls = invoke(argv("apply", str(evidence)), [replan])
    assert code != 0 and result["status"] == "replan_required"
    assert len(calls) == 1


def test_refusal_reasons_are_allowlisted_and_do_not_override_recovery(tmp_path: Path) -> None:
    import urllib.error
    from launchplane_review_withdrawal import REFUSAL_ADVICE, RECOVERY

    _, dry, _ = invoke(argv(), [response()])
    evidence = tmp_path / "dry.json"
    evidence.write_text(json.dumps(dry))
    def failure(reason: object, code: str = "product_review_withdrawal_refused", status: int = 409) -> urllib.error.HTTPError:
        body = {"error": {"code": code, "reason": reason, "message": "PRIVATE_REASON_SENTINEL"}}
        return urllib.error.HTTPError("https://plane.example.invalid", status, "Failure", {}, io.BytesIO(json.dumps(body).encode()))
    reason = "no_pending_labeled_request"
    code, result, calls = invoke(argv("apply", str(evidence)), [failure(reason)])
    assert code != 0 and result["status"] == "refused" and len(calls) == 1
    assert result["summary"]["refusal_reason"] == reason
    assert REFUSAL_ADVICE[reason] in result["summary"]["recommendation"]
    assert RECOVERY["refused"] in result["summary"]["recommendation"]
    assert result["summary"]["idempotency_key"] == OPERATION_KEY
    assert "PRIVATE_REASON_SENTINEL" not in json.dumps(result)
    for bad_reason in (None, "PRIVATE_REASON_SENTINEL", "removal_unconfirmed", "history_unconfirmed", "later_review_request",
                   {"no_pending_labeled_request": True}, ["no_pending_labeled_request"]):
        _, result, calls = invoke(argv(), [failure(bad_reason)])
        assert result["status"] == "refused" and len(calls) == 1
        assert "refusal_reason" not in result["summary"]
        assert "PRIVATE_REASON_SENTINEL" not in json.dumps(result)
    for mode in ("dry-run", "readback"):
        _, result, calls = invoke(argv(mode), [failure(reason, status=503)])
        assert "refusal_reason" not in result["summary"] and len(calls) == 1
    reason = next(iter(REFUSAL_ADVICE))
    for replies, expected in (([failure(reason, status=503)], "outcome_unknown"),
                              ([failure(reason, code="product_review_withdrawal_stale_plan")], "stale_plan"),
                              ([failure(reason, code="product_review_withdrawal_unsettled")], "outcome_unknown"),
                              ([response(True), failure(reason)], "accepted_unverified")):
        _, result, _ = invoke(argv("apply", str(evidence)), replies)
        assert result["status"] == expected and "refusal_reason" not in result["summary"]
        assert result["summary"]["idempotency_key"] == OPERATION_KEY
        if expected == "accepted_unverified":
            assert result["result"]["applied"] is True
        assert "PRIVATE_REASON_SENTINEL" not in json.dumps(result)


def test_canonical_repository_casing_is_accepted_on_readback():
    args = argv("readback")
    args[args.index("example/site")] = "Example/Site"
    code, result, _ = invoke(args, [response(True, True)])
    assert code == 0 and result["result"]["readback_matches"]


def test_recovery_classifier_preserves_receipts_and_distinguishes_other_callers(tmp_path):
    import urllib.error

    _, dry, _ = invoke(argv(), [response()])
    evidence = tmp_path / "dry.json"
    evidence.write_text(json.dumps(dry))
    def failure(status, code):
        body = {"trace_id": "trace-test", "error": {"code": code, "message": "PRIVATE_REASON_SENTINEL"}}
        return urllib.error.HTTPError("https://plane.example.invalid", status, "Failure", {}, io.BytesIO(json.dumps(body).encode()))
    for http_status, error_code, expected in (
        (503, "product_review_withdrawal_unavailable", "outcome_unknown"),
        (502, "gateway_error", "outcome_unknown"),
        (409, "product_review_withdrawal_target_busy", "target_busy"),
        (409, "product_review_withdrawal_in_progress", "in_progress"),
        (409, "product_review_withdrawal_conflict", "conflict"),
        (409, "product_review_withdrawal_not_started", "not_started"),
        (409, "product_review_withdrawal_invalid_request", "invalid_request"),
        (409, "product_review_withdrawal_stale_plan", "stale_plan"),
        (409, "product_review_withdrawal_history_unverifiable", "history_unverifiable"),
    ):
        code, result, calls = invoke(argv("apply", str(evidence)), [failure(http_status, error_code)])
        assert code != 0 and result["status"] == expected
        assert result["summary"]["idempotency_key"] == OPERATION_KEY
        assert len(calls) == 1
        assert "PRIVATE_REASON_SENTINEL" not in json.dumps(result)
        if expected == "outcome_unknown":
            from launchplane_review_withdrawal import RECOVERY
            assert result["summary"]["recommendation"] == RECOVERY[expected]
    code, result, _ = invoke(argv("apply", str(evidence)), [response(True), failure(503, "service_unavailable")])
    assert code != 0 and result["status"] == "accepted_unverified"
    assert result["result"]["applied"] is True
    assert result["result"]["history_url"] == response(True)["result"]["history_url"]
    unresolved = response()
    unresolved["result"].update(readback_matches=False, operation_state="reconcile_required")
    code, result, _ = invoke(argv("readback"), [unresolved])
    assert code != 0 and result["status"] == "outcome_unknown"
    replan = response()
    replan["result"].update(replan_required=True, prior_effects_unknown=True, readback_matches=False, operation_state="completed")
    code, result, _ = invoke(argv("readback"), [replan])
    assert code != 0 and result["status"] == "replan_required"
    assert result["result"]["prior_effects_unknown"] is True
    replan["result"]["history_unverifiable"] = True
    code, result, _ = invoke(argv("readback"), [replan])
    assert code != 0 and result["status"] == "history_unverifiable"
    assert result["result"]["prior_effects_unknown"] is True
    from launchplane_review_withdrawal import RECOVERY
    assert result["summary"]["recommendation"].startswith(RECOVERY["history_unverifiable"])
    assert RECOVERY["replan_required"] in result["summary"]["recommendation"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__]))
