# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Reviewed application testing ingress through the existing service identity."""

from __future__ import annotations

import argparse
import http.client
import re
import urllib.error
import urllib.parse
from typing import Any

PREFIX = "testing-private-ingress-"
COMMANDS = frozenset(PREFIX + mode for mode in ("dry-run", "apply", "readback"))
SCOPE = ("product", "context", "instance", "endpoint_key", "edge_endpoint_key", "operation")
INPUT_FIELDS = {*SCOPE, "access_list_id", "certificate_id", "reason"}
RESULT_FIELDS = {"product", "context", "instance", "endpoint_key", "operation",
                 "plan_sha256", "applied", "deployment_performed", "transport_verified"}
REPLAN_FIELDS = {"replan_required", "previous_effects_absent"}
RECOVERY = (
    "Retain the exact private request, reviewed evidence and original idempotency key. "
    "Use testing-private-ingress-readback first; if unverified, explicitly repeat the "
    "original apply with the same key for service-owned recovery. Never change identity or use a provider call."
)


def add_parser(subparsers: Any) -> None:
    for mode in ("dry-run", "apply", "readback"):
        parser = subparsers.add_parser(PREFIX + mode, help="Plan, publish or verify one private application testing hostname.")
        parser.add_argument("--payload-file", required=True, help="Private request JSON outside the repository.")
        if mode != "dry-run":
            parser.add_argument("--expected-plan-digest", required=True)
            parser.add_argument("--dry-run-evidence-file", required=True)
            parser.add_argument("--reviewed-dry-run", action="store_true")
        if mode == "apply":
            parser.add_argument("--idempotency-key", required=True, help="Original key, also on recovery.")


def intent(h: Any, args: argparse.Namespace) -> tuple[dict[str, object], dict[str, Any]]:
    body = h.read_payload_file(args.payload_file)
    if body.keys() != INPUT_FIELDS:
        raise ValueError("invalid_testing_private_ingress_fields")
    for field in (*SCOPE, "reason"):
        value = body[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("invalid_testing_private_ingress_" + field)
        body[field] = value.strip()
    for field in SCOPE:
        if not h.PRODUCT_READ_PATH_SEGMENT_RE.fullmatch(body[field]):
            raise ValueError("invalid_testing_private_ingress_" + field)
        h.public_identifier(body[field])
    if body["instance"] != "testing" or body["operation"] not in {"create", "disable"}:
        raise ValueError("invalid_testing_private_ingress_scope")
    for field in ("access_list_id", "certificate_id"):
        if type(body[field]) is not int or body[field] <= 0:
            raise ValueError("invalid_testing_private_ingress_" + field)
    request = {field: body[field] for field in SCOPE}
    request["payload_digest"] = h.metadata_review_digest(body)
    request["payload_source"] = "private_file"
    h.assert_public_safe_shape(request)
    return request, body


def binding(h: Any, request: dict[str, object], digest: str, service: str) -> str:
    return h.metadata_review_digest({"request": request, "plan_sha256": digest, "service_fingerprint": service})


def require_review(h: Any, args: argparse.Namespace, request: dict[str, object]) -> tuple[str, dict[str, Any]]:
    if not args.reviewed_dry_run:
        raise ValueError("reviewed_dry_run_required")
    digest = args.expected_plan_digest.strip()
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("invalid_expected_plan_digest")
    evidence = h.read_payload_file(args.dry_run_evidence_file)
    result = evidence.get("result")
    if (evidence.get("operation") != PREFIX + "dry-run" or evidence.get("status") != "accepted"
        or evidence.get("request") != request or not isinstance(result, dict)
        or any(result.get(field) != request[field] for field in SCOPE)
        or result.get("mode") != "dry-run" or result.get("applied") is not False
        or result.get("deployment_performed") is not False or result.get("transport_verified") is not False
        or result.get("plan_sha256") != digest
        or not isinstance(result.get("service_fingerprint"), str)
        or result.get("review_binding_sha256") != binding(h, request, digest, result["service_fingerprint"])):
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    return digest, result


def project(h: Any, response: dict[str, Any], body: dict[str, Any], mode: str, digest: str) -> dict[str, object]:
    source = h._require_dict(response.get("result"))
    if response.get("status") != "accepted" or source.keys() - RESULT_FIELDS - REPLAN_FIELDS:
        raise h.LaunchplaneSafetyError("invalid_response")
    if (any(source.get(field) != body[field] for field in RESULT_FIELDS & set(SCOPE))
        or source.get("deployment_performed") is not False or source.get("transport_verified") is not False
        or type(source.get("applied")) is not bool
        or not isinstance(source.get("plan_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", source["plan_sha256"])
        or (mode != "dry-run" and source["plan_sha256"] != digest)):
        raise h.LaunchplaneSafetyError("invalid_response")
    replan = bool(source.keys() & REPLAN_FIELDS)
    if replan:
        if (mode != "apply" or source.get("applied") is not False
            or any(source.get(key) is not True for key in REPLAN_FIELDS)):
            raise h.LaunchplaneSafetyError("invalid_response")
    elif source["applied"] is not (mode != "dry-run"):
        raise h.LaunchplaneSafetyError("invalid_response")
    result = {**source, **{field: body[field] for field in SCOPE}, "mode": mode}
    h.assert_public_safe_shape(result)
    return result


def execute(h: Any, args: argparse.Namespace) -> int:
    mode = args.command.removeprefix(PREFIX)
    request, private = intent(h, args)
    digest = ""
    reviewed: dict[str, Any] = {}
    if mode != "dry-run":
        if mode == "apply":
            h._require_idempotency(args)
        digest, reviewed = require_review(h, args, request)
    settings = h.prepare_operator_settings(args=args, operation=args.command, request=request)
    if settings is None:
        return 2
    service = h.metadata_review_digest({"service_url": settings["service_url"].rstrip("/")})
    if mode != "dry-run" and reviewed["service_fingerprint"] != service:
        raise ValueError("reviewed_dry_run_service_changed")
    body = {key: value for key, value in private.items() if key != "product"}
    path = h.helper_command_path(args.command).format(product=urllib.parse.quote(private["product"], safe=""))
    if mode != "dry-run":
        body["reviewed_plan_sha256"] = digest
    response: dict[str, Any] | None = None
    readback: dict[str, Any] | None = None
    result: dict[str, object] | None = None
    applied = False

    def call(call_mode: str) -> dict[str, Any]:
        return h.request_launchplane(service_url=settings["service_url"], path=path,
            settings=settings, body={**body, "mode": call_mode}, timeout=args.timeout,
            idempotency_key=args.idempotency_key.strip() if call_mode == "apply" else "")

    try:
        # Do not replan before original-key recovery: effects may already exist.
        response = call(mode)
        result = project(h, response, private, mode, digest)
        result["service_fingerprint"] = service
        if mode == "dry-run":
            result["review_binding_sha256"] = binding(h, request, str(result["plan_sha256"]), service)
        elif mode == "apply" and result.get("replan_required") is not True:
            applied = True
            readback = call("readback")
            project(h, readback, private, "readback", digest)
            result["read_back_matches"] = True
            result["read_back_trace_id"] = h.service_response_diagnostics(readback).get("trace_id", "")
        status = "replan_required" if result.get("replan_required") else "accepted"
        output = h.base_payload(status=status, operation=args.command, request=request)
        output["result"] = result
        output["summary"] = {**h.service_response_diagnostics(response),
            "recommendation": ("Previous effects are absent. Obtain a fresh reviewed dry-run and a new key."
                if status == "replan_required" else "The requested ingress state was verified. Retain the reviewed evidence and original key. Transport and runtime health remain unproven." if mode != "dry-run" else
                "Save and review this evidence before apply. This plan changes no provider state and proves no transport or runtime health.")}
        if mode == "apply":
            output["summary"]["idempotency_key_fingerprint"] = h.repository_inventory_idempotency_key_fingerprint(args.idempotency_key)
        if type(response.get("replayed")) is bool:
            output["summary"]["replayed"] = response["replayed"]
        original = h.service_response_diagnostics({"trace_id": response.get("original_trace_id")})
        if original.get("trace_id"):
            output["summary"]["original_trace_id"] = original["trace_id"]
        h.assert_public_safe_shape(output["summary"])
        h.emit(output)
        return 1 if status == "replan_required" else 0
    except urllib.error.HTTPError as error:
        error_payload = h._ServiceResponse({}, error.code)
        try:
            try:
                error_payload = h._error_payload_from_http(error)
                failure = h.summarize_http_error(operation=args.command, request=request, exc=error,
                                                 provider_payload=error_payload)
            except (h.LaunchplaneSafetyError, ValueError, OSError, http.client.HTTPException):
                failure = h.unavailable_payload(operation=args.command, request=request,
                    status="unavailable", code="invalid_response", message="The service error could not be verified.")
        finally:
            error.close()
        if mode != "apply" or (not applied and 400 <= error.code < 500 and error.code != 408):
            if mode == "apply" and error.code == 409:
                failure["summary"]["recommendation"] = RECOVERY
            h.emit(failure)
            return 1
        diagnostics = h.service_response_diagnostics(error_payload)
    except h.ServiceResponseError as error:
        diagnostics = h.service_response_diagnostics(error)
    except h.LaunchplaneSafetyError as error:
        if mode != "apply":
            h.emit_safety_error_payload(operation=args.command, request=request, exc=error, provider_payload=response)
            return 1
        diagnostics = h.service_response_diagnostics(readback if applied else response)
    except ValueError:
        diagnostics = h.service_response_diagnostics(readback if applied else response)
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as error:
        if mode != "apply":
            h.emit_read_transport_error(operation=args.command, request=request, timeout=args.timeout, exc=error)
            return 1
        diagnostics = h.service_response_diagnostics(error)
    if mode == "apply":
        output = h._apply_outcome_unknown(operation=args.command, request=request, label="private testing hostname")
        if applied:
            output["status"] = "accepted_unverified"
            output["result"] = result or {}
        output["summary"].update(h.service_response_diagnostics(response))
        for key in ("trace_id", "error_code", "http_status"):
            if key in diagnostics:
                output["summary"][("read_back_" if applied else "") + key] = diagnostics[key]
        output["summary"]["recommendation"] = RECOVERY
        h.emit(output)
    else:
        h.emit_invalid_response(operation=args.command, request=request, diagnostics=diagnostics)
    return 1
