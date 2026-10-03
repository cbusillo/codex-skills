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
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import Mock, patch


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
owner_review: Any = load_module("launchplane-owner-review.py", "launchplane_owner_review")


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
    assert contract.internal_helper_path("merge-train-policy-targets-read") == (
        "/v1/work-graph/merge-train/policy-targets"
    )
    assert "authz-activation-preflight-read" not in contract.PROJECTED_HELPER_COMMANDS
    assert not hasattr(write_action, "execute_authz_activation_preflight_read")
    context_args = argparse.Namespace(
        repo="example/repo", branch=None, issue=None, pr=None
    )
    assert context_helper.build_context_url(
        "https://launchplane.example.invalid", context_args
    ) == (
        "https://launchplane.example.invalid/v1/agent/context"
        "?repository=example%2Frepo"
    )


def test_repository_inventory_review_evidence_binds_exact_private_payload() -> None:
    inventory_digest = "a" * 64
    with TemporaryDirectory() as directory:
        payload_path = Path(directory) / "repository-inventory.json"
        evidence_path = Path(directory) / "repository-inventory-dry-run.json"
        payload = {
            "schema_version": 1,
            "expected_current_record_id": "",
            "record": {
                "schema_version": 1,
                "record_id": "repository-inventory-1001-r1",
                "repository_id": "1001",
                "repository_owner_id": "2001",
                "repository": "example/repository",
                "inventory_state": "tracked",
                "inventory_revision": 1,
                "recorded_at": "2026-08-26T00:00:00Z",
                "source": "operator",
                "reason": "issue-backed inventory",
                "supersedes_record_id": None,
                "inventory_digest": "",
            },
        }
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        idempotency_key = "repository-inventory-example-1"
        idempotency_key_fingerprint = (
            write_action.repository_inventory_idempotency_key_fingerprint(idempotency_key)
        )
        dry_run_args = argparse.Namespace(
            payload_file=str(payload_path),
            idempotency_key=idempotency_key,
        )
        dry_run_body = write_action.repository_inventory_payload_body(
            dry_run_args, mode="dry_run"
        )
        payload_digest = write_action.metadata_review_digest(dry_run_body)
        evidence_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "status": "ok",
                    "provider": "launchplane",
                    "operation": "repository-inventory-dry-run",
                    "request": {
                        "mode": "dry_run",
                        "payload_source": "private_file",
                        "payload_digest": payload_digest,
                        "idempotency_key_fingerprint": idempotency_key_fingerprint,
                    },
                    "result": {
                        "status": "would_apply",
                        "mode": "dry_run",
                        "inventory_revision": 1,
                        "record_id": "repository-inventory-1001-r1",
                        "inventory_digest": inventory_digest,
                        "supersedes_record_id": None,
                        "applied_at": "2026-08-26T00:00:01Z",
                    },
                }
            ),
            encoding="utf-8",
        )
        apply_args = argparse.Namespace(
            payload_file=str(payload_path),
            idempotency_key=idempotency_key,
            reviewed_dry_run=True,
            expected_inventory_digest=inventory_digest,
            dry_run_evidence_file=str(evidence_path),
        )
        apply_body = write_action.repository_inventory_payload_body(
            apply_args, mode="apply"
        )
        assert apply_body["mode"] == "apply"

        apply_args.idempotency_key = "repository-inventory-example-2"
        try:
            write_action.repository_inventory_payload_body(apply_args, mode="apply")
        except ValueError as exc:
            assert str(exc) == "reviewed_dry_run_not_apply_eligible"
        else:
            raise AssertionError("changed idempotency key must invalidate dry-run evidence")
        apply_args.idempotency_key = idempotency_key

        payload["expected_current_record_id"] = "repository-inventory-1001-r0"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        try:
            write_action.repository_inventory_payload_body(apply_args, mode="apply")
        except ValueError as exc:
            assert str(exc) == "reviewed_dry_run_not_apply_eligible"
        else:
            raise AssertionError("changed inventory payload must invalidate dry-run evidence")


def test_repository_inventory_projection_is_bounded_and_fail_closed() -> None:
    record = {
        "schema_version": 1,
        "record_id": "repository-inventory-1001-r1",
        "repository_id": "1001",
        "repository_owner_id": "2001",
        "repository": "example/repository",
        "inventory_state": "tracked",
        "inventory_revision": 1,
        "recorded_at": "2026-08-26T00:00:00Z",
        "source": "operator",
        "reason": "issue-backed inventory",
        "supersedes_record_id": None,
        "inventory_digest": "a" * 64,
    }
    read_payload = write_action.summarize_repository_inventory_read(
        request={"payload_source": "operator_argument"},
        provider_payload={
            "status": "ok",
            "trace_id": "launchplane_req_inventory_read",
            "read_model": {
                "schema_version": 1,
                "status": "available",
                "repository_id": "1001",
                "current_record": record,
                "history_count": 1,
                "generated_at": "2026-08-26T00:00:01Z",
            },
        },
    )
    rendered = json.dumps(read_payload)
    assert "example/repository" not in rendered
    assert "repository_owner_id" not in rendered
    assert "issue-backed inventory" not in rendered
    assert read_payload["result"]["current_record"]["record_id"] == (
        "repository-inventory-1001-r1"
    )

    idempotency_key = "repository-inventory-example-1"
    idempotency_key_fingerprint = (
        write_action.repository_inventory_idempotency_key_fingerprint(idempotency_key)
    )
    assert idempotency_key not in idempotency_key_fingerprint
    assert idempotency_key_fingerprint.startswith("sha256:")
    assert len(idempotency_key_fingerprint) == 71

    provider_payload = {
        "status": "ok",
        "trace_id": "launchplane_req_inventory_apply",
        "result": {
            "schema_version": 1,
            "status": "applied",
            "mode": "apply",
            "repository_id": "1001",
            "inventory_revision": 1,
            "record_id": "repository-inventory-1001-r1",
            "inventory_digest": "a" * 64,
            "supersedes_record_id": None,
            "applied_at": "2026-08-26T00:00:01Z",
        },
    }
    apply_payload = write_action.summarize_success(
        operation="repository-inventory-apply",
        request={
            "mode": "apply",
            "payload_source": "private_file",
            "idempotency_key_fingerprint": idempotency_key_fingerprint,
        },
        provider_payload=provider_payload,
    )
    assert "repository_id" not in apply_payload["result"]
    assert apply_payload["request"]["idempotency_key_fingerprint"] == (
        idempotency_key_fingerprint
    )
    provider_payload["result"]["repository"] = "example/repository"
    try:
        write_action.summarize_success(
            operation="repository-inventory-apply",
            request={"mode": "apply", "payload_source": "private_file"},
            provider_payload=provider_payload,
        )
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == "unsafe_response_shape"
    else:
        raise AssertionError("unexpected inventory response fields must fail closed")


def test_agent_operator_contract_cli_is_public_safe_and_hermetic() -> None:
    result = run_helper("check-agent-operator-contract.py", [])
    assert result["returncode"] == 0
    payload = result["payload"]
    assert payload["status"] == "ok"
    assert payload["summary"]["hermetic_only"] is True
    assert payload["summary"]["upstream_freshness_proven"] is False
    assert "url" not in json.dumps(payload).lower()


def _merge_train_policy_import_payload(
    *,
    mode: str = "dry_run",
    reason: str = "",
    status: str = "active",
    policy_sha256: str = "c" * 64,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "launchplane",
        "mode": mode,
        "reason": reason,
        "record": {
            "schema_version": 1,
            "record_id": f"merge-train-policy-20260829T203000Z-{policy_sha256[:12]}",
            "status": status,
            "source": "operator:private-policy-source",
            "updated_at": "2026-08-29T20:30:00Z",
            "policy_sha256": policy_sha256,
            "policy": {
                "schema_version": 1,
                "policies": [
                    {
                        "repository": "private-owner/private-repository",
                        "base_branch": "main",
                        "github_token": {"env_var": "PRIVATE_GITHUB_TOKEN"},
                    }
                ],
            },
        },
    }


def _merge_train_policy_targets_payload(
    *, policy_sha256: str = "a" * 64
) -> dict[str, object]:
    return {
        "status": "ok",
        "trace_id": "launchplane_req_policy_targets",
        "policy": {
            "record_id": f"merge-train-policy-current-{policy_sha256[:12]}",
            "updated_at": "2026-08-29T20:00:00Z",
            "policy_sha256": policy_sha256,
        },
        "targets": [
            {
                "repository": "private-owner/current-repository",
                "base_branch": "main",
                "policy_key": "private-owner/current-repository:main",
                "scheduler": {
                    "enabled": False,
                    "runner_mode": "controller",
                    "mutate": False,
                },
                "service_authz": {
                    "action": "merge_train.run_once",
                    "product": "launchplane",
                    "context": "launchplane",
                },
            }
        ],
    }


def _merge_train_policy_targets_projection(
    *, policy_sha256: str = "a" * 64
) -> dict[str, object]:
    return {
        "record_id": f"merge-train-policy-current-{policy_sha256[:12]}",
        "updated_at": "2026-08-29T20:00:00Z",
        "policy_sha256": policy_sha256,
        "target_count": 1,
        "trace_id": "launchplane_req_policy_targets",
    }


def _merge_train_policy_import_response(
    *,
    mode: str = "dry_run",
    status: str = "active",
    policy_sha256: str = "c" * 64,
    replayed: bool | None = None,
) -> dict[str, object]:
    request_payload = _merge_train_policy_import_payload(
        mode=mode,
        reason="Approved policy import." if mode == "apply" else "",
        status=status,
        policy_sha256=policy_sha256,
    )
    record = request_payload["record"]
    assert isinstance(record, dict)
    response: dict[str, object] = {
        "status": "accepted",
        "trace_id": "launchplane_req_policy_import",
        "records": {},
        "result": {
            "mode": mode,
            "record": {
                "record_id": record["record_id"],
                "status": status,
                "source": record["source"],
                "updated_at": record["updated_at"],
                "policy_sha256": policy_sha256,
                "repository_count": 1,
                "policy_keys": ["private-owner/private-repository:main"],
            },
        },
    }
    if replayed is not None:
        response["replayed"] = replayed
    return response


def _merge_train_policy_dry_run_evidence() -> dict[str, object]:
    return write_action.summarize_merge_train_policy_import_success(
        operation="merge-train-policy-import-dry-run",
        request={
            "mode": "dry_run",
            "payload_source": "private_file",
            "expected_current_policy_sha256": "a" * 64,
            "candidate_policy_sha256": "c" * 64,
        },
        current_policy=_merge_train_policy_targets_projection(),
        provider_payload=_merge_train_policy_import_response(),
        expected_record_id=f"merge-train-policy-20260829T203000Z-{'c' * 12}",
        expected_policy_sha256="c" * 64,
    )


def test_merge_train_policy_import_body_projection_and_redaction() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "private-merge-train-policy.json"
        payload_path.write_text(
            json.dumps(_merge_train_policy_import_payload()), encoding="utf-8"
        )
        body, current_digest, candidate_digest = (
            write_action.merge_train_policy_import_body(
                argparse.Namespace(
                    payload_file=str(payload_path),
                    expected_current_policy_digest="a" * 64,
                    idempotency_key="",
                ),
                mode="dry_run",
            )
        )
    assert body["mode"] == "dry_run"
    assert current_digest == "a" * 64
    assert candidate_digest == "c" * 64

    evidence = _merge_train_policy_dry_run_evidence()
    evidence_result = evidence["result"]
    assert isinstance(evidence_result, dict)
    current_policy = evidence_result["current_policy"]
    candidate = evidence_result["candidate"]
    assert isinstance(current_policy, dict)
    assert isinstance(candidate, dict)
    assert current_policy["target_count"] == 1
    assert candidate["target_count"] == 1
    rendered = json.dumps(evidence)
    for private_value in (
        "private-owner/private-repository",
        "private-owner/current-repository",
        "operator:private-policy-source",
        "PRIVATE_GITHUB_TOKEN",
        str(payload_path),
    ):
        assert private_value not in rendered

    malformed = _merge_train_policy_import_response()
    malformed_result = malformed["result"]
    assert isinstance(malformed_result, dict)
    malformed_record = malformed_result["record"]
    assert isinstance(malformed_record, dict)
    malformed_record["unexpected"] = "must-not-pass"
    try:
        write_action.summarize_merge_train_policy_import_success(
            operation="merge-train-policy-import-dry-run",
            request={"mode": "dry_run"},
            current_policy=_merge_train_policy_targets_projection(),
            provider_payload=malformed,
            expected_record_id=f"merge-train-policy-20260829T203000Z-{'c' * 12}",
            expected_policy_sha256="c" * 64,
        )
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == "unsafe_response_shape"
    else:
        raise AssertionError("expected extra merge-train import response field to fail closed")


def test_merge_train_policy_import_apply_requires_bound_evidence() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "private-merge-train-policy-apply.json"
        evidence_path = Path(directory) / "private-merge-train-policy-dry-run.json"
        payload_path.write_text(
            json.dumps(
                _merge_train_policy_import_payload(
                    mode="apply", reason="Approved policy import."
                )
            ),
            encoding="utf-8",
        )
        evidence_path.write_text(
            json.dumps(_merge_train_policy_dry_run_evidence()), encoding="utf-8"
        )
        args = argparse.Namespace(
            payload_file=str(payload_path),
            expected_current_policy_digest="a" * 64,
            expected_new_policy_digest="c" * 64,
            idempotency_key="merge-train-policy-import-example",
            reviewed_dry_run=True,
            dry_run_evidence_file=str(evidence_path),
        )
        body, current_digest, candidate_digest = (
            write_action.merge_train_policy_import_body(args, mode="apply")
        )
        assert body["mode"] == "apply"
        assert current_digest == "a" * 64
        assert candidate_digest == "c" * 64

        for attribute, value, expected_code in (
            ("idempotency_key", "", "idempotency_key_required"),
            ("reviewed_dry_run", False, "reviewed_dry_run_required"),
            ("expected_new_policy_digest", "d" * 64, "new_policy_digest_mismatch"),
        ):
            invalid_args = copy.copy(args)
            setattr(invalid_args, attribute, value)
            try:
                write_action.merge_train_policy_import_body(
                    invalid_args, mode="apply"
                )
            except ValueError as exc:
                assert str(exc) == expected_code
            else:
                raise AssertionError(f"expected {expected_code}")

        mismatched_evidence = _merge_train_policy_dry_run_evidence()
        mismatched_result = mismatched_evidence["result"]
        assert isinstance(mismatched_result, dict)
        mismatched_current_policy = mismatched_result["current_policy"]
        assert isinstance(mismatched_current_policy, dict)
        mismatched_current_policy["policy_sha256"] = "b" * 64
        evidence_path.write_text(json.dumps(mismatched_evidence), encoding="utf-8")
        try:
            write_action.merge_train_policy_import_body(args, mode="apply")
        except ValueError as exc:
            assert str(exc) == "reviewed_dry_run_not_apply_eligible"
        else:
            raise AssertionError("expected mismatched policy evidence to fail closed")


def test_merge_train_policy_import_preflight_blocks_stale_policy_before_post() -> None:
    calls: list[str] = []
    output = io.StringIO()
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "private-merge-train-policy.json"
        payload_path.write_text(
            json.dumps(_merge_train_policy_import_payload()), encoding="utf-8"
        )

        def fake_read(**kwargs: Any) -> dict[str, object]:
            calls.append(str(kwargs["path"]))
            return _merge_train_policy_targets_payload(policy_sha256="b" * 64)

        def fail_post(**_kwargs: Any) -> dict[str, object]:
            raise AssertionError("policy import POST must not run after stale preflight")

        with temporary_attribute(
            write_action,
            "resolve_settings",
            lambda _args: {
                "service_url": "https://launchplane.example.invalid",
                "token": "operator-token",
                "public_url_hint_sources": "",
            },
        ):
            with temporary_attribute(write_action, "request_launchplane_read", fake_read):
                with temporary_attribute(write_action, "request_launchplane", fail_post):
                    with redirect_stdout(output):
                        status = write_action.main(
                            [
                                "merge-train-policy-import-dry-run",
                                "--payload-file",
                                str(payload_path),
                                "--expected-current-policy-digest",
                                "a" * 64,
                            ]
                        )
    assert status == 1
    assert calls == ["/v1/work-graph/merge-train/policy-targets"]
    result = json.loads(output.getvalue())
    assert result["status"] == "stale"
    assert result["summary"]["error_code"] == "current_policy_digest_mismatch"


def test_merge_train_policy_import_uses_exact_read_and_write_routes() -> None:
    calls: list[tuple[str, str]] = []
    output = io.StringIO()
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "private-merge-train-policy.json"
        payload_path.write_text(
            json.dumps(_merge_train_policy_import_payload()), encoding="utf-8"
        )

        def fake_read(**kwargs: Any) -> dict[str, object]:
            calls.append(("GET", str(kwargs["path"])))
            payload = _merge_train_policy_targets_payload()
            policy = payload["policy"]
            assert isinstance(policy, dict)
            policy["updated_at"] = "2026-09-14T17:13:11.787617Z"
            return payload

        def fake_post(**kwargs: Any) -> dict[str, object]:
            calls.append(("POST", str(kwargs["path"])))
            assert kwargs["idempotency_key"] == ""
            return _merge_train_policy_import_response(replayed=False)

        with temporary_attribute(
            write_action,
            "resolve_settings",
            lambda _args: {
                "service_url": "https://launchplane.example.invalid",
                "token": "operator-token",
                "public_url_hint_sources": "",
            },
        ):
            with temporary_attribute(write_action, "request_launchplane_read", fake_read):
                with temporary_attribute(write_action, "request_launchplane", fake_post):
                    with redirect_stdout(output):
                        status = write_action.main(
                            [
                                "merge-train-policy-import-dry-run",
                                "--payload-file",
                                str(payload_path),
                                "--expected-current-policy-digest",
                                "a" * 64,
                            ]
                        )
        evidence_path = Path(directory) / "private-merge-train-policy-evidence.json"
        evidence_path.write_text(output.getvalue(), encoding="utf-8")
        payload_path.write_text(
            json.dumps(
                _merge_train_policy_import_payload(
                    mode="apply", reason="Approved policy import."
                )
            ),
            encoding="utf-8",
        )
        apply_body, apply_current_digest, apply_candidate_digest = (
            write_action.merge_train_policy_import_body(
                argparse.Namespace(
                    payload_file=str(payload_path),
                    expected_current_policy_digest="a" * 64,
                    expected_new_policy_digest="c" * 64,
                    idempotency_key="merge-train-policy-import-example",
                    reviewed_dry_run=True,
                    dry_run_evidence_file=str(evidence_path),
                ),
                mode="apply",
            )
        )
    assert status == 0
    assert calls == [
        ("GET", "/v1/work-graph/merge-train/policy-targets"),
        ("POST", "/v1/merge-train/policies/import"),
    ]
    result = json.loads(output.getvalue())
    assert result["status"] == "accepted"
    assert result["result"]["current_policy"]["updated_at"] == "2026-09-14T17:13:11.787617Z"
    assert result["result"]["candidate"]["policy_sha256"] == "c" * 64
    assert result["result"]["candidate"]["replayed"] is False
    assert apply_body["mode"] == "apply"
    assert apply_current_digest == "a" * 64
    assert apply_candidate_digest == "c" * 64


def test_merge_train_policy_import_apply_handles_ambiguous_success_and_transport() -> None:
    body = _merge_train_policy_import_payload(
        mode="apply", reason="Approved policy import."
    )
    args = argparse.Namespace(timeout=3, idempotency_key="policy-import-example")
    request = {
        "mode": "apply",
        "payload_source": "private_file",
        "expected_current_policy_sha256": "a" * 64,
        "candidate_policy_sha256": "c" * 64,
    }
    settings = {
        "service_url": "https://launchplane.example.invalid",
        "token": "operator-token",
        "public_url_hint_sources": "",
    }

    for provider_result, expected_status, expected_code, expected_returncode in (
        (
            {"status": "accepted", "trace_id": "launchplane_req_unverified"},
            "accepted_unverified",
            "apply_response_unverified",
            0,
        ),
        (
            urllib.error.URLError("connection reset"),
            "outcome_unknown",
            "apply_outcome_unknown",
            1,
        ),
        (
            safety.LaunchplaneSafetyError("unsafe_redirect"),
            "outcome_unknown",
            "apply_outcome_unknown",
            1,
        ),
    ):
        output = io.StringIO()

        def fake_post(**_kwargs: Any) -> dict[str, object]:
            if isinstance(provider_result, Exception):
                raise provider_result
            return provider_result

        with temporary_attribute(write_action, "resolve_settings", lambda _args: settings):
            with temporary_attribute(
                write_action,
                "request_launchplane_read",
                lambda **_kwargs: _merge_train_policy_targets_payload(),
            ):
                with temporary_attribute(write_action, "request_launchplane", fake_post):
                    with redirect_stdout(output):
                        status = write_action.execute_merge_train_policy_import(
                            args=args,
                            operation="merge-train-policy-import-apply",
                            request=request,
                            body=body,
                            expected_current_policy_digest="a" * 64,
                            expected_candidate_policy_digest="c" * 64,
                        )
        payload = json.loads(output.getvalue())
        assert status == expected_returncode
        assert payload["status"] == expected_status
        assert payload["warnings"][0]["code"] == expected_code


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

    read_calls: list[dict[str, Any]] = []

    def fake_execute_read(**kwargs: Any) -> int:
        read_calls.append(kwargs)
        return 0

    with temporary_attribute(
        write_action, "execute_change_impact_policy_read", fake_execute_read
    ):
        assert (
            write_action.main(
                ["change-impact-policy-read", "--repository-id", "123"]
            )
            == 0
        )
    assert read_calls[0]["request"] == {"payload_source": "operator_argument"}


def current_change_impact_policy_read_response() -> dict[str, Any]:
    """Pre-audit service shape retained to exercise legacy compatibility."""
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
    return {
        "status": "ok",
        "trace_id": "launchplane_req_change_impact_read",
        "read_model": {
            "schema_version": 1,
            "repository_id": "123",
            "current_policy": record,
            "policy_history_count": 1,
        },
    }


def test_change_impact_policy_read_projection_is_bounded() -> None:
    response = current_change_impact_policy_read_response()
    payload = write_action.summarize_change_impact_policy_read(
        request={"payload_source": "operator_argument"}, provider_payload=response,
    )
    assert set(payload["result"]) == {"policy_history_count", "current_policy"}
    assert set(payload["result"]["current_policy"]) == {
        "record_id", "policy_digest", "policy_revision", "status", "effective_at",
    }
    assert payload["result"]["current_policy"]["policy_digest"] == "a" * 64
    rendered = json.dumps(payload)
    for private_value in ("repository_id", "example/repo", "private-component", "private/path", "456"):
        assert private_value not in rendered

    empty_payload = write_action.summarize_change_impact_policy_read(
        request={"payload_source": "operator_argument"},
        provider_payload={
            "status": "ok",
            "trace_id": "launchplane_req_change_impact_empty",
            "read_model": {
                "schema_version": 1,
                "repository_id": "123",
                "current_policy": None,
                "policy_history_count": 0,
            },
        },
    )
    assert empty_payload["result"]["current_policy"] is None
    assert empty_payload["result"]["policy_history_count"] == 0


def audited_policy_read_response(*, attributed: bool = True) -> dict[str, Any]:
    response = current_change_impact_policy_read_response()
    model = response["read_model"]
    record = model["current_policy"]
    record["classification_model"] = None
    record["component_rules"][0].update(product_impact=None, governance_impact=None, generated_by=None)
    model["attribution_status"] = "attributed" if attributed else "legacy_unattributed"
    model["audit"] = None
    if attributed:
        model["audit"] = {
            "schema_version": 1, "record_id": record["record_id"], "policy_digest": record["policy_digest"],
            "actor_kind": "github_actions", "actor_subject": "private-audit-subject",
            "workflow_identity": {
                "repository": "private-owner/private-repo", "repository_id": "987", "repository_owner_id": "654",
                "workflow_ref": "private-workflow-ref", "job_workflow_ref": "private-job-workflow-ref",
                "ref": "private-git-ref", "sha": "b" * 40,
            },
            "trace_id": "launchplane_req_private_audit", "recorded_at": "2026-09-05T22:00:00.123456Z",
        }
    return response


def test_change_impact_current_policy_responses_preserve_bounded_attribution() -> None:
    response = audited_policy_read_response()
    model = response["read_model"]
    payload = write_action.summarize_change_impact_policy_read(request={}, provider_payload=response)
    expected_audit = {
        "record_id": model["audit"]["record_id"], "policy_digest": "a" * 64,
        "actor_kind": "github_actions", "recorded_at": "2026-09-05T22:00:00Z",
    }
    assert payload["result"]["audit"] == expected_audit
    assert payload["result"]["attribution_status"] == "attributed"
    for timestamp in ("2026-09-05T22:00:00Z", "2026-09-05T18:00:00.123456-04:00", "2026-09-06T00:00:00.1+02:00"):
        model["audit"]["recorded_at"] = timestamp
        projected = write_action.summarize_change_impact_policy_read(request={}, provider_payload=response)
        assert projected["result"]["audit"] == expected_audit
        assert model["audit"]["recorded_at"] == timestamp
    rendered = json.dumps(payload)
    for private in ("private-audit-subject", "launchplane_req_private_audit", "private-owner", "private-workflow-ref", "private-job-workflow-ref", "private-git-ref"):
        assert private not in rendered
    for kind in ("local_admin", "local_operator"):
        model["audit"].update(actor_kind=kind, workflow_identity=None)
        projected = write_action.summarize_change_impact_policy_read(request={}, provider_payload=response)
        assert projected["result"]["audit"] == {**expected_audit, "actor_kind": kind}
    legacy = audited_policy_read_response(attributed=False)
    projected = write_action.summarize_change_impact_policy_read(request={}, provider_payload=legacy)
    assert projected["result"]["audit"] is None
    assert projected["result"]["attribution_status"] == "legacy_unattributed"
    legacy["read_model"].update(current_policy=None, policy_history_count=0, attribution_status="attribution_unavailable")
    assert write_action.summarize_change_impact_policy_read(request={}, provider_payload=legacy)["result"] == {
        "current_policy": None, "policy_history_count": 0, "attribution_status": "attribution_unavailable", "audit": None,
    }
    for status in ("would_apply", "would_replay", "applied", "replayed"):
        result = {"schema_version": 1, "status": status, "record": model["current_policy"], "audit": model["audit"], "attribution_status": "attributed"}
        if status.startswith("would_"):
            result.update(audit=None, attribution_status="not_applied")
        projected = write_action._project_change_impact_policy_result(result)
        assert projected["attribution_status"] == result["attribution_status"]
        assert (projected["audit"] is None) == status.startswith("would_")
    result.update(status="replayed", audit=None, attribution_status="legacy_unattributed")
    assert write_action._project_change_impact_policy_result(result)["audit"] is None


def test_change_impact_current_policy_rejects_invalid_nested_metadata() -> None:
    changes = [
        (("attribution_status",), "unexpected"), (("attribution_status",), None),
        (("audit",), None), (("current_policy",), None),
        (("audit", "record_id"), "different-record"), (("audit", "policy_digest"), "c" * 64),
        (("audit", "schema_version"), True), (("audit", "actor_kind"), "human"),
        (("audit", "actor_subject"), {}), (("audit", "actor_subject"), " " * 10),
        (("audit", "trace_id"), "x" * 257), (("audit", "recorded_at"), "not-a-date"),
        (("audit", "recorded_at"), "2026-09-05T22:00:00.123456"),
        (("audit", "recorded_at"), "2026-02-30T22:00:00Z"),
        (("audit", "recorded_at"), "2026-09-05T22:00:00+00:60"),
        (("audit", "recorded_at"), "2026-09-05T22:00:00+24:00"),
        (("audit", "recorded_at"), None),
        (("audit", "unknown_private_field"), "private-value"),
        (("audit", "workflow_identity", "unknown_private_field"), "private-value"),
        (("audit", "workflow_identity", "repository"), []),
        (("audit", "workflow_identity"), None),
        (("current_policy", "classification_model"), {}),
        (("current_policy", "classification_model"), "v3"),
    ]
    for path, value in changes:
        response = audited_policy_read_response()
        target = response["read_model"]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        try:
            write_action.summarize_change_impact_policy_read(request={}, provider_payload=response)
        except safety.LaunchplaneSafetyError:
            pass
        else:
            raise AssertionError(f"expected rejection for {path}")


def test_change_impact_policy_v2_response_fields_are_validated_and_redacted() -> None:
    response = audited_policy_read_response(attributed=False)
    record = response["read_model"]["current_policy"]
    record["classification_model"] = "v2"
    rule = record["component_rules"][0]
    for additions in ({"product_impact": "declared_none"}, {"governance_impact": True}, {"generated_by": ["private-generator"]}):
        rule.update(product_impact=None, governance_impact=None, generated_by=None)
        rule.update(additions)
        payload = write_action.summarize_change_impact_policy_read(request={}, provider_payload=response)
        assert set(payload["result"]["current_policy"]) == {"record_id", "policy_digest", "policy_revision", "status", "effective_at"}
        assert "private-generator" not in json.dumps(payload)
    for additions in ({"product_impact": "unknown"}, {"governance_impact": "yes"}, {"generated_by": []}, {"generated_by": ["same", "same"]}, {"generated_by": [{}]}, {"generated_by": ["generator"], "product_impact": "declared_none"}, {"unknown_field": None}):
        invalid = copy.deepcopy(response)
        invalid["read_model"]["current_policy"]["component_rules"][0].update(additions)
        try:
            write_action.summarize_change_impact_policy_read(request={}, provider_payload=invalid)
        except safety.LaunchplaneSafetyError:
            pass
        else:
            raise AssertionError(f"expected rejection for {additions}")
    record["classification_model"] = None
    try:
        write_action.summarize_change_impact_policy_read(request={}, provider_payload=response)
    except safety.LaunchplaneSafetyError:
        pass
    else:
        raise AssertionError("v2 rule fields require a v2 policy")


def test_change_impact_policy_read_optional_legacy_metadata() -> None:
    response = current_change_impact_policy_read_response()
    flags = {"mode": "shadow", "authoritative": False, "enforcement_effect": "none"}
    response["read_model"].update(flags)
    payload = write_action.summarize_change_impact_policy_read(request={}, provider_payload=response)
    for name, value in flags.items():
        assert payload["result"][name] == value
    response["read_model"].update({name: None for name in flags})
    payload = write_action.summarize_change_impact_policy_read(request={}, provider_payload=response)
    assert set(payload["result"]) == {"policy_history_count", "current_policy"}


def test_change_impact_policy_read_rejects_malformed_metadata() -> None:
    invalid_values = [
        ("policy_history_count", value) for value in (None, True, -1, "1")
    ] + [
        ("authoritative", value) for value in ("true", 1)
    ] + [
        ("mode", []), ("enforcement_effect", {}),
        ("current_policy", []), ("unexpected_private_field", "private-value"),
    ]
    for field, value in invalid_values:
        response = current_change_impact_policy_read_response()
        response["read_model"][field] = value
        try:
            write_action.summarize_change_impact_policy_read(request={}, provider_payload=response)
        except safety.LaunchplaneSafetyError:
            pass
        else:
            raise AssertionError(f"accepted invalid policy read field: {field}")
    for field, value in (
        ("policy_digest", "a" * 63), ("policy_digest", "g" * 64),
        ("status", "unknown"), ("policy_revision", 0), ("policy_revision", True),
    ):
        response = current_change_impact_policy_read_response()
        response["read_model"]["current_policy"][field] = value
        try:
            write_action.summarize_change_impact_policy_read(request={}, provider_payload=response)
        except safety.LaunchplaneSafetyError:
            pass
        else:
            raise AssertionError(f"accepted invalid current policy field: {field}")
    for response in ({"status": "ok"}, {"read_model": None}, {
        **current_change_impact_policy_read_response(), "unexpected_private_field": "private-value",
    }):
        try:
            write_action.summarize_change_impact_policy_read(request={}, provider_payload=response)
        except safety.LaunchplaneSafetyError:
            pass
        else:
            raise AssertionError("accepted malformed policy response envelope")


def test_change_impact_policy_read_execution_projects_current_service_response() -> None:
    response = audited_policy_read_response()
    for has_policy in (True, False):
        if not has_policy:
            response["read_model"].update(
                current_policy=None, policy_history_count=0,
                audit=None, attribution_status="attribution_unavailable",
            )
        output = io.StringIO()
        read = Mock(return_value=response)
        with temporary_attribute(write_action, "prepare_operator_settings", lambda **_kwargs: {
            "service_url": "https://launchplane.example.invalid", "token": "test-token",
        }), temporary_attribute(write_action, "request_launchplane_read", read):
            with redirect_stdout(output):
                exit_code = write_action.execute_change_impact_policy_read(
                    args=argparse.Namespace(repository_id="123", timeout=3), request={},
                )
        assert exit_code == 0
        read.assert_called_once_with(
            service_url="https://launchplane.example.invalid", path="/v1/change-impact/policy",
            settings={"service_url": "https://launchplane.example.invalid", "token": "test-token"},
            query={"repository_id": "123"}, timeout=3,
        )
        payload = json.loads(output.getvalue())
        assert payload["status"] == "ok"
        assert (payload["result"]["current_policy"] is not None) == has_policy
        assert "private/path" not in output.getvalue()
        assert "private-audit-subject" not in output.getvalue()
        assert "launchplane_req_private_audit" not in output.getvalue()


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


def _queue_refusal_response() -> dict[str, Any]:
    return {
        "status": "accepted",
        "trace_id": "launchplane_req_queue",
        "records": {},
        "result": {
            "repository": "example/repo",
            "base_branch": "main",
            "mode": "dry-run",
            "controller_action": "idle",
            "dry_run_result": {
                "mode": "dry-run",
                "queue_order": [],
                "selected_pr": None,
                "intended_next_action": "idle",
                "next_action_detail": "No eligible pull requests are queued.",
                "queue": [
                    {
                        "number": number,
                        "head_sha": str(number % 10) * 40,
                        "title": "A private pull request title",
                        "url": f"https://github.com/example/repo/pull/{number}",
                        "labels": ["ready-to-merge", "private-label"],
                        "created_at": "2026-09-25T08:00:00Z",
                        "actor_role": "unknown",
                        "mergeable": "mergeable",
                        "required_checks_status": "pass",
                        "branch_update_required": False,
                        "eligible": False,
                        "ineligible_reasons": ["actor role is not allowed to enqueue"],
                    }
                    for number in (42, 43)
                ],
            },
        },
    }


def _summarize_queue_response(response: dict[str, Any]) -> dict[str, Any]:
    return write_action.summarize_success(
        operation="merge-train-controller-run-once",
        request={"repository": "example/repo", "base_branch": "main", "mutate": False},
        provider_payload=response,
    )


def _run_controller_response(response: object, *, mutate: bool = False, timeout: float | None = None) -> tuple[int, dict[str, Any]]:
    argv = ["merge-train-controller-run-once", "--repo", "example/repo"]
    if mutate:
        argv += ["--mutate", "--idempotency-key", "controller-pass-one"]
    if timeout is not None:
        argv = ["--timeout", str(timeout), *argv]
    output = io.StringIO()
    def fake_post(**kwargs: Any) -> dict[str, Any]:
        if isinstance(response, Exception):
            raise response
        # The observed controller pass needs 19 seconds, even without mutation.
        if timeout is None:
            assert kwargs["timeout"] > 19
        else:
            assert kwargs["timeout"] == timeout
        return cast(dict[str, Any], response)
    post = Mock(side_effect=fake_post)
    with temporary_attribute(write_action, "prepare_operator_settings", lambda **_kwargs: {
        "service_url": "https://launchplane.example.invalid", "token": "fixture-only",
    }), temporary_attribute(write_action, "request_launchplane", post), redirect_stdout(output):
        status = write_action.main(argv)
    assert post.call_count == 1
    return status, json.loads(output.getvalue())


def test_controller_timeout_covers_slow_dry_run_and_mutation_and_keeps_override() -> None:
    response = _queue_refusal_response()
    for mutate in (False, True):
        status, payload = _run_controller_response(response, mutate=mutate)
        assert status == 0
        assert payload["summary"]["trace_id"] == response["trace_id"]
        status, _ = _run_controller_response(response, mutate=mutate, timeout=7.5)
        assert status == 0
    controller = write_action.parse_args(["merge-train-controller-run-once", "--repo", "example/repo"])
    ordinary = write_action.parse_args(["product-profile-read", "--product", "example-product"])
    assert ordinary.timeout < controller.timeout


def test_controller_block_and_reconciliation_preserve_durable_diagnostics() -> None:
    result = {
        "controller_action": "block", "mode": "blocked",
        "blocking_reason": {"code": "merge_readiness_not_ready", "message": "Required checks failed."},
        "merge_readiness": {"state": "blocked_checks", "reason_codes": ["checks_failed"]},
        "merge_train_batch_landing_plan_record_id": "landing-plan-example",
        "landing_plan": {"record_id": "landing-plan-example"},
    }
    response = {"status": "accepted", "trace_id": "launchplane_req_block", "records": {}, "result": result}
    status, payload = _run_controller_response(response, mutate=True)
    assert status == 0
    assert payload["status"] == "accepted"
    assert payload["summary"]["controller_action"] == result["controller_action"]
    assert payload["summary"]["error_code"] == result["blocking_reason"]["code"]
    assert payload["result"]["merge_readiness"] == result["merge_readiness"]
    assert payload["result"]["merge_train_batch_landing_plan_record_id"] == result["merge_train_batch_landing_plan_record_id"]
    assert payload["result"]["landing_plan"] == result["landing_plan"]
    lease = {
        "controller_action": "resume_reconciliation", "controller_reconciliation_status": "required",
        "active_action": "land_batch", "active_phase": "merge_batch_entries",
        "active_record_id": "landing-plan-example",
    }
    response["result"] = lease
    status, payload = _run_controller_response(response)
    assert status == 0
    assert payload["result"] == lease
    lease["active_record_id"] = ""
    assert _run_controller_response(response)[1]["result"]["active_record_id"] is None


def test_controller_client_timeout_is_not_a_service_outage_and_never_retries() -> None:
    for error in (TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out"))):
        for mutate in (False, True):
            status, payload = _run_controller_response(error, mutate=mutate, timeout=7.5)
            assert status == 1
            assert payload["summary"]["error_code"] == "client_timeout"
            assert payload["summary"]["timeout_seconds"] == 7.5
            assert payload["warnings"][0]["code"] == "client_timeout"
            assert "7.5 s" in payload["warnings"][0]["message"]
            if mutate:
                assert "may have completed" in payload["summary"]["recommendation"]
    status, payload = _run_controller_response(urllib.error.URLError("connection refused"))
    assert status == 1
    assert payload["warnings"][0]["code"] == "provider_unavailable"


def test_controller_rejected_response_keeps_only_safe_trace_and_code() -> None:
    for code_source in ("top", "error", "blocking_reason"):
        response: dict[str, Any] = {
            "status": "accepted", "trace_id": "launchplane_req_diagnostic", "records": {},
            "result": {"controller_action": "block", "token": "private-fixture-value"},
        }
        error = {"code": "merge_readiness_not_ready", "message": "Bearer private-fixture-value"}
        if code_source == "top":
            response["error"] = error
        else:
            response["result"][code_source] = error
        status, payload = _run_controller_response(response, mutate=True)
        assert status == 1
        assert payload["status"] == "invalid"
        assert payload["result"] == {}
        assert payload["summary"]["trace_id"] == response["trace_id"]
        assert payload["summary"]["error_code"] == error["code"]
        assert "private-fixture-value" not in json.dumps(payload)
        status, dry_run_payload = _run_controller_response(response)
        assert status == 1
        assert "may have completed" not in dry_run_payload["summary"]["recommendation"]
    for trace, code in (
        ("launchplane_req_diagnostic", "ghp_privatefixture"),
        ("Bearer private-fixture-value", "merge_readiness_not_ready"),
        ({}, {}),
    ):
        response = {"trace_id": trace, "error": {"code": code}, "result": {"unexpected": True}}
        status, payload = _run_controller_response(response)
        assert status == 1
        assert ("trace_id" in payload["summary"]) == (trace == "launchplane_req_diagnostic")
        assert ("error_code" in payload["summary"]) == (code == "merge_readiness_not_ready")


def test_controller_http_error_keeps_safe_identifiers_without_raw_error_text() -> None:
    for http_status, error_code in (
        (403, "merge_readiness_not_ready"), (409, "merge_readiness_not_ready"),
        (502, "merge_readiness_not_ready"), (409, "merge_train_controller_lease_held"),
    ):
        response = {
            "trace_id": "launchplane_req_http", "error": {
                "code": error_code, "message": "Bearer private-fixture-value",
            }, "token": "private-fixture-value",
        }
        error = urllib.error.HTTPError(
            "https://launchplane.example.invalid/controller", http_status, "Rejected", Message(),
            io.BytesIO(json.dumps(response).encode()),
        )
        status, payload = _run_controller_response(error)
        assert status == 1
        assert payload["summary"]["http_status"] == http_status
        assert payload["summary"]["trace_id"] == response["trace_id"]
        assert payload["summary"]["error_code"] == response["error"]["code"]
        assert "private-fixture-value" not in json.dumps(payload)


def test_merge_train_idle_preserves_author_refusal_without_pr_content() -> None:
    result = _summarize_queue_response(_queue_refusal_response())
    dry_run = result["result"]["dry_run_result"]
    assert dry_run["queue_order"] == []
    assert dry_run["selected_pr"] is None
    assert dry_run["intended_next_action"] == "idle"
    assert dry_run["next_action_detail"] == "No eligible pull requests are queued."
    assert [entry["number"] for entry in dry_run["queue"]] == [42, 43]
    for entry in dry_run["queue"]:
        assert entry["actor_role"] == "unknown"
        assert entry["eligible"] is False
        assert entry["ineligible_reasons"] == ["actor role is not allowed to enqueue"]
        assert entry["required_checks_status"] == "pass"
        assert entry["branch_update_required"] is False
        assert entry["head_sha"] == str(entry["number"] % 10) * 40
        assert not {"title", "url", "labels", "created_at"}.intersection(entry)
    assert "private pull request" not in json.dumps(result)
    assert "private-label" not in json.dumps(result)


def test_merge_train_queue_distinguishes_eligible_empty_and_unavailable() -> None:
    response = _queue_refusal_response()
    dry_run = response["result"]["dry_run_result"]
    selected = dry_run["queue"][0]
    selected.update(actor_role="trusted_automation", eligible=True, ineligible_reasons=[])
    dry_run.update(queue_order=[42], selected_pr=selected, intended_next_action="merge")
    response["result"]["controller_action"] = "plan_candidate"
    projected = _summarize_queue_response(response)["result"]["dry_run_result"]
    assert projected["queue_order"] == [42]
    assert projected["selected_pr"]["eligible"] is True
    assert projected["selected_pr"]["actor_role"] == "trusted_automation"
    assert projected["selected_pr"] == projected["queue"][0]
    response["result"]["dry_run_result"] = {
        "mode": "dry-run", "queue": [], "queue_order": [], "selected_pr": None
    }
    assert _summarize_queue_response(response)["result"]["dry_run_result"]["queue"] == []
    response["result"]["dry_run_result"] = {"mode": "dry-run"}
    assert _summarize_queue_response(response)["result"]["dry_run_result"] == {"mode": "dry-run"}


def test_merge_train_queue_rejects_malformed_or_sensitive_evidence() -> None:
    for key, value in (
        ("queue", {}), ("queue_order", [True]), ("selected_pr", "unknown"),
        ("intended_next_action", []),
    ):
        response = _queue_refusal_response()
        response["result"]["dry_run_result"][key] = value
        try:
            _summarize_queue_response(response)
        except safety.LaunchplaneSafetyError:
            pass
        else:
            raise AssertionError(f"expected malformed {key} to fail closed")
    for key, value in (
        ("number", True), ("number", 0), ("head_sha", None), ("actor_role", []),
        ("eligible", "false"), ("branch_update_required", None),
        ("ineligible_reasons", "not allowed"),
        ("ineligible_reasons", ["Bearer " + "private-response-value"]),
        ("token", "private-response-value"),
    ):
        response = _queue_refusal_response()
        response["result"]["dry_run_result"]["queue"][0][key] = value
        try:
            _summarize_queue_response(response)
        except safety.LaunchplaneSafetyError:
            pass
        else:
            raise AssertionError(f"expected unsafe queue field {key} to fail closed")


def test_controller_branch_update_result_reaches_the_caller() -> None:
    # The controller refreshes a behind-base branch itself and says so; rejecting the field hid that pass (#883).
    merge = write_action.summarize_success(
        operation="merge-train-controller-run-once",
        request={"repository": "example/repo", "base_branch": "main", "mutate": True},
        provider_payload={
            "status": "accepted",
            "trace_id": "launchplane_req_update",
            "records": {},
            "result": {
                "repository": "example/repo",
                "base_branch": "main",
                "mode": "update_branch",
                "controller_action": "update_branch",
                "branch_update_result": {
                    "status": "updated",
                    "repository": "example/repo",
                    "base_branch": "main",
                    "pull_request_number": 42,
                    "expected_head_sha": "abc123",
                    "reread_required": True,
                    "detail": "Updated pull request branch from base.",
                },
            },
        },
    )
    assert merge["status"] == "accepted"
    assert merge["result"]["branch_update_result"] == {"status": "updated"}


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

    blocked_merge = write_action.summarize_success(
        operation="merge-train-controller-run-once",
        request={"repository": "example/repo", "base_branch": "main", "mutate": True},
        provider_payload={
            "status": "accepted",
            "trace_id": "launchplane_req_blocked_merge",
            "records": {
                "merge_train_batch_landing_plan_record_id": "landing-plan-example",
            },
            "result": {
                "repository": "example/repo",
                "base_branch": "main",
                "mode": "blocked",
                "controller_action": "block",
                "blocking_reason": {
                    "code": "merge_readiness_not_ready",
                    "message": "Merge readiness is not ready.",
                },
                "merge_readiness": {
                    "state": "blocked_engineering_review",
                    "reason_codes": ["engineering_review_pending"],
                    "owner_states": ["ready"],
                    "technical_checks_state": "ready",
                    "engineering_review_state": "blocked_engineering_review",
                    "policy_state": "ready",
                    "candidate_state": "ready",
                    "fence_state": "ready",
                },
                "structural_provenance": {
                    "status": "recorded_rolling",
                    "reason_codes": ["structural_rolling_chain_recorded"],
                    "effective_base_sha": "a" * 40,
                    "effective_base_tree_sha": "b" * 40,
                    "candidate_sha256": "c" * 64,
                    "landing_plan_sha256": "d" * 64,
                    "provenance_sha256": "e" * 64,
                },
                "landing_plan": {
                    "status": "planned",
                    "candidate_sha": "abc123",
                    "entries": [{"pull_request_number": 42, "status": "planned"}],
                },
            },
        },
    )
    assert blocked_merge["summary"]["controller_action"] == "block"
    assert blocked_merge["summary"]["trace_id"] == "launchplane_req_blocked_merge"
    assert blocked_merge["result"]["structural_provenance"]["status"] == "recorded_rolling"
    assert blocked_merge["result"]["structural_provenance"]["effective_base_sha"] == "a" * 40
    assert blocked_merge["summary"]["recommendation"] == (
        "Stop and report this merge-train state."
    )
    assert blocked_merge["records"] == {
        "merge_train_batch_landing_plan_record_id": "landing-plan-example"
    }
    assert blocked_merge["result"]["blocking_reason"] == {
        "code": "merge_readiness_not_ready",
        "message": "Merge readiness is not ready.",
    }
    assert blocked_merge["result"]["merge_readiness"] == {
        "state": "blocked_engineering_review",
        "reason_codes": ["engineering_review_pending"],
        "owner_states": ["ready"],
        "technical_checks_state": "ready",
        "engineering_review_state": "blocked_engineering_review",
        "policy_state": "ready",
        "candidate_state": "ready",
        "fence_state": "ready",
    }
    assert blocked_merge["result"]["landing_plan"] == {
        "status": "planned",
        "candidate_sha": "abc123",
        "entries_count": 1,
    }

    early_refusal = write_action.summarize_success(
        operation="merge-train-controller-run-once",
        request={"repository": "example/repo", "base_branch": "main", "mutate": True},
        provider_payload={
            "status": "accepted",
            "trace_id": "launchplane_req_lineage_changed",
            "records": {},
            "result": {
                "controller_action": "block",
                "blocking_reason": {
                    "code": "landing_lineage_changed",
                    "message": "Live merge queue changed from the landing-plan lineage.",
                },
                "merge_readiness": None,
                "structural_provenance": None,
            },
        },
    )
    assert early_refusal["summary"]["trace_id"] == "launchplane_req_lineage_changed"
    assert early_refusal["result"]["blocking_reason"]["code"] == "landing_lineage_changed"
    assert early_refusal["result"]["structural_provenance"] is None

    for field, value in (
        (
            "blocking_reason",
            {
                "code": "merge_readiness_not_ready",
                "message": "Merge readiness is not ready.",
                "detail": "unexpected",
            },
        ),
        (
            "merge_readiness",
            {
                "state": "blocked_engineering_review",
                "reason_codes": ["engineering_review_pending"],
                "owner_states": ["ready"],
                "unexpected": "value",
            },
        ),
        ("structural_provenance", {"status": "unknown", "unexpected": "value"}),
    ):
        result = {
            "repository": "example/repo",
            "base_branch": "main",
            "controller_action": "block",
            "blocking_reason": {
                "code": "merge_readiness_not_ready",
                "message": "Merge readiness is not ready.",
            },
            "merge_readiness": None,
            field: value,
        }
        try:
            write_action.summarize_success(
                operation="merge-train-controller-run-once",
                request={
                    "repository": "example/repo",
                    "base_branch": "main",
                    "mutate": True,
                },
                provider_payload={
                    "status": "accepted",
                    "trace_id": "launchplane_req_blocked_merge",
                    "records": {},
                    "result": result,
                },
            )
        except safety.LaunchplaneSafetyError as exc:
            assert exc.code == "unsafe_response_shape"
        else:
            raise AssertionError(f"expected unexpected {field} field to fail closed")

    for field, value in (
        ("state", {}),
        ("reason_codes", [{"unexpected": "shape"}]),
    ):
        try:
            write_action.summarize_success(
                operation="merge-train-controller-run-once",
                request={
                    "repository": "example/repo",
                    "base_branch": "main",
                    "mutate": True,
                },
                provider_payload={
                    "status": "accepted",
                    "trace_id": "launchplane_req_blocked_merge",
                    "records": {},
                    "result": {
                        "repository": "example/repo",
                        "base_branch": "main",
                        "controller_action": "block",
                        "blocking_reason": {
                            "code": "merge_readiness_not_ready",
                            "message": "Merge readiness is not ready.",
                        },
                        "merge_readiness": {field: value},
                    },
                },
            )
        except safety.LaunchplaneSafetyError as exc:
            assert exc.code == "invalid_response"
        else:
            raise AssertionError(f"expected invalid {field} value to fail closed")

    try:
        write_action.summarize_success(
            operation="merge-train-controller-run-once",
            request={
                "repository": "example/repo",
                "base_branch": "main",
                "mutate": True,
            },
            provider_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_blocked_merge",
                "records": {},
                "result": {
                    "repository": "example/repo",
                    "base_branch": "main",
                    "controller_action": "block",
                    "blocking_reason": {
                        "code": {"unexpected": "shape"},
                        "message": "Merge readiness is not ready.",
                    },
                    "merge_readiness": None,
                },
            },
        )
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == "invalid_response"
    else:
        raise AssertionError("expected invalid blocking reason code to fail closed")

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
                    "retired_provider_keys_before": [],
                    "retired_provider_keys_after": ["LEGACY_API_PASSWORD"],
                    "record": {
                        "scope": "instance",
                        "context": "testing",
                        "instance": "example-instance",
                        "updated_at": "2026-07-19T23:00:00Z",
                        "source_label": "product-config-api",
                        "env_keys": ["EXAMPLE_MODE"],
                        "env_value_count": 1,
                        "retired_provider_keys": ["LEGACY_API_PASSWORD"],
                    },
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
    assert apply["result"]["runtime_environment"]["retired_provider_keys_before"] == []
    assert apply["result"]["runtime_environment"]["retired_provider_keys_after"] == [
        "LEGACY_API_PASSWORD"
    ]
    assert "record" not in apply["result"]["runtime_environment"]
    assert apply["result"]["secrets"] == [
        {
            "action": "rotated",
            "integration": "example-provider",
            "binding_key": "EXAMPLE_API_TOKEN",
        }
    ]
    assert "secret-record-example" not in json.dumps(apply)


def test_product_config_projection_keeps_adoption_names_and_refuses_values() -> None:
    adoption = [
        {"key": "EXAMPLE_LABEL", "disposition": "adopted"},
        {"key": "EXAMPLE_WORKERS", "disposition": "template_default"},
        {"key": "EXAMPLE_CALLBACK_URL", "disposition": "refused_credential"},
    ]
    assert write_action._project_provider_key_adoption(adoption) == adoption

    for unsafe in (
        [{"key": "EXAMPLE_LABEL", "disposition": "adopted", "value": "private-value"}],
        [{"key": "EXAMPLE_LABEL", "disposition": "private-value"}],
        [{"key": "private value", "disposition": "adopted"}],
    ):
        try:
            write_action._project_provider_key_adoption(unsafe)
        except safety.LaunchplaneSafetyError:
            continue
        raise AssertionError(f"expected {unsafe!r} to be refused")


def test_product_config_secret_results_keep_declared_secret_class() -> None:
    projected = write_action._project_secret_results(
        [
            {
                "action": "rotated",
                "scope": "context_instance",
                "integration": "runtime_environment",
                "name": "EXAMPLE_API_TOKEN",
                "binding_key": "EXAMPLE_API_TOKEN",
                "context": "example-product",
                "instance": "testing",
                "secret_id": "secret-record-example",
                "secret_class": "testing",
            },
            {
                "action": "created",
                "scope": "context_instance",
                "integration": "runtime_environment",
                "name": "EXAMPLE_SYNC_API_TOKEN",
                "binding_key": "EXAMPLE_SYNC_API_TOKEN",
                "context": "example-product",
                "instance": "testing",
                "secret_class": "shared_safe",
                "sharing_reason": {
                    "kind": "read_only_source",
                    "reason": "Testing imports from the production account.",
                    "evidence": "The Client confirmed a read-only token on 2026-10-02.",
                },
            },
        ]
    )

    assert projected == [
        {
            "action": "rotated",
            "integration": "runtime_environment",
            "binding_key": "EXAMPLE_API_TOKEN",
            "secret_class": "testing",
        },
        {
            "action": "created",
            "integration": "runtime_environment",
            "binding_key": "EXAMPLE_SYNC_API_TOKEN",
            "secret_class": "shared_safe",
            "sharing_reason": {
                "kind": "read_only_source",
                "reason": "Testing imports from the production account.",
                "evidence": "The Client confirmed a read-only token on 2026-10-02.",
            },
        },
    ]


def test_product_config_projection_keeps_declared_secret_class_end_to_end() -> None:
    result = write_action.summarize_success(
        operation="product-config-dry-run",
        request={"mode": "dry-run", "payload_source": "private_file"},
        provider_payload={
            "status": "accepted",
            "trace_id": "launchplane_req_secret_class",
            "records": {},
            "result": {
                "status": "ok",
                "mode": "dry-run",
                "product": "example-product",
                "context": "example-product",
                "instance": "testing",
                "runtime_environment": {
                    "action": "skipped",
                    "scope": "instance",
                    "context": "example-product",
                    "instance": "testing",
                    "keys": [],
                    "changed_keys": [],
                    "unchanged_keys": [],
                    "env_value_count_after": 0,
                    "retired_provider_keys_before": [],
                    "retired_provider_keys_after": [],
                },
                "runtime_key_safety": {
                    "required": True,
                    "status": "pass",
                    "policy_record_id": "policy-example",
                    "policy_sha256": "abc123",
                    "target": {
                        "context": "example-product",
                        "instance": "testing",
                        "environment_class": "testing",
                    },
                    "checked_binding_keys": ["EXAMPLE_API_TOKEN"],
                    "findings": [],
                    "reported": [
                        {
                            "code": "sharing_reason_missing",
                            "binding_key": "EXAMPLE_SYNC_API_TOKEN",
                            "binding_id": "binding-example",
                            "secret_id": "secret-record-example",
                            "secret_class": "shared_safe",
                            "detail": "Integration key is declared shared_safe with no reason.",
                        }
                    ],
                },
                "secrets": [
                    {
                        "action": "rotated",
                        "scope": "context_instance",
                        "integration": "runtime_environment",
                        "name": "EXAMPLE_API_TOKEN",
                        "binding_key": "EXAMPLE_API_TOKEN",
                        "context": "example-product",
                        "instance": "testing",
                        "secret_id": "secret-record-example",
                        "secret_class": "testing",
                    }
                ],
                "summary": {"runtime_changed_key_count": 0, "secret_change_count": 1},
                "next_actions": [],
            },
        },
    )

    assert result["status"] == "accepted"
    assert result["result"]["runtime_key_safety"]["reported"] == [
        {"key": "EXAMPLE_SYNC_API_TOKEN", "code": "sharing_reason_missing"}
    ]
    assert result["result"]["secrets"] == [
        {
            "action": "rotated",
            "integration": "runtime_environment",
            "binding_key": "EXAMPLE_API_TOKEN",
            "secret_class": "testing",
        }
    ]


_COPY_REFERENCE = {
    "context": "example-product",
    "instance": "prod",
    "version_id": "secret-version-example-1",
}
_COPY_SHARING_REASON = {
    "kind": "read_only_source",
    "reason": "Testing reads the same source.",
    "evidence": "The Client verified view-only permissions on 2026-10-02.",
}


def _copy_reference_payload() -> dict[str, object]:
    return {
        "product": "example-product",
        "context": "example-product",
        "instance": "testing",
        "reason": "Copy the verified read-only source to testing.",
        "secrets": [
            {
                "binding_key": "EXAMPLE_SYNC_API_TOKEN",
                "copy_from": dict(_COPY_REFERENCE),
                "secret_class": "shared_safe",
                "sharing_reason": dict(_COPY_SHARING_REASON),
            }
        ],
    }


def _copy_reference_response(mode: str, *, replayed: bool = False) -> dict[str, object]:
    response: dict[str, object] = {
        "status": "accepted",
        "trace_id": f"launchplane_req_copy_{mode}",
        "records": {},
        "result": {
            "status": "ok",
            "mode": mode,
            "product": "example-product",
            "context": "example-product",
            "instance": "testing",
            "runtime_environment": {
                "action": "skipped",
                "scope": "instance",
                "context": "example-product",
                "instance": "testing",
                "keys": [],
                "changed_keys": [],
                "unchanged_keys": [],
                "env_value_count_after": 0,
            },
            "runtime_key_safety": {
                "required": True,
                "status": "pass",
                "checked_binding_keys": ["EXAMPLE_SYNC_API_TOKEN"],
                "findings": [],
            },
            "secrets": [
                {
                    "action": "created",
                    "scope": "context_instance",
                    "integration": "runtime_environment",
                    "name": "EXAMPLE_SYNC_API_TOKEN",
                    "binding_key": "EXAMPLE_SYNC_API_TOKEN",
                    "context": "example-product",
                    "instance": "testing",
                    "secret_id": "secret-record-copy",
                    "copy_from": dict(_COPY_REFERENCE),
                    "sharing_reason": dict(_COPY_SHARING_REASON),
                    "secret_class": "shared_safe",
                }
            ],
            "summary": {"runtime_changed_key_count": 0, "secret_change_count": 1},
            "next_actions": [],
        },
    }
    if replayed:
        response["replayed"] = True
        response["original_trace_id"] = "launchplane_req_copy_apply"
    return response


def test_product_config_secret_copy_keeps_the_source_through_dry_run_apply_and_replay() -> None:
    expected_secret = {
        "action": "created",
        "integration": "runtime_environment",
        "binding_key": "EXAMPLE_SYNC_API_TOKEN",
        "secret_class": "shared_safe",
        "sharing_reason": _COPY_SHARING_REASON,
        "copy_from": _COPY_REFERENCE,
    }
    responses = [
        _copy_reference_response("dry-run"),
        _copy_reference_response("apply"),
        _copy_reference_response("apply", replayed=True),
    ]
    calls: list[dict[str, Any]] = []

    def fake_request(**kwargs: Any) -> dict[str, object]:
        calls.append(kwargs)
        return responses[len(calls) - 1]

    settings = {"service_url": "https://launchplane.example.invalid", "token": "t"}
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "copy.json"
        payload_path.write_text(json.dumps(_copy_reference_payload()), encoding="utf-8")
        apply_argv = [
            "product-config-apply",
            "--payload-file",
            str(payload_path),
            "--idempotency-key",
            "example-copy-1",
            "--reviewed-dry-run",
        ]
        outputs: list[dict[str, Any]] = []
        for argv in (
            ["product-config-dry-run", "--payload-file", str(payload_path)],
            apply_argv,
            apply_argv,
        ):
            output = io.StringIO()
            with (
                temporary_attribute(
                    write_action, "prepare_operator_settings", lambda **_kwargs: settings
                ),
                temporary_attribute(write_action, "request_launchplane", fake_request),
                redirect_stdout(output),
            ):
                assert write_action.main(argv) == 0, output.getvalue()
            outputs.append(json.loads(output.getvalue()))

    assert [call["body"]["mode"] for call in calls] == ["dry-run", "apply", "apply"]
    for call in calls:
        (secret,) = call["body"]["secrets"]
        assert secret["copy_from"] == _COPY_REFERENCE
        assert "value" not in secret
    assert [call["idempotency_key"] for call in calls] == ["", "example-copy-1", "example-copy-1"]
    for payload in outputs:
        assert payload["status"] == "accepted"
        assert payload["result"]["secrets"] == [expected_secret]
        assert "secret-record-copy" not in json.dumps(payload)


def test_product_config_secret_copy_refuses_a_value_or_malformed_source() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "copy.json"
        args = argparse.Namespace(payload_file=str(payload_path), idempotency_key="")
        for change, code in (
            ({"value": "should-not-be-sent"}, "secret_copy_with_value"),
            ({"copy_from": {**_COPY_REFERENCE, "product": "other"}}, "invalid_secret_copy_from"),
            ({"copy_from": {**_COPY_REFERENCE, "version_id": " "}}, "invalid_secret_copy_from"),
            ({"copy_from": "example-product/prod"}, "invalid_secret_copy_from"),
        ):
            payload = _copy_reference_payload()
            payload_entries = cast(list[dict[str, object]], payload["secrets"])
            payload_entries[0] = {**payload_entries[0], **change}
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            try:
                write_action.product_config_payload_body(args, mode="dry-run")
            except ValueError as exc:
                assert str(exc) == code
            else:
                raise AssertionError(f"expected {code}")

        ordinary = {
            "product": "example-product",
            "secrets": [{"binding_key": "EXAMPLE_API_TOKEN", "value": "private-value"}],
        }
        payload_path.write_text(json.dumps(ordinary), encoding="utf-8")
        body = write_action.product_config_payload_body(args, mode="dry-run")
        assert body["secrets"] == ordinary["secrets"]


def test_product_config_secret_copy_projection_refuses_extra_source_fields() -> None:
    for copy_from in (
        {**_COPY_REFERENCE, "ciphertext": "opaque"},
        {**_COPY_REFERENCE, "secret_id": "secret-record-source"},
        {"context": "example-product", "instance": "prod"},
        {**_COPY_REFERENCE, "version_id": "ghp_" + "abcdefghijklmnop"},
    ):
        response = _copy_reference_response("apply")
        result = cast(dict[str, Any], response["result"])
        result["secrets"][0]["copy_from"] = copy_from
        try:
            write_action.summarize_success(
                operation="product-config-apply",
                request={"mode": "apply", "payload_source": "private_file"},
                provider_payload=response,
            )
        except safety.LaunchplaneSafetyError:
            continue
        raise AssertionError(f"expected {copy_from!r} to be refused")


def _secret_bindings_response() -> dict[str, object]:
    return {
        "status": "ok",
        "trace_id": "launchplane_req_secret_bindings",
        "product": "example-product",
        "bindings": [
            {
                "binding_key": "EXAMPLE_SYNC_API_TOKEN",
                "name": "EXAMPLE_SYNC_API_TOKEN",
                "scope": "context_instance",
                "context": "example-product",
                "instance": "prod",
                "secret_class": "shared_safe",
                "sharing_reason": {
                    **_COPY_SHARING_REASON,
                    "recorded_by": "operator-example",
                    "recorded_at": "2026-10-02T00:00:00Z",
                },
                "version_id": "secret-version-example-1",
                "provider_note": "private provider text",
            },
            {
                "binding_key": "EXAMPLE_SITE_PASSWORD",
                "name": "EXAMPLE_SITE_PASSWORD",
                "scope": "context",
                "context": "example-product",
                "instance": "",
                "secret_class": None,
                "sharing_reason": None,
                "version_id": "secret-version-example-2",
            },
            {"binding_key": "has spaces", "scope": "context", "context": "example-product"},
        ],
    }


def test_product_secret_bindings_read_keeps_metadata_and_counts_the_rest() -> None:
    argv = ["product-secret-bindings-read", "--product", "example-product"]
    status, payload, calls = _run_product_read(argv, _secret_bindings_response())

    assert status == 0, payload
    assert calls[0]["path"] == "/v1/products/example-product/secret-bindings"
    assert contract.LOCAL_EXTENSION_ROUTES["product-secret-bindings-read"]["method"] == "GET"
    result = payload["result"]
    assert result["bindings"] == [
        {
            "binding_key": "EXAMPLE_SYNC_API_TOKEN",
            "name": "EXAMPLE_SYNC_API_TOKEN",
            "scope": "context_instance",
            "context": "example-product",
            "instance": "prod",
            "secret_class": "shared_safe",
            "sharing_reason": {
                **_COPY_SHARING_REASON,
                "recorded_by": "operator-example",
                "recorded_at": "2026-10-02T00:00:00Z",
            },
            "version_id": "secret-version-example-1",
        },
        {
            "binding_key": "EXAMPLE_SITE_PASSWORD",
            "name": "EXAMPLE_SITE_PASSWORD",
            "scope": "context",
            "context": "example-product",
            "instance": "",
            "secret_class": "",
            "sharing_reason": None,
            "version_id": "secret-version-example-2",
        },
    ]
    assert result["omitted_binding_count"] == 1
    assert result["dropped_field_paths"] == [
        "bindings[].<unlisted field>",
        "bindings[].binding_key",
    ]
    assert result["dropped_field_count"] == 2
    rendered = json.dumps(payload)
    assert "provider_note" not in rendered
    assert "private provider text" not in rendered


def test_product_secret_bindings_read_fails_closed_on_values_and_ciphertext() -> None:
    argv = ["product-secret-bindings-read", "--product", "example-product"]
    for field, value in (
        ("value", "private-value"),
        ("ciphertext", "gAAAAAexample"),
        ("plaintext_value", "private-value"),
        ("secret_value", "private-value"),
        ("version_id", "ghp_" + "abcdefghijklmnop"),
        ("provider_note", "ghp_" + "abcdefghijklmnop"),
    ):
        response = _secret_bindings_response()
        cast(list[dict[str, object]], response["bindings"])[0][field] = value
        status, payload, _calls = _run_product_read(argv, response)
        assert status == 1, field
        assert payload["status"] == "invalid"
        assert value not in json.dumps(payload)

    nested = _secret_bindings_response()
    first = cast(list[dict[str, Any]], nested["bindings"])[0]
    first["sharing_reason"] = {**first["sharing_reason"], "value": "private-value"}
    status, payload, _calls = _run_product_read(argv, nested)
    assert status == 1
    assert "private-value" not in json.dumps(payload)

    extra_top_level = {**_secret_bindings_response(), "values": ["private-value"]}
    status, payload, _calls = _run_product_read(argv, extra_top_level)
    assert status == 1
    assert "private-value" not in json.dumps(payload)


def _integration_allowance_payload() -> dict[str, object]:
    return {
        "integration": "fishbowl",
        "kind": "read_only_source",
        "reason": "Imports from Fishbowl with a read-only account.",
        "evidence": "SELECT and SHOW VIEW grant read on 2026-09-28.",
        "recorded_by": "operator-example",
        "recorded_at": "2026-09-29T12:00:00Z",
    }


def test_integration_allowances_plan_projection_keeps_diff_and_digest() -> None:
    result = write_action.summarize_success(
        operation="integration-allowances-dry-run",
        request={"mode": "dry-run", "payload_source": "private_file"},
        provider_payload={
            "status": "accepted",
            "trace_id": "launchplane_req_allowances",
            "records": {"product_profile": "example-product", "context": "example", "instance": "testing"},
            "result": {
                "status": "ok",
                "mode": "dry-run",
                "product": "example-product",
                "context": "example",
                "instance": "testing",
                "environment_class": "testing",
                "changed": True,
                "applied": False,
                "changes": [
                    {"integration": "fishbowl", "action": "add", "before": None, "after": _integration_allowance_payload()}
                ],
                "read_back": [],
                "reason": "Record the Fishbowl import source.",
                "source_label": "service:integration-allowances",
                "record_sha256_before": "a" * 64,
                "record_sha256_after": "",
                "plan_sha256": "b" * 64,
            },
        },
    )

    assert result["status"] == "accepted"
    assert result["summary"]["plan_sha256"] == "b" * 64
    change = result["result"]["changes"][0]
    assert change["action"] == "add"
    assert change["after"]["kind"] == "read_only_source"
    assert "before" not in change


def test_integration_allowances_projection_refuses_unknown_fields() -> None:
    allowance = _integration_allowance_payload()
    allowance["value"] = "not-allowed"
    try:
        write_action._project_integration_allowance(allowance)
    except write_action.LaunchplaneSafetyError as exc:
        assert exc.code == "unsafe_response_shape"
    else:
        raise AssertionError("unknown allowance field was accepted")


def test_integration_allowances_read_summary_projects_allowances() -> None:
    result = write_action.summarize_integration_allowances_read(
        request={"payload_source": "operator_argument"},
        provider_payload={
            "status": "accepted",
            "trace_id": "launchplane_req_allowances_read",
            "records": {"product_profile": "example-product", "context": "example", "instance": "testing"},
            "result": {
                "status": "ok",
                "product": "example-product",
                "context": "example",
                "instance": "testing",
                "environment_class": "testing",
                "allowances": [_integration_allowance_payload()],
                "record_sha256": "c" * 64,
            },
        },
    )

    assert result["result"]["allowances"][0]["integration"] == "fishbowl"
    assert result["result"]["allowances"][0]["recorded_by"] == "operator-example"


def test_integration_allowances_read_shows_why_a_lane_key_is_shared() -> None:
    sharing_reason = {
        "kind": "read_only_source",
        "reason": "Testing imports from the production account.",
        "evidence": "The Client confirmed a read-only token on 2026-10-02.",
        "recorded_by": "operator-example",
        "recorded_at": "2026-10-02T00:00:00Z",
    }
    result = write_action.summarize_integration_allowances_read(
        request={"payload_source": "operator_argument"},
        provider_payload={
            "status": "accepted",
            "trace_id": "launchplane_req_allowances_read",
            "records": {"product_profile": "example-product", "context": "example", "instance": "testing"},
            "result": {
                "status": "ok",
                "product": "example-product",
                "context": "example",
                "instance": "testing",
                "environment_class": "testing",
                "allowances": [],
                "integration_keys": [
                    {
                        "binding_key": "EXAMPLE_API_TOKEN",
                        "declared_secret_class": "shared_safe",
                        "sharing_reason": sharing_reason,
                    }
                ],
                "record_sha256": "c" * 64,
            },
        },
    )

    assert result["result"]["integration_keys"] == [
        {
            "binding_key": "EXAMPLE_API_TOKEN",
            "secret_class": "shared_safe",
            "sharing_reason": sharing_reason,
        }
    ]
    pasted_key = "rk_live_" + "51HxQ2eZvKYlo2C0aBcDeFgH1234"
    redacted = write_action._project_sharing_reason(
        {**sharing_reason, "evidence": f"Verified read-only scope of {pasted_key} on 2026-10-02."}
    )
    assert redacted["evidence"] == "Verified read-only scope of [redacted] on 2026-10-02."
    for unsafe in ({**sharing_reason, "value": "x"}, {**sharing_reason, "kind": "borrowed"}):
        try:
            write_action._project_sharing_reason(unsafe)
        except write_action.LaunchplaneSafetyError:
            continue
        raise AssertionError(f"expected {unsafe!r} to be refused")


def test_integration_allowances_payload_rejects_unknown_fields_and_unreviewed_apply() -> None:
    with TemporaryDirectory() as directory:
        payload_path = Path(directory) / "allowances.json"
        payload: dict[str, object] = {
            "schema_version": 1,
            "product": "example-product",
            "context": "example",
            "instance": "testing",
            "reason": "Record the Fishbowl import source.",
            "allowances": [
                {"integration": "fishbowl", "kind": "pre_live", "reason": "Not live yet.", "value": "x"}
            ],
        }
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        args = argparse.Namespace(payload_file=str(payload_path), idempotency_key="")
        try:
            write_action.integration_allowances_body(args, mode="dry-run")
        except ValueError as exc:
            assert str(exc) == "unsupported_allowance_field"
        else:
            raise AssertionError("unknown allowance input field was accepted")

        payload["allowances"] = [{"integration": "fishbowl", "kind": "pre_live", "reason": "Not live yet."}]
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        apply_args = argparse.Namespace(
            payload_file=str(payload_path),
            idempotency_key="allowances-apply-1",
            reviewed_dry_run=True,
            expected_plan_digest="b" * 64,
            dry_run_evidence_file="",
        )
        try:
            write_action.integration_allowances_body(apply_args, mode="apply")
        except ValueError as exc:
            assert str(exc) == "reviewed_dry_run_not_apply_eligible"
        else:
            raise AssertionError("apply without saved dry-run evidence was accepted")


def _expect_error(call: Any, code: str) -> None:
    try:
        call()
    except (ValueError, write_action.LaunchplaneSafetyError) as exc:
        assert getattr(exc, "code", str(exc)) == code, (code, exc)
    else:
        raise AssertionError(f"expected {code}")


def _testing_hold_plan(**overrides: object) -> dict[str, object]:
    plan: dict[str, object] = {
        "status": "ok",
        "mode": "dry-run",
        "product": "example-product",
        "context": "example",
        "instance": "testing",
        "action": "set",
        "changed": True,
        "applied": False,
        "before": None,
        "after": {
            "reason": "Staff are testing the checkout flow.",
            "recorded_by": "operator-example",
            "recorded_at": "2026-09-30T12:00:00Z",
        },
        "read_back": None,
        "read_back_matches": None,
        "reconcile_requested": False,
        "reason": "Staff are testing the checkout flow.",
        "source_label": "service:testing-hold",
        "record_sha256_before": "a" * 64,
        "record_sha256_after": "",
        "plan_sha256": "b" * 64,
    }
    plan.update(overrides)
    return plan


def _testing_hold_response(plan: dict[str, object]) -> dict[str, object]:
    return {
        "status": "accepted",
        "trace_id": "launchplane_req_testing_hold",
        "records": {"product_profile": "example-product", "context": "example", "instance": "testing"},
        "result": plan,
    }


def _saved_dry_run_output(operation: str, response: dict[str, object]) -> dict[str, Any]:
    # The apply's evidence is the helper's own saved dry-run output, not the raw service reply.
    return write_action.summarize_success(
        operation=operation, request={"mode": "dry-run"}, provider_payload=response
    )


def test_testing_hold_plan_projection_is_bounded_and_fail_closed() -> None:
    result = _saved_dry_run_output(
        "testing-hold-dry-run", _testing_hold_response(_testing_hold_plan())
    )
    assert result["summary"]["plan_sha256"] == "b" * 64
    assert result["result"]["before"] is None
    assert result["result"]["after"]["recorded_by"] == "operator-example"
    assert "read_back_matches" not in result["result"]

    for plan, code in (
        (_testing_hold_plan(extra="x"), "unsafe_response_shape"),
        (_testing_hold_plan(action="delete"), "invalid_response"),
        (_testing_hold_plan(mode="plan"), "invalid_response"),
        (_testing_hold_plan(plan_sha256="not-a-digest"), "invalid_response"),
        (
            _testing_hold_plan(after={"reason": "Testing.", "token": "x"}),
            "unsafe_response_shape",
        ),
    ):
        _expect_error(lambda candidate=plan: write_action._project_testing_hold_plan(candidate), code)
    _expect_error(
        lambda: write_action._project_success_output(
            "testing-hold-dry-run",
            {**_testing_hold_response(_testing_hold_plan()), "records": {"secret": "x"}},
        ),
        "unsafe_response_shape",
    )


def test_testing_hold_read_sends_lane_query_and_projects_hold() -> None:
    calls: list[dict[str, Any]] = []
    response = {
        "status": "accepted",
        "trace_id": "launchplane_req_testing_hold_read",
        "records": {"product_profile": "example-product", "context": "example", "instance": "testing"},
        "result": {
            "status": "ok",
            "product": "example-product",
            "context": "example",
            "instance": "testing",
            "hold": {"reason": "Staff are testing.", "recorded_by": "", "recorded_at": ""},
            "record_sha256": "c" * 64,
        },
    }

    def fake_read(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return response

    settings = {"service_url": "https://launchplane.example.invalid", "token": "t"}
    output = io.StringIO()
    with (
        patch.object(write_action, "prepare_operator_settings", return_value=settings),
        patch.object(write_action, "request_launchplane_read", side_effect=fake_read),
        redirect_stdout(output),
    ):
        status = write_action.main(
            ["testing-hold-read", "--product", "example-product", "--context", "example", "--instance", "testing"]
        )
    assert status == 0
    assert calls[0]["path"] == contract.LOCAL_EXTENSION_ROUTES["testing-hold-read"]["path"]
    assert calls[0]["query"] == {"product": "example-product", "context": "example", "instance": "testing"}
    payload = json.loads(output.getvalue())
    assert payload["result"]["hold"] == {"reason": "Staff are testing."}

    response["result"] = {**cast(dict[str, object], response["result"]), "hold": {"reason": "x", "value": "y"}}
    output = io.StringIO()
    with (
        patch.object(write_action, "prepare_operator_settings", return_value=settings),
        patch.object(write_action, "request_launchplane_read", side_effect=fake_read),
        redirect_stdout(output),
    ):
        status = write_action.main(
            ["testing-hold-read", "--product", "example-product", "--context", "example", "--instance", "testing"]
        )
    assert status == 1
    assert "result" not in json.loads(output.getvalue()) or not json.loads(output.getvalue())["result"]


def _product_environment_response() -> dict[str, object]:
    identity = {
        "schema_version": 1,
        "product": "example-product",
        "context": "example",
        "instance": "testing",
        "environment_kind": "stable",
        "deployment_record_id": "deployment-example-testing-1",
        "artifact_id": "artifact-example-abc123",
        "source_git_ref": "abc123",
        "image_reference": "ghcr.io/example/site@sha256:" + "d" * 64,
        "release_tuple_id": "",
        "preview_id": "",
        "preview_generation_id": "",
        "deployed_at": "2026-09-30T12:00:00Z",
    }
    return {
        "status": "ok",
        "trace_id": "launchplane_req_product_environment",
        "environment": {
            "schema_version": 1,
            "product": "example-product",
            "display_name": "Example Product",
            "repository": "example/site",
            "driver_id": "odoo",
            "base_driver_id": "",
            "environment": "testing",
            "context": "example",
            "base_url": "https://testing.example.invalid",
            "health_url": "https://testing.example.invalid/health",
            "driver_extensions": {"odoo": {"upstream_source": "private-upstream"}},
            "target": {
                "provider": "dokploy",
                "target_type": "compose",
                "target_name": "private-target-name",
                "provider_target_type": "compose",
                "target_id_recorded": True,
                "artifact_manifest": {
                    "schema_version": 2,
                    "artifact_id": "artifact-example-abc123",
                    "source_commit": "abc123",
                    "enterprise_base_digest": "sha256:" + "e" * 64,
                    "build_flags": {"values": {"PRIVATE_FLAG": "private"}},
                    "image": {
                        "repository": "ghcr.io/example/site",
                        "digest": "sha256:" + "d" * 64,
                        "tags": ["private-tag"],
                    },
                    "source_build": {
                        "repository": "example/site",
                        "repository_id": "1001",
                        "workflow_path": ".github/workflows/build.yml",
                        "event": "push",
                        "purpose": "testing",
                        "pull_request_number": None,
                        "run_id": 42,
                        "run_attempt": 1,
                        "github_artifact_id": 7,
                        "manifest_artifact_id": "manifest-1",
                    },
                },
                "expected_runtime_identity": identity,
                "observed_runtime_identity": identity,
                "runtime_identity_status": "match",
                "runtime_identity_detail": "Runtime identity matches the expected deployment record.",
                "trust_state": "recorded",
            },
            "topology": {"private": "topology"},
            "health_monitoring": {
                "monitoring_intent": "prelaunch",
                "public_incident_eligible": False,
                "checks": [
                    {
                        "name": "public",
                        "kind": "http",
                        "enabled": True,
                        "probe_effective": True,
                        "incident_eligible": False,
                        "private_endpoint_configured": True,
                        "status": "pass",
                        "failure_code": "",
                        "observed_at": "2026-09-30T12:05:00Z",
                        "record_id": "observation-1",
                        "summary": "ok",
                        "incident_status": "",
                        "trust_state": "recorded",
                    }
                ],
                "trust_state": "recorded",
                "provenance": {"source_kind": "record", "freshness_status": "recorded"},
            },
            "public_ingress": {"status": "pass", "failure_code": "", "trust_state": "recorded"},
            "runtime_settings": [
                {"scope": "instance", "env_keys": ["PRIVATE_KEY_NAME"], "env_value_count": 1}
            ],
            "managed_secrets": [{"binding_id": "binding-1", "secret_id": "secret-1"}],
            "available_actions": [{"action_id": "deploy", "route_path": "/v1/private"}],
            "warnings": ["private warning"],
            "trust_state": "recorded",
            "provenance": {
                "source_kind": "record",
                "source_record_id": "deployment-example-testing-1",
                "recorded_at": "2026-09-30T12:00:00Z",
                "refreshed_at": "",
                "freshness_status": "recorded",
                "stale_after": "",
                "detail": "private detail",
            },
        },
    }


def _run_product_read(
    argv: list[str], response: object
) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def fake_read(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if isinstance(response, BaseException):
            raise response
        return response

    settings = {"service_url": "https://launchplane.example.invalid", "token": "t"}
    output = io.StringIO()
    with (
        temporary_attribute(write_action, "prepare_operator_settings", lambda **_kwargs: settings),
        temporary_attribute(write_action, "request_launchplane_read", fake_read),
        redirect_stdout(output),
    ):
        status = write_action.main(argv)
    return status, json.loads(output.getvalue()), calls


def _path_check_response(path: str = "testing") -> dict[str, Any]:
    return {
        "status": "ok",
        "trace_id": "launchplane_req_path_check",
        "check": {
            "product": "example-product",
            "path": path,
            "state": "clear",
            "blocked_count": 0,
            "unknown_count": 0,
            "steps": [
                {
                    "step_id": "profile_lane",
                    "state": "clear",
                    "code": "lane_recorded",
                    "description": "The lane is recorded.",
                    "fix": "none",
                    "record_ids": [],
                },
                {
                    "step_id": "testing_deploy" if path == "testing" else "backup_authority",
                    "state": "clear",
                    "code": "already_deployed" if path == "testing" else "backup_ready",
                    "description": "The evidence is recorded.",
                    "fix": "none",
                    "record_ids": ["operation-example-1"],
                },
            ],
        },
    }


def test_path_check_reads_both_paths_and_preserves_clear_blocked_unknown() -> None:
    for path in ("testing", "promote"):
        for state, fix, code in (
            ("clear", "none", "already_deployed"),
            ("blocked", "grant", "caller_lacks_promotion_grant"),
            ("unknown", "wait", "reconcile_requests_unread"),
        ):
            response = _path_check_response(path)
            check = response["check"]
            check["state"] = state
            check["blocked_count"] = int(state == "blocked")
            check["unknown_count"] = int(state == "unknown")
            check["steps"][1].update(state=state, fix=fix, code=code)
            argv = ["path-check", "--product", check["product"], "--path", path]
            status, payload, calls = _run_product_read(argv, response)
            assert status == 0, payload
            assert len(calls) == 1
            route = contract.LOCAL_EXTENSION_ROUTES["path-check"]
            assert calls[0]["path"] == route["path"].format(product=check["product"])
            assert calls[0]["query"] == {"path": path}
            assert payload["result"] == check
            assert payload["summary"]["trace_id"] == response["trace_id"]

    # Blockers and unread evidence must survive together, in service order.
    response = _path_check_response()
    check = response["check"]
    check.update(state="blocked", blocked_count=1, unknown_count=1)
    check["steps"][0].update(state="blocked", fix="by_hand")
    check["steps"][1].update(state="unknown", fix="wait")
    status, payload, _calls = _run_product_read(
        ["path-check", "--product", check["product"], "--path", check["path"]], response
    )
    assert status == 0 and payload["result"] == check


def test_path_check_refuses_unknown_fields_unsafe_values_and_incomplete_evidence() -> None:
    mutations = (
        lambda body: body.update(extra="private detail"),
        lambda body: body["check"].update(extra="private detail"),
        lambda body: body["check"]["steps"][0].update(provider_error="private detail"),
        lambda body: body["check"]["steps"][0].update(secret_name="PRIVATE_SECRET"),
        lambda body: body["check"]["steps"][0].update(description="Bearer abcdefghijklmnop"),
        lambda body: body["check"]["steps"][0].update(description="https://private.example.invalid/x"),
        lambda body: body["check"]["steps"][0].update(record_ids=["token=private-value"]),
        lambda body: body["check"]["steps"][0].pop("state"),
        lambda body: body["check"]["steps"][0].update(state="pending"),
        lambda body: body["check"]["steps"][0].update(state=[]),
        lambda body: body["check"]["steps"][0].update(fix="invent_access"),
        lambda body: body["check"]["steps"][0].update(code={}),
        lambda body: body["check"]["steps"][0].update(record_ids={}),
        lambda body: body["check"].update(steps=[]),
        lambda body: body["check"].update(steps=[None]),
        lambda body: body["check"].update(blocked_count=1),
        lambda body: body["check"].update(unknown_count=False),
        lambda body: body["check"].update(state="blocked"),
        lambda body: body["check"].update(path="rollback"),
        lambda body: body["check"].update(path="promote"),
        lambda body: body["check"].update(product="another-product"),
    )
    argv = ["path-check", "--product", "example-product", "--path", "testing"]
    for mutate in mutations:
        response = _path_check_response()
        mutate(response)
        status, payload, calls = _run_product_read(argv, response)
        assert status == 1 and len(calls) == 1, payload
        assert payload["status"] == "invalid" and not payload["result"], payload
        for value in ("private detail", "PRIVATE_SECRET", "abcdefghijklmnop", "private.example", "private-value"):
            assert value not in json.dumps(payload)


def test_path_check_refuses_bad_selector_and_surfaces_read_denial() -> None:
    status, payload, calls = _run_product_read(
        ["path-check", "--product", "../admin", "--path", "testing"], _path_check_response()
    )
    assert status == 2 and not calls
    assert payload["warnings"][0]["code"] == "invalid_product"
    with redirect_stderr(io.StringIO()):
        try:
            _run_product_read(
                ["path-check", "--product", "example-product", "--path", "rollback"], {}
            )
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError("Unsupported path was accepted")
    denied = urllib.error.HTTPError(
        "https://launchplane.example.invalid", 404, "Not found", Message(),
        io.BytesIO(json.dumps({"status": "error", "trace_id": "launchplane_req_denied",
                              "error": {"code": "not_found", "message": "private detail"}}).encode()),
    )
    status, payload, calls = _run_product_read(
        ["path-check", "--product", "example-product", "--path", "testing"], denied
    )
    assert status == 1 and len(calls) == 1
    assert not payload["result"] and "private detail" not in json.dumps(payload)
    assert payload["summary"]["trace_id"] == "launchplane_req_denied"


def test_product_environment_read_uses_path_route_and_projects_deploy_identity() -> None:
    argv = ["product-environment-read", "--product", "example-product", "--environment", "testing"]
    status, payload, calls = _run_product_read(argv, _product_environment_response())
    assert status == 0
    assert contract.LOCAL_EXTENSION_ROUTES["product-environment-read"]["method"] == "GET"
    assert calls[0]["path"] == "/v1/products/example-product/environments/testing"
    assert calls[0]["query"] == {}
    assert payload["operation"] == "product-environment-read"
    assert payload["request"] == {
        "product": "example-product",
        "environment": "testing",
        "payload_source": "operator_argument",
    }
    result = payload["result"]
    assert result["target"]["artifact"] == {
        "artifact_id": "artifact-example-abc123",
        "source_commit": "abc123",
        "image_repository": "ghcr.io/example/site",
        "image_digest": "sha256:" + "d" * 64,
        "source_build": {
            "repository": "example/site",
            "event": "push",
            "purpose": "testing",
            "run_id": 42,
            "pull_request_number": None,
        },
    }
    assert result["target"]["expected_runtime_identity"]["deployment_record_id"] == (
        "deployment-example-testing-1"
    )
    assert result["target"]["runtime_identity_status"] == "match"
    assert result["health_monitoring"]["checks"][0]["status"] == "pass"
    assert payload["summary"]["trace_id"] == "launchplane_req_product_environment"
    rendered = json.dumps(payload)
    for dropped in (
        "private-target-name",
        "PRIVATE_KEY_NAME",
        "PRIVATE_FLAG",
        "private-tag",
        "binding-1",
        "secret-1",
        "private-upstream",
        "private warning",
        "private detail",
        "testing.example.invalid",
        "/v1/private",
        "topology",
    ):
        assert dropped not in rendered, dropped


def _product_profile_response() -> dict[str, object]:
    return {
        "status": "ok",
        "trace_id": "launchplane_req_product_profile",
        "profile": {
            "schema_version": 1,
            "product": "example-product",
            "display_name": "Example Site",
            "driver_id": "odoo",
            "repository": "example/site",
            "production_use": "unknown",
            "lifecycle_state": "active",
            "owner": {"github_login": "example-owner", "github_id": "123", "review_label": "owner-review"},
            "lanes": [
                {"context": "example", "instance": "testing", "base_url": "https://testing.example.invalid"},
                {"context": "example", "instance": "prod", "base_url": "https://prod.example.invalid"},
            ],
            "image": {"repository": "ghcr.io/example/private-image"},
            "expected_config": {"runtime_environment_keys": [{"key": "PRIVATE_KEY_NAME"}]},
            "promotion_workflow": {"workflow_file": "private-workflow.yml"},
            "updated_at": "2026-09-26T20:28:34Z",
        },
    }


def test_product_profile_read_returns_owner_and_production_use_only() -> None:
    argv = ["product-profile-read", "--product", "example-product"]
    status, payload, calls = _run_product_read(argv, _product_profile_response())
    assert status == 0
    assert contract.LOCAL_EXTENSION_ROUTES["product-profile-read"]["method"] == "GET"
    assert calls[0]["path"] == "/v1/product-profiles/example-product"
    assert payload["result"] == {
        "product": "example-product",
        "display_name": "Example Site",
        "driver_id": "odoo",
        "repository": "example/site",
        "production_use": "unknown",
        "lifecycle_state": "active",
        "owner_github_login": "example-owner",
        "owner_review_label": "owner-review",
        "lanes": [
            {"context": "example", "instance": "testing"},
            {"context": "example", "instance": "prod"},
        ],
        "lanes_truncated": False,
        "updated_at": "2026-09-26T20:28:34Z",
    }
    rendered = json.dumps(payload)
    for dropped in ("example.invalid", "private-image", "PRIVATE_KEY_NAME", "private-workflow", "123"):
        assert dropped not in rendered, dropped

    for mutate in (
        lambda body: body.update(extra="x"),
        lambda body: body["profile"].update(production_use="not a code"),
        lambda body: body["profile"]["owner"].update(github_login="Bearer abcdefghijklmnop"),
        lambda body: body["profile"]["owner"].update(github_login="https://private.example/x"),
        lambda body: body["profile"]["owner"].update(review_label="https://private.example/x"),
        lambda body: body["profile"].update(production_use="retired"),
        lambda body: body["profile"].update(owner=False),
        lambda body: body["profile"].update(lanes={}),
    ):
        mutated = _product_profile_response()
        mutate(mutated)
        status, payload, _calls = _run_product_read(argv, mutated)
        assert status == 1
        assert payload["status"] == "invalid"
        assert not payload["result"]


def test_product_environment_read_refuses_bad_segments_and_unsafe_values() -> None:
    for argv, code in (
        (["product-environment-read", "--product", "../admin", "--environment", "testing"], "invalid_product"),
        (["product-environment-read", "--product", "example", "--environment", "a/b"], "invalid_environment"),
        (["product-activity-read", "--product", "example?x=1"], "invalid_product"),
        (["product-profile-read", "--product", "../admin"], "invalid_product"),
    ):
        status, payload, calls = _run_product_read(argv, _product_environment_response())
        assert status == 2
        assert calls == []
        assert payload["warnings"][0]["code"] == code

    argv = ["product-environment-read", "--product", "example-product", "--environment", "testing"]
    for mutate in (
        lambda body: body.update(extra="x"),
        lambda body: body["environment"]["target"]["expected_runtime_identity"].update(
            artifact_id="Bearer abcdefghijklmnop"
        ),
        lambda body: body["environment"].update(product=None),
    ):
        mutated = _product_environment_response()
        mutate(mutated)
        status, payload, _calls = _run_product_read(argv, mutated)
        assert status == 1
        assert payload["status"] == "invalid"
        assert not payload["result"]


def _product_activity_event(index: int) -> dict[str, object]:
    return {
        "event_id": f"deployment:deployment-example-testing-{index}",
        "event_type": "deployment",
        "product": "example-product",
        "context": "example",
        "environment": "testing",
        "driver_id": "odoo",
        "action_id": "testing_deploy",
        "status": "pass",
        "occurred_at": "2026-09-30T12:00:00Z",
        "title": "Example Product testing deployment",
        "summary": "Deployment pass for example/testing.",
        "records": [
            {"record_type": "deployment", "record_id": f"deployment-example-testing-{link}"}
            for link in range(12)
        ],
        "trust_state": "recorded",
        "private_extra": "private-event-field",
    }


def test_product_activity_read_bounds_events_and_record_links() -> None:
    response = {
        "status": "ok",
        "trace_id": "launchplane_req_product_activity",
        "activity": {
            "schema_version": 1,
            "product": "example-product",
            "display_name": "Example Product",
            "repository": "example/site",
            "driver_id": "odoo",
            "events": [_product_activity_event(index) for index in range(60)],
        },
    }
    status, payload, calls = _run_product_read(
        ["product-activity-read", "--product", "example-product"], response
    )
    assert status == 0
    assert contract.LOCAL_EXTENSION_ROUTES["product-activity-read"]["method"] == "GET"
    assert calls[0]["path"] == "/v1/products/example-product/activity"
    result = payload["result"]
    assert len(result["events"]) == write_action.PRODUCT_ACTIVITY_MAX_EVENTS
    assert result["events_truncated"] is True
    first = result["events"][0]
    assert len(first["records"]) == write_action.PRODUCT_ACTIVITY_MAX_RECORD_LINKS
    assert first["records"][0] == {
        "record_type": "deployment",
        "record_id": "deployment-example-testing-0",
    }
    assert first["status"] == "pass"
    assert "private-event-field" not in json.dumps(payload)


def test_product_activity_read_keeps_real_events_and_drops_odd_fields() -> None:
    # Shaped like the live refusal: authz events carry dotted action ids and joined summaries.
    authz_event = {
        **_product_activity_event(0),
        "event_id": "authz_policy:authz-policy-7",
        "event_type": "authz_policy",
        "context": "launchplane",
        "environment": "",
        "driver_id": "launchplane",
        "action_id": "authz_policy.grant",
        "status": "active",
        "title": "Example Product authorization granted",
        "summary": "service:authz · managed authorization grant · terminal-agent-credential-rule",
        "records": [{"record_type": "authz_policy", "record_id": "authz-policy-7"}],
    }
    odd_status_event = {**_product_activity_event(1), "status": "needs review"}
    unusable_event = {**_product_activity_event(2), "event_id": "has spaces in id"}
    response = {
        "status": "ok",
        "trace_id": "launchplane_req_product_activity",
        "activity": {
            "product": "example-product",
            "repository": "example/site",
            "driver_id": "odoo",
            "events": [authz_event, odd_status_event, unusable_event, _product_activity_event(3)],
        },
    }
    status, payload, _calls = _run_product_read(
        ["product-activity-read", "--product", "example-product"], response
    )
    assert status == 0
    result = payload["result"]
    assert [event["event_id"] for event in result["events"]] == [
        "authz_policy:authz-policy-7",
        "deployment:deployment-example-testing-1",
        "deployment:deployment-example-testing-3",
    ]
    authz = result["events"][0]
    assert authz["action_id"] == "authz_policy.grant"
    assert authz["status"] == "active"
    assert authz["summary"] == ""
    assert authz["records"] == [{"record_type": "authz_policy", "record_id": "authz-policy-7"}]
    assert result["events"][1]["status"] == ""
    assert result["events"][2]["records_truncated"] is True
    assert result["omitted_event_count"] == 1
    assert result["dropped_field_count"] == 3
    assert result["dropped_field_paths"] == [
        "events[].event_id",
        "events[].status",
        "events[].summary",
    ]

    secret_event = {**_product_activity_event(4), "summary": "rotated ghp_abcdefghijklmnop"}
    response["activity"] = {**cast(dict[str, object], response["activity"]), "events": [secret_event]}
    status, payload, _calls = _run_product_read(
        ["product-activity-read", "--product", "example-product"], response
    )
    assert status == 1
    assert payload["status"] == "invalid"
    assert not payload["result"]


def _preview_history_response() -> dict[str, object]:
    return {
        "status": "ok",
        "trace_id": "launchplane_req_preview_history",
        "preview": {
            "preview_id": "preview-example-site-pr-7",
            "context": "example",
            "anchor_repo": "site",
            "anchor_pr_number": 7,
            "anchor_pr_url": "https://github.com/example/site/pull/7",
            "canonical_url": "https://pr-7.example.invalid",
            "state": "active",
            "created_at": "2026-09-30T18:00:00Z",
            "updated_at": "2026-09-30T18:05:00Z",
            "serving_generation_id": "preview-example-site-pr-7-generation-0002",
            "private_note": "private-preview-field",
        },
        "generations": [
            {
                "generation_id": "preview-example-site-pr-7-generation-0001",
                "sequence": 1,
                "state": "superseded",
            },
            {
                "generation_id": "preview-example-site-pr-7-generation-0002",
                "sequence": 2,
                "state": "ready",
                "requested_reason": "external_preview_refresh",
                "artifact_id": "artifact-example-abc123",
                "anchor_summary": {"head_sha": "a" * 40, "private": "private-anchor-field"},
                "deploy_status": "pass",
                "runtime_identity": {
                    "deployment_record_id": "deployment-example-preview-7",
                    "artifact_id": "artifact-example-abc123",
                    "source_git_ref": "a" * 40,
                    "image_reference": "ghcr.io/example/site@sha256:" + "d" * 64,
                    "deployed_at": "2026-09-30T18:05:00Z",
                },
                "env": {"PRIVATE_KEY_NAME": "private-env-value"},
            },
        ],
    }


def test_preview_history_read_derives_launchplanes_preview_id() -> None:
    # Values computed with Launchplane's generate_preview_id.
    assert write_action.preview_id_for(
        context="cm_website", repository="cbusillo/odoo-tenant-cm-website", pr_number=111
    ) == "preview-cm-website-odoo-tenant-cm-website-pr-111"
    assert write_action.preview_id_for(
        context="Example_Ctx", repository="owner/Site.Repo", pr_number=7
    ) == "preview-example-ctx-site-repo-pr-7"


def test_preview_history_read_projects_newest_generation_first() -> None:
    argv = ["preview-history-read", "--context", "example", "--repository", "example/site", "--pr", "7"]
    status, payload, calls = _run_product_read(argv, _preview_history_response())
    assert status == 0
    assert calls[0]["path"] == "/v1/previews/preview-example-site-pr-7/history"
    result = payload["result"]
    assert result["preview"]["state"] == "active"
    assert result["preview"]["serving_generation_id"] == "preview-example-site-pr-7-generation-0002"
    newest = result["generations"][0]
    assert newest["sequence"] == 2
    assert newest["anchor_head_sha"] == "a" * 40
    assert newest["runtime_identity"]["image_digest"] == "sha256:" + "d" * 64
    assert result["generations"][1]["state"] == "superseded"
    rendered = json.dumps(payload)
    for dropped in ("private-preview-field", "private-anchor-field", "PRIVATE_KEY_NAME", "private-env-value"):
        assert dropped not in rendered, dropped


def test_preview_history_read_needs_exactly_one_selector() -> None:
    for argv in (
        ["preview-history-read"],
        ["preview-history-read", "--preview-id", "preview-x-pr-1", "--pr", "1"],
        ["preview-history-read", "--context", "example", "--pr", "7"],
    ):
        status, payload, calls = _run_product_read(argv, _preview_history_response())
        assert status != 0, argv
        assert calls == [], argv


def test_product_activity_read_reports_http_denial_as_read_error() -> None:
    denial = urllib.error.HTTPError(
        "https://launchplane.example.invalid/v1/products/example-product/activity",
        403,
        "Forbidden",
        hdrs=Message(),
        fp=io.BytesIO(
            json.dumps(
                {"trace_id": "launchplane_req_denied", "error": {"code": "authorization_denied"}}
            ).encode()
        ),
    )
    status, payload, _calls = _run_product_read(
        ["product-activity-read", "--product", "example-product"], denial
    )
    assert status == 1
    assert payload["status"] == "denied"
    assert payload["summary"]["error_code"] == "authorization_denied"
    assert "read was rejected" in payload["warnings"][0]["message"]


def _reconcile_requests_response() -> dict[str, object]:
    return {
        "status": "ok",
        "trace_id": "launchplane_req_reconcile_requests",
        "product": "example-product",
        "requests": [
            {
                "target_key": "example-product:preview:7",
                "target_kind": "preview",
                "pull_request_number": 7,
                "state": "failed",
                "requested_at": "2026-09-30T18:48:00Z",
                "updated_at": "2026-09-30T18:49:00Z",
                "request_count": 2,
                "attempt": 1,
                "last_delivery_id": "7d0e5c10-9e8f-11f0-8a2b-3c1d2e4f5a6b",
                "last_error": "Preview data workflow failed: [redacted-secret]",
                "last_plan": {
                    "target": "preview",
                    "action": "apply",
                    "held": False,
                    "head_sha": "cdd8f4a0d68be3575389fdffbcd6ef138ca13cc9",
                    "desired_image_digest": "sha256:" + "d5da36c3" * 8,
                    "preview_plan_id": "odoo-preview-plan-" + "ab12" * 16,
                    "preview_url": "https://pr-7.example.invalid",
                    "omitted_integration_credential_keys": ["EXAMPLE_SMTP_PASSWORD"],
                    "rejected_builds": [{"run_id": 1}],
                    "provider": {"response": "private provider text"},
                    "provider_response": "Private customer migration failed",
                },
            },
            {"target_key": "has spaces in key"},
        ],
    }


def test_reconcile_requests_read_keeps_the_decision_and_drops_the_rest() -> None:
    argv = ["reconcile-requests-read", "--product", "example-product"]
    status, payload, calls = _run_product_read(argv, _reconcile_requests_response())

    assert status == 0
    assert contract.LOCAL_EXTENSION_ROUTES["reconcile-requests-read"]["method"] == "GET"
    assert calls[0]["path"] == "/v1/product-profiles/example-product/reconcile-requests"
    result = payload["result"]
    (request,) = result["requests"]
    assert request["state"] == "failed"
    assert request["last_delivery_id"] == "7d0e5c10-9e8f-11f0-8a2b-3c1d2e4f5a6b"
    assert request["last_error"] == "Preview data workflow failed: [redacted-secret]"
    plan = request["last_plan"]
    assert plan["head_sha"] == "cdd8f4a0d68be3575389fdffbcd6ef138ca13cc9"
    assert plan["desired_image_digest"] == "sha256:" + "d5da36c3" * 8
    assert plan["preview_plan_id"] == "odoo-preview-plan-" + "ab12" * 16
    assert plan["preview_url"] == "https://pr-7.example.invalid"
    assert plan["omitted_integration_keys"] == ["EXAMPLE_SMTP_PASSWORD"]
    assert set(plan) == {
        "target",
        "action",
        "held",
        "head_sha",
        "desired_image_digest",
        "preview_plan_id",
        "preview_url",
        "omitted_integration_keys",
    }
    assert result["omitted_request_count"] == 1
    assert result["dropped_field_paths"] == [
        "requests[].last_plan.<unlisted field>",
        "requests[].target_key",
    ]
    rendered = json.dumps(payload)
    assert "private provider text" not in rendered
    assert "Private customer" not in rendered

    odd = _reconcile_requests_response()
    first = cast(list[dict[str, Any]], odd["requests"])[0]
    first["last_error"] = {"code": "build_failed"}
    first["last_plan"]["preview_url"] = "https://pr-7.example.invalid/access/private-value?k=v"
    status, payload, _calls = _run_product_read(argv, odd)
    assert status == 0
    (request,) = payload["result"]["requests"]
    assert request["last_error"] == ""
    assert request["last_plan"]["preview_url"] == "https://pr-7.example.invalid"
    assert "private-value" not in json.dumps(payload)

    first["last_plan"]["preview_url"] = "https://[broken"
    status, payload, _calls = _run_product_read(argv, odd)
    assert status == 0
    assert payload["result"]["requests"][0]["last_plan"]["preview_url"] == ""

    secret = _reconcile_requests_response()
    cast(list[dict[str, object]], secret["requests"])[0]["last_error"] = "token ghp_abcdefghijklmnop"
    status, payload, _calls = _run_product_read(argv, secret)
    assert status == 1
    assert payload["status"] == "invalid"
    assert not payload["result"]


def test_reconcile_requests_read_keeps_a_summary_that_lists_many_keys() -> None:
    keys = ", ".join(f"EXAMPLE_TUNING_SETTING_{index:02d}" for index in range(32))
    summary = (
        "The replacement plan was blocked before the deploy started. Blocker: The lane "
        f"configures settings its product profile does not declare. Keys: {keys}."
    )
    plan = {
        "target": "testing",
        "action": "deploy",
        "last_failed_error_code": "plan_not_ready.runtime_keys_undeclared",
        "last_failed_error_summary": summary,
    }
    response = {
        "status": "ok",
        "product": "example-product",
        "requests": [{"target_key": "example-product:testing", "last_plan": plan}],
    }
    argv = ["reconcile-requests-read", "--product", "example-product"]
    status, payload, _calls = _run_product_read(argv, response)

    assert status == 0
    (request,) = payload["result"]["requests"]
    assert request["last_plan"]["last_failed_error_summary"] == summary


def test_reconcile_requests_read_keeps_testing_operation_ids() -> None:
    plan = {
        "target": "testing",
        "action": "deploy",
        "held": False,
        "deferred": "lane_busy",
        "owner_review_requested": True,
        "desired_artifact_id": "artifact-example-abc123",
        "queued_operation_id": "odoo-target-replacement-example-testing-2",
        "active_operation_id": "odoo-target-replacement-example-testing-3",
        "deployed_operation_id": "odoo-target-replacement-example-testing-0",
        "last_failed_operation_id": "odoo-target-replacement-example-testing-1",
        "last_failed_error_code": "health_check_failed",
        "last_failed_error_summary": "Health check did not pass within the wait window.",
        "missing_keys": ["EXAMPLE_ODOO_ADMIN_LOGIN"],
        "rejected_builds": [{"commit": "abc123", "error": "Private build failure text"}],
        "pr_feedback": {"error": "Private feedback text"},
    }
    response = {
        "status": "ok",
        "product": "example-product",
        "requests": [{"target_key": "example-product:testing", "last_plan": plan}],
    }
    argv = ["reconcile-requests-read", "--product", "example-product"]
    status, payload, _calls = _run_product_read(argv, response)

    assert status == 0
    (request,) = payload["result"]["requests"]
    kept = request["last_plan"]
    for name in (
        "deferred",
        "owner_review_requested",
        "queued_operation_id",
        "active_operation_id",
        "deployed_operation_id",
        "last_failed_operation_id",
        "last_failed_error_code",
        "last_failed_error_summary",
        "missing_keys",
    ):
        assert kept[name] == plan[name], name
    assert "rejected_builds" not in kept
    assert "pr_feedback" not in kept
    assert payload["result"]["dropped_field_count"] == 2
    rendered = json.dumps(payload)
    assert "Private build failure text" not in rendered
    assert "Private feedback text" not in rendered


def test_reconcile_requests_read_keeps_generic_web_testing_outcome() -> None:
    import hashlib

    key = "product-reconcile:example-product:example:testing:sha256:abc:from-deployment-1"
    plan: dict[str, Any] = {
        "target": "testing",
        "deploy_operation_status": "replayed",
        "deploy_status": "fail",
        "post_deploy_status": "skipped",
        "deployment_record_id": "deployment-example-testing-1",
        "deploy_idempotency_key": key,
        "driver_message": "Private provider failure text",
    }
    response = {
        "status": "ok",
        "product": "example-product",
        "requests": [{"target_key": "example-product:testing", "last_plan": plan}],
    }
    argv = ["reconcile-requests-read", "--product", "example-product"]
    status, payload, _calls = _run_product_read(argv, response)
    assert status == 0
    kept = payload["result"]["requests"][0]["last_plan"]
    for field in ("deploy_operation_status", "deploy_status", "post_deploy_status", "deployment_record_id"):
        assert kept[field] == plan[field]
    assert kept["deploy_key_sha256"] == hashlib.sha256(key.encode()).hexdigest()
    assert "deploy_idempotency_key" not in kept
    assert "driver_message" not in kept
    assert payload["result"]["dropped_field_count"] == 1
    assert key not in json.dumps(payload)
    assert "Private provider failure text" not in json.dumps(payload)

    # A new starting deployment produces a distinct comparison value.
    plan["deploy_idempotency_key"] = key + "-next"
    status, next_payload, _calls = _run_product_read(argv, response)
    assert status == 0
    assert next_payload["result"]["requests"][0]["last_plan"]["deploy_key_sha256"] != kept["deploy_key_sha256"]

    for field in ("deploy_operation_status", "deploy_status", "post_deploy_status"):
        plan[field] = "Private provider text with spaces"
    plan["deployment_record_id"] = {"message": "Private record text"}
    plan["deploy_idempotency_key"] = ["Private key text"]
    status, payload, _calls = _run_product_read(argv, response)
    assert status == 0
    kept = payload["result"]["requests"][0]["last_plan"]
    assert all(kept[field] == "" for field in (
        "deploy_operation_status", "deploy_status", "post_deploy_status",
        "deployment_record_id", "deploy_key_sha256",
    ))
    assert "Private" not in json.dumps(payload)


def _target_replacement_operation_response() -> dict[str, Any]:
    operation_id = "odoo-target-replacement-example-testing-20261001T021400Z-0123456789abcdef"
    result = {
        "schema_version": 1,
        "product": "example-product",
        "context": "example",
        "instance": "testing",
        "strategy": "recreate-in-place",
        "deployment_record_id": "deployment-example-testing-7",
        "release_tuple_id": "",
        "deploy_status": "pass",
        "post_deploy_status": "fail",
        "health_status": "skipped",
        "canonical_status": "skipped",
        "logo_status": "skipped",
        "health_url": "https://testing.example.invalid/web/health",
        "canonical_url": "https://testing.example.invalid",
        "logo_urls": ["https://testing.example.invalid/logo.png"],
        "verification_evidence": {"detail": "private verification detail"},
        "post_deploy_override_evidence": {"EXAMPLE_FLAG": "private-override-value"},
        "target_id": "provider-target-id-77",
        "target_name": "private-target-name",
        "artifact_id": "artifact-example-abc123",
        "image_reference": "ghcr.io/example/private-image@sha256:" + "e" * 64,
        "runtime_source": {"EXAMPLE_SETTING": "private-runtime-value"},
        "error_message": "Post-deploy update failed on private-target-name.",
    }
    return {
        "status": "ok",
        "trace_id": "launchplane_req_target_replacement",
        "operation": {
            "schema_version": 2,
            "operation_id": operation_id,
            "product": "example-product",
            "context": "example",
            "instance": "testing",
            "idempotency_key": "launchplane-reconcile:private-key",
            "idempotency_scope": "reconcile:example-product",
            "request_fingerprint": "f" * 64,
            "request": {
                "product": "example-product",
                "instance": "testing",
                "strategy": "recreate-in-place",
                "artifact_id": "artifact-example-abc123",
                "source_git_ref": "abc123",
                "confirmation": "private confirmation phrase",
            },
            "authorization": {"action": "odoo_target_replacement_apply.execute"},
            "status": "fail",
            "phase": "post_deploy",
            "deployment_record_id": "deployment-example-testing-7",
            "created_at": "2026-10-01T02:14:00Z",
            "updated_at": "2026-10-01T02:20:00Z",
            "started_at": "2026-10-01T02:14:05Z",
            "finished_at": "2026-10-01T02:20:00Z",
            "lease_owner": "private-worker-host",
            "lease_expires_at": "",
            "heartbeat_at": "2026-10-01T02:19:30Z",
            "attempt": 1,
            "result": result,
            "cancellation": None,
            "error_code": "post_deploy_failed",
            "error_message": (
                "Deploy failed on private-target-name: Connection refused by "
                "db.internal.example:5432; login=\"correct horse battery staple\""
            ),
            "runner_trace_id": "runner-1",
            "poll_url": f"/v1/drivers/odoo/target-replacement/operations/{operation_id}",
            "private_extension": {"value": "private extension value"},
        },
        "result": result,
    }


def test_target_replacement_operation_read_keeps_progress_and_drops_error_text() -> None:
    response = _target_replacement_operation_response()
    operation_id = response["operation"]["operation_id"]
    argv = ["target-replacement-operation-read", "--operation-id", operation_id]
    status, payload, calls = _run_product_read(argv, response)

    assert status == 0
    route = contract.LOCAL_EXTENSION_ROUTES["target-replacement-operation-read"]
    assert route["method"] == "GET"
    assert calls[0]["path"] == route["path"].format(operation_id=operation_id)
    assert calls[0]["query"] == {}
    assert payload["request"] == {
        "operation_id": operation_id,
        "payload_source": "operator_argument",
    }
    operation = payload["result"]["operation"]
    assert operation["operation_id"] == operation_id
    assert (operation["status"], operation["phase"], operation["attempt"]) == (
        "fail",
        "post_deploy",
        1,
    )
    assert operation["artifact_id"] == "artifact-example-abc123"
    assert operation["started_at"] == "2026-10-01T02:14:05Z"
    assert operation["error_code"] == "post_deploy_failed"
    assert "error_message" not in operation
    result = payload["result"]["result"]
    assert result["post_deploy_status"] == "fail"
    assert result["image_digest"] == "sha256:" + "e" * 64
    assert "error_message" not in result
    paths = payload["result"]["dropped_field_paths"]
    for path in (
        "operation.error_message",
        "result.error_message",
        "operation.idempotency_key",
        "operation.authorization",
        "operation.lease_owner",
        "operation.poll_url",
        "operation.<unlisted field>",
        "operation.request.confirmation",
        "result.health_url",
        "result.target_name",
        "result.runtime_source",
        "result.image_reference",
    ):
        assert path in paths, path
    rendered = json.dumps(payload)
    for private in (
        "private-target-name",
        "provider-target-id-77",
        "private-image",
        "private-key",
        "private-worker-host",
        "private confirmation phrase",
        "private extension value",
        "private-override-value",
        "private-runtime-value",
        "private verification detail",
        "db.internal.example",
        "battery staple",
        "example.invalid",
    ):
        assert private not in rendered, private


def test_target_replacement_operation_read_projects_failure_details() -> None:
    response = _target_replacement_operation_response()
    source = response["operation"]
    source.update(
        error_code="deploy_blocked.compose_keys_missing",
        error_description="The compose template requires settings the target does not provide.",
        error_detail_keys=["EXAMPLE_SETTING", "EXAMPLE_FLAG"],
    )
    # operations.read returns a structured view with no request or embedded result.
    source.pop("request")
    source.pop("result")
    source["free_text_omitted"] = True
    response["result"] = None
    argv = ["target-replacement-operation-read", "--operation-id", source["operation_id"]]
    status, payload, _calls = _run_product_read(argv, response)
    assert status == 0
    operation = payload["result"]["operation"]
    for name in ("error_code", "error_description", "error_detail_keys"):
        assert operation[name] == source[name]
        assert f"operation.{name}" not in payload["result"]["dropped_field_paths"]
    assert "error_message" not in operation
    assert "operation.free_text_omitted" in payload["result"]["dropped_field_paths"]
    source["error_description"] = (
        "A setting the site's records would carry is a platform credential, which never "
        "belongs in an app runtime."
    )
    status, payload, _calls = _run_product_read(argv, response)
    assert status == 0
    description = payload["result"]["operation"]["error_description"]
    assert description == source["error_description"].replace("credential", "[redacted]")
    assert "operation.error_description" not in payload["result"]["dropped_field_paths"]


def test_target_replacement_operation_read_bounds_failure_details() -> None:
    response = _target_replacement_operation_response()
    source = response["operation"]
    source.update(
        error_description="x" * 501,
        error_detail_keys=["EXAMPLE_SETTING", "not a key", {"unexpected": "detail"}],
    )
    argv = ["target-replacement-operation-read", "--operation-id", source["operation_id"]]
    status, payload, _calls = _run_product_read(argv, response)
    assert status == 0
    operation = payload["result"]["operation"]
    assert operation["error_description"] == ""
    assert operation["error_detail_keys"] == ["EXAMPLE_SETTING"]
    assert "operation.error_description" in payload["result"]["dropped_field_paths"]
    assert "operation.error_detail_keys[]" in payload["result"]["dropped_field_paths"]
    for description in ("credential=hunter2-example", "credential : hunter2-example"):
        source["error_description"] = description
        status, payload, _calls = _run_product_read(argv, response)
        assert status == 0
        assert payload["result"]["operation"]["error_description"] == ""
        assert "hunter2-example" not in json.dumps(payload)
        assert "operation.error_description" in payload["result"]["dropped_field_paths"]
    for value in (None, [], "not a list", {"unexpected": "detail"}):
        source["error_detail_keys"] = value
        status, payload, _calls = _run_product_read(argv, response)
        assert status == 0
        assert payload["result"]["operation"]["error_detail_keys"] == []
    source["error_detail_keys"] = [f"EXAMPLE_{i}" for i in range(300)]
    status, payload, _calls = _run_product_read(argv, response)
    assert status == 0
    assert payload["result"]["operation"]["error_detail_keys"] == source["error_detail_keys"][
        :write_action.TARGET_REPLACEMENT_PLAN_MAX_LIST_ITEMS
    ]
    assert "operation.error_detail_keys[]" in payload["result"]["dropped_field_paths"]


def test_target_replacement_operation_read_tolerates_a_pending_operation() -> None:
    response = _target_replacement_operation_response()
    operation = response["operation"]
    operation.update(status="pending", phase="created", error_code="", error_message="")
    operation.update(result=None, started_at="", finished_at="", heartbeat_at="")
    response["result"] = None
    argv = ["target-replacement-operation-read", "--operation-id", operation["operation_id"]]
    status, payload, _calls = _run_product_read(argv, response)
    assert status == 0
    assert payload["result"]["result"] is None
    assert payload["result"]["operation"]["status"] == "pending"
    assert payload["result"]["operation"]["error_description"] == ""
    assert payload["result"]["operation"]["error_detail_keys"] == []
    assert "operation.error_message" not in payload["result"]["dropped_field_paths"]


def test_target_replacement_operation_read_refuses_bad_ids_and_unsafe_values() -> None:
    for operation_id in ("../admin", "a/b", "x?y=1"):
        status, payload, calls = _run_product_read(
            ["target-replacement-operation-read", "--operation-id", operation_id],
            _target_replacement_operation_response(),
        )
        assert status == 2
        assert calls == []
        assert payload["warnings"][0]["code"] == "invalid_operation_id"

    response = _target_replacement_operation_response()
    argv = ["target-replacement-operation-read", "--operation-id", response["operation"]["operation_id"]]
    for mutate in (
        lambda body: body.update(extra="x"),
        lambda body: body["operation"].update(deployment_record_id="Bearer abcdefghijklmnop"),
        lambda body: body["operation"].update(product=None),
        lambda body: body["operation"].update(error_description="Bearer abcdefghijklmnop"),
        lambda body: body["operation"].update(error_detail_keys=["ghp_planted_example"]),
        lambda body: body["operation"].update(error_detail_keys=[{"token": "planted"}]),
    ):
        mutated = _target_replacement_operation_response()
        mutate(mutated)
        status, payload, _calls = _run_product_read(argv, mutated)
        assert status == 1
        assert payload["status"] == "invalid"
        assert not payload["result"]


def _target_replacement_plan_response() -> dict[str, Any]:
    return {
        "status": "accepted",
        "trace_id": "launchplane_req_replacement_plan",
        "records": {},
        "result": {
            "plan_status": "blocked",
            "product": "example-product",
            "context": "example",
            "instance": "testing",
            "strategy": "recreate-in-place",
            "target_record_found": True,
            "target_id_record_found": True,
            "inventory_found": True,
            "current_target": {
                "target_type": "compose",
                "target_id": "provider-target-id-77",
                "target_name": "private-target-name",
                "project_name": "private-project",
                "domain_hosts": ["testing.example.invalid"],
                "env_keys": ["EXAMPLE_DB_NAME", "EXAMPLE_LEFTOVER", "BAD KEY=private-env-value"],
                "required_volume_keys_present": ["ODOO_DATA_VOLUME"],
                "required_volume_keys_missing": ["ODOO_LOG_VOLUME"],
                "live_volume_values": {"ODOO_DATA_VOLUME": "private-volume-value"},
                "runtime_identity_present": False,
            },
            "expected_next_target_name": "private-next-target",
            "expected_domain_hosts": ["next.example.invalid"],
            "expected_artifact_id": "",
            "data_source_mode": "existing",
            "approval_issue_url": "https://github.com/example/private/issues/1",
            "retired_provider_keys": ["EXAMPLE_RETIRED"],
            "delivered_runtime_keys": ["EXAMPLE_DB_NAME", "EXAMPLE_SITE_FLAG"],
            "blockers": ["Provider key EXAMPLE_LEFTOVER on private-target-name is not recorded."],
            "blocker_codes": ["provider_keys_unrecorded"],
            "blocker_keys": {"provider_keys_unrecorded": ["EXAMPLE_LEFTOVER"]},
            "warnings": ["Current target does not expose a runtime identity at private-host."],
            "steps": [
                {"step_id": "resolve_target", "status": "ready", "message": "private-target-name"},
                {"step_id": "deliver_env", "status": "blocked", "message": "private step text"},
            ],
        },
    }


def _run_plan_read(
    argv: list[str], response: object
) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def fake_post(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if isinstance(response, BaseException):
            raise response
        return response

    settings = {"service_url": "https://launchplane.example.invalid", "token": "t"}
    output = io.StringIO()
    with (
        temporary_attribute(write_action, "prepare_operator_settings", lambda **_kwargs: settings),
        temporary_attribute(write_action, "request_launchplane", fake_post),
        redirect_stdout(output),
    ):
        status = write_action.main(argv)
    return status, json.loads(output.getvalue()), calls


PLAN_READ_ARGV = [
    "target-replacement-plan-read",
    "--product",
    "example-product",
    "--instance",
    "testing",
]


def test_target_replacement_plan_read_keeps_key_names_and_drops_text() -> None:
    status, payload, calls = _run_plan_read(PLAN_READ_ARGV, _target_replacement_plan_response())

    assert status == 0
    route = contract.LOCAL_EXTENSION_ROUTES["target-replacement-plan-read"]
    assert route["method"] == "POST"
    assert calls[0]["path"] == route["path"]
    assert calls[0]["body"] == {
        "schema_version": 1,
        "product": "example-product",
        "replacement": {"product": "example-product", "instance": "testing"},
    }
    assert not calls[0].get("idempotency_key")
    plan = payload["result"]
    assert (plan["plan_status"], plan["instance"], plan["data_source_mode"]) == (
        "blocked",
        "testing",
        "existing",
    )
    assert plan["delivered_runtime_keys"] == ["EXAMPLE_DB_NAME", "EXAMPLE_SITE_FLAG"]
    assert plan["retired_provider_keys"] == ["EXAMPLE_RETIRED"]
    assert plan["blocker_codes"] == ["provider_keys_unrecorded"]
    assert plan["blocker_keys"] == {"provider_keys_unrecorded": ["EXAMPLE_LEFTOVER"]}
    assert (plan["blocker_count"], plan["warning_count"]) == (1, 1)
    assert plan["current_target"] == {
        "env_keys": ["EXAMPLE_DB_NAME", "EXAMPLE_LEFTOVER"],
        "required_volume_keys_missing": ["ODOO_LOG_VOLUME"],
    }
    assert plan["steps"] == [
        {"step_id": "resolve_target", "status": "ready"},
        {"step_id": "deliver_env", "status": "blocked"},
    ]
    paths = plan["dropped_field_paths"]
    for path in (
        "plan.blockers",
        "plan.warnings",
        "plan.expected_next_target_name",
        "plan.expected_domain_hosts",
        "plan.approval_issue_url",
        "plan.current_target.target_id",
        "plan.current_target.target_name",
        "plan.current_target.domain_hosts",
        "plan.current_target.live_volume_values",
        "plan.current_target.env_keys[]",
        "plan.steps[].message",
    ):
        assert path in paths, path
    rendered = json.dumps(payload)
    for private in (
        "private-target-name",
        "provider-target-id-77",
        "private-project",
        "private-next-target",
        "private-volume-value",
        "private-env-value",
        "private step text",
        "private-host",
        "example.invalid",
        "github.com",
    ):
        assert private not in rendered, private


def test_target_replacement_plan_read_marks_missing_key_lists_as_unreported() -> None:
    response = _target_replacement_plan_response()
    result = response["result"]
    del result["delivered_runtime_keys"]
    result.update(plan_status="ready", blockers=[], blocker_codes=[], blocker_keys={}, warnings=[])
    status, payload, _calls = _run_plan_read(PLAN_READ_ARGV, response)
    assert status == 0
    plan = payload["result"]
    assert plan["delivered_runtime_keys"] is None
    result["delivered_runtime_keys"] = None
    _status, payload, _calls = _run_plan_read(PLAN_READ_ARGV, response)
    assert payload["result"]["delivered_runtime_keys"] is None
    result["delivered_runtime_keys"] = []
    _status, payload, _calls = _run_plan_read(PLAN_READ_ARGV, response)
    assert payload["result"]["delivered_runtime_keys"] == []
    assert plan["retired_provider_keys"] == ["EXAMPLE_RETIRED"]
    assert (plan["blocker_codes"], plan["blocker_keys"], plan["blocker_count"]) == ([], {}, 0)


def test_target_replacement_plan_read_reports_denial_with_trace() -> None:
    denial = urllib.error.HTTPError(
        "https://launchplane.example.invalid/v1/drivers/odoo/target-replacement-plan",
        403,
        "Forbidden",
        hdrs=Message(),
        fp=io.BytesIO(
            json.dumps(
                {"trace_id": "launchplane_req_denied", "error": {"code": "authorization_denied"}}
            ).encode()
        ),
    )
    status, payload, _calls = _run_plan_read(PLAN_READ_ARGV, denial)
    assert status == 1
    assert payload["status"] == "denied"
    assert payload["summary"]["trace_id"] == "launchplane_req_denied"
    assert payload["summary"]["error_code"] == "authorization_denied"
    assert "read was rejected" in payload["warnings"][0]["message"]


def test_target_replacement_plan_read_refuses_bad_input_and_unsafe_values() -> None:
    for instance in ("../admin", "a/b", "x y"):
        argv = [*PLAN_READ_ARGV[:-1], instance]
        status, payload, calls = _run_plan_read(argv, _target_replacement_plan_response())
        assert status == 2, instance
        assert calls == [], instance

    for mutate in (
        lambda body: body.update(extra="x"),
        lambda body: body.update(records={"deployment": "x"}),
        lambda body: body["result"].update(expected_artifact_id="Bearer abcdefghijklmnop"),
        lambda body: body.update(result=None),
    ):
        mutated = _target_replacement_plan_response()
        mutate(mutated)
        status, payload, _calls = _run_plan_read(PLAN_READ_ARGV, mutated)
        assert status == 1
        assert payload["status"] == "invalid"
        assert not payload["result"]

    # A blocker code that reads like a secret field name is dropped by path, never echoed.
    response = _target_replacement_plan_response()
    response["result"]["blocker_keys"]["token_keys"] = ["EXAMPLE"]
    status, payload, _calls = _run_plan_read(PLAN_READ_ARGV, response)
    assert status == 0
    assert "token_keys" not in json.dumps(payload)
    assert "plan.blocker_keys.<unlisted field>" in payload["result"]["dropped_field_paths"]


def _testing_hold_args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "product": "example-product",
        "context": "Example",
        "instance": "testing",
        "hold": True,
        "reason": "Staff are testing the checkout flow.",
        "idempotency_key": "",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_testing_hold_body_binds_apply_to_saved_dry_run() -> None:
    dry_run = write_action.testing_hold_body(_testing_hold_args(), mode="dry-run")
    assert dry_run["mode"] == "dry-run"
    assert dry_run["hold"] is True
    assert dry_run["context"] == "example"
    assert "reviewed_plan_sha256" not in dry_run
    assert write_action.testing_hold_body(_testing_hold_args(hold=False), mode="dry-run")["hold"] is False
    _expect_error(
        lambda: write_action.testing_hold_body(_testing_hold_args(reason=" "), mode="dry-run"),
        "reason_required",
    )

    with TemporaryDirectory(dir=Path.home()) as directory:
        evidence_path = Path(directory) / "testing-hold-dry-run.json"
        evidence_path.write_text(
            json.dumps(
                _saved_dry_run_output(
                    "testing-hold-dry-run", _testing_hold_response(_testing_hold_plan())
                )
            ),
            encoding="utf-8",
        )
        apply_args = _testing_hold_args(
            idempotency_key="testing-hold-1",
            reviewed_dry_run=True,
            expected_plan_digest="B" * 64,
            dry_run_evidence_file=str(evidence_path),
        )
        body = write_action.testing_hold_body(apply_args, mode="apply")
        assert body["mode"] == "apply"
        assert body["reviewed_plan_sha256"] == "b" * 64

        for overrides, code in (
            ({"reviewed_dry_run": False}, "reviewed_dry_run_required"),
            ({"idempotency_key": ""}, "idempotency_key_required"),
            ({"expected_plan_digest": "short"}, "invalid_expected_plan_digest"),
            ({"expected_plan_digest": "d" * 64}, "reviewed_dry_run_not_apply_eligible"),
            ({"dry_run_evidence_file": ""}, "reviewed_dry_run_not_apply_eligible"),
            ({"hold": False}, "reviewed_dry_run_not_apply_eligible"),
            ({"context": "other"}, "reviewed_dry_run_not_apply_eligible"),
            ({"reason": "A reason nobody reviewed"}, "reviewed_dry_run_not_apply_eligible"),
        ):
            args = argparse.Namespace(**{**vars(apply_args), **overrides})
            _expect_error(lambda candidate=args: write_action.testing_hold_body(candidate, mode="apply"), code)


def test_testing_hold_cli_dispatches_local_extension_route() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        evidence_path = Path(directory) / "testing-hold-dry-run.json"
        lift_plan = _testing_hold_plan(
            action="clear", before=_testing_hold_plan()["after"], after=None, reason="Testing done."
        )
        evidence_path.write_text(
            json.dumps(_saved_dry_run_output("testing-hold-dry-run", _testing_hold_response(lift_plan))),
            encoding="utf-8",
        )
        calls: list[dict[str, Any]] = []
        lane = ["--product", "example-product", "--context", "example", "--instance", "testing"]
        with temporary_attribute(write_action, "execute_post", lambda **kwargs: calls.append(kwargs) or 0):
            assert write_action.main(["testing-hold-dry-run", *lane, "--lift", "--reason", "Testing done."]) == 0
            assert (
                write_action.main(
                    [
                        "testing-hold-apply",
                        *lane,
                        "--lift",
                        "--reason",
                        "Testing done.",
                        "--idempotency-key",
                        "testing-hold-lift-1",
                        "--reviewed-dry-run",
                        "--expected-plan-digest",
                        "b" * 64,
                        "--dry-run-evidence-file",
                        str(evidence_path),
                    ]
                )
                == 0
            )
    route = contract.LOCAL_EXTENSION_ROUTES["testing-hold-apply"]["path"]
    assert [call["path"] for call in calls] == [route, route]
    assert [call["body"]["mode"] for call in calls] == ["dry-run", "apply"]
    assert all(call["body"]["hold"] is False for call in calls)
    assert "Testing done." not in json.dumps([call["request"] for call in calls])


def _repository_identity_plan(**overrides: object) -> dict[str, object]:
    plan: dict[str, object] = {
        "status": "ok",
        "mode": "dry-run",
        "product": "example-product",
        "repository": "example-owner/example-repo",
        "operation": "record",
        "identity_before": {"repository_id": "", "repository_owner_id": ""},
        "identity_after": {"repository_id": "123456", "repository_owner_id": "7890"},
        "inventory_record_id": "repository-inventory-123456-1",
        "inventory_revision": 1,
        "inventory_digest": "e" * 64,
        "changed": True,
        "applied": False,
        "reason": "Route GitHub events by repository id.",
        "source_label": "service:product-repository-identity",
        "profile_record_sha256_before": "f" * 64,
        "profile_updated_at_before": "2026-09-29T12:00:00Z",
        "profile_updated_at_after": "",
        "plan_sha256": "b" * 64,
    }
    plan.update(overrides)
    return plan


def _repository_identity_response(plan: dict[str, object]) -> dict[str, object]:
    return {
        "status": "accepted",
        "trace_id": "launchplane_req_repository_identity",
        "records": {"product_profile": "example-product", "repository_inventory": "repository-inventory-123456-1"},
        "result": plan,
    }


def test_product_repository_identity_projection_is_bounded_and_fail_closed() -> None:
    result = _saved_dry_run_output(
        "product-repository-identity-dry-run", _repository_identity_response(_repository_identity_plan())
    )
    assert result["summary"]["plan_sha256"] == "b" * 64
    assert result["result"]["identity_after"] == {"repository_id": "123456", "repository_owner_id": "7890"}
    assert "read_back" not in result["result"]
    applied = write_action._project_product_repository_identity_plan(
        _repository_identity_plan(
            mode="apply",
            applied=True,
            read_back={"repository_id": "123456", "repository_owner_id": "7890"},
            read_back_matches=True,
        )
    )
    assert applied["read_back_matches"] is True

    for plan, code in (
        (_repository_identity_plan(extra="x"), "unsafe_response_shape"),
        (_repository_identity_plan(operation="overwrite"), "invalid_response"),
        (_repository_identity_plan(inventory_revision=True), "invalid_response"),
        (
            _repository_identity_plan(identity_after={"repository_id": "abc", "repository_owner_id": "1"}),
            "invalid_response",
        ),
        (
            _repository_identity_plan(identity_after={"repository_id": "1", "owner_token": "x"}),
            "unsafe_response_shape",
        ),
        (_repository_identity_plan(inventory_digest="not-a-digest"), "invalid_response"),
        (
            _repository_identity_plan(
                identity_after={"repository_id": "1" * 21, "repository_owner_id": "1"}
            ),
            "invalid_response",
        ),
    ):
        _expect_error(
            lambda candidate=plan: write_action._project_product_repository_identity_plan(candidate), code
        )


def test_product_repository_identity_apply_requires_saved_dry_run() -> None:
    base = argparse.Namespace(product="example-product", reason="Route GitHub events.", idempotency_key="")
    dry_run = write_action.product_repository_identity_body(base, mode="dry-run")
    assert dry_run == {
        "schema_version": 1,
        "product": "example-product",
        "mode": "dry-run",
        "reason": "Route GitHub events.",
    }

    with TemporaryDirectory(dir=Path.home()) as directory:
        evidence_path = Path(directory) / "repository-identity-dry-run.json"
        evidence_path.write_text(
            json.dumps(
                _saved_dry_run_output(
                    "product-repository-identity-dry-run",
                    _repository_identity_response(_repository_identity_plan()),
                )
            ),
            encoding="utf-8",
        )
        hold_evidence_path = Path(directory) / "testing-hold-dry-run.json"
        hold_evidence_path.write_text(
            json.dumps(
                _saved_dry_run_output("testing-hold-dry-run", _testing_hold_response(_testing_hold_plan()))
            ),
            encoding="utf-8",
        )
        apply_args = argparse.Namespace(
            product="example-product",
            reason="Route GitHub events.",
            idempotency_key="repository-identity-1",
            reviewed_dry_run=True,
            expected_plan_digest="b" * 64,
            dry_run_evidence_file=str(evidence_path),
        )
        assert write_action.product_repository_identity_body(apply_args, mode="apply")[
            "reviewed_plan_sha256"
        ] == "b" * 64
        for overrides, code in (
            ({"reviewed_dry_run": False}, "reviewed_dry_run_required"),
            ({"idempotency_key": " "}, "idempotency_key_required"),
            ({"expected_plan_digest": "d" * 64}, "reviewed_dry_run_not_apply_eligible"),
            ({"product": "other-product"}, "reviewed_dry_run_not_apply_eligible"),
            # Another operation's evidence with the same digest is not this plan's review.
            ({"dry_run_evidence_file": str(hold_evidence_path)}, "reviewed_dry_run_not_apply_eligible"),
        ):
            args = argparse.Namespace(**{**vars(apply_args), **overrides})
            _expect_error(
                lambda candidate=args: write_action.product_repository_identity_body(candidate, mode="apply"), code
            )

        calls: list[dict[str, Any]] = []
        with temporary_attribute(write_action, "execute_post", lambda **kwargs: calls.append(kwargs) or 0):
            assert (
                write_action.main(
                    ["product-repository-identity-dry-run", "--product", "example-product", "--reason", "Route GitHub events."]
                )
                == 0
            )
    route = contract.LOCAL_EXTENSION_ROUTES["product-repository-identity-apply"]["path"]
    assert calls[0]["path"] == route
    assert calls[0]["body"]["mode"] == "dry-run"


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


def test_runtime_retirement_projection_rejects_values_and_malformed_metadata() -> None:
    runtime: dict[str, Any] = {
        "action": "updated",
        "scope": "instance",
        "context": "testing",
        "instance": "example-instance",
    }
    for field in (
        "retired_provider_keys_before",
        "retired_provider_keys_after",
        "record",
    ):
        for invalid in (
            None,
            "LEGACY_KEY",
            {"LEGACY_KEY": "private-value"},
            ["Bearer private-example"],
            ["https://private.example.invalid"],
            [42],
            ["LEGACY_KEY", "LEGACY_KEY"],
            ["K" * 129],
            [f"KEY_{index}" for index in range(257)],
        ):
            candidate = {
                **runtime,
                field: {"retired_provider_keys": invalid} if field == "record" else invalid,
            }
            try:
                write_action._project_runtime_environment(candidate)
            except safety.LaunchplaneSafetyError:
                pass
            else:
                raise AssertionError("expected malformed retirement metadata to fail closed")

    for extra in (
        {"record": {"retired_provider_keys": ["LEGACY_KEY"], "env": {"KEY": "value"}}},
        {"provider_environment": {"KEY": "value"}},
        {"scope": "context", "instance": "", "retired_provider_keys_after": ["LEGACY_KEY"]},
    ):
        try:
            write_action._project_runtime_environment({**runtime, **extra})
        except safety.LaunchplaneSafetyError:
            pass
        else:
            raise AssertionError("expected unsafe retirement response to fail closed")

    cleared = write_action._project_runtime_environment(
        {**runtime, "retired_provider_keys_before": ["LEGACY_KEY"], "retired_provider_keys_after": []}
    )
    assert cleared["retired_provider_keys_before"] == ["LEGACY_KEY"]
    assert cleared["retired_provider_keys_after"] == []


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
    try:
        write_action.summarize_success(
            operation="merge-train-controller-run-once",
            request={"repository": "example/repo", "base_branch": "main", "mutate": True},
            provider_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_blocked_merge",
                "records": {},
                "result": {
                    "repository": "example/repo",
                    "base_branch": "main",
                    "controller_action": "block",
                    "blocking_reason": {
                        "code": "merge_readiness_not_ready",
                        "message": "Use Bearer secret-token before retrying.",
                    },
                    "merge_readiness": None,
                },
            },
        )
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == "unsafe_response_shape"
    else:
        raise AssertionError("expected unsafe merge blocking reason to fail closed")
    try:
        write_action.summarize_success(
            operation="merge-train-controller-run-once",
            request={"repository": "example/repo", "base_branch": "main", "mutate": True},
            provider_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_blocked_merge",
                "records": {},
                "result": {
                    "repository": "example/repo",
                    "base_branch": "main",
                    "controller_action": "block",
                    "blocking_reason": {
                        "code": "merge_readiness_not_ready",
                        "message": "Operator credential is unavailable.",
                    },
                    "merge_readiness": None,
                },
            },
        )
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == "invalid_response"
    else:
        raise AssertionError("expected denied merge blocking summary to fail closed")
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
    assert "block only the affected work" in recommendation
    assert "continue independent safe work" in recommendation
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


def test_expected_config_review_binds_metadata_and_never_prints_owner_instructions() -> None:
    with TemporaryDirectory() as directory:
        payload_path = Path(directory) / "metadata.json"
        evidence_path = Path(directory) / "review.json"
        requirement = {
            "integration": "runtime_environment", "binding_key": "SMTP_PASSWORD",
            "context": "example-site", "instance": "",
            "owner_input": {"label": "Mail credential", "instructions": "private-account-context"},
        }
        body = {"schema_version": 1, "product": "example-site", "reason": "Configure mail input.", "managed_secret_bindings": [requirement]}
        payload_path.write_text(json.dumps(body))
        calls: list[dict[str, Any]] = []

        def post(**kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {
                "status": "accepted", "trace_id": "launchplane_req_expected_config",
                "records": {"product_profile": "example-site"},
                "result": {
                    "status": "ok", "mode": kwargs["body"]["mode"], "product": "example-site",
                    "source_label": "operator", "changed": True,
                    "runtime_environment_keys": {"added": [], "unchanged": []},
                    "managed_secret_bindings": {"added": [requirement], "unchanged": []},
                    "summary": {"runtime_environment_key_add_count": 0, "managed_secret_binding_add_count": 1, "runtime_environment_key_unchanged_count": 0, "managed_secret_binding_unchanged_count": 0},
                },
            }

        with (
            temporary_attribute(write_action, "prepare_operator_settings", lambda **_kwargs: {"service_url": "https://launchplane.example.invalid", "token": "fixture-only"}),
            temporary_attribute(write_action, "request_launchplane", post),
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                assert write_action.main(["product-expected-config-dry-run", "--payload-file", str(payload_path)]) == 0
            evidence = json.loads(output.getvalue())
            assert "private-account-context" not in output.getvalue()
            assert "fixture-only" not in output.getvalue()
            assert evidence["result"]["managed_secret_bindings_added_count"] == 1
            evidence_path.write_text(output.getvalue())
            apply_args = ["product-expected-config-apply", "--payload-file", str(payload_path), "--dry-run-evidence-file", str(evidence_path), "--reviewed-dry-run", "--idempotency-key", "example-config-apply"]
            body["reason"] = "A different reviewed change."
            payload_path.write_text(json.dumps(body))
            with redirect_stdout(io.StringIO()):
                assert write_action.main(apply_args) == 2
            assert len(calls) == 1, "Changed metadata must not reach the service"
            body["reason"] = "Configure mail input."
            payload_path.write_text(json.dumps(body))
            with redirect_stdout(io.StringIO()):
                assert write_action.main(apply_args) == 0
            assert len(calls) == 2
            assert calls[-1]["path"] == "/v1/product-profiles/expected-config/apply"
            assert calls[-1]["body"]["mode"] == "apply"
            assert calls[-1]["body"]["managed_secret_bindings"] == [requirement]
            requirement["value"] = "must-never-be-metadata"
            payload_path.write_text(json.dumps(body))
            output = io.StringIO()
            with redirect_stdout(output):
                assert write_action.main(["product-expected-config-dry-run", "--payload-file", str(payload_path)]) == 2
            assert len(calls) == 2, "Credential values must not reach the metadata endpoint"
            assert "must-never-be-metadata" not in output.getvalue()


def test_expected_config_removal_shape_is_bound_to_the_request() -> None:
    with TemporaryDirectory() as directory:
        payload_path = Path(directory) / "metadata.json"
        evidence_path = Path(directory) / "review.json"
        removal = {"key": "ODOO_VERSION", "context": "example-site", "instance": ""}
        binding_removal = {"integration": "runtime_environment", "binding_key": "EXAMPLE_BINDING", "context": "example-site", "instance": ""}
        body = {
            "schema_version": 1, "product": "example-site", "reason": "Sites build their own images.",
            "remove_runtime_environment_keys": [removal], "remove_managed_secret_bindings": [binding_removal],
        }
        payload_path.write_text(json.dumps(body))
        removal_summary = {
            "runtime_environment_key_remove_count": 1, "managed_secret_binding_remove_count": 0,
            "runtime_environment_key_absent_count": 0, "managed_secret_binding_absent_count": 1,
            "managed_secret_binding_still_bound_count": 0,
        }
        result: dict[str, Any] = {
            "status": "ok", "mode": "dry-run", "product": "example-site", "source_label": "operator", "changed": True,
            "runtime_environment_keys": {"added": [], "unchanged": [], "removed": [removal], "absent": []},
            "managed_secret_bindings": {"added": [], "unchanged": [], "removed": [], "absent": [binding_removal], "still_bound": []},
            "summary": removal_summary,
        }
        calls: list[dict[str, Any]] = []

        def post(**kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {
                "status": "accepted", "trace_id": "launchplane_req_expected_config",
                "records": {"product_profile": "example-site"},
                "result": {**result, "mode": kwargs["body"]["mode"]},
            }

        def dry_run() -> tuple[int, str]:
            captured = io.StringIO()
            with redirect_stdout(captured):
                status = write_action.main(["product-expected-config-dry-run", "--payload-file", str(payload_path)])
            return status, captured.getvalue()

        with (
            temporary_attribute(write_action, "prepare_operator_settings", lambda **_kwargs: {"service_url": "https://launchplane.example.invalid", "token": "fixture-only"}),
            temporary_attribute(write_action, "request_launchplane", post),
        ):
            exit_code, output = dry_run()
            assert exit_code == 0
            projected = json.loads(output)["result"]
            assert calls[-1]["body"]["remove_runtime_environment_keys"] == [removal]
            assert projected["runtime_environment_keys_removed_count"] == 1
            assert projected["managed_secret_bindings_absent_count"] == 1
            assert projected["managed_secret_bindings_still_bound_count"] == 0

            # An older service ignores removals and answers with the add-only shape.
            result["runtime_environment_keys"] = {"added": [], "unchanged": []}
            result["managed_secret_bindings"] = {"added": [], "unchanged": []}
            result["summary"] = {key: 0 for key in ("runtime_environment_key_add_count", "managed_secret_binding_add_count", "runtime_environment_key_unchanged_count", "managed_secret_binding_unchanged_count")}
            exit_code, output = dry_run()
            assert exit_code != 0
            evidence_path.write_text(output)
            with redirect_stdout(io.StringIO()):
                assert write_action.main([
                    "product-expected-config-apply", "--payload-file", str(payload_path),
                    "--dry-run-evidence-file", str(evidence_path), "--reviewed-dry-run", "--idempotency-key", "example-remove",
                ]) == 2
            assert all(call["body"]["mode"] == "dry-run" for call in calls), "Unverified removal evidence must not apply"

            # An add-only request still requires exactly added/unchanged.
            del body["remove_runtime_environment_keys"], body["remove_managed_secret_bindings"]
            body["runtime_environment_keys"] = [removal]
            payload_path.write_text(json.dumps(body))
            result["runtime_environment_keys"] = {"added": [removal], "unchanged": [], "removed": [], "absent": []}
            assert dry_run()[0] != 0


def test_expected_config_removal_items_are_plain_identities() -> None:
    valid_runtime = {"key": "ODOO_VERSION", "context": "example-site", "instance": ""}
    valid_binding = {"integration": "runtime_environment", "binding_key": "EXAMPLE_BINDING", "context": "example-site", "instance": "testing"}
    refused = [
        ("remove_runtime_environment_keys", {**valid_runtime, "key": " "}),
        ("remove_runtime_environment_keys", {"context": "example-site"}),
        ("remove_runtime_environment_keys", {**valid_runtime, "context": "", "instance": "testing"}),
        ("remove_runtime_environment_keys", {**valid_runtime, "key": ["ODOO_VERSION"]}),
        ("remove_managed_secret_bindings", {**valid_binding, "binding_key": {"value": "nested"}}),
        ("remove_managed_secret_bindings", {**valid_binding, "integration": ""}),
        ("remove_managed_secret_bindings", {**valid_binding, "context": None}),
        ("remove_managed_secret_bindings", {**valid_binding, "owner_input": {"label": "Mail"}}),
    ]
    with TemporaryDirectory() as directory:
        payload_path = Path(directory) / "metadata.json"
        args = argparse.Namespace(payload_file=str(payload_path))
        base = {"schema_version": 1, "product": "example-site", "reason": "Sites build their own images."}
        payload_path.write_text(json.dumps({**base, "remove_runtime_environment_keys": [valid_runtime], "remove_managed_secret_bindings": [valid_binding]}))
        assert write_action.product_expected_config_payload_body(args, mode="dry-run")["remove_managed_secret_bindings"] == [valid_binding]
        for kind, item in refused:
            payload_path.write_text(json.dumps({**base, kind: [item]}))
            try:
                write_action.product_expected_config_payload_body(args, mode="dry-run")
            except ValueError:
                continue
            raise AssertionError(f"{kind} accepted {item!r}")


def test_owner_review_reader_keeps_full_prose_and_uses_only_the_private_route() -> None:
    decision = {
        "record_id": "decision-one", "product": "example-site", "repository": "example/site",
        "pull_request_number": 42, "head_sha": "a" * 40, "preview_url": "https://preview.example.invalid",
        "decision": "changes_requested", "reason": "Keep USB-C.\n\nCafé, @mentions, and --> remain literal.",
        "owner_github_id": "9001", "owner_github_login": "example-owner",
        "decided_at": "2026-09-26T12:00:00Z", "feedback_url": "https://github.com/example/site/pull/42#issuecomment-1",
    }
    payload = {"status": "ok", "repository": "example/site", "pull_request_number": 42,
               "latest_decision": {**decision, "extra_private_state": "must-not-escape"}}
    settings = {"service_url": "https://private.example.invalid", "token": "private-credential"}
    with patch.object(owner_review, "resolve_settings", return_value=settings), patch.object(
        owner_review, "request_launchplane_read", return_value=payload
    ) as request:
        output = io.StringIO()
        with redirect_stdout(output):
            assert owner_review.main(["--repo", "example/site", "--pr", "42", "--decision-id", "decision-one"]) == 0
        assert json.loads(output.getvalue()) == {"ok": True, "decision": decision}
        request.assert_called_once_with(
            service_url=settings["service_url"], path="/v1/product-review", settings=settings,
            query={"repository": "example/site", "pull_request": "42", "decision_id": "decision-one"}, timeout=10.0,
        )


def test_owner_review_reader_rejects_wrong_subject_or_selected_record() -> None:
    settings = {"service_url": "https://private.example.invalid", "token": "private-credential"}
    for payload in (
        {"status": "ok", "repository": "different/site", "pull_request_number": 42, "latest_decision": None},
        {"status": "ok", "repository": "example/site", "pull_request_number": 42, "latest_decision": None},
        {"status": "ok", "repository": "example/site", "pull_request_number": 42, "latest_decision": {"record_id": "other"}},
    ):
        with patch.object(owner_review, "resolve_settings", return_value=settings), patch.object(
            owner_review, "request_launchplane_read", return_value=payload
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                assert owner_review.main(["--repo", "example/site", "--pr", "42", "--decision-id", "decision-one"]) == 1
            assert json.loads(output.getvalue()) == {"ok": False, "error": "owner_review_read_unavailable"}


def test_owner_review_reader_surfaces_denial_without_credentials_or_provider_text() -> None:
    settings = {"service_url": "https://private.example.invalid", "token": "private-credential"}
    denial = urllib.error.HTTPError(settings["service_url"], 403, "private-provider-message", Message(), io.BytesIO(b"private-body"))
    with patch.object(owner_review, "resolve_settings", return_value=settings), patch.object(
        owner_review, "request_launchplane_read", side_effect=denial
    ) as request:
        output = io.StringIO()
        with redirect_stdout(output):
            assert owner_review.main(["--repo", "example/site", "--pr", "42"]) == 1
        assert json.loads(output.getvalue()) == {"ok": False, "error": "owner_review_read_failed", "status_code": 403}
        assert request.call_count == 1


SETTINGS = {"service_url": "https://launchplane.example.invalid", "token": "t"}


def _run_main(
    argv: list[str],
    *,
    post: object = None,
    read: object = None,
) -> tuple[int, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the helper with its HTTP calls replaced; returns status, output, POSTs and GETs."""
    posts: list[dict[str, Any]] = []
    reads: list[dict[str, Any]] = []

    def fake_post(**kwargs: Any) -> dict[str, Any]:
        posts.append(kwargs)
        if isinstance(post, BaseException):
            raise post
        return cast(Any, post)(kwargs) if callable(post) else cast(dict[str, Any], post)

    def fake_read(**kwargs: Any) -> dict[str, Any]:
        reads.append(kwargs)
        value = cast(Any, read)(kwargs) if callable(read) else read
        if isinstance(value, BaseException):
            raise value
        return value

    output = io.StringIO()
    with (
        temporary_attribute(write_action, "prepare_operator_settings", lambda **_kwargs: SETTINGS),
        temporary_attribute(write_action, "request_launchplane", fake_post),
        temporary_attribute(write_action, "request_launchplane_read", fake_read),
        redirect_stdout(output),
    ):
        status = write_action.main(argv)
    return status, json.loads(output.getvalue()), posts, reads


def _write_json(directory: str, name: str, value: object) -> str:
    path = Path(directory) / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def _reviewed_apply_argv(digest: str, evidence_path: str, key: str = "apply-1") -> list[str]:
    return [
        "--idempotency-key",
        key,
        "--reviewed-dry-run",
        "--expected-plan-digest",
        digest,
        "--dry-run-evidence-file",
        evidence_path,
    ]


def _owner_plan(**overrides: object) -> dict[str, object]:
    plan: dict[str, object] = {
        "status": "ok",
        "mode": "dry-run",
        "product": "example-product",
        "operation": "set",
        "resolved_github_login": "example-client",
        "resolved_github_id": "1234567",
        "owner_before": {"github_login": "", "github_id": ""},
        "owner_after": {"github_login": "example-client", "github_id": "1234567"},
        "changed": True,
        "applied": False,
        "reason": "Record the Client.",
        "source_label": "service:product-owner",
        "profile_updated_at_before": "2026-10-01T12:00:00Z",
        "profile_updated_at_after": "",
    }
    plan.update(overrides)
    return plan


def _owner_response(plan: dict[str, object]) -> dict[str, object]:
    return {
        "status": "accepted",
        "trace_id": "launchplane_req_owner",
        "records": {"product_profile": "example-product"},
        "result": plan,
    }


def _profile_response(login: str = "", github_id: str = "") -> dict[str, object]:
    return {
        "status": "ok",
        "trace_id": "launchplane_req_profile",
        "profile": {
            "product": "example-product",
            "owner": {"github_login": login, "github_id": github_id, "review_label": "owner-review"},
        },
    }


OWNER_DRY_RUN_ARGV = [
    "product-owner-dry-run",
    "--product",
    "example-product",
    "--github-login",
    "@Example-Client",
    "--reason",
    "Record the Client.",
]


def test_product_owner_plan_projection_digests_the_reviewed_change() -> None:
    result = _saved_dry_run_output("product-owner-dry-run", _owner_response(_owner_plan()))
    projected = result["result"]
    assert projected["owner_after"] == {"github_login": "example-client", "github_id": "1234567"}
    assert projected["plan_sha256"] == result["summary"]["plan_sha256"]
    # The digest covers the change itself: a different previous Client is a different plan.
    other = write_action._project_product_owner_plan(
        _owner_plan(owner_before={"github_login": "someone-else", "github_id": "7"})
    )
    assert other["plan_sha256"] != projected["plan_sha256"]
    for candidate, code in (
        (_owner_plan(extra="x"), "unsafe_response_shape"),
        (_owner_plan(operation="transfer"), "invalid_response"),
        (_owner_plan(owner_after={"github_login": "a", "github_id": "1", "email": "x"}), "unsafe_response_shape"),
        (_owner_plan(resolved_github_id="12ab"), "invalid_response"),
        (_owner_plan(resolved_github_login="not a login"), "invalid_response"),
    ):
        _expect_error(
            lambda value=candidate: write_action._project_product_owner_plan(value), code
        )
    _expect_error(
        lambda: write_action._project_success_output(
            "product-owner-dry-run",
            {**_owner_response(_owner_plan()), "records": {"product_profile": "x", "token": "y"}},
        ),
        "unsafe_response_shape",
    )


def test_product_owner_dry_run_sends_normalized_login_to_the_product_route() -> None:
    status, payload, posts, _reads = _run_main(
        OWNER_DRY_RUN_ARGV, post=_owner_response(_owner_plan())
    )
    assert status == 0
    assert posts[0]["path"] == "/v1/product-profiles/example-product/owner"
    assert posts[0]["body"] == {
        "schema_version": 1,
        "mode": "dry-run",
        "github_login": "Example-Client",
        "clear": False,
        "reason": "Record the Client.",
    }
    assert posts[0]["idempotency_key"] == ""
    assert payload["result"]["plan_sha256"]
    for argv, code in (
        ([*OWNER_DRY_RUN_ARGV[:4], "bad login", *OWNER_DRY_RUN_ARGV[5:]], "invalid_github_login"),
        (["product-owner-dry-run", "--product", "../x", "--clear", "--reason", "r"], "invalid_product"),
    ):
        status, payload, posts, _reads = _run_main(argv, post=_owner_response(_owner_plan()))
        assert status == 2 and posts == [], argv
        assert payload["warnings"][0]["code"] == code


def test_product_owner_apply_checks_the_client_reapplies_and_reads_back() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        evidence = _saved_dry_run_output("product-owner-dry-run", _owner_response(_owner_plan()))
        evidence_path = _write_json(directory, "owner-dry-run.json", evidence)
        digest = evidence["result"]["plan_sha256"]
        apply_argv = [
            "product-owner-apply",
            *OWNER_DRY_RUN_ARGV[1:],
            *_reviewed_apply_argv(digest, evidence_path),
        ]
        applied = _owner_response(
            _owner_plan(mode="apply", applied=True, profile_updated_at_after="2026-10-02T12:00:00Z")
        )
        profiles = iter([_profile_response(), _profile_response("example-client", "1234567")])
        status, payload, posts, reads = _run_main(
            apply_argv, post=applied, read=lambda _kwargs: next(profiles)
        )
        assert status == 0, payload
        assert payload["status"] == "accepted"
        assert posts[0]["body"]["mode"] == "apply"
        assert posts[0]["idempotency_key"] == "apply-1"
        assert "reviewed_plan_sha256" not in posts[0]["body"]
        assert [read["path"] for read in reads] == ["/v1/product-profiles/example-product"] * 2
        assert payload["result"]["read_back_matches"] is True

        # Someone else set a Client after the review: stop before the POST.
        status, payload, posts, _reads = _run_main(
            apply_argv, post=applied, read=_profile_response("someone-else", "7")
        )
        assert (status, payload["status"], posts) == (1, "stale", [])
        assert payload["summary"]["error_code"] == "owner_changed_since_review"

        # Launchplane applied something other than the review: never report success.
        moved = _owner_response(
            _owner_plan(mode="apply", applied=True, owner_before={"github_login": "x", "github_id": "9"})
        )
        profiles = iter([_profile_response(), _profile_response("example-client", "1234567")])
        status, payload, _posts, _reads = _run_main(
            apply_argv, post=moved, read=lambda _kwargs: next(profiles)
        )
        assert (status, payload["status"]) == (1, "accepted_unverified")
        assert "applied_plan_differs_from_review" in [item["code"] for item in payload["warnings"]]

        # The read-back fails: the write is not called verified.
        profiles = iter([_profile_response(), OSError("down")])
        status, payload, _posts, _reads = _run_main(
            apply_argv, post=applied, read=lambda _kwargs: next(profiles)
        )
        assert (status, payload["status"]) == (1, "accepted_unverified")
        assert "read_back_unavailable" in [item["code"] for item in payload["warnings"]]

        # The connection drops after the POST began: the outcome is unknown.
        status, payload, _posts, _reads = _run_main(
            apply_argv, post=TimeoutError(), read=_profile_response()
        )
        assert (status, payload["status"]) == (1, "outcome_unknown")

        def http_error(http_status: int) -> urllib.error.HTTPError:
            body = {"trace_id": "launchplane_req_error", "error": {"code": "example"}}
            return urllib.error.HTTPError(
                "https://launchplane.example.invalid", http_status, "error", hdrs=Message(),
                fp=io.BytesIO(json.dumps(body).encode()),
            )

        # A gateway error after the POST may follow a completed write; a 4xx is Launchplane's refusal.
        status, payload, _posts, _reads = _run_main(
            apply_argv, post=http_error(502), read=_profile_response()
        )
        assert (status, payload["status"]) == (1, "outcome_unknown")
        status, payload, _posts, _reads = _run_main(
            apply_argv, post=http_error(409), read=_profile_response()
        )
        assert status == 1 and payload["status"] not in {"outcome_unknown", "accepted"}

        for overrides, code in (
            (["--clear"], "reviewed_dry_run_not_apply_eligible"),
            (["--github-login", "another-user"], "reviewed_dry_run_not_apply_eligible"),
        ):
            argv = [
                "product-owner-apply",
                "--product",
                "example-product",
                *overrides,
                "--reason",
                "Record the Client.",
                *_reviewed_apply_argv(digest, evidence_path),
            ]
            status, payload, posts, reads = _run_main(argv, post=applied, read=_profile_response())
            assert (status, posts, reads) == (2, [], []), overrides
            assert payload["warnings"][0]["code"] == code
        unchanged = _saved_dry_run_output(
            "product-owner-dry-run",
            _owner_response(_owner_plan(operation="unchanged", changed=False)),
        )
        unchanged_path = _write_json(directory, "owner-unchanged.json", unchanged)
        argv = [
            "product-owner-apply",
            *OWNER_DRY_RUN_ARGV[1:],
            *_reviewed_apply_argv(unchanged["result"]["plan_sha256"], unchanged_path),
        ]
        status, _payload, posts, _reads = _run_main(argv, post=applied, read=_profile_response())
        assert (status, posts) == (2, [])


_OLD_IMAGE = "ghcr.io/example-owner/example-product-app"
_NEW_IMAGE = "ghcr.io/example-owner/example-product"


def _image_plan(**overrides: object) -> dict[str, object]:
    plan: dict[str, object] = {
        "status": "ok",
        "mode": "dry-run",
        "product": "example-product",
        "repository": "example-owner/example-product",
        "image_repository_before": _OLD_IMAGE,
        "image_repository_after": _NEW_IMAGE,
        "changed": True,
        "applied": False,
        "lanes": [
            {
                "instance": "testing",
                "context": "example-product",
                "current_artifact_id": f"{_OLD_IMAGE}@sha256:{'a' * 64}",
                "in_new_repository": False,
            }
        ],
        "reason": "Publish to the package named after the repository.",
        "source_label": "service:product-image-repository",
        "profile_updated_at_before": "2026-10-01T12:00:00Z",
        "profile_updated_at_after": "",
    }
    plan.update(overrides)
    return plan


def _image_response(plan: dict[str, object]) -> dict[str, object]:
    return {
        "status": "accepted",
        "trace_id": "launchplane_req_image",
        "records": {"product_profile": "example-product"},
        "result": plan,
    }


def _image_profile_response(repository: str = _OLD_IMAGE) -> dict[str, object]:
    return {
        "status": "ok",
        "trace_id": "launchplane_req_profile",
        "profile": {"product": "example-product", "image": {"repository": repository}},
    }


IMAGE_DRY_RUN_ARGV = [
    "product-image-repository-dry-run",
    "--product",
    "example-product",
    "--image-repository",
    f"{_NEW_IMAGE}/",
    "--reason",
    "Publish to the package named after the repository.",
]


def test_product_image_repository_plan_projection_digests_the_reviewed_move() -> None:
    result = _saved_dry_run_output("product-image-repository-dry-run", _image_response(_image_plan()))
    projected = result["result"]
    assert projected["image_repository_after"] == _NEW_IMAGE
    assert projected["lanes"][0]["current_artifact_id"].startswith(f"{_OLD_IMAGE}@sha256:")
    assert projected["plan_sha256"] == result["summary"]["plan_sha256"]
    # A different starting repository is a different plan; a lane deploy is not.
    other = write_action._project_product_image_repository_plan(
        _image_plan(image_repository_before="ghcr.io/example-owner/other")
    )
    assert other["plan_sha256"] != projected["plan_sha256"]
    redeployed = write_action._project_product_image_repository_plan(_image_plan(lanes=[]))
    assert redeployed["plan_sha256"] == projected["plan_sha256"]
    lane = cast(list[dict[str, object]], _image_plan()["lanes"])[0]
    for candidate, code in (
        (_image_plan(extra="x"), "unsafe_response_shape"),
        (_image_plan(lanes=[{**lane, "domain": "example.invalid"}]), "unsafe_response_shape"),
        (_image_plan(lanes=[{**lane, "current_artifact_id": "not an id"}]), "invalid_response"),
        (_image_plan(image_repository_after="https://ghcr.io/x/y"), "invalid_response"),
        (_image_plan(image_repository_after=""), "invalid_response"),
        (_image_plan(image_repository_before="https://registry.example.invalid/x"), "invalid_response"),
        (
            _image_plan(lanes=[{**lane, "current_artifact_id": "https://registry.example.invalid/x"}]),
            "invalid_response",
        ),
    ):
        _expect_error(
            lambda value=candidate: write_action._project_product_image_repository_plan(value),
            code,
        )


def test_product_image_repository_dry_run_sends_the_package_to_the_product_route() -> None:
    status, payload, posts, _reads = _run_main(
        IMAGE_DRY_RUN_ARGV, post=_image_response(_image_plan())
    )
    assert status == 0, payload
    assert posts[0]["path"] == "/v1/product-profiles/example-product/image-repository"
    assert posts[0]["body"] == {
        "schema_version": 1,
        "mode": "dry-run",
        "image_repository": _NEW_IMAGE,
        "reason": "Publish to the package named after the repository.",
    }
    assert posts[0]["idempotency_key"] == ""
    assert payload["summary"]["changed"] is True
    for argv, code in (
        (
            [*IMAGE_DRY_RUN_ARGV[:4], "ghcr.io/Example/Upper", *IMAGE_DRY_RUN_ARGV[5:]],
            "invalid_image_repository",
        ),
        (
            [*IMAGE_DRY_RUN_ARGV[:4], f"{_NEW_IMAGE}:latest", *IMAGE_DRY_RUN_ARGV[5:]],
            "invalid_image_repository",
        ),
        ([*IMAGE_DRY_RUN_ARGV[:2], "../x", *IMAGE_DRY_RUN_ARGV[3:]], "invalid_product"),
    ):
        status, payload, posts, _reads = _run_main(argv, post=_image_response(_image_plan()))
        assert status == 2 and posts == [], argv
        assert payload["warnings"][0]["code"] == code


def test_product_image_repository_apply_names_the_reviewed_start_and_reads_back() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        evidence = _saved_dry_run_output(
            "product-image-repository-dry-run", _image_response(_image_plan())
        )
        evidence_path = _write_json(directory, "image-dry-run.json", evidence)
        digest = evidence["result"]["plan_sha256"]
        apply_argv = [
            "product-image-repository-apply",
            *IMAGE_DRY_RUN_ARGV[1:],
            *_reviewed_apply_argv(digest, evidence_path),
        ]
        applied = _image_response(
            _image_plan(mode="apply", applied=True, profile_updated_at_after="2026-10-02T12:00:00Z")
        )
        profiles = iter([_image_profile_response(), _image_profile_response(_NEW_IMAGE)])
        status, payload, posts, reads = _run_main(
            apply_argv, post=applied, read=lambda _kwargs: next(profiles)
        )
        assert status == 0, payload
        assert payload["status"] == "accepted"
        assert posts[0]["body"]["mode"] == "apply"
        assert posts[0]["body"]["expected_image_repository"] == _OLD_IMAGE
        assert posts[0]["idempotency_key"] == "apply-1"
        assert [read["path"] for read in reads] == ["/v1/product-profiles/example-product"] * 2
        assert payload["result"]["read_back_matches"] is True

        # The profile moved after the review: stop before the POST.
        status, payload, posts, _reads = _run_main(
            apply_argv, post=applied, read=_image_profile_response("ghcr.io/example-owner/other")
        )
        assert (status, payload["status"], posts) == (1, "stale", [])
        assert payload["summary"]["error_code"] == "image_repository_changed_since_review"

        # The read-back still shows the old repository: never report success.
        status, payload, _posts, _reads = _run_main(
            apply_argv, post=applied, read=_image_profile_response()
        )
        assert (status, payload["status"]) == (1, "accepted_unverified")

        # Launchplane applied a move from somewhere else: never report success.
        moved = _image_response(
            _image_plan(mode="apply", applied=True, image_repository_before="ghcr.io/x/y")
        )
        profiles = iter([_image_profile_response(), _image_profile_response(_NEW_IMAGE)])
        status, payload, _posts, _reads = _run_main(
            apply_argv, post=moved, read=lambda _kwargs: next(profiles)
        )
        assert (status, payload["status"]) == (1, "accepted_unverified")
        assert "applied_plan_differs_from_review" in [item["code"] for item in payload["warnings"]]

        # The connection drops after the POST began: the outcome is unknown.
        status, payload, _posts, _reads = _run_main(
            apply_argv, post=TimeoutError(), read=_image_profile_response()
        )
        assert (status, payload["status"]) == (1, "outcome_unknown")

        # A different package, or an edited evidence file, is not the reviewed plan.
        tampered = copy.deepcopy(evidence)
        tampered["result"]["image_repository_before"] = "ghcr.io/example-owner/other"
        tampered_path = _write_json(directory, "image-tampered.json", tampered)
        for argv in (
            [
                "product-image-repository-apply",
                *IMAGE_DRY_RUN_ARGV[1:4],
                "ghcr.io/example-owner/another",
                *IMAGE_DRY_RUN_ARGV[5:],
                *_reviewed_apply_argv(digest, evidence_path),
            ],
            [
                "product-image-repository-apply",
                *IMAGE_DRY_RUN_ARGV[1:],
                *_reviewed_apply_argv(digest, tampered_path),
            ],
        ):
            status, payload, posts, reads = _run_main(
                argv, post=applied, read=_image_profile_response()
            )
            assert (status, posts, reads) == (2, [], []), argv
            assert payload["warnings"][0]["code"] == "reviewed_dry_run_not_apply_eligible"

        unchanged = _saved_dry_run_output(
            "product-image-repository-dry-run",
            _image_response(_image_plan(image_repository_before=_NEW_IMAGE, changed=False)),
        )
        unchanged_path = _write_json(directory, "image-unchanged.json", unchanged)
        argv = [
            "product-image-repository-apply",
            *IMAGE_DRY_RUN_ARGV[1:],
            *_reviewed_apply_argv(unchanged["result"]["plan_sha256"], unchanged_path),
        ]
        status, _payload, posts, _reads = _run_main(
            argv, post=applied, read=_image_profile_response()
        )
        assert (status, posts) == (2, [])


def _backup_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "targets": [
            {
                "target_id": "example-prod-guest",
                "target_revision": 1,
                "destination": {
                    "destination_kind": "proxmox_guest",
                    "host": "proxmox.example.invalid",
                    "username": "backup-operator",
                    "guest_kind": "lxc",
                    "guest_id": "101",
                },
                "effective_at": "2026-10-01T00:00:00Z",
                "review_after": "2027-10-01T00:00:00Z",
                "source": "operator",
                "reason": "Capture the production guest.",
            }
        ],
        "policy": {
            "product": "example-product",
            "context": "example",
            "instance": "prod",
            "promotion_action": "generic_web_prod_promotion.execute",
            "policy_revision": 1,
        },
        "expected_current_policy_record_id": "",
        "expected_current_target_record_ids": {},
    }


def _backup_policy_summary() -> dict[str, object]:
    return {
        "policy_id": "production-backup-policy-abc",
        "record_id": "production-backup-policy-abc-r1",
        "policy_revision": 1,
        "status": "active",
        "promotion_action": "generic_web_prod_promotion.execute",
        "source_target_id": "example-prod-guest",
        "destination_target_id": "example-independent-backup",
        "effective_at": "2026-10-01T00:00:00Z",
        "review_after": "2027-10-01T00:00:00Z",
    }


def _backup_target_summary(**overrides: object) -> dict[str, object]:
    target: dict[str, object] = {
        "target_id": "example-prod-guest",
        "record_id": "production-backup-target-example-prod-guest-r1",
        "target_revision": 1,
        "status": "active",
        "provider_type": "proxmox",
        "destination_kind": "proxmox_guest",
        "effective_at": "2026-10-01T00:00:00Z",
        "review_after": "2027-10-01T00:00:00Z",
    }
    target.update(overrides)
    return target


def _backup_response(mode: str = "dry_run", status: str = "would_apply", **overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": 1,
        "mode": mode,
        "status": status,
        "authority_digest": "c" * 64,
        "policy": _backup_policy_summary(),
        "targets": [_backup_target_summary()],
    }
    result.update(overrides)
    return {"status": "ok", "trace_id": "launchplane_req_backup", "result": result}


def _backup_read_response(**overrides: object) -> dict[str, object]:
    authority: dict[str, object] = {
        "schema_version": 1,
        "product": "example-product",
        "context": "example",
        "instance": "prod",
        "promotion_action": "generic_web_prod_promotion.execute",
        "state": "ready",
        "ready": True,
        "summary": "Backup authority is ready.",
        "reason_codes": [],
        "policy": _backup_policy_summary(),
        "targets": [_backup_target_summary()],
        "generated_at": "2026-10-02T12:00:00Z",
    }
    authority.update(overrides)
    return {"status": "ok", "trace_id": "launchplane_req_backup_read", "authority": authority}


def test_production_backup_authority_never_prints_provider_coordinates() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = _write_json(directory, "backup.json", _backup_payload())
        status, payload, posts, _reads = _run_main(
            ["production-backup-authority-dry-run", "--payload-file", payload_path],
            post=_backup_response(),
        )
        assert status == 0, payload
        assert posts[0]["path"] == "/v1/production-backup-authority/apply"
        assert posts[0]["body"]["mode"] == "dry_run"
        assert posts[0]["body"]["targets"][0]["destination"]["guest_id"] == "101"
        printed = json.dumps(payload)
        for private in ("proxmox.example.invalid", "backup-operator", '"101"'):
            assert private not in printed
        assert payload["summary"]["authority_digest"] == "c" * 64
        assert payload["request"]["promotion_action"] == "generic_web_prod_promotion.execute"

        # A response that echoes a destination is refused, not printed.
        leaked = _backup_response(targets=[{**_backup_target_summary(), "destination": {"host": "x"}}])
        status, payload, _posts, _reads = _run_main(
            ["production-backup-authority-dry-run", "--payload-file", payload_path], post=leaked
        )
        assert status == 1 and "proxmox" not in json.dumps(payload.get("result"))

        for mutate, code in (
            (lambda data: data.update(mode="apply"), "unsupported_backup_authority_field"),
            (lambda data: data.update(reviewed_authority_digest="c" * 64), "unsupported_backup_authority_field"),
            (lambda data: data.pop("policy"), "backup_policy_required"),
            (lambda data: data["policy"].pop("promotion_action"), "backup_policy_promotion_action_required"),
        ):
            changed = _backup_payload()
            mutate(changed)
            bad_path = _write_json(directory, "backup-bad.json", changed)
            status, payload, posts, _reads = _run_main(
                ["production-backup-authority-dry-run", "--payload-file", bad_path],
                post=_backup_response(),
            )
            assert (status, posts) == (2, []), code
            assert payload["warnings"][0]["code"] == code


def test_production_backup_authority_apply_binds_the_exact_reviewed_payload() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = _write_json(directory, "backup.json", _backup_payload())
        _status, evidence, _posts, _reads = _run_main(
            ["production-backup-authority-dry-run", "--payload-file", payload_path],
            post=_backup_response(),
        )
        evidence_path = _write_json(directory, "backup-dry-run.json", evidence)
        apply_argv = [
            "production-backup-authority-apply",
            "--payload-file",
            payload_path,
            *_reviewed_apply_argv("c" * 64, evidence_path),
        ]
        status, payload, posts, reads = _run_main(
            apply_argv,
            post=_backup_response(mode="apply", status="applied"),
            read=_backup_read_response(),
        )
        assert status == 0, payload
        assert posts[0]["body"]["mode"] == "apply"
        assert posts[0]["body"]["reviewed_authority_digest"] == "c" * 64
        assert posts[0]["idempotency_key"] == "apply-1"
        assert reads[0]["path"] == "/v1/production-backup-authority"
        assert reads[0]["query"] == {
            "product": "example-product",
            "context": "example",
            "instance": "prod",
            "promotion_action": "generic_web_prod_promotion.execute",
        }
        assert payload["result"]["read_back_matches"] is True

        # A target the apply reported is missing from the read-back.
        status, payload, _posts, _reads = _run_main(
            apply_argv,
            post=_backup_response(mode="apply", status="applied"),
            read=_backup_read_response(targets=[], state="missing", ready=False),
        )
        assert (status, payload["status"]) == (1, "accepted_unverified")
        assert payload["result"]["read_back_matches"] is False

        # The response leaves out a reviewed target while reporting the reviewed digest.
        status, payload, _posts, _reads = _run_main(
            apply_argv,
            post=_backup_response(mode="apply", status="applied", targets=[]),
            read=_backup_read_response(),
        )
        assert (status, payload["status"]) == (1, "accepted_unverified")
        assert "applied_plan_differs_from_review" in [item["code"] for item in payload["warnings"]]

        # Launchplane reports an authority other than the reviewed one.
        status, payload, _posts, _reads = _run_main(
            apply_argv,
            post=_backup_response(mode="apply", status="applied", authority_digest="e" * 64),
            read=_backup_read_response(),
        )
        assert (status, payload["status"]) == (1, "accepted_unverified")
        assert "applied_plan_differs_from_review" in [item["code"] for item in payload["warnings"]]

        stale_read = _backup_read_response(policy={**_backup_policy_summary(), "record_id": "other-r2"})
        status, payload, _posts, _reads = _run_main(
            apply_argv, post=_backup_response(mode="apply", status="applied"), read=stale_read
        )
        assert (status, payload["status"]) == (1, "accepted_unverified")
        assert payload["result"]["read_back_matches"] is False

        # The private payload changed after review: refuse before any request.
        changed = _backup_payload()
        cast(dict[str, Any], changed["policy"])["policy_revision"] = 2
        _write_json(directory, "backup.json", changed)
        status, payload, posts, reads = _run_main(
            apply_argv, post=_backup_response(mode="apply", status="applied"), read=_backup_read_response()
        )
        assert (status, posts, reads) == (2, [], [])
        assert payload["warnings"][0]["code"] == "reviewed_dry_run_not_apply_eligible"


def test_production_backup_authority_read_keeps_state_and_record_ids() -> None:
    argv = [
        "production-backup-authority-read",
        "--product",
        "example-product",
        "--context",
        "example",
        "--instance",
        "prod",
        "--promotion-action",
        "generic_web_prod_promotion.execute",
    ]
    status, payload, _posts, reads = _run_main(
        argv,
        read=_backup_read_response(
            state="stale", ready=False, reason_codes=["production_backup_target_stale:example-prod-guest"]
        ),
    )
    assert status == 0, payload
    assert reads[0]["query"]["promotion_action"] == "generic_web_prod_promotion.execute"
    assert payload["result"]["state"] == "stale"
    assert payload["result"]["policy"]["record_id"] == "production-backup-policy-abc-r1"
    status, payload, _posts, _reads = _run_main(argv, read=_backup_read_response(state="excellent"))
    assert status == 1 and payload["status"] == "invalid"


def _compose_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "context": "example",
        "instance": "testing",
        "target_name": "example-testing",
        "server_id": "server-private-1",
        "project_id": "project-private-1",
        "healthcheck_path": "/health",
        "domains": ["testing.example.invalid"],
        "reason": "Create the testing lane's target.",
    }


def _compose_response(mode: str = "dry-run", target_id: str = "planned-compose-id") -> dict[str, Any]:
    return {
        "status": "accepted",
        "trace_id": "launchplane_req_compose",
        "records": {},
        "result": {
            "mode": mode,
            "operation": "create-compose",
            "context": "example",
            "instance": "testing",
            "applied": mode == "apply",
            "route_domain_ids": [],
            "reason": "Create the testing lane's target.",
            "setup": {
                "applied": mode == "apply",
                "plan": {
                    "project": {"action": "reuse", "project_id": "project-private-1", "project_name": ""},
                    "environment": {"action": "create", "environment_id": "", "environment_name": "testing"},
                    "compose": {"action": "create", "target_name": "example-testing", "server_id": "server-private-1"},
                },
                "target_record": {
                    "context": "example",
                    "instance": "testing",
                    "target_name": "example-testing",
                    "domains": ["testing.example.invalid"],
                    "healthcheck_path": "/health",
                    "custom_git_url": "git@example.invalid:private/repo.git",
                    "env_keys": ["EXAMPLE_KEY"],
                },
                "target_id_record": {"context": "example", "instance": "testing", "target_id": target_id},
                "provider_target_record": {
                    "provider_id": "dokploy",
                    "target_category": "compose",
                    "provider_target_type": "compose",
                    "target_id": target_id,
                },
                "provider_requests": [{"path": "/api/compose.create", "payload": {"serverId": "server-private-1"}}],
                "warnings": ["dry run only; provider was not mutated and records were not written"],
            },
        },
    }


def _inspect_response(target_id: str = "compose-private-9", status: str = "present") -> dict[str, Any]:
    return {
        "status": "ok",
        "trace_id": "launchplane_req_inspect",
        "inspect": {
            "status": "ok",
            "target_type": "compose",
            "target_id": target_id,
            "tracked_target": {
                "target_id": target_id,
                "domains": ["Testing.example.invalid"],
                "healthcheck_path": "/health",
            },
            "provider_target_record": {"status": status, "target_id": target_id},
        },
    }


def test_dokploy_compose_target_hides_provider_ids_and_binds_the_payload() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = _write_json(directory, "compose.json", _compose_payload())
        status, evidence, posts, _reads = _run_main(
            ["dokploy-target-create-compose-dry-run", "--payload-file", payload_path],
            post=_compose_response(),
        )
        assert status == 0, evidence
        body = posts[0]["body"]
        assert (body["operation"], body["product"], body["mode"]) == ("create-compose", "launchplane", "dry-run")
        assert "confirmation" not in body
        printed = json.dumps(evidence)
        for private in (
            "server-private-1",
            "project-private-1",
            "testing.example.invalid",
            "git@",
            "EXAMPLE_KEY",
            "planned-compose-id",
            "Create the testing lane",
        ):
            assert private not in printed, private
        assert evidence["result"]["plan_actions"] == {"project": "reuse", "environment": "create", "compose": "create"}
        assert evidence["result"]["domain_count"] == 1
        digest = evidence["result"]["plan_sha256"]
        evidence_path = _write_json(directory, "compose-dry-run.json", evidence)
        apply_argv = [
            "dokploy-target-create-compose-apply",
            "--payload-file",
            payload_path,
            *_reviewed_apply_argv(digest, evidence_path),
        ]
        status, payload, posts, reads = _run_main(
            apply_argv,
            post=_compose_response("apply", "compose-private-9"),
            read=_inspect_response(),
        )
        assert status == 0, payload
        assert posts[0]["body"]["confirmation"] == write_action.DOKPLOY_TARGET_SETUP_CONFIRMATION
        assert reads[0]["path"] == contract.internal_helper_path("dokploy-target-inspect")
        assert reads[0]["query"] == {"context": "example", "instance": "testing"}
        assert payload["result"]["read_back_matches"] is True
        assert "compose-private-9" not in json.dumps(payload)

        # Launchplane applied a different plan than the reviewed one.
        changed_plan = _compose_response("apply", "compose-private-9")
        changed_plan["result"]["setup"]["plan"]["project"]["action"] = "create"
        status, payload, _posts, _reads = _run_main(
            apply_argv, post=changed_plan, read=_inspect_response()
        )
        assert (status, payload["status"]) == (1, "accepted_unverified")
        assert "applied_plan_differs_from_review" in [item["code"] for item in payload["warnings"]]

        # The tracked target does not hold the reviewed domain.
        moved_domain = _inspect_response()
        moved_domain["inspect"]["tracked_target"]["domains"] = ["other.example.invalid"]
        status, payload, _posts, _reads = _run_main(
            apply_argv, post=_compose_response("apply", "compose-private-9"), read=moved_domain
        )
        assert (status, payload["status"]) == (1, "accepted_unverified")
        assert payload["result"]["read_back"]["configuration_matches_review"] is False
        assert "other.example.invalid" not in json.dumps(payload)

        # The records name a different target than the one created.
        status, payload, _posts, _reads = _run_main(
            apply_argv,
            post=_compose_response("apply", "compose-private-9"),
            read=_inspect_response(target_id="compose-other"),
        )
        assert (status, payload["status"]) == (1, "accepted_unverified")

        changed = {**_compose_payload(), "domains": ["other.example.invalid"]}
        _write_json(directory, "compose.json", changed)
        status, payload, posts, _reads = _run_main(
            apply_argv, post=_compose_response("apply"), read=_inspect_response()
        )
        assert (status, posts) == (2, [])
        assert payload["warnings"][0]["code"] == "reviewed_dry_run_not_apply_eligible"

        for mutate, code in (
            (lambda data: data.update(confirmation="APPLY DOKPLOY TARGET SETUP"), "unsupported_dokploy_target_field"),
            (lambda data: data.update(domains=[]), "domains_required"),
            (lambda data: data.update(healthcheck_path="health"), "healthcheck_path_required"),
            (lambda data: data.pop("project_id"), "dokploy_project_required"),
            (lambda data: data.pop("server_id"), "server_id_required"),
        ):
            bad = _compose_payload()
            mutate(bad)
            bad_path = _write_json(directory, "compose-bad.json", bad)
            status, payload, posts, _reads = _run_main(
                ["dokploy-target-create-compose-dry-run", "--payload-file", bad_path],
                post=_compose_response(),
            )
            assert (status, posts) == (2, []), code
            assert payload["warnings"][0]["code"] == code


COMPOSE_SOURCE = {
    "custom_git_url": "https://github.com/example-owner/example-product.git",
    "custom_git_branch": "main",
    "compose_path": "./docker/compose.yml",
}


def _source_completion_response(mode: str = "dry-run", target_id: str = "compose-private-9") -> dict[str, Any]:
    response = _compose_response(mode, target_id)
    response["result"]["operation"] = "complete-compose-source"
    response["result"]["setup"].pop("plan")
    response["result"]["setup"]["source"] = {**COMPOSE_SOURCE, "repository": "example-owner/example-product"}
    return response


def _source_inspect_response() -> dict[str, Any]:
    response = _inspect_response()
    response["inspect"]["provider"] = {}
    for target in (response["inspect"]["provider"], response["inspect"]["tracked_target"]):
        target.update(COMPOSE_SOURCE, source_type="git")
    return response


def test_compose_source_create_reads_back_repository_branch_and_path() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        source_inputs = {key: value for key, value in COMPOSE_SOURCE.items() if key != "custom_git_url"}
        payload_path = _write_json(directory, "compose.json", {**_compose_payload(), **source_inputs})
        def response(mode: str) -> dict[str, Any]:
            value = _compose_response(mode, "compose-private-9")
            value["result"]["setup"]["plan"]["compose"].update(COMPOSE_SOURCE)
            return value
        status, evidence, posts, _ = _run_main(
            ["dokploy-target-create-compose-dry-run", "--payload-file", payload_path], post=response("dry-run")
        )
        assert status == 0, evidence
        assert posts[0]["body"]["custom_git_branch"] == source_inputs["custom_git_branch"]
        evidence_path = _write_json(directory, "review.json", evidence)
        argv = ["dokploy-target-create-compose-apply", "--payload-file", payload_path,
                *_reviewed_apply_argv(evidence["result"]["plan_sha256"], evidence_path)]
        status, result, _, _ = _run_main(argv, post=response("apply"), read=_source_inspect_response())
        assert status == 0 and result["result"]["read_back_matches"] is True, result
        changed = _source_inspect_response()
        changed["inspect"]["provider"]["custom_git_url"] = "https://github.com/other/repo.git"
        status, result, _, _ = _run_main(argv, post=response("apply"), read=changed)
        assert status == 1 and result["result"]["read_back_matches"] is False
        assert COMPOSE_SOURCE["custom_git_url"] not in json.dumps(result)


def test_compose_source_completion_binds_source_and_existing_target() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        private = {"context": "example", "instance": "testing", "reason": "Complete testing source.",
                   "custom_git_branch": COMPOSE_SOURCE["custom_git_branch"], "compose_path": COMPOSE_SOURCE["compose_path"]}
        payload_path = _write_json(directory, "source.json", private)
        dry_argv = ["dokploy-target-complete-compose-source-dry-run", "--payload-file", payload_path]
        status, evidence, posts, _ = _run_main(dry_argv, post=_source_completion_response())
        assert status == 0, evidence
        assert posts[0]["body"] == {**private, "operation": "complete-compose-source", "product": "launchplane", "mode": "dry-run"}
        assert posts[0]["path"] == contract.helper_command_path(dry_argv[0])
        printed = json.dumps(evidence)
        for hidden in ("compose-private-9", COMPOSE_SOURCE["custom_git_url"], "git@", "EXAMPLE_KEY", "testing.example.invalid"):
            assert hidden not in printed, hidden
        evidence_path = _write_json(directory, "review.json", evidence)
        argv = ["dokploy-target-complete-compose-source-apply", "--payload-file", payload_path,
                *_reviewed_apply_argv(evidence["result"]["plan_sha256"], evidence_path)]
        reads = iter([_inspect_response(), _source_inspect_response()])
        status, result, posts, _ = _run_main(argv, post=_source_completion_response("apply"), read=lambda _: next(reads))
        assert status == 0 and result["result"]["read_back_matches"] is True, result
        assert len(posts) == 1 and posts[0]["idempotency_key"] == "apply-1"
        assert posts[0]["body"]["confirmation"] == write_action.DOKPLOY_TARGET_SETUP_CONFIRMATION
        # Stale binding is rejected before the write.
        status, result, posts, _ = _run_main(argv, post=_source_completion_response("apply"), read=_inspect_response("other-compose"))
        assert status == 1 and result["status"] == "stale" and posts == []
        # A mismatched tracked or live source is never verified.
        for location in ("tracked", "live"):
            changed = _source_inspect_response()
            target = changed["inspect"]["tracked_target"] if location == "tracked" else changed["inspect"]["provider"]
            target["custom_git_branch"] = "other-branch"
            reads = iter([_inspect_response(), changed])
            status, result, posts, _ = _run_main(argv, post=_source_completion_response("apply"), read=lambda _: next(reads))
            assert status == 1 and result["status"] == "accepted_unverified"
            assert result["result"]["read_back_matches"] is False and len(posts) == 1
        # The service applied a different target than it planned.
        reads = iter([_inspect_response(), _source_inspect_response()])
        status, result, posts, _ = _run_main(argv, post=_source_completion_response("apply", "other-compose"), read=lambda _: next(reads))
        assert status == 1 and result["status"] == "accepted_unverified"
        # Exact payload evidence and reviewed acknowledgement are required.
        for field in ("custom_git_branch", "compose_path", "reason"):
            _write_json(directory, "source.json", {**private, field: "changed"})
            status, _, posts, _ = _run_main(argv, post=_source_completion_response("apply"))
            assert status == 2 and posts == []
        _write_json(directory, "source.json", private)
        status, _, posts, _ = _run_main([value for value in argv if value != "--reviewed-dry-run"], post=_source_completion_response("apply"))
        assert status == 2 and posts == []
        # Corrupt saved source metadata cannot cause a post-write traceback.
        corrupted = {**evidence, "result": {**evidence["result"], "source": {}}}
        _write_json(directory, "review.json", corrupted)
        reads = iter([_inspect_response(), _source_inspect_response()])
        status, result, posts, _ = _run_main(argv, post=_source_completion_response("apply"), read=lambda _: next(reads))
        assert status == 0 and result["result"]["read_back_matches"] is True
        _write_json(directory, "review.json", evidence)
        # Partial outcomes retain safe trace/code; no automatic retry or read follows the error.
        error = urllib.error.HTTPError("https://private.invalid", 502, "private", Message(), io.BytesIO(json.dumps({
            "trace_id": "launchplane_req_partial", "error": {"code": "dokploy_source_partial_outcome", "message": "private-provider-data"},
        }).encode()))
        status, result, posts, reads = _run_main(argv, post=error, read=_inspect_response())
        assert status == 1 and result["status"] == "outcome_unknown"
        assert result["summary"]["error_code"] == "dokploy_source_partial_outcome"
        assert result["summary"]["trace_id"] == "launchplane_req_partial"
        assert len(posts) == 1 and len(reads) == 1
        assert "private-provider-data" not in json.dumps(result)
        for field, value in (("instance", "prod"), ("target_id", "private-id"), ("server_id", "private-id"),
                             ("custom_git_url", "https://github.com/other/repo.git"), ("domains", []),
                             ("credential", "secret"), ("custom_git_branch", "main;echo x"),
                             ("compose_path", "../compose.yml")):
            path = _write_json(directory, "bad.json", {**private, field: value})
            status, _, posts, _ = _run_main([dry_argv[0], "--payload-file", path], post=_source_completion_response())
            assert status == 2 and posts == [], field


PRIVATE_ENDPOINT_URL = "http://10.0.0.106:8001/healthz"


def _private_endpoint_payload() -> dict[str, object]:
    return {
        "endpoint_key": "example-testing-runtime",
        "product": "example",
        "context": "example",
        "instance": "testing",
        "url": PRIVATE_ENDPOINT_URL,
        "source_label": "lxc-106 private host",
    }


def _private_endpoint_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 1,
        **_private_endpoint_payload(),
        "status": "active",
        "updated_at": "2026-10-03T12:00:00Z",
    }
    record.update(overrides)
    return record


def _private_endpoint_response(mode: str = "dry-run", **record: object) -> dict[str, object]:
    return {
        "status": "accepted",
        "trace_id": "launchplane_req_private_endpoint",
        "records": {},
        "result": {
            "mode": mode,
            "endpoint_key": "example-testing-runtime",
            "endpoint_status": "applied" if mode == "apply" else "planned",
            "record": _private_endpoint_record(**record),
        },
    }


def _private_endpoint_read(**record: object) -> dict[str, object]:
    return {
        "status": "ok",
        "trace_id": "launchplane_req_private_endpoint_read",
        "record": _private_endpoint_record(**record),
    }


def test_private_health_endpoint_hides_the_url_and_binds_apply_to_the_review() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = _write_json(directory, "endpoint.json", _private_endpoint_payload())
        dry_run_argv = [
            "private-health-endpoint-dry-run",
            "--payload-file",
            payload_path,
            "--reason",
            "Monitor the testing lane privately.",
        ]
        status, evidence, posts, _reads = _run_main(dry_run_argv, post=_private_endpoint_response())
        assert status == 0, evidence
        assert posts[0]["path"] == "/v1/private-health-endpoints/apply"
        body = posts[0]["body"]
        assert body["mode"] == "dry-run"
        assert body["endpoint"]["url"] == PRIVATE_ENDPOINT_URL
        assert body["endpoint"]["status"] == "active"
        assert "confirmation" not in body
        printed = json.dumps(evidence)
        for private in ("10.0.0.106", "8001", "healthz", "lxc-106", "Monitor the testing lane"):
            assert private not in printed, private
        assert evidence["result"]["record"]["endpoint_key"] == "example-testing-runtime"
        digest = evidence["result"]["plan_sha256"]
        reviewed_at = evidence["result"]["record"]["updated_at"]
        evidence_path = _write_json(directory, "endpoint-dry-run.json", evidence)
        apply_argv = [
            "private-health-endpoint-apply",
            "--payload-file",
            payload_path,
            "--reason",
            "Monitor the testing lane privately.",
            *_reviewed_apply_argv(digest, evidence_path),
        ]
        status, payload, posts, reads = _run_main(
            apply_argv, post=_private_endpoint_response("apply"), read=_private_endpoint_read()
        )
        assert status == 0, payload
        body = posts[0]["body"]
        assert body["confirmation"] == write_action.PRIVATE_HEALTH_ENDPOINT_CONFIRMATION
        assert posts[0]["idempotency_key"] == "apply-1"
        # A retry with the same evidence sends the same body, so Launchplane can replay it.
        assert body["endpoint"]["updated_at"] == reviewed_at
        assert reads[0]["path"] == contract.internal_helper_path(
            "private-health-endpoint-record-read"
        ).format(endpoint_key="example-testing-runtime")
        assert reads[0]["query"] == {"product": "example", "context": "example", "instance": "testing"}
        assert payload["result"]["read_back_matches"] is True
        assert "10.0.0.106" not in json.dumps(payload)

        # Launchplane recorded a different URL than the reviewed payload.
        status, payload, _posts, _reads = _run_main(
            apply_argv,
            post=_private_endpoint_response("apply", url="http://10.0.0.107:8001/healthz"),
            read=_private_endpoint_read(),
        )
        assert (status, payload["status"]) == (1, "accepted_unverified")
        assert "applied_plan_differs_from_review" in [item["code"] for item in payload["warnings"]]
        assert "10.0.0.107" not in json.dumps(payload)

        # The record read back points somewhere else.
        status, payload, _posts, _reads = _run_main(
            apply_argv,
            post=_private_endpoint_response("apply"),
            read=_private_endpoint_read(url="http://10.0.0.107:8001/healthz"),
        )
        assert (status, payload["status"]) == (1, "accepted_unverified")
        assert payload["result"]["read_back"]["url_matches_review"] is False
        assert "10.0.0.107" not in json.dumps(payload)

        # The record read back is another lane's, even though the URL agrees.
        status, payload, _posts, _reads = _run_main(
            apply_argv,
            post=_private_endpoint_response("apply"),
            read=_private_endpoint_read(instance="prod"),
        )
        assert (status, payload["status"]) == (1, "accepted_unverified")
        assert payload["result"]["read_back_matches"] is False

        # Evidence edited to vouch for a different payload still fails the reviewed digest.
        swapped = {**_private_endpoint_payload(), "url": "http://10.0.0.107:8001/healthz"}
        swapped_path = _write_json(directory, "endpoint-swapped.json", swapped)
        forged = copy.deepcopy(evidence)
        forged["request"]["payload_digest"] = write_action.metadata_review_digest(
            {**swapped, "status": "active", "reason": "Monitor the testing lane privately."}
        )
        forged_path = _write_json(directory, "endpoint-forged.json", forged)
        status, payload, posts, _reads = _run_main(
            [
                "private-health-endpoint-apply",
                "--payload-file",
                swapped_path,
                "--reason",
                "Monitor the testing lane privately.",
                *_reviewed_apply_argv(digest, forged_path),
            ],
            post=_private_endpoint_response("apply"),
            read=_private_endpoint_read(),
        )
        assert (status, posts) == (2, [])
        assert payload["warnings"][0]["code"] == "reviewed_dry_run_not_apply_eligible"

        # A different reason than the reviewed one is a different change.
        status, payload, posts, _reads = _run_main(
            [*apply_argv[:4], "Another reason.", *apply_argv[5:]],
            post=_private_endpoint_response("apply"),
            read=_private_endpoint_read(),
        )
        assert (status, posts) == (2, [])
        assert payload["warnings"][0]["code"] == "reviewed_dry_run_not_apply_eligible"

        changed = {**_private_endpoint_payload(), "url": "http://10.0.0.107:8001/healthz"}
        _write_json(directory, "endpoint.json", changed)
        status, payload, posts, _reads = _run_main(
            apply_argv, post=_private_endpoint_response("apply"), read=_private_endpoint_read()
        )
        assert (status, posts) == (2, [])
        assert payload["warnings"][0]["code"] == "reviewed_dry_run_not_apply_eligible"

        for mutate, code in (
            (lambda data: data.update(updated_at="2026-10-03T12:00:00Z"), "unsupported_private_health_endpoint_field"),
            (lambda data: data.update(url="ftp://10.0.0.106/health"), "private_url_required"),
            (lambda data: data.pop("url"), "private_url_required"),
            (lambda data: data.update(endpoint_key="../other"), "endpoint_key_required"),
            (lambda data: data.pop("instance"), "instance_required"),
            (lambda data: data.update(status="paused"), "invalid_private_health_endpoint_status"),
        ):
            bad = _private_endpoint_payload()
            mutate(bad)
            bad_path = _write_json(directory, "endpoint-bad.json", bad)
            status, payload, posts, _reads = _run_main(
                ["private-health-endpoint-dry-run", "--payload-file", bad_path, "--reason", "r"],
                post=_private_endpoint_response(),
            )
            assert (status, posts) == (2, []), code
            assert payload["warnings"][0]["code"] == code


def test_private_health_endpoint_read_lists_keys_without_urls() -> None:
    response = {
        "status": "ok",
        "trace_id": "launchplane_req_private_endpoints",
        "product": "example",
        "context": "example",
        "instance": "testing",
        "limit": 25,
        "count": 1,
        "records": [_private_endpoint_record(status="disabled")],
    }
    argv = ["private-health-endpoint-read", "--product", "example", "--context", "example"]
    status, payload, _posts, reads = _run_main([*argv, "--instance", "testing"], read=response)
    assert status == 0, payload
    assert reads[0]["path"] == "/v1/private-health-endpoints/records"
    assert reads[0]["query"] == {"product": "example", "context": "example", "instance": "testing"}
    assert payload["result"]["records"] == [
        {
            "endpoint_key": "example-testing-runtime",
            "product": "example",
            "context": "example",
            "instance": "testing",
            "status": "disabled",
            "updated_at": "2026-10-03T12:00:00Z",
        }
    ]
    assert "10.0.0.106" not in json.dumps(payload)

    status, payload, _posts, reads = _run_main(argv, read=response)
    assert status == 0, payload
    assert reads[0]["query"] == {"product": "example", "context": "example"}

    # A field Launchplane adds later is refused rather than passed through.
    leaked = {**response, "records": [{**_private_endpoint_record(), "host": "lxc-106"}]}
    status, payload, _posts, _reads = _run_main(argv, read=leaked)
    assert status == 1
    assert "lxc-106" not in json.dumps(payload)


def _promotion_status_response() -> dict[str, object]:
    availability = {
        "operation": "direct_dry_run",
        "authz_action": "generic_web_prod_promotion.execute",
        "enabled": True,
        "disabled_reasons": [],
        "requires_reason": True,
        "requires_idempotency_key": True,
        "requires_matching_direct_dry_run": False,
        "requires_confirmation": False,
        "consequences": [],
        "trust_state": "verified",
    }
    evidence = {
        "environment": "testing",
        "artifact_id": "ghcr.io/example/app@sha256:" + "a" * 64,
        "source_git_ref": "b" * 40,
        "deployment_status": "pass",
        "health_status": "pass",
        "runtime_identity_status": "match",
        "runtime_identity_detail": "free text",
        "trust_state": "verified",
        "inventory_updated_at": "2026-10-02T12:00:00Z",
    }
    return {
        "status": "ok",
        "trace_id": "launchplane_req_promotion_status",
        "promotion_status": {
            "product": "example-product",
            "context": "example",
            "base_driver_id": "generic-web",
            "repository": "example/private-repo",
            "source_environment": "testing",
            "destination_environment": "prod",
            "source": evidence,
            "destination": {**evidence, "environment": "prod"},
            "release_review": {
                "required": True,
                "approved": False,
                "blockers": ["Client has not accepted."],
                "checklist": {"testing_url": "https://testing.example.invalid"},
            },
            "evidence_fingerprint": "d" * 64,
            "default_bump": "patch",
            "direct_dry_run": availability,
            "workflow_dry_run": {**availability, "enabled": False, "disabled_reasons": ["Needs a dry-run."]},
            "workflow_live": {**availability, "enabled": False, "disabled_reasons": ["See https://x.example.invalid"]},
            "live_confirmations": {"patch": "PROMOTE example-product ... DEPLOY PRODUCTION"},
            "trust_state": "verified",
        },
    }


def test_product_promotion_status_keeps_the_fingerprint_and_drops_release_detail() -> None:
    status, payload, _posts, reads = _run_main(
        ["product-promotion-status-read", "--product", "example-product"],
        read=_promotion_status_response(),
    )
    assert status == 0, payload
    assert reads[0]["path"] == "/v1/products/example-product/environments/prod/promotion-status"
    result = payload["result"]
    assert result["evidence_fingerprint"] == "d" * 64
    assert result["release_review"] == {
        "required": True,
        "approved": False,
        "blocker_count": 1,
        "unavailable": False,
    }
    assert result["availability"]["workflow_dry_run"]["disabled_reason_count"] == 1
    assert result["availability"]["direct_dry_run"]["enabled"] is True
    printed = json.dumps(payload)
    for private in (
        "sha256:",
        "b" * 40,
        "private-repo",
        "PROMOTE",
        "testing.example.invalid",
        "free text",
        "Needs a dry-run",
        "x.example.invalid",
        "Client has not accepted",
    ):
        assert private not in printed, private


def test_product_promotion_dry_run_never_accepts_a_live_result() -> None:
    result = {
        "product": "example-product",
        "context": "example",
        "from_instance": "testing",
        "to_instance": "prod",
        "artifact_id": "ghcr.io/example/app@sha256:" + "a" * 64,
        "promotion_status": "pending",
        "deployment_status": "skipped",
        "backup_status": "pending",
        "source_health_status": "pending",
        "destination_health_status": "pending",
        "release_status": "skipped",
        "target_id": "compose-private-9",
        "dry_run": True,
        "error_message": "",
        "evidence_fingerprint": "d" * 64,
        "bump": "patch",
    }
    response = {
        "status": "accepted",
        "trace_id": "launchplane_req_promotion_dry_run",
        "records": {"release_url": "https://example.invalid/release", "dry_run": "True"},
        "result": result,
    }
    argv = [
        "product-promotion-dry-run",
        "--product",
        "example-product",
        "--evidence-fingerprint",
        "d" * 64,
        "--reason",
        "Check the release before asking.",
        "--idempotency-key",
        "promotion-dry-run-1",
    ]
    status, payload, posts, _reads = _run_main(argv, post=response)
    assert status == 0, payload
    assert posts[0]["path"] == "/v1/products/example-product/environments/prod/promotion/dry-run"
    assert posts[0]["body"] == {
        "schema_version": 1,
        "reason": "Check the release before asking.",
        "evidence_fingerprint": "d" * 64,
        "bump": "patch",
    }
    assert posts[0]["idempotency_key"] == "promotion-dry-run-1"
    printed = json.dumps(payload)
    assert "compose-private-9" not in printed and "release" not in json.dumps(payload["records"])
    assert payload["summary"]["backup_status"] == "pending"
    status, payload, _posts, _reads = _run_main(
        argv, post={**response, "result": {**result, "dry_run": False}}
    )
    assert status == 1 and payload["status"] == "invalid"

def test_operator_free_text_redacts_credentials_and_urls() -> None:
    project = write_action.public_operator_text
    examples = (
        ("Use password=demo-pass before retrying.", "Use [redacted] before retrying."),
        ("Check ssh://deploy@internal.example.invalid/private next.", "Check [redacted] next."),
        ('Use API_KEY="demo key with spaces" safely.', 'Use [redacted] safely.'),
        ("Use 'client_secret': 'demo secret' safely.", "Use [redacted] safely."),
        ("Visit " + "https://" + "demo:pass@example.invalid/private next.", "Visit [redacted] next."),
        ("See https://example.invalid/review next.", "See [redacted] next."),
        ("Use Bearer abcdefghijklmnop next.", "Use [redacted] next."),
        ("Use rk_live_1234567890abcdefghijkl next.", "Use [redacted] next."),
        ("Use ghp_example123 next.", "Use [redacted] next."),
        ('Updated env_vars="password=demo-pass"', 'Updated env_vars=[redacted]'),
        ('Use password="demo secret without closing quote', 'Use [redacted]'),
        ("Testing   is complete.", "Testing is complete."),
    )
    for raw, expected in examples:
        assert project(raw) == expected
        assert project(expected) == expected
        write_action.assert_public_safe_shape({"reason": project(raw)})
        for operation, response in (
            ("product-owner-dry-run", _owner_response(_owner_plan(reason=raw))),
            ("product-image-repository-dry-run", _image_response(_image_plan(reason=raw))),
            ("testing-hold-dry-run", _testing_hold_response(_testing_hold_plan(reason=raw))),
        ):
            output = _saved_dry_run_output(operation, response)
            assert output["result"]["reason"] == expected
    sharing = write_action._project_sharing_reason({
        "kind": "read_only_source", "reason": examples[0][0], "evidence": examples[1][0],
    })
    assert sharing["reason"] == examples[0][1]
    assert sharing["evidence"] == examples[1][1]
    allowance = write_action._project_integration_allowance({
        "integration": "example", "kind": "read_only_source",
        "reason": examples[0][0], "evidence": examples[1][0],
    })
    assert allowance["reason"] == examples[0][1]
    assert allowance["evidence"] == examples[1][1]
    for invalid in (None, {}, "", "x" * 501):
        _expect_error(lambda value=invalid: project(value), "invalid_response")


def test_redacted_reasons_allow_matching_reviewed_apply() -> None:
    for reason in ("Use password=demo-pass safely.", "Check ssh://deploy@internal.example.invalid/private next."):
        with TemporaryDirectory() as directory:
            for operation, argv, response, applied, profiles in (
                ("product-owner", OWNER_DRY_RUN_ARGV,
                 _owner_response(_owner_plan(reason=reason)),
                 _owner_response(_owner_plan(reason=reason, mode="apply", applied=True)),
                 [_profile_response(), _profile_response("example-client", "1234567")]),
                ("product-image-repository", IMAGE_DRY_RUN_ARGV,
                 _image_response(_image_plan(reason=reason)),
                 _image_response(_image_plan(reason=reason, mode="apply", applied=True)),
                 [_image_profile_response(), _image_profile_response(_NEW_IMAGE)]),
            ):
                dry_argv = [*argv[:-1], reason]
                status, evidence, posts, _reads = _run_main(dry_argv, post=response)
                assert status == 0, evidence
                assert posts[0]["body"]["reason"] == reason
                evidence_path = _write_json(directory, operation + ".json", evidence)
                digest = evidence["result"]["plan_sha256"]
                apply_argv = [operation + "-apply", *dry_argv[1:], *_reviewed_apply_argv(digest, evidence_path)]
                profile_reads = iter(profiles)
                status, output, posts, _reads = _run_main(
                    apply_argv, post=applied, read=lambda _kwargs: next(profile_reads)
                )
                assert status == 0, output
                assert posts[0]["body"]["reason"] == reason
                assert output["result"]["reason"] == write_action.public_operator_text(reason)
                different = [operation + "-apply", *argv[1:-1], "A different public reason.",
                             *_reviewed_apply_argv(digest, evidence_path)]
                status, _output, posts, reads = _run_main(different, post=applied, read=profiles[0])
                assert status == 2 and not posts and not reads
            evidence = _saved_dry_run_output(
                "testing-hold-dry-run", _testing_hold_response(_testing_hold_plan(reason=reason))
            )
            path = _write_json(directory, "hold.json", evidence)
            args = _testing_hold_args(reason=reason, idempotency_key="hold-1", reviewed_dry_run=True,
                                      expected_plan_digest=evidence["result"]["plan_sha256"],
                                      dry_run_evidence_file=path)
            assert write_action.testing_hold_body(args, mode="apply")["reason"] == reason


def main() -> int:
    tests = [
        test_operator_free_text_redacts_credentials_and_urls,
        test_redacted_reasons_allow_matching_reviewed_apply,
        test_path_check_reads_both_paths_and_preserves_clear_blocked_unknown,
        test_path_check_refuses_unknown_fields_unsafe_values_and_incomplete_evidence,
        test_path_check_refuses_bad_selector_and_surfaces_read_denial,
        test_product_owner_plan_projection_digests_the_reviewed_change,
        test_client_named_fields_read_like_their_legacy_owner_names,
        test_product_owner_dry_run_sends_normalized_login_to_the_product_route,
        test_product_owner_apply_checks_the_client_reapplies_and_reads_back,
        test_product_image_repository_plan_projection_digests_the_reviewed_move,
        test_product_image_repository_dry_run_sends_the_package_to_the_product_route,
        test_product_image_repository_apply_names_the_reviewed_start_and_reads_back,
        test_production_backup_authority_never_prints_provider_coordinates,
        test_production_backup_authority_apply_binds_the_exact_reviewed_payload,
        test_production_backup_authority_read_keeps_state_and_record_ids,
        test_dokploy_compose_target_hides_provider_ids_and_binds_the_payload,
        test_compose_source_create_reads_back_repository_branch_and_path,
        test_compose_source_completion_binds_source_and_existing_target,
        test_private_health_endpoint_hides_the_url_and_binds_apply_to_the_review,
        test_private_health_endpoint_read_lists_keys_without_urls,
        test_product_promotion_status_keeps_the_fingerprint_and_drops_release_detail,
        test_product_promotion_dry_run_never_accepts_a_live_result,
        test_controller_branch_update_result_reaches_the_caller,
        test_owner_review_reader_keeps_full_prose_and_uses_only_the_private_route,
        test_owner_review_reader_rejects_wrong_subject_or_selected_record,
        test_owner_review_reader_surfaces_denial_without_credentials_or_provider_text,
        test_expected_config_review_binds_metadata_and_never_prints_owner_instructions,
        test_expected_config_removal_shape_is_bound_to_the_request,
        test_expected_config_removal_items_are_plain_identities,
        test_agent_operator_contract_identity_and_provenance_semantics,
        test_agent_operator_contract_rejects_drift_and_unsafe_content,
        test_agent_operator_contract_routes_every_local_consumer,
        test_odoo_addon_settings_projection_redacts_secret_settings,
        test_odoo_addon_settings_body_refuses_plaintext_and_binds_digest,
        test_odoo_addon_settings_cli_dispatches_local_extension_route,
        test_integration_allowances_plan_projection_keeps_diff_and_digest,
        test_integration_allowances_projection_refuses_unknown_fields,
        test_integration_allowances_read_summary_projects_allowances,
        test_integration_allowances_read_shows_why_a_lane_key_is_shared,
        test_integration_allowances_payload_rejects_unknown_fields_and_unreviewed_apply,
        test_testing_hold_plan_projection_is_bounded_and_fail_closed,
        test_testing_hold_read_sends_lane_query_and_projects_hold,
        test_product_environment_read_uses_path_route_and_projects_deploy_identity,
        test_product_environment_read_refuses_bad_segments_and_unsafe_values,
        test_product_activity_read_bounds_events_and_record_links,
        test_product_activity_read_keeps_real_events_and_drops_odd_fields,
        test_product_activity_read_reports_http_denial_as_read_error,
        test_preview_history_read_derives_launchplanes_preview_id,
        test_preview_history_read_projects_newest_generation_first,
        test_preview_history_read_needs_exactly_one_selector,
        test_reconcile_requests_read_keeps_the_decision_and_drops_the_rest,
        test_reconcile_requests_read_keeps_testing_operation_ids,
        test_reconcile_requests_read_keeps_generic_web_testing_outcome,
        test_target_replacement_operation_read_keeps_progress_and_drops_error_text,
        test_target_replacement_operation_read_projects_failure_details,
        test_target_replacement_operation_read_bounds_failure_details,
        test_target_replacement_operation_read_tolerates_a_pending_operation,
        test_target_replacement_operation_read_refuses_bad_ids_and_unsafe_values,
        test_target_replacement_plan_read_keeps_key_names_and_drops_text,
        test_target_replacement_plan_read_marks_missing_key_lists_as_unreported,
        test_target_replacement_plan_read_reports_denial_with_trace,
        test_target_replacement_plan_read_refuses_bad_input_and_unsafe_values,
        test_testing_hold_body_binds_apply_to_saved_dry_run,
        test_testing_hold_cli_dispatches_local_extension_route,
        test_product_repository_identity_projection_is_bounded_and_fail_closed,
        test_product_repository_identity_apply_requires_saved_dry_run,
        test_product_config_secret_results_keep_declared_secret_class,
        test_product_config_projection_keeps_declared_secret_class_end_to_end,
        test_product_config_secret_copy_keeps_the_source_through_dry_run_apply_and_replay,
        test_product_config_secret_copy_refuses_a_value_or_malformed_source,
        test_product_config_secret_copy_projection_refuses_extra_source_fields,
        test_product_secret_bindings_read_keeps_metadata_and_counts_the_rest,
        test_product_secret_bindings_read_fails_closed_on_values_and_ciphertext,
        test_repository_inventory_review_evidence_binds_exact_private_payload,
        test_repository_inventory_projection_is_bounded_and_fail_closed,
        test_agent_operator_contract_cli_is_public_safe_and_hermetic,
        test_merge_train_policy_import_body_projection_and_redaction,
        test_merge_train_policy_import_apply_requires_bound_evidence,
        test_merge_train_policy_import_preflight_blocks_stale_policy_before_post,
        test_merge_train_policy_import_uses_exact_read_and_write_routes,
        test_merge_train_policy_import_apply_handles_ambiguous_success_and_transport,
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
        test_change_impact_policy_read_optional_legacy_metadata,
        test_change_impact_policy_read_rejects_malformed_metadata,
        test_change_impact_policy_read_execution_projects_current_service_response,
        test_change_impact_current_policy_responses_preserve_bounded_attribution,
        test_change_impact_current_policy_rejects_invalid_nested_metadata,
        test_change_impact_policy_v2_response_fields_are_validated_and_redacted,
        test_change_impact_apply_success_projection_failure_is_unverified,
        test_invalid_private_payload_does_not_expose_path,
        test_product_config_projection_accepts_context_scoped_runtime_environment,
        test_runtime_retirement_projection_rejects_values_and_malformed_metadata,
        test_optional_public_identifier_rejects_null_values,
        test_runtime_environment_projection_enforces_scope_identity,
        test_merge_train_idle_preserves_author_refusal_without_pr_content,
        test_controller_timeout_covers_slow_dry_run_and_mutation_and_keeps_override,
        test_controller_block_and_reconciliation_preserve_durable_diagnostics,
        test_controller_client_timeout_is_not_a_service_outage_and_never_retries,
        test_controller_rejected_response_keeps_only_safe_trace_and_code,
        test_controller_http_error_keeps_safe_identifiers_without_raw_error_text,
        test_merge_train_queue_distinguishes_eligible_empty_and_unavailable,
        test_merge_train_queue_rejects_malformed_or_sensitive_evidence,
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



def _odoo_addon_settings_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "example-odoo",
        "context": "example",
        "instance": "testing",
        "addon": "shopify",
        "reason": "Load the development store for testing restores.",
        "shopify": {
            "shop_url_key": "example-dev-store",
            "api_version": "2025-07",
            "api_token_secret_binding_id": "secret-shopify-api-token-binding",
            "webhook_key_secret_binding_id": "secret-shopify-webhook-key-binding",
            "test_store": True,
        },
    }


def _odoo_addon_settings_result(*, mode: str = "dry-run") -> dict[str, object]:
    return {
        "status": "ok",
        "mode": mode,
        "product": "example-odoo",
        "context": "example",
        "instance": "testing",
        "addon": "shopify",
        "production_lane": False,
        "record_exists": True,
        "changed": True,
        "applied": mode == "apply",
        "rendered_action": "apply",
        "changes": [
            {
                "setting": "shop_url_key",
                "action": "add",
                "before": None,
                "after": {
                    "setting": "shop_url_key",
                    "source": "literal",
                    "value": "example-dev-store",
                    "value_present": True,
                    "secret_binding_id": "",
                    "secret_binding_present": None,
                },
            },
            {
                "setting": "api_token",
                "action": "update",
                "before": {
                    "setting": "api_token",
                    "source": "literal",
                    "value": None,
                    "value_present": True,
                    "secret_binding_id": "",
                    "secret_binding_present": None,
                },
                "after": {
                    "setting": "api_token",
                    "source": "secret_binding",
                    "value": None,
                    "value_present": True,
                    "secret_binding_id": "secret-shopify-api-token-binding",
                    "secret_binding_present": True,
                },
            },
        ],
        "read_back": [],
        "read_back_matches": True if mode == "apply" else None,
        "reason": "Load the development store for testing restores.",
        "source_label": "service:odoo-addon-settings",
        "record_sha256_before": "b" * 64,
        "record_sha256_after": "c" * 64 if mode == "apply" else "",
        "plan_sha256": "a" * 64,
        "next_actions": ["Run Odoo post-deploy for this lane."],
    }


def _odoo_addon_settings_evidence(**result_overrides: object) -> dict[str, Any]:
    projected: dict[str, Any] = write_action.summarize_success(
        operation="odoo-addon-settings-dry-run",
        request={"mode": "dry-run", "payload_source": "private_file"},
        provider_payload={
            "status": "accepted",
            "trace_id": "launchplane_req_addon_settings",
            "records": {"product_profile": "example-odoo", "context": "example", "instance": "testing"},
            "result": {**_odoo_addon_settings_result(), **result_overrides},
        },
    )
    return projected


def test_odoo_addon_settings_projection_redacts_secret_settings() -> None:
    projected = _odoo_addon_settings_evidence()
    assert projected["status"] == "accepted"
    assert projected["summary"]["plan_sha256"] == "a" * 64
    changes = {change["setting"]: change for change in projected["result"]["changes"]}
    assert changes["shop_url_key"]["after"]["literal"] == "example-dev-store"
    assert changes["api_token"]["after"]["binding_ref"] == "secret-shopify-api-token-binding"
    assert changes["api_token"]["after"]["binding_present"] is True
    assert "literal" not in changes["api_token"]["before"]
    assert changes["api_token"]["before"]["present"] is True

    leaked = _odoo_addon_settings_result()
    leaked_changes = cast(list[dict[str, Any]], leaked["changes"])
    leaked_before = cast(dict[str, Any], leaked_changes[1]["before"])
    leaked_before["value"] = "plaintext-token-value"
    try:
        write_action.summarize_success(
            operation="odoo-addon-settings-dry-run",
            request={},
            provider_payload={"status": "accepted", "records": {}, "result": leaked},
        )
    except write_action.LaunchplaneSafetyError:
        pass
    else:
        raise AssertionError("secret literal must not be projected")

    extra = {**_odoo_addon_settings_result(), "unexpected": "field"}
    try:
        write_action.summarize_success(
            operation="odoo-addon-settings-dry-run",
            request={},
            provider_payload={"status": "accepted", "records": {}, "result": extra},
        )
    except write_action.LaunchplaneSafetyError:
        pass
    else:
        raise AssertionError("unknown result fields must fail closed")


def test_odoo_addon_settings_body_refuses_plaintext_and_binds_digest() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "addon-settings.json"
        evidence_path = Path(directory) / "addon-settings-dry-run.json"
        payload_path.write_text(json.dumps(_odoo_addon_settings_payload()), encoding="utf-8")
        evidence_path.write_text(json.dumps(_odoo_addon_settings_evidence()), encoding="utf-8")

        dry_run_body = write_action.odoo_addon_settings_body(
            argparse.Namespace(payload_file=str(payload_path), idempotency_key=""),
            mode="dry-run",
        )
        assert dry_run_body["mode"] == "dry-run"
        assert "reviewed_plan_sha256" not in dry_run_body

        apply_args = argparse.Namespace(
            payload_file=str(payload_path),
            idempotency_key="example-testing-shopify-1",
            reviewed_dry_run=True,
            expected_plan_digest="a" * 64,
            dry_run_evidence_file=str(evidence_path),
        )
        apply_body = write_action.odoo_addon_settings_body(apply_args, mode="apply")
        assert apply_body["reviewed_plan_sha256"] == "a" * 64

        for overrides, code in (
            ({"reviewed_dry_run": False}, "reviewed_dry_run_required"),
            ({"expected_plan_digest": "b" * 64}, "reviewed_dry_run_not_apply_eligible"),
            ({"expected_plan_digest": "short"}, "invalid_expected_plan_digest"),
            ({"idempotency_key": ""}, "idempotency_key_required"),
        ):
            args = argparse.Namespace(**{**vars(apply_args), **overrides})
            try:
                write_action.odoo_addon_settings_body(args, mode="apply")
            except ValueError as exc:
                assert str(exc) == code
            else:
                raise AssertionError(f"expected {code}")

        plaintext = _odoo_addon_settings_payload()
        cast(dict[str, object], plaintext["shopify"])["api_token"] = "plaintext"
        payload_path.write_text(json.dumps(plaintext), encoding="utf-8")
        try:
            write_action.odoo_addon_settings_body(
                argparse.Namespace(payload_file=str(payload_path), idempotency_key=""),
                mode="dry-run",
            )
        except ValueError as exc:
            assert str(exc) == "unsupported_shopify_field"
        else:
            raise AssertionError("plaintext secret field must be refused")


def test_odoo_addon_settings_cli_dispatches_local_extension_route() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "addon-settings.json"
        evidence_path = Path(directory) / "addon-settings-dry-run.json"
        payload_path.write_text(json.dumps(_odoo_addon_settings_payload()), encoding="utf-8")
        evidence_path.write_text(json.dumps(_odoo_addon_settings_evidence()), encoding="utf-8")
        calls: list[dict[str, Any]] = []

        def fake_execute_post(**kwargs: Any) -> int:
            calls.append(kwargs)
            return 0

        with temporary_attribute(write_action, "execute_post", fake_execute_post):
            assert (
                write_action.main(
                    ["odoo-addon-settings-dry-run", "--payload-file", str(payload_path)]
                )
                == 0
            )
            assert (
                write_action.main(
                    [
                        "odoo-addon-settings-apply",
                        "--payload-file",
                        str(payload_path),
                        "--idempotency-key",
                        "example-testing-shopify-1",
                        "--reviewed-dry-run",
                        "--expected-plan-digest",
                        "a" * 64,
                        "--dry-run-evidence-file",
                        str(evidence_path),
                    ]
                )
                == 0
            )
    assert [call["path"] for call in calls] == [
        "/v1/product-config/odoo-addon-settings/apply",
        "/v1/product-config/odoo-addon-settings/apply",
    ]
    assert calls[0]["body"]["mode"] == "dry-run"
    assert calls[1]["body"]["mode"] == "apply"
    assert "secret-shopify-api-token-binding" not in json.dumps(calls[1]["request"])


def test_client_named_fields_read_like_their_legacy_owner_names() -> None:
    argv = ["product-profile-read", "--product", "example-product"]
    _status, legacy, _calls = _run_product_read(argv, _product_profile_response())
    renamed = _product_profile_response()
    profile = cast(dict[str, Any], renamed["profile"])
    profile["client"] = profile.pop("owner")
    status, payload, _calls = _run_product_read(argv, renamed)
    assert status == 0
    assert payload["result"] == legacy["result"]
    both = copy.deepcopy(renamed)
    cast(dict[str, Any], both["profile"])["owner"] = {"github_login": "someone-else", "github_id": "7"}
    status, _payload, _calls = _run_product_read(argv, both)
    assert status == 1

    plan = _owner_plan()
    renamed_plan = {**plan, "client_before": plan["owner_before"], "client_after": plan["owner_after"]}
    del renamed_plan["owner_before"], renamed_plan["owner_after"]
    assert write_action._project_product_owner_plan(renamed_plan) == write_action._project_product_owner_plan(plan)
    _expect_error(
        lambda: write_action._project_product_owner_plan(
            {**plan, "client_after": {"github_login": "someone-else", "github_id": "7"}}
        ),
        "invalid_response",
    )

    decision = {
        "record_id": "decision-one", "product": "example-site", "repository": "example/site",
        "pull_request_number": 42, "head_sha": "a" * 40, "preview_url": "https://preview.example.invalid",
        "decision": "accepted", "reason": "Looks right.",
        "owner_github_id": "9001", "owner_github_login": "example-client",
        "decided_at": "2026-09-26T12:00:00Z", "feedback_url": "https://github.com/example/site/pull/42#issuecomment-1",
    }
    renamed_decision = {
        **{key: value for key, value in decision.items() if not key.startswith("owner_")},
        "client_github_id": "9001", "client_github_login": "example-client",
    }
    conflicting = {**decision, "client_github_login": "someone-else"}
    settings = {"service_url": "https://private.example.invalid", "token": "private-credential"}
    for latest, expected in (
        (renamed_decision, {"ok": True, "decision": decision}),
        (conflicting, {"ok": False, "error": "owner_review_read_unavailable"}),
    ):
        payload = {"status": "ok", "repository": "example/site", "pull_request_number": 42, "latest_decision": latest}
        with patch.object(owner_review, "resolve_settings", return_value=settings), patch.object(
            owner_review, "request_launchplane_read", return_value=payload
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                owner_review.main(["--repo", "example/site", "--pr", "42"])
            assert json.loads(output.getvalue()) == expected


if __name__ == "__main__":
    raise SystemExit(main())
