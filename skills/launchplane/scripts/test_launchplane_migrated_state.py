# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.2.0", "websockets==17.4"]
# ///
from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import Mock

import launchplane_migrated_upload as upload
import pytest
from test_launchplane_helpers import write_action as helper
from test_launchplane_migrated_upload import fixture, patch_attr, state


class ReadResult(NamedTuple):
    code: int
    output: dict[str, Any]
    connection: Mock


def invoke(tmp_path: Path, *, prerequisites: object = None) -> ReadResult:
    args, data, manifest = fixture(tmp_path)
    for part in data:
        getattr(args, part).unlink()
    response = state(manifest, data, {"database": 4})
    if prerequisites is not None:
        response["prerequisites"] = prerequisites
    return invoke_response(args, response)


def invoke_response(
    args: argparse.Namespace,
    response: object,
    *,
    status: int = 200,
    transport_error: OSError | None = None,
) -> ReadResult:
    connection = Mock()
    connection.getresponse.return_value = Mock(status=status, read=Mock(return_value=json.dumps(response).encode()))
    connection.getresponse.side_effect = transport_error
    output = io.StringIO()
    with (
        patch_attr(upload, "PrivateHTTPSConnection", return_value=connection),
        patch_attr(
            helper, "prepare_operator_settings", return_value={"service_url": "https://plane.example.invalid", "token": "fixture"}
        ),
        patch_attr(upload, "private_send", side_effect=AssertionError("read attempted an upload")),
        patch_attr(upload, "execute", side_effect=AssertionError("read entered the uploader")),
        patch_attr(helper, "request_launchplane", side_effect=AssertionError("read attempted a mutation")),
        redirect_stdout(output),
    ):
        code = helper.main(
            [
                "testing-rebuild-migrated-state-read",
                "--product",
                args.product,
                "--operation-id",
                args.operation_id,
                "--private-address",
                args.private_address,
                "--migrated-source-file",
                str(args.migrated_source_file),
            ]
        )
    connection.close.assert_called_once_with()
    connection.request.assert_called_once_with(
        "GET",
        "/v1/products/example/testing-rebuild/original/migrated-source?include_prerequisites=true",
        headers={"Accept": "application/json", "Authorization": "Bearer fixture"},
    )
    return ReadResult(code, json.loads(output.getvalue()), connection)


def test_partial_original_read_preserves_unknown_start_without_opening_archives(tmp_path: Path) -> None:
    prerequisites = {"candidate_phase": "restoring", "restore_workflow_started": None, "holds": []}
    code, output, _ = invoke(tmp_path, prerequisites=prerequisites)
    assert code == 0
    assert output["status"] == "ok"
    assert output["result"]["prerequisites"] == prerequisites
    assert output["result"]["parts"] == {
        "database": {"complete": False, "size": 4},
        "filestore": {"complete": False, "size": 0},
    }
    for field in ("product", "operation_id", "source_binding_sha256"):
        assert output["result"][field] == output["request"][field]
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_older_producer_omission_returns_unavailable_coverage_with_retained_metadata(tmp_path: Path) -> None:
    code, output, _ = invoke(tmp_path)
    assert code == 1
    assert output["status"] == "unavailable"
    assert output["result"]["prerequisite_coverage"] == "unavailable"
    assert "prerequisites" not in output["result"]
    assert output["result"]["parts"]["database"]["size"] == 4


@pytest.mark.parametrize("receipt", [False, True])
def test_complete_pair_preserves_simultaneous_recorded_holds_with_or_without_receipt(tmp_path: Path, receipt: bool) -> None:
    args, data, manifest = fixture(tmp_path)
    response = state(manifest, data, {part: len(value) for part, value in data.items()}, receipt=receipt)
    prerequisites = {
        "candidate_phase": "abandoned",
        "restore_workflow_started": True,
        "holds": ["candidate_abandoned", "restore_started", "original_caller_required"],
    }
    response["prerequisites"] = prerequisites
    response["private_details"] = {"operator": "PRIVATE_SENTINEL", "path": "/private/PRIVATE_SENTINEL"}
    code, output, _ = invoke_response(args, response)
    assert code == 0
    result = output["result"]
    assert result["receipt_recorded"] is receipt
    assert result["prerequisites"] == prerequisites
    assert result["parts"] == {part: {"complete": True, "size": len(value)} for part, value in data.items()}
    assert "PRIVATE_SENTINEL" not in json.dumps(output)
    assert not ({"eligible", "safe_to_release", "no_effects", "retry_allowed"} & result.keys())


def test_empty_holds_preserves_explicit_false_without_creating_upload_eligibility(tmp_path: Path) -> None:
    prerequisites = {"candidate_phase": "configuring", "restore_workflow_started": False, "holds": []}
    code, output, _ = invoke(tmp_path, prerequisites=prerequisites)
    assert code == 0
    assert output["result"]["prerequisites"] == prerequisites
    assert output["result"]["prerequisite_coverage"] == "recorded_candidate_and_scope"
    assert "eligible" not in output["result"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("product", "foreign"), ("operation_id", "replacement"), ("source_binding_sha256", "f" * 64)],
)
def test_foreign_original_or_source_cannot_become_a_successful_disposition(tmp_path: Path, field: str, value: str) -> None:
    args, data, manifest = fixture(tmp_path)
    response = state(manifest, data)
    response[field] = value
    response["prerequisites"] = {"candidate_phase": "planned", "restore_workflow_started": False, "holds": []}
    code, output, _ = invoke_response(args, response)
    assert code == 2
    assert output["status"] == "refused"
    assert output["result"] == {}
    assert output["warnings"][0]["code"] == "migrated_upload_binding_changed"


@pytest.mark.parametrize(
    "prerequisites",
    [
        None,
        {},
        [],
        {"candidate_phase": "prepared", "restore_workflow_started": False},
        {"candidate_phase": "unknown", "restore_workflow_started": False, "holds": []},
        {"candidate_phase": {}, "restore_workflow_started": False, "holds": []},
        {"candidate_phase": "planned", "restore_workflow_started": 0, "holds": []},
        {"candidate_phase": "planned", "restore_workflow_started": "unknown", "holds": []},
        {"candidate_phase": "planned", "restore_workflow_started": False, "holds": None},
        {"candidate_phase": "planned", "restore_workflow_started": False, "holds": ["PRIVATE_SENTINEL"]},
        {"candidate_phase": "planned", "restore_workflow_started": False, "holds": [{}]},
        {"candidate_phase": "planned", "restore_workflow_started": False, "holds": [], "original_caller": True},
    ],
)
def test_malformed_or_draft_prerequisites_are_unavailable_not_cleared(tmp_path: Path, prerequisites: object) -> None:
    args, data, manifest = fixture(tmp_path)
    response = state(manifest, data)
    response["prerequisites"] = prerequisites
    code, output, _ = invoke_response(args, response)
    assert code == 2
    assert output["status"] == "refused"
    assert output["result"] == {}
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize(
    "parts",
    [
        None,
        {},
        {"database": {"complete": False, "size": 0}},
        {"database": {"complete": "false", "size": 0}, "filestore": {"complete": False, "size": 0}},
        {"database": {"complete": False, "size": True}, "filestore": {"complete": False, "size": 0}},
        {"database": {"complete": False, "size": -1}, "filestore": {"complete": False, "size": 0}},
        {"database": {"complete": True, "size": 0}, "filestore": {"complete": False, "size": 0}},
    ],
)
def test_invalid_retained_part_evidence_is_not_projected(tmp_path: Path, parts: object) -> None:
    args, data, manifest = fixture(tmp_path)
    response = state(manifest, data)
    response["parts"] = parts
    code, output, _ = invoke_response(args, response)
    assert code == 2
    assert output["result"] == {}


@pytest.mark.parametrize("receipt", [None, "false", True])
def test_missing_or_inconsistent_receipt_evidence_is_not_projected(tmp_path: Path, receipt: object) -> None:
    args, data, manifest = fixture(tmp_path)
    response = state(manifest, data)
    if receipt is None:
        del response["receipt_recorded"]
    else:
        response["receipt_recorded"] = receipt
    code, output, _ = invoke_response(args, response)
    assert code == 2
    assert output["result"] == {}


@pytest.mark.parametrize("status", [403, 409, 503])
def test_current_service_refusal_keeps_its_explanation_without_upload_fallback(tmp_path: Path, status: int) -> None:
    args, _, _ = fixture(tmp_path)
    response = {
        "error": {"code": "migrated_source_upload_incomplete", "message": "The original source cannot be read."},
        "trace_id": "trace-fixture",
    }
    code, output, _ = invoke_response(args, response, status=status)
    assert code == (1 if status == 503 else 2)
    assert output["status"] == ("unavailable" if status == 503 else "refused")
    assert output["result"] == {}
    assert output["summary"]["error_code"] == response["error"]["code"]
    assert output["summary"]["error_message"] == response["error"]["message"]
    assert output["summary"]["http_status"] == status


def test_unavailable_transport_keeps_disposition_unknown_without_retry_or_mutation(tmp_path: Path) -> None:
    args, _, _ = fixture(tmp_path)
    code, output, _ = invoke_response(args, {}, transport_error=OSError("PRIVATE_SENTINEL"))
    assert code == 1
    assert output["status"] == "unavailable"
    assert output["result"] == {}
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
