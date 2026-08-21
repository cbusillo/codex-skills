"""Load and validate the vendored Launchplane agent/operator contract."""
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
NORMALIZATION_VERSION = 1
DEFAULT_CONTRACT_PATH = (
    Path(__file__).resolve().parent.parent
    / "references"
    / "agent-operator-contract.json"
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA_PATTERN = re.compile(r"^(unknown|[0-9a-f]{40}|[0-9a-f]{64})$")
_OPERATION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_PATH_PATTERN = re.compile(r"^/v1/[a-z0-9_./-]+$")
_CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_STATUS_PATTERN = re.compile(r"^[1-5][0-9]{2}$")
_WORKFLOW_PATTERN = re.compile(r"^\.github/workflows/[a-z0-9_.-]+\.ya?ml$")
_UNSAFE_STRING_PATTERNS = (
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\bLAUNCHPLANE_[A-Z0-9_]+\b"),
    re.compile(r"\b(?:token|password|private[_-]?key)\s*[:=]", re.IGNORECASE),
)

_ARTIFACT_KEYS = {
    "schema_version",
    "normalization_version",
    "semantic_digest_sha256",
    "provenance",
    "contract",
}
_CONTRACT_KEYS = {"operations", "protected_workflows", "invariants"}
_OPERATION_KEYS = {
    "method",
    "path",
    "operation_id",
    "identity_dependencies",
    "purpose",
    "supported_surfaces",
    "modes",
    "idempotency",
    "reviewed_evidence",
    "response_statuses",
    "schema_fingerprint_sha256",
}
_WORKFLOW_KEYS = {"workflow_file", "reusable_workflow_file", "route"}

EXPECTED_OPERATION_CONTRACTS = {
    "read_agent_context": {
        "method": "GET",
        "purpose": "Read public-safe Launchplane context for an agent task.",
        "supported_surfaces": ["agent_helper", "read_only_service"],
        "modes": ["read"],
        "idempotency": "none",
        "reviewed_evidence": [],
    },
    "evaluate_agent_write_intent": {
        "method": "POST",
        "purpose": "Evaluate a bounded agent write intent before mutation.",
        "supported_surfaces": ["agent_helper", "service_api"],
        "modes": ["preflight"],
        "idempotency": "optional",
        "reviewed_evidence": [],
    },
    "apply_product_config": {
        "method": "POST",
        "purpose": "Dry-run or apply reviewed product configuration.",
        "supported_surfaces": ["agent_helper", "operator_ui", "service_api"],
        "modes": ["dry-run", "apply"],
        "idempotency": "apply",
        "reviewed_evidence": ["reviewed_dry_run"],
    },
    "apply_change_impact_policy": {
        "method": "POST",
        "purpose": "Dry-run or apply a reviewed change-impact policy revision.",
        "supported_surfaces": ["agent_helper", "operator_ui", "service_api"],
        "modes": ["dry-run", "apply"],
        "idempotency": "none",
        "reviewed_evidence": ["reviewed_dry_run", "expected_policy_digest"],
    },
    "read_change_impact_policy": {
        "method": "GET",
        "purpose": "Read active change-impact policy revision evidence.",
        "supported_surfaces": ["agent_helper", "read_only_service"],
        "modes": ["read"],
        "idempotency": "none",
        "reviewed_evidence": [],
    },
    "write_merge_train_controller_run_once": {
        "method": "POST",
        "purpose": "Advance one merge-train controller phase with bounded recovery evidence.",
        "supported_surfaces": ["agent_helper", "operator_ui", "service_api"],
        "modes": ["dry-run", "mutate"],
        "idempotency": "mutate",
        "reviewed_evidence": [],
    },
    "remediate_preview_pr_feedback": {
        "method": "POST",
        "purpose": "Dry-run or apply bounded historical preview-feedback remediation.",
        "supported_surfaces": ["agent_helper", "operator_ui", "service_api"],
        "modes": ["dry-run", "apply"],
        "idempotency": "apply",
        "reviewed_evidence": ["reviewed_dry_run"],
    },
    "execute_product_retirement": {
        "method": "POST",
        "purpose": "Plan or apply protected product retirement.",
        "supported_surfaces": ["protected_workflow", "operator_ui", "service_api"],
        "modes": ["plan", "apply"],
        "idempotency": "always",
        "reviewed_evidence": ["reviewed_plan_digest"],
    },
    "execute_detached_application_retirement": {
        "method": "POST",
        "purpose": "Plan or apply detached application retirement with zero authority writes.",
        "supported_surfaces": ["protected_workflow", "operator_ui", "service_api"],
        "modes": ["plan", "apply"],
        "idempotency": "always",
        "reviewed_evidence": ["reviewed_plan_digest"],
    },
    "reconcile_managed_authz_policy": {
        "method": "POST",
        "purpose": "Dry-run or apply managed authorization reconciliation.",
        "supported_surfaces": ["protected_workflow", "operator_ui", "service_api"],
        "modes": ["dry-run", "apply"],
        "idempotency": "apply",
        "reviewed_evidence": ["reviewed_plan_digest"],
    },
    "apply_product_stable_lane_repair": {
        "method": "POST",
        "purpose": "Dry-run or apply a bounded stable-lane profile repair.",
        "supported_surfaces": ["protected_workflow", "operator_ui", "service_api"],
        "modes": ["dry-run", "apply"],
        "idempotency": "apply",
        "reviewed_evidence": ["reviewed_plan_digest"],
    },
    "read_governance_projection": {
        "method": "GET",
        "purpose": "Read governance evidence without granting mutation authority.",
        "supported_surfaces": ["read_only_service", "operator_ui"],
        "modes": ["read"],
        "idempotency": "none",
        "reviewed_evidence": [],
    },
}

PROJECTED_HELPER_COMMANDS = {
    "agent-context": ("read_agent_context", ("read",)),
    "product-config-preflight": (
        "evaluate_agent_write_intent",
        ("preflight",),
    ),
    "product-config-dry-run": ("apply_product_config", ("dry-run",)),
    "product-config-apply": ("apply_product_config", ("apply",)),
    "change-impact-policy-dry-run": (
        "apply_change_impact_policy",
        ("dry-run",),
    ),
    "change-impact-policy-apply": ("apply_change_impact_policy", ("apply",)),
    "change-impact-policy-read": ("read_change_impact_policy", ("read",)),
    "merge-train-controller-run-once": (
        "write_merge_train_controller_run_once",
        ("dry-run", "mutate"),
    ),
    "preview-feedback-remediation": (
        "remediate_preview_pr_feedback",
        ("dry-run", "apply"),
    ),
}

LOCAL_EXTENSION_ROUTES = {
    "generic-web-deploy-recovery-dry-run": {
        "method": "POST",
        "path": "/v1/admin/generic-web/deploy-recovery/dry-run",
        "mode": "dry-run",
    },
    "generic-web-deploy-recovery-apply": {
        "method": "POST",
        "path": "/v1/admin/generic-web/deploy-recovery/apply",
        "mode": "apply",
    },
}

EXPECTED_PROTECTED_WORKFLOWS = {
    ".github/workflows/product-retirement.yml": {
        "operation_id": "execute_product_retirement",
        "reusable_workflow_file": ".github/workflows/reusable-product-retirement.yml",
    },
    ".github/workflows/detached-application-retirement.yml": {
        "operation_id": "execute_detached_application_retirement",
        "reusable_workflow_file": ".github/workflows/reusable-detached-application-retirement.yml",
    },
    ".github/workflows/authz-policy-reconcile.yml": {
        "operation_id": "reconcile_managed_authz_policy",
        "reusable_workflow_file": ".github/workflows/reusable-authz-policy-reconcile.yml",
    },
    ".github/workflows/product-onboarding-manifest.yml": {
        "operation_id": "apply_product_stable_lane_repair",
    },
}

EXPECTED_INVARIANTS = {
    "detached_retirement": {
        "authority_write_count": 0,
        "completion_requires_candidate_absence": True,
    },
    "governance": {
        "authorization_admission_and_landing_are_independent": True,
        "engineering_review_advisory": True,
        "github_projection_role": "routing_and_status_only",
        "owner_acceptance_authoritative": True,
    },
    "product_lifecycle": {
        "active_automation_states": ["active"],
        "retired_previews_enabled": False,
        "states": ["active", "retiring", "retired"],
        "transitions": ["active->retiring", "retiring->retired"],
    },
    "protected_workflow_policy": {
        "dispatch_and_watch_helper": "github_workflow_babysit.py",
        "raw_workflow_dispatch_allowed": False,
    },
    "reconciliation": {
        "effect_unknown": "reconciliation_required_before_reexecution",
        "landing_sha_source": "terminal_controller_result_only",
        "pre_effect": "retry_only_after_fixing_the_block",
    },
    "stable_deploy_identity": {
        "artifact_id": "digest_pinned_evidence_and_runtime_identity",
        "deploy_reference": "published_same_repository_immutable_sha_tag",
        "floating_tags_allowed": False,
        "image_reference": "provider_tag_readback",
        "target_category": "application",
    },
}


class ContractError(ValueError):
    """Fail-closed contract validation error with a public-safe code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def semantic_digest(artifact: Mapping[str, Any]) -> str:
    payload = {
        "normalization_version": artifact.get("normalization_version"),
        "contract": artifact.get("contract"),
    }
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _require_exact_keys(
    value: Mapping[str, Any], expected: set[str], code: str
) -> None:
    if set(value) != expected:
        raise ContractError(code)


def _require_string_list(value: object, code: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ContractError(code)
    return value


def _validate_public_safety(value: Any) -> None:
    if isinstance(value, str):
        if any(pattern.search(value) for pattern in _UNSAFE_STRING_PATTERNS):
            raise ContractError("unsafe_public_value")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_public_safety(str(key))
            _validate_public_safety(item)
        return
    if isinstance(value, list | tuple):
        for item in value:
            _validate_public_safety(item)
        return
    if value is not None and not isinstance(value, bool | int | float):
        raise ContractError("non_json_value")


def _operation_map(contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    operations = contract.get("operations")
    if not isinstance(operations, list):
        raise ContractError("invalid_operations")
    operation_map: dict[str, Mapping[str, Any]] = {}
    route_keys: set[tuple[str, str]] = set()
    for operation in operations:
        if not isinstance(operation, Mapping):
            raise ContractError("invalid_operation")
        _require_exact_keys(operation, _OPERATION_KEYS, "invalid_operation_keys")
        operation_id = operation.get("operation_id")
        method = operation.get("method")
        path = operation.get("path")
        purpose = operation.get("purpose")
        if not isinstance(operation_id, str) or not _OPERATION_ID_PATTERN.fullmatch(
            operation_id
        ):
            raise ContractError("invalid_operation_id")
        if operation_id in operation_map:
            raise ContractError("duplicate_operation_id")
        if method not in {"GET", "POST"}:
            raise ContractError("invalid_operation_method")
        if not isinstance(path, str) or not _PATH_PATTERN.fullmatch(path):
            raise ContractError("invalid_operation_path")
        route_key = (method, path)
        if route_key in route_keys:
            raise ContractError("duplicate_operation_route")
        route_keys.add(route_key)
        if not isinstance(purpose, str) or not purpose.strip():
            raise ContractError("invalid_operation_purpose")
        for field in (
            "identity_dependencies",
            "supported_surfaces",
            "modes",
            "reviewed_evidence",
        ):
            values = _require_string_list(
                operation.get(field), f"invalid_operation_{field}"
            )
            if len(values) != len(set(values)) or not all(
                _CODE_PATTERN.fullmatch(item) for item in values
            ):
                raise ContractError(f"invalid_operation_{field}")
        if not operation["identity_dependencies"]:
            raise ContractError("missing_identity_dependencies")
        statuses = _require_string_list(
            operation.get("response_statuses"), "invalid_response_statuses"
        )
        if len(statuses) != len(set(statuses)) or not all(
            _STATUS_PATTERN.fullmatch(item) for item in statuses
        ):
            raise ContractError("invalid_response_statuses")
        idempotency = operation.get("idempotency")
        if not isinstance(idempotency, str) or not _CODE_PATTERN.fullmatch(
            idempotency
        ):
            raise ContractError("invalid_operation_idempotency")
        fingerprint = operation.get("schema_fingerprint_sha256")
        if not isinstance(fingerprint, str) or not _SHA256_PATTERN.fullmatch(
            fingerprint
        ):
            raise ContractError("invalid_operation_fingerprint")
        operation_map[operation_id] = operation
    return operation_map


def _validate_operation_contracts(
    operation_map: Mapping[str, Mapping[str, Any]],
) -> None:
    for operation_id, expected in EXPECTED_OPERATION_CONTRACTS.items():
        operation = operation_map[operation_id]
        for field, expected_value in expected.items():
            if operation.get(field) != expected_value:
                raise ContractError(f"operation_contract_mismatch_{operation_id}_{field}")


def _validate_helper_bindings(
    operation_map: Mapping[str, Mapping[str, Any]],
) -> None:
    for operation_id, modes in PROJECTED_HELPER_COMMANDS.values():
        operation = operation_map[operation_id]
        if "agent_helper" not in operation["supported_surfaces"]:
            raise ContractError("helper_surface_mismatch")
        if any(mode not in operation["modes"] for mode in modes):
            raise ContractError("helper_mode_mismatch")
    projected_routes = {
        (operation["method"], operation["path"])
        for operation in operation_map.values()
    }
    for extension in LOCAL_EXTENSION_ROUTES.values():
        route = (extension["method"], extension["path"])
        if route in projected_routes:
            raise ContractError("local_extension_now_projected")


def _protected_workflow_map(
    contract: Mapping[str, Any], operation_map: Mapping[str, Mapping[str, Any]]
) -> dict[str, Mapping[str, Any]]:
    workflows = contract.get("protected_workflows")
    if not isinstance(workflows, list):
        raise ContractError("invalid_protected_workflows")
    actual: dict[str, Mapping[str, Any]] = {}
    for workflow in workflows:
        if not isinstance(workflow, Mapping):
            raise ContractError("invalid_protected_workflow")
        allowed_keys = _WORKFLOW_KEYS if "reusable_workflow_file" in workflow else {
            "workflow_file",
            "route",
        }
        _require_exact_keys(
            workflow, allowed_keys, "invalid_protected_workflow_keys"
        )
        workflow_file = workflow.get("workflow_file")
        route = workflow.get("route")
        if not isinstance(workflow_file, str) or not _WORKFLOW_PATTERN.fullmatch(
            workflow_file
        ):
            raise ContractError("invalid_workflow_file")
        if workflow_file in actual:
            raise ContractError("duplicate_workflow_file")
        if not isinstance(route, str) or not _PATH_PATTERN.fullmatch(route):
            raise ContractError("invalid_workflow_route")
        reusable = workflow.get("reusable_workflow_file")
        if reusable is not None and (
            not isinstance(reusable, str)
            or not _WORKFLOW_PATTERN.fullmatch(reusable)
        ):
            raise ContractError("invalid_reusable_workflow_file")
        if route not in {operation["path"] for operation in operation_map.values()}:
            raise ContractError("unknown_protected_workflow_route")
        actual[workflow_file] = workflow
    return actual


def _validate_protected_workflows(
    actual: Mapping[str, Mapping[str, Any]],
    operation_map: Mapping[str, Mapping[str, Any]],
) -> None:
    if set(actual) != set(EXPECTED_PROTECTED_WORKFLOWS):
        raise ContractError("unexpected_protected_workflows")
    for workflow_file, expected in EXPECTED_PROTECTED_WORKFLOWS.items():
        workflow = actual[workflow_file]
        operation = operation_map[expected["operation_id"]]
        if workflow.get("route") != operation["path"]:
            raise ContractError("protected_workflow_route_mismatch")
        if workflow.get("reusable_workflow_file") != expected.get(
            "reusable_workflow_file"
        ):
            raise ContractError("protected_workflow_binding_mismatch")


def _validate_operation_coverage(
    operation_map: Mapping[str, Mapping[str, Any]],
) -> None:
    helper_operations = {
        operation_id for operation_id, _modes in PROJECTED_HELPER_COMMANDS.values()
    }
    workflow_operations = {
        expected["operation_id"]
        for expected in EXPECTED_PROTECTED_WORKFLOWS.values()
    }
    read_only_operations = {"read_governance_projection"}
    categories = (helper_operations, workflow_operations, read_only_operations)
    covered: set[str] = set()
    for category in categories:
        if covered & category:
            raise ContractError("overlapping_operation_coverage")
        covered.update(category)
    if covered != set(operation_map):
        raise ContractError("incomplete_operation_coverage")


def _validate_semantic_artifact(
    artifact: Mapping[str, Any], *, verify_digest: bool
) -> tuple[dict[str, object], dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    _require_exact_keys(artifact, _ARTIFACT_KEYS, "invalid_artifact_keys")
    if artifact.get("schema_version") != SCHEMA_VERSION:
        raise ContractError("unsupported_schema_version")
    if artifact.get("normalization_version") != NORMALIZATION_VERSION:
        raise ContractError("unsupported_normalization_version")
    digest = artifact.get("semantic_digest_sha256")
    if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
        raise ContractError("invalid_semantic_digest")
    provenance = artifact.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ContractError("invalid_provenance")
    _require_exact_keys(provenance, {"source_commit_sha"}, "invalid_provenance_keys")
    source_sha = provenance.get("source_commit_sha")
    if not isinstance(source_sha, str) or not _SOURCE_SHA_PATTERN.fullmatch(source_sha):
        raise ContractError("invalid_source_provenance")
    contract = artifact.get("contract")
    if not isinstance(contract, Mapping):
        raise ContractError("invalid_contract")
    _require_exact_keys(contract, _CONTRACT_KEYS, "invalid_contract_keys")
    _validate_public_safety(artifact)
    operation_map = _operation_map(contract)
    workflow_map = _protected_workflow_map(contract, operation_map)
    invariants = contract.get("invariants")
    if not isinstance(invariants, Mapping):
        raise ContractError("invalid_invariants")
    if verify_digest and semantic_digest(artifact) != digest:
        raise ContractError("semantic_digest_mismatch")
    summary: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "semantic_digest_sha256": digest,
        "source_commit_sha": source_sha,
        "operation_count": len(operation_map),
        "protected_workflow_count": len(workflow_map),
    }
    return summary, operation_map, workflow_map


def validate_semantic_artifact(artifact: Mapping[str, Any]) -> dict[str, object]:
    summary, _operation_map_value, _workflow_map_value = _validate_semantic_artifact(
        artifact, verify_digest=True
    )
    return summary


def validate_contract(artifact: Mapping[str, Any]) -> dict[str, object]:
    summary, operation_map, workflow_map = _validate_semantic_artifact(
        artifact, verify_digest=False
    )
    if set(operation_map) != set(EXPECTED_OPERATION_CONTRACTS):
        raise ContractError("unexpected_operation_allow_list")
    _validate_operation_contracts(operation_map)
    _validate_helper_bindings(operation_map)
    _validate_protected_workflows(workflow_map, operation_map)
    _validate_operation_coverage(operation_map)
    contract = artifact["contract"]
    if contract.get("invariants") != EXPECTED_INVARIANTS:
        raise ContractError("invariant_contract_mismatch")
    if semantic_digest(artifact) != artifact["semantic_digest_sha256"]:
        raise ContractError("semantic_digest_mismatch")
    return {
        **summary,
        "projected_helper_command_count": len(PROJECTED_HELPER_COMMANDS),
        "local_extension_count": len(LOCAL_EXTENSION_ROUTES),
        "hermetic_only": True,
        "upstream_freshness_proven": False,
    }


def load_contract(path: Path = DEFAULT_CONTRACT_PATH) -> dict[str, Any]:
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("contract_unreadable") from exc
    if not isinstance(artifact, dict):
        raise ContractError("invalid_artifact")
    validate_contract(artifact)
    return artifact


@lru_cache(maxsize=1)
def _default_contract() -> dict[str, Any]:
    return load_contract()


def operation_path(operation_id: str) -> str:
    artifact = _default_contract()
    operations = artifact["contract"]["operations"]
    for operation in operations:
        if operation["operation_id"] == operation_id:
            return str(operation["path"])
    raise ContractError("unknown_operation_id")


def helper_command_path(command: str) -> str:
    projected = PROJECTED_HELPER_COMMANDS.get(command)
    if projected is not None:
        return operation_path(projected[0])
    extension = LOCAL_EXTENSION_ROUTES.get(command)
    if extension is not None:
        return extension["path"]
    raise ContractError("unknown_helper_command")
