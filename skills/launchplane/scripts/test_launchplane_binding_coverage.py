#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1"]
# ///

import copy
import io
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from test_launchplane_helpers import (
    _run_product_read,
    _secret_bindings_response,
    temporary_attribute,
    write_action,
)

ARGS = ["product-secret-bindings-read", "--product", "example-product"]


def coverage(*, source: str = "complete", authorization: str = "complete") -> dict[str, object]:
    return {
        "population": "configured_runtime_copy_metadata_in_exclusive_declared_product_contexts",
        "status": authorization if source == "complete" else source,
        "source": source,
        "authorization": authorization,
        "exclusions": ["global_secrets", "historical_and_undeclared_lanes", "ambiguous_contexts", "other_integrations"],
    }


def response() -> dict[str, Any]:
    binding = dict(_secret_bindings_response()["bindings"][0])
    binding.pop("provider_note")
    return {"status": "ok", "product": "example-product", "bindings": [binding], "coverage": coverage()}


def resolution() -> dict[str, Any]:
    lane_coverage = {
        **coverage(),
        "population": "configured_binding_selection_for_requested_integration_and_scopes",
        "exclusions": ["runtime_delivery_and_value_validation"],
    }
    return {
        "integration": "runtime_environment",
        "selection_scopes": ["global", "context", "context_instance"],
        "lanes": [{"context": "example-product", "instance": "prod", "bindings": response()["bindings"], "coverage": lane_coverage}],
        "coverage": {
            **coverage(),
            "population": "declared_product_lanes_for_requested_integration_and_scopes",
            "exclusions": [
                "managed_preview_configuration_scope",
                "historical_and_undeclared_lanes",
                "other_integrations",
                "runtime_delivery",
            ],
        },
    }


@pytest.mark.parametrize(
    "source,authorization",
    [
        ("complete", "complete"),
        ("complete", "scoped"),
        ("complete", "unknown"),
        ("limited", "complete"),
        ("unknown", "complete"),
        ("unsupported", "scoped"),
    ],
)
def test_service_coverage_is_separate_from_projection(source: str, authorization: str) -> None:
    body = response()
    body["coverage"] = coverage(source=source, authorization=authorization)
    if authorization != "complete":
        body["bindings"] = []
    status, payload, _ = _run_product_read(ARGS, body)
    assert status == 0, payload
    assert payload["result"]["coverage"] == body["coverage"]
    assert payload["result"]["projection_coverage"] == "complete"


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        {},
        {"status": "complete"},
        {**coverage(), "source": "partial"},
        {**coverage(), "status": "complete", "authorization": "scoped"},
        {**coverage(), "exclusions": "global_secrets"},
    ],
)
def test_missing_or_malformed_coverage_stays_unknown(invalid: object) -> None:
    body = response()
    body["coverage"] = invalid
    status, payload, _ = _run_product_read(ARGS, body)
    assert status == 0, payload
    assert payload["result"]["coverage"]["status"] == "unknown"
    assert payload["result"]["projection_coverage"] == "unknown"
    body.pop("coverage")
    status, payload, _ = _run_product_read(ARGS, body)
    assert status == 0, payload
    assert payload["result"]["coverage"]["status"] == "unknown"


@pytest.mark.parametrize("damage", ["cap", "identity", "required", "optional"])
def test_local_loss_limits_projection_without_rewriting_server_coverage(damage: str) -> None:
    body = response()
    if damage == "identity":
        body["bindings"][0]["binding_key"] = "bad key"
    elif damage == "required":
        body["bindings"][0].pop("name")
    elif damage == "optional":
        body["bindings"][0]["sharing_reason"] = "invalid"
    else:
        body["bindings"] *= 2
    with temporary_attribute(write_action, "PRODUCT_SECRET_BINDINGS_MAX", 1):
        status, payload, _ = _run_product_read(ARGS, body)
    assert status == 0, payload
    assert payload["result"]["coverage"] == body["coverage"]
    assert payload["result"]["projection_coverage"] == "limited"


def test_requested_resolution_preserves_selection_and_global_metadata() -> None:
    body = response()
    selected = resolution()
    global_binding = copy.deepcopy(body["bindings"][0])
    global_binding.update(scope="global", context="", instance="")
    selected["lanes"][0]["bindings"].append(global_binding)
    body["lane_binding_resolution"] = selected
    args = [
        *ARGS,
        "--include-resolution",
        "--integration",
        "runtime_environment",
        "--scope",
        "global",
        "--scope",
        "context_instance",
    ]
    status, payload, calls = _run_product_read(args, body)
    assert status == 0, payload
    projected = payload["result"]["lane_binding_resolution"]
    for key in ("integration", "selection_scopes", "coverage"):
        assert projected[key] == selected[key]
    lane = projected["lanes"][0]
    assert lane["bindings"] == selected["lanes"][0]["bindings"]
    assert lane["coverage"] == selected["lanes"][0]["coverage"]
    assert lane["projection_coverage"] == projected["projection_coverage"] == "complete"
    assert calls[0]["query"] == {
        "include_resolution": "true",
        "integration": "runtime_environment",
        "scopes": ["global", "context_instance"],
    }


def test_resolution_hidden_sources_and_local_caps_are_honest() -> None:
    body = response()
    selected = resolution()
    selected["coverage"] = {**selected["coverage"], "status": "scoped", "authorization": "scoped"}
    lane = selected["lanes"][0]
    lane["coverage"] = {**lane["coverage"], "status": "scoped", "authorization": "scoped"}
    lane["bindings"] = []
    body["lane_binding_resolution"] = selected
    status, payload, _ = _run_product_read([*ARGS, "--include-resolution"], body)
    assert status == 0, payload
    assert payload["result"]["lane_binding_resolution"]["lanes"][0]["bindings"] == []
    assert payload["result"]["lane_binding_resolution"]["coverage"]["status"] == "scoped"
    lane["bindings"] = response()["bindings"] * 2
    with temporary_attribute(write_action, "PRODUCT_SECRET_BINDINGS_MAX", 1):
        status, payload, _ = _run_product_read([*ARGS, "--include-resolution"], body)
    assert status == 0, payload
    projected = payload["result"]["lane_binding_resolution"]
    assert projected["lanes"][0]["projection_coverage"] == projected["projection_coverage"] == "limited"
    assert payload["result"]["projection_coverage"] == "limited"


def test_missing_requested_resolution_stays_unknown() -> None:
    status, payload, _ = _run_product_read([*ARGS, "--include-resolution"], response())
    assert status == 0, payload
    assert payload["result"]["coverage"] == response()["coverage"]
    assert payload["result"]["lane_binding_resolution"] is None
    assert payload["result"]["projection_coverage"] == "unknown"


def test_scope_query_is_repeated_on_the_existing_get_transport() -> None:
    calls = []

    def fake_open(request: Any, *, timeout: float) -> io.BytesIO:
        calls.append(request)
        return io.BytesIO(b'{"status":"ok"}')

    query = {"include_resolution": "true", "scopes": ["global", "context"]}
    with temporary_attribute(write_action, "safe_urlopen", fake_open):
        write_action.request_launchplane_read(
            service_url="https://service.example.invalid",
            path="/v1/products/example-product/secret-bindings",
            settings={"token": "PRIVATE_SENTINEL"},
            query=query,
            timeout=3,
        )
    assert calls[0].method == "GET"
    assert parse_qs(urlsplit(calls[0].full_url).query) == {"include_resolution": ["true"], "scopes": query["scopes"]}


@pytest.mark.parametrize("damage", ["lane_cap", "invalid_lane", "missing_scope", "missing_lane_coverage", "malformed_resolution"])
def test_missing_selection_evidence_cannot_project_complete(damage: str) -> None:
    body = response()
    selected = resolution()
    if damage == "lane_cap":
        selected["lanes"] *= 2
    elif damage == "invalid_lane":
        selected["lanes"].append({"context": "unusable"})
    elif damage == "missing_scope":
        selected.pop("selection_scopes")
    elif damage == "missing_lane_coverage":
        selected["lanes"][0]["coverage"] = None
    else:
        selected = []
    body["lane_binding_resolution"] = selected
    with temporary_attribute(write_action, "PRODUCT_SECRET_BINDINGS_MAX", 1):
        status, payload, _ = _run_product_read([*ARGS, "--include-resolution"], body)
    assert status == 0, payload
    assert payload["result"]["projection_coverage"] in {"limited", "unknown"}


@pytest.mark.parametrize("location", ["coverage", "resolution", "lane", "binding"])
def test_sensitive_fields_anywhere_in_selection_fail_closed(location: str) -> None:
    body = response()
    body["lane_binding_resolution"] = resolution()
    targets = {
        "coverage": body["coverage"],
        "resolution": body["lane_binding_resolution"],
        "lane": body["lane_binding_resolution"]["lanes"][0],
        "binding": body["lane_binding_resolution"]["lanes"][0]["bindings"][0],
    }
    targets[location]["ciphertext"] = "PRIVATE_SENTINEL"
    status, payload, _ = _run_product_read([*ARGS, "--include-resolution"], body)
    assert status == 1
    assert "PRIVATE_SENTINEL" not in json.dumps(payload)


def test_private_coverage_sources_and_sensitive_rows_beyond_caps_are_never_emitted() -> None:
    body = response()
    body["coverage"]["hidden_sources"] = ["private-hidden-source"]
    body["coverage"]["hidden_count"] = 7
    status, payload, _ = _run_product_read(ARGS, body)
    assert status == 0, payload
    assert payload["result"]["coverage"]["status"] == "unknown"
    assert "private-hidden-source" not in json.dumps(payload)
    assert "hidden_count" not in json.dumps(payload)
    body = response()
    body["bindings"].append({"ciphertext": "PRIVATE_SENTINEL"})
    with temporary_attribute(write_action, "PRODUCT_SECRET_BINDINGS_MAX", 1):
        status, payload, _ = _run_product_read(ARGS, body)
    assert status == 1
    assert "PRIVATE_SENTINEL" not in json.dumps(payload)


@pytest.mark.parametrize("field,value", [("instance", None), ("context", None), ("sharing_reason", ""), ("secret_class", "")])
def test_malformed_selection_binding_cannot_project_complete(field: str, value: object) -> None:
    body = response()
    selected = resolution()
    binding = selected["lanes"][0]["bindings"][0]
    binding.update(scope="global", context="", instance="")
    binding[field] = value
    body["lane_binding_resolution"] = selected
    status, payload, _ = _run_product_read([*ARGS, "--include-resolution"], body)
    assert status == 0, payload
    assert payload["result"]["projection_coverage"] == "limited"


@pytest.mark.parametrize(
    "scan",
    [
        {"status": "pass", "reason": "no_high_or_critical_vulnerabilities", "high": 0, "critical": 0},
        {"status": "fail", "reason": "high_or_critical_vulnerabilities", "high": 4, "critical": 2},
        {"status": "unavailable", "reason": "scanner_busy", "high": 0, "critical": 0},
    ],
)
def test_reconcile_scan_evidence_keeps_only_public_fields(scan: dict[str, object]) -> None:
    scan = {**scan, "scanned_at": "2026-10-27T13:00:00Z"}
    body = {
        "product": "example-product",
        "requests": [
            {
                "target_key": "example-product:testing",
                "last_plan": {
                    "artifact_scan": {
                        **scan,
                        "image_reference": "registry.private.invalid/private-image",
                        "provider_data": {"private": "PRIVATE_SENTINEL"},
                    },
                },
            }
        ],
    }
    status, payload, _ = _run_product_read(["reconcile-requests-read", "--product", "example-product"], body)
    assert status == 0, payload
    assert payload["result"]["requests"][0]["last_plan"]["artifact_scan"] == scan
    assert "private-image" not in json.dumps(payload)
    assert "PRIVATE_SENTINEL" not in json.dumps(payload)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
