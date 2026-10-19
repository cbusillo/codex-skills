# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Submit one testing rebuild; the existing service worker owns its lifecycle."""

from __future__ import annotations

import argparse
import http.client
import re
import urllib.error
from typing import Any

COMMAND = "testing-rebuild-submit"
PLAN_COMMAND = "testing-rebuild-dry-run"
COMMANDS = frozenset({COMMAND, PLAN_COMMAND})
COPY_SOURCES = {
    "empty": "empty",
    "production": "production_restore",
    "upstream": "upstream_restore",
}


def add_parser(subparsers: Any) -> None:
    for command in (PLAN_COMMAND, COMMAND):
        parser = subparsers.add_parser(command, help="Inspect or queue the native testing rebuild at its existing address.")
        parser.add_argument("--product", required=True)
        parser.add_argument("--copy-source", choices=tuple(COPY_SOURCES), required=True)
        parser.add_argument("--artifact-id", required=True)
        parser.add_argument("--source-git-ref", required=True, help="Immutable artifact source commit.")
        parser.add_argument("--reason", required=True)
        if command == COMMAND:
            parser.add_argument("--idempotency-key", required=True)
            parser.add_argument("--reviewed-dry-run", action="store_true")
            parser.add_argument("--expected-plan-digest", default="")
            parser.add_argument("--dry-run-evidence-file", default="")


def intent(h: Any, args: argparse.Namespace) -> tuple[dict[str, object], dict[str, object]]:
    if args.command == COMMAND:
        h._require_idempotency(args)
    product = h._required_argument(args, "product")
    artifact = h._required_argument(args, "artifact_id")
    source = h._required_argument(args, "source_git_ref")
    reason = h._required_argument(args, "reason")
    if len(reason) > 1000:
        raise ValueError("invalid_reason")
    for field, value in (("product", product), ("artifact_id", artifact)):
        if not h.PRODUCT_READ_PATH_SEGMENT_RE.fullmatch(value):
            raise ValueError("invalid_" + field)
        h.public_identifier(value)
    if re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", source) is None:
        raise ValueError("immutable_source_commit_required")
    replacement: dict[str, object] = {
        "product": product,
        "instance": "testing",
        "strategy": "replace-and-cutover",
        "data_source_mode": COPY_SOURCES[args.copy_source],
        "artifact_id": artifact,
        "source_git_ref": source,
        "allow_empty_data": args.copy_source == "empty",
        "verify_health": True,
        "reason": reason,
    }
    body: dict[str, object] = {"schema_version": 1, "product": product, "replacement": replacement}
    request: dict[str, object] = {
        **{key: value for key, value in replacement.items() if key != "reason"},
        "payload_source": "operator_argument",
        "payload_digest": h.metadata_review_digest(body),
    }
    if args.command == COMMAND:
        request["idempotency_key_fingerprint"] = h.repository_inventory_idempotency_key_fingerprint(args.idempotency_key)
    h.assert_public_safe_shape(request)
    return request, body


def plan_agrees(h: Any, result: dict[str, Any], body: dict[str, object]) -> bool:
    replacement = h._require_dict(body["replacement"])
    return (result.get("status") == "pass" and result.get("candidate_verification") == "pending"
        and all(result.get(key) == replacement[key] for key in (
            "product", "instance", "artifact_id", "source_git_ref", "data_source_mode")))


def service_fingerprint(h: Any, service_url: str) -> str:
    return h.metadata_review_digest({"service_url": service_url.strip().rstrip("/")})


def project_plan(h: Any, response: dict[str, Any], body: dict[str, object], *, service_url: str) -> dict[str, object]:
    result = h._require_dict(response.get("result"))
    if response.get("status") != "accepted" or not plan_agrees(h, result, body):
        raise h.LaunchplaneSafetyError("invalid_response")
    projected = {key: result[key] for key in (
        "status", "product", "instance", "artifact_id", "source_git_ref", "data_source_mode", "candidate_verification")}
    projected.update(context=h.public_identifier(result.get("context")),
        address_origin=h._public_origin_url(result.get("base_url")),
        observed_at=h.public_timestamp(result.get("observed_at")),
        service_fingerprint=service_fingerprint(h, service_url))
    h.assert_public_safe_shape(projected)
    projected["mode"] = "dry-run"
    projected["plan_sha256"] = h.metadata_review_digest({"body": body, "preflight": projected})
    return projected


def require_review(h: Any, args: argparse.Namespace, body: dict[str, object]) -> dict[str, Any]:
    expected = h._reviewed_apply_digest(args)
    _, result = h._load_reviewed_evidence(args, operation=PLAN_COMMAND,
        expected_digest=expected, evidence_status="ok", result_status="pass")
    preflight = {key: value for key, value in result.items() if key != "plan_sha256"}
    if h.metadata_review_digest({"body": body, "preflight": preflight}) != expected:
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    return result


def execute_plan(h: Any, args: argparse.Namespace, request: dict[str, object], body: dict[str, object]) -> int:
    settings = h.prepare_operator_settings(args=args, operation=PLAN_COMMAND, request=request)
    if settings is None:
        return 2
    replacement = {key: value for key, value in h._require_dict(body["replacement"]).items() if key != "verify_health"}
    response = None
    try:
        response = h.request_launchplane(service_url=settings["service_url"],
            path=h.helper_command_path(PLAN_COMMAND), settings=settings,
            body={**body, "replacement": replacement}, timeout=args.timeout)
        output = h.base_payload(status="ok", operation=PLAN_COMMAND, request=request)
        output["result"] = project_plan(h, response, body, service_url=settings["service_url"])
        output["summary"] = {**h.service_response_diagnostics(response),
            "recommendation": "Inspect this admission evidence and save it for submit. The worker must still verify the new copy before cutover."}
        h.emit(output)
        return 0
    except urllib.error.HTTPError as error:
        h.emit_http_error_payload(operation=PLAN_COMMAND, request=request, exc=error)
    except h.ServiceResponseError as error:
        output = h.unavailable_payload(operation=PLAN_COMMAND, request=request, status="unavailable", code="invalid_response", message="The native preflight response was unavailable.")
        output["summary"] = h.service_response_diagnostics(error)
        h.emit(output)
    except h.LaunchplaneSafetyError as error:
        h.emit_safety_error_payload(operation=PLAN_COMMAND, request=request, exc=error, provider_payload=response)
    except ValueError:
        h.emit_invalid_response(operation=PLAN_COMMAND, request=request,
            diagnostics=h.service_response_diagnostics(response))
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as error:
        h.emit_read_transport_error(operation=PLAN_COMMAND, request=request, timeout=args.timeout, exc=error)
    return 1


def aligned_receipt(h: Any, response: dict[str, Any], body: dict[str, object], key: str) -> dict[str, Any]:
    operation = h._require_dict(response.get("result"))
    original = h._require_dict(operation.get("request"))
    replacement = h._require_dict(body["replacement"])
    if (
        response.get("status") != "accepted"
        or operation.get("idempotency_key") != key
        or not h.public_identifier(operation.get("operation_id"))
        or operation.get("product") != body["product"]
        or operation.get("instance") != replacement["instance"]
        or any(original.get(key) != value for key, value in replacement.items())
    ):
        raise h.LaunchplaneSafetyError("invalid_response")
    return operation


def replay_diagnostics(h: Any, response: dict[str, Any]) -> dict[str, object]:
    result: dict[str, object] = {}
    if type(response.get("replayed")) is bool:
        result["replayed"] = response["replayed"]
    original = h.service_response_diagnostics({"trace_id": response.get("original_trace_id")})
    if original.get("trace_id"):
        result["original_trace_id"] = original["trace_id"]
    return result


def rejection(h: Any, request: dict[str, object], error: urllib.error.HTTPError) -> dict[str, object]:
    provider = h._error_payload_from_http(error)
    output = h.summarize_http_error(operation=COMMAND, request=request, exc=error, provider_payload=provider)
    summary = output["summary"]
    if error.code == 409 and summary.get("error_code") == "odoo_stable_target_replacement_operation_active":
        try:
            projected = h._project_target_replacement_operation({"operation": provider.get("operation")})["operation"]
            output["result"] = {"operation": {key: projected[key] for key in ("operation_id", "status", "phase")}}
        except (h.LaunchplaneSafetyError, ValueError):
            pass
        summary["recommendation"] = "Read the blocking operation with target-replacement-operation-read. Preserve the original request and key; do not submit under a new key."
    elif error.code == 409 and summary.get("error_code") == "idempotency_key_reused":
        summary["recommendation"] = "Restore the original request arguments for this key. Do not change the key to bypass the conflict."
    elif error.code in {400, 404} and output["status"] != "denied":
        summary["recommendation"] = "Confirm the deployed service supports native testing rebuild preflight and submission. It may predate this capability; do not fall back to a legacy route or provider call."
    h.assert_public_safe_shape(output["summary"])
    h.assert_public_safe_shape(output["result"])
    return output


def execute(h: Any, args: argparse.Namespace) -> int:
    request, body = intent(h, args)
    if args.command == PLAN_COMMAND:
        return execute_plan(h, args, request, body)
    reviewed = require_review(h, args, body)
    settings = h.prepare_operator_settings(args=args, operation=COMMAND, request=request)
    if settings is None:
        return 2
    if reviewed.get("service_fingerprint") != service_fingerprint(h, settings["service_url"]):
        raise ValueError("reviewed_dry_run_service_changed")
    response: dict[str, Any] | None = None
    diagnostics: dict[str, object] = {}
    operation_reference: dict[str, object] = {}
    try:
        response = h.request_launchplane(
            service_url=settings["service_url"],
            path=h.helper_command_path(COMMAND),
            settings=settings,
            body=body,
            timeout=args.timeout,
            idempotency_key=args.idempotency_key.strip(),
        )
        operation = aligned_receipt(h, response, body, args.idempotency_key.strip())
        operation_reference = {"operation_id": h.public_identifier(operation["operation_id"])}
        result = h._project_target_replacement_operation({"operation": operation})
        output = h.base_payload(status="accepted", operation=COMMAND, request=request)
        output["result"] = result
        output["summary"] = {
            "recommendation": (
                "Read this operation with target-replacement-operation-read. Acceptance is not "
                "completion. Preserve its operation ID and original idempotency key."
            ),
            **h.service_response_diagnostics(response),
            **replay_diagnostics(h, response),
        }
        h.assert_public_safe_shape(output["summary"])
        h.emit(output)
        return 0
    except urllib.error.HTTPError as error:
        if 400 <= error.code < 500 and error.code != 408:
            try:
                h.emit(rejection(h, request, error))
            finally:
                error.close()
            return 1
        try:
            summary = h.summarize_http_error(operation=COMMAND, request=request, exc=error)["summary"]
            diagnostics = {key: value for key, value in summary.items()
                           if key in {"trace_id", "error_code", "http_status"}}
        finally:
            error.close()
    except h.ServiceResponseError as error:
        diagnostics = h.service_response_diagnostics(error)
    except (h.LaunchplaneSafetyError, ValueError):
        pass
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as error:
        diagnostics = h.service_response_diagnostics(error)
    output = h._apply_outcome_unknown(
        operation=COMMAND, request=request, label="original testing rebuild operation"
    )
    output["summary"].update(h.service_response_diagnostics(response))
    output["summary"].update(diagnostics)
    output["summary"].update(operation_reference)
    output["summary"]["recommendation"] = (
        "If an operation ID is present, read it with target-replacement-operation-read. "
        "Otherwise explicitly recover by submitting the identical original arguments, "
        "reviewed evidence and idempotency key. Do not generate a new key or change intent."
    )
    output["warnings"] = [h.warning("testing_rebuild_outcome_unknown",
        "Recover only with the identical original arguments, reviewed evidence and key. "
        "Never use a new key or resubmit after a denial. A mismatched receipt ID is not recovery evidence.")]
    h.emit(output)
    return 1
