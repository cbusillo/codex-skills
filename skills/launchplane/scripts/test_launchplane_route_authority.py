#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.2.0"]
# ///
"""Offline reviewed repair, CAS, drift, refusal and uncertain-effect checks."""
from __future__ import annotations

import copy
import io
import json
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

import pytest
from test_launchplane_helpers import write_action as h
from test_launchplane_inert_planners import (
    DOMAIN_COMMAND,
    DOMAINS,
    INGRESS_COMMAND,
    assert_private_dropped,
    domain_response,
    fingerprint_fixture,
    ingress_input,
    ingress_reads,
    ingress_response,
    inspection,
)

SCOPE = {"product": "shop", "context": "shop", "instance": "prod"}


def http_error(status=404, code="not_found"):
    return urllib.error.HTTPError("https://example.invalid", status, "private prose", {},
        io.BytesIO(json.dumps({"trace_id": "launchplane_req_failure", "error": {
            "code": code, "message": "PRIVATE_SENTINEL"}}).encode()))


def profiles():
    return {"status": "ok", "trace_id": "launchplane_req_profiles", "profile": {
        "product": SCOPE["product"], "production_use": "live", "lanes": [
            {"context": SCOPE["context"], "instance": SCOPE["instance"],
             "base_url": "https://shop.example.invalid"}]}}


def absent():
    return [http_error(), {"status": "ok", "trace_id": "launchplane_req_list", **SCOPE,
                           "records": [], "count": 0, "limit": 2}]


def binding_record():
    return {**SCOPE, "status": "active", "record_sha256": "a" * 64,
        "domains": [{"domain_name": DOMAINS[0], "role": "primary"}],
        "provider_target": {"provider_id": "dokploy", "target_name": "PRIVATE_SENTINEL"},
        "ingress": {"provider": "npmplus", "endpoint_key": "edge-shop"},
        "tls": {"owner": "provider"}, "updated_at": "2026-10-14T22:00:00Z",
        "source": {"source_kind": "service", "source_label": "operator-reconcile",
                   "source_record_ids": ["target:shop:prod"],
                   "source_versions": {"target:shop:prod": "2026-10-14T21:00:00Z"},
                   "freshness_status": "recorded", "refreshed_at": "2026-10-14T22:00:00Z",
                   "stale_after": "2026-10-15T21:00:00Z"}}


def binding_response(mode: str = "dry-run", status: str = "ready", current: str = "") -> dict[str, Any]:
    return {"status": "accepted", "trace_id": "launchplane_req_binding", "records": {
        **SCOPE, "route_binding_status": "created" if mode == "apply" else "planned_create"},
        "result": {"mode": mode, "status": status, "operation": "create", "findings": [],
                   "current_record_sha256": current, "candidate_record_sha256": "a" * 64,
                   "record": binding_record(),
                   "route_binding_status": "created" if mode == "apply" else "planned_create"}}


def run(command, reads, posts=(), evidence=None, private=None, extra=()):
    with TemporaryDirectory() as directory:
        root = Path(directory)
        payload_path = root / "route.json"
        payload_path.write_text(json.dumps(private or ingress_input()))
        if command.startswith("dokploy-target-repair-domain-authority"):
            argv = [command, "--context", "shop", "--instance", "prod", "--reason", "private reason"]
        elif command.startswith("ingress-route"):
            argv = [command, "--payload-file", str(payload_path)]
        else:
            argv = [command, *[arg for key, value in SCOPE.items() for arg in ("--" + key, value)]]
            if command.endswith(("dry-run", "apply")):
                argv += ["--reason", "private reason"]
                if private is not None and command == "route-binding-dry-run":
                    argv += ["--ingress-payload-file", str(payload_path)]
        if command.endswith("-apply"):
            evidence_path = root / "review.json"
            evidence_path.write_text(json.dumps(evidence))
            argv += ["--reviewed-dry-run", "--expected-plan-digest", evidence["result"]["plan_sha256"],
                     "--idempotency-key", "record-repair-example", "--dry-run-evidence-file", str(evidence_path)]
        stdout = io.StringIO()
        with patch.object(h, "prepare_operator_settings", return_value={"service_url": "https://example.invalid"}), \
             patch.object(h, "request_launchplane_read", side_effect=[copy.deepcopy(x) if isinstance(x, dict) else x for x in reads]) as get, \
             patch.object(h, "request_launchplane", side_effect=[copy.deepcopy(x) if isinstance(x, dict) else x for x in posts]) as post, redirect_stdout(stdout):
            code = h.main(argv + list(extra))
        payload = json.loads(stdout.getvalue())
        assert_private_dropped(payload)
        return code, payload, get, post


def domain_plan():
    return run(DOMAIN_COMMAND, [inspection()], [domain_response()])[1]


def domain_apply_response():
    response = domain_response()
    response["result"].update(mode="apply", applied=True)
    response["result"]["setup"]["applied"] = True
    return response


def domain_readback():
    record = inspection()
    record["inspect"]["tracked_target"]["domains"] = DOMAINS
    return record


def test_fingerprint_plan_cannot_be_promoted_to_apply():
    observed, response = fingerprint_fixture()
    code, evidence, _, _ = run(DOMAIN_COMMAND, [observed], [response])
    assert code == 0 and evidence["result"]["apply_eligible"] is False
    code, _, get, post = run("dokploy-target-repair-domain-authority-apply", [], [], evidence)
    assert code != 0
    get.assert_not_called()
    post.assert_not_called()
    # A caller cannot turn an old canonical receipt into fingerprint apply either.
    code, _, _, post = run("dokploy-target-repair-domain-authority-apply",
                          [observed], [response], domain_plan())
    assert code != 0 and post.call_count == 1
    assert post.call_args.kwargs["body"]["mode"] == "dry-run"


def test_domain_apply_rechecks_reviewed_exact_domains_then_reads_back():
    evidence = domain_plan()
    code, output, get, post = run("dokploy-target-repair-domain-authority-apply",
        [inspection(), domain_readback()], [domain_response(), domain_apply_response()], evidence)
    assert code == 0 and output["result"]["read_back_matches"] is True
    assert post.call_count == 2 and get.call_count == 2
    call = post.call_args.kwargs
    assert call["body"]["domains"] == DOMAINS
    assert call["body"]["mode"] == "apply" and call["idempotency_key"] == "record-repair-example"


@pytest.mark.parametrize("cause", ["domains", "metadata", "identity"])
def test_domain_drift_stops_before_apply(cause):
    observed = inspection()
    if cause == "domains":
        observed["inspect"]["provider"]["domains"].append({"host": "alias.example.invalid"})
    elif cause == "metadata":
        observed["inspect"]["tracked_target"]["env_keys"] = ["NEW_SETTING"]
    else:
        observed["inspect"]["provider"]["id"] = "another-target"
    code, output, _, post = run("dokploy-target-repair-domain-authority-apply", [observed],
        [domain_response()], domain_plan())
    assert code != 0
    assert all(call.kwargs["body"]["mode"] == "dry-run" for call in post.call_args_list)


def test_domain_readback_metadata_mismatch_is_not_success():
    observed = domain_readback()
    observed["inspect"]["tracked_target"]["env_keys"] = ["DRIFT"]
    code, output, _, _ = run("dokploy-target-repair-domain-authority-apply", [inspection(), observed],
        [domain_response(), domain_apply_response()], domain_plan())
    assert code != 0 and output["status"] == "accepted_unverified"


@pytest.mark.parametrize("failure,status", [
    (TimeoutError("PRIVATE_SENTINEL"), "outcome_unknown"),
    (http_error(503, "service_unavailable"), "outcome_unknown"),
    (h.LaunchplaneSafetyError("unsafe_redirect"), "outcome_unknown"),
    (ValueError("PRIVATE_SENTINEL"), "accepted_unverified"),
    (http_error(403, "authorization_denied"), "denied"),
])
def test_apply_failure_keeps_unknown_separate_from_denial_and_never_retries(failure, status):
    code, output, _, post = run("dokploy-target-repair-domain-authority-apply", [inspection()],
        [domain_response(), failure], domain_plan())
    assert code != 0 and output["status"] == status and post.call_count == 2
    if status == "denied":
        assert output["summary"]["trace_id"] == "launchplane_req_failure"
        assert output["summary"]["error_code"] == "authorization_denied"


def test_review_of_other_scope_cannot_apply_or_read():
    evidence = domain_plan()
    evidence["request"]["instance"] = "testing"
    code, _, get, post = run("dokploy-target-repair-domain-authority-apply", [], [], evidence)
    assert code != 0 and get.call_count == post.call_count == 0


def ingress_plan():
    return run(INGRESS_COMMAND, ingress_reads(), [ingress_response()])[1]


def test_current_verification_uses_audit_action_without_apply_key() -> None:
    private = ingress_input()
    private["verify_route_binding"] = True
    code, output, _, post = run(INGRESS_COMMAND, verification_reads(), verification_posts(), private=private)
    assert code == 0
    assert post.call_args.kwargs["body"]["ingress"]["verify_route_binding"] is True
    assert post.call_args.kwargs["idempotency_key"] == ""
    assert output["result"]["apply_eligible"] is False


def observed_host_response() -> dict[str, Any]:
    response: dict[str, Any] = ingress_response()
    host = response["result"]["proxy_host"]
    host.update({key: False for key in h.INGRESS_PLAN_ROUTE_FIELDS - {
        "domain_names", "edge_endpoint_key", "certificate_id", "access_list_id",
        "npmplus_x_frame_options", "npmplus_auth_request", "enabled"}})
    host.update(access_list_id=0, npmplus_x_frame_options="SAMEORIGIN", npmplus_auth_request="none", enabled=True)
    return response


def verification_reads() -> list[dict[str, Any]]:
    tracked = inspection()
    tracked["inspect"]["tracked_target"]["domains"] = DOMAINS
    audit = audit_readback("dry-run")
    audit["record"].update(idempotency_key="",
                           provider_route_sha256="b" * 64,
                           route_verification={"instance": SCOPE["instance"], "sources_sha256": "c" * 64})
    return [*ingress_reads(), tracked, audit]


def verification_posts() -> list[Any]:
    return [observed_host_response(), observed_host_response()]


def blocked_binding(code: str) -> dict[str, Any]:
    response = binding_response(status="blocked")
    response["result"]["findings"] = [{"code": code, "detail": "PRIVATE_SENTINEL"}]
    return response


@pytest.mark.parametrize("finding", ["ingress_audit_missing", "ingress_verification_required"])
def test_missing_history_or_expired_sources_verify_then_review_without_ingress_apply(finding: str) -> None:
    private = ingress_input()
    private.pop("expected_host_id")
    reads = [profiles(), *absent(), *verification_reads(), *absent()]
    posts = [blocked_binding(finding), *verification_posts(), binding_response()]
    code, plan, _, post = run("route-binding-dry-run", reads, posts, private=private)
    assert code == 0 and plan["result"]["verification_refreshed"] is True
    comparison = post.call_args_list[1].kwargs["body"]["ingress"]
    assert "verify_route_binding" not in comparison and "expected_host_id" not in comparison
    assert comparison["mode"] == "dry-run"
    observation = post.call_args_list[2].kwargs["body"]["ingress"]
    assert observation["verify_route_binding"] is True
    assert observation["require_exact_expected_host_domains"] is True
    assert all(observation[key] is False for key in ("allow_create", "allow_update", "allow_enable_disable"))
    assert all(call.kwargs["idempotency_key"] == "" for call in post.call_args_list)
    readback = {"status": "ok", "record": binding_record()}
    code, output, _, post = run("route-binding-apply", [profiles(), *absent(), readback],
        [binding_response(), binding_response("apply")], plan, private=private)
    assert code == 0 and output["result"]["read_back_matches"] is True
    assert all("ingress" not in call.kwargs["body"] for call in post.call_args_list)


def test_expiry_during_review_returns_to_dry_run_without_observation_or_apply() -> None:
    code, output, _, post = run("route-binding-apply", [profiles(), *absent()],
        [blocked_binding("ingress_verification_required")], binding_plan())
    assert code != 0 and output["status"] == "stale" and post.call_count == 1
    assert post.call_args.kwargs["body"]["mode"] == "dry-run"


@pytest.mark.parametrize("codes", [["ingress_audit_unresolved"], ["ingress_verification_required", "ingress_audit_unresolved"]])
def test_pending_original_blocks_refresh_even_with_an_observation_finding(codes: list[str]) -> None:
    response = blocked_binding(codes[0])
    response["result"]["findings"] = [{"code": code} for code in codes]
    code, output, _, post = run("route-binding-dry-run", [profiles(), *absent()], [response], private=ingress_input())
    assert code == 0 and output["result"]["apply_eligible"] is False
    assert output["result"]["finding_codes"] == codes and post.call_count == 1


@pytest.mark.parametrize("cause", ["domains", "target", "scope", "settings", "host", "ambiguous", "unavailable", "drift", "audit"])
def test_current_fact_failures_never_reconcile(cause: str) -> None:
    reads, posts = verification_reads(), verification_posts()
    if cause == "domains":
        reads[2]["inspect"]["tracked_target"]["domains"] = ["other.example.invalid"]
    elif cause == "target":
        reads[2]["inspect"]["target_id"] = "other"
    elif cause == "scope":
        reads[2]["inspect"]["tracked_target"]["instance"] = "other"
    elif cause == "settings":
        posts[0]["result"]["proxy_host"].pop("ssl_forced")
    elif cause == "host":
        posts[0]["result"]["proxy_host"]["id"] += 1
    elif cause in {"ambiguous", "unavailable"}:
        posts[0] = http_error(400 if cause == "ambiguous" else 403, "invalid_request" if cause == "ambiguous" else "authorization_denied")
    elif cause == "drift":
        posts[1]["result"]["status"] = "planned"
        posts[1]["result"]["operations"][0].update(action="update", requires_apply=True, change_categories=["tls"])
    else:
        reads[3]["record"]["route_verification"]["instance"] = "other"
    private: dict[str, Any] = {**ingress_input(), "verify_route_binding": True}
    code, output, _, post = run("route-binding-dry-run", [profiles(), *absent(), *reads],
        [blocked_binding("ingress_audit_missing"), *posts], private=private)
    expected = {"domains": "ingress_current_target_required", "target": "ingress_current_target_required",
                "scope": "ingress_current_target_required", "settings": "ingress_current_settings_required",
                "host": "ingress_current_host_required", "ambiguous": "invalid_request",
                "unavailable": "authorization_denied", "drift": "ingress_route_drift",
                "audit": "ingress_current_observation_required"}
    assert code != 0 and output["summary"]["error_code"] == expected[cause]
    assert sum("ingress" not in call.kwargs["body"] for call in post.call_args_list) == 1
    if cause == "drift":
        assert output["result"]["status"] == "planned" and output["result"]["verification_usable"] is False
        assert output["records"]["ingress_route_audit_record_id"] == posts[1]["records"]["ingress_route_audit_record_id"]


@pytest.mark.parametrize("flag,valid", [(False, True), ("true", False), (1, False), (None, False)])
def test_verification_false_is_omitted_and_non_boolean_is_refused(flag: object, valid: bool) -> None:
    private = {**ingress_input(), "verify_route_binding": flag}
    code, _, _, post = run(INGRESS_COMMAND, ingress_reads(), [ingress_response()], private=private)
    if valid:
        assert code == 0 and "verify_route_binding" not in post.call_args.kwargs["body"]["ingress"]
    else:
        assert code != 0
        post.assert_not_called()


@pytest.mark.parametrize("missing", ["records", "id", "result"])
def test_malformed_planned_observation_keeps_the_json_failure_envelope(missing: str) -> None:
    posts = verification_posts()
    posts[1]["result"]["status"] = "planned"
    if missing == "records":
        posts[1].pop("records")
    elif missing == "id":
        posts[1]["records"]["ingress_route_audit_record_id"] = "invalid id"
    else:
        posts[1]["result"] = []
    code, output, _, _ = run(INGRESS_COMMAND, verification_reads(), posts,
                            private={**ingress_input(), "verify_route_binding": True})
    assert code != 0 and output["status"] == "invalid"
    assert output["warnings"][0]["code"] != "ingress_route_drift"


def test_known_full_settings_skip_the_discovery_comparison() -> None:
    private: dict[str, Any] = {**ingress_input(), "verify_route_binding": True}
    host = observed_host_response()["result"]["proxy_host"]
    private["route"].update({key: host[key] for key in h.INGRESS_PLAN_ROUTE_FIELDS - {"edge_endpoint_key"}})
    code, output, _, post = run(INGRESS_COMMAND, verification_reads(), [observed_host_response()], private=private)
    assert code == 0 and output["result"]["verification_usable"] is True
    assert post.call_count == 1


@pytest.mark.parametrize("cause", ["scope", "path"])
def test_continuation_input_is_validated_before_service_calls(cause: str) -> None:
    private = ingress_input()
    if cause == "scope":
        private["product"] = "foreign"
        extra = ()
    else:
        extra = ("--ingress-payload-file", "/missing/fictional-ingress.json")
    code, output, get, post = run("route-binding-dry-run", [], private=private, extra=extra)
    assert code != 0
    assert output["warnings"][0]["code"] == ("binding_scope_mismatch" if cause == "scope" else "invalid_payload_path")
    get.assert_not_called()
    post.assert_not_called()


def test_commit_time_expiry_returns_the_supported_review_continuation() -> None:
    code, output, _, post = run("route-binding-apply", [profiles(), *absent()],
        [binding_response(), http_error(409, "ingress_verification_required")], binding_plan())
    assert code != 0 and post.call_count == 2
    assert "--ingress-payload-file" in output["summary"]["recommendation"]


def ingress_apply_response():
    response = ingress_response()
    response["result"]["dry_run"] = False
    return response


def audit_readback(mode: str = "apply") -> dict[str, Any]:
    response = ingress_response()
    private = ingress_input()
    return {"status": "ok", "trace_id": "launchplane_req_read", "record": {
        "record_id": response["records"]["ingress_route_audit_record_id"],
        "product": "shop", "context": "shop", "mode": mode, "dry_run": mode == "dry-run",
        "status": "unchanged", "edge_endpoint_key": private["route"]["edge_endpoint_key"],
        "expected_host_id": private["expected_host_id"], "provider_host_id": private["expected_host_id"],
        "requested_domains": DOMAINS, "tls_owner": "provider", "provider_certificate_ref": "8",
        "operations": response["result"]["operations"], "trace_id": response["trace_id"],
        "idempotency_key": "record-repair-example"}}


@pytest.mark.parametrize("profile_host", [DOMAINS[0], "business.example.invalid"])
def test_ingress_apply_keeps_every_provider_mutation_disabled_and_reads_audit(profile_host: str) -> None:
    reads = ingress_reads()
    reads[0]["profile"]["lanes"][0]["base_url"] = f"https://{profile_host}"
    reads[0]["profile"]["lanes"][0]["health_url"] = f"https://{profile_host}/health"
    evidence = run(INGRESS_COMMAND, reads, [ingress_response()])[1]
    code, output, _, post = run("ingress-route-authority-apply", [*reads, audit_readback()],
        [ingress_response(), ingress_apply_response()], evidence)
    assert code == 0 and output["result"]["read_back_matches"] is True
    ingress = post.call_args.kwargs["body"]["ingress"]
    assert ingress["mode"] == "apply" and ingress["require_exact_expected_host_domains"] is True
    assert all(ingress[key] is False for key in ("allow_create", "allow_update", "allow_enable_disable"))


@pytest.mark.parametrize("field,value", [("mode", "dry-run"), ("tls_owner", "unknown"),
    ("provider_certificate_ref", "9"), ("edge_endpoint_key", "other"), ("idempotency_key", "other")])
def test_ingress_requires_exact_applied_audit(field, value):
    audit = audit_readback()
    audit["record"][field] = value
    code, output, _, _ = run("ingress-route-authority-apply", [*ingress_reads(), audit],
        [ingress_response(), ingress_apply_response()], ingress_plan())
    assert code != 0 and output["status"] == "accepted_unverified"


def test_ingress_endpoint_drift_stops_before_apply():
    reads = ingress_reads()
    reads[1]["record"]["upstream_host"] = "192.0.2.20"
    code, output, _, post = run("ingress-route-authority-apply", reads, [ingress_response()], ingress_plan())
    assert code != 0 and output["status"] == "stale" and post.call_count == 1


def binding_plan():
    return run("route-binding-dry-run", [profiles(), *absent()], [binding_response()])[1]


def test_binding_absent_expectation_and_record_only_apply():
    plan = binding_plan()
    readback = {"status": "ok", "trace_id": "launchplane_req_read", "record": binding_record()}
    code, output, _, post = run("route-binding-apply", [profiles(), *absent(), readback],
        [binding_response(), binding_response("apply")], plan)
    assert code == 0 and output["result"]["read_back_matches"] is True
    assert post.call_args.kwargs["body"]["expected_current"] == {"state": "absent"}
    assert not {"domains", "target_id", "edge_endpoint_key"} & post.call_args.kwargs["body"].keys()


def test_binding_present_uses_service_digest():
    current = {"status": "ok", "trace_id": "launchplane_req_current", "record": binding_record()}
    response = binding_response(current="a" * 64)
    code, _, _, post = run("route-binding-dry-run", [profiles(), current], [response])
    assert code == 0
    assert post.call_args.kwargs["body"]["expected_current"] == {"state": "present", "record_sha256": current["record"]["record_sha256"]}


@pytest.mark.parametrize("cause", ["source", "domains", "scope", "blocked"])
def test_binding_review_drift_never_applies(cause):
    response = binding_response()
    record = response["result"]["record"]
    if cause == "source":
        record["source"]["source_versions"]["target:shop:prod"] = "2026-10-14T21:30:00Z"
    elif cause == "domains":
        record["domains"].append({"domain_name": "alias.example.invalid", "role": "alias"})
    elif cause == "scope":
        record["product"] = "foreign"
    else:
        response["result"]["status"] = "blocked"
    code, _, _, post = run("route-binding-apply", [profiles(), *absent()], [response], binding_plan())
    assert code != 0 and post.call_count == 1


def test_binding_time_only_candidate_movement_retains_same_review():
    fresh = binding_response()
    fresh["result"]["record"]["source"]["refreshed_at"] = "2026-10-14T22:05:00Z"
    fresh["result"]["record"]["updated_at"] = "2026-10-14T22:05:00Z"
    code, output, _, _ = run("route-binding-dry-run", [profiles(), *absent()], [fresh])
    assert code == 0 and output["result"]["plan_sha256"] == binding_plan()["result"]["plan_sha256"]


@pytest.mark.parametrize("error", [http_error(403, "authorization_denied"), TimeoutError()])
def test_binding_unavailable_current_is_never_absent(error):
    code, _, _, post = run("route-binding-dry-run", [profiles(), error], [])
    assert code != 0 and post.call_count == 0


def test_discovery_verifies_service_owned_tuple_without_post():
    code, output, _, post = run("route-authority-discover", [profiles(), *absent()], [])
    assert code == 0 and output["result"]["lane_verified"] is True and post.call_count == 0
    assert "error_code" not in output["summary"]


def test_generic_missing_routes_cannot_establish_absence():
    code, output, _, post = run("route-binding-dry-run", [profiles(), http_error(), http_error()])
    assert code != 0 and post.call_count == 0
    assert output["summary"]["trace_id"] == "launchplane_req_failure"


def test_ingress_applied_audit_read_view_is_sufficient_after_full_apply_response():
    audit = audit_readback()
    audit["record"].update(expected_host_id=None, provider_host_id=None, provider_certificate_ref="")
    for operation in audit["record"]["operations"]:
        operation["host_id"] = None
    code, output, _, _ = run("ingress-route-authority-apply", [*ingress_reads(), audit],
        [ingress_response(), ingress_apply_response()], ingress_plan())
    assert code == 0 and output["result"]["read_back_view"] == "audit_read_view"
    assert output["records"]["ingress_route_audit_record_id"] == audit["record"]["record_id"]


def test_ingress_replay_retains_original_audit_trace_and_typed_count():
    response = ingress_apply_response()
    response.update(replayed=True, original_trace_id=response["trace_id"], trace_id="launchplane_req_replay")
    response["records"] = {key: str(value) for key, value in response["records"].items()}
    code, output, _, _ = run("ingress-route-authority-apply", [*ingress_reads(), audit_readback()],
        [ingress_response(), response], ingress_plan())
    assert code == 0 and output["summary"]["original_trace_id"] == response["original_trace_id"]


def test_unreadable_apply_response_preserves_available_http_diagnostics():
    failure = h.ServiceResponseError({"http_status": 202, "trace_id": "launchplane_req_broken"})
    code, output, _, _ = run("dokploy-target-repair-domain-authority-apply", [inspection()],
        [domain_response(), failure], domain_plan())
    assert code != 0 and output["status"] == "accepted_unverified"
    assert output["summary"]["http_status"] == 202
    assert output["summary"]["trace_id"] == "launchplane_req_broken"


@pytest.mark.parametrize("status", ["blocked", "conflict"])
def test_apply_time_binding_refusal_proves_nothing_written(status):
    code, output, _, post = run("route-binding-apply", [profiles(), *absent()],
        [binding_response(), binding_response("apply", status=status)], binding_plan())
    assert code != 0 and output["status"] == "stale" and post.call_count == 2
    assert output["result"]["applied"] is False


@pytest.mark.parametrize("error_code", ["mutation_in_progress", "mutation_reconciliation_required"])
def test_unresolved_mutation_is_not_a_harmless_conflict(error_code):
    code, output, _, post = run("route-binding-apply", [profiles(), *absent()],
        [binding_response(), http_error(409, error_code)], binding_plan())
    assert code != 0 and output["status"] == "outcome_unknown" and post.call_count == 2
    assert output["summary"]["error_code"] == error_code


@pytest.mark.parametrize("error", [http_error(403, "authorization_denied"),
    h.LaunchplaneSafetyError("unsafe_redirect"), OSError("PRIVATE_SENTINEL")])
def test_failed_audit_read_retains_accepted_trace_and_audit_id(error):
    code, output, _, post = run("ingress-route-authority-apply", [*ingress_reads(), error],
        [ingress_response(), ingress_apply_response()], ingress_plan())
    assert code != 0 and output["status"] == "accepted_unverified" and post.call_count == 2
    assert output["summary"]["trace_id"] == ingress_apply_response()["trace_id"]
    assert output["records"]["ingress_route_audit_record_id"] == ingress_apply_response()["records"]["ingress_route_audit_record_id"]


def test_malformed_applied_ingress_records_still_emit_bounded_json():
    response = ingress_apply_response()
    response["records"] = ["PRIVATE_SENTINEL"]
    code, output, _, post = run("ingress-route-authority-apply", ingress_reads(),
        [ingress_response(), response], ingress_plan())
    assert code != 0 and output["status"] == "accepted_unverified" and post.call_count == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
