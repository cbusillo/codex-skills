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
import pytest
from test_launchplane_helpers import write_action as h

PREFIX = "testing-replacement-resources-"
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
    return {"status": "accepted", "trace_id": "launchplane_req_resources", "records": {}, "result": {**result, **updates}}


def invoke(
    mode: str = "dry-run",
    *,
    body: dict[str, Any] | None = None,
    responses: list[dict[str, Any] | Exception] | None = None,
    edit_evidence: Callable[[dict[str, Any]], object] | None = None,
    service: str = SERVICE,
    reviewed: bool = True,
    idempotency_key: str = "original-key",
) -> Invocation:
    output = io.StringIO()
    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "repo").mkdir()
        payload, evidence_path = root / "request.json", root / "review.json"
        payload.write_text(json.dumps(private_request()))
        with (
            patch.object(h, "active_repo_root", return_value=root / "repo"),
            patch.object(h, "prepare_operator_settings", return_value={"service_url": SERVICE}) as settings,
            patch.object(h, "request_launchplane", return_value=receipt("dry-run")) as post,
            redirect_stdout(output),
        ):
            if mode == "apply":
                assert h.main([PREFIX + "dry-run", "--payload-file", str(payload)]) == 0, output.getvalue()
                evidence = json.loads(output.getvalue())
                if edit_evidence:
                    edit_evidence(evidence)
                evidence_path.write_text(json.dumps(evidence))
                post.reset_mock()
                settings.reset_mock()
                output.seek(0)
                output.truncate()
            payload.write_text(json.dumps(private_request() if body is None else body))
            settings.return_value = {"service_url": service}
            post.side_effect = responses if responses is not None else [receipt(mode), receipt("readback")]
            argv = [PREFIX + mode, "--payload-file", str(payload)]
            if mode == "apply":
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
        assert call.kwargs["timeout"] == 600
    serialized = json.dumps(output)
    assert all(value not in serialized for value in ("PRIVATE_SENTINEL", "PRIVATE_SCHEDULE", "retained-fixture", "example-testing"))
    assert output["result"]["restore_started"] is False
    if mode == "apply":
        assert output["result"]["resource_readback_matches"] is True


@pytest.mark.parametrize(
    "updates",
    [
        {"instance": "prod"},
        {"preview_id": "preview-1"},
        {"operation_id": ""},
        {"operation_id": "../escape"},
        {"product": "../escape"},
        {"context": None},
        {"mode": "apply"},
        {"reviewed_plan_sha256": PLAN},
        {"reason": " "},
        {"initial_generation": True},
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


@pytest.mark.parametrize("response", [receipt("readback", resources_binding_sha256="d" * 64), TimeoutError("PRIVATE_SENTINEL")])
def test_apply_with_unverified_readback_preserves_original_recovery(response: dict[str, Any] | Exception) -> None:
    code, output, calls, _ = invoke("apply", responses=[receipt("apply"), response])
    assert code != 0 and len(calls) == 2
    assert output["status"] == "accepted_unverified"
    assert output["result"]["resource_readback_matches"] is False
    assert "original" in output["summary"]["recommendation"]


@pytest.mark.parametrize("mode", ["dry-run", "apply", "readback"])
def test_no_automatic_retries_after_lost_resource_receipt(mode: str) -> None:
    code, output, calls, _ = invoke(mode, responses=[TimeoutError("PRIVATE_SENTINEL")])
    assert code != 0 and len(calls) == 1 and output["status"] == "outcome_unknown"
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize("source_mode", ["ssh", "container-export"])
def test_resource_source_mode_is_normalized_for_the_producer(source_mode: str) -> None:
    code, _, calls, _ = invoke(body=private_request(source_mode=source_mode))
    assert code == 0
    assert calls[0].kwargs["body"].get("source_mode", "ssh") == source_mode
    assert ("source_mode" in calls[0].kwargs["body"]) is (source_mode != "ssh")


@pytest.mark.parametrize("status", [403, 409, 408, 503])
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
    assert "original" in output["summary"]["recommendation"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
