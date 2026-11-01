# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import argparse
import copy
import hashlib
import http.client
import json
import os
import re
import urllib.error
from pathlib import Path, PurePosixPath
from typing import Any

PLAN_COMMAND = "testing-state-dry-run"
APPLY_COMMAND = "testing-state-apply"
COMMANDS = frozenset({PLAN_COMMAND, APPLY_COMMAND})
INTENT_FIELDS = {"product", "context", "kind", "container_path"}
PATH_PATTERN = re.compile(r"/[A-Za-z0-9_./-]+")
PROTECTED_PATHS = ("/proc", "/sys", "/dev", "/etc", "/run")


def add_parser(subparsers: Any) -> None:
    plan = subparsers.add_parser(PLAN_COMMAND, help="Review one generic-web testing state declaration.")
    plan.add_argument("--product", required=True)
    plan.add_argument("--context", required=True)
    plan.add_argument("--kind", choices=("none", "volume"), required=True)
    plan.add_argument("--container-path", default="")
    apply = subparsers.add_parser(APPLY_COMMAND, help="Apply the exact saved testing state declaration.")
    apply.add_argument("--idempotency-key", required=True)
    apply.add_argument("--reviewed-dry-run", action="store_true")
    apply.add_argument("--expected-plan-digest", default="")
    apply.add_argument("--dry-run-evidence-file", default="")
    for parser in (plan, apply):
        parser.add_argument("--profile-request-file", required=True)


def profile_digest(profile: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(profile, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def service_fingerprint(h: Any, settings: dict[str, str]) -> str:
    return h.metadata_review_digest({"service_url": settings["service_url"].strip().rstrip("/")})


def checked_container_path(h: Any, path: str) -> str:
    if not path:
        return ""
    canonical = PurePosixPath(path)
    if (
        str(canonical) != path
        or ".." in canonical.parts
        or path == "/"
        or any(path == prefix or path.startswith(prefix + "/") for prefix in PROTECTED_PATHS)
    ):
        raise ValueError("invalid_testing_state_path")
    public = h.public_identifier(path, pattern=PATH_PATTERN)
    if public != path:
        raise ValueError("invalid_testing_state_path")
    return public


def validate_intent(h: Any, intent: dict[str, Any]) -> dict[str, str]:
    if set(intent) != INTENT_FIELDS or not all(isinstance(value, str) for value in intent.values()):
        raise ValueError("invalid_testing_state_request")
    for field in ("product", "context"):
        if not h.PRODUCT_READ_PATH_SEGMENT_RE.fullmatch(intent[field]):
            raise ValueError("invalid_" + field)
        h.public_identifier(intent[field])
    if intent["kind"] not in {"none", "volume"}:
        raise ValueError("invalid_testing_state_request")
    path = intent["container_path"]
    if (intent["kind"] == "none" and path) or (intent["kind"] == "volume" and not path):
        raise ValueError("invalid_testing_state_path")
    intent["container_path"] = checked_container_path(h, path)
    return intent


def lane_index(profile: dict[str, Any], intent: dict[str, str]) -> int:
    if (
        profile.get("product") != intent["product"]
        or profile.get("driver_id") != "generic-web"
        or profile.get("lifecycle_state") != "active"
        or not isinstance(profile.get("lanes"), list)
    ):
        raise ValueError("generic_testing_profile_required")
    matching = [
        index
        for index, lane in enumerate(profile["lanes"])
        if isinstance(lane, dict) and lane.get("context") == intent["context"] and lane.get("instance") == "testing"
    ]
    if len(matching) != 1:
        raise ValueError("exact_testing_lane_required")
    return matching[0]


def validate_staged(h: Any, staged: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    if set(staged) != {"profile", "etag", "intent", "service_fingerprint"}:
        raise ValueError("invalid_testing_state_request")
    profile = h._require_dict(staged["profile"])
    intent = validate_intent(h, h._require_dict(staged["intent"]))
    if staged["etag"] != '"' + profile_digest(profile) + '"':
        raise ValueError("profile_revision_unverified")
    lane_index(profile, intent)
    return profile, intent


def replacement_profile(profile: dict[str, Any], intent: dict[str, str]) -> dict[str, Any]:
    replacement = copy.deepcopy(profile)
    replacement["lanes"][lane_index(profile, intent)]["generic_web_testing_state"] = {
        "kind": intent["kind"],
        "container_path": intent["container_path"],
    }
    return replacement


def request_summary(h: Any, intent: dict[str, str]) -> dict[str, object]:
    request: dict[str, object] = dict(intent)
    request.update(instance="testing", payload_source="private_file")
    h.assert_public_safe_shape(request)
    return request


def write_staged(h: Any, path: str, staged: dict[str, Any]) -> None:
    target = Path(path).expanduser()
    try:
        resolved = h.path_with_resolved_parent(target)
        if h._is_relative_to(resolved, h.active_repo_root()):
            raise ValueError("repo_local_payload_unsupported")
        descriptor = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(staged, output, ensure_ascii=False, sort_keys=True)
    except OSError:
        raise ValueError("private_request_unavailable") from None


def execute_plan(h: Any, args: argparse.Namespace) -> int:
    intent = validate_intent(h, {field: getattr(args, field) for field in INTENT_FIELDS})
    request = request_summary(h, intent)
    settings = h.prepare_operator_settings(args=args, operation=PLAN_COMMAND, request=request)
    if settings is None:
        return 2
    response = None
    try:
        response = h.request_launchplane_read(
            service_url=settings["service_url"],
            settings=settings,
            path=h._product_read_path("product-profile-read", product=intent["product"]),
            query={},
            timeout=args.timeout,
        )
        staged = {
            "profile": h._require_dict(response.get("profile")),
            "etag": getattr(response, "etag", ""),
            "intent": intent,
            "service_fingerprint": service_fingerprint(h, settings),
        }
        profile, _ = validate_staged(h, staged)
        previous = profile["lanes"][lane_index(profile, intent)].get("generic_web_testing_state")
        previous_kind = previous.get("kind") if isinstance(previous, dict) else "undeclared"
        result = {
            "status": "pass",
            "mode": "dry-run",
            "product": intent["product"],
            "context": intent["context"],
            "kind_before": h.public_identifier(previous_kind),
            "container_path_before": checked_container_path(h, previous.get("container_path", ""))
            if isinstance(previous, dict)
            else "",
            "kind_after": intent["kind"],
            "container_path_after": intent["container_path"],
            "profile_sha256": profile_digest(profile),
            "changed": replacement_profile(profile, intent) != profile,
            "plan_sha256": h.metadata_review_digest(staged),
        }
        h.assert_public_safe_shape(result)
        write_staged(h, args.profile_request_file, staged)
        output = h.base_payload(status="ok", operation=PLAN_COMMAND, request=request)
        output["result"] = result
        output["summary"] = {
            **h.service_response_diagnostics(response),
            "recommendation": "Review this testing-only declaration. Keep the private request file unchanged with this evidence for apply and original-key recovery. This does not rebuild or qualify a lane.",
        }
        h.emit(output)
        return 0
    except urllib.error.HTTPError as error:
        h.emit_http_error_payload(operation=PLAN_COMMAND, request=request, exc=error)
    except h.ServiceResponseError as error:
        h.emit_safety_error_payload(
            operation=PLAN_COMMAND, request=request, exc=h.LaunchplaneSafetyError("invalid_response"), provider_payload=error
        )
    except h.LaunchplaneSafetyError as error:
        h.emit_safety_error_payload(operation=PLAN_COMMAND, request=request, exc=error, provider_payload=response)
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as error:
        h.emit_read_transport_error(operation=PLAN_COMMAND, request=request, timeout=args.timeout, exc=error)
    return 1


def read_back(h: Any, args: argparse.Namespace, settings: dict[str, str], intent: dict[str, str]) -> tuple[bool, dict[str, object]]:
    try:
        response = h.request_launchplane_read(
            service_url=settings["service_url"],
            settings=settings,
            path=h._product_read_path("product-profile-read", product=intent["product"]),
            query={},
            timeout=args.timeout,
        )
        profile = h._require_dict(response.get("profile"))
        actual = profile["lanes"][lane_index(profile, intent)].get("generic_web_testing_state")
        expected = {"kind": intent["kind"], "container_path": intent["container_path"]}
        return actual == expected, {
            "read_back_status": "observed",
            **{"read_back_" + key: value for key, value in h.service_response_diagnostics(response).items()},
        }
    except urllib.error.HTTPError as error:
        try:
            refusal = h.summarize_http_error(operation="product-profile-read", request={"product": intent["product"]}, exc=error)
            diagnostics = refusal["summary"]
            return False, {
                "read_back_status": refusal["status"],
                **{
                    "read_back_" + key: value
                    for key, value in diagnostics.items()
                    if key in {"http_status", "trace_id", "error_code"}
                },
            }
        finally:
            error.close()
    except (
        h.ServiceResponseError,
        h.LaunchplaneSafetyError,
        ValueError,
        OSError,
        TimeoutError,
        urllib.error.URLError,
        http.client.HTTPException,
    ):
        return False, {"read_back_status": "unavailable"}


def finish_apply(
    h: Any,
    args: argparse.Namespace,
    settings: dict[str, str],
    intent: dict[str, str],
    request: dict[str, object],
    response: dict[str, Any] | None,
    *,
    applied: bool,
) -> int:
    matches, diagnostics = read_back(h, args, settings, intent)
    status = ("accepted" if matches else "accepted_unverified") if applied else ("unchanged" if matches else "unverified")
    output = h.base_payload(status=status, operation=APPLY_COMMAND, request=request)
    output["result"] = {
        "applied": applied,
        "read_back_matches": matches,
        "product": intent["product"],
        "context": intent["context"],
        "kind": intent["kind"],
        "container_path": intent["container_path"],
    }
    output["summary"] = {
        **h.service_response_diagnostics(response),
        **diagnostics,
        "recommendation": ("The state declaration was accepted. " if applied else "No profile write was needed. ")
        + "Readback qualifies only the declaration, not runtime isolation, a rebuild or a restore. Preserve the original private request, review and key.",
    }
    h.assert_public_safe_shape(output["result"])
    h.assert_public_safe_shape(output["summary"])
    h.emit(output)
    return 0 if matches else 1


def execute_apply(h: Any, args: argparse.Namespace) -> int:
    h._require_idempotency(args)
    expected = h._reviewed_apply_digest(args)
    h._load_reviewed_evidence(args, operation=PLAN_COMMAND, expected_digest=expected, evidence_status="ok", result_status="pass")
    staged = h.read_payload_file(args.profile_request_file)
    profile, intent = validate_staged(h, staged)
    if h.metadata_review_digest(staged) != expected:
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    request = request_summary(h, intent)
    request["idempotency_key_fingerprint"] = h.repository_inventory_idempotency_key_fingerprint(args.idempotency_key)
    settings = h.prepare_operator_settings(args=args, operation=APPLY_COMMAND, request=request)
    if settings is None:
        return 2
    if staged["service_fingerprint"] != service_fingerprint(h, settings):
        raise ValueError("reviewed_dry_run_service_changed")
    body = replacement_profile(profile, intent)
    if body == profile:
        return finish_apply(h, args, settings, intent, request, None, applied=False)
    response = None
    diagnostics: dict[str, object] = {}
    try:
        response = h.request_launchplane(
            service_url=settings["service_url"],
            settings=settings,
            path=h.internal_helper_path("testing-state-profile-write"),
            body=body,
            timeout=args.timeout,
            idempotency_key=args.idempotency_key.strip(),
            if_match=staged["etag"],
        )
        if (
            response.get("status") != "accepted"
            or h._require_dict(response.get("records")).get("product_profile") != intent["product"]
        ):
            raise h.LaunchplaneSafetyError("invalid_response")
    except urllib.error.HTTPError as error:
        try:
            if 400 <= error.code < 500 and error.code != 408:
                refusal = h.summarize_http_error(operation=APPLY_COMMAND, request=request, exc=error)
                if refusal["status"] == "stale" or refusal["summary"].get("error_code") == "product_profile_conflict":
                    refusal["summary"]["recommendation"] = (
                        "The reviewed profile changed. Read a fresh profile for a new review only when no earlier attempt remains unresolved."
                    )
                refusal["summary"]["recommendation"] += (
                    " This refusal describes this attempt only. Preserve any earlier unknown request, review and original key; it does not settle that earlier outcome. If identical replay cannot recover acceptance, stop automatic recovery and report the missing service-side settlement capability on the owning issue."
                )
                h.assert_public_safe_shape(refusal["summary"])
                h.emit(refusal)
                return 1
            summary = h.summarize_http_error(operation=APPLY_COMMAND, request=request, exc=error)["summary"]
            diagnostics = {key: value for key, value in summary.items() if key in {"trace_id", "http_status", "error_code"}}
        finally:
            error.close()
    except (
        h.ServiceResponseError,
        h.LaunchplaneSafetyError,
        ValueError,
        OSError,
        TimeoutError,
        urllib.error.URLError,
        http.client.HTTPException,
    ) as error:
        diagnostics = h.service_response_diagnostics(error)
    else:
        return finish_apply(h, args, settings, intent, request, response, applied=True)
    output = h._apply_outcome_unknown(operation=APPLY_COMMAND, request=request, label="original testing state declaration")
    output["summary"].update(h.service_response_diagnostics(response))
    output["summary"].update(diagnostics)
    output["summary"]["recommendation"] = (
        "Preserve the unchanged private request file, reviewed evidence and original key. Recover only by explicitly replaying those same inputs. Do not read a new profile into this request, change identity or choose a new key."
    )
    output["warnings"] = [
        h.warning(
            "testing_state_outcome_unknown",
            "Recover only by explicitly replaying the identical private request, saved review and original key. A later refusal does not by itself settle an earlier unknown attempt.",
        )
    ]
    h.emit(output)
    return 1


def execute(h: Any, args: argparse.Namespace) -> int:
    return execute_plan(h, args) if args.command == PLAN_COMMAND else execute_apply(h, args)
