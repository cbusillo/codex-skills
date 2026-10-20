#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1"]
# ///
"""Withdrawal apply binds a reviewed dry run and verifies a service readback."""
import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch

from test_launchplane_helpers import write_action as h

HEAD = "a" * 40
PLAN = "b" * 64
REASON = "PRIVATE_REASON_SENTINEL Director rerouted the PR."
DECISION = "https://github.com/example/direction/issues/1#issuecomment-20"


def argv(mode="dry-run", evidence=""):
    args = ["product-review-withdrawal-" + mode, "--product", "site", "--repo", "example/site", "--pr", "42", "--head", HEAD, "--reason", REASON, "--director-decision", DECISION]
    if mode != "dry-run":
        args += ["--expected-plan-digest", PLAN, "--idempotency-key", "withdraw-1"]
    if mode == "apply":
        args += ["--reviewed-dry-run", "--dry-run-evidence-file", evidence]
    return args


def response(applied=False, readback=False):
    result = {"applied": applied, "plan_sha256": PLAN, "head_sha": HEAD}
    if applied:
        result.update(history_url="https://github.com/example/site/pull/42#issuecomment-100", history_marker="<!-- launchplane:review-withdrawal:" + "c" * 64 + " -->")
    if readback:
        result["readback_matches"] = True
    return {"status": "accepted", "trace_id": "trace-test", "records": {}, "result": result}


def invoke(args, replies):
    output = io.StringIO()
    with patch.object(h, "prepare_operator_settings", return_value={"service_url": "https://plane.example.invalid"}), patch.object(h, "request_launchplane", side_effect=replies) as post, redirect_stdout(output):
        code = h.main(args)
    return code, json.loads(output.getvalue()), post.call_args_list


def test_dry_run_and_apply_readback_bind_exact_request(tmp_path):
    code, dry, calls = invoke(argv(), [response()])
    assert code == 0 and calls[0].kwargs["body"]["mode"] == "dry-run"
    assert not calls[0].kwargs.get("idempotency_key")
    assert REASON not in json.dumps(dry)
    evidence = tmp_path / "dry.json"
    evidence.write_text(json.dumps(dry))
    code, applied, calls = invoke(argv("apply", str(evidence)), [response(True), response(True, True)])
    assert code == 0 and applied["result"]["readback_matches"] is True
    assert [call.kwargs["body"]["mode"] for call in calls] == ["apply", "readback"]
    assert all(call.kwargs["idempotency_key"] == "withdraw-1" for call in calls)
    assert calls[0].kwargs["body"]["director_decision_url"] == DECISION
    assert REASON not in json.dumps(applied)


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


def test_canonical_repository_casing_is_accepted_on_readback():
    args = argv("readback")
    args[args.index("example/site")] = "Example/Site"
    code, result, _ = invoke(args, [response(True, True)])
    assert code == 0 and result["result"]["readback_matches"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__]))
