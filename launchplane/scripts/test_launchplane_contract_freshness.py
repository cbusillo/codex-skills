#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Hermetic tests for Launchplane contract semantic freshness."""

from __future__ import annotations

import copy
import http.client
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import launchplane_contract as contract
import launchplane_contract_freshness as freshness


def contract_artifact() -> dict[str, Any]:
    return json.loads(contract.DEFAULT_CONTRACT_PATH.read_text(encoding="utf-8"))


def encoded(artifact: dict[str, Any]) -> bytes:
    return json.dumps(artifact).encode()


def changed_artifact() -> dict[str, Any]:
    artifact = copy.deepcopy(contract_artifact())
    artifact["contract"]["invariants"]["governance"][
        "owner_acceptance_authoritative"
    ] = False
    artifact["semantic_digest_sha256"] = contract.semantic_digest(artifact)
    return artifact


def test_matching_digest_is_current() -> None:
    result = freshness.evaluate_freshness(
        attempts=1, fetcher=lambda **_kwargs: encoded(contract_artifact())
    )
    assert result["classification"] == "current"
    assert result["retryable"] is False
    assert result["comparison"]["semantic_digest_match"] is True


def test_provenance_only_change_is_current() -> None:
    upstream = contract_artifact()
    upstream["provenance"]["source_commit_sha"] = "f" * 40
    result = freshness.evaluate_freshness(
        attempts=1, fetcher=lambda **_kwargs: encoded(upstream)
    )
    assert result["classification"] == "current"
    assert result["upstream"]["source_commit_sha"] == "f" * 40


def test_valid_semantic_mismatch_is_known_stale() -> None:
    upstream = changed_artifact()
    contract.validate_semantic_artifact(upstream)
    try:
        contract.validate_contract(upstream)
    except contract.ContractError as exc:
        assert exc.code == "invariant_contract_mismatch"
    else:
        raise AssertionError("local conformance must reject changed semantics")
    result = freshness.evaluate_freshness(
        attempts=1, fetcher=lambda **_kwargs: encoded(upstream)
    )
    assert result["classification"] == "known-stale"
    assert result["comparison"]["semantic_digest_match"] is False


def test_semantic_parser_accepts_changed_allow_list_and_workflow_bindings() -> None:
    added_operation = contract_artifact()
    operation = copy.deepcopy(added_operation["contract"]["operations"][0])
    operation["operation_id"] = "future_agent_operation"
    operation["path"] = "/v1/agent/future-operation"
    operation["schema_fingerprint_sha256"] = "e" * 64
    added_operation["contract"]["operations"].append(operation)
    added_operation["semantic_digest_sha256"] = contract.semantic_digest(
        added_operation
    )
    contract.validate_semantic_artifact(added_operation)
    try:
        contract.validate_contract(added_operation)
    except contract.ContractError as exc:
        assert exc.code == "unexpected_operation_allow_list"
    else:
        raise AssertionError("local conformance must reject a changed allow-list")

    changed_workflow = contract_artifact()
    changed_workflow["contract"]["protected_workflows"][0]["workflow_file"] = (
        ".github/workflows/future-product-retirement.yml"
    )
    changed_workflow["semantic_digest_sha256"] = contract.semantic_digest(
        changed_workflow
    )
    contract.validate_semantic_artifact(changed_workflow)
    try:
        contract.validate_contract(changed_workflow)
    except contract.ContractError as exc:
        assert exc.code == "unexpected_protected_workflows"
    else:
        raise AssertionError("local conformance must reject changed workflow bindings")


def test_unavailable_upstream_is_unknown_and_retryable() -> None:
    def unavailable(**_kwargs: Any) -> bytes:
        raise freshness.FreshnessError("upstream_unavailable", retryable=True)

    result = freshness.evaluate_freshness(attempts=1, fetcher=unavailable)
    assert result["classification"] == "unknown"
    assert result["retryable"] is True
    assert result["error"] == {
        "code": "upstream_unavailable",
        "detail_code": None,
    }

    incomplete = freshness.evaluate_freshness(
        attempts=1,
        fetcher=lambda **_kwargs: (_ for _ in ()).throw(
            http.client.IncompleteRead(b"partial")
        ),
    )
    assert incomplete["classification"] == "unknown"
    assert incomplete["retryable"] is True


def test_malformed_and_unsupported_upstream_are_unknown() -> None:
    malformed = freshness.evaluate_freshness(
        attempts=1, fetcher=lambda **_kwargs: b"{not-json"
    )
    assert malformed["classification"] == "unknown"
    assert malformed["error"]["code"] == "upstream_json_invalid"

    unsupported = contract_artifact()
    unsupported["normalization_version"] = 2
    unsupported["semantic_digest_sha256"] = contract.semantic_digest(unsupported)
    result = freshness.evaluate_freshness(
        attempts=1, fetcher=lambda **_kwargs: encoded(unsupported)
    )
    assert result["classification"] == "unknown"
    assert result["error"] == {
        "code": "upstream_contract_invalid",
        "detail_code": "unsupported_normalization_version",
    }

    tampered = changed_artifact()
    tampered["semantic_digest_sha256"] = contract_artifact()["semantic_digest_sha256"]
    result = freshness.evaluate_freshness(
        attempts=1, fetcher=lambda **_kwargs: encoded(tampered)
    )
    assert result["classification"] == "unknown"
    assert result["error"]["detail_code"] == "semantic_digest_mismatch"


def test_known_stale_creates_one_maintenance_issue() -> None:
    evidence = freshness.compare_artifacts(contract_artifact(), changed_artifact())
    calls: list[tuple[list[str], str | None]] = []

    def runner(args: list[str], stdin: str | None) -> dict[str, Any]:
        calls.append((args, stdin))
        if args[1].endswith("gh-plan.py"):
            return {"ok": True, "issues": []}
        return {"ok": True, "issue": {"number": 700}}

    report = freshness.report_maintenance_issue(
        evidence, repository="example/repo", runner=runner
    )
    assert report["issue"] == {"action": "created", "number": 700}
    assert len(calls) == 2
    assert calls[1][0][-1] == freshness.MAINTENANCE_ISSUE_TITLE
    assert calls[1][1] is not None
    assert freshness.MAINTENANCE_ISSUE_MARKER in calls[1][1]


def test_repeated_mismatch_updates_the_existing_issue() -> None:
    evidence = freshness.compare_artifacts(contract_artifact(), changed_artifact())
    calls: list[tuple[list[str], str | None]] = []

    def runner(args: list[str], stdin: str | None) -> dict[str, Any]:
        calls.append((args, stdin))
        if args[1].endswith("gh-plan.py"):
            return {
                "ok": True,
                "issues": [
                    {
                        "number": 701,
                        "repo": "example/repo",
                        "state": "OPEN",
                        "title": "Renamed maintenance issue",
                    }
                ],
            }
        return {"ok": True, "issue": {"number": 701}}

    report = freshness.report_maintenance_issue(
        evidence, repository="example/repo", runner=runner
    )
    assert report["issue"] == {"action": "updated", "number": 701}
    assert len(calls) == 2
    assert "edit" in calls[1][0]
    assert calls[1][0][-1] == "701"
    assert freshness.MAINTENANCE_ISSUE_TITLE in calls[1][0]
    assert any(freshness.MAINTENANCE_ISSUE_KEY in argument for argument in calls[0][0])


def test_unknown_never_opens_a_maintenance_issue() -> None:
    calls: list[tuple[list[str], str | None]] = []
    evidence = freshness.evaluate_freshness(
        attempts=1,
        fetcher=lambda **_kwargs: (_ for _ in ()).throw(
            freshness.FreshnessError("upstream_unavailable", retryable=True)
        ),
    )
    report = freshness.report_maintenance_issue(
        evidence,
        repository="example/repo",
        runner=lambda args, stdin: calls.append((args, stdin)) or {},
    )
    assert report["issue"] == {"action": "none", "number": None}
    assert calls == []


def test_evidence_and_issues_redact_raw_failures_and_contract_bodies() -> None:
    private_failure = (
        "https://private.example.invalid/contracts?token=never-render-this"
    )

    def unavailable(**_kwargs: Any) -> bytes:
        raise OSError(private_failure)

    unknown = freshness.evaluate_freshness(attempts=1, fetcher=unavailable)
    rendered_unknown = json.dumps(unknown)
    assert private_failure not in rendered_unknown
    assert "never-render-this" not in rendered_unknown
    assert "https://" not in rendered_unknown
    assert '"operations"' not in rendered_unknown

    invalid_source = freshness.evaluate_freshness(
        repository="private-org/private-repo",
        attempts=1,
        fetcher=lambda **_kwargs: encoded(contract_artifact()),
    )
    rendered_source = json.dumps(invalid_source)
    assert "private-org" not in rendered_source
    assert invalid_source["error"]["code"] == "unapproved_upstream_source"

    stale = freshness.compare_artifacts(contract_artifact(), changed_artifact())
    body = freshness.maintenance_issue_body(stale)
    assert "https://" not in body
    assert "never-render-this" not in body
    assert '"operations"' not in body


def test_schedule_and_dispatch_share_the_comparison_implementation() -> None:
    workflow = (
        REPO_ROOT / ".github" / "workflows" / "launchplane-contract-freshness.yml"
    ).read_text(encoding="utf-8")
    assert "schedule:" in workflow
    assert "workflow_dispatch:" in workflow
    assert workflow.count("check-agent-operator-contract-freshness.py compare") == 1
    assert workflow.count("check-agent-operator-contract-freshness.py report") == 1
    assert "issues: write" in workflow
    assert "contents: read" in workflow
    assert "GH_WITH_ENV_TOKEN_EXPECTED_LOGIN: github-actions[bot]" in workflow
    assert "needs.compare.result == 'success'" in workflow


def test_local_conformance_remains_independent_of_remote_freshness() -> None:
    script = (SCRIPT_DIR / "check-agent-operator-contract.py").read_text(
        encoding="utf-8"
    )
    assert "launchplane_contract_freshness" not in script
    result = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "check-agent-operator-contract.py")],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["summary"]["upstream_freshness_proven"] is False


def main() -> int:
    tests = [
        test_matching_digest_is_current,
        test_provenance_only_change_is_current,
        test_valid_semantic_mismatch_is_known_stale,
        test_semantic_parser_accepts_changed_allow_list_and_workflow_bindings,
        test_unavailable_upstream_is_unknown_and_retryable,
        test_malformed_and_unsupported_upstream_are_unknown,
        test_known_stale_creates_one_maintenance_issue,
        test_repeated_mismatch_updates_the_existing_issue,
        test_unknown_never_opens_a_maintenance_issue,
        test_evidence_and_issues_redact_raw_failures_and_contract_bodies,
        test_schedule_and_dispatch_share_the_comparison_implementation,
        test_local_conformance_remains_independent_of_remote_freshness,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
