# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema==4.26.0", "rfc3339-validator==0.1.4"]
# ///
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

PREFIX = "testing-candidate-release-"
COMMANDS = frozenset(PREFIX + mode for mode in ("dry-run", "apply", "readback"))
DIGEST_RE = re.compile(r"[a-f0-9]{64}")
RESULT_STATUSES = {
    "dry-run": {"eligible", "absent", "held"},
    "apply": {"released", "absent", "held"},
    "readback": {"released", "absent", "held"},
}
RESOURCE_STATUSES = {"eligible", "held", "removed", "absent", "unknown"}
TARGET_FIELDS = ("product", "operation_id", "receiver_operation_id")

REFUSAL_STATUS = {
    "candidate_release_plan_changed": "stale_plan",
    "idempotency_key_reused": "conflict",
    "release_receipt_unavailable": "not_started",
    "mutation_reconciliation_required": "outcome_unknown",
    "idempotency_key_required": "invalid",
    "authorization_denied": "refused",
}
CANDIDATE_REFUSALS = {
    "candidate_release_held",
    "candidate_release_no_candidate",
    "candidate_release_not_testing",
    "candidate_release_outcome_unrecorded",
    "candidate_release_selected",
    "testing_in_use",
    "testing_needed_copy",
    "testing_production_resources_shared",
    "testing_staff_hold",
}

RECOVERY = {
    "ok": "Keep the operation record, failure receipts and evidence. Check measured free space before another restore.",
    "held": "Review the candidate and resource reasons. Apply requires a new eligible plan and, if a key was used, a new key.",
    "refused": "Check the refusal and resolve what protects the candidate. If an earlier apply is unsettled, retain its original evidence and key for readback before planning again.",
    "stale_plan": "The reviewed plan changed. Read back any unsettled earlier apply with its original evidence and key before reviewing a new plan.",
    "conflict": "The key belongs to a different release request. Use that key with the exact request it was made for.",
    "not_started": "No release exists for this key. Apply the reviewed dry run with this key first.",
    "outcome_unknown": "The release may have started. Keep the original request, reviewed evidence, digest and key. Use testing-candidate-release-readback with those original arguments until the receipts establish the outcome.",
    "unavailable": "The service could not plan the release; nothing was released. Inspect the trace and retry the dry run.",
    "invalid": "Check the request arguments. Apply requires saved evidence from a reviewed dry run of the exact request.",
}


def add_parser(subparsers: Any) -> None:
    for mode in RESULT_STATUSES:
        parser = subparsers.add_parser(
            PREFIX + mode,
            help="Release a failed native testing candidate's containers and volumes.",
        )
        parser.add_argument("--product", required=True)
        parser.add_argument("--operation-id", required=True)
        parser.add_argument(
            "--receiver-operation-id",
            help="Original receiver operation to assess for capacity.",
        )
        if mode == "dry-run":
            parser.add_argument(
                "--custody-evidence-file",
                help="Request custody evidence without a receiver and save it to a new private file outside the repository.",
            )
        if mode != "dry-run":
            for flag in ("--expected-plan-digest", "--idempotency-key"):
                parser.add_argument(flag, required=True)
        if mode == "apply":
            parser.add_argument("--reviewed-dry-run", required=True, action="store_true")
            parser.add_argument("--dry-run-evidence-file", required=True)


def intent(h: Any, args: argparse.Namespace) -> tuple[dict[str, str], dict[str, object]]:
    mode = args.command.removeprefix(PREFIX)
    request = {
        "product": h.public_identifier(args.product),
        "operation_id": h.public_identifier(args.operation_id),
        "mode": mode,
    }
    body: dict[str, object] = {"mode": mode}
    if getattr(args, "custody_evidence_file", None):
        from launchplane_website_indexing import destination

        if (
            args.receiver_operation_id is not None
            or request["product"] != args.product
            or request["operation_id"] != args.operation_id
        ):
            raise ValueError("custody_evidence_receiver_unsupported")
        destination(h, args.custody_evidence_file)
        body["include_evidence"] = True
    if args.receiver_operation_id is not None:
        receiver = h.public_identifier(args.receiver_operation_id)
        if receiver != args.receiver_operation_id or len(receiver) > 200:
            raise ValueError("invalid_receiver_operation_id")
        request["receiver_operation_id"] = receiver
        body["receiver_operation_id"] = receiver
    if mode != "dry-run":
        if not DIGEST_RE.fullmatch(args.expected_plan_digest):
            raise ValueError("invalid_candidate_release_review")
        h._require_idempotency(args)
        if len(args.idempotency_key.strip()) > 200:
            raise ValueError("idempotency_key_too_long")
        body["reviewed_plan_sha256"] = args.expected_plan_digest
        request["reviewed_plan_sha256"] = args.expected_plan_digest
    h.assert_public_safe_shape(request)
    return request, body


def integer(value: object, *, signed: bool = False) -> int:
    if type(value) is not int or (not signed and value < 0):
        raise ValueError("invalid_response")
    return value


def digest_field(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or not (DIGEST_RE.fullmatch(value) or allow_empty and value == ""):
        raise ValueError("invalid_response")
    return value


def array(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("invalid_response")
    return value


def row_status(value: object) -> str:
    if not isinstance(value, str) or value not in RESOURCE_STATUSES:
        raise ValueError("invalid_response")
    return value


def optional_identifier(h: Any, value: object) -> str:
    return "" if value == "" else h.public_identifier(value)


def project_readback(h: Any, value: object) -> dict[str, str]:
    result = {}
    for predicate, outcome in h._require_dict(value).items():
        if not isinstance(outcome, str) or outcome not in {
            "pass",
            "fail",
            "unavailable",
            "skipped",
        }:
            raise ValueError("invalid_response")
        result[h.public_identifier(predicate)] = outcome
    return result


def project_reference(h: Any, value: object) -> dict[str, str]:
    row = h._require_dict(value)
    return {
        "reason": h.public_identifier(row.get("reason")),
        "kind": h.public_identifier(row.get("kind")),
        "record_id": optional_identifier(h, row.get("record_id", "")),
        "record_sha256": digest_field(row.get("record_sha256", ""), allow_empty=True),
    }


def project_automatic_release(h: Any, value: object) -> dict[str, object]:
    row = h._require_dict(value)
    status = row.get("status")
    reasons = array(row.get("reasons", []))
    if not isinstance(status, str) or status not in {"released", "absent", "held", "unknown"} or len(reasons) > 32:
        raise ValueError("invalid_response")
    return {
        "status": status,
        "reasons": [h.public_identifier(reason) for reason in reasons],
        "plan_sha256": digest_field(row.get("plan_sha256", ""), allow_empty=True),
        "idempotency_key": h.public_identifier(row.get("idempotency_key")),
    }


def project_resource(h: Any, value: object) -> dict[str, object]:
    row = h._require_dict(value)
    result: dict[str, object] = {
        "role": h.public_identifier(row.get("role")),
        "volume_identity_sha256": digest_field(row.get("volume_identity_sha256"), allow_empty=True),
        "status": row_status(row.get("status")),
        "reason": optional_identifier(h, row.get("reason")),
        "backing_sha256": digest_field(row.get("backing_sha256", ""), allow_empty=True),
    }
    if readback := project_readback(h, row.get("readback", {})):
        result["readback"] = readback
    if row.get("free_bytes") is not None:
        result["free_bytes"] = integer(row["free_bytes"])
    return result


def project_physical(h: Any, value: object) -> dict[str, object]:
    readings = {}
    for identity, measurement in h._require_dict(value).items():
        row = h._require_dict(measurement)
        readings[digest_field(identity)] = (
            {"reason": "backing_readback_unknown"}
            if not row or row.get("reason") == "backing_readback_unknown"
            else {
                "before_bytes": integer(row.get("before_bytes")),
                "after_bytes": integer(row.get("after_bytes")),
                "observed_change_bytes": integer(row.get("observed_change_bytes"), signed=True),
            }
        )
    return readings


def project_candidate(h: Any, value: object, request: dict[str, str]) -> dict[str, object]:
    row = h._require_dict(value)
    selected = row.get("selected")
    if (
        row.get("instance") != "testing"
        or row.get("product") != request["product"]
        or selected is not (row.get("operation_id") == request["operation_id"])
    ):
        raise ValueError("invalid_response")
    identities = (
        "namespace",
        "operation_id",
        "product",
        "context",
        "instance",
    )
    digests = (
        "ownership_sha256",
        "source_evidence_sha256",
        "provider_evidence_sha256",
        "lifecycle_snapshot_sha256",
    )
    result: dict[str, object] = {
        **{key: h.public_identifier(row.get(key)) for key in identities},
        **{key: digest_field(row.get(key, ""), allow_empty=True) for key in digests},
        "target_id": optional_identifier(h, row.get("target_id")),
        "selected": selected,
        "status": row_status(row.get("status")),
        "reasons": [h.public_identifier(reason) for reason in array(row.get("reasons"))],
        "container_count": integer(row.get("container_count", 0)),
        "resources": [project_resource(h, item) for item in array(row.get("resources", []))],
        "physical_readback": project_physical(h, row.get("physical_readback", {})),
    }

    if references := [project_reference(h, item) for item in array(row.get("references", []))]:
        result["references"] = references
    if readback := project_readback(h, row.get("readback", {})):
        result["readback"] = readback
    return result


def project_capacity(h: Any, value: object) -> dict[str, object]:
    row = h._require_dict(value)
    return {
        "backing_sha256": digest_field(row.get("backing_sha256")),
        **{
            key: integer(row.get(key))
            for key in (
                "free_bytes",
                "required_additional_bytes",
                "reserve_bytes",
                "deficit_bytes",
            )
        },
    }


def project(h: Any, response: object, request: dict[str, str]) -> dict[str, object]:
    source = h._require_dict(response)
    digest = digest_field(source.get("plan_sha256"))
    status, size = source.get("status"), source.get("reclaimable_bytes")
    mode = request["mode"]
    if (
        not isinstance(status, str)
        or status not in RESULT_STATUSES[mode]
        or source.get("product") != request["product"]
        or source.get("operation_id") != request["operation_id"]
        or mode != "dry-run"
        and digest != request["reviewed_plan_sha256"]
    ):
        raise ValueError("invalid_response")
    projected: dict[str, object] = {
        "status": status,
        "plan_sha256": digest,
        "reclaimable_bytes": None if size is None else integer(size),
        "failure_code": optional_identifier(h, source.get("error_code")),
    }
    if source.get("reason_code") is not None:
        reason = h.public_identifier(source["reason_code"])
        if len(reason) > 200:
            raise ValueError("invalid_response")
        projected["reason_code"] = reason
    if source.get("replayed") is True:
        projected["replayed"] = True
    if "candidates" in source:
        projected["candidates"] = [project_candidate(h, item, request) for item in array(source["candidates"])]
    if "capacity" in source:
        projected["capacity"] = [project_capacity(h, item) for item in array(source["capacity"])]
    if "capacity_refusal" in source:
        projected["capacity_refusal"] = optional_identifier(h, source["capacity_refusal"])
    h.assert_public_safe_shape(projected)
    return projected


def without_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [without_nulls(item) for item in value]
    return value


def retain_custody(h: Any, response: dict[str, Any], request: dict[str, str], path: str) -> dict[str, object]:
    from jsonschema import Draft202012Validator, FormatChecker, validators
    from launchplane_testing_state import write_staged

    schema_path = Path(__file__).resolve().parent.parent / "references" / "candidate-custody-evidence.schema.json"
    packet = response.get("evidence")
    checker = Draft202012Validator.TYPE_CHECKER.redefine(
        "integer",
        lambda _checker, value: type(value) is int and -(2**63) <= value < 2**63,
    )
    validator = validators.extend(Draft202012Validator, type_checker=checker)(
        json.loads(schema_path.read_text()), format_checker=FormatChecker()
    )
    if not isinstance(packet, dict) or not validator.is_valid(packet):
        raise ValueError("invalid_custody_evidence")
    trace = response.get("trace_id")
    if (
        not isinstance(trace, str)
        or not trace
        or h.public_identifier(trace) != trace
        or len(trace) > 200
        or response.get("product") != request["product"]
        or response.get("operation_id") != request["operation_id"]
        or packet.get("plan_sha256") != response.get("plan_sha256")
        or not packet.get("completed_at")
    ):
        raise ValueError("invalid_custody_binding")
    canonical = json.dumps(
        without_nulls({key: value for key, value in packet.items() if key != "packet_sha256"}),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    packet_hash = hashlib.sha256(canonical).hexdigest()
    if packet.get("packet_sha256") != packet_hash:
        raise ValueError("invalid_custody_hash")
    saved = {key: response[key] for key in ("product", "operation_id", "trace_id", "plan_sha256", "status")}
    saved["evidence"] = packet
    saved["release_review"] = project(h, response, request)
    reference = str(h.path_with_resolved_parent(Path(path).expanduser()))
    public_reference = h.public_identifier(reference, pattern=re.compile(r"/[^\x00-\x1f\x7f]+"))
    if public_reference != reference:
        raise ValueError("invalid_custody_reference")
    summary = {
        "status": response["status"],
        "plan_sha256": response["plan_sha256"],
        "custody_evidence_file": public_reference,
        "packet_sha256": packet_hash,
        "outcomes": custody_outcomes(packet),
    }
    h.assert_public_safe_shape(summary)
    write_staged(h, path, saved)
    summary["file_sha256"] = hashlib.sha256(Path(reference).read_bytes()).hexdigest()
    return summary


def custody_outcomes(value: object) -> dict[str, int]:
    counts = dict.fromkeys(("pass", "fail", "unavailable", "not_reached", "not_applicable"), 0)

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key == "checks":
                    for outcome in item.values():
                        counts[outcome] += 1
                else:
                    collect(item)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    collect(value)
    return counts


def reviewed(evidence_file: str, request: dict[str, str]) -> bool:
    try:
        evidence = json.loads(Path(evidence_file).read_text())
        result = evidence["result"]
        return (
            evidence.get("status") == "ok"
            and evidence.get("operation") == PREFIX + "dry-run"
            and all(evidence["request"].get(key) == request.get(key) for key in TARGET_FIELDS)
            and result.get("status") == "eligible"
            and result.get("plan_sha256") == request["reviewed_plan_sha256"]
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def finish(
    h: Any,
    *,
    operation: str,
    request: dict[str, str],
    status: str,
    result: dict[str, object] | None = None,
    diagnostics: dict[str, object] | None = None,
) -> int:
    output = h.base_payload(status=status, operation=operation, request=request)
    summary = dict(diagnostics or {})
    summary["recommendation"] = RECOVERY[status]
    output["summary"] = summary
    if result is not None:
        output["result"] = result
    if status != "ok":
        error_code = summary.get("error_code")
        output["warnings"] = [h.warning(error_code if isinstance(error_code, str) else status, RECOVERY[status])]
    for value in output.values():
        h.assert_public_safe_shape(value)
    h.emit(output)
    return 0 if status == "ok" else 1


def execute(h: Any, args: argparse.Namespace) -> int:
    operation = args.command
    request, body = intent(h, args)
    mode = request["mode"]
    if mode == "apply" and not reviewed(args.dry_run_evidence_file, request):
        return finish(
            h,
            operation=operation,
            request=request,
            status="invalid",
            diagnostics={"error_code": "candidate_release_review_evidence_mismatch"},
        )
    settings = h.prepare_operator_settings(args=args, operation=operation, request=request)
    if settings is None:
        return 2
    path = h.helper_command_path(operation).format(
        product=urllib.parse.quote(request["product"], safe=""),
        operation_id=urllib.parse.quote(request["operation_id"], safe=""),
    )
    try:
        response = h.request_launchplane(
            service_url=settings["service_url"],
            path=path,
            settings=settings,
            body=body,
            timeout=args.timeout,
            idempotency_key=getattr(args, "idempotency_key", ""),
            **({"no_store": True} if body.get("include_evidence") else {}),
        )
        result = project(h, response, request)
        if getattr(args, "custody_evidence_file", None):
            result = retain_custody(h, response, request, args.custody_evidence_file)
        status = "held" if result["status"] == "held" else "ok"
        return finish(
            h,
            operation=operation,
            request=request,
            status=status,
            result=result,
            diagnostics=h.service_response_diagnostics(response),
        )
    except urllib.error.HTTPError as error:
        try:
            payload = h._error_payload_from_http(error)
            diagnostics = h.summarize_http_error(
                operation=operation,
                request=request,
                exc=error,
                provider_payload=payload,
            ).get("summary", {})
            code = diagnostics.get("error_code")
            status = "unavailable" if mode == "dry-run" else "outcome_unknown"
            if isinstance(code, str):
                if code in REFUSAL_STATUS:
                    status = REFUSAL_STATUS[code]
                elif mode != "readback" and code in CANDIDATE_REFUSALS:
                    status = "refused"
            return finish(
                h,
                operation=operation,
                request=request,
                status=status,
                diagnostics=diagnostics,
            )
        finally:
            error.close()
    except (ValueError, OSError, http.client.HTTPException, h.ServiceResponseError):
        status = "unavailable" if mode == "dry-run" else "outcome_unknown"
        return finish(
            h,
            operation=operation,
            request=request,
            status=status,
            diagnostics={"error_code": "candidate_release_unverified"},
        )
