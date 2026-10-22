#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
# ]
# ///
"""Focused regression tests for Launchplane helper trust boundaries."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import io
import http.client
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
from urllib.parse import parse_qs, urlsplit

import pytest
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


def test_agent_operator_contract_routes_every_local_consumer() -> None:
    for lookup, key in ((contract.operation_path, "unknown_operation"),
                        (contract.helper_command_path, "unknown_command"),
                        (contract.internal_helper_path, "unknown_route")):
        try:
            lookup(key)
        except contract.ContractError:
            pass
        else:
            raise AssertionError("unknown bindings must fail closed")
    context_args = argparse.Namespace(
        repo="example/repo", branch=None, issue=None, pr=None
    )
    assert context_helper.build_context_url(
        "https://launchplane.example.invalid", context_args
    ) == (
        "https://launchplane.example.invalid"
        f"{contract.operation_path('read_agent_context')}?repository=example%2Frepo"
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
    source: object = None,
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
                "source": record["source"] if source is None else source,
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


def test_merge_train_policy_import_accepts_free_text_source_without_emitting_it() -> None:
    def summarize(source: object) -> dict[str, object]:
        return write_action.summarize_merge_train_policy_import_success(
            operation="merge-train-policy-import-dry-run",
            request={"mode": "dry_run"},
            current_policy=_merge_train_policy_targets_projection(),
            provider_payload=_merge_train_policy_import_response(source=source),
            expected_record_id=f"merge-train-policy-20260829T203000Z-{'c' * 12}",
            expected_policy_sha256="c" * 64,
        )

    free_text = "PRIVATE-D1 Q999 private-tap enrollment"
    evidence = summarize(free_text)
    evidence_result = evidence["result"]
    assert isinstance(evidence_result, dict)
    candidate = evidence_result["candidate"]
    assert isinstance(candidate, dict)
    assert candidate["target_count"] == 1
    assert free_text not in json.dumps(evidence)

    for invalid_source in ("   ", 7):
        try:
            summarize(invalid_source)
        except safety.LaunchplaneSafetyError as exc:
            assert exc.code == "invalid_response"
        else:
            raise AssertionError(f"expected source {invalid_source!r} to fail closed")


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


def test_invalid_private_payload_does_not_expose_path() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "private-payload.json"
        payload_path.write_text("{broken", encoding="utf-8")
        try:
            write_action.read_payload_file(str(payload_path))
        except ValueError as exc:
            assert str(exc) == "invalid_payload"
            assert str(payload_path) not in str(exc)
        else:
            raise AssertionError("expected invalid private payload rejection")


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("root_alias", [False, True])
@pytest.mark.parametrize(
    ("spelling", "repo_local"),
    [
        ("repo/local.json", True),
        ("repo/local-link.json", True),
        ("alias/local.json", True),
        ("alias/local-link.json", True),
        ("external-local-link.json", True),
        ("private.json", False),
        ("external-private-link.json", False),
    ],
)
def test_private_payload_placement_through_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str,
    repo_local: bool, relative: bool, root_alias: bool,
) -> None:
    fixture_root = tmp_path.resolve()
    repo_root = fixture_root / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo_root)], check=True)
    payload = {"reason": "Disposable private payload fixture."}
    private_path = fixture_root / "private.json"
    private_path.write_text(json.dumps(payload), encoding="utf-8")
    local_path = repo_root / "local.json"
    local_path.write_text(json.dumps(payload), encoding="utf-8")
    (repo_root / "local-link.json").symlink_to(private_path)
    (fixture_root / "alias").symlink_to(repo_root, target_is_directory=True)
    (fixture_root / "external-local-link.json").symlink_to(local_path)
    (fixture_root / "external-private-link.json").symlink_to(private_path)
    monkeypatch.chdir(repo_root)
    if root_alias:
        # Exercise actual directory identity even on case-sensitive CI hosts.
        monkeypatch.setattr(write_action, "active_repo_root", lambda: fixture_root / "alias")
    payload_path = fixture_root / spelling
    path = os.path.relpath(payload_path, repo_root) if relative else str(payload_path)
    if repo_local:
        with pytest.raises(ValueError, match="^repo_local_payload_unsupported$"):
            write_action.read_payload_file(path)
    else:
        assert write_action.read_payload_file(path) == payload


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize(
    "spelling",
    [
        "repository/local.json",
        "REPOSITORY/local.json",
        "REPOSITORY/nested/local.json",
        "REPOSITORY/local-link.json",
        "external-case-local-link.json",
        "external-case-private-link.json",
    ],
)
def test_private_payload_placement_uses_filesystem_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str, relative: bool,
) -> None:
    fixture_root = tmp_path.resolve()
    repo_root = fixture_root / "repository"
    repo_root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo_root)], check=True)
    case_path = fixture_root / "REPOSITORY"
    case_alias = case_path.exists() and case_path.samefile(repo_root)
    if not case_alias:
        # On a case-sensitive filesystem this is a distinct external directory.
        case_path.mkdir()
    payload = {"fixture": True}
    private_path = fixture_root / "private.json"
    private_path.write_text(json.dumps(payload), encoding="utf-8")
    for directory in {repo_root, case_path}:
        (directory / "local.json").write_text(json.dumps(payload), encoding="utf-8")
        (directory / "nested").mkdir(exist_ok=True)
        (directory / "nested" / "local.json").write_text(json.dumps(payload), encoding="utf-8")
        link = directory / "local-link.json"
        if not link.is_symlink():
            link.symlink_to(private_path)
    (fixture_root / "external-case-local-link.json").symlink_to(case_path / "local.json")
    (fixture_root / "external-case-private-link.json").symlink_to(private_path)
    monkeypatch.chdir(repo_root)
    payload_path = fixture_root / spelling
    path = os.path.relpath(payload_path, repo_root) if relative else str(payload_path)
    repo_local = spelling.startswith("repository/") or (
        case_alias and spelling != "external-case-private-link.json"
    )
    if repo_local:
        with pytest.raises(ValueError, match="^repo_local_payload_unsupported$"):
            write_action.read_payload_file(path)
    else:
        assert write_action.read_payload_file(path) == payload


def test_private_payload_identity_failure_refuses_without_exposing_path(
    tmp_path: Path,
) -> None:
    payload_path = tmp_path / "private.json"
    payload_path.write_text(json.dumps({"fixture": True}), encoding="utf-8")
    with patch.object(Path, "samefile", side_effect=OSError(str(payload_path))):
        with pytest.raises(ValueError, match="^invalid_payload_path$"):
            write_action.read_payload_file(str(payload_path))


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


def test_controller_projects_stack_child_dispositions_without_private_comment_text() -> None:
    response = _queue_refusal_response()
    response["result"]["stack_collapse_plan"] = {
        "collapse_id": "collapse-example", "root_pull_request_number": 42,
        "child_dispositions": [{"pull_request_number": 43, "expected_head_sha": "b" * 40,
                                "status": "closed", "detail": "Private child disposition prose",
                                "comment_url": "https://github.com/example/repo/pull/43#issuecomment-1"}],
    }
    code, payload = _run_controller_response(response)
    assert code == 0
    stack = payload["result"]["stack_collapse_plan"]
    assert stack["root_pull_request_number"] == 42
    assert stack["child_dispositions"] == [{"pull_request_number": 43, "expected_head_sha": "b" * 40, "status": "closed"}]
    assert "Private child disposition prose" not in json.dumps(payload)
    assert "issuecomment-1" not in json.dumps(payload)


@pytest.mark.parametrize("child", [None, "closed", {"pull_request_number": True},
                                    {"pull_request_number": 0},
                                    {"pull_request_number": 43, "expected_head_sha": "ghp_" + "a" * 36, "status": "closed"}])
def test_controller_rejects_invalid_or_unsafe_stack_child_dispositions(child: object) -> None:
    response = _queue_refusal_response()
    response["result"]["stack_collapse_plan"] = {"child_dispositions": [child]}
    code, payload = _run_controller_response(response)
    assert code != 0
    assert payload["status"] == "invalid"
    assert not payload.get("result")


def _controller_last_result_fixture() -> dict[str, object]:
    return {
        "repository": "example/repo", "base_branch": "main", "policy_key": "example-main",
        "policy_sha256": "a" * 64, "pass_started_at": "2026-10-12T18:00:00Z",
        "observed_at": "2026-10-12T18:00:10Z", "trace_id": "launchplane_req_example",
        "action": "candidate_failed", "reason_code": "candidate_check_retry_used",
        "record_id": "failed-record", "source_failed_record_id": "source-record",
        "candidate_batch_id": "replacement-batch", "recovery_kind": "candidate_checks_recovered",
        "candidate_sha": "b" * 40, "observed_candidate_sha": "b" * 40, "base_sha": "c" * 40,
        "candidate_checks_status": "pass", "base_checks_status": "not_observed",
        "check_retry_of": "source-record", "infrastructure_retry_of": "", "batch_body_retry_of": "",
        "reconciliation_required": False, "semantic_progress_at": "2026-10-12T17:00:00Z",
        "next_actor": "repository_agent", "next_observation": "Repair the failed checks; then reobserve the lineage.",
    }


def test_controller_projects_bounded_last_result_without_losing_empty_evidence() -> None:
    diagnosis = _controller_last_result_fixture()
    response = {"status": "accepted", "trace_id": diagnosis["trace_id"], "records": {}, "result": {"controller_action": diagnosis["action"], "last_controller_result": diagnosis}}
    status, payload = _run_controller_response(response, mutate=True)
    assert status == 0
    assert payload["result"]["last_controller_result"] == diagnosis


@pytest.mark.parametrize("change", [
    {"token": "secret-value"}, {"reason_code": "ghp_" + "a" * 36},
    {"next_observation": "x" * 257}, {"candidate_checks_status": "green"},
    {"reconciliation_required": "false"}, {"next_actor": "needs_owner"},
])
def test_controller_rejects_unsafe_or_unbound_diagnostic_shapes(change: dict[str, object]) -> None:
    diagnosis = {**_controller_last_result_fixture(), **change}
    response = {"status": "accepted", "trace_id": "launchplane_req_example", "records": {}, "result": {"controller_action": "candidate_failed", "last_controller_result": diagnosis}}
    received = write_action._ServiceResponse(response, 202)
    status, payload = _run_controller_response(received, mutate=True)
    assert status != 0
    assert not payload.get("result")
    assert payload["summary"]["http_status"] == received.http_status
    assert payload["summary"]["trace_id"] == response["trace_id"]
    assert "secret-value" not in json.dumps(payload)


def test_controller_block_and_reconciliation_preserve_durable_diagnostics() -> None:
    result = {
        "controller_action": "block", "mode": "blocked",
        "blocking_reason": {"code": "merge_readiness_not_ready", "message": "Required checks failed."},
        "merge_readiness": {
            "state": "blocked_checks", "reason_codes": ["checks_failed"], "owner_states": ["ready"],
            "technical_checks_state": "failed", "engineering_review_state": "ready",
            "policy_state": "ready", "candidate_state": "ready", "fence_state": "ready",
        },
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


def test_controller_applied_block_reaches_driver_with_attribution_and_policy() -> None:
    block = {"status": "blocked", "repository": "example/repo", "base_branch": "main",
             "pull_request_number": 42, "blocked_label": "held", "train_should_continue": True,
             "detail": "Applied held to pull request #42."}
    response = _queue_refusal_response()
    response["result"]["controller_action"] = "block"
    response["result"]["mode"] = "block"
    response["result"]["dry_run_result"]["selected_pr"] = response["result"]["dry_run_result"]["queue"][0]
    response["result"]["block_result"] = block
    status, payload = _run_controller_response(response, mutate=True)
    assert status == 0
    assert payload["result"]["block_result"] == block
    driver = load_module("launchplane-train-drive.py", "block_projection_driver")
    settings = driver.DriveSettings(repository="example/repo", number=43)
    assert driver._judge(settings, None, driver.DriveState(), payload["result"], "block") is None
    block["train_should_continue"] = False
    status, payload = _run_controller_response(response, mutate=True)
    assert status == 0
    outcome, detail = driver._judge(settings, None, driver.DriveState(), payload["result"], "block")
    assert outcome == "needs_owner"
    assert detail["blocking_pull_request_number"] == 42


def test_controller_block_projection_rejects_unsafe_or_invalid_fields() -> None:
    for block in (
        {"status": "blocked", "pull_request_number": 42, "train_should_continue": "true"},
        {"status": "blocked", "pull_request_number": True, "train_should_continue": True},
    ):
        response = _queue_refusal_response()
        response["result"]["block_result"] = block
        status, payload = _run_controller_response(response, mutate=True)
        assert status != 0
        assert payload["status"] == "invalid"


def _candidate_failure_isolated_response() -> dict[str, Any]:
    """Launchplane's culprit block after a batch candidate failed (merge_train_controller_run_once)."""
    response = _queue_refusal_response()
    result = response["result"]
    culprit = {**result["dry_run_result"]["queue"][0], "eligible": True, "ineligible_reasons": [],
               "actor_role": "repo_admin"}
    detail = "Pull request #42 failed the merge-train candidate checks when tested without the other queued pull requests."
    result.update({
        "mode": "block", "controller_action": "block", "reason_code": "candidate_failure_isolated",
        "merge_train_batch_candidate_record_id": "merge-train-batch-candidate-intent",
        "retired_merge_train_batch_candidate_record_id": "merge-train-batch-candidate-failed",
        "blocking_reason": {"code": "merge_train_candidate_failure_isolated", "message": detail},
        "details": {
            "failed_checks": [{"name": "test_shard (0)", "conclusion": "failure",
                               "url": "https://github.com/example/repo/actions/runs/1/job/2"}],
            "failed_tests": ["tests.test_widget.WidgetTests.test_renders"],
            "recovery_source_record_id": "merge-train-batch-candidate-source",
        },
        "conflict_probe": {"status": "persisted", "pull_request_numbers": [], "held_out": [
            {"pull_request_number": 43, "head_sha": "3" * 40, "reason": "failure_isolation", "conflicts_with": []},
        ]},
        "block_result": {"status": "blocked", "repository": "example/repo", "base_branch": "main",
                         "pull_request_number": 42, "blocked_label": "merge-blocked",
                         "train_should_continue": True, "detail": "Applied merge-blocked to pull request #42."},
    })
    result["dry_run_result"].update({"selected_pr": culprit, "intended_next_action": "block",
                                     "next_action_detail": detail})
    response["records"] = {
        "merge_train_batch_candidate_record_id": "merge-train-batch-candidate-intent",
        "retired_merge_train_batch_candidate_record_id": "merge-train-batch-candidate-failed",
    }
    return response


def test_controller_candidate_failure_isolated_block_is_a_confirmed_result() -> None:
    response = _candidate_failure_isolated_response()
    status, payload = _run_controller_response(response, mutate=True)
    assert status == 0
    assert payload["status"] == "accepted"
    assert payload["records"] == response["records"]
    result = payload["result"]
    assert result["reason_code"] == "candidate_failure_isolated"
    assert result["retired_merge_train_batch_candidate_record_id"] == response["records"][
        "retired_merge_train_batch_candidate_record_id"]
    assert result["block_result"] == response["result"]["block_result"]
    assert payload["summary"]["error_code"] == "merge_train_candidate_failure_isolated"
    assert "private" not in json.dumps(payload)
    driver = load_module("launchplane-train-drive.py", "culprit_block_driver")
    assert not driver._uncertain(payload)
    # The held-out innocent PR keeps going; the culprit's own driver stops.
    innocent = driver.DriveSettings(repository="example/repo", number=43)
    assert driver._judge(innocent, None, driver.DriveState(), result, "block") is None
    culprit = driver.DriveSettings(repository="example/repo", number=42)
    assert driver._judge(culprit, None, driver.DriveState(), result, "block")[0] == "failed"


def test_controller_land_batch_reports_landing_progress() -> None:
    for progress in ("partial", "cleanup_pending"):
        response = {"status": "accepted", "trace_id": "launchplane_req_land", "records": {}, "result": {
            "controller_action": "land_batch", "mode": "land", "landing_progress": progress,
            "merge_train_batch_landing_plan_record_id": "landing-plan-example",
        }}
        status, payload = _run_controller_response(response, mutate=True)
        assert status == 0
        assert payload["result"]["landing_progress"] == progress
    response["result"]["landing_progress"] = "Bearer private-fixture-value"
    status, payload = _run_controller_response(response, mutate=True)
    assert status == 1 and "private-fixture-value" not in json.dumps(payload)


def test_controller_unreadable_reply_says_whether_launchplane_accepted_it() -> None:
    body = _candidate_failure_isolated_response()
    body["result"]["block_result"]["train_should_continue"] = "private-fixture-value"
    for http_status, service_status, accepted in (
        (202, "accepted", True), (200, "accepted", True), (202, "pending", False), (None, "accepted", False),
    ):
        response = write_action._ServiceResponse({**body, "status": service_status}, http_status)
        status, payload = _run_controller_response(response, mutate=True)
        assert status == 1 and payload["status"] == "invalid"
        assert payload["summary"]["projection_error_code"] == "invalid_response"
        assert (payload["summary"].get("launchplane_status") == "accepted") is accepted
        assert "private-fixture-value" not in json.dumps(payload)


def test_controller_client_timeout_is_not_a_service_outage_and_never_retries() -> None:
    response = Mock(status=202)
    response.read.side_effect = TimeoutError("private-provider")
    with pytest.raises(TimeoutError) as interrupted:
        write_action._read_service_response(response)
    for error in (
        TimeoutError("timed out"),
        urllib.error.URLError(TimeoutError("timed out")),
        interrupted.value,
    ):
        for mutate in (False, True):
            status, payload = _run_controller_response(error, mutate=mutate, timeout=7.5)
            assert status == 1
            assert payload["summary"]["error_code"] == "client_timeout"
            assert payload["summary"]["timeout_seconds"] == 7.5
            assert payload["warnings"][0]["code"] == "client_timeout"
            assert "7.5 s" in payload["warnings"][0]["message"]
            if error is interrupted.value:
                assert payload["summary"]["http_status"] == response.status
            assert "private-provider" not in json.dumps(payload)
    for error in (
        urllib.error.URLError("connection refused"),
        http.client.RemoteDisconnected("remote disconnected"),
    ):
        status, payload = _run_controller_response(error)
        assert status == 1
        assert payload["warnings"][0]["code"] == "provider_unavailable"


def _conflict_probe_response(probe: object, action: str = "plan_candidate") -> dict[str, Any]:
    return {
        "status": "accepted", "trace_id": "launchplane_req_probe", "records": {},
        "result": {"controller_action": action, "conflict_probe": probe},
    }


def test_controller_conflict_probe_preserves_planning_and_update_branch_evidence() -> None:
    held_out = {
        "pull_request_number": 43, "head_sha": "b" * 40,
        "reason": "entry_conflict", "conflicts_with": [42],
    }
    for probe, mutate in (
        ({"status": "will_run", "pull_request_numbers": [42, 43, 44]}, False),
        ({"status": "ran", "pull_request_numbers": [42, 43, 44], "held_out": [held_out]}, True),
        ({"status": "ran", "pull_request_numbers": [42, 43], "held_out": []}, True),
        ({"status": "ran", "pull_request_numbers": [43], "held_out": [
            {**held_out, "conflicts_with": []},
        ]}, True),
    ):
        status, payload = _run_controller_response(_conflict_probe_response(probe), mutate=mutate)
        assert status == 0
        assert payload["status"] == "accepted"
        assert payload["result"]["conflict_probe"] == probe
        assert payload["summary"]["controller_action"] == "plan_candidate"
    for probe in (None, {"status": "will_run", "pull_request_numbers": [42, 43]}):
        response = _conflict_probe_response(probe, "update_branch")
        response["result"]["branch_update_result"] = {"status": "updated"}
        status, payload = _run_controller_response(response, mutate=True)
        assert status == 0
        assert payload["result"]["conflict_probe"] == probe
        assert payload["summary"]["controller_action"] == "update_branch"
        assert payload["result"]["branch_update_result"] == {"status": "updated"}
    response = _conflict_probe_response(None)
    del response["result"]["conflict_probe"]
    assert "conflict_probe" not in _run_controller_response(response)[1]["result"]


def test_controller_conflict_probe_rejects_malformed_and_unsafe_evidence() -> None:
    probe: dict[str, Any] = {"status": "ran", "pull_request_numbers": [42, 43], "held_out": [{
        "pull_request_number": 43, "head_sha": "b" * 40,
        "reason": "entry_conflict", "conflicts_with": [42],
    }]}
    invalid = [False, [], "ran", {}]
    for key, value in (
        ("status", []), ("status", "Bearer private-probe-value"),
        ("pull_request_numbers", {}), ("pull_request_numbers", [True]),
        ("pull_request_numbers", [0]), ("held_out", {}), ("held_out", [None]),
    ):
        invalid.append({**probe, key: value})
    for key, value in (
        ("pull_request_number", True), ("pull_request_number", 0),
        ("head_sha", None), ("head_sha", "ghp_privateprobevalue"),
        ("reason", []), ("reason", "Bearer private-probe-value"),
        ("conflicts_with", "42"), ("conflicts_with", [False]),
        ("conflicts_with", [0]), ("token", "private-probe-value"),
    ):
        invalid.append({**probe, "held_out": [{**probe["held_out"][0], key: value}]})
    for malformed in invalid:
        status, payload = _run_controller_response(_conflict_probe_response(malformed), mutate=True)
        assert status == 1, malformed
        assert payload["status"] == "invalid"
        assert payload["result"] == {}
        assert "private-probe-value" not in json.dumps(payload)


def test_controller_reply_fields_newer_than_the_pin_are_dropped_and_named() -> None:
    response = _candidate_failure_isolated_response()
    response["result"]["later_result_field"] = "private-fixture-value"
    # Fields an older helper projected but the contract does not declare are dropped too.
    response["result"]["workflow_run_url"] = "https://private.example/path"
    response["result"]["candidate_sha"] = "d" * 40
    response["records"]["later_record_id"] = "private-record-value"
    response["result"]["block_result"]["later_block_field"] = "private-block-value"
    status, payload = _run_controller_response(response, mutate=True)
    assert status == 0 and payload["status"] == "accepted"
    assert payload["result"]["block_result"]["pull_request_number"] == 42
    assert payload["summary"]["controller_action"] == "block"
    assert payload["summary"]["unrecognized_fields"].split(",") == [
        "records.later_record_id", "result.block_result.later_block_field", "result.candidate_sha",
        "result.later_result_field", "result.workflow_run_url",
    ]
    assert "candidate_sha" not in payload["result"] and "private.example" not in json.dumps(payload)
    assert payload["warnings"][0]["code"] == "controller_reply_newer_than_pinned_schema"
    for value in ("private-fixture-value", "private-record-value", "private-block-value"):
        assert value not in json.dumps(payload)
    response["records"]["later_record_id"] = "Bearer private-record-value"
    status, payload = _run_controller_response(response, mutate=True)
    assert status == 1 and payload["summary"]["projection_error_code"] == "unsafe_response_shape"
    assert "private-record-value" not in json.dumps(payload)


@pytest.mark.parametrize("key", ["https://private.example/path", "Bearer private-fixture-value", "ghp_privatefixture", "client_secret", "x" * 81])
def test_controller_rejected_response_omits_unsafe_unknown_key(key: str) -> None:
    response = {"status": "accepted", "trace_id": "launchplane_req_diagnostic", "records": {},
                "result": {key: "private-fixture-value"}}
    status, payload = _run_controller_response(response, mutate=True)
    assert status == 1 and payload["status"] == "invalid"
    assert payload["summary"]["projection_error_code"] == "unsafe_response_shape"
    assert "unrecognized_fields" not in payload["summary"]
    assert key not in json.dumps(payload)


def test_controller_rejected_response_names_bad_value() -> None:
    response = {"status": "accepted", "records": {}, "result": {"controller_action": []}}
    status, payload = _run_controller_response(response, mutate=True)
    assert status == 1 and payload["status"] == "invalid"
    assert payload["summary"]["projection_error_code"] == "invalid_response"
    assert payload["warnings"][0]["code"] == "invalid_response"
    assert "unrecognized_fields" not in payload["summary"]
    assert payload["summary"]["recommendation"]


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
        status, _dry_run_payload = _run_controller_response(response)
        assert status == 1
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


@pytest.mark.parametrize("mutate", [False, True])
@pytest.mark.parametrize("error_location", ["error", "result.error", "result.blocking_reason"])
def test_controller_http_error_keeps_safe_identifiers_without_raw_error_text(mutate: bool, error_location: str) -> None:
    for http_status, error_code in (
        (403, "merge_readiness_not_ready"), (409, "merge_readiness_not_ready"),
        (502, "merge_readiness_not_ready"), (408, "merge_readiness_not_ready"),
        (409, "merge_train_controller_lease_held"),
    ):
        response = {
            "trace_id": "launchplane_req_http", "error": {
                "code": error_code, "message": "Bearer private-fixture-value",
            }, "token": "private-fixture-value",
        }
        if error_location != "error":
            response["result"] = {error_location.split(".")[1]: response.pop("error")}
        error = urllib.error.HTTPError(
            "https://launchplane.example.invalid/controller", http_status, "Rejected", Message(),
            io.BytesIO(json.dumps(response).encode()),
        )
        status, payload = _run_controller_response(error, mutate=mutate)
        assert status == 1
        if mutate and (http_status >= 500 or http_status == 408):
            assert payload["status"] == "outcome_unknown"
            assert payload["summary"]["recommendation"]
        assert payload["summary"]["http_status"] == http_status
        assert payload["summary"]["trace_id"] == response["trace_id"]
        assert payload["summary"]["error_code"] == error_code
        assert "private-fixture-value" not in json.dumps(payload)


@pytest.mark.parametrize("mutate", [False, True])
@pytest.mark.parametrize("http_status", [409, 502, 408])
def test_controller_merge_refusal_projects_only_valid_pr_number(mutate: bool, http_status: int) -> None:
    for number in (7, True, -1, 0, "7", "Bearer private-fixture-value"):
        response = {
            "trace_id": "launchplane_req_http", "error": {"code": "github_merge_rejected"},
            "details": {"pull_request_number": number, "message": "Bearer private-fixture-value"},
        }
        error = urllib.error.HTTPError(
            "https://launchplane.example.invalid/controller", http_status, "Rejected", Message(),
            io.BytesIO(json.dumps(response).encode()),
        )
        status, payload = _run_controller_response(error, mutate=mutate)
        assert status == 1
        assert payload["summary"].get("pull_request_number") == (7 if number == 7 else None)
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
        "pull_request_numbers": [42],
    }
    for entries in ([{"pull_request_number": True}], [{"pull_request_number": 42}, {"status": "pending"}]):
        projected = write_action._project_merge_component({"entries": entries}, include_membership=True)
        assert "pull_request_numbers" not in projected
        assert projected["entries_count"] == len(entries)

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
        "pull_request_numbers": [42],
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


def _public_hosts_response(*, mode: str = "dry-run") -> dict[str, Any]:
    hosts = ["example.com", "www.example.com"]
    return {
        "status": "accepted", "trace_id": "launchplane_req_hosts", "records": {},
        "result": {
            "status": "ok", "mode": mode, "product": "example-product",
            "context": "example-site", "instance": "prod",
            "runtime_environment": {"action": "skipped", "scope": "instance",
                                    "context": "example-site", "instance": "prod"},
            "runtime_key_safety": {"required": False, "status": "not_required"},
            "secrets": [], "summary": {"runtime_changed_key_count": 0, "secret_change_count": 0},
            "next_actions": [],
            "public_hosts": {
                "plan_digest": "b" * 64, "before": [], "after": hosts,
                "added": hosts, "updated": [], "removed": [], "unchanged": [],
                "runtime_port": 8069, "https": True, "service_name": "web",
                "certificate_type": "none", "verified": mode == "apply",
                "read_back_hosts": hosts if mode == "apply" else [],
            },
        },
    }


@pytest.mark.parametrize("mode", ["dry-run", "apply"])
def test_product_config_projects_public_hostname_diff_and_readback(mode: str) -> None:
    response = _public_hosts_response(mode=mode)
    result = write_action.summarize_success(
        operation="product-config-apply" if mode == "apply" else "product-config-dry-run",
        request={"product": "example-product", "context": "example-site", "instance": "prod"},
        provider_payload=response,
    )
    assert result["result"]["public_hosts"] == {
        **response["result"]["public_hosts"],
        "resolved_base_url": "https://example.com",
    }


@pytest.mark.parametrize("mode", ["dry-run", "apply"])
def test_product_config_public_base_url_uses_ordered_intent_and_empty_is_distinct(
    mode: str,
) -> None:
    for hosts in (["www.example.com", "example.com"], []):
        response = _public_hosts_response(mode=mode)
        public_hosts = response["result"]["public_hosts"]
        public_hosts.update(
            after=hosts, added=hosts, read_back_hosts=hosts if mode == "apply" else []
        )
        payload = write_action.summarize_success(
            operation="product-config-apply" if mode == "apply" else "product-config-dry-run",
            request={}, provider_payload=response,
        )
        assert payload["result"]["public_hosts"]["resolved_base_url"] == (
            f"https://{hosts[0]}" if hosts else ""
        )
    response["result"].pop("public_hosts")
    payload = write_action.summarize_success(
        operation="product-config-apply" if mode == "apply" else "product-config-dry-run",
        request={}, provider_payload=response,
    )
    assert "public_hosts" not in payload["result"]


def test_product_config_cannot_supply_an_arbitrary_resolved_base_url() -> None:
    response = _public_hosts_response()
    response["result"]["public_hosts"]["resolved_base_url"] = "https://private-origin.example.invalid"
    with pytest.raises(safety.LaunchplaneSafetyError):
        write_action.summarize_success(
            operation="product-config-dry-run", request={}, provider_payload=response,
        )


@pytest.mark.parametrize("change", [
    {"provider": {"token": "private-value"}}, {"after": ["https://example.com"]},
    {"before": ["example.com", "example.com"]}, {"added": "example.com"},
    {"plan_digest": "private-value"}, {"runtime_port": True}, {"runtime_port": 0},
    {"https": False}, {"service_name": "admin"}, {"certificate_type": "letsencrypt"},
    {"verified": "true"}, {"verified": True}, {"read_back_hosts": ["example.com"]},
])
def test_product_config_refuses_unsafe_or_inconsistent_public_host_results(change: dict[str, object]) -> None:
    response = _public_hosts_response()
    response["result"]["public_hosts"].update(change)
    with pytest.raises(safety.LaunchplaneSafetyError):
        write_action.summarize_success(
            operation="product-config-dry-run", request={}, provider_payload=response,
        )


def test_product_config_public_host_verification_must_match_mode() -> None:
    response = _public_hosts_response(mode="apply")
    response["result"]["mode"] = "dry-run"
    with pytest.raises(safety.LaunchplaneSafetyError):
        write_action._project_product_config_apply_result(response["result"])


def test_product_config_apply_cannot_accept_a_dry_run_result() -> None:
    with pytest.raises(safety.LaunchplaneSafetyError):
        write_action.summarize_success(
            operation="product-config-apply", request={}, provider_payload=_public_hosts_response(),
        )


def test_public_dns_names_with_token_like_substrings_are_supported() -> None:
    response = _public_hosts_response(mode="apply")
    hosts = ["helpdesk-portal.example.com", "kiosk-app.example.com"]
    for key in ("after", "added", "read_back_hosts"):
        response["result"]["public_hosts"][key] = hosts
    result = write_action.summarize_success(
        operation="product-config-apply", request={}, provider_payload=response,
    )
    assert result["result"]["public_hosts"]["read_back_hosts"] == hosts


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


def test_integration_allowance_removal_with_credential_prose() -> None:
    response = json.loads(
        (SCRIPT_DIR / "fixtures" / "integration-allowances-removal-response.json").read_text()
    )
    plan = response["result"]
    added = copy.deepcopy(plan["changes"][0])
    added.update(integration="new_source", action="add", before=None)
    added["after"]["integration"] = "new_source"
    plan["changes"].insert(0, added)
    for change in plan["changes"]:
        for side in ("before", "after"):
            if change[side] is not None:
                change[side]["reason"] = "Existing testing credentials for the development store."
                change[side]["evidence"] = "Rotated cookie Abc.Def9 and password=fixture-private-value"
    request = {
        "schema_version": 1,
        **{key: plan[key] for key in ("product", "context", "instance", "reason")},
        "allowances": [
            {key: change["after"][key] for key in ("integration", "kind", "reason", "evidence")}
            for change in plan["changes"] if change["after"] is not None
        ],
    }
    with TemporaryDirectory() as directory:
        payload_file = _write_json(directory, "removal.json", request)
        argv = ["integration-allowances-dry-run", "--payload-file", payload_file]
        status, output, posts, reads = _run_main(argv, post=response)
        assert status == 0, output
        assert output["status"] == "accepted"
        assert output["result"]["applied"] is False
        assert output["summary"]["trace_id"] == response["trace_id"]
        assert output["summary"]["plan_sha256"] == plan["plan_sha256"]
        assert output["result"]["record_sha256_before"] == plan["record_sha256_before"]
        assert [change["action"] for change in output["result"]["changes"]] == [
            change["action"] for change in plan["changes"]
        ]
        removed = output["result"]["changes"][-1]
        assert removed["before"]["integration"] == "legacy_source"
        assert "after" not in removed
        assert output["result"]["reason"] == "[redacted]"
        for change in output["result"]["changes"]:
            for side in ("before", "after"):
                if side in change:
                    assert isinstance(change[side]["reason"], str) and change[side]["reason"]
                    assert isinstance(change[side]["evidence"], str) and change[side]["evidence"]
        assert "fixture-private-value" not in json.dumps(output)
        assert len(posts) == 1 and not reads
        assert posts[0]["body"] == {**request, "mode": "dry-run"}

        read_response = {
            **{key: response[key] for key in ("status", "trace_id", "records")},
            "result": {
                **{key: plan[key] for key in ("status", "product", "context", "instance", "environment_class")},
                "allowances": [change["after"] for change in plan["changes"] if change["after"] is not None],
                "integration_keys": [], "record_sha256": plan["record_sha256_before"],
            },
        }
        read_output = write_action.summarize_integration_allowances_read(
            request={}, provider_payload=read_response
        )
        assert all(isinstance(item["reason"], str) and item["reason"] for item in read_output["result"]["allowances"])
        assert "fixture-private-value" not in json.dumps(read_output)
        evidence_file = _write_json(directory, "review.json", output)
        applied = copy.deepcopy(response)
        applied["result"].update(mode="apply", applied=True, read_back_matches=True)
        applied["result"]["read_back"] = [
            change["after"] for change in plan["changes"] if change["after"] is not None
        ]
        apply_argv = [
            "integration-allowances-apply", "--payload-file", payload_file,
            *_reviewed_apply_argv(plan["plan_sha256"], evidence_file),
        ]
        status, receipt, posts, _reads = _run_main(apply_argv, post=applied)
        assert status == 0 and receipt["result"]["read_back_matches"] is True
        assert posts[0]["body"] == {
            **request, "mode": "apply", "reviewed_plan_sha256": plan["plan_sha256"],
        }
        malformed = copy.deepcopy(applied)
        malformed["result"]["read_back"][0]["reason"] = None
        received = write_action._ServiceResponse(malformed, 202)
        status, unverified, posts, _reads = _run_main(apply_argv, post=received)
        assert unverified["status"] == "accepted_unverified"
        assert unverified["summary"]["http_status"] == received.http_status
        assert unverified["summary"]["trace_id"] == response["trace_id"]
        assert len(posts) == 1
        assert "fixture-private-value" not in json.dumps(unverified)
        wrong_digest = copy.deepcopy(output)
        wrong_digest["result"]["plan_sha256"] = "0" * 64
        _write_json(directory, "review.json", wrong_digest)
        status, _rejected, posts, reads = _run_main(apply_argv, post=applied)
        assert status == 2 and not posts and not reads

        # A valid removal fixture must not turn malformed or secret-bearing
        # responses into reviewable evidence.
        for replacement in (
            {"kind": "unsupported_kind"},
            {"value": "fixture-private-value"},
            {"reason": None},
        ):
            invalid = copy.deepcopy(response)
            invalid["result"]["changes"][-1]["before"].update(replacement)
            status, rejected, _posts, _reads = _run_main(argv, post=invalid)
            assert status == 1 and rejected["status"] == "invalid"
            assert rejected["result"] == {}
            assert "fixture-private-value" not in json.dumps(rejected)


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


@pytest.mark.parametrize("purpose", [None, "staff_testing", "isolation_staging"])
@pytest.mark.parametrize("with_rehearsal", [False, True])
def test_testing_hold_optional_metadata_survives_read_and_plan(
    purpose: str | None, with_rehearsal: bool,
) -> None:
    hold: dict[str, object] = {"reason": "Review the testing lane."}
    if purpose is not None:
        hold["purpose"] = purpose
    if with_rehearsal:
        hold["rehearsal"] = {
            "binding_sha256": "d" * 64,
            "target_updated_at": "2026-10-16T12:00:00Z",
            "target_source_label": "service:testing-separation",
        }
    plan = write_action._project_testing_hold_plan(
        _testing_hold_plan(before=hold, after=hold, read_back=hold)
    )
    read = write_action._project_testing_hold_read({
        "status": "ok", "product": "example-product", "context": "example",
        "instance": "testing", "hold": hold, "record_sha256": "a" * 64,
    })
    assert read["hold"] == hold
    assert all(plan[side] == read["hold"] for side in ("before", "after", "read_back"))


@pytest.mark.parametrize("purpose", ["deployment", "", None, [], True])
def test_testing_hold_unknown_purpose_is_refused(purpose: object) -> None:
    with pytest.raises(write_action.LaunchplaneSafetyError):
        write_action._project_testing_hold({"reason": "Review.", "purpose": purpose})


@pytest.mark.parametrize("change", [
    {"extra": "unsafe"}, {"binding_sha256": "bad"}, {"target_updated_at": {}},
    {"target_source_label": {}},
])
def test_testing_hold_rehearsal_projection_remains_closed(change: dict[str, object]) -> None:
    rehearsal = {
        "binding_sha256": "d" * 64,
        "target_updated_at": "2026-10-16T12:00:00Z",
        "target_source_label": "service:testing-separation", **change,
    }
    with pytest.raises(write_action.LaunchplaneSafetyError):
        write_action._project_testing_hold({"reason": "Review.", "rehearsal": rehearsal})


@pytest.mark.parametrize("label", ["", "legacy target imported"])
@pytest.mark.parametrize("updated_at", ["2026-10-16T12:00:00+00:00", "legacy-target-revision"])
def test_testing_hold_rehearsal_accepts_producer_provenance_strings(label: str, updated_at: str) -> None:
    rehearsal = {"binding_sha256": "d" * 64, "target_updated_at": updated_at, "target_source_label": label}
    hold = {"reason": "Review.", "rehearsal": rehearsal}
    result = write_action._project_testing_hold_plan(_testing_hold_plan(before=hold))
    assert result["before"]["rehearsal"] == rehearsal


def test_testing_hold_rehearsal_provenance_redacts_private_text() -> None:
    result = write_action._project_testing_hold({
        "reason": "Review.", "rehearsal": {
            "binding_sha256": "d" * 64,
            "target_updated_at": "imported from https://private.example.invalid/time",
            "target_source_label": "password=private-sentinel",
        },
    })
    assert "private" not in json.dumps(result)
    assert result["rehearsal"]["binding_sha256"] == "d" * 64


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


@pytest.mark.parametrize("intent", ["provision", "generation", "testing-replacement"])
def test_path_check_forwards_operation_intent_and_refuses_a_service_ignoring_it(intent: str) -> None:
    response = _path_check_response()
    response["check"]["intent"] = intent
    task_id = "a" * 32
    argv = ["path-check", "--product", "example-product", "--path", "testing",
            "--intent", intent, "--source-mode", "ssh", "--operation-id", "initial-testing-fixture",
            "--task-id", task_id]
    status, payload, calls = _run_product_read(argv, response)
    assert status == 0, payload
    assert calls[0]["query"] == {"path": "testing", "intent": intent, "source_mode": "ssh",
                                  "operation_id": "initial-testing-fixture", "task_id": task_id}
    assert payload["result"]["intent"] == intent
    for selector in ("intent", "source_mode", "operation_id", "task_id"):
        assert payload["request"][selector] == calls[0]["query"][selector]
    response["check"].pop("intent")
    status, payload, _calls = _run_product_read(argv, response)
    assert status == 1 and payload["result"] == {}


def test_path_check_operation_budget_allows_bounded_probes_and_honors_explicit_timeout() -> None:
    response = _path_check_response()
    response["check"]["intent"] = "provision"
    argv = ["path-check", "--product", "example-product", "--path", "testing", "--intent", "provision"]
    status, _payload, calls = _run_product_read(argv, response)
    assert status == 0 and calls[0]["timeout"] == write_action.OPERATION_PATH_CHECK_TIMEOUT_SECONDS
    requested = 17.5
    status, _payload, calls = _run_product_read(["--timeout", str(requested), *argv], response)
    assert status == 0 and calls[0]["timeout"] == requested


@pytest.mark.parametrize("extra", [
    ["--operation-id", "initial-testing-fixture"],
    ["--intent", "provision", "--task-id", "not-a-task"],
    ["--intent", "provision", "--operation-id", "../admin"],
])
def test_path_check_refuses_incomplete_or_unsafe_operation_selectors_without_a_call(extra: list[str]) -> None:
    status, _payload, calls = _run_product_read(
        ["path-check", "--product", "example-product", "--path", "testing", *extra],
        _path_check_response(),
    )
    assert status == 2 and not calls


def test_path_check_refuses_unknown_fields_unsafe_values_and_incomplete_evidence() -> None:
    mutations = (
        lambda body: body.update(extra="private detail"),
        lambda body: body["check"].update(extra="private detail"),
        lambda body: body["check"]["steps"][0].update(provider_error="private detail"),
        lambda body: body["check"]["steps"][0].update(secret_name="PRIVATE_SECRET"),
        lambda body: body["check"]["steps"][0].update(description="Bearer abcdefghijklmnop"),
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
        assert payload["summary"]["trace_id"] == response["trace_id"]
        for value in ("private detail", "PRIVATE_SECRET", "abcdefghijklmnop", "private.example", "private-value"):
            assert value not in json.dumps(payload)

    response = _path_check_response()
    response["check"]["steps"][0]["description"] = "https://private.example.invalid/x"
    status, payload, calls = _run_product_read(argv, response)
    assert status == 0 and len(calls) == 1
    assert payload["result"]["steps"][0]["description"] == "[redacted]"
    assert "private.example.invalid" not in json.dumps(payload)


@pytest.mark.parametrize("context", ["task", "helpdesk", "risk"])
def test_path_check_preserves_operation_ids_with_embedded_sk(context: str) -> None:
    response = _path_check_response()
    check = response["check"]
    check.update(state="blocked", blocked_count=1)
    operation_id = f"odoo-target-replacement-{context}-testing-0123456789abcdef"
    check["steps"][1].update(
        state="blocked", fix="wait", code="deploy_in_progress", record_ids=[operation_id]
    )
    status, payload, calls = _run_product_read(
        ["path-check", "--product", check["product"], "--path", check["path"]], response
    )
    assert status == 0 and len(calls) == 1, payload
    assert payload["result"] == check
    assert payload["summary"]["trace_id"] == response["trace_id"]


# A failed testing deploy carries the operation's error code, which Launchplane
# writes as up to four dot-separated snake_case words of at most 96 characters.
@pytest.mark.parametrize("code", [
    "plan_not_ready.source_artifact_missing",
    "deploy_blocked.runtime_environment_unresolved",
    ".".join(["a" + "b" * 23] + ["a" + "b" * 22] * 3),
])
def test_path_check_accepts_a_failed_testing_deploy_with_a_dotted_error_code(code: str) -> None:
    response = _path_check_response()
    check = response["check"]
    check.update(state="blocked", blocked_count=1)
    check["steps"][1].update(
        state="blocked", fix="by_hand", code=code,
        description="The last testing deploy failed.",
        record_ids=["odoo-target-replacement-cm-testing-0123456789abcdef"],
    )
    status, payload, calls = _run_product_read(
        ["path-check", "--product", check["product"], "--path", check["path"]], response
    )
    assert status == 0 and len(calls) == 1, payload
    assert payload["result"] == check


@pytest.mark.parametrize("code", [
    "internal-db.example.invalid:5432",
    "internal-db.example.invalid",
    "plan_not_ready..source",
    "plan_not_ready.",
    "Plan_not_ready.source",
    "a.b.c.d.e",
    ".".join(["a" + "b" * 24] + ["a" + "b" * 22] * 3),
])
def test_path_check_refuses_step_codes_outside_launchplane_code_grammar(code: str) -> None:
    response = _path_check_response()
    check = response["check"]
    check.update(state="blocked", blocked_count=1)
    check["steps"][1].update(state="blocked", fix="by_hand", code=code)
    status, payload, _calls = _run_product_read(
        ["path-check", "--product", check["product"], "--path", check["path"]], response
    )
    assert status == 1 and payload["status"] == "invalid" and payload["result"] == {}
    assert code not in json.dumps(payload)


@pytest.mark.parametrize("prefix", ["sk-", "ghp_", "ghs_", "gho_", "ghu_", "ghr_", "github_pat_"])
@pytest.mark.parametrize("wrapper", ["{}", "operation-{}"])
def test_path_check_rejects_credentials_in_record_ids(prefix: str, wrapper: str) -> None:
    response = _path_check_response()
    credential = wrapper.format(prefix + "A1" * 20)
    response["check"]["steps"][1]["record_ids"] = [credential]
    status, payload, calls = _run_product_read(
        ["path-check", "--product", "example-product", "--path", "testing"], response
    )
    assert status == 1 and len(calls) == 1
    assert payload["status"] == "invalid" and payload["result"] == {}
    assert payload["summary"]["trace_id"] == response["trace_id"]
    assert credential not in json.dumps(payload)


@pytest.mark.parametrize("trace", [None, {}, "sk-" + "A1" * 20,
                                   "https://private.example.invalid/trace", "trace with spaces"])
def test_path_check_projection_failure_omits_unsafe_trace(trace: object) -> None:
    response = _path_check_response()
    response["trace_id"] = trace
    response["check"]["steps"][0]["unexpected"] = "synthetic-private-detail"
    status, payload, calls = _run_product_read(
        ["path-check", "--product", "example-product", "--path", "testing"], response
    )
    assert status == 1 and len(calls) == 1
    assert payload["status"] == "invalid" and payload["result"] == {}
    assert not payload["summary"].get("trace_id")
    assert "synthetic-private-detail" not in json.dumps(payload)
    if isinstance(trace, str):
        assert trace not in json.dumps(payload)


@pytest.mark.parametrize("lead", [r"log\n", r"log\t", "key", "abc123"])
def test_path_check_rejects_sk_tokens_embedded_in_description(lead: str) -> None:
    response = _path_check_response()
    credential = "sk-proj-" + "A1" * 20
    response["check"]["steps"][0]["description"] = lead + credential
    status, payload, _calls = _run_product_read(
        ["path-check", "--product", "example-product", "--path", "testing"], response
    )
    assert status == 1 and payload["result"] == {}
    assert payload["summary"]["trace_id"] == response["trace_id"]
    assert credential not in json.dumps(payload)


def test_shared_response_shape_rejects_sk_token_inside_url_query() -> None:
    credential = "sk-proj-" + "A1" * 20
    with pytest.raises(safety.LaunchplaneSafetyError):
        safety.assert_public_safe_shape({"url": "https://example.invalid/review?ref=abc123" + credential})


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


@pytest.mark.parametrize("mode", ["missing", "stale", "fresh", "disabled", "inapplicable"])
def test_product_environment_read_preserves_canonical_monitoring_evidence(mode: str) -> None:
    response = _product_environment_response()
    monitoring = cast(dict[str, Any], response["environment"])["health_monitoring"]
    check = monitoring["checks"][0]
    effective = mode not in {"disabled", "inapplicable"}
    observed = mode in {"stale", "fresh"}
    freshness = "verified" if mode == "fresh" else mode if effective else "recorded"
    monitoring["monitoring_intent"] = "private" if mode == "inapplicable" else "public"
    monitoring["public_incident_eligible"] = effective
    # A newer profile does not renew an older probe's deadline.
    monitoring["provenance"] = {
        "source_kind": "record", "source_record_id": "profile-1",
        "recorded_at": "2026-10-16T13:00:00Z", "refreshed_at": "",
        "freshness_status": "recorded", "stale_after": "", "detail": "private profile detail",
    }
    check.update({
        "kind": "public_http", "enabled": mode != "disabled", "probe_effective": effective,
        "incident_eligible": effective, "status": "pass" if observed else {
            "missing": "missing", "disabled": "disabled", "inapplicable": "not_expected",
        }.get(mode),
        "trust_state": freshness, "record_id": f"probe-{mode}" if observed else "",
        "observed_at": "2026-10-16T12:00:00Z" if observed else "",
        "provenance": {
            "source_kind": "record", "source_record_id": f"probe-{mode}" if observed else "profile-1",
            "recorded_at": "2026-10-16T12:00:00Z", "refreshed_at": "2026-10-16T12:00:00Z",
            "freshness_status": freshness, "stale_after": "2026-10-16T12:30:00Z" if observed else "",
            "detail": "https://private.example.invalid/health", "unknown": "private-provenance-value",
        },
        "url": "https://private.example.invalid/health", "private_endpoint_key": "private-endpoint",
    })
    status, payload, _calls = _run_product_read(
        ["product-environment-read", "--product", "example-product", "--environment", "testing"], response,
    )
    assert status == 0, payload
    result = payload["result"]["health_monitoring"]
    assert "trust_state" not in result
    assert result["public_incident_eligible"] is monitoring["public_incident_eligible"]
    assert result["provenance"] == {key: value for key, value in monitoring["provenance"].items() if key != "detail"}
    projected = result["checks"][0]
    for field in ("enabled", "probe_effective", "incident_eligible", "status", "trust_state", "record_id", "observed_at"):
        assert projected[field] == check[field]
    assert projected["provenance"] == {
        key: value for key, value in check["provenance"].items() if key not in {"detail", "unknown"}
    }
    for dropped in ("private.example.invalid", "private-endpoint", "private-provenance-value", "private profile detail"):
        assert dropped not in json.dumps(payload)


def test_product_health_legacy_evidence_stays_unknown_and_retired_aggregate_is_ignored() -> None:
    result = write_action._project_product_health({
        "monitoring_intent": "public", "trust_state": "verified", "checks": [{"enabled": True}],
    })
    assert "trust_state" not in result
    assert result["public_incident_eligible"] is None and result["provenance"] is None
    check = result["checks"][0]
    assert check["probe_effective"] is None and check["incident_eligible"] is None
    assert check["provenance"] is None


@pytest.mark.parametrize("extra", [0, 1])
def test_product_health_discloses_incomplete_check_projection(extra: int) -> None:
    checks = [{"name": f"check-{index}", "probe_effective": True}
              for index in range(write_action.PRODUCT_HEALTH_MAX_CHECKS + extra)]
    result = write_action._project_product_health({"checks": checks})
    assert [check["name"] for check in result["checks"]] == [
        check["name"] for check in checks[:write_action.PRODUCT_HEALTH_MAX_CHECKS]
    ]
    assert result["checks_truncated"] is bool(extra)


@pytest.mark.parametrize("field,value", [
    ("probe_effective", "false"), ("incident_eligible", 1), ("provenance", "verified"),
    ("record_id", "invalid record id"),
    ("provenance", {"stale_after": "https://private.example.invalid/deadline"}),
    ("provenance", {"source_record_id": "invalid record id"}),
])
def test_product_environment_read_refuses_invalid_monitoring_evidence(field: str, value: object) -> None:
    response = _product_environment_response()
    monitoring = cast(dict[str, Any], response["environment"])["health_monitoring"]
    monitoring["checks"][0][field] = value
    status, payload, _calls = _run_product_read(
        ["product-environment-read", "--product", "example-product", "--environment", "testing"], response,
    )
    assert status != 0 and not payload["result"]
    assert "private.example.invalid" not in json.dumps(payload)


@pytest.mark.parametrize(
    "capacity",
    [
        {"host_network_capacity_available": False, "host_network_capacity": None},
        {
            "host_network_capacity_available": True,
            "host_network_capacity": {
                "server_id": "server-example",
                "observed_at": "2026-10-21T13:00:00Z",
                "status": "short",
                "capacity": {"total_slots": 31, "free_slots": 1},
                "required_networks": 2,
                "safety_margin": 1,
                "reason": "",
            },
        },
    ],
)
def test_product_environment_read_accepts_and_drops_host_network_capacity(capacity: dict[str, Any]) -> None:
    # Launchplane's ProductEnvironmentResponse always carries the availability flag.
    argv = ["product-environment-read", "--product", "example-product", "--environment", "testing"]
    _status, baseline, _calls = _run_product_read(argv, _product_environment_response())
    status, payload, _calls = _run_product_read(argv, {**_product_environment_response(), **capacity})
    assert status == 0
    assert (payload["result"], payload["summary"]) == (baseline["result"], baseline["summary"])
    assert "network_capacity" not in json.dumps(payload)


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
        "release_on_acceptance": "held",
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


@pytest.mark.parametrize(
    "mode", write_action.PRODUCT_RELEASE_ON_ACCEPTANCE_MODES
)
@pytest.mark.parametrize("production_use", write_action.PRODUCT_PRODUCTION_USES)
def test_product_profile_read_reports_service_metadata_only(mode: str, production_use: str) -> None:
    response = _product_profile_response()
    profile = cast(dict[str, Any], response["profile"])
    profile.update(release_on_acceptance=mode, production_use=production_use)
    status, payload, calls = _run_product_read(
        ["product-profile-read", "--product", "example-product"], response
    )
    assert status == 0
    assert payload["result"]["release_on_acceptance"] == mode
    assert payload["result"]["production_use"] == production_use
    assert payload["summary"] == {
        "launchplane_status": response["status"],
        "trace_id": response["trace_id"],
    }
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["invalid", "", None, False, 1, [], {}])
def test_product_profile_read_refuses_invalid_release_on_acceptance(mode: object) -> None:
    response = _product_profile_response()
    cast(dict[str, Any], response["profile"])["release_on_acceptance"] = mode
    status, payload, _calls = _run_product_read(
        ["product-profile-read", "--product", "example-product"], response
    )
    assert status == 1
    assert payload["status"] == "invalid"
    assert payload["warnings"][0]["code"] == "invalid_response"
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



def _protected_artifacts_response() -> dict[str, Any]:
    return {
        "status": "ok", "trace_id": "launchplane_req_protected",
        "protected_artifacts": {
            "product": "example-product", "context": "example-context",
            "entries": [{
                "reason": "previous-good-deployment", "context": "example-context",
                "instance": "prod", "artifact_id": "ghcr.io/example/app:sha-old",
                "source_record_type": "deployment", "source_record_id": "deployment-old",
                "image_digest": "sha256:" + "a" * 64,
                "image_repository": "private-registry.example.invalid/app",
                "image_references": ["private-registry.example.invalid/app:tag"],
                "private_extra": "private-entry-field",
            }],
            "warnings": [
                "Protected artifact ghcr.io/example/app:sha-old from deployment has no stored artifact manifest; cleanup consumers must still treat the artifact id as protected.",
            ],
        },
    }


def test_protected_artifacts_read_projection_and_query() -> None:
    response = _protected_artifacts_response()
    for context in (None, "example-context"):
        argv = ["protected-artifacts-read", "--product", "example-product"]
        query = {"product": "example-product"}
        if context:
            argv += ["--context", context]
            query["context"] = context
        status, payload, calls = _run_product_read(argv, response)
        assert status == 0
        route = contract.LOCAL_EXTENSION_ROUTES["protected-artifacts-read"]
        assert route["method"] == "GET" and route["mode"] == "read"
        assert calls[0]["path"] == route["path"] and calls[0]["query"] == query
        assert "protected-artifacts-read" in write_action.READ_ONLY_OPERATIONS
        result = payload["result"]
        original = response["protected_artifacts"]["entries"][0]
        assert result["entries"][0] == {
            key: original[key] for key in (
                "reason", "context", "instance", "artifact_id", "source_record_type",
                "source_record_id", "image_digest",
            )
        }
        assert result["warnings"] == response["protected_artifacts"]["warnings"]
        assert result["warning_count"] == 1 and result["entry_count"] == 1
        assert not result["entries_truncated"] and not result["warnings_truncated"]
        assert "private-registry" not in json.dumps(payload)
        assert "private-entry-field" not in json.dumps(payload)


    for artifact in ("ghcr.io/example/app@sha256:" + "a" * 64, "registry.example:5000/app@sha256:" + "a" * 64):
        response["protected_artifacts"]["entries"][0]["artifact_id"] = artifact
        status, payload, _ = _run_product_read(["protected-artifacts-read", "--product", "example-product"], response)
        assert status == 0 and payload["result"]["entries"][0]["artifact_id"] == artifact
    artifact = "ghcr.io/example/app@sha256:" + "a1" * 32
    response["protected_artifacts"]["entries"][0]["artifact_id"] = artifact
    warning = f"Protected artifact {artifact} from deployment has no stored artifact manifest."
    response["protected_artifacts"]["warnings"] = [warning]
    status, payload, _ = _run_product_read(["protected-artifacts-read", "--product", "example-product"], response)
    assert status == 0 and payload["result"]["entries"][0]["artifact_id"] == artifact
    assert payload["result"]["warnings"] == [write_action.public_operator_text(warning)]
    # Active preview feedback can protect image references without an artifact id.
    response["protected_artifacts"]["entries"][0].update(artifact_id="", image_digest="", instance="")
    status, payload, _ = _run_product_read(["protected-artifacts-read", "--product", "example-product"], response)
    assert status == 0 and payload["result"]["entries"][0]["artifact_id"] == ""


def test_protected_artifacts_read_preserves_digest_bearing_record_ids() -> None:
    response = _protected_artifacts_response()
    inventory = response["protected_artifacts"]
    original = inventory["entries"][0]
    inventory["entries"] = [copy.deepcopy(original) for _ in range(4)]
    for instance in ("prod", "testing"):
        artifact = "ghcr.io/example/app@sha256:" + "a1" * 32
        inventory["entries"].append({
            **original,
            "instance": instance,
            "artifact_id": artifact,
            "source_record_type": "release_tuple",
            "source_record_id": f"example-context-{instance}-{artifact}",
        })
        inventory["warnings"].append(f"Protected artifact {artifact} has no stored manifest.")
    status, payload, _ = _run_product_read(
        ["protected-artifacts-read", "--product", "example-product"], response
    )
    assert status == 0 and payload["status"] == response["status"]
    result = payload["result"]
    assert result["entry_count"] == len(inventory["entries"])
    assert result["warning_count"] == len(inventory["warnings"])
    assert not result["entries_truncated"] and not result["warnings_truncated"]
    for projected, source in zip(result["entries"], inventory["entries"], strict=True):
        assert projected == {key: source[key] for key in projected}
        assert projected["source_record_id"] == source["source_record_id"]
    assert result["warnings"] == [
        write_action.public_operator_text(warning) for warning in inventory["warnings"]
    ]
    assert "private-entry-field" not in json.dumps(payload)
    assert "private-registry" not in json.dumps(payload)


def test_protected_artifacts_read_rejects_unsafe_record_ids() -> None:
    argv = ["protected-artifacts-read", "--product", "example-product"]
    for record_id in (
        "user:pass@registry.example/app",
        "example-prod-user:pass@registry.example/app@sha256:" + "a" * 64,
        "example-prod-ghcr.io/example/app@sha256:abc",
        "example-prod-ghcr.io/example/app@sha256:" + "g" * 64,
        "example-prod-ghcr.io/example/app@sha512:" + "a" * 64,
        "example-prod-https://registry.example/app@sha256:" + "a" * 64,
        "Bearer abcdefghijklmnop",
        "",
        None,
    ):
        response = _protected_artifacts_response()
        response["protected_artifacts"]["entries"][0].update(
            source_record_type="release_tuple", source_record_id=record_id
        )
        status, payload, _ = _run_product_read(argv, response)
        assert status == 1 and payload["status"] == "invalid" and not payload["result"]
        assert "user:pass" not in json.dumps(payload)


def test_protected_artifacts_read_bounds_and_sanitizes_warnings() -> None:
    response = _protected_artifacts_response()
    inventory = response["protected_artifacts"]
    inventory["entries"] *= write_action.PROTECTED_ARTIFACTS_MAX_ENTRIES + 1
    inventory["warnings"] = ["Warning at https://private.example.invalid/path password=secret-value"] * (
        write_action.PROTECTED_ARTIFACTS_MAX_WARNINGS + 1
    )
    status, payload, _ = _run_product_read(["protected-artifacts-read", "--product", "example-product"], response)
    assert status == 0
    result = payload["result"]
    assert result["entry_count"] == len(inventory["entries"])
    assert result["warning_count"] == len(inventory["warnings"])
    assert len(result["entries"]) == write_action.PROTECTED_ARTIFACTS_MAX_ENTRIES
    assert len(result["warnings"]) == write_action.PROTECTED_ARTIFACTS_MAX_WARNINGS
    assert result["entries_truncated"] and result["warnings_truncated"]
    assert "private.example" not in json.dumps(payload) and "secret-value" not in json.dumps(payload)


def test_protected_artifacts_read_empty_invalid_and_denied() -> None:
    argv = ["protected-artifacts-read", "--product", "example-product"]
    response = _protected_artifacts_response()
    response["protected_artifacts"].update(entries=[], warnings=[])
    status, payload, _ = _run_product_read(argv, response)
    assert status == 0 and payload["result"]["entry_count"] == 0
    for change in (
        lambda fixture: fixture["protected_artifacts"].update(entries="bad"),
        lambda fixture: fixture["protected_artifacts"].update(warnings="bad"),
        lambda fixture: fixture["protected_artifacts"]["entries"][0].update(artifact_id="Bearer abcdefghijklmnop"),
        lambda fixture: fixture["protected_artifacts"]["entries"][0].update(artifact_id="user:pass@private-host/app"),
        lambda fixture: fixture["protected_artifacts"].update(warnings=["Protected artifact user:pass@private-host/app has no manifest."]),
        lambda fixture: fixture["protected_artifacts"]["entries"][0].update(artifact_id="user:ab/cd@registry.example/app:tag"),
        lambda fixture: fixture["protected_artifacts"].update(warnings=["Protected artifact user:ab/cd@registry.example/app:tag has no manifest."]),
        lambda fixture: fixture["protected_artifacts"]["entries"][0].update(image_digest="user:pass@host"),
        lambda fixture: fixture["protected_artifacts"]["entries"][0].update(instance="user:pass@host"),
        lambda fixture: fixture["protected_artifacts"].update(warnings=["Protected artifact 1://user:pass@host is missing."]),
        lambda fixture: fixture.update(unexpected="private-field"),
    ):
        body = _protected_artifacts_response()
        change(body)
        status, payload, _ = _run_product_read(argv, body)
        assert status == 1 and payload["status"] == "invalid" and not payload["result"]
    status, payload, _ = _run_product_read(argv, urllib.error.HTTPError(
        "https://launchplane.example.invalid/v1/artifacts/protected", 403, "Forbidden", Message(),
        io.BytesIO(json.dumps({"error": {"code": "authorization_denied", "message": "private-error"}}).encode()),
    ))
    assert status == 1 and "private-error" not in json.dumps(payload)
    status, payload, calls = _run_product_read([*argv, "--context", "bad?query=1"], response)
    assert status != 0 and not calls

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
    assert authz["summary"] == "[redacted]"
    assert authz["records"] == [{"record_type": "authz_policy", "record_id": "authz-policy-7"}]
    assert result["events"][1]["status"] == ""
    assert result["events"][2]["records_truncated"] is True
    assert result["omitted_event_count"] == 1
    assert result["dropped_field_count"] == 2
    assert result["dropped_field_paths"] == [
        "events[].event_id",
        "events[].status",
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
        "rejected_builds",
    }
    assert plan["rejected_builds"] == []
    assert result["omitted_request_count"] == 1
    assert result["dropped_field_paths"] == [
        "requests[].last_plan.<unlisted field>",
        "requests[].last_plan.rejected_builds[].<unlisted field>",
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
        "rejected_builds": [{"commit": "abc123" * 6 + "abcd", "error": "Private build failure text"}],
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
    assert kept["rejected_builds"] == [{"commit": "abc123" * 6 + "abcd"}]
    assert "pr_feedback" not in kept
    assert payload["result"]["dropped_field_paths"] == [
        "requests[].last_plan.<unlisted field>",
        "requests[].last_plan.rejected_builds[].error",
    ]
    rendered = json.dumps(payload)
    assert "Private build failure text" not in rendered
    assert "Private feedback text" not in rendered


def test_reconcile_requests_read_shows_why_testing_skipped_a_newer_build() -> None:
    old_commit = "1" * 40
    new_commit = "2" * 40
    plan = {
        "target": "testing",
        "action": "none",
        "reason": "already_deployed",
        "held": False,
        "desired_commit": old_commit,
        "current_commit": old_commit,
        "build_runs_seen": 30,
        "build_runs_total": 412,
        "commit_history_seen": 2,
        "current_commit_seen": True,
        "built_commits_seen": 2,
        "ordered_built_commits": 2,
        "current_build_evidence": "recorded_runtime_identity",
        "rejected_builds": [
            {
                "commit": new_commit,
                "error": "GitHub read failed for /repos/example/private-path: HTTP Error 404",
            },
            {"commit": "https://private-host.example.invalid/path#frag", "error": "Private text"},
            "Private entry text",
        ],
        "preview_recovery_plan": {"provider_target": "private-host"},
        "destroy_retry_stop_reason": "Delivery failed: ssh://admin@private-host.example.invalid/data",
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
    assert kept["rejected_builds"] == [{"commit": new_commit}]
    for name in (
        "current_commit",
        "build_runs_seen",
        "build_runs_total",
        "commit_history_seen",
        "current_commit_seen",
        "built_commits_seen",
        "ordered_built_commits",
        "current_build_evidence",
    ):
        assert kept[name] == plan[name], name
    assert "preview_recovery_plan" not in kept
    assert "destroy_retry_stop_reason" not in kept
    assert payload["result"]["dropped_field_paths"] == [
        "requests[].last_plan.destroy_retry_stop_reason",
        "requests[].last_plan.preview_recovery_plan",
        "requests[].last_plan.rejected_builds[]",
        "requests[].last_plan.rejected_builds[].commit",
        "requests[].last_plan.rejected_builds[].error",
    ]
    rendered = json.dumps(payload)
    assert "private-path" not in rendered
    assert "Private" not in rendered
    assert "private-host" not in rendered

    plan["build_runs_seen"] = "30"
    plan["current_commit_seen"] = "yes"
    plan["current_commit"] = "https://private-host.example.invalid/path#frag"
    status, payload, _calls = _run_product_read(argv, response)
    assert status == 0
    kept = payload["result"]["requests"][0]["last_plan"]
    assert kept["build_runs_seen"] is None
    assert kept["current_commit_seen"] is None
    assert kept["current_commit"] == ""
    assert "requests[].last_plan.current_commit" in payload["result"]["dropped_field_paths"]
    assert "private-host" not in json.dumps(payload)


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
    assert description == "[redacted]"
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
        assert payload["result"]["operation"]["error_description"] == "[redacted]"
        assert "hunter2-example" not in json.dumps(payload)
        assert "operation.error_description" not in payload["result"]["dropped_field_paths"]
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


def _deployed_recovery_operation_response() -> dict[str, Any]:
    # Captured through the maintained GET on 2026-10-12 after launchplane#3483;
    # live identity replaced with examples, unrelated record fields omitted.
    return json.loads(
        (SCRIPT_DIR / "fixtures/odoo-recovery-operation-response.json").read_text()
    )


def test_target_replacement_operation_read_accepts_deployed_recovery() -> None:
    response = _deployed_recovery_operation_response()
    argv = ["target-replacement-operation-read", "--operation-id", response["operation"]["operation_id"]]
    status, payload, calls = _run_product_read(argv, response)
    assert status == 0 and len(calls) == 1
    recovery = payload["result"]["recovery"]
    assert {key: recovery[key] for key in response["recovery"]} == response["recovery"]
    for name in ("attempt_matches", "binding_matches", "effects_acknowledged"):
        assert name not in response["recovery"]
        assert recovery[name] is None
    for name in ("status", "phase", "error_code", "error_description", "error_detail_keys"):
        assert payload["result"]["operation"][name] == response["operation"][name]


@pytest.mark.parametrize("agreement", [None, False, True])
def test_target_replacement_operation_read_preserves_recovery_agreement(agreement: bool | None) -> None:
    response = _deployed_recovery_operation_response()
    nullable = {"attempt_matches", "binding_matches", "effects_acknowledged"}
    response["recovery"].update({name: agreement for name in nullable})
    response["recovery"].update(
        status="eligible", reason="", journal="present", effect_count=2,
        provider_effects="quiescent_observed", provider_inspected=True,
    )
    status, payload, _ = _run_product_read(
        ["target-replacement-operation-read", "--operation-id", response["operation"]["operation_id"]],
        response,
    )
    assert status == 0
    assert payload["result"]["recovery"] == response["recovery"]


@pytest.mark.parametrize("recovery", [None, {}, [], "refused"])
def test_target_replacement_operation_read_handles_unavailable_or_bad_recovery(recovery: object) -> None:
    response = _deployed_recovery_operation_response()
    response["recovery"] = recovery
    status, payload, _ = _run_product_read(
        ["target-replacement-operation-read", "--operation-id", response["operation"]["operation_id"]],
        response,
    )
    if recovery is None:
        assert status == 0 and payload["result"]["recovery"] is None
    else:
        assert status == 1 and not payload["result"]


@pytest.mark.parametrize("field,value", [
    ("status", "safe"), ("reason", "recovery_new_reason"), ("reason", []),
    ("journal", "missing"), ("provider_effects", "absent"),
    ("effect_count", -1), ("effect_count", True), ("effect_count", 1.5),
    ("effect_count", "0"), ("provider_inspected", 0), ("provider_inspected", None),
    ("safe_to_release", True), ("safe_to_release", 0), ("safe_to_release", None),
    ("attempt_matches", 1), ("binding_matches", "true"), ("effects_acknowledged", []),
    ("authorization", {"token": "private-example"}), ("target_id", "private-target"),
    ("reason", "Bearer planted-private-value"),
])
def test_target_replacement_operation_read_refuses_unsafe_recovery(field: str, value: object) -> None:
    response = _deployed_recovery_operation_response()
    response["recovery"][field] = value
    status, payload, _ = _run_product_read(
        ["target-replacement-operation-read", "--operation-id", response["operation"]["operation_id"]],
        response,
    )
    assert status == 1 and not payload["result"]
    assert "private-example" not in json.dumps(payload)
    assert "private-target" not in json.dumps(payload)
    assert "planted-private-value" not in json.dumps(payload)


def test_target_replacement_operation_read_refuses_incomplete_recovery() -> None:
    original = _deployed_recovery_operation_response()
    for field in original["recovery"]:
        response = copy.deepcopy(original)
        del response["recovery"][field]
        status, payload, _ = _run_product_read(
            ["target-replacement-operation-read", "--operation-id", response["operation"]["operation_id"]],
            response,
        )
        assert status == 1 and not payload["result"], field


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
            "base_url": "https://private-origin.example.invalid",
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


CANDIDATE_ARTIFACT_ID = "artifact-example-20261016"
CANDIDATE_SOURCE_GIT_REF = "0123456789abcdef0123456789abcdef01234567"


def test_target_replacement_plan_read_plans_with_a_named_candidate_artifact() -> None:
    response = _target_replacement_plan_response()
    response["result"].update(
        expected_artifact_id=CANDIDATE_ARTIFACT_ID, expected_source_git_ref=CANDIDATE_SOURCE_GIT_REF
    )
    argv = [
        *PLAN_READ_ARGV,
        "--artifact-id",
        CANDIDATE_ARTIFACT_ID,
        "--source-git-ref",
        CANDIDATE_SOURCE_GIT_REF,
    ]
    status, payload, calls = _run_plan_read(argv, response)

    assert status == 0
    assert calls[0]["body"]["replacement"] == {
        "product": "example-product",
        "instance": "testing",
        "artifact_id": CANDIDATE_ARTIFACT_ID,
        "source_git_ref": CANDIDATE_SOURCE_GIT_REF,
    }
    assert not calls[0].get("idempotency_key")
    assert (payload["request"]["artifact_id"], payload["request"]["source_git_ref"]) == (
        CANDIDATE_ARTIFACT_ID,
        CANDIDATE_SOURCE_GIT_REF,
    )
    plan = payload["result"]
    assert (plan["expected_artifact_id"], plan["expected_source_git_ref"]) == (
        CANDIDATE_ARTIFACT_ID,
        CANDIDATE_SOURCE_GIT_REF,
    )


def test_target_replacement_plan_read_refuses_a_partial_or_unsafe_candidate() -> None:
    for extra, code in (
        (["--artifact-id", CANDIDATE_ARTIFACT_ID], "artifact_id_and_source_git_ref_required_together"),
        (["--source-git-ref", CANDIDATE_SOURCE_GIT_REF], "artifact_id_and_source_git_ref_required_together"),
        (["--artifact-id", "x y!", "--source-git-ref", CANDIDATE_SOURCE_GIT_REF], "invalid_artifact_id"),
        (
            ["--artifact-id", CANDIDATE_ARTIFACT_ID, "--source-git-ref", "Bearer abcdefghijklmnop"],
            "invalid_source_git_ref",
        ),
    ):
        status, payload, calls = _run_plan_read([*PLAN_READ_ARGV, *extra], _target_replacement_plan_response())
        assert status == 2, extra
        assert calls == [], extra
        assert payload["warnings"][0]["code"] == code, extra


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


@pytest.mark.parametrize("purpose", ["staff_testing", "isolation_staging"])
@pytest.mark.parametrize("direction", ["set", "clear", "unchanged"])
def test_testing_hold_cli_binds_purpose_and_preserves_apply_key(
    tmp_path: Path, purpose: str, direction: str,
) -> None:
    hold = {"reason": "Review testing.", "purpose": purpose}
    plan = _testing_hold_plan(
        action=direction, reason=hold["reason"],
        before=hold if direction == "clear" else None,
        after=hold if direction == "set" else None,
        changed=direction != "unchanged",
    )
    lane = ["--product", "example-product", "--context", "example", "--instance", "testing"]
    intent = ["--hold" if direction == "set" else "--lift", "--reason", str(hold["reason"])]
    evidence_path = tmp_path / "hold.json"
    calls: list[dict[str, Any]] = []

    def fake_post(**kwargs: Any) -> dict[str, object]:
        calls.append(kwargs)
        applying = kwargs["body"]["mode"] == "apply"
        return _testing_hold_response({
            **plan, "mode": "apply" if applying else "dry-run", "applied": applying,
            "read_back": plan["after"] if applying else None,
            "read_back_matches": True if applying else None,
            "reconcile_requested": applying and direction != "set" and purpose == "staff_testing",
        })

    def run(argv: list[str]) -> tuple[int, dict[str, Any]]:
        output = io.StringIO()
        with redirect_stdout(output):
            status = write_action.main(argv)
        return status, json.loads(output.getvalue())

    with (
        patch.object(write_action, "prepare_operator_settings", return_value=SETTINGS),
        patch.object(write_action, "request_launchplane", side_effect=fake_post),
    ):
        status, evidence = run(["testing-hold-dry-run", *lane, *intent, "--purpose", purpose])
        assert status == 0
        evidence_path.write_text(json.dumps(evidence))
        apply_args = [
            "testing-hold-apply", *lane, *intent, "--purpose", purpose,
            "--reviewed-dry-run", "--expected-plan-digest", str(plan["plan_sha256"]),
            "--dry-run-evidence-file", str(evidence_path), "--idempotency-key", "original-hold-key",
        ]
        status, receipt = run(apply_args)
        assert status == 0
        assert receipt["result"]["reconcile_requested"] == (
            direction != "set" and purpose == "staff_testing"
        )
        assert calls[-1]["idempotency_key"] == "original-hold-key"
        assert all(call["body"].get("purpose", "staff_testing") == purpose for call in calls)
        if purpose == "staff_testing":
            # The server fingerprints raw JSON: default staff retries must keep the legacy payload.
            assert all("purpose" not in call["body"] for call in calls)
        assert receipt["request"]["purpose"] == evidence["request"]["purpose"]
        other_purpose = "staff_testing" if purpose == "isolation_staging" else "isolation_staging"
        mismatched = [other_purpose if value == purpose else value for value in apply_args]
        previous_calls = len(calls)
        status, _ = run(mismatched)
        assert status != 0 and len(calls) == previous_calls

        # Even edited request metadata cannot reinterpret a concrete reviewed hold.
        if direction != "unchanged":
            evidence["request"]["purpose"] = other_purpose
            evidence_path.write_text(json.dumps(evidence))
            status, _ = run(mismatched)
            assert status != 0 and len(calls) == previous_calls


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


def test_trace_ids_refuse_secrets_and_response_prose_redacts_them() -> None:
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
    output = write_action.summarize_success(
        operation="product-config-preflight",
        request={"product": "example-product", "context": "example-testing"},
        provider_payload={"status": "accepted", "trace_id": "launchplane_req_product", "records": {}, "result": {"intent": {"status": "allowed", "reason_code": "policy_allowed", "safe_to_execute": True, "next_action": "Use Bearer secret-token before retrying."}}},
    )
    assert output["result"]["intent"]["next_action"] == "Use [redacted] before retrying."
    assert "secret-token" not in json.dumps(output)
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
    output = write_action.summarize_success(
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
    assert output["result"]["blocking_reason"]["message"] == "[redacted]"
    http_error = urllib.error.HTTPError(
        "https://launchplane.example.invalid/v1/example",
        403,
        "Forbidden",
        hdrs=Message(),
        fp=io.BytesIO(json.dumps({"trace_id": "Bearer secret-token", "error": {"code": "authorization_denied", "message": "secret"}}).encode()),
    )
    projected_error = write_action.summarize_http_error(
        operation="product-config-preflight", request={}, exc=http_error
    )
    assert not projected_error["summary"]["trace_id"]
    assert projected_error["summary"]["http_status"] == http_error.code
    assert projected_error["summary"]["error_code"] == "authorization_denied"
    assert "Bearer secret-token" not in json.dumps(projected_error)


def test_authorization_denial_keeps_denied_status() -> None:

    assert write_action._status_for_http_error(
        403, {"error": {"code": "authorization_denied"}}
    ) == "denied"


@pytest.mark.parametrize("result", [None, [], 42])
@pytest.mark.parametrize("legacy_context", [False, True])
def test_context_main_handles_malformed_result_envelopes(
    result: object, legacy_context: bool
) -> None:
    response: dict[str, object] = {"result": result, "private_extra": "private-response-marker"}
    if legacy_context:
        fixture = json.loads(
            (SCRIPT_DIR.parent / "references" / "context.available.example.json").read_text()
        )
        response["context"] = {"generated_at": fixture["generated_at"], **fixture["sections"]}
    stdout, stderr = io.StringIO(), io.StringIO()
    with (
        patch.object(context_helper, "resolve_settings", return_value={
            "service_url": "https://launchplane.example.invalid", "token": "fake-token",
        }),
        patch.object(context_helper, "request_launchplane", return_value=response),
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        assert context_helper.main(["--repo", "example/repo"]) == 0
    payload = json.loads(stdout.getvalue())
    if legacy_context:
        assert payload["status"] == "available"
        assert payload["sections"]["work_graph"]["items"][0]["safe_to_start"] is False
    else:
        assert payload["status"] == "invalid"
        assert payload["warnings"][0]["code"] == "invalid_response"
        assert payload["sections"] == {}
    assert "private-response-marker" not in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_context_projection_contract_and_secret_shape() -> None:
    provider_context = json.loads((SCRIPT_DIR.parent / "references" / "context.available.example.json").read_text())
    raw_context = {"generated_at": provider_context["generated_at"], **provider_context["sections"]}
    payload = context_helper.normalize_launchplane_payload(
        {"result": {"context": raw_context}}, request=provider_context["request"]
    )
    assert payload["status"] == "available"
    assert payload["generated_at"] == provider_context["generated_at"]
    assert payload["summary"] == provider_context["summary"]
    assert payload["sections"]["work_graph"] == provider_context["sections"]["work_graph"]
    provider_payload = {"result": {"context": {"work_graph": {"status": "available", "items": [{"source_of_truth_url": "https://github.com/example/repo/issues/1", "state": "waiting", "safe_to_start": False, "next_action": "Wait for checks.", "api_key": "secret-value"}]}}}}
    try:
        context_helper.normalize_launchplane_payload(provider_payload, request={"repository": "example/repo"})
    except safety.LaunchplaneSafetyError as exc:
        assert exc.code == "unsafe_response_shape"
    else:
        raise AssertionError("expected context projection to fail closed")


@pytest.mark.parametrize("selector", ["--repo", "--branch"])
@pytest.mark.parametrize("value", [
    *[prefix + "A" * 40 for prefix in ("ghp_", "ghs_", "gho_", "ghu_", "ghr_", "github_pat_")],
    "/private/synthetic-path",
    "C:\\private\\synthetic-path",
    "~/synthetic-path",
    "~fixture/synthetic-path",
    "file:///private/synthetic-path",
    "../../private/synthetic-path",
    "work/../synthetic-path",
    "..\\..\\private\\synthetic-path",
    "  ~fixture/synthetic-path  ",
])
def test_context_entrypoint_omits_unsafe_request_selectors(selector: str, value: str) -> None:
    args = ["--repo", "example/repo"] if selector == "--branch" else []
    result = run_helper("launchplane-context.py", [*args, selector, value])
    assert result["returncode"] == 0
    assert result["payload"]["status"] == "invalid"
    assert result["payload"]["request"] == {}
    assert value not in json.dumps(result["payload"])


@pytest.mark.parametrize("prefix", ["sk-", "ghp_", "ghs_", "gho_", "ghu_", "ghr_", "github_pat_"])
def test_shared_safety_rejects_and_redacts_tokens(prefix: str) -> None:
    synthetic_token = prefix + "A" * 40
    for validator in (safety.public_summary_string, safety.public_identifier, safety.public_trace_id):
        with pytest.raises(safety.LaunchplaneSafetyError):
            validator(synthetic_token)
    with pytest.raises(safety.LaunchplaneSafetyError):
        safety.assert_public_safe_shape({"summary": [synthetic_token]})
    assert safety.public_operator_text("Review " + synthetic_token) == "Review [redacted]"


def test_context_valid_request_selectors_keep_optional_fallback() -> None:
    result = run_helper("launchplane-context.py", ["--repo", "example/repo", "--branch", "work/café-42", "--issue", "42", "--pr", "43"])
    assert result["returncode"] == 0
    assert result["payload"]["status"] == "no_context"
    assert result["payload"]["request"] == {"repository": "example/repo", "branch": "work/café-42", "issue_number": 42, "pr_number": 43}


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
            (lambda data: data.update(domains="testing.example.invalid"), "domains_required"),
            (lambda data: data.update(domains=[""]), "domains_required"),
            (lambda data: data.update(domains=[None]), "domains_required"),
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


@pytest.mark.parametrize("domains", [[], ["testing.example.invalid"]])
def test_compose_target_review_and_readback_cover_private_and_public_routes(domains: list[str]) -> None:
    def response(mode: str) -> dict[str, Any]:
        value = _compose_response(mode, "compose-private-9")
        value["result"]["setup"]["target_record"]["domains"] = domains
        return value

    def inspect(observed_domains: list[str]) -> dict[str, Any]:
        value = _inspect_response()
        value["inspect"]["tracked_target"]["domains"] = observed_domains
        return value

    with TemporaryDirectory(dir=Path.home()) as directory:
        inputs = {**_compose_payload(), "domains": domains}
        payload_path = _write_json(directory, "compose.json", inputs)
        status, evidence, posts, reads = _run_main(
            ["dokploy-target-create-compose-dry-run", "--payload-file", payload_path],
            post=response("dry-run"),
        )
        assert status == 0, evidence
        assert len(posts) == 1 and not reads
        assert posts[0]["body"]["domains"] == domains
        assert evidence["result"]["domain_count"] == len(domains)
        evidence_path = _write_json(directory, "review.json", evidence)
        idempotency_key = "compose-route-review"
        apply_argv = [
            "dokploy-target-create-compose-apply", "--payload-file", payload_path,
            *_reviewed_apply_argv(evidence["result"]["plan_sha256"], evidence_path, idempotency_key),
        ]
        status, receipt, posts, reads = _run_main(
            apply_argv, post=response("apply"), read=inspect(domains),
        )
        assert status == 0, receipt
        assert len(posts) == len(reads) == 1
        assert posts[0]["body"]["domains"] == domains
        assert posts[0]["idempotency_key"] == idempotency_key
        assert receipt["result"]["read_back_matches"] is True
        for private in ("server-private-1", "project-private-1", "compose-private-9", *domains):
            assert private not in json.dumps(receipt)

        unexpected_domains = ["unexpected.example.invalid"]
        status, receipt, posts, reads = _run_main(
            apply_argv, post=response("apply"), read=inspect(unexpected_domains),
        )
        assert status == 1 and receipt["status"] == "accepted_unverified"
        assert receipt["result"]["read_back"]["configuration_matches_review"] is False
        assert unexpected_domains[0] not in json.dumps(receipt)

        # Adding or removing a route after review cannot reuse that evidence.
        changed = {**inputs, "domains": [] if domains else ["testing.example.invalid"]}
        _write_json(directory, "compose.json", changed)
        status, receipt, posts, reads = _run_main(
            apply_argv, post=response("apply"), read=inspect(domains),
        )
        assert status == 2 and not posts and not reads
        assert receipt["warnings"][0]["code"] == "reviewed_dry_run_not_apply_eligible"


STATE_PLAN_ID = "0123456789abcdef0123456789abcdef"
STATE_PLAN_SHA256 = "f" * 64


def _application_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "context": "example",
        "instance": "testing",
        "target_name": "example-testing",
        "server_id": "server-private-1",
        "project_name": "example-project-private",
        "environment_name": "testing",
        "healthcheck_path": "/health",
        "state_volume_path": "/srv/state",
        "reason": "Create the testing lane's application.",
    }
    payload.update(overrides)
    return payload


def _state_volume_response(
    mode: str = "dry-run", target_id: str = "app-private-9", **result: object
) -> dict[str, Any]:
    planned: dict[str, Any] = {
        "operation": "create-application",
        "context": "example",
        "instance": "testing",
        "applied": False,
        "deployment_performed": False,
        "target": {"context": "example", "instance": "testing", "server_id": "server-private-1"},
        "allocation": {"app_name": f"lp-testing-app-{STATE_PLAN_ID}-state"},
        "state_volume_plan_id": STATE_PLAN_ID,
        "plan_sha256": STATE_PLAN_SHA256,
        "state_volume": {"name": f"lp-testing-state-{STATE_PLAN_ID}", "mount_path": "/srv/state"},
    }
    if mode == "apply":
        planned.update(
            applied=True,
            configuration_matches=True,
            configuration_evidence="matches",
            allocation_state="adopted",
            setup={
                "applied": True,
                "target_record": {"context": "example", "instance": "testing", "healthcheck_path": "/health"},
                "target_id_record": {"context": "example", "instance": "testing", "target_id": target_id},
                "provider_target_record": {
                    "provider_id": "dokploy",
                    "target_category": "application",
                    "provider_target_type": "application",
                    "target_id": target_id,
                },
            },
        )
    planned.update(result)
    return {"status": "accepted", "trace_id": "launchplane_req_application", "records": {}, "result": planned}


def _application_inspect_response(**tracked: object) -> dict[str, Any]:
    response = _inspect_response("app-private-9")
    response["inspect"]["target_type"] = "application"
    response["inspect"]["tracked_target"].update(domains=[], **tracked)
    return response


def test_application_target_apply_reuses_the_reviewed_storage_plan() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = _write_json(directory, "application.json", _application_payload())
        status, evidence, posts, _reads = _run_main(
            ["dokploy-target-create-application-dry-run", "--payload-file", payload_path],
            post=_state_volume_response(),
        )
        assert status == 0, evidence
        body = posts[0]["body"]
        assert (body["operation"], body["product"], body["mode"]) == ("create-application", "launchplane", "dry-run")
        assert "state_volume_plan_id" not in body and "confirmation" not in body
        assert evidence["result"]["state_volume"] == {"plan_id": STATE_PLAN_ID, "service_plan_sha256": STATE_PLAN_SHA256}
        for private in ("server-private-1", "example-project-private", "lp-testing-", "/srv/state", "Create the testing"):
            assert private not in json.dumps(evidence), private
        evidence_path = _write_json(directory, "application-dry-run.json", evidence)
        argv = [
            "dokploy-target-create-application-apply", "--payload-file", payload_path,
            *_reviewed_apply_argv(evidence["result"]["plan_sha256"], evidence_path, "original-key"),
        ]
        # A retry sends the same saved plan and key; it never plans again.
        for _attempt in range(2):
            status, receipt, posts, reads = _run_main(
                argv, post=_state_volume_response("apply"), read=_application_inspect_response()
            )
            assert status == 0, receipt
            assert len(posts) == len(reads) == 1
            assert posts[0]["idempotency_key"] == "original-key"
            assert posts[0]["body"]["state_volume_plan_id"] == STATE_PLAN_ID
            assert posts[0]["body"]["expected_plan_sha256"] == STATE_PLAN_SHA256
            assert posts[0]["body"]["confirmation"] == write_action.DOKPLOY_TARGET_SETUP_CONFIRMATION
            assert receipt["result"]["read_back_matches"] is True
            assert "app-private-9" not in json.dumps(receipt)

        # A 202 is a receipt: retained custody and unverified mounts are not creation.
        retained = {"status": "accepted", "trace_id": "launchplane_req_retained", "records": {}, "result": {
            "applied": False, "allocation_state": "retained_uncertain", "original_key": "original-key",
            "plan_id": STATE_PLAN_ID, "deployment_performed": False, "resources_retained": True,
            "reviewed_custody_sha256": "e" * 64,
        }}
        status, receipt, _posts, reads = _run_main(argv, post=retained, read=_application_inspect_response())
        assert (status, receipt["status"], reads) == (1, "accepted_unverified", [])
        assert "state_volume_allocation_retained" in [item["code"] for item in receipt["warnings"]]
        unverified = _state_volume_response("apply", configuration_evidence="unavailable", configuration_matches=False)
        status, receipt, _posts, _reads = _run_main(argv, post=unverified, read=_application_inspect_response())
        assert (status, receipt["status"]) == (1, "accepted_unverified")
        assert "state_volume_configuration_unverified" in [item["code"] for item in receipt["warnings"]]

        # The tracked target must be the created application with the reviewed health path.
        for observed in (
            _application_inspect_response(healthcheck_path="/other"),
            _application_inspect_response(target_id="app-other"),
            {**_application_inspect_response(), "inspect": {**_application_inspect_response()["inspect"], "target_type": "compose"}},
        ):
            status, receipt, _posts, _reads = _run_main(argv, post=_state_volume_response("apply"), read=observed)
            assert (status, receipt["result"]["read_back_matches"]) == (1, False)

        # Edited evidence, a changed payload or a missing review never reach the service.
        _write_json(directory, "application-dry-run.json", {
            **evidence, "result": {**evidence["result"], "state_volume": {
                "plan_id": "a" * 32, "service_plan_sha256": STATE_PLAN_SHA256,
            }},
        })
        status, receipt, posts, _reads = _run_main(argv, post=_state_volume_response("apply"))
        assert (status, posts) == (2, [])
        assert receipt["warnings"][0]["code"] == "reviewed_dry_run_not_apply_eligible"
        _write_json(directory, "application-dry-run.json", evidence)
        _write_json(directory, "application.json", _application_payload(state_volume_path="/srv/other"))
        status, receipt, posts, _reads = _run_main(argv, post=_state_volume_response("apply"))
        assert (status, posts) == (2, [])
        _write_json(directory, "application.json", _application_payload())
        status, receipt, posts, _reads = _run_main(
            [value for value in argv if value != "--reviewed-dry-run"], post=_state_volume_response("apply")
        )
        assert (status, posts) == (2, [])


STATE_CUSTODY_SHA256 = "c" * 64


def _state_volume_readback_response(**result: object) -> dict[str, Any]:
    response = _state_volume_response()
    response["result"].update(
        operation_state="reconcile_required",
        provider_effect_phase=json.dumps(
            {"project": "project-private-1", "environment": "environment-private-1",
             "pending": "/api/application.create"},
            sort_keys=True,
        ),
        configuration_matches=False,
        configuration_evidence="unavailable",
        custody_sha256=STATE_CUSTODY_SHA256,
        allocation_state="reconcile_required",
    )
    response["result"].update(result)
    return response


def _state_volume_refusal(code: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://launchplane.example.invalid", 409, "Conflict", Message(),
        io.BytesIO(json.dumps({"status": "error", "trace_id": "launchplane_req_refused",
                              "error": {"code": code, "message": "private detail"}}).encode()),
    )


def _saved_state_volume_dry_run(directory: str) -> tuple[str, dict[str, Any], str]:
    payload_path = _write_json(directory, "application.json", _application_payload())
    status, evidence, _posts, _reads = _run_main(
        ["dokploy-target-create-application-dry-run", "--payload-file", payload_path],
        post=_state_volume_response(),
    )
    assert status == 0, evidence
    return payload_path, evidence, _write_json(directory, "application-dry-run.json", evidence)


def test_application_storage_readback_uses_saved_plan_and_original_key() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path, evidence, evidence_path = _saved_state_volume_dry_run(directory)
        digest = evidence["result"]["plan_sha256"]
        argv = [
            "dokploy-target-create-application-readback", "--payload-file", payload_path,
            "--idempotency-key", "original-key", "--expected-plan-digest", digest,
            "--dry-run-evidence-file", evidence_path,
        ]
        status, readback, posts, _reads = _run_main(argv, post=_state_volume_readback_response())
        assert status == 0, readback
        body = posts[0]["body"]
        assert posts[0]["idempotency_key"] == "original-key"
        assert (body["mode"], body["state_volume_plan_id"], body["expected_plan_sha256"]) == (
            "readback", STATE_PLAN_ID, STATE_PLAN_SHA256,
        )
        # Readback observes only: no apply confirmation and no retention request.
        assert "confirmation" not in body and "retain_state_volume_allocation" not in body
        result = readback["result"]
        assert result["plan_sha256"] == digest
        assert (result["operation_state"], result["configuration_evidence"], result["custody_sha256"]) == (
            "reconcile_required", "unavailable", STATE_CUSTODY_SHA256,
        )
        assert result["configuration_matches"] is False
        assert result["provider_effect_phase"] == {
            "recorded": True, "created": ["project", "environment"],
            "mount_verified": False, "pending": "application",
        }
        printed = json.dumps(readback)
        for private in ("project-private-1", "environment-private-1", "server-private-1", "lp-testing-", "/srv/state"):
            assert private not in printed, private

        # A response for another plan is never reported as this plan's custody.
        other_plan = _state_volume_readback_response(state_volume_plan_id="a" * 32)
        status, refused, _posts, _reads = _run_main(argv, post=other_plan)
        assert status == 1 and "custody_sha256" not in json.dumps(refused["result"])

        # Missing or edited evidence, a different digest or no key never reach the service.
        edited = copy.deepcopy(evidence)
        edited["result"]["state_volume"]["plan_id"] = "a" * 32
        edited_path = _write_json(directory, "edited-dry-run.json", edited)
        for changed in (
            [edited_path if value == evidence_path else value for value in argv],
            [str(Path(directory) / "missing.json") if value == evidence_path else value for value in argv],
            ["e" * 64 if value == digest else value for value in argv],
            [" " if value == "original-key" else value for value in argv],
        ):
            status, refused, posts, _reads = _run_main(changed, post=_state_volume_readback_response())
            assert (status, posts) == (2, []), refused

        # The service refuses another key by code; the next step comes from that code.
        status, refused, _posts, _reads = _run_main(
            argv, post=_state_volume_refusal("application_state_volume_unqualified")
        )
        assert status == 1
        assert refused["summary"]["error_code"] == "application_state_volume_unqualified"
        assert refused["summary"]["recommendation"] == write_action.APPLICATION_STATE_VOLUME_NEXT_STEPS[
            "application_state_volume_unqualified"
        ]
        assert "private detail" not in json.dumps(refused)


def test_application_storage_retention_requires_original_key_readback_custody() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path, evidence, evidence_path = _saved_state_volume_dry_run(directory)
        digest = evidence["result"]["plan_sha256"]

        def readback_evidence(key: str, name: str) -> str:
            readback_status, readback, *_ = _run_main(
                [
                    "dokploy-target-create-application-readback", "--payload-file", payload_path,
                    "--idempotency-key", key, "--expected-plan-digest", digest,
                    "--dry-run-evidence-file", evidence_path,
                ],
                post=_state_volume_readback_response(),
            )
            assert readback_status == 0, readback
            return _write_json(directory, name, readback)

        readback_path = readback_evidence("original-key", "readback.json")
        argv = [
            "dokploy-target-create-application-retain", "--payload-file", payload_path,
            *_reviewed_apply_argv(digest, evidence_path, "original-key"),
            "--readback-evidence-file", readback_path,
            "--expected-custody-digest", STATE_CUSTODY_SHA256,
        ]
        receipt_result = {
            "applied": False, "allocation_state": "retained_uncertain", "original_key": "original-key",
            "plan_id": STATE_PLAN_ID, "deployment_performed": False, "resources_retained": True,
            "reviewed_custody_sha256": STATE_CUSTODY_SHA256,
            "retained_provider_effect_phase": '{"project": "project-private-1"}',
        }
        retained = {"status": "accepted", "trace_id": "launchplane_req_retained", "records": {}, "result": receipt_result}
        status, receipt, posts, reads = _run_main(argv, post=retained)
        assert status == 0, receipt
        assert reads == [] and posts[0]["idempotency_key"] == "original-key"
        body = posts[0]["body"]
        assert body["retain_state_volume_allocation"] is True
        assert body["expected_state_volume_custody_sha256"] == STATE_CUSTODY_SHA256
        assert (body["mode"], body["state_volume_plan_id"], body["expected_plan_sha256"]) == (
            "apply", STATE_PLAN_ID, STATE_PLAN_SHA256,
        )
        assert body["confirmation"] == write_action.DOKPLOY_TARGET_SETUP_CONFIRMATION
        assert receipt["result"]["allocation_state"] == "retained_uncertain"
        assert receipt["result"]["reviewed_custody_sha256"] == STATE_CUSTODY_SHA256
        assert "project-private-1" not in json.dumps(receipt)

        # A receipt that differs from the reviewed retention in any binding is not it.
        for field, value in (
            ("reviewed_custody_sha256", "d" * 64),
            ("plan_id", "a" * 32),
            ("original_key", "other-key"),
            ("allocation_state", "adopted"),
            ("applied", True),
            ("deployment_performed", True),
            ("resources_retained", False),
        ):
            other = {**retained, "result": {**receipt_result, field: value}}
            status, receipt, _posts, _reads = _run_main(argv, post=other)
            assert (status, receipt["status"]) == (1, "accepted_unverified"), field

        # Without readback custody from the original key, payload and plan, nothing is sent.
        other_key_readback = readback_evidence("other-key", "other-readback.json")
        saved_readback = json.loads(Path(readback_path).read_text(encoding="utf-8"))
        edited_readbacks = []
        for section, field, value in (
            ("result", "custody_sha256", "d" * 64),
            ("result", "plan_sha256", "e" * 64),
            ("result", "state_volume", {"plan_id": "a" * 32, "service_plan_sha256": STATE_PLAN_SHA256}),
            ("request", "payload_digest", "f" * 64),
        ):
            edited = copy.deepcopy(saved_readback)
            edited[section][field] = value
            edited_readbacks.append(_write_json(directory, f"edited-readback-{field}.json", edited))
        for changed in (
            ["d" * 64 if value == STATE_CUSTODY_SHA256 else value for value in argv],
            [other_key_readback if value == readback_path else value for value in argv],
            *([edited_path if value == readback_path else value for value in argv] for edited_path in edited_readbacks),
            [str(Path(directory) / "missing.json") if value == readback_path else value for value in argv],
            ["other-key" if value == "original-key" else value for value in argv],
            [value for value in argv if value != "--reviewed-dry-run"],
        ):
            status, refused, posts, _reads = _run_main(changed, post=retained)
            assert (status, posts) == (2, []), refused

        # A service refusal is answered by its code's next step.
        status, refused, _posts, _reads = _run_main(
            argv, post=_state_volume_refusal("application_state_volume_custody_changed")
        )
        assert status == 1
        assert refused["summary"]["recommendation"] == write_action.APPLICATION_STATE_VOLUME_NEXT_STEPS[
            "application_state_volume_custody_changed"
        ]


def _application_response(mode: str = "dry-run", target_id: str = "app-private-9") -> dict[str, Any]:
    response = _compose_response(mode, target_id)
    result = response["result"]
    result["operation"] = "create-application"
    plan = result["setup"]["plan"]
    plan["application"] = plan.pop("compose")
    result["setup"]["provider_target_record"].update(target_category="application", provider_target_type="application")
    result["setup"]["target_record"]["domains"] = []
    return response


def test_application_target_without_storage_binds_plan_actions_and_payload() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        inputs = {key: value for key, value in _application_payload().items() if key != "state_volume_path"}
        payload_path = _write_json(directory, "application.json", inputs)
        status, evidence, posts, _reads = _run_main(
            ["dokploy-target-create-application-dry-run", "--payload-file", payload_path],
            post=_application_response(),
        )
        assert status == 0, evidence
        assert evidence["result"]["plan_actions"] == {"project": "reuse", "environment": "create", "application": "create"}
        evidence_path = _write_json(directory, "review.json", evidence)
        argv = [
            "dokploy-target-create-application-apply", "--payload-file", payload_path,
            *_reviewed_apply_argv(evidence["result"]["plan_sha256"], evidence_path),
        ]
        status, receipt, posts, _reads = _run_main(
            argv, post=_application_response("apply"), read=_application_inspect_response()
        )
        assert status == 0, receipt
        assert "state_volume_plan_id" not in posts[0]["body"]
        # Edited evidence keeping the reviewed digest is refused before any POST.
        edited_actions = copy.deepcopy(evidence)
        edited_actions["result"]["plan_actions"]["project"] = "create"
        changed_payload_path = _write_json(directory, "application-changed.json", {**inputs, "description": "other"})
        edited_payload = copy.deepcopy(evidence)
        with open(changed_payload_path, encoding="utf-8") as handle:
            edited_payload["request"]["payload_digest"] = write_action.metadata_review_digest(json.load(handle))
        for edited, path in ((edited_actions, payload_path), (edited_payload, changed_payload_path)):
            edited_path = _write_json(directory, "review-edited.json", edited)
            status, refused, posts, _reads = _run_main(
                [
                    "dokploy-target-create-application-apply", "--payload-file", path,
                    *_reviewed_apply_argv(evidence["result"]["plan_sha256"], edited_path),
                ],
                post=_application_response("apply"),
            )
            assert (status, posts) == (2, []), refused
        changed_plan = _application_response("apply")
        changed_plan["result"]["setup"]["plan"]["project"]["action"] = "create"
        status, receipt, _posts, _reads = _run_main(argv, post=changed_plan, read=_application_inspect_response())
        assert (status, receipt["status"]) == (1, "accepted_unverified")
        assert "applied_plan_differs_from_review" in [item["code"] for item in receipt["warnings"]]

        for mutate, code in (
            (lambda data: data.update(instance="prod"), "application_target_requires_testing"),
            (lambda data: data.update(domains=["testing.example.invalid"]), "unsupported_dokploy_target_field"),
            (lambda data: data.update(project_id="project-private-1"), "unsupported_dokploy_target_field"),
            (lambda data: data.update(app_name="chosen"), "unsupported_dokploy_target_field"),
            (lambda data: data.pop("environment_name"), "environment_name_required"),
            (lambda data: data.update(healthcheck_path="health"), "invalid_healthcheck_path"),
            (lambda data: data.update(state_volume_path="srv/state"), "invalid_state_volume_path"),
            (lambda data: data.update(state_volume_path="/srv/../etc"), "invalid_state_volume_path"),
            (lambda data: data.update(expected_current_provider_target="app-1"), "invalid_expected_current_provider_target"),
        ):
            bad = dict(inputs)
            mutate(bad)
            bad_path = _write_json(directory, "application-bad.json", bad)
            status, payload, posts, _reads = _run_main(
                ["dokploy-target-create-application-dry-run", "--payload-file", bad_path],
                post=_application_response(),
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


def _run_read_transport(
    argv: list[str], *, response: object, required_seconds: float = 0,
) -> tuple[int, dict[str, Any], list[dict[str, object]]]:
    calls: list[dict[str, object]] = []

    class Response:
        status = 202

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            if isinstance(response, BaseException):
                raise response
            return json.dumps(response).encode()

    def fake_open(request: urllib.request.Request, *, timeout: float) -> Response:
        calls.append({"method": request.get_method(), "timeout": timeout})
        # Controlled clock: the complete checklist takes about 16 seconds.
        if timeout < required_seconds:
            raise urllib.error.URLError(TimeoutError("fixture-private-message"))
        return Response()

    output = io.StringIO()
    with (
        temporary_attribute(write_action, "prepare_operator_settings", lambda **_kwargs: SETTINGS),
        temporary_attribute(write_action, "safe_urlopen", fake_open),
        redirect_stdout(output),
    ):
        status = write_action.main(argv)
    return status, json.loads(output.getvalue()), calls


def test_complete_promotion_read_budget_and_explicit_override_reach_transport() -> None:
    argv = ["product-promotion-status-read", "--product", "example-product"]
    response = _promotion_status_response()
    status, payload, calls = _run_read_transport(argv, response=response, required_seconds=16)
    assert status == 0, payload
    assert payload["summary"]["trace_id"] == response["trace_id"]
    assert len(calls) == 1
    assert calls[0]["method"] == "GET"
    write = write_action.parse_args(["product-promotion-dry-run", "--product", "example-product",
        "--reason", "fixture", "--evidence-fingerprint", "d" * 64, "--idempotency-key", "fixture"])
    controller = write_action.parse_args(["merge-train-controller-run-once", "--repo", "example/repo"])
    assert write.timeout < 16 <= calls[0]["timeout"] < controller.timeout
    ordinary = write_action.parse_args(["product-profile-read", "--product", "example-product"])
    policy = write_action.parse_args(["merge-train-policy-read", "--repo", "example/repo"])
    assert ordinary.timeout == policy.timeout == write.timeout
    for budget, expected_status in ((7.5, 1), (22.5, 0)):
        status, payload, calls = _run_read_transport(
            ["--timeout", str(budget), *argv], response=response, required_seconds=16,
        )
        assert status == expected_status, payload
        assert calls == [{"method": "GET", "timeout": budget}]
        if status:
            assert payload["summary"]["error_code"] == "client_timeout"
            assert payload["summary"]["timeout_seconds"] == budget


@pytest.mark.parametrize("argv", [
    ["product-promotion-status-read", "--product", "example-product"],
    ["product-profile-read", "--product", "example-product"],
    ["integration-allowances-read", "--product", "example-product", "--context", "example", "--instance", "testing"],
    ["target-replacement-plan-read", "--product", "example-product", "--instance", "testing"],
    ["repository-inventory-read", "--repository-id", "42"],
    ["production-backup-authority-read", "--product", "example-product", "--context", "example", "--instance", "prod", "--promotion-action", "generic_web_prod_promotion"],
    ["private-health-endpoint-read", "--product", "example-product", "--context", "example"],
])
@pytest.mark.parametrize("error", [
    TimeoutError("fixture-private-message"),
    urllib.error.URLError(TimeoutError("fixture-private-message")),
])
def test_read_socket_timeout_is_client_expiry_without_retry(argv: list[str], error: Exception) -> None:
    status, payload, calls = _run_read_transport(["--timeout", "7.5", *argv], response=error)
    assert status == 1, payload
    assert len(calls) == 1
    assert payload["summary"]["error_code"] == "client_timeout"
    assert payload["summary"]["timeout_seconds"] == 7.5
    assert payload["warnings"][0]["code"] == "client_timeout"
    assert "7.5 s" in payload["warnings"][0]["message"]
    assert "--timeout" in payload["summary"]["recommendation"]
    assert not payload["summary"].get("trace_id")
    assert "fixture-private-message" not in json.dumps(payload)
    assert "inspect the Launchplane trace" not in json.dumps(payload)


@pytest.mark.parametrize("response,expected_code", [
    (ConnectionRefusedError("fixture-private-message"), "provider_unavailable"),
    (urllib.error.URLError("fixture-private-message"), "provider_unavailable"),
    (urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", Message(),
        io.BytesIO(b'{"error_code":"authorization_denied","trace_id":"launchplane_req_denied"}')), "denied"),
    ({"status": "ok", "promotion_status": []}, "invalid_response"),
])
def test_read_timeout_does_not_reclassify_other_failures(response: object, expected_code: str) -> None:
    status, payload, calls = _run_read_transport(
        ["product-promotion-status-read", "--product", "example-product"], response=response,
    )
    assert status == 1, payload
    assert len(calls) == 1
    assert payload["warnings"][0]["code"] == expected_code
    assert "fixture-private-message" not in json.dumps(payload)


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
        ('updated password="demo API_KEY="superSecret123"', 'updated [redacted]'),
        ('{"password": "abc, "api_key": "xyz"}', '{[redacted]}'),
        ('verified password == mySecret', 'verified [redacted]'),
        ('set API_KEY := mySecret', 'set [redacted]'),
        ('set password=super,secret;value', 'set [redacted]'),
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


def test_allowance_plan_prose_fallback_keeps_other_reviewed_reasons_strict() -> None:
    for raw in (
        "Remove unused credentials through the approved path.",
        "Rotated 2fa_cookie: Xy.Zw9q", "2fa_token=12-34-56",
        "api_key is Abc.Def9", "cookie Abc.Def9", "hunter2cookie",
    ):
        # Callers that compare projected reasons must retain their old refusal.
        _expect_error(lambda value=raw: write_action.public_operator_text(value), "invalid_response")
        projected = write_action.public_operator_text(raw, redact_denied_markers=True)
        assert projected == "[redacted]"
        assert write_action.public_operator_text(projected) == projected
        write_action.assert_public_safe_shape({"reason": projected})
    for invalid in (None, {}, "", "x" * 501):
        _expect_error(
            lambda value=invalid: write_action.public_operator_text(value, redact_denied_markers=True),
            "invalid_response",
        )


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



def _domain_setup_response(operation: str, mode: str = "dry-run") -> dict[str, Any]:
    response = _compose_response(mode, "compose-private-9")
    response["result"]["operation"] = operation
    response["result"]["instance"] = "prod"
    response["result"]["setup"] = {
        "applied": mode == "apply",
        "target_record": {"target_type": "compose", "domains": ["private.example.invalid"]},
        "target_id_record": {"target_id": "compose-private-9"},
        "domains": ["public.example.invalid"],
        "warnings": ["private.example.invalid provider details"],
    }
    if operation == "reconcile-compose-domain":
        response["result"]["setup"]["runtime_port"] = 8069
    else:
        response["result"]["setup"].update(matched_domain_ids=["private-route-id"], deleted_domain_ids=[], missing_domains=[])
    return response


def _domain_inspect(operation: str, after: bool = False) -> dict[str, Any]:
    response = _inspect_response()
    domains = ["private.example.invalid"]
    if operation == "prune-compose-domain" and not after or operation == "reconcile-compose-domain" and after:
        domains.append("public.example.invalid")
    response["inspect"]["tracked_target"]["domains"] = domains
    response["inspect"]["provider"] = {"id": "compose-private-9", "target_id": "compose-private-9", "target_type": "compose",
                                        "domains": [{"host": domain, "port": 8069, "https": True} for domain in domains]}
    return response


def test_compose_domain_review_apply_and_read_back() -> None:
    for operation in ("reconcile-compose-domain", "prune-compose-domain"):
        with TemporaryDirectory(dir=Path.home()) as directory:
            private: dict[str, Any] = {"context": "example", "instance": "prod", "reason": "Adjust private.example.invalid routes",
                       "domains": ["public.example.invalid"]}
            if operation == "reconcile-compose-domain":
                private["runtime_port"] = 8069
            path = _write_json(directory, "payload.json", private)
            dry = f"dokploy-target-{operation}-dry-run"
            status, evidence, posts, reads = _run_main([dry, "--payload-file", path], post=_domain_setup_response(operation))
            assert status == 0 and reads == [], evidence
            assert posts[0]["body"] == {**private, "product": "launchplane", "operation": operation, "mode": "dry-run"}
            assert posts[0]["path"] == contract.helper_command_path(dry)
            assert "confirmation" not in posts[0]["body"]
            saved = _write_json(directory, "review.json", evidence)
            argv = [f"dokploy-target-{operation}-apply", "--payload-file", path,
                    *_reviewed_apply_argv(evidence["result"]["plan_sha256"], saved)]
            def run(after: dict[str, Any], post: object = None) -> tuple[Any, ...]:
                snapshots = iter([_domain_inspect(operation), after])
                return _run_main(argv, post=post or _domain_setup_response(operation, "apply"), read=lambda _: next(snapshots))
            status, result, posts, reads = run(_domain_inspect(operation, True))
            assert status == 0 and result["result"]["read_back_matches"] is True, result
            assert len(posts) == 1 and len(reads) == 2
            assert posts[0]["body"]["confirmation"] == write_action.DOKPLOY_TARGET_SETUP_CONFIRMATION
            for hidden in ("public.example.invalid", "private.example.invalid", "compose-private-9", "private-route-id"):
                assert hidden not in json.dumps(evidence) and hidden not in json.dumps(result)
            for invalid_id in ({"private": "PRIVATE_SENTINEL"}, ["PRIVATE_SENTINEL"]):
                changed = write_action._ServiceResponse(_domain_inspect(operation, True), 202)
                changed["trace_id"] = "launchplane_req_invalid_target"
                changed["inspect"]["provider"]["id"] = invalid_id
                status, result, posts, reads = run(changed)
                assert status == 1 and result["status"] == "accepted_unverified"
                assert len(posts) == 1 and len(reads) == 2
                assert result["summary"]["read_back_http_status"] == changed.http_status
                assert result["summary"]["read_back_trace_id"] == changed["trace_id"]
                assert result["summary"]["trace_id"] == _domain_setup_response(operation, "apply")["trace_id"]
                status, result, posts, reads = _run_main(argv, read=changed)
                assert status == 1 and result["status"] == "invalid" and not posts
                assert len(reads) == 1 and result["summary"]["trace_id"] == changed["trace_id"]
                assert "PRIVATE_SENTINEL" not in json.dumps(result)
            for invalid_routes in ({"private": "PRIVATE_SENTINEL"}, [{"host": ["PRIVATE_SENTINEL"]}]):
                changed = write_action._ServiceResponse(_domain_inspect(operation, True), 202)
                changed["trace_id"] = "launchplane_req_invalid_routes"
                changed["inspect"]["provider"]["domains"] = invalid_routes
                status, result, posts, reads = run(changed)
                assert status == 1 and result["status"] == "accepted_unverified"
                assert len(posts) == 1 and len(reads) == 2
                assert result["summary"]["read_back_http_status"] == changed.http_status
                assert result["summary"]["read_back_trace_id"] == changed["trace_id"]
                assert result["summary"]["trace_id"] == _domain_setup_response(operation, "apply")["trace_id"]
                assert "PRIVATE_SENTINEL" not in json.dumps(result)
            # A provider route failure cannot be hidden by the tracked record.
            changed = _domain_inspect(operation, True)
            changed["inspect"]["provider"] = _domain_inspect(operation)["inspect"]["provider"]
            status, result, _, _ = run(changed)
            assert status == 1 and result["status"] == "accepted_unverified"
            assert result["result"]["read_back_matches"] is False
            # Unrelated tracked domains must survive both operations.
            changed = _domain_inspect(operation, True)
            changed["inspect"]["tracked_target"]["domains"].remove("private.example.invalid")
            status, result, _, _ = run(changed)
            assert status == 1 and result["status"] == "accepted_unverified"
            status, result, posts, _ = _run_main(argv, post=_domain_setup_response(operation, "apply"), read=_inspect_response("changed-compose"))
            assert status == 1 and result["status"] == "stale" and not posts
            # No request may apply changed input or missing review acknowledgement.
            _write_json(directory, "payload.json", {**private, "domains": ["changed.example.invalid"]})
            status, _, posts, _ = _run_main(argv)
            assert status == 2 and not posts
            _write_json(directory, "payload.json", private)
            status, _, posts, _ = _run_main([arg for arg in argv if arg != "--reviewed-dry-run"])
            assert status == 2 and not posts
            # Ambiguous provider outcomes stop without replaying the mutation.
            status, result, posts, reads = run(_domain_inspect(operation, True), TimeoutError())
            assert status == 1 and result["status"] == "outcome_unknown" and len(posts) == 1 and len(reads) == 1
            # The setup service can report a 400 after a partial provider mutation.
            error = urllib.error.HTTPError("https://private.invalid", 400, "private", Message(), io.BytesIO(json.dumps({
                "trace_id": "launchplane_req_partial", "error": {
                    "code": "invalid_dokploy_target_setup", "message": "private.example.invalid partially changed",
                },
            }).encode()))
            status, result, posts, reads = run(_domain_inspect(operation, True), error)
            assert status == 1 and result["status"] == "outcome_unknown" and len(posts) == 1 and len(reads) == 1
            assert result["summary"]["error_code"] == "invalid_dokploy_target_setup"
            assert result["summary"]["trace_id"] == "launchplane_req_partial"
            assert "private.example.invalid" not in json.dumps(result)
            malformed = urllib.error.HTTPError("https://private.invalid", 400, "private", Message(), io.BytesIO(json.dumps({
                "trace_id": "https://private.example.invalid/token", "error": {"code": "invalid_dokploy_target_setup"},
            }).encode()))
            status, result, posts, _ = run(_domain_inspect(operation, True), malformed)
            assert status == 1 and result["status"] == "outcome_unknown" and len(posts) == 1
            assert "private.example.invalid" not in json.dumps(result)
            changed = _domain_inspect(operation, True)
            changed["inspect"]["provider"]["id"] = "other-compose"
            status, result, _, _ = run(changed)
            assert status == 1 and result["result"]["read_back_matches"] is False
            # A surviving mixed-case host or conflicting duplicate route is not verified.
            changed = _domain_inspect(operation, True)
            changed["inspect"]["provider"]["domains"].append({
                "host": "PUBLIC.EXAMPLE.INVALID", "port": 9999, "https": True,
            })
            status, result, _, _ = run(changed)
            assert status == 1 and result["result"]["read_back_matches"] is False
            # Placement, credential fields and empty/invalid hosts never reach HTTP.
            for field, value in (("target_id", "private-id"), ("token", "private-token"), ("domains", []),
                                 ("domains", ["https://example.invalid"]), ("domains", ["duplicate", "duplicate"])):
                bad = _write_json(directory, "bad.json", {**private, field: value})
                status, _, posts, _ = _run_main([dry, "--payload-file", bad])
                assert status == 2 and not posts, field
            if operation == "reconcile-compose-domain":
                for port in (True, 0, 65536, "8069"):
                    bad = _write_json(directory, "bad.json", {**private, "runtime_port": port})
                    status, _, posts, _ = _run_main([dry, "--payload-file", bad])
                    assert status == 2 and not posts


def _runtime_sync_response(*, mode: str = "dry-run", changed: bool = True) -> dict[str, Any]:
    return {
        "status": "accepted", "trace_id": "launchplane_req_runtime_sync",
        "records": {"target_id": "private-provider-id"},
        "result": {
            "status": "ok", "mode": mode, "context": "example-context", "instance": "testing",
            "tracked_target": {"target_id": "private-provider-id", "target_type": "compose", "target_name": "private-name"},
            "runtime_environment": {
                "desired_key_count": 2, "live_key_count": 2, "unchanged_key_count": 1 if changed else 2,
                "missing_keys": [], "different_keys": ["EXAMPLE_API_TOKEN"] if changed else [],
                "changed_keys": ["EXAMPLE_API_TOKEN"] if changed else [],
            },
            "provider_env_platform_credentials": {"keys": []},
            "apply": {"applied": mode == "apply", "env_updated": mode == "apply" and changed,
                      "verification": {"status": "pass" if mode == "apply" and changed else "skipped"}},
            "deploy": {"requested": False, "triggered": False, "result": None},
        },
    }


def _run_runtime_sync(argv: list[str], responses: list[object]) -> tuple[int, dict[str, Any], Mock]:
    output = io.StringIO()
    with patch.object(write_action, "resolve_settings", return_value={"service_url": "https://example.invalid", "token": "private-token"}), patch.object(
        write_action, "request_launchplane", side_effect=responses
    ) as transport, redirect_stdout(output):
        code = write_action.main(argv)
    return code, json.loads(output.getvalue()), transport


def test_runtime_sync_review_persistence_and_boundaries() -> None:
    flags = ["--product", "example-site", "--context", "example-context", "--instance", "testing"]
    dry_argv = ["live-target-runtime-sync-dry-run", *flags]
    code, evidence, transport = _run_runtime_sync(dry_argv, [_runtime_sync_response()])
    assert code == 0 and evidence["result"]["runtime_environment"]["changed_keys"] == ["EXAMPLE_API_TOKEN"]
    assert transport.call_args.kwargs["body"]["deploy"] is False
    assert transport.call_args.kwargs["idempotency_key"] == ""
    encoded = json.dumps(evidence)
    assert "private-provider-id" not in encoded and "private-name" not in encoded and "private-token" not in encoded
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "review.json"
        path.write_text(encoded)
        apply_argv = ["live-target-runtime-sync-apply", *flags, "--idempotency-key", "example-sync",
                      "--reviewed-dry-run", "--expected-plan-digest", evidence["result"]["plan_sha256"],
                      "--dry-run-evidence-file", str(path)]
        code, result, transport = _run_runtime_sync(apply_argv, [
            _runtime_sync_response(), _runtime_sync_response(mode="apply"), _runtime_sync_response(changed=False),
        ])
        assert code == 0 and result["result"]["read_back_matches"] is True
        assert result["result"]["persistence_status"] == "pass"
        assert result["result"]["read_back_trace_id"] == "launchplane_req_runtime_sync"
        for http_status, service_code, expected_status in (
            (403, "authorization_denied", "denied"),
            (400, "dokploy_target_verification_failed", "accepted_unverified"),
            (502, "service_timeout", "accepted_unverified"),
            (408, "service_timeout", "accepted_unverified"),
        ):
            error = urllib.error.HTTPError("https://example.invalid", http_status, "Refused", Message(), io.BytesIO(json.dumps({
                "error": {"code": service_code}, "trace_id": "launchplane_req_apply_error"
            }).encode()))
            code, refused, refused_transport = _run_runtime_sync(apply_argv, [_runtime_sync_response(), error])
            assert code == 1 and refused["status"] == expected_status
            assert refused["summary"]["error_code"] == service_code
            assert refused["summary"]["trace_id"] == "launchplane_req_apply_error"
            assert refused_transport.call_count == 2
            assert refused["summary"]["http_status"] == error.code
        assert [call.kwargs["body"]["mode"] for call in transport.call_args_list] == ["dry-run", "apply", "dry-run"]
        assert [call.kwargs["idempotency_key"] for call in transport.call_args_list] == ["", "example-sync", ""]
        code, result, transport = _run_runtime_sync(apply_argv, [_runtime_sync_response(changed=False)])
        assert code == 2 and result["warnings"][0]["code"] == "runtime_sync_plan_changed"
        assert transport.call_count == 1
        code, result, transport = _run_runtime_sync(apply_argv, [
            _runtime_sync_response(), _runtime_sync_response(mode="apply"), _runtime_sync_response(),
        ])
        assert code == 1 and result["status"] == "accepted_unverified"
        wrong_lane = list(apply_argv)
        wrong_lane[wrong_lane.index("testing")] = "prod"
        code, _, transport = _run_runtime_sync(wrong_lane, [])
        assert code == 2 and transport.call_count == 0
        unreviewed = [value for value in apply_argv if value != "--reviewed-dry-run"]
        code, _, transport = _run_runtime_sync(unreviewed, [])
        assert code == 2 and transport.call_count == 0
        code, result, transport = _run_runtime_sync(apply_argv, [_runtime_sync_response(), TimeoutError()])
        assert code == 1 and result["status"] == "accepted_unverified" and transport.call_count == 2
        for http_status, service_code in ((401, "authorization_denied"), (502, "service_timeout"), (408, "service_timeout")):
            failed_readback = urllib.error.HTTPError(
                "https://example.invalid", http_status, "PRIVATE_SENTINEL", Message(), io.BytesIO(json.dumps({
                    "error": {"code": service_code, "message": "PRIVATE_SENTINEL"},
                    "trace_id": "launchplane_req_failed_readback",
                }).encode())
            )
            code, result, readback_transport = _run_runtime_sync(apply_argv, [
                _runtime_sync_response(), _runtime_sync_response(mode="apply"), failed_readback,
            ])
            assert code == 1 and result["status"] == "accepted_unverified"
            assert readback_transport.call_count == 3
            assert result["summary"]["trace_id"] == "launchplane_req_runtime_sync"
            assert result["summary"]["read_back_trace_id"] == "launchplane_req_failed_readback"
            assert result["summary"]["read_back_error_code"] == service_code
            assert result["summary"]["read_back_http_status"] == failed_readback.code
            assert "PRIVATE_SENTINEL" not in json.dumps(result)
        code, result, _ = _run_runtime_sync(apply_argv, [
            _runtime_sync_response(), http.client.IncompleteRead(b"private-provider-response", 10),
        ])
        assert code == 1 and result["status"] == "accepted_unverified"
        assert "private-provider-response" not in json.dumps(result)
        malformed_apply = _runtime_sync_response(mode="apply")
        malformed_apply["result"]["apply"]["verification"]["status"] = "partial"
        code, result, _ = _run_runtime_sync(apply_argv, [_runtime_sync_response(), malformed_apply])
        assert code == 1 and result["status"] == "accepted_unverified"
        assert result["summary"]["trace_id"] == "launchplane_req_runtime_sync"
        invalid_trace = _runtime_sync_response(mode="apply")
        invalid_trace["trace_id"] = {}
        code, result, _ = _run_runtime_sync(apply_argv, [_runtime_sync_response(), invalid_trace])
        assert code == 1 and result["status"] == "accepted_unverified"
        invalid_status = _runtime_sync_response(mode="apply")
        invalid_status["result"]["apply"]["verification"]["status"] = []
        code, result, _ = _run_runtime_sync(apply_argv, [_runtime_sync_response(), invalid_status])
        assert code == 1 and result["status"] == "accepted_unverified"


def test_runtime_sync_denial_and_unsafe_output() -> None:
    argv = ["live-target-runtime-sync-dry-run", "--product", "example-site", "--context", "example-context", "--instance", "testing"]
    error = urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", Message(), io.BytesIO(json.dumps({
        "error": {"code": "authorization_denied"}, "trace_id": "launchplane_req_denied"
    }).encode()))
    code, result, transport = _run_runtime_sync(argv, [error])
    assert code == 1 and result["status"] == "denied" and transport.call_count == 1
    assert result["summary"]["trace_id"] == "launchplane_req_denied"
    unreadable_denial = urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", Message(), io.BytesIO())
    unreadable_denial.fp.read = Mock(side_effect=http.client.IncompleteRead(b"private-error", 10))
    code, result, _ = _run_runtime_sync(argv, [unreadable_denial])
    assert code == 1 and result["status"] == "denied" and "private-error" not in json.dumps(result)
    code, result, _ = _run_runtime_sync(argv, [json.JSONDecodeError("Invalid", "", 0)])
    assert code == 1 and result["status"] == "invalid"
    assert result["warnings"][0]["code"] == "invalid_response"
    code, result, _ = _run_runtime_sync(argv, [safety.LaunchplaneSafetyError("unsafe_redirect")])
    assert code == 1 and result["warnings"][0]["code"] == "unsafe_redirect"
    unsafe = _runtime_sync_response()
    unsafe["result"]["runtime_environment"]["changed_keys"] = ["EXAMPLE_API_TOKEN=secret"]
    code, result, _ = _run_runtime_sync(argv, [unsafe])
    assert code == 1 and "secret" not in json.dumps(result)
    unexpected_deploy = _runtime_sync_response()
    unexpected_deploy["result"]["deploy"]["triggered"] = True
    code, _, _ = _run_runtime_sync(argv, [unexpected_deploy])
    assert code == 1



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


def test_odoo_addon_settings_cli_dispatches_projected_route() -> None:
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


def _odoo_website_settings_intent() -> dict[str, object]:
    return {
        "artifact_id": "artifact-example-odoo-1",
        "settings": {
            "operation": "apply",
            "website_id": 1,
            "public_origin": "https://www.example.com",
            "public_indexing_enabled": True,
            "google_analytics_key": "G-EXAMPLE123",
            "cookies_bar": True,
            "block_third_party_domains": True,
        },
    }


def _odoo_website_settings_payload(**overrides: object) -> dict[str, object]:
    return {
        "product": "example-odoo",
        "context": "example",
        "reason": "Open the public site to search engines and analytics.",
        "website_settings": _odoo_website_settings_intent(),
        **overrides,
    }


def _odoo_website_settings_result(**overrides: object) -> dict[str, object]:
    return {
        "product": "example-odoo",
        "context": "example",
        "instance": "prod",
        "record_sha256_before": "b" * 64,
        "mode": "dry-run",
        "plan_sha256": "a" * 64,
        "before": None,
        "after": _odoo_website_settings_intent(),
        "changed": True,
        "applied": False,
        "read_back_matches": False,
        "activation": "Client acceptance of the exact release applies these settings; "
        "preparation changes no Odoo database.",
        **overrides,
    }


def _odoo_website_settings_evidence(**result_overrides: object) -> dict[str, Any]:
    projected: dict[str, Any] = write_action.summarize_success(
        operation="odoo-website-settings-dry-run",
        request={"mode": "dry-run", "payload_source": "private_file"},
        provider_payload={
            "status": "accepted",
            "trace_id": "launchplane_req_website_settings",
            "records": {"product_profile": "example-odoo", "context": "example", "instance": "prod"},
            "result": _odoo_website_settings_result(**result_overrides),
        },
    )
    return projected


def test_odoo_website_settings_projection_fails_closed_on_unknown_fields() -> None:
    projected = _odoo_website_settings_evidence()
    assert projected["summary"]["plan_sha256"] == "a" * 64
    assert projected["result"]["after"] == _odoo_website_settings_intent()
    assert projected["result"]["before"] is None

    withdrawn = _odoo_website_settings_evidence(
        before=_odoo_website_settings_intent(), after=None
    )
    assert withdrawn["result"]["after"] is None
    assert withdrawn["result"]["before"]["settings"]["website_id"] == 1

    unknown_setting = _odoo_website_settings_intent()
    cast(dict[str, object], unknown_setting["settings"])["admin_password"] = "PRIVATE_SENTINEL"
    for result in (
        _odoo_website_settings_result(unexpected="PRIVATE_SENTINEL"),
        _odoo_website_settings_result(after=unknown_setting),
    ):
        with pytest.raises(write_action.LaunchplaneSafetyError):
            write_action.summarize_success(
                operation="odoo-website-settings-dry-run",
                request={},
                provider_payload={"status": "accepted", "records": {}, "result": result},
            )


def test_odoo_website_settings_body_validates_payload_and_binds_digest() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "website-settings.json"
        evidence_path = Path(directory) / "website-settings-dry-run.json"
        payload_path.write_text(json.dumps(_odoo_website_settings_payload()), encoding="utf-8")
        evidence_path.write_text(json.dumps(_odoo_website_settings_evidence()), encoding="utf-8")

        dry_run_body = write_action.odoo_website_settings_body(
            argparse.Namespace(payload_file=str(payload_path), idempotency_key=""),
            mode="dry-run",
        )
        assert dry_run_body["instance"] == "prod"
        assert dry_run_body["mode"] == "dry-run"
        assert "reviewed_plan_sha256" not in dry_run_body

        apply_args = argparse.Namespace(
            payload_file=str(payload_path),
            idempotency_key="example-prod-website-1",
            reviewed_dry_run=True,
            expected_plan_digest="a" * 64,
            dry_run_evidence_file=str(evidence_path),
        )
        apply_body = write_action.odoo_website_settings_body(apply_args, mode="apply")
        assert apply_body["reviewed_plan_sha256"] == "a" * 64

        for overrides, code in (
            ({"reviewed_dry_run": False}, "reviewed_dry_run_required"),
            ({"expected_plan_digest": "b" * 64}, "reviewed_dry_run_not_apply_eligible"),
            ({"expected_plan_digest": "short"}, "invalid_expected_plan_digest"),
            ({"idempotency_key": ""}, "idempotency_key_required"),
        ):
            args = argparse.Namespace(**{**vars(apply_args), **overrides})
            with pytest.raises(ValueError, match=f"^{code}$"):
                write_action.odoo_website_settings_body(args, mode="apply")

        other_context = Path(directory) / "other-context-dry-run.json"
        other_context.write_text(
            json.dumps(_odoo_website_settings_evidence(context="other")), encoding="utf-8"
        )
        args = argparse.Namespace(**{**vars(apply_args), "dry_run_evidence_file": str(other_context)})
        with pytest.raises(ValueError, match="^reviewed_dry_run_not_apply_eligible$"):
            write_action.odoo_website_settings_body(args, mode="apply")

        def settings_with(**changes: object) -> dict[str, object]:
            intent = _odoo_website_settings_intent()
            return {**intent, "settings": {**cast(dict[str, object], intent["settings"]), **changes}}

        for payload, code in (
            (_odoo_website_settings_payload(instance="testing"), "unsupported_instance"),
            (_odoo_website_settings_payload(schema_version=1), "unsupported_payload_field"),
            (
                _odoo_website_settings_payload(
                    website_settings=settings_with(public_origin="http://www.example.com")
                ),
                "https_public_origin_required",
            ),
            (
                _odoo_website_settings_payload(website_settings=settings_with(ga_secret="x")),
                "unsupported_website_settings_field",
            ),
            (
                _odoo_website_settings_payload(
                    website_settings=settings_with(operation="disable_collection")
                ),
                "unsupported_website_settings_operation",
            ),
            (
                _odoo_website_settings_payload(website_settings=settings_with(website_id=True)),
                "website_id_required",
            ),
        ):
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with pytest.raises(ValueError, match=f"^{code}$"):
                write_action.odoo_website_settings_body(
                    argparse.Namespace(payload_file=str(payload_path), idempotency_key=""),
                    mode="dry-run",
                )

        missing = _odoo_website_settings_payload()
        del missing["website_settings"]
        payload_path.write_text(json.dumps(missing), encoding="utf-8")
        with pytest.raises(ValueError, match="^website_settings_required$"):
            write_action.odoo_website_settings_body(
                argparse.Namespace(payload_file=str(payload_path), idempotency_key=""),
                mode="dry-run",
            )


def test_odoo_website_settings_cli_withdraws_pending_intent_with_null() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload_path = Path(directory) / "website-settings.json"
        evidence_path = Path(directory) / "website-settings-dry-run.json"
        payload_path.write_text(
            json.dumps(_odoo_website_settings_payload(website_settings=None)), encoding="utf-8"
        )
        evidence_path.write_text(
            json.dumps(
                _odoo_website_settings_evidence(before=_odoo_website_settings_intent(), after=None)
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
                    ["odoo-website-settings-dry-run", "--payload-file", str(payload_path)]
                )
                == 0
            )
            assert (
                write_action.main(
                    [
                        "odoo-website-settings-apply",
                        "--payload-file",
                        str(payload_path),
                        "--idempotency-key",
                        "example-prod-website-1",
                        "--reviewed-dry-run",
                        "--expected-plan-digest",
                        "a" * 64,
                        "--dry-run-evidence-file",
                        str(evidence_path),
                    ]
                )
                == 0
            )
    expected_path = contract.operation_path("apply_odoo_website_settings")
    assert [call["path"] for call in calls] == [expected_path, expected_path]
    assert [call["body"]["mode"] for call in calls] == ["dry-run", "apply"]
    assert all(call["body"]["website_settings"] is None for call in calls)
    assert all(call["request"]["withdraw"] is True for call in calls)
    assert "Open the public site" not in json.dumps(calls[1]["request"])


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


@pytest.mark.parametrize(
    "descriptor,request_body,counts",
    [
        (
            "managed-authz-policy-set",
            {
                "managed_set_id": "example.proposal",
                "desired_policy": {"schema_version": 2},
                "reason": "Review the prepared grant.",
            },
            {
                "added_rule_count": 1,
                "adopted_rule_count": 0,
                "updated_rule_count": 0,
                "removed_rule_count": 0,
                "unchanged_rule_count": 0,
                "policy_safety_blocker_count": 0,
                "operational_readiness_blocked_rule_count": 0,
            },
        ),
        (
            "managed-merge-train-policy-import",
            {
                "record": _merge_train_policy_import_payload()["record"],
                "reason": "Review the prepared policy.",
            },
            {
                "active_target_count": 1,
                "candidate_target_count": 1,
                "unchanged_policy_key_count": 1,
            },
        ),
    ],
)
@pytest.mark.parametrize(
    "expires_at,expected_expiry",
    [("", ""), ("2026-10-05T18:00:00+02:00", "2026-10-05T16:00:00Z")],
)
@pytest.mark.parametrize("service_url", [
    "https://private.example.invalid",
    "https://private.example.invalid:8443/launchplane/",
    "http://127.0.0.1:8080",
    "http://[::1]:8080/launchplane",
])
def test_privileged_policy_propose_sends_private_envelope_and_only_returns_review_metadata(
    descriptor: str, request_body: dict[str, object], counts: dict[str, int],
    expires_at: str, expected_expiry: str, service_url: str,
) -> None:
    envelope = {
        "descriptor_id": descriptor,
        "source_event_id": "test:proposal:one",
        "request": request_body,
    }
    operation_id = "privileged-operation-" + "a" * 32
    response: dict[str, Any] = {
        "status": "ok",
        "trace_id": "launchplane_req_" + "b" * 32,
        "write_status": "written",
        "summary": {
            "operation_id": operation_id,
            "descriptor_id": descriptor,
            "status": "planned",
            "result_status": "ok",
            "changed": True,
            **counts,
            "expires_at": expires_at,
            "private_selectors": "never-emit-this",
        },
    }
    calls: list[dict[str, object]] = []

    def post(**kwargs: Any) -> dict[str, object]:
        calls.append(kwargs)
        assert kwargs["path"] == contract.helper_command_path("privileged-policy-propose")
        assert kwargs["body"] == envelope
        return response

    with TemporaryDirectory() as directory:
        payload_path = Path(directory) / "proposal.json"
        payload_path.write_text(json.dumps(envelope))
        for state, write_status in (
            ("planned", "written"),
            ("approved", "replayed"),
            ("executing", "replayed"),
            ("expired", "replayed"),
        ):
            response["summary"]["status"] = state
            response["write_status"] = write_status
            output = io.StringIO()
            with (
                patch.object(
                    write_action,
                    "resolve_settings",
                    return_value={
                        "service_url": service_url,
                        "token": "private-token",
                    },
                ),
                patch.object(write_action, "request_launchplane", side_effect=post),
                patch.object(
                    write_action,
                    "request_launchplane_read",
                    side_effect=AssertionError("No import or preflight read"),
                ),
                redirect_stdout(output),
            ):
                assert (
                    write_action.main(
                        [
                            "privileged-policy-propose",
                            "--payload-file",
                            str(payload_path),
                        ]
                    )
                    == 0
                )
            payload = json.loads(output.getvalue())
            assert payload["status"] == "accepted"
            result = payload["result"]
            assert result["expires_at"] == expected_expiry
            assert result["state"] == state
            assert result["operation_id"] == operation_id
            assert result["review_path"].endswith(operation_id)
            review_url = urlsplit(result["review_url"])
            configured_url = urlsplit(service_url)
            assert (review_url.scheme, review_url.netloc) == (
                configured_url.scheme, configured_url.netloc,
            )
            assert review_url.path == (
                configured_url.path.rstrip("/") + urlsplit(result["review_path"]).path
            )
            assert parse_qs(review_url.query) == {"operation_id": [operation_id]}
            assert not review_url.fragment
            assert result["counts"] == counts
            assert result["authorizes_approval"] is False
            assert result["authorizes_execution"] is False
            without_review_url = copy.deepcopy(payload)
            del without_review_url["result"]["review_url"]
            assert configured_url.netloc not in json.dumps(without_review_url)
            for private in (
                "private-token",
                "never-emit-this",
                str(request_body),
            ):
                assert private not in output.getvalue()
    assert calls[0]["body"] == calls[1]["body"]


@pytest.mark.parametrize("expires_at", [None, 0, False, " ", "invalid", "2026-10-05T16:00:00"])
def test_privileged_policy_propose_rejects_invalid_review_deadlines(expires_at: object) -> None:
    response = {
        "status": "ok",
        "write_status": "written",
        "summary": {
            "operation_id": "privileged-operation-" + "a" * 32,
            "descriptor_id": "managed-merge-train-policy-import",
            "status": "planned",
            "result_status": "ok",
            "active_target_count": 1,
            "candidate_target_count": 1,
            "unchanged_policy_key_count": 1,
            "expires_at": expires_at,
        },
    }
    with pytest.raises(safety.LaunchplaneSafetyError, match="invalid_response"):
        write_action.summarize_privileged_policy_proposal(
            request={"descriptor_id": "managed-merge-train-policy-import"},
            provider_payload=response,
            service_url="https://launchplane.example.invalid",
        )


def test_privileged_policy_propose_refuses_unsupported_fields_and_descriptors_before_network() -> (
    None
):
    valid: dict[str, Any] = {
        "descriptor_id": "managed-authz-policy-set",
        "source_event_id": "test:proposal",
        "request": {
            "managed_set_id": "example.proposal",
            "desired_policy": {"schema_version": 2},
            "reason": "Review access.",
        },
    }
    variants = [
        {**valid, "descriptor_id": "ordinary-agent-delivery-activation"},
        {**valid, "descriptor_id": "managed-secret-reencryption"},
        {**valid, "approve": True},
        {**valid, "source_event_id": "invalid source"},
        {
            **valid,
            "request": {**valid["request"], "ordinary_agent_preparation_context": {}},
        },
        {**valid, "request": {**valid["request"], "reason": " "}},
    ]
    with (
        TemporaryDirectory() as directory,
        patch.object(
            write_action,
            "request_launchplane",
            side_effect=AssertionError("No network"),
        ),
    ):
        payload_path = Path(directory) / "proposal.json"
        for envelope in variants:
            payload_path.write_text(json.dumps(envelope))
            with redirect_stdout(io.StringIO()):
                assert (
                    write_action.main(
                        [
                            "privileged-policy-propose",
                            "--payload-file",
                            str(payload_path),
                        ]
                    )
                    == 2
                )


def test_privileged_policy_propose_rejects_malformed_result_without_echoing_private_fields() -> (
    None
):
    for operation_id, descriptor, count in (
        ("wrong-id", "managed-authz-policy-set", 0),
        ("privileged-operation-" + "a" * 32, "managed-secret-reencryption", 0),
        (
            "privileged-operation-" + "a" * 32,
            "managed-authz-policy-set",
            "private-token",
        ),
    ):
        response = {
            "status": "ok",
            "write_status": "written",
            "summary": {
                "operation_id": operation_id,
                "descriptor_id": descriptor,
                "status": "planned",
                "added_rule_count": count,
                "adopted_rule_count": 0,
                "updated_rule_count": 0,
                "removed_rule_count": 0,
                "unchanged_rule_count": 0,
                "policy_safety_blocker_count": 0,
                "operational_readiness_blocked_rule_count": 0,
                "expires_at": "2026-10-05T16:00:00Z",
                "result_status": "ok",
                "changed": True,
            },
        }
        with pytest.raises(safety.LaunchplaneSafetyError):
            write_action.summarize_privileged_policy_proposal(
                request={"descriptor_id": "managed-authz-policy-set"},
                provider_payload=response,
                service_url="https://launchplane.example.invalid",
            )


@pytest.mark.parametrize(
    "failure,expected",
    [
        (TimeoutError(), "outcome_unknown"),
        (urllib.error.URLError("offline"), "outcome_unknown"),
        (http.client.IncompleteRead(b"partial"), "outcome_unknown"),
        (json.JSONDecodeError("cut off", "{", 1), "accepted_unverified"),
        ("malformed", "accepted_unverified"),
        ("partial_body", "outcome_unknown"),
        ("non_object_body", "accepted_unverified"),
        (403, "denied"),
        (409, "conflict"),
        (404, "unsupported"),
        (500, "outcome_unknown"),
        (502, "outcome_unknown"),
        (503, "outcome_unknown"),
        (504, "outcome_unknown"),
        *(({"field": field, "value": value}, "accepted_unverified")
          for field in ("status", "write_status", "result_status")
          for value in ({"private": "never-emit"}, ["never-emit"])),
    ],
)
def test_privileged_policy_propose_failure_reports_safe_reconciliation(
    failure: object, expected: str
) -> None:
    envelope = {
        "descriptor_id": "managed-authz-policy-set",
        "source_event_id": "test:proposal",
        "request": {
            "managed_set_id": "example.proposal",
            "desired_policy": {"schema_version": 2},
            "reason": "Review access.",
        },
    }
    received_status = None
    if isinstance(failure, int):
        error_code = (
            "authorization_denied" if failure == 403 else "privileged_operation_plan_conflict"
        )
        exception = urllib.error.HTTPError(
            "https://private.example.invalid",
            failure,
            "private",
            Message(),
            io.BytesIO(
                json.dumps(
                    {"trace_id": "launchplane_req_" + "c" * 32, "error": {"code": error_code}}
                ).encode()
            ),
        )
        post = Mock(side_effect=exception)
    elif isinstance(failure, BaseException):
        post = Mock(side_effect=failure)
    elif isinstance(failure, dict):
        response = write_action._ServiceResponse({
            "status": "ok", "trace_id": "launchplane_req_" + "c" * 32, "write_status": "written",
            "summary": {"operation_id": "privileged-operation-" + "a" * 32,
                "descriptor_id": "managed-authz-policy-set", "status": "planned",
                "result_status": "ok", "changed": True, "expires_at": "",
                **{field: 0 for field in ("added_rule_count", "adopted_rule_count", "updated_rule_count",
                    "removed_rule_count", "unchanged_rule_count", "policy_safety_blocker_count",
                    "operational_readiness_blocked_rule_count")}},
        }, 202)
        target = response if failure["field"] == "write_status" else response["summary"]
        target[failure["field"]] = failure["value"]
        post = Mock(return_value=response)
    elif failure in ("partial_body", "non_object_body"):
        response = Mock(status=202)
        received_status = response.status
        response.read = (Mock(side_effect=http.client.IncompleteRead(b"never-emit"))
                         if failure == "partial_body" else Mock(return_value=b"null"))
        try:
            write_action._read_service_response(response)
        except (write_action.ServiceResponseError, http.client.HTTPException) as error:
            post = Mock(side_effect=error)
        else:
            raise AssertionError("fixture must fail while reading the response")
    else:
        post = Mock(
            return_value={
                "status": "ok",
                "trace_id": "launchplane_req_" + "c" * 32,
                "summary": {"private": "never-emit"},
            }
        )
    with TemporaryDirectory() as directory:
        payload_path = Path(directory) / "proposal.json"
        payload_path.write_text(json.dumps(envelope))
        output = io.StringIO()
        with (
            patch.object(
                write_action,
                "resolve_settings",
                return_value={
                    "service_url": "https://private.example.invalid",
                    "token": "private-token",
                },
            ),
            patch.object(write_action, "request_launchplane", post),
            redirect_stdout(output),
        ):
            assert (
                write_action.main(
                    ["privileged-policy-propose", "--payload-file", str(payload_path)]
                )
                == 1
            )
        result = json.loads(output.getvalue())
        assert result["status"] == expected
        post.assert_called_once()
        if received_status is not None:
            assert result["summary"]["http_status"] == received_status
        if isinstance(failure, dict):
            assert result["summary"]["http_status"] == post.return_value.http_status
            assert result["summary"]["trace_id"] == post.return_value["trace_id"]
        if expected in {"outcome_unknown", "accepted_unverified"}:
            assert result["summary"]["recommendation"]
        if failure in (403, 409, "malformed"):
            assert result["summary"]["trace_id"] == "launchplane_req_" + "c" * 32
        assert "private-token" not in output.getvalue()
        assert "private.example.invalid" not in output.getvalue()
        assert "never-emit" not in output.getvalue()


def test_privileged_policy_proposal_saved_before_500_replays_the_same_plan() -> None:
    envelope = {"descriptor_id": "managed-authz-policy-set", "source_event_id": "test:saved-proposal",
                "request": {"managed_set_id": "example.proposal", "desired_policy": {"schema_version": 2},
                            "reason": "Review access."}}
    saved: dict[str, dict[str, object]] = {}
    operation_id = "privileged-operation-" + "a" * 32

    def post(**kwargs: Any) -> dict[str, object]:
        body = kwargs["body"]
        source = body["source_event_id"]
        if source not in saved:
            saved[source] = body
            raise urllib.error.HTTPError("https://private.example.invalid", 500, "after save", Message(),
                                         io.BytesIO(b'{"error":{"code":"internal_error"}}'))
        assert body == saved[source]
        return {"status": "ok", "write_status": "replayed", "trace_id": "launchplane_req_" + "b" * 32,
                "summary": {"operation_id": operation_id, "descriptor_id": body["descriptor_id"],
                            "status": "planned", "result_status": "ok", "changed": False,
                            "expires_at": "", "added_rule_count": 1, "adopted_rule_count": 0,
                            "updated_rule_count": 0, "removed_rule_count": 0, "unchanged_rule_count": 0,
                            "policy_safety_blocker_count": 0, "operational_readiness_blocked_rule_count": 0}}

    with TemporaryDirectory() as directory:
        path = Path(directory) / "proposal.json"
        path.write_text(json.dumps(envelope))
        outputs = []
        with (patch.object(write_action, "resolve_settings", return_value={
                "service_url": "https://private.example.invalid", "token": "private-token"}),
              patch.object(write_action, "request_launchplane", side_effect=post) as transport):
            for expected in (1, 0):
                output = io.StringIO()
                with redirect_stdout(output):
                    assert write_action.main(["privileged-policy-propose", "--payload-file", str(path)]) == expected
                outputs.append(json.loads(output.getvalue()))
                assert "private-token" not in output.getvalue()
                without_review_url = copy.deepcopy(outputs[-1])
                without_review_url.get("result", {}).pop("review_url", None)
                assert "private.example.invalid" not in json.dumps(without_review_url)
        assert transport.call_count == 2
        assert len(saved) == 1
        assert outputs[0]["status"] == "outcome_unknown"
        assert outputs[0]["summary"]["recommendation"]
        assert outputs[1]["result"]["operation_id"] == operation_id
        assert outputs[1]["result"]["authorizes_approval"] is False
        assert outputs[1]["result"]["authorizes_execution"] is False


def test_merge_policy_enrollment_projects_only_requested_repository() -> None:
    response: dict[str, Any] = {
        "status": "ok", "trace_id": "launchplane_req_policy_read",
        "record": {
            "status": "active", "record_id": "fixture-policy",
            "updated_at": "2026-10-05T12:00:00Z", "policy_sha256": "a" * 64,
            "policy": {"policies": [
                {"repository": "example/Repo", "base_branch": "release",
                 "enqueue_label": "ship  it", "github_token": {"env_var": "PRIVATE_TOKEN"}},
                {"repository": "private/task-runner", "base_branch": "risk-fixes", "enqueue_label": "other"},
            ]},
        },
    }
    request = {"repository": "EXAMPLE/repo"}
    projected = write_action.summarize_merge_train_policy_read(request=request, provider_payload=response)
    assert projected["result"]["enabled"] is True
    assert projected["result"]["targets"] == [{"baseBranch": "release", "readyLabel": "ship  it"}]
    assert "PRIVATE_TOKEN" not in json.dumps(projected)
    assert "private/task-runner" not in json.dumps(projected)
    request = {"repository": "example/absent"}
    result = write_action.summarize_merge_train_policy_read(request=request, provider_payload=response)["result"]
    assert result["status"] == "not_enrolled" and result["enabled"] is False
    response["record"]["policy"]["policies"][0]["enqueue_label"] = "unsafe\x1b[31m"
    with pytest.raises(safety.LaunchplaneSafetyError):
        write_action.summarize_merge_train_policy_read(request={"repository": "example/repo"}, provider_payload=response)
    response["record"]["policy"]["policies"][0]["enqueue_label"] = "ship  it"
    response["record"]["policy"]["policies"].append(response["record"]["policy"]["policies"][0])
    with pytest.raises(safety.LaunchplaneSafetyError):
        write_action.summarize_merge_train_policy_read(request=request, provider_payload=response)


@pytest.mark.parametrize("record", [{}, {"status": "superseded", "policy": {"policies": []}},
                                    {"status": "active", "policy": {"policies": []}}])
def test_merge_policy_invalid_record_cannot_prove_absence(record: dict[str, object]) -> None:
    with pytest.raises(safety.LaunchplaneSafetyError):
        write_action.summarize_merge_train_policy_read(
            request={"repository": "example/repo"},
            provider_payload={"status": "ok", "trace_id": "launchplane_req_fixture", "record": record},
        )


def test_merge_policy_read_uses_only_get_and_preserves_denial(tmp_path: Path) -> None:
    config = tmp_path / "operator.json"
    config.write_text(json.dumps({"service_url": "https://service.example.invalid", "operator_token_env": "FIXTURE_TOKEN"}))
    calls = []
    def denied(**kwargs: Any) -> dict[str, object]:
        calls.append(kwargs)
        raise urllib.error.HTTPError("https://service.example.invalid", 403, "Denied", Message(), io.BytesIO(b'{}'))
    output = io.StringIO()
    with patch.dict(os.environ, {"FIXTURE_TOKEN": "fixture-value"}, clear=True), patch.object(
        write_action, "request_launchplane_read", denied,
    ), patch.object(write_action, "request_launchplane", side_effect=AssertionError("write attempted")), redirect_stdout(output):
        status = write_action.main(["--config", str(config), "--env-config", str(tmp_path / "missing.env"),
                                    "merge-train-policy-read", "--repo", "example/repo"])
    assert status != 0
    assert calls[0]["path"] == contract.helper_command_path("merge-train-policy-read")
    assert json.loads(output.getvalue())["status"] == "denied"


def test_merge_policy_unresolved_origin_uses_safe_code(tmp_path: Path) -> None:
    output = io.StringIO()
    with patch.object(write_action, "prepare_operator_settings", side_effect=AssertionError("config read attempted")), redirect_stdout(output):
        status = write_action.main(["merge-train-policy-read", "--repo-root", str(tmp_path)])
    assert status == 2
    assert json.loads(output.getvalue())["warnings"][0]["code"] == "repository_unresolved"

def _monitoring_arguments() -> list[str]:
    return ["--product", "example-product", "--context", "example", "--instance", "testing",
            "--check-name", "public-ingress", "--check-kind", "public_http",
            "--monitoring-intent", "public", "--enabled", "--require-runtime-identity",
            "--reason", "Require strict monitoring on the approved check."]


def _monitoring_response(mode: str = "dry-run", **overrides: object) -> dict[str, object]:
    plan = {
        "status": "ok", "mode": mode, "product": "example-product", "context": "example",
        "instance": "testing", "check_name": "public-ingress", "current_check_kind": "public_http",
        "requested_check_kind": "public_http", "current_monitoring_intent": "prelaunch",
        "requested_monitoring_intent": "public", "operation": "update", "current_enabled": False,
        "requested_enabled": True, "current_require_runtime_identity": False,
        "requested_require_runtime_identity": True, "private_endpoint_key": "",
        "resolved_url": "https://testing.example.invalid/private-health-path",
        "changed": True, "applied": mode == "apply", "reason": "Private audit reason.",
        "source_label": "service:product-health-monitoring",
        "profile_updated_at_before": "2026-10-07T12:00:00Z", "profile_updated_at_after": "",
        "profile_sha256_before": write_action._canonical_sha256({"profile": "before"}),
        "plan_sha256": write_action._canonical_sha256({"plan": "approved-monitoring"}),
        **overrides,
    }
    return {"status": "accepted", "trace_id": "launchplane_req_monitoring", "records": {
        "product_profile": "example-product", "context": "example", "instance": "testing",
        "health_check": "public-ingress",
    }, "result": plan}


def _monitoring_profile(*, applied: bool = False, **check_changes: object) -> dict[str, Any]:
    profile = _product_profile_response()
    lane = profile["profile"]["lanes"][0]
    lane["health_url"] = "https://testing.example.invalid/private-health-path"
    lane["health_monitoring"] = {
        "monitoring_intent": "public" if applied else "prelaunch", "checks": [{
            "name": "public-ingress", "kind": "public_http", "url": "", "private_endpoint_key": "",
            "enabled": applied, "require_runtime_identity": applied, **check_changes,
        }],
    }
    return profile


def _monitoring_review(tmp_path: Path, arguments: list[str] | None = None, **plan: object) -> tuple[list[str], dict[str, Any]]:
    arguments = _monitoring_arguments() if arguments is None else arguments
    status, evidence, _posts, _reads = _run_main(
        ["health-monitoring-dry-run", *arguments], post=_monitoring_response(**plan),
    )
    assert status == 0, evidence
    path = _write_json(str(tmp_path), "monitoring-review.json", evidence)
    return ["health-monitoring-apply", *arguments,
            *_reviewed_apply_argv(evidence["result"]["plan_sha256"], path)], evidence


def test_health_monitoring_dry_run_and_verified_apply_preserve_endpoint(tmp_path: Path) -> None:
    status, evidence, posts, reads = _run_main(
        ["health-monitoring-dry-run", *_monitoring_arguments()], post=_monitoring_response(),
    )
    assert status == 0 and not reads
    assert posts[0]["path"] == contract.helper_command_path("health-monitoring-dry-run")
    assert posts[0]["body"]["mode"] == "dry-run"
    assert not ({"url", "domain", "profile", "provider", "reviewed_plan_sha256"} & posts[0]["body"].keys())
    argv, saved = _monitoring_review(tmp_path)
    profiles = iter([_monitoring_profile(), _monitoring_profile(applied=True)])
    status, receipt, posts, reads = _run_main(argv, post=_monitoring_response("apply"), read=lambda _: next(profiles))
    assert status == 0, receipt
    assert posts[0]["body"]["reviewed_plan_sha256"] == saved["result"]["plan_sha256"]
    assert posts[0]["idempotency_key"] == "apply-1"
    assert len(reads) == 2
    assert all(call["path"] == contract.helper_command_path("product-profile-read").format(product="example-product") for call in reads)
    assert receipt["result"]["read_back_matches"] is True
    assert receipt["result"]["read_back"]["endpoint_preserved"] is True
    for output in (evidence, receipt):
        printed = json.dumps(output)
        assert "testing.example.invalid" not in printed and "private-health-path" not in printed
        assert "Private audit reason" not in printed and "Require strict monitoring" not in printed

    # An idempotent replay with the already-applied profile verifies as well.
    status, replay, posts, _reads = _run_main(argv, post=_monitoring_response("apply"), read=_monitoring_profile(applied=True))
    assert status == 0 and posts[0]["idempotency_key"] == "apply-1", replay


@pytest.mark.parametrize("field,value", [
    ("--instance", "prod"), ("--product", "other-product"), ("--check-name", "other-check"),
    ("--monitoring-intent", "prelaunch"), ("--reason", "Different reason."),
    ("--dry-run-evidence-file", ""), ("--expected-plan-digest", "f" * 64), ("--idempotency-key", ""),
])
def test_health_monitoring_apply_refuses_unbound_arguments(tmp_path: Path, field: str, value: str) -> None:
    argv, _evidence = _monitoring_review(tmp_path)
    argv[argv.index(field) + 1] = value
    status, _receipt, posts, reads = _run_main(argv)
    assert status == 2 and not posts and not reads


def test_health_monitoring_apply_requires_acknowledgement_and_saved_evidence(tmp_path: Path) -> None:
    argv, evidence = _monitoring_review(tmp_path)
    argv.remove("--reviewed-dry-run")
    status, receipt, posts, reads = _run_main(argv)
    assert status == 2 and not posts and not reads
    assert receipt["warnings"][0]["code"] == "reviewed_dry_run_required"
    for mutate in (
        lambda data: data.update(operation="testing-hold-dry-run"),
        lambda data: data.update(status="accepted_unverified"),
        lambda data: data["result"].update(mode="apply"),
        lambda data: data["result"].update(applied=True),
        lambda data: data["request"].update(payload_digest="f" * 64),
    ):
        argv, _ = _monitoring_review(tmp_path)
        forged = copy.deepcopy(evidence)
        mutate(forged)
        _write_json(str(tmp_path), "monitoring-review.json", forged)
        status, _receipt, posts, reads = _run_main(argv)
        assert status == 2 and not posts and not reads


def test_health_monitoring_changed_endpoint_stops_before_apply(tmp_path: Path) -> None:
    argv, _ = _monitoring_review(tmp_path)
    status, receipt, posts, reads = _run_main(argv, read=_monitoring_profile(url="https://other.example.invalid/health"))
    assert (status, receipt["status"]) == (1, "stale")
    assert not posts and len(reads) == 1
    assert "other.example.invalid" not in json.dumps(receipt)


@pytest.mark.parametrize("failure", ["endpoint", "policy", "lane", "digest", "unavailable"])
def test_health_monitoring_read_back_or_apply_mismatch_is_unverified(tmp_path: Path, failure: str) -> None:
    argv, _ = _monitoring_review(tmp_path)
    observed: object = _monitoring_profile(applied=True)
    post = _monitoring_response("apply")
    if failure == "endpoint":
        observed = _monitoring_profile(applied=True, url="https://testing.example.invalid/changed")
    elif failure == "policy":
        observed = _monitoring_profile(applied=True, require_runtime_identity=False)
    elif failure == "lane":
        cast(dict[str, Any], observed)["profile"]["lanes"][0]["instance"] = "other"
    elif failure == "digest":
        post = _monitoring_response("apply", plan_sha256="f" * 64)
    else:
        observed = TimeoutError("private endpoint details")
    profiles = iter([_monitoring_profile(), observed])
    status, receipt, posts, _reads = _run_main(argv, post=post, read=lambda _: next(profiles))
    assert (status, receipt["status"]) == (1, "accepted_unverified") and len(posts) == 1
    assert "private endpoint details" not in json.dumps(receipt)


def test_health_monitoring_private_check_uses_registered_key_and_redacts_it(tmp_path: Path) -> None:
    arguments = _monitoring_arguments()
    arguments[arguments.index("--check-kind") + 1] = "private_http"
    arguments[arguments.index("--monitoring-intent") + 1] = "private"
    arguments += ["--private-endpoint-key", "example-private-runtime"]
    plan = {"current_check_kind": "private_http", "requested_check_kind": "private_http",
            "requested_monitoring_intent": "private", "private_endpoint_key": "example-private-runtime", "resolved_url": ""}
    argv, evidence = _monitoring_review(tmp_path, arguments, **plan)
    before = _monitoring_profile(kind="private_http", private_endpoint_key="example-private-runtime")
    after = _monitoring_profile(applied=True, kind="private_http", private_endpoint_key="example-private-runtime")
    after["profile"]["lanes"][0]["health_monitoring"]["monitoring_intent"] = "private"
    profiles = iter([before, after])
    status, receipt, posts, _reads = _run_main(argv, post=_monitoring_response("apply", **plan), read=lambda _: next(profiles))
    assert status == 0, receipt
    assert posts[0]["body"]["private_endpoint_key"] == "example-private-runtime"
    assert "example-private-runtime" not in json.dumps(evidence) + json.dumps(receipt)
    changed = list(argv)
    changed[changed.index("--private-endpoint-key") + 1] = "another-key"
    status, _receipt, posts, reads = _run_main(changed)
    assert status == 2 and not posts and not reads


def test_health_monitoring_disabled_and_unchanged_plans_are_supported(tmp_path: Path) -> None:
    arguments = _monitoring_arguments()
    arguments[arguments.index("--enabled")] = "--no-enabled"
    arguments[arguments.index("--require-runtime-identity")] = "--no-require-runtime-identity"
    arguments[arguments.index("--monitoring-intent") + 1] = "prelaunch"
    plan = {"operation": "unchanged", "changed": False, "requested_enabled": False,
            "requested_require_runtime_identity": False, "requested_monitoring_intent": "prelaunch"}
    argv, _ = _monitoring_review(tmp_path, arguments, **plan)
    status, receipt, posts, _reads = _run_main(argv, post=_monitoring_response("apply", **plan), read=_monitoring_profile())
    assert status == 0, receipt
    assert posts[0]["body"]["enabled"] is False


@pytest.mark.parametrize("change", ["public_key", "disabled_identity", "private_missing_key"])
def test_health_monitoring_invalid_request_sends_nothing(change: str) -> None:
    arguments = _monitoring_arguments()
    if change == "public_key":
        arguments += ["--private-endpoint-key", "private-key"]
    elif change == "disabled_identity":
        arguments[arguments.index("--enabled")] = "--no-enabled"
    else:
        arguments[arguments.index("--check-kind") + 1] = "private_http"
    status, _receipt, posts, reads = _run_main(["health-monitoring-dry-run", *arguments])
    assert status == 2 and not posts and not reads


@pytest.mark.parametrize("changes", [
    {"instance": "other"}, {"requested_enabled": 1}, {"requested_require_runtime_identity": False},
    {"mode": "apply"}, {"resolved_url": None}, {"provider": "unexpected-private-data"},
])
def test_health_monitoring_malformed_plan_is_rejected(changes: dict[str, object]) -> None:
    status, receipt, _posts, _reads = _run_main(
        ["health-monitoring-dry-run", *_monitoring_arguments()], post=_monitoring_response(**changes),
    )
    assert status != 0
    assert receipt["status"] != "accepted"
    assert "unexpected-private-data" not in json.dumps(receipt)


OVERRIDE_REASON = "Private dependency override reason."
REQUIRED_DERIVED = {"mode": "required", "override": None}


def _dependency_arguments(mode: str, source: str, *dependencies: str, reason: str = "") -> list[str]:
    arguments = [*_monitoring_arguments(), "--dependency-mode", mode, "--dependency-source", source]
    for dependency in dependencies:
        arguments += ["--dependency", dependency]
    return arguments + (["--dependency-override-reason", reason] if reason else [])


def _with_dependency_policy(profile: dict[str, Any], policy: object) -> dict[str, Any]:
    profile["profile"]["lanes"][0]["health_monitoring"]["dependency_health"] = policy
    return profile


@pytest.mark.parametrize("arguments,policy", [
    (["required", "derived"], REQUIRED_DERIVED),
    (["informational", "derived"], {"mode": "informational", "override": None}),
    (["required", "override", "email", "database"],
     {"mode": "required", "override": {"dependencies": ["database", "email"], "reason": OVERRIDE_REASON}}),
    (["informational", "override"],
     {"mode": "informational", "override": {"dependencies": [], "reason": OVERRIDE_REASON}}),
])
def test_health_monitoring_dependency_policy_is_reviewed_applied_and_read_back(
    tmp_path: Path, arguments: list[str], policy: dict[str, Any],
) -> None:
    mode, source, *dependencies = arguments
    reason = OVERRIDE_REASON if source == "override" else ""
    argv_args = _dependency_arguments(mode, source, *dependencies, reason=reason)
    current = {"mode": "required" if mode == "informational" else "informational", "override": None}
    plan = {"current_dependency_health": current, "requested_dependency_health": policy}
    argv, evidence = _monitoring_review(tmp_path, argv_args, **plan)
    public = {"mode": mode, "override": None if policy["override"] is None else {
        "dependencies": policy["override"]["dependencies"]}}
    assert evidence["request"]["dependency_health"] == public
    assert evidence["result"]["requested_dependency_health"] == public
    profiles = iter([_with_dependency_policy(_monitoring_profile(), current),
                     _with_dependency_policy(_monitoring_profile(applied=True), policy)])
    status, receipt, posts, _reads = _run_main(argv, post=_monitoring_response("apply", **plan), read=lambda _: next(profiles))
    assert status == 0, receipt
    assert posts[0]["body"]["dependency_health"] == policy
    assert receipt["result"]["read_back"]["dependency_health"] == public
    assert OVERRIDE_REASON not in json.dumps(evidence) + json.dumps(receipt)

    # A same-key replay against the already-applied lane verifies the same policy.
    status, replay, posts, _reads = _run_main(
        argv, post=_monitoring_response("apply", **plan),
        read=_with_dependency_policy(_monitoring_profile(applied=True), policy),
    )
    assert status == 0 and posts[0]["idempotency_key"] == "apply-1", replay


def test_health_monitoring_omitted_dependency_policy_is_legacy_and_preserved(tmp_path: Path) -> None:
    required = {"mode": "required", "override": {"dependencies": ["database"], "reason": OVERRIDE_REASON}}
    argv, evidence = _monitoring_review(tmp_path)
    assert "dependency_health" not in evidence["request"]
    assert "requested_dependency_health" not in evidence["result"]

    def apply(*after_policy: object) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
        profiles = iter([_with_dependency_policy(_monitoring_profile(), required),
                         _with_dependency_policy(_monitoring_profile(applied=True), *after_policy)])
        status, receipt, posts, _reads = _run_main(argv, post=_monitoring_response("apply"), read=lambda _: next(profiles))
        return status, receipt, posts

    status, receipt, posts = apply(required)
    assert status == 0, receipt
    assert "dependency_health" not in posts[0]["body"]
    for changed in (REQUIRED_DERIVED, {**required, "override": {**required["override"], "reason": "Other."}}):
        status, receipt, _posts = apply(changed)
        assert (status, receipt["status"]) == (1, "accepted_unverified")


def test_health_monitoring_legacy_request_accepts_only_preserving_dependency_review() -> None:
    preserved = {"current_dependency_health": REQUIRED_DERIVED, "requested_dependency_health": REQUIRED_DERIVED}
    status, receipt, _posts, _reads = _run_main(
        ["health-monitoring-dry-run", *_monitoring_arguments()], post=_monitoring_response(**preserved),
    )
    assert status == 0 and receipt["result"]["requested_dependency_health"] == REQUIRED_DERIVED
    for plan in (
        {**preserved, "requested_dependency_health": {"mode": "informational", "override": None}},
        {"current_dependency_health": REQUIRED_DERIVED},
    ):
        status, receipt, _posts, _reads = _run_main(
            ["health-monitoring-dry-run", *_monitoring_arguments()], post=_monitoring_response(**plan),
        )
        assert status != 0 and receipt["status"] != "accepted"


@pytest.mark.parametrize("plan", [
    {},
    {"current_dependency_health": None, "requested_dependency_health": {"mode": "informational", "override": None}},
    {"current_dependency_health": None, "requested_dependency_health": {**REQUIRED_DERIVED, "extra": "private"}},
    {"current_dependency_health": None, "requested_dependency_health": {
        "mode": "required", "override": {"dependencies": ["database", "database"], "reason": OVERRIDE_REASON}}},
])
def test_health_monitoring_dependency_request_rejects_unmatched_review(plan: dict[str, object]) -> None:
    status, receipt, _posts, _reads = _run_main(
        ["health-monitoring-dry-run", *_dependency_arguments("required", "derived")],
        post=_monitoring_response(**plan),
    )
    assert status != 0 and receipt["status"] != "accepted"
    assert "private" not in json.dumps(receipt.get("result"))


@pytest.mark.parametrize("arguments", [
    [*_monitoring_arguments(), "--dependency-source", "derived"],
    [*_monitoring_arguments(), "--dependency-mode", "required"],
    _dependency_arguments("required", "derived", "database"),
    _dependency_arguments("required", "derived", reason=OVERRIDE_REASON),
    _dependency_arguments("required", "override", "database"),
    _dependency_arguments("required", "override", "email", "email", reason=OVERRIDE_REASON),
    _dependency_arguments("required", "override", reason="x" * 1001),
])
def test_health_monitoring_invalid_dependency_request_sends_nothing(arguments: list[str]) -> None:
    status, _receipt, posts, reads = _run_main(["health-monitoring-dry-run", *arguments])
    assert status == 2 and not posts and not reads


def test_health_monitoring_apply_binds_reviewed_dependency_policy(tmp_path: Path) -> None:
    arguments = _dependency_arguments("required", "override", "database", reason=OVERRIDE_REASON)
    plan = {"current_dependency_health": None, "requested_dependency_health": {
        "mode": "required", "override": {"dependencies": ["database"], "reason": OVERRIDE_REASON}}}
    argv, _ = _monitoring_review(tmp_path, arguments, **plan)
    for field, value in (("--dependency-mode", "informational"), ("--dependency", "email"),
                         ("--dependency-override-reason", "Other reason.")):
        changed = list(argv)
        changed[changed.index(field) + 1] = value
        status, _receipt, posts, reads = _run_main(changed)
        assert status == 2 and not posts and not reads, field
    # Dropping the policy from an apply reviewed with one is also unbound.
    legacy = argv[:argv.index("--dependency-mode")] + argv[argv.index("--idempotency-key"):]
    status, _receipt, posts, reads = _run_main(legacy)
    assert status == 2 and not posts and not reads


def test_health_monitoring_dependency_refusal_is_reported_without_details() -> None:
    refusal = urllib.error.HTTPError(
        "https://launchplane.example.invalid/v1/product-profiles/health-monitoring/apply", 400, "Bad Request",
        hdrs=Message(), fp=io.BytesIO(json.dumps({"trace_id": "launchplane_req_refused", "error": {
            "code": "invalid_product_profile", "message": "Updated product profile failed validation."}}).encode()),
    )
    arguments = _dependency_arguments("required", "derived")
    arguments[arguments.index("--enabled")] = "--no-enabled"
    arguments[arguments.index("--require-runtime-identity")] = "--no-require-runtime-identity"
    status, receipt, posts, _reads = _run_main(["health-monitoring-dry-run", *arguments], post=refusal)
    assert status != 0 and len(posts) == 1
    assert receipt["status"] not in {"accepted", "ok"}



EVENT_REFERENCE = "event-deploy-" + "e" * 64


def _event_recovery_payload(**changes: object) -> dict[str, object]:
    return {"product": "example-product", "instance": "testing",
            "recovery_reference": EVENT_REFERENCE, "reason": "Inspect held testing deploy.", **changes}


def _event_recovery_response(**changes: object) -> dict[str, object]:
    return {"schema_version": 1, "status": "ok", "mode": "dry-run",
            "product": "example-product", "context": "example", "instance": "testing",
            "reservation_state": "reconcile_required", "reservation_attempt": 2,
            "reservation_created_at": "2026-10-04T00:00:00Z",
            "reservation_updated_at": "2026-10-04T00:01:00Z",
            "observed_at": "2026-10-09T00:00:00Z", "reconciliation_key_sha256": "b" * 64,
            "provider_target_key_sha256": "c" * 64, "provider_outcome": "absent",
            "retry_safe": True, "proposed_action": "retry_original_operation",
            "recovery_digest": "d" * 64, **changes}


def _event_recovery_review(directory: str, **changes: object) -> tuple[str, str, dict[str, Any]]:
    payload = _write_json(directory, "reference.json", _event_recovery_payload())
    status, receipt, posts, _ = _run_main(
        ["generic-web-deploy-recovery-dry-run", "--payload-file", payload],
        post=_event_recovery_response(**changes),
    )
    assert status == 0, receipt
    assert posts[0]["body"] == _event_recovery_payload()
    assert posts[0]["idempotency_key"] == ""
    evidence = _write_json(directory, "review.json", receipt)
    return payload, evidence, receipt


def _event_recovery_apply_argv(payload: str, evidence: str, digest: str) -> list[str]:
    return ["generic-web-deploy-recovery-apply", "--payload-file", payload,
            "--reviewed-dry-run", "--expected-recovery-digest", digest,
            "--dry-run-evidence-file", evidence]


def test_event_recovery_reference_read_uses_service_route() -> None:
    source = {"product": "example-product", "context": "example", "instance": "testing",
              "recovery_reference": EVENT_REFERENCE, "reservation_state": "reconcile_required",
              "reservation_attempt": 2}
    status, receipt, posts, reads = _run_main(
        ["generic-web-deploy-recovery-reference-read", "--product", "example-product"], read=source,
    )
    assert status == 0 and not posts and len(reads) == 1
    assert reads[0]["path"] == contract.helper_command_path(
        "generic-web-deploy-recovery-reference-read").format(product="example-product")
    assert receipt["result"] == source


@pytest.mark.parametrize("changes", [
    {"instance": "prod"}, {"product": "other"}, {"recovery_reference": "not-a-reference"},
    {"reservation_state": "completed"}, {"reservation_attempt": True},
    {"original_deploy": "private-request"}, {"idempotency_key": "private-key"},
])
def test_event_recovery_reference_read_refuses_unsafe_or_wrong_lane(changes: dict[str, object]) -> None:
    source = {"product": "example-product", "context": "example", "instance": "testing",
              "recovery_reference": EVENT_REFERENCE, "reservation_state": "reconcile_required",
              "reservation_attempt": 2, **changes}
    status, receipt, posts, _ = _run_main(
        ["generic-web-deploy-recovery-reference-read", "--product", "example-product"], read=source,
    )
    assert status == 1 and not posts and receipt["status"] != "ok"
    assert "private-request" not in json.dumps(receipt) and "private-key" not in json.dumps(receipt)


@pytest.mark.parametrize("action,outcome,retry", [
    ("retry_original_operation", "absent", True), ("adopt_observed", "present", True),
    ("adopt_observed", "present", False),
    ("close_out_observed", "absent", False),
])
def test_event_recovery_apply_binds_review_without_caller_key(action: str, outcome: str, retry: bool) -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload, evidence, receipt = _event_recovery_review(
            directory, proposed_action=action, provider_outcome=outcome, retry_safe=retry)
        result = {"schema_version": 1, "status": "accepted", "mode": "apply",
                  "trace_id": "launchplane_req_event_apply", "product": "example-product",
                  "context": "example", "instance": "testing", "reservation_state": "completed",
                  "reservation_attempt": 2, "recovery_action": action, "provider_outcome": outcome,
                  "retry_safe": retry, "recovery_digest": receipt["result"]["recovery_digest"]}
        status, applied, posts, _ = _run_main(
            _event_recovery_apply_argv(payload, evidence, result["recovery_digest"]), post=result)
    assert status == 0, applied
    assert len(posts) == 1 and posts[0]["idempotency_key"] == ""
    assert posts[0]["body"]["recovery_reference"] == EVENT_REFERENCE
    assert posts[0]["body"]["expected_recovery_digest"] == result["recovery_digest"]
    assert "Inspect held testing deploy." not in json.dumps(receipt)


@pytest.mark.parametrize("changes", [
    {"recovery_reference": "event-deploy-" + "f" * 64}, {"reason": "Changed review reason."},
    {"product": "other"}, {"instance": "prod"}, {"original_deploy": {}},
])
def test_event_recovery_changed_review_sends_no_request(changes: dict[str, object]) -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload, evidence, receipt = _event_recovery_review(directory)
        Path(payload).write_text(json.dumps(_event_recovery_payload(**changes)))
        status, _, posts, reads = _run_main(
            _event_recovery_apply_argv(payload, evidence, receipt["result"]["recovery_digest"]))
    assert status == 2 and not posts and not reads


@pytest.mark.parametrize("changes", [
    {"proposed_action": "hold_unknown", "provider_outcome": "unknown", "retry_safe": False},
    {"provider_outcome": "not_inspected"}, {"retry_safe": False},
    {"proposed_action": "wait_for_active_lease"}, {"proposed_action": "replay_completed"},
    {"proposed_action": "close_out_observed", "provider_outcome": "unknown", "retry_safe": False},
    {"proposed_action": "adopt_observed", "provider_outcome": "absent"},
    {"proposed_action": "retry_original_operation", "provider_outcome": "present"},
])
def test_event_recovery_held_or_uninspected_review_sends_no_apply(changes: dict[str, object]) -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload, evidence, receipt = _event_recovery_review(directory, **changes)
        status, failed, posts, reads = _run_main(
            _event_recovery_apply_argv(payload, evidence, receipt["result"]["recovery_digest"]))
    assert status == 2 and not posts and not reads
    assert failed["warnings"][0]["code"] == "reviewed_dry_run_not_apply_eligible"


def test_event_recovery_rejects_guessed_key_and_unbound_review() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload, evidence, receipt = _event_recovery_review(directory)
        argv = _event_recovery_apply_argv(payload, evidence, receipt["result"]["recovery_digest"])
        status, _, posts, reads = _run_main(argv + ["--idempotency-key", "guessed-key"])
        assert status == 2 and not posts and not reads
        receipt["request"].pop("recovery_request_sha256")
        Path(evidence).write_text(json.dumps(receipt))
        status, _, posts, reads = _run_main(argv)
        assert status == 2 and not posts and not reads


def test_reconcile_target_selection_reaches_testing_after_output_bound() -> None:
    requests = [{"target_key": f"example-product:preview:{i}", "state": "done"}
                for i in range(write_action.RECONCILE_MAX_REQUESTS + 1)]
    testing = {"target_key": "example-product:testing", "state": "failed",
               "last_plan": {"deploy_operation_status": "reconcile_required"}}
    source = {"status": "ok", "product": "example-product", "requests": requests + [testing]}
    argv = ["reconcile-requests-read", "--product", "example-product"]
    status, default, _, _ = _run_main(argv, read=source)
    assert status == 0 and default["result"]["requests_truncated"] is True
    status, selected, posts, reads = _run_main(argv + ["--target-key", testing["target_key"]], read=source)
    assert status == 0 and not posts and len(reads) == 1
    assert selected["result"]["requests_truncated"] is False
    assert [item["target_key"] for item in selected["result"]["requests"]] == [testing["target_key"]]
    assert selected["result"]["requests"][0]["last_plan"] == testing["last_plan"]


def test_event_recovery_authorization_denial_never_falls_back() -> None:
    denied = urllib.error.HTTPError("https://example.invalid", 403, "denied", Message(),
                                   io.BytesIO(b'{"error":{"code":"authorization_denied","message":"Denied"},"trace_id":"launchplane_req_denied"}'))
    status, receipt, posts, reads = _run_main(
        ["generic-web-deploy-recovery-reference-read", "--product", "example-product"], read=denied)
    assert status == 1 and not posts and len(reads) == 1
    assert receipt["warnings"][0]["code"] == "authorization_denied"
    assert receipt["summary"]["trace_id"] == "launchplane_req_denied"


@pytest.mark.parametrize("changes", [{"product": "other"}, {"instance": "prod"}])
def test_event_recovery_wrong_response_lane_cannot_supply_review(changes: dict[str, object]) -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload = _write_json(directory, "reference.json", _event_recovery_payload())
        status, receipt, posts, _ = _run_main(
            ["generic-web-deploy-recovery-dry-run", "--payload-file", payload],
            post=_event_recovery_response(**changes),
        )
    assert status == 1 and len(posts) == 1
    assert receipt["status"] != "ok" and receipt["result"] == {}


def test_event_recovery_product_whitespace_is_normalized_before_review() -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        payload = _write_json(directory, "reference.json", _event_recovery_payload(product=" example-product "))
        status, receipt, posts, _ = _run_main(
            ["generic-web-deploy-recovery-dry-run", "--payload-file", payload],
            post=_event_recovery_response(),
        )
    assert status == 0
    assert posts[0]["body"]["product"] == receipt["request"]["product"] == "example-product"

def _interrupted_http(kind: str) -> Exception:
    error = (http.client.IncompleteRead(b"private-provider-body", 100) if kind == "incomplete"
             else http.client.RemoteDisconnected("private-provider-message"))
    setattr(error, "http_status", 202)
    return error


@pytest.mark.parametrize("kind", ["incomplete", "disconnected"])
@pytest.mark.parametrize("argv", [
    ["testing-hold-read", "--product", "example-product", "--context", "example", "--instance", "testing"],
    ["product-profile-read", "--product", "example-product"],
    ["repository-inventory-read", "--repository-id", "123"],
    ["production-backup-authority-read", "--product", "example-product", "--context", "example", "--instance", "prod", "--promotion-action", "example.promote"],
    ["private-health-endpoint-read", "--product", "example-product", "--context", "example", "--instance", "testing"],
    ["product-promotion-status-read", "--product", "example-product"],
    ["target-replacement-plan-read", "--product", "example-product", "--instance", "testing"],
])
def test_interrupted_http_admin_reads_emit_bounded_json(argv: list[str], kind: str) -> None:
    code, payload, calls = _run_read_transport(argv, response=_interrupted_http(kind))
    assert code == 1 and payload["status"] == "unavailable"
    assert payload["summary"]["http_status"] == 202
    assert len(calls) == 1
    assert "private-provider" not in json.dumps(payload)


@pytest.mark.parametrize("kind,phase", [('incomplete', 'preflight'), ('incomplete', 'post'), ('incomplete', 'readback'), ('disconnected', 'preflight'), ('disconnected', 'post'), ('disconnected', 'readback'), ('decoding', 'preflight'), ('gateway', 'preflight'), ('gateway', 'post'), ('gateway', 'readback'), ('http_timeout', 'preflight'), ('http_timeout', 'post'), ('http_timeout', 'readback')])
def test_interrupted_http_verified_apply_preserves_failure_phase(kind: str, phase: str) -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        evidence = _saved_dry_run_output("product-owner-dry-run", _owner_response(_owner_plan()))
        path = _write_json(directory, "owner-review.json", evidence)
        argv = ["product-owner-apply", *OWNER_DRY_RUN_ARGV[1:],
                *_reviewed_apply_argv(evidence["result"]["plan_sha256"], path)]
        fault = (write_action.ServiceResponseError({"http_status": 202})
             if kind == "decoding" else _interrupted_http(kind))
        if kind in {"gateway", "http_timeout"}:
            fault = urllib.error.HTTPError("https://private.invalid", 502 if kind == "gateway" else 408,
                "PRIVATE_SENTINEL", Message(), io.BytesIO(json.dumps({
                    "trace_id": "launchplane_req_failed_exchange", "error": {
                        "code": "service_timeout", "message": "PRIVATE_SENTINEL",
                    },
                }).encode()))
        reads = iter([fault] if phase == "preflight" else [_profile_response(), fault])
        applied = _owner_response(_owner_plan(mode="apply", applied=True))
        code, payload, posts, gets = _run_main(
            argv, post=fault if phase == "post" else applied, read=lambda _kwargs: next(reads),
        )
    assert code == 1
    assert payload["status"] == {
        "preflight": "invalid" if kind == "decoding" else "unavailable", "post": "outcome_unknown", "readback": "accepted_unverified",
    }[phase]
    assert len(posts) == (0 if phase == "preflight" else 1)
    assert len(gets) == (2 if phase == "readback" else 1)
    if phase != "preflight":
        assert payload["summary"]["recommendation"]
        assert posts[0]["idempotency_key"] == "apply-1"
    if phase == "readback":
        assert payload["summary"]["trace_id"] == applied["trace_id"]
    assert "private-provider" not in json.dumps(payload)

    expected_http_status = fault.code if isinstance(fault, urllib.error.HTTPError) else 202
    assert payload["summary"]["read_back_http_status" if phase == "readback" else "http_status"] == expected_http_status
    if kind in {"gateway", "http_timeout"} and phase == "post":
        assert payload["summary"]["trace_id"] == "launchplane_req_failed_exchange"
        assert payload["summary"]["error_code"] == "service_timeout"
    assert "PRIVATE_SENTINEL" not in json.dumps(payload)


@pytest.mark.parametrize("kind", ["incomplete", "disconnected", "gateway", "denial", "projection", "decoding", "non_object"])
@pytest.mark.parametrize("mode", ["dry-run", "apply"])
@pytest.mark.parametrize("purpose", ["staff_testing", "isolation_staging"])
def test_interrupted_http_simple_post_preserves_write_uncertainty(kind: str, mode: str, purpose: str) -> None:
    with TemporaryDirectory(dir=Path.home()) as directory:
        hold = {**cast(dict[str, object], _testing_hold_plan()["after"]), "purpose": purpose}
        evidence = write_action.summarize_success(
            operation="testing-hold-dry-run", request={"mode": "dry-run", "purpose": purpose},
            provider_payload=_testing_hold_response(_testing_hold_plan(after=hold)),
        )
        path = _write_json(directory, "hold-review.json", evidence)
        argv = [f"testing-hold-{mode}", "--product", "example-product", "--context", "example",
                "--instance", "testing", "--hold", "--purpose", purpose,
                "--reason", str(_testing_hold_plan()["reason"])]
        if mode == "apply":
            argv += _reviewed_apply_argv(evidence["result"]["plan_sha256"], path)
        fault = _interrupted_http(kind)
        if kind in {"gateway", "denial"}:
            error_body = Mock()
            error_body.read.side_effect = _interrupted_http("incomplete")
            fault = urllib.error.HTTPError("https://example.invalid", 502 if kind == "gateway" else 403,
                                           "private-message", Message(), error_body)
        if kind == "projection":
            fault = write_action._ServiceResponse({
                **_testing_hold_response(_testing_hold_plan(after=hold)),
                "unexpected": "private-provider",
            }, 202)
        if kind in {"decoding", "non_object"}:
            response = Mock(status=202)
            response.read.return_value = b"PRIVATE_SENTINEL" if kind == "decoding" else b"null"
            with pytest.raises(write_action.ServiceResponseError) as unreadable:
                write_action._read_service_response(response)
            fault = unreadable.value
        code, payload, posts, _ = _run_main(argv, post=fault)
    assert code == (0 if kind == "projection" and mode == "apply" else 1)
    assert len(posts) == 1
    assert posts[0]["body"].get("purpose", "staff_testing") == purpose
    expected = ("accepted_unverified" if mode == "apply" else "invalid") if kind in {"projection", "decoding", "non_object"} else "denied" if kind == "denial" else "outcome_unknown" if mode == "apply" else "unavailable"
    if kind in {"decoding", "non_object"}:
        assert payload["summary"]["http_status"] == response.status
        assert "PRIVATE_SENTINEL" not in json.dumps(payload)
    if kind == "projection":
        assert payload["summary"]["http_status"] == fault.http_status
        assert payload["summary"]["trace_id"] == fault["trace_id"]
    assert payload["status"] == expected
    if expected in {"outcome_unknown", "accepted_unverified"}:
        assert payload["summary"]["recommendation"]
    assert "private-provider" not in json.dumps(payload)


@pytest.mark.parametrize("kind", ["incomplete", "disconnected"])
@pytest.mark.parametrize("helper", ["context", "client"])
@pytest.mark.parametrize("http_error", [False, True])
def test_interrupted_http_context_and_client_entrypoints(kind: str, helper: str, http_error: bool) -> None:
    fault = _interrupted_http(kind)
    body = Mock()
    body.read.side_effect = fault
    error = urllib.error.HTTPError("https://example.invalid", 502, "private-message", Message(), body)
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=None)
    response.read.side_effect = fault
    module = context_helper if helper == "context" else owner_review
    transport = module if helper == "context" else owner_review.operator
    output = io.StringIO()
    with (
        patch.object(module, "resolve_settings", return_value=SETTINGS),
        patch.object(transport, "safe_urlopen", side_effect=error if http_error else None,
                     return_value=response),
        redirect_stdout(output),
    ):
        code = module.main(["--repo", "example/repo", "--pr", "42"])
    payload = json.loads(output.getvalue())
    assert code == (0 if helper == "context" else 1)
    if helper == "context":
        assert payload["status"] == "unavailable"
    else:
        assert payload["ok"] is False
    assert "private-" not in output.getvalue()
    if http_error:
        if helper == "client":
            assert payload["status_code"] == error.code
        else:
            body.read.assert_not_called()


@pytest.mark.parametrize("kind", ["incomplete", "disconnected"])
@pytest.mark.parametrize("status", [403, 502])
def test_interrupted_http_error_body_keeps_admin_refusal_bounded(kind: str, status: int) -> None:
    body = Mock()
    body.read.side_effect = _interrupted_http(kind)
    error = urllib.error.HTTPError("https://example.invalid", status, "private-message", Message(), body)
    code, payload, _ = _run_read_transport(["product-profile-read", "--product", "example-product"], response=error)
    assert code == 1 and payload["status"] == ("denied" if status == 403 else "unavailable")
    assert "private-" not in json.dumps(payload)


@pytest.mark.parametrize("service_url", ["https://private.example.invalid", "https://private.example.invalid/launchplane/"])
def test_backup_policy_proposal_returns_only_the_bound_product_review_link(service_url: str) -> None:
    operation_id = "privileged-operation-" + "d" * 32
    envelope = {"descriptor_id": "production-backup-policy", "source_event_id": "backup-policy-1", "request": {
        "authority": {"mode": "dry_run", "policy": {"product": "example-product"}, "targets": [{"destination": {"host": "private-proxmox.example.invalid", "guest_id": "999"}}]},
        "reason": "Private preparation reason", "related_issue": "example/launchplane#1",
    }}
    review_path = f"/ui/products/example-product/environments/prod/actions?backup_proposal={operation_id}#production-backup-authority"
    response = {"status": "ok", "trace_id": "launchplane_req_" + "a" * 32, "write_status": "written", "summary": {
        "operation_id": operation_id, "descriptor_id": envelope["descriptor_id"], "status": "planned", "result_status": "ok", "expires_at": "", "review_path": review_path,
    }}
    sent = []
    def post(**kwargs: Any) -> dict[str, object]:
        sent.append(kwargs["body"])
        assert kwargs["path"] == contract.helper_command_path("privileged-policy-propose")
        return response
    with TemporaryDirectory() as directory:
        file = Path(directory) / "proposal.json"
        file.write_text(json.dumps(envelope))
        outputs = []
        for state in ("planned", "approved", "executed"):
            response["summary"]["status"] = state
            response["write_status"] = "written" if state == "planned" else "replayed"
            output = io.StringIO()
            with patch.object(write_action, "resolve_settings", return_value={"service_url": service_url, "token": "private-token"}), patch.object(write_action, "request_launchplane", side_effect=post), redirect_stdout(output):
                assert write_action.main(["privileged-policy-propose", "--payload-file", str(file)]) == 0
            payload = json.loads(output.getvalue())
            assert payload["result"]["review_path"] == review_path
            parsed = urlsplit(payload["result"]["review_url"])
            assert parsed.path == urlsplit(service_url).path.rstrip("/") + urlsplit(review_path).path
            assert parse_qs(parsed.query) == {"backup_proposal": [operation_id]}
            assert parsed.fragment == urlsplit(review_path).fragment
            assert payload["result"]["counts"] == {}
            assert not payload["result"]["authorizes_approval"] and not payload["result"]["authorizes_execution"]
            for private in ("private-proxmox", "999", "private-token", "Private preparation reason", "destination"):
                assert private not in output.getvalue()
            outputs.append(payload)
        assert all(body == envelope for body in sent)
        bad = copy.deepcopy(response)
        bad["summary"]["review_path"] = "https://another-service.example.invalid/private"
        with pytest.raises(write_action.LaunchplaneSafetyError):
            write_action.summarize_privileged_policy_proposal(request={"descriptor_id": envelope["descriptor_id"], "product": "example-product"}, provider_payload=bad, service_url=service_url)

@pytest.mark.parametrize("authority", [{"mode": "apply"}, {"mode": "dry_run", "reviewed_authority_digest": "a" * 64}])
def test_backup_proposal_never_accepts_apply_or_a_caller_reviewed_digest(authority: dict[str, object]) -> None:
    with TemporaryDirectory() as directory:
        file = Path(directory) / "proposal.json"
        file.write_text(json.dumps({"descriptor_id": "production-backup-policy", "source_event_id": "backup-1", "request": {"authority": authority, "reason": "Plan backup protection"}}))
        with patch.object(write_action, "request_launchplane", side_effect=AssertionError("No request should be sent")), pytest.raises(ValueError):
            write_action.privileged_policy_proposal_body(argparse.Namespace(payload_file=str(file)))


@pytest.mark.parametrize("bad_path", ["/ui/products/other-product/environments/prod/actions?backup_proposal=privileged-operation-" + "d" * 32 + "#production-backup-authority", "/ui/products/example-product/environments/prod/actions?backup_proposal=privileged-operation-" + "e" * 32 + "#production-backup-authority"])
def test_backup_proposal_wrong_product_or_operation_is_unverified_and_retained(bad_path: str) -> None:
    envelope = {"descriptor_id": "production-backup-policy", "source_event_id": "backup-1", "request": {"authority": {"mode": "dry_run", "policy": {"product": "example-product"}}, "reason": "Plan protection"}}
    response = {"status": "ok", "trace_id": "launchplane_req_" + "a" * 32, "write_status": "written", "summary": {"operation_id": "privileged-operation-" + "d" * 32, "descriptor_id": envelope["descriptor_id"], "status": "planned", "result_status": "ok", "expires_at": "", "review_path": bad_path}}
    with TemporaryDirectory() as directory:
        file = Path(directory) / "proposal.json"
        file.write_text(json.dumps(envelope))
        output = io.StringIO()
        with patch.object(write_action, "resolve_settings", return_value={"service_url": "https://private.example.invalid", "token": "private-token"}), patch.object(write_action, "request_launchplane", return_value=response), redirect_stdout(output):
            assert write_action.main(["privileged-policy-propose", "--payload-file", str(file)]) == 1
        payload = json.loads(output.getvalue())
        assert payload["status"] == "accepted_unverified"
        assert "review_url" not in payload["result"]
        assert "identical" in payload["summary"]["recommendation"]

@pytest.mark.parametrize("policy", [None, "not-an-object", {}])
def test_backup_proposal_malformed_product_is_a_local_input_error(policy: object) -> None:
    with TemporaryDirectory() as directory:
        file = Path(directory) / "proposal.json"
        file.write_text(json.dumps({"descriptor_id": "production-backup-policy", "source_event_id": "backup-1", "request": {"authority": {"mode": "dry_run", "policy": policy}, "reason": "Plan protection"}}))
        with pytest.raises(ValueError, match="invalid_backup_proposal_product"):
            write_action.privileged_policy_proposal_body(argparse.Namespace(payload_file=str(file)))



@pytest.mark.parametrize("argv", [
    ["repository-inventory-read", "--repository-id", "123"],
    ["production-backup-authority-read", "--product", "example-product", "--context", "example", "--instance", "prod", "--promotion-action", "example.promote"],
    ["private-health-endpoint-read", "--product", "example-product", "--context", "example", "--instance", "testing"],
    ["product-promotion-status-read", "--product", "example-product"],
])
@pytest.mark.parametrize("decoding_failure", [False, True])
def test_legacy_read_failure_keeps_safe_exchange_diagnostics(argv, decoding_failure):
    response = write_action._ServiceResponse({"trace_id": "launchplane_req_invalid", "private": "PRIVATE_SENTINEL"}, 202)
    source = write_action.ServiceResponseError(write_action.service_response_diagnostics(response)) if decoding_failure else response
    code, payload, posts, reads = _run_main(argv, read=source)
    assert code == 1 and not posts and len(reads) == 1
    assert payload["summary"]["http_status"] == response.http_status
    assert payload["summary"]["trace_id"] == response["trace_id"]
    assert "PRIVATE_SENTINEL" not in json.dumps(payload)



RECORD_READ_CASES = [
    (write_action.read_product_owner, {"product": "example-product"}),
    (write_action.read_product_image_repository, {"product": "example-product"}),
    (write_action.read_production_backup_authority, {"query": {}}),
    (write_action.read_dokploy_target, {"context": "example", "instance": "testing"}),
    (write_action.read_health_monitoring, {"body": {"product": "example-product"}}),
    (write_action.read_private_health_endpoint, {"endpoint": {"endpoint_key": "health", "product": "example-product", "context": "example", "instance": "testing"}}),
]

@pytest.mark.parametrize("reader,kwargs", RECORD_READ_CASES)
@pytest.mark.parametrize("safe_trace", [False, True])
def test_record_projection_failures_carry_only_their_safe_exchange_diagnostics(reader, kwargs, safe_trace):
    response = write_action._ServiceResponse({"trace_id": "launchplane_req_preflight" if safe_trace else {"private": "PRIVATE_SENTINEL"}}, 202)
    with patch.object(write_action, "request_launchplane_read", return_value=response):
        with pytest.raises(write_action.ServiceResponseError) as caught:
            reader(settings=SETTINGS, timeout=3, **kwargs)
    diagnostics = write_action.service_response_diagnostics(caught.value)
    assert diagnostics["http_status"] == response.http_status
    assert diagnostics.get("trace_id") == (response["trace_id"] if safe_trace else None)
    assert "PRIVATE_SENTINEL" not in json.dumps(diagnostics)


@pytest.mark.parametrize("error", [write_action.LaunchplaneSafetyError("unsafe_redirect"), _interrupted_http("incomplete")])
@pytest.mark.parametrize("reader,kwargs", RECORD_READ_CASES)
def test_record_reader_preserves_request_failure_type(error, reader, kwargs):
    with patch.object(write_action, "request_launchplane_read", side_effect=error):
        with pytest.raises(type(error)) as caught:
            reader(settings=SETTINGS, timeout=3, **kwargs)
    assert caught.value is error


@pytest.mark.parametrize("mode", ["dry-run", "apply"])
@pytest.mark.parametrize("phase", ["preflight", "post"])
@pytest.mark.parametrize("failure", ["projection", "decoding", "redirect", "transport", "disconnected", "gateway", "http_timeout", "denial"])
def test_policy_import_failure_diagnostics_belong_to_the_failing_exchange(mode, phase, failure):
    preflight = write_action._ServiceResponse(_merge_train_policy_targets_payload(), 201)
    preflight["trace_id"] = "launchplane_req_preflight"
    response = write_action._ServiceResponse(_merge_train_policy_import_response(mode="apply" if mode == "dry-run" else "dry_run"), 202)
    response["trace_id"] = "launchplane_req_post"
    current = preflight if phase == "preflight" else response
    if failure == "projection":
        fault = write_action._ServiceResponse({"trace_id": current["trace_id"]}, current.http_status) if phase == "preflight" else response
    elif failure == "decoding":
        fault = write_action.ServiceResponseError({"http_status": current.http_status})
    elif failure == "redirect":
        fault = write_action.LaunchplaneSafetyError("unsafe_redirect")
    elif failure in {"gateway", "http_timeout", "denial"}:
        fault = urllib.error.HTTPError("https://private.invalid",
            {"gateway": 502, "http_timeout": 408, "denial": 403}[failure],
            "PRIVATE_SENTINEL", Message(), io.BytesIO(json.dumps({
                "trace_id": current["trace_id"], "error": {"code": "service_timeout", "message": "PRIVATE_SENTINEL"},
            }).encode()))
    else:
        fault = (http.client.IncompleteRead(b"PRIVATE_SENTINEL") if failure == "transport"
                 else http.client.RemoteDisconnected("PRIVATE_SENTINEL"))
        setattr(fault, "http_status", current.http_status)
    output = io.StringIO()
    with (
        patch.object(write_action, "prepare_operator_settings", return_value=SETTINGS),
        patch.object(write_action, "request_launchplane_read", side_effect=fault if phase == "preflight" and isinstance(fault, BaseException) else None,
                     return_value=fault if phase == "preflight" else preflight) as get,
        patch.object(write_action, "request_launchplane", side_effect=fault if isinstance(fault, BaseException) else None, return_value=fault) as post,
        redirect_stdout(output),
    ):
        code = write_action.execute_merge_train_policy_import(
            args=argparse.Namespace(timeout=3, idempotency_key="policy-import-example"),
            operation="merge-train-policy-import-" + mode, request={"mode": mode},
            body=_merge_train_policy_import_payload(mode="apply" if mode == "apply" else "dry_run", reason="Approved policy import."),
            expected_current_policy_digest="a" * 64, expected_candidate_policy_digest="c" * 64,
        )
    payload = json.loads(output.getvalue())
    assert get.call_count == 1 and post.call_count == (phase == "post")
    mutating = mode == "apply" and phase == "post"
    if mutating:
        assert payload["status"] == ("accepted_unverified" if failure in {"projection", "decoding"} else "denied" if failure == "denial" else "outcome_unknown")
        if failure in {"gateway", "http_timeout"}:
            assert payload["summary"]["recommendation"]
        assert code == (0 if failure in {"projection", "decoding"} else 1)
    else:
        assert code == 1
        assert payload["status"] == ("unavailable" if failure in {"redirect", "transport", "disconnected", "gateway", "http_timeout"} else "denied" if failure == "denial" else "invalid")
    if failure == "redirect":
        assert "trace_id" not in payload["summary"] and "http_status" not in payload["summary"]
        if not mutating:
            assert payload["warnings"][0]["code"] == fault.code
    else:
        assert payload["summary"]["http_status"] == (fault.code if isinstance(fault, urllib.error.HTTPError) else current.http_status)
        if failure in {"projection", "gateway", "http_timeout", "denial"}:
            assert payload["summary"]["trace_id"] == current["trace_id"]
    if phase == "post":
        assert preflight["trace_id"] not in json.dumps(payload["summary"])
    assert "PRIVATE_SENTINEL" not in json.dumps(payload)


@pytest.mark.parametrize("decoding", [False, True])
def test_verified_apply_malformed_receipt_keeps_the_apply_diagnostics(decoding):
    response = write_action._ServiceResponse(_owner_response(_owner_plan(mode="apply", applied=True)), 202)
    response["unexpected"] = "PRIVATE_SENTINEL"
    fault = write_action.ServiceResponseError({"http_status": response.http_status}) if decoding else response
    with TemporaryDirectory(dir=Path.home()) as directory:
        evidence = _saved_dry_run_output("product-owner-dry-run", _owner_response(_owner_plan()))
        path = _write_json(directory, "owner-review.json", evidence)
        argv = ["product-owner-apply", *OWNER_DRY_RUN_ARGV[1:], *_reviewed_apply_argv(evidence["result"]["plan_sha256"], path)]
        code, payload, posts, reads = _run_main(argv, post=fault, read=_profile_response())
    assert code == 1 and payload["status"] == "accepted_unverified"
    assert len(posts) == 1 and len(reads) == 1
    assert payload["summary"]["http_status"] == response.http_status
    if not decoding:
        assert payload["summary"]["trace_id"] == response["trace_id"]
    assert "PRIVATE_SENTINEL" not in json.dumps(payload)


UNHASHABLE_FIELDS = [
    ("health", "intent"), ("health", "kind"), ("health", "dependency_mode"),
    ("health", "dependencies"), ("backup", "state"), ("backup", "policy"),
    ("backup", "target_status"), ("backup", "destination"), ("endpoint", "status"),
]
UNHASHABLE_CASES = [(family, field, surface) for family, field in UNHASHABLE_FIELDS
                   for surface in ("reader", "dry-run")]
UNHASHABLE_CASES += [(family, field, "standalone") for family, field in UNHASHABLE_FIELDS if family != "health"]
UNHASHABLE_CASES += [(family, field, surface) for family, field in (("health", "intent"), ("backup", "state"), ("endpoint", "status"))
                    for surface in ("apply-post", "apply-readback")]
UNHASHABLE_CASES += [(family, "mode", surface) for family in ("health", "backup", "endpoint") for surface in ("dry-run", "apply-post")]


def _unhashable_fixture(family, field, bad, *, observation, apply=False):
    if family == "health":
        response = _monitoring_profile(applied=apply) if observation else _monitoring_response("apply" if apply else "dry-run")
        record = response["profile"]["lanes"][0]["health_monitoring"] if observation else response["result"]
        if field == "intent":
            record["monitoring_intent" if observation else "current_monitoring_intent"] = bad
        elif field == "kind":
            if observation:
                record["checks"][0]["kind"] = bad
            else:
                record["current_check_kind"] = bad
        elif field in {"dependency_mode", "dependencies"}:
            policy = {"mode": bad, "override": None} if field == "dependency_mode" else {
                "mode": "required", "override": {"dependencies": [bad], "reason": "PRIVATE_SENTINEL"}}
            record["dependency_health" if observation else "current_dependency_health"] = policy
            if not observation:
                record["requested_dependency_health"] = {"mode": "informational", "override": None}
        else:
            record["mode"] = bad
    elif family == "backup":
        response = _backup_read_response() if observation else _backup_response("apply" if apply else "dry_run", "applied" if apply else "would_apply")
        record = response["authority" if observation else "result"]
        if field == "state":
            record["state" if observation else "status"] = bad
        elif field == "policy":
            record["policy"]["status"] = bad
        elif field in {"target_status", "destination"}:
            record["targets"][0]["status" if field == "target_status" else "destination_kind"] = bad
        else:
            record["mode"] = bad
    else:
        response = _private_endpoint_read() if observation else _private_endpoint_response("apply" if apply else "dry-run")
        record = response["record"] if observation else response["result"]["record"]
        if field == "mode":
            response["result"]["mode"] = bad
        else:
            record["status"] = bad
    response["trace_id"] = "launchplane_req_malformed_observation" if observation else "launchplane_req_malformed_post"
    return write_action._ServiceResponse(response, 202)


@pytest.mark.parametrize("family,field,surface", UNHASHABLE_CASES)
@pytest.mark.parametrize("bad", [{"private": "PRIVATE_SENTINEL"}, ["PRIVATE_SENTINEL"]])
def test_unhashable_service_values_preserve_bounded_failure_receipts(tmp_path, family, field, surface, bad):
    observation = surface in {"reader", "standalone", "apply-readback"}
    malformed = _unhashable_fixture(family, field, bad, observation=observation, apply=surface.startswith("apply"))
    if family == "health":
        command, arguments = "health-monitoring", _monitoring_arguments()
        reader, reader_args = write_action.read_health_monitoring, {"body": {"product": "example-product", "context": "example", "instance": "testing", "check_name": "public-ingress"}}
        dry_response, applied = _monitoring_response(), _monitoring_response("apply")
        valid_observation = _monitoring_profile()
    else:
        command = "production-backup-authority" if family == "backup" else "private-health-endpoint"
        path = _write_json(str(tmp_path), "request.json", _backup_payload() if family == "backup" else _private_endpoint_payload())
        arguments = ["--payload-file", path] + (["--reason", "Register the private health endpoint."] if family == "endpoint" else [])
        reader = write_action.read_production_backup_authority if family == "backup" else write_action.read_private_health_endpoint
        reader_args = {"query": {}} if family == "backup" else {"endpoint": _private_endpoint_payload()}
        dry_response = _backup_response() if family == "backup" else _private_endpoint_response()
        applied = _backup_response("apply", "applied") if family == "backup" else _private_endpoint_response("apply")
        valid_observation = _backup_read_response() if family == "backup" else _private_endpoint_read()
    if surface == "reader":
        with patch.object(write_action, "request_launchplane_read", return_value=malformed):
            with pytest.raises(write_action.ServiceResponseError) as caught:
                reader(settings=SETTINGS, timeout=3, **reader_args)
        diagnostics = write_action.service_response_diagnostics(caught.value)
        assert diagnostics == {"http_status": malformed.http_status, "trace_id": malformed["trace_id"]}
        assert "PRIVATE_SENTINEL" not in json.dumps(diagnostics)
        return
    if surface == "standalone":
        if family == "backup":
            argv = [command + "-read", "--product", "example-product", "--context", "example", "--instance", "prod", "--promotion-action", "generic_web_prod_promotion.execute"]
        else:
            argv = [command + "-read", "--product", "example", "--context", "example", "--instance", "testing"]
            malformed = write_action._ServiceResponse({"status": "ok", "trace_id": malformed["trace_id"], "records": [malformed["record"]]}, malformed.http_status)
        code, output, posts, reads = _run_main(argv, read=malformed)
        assert not posts and len(reads) == 1
    elif surface == "dry-run":
        code, output, posts, reads = _run_main([command + "-dry-run", *arguments], post=malformed)
        assert len(posts) == 1 and not reads
    else:
        code, evidence, _, _ = _run_main([command + "-dry-run", *arguments], post=dry_response)
        assert code == 0
        saved = _write_json(str(tmp_path), "evidence.json", evidence)
        digest = evidence["result"]["authority_digest" if family == "backup" else "plan_sha256"]
        argv = [command + "-apply", *arguments, *_reviewed_apply_argv(digest, saved)]
        observed = malformed if surface == "apply-readback" else valid_observation
        observations = iter(([valid_observation] if family == "health" else []) + [observed])
        code, output, posts, reads = _run_main(argv, post=applied if surface == "apply-readback" else malformed, read=lambda _: next(observations))
        assert len(posts) == 1
        assert output["status"] == "accepted_unverified"
        if surface == "apply-readback":
            assert output["summary"]["trace_id"] == applied["trace_id"]
    assert code == 1
    if not surface.startswith("apply"):
        assert output["status"] == "invalid"
    prefix = "read_back_" if surface == "apply-readback" else ""
    assert output["summary"][prefix + "http_status"] == malformed.http_status
    assert output["summary"][prefix + "trace_id"] == malformed["trace_id"]
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))


@pytest.mark.parametrize("phase,failure", [(phase, failure) for phase in ("preflight", "apply", "readback")
    for failure in ("decoding", "partial", "projection", "comparison") if not (phase == "preflight" and failure == "comparison")])
def test_runtime_sync_failure_diagnostics_follow_the_current_exchange(phase, failure):
    flags = ["--product", "example-site", "--context", "example-context", "--instance", "testing"]
    _, evidence, _ = _run_runtime_sync(["live-target-runtime-sync-dry-run", *flags], [_runtime_sync_response()])
    applied = write_action._ServiceResponse(_runtime_sync_response(mode="apply"), 201)
    applied["trace_id"] = "launchplane_req_apply"
    response = write_action._ServiceResponse(_runtime_sync_response(mode="apply" if phase == "apply" else "dry-run", changed=phase != "readback"), 202)
    response["trace_id"] = "launchplane_req_current"
    fault = response
    if failure == "decoding":
        raw = Mock(status=response.http_status)
        raw.read.return_value = b"null"
        with pytest.raises(write_action.ServiceResponseError) as error:
            write_action._read_service_response(raw)
        fault = error.value
    elif failure == "partial":
        raw = Mock(status=response.http_status)
        raw.read.side_effect = http.client.IncompleteRead(b"PRIVATE_SENTINEL")
        with pytest.raises(http.client.IncompleteRead) as error:
            write_action._read_service_response(raw)
        fault = error.value
    elif failure == "projection":
        response["status"] = "invalid"
    elif phase == "apply":
        response["result"]["tracked_target"]["target_id"] = "different-target"
    else:
        response["result"]["runtime_environment"]["changed_keys"] = ["EXAMPLE_API_TOKEN"]
    responses = ([fault] if phase == "preflight" else [_runtime_sync_response(), fault] if phase == "apply"
        else [_runtime_sync_response(), applied, fault])
    with TemporaryDirectory() as directory:
        path = _write_json(directory, "review.json", evidence)
        argv = ["live-target-runtime-sync-apply", *flags, "--idempotency-key", "same-apply", "--reviewed-dry-run",
            "--expected-plan-digest", evidence["result"]["plan_sha256"], "--dry-run-evidence-file", path]
        code, output, calls = _run_runtime_sync(argv, responses)
    assert code == 1
    expected_status = ("unavailable" if failure == "partial" else "invalid") if phase == "preflight" else "accepted_unverified"
    assert output["status"] == expected_status
    assert calls.call_count == {"preflight": 1, "apply": 2, "readback": 3}[phase]
    assert sum(call.kwargs["body"]["mode"] == "apply" for call in calls.call_args_list) == (phase != "preflight")
    prefix = "read_back_" if phase == "readback" else ""
    assert output["summary"][prefix + "http_status"] == response.http_status
    if failure in {"projection", "comparison"}:
        assert output["summary"][prefix + "trace_id"] == response["trace_id"]
    if phase == "readback":
        assert output["summary"]["trace_id"] == applied["trace_id"]
    else:
        assert "read_back_http_status" not in output["summary"]
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize("operation", [
    "odoo-addon-settings-apply", "odoo-website-settings-apply",
    "integration-allowances-apply", "testing-hold-apply",
    "product-repository-identity-apply", "generic-web-deploy-recovery-apply",
    "repository-inventory-apply", "product-expected-config-apply",
])
@pytest.mark.parametrize("body", [b"null", b"PRIVATE_SENTINEL"])
def test_simple_apply_unreadable_receipts_keep_projection_recovery(operation, body):
    response = Mock(status=202)
    response.read.return_value = body
    with pytest.raises(write_action.ServiceResponseError) as failure:
        write_action._read_service_response(response)
    receipts = []
    for source in (write_action._ServiceResponse({"status": "accepted", "trace_id": "launchplane_req_projection"}, response.status), failure.value):
        output = io.StringIO()
        with patch.object(write_action, "prepare_operator_settings", return_value=SETTINGS), patch.object(
            write_action, "request_launchplane", side_effect=source if isinstance(source, Exception) else None,
            return_value=source,
        ) as post, redirect_stdout(output):
            code = write_action.execute_post(args=argparse.Namespace(timeout=3, idempotency_key="same-apply"),
                operation=operation, path="/fixture", request={"mode": "apply"}, body={})
        receipt = json.loads(output.getvalue())
        assert code == (1 if isinstance(source, Exception) else 0)
        assert receipt["status"] == "accepted_unverified"
        assert receipt["summary"]["http_status"] == response.status
        post.assert_called_once()
        assert "PRIVATE_SENTINEL" not in output.getvalue()
        receipts.append(receipt)
    assert receipts[1]["summary"]["recommendation"] == receipts[0]["summary"]["recommendation"]
    assert receipts[1]["warnings"] == receipts[0]["warnings"]


@pytest.mark.parametrize("prose", [
    "Existing testing credentials for the development store.",
    "cookie Abc.Def9", "password=fixture-private-value", "ghp_" + "a" * 36, "credentials:",
])
def test_nested_operator_prose_redacts_without_rejecting(prose: str) -> None:
    sharing = write_action._project_sharing_reason({
        "kind": "read_only_source", "reason": prose, "evidence": prose,
    })
    hold = write_action._project_testing_hold({
        "reason": prose, "rehearsal": {
            "binding_sha256": "a" * 64, "target_updated_at": prose,
            "target_source_label": prose,
        },
    })
    warning = write_action._protected_artifact_warning(prose)
    for value in (sharing["reason"], sharing["evidence"], hold["reason"],
                  hold["rehearsal"]["target_updated_at"], hold["rehearsal"]["target_source_label"], warning):
        assert isinstance(value, str) and value
    result = {"sharing": sharing, "hold": hold, "warning": warning}
    for private in ("Abc.Def9", "fixture-private-value", "ghp_" + "a" * 36):
        assert private not in json.dumps(result)
    write_action.assert_public_safe_shape(result)


@pytest.mark.parametrize("prose", ["Waiting for credentials to be removed.", "cookie Abc.Def9"])
def test_nested_service_prose_redacts_without_rejecting(prose: str) -> None:
    blocked = write_action._project_merge_train_blocking_reason({"code": "not_ready", "message": prose})
    drops = write_action._FieldDrops()
    event = write_action._project_product_activity_event({
        "event_id": "event-1", "event_type": "testing.updated", "title": prose, "summary": prose,
    }, drops)
    assert blocked["message"] and event["title"] and event["summary"]
    assert "Abc.Def9" not in json.dumps({"blocked": blocked, "event": event})
    assert not drops.paths
    write_action.assert_public_safe_shape({"blocked": blocked, "event": event})


@pytest.mark.parametrize("prose", [
    "Launchplane rejected the request: lane busy",
    "The required value:",
    "Observed OPW_ODOO_19_DATABASE_NAME and GA4_MEASUREMENT_ID_PRIMARY",
])
def test_service_prose_preserves_diagnostic_identifiers_and_ordinary_colons(prose: str) -> None:
    result = write_action._project_merge_train_blocking_reason({"code": "not_ready", "message": prose})
    assert result["message"] == prose


def test_service_prose_with_a_denied_word_and_empty_assignment_still_projects() -> None:
    result = write_action._project_merge_train_blocking_reason({
        "code": "not_ready", "message": "Check credentials; the required value:",
    })
    assert isinstance(result["message"], str) and result["message"]
    write_action.assert_public_safe_shape(result)


def test_context_prose_with_credentials_still_projects() -> None:
    graph = context_helper._project_work_graph_item({
        "next_action": "Add the Shopify credentials to the testing lane.",
        "why_now": "Testing credentials were rotated.",
    })
    preview = context_helper._project_preview_item({"detail": "Testing credentials were rotated."})
    assert graph["next_action"] and graph["why_now"] and preview["detail"]
    write_action.assert_public_safe_shape({"graph": graph, "preview": preview})


def test_merge_train_protocol_reasons_preserve_machine_matched_labels() -> None:
    reasons = ["missing queue-for-testing-2026 label"]
    projected = write_action._project_merge_train_queue_entry({
        "number": 17, "head_sha": "a" * 40, "actor_role": "agent", "mergeable": "MERGEABLE",
        "required_checks_status": "pass", "eligible": False, "branch_update_required": False,
        "ineligible_reasons": reasons,
    })
    assert projected["ineligible_reasons"] == reasons
