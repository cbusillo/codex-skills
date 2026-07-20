#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused regression tests for Launchplane helper trust boundaries."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import subprocess
import sys
import types
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parent


def load_module(filename: str, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


safety = load_module("launchplane_safety.py", "launchplane_safety")
write_action = load_module("launchplane-write-action.py", "launchplane_write_action")
context_helper = load_module("launchplane-context.py", "launchplane_context")


def run_helper(script: str, args: list[str], env: dict[str, str] | None = None) -> dict[str, Any]:
    merged_env = {key: value for key, value in os.environ.items() if not key.startswith("LAUNCHPLANE_")}
    if env:
        merged_env.update(env)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / script), *args],
        check=False,
        capture_output=True,
        text=True,
        env=merged_env,
    )
    return {"returncode": proc.returncode, "payload": json.loads(proc.stdout)}


def assert_rejects_url(value: str, code: str) -> None:
    try:
        safety.validate_service_url(value)
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == code, exc.code
    else:
        raise AssertionError(f"expected {value!r} to be rejected")


def test_endpoint_validation_policy() -> None:
    assert safety.validate_service_url("https://launchplane.example.invalid/base/").url == "https://launchplane.example.invalid/base"
    assert safety.validate_service_url("http://127.0.0.1:8000").origin == ("http", "127.0.0.1", 8000)
    assert safety.validate_service_url("http://localhost:8000").origin == ("http", "localhost", 8000)
    assert_rejects_url("launchplane.example.invalid", "invalid_service_url_absolute")
    assert_rejects_url("https:///v1", "invalid_service_url_absolute")
    credentialed_url = "https://" + "user" + ":" + "pass" + "@launchplane.example.invalid"
    assert_rejects_url(credentialed_url, "invalid_service_url_userinfo")
    assert_rejects_url("ftp://launchplane.example.invalid", "invalid_service_url_scheme")
    assert_rejects_url("http://launchplane.example.invalid", "invalid_service_url_http")
    assert_rejects_url("https://launchplane.example.invalid?token=secret", "invalid_service_url_component")


def test_build_url_and_redirect_policy() -> None:
    assert safety.build_launchplane_url("https://launchplane.example.invalid/base/", "/v1/agent/context", query="repository=a%2Fb") == "https://launchplane.example.invalid/base/v1/agent/context?repository=a%2Fb"
    for bad_path in ("v1/agent/context", "https://other.example.invalid/v1", "//other.example.invalid/v1"):
        try:
            safety.build_launchplane_url("https://launchplane.example.invalid", bad_path)
        except safety.LaunchplaneSafetyError as exc:
            assert exc.code == "invalid_request_path"
        else:
            raise AssertionError(f"expected bad path {bad_path!r} to fail")
    request = urllib.request.Request("https://launchplane.example.invalid/v1/example")
    handler = safety.SameOriginRedirectHandler()
    assert handler.redirect_request(request, None, 302, "Found", {}, "/v1/other").full_url == "https://launchplane.example.invalid/v1/other"
    try:
        handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example.invalid/v1/steal")
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == "unsafe_redirect"
    else:
        raise AssertionError("expected cross-origin redirect to fail")


def test_write_helper_validates_cli_env_and_json_url_sources() -> None:
    token_env = {"LAUNCHPLANE_LOCAL_OPERATOR_TOKEN": "secret-token-never-render"}
    cli = run_helper(
        "launchplane-write-action.py",
        ["--url", "http://launchplane.example.invalid", "merge-train-controller-run-once", "--repo", "example/repo"],
        token_env,
    )
    assert cli["returncode"] == 2
    assert cli["payload"]["warnings"][0]["code"] == "invalid_service_url_http"
    env_source = run_helper(
        "launchplane-write-action.py",
        ["merge-train-controller-run-once", "--repo", "example/repo"],
        {**token_env, "LAUNCHPLANE_OPERATOR_URL": "https://user@launchplane.example.invalid"},
    )
    assert env_source["returncode"] == 2
    assert env_source["payload"]["warnings"][0]["code"] == "invalid_service_url_userinfo"
    args = argparse.Namespace(config="local.json", env_config=None, url=None)
    with patch.object(write_action, "load_config", return_value={"service_url": "ftp://launchplane.example.invalid"}):
        with patch.dict(write_action.os.environ, {"LAUNCHPLANE_LOCAL_OPERATOR_TOKEN": "secret"}, clear=True):
            diagnostic = write_action.settings_diagnostic(args)
    assert diagnostic["classification"] == "invalid_service_url_scheme"


def test_context_helper_validates_env_and_json_url_sources() -> None:
    env_source = run_helper(
        "launchplane-context.py",
        ["--repo", "example/repo"],
        {"LAUNCHPLANE_CONTEXT_URL": "https://user@launchplane.example.invalid", "LAUNCHPLANE_CONTEXT_TOKEN": "secret-token-never-render"},
    )
    assert env_source["returncode"] == 0
    assert env_source["payload"]["status"] == "invalid"
    assert env_source["payload"]["warnings"][0]["code"] == "invalid_service_url_userinfo"
    with patch.object(context_helper, "load_config", return_value={"service_url": "http://launchplane.example.invalid"}):
        with patch.dict(context_helper.os.environ, {"LAUNCHPLANE_CONTEXT_TOKEN": "secret"}, clear=True):
            settings = context_helper.resolve_settings(argparse.Namespace(config="local.json", url=None))
    try:
        safety.validate_service_url(settings["service_url"])
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == "invalid_service_url_http"
    else:
        raise AssertionError("expected JSON config URL source to fail")


def test_success_projection_preserves_contracts() -> None:
    merge = write_action.summarize_success(
        operation="merge-train-controller-run-once",
        request={"repository": "example/repo", "base_branch": "main", "mutate": False},
        provider_payload={
            "status": "accepted",
            "trace_id": "launchplane_req_example",
            "records": {"merge_train_batch_candidate_record_id": "candidate-example"},
            "result": {"repository": "example/repo", "base_branch": "main", "mode": "dry-run", "controller_action": "build_candidate"},
        },
    )
    assert merge["summary"]["trace_id"] == "launchplane_req_example"
    assert merge["summary"]["controller_action"] == "build_candidate"
    assert merge["records"] == {"merge_train_batch_candidate_record_id": "candidate-example"}
    product = write_action.summarize_success(
        operation="product-config-preflight",
        request={"product": "example-product", "context": "example-testing", "mode": "dry_run"},
        provider_payload={
            "status": "accepted",
            "trace_id": "launchplane_req_product",
            "records": {"intent_record_id": "intent-example"},
            "result": {
                "intent": {"status": "allowed", "reason_code": "policy_allowed", "safe_to_execute": True, "next_action": "Review managed binding evidence before apply."},
                "secret_binding_keys": ["EXAMPLE_API_TOKEN"],
                "runtime_key_safety_findings": [{"key": "EXAMPLE_API_TOKEN", "code": "managed_secret", "severity": "info"}],
            },
        },
    )
    assert product["summary"]["intent_status"] == "allowed"
    assert product["summary"]["safe_to_execute"] is True
    assert product["records"] == {"intent_record_id": "intent-example"}
    assert product["result"]["secret_binding_keys"] == ["EXAMPLE_API_TOKEN"]


def test_current_launchplane_service_response_shapes() -> None:
    merge = write_action.summarize_success(
        operation="merge-train-controller-run-once",
        request={"repository": "example/repo", "base_branch": "main", "mutate": False},
        provider_payload={
            "status": "accepted",
            "trace_id": "launchplane_req_merge",
            "replayed": True,
            "original_trace_id": "launchplane_req_original",
            "records": {
                "merge_train_batch_candidate_record_id": "candidate-example",
                "merge_train_batch_landing_plan_record_id": "landing-example",
            },
            "result": {
                "repository": "example/repo",
                "base_branch": "main",
                "mode": "build_candidate",
                "controller_action": "build_candidate",
                "candidate": {
                    "status": "ready_for_checks",
                    "candidate_sha": "abc123",
                    "entries": [{"pull_request_number": 42, "status": "pending"}],
                },
            },
        },
    )
    assert merge["records"]["merge_train_batch_landing_plan_record_id"] == "landing-example"
    assert merge["result"]["candidate"] == {
        "status": "ready_for_checks",
        "candidate_sha": "abc123",
        "entries_count": 1,
    }

    preflight = write_action.summarize_success(
        operation="product-config-preflight",
        request={"product": "example-product", "context": "testing"},
        provider_payload={
            "status": "accepted",
            "trace_id": "launchplane_req_intent",
            "records": {},
            "result": {
                "intent": {
                    "schema_version": 1,
                    "intent": "product_config_apply",
                    "mode": "dry_run",
                    "status": "allowed",
                    "authz_action": "product_config.apply",
                    "product": "example-product",
                    "context": "testing",
                    "source_url": "https://github.com/example/repo/issues/1",
                    "safe_to_execute": True,
                    "next_action": "Review the matching dry-run before apply.",
                    "reason_code": "authorized",
                    "audit": {
                        "decision": "allowed",
                        "reason_code": "authorized",
                        "subject": {"kind": "local_operator"},
                        "action": "product_config.apply",
                        "product": "example-product",
                        "context": "testing",
                        "policy_source": "managed",
                        "policy_sha256": "abc123",
                        "source_kind": "authz_policy",
                    },
                    "secret_evidence": {
                        "status": "not_required",
                        "destination": None,
                        "checked_binding_keys": [],
                        "policy_record_id": "",
                        "policy_sha256": "",
                        "findings": [],
                    },
                },
                "record": {
                    "record_id": "agent-write-intent-example",
                    "recorded_at": "2026-07-19T23:00:00Z",
                },
            },
        },
    )
    assert preflight["summary"]["intent_status"] == "allowed"
    assert preflight["result"]["record"]["record_id"] == "agent-write-intent-example"

    apply = write_action.summarize_success(
        operation="product-config-apply",
        request={"product": "example-product", "context": "testing"},
        provider_payload={
            "status": "accepted",
            "trace_id": "launchplane_req_apply",
            "records": {},
            "result": {
                "status": "ok",
                "mode": "apply",
                "product": "example-product",
                "context": "testing",
                "instance": "example-instance",
                "actor": "operator",
                "source_label": "product-config-api",
                "reason": "Apply reviewed configuration.",
                "runtime_environment": {
                    "action": "updated",
                    "scope": "instance",
                    "context": "testing",
                    "instance": "example-instance",
                    "keys": ["EXAMPLE_MODE"],
                    "changed_keys": ["EXAMPLE_MODE"],
                    "unchanged_keys": [],
                    "env_value_count_after": 1,
                    "record": None,
                },
                "runtime_key_safety": {
                    "required": True,
                    "status": "pass",
                    "policy_record_id": "policy-example",
                    "policy_sha256": "abc123",
                    "target": {
                        "context": "testing",
                        "instance": "example-instance",
                        "environment_class": "nonprod",
                    },
                    "checked_binding_keys": ["EXAMPLE_MODE"],
                    "findings": [],
                },
                "secrets": [
                    {
                        "action": "rotated",
                        "scope": "instance",
                        "integration": "example-provider",
                        "name": "example-secret",
                        "binding_key": "EXAMPLE_API_TOKEN",
                        "context": "testing",
                        "instance": "example-instance",
                        "secret_id": "secret-record-example",
                    }
                ],
                "summary": {
                    "runtime_changed_key_count": 1,
                    "secret_change_count": 1,
                },
                "next_actions": [],
            },
        },
    )
    assert apply["result"]["runtime_environment"]["changed_keys"] == ["EXAMPLE_MODE"]
    assert apply["result"]["secrets"] == [
        {
            "action": "rotated",
            "integration": "example-provider",
            "binding_key": "EXAMPLE_API_TOKEN",
        }
    ]
    assert "secret-record-example" not in json.dumps(apply)


def test_success_projection_fails_closed_on_secret_bearing_payloads() -> None:
    for key in ("secret", "client_secret", "api_key", "private_key", "credential", "cookie", "token", "opaque_value"):
        try:
            write_action.summarize_success(
                operation="merge-train-controller-run-once",
                request={"repository": "example/repo", "base_branch": "main", "mutate": False},
                provider_payload={
                    "status": "accepted",
                    "trace_id": "launchplane_req_example",
                    "records": {"merge_train_batch_candidate_record_id": "candidate-example"},
                    "result": {"repository": "example/repo", "base_branch": "main", "controller_action": "build_candidate", key: "secret-value"},
                },
            )
        except safety.LaunchplaneSafetyError as exc:
            assert exc.code == "unsafe_response_shape"
        else:
            raise AssertionError(f"expected secret key {key!r} to fail closed")
    try:
        write_action.summarize_success(
            operation="product-config-preflight",
            request={"product": "example-product", "context": "example-testing"},
            provider_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_product",
                "records": {},
                "result": {
                    "intent": {"status": "allowed", "reason_code": "policy_allowed"},
                    "runtime_key_safety_findings": [
                        {"key": "EXAMPLE_API_TOKEN", "code": "managed_secret", "client_secret": "secret-value"}
                    ],
                },
            },
        )
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == "unsafe_response_shape"
    else:
        raise AssertionError("expected nested secret-bearing product-config payload to fail closed")


def test_summaries_and_trace_ids_fail_closed_on_secret_values() -> None:
    for trace_id in ("Bearer secret-token", "https://private.example.invalid/trace", "trace id with spaces"):
        try:
            write_action.summarize_success(
                operation="merge-train-controller-run-once",
                request={"repository": "example/repo", "base_branch": "main", "mutate": False},
                provider_payload={"status": "accepted", "trace_id": trace_id, "records": {}, "result": {"repository": "example/repo", "base_branch": "main", "controller_action": "build_candidate"}},
            )
        except safety.LaunchplaneSafetyError as exc:
            assert exc.code == "invalid_response"
        else:
            raise AssertionError(f"expected unsafe trace id {trace_id!r} to fail closed")
    try:
        write_action.summarize_success(
            operation="product-config-preflight",
            request={"product": "example-product", "context": "example-testing"},
            provider_payload={"status": "accepted", "trace_id": "launchplane_req_product", "records": {}, "result": {"intent": {"status": "allowed", "reason_code": "policy_allowed", "safe_to_execute": True, "next_action": "Use Bearer secret-token before retrying."}}},
        )
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == "invalid_response"
    else:
        raise AssertionError("expected unsafe next_action to fail closed")
    http_error = urllib.error.HTTPError(
        "https://launchplane.example.invalid/v1/example",
        403,
        "Forbidden",
        hdrs=Message(),
        fp=io.BytesIO(json.dumps({"trace_id": "Bearer secret-token", "error": {"code": "authorization_denied", "message": "secret"}}).encode()),
    )
    try:
        write_action.summarize_http_error(operation="product-config-preflight", request={}, exc=http_error)
    except safety.LaunchplaneSafetyError as raised:
        assert raised.code == "invalid_response"
    else:
        raise AssertionError("expected unsafe HTTP error trace to fail closed")


def test_context_projection_contract_and_secret_shape() -> None:
    provider_context = json.loads((SCRIPT_DIR.parent / "references" / "context.available.example.json").read_text())
    raw_context = {"generated_at": provider_context["generated_at"], **provider_context["sections"]}
    payload = context_helper.normalize_launchplane_payload(
        {"result": {"context": raw_context}}, request=provider_context["request"]
    )
    assert payload["status"] == "available"
    assert payload["generated_at"] == "2026-01-02T03:04:05Z"
    assert payload["summary"]["state"] == "waiting"
    assert payload["sections"]["work_graph"]["items"][0]["safe_to_start"] is False
    provider_payload = {"result": {"context": {"work_graph": {"status": "available", "items": [{"source_of_truth_url": "https://github.com/example/repo/issues/1", "state": "waiting", "safe_to_start": False, "next_action": "Wait for checks.", "api_key": "secret-value"}]}}}}
    try:
        context_helper.normalize_launchplane_payload(provider_payload, request={"repository": "example/repo"})
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == "unsafe_response_shape"
    else:
        raise AssertionError("expected context projection to fail closed")


def test_current_agent_context_service_shape() -> None:
    provider_payload = {
        "status": "ok",
        "trace_id": "launchplane_req_context",
        "context": {
            "schema_version": 1,
            "generated_at": "2026-07-19T23:00:00Z",
            "repository": "example/repo",
            "source": {"section_count": 4, "available_section_count": 4},
            "sections": {
                "repo_product_mapping": {
                    "status": "available",
                    "reason_code": "",
                    "payload": {
                        "mapping": {
                            "schema_version": 1,
                            "generated_at": "2026-07-19T23:00:00Z",
                            "repositories": [
                                {
                                    "repository": "example/repo",
                                    "classification": "managed_runtime",
                                    "product": "example-product",
                                    "display_name": "Example",
                                    "driver_id": "generic-web",
                                    "contexts": ["testing"],
                                    "environments": ["example-instance"],
                                    "preview_context": "testing",
                                    "source": "product_profile",
                                    "updated_at": "2026-07-19T22:00:00Z",
                                }
                            ],
                        },
                        "source": {"product_count": 1, "work_request_count": 1},
                    },
                },
                "work_graph_snapshot": {
                    "status": "available",
                    "reason_code": "",
                    "payload": {
                        "snapshot": {
                            "schema_version": 1,
                            "generated_at": "2026-07-19T23:00:00Z",
                            "repos": [],
                            "issues": [
                                {
                                    "repository": "example/repo",
                                    "number": 1,
                                    "title": "Example issue",
                                    "url": "https://github.com/example/repo/issues/1",
                                    "state": "open",
                                    "blocked_by": 0,
                                }
                            ],
                        },
                        "source": {
                            "product_count": 1,
                            "work_request_count": 1,
                            "planning_fact_count": 1,
                        },
                    },
                },
                "every_code_summary": {
                    "status": "available",
                    "reason_code": "",
                    "payload": {
                        "summary": {
                            "schema_version": 1,
                            "generated_at": "2026-07-19T23:00:00Z",
                            "repository": "example/repo",
                            "summaries": [
                                {
                                    "state": "running",
                                    "summary_status": "active",
                                    "issue_url": "https://github.com/example/repo/issues/1",
                                    "result_pr_url": "https://github.com/example/repo/pull/2",
                                }
                            ],
                        }
                    },
                },
                "preview_readiness": {
                    "status": "available",
                    "reason_code": "",
                    "payload": {
                        "readiness": {
                            "schema_version": 1,
                            "generated_at": "2026-07-19T23:00:00Z",
                            "repository": "example/repo",
                            "items": [
                                {
                                    "readiness_status": "ready",
                                    "source_of_truth_url": "https://github.com/example/repo/pull/2",
                                    "detail": "Required checks passed.",
                                }
                            ],
                        }
                    },
                },
            },
        },
    }
    payload = context_helper.normalize_launchplane_payload(
        provider_payload, request={"repository": "example/repo"}
    )

    assert payload["summary"]["state"] == "open"
    assert payload["sections"]["repo_product_mapping"]["repositories"][0][
        "product_key"
    ] == "example-product"
    assert payload["sections"]["every_code"]["requests"][0]["summary_status"] == "active"
    assert payload["sections"]["preview_readiness"]["items"][0]["status"] == "ready"


def test_request_helpers_use_shared_safe_urlopen() -> None:
    calls: list[dict[str, Any]] = []

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"status":"accepted","result":{"controller_action":"idle"}}'

    def fake_safe_urlopen(request: urllib.request.Request, *, timeout: float) -> Response:
        calls.append({"url": request.full_url, "timeout": timeout, "headers": dict(request.header_items())})
        return Response()

    with patch.object(write_action, "safe_urlopen", side_effect=fake_safe_urlopen):
        write_action.request_launchplane(service_url="https://launchplane.example.invalid", path="/v1/work-graph/merge-train/controller/run-once", settings={"token": "operator-token"}, body={"schema_version": 1}, timeout=3)
    assert calls[0]["url"] == "https://launchplane.example.invalid/v1/work-graph/merge-train/controller/run-once"
    assert calls[0]["headers"]["Authorization"] == "Bearer operator-token"
    context_calls: list[str] = []

    def fake_context_safe_urlopen(request: urllib.request.Request, *, timeout: float) -> Response:
        context_calls.append(request.full_url)
        return Response()

    with patch.object(context_helper, "safe_urlopen", side_effect=fake_context_safe_urlopen):
        context_helper.request_launchplane("https://launchplane.example.invalid/v1/agent/context?repository=example%2Frepo", {"token": "context-token"}, 3)
    assert context_calls == ["https://launchplane.example.invalid/v1/agent/context?repository=example%2Frepo"]


def test_settings_diagnostic_validates_sources_without_printing_values() -> None:
    args = argparse.Namespace(config=None, env_config=None, url=None)
    with patch.object(write_action, "load_config", return_value={}):
        with patch.object(write_action, "load_operator_env", return_value={"LAUNCHPLANE_OPERATOR_URL": "http://launchplane.example.invalid", "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN": "secret-token-never-render"}):
            with patch.dict(write_action.os.environ, {}, clear=True):
                diagnostic = write_action.settings_diagnostic(args)
    rendered = json.dumps(diagnostic)
    assert diagnostic["classification"] == "invalid_service_url_http"
    assert diagnostic["ready"] is False
    assert "launchplane.example.invalid" not in rendered
    assert "secret-token-never-render" not in rendered


def main() -> int:
    tests = [
        test_endpoint_validation_policy,
        test_build_url_and_redirect_policy,
        test_write_helper_validates_cli_env_and_json_url_sources,
        test_context_helper_validates_env_and_json_url_sources,
        test_success_projection_preserves_contracts,
        test_current_launchplane_service_response_shapes,
        test_success_projection_fails_closed_on_secret_bearing_payloads,
        test_summaries_and_trace_ids_fail_closed_on_secret_values,
        test_context_projection_contract_and_secret_shape,
        test_current_agent_context_service_shape,
        test_request_helpers_use_shared_safe_urlopen,
        test_settings_diagnostic_validates_sources_without_printing_values,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
