# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.2.0"]
# ///

import copy
import io
import json
import urllib.error
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import launchplane_testing_state as testing_state
import pytest
from test_launchplane_helpers import _product_status_response, _run_product_read
from test_launchplane_helpers import write_action as helper


def response() -> dict[str, Any]:
    payload = _product_status_response()
    payload["products"][0]["preview"] = {
        "context": "example-preview",
        "records_status": "available",
        "inventory": {
            "status": "nonempty",
            "observed_count": 1,
            "observed_slugs": ["pr-42"],
            "provenance": {
                "source_kind": "record",
                "source_record_id": "scan-example",
                "recorded_at": "2026-11-11T12:00:00Z",
                "refreshed_at": "2026-11-11T12:00:00Z",
                "freshness_status": "verified",
                "stale_after": "2026-11-11T12:30:00Z",
                "detail": "PRIVATE_PROSE",
            },
            "sleep_observations": [
                {
                    "slug": "pr-42",
                    "state": "asleep",
                    "reason": "PRIVATE_PROSE",
                    "evidence": {
                        "coverage": "complete",
                        "completed_at": "2026-11-11T12:00:05Z",
                        "freshness": "fresh",
                        "binding": {
                            "product": "example-product",
                            "context": "example-preview",
                            "instance": "pr-42",
                            "preview_id": "observed-preview",
                            "generation_id": "observed-generation",
                            "provider_target_id": "observed-target",
                            "artifact_id": "observed-artifact",
                            "source_git_ref": "observed-ref",
                            "image_reference": "registry.example.invalid/site:observed",
                        },
                        "gateway_mode": "asleep",
                        "activity_requested": False,
                        "active_connections": 0,
                        "services": {
                            "web": {"running": False, "healthy": None},
                            "database": {"running": False, "healthy": False},
                            "script_runner": {"running": False, "healthy": None},
                        },
                        "receipts": {
                            "sleep_present": True,
                            "wake_present": False,
                            "persisted_state": "observed",
                            "identity_agrees": True,
                            "mode_agrees": True,
                            "sleep_agrees": True,
                            "wake_agrees": None,
                        },
                        "phase": "asleep",
                        "current_binding": "current",
                    },
                }
            ],
        },
    }
    return payload


def inventory(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["products"][0]["preview"]["inventory"]


def test_default_does_not_request_or_expose_private_rows(tmp_path: Path) -> None:
    source = response()
    code, ordinary, calls = _run_product_read(["product-status-read"], source)
    assert code == 0 and calls[0]["query"] == {} and "no_store" not in calls[0]
    assert "preview_sleep_evidence" not in json.dumps(ordinary)
    destination = tmp_path / "private.json"
    code, opted, calls = _run_product_read(["product-status-read", "--preview-sleep-evidence-file", str(destination)], source)
    assert code == 0 and len(calls) == 1
    assert calls[0]["query"] == {"include_preview_sleep_evidence": "true"} and calls[0]["no_store"] is True
    assert opted["result"] == ordinary["result"] and opted["summary"] == ordinary["summary"]
    saved = json.loads(destination.read_text())
    private = saved["products"][0]["preview_sleep_evidence"]
    expected = copy.deepcopy(source["products"][0]["preview"])
    expected["inventory"]["provenance"].pop("detail")
    expected["inventory"]["sleep_observations"][0].pop("reason")
    assert private == expected
    assert set(saved["products"][0]) == {"product", "preview_sleep_evidence"}
    assert destination.stat().st_mode & 0o777 == 0o600
    assert "observed-generation" not in json.dumps(opted) and "PRIVATE_PROSE" not in destination.read_text()


@pytest.mark.parametrize("state", ["partial", "unavailable", "not_attempted", "legacy", "changed", "unknown", "stale", "mismatch"])
def test_private_read_preserves_unqualified_observation_without_rebinding(tmp_path: Path, state: str) -> None:
    source = response()
    evidence = inventory(source)["sleep_observations"][0]["evidence"]
    if state in {"partial", "unavailable", "not_attempted", "legacy"}:
        evidence["coverage"] = state
        evidence["binding"] = None if state == "legacy" else evidence["binding"]
        evidence["receipts"]["identity_agrees"] = None
        evidence["services"]["web"]["running"] = None
    elif state == "stale":
        evidence.update(freshness="stale", current_binding="unknown")
    elif state == "mismatch":
        evidence["receipts"]["identity_agrees"] = False
    else:
        evidence["current_binding"] = state
        source["products"][0]["preview"]["records"] = [{"preview_id": "new-current-preview"}]
    destination = tmp_path / "private.json"
    code, public, _ = _run_product_read(["product-status-read", "--preview-sleep-evidence-file", str(destination)], source)
    assert code == 0
    observed = json.loads(destination.read_text())["products"][0]["preview_sleep_evidence"]["inventory"]["sleep_observations"][0]
    assert observed["evidence"] == evidence
    if state in {"changed", "unknown"}:
        assert "new-current-preview" not in destination.read_text()
    assert "preview_sleep_evidence" not in json.dumps(public["result"])


@pytest.mark.parametrize(
    "state",
    ["missing", "stale", "failed", "unsupported", "unavailable", "authorization_denied", "legacy", "absent_inventory", "legacy_row"],
)
def test_inventory_failures_and_legacy_are_retained_without_fallback(tmp_path: Path, state: str) -> None:
    source = response()
    preview = source["products"][0]["preview"]
    if state == "legacy":
        source["products"][0].pop("preview")
    elif state == "absent_inventory":
        preview["inventory"] = None
    elif state == "legacy_row":
        inventory(source)["sleep_observations"][0].pop("evidence")
    else:
        inventory(source)["sleep_observations"] = []
        inventory(source)["observed_slugs"] = []
        if state == "authorization_denied":
            preview["records_status"] = state
        else:
            inventory(source)["status"] = state
            inventory(source)["provenance"]["freshness_status"] = (
                "stale" if state == "stale" else "unsupported" if state == "unsupported" else "missing"
            )
            inventory(source)["observed_count"] = None
            inventory(source)["provenance"]["source_kind"] = "unsupported" if state == "unsupported" else "record"
    destination = tmp_path / "private.json"
    code, _, calls = _run_product_read(["product-status-read", "--preview-sleep-evidence-file", str(destination)], source)
    assert code == 0 and len(calls) == 1
    saved_product = json.loads(destination.read_text())["products"][0]
    saved = saved_product.get("preview_sleep_evidence")
    if state == "legacy":
        assert "preview_sleep_evidence" not in saved_product
    elif state == "absent_inventory":
        assert saved["inventory"] is None
    elif state == "authorization_denied":
        assert saved["records_status"] == state and saved["inventory"]["sleep_observations"] == []
    elif state == "legacy_row":
        assert "evidence" not in saved["inventory"]["sleep_observations"][0]
    else:
        assert saved["inventory"]["status"] == state


@pytest.mark.parametrize(
    "damage",
    [
        "extra",
        "receipt",
        "state",
        "services",
        "binding",
        "integer_bool",
        "negative",
        "bool_string",
        "phase",
        "timestamp",
        "missing_binding",
        "null_evidence",
        "denied_rows",
    ],
)
def test_malformed_or_sensitive_evidence_never_creates_private_file(tmp_path: Path, damage: str) -> None:
    source = response()
    evidence = inventory(source)["sleep_observations"][0]["evidence"]
    if damage == "extra":
        evidence["environment"] = {"PASSWORD": "PRIVATE_SENTINEL"}
    elif damage == "receipt":
        evidence["receipts"]["receipt_id"] = "PRIVATE_SENTINEL"
    elif damage == "state":
        evidence["services"]["web"]["Config"] = {"State": "PRIVATE_SENTINEL"}
    elif damage == "services":
        evidence["services"]["other"] = {}
    elif damage == "binding":
        evidence["binding"]["control_token"] = "PRIVATE_SENTINEL"
    elif damage == "missing_binding":
        evidence["binding"].pop("generation_id")
    elif damage == "integer_bool":
        evidence["active_connections"] = True
    elif damage == "negative":
        evidence["active_connections"] = -1
    elif damage == "bool_string":
        evidence["activity_requested"] = "false"
    elif damage == "phase":
        evidence["phase"] = "safe_to_wake"
    elif damage == "timestamp":
        evidence["completed_at"] = "x" * 65
    elif damage == "null_evidence":
        inventory(source)["sleep_observations"][0]["evidence"] = None
    else:
        source["products"][0]["preview"]["records_status"] = "authorization_denied"
    destination = tmp_path / "private.json"
    code, public, _ = _run_product_read(["product-status-read", "--preview-sleep-evidence-file", str(destination)], source)
    assert code == 1 and not destination.exists() and "PRIVATE_SENTINEL" not in json.dumps(public)


def test_denied_transport_and_existing_destination_never_replace_private_evidence(tmp_path: Path) -> None:
    destination = tmp_path / "private.json"
    error = urllib.error.HTTPError("https://example.invalid/v1/products", 403, "Denied", Message(), None)
    code, public, calls = _run_product_read(["product-status-read", "--preview-sleep-evidence-file", str(destination)], error)
    assert code == 1 and len(calls) == 1 and not destination.exists()
    assert public["status"] == "denied"
    destination.write_text("original")
    code, _, calls = _run_product_read(["product-status-read", "--preview-sleep-evidence-file", str(destination)], response())
    assert code == 2 and not calls and destination.read_text() == "original"


def test_private_http_read_uses_existing_get_without_activity(tmp_path: Path) -> None:
    destination = tmp_path / "private.json"
    transport_response = MagicMock()
    transport_response.__enter__.return_value = transport_response
    transport_response.read.return_value = json.dumps(response()).encode()
    transport_response.status = 200
    transport_response.headers = Message()
    transport_response.headers["Cache-Control"] = "private, no-store"
    output = io.StringIO()
    with (
        patch.object(helper, "safe_urlopen", return_value=transport_response) as transport,
        patch.object(
            helper, "resolve_settings", return_value={"service_url": "https://service.example.invalid", "token": "fictional"}
        ),
        redirect_stdout(output),
    ):
        code = helper.main(["product-status-read", "--preview-sleep-evidence-file", str(destination)])
    request = transport.call_args.args[0]
    assert transport.call_count == 1 and request.method == "GET" and request.data is None
    assert (
        request.full_url
        == f"https://service.example.invalid{helper.helper_command_path('product-status-read')}?include_preview_sleep_evidence=true"
    )
    assert request.get_header("Cache-control") == "no-store"
    assert destination.exists() and code == 0
    assert "observed-generation" not in output.getvalue()


@pytest.mark.parametrize("value", ["work/task-123", "registry.example.invalid/helpdesk-site:tag", "risk-target", "ghp_plantedtoken"])
def test_observed_identifiers_use_identifier_token_rules(tmp_path: Path, value: str) -> None:
    source = response()
    binding = inventory(source)["sleep_observations"][0]["evidence"]["binding"]
    binding.update({key: value for key in binding})
    destination = tmp_path / "private.json"
    code, _, _ = _run_product_read(["product-status-read", "--preview-sleep-evidence-file", str(destination)], source)
    if value.startswith("ghp_"):
        assert code == 1 and not destination.exists()
    else:
        assert code == 0
        saved = json.loads(destination.read_text())["products"][0]["preview_sleep_evidence"]["inventory"]
        assert saved["sleep_observations"][0]["evidence"]["binding"] == binding


def test_unsupported_record_listing_preserves_separately_available_inventory(tmp_path: Path) -> None:
    source = response()
    source["products"][0]["preview"]["records_status"] = "unsupported"
    destination = tmp_path / "private.json"
    code, _, _ = _run_product_read(["product-status-read", "--preview-sleep-evidence-file", str(destination)], source)
    assert code == 0
    saved = json.loads(destination.read_text())["products"][0]["preview_sleep_evidence"]
    assert saved["records_status"] == "unsupported"
    assert saved["inventory"]["sleep_observations"][0]["evidence"] == inventory(source)["sleep_observations"][0]["evidence"]


def test_absent_optional_inventory_fields_are_not_fabricated(tmp_path: Path) -> None:
    source = response()
    for key in ("sleep_observations", "observed_slugs", "observed_count"):
        inventory(source).pop(key)
    destination = tmp_path / "private.json"
    code, _, _ = _run_product_read(["product-status-read", "--preview-sleep-evidence-file", str(destination)], source)
    assert code == 0
    saved = json.loads(destination.read_text())["products"][0]["preview_sleep_evidence"]["inventory"]
    assert set(saved) == {"status", "provenance"}


@pytest.mark.parametrize("kind", ["repo", "missing_parent", "save_failure"])
def test_private_destination_is_checked_before_read_and_save_failure_preserves_trace(tmp_path: Path, kind: str) -> None:
    source = response()
    destination = tmp_path / "private.json"
    if kind == "missing_parent":
        destination = tmp_path / "absent" / "private.json"
    (tmp_path / "repo").mkdir()
    with patch.object(helper, "active_repo_root", return_value=tmp_path if kind == "repo" else tmp_path / "repo"):
        with patch.object(testing_state, "write_staged", side_effect=ValueError("private_request_unavailable")):
            code, public, calls = _run_product_read(
                ["product-status-read", "--preview-sleep-evidence-file", str(destination)], source
            )
    assert not destination.exists()
    if kind == "save_failure":
        assert code == 1 and len(calls) == 1
        assert public["summary"]["trace_id"] == source["trace_id"]
        assert public["warnings"][0]["code"] == "private_result_unavailable"
    else:
        assert code == 2 and not calls


@pytest.mark.parametrize("field", ["records_status", "status", "source_kind", "freshness_status", "state"])
def test_unhashable_status_is_a_safe_invalid_response(tmp_path: Path, field: str) -> None:
    source = response()
    if field == "records_status":
        source["products"][0]["preview"][field] = []
    elif field == "status":
        inventory(source)[field] = []
    elif field == "state":
        inventory(source)["sleep_observations"][0][field] = []
    else:
        inventory(source)["provenance"][field] = []
    destination = tmp_path / "private.json"
    code, _, _ = _run_product_read(["product-status-read", "--preview-sleep-evidence-file", str(destination)], source)
    assert code == 1 and not destination.exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
