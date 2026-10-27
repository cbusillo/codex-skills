# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Bounded record repairs through the configured Launchplane service identity.

Review digests are helper checks. The service owns authorization, provider
projection checks, no-op enforcement, and route-binding record CAS.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import io
import json
import re
import urllib.error
import urllib.parse
from typing import Any

DOMAIN = "dokploy-target-repair-domain-authority"
INGRESS = "ingress-route"
COMMANDS = {
    DOMAIN + "-dry-run", DOMAIN + "-apply", INGRESS + "-dry-run",
    INGRESS + "-authority-apply", "route-authority-discover", "route-binding-read",
    "route-binding-dry-run", "route-binding-apply",
}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def stable_record(record: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(record)
    for field in ("updated_at", "source_label", "record_sha256"):
        value.pop(field, None)
    if isinstance(value.get("source"), dict):
        for field in ("refreshed_at", "stale_after"):
            value["source"].pop(field, None)
    return value


def _host_material(h: Any, host: dict[str, Any]) -> dict[str, Any]:
    return {key: host.get(key) for key in (
        *h.INGRESS_PLAN_ROUTE_FIELDS, "id", "forward_host", "forward_port", "forward_scheme")}


def _scope(h: Any, args: Any) -> dict[str, str]:
    scope = {key: h._plan_identifier(getattr(args, key), key)
             for key in ("product", "context", "instance")}
    if not h.PRODUCT_READ_PATH_SEGMENT_RE.fullmatch(scope["product"]):
        raise ValueError("invalid_product")
    return scope


def _apply_evidence(s: Session, payload: dict[str, Any], error: Any = None) -> None:
    """Keep accepted-call evidence when a later read or projection fails."""
    h = s.h
    payload["summary"].update(h.service_response_diagnostics(error))
    payload["summary"].update(h.service_response_diagnostics(s.apply_response))
    if not s.apply_response:
        return
    if s.apply_response.get("replayed") is True:
        try:
            payload["summary"]["original_trace_id"] = h.public_trace_id(s.apply_response.get("original_trace_id"))
        except h.LaunchplaneSafetyError:
            pass
    if s.args.command == "ingress-route-authority-apply":
        try:
            records = h._require_dict(s.apply_response.get("records"))
            payload["records"] = {"ingress_route_audit_record_id": h.public_identifier(
                records.get("ingress_route_audit_record_id"))}
        except h.LaunchplaneSafetyError:
            pass


class Session:
    def __init__(self, h: Any, args: Any, settings: dict[str, str], request: dict[str, Any]):
        self.h, self.args, self.settings = h, args, settings
        self.provider: dict[str, Any] | None = None
        self.reads: list[dict[str, Any]] = []
        self.apply_started = False
        self.apply_accepted = False
        self.request = request
        self.apply_response: dict[str, Any] | None = None
        self.readback_view = "full"

    def read(self, path: str, query: dict[str, str]) -> dict[str, Any]:
        self.provider = None
        self.provider = self.h.request_launchplane_read(
            service_url=self.settings["service_url"], path=path, settings=self.settings,
            query=query, timeout=self.args.timeout)
        if self.provider.get("status") != "ok":
            raise self.h.LaunchplaneSafetyError("invalid_response")
        self.reads.append(copy.deepcopy(self.provider))
        return self.provider

    def post(self, command: str, body: dict[str, Any], *, apply: bool = False) -> dict[str, Any]:
        self.provider = None
        self.request["planner_request_sent"] = True
        self.request["authorization_action"] = (
            "dokploy_target.repair_domain_authority" + ("" if apply else ".plan")
            if command.startswith(DOMAIN) else
            "ingress_route.apply" if apply and command.startswith(INGRESS) else
            "ingress_route.plan" if command.startswith(INGRESS) else
            "route_binding.apply" if apply else "route_binding.read")
        self.request["authorization_context"] = "launchplane" if command.startswith(DOMAIN) else body["context"]
        if apply:
            self.apply_started = True
        self.provider = self.h.request_launchplane(
            service_url=self.settings["service_url"], path=self.h.helper_command_path(command),
            settings=self.settings, body=body, timeout=self.args.timeout,
            idempotency_key=self.args.idempotency_key if apply else "")
        self.apply_accepted = apply and self.provider.get("status") == "accepted"
        if apply:
            self.apply_response = self.provider
        return self.provider

    def current(self, scope: dict[str, str]) -> tuple[dict[str, str], dict[str, Any] | None]:
        try:
            record = self.h._require_dict(self.read(
                self.h.helper_command_path("route-binding-read"), scope).get("record"))
        except urllib.error.HTTPError as exc:
            # Absence is established only by this exact scoped route's structured 404.
            error = self.h._error_payload_from_http(exc)
            self.provider = error
            if exc.code == 404 and error.get("error", {}).get("code") == "not_found":
                # Generic unknown-route 404s have the same code. Confirm absence
                # through the independent, exactly scoped collection read.
                listing = self.read(self.h.internal_helper_path("route-authority-binding-list"),
                                    {**scope, "limit": "2"})
                if (any(listing.get(key) != value for key, value in scope.items())
                    or listing.get("records") != [] or listing.get("count") != 0
                    or not self.h.public_trace_id(listing.get("trace_id"))):
                    raise ValueError("binding_current_unavailable")
                return {"state": "absent"}, None
            raise urllib.error.HTTPError(exc.url, exc.code, exc.msg, exc.headers,
                                         io.BytesIO(json.dumps(error).encode())) from None
        if any(record.get(key) != value for key, value in scope.items()):
            raise ValueError("binding_scope_mismatch")
        sha = record.get("record_sha256")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("binding_digest_missing")
        return {"state": "present", "record_sha256": sha}, record


def _plan(s: Session, kind: str, scope: dict[str, str], ingress: dict[str, Any] | None):
    h, args = s.h, s.args
    s.reads = []
    if kind == "domain":
        body = h._domain_authority_plan_body(args, s.read)
        observed = s.reads[-1]["inspect"]
        sources = {key: observed[key] for key in ("tracked_target", "provider_target_record")}
        response = s.post(DOMAIN + "-dry-run", body)
        public = h._project_inert_plan(response, body)
        material = {"body": body, "sources": sources}
    elif kind == "ingress":
        body = h._ingress_plan_body(ingress, s.read)
        profile, endpoint = s.reads[0], s.reads[-1]
        lane = next(lane for lane in profile["profile"]["lanes"]
                    if lane.get("context") == scope["context"] and lane.get("instance") == scope["instance"])
        response = s.post(INGRESS + "-dry-run", body)
        public = h._project_inert_plan(response, body)
        host = h._require_dict(response["result"].get("proxy_host"))
        # Bind resolved upstream and all supported options, without exporting them.
        certificate = host.get("certificate_id")
        if type(certificate) is not int or certificate < 0:
            raise ValueError("tls_ownership_unknown")
        material = {"body": body, "lane": lane, "endpoint": endpoint["record"],
                    "host": _host_material(h, host)}
        if len(s.reads) > 2:
            material["tracked_target"] = s.reads[1]["inspect"]["tracked_target"]
    else:
        profile = s.read(h._product_read_path("product-profile-read", product=scope["product"]), {})
        _lane_from_profile(h, profile, scope)
        expected, _record = s.current(scope)
        body = {"schema_version": 1, **scope, "mode": "dry-run", "expected_current": expected,
                "source_label": "operator-reconcile", "reason": args.reason}
        response = s.post("route-binding-dry-run", body)
        public, candidate = _binding_result(h, response, body)
        material = {"body": body, "candidate": candidate}
    return body, public, material, response


def _lane_from_profile(h: Any, response: dict[str, Any], scope: dict[str, str]):
    profile = h._require_dict(response.get("profile"))
    if profile.get("product") != scope["product"] or not isinstance(profile.get("lanes"), list):
        raise ValueError("invalid_profile")
    matches = [(profile, lane) for lane in profile["lanes"] if isinstance(lane, dict)
               and all(lane.get(key) == scope[key] for key in ("context", "instance"))]
    if len(matches) != 1:
        raise ValueError("plan_lane_not_unique")
    return matches[0]


def _binding_result(h: Any, response: dict[str, Any], body: dict[str, Any]):
    if response.keys() - h.SUCCESS_TOP_LEVEL_KEYS or response.get("status") != "accepted":
        raise ValueError("invalid_binding_response")
    result = h._require_dict(response.get("result"))
    if result.keys() - {"status", "operation", "findings", "current_record_sha256", "candidate_record_sha256",
                        "record", "mode", "route_binding_status"}:
        raise ValueError("invalid_binding_response")
    if result.get("mode") != body["mode"] or result.get("status") not in {"ready", "unchanged", "blocked", "conflict"}:
        raise ValueError("invalid_binding_response")
    records = h._require_dict(response.get("records"))
    if any(records.get(key) != body[key] for key in ("product", "context", "instance")):
        raise ValueError("binding_scope_mismatch")
    findings = result.get("findings", [])
    if not isinstance(findings, list) or len(findings) > 100:
        raise ValueError("invalid_binding_findings")
    public = {"mode": body["mode"], "status": result["status"],
              "operation": h.public_code(result.get("operation")),
              "route_binding_status": h.public_code(result.get("route_binding_status")),
              "finding_codes": [h.public_code(h._require_dict(finding).get("code")) for finding in findings]}
    if result["status"] in {"blocked", "conflict"}:
        return public, None
    expected = body["expected_current"].get("record_sha256", "")
    if result.get("current_record_sha256") != expected:
        raise ValueError("binding_current_mismatch")
    record = h._require_dict(result.get("record"))
    if any(record.get(key) != body[key] for key in ("product", "context", "instance")):
        raise ValueError("binding_scope_mismatch")
    source = h._require_dict(record.get("source"))
    if (record.get("status") != "active" or h._require_dict(record.get("tls")).get("owner") not in {"provider", "launchplane", "external", "none"}
        or source.get("source_kind") != "service" or source.get("freshness_status") != "recorded"
        or not isinstance(source.get("source_versions"), dict) or not source["source_versions"]
        or set(source.get("source_record_ids", [])) != set(source["source_versions"])):
        raise ValueError("binding_source_unverified")
    sha = result.get("candidate_record_sha256")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha) or record.get("record_sha256") != sha:
        raise ValueError("binding_candidate_mismatch")
    h._plan_domains([h._require_dict(entry).get("domain_name") for entry in record.get("domains", [])])
    public.update(source_count=len(source["source_versions"]), source_versions_verified=True)
    return public, stable_record(record)


def _review(h: Any, args: Any, kind: str, scope: dict[str, str]):
    expected = h._reviewed_apply_digest(args)
    plan_command = DOMAIN + "-dry-run" if kind == "domain" else INGRESS + "-dry-run" if kind == "ingress" else "route-binding-dry-run"
    evidence, result = h._load_reviewed_evidence(args, operation=plan_command,
        expected_digest=expected, result_status=None)
    if (any(evidence["request"].get(key) != value for key, value in scope.items())
        or result.get("apply_eligible") is not True):
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    return expected


def _readback(s: Session, kind: str, scope: dict[str, str], body: dict[str, Any],
              material: dict[str, Any], response: dict[str, Any]) -> bool:
    h = s.h
    if kind == "domain":
        observed_body = h._domain_authority_plan_body(s.args, s.read)
        observed = s.reads[-1]["inspect"]
        before = stable_record(material["sources"]["tracked_target"])
        before["domains"] = body["domains"]
        return (observed_body["domains"] == body["domains"]
                and observed_body["target_id"] == body["target_id"]
                and observed_body["expected_current_provider_target"] == body["expected_current_provider_target"]
                and stable_record(observed["tracked_target"]) == before
                and stable_record(observed["provider_target_record"]) == stable_record(material["sources"]["provider_target_record"]))
    if kind == "ingress":
        audit_id = h.public_identifier(response["records"].get("ingress_route_audit_record_id"))
        audit = h._require_dict(s.read(h.internal_helper_path("route-authority-audit-read").format(
            record_id=urllib.parse.quote(audit_id, safe="")),
            {"product": scope["product"], "context": scope["context"]}).get("record"))
        ingress = body["ingress"]
        operations = copy.deepcopy(response["result"]["operations"])
        limited = (audit.get("expected_host_id") is None and audit.get("provider_host_id") is None
                   and audit.get("provider_certificate_ref") == "")
        if limited:
            s.readback_view = "audit_read_view"
            for operation in operations:
                operation["host_id"] = None
        return (audit.get("record_id") == audit_id and audit.get("mode") == "apply"
                and audit.get("dry_run") is False and audit.get("status") == "unchanged"
                and audit.get("product") == scope["product"] and audit.get("context") == scope["context"]
                and audit.get("edge_endpoint_key") == ingress["route"]["edge_endpoint_key"]
                and (limited or (audit.get("expected_host_id") == ingress["expected_host_id"]
                                 and audit.get("provider_host_id") == ingress["expected_host_id"]))
                and set(audit.get("requested_domains", [])) == set(ingress["route"]["domain_names"])
                and audit.get("tls_owner") == ("provider" if material["host"]["certificate_id"] else "none")
                and (limited or audit.get("provider_certificate_ref") == (str(material["host"]["certificate_id"]) if material["host"]["certificate_id"] else ""))
                and audit.get("operations") == operations
                and audit.get("trace_id") == (response.get("original_trace_id") if response.get("replayed") is True else response["trace_id"])
                and audit.get("idempotency_key") == s.args.idempotency_key)
    expected, record = s.current(scope)
    return (record is not None and stable_record(record) == material["candidate"]
            and expected.get("record_sha256") == response["result"].get("candidate_record_sha256"))


def execute(h: Any, args: Any) -> int:
    apply = args.command.endswith("-apply")
    kind = "domain" if args.command.startswith(DOMAIN) else "ingress" if args.command.startswith(INGRESS) else "binding"
    ingress = h._ingress_plan_input(args) if kind == "ingress" else None
    if ingress:
        scope = {key: ingress[key] for key in ("product", "context", "instance")}
    elif kind == "domain":
        scope = {"product": "launchplane", **{key: h._plan_identifier(getattr(args, key), key)
                                               for key in ("context", "instance")}}
    else:
        scope = _scope(h, args)
    if hasattr(args, "reason") and not args.reason.strip():
        raise ValueError("reason_required")
    expected = _review(h, args, kind, scope) if apply else None
    request = {**scope, "mode": "apply" if apply else "read" if args.command.endswith(("read", "discover")) else "dry-run",
               "planner_request_sent": False}
    settings = h.prepare_operator_settings(args=args, operation=args.command, request=request)
    if settings is None:
        return 2
    s = Session(h, args, settings, request)
    try:
        if args.command == "route-authority-discover":
            profile, lane = _lane_from_profile(h, s.read(h._product_read_path("product-profile-read", product=scope["product"]), {}), scope)
            expectation, record = s.current(scope)
            result = {"mode": "read", "lane_verified": True, "binding_state": expectation["state"],
                      "public_url_recorded": bool(lane.get("base_url")),
                      "production_use": h.public_code(profile.get("production_use"), default="unknown")}
        elif args.command == "route-binding-read":
            expectation, record = s.current(scope)
            result = {"mode": "read", "binding_state": expectation["state"]}
            if record is not None:
                result["record_sha256"] = expectation["record_sha256"]
        else:
            body, result, material, response = _plan(s, kind, scope, ingress)
            eligible = (kind != "binding" or result["status"] in {"ready", "unchanged"}) and not (
                kind == "domain" and body.get("expected_current_provider_target_fingerprint")
            )
            result.update(plan_sha256=digest(material), apply_eligible=eligible)
            if apply:
                if not eligible or result["plan_sha256"] != expected:
                    payload = h.base_payload(status="stale", operation=args.command, request=request)
                    payload["summary"] = {"recommendation": "Fresh service evidence differs from the reviewed plan. Review a new dry run."}
                    h.emit(payload)
                    return 1
                if kind == "domain":
                    body.update(mode="apply", confirmation="APPLY DOKPLOY TARGET SETUP")
                elif kind == "ingress":
                    body["ingress"]["mode"] = "apply"
                else:
                    body.update(mode="apply", confirmation="APPLY LAUNCHPLANE ROUTE BINDING RECONCILE")
                response = s.post(args.command, body, apply=True)
                if kind == "binding":
                    applied, candidate = _binding_result(h, response, body)
                    if applied["status"] in {"blocked", "conflict"}:
                        payload = h.base_payload(status="stale", operation=args.command, request=request)
                        payload["result"] = {**applied, "applied": False}
                        payload["summary"] = {"trace_id": h.public_trace_id(response.get("trace_id")),
                            "recommendation": "The service refused the changed plan before writing. Review fresh evidence."}
                        h.emit(payload)
                        return 1
                    if candidate != material["candidate"]:
                        raise ValueError("binding_apply_mismatch")
                    result = applied
                else:
                    checked = copy.deepcopy(response)
                    if kind == "ingress" and checked.get("replayed") is True:
                        count = checked.get("records", {}).get("ingress_provider_domain_count")
                        if not isinstance(count, str) or not re.fullmatch(r"[1-9][0-9]{0,2}", count):
                            raise ValueError("invalid_replay_records")
                        checked["records"]["ingress_provider_domain_count"] = int(count)
                    if kind == "domain":
                        if checked["result"].get("applied") is not True or checked["result"]["setup"].get("applied") is not True or checked["result"].get("mode") != "apply":
                            raise ValueError("domain_apply_mismatch")
                        checked["result"].update(mode="dry-run", applied=False)
                        checked["result"]["setup"]["applied"] = False
                    else:
                        if checked["result"].get("dry_run") is not False:
                            raise ValueError("ingress_apply_mismatch")
                        if _host_material(h, h._require_dict(checked["result"].get("proxy_host"))) != material["host"]:
                            raise ValueError("ingress_apply_mismatch")
                        checked["result"]["dry_run"] = True
                    checked_body = copy.deepcopy(body)
                    if kind == "ingress":
                        checked_body["ingress"]["mode"] = "dry-run"
                    result = h._project_inert_plan(checked, checked_body)
                result.update(mode="apply", plan_sha256=expected)
                if kind == "domain":
                    result["applied"] = True
                elif kind == "ingress":
                    result["dry_run"] = False
                accepted_response = response
                matches = _readback(s, kind, scope, body, material, response)
                result["read_back_matches"] = matches
                payload = h.base_payload(status="accepted" if matches else "accepted_unverified", operation=args.command, request=request)
                payload["result"] = result
                if kind == "ingress":
                    result["read_back_view"] = s.readback_view
                    payload["records"] = {"ingress_route_audit_record_id": h.public_identifier(
                        accepted_response["records"].get("ingress_route_audit_record_id"))}
                payload["summary"] = {"trace_id": h.public_trace_id(accepted_response.get("trace_id")),
                                      "recommendation": "Record read-back verified." if matches else "Read-back differs; inspect the records before any retry."}
                if accepted_response.get("replayed") is True:
                    payload["summary"]["original_trace_id"] = h.public_trace_id(accepted_response.get("original_trace_id"))
                h.emit(payload)
                return 0 if matches else 1
        payload = h.base_payload(status="accepted", operation=args.command, request=request)
        payload["result"] = result
        if s.provider:
            payload["summary"] = {"trace_id": h.public_trace_id(s.provider.get("trace_id"))}
        if kind == "ingress":
            payload["records"] = {"ingress_route_audit_record_id": h.public_identifier(response["records"].get("ingress_route_audit_record_id"))}
        h.emit(payload)
        return 0
    except urllib.error.HTTPError as exc:
        if s.apply_accepted:
            payload = h._apply_response_unverified(operation=args.command, request=request, label="route authority")
            _apply_evidence(s, payload)
            h.emit(payload)
        elif s.apply_started and (exc.code >= 500 or exc.code == 408):
            payload = h._apply_outcome_unknown(operation=args.command, request=request, label="route authority")
            payload["summary"].update(h.service_response_diagnostics(h._error_payload_from_http(exc)))
            h.emit(payload)
        else:
            payload = h.summarize_http_error(operation=args.command, request=request, exc=exc)
            if s.apply_started and payload["summary"].get("error_code") in {"mutation_in_progress", "mutation_reconciliation_required"}:
                payload["status"] = "outcome_unknown"
                payload["summary"]["recommendation"] = "The service reports an unresolved mutation. Resolve its state before any retry; retain the original key."
            h.emit(payload)
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
        if s.apply_started:
            payload = (h._apply_response_unverified(operation=args.command, request=request, label="route authority") if s.apply_accepted
                       else h._apply_outcome_unknown(operation=args.command, request=request, label="route authority"))
            _apply_evidence(s, payload, exc)
            h.emit(payload)
        else:
            h.emit_provider_unavailable(operation=args.command, request=request)
    except (ValueError, KeyError, TypeError, h.LaunchplaneSafetyError) as exc:
        if s.apply_started:
            payload = (h._apply_outcome_unknown(operation=args.command, request=request, label="route authority")
                       if not s.apply_accepted and isinstance(exc, h.LaunchplaneSafetyError) and exc.code == "unsafe_redirect"
                       else h._apply_response_unverified(operation=args.command, request=request, label="route authority"))
            payload["summary"].update(h.service_response_diagnostics(s.provider))
            _apply_evidence(s, payload, exc)
            h.emit(payload)
        else:
            code = str(exc)
            if isinstance(exc, ValueError) and code in {
                "plan_lane_not_unique", "invalid_plan_lane_url", "plan_domain_scope_mismatch",
                "plan_endpoint_not_active", "invalid_plan_domains", "invalid_product",
                "tls_ownership_unknown", "binding_current_mismatch", "binding_source_unverified",
                "binding_current_unavailable",
            }:
                payload = h.unavailable_payload(operation=args.command, request=request, status="invalid",
                    code=code, message="Service evidence did not match the requested plan scope.")
                payload["summary"].update(error_code=code, **h.service_response_diagnostics(s.provider))
                h.emit(payload)
            else:
                h.emit_safety_error_payload(operation=args.command, request=request,
                    exc=exc if isinstance(exc, h.LaunchplaneSafetyError) else h.LaunchplaneSafetyError("invalid_response"), provider_payload=s.provider)
    return 1
