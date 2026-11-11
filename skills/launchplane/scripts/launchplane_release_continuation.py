# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Review, apply and read back the continuation of an original accepted release."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

REVIEW = "release-continuation-review"
APPLY = "release-continuation-apply"
READ = "odoo-promotion-operation-read"
COMMANDS = frozenset({REVIEW, APPLY, READ})
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
CONTINUATION_FIELDS = frozenset({
    "trace_id", "decision_record_id", "operation_id", "review_digest", "status", "provider_mutation", "apply_authorized",
})
OPERATION_FIELDS = frozenset({
    "operation_id", "product", "context", "instance", "attempt", "created_at", "started_at", "updated_at",
    "finished_at", "error_code", "error_message", "request_id", "status", "phase", "result",
})
OPERATION_STATUSES = frozenset({"pending", "running", "reconciliation_required", "pass", "fail", "cancelled"})
OPERATION_PHASES = frozenset({
    "created", "running", "validated", "logical_backup_started", "logical_backup_completed",
    "promotion_started", "completed", "failed", "cancelled",
})
RESULT_STATUSES = {
    "run_status": {"pass", "fail", "blocked"},
    "input_status": {"ready", "blocked"},
    **{name: {"pending", "pass", "fail", "skipped"} for name in (
        "backup_status", "promotion_status", "deployment_status", "post_deploy_status", "destination_health_status",
    )},
}
RESULT_IDS = ("artifact_id", "backup_record_id", "promotion_record_id", "deployment_record_id")
PRINTED_DIAGNOSTICS = frozenset({"http_status", "trace_id", "error_code"})
RECOVERY = {
    "ok": "Only the original operation continues; acceptance and promotion permission are unchanged.",
    "stale_review": "The reviewed evidence changed. Review again and apply only the new digest.",
    "outcome_unknown": "Outcome unknown. Resubmit the identical apply as the same identity: a committed continuation "
    "returns replayed and nothing is queued twice. Do not review afresh or change the reason first.",
    "not_found": "Launchplane found no record for this binding. A 404 never proves the operation is absent or had no effect.",
    "refused": "The service refused continuation. Inspect the code and trace; do not bypass it by hand.",
    "unavailable": "This review or read changed nothing. Retry it when the service answers.",
    "invalid_response": "The reply is not this binding's Odoo promotion operation view, such as another family or scope. "
    "It is not evidence of absence or of no effect.",
    "invalid": "Nothing was sent. Save the exact review output and apply that review's digest with the same reason.",
}


def add_parser(subparsers: Any) -> None:
    for command in (REVIEW, APPLY):
        parser = subparsers.add_parser(command, help="Continue an original accepted release; never a new acceptance.")
        parser.add_argument("--product", required=True)
        parser.add_argument("--decision-record-id", required=True)
        parser.add_argument("--reason", required=True)
        if command == APPLY:
            parser.add_argument("--expected-digest", required=True)
            parser.add_argument("--review-evidence-file", required=True, help="Saved release-continuation-review output.")
    read = subparsers.add_parser(READ, help="Read the Odoo promotion operation a release continuation named.")
    read.add_argument("--continuation-evidence-file", required=True, help="Saved continuation review or apply output.")
    read.add_argument("--context", required=True, help="The product's prod lane context.")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _evidence(path: str, operation: str | set[str]) -> dict[str, Any]:
    allowed = {operation} if isinstance(operation, str) else operation
    try:
        evidence = json.loads(Path(path).read_text())
        if evidence.get("status") != "ok" or evidence.get("operation") not in allowed:
            raise ValueError
        if not isinstance(evidence.get("request"), dict) or not isinstance(evidence.get("result"), dict):
            raise ValueError
        return evidence
    except (OSError, ValueError, AttributeError):
        raise ValueError("continuation_evidence_mismatch") from None


def project_continuation(h: Any, response: object, request: dict[str, object], *, apply: bool) -> dict[str, object]:
    source = h._require_dict(response)
    if set(source) != CONTINUATION_FIELDS or source["decision_record_id"] != request["decision_record_id"]:
        raise h.LaunchplaneSafetyError("invalid_response")
    if source["status"] not in ({"queued", "replayed"} if apply else {"ready"}):
        raise h.LaunchplaneSafetyError("invalid_response")
    if not isinstance(source["review_digest"], str) or not DIGEST_RE.fullmatch(source["review_digest"]):
        raise h.LaunchplaneSafetyError("invalid_response")
    if type(source["provider_mutation"]) is not bool or type(source["apply_authorized"]) is not bool:
        raise h.LaunchplaneSafetyError("invalid_response")
    return {
        "decision_record_id": h.public_identifier(source["decision_record_id"]),
        "operation_id": h.public_identifier(source["operation_id"]),
        "review_digest": source["review_digest"],
        "continuation_status": source["status"],
        "provider_mutation": source["provider_mutation"],
        "apply_authorized": source["apply_authorized"],
    }


def project_operation(h: Any, response: object, request: dict[str, object]) -> dict[str, object]:
    """The promotion family's view only; rollback or replacement views fail closed."""
    source = h._require_dict(response)
    operation = h._require_dict(source.get("operation"))
    if set(source) != h.BOUNDED_READ_RESPONSE_FIELDS[READ] or source["status"] != "accepted" or set(operation) != OPERATION_FIELDS:
        raise h.LaunchplaneSafetyError("invalid_response")
    if (operation["operation_id"], operation["product"], operation["context"], operation["instance"]) != (
        request["operation_id"], request["product"], request["context"], "prod"
    ) or operation["status"] not in OPERATION_STATUSES or operation["phase"] not in OPERATION_PHASES:
        raise h.LaunchplaneSafetyError("invalid_response")
    if isinstance(operation["attempt"], bool) or not isinstance(operation["attempt"], int):
        raise h.LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        key: h.public_identifier(operation[key]) for key in ("operation_id", "product", "context", "instance", "request_id")
    }
    projected.update(status=operation["status"], phase=operation["phase"], attempt=operation["attempt"])
    for key in ("created_at", "started_at", "updated_at", "finished_at"):
        projected[key] = h.public_summary_string(operation[key], max_length=64) if operation[key] else ""
    projected["error_code"] = h.public_code(operation["error_code"]) if operation["error_code"] else ""
    result = operation["result"]
    if result is not None:
        result = h._require_dict(result)
        projected["result"] = {key: result.get(key, "skipped") for key in RESULT_STATUSES}
        if any(value not in RESULT_STATUSES[key] for key, value in projected["result"].items()):
            raise h.LaunchplaneSafetyError("invalid_response")
        projected["result"].update({key: h.public_identifier(result[key]) for key in RESULT_IDS if result.get(key)})
    h.assert_public_safe_shape(projected)
    return projected


def finish(h: Any, *, operation: str, request: dict[str, object], status: str,
           result: dict[str, object] | None = None, diagnostics: dict[str, object] | None = None) -> int:
    output = h.base_payload(status=status, operation=operation, request=request)
    summary = {key: value for key, value in (diagnostics or {}).items() if key in PRINTED_DIAGNOSTICS}
    summary["recommendation"] = RECOVERY.get(status, RECOVERY["refused"])
    warnings = [] if status == "ok" else [h.warning(str(summary.get("error_code", status)), summary["recommendation"])]
    if result is not None:
        output["result"] = result
        if result.get("provider_mutation") is False and operation == APPLY:
            summary["recommendation"] += f" provider_mutation=false covers this call only; read back with {READ}."
        if result.get("apply_authorized") is False:
            warnings.append(h.warning("apply_not_authorized", "This identity can review but not apply this continuation."))
        if result.get("status") == "reconciliation_required":
            warnings.append(h.warning("reconciliation_required", "Effects are unknown and fenced. Do not continue or retry; use the existing reconciliation."))
    output["summary"] = summary
    output["warnings"] = warnings
    for value in output.values():
        h.assert_public_safe_shape(value)
    h.emit(output)
    return 0 if status == "ok" else 1


def _bind(h: Any, args: argparse.Namespace) -> tuple[dict[str, object], dict[str, object]]:
    """The public request binding and the service body."""
    if args.command == READ:
        evidence = _evidence(args.continuation_evidence_file, {REVIEW, APPLY})
        request = {
            "product": h.public_identifier(evidence["request"].get("product")),
            "context": h.public_identifier(args.context),
            "operation_id": h.public_identifier(evidence["result"].get("operation_id")),
            "decision_record_id": h.public_identifier(evidence["request"].get("decision_record_id")),
            "service_url_sha256": evidence["request"].get("service_url_sha256"),
        }
        return request, {}
    reason = args.reason.strip()
    if not reason or len(reason) > 1000:
        raise ValueError("invalid_continuation_reason")
    request = {
        "product": h.public_identifier(args.product),
        "decision_record_id": h.public_identifier(args.decision_record_id),
        "reason_sha256": _sha256(reason),
    }
    if args.command == REVIEW:
        return request, {"reason": reason, "expected_digest": "", "apply": False}
    if not DIGEST_RE.fullmatch(args.expected_digest):
        raise ValueError("invalid_continuation_digest")
    return request, {"reason": reason, "expected_digest": args.expected_digest, "apply": True}


def execute(h: Any, args: argparse.Namespace) -> int:
    operation = args.command
    try:
        request, body = _bind(h, args)
    except (ValueError, h.LaunchplaneSafetyError):
        return finish(h, operation=operation, request={}, status="invalid", diagnostics={"error_code": "continuation_evidence_mismatch"})
    settings = h.prepare_operator_settings(args=args, operation=operation, request=request)
    if settings is None:
        return 2
    url_digest = _sha256(settings["service_url"])
    reviewed_operation = ""
    if operation == READ and request.pop("service_url_sha256") != url_digest:
        return finish(h, operation=operation, request=request, status="invalid", diagnostics={"error_code": "continuation_evidence_mismatch"})
    if operation != READ:
        request["service_url_sha256"] = url_digest
    if operation == APPLY:
        try:
            review = _evidence(args.review_evidence_file, REVIEW)
            if review["request"] != request or review["result"].get("review_digest") != args.expected_digest:
                raise ValueError("continuation_evidence_mismatch")
            reviewed_operation = str(review["result"].get("operation_id"))
        except ValueError:
            return finish(h, operation=operation, request=request, status="invalid", diagnostics={"error_code": "continuation_evidence_mismatch"})
    timeout = getattr(args, "timeout", 30)
    try:
        if operation == READ:
            path = h.helper_command_path(READ).format(operation_id=urllib.parse.quote(str(request["operation_id"]), safe=""))
            response = h.request_launchplane_read(
                service_url=settings["service_url"], path=path, settings=settings,
                query={"product": request["product"], "context": request["context"]}, timeout=timeout, no_store=True,
            )
            result = project_operation(h, response, request)
        else:
            path = h.helper_command_path(operation).format(
                product=urllib.parse.quote(str(request["product"]), safe=""),
                decision_record_id=urllib.parse.quote(str(request["decision_record_id"]), safe=""),
            )
            response = h.request_launchplane(service_url=settings["service_url"], path=path, settings=settings, body=body, timeout=timeout)
            result = project_continuation(h, response, request, apply=operation == APPLY)
            if operation == APPLY and (result["review_digest"] != body["expected_digest"] or result["operation_id"] != reviewed_operation):
                raise h.LaunchplaneSafetyError("invalid_response")
        return finish(h, operation=operation, request=request, status="ok", result=result, diagnostics=h.service_response_diagnostics(response))
    except urllib.error.HTTPError as error:
        try:
            payload = h._error_payload_from_http(error)
            diagnostics = h.summarize_http_error(operation=operation, request=request, exc=error, provider_payload=payload).get("summary", {})
            code = diagnostics.get("error_code")
            if operation == APPLY and (error.code >= 500 or error.code == 408):
                status = "outcome_unknown"
            elif code == "backup_continuation_review_changed":
                status = "stale_review"
            elif error.code == 404:
                status = "not_found"
            else:
                status = "refused"
            return finish(h, operation=operation, request=request, status=status, diagnostics=diagnostics)
        finally:
            error.close()
    except (ValueError, OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException, h.ServiceResponseError, h.LaunchplaneSafetyError) as error:
        safety = isinstance(error, h.LaunchplaneSafetyError)
        status = "outcome_unknown" if operation == APPLY else "invalid_response" if safety else "unavailable"
        return finish(h, operation=operation, request=request, status=status, diagnostics={"error_code": "continuation_unverified"})
