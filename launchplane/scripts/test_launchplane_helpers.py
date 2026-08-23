#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused regression tests for Launchplane helper trust boundaries."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import types
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
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


contract: Any = load_module("launchplane_contract.py", "launchplane_contract")
safety: Any = load_module("launchplane_safety.py", "launchplane_safety")
write_action: Any = load_module("launchplane-write-action.py", "launchplane_write_action")
context_helper: Any = load_module("launchplane-context.py", "launchplane_context")


@contextmanager
def temporary_attribute(target: Any, name: str, value: Any) -> Iterator[None]:
    original = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, original)


def run_helper(script: str, args: list[str], env: dict[str, str] | None = None) -> dict[str, Any]:
    merged_env = {key: value for key, value in os.environ.items() if not key.startswith("LAUNCHPLANE_")}
    if env:
        merged_env.update(env)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / script), *args],
        capture_output=True,
        text=True,
        env=merged_env,
    )
    if not proc.stdout.strip():
        raise AssertionError(
            f"{script} emitted no JSON output; stderr={proc.stderr.strip()!r}"
        )
    return {"returncode": proc.returncode, "payload": json.loads(proc.stdout)}


def assert_rejects_url(value: str, code: str) -> None:
    try:
        safety.validate_service_url(value)
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == code, exc.code
    else:
        raise AssertionError(f"expected {value!r} to be rejected")


def contract_artifact() -> dict[str, Any]:
    return json.loads(contract.DEFAULT_CONTRACT_PATH.read_text(encoding="utf-8"))


def assert_contract_error(artifact: dict[str, Any], code: str) -> None:
    try:
        contract.validate_contract(artifact)
    except contract.ContractError as exc:
        assert exc.code == code, exc.code
    else:
        raise AssertionError(f"expected contract error {code}")


def test_agent_operator_contract_identity_and_provenance_semantics() -> None:
    artifact = contract_artifact()
    summary = contract.validate_contract(artifact)
    assert summary["semantic_digest_sha256"] == (
        "cd3ebae5f104042b4a8238a0ee4183c1bc01e8a60a764e44d1de734bde502da0"
    )
    assert summary["operation_count"] == 13
    assert summary["protected_workflow_count"] == 4
    assert summary["local_extension_count"] == 2
    assert summary["hermetic_only"] is True
    assert summary["upstream_freshness_proven"] is False

    provenance_only = copy.deepcopy(artifact)
    provenance_only["provenance"]["source_commit_sha"] = "f" * 40
    assert contract.semantic_digest(provenance_only) == contract.semantic_digest(
        artifact
    )
    contract.validate_contract(provenance_only)


def test_agent_operator_contract_rejects_drift_and_unsafe_content() -> None:
    artifact = contract_artifact()

    unsupported = copy.deepcopy(artifact)
    unsupported["normalization_version"] = 2
    assert_contract_error(unsupported, "unsupported_normalization_version")

    unsupported_schema = copy.deepcopy(artifact)
    unsupported_schema["schema_version"] = 2
    assert_contract_error(unsupported_schema, "unsupported_schema_version")

    digest_drift = copy.deepcopy(artifact)
    digest_drift["contract"]["invariants"]["governance"][
        "owner_acceptance_authoritative"
    ] = False
    assert_contract_error(digest_drift, "invariant_contract_mismatch")

    unsafe = copy.deepcopy(artifact)
    unsafe["contract"]["operations"][0]["purpose"] = (
        "Contact https://private.example.invalid for details."
    )
    unsafe["semantic_digest_sha256"] = contract.semantic_digest(unsafe)
    assert_contract_error(unsafe, "unsafe_public_value")

    malformed = copy.deepcopy(artifact)
    malformed["contract"]["operations"][0]["unexpected"] = True
    assert_contract_error(malformed, "invalid_operation_keys")

    digest_mismatch = copy.deepcopy(artifact)
    digest_mismatch["contract"]["operations"][0][
        "schema_fingerprint_sha256"
    ] = "f" * 64
    assert_contract_error(digest_mismatch, "semantic_digest_mismatch")

    projected_extension = copy.deepcopy(artifact)
    for operation in projected_extension["contract"]["operations"]:
        if operation["operation_id"] == "evaluate_agent_write_intent":
            operation["path"] = contract.LOCAL_EXTENSION_ROUTES[
                "generic-web-deploy-recovery-dry-run"
            ]["path"]
            break
    assert_contract_error(projected_extension, "local_extension_now_projected")


def test_agent_operator_contract_routes_every_local_consumer() -> None:
    artifact = contract_artifact()
    operation_paths = {
        operation["operation_id"]: operation["path"]
        for operation in artifact["contract"]["operations"]
    }
    for command, (operation_id, modes) in contract.PROJECTED_HELPER_COMMANDS.items():
        assert contract.helper_command_path(command) == operation_paths[operation_id]
        assert modes
    for command, extension in contract.LOCAL_EXTENSION_ROUTES.items():
        assert contract.helper_command_path(command) == extension["path"]
        assert extension["path"] not in operation_paths.values()
    context_args = argparse.Namespace(
        repo="example/repo", branch=None, issue=None, pr=None
    )
    assert context_helper.build_context_url(
        "https://launchplane.example.invalid", context_args
    ) == (
        "https://launchplane.example.invalid/v1/agent/context"
        "?repository=example%2Frepo"
    )
    for helper_filename in (
        "launchplane-context.py",
        "launchplane-write-action.py",
    ):
        helper_source = (SCRIPT_DIR / helper_filename).read_text(encoding="utf-8")
        assert '"/v1/' not in helper_source


def test_agent_operator_contract_cli_is_public_safe_and_hermetic() -> None:
    result = run_helper("check-agent-operator-contract.py", [])
    assert result["returncode"] == 0
    payload = result["payload"]
    assert payload["status"] == "ok"
    assert payload["summary"]["hermetic_only"] is True
    assert payload["summary"]["upstream_freshness_proven"] is False
    assert "url" not in json.dumps(payload).lower()


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
    with temporary_attribute(
        write_action,
        "load_config",
        lambda _path: {"service_url": "ftp://launchplane.example.invalid"},
    ):
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
    with temporary_attribute(
        context_helper,
        "load_config",
        lambda _path: {"service_url": "http://launchplane.example.invalid"},
    ):
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


def test_preview_feedback_remediation_body_and_projection() -> None:
    args = argparse.Namespace(
        mode="apply",
        product="verireel",
        context="verireel-preview",
        repository="cbusillo/verireel",
        pull_request_url="https://github.com/cbusillo/verireel/pull/311",
        terminal_status="cleared",
        reason="Clear stale preview feedback.",
        related_issue="cbusillo/launchplane#2076",
        idempotency_key="preview-remediation-311",
        reviewed_dry_run=True,
    )
    body = write_action.preview_feedback_remediation_body(args)
    assert body["confirmation"] == (
        "remediate preview feedback https://github.com/cbusillo/verireel/pull/311 "
        "to cleared"
    )
    projected = write_action.summarize_success(
        operation="preview-feedback-remediation",
        request={"mode": "apply", "product": "verireel"},
        provider_payload={
            "status": "accepted",
            "trace_id": "launchplane_req_remediation",
            "records": {
                "preview_pr_feedback_remediation_id": "remediation-311",
                "preview_pr_feedback_id": "feedback-311",
            },
            "result": {
                "schema_version": 1,
                "remediation_id": "remediation-311",
                "product": "verireel",
                "context": "verireel-preview",
                "repository": "cbusillo/verireel",
                "pull_request_url": "https://github.com/cbusillo/verireel/pull/311",
                "pull_request_number": 311,
                "mode": "apply",
                "terminal_status": "cleared",
                "actor": "local-operator:owner",
                "reason": "Clear stale preview feedback.",
                "related_issue": "cbusillo/launchplane#2076",
                "trace_id": "launchplane_req_remediation",
                "idempotency_key": "preview-remediation-311",
                "requested_at": "2026-08-10T21:00:00Z",
                "continuity_sha256": "a" * 64,
                "observation": {
                    "state": "absent",
                    "digest_sha256": "b" * 64,
                    "excerpt": "",
                    "marker": "",
                    "comment_id": 0,
                    "comment_url": "",
                    "comment_author_id": 0,
                    "comment_author_login": "",
                    "token_actor_id": 101,
                    "token_actor_login": "launchplane-bot",
                },
                "planned_action": "none",
                "outcome": "already_absent",
                "mutation_evidence": {
                    "attempted": False,
                    "mutated": False,
                    "method": "",
                    "comment_id": 0,
                    "before_digest_sha256": "",
                    "after_digest_sha256": "",
                    "verified_absent": True,
                    "error_message": "",
                },
                "companion_feedback_id": "feedback-311",
            },
        },
    )
    assert projected["result"]["outcome"] == "already_absent"
    assert projected["result"]["mutation_evidence"]["mutated"] is False
    dry_run_provider = {
        "status": "accepted",
        "trace_id": "launchplane_req_remediation_dry_run",
        "records": {"preview_pr_feedback_remediation_id": "remediation-dry-run-311"},
        "result": {
            "remediation_id": "remediation-dry-run-311",
            "mode": "dry-run",
            "outcome": "planned",
            "companion_feedback_id": "",
        },
    }
    dry_run = write_action.summarize_success(
        operation="preview-feedback-remediation",
        request={"mode": "dry-run", "product": "verireel"},
        provider_payload=dry_run_provider,
    )
    assert "companion_feedback_id" not in dry_run["result"]


def test_change_impact_policy_body_and_projection() -> None:
    private_record = {
        "schema_version": 1,
        "record_id": "change-impact-policy-123-r1",
        "status": "active",
        "repository_id": "123",
        "repository_owner_id": "456",
        "repository": "example/repo",
        "policy_revision": 1,
        "component_rules": [
            {
                "schema_version": 1,
                "rule_id": "change-impact-rule-private",
                "component": "private-component",
                "path_prefixes": ["private/path"],
                "affected_products": [],
                "review_tier": "routine",
                "production_affecting": None,
                "reason": "Private rule reason.",
            }
        ],
        "default_unknown_review_tier": "sensitive",
        "effective_at": "2026-08-13T20:00:00Z",
        "source": "private operator input",
        "reason": "Establish explicit repository maintenance impact policy.",
        "supersedes_record_id": None,
        "policy_digest": "a" * 64,
    }
    private_payload = {
        "schema_version": 1,
        "mode": "apply",
        "expected_current_record_id": "",
        "expected_current_policy_digest": "",
        "record": private_record,
    }
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "change-impact-policy.json"
        payload_path.write_text(json.dumps(private_payload), encoding="utf-8")
        apply_body = write_action.change_impact_policy_payload_body(
            argparse.Namespace(
                payload_file=str(payload_path),
                idempotency_key="change-impact-policy-example-1",
                reviewed_dry_run=True,
                expected_policy_digest="a" * 64,
            ),
            mode="apply",
        )
        assert apply_body == private_payload
        dry_run_body = write_action.change_impact_policy_payload_body(
            argparse.Namespace(
                payload_file=str(payload_path),
                idempotency_key="",
                reviewed_dry_run=False,
                expected_policy_digest="",
            ),
            mode="dry_run",
        )
        assert dry_run_body["mode"] == "dry_run"
        assert private_payload["mode"] == "apply"

        payload_without_digest = {
            **private_payload,
            "record": {**private_record, "policy_digest": ""},
        }
        payload_path.write_text(json.dumps(payload_without_digest), encoding="utf-8")
        digest_bound_body = write_action.change_impact_policy_payload_body(
            argparse.Namespace(
                payload_file=str(payload_path),
                idempotency_key="change-impact-policy-example-1",
                reviewed_dry_run=True,
                expected_policy_digest="a" * 64,
            ),
            mode="apply",
        )
        assert digest_bound_body["record"]["policy_digest"] == "a" * 64

    projected = write_action.summarize_success(
        operation="change-impact-policy-dry-run",
        request={"mode": "dry_run", "payload_source": "private_file"},
        provider_payload={
            "status": "ok",
            "trace_id": "launchplane_req_change_impact",
            "result": {
                "schema_version": 1,
                "status": "would_apply",
                "record": private_record,
            },
        },
    )
    assert projected["summary"]["policy_apply_status"] == "would_apply"
    assert projected["result"] == {
        "status": "would_apply",
        "record": {
            "record_id": "change-impact-policy-123-r1",
            "policy_digest": "a" * 64,
            "policy_revision": 1,
            "status": "active",
            "effective_at": "2026-08-13T20:00:00Z",
        },
    }
    rendered = json.dumps(projected)
    for private_value in (
        "private-component",
        "private/path",
        "Private rule reason.",
        "example/repo",
        "private operator input",
    ):
        assert private_value not in rendered


def test_change_impact_policy_apply_requires_review_idempotency_and_reason() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "change-impact-policy.json"
        payload_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "record": {"reason": "Approved.", "policy_digest": "a" * 64},
                }
            ),
            encoding="utf-8",
        )
        args = argparse.Namespace(
            payload_file=str(payload_path),
            idempotency_key="",
            reviewed_dry_run=False,
            expected_policy_digest="",
        )
        try:
            write_action.change_impact_policy_payload_body(args, mode="apply")
        except ValueError as exc:
            assert str(exc) == "idempotency_key_required"
        else:
            raise AssertionError("expected change-impact apply idempotency requirement")
        args.idempotency_key = "change-impact-policy-example-1"
        try:
            write_action.change_impact_policy_payload_body(args, mode="apply")
        except ValueError as exc:
            assert str(exc) == "reviewed_dry_run_required"
        else:
            raise AssertionError("expected reviewed dry-run acknowledgement")
        args.reviewed_dry_run = True
        args.expected_policy_digest = "a" * 64
        payload_path.write_text(
            json.dumps({"schema_version": 1, "record": {"reason": ""}}),
            encoding="utf-8",
        )
        try:
            write_action.change_impact_policy_payload_body(args, mode="apply")
        except ValueError as exc:
            assert str(exc) == "reason_required"
        else:
            raise AssertionError("expected embedded policy reason requirement")
        payload_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "record": {"reason": "Approved.", "policy_digest": "b" * 64},
                }
            ),
            encoding="utf-8",
        )
        try:
            write_action.change_impact_policy_payload_body(args, mode="apply")
        except ValueError as exc:
            assert str(exc) == "policy_digest_mismatch"
        else:
            raise AssertionError("expected dry-run policy digest binding")
        args.expected_policy_digest = "not-a-digest"
        try:
            write_action.change_impact_policy_payload_body(args, mode="apply")
        except ValueError as exc:
            assert str(exc) == "invalid_expected_policy_digest"
        else:
            raise AssertionError("expected local policy digest validation")


def test_change_impact_policy_payload_rejects_repo_local_files() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        repo_root = Path(directory) / "repo"
        repo_root.mkdir()
        payload_path = repo_root / "change-impact-policy.json"
        payload_path.write_text(
            json.dumps({"schema_version": 1, "record": {"reason": "Approved."}}),
            encoding="utf-8",
        )
        with temporary_attribute(write_action, "active_repo_root", lambda: repo_root):
            try:
                write_action.change_impact_policy_payload_body(
                    argparse.Namespace(
                        payload_file=str(payload_path),
                        idempotency_key="",
                        reviewed_dry_run=False,
                        expected_policy_digest="",
                    ),
                    mode="dry_run",
                )
            except ValueError as exc:
                assert str(exc) == "repo_local_payload_unsupported"
            else:
                raise AssertionError("expected repo-local change-impact payload rejection")


def test_change_impact_policy_projection_fails_closed_on_extra_fields() -> None:
    try:
        write_action.summarize_success(
            operation="change-impact-policy-apply",
            request={"mode": "apply", "payload_source": "private_file"},
            provider_payload={
                "status": "ok",
                "trace_id": "launchplane_req_change_impact",
                "result": {
                    "schema_version": 1,
                    "status": "applied",
                    "record": {
                        "record_id": "change-impact-policy-123-r1",
                        "status": "active",
                        "repository_id": "123",
                        "repository_owner_id": "456",
                        "repository": "example/repo",
                        "policy_revision": 1,
                        "component_rules": [],
                        "default_unknown_review_tier": "sensitive",
                        "effective_at": "2026-08-13T20:00:00Z",
                        "source": "private operator input",
                        "reason": "Approved.",
                        "supersedes_record_id": None,
                        "policy_digest": "a" * 64,
                        "unexpected_private_field": "must not pass",
                    },
                },
            },
        )
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == "unsafe_response_shape"
    else:
        raise AssertionError("expected extra change-impact record field to fail closed")

    nested_record = {
        "schema_version": 1,
        "record_id": "change-impact-policy-123-r1",
        "status": "active",
        "repository_id": "123",
        "repository_owner_id": "456",
        "repository": "example/repo",
        "policy_revision": 1,
        "component_rules": [
            {
                "schema_version": 1,
                "rule_id": "change-impact-rule-private",
                "component": "private-component",
                "path_prefixes": ["private/path"],
                "affected_products": [],
                "review_tier": "routine",
                "production_affecting": None,
                "reason": "Private rule reason.",
                "unexpected_private_field": "must not pass",
            }
        ],
        "default_unknown_review_tier": "sensitive",
        "effective_at": "2026-08-13T20:00:00Z",
        "source": "private operator input",
        "reason": "Approved.",
        "supersedes_record_id": None,
        "policy_digest": "a" * 64,
    }
    try:
        write_action.summarize_success(
            operation="change-impact-policy-apply",
            request={"mode": "apply", "payload_source": "private_file"},
            provider_payload={
                "status": "ok",
                "trace_id": "launchplane_req_change_impact",
                "result": {
                    "schema_version": 1,
                    "status": "applied",
                    "record": nested_record,
                },
            },
        )
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == "unsafe_response_shape"
    else:
        raise AssertionError("expected nested change-impact field to fail closed")


def test_change_impact_policy_cli_dispatches_exact_route_and_modes() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "change-impact-policy.json"
        payload_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "record": {"reason": "Approved.", "policy_digest": "a" * 64},
                }
            ),
            encoding="utf-8",
        )
        calls: list[dict[str, Any]] = []

        def fake_execute_post(**kwargs: Any) -> int:
            calls.append(kwargs)
            return 0

        with temporary_attribute(write_action, "execute_post", fake_execute_post):
            assert (
                write_action.main(
                    [
                        "change-impact-policy-dry-run",
                        "--payload-file",
                        str(payload_path),
                    ]
                )
                == 0
            )
            assert (
                write_action.main(
                    [
                        "change-impact-policy-apply",
                        "--payload-file",
                        str(payload_path),
                        "--reviewed-dry-run",
                        "--idempotency-key",
                        "change-impact-policy-example-1",
                        "--expected-policy-digest",
                        "a" * 64,
                    ]
                )
                == 0
            )
    assert [call["path"] for call in calls] == [
        "/v1/change-impact/policies/apply",
        "/v1/change-impact/policies/apply",
    ]
    assert [call["body"]["mode"] for call in calls] == ["dry_run", "apply"]
    assert calls[0]["request"] == {
        "mode": "dry_run",
        "payload_source": "private_file",
    }
    assert calls[1]["request"] == {
        "mode": "apply",
        "payload_source": "private_file",
    }


def test_change_impact_policy_read_projection_is_bounded() -> None:
    record = {
        "schema_version": 1,
        "record_id": "change-impact-policy-123-r1",
        "status": "active",
        "repository_id": "123",
        "repository_owner_id": "456",
        "repository": "example/repo",
        "policy_revision": 1,
        "component_rules": [
            {
                "schema_version": 1,
                "rule_id": "change-impact-rule-private",
                "component": "private-component",
                "path_prefixes": ["private/path"],
                "affected_products": [],
                "review_tier": "routine",
                "production_affecting": None,
                "reason": "Private rule reason.",
            }
        ],
        "default_unknown_review_tier": "sensitive",
        "effective_at": "2026-08-13T20:00:00Z",
        "source": "private operator input",
        "reason": "Approved.",
        "supersedes_record_id": None,
        "policy_digest": "a" * 64,
    }
    payload = write_action.summarize_change_impact_policy_read(
        request={"payload_source": "operator_argument"},
        provider_payload={
            "status": "ok",
            "trace_id": "launchplane_req_change_impact_read",
            "read_model": {
                "schema_version": 1,
                "mode": "shadow",
                "authoritative": False,
                "enforcement_effect": "none",
                "repository_id": "123",
                "current_policy": record,
                "policy_history_count": 1,
            },
        },
    )
    assert payload["result"]["current_policy"]["policy_digest"] == "a" * 64
    rendered = json.dumps(payload)
    for private_value in ("example/repo", "private-component", "private/path", "456"):
        assert private_value not in rendered

    empty_payload = write_action.summarize_change_impact_policy_read(
        request={"payload_source": "operator_argument"},
        provider_payload={
            "status": "ok",
            "trace_id": "launchplane_req_change_impact_empty",
            "read_model": {
                "schema_version": 1,
                "mode": "shadow",
                "authoritative": False,
                "enforcement_effect": "none",
                "repository_id": "123",
                "current_policy": None,
                "policy_history_count": 0,
            },
        },
    )
    assert empty_payload["result"]["current_policy"] is None
    assert empty_payload["result"]["policy_history_count"] == 0


def _activation_preflight_payload() -> dict[str, object]:
    return {
        "status": "ok",
        "trace_id": "launchplane_req_activation_preflight",
        "policy": {
            "record_id": "authz-policy-17",
            "revision": 17,
            "policy_sha256": "a" * 64,
            "schema_version": 2,
        },
        "session": {
            "session_count": 1,
            "claims_current": True,
            "claims_age_bucket_hours": 2,
            "expiry_bucket_hours": 4,
            "identity_fingerprint": f"identity_{'b' * 64}",
        },
        "scope": {
            "action": "authz_policy_grant.write",
            "product": "launchplane",
            "context": "launchplane",
            "target_scope": "context",
        },
        "evaluation": {"decision": "allowed", "reason_code": "allowed"},
        "unmanaged_action_empty_rules": {
            "total": 1,
            "github_actions": 0,
            "github_humans": 1,
            "terminal_agents": 0,
            "local_operators": 0,
            "local_admins": 0,
        },
    }


def test_authz_activation_preflight_projection_is_bounded() -> None:
    payload = write_action.summarize_authz_activation_preflight_read(
        request={"github_id": 123, "payload_source": "operator_argument"},
        provider_payload=_activation_preflight_payload(),
    )
    assert payload["status"] == "ok"
    assert payload["summary"]["decision"] == "allowed"
    assert payload["result"]["scope"] == {
        "action": "authz_policy_grant.write",
        "product": "launchplane",
        "context": "launchplane",
        "target_scope": "context",
    }
    assert "github_id" not in json.dumps(payload["result"])

    malformed = _activation_preflight_payload()
    malformed_session = malformed["session"]
    assert isinstance(malformed_session, dict)
    malformed["session"] = {**malformed_session, "login": "must-not-pass"}
    try:
        write_action.summarize_authz_activation_preflight_read(
            request={"github_id": 123}, provider_payload=malformed
        )
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == "unsafe_response_shape"
    else:
        raise AssertionError("expected extra activation-preflight field to fail closed")

    inconsistent = _activation_preflight_payload()
    inconsistent["evaluation"] = {
        "decision": "allowed",
        "reason_code": "no_matching_grant",
    }
    try:
        write_action.summarize_authz_activation_preflight_read(
            request={"github_id": 123}, provider_payload=inconsistent
        )
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == "invalid_response"
    else:
        raise AssertionError("expected inconsistent decision to fail closed")


def test_authz_activation_preflight_uses_admin_token_and_exact_route() -> None:
    args = argparse.Namespace(
        config=None,
        env_config=None,
        url=None,
        timeout=3,
        github_id=123,
    )
    with temporary_attribute(write_action, "load_config", lambda _path: {}):
        with temporary_attribute(write_action, "load_operator_env", lambda _path=None: {}):
            with patch.dict(
                write_action.os.environ,
                {
                    "LAUNCHPLANE_OPERATOR_URL": "https://launchplane.example.invalid",
                    "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN": "operator-token-must-not-be-used",
                },
                clear=True,
            ):
                settings = write_action.resolve_admin_settings(args)
    assert settings["token"] == ""

    calls: list[dict[str, Any]] = []

    def fake_request(**kwargs: Any) -> dict[str, object]:
        calls.append(kwargs)
        return _activation_preflight_payload()

    output = io.StringIO()
    with temporary_attribute(
        write_action,
        "resolve_admin_settings",
        lambda _args: {
            "service_url": "https://launchplane.example.invalid",
            "token": "admin-token",
            "subject": "",
            "token_label": "",
            "public_url_hint_sources": "",
        },
    ):
        with temporary_attribute(write_action, "request_launchplane", fake_request):
            with redirect_stdout(output):
                status = write_action.main(
                    ["authz-activation-preflight-read", "--github-id", "123"]
                )
    assert status == 0
    assert calls[0]["path"] == "/v1/authz-diagnostics/activation-preflight/read"
    assert calls[0]["body"] == {"github_id": 123}
    assert "idempotency_key" not in calls[0]
    assert json.loads(output.getvalue())["summary"]["decision"] == "allowed"


def test_authz_activation_preflight_errors_are_read_only() -> None:
    http_error = urllib.error.HTTPError(
        "https://launchplane.example.invalid/v1/authz-diagnostics/activation-preflight/read",
        403,
        "Forbidden",
        hdrs=Message(),
        fp=io.BytesIO(
            json.dumps(
                {
                    "trace_id": "launchplane_req_activation_denied",
                    "error": {"code": "authorization_denied"},
                }
            ).encode()
        ),
    )
    payload = write_action.summarize_http_error(
        operation="authz-activation-preflight-read", request={"github_id": 123}, exc=http_error
    )
    message = payload["warnings"][0]["message"]
    assert "read was rejected" in message
    assert "write action" not in message


def test_change_impact_apply_success_projection_failure_is_unverified() -> None:
    output = io.StringIO()
    with temporary_attribute(
        write_action,
        "resolve_settings",
        lambda _args: {
            "service_url": "https://launchplane.example.invalid",
            "token": "operator-token",
            "public_url_hint_sources": [],
        },
    ):
        with temporary_attribute(
            write_action,
            "request_launchplane",
            lambda **_kwargs: {
                "status": "ok",
                "trace_id": "launchplane_req_applied_unverified",
                "result": {"unexpected": "shape"},
            },
        ):
            with redirect_stdout(output):
                status = getattr(write_action, "execute_post")(
                    args=argparse.Namespace(
                        timeout=3,
                        idempotency_key="change-impact-policy-example-1",
                    ),
                    operation="change-impact-policy-apply",
                    path="/v1/change-impact/policies/apply",
                    request={"mode": "apply", "payload_source": "private_file"},
                    body={"schema_version": 1},
                )
    payload = json.loads(output.getvalue())
    assert status == 0
    assert payload["status"] == "accepted_unverified"
    assert payload["warnings"][0]["code"] == "apply_response_unverified"


def test_invalid_private_payload_does_not_expose_path() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "private-change-impact-policy.json"
        payload_path.write_text("{broken", encoding="utf-8")
        try:
            write_action.read_payload_file(str(payload_path))
        except ValueError as exc:
            assert str(exc) == "invalid_payload"
            assert str(payload_path) not in str(exc)
        else:
            raise AssertionError("expected invalid private payload rejection")


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


def test_product_config_projection_accepts_context_scoped_runtime_environment() -> None:
    result = write_action.summarize_success(
        operation="product-config-dry-run",
        request={"product": "example-product", "context": "testing"},
        provider_payload={
            "status": "accepted",
            "trace_id": "launchplane_req_context_runtime",
            "records": {},
            "result": {
                "status": "ok",
                "mode": "dry-run",
                "product": "example-product",
                "context": "testing",
                "instance": "",
                "runtime_environment": {
                    "action": "updated",
                    "scope": "context",
                    "context": "testing",
                    "instance": "",
                    "keys": ["EXAMPLE_PREVIEW_URL"],
                    "changed_keys": ["EXAMPLE_PREVIEW_URL"],
                    "unchanged_keys": [],
                    "env_value_count_after": 1,
                    "record": None,
                },
                "runtime_key_safety": {
                    "required": False,
                    "status": "skipped",
                    "checked_binding_keys": [],
                    "findings": [],
                },
                "secrets": [],
                "summary": {
                    "runtime_changed_key_count": 1,
                    "secret_change_count": 0,
                },
                "next_actions": [],
            },
        },
    )

    assert "instance" not in result["result"]
    assert result["result"]["runtime_environment"] == {
        "action": "updated",
        "scope": "context",
        "context": "testing",
        "keys": ["EXAMPLE_PREVIEW_URL"],
        "changed_keys": ["EXAMPLE_PREVIEW_URL"],
        "unchanged_keys": [],
        "env_value_count_after": 1,
    }


def test_optional_public_identifier_rejects_null_values() -> None:
    assert write_action._optional_public_identifier("") is None
    try:
        write_action._optional_public_identifier(None)
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == "invalid_response"
    else:
        raise AssertionError("expected null optional identifier to fail closed")


def test_runtime_environment_projection_enforces_scope_identity() -> None:
    global_result = write_action._project_runtime_environment(
        {
            "action": "updated",
            "scope": "global",
            "context": "",
            "instance": "",
            "keys": [],
            "changed_keys": [],
            "unchanged_keys": [],
            "env_value_count_after": 0,
            "record": None,
        }
    )
    assert global_result["scope"] == "global"
    assert "context" not in global_result
    assert "instance" not in global_result

    for invalid_target in (
        {"scope": "context", "context": "", "instance": ""},
        {"scope": "context", "context": "testing", "instance": "example-instance"},
        {"scope": "instance", "context": "testing", "instance": ""},
        {"scope": "global", "context": "testing", "instance": ""},
    ):
        try:
            write_action._project_runtime_environment(
                {
                    "action": "updated",
                    **invalid_target,
                    "keys": [],
                    "changed_keys": [],
                    "unchanged_keys": [],
                    "env_value_count_after": 0,
                    "record": None,
                }
            )
        except safety.LaunchplaneSafetyError as exc:
            assert exc.code == "invalid_response"
        else:
            raise AssertionError(f"expected invalid runtime target to fail: {invalid_target}")


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


def test_denied_recommendation_escalates_without_borrowing_ci_authority() -> None:
    recommendation = write_action.http_error_recommendation("denied").lower()

    assert write_action._status_for_http_error(
        403, {"error": {"code": "authorization_denied"}}
    ) == "denied"
    assert "authority-scope" in recommendation
    assert "escalate" in recommendation
    assert "do not probe routes manually" in recommendation
    assert "workflow" in recommendation
    assert "check the intended launchplane authz reconciliation" not in recommendation


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

        @staticmethod
        def read() -> bytes:
            return b'{"status":"accepted","result":{"controller_action":"idle"}}'

    def fake_safe_urlopen(request: urllib.request.Request, *, timeout: float) -> Response:
        calls.append({"url": request.full_url, "timeout": timeout, "headers": dict(request.header_items())})
        return Response()

    with temporary_attribute(write_action, "safe_urlopen", fake_safe_urlopen):
        write_action.request_launchplane(service_url="https://launchplane.example.invalid", path="/v1/work-graph/merge-train/controller/run-once", settings={"token": "operator-token"}, body={"schema_version": 1}, timeout=3)
    assert calls[0]["url"] == "https://launchplane.example.invalid/v1/work-graph/merge-train/controller/run-once"
    assert calls[0]["headers"]["Authorization"] == "Bearer operator-token"
    context_calls: list[str] = []

    def fake_context_safe_urlopen(request: urllib.request.Request, *, timeout: float) -> Response:
        assert timeout == 3
        context_calls.append(request.full_url)
        return Response()

    with temporary_attribute(context_helper, "safe_urlopen", fake_context_safe_urlopen):
        context_helper.request_launchplane("https://launchplane.example.invalid/v1/agent/context?repository=example%2Frepo", {"token": "context-token"}, 3)
    assert context_calls == ["https://launchplane.example.invalid/v1/agent/context?repository=example%2Frepo"]


def test_settings_diagnostic_validates_sources_without_printing_values() -> None:
    args = argparse.Namespace(config=None, env_config=None, url=None)
    with temporary_attribute(write_action, "load_config", lambda _path: {}):
        with temporary_attribute(
            write_action,
            "load_operator_env",
            lambda _args=None: {
                "LAUNCHPLANE_OPERATOR_URL": "http://launchplane.example.invalid",
                "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN": "secret-token-never-render",
            },
        ):
            with patch.dict(write_action.os.environ, {}, clear=True):
                diagnostic = write_action.settings_diagnostic(args)
    rendered = json.dumps(diagnostic)
    assert diagnostic["classification"] == "invalid_service_url_http"
    assert diagnostic["ready"] is False
    assert "launchplane.example.invalid" not in rendered
    assert "secret-token-never-render" not in rendered


def _generic_web_deploy_recovery_payload(*, reason: str = "Recover from failed deploy.") -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "example-product",
        "instance": "example-instance",
        "original_deploy": {
            "schema_version": 1,
            "product": "example-product",
            "deploy": {
                "schema_version": 1,
                "product": "example-product",
                "instance": "example-instance",
                "artifact_id": "ghcr.io/example/product@sha256:abc123",
                "source_git_ref": "abc123",
            },
        },
        "reason": reason,
    }


def _generic_web_deploy_recovery_evidence(
    *,
    recovery_digest: str = "a" * 64,
    proposed_action: str = "retry_original_operation",
    provider_outcome: str = "absent",
    retry_safe: bool = True,
    product: str = "example-product",
    instance: str = "example-instance",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "ok",
        "provider": "launchplane",
        "operation": "generic-web-deploy-recovery-dry-run",
        "generated_at": "2026-08-17T03:10:05Z",
        "request": {"mode": "dry_run", "payload_source": "private_file"},
        "summary": {},
        "records": {},
        "result": {
            "status": "ok",
            "mode": "dry-run",
            "product": product,
            "instance": instance,
            "provider_outcome": provider_outcome,
            "retry_safe": retry_safe,
            "proposed_action": proposed_action,
            "recovery_digest": recovery_digest,
        },
        "warnings": [],
    }


def test_generic_web_deploy_recovery_body_and_projection() -> None:
    private_payload = _generic_web_deploy_recovery_payload()
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "deploy-recovery.json"
        evidence_path = Path(directory) / "deploy-recovery-dry-run-output.json"
        payload_path.write_text(json.dumps(private_payload), encoding="utf-8")
        evidence_path.write_text(
            json.dumps(_generic_web_deploy_recovery_evidence()),
            encoding="utf-8",
        )

        dry_run_body = write_action.generic_web_deploy_recovery_body(
            argparse.Namespace(
                payload_file=str(payload_path),
                idempotency_key="original-deploy-key-1",
                reviewed_dry_run=False,
            ),
            mode="dry_run",
        )
        assert dry_run_body == private_payload
        assert "expected_recovery_digest" not in dry_run_body

        apply_body = write_action.generic_web_deploy_recovery_body(
            argparse.Namespace(
                payload_file=str(payload_path),
                idempotency_key="original-deploy-key-1",
                reviewed_dry_run=True,
                expected_recovery_digest="a" * 64,
                dry_run_evidence_file=str(evidence_path),
            ),
            mode="apply",
        )
        assert apply_body["expected_recovery_digest"] == "a" * 64

        payload_with_digest = {**private_payload, "expected_recovery_digest": ""}
        payload_path.write_text(json.dumps(payload_with_digest), encoding="utf-8")
        digest_bound_body = write_action.generic_web_deploy_recovery_body(
            argparse.Namespace(
                payload_file=str(payload_path),
                idempotency_key="original-deploy-key-1",
                reviewed_dry_run=True,
                expected_recovery_digest="a" * 64,
                dry_run_evidence_file=str(evidence_path),
            ),
            mode="apply",
        )
        assert digest_bound_body["expected_recovery_digest"] == "a" * 64

    projected_dry_run = write_action.summarize_success(
        operation="generic-web-deploy-recovery-dry-run",
        request={"mode": "dry_run", "payload_source": "private_file"},
        provider_payload={
            "schema_version": 1,
            "status": "ok",
            "mode": "dry-run",
            "product": "example-product",
            "context": "prod",
            "instance": "example-instance",
            "reservation_state": "reconcile_required",
            "reservation_attempt": 1,
            "reservation_created_at": "2026-08-15T00:00:00Z",
            "reservation_updated_at": "2026-08-15T00:01:00Z",
            "reservation_lease_expires_at": "",
            "observed_at": "2026-08-16T00:00:00Z",
            "reconciliation_key_sha256": "b" * 64,
            "provider_target_key_sha256": "c" * 64,
            "provider_effect_phase": "target_update",
            "provider_outcome": "absent",
            "provider_status": "",
            "retry_safe": True,
            "proposed_action": "retry_original_operation",
            "recovery_digest": "a" * 64,
        },
    )
    assert projected_dry_run["summary"]["recovery_action"] == "retry_original_operation"
    assert projected_dry_run["result"]["recovery_digest"] == "a" * 64
    assert projected_dry_run["records"] == {}

    rendered = json.dumps(projected_dry_run)
    for private_value in (
        "ghcr.io/example/product@sha256:abc123",
        "original-deploy-key-1",
    ):
        assert private_value not in rendered

    projected_apply = write_action.summarize_success(
        operation="generic-web-deploy-recovery-apply",
        request={"mode": "apply", "payload_source": "private_file"},
        provider_payload={
            "schema_version": 1,
            "status": "accepted",
            "mode": "apply",
            "trace_id": "launchplane_req_recovery_apply",
            "product": "example-product",
            "context": "prod",
            "instance": "example-instance",
            "reservation_state": "completed",
            "reservation_attempt": 2,
            "recovery_action": "retry_original_operation",
            "recovery_digest": "a" * 64,
            "provider_outcome": "absent",
            "provider_status": "",
            "retry_safe": True,
        },
    )
    assert projected_apply["result"]["status"] == "accepted"
    assert projected_apply["result"]["recovery_action"] == "retry_original_operation"
    assert "Verify" in projected_apply["summary"]["recommendation"]


def test_generic_web_deploy_recovery_apply_requires_review_idempotency_and_reason() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "deploy-recovery.json"
        evidence_path = Path(directory) / "deploy-recovery-dry-run-output.json"
        payload_path.write_text(
            json.dumps(_generic_web_deploy_recovery_payload()),
            encoding="utf-8",
        )
        evidence_path.write_text(
            json.dumps(_generic_web_deploy_recovery_evidence()),
            encoding="utf-8",
        )
        args = argparse.Namespace(
            payload_file=str(payload_path),
            idempotency_key="",
            reviewed_dry_run=False,
            expected_recovery_digest="",
            dry_run_evidence_file=str(evidence_path),
        )
        try:
            write_action.generic_web_deploy_recovery_body(args, mode="dry_run")
        except ValueError as exc:
            assert str(exc) == "idempotency_key_required"
        else:
            raise AssertionError("expected idempotency requirement for dry-run")

        args.idempotency_key = "original-deploy-key-1"
        dry_run_body = write_action.generic_web_deploy_recovery_body(args, mode="dry_run")
        assert dry_run_body["reason"] == "Recover from failed deploy."

        args.idempotency_key = ""
        try:
            write_action.generic_web_deploy_recovery_body(args, mode="apply")
        except ValueError as exc:
            assert str(exc) == "idempotency_key_required"
        else:
            raise AssertionError("expected idempotency requirement for apply")

        args.idempotency_key = "original-deploy-key-1"
        try:
            write_action.generic_web_deploy_recovery_body(args, mode="apply")
        except ValueError as exc:
            assert str(exc) == "reviewed_dry_run_required"
        else:
            raise AssertionError("expected reviewed_dry_run requirement")

        args.reviewed_dry_run = True
        payload_path.write_text(
            json.dumps(_generic_web_deploy_recovery_payload(reason="")),
            encoding="utf-8",
        )
        try:
            write_action.generic_web_deploy_recovery_body(args, mode="apply")
        except ValueError as exc:
            assert str(exc) == "reason_required"
        else:
            raise AssertionError("expected embedded reason requirement")

        payload_path.write_text(
            json.dumps(
                {
                    **_generic_web_deploy_recovery_payload(),
                    "expected_recovery_digest": "b" * 64,
                }
            ),
            encoding="utf-8",
        )
        args.expected_recovery_digest = "a" * 64
        try:
            write_action.generic_web_deploy_recovery_body(args, mode="apply")
        except ValueError as exc:
            assert str(exc) == "recovery_digest_mismatch"
        else:
            raise AssertionError("expected recovery_digest_mismatch")

        args.expected_recovery_digest = "not-a-digest"
        try:
            write_action.generic_web_deploy_recovery_body(args, mode="apply")
        except ValueError as exc:
            assert str(exc) == "invalid_expected_recovery_digest"
        else:
            raise AssertionError("expected invalid_expected_recovery_digest")


def test_generic_web_deploy_recovery_apply_requires_apply_eligible_evidence() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "deploy-recovery.json"
        evidence_path = Path(directory) / "deploy-recovery-dry-run-output.json"
        payload_path.write_text(
            json.dumps(_generic_web_deploy_recovery_payload()),
            encoding="utf-8",
        )
        args = argparse.Namespace(
            payload_file=str(payload_path),
            idempotency_key="original-deploy-key-1",
            reviewed_dry_run=True,
            expected_recovery_digest="a" * 64,
            dry_run_evidence_file=str(evidence_path),
        )
        ineligible_evidence = (
            _generic_web_deploy_recovery_evidence(proposed_action="hold_unknown"),
            _generic_web_deploy_recovery_evidence(provider_outcome="unknown"),
            _generic_web_deploy_recovery_evidence(retry_safe=False),
            _generic_web_deploy_recovery_evidence(recovery_digest="b" * 64),
            _generic_web_deploy_recovery_evidence(instance="another-instance"),
            {"status": "ok", "result": {}},
        )
        for evidence in ineligible_evidence:
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            try:
                write_action.generic_web_deploy_recovery_body(args, mode="apply")
            except ValueError as exc:
                assert str(exc) == "reviewed_dry_run_not_apply_eligible"
            else:
                raise AssertionError("expected ineligible reviewed dry-run rejection")

        args.dry_run_evidence_file = ""
        try:
            write_action.generic_web_deploy_recovery_body(args, mode="apply")
        except ValueError as exc:
            assert str(exc) == "reviewed_dry_run_not_apply_eligible"
        else:
            raise AssertionError("expected reviewed dry-run evidence requirement")


def test_generic_web_deploy_recovery_payload_rejects_repo_local_files() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        repo_root = Path(directory) / "repo"
        repo_root.mkdir()
        payload_path = repo_root / "deploy-recovery.json"
        payload_path.write_text(
            json.dumps(_generic_web_deploy_recovery_payload(reason="Recover.")),
            encoding="utf-8",
        )
        with temporary_attribute(write_action, "active_repo_root", lambda: repo_root):
            try:
                write_action.generic_web_deploy_recovery_body(
                    argparse.Namespace(
                        payload_file=str(payload_path),
                        idempotency_key="original-deploy-key-1",
                        reviewed_dry_run=False,
                    ),
                    mode="dry_run",
                )
            except ValueError as exc:
                assert str(exc) == "repo_local_payload_unsupported"
            else:
                raise AssertionError("expected repo-local payload rejection")


def test_generic_web_deploy_recovery_cli_dispatches_exact_routes() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "deploy-recovery.json"
        evidence_path = Path(directory) / "deploy-recovery-dry-run-output.json"
        payload_path.write_text(
            json.dumps(_generic_web_deploy_recovery_payload()),
            encoding="utf-8",
        )
        evidence_path.write_text(
            json.dumps(_generic_web_deploy_recovery_evidence()),
            encoding="utf-8",
        )
        calls: list[dict[str, Any]] = []

        def fake_execute_post(**kwargs: Any) -> int:
            calls.append(kwargs)
            return 0

        with temporary_attribute(write_action, "execute_post", fake_execute_post):
            assert (
                write_action.main(
                    [
                        "generic-web-deploy-recovery-dry-run",
                        "--payload-file",
                        str(payload_path),
                        "--idempotency-key",
                        "original-deploy-key-1",
                    ]
                )
                == 0
            )
            assert (
                write_action.main(
                    [
                        "generic-web-deploy-recovery-apply",
                        "--payload-file",
                        str(payload_path),
                        "--idempotency-key",
                        "original-deploy-key-1",
                        "--reviewed-dry-run",
                        "--expected-recovery-digest",
                        "a" * 64,
                        "--dry-run-evidence-file",
                        str(evidence_path),
                    ]
                )
                == 0
            )
    assert [call["path"] for call in calls] == [
        "/v1/admin/generic-web/deploy-recovery/dry-run",
        "/v1/admin/generic-web/deploy-recovery/apply",
    ]
    assert calls[0]["request"] == {"mode": "dry_run", "payload_source": "private_file"}
    assert calls[1]["request"] == {"mode": "apply", "payload_source": "private_file"}
    assert calls[1]["body"]["expected_recovery_digest"] == "a" * 64
    assert "original_deploy" not in json.dumps(calls[0]["request"])
    assert "original_deploy" not in json.dumps(calls[1]["request"])


def test_generic_web_deploy_recovery_cli_refuses_ineligible_evidence_without_http() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "deploy-recovery.json"
        evidence_path = Path(directory) / "deploy-recovery-dry-run-output.json"
        payload_path.write_text(
            json.dumps(_generic_web_deploy_recovery_payload()),
            encoding="utf-8",
        )
        evidence_path.write_text(
            json.dumps(
                _generic_web_deploy_recovery_evidence(
                    proposed_action="hold_unknown",
                    provider_outcome="unknown",
                    retry_safe=False,
                )
            ),
            encoding="utf-8",
        )
        calls: list[dict[str, Any]] = []
        output = io.StringIO()

        with (
            temporary_attribute(
                write_action,
                "execute_post",
                lambda **kwargs: calls.append(kwargs) or 0,
            ),
            redirect_stdout(output),
        ):
            status = write_action.main(
                [
                    "generic-web-deploy-recovery-apply",
                    "--payload-file",
                    str(payload_path),
                    "--idempotency-key",
                    "original-deploy-key-1",
                    "--reviewed-dry-run",
                    "--expected-recovery-digest",
                    "a" * 64,
                    "--dry-run-evidence-file",
                    str(evidence_path),
                ]
            )

    assert status == 2
    assert calls == []
    emitted = json.loads(output.getvalue())
    assert emitted["warnings"][0]["code"] == "reviewed_dry_run_not_apply_eligible"


def test_generic_web_deploy_recovery_projection_fails_closed_on_extra_fields() -> None:
    try:
        write_action.summarize_success(
            operation="generic-web-deploy-recovery-apply",
            request={"mode": "apply", "payload_source": "private_file"},
            provider_payload={
                "status": "ok",
                "trace_id": "launchplane_req_recovery",
                "result": {
                    "status": "applied",
                    "mode": "apply",
                    "recovery_digest": "a" * 64,
                    "unexpected_private_field": "must not pass",
                },
            },
        )
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == "unsafe_response_shape"
    else:
        raise AssertionError("expected extra field to fail closed")


def test_generic_web_deploy_recovery_apply_unverified_on_projection_failure() -> None:
    output = io.StringIO()
    with temporary_attribute(
        write_action,
        "resolve_settings",
        lambda _args: {
            "service_url": "https://launchplane.example.invalid",
            "token": "operator-token",
            "public_url_hint_sources": [],
        },
    ):
        with temporary_attribute(
            write_action,
            "request_launchplane",
            lambda **_kwargs: {
                "status": "ok",
                "trace_id": "launchplane_req_recovery_unverified",
                "result": {"unexpected": "shape"},
            },
        ):
            with redirect_stdout(output):
                status = getattr(write_action, "execute_post")(
                    args=argparse.Namespace(
                        timeout=3,
                        idempotency_key="original-deploy-key-1",
                    ),
                    operation="generic-web-deploy-recovery-apply",
                    path="/v1/admin/generic-web/deploy-recovery/apply",
                    request={"mode": "apply", "payload_source": "private_file"},
                    body={"schema_version": 1},
                )
    payload = json.loads(output.getvalue())
    assert status == 0
    assert payload["status"] == "accepted_unverified"
    assert payload["warnings"][0]["code"] == "apply_response_unverified"
    assert "reservation" in payload["summary"]["recommendation"]


def main() -> int:
    tests = [
        test_agent_operator_contract_identity_and_provenance_semantics,
        test_agent_operator_contract_rejects_drift_and_unsafe_content,
        test_agent_operator_contract_routes_every_local_consumer,
        test_agent_operator_contract_cli_is_public_safe_and_hermetic,
        test_endpoint_validation_policy,
        test_build_url_and_redirect_policy,
        test_write_helper_validates_cli_env_and_json_url_sources,
        test_context_helper_validates_env_and_json_url_sources,
        test_success_projection_preserves_contracts,
        test_preview_feedback_remediation_body_and_projection,
        test_change_impact_policy_body_and_projection,
        test_change_impact_policy_apply_requires_review_idempotency_and_reason,
        test_change_impact_policy_payload_rejects_repo_local_files,
        test_change_impact_policy_projection_fails_closed_on_extra_fields,
        test_change_impact_policy_cli_dispatches_exact_route_and_modes,
        test_change_impact_policy_read_projection_is_bounded,
        test_authz_activation_preflight_projection_is_bounded,
        test_authz_activation_preflight_uses_admin_token_and_exact_route,
        test_authz_activation_preflight_errors_are_read_only,
        test_change_impact_apply_success_projection_failure_is_unverified,
        test_invalid_private_payload_does_not_expose_path,
        test_product_config_projection_accepts_context_scoped_runtime_environment,
        test_optional_public_identifier_rejects_null_values,
        test_runtime_environment_projection_enforces_scope_identity,
        test_current_launchplane_service_response_shapes,
        test_success_projection_fails_closed_on_secret_bearing_payloads,
        test_summaries_and_trace_ids_fail_closed_on_secret_values,
        test_denied_recommendation_escalates_without_borrowing_ci_authority,
        test_context_projection_contract_and_secret_shape,
        test_current_agent_context_service_shape,
        test_request_helpers_use_shared_safe_urlopen,
        test_settings_diagnostic_validates_sources_without_printing_values,
        test_generic_web_deploy_recovery_body_and_projection,
        test_generic_web_deploy_recovery_apply_requires_review_idempotency_and_reason,
        test_generic_web_deploy_recovery_apply_requires_apply_eligible_evidence,
        test_generic_web_deploy_recovery_payload_rejects_repo_local_files,
        test_generic_web_deploy_recovery_cli_dispatches_exact_routes,
        test_generic_web_deploy_recovery_cli_refuses_ineligible_evidence_without_http,
        test_generic_web_deploy_recovery_projection_fails_closed_on_extra_fields,
        test_generic_web_deploy_recovery_apply_unverified_on_projection_failure,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
