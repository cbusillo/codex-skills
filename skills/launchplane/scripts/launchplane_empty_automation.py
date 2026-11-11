# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import argparse
import http.client
import re
import urllib.error
from datetime import datetime
from typing import Any

OPERATION = "disable-empty-compose-auto-deploy"
PREFIX = "dokploy-target-" + OPERATION + "-"
MODES = ("dry-run", "apply", "readback")
COMMANDS = frozenset(PREFIX + mode for mode in MODES)
IDENTITIES = ("context", "instance", "target_id", "project_id", "environment_id", "server_id")
PAYLOAD_FIELDS = {*IDENTITIES, "reason", "expected_current_provider_target", "schema_version", "target_type"}
REVIEW_FIELDS = {"observed_auto_deploy", "observed_at", "plan_sha256"}
RECOVERY = (
    "Preserve the exact original corrective request, review and idempotency key with the same caller and token label. "
    f"Use {PREFIX}readback to reconcile that original operation without another mutation. "
    "Do not retry apply, change key, identity, review or target, replay creation or clear a fence."
)


def add_parser(subparsers: Any) -> None:
    for mode in MODES:
        parser = subparsers.add_parser(PREFIX + mode, help="Review or reconcile the original empty Compose automation correction.")
        parser.add_argument("--payload-file", required=True, help="Private original target identities and reason.")
        if mode == "dry-run":
            parser.set_defaults(idempotency_key="")
        else:
            parser.add_argument("--idempotency-key", required=True, help="Original corrective key, distinct from creation.")
            parser.add_argument("--expected-plan-digest", required=True)
            parser.add_argument("--dry-run-evidence-file", required=True)
            if mode == "apply":
                parser.add_argument("--reviewed-dry-run", action="store_true")


def read_intent(h: Any, args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    body = h.read_payload_file(args.payload_file)
    if body.keys() - PAYLOAD_FIELDS:
        raise ValueError("unsupported_dokploy_target_field")
    for field in (*IDENTITIES, "reason"):
        value = body.get(field)
        if not isinstance(value, str) or not value.strip() or (field in IDENTITIES and value != value.strip()):
            raise ValueError("invalid_empty_automation_intent")
    version = body.get("schema_version", 1)
    if type(version) is not int or version < 1 or body["instance"] != "prod" or body.get("target_type", "compose") != "compose":
        raise ValueError("invalid_empty_automation_intent")
    reference = body.get("expected_current_provider_target")
    if (
        not isinstance(reference, dict)
        or reference.keys()
        - {"provider_id", "target_id", "display_name", "target_category", "provider_target_type", "provider_evidence"}
        or any(not isinstance(reference.get(field), str) for field in ("provider_id", "target_id", "display_name"))
        or reference["provider_id"] != "dokploy"
        or reference["target_id"] != body["target_id"]
        or not isinstance(reference.get("target_category", "unknown"), str)
        or reference.get("target_category", "unknown") not in {"application", "compose", "container", "service", "static", "unknown"}
        or not isinstance(reference.get("provider_target_type", ""), str)
        or not isinstance(reference.get("provider_evidence", {}), dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in reference.get("provider_evidence", {}).items()
        )
    ):
        raise ValueError("invalid_expected_current_provider_target")
    request = {
        "mode": args.command.removeprefix(PREFIX),
        "payload_source": "private_file",
        "payload_digest": h.metadata_review_digest(body),
        "context": h.public_identifier(body["context"]),
        "instance": "prod",
    }
    return body, request


def validate_review(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.keys() != REVIEW_FIELDS or type(value.get("observed_auto_deploy")) is not bool:
        raise ValueError("invalid_automation_review")
    if not isinstance(value["plan_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["plan_sha256"]):
        raise ValueError("invalid_automation_review")
    if not isinstance(value["observed_at"], str):
        raise ValueError("invalid_automation_review")
    try:
        observed_at = datetime.fromisoformat(value["observed_at"])
    except ValueError:
        raise ValueError("invalid_automation_review") from None
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("invalid_automation_review")
    return dict(value)


def review_binding(h: Any, request: dict[str, Any], review: dict[str, Any], service_url: str) -> str:
    return h.metadata_review_digest(
        {
            "request": {key: value for key, value in request.items() if key != "mode"},
            "automation_review": review,
            "service_url": service_url.rstrip("/"),
        }
    )


def load_review(h: Any, args: argparse.Namespace, request: dict[str, Any]) -> dict[str, Any]:
    h._require_idempotency(args)
    digest = h._reviewed_apply_digest(args) if args.command.endswith("-apply") else args.expected_plan_digest.strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("invalid_expected_plan_digest")
    evidence, result = h._load_reviewed_evidence(args, operation=PREFIX + "dry-run", expected_digest=digest, result_status=None)
    review = validate_review(result.get("automation_review"))
    if (
        {key: value for key, value in evidence["request"].items() if key != "mode"}
        != {key: value for key, value in request.items() if key != "mode"}
        or result.get("context") != request["context"]
        or result.get("instance") != "prod"
        or result.get("operation") != OPERATION
        or result.get("applied") is not False
        or result.get("automatic_deployment_disabled") is not (not review["observed_auto_deploy"])
        or review["plan_sha256"] != digest
    ):
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    return result


def project(h: Any, response: dict[str, Any], body: dict[str, Any], mode: str) -> dict[str, Any]:
    response = h._require_dict(response)
    result = h._require_dict(response.get("result"))
    setup = h._require_dict(result.get("setup"))
    if (
        response.keys() - {"status", "trace_id", "records", "result", "replayed", "original_trace_id"}
        or response.get("status") != "accepted"
        or response.get("records") != {}
        or result.keys() != {"mode", "operation", "context", "instance", "applied", "route_domain_ids", "setup"}
        or setup.keys() != {"automatic_deployment_disabled", "automation_review"}
        or result.get("operation") != OPERATION
        or result.get("context") != body["context"]
        or result.get("instance") != body["instance"]
        or result.get("mode") != ("dry-run" if mode == "dry-run" else "apply")
        or type(result.get("applied")) is not bool
        or result["applied"] is not (mode != "dry-run")
        or type(setup.get("automatic_deployment_disabled")) is not bool
        or result.get("route_domain_ids") != []
    ):
        raise h.LaunchplaneSafetyError("invalid_response")
    trace = h.public_trace_id(response.get("trace_id"))
    replayed = response.get("replayed", False)
    if not trace or trace != response.get("trace_id") or type(replayed) is not bool or (mode == "dry-run" and replayed):
        raise h.LaunchplaneSafetyError("invalid_response")
    original = response.get("original_trace_id")
    if (replayed and (not h.public_trace_id(original) or original != h.public_trace_id(original) or original == trace)) or (
        not replayed and original is not None
    ):
        raise h.LaunchplaneSafetyError("invalid_response")
    review = validate_review(setup["automation_review"])
    if (mode != "dry-run" and review != body["automation_review"]) or setup["automatic_deployment_disabled"] is not (
        mode != "dry-run" or not review["observed_auto_deploy"]
    ):
        raise h.LaunchplaneSafetyError("invalid_response")
    projected = {
        "mode": result["mode"],
        "operation": OPERATION,
        "context": h.public_identifier(result["context"]),
        "instance": "prod",
        "applied": result["applied"],
        "automatic_deployment_disabled": setup["automatic_deployment_disabled"],
        "automation_review": review,
        "plan_sha256": review["plan_sha256"],
        "replayed": replayed,
        "current_state_verified": mode != "dry-run" and not replayed,
    }
    h.assert_public_safe_shape(projected)
    return projected


def execute(h: Any, args: argparse.Namespace) -> int:
    mode = args.command.removeprefix(PREFIX)
    private, request = read_intent(h, args)
    reviewed = load_review(h, args, request) if mode != "dry-run" else {}
    settings = h.prepare_operator_settings(args=args, operation=args.command, request=request)
    if settings is None:
        return 2
    review = validate_review(reviewed["automation_review"]) if reviewed else {}
    if reviewed and reviewed.get("review_binding_sha256") != review_binding(h, request, review, settings["service_url"]):
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    body = {**private, "operation": OPERATION, "product": "launchplane", "mode": mode}
    if reviewed:
        body["automation_review"] = review
    if mode == "apply":
        body["confirmation"] = h.DOKPLOY_TARGET_SETUP_CONFIRMATION
    response: dict[str, Any] | None = None
    try:
        received: dict[str, Any] = h.request_launchplane(
            service_url=settings["service_url"],
            path=h.helper_command_path(args.command),
            settings=settings,
            body=body,
            timeout=args.timeout,
            idempotency_key=args.idempotency_key.strip(),
        )
        response = received
        result = project(h, received, body, mode)
        if mode == "dry-run":
            result["review_binding_sha256"] = review_binding(h, request, result["automation_review"], settings["service_url"])
        output = h.base_payload(status="accepted", operation=args.command, request=request)
        output["result"] = result
        output["summary"] = h.service_response_diagnostics(response)
        if result["replayed"]:
            output["summary"].update(original_trace_id=h.public_trace_id(received["original_trace_id"]), replayed=True)
        output["summary"]["recommendation"] = (
            (
                "Stored original completion verified; this is no fresh effect or current-state observation."
                if result["replayed"]
                else "Save and review this evidence before apply."
                if mode == "dry-run"
                else "The service independently verified automatic deployment disabled for the original correction."
            )
            + " "
            + RECOVERY
        )
        h.emit(output)
        return 0
    except urllib.error.HTTPError as error:
        try:
            error_payload = h._error_payload_from_http(error)
            output = h.summarize_http_error(operation=args.command, request=request, exc=error, provider_payload=error_payload)
            code = output["summary"].get("error_code")
            if mode == "apply" and (
                error.code >= 500 or error.code == 408 or code in {"mutation_reconciliation_required", "mutation_in_progress"}
            ):
                output["status"] = "outcome_unknown"
            output["summary"]["recommendation"] = RECOVERY
            h.emit(output)
            return 1
        except (h.LaunchplaneSafetyError, ValueError, OSError, http.client.HTTPException):
            diagnostics: dict[str, Any] = {"http_status": error.code}
        finally:
            error.close()
    except (h.LaunchplaneSafetyError, ValueError, OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as error:
        diagnostics = h.service_response_diagnostics(error)
    output = h.base_payload(
        status="accepted_unverified" if response is not None else "outcome_unknown" if mode == "apply" else "unavailable",
        operation=args.command,
        request=request,
    )
    output["summary"] = {**h.service_response_diagnostics(response), **diagnostics, "recommendation": RECOVERY}
    h.emit(output)
    return 1
