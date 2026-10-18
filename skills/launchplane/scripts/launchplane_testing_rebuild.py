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
COPY_SOURCES = {
    "empty": "empty",
    "production": "production_restore",
    "upstream": "upstream_restore",
}


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        COMMAND,
        help="Queue an independent testing rebuild at its existing address; read its operation for completion.",
    )
    parser.add_argument("--product", required=True)
    parser.add_argument("--copy-source", choices=tuple(COPY_SOURCES), required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--source-git-ref", required=True, help="Immutable artifact source commit.")
    parser.add_argument("--idempotency-key", required=True)


def intent(h: Any, args: argparse.Namespace) -> tuple[dict[str, object], dict[str, object]]:
    h._require_idempotency(args)
    product = h._required_argument(args, "product")
    artifact = h._required_argument(args, "artifact_id")
    source = h._required_argument(args, "source_git_ref")
    for field, value in (("product", product), ("artifact_id", artifact)):
        if not h.PRODUCT_READ_PATH_SEGMENT_RE.fullmatch(value):
            raise ValueError("invalid_" + field)
        h.public_identifier(value)
    if re.fullmatch(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", source) is None:
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
    }
    request: dict[str, object] = {
        **replacement,
        "payload_source": "operator_argument",
        "idempotency_key_fingerprint": h.repository_inventory_idempotency_key_fingerprint(
            args.idempotency_key
        ),
    }
    h.assert_public_safe_shape(request)
    return request, {"schema_version": 1, "product": product, "replacement": replacement}


def project_receipt(h: Any, response: dict[str, Any], body: dict[str, object]) -> dict[str, object]:
    operation = h._require_dict(response.get("result"))
    original = h._require_dict(operation.get("request"))
    replacement = h._require_dict(body["replacement"])
    if (
        response.get("status") != "accepted"
        or not h.public_identifier(operation.get("operation_id"))
        or operation.get("product") != body["product"]
        or operation.get("instance") != replacement["instance"]
        or any(original.get(key) != value for key, value in replacement.items())
    ):
        raise h.LaunchplaneSafetyError("invalid_response")
    return h._project_target_replacement_operation({"operation": operation})


def execute(h: Any, args: argparse.Namespace) -> int:
    request, body = intent(h, args)
    settings = h.prepare_operator_settings(args=args, operation=COMMAND, request=request)
    if settings is None:
        return 2
    response: dict[str, Any] | None = None
    diagnostics: dict[str, object] = {}
    try:
        response = h.request_launchplane(
            service_url=settings["service_url"],
            path=h.helper_command_path(COMMAND),
            settings=settings,
            body=body,
            timeout=args.timeout,
            idempotency_key=args.idempotency_key.strip(),
        )
        operation = h._require_dict(response.get("result"))
        if operation.get("idempotency_key") != args.idempotency_key.strip():
            raise h.LaunchplaneSafetyError("invalid_response")
        result = project_receipt(h, response, body)
        output = h.base_payload(status="accepted", operation=COMMAND, request=request)
        output["result"] = result
        output["summary"] = {
            "trace_id": h.public_trace_id(response.get("trace_id")),
            "recommendation": (
                "Read this operation with target-replacement-operation-read. Acceptance is not "
                "completion. Preserve its operation ID and original idempotency key."
            ),
            **h.service_response_diagnostics(response),
        }
        h.assert_public_safe_shape(output["summary"])
        h.emit(output)
        return 0
    except urllib.error.HTTPError as error:
        if 400 <= error.code < 500 and error.code != 408:
            h.emit_http_error_payload(operation=COMMAND, request=request, exc=error)
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
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException):
        pass
    output = h._apply_outcome_unknown(
        operation=COMMAND, request=request, label="original testing rebuild operation"
    )
    output["summary"].update(h.service_response_diagnostics(response))
    output["summary"].update(diagnostics)
    if response is not None:
        try:
            output["summary"]["trace_id"] = h.public_trace_id(response.get("trace_id"))
        except h.LaunchplaneSafetyError:
            pass
    h.emit(output)
    return 1
