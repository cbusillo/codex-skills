# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Continue the original held request through its existing service authority."""

from __future__ import annotations

import argparse
import http.client
import urllib.error
from typing import Any

COMMAND = "testing-rebuild-resume"


def execute(h: Any, args: argparse.Namespace) -> int:
    h._require_idempotency(args)
    references = {name: h._required_argument(args, name) for name in ("product", "operation_id")}
    for name, value in references.items():
        if not h.PRODUCT_READ_PATH_SEGMENT_RE.fullmatch(value):
            raise ValueError("invalid_" + name)
        h.public_identifier(value)
    updated = h.public_timestamp(h._required_argument(args, "expected_updated_at"))
    if type(args.expected_attempt) is not int or args.expected_attempt < 0:
        raise ValueError("invalid_expected_attempt")
    body = {"expected_updated_at": updated, "expected_attempt": args.expected_attempt}
    request = {**references, **body,
        "idempotency_key_fingerprint": h.repository_inventory_idempotency_key_fingerprint(args.idempotency_key)}
    h.assert_public_safe_shape(request)
    settings = h.prepare_operator_settings(args=args, operation=COMMAND, request=request)
    if settings is None:
        return 2
    diagnostics: dict[str, object] = {}
    try:
        response = h.request_launchplane(service_url=settings["service_url"],
            path=h.helper_command_path(COMMAND).format(**references), settings=settings,
            body=body, timeout=args.timeout, idempotency_key=args.idempotency_key.strip())
        operation = h._require_dict(response.get("operation"))
        if set(response) != {"trace_id", "operation"} or set(operation) != {
            "product", "operation_id", "instance", "status", "phase", "attempt", "updated_at",
        } or any(operation.get(name) != value for name, value in references.items()) or operation.get("instance") != "testing":
            raise h.LaunchplaneSafetyError("invalid_response")
        if type(operation["attempt"]) is not int or operation["attempt"] < 0:
            raise h.LaunchplaneSafetyError("invalid_response")
        projected = {**references, "instance": "testing",
            "status": h._public_dotted_code(operation["status"]),
            "phase": h._public_dotted_code(operation["phase"]),
            "attempt": operation["attempt"], "updated_at": h.public_timestamp(operation["updated_at"])}
        h.assert_public_safe_shape(projected)
        output = h.base_payload(status="accepted", operation=COMMAND, request=request)
        output["result"] = {"operation": projected}
        output["summary"] = {**h.service_response_diagnostics(response),
            "recommendation": "Read the original operation with target-replacement-operation-read. Resume acceptance is not completion."}
        h.emit(output)
        return 0
    except urllib.error.HTTPError as error:
        try:
            if 400 <= error.code < 500 and error.code != 408:
                output = h.summarize_http_error(operation=COMMAND, request=request, exc=error)
                output["summary"]["operation_id"] = references["operation_id"]
                output["summary"]["recommendation"] = (
                    "Stop this resume action and report its denial code and trace. Keep the original request and key; do not retry through another identity or route."
                    if error.code in {401, 403} else
                    "Read the original operation before another explicit resume. Confirm deployed resume support if unavailable; do not use another route."
                )
                h.emit(output)
                return 1
            summary = h.summarize_http_error(operation=COMMAND, request=request, exc=error)["summary"]
            diagnostics = {key: value for key, value in summary.items() if key in {"trace_id", "error_code", "http_status"}}
        finally:
            error.close()
    except h.ServiceResponseError as error:
        diagnostics = h.service_response_diagnostics(error)
    except (h.LaunchplaneSafetyError, ValueError):
        pass
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException):
        pass
    output = h._apply_outcome_unknown(operation=COMMAND, request=request, label="original testing rebuild resume")
    output["summary"].update(diagnostics)
    output["summary"]["operation_id"] = references["operation_id"]
    output["summary"]["recommendation"] = "Read the original operation before another explicit resume. Keep its original key and intent; do not submit a successor or switch identity."
    h.emit(output)
    return 1
