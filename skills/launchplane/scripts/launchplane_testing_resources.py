# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import argparse
import http.client
import re
import urllib.error
import urllib.parse
from typing import Any

PREFIX = "testing-replacement-resources-"
MODES = ("dry-run", "apply", "readback")
COMMANDS = frozenset(PREFIX + mode for mode in MODES)
SCOPE = ("product", "context", "instance", "operation_id")
INPUT_FIELDS = {*SCOPE, "reason", "source_mode"}
PRESERVED = {"retained_target_preserved": True, "restore_started": False}
PLAN_FACTS = {"fresh_storage_required": True, "published_host_ports": False, "host_ssh_mounts": False}
RESOURCE_FIELDS = {"resources_verified", "resources_binding_sha256", "schedule_id"}
DIGEST_RE = re.compile(r"[a-f0-9]{64}")
RECOVERY = (
    "Preserve the exact private request, reviewed plan, retained operation and original idempotency key. "
    "Use testing-replacement-resources-readback for actual resource qualification. If it is unverified, "
    "retain partial resources and inspect the original operation; explicitly resume only the original-key "
    "apply through the service's supported recovery. Do not retry automatically, allocate another target, "
    "delete resources, change identity or call a provider. Preparation does not restore a copy or release a fence."
)
PLAN_RECOVERY = (
    "Inert preparation may have been recorded. This mode allocates no provider resources. "
    "Inspect the lane and retained operation before explicitly preparing and reviewing a new dry run. "
    "Do not retry automatically or clear retained preparation to evade a refusal."
)
READBACK_RECOVERY = (
    "Resource qualification is unverified. This call allocates no provider resources. "
    "Retain the resources and inspect the original operation before explicitly requesting "
    "testing-replacement-resources-readback again. Do not repeat apply, allocate another target or delete resources."
)
CLEAN_REFUSAL_RECOVERY = (
    "The service refused this attempt before provider work. Inspect the current lane and prepare and review a new dry run. "
    "Preserve any earlier request, review and original key; this refusal proves nothing about earlier attempts. "
    "Retained preparation can still require its earlier digest. Stop on the service's exact refusal; "
    "do not change keys or clear preparation to evade it."
)


def recommend(h: Any, mode: str, status: str, code: str = "", refusal_status: str = "") -> str:
    if status in {"denied", "unauthorized"}:
        return h.http_error_recommendation(status) + " Preserve the trace and any earlier original-operation artifacts."
    if status == "accepted_unverified":
        return (
            "Allocation was accepted, but its separate readback failed. "
            + READBACK_RECOVERY
            + (" " + h.http_error_recommendation(refusal_status) if refusal_status in {"denied", "unauthorized"} else "")
        )
    if status == "accepted":
        return (
            "Save and review this plan before apply. The dry run records inert preparation; it allocates no provider resources."
            if mode == "dry-run"
            else "Resources were qualified as separation_ready. No restore, completed isolation or supersession is proven."
        )
    if status == "outcome_unknown":
        return {"dry-run": PLAN_RECOVERY, "apply": RECOVERY, "readback": READBACK_RECOVERY}[mode]
    if code == "testing_resources_refused" and mode in {"dry-run", "apply"}:
        return CLEAN_REFUSAL_RECOVERY
    return h.http_error_recommendation(status) + " Preserve any earlier original-operation artifacts; do not retry automatically."


def is_definite_refusal(mode: str, http_status: int, code: str) -> bool:
    return (
        400 <= http_status < 500
        and http_status != 408
        and (http_status != 409 or (mode in {"dry-run", "apply"} and code == "testing_resources_refused"))
    )


def add_parser(subparsers: Any) -> None:
    for mode in MODES:
        parser = subparsers.add_parser(PREFIX + mode, help="Plan, allocate or qualify empty replacement testing resources.")
        parser.add_argument("--payload-file", required=True, help="Private retained-operation request outside the repository.")
        if mode == "apply":
            parser.add_argument("--expected-plan-digest", required=True)
            parser.add_argument("--dry-run-evidence-file", required=True)
            parser.add_argument("--reviewed-dry-run", action="store_true")
            parser.add_argument("--idempotency-key", required=True, help="Original key, including recovery.")


def intent(h: Any, args: argparse.Namespace) -> tuple[dict[str, object], dict[str, Any]]:
    raw = h.read_payload_file(args.payload_file)
    if raw.keys() - INPUT_FIELDS or any(field not in raw for field in (*SCOPE, "reason")):
        raise ValueError("invalid_testing_resources_fields")
    body: dict[str, Any] = {}
    for field in (*SCOPE, "reason"):
        value = raw[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("invalid_testing_resources_" + field)
        body[field] = value.strip()
        if field in SCOPE:
            if not h.PRODUCT_READ_PATH_SEGMENT_RE.fullmatch(body[field]):
                raise ValueError("invalid_testing_resources_" + field)
            try:
                h.public_identifier(body[field])
            except h.LaunchplaneSafetyError:
                raise ValueError("invalid_testing_resources_" + field) from None
    if body["instance"] != "testing":
        raise ValueError("testing_resources_requires_testing")
    source = raw.get("source_mode", "ssh")
    if source not in ("ssh", "container-export"):
        raise ValueError("invalid_testing_resources_source_mode")
    if source != "ssh":
        body["source_mode"] = source
    request = {"payload_source": "private_file", "payload_digest": h.metadata_review_digest(body)}
    h.assert_public_safe_shape(request)
    return request, body


def review_binding(h: Any, request: dict[str, object], result: dict[str, Any]) -> str:
    return h.metadata_review_digest({"request": request, "result": result})


def require_review(h: Any, args: argparse.Namespace, request: dict[str, object]) -> dict[str, Any]:
    if not args.reviewed_dry_run:
        raise ValueError("reviewed_dry_run_required")
    expected = args.expected_plan_digest.strip()
    evidence = h.read_payload_file(args.dry_run_evidence_file)
    result = evidence.get("result")
    if (
        not DIGEST_RE.fullmatch(expected)
        or not isinstance(result, dict)
        or evidence.get("operation") != PREFIX + "dry-run"
        or evidence.get("status") != "accepted"
        or evidence.get("request") != request
        or result.get("mode") != "dry-run"
        or result.get("plan_sha256") != expected
        or result.get("applied") is not False
        or result.get("scope_matches") is not True
        or any(result.get(key) is not value for key, value in {**PRESERVED, **PLAN_FACTS}.items())
        or not isinstance(result.get("service_fingerprint"), str)
        or not DIGEST_RE.fullmatch(result["service_fingerprint"])
        or result.get("review_binding_sha256")
        != review_binding(h, request, {key: value for key, value in result.items() if key != "review_binding_sha256"})
    ):
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    return result


def project(h: Any, response: dict[str, Any], body: dict[str, Any], mode: str, digest: str = "") -> dict[str, Any]:
    source = h._require_dict(response.get("result"))
    fields = {*SCOPE, *PRESERVED}
    facts = dict(PRESERVED)
    if mode == "readback":
        fields |= {*RESOURCE_FIELDS, "binding_sha256", "production_resources"}
    else:
        fields |= {*PLAN_FACTS, "plan_sha256", "applied"}
        facts.update(PLAN_FACTS, applied=mode == "apply")
        if mode == "apply":
            fields |= RESOURCE_FIELDS
    if (
        response.get("status") != "accepted"
        or source.keys() != fields
        or any(source.get(field) != body[field] for field in SCOPE)
        or any(source.get(key) is not value for key, value in facts.items())
    ):
        raise h.LaunchplaneSafetyError("invalid_response")
    result: dict[str, Any] = {"mode": mode, "scope_matches": True, **facts}
    digests = ("binding_sha256", "resources_binding_sha256") if mode == "readback" else ("plan_sha256",)
    if mode == "apply":
        digests += ("resources_binding_sha256",)
    for field in digests:
        value = source.get(field)
        if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
            raise h.LaunchplaneSafetyError("invalid_response")
        result[field] = value
    if mode == "apply" and source["plan_sha256"] != digest:
        raise h.LaunchplaneSafetyError("invalid_response")
    if mode != "dry-run":
        if (
            source.get("resources_verified") is not True
            or not isinstance(source.get("schedule_id"), str)
            or not source["schedule_id"].strip()
            or (mode == "readback" and source.get("production_resources") != "separation_ready")
        ):
            raise h.LaunchplaneSafetyError("invalid_response")
        result["resources_verified"] = True
        if mode == "readback":
            result["production_resources"] = "separation_ready"
    h.assert_public_safe_shape(result)
    return result


def execute(h: Any, args: argparse.Namespace) -> int:
    mode = args.command.removeprefix(PREFIX)
    request, private = intent(h, args)
    reviewed: dict[str, Any] = {}
    if mode == "apply":
        h._require_idempotency(args)
        reviewed = require_review(h, args, request)
    settings = h.prepare_operator_settings(args=args, operation=args.command, request=request)
    if settings is None:
        return 2
    service = h.metadata_review_digest({"service_url": settings["service_url"].rstrip("/")})
    if mode == "apply" and reviewed["service_fingerprint"] != service:
        raise ValueError("reviewed_dry_run_service_changed")
    body = {key: value for key, value in private.items() if key != "product"}
    if mode == "apply":
        body["reviewed_plan_sha256"] = reviewed["plan_sha256"]
    path = h.helper_command_path(args.command).format(product=urllib.parse.quote(private["product"], safe=""))

    def call(call_mode: str) -> dict[str, Any]:
        return h.request_launchplane(
            service_url=settings["service_url"],
            path=path,
            settings=settings,
            body={**body, "mode": call_mode},
            timeout=args.timeout,
            idempotency_key=args.idempotency_key.strip() if call_mode == "apply" else "",
        )

    response: dict[str, Any] | None = None
    result: dict[str, Any] = {}
    diagnostics: dict[str, Any] = {}
    failure: dict[str, Any] | None = None
    accepted = False
    try:
        response = call(mode)
        result = project(h, response, private, mode, str(reviewed.get("plan_sha256", "")))
        if mode == "apply":
            accepted = True
            result["resource_readback_matches"] = False
            readback = call("readback")
            diagnostics = h.service_response_diagnostics(readback)
            observed = project(h, readback, private, "readback")
            if observed["resources_binding_sha256"] != result["resources_binding_sha256"]:
                raise h.LaunchplaneSafetyError("invalid_response")
            result["resource_readback_matches"] = True
        if mode == "dry-run":
            result["service_fingerprint"] = service
            result["review_binding_sha256"] = review_binding(h, request, result)
        output = h.base_payload(status="accepted", operation=args.command, request=request)
        output["result"] = result
        output["summary"] = {
            **h.service_response_diagnostics(response),
            **{"read_back_" + key: value for key, value in diagnostics.items()},
            "recommendation": recommend(h, mode, "accepted"),
        }
        h.emit(output)
        return 0
    except urllib.error.HTTPError as error:
        try:
            try:
                error_payload = h._error_payload_from_http(error)
                failure = h.summarize_http_error(operation=args.command, request=request, exc=error, provider_payload=error_payload)
                diagnostics = h.service_response_diagnostics(error_payload)
            except (h.LaunchplaneSafetyError, ValueError, OSError, http.client.HTTPException):
                diagnostics = {"http_status": error.code}
                failure = h.unavailable_payload(
                    operation=args.command,
                    request=request,
                    status=h._status_for_http_error(error.code, {}),
                    code="invalid_response",
                    message="The service refusal details could not be verified.",
                )
        finally:
            error.close()
        if not accepted and is_definite_refusal(mode, error.code, str(diagnostics.get("error_code", ""))):
            failure["summary"]["recommendation"] = recommend(h, mode, failure["status"], str(diagnostics.get("error_code", "")))
            h.emit(failure)
            return 1
    except (
        h.ServiceResponseError,
        h.LaunchplaneSafetyError,
        ValueError,
        OSError,
        TimeoutError,
        urllib.error.URLError,
        http.client.HTTPException,
    ) as error:
        diagnostics.update(h.service_response_diagnostics(error))
        if isinstance(error, h.LaunchplaneSafetyError):
            diagnostics["projection_error_code"] = error.code
    status = "accepted_unverified" if accepted else "outcome_unknown"
    output = h.base_payload(status=status, operation=args.command, request=request)
    output["warnings"] = [h.warning(status, "Inspect the original operation before another explicit request.")]
    if accepted:
        output["result"] = result
    output["summary"] = h.service_response_diagnostics(response)
    output["summary"].update({("read_back_" if accepted else "") + key: value for key, value in diagnostics.items()})
    output["summary"]["recommendation"] = recommend(h, mode, status, refusal_status=failure["status"] if failure else "")
    h.emit(output)
    return 1
