# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Release a failed native testing candidate's storage after a reviewed dry run."""

from __future__ import annotations

import argparse
import http.client
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse
from typing import Any

PREFIX = "testing-candidate-release-"
COMMANDS = frozenset(PREFIX + mode for mode in ("dry-run", "apply", "readback"))
DIGEST_RE = re.compile(r"[a-f0-9]{64}")
STATUSES = {"eligible", "absent", "released"}

REFUSAL_STATUS = {
    "candidate_release_plan_changed": "stale_plan",
    "idempotency_key_reused": "conflict",
    "release_receipt_unavailable": "not_started",
    "mutation_reconciliation_required": "outcome_unknown",
}

RECOVERY = {
    "ok": "The operation record, failure receipts and evidence are kept. Measure free space before another restore.",
    "refused": "Nothing was released. The refusal names what protects the candidate; resolve that, then plan a new dry run. Do not remove containers or volumes by hand.",
    "stale_plan": "Nothing was released. Save and review a new dry run before apply.",
    "conflict": "This request was not dispatched. The key belongs to a different release request; use the request it was made for.",
    "not_started": "No release exists for this key. Apply the reviewed dry run with this key first.",
    "outcome_unknown": "The release may have started. Keep the reviewed digest and key. Run testing-candidate-release-readback; if it is still unsettled, apply again with the same digest and key to finish it. Do not start over with a new key.",
    "unavailable": "The service could not plan the release; nothing was released. Inspect the trace and retry the dry run.",
    "invalid": "This request was not dispatched. Save and review a dry run of this exact request before apply.",
}


def add_parser(subparsers: Any) -> None:
    for command in sorted(COMMANDS):
        parser = subparsers.add_parser(
            command, help="Release a failed native testing candidate's containers and volumes."
        )
        parser.add_argument("--product", required=True)
        parser.add_argument("--operation-id", required=True)
        if not command.endswith("-dry-run"):
            parser.add_argument("--expected-plan-digest", required=True)
            parser.add_argument("--idempotency-key", required=True)
        if command.endswith("-apply"):
            parser.add_argument("--reviewed-dry-run", required=True, action="store_true")
            parser.add_argument("--dry-run-evidence-file", required=True)


def intent(h: Any, args: argparse.Namespace) -> tuple[dict[str, object], dict[str, object]]:
    mode = args.command.removeprefix(PREFIX)
    request: dict[str, object] = {
        "product": h.public_identifier(args.product),
        "operation_id": h.public_identifier(args.operation_id),
        "mode": mode,
    }
    body: dict[str, object] = {"mode": mode}
    if mode != "dry-run":
        if not DIGEST_RE.fullmatch(args.expected_plan_digest):
            raise ValueError("invalid_candidate_release_review")
        h._require_idempotency(args)
        body["reviewed_plan_sha256"] = args.expected_plan_digest
        request["reviewed_plan_sha256"] = args.expected_plan_digest
    h.assert_public_safe_shape(request)
    return request, body


def project(h: Any, response: object, request: dict[str, object]) -> dict[str, object]:
    source = h._require_dict(response)
    digest, status, size = (source.get(key) for key in ("plan_sha256", "status", "reclaimable_bytes"))
    if (
        not isinstance(digest, str)
        or not DIGEST_RE.fullmatch(digest)
        or status not in STATUSES
        or (size is not None and (type(size) is not int or size < 0))
        or source.get("product") != request["product"]
        or source.get("operation_id") != request["operation_id"]
        or not isinstance(source.get("error_code"), str)
        or request["mode"] == "dry-run" and status == "released"
        or request["mode"] != "dry-run" and status == "eligible"
        or request["mode"] != "dry-run" and digest != request["reviewed_plan_sha256"]
    ):
        raise h.LaunchplaneSafetyError("invalid_response")
    projected = {
        "status": status,
        "plan_sha256": digest,
        "reclaimable_bytes": size,
        "failure_code": h.public_identifier(source["error_code"]),
    }
    if source.get("replayed") is True:
        projected["replayed"] = True
    h.assert_public_safe_shape(projected)
    return projected


def reviewed(evidence_file: str, request: dict[str, object]) -> bool:
    try:
        evidence = json.loads(Path(evidence_file).read_text())
        result = evidence["result"]
        return (
            evidence.get("status") == "ok"
            and evidence.get("operation") == PREFIX + "dry-run"
            and {key: evidence["request"].get(key) for key in ("product", "operation_id")}
            == {key: request[key] for key in ("product", "operation_id")}
            and result.get("status") == "eligible"
            and result.get("plan_sha256") == request["reviewed_plan_sha256"]
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def finish(h: Any, *, operation: str, request: dict[str, object], status: str,
           result: dict[str, object] | None = None, diagnostics: dict[str, object] | None = None) -> int:
    output = h.base_payload(status=status, operation=operation, request=request)
    summary = {key: value for key, value in (diagnostics or {}).items() if key != "error_reason"}
    summary["recommendation"] = RECOVERY[status]
    output["summary"] = summary
    if result is not None:
        output["result"] = result
    if status != "ok":
        output["warnings"] = [h.warning(str(summary.get("error_code", status)), RECOVERY[status])]
    for value in output.values():
        h.assert_public_safe_shape(value)
    h.emit(output)
    return 0 if status == "ok" else 1


def execute(h: Any, args: argparse.Namespace) -> int:
    operation = args.command
    request, body = intent(h, args)
    mode = request["mode"]
    if mode == "apply" and not reviewed(args.dry_run_evidence_file, request):
        return finish(h, operation=operation, request=request, status="invalid",
                      diagnostics={"error_code": "candidate_release_review_evidence_mismatch"})
    settings = h.prepare_operator_settings(args=args, operation=operation, request=request)
    if settings is None:
        return 2
    path = h.helper_command_path(operation).format(
        product=urllib.parse.quote(str(request["product"]), safe=""),
        operation_id=urllib.parse.quote(str(request["operation_id"]), safe=""),
    )
    try:
        response = h.request_launchplane(
            service_url=settings["service_url"], path=path, settings=settings, body=body,
            timeout=getattr(args, "timeout", 600), idempotency_key=getattr(args, "idempotency_key", ""),
        )
        return finish(h, operation=operation, request=request, status="ok",
                      result=project(h, response, request), diagnostics=h.service_response_diagnostics(response))
    except urllib.error.HTTPError as error:
        try:
            payload = h._error_payload_from_http(error)
            diagnostics = h.summarize_http_error(
                operation=operation, request=request, exc=error, provider_payload=payload
            ).get("summary", {})
            code = str(diagnostics.get("error_code", ""))
            if code in REFUSAL_STATUS:
                status = REFUSAL_STATUS[code]
            elif code == "candidate_release_unavailable":
                status = "unavailable"
            elif error.code in {403, 409}:
                status = "refused"
            else:
                status = "unavailable" if mode == "dry-run" else "outcome_unknown"
            return finish(h, operation=operation, request=request, status=status, diagnostics=diagnostics)
        finally:
            error.close()
    except (ValueError, OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException, h.ServiceResponseError):
        status = "unavailable" if mode == "dry-run" else "outcome_unknown"
        return finish(h, operation=operation, request=request, status=status,
                      diagnostics={"error_code": "candidate_release_unverified"})
