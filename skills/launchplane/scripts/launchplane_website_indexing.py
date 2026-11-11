# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import argparse
import hashlib
import http.client
import os
import urllib.error
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from launchplane_native_evidence import _closed_object
from launchplane_safety import LaunchplaneSafetyError
from launchplane_testing_state import write_staged

PREFIX = "odoo-website-indexing-"
MODES = ("dry-run", "apply", "readback")
COMMANDS = frozenset(PREFIX + mode for mode in MODES)
TIMEOUT_SECONDS = 180.0
PLAN_FIELDS = {"binding_sha256", "database_uuid", "native", "plan_sha256", "committed"}
NATIVE_FIELDS = {
    "website_id",
    "public_origin",
    "production_use",
    "field",
    "before_sha256",
    "after_sha256",
    "changed",
    "managed_noindex_before",
    "managed_noindex_after",
}
RECOVERY = (
    "Preserve the original private request, full reviewed plan, receipt and Idempotency-Key. "
    "Read back through odoo-website-indexing-readback. Readback alone does not settle the original apply. "
    "Use only the service's durable reconciliation with that identical apply and original key; never retry automatically "
    "or replace the key. The exact bound native after-image proves completion; only the unchanged reviewed before-image "
    "allows the service to retry that original operation. Other images remain unknown. Current route authority remains required."
)


def add_parser(subparsers: Any) -> None:
    for mode in MODES:
        parser = subparsers.add_parser(
            PREFIX + mode,
            help="Configure only native Odoo website indexing through the service.",
        )
        parser.add_argument(
            "--payload-file",
            required=True,
            help="Private typed indexing-only JSON envelope.",
        )
        parser.add_argument(
            "--result-file",
            required=True,
            help="New private file for the complete typed service plan or receipt.",
        )
        if mode == "apply":
            parser.add_argument("--idempotency-key", required=True)
            parser.add_argument(
                "--reviewed-plan-file",
                help="Saved private dry-run result; alternative to embedded reviewed_plan.",
            )
        else:
            parser.set_defaults(idempotency_key="")


def origin(value: object) -> str:
    if not isinstance(value, str):
        raise LaunchplaneSafetyError("invalid_indexing_origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("invalid_indexing_origin") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in value)
        or port is not None
        and not 0 < port <= 65535
    ):
        raise ValueError("invalid_indexing_origin")
    return value.rstrip("/")


def validate_plan(h: Any, value: object) -> dict[str, Any]:
    source = h._require_exact_fields(value, PLAN_FIELDS)
    native = h._require_exact_fields(source["native"], NATIVE_FIELDS)
    for digest in (
        source["binding_sha256"],
        source["plan_sha256"],
        native["before_sha256"],
        native["after_sha256"],
    ):
        h._project_sha256(digest)
    if not isinstance(source["database_uuid"], str) or not source["database_uuid"]:
        raise h.LaunchplaneSafetyError("invalid_response")
    if type(native["website_id"]) is not int or native["website_id"] < 1 or native["field"] != "custom_code_head":
        raise h.LaunchplaneSafetyError("invalid_response")
    origin(native["public_origin"])
    if native["production_use"] not in ("prelaunch", "live") or any(
        type(value) is not bool
        for value in (
            source["committed"],
            native["changed"],
            native["managed_noindex_before"],
            native["managed_noindex_after"],
        )
    ):
        raise h.LaunchplaneSafetyError("invalid_response")
    if native["managed_noindex_after"] is not (native["production_use"] == "prelaunch"):
        raise h.LaunchplaneSafetyError("invalid_response")
    return source


def destination(h: Any, path: str) -> None:
    if not path.strip() or path == "-" or "\0" in path:
        raise ValueError("private_request_unavailable")
    target = h.path_with_resolved_parent(Path(path).expanduser())
    if h._is_relative_to(target, h.active_repo_root()):
        raise ValueError("repo_local_payload_unsupported")
    if not target.parent.is_dir() or not os.access(target.parent, os.W_OK | os.X_OK) or os.path.lexists(target):
        raise ValueError("private_request_unavailable")


def intent(h: Any, args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, object]]:
    body = _closed_object(
        h.read_payload_file(args.payload_file),
        {"product", "post_deploy"},
        {"schema_version"},
    )
    post = _closed_object(
        body["post_deploy"],
        {"context", "instance", "website_indexing"},
        {"schema_version", "reason"},
    )
    indexing = _closed_object(
        post["website_indexing"],
        {"public_origin"},
        {"mode", "website_id", "reviewed_plan"},
    )
    for obj in (body, post):
        if "schema_version" in obj and (type(obj["schema_version"]) is not int or obj["schema_version"] < 1):
            raise ValueError("invalid_indexing_request")
    for value in (body["product"], post["context"]):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("invalid_indexing_request")
    mode = args.command.removeprefix(PREFIX)
    indexing.setdefault("mode", mode)
    if indexing["mode"] != mode or post["instance"] != "prod":
        raise ValueError("invalid_indexing_request")
    origin(indexing["public_origin"])
    website_id = indexing.get("website_id")
    if website_id is not None and (type(website_id) is not int or website_id < 1):
        raise ValueError("invalid_indexing_request")
    reason = post.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 500:
        raise ValueError("invalid_indexing_request")
    if getattr(args, "reviewed_plan_file", None):
        if indexing.get("reviewed_plan") is not None:
            raise ValueError("ambiguous_indexing_review")
        saved = h.read_payload_file(args.reviewed_plan_file)
        indexing["reviewed_plan"] = saved.get("result")
    reviewed = indexing.get("reviewed_plan")
    if reviewed is not None:
        reviewed = validate_plan(h, reviewed)
        if origin(reviewed["native"]["public_origin"]) != origin(indexing["public_origin"]) or (
            website_id is not None and reviewed["native"]["website_id"] != website_id
        ):
            raise ValueError("indexing_review_selector_mismatch")
    if mode == "apply":
        h._require_idempotency(args)
        if reviewed is None or not reason.strip():
            raise ValueError("indexing_review_required")
    destination(h, args.result_file)
    request = {
        "product": h.public_identifier(body["product"]),
        "context": h.public_identifier(post["context"]),
        "instance": "prod",
        "mode": mode,
        "payload_source": "private_file",
    }
    return body, request


def checked_response(h: Any, value: object, body: dict[str, Any]) -> dict[str, Any]:
    response = _closed_object(
        value,
        {"status", "trace_id", "records", "result"},
        {"replayed", "original_trace_id"},
    )
    if (
        response["status"] != "accepted"
        or response["records"] != {}
        or h.public_trace_id(response["trace_id"]) != response["trace_id"]
    ):
        raise h.LaunchplaneSafetyError("invalid_response")
    if "replayed" in response and type(response["replayed"]) is not bool:
        raise h.LaunchplaneSafetyError("invalid_response")
    if "original_trace_id" in response and h.public_trace_id(response["original_trace_id"]) != response["original_trace_id"]:
        raise h.LaunchplaneSafetyError("invalid_response")
    result = validate_plan(h, response["result"])
    indexing = body["post_deploy"]["website_indexing"]
    native = result["native"]
    if (
        origin(native["public_origin"]) != origin(indexing["public_origin"])
        or (indexing.get("website_id") is not None and native["website_id"] != indexing["website_id"])
        or result["committed"] is not (indexing["mode"] == "apply")
    ):
        raise h.LaunchplaneSafetyError("invalid_response")
    if indexing["mode"] == "apply" and result != {
        **indexing["reviewed_plan"],
        "committed": True,
    }:
        raise h.LaunchplaneSafetyError("invalid_response")
    return response


def project(result: dict[str, Any]) -> dict[str, object]:
    native = result["native"]
    return {
        **{key: result[key] for key in ("binding_sha256", "plan_sha256", "committed")},
        "database_uuid_sha256": hashlib.sha256(result["database_uuid"].encode()).hexdigest(),
        "native": {
            **{key: value for key, value in native.items() if key != "public_origin"},
            "public_origin_sha256": hashlib.sha256(native["public_origin"].encode()).hexdigest(),
        },
    }


def execute(h: Any, args: argparse.Namespace) -> int:
    body, request = intent(h, args)
    settings = h.prepare_operator_settings(args=args, operation=args.command, request=request)
    if settings is None:
        return 2
    mode = request["mode"]
    output = h.base_payload(status="unavailable", operation=args.command, request=request)
    response = None
    try:
        response = h.request_launchplane(
            service_url=settings["service_url"],
            path=h.helper_command_path(args.command),
            settings=settings,
            body=body,
            timeout=args.timeout,
            idempotency_key=args.idempotency_key,
        )
        checked = checked_response(h, response, body)
        output["result"] = project(checked["result"])
        summary = h.service_response_diagnostics(response)
        summary.update({key: checked[key] for key in ("replayed", "original_trace_id") if key in checked})
        output.update(status="accepted", summary=summary)
        try:
            write_staged(h, args.result_file, checked)
        except ValueError:
            output["status"] = "accepted_unverified"
            summary["error_code"] = "private_result_unavailable"
        else:
            summary["private_result_saved"] = True
    except urllib.error.HTTPError as error:
        try:
            try:
                output = h.summarize_http_error(operation=args.command, request=request, exc=error)
            except h.LaunchplaneSafetyError:
                output["summary"] = h.service_response_diagnostics(error)
            if mode == "apply" and error.code not in {400, 401, 403, 404}:
                output["status"] = "outcome_unknown"
        finally:
            error.close()
    except (h.LaunchplaneSafetyError, ValueError) as error:
        output["status"] = "accepted_unverified" if mode == "apply" and response is not None else "invalid"
        output["summary"] = h.service_response_diagnostics(error if isinstance(error, h.ServiceResponseError) else response)
        output["summary"]["projection_error_code"] = "indexing_response_unverified"
    except (
        OSError,
        TimeoutError,
        urllib.error.URLError,
        http.client.HTTPException,
    ) as error:
        output["status"] = "outcome_unknown" if mode == "apply" else "unavailable"
        output["summary"] = h.service_response_diagnostics(error)
    if mode == "apply" and output["status"] == "invalid":
        output["status"] = "accepted_unverified"
    summary = output["summary"]
    summary["recommendation"] = (
        "Review the full private plan and its exact native binding before apply. "
        if mode == "dry-run"
        else "Compare the fresh private native baseline with the retained committed receipt. "
        if mode == "readback"
        else "Retain the committed private receipt and verify fresh native readback. "
    ) + RECOVERY
    if summary.get("error_code") == "private_result_unavailable":
        summary["recommendation"] = (
            "The private result could not be saved. Preserve this verified summary; use a new private destination for supported original-key reconciliation. "
            + RECOVERY
        )
    for value in output.values():
        h.assert_public_safe_shape(value)
    h.emit(output)
    return 0 if output["status"] == "accepted" else 1
