# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.2.0"]
# ///

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from test_launchplane_inventory_readback import invoke as invoke_private


def intent() -> dict[str, Any]:
    return {
        "product": "example",
        "pr_number": 42,
        "source_git_ref": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "original_reservation_id": "mutation-reservation-" + "c" * 64,
        "preview_id": "preview-fictional",
        "generation_id": "generation-fictional",
        "target_id": "target-fictional",
        "mode": "container_observation",
    }


def response() -> dict[str, Any]:
    request = intent()
    return {
        "status": "ok",
        "trace_id": "req_observation_fictional",
        "result": {
            "mode": "container_observation",
            "read_only": True,
            "observed_at": "2026-11-11T12:00:00Z",
            "binding": {key: value for key, value in request.items() if key != "mode"} | {"artifact_id": "artifact-fictional"},
            "current_binding": "current",
            "observation": {
                "inventory_received_at": "2026-11-11T12:00:00Z",
                "inspection_attempted_at": "2026-11-11T12:00:00Z",
                "inspection_received_at": None,
                "container_id": "d" * 64,
                "selection": "unique",
                "inventory_coverage": "complete",
                "exists": True,
                "existence_coverage": "observed",
                "inspection_coverage": "unknown",
                "config_valid": None,
                "state_valid": None,
                "running": None,
                "running_coverage": "unknown",
                "runtime_identity": "not_reached",
                "image_agreement": "not_reached",
                "stage": "inspection",
            },
            "runtime_verification": "not_performed",
            "import_eligibility": "not_evaluated",
        },
    }


def invoke(tmp_path: Path, **kwargs: Any) -> Any:
    return invoke_private(
        tmp_path,
        command="preview-container-observation",
        request=kwargs.pop("request", intent()),
        returned=kwargs.pop("returned", response()),
        **kwargs,
    )


def test_failed_inspection_preserves_partial_facts_and_exact_private_request(tmp_path: Path) -> None:
    code, public, transport, destination = invoke(tmp_path)
    assert code == 0
    request = transport.call_args.args[0]
    assert request.method == "GET"
    assert urlsplit(request.full_url).path == "/v1/products/example/preview-evidence"
    assert parse_qs(urlsplit(request.full_url).query) == {key: [str(value)] for key, value in intent().items() if key != "product"}
    assert request.get_header("Cache-control") == "no-store"
    assert json.loads(destination.read_text()) == response()
    assert public["summary"]["trace_id"] == response()["trace_id"]
    assert public["result"] == {
        "private_result_saved": True,
        "runtime_verification": "not_performed",
        "import_eligibility": "not_evaluated",
    }
    assert "container_id" not in json.dumps(public)


@pytest.mark.parametrize("running", [False, None, True])
@pytest.mark.parametrize("current", ["current", "changed", "unavailable"])
def test_running_and_drift_never_upgrade_verification(tmp_path: Path, running: bool | None, current: str) -> None:
    returned = response()
    returned["result"]["current_binding"] = current
    returned["result"]["observation"].update(running=running, runtime_identity="verified", image_agreement="verified")
    code, public, _, destination = invoke(tmp_path, returned=returned)
    assert code == 0
    assert json.loads(destination.read_text()) == returned
    assert public["result"]["runtime_verification"] == "not_performed"
    assert public["result"]["import_eligibility"] == "not_evaluated"


@pytest.mark.parametrize("selection", ["unknown", "missing_or_unavailable", "missing", "ambiguous"])
@pytest.mark.parametrize("coverage", ["unknown", "invalid", "complete"])
def test_inventory_outcomes_preserve_unknowns_without_inventing_absence(tmp_path: Path, selection: str, coverage: str) -> None:
    returned = response()
    returned["result"]["observation"] = {"selection": selection, "inventory_coverage": coverage, "exists": None}
    code, _, _, destination = invoke(tmp_path, returned=returned)
    assert code == 0
    assert json.loads(destination.read_text()) == returned


def test_controller_plan_selector_is_not_replaced_with_resolved_original(tmp_path: Path) -> None:
    request = intent()
    request.pop("original_reservation_id")
    request["plan_id"] = "odoo-preview-plan-fictional"
    code, _, transport, destination = invoke(tmp_path, request=request)
    assert code == 0
    query = parse_qs(urlsplit(transport.call_args.args[0].full_url).query)
    assert query["plan_id"] == [request["plan_id"]]
    assert "original_reservation_id" not in query
    assert json.loads(destination.read_text()) == response()


@pytest.mark.parametrize(
    "field,value",
    [
        ("pr_number", True),
        ("pr_number", 0),
        ("source_git_ref", "main"),
        ("image_digest", "latest"),
        ("mode", "qualification"),
        ("target_id", ""),
        ("target_id", "x/y"),
        ("preview_id", ""),
        ("generation_id", ""),
        ("original_reservation_id", ""),
        ("plan_id", "odoo-preview-plan-extra"),
        ("program", "arbitrary"),
        ("url", "https://example.invalid"),
    ],
)
def test_invalid_selectors_never_reach_transport(tmp_path: Path, field: str, value: object) -> None:
    request = intent() | {field: value}
    code, _, transport, destination = invoke(tmp_path, request=request)
    assert code != 0
    transport.assert_not_called()
    assert not destination.exists()


@pytest.mark.parametrize("location", ["envelope", "result", "binding", "observation"])
def test_unknown_fields_and_provider_bodies_are_rejected_privately(tmp_path: Path, location: str) -> None:
    returned = response()
    target = returned if location == "envelope" else returned["result"] if location == "result" else returned["result"][location]
    target["Config"] = {"Env": ["PRIVATE_PASSWORD=PRIVATE_VALUE"]}
    code, _, _, destination = invoke(tmp_path, returned=returned)
    assert code != 0
    assert not destination.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("running", 0),
        ("exists", "false"),
        ("config_valid", 1),
        ("state_valid", {}),
        ("container_id", "PRIVATE_RAW_ERROR"),
        ("selection", []),
        ("runtime_identity", "ok"),
        ("inventory_received_at", "PRIVATE_RAW_ERROR"),
    ],
)
def test_invalid_observations_never_become_saved_evidence(tmp_path: Path, field: str, value: object) -> None:
    returned = response()
    returned["result"]["observation"][field] = value
    code, _, _, destination = invoke(tmp_path, returned=returned)
    assert code != 0
    assert not destination.exists()


@pytest.mark.parametrize(
    "field",
    [
        "product",
        "pr_number",
        "source_git_ref",
        "image_digest",
        "preview_id",
        "generation_id",
        "target_id",
        "original_reservation_id",
    ],
)
def test_cross_binding_response_is_rejected(tmp_path: Path, field: str) -> None:
    returned = response()
    returned["result"]["binding"][field] = 43 if field == "pr_number" else "other"
    code, _, _, destination = invoke(tmp_path, returned=returned)
    assert code != 0
    assert not destination.exists()


@pytest.mark.parametrize("cache_control", ["", "private", "no-store-extra", "max-age=10"])
def test_cached_response_cannot_be_saved(tmp_path: Path, cache_control: str) -> None:
    code, _, _, destination = invoke(tmp_path, cache_control=cache_control)
    assert code != 0
    assert not destination.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("runtime_verification", "verified"),
        ("import_eligibility", "eligible"),
        ("mode", "qualification"),
        ("read_only", 1),
        ("current_binding", "verified"),
    ],
)
def test_aggregate_claims_are_never_accepted(tmp_path: Path, field: str, value: object) -> None:
    returned = response()
    returned["result"][field] = value
    code, _, _, destination = invoke(tmp_path, returned=returned)
    assert code != 0
    assert not destination.exists()


def test_missing_optional_fields_stay_missing(tmp_path: Path) -> None:
    returned = response()
    returned.pop("status")
    returned["result"] = {
        key: value
        for key, value in returned["result"].items()
        if key in {"observed_at", "binding", "current_binding", "observation"}
    }
    returned["result"]["observation"] = {}
    code, _, _, destination = invoke(tmp_path, returned=returned)
    assert code == 0
    assert json.loads(destination.read_text()) == returned


@pytest.mark.parametrize("field", ["observed_at", "binding", "current_binding", "observation"])
def test_missing_required_result_fields_are_rejected(tmp_path: Path, field: str) -> None:
    returned = response()
    del returned["result"][field]
    code, _, _, destination = invoke(tmp_path, returned=returned)
    assert code != 0
    assert not destination.exists()


@pytest.mark.parametrize("status", [401, 403, 409, 503])
def test_http_failure_retains_safe_trace_without_raw_error(tmp_path: Path, status: int) -> None:
    import io
    import urllib.error
    from email.message import Message

    body = {
        "status": "error",
        "trace_id": "req_failure_fictional",
        "error": {"code": "provider_unavailable", "message": "PRIVATE_RAW_ERROR"},
    }
    headers = Message()
    headers["X-Trace-Id"] = body["trace_id"]
    error = urllib.error.HTTPError(
        "https://plane.example.invalid", status, "PRIVATE_REASON", headers, io.BytesIO(json.dumps(body).encode())
    )
    code, public, _, destination = invoke(tmp_path, error=error)
    assert code != 0
    assert public["summary"]["trace_id"] == body["trace_id"]
    assert not destination.exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
