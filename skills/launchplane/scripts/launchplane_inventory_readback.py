# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import argparse
import http.client
import re
import urllib.error
from typing import Any

from launchplane_native_evidence import _closed_object, _runtime_identity
from launchplane_safety import LaunchplaneSafetyError
from launchplane_testing_state import write_staged
from launchplane_website_indexing import destination as validate_destination

COMMAND = "odoo-inventory-readback"
TIMEOUT_SECONDS = 180.0
PURCHASER = "cm_custom.user_gunneet"
BINDING_FIELDS = {
    "context",
    "deployment_record_id",
    "source_git_ref",
    "artifact_id",
    "image_digest",
    "target_id",
}
SELECTOR_FIELDS = {
    "product",
    *BINDING_FIELDS,
    "company_id",
    "part_ids",
    "purchaser_xmlid",
}
COUNTS = {
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
}
HISTORICAL = {
    "historical_fields_copied",
    "historical_listing_conflicts",
    "historical_unmapped_lines",
    "historical_tracking_results",
}
FAILURES = {
    "capacity",
    "authority",
    "runtime_selection",
    "artifact",
    "runtime_connection",
    "producer_inventory",
    "runtime_inspection",
    "transport",
    "result_validation",
    "host_inspection",
    "host_execution",
    "configuration",
    "code_roots",
    "payload_provenance",
    "payload_producer",
    "payload_decode",
    "source_provenance",
    "source_producer",
    "applied_parameters",
    "addon_state",
    "source_resolution",
    "drift",
    "application_read",
}


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        COMMAND,
        help="Read bounded current testing inventory and purchaser facts into private evidence.",
    )
    parser.add_argument(
        "--payload-file",
        required=True,
        help="Private JSON with every expected runtime binding and witness selector.",
    )
    parser.add_argument(
        "--result-file",
        required=True,
        help="New private result file outside the repository; stdout contains no witnesses.",
    )


def require(condition: bool) -> None:
    if not condition:
        raise LaunchplaneSafetyError("invalid_response")


def text(value: object, *, maximum: int = 512, pattern: str | None = None) -> None:
    require(isinstance(value, str) and len(value) <= maximum and (pattern is None or re.fullmatch(pattern, value) is not None))


def count(value: object) -> None:
    require(type(value) is int and value >= 0)


def nullable_fields(source: dict[str, Any], fields: set[str], validate: Any) -> None:
    for field in fields:
        if source.get(field) is not None:
            validate(source[field])


def identifier(value: object) -> None:
    text(value, maximum=256, pattern=r"[a-zA-Z0-9_.:-]+")


def number(value: object) -> None:
    text(value, maximum=80, pattern=r"-?[0-9]+(?:\.[0-9]+)?")


def boolean(value: object) -> None:
    require(type(value) is bool)


def validate_intent(value: object) -> dict[str, Any]:
    source = _closed_object(value, SELECTOR_FIELDS, set())
    for field in {"product", *BINDING_FIELDS} - {"source_git_ref", "image_digest"}:
        identifier(source[field])
    text(source["source_git_ref"], pattern=r"[a-f0-9]{40}")
    text(source["image_digest"], pattern=r"sha256:[a-f0-9]{64}")
    count(source["company_id"])
    require(source["company_id"] >= 1 and source["purchaser_xmlid"] == PURCHASER)
    parts = source["part_ids"]
    require(isinstance(parts, list) and 1 <= len(parts) <= 10)
    for part in parts:
        text(part, pattern=r"[1-9][0-9]{0,17}")
    require(len(set(parts)) == len(parts))
    return source


def validate_part(value: object) -> dict[str, Any]:
    part = _closed_object(
        value,
        {"part_id", "status"},
        {
            "template_id",
            "name",
            "part_number",
            "barcode",
            "active",
            "cost",
            "sale_price",
            "tracking",
            "internal_quantity",
            "supplier_costs",
            "suppliers_truncated",
        },
    )
    text(part["part_id"], pattern=r"[1-9][0-9]{0,17}")
    require(part["status"] in ("present", "missing", "unavailable"))
    nullable_fields(part, {"template_id"}, count)
    nullable_fields(part, {"name", "part_number", "barcode"}, text)
    nullable_fields(part, {"active", "suppliers_truncated"}, boolean)
    nullable_fields(part, {"cost", "sale_price", "internal_quantity"}, number)
    require(part.get("tracking") in (None, "none", "lot", "serial"))
    if part.get("supplier_costs") is not None:
        suppliers = part["supplier_costs"]
        require(isinstance(suppliers, list) and len(suppliers) <= 10)
        for supplier in suppliers:
            supplier = _closed_object(
                supplier,
                {
                    "supplier_id",
                    "supplier_name",
                    "supplier_part_number",
                    "cost",
                    "currency_id",
                    "unit_id",
                },
                set(),
            )
            count(supplier["supplier_id"])
            nullable_fields(supplier, {"supplier_name", "supplier_part_number"}, text)
            nullable_fields(supplier, {"cost"}, number)
            nullable_fields(supplier, {"currency_id", "unit_id"}, count)
    return part


def validate_purchaser(value: object) -> None:
    purchaser = _closed_object(
        value,
        {"xmlid", "status"},
        {
            "module_state",
            "user_id",
            "active",
            "login",
            "name",
            "group_xmlids",
            "groups_without_xmlid",
            "first_signin",
        },
    )
    require(purchaser["xmlid"] == PURCHASER and purchaser["status"] in ("present", "missing", "unavailable"))
    require(purchaser.get("first_signin", "not_observed") == "not_observed")
    require(
        purchaser.get("module_state")
        in (
            None,
            "installed",
            "uninstalled",
            "to install",
            "to upgrade",
            "to remove",
            "uninstallable",
        )
    )
    nullable_fields(purchaser, {"user_id", "groups_without_xmlid"}, count)
    nullable_fields(purchaser, {"active"}, boolean)
    nullable_fields(purchaser, {"login", "name"}, text)
    if purchaser.get("group_xmlids") is not None:
        groups = purchaser["group_xmlids"]
        require(isinstance(groups, list) and len(groups) <= 100)
        for group in groups:
            identifier(group)


def validate_evidence(value: object, intent: dict[str, Any]) -> None:
    evidence = _closed_object(
        value,
        {"identity", "target_id", "target_name", "container_id", "observed_at"},
        set(),
    )
    for field in ("target_id", "target_name", "container_id", "observed_at"):
        require(isinstance(evidence[field], str))
    require(evidence["target_id"] == intent["target_id"])
    identity = evidence["identity"]
    _runtime_identity(identity, {**intent, "instance": "testing", "request": intent})
    require(identity["deployment_record_id"] == intent["deployment_record_id"])
    digest = identity.get("image_digest") or identity.get("image_reference", "").rpartition("@")[2]
    require(digest == intent["image_digest"])
    if identity.get("image_reference"):
        require(identity["image_reference"].rpartition("@")[2] == intent["image_digest"])


def validate_response(value: object, intent: dict[str, Any]) -> dict[str, Any]:
    source = _closed_object(value, {"trace_id", "result"}, {"status"})
    require(source.get("status", "ok") == "ok")
    require(isinstance(source["trace_id"], str))
    result = _closed_object(
        source["result"],
        set(),
        {
            "runtime",
            "evidence",
            "observed_at",
            "database_snapshot",
            "database_uuid",
            "company_id",
            "facts",
            "parts",
            "purchaser",
            "failure_stage",
            "applied",
            "scope",
        },
    )
    runtime = result.get("runtime", "unknown")
    require(runtime in ("verified", "unknown") and result.get("applied", False) is False)
    require(result.get("scope", "current_database_stored_facts") == "current_database_stored_facts")
    require(result.get("failure_stage") is None or result["failure_stage"] in tuple(FAILURES))
    if result.get("evidence") is not None:
        validate_evidence(result["evidence"], intent)
    if result.get("observed_at") is not None:
        require(isinstance(result["observed_at"], str))
    if result.get("database_snapshot") is not None:
        text(result["database_snapshot"], maximum=4096, pattern=r"[0-9]+:[0-9]+:[0-9,]*")
    if result.get("database_uuid") is not None:
        text(result["database_uuid"], pattern=r"[a-f0-9-]{36}")
    if result.get("company_id") is not None:
        count(result["company_id"])
        require(result["company_id"] == intent["company_id"])
    if result.get("facts") is not None:
        facts = _closed_object(result["facts"], {*COUNTS, "internal_quantity"}, HISTORICAL)
        nullable_fields(facts, COUNTS, count)
        nullable_fields(facts, {"internal_quantity"}, number)
        require(all(facts.get(field) is None for field in HISTORICAL))
    parts = result.get("parts", [])
    require(isinstance(parts, list) and len(parts) <= 10)
    ids = [validate_part(part)["part_id"] for part in parts]
    require(len(set(ids)) == len(ids) and all(part in intent["part_ids"] for part in ids))
    if result.get("purchaser") is not None:
        validate_purchaser(result["purchaser"])
    if runtime == "verified":
        require(
            all(
                result.get(field) is not None
                for field in (
                    "evidence",
                    "observed_at",
                    "database_snapshot",
                    "company_id",
                    "facts",
                    "purchaser",
                )
            )
        )
        require(ids == intent["part_ids"] and result.get("failure_stage") is None)
    return source


def execute_private_read(
    h: Any,
    args: argparse.Namespace,
    *,
    command: str,
    intent_validator: Any,
    response_validator: Any,
    result_summary: Any,
    recommendation: str,
    invalid_transport_code: str = "invalid_inventory_transport",
) -> int:
    request = {"payload_source": "private_file"}
    try:
        intent = intent_validator(h.read_payload_file(args.payload_file))
        destination = args.result_file
        validate_destination(h, destination)
        path = h._product_read_path(command, product=intent["product"])
    except (ValueError, OSError):
        h.emit(
            h.unavailable_payload(
                operation=command,
                request=request,
                status="invalid",
                code="invalid_request",
                message="The private read selectors or destination are invalid.",
            )
        )
        return 2
    settings = h.prepare_operator_settings(args=args, operation=command, request=request)
    if settings is None:
        return 2
    response = None
    try:
        response = h.request_launchplane_read(
            service_url=settings["service_url"],
            settings=settings,
            path=path,
            query={key: str(value) if type(value) is int else value for key, value in intent.items() if key != "product"},
            timeout=args.timeout,
            no_store=True,
        )
        validated = response_validator(response, intent)
        try:
            write_staged(h, destination, validated)
        except ValueError:
            payload = h.unavailable_payload(
                operation=command,
                request=request,
                status="unavailable",
                code="private_result_unavailable",
                message="A valid response was received, but the private result could not be saved.",
            )
            payload["summary"].update(h.service_response_diagnostics(response))
            payload["summary"]["recommendation"] = (
                "Preserve any partial private destination and existing holds. No saved evidence is established."
            )
            h.emit(payload)
            return 1
        payload = h.base_payload(status="ok", operation=command, request=request)
        payload["result"] = {"private_result_saved": True, **result_summary(validated["result"])}
        payload["summary"] = {**h.service_response_diagnostics(response), "recommendation": recommendation}
        h.emit(payload)
        return 0
    except urllib.error.HTTPError as error:
        try:
            h.emit_http_error_payload(operation=command, request=request, exc=error)
        finally:
            error.close()
    except h.ServiceResponseError as error:
        h.emit_safety_error_payload(
            operation=command, request=request, exc=LaunchplaneSafetyError("invalid_response"), provider_payload=error
        )
    except LaunchplaneSafetyError as error:
        h.emit_safety_error_payload(operation=command, request=request, exc=error, provider_payload=response)
    except ValueError:
        h.emit(
            h.unavailable_payload(
                operation=command,
                request=request,
                status="invalid",
                code=invalid_transport_code,
                message="The read transport request is invalid; no saved evidence is established.",
            )
        )
    except (OSError, http.client.HTTPException, urllib.error.URLError) as error:
        h.emit_read_transport_error(operation=command, request=request, timeout=args.timeout, exc=error)
    return 1


def execute(h: Any, args: argparse.Namespace) -> int:
    return execute_private_read(
        h,
        args,
        command=COMMAND,
        intent_validator=validate_intent,
        response_validator=validate_response,
        result_summary=lambda result: {"runtime": result.get("runtime", "unknown"), "applied": False},
        recommendation="Read the private current-facts evidence. Historical reconciliation and first sign-in remain unobserved; existing holds remain.",
    )
