# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

"""Local Launchplane HTTP caller bindings, never operation authority.

The producer owns contracts/agent-operator-contract.json. These paths are the
installed client's implementation; payload and response safeguards live in each
caller and launchplane_safety.py.
"""

from __future__ import annotations

import re

OPERATION_PATHS = {
    "prepare_testing_private_ingress": "/v1/products/{product}/testing-private-ingress",
    "record_disposable_lane_safety": "/v1/disposable-lanes/observations",
    "prepare_testing_replacement_resources": "/v1/products/{product}/testing-replacement-resources",
    "read_agent_context": "/v1/agent/context",
    "evaluate_agent_write_intent": "/v1/agent/write-intents/evaluate",
    "apply_product_config": "/v1/product-config/apply",
    "apply_odoo_addon_settings": "/v1/product-config/odoo-addon-settings/apply",
    "apply_odoo_website_settings": "/v1/product-config/odoo-website-settings/apply",
    "write_merge_train_controller_run_once": "/v1/work-graph/merge-train/controller/run-once",
    "remediate_preview_pr_feedback": "/v1/previews/pr-feedback/remediation",
}

HELPER_OPERATIONS = {
    **{"testing-replacement-resources-" + mode: "prepare_testing_replacement_resources"
       for mode in ("dry-run", "apply", "readback", "release")},
    "agent-context": "read_agent_context",
    "product-config-preflight": "evaluate_agent_write_intent",
    "product-config-dry-run": "apply_product_config",
    "product-config-apply": "apply_product_config",
    "odoo-addon-settings-dry-run": "apply_odoo_addon_settings",
    "odoo-addon-settings-apply": "apply_odoo_addon_settings",
    "odoo-website-settings-dry-run": "apply_odoo_website_settings",
    "odoo-website-settings-apply": "apply_odoo_website_settings",
    "merge-train-controller-run-once": "write_merge_train_controller_run_once",
    "preview-feedback-remediation": "remediate_preview_pr_feedback",
    "testing-private-ingress-dry-run": "prepare_testing_private_ingress",
    "testing-private-ingress-apply": "prepare_testing_private_ingress",
    "testing-private-ingress-readback": "prepare_testing_private_ingress",
    "disposable-lane-inspect": "record_disposable_lane_safety",
    "disposable-lane-collect": "record_disposable_lane_safety",
    "disposable-lane-record": "record_disposable_lane_safety",
    "disposable-lane-clear": "record_disposable_lane_safety",
    "disposable-lane-prepare-rebuild": "record_disposable_lane_safety",
}

LOCAL_EXTENSION_ROUTES = {
    "attention-events-read": {
        "method": "GET", "path": "/v1/attention/requests/events", "mode": "read",
    },
    "production-backup-preflight-read": {
        "method": "GET", "path": "/v1/production-backup-authority/preflight", "mode": "read",
    },
    "backup-worker-bindings-read": {
        "method": "GET", "path": "/v1/products/{product}/environments/prod/backup-worker-bindings", "mode": "read",
    },
    **{
        "backup-worker-bindings-" + mode: {
            "method": "POST", "path": "/v1/products/{product}/environments/prod/backup-worker-bindings/apply", "mode": mode,
        }
        for mode in ("dry-run", "apply")
    },
    **{
        "product-review-withdrawal-" + mode: {
            "method": "POST", "path": "/v1/product-review/withdrawal", "mode": mode,
        }
        for mode in ("dry-run", "apply", "readback")
    },
    "context-runtime-settings-read": {
        "method": "GET", "path": "/v1/contexts/{context}/runtime-settings", "mode": "read",
    },
    "instance-runtime-settings-read": {
        "method": "GET", "path": "/v1/contexts/{context}/instances/{instance}/runtime-settings", "mode": "read",
    },
    "context-secrets-read": {
        "method": "GET", "path": "/v1/contexts/{context}/secrets", "mode": "read",
    },
    "instance-secrets-read": {
        "method": "GET", "path": "/v1/contexts/{context}/instances/{instance}/secrets", "mode": "read",
    },
    "dokploy-inventory-read": {
        "method": "GET", "path": "/v1/dokploy/inventory", "mode": "read",
    },
    "dokploy-target-repair-domain-authority-apply": {
        "method": "POST", "path": "/v1/dokploy-targets/setup", "mode": "apply",
    },
    "ingress-route-authority-apply": {
        "method": "POST", "path": "/v1/drivers/ingress/route-apply", "mode": "apply",
    },
    "route-authority-discover": {
        "method": "GET", "path": "/v1/product-profiles/{product}", "mode": "read",
    },
    "route-binding-read": {
        "method": "GET", "path": "/v1/route-bindings/records/current", "mode": "read",
    },
    "route-binding-dry-run": {
        "method": "POST", "path": "/v1/route-bindings/reconcile", "mode": "dry-run",
    },
    "route-binding-apply": {
        "method": "POST", "path": "/v1/route-bindings/reconcile", "mode": "apply",
    },
    "dokploy-target-repair-domain-authority-dry-run": {
        "method": "POST",
        "path": "/v1/dokploy-targets/setup",
        "mode": "dry-run",
    },
    "ingress-route-dry-run": {
        "method": "POST",
        "path": "/v1/drivers/ingress/route-apply",
        "mode": "dry-run",
    },
    "health-monitoring-dry-run": {
        "method": "POST",
        "path": "/v1/product-profiles/health-monitoring/apply",
        "mode": "dry-run",
    },
    "health-monitoring-apply": {
        "method": "POST",
        "path": "/v1/product-profiles/health-monitoring/apply",
        "mode": "apply",
    },
    "privileged-policy-propose": {
        "method": "POST",
        "path": "/v1/agent/privileged-operations/plans",
        "mode": "propose",
    },
    "merge-train-policy-read": {
        "method": "GET",
        "path": "/v1/work-graph/merge-train/policy",
        "mode": "read",
    },
    "live-target-runtime-sync-dry-run": {
        "method": "POST",
        "path": "/v1/live-target-runtime/apply",
        "mode": "dry-run",
    },
    "live-target-runtime-sync-apply": {
        "method": "POST",
        "path": "/v1/live-target-runtime/apply",
        "mode": "apply",
    },
    "owner-review-read": {
        "method": "GET",
        "path": "/v1/product-review",
        "mode": "read",
    },
    "product-expected-config-dry-run": {
        "method": "POST",
        "path": "/v1/product-profiles/expected-config/apply",
        "mode": "dry-run",
    },
    "product-expected-config-apply": {
        "method": "POST",
        "path": "/v1/product-profiles/expected-config/apply",
        "mode": "apply",
    },
    "merge-train-policy-import-dry-run": {
        "method": "POST",
        "path": "/v1/merge-train/policies/import",
        "mode": "dry-run",
    },
    "merge-train-policy-import-apply": {
        "method": "POST",
        "path": "/v1/merge-train/policies/import",
        "mode": "apply",
    },
    "governance-projection-read": {"method": "GET", "path": "/v1/governance/projection", "mode": "read"},
    "repository-inventory-read": {
        "method": "GET",
        "path": "/v1/repository-inventory",
        "mode": "read",
    },
    "repository-inventory-dry-run": {
        "method": "POST",
        "path": "/v1/repository-inventory/apply",
        "mode": "dry-run",
    },
    "repository-inventory-apply": {
        "method": "POST",
        "path": "/v1/repository-inventory/apply",
        "mode": "apply",
    },
    "integration-allowances-read": {
        "method": "GET",
        "path": "/v1/product-config/integration-allowances",
        "mode": "read",
    },
    "integration-allowances-dry-run": {
        "method": "POST",
        "path": "/v1/product-config/integration-allowances/apply",
        "mode": "dry-run",
    },
    "integration-allowances-apply": {
        "method": "POST",
        "path": "/v1/product-config/integration-allowances/apply",
        "mode": "apply",
    },
    "testing-hold-read": {
        "method": "GET",
        "path": "/v1/product-config/testing-hold",
        "mode": "read",
    },
    "testing-hold-dry-run": {
        "method": "POST",
        "path": "/v1/product-config/testing-hold/apply",
        "mode": "dry-run",
    },
    "testing-hold-apply": {
        "method": "POST",
        "path": "/v1/product-config/testing-hold/apply",
        "mode": "apply",
    },
    "product-status-read": {"method": "GET", "path": "/v1/products", "mode": "read"},
    "product-environment-read": {
        "method": "GET",
        "path": "/v1/products/{product}/environments/{environment}",
        "mode": "read",
    },
    "product-profile-read": {
        "method": "GET",
        "path": "/v1/product-profiles/{product}",
        "mode": "read",
    },
    "repo-product-mapping-read": {
        "method": "GET",
        "path": "/v1/repo-product-mapping",
        "mode": "read",
    },
    "path-check": {
        "method": "GET",
        "path": "/v1/products/{product}/path-check",
        "mode": "read",
    },
    "protected-artifacts-read": {
        "method": "GET",
        "path": "/v1/artifacts/protected",
        "mode": "read",
    },
    "product-activity-read": {
        "method": "GET",
        "path": "/v1/products/{product}/activity",
        "mode": "read",
    },
    "preview-history-read": {
        "method": "GET",
        "path": "/v1/previews/{preview_id}/history",
        "mode": "read",
    },
    "reconcile-requests-read": {
        "method": "GET",
        "path": "/v1/product-profiles/{product}/reconcile-requests",
        "mode": "read",
    },
    "product-secret-bindings-read": {
        "method": "GET",
        "path": "/v1/products/{product}/secret-bindings",
        "mode": "read",
    },
    "target-replacement-operation-read": {
        "method": "GET",
        "path": "/v1/drivers/odoo/target-replacement/operations/{operation_id}",
        "mode": "read",
    },
    "target-replacement-plan-read": {
        "method": "POST",
        "path": "/v1/drivers/odoo/target-replacement-plan",
        "mode": "read",
    },
    "testing-rebuild-submit": {
        "method": "POST",
        "path": "/v1/drivers/odoo/target-replacement-apply",
        "mode": "apply",
    },
    "testing-rebuild-dry-run": {
        "method": "POST",
        "path": "/v1/drivers/odoo/target-replacement-plan",
        "mode": "read",
    },
    "workflow-rule-retirement-inspect": {
        "method": "POST",
        "path": "/v1/authz-policies/workflow-rule-retirement-plan",
        "mode": "read",
    },
    "workflow-rule-retirement-plan": {
        "method": "POST",
        "path": "/v1/authz-policies/workflow-rule-retirement-plan",
        "mode": "read",
    },
    "runtime-setting-removal-dry-run": {
        "method": "POST",
        "path": "/v1/admin/runtime-settings/removal",
        "mode": "read",
    },
    "runtime-setting-removal-readback": {
        "method": "POST",
        "path": "/v1/admin/runtime-settings/removal",
        "mode": "read",
    },
    "product-repository-identity-dry-run": {
        "method": "POST",
        "path": "/v1/product-profiles/repository-identity/apply",
        "mode": "dry-run",
    },
    "product-repository-identity-apply": {
        "method": "POST",
        "path": "/v1/product-profiles/repository-identity/apply",
        "mode": "apply",
    },
    "service-runtime-setting-dry-run": {
        "method": "POST",
        "path": "/v1/service/runtime-settings",
        "mode": "dry-run",
    },
    "service-runtime-setting-apply": {
        "method": "POST",
        "path": "/v1/service/runtime-settings",
        "mode": "apply",
    },
    "product-owner-dry-run": {
        "method": "POST",
        "path": "/v1/product-profiles/{product}/owner",
        "mode": "dry-run",
    },
    "product-owner-apply": {
        "method": "POST",
        "path": "/v1/product-profiles/{product}/owner",
        "mode": "apply",
    },
    "product-image-repository-dry-run": {
        "method": "POST",
        "path": "/v1/product-profiles/{product}/image-repository",
        "mode": "dry-run",
    },
    "product-image-repository-apply": {
        "method": "POST",
        "path": "/v1/product-profiles/{product}/image-repository",
        "mode": "apply",
    },
    "production-backup-authority-read": {
        "method": "GET",
        "path": "/v1/production-backup-authority",
        "mode": "read",
    },
    "production-backup-authority-dry-run": {
        "method": "POST",
        "path": "/v1/production-backup-authority/apply",
        "mode": "dry-run",
    },
    "production-backup-authority-apply": {
        "method": "POST",
        "path": "/v1/production-backup-authority/apply",
        "mode": "apply",
    },
    "dokploy-target-create-compose-dry-run": {
        "method": "POST",
        "path": "/v1/dokploy-targets/setup",
        "mode": "dry-run",
    },
    "dokploy-target-create-compose-apply": {
        "method": "POST",
        "path": "/v1/dokploy-targets/setup",
        "mode": "apply",
    },
    "dokploy-target-create-application-dry-run": {
        "method": "POST",
        "path": "/v1/dokploy-targets/setup",
        "mode": "dry-run",
    },
    "dokploy-target-create-application-apply": {
        "method": "POST",
        "path": "/v1/dokploy-targets/setup",
        "mode": "apply",
    },
    "dokploy-target-create-application-readback": {
        "method": "POST",
        "path": "/v1/dokploy-targets/setup",
        "mode": "readback",
    },
    "dokploy-target-create-application-retain": {
        "method": "POST",
        "path": "/v1/dokploy-targets/setup",
        "mode": "apply",
    },
    "dokploy-target-complete-compose-source-dry-run": {
        "method": "POST",
        "path": "/v1/dokploy-targets/setup",
        "mode": "dry-run",
    },
    "dokploy-target-complete-compose-source-apply": {
        "method": "POST",
        "path": "/v1/dokploy-targets/setup",
        "mode": "apply",
    },
    "dokploy-target-reconcile-compose-domain-dry-run": {
        "method": "POST",
        "path": "/v1/dokploy-targets/setup",
        "mode": "dry-run",
    },
    "dokploy-target-reconcile-compose-domain-apply": {
        "method": "POST",
        "path": "/v1/dokploy-targets/setup",
        "mode": "apply",
    },
    "dokploy-target-prune-compose-domain-dry-run": {
        "method": "POST",
        "path": "/v1/dokploy-targets/setup",
        "mode": "dry-run",
    },
    "dokploy-target-prune-compose-domain-apply": {
        "method": "POST",
        "path": "/v1/dokploy-targets/setup",
        "mode": "apply",
    },
    "private-health-endpoint-read": {
        "method": "GET",
        "path": "/v1/private-health-endpoints/records",
        "mode": "read",
    },
    "private-health-endpoint-dry-run": {
        "method": "POST",
        "path": "/v1/private-health-endpoints/apply",
        "mode": "dry-run",
    },
    "private-health-endpoint-apply": {
        "method": "POST",
        "path": "/v1/private-health-endpoints/apply",
        "mode": "apply",
    },
    "product-promotion-status-read": {
        "method": "GET",
        "path": "/v1/products/{product}/environments/{environment}/promotion-status",
        "mode": "read",
    },
    "product-promotion-dry-run": {
        "method": "POST",
        "path": "/v1/products/{product}/environments/{environment}/promotion/dry-run",
        "mode": "dry-run",
    },
    "generic-web-deploy-recovery-dry-run": {
        "method": "POST",
        "path": "/v1/admin/generic-web/deploy-recovery/dry-run",
        "mode": "dry-run",
    },
    "generic-web-deploy-recovery-reference-read": {
        "method": "GET",
        "path": "/v1/admin/generic-web/deploy-recovery/{product}/testing",
        "mode": "read",
    },
    "generic-web-deploy-recovery-apply": {
        "method": "POST",
        "path": "/v1/admin/generic-web/deploy-recovery/apply",
        "mode": "apply",
    },
}

INTERNAL_HELPER_ROUTES = {
    "testing-state-profile-write": {
        "method": "POST", "path": "/v1/product-profiles",
    },
    "generic-testing-rebuild-inputs": {
        "method": "GET", "path": "/v1/products/{product}/testing-rebuild/inputs",
    },
    "generic-testing-rebuild-submit": {
        "method": "POST", "path": "/v1/products/{product}/testing-rebuild",
    },
    "testing-rebuild-operation-read": {
        "method": "GET", "path": "/v1/products/{product}/testing-rebuild/operations/{operation_id}",
    },
    "route-authority-binding-list": {
        "method": "GET", "path": "/v1/route-bindings/records",
    },
    "route-authority-audit-read": {
        "method": "GET", "path": "/v1/ingress/route-audits/records/{record_id}",
    },
    "ingress-plan-edge-endpoint-read": {
        "method": "GET",
        "path": "/v1/edge-endpoints/records/{endpoint_key}",
    },
    "merge-train-policy-targets-read": {
        "method": "GET",
        "path": "/v1/work-graph/merge-train/policy-targets",
    },
    "dokploy-target-inspect": {
        "method": "GET",
        "path": "/v1/dokploy-targets/inspect",
    },
    "private-health-endpoint-record-read": {
        "method": "GET",
        "path": "/v1/private-health-endpoints/records/{endpoint_key}",
    },
}

# Top-level response fields each bounded GET read accepts. A field outside its
# set fails closed; an accepted field the read does not project is dropped.
# references/launchplane-read-response-fields.json pins the fields Launchplane
# declares for these routes, and the tests require each pinned set to fit here.
BOUNDED_READ_RESPONSE_FIELDS: dict[str, frozenset[str]] = {
    command: frozenset(fields)
    for command, fields in {
        "dokploy-inventory-read": {"status", "trace_id", "inventory"},
        "generic-web-deploy-recovery-reference-read": {
            "product", "context", "instance", "recovery_reference",
            "reservation_state", "reservation_attempt",
        },
        "integration-allowances-read": {
            "status", "trace_id", "records", "result", "replayed", "original_trace_id",
        },
        "merge-train-policy-read": {"status", "trace_id", "record"},
        "governance-projection-read": {"status", "trace_id", "projection"},
        "merge-train-policy-targets-read": {"status", "trace_id", "policy", "targets"},
        "path-check": {"status", "trace_id", "check"},
        "preview-history-read": {"status", "trace_id", "preview", "generations"},
        "private-health-endpoint-read": {
            "status", "trace_id", "product", "context", "instance", "limit", "count", "records",
        },
        "private-health-endpoint-record-read": {"status", "trace_id", "record"},
        "product-activity-read": {"status", "trace_id", "activity"},
        "product-environment-read": {
            "status", "trace_id", "environment",
            "host_network_capacity_available", "host_network_capacity",
        },
        "product-status-read": {"status", "trace_id", "products"},
        "product-profile-read": {"status", "trace_id", "profile"},
        "product-promotion-status-read": {"status", "trace_id", "promotion_status"},
        "product-secret-bindings-read": {
            "status", "trace_id", "product", "bindings", "coverage", "lane_binding_resolution",
        },
        "production-backup-authority-read": {"status", "trace_id", "authority", "policy_configuration"},
        "protected-artifacts-read": {"status", "trace_id", "protected_artifacts"},
        "reconcile-requests-read": {"status", "trace_id", "product", "requests"},
        "repository-inventory-read": {
            "status", "trace_id", "read_model", "config_authority_delivery",
        },
        "target-replacement-operation-read": {
            "status", "trace_id", "operation", "result", "recovery",
        },
        "testing-hold-read": {
            "status", "trace_id", "records", "result", "replayed", "original_trace_id",
        },
    }.items()
}


class ContractError(ValueError):
    """Public-safe binding validation error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_local_bindings() -> dict[str, object]:
    path_pattern = re.compile(
        r"^/v1/(?:[a-z0-9_.-]+|\{[a-z][a-z0-9_]*})"
        r"(?:/(?:[a-z0-9_.-]+|\{[a-z][a-z0-9_]*}))*$"
    )
    paths = (
        *OPERATION_PATHS.values(),
        *(route["path"] for route in LOCAL_EXTENSION_ROUTES.values()),
        *(route["path"] for route in INTERNAL_HELPER_ROUTES.values()),
    )
    for path in paths:
        if not isinstance(path, str) or not path_pattern.fullmatch(path):
            raise ContractError("invalid_local_route")
    for command in HELPER_OPERATIONS:
        helper_command_path(command)
    for command in ("dokploy-target-repair-domain-authority-dry-run", "ingress-route-dry-run"):
        route = LOCAL_EXTENSION_ROUTES.get(command, {})
        if route.get("method") != "POST" or route.get("mode") != "dry-run":
            raise ContractError("inert_planner_binding_mismatch")
    if LOCAL_EXTENSION_ROUTES["dokploy-target-repair-domain-authority-dry-run"]["path"] != LOCAL_EXTENSION_ROUTES["dokploy-target-create-compose-dry-run"]["path"]:
        raise ContractError("inert_planner_binding_mismatch")
    return {
        "helper_command_count": len(HELPER_OPERATIONS) + len(LOCAL_EXTENSION_ROUTES),
        "internal_helper_route_count": len(INTERNAL_HELPER_ROUTES),
        "hermetic_only": True,
        "upstream_freshness_proven": False,
    }


def operation_path(operation_id: str) -> str:
    try:
        return OPERATION_PATHS[operation_id]
    except KeyError:
        raise ContractError("unknown_operation_id") from None


def helper_command_path(command: str) -> str:
    caller = HELPER_OPERATIONS.get(command)
    if caller is not None:
        return operation_path(caller)
    route = LOCAL_EXTENSION_ROUTES.get(command)
    if route is not None:
        return route["path"]
    raise ContractError("unknown_helper_command")


def internal_helper_path(name: str) -> str:
    try:
        return INTERNAL_HELPER_ROUTES[name]["path"]
    except KeyError:
        raise ContractError("unknown_internal_helper_route") from None


def bounded_read_path(command: str) -> str:
    """The GET route a bounded read command calls, from either route table."""
    route = LOCAL_EXTENSION_ROUTES.get(command) or INTERNAL_HELPER_ROUTES.get(command)
    if route is None or route["method"] != "GET":
        raise ContractError("unknown_bounded_read")
    return route["path"]
