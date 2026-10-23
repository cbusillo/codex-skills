# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1"]
# ///

import io
import json
import urllib.error
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, NamedTuple
from unittest.mock import patch

import launchplane_contract as contract
import launchplane_testing_resources as resources
import pytest
from test_launchplane_helpers import write_action as h

PREFIX = resources.PREFIX
SERVICE = "https://plane.example.invalid"
PLAN = "a" * 64
RESOURCES = "b" * 64
LANE_BINDING = "c" * 64


class Invocation(NamedTuple):
    code: int
    output: dict[str, Any]
    calls: list[Any]
    settings_calls: int


def private_request(**updates: object) -> dict[str, Any]:
    return {
        "product": "example",
        "context": "example-testing",
        "instance": "testing",
        "operation_id": "retained-fixture",
        "reason": "PRIVATE_SENTINEL",
        **updates,
    }


def receipt(mode: str, **updates: object) -> dict[str, Any]:
    result: dict[str, Any] = {
        "product": "example",
        "context": "example-testing",
        "instance": "testing",
        "operation_id": "retained-fixture",
        "retained_target_preserved": True,
        "restore_started": False,
    }
    if mode == "readback":
        result.update(
            resources_verified=True,
            resources_binding_sha256=RESOURCES,
            binding_sha256=LANE_BINDING,
            production_resources="separation_ready",
            schedule_id="PRIVATE_SCHEDULE",
        )
    else:
        result.update(
            plan_sha256=PLAN, fresh_storage_required=True, published_host_ports=False, host_ssh_mounts=False, applied=mode == "apply"
        )
        if mode == "apply":
            result.update(resources_verified=True, resources_binding_sha256=RESOURCES, schedule_id="PRIVATE_SCHEDULE")
    return {"status": "accepted", "trace_id": f"launchplane_req_resources_{mode}", "records": {}, "result": {**result, **updates}}


def invoke(
    mode: str = "dry-run",
    *,
    body: dict[str, Any] | None = None,
    responses: list[dict[str, Any] | Exception] | None = None,
    edit_evidence: Callable[[dict[str, Any]], object] | None = None,
    service: str = SERVICE,
    reviewed: bool = True,
    idempotency_key: str = "original-key",
    initial: bool = False,
) -> Invocation:
    output = io.StringIO()
    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "repo").mkdir()
        payload, evidence_path = root / "request.json", root / "review.json"
        planning = private_request(initial_generation=True, operation_id="") if initial else private_request()
        payload.write_text(json.dumps(planning))
        with (
            patch.object(h, "active_repo_root", return_value=root / "repo"),
            patch.object(h, "prepare_operator_settings", return_value={"service_url": SERVICE}) as settings,
            patch.object(h, "request_launchplane", return_value=receipt("dry-run")) as post,
            redirect_stdout(output),
        ):
            if mode in {"apply", "release"}:
                argv = [PREFIX + "dry-run", "--payload-file", str(payload)]
                if initial:
                    argv += ["--idempotency-key", "original-key"]
                assert h.main(argv) == 0, output.getvalue()
                evidence = json.loads(output.getvalue())
                if edit_evidence:
                    edit_evidence(evidence)
                evidence_path.write_text(json.dumps(evidence))
                post.reset_mock()
                settings.reset_mock()
                output.seek(0)
                output.truncate()
            payload.write_text(json.dumps(planning if body is None else body))
            settings.return_value = {"service_url": service}
            post.side_effect = responses if responses is not None else [receipt(mode), receipt("readback")]
            argv = [PREFIX + mode, "--payload-file", str(payload)]
            if initial and mode not in {"apply", "release"}:
                argv += ["--idempotency-key", idempotency_key]
            if mode in {"apply", "release"}:
                argv += [
                    "--expected-plan-digest",
                    PLAN,
                    "--dry-run-evidence-file",
                    str(evidence_path),
                    "--idempotency-key",
                    idempotency_key,
                ]
                if reviewed:
                    argv.append("--reviewed-dry-run")
            code = h.main(argv)
            return Invocation(code, json.loads(output.getvalue()), list(post.call_args_list), settings.call_count)


@pytest.mark.parametrize("mode", ["dry-run", "apply", "readback"])
def test_resource_modes_send_only_the_retained_request(mode: str) -> None:
    code, output, calls, _ = invoke(mode)
    assert code == 0, output
    modes = ["apply", "readback"] if mode == "apply" else [mode]
    assert [call.kwargs["body"]["mode"] for call in calls] == modes
    for call in calls:
        expected = {key: value for key, value in private_request().items() if key != "product"}
        expected["mode"] = call.kwargs["body"]["mode"]
        if mode == "apply":
            expected["reviewed_plan_sha256"] = PLAN
        assert call.kwargs["body"] == expected
        assert call.kwargs["path"] == contract.helper_command_path(PREFIX + mode).format(product="example")
        assert call.kwargs.get("idempotency_key", "") == ("original-key" if expected["mode"] == "apply" else "")
    serialized = json.dumps(output)
    assert all(value not in serialized for value in ("PRIVATE_SENTINEL", "PRIVATE_SCHEDULE", "retained-fixture", "example-testing"))
    assert output["result"]["restore_started"] is False
    if mode == "apply":
        assert output["result"]["resource_readback_matches"] is True


@pytest.mark.parametrize(
    "updates",
    [
        {"instance": "prod"},
        {"operation_id": ""},
        {"operation_id": "../escape"},
        {"product": "../escape"},
        {"context": None},
        {"reason": " "},
        {"source_mode": "foreign"},
        {"target_id": "foreign"},
    ],
)
def test_invalid_resource_intent_never_reads_configuration(updates: dict[str, Any]) -> None:
    code, _, calls, settings = invoke(body=private_request(**updates))
    assert code != 0 and calls == [] and settings == 0


@pytest.mark.parametrize(
    "updates",
    [
        {"product": "other"},
        {"context": "other-testing"},
        {"operation_id": "other-operation"},
        {"source_mode": "container-export"},
        {"reason": "changed private intent"},
    ],
)
def test_apply_binds_every_private_request_field(updates: dict[str, Any]) -> None:
    code, _, calls, settings = invoke("apply", body=private_request(**updates))
    assert code != 0 and calls == [] and settings == 0


@pytest.mark.parametrize(
    "change",
    [
        lambda e: e.update(status="denied"),
        lambda e: e.update(operation=PREFIX + "readback"),
        lambda e: e["request"].update(payload_digest="d" * 64),
        lambda e: e["result"].update(plan_sha256="d" * 64),
        lambda e: e["result"].update(applied=True),
        lambda e: e["result"].update(restore_started=True),
        lambda e: e["result"].update(review_binding_sha256="d" * 64),
    ],
)
def test_apply_refuses_ineligible_review_before_configuration(change: Callable[[dict[str, Any]], object]) -> None:
    code, _, calls, settings = invoke("apply", edit_evidence=change)
    assert code != 0 and calls == [] and settings == 0


def test_apply_requires_acknowledgement_and_same_service() -> None:
    for kwargs in ({"reviewed": False}, {"service": "https://different.example.invalid"}, {"idempotency_key": " "}):
        code, _, calls, _ = invoke("apply", **kwargs)
        assert code != 0 and calls == []


@pytest.mark.parametrize(
    "mode,updates",
    [
        ("dry-run", {"product": "other"}),
        ("dry-run", {"retained_target_preserved": 1}),
        ("dry-run", {"fresh_storage_required": False}),
        ("dry-run", {"published_host_ports": True}),
        ("dry-run", {"host_ssh_mounts": True}),
        ("dry-run", {"applied": True}),
        ("dry-run", {"plan_sha256": "bad"}),
        ("dry-run", {"foreign": "PRIVATE_SENTINEL"}),
        ("apply", {"plan_sha256": "d" * 64}),
        ("apply", {"resources_verified": False}),
        ("apply", {"resources_binding_sha256": "bad"}),
        ("apply", {"schedule_id": ""}),
        ("readback", {"instance": "prod"}),
        ("readback", {"production_resources": "isolated"}),
        ("readback", {"restore_started": True}),
        ("readback", {"binding_sha256": "bad"}),
    ],
)
def test_resource_receipts_fail_closed(mode: str, updates: dict[str, Any]) -> None:
    code, output, calls, _ = invoke(mode, responses=[receipt(mode, **updates)])
    assert code != 0 and len(calls) == 1
    assert output["status"] == "outcome_unknown" and output["result"] == {}
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize(
    "response",
    [
        receipt("readback", resources_binding_sha256="d" * 64),
        receipt("readback", restore_started=True),
        TimeoutError("PRIVATE_SENTINEL"),
    ],
)
def test_apply_with_unverified_readback_preserves_original_recovery(response: dict[str, Any] | Exception) -> None:
    code, output, calls, _ = invoke("apply", responses=[receipt("apply"), response])
    assert code != 0 and len(calls) == 2
    assert output["status"] == "accepted_unverified"
    assert output["result"]["resource_readback_matches"] is False
    if isinstance(response, dict):
        assert output["summary"]["read_back_trace_id"] == response["trace_id"]
        assert output["summary"]["trace_id"] != response["trace_id"]


@pytest.mark.parametrize(
    "mode,recommendation",
    [("dry-run", resources.PLAN_RECOVERY), ("apply", resources.RECOVERY), ("readback", resources.READBACK_RECOVERY)],
)
def test_no_automatic_retries_after_lost_resource_receipt(mode: str, recommendation: str) -> None:
    code, output, calls, _ = invoke(mode, responses=[TimeoutError("PRIVATE_SENTINEL")])
    assert code != 0 and len(calls) == 1 and output["status"] == "outcome_unknown"
    assert "PRIVATE_SENTINEL" not in json.dumps(output)
    assert output["warnings"][0]["code"] == output["status"]
    assert output["summary"]["recommendation"] == recommendation


@pytest.mark.parametrize("source_mode", ["ssh", "container-export"])
def test_resource_source_mode_is_normalized_for_the_producer(source_mode: str) -> None:
    code, _, calls, _ = invoke(body=private_request(source_mode=source_mode))
    assert code == 0
    assert calls[0].kwargs["body"].get("source_mode", "ssh") == source_mode
    assert ("source_mode" in calls[0].kwargs["body"]) is (source_mode != "ssh")


@pytest.mark.parametrize("status", [408, 503])
def test_resource_service_errors_preserve_diagnostics_without_replay(status: int) -> None:
    response = {
        "trace_id": "launchplane_req_refused",
        "error": {"code": "testing_resources_unverified", "message": "PRIVATE_SENTINEL"},
    }
    error = urllib.error.HTTPError(SERVICE, status, "PRIVATE_SENTINEL", None, io.BytesIO(json.dumps(response).encode()))
    code, output, calls, _ = invoke("apply", responses=[error])
    assert code != 0 and len(calls) == 1
    assert output["summary"]["trace_id"] == response["trace_id"]
    assert output["summary"]["error_code"] == response["error"]["code"]
    assert "PRIVATE_SENTINEL" not in json.dumps(output)
    assert output["status"] == "outcome_unknown"


@pytest.mark.parametrize("field", ["product", "context", "operation_id"])
def test_secret_shaped_local_identifiers_are_local_input_errors(field: str) -> None:
    code, output, calls, settings = invoke(body=private_request(**{field: "ghp_" + "a" * 36}))
    assert code != 0 and calls == [] and settings == 0
    assert output["warnings"][0]["code"] == "invalid_testing_resources_" + field


@pytest.mark.parametrize("mode", ["dry-run", "apply", "readback"])
@pytest.mark.parametrize(
    "status,error_code", [(401, "unauthorized"), (403, "authorization_denied"), (409, "testing_resources_unverified")]
)
def test_failure_classification_and_recovery_cover_every_mode(mode: str, status: int, error_code: str) -> None:
    payload = {"trace_id": "launchplane_req_matrix", "error": {"code": error_code, "message": "PRIVATE_SENTINEL"}}
    error = urllib.error.HTTPError(SERVICE, status, "PRIVATE_SENTINEL", None, io.BytesIO(json.dumps(payload).encode()))
    code, output, calls, _ = invoke(mode, responses=[error])
    assert code != 0 and len(calls) == 1
    if status in {401, 403}:
        assert output["summary"]["recommendation"].startswith(h.http_error_recommendation(output["status"]))
        if mode == "apply":
            assert resources.APPLY_DENIAL_PRESERVATION in output["summary"]["recommendation"]
    else:
        assert output["status"] == "outcome_unknown"
        expected = {"dry-run": resources.PLAN_RECOVERY, "apply": resources.RECOVERY, "readback": resources.READBACK_RECOVERY}
        assert output["summary"]["recommendation"] == expected[mode]


@pytest.mark.parametrize("mode,expected", [("dry-run", "stale"), ("apply", "stale"), ("readback", "outcome_unknown")])
def test_clean_refusal_is_distinct_from_uncertain_allocation(mode: str, expected: str) -> None:
    payload = {"error": {"code": "testing_resources_refused"}, "trace_id": "launchplane_req_clean"}
    error = urllib.error.HTTPError(SERVICE, 409, "refused", None, io.BytesIO(json.dumps(payload).encode()))
    code, output, calls, _ = invoke(mode, responses=[error])
    assert code != 0 and len(calls) == 1 and output["status"] == expected
    expected_recommendation = resources.READBACK_RECOVERY if mode == "readback" else resources.CLEAN_REFUSAL_RECOVERY
    assert output["summary"]["recommendation"] == expected_recommendation


def test_missing_planning_record_points_to_the_private_request() -> None:
    payload = {"error": {"code": "not_found"}}
    error = urllib.error.HTTPError(SERVICE, 404, "missing", None, io.BytesIO(json.dumps(payload).encode()))
    code, output, calls, _ = invoke(responses=[error])
    assert code != 0 and len(calls) == 1
    assert output["summary"]["recommendation"] == resources.MISSING_RECORD_RECOVERY


@pytest.mark.parametrize("mode", ["dry-run", "apply", "readback"])
def test_unparseable_conflict_never_establishes_a_clean_refusal(mode: str) -> None:
    error = urllib.error.HTTPError(SERVICE, 409, "refused", None, io.BytesIO(b"PRIVATE_SENTINEL"))
    code, output, calls, _ = invoke(mode, responses=[error])
    assert code != 0 and len(calls) == 1 and output["status"] == "outcome_unknown"
    assert output["summary"]["http_status"] == 409


@pytest.mark.parametrize("error_code", ["mutation_in_progress", "mutation_reconciliation_required", "idempotency_key_reused"])
def test_original_operation_conflicts_preserve_uncertain_apply_recovery(error_code: str) -> None:
    payload = {"error": {"code": error_code}}
    error = urllib.error.HTTPError(SERVICE, 409, "conflict", None, io.BytesIO(json.dumps(payload).encode()))
    code, output, calls, _ = invoke("apply", responses=[error])
    assert code != 0 and len(calls) == 1 and output["status"] == "outcome_unknown"
    assert output["summary"]["recommendation"] == resources.RECOVERY


def test_accepted_apply_then_denied_readback_retains_the_denial_boundary() -> None:
    payload = {"error": {"code": "authorization_denied"}, "trace_id": "launchplane_req_read_denied"}
    error = urllib.error.HTTPError(SERVICE, 403, "denied", None, io.BytesIO(json.dumps(payload).encode()))
    code, output, calls, _ = invoke("apply", responses=[receipt("apply"), error])
    assert code != 0 and len(calls) == 2 and output["status"] == "accepted_unverified"
    assert output["result"]["resource_readback_matches"] is False
    assert output["summary"]["read_back_trace_id"] == payload["trace_id"]
    assert h.http_error_recommendation("denied") in output["summary"]["recommendation"]


def release_receipt(**updates: object) -> dict[str, Any]:
    return {
        "status": "accepted",
        "trace_id": "launchplane_req_release",
        "result": {
            "product": "example",
            "context": "example-testing",
            "instance": "testing",
            "operation_id": "retained-fixture",
            "released": True,
            "provider_effects_started": False,
            "retained_target_preserved": True,
            **updates,
        },
    }


@pytest.mark.parametrize("mode", ["dry-run", "apply", "readback", "release"])
def test_initial_modes_keep_original_key_and_private_operation(mode: str) -> None:
    responses = [release_receipt()] if mode == "release" else None
    code, output, calls, _ = invoke(mode, initial=True, responses=responses)
    assert code == 0, output
    assert all(call.kwargs["idempotency_key"] == "original-key" for call in calls)
    assert all(call.kwargs["body"]["initial_generation"] is True for call in calls)
    assert all(call.kwargs["body"]["operation_id"] == "" for call in calls)
    assert "retained-fixture" not in json.dumps(output)
    if mode == "release":
        assert len(calls) == 1
        assert calls[0].kwargs["body"]["reviewed_plan_sha256"] == PLAN
        assert output["result"]["released"] is True
        assert output["result"]["resources_verified"] is False


@pytest.mark.parametrize("mode", ["dry-run", "apply", "readback", "release"])
def test_initial_modes_require_original_key(mode: str) -> None:
    code, _, calls, _ = invoke(mode, initial=True, idempotency_key=" ")
    assert code != 0 and calls == []


@pytest.mark.parametrize("mode", ["apply", "release"])
def test_initial_mutations_bind_key_and_reviewed_operation(mode: str) -> None:
    code, _, calls, _ = invoke(mode, initial=True, idempotency_key="different-key")
    assert code != 0 and calls == []
    response = release_receipt(operation_id="other") if mode == "release" else receipt("apply", operation_id="other")
    code, output, calls, _ = invoke(mode, initial=True, responses=[response])
    assert code != 0 and len(calls) == 1 and output["status"] == "outcome_unknown"


@pytest.mark.parametrize(
    "updates",
    [
        {"provider_effects_started": True},
        {"released": False},
        {"retained_target_preserved": False},
        {"context": "other"},
        {"foreign": "PRIVATE_SENTINEL"},
    ],
)
def test_release_receipt_never_hides_effects_or_scope_drift(updates: dict[str, Any]) -> None:
    code, output, calls, _ = invoke("release", initial=True, responses=[release_receipt(**updates)])
    assert code != 0 and len(calls) == 1 and output["status"] == "outcome_unknown"
    assert output["result"] == {}


def test_release_requires_initial_intent() -> None:
    code, _, calls, _ = invoke("release")
    assert code != 0 and calls == []


def test_initial_apply_released_intent_is_not_allocation_success() -> None:
    code, output, calls, _ = invoke("apply", initial=True, responses=[release_receipt()])
    assert code != 0 and len(calls) == 1
    assert output["status"] == "released"
    assert output["result"]["resources_verified"] is False


@pytest.mark.parametrize("mode", ["apply", "release"])
def test_initial_lost_mutation_never_replays(mode: str) -> None:
    code, output, calls, _ = invoke(mode, initial=True, responses=[TimeoutError("PRIVATE_SENTINEL")])
    assert code != 0 and len(calls) == 1 and output["status"] == "outcome_unknown"
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
