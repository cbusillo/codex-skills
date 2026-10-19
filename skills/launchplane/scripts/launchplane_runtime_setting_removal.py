# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Director-agent dry run and readback of reviewed stored runtime-setting removal; no apply."""

from __future__ import annotations

import argparse
import http.client
import re
import urllib.error
from typing import Any

COMMAND_MODES = {
    "runtime-setting-removal-dry-run": "dry-run",
    "runtime-setting-removal-readback": "readback",
}
COMMANDS = frozenset(COMMAND_MODES)
SCOPES = ("global", "context", "instance")
# Mirrors Launchplane's RuntimeSettingRemovalRequest and printable_key
# (control_plane/runtime_setting_removal.py).
MAX_KEYS = 256
MAX_REASON = 1000
MAX_ID_LENGTH = 256
KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}")
TOKEN_RE = re.compile(r"(?:gh[pousr]_|github_pat_|sk-|AKIA)[A-Za-z0-9_-]{12,}")
HEX64_RE = re.compile(r"[a-f0-9]{64}")
REVIEW_ID_PREFIX = "runtime-removal-"
REVIEW_ID_RE = re.compile(REVIEW_ID_PREFIX + r"[a-f0-9]{64}")
GLOBAL_PRODUCT = "launchplane"
# Bound on printed surviving names; the rest are counted, not listed.
SURVIVOR_CAP = 500
RESPONSE_FIELDS = frozenset({"status", "trace_id", "result"})
DIGEST_FIELDS = ("record_sha256", "authority_sha256", "plan_sha256")
FLAG_FIELDS = ("record_deleted", "requires_compatible_readers_and_rollback")
COUNT_FIELDS = ("unprintable_survivor_count", "surviving_value_count", "retirement_intent_count")
READBACK_FIELDS = frozenset({"readback_matches", "current_record_present"})
APPLY_ONLY_FIELDS = frozenset({"applied_at", "audit_review_id", "replayed"})
RESULT_FIELDS = frozenset({
    "product", "scope", "context", "instance", "removed_keys", "updated_at", "surviving_keys",
    "applied", "review_id", *DIGEST_FIELDS, *FLAG_FIELDS, *COUNT_FIELDS, *READBACK_FIELDS,
})
STALE_CODES = frozenset({"runtime_setting_removal_unverified"})
STALE_RECOMMENDATION = (
    "The record, its keys, ownership or policy changed, or the review does not match this request. "
    "Refresh the names-only runtime-settings read and run a fresh dry run. For readback, reuse the "
    "dry run's exact keys, reason, review id and plan digest."
)


def add_parser(subparsers: Any) -> None:
    for command, mode in COMMAND_MODES.items():
        parser = subparsers.add_parser(
            command,
            help=f"Runtime-setting removal {mode} by names only; apply belongs to the signed-in admin.",
        )
        parser.add_argument("--product", required=True)
        parser.add_argument("--scope", required=True, choices=SCOPES)
        parser.add_argument("--context", default="")
        parser.add_argument("--instance", default="")
        parser.add_argument("--key", action="append", required=True, help="Stored setting name; repeat per key.")
        parser.add_argument("--reason", required=True, help="Private review reason; never printed.")
        if mode == "readback":
            parser.add_argument("--review-id", required=True)
            parser.add_argument("--plan-sha256", required=True)


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and HEX64_RE.fullmatch(value) is not None


def _printable_key(value: object) -> bool:
    return (isinstance(value, str) and KEY_RE.fullmatch(value) is not None
            and TOKEN_RE.search(value) is None)


def _bounded_identifier(h: Any, value: str, code: str) -> str:
    try:
        projected = h.public_identifier(value)
    except h.LaunchplaneSafetyError:
        raise ValueError(code) from None
    if projected != value or len(projected) > MAX_ID_LENGTH:
        raise ValueError(code)
    return projected


def intent(h: Any, args: argparse.Namespace) -> tuple[dict[str, object], dict[str, object]]:
    mode = COMMAND_MODES[args.command]
    product = _bounded_identifier(h, args.product, "invalid_runtime_setting_removal_product")
    scope, context, instance = args.scope, args.context, args.instance
    if scope == "global":
        if product != GLOBAL_PRODUCT or context or instance:
            raise ValueError("invalid_runtime_setting_removal_scope")
    elif not context or (scope == "context" and instance) or (scope == "instance" and not instance):
        raise ValueError("invalid_runtime_setting_removal_scope")
    if context:
        _bounded_identifier(h, context, "invalid_runtime_setting_removal_context")
    if instance:
        _bounded_identifier(h, instance, "invalid_runtime_setting_removal_instance")
    keys = list(args.key)
    if not 1 <= len(keys) <= MAX_KEYS or len(set(keys)) != len(keys):
        raise ValueError("invalid_runtime_setting_removal_keys")
    if not all(_printable_key(key) for key in keys):
        raise ValueError("invalid_runtime_setting_removal_keys")
    reason = args.reason
    # Readback must repeat the saved reason exactly, so accept what the service accepts.
    if not reason or len(reason) > MAX_REASON:
        raise ValueError("invalid_runtime_setting_removal_reason")
    body: dict[str, object] = {
        "mode": mode, "product": product, "scope": scope, "context": context,
        "instance": instance, "keys": keys, "reason": reason,
    }
    request: dict[str, object] = {
        "mode": mode, "product": product, "scope": scope, "context": context,
        "instance": instance, "keys": keys, "payload_source": "operator_argument",
    }
    if mode == "readback":
        if REVIEW_ID_RE.fullmatch(args.review_id) is None:
            raise ValueError("invalid_runtime_setting_removal_review_id")
        if not _is_hex64(args.plan_sha256):
            raise ValueError("invalid_runtime_setting_removal_plan_sha256")
        body["review_id"] = args.review_id
        body["reviewed_plan_sha256"] = args.plan_sha256
        request["review_id"] = args.review_id
    try:
        h.assert_public_safe_shape(request)
    except h.LaunchplaneSafetyError:
        raise ValueError("invalid_runtime_setting_removal_keys") from None
    return request, body


def _surviving_keys(h: Any, value: object, drops: Any) -> list[str]:
    if not isinstance(value, list):
        raise h.LaunchplaneSafetyError("invalid_response")

    def validate(candidate: object) -> str:
        if not isinstance(candidate, str) or KEY_RE.fullmatch(candidate) is None:
            raise h.LaunchplaneSafetyError("invalid_response")
        return candidate

    kept: list[str] = []
    for name in value:
        # The service prints only non-token names; a token-shaped one is drift, not an odd name.
        if isinstance(name, str) and TOKEN_RE.search(name):
            raise h.LaunchplaneSafetyError("invalid_response")
        projected = drops.keep("result.surviving_keys", validate, name)
        if projected:
            kept.append(str(projected))
        elif name in (None, ""):
            drops.drop("result.surviving_keys")
    return kept


def project(h: Any, response: object, body: dict[str, Any]) -> tuple[dict[str, object], dict[str, object]]:
    source = h._require_dict(response)
    if source.keys() - RESPONSE_FIELDS or source.get("status") != "ok":
        raise h.LaunchplaneSafetyError("invalid_response")
    result = h._require_dict(source.get("result"))
    readback = body["mode"] == "readback"
    if (any(result.get(key) != body[key] for key in ("product", "scope", "context", "instance"))
            or not isinstance(result.get("removed_keys"), list)
            or not all(isinstance(key, str) for key in result["removed_keys"])
            or sorted(result["removed_keys"]) != sorted(body["keys"])
            or result.get("applied") is not False
            or APPLY_ONLY_FIELDS & result.keys()
            or any(not _is_hex64(result.get(key)) for key in DIGEST_FIELDS)
            or result.get("review_id") != REVIEW_ID_PREFIX + result["plan_sha256"]
            or (READBACK_FIELDS & result.keys()) != (READBACK_FIELDS if readback else frozenset())):
        raise h.LaunchplaneSafetyError("invalid_response")
    if readback and (result["review_id"] != body["review_id"]
                     or result["plan_sha256"] != body["reviewed_plan_sha256"]):
        raise h.LaunchplaneSafetyError("invalid_response")
    flags = (*FLAG_FIELDS, *(sorted(READBACK_FIELDS) if readback else ()))
    if any(type(result.get(key)) is not bool for key in flags):
        raise h.LaunchplaneSafetyError("invalid_response")
    if any(type(result.get(key)) is not int or result[key] < 0 for key in COUNT_FIELDS):
        raise h.LaunchplaneSafetyError("invalid_response")
    raw_survivors = result.get("surviving_keys")
    if not isinstance(raw_survivors, list) or result["surviving_value_count"] < len(raw_survivors):
        raise h.LaunchplaneSafetyError("invalid_response")
    drops = h._FieldDrops()
    survivors = _surviving_keys(h, raw_survivors, drops)
    projected: dict[str, object] = {
        "mode": body["mode"],
        "product": body["product"],
        "scope": body["scope"],
        "context": body["context"],
        "instance": body["instance"],
        "removed_keys": sorted(body["keys"]),
        "updated_at": drops.keep("result.updated_at", h.public_timestamp, result.get("updated_at")),
        "surviving_keys": survivors[:SURVIVOR_CAP],
        "surviving_keys_truncated": len(survivors) > SURVIVOR_CAP,
        **{key: result[key] for key in COUNT_FIELDS},
        **{key: result[key] for key in flags},
        **{key: result[key] for key in DIGEST_FIELDS},
        "review_id": result["review_id"],
        "applied": False,
        "apply_supported": False,
    }
    h._report_dropped_fields(result, prefix="result", kept=set(RESULT_FIELDS),
                             known=RESULT_FIELDS, drops=drops)
    projected["dropped_field_paths"] = sorted(drops.paths)
    h.assert_public_safe_shape(projected)
    if readback:
        recommendation = (
            "readback_matches true means the stored record now matches the reviewed removal. "
            "False means drift or the removal is not applied yet; it is not success."
        )
    else:
        recommendation = (
            "Save review_id, plan_sha256 and the exact keys and reason for readback. "
            + ("requires_compatible_readers_and_rollback is true: qualify readers of retirement-only "
               "records and keep a rollback build before any admin apply. "
               if result["requires_compatible_readers_and_rollback"] else "")
            + "This helper has no apply; apply belongs to the signed-in admin."
        )
    summary: dict[str, object] = {
        "launchplane_status": "ok",
        "trace_id": h.service_response_diagnostics(source).get("trace_id", ""),
        "review_id": result["review_id"],
        "plan_sha256": result["plan_sha256"],
        "requires_compatible_readers_and_rollback": result["requires_compatible_readers_and_rollback"],
        "record_deleted": result["record_deleted"],
        "recommendation": recommendation,
    }
    if readback:
        summary["readback_matches"] = result["readback_matches"]
    h.assert_public_safe_shape(summary)
    return projected, summary


def _rejection(h: Any, operation: str, request: dict[str, object], error: urllib.error.HTTPError) -> dict[str, object]:
    provider = h._error_payload_from_http(error)
    output = h.summarize_http_error(operation=operation, request=request, exc=error, provider_payload=provider)
    summary = output["summary"]
    if error.code == 409 and summary.get("error_code") in STALE_CODES:
        summary["recommendation"] = STALE_RECOMMENDATION
    h.assert_public_safe_shape(summary)
    return output


def execute(h: Any, args: argparse.Namespace) -> int:
    operation = args.command
    request, body = intent(h, args)
    settings = h.prepare_operator_settings(args=args, operation=operation, request=request)
    if settings is None:
        return 2
    try:
        # Dry run and readback take no Idempotency-Key; only the admin's apply does.
        response = h.request_launchplane(service_url=settings["service_url"],
            path=h.helper_command_path(operation), settings=settings, body=body, timeout=args.timeout)
    except urllib.error.HTTPError as error:
        try:
            h.emit(_rejection(h, operation, request, error))
        except h.LaunchplaneSafetyError:
            h.emit_invalid_response(operation=operation, request=request)
        finally:
            error.close()
        return 1
    except h.ServiceResponseError as error:
        output = h.unavailable_payload(operation=operation, request=request, status="unavailable",
            code="invalid_response", message="The runtime setting removal response was unavailable.")
        output["summary"] = h.service_response_diagnostics(error)
        h.emit(output)
        return 1
    except h.LaunchplaneSafetyError as error:
        h.emit_safety_error_payload(operation=operation, request=request, exc=error)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as error:
        h.emit_read_transport_error(operation=operation, request=request, timeout=args.timeout, exc=error)
        return 1
    try:
        result, summary = project(h, response, body)
    except ValueError:
        h.emit_invalid_response(operation=operation, request=request,
            diagnostics=h.service_response_diagnostics(response))
        return 1
    output = h.base_payload(status="ok", operation=operation, request=request)
    output["result"] = result
    output["summary"] = summary
    h.emit(output)
    return 0
