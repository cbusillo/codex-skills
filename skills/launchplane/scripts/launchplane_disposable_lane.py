# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Disposable-lane safety observations through the existing service identity."""

from __future__ import annotations

import argparse
import http.client
import re
import urllib.error
from datetime import UTC, datetime
from typing import Any

PREFIX = "disposable-lane-"
MODES = ("inspect", "collect", "record", "clear")
COMMANDS = frozenset(PREFIX + mode for mode in MODES)
TIMEOUTS = {PREFIX + "collect": 240.0, PREFIX + "inspect": 30.0, PREFIX + "record": 30.0, PREFIX + "clear": 30.0}
SCOPE = ("product", "context", "instance")
FACTS = {
    "usage": ({"unknown", "unused", "in_use"}, "usage_evidence"),
    "data": ({"unknown", "reproducible", "only_copy"}, "data_evidence"),
    "production_resources": ({"unknown", "isolated", "separation_ready", "shared"}, "isolation_evidence"),
}
EVIDENCE = tuple(reference for _allowed, reference in FACTS.values())
INPUT_FIELDS = {
    "inspect": {*SCOPE, "preview_id"},
    "clear": {*SCOPE, "preview_id"},
    "collect": {*SCOPE, "preview_id", "lifetime_seconds"},
    "record": {*SCOPE, "preview_id", "lifetime_seconds", *FACTS, *EVIDENCE},
}
MAX_LIFETIME_SECONDS = 900
DIGEST_RE = re.compile(r"[a-f0-9]{64}")
CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
EVIDENCE_KIND_RE = re.compile(r"service:([a-z][a-z0-9-]{0,63}):")
COVERAGE_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
OBSERVER_KINDS = {"director_agent", "director_agent_preview_cleanup"}
NEVER_SERVED = {"never_served", "never_served_records_incomplete", "never_served_protection_seen",
                "never_served_not_attempted"}
RESULT_FIELDS = {
    "inspect": {"mode", "binding_sha256", *FACTS, "automatic_fact_collection", "source_coverage"},
    "collect": {"mode", "binding_sha256", "observed_by", "observer_kind", *FACTS, *EVIDENCE,
                "historical_usage_retained", "observed_at", "odoo_usage", "never_served_qualification",
                "expires_at", "provider_mutation", "automatic_fact_collection", "source_coverage"},
    "record": {"mode", "binding_sha256", "observed_by", "observer_kind", *FACTS, "provider_mutation",
               "automatic_fact_collection", "source_coverage", "reconcile_requested"},
    "clear": {"mode", "provider_mutation"},
}
RECOVERY = (
    "The observation may or may not be stored. Do not retry automatically. Run disposable-lane-inspect, "
    "review its binding, then re-run this command explicitly if it is still needed. Never change identity "
    "or use a provider call."
)


def add_parser(subparsers: Any) -> None:
    for mode in MODES:
        parser = subparsers.add_parser(PREFIX + mode, help="Inspect, collect, record or clear one disposable lane observation.")
        parser.add_argument("--payload-file", required=True, help="Private request JSON outside the repository.")
        if mode != "inspect":
            parser.add_argument("--expected-binding-sha256", required=True)
            parser.add_argument("--inspect-evidence-file", required=True,
                                help="Saved output of a prior disposable-lane-inspect for the same lane.")
            parser.add_argument("--reviewed-inspect", action="store_true")


def _scope_value(h: Any, body: dict[str, Any], field: str) -> str:
    value = body[field]
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid_disposable_lane_" + field)
    value = value.strip()
    if not h.PRODUCT_READ_PATH_SEGMENT_RE.fullmatch(value):
        raise ValueError("invalid_disposable_lane_" + field)
    try:
        h.public_identifier(value)
    except h.LaunchplaneSafetyError:
        raise ValueError("invalid_disposable_lane_" + field) from None
    return value


def intent(h: Any, mode: str, args: argparse.Namespace) -> tuple[dict[str, object], dict[str, Any]]:
    """Validate the private file locally; return the public echo and the private service body."""
    raw = h.read_payload_file(args.payload_file)
    if raw.keys() - INPUT_FIELDS[mode] or any(field not in raw for field in SCOPE):
        raise ValueError("invalid_disposable_lane_fields")
    body: dict[str, Any] = {field: _scope_value(h, raw, field) for field in SCOPE}
    if "preview_id" in raw:
        body["preview_id"] = _scope_value(h, raw, "preview_id")
    if body["instance"] == "prod":
        raise ValueError("disposable_lane_production_refused")
    if body["instance"] != "testing" and "preview_id" not in body:
        raise ValueError("disposable_lane_requires_testing_or_preview")
    if "lifetime_seconds" in raw:
        lifetime = raw["lifetime_seconds"]
        if type(lifetime) is not int or not 0 < lifetime <= MAX_LIFETIME_SECONDS:
            raise ValueError("invalid_disposable_lane_lifetime_seconds")
        body["lifetime_seconds"] = lifetime
    known = 0
    if mode == "record":
        for fact, (allowed, reference) in FACTS.items():
            value = raw.get(fact, "unknown")
            evidence = raw.get(reference, "")
            if value not in allowed:
                raise ValueError("invalid_disposable_lane_" + fact)
            if not isinstance(evidence, str) or len(evidence) > 1024:
                raise ValueError("invalid_disposable_lane_" + reference)
            if value != "unknown":
                if "preview_id" not in body:
                    # The service collects testing facts itself; an assertion would be discarded.
                    raise ValueError("disposable_lane_testing_facts_are_service_collected")
                if not evidence.strip():
                    raise ValueError("disposable_lane_fact_requires_evidence")
                known += 1
            if fact in raw:
                body[fact] = value
            if reference in raw:
                body[reference] = evidence.strip()
    request: dict[str, object] = {"mode": mode, "payload_source": "private_file"}
    if mode == "record":
        request["known_fact_count"] = known
    h.assert_public_safe_shape(request)
    return request, body


def scope_of(body: dict[str, Any]) -> dict[str, str]:
    return {field: body[field] for field in (*SCOPE, "preview_id") if field in body}


def review_binding(h: Any, scope: dict[str, str], digest: str, service: str) -> str:
    return h.metadata_review_digest({"scope": scope, "binding_sha256": digest, "service_fingerprint": service})


def require_review(h: Any, args: argparse.Namespace, scope: dict[str, str]) -> tuple[str, str]:
    if not args.reviewed_inspect:
        raise ValueError("reviewed_inspect_required")
    digest = args.expected_binding_sha256.strip()
    if not DIGEST_RE.fullmatch(digest):
        raise ValueError("invalid_expected_binding_sha256")
    evidence = h.read_payload_file(args.inspect_evidence_file)
    result = evidence.get("result")
    if (evidence.get("operation") != PREFIX + "inspect" or evidence.get("status") != "accepted"
        or evidence.get("request") != {"mode": "inspect", "payload_source": "private_file"}
        or not isinstance(result, dict) or result.get("mode") != "inspect"
        or result.get("binding_sha256") != digest
        or not isinstance(result.get("service_fingerprint"), str)
        or result.get("review_binding_sha256") != review_binding(h, scope, digest, result["service_fingerprint"])):
        raise ValueError("reviewed_inspect_not_eligible")
    return digest, result["service_fingerprint"]


def _digest(h: Any, value: object) -> str:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise h.LaunchplaneSafetyError("invalid_response")
    return value


def _coverage(h: Any, value: object) -> list[str]:
    if (not isinstance(value, dict) or not value
        or any(not isinstance(key, str) or not COVERAGE_KEY_RE.fullmatch(key) or not isinstance(item, str)
               for key, item in value.items())):
        raise h.LaunchplaneSafetyError("invalid_response")
    return sorted(value)


def _timestamp(h: Any, value: object) -> str:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.utcoffset() is None:
        raise h.LaunchplaneSafetyError("invalid_response")
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _evidence_summary(h: Any, source: dict[str, Any], facts: dict[str, str]) -> dict[str, dict[str, object]]:
    summary: dict[str, dict[str, object]] = {}
    for fact, (_allowed, reference) in FACTS.items():
        value = source.get(reference)
        if not isinstance(value, str):
            raise h.LaunchplaneSafetyError("invalid_response")
        if facts[fact] != "unknown" and not value.strip():
            raise h.LaunchplaneSafetyError("invalid_response")
        match = EVIDENCE_KIND_RE.match(value)
        # Only the source kind leaves the helper; hashes and references never do.
        summary[fact] = {"present": bool(value.strip()),
                         **({"kind": match[1] if match else "unrecognized"} if value.strip() else {})}
    return summary


def _collect_checks(h: Any, source: dict[str, Any], facts: dict[str, str]) -> dict[str, object]:
    """Mirror the producer client's collection consistency checks."""
    usage = source.get("odoo_usage", {})
    if not isinstance(usage, dict) or usage.keys() - {"recent_signins", "authenticated_sessions", "reason_code"}:
        raise h.LaunchplaneSafetyError("invalid_response")
    usage_facts = {key: usage.get(key) for key in ("recent_signins", "authenticated_sessions")}
    if any(value is not None and type(value) is not bool for value in usage_facts.values()):
        raise h.LaunchplaneSafetyError("invalid_response")
    reason = usage.get("reason_code", "diagnostics_unavailable")
    if not isinstance(reason, str) or not CODE_RE.fullmatch(reason):
        raise h.LaunchplaneSafetyError("invalid_response")
    if "reason_code" in usage and (
        (facts["usage"] == "unused" and reason not in {"complete", "never_served"})
        or (facts["usage"] == "unknown" and reason in {"complete", "never_served"})):
        raise h.LaunchplaneSafetyError("invalid_response")
    retained = source.get("historical_usage_retained", False)
    if type(retained) is not bool or (retained and facts["usage"] != "in_use"):
        raise h.LaunchplaneSafetyError("invalid_response")
    if facts["usage"] == "unused" and any(
        value is not (None if reason == "never_served" else False) for value in usage_facts.values()):
        raise h.LaunchplaneSafetyError("invalid_response")
    qualification = source.get("never_served_qualification")
    if qualification not in NEVER_SERVED:
        qualification = "never_served_diagnostics_unavailable"
    if (reason == "never_served") != (qualification == "never_served"):
        raise h.LaunchplaneSafetyError("invalid_response")
    return {"historical_usage_retained": retained, "odoo_usage": {**usage_facts, "reason_code": reason},
            "never_served_qualification": qualification}


def project(h: Any, response: dict[str, Any], mode: str, body: dict[str, Any], digest: str) -> dict[str, object]:
    """Fail closed on any unexpected receipt; never synthesize a positive fact."""
    source = h._require_dict(response.get("result"))
    if (response.get("status") != "accepted" or source.get("mode") != mode
        or source.keys() - RESULT_FIELDS[mode]):
        raise h.LaunchplaneSafetyError("invalid_response")
    if mode == "clear":
        if source.keys() != RESULT_FIELDS["clear"] or source.get("provider_mutation") is not False:
            raise h.LaunchplaneSafetyError("invalid_response")
        return {"mode": "clear", "provider_mutation": False}
    if source.keys() & {"provider_mutation"} != ({"provider_mutation"} if mode != "inspect" else set()):
        raise h.LaunchplaneSafetyError("invalid_response")
    if mode != "inspect" and source["provider_mutation"] is not False:
        raise h.LaunchplaneSafetyError("invalid_response")
    binding = _digest(h, source.get("binding_sha256"))
    if mode != "inspect" and binding != digest:
        raise h.LaunchplaneSafetyError("invalid_response")
    facts = {fact: source.get(fact) for fact in FACTS}
    if any(facts[fact] not in allowed for fact, (allowed, _ref) in FACTS.items()):
        raise h.LaunchplaneSafetyError("invalid_response")
    automatic = source.get("automatic_fact_collection")
    result: dict[str, object] = {"mode": mode, "binding_sha256": binding, **facts,
                                 "source_coverage_keys": _coverage(h, source.get("source_coverage"))}
    if mode == "inspect":
        if any(value != "unknown" for value in facts.values()) or automatic != "limited_service_records":
            raise h.LaunchplaneSafetyError("invalid_response")
        result["automatic_fact_collection"] = automatic
        h.assert_public_safe_shape(result)
        return result
    preview = "preview_id" in body
    observer = source.get("observer_kind")
    if observer not in OBSERVER_KINDS:
        raise h.LaunchplaneSafetyError("invalid_response")
    result.update(observer_kind=observer, provider_mutation=False)
    if mode == "collect":
        if automatic != "limited_service_records" or observer != "director_agent":
            raise h.LaunchplaneSafetyError("invalid_response")
        result.update(_collect_checks(h, source, facts), evidence=_evidence_summary(h, source, facts),
                      automatic_fact_collection=automatic, expires_at=_timestamp(h, source.get("expires_at")))
        _timestamp(h, source.get("observed_at"))
    else:
        submitted = {fact: body.get(fact, "unknown") for fact in FACTS} if preview else dict.fromkeys(FACTS, "unknown")
        reconcile = source.get("reconcile_requested")
        if (automatic != "unavailable" or facts != submitted or type(reconcile) is not bool
            or reconcile is not preview
            or observer != ("director_agent_preview_cleanup" if preview else "director_agent")):
            raise h.LaunchplaneSafetyError("invalid_response")
        result.update(automatic_fact_collection=automatic, reconcile_requested=reconcile)
    h.assert_public_safe_shape(result)
    return result


def _readback(h: Any, call: Any, body: dict[str, Any], mode: str, digest: str,
              result: dict[str, object]) -> tuple[str, dict[str, object]]:
    """Separate inspect POST. Only the binding can be read back; stored facts have no read route."""
    result["effect_readback"] = "unavailable"
    try:
        readback = call({**scope_of(body), "mode": "inspect"})
        observed = project(h, readback, "inspect", body, digest)
    except urllib.error.HTTPError as error:
        try:
            diagnostics = h.service_response_diagnostics(h._error_payload_from_http(error))
        except (h.LaunchplaneSafetyError, ValueError, OSError, http.client.HTTPException):
            diagnostics = {}
        finally:
            error.close()
        diagnostics["http_status"] = error.code
        if mode == "clear" and "preview_id" in body and error.code in {404, 409}:
            result["readback_lane_state"] = "unavailable_after_clear"
            return "accepted", diagnostics
        return "accepted_unverified", diagnostics
    except h.ServiceResponseError as error:
        return "accepted_unverified", h.service_response_diagnostics(error)
    except (ValueError, OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as error:
        return "accepted_unverified", h.service_response_diagnostics(error)
    diagnostics = h.service_response_diagnostics(readback)
    result["binding_readback_matches"] = observed["binding_sha256"] == digest
    return ("accepted" if result["binding_readback_matches"] else "accepted_unverified"), diagnostics


def execute(h: Any, args: argparse.Namespace) -> int:
    mode = args.command.removeprefix(PREFIX)
    request, body = intent(h, mode, args)
    scope = scope_of(body)
    digest, reviewed_service = ("", "")
    if mode != "inspect":
        digest, reviewed_service = require_review(h, args, scope)
    settings = h.prepare_operator_settings(args=args, operation=args.command, request=request)
    if settings is None:
        return 2
    service = h.metadata_review_digest({"service_url": settings["service_url"].rstrip("/")})
    if mode != "inspect" and reviewed_service != service:
        raise ValueError("reviewed_inspect_service_changed")
    path = h.helper_command_path(args.command)
    payload = {**body, "mode": mode}
    if mode in {"collect", "record"}:
        # The service rechecks this binding; clear deletes the exact row and ignores it.
        payload["expected_binding_sha256"] = digest

    def call(sent: dict[str, Any]) -> dict[str, Any]:
        return h.request_launchplane(service_url=settings["service_url"], path=path, settings=settings,
                                     body=sent, timeout=args.timeout)

    response: dict[str, Any] | None = None
    try:
        response = call(payload)
        result = project(h, response, mode, body, digest)
    except urllib.error.HTTPError as error:
        error_payload = h._ServiceResponse({}, error.code)
        try:
            try:
                error_payload = h._error_payload_from_http(error)
                failure = h.summarize_http_error(operation=args.command, request=request, exc=error,
                                                 provider_payload=error_payload)
            except (h.LaunchplaneSafetyError, ValueError, OSError, http.client.HTTPException):
                failure = h.unavailable_payload(operation=args.command, request=request, status="unavailable",
                    code="invalid_response", message="The service error could not be verified.")
        finally:
            error.close()
        if mode == "inspect" or (400 <= error.code < 500 and error.code != 408):
            if mode == "collect" and error.code == 409:
                failure["summary"]["recommendation"] = (
                    "The service refused this collection. A refusal can follow publication, so an observation "
                    "may still be stored. Inspect again before relying on any fact; do not retry automatically.")
            h.emit(failure)
            return 1
        return _outcome_unknown(h, args, request, h.service_response_diagnostics(error_payload))
    except h.ServiceResponseError as error:
        if mode == "inspect":
            h.emit_invalid_response(operation=args.command, request=request,
                                    diagnostics=h.service_response_diagnostics(error))
            return 1
        return _outcome_unknown(h, args, request, h.service_response_diagnostics(error))
    except h.LaunchplaneSafetyError as error:
        h.emit_safety_error_payload(operation=args.command, request=request, exc=error, provider_payload=response)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as error:
        if mode == "inspect":
            h.emit_read_transport_error(operation=args.command, request=request, timeout=args.timeout, exc=error)
            return 1
        return _outcome_unknown(h, args, request, h.service_response_diagnostics(error))
    status = "accepted"
    readback: dict[str, object] = {}
    if mode == "inspect":
        result["service_fingerprint"] = service
        result["review_binding_sha256"] = review_binding(h, scope, str(result["binding_sha256"]), service)
    else:
        status, readback = _readback(h, call, body, mode, digest, result)
    output = h.base_payload(status=status, operation=args.command, request=request)
    output["result"] = result
    output["summary"] = {**h.service_response_diagnostics(response),
                         **{"read_back_" + key: value for key, value in readback.items()},
                         "recommendation": _recommendation(mode, status, body)}
    h.assert_public_safe_shape(output["summary"])
    h.emit(output)
    return 0 if status == "accepted" else 1


def _recommendation(mode: str, status: str, body: dict[str, Any]) -> str:
    if mode == "inspect":
        return ("Save this output privately and review the binding. It is required evidence for collect, record "
                "and clear. Facts stay unknown until the service collects them.")
    if status != "accepted":
        return ("The service accepted the request, but the lane binding could not be confirmed afterwards. "
                "Inspect again before relying on it; do not retry automatically.")
    if mode == "record" and "preview_id" in body:
        return "The preview observation is recorded and a preview reconcile was requested. Stored facts cannot be read back."
    if mode == "clear":
        return "The observation row for this lane was cleared. No provider state changed."
    return "The observation is stored for a short time. Stored facts cannot be read back; only the lane binding was rechecked."


def _outcome_unknown(h: Any, args: argparse.Namespace, request: dict[str, object],
                     diagnostics: dict[str, object]) -> int:
    output = h._apply_outcome_unknown(operation=args.command, request=request, label="disposable lane observation")
    for key in ("trace_id", "error_code", "http_status"):
        if key in diagnostics:
            output["summary"][key] = diagnostics[key]
    output["summary"]["recommendation"] = RECOVERY
    output["warnings"] = [h.warning("outcome_unknown", "Do not retry automatically; inspect the lane first.")]
    h.emit(output)
    return 1
