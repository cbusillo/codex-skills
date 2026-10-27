#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1"]
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
from unittest.mock import patch

import pytest

from test_launchplane_inert_planners import (
    DOMAINS, DOMAIN_COMMAND, INGRESS_COMMAND, assert_private_dropped,
    domain_response, ingress_input, ingress_reads, ingress_response, inspection, fingerprint_fixture,
)
from test_launchplane_helpers import write_action as h

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


def binding_response(mode="dry-run", status="ready", current=""):
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


def ingress_apply_response():
    response = ingress_response()
    response["result"]["dry_run"] = False
    return response


def audit_readback():
    response = ingress_apply_response()
    private = ingress_input()
    return {"status": "ok", "trace_id": "launchplane_req_read", "record": {
        "record_id": response["records"]["ingress_route_audit_record_id"],
        "product": "shop", "context": "shop", "mode": "apply", "dry_run": False,
        "status": "unchanged", "edge_endpoint_key": private["route"]["edge_endpoint_key"],
        "expected_host_id": private["expected_host_id"], "provider_host_id": private["expected_host_id"],
        "requested_domains": DOMAINS, "tls_owner": "provider", "provider_certificate_ref": "8",
        "operations": response["result"]["operations"], "trace_id": response["trace_id"],
        "idempotency_key": "record-repair-example"}}


def test_ingress_apply_keeps_every_provider_mutation_disabled_and_reads_audit():
    code, output, _, post = run("ingress-route-authority-apply", [*ingress_reads(), audit_readback()],
        [ingress_response(), ingress_apply_response()], ingress_plan())
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


@pytest.mark.parametrize("drift", [False, True])
def test_alias_scope_is_bound_to_review_and_preserves_noop_apply(drift: bool) -> None:
    profile, endpoint = ingress_reads()
    profile["profile"]["lanes"][0].update(base_url="https://business.example.invalid", health_url="https://business.example.invalid/health")
    tracked = inspection()
    tracked["inspect"]["tracked_target"]["domains"] = [*DOMAINS, "business.example.invalid"]
    reads = [profile, tracked, endpoint]
    code, evidence, _, _ = run(INGRESS_COMMAND, reads, [ingress_response()])
    assert code == 0
    if drift:
        tracked["inspect"]["tracked_target"]["target_id"] = "changed-target"
    code, output, _, post = run("ingress-route-authority-apply", [*reads, audit_readback()],
        [ingress_response(), ingress_apply_response()], evidence)
    if drift:
        assert code != 0 and output["status"] == "stale"
        assert all(call.kwargs["body"]["ingress"]["mode"] == "dry-run" for call in post.call_args_list)
    else:
        assert code == 0 and output["result"]["read_back_matches"] is True
        ingress = post.call_args.kwargs["body"]["ingress"]
        assert all(ingress[key] is False for key in ("allow_create", "allow_update", "allow_enable_disable"))


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
