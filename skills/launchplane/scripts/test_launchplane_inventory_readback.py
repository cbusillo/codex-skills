# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.2.0"]
# ///

from __future__ import annotations

import io
import json
import os
import stat
import urllib.error
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import launchplane_inventory_readback as inventory_readback
import pytest
from launchplane_inventory_readback import PURCHASER
from test_launchplane_helpers import write_action as helper


class Invocation(NamedTuple):
    code: int
    output: dict[str, Any]
    transport: Any
    destination: Path


def intent() -> dict[str, Any]:
    return {
        "product": "example",
        "context": "example-testing",
        "deployment_record_id": "deploy-fictional",
        "source_git_ref": "a" * 40,
        "artifact_id": "artifact-fictional",
        "image_digest": "sha256:" + "b" * 64,
        "target_id": "target-fictional",
        "company_id": 7,
        "part_ids": ["11", "12"],
        "purchaser_xmlid": PURCHASER,
    }


def response() -> dict[str, Any]:
    expected = intent()
    identity = {
        key: expected[key]
        for key in (
            "product",
            "context",
            "deployment_record_id",
            "source_git_ref",
            "artifact_id",
            "image_digest",
        )
    }
    identity["instance"] = "testing"
    return {
        "status": "ok",
        "trace_id": "req_inventory_fictional",
        "result": {
            "runtime": "verified",
            "evidence": {
                "identity": identity,
                "target_id": expected["target_id"],
                "target_name": "PRIVATE_TARGET",
                "container_id": "PRIVATE_CONTAINER",
                "observed_at": "2026-11-11T12:00:00Z",
            },
            "observed_at": "2026-11-11T12:00:01Z",
            "database_snapshot": "20:30:",
            "database_uuid": "00000000-0000-0000-0000-000000000001",
            "company_id": expected["company_id"],
            "facts": {
                **dict.fromkeys(
                    (
                        "parts",
                        "supplier_cost_rows",
                        "listing_bindings",
                        "nonzero_sale_price_parts",
                        "stockable_parts",
                        "lot_tracked_parts",
                        "serial_tracked_parts",
                        "current_conflicts",
                        "parts_with_extra_details",
                        "parts_with_upc",
                        "parts_with_packaging",
                        "stock_move_rows",
                        "done_stock_move_rows",
                    ),
                ),
                "internal_quantity": "0",
            },
            "parts": [
                {
                    "part_id": "11",
                    "status": "present",
                    "name": "PRIVATE_PART",
                    "cost": "2",
                    "barcode": None,
                },
                {"part_id": "12", "status": "missing"},
            ],
            "purchaser": {
                "xmlid": expected["purchaser_xmlid"],
                "status": "unavailable",
                "login": None,
            },
            "failure_stage": None,
            "applied": False,
            "scope": "current_database_stored_facts",
        },
    }


class Response(io.BytesIO):
    def __init__(self, value: object, cache_control: str = "private, no-store") -> None:
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode())
        self.status = 200
        self.headers = Message()
        for header in cache_control.split(";"):
            self.headers["Cache-Control"] = header


def invoke(
    tmp_path: Path,
    *,
    request: dict[str, Any] | None = None,
    returned: object = None,
    cache_control: str = "private, no-store",
    error: Exception | None = None,
    result_file: Path | None = None,
) -> Invocation:
    (tmp_path / "repo").mkdir(exist_ok=True)
    private = tmp_path / "request.json"
    private.write_text(json.dumps(intent() if request is None else request))
    destination = result_file if result_file is not None else tmp_path / "result.json"
    output = io.StringIO()
    with (
        patch.object(helper, "active_repo_root", return_value=tmp_path / "repo"),
        patch.object(
            helper,
            "prepare_operator_settings",
            return_value={
                "service_url": "https://plane.example.invalid",
                "token": "PRIVATE_TOKEN",
            },
        ),
        patch.object(
            helper,
            "safe_urlopen",
            return_value=Response(response() if returned is None else returned, cache_control),
            side_effect=error,
        ) as transport,
        redirect_stdout(output),
    ):
        code = helper.main(
            [
                "odoo-inventory-readback",
                "--payload-file",
                str(private),
                "--result-file",
                str(destination),
            ]
        )
    public = output.getvalue()
    assert "PRIVATE_" not in public
    assert str(tmp_path) not in public
    return Invocation(code, json.loads(public), transport, destination)


def test_exact_binding_and_repeated_witnesses_keep_private_nullable_facts(
    tmp_path: Path,
) -> None:
    code, public, transport, destination = invoke(tmp_path)
    assert code == 0
    request = transport.call_args.args[0]
    assert request.method == "GET"
    assert request.get_header("Authorization") == "Bearer PRIVATE_TOKEN"
    assert request.get_header("Cache-control") == "no-store"
    assert urlsplit(request.full_url).path == helper.helper_command_path("odoo-inventory-readback").format(
        product=intent()["product"]
    )
    assert parse_qs(urlsplit(request.full_url).query) == {
        key: value if isinstance(value, list) else [str(value)] for key, value in intent().items() if key != "product"
    }
    assert json.loads(destination.read_text()) == response()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert "facts" not in public["result"]


@pytest.mark.parametrize("field", list(intent()))
def test_every_expected_selector_is_required_before_transport(tmp_path: Path, field: str) -> None:
    request = intent()
    del request[field]
    code, _, transport, destination = invoke(tmp_path, request=request)
    assert code != 0
    transport.assert_not_called()
    assert not destination.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("part_ids", []),
        ("part_ids", ["11"] * 2),
        ("part_ids", [str(n) for n in range(1, 12)]),
        ("part_ids", ["0"]),
        ("part_ids", ["1&model=res.users"]),
        ("company_id", True),
        ("company_id", 0),
        ("source_git_ref", "main"),
        ("image_digest", "latest"),
        ("purchaser_xmlid", "base.user_admin"),
        ("model", "res.users"),
    ],
)
def test_invalid_or_open_selectors_never_reach_transport(tmp_path: Path, field: str, value: object) -> None:
    code, _, transport, destination = invoke(tmp_path, request={**intent(), field: value})
    assert code != 0
    transport.assert_not_called()
    assert not destination.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("company_id", 8),
        ("applied", True),
        ("runtime", "clean"),
        ("parts", [{"part_id": "99", "status": "missing"}]),
        ("facts", {"parts": 0}),
        ("scope", "historical_reconciliation"),
        ("unexpected", "PRIVATE_UNKNOWN"),
    ],
)
def test_invalid_or_unbound_response_is_not_saved(tmp_path: Path, field: str, value: object) -> None:
    returned = response()
    returned["result"][field] = value
    code, _, _, destination = invoke(tmp_path, returned=returned)
    assert code != 0
    assert not destination.exists()


@pytest.mark.parametrize(
    "field",
    [
        "product",
        "context",
        "instance",
        "deployment_record_id",
        "source_git_ref",
        "artifact_id",
        "image_digest",
    ],
)
def test_runtime_identity_drift_is_not_saved(tmp_path: Path, field: str) -> None:
    returned = response()
    returned["result"]["evidence"]["identity"][field] = "wrong"
    code, _, _, destination = invoke(tmp_path, returned=returned)
    assert code != 0
    assert not destination.exists()


def test_unknown_runtime_is_preserved_without_claiming_application_success(
    tmp_path: Path,
) -> None:
    returned = {
        "trace_id": "req_unknown",
        "result": {"runtime": "unknown", "failure_stage": "application_read"},
    }
    code, public, _, destination = invoke(tmp_path, returned=returned)
    assert code == 0
    assert public["result"]["runtime"] == "unknown"
    assert json.loads(destination.read_text()) == returned


@pytest.mark.parametrize("cache_control", ["", "private", "no-cache", "x-no-store"])
def test_missing_no_store_refuses_before_private_save(tmp_path: Path, cache_control: str) -> None:
    code, _, _, destination = invoke(tmp_path, cache_control=cache_control)
    assert code != 0
    assert not destination.exists()


@pytest.mark.parametrize("returned", [b"PRIVATE_INVALID", {"result": {}}])
def test_malformed_response_fails_without_disclosure(tmp_path: Path, returned: object) -> None:
    code, _, _, destination = invoke(tmp_path, returned=returned)
    assert code != 0
    assert not destination.exists()


def test_http_error_does_not_save_or_disclose_private_body(tmp_path: Path) -> None:
    error = urllib.error.HTTPError("https://PRIVATE_HOST", 403, "PRIVATE_REASON", Message(), io.BytesIO(b"PRIVATE_ERROR"))
    code, public, _, destination = invoke(tmp_path, error=error)
    assert code != 0
    assert public["status"] == "denied"
    assert public["summary"]["http_status"] == 403
    assert not destination.exists()


def test_existing_result_is_never_overwritten(tmp_path: Path) -> None:
    destination = tmp_path / "result.json"
    destination.write_text("retained")
    code, _, transport, _ = invoke(tmp_path)
    assert code != 0
    transport.assert_not_called()
    assert destination.read_text() == "retained"


def test_decimals_supplier_rows_and_purchaser_groups_survive_private_projection(tmp_path: Path) -> None:
    returned = response()
    part = returned["result"]["parts"][0]
    part.update(
        cost="2.75",
        internal_quantity="-1.50",
        supplier_costs=[
            {
                "supplier_id": 21,
                "supplier_name": "PRIVATE_SUPPLIER",
                "supplier_part_number": None,
                "cost": "3.25",
                "currency_id": None,
                "unit_id": 1,
            }
        ],
        suppliers_truncated=True,
    )
    returned["result"]["purchaser"].update(
        status="present",
        active=False,
        user_id=17,
        group_xmlids=["base.group_user"],
        groups_without_xmlid=0,
        first_signin="not_observed",
    )
    returned["result"]["facts"].update(parts=0, historical_fields_copied=None)
    code, _, _, destination = invoke(tmp_path, returned=returned)
    assert code == 0
    assert json.loads(destination.read_text()) == returned


@pytest.mark.parametrize("location", ["envelope", "evidence", "identity", "part", "purchaser", "facts"])
def test_unknown_fields_at_every_private_boundary_fail_safely(tmp_path: Path, location: str) -> None:
    returned = response()
    result = returned["result"]
    selected = {
        "envelope": returned,
        "evidence": result["evidence"],
        "identity": result["evidence"]["identity"],
        "part": result["parts"][0],
        "purchaser": result["purchaser"],
        "facts": result["facts"],
    }[location]
    selected["future_field"] = "PRIVATE_FUTURE"
    code, _, _, destination = invoke(tmp_path, returned=returned)
    assert code != 0
    assert not destination.exists()


@pytest.mark.parametrize("location", ["repo", "missing-parent", "nul"])
def test_invalid_private_destination_is_refused_before_transport(tmp_path: Path, location: str) -> None:
    destination = {
        "repo": tmp_path / "repo" / "result.json",
        "missing-parent": tmp_path / "missing" / "result.json",
        "nul": tmp_path / "bad\0name",
    }[location]
    code, _, transport, _ = invoke(tmp_path, result_file=destination)
    assert code != 0
    transport.assert_not_called()


@pytest.mark.parametrize("product", [".", "..", "example:alternate"])
def test_product_uses_existing_read_path_validation(tmp_path: Path, product: str) -> None:
    code, _, transport, _ = invoke(tmp_path, request={**intent(), "product": product})
    assert code != 0
    transport.assert_not_called()


def test_valid_oversized_response_is_refused_by_transport_limit(tmp_path: Path) -> None:
    returned = response()
    returned["result"]["evidence"]["target_name"] = "x" * (1024 * 1024)
    code, public, _, destination = invoke(tmp_path, returned=returned)
    assert code != 0
    assert public["status"] == "invalid"
    assert not destination.exists()


def test_separate_cache_control_headers_preserve_no_store(tmp_path: Path) -> None:
    code, _, _, _ = invoke(tmp_path, cache_control="private;no-store")
    assert code == 0


def test_optional_runtime_identity_uses_exact_pinned_reference_digest(tmp_path: Path) -> None:
    returned = response()
    identity = returned["result"]["evidence"]["identity"]
    del identity["product"]
    del identity["image_digest"]
    identity["image_reference"] = "registry.example.invalid/fictional@" + intent()["image_digest"]
    code, _, _, destination = invoke(tmp_path, returned=returned)
    assert code == 0
    assert json.loads(destination.read_text()) == returned


@pytest.mark.parametrize("field", ["evidence", "observed_at", "database_snapshot", "company_id", "facts", "purchaser"])
def test_verified_runtime_requires_complete_bound_application_evidence(tmp_path: Path, field: str) -> None:
    returned = response()
    del returned["result"][field]
    code, _, _, destination = invoke(tmp_path, returned=returned)
    assert code != 0
    assert not destination.exists()


@pytest.mark.parametrize(
    "change", ["target", "partial-parts", "reordered-parts", "failure-stage", "supplier-type", "purchaser-type", "fact-type"]
)
def test_application_binding_and_nested_types_fail_closed(tmp_path: Path, change: str) -> None:
    returned = response()
    result = returned["result"]
    if change == "target":
        result["evidence"]["target_id"] = "another-target"
    elif change == "partial-parts":
        result["parts"] = result["parts"][:1]
    elif change == "reordered-parts":
        result["parts"].reverse()
    elif change == "failure-stage":
        result["failure_stage"] = "drift"
    elif change == "supplier-type":
        result["parts"][0]["supplier_costs"] = {"supplier_id": 1}
    elif change == "purchaser-type":
        result["purchaser"]["active"] = "yes"
    else:
        result["facts"]["parts"] = True
    code, _, _, destination = invoke(tmp_path, returned=returned)
    assert code != 0
    assert not destination.exists()


@pytest.mark.parametrize("field", ["digest", "reference-only", "reference-with-digest"])
def test_well_formed_image_drift_is_never_saved(tmp_path: Path, field: str) -> None:
    returned = response()
    identity = returned["result"]["evidence"]["identity"]
    different = "sha256:" + "c" * 64
    if field == "digest":
        identity["image_digest"] = different
    else:
        identity["image_reference"] = "registry.example.invalid/fictional@" + different
        if field == "reference-only":
            del identity["image_digest"]
    code, _, _, destination = invoke(tmp_path, returned=returned)
    assert code != 0
    assert not destination.exists()


def test_save_failure_retains_safe_read_diagnostics(tmp_path: Path) -> None:
    with patch.object(inventory_readback, "write_staged", side_effect=ValueError("PRIVATE_SAVE_ERROR")):
        code, public, _, destination = invoke(tmp_path)
    assert code != 0
    assert public["summary"]["trace_id"] == response()["trace_id"]
    assert public["summary"]["http_status"] == 200
    assert not destination.exists()


def test_unwritable_private_destination_refuses_before_transport(tmp_path: Path) -> None:
    with patch.object(os, "access", return_value=False):
        code, _, transport, _ = invoke(tmp_path)
    assert code != 0
    transport.assert_not_called()


@pytest.mark.parametrize(
    "error", [TimeoutError("PRIVATE_TIMEOUT"), urllib.error.URLError("PRIVATE_CONNECTION"), ValueError("PRIVATE_HEADER")]
)
def test_transport_failures_do_not_claim_private_save_failure(tmp_path: Path, error: Exception) -> None:
    code, public, _, destination = invoke(tmp_path, error=error)
    assert code != 0
    assert not destination.exists()
    assert all(warning["code"] != "private_result_unavailable" for warning in public["warnings"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
