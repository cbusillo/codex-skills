#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1"]
# ///
"""Offline transport, identity, no-op and redaction regressions for planners."""

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

from test_launchplane_helpers import contract, write_action as helper


DOMAIN_COMMAND = "dokploy-target-repair-domain-authority-dry-run"
INGRESS_COMMAND = "ingress-route-dry-run"
DOMAINS = ["shop.example.invalid"]
TARGET = "private-target-1"
HOST_ID = 37
EXPECTED = {"provider_id": "dokploy", "target_category": "application", "target_id": TARGET,
            "display_name": "private-display", "provider_target_type": "application",
            "provider_evidence": {}}


def inspection() -> dict[str, Any]:
    return {"status": "ok", "trace_id": "launchplane_req_inspect", "inspect": {
        "status": "ok", "target_type": "application", "target_id": TARGET,
        "tracked_target": {"context": "shop", "instance": "prod", "target_id": TARGET,
                           "target_type": "application", "domains": ["old.example.invalid"]},
        "provider_target_record": {"status": "present", **EXPECTED},
        "provider": {"id": TARGET, "target_id": TARGET, "target_type": "application",
                     "domains": [{"host": host} for host in DOMAINS]},
    }}


def domain_response():
    return {"status": "accepted", "trace_id": "launchplane_req_plan", "records": {}, "result": {
        "mode": "dry-run", "operation": "repair-domain-authority", "context": "shop",
        "instance": "prod", "applied": False, "route_domain_ids": [], "reason": "private reason", "setup": {
            "applied": False, "requested_domains": DOMAINS, "live_provider_domains": DOMAINS,
            "target_id_record": {"target_id": TARGET}, "provider_target_record": EXPECTED,
            "target_record": {"target_type": "application", "domains": DOMAINS,
                              "env_keys": ["PRIVATE_CONFIGURATION"]},
            "warnings": ["private provider prose"],
        },
    }}


def ingress_input():
    return {"product": "shop", "context": "shop", "instance": "prod", "reason": "private reason",
            "expected_host_id": HOST_ID,
            "route": {"domain_names": DOMAINS, "edge_endpoint_key": "edge-shop", "certificate_id": 8}}


def ingress_reads() -> list[dict[str, Any]]:
    return [{"status": "ok", "profile": {"product": "shop", "lanes": [{
        "context": "shop", "instance": "prod", "base_url": "https://shop.example.invalid",
        "health_url": "https://shop.example.invalid/health", "health_monitoring": {"checks": []},
    }]}}, {"status": "ok", "record": {"status": "active", "endpoint_key": "edge-shop",
                                          "upstream_host": "192.0.2.10"}}]


def ingress_response():
    return {"status": "accepted", "trace_id": "launchplane_req_plan", "records": {
        "ingress_provider": "npmplus", "ingress_provider_domain_comparison": "requested_domains",
        "ingress_provider_domain_count": len(DOMAINS), "ingress_route_audit_record_id": "audit-example",
    }, "result": {"status": "unchanged", "dry_run": True, "operations": [{
        "action": "no-op", "host_id": HOST_ID, "domain_names": DOMAINS,
        "requires_apply": False, "change_categories": [],
    }], "proxy_host": {"id": HOST_ID, "domain_names": DOMAINS, "certificate_id": 8, "forward_host": "192.0.2.10",
                       "advanced_config": "private provider prose", "meta": {"token": "PRIVATE_SENTINEL"}}}}


def invoke(command, *, reads=None, response=None, private=None, post_error=None):
    with TemporaryDirectory() as directory:
        path = Path(directory) / "private.json"
        path.write_text(json.dumps(private or ingress_input()))
        argv = ([DOMAIN_COMMAND, "--context", "shop", "--instance", "prod", "--reason", "private reason"]
                if command == DOMAIN_COMMAND else [INGRESS_COMMAND, "--payload-file", str(path)])
        stdout = io.StringIO()
        with patch.object(helper, "prepare_operator_settings", return_value={"service_url": "https://example.invalid"}), \
             patch.object(helper, "request_launchplane_read", side_effect=reads or [inspection()]) as get, \
             patch.object(helper, "request_launchplane", return_value=response or domain_response(), side_effect=post_error) as post, \
             redirect_stdout(stdout):
            code = helper.main(argv)
        return code, json.loads(stdout.getvalue()), get, post


def assert_private_dropped(payload):
    rendered = json.dumps(payload)
    for private in (*DOMAINS, TARGET, "192.0.2.10", "private-display", "private reason",
                    "PRIVATE_CONFIGURATION", "PRIVATE_SENTINEL", "private provider prose", "edge-shop"):
        assert private not in rendered


def test_domain_plan_uses_fresh_identity_domains_and_contract_route():
    code, payload, get, post = invoke(DOMAIN_COMMAND)
    assert code == 0
    assert get.call_args.kwargs["path"] == contract.internal_helper_path("dokploy-target-inspect")
    call = post.call_args.kwargs
    assert call["path"] == contract.helper_command_path(DOMAIN_COMMAND)
    assert call["body"]["mode"] == "dry-run"
    assert call["body"]["operation"] == "repair-domain-authority"
    assert call["body"]["domains"] == DOMAINS
    assert call["body"]["expected_current_provider_target"] == EXPECTED
    assert call["body"]["target_id"] == TARGET
    assert payload["result"]["applied"] is False
    assert payload["result"]["exact_domains_verified"] is True
    assert_private_dropped(payload)


@pytest.mark.parametrize("source,field,value", [
    ("provider", "target_id", "different"), ("provider_target_record", "status", "missing"),
    ("provider", "id", "different"), ("provider", "id", None),
    ("tracked_target", "instance", "testing"), ("provider", "domains", []),
])
def test_domain_bad_inspection_never_posts(source, field, value):
    observed = inspection()
    observed["inspect"][source][field] = value
    code, payload, _, post = invoke(DOMAIN_COMMAND, reads=[observed])
    assert code != 0
    post.assert_not_called()
    assert payload["summary"]["trace_id"] == observed["trace_id"]
    assert_private_dropped(payload)


def test_ingress_plan_is_exact_noop_with_all_mutations_disabled():
    code, payload, get, post = invoke(INGRESS_COMMAND, reads=ingress_reads(), response=ingress_response())
    assert code == 0
    assert get.call_count == 2
    call = post.call_args.kwargs
    assert call["path"] == contract.helper_command_path(INGRESS_COMMAND)
    assert call["body"]["instance"] == "prod"
    ingress = call["body"]["ingress"]
    assert ingress["mode"] == "dry-run"
    assert ingress["require_exact_expected_host_domains"] is True
    assert all(ingress[key] is False for key in ("allow_create", "allow_update", "allow_enable_disable"))
    assert ingress["expected_host_id"] == HOST_ID
    assert payload["result"]["requires_apply"] is False
    assert payload["records"]["ingress_route_audit_record_id"] == "audit-example"
    assert_private_dropped(payload)


@pytest.mark.parametrize("tracked_instance,tracked_domains,accepted", [
    ("prod", [*DOMAINS, "business.example.invalid"], True),
    ("testing", DOMAINS, False),
    ("prod", ["unrelated.example.invalid"], False),
])
def test_exact_alias_audit_uses_current_tracked_lane_hosts(tracked_instance: str, tracked_domains: list[str], accepted: bool) -> None:
    profile, endpoint = ingress_reads()
    lane = profile["profile"]["lanes"][0]
    lane.update(base_url="https://business.example.invalid", health_url="https://business.example.invalid/health")
    target = inspection()
    target["inspect"]["tracked_target"].update(instance=tracked_instance, domains=tracked_domains)
    code, payload, _, post = invoke(INGRESS_COMMAND, reads=[profile, target, endpoint], response=ingress_response())
    assert (code == 0) is accepted
    if accepted:
        assert post.call_args.kwargs["body"]["ingress"]["route"]["domain_names"] == DOMAINS
        assert payload["result"]["requires_apply"] is False
    else:
        post.assert_not_called()
    assert_private_dropped(payload)


@pytest.mark.parametrize("key,value", [("mode", "apply"), ("allow_update", True), ("confirmation", "yes")])
def test_ingress_input_cannot_select_apply_or_options(key, value):
    private = ingress_input()
    private[key] = value
    code, _, get, post = invoke(INGRESS_COMMAND, private=private)
    assert code != 0
    get.assert_not_called()
    post.assert_not_called()


def test_ingress_foreign_lane_and_inactive_endpoint_never_post():
    reads = ingress_reads()
    reads[0]["profile"]["lanes"][0]["base_url"] = "https://foreign.example.invalid"
    code, _, _, post = invoke(INGRESS_COMMAND, reads=reads)
    assert code != 0
    post.assert_not_called()
    reads = ingress_reads()
    reads[1]["record"]["status"] = "disabled"
    code, _, _, post = invoke(INGRESS_COMMAND, reads=reads)
    assert code != 0
    post.assert_not_called()


@pytest.mark.parametrize("change", ["apply", "change", "host", "domains", "shared_host", "unknown"])
def test_ingress_bad_plan_retains_trace_without_private_fields(change):
    response = ingress_response()
    if change == "apply":
        response["result"]["dry_run"] = False
    elif change == "change":
        response["result"]["operations"][0]["requires_apply"] = True
    elif change == "host":
        response["result"]["proxy_host"]["id"] += 1
    elif change == "domains":
        response["result"]["proxy_host"]["domain_names"] = ["other.example.invalid"]
    elif change == "shared_host":
        response["records"]["ingress_provider_domain_comparison"] = "full_expected_host"
        response["records"]["ingress_provider_domain_count"] += 1
    else:
        response["result"]["unknown"] = "PRIVATE_SENTINEL"
    code, payload, _, _ = invoke(INGRESS_COMMAND, reads=ingress_reads(), response=response)
    assert code != 0
    assert payload["summary"]["trace_id"] == response["trace_id"]
    assert_private_dropped(payload)


@pytest.mark.parametrize("command", [DOMAIN_COMMAND, INGRESS_COMMAND])
def test_denial_retains_service_code_trace_and_identity(command):
    error = urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", {}, io.BytesIO(json.dumps({
        "trace_id": "launchplane_req_denied", "error": {"code": "authorization_denied", "message": "PRIVATE_SENTINEL"},
    }).encode()))
    code, payload, _, post = invoke(command, reads=ingress_reads() if command == INGRESS_COMMAND else None,
                                    post_error=error)
    assert code != 0 and post.call_count == 1
    assert payload["status"] == "denied"
    assert payload["summary"]["error_code"] == "authorization_denied"
    assert payload["summary"]["trace_id"] == "launchplane_req_denied"
    assert_private_dropped(payload)


def test_conformance_refuses_mutating_planner_binding():
    routes = copy.deepcopy(contract.LOCAL_EXTENSION_ROUTES)
    routes[INGRESS_COMMAND]["mode"] = "apply"
    with patch.object(contract, "LOCAL_EXTENSION_ROUTES", routes), pytest.raises(contract.ContractError):
        contract.validate_local_bindings()


@pytest.mark.parametrize("change", ["apply", "target", "provider", "domains", "unknown"])
def test_domain_bad_plan_is_not_accepted(change):
    response = domain_response()
    setup = response["result"]["setup"]
    if change == "apply":
        setup["applied"] = True
    elif change == "target":
        setup["target_id_record"]["target_id"] = "other"
    elif change == "provider":
        setup["provider_target_record"] = {**EXPECTED, "target_id": "other"}
    elif change == "domains":
        setup["live_provider_domains"] = ["other.example.invalid"]
    else:
        response["result"]["unknown"] = "PRIVATE_SENTINEL"
    code, payload, _, _ = invoke(DOMAIN_COMMAND, response=response)
    assert code != 0
    assert payload["summary"]["trace_id"] == response["trace_id"]
    assert_private_dropped(payload)


@pytest.mark.parametrize("command", [DOMAIN_COMMAND, INGRESS_COMMAND])
def test_transport_failure_never_retries(command):
    code, payload, _, post = invoke(command, reads=ingress_reads() if command == INGRESS_COMMAND else None,
                                    post_error=TimeoutError("PRIVATE_SENTINEL"))
    assert code != 0 and post.call_count == 1
    assert payload["status"] == "unavailable"
    assert_private_dropped(payload)


def test_ingress_private_input_rejects_upstream_and_malformed_scope():
    private = ingress_input()
    private["route"]["forward_host"] = "192.0.2.10"
    code, _, get, post = invoke(INGRESS_COMMAND, private=private)
    assert code != 0
    get.assert_not_called()
    post.assert_not_called()


def test_ingress_product_cannot_change_read_route():
    private = ingress_input()
    private["product"] = "shop/../../service"
    code, payload, get, post = invoke(INGRESS_COMMAND, private=private)
    assert code != 0
    get.assert_not_called()
    post.assert_not_called()
    assert private["product"] not in json.dumps(payload)
    assert payload["request"] == {}


def test_domain_inspect_observed_identity_and_normalized_hosts():
    observed = inspection()
    del observed["inspect"]["provider"]["target_id"]
    observed["inspect"]["provider"]["domains"] = [{"host": DOMAINS[0].upper() + ". "}]
    code, _, _, post = invoke(DOMAIN_COMMAND, reads=[observed])
    assert code == 0
    assert post.call_args.kwargs["body"]["domains"] == DOMAINS


@pytest.mark.parametrize("command", [DOMAIN_COMMAND, INGRESS_COMMAND])
@pytest.mark.parametrize("error", [helper.LaunchplaneSafetyError("unsafe_redirect"), ValueError("invalid_response")])
def test_post_failure_never_reuses_read_trace(command, error):
    reads = ingress_reads() if command == INGRESS_COMMAND else [inspection()]
    for read in reads:
        read["trace_id"] = "launchplane_req_read"
    code, payload, _, post = invoke(command, reads=reads, post_error=error)
    assert code != 0 and post.call_count == 1
    assert payload["request"]["planner_request_sent"] is True
    assert "launchplane_req_read" not in json.dumps(payload)
    if isinstance(error, helper.LaunchplaneSafetyError):
        assert payload["status"] == "unavailable"
        assert payload["warnings"][0]["code"] == error.code


@pytest.mark.parametrize("cause,expected", [
    ("lane", "plan_lane_not_unique"), ("domains", "plan_domain_scope_mismatch"),
    ("endpoint", "plan_endpoint_not_active"),
])
def test_prepost_refusal_keeps_specific_code(cause, expected):
    reads = ingress_reads()
    if cause == "lane":
        reads[0]["profile"]["lanes"] = []
    elif cause == "domains":
        reads[0]["profile"]["lanes"][0]["base_url"] = "https://foreign.example.invalid"
        reads.insert(1, inspection())
    else:
        reads[1]["record"]["status"] = "disabled"
    code, payload, _, post = invoke(INGRESS_COMMAND, reads=reads)
    assert code != 0
    post.assert_not_called()
    assert payload["summary"]["error_code"] == expected
    assert payload["request"]["planner_request_sent"] is False


@pytest.mark.parametrize("command", [DOMAIN_COMMAND, INGRESS_COMMAND])
def test_standard_success_metadata_and_unknown_records(command):
    response = ingress_response() if command == INGRESS_COMMAND else domain_response()
    response.update(schema_version=1, replayed=False, original_trace_id="launchplane_req_original")
    reads = ingress_reads() if command == INGRESS_COMMAND else None
    code, _, _, _ = invoke(command, reads=reads, response=response)
    assert code == 0
    response["records"]["unknown"] = "PRIVATE_SENTINEL"
    code, payload, _, _ = invoke(command, reads=ingress_reads() if command == INGRESS_COMMAND else None,
                                response=response)
    assert code != 0
    assert_private_dropped(payload)


def test_ingress_missing_scope_never_reads_or_posts():
    private = ingress_input()
    private["product"] = None
    code, _, get, post = invoke(INGRESS_COMMAND, private=private)
    assert code != 0
    get.assert_not_called()
    post.assert_not_called()


@pytest.mark.parametrize("error", [helper.LaunchplaneSafetyError("unsafe_redirect"), ValueError("invalid_response")])
def test_second_read_failure_never_reuses_first_trace(error):
    first = ingress_reads()[0]
    first["trace_id"] = "launchplane_req_profile"
    code, payload, get, post = invoke(INGRESS_COMMAND, reads=[first, error])
    assert code != 0 and get.call_count == 2
    post.assert_not_called()
    assert "launchplane_req_profile" not in json.dumps(payload)
    assert payload["request"]["planner_request_sent"] is False


FINGERPRINT = "synthetic-private-comparison-fingerprint"


def fingerprint_fixture():
    observed, response = inspection(), copy.deepcopy(domain_response())
    for provider in (observed["inspect"]["provider_target_record"],
                     response["result"]["setup"]["provider_target_record"]):
        provider.pop("provider_evidence", None)
        provider["comparison_fingerprint"] = FINGERPRINT
    return observed, response


def test_domain_fingerprint_stays_private_and_is_forwarded_and_verified():
    observed, response = fingerprint_fixture()
    code, payload, _, post = invoke(DOMAIN_COMMAND, reads=[observed], response=response)
    assert code == 0
    assert post.call_args.kwargs["body"]["expected_current_provider_target_fingerprint"] == FINGERPRINT
    assert payload["result"]["apply_eligible"] is False
    assert FINGERPRINT not in json.dumps(payload)
    assert_private_dropped(payload)


@pytest.mark.parametrize("fingerprint", [None, 37, "", "x" * 1024])
def test_bad_fingerprint_never_posts(fingerprint):
    observed, response = fingerprint_fixture()
    observed["inspect"]["provider_target_record"]["comparison_fingerprint"] = fingerprint
    code, payload, _, post = invoke(DOMAIN_COMMAND, reads=[observed], response=response)
    assert code != 0
    post.assert_not_called()
    assert_private_dropped(payload)


@pytest.mark.parametrize("change", ["missing", "tampered", "identity", "evidence"])
def test_fingerprint_plan_rejects_mismatch_without_publication(change):
    observed, response = fingerprint_fixture()
    provider = response["result"]["setup"]["provider_target_record"]
    if change == "missing":
        provider.pop("comparison_fingerprint")
    elif change == "tampered":
        provider["comparison_fingerprint"] = FINGERPRINT[:-1] + "d"
    elif change == "identity":
        provider["target_id"] = "other-target"
    else:
        provider["provider_evidence"] = {"private_payload": "PRIVATE_SENTINEL"}
    code, payload, _, _ = invoke(DOMAIN_COMMAND, reads=[observed], response=response)
    assert code != 0
    assert payload["summary"]["trace_id"] == response["trace_id"]
    assert FINGERPRINT not in json.dumps(payload)
    assert_private_dropped(payload)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
