#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1"]
# ///
"""Runtime-setting removal dry run and readback bind the exact review and never print the reason."""

import io
import json
import urllib.error
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from test_launchplane_helpers import contract, write_action as h
import launchplane_runtime_setting_removal as removal

DRY_RUN, READBACK = "runtime-setting-removal-dry-run", "runtime-setting-removal-readback"
SERVICE = "https://plane.example.invalid"
PLAN = "a" * 64
REVIEW = "runtime-removal-" + PLAN
REASON = "PRIVATE_SENTINEL retire unused settings"
SCOPE_ARGS = ("--product", "example", "--scope", "context", "--context", "example-testing")


def argv(command=DRY_RUN, *, scope=SCOPE_ARGS, keys=("OLD_FLAG", "LEGACY_URL"), reason=REASON,
         review=REVIEW, plan=PLAN):
    args = [command, *scope, *[item for key in keys for item in ("--key", key)], "--reason", reason]
    if command == READBACK:
        if review is not None:
            args += ["--review-id", review]
        if plan is not None:
            args += ["--plan-sha256", plan]
    return args


def response(mode="dry-run", **updates):
    # Producer removal_plan (control_plane/runtime_setting_removal.py) plus the
    # readback fields from PostgresRecordStore.runtime_setting_removal.
    result = {
        "product": "example", "scope": "context", "context": "example-testing", "instance": "",
        "removed_keys": ["LEGACY_URL", "OLD_FLAG"], "record_sha256": "b" * 64,
        "updated_at": "2026-10-01T12:00:00Z", "surviving_keys": ["KEEP_ME"],
        "unprintable_survivor_count": 1, "surviving_value_count": 2, "retirement_intent_count": 0,
        "record_deleted": False, "requires_compatible_readers_and_rollback": False,
        "authority_sha256": "c" * 64, "applied": False, "plan_sha256": PLAN, "review_id": REVIEW,
    }
    if mode == "readback":
        result.update(readback_matches=True, current_record_present=True)
    result.update(updates)
    return {"status": "ok", "trace_id": "launchplane_req_runtime_removal", "result": result}


def invoke(args, *, reply=None):
    output = io.StringIO()
    mode = "readback" if args[0] == READBACK else "dry-run"
    with (patch.object(h, "prepare_operator_settings", return_value={"service_url": SERVICE}),
          patch.object(h, "request_launchplane", return_value=response(mode)) as post,
          redirect_stdout(output)):
        if reply is not None:
            post.side_effect = [reply]
        code = h.main(args)
    return code, json.loads(output.getvalue()), list(post.call_args_list)


def test_dry_run_posts_exact_body_without_reason_in_output():
    code, output, calls = invoke(argv())
    assert code == 0 and output["status"] == "ok" and len(calls) == 1
    call = calls[0].kwargs
    assert call["path"] == contract.helper_command_path(DRY_RUN)
    assert call["body"] == {"mode": "dry-run", "product": "example", "scope": "context",
        "context": "example-testing", "instance": "", "keys": ["OLD_FLAG", "LEGACY_URL"], "reason": REASON}
    assert not call.get("idempotency_key")
    assert output["request"] == {"mode": "dry-run", "product": "example", "scope": "context",
        "context": "example-testing", "instance": "", "keys": ["OLD_FLAG", "LEGACY_URL"],
        "payload_source": "operator_argument"}
    assert "PRIVATE_SENTINEL" not in json.dumps(output)
    result = output["result"]
    source = response()["result"]
    for key in ("removed_keys", "surviving_keys", "record_sha256", "authority_sha256", "plan_sha256",
                "review_id", "updated_at", "unprintable_survivor_count", "surviving_value_count",
                "retirement_intent_count", "record_deleted", "requires_compatible_readers_and_rollback"):
        assert result[key] == source[key]
    assert result["applied"] is False and result["apply_supported"] is False
    assert result["surviving_keys_truncated"] is False and result["dropped_field_paths"] == []
    assert not removal.READBACK_FIELDS & result.keys()
    assert output["summary"]["trace_id"] == response()["trace_id"]
    assert (output["summary"]["review_id"], output["summary"]["plan_sha256"]) == (REVIEW, PLAN)


def test_readback_sends_review_binding_and_projects_match():
    reply = response("readback", readback_matches=False, current_record_present=True)
    code, output, calls = invoke(argv(READBACK), reply=reply)
    assert code == 0 and len(calls) == 1
    body = calls[0].kwargs["body"]
    assert (body["mode"], body["review_id"], body["reviewed_plan_sha256"]) == ("readback", REVIEW, PLAN)
    assert body["reason"] == REASON
    assert not calls[0].kwargs.get("idempotency_key")
    assert output["request"]["review_id"] == REVIEW
    assert "reviewed_plan_sha256" not in output["request"]
    assert output["result"]["readback_matches"] is False
    assert output["result"]["current_record_present"] is True
    assert output["summary"]["readback_matches"] is False
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize("missing", [{"review": None}, {"plan": None}])
def test_readback_requires_review_binding_arguments(missing):
    assert h.parse_args(argv(READBACK)).command == READBACK
    with pytest.raises(SystemExit) as raised, redirect_stdout(io.StringIO()):
        h.main(argv(READBACK, **missing))
    assert raised.value.code == 2


@pytest.mark.parametrize("command,kwargs", [
    (DRY_RUN, {"scope": ["--product", "example", "--scope", "global"]}),
    (DRY_RUN, {"scope": ["--product", removal.GLOBAL_PRODUCT, "--scope", "global", "--context", "x"]}),
    (DRY_RUN, {"scope": [*SCOPE_ARGS, "--instance", "testing"]}),
    (DRY_RUN, {"scope": ["--product", "example", "--scope", "instance", "--instance", "testing"]}),
    (DRY_RUN, {"scope": ["--product", "example", "--scope", "context"]}),
    (DRY_RUN, {"scope": ["--product", "../escape", "--scope", "context", "--context", "example-testing"]}),
    (DRY_RUN, {"keys": ("OLD_FLAG", "OLD_FLAG")}),
    (DRY_RUN, {"keys": ("ghp_" + "A" * 20,)}),
    (DRY_RUN, {"keys": ("has space",)}),
    (DRY_RUN, {"keys": tuple(f"KEY_{index}" for index in range(removal.MAX_KEYS + 1))}),
    (DRY_RUN, {"reason": "x" * (removal.MAX_REASON + 1)}),
    (DRY_RUN, {"reason": ""}),
    (READBACK, {"review": "runtime-removal-" + "A" * 64}),
    (READBACK, {"review": PLAN}),
    (READBACK, {"plan": "a" * 63}),
])
def test_invalid_arguments_are_refused_before_post(command, kwargs):
    code, output, calls = invoke(argv(command, **kwargs))
    assert code == 2 and output["status"] == "invalid" and calls == []
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_reason_is_sent_exactly_as_the_service_accepts_it():
    # Readback replays the saved reason, so even a whitespace-only one goes through unchanged.
    code, _, calls = invoke(argv(reason=" "))
    assert code == 0 and calls[0].kwargs["body"]["reason"] == " "


def test_global_scope_and_max_keys_are_accepted():
    keys = tuple(f"KEY_{index}" for index in range(removal.MAX_KEYS))
    reply = response(product=removal.GLOBAL_PRODUCT, scope="global", context="", removed_keys=sorted(keys))
    code, _, calls = invoke(argv(scope=["--product", removal.GLOBAL_PRODUCT, "--scope", "global"], keys=keys),
                            reply=reply)
    assert code == 0 and len(calls) == 1
    assert calls[0].kwargs["body"]["context"] == ""


def _set(**updates):
    return lambda reply: reply["result"].update(updates)


@pytest.mark.parametrize("command,change", [
    (DRY_RUN, _set(context="other-context")),
    (DRY_RUN, _set(product="other")),
    (DRY_RUN, _set(removed_keys=["OLD_FLAG"])),
    (DRY_RUN, _set(applied=True)),
    (DRY_RUN, _set(applied_at="2026-10-01T12:00:00Z")),
    (DRY_RUN, _set(replayed=True)),
    (DRY_RUN, _set(record_sha256="B" * 64)),
    (DRY_RUN, _set(review_id="runtime-removal-" + "d" * 64)),
    (DRY_RUN, _set(readback_matches=True, current_record_present=True)),
    (DRY_RUN, _set(record_deleted="false")),
    (DRY_RUN, _set(surviving_value_count=0)),
    (DRY_RUN, _set(retirement_intent_count=-1)),
    (DRY_RUN, lambda reply: reply.update(status="accepted")),
    (DRY_RUN, lambda reply: reply.update(records={"PRIVATE_SENTINEL": 1})),
    (READBACK, lambda reply: reply["result"].pop("readback_matches")),
    (READBACK, _set(plan_sha256="d" * 64, review_id="runtime-removal-" + "d" * 64)),
])
def test_response_drift_fails_closed(command, change):
    reply = response("readback" if command == READBACK else "dry-run")
    change(reply)
    code, output, calls = invoke(argv(command), reply=reply)
    assert code == 1 and output["status"] == "invalid" and len(calls) == 1
    assert output["result"] == {}
    assert output["summary"]["trace_id"] == reply["trace_id"]
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_token_shaped_surviving_key_invalidates_the_read():
    # Built at run time so the repository's own secret scanner sees no key-shaped literal.
    token_shaped = "sk-" + "A1" * 10
    code, output, _ = invoke(argv(), reply=response(surviving_keys=[token_shaped]))
    assert code == 1 and output["status"] == "invalid"
    assert token_shaped not in json.dumps(output)


def test_odd_surviving_name_and_unlisted_fields_are_dropped_by_path():
    reply = response(surviving_keys=["KEEP_ME", "odd name"], note="PRIVATE_SENTINEL",
                     updated_at="yesterday")
    code, output, _ = invoke(argv(), reply=reply)
    assert code == 0 and "PRIVATE_SENTINEL" not in json.dumps(output)
    assert output["result"]["surviving_keys"] == ["KEEP_ME"]
    assert output["result"]["updated_at"] == ""
    assert output["result"]["dropped_field_paths"] == [
        "result.<unlisted field>", "result.surviving_keys", "result.updated_at"]


def test_surviving_names_are_capped():
    names = [f"KEEP_{index}" for index in range(removal.SURVIVOR_CAP + 1)]
    code, output, _ = invoke(argv(), reply=response(surviving_keys=names, surviving_value_count=len(names)))
    assert code == 0
    assert output["result"]["surviving_keys"] == names[:removal.SURVIVOR_CAP]
    assert output["result"]["surviving_keys_truncated"] is True


def http_error(status, code):
    body = {"trace_id": "launchplane_req_refusal", "error": {"code": code, "message": "PRIVATE_SENTINEL"}}
    return urllib.error.HTTPError(SERVICE, status, "PRIVATE_SENTINEL", {}, io.BytesIO(json.dumps(body).encode()))


@pytest.mark.parametrize("status,code", [
    (403, "authorization_denied"),
    (409, "runtime_setting_removal_unverified"),
    (503, "database_storage_required"),
    (400, "invalid_request"),
    (401, "unauthorized"),
])
def test_http_refusals_keep_trace_and_shared_classification(status, code):
    exit_code, output, calls = invoke(argv(), reply=http_error(status, code))
    shared = h.summarize_http_error(operation=DRY_RUN, request={}, exc=http_error(status, code))
    assert exit_code == 1 and len(calls) == 1
    assert output["status"] == shared["status"]
    assert output["warnings"] == shared["warnings"]
    assert output["summary"]["trace_id"] == "launchplane_req_refusal"
    assert output["summary"]["error_code"] == code
    if status == 409:
        assert output["summary"]["recommendation"] == removal.STALE_RECOMMENDATION
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_client_timeout_is_a_read_budget_diagnostic():
    code, output, calls = invoke(argv(), reply=TimeoutError("PRIVATE_SENTINEL"))
    assert code == 1 and output["status"] == "unavailable" and len(calls) == 1
    assert output["summary"]["error_code"] == "client_timeout"
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
